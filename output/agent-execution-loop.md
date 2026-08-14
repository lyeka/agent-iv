# 别把停止交给模型：从 PI 与 Claude Code 源码拆解 Agent 核心执行循环

假设一个 Coding Agent 正在修改代码。

模型先输出了半句解释，紧接着生成一个“执行数据库迁移”的工具调用。参数刚传到一半，网络断了。UI 上已经显示了一段文字，工具进程也许已经启动，但服务端没有返回完整的结束事件。系统准备切到备用模型重试——这时真正棘手的问题不是“再调用一次模型”这么简单，而是：刚才那次调用究竟算不算数？迁移是否已经执行？残缺参数能不能用？旧模型产生的工具调用 ID 能否交给新模型继续？用户此时按下中断键，又该由谁收尾？

这正是 **Agent loop（智能体执行循环）** 与普通聊天请求的区别。Agent loop 是一段反复调用模型、执行工具并把结果送回模型的控制程序；遇到网络中断或半条工具调用时，它还要判断工具是否已经执行、能否重试，以及是否应该停止。所谓 **副作用（side effect）**，指调用结束后会改变外部状态的操作，例如写文件、发邮件、扣款或执行数据库迁移。文本生成失败可以重来，副作用执行两次却可能造成真实损失。

本文分为两部分。第一部分沿着“最小循环—真实故障—生产级实现—可以直接实现的设计”的顺序，阅读 PI 与 Claude Code 的源码；第二部分把主要结论整理成一套适合面试口述的分析式回答。

> **本文使用哪些源码，哪些结论是推断**
>
> - PI 使用 `earendil-works/pi` commit `46bb9a2c3bdb296b0d2179f7309ec6b79a7f3106`。
> - Claude Code 使用本地保存的**非官方公开源码快照**，标注 commit `09f43552c76cb8856c4a5414f9aa9c9cda6ee035`。它不是 Anthropic 官方源码仓库，本文只把快照中可直接核对的代码当作实现证据。
> - 文中会明确区分“源码事实”“基于源码的工程推断”和“推荐设计”，不把后两者伪装成项目现状。

---

# 第一部分：源码如何实现，以及为什么这样实现

## 1. 先把四个容易混淆的层次分开

在读循环以前，先约定四个概念。它们日常都可能被含糊地叫作“一轮”，但在设计重试和预算时必须分开：

- **Run（一次运行）**：从用户提交任务到 Agent 最终结束的全过程。
- **Turn（交互轮次）**：一次模型响应，加上该响应触发的一批工具执行和工具结果。PI 的事件注释就是这样定义 turn 的。
- **Model attempt（模型请求尝试）**：为了拿到这一次模型响应而发出的一次网络请求。限流重试、流式转非流式，都可能让一个 turn 包含多个 attempt。
- **Tool batch（工具批次）**：同一条模型响应里出现的一组工具调用。它们可能串行，也可能在确认安全后并行。

这个区分看似只是命名，实际上决定了“重试谁”。网络超时通常只重试 model attempt；模型输出被截断，可能要重做一个 turn；已经执行过的付款工具，则不能因为 turn 重试而再付一次。

一个最小循环大致只有下面这些步骤：

```text
state = initial_state

while true:
    enforce_hard_limits(state)
    response = call_model(build_context(state))
    settled = finalize_response(response)

    if settled.failed_or_aborted:
        return terminal(settled.reason)

    calls = collect_complete_tool_calls(settled)
    if calls is not empty:
        results = validate_and_execute(calls)
        state.append(settled, results)
        continue

    if host_requires_continuation(state, settled):
        state.append_continuation_message()
        continue

    return terminal("completed")
```

真正的系统不过是在这副骨架上回答更多问题：`response` 还在流式传输时算不算状态？多个工具如何并发？模型说结束就结束吗？预算在哪一行检查？网络中断后，前一个 attempt 的半成品如何清理？

这些问题适合用 **状态机（state machine）** 建模。状态机是把“系统现在处于哪个阶段、这一阶段可以处理什么、处理后会进入哪个阶段”写成有限且可检查的规则。例如，只有“调用模型”结束后才能“处理模型响应”，不能拿尚未收完的参数直接执行工具。这里不要求引入专门的状态机框架，普通的类型、分支和循环也能实现。

先不展开网络重试、输出截断和中断恢复，只看一个简化但完整的 Agent loop：

```mermaid
stateDiagram-v2
    direction TB

    state "准备本轮" as PreparingTurn
    state "调用模型" as CallingModel
    state "处理模型响应" as HandlingResponse
    state "检查并执行工具" as RunningTools
    state "记录工具结果" as RecordingToolResults
    state "判断继续还是结束" as DecidingNextStep
    state "结束运行" as Terminal

    [*] --> PreparingTurn
    PreparingTurn --> CallingModel: 上下文和工具已经准备好
    CallingModel --> HandlingResponse: 收到本次响应
    HandlingResponse --> RunningTools: 响应中有工具调用
    HandlingResponse --> DecidingNextStep: 响应中没有工具调用
    RunningTools --> RecordingToolResults: 工具执行完成
    RecordingToolResults --> DecidingNextStep: 工具结果已经加入上下文
    DecidingNextStep --> PreparingTurn: 需要下一轮
    DecidingNextStep --> Terminal: 可以结束
    Terminal --> [*]
```

一次循环从准备上下文开始。运行时把已有消息、系统提示和当前可用工具整理好，然后调用模型。模型返回后，运行时读取响应内容，先看其中有没有工具调用。这一步不能只看模型给出的停止原因，因为真正决定下一步的是响应里实际出现了什么。

如果响应中有工具调用，运行时就检查参数和权限，执行工具，再把结果加入上下文。这里的结果不只包括成功值，也包括工具报错、参数不合法或调用被拒绝。模型必须在下一轮看到这些结果，才能继续判断任务是否完成，所以工具调用不是循环的终点，而是连接前后两个 turn 的中间步骤。

如果响应中没有工具调用，或者工具结果已经记录完成，运行时就判断是否还需要下一轮。需要继续时，系统带着更新后的上下文重新调用模型；不需要继续时，整个 run 结束。这个判断由运行时负责，模型的停止原因只是其中一个输入；上层应用新加入的消息、步数或预算限制也会改变结果。

因此，一个 Agent loop 的核心可以归纳为五步：准备上下文、调用模型、处理响应、执行并记录工具结果、判断是否继续。图中所谓“完整”，指这组步骤能够从开始走到结束，也能够在工具结果返回后进入下一轮。网络重试、输出截断、切换备用方案和用户中断会改变其中某些步骤的处理方式，后文再分别展开。

## 2. PI：先看一套足够小、又真实可用的核心实现

PI 的 [`packages/agent/src/agent-loop.ts`](../related-repos/pi/packages/agent/src/agent-loop.ts) 很适合用来理解基本循环。核心入口是 `runLoop`，模型流式处理集中在 `streamAssistantResponse`，工具处理则拆成 prepare、execute、finalize 三段。

### 2.1 一次 turn 到底发生了什么

`runLoop` 有内外两层循环：

1. 内层循环逐条处理等待插入的 steering message，调用模型，执行这次响应中的工具，再判断要不要进入下一轮。
2. 当模型原本准备结束时，外层循环再查看 follow-up 队列；若有新消息，就重新进入内层。

这里的 **steering message（转向消息）** 是运行过程中插入、用于改变当前方向的消息，例如用户在 Agent 工作时补一句“先别改配置”；follow-up 则是在 Agent 本来准备结束后才接续的新消息。这两个队列解释了一个容易被忽略的事实：模型认为自己答完了，不代表运行 Agent 的上层应用没有新工作。

PI 每个 turn 的主要顺序是：

