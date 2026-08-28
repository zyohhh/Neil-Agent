# Neil Agent Architecture

## 目标

Neil Agent 是一个运行在终端与本地浏览器中的 Coding Agent。当前版本支持 DeepSeek、Claude、OpenAI、Ollama 与 vLLM 五类 Provider，能够在瞬时失败后进行可观察的有界重试，加载分层项目指令，显式压缩长会话，在限定工作区内读写文件、执行检查和创建本地提交，并管理可搜索、可恢复的本地会话。除 Rich/Textual 终端外，还提供绑定 `127.0.0.1` 的 Web Workbench（`neil-agent-web`）。

## 分层结构

```text
cli.py                          neil-agent-web (web/runtime.py)
  参数路由、终端输入、活动与流式文本协调、高风险操作审批、会话命令    本地 loopback 启动器、静态资源校验、Uvicorn
    ↘ noninteractive.py              ↘ web/app.py
      一次性运行、版本化协议、显式退出码和双阶段审批协调                  FastAPI 路由、CORS/CSP、会话 cookie 与 WebSocket
        ↘ approval.py                    ↘ web/controller.py
          有界审批记录、精确预览绑定、过期检查和一次性消费                    单活动 turn、控制租约、取消与审批编排
    ├→ cockpit.py                          ↘ web/service.py
    │    任务、上下文、安全边界和工作区信号的只读 Rich 投影                    只读快照、Git review、文件树与会话列表 DTO
    └→ live_cockpit.py
         Textual 实时执行树、线程桥接、筛选、详情和审批界面
    ↓
host_runtime.py
  CLI / 非交互 / Web 共享的工具注册、指令作用域、审计 hooks 与 HostProfile 能力矩阵
    ↓
agent.py
  对话历史、工具循环、活动事件、生命周期 hooks、审批协调、修改后验证工作流
    ↘ events.py
      版本化运行时事件、严格元数据白名单、稳定关联 ID 和有界观察者队列
        ├→ event_store.py
        │    显式启用的版本化 JSONL 存储、跨进程锁和单备份轮转
        └→ projections.py
             确定性执行图、时间线、指标与有界纯文本回放
    ↘ hooks.py
      类型化进程内回调、拒绝、审计和有界请求上下文
        ↘ audit.py
          有界、元数据专用的本地 JSONL 审计与单备份轮转
    ↘ activity.py
      工具输入与结果的安全摘要、执行轨迹格式化
    ↘ task.py
      任务计划、步骤状态、最近质量检查
    ↘ context.py
      API 字符估算、完整轮次历史预算、待应用压缩结果
    ↘ diagnostics.py
      配置、工作区、会话、OS 沙箱能力和 Git 的只读本地诊断
    ↘ instructions.py
      分层 AGENTS.md 加载、热重载和非覆盖初始化
    ↘ session.py
      版本迁移、标题、搜索、原子保存、显式加载与删除
    ↘ checkpoint.py
      本进程 Agent 回合级多文件检查点、内容哈希与恢复候选
    ↓
llm.py
  DeepSeek 向后兼容 facade（计划在 0.2.0 移除）；生产路径使用 ProviderFactory
    ↓
providers/
  Provider 身份/能力/错误/重试/工厂，以及 Anthropic Messages 与 OpenAI Responses 编解码边界
    ↓
tools/registry.py
  工具注册、参数绑定、预览和执行分发
    ├→ tools/filesystem.py
    │    工作区受限的读取、搜索和原子写入
    └→ tools/shell.py
         固定质量检查、只读 Git、本地暂存和提交、子进程安全边界
    sensitive_paths.py
      共享凭据目录/文件 denylist，供文件工具、Git 暂存、Web 文件树、guest export 与沙箱快照使用

sandbox.py
  平台无关的不可变执行策略、Windows Sandbox 能力探测和 fail-closed 适配层

evals.py
  默认离线场景执行器、双重显式开启的真实 DeepSeek 协议验收

web/ (React + TypeScript)
  本地 Workbench UI；生产构建写入 src/neil_agent/web/static/ 并随 wheel 分发
```

`schemas.py` 为各层提供消息、工具和用户可见活动事件数据结构，`events.py` 提供独立的可视化观察事件层，`errors.py` 提供统一但分层的用户可见异常，`config.py` 负责从环境变量和 `.env` 加载配置，`sensitive_paths.py` 维护一份凭据目录与文件 denylist。沙箱适配层不会注册工具；通用命令是否可见仍必须由宿主在平台安全门禁通过后显式决定。三条入口（CLI、非交互、Web）通过 `host_runtime.py` 共享工具装配；各入口仍独立负责审批、会话、输出与 UI。能力矩阵与已知差距见 [`host-runtime.md`](host-runtime.md)。对照 Claude Code 后的安全加固批次见 [`security-hardening.md`](security-hardening.md)。Web 产品与协议细节见 [`web-workbench-development.md`](web-workbench-development.md)。

## 项目指令边界

- 指令目标默认为 Neil Agent 启动目录；只有该目录真实位于 `WORKSPACE_ROOT` 内时才使用，否则回退到工作区根目录。
- 加载器从工作区根目录沿真实祖先目录读取到目标目录，按由外到内的顺序组合 `AGENTS.md`；更内层规则只适用于其目录和后代，并在冲突时优先。
- 每个文件必须是对应作用目录中的真实普通文件，不能是符号链接或解析到其他位置；单文件最大 32768 字节，整个生效链最大 65536 字节。
- 内容必须是 UTF-8，可接受 BOM、换行和制表符；拒绝其他 Unicode 控制或格式字符。CRLF/CR 会规范为 LF，空白文件视为未生效。
- 有效内容使用固定边界追加到系统提示词，项目指令之后仍追加不可配置的工具工作流与压缩要求，因此不能降低代码内置的审批和安全边界。
- 项目指令不进入 `Message` 历史、会话 JSON 或 `/instructions` 正文输出；`/instructions` 按顺序展示目标、生效来源、各文件作用域和规模。
- 项目指令段明确声明为非可信仓库上下文；当前用户明确请求优先。即便模型没有遵循该声明，代码级路径与审批边界仍然生效。
- `/reload-instructions` 构造完整的新快照后才替换 Agent 系统上下文，不改变消息历史；任一文件无效、越界或累计超限时继续使用旧的有效快照。
- `/init` 不调用模型，只读取有界的常见项目清单，生成简洁初稿并展示 unified diff；只有 `y` 或 `yes` 才创建，批准后使用独占创建再次保证不覆盖已出现的根 `AGENTS.md`。
- 每个带 `path` 的文件工具调用都会先解析目标目录。生效指令链发生变化时，Agent 更新系统上下文并返回一个“本次未执行”的工具结果；模型必须在下一轮看到新规则后重新决定是否调用，避免先读写再补载规则。
- 作用域刷新失败时文件操作直接拒绝；`AGENTS.md` 仍只提供行为上下文，工作区边界、敏感路径屏蔽和审批继续由代码强制执行。

