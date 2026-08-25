# Guest 产物导出与二次批准导入

本文档描述认证 Windows Sandbox 下，受控 guest 产物从沙箱导出到工作区的完整流程。它与 CLI 会话 `/export`、`/import`（严格 JSON 信封）无关；后者面向人类会话迁移，而 guest export 面向**沙箱内不可信进程**产生的 UTF-8 文本文件。

## 目标

1. **调用前声明**：`run_command` 必须在第一次审批时列出 `export_paths`；审批绑定、RunSpec 与 WsbExecutionPlan 同步携带这些路径。
2. **有界 manifest**：host 只收集声明文件，生成自哈希 `GuestExportManifest` v1；预览不泄漏文件正文。
3. **二次批准导入**：`import_guest_export` 绑定 run、certification、manifest 与逐文件 digest；批准后由 `FileSystemTools` 原子写入，失败回滚。

未声明路径、未知新增文件、敏感路径、非 UTF-8 内容、超限体积或 manifest 与内容不一致时均 fail closed。

## 端到端流程

```text
run_command(executable, argv, export_paths?)
  │
  ├─ 第一次审批（binding_kind: sandbox-run-command）
  │    预览包含声明的 export_paths 列表
  │
  ├─ WSB 执行：只读快照 + 禁网；guest 写入 C:\NeilAgent\Export\{workspace-relative-path}
  │
  ├─ host 收集：仅 export_paths 中的文件；拒绝 result.json 以外的未知项
  │
  ├─ build_guest_export_manifest + guest_import.stage()
  │    磁盘暂存：.neil-agent/guest-export-staging/{manifest_sha256}/
  │
  └─ JSON 返回 guest_export.manifest_sha256

import_guest_export(manifest_sha256)
  │
  ├─ 第二次审批（binding_kind: guest-export-import）
  │    预览：有界 diff、逐文件 digest、无正文泄漏
  │
  └─ FileSystemTools.apply_guest_export_import()
       批准后复核工作区 → 原子写入 → 失败回滚
```

## 工具与入口

| 工具 | 第一次/第二次审批 | CLI | 非交互 v2 写入 | Web | 非交互只读 / v1 |
| --- | --- | --- | --- | --- | --- |
| `run_command` | 第一次（可选 `export_paths`） | 认证后 | 认证后 | 认证后 | 不注册 |
| `import_guest_export` | 第二次 | ✅ | ✅ | ✅ | 不注册 |

三条写入入口通过 [`host_runtime.py`](../src/neil_agent/host_runtime.py) 共享注册；`read-only` 与非交互 v1 不暴露导入能力。

### `run_command` 参数

- `executable`：工作区相对 `.exe` 路径。
- `argv`：参数向量；不接受 shell 字符串。
- `export_paths`（可选）：工作区相对 UTF-8 文本路径列表；省略时与旧行为一致，guest 修改全部丢弃。

成功且存在导出时，工具 JSON 结果包含：

```json
{
  "guest_export": {
    "manifest_sha256": "<64 hex>",
    "file_count": 1,
    "staged_import_manifest_sha256": "<same digest>"
  },
  "guest_modifications": "exported-for-import"
}
```

Agent 应使用 `guest_export.manifest_sha256` 调用 `import_guest_export`。

### `import_guest_export` 参数

- `manifest_sha256`：已暂存 manifest 的 SHA-256；须先由 `run_command` 导出或 `GuestExportImportTools.stage()` 写入。

暂存目录结构：

```text
.neil-agent/guest-export-staging/{manifest_sha256}/
  manifest.json
  files/{workspace-relative-path}
```

非交互 v2 的 request/approve 跨进程时依赖磁盘暂存；进程内 CLI/Web 同样写入该目录以保持一致语义。

## Guest 写入约定

Guest 进程（含 C# runner 调度的不可信子进程）应将声明文件写入：

```text
C:\NeilAgent\Export\{workspace-relative-path}
```

- 路径使用工作区相对 POSIX 风格（如 `out/result.txt`）。
- `result.json` 为 runner 协议产物；host 收集时忽略，不进入 manifest。
- host 在 exporter 完成后扫描 export 根目录，**只接受** `export_paths` 中声明的路径；任何额外文件导致失败。
- 当前仅支持 UTF-8 文本；二进制文件在导入预览阶段被拒绝。

