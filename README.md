# Neil-Agent

A local coding agent built from scratch with Python and provider-neutral model boundaries. DeepSeek and Claude are currently supported.

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
- `/doctor`：只读检查配置、工作区、会话、审计、OS 沙箱能力和 Git，不调用模型或自动修复。
- `/cockpit`：显示任务、上下文、安全边界和工作区信号的只读基础快照，不调用模型。
- `/cockpit --live`：进入全屏实时驾驶舱；使用 `F3`（或 `Ctrl+T`）在执行 DAG 与上下文断层图之间切换。断层图分开显示下一次请求的五层本地估算与最近成功回合的服务端历史实测，标出裁剪轮次/体积、压缩检查点及最大工具结果占用（不显示正文），并按字符/token 软预算显示分级压力。Context 视图按 `F4` 可在不调用模型的情况下模拟下一次输入增加 N 个 ASCII 字符。`F5` 打开 Security Shield，以统一色带展示直接执行、逐次审批、永久禁止和不可用能力，明确分开应用工具白名单与 OS 沙箱状态，并列出最近审批的决策、对应工具节点和批准预览的最终绑定状态；DAG 中的审批子节点也显示同一关联。每次重新进入安全视图还会只读观察路径、网络、命令与审计四类边界，显示有界的状态变化和聚合告警；观察失败时保留上一份安全快照，且不显示路径、命令或审计内容。再次按下返回此前的 DAG/Context 视图。`F2`（或 `Ctrl+O`）展开/恢复结果，`1`–`4` 在 DAG 模式筛选节点，`Ctrl+X` 取消请求、`Ctrl+Q` 退出。各区域会按终端宽高自适应；非交互终端或 Textual 启动失败时自动降级为基础快照。
- `/rewind-task`：预览并恢复本进程最近一次 Agent 回合的全部有效文件编辑；`/rewind-file` 保留为兼容别名。
- `/permissions`：显示真正由代码执行的工具审批和工作区边界。

Claude Code 官方文档对照结论与保留差异见 [`docs/claude-code-review.md`](docs/claude-code-review.md)。
高级上下文断层图、安全盾、时间机器和仓库热力图的增量路线见
[`docs/visualization-development.md`](docs/visualization-development.md)。
多 LLM Provider 的协议边界、配置迁移和分阶段实现见
[`docs/provider-adapter-development.md`](docs/provider-adapter-development.md)；当前可选运行实现为 DeepSeek 和 Claude，尚未实现的 OpenAI、Ollama 与 vLLM 会在网络请求前 fail closed。

## 模型 Provider 配置

DeepSeek 仍是兼容默认值，旧配置可继续使用：

```text
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=<your-key>
DEEPSEEK_MODEL=deepseek-v4-flash
```

Claude 使用 Anthropic 原生 Messages API，必须显式提供模型 ID：

```text
LLM_PROVIDER=claude
LLM_MODEL=<anthropic-model-id>
ANTHROPIC_API_KEY=<your-key>
```

启用 Claude thinking 时，默认采用 `CLAUDE_THINKING_MODE=adaptive`。需要兼容只支持手动 extended thinking 的模型时，可设置 `CLAUDE_THINKING_MODE=enabled` 和 `CLAUDE_THINKING_BUDGET_TOKENS`；手动预算必须至少为 1024 且小于 `MAX_TOKENS`。`LLM_BASE_URL` 仅用于显式 endpoint 覆盖，不设置时 Claude 使用 Anthropic SDK 原生地址。

| Provider | 线协议 | 运行状态 | thinking 私有状态 |
| --- | --- | --- | --- |
| DeepSeek | Anthropic Messages compatible | 可用，默认兼容入口 | 签名块绑定 DeepSeek 与模型后回放 |
| Claude | Anthropic Messages native | 可用 | thinking 与 redacted-thinking 原样绑定并回放 |
| OpenAI | Responses | 尚未实现，fail closed | Phase 3 |
| Ollama / vLLM | OpenAI-compatible | 尚未实现，fail closed | Phase 4 |

默认测试使用合成、脱敏 fixture，不访问公网；本阶段未执行付费 Claude 在线 smoke test。

