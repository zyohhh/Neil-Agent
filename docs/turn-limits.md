# 回合累计预算与工具停滞检测

这些限制通过 `host_runtime.build_agent()` 应用于 CLI、实时 TUI、Web、非交互和只读子任务，不改变工具面、审批策略或工作区边界。每次用户回合创建一个 `TurnBudget`；只读子任务继承同一账本，不按子任务重新获得额度。显式 `/compact` 的一次完整摘要操作也使用一个账本。

## 配置

| 环境变量 / Settings 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `TURN_MAX_MODEL_ATTEMPTS` / `turn_max_model_attempts` | 32 | 主任务、子任务和 SDK 重试共享的请求尝试额度 |
| `TURN_MAX_REPORTED_TOKENS` / `turn_max_reported_tokens` | 100000 | 累计已报告 token 的停止阈值 |
| `TURN_TIMEOUT_SECONDS` / `turn_timeout_seconds` | 600 秒 | 回合协作 deadline；继承更早的宿主 deadline |
| `REPEATED_TOOL_RESULT_LIMIT` / `repeated_tool_result_limit` | 3 | 没有新增成功观察时，同一工具/参数/结果的重复阈值；同批重复也检查 |
| `FAILED_TOOL_ROUND_LIMIT` / `failed_tool_round_limit` | 3 | 连续整轮工具均失败或被拒绝的阈值 |

原有 `MAX_TOOL_ROUNDS`、单次请求输出上限和上下文预算仍单独生效。子任务自身的上下文、轮次、调用次数和超时限制继续保留。环境变量使用有效正数；Python Settings 可将累计 token 阈值设为 `None`，请求次数和时限仍受限。

## 请求与 usage 的记账

Agent 在模型调用前预留一次尝试。内置 SDK 适配器领取该预留，每次重试另扣一次，避免 Agent 与适配器重复计数。包装模型、子任务和旧版 facade 仍沿同一上下文。限额耗尽时不会等待一次无额度的重试，也不会发送下一次请求；工具响应还需要后续模型请求完成回合时，会先检查额度，再进入工具审批与执行。

默认 SDK 客户端关闭内建重试；注入的真实 SDK 客户端通过副本关闭内建重试，不改调用方原对象，避免在运行时账本下面额外发送重试。两类 SDK 均以本地 HTTP mock transport 验证实际请求次数。

`model_attempts` 是保守的模型调用尝试数：失败尝试也计入，部分前置验证可能在实际发网前失败；它不是网络抓包次数。自定义 ChatModel 默认按逻辑调用扣一次，内部自行重试的实现必须接入 `model_attempt_scope()` 才能逐次计数。可信 Python 扩展不能依赖此协作机制获得 OS 级隔离。

只有 SDK 实际提供有效 input/output 计数时才记录 usage。失败响应仍可能有可用 usage，流中断或缺少计数则记为未报告；不将缺失计数填成测得的 0。`Agent.last_run_budget` 在成功和失败后保留安全快照，含尝试数、未报告数、已报告 subtotal、完整性及停止原因。原有 `Agent.last_usage` 继续表示该 Agent 自身成功逻辑响应的 usage；共享账本包含父子调用和重试，二者口径不同。

累计 token 使用现有 `TokenUsage` 分类口径，仅在下一次请求/工具后续门禁检查阈值。最后一次响应可以跨过阈值，失败请求或缺失 usage 的费用不可推断，因此这不是精确费用硬上限。SDK 连接/读取超时会压缩到剩余 deadline，已建立流和宿主命令响应协作中断；非流式底层 I/O 仍受 SDK 超时语义影响，不能宣称任意阻塞代码在绝对时刻被强杀。

回合结束事件追加 allowlist 内的 `shared_model_attempts`、`shared_unreported_attempts`、`shared_usage_complete`，以及可用时的 `shared_reported_tokens` 和 `budget_stop_reason`。这些是共享账本的累计值，不应再与子任务结束事件相加。提示、参数和输出正文不进入这些元数据。

## 停滞判断

`ToolLoopGuard` 每回合独立创建，只保存规范化工具名/参数与结果的哈希、重复次数；忽略 tool_call_id 和助手分析文字。读取会重新执行，不缓存旧结果。结果发生变化、出现新的成功观察或项目指令更新时允许继续；新的成功审批操作会清理旧读观察，避免正常修改后复查被误判。连续全失败回合另行计数。

阈值命中后以 `AgentLoopDetected` 结束失败回合，不调用额外模型解释或自动压缩。判断在当前工具批次执行后进行，不能撤销本批已产生的副作用；持久执行记录继续提供失败核对依据。观察哈希与单批记录各限 256 项。它是可配置的防循环启发式，不能证明任务在语义上没有进展，也不保证检测所有循环。模型轮数与累计预算仍是独立边界。

主要回归见 `tests/test_turn_budget.py`、`tests/test_loop_guard.py` 和 `tests/test_execution_budget.py`：覆盖 SDK 重试、父子共享、缺失 usage、失败终态、deadline 中断、审批前拒绝、只读循环、内容变化和新操作后的复查。
