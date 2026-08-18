# 模型没有调用函数：从 PI 与 Claude Code 看一次 Tool Call 如何闭环

用户要求 Agent 查看仓库状态时，模型并不会直接启动 Shell。它先看到一份 Bash Tool 的调用说明，再生成一段结构化数据，表达“我想以这些参数调用这个工具”。真正查找工具、判断权限并启动进程的是 Agent Runtime。

以 Anthropic Messages API 的客户端工具为例，第一次请求会把 Tool Definition 和用户消息一起发给模型。下面省略了 model、max_tokens 等无关字段：

~~~json
{
  "tools": [
    {
      "name": "bash",
      "description": "Run a shell command and return its output.",
      "input_schema": {
        "type": "object",
        "properties": {
          "command": { "type": "string" }
        },
        "required": ["command"]
      }
    }
  ],
  "messages": [
    {
      "role": "user",
      "content": "查看当前仓库状态"
    }
  ]
}
~~~

模型可能返回：

~~~json
{
  "role": "assistant",
  "stop_reason": "tool_use",
  "content": [
    {
      "type": "tool_use",
      "id": "toolu_01",
      "name": "bash",
      "input": {
        "command": "git status --short"
      }
    }
  ]
}
~~~

这段 `tool_use` 不是函数已经执行的通知，而是一条调用意图。宿主执行命令以后，还要保留上面的 Assistant Message，并紧接着追加一条带相同调用 ID 的结果消息：

~~~json
{
  "role": "user",
  "content": [
    {
      "type": "tool_result",
      "tool_use_id": "toolu_01",
      "content": " M src/app.ts"
    }
  ]
}
~~~

模型在下一轮看到 `tool_result`，才会继续判断应该读取文件、修改代码还是直接回答用户。不同模型 API 对角色和字段的命名并不完全相同，但客户端 Tool 都要完成相同的闭环：

`Tool Definition → 模型生成 Tool Call → 宿主校验、授权与调度 → 执行 → Tool Outcome → 下一轮模型请求`

本文把宿主在一次调用结束后形成的完整结果统称为 **Tool Outcome**。它不是某个协议规定的字段名，而是成功内容、错误状态、结构化数据和宿主元数据的总称。

