# 子进程 OS 沙箱契约与平台评估（2026-07-26）

## 当前结论

Neil Agent 仍不开放任意 shell。现有 `shell=False`、固定参数、最小环境、
超时、输出上限和逐次审批属于应用层白名单；它们不能限制已经启动的进程
读取用户文件、凭据、网络或其他进程，因此不能称为 OS 沙箱。

本阶段新增的平台无关契约和 Windows 能力门禁也不会扩大现有命令权限：

- 默认关闭 OS 沙箱；固定质量检查和 Git 工具继续使用原实现。
- Windows 后端只在完整能力探测和目标平台隔离验收均通过后才允许进入
  `ready`；不可用、初始化失败、策略不完整或验收未通过都 fail-closed。
- 当前不注册通用命令工具，也不会在后端不可用时回退到普通
  `subprocess`。
- 沙箱只允许看到一次运行的过滤快照或完全不提供工作区；真实工作区、
  `.env`、`.git`、`.neil-agent`、代理配置和私钥不会映射为可写目录。

因此，“实现后端契约与能力诊断”不等于“通用命令已经安全开放”。后者必须
满足本文最后列出的平台门禁。

## 平台无关执行契约

一次沙箱请求必须先固化为不可变、可验证的运行规范，至少包含：

- **可执行文件与参数**：只接受绝对可执行文件和分离的 argv；拒绝 shell
  字符串、NUL、批处理/脚本解释器隐式搜索和标准输入。
- **工作区**：仅允许 `none` 或过滤后的只读快照。快照有文件数、单文件和
  总字节上限，拒绝符号链接、junction、mount point、其他 reparse point
  和硬链接；命令产生的修改不直接回写宿主仓库。
- **环境变量**：从空环境构造，只暴露固定名称；临时 HOME、USERPROFILE、
  TEMP/TMP 指向该次运行的 scratch。API Key、令牌、代理变量、SSH/云凭据、
  `VIRTUAL_ENV` 和宿主用户目录不继承。
- **网络**：默认且当前唯一允许的策略是 `deny`；外部 IPv4/IPv6/DNS 及
  经对照校准、guest 可达的宿主/LAN endpoint 都属于目标平台验收范围。
  guest 自己的 loopback 不是宿主可达性的证据。
- **进程树**：后端必须控制完整子进程树；超时、取消、输出超限、资源超限
  和初始化失败都终止整个树，不能只杀直接子进程。
- **资源**：固定墙钟超时、内存、进程数和输出上限；stdout/stderr 必须在
  读取过程中限流，不能先无界捕获再截断。
- **结果**：只返回有界 stdout/stderr、退出码、终止原因和安全能力摘要；
  沙箱输出始终按不可信数据处理。

策略对象必须拒绝未知或不完整组合。后端返回成功前，必须确认所有隔离层和
清理步骤均成功；任何异常都转换为明确的沙箱错误。

## Fail-closed 状态与取消语义

能力诊断使用固定状态，而不是通过一次普通进程执行猜测安全性：

- `disabled`：宿主没有启用沙箱，属于安全的默认状态。
- `unavailable`：操作系统、可选组件或所需 API 不存在。
- `incomplete`：发现部分能力，但网络、文件、进程树或资源边界不完整。
- `ready`：只读探测通过，并且同一后端版本已经在目标平台安全测试中通过。

`/doctor` 的探测必须纯只读：不能创建 AppContainer profile、修改 ACL、
创建临时目录或启动探针进程。真正的 provision 和执行只能发生在一次精确
审批之后。

调用方取消、键盘中断和墙钟超时使用同一终止协议：先请求后端停止完整隔离
实例或 Job，等待其确认结束，再清理 scratch。无法确认进程树已经终止时，
本次结果必须是失败，不能返回部分成功。

## Windows 后端判断

### Windows Sandbox

[Windows Sandbox](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/)
使用 Hyper-V 隔离的独立内核，适合作为高隔离后端。配置必须显式禁用
networking、vGPU、clipboard、printer、audio/video input，启用
Protected Client，并只映射过滤快照为只读目录。Microsoft 文档指出网络
默认启用，且可写 mapped folder 会把沙箱修改保留到宿主，因此不能依赖
默认配置，也不能直接映射真实仓库。

[Windows Sandbox CLI](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-cli)
从 Windows 11 24H2 起提供 `start`、`exec`、`share` 和 `stop`。其中
`exec` 不提供进程 I/O，因此候选实现不能直接捕获其 stdout/stderr，也不能
在不可信命令运行时提前映射可写宿主目录。当前设计先只读映射 snapshot 与
control，固定 runner 在 guest 内部收集有界结果并确认 Job 为空；runner
退出后才把一次性空 export 目录映射为可写，再由固定 exporter 导出结果，
最后无条件停止显式 UUID 实例。

该组件不是所有 Windows 主机的默认能力。后端必须先只读解析可执行文件与
版本，并在专用 Windows 安全 CI 中完成实际逃逸测试；找不到
`WindowsSandbox.exe`/`wsb.exe`、版本能力不足或无法可靠停止实例时保持
`unavailable`，绝不改用普通子进程。

