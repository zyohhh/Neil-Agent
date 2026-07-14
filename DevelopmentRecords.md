# Neil Agent 开发记录

## 项目信息

- 项目名称：Neil Agent
- 项目类型：本地 Coding Agent / AI 编程助手
- 开发语言：Python
- 开发环境：VS Code
- 项目负责人：Neil
- 文档创建时间：2026 年 7 月
- GitHub 仓库：https://github.com/zyohhh/Neil-Agent
- 当前版本：v0.1.0-dev
- 当前状态：只读工具调用闭环已完成，下一步开发受控文件修改

## 文件职责

| 文件 | 当前职责 |
| --- | --- |
| `cli.py` | 通过可注入 Console 接收输入、展示流式文本并处理终端命令 |
| `agent.py` | 管理多轮历史，以及“模型 → 工具 → 模型”的受限循环 |
| `llm.py` | 封装 DeepSeek 请求、流式事件、工具定义和工具调用解析 |
| `errors.py` | 定义应用、Agent、LLM 和工具层的用户可见异常层级 |
| `schemas.py` | 定义消息、思考内容、工具定义、调用、结果和模型响应结构 |
| `config.py` | 从环境变量或 `.env` 读取并校验 API Key、模型、系统提示词、思考模式和运行限制 |
| `registry.py` | 注册工具、校验调用参数、分发执行并统一返回结果 |
| `filesystem.py` | 在工作区边界内列目录、读取 UTF-8 文件和搜索文本 |
| `shell.py` | 预留：受控执行测试、Git 和其他 Shell 命令 |
| `__init__.py` | 保存包版本，并把命令行入口转发给 `cli.py` |

## 第一阶段目标

完成一个不包含工具调用的最小聊天闭环：

```text
用户输入
   ↓
cli.py 处理终端交互
   ↓
agent.py 组织最近的多轮消息
   ↓
llm.py 调用 DeepSeek 并返回流式文本
   ↓
cli.py 实时展示回答
   ↓
agent.py 在回答完整结束后保存本轮消息
```

## 2026-07-13：第一个可用版本

### 已完成

- 使用 `pydantic-settings` 从 `.env` 加载配置。
- 默认模型设置为 `deepseek-v4-flash`。
- 使用 DeepSeek Anthropic 兼容地址 `https://api.deepseek.com/anthropic`。
- 使用 `SecretStr` 保存 API Key，避免配置对象意外输出明文密钥。
- 定义不可变的 `Message`、`ToolCall` 和 `ToolResult` 数据结构。
- 实现普通模型请求 `LLMClient.complete()`。
- 实现流式模型请求 `LLMClient.stream()`。
- 将常见的鉴权、限流、超时、连接和 HTTP 错误转换为中文提示。
- 实现 `Agent.chat()` 和 `Agent.stream_chat()`。
- 只在一次回答完整结束后写入消息历史；失败或中断的回答不会污染历史。
- 根据 `MAX_ROUNDS` 仅向模型发送最近的对话轮次，默认保留 20 轮。
- 实现终端多轮聊天，以及 `/help`、`/clear`、`/exit` 命令。
- 修正包入口，现在可以通过 `neil-agent` 启动终端程序。

### 当前配置

将 `.env.example` 复制为 `.env`，然后填写真实 API Key：

```dotenv
DEEPSEEK_API_KEY=your_real_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic
DEEPSEEK_MODEL=deepseek-v4-flash
THINKING_ENABLED=false
SYSTEM_PROMPT="You are Neil Agent, a helpful local coding assistant. Give accurate, practical, and concise answers."
MAX_TOKENS=8192
MAX_ROUNDS=20
MAX_TOOL_ROUNDS=5
REQUEST_TIMEOUT=120
WORKSPACE_ROOT=.
```

`THINKING_ENABLED` 默认是 `false`，让响应更直接、流式展示更及时。设置为 `true` 后，普通请求和流式请求都会启用 DeepSeek 思考模式。`SYSTEM_PROMPT` 用于调整 Agent 的角色和回答方式，不再需要修改 Python 代码。