```text
插入 pending message
→ streamAssistantResponse
→ 从最终消息中筛出 toolCall
→ 工具调用检查和执行
→ 把 toolResult 写回上下文
→ turn_end
→ prepareNextTurn / shouldStopAfterTurn
→ 拉取下一批 steering message
```

这不是凭注释推测，而是 `runLoop` 的直接控制流程。相应的生命周期会发出 `turn_start`、`message_start/update/end`、`tool_execution_start/update/end`、`turn_end` 和 `agent_end` 等事件。UI、日志和上层应用可以用这些事件显示进度或保存记录，但写回上下文的消息顺序不会因此改变。

### 2.2 流式片段不是最终事实

模型通常不是一次性返回完整响应，而是不断发来 text delta、thinking delta 和 tool-call delta。所谓 **delta（增量）**，就是“相对于上一时刻新增的那一小段内容”。

在 `streamAssistantResponse` 中，PI 收到 `start` 后，会把 partial message 临时放进上下文；收到各种 delta 后，用新 partial 替换最后一条；只有收到 `done` 或 `error`，才调用 `response.result()` 取得最终消息并覆盖临时值。

因此需要区分两类内容：

- 流式 partial 只供 UI 显示当前进度；
- final message 是 **canonical state**，也就是系统最终采用的那份完整消息。后续工具执行和持久化都以它为准。

这么做的理由很现实：工具参数在流式阶段可能只是 `{"path":"/tmp`，甚至尚未形成合法 JSON。把 partial 当成可执行命令，会让网络分包方式决定业务行为。

PI 的 [`packages/agent/README.md`](../related-repos/pi/packages/agent/README.md) 还特意提醒：低层 `agentLoop()` 发出的事件是 observational，也就是只用来观察进度；它不会等异步事件监听器全部处理完再继续。如果上层应用要求“消息监听处理完成后才能检查工具调用”，应使用更高层的 `Agent` 来保证这个先后顺序。监听器收到了事件，并不等于循环已经完成这一步。

### 2.3 决定继续的是实际工具调用，不是一个标签

PI 的 `AssistantMessage` 带有 `stopReason`，可取 `stop`、`length`、`toolUse`、`error`、`aborted` 等值。这里的 **provider（模型服务提供方）** 是实际接收请求并返回模型响应的 API 服务，例如 Anthropic API；运行时通常还会在它前面放一层 adapter（适配层），把不同服务返回的事件和错误转成 loop 能统一处理的格式。**Stop reason（停止原因）**是 provider 用来说明“这次生成为什么停止”的字段，例如自然结束、达到输出上限或准备使用工具。它很有用，但不能单独决定整个 Agent 是否结束。

`runLoop` 只把 `error` 和 `aborted` 直接视为终止错误。对于工具，它没有写成：

```text
if stopReason == "toolUse": execute tools
```

而是直接从 `message.content` 中筛出所有 `toolCall`。只要实际存在调用，就进入工具阶段；没有调用，循环才可能自然停下。也就是说，`stopReason` 说明 provider 为什么停止本次生成，响应中真实存在的 `toolCall` 才决定运行时是否执行工具。

Claude Code 在 [`src/query.ts`](../related-repos/claude-code/src/query.ts) 中把这个判断写得更直白：注释明确说 `stop_reason === 'tool_use'` 并不总是可靠，所以循环在流式过程中只要观察到真实 `tool_use` block，就设置 `needsFollowUp=true`；最终根据实际出现的 `tool_use` 决定是否继续。

这回答了“模型返回的停止信号可信吗”：

> 可以相信它描述了一次 provider 响应，但不能把它当成整个 Agent run 的最终决定。运行时还要检查响应中是否真的有工具调用、上层应用是否还有待处理消息、最终输出是否符合指定格式和业务要求，以及步数、token 和成本是否已经达到限制。

### 2.4 工具执行不是一个 `execute(args)` 就结束了

PI 把工具调用分成三个阶段，很值得借鉴：

1. **Prepare**：按名称找到工具，必要时整理参数，依据 schema 校验，再执行 `beforeToolCall` 权限或策略钩子。这里的 **schema（结构约束）** 描述参数允许有哪些字段、字段类型是什么、哪些字段必填；例如文件读取工具可以规定 `path` 必须是字符串，而且不能为空。
2. **Execute**：真正调用工具；工具可以持续上报进度。发生异常时，PI 会生成错误结果，保证原来的 `toolCall` 仍有对应的 `toolResult`。
3. **Finalize**：运行 `afterToolCall`，允许上层应用调整内容、错误标记、usage 或终止提示，最后生成 `toolResult` 消息。

这里的 **hook（钩子）** 是上层应用在固定处理步骤上提供的回调，例如在工具执行前检查权限、执行后删除敏感信息。它由程序执行，不是让模型自由发挥的提示词。

PI 支持串行和并行两种工具执行。如果配置要求串行，或者批次中任何一个工具声明 `executionMode="sequential"`，整批就串行；否则先检查所有调用，再用 `Promise.all` 并发执行。即使并行完成顺序不同，结果仍按原工具调用顺序回填，从而保持稳定的消息顺序。

工具还可以在结果中设置 `terminate=true`，表示“这个工具的结果本身就是终点，不必再花一轮让模型复述”。但 PI 的 `shouldTerminateToolBatch` 要求**批次中每一个结果都同意终止**。如果两个并行调用中，一个是“提交最终答案”，另一个仍需要模型解释错误，仅凭前者就结束会丢失后续工作。因此，只有整批结果都表示不需要下一轮时，PI 才结束。

### 2.5 低层 loop 故意不包办所有规则

PI 提供 `prepareNextTurn`，允许上层应用在一个 turn 结束后替换上下文、模型或 reasoning level（模型的推理强度）；又提供 `shouldStopAfterTurn`，让上层应用在工具全部结束、`turn_end` 已经发出之后增加停止条件。

它没有在最小 loop 里硬编码统一的步数、token 或成本限制。低层 loop 负责正确保存模型消息，并保证每个工具调用都有结果；运行 Agent 的上层应用负责产品侧的步数和预算规则。PI 仓库还包含一份 AgentHarness 的实现规范和未完成的代码框架，描述了进程中断后的恢复方向；但当前快照中的运行、恢复等主要入口仍未实现，因此本文只把它作为设计资料，不把其中的方案写成 PI 已经提供的能力。

## 3. 从 `stop_reason` 到下一步：模型停了，Agent 不一定结束

拿到一次模型响应后，运行时首先要判断它是最终回答、工具请求、截断内容还是拒绝；处理完这次响应后，还要判断 Agent 是否有事情需要下一 turn，以及是否允许再次调用模型。本节就沿着这条顺序展开。

下面使用 Anthropic Messages API 举例，因为 Claude Code 直接使用这套消息格式，PI 也包含对应的 Anthropic adapter。不同 LLM API 的具体字段并不统一：有些使用 `stop_reason`，有些通过响应状态、结束原因、输出条目或流式结束事件表达相近信息；工具调用的字段和结束事件也可能不同。实现多模型 Agent 时，需要为每个 provider 编写转换层。可以复用的是后面的判断顺序，不能直接复用 Anthropic 的字段枚举。

### 3.1 先看 Anthropic：`stop_reason` 只说明这次生成为什么停

例如，Anthropic Messages API 可能返回下面这条响应：

```json
{
  "stop_reason": "tool_use",
  "content": [
    {
      "type": "tool_use",
      "id": "toolu_01",
      "name": "read_file",
      "input": { "path": "app.yaml" }
    }
  ]
}
```

模型此时已经停止生成，但任务显然没有完成。`tool_use` 表示模型正在等待应用读取工具调用、执行 `read_file`，再把结果放进下一次请求。Anthropic 的[停止原因说明](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)列出了下面几种常见情况：