### AppContainer / LPAC

[Microsoft AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)
可以通过低完整性令牌、SID/DACL 和显式 capability 限制文件、注册表、
设备、进程与网络，但需要可信 native launcher、profile 生命周期、ACL、
`PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` 和 Job Object 协同。Job
Object 或 restricted token 单独只能管理生命周期/资源，不能提供所需文件
与网络边界。

首版不能给 AppContainer 递归授权真实仓库：这会暴露 `.env`、`.git` 和
会话目录，也会留下持久 ACL 与 TOCTOU 清理风险。只有“临时过滤快照 +
运行后扫描 diff + 第二次独立审批导入”才适合作为未来的可写工作流。

### 当前 Windows 候选实现边界

仓库现在包含五层彼此约束的候选构件，并在通过独立审查与运行时认证后可进入
`ready` 状态：

1. `sandbox.py` 保留平台无关契约、只读探测和 fail-closed 公共边界；
   `WindowsSandboxBackend.run()` 在持有有效 `certification.json` 且 probe
   为 `ready` 时执行有界 guest 命令。
2. `sandbox_snapshot.py` 从真实仓库复制过滤后的独立只读快照，生成 canonical
   manifest 与 SHA-256；Windows 遍历期间用不共享 delete 的目录/文件句柄
   阻止 junction、reparse point、硬链接和并发替换。
3. `sandbox_guest.py` 与固定 C# runner 定义 canonical 请求/结果协议，绑定
   run/request/instance 身份。可信 runner 保持原身份并独占中完整性结果目录；
   不可信命令使用去除特权的 restricted primary token 和 Low Integrity，借助
   `CreateProcessAsUser` 以 suspended 状态创建。STARTUPINFOEX 仅继承三个标准流
   句柄，分配带进程数、进程/Job 内存和 `KILL_ON_JOB_CLOSE` 的 Job 后才恢复。
   stdout/stderr 在读取时共享有界预算；超时、取消和洪泛终止完整 Job，并查询
   `ActiveProcesses == 0` 后才写入 `job_terminated=true`。
4. `sandbox_lease.py` 为 snapshot、control 和 export 建立有条目/字节上限的
   执行期树租约。Windows 对每个既有对象持有不共享 write/delete 的真实句柄，
   每次复核同时比较持有句柄和当前路径 identity；export 根在写入阶段只允许新增
   结果但不能替换根，exporter 退出后立即封存唯一结果文件。新条目、身份变化、
   超限或任一句柄关闭失败都会拒绝本次结果。
5. `windows_sandbox.py` 只接受真实 `wsb.exe`、显式 UUID、固定 guest 命令和
   有界 `--raw` JSON；按 start → execute → late share → export → stop →
   seal-result → stop → list-confirm → parse-result 顺序执行。结果在实例停止前
   不会读取；stop 后还必须从 `wsb list --raw` 确认目标 UUID 消失，随后从已封存
   的同一文件句柄读取。即使 stop 自身失败也仍独立尝试 list 确认；任一阶段、
   结果绑定、租约、输入 manifest 复核或清理确认失败都会拒绝结果，没有宿主
   subprocess fallback。

候选执行器在未认证前仍使用
`candidate-restricted-low-integrity-job-not-certified` 保证字符串；固定攻击集
必须在一次性 Windows 11 Pro/Enterprise 24H2+ WSB runner 上重复执行并接受
独立审查。`host_runtime.py` 仅在 probe `ready` 时注册条件 `run_command`；
`/doctor` 会报告 `ready` 或具体 fail-closed 原因（`cli_executable_required`、
`certification_required` 等），不会把配置意图冒充为已生效隔离。

目标机器没有可用 Windows Sandbox 组件时，普通平台验收会明确跳过，同时
诊断报告 `unavailable`。这种跳过不能用于把后端标成 `ready`；设置
`SANDBOX_REQUIRED=1` 后，同一缺失必须转为失败。

## Linux 后端评估

