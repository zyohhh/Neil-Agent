# 安全加固路线（对照 Claude Code）

本文把 2026-08-27 全树审查的建议实现顺序写成可执行批次。目标是补齐 **Neil 自己已经更严的沙箱快照策略** 与 **host 工具之间的缺口**，并收紧审批/Web/配置面，而不是引入任意 Bash 或默认打开 OS 沙箱。

对照背景见 [`claude-code-review.md`](claude-code-review.md)。审查结论：没有远程 RCE 或 Workbench XSS；Neil 是应用层白名单 + 预览绑定写操作 + 可选认证 WSB，不是默认 OS 沙箱 Agent。

## 原则

1. **权限由代码执行**，`AGENTS.md` / 模型输出不能放宽 denylist 或跳过审批。
2. **一份名单**：凭据目录与文件名只在 `sensitive_paths.py` 维护；host 文件工具、Git 暂存、Web 文件树/diff 路径、guest export、sandbox snapshot 与 WSB 校验共用。
3. **不把 loopback 当成对抗本机同用户恶意进程的隔离**；Web 加固针对协议完整性，不假装防 malware。
4. **质量检查等于在宿主机跑项目代码**；文案与提示词必须与该事实一致，直到有独立的沙箱检查批次。
5. Time Machine 恢复范围、Web PTY、跨 Provider 切换仍不在本路线内。

## 批次顺序

| 批次 | 状态 | 范围 | 完成标准 |
| --- | --- | --- | --- |
| 1 | 已完成 | 共享 secret denylist | 所有列出的调用点引用 `sensitive_paths`；`.ssh` / `.aws` / `id_rsa` / `credentials.json` 与 `.env` 同等拒绝；`.env.example` 仍可读 |
| 2 | 未开始 | 质量检查预警与去掉自动催促 | `run_quality_check` 预览明确写「无 OS 隔离的宿主执行」；去掉写入成功后必须跑检查的系统提示 |
| 3 | 未开始 | 审批绑定对齐 v2 | 非交互 `ApprovalStore.consume()` 在执行前再校验；CLI/Web 批准后在应用前复核当前 `AGENTS.md`（及可选 prompt）摘要 |
| 4 | 未开始 | Web `command_id` 与 `LLM_BASE_URL` | 命令结果缓存按 `client_id` 隔离，ID 不可猜测；`LLM_BASE_URL` 有 host 策略或显式危险开关；生产 `TRUSTED_HOSTS` 去掉 `testserver` |
| 5 | 未开始 | `git_diff` / `git_status` 内容过滤 | 只读 Git 输出不包含 denylist 路径的 diff hunk；与批次 1 的路径拒绝互补 |
| 6 | 未开始 | 写路径 `O_NOFOLLOW` | 普通 `write_file` / guest staging 与检查点恢复一样拒绝 symlink 写穿 |

批次 1 完成后，编号可视化 / Web P0–P9 仍保持已收口；本文件是后续安全工作的唯一检查清单。

## 批次 1：共享 denylist

**问题：** `sandbox_snapshot.py` 已拦截 `.ssh`、`.aws`、`id_rsa`、`credentials.json` 等，但 `tools/filesystem.py`、`sandbox_export.py` 与 Git/Web 名单更窄。模型可直接 `read_file` 这些路径。

**交付：**

- 新增 `src/neil_agent/sensitive_paths.py`：目录、文件名、后缀、`.env` / `.env.*`（排除 `.env.example`）。
- 下列模块删除私有副本，改为调用共享谓词：
  - `tools/filesystem.py`
  - `tools/shell.py`（`git_stage` 路径）
  - `web/service.py`（文件树与 review diff 路径）
  - `sandbox_export.py`
  - `sandbox_snapshot.py`
  - `sandbox.py` 快照内容校验
  - `windows_sandbox.py` 快照扫描
- 回归：host 读/写/搜索、guest export 声明路径、Git 暂存、Web 文件树均拒绝 `.ssh/id_rsa` 一类路径。

**本批不做：** 解析 `git diff` 正文（批次 5）；改变 WSB 默认关闭。

## 批次 2：质量检查 = 宿主执行

`run_quality_check` 在工作区以 `shell=False` 跑 pytest/ruff/mypy，**没有** OS 路径/网络隔离。当前系统提示还要求每次成功写入后选择一次检查，容易把审批变成习惯性确认。

交付：预览与 `/permissions` 文案升级；删除或改写 `TOOL_WORKFLOW_INSTRUCTIONS` 中的强制检查句。可选后续（需单独立项）：仅在认证 WSB 内跑检查。

## 批次 3：审批绑定

非交互 v2 已绑定 workspace、prompt、指令、参数与预览摘要，但 broker 匹配成功后未调用 `consume()` 做执行点二次校验。CLI/Web 仍是预览文本上的布尔值；`execute_approved` 的预览重算能抓住内容漂移，抓不住「点了 Yes 之后 AGENTS.md 被改」。

交付：执行前 `consume()`；交互路径在应用突变前比较当前指令摘要。截断 diff 须同时展示 Change-ID / 规模，避免未见尾部仍被当成已审全文。

## 批次 4：Web 协议与 Provider URL

- `WorkbenchController` 的 `_command_results` 按 `command_id` 全局缓存，ID 为 `web-<time>-<counter>`，多标签可碰撞。
- `LLM_BASE_URL` 为任意 `AnyHttpUrl`，选中的 API Key 会发往该 origin。
- `TRUSTED_HOSTS` 含 `testserver` 属于生产多余面。

交付见上表。Loopback + `SameSite=strict` 保持不变。

## 批次 5：只读 Git 内容

`git_stage` 已拒敏感路径；`git_diff` / `git_status` 仍可能把**已跟踪**的密钥文件正文交给模型。批次 1 只挡住工具参数里的显式路径和 Web diff 查询路径。

交付：对 porcelain / diff 输出按共享 denylist 剥离或替换为占位行，不把密钥 hunk 送进上下文。

## 批次 6：symlink / TOCTOU

检查点恢复已拒绝 symlink；普通写入与 guest staging 仍 `resolve()` 后 `write_bytes`。同用户竞态可写穿。交付：突变写使用 `O_NOFOLLOW` 或与恢复路径相同的 lexical 校验。

## 明确不做

- 为对齐 Claude Code 而增加任意 Bash / MCP。
- 默认打开 OS 沙箱（仍需专用 runner 认证）。
- 扩大 Time Machine 恢复范围。
- 立项 Web PTY、Focus/Build、跨 Provider 切换。

## 相关代码

- `src/neil_agent/sensitive_paths.py` — 共享 denylist
- `src/neil_agent/tools/filesystem.py` / `shell.py`
- `src/neil_agent/web/service.py`
- `src/neil_agent/sandbox_export.py` / `sandbox_snapshot.py` / `sandbox.py` / `windows_sandbox.py`

## 相关文档

- [`claude-code-review.md`](claude-code-review.md) — 对照审核与保留差异
- [`architecture.md`](architecture.md) — 分层与文件安全边界
- [`host-runtime.md`](host-runtime.md) — 三入口能力矩阵