## 对话和工具循环

1. CLI 把用户输入交给 `Agent.stream_chat()`。
2. Agent 先发布高层活动事件，再将最近的对话历史和工具定义发送给 LLM。
3. LLM 流式返回文本，或在结束事件中返回一个或多个 `ToolCall`。
4. Agent 在工具执行前后发布安全活动摘要，并将 `ToolResult` 作为用户消息返回模型。
5. 模型可以继续调用工具，直到生成最终回答或达到 `MAX_TOOL_ROUNDS`。
6. 只有整个请求成功完成，Agent 才将本轮消息写入历史。
7. CLI 将成功历史、任务计划、最近检查和该回合服务端 `usage` 原子保存到当前本地会话。

思考模式发生工具调用时，LLM 层会保留 Anthropic thinking block，并在后续工具结果请求中完整回传。

可选 `EventBus` 会从同一执行路径旁路接收 `RuntimeEvent`。每个 Agent 回合、模型请求、工具调用、审批和质量检查各有稳定关联 ID；开始与结束事实拥有独立事件 ID，子阶段通过父事件 ID 形成可投影的层级。未配置事件总线时不分配这些 ID，也不增加线程。

事件发布只向每个观察者的独立有界队列执行 `put_nowait`。观察者在守护线程中运行；回调异常被计数后隔离，队列已满时只丢对应观察数据。显式 `flush()` / `close()` 具有超时边界，Agent 主链路从不等待观察者。

运行时事件元数据按阶段使用固定字段白名单，只允许工具名、计数、耗时、状态和 token 用量等有界标量。未知字段、控制/格式字符、过长字符串和非法计数都会被拒绝；prompt、thinking、工具参数值、工具结果正文、审批预览和项目指令正文没有对应字段，不能进入事件。

`JsonlEventStore` 的构造不创建文件；只有宿主显式调用 `register(event_bus)` 才会预检 `.neil-agent/runtime-events/` 并订阅事件。实时 CLI 仅在 `RUNTIME_EVENT_STORE_ENABLED=true` 时执行该注册，默认保持纯内存事件流；`RUNTIME_EVENT_STORE_MAX_BYTES` 控制当前文件的有界轮转上限。每条记录仍由 `RuntimeEvent` 版本 1 模型严格校验，单条和单文件大小都有上限；当前文件、`.1` 备份和锁锚点必须是真实普通文件。大小检查、单备份轮转与追加处于同一个跨进程内核锁临界区，追加后刷新磁盘；加载按备份到当前文件的追加顺序严格检查全部记录，但 Time Machine 在内存中只保留最后 512 条。截断、超限、未知版本或无效 JSONL 都转换为 `EventStoreError`，实时 CLI 会显示通用警告并退回内存回放。

`ExecutionGraphProjector` 与 `TimelineProjector` 是不修改输入的纯投影。它们先按事件 ID 去重，并以规范 JSON 的字典序稳定裁决同 ID 冲突，再按 UTC 时间和事件 ID 排序，因此输入到达顺序不影响结果。缺失开始事件仍生成孤立节点并标记异常；多个开始、重复状态、缺失或无效父事件、结束早于开始都会保留固定异常。冲突终态使用 `failed > skipped > succeeded` 的固定优先级；父级环反复移除环中字典序最大的关联 ID 的父边，直到得到 DAG。`MetricsProjector` 再从图中汇总节点状态、模型/工具数量、服务端 token 和已报告阶段耗时。纯文本回放只消费这些不可变投影，并限制展示条目，不依赖 Rich、Textual 或 Agent 执行。

当注册表同时提供文件写入和质量检查工具时，Agent 会在用户系统提示词后追加不可配置的本地工具工作流：质量检查仅在用户明确要求或确有必要时调用，且预览与 `/permissions` 明确其为无 OS 隔离的宿主执行；命令结果固定返回 `Command`、`Working directory`、`Exit code` 和 `Output`，最终回答据此汇总验证结果。写入成功后不再自动催促模型跑检查。

当注册表提供计划工具时，多步骤开发任务会先调用 `set_task_plan`，再用 `update_task_step` 按顺序推进。计划变化通过注入式回调立即显示在 CLI，不需要等待模型最终回答。

CLI 使用 `TerminalRenderer` 统一处理三类异步输出：Agent 活动事件、任务计划变化和模型流式文字。执行中的事件启动 Rich spinner 和动态耗时，使模型和长时间工具请求保持持续反馈；活动完成、计划插入或收到模型文字时停止 spinner。活动或计划插入时会先结束当前回答行，后续模型文字重新显示回答前缀，避免多个回调直接写终端导致内容粘连。

### 启动工作台

- CLI 在初始化配置、工具、指令快照、会话和 Agent 后，只渲染一个响应式 Rich 面板，再进入输入循环。
- 面板集中显示模型、思考模式、工作区、会话 ID、工具总数、需审批工具数和项目指令状态；它只读取已加载的本地元数据，不增加 API 请求或 Git 操作。
- 项目指令只显示状态、来源数量、总字节数或固定错误原因，绝不展示正文；缺失与空文件状态分别给出 `/init` 和 `/reload-instructions` 提示。
- 所有路径、模型名和会话 ID 都使用 `Text` 节点按纯文本渲染；网格第二列允许折行，因此窄终端和长路径不会破坏边框，也不能通过 Rich markup 改变界面。
- 面板底部只提供常用入口和取消/退出提示；详细命令仍由 `/help` 维护，避免启动页随功能增长而变成完整手册。

### 基础只读驾驶舱