[bubblewrap](https://github.com/containers/bubblewrap) 可使用 user、mount、
PID 和 network namespace 构造最小文件系统，并组合 seccomp。它是构建沙箱
的低层工具，安全性仍取决于调用策略和参数。

未来 Linux 后端至少需要：只读运行时与依赖、过滤工作区快照、临时 HOME、
默认新 network namespace、隐藏凭据目录、PID namespace、进程树终止、
输出/时间/内存上限，以及启动时对非特权 user namespace 和 seccomp 的
实际探测。不可用时同样 fail-closed。

## 自动化与平台验收边界

不依赖真实模型的单元测试必须覆盖：

- 策略字段、绝对可执行文件、argv、环境名称和资源上下限校验。
- Windows Sandbox 配置中所有显式关闭项与只读映射。
- unsupported、初始化失败和不完整能力均不启动普通子进程。
- 诊断不产生副作用、不回显凭据，并区分 disabled/unavailable/incomplete/
  ready。
- 路径越界、符号链接/reparse point、硬链接、敏感目录、文件数量和字节
  上限在准备阶段 fail-closed。
- 超时、取消、输出超限和资源超限映射为稳定终止原因。

专用 Windows 安全 CI 还必须实际验证：

- 沙箱内无法读取宿主 sentinel、`.env`、HOME、SSH 和云凭据。
- 先证明安全 runner 自身可访问外部 IPv4、IPv6 和 DNS，再证明 guest 中
  同一连接全部失败；宿主可达性还需使用 networking-enabled 对照校准的
  host/LAN endpoint，不能把 guest loopback 当作宿主地址。
- 子进程和孙进程在超时后有明确就绪证据且全部消失；取消测试还需先证明
  guest tree 已启动，再确认显式实例和完整进程树都已停止。
- 输出洪泛、单进程/聚合 Job 内存和进程数量分别被限制，宿主工作区不发生
  修改，不能用先触发的单进程上限代替聚合 Job 内存证据。
- junction/reparse point 与并发替换不能逃出快照。
- 不可信命令实际处于 restricted/Low Integrity token，不能写 control/结果、以
  写权限打开 runner，或通过 SCM、Task Scheduler、WMI 和 breakaway 在 Job 外
  创建进程。
- snapshot/control/export 的既有文件在完整执行期不能写入、删除、重命名或替换；
  新条目与句柄关闭失败必须 fail-closed，结果必须从 stop 前封存的同一 identity
  句柄读取。

平台组件不可用时，普通 CI 可以 skip 这些真实隔离测试；安全发布任务必须
提供 `SANDBOX_REQUIRED=1` 一类的强制模式，使 skip 变为失败。

仓库的 `.github/workflows/windows-sandbox-security.yml` 是这一强制任务：
它只接受受保护的 `main` ref，面向带 `windows-sandbox-security` 标签、
受 environment 保护的专用 Windows x64 自托管 runner，禁止并行 WSB 实例，
并运行攻击型 guest probe、快速输出竞态和真实 junction 替换测试。同一
revision 与构建必须串行重复三轮；每轮保存平台、源码/产物、固定测试 manifest、
pytest 退出码、JUnit 和绑定完整 argv/执行身份/完成状态的真实 raw CLI
transcript，再由独立 verifier 检查 repeat、transcript 与执行身份彼此独立，
并验证三轮 platform/subject/schema 一致性。workflow 在随机独占环境中实际
安装并测试唯一 wheel，避免工作树源码覆盖被测产物。任何非零 pytest 退出码、
skip、xfail、xpass、error 或 failure 都会使任务失败。该 runner 应当是
一次性、无仓库外凭据的 Windows 11 Pro/Enterprise 24H2 或更高版本机器；
不能把普通开发机上的 skip 结果当作发布证据。

aggregate 随后由固定 commit 的 `actions/attest` 生成 SLSA/Sigstore provenance；
上传前 verifier 会从三轮 raw JSONL、真实 JUnit、schema、run、构建产物重新
推导 aggregate，并验证固定 repository/workflow/ref/commit。artifact 存在或
上传成功仍不等于认证。

认证必须绑定独立 reviewer、固定十四项 gate 全部关闭、零开放问题、显式 trust
pins 和有效期；review 最晚在证据完成后 7 天内完成，证书不能晚于证据完成后
90 天。默认空 trust 配置必然拒绝。运行时 `ready` 不能由布尔值打开：verifier
还会将完整 bundle 与当前 commit、源码 manifest、OS/WSB/runner/compiler hashes
及协议版本重新绑定。只有全部通过才向 `/doctor` 投影 ready 并注册需要逐次审批
的 `run_command`；该工具只接收相对 `.exe` 和 argv。未声明 `export_paths` 时
所有 guest 修改丢弃；声明后可经二次批准的 `import_guest_export` 写回工作区。

## 开放通用命令前的硬门槛

1. Windows Sandbox 后端必须使用上述目标平台 artifact、Sigstore provenance、
   独立审查 pin 和未过期认证；没有这些材料时工具保持不存在。
2. 审批预览绑定真实可执行文件、argv、逻辑 cwd、后端/策略版本、网络与
   文件权限、资源上限、认证摘要、runner 源码/二进制和确定性快照摘要；host 必须从
   实际 guest request 重新计算 binding，而不是信任调用者提供的摘要。任一
   变化都要求重新批准。
3. 通用能力只接受 argv，不接受 shell 字符串；只在交互审批或非交互 v2
   request/approve 中暴露，v1 始终只读。
4. 首版未声明 `export_paths` 时命令修改全部丢弃。若需导入 guest 生成结果，须调用前声明路径、经 manifest 暂存，并通过 `import_guest_export` 第二次审批与原子写入；见 [`guest-export-import.md`](guest-export-import.md)。
5. 任一平台门禁没有通过时继续使用固定命令白名单，不增加
   `run_shell(command: str)`。
