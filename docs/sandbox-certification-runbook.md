# Windows Sandbox 认证操作手册

本手册面向**专用 WSB runner 运维与独立安全审查员**，把
[`sandbox-certification.md`](sandbox-certification.md) 中的契约落成可执行步骤。
上传 CI artifact 本身不等于认证；只有完整 bundle 重放、独立 review pin 和
`certification.json` 签发后，开发机才能设置运行时环境变量并进入 `ready`。

## 前置条件

| 项 | 要求 |
| --- | --- |
| Runner | GitHub `windows-sandbox-security` 自托管环境；`SANDBOX_ACTIONS_RUNNER_EPHEMERAL=1` |
| 平台 | Windows 11 Pro/Enterprise 24H2+；Sandbox 功能已启用；真实 `wsb.exe` |
| 分支 | 受保护 `main` 上 workflow 成功完成 |
| 审查 | Reviewer **不得**是 evidence producer；review digest 通过仓库外渠道 pin |
| 工具 | Python 3.13 isolated mode；`gh` CLI（bundle-verify 的 Sigstore 校验） |

## 步骤 1：获取 evidence bundle

1. 在 GitHub Actions 打开 `Windows Sandbox security gate` 最近一次 **main** 成功运行。
2. 下载 artifact `windows-sandbox-evidence-<run>-<attempt>-<nonce>`。
3. 解压到**绝对路径**目录，例如 `C:\evidence\windows-sandbox-12345`。
4. 确认根目录包含：`aggregate.json`、`aggregate.attestation.sigstore.json`、`platform.json`、`subject.json`、`required-tests.json`、`repeat-1`…`repeat-3`、`build\`。

## 步骤 2：重放 bundle（审查员必做）

在解压目录所在机器执行：

```powershell
python -I -m neil_agent.sandbox_evidence bundle-verify `
  --bundle-root C:\absolute\evidence
```

或使用仓库脚本（等价封装）：

```powershell
.\scripts\windows-sandbox-certify.ps1 `
  -BundleRoot C:\absolute\evidence `
  -Phase Verify
```

失败即停止；不得对未通过重放的 bundle 签发 review 或 certification。

## 步骤 3：独立 review

审查员在**与 producer 不同的身份**下执行：

```powershell
.\scripts\windows-sandbox-certify.ps1 `
  -BundleRoot C:\absolute\evidence `
  -Phase Review `
  -ReviewerId independent-security-reviewer `
  -ReviewId <32-lowercase-hex>
```

脚本写出 `independent-review.json` 并打印 `review_sha256`。**通过仓库外渠道 pin 该 digest**（不得只保存在 bundle 内）。

## 步骤 4：签发 certification

Pin 到位后，由授权方执行（`ExpiresAt` 不得超过证据完成后 90 天）：

```powershell
.\scripts\windows-sandbox-certify.ps1 `
  -BundleRoot C:\absolute\evidence `
  -Phase Certify `
  -TrustedReviewer independent-security-reviewer `
  -TrustedReviewSha256 <64-hex-from-step-3> `
  -IssuedAt 2026-08-25T10:00:00Z `
  -ExpiresAt 2026-09-24T10:00:00Z
```

产出 `certification.json`。`certify` 会再次完整重放 bundle 与 attestation。

## 步骤 5：绑定运行时（目标开发机）

在**与 evidence 相同 Git commit、相同 WSB/OS 指纹、相同 runner/compiler 哈希**的检出上设置：

```text
SANDBOX_BACKEND=windows-sandbox
SANDBOX_CERTIFICATION_ROOT=C:\absolute\evidence
SANDBOX_TRUSTED_REVIEWER=independent-security-reviewer
SANDBOX_TRUSTED_REVIEW_SHA256=<pinned-64-hex>
```

验证：

```powershell
neil-agent doctor
```

OS 沙箱应显示 `ready` 或等价 enforced 语义，且认证证据为「已验证并绑定」。此时 `run_command` 才会在 CLI/Web 条件注册。

## 步骤 6：认证后开发（下一代码里程碑）

认证通过后按 [`DevelopmentRecords.md`](../DevelopmentRecords.md) 与
[`claude-code-review.md`](claude-code-review.md) 推进：

1. **Guest 产物导出 manifest**（`sandbox_export.py`）— 调用前声明路径、有界自哈希 manifest、无正文预览。
2. **二次批准导入事务**（`import_guest_export` + `FileSystemTools`）— 绑定 certification、export manifest 与逐文件 hash，批准后原子写回工作区。
3. **`run_command` + `export_paths`** — 声明路径、沙箱导出收集、manifest 暂存与 `import_guest_export` 衔接已完成；guest 侧写入约定见 runbook。

Guest 进程应将声明文件写入沙箱内 `C:\NeilAgent\Export\{workspace-relative-path}`（与 `result.json` 同级或子路径）；host 在 exporter 完成后只收集 `export_paths` 中声明的路径，并拒绝任何额外文件。

## 常见失败

| 现象 | 处理 |
| --- | --- |
| `bundle-verify` Sigstore 失败 | 确认 `gh` 已登录且 attestation 来自受保护 `main` workflow |
| `certification_invalid` on doctor | commit/OS/WSB/compiler 与 subject 不一致；需对新 revision 重新收集 evidence |
| `只能恢复最新的任务检查点` | 与认证无关；Time Machine / rewind 语义 |
| 开发机无 `wsb.exe` | 预期跳过；`SANDBOX_REQUIRED=1` 时须失败 closed |

## 相关文件

- 契约：[`sandbox-certification.md`](sandbox-certification.md)
- CI：`.github/workflows/windows-sandbox-security.yml`
- 脚本：[`scripts/windows-sandbox-certify.ps1`](../scripts/windows-sandbox-certify.ps1)
- 运行时加载：`src/neil_agent/sandbox_runtime.py`
