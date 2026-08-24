# Claude Code 官方文档对照审核（更新于 2026-08-23）

本审核把 Claude Code 当作成熟产品参考，不把 Neil Agent 改造成 Claude Code 的复制品。结论基于 Anthropic 官方的[项目指令](https://code.claude.com/docs/en/memory)、[权限](https://code.claude.com/docs/en/permissions)、[沙箱](https://code.claude.com/docs/en/sandboxing)、[会话](https://code.claude.com/docs/en/sessions)、[检查点](https://code.claude.com/docs/en/checkpointing)、[非交互模式](https://code.claude.com/docs/en/headless)和 [hooks](https://code.claude.com/docs/en/hooks) 文档。

## 总体结论

Neil Agent 的最小闭环已经具备清晰分层：模型层不直接执行工具，注册表只暴露固定定义，文件和 Git 写操作需要预览与批准，会话与项目指令都受工作区边界约束。它适合继续作为一个可学习、可测试的小型 Coding Agent，而不是提前引入任意 shell、插件市场或多 Agent 调度。

除终端 CLI 与 Textual 驾驶舱外，仓库现已包含本地 Web Workbench（`neil-agent-web`）、五类 LLM Provider 适配层，以及 Windows Sandbox 认证契约代码。三条运行入口通过 `host_runtime.py` 共享工具装配；Web 的沙箱注册、项目指令作用域、会话连续性和 Security Shield 投影现已与 CLI 对齐。

## 对照结果

| 领域 | Neil Agent 当前状态 | 审核判断 |
| --- | --- | --- |
| 项目指令 | 根到目标的 `AGENTS.md` 链，首次文件访问前按作用域刷新 | 与官方“上层启动加载、下层按访问加载”的核心思路一致 |
| 权限 | 只读文件/Git 直接执行；写入、检查、暂存和提交逐次预览批准 | 与官方分层权限方向一致，且权限由代码而非提示词执行 |
| 命令 | 不提供任意 shell，只提供固定检查与受限 Git；认证后可选 `run_command` | 对当前学习阶段比实现复杂 Bash 规则更安全 |
| 会话 | 严格本地快照、恢复、搜索、分页、导入导出和分支 | CLI 完整覆盖；Web 可新建、选择、恢复并在成功 turn 后原子保存 |
| 上下文 | 完整轮次裁剪、字符/token 双软预算、服务端 usage、显式压缩 | 请求前仍是软估算；最近成功回合保留服务端实测 |
| 可观察性 | 模型、工具、审批、计划和重试都有实时活动；TUI 驾驶舱与 Web 工作台 | 已达到可理解的执行轨迹，不暴露思维链 |
| 自动化 | 离线评测，以及一次性 `text`、`json`、`stream-json` | v1 默认只读；v2 以两阶段精确审批开放受限写操作 |
| Hooks | 类型化进程内 `before/after model/tool` 回调 | 支持审计、拒绝和有界上下文；有意不执行任意 shell |
| 浏览器 UI | 本地 loopback Web Workbench，逐工具审批与只读 Git review | 桌面工作台方向一致；无 PTY、无批量 Apply |
| OS 沙箱 | 不可变策略、认证契约、条件 `run_command`、fail-closed 诊断 | CLI/Web 同路径条件注册；真实 WSB 认证仍依赖专用 runner |
| 多 Provider | DeepSeek、Claude、OpenAI、Ollama、vLLM | 超出 Claude Code 单一生态；维护期见 provider 文档 |

## 已实施优化

1. `AGENTS.md` 提示段现在明确标记为非可信仓库上下文；当前用户明确请求优先，安全策略仍由工具代码强制。
2. `/permissions` 展示直接工具、逐次审批工具、工作区、敏感路径、命令与网络边界，并区分应用层白名单与 OS 沙箱状态。
3. `/branch [标题]` 复制完整消息、计划与最近检查到新 ID 并切换，原会话保持不变。
4. `/compact [关注点]` 支持最多 500 字符的摘要关注点；应用摘要前先创建“压缩前”会话副本，完整历史可以通过 `/resume` 恢复。
5. 自动化测试规模已显著扩大；当前离线 pytest 为 762 项通过，含 Web Workbench 契约与安全回归；23 项平台相关用例按条件跳过。
6. 新增 `-p/--print` 一次性入口；默认 v1 只读，`json` 和 `stream-json` 不混入终端装饰或思考内容，默认不保存会话。
7. 新增类型化生命周期 hooks：前置阶段可拒绝，`before_model` 可提供有界请求上下文，后置阶段只审计；回调异常默认关闭相关操作。
8. 依据 Provider 文档与字符比例调整 token 软估算，并明确实际请求与费用仍以服务端 `usage` 为准。
9. 完成 Windows Sandbox 契约、guest runner、认证 bundle 与条件 `run_command` 实现；普通开发机缺少 `wsb.exe` 时按设计跳过，不产生认证。
10. 接收并累加服务端 `usage`，在 `/context`、会话版本 3+、一次性结构化结果与 Web 快照中保留最近成功回合的实测；会话版本 4 以可选直接父 ID 记录本地分支谱系，版本 5 增加可选 Provider/模型绑定。
11. 用独立版本化夹具固定 v1/v2 的 `json` / `stream-json` 字段与错误代码；v2 通过 request/approve 两次运行、精确预览绑定和一次性消费开放受限操作，不改变 v1。
12. 增加可选的元数据 JSONL 审计 sink；它预检真实路径、限制单条与总大小并做单备份轮转，不记录正文或凭据。
13. 增加进程内多文件任务检查点；`/rewind-task` 全量预检并恢复多个路径，容量不足在写入前拒绝，进程内中途失败会回滚已应用路径。
14. 审计的大小检查、单备份轮转和追加现在由跨进程内核文件锁串行化；`/doctor` 可只读检查锁、大小、记录数与格式，不返回日志正文。
15. 新增 Textual 实时驾驶舱 Phase 0A–2B：执行 DAG、上下文断层图、Security Shield 与边界观察。
16. 新增本地 Web Workbench P0–P9：React 前端、Python 适配层、wheel 静态分发、bootstrap/CSRF/ticket 安全边界、实时 turn、逐工具审批、多轮会话恢复与受控模型切换。
17. 引入 `host_runtime.py`，统一 CLI、非交互与 Web 的工具注册与能力矩阵文档。
18. Web 会话切换受控制租约、revision 与 idle 状态保护；成功 turn 原子保存，失败/取消不写入，保存失败闭锁，跨 Provider/模型私有状态在发网前拒绝。
19. 完成 Textual Time Machine Phase 3A：按事件游标只读重建历史 DAG/指标，浏览脱敏会话分支、压缩与进程内任务检查点；默认不持久化事件，不重新调用模型或工具，也不混入恢复能力。
20. Web 可在控制租约、精确 revision、idle 且空会话门禁下选择操作方明确允许的同 Provider 模型；事务失败保持旧 worker，会话绑定阻止通过历史恢复绕过预检。

本轮实现后的自动化与显式真实验收结果见开发记录；常规测试和离线检查不调用真实付费 API，除非显式开启 smoke 或 eval 验收。

## 明确保留的差异

- Claude Code 把项目 `CLAUDE.md` 作为上下文而非安全配置。Neil Agent 仍把包裹后的项目段拼入系统字符串，这是当前多 Provider 接口的简化；低信任声明和代码权限边界降低了优先级混淆风险，但后续仍可把项目上下文改为独立消息块。
- Claude Code 的 `/export` 面向人类可读文本。Neil Agent 的 `/export` 仍是为安全导入设计的严格 JSON 信封；`-p --output-format json|stream-json` 才是脚本协议，两者语义必须持续区分。
- Claude Code 的检查点可以按对话持续恢复多文件状态。Neil Agent 按单次 Agent 回合恢复多文件正文，但仍只存在于本进程；Git 仍是跨进程和持久化回退的可靠机制。
- Claude Code 在 IDE/终端中提供持续的进程内会话。Neil Agent Web 为每个 turn 构造隔离 Agent 并从严格本地快照恢复；这保留了连续性，但没有把浏览器变成长驻 Agent 对象的直接宿主。
- Claude Code 同时使用权限规则和已投入执行的 OS 级沙箱。Neil Agent CLI/Web 已共用认证契约与条件 `run_command`；没有通过专用 Windows runner 认证的宿主仍不能声称 OS 隔离等价。

## 后续优先级

1. 在专用 Windows runner 完成三轮强制安全 workflow、独立 review 与运行时认证；随后评估 guest 产物导出与二次批准导入。
2. 在认证完成后设计受控 guest 产物导出与二次批准导入，继续保持调用前声明、hash/revision 绑定和失败回滚。
3. 维持 Time Machine Phase 3A 的只读边界；只有在审批、版本/哈希并发校验和失败原子性协议独立完成后，才评估 Phase 3B 安全恢复。

## 相关文档

- [`architecture.md`](architecture.md) — 总体分层（含 Web）
- [`host-runtime.md`](host-runtime.md) — 三入口能力矩阵与迁移状态
- [`web-workbench-development.md`](web-workbench-development.md) — Web 产品与协议
- [`provider-adapter-development.md`](provider-adapter-development.md) — 多 Provider 维护期说明