Guest runner 侧对声明路径的强制校验尚未实现；host 收集已是 fail-closed 防线。见 [`sandbox-assessment.md`](sandbox-assessment.md) 与后续 runner 工作项。

## 审批绑定种类

| `binding_kind` | 工具 | 绑定内容 |
| --- | --- | --- |
| `sandbox-run-command` | `run_command` | 可执行文件、argv、cwd、快照/认证/runner 摘要、资源上限、`export_paths` |
| `guest-export-import` | `import_guest_export` | run_id、request_hash、certification、manifest 与逐文件 digest |
| `generic-tool` | 文件/Git 写等 | 工具名、参数与预览的域分离摘要 |

### 非交互 v2

`approval_required` 与 `approval_request` 事件中的每条记录包含 `binding_kind`（元数据，不含摘要正文）。`approve` 仍须完全相同的 prompt 与 approval ID；binding 不匹配时拒绝执行。

### Web Workbench

`ApprovalRequestDto` 与 `approval_requested` 事件携带 `binding_kind`。Review 面板在工具名旁显示人类可读标签（如「Sandbox command」「Guest export import」），帮助区分两次审批。详见 [`web-workbench-development.md`](web-workbench-development.md) P3。

### 审计

`import_guest_export` 与 `run_command` 的 `after_tool` 审计记录包含 `approval_binding_kind`（仅元数据，无 manifest 或文件正文）。

## 限制

| 项 | 上限 |
| --- | --- |
| 单次 manifest 文件数 | 32 |
| 单文件大小 | 1 MB |
| manifest 总体积 | 5 MB |
| 单路径长度 | 1000 字符 |

拒绝：路径越界、`..`、`.git`/`.neil-agent` 等屏蔽目录、常见密钥文件名、重复路径、非 UTF-8。

## 前置条件

1. `SANDBOX_BACKEND=windows-sandbox` 且 `/doctor` 显示 OS 沙箱 `ready`（完整认证 bundle 与 trust pins）。见 [`sandbox-certification-runbook.md`](sandbox-certification-runbook.md)。
2. 开发机无 `wsb.exe` 时 `run_command` 不注册；可单独测试 `import_guest_export` 的暂存与导入逻辑。
3. 写入模式：CLI 交互审批、非交互 `--permission-mode request|approve`、Web 控制租约下的逐工具审批。

## 离线评测

`evals/tasks.json` 中的 `guest-export-import-approval` 在临时工作区暂存 manifest，验证非交互 v2 request → approve 全流程与 `guest-export-import` 绑定元数据。不调用真实 WSB。

```text
uv run neil-agent-eval --task guest-export-import-approval
```

## 相关实现文件

| 模块 | 职责 |
| --- | --- |
| `sandbox_export.py` | Manifest v1、路径校验、预览拼装 |
| `sandbox_export_collect.py` | 声明路径规范化与 sealed export 根收集 |
| `sandbox_approval.py` | `RunCommandApprovalBinding`、`GuestExportImportBinding` |
| `tools/sandbox.py` | `run_command` + 导出后 `stage()` |
| `tools/guest_import.py` | `import_guest_export`、磁盘暂存 |
| `tools/filesystem.py` | `prepare_guest_export_import` / `apply_guest_export_import` |
| `windows_sandbox.py` | 执行后收集 `exported_files` |

## 相关文档

- [`sandbox-certification-runbook.md`](sandbox-certification-runbook.md) — 认证与手测清单
- [`sandbox-assessment.md`](sandbox-assessment.md) — 策略门槛与开放命令前的硬约束
- [`host-runtime.md`](host-runtime.md) — 三入口工具矩阵
- [`non-interactive.md`](non-interactive.md) — v2 审批协议与 `binding_kind`
- [`architecture.md`](architecture.md) — 总体分层

## 已知待办（可选）

- **Guest runner（C#）**：在 guest 内强制只允许写入已声明的 `export_paths`（纵深防御）。
- **真实 WSB 集成测试**：需本机 `wsb.exe` 与认证环境。
- **离线 eval 全流程**：`run_command(export_paths)` → stage → `import_guest_export` 单条 eval（当前 eval 仅覆盖导入半段）。
