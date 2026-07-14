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
- 当前状态：第一个可用版本已完成，正在开发可配置能力和只读工具

## 文件职责

| 文件 | 当前职责 |
| --- | --- |
| `cli.py` | 接收用户输入、实时展示流式文本、处理 `/help`、`/clear` 和 `/exit` |
| `agent.py` | 管理系统提示词、多轮消息历史以及模型调用流程 |
| `llm.py` | 封装 DeepSeek 客户端、普通请求、流式请求和错误转换 |
| `schemas.py` | 定义 `Message`、`ToolCall` 和 `ToolResult` 数据结构 |
| `config.py` | 从环境变量或 `.env` 读取并校验 API Key、模型、系统提示词、思考模式和运行限制 |
| `registry.py` | 预留：注册工具并根据名称分发工具调用 |
| `filesystem.py` | 预留：受控查看、搜索和修改文件 |
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
REQUEST_TIMEOUT=120
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
- mypy 类型检查：通过（11 个源文件）
- pytest 离线单元测试：13 项通过
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

## 下一阶段计划

- [x] 使用真实 API Key 完成一次端到端聊天测试。
- [x] 为思考模式和系统提示词增加可配置项。
- [ ] 实现只读工具：`list_directory`、`read_file`、`search_text`。
- [ ] 在 `registry.py` 中实现工具注册和分发。
- [ ] 在 `agent.py` 中加入“模型请求工具 → 执行工具 → 返回结果给模型”的循环。
- [ ] 在确认权限边界后，再实现文件修改和 Shell 命令执行。
