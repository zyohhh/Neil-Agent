# Web Workbench P9 威胁模型与安全审查

审查版本：3

审查日期：2026-08-24

适用范围：P5 本地发布边界、P8 会话连续性，以及 `feature/web-runtime-model-switching` 新增的 P9 idle-only 同 Provider 模型切换

## 1. 结论

Web Workbench 可以作为仅绑定回环地址的本地应用发布。它不是远程服务，也不因 `localhost` 而被视为天然可信：所有浏览器进入点仍同时受 Host、Origin、一次性 bootstrap、本地会话、CSRF 和短时 WebSocket ticket 约束。前端产物随 wheel 安装，并在服务创建前逐文件校验 SHA-256 清单；校验失败、端口冲突或配置错误时，不启动服务，也不打开携带 bootstrap 的浏览器页。

P8 允许 Web Controller 选择和更新工作区内既有的严格会话快照，但不把消息正文、thinking、Provider 私有状态或质量检查输出加入浏览器协议。切换仅允许控制租约持有者在 idle 状态以精确 revision 执行；跨 Provider/模型私有状态在发网前拒绝。成功 turn 原子保存，失败/取消不写入；保存失败保留旧快照并闭锁后续 turn，直到显式新建或重新选择。

P9 只允许从操作方显式配置的同 Provider、有界模型目录中选择下一 turn 模型。命令还要求当前活动会话为空且未保存；候选 `Settings` 与 worker 全部准备成功后才原子替换，准备阶段不发送网络请求。Session v5 保存 Provider/模型绑定，选择历史时必须完全匹配；发生过运行时切换后，含历史但没有绑定的旧会话 fail closed，因此会话切换不能绕过空会话门禁。

本次审查没有批准远程绑定、PTY、任意 shell、聚合批准、远程字体/脚本、遥测或长期 Web 凭据。若以后加入其中任一能力，必须重新做威胁评审。

## 2. 资产、攻击者与信任边界

需要保护的资产包括工作区文件与 Git 状态、Provider 凭据、prompt、审批预览、Agent 控制权、本地会话和运行输出。Web DTO 只允许传递已定义的有界数据；thinking、API Key、环境变量、完整隐藏工具参数和未授权文件正文不属于浏览器协议。

考虑的攻击者：

- 用户访问的恶意网页，可向本机地址发起 HTTP/WebSocket 请求；
- 尝试 DNS rebinding、伪造 Host/Origin 或跨站请求的远程站点；
- 同一浏览器中的第二个标签页，尝试重放命令或审批；
- 能修改安装目录静态文件或占用监听端口的本机进程；
- 发送畸形、超大、过期或重复协议消息的客户端。

信任边界：浏览器与 loopback HTTP/WS 服务之间、Web Controller 与 Agent/ToolRegistry 之间、工作区路径与宿主文件系统之间、构建产物与已安装 Python 包之间。拥有同一 Windows 用户完整权限的恶意进程、恶意浏览器扩展、已被攻陷的 Python/Node 构建依赖和操作系统本身不在本应用可独立防御的范围内。

## 3. 威胁与控制记录