| Anthropic `stop_reason` | 这次生成发生了什么 | 应用接下来做什么 |
|---|---|---|
| `end_turn` | 模型到达自然停止点 | 检查实际输出和待处理消息；满足整个 run 的完成条件后才能结束 |
| `tool_use` | 模型正在等待客户端工具结果 | 从 `content` 中读取完整的 `tool_use` block，校验、执行并返回 `tool_result` |
| `max_tokens` / `model_context_window_exceeded` | 响应被输出上限或上下文窗口截断 | 进入截断恢复，不能执行可能残缺的工具调用 |
| `stop_sequence` | 命中了调用方设置的停止序列 | 查看具体命中的序列，并按它在应用中的用途处理 |
| `pause_turn` | Anthropic 的服务端工具循环达到单次请求的迭代上限 | 保留已有 assistant 内容和工具配置，再发一次请求让服务端工具循环继续 |
| `refusal` | 模型拒绝完成请求 | 读取拒绝信息，返回拒绝或按产品策略切换模型，不能当作普通格式错误反复重试 |

这里的 `pause_turn` 是 Anthropic 服务端工具特有的返回值，不是 Agent loop 通用的“暂停状态”。客户端工具仍以 `tool_use` 返回，由应用执行并提交 `tool_result`。

`stop_reason` 属于成功返回的 Messages API 响应。网络超时、连接失败等情况可能根本没有完整响应，自然也没有可用的 `stop_reason`，这些故障要交给第 8 节的请求重试处理。未知停止原因也不能默认当作 `end_turn`：PI 的 [`mapStopReason`](../related-repos/pi/packages/ai/src/api/anthropic-messages.ts) 对无法识别的值直接抛错，避免 provider 新增枚举后被旧程序误判成正常完成。

### 3.2 为什么不能只写 `switch (stop_reason)`

`stop_reason` 是重要输入，但它不能代替对实际响应内容的检查。PI 与 Claude Code 都体现了这一点：

| 实现 | 源码实际做法 | 说明 |
|---|---|---|
| PI Anthropic adapter | `mapStopReason` 把 `end_turn`、`max_tokens`、`tool_use`、`refusal` 等值转换成 PI 内部的 `stop`、`length`、`toolUse` 和 `error` | 多 provider 系统需要先把不同 API 的返回方式转换成内部统一表示 |
| PI `runLoop` | 完成转换后，仍从 `message.content` 中筛选真实的 `toolCall` | 内部停止原因也不能代替实际内容检查 |
| PI 截断处理 | `stopReason === "length"` 时不执行消息中的工具调用，而是为整批调用生成错误结果 | 停止原因和实际内容必须结合判断 |
| Claude Code `queryLoop` | 源码明确注明 `stop_reason === 'tool_use'` 不总可靠；观察到真实 `tool_use` block 才设置 `needsFollowUp` | 是否进入工具流程应以真实工具块为准 |
| [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/running_agents/) | 只有已经得到最终输出并且不存在工具调用时才结束；存在工具调用就执行并把结果加入下一轮 | 不同 provider 的字段不同，但都要检查模型实际返回了什么 |

因此，provider 的结束字段用来解释这次生成为什么停，模型实际返回的内容决定接下来要处理什么。多模型 Agent 可以先通过 adapter 把不同 provider 的返回格式转换成内部表示，但转换后的状态仍要与真实文本、工具调用和错误内容一起判断。

### 3.3 从一次模型响应，上升到整个 run 的继续与结束

3.1 解释了 provider 为什么停止生成，3.2 进一步说明 adapter 不能只转换停止字段，还要检查实际返回的内容。不过，这两步仍然只解决“一次模型请求该怎样处理”。生产级 Agent 还要继续向外判断：当前 turn 是否已经处理完整，整个 run 是否还有后续工作，以及步数、预算和用户操作是否允许再次调用模型。

下面这张图把三个范围套在一起：一个 run 包含多个 turn，一个 turn 又可能包含多个 model attempt。它是结合 PI 与 Claude Code 整理出的工程抽象，不是任一项目的原样 API。

```mermaid
flowchart TB
    subgraph RUN["一个 run：从收到任务到最终结束"]
        direction TB
        MODEL_ERROR(["整个 run 结束<br/>model_error"])

        subgraph TURN["一个 turn：得到一条模型响应，并处理它触发的工具"]
            direction TB

            subgraph ATTEMPT["一次 model attempt：向 provider 发起一次请求"]
                direction LR
                CALL["调用 provider"] --> ADAPTER["转换停止信息<br/>检查实际 content"]
                ADAPTER --> RESPONSE{"当前响应能继续处理吗？"}
                RESPONSE -- "截断或临时失败" --> RETRY["恢复当前请求<br/>发起新的 model attempt"]
                RETRY -. "仍在当前 turn" .-> CALL
                RESPONSE -- "无法恢复" --> MODEL_ERROR
                RESPONSE -- "可以" --> CONFIRMED["确认模型响应"]
            end

            CONFIRMED --> TOOLS["处理完整的工具调用"]
            TOOLS --> RESULTS["记录每个工具结果<br/>没有工具则直接完成 turn"]
        end

        RESULTS --> WORK{"还有下一 turn 的工作吗？"}
        MORE["工具结果还需分析<br/>steering / follow-up 还有消息<br/>Stop hook 要求修复"] -. "影响判断" .-> WORK
        WORK -- "没有" --> DONE(["正常完成<br/>completed"])
        WORK -- "有" --> ALLOWED{"允许再调用一次模型吗？"}
        LIMITS["maxTurns<br/>累计 token / 成本<br/>工具、策略或上层应用明确停止"] -. "限制下一次请求" .-> ALLOWED
        ALLOWED -- "不允许" --> LIMITED(["按具体原因结束<br/>max_turns / token_budget / cost_budget / policy"])
        ALLOWED -- "允许" --> NEXT["开始下一 turn<br/>重新进入 model attempt"]

        CANCEL["全程都可能发生<br/>用户取消或不可恢复的内部错误"]
        CANCEL -. "中断模型请求、等待或工具" .-> CLEANUP["停止当前工作<br/>补上必要的中断结果"]
        CLEANUP --> ABORTED(["结束<br/>user_aborted / runtime_error"])
    end
```

最里面的 model attempt 只负责拿到一条可用的模型响应。网络临时失败、输出截断或 fallback 可能产生新的 attempt，但它们不会自动开始新的 turn。响应可以继续处理后，程序才进入 turn 的其余部分：检查实际工具调用，执行完整调用，并为每个调用记录成功、失败、拒绝或中断结果。普通工具失败通常也是一个 `tool_result`，不等于整个 run 失败。

当前 turn 处理完整后，run 先判断还有没有事情需要模型处理。工具结果需要分析、steering 或 follow-up 队列还有消息、Stop hook 要求修复，都会产生下一 turn；这些情况都不存在时才是正常完成。确实需要下一 turn 时，程序才检查 `maxTurns`、累计 token、成本和停止策略，决定是否允许再次发起模型请求。

用户取消不属于上述顺序中的某一步。它可能发生在模型生成、退避等待或工具执行期间，因此必须能立即传给当前请求和可取消工具；退出前还要为已经记录的工具调用补上中断结果。用户追加消息则相反：它会增加待处理工作，通常推动 loop 进入下一 turn。

#### 生产级 loop 在决定继续或结束时检查什么

下面的表格不是简单罗列“停止原因”，而是把可能要求恢复、继续或结束的条件放在一起。阅读时要注意四个区别：`max_tokens` 表示单次响应被截断，不是整个 run 的 token 预算；用户追加消息和用户取消的作用相反；普通工具失败通常交给模型继续处理；只有已经没有后续工作时，才可以把结果称为 `completed`。