- `/cockpit` 从当前 Agent、任务计划、工具注册表、项目指令快照、任务文件检查点和 Git 状态构造不可变的有界快照；它不调用模型、不写文件，也不改变执行状态。
- 驾驶舱展示任务矩阵、字符/token 软预算与最近服务端实测、工具审批与安全边界、项目指令元数据、检查点数量和 Git 信号。
- 运行时字符串使用 Rich `Text` 按纯文本渲染并限制长度；项目指令正文、prompt、thinking、工具参数/结果正文和凭据不进入快照。
- `/cockpit` 继续是请求时生成的 Rich 基础视图，不订阅事件总线；它也是实时模式不适用或启动失败时的稳定降级入口。

### 实时执行树

- `/cockpit --live` 只在交互终端显式启动 Textual 8 全屏界面。进入时为当前 Agent 临时挂载独立 `EventBus`，退出后立即解除并关闭；普通交互请求、`text`、`json` 和 `stream-json` 协议不创建该总线。
- Agent 请求在 Textual thread worker 中运行；事件总线观察者只把事件放入容量 1024 的合并桥，再使用线程安全的 Textual message 更新主线程。界面事件窗口最多保留 10000 条；总线或视图丢弃会进入可见计数，不阻塞 Agent。
- 每批事件都重新经过确定性 `ExecutionGraphProjector` 与 `MetricsProjector`，Textual 不维护第二套节点状态。树按 Agent 回合 → 模型请求 → 工具/审批/检查展开，并提供全部、进行中、失败和工具四种筛选；筛选结果自动保留祖先上下文。
- 详情抽屉只显示事件已有的状态、稳定 ID、时间、异常和白名单元数据，不显示 prompt、thinking、工具参数或结果正文。界面底部的 Agent 文本流属于当前交互输出，不会被复制到 RuntimeEvent 或图投影。
- 高风险工具仍展示完整预览并要求 `Y` 明确批准；取消或界面关闭会拒绝待处理审批。全屏期间经典 Rich 活动、重试和计划回调临时静默，避免跨线程写坏终端，退出后恢复原处理器。
- 宽终端并列显示主视图和详情；宽度小于 88 列时隐藏详情。监控区与回答流默认均衡使用剩余高度，高度小于 36 行时压缩标题和指标并优先扩大回答流；`F2`（或 `Ctrl+O`）可隐藏监控区、展开回答流，再次触发后无损恢复。resize 会即时切换宽度和高度布局。非交互终端、导入失败或 Textual 启动异常自动回退 `/cockpit` 快照。

### Time Machine 只读回放（Phase 3A，已完成）

- `TimeMachineProjector` 是版本 1 的纯投影。它把持久事件和当前进程事件交给既有 `TimelineProjector` 规范化，在最多 512 条可见事件上按游标重新构造 `ExecutionGraph` 与 `RuntimeMetrics`；选择历史事件不会发布事件、调用模型/工具/hook 或改变 Agent。
- 会话宿主最多读取当前 50 条严格 `SessionSummary`，任务检查点宿主最多读取当前 20 条进程内快照；原始对象只跨越一次短生命周期边界，随即变成不含标题、预览、消息、路径、哈希或文件正文的 `TimeMachineHistoryProjection`。Textual 应用长期保存的只有该脱敏投影。事件 DAG/指标按游标重建 as-of 状态；会话与检查点明确标作当前有界目录，不伪称能够从这些摘要还原过去每一时刻的内容。
- 会话投影只显示稳定 ID 后缀、创建/更新时间、轮数、计划/检查/压缩标记和直接父 ID，由此区分根、分支与父节点已在窗口外的孤立分支。检查点只显示时间、创建/修改文件数量与结果字符数。
- `F6` 在现有监控槽打开只读导航树；宽屏右侧显示详情，窄屏在树下显示内联详情，矮屏隐藏次要标题并完整保留回答流和输入框。事件选择使用稳定 event ID，重新投影后不会因序号变化误选别的事实。
- 持久事件存储默认关闭。显式开启时，进入实时驾驶舱会先严格加载备份与当前 JSONL，再注册同一 `EventBus` 观察者；退出前只在有界等待内刷新已接受事件。界面中的持久数量只表示进入视图前已成功加载并验证的已知记录，不把仍在观察队列中的事件冒充为已落盘。
- Phase 3A 不提供恢复命令、按钮或回调。Phase 3B 在 `/cockpit --live` Time Machine 中为**最新**任务检查点提供 `R` 审批恢复，复用 `/rewind-task` 预检与回滚语义；较旧检查点与会话/事件状态仍不能从此入口恢复。

### Neural Map 仓库活动热力图（Phase 4，已完成）

- `NeuralMapProjector` 从 `tool_call` 事件的脱敏 `workspace_path` 与 `activity_kind` 元数据聚合目录级读/写/检查热度；不扫描工作区、不保存文件正文或哈希。
- 投影在最多 512 条事件上保留最多 48 个目录节点，按 EARLY/MID/LATE 时间窗口与低/中/高风险着色；超限时向父目录 rollup。
- `F7` 在现有监控槽打开目录树与活动投影；宽屏右侧显示详情，窄屏在树下显示内联详情，交互模式与 Time Machine 一致。

### 实时上下文断层图（Phase 2A，已完成）

