# Neil Agent Architecture

## 目标

Neil Agent 是一个运行在终端中的本地 Coding Agent。当前版本能够与 DeepSeek V4 Flash 多轮对话，在限定工作区内读取、搜索和修改文本文件，运行固定的项目质量检查，并在批准后暂存明确文件和创建本地 Git 提交。

## 分层结构

```text
cli.py
  终端输入、流式展示、高风险操作审批
    ↓
agent.py
  对话历史、工具循环、审批协调、修改后验证工作流
    ↓
llm.py
  DeepSeek Anthropic API、流式事件、ToolCall 解析
    ↓
tools/registry.py
  工具注册、参数绑定、预览和执行分发
    ├→ tools/filesystem.py
    │    工作区受限的读取、搜索和原子写入
    └→ tools/shell.py
         固定质量检查、只读 Git、本地暂存和提交、子进程安全边界
```

`schemas.py` 为各层提供消息和工具数据结构，`errors.py` 提供统一但分层的用户可见异常，`config.py` 负责从环境变量和 `.env` 加载配置。

## 对话和工具循环

1. CLI 把用户输入交给 `Agent.stream_chat()`。
2. Agent 将最近的对话历史和工具定义发送给 LLM。
3. LLM 流式返回文本，或在结束事件中返回一个或多个 `ToolCall`。
4. Agent 执行工具，将 `ToolResult` 作为用户消息返回模型。
5. 模型可以继续调用工具，直到生成最终回答或达到 `MAX_TOOL_ROUNDS`。
6. 只有整个请求成功完成，Agent 才将本轮消息写入历史。

思考模式发生工具调用时，LLM 层会保留 Anthropic thinking block，并在后续工具结果请求中完整回传。

当注册表同时提供文件写入和质量检查工具时，Agent 会在用户系统提示词后追加不可配置的本地工具工作流。文件修改结果会提醒模型选择合适的质量检查；命令结果固定返回 `Command`、`Working directory`、`Exit code` 和 `Output`，最终回答据此汇总验证结果。

## 工具权限模型

工具注册分为两类：

- 直接执行：`list_directory`、`read_file`、`search_text`、`git_status`、`git_diff`
- 必须审批：`write_file`、`replace_text`、`run_quality_check`、`git_stage`、`git_commit`

审批工具必须同时注册预览函数。执行流程为：

```text
参数校验 → 生成操作预览 → 用户确认 → 执行 → ToolResult
```

没有明确批准时，注册表拒绝执行高风险工具。CLI 只接受 `y` 或 `yes`，其他输入均视为拒绝。文件 diff 包含基于修改前后内容生成的 `Change-ID`；执行前注册表会重新生成预览，如果与用户批准的版本不一致，则要求重新确认。质量检查预览则显示精确命令、工作目录和超时时间。

## 文件安全边界

- 所有路径解析后必须位于 `WORKSPACE_ROOT`。
- 防止利用 `..`、绝对路径或符号链接逃出工作区。
- 屏蔽 `.env`、`.git`、`.venv`、缓存目录和常见私钥文件。
- 单文件读取和写入上限为 1 MB。
- 搜索结果最多返回 100 条。
- diff 预览最多显示 20,000 字符。
- 过期的 diff 审批不能用于已经发生外部变化的文件。
- 精确替换要求实际匹配数量等于 `expected_replacements`。
- 写入使用同目录临时文件和 `os.replace`；替换失败时原文件保持不变。

## 命令安全边界

- `run_quality_check` 只允许 `pytest`、`ruff`、`mypy`，调用参数由程序固定拼装。
- `git_status` 固定读取简洁状态；`git_diff` 只允许切换是否查看暂存区，并禁用 external diff 与 textconv。
- Git 命令禁用 fsmonitor、分页器和可选锁，避免执行扩展程序或产生非必要写入。
- 不接收任意可执行文件、命令参数或 Shell 字符串，子进程始终使用 `shell=False`。
- 命令工作目录固定为解析后的 `WORKSPACE_ROOT`，标准输入设为空，避免命令等待交互。
- 子进程环境采用最小白名单，不继承 API Key、访问令牌等敏感变量。
- 命令受 `COMMAND_TIMEOUT` 约束，返回内容受 `MAX_COMMAND_OUTPUT_CHARS` 约束。
- 非零退出码和超时会作为 `ToolResult(is_error=True)` 返回模型，而不是绕过工具错误边界。

## 本地 Git 写入边界

- `git_stage` 只接受最多 50 个明确的工作区相对文件路径，并使用 literal pathspec 阻止 Git pathspec magic。
- 不允许暂存整个工作区、目录、越界路径、受保护目录、`.env` 或常见私钥文件。
- 暂存预览包含状态、已暂存和未暂存 diff，以及未跟踪文本文件内容；Git clean filter 可能运行外部程序，因此必须审批。
- 暂存预览的 `Change-ID` 同时覆盖 Git diff 和完整文件内容，预览后发生任何相关变化都会要求重新确认。
- `git_commit` 只从当前暂存区创建本地提交，消息必须是 1–200 字符的单行文本。
- 提交预览显示暂存区统计和 diff；超长 diff 会截断，但 `Change-ID` 覆盖完整内容；空暂存区不能提交。
- 提交时禁用 hooks 和 GPG 签名，不提供 amend、空提交、远程操作或推送入口。

## 异常边界

```text
NeilAgentError
├── AgentError   Agent 循环和编排错误
├── LLMError     DeepSeek/API 和模型响应错误
└── ToolError    工具参数、权限和文件操作错误
```

工具执行错误通常会转换为 `ToolResult(is_error=True)` 返回模型；无法在工具内部处理的 Agent 或 LLM 错误由 CLI 捕获并展示。

## 关键配置

| 环境变量 | 作用 | 默认值 |
| --- | --- | --- |
| `DEEPSEEK_MODEL` | 模型名称 | `deepseek-v4-flash` |
| `THINKING_ENABLED` | 是否启用思考模式 | `false` |
| `MAX_ROUNDS` | 对话历史窗口 | `20` |
| `MAX_TOOL_ROUNDS` | 单次请求工具循环上限 | `5` |
| `WORKSPACE_ROOT` | 本地工具工作区边界 | `.` |
| `COMMAND_TIMEOUT` | 本地命令超时时间（秒） | `120` |
| `MAX_COMMAND_OUTPUT_CHARS` | 返回模型的命令输出上限 | `20000` |