| 影响范围 | 需要检查的情况 | 检查时间 | 运行时怎样处理 | 典型结果 |
|---|---|---|---|---|
| Model attempt | 单次响应截断或临时请求失败 | 收到截断响应或请求失败时 | 不执行残缺工具调用；重试、要求模型继续生成或 fallback | 通常仍在当前 turn，不直接结束 run |
| Model attempt | 拒绝或无法恢复的模型错误 | 当前请求处理完成后 | 保留具体错误并停止自动恢复 | `model_error` 或产品定义的拒绝结果 |
| Turn | 工具结果还需分析、steering/follow-up 还有消息、Stop hook 要求修复 | 工具结果全部记录后 | 标记仍需要下一 turn | 如果运行限制允许，则继续 |
| Turn | 没有工具结果要处理、没有待处理消息，也没有修复要求 | 当前 turn 完整处理后 | 正常返回最终输出 | `completed` |
| Run 全程 | 用户取消 | 模型生成、退避等待或工具执行期间均可发生 | 取消当前工作，为已记录的工具调用补上中断结果 | `user_aborted` |
| 下一 turn 前 | `maxTurns` 已达到 | 当前 turn 完成后、下一次模型请求前 | 不再发起模型请求 | `max_turns` |
| 下一 turn 前 | 累计 token 达到限制 | 更新本轮 usage 后 | 不再发起模型请求 | `token_budget` |
| 下一 turn 前 | 累计成本达到限制 | 更新本轮费用后 | 不再发起模型请求；可能出现一次请求范围内的小幅超限 | `cost_budget` |
| Turn 或 run | 工具明确要求结束整个 run | 工具结果记录完成后 | 根据工具执行器与上层 loop 的约定结束 | `tool_terminated` |
| Run | 策略、Stop hook 或上层应用明确禁止继续 | 检查最终输出或准备下一次请求时 | 保留禁止原因并结束 | `policy_blocked` |
| Run 全程 | 无法恢复的内部运行错误 | 错误发生时 | 停止当前工作，完成必要清理后结束 | 推荐记录为 `runtime_error` |

表中的检查位置可以在源码中找到对应依据。PI 先记录工具结果并发出 `turn_end`，再调用 `shouldStopAfterTurn`，steering 和 follow-up 还能增加下一轮工作；它也会让 `error` 和 `aborted` 直接结束当前路径。Claude Code 提供了 `max_turns`、`aborted_streaming`、`aborted_tools` 和 Stop hook 等具体结果，并在 [`QueryEngine`](../related-repos/claude-code/src/QueryEngine.ts) 中使用 `maxBudgetUsd` 阻止后续请求。

累计 token 硬上限，以及统一的 `token_budget`、`cost_budget`、`runtime_error` 等结束原因，是基于这些源码提出的推荐设计，不是 PI 或 Claude Code 的原样 API。Claude Code 的 `taskBudget`、本地 `tokenBudget` 和单次 `max_tokens` 也有不同用途，不能合并成同一个限制。PI 工具结果里的 `terminate` 已在 2.4 解释；它是否结束整个 run，仍取决于工具执行器和外层循环的约定。

## 4. 模型响应被截断后怎样处理

模型响应触及输出 token 上限后，Agent loop 首先要判断已经收到的内容能否继续使用。普通文本没有写完，和工具参数没有生成完整，不能采用同一种处理方式。

### 4.1 普通文本和工具调用的风险不同

普通文本被截断，通常只是表达没有完成。只要没有产生工具副作用，程序可以保留已有文本，再通过新的模型请求继续生成。工具调用被截断则不同：工具名称或参数可能不完整，运行时拿到的对象即使能够解析，含义也可能已经改变。

例如模型实际想生成：

```json
{"path":"db.sql","mode":"dry-run"}
```

截断后只留下 `{"path":"db.sql"}`。如果 `mode` 在 schema 中可选，解析和校验仍可能通过，但默认模式也许会直接修改文件。此时不能根据“JSON 可以解析”推断调用是完整的。

因此，普通文本可以继续生成；残缺工具调用不能执行，也不能靠补括号、补字段或拼接两段 JSON 恢复。

### 4.2 PI 怎样处理截断

PI 收到因长度限制而结束的模型响应时，只要其中包含工具调用，就不会执行这一批工具。它会为每个调用返回一条错误结果，说明参数可能被截断，要求模型重新发出完整调用。

这里选择整批不执行，而不是只丢掉最后一个调用，是因为不同 provider 组装并行工具调用的方式可能不同。运行时如果无法证明前面的调用已经完整，整批作废虽然会多用一轮模型请求，却能保证没有工具拿着不确定的参数开始执行。

### 4.3 Claude Code 怎样处理截断

Claude Code 在模型响应因输出上限而截断、又没有完整工具调用等待执行时，先保持原输入不变，用更高的单次输出额度重新发起模型请求。如果仍然截断，它会保留已经生成的 assistant 内容，再加入一条“从中断处继续，不要道歉或复述”的消息，然后重新调用模型。该源码快照把这种恢复限制在最多三次。

这里的**续写**有一个很具体的含义：程序把已经生成的文本放进对话历史，再发起一次新的模型请求，让模型从上次停止的位置接着表达。它不是恢复已经结束的网络数据流，新的内容也是另一条 assistant 消息；更不能把这种方式用于拼接前后两段工具参数。

Claude Code 只会把已经完整结束的工具块交给执行器。截断前已经完整生成的工具块可以继续处理；最后一个没有完整结束的工具块不会被当成可执行调用。文本续写也不会被用来补齐这个工具块。

### 4.4 从两种实现中可以归纳的规则

下面是基于 PI 与 Claude Code 实现整理出的工程建议，不是两个项目现有的公共接口：

1. `max_tokens` 表示当前模型响应不完整，不直接表示整个 run 应当结束。
2. 普通文本可以通过新的模型请求继续生成。
3. 工具参数不能通过补 JSON 或拼接两次模型输出来恢复。
4. 只有能够证明完整的工具调用才能执行。如果消息格式不能可靠标出每个调用是否完整，就让整批调用失败并重新生成。
5. 提高输出额度、继续生成和重新请求都必须有最大次数或总耗时限制，避免模型一直产生新的截断响应。

## 5. Fallback 发生时怎样处理旧 attempt

**Fallback（降级切换）**指主要方案不可用时换到备用方案，负责在普通重试仍无法恢复时继续任务。例如流式请求失败后改用非流式请求，或主模型持续过载后切换备用模型。它和普通重试的差别在于：执行方式或模型身份发生了变化，旧 attempt 留下的消息、工具调用和模型专属信息未必能直接复用。

### 5.1 PI：模型切换由上层应用决定

PI 的核心 Agent loop 遇到模型错误或用户取消时，会结束当前运行，不会自动挑选备用模型并重放失败的 attempt。因此，如果把 fallback 专指“模型请求失败后，由 loop 自动切换备用模型”，PI 没有提供一套内建的统一机制。

不过，PI 并非完全不支持模型切换。它的 AI 层能够把不同 provider 产生的历史消息转换成对方可以接收的格式，同一会话可以在后续请求中改用另一模型；运行 Agent 的上层应用也可以在一个 turn 完成后替换下一轮使用的模型。个别 provider adapter 还会处理自身的传输方式降级，例如 WebSocket 不可用时改走 SSE。

所以更准确的说法是：PI 提供跨 provider 继续会话和由上层应用更换模型的能力，但核心 loop 没有统一的、由失败自动触发的 run 级 fallback。何时切换、选择哪个备用模型，以及怎样处理失败 attempt 留下的状态，需要由上层应用决定。

### 5.2 Claude Code：清理旧 attempt 后再切换

Claude Code 处理了两类自动 fallback。第一类是流式请求失败后改用非流式请求。切换发生时，程序会把 UI 和会话记录中已经显示的半成品标记为无效，清空旧 attempt 收集的消息、工具调用和工具结果，并停止使用旧的工具执行器，防止旧调用 ID 的结果混入新响应。

