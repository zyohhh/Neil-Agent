# 运行时预设与能力接缝（对照 DeepSeek Harness）

本文把 2026-08-28 对照 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的结论写成可执行批次。目标是吸收 **「运行时 = 可组合预设 + 可逆注册 + 明确能力接缝」**，而不是引入 Cordis、插件市场、任意 Bash 或让模型改写正在运行的进程。

对照背景：DeepSeek Harness 是 MIT 许可的 agent 运行时，能力（模型、工具、会话、沙箱、循环、UI）都以插件挂到 Cordis 接缝上；官方评测使用 **Minimal** 模式（bash + 编辑器），因此分数高度依赖 harness 脚手架。Neil Agent 已有 `host_runtime.py`、`HostMode`、`HostProfile`、审批绑定与离线 eval，但缺少**与入口正交的能力预设**、**切换时的可逆卸载**，以及**受控只读子任务**。

安全加固仍只走 [`security-hardening.md`](security-hardening.md)；本文件不替代该清单。编号可视化 / Web P0–P9 仍保持已收口。

## 原则

1. **安全内核不是插件。** denylist、审批绑定、工作区边界、敏感路径与 TrustedHost 由代码强制；preset 只能**收窄**已注册能力，不能放宽。
2. **`HostMode` 与 `RuntimeProfile` 正交。** `HostMode` 表示入口（CLI / 非交互 / Web）及其审批与会话语义；`RuntimeProfile` 表示挂载哪些工具与循环能力。默认是 `HostMode.CLI` + `standard`。
3. **已有 `HostProfile` 是装配结果快照**，不是输入预设。新的 `RuntimeProfile` 是 `build_host_runtime()` 的输入；装配后的 `HostProfile` 必须能回放该预设（工具名集合可断言）。
4. **评测必须声明 harness。** 对外说「某模型在 Neil 上的分数」时，文档与 CLI 须写明 profile；默认 eval 绑定 `benchmark-minimal`，避免与 Standard / Claude Code 不可比。
5. **不做自修改 runtime。** 不引入 Creator Mode、HMR、进程内热挂任意插件。临时能力只能通过显式 preset 或一次性只读子运行时，卸载必须有 disposer。

## 与 DeepSeek Harness 的映射

| Harness | Neil 现状 | 本路线取舍 |
| --- | --- | --- |
| Standard / Code / Minimal / Creator 四种 mode | `HostMode` 按入口切能力 | 增加 `RuntimeProfile`；不实现 Code Mode 任意 TS、不实现 Creator |
| Cordis 可逆 effect / 依赖卸载 | 手工装配；Web `switch_model` 替换 worker | 批次 2：注册返回 disposer，切换按逆序 teardown |
| `ctx.tools` / `ctx.agents` 等接缝 | `ToolRegistry`、`Agent`、`SessionStore` 分层存在但未成表 | 下文接缝表；新能力只挂接缝，不改 Agent 内核特权 |
| Subagent spawn / fork / 后台 | 无 | 批次 3：只读、一次性、必 dispose；无写、无 `run_command` |
| `ctx.goals` 会话持久目标 | `set_task_plan` 偏当前回合 | 批次 4（可选）：会话事件化 goal，CAS 修订 |
| Skills 目录 | `AGENTS.md` 链 | 批次 5（可选）：`skills/<name>/SKILL.md` + `load_skill` |
| Workflow / Code Mode 编排 | 系统提示里的工具工作流句 | 批次 6（可选）：受限 Plan DSL，runtime 解释，不 eval 代码 |
| 完整 bash / 插件生态 / HMR | 刻意没有任意 shell | **明确不做** |

## 能力接缝（实现时不得绕过）

| 接缝 | 现有模块 | 允许扩展 | 禁止 |
| --- | --- | --- | --- |
| `tools` | `ToolRegistry` + `build_host_runtime()` | 按 profile 注册白名单工具 | 模型或 `AGENTS.md` 增补工具 |
| `approval` | CLI / Web / 非交互 broker | 统一 consume 语义（见安全批次 3） | 子运行时自批写入 |
| `session` | `SessionStore` | 子会话句柄、goal 事件 | 浏览器直接持有 Agent 对象 |
| `observability` | `EventBus` / audit | 子任务事件带 parent run id | 把 prompt / 工具正文写入事件 |
| `sandbox` | WSB 认证路径 | 多 backend 仍经 `windows_sandbox_backend()` | 未认证宿主声称 OS 隔离 |
| `provider` | `ProviderFactory` | 同 Provider 模型切换（Web P9） | 子任务更换凭据或 endpoint |

