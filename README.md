# Neil-Agent

A local coding agent built from scratch with Python and provider-neutral model boundaries. DeepSeek, Claude, OpenAI, Ollama, and vLLM are currently supported.

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
- `/branch [标题]`：复制当前会话并切换到新 ID，原会话保持不变；版本 4+ 快照记录直接父会话 ID，当前版本 5 还保留可选 Provider/模型绑定。
- `/export [id]`：预览后导出当前或指定会话。
- `/import <文件名>`：预览后导入 `.neil-agent/exports/` 中的严格版本化文件。
- `/compact [关注点]`：总结较早轮次、保留最近完整上下文，并保存压缩前会话副本。
- `/context`：区分下一次请求的本地软预算估算与最近一次服务端实测 `usage`。
- `/doctor`：只读检查配置、工作区、会话、审计、OS 沙箱能力和 Git，不调用模型或自动修复。
- `/cockpit`：显示任务、上下文、安全边界和工作区信号的只读基础快照，不调用模型。
- `/cockpit --live`：进入全屏实时驾驶舱；使用 `F3`（或 `Ctrl+T`）在执行 DAG 与上下文断层图之间切换。断层图分开显示下一次请求的五层本地估算与最近成功回合的服务端历史实测，标出裁剪轮次/体积、压缩检查点及最大工具结果占用（不显示正文），并按字符/token 软预算显示分级压力。Context 视图按 `F4` 可在不调用模型的情况下模拟下一次输入增加 N 个 ASCII 字符。`F5` 打开 Security Shield，以统一色带展示直接执行、逐次审批、永久禁止和不可用能力，明确分开应用工具白名单与 OS 沙箱状态，并列出最近审批的决策、对应工具节点和批准预览的最终绑定状态；DAG 中的审批子节点也显示同一关联。每次重新进入安全视图还会只读观察路径、网络、命令与审计四类边界，显示有界的状态变化和聚合告警；观察失败时保留上一份安全快照，且不显示路径、命令或审计内容。再次按下返回此前的 DAG/Context 视图。`F2`（或 `Ctrl+O`）展开/恢复结果，`1`–`4` 在 DAG 模式筛选节点，`Ctrl+X` 取消请求、`Ctrl+Q` 退出。各区域会按终端宽高自适应；非交互终端或 Textual 启动失败时自动降级为基础快照。
- 实时驾驶舱按 `F6` 打开 Time Machine：可在最多 512 条脱敏运行事件上移动只读游标，并浏览最多 50 个会话的根/分支/压缩状态与 20 个进程内任务检查点的计数。它只重建历史投影，不重新调用模型或工具；Agent 空闲且无待审批时，可对**最新**任务检查点按 `R` 经审批恢复。会话标题、消息正文以及检查点路径、哈希和文件正文不会进入长生命周期 UI 状态。
- 实时驾驶舱按 `F7` 打开 Neural Map：从脱敏 `tool_call` 元数据聚合目录级读/写/检查热度、EARLY/MID/LATE 时间窗口与风险着色；不扫描工作区或显示文件正文。再次按下返回此前的 DAG/Context 视图。
- `/rewind-task`：预览并恢复本进程最近一次 Agent 回合的全部有效文件编辑；`/rewind-file` 保留为兼容别名。
- `/permissions`：显示真正由代码执行的工具审批和工作区边界。

Claude Code 官方文档对照结论与保留差异见 [`docs/claude-code-review.md`](docs/claude-code-review.md)。对照后的安全加固批次（批次 1–6 已完成）见 [`docs/security-hardening.md`](docs/security-hardening.md)。对照 DeepSeek Harness 的运行时预设路线（批次 1–3 已完成，含只读子任务）见 [`docs/runtime-profile.md`](docs/runtime-profile.md)。**项目状态、已知缺口与必做后续** 见 [`docs/project-status.md`](docs/project-status.md)。
TUI 可视化编号路线（上下文断层图、安全盾、时间机器、Neural Map）已全部交付，见
[`docs/visualization-development.md`](docs/visualization-development.md)。

Time Machine 的运行事件默认只存在于当前进程内。如需显式保留元数据事件，可设置 `RUNTIME_EVENT_STORE_ENABLED=true`；`RUNTIME_EVENT_STORE_MAX_BYTES` 控制当前 JSONL 文件的轮转上限（默认 5,000,000 字节，允许 10,000–50,000,000）。持久化仍只记录版本化 `RuntimeEvent` 白名单元数据，损坏或不安全的存储会 fail closed 并退回内存回放。

