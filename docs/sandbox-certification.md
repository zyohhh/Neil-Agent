# Windows Sandbox 目标平台证据契约

本契约把真实平台证据、独立审查和运行时能力严格分层。单轮测试、三轮
aggregate，甚至未经 pin 的 certification 都不会改变 `WindowsSandboxBackend`
状态；只有完整 bundle 重新推导、GitHub Sigstore provenance、独立 review pin、
证书时效与当前宿主身份全部通过，才会映射为 ready。

## 工作流输入

强制安全任务为每次尝试创建带 run ID、attempt 和随机 nonce 的独占
`$RUNNER_TEMP/windows-sandbox-evidence-*` 根目录；目录创建成功后才把精确路径
传给 artifact 上传步骤。任务同时创建一次性虚拟环境，只安装锁定依赖和由当前
revision 构建的唯一 wheel，并以 Python isolated mode 运行测试。它向测试进程
提供：

- `SANDBOX_EVIDENCE_ROOT`：本次 workflow 的独占证据根目录；
- `SANDBOX_EVIDENCE_BUILD_ROOT`：三轮测试共同使用的固定 runner/probe 构建目录；
- `SANDBOX_EVIDENCE_WHEEL`：三轮开始前由当前 revision 构建的唯一 wheel；
- `SANDBOX_EVIDENCE_PLATFORM_JSON`：只读平台指纹；
- `SANDBOX_EVIDENCE_SUBJECT_JSON`：本轮实际执行的源码、构建产物和策略摘要；
- `SANDBOX_EVIDENCE_RAW_JSONL`：当前 repeat 的真实 CLI 原始响应；
- `SANDBOX_EVIDENCE_REPEAT_ID` 和
  `SANDBOX_EVIDENCE_EXECUTION_NONCE`：当前重复运行身份。
- `SANDBOX_ACTIONS_RUNNER_VERSION`：受保护 environment 显式记录的 runner
  版本；
- `SANDBOX_ACTIONS_RUNNER_EPHEMERAL=1`：专用 runner 的一次性声明，其他值
  直接失败。

上传步骤在测试失败时也会保存本次新建目录用于诊断；artifact 存在本身不表示
证据有效，只有严格 `verify` 生成且随后通过独立审查的 aggregate 才能进入认证
流程。

platform 和 subject 必须由安全 fixture 根据实际文件和系统状态生成。平台以
build 而不是可能保留旧名称的 `ProductName` 判断版本，只接受 Pro/Enterprise
且 build 26100（Windows 11 24H2）或更高版本、已启用的
Sandbox feature、有效 Authenticode 签名和真实 `wsb.exe`。三轮测试必须复用
同一 wheel、runner/probe 二进制；若重新编译或任一 identity hash 变化，聚合
验证会失败。共享构建目录必须位于证据根内且不能经过 symlink/reparse point。

## 原始 CLI 观察

执行器会在解析前为每次有界 `wsb.exe --raw` completion 调用 observer，包括
允许没有 stdout 的 host cancellation。实际接线必须把 repeat 与 nonce 交给
recorder，并原样转发完整 observation：

```python
from neil_agent.sandbox_evidence import RawObservationRecorder

with RawObservationRecorder(
    raw_jsonl_path,
    repeat_id=repeat_id,
    execution_nonce=execution_nonce,
) as recorder:
    def observe(observation):
        completed = observation.completed
        recorder.record(
            observation.stage,
            completed.stdout,
            argv=observation.argv,
            instance_id=str(observation.instance_id),
            run_id=str(observation.run_id),
            request_hash=observation.request_hash,
            returncode=completed.returncode,
            timed_out=completed.timed_out,
            cancelled=completed.cancelled,
            output_limited=completed.output_limited,
        )
```

每行 canonical JSON 同时绑定 repeat ID、nonce、顺序、stage、完整固定 argv、
instance/run/request 身份、完成状态以及真实 stdout 的 base64 与 SHA-256。
`raw_b64` 不是重新序列化的 JSON。只有被 timeout、cancel 或 output limit
终止的调用允许空 stdout；普通完成调用的空响应会被拒绝。recorder 还会拒绝
超限响应、重复 JSON key、浮点/非有限数、非法根类型和不连续 sequence。

`list_after_stop` 必须来自 stop 后真实执行的 `wsb list --raw`，并且调用方还
必须验证目标 instance UUID 已经消失。不能用空数组常量、stop 响应或 mock
替代。证据模块只保存和推导 schema，不替执行器判断实例是否已经停止。

测试结束后由 CLI 从 raw JSONL 推导 schema：

```powershell
& $venvPython -I -m neil_agent.sandbox_evidence schema `
  --raw-jsonl $raw `
  --output $schema
```

报告必须恰好覆盖 `start`、`runner`、`share`、`exporter`、`stop` 和
`list_after_stop`。每个 execution identity 必须遵循完整成功状态机，或唯一允许
的 host-cancel 状态机；stop 后必须至少有一次连续 list 轮询，且最后一次响应
不再包含目标 instance。同一阶段的 root type、字段类型或规范化嵌套 shape
不一致时拒绝生成报告。报告同时绑定 transcript hash 和本轮全部 execution
identity；测试代码不得自行拼装 schema JSON。