## 预设定义

实现前以本文为准；代码落地后以 `HostProfile.tool_names` 断言为准。

| Profile | 用途 | 工具面（示意） | 入口约束 |
| --- | --- | --- | --- |
| `standard` | 日常 CLI / Web | 当前完整集合（文件、受限 Git、计划、认证后 `run_command`） | 默认；不改变现有行为 |
| `benchmark-minimal` | 离线 eval、模型对比 | `read_file`、`replace_text`（及现有只读探路所需的最小集）；无 Git 写、无质量检查、无 `run_command`、无 `set_task_plan` | `neil-agent-eval` 默认；非交互可显式选用 |
| `web-safe` | 文档化 Web 子集 | 与当前 Web `HostMode` 已暴露集合对齐，作为显式 preset 而非隐式入口差异 | Web 可声明；不得宽于 `standard` |

`web-safe` 在批次 1 可以只是 `standard` 在 `HostMode.WEB` 下的别名（行为不变），但必须能在日志与 `/permissions`（或 Web 快照）中读到 profile 名。

## 批次顺序

| 批次 | 状态 | 范围 | 完成标准 |
| --- | --- | --- | --- |
| 1 | 已完成 | `RuntimeProfile` 与 `benchmark-minimal` | `build_host_runtime(..., profile=)`；eval 默认 minimal；`HostProfile` 可断言工具集；文档与 CLI 声明 harness |
| 2 | 已完成 | 可逆注册与 runtime teardown | 工具 / hook / 审批 / sandbox 注册返回 disposer；`switch_model` 与关闭路径逆序清理，无残留注册 |
| 3 | 已完成 | 只读子任务 | 并行只读探索；独立预算；无写 / 无 shell；`await` + 必 `dispose`；事件带 parent |
| 4 | 未开始（可选） | Session goals | 会话日志中的可 pause/resume 目标；CAS；压缩与分支后仍在 |
| 5 | 未开始（可选） | Skills 目录 | 仅加载工作区内已声明 `SKILL.md`；与 `sensitive_paths` 共用 denylist |
| 6 | 未开始（可选） | 受限 Plan DSL | 串行步骤由 runtime 解释；步骤仍走审批；禁止任意脚本 |

批次 1–3 是本路线的提交范围。4–6 记录以免遗忘，**不在 1–3 完成前开工**。安全批次 5–6（Git 内容过滤、`O_NOFOLLOW`）与本路线并行，互不阻塞。

## 批次 1：RuntimeProfile + benchmark-minimal

**问题：** 入口能力由 `HostMode` 隐式决定；eval 使用与日常 Agent 相近的工具面，模型分数无法声明 harness。DeepSeek 官方 benchmark 明确跑在 Minimal 上。

**交付：**

- 新增不可变 `RuntimeProfile`（名称与固定工具子集）。`build_host_runtime()` 接受 `profile`，默认 `standard`（行为与今日装配一致）。
- `HostProfile` 增加 `runtime_profile` 字段；测试断言 `benchmark-minimal` 的 `tool_names` 不含写 Git、质量检查、`run_command`、`import_guest_export`。
- `neil-agent-eval` 默认 `benchmark-minimal`；真实 DeepSeek 验收同样声明 profile。需要完整工具面的回归另开显式 `standard` 套件，不得 silently 混用。
- `/permissions`、非交互帮助与 eval JSON 报告包含 `runtime_profile`。
- `web-safe` 可先作为 Web 入口的声明名，工具集与当前 Web 矩阵一致。

**本批不做：** 改变默认 CLI 工具集；引入插件加载器；缩小 Web 现有能力。

## 批次 2：可逆注册

**问题：** Web `switch_model` 与关闭路径替换 worker / `Settings`，工具与 hook 注册若残留会跨模型泄漏能力。Harness 用 effect+disposer 保证卸载收敛。

**交付：**

