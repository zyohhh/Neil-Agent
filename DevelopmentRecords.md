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
- 当前状态：第一个可用版本已完成（终端多轮对话）

## 文件职责

| 文件 | 当前职责 |
| --- | --- |
| `cli.py` | 接收用户输入、实时展示流式文本、处理 `/help`、`/clear` 和 `/exit` |
| `agent.py` | 管理系统提示词、多轮消息历史以及模型调用流程 |
| `llm.py` | 封装 DeepSeek 客户端、普通请求、流式请求和错误转换 |
| `schemas.py` | 定义 `Message`、`ToolCall` 和 `ToolResult` 数据结构 |
| `config.py` | 从环境变量或 `.env` 读取并校验 API Key、模型名和运行限制 |
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
MAX_TOKENS=8192
MAX_ROUNDS=20
REQUEST_TIMEOUT=120
```

为了让第一版响应更直接、流式展示更及时，当前模型请求显式关闭思考模式。后续可以把思考模式做成可配置选项。

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
- mypy 类型检查：通过（10 个源文件）
- pytest 离线单元测试：9 项通过
- 单元测试不会发送真实 DeepSeek 请求，也不会消耗 API 额度

尚未使用真实 API Key 进行在线调用验证。首次配置 `.env` 后，需要手动发送一条消息确认网络、账户余额和 API 权限均正常。

## 下一阶段计划

1. 使用真实 API Key 完成一次端到端聊天测试。
2. 为思考模式和系统提示词增加可配置项。
3. 实现只读工具：`list_directory`、`read_file`、`search_text`。
4. 在 `registry.py` 中实现工具注册和分发。
5. 在 `agent.py` 中加入“模型请求工具 → 执行工具 → 返回结果给模型”的循环。
6. 在确认权限边界后，再实现文件修改和 Shell 命令执行。
