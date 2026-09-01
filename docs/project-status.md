# 项目状态与后续路线

本文是 **Neil Agent 当前状态、已知缺口与必做后续** 的单一事实来源（2026-09-01 审视）。批次路线图仍以 [`security-hardening.md`](security-hardening.md) 与 [`runtime-profile.md`](runtime-profile.md) 为准；本文汇总二者完成度、交叉缺口与工程优先级，不替代专项文档中的交付细节。

## 当前快照

| 项 | 状态 |
| --- | --- |
| 版本 | `v0.1.0-dev` |
| Python | ≥ 3.13；包管理 `uv` |
| 入口 | `neil-agent`（CLI）、`neil-agent -p`（非交互）、`neil-agent-eval`、`neil-agent-web` |
| Provider | DeepSeek、Claude、OpenAI、Ollama、vLLM |
| 测试 | 离线门禁 `pytest -m "not online and not windows_sandbox_security"`：**829 passed**、10 skipped、16 deselected；Web 前端独立 `npm test` / Playwright |
| 静态检查 | `uv run ruff check .` 与 `uv run mypy src` 为开发门禁（`pyproject.toml` 含 `[tool.ruff]` / `[tool.mypy]`） |
| 安全加固 | 批次 1–6 **已完成** |
| 运行时预设 | 批次 1–3 **已完成**；批次 4–6 **可选、未开始** |

## 已完成能力（摘要）

### 安全加固（[`security-hardening.md`](security-hardening.md)）

- **批次 1**：`sensitive_paths.py` 共享 denylist，文件/Git/Web/guest/sandbox 共用。
- **批次 2**：质量检查明确为宿主执行；去掉写入后自动催促检查。
- **批次 3**：非交互 `ApprovalStore.consume()`；CLI/Web 批准前复核 `AGENTS.md` 摘要。
- **批次 4**：Web `command_id` 按 `client_id` 隔离；`LLM_ALLOW_CUSTOM_BASE_URL`；生产 `TRUSTED_HOSTS` 不含 `testserver`。

### 运行时预设（[`runtime-profile.md`](runtime-profile.md)）

- **批次 1**：`RuntimeProfile`（`standard` / `benchmark-minimal` / `web-safe`）；eval 默认 `benchmark-minimal`。
- **批次 2**：注册返回 disposer；`HostRuntime.close()` 逆序 teardown。
- **批次 3**：`run_readonly_subtask` + `READONLY_SUBTASK` 子运行时；独立 `subtask_*` 预算；`parent_run_id` 事件链路。

### 其他已收口主线

- 多 Provider 适配（Phase 5）、Web Workbench P0–P9、TUI 可视化 Phase 0A–4、三入口 `host_runtime` 共享装配、Windows Sandbox 契约与认证路径。

## 已知缺口与风险

按严重度排序；**代码修复项**在实现前应同步更新本文与对应批次文档。

| 严重度 | 领域 | 问题 | 位置 / 说明 |
| --- | --- | --- | --- |
| **低** | 产品 | Web 已注册 `run_readonly_subtask`，前端无独立子任务 UI | 后端能力；见 `web-workbench-development.md` |
| **低** | CI | 通用 PR 工作流文件在本地（`.github/workflows/ci.yml`），因 token 缺 `workflow` scope 尚未推送 | 推送后启用 ruff/mypy/pytest + Web lint/test/build |

CLI / 非交互 / Web / 只读子任务通过 `host_runtime.build_agent()` 装配 `Agent`。Web turn 复用 `WorkbenchSnapshotService.host_runtime`，不再每轮 `build_host_runtime()`。

**非缺陷说明：** `test_web_workbench.py` 体量大（46 项），需 mock worker 接受 `parent_run_id`。`test_real_runner_*` 标为 `windows_sandbox_security`，不进入普通离线门禁。在线 Provider smoke 为 `@pytest.mark.online`。

## 必做后续（按优先级）

以下视为 **v0.2.0 前建议完成** 的工程项（不含可选的 runtime-profile 4–6）。

