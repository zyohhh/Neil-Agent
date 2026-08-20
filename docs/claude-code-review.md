# Claude Code 官方文档对照审核（更新于 2026-08-20）

本审核把 Claude Code 当作成熟产品参考，不把 Neil Agent 改造成 Claude Code 的复制品。结论基于 Anthropic 官方的[项目指令](https://code.claude.com/docs/en/memory)、[权限](https://code.claude.com/docs/en/permissions)、[沙箱](https://code.claude.com/docs/en/sandboxing)、[会话](https://code.claude.com/docs/en/sessions)、[检查点](https://code.claude.com/docs/en/checkpointing)、[非交互模式](https://code.claude.com/docs/en/headless)和 [hooks](https://code.claude.com/docs/en/hooks) 文档。

## 总体结论

Neil Agent 的最小闭环已经具备清晰分层：模型层不直接执行工具，注册表只暴露固定定义，文件和 Git 写操作需要预览与批准，会话与项目指令都受工作区边界约束。它适合继续作为一个可学习、可测试的小型 Coding Agent，而不是提前引入任意 shell、插件市场或多 Agent 调度。

除终端 CLI 与 Textual 驾驶舱外，仓库现已包含本地 Web Workbench（`neil-agent-web`）、五类 LLM Provider 适配层，以及 Windows Sandbox 认证契约代码。三条运行入口通过 `host_runtime.py` 共享工具装配，但 Web 在会话连续性、沙箱注册和安全投影上仍与 CLI 存在已知差距。

## 对照结果

| 领域 | Neil Agent 当前状态 | 审核判断 |
| --- | --- | --- |
| 项目指令 | 根到目标的 `AGENTS.md` 链，首次文件访问前按作用域刷新 | 与官方“上层启动加载、下层按访问加载”的核心思路一致 |
| 权限 | 只读文件/Git 直接执行；写入、检查、暂存和提交逐次预览批准 | 与官方分层权限方向一致，且权限由代码而非提示词执行 |
| 命令 | 不提供任意 shell，只提供固定检查与受限 Git；认证后可选 `run_command` | 对当前学习阶段比实现复杂 Bash 规则更安全 |
| 会话 | 严格本地快照、恢复、搜索、分页、导入导出和分支 | CLI 已覆盖；Web 仅只读列表，尚未多轮恢复 |
| 上下文 | 完整轮次裁剪、字符/token 双软预算、服务端 usage、显式压缩 | 请求前仍是软估算；最近成功回合保留服务端实测 |
| 可观察性 | 模型、工具、审批、计划和重试都有实时活动；TUI 驾驶舱与 Web 工作台 | 已达到可理解的执行轨迹，不暴露思维链 |
| 自动化 | 离线评测，以及一次性 `text`、`json`、`stream-json` | v1 默认只读；v2 以两阶段精确审批开放受限写操作 |
| Hooks | 类型化进程内 `before/after model/tool` 回调 | 支持审计、拒绝和有界上下文；有意不执行任意 shell |
| 浏览器 UI | 本地 loopback Web Workbench，逐工具审批与只读 Git review | 桌面工作台方向一致；无 PTY、无批量 Apply |
| OS 沙箱 | 不可变策略、认证契约、条件 `run_command`、fail-closed 诊断 | 代码已接入；真实 WSB 认证依赖专用 runner；Web 尚未注册沙箱工具 |
| 多 Provider | DeepSeek、Claude、OpenAI、Ollama、vLLM | 超出 Claude Code 单一生态；维护期见 provider 文档 |

## 已实施优化

1. `AGENTS.md` 提示段现在明确标记为非可信仓库上下文；当前用户明确请求优先，安全策略仍由工具代码强制。
2. `/permissions` 展示直接工具、逐次审批工具、工作区、敏感路径、命令与网络边界，并区分应用层白名单与 OS 沙箱状态。
3. `/branch [标题]` 复制完整消息、计划与最近检查到新 ID 并切换，原会话保持不变。
4. `/compact [关注点]` 支持最多 500 字符的摘要关注点；应用摘要前先创建“压缩前”会话副本，完整历史可以通过 `/resume` 恢复。
5. 自动化测试规模已显著扩大；离线 pytest 约 750 项，含 Web Workbench 契约与安全回归；平台相关用例按条件跳过。
6. 新增 `-p/--print` 一次性入口；默认 v1 只读，`json` 和 `stream-json` 不混入终端装饰或思考内容，默认不保存会话。
7. 新增类型化生命周期 hooks：前置阶段可拒绝，`before_model` 可提供有界请求上下文，后置阶段只审计；回调异常默认关闭相关操作。
8. 依据 Provider 文档与字符比例调整 token 软估算，并明确实际请求与费用仍以服务端 `usage` 为准。
9. 完成 Windows Sandbox 契约、guest runner、认证 bundle 与条件 `run_command` 实现；普通开发机缺少 `wsb.exe` 时按设计跳过，不产生认证。
10. 接收并累加服务端 `usage`，在 `/context`、会话版本 3、一次性结构化结果与 Web 快照中保留最近成功回合的实测。
11. 用独立版本化夹具固定 v1/v2 的 `json` / `stream-json` 字段与错误代码；v2 通过 request/approve 两次运行、精确预览绑定和一次性消费开放受限操作，不改变 v1。
12. 增加可选的元数据 JSONL 审计 sink；它预检真实路径、限制单条与总大小并做单备份轮转，不记录正文或凭据。
13. 增加进程内多文件任务检查点；`/rewind-task` 全量预检并恢复多个路径，容量不足在写入前拒绝，进程内中途失败会回滚已应用路径。
14. 审计的大小检查、单备份轮转和追加现在由跨进程内核文件锁串行化；`/doctor` 可只读检查锁、大小、记录数与格式，不返回日志正文。
15. 新增 Textual 实时驾驶舱 Phase 0A–2B：执行 DAG、上下文断层图、Security Shield 与边界观察。
16. 新增本地 Web Workbench P0–P7：React 前端、Python 适配层、wheel 静态分发、bootstrap/CSRF/ticket 安全边界、实时 turn 与逐工具审批。
17. 引入 `host_runtime.py`，统一 CLI、非交互与 Web 的工具注册与能力矩阵文档。

本轮实现后的自动化与显式真实验收结果见开发记录；常规测试和离线检查不调用真实付费 API，除非显式开启 smoke 或 eval 验收。

## 明确保留的差异

- Claude Code 把项目 `CLAUDE.md` 作为上下文而非安全配置。Neil Agent 仍把包裹后的项目段拼入系统字符串，这是当前多 Provider 接口的简化；低信任声明和代码权限边界降低了优先级混淆风险，但后续仍可把项目上下文改为独立消息块。
- Claude Code 的 `/export` 面向人类可读文本。Neil Agent 的 `/export` 仍是为安全导入设计的严格 JSON 信封；`-p --output-format json|stream-json` 才是脚本协议，两者语义必须持续区分。
- Claude Code 的检查点可以按对话持续恢复多文件状态。Neil Agent 按单次 Agent 回合恢复多文件正文，但仍只存在于本进程；Git 仍是跨进程和持久化回退的可靠机制。
- Claude Code 在 IDE/终端中提供持续会话。Neil Agent Web Workbench 当前每轮新建 `Agent`，左侧会话列表为只读投影，尚未 `select_session` 恢复历史。
- Claude Code 同时使用权限规则和已投入执行的 OS 级沙箱。Neil Agent 已有认证契约与条件 `run_command`，但 Web 路径尚未注册沙箱工具；没有通过认证的宿主不能声称 OS 隔离等价。

## 后续优先级

1. 对齐 Web 与 CLI 的运行时差距：沙箱工具、`instruction_target` 作用域、会话 load/save、共享安全投影。
2. 在专用 Windows runner 完成三轮强制安全 workflow、独立 review 与运行时认证；随后评估 guest 产物导出与二次批准导入。
3. 可视化 Phase 3A：Time Machine 只读回放（事件与会话检查点浏览，不重新调用模型）。

## 相关文档

- [`architecture.md`](architecture.md) — 总体分层（含 Web）
- [`host-runtime.md`](host-runtime.md) — 三入口能力矩阵与迁移状态
- [`web-workbench-development.md`](web-workbench-development.md) — Web 产品与协议
- [`provider-adapter-development.md`](provider-adapter-development.md) — 多 Provider 维护期说明