多 LLM Provider 的协议边界、配置迁移和分阶段实现见
[`docs/provider-adapter-development.md`](docs/provider-adapter-development.md)；当前五个 Provider 均可显式选择，兼容端点未声明的能力会在网络请求前 fail closed。
浏览器端 Web Workbench 的产品边界、fixture 原型和实时接入路线见
[`docs/web-workbench-development.md`](docs/web-workbench-development.md)。三入口共享装配与能力矩阵见 [`docs/host-runtime.md`](docs/host-runtime.md)。仓库内的 [`docs/Expected web UI.png`](<docs/Expected web UI.png>) 是 P6 视觉参考；稳定的 `?scene=` fixture 用于状态与截图回归，正常启动则进入保留 P5 安全边界、P7 故障恢复、P8 会话连续性并加入 P9 受控模型切换的本地工作台。安全评审见 [`docs/web-workbench-security-review.md`](docs/web-workbench-security-review.md)，Windows 安装与升级见 [`docs/web-workbench-operations.md`](docs/web-workbench-operations.md)。

P0 本地预览与验证：

```text
cd web
npm install
npx playwright install chromium
npm run dev
npm run lint
npm run typecheck
npm run test
npm run build
npm run e2e
npm run capture:baselines
```

Playwright 默认使用其标准浏览器缓存。若希望把浏览器二进制保存在仓库内的忽略目录，可先将 `PLAYWRIGHT_BROWSERS_PATH` 指向 `web/.playwright-browsers`，再执行安装、E2E 和基线截图命令。

P6 在 P5 可安装实时工作台、逐工具审批和受限 Review 的基础上完成参考图视觉收口，并让 `npm run e2e` 实际比较四个断点的受审截图。P7 将 fixture 和实时连接职责从页面组合层抽离，并为顶层渲染错误和 Review 局部错误提供不泄漏正文、不会触发 Agent 或工具的恢复界面。P8 增加受控制租约、revision 与 idle 状态保护的 Web 会话新建/选择：选中快照会在下一 turn 发网前恢复，成功 turn 原子保存，失败/取消不落盘，保存失败会要求显式新建或重选；跨 Provider/模型私有状态不会静默回放。P9 增加 Web 专用的同 Provider 模型选择：只有显式 `WEB_RUNTIME_MODEL_ALLOWLIST`、持有控制权、精确 revision、无运行/审批且当前会话为空并未保存时才能切换；切换不发网络请求，只作用于下一 turn。会话版本 5 持久化 Provider/模型绑定，因此不能先切模型再选择不兼容历史绕过门禁。源码发布前在 `web/` 执行 `npm run build`，再于根目录执行 `uv build --wheel`；前端生产资源及 SHA-256 清单会随 wheel 分发，安装后的 `neil-agent-web` 不需要 Node，也不依赖当前目录存在 `web/dist`。启动器只绑定 `127.0.0.1`，验证资源与端口后才生成 bootstrap，并只在自己的服务取得端口后打开浏览器；端口冲突或资源损坏时 fail closed。凭据交换后使用 `HttpOnly`、`SameSite=Strict` 本地会话、session 绑定 CSRF 和短时单次 WebSocket ticket。浏览器可开始或取消一个 Agent turn，并接收流式回答、活动、运行步骤和单个高风险工具的有界预览。Review 使用固定只读 Git 命令提供逐文件 `+/-`、rename/conflict/binary 状态和当前 revision 绑定的 40K 单文件 diff；未跟踪文件正文不返回，文件树可按 revision 增量刷新。质量检查历史在当前 Web turn 内最多保留 20 条；持久化会话当前恢复最后一条真实检查。

Cost 默认保持 `Unavailable`。如需估算，复制 [`docs/provider-rate-table.example.json`](docs/provider-rate-table.example.json)，填入已核验的真实费率，再设置 `WEB_RATE_TABLE` 指向该本地 JSON；表必须包含 `schema_version: 1`、版本号、生效日期、精确 provider/model、输入/输出费率、缓存费率（若有缓存 token）和 `input_token_accounting`。无表、费率尚未生效、模型未列出、缓存费率不完整或 token 记账语义不匹配时均不显示金额。金额始终标为 estimate，不代表 Provider 账单。只有持有控制租约的标签页能 Approve/Reject 当前 request；没有聚合 `Approve & Apply`、PTY 或任意 shell。

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

OpenAI 使用原生 Responses API，不通过 Chat Completions 或 Anthropic content block 模拟。模型 ID 必须显式提供；适配器默认 `store=false`，由本地会话保存并回放完整 output items：

```text
LLM_PROVIDER=openai
LLM_MODEL=<openai-model-id>
OPENAI_API_KEY=<your-key>
```

`THINKING_ENABLED=true` 时通过 `OPENAI_REASONING_EFFORT` 设置推理强度，默认 `medium`。推理 item 的加密内容会绑定 OpenAI 与模型后保存，用于手动管理的后续 Responses 上下文；跨 Provider 或模型回放会在网络请求前失败。

Ollama 与 vLLM 复用经过审计的 Responses 编解码和流式状态机，但使用独立的保守 capability profile。两者必须显式提供模型 ID，默认连接本机端点且不要求云 API Key：

