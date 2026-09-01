# Host Runtime 组装与入口一致性

## 目的

Neil Agent 有三个主要运行入口，它们都必须向 `Agent` 提供一致的工具、指令、审计和沙箱边界：

| 入口 | 模块 | 启动命令 |
| --- | --- | --- |
| 交互式 CLI | `cli.py` | `neil-agent` |
| 一次性非交互 | `noninteractive.py` | `neil-agent -p ...` |
| 本地 Web Workbench | `web/controller.py` | `neil-agent-web` |

历史上这三处各自复制 `FileSystemTools`、`ShellTools`、`ToolRegistry` 和 `ProjectInstructionManager` 的装配逻辑，容易产生行为漂移。`host_runtime.py` 是统一装配层；各入口仍负责自己的审批、会话、输出和 UI。

## 模块位置

```text
src/neil_agent/host_runtime.py
  HostMode
  HostProfile
  HostRuntime
  instruction_target()
  windows_sandbox_backend()
  observe_host_security()
  build_host_runtime()
  build_agent()
```

Web 启动器仍在 `web/runtime.py`（Uvicorn 与静态资源），不要与 `host_runtime.py` 混淆。

## HostMode 与当前能力矩阵

| 能力 | CLI | 非交互只读 | 非交互写入 | Web |
| --- | --- | --- | --- | --- |
| 文件写工具 | ✅ | ❌ | ✅ | ✅ |
| Git 写工具 | ✅ | ❌ | ✅ | ✅ |
| 计划工具 `set_task_plan` | ✅ | ❌ | ❌ | ✅ |
| 只读子任务 `run_readonly_subtask` | ✅ | ❌ | ❌ | ✅ |
| Windows `run_command` | 认证后 | 认证后 | 认证后 | 认证后（与 CLI 同路径） |
| `import_guest_export` | 认证后需暂存 | 写入模式 | 写入模式 | 写入模式 |
| 指令作用域 | 启动目录 `cwd` | `cwd` | `cwd` | `cwd`（已与 CLI 对齐） |
| 审计 hooks | 可选 | 可选 | 可选 | 可选 |
| 会话持久化 | ✅ 多轮 | 单次/可选保存 | 单次/可选保存 | ✅ 选择、恢复、成功后原子保存 |
| 进程内模型切换 | ❌ 启动配置 | ❌ 启动配置 | ❌ 启动配置 | ✅ 同 Provider 显式白名单、idle + 空会话 |
| Security Shield 投影 | ✅ `/cockpit` | ❌（无对应 UI） | ❌（无对应 UI） | ✅ 与 CLI 共用观察函数 |
| ContextTomography 投影 | ✅ `/context`、驾驶舱 | ❌（无对应 UI） | ❌（无对应 UI） | ✅ 快照 `context` DTO（与 Agent 同源） |

Web 与 CLI 的沙箱注册、指令 `cwd` 作用域、会话连续性和安全投影现已对齐。Guest 产物导出与二次批准导入在 CLI、非交互 v2 写入与 Web 共用同一工具链；详见 [`guest-export-import.md`](guest-export-import.md)。入口仍保留各自的交互语义：CLI 长驻一个 Agent；Web 每个 turn 构造隔离的 Agent，再从控制器选中的严格快照恢复，且只在成功完成后保存。P9 的模型选择是 Web Controller 能力，不进入共享工具装配：它只替换下一 turn 使用的不可变 `Settings` 与 `AgentTurnWorker`，而 `build_host_runtime()` 仍按该 turn 捕获的设置装配相同安全边界。

## 使用方式

### CLI

```python
runtime = build_host_runtime(
    settings,
    mode=HostMode.CLI,
    task_change_handler=renderer.show_plan,
)
# runtime.filesystem, runtime.registry, runtime.instruction_manager, ...
# Agent 仍由 cli.py 构造，并注入终端审批与会话存储
```

### 非交互

```python
mode = (
    HostMode.NONINTERACTIVE_READONLY
    if permission_mode == "read-only"
    else HostMode.NONINTERACTIVE_WRITE
)
runtime = build_host_runtime(settings, mode=mode, base_hooks=hooks)
```

### Web

```python
runtime = build_host_runtime(settings, mode=HostMode.WEB)
# WorkbenchController 为每轮 turn 创建 EventBus 与 approval_handler，
# 并在 Agent 构造后恢复当前 SessionSnapshot。
```

## 迁移状态

| 步骤 | 状态 |
| --- | --- |
| 抽出 `instruction_target` / `windows_sandbox_backend` | ✅ |
| 引入 `build_host_runtime` 与 `HostProfile` | ✅ |
| CLI / 非交互 / Web 改用共享装配 | ✅ |
| Web 注册沙箱工具 | ✅ |
| Web 使用 `instruction_target` | ✅ |
| Web 会话 load/save | ✅ |
| Web idle-only 同 Provider 模型切换 | ✅ |
| CLI / Web 共用 Security Shield 观察 | ✅ |
| Web ContextTomography 快照投影 | ✅ |
| 跨入口 parity 回归测试 | ✅ `tests/test_host_runtime.py` |

当前无已知 host_runtime 迁移缺口；后续新能力应优先经 `build_host_runtime()` 接入三入口。

`HostMode` 只描述入口差异。与入口正交的能力预设（`standard` / `benchmark-minimal` / `web-safe` / `readonly-subtask`）、可逆注册与只读子任务见 [`runtime-profile.md`](runtime-profile.md)（批次 1–3 已完成）。Web 默认 `web-safe`（与 `standard` 工具面一致）；`neil-agent-eval` 默认 `benchmark-minimal`。`run_readonly_subtask` 仅在 CLI/Web 的 `standard` / `web-safe` 面注册；子运行时使用 `readonly-subtask` profile。CLI 与 Web 每 turn 使用 `new_parent_run_id()` 与 `subtask_parent_scope`（含 `cancel`）；协作取消/超时见 `execution_budget.py`。Web **后端**支持子任务，前端无独立子任务面板。CLI / 非交互进程结束、Web `replace_host_runtime()`（模型切换）与 Workbench 关闭时调用 `HostRuntime.close()` 逆序卸载。Web **turn 结束不再**关闭共享 runtime。`WorkbenchSnapshotService` 持有会话级实例；`AgentTurnWorker` 经 `host_runtime_provider` 复用。已知缺口见 [`project-status.md`](project-status.md)。

## 相关文档

- [`architecture.md`](architecture.md) — 总体分层
- [`guest-export-import.md`](guest-export-import.md) — Guest 产物导出与二次批准导入
- [`visualization-development.md`](visualization-development.md) — TUI 可视化路线（Phase 0A–4 已收口）
- [`web-workbench-development.md`](web-workbench-development.md) — Web 产品与协议
- [`claude-code-review.md`](claude-code-review.md) — 与 Claude Code 的能力对照
- [`security-hardening.md`](security-hardening.md) — 对照后的安全加固批次（批次 1–6 已完成）
- [`runtime-profile.md`](runtime-profile.md) — 对照 DeepSeek Harness 的运行时预设（批次 1–3 已完成）
- [`project-status.md`](project-status.md) — 项目状态、缺口与必做后续