- `ContextTomography` 是版本 2 的不可变、元数据专用快照，固定包含系统开销、工具 schema、项目指令、已选择历史和当前链路五层。每层只保存字符数、本地 token 估算和项目数；prompt、项目指令、消息、工具参数与结果正文都不进入快照。
- 五层使用与实际请求预算相同的紧凑 JSON 估算器。系统、项目和工具层通过固定开销的差值拆分，五层总和严格等于完整固定开销、入选历史与当前链路之和；抽取共享的历史选择函数后，原有完整轮次裁剪语义不变。
- 快照同时保存历史总量与入选量，因此可以确定性展示被裁剪的轮次数、字符数和估算 token 数；压缩检查点标记为 `none`、`kept` 或 `omitted`。最多保留三个最大工具结果的时间序号、估算体积及保留状态，不保存工具 ID、名称或结果正文。
- `LOCAL NEXT INPUT · ESTIMATE` 只描述下一次请求的本地软预算；`LAST SERVER MEASUREMENT` 是最近成功 Agent 回合中全部模型请求的历史汇总，明确拆分输入、输出和缓存 token，并固定标为 `HISTORICAL · NOT A FORECAST`。
- `ContextBudgetPressure` 使用整数基点比较字符预算与可选 token 预算中更紧的一项：低于 75% 为 `SAFE`，75%–不足 90% 为 `WATCH`，90%–100% 为 `HIGH`，超过 100% 为 `OVER`。颜色、文本和边框同时表达状态，不依赖单一颜色辨识。
- `Agent.context_what_if()` 接受 1–1,000,000 个字符，使用明确标注的 ASCII（约 0.3 token/字符）合成输入重新运行同一完整轮次选择路径，生成版本 1 的只含计数 `ContextWhatIf`。它不调用模型、工具或 hook，不发布运行时事件，也不修改历史或最近服务端 usage。
- Textual 使用同一个工作区槽位承载 DAG 和 Context，`F3`（或 `Ctrl+T`）切换，不再增加会压缩回答区的第三个纵向面板。Context 视图按 `F4` 打开本地模拟对话框，输入 `0` 可清除；Agent 回合执行时禁用模拟，避免与历史写入并发。宽屏显示完整前后对照，窄屏压缩为四行洞察，矮屏将基础与模拟压力收进边框副标题，完整保留五层和回答区。
- 快照只在驾驶舱挂载、用户提交和回合结束时计算，不由 250 ms 指标刷新反复扫描历史。提交态的 `CURRENT CHAIN` 目前只表示规范化后的当前用户消息，界面明确标记 `SUBMIT SNAPSHOT`；工具循环追加消息和 `before_model` hook 的动态上下文尚未纳入本切片。
- Phase 2A 的三批交付已全部完成；Phase 2B Security Shield、Phase 3A Time Machine 只读回放、Phase 3B 最新检查点恢复与 Phase 4 Neural Map 均已收口，见 [`visualization-development.md`](visualization-development.md)。

### Web Workbench（P9 已完成）

- `neil-agent-web` 只绑定 `127.0.0.1`；启动器校验 wheel 内静态资源 SHA-256 清单后才打开浏览器，端口冲突或资源损坏时 fail-closed。
- HTTP 提供健康检查、一次性 bootstrap、完整快照、只读会话/文件树/Git review；WebSocket 使用短时单次 ticket，命令带 `expected_revision` 与 `command_id` 去重。
- `WorkbenchController` 在后台线程运行 `Agent.stream_chat()`，通过 `EventBus` 与 `asyncio` 队列向浏览器推送有界元数据事件；审批仍逐工具、预览重校验，默认拒绝。
- 浏览器可开始/取消单个 Agent turn、接收流式回答与活动、审批高风险工具；Review 使用固定只读 Git 命令，不向浏览器开放任意 shell 或 thinking 正文。
- Web 每轮构造隔离的 `Agent`，随后恢复当前选中的严格 `SessionSnapshot`；成功 turn 原子保存，失败/取消不落盘，保存失败会闭锁后续 turn 直到显式新建或重选会话。
- `new_session` / `select_session` 受控制租约、精确 revision 与 idle 状态约束；跨 Provider/模型私有状态在发网前拒绝，消息正文不进入浏览器快照或 `session_changed` 事件。
- `switch_model` 只接受 `WEB_RUNTIME_MODEL_ALLOWLIST` 中同一启动 Provider 的精确模型 ID，并且仅在控制端、revision 匹配、无运行/审批且活动会话为空并未保存时生效。事务先重建并校验完整 `Settings` 与下一 turn worker，再一次性替换运行时；准备失败保持旧模型，切换本身不发送网络请求。
- 会话版本 5 保存可选 Provider/模型绑定。Web 成功 turn 必须写入绑定；已绑定会话只能由完全相同的运行时恢复和续写。进程内发生过模型切换后，含历史但没有绑定的旧会话 fail closed，不能借 `select_session` 绕过空会话门禁。
- Web 与 CLI 共用 `observe_host_security()`；条件 `run_command`、应用工具边界、OS 沙箱和审计状态使用同一注册与观察语义。Web 快照通过 `build_host_context_tomography()` 投影与 Agent `/context` 同源的 `ContextTomography` 元数据。详见 [`host-runtime.md`](host-runtime.md)。

## 上下文预算