这里使用的 **tombstone（作废标记）**，是一条“这条已显示消息已经失效”的通知。UI 和会话记录程序收到后不再使用旧消息，但它不会删除已经写入的文件，也不会撤销已经发送给外部服务的请求。

第二类是主模型持续过载后切换备用模型。程序会先为旧消息中缺少结果的工具调用补上失败结果，再清空旧响应和工具状态、切换模型，并删除不能交给备用模型的思考签名。

**Thinking signature（思考签名）**是 provider 用来证明某段思考内容完整、未被篡改的模型相关元数据。它可能只适用于生成该内容的模型或 API 格式，把主模型签名原样交给备用模型会触发请求校验错误。因此切模型不只换一个字符串，还要清理不能跨模型使用的信息。

这些操作解决的是旧 attempt 在消息、UI 和工具调度状态中留下的内容，不能撤销已经发生的外部操作。

### 5.3 工具可能已经执行时，不能直接重放请求

Claude Code 的源码已经明确识别出重复执行风险：流式阶段可能已经启动工具，非流式 fallback 又可能生成相同的调用，导致工具执行两次。它提供的通用处理不是自动撤销第一次操作，而是允许关闭这条 fallback，让错误直接返回。这个做法是在无法保证安全时停止自动重放。

Claude Code 能够处理的边界很明确：如果工具尚未开始，可以清理旧 attempt 后重新请求；如果工具可能已经开始，就关闭这条自动 fallback 并返回错误。源码没有提供一套适用于任意工具的通用恢复机制，也不能撤销已经发生的文件写入或外部请求。

## 6. 流式响应在 Agent loop 中怎样处理

第 2.2 节已经说明，模型生成中的 partial message 不能直接当成最终消息。本节再向前一步，只回答流式响应与核心 loop 有关的问题：内容分批到达时，什么时候只能展示，什么时候可以确认一个工具调用完整，什么时候可以处理最终响应；如果中途失败，又该怎样根据工具是否已经开始执行来处理。

这里不讨论 SSE、WebSocket 等传输协议。它们负责怎样把数据送到客户端，而 Agent loop 更关心收到的数据已经完整到什么程度，以及当前允许执行哪些动作。

### 6.1 先分清三种容易混淆的“流式”

| 概念 | 分批返回的内容 | 对 Agent loop 的影响 |
|---|---|---|
| 模型响应流 | 文本、思考内容和工具参数片段 | 片段可以更新 UI，但不能直接作为最终消息或工具输入 |
| 工具进度流 | 命令输出、下载进度等执行过程 | 进度可以实时展示，最终工具结果仍需单独记录 |
| 提前执行工具 | 模型响应尚未结束，完整工具块到达后就开始执行 | 工具可能提前改变外部状态，因此中断和 fallback 更难处理 |

前两种说的是“数据分批到达”，第三种说的是“什么时候开始产生实际操作”，含义并不相同。本节重点讨论模型响应流和提前执行工具；工具进度流只用于展示执行过程，不在这里扩展成 Tool 设计问题。

### 6.2 用一次具体响应说明三个完成程度

下面是一组用于说明概念的事件。不同 LLM API 的事件名称和分块方式并不统一，provider adapter 需要先把它们转换成 loop 能处理的内部事件。

```text
text_delta("先读取配置")
tool_call_delta('{"path":"app')
tool_call_delta('.yaml"}')
tool_call_end(read_file, {"path":"app.yaml"})
response_end(...)
```

| 完成程度 | 此时已经知道什么 | 可以做什么 | 不能做什么 |
|---|---|---|---|
| 收到普通 delta | 只知道新增加的一小段内容 | 更新 UI 中的临时消息 | 写成最终消息或执行工具 |
| 工具参数仍在生成 | JSON 可能还不完整 | 缓存片段并继续拼装 | 校验或执行调用 |
| 完整工具块已经结束 | 工具名和参数已经完整接收 | 校验参数；是否提前执行取决于 loop 的策略 | 把整个 assistant 响应当成已经结束 |
| 整个响应结束 | 最终文本、工具块和停止信息都已确定 | 用最终消息替换临时内容，并处理工具和下一步 | 继续使用先前的半成品消息 |

因此，“流式接收模型响应”和“收到一个 delta 就执行”是两回事。原始参数片段没有独立的业务含义；至少要等一个工具块完整结束，运行时才有条件检查工具名称、参数格式和权限。

### 6.3 PI 与 Claude Code 选择了不同的工具启动时间

PI 采用等待完整响应的方式。流式片段不断更新临时 assistant 消息，整个响应结束后，最终消息替换临时消息；loop 随后才从最终消息中找出工具调用并开始执行。这样做的好处是，模型流中断时通常还没有工具产生外部操作；代价是即使第一个工具块已经完整到达，也要等模型生成完整条响应。

Claude Code 可以更早启动工具。模型响应仍在生成时，只要一个完整工具块已经到达，就可以把它加入执行队列；真正执行前仍要检查工具是否存在、参数是否符合 schema，以及权限是否允许。并发安全的工具可以一起执行，必须单独执行的工具则继续排队。这里的 **并发安全** 是指多个工具同时运行不会互相破坏状态，例如并行读取两个文件通常安全，同时改写同一个配置文件通常不安全。

仍以读取 `app.yaml` 为例：PI 会等到 `response_end` 后才执行 `read_file`；Claude Code 可以在 `tool_call_end` 后开始读取，同时继续接收模型后面的内容。后者减少了等待时间，但运行时必须记录每个工具是仍在排队、正在执行、已经完成还是已经取消，否则中断后无法知道应该补什么结果。

### 6.4 中断后按照工具是否已经开始处理

| 中断发生时 | PI 的处理特点 | Claude Code 的处理特点 | 后续原则 |
|---|---|---|---|
| 只有文本或残缺工具参数，工具尚未开始 | 将本次模型响应记为错误或取消并结束运行，不执行其中的工具 | 不执行没有完整结束的工具块 | 尚未产生工具副作用，可以按第 8 节重试请求，或按第 5 节清理后 fallback |
| 完整工具块已经出现，但工具仍在排队 | PI 不会在模型流阶段进入这种状态 | 阻止尚未开始的工具继续执行，并为已经记录的调用生成取消或错误结果 | 处理完旧调用后才能重新请求 |
| 工具正在执行时发生取消 | PI 只会在完整响应后的工具阶段遇到这种情况；取消信号会传给工具，已经处理的调用仍会得到结果 | 向可取消工具发送取消信号，收集剩余结果，并为没有正常完成的调用生成错误结果 | 取消只能阻止后续工作，不能保证撤销已经发生的外部操作 |
| 工具已经完成，但整个模型流随后失败 | PI 等完整响应后才启动工具，通常不会出现这个时间顺序 | 工具可能已经产生副作用，消息作废也无法撤销 | 停止自动重放，并保留这次中断或错误的原因 |

从 PI 源码可以确认的是：流式片段只更新临时消息，最终响应会替换它；只有最终响应可用后才进入工具阶段；模型响应以错误或取消结束时，不会执行其中的工具调用。

从 Claude Code 源码可以确认的是：完整工具块可以在模型响应结束前进入执行队列；流式阶段取消时，程序会处理排队和执行中的工具，为已经出现的调用生成必要结果；模型流阶段中断和后续工具阶段中断也会记录成不同的结束原因。它能够清理消息和调度状态，但不能撤销已经完成的文件写入或外部请求。

在此基础上，可以归纳出三条工程建议。这些不是 PI 或 Claude Code 的公共 API：原始 delta 不能直接产生副作用；提前执行工具时要记录每个调用的执行状态；只要无法确认工具是否已经改变外部状态，就不能自动重放整个 attempt。

