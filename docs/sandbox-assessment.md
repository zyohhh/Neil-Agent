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
- **网络**：默认且当前唯一允许的策略是 `deny`；IPv4、IPv6 和 localhost
  都属于目标平台验收范围。
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

### 当前 Windows 实现边界

仓库中的 Windows 适配层负责：

1. 只读探测宿主平台和 Windows Sandbox CLI/可选组件。
2. 校验平台无关策略，并生成固定的高隔离 `.wsb` 配置。
3. 在任何能力缺失时通过统一沙箱异常拒绝，且没有普通进程 fallback。
4. 向 `/doctor` 提供结构化状态，不显示环境变量值、命令内容或敏感路径。

目标机器没有可用 Windows Sandbox 组件时，平台集成验收会明确跳过，同时
诊断报告 `unavailable`。这种跳过不能用于把后端标成 `ready`。

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
- IPv4、IPv6、DNS 与 localhost 网络连接均失败。
- 子进程和孙进程在超时/取消后全部消失。
- 输出洪泛、内存和进程数量被限制，宿主工作区不发生修改。
- junction/reparse point 与并发替换不能逃出快照。

平台组件不可用时，普通 CI 可以 skip 这些真实隔离测试；安全发布任务必须
提供 `SANDBOX_REQUIRED=1` 一类的强制模式，使 skip 变为失败。

## 开放通用命令前的硬门槛

1. 至少一个 Windows 或 Linux 执行后端在目标平台通过上述真实隔离测试，
   并有独立安全审查记录。
2. 审批预览绑定真实可执行文件、argv、逻辑 cwd、后端/策略版本、网络与
   文件权限、资源上限和确定性快照摘要；任一变化都要求重新批准。
3. 通用能力只接受 argv，不接受 shell 字符串；只在交互审批或非交互 v2
   request/approve 中暴露，v1 始终只读。
4. 首版命令修改全部丢弃。若要导入生成结果，必须先扫描有界 diff，再经过
   第二次审批和现有文件检查点机制。
5. 任一平台门禁没有通过时继续使用固定命令白名单，不增加
   `run_shell(command: str)`。
