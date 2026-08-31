# 项目状态与后续路线

本文是 **Neil Agent 当前状态、已知缺口与必做后续** 的单一事实来源（2026-08-28 审视）。批次路线图仍以 [`security-hardening.md`](security-hardening.md) 与 [`runtime-profile.md`](runtime-profile.md) 为准；本文汇总二者完成度、交叉缺口与工程优先级，不替代专项文档中的交付细节。

## 当前快照

| 项 | 状态 |
| --- | --- |
| 版本 | `v0.1.0-dev`（`main` @ `3924677` 及之后） |
| Python | ≥ 3.13；包管理 `uv` |
| 入口 | `neil-agent`（CLI）、`neil-agent -p`（非交互）、`neil-agent-eval`、`neil-agent-web` |
| Provider | DeepSeek、Claude、OpenAI、Ollama、vLLM |
| 测试 | 846 项 pytest（`uv run pytest`）；Web 前端独立 `npm test` / Playwright |
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
| **高** | 子任务 | `KeyboardInterrupt` / `SystemExit` 曾被 `BaseException` 包装为 `ToolError` | `subtask.py`（已修复） |
| **中** | 子任务 | 超时/取消仅在流式 chunk 间检查，阻塞中的 `read_file` 等无法被强制打断 | `subtask.py` + `agent.py` 工具循环 |
| **中** | 子任务 | CLI 未向 `SubtaskParentState` 传入 `cancel`；`parent_run_id` CLI 用 `turn-…`、Web 用 `run-…` | `cli.py` / `controller.py` |
| **中** | 子任务 | 工具 JSON schema `maxLength` 与 `Settings.subtask_max_prompt_chars` 可能漂移 | `tools/subtask.py`（已改为注册时绑定 settings） |
| **中** | 安全（批次 6） | 普通 `write_file` / guest staging 无 `O_NOFOLLOW`，存在 symlink TOCTOU | `tools/filesystem.py`（已修复） |
| **低** | 观测 | 子任务转发事件仍含 `workspace_path` 元数据（无正文） | `events.py` 白名单 + Web `runtime_step` |
| **低** | 安全投影 | `run_readonly_subtask` 未单独列入 Security Shield 分组 | `security.py` |
| **低** | 成本 | 单回合可多次调用子任务，无显式调用次数上限 | 设计取舍，可按需加 `subtask_max_invocations` |

**非缺陷说明：** `test_web_workbench.py` 体量大（46 项、~2200 行），全量跑测耗时长，但无已知 flaky 标记；慢消费者行为有专门测试。在线 Provider smoke 与 `windows_sandbox_security` 为条件跳过。

## 必做后续（按优先级）

以下视为 **v0.2.0 前建议完成** 的工程项（不含可选的 runtime-profile 4–6）。

| 优先级 | 项 | 归属 | 完成标准 |
| --- | --- | --- | --- |
| P0 | **安全批次 5**：Git 输出内容过滤 | `security-hardening.md` | ✅ 已完成 |
| P0 | **安全批次 6**：突变写 `O_NOFOLLOW` | `security-hardening.md` | ✅ 已完成 |
| P1 | **子任务超时/取消可中断** | `runtime-profile.md` 批次 3 补强 | 工具 I/O 期间可感知 deadline/cancel；Web turn 取消不长时间挂起 |
| P1 | **子任务 CLI 取消与统一 `parent_run_id`** | 观测一致性 | CLI 绑定 cancel；驾驶舱折叠语义 CLI/Web 一致 |
| P2 | **子任务测试补强** | `tests/test_readonly_subtask.py` | 超时、KeyboardInterrupt、schema 与 settings 一致 |
| P2 | **Security Shield 子任务面** | `security.py` | 投影中显式列出 `run_readonly_subtask` |
| P3 | **文档与验收记录** | 本文 + 各专项 doc | 架构图、能力矩阵、README 状态与代码同步（审视批次） |

## 可选后续（不阻塞发版）

| 项 | 文档 |
| --- | --- |
| Session goals（跨回合目标） | [`runtime-profile.md`](runtime-profile.md) 批次 4 |
| Skills 目录 + `load_skill` | 批次 5 |
| 受限 Plan DSL | 批次 6 |
| 子任务每回合调用次数 / 聚合 token 上限 | 本文 §已知缺口 |
| Web 子树 UI 进一步折叠路径元数据 | `web-workbench-development.md` |
| `AGENTS.md` 仓库级协作规则扩充 | 根目录 `AGENTS.md` |

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
