# 子进程 OS 沙箱评估（2026-07-22）

## 结论

Neil Agent 当前不应开放任意 shell。现有 `shell=False`、固定参数、最小环境、超时、输出上限和逐次审批能够缩小应用层攻击面，但不能限制一个已经启动的进程访问用户文件、凭据、网络或其他进程，因此不能称为 OS 沙箱。

## 平台判断

### Windows

- [Microsoft AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer) 能以低完整性令牌、SID/DACL 和显式 capability 限制文件、注册表、设备、进程及网络访问，符合“仅工作区可写、默认无网络”的目标，但需要创建 profile、配置 ACL/capability 和专用进程启动属性，不是 `subprocess` 的一个开关。
- [Windows Sandbox](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/) 使用 Hyper-V 隔离的独立内核，隔离更强，但启动成本、文件映射、工具安装和结果回传更重，更适合作为可选高隔离执行后端，而不是每个短命令的默认实现。
- Job Object、超时和隐藏窗口可帮助管理进程生命周期与资源，但单独不能提供所需的文件系统和网络权限边界。

### Linux

- [bubblewrap](https://github.com/containers/bubblewrap) 可以用 user/mount/PID/network namespace 构造最小可见文件系统，也可组合 seccomp；但官方明确说明它是构造沙箱的底层工具，真正安全性取决于调用方策略和参数。
- 可行策略是只读挂载运行时和依赖、仅把工作区绑定为所需读写模式、使用临时 HOME、默认断网、隐藏凭据目录，并限制进程/输出/时间。不同发行版对非特权 user namespace 的支持仍需启动时探测。

## 开放通用命令前的硬门槛

1. 定义平台无关策略：工作区读写范围、默认网络策略、环境变量、可执行文件来源、资源上限和子进程树终止语义。
2. Windows 至少有 AppContainer/LPAC 级后端，Linux 至少有经过测试的 namespace + seccomp 后端；不支持的平台必须 fail closed，不能静默退化成普通进程。
3. 增加逃逸回归：绝对/父路径、符号链接、HOME/SSH/云凭据读取、网络连接、子进程遗留、超时、超量输出和信号中断。
4. 审批预览必须展示实际执行文件、参数、工作目录、文件/网络权限和沙箱后端；批准内容变化后必须重新确认。
5. 沙箱实现需独立安全审核。在上述条件完成前继续使用固定命令白名单，不增加 `run_shell(command: str)`。