- 装配层为每次注册提供 `disposer`（hooks、audit sink、sandbox 工具、任务 tracker）。`HostRuntime.close()` 逆序调用。
- `WorkbenchController` 切换模型或 `unsubscribe`/进程退出时调用 close，测试证明旧 registry 定义不再可见。
- 文档约定：后挂能力必须能在不重启进程的情况下卸掉；失败则保持旧 runtime（与 P9 事务语义一致）。

**本批不做：** 热重载 Python 模块；第三方插件；在 Agent 回合中途换 profile。

## 批次 3：只读子任务

**问题：** 大仓库探索占用主回合工具轮次。Harness subagent 含 fork/后台/可继续对话，安全面过大。

**交付：**

- 主 Agent 可启动**一次性**子运行时：`RuntimeProfile` 为只读子集（不宽于 `benchmark-minimal` 的读路径，且禁止 `replace_text`）。
- 独立 token/字符预算与 `MAX_TOOL_ROUNDS`；结果为有界摘要，不把子会话全文并入主历史。
- 无审批写、无 `run_command`、无 guest import；失败与取消都 `dispose`；超时强制 settle。
- `RuntimeEvent` 增加稳定 `parent_run_id`（或沿用现有父事件 ID），驾驶舱可折叠子树，不展示子任务正文。

**本批不做：** 可继续子对话、后台常驻、跨 Provider 子任务、子任务写文件。

**已实现（批次 3）：**

- `RuntimeProfile.READONLY_SUBTASK`：只读子运行时，工具面为 `list_directory` / `read_file` / `search_text`，无 `replace_text`。
- 主 Agent 工具 `run_readonly_subtask`（仅 CLI / Web `standard` / `web-safe`）；独立 `subtask_*` 预算与超时；有界摘要返回，子会话不并入主历史。
- `SubtaskParentState` + `subtask_parent_scope`；Web 传入 `parent_run_id=run_id`；子事件经 `parent_run_id` 与 `parent_event_id` 折叠。
- `HostRuntime.close()` 在子任务结束路径必调用；取消与超时均 settle。

## 批次 4–6（可选，不提前开工）

- **Goals：** 用户级持久目标写入会话事件，与 `set_task_plan` 分工（plan = 当前回合步骤，goal = 跨回合目标）。创建/暂停需与当前审批主体一致。
- **Skills：** `skills/<name>/SKILL.md` 经路径校验后由 `load_skill` 注入有界上下文；脚本类技能若出现必须走现有工具，不得新开解释器。
- **Plan DSL：** JSON 步骤列表（只读或已批准写），runtime 串行执行并在每步重绑预览。明确拒绝 TypeScript/Python eval（Harness Code Mode 不照搬）。

## 明确不做

- 引入 Cordis 或「一切皆平等插件」；审批与 denylist 不是可卸载插件。
- Creator Mode、HMR、让模型在内存里 mount 插件。
- 为对齐 Harness Standard 而增加任意 Bash / MCP。
- 默认打开 OS 沙箱或把 loopback Web 当成对抗本机恶意进程的隔离。
- 立项 Web PTY、跨 Provider 自动 fallback（仍见 [`web-workbench-development.md`](web-workbench-development.md) §19）。

## 相关代码（落地后维护此表）

- `src/neil_agent/host_runtime.py` — `HostMode`、`HostProfile`、`build_host_runtime()`
- `src/neil_agent/subtask.py` — 只读子任务上下文、预算与 `execute_readonly_subtask()`
- `src/neil_agent/tools/subtask.py` — `run_readonly_subtask` 工具注册
- `src/neil_agent/evals.py` — 离线与真实验收入口
- `src/neil_agent/web/controller.py` — 模型切换与 worker 生命周期
- `src/neil_agent/hooks.py` / `audit.py` — 注册与卸除

## 相关文档

- [`host-runtime.md`](host-runtime.md) — 三入口能力矩阵；本路线在其上增加正交 profile
- [`security-hardening.md`](security-hardening.md) — 应用层安全批次（并行，不替代）
- [`architecture.md`](architecture.md) — 分层与工具循环
- [`claude-code-review.md`](claude-code-review.md) — 与 Claude Code 的对照；本文件是与 DeepSeek Harness 的对照
- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) — 上游仓库（developer preview，API 仍会变）