- `MAX_ROUNDS` 限制内存和快照中保留的完整用户轮数；构造下一次请求时，最多携带其中最近的 `MAX_ROUNDS - 1` 轮，为当前输入留出一轮。
- `MAX_CONTEXT_CHARS` 是本地软预算，默认 `120000`。字符数根据紧凑 API JSON 近似计算，包括系统提示词、工具定义、文本、思考块、工具参数和工具结果，但不等同于 DeepSeek 的精确 token 数。
- `MAX_CONTEXT_TOKENS` 是可选软上限；未配置时保持原有字符预算。当前适配层依据 DeepSeek 官方近似说明使用英文/ASCII 字符约 `0.3 token/字符`、中文字符约 `0.6 token/字符`，其他非 ASCII 字符按 `1 token/字符` 保守计算。
- DeepSeek 当前提供可下载的离线 tokenizer 演示包，但没有在聊天请求前返回精确计数的轻量接口；实际请求和计费以响应 `usage` 为准。因此本地数值始终标记为估算，不代替服务端 token 统计。参见 [DeepSeek Token 用量说明](https://api-docs.deepseek.com/quick_start/token_usage/)。
- LLM 层复制服务端输入、输出和缓存 token 字段；Agent 会累加同一成功工具回合中的多个模型请求。`/context` 同时显示下一次请求的本地软估算和最近成功回合的服务端实测，明确不把历史实测外推为下一次费用。
- Agent 从最新历史轮次向前选择连续后缀，同时满足轮数、字符预算和可选 token 预算。任一轮超出预算时停止，不会拆分工具调用/结果，也不会跳过较新的大轮次去保留更旧的小轮次。
- 当前用户输入及当前正在进行的工具链始终保持完整；它们可以使本次请求超过软预算。预算的职责是阻止以前的大型工具结果持续占满后续请求，而不是破坏当前协议结构。
- `/context` 只读取本地状态，展示固定开销、保存历史、下次请求可带历史和省略轮数，不调用模型。由于下一条输入未知，实际请求可能比该页面显示的基础占用更大，并进一步减少历史。

## 显式会话压缩

- `/compact [关注点]` 不是自动策略，只有用户明确输入时才调用模型；默认至少需要 3 轮，并完整保留最近 2 轮。可选关注点最多 500 字符并拒绝控制字符。
- 较早轮次按 API JSON 序列化，使文本、思考、工具调用参数和工具结果关系仍可被摘要模型理解。单轮输入上限 20000 字符，超出部分以保留前后内容的明确标记缩减。
- 压缩请求同样受 `MAX_CONTEXT_CHARS` 约束。多轮无法一次容纳时按顺序分批滚动更新摘要；连最小批次也无法容纳时直接失败，单次命令最多调用模型 8 次。
- 压缩系统规则要求旧历史仅作为数据，摘要只保留持续工作需要的事实，且输出不得超过 8000 字符。压缩调用不提供工具；候选历史未实际减少近似字符数时也拒绝应用。
- 候选历史由一个固定的用户/助手检查点轮次和最近原始轮次组成，继续满足消息历史与工具调用配对验证，并使用当前会话格式版本 5 保存。
- 候选生成阶段不修改内存；应用前先把当前完整会话分支为“压缩前”副本，再原子保存候选并应用到 Agent。模型失败、取消或备份失败均保留原历史；候选保存失败时原会话仍不变，已创建的备份也保留。
- 固定检查点在轮数清理时与最近轮次一起保留，在字符预算足够时也优先进入模型请求；`/clear` 或后续压缩会清除或替换它。

## 模型请求重试

- Provider SDK 客户端关闭隐藏重试；`providers/retry.py` 提供公共有界策略，各 Provider runtime 负责驱动并报告每次重试，保证次数、等待和终端状态可观察。生产入口统一通过 `ProviderFactory` 构造实现；旧 `LLMClient` 只保留为 0.1.x DeepSeek 外部兼容 facade，并计划在 0.2.0 移除。
- 只把限流、HTTP 408、HTTP 5xx、超时和连接错误视为瞬时失败；鉴权、权限、请求格式及其他 4xx 错误不重试。
- 等待时间以 `RETRY_BASE_DELAY` 指数增长，并受 `RETRY_MAX_DELAY` 限制；有效的 `Retry-After` 或 `retry-after-ms` 可以提供等待建议，但不能突破本地上限。
- `MAX_RETRIES` 表示首次请求后的额外尝试次数。达到上限后，最后一个 SDK 异常转换为稳定的中文 `LLMError`。
- LLM 层通过注入式活动回调报告失败原因、重试次数和等待时间；CLI 复用 `TerminalRenderer` 显示动态状态，不输出原始响应体或请求数据。
- 流式响应只在尚未输出正文时重试。任意正文片段已经交给用户后禁止重试，避免重复输出；当前失败轮仍不会进入 Agent 成功历史。

## 本地诊断边界

- `/doctor` 只检查当前已加载配置、Provider 线协议与能力快照、工作区权限、会话目录、启用后的审计文件/锁、OS 沙箱静态能力和 Git，不构造 SDK client、不调用模型 API，也不修改文件、会话、审计日志、系统沙箱配置或 Git 状态。
- API Key 只报告已配置且值已隐藏；endpoint 只显示 scheme、host、port 和 path，凭据及 query/fragment 值始终隐藏。Ollama/vLLM 的 loopback HTTP 是正常本地端点，其他非 HTTPS 地址产生警告。
- 工作区检查使用本地权限信息；会话检查复用 `SessionStore.list_sessions()` 的路径、符号链接、格式和版本边界。
- 审计检查不会创建目录或文件；它先做非阻塞锁探测，空闲时在锁内统计当前/备份文件的记录和格式，繁忙时只报告警告。诊断不返回任何审计正文。
- OS 沙箱检查只读取平台和可执行组件能力。默认 `disabled` 是安全的正常状态；显式选择 Windows 后端但组件不可用或能力不完整时报告错误，且不会启动探针进程、创建 profile、修改 ACL 或回退到普通子进程。
- Git 检查复用受限的 `git_status_snapshot()`，仅报告可用性和是否存在未提交变更，不复制文件列表或 diff。
- Git 不可用、非 HTTPS 地址或损坏会话属于警告；工作区不可读或会话目录不安全属于错误。诊断不自动修复任何问题。

## 执行活动边界

- 活动事件只描述可观察的输入、执行阶段和结果，不展示或推断模型思维链。
- 模型请求报告轮次、上下文规模和可用工具数；工具响应列出本轮选择的工具名称。
- 工具状态区分执行中、等待批准、成功、失败和审批跳过；成功与失败事件显示本地流程耗时。
- 文件读取只显示路径和结果规模；写入与替换只显示路径、字符/行数和预计替换数，不复制文件正文或替换文本。
- 搜索显示经过限制的查询、范围、匹配数和最多三个匹配位置；目录查看最多展示前三个条目。
- 质量检查和 Git 工具显示有界命令、退出码及结果摘要；Git diff 不打印完整补丁，只报告行数、字符数和最多四个文件名。
- 提交消息、搜索词、路径和错误摘要会移除控制字符、压缩为单行并限制长度。
- 未知工具只显示受限工具名称和参数字段名，不回显未知参数值。

## 本地会话边界

- 会话快照固定存储在 `WORKSPACE_ROOT/.neil-agent/sessions/`，当前写入版本 5；版本 1–4 仍可严格读取，并在下一次保存或重命名时迁移为版本 5，未知未来版本继续拒绝。
- 版本 2 新增本地标题；首次成功回答后从第一条用户请求确定性生成默认标题，不调用模型。标题可通过 `/rename-session <标题>` 修改，最多 80 个字符并拒绝控制或格式字符。
- 版本 3 新增最近一次成功 Agent 回合的可选服务端 `usage`；旧会话迁移时该字段为 `null`。
- 版本 4 新增可选直接父会话 ID；根会话和从版本 1–3 迁移的会话为 `null`，`/branch` 创建的新会话绑定源会话 ID，并拒绝自引用。该字段只表达本地谱系，不继承审批或权限。
- 版本 5 新增成对出现的可选运行时 Provider/模型绑定；Web 成功 turn 写入绑定，重命名、分支与导入导出原样保留，已有绑定不能在后续保存时改写。从版本 1–4 迁移的会话保持未绑定。
- 快照字段仅包含会话 ID、直接父会话 ID、运行时绑定、标题、创建/更新时间、成功消息历史、任务计划、最近质量检查和最近 usage；不包含 API Key、endpoint、环境变量、系统提示词或远程同步配置。
- 消息历史必须由完整的用户/助手轮次组成；工具调用 ID 与结果 ID 必须按原顺序完全匹配，失败或中断的半轮消息不能恢复。
- 任务计划恢复时重新验证标题、数量和状态顺序，保证最多一个进行中步骤，且其前后只能是已完成和待处理步骤。
- 单文件上限为 25 MB；会话 ID 使用固定格式，并同时校验 JSON 内部 ID 与文件名。
- 保存使用会话目录内临时文件、`fsync` 和 `os.replace`；替换失败保留原文件，临时文件会被清理。
- 读取拒绝工作区外目录、符号链接和不匹配路径；损坏或不兼容文件在 `/sessions` 中被计数并跳过。
- `.neil-agent` 被 Git 忽略，也是文件工具与 `git_stage` 的受保护目录，模型不能通过现有工具读取或暂存会话。
- 成功回答后自动保存当前会话；`/resume <id>` 必须由用户明确触发，程序启动时不自动恢复或上传数据。
- `/branch [标题]` 复制当前完整快照到新 ID、记录源会话为直接父节点并切换，原会话不变；审批决定不会持久化，因此不存在跨分支继承的批准状态。
- 压缩检查点使用普通且严格验证的完整消息轮次保存，不新增秘密字段；恢复后 Agent 会重新识别其固定语义。
- `/sessions [选项] [关键词]` 展示有效会话数量、匹配数量、所有 JSON 文件总占用及各会话大小；支持页码、每页数量、标题/更新时间排序，以及 `planned`、`failed`、`compacted` 状态筛选，全程不调用模型。
- `/export [id]` 将严格的版本 1 导出信封写入 `.neil-agent/exports/`；当前导出版本 5 会话，同时兼容导入版本 1–4，不包含环境配置、系统提示词和项目指令，且不会覆盖同名文件。
- `/import <文件名>` 只读取该导出目录中的真实普通 `.json` 文件，验证 25 MB 上限、信封版本、会话版本、完整消息和重复 ID；批准后重新校验源文件哈希与目标不存在，再独占创建会话快照。
- `/delete-session <id>` 只接受精确 ID，先展示会话摘要，再要求 `y` 或 `yes` 明确确认；当前活动会话不能删除。
- 删除时重新验证会话文件；程序不会自动删除、轮转、上传或合并任何历史会话，损坏文件也不会被静默清理。

## 工具权限模型

工具注册分为两类：

- 直接执行：`list_directory`、`read_file`、`search_text`、`git_status`、`git_diff`、`set_task_plan`、`update_task_step`
- 必须审批：`write_file`、`replace_text`、`run_quality_check`、`git_stage`、`git_commit`；认证就绪时还条件注册 `run_command` 与 `import_guest_export`（后者依赖已暂存的 guest export manifest）

`/permissions` 只读取注册表与工作区配置，展示上述分类、敏感路径、命令和网络边界，并明确当前没有 OS 级命令沙箱；它不修改规则，也不把提示词描述成强制权限。

审批工具必须同时注册预览函数。执行流程为：

```text
参数校验 → 生成操作预览 → 用户确认 → 执行 → ToolResult
```

没有明确批准时，注册表拒绝执行高风险工具。CLI 只接受 `y` 或 `yes`，其他输入均视为拒绝。文件 diff 包含基于修改前后内容生成的 `Change-ID`；执行前注册表会重新生成预览，如果与用户批准的版本不一致，则要求重新确认。截断预览仍保留 Change-ID 与完整规模，避免未见尾部被当成已审全文。批准后、执行前，Agent 还会复核当前项目指令摘要是否与预览时一致。质量检查预览显示精确命令、工作目录、超时时间，并说明为无 OS 隔离的宿主执行。

## 文件安全边界

- 所有路径解析后必须位于 `WORKSPACE_ROOT`。
- 防止利用 `..`、绝对路径或符号链接逃出工作区。
- 屏蔽规则只在 `sensitive_paths.py` 维护：`.env` / `.env.*`（`.env.example` 除外）、`.ssh` / `.aws` 等凭据目录、`id_rsa` / `credentials.json` 等文件名、`.git`、`.neil-agent`、`.venv`、缓存目录和常见私钥后缀。host 文件工具、Git 暂存、Web 文件树、guest export 与 sandbox snapshot 共用该名单。
- 单文件读取和写入上限为 1 MB。
- 搜索结果最多返回 100 条。
- diff 预览最多显示 20,000 字符。
- 过期的 diff 审批不能用于已经发生外部变化的文件。
- 精确替换要求实际匹配数量等于 `expected_replacements`。
- 写入使用同目录临时文件和 `os.replace`；替换失败时原文件保持不变。
- 一次 `Agent.stream_chat()` 回合构成一个文件任务边界；无论回合成功、失败或被取消，已完成的 `write_file` / `replace_text` 都会在边界结束时合并为一个内存检查点。同一路径只保留任务前原内容和最终结果哈希，任务内回到原内容的净零修改不会留下检查点。
- 单个任务最多记录 50 个路径；任务前原内容与任务后回滚内容分别限制为 5,000,000 字符，路径最多 1,000 字符，历史最多保留 20 个任务。任何无法安全记录的下一次写入都在文件变更前 fail-closed，旧历史只在新任务完成后按边界淘汰。
- `/rewind-task`（兼容别名 `/rewind-file`）列出全部目标和有界反向 diff，再要求 `y` / `yes` 批准；准备阶段及批准后会全量校验最新任务、路径范围、真实普通文件和每个结果哈希，任一目标变化都在首个恢复写入前拒绝。
- 多文件恢复逐个使用原子文件替换或删除；普通进程内步骤失败会把已经恢复的路径按反向顺序写回预览时内容。若回滚期间又有外部变化或回滚失败，会明确报告工作区可能不一致并保留检查点，要求使用 Git。
- 文件任务检查点不持久化，不恢复目录、权限、ACL、扩展属性或外部程序修改；进程/系统崩溃可能发生在两个文件替换之间，因此不承诺崩溃原子性。完整威胁模型见 [`checkpointing.md`](checkpointing.md)，Git 仍是跨进程和持久化回退的可靠方式。

## 命令安全边界

- `run_quality_check` 只允许离线 `eval`、`pytest`、`ruff`、`mypy`，调用参数由程序固定拼装；在宿主机以 `shell=False` 执行，无 OS 沙箱或网络隔离，预览与 `/permissions` 会明确告知。
- `git_status` 固定读取简洁状态；`git_diff` 只允许切换是否查看暂存区，并禁用 external diff 与 textconv。
- Git 命令禁用 fsmonitor、分页器和可选锁，避免执行扩展程序或产生非必要写入。
- 不接收任意可执行文件、命令参数或 Shell 字符串，子进程始终使用 `shell=False`。
- 命令工作目录固定为解析后的 `WORKSPACE_ROOT`，标准输入设为空，避免命令等待交互。
- 子进程环境采用最小白名单，不继承 API Key、访问令牌等敏感变量。
- 命令受 `COMMAND_TIMEOUT` 约束，返回内容受 `MAX_COMMAND_OUTPUT_CHARS` 约束。
- 非零退出码和超时会作为 `ToolResult(is_error=True)` 返回模型，而不是绕过工具错误边界。
- 上述限制是应用层白名单，不是 OS 沙箱。原生 Windows 的 AppContainer/LPAC 或独立 Windows Sandbox、Linux 的 namespace/seccomp 等都需要独立策略和平台实现；结论与开放通用命令前的门槛见 [`sandbox-assessment.md`](sandbox-assessment.md)。
- 认证 Windows Sandbox 下，可选 `run_command(export_paths=...)` 允许 guest 在声明路径产出 UTF-8 文件，经 manifest 暂存与 `import_guest_export` 二次批准后写回工作区；未声明修改仍丢弃。完整流程见 [`guest-export-import.md`](guest-export-import.md)。

## 一次性运行与结构化协议

- `neil-agent -p <prompt>` 复用 `Agent.stream_chat()`，完成一个请求后退出；交互式启动行为保持不变。
- 默认协议 v1 的注册表仅包含 `list_directory`、`read_file`、`search_text`、`git_status` 和 `git_diff`，不存在审批输入，也不暴露任何写操作或子进程质量检查。
- 协议 v2 必须显式选择 `request` 或 `approve` 才注册受审批工具；`request` 运行只生成预览并拒绝执行，`approve` 运行最多消费一个完全匹配的请求。
- `text` 只把模型正文流写入标准输出；`json` 只写一个最终 JSON 对象；`stream-json` 写入逐行独立 JSON 事件。结构化协议不会夹入 Rich 装饰、spinner 或思考内容。
- 审批请求保存在 `.neil-agent/approvals/`，有效期为 15 分钟。记录保留精确预览、工作区和哈希，不保存 prompt、项目指令或工具参数正文；预览本身可能包含用户必须审查的目标代码 diff。
- 调用方收到的 approval ID 同时携带完整审批记录摘要，因此待审批文件被外部改写也不能改变用户已经确认的授权。批准进一步绑定工作区、prompt、当前项目指令、工具名、规范化参数和最新预览；任一变化都会拒绝旧请求并生成新预览。
- 匹配请求在工具执行前以独占标记消费，进程失败或执行失败也不能重放。
- Agent 在批准后仍会由 `ToolRegistry` 重新生成一次预览，从而覆盖消费审批到实际 handler 之间的文件变化窗口。OS 级并发仍由具体工具的原子写入和 Git 校验负责。
- 默认会话只存在于本次进程中；`--save-session` 才以独占创建方式保存成功会话。等待审批、失败或中断不会写入快照。
- 成功终止对象包含本回合服务端 `usage`；结构化错误包含稳定的 `error_code`。版本 1、2 的精确字段集合与错误代码由独立契约夹具和 JSON/JSONL 回归测试固定。
- 当前退出码为成功 `0`、运行错误 `1`、参数或配置错误 `2`、等待审批 `3`、用户中断 `130`。

## 生命周期 hooks

- `LifecycleHooks` 只接受宿主显式注册的 Python callable，不解析配置中的程序名、命令参数或 shell 字符串。
- `before_model` 可以拒绝请求或附加有界上下文；单个回调最多 2000 字符、单阶段累计最多 4000 字符，并拒绝不允许的控制/格式字符。附加内容仅进入本次模型请求，不进入消息或会话快照。
- `before_tool` 可以在指令作用域解析和工具执行前拒绝调用；失败时默认关闭该次工具执行，并向模型返回错误结果。
- `after_model` 与 `after_tool` 只用于审计，分别接收类型化模型响应或工具结果，不能拒绝或追加上下文。后置工具审计失败会明确说明操作可能已经完成，避免错误暗示回滚。
- 每个阶段最多注册 10 个回调，按注册顺序执行；回调异常和无效返回类型统一转换为 `HookError`。
- hooks 是宿主代码信任边界而不是插件沙箱；`after_model` 能看到完整类型化响应，其中可能含 thinking block。不得注册不可信回调，也不应原样记录 prompt、thinking 或工具正文。
- 可选 `JsonlAuditSink` 在注册时预检 `.neil-agent/audit/`，随后复用四个 hook 阶段，只写时间、阶段、轮次、数量、工具名和结果规模等有界元数据。它不写 prompt、thinking、工具参数值、工具结果正文或 API Key。
- 审计日志单条最多 4096 字节，达到 `AUDIT_LOG_MAX_BYTES` 后保留一个 `.1` 备份；目录、当前日志和备份都必须是真实路径/普通文件，写入会刷新磁盘，失败不会静默忽略。
- `events.lock` 只作为操作系统文件锁的稳定锚点；大小检查、备份替换和追加处于同一个跨进程临界区。Windows 使用字节范围锁，POSIX 使用 `flock`，等待有固定上限并 fail-closed。
- 锁归属由内核维护，进程正常退出或崩溃都会释放；实现不根据 PID 或时间戳猜测并删除“陈旧锁”，避免误删仍由其他进程持有的锁文件。

## 本地 Git 写入边界

- `git_stage` 只接受最多 50 个明确的工作区相对文件路径，并使用 literal pathspec 阻止 Git pathspec magic。
- 不允许暂存整个工作区、目录、越界路径，以及 `sensitive_paths.py` 列出的受保护目录、`.env` 或常见私钥/凭据文件。
- 暂存预览包含状态、已暂存和未暂存 diff，以及未跟踪文本文件内容；Git clean filter 可能运行外部程序，因此必须审批。
- 暂存预览的 `Change-ID` 同时覆盖 Git diff 和完整文件内容，预览后发生任何相关变化都会要求重新确认。
- `git_commit` 只从当前暂存区创建本地提交，消息必须是 1–200 字符的单行文本。
- 提交预览显示暂存区统计和 diff；超长 diff 会截断，但 `Change-ID` 覆盖完整内容；空暂存区不能提交。
- 提交时禁用 hooks 和 GPG 签名，不提供 amend、空提交、远程操作或推送入口。

## 任务状态边界

- 活动计划存在于当前进程内，最多包含 5 个步骤，每个步骤最多 200 字符；成功回答后会随本地会话快照保存。
- 状态只允许 `pending`、`in_progress`、`completed`；同一时间最多有一个进行中步骤。
- 新计划自动启动第一步；完成当前步骤后自动启动下一步，禁止跳步或重新打开已完成步骤。
- `TaskTracker` 只记录最近一次质量检查，并把审批拒绝区分为 `not_run`，把启动或执行错误记录为 `failed`。
- `/status` 直接读取内存计划和检查快照，同时调用受限的只读 Git 状态方法获取当前仓库状态。
- `/status` 的 Git 快照使用独立的 5 秒上限；超时会显示不可用，不会长时间阻塞终端。
- `/status` 不发送模型请求；`/clear` 会清除活动对话、计划和最近检查并创建新会话 ID，但不会删除旧快照或修改 Git。

## 评测边界

- `neil-agent-eval` 默认读取 `evals/tasks.json`，使用确定性假模型和临时工作区执行全部已声明场景，不读取 API Key、不访问网络，也不修改真实项目文件。
- `--task <id>` 可重复指定单个场景，`--format json` 输出稳定字段和毫秒耗时；`run_quality_check(eval)` 复用同一离线 JSON 命令，不会自动启用真实 DeepSeek 模式。
- 未注册离线执行器的任务会明确失败，防止只增加文字场景却未接入执行逻辑；进程以非零退出码报告失败，可直接接入本地检查或 CI。
- `--real-deepseek` 单独使用不会发送请求；必须同时提供 `--confirm-api-cost` 才会运行真实验收。
- 真实模式把工作区固定到自动删除的临时目录，验收设置固定关闭 thinking、单轮工具上限、1024 输出 token、40000 字符上下文、60 秒超时和最多一次瞬时失败重试。它验证 v1 五个默认只读工具、精确 `read_file`、项目指令、服务端 `usage`、显式会话保存、压缩恢复，以及 v2 request/approve 和消费后重放拒绝。
- v2 的唯一写入只发生在临时目录；审批 ID、预览和原始模型响应不进入报告。v1 前置失败时不会继续发起压缩和 v2 请求。自然重试只观察真实发生次数，不主动制造限流或网络故障。

## 异常边界

```text
NeilAgentError
├── AgentError   Agent 循环和编排错误
├── LLMError     DeepSeek/API 和模型响应错误
├── ToolError    工具参数、权限和文件操作错误
├── SessionError 本地会话存储、格式和恢复错误
├── InstructionError 项目指令初始化与重载错误
├── HookError    生命周期 hook 注册、决策或回调错误
├── AuditError   本地审计初始化或写入错误
├── ApprovalError 一次性审批记录无效、过期或已消费
├── EventStoreError 运行时事件存储、格式或轮转错误
└── SandboxError OS 沙箱不可用、能力不完整或拒绝执行
```

工具执行错误通常会转换为 `ToolResult(is_error=True)` 返回模型；无法在工具内部处理的 Agent 或 LLM 错误由 CLI 捕获并展示。

## 关键配置

| 环境变量 | 作用 | 默认值 |
| --- | --- | --- |
| `DEEPSEEK_MODEL` | 模型名称 | `deepseek-v4-flash` |
| `THINKING_ENABLED` | 是否启用思考模式 | `false` |
| `MAX_ROUNDS` | 对话历史窗口 | `20` |
| `MAX_CONTEXT_CHARS` | 模型请求的近似 JSON 字符软预算 | `120000` |
| `MAX_CONTEXT_TOKENS` | 可选的近似 token 软预算 | 未配置 |
| `MAX_TOOL_ROUNDS` | 单次请求工具循环上限 | `5` |
| `REQUEST_TIMEOUT` | 单次模型请求超时（秒） | `120` |
| `MAX_RETRIES` | 瞬时模型错误的额外尝试次数 | `2` |
| `RETRY_BASE_DELAY` | 首次重试等待（秒） | `1` |
| `RETRY_MAX_DELAY` | 单次重试等待上限（秒） | `8` |
| `WORKSPACE_ROOT` | 本地工具工作区边界 | `.` |
| `COMMAND_TIMEOUT` | 本地命令超时时间（秒） | `120` |
| `MAX_COMMAND_OUTPUT_CHARS` | 返回模型的命令输出上限 | `20000` |
| `SANDBOX_BACKEND` | 可选 OS 沙箱能力门禁（`disabled` / `windows-sandbox`） | `disabled` |
| `LLM_PROVIDER` | 模型 Provider（`deepseek` / `claude` / `openai` / `ollama` / `vllm`） | `deepseek` |
| `LLM_MODEL` | 显式模型 ID（Claude/OpenAI/Ollama/vLLM 必填） | 随 Provider |
| `WEB_RUNTIME_MODEL_ALLOWLIST` | Web idle-only 的同 Provider 附加模型 ID JSON 数组（最多 15 个） | `[]` |
| `WEB_RATE_TABLE` | Web 可选 token 单价表 JSON，用于成本估算 | 未配置 |
| `AUDIT_LOG_ENABLED` | 是否启用元数据专用 JSONL 生命周期审计 | `false` |
| `AUDIT_LOG_MAX_BYTES` | 审计日志单文件轮转上限 | `1000000` |
