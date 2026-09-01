# Agent instructions

本文件只约束在 **Neil Agent 仓库** 内协作的 Cursor / 编码 Agent。

用户用 `neil-agent` 打开**另一个工作区**时，运行时由 `instructions.py` 加载的是那个工作区里的 `AGENTS.md` 链，与本文件无关。可选的 **Skills**（`skills/<name>/SKILL.md` + `load_skill`）也是运行时能力，**尚未实现**；规范见 [`docs/runtime-profile.md`](docs/runtime-profile.md) 批次 5。不要把本文件改写成 Skill，也不要在未立项时实现 `load_skill`。

## 文档权威

1. 行为与协议以 **代码 + 测试** 为准。
2. 安全批次：[`docs/security-hardening.md`](docs/security-hardening.md)（批次 1–6 已完成）。
3. 运行时预设：[`docs/runtime-profile.md`](docs/runtime-profile.md)（批次 1–3 已完成；4–6 可选）。
4. 当前缺口与必做项：[`docs/project-status.md`](docs/project-status.md)。
5. Web 协议细节：[`docs/web-workbench-development.md`](docs/web-workbench-development.md)；以 `tests/test_web_workbench.py` 为准。

## 架构约束

- **安全内核不是插件**：`sensitive_paths`、审批绑定、工作区边界不得被 preset 或 `AGENTS.md` 放宽。
- **新工具 / 能力** 须经 `host_runtime.build_host_runtime()` 按 `HostMode` + `RuntimeProfile` 注册；注册须返回 disposer，并纳入 `HostRuntime.close()`。
- **`HostMode` 与 `RuntimeProfile` 正交**；不得在非 Web 入口隐式改变默认工具面。
- **子任务** 仅只读、一次性、`READONLY_SUBTASK` profile；禁止在子运行时注册写工具、`run_command` 或嵌套 `run_readonly_subtask`。

## 开发命令

```text
uv run pytest -m "not online and not windows_sandbox_security"  # 离线门禁
uv run pytest tests/test_host_runtime.py tests/test_readonly_subtask.py  # 运行时相关
uv run ruff check . && uv run mypy src
cd web && npm run lint && npm run test && npm run build
uv build --wheel
```

## 变更纪律

- 最小范围 diff；不重构无关代码。
- 完成安全或 runtime 批次后：更新对应 `docs/*.md` 状态表、`DevelopmentRecords.md`、`docs/project-status.md`。
- 仅当用户明确要求时 `git commit` / `git push`。
- 不提交 `.env`、API Key 或凭据文件。
- 不要推送 `.github/workflows/*`，除非用户确认 GitHub token 具有 `workflow` scope 并明确要求推送。
- Windows 开发机默认 PowerShell：多条命令用 `;` 连接，不要用 bash 的 `&&`。

## 关键模块

| 模块 | 职责 |
| --- | --- |
| `host_runtime.py` | 三入口共享装配、`RuntimeProfile`、`build_agent()`、`HostRuntime.close()` |
| `subtask.py` / `tools/subtask.py` | 只读子任务 |
| `execution_budget.py` | 协作式 cancel / deadline |
| `git_output_filter.py` | Git status/diff 脱敏 |
| `security.py` | Security Shield 投影 |
| `agent.py` | 对话与工具循环 |
| `sensitive_paths.py` | 共享 denylist |
| `approval.py` | 审批绑定与 `consume()` |
| `web/controller.py` | Web turn、审批、控制租约 |
