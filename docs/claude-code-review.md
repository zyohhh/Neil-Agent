# Claude Code 官方文档对照审核（更新于 2026-07-23）

本审核把 Claude Code 当作成熟产品参考，不把 Neil Agent 改造成 Claude Code 的复制品。结论基于 Anthropic 官方的[项目指令](https://code.claude.com/docs/en/memory)、[权限](https://code.claude.com/docs/en/permissions)、[沙箱](https://code.claude.com/docs/en/sandboxing)、[会话](https://code.claude.com/docs/en/sessions)、[检查点](https://code.claude.com/docs/en/checkpointing)、[非交互模式](https://code.claude.com/docs/en/headless)和 [hooks](https://code.claude.com/docs/en/hooks) 文档。

## 总体结论

Neil Agent 的最小闭环已经具备清晰分层：模型层不直接执行工具，注册表只暴露固定定义，文件和 Git 写操作需要预览与批准，会话与项目指令都受工作区边界约束。它适合继续作为一个可学习、可测试的小型 Coding Agent，而不是提前引入任意 shell、插件市场或多 Agent 调度。

本轮发现的高价值缺口已经修复：文件工具按目标目录懒加载指令；仓库指令明确是低于当前用户请求的非可信上下文；新增 `/permissions` 说明真实强制边界；会话可以 `/branch`；`/compact [关注点]` 会保留一个压缩前完整会话副本。

## 对照结果

| 领域 | Neil Agent 当前状态 | 审核判断 |
| --- | --- | --- |
| 项目指令 | 根到目标的 `AGENTS.md` 链，首次文件访问前按作用域刷新 | 与官方“上层启动加载、下层按访问加载”的核心思路一致 |
| 权限 | 只读文件/Git 直接执行；写入、检查、暂存和提交逐次预览批准 | 与官方分层权限方向一致，且权限由代码而非提示词执行 |
| 命令 | 不提供任意 shell，只提供固定检查与受限 Git | 对当前学习阶段比实现复杂 Bash 规则更安全 |
| 会话 | 严格本地快照、恢复、搜索、分页、导入导出和分支 | 已覆盖名称、恢复和分支的主要生命周期 |
| 上下文 | 完整轮次裁剪、字符/token 双软预算、服务端 usage、显式压缩 | 请求前仍是软估算；最近成功回合保留服务端实测 |
| 可观察性 | 模型、工具、审批、计划和重试都有实时活动 | 已达到可理解的执行轨迹，不暴露思维链 |
| 自动化 | 离线评测，以及一次性 `text`、`json`、`stream-json` | v1 默认只读；v2 以两阶段精确审批开放受限写操作 |
| Hooks | 类型化进程内 `before/after model/tool` 回调 | 支持审计、拒绝和有界上下文；有意不执行任意 shell |

## 已实施优化

1. `AGENTS.md` 提示段现在明确标记为非可信仓库上下文；当前用户明确请求优先，安全策略仍由工具代码强制。
2. `/permissions` 展示直接工具、逐次审批工具、工作区、敏感路径、命令与网络边界，并明确说明当前没有 OS 级子进程沙箱。
3. `/branch [标题]` 复制完整消息、计划与最近检查到新 ID 并切换，原会话保持不变。
4. `/compact [关注点]` 支持最多 500 字符的摘要关注点；应用摘要前先创建“压缩前”会话副本，完整历史可以通过 `/resume` 恢复。
5. 增加对应单元与 CLI 回归测试；当前结果为 140 项通过、1 项条件跳过。
6. 新增 `-p/--print` 一次性入口；默认 v1 只读，`json` 和 `stream-json` 不混入终端装饰或思考内容，默认不保存会话。
7. 新增类型化生命周期 hooks：前置阶段可拒绝，`before_model` 可提供有界请求上下文，后置阶段只审计；回调异常默认关闭相关操作。
8. 依据 DeepSeek 官方字符比例调整 token 软估算，并明确实际请求与费用仍以服务端 `usage` 为准。
9. 完成 Windows AppContainer/Windows Sandbox 与 Linux bubblewrap 的初步隔离评估；通用命令继续保持关闭。
10. 接收并累加 DeepSeek `usage`，在 `/context`、会话版本 3 和一次性结构化结果中保留最近成功回合的服务端实测。
11. 用独立版本化夹具固定 v1/v2 的 `json` / `stream-json` 字段与错误代码；v2 通过 request/approve 两次运行、精确预览绑定和一次性消费开放受限操作，不改变 v1。
12. 增加可选的元数据 JSONL 审计 sink；它预检真实路径、限制单条与总大小并做单备份轮转，不记录正文或凭据。
13. 增加 `/rewind-file` 最小文件检查点，只恢复本进程最新一次 Agent 工具编辑，预览并批准后仍重新检查路径与内容。
14. 审计的大小检查、单备份轮转和追加现在由跨进程内核文件锁串行化；`/doctor` 可只读检查锁、大小、记录数与格式，不返回日志正文。

本轮实现后的自动化结果见开发记录；离线检查不调用真实 DeepSeek API。

## 明确保留的差异

- Claude Code 把项目 `CLAUDE.md` 作为上下文而非安全配置。Neil Agent 仍把包裹后的项目段拼入系统字符串，这是当前 DeepSeek/LLM 接口的简化；新增的低信任声明和代码权限边界降低了优先级混淆风险，但后续仍可把项目上下文改为独立消息块。
- Claude Code 的 `/export` 面向人类可读文本。Neil Agent 的 `/export` 仍是为安全导入设计的严格 JSON 信封；新增的 `-p --output-format json|stream-json` 才是脚本协议，两者语义必须持续区分。
- Claude Code 的检查点可以按对话恢复多文件状态。Neil Agent 只有本进程、单步后进先出的文件内容恢复，不持久化权限/目录元数据，也不覆盖外部程序修改；Git 仍是跨进程和多文件回退的可靠机制。
- Claude Code 同时使用权限规则和 OS 级沙箱。Neil Agent 在原生 Windows 上只有工具白名单、路径验证、安全环境和逐次审批，不能声称等价于 OS 沙箱。

## 后续优先级

1. 使用真实 DeepSeek API 手工核对 `usage`、默认只读 v1、显式审批 v2 和会话保存；自动化继续不消耗额度。
2. 若扩展文件检查点，优先定义多文件任务边界、容量失败行为和持久化威胁模型；在此之前维持单步内存恢复并以 Git 作为可靠回退。
3. 若继续推进通用命令，按 [`sandbox-assessment.md`](sandbox-assessment.md) 实现至少一个 fail-closed 平台后端并完成逃逸测试；在此之前保持固定 allowlist。