## 收集与聚合

`collect` 把 platform、subject、derived schema、pytest 退出码和 JUnit 绑定为
一个自哈希 canonical evidence run。pytest 退出码必须恰好为 0；JUnit
`tests/failures/errors/skipped` 汇总必须与 testcase 明细一致。失败、error、
skip、xfail 和 xpass 即使出现在报告中，也不能通过 `verify`。

`verify` 至少需要三个 repeat，并强制：

- repeat ID、execution nonce、transcript hash 和 evidence digest 分别唯一；
- 三轮来自同一 workflow run/attempt 和 producer，并按时间串行且不重叠；
- 三轮的 instance ID、run ID 和 request hash 集合彼此不重叠；
- 固定的十四项攻击测试完整存在；
- failed/error/skipped/xfailed/xpassed 全为零；
- platform、subject 和规范化 CLI schema 完全一致。

输出的 `aggregate.json` 只是结构化测试证据，不是认证。workflow 随后使用固定
commit 的 `actions/attest` 为该文件生成 SLSA provenance，把 Sigstore bundle
保存为 `aggregate.attestation.sigstore.json`，并立即执行 `bundle-verify`。GitHub
CLI 必须校验固定仓库、固定 workflow、`main` ref 和 subject commit；失败时仍
上传诊断 artifact，但该 artifact 不可认证。

## 完整 bundle 重放

`bundle-verify` 不信任 aggregate 的摘要结论。它要求固定三轮目录和唯一构建
产物，并逐项执行：

- 从每轮 `cli-raw.jsonl` 重新推导 schema，比较完整 transcript 与 response
  hashes；
- 重新解析真实 JUnit，比较 testcase、结果计数和原文件 hash；
- 由 raw schema、JUnit、platform、subject 与时间重新创建每个 evidence run，
  再重新创建 aggregate；
- 重新哈希唯一 wheel、runner 源码/二进制和 security probe；
- 拒绝未知目录、未知文件、hardlink、symlink/reparse point、非 canonical JSON
  和任何 identity 差异；
- 使用本地 `gh attestation verify --bundle` 验证 Sigstore 签名、固定 signer
  workflow、source digest 与 source ref。

因此，复制旧 aggregate、替换 raw/JUnit、混合三轮 artifact 或只伪造 summary
都不能通过。

## 独立审查与认证

`issue_certification()` 和 `certify` CLI 默认使用空 `ReviewTrustPins`，因此默认
必然拒绝。只有同时满足以下条件才可能生成认证：

- review 精确绑定 aggregate，结论为 approved，open findings 为零；
- reviewer 不是任一 evidence producer；
- reviewer ID 和 review SHA-256 都由宿主显式、独立 pin；
- review 不早于三轮证据全部完成，且最迟在证据完成后 7 天内完成；
- review 必须关闭固定十四项 security gate，不能用自定义或不完整集合替代；
- certification 签发时间不早于 review，单张证书最长 90 天，并且绝不能晚于
  原始证据完成后的第 90 天；
- subject 的安全保证为 `certified-windows-sandbox-v1`。

独立 reviewer 应先在下载后的绝对 bundle 根执行：

```powershell
python -I -m neil_agent.sandbox_evidence review `
  --bundle-root C:\absolute\evidence `
  --review-id <32-lowercase-hex> `
  --reviewer-id <independent-id> `
  --reviewed-at <UTC-ISO8601> `
  --output C:\absolute\evidence\independent-review.json
```

review digest 通过仓库外渠道 pin 后，再以 `certify --bundle-root ... --review ...`
签发 `certification.json`。`review` 与 `certify` 都会先完整重放 bundle 和
attestation，不接受单独传入的 aggregate。

## 运行时映射与最小工具

启用时设置以下三项，缺一即 fail-closed：

```text
SANDBOX_BACKEND=windows-sandbox
SANDBOX_CERTIFICATION_ROOT=C:\absolute\evidence
SANDBOX_TRUSTED_REVIEWER=<independent-id>
SANDBOX_TRUSTED_REVIEW_SHA256=<64-lowercase-hex>
```

每次执行前 verifier 都重新检查完整 bundle、Sigstore provenance、review pin 和
证书时间，并把 evidence 与当前 Git commit、相关源码 manifest、Windows build/
edition、WSB feature/Authenticode/file hash、runner source/binary、compiler、
framework reference、策略/协议版本和固定 gate 集逐项比较。任一变化都会让
`/doctor` 报告 `certification_invalid`。

只有 ready 时才注册 `run_command`。它只接受工作区相对 `.exe` 和 argv，固定
使用过滤后的只读快照、禁网、空 guest 环境、资源/输出/取消/完整进程树门禁，
所有 guest 修改均丢弃；不接受 shell 字符串，也不会回退到宿主进程。非交互
read-only/v1 模式仍不注册该工具。