| 威胁 | 主要控制 | 验证证据 | 残余风险 |
|---|---|---|---|
| DNS rebinding / Host 注入 | 只绑定 `127.0.0.1`；Host 仅允许 `127.0.0.1`、`localhost`；Origin 必须是当前端口的精确 HTTP origin；5173 仅能由显式开发开关加入 | Host/Origin 拒绝测试；自定义端口 origin 由启动器生成 | 同用户恶意进程仍可直接访问回环端口，但拿不到高熵会话凭据 |
| CSRF | 所有非安全 HTTP 方法要求可信 Origin；bootstrap 由高熵单次 secret 保护；ticket 改为 POST，并要求与 HttpOnly 会话绑定的 double-submit CSRF token；cookie 为 `SameSite=Strict` | 缺失/错误 Origin、缺失/错误 CSRF、GET ticket 均被拒绝 | 非 HttpOnly CSRF cookie 可被同源 XSS 读取，因此仍依赖 CSP 与静态完整性 |
| 跨站 WebSocket 劫持 | 精确 Origin + Host；30 秒、单次消费、绑定有效会话的 ticket；连接后仍受单控制租约约束 | origin-less、ticket 重放、ticket 过期测试 | 已获得本地会话的同源恶意代码仍可请求 ticket |
| bootstrap 泄漏/投递到错误进程 | secret 只放 URL fragment，不进入 HTTP 请求；前端读取后立即清除；访问日志关闭；先验证资源和端口，且只有 Uvicorn 自身标记 `started` 后才打开浏览器 | 端口占用时 server/browser 均不启动；控制台不包含 fragment | 浏览器历史恢复、扩展或屏幕录制可能观察短时 fragment |
| 会话/ticket 重放 | bootstrap 单次且 2 分钟；会话仅内存保存且 8 小时；ticket 30 秒、单次；所有比较使用常量时间比较；存储操作由锁串行化 | 并发 bootstrap 仅一个成功；边界时刻过期；ticket 重放失败 | 进程持续运行期间，被窃取的有效 session cookie 在过期前仍有效 |
| XSS、点击劫持和远程依赖注入 | CSP `default-src 'none'`，脚本/样式/连接仅 self，无 `unsafe-inline`；`frame-ancestors 'none'`、`X-Frame-Options: DENY`；无远程脚本、字体、头像或分析 | CSP/安全响应头测试；前端无 inline style；E2E 断言无外部请求 | 浏览器或依赖实现漏洞不由本项目消除 |
| 静态产物被篡改/升级缓存错配 | wheel 内嵌生产资源；确定性 SHA-256 清单；静态目录禁用 Git 换行转换；拒绝额外、缺失、链接或哈希不符文件；index `no-store`，带内容哈希的 assets 长缓存 immutable | 篡改、额外文件、路径逃逸清单测试；wheel 内容与隔离安装验证 | 清单与资源若被同时由同权限攻击者改写，应用级哈希无法代替包签名 |
| 端口抢占 | 启动前独占式探测；secret 在探测后生成；浏览器等待本 Uvicorn 实例 started | 真实监听 socket 的冲突测试 | 探测与正式 bind 间仍有极短竞争；竞争获胜者不会触发本实例 started，因此不会收到浏览器 secret |
| 多标签重复执行/审批 | 单活动 turn、单控制租约、command idempotency、request ID + run ID + revision 绑定、preview 执行前复核；断线/退出 fail closed | P2/P3 多客户端、重复命令、revision、preview 变化和断线测试 | 用户可主动把控制权交给另一个已认证标签页 |
| 未授权、运行中或竞态模型切换 | `switch_model` 要求控制租约、精确 revision、idle、无待审批、空且未保存会话；候选 worker 在锁外准备，提交时再次校验全部状态；失败不改变旧设置/worker | 未列入白名单、运行中、非空、factory 失败、WebSocket 命令与 revision 回归 | 同一已认证控制端可选择操作方预先允许的模型，这是预期能力 |
| 任意或跨 Provider 模型注入 | 候选只来自最多 15 项精确配置；模型 ID 拒绝空白、首尾空格、控制字符和超长值；重建完整 `Settings` 并强制 Provider 身份不变，不接受浏览器提供 endpoint、Provider 或凭据 | 配置边界、目录稳定性和未列入白名单拒绝测试 | 操作方仍需确认同一 Provider endpoint 实际提供并授权候选模型；错误模型会在下一请求由 Provider 正常失败 |
| 会话切换越权或竞态 | `new_session` / `select_session` 仅允许控制端、idle 状态和精确 revision；加载与兼容性预检完成后再次校验；切换清除瞬时 run、timeline、output 与 approval | 控制/revision/run conflict、WebSocket 命令与选择状态测试 | 同一已认证控制端可主动选择工作区内任一有效会话，这是预期本地能力 |
| 会话正文或 Provider 私有状态泄漏 | 浏览器只接收 `ActiveSessionDto`、`SessionDto`、task/usage/review 摘要；`session_changed` 不含 messages、thinking、工具正文或 provider state | DTO 泄漏断言、前端 reducer 契约、跨 Provider 私有状态拒绝测试 | 活动 turn 的流式助手文本和逐工具审批预览仍按既有显式 UI 功能显示 |
| 部分或错误会话写入 | 只在完整成功 turn 后调用现有原子 `SessionStore.save`；失败/取消/关闭不写；save error 保留旧文件并进入 `save_failed` 闭锁 | 成功恢复续跑、失败/取消不覆盖、模拟保存失败与显式恢复测试 | 磁盘故障可能导致本轮结果无法持久化，但不会被声称为已保存 |
| 跨 Provider/模型错误回放 | Session v5 保存运行时绑定；选择时先比较绑定，再逐消息检查 opaque provider state；不兼容即在 Agent/网络构造前拒绝，不剥离私有状态；进程内切换后拒绝含历史的旧未绑定快照 | v1–v4 迁移、绑定保存/分支、provider/model mismatch 和旧会话绕过回归 | 服务重启后旧未绑定、无私有状态历史仍按兼容语义恢复；首次成功 Web turn 会把它升级并绑定 |
| 路径穿越、符号链接与正文外泄 | 工作区 resolve 后再校验；文件树不跟随链接；diff 仅当前 Git 变更集合且 revision 绑定；未跟踪文件正文不返回 | P1/P4 路径、symlink、diff revision 和 untracked 测试 | 校验与文件系统变化间的竞态由底层工具边界继续防守 |
| 超大输入、慢客户端和内存耗尽 | WS 输入帧与应用消息均为 64 KiB；command payload 顶层最多 8 项；prompt、事件、输出、时间线、diff、树、订阅者和队列均有界；`session_changed` 排除完整 output 与嵌套 Git 列表，并在重放窗口中只保留最新一条 | oversized command、slow consumer、session event coalescing、snapshot invalidation 和 DTO 上限测试 | 单机资源耗尽攻击仍可能影响可用性，不影响授权边界 |
| 退出时遗留执行/审批 | FastAPI lifespan 关闭 Controller；关闭会设置取消信号并拒绝待审批；浏览器关闭不被误当成服务退出 | active worker close、审批断线/超时测试 | Provider 或 OS 调用若不支持协作取消，进程退出仍依赖其自身终止行为 |
| 日志/错误泄密 | Uvicorn access log 与 server header 关闭；启动日志不含 token；浏览器错误使用类别信息；协议错误不回显原始 payload | 启动器输出测试、DTO 泄漏断言 | 操作系统级崩溃转储不在应用日志策略范围内 |

