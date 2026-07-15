# Neil Agent Architecture

## 目标

Neil Agent 是一个运行在终端中的本地 Coding Agent。当前版本能够与 DeepSeek V4 Flash 多轮对话，在限定工作区内读取、搜索和修改文本文件，并在任何写操作前请求用户确认。

## 分层结构

```text
cli.py
  终端输入、流式展示、写入审批
    ↓
agent.py
  对话历史、工具循环、审批协调
    ↓
llm.py
  DeepSeek Anthropic API、流式事件、ToolCall 解析
    ↓
tools/registry.py
  工具注册、参数绑定、预览和执行分发
    ↓
tools/filesystem.py
  工作区受限的读取、搜索和原子写入
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

## 工具权限模型

工具注册分为两类：

- 直接执行：`list_directory`、`read_file`、`search_text`
- 必须审批：`write_file`、`replace_text`

审批工具必须同时注册预览函数。执行流程为：

```text
参数校验 → 生成 diff → 用户确认 → 执行 → ToolResult
```

没有明确批准时，注册表拒绝执行写工具。CLI 只接受 `y` 或 `yes`，其他输入均视为拒绝。diff 包含基于修改前后内容生成的 `Change-ID`；执行前注册表会重新生成预览，如果与用户批准的版本不一致，则要求重新确认。

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
| `WORKSPACE_ROOT` | 文件工具工作区边界 | `.` |