```text
# Ollama（默认 http://localhost:11434/v1）
LLM_PROVIDER=ollama
LLM_MODEL=<local-model-id>

# 或 vLLM（默认 http://localhost:8000/v1）
LLM_PROVIDER=vllm
LLM_MODEL=<served-model-name>
```

`LLM_BASE_URL` 可覆盖端点，且必须以 `/v1` 结尾；受保护的本地网关可通过 `LOCAL_API_KEY` 提供 bearer key。适配器不会向兼容端点发送 OpenAI 的 `store` 或私有 reasoning state。由于工具能力取决于服务版本、启动参数和具体模型，默认只开放 Provider 的文本完成与流式输出；验证当前部署后才可设置 `LOCAL_TOOL_CALLING_ENABLED=true`。Neil Agent 的交互和一次性 Agent 循环默认携带内置工具，因此使用这两个入口时必须先开启该开关，否则会在发网前明确拒绝。vLLM 还可显式设置 `LOCAL_PARALLEL_TOOL_CALLS_ENABLED=true`，Ollama profile 不声明并行工具调用。未启用能力时，工具定义、工具历史和 reasoning 均在发网前被拒绝，不做静默降级。

Web Workbench 如需在运行时选择同一 Provider 的其他模型，可配置 JSON 数组，例如 `WEB_RUNTIME_MODEL_ALLOWLIST=["deepseek-fast","deepseek-reasoning"]`。最多允许 15 个附加精确模型 ID，启动模型会自动加入列表；未配置时选择器保持禁用。该设置不允许更换 Provider、endpoint 或凭据，也不改变 CLI/非交互入口；跨 Provider 仍需停止服务并修改启动配置。

| Provider | 线协议 | 运行状态 | thinking 私有状态 |
| --- | --- | --- | --- |
| DeepSeek | Anthropic Messages compatible | 可用，默认 Provider | 签名块绑定 DeepSeek 与模型后回放 |
| Claude | Anthropic Messages native | 可用 | thinking 与 redacted-thinking 原样绑定并回放 |
| OpenAI | Responses native | 可用 | output items 与 encrypted reasoning 绑定后原样回放 |
| Ollama | OpenAI-compatible Responses | 可用；工具默认关闭 | 不接受或保存私有 reasoning state |
| vLLM | OpenAI-compatible Responses | 可用；工具与并行调用分别显式开启 | 不接受或保存私有 reasoning state |

默认测试使用合成、脱敏 fixture，不访问公网。Claude/OpenAI smoke 必须分别同时设置 `NEIL_AGENT_RUN_CLAUDE_SMOKE=1` / `ANTHROPIC_API_KEY` / `NEIL_AGENT_CLAUDE_SMOKE_MODEL` 和 `NEIL_AGENT_RUN_OPENAI_SMOKE=1` / `OPENAI_API_KEY` / `NEIL_AGENT_OPENAI_SMOKE_MODEL`，执行时会消耗额度。本地 smoke 分别使用 `NEIL_AGENT_RUN_OLLAMA_SMOKE=1` / `NEIL_AGENT_OLLAMA_SMOKE_MODEL` 和 `NEIL_AGENT_RUN_VLLM_SMOKE=1` / `NEIL_AGENT_VLLM_SMOKE_MODEL` 显式开启；默认测试不会假定本机已运行模型服务。DeepSeek 使用 `neil-agent-eval --real-deepseek --confirm-api-cost --format json` 的双重确认验收。当前协议层锁定并验证 Anthropic SDK `0.116.0`、OpenAI SDK `2.53.0`；截至 2026-08-13，本分支没有可核验的在线 smoke 成功记录，因此不会把离线 fixture 标记为在线验证。

`/doctor` 只读显示当前 Provider、线协议、模型、脱敏 endpoint 和能力快照，不构造 SDK client、不发送模型请求，也不会显示 endpoint 凭据、query/fragment 值或 API Key。Ollama/vLLM 的 loopback HTTP 属于正常本地配置；远程明文 HTTP 会产生警告。

内部运行入口现在直接使用 `ProviderFactory` 和 `DeepSeekProvider`。`neil_agent.llm.LLMClient` 仅为 0.1.x 外部导入兼容保留，构造时发出 `DeprecationWarning`，计划在 `0.2.0` 移除；新代码应改为 `from neil_agent.providers.deepseek import DeepSeekProvider`。

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
与 argv，使用只读禁网快照。未声明 `export_paths` 时丢弃全部 guest 修改；
声明后可将 guest 产出的 UTF-8 文件经 manifest 暂存与 `import_guest_export`
二次批准写回工作区。不接受 shell 字符串。完整流程见
[`docs/guest-export-import.md`](docs/guest-export-import.md)。策略、候选边界和开放门禁见
[`docs/sandbox-assessment.md`](docs/sandbox-assessment.md)，证据格式、审查
流程与手测清单见 [`docs/sandbox-certification.md`](docs/sandbox-certification.md)
与 [`docs/sandbox-certification-runbook.md`](docs/sandbox-certification-runbook.md)。

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