## 7. 结构化输出不合规时怎样处理

**Structured output（结构化输出）**要求模型结果符合指定的数据结构。合法 JSON 只说明语法正确，不代表字段、类型和必填项符合 JSON Schema；运行时仍要校验模型真正提交的数据。

Claude Code 使用专门的结果提交工具接收并校验数据。模型没有调用该工具，或者提交的数据不合规时，程序会把具体错误交给模型修复，同时限制最大尝试次数，避免一直重复“结束—拒绝结束—再次生成”。

不同失败要分开处理：schema 本身非法时，在请求模型前直接失败；模型结果不匹配时，反馈校验错误并有限修复；输出被截断时，按第 4 节处理；refusal 或 API 错误则按拒绝或请求错误处理，不能伪装成普通格式问题。

## 8. 模型请求怎样重试

PI 与 Claude Code 的通用重试主要针对模型 API 请求，不负责自动重试任意工具。模型响应因 `max_tokens` 被截断时，按第 4 节继续生成或重新生成；工具已经开始执行时，也不能直接套用模型请求的重试策略。

### 8.1 哪些错误可以重试

| 情况 | 是否自动重试 | 原因 |
|---|---|---|
| 网络中断、连接重置、请求超时 | 通常可以 | 属于可能自行恢复的传输故障 |
| 429、503、529 和部分 5xx | 通常可以 | 限流或服务暂时过载 |
| 凭证过期 | 有条件 | 只有刷新凭证成功后才能重试 |
| 400、参数或 schema 错误 | 不可以 | 原请求不修改就会再次失败 |
| 配额、余额或 billing 耗尽 | 不可以 | 重试不会改变账户状态 |
| refusal、安全或业务拒绝 | 不可以 | 这是有效结果，不是瞬时故障 |
| 用户取消 | 不可以 | 用户已经明确要求停止 |

PI 与 Claude Code 覆盖的具体状态码和特殊场景不同，但共同做法是只重试可能自行恢复的请求错误，并排除用户取消、配额问题和其他确定性错误。

### 8.2 重试必须受到哪些限制

1. 优先遵守服务端通过 `Retry-After` 指定的等待时间。
2. 使用 **指数退避（exponential backoff）**，让等待时间随失败次数逐步增加；必要时加入 **jitter（随机抖动）**，避免大量客户端同时再次请求。
3. 同时设置最大重试次数和总时长，不能因为单次等待有限就无限循环。
4. 退避等待必须响应用户取消。
5. 同一种错误只交给 SDK、provider adapter 或 Agent loop 中的一层重试，避免各层重试次数相乘。