## 一次性非交互运行

`-p/--print` 执行一个 prompt 后退出，适合脚本和 CI。默认协议 v1 只向模型暴露文件读取、搜索以及只读 Git 工具。

```text
uv run neil-agent -p "概括当前项目结构"
uv run neil-agent -p "检查工作区状态" --output-format json
uv run neil-agent -p "检查工作区状态" --output-format stream-json
```

- 默认协议 v1 始终只读；不会接受审批 ID，也不提供写文件、质量检查、暂存或提交。
- 显式协议 v2 提供两次运行的审批流程：`request` 只生成精确预览，`approve` 使用一个未过期请求 ID 执行完全匹配的单项操作。
- v2 审批模式仅支持 `json` / `stream-json`，审批请求有效期为 15 分钟；工作区、prompt、项目指令、工具参数、当前预览或机器可读 binding 变化都会拒绝旧请求。approve 会在模型调用前原子领取 ID，因此失败、并发和篡改后恢复也不能重放。
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
`.neil-agent/audit/events.jsonl`，只记录有界元数据，不记录 prompt、thinking、工具参数/正文或 API Key；`AUDIT_LOG_MAX_BYTES` 控制单文件轮转上限。审计写入使用操作系统文件锁串行化检查、轮转和追加，锁等待超时会明确失败，进程崩溃后锁由内核释放。`/doctor` 只读报告审计文件大小、记录数、格式与锁状态，不显示日志正文。

`SANDBOX_BACKEND` 默认为 `disabled`；也可显式选择
`windows-sandbox` 启用认证探测。单独设置该值不会开放通用命令；还必须提供
`SANDBOX_CERTIFICATION_ROOT`、`SANDBOX_TRUSTED_REVIEWER` 和
`SANDBOX_TRUSTED_REVIEW_SHA256`，并让完整 raw bundle、GitHub Sigstore
provenance、独立 review、证书时效及当前 commit/OS/WSB/runner hashes 全部
匹配。后端不可用、证据缺失或任一绑定不一致时会 fail-closed，现有质量检查与
Git 命令仍保持固定白名单。`/doctor` 只显示结构化能力状态。
专用 Windows 安全任务使用 `SANDBOX_REQUIRED=1`，缺少组件或任何隔离用例
未通过都会失败；它还要求同一构建重复三轮，记录平台/产物/JUnit 与真实
`--raw` 调用 transcript，并实际安装和测试唯一 wheel 后生成 canonical
aggregate。aggregate 本身不是认证，默认空的独立审查 trust pins 不能使后端
ready。workflow 还要求受保护 environment 声明一次性 runner 及其版本，用固定
commit 的 `actions/attest` 签署 aggregate，再从 raw transcript、真实 JUnit 和
构建产物完整重放 bundle。普通开发机可以跳过真实平台用例；skip 绝不产生
认证。只有 ready 时才条件注册最小 `run_command`，它只接收工作区相对 `.exe`
与 argv，使用只读禁网快照并丢弃全部 guest 修改，不接受 shell 字符串。
完整策略、候选边界和开放门禁见
[`docs/sandbox-assessment.md`](docs/sandbox-assessment.md)，证据格式与审查
流程见 [`docs/sandbox-certification.md`](docs/sandbox-certification.md)。

离线评测支持单场景和 JSON 报告，也可由 `run_quality_check(eval)` 在受审批的固定命令中运行：

```text
uv run neil-agent-eval --task root-project-instructions --format json
```

默认评测完全离线，使用假模型和临时工作区，不读取 API Key。真实 DeepSeek 验收必须同时提供两个显式参数：

```text
uv run neil-agent-eval --real-deepseek --confirm-api-cost --format json
```

真实验收会消耗 API 额度。它在一次性临时工作区中核对服务端 `usage`、v1
默认只读工具与显式会话保存、压缩恢复，以及 v2 request/approve 和审批
重放保护；唯一写入发生在该临时目录，结束后删除。验收不主动制造限流或
网络故障，也不会输出 API Key、完整审批 ID、审批预览或原始模型响应。
