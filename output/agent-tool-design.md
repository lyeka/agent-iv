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

模型生成的 `tool_use` 和用户提交的表单一样，都是需要验证的不可信输入。生产级运行时会把它依次送过多道 Gate：

`查找 Tool → Schema 校验 → 语义校验 → 判断副作用 → Allow / Ask / Deny → 调度 → 执行`

查找与 Schema 校验可以确认“存在名为 Bash 的 Tool，command 是字符串”。越权和副作用还需要结合解析后的具体参数做语义判断。

### 副作用是本次调用的属性

同一个 Bash Tool 可以承载完全不同的动作：

- `git status --short` 读取仓库状态，不修改工作区；
- `printf ... > config.yaml` 会覆盖文件；
- `rm -rf build` 会删除目录；
- `curl https://example.com` 可以只读取远端资源，同时也会访问开放网络。

只读、破坏性、幂等性和 Open World 访问是四个独立维度。一个写操作可以同时具备幂等性和副作用；一个只读操作也可能把敏感查询发到外部系统。权限系统需要根据本次参数、用户规则和执行环境作出 `allow`、`ask` 或 `deny`，Tool 名称只能提供初步线索。

Claude Code 按输入实现这些判断。Bash Tool 的 `isReadOnly(input)` 分析具体命令，`isConcurrencySafe(input)` 只有在本次调用被判为只读时才返回真；执行器在调用 `call` 前还会完成 Schema 校验、Tool 语义校验、PreToolUse Hook 和权限裁决。远端 MCP Tool 带来的 `readOnlyHint`、`destructiveHint` 等信息会参与展示或分类，最终进入宿主的权限流程。

PI 的边界更小。核心循环在执行前完成 Tool 查找、参数准备、Schema 校验和 `beforeToolCall` Hook，应用可以在 Hook 中阻止调用。PI 明确不内置文件系统、进程、网络和凭据沙箱，默认继承启动进程的权限；应用需要借助 Hook 接入 Policy，并另外提供隔离边界。

MCP 规范也把 Tool Annotation 定义为 Hint，并要求客户端不能根据不可信 Server 的声明作安全决定。描述和 Annotation 用于理解调用，本地执行前的 Policy Decision 负责授权；信息不足时按高风险调用处理，触发询问或拒绝。

### 并发建立在准入和资源判断之上

一次模型响应可以包含多个 Tool Call。这些调用满足四个条件时，运行时才会并发执行：

1. 后一个调用不依赖前一个调用的结果；
2. 调用之间不读写冲突的可变资源；
3. 一个调用失败后，其他调用继续执行仍然合理；
4. 外部限流、审批和连接能力允许并发。

只读可以作为并发判断的初步信号，运行时还需要了解调用之间的资源关系。两个只读数据库查询可能争用同一事务，两个访问不同文件的写操作则可能互不冲突。资源关系不明确时，默认串行更安全。

PI 使用较保守的批次策略：全局要求串行，或批次中任意 Tool 声明 `executionMode="sequential"`，整批就按模型给出的顺序执行；否则先完成每个 Call 的准备和拦截，再并发执行。Claude Code 的粒度更细，`isConcurrencySafe(input)` 把连续的安全调用组成并发 Batch，不安全调用独占一个 Batch。

两种实现都会把并发结果恢复成原 Tool Call 顺序再写入 Transcript。进度可以按完成时间展示，模型上下文则维持稳定顺序，避免一次偶然的网络快慢改变消息排列。Definition 列表顺序服务请求稳定性，Call 顺序可能表达本次任务的依赖，两者承担不同职责。

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
> 执行阶段把模型返回的 Call 当作不可信输入。宿主先查找 Tool、做 Schema 和语义校验，再根据具体参数判断只读、破坏性、幂等性和外部访问，最后做 Allow、Ask 或 Deny。Claude Code 的 Bash Tool 会分析本次命令，`git status` 可以是只读调用，写文件或删除命令走另一条权限路径。PI 提供执行前 Hook，文件和网络沙箱需要应用另外实现。
>
> 同一响应中的多个 Call 由 Runtime 安排调度。数据依赖、资源冲突、失败传播和外部限流都满足并发条件时，Runtime 才并行执行；信息不足就串行。PI 的粒度偏批次级，Claude Code 可以按具体 Input 判断安全性。执行方式可以变化，写回模型的结果顺序需要保持稳定。
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

这些属性应结合本次 Input 计算。同一个 Bash Tool，`git status` 和删除命令的副作用不同。声明与 MCP Annotation 可以辅助分类和展示，执行前的宿主 Policy 根据最新状态作出 Allow、Ask 或 Deny。

### 4）Tool Result 怎样进入上下文？

Tool Result 要带原 Call ID，作为下一轮模型消息的一部分与 Tool Call 配对。模型可见 Content、程序使用的 Structured Data、UI 展示字段和日志 Trace 从同一 Outcome 分别投影，每个通道独立控制权限、脱敏和大小。

### 5）结果特别大怎么办？

先在 Tool 输入端过滤和分页；必须保留大结果时，根据语义返回头部、尾部或匹配附近的 Preview，把全文放到上下文外并给出大小、引用和续读方法。摘要作为派生视图，需要保留回查原文的能力；单个 Tool 和整批结果都要设置大小限制。

### 6）Tool 失败应该抛异常还是交给模型？

Tool 实现内部可以抛异常，Executor 在单次调用边界捕获可恢复失败，并转换成带 `is_error` 和 Call ID 的 Outcome。参数错误、权限拒绝和业务失败通常交给模型修正；协议状态损坏、进程级故障或明确中断才终止 Run。自动重试还必须确认幂等性和副作用是否已经发生。

### 7）工具集能否动态增删？

可以。Tool 少时我会预加载全部 Definition；Tool 多时再按需加载 Schema。Claude Code 内置了加权关键词搜索；PI 提供的是 Extension Tool 激活其他已注册 Tool 的能力，具体搜索策略由 Extension 自己实现。按需加载能节省 Context、减少无关 Tool 干扰，代价是多一次往返和搜索遗漏；Provider 不支持时就退回完整预加载。

### 8）什么时候并发，工具列表顺序有讲究吗？

并发需要同时满足四项条件：没有数据依赖、不访问冲突资源、失败互不影响，并且外部限流允许；判断不清就串行。Tool Call 顺序可能表达本次任务依赖，并发结果最好按原顺序回填。Tool Definition 列表顺序服务于确定性序列化和 Prompt Cache，业务优先级应通过名称、描述、Schema 和当前任务表达。

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
