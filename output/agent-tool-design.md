# 一次 Tool Call 如何闭环：从模型意图到 Runtime 执行

用户要求 Agent 查看仓库状态时，一次 Tool Call 从模型生成调用意图开始。模型先读取 Bash Tool 的调用说明，再生成一段结构化数据，表达“我想以这些参数调用这个工具”。Agent Runtime 随后查找工具、判断权限并启动进程。

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

这段 `tool_use` 表示一条调用意图。宿主执行命令以后，还要保留上面的 Assistant Message，并紧接着追加一条带相同调用 ID 的结果消息：

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

模型在下一轮看到 `tool_result`，才会继续判断应该读取文件、修改代码还是直接回答用户。模型 API 对角色和字段各有命名，客户端 Tool 的执行都要完成相同的闭环：

`Tool Definition → 模型生成 Tool Call → 宿主校验、授权与调度 → 执行 → Tool Outcome → 下一轮模型请求`

本文把宿主在一次调用结束后形成的完整结果统称为 **Tool Outcome**，范围包括成功内容、错误状态、结构化数据和宿主元数据。这个名称用于本文的机制分析，不对应某个协议的固定字段。

接下来主要对照 [PI commit `46bb9a2c`](https://github.com/earendil-works/pi/tree/46bb9a2c3bdb296b0d2179f7309ec6b79a7f3106) 与 Claude Code 公开源码快照。Claude Code 快照标注 commit `09f43552`，来自 2026 年 3 月公开暴露的 Source Map；本文使用其中可以直接核对的客户端实现，讨论范围不包含 Anthropic 未公开的服务端逻辑。

# 第一部分：Tool Call 的执行闭环

## 1. Definition、Call 与 Outcome 的责任边界

前面的三段 JSON 对应三个不同对象：

- **Tool Definition** 告诉模型有哪些能力，以及应该生成什么形状的参数。
- **Tool Call** 是模型根据当前对话生成的调用意图，包含调用 ID、工具名和参数。
- **Tool Outcome** 是宿主处理这次意图后的完整结果，范围大于工具函数的原始返回值。

三者之间隔着两个责任边界。模型根据 Definition 生成 Call；宿主把 Call 转换为真实执行，再把执行信息规范化为 Outcome。Bash 进程以及 JavaScript 或 TypeScript 的执行函数都由宿主管理，模型接触的是调用说明和执行结果。

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

图中的三段职责共同构成 Tool 设计。名称和 Schema 影响模型能否提出正确请求；权限与调度决定请求能否变成外部动作；结果回填决定模型能否在下一轮使用刚获得的事实。任何一段缺失，调用都没有闭环。

本文重点讨论由 Agent 宿主执行的客户端 Tool。Web Search 等服务端 Tool 的执行器可能位于模型服务商一侧，“模型提出调用、某个运行时执行、结果返回模型”的责任分离依然存在。

## 2. Tool Definition 描述模型可见的调用接口

Tool Definition 的直接消费者是模型。对自定义客户端 Tool，基本字段有：

- `name`：稳定、可唯一定位的调用名；
- `description`：什么时候使用以及能力边界；
- `input_schema`：允许哪些参数、类型和必填关系。

Provider 还可能支持 Output Schema、Strict Mode、延迟加载或协议 Annotation。这些字段继续描述模型如何选择工具、构造参数和理解结果，执行代码由宿主持有。

宿主需要的信息更多。运行时要知道怎样执行和取消、怎样报告进度、多久算超时；策略层要判断参数语义、权限、副作用和并发安全；结果层还要规定怎样生成模型消息、怎样渲染 UI、保存哪些日志以及允许占用多少上下文。这些字段留在宿主侧，可以避免额外占用模型 Context；其中的权限声明也需要宿主执行才能形成安全边界。

PI 对这些消费者进行了分层。底层 `Tool` 主要保存名称、描述和参数 Schema；`AgentTool` 再增加参数准备、执行函数和执行模式；Coding Agent 的扩展定义继续增加提示片段与 UI Renderer。Claude Code 的产品运行时更集中，`Tool` 类型同时暴露 `call`、`isReadOnly(input)`、`isConcurrencySafe(input)`、`checkPermissions`、结果映射和 Renderer。两种组织方式都按照消费者职责分配字段，发给模型的 Definition 无须承载 Tool 系统的全部能力。

### 先区分模型接口与调用说明

讨论“Tool 描述应该静态还是动态”以前，先要确认这段文字给谁看。同一个 Bash Tool 至少涉及两类说明：模型需要一段稳定的能力描述，例如“执行 Shell 命令并返回结果”；用户面对具体调用时，则需要知道这次操作会做什么。`git status --short` 可以显示成“读取当前仓库状态”，`rm -rf build` 则应该明确显示成“删除 build 目录”。后两句话必须读取本次 Input，无法由一段通用描述代替。

这两类内容承担不同职责：

- **模型接口描述**进入 Tool Definition，帮助模型判断什么时候选择 Bash、应该生成什么参数；
- **调用说明**进入权限确认和 UI，帮助用户判断这一次具体操作是否符合预期。

PI 的底层 `Tool.description` 是静态的模型接口描述。Claude Code 把两类内容显式分开：`prompt(options)` 生成 API Definition 中的模型描述，`description(input, options)` 根据具体参数和权限上下文生成用户可见说明。`toolToAPISchema` 首次调用 `prompt()` 后，会把名称、描述和 Input Schema 等基础字段放入 Session 级 Schema Cache，后续请求直接复用。

动态能力仍然有必要，只是两类内容的更新时间不同。模型接口可以在会话建立或可用 Tool 列表更新时，根据 Shell 类型、租户能力和已经安装的 Tool 生成一次；调用说明则要在每次执行前，根据 Input 和当前权限状态重新生成。实时业务数据不适合写进模型接口，可以通过普通上下文或专门的查询 Tool 提供。

模型接口需要按 Definition 版本保持稳定，是因为它位于模型请求的前缀。Anthropic 的请求会先序列化 Tools，再放入 System Prompt 和 Messages；Prompt Cache 复用的是这段字节前缀。如果同一个 Tool 每轮重新生成不同描述，整个后续前缀都会失去缓存，同一段 Transcript 在重放时也会面对不同的接口语义。

因此，“支持动态描述”不是要求所有说明一起变化：模型接口可以在版本建立时生成一次，随后保持稳定；用户调用说明按 Input 动态生成。单个 Tool 的描述保持稳定，不表示所有 Tool 都必须从第一轮开始加载。

### Tool 多时，先搜索，再加载完整 Schema

Tool 很少时，Runtime 可以把全部 Definition 发给模型。Tool 很多时，每个 Tool 的描述和 Input Schema 会持续占用 Context，大量无关定义也会干扰模型选择。此时 Runtime 可以在初始请求中只发送常用 Tool 的完整 Definition，再提供一个 `ToolSearch`；延迟加载的 Tool 先只出现名称，完整 Schema 暂不进入模型上下文。

Claude Code 公开快照的 `src/tools/ToolSearchTool/ToolSearchTool.ts` 中，`ToolSearchTool` 做的是本地词法搜索。模型负责把用户意图写成搜索词，Runtime 只执行一套确定性的匹配和排序算法。以 `ToolSearch(query: "database query")` 搜索 `query_database` 为例：

1. 查询先转成小写，再按空格拆成 `database` 和 `query`。Tool 名称按 CamelCase 和下划线拆分，所以 `QueryDatabase` 与 `query_database` 都会得到 `query`、`database` 两段。
2. Runtime 只给尚未加载的候选 Tool 计分。每个查询词与名称分段完全相等加 10 分，被某个名称分段包含加 5 分；命中 `searchHint` 加 4 分；名称的规范化完整形式命中、且候选此前尚未得分时兜底加 3 分；Description 按单词边界命中再加 2 分。
3. `query_database` 的两个名称分段分别精确命中，因此仅名称就得到 20 分。如果它的 `searchHint` 是 `database query`，Description 是 `Run a database query and return rows`，两个词还会分别得到 4 分和 2 分，总分为 32。
4. Runtime 过滤零分候选，按总分倒序排列，默认返回前 5 个。查询中的 `+database` 表示必选词：候选的名称、Description 或 `searchHint` 必须命中所有带 `+` 的词，才会进入计分阶段。

还有一条不经过排序的路径：`select:query_database` 直接按完整名称选择 Tool。关键词搜索也会优先处理与完整 Tool 名称完全相等的查询。

搜索结果只返回 `tool_reference`，不是把执行器交给模型。`src/utils/toolSearch.ts` 中的 `extractDiscoveredToolNames()` 从消息历史识别这些引用，后续请求才加入命中 Tool 的完整 Schema；模型拿到 Schema 后，才能生成 `query_database(sql, database)` Call。

这套搜索没有使用 Embedding、向量数据库或另一个 LLM，也没有内置同义词扩展、翻译和拼写纠错。模型可能根据中文用户请求主动生成英文查询 `database query`；如果它直接提交 `查询数据库`，而 Tool 的名称、Description 和 `searchHint` 只有英文，Runtime 不会自动把中文语义匹配到 `query_database`。因此，Tool 的命名和搜索元数据会直接影响召回率。

PI 没有内置通用 Tool Search，只提供动态激活 Tool 的扩展能力。它的回归测试中，普通 Extension Tool `load_more_tools` 执行时调用 `pi.setActiveTools()`，把预先注册的 `after_load` 加入 Active Tools；PI 记录这个变化，并在后续模型请求中提供 `after_load` 的 Schema。

加载哪个 Tool、怎样找到它，都由 Extension 自己决定。测试只是写死了 `after_load`；实际项目可以在这个普通 Tool 中实现关键词、规则或语义检索。这里所说的 Loader Tool 只是一种扩展用法，不是 PI 的内置类型。相比之下，Claude Code 把通用加权关键词搜索直接做进了 Runtime。两种方式都能把完整 Schema 推迟到真正需要时再加载，但 Tool 数量少时，直接预加载全部 Definition 更简单。

Definition 帮助模型产生结构化 Call。Call 进入宿主后，还要经过语义判断和权限裁决才能执行。

## 3. 宿主把调用意图变成受控执行

### 一次真实调用怎样进入执行

模型生成的 Call 和用户提交的表单一样，只是一份待处理的外部输入。设想模型要修改 `config.yaml`，第一步先生成：

`Read(path: "config.yaml")`

Runtime 不会拿到这段数据就直接读盘。它先在本轮可用 Tool 中查找 `Read`；确认 `path` 是字符串，而且没有缺少必填参数；再检查路径是否有效、文件是否存在、当前工作区状态是否允许读取；随后依据用户权限和沙箱边界决定放行、询问还是拒绝。如果同一时刻还有会改动这个文件的调用，读取还可能需要等待。只有这些检查全部通过，Runtime 才会启动真正的文件操作。

因此，一次调用进入执行的实际顺序是：

`找到 Tool → 检查参数形状 → 检查当前状态 → 判断权限 → 安排执行顺序 → 启动 Tool`

每一步拦住的是不同问题。Tool 不存在，Runtime 就无法解释这次调用；`path` 被模型生成为数组，参数便不能安全交给执行器；文件已经删除或自上次读取后发生变化，说明参数形式正确，当前状态却不再满足执行条件；操作本身有效但用户没有权限，应当在产生副作用前停止；调用与另一个写操作命中同一资源，则不是永远不能执行，而是必须等待正确时机。

PI 和 Claude Code 都把参数校验与宿主拦截放在真正执行之前。二者的具体接口不同，共同点是执行函数没有最终决定权：Runtime 可以因为输入、状态、权限或调度关系不满足而不调用它。

### Tool 特性分别解决什么执行问题

Runtime 要作出上述判断，需要的不只是 Tool 的名称。生产级 Tool 往往还要描述这次调用的执行特性：

| Tool 特性 | Runtime 用它判断什么 |
|---|---|
| 只读性 | 本次调用是否会改变可观察状态，能否采用较低的权限与并发门槛 |
| 破坏性 | 修改是否难以恢复，是否需要更强确认或恢复措施 |
| 开放世界访问 | 是否访问外部网络或系统，是否可能把数据发送到受控环境之外 |
| 并发安全 | 与其他调用同时运行时，结果是否仍然正确 |
| 可中断性 | 用户插入新指令时，操作能否立即停止，还是必须越过关键区后再结束 |
| 资源位置 | 本次调用具体操作哪个文件、进程、事务或远端对象 |

这些特性通常属于**本次 Call**，不能只按 Tool 名称写死。`git status --short` 读取本地仓库；重定向到 `config.yaml` 会修改状态；删除目录不但写入，而且难以恢复；`curl` 即使只发起读取，也越过了本地边界，并可能把 URL、Header 或请求体发送给外部系统。同一个 Bash Tool 会随着 `command` 改变风险画像。

只读、无破坏性和可并发也不是同一个结论。两个只读查询可能争用同一事务或触发外部限流；两个写操作如果修改不同文件，反而可能安全并行；一次网络读取没有修改本地文件，却仍可能泄露数据。Tool 的声明只是给 Runtime 提供事实，真正的准入、确认和调度必须由 Runtime 结合 Input、当前状态与本地 Policy 执行。无法判断时，应采用更保守的权限和调度方式。

Claude Code 会根据本次 Input 计算只读性、外部访问和并发安全等特征，再交给权限与调度流程。PI 提供执行前拦截点，由宿主应用补上权限规则和隔离环境。这里值得借鉴的不是某个字段名，而是把“Tool 声称自己能做什么”和“这次调用是否允许做”分成两步。

### Read、Edit 与 Write 为什么存在关联

单次调用通过校验，不代表多个调用组合起来仍然安全。继续看 `config.yaml`：模型先读取文件，根据其中的内容生成修改方案，然后调用 `Edit`。这时，Edit 的正确性依赖模型刚才看到的版本。如果用户、格式化器或另一个进程已经改过文件，参数仍然符合 Schema，修改却可能落在过期内容上。

Claude Code 的 Read 会记录模型实际看到的文件内容和版本状态。Edit 或覆盖已有文件的 Write 执行前，Runtime 会确认模型此前完整读过这个文件；如果文件在读取后发生变化，写入会被拒绝，模型必须重新读取后再生成修改。真正写盘前还会再次核对文件状态，以缩小“检查时没变，落盘时已经变了”的时间窗口。

这样，`Read → Edit/Write` 就不再是一句提示词建议，而是 Runtime 维护的状态关系。它防止的不是非法参数，而是**基于旧事实作出合法但错误的修改**。

PI 采用了另一种更局部的保护。Edit 真正执行时会读取当前文件，确认模型要求替换的旧文本仍然存在；不存在就拒绝，而不是猜测应该改哪里。Edit 和 Write 修改同一个文件时还会进入同一条队列：前一个操作结束后，后一个才能开始。修改不同文件的调用仍可并行；两个不同路径如果最终指向同一文件，也会按同一资源处理。

两种设计处理的是相邻但不同的风险。Claude Code 的读后写检查确保模型基于自己见过的当前版本修改；PI 的旧文本检查与同文件队列确保修改目标仍然成立，并避免多个写操作在同一资源上交错覆盖。真实运行时往往既需要识别调用属性，也需要维护 Tool 之间的资源关系。

### 并发控制需要同时看属性和关系

一次模型响应可能包含多个 Call。下面三个场景分别对应没有冲突、争用同一资源和存在结果依赖。

用户要求比较两份配置时，模型可以同时生成 `Read(config.yaml)` 与 `Read(schema.yaml)`。两个调用不修改文件，也不需要对方的结果来构造自己的参数，Runtime 可以同时启动它们。哪个文件先读完不影响另一个结果。

文件修改的判断更细。`Edit(config.yaml)` 与 `Write(config.yaml)` 命中同一资源，必须排队，否则后完成的写入可能覆盖先完成的结果。`Edit(config.yaml)` 与 `Write(schema.yaml)` 操作不同文件；能够按文件隔离资源的 Runtime 可以让它们并行，只有 Tool 级“写操作一律独占”的 Runtime 则会选择串行。后者牺牲一部分吞吐量，换来更简单的安全边界。

修改配置后运行测试属于结果依赖。测试只有在新内容落盘后才有意义。最清楚的表达方式是分成两轮：模型先调用 `Edit(config.yaml)`，收到成功 Outcome 后，再生成测试命令。如果两个调用已经出现在同一响应中，Runtime 只有在采用串行策略并保留 Call 顺序时才能保证测试后执行；通用并行执行器无法仅凭 Tool 名称推断这条业务关系。

因此，调度可以按下面的顺序判断：

`先看结果依赖 → 再看资源冲突 → 最后检查 Tool 的并发声明和外部限制`

需要前一个结果才能生成参数的调用必须分轮；已经具有完整参数的兄弟调用，再检查它们是否争用文件、进程或事务；这些关系都不构成阻塞时，Tool 的并发声明、外部限流和连接数才决定能否真正同时启动。

Claude Code 收到一个完整 Call 后，先按 Schema 解析 Input，再把解析后的参数交给这个 Tool 自己判断并发安全性。Read 明确返回安全；Edit 和 Write 没有声明并发安全，按默认值视为不安全；Bash 则检查本次 `command`，只有整条命令都能被识别为只读操作时才返回安全。`git status --short` 可以通过，写重定向、无法解析的复杂命令以及 `npm test` 这类不能确认只读的命令都会落到不安全一侧。Schema 校验失败或判断过程报错时，Runtime 同样按不安全处理。

这里的“安全”只回答能否与其他 Call 同时执行，不替代权限判断。Read 仍然可能因为路径权限被拒绝；Edit 被判为不可并发，也不表示每次都需要用户确认，只表示它运行时要独占执行。

在等待完整响应后再执行的路径里，Claude Code 按 Call 顺序切分批次。例如模型依次生成：

`Read(config.yaml) → Read(schema.yaml) → Edit(config.yaml) → Read(other.yaml)`

前两个 Read 连续且都安全，会一起启动；Edit 单独形成一个批次，等两个 Read 结束后执行；最后一个 Read 虽然安全，也不能越过前面的 Edit，只能在 Edit 完成后开始。修改不同文件的两个 Edit 也都会被归入独占调用，因此不会获得额外并发。这套规则不需要计算两个 Input 是否指向同一文件，代价是放弃了一部分原本可能安全的并行。

PI 不对每个 Input 做同样的读写分类。它拿到完整 Assistant Message 后，先检查 Agent 的批次设置；默认值是并行，也可以由宿主改成串行。随后检查本批每个 Call 对应的 Tool：只要其中一个 Tool 声明必须串行，整个批次就按 Call 顺序执行；全局允许并行且没有 Tool 要求串行时，Runtime 先逐个完成参数准备和执行前拦截，再同时启动通过检查的调用。

文件 Tool 在这套批次判断下面还有一层资源协调。默认并行批次中如果同时出现 `Edit(config.yaml)`、`Write(config.yaml)` 和 `Edit(schema.yaml)`，前两个调用会争用同一文件队列，只能依次写入；第三个调用使用另一条队列，可以同时修改 `schema.yaml`。因此，Claude Code 主要用本次 Input 的属性决定独占还是并发，PI 先用全局设置和 Tool 声明决定整批模式，再由具体 Tool 处理同一资源的冲突。

#### 流式到达时怎样处理尚未出现的 Call

流式提前执行不表示 Runtime 会运行一段尚未生成完整的参数。模型先分片输出 Tool 名称和参数 JSON；Runtime 等到**一个 Tool Call**结束，拿到完整 Input 并通过解析后，才可能启动它。此时整条 Assistant Response 可以仍在生成，后面还可能出现其他 Call。

Claude Code 的流式执行过程可以按到达顺序观察：

1. `Read(config.yaml)` 完整到达。Runtime 将它判为并发安全并立即启动。
2. `Read(schema.yaml)` 随后完整到达。当前正在运行的也是安全调用，因此第二个 Read 同时启动。
3. `Edit(config.yaml)` 最后到达。它不属于并发安全调用，必须等两个 Read 都结束后才能执行。

反过来也一样：如果先到达并启动的是 Edit，后到达的 Read 也要等待，因为非安全调用在执行期间独占运行。Runtime 不需要预先知道后面会出现什么；每个新 Call 到达时，只要拿它的属性与当前正在执行的调用比较，就能决定立即启动还是排队。

这条规则依赖一个严格前提：只有能够和任意其他安全调用重叠的操作，才能被标记为并发安全。如果两个调用都被标成安全，实际却会争用同一事务或突破同一个外部限流，调度器在冲突发生后无法补救。更细的实现可以在调用到达时为文件、进程或事务申请资源锁；后续 Call 命中已占用资源就等待，操作其他资源仍可执行。资源锁只能发现共享对象，不能推断“测试必须看见刚才的配置修改”这类业务语义；这类依赖仍需分轮表达，或等完整响应结束后统一调度。

这也解释了 PI 与 Claude Code 的取舍。Claude Code 在单个 Call 完整后就可以提前执行，以安全调用并发、非安全调用独占来处理尚未到达的兄弟调用。PI 的核心循环先取得完整 Assistant Message，再从中提取全部 Call；它没有同样的“未知后续 Call”窗口，但仍要在批次执行时通过串行模式和同文件队列控制冲突。

提前执行还会把流式故障带入执行边界。如果响应随后中断，已经完成的外部动作不会因为丢弃这次响应而撤销；Runtime 不应透明地重放同一个 Call，除非操作本身幂等，或执行端能够根据稳定的幂等键去重。失败怎样形成 Outcome 和决定重试，由下一章继续讨论。

无论调用何时启动，Runtime 都不能用数组位置猜测结果属于哪个 Call，每个 Result 都要保留对应的 Call ID。PI 会在并发批次结束后按原 Call 顺序形成 Result；Claude Code 的流式安全调用可以按完成时间交还结果，非安全调用则继续充当顺序屏障。两种做法都依靠 ID 保持 Call 与 Result 的配对，是否保留原顺序属于调度与 Transcript 设计的另一项选择。

### 执行开始后还要处理取消和失败扩散

调用启动以后，Runtime 仍然要决定哪些操作可以停、哪些必须等。用户发来新指令时，如果一个读取或搜索能够安全终止，Runtime 可以立即取消；文件已经进入关键写入阶段时，强行终止可能只写入一半，Runtime 更适合等待原子操作完成，再响应新指令。可中断性描述的正是这条执行边界，而不只是“是否支持 AbortSignal”。

并发批次中的失败也不一定只影响自己。几条 Bash 命令可能组成一条隐含操作链，前一条失败后，继续运行兄弟调用可能扩大错误；几次独立文件读取则互不依赖，一次失败没有必要取消其余调用。Claude Code 区分了这两类行为：Bash 调用报错时会取消同批仍在运行的兄弟调用，普通读取失败不会触发相同的失败扩散。

PI 的文件修改队列还处理了一个不易察觉的取消竞态。一次写操作收到取消信号后，队列不会立刻放行针对同一文件的下一次修改，而会等底层文件操作真正结束。否则，已经宣布“取消”的旧写入可能晚于新写入落盘，反过来覆盖新结果。

这些机制决定一次调用被拒绝、等待、取消还是继续执行。执行最终产生的成功内容和错误怎样回填模型，由下一章的 Tool Outcome 继续说明。

## 4. Tool Outcome 将执行结果带回下一轮

Bash 进程返回 stdout、stderr 和退出码以后，Agent Runtime 还要把执行信息变成与 `tool_use_id` 配对的 Tool Result。对 Anthropic Messages API，这个结果位于紧跟 Assistant Tool Use 之后的 User Message 中；其他 Provider 可能使用 `tool` 或 `function` Role，同样需要保留 Call 与 Result 的配对关系。

如果某个 Call 没有结果，模型不知道它尚未执行、执行失败还是宿主丢失了消息。失败结果同样重要：它告诉模型应该修改参数、换 Tool、请求权限还是向用户报告。

### 同一 Outcome 面向三类消费者

同一份 Outcome 通常有三种消费者：

- **模型**需要足以决定下一步的内容，例如命令是否成功、关键输出和继续读取的方法；
- **UI 或 SDK**需要结构化状态、进度、可展开内容和展示元数据；
- **日志与 Trace**需要耗时、退出状态、调用来源和受控的诊断信息。

三个消费者需要独立的结果视图。模型视图可以省略终端颜色控制符和完整错误栈，日志则保留可诊断字段，不受模型摘要长度影响。

PI 的 `AgentToolResult` 用 `content` 保存随后进入模型消息的文本或图片，用 `details` 保存 Renderer 和宿主需要的结构化详情。Claude Code 让 Tool 的 `call()` 先返回内部数据，再由 `mapToolResultToToolResultBlockParam` 生成模型 API 的 `tool_result`，UI 则使用独立 Renderer。MCP 的 `structuredContent` 和 `_meta` 也可以保留给 SDK Consumer，而模型可见内容继续经过宿主映射。

运行时从同一份 Outcome 生成不同投影，并为每个投影分别设置大小、权限和脱敏规则。

### 可恢复失败应成为 Outcome

Tool 实现内部可以通过异常表达失败，Executor 则在单次 Tool 边界决定 Agent Run 是否继续。PI 的 Executor 会捕获异常，把未知 Tool、Schema 错误、Hook 阻止和执行异常转换成带 `isError` 的结果；Claude Code 也会把参数错误、权限拒绝和大多数 Tool 执行异常变成 `is_error=true` 的 `tool_result`。

把可恢复失败交还模型有两个条件：错误必须与原 Call 配对，而且内容要能指导下一步。“发生异常”缺少可用于恢复的信息；“路径不在允许目录中，请选择工作区内路径”则能帮助模型修正参数。

请求无法构造、消息配对已经损坏、进程级故障或用户明确中断时，运行时才应让错误越过 Tool 边界终止当前 Run。自动重试还需要单独检查幂等性和副作用是否可能已经发生：命令响应超时只能说明响应没有及时返回，操作可能已经执行，盲目重放写操作会带来第二次修改的风险。

OpenAI Agents SDK 采用了相同的可配置边界：Function Tool 默认把崩溃交给 Error Function 生成模型可见错误，显式关闭后才重新抛出；超时也可以选择 Error as Result 或终止 Run。

### 大结果需要预览与续读路径

如果 `git status`、测试命令或构建日志产生数万行输出，直接回填会占满 Context；简单 `slice(0, N)` 又可能丢掉末尾错误、切坏 JSON，或者让模型误以为看到了完整结果。

治理顺序应该与结果语义一致：

1. **先减少产生量**：让 Tool 支持过滤、字段选择、范围和 Limit。
2. **提供分页或游标**：返回当前范围与下一次调用参数。
3. **生成语义化 Preview**：源码保留目标行附近，搜索保留匹配片段，命令日志通常保留尾部。
4. **把全文放到上下文外**：保存到有生命周期与访问控制的文件或对象，只回填大小、Preview 和引用。
5. **摘要必须可回查**：明确摘要是派生内容，不能替代唯一原文。
6. **限制整批结果**：单个结果满足限制后，还要检查多个并发结果的总量。

PI 的 Bash 输出累积器保留尾部；一旦超过行数或字节限制，就把完整输出写入临时文件，并在模型内容中给出当前显示范围和路径。Claude Code 为 Tool 设置单结果阈值，超限后保存全文并返回 Preview；Query Loop 还限制一条 Message 中所有新 Tool Result 的总量，必要时优先外置最大的结果。

大结果治理需要同时保留三类信息：发生了什么、当前内容是否完整，以及下一步怎样取得所需部分。Context Budget 决定本轮放入多少结果内容，引用和续读方法则保留后续行动能力。

## 5. MCP 负责跨边界传输

前面讨论的是一个 Host 内部怎样完成 Tool Call。MCP 解决的是另一层问题：当 Tool 来自独立进程、另一种语言或第三方服务时，Host 与 Tool Server 怎样用统一协议接线。

MCP 可以直接映射到前面的三个对象：

- `tools/list` 让 Client 发现当前可用的 Tool Definition 列表，这份可用能力列表也常被称为 Tool Catalog；
- `tools/call` 把名称与参数作为 Call 发给 Server；
- Call Tool Result 用 `content`、`structuredContent`、`isError` 和 `_meta` 表达 Outcome。

这套标准化减少了重复 Adapter。一个 MCP Server 可以被多个 Host 发现和调用，输入与输出 Schema、错误标记、多模态内容和资源引用也有共同表示。跨进程、跨语言、跨 Host 复用的 Tool 很适合采用 MCP；少量进程内函数通常使用本地类型和直接调用更轻。

MCP 覆盖 Host 与 Tool Server 之间的协议交互，本地权限、审批体验、沙箱、并发、重试、大结果预算、UI 和 Telemetry 归 Host 负责。Tool Annotation 是 Server 提供的 Hint。Claude Code 的 MCP Adapter 正好展示了这条边界：它把远端 Definition 转换为本地 Tool，把 Annotation 和结构化结果映射到本地字段，`canUseTool`、调度、结果持久化和渲染继续由 Claude Code Runtime 完成。

MCP 为 Definition、Call 和 Outcome 建立跨边界的传输协议，Host 需要继续完成本地 Tool Runtime 的设计。

沿着一次调用回看，Tool 系统的各项设计都在维护三段关系：模型通过稳定清楚的 Definition 提出请求；宿主验证并授权所有外部动作；Outcome 记录结果并为下一步保留可行动信息。Tool Search 决定哪些按需 Definition 进入模型请求，并发影响 Call 的调度，大结果治理约束 Outcome 的投影，MCP 则让三者跨越进程和语言边界。

# 第二部分：面试时怎么回答

## 6. 一段 3—5 分钟的完整回答

> 我会从 Tool Call 的生命周期开始拆解。模型读取 Tool Definition 后，生成一条带 Name、Arguments 和 Call ID 的结构化调用意图；Agent Runtime 接收这条意图，完成真实执行，再把 Tool Result 配对回填给下一轮模型。
>
> Tool Definition 面向模型，通常包含名称、描述和 Input Schema，Provider 支持时再加 Output Schema、Strict 或 Deferred Loading。执行函数、取消、超时、进度、权限和 Renderer 留在宿主侧。PI 把这些职责拆在 Tool、AgentTool 和扩展定义中；Claude Code 的产品型 Tool 接口把权限、并发、结果映射和 UI 能力集中在一起。
>
> 执行阶段把模型返回的 Call 当作不可信输入。参数形状正确，只能说明它可以被解析；宿主还要检查当前文件或进程状态，再根据本次 Input 判断只读性、破坏性、开放世界访问和可中断性，最后做 Allow、Ask 或 Deny。这里先看单次调用的属性，再看它与其他调用的关系，不能把 Tool 自己的声明当成授权结果。
>
> 同一响应中的多个 Call 由 Runtime 安排调度：先看结果依赖，再看资源冲突，最后检查 Tool 的并发声明和外部限制。Claude Code 会把 Read 判为可并发、Edit 和 Write 判为独占，Bash 只有在整条命令被确认只读时才能并发；PI 默认允许整批并行，但全局串行设置或批次中任意一个串行 Tool 都会让整批按顺序执行，同文件写入还要在下层排队。执行和完成顺序可以变化，每个 Result 必须用 Call ID 维持正确配对。
>
> 流式执行等待的是单个 Tool Call 的完整参数，不必等待整条 Assistant Response 结束。Claude Code 不预测尚未出现的调用：安全调用之间可以重叠，任何非安全调用都独占执行；需要更细粒度时，执行层还可以按文件或事务加锁，让后到的冲突调用等待。
>
> 工具执行结束后，Runtime 把函数返回值整理为 Tool Outcome。模型获得可行动的 Content，SDK 或 UI 使用结构化详情，日志保存 Trace 和诊断字段。参数错误、权限拒绝和可恢复执行失败也会形成带 Call ID 的 Error Result，让模型修参、换 Tool 或上报；协议损坏、进程故障和 Run 级中断则会打断循环。
>
> 大结果先在数据源处过滤或分页，再做语义化 Preview；完整内容放到上下文外并返回引用，同时限制单结果和整批结果。Tool 少时可以预加载全部 Definition；Tool 多时只发送常用 Tool 和 Tool Search，命中后再加载完整 Schema。Claude Code 的搜索是名称、Description 和 `searchHint` 的加权关键词匹配，不是向量语义搜索；这种延迟加载能节省 Context，但会增加一次往返，并依赖搜索元数据和 Provider 能力。Definition 列表采用确定性顺序，以便复现并复用 Prompt Cache。
>
> 跨进程和跨语言复用 Tool 时，MCP 可以统一发现、Schema、调用和结果的传输。权限、沙箱、并发、重试、Context Budget 和 UI 归 Host Runtime 负责。责任边界由此明确下来：模型提出调用，宿主拥有执行权，Outcome 关闭循环。

## 7. 九组高频追问

### 1）除了名称、描述、Schema、执行和返回结构，还需要什么？

我会按消费者分配字段。模型侧通常需要名称、描述、Input Schema，以及可选的 Output Schema、Strict 和延迟加载信息；宿主侧处理取消、超时、进度、语义校验、权限、并发、结果映射、大小限制和观测。把宿主字段全部暴露给模型会浪费 Context，也容易让字段声明被误解为已经生效的安全控制。

### 2）描述应该静态还是动态？

先看描述给谁使用。给模型看的接口描述可以在会话建立或可用 Tool 列表更新时，根据 OS、租户权限和已安装能力生成一次，随后按 Definition 版本保持稳定；给用户看的权限和 UI 说明则应根据本次 Input 动态生成。每轮改写模型接口会破坏 Prompt Cache，也会让同一段历史面对不同的 Tool 语义。

### 3）怎样声明只读、破坏性和权限确认？

我会先把三个概念分开：只读性判断是否改变状态，破坏性判断改变是否难以恢复，开放世界访问判断是否会连接外部系统或向外发送数据。它们都要结合本次 Input 计算，例如 `git status`、删除目录和 `curl` 即使都由 Bash 执行，风险也完全不同。这些声明为权限系统提供事实，执行前仍由宿主结合用户授权、环境状态和沙箱边界作出 Allow、Ask 或 Deny。

### 4）Tool Result 怎样进入上下文？

Tool Result 要带原 Call ID，作为下一轮模型消息的一部分与 Tool Call 配对。模型可见 Content、程序使用的 Structured Data、UI 展示字段和日志 Trace 从同一 Outcome 分别投影，每个通道独立控制权限、脱敏和大小。

### 5）结果特别大怎么办？

先在 Tool 输入端过滤和分页；必须保留大结果时，根据语义返回头部、尾部或匹配附近的 Preview，把全文放到上下文外并给出大小、引用和续读方法。摘要作为派生视图，需要保留回查原文的能力；单个 Tool 和整批结果都要设置大小限制。

### 6）Tool 失败应该抛异常还是交给模型？

Tool 实现内部可以抛异常，Executor 在单次调用边界捕获可恢复失败，并转换成带 `is_error` 和 Call ID 的 Outcome。参数错误、权限拒绝和业务失败通常交给模型修正；协议状态损坏、进程级故障或明确中断才终止 Run。自动重试还必须确认幂等性和副作用是否已经发生。

### 7）工具集能否动态增删？

可以。Tool 少时我会预加载全部 Definition；Tool 多时再按需加载 Schema。Claude Code 内置了加权关键词搜索；PI 提供的是 Extension Tool 激活其他已注册 Tool 的能力，具体搜索策略由 Extension 自己实现。按需加载能节省 Context、减少无关 Tool 干扰，代价是多一次往返和搜索遗漏；Provider 不支持时就退回完整预加载。

### 8）什么时候并发，工具列表顺序有讲究吗？

我会按“结果依赖、资源冲突、并发声明”的顺序判断。Claude Code 在 Input 校验后询问具体 Tool：Read 可以并发，Edit 和 Write 独占，Bash 只有整条命令被确认只读时才能并发，判断失败就按独占处理。PI 默认让批次并行；全局配置为串行，或本批任意 Tool 声明串行，整批就按 Call 顺序执行，同文件写入还会在 Tool 内部排队。流式执行不需要预知后续 Tool，完整 Call 到达后按同一规则启动或等待。Result 必须用 Call ID 配对；Definition 列表采用确定性顺序是为了复现和 Prompt Cache，不表示业务优先级。

### 9）Tool 协议要不要标准化成 MCP？

Tool 需要跨进程、跨语言或被多个 Host 复用时，我会优先 MCP，因为它统一发现、Definition、Call 和 Outcome；几个进程内函数直接使用本地接口更简单。MCP 负责协议传输，Host 继续负责权限、沙箱、调度、重试、Context Budget 和 UI。

这九个问题最终都落在同一条链上：模型怎样提出调用，宿主怎样把提议变成受控动作，结果怎样进入下一轮。这条闭环确定了字段与协议各自服务的位置，也把分散的功能放回同一套 Tool 设计中。

## 参考资料

- [PI 源码，commit `46bb9a2c`](https://github.com/earendil-works/pi/tree/46bb9a2c3bdb296b0d2179f7309ec6b79a7f3106)
- Claude Code 公开源码快照：commit `09f43552c76cb8856c4a5414f9aa9c9cda6ee035`；该快照来自公开暴露的 Source Map，Anthropic 未将其作为完整源码正式发布
- [Anthropic：How tool use works](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works)
- [Anthropic：Handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)
- [Anthropic：Tool reference](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-reference)
- [Anthropic：Tool use with prompt caching](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching)
- [Anthropic：Tool search tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)
- [Model Context Protocol 2025-11-25：Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [OpenAI Agents SDK：Tools](https://openai.github.io/openai-agents-python/tools/)
