# Neil-Agent

A local coding agent built from scratch with Python and DeepSeek V4 Flash.

## 常用命令

```text
uv run neil-agent
uv run neil-agent-eval
```

Neil Agent 会在工作区内提供多轮对话、流式活动、受审批保护的文件和 Git 工具、项目指令、显式上下文压缩，以及可恢复的本地会话。

启动后首先显示一个响应式工作台，集中展示当前模型、思考模式、工作区、会话、工具审批数量和项目指令状态；它不会显示 API Key 或 `AGENTS.md` 正文。

项目指令命令：

- `/instructions`：显示当前文件作用域生效的 `AGENTS.md` 来源，不显示正文。
- `/reload-instructions`：不重启进程重新加载；失败时保留旧快照。
- `/init`：本地分析项目并预览根 `AGENTS.md` 初稿，批准后仅在文件不存在时创建。

会话命令：

- `/sessions [选项] [关键词]`：本地分页、排序、搜索，并按计划/检查失败/压缩状态筛选。
- `/rename-session <标题>`：重命名当前本地会话。
- `/resume <id>`：恢复指定会话。
- `/branch [标题]`：复制当前会话并切换到新 ID，原会话保持不变。
- `/export [id]`：预览后导出当前或指定会话。
- `/import <文件名>`：预览后导入 `.neil-agent/exports/` 中的严格版本化文件。
- `/compact [关注点]`：总结较早轮次、保留最近完整上下文，并保存压缩前会话副本。
- `/context`：区分下一次请求的本地软预算估算与最近一次服务端实测 `usage`。
- `/cockpit`：显示任务、上下文、安全边界和工作区信号的只读基础可视化，不调用模型。
- `/rewind-file`：预览并恢复本进程最近一次 Agent 文件工具编辑；外部变化会拒绝恢复。
- `/permissions`：显示真正由代码执行的工具审批和工作区边界。

Claude Code 官方文档对照结论与保留差异见 [`docs/claude-code-review.md`](docs/claude-code-review.md)。
高级 DAG、上下文断层图、安全盾、时间机器和仓库热力图的增量路线见
[`docs/visualization-development.md`](docs/visualization-development.md)。

## 一次性非交互运行

`-p/--print` 执行一个 prompt 后退出，适合脚本和 CI。默认协议 v1 只向模型暴露文件读取、搜索以及只读 Git 工具。

```text
uv run neil-agent -p "概括当前项目结构"
uv run neil-agent -p "检查工作区状态" --output-format json
uv run neil-agent -p "检查工作区状态" --output-format stream-json
```

- 默认协议 v1 始终只读；不会接受审批 ID，也不提供写文件、质量检查、暂存或提交。
- 显式协议 v2 提供两次运行的审批流程：`request` 只生成精确预览，`approve` 使用一个未过期请求 ID 执行完全匹配的单项操作。
- v2 审批模式仅支持 `json` / `stream-json`，审批请求有效期为 15 分钟；工作区、prompt、项目指令、工具参数或当前预览变化都会拒绝旧请求。
- `text`：标准输出只有最终文本流；错误写入标准错误。
- `json`：标准输出只有一行最终 JSON，包含协议版本、结果、活动、服务端 `usage` 与退出状态。
- `stream-json`：标准输出为 JSONL，依次发送 `session_start`、活动、文本增量和最终结果或错误。
- 结构化错误包含稳定的 `error_code`；协议版本 1、2 的字段契约分别由测试夹具固定。
- 成功、运行错误、参数/配置错误、等待审批和用户中断分别使用退出码 `0`、`1`、`2`、`3`、`130`。
- 一次性运行默认不保存；显式添加 `--save-session` 才写入工作区会话目录。
- 所有结构化格式都不会输出思考内容。完整协议见 [`docs/non-interactive.md`](docs/non-interactive.md)。

非交互写入示例：

```text
uv run neil-agent -p "更新版本号" --protocol-version 2 --permission-mode request --output-format json
uv run neil-agent -p "更新版本号" --protocol-version 2 --permission-mode approve --approval-id <request-id> --output-format json
```

如需本地生命周期审计，可设置 `AUDIT_LOG_ENABLED=true`。日志写入
`.neil-agent/audit/events.jsonl`，只记录有界元数据，不记录 prompt、thinking、工具参数/正文或 API Key；`AUDIT_LOG_MAX_BYTES` 控制单文件轮转上限。

离线评测支持单场景和 JSON 报告，也可由 `run_quality_check(eval)` 在受审批的固定命令中运行：

```text
uv run neil-agent-eval --task root-project-instructions --format json
```

默认评测完全离线，使用假模型和临时工作区，不读取 API Key。真实 DeepSeek 验收必须同时提供两个显式参数：

```text
uv run neil-agent-eval --real-deepseek --confirm-api-cost
```

真实验收会消耗 API 额度，只执行临时工作区内的只读工具、项目指令、压缩和恢复检查，不主动制造限流或网络故障。