AWS 的[重试控制最佳实践](https://docs.aws.amazon.com/wellarchitected/latest/framework/rel_mitigate_interaction_failure_limit_retries.html)同样强调限制重试次数、使用退避，以及避免多层重试叠加。PI 与 Claude Code 的具体参数不同，但都体现了错误分类、次数限制和用户可取消这些基本要求。

如果模型流式生成期间已经提前启动工具，或者无法确认工具是否已经执行，就不再自动重试整个模型请求。PI 核心 loop 和 Claude Code 都没有提供适用于任意工具的通用幂等保证；具体工具能否安全重试，属于工具和下游服务自身的能力，本文不再展开。

## 9. 把前面的结论写成可实现的状态机

前面从简单循环一路讲到截断、fallback 和持久化，最后可以用三组类型明确记录循环当前走到哪里、为什么结束：

```ts
type LoopPhase =
  | "preparing_turn"
  | "calling_model"
  | "finalizing_response"
  | "preparing_retry"
  | "checking_tool_calls"
  | "running_tools"
  | "recording_tool_results"
  | "repairing_interrupted_state"
  | "deciding_next_step"
  | "terminal"

type TerminalReason =
  | "completed"
  | "model_error"
  | "runtime_error"
  | "max_turns"
  | "token_budget"
  | "cost_budget"
  | "user_aborted"
  | "tool_terminated"
  | "policy_blocked"

type LoopState = {
  phase: LoopPhase
  turn: number
  attempt: AttemptState | null
  pendingToolCallIds: Set<string>
  cumulativeUsage: { inputTokens: number; outputTokens: number; cost: number }
  terminalReason?: TerminalReason
}
```

这些类型不是 PI 或 Claude Code 的原样 API，而是根据两份实现整理出的建议数据结构。字段名可以调整，但程序至少要单独记录当前步骤、turn、model attempt、待处理工具 ID、累计用量和结束原因。这样出错后才能直接判断可以重试哪一步，不必从消息数组和十几个布尔变量中反推。`preparing_retry` 表示同一 turn 内准备重试模型请求；`repairing_interrupted_state` 表示工具可能已经执行，此时停止自动重放并记录中断原因。这两个阶段属于生产环境中的异常处理，所以没有放进第 1 节的简化主图。`runtime_error` 用于循环程序或工具执行器自身无法恢复的错误，避免把它误记为模型错误。这里的 `tool_terminated` 是推荐设计中的 run 级结束原因，只有工具或上层应用明确要求结束整个 run 时才使用，不能直接等同于 PI 工具结果里的 `terminate` 字段。

判断是否继续可以按下面的顺序进行：

```text
1. 先处理用户取消和不可恢复错误
2. 再为缺少结果的工具调用补上错误结果，或者明确将调用作废
3. 若有完整工具调用，先检查参数和权限，再执行并记录整批结果
4. 这一 turn 的响应和工具结果都处理完后，再检查步数、token、成本，以及工具或上层应用是否禁止下一轮
5. 运行 stop hook，检查结构化输出、测试结果或合规要求
6. 检查 steering / follow-up 队列
7. 全部通过，才接受模型的自然结束
```

无论具体框架怎样变化，下面四件事都必须保证：

1. **每个已经记录的工具调用最终都有对应结果。** 成功、失败、拒绝和中断都不能留下只有调用、没有结果的消息。
2. **尚未完整收完的流式内容不能直接改变外部状态。** 只有工具调用已经完整生成，并且重复执行也不会造成额外影响时，才允许提前执行。
3. **已经产生副作用的 turn 不能盲目重放。** 如果通用 loop 无法确认工具结果，就停止自动重试并保留具体错误。
4. **任何重复调用模型或工具的分支都必须能停下来。** 步数、token、成本、重试次数、总时长或用户中断，至少有一种能结束循环。

到这里，核心 loop 已不再是一句“不断调用模型直到它说完成”，而是一段负责控制执行顺序的程序：模型提出下一步，执行引擎检查工具调用，避免外部操作被错误地执行或重复执行，保存工具结果，再决定是否真的继续。

---

# 第二部分：面试场景中的分析式回答

## 10. 3—5 分钟完整回答

如果面试官问“Agent 的核心执行循环怎么设计”，可以按下面的思路展开：

> Agent loop 不应被理解成“模型没说 stop 就一直 while”。分析这类系统时，首先要把一次运行拆成 run、turn、model attempt 和 tool batch 四层：一次 turn 是一条模型响应加上它触发的一批工具；一次 turn 里可能因为网络重试包含多个模型 attempt。这样发生错误时，才能明确应该重试的是网络请求、整个模型轮次，还是某个工具，而不是笼统地“再来一次”。
>
> 状态机通常可分成准备本轮、调用模型、处理模型响应、检查并执行工具、记录工具结果、判断是否继续和结束运行几个阶段。流式 delta 可以用于 UI 实时展示，但工具只能读取已经完整生成并通过 schema 校验的参数。无论工具成功、失败、被权限检查拒绝还是被用户中断，都要为原来的 tool call 生成一个结果，不能让下一轮只看到调用、看不到结果。
>
> 模型停止生成后，先看 provider 返回的停止信息，再看实际输出内容。以 Anthropic 为例，`end_turn` 表示自然停止，`tool_use` 表示等待客户端工具结果，`max_tokens` 表示响应被截断；其他 LLM API 的字段和值可能不同，需要先由 adapter 转换。这一步仍只决定当前 model attempt 怎样处理：临时失败或截断可能在同一 turn 内产生新的 attempt，无法恢复才按错误结束。拿到可用响应后，程序还要处理完整个 turn，为每个工具调用记录结果；随后判断工具结果、steering、follow-up 和 Stop hook 是否产生了下一 turn。确实需要继续时，最后检查 `maxTurns`、累计 token、成本和策略是否允许再次调用模型。没有后续工作是正常完成，有后续工作但受到限制则要带着具体原因结束；用户取消则可以在上述任意阶段中断当前工作。
>
> 模型响应被截断时，要区分普通文本和工具调用。普通文本可以提高输出额度，或者把已有文本放进对话历史，再通过一次新的模型请求继续生成；这不是恢复旧数据流。工具调用如果没有完整生成，就不能靠猜测补 JSON 后执行：PI 会让这一批调用全部失败并要求模型重发，Claude Code 只把已经完整结束的工具块交给执行器。
>
> PI 支持上层应用在后续 turn 更换模型，也能转换不同 provider 的历史消息，但核心 loop 不会在模型失败后自动选择备用模型。Claude Code 提供了自动 fallback：切换前会把 UI 中的半成品标记为无效，为缺少结果的工具调用补上失败结果，清空旧 attempt 的消息和工具状态，并移除不能跨模型使用的思考签名。如果工具尚未开始，清理后可以重新请求；如果工具可能已经执行，源码提供的通用防护是关闭不安全的自动 fallback，让错误直接返回。
>
> 流式处理要区分三个完成程度：delta 只能更新 UI；完整工具块已经具备校验条件，但整个 assistant 响应可能仍未结束；完整响应才能替换临时消息并用于后续判断。PI 会等完整响应后再执行工具，Claude Code 则可以让完整工具块提前进入执行队列。如果中断发生在工具启动前，清理当前请求后可以重试；如果工具正在执行或已经完成，就要先取消仍可取消的工作、记录调用结果，并确认是否已经产生外部操作，不能直接重放整个 attempt。结构化输出则要用 strict schema 或专门的提交结果工具，在运行时再次校验，失败后把具体错误反馈给模型，但必须设置最大修复次数，并单独处理 refusal 和 token 截断。
>
> 最后是重试。瞬时错误和确定性错误必须分开处理：网络失败、超时、429、部分 5xx 可以在尚未执行工具时重试模型请求，同时尊重 `Retry-After`，使用指数退避加 jitter，并限制最大次数或总时长；用户取消还要能打断等待。参数错误、配额耗尽、安全拒绝不应该机械重试。同一种故障只交给一个层级重试，避免 SDK、provider 和 Agent 各自重试三次。工具一旦可能已经执行，就不再自动重试整个模型请求。
>
> 所以，核心 loop 可以归纳为：模型负责提出下一步，执行引擎负责检查工具调用、保存工具结果、限制循环次数，并决定是否真的继续。

这段回答先分清 run、turn、model attempt 和 tool batch，再讲正常循环、停止条件、故障处理和重试边界。即使面试官中途打断，也可以从任一部分继续展开。

## 11. 高频追问

### 追问一：一个 loop 里具体干哪几件事？状态机怎么建模？

一次 turn 通常包含：准备上下文和工具定义、调用模型并逐段接收流式响应、确认最终响应、提取完整工具调用、校验参数与权限、串行或并行执行工具、按原顺序记录结果、累计 usage，最后判断继续或终止。推荐单独记录 `phase`、`turn`、`attemptId`、`responseSettled`、`effectsStarted`、待处理工具 ID 和累计预算，不从消息数组临时反推所有状态。

PI 的 `runLoop` 清楚标出了模型响应、工具批次和一个 turn 在哪里结束；Claude Code 的 `queryLoop` 则用 `State` 在多次循环之间保留上下文压缩记录、截断重试次数、turn count 和 transition reason（为什么切换到下一阶段）。消息历史负责保存对话内容，“程序现在执行到哪一步”则最好另用状态字段记录。

### 追问二：模型返回的停止信号到底可不可信？

停止信号可以相信，但只能相信它描述的那一层。以 Anthropic 为例，`end_turn` 表示模型自然停止生成，`tool_use` 表示模型在等待客户端工具结果，`max_tokens` 表示这次响应被截断；它们都没有直接回答整个 Agent run 是否完成。其他 LLM API 的字段和值可能不同，所以多模型 Agent 通常先用 adapter 转换，再进入统一的 loop。

转换后的停止原因也不能代替内容检查。PI 仍从 content 中筛选真实 `toolCall`；Claude Code 也根据实际 `tool_use` block 设置 `needsFollowUp`，源码注释还明确指出单独依赖 `stop_reason === 'tool_use'` 不可靠。处理完实际内容和工具结果后，运行时再判断是否还需要下一 turn；如果需要，还要检查步数、预算和用户取消是否允许继续。

### 追问三：几类终止条件分别在哪一层拦？

可以沿着 model attempt、turn 和 run 三个范围回答。单次输出的 `length` 或 `max_tokens` 属于 model attempt：它表示当前响应不完整，恢复程序可以提高额度、续写或重新请求；只有无法恢复时才按模型错误结束，不能把它当成整个 run 的 token 预算。

进入 turn 后，程序先处理实际工具调用并记录每个结果。工具结果还需分析、steering 或 follow-up 还有消息、Stop hook 要求修复，都表示需要下一 turn；这些条件都不存在时才是 `completed`。普通工具报错一般作为 `tool_result` 交给模型，不会直接结束 run。工具或策略明确要求停止时，也要先记录工具结果，再按约定返回 `tool_terminated` 或 `policy_blocked`。

确定仍有后续工作后，run 才在下一次模型请求前检查 `maxTurns`、累计 token 和成本；达到限制就分别以 `max_turns`、`token_budget` 或 `cost_budget` 结束。用户取消和无法恢复的内部错误不必等待上述检查，可以在模型生成、退避等待或工具执行期间中断；退出前仍要取消当前工作，并为已经记录的工具调用补上必要的中断结果。

PI 的顺序是先记录工具结果并发出 `turn_end`，再执行 `shouldStopAfterTurn`，随后检查 steering 和 follow-up。Claude Code 同样在工具结果完成后检查 `maxTurns`，`QueryEngine` 再根据已经返回的 usage 累计 `maxBudgetUsd`。因此，这些限制的作用不是把当前 turn 截断，而是阻止下一次模型请求。

### 追问四：模型响应被截断，文本和工具调用分别怎么办？

普通文本可以先提高单次输出额度；仍然不够时，把已经生成的内容放回对话历史，再发起新的模型请求，让模型从中断处继续。这里的续写不是恢复旧数据流，而是一次带着已有文本的新请求，因此必须限制次数。

工具调用要严格得多。没有完整生成的工具参数不能执行，也不能把两段 JSON 拼起来执行。PI 无法证明同批调用是否完整时，会让整批调用失败并要求模型重发；Claude Code 则只把已经完整结束的工具块交给执行器，未完成的最后一个工具块不会执行。

### 追问五：主模型失败切备用模型，半成品状态怎么处理？

先区分项目能力。PI 支持上层应用更换模型并转换跨 provider 的历史消息，但核心 loop 不会因为一次模型请求失败就自动切换备用模型。Claude Code 会自动切换流式与非流式路径，也能在主模型过载时改用备用模型；切换前会作废 UI 中的半成品、为缺少结果的工具调用补失败结果、清空旧 attempt 的消息和工具状态，并删除不能交给备用模型的思考签名。

完成这些清理后，还要看工具是否已经执行。工具尚未开始，可以重新请求；工具可能已经开始时，就必须关闭自动 fallback 并返回错误，不能直接重放整个 turn。Claude Code 实现了旧状态清理和关闭不安全 fallback，但没有提供适用于任意工具的通用副作用恢复机制。

### 追问六：流式输出如何和 loop 结合？

先区分模型响应流、工具进度流和提前执行工具。模型响应中的 delta 只适合更新 UI；工具参数仍在生成时只能继续拼装；完整工具块到达后才有条件校验工具、参数和权限；整个响应结束后，最终消息才能替换临时内容并用于后续判断。工具进度流只是展示执行过程，不能代替最终工具结果。

PI 和 Claude Code 的主要区别是工具启动时间。PI 等整个模型响应结束后才执行工具，因此模型流中断时通常还没有工具副作用。Claude Code 可以在完整工具块到达后提前执行，从而减少等待时间，但必须记录工具是排队、执行中、完成还是取消。

中断处理也由这个状态决定。工具尚未启动时，可以清理当前请求后重试；工具仍在排队时，要阻止它开始并生成取消或错误结果；工具正在执行时，只能取消支持取消的工作，并收集已经完成或失败的结果；工具已经产生外部操作时，消息清理无法撤销副作用，不能直接重放整个 attempt。Claude Code 实现了排队和执行状态的清理以及必要的工具结果生成，但没有进一步提供通用恢复机制。

### 追问七：结构化输出不合规怎么办？

合法 JSON 不代表符合 schema，运行时仍要检查字段、类型和必填项。Claude Code 使用专门的结果提交工具校验数据；模型没有提交或格式错误时，把具体问题交给模型修复，并限制次数。Schema 本身非法要在请求前失败；输出截断、refusal 和 provider error 则按各自原因处理，不能都当成格式错误。

### 追问八：哪些模型请求错误可以重试，退避和上限怎样设计？

只重试有较大概率自行恢复的模型请求错误，例如网络断开、超时、限流和部分 5xx。参数或 schema 错误、配额耗尽、安全拒绝和用户取消不自动重试；凭证过期只有刷新成功后才能重试。

等待时间优先采用服务端的 `Retry-After`，否则使用指数退避和 jitter，并同时限制最大次数和总时长。用户取消必须能打断等待；同一种错误也只能由 SDK、provider adapter 或 Agent loop 中的一层负责重试，避免次数相乘。PI 与 Claude Code 的具体规则不同，但都遵循这组基本判断。工具可能已经执行时，不再自动重试整个模型请求；通用 loop 不负责解决任意工具的重复执行问题。

### 追问九：如果只能保留几条设计原则，应当保留什么？

可以保留四条：第一，响应里实际出现的工具调用比 stop reason 更能决定下一步；第二，流式片段和最终确认的消息分开处理；第三，每个工具调用必须有结果，已经执行过外部操作的 turn 不能直接重放；第四，所有重复调用模型或工具的分支都必须受步数、预算、次数、总时长或用户取消限制。

这四条能覆盖绝大多数实现差异。框架会变、模型 API 会变，但“不要执行残缺工具参数、不要重复修改外部状态、不要留下只有调用却没有结果的消息、不要无限循环”不会变。

## 12. 结语

最小 Agent loop 的确可以写成几十行：调用模型、执行工具、回填结果、继续循环。PI 展示了怎样把这组核心步骤写得清楚；Claude Code 则展示了产品进入真实环境后，截断、流式 fallback、预算、hook、并行工具和中断分别需要增加哪些判断和处理步骤。

理解源码的目的，不是背下 `runLoop` 或 `queryLoop` 的函数名，而是弄清三组差别：完整响应和残缺响应怎样处理，模型建议结束和运行时真正结束怎样区分，以及哪些请求可以重试、哪些外部操作不能重复执行。

把这些规则写成明确的状态和判断以后，核心执行循环才真正成立：

> **模型负责提出下一步，执行引擎负责检查工具调用、保存工具结果、限制循环次数，并决定是否真的继续。**

---

## 参考源码与一手资料

### PI（commit `46bb9a2c3bdb296b0d2179f7309ec6b79a7f3106`）

- [`packages/agent/src/agent-loop.ts`](../related-repos/pi/packages/agent/src/agent-loop.ts)：`runLoop`、`streamAssistantResponse`、`failToolCallsFromTruncatedMessage`、`executeToolCalls*`、`shouldTerminateToolBatch`。
- [`packages/agent/src/types.ts`](../related-repos/pi/packages/agent/src/types.ts)：`AgentLoopConfig`、`shouldStopAfterTurn`，以及工具结果中 `terminate` 字段的含义。
- [`packages/ai/src/api/anthropic-messages.ts`](../related-repos/pi/packages/ai/src/api/anthropic-messages.ts)：Anthropic `stop_reason` 到 PI 内部停止原因的 `mapStopReason` 转换。
- [`packages/ai/src/types.ts`](../related-repos/pi/packages/ai/src/types.ts)：`StopReason`、`AssistantMessage` 和流事件类型。
- [`packages/ai/README.md`](../related-repos/pi/packages/ai/README.md)：跨 provider handoff、历史消息转换和切换模型后的上下文兼容。
- [`packages/ai/src/utils/retry.ts`](../related-repos/pi/packages/ai/src/utils/retry.ts)：`retryAssistantCall`、`isRetryableAssistantError`。
- [`packages/agent/docs/harness.md`](../related-repos/pi/packages/agent/docs/harness.md) 与 [`packages/agent/src/harness/agent-harness.ts`](../related-repos/pi/packages/agent/src/harness/agent-harness.ts)：AgentHarness 的实现规范、类型框架，以及当前快照中尚未实现的主要运行入口。

### Claude Code 非官方公开源码快照（标注 commit `09f43552c76cb8856c4a5414f9aa9c9cda6ee035`）

- [`src/query.ts`](../related-repos/claude-code/src/query.ts)：`queryLoop`、跨迭代 `State`、工具驱动续轮、截断恢复、fallback 清理、turn 上限与 stop hook。
- [`src/QueryEngine.ts`](../related-repos/claude-code/src/QueryEngine.ts)：会话状态、usage/成本、结构化输出结果和重试上限。
- [`src/services/api/claude.ts`](../related-repos/claude-code/src/services/api/claude.ts)：流式消息组装、最终 stop reason、输出上限错误和流式转非流式 fallback。
- [`src/services/api/withRetry.ts`](../related-repos/claude-code/src/services/api/withRetry.ts)：错误分类、退避、jitter、`Retry-After`、529 策略与模型 fallback。
- [`src/services/tools/StreamingToolExecutor.ts`](../related-repos/claude-code/src/services/tools/StreamingToolExecutor.ts)：流式工具调度、并发约束、顺序回填和中断结果。
- [`src/tools/SyntheticOutputTool/SyntheticOutputTool.ts`](../related-repos/claude-code/src/tools/SyntheticOutputTool/SyntheticOutputTool.ts) 与 [`src/utils/hooks/hookHelpers.ts`](../related-repos/claude-code/src/utils/hooks/hookHelpers.ts)：动态 JSON Schema 校验与结构化输出 Stop hook。

### 官方资料

- Anthropic：[Stop reasons and fallback](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)、[Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)。
- OpenAI：[Agents SDK agent loop](https://openai.github.io/openai-agents-python/running_agents/)、[Function calling](https://developers.openai.com/api/docs/guides/function-calling)、[Structured model outputs](https://developers.openai.com/api/docs/guides/structured-outputs)。
- AWS Well-Architected Framework：[Control and limit retry calls](https://docs.aws.amazon.com/wellarchitected/latest/framework/rel_mitigate_interaction_failure_limit_retries.html)。