## 4. 响应头与协议基线

- CSP：`default-src 'none'`；`script-src/style-src/connect-src 'self'`；禁止 frame、form、object 和 worker。
- 其他响应头：`nosniff`、`no-referrer`、COOP/CORP same-origin、Permissions-Policy deny、`DENY` frame policy。
- API、HTML 与错误响应均为 `Cache-Control: no-store`；只有构建生成的 `/assets/` 内容哈希文件使用 immutable 缓存。
- session cookie 为 HttpOnly + SameSite Strict；CSRF cookie 为 SameSite Strict，且必须同时出现在 `X-Neil-CSRF` 并与 session 服务端记录一致。
- WebSocket 只接受文本 JSON，最大 64 KiB；二进制、畸形、超大或协议不匹配消息不会进入 Controller。

## 5. 发布门禁与复审触发器

发布前必须通过 Python 全量测试、Ruff、mypy、前端 lint/typecheck/unit/build、Chromium E2E/axe、wheel 内容检查和隔离安装验证。安装和运维步骤见 [`web-workbench-operations.md`](web-workbench-operations.md)。

以下变化必须重新审查：监听非回环地址、HTTPS/远程访问、Web 会话删除/导入/导出/分支或浏览器正文读取、运行时 Provider 切换、任意模型输入/自动路由/fallback、用户登录、上传、PTY/shell、浏览器直接选择任意路径、聚合审批、Service Worker、远程资源、插件脚本、遥测或审批协议字段变化。