### 启动方式

```powershell
Copy-Item .env.example .env
# 编辑 .env 并填写真实的 DEEPSEEK_API_KEY
uv run neil-agent
```

终端内可用命令：

```text
/help   显示帮助
/clear  清空对话历史
/exit   退出程序
```

### 验证结果

- Ruff 代码检查：通过
- Ruff 格式检查：通过
- mypy 类型检查：通过（12 个源文件）
- pytest 离线单元测试：27 项通过
- 单元测试不会发送真实 DeepSeek 请求，也不会消耗 API 额度

2026-07-14，Neil 已使用真实 API Key 完成端到端聊天测试，确认配置加载、网络请求、DeepSeek V4 Flash、流式输出和多轮消息链路均可用。

## 2026-07-14：可配置系统提示词和思考模式

### 已完成

- 增加 `SYSTEM_PROMPT` 配置，可通过 `.env` 修改 Agent 的系统指令。
- 拒绝空白系统提示词，避免向模型发送无效配置。
- 增加 `THINKING_ENABLED` 布尔配置，默认关闭。
- 普通请求和流式请求共享同一套思考模式配置。
- 开启思考模式时发送 Anthropic 兼容的 `thinking` 参数。
- CLI 启动信息会显示当前思考模式是开启还是关闭。
- 增加系统提示词、思考模式和配置校验测试。

## 2026-07-14：只读工具调用闭环

### 运行流程

```text
用户提出项目问题
   ↓
DeepSeek 返回 ToolCall
   ↓
ToolRegistry 校验名称和参数
   ↓
执行 list_directory / read_file / search_text
   ↓
Agent 将 ToolResult 返回 DeepSeek
   ↓
DeepSeek 继续调用工具或生成最终回答
```

### 已完成

- 实现 `list_directory`、`read_file` 和 `search_text` 三个只读工具。
- 使用 `WORKSPACE_ROOT` 限制可访问的项目根目录。
- 解析真实路径，防止利用 `..`、绝对路径或符号链接逃出工作区。
- 默认隐藏 `.env`、`.git`、虚拟环境、缓存目录和常见私钥文件。
- 单文件读取上限为 1 MB，搜索结果上限为 100 条。
- 实现工具注册、重复名称检查、参数绑定、异常捕获和统一 `ToolResult`。
- 将 Anthropic 格式的工具定义、`tool_use` 和 `tool_result` 接入 DeepSeek。
- 思考模式执行工具时保留并回传必要的 thinking 内容。
- 使用 `MAX_TOOL_ROUNDS` 限制单次用户请求的工具循环，默认最多 5 轮。
- 工具循环完整成功后才写入消息历史；失败不会留下半轮历史。
- CLI 启动时显示工作区和已注册的只读工具数量。
- 新增文件权限、注册表、工具循环和 API 消息转换测试。

## 2026-07-14：异常分层与 CLI 可测试性

- 新增 `NeilAgentError` 作为用户可见错误的共同基类。
- `AgentError`、`LLMError` 和 `ToolError` 分别归属编排层、模型层和工具层。
- Agent 不再依赖或误用 `LLMError` 表示自身循环错误。
- CLI 统一捕获 `NeilAgentError`，仍向用户显示简洁的中文错误。
- 移除模块级 `Console` 实例，由 `main()` 创建并传给 `run(console)`。
- 所有 CLI 展示函数均显式接收 Console，便于替换输入和捕获输出。
- 增加异常继承关系和注入式 CLI 交互测试。

## 下一阶段计划

下一阶段只聚焦“受控文件修改”，暂不实现 Shell：

1. 设计写入权限、修改前确认和差异预览流程。
2. 实现受工作区保护的 `write_file` 与精确文本替换工具。
3. 补充失败回滚、敏感文件保护和端到端测试。