接下来主要对照 [PI commit `46bb9a2c`](https://github.com/earendil-works/pi/tree/46bb9a2c3bdb296b0d2179f7309ec6b79a7f3106) 与 Claude Code 公开源码快照。Claude Code 快照标注 commit `09f43552`，来自 2026 年 3 月公开暴露的 Source Map；本文只使用其中可以直接核对的客户端实现，不据此推断 Anthropic 未公开的服务端逻辑。

# 第一部分：一次 Tool Call 怎样闭环

## 1. 模型没有调用函数，只生成调用意图

前面的三段 JSON 对应三个不同对象：

- **Tool Definition** 告诉模型有哪些能力，以及应该生成什么形状的参数。
- **Tool Call** 是模型根据当前对话生成的调用意图，包含调用 ID、工具名和参数。
- **Tool Outcome** 是宿主处理这次意图后的结果，并不等于工具函数的原始返回值。

三者之间隔着两个责任边界。模型根据 Definition 生成 Call；宿主把 Call 转换为真实执行，再把执行信息规范化为 Outcome。模型没有接触 Bash 进程，也看不到 JavaScript 或 TypeScript 的执行函数。

~~~mermaid
sequenceDiagram
    participant R as Agent Runtime
    participant M as Model
    participant B as Bash Process

    R->>M: Messages + Bash Tool Definition
    M-->>R: tool_use(id, name, input)
    Note over R: 查找、校验、授权与调度
    R->>B: 执行 git status --short
    B-->>R: stdout / stderr / exit status
    Note over R: 规范化为 Tool Outcome
    R->>M: History + tool_result(id, content)
    M-->>R: 继续调用工具或生成最终回答
~~~

这张图也解释了为什么 Tool 设计不能只讨论函数签名。名称和 Schema 影响模型能否提出正确请求；权限与调度决定请求能否变成外部动作；结果回填决定模型能否在下一轮使用刚获得的事实。任何一段缺失，调用都没有闭环。

本文重点讨论由 Agent 宿主执行的客户端 Tool。对于 Web Search 等服务端 Tool，执行器可能位于模型服务商一侧，但“模型提出调用、某个运行时执行、结果返回模型”的责任分离仍然存在。

## 2. 模型看到的是调用说明，不是完整执行器

Tool Definition 的直接消费者是模型。对自定义客户端 Tool，最核心的信息是：

- `name`：稳定、可唯一定位的调用名；
- `description`：什么时候使用以及能力边界；
- `input_schema`：允许哪些参数、类型和必填关系。

Provider 还可能支持 Output Schema、Strict Mode、延迟加载或协议 Annotation。它们仍然在描述模型如何选择工具、如何构造参数或如何理解结果，没有包含真正的执行代码。

宿主需要的信息更多，但不必全部发送给模型。运行时要知道怎样执行和取消、怎样报告进度、多久算超时；策略层要判断参数语义、权限、副作用和并发安全；结果层还要规定怎样生成模型消息、怎样渲染 UI、保存哪些日志以及允许占用多少上下文。把这些信息全部塞进模型可见的 JSON 只会增加 Token，并不会自动形成安全边界。

PI 对这些消费者进行了分层。底层 `Tool` 主要保存名称、描述和参数 Schema；`AgentTool` 再增加参数准备、执行函数和执行模式；Coding Agent 的扩展定义继续增加提示片段与 UI Renderer。Claude Code 的产品运行时更集中，`Tool` 类型同时暴露 `call`、`isReadOnly(input)`、`isConcurrencySafe(input)`、`checkPermissions`、结果映射和 Renderer。两种组织方式不同，却说明了同一件事：Tool 系统需要的能力取决于运行时职责，而不是所有字段都属于一份发给模型的 Definition。

### 模型描述可以生成，但不应该每轮漂移

PI 的底层 `Tool.description` 是静态字符串。Claude Code 的接口允许计算两种说明：`prompt(options)` 生成模型可见描述，`description(input, options)` 可以根据本次输入生成权限确认和 UI 文案。源码进一步用 Session 级 Schema Cache 固定 `prompt()` 首次生成的基础定义，后续请求只叠加延迟加载、缓存控制等请求级属性。

这种区分比简单选择“静态还是动态”更准确：

- 根据操作系统、租户能力或已安装插件在会话初始化时生成一次，能让描述符合真实环境；
- 根据本次命令生成“将读取仓库状态”一类确认文案，能帮助用户判断具体动作；
- 每轮改写模型可见描述，则会改变模型面对的接口，造成 Prompt Cache 失效、行为漂移和会话难以复现。

因此，默认策略应是：**模型可见定义在一次会话或稳定目录版本内保持不变；输入相关的解释放在审批与 UI 层；实时环境状态通过普通上下文或查询 Tool 提供。**

### 工具目录可以变化，但当前 Turn 必须有稳定快照

运行中增删 Tool 可以解决三类问题：权限变化时隐藏能力，插件或 MCP Server 连接后引入新能力，以及在大目录中延迟加载暂时用不到的 Schema。OpenAI Agents SDK 的 `is_enabled`、Anthropic Tool Search 和 Claude Code 的 MCP 刷新都属于这类能力。

动态目录不能在模型已经产生 Call 后悄悄换一版。PI 在运行开始时复制当前 Tool 数组，Coding Agent 在下一 Turn 的准备阶段更新 Active Tools；Claude Code 也在当前 Tool Batch 结束后调用 `refreshTools()`，新连接的 MCP Tool 从下一轮开始可见，正在执行的批次继续使用创建 Executor 时的定义。

一个可复现的实现至少需要：

- 当前 Turn 使用不可变目录快照；
- 目录变化只影响下一 Turn，并记录稳定名称或目录版本；
- 已经生成的 Call 按产生它的快照解释；
- Definition 使用确定性顺序。

最后一点不是为了暗示模型优先选择排在前面的 Tool。Anthropic 的请求缓存把 Tools 放在 System Prompt 和 Messages 之前；同一组工具每轮随机排序会改变请求前缀。稳定顺序主要服务于 Prompt Cache 和问题复现，业务路由仍应依靠名称、描述、Schema 和当前任务。

Definition 到这里完成了自己的职责：帮助模型产生结构化 Call。但结构合法不代表操作应该执行，执行权仍在宿主。

## 3. 宿主把调用意图变成受控执行

模型生成的 `tool_use` 和用户提交的表单一样，都是需要验证的不可信输入。生产级执行路径不是 `execute(args)` 一步，而是一条有顺序的 Gate：

`查找 Tool → Schema 校验 → 语义校验 → 判断副作用 → Allow / Ask / Deny → 调度 → 执行`

查找与 Schema 校验只能确认“存在名为 Bash 的 Tool，command 是字符串”。它们不能判断 `git status --short` 是否越权，也不能证明另一条命令没有副作用。语义判断必须查看解析后的具体参数。

### 副作用是本次调用的属性

同一个 Bash Tool 可以承载完全不同的动作：

- `git status --short` 读取仓库状态，不修改工作区；
- `printf ... > config.yaml` 会覆盖文件；
- `rm -rf build` 会删除目录；
- `curl https://example.com` 虽然可能只读，却访问开放网络。

因此，只读、破坏性、幂等性和 Open World 访问是四个不同维度。一个写操作可以幂等，但仍有副作用；一个只读操作也可能把敏感查询发到外部系统。权限系统需要根据本次参数、用户规则和执行环境作出 `allow`、`ask` 或 `deny`，不能只相信 Tool 名称。

Claude Code 正是按输入实现这些判断。Bash Tool 的 `isReadOnly(input)` 分析具体命令，`isConcurrencySafe(input)` 只有在本次调用被判为只读时才返回真；执行器在调用 `call` 前还会完成 Schema 校验、Tool 语义校验、PreToolUse Hook 和权限裁决。远端 MCP Tool 带来的 `readOnlyHint`、`destructiveHint` 等信息会参与展示或分类，但最终仍进入宿主的权限流程。

PI 的边界更小。核心循环在执行前完成 Tool 查找、参数准备、Schema 校验和 `beforeToolCall` Hook，应用可以在 Hook 中阻止调用；但 PI 明确不内置文件系统、进程、网络和凭据沙箱，默认继承启动进程的权限。Hook 是接入 Policy 的位置，不等于系统已经拥有隔离边界。

MCP 规范也把 Tool Annotation 定义为 Hint，并要求客户端不能根据不可信 Server 的声明作安全决定。更稳妥的原则是：**描述和 Annotation 帮助理解，本地执行前的 Policy Decision 才能授权；信息不足时走高风险路径。**

### 准入之后才能讨论并发

一次模型响应可以包含多个 Tool Call，但“同时提出”不等于“可以同时执行”。并发至少要满足四个条件：

1. 后一个调用不依赖前一个调用的结果；
2. 调用之间不读写冲突的可变资源；
3. 一个调用失败后，其他调用继续执行仍然合理；
4. 外部限流、审批和连接能力允许并发。

只读是一个实用信号，却不是充分证明。两个只读数据库查询也可能争用同一事务，两个访问不同文件的写操作反而可能互不冲突。运行时不知道资源关系时，默认串行更安全。

PI 使用较保守的批次策略：全局要求串行，或批次中任意 Tool 声明 `executionMode="sequential"`，整批就按模型给出的顺序执行；否则先完成每个 Call 的准备和拦截，再并发执行。Claude Code 的粒度更细，`isConcurrencySafe(input)` 把连续的安全调用组成并发 Batch，不安全调用独占一个 Batch。

两种实现都会把并发结果恢复成原 Tool Call 顺序再写入 Transcript。进度可以按完成时间展示，但模型上下文不应因为一次偶然的网络快慢而换序。这里要区分两种顺序：Definition 列表顺序服务请求稳定性，Call 顺序才可能表达本次任务的依赖。

## 4. Tool Outcome 让调用真正闭环

Bash 进程返回 stdout、stderr 和退出码以后，工作还没有结束。Agent Runtime 必须把执行信息变成与 `tool_use_id` 配对的 Tool Result。对 Anthropic Messages API，这个结果位于紧跟 Assistant Tool Use 之后的 User Message 中；其他 Provider 可能使用 `tool` 或 `function` Role，但都要保留 Call 与 Result 的配对关系。

如果某个 Call 没有结果，模型不知道它尚未执行、执行失败还是宿主丢失了消息。失败结果同样重要：它告诉模型应该修改参数、换 Tool、请求权限还是向用户报告。

### 原始返回值要投影给不同消费者

同一份 Outcome 通常有三种消费者：

- **模型**需要足以决定下一步的内容，例如命令是否成功、关键输出和继续读取的方法；
- **UI 或 SDK**需要结构化状态、进度、可展开内容和展示元数据；
- **日志与 Trace**需要耗时、退出状态、调用来源和受控的诊断信息。

它们不应该共用一段无限增长的字符串。模型不需要终端颜色控制符和完整错误栈；日志中也不应该因为模型需要一段摘要就丢失可诊断字段。

PI 的 `AgentToolResult` 用 `content` 保存随后进入模型消息的文本或图片，用 `details` 保存 Renderer 和宿主需要的结构化详情。Claude Code 让 Tool 的 `call()` 先返回内部数据，再由 `mapToolResultToToolResultBlockParam` 生成模型 API 的 `tool_result`，UI 则使用独立 Renderer。MCP 的 `structuredContent` 和 `_meta` 也可以保留给 SDK Consumer，而模型可见内容继续经过宿主映射。

这种分层不是要求复制三份完整结果，而是从一份 Outcome 生成不同投影，并分别设置大小、权限和脱敏规则。

### 可恢复失败应成为 Outcome

Tool 实现内部可以通过异常表达失败，但异常不必直接终止整个 Agent Run。PI 的 Executor 会在单次 Tool 边界捕获异常，把未知 Tool、Schema 错误、Hook 阻止和执行异常转换成带 `isError` 的结果；Claude Code 也会把参数错误、权限拒绝和大多数 Tool 执行异常变成 `is_error=true` 的 `tool_result`。

把可恢复失败交还模型有两个条件：错误必须与原 Call 配对，而且内容要能指导下一步。只返回“发生异常”没有恢复价值；返回“路径不在允许目录中，请选择工作区内路径”才可能让模型修正。

请求无法构造、消息配对已经损坏、进程级故障或用户明确中断时，运行时才应让错误越过 Tool 边界终止当前 Run。自动重试还需要单独检查幂等性和副作用是否可能已经发生：命令响应超时不代表命令没有执行，盲目重放写操作可能造成第二次修改。

OpenAI Agents SDK 采用了相同的可配置边界：Function Tool 默认把崩溃交给 Error Function 生成模型可见错误，显式关闭后才重新抛出；超时也可以选择 Error as Result 或终止 Run。

### 大结果要保留行动能力，而不是只保留字符

如果 `git status`、测试命令或构建日志产生数万行输出，直接回填会占满 Context；简单 `slice(0, N)` 又可能丢掉末尾错误、切坏 JSON，或者让模型误以为看到了完整结果。

治理顺序应该与结果语义一致：

1. **先减少产生量**：让 Tool 支持过滤、字段选择、范围和 Limit。
2. **提供分页或游标**：返回当前范围与下一次调用参数。
3. **生成语义化 Preview**：源码保留目标行附近，搜索保留匹配片段，命令日志通常保留尾部。
4. **把全文放到上下文外**：保存到有生命周期与访问控制的文件或对象，只回填大小、Preview 和引用。
5. **摘要必须可回查**：明确摘要是派生内容，不能替代唯一原文。
6. **限制整批结果**：单个结果都不超限，并不代表十个并发结果合起来安全。

PI 的 Bash 输出累积器保留尾部；一旦超过行数或字节限制，就把完整输出写入临时文件，并在模型内容中给出当前显示范围和路径。Claude Code 为 Tool 设置单结果阈值，超限后保存全文并返回 Preview；Query Loop 还限制一条 Message 中所有新 Tool Result 的总量，必要时优先外置最大的结果。

所以，大结果治理的目标不是“尽量塞进上下文”，而是让模型知道发生了什么、当前内容是否完整，以及下一步怎样取得所需部分。

## 5. MCP 标准化三个对象，不接管 Runtime

前面讨论的是一个 Host 内部怎样完成 Tool Call。MCP 解决的是另一层问题：当 Tool 来自独立进程、另一种语言或第三方服务时，Host 与 Tool Server 怎样用统一协议接线。

MCP 可以直接映射到前面的三个对象：

- `tools/list` 让 Client 发现 Tool Definition 和当前 Catalog；
- `tools/call` 把名称与参数作为 Call 发给 Server；
- Call Tool Result 用 `content`、`structuredContent`、`isError` 和 `_meta` 表达 Outcome。

`notifications/tools/list_changed` 还能通知 Client 目录已经变化。但通知只意味着旧目录需要失效，不会规定 Host 应该在当前 Batch 还是下一 Turn 刷新。

这套标准化减少了重复 Adapter。一个 MCP Server 可以被多个 Host 发现和调用，输入与输出 Schema、错误标记、多模态内容和资源引用也有共同表示。它尤其适合跨进程、跨语言、跨 Host 复用的 Tool；只有少量进程内函数时，本地类型和直接调用通常更轻。

MCP 没有替 Host 决定本地权限、审批体验、沙箱、并发、重试、大结果预算、UI 和 Telemetry。Tool Annotation 也只是 Server 提供的 Hint。Claude Code 的 MCP Adapter 正好展示了这条边界：它把远端 Definition 转换为本地 Tool，把 Annotation 和结构化结果映射到本地字段，但 `canUseTool`、调度、结果持久化和渲染仍由 Claude Code Runtime 完成。

因此，MCP 标准化的是 Definition、Call 和 Outcome 怎样跨边界传输，不是“采用 MCP 后 Tool Runtime 就设计完了”。

沿着一次调用回看全文，成熟 Tool 系统只需守住三件事：模型看到稳定而清楚的调用说明；任何外部动作都必须经过宿主验证和授权；每一个调用最终都有可审计、可继续行动的 Outcome。动态目录、并发、大结果和 MCP，都是在维护这条闭环，而不是彼此独立的功能清单。

# 第二部分：面试时怎么回答

## 6. 一段 3—5 分钟的完整回答

> 我会先纠正一个容易混淆的前提：模型并没有直接调用函数。它看到 Tool Definition 后，只会生成一条带 Name、Arguments 和 Call ID 的结构化调用意图；Agent Runtime 才负责把这个意图变成真实执行，再把 Tool Result 配对回填给下一轮模型。
>
> 所以我会沿这条生命周期设计 Tool。Definition 层给模型的核心是名称、描述和 Input Schema，Provider 支持时再加 Output Schema、Strict 或 Deferred Loading。执行函数、取消、超时、进度、权限和 Renderer 属于宿主，不需要全部暴露给模型。PI 把这些职责拆在 Tool、AgentTool 和扩展定义中；Claude Code 的产品型 Tool 接口则把权限、并发、结果映射和 UI 能力集中在一起。
>
> 模型返回的 Call 要按不可信输入处理。宿主先查找 Tool、做 Schema 和语义校验，再根据具体参数判断只读、破坏性、幂等性和外部访问，最后做 Allow、Ask 或 Deny。Claude Code 的 Bash Tool 会分析本次命令，`git status` 可以是只读调用，写文件或删除命令则走另一条权限路径。PI 提供执行前 Hook，但它本身不等于文件或网络沙箱。
>
> 多个 Call 是否并发也由 Runtime 决定。我会检查数据依赖、资源冲突、失败传播和外部限流；信息不足就串行。PI 的粒度偏批次级，Claude Code 可以按具体 Input 判断安全性。无论怎样执行，写回模型的结果顺序应该保持稳定。
>
> 工具执行结束后，我不会直接把函数返回对象塞进 Context，而会形成 Tool Outcome。模型看可行动的 Content，SDK 或 UI 看结构化详情，日志看 Trace 和诊断字段。参数错误、权限拒绝和可恢复执行失败也形成带 Call ID 的 Error Result，让模型修参、换 Tool 或上报；只有协议损坏、进程故障和 Run 级中断才打断循环。
>
> 大结果先在数据源处过滤或分页，再做语义化 Preview；完整内容放到上下文外并返回引用，同时限制单结果和整批结果。工具目录可以动态变化，但当前 Turn 要使用稳定快照，Definition 的顺序也要确定，避免目录竞态和 Prompt Cache 抖动。
>
> MCP 适合把 Tool 的发现、Schema、调用和结果标准化，尤其是跨进程和跨语言场景。但权限、沙箱、并发、重试、Context Budget 和 UI 仍然由 Host Runtime 负责。我的总原则是：模型负责提出调用，宿主拥有执行权，Outcome 负责关闭循环。

## 7. 九组高频追问

### 1）除了名称、描述、Schema、执行和返回结构，还需要什么？

我的选择是按消费者补职责，不做万能字段表。模型侧通常只需要名称、描述、Input Schema，以及可选的 Output Schema、Strict 和延迟加载信息；宿主侧再处理取消、超时、进度、语义校验、权限、并发、结果映射、大小限制和观测。风险是把宿主字段全部暴露给模型，既浪费 Context，也让“声明”看起来像真正的安全控制。

### 2）描述应该静态还是动态？

我默认让模型可见描述在会话或稳定目录版本内不变。初始化时可以根据 OS、租户权限和已安装能力生成一次；权限弹窗和 UI 文案则可以按本次 Input 动态生成。每轮改模型描述的主要风险是 Prompt Cache 失效、模型行为漂移和会话无法复现。

### 3）怎样声明只读、破坏性和权限确认？

我会把它们设计成对本次 Input 的判断，而不是只给 Tool 类别贴布尔标签。同一个 Bash Tool，`git status` 和删除命令的副作用不同。声明与 MCP Annotation 只辅助分类和展示，真正的 Allow、Ask、Deny 必须在执行前由宿主 Policy 根据最新状态决定。

### 4）Tool Result 怎样进入上下文？

它要带原 Call ID，作为下一轮模型消息的一部分与 Tool Call 配对。模型可见 Content、程序使用的 Structured Data、UI 展示字段和日志 Trace 应从同一 Outcome 分别投影，而不是把一个内部对象原样复制到所有通道。每个通道还要独立控制权限、脱敏和大小。

### 5）结果特别大怎么办？

优先在 Tool 输入端过滤和分页；必须保留大结果时，根据语义返回头部、尾部或匹配附近的 Preview，把全文放到上下文外并给出大小、引用和续读方法。摘要只能作为派生视图，最好能回查原文；还要限制整批结果，不能只限制单个 Tool。

### 6）Tool 失败应该抛异常还是交给模型？

Tool 实现内部可以抛异常，但 Executor 应在单次调用边界捕获可恢复失败，转换成带 `is_error` 和 Call ID 的 Outcome。参数错误、权限拒绝和业务失败通常交给模型修正；协议状态损坏、进程级故障或明确中断才终止 Run。自动重试还必须确认幂等性和副作用是否已经发生。

### 7）工具集能否动态增删？

可以，适合权限过滤、插件接入、MCP 热连接和大目录延迟加载。但当前 Turn 应持有不可变快照，变化从下一 Turn 生效，并记录稳定名称或目录版本。主要风险是模型看到的 Definition 与执行时不一致、名称冲突、缓存抖动和历史无法复现。

### 8）什么时候并发，工具列表顺序有讲究吗？

没有数据依赖、不访问冲突资源、失败互不影响且外部限流允许时才并发；判断不清就串行。Tool Call 顺序可能表达本次任务依赖，并发结果最好按原顺序回填。Tool Definition 列表顺序主要用于确定性序列化和 Prompt Cache，不应该当成业务优先级。

### 9）Tool 协议要不要标准化成 MCP？

Tool 需要跨进程、跨语言或被多个 Host 复用时，我会优先 MCP，因为它统一发现、Definition、Call 和 Outcome；几个进程内函数直接使用本地接口更简单。MCP 的边界是它不替 Host 负责权限、沙箱、调度、重试、Context Budget 和 UI。

这九个问题最终都落在同一条链上：模型怎样提出调用，宿主怎样把提议变成受控动作，结果怎样进入下一轮。先把这条闭环讲清楚，再讨论字段和协议，Tool 设计才不会退化成一份互不相干的功能清单。

## 参考资料

- [PI 源码，commit `46bb9a2c`](https://github.com/earendil-works/pi/tree/46bb9a2c3bdb296b0d2179f7309ec6b79a7f3106)
- Claude Code 公开源码快照：commit `09f43552c76cb8856c4a5414f9aa9c9cda6ee035`；该快照不是 Anthropic 官方发布的完整源码
- [Anthropic：How tool use works](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works)
- [Anthropic：Handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)
- [Anthropic：Tool reference](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-reference)
- [Anthropic：Tool use with prompt caching](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching)
- [Anthropic：Tool search tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)
- [Model Context Protocol 2025-11-25：Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [OpenAI Agents SDK：Tools](https://openai.github.io/openai-agents-python/tools/)
