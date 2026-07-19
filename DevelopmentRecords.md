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
- 当前状态：可见任务计划和 `/status` 已完成，下一步设计本地会话恢复

## 文件职责

| 文件 | 当前职责 |
| --- | --- |
| `cli.py` | 通过可注入 Console 接收输入、展示流式文本并处理终端命令 |
| `agent.py` | 管理多轮历史、受限工具循环，以及修改后验证和本地提交工作流 |
| `llm.py` | 封装 DeepSeek 请求、流式事件、工具定义和工具调用解析 |
| `errors.py` | 定义应用、Agent、LLM 和工具层的用户可见异常层级 |
| `schemas.py` | 定义消息、思考内容、工具定义、调用、结果和模型响应结构 |
| `task.py` | 管理单次任务计划、步骤状态和最近一次质量检查快照 |
| `config.py` | 从环境变量或 `.env` 读取并校验 API Key、模型、系统提示词、思考模式和运行限制 |
| `registry.py` | 注册工具、生成预览、执行审批检查并统一返回结果 |
| `filesystem.py` | 在工作区内读取、搜索以及原子写入 UTF-8 文件 |
| `shell.py` | 执行固定质量检查、只读 Git，以及需审批的明确路径暂存和本地提交 |
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
COMMAND_TIMEOUT=120
MAX_COMMAND_OUTPUT_CHARS=20000
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
/status 显示任务计划、最近检查和本地 Git 状态
/exit   退出程序
```

### 验证结果

- Ruff 代码检查：通过
- Ruff 格式检查：通过
- mypy 类型检查：通过（13 个源文件）
- pytest 离线单元测试：62 项通过
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

## 2026-07-15：受控文件修改

### 审批流程

```text
DeepSeek 请求 write_file / replace_text
   ↓
ToolRegistry 生成 unified diff
   ↓
CLI 展示修改预览并等待用户输入 y/yes
   ├─ 拒绝：不修改文件，将拒绝结果返回模型
   └─ 批准：执行原子写入，再将结果返回模型
