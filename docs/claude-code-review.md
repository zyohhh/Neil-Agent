# Claude Code 官方文档对照审核（2026-07-22）

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
| 上下文 | 完整轮次裁剪、字符/token 双软预算、显式压缩 | 结构安全；token 仍是估算而非模型 tokenizer |
| 可观察性 | 模型、工具、审批、计划和重试都有实时活动 | 已达到可理解的执行轨迹，不暴露思维链 |
| 自动化 | 离线评测支持筛选、JSON 和稳定耗时 | 适合本地检查；尚不是完整的非交互 Agent 事件协议 |

## 已实施优化

1. `AGENTS.md` 提示段现在明确标记为非可信仓库上下文；当前用户明确请求优先，安全策略仍由工具代码强制。
2. `/permissions` 展示直接工具、逐次审批工具、工作区、敏感路径、命令与网络边界，并明确说明当前没有 OS 级子进程沙箱。
3. `/branch [标题]` 复制完整消息、计划与最近检查到新 ID 并切换，原会话保持不变。
4. `/compact [关注点]` 支持最多 500 字符的摘要关注点；应用摘要前先创建“压缩前”会话副本，完整历史可以通过 `/resume` 恢复。
5. 增加对应单元与 CLI 回归测试；当前结果为 140 项通过、1 项条件跳过。

## 明确保留的差异

- Claude Code 把项目 `CLAUDE.md` 作为上下文而非安全配置。Neil Agent 仍把包裹后的项目段拼入系统字符串，这是当前 DeepSeek/LLM 接口的简化；新增的低信任声明和代码权限边界降低了优先级混淆风险，但后续仍可把项目上下文改为独立消息块。
- Claude Code 的 `/export` 面向人类可读文本，结构化脚本使用 JSON/stream JSON。Neil Agent 当前 `/export` 是为安全导入设计的严格 JSON 信封，语义不同，文档必须持续写清楚。
- Claude Code 的检查点能够恢复直接编辑产生的文件状态。Neil Agent 目前只保留对话分支与压缩前副本；Git 仍是代码回退的唯一可靠机制。
- Claude Code 同时使用权限规则和 OS 级沙箱。Neil Agent 在原生 Windows 上只有工具白名单、路径验证、安全环境和逐次审批，不能声称等价于 OS 沙箱。

## 后续优先级

1. 先定义非交互 `text`、`json`、`stream-json` 事件协议，再实现一次性 prompt 入口；结构化事件应包含 session ID、活动、工具结果和错误，但默认不含思考内容。
2. 设计类型化生命周期 hooks，只允许已注册的本地程序或 Python 回调，优先支持审计与拒绝，不直接开放任意 shell 字符串。
3. 调研 DeepSeek 官方 tokenizer 或 token 计数接口；不可用时继续显示“估算”，不能将其用作精确费用数据。
4. 如果未来开放通用命令，必须先设计子进程 OS 沙箱和网络隔离；在此之前保持固定 allowlist。
5. 在独立阶段设计文件编辑检查点，并明确它不能替代 Git，也不能可靠恢复外部程序、符号链接或并发进程的修改。
