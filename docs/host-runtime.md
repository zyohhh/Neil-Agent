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
  build_host_runtime()
```

Web 启动器仍在 `web/runtime.py`（Uvicorn 与静态资源），不要与 `host_runtime.py` 混淆。

## HostMode 与当前能力矩阵

| 能力 | CLI | 非交互只读 | 非交互写入 | Web |
| --- | --- | --- | --- | --- |
| 文件写工具 | ✅ | ❌ | ✅ | ✅ |
| Git 写工具 | ✅ | ❌ | ✅ | ✅ |
| 计划工具 `set_task_plan` | ✅ | ❌ | ❌ | ✅ |
| Windows `run_command` | 认证后 | 认证后 | 认证后 | 认证后（与 CLI 同路径） |
| 指令作用域 | 启动目录 `cwd` | `cwd` | `cwd` | `cwd`（已与 CLI 对齐） |
| 审计 hooks | 可选 | 可选 | 可选 | 可选 |
| 会话持久化 | ✅ 多轮 | 单次/可选保存 | 单次/可选保存 | ✅ 成功回合保存；`select_session` / `new_session` |
| Security Shield 投影 | ✅ `/cockpit` | ❌ | ❌ | ✅ 快照 `security` DTO（与 CLI 同源） |
| ContextTomography 投影 | ✅ `/context`、驾驶舱 | ❌ | ❌ | ✅ 快照 `context` DTO（与 Agent 同源） |

Web 入口 parity 迁移已完成；后续可视化可在此基础上展示 richer UI。

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
# WorkbenchController 为每轮 turn 注入已选会话历史，成功后写入 SessionStore
# 浏览器通过 select_session / new_session 切换；启动时自动恢复最近一次已保存会话
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
| Web Security Shield 快照投影 | ✅ |
| Web ContextTomography 快照投影 | ✅ |
| 跨入口 parity 回归测试 | ✅ `tests/test_host_runtime.py` |

## 相关文档

- [`architecture.md`](architecture.md) — 总体分层
- [`web-workbench-development.md`](web-workbench-development.md) — Web 产品与协议
- [`claude-code-review.md`](claude-code-review.md) — 与 Claude Code 的能力对照