```

### 已完成

- 新增 `write_file`，支持创建或完整替换 UTF-8 文本文件。
- 新增 `replace_text`，只有实际匹配数量等于 `expected_replacements` 时才执行。
- 写工具在注册时标记为必须审批，并且必须提供预览函数。
- 未审批、无法审批或用户拒绝时，写工具不会执行。
- CLI 只将明确的 `y` 或 `yes` 视为批准，其他输入默认拒绝。
- 使用 unified diff 展示修改内容，预览最多 20,000 字符。
- 预览包含内容 `Change-ID`；文件在确认后发生变化时拒绝使用旧审批。
- 使用同目录临时文件、刷新磁盘和 `os.replace` 实现原子写入。
- 写入失败时清理临时文件并保留原文件内容。
- 继续复用工作区边界、敏感文件屏蔽和 1 MB 文件大小限制。
- CLI 当前注册 5 个工具，其中 2 个写工具必须人工确认。
- 增加批准、拒绝、精确匹配、敏感路径和模拟写入失败测试。

2026-07-16，Neil 已使用真实 DeepSeek API 验证 `write_file` / `replace_text` 的写入预览、批准和拒绝流程，确认审批闭环可用。

## 2026-07-16：受限命令执行

### 权限规则

- `run_quality_check` 只接受 `pytest`、`ruff`、`mypy` 三个枚举值，并在执行前展示固定命令、工作目录和超时时间。
- 质量检查器的配置或插件可能运行项目代码，因此 `run_quality_check` 必须经过人工批准。
- `git_status` 和 `git_diff` 只读取仓库状态，不修改工作区，可直接执行；同时禁用分页器、fsmonitor、external diff、textconv 和可选锁。
- 不提供任意命令、任意参数、管道、重定向或 Shell 字符串入口。

### 已完成

- 使用当前 Python 解释器的 `-m` 形式运行质量检查，确保复用 Neil Agent 的虚拟环境。
- 所有命令都固定在 `WORKSPACE_ROOT` 中运行，并显式设置 `shell=False` 和空标准输入。
- 子进程只继承运行所需的少量环境变量，不传递 DeepSeek API Key、GitHub Token 等凭据。
- 使用 `COMMAND_TIMEOUT` 限制单次命令时间，默认 120 秒。
- 使用 `MAX_COMMAND_OUTPUT_CHARS` 限制返回给模型的输出，默认 20,000 字符；超长输出保留开头和结尾。
- 命令不存在、超时、启动失败和非零退出码统一转换为工具错误。
- CLI 当前注册 8 个工具，其中文件写入和质量检查共 3 个工具必须人工确认。
- 增加白名单、审批、只读 Git、工作目录、环境净化、超时和输出截断测试。

2026-07-17，Neil 已使用真实 DeepSeek API 验证质量检查的预览、批准和拒绝，以及 `git_status` / `git_diff` 只读调用，确认受限命令闭环可用。

## 2026-07-17：主动质量验证和本地 Git 保存

### 修改后验证

- Agent 在文件写入成功后会明确提醒模型根据修改类型选择合适的 `run_quality_check`，而不是无理由运行全部检查。
- 内置工具工作流会追加到可配置系统提示词之后，不会替换用户在 `.env` 中设置的角色提示词。
- 命令结果统一包含 `Command`、`Working directory`、`Exit code` 和 `Output`，便于模型准确汇总执行情况。
- 最终回答必须说明尝试了哪些检查、退出码和关键结果；用户拒绝审批时必须说明检查未运行。

### 本地 Git 保存

- 新增 `git_stage`，只接受最多 50 个明确的工作区相对文件路径。
- 拒绝 `.`、目录、工作区外路径、受保护目录、`.env` 和常见私钥文件，不提供 `git add -A` 或任意 pathspec。
- 暂存预览同时展示当前状态、已暂存内容、工作区 diff 和未跟踪文本文件内容。
- 暂存预览包含完整文件状态生成的 `Change-ID`；文件或暂存区变化后必须重新批准。
- `git add` 可能运行仓库配置的 clean filter，因此预览会明确提示风险，并始终要求人工批准。
- 新增 `git_commit`，只接受不超过 200 字符的单行提交消息，并在执行前展示暂存区 diff；超长内容会截断，但 `Change-ID` 覆盖完整 diff。
- 本地提交禁用 Git hooks 和 GPG 签名，不支持 amend、空提交、任意参数或推送。
- 暂存完成后提醒模型先调用 `git_diff(staged=true)`；提交完成后明确告知模型不能声称已经推送。
- CLI 当前注册 10 个工具，其中写入、质量检查、暂存和提交共 5 个工具必须人工确认。
- 增加主动验证提示、路径边界、敏感文件、旧审批失效、空暂存区和安全提交参数测试。

2026-07-19，Neil 已使用真实 DeepSeek API 验证修改后主动检查、结果汇总，以及 `git_stage` / `git_commit` 的批准和拒绝流程，确认本地版本保存闭环可用。

## 2026-07-19：可见任务计划和状态

### 任务计划

- 新增 `set_task_plan`，为多步骤开发任务创建 1–5 个简洁步骤。
- 新计划自动把第一步设为 `in_progress`，其余步骤设为 `pending`。
- 新增 `update_task_step`；只有当前进行中的步骤可以完成，完成后自动启动下一步。
- 计划必须按顺序推进，已完成步骤不能重新打开，并拒绝空白、重复或超过 200 字符的步骤。
- 计划只保存在当前 CLI 会话内；`/clear` 会同时清除对话、计划和最近检查。
- Agent 的内置工作流要求模型只为多步骤开发任务创建计划，并尽量把状态更新与相关工具调用合并。
- 计划创建或变化后由 CLI 立即展示，不需要等待最终回答。

### `/status`

- 新增 `/status`，不调用 DeepSeek，也不消耗 API 额度。
- 状态页展示当前计划及 `pending`、`in_progress`、`completed` 状态。
- 记录最近一次 `run_quality_check`，区分 `passed`、`failed` 和因拒绝审批导致的 `not_run`。
- 展示最近检查的命令、退出码和最多 500 字符的关键输出。
- 每次查看状态时通过只读 Git 命令获取最新分支和工作区状态，不依赖旧缓存。
- `/status` 的 Git 快照最多等待 5 秒，避免 Git 偶发卡顿阻塞 CLI。
- CLI 当前注册 12 个工具；两个计划工具只修改内存状态，不需要审批，高风险工具数量仍为 5 个。
- 增加计划限制、顺序推进、即时通知、检查状态解析、清理和 CLI 状态展示测试。

## 下一阶段计划

下一阶段只聚焦“本地会话恢复”，暂不增加云同步或远程 Git：

1. 使用真实 DeepSeek API 验证任务计划的创建、自动推进、即时展示和 `/status`。
2. 设计不包含 API Key 的版本化本地会话快照，并使用原子写入保存对话、计划和最近检查。
3. 增加 `/sessions` 与 `/resume`，让用户明确选择恢复的会话，不自动上传或同步数据。
