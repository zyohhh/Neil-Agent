# Neil-Agent

A local coding agent built from scratch with Python and DeepSeek V4 Flash.

## 常用命令

```text
uv run neil-agent
uv run neil-agent-eval
```

Neil Agent 会在工作区内提供多轮对话、流式活动、受审批保护的文件和 Git 工具、项目指令、显式上下文压缩，以及可恢复的本地会话。

项目指令命令：

- `/instructions`：显示当前生效的 `AGENTS.md` 来源，不显示正文。
- `/reload-instructions`：不重启进程重新加载；失败时保留旧快照。
- `/init`：本地分析项目并预览根 `AGENTS.md` 初稿，批准后仅在文件不存在时创建。

会话命令：

- `/sessions [关键词]`：按标题、会话 ID 或最近请求搜索。
- `/rename-session <标题>`：重命名当前本地会话。
- `/resume <id>`：恢复指定会话。
- `/compact`：总结较早轮次并保留最近完整上下文。

默认评测完全离线，使用假模型和临时工作区，不读取 API Key。真实 DeepSeek 验收必须同时提供两个显式参数：

```text
uv run neil-agent-eval --real-deepseek --confirm-api-cost
```

真实验收会消耗 API 额度，只执行临时工作区内的只读工具、项目指令、压缩和恢复检查，不主动制造限流或网络故障。