| 优先级 | 项 | 归属 | 完成标准 |
| --- | --- | --- | --- |
| P0 | **安全批次 5**：Git 输出内容过滤 | `security-hardening.md` | ✅ 已完成 |
| P0 | **安全批次 6**：突变写 `O_NOFOLLOW` | `security-hardening.md` | ✅ 已完成 |
| P1 | **子任务超时/取消可中断** | `runtime-profile.md` 批次 3 补强 | ✅ 已完成 |
| P1 | **子任务 CLI 取消与统一 `parent_run_id`** | 观测一致性 | ✅ 已完成 |
| P2 | **子任务测试补强** | `tests/test_readonly_subtask.py` | ✅ 已完成（超时/中断/ schema） |
| P2 | **Security Shield 子任务面** | `security.py` | ✅ 已完成 |
| P3 | **文档与验收记录** | 本文 + 各专项 doc | ✅ 2026-09-01 同步（测试数、模块表、Git 脱敏 / `O_NOFOLLOW` / budget / CI） |
| P3 | **静态检查与离线 pytest 门禁** | ruff / mypy / 测试对齐 | ✅ 已完成；通用 GitHub Actions 工作流待有 `workflow` 权限后推送 |

## 可选后续（不阻塞发版）

| 项 | 含义 | 文档 |
| --- | --- | --- |
| Session goals | 跨回合、可暂停的用户目标（会话事件）；区别于当前回合的 `set_task_plan` | [`runtime-profile.md`](runtime-profile.md) 批次 4 |
| Skills 目录 + `load_skill` | 用户工作区 `skills/<name>/SKILL.md`，模型按需加载长手册；**不是**始终注入的 `AGENTS.md` | 同上，批次 5 |
| 受限 Plan DSL | JSON 步骤由 runtime 串行执行并走审批；不 eval 脚本 | 同上，批次 6 |
| 子任务聚合 token 上限 | 每回合调用次数已有 `subtask_max_invocations`（默认 3）；跨子任务 token 加总仍可选 | `config.py` / 批次 3 |
| Web 独立子任务面板 | 路径元数据已在 `runtime_step` 折成目录；前端仍无子树 UI | [`web-workbench-development.md`](web-workbench-development.md) |
| 本仓库 `AGENTS.md` 扩充 | **Cursor 协作约束**（本文件仓库根），与运行时用户工作区 `AGENTS.md` 链无关 | 根目录 [`AGENTS.md`](../AGENTS.md) |

**两种 `AGENTS.md`（勿混用）：**

1. **运行时（已实现）：** 用户打开的工作区里，`instructions.py` 按根→目标目录加载同名文件，作为非可信项目指令。
2. **本仓库协作（本表「扩充」）：** Neil-Agent 仓库根给 Cursor / 编码 Agent 的开发纪律。扩充 = 把容易踩的约定写进该文件（门禁命令、安全内核、禁止事项），不是给 `neil-agent` 增加新指令格式。

## 文档权威与维护规则

1. **协议与行为以代码 + 测试为准**；[`web-workbench-development.md`](web-workbench-development.md) 已声明 Web 协议以 `tests/test_web_workbench.py` 为准。
2. **安全批次** 只增不改 [`security-hardening.md`](security-hardening.md) 编号；完成后更新状态列与 [`DevelopmentRecords.md`](../DevelopmentRecords.md)。
3. **运行时预设** 同理维护 [`runtime-profile.md`](runtime-profile.md)；新 profile 须可经 `HostProfile.tool_names` 断言。
4. **本文件** 在每次里程碑审视或批次完成后更新「当前快照」「已知缺口」「必做后续」三节；避免在 README 重复长列表，README 只保留一行摘要与链接。
5. **历史验收记录**（如 [`web-workbench-basic-acceptance.md`](web-workbench-basic-acceptance.md)）保留原始基线，顶部注明历史性质与当前 `main` 参考提交。

## 相关文档索引

| 文档 | 用途 |
| --- | --- |
| [`architecture.md`](architecture.md) | 分层架构与模块职责 |
| [`host-runtime.md`](host-runtime.md) | 三入口装配与能力矩阵 |
| [`security-hardening.md`](security-hardening.md) | 安全加固批次清单 |
| [`runtime-profile.md`](runtime-profile.md) | 运行时预设与只读子任务 |
| [`claude-code-review.md`](claude-code-review.md) | 与 Claude Code 对照 |
| [`web-workbench-development.md`](web-workbench-development.md) | Web 产品与协议 |
| [`DevelopmentRecords.md`](../DevelopmentRecords.md) | 按日开发记录 |
