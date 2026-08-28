# Web Workbench 基本验收记录

> **历史记录**：基线提交 `eba82fd`、分支 `feature/web-workbench`、复验 2026-08-19。当前主线见 [`project-status.md`](project-status.md) 与 `main` 最新提交；本文保留合并前验收证据，不作为当前行为规格。
>
> 验收结论：**GO（发布阻塞项已关闭）**
>
> 基线提交：`eba82fd84885827406a8e3bd8c3810d51f8a3fc7`
>
> 整改状态：修复与本记录作为一个独立提交交付
>
> 分支：`feature/web-workbench`
>
> 复验日期：2026-08-19

## 1. 验收范围

本轮是合并前的基本验收与阻塞项复验，覆盖：

- 前端 lint、类型、组件测试、E2E、axe 与视觉基线；
- Python lint、格式、类型、全量测试与离线 Agent 评估；
- wheel 构建、Python 3.13 隔离安装、入口版本和内嵌资源完整性；
- 发布服务健康接口、缓存、CSP、Host/Origin、未认证和路径边界；
- bootstrap、WebSocket ticket、真实 upgrade、控制租约、刷新与重载；
- 空闲、活动连接、活动 worker、待审批与断线恢复状态下的关闭边界；
- 启动凭据和 WebSocket ticket 的终端日志泄漏检查。

本轮不调用付费模型，不提交真实 Agent turn，不批准真实高风险工具，也不验收尚未立项的 PTY 或 Focus/Build 权限语义。P9 同 Provider 运行时模型切换已纳入常规 Web 回归范围。

## 2. 验收结果

| 检查 | 结果 | 证据摘要 |
| --- | --- | --- |
| 前端 lint | PASS | `npm run lint` |
| 前端组件测试 | PASS | 2 个文件、8 个测试 |
| TypeScript 与生产构建 | PASS | 22 modules；3 个清单资源 |
| 浏览器 E2E | PASS | 5 个测试；1440/1280/768/390/320 px |
| 无障碍与视觉回归 | PASS | axe 无 critical/serious；4 张 P6 基线一致 |
| Python 静态门禁 | PASS | Ruff、format、mypy 全部通过 |
| Python 全量测试 | PASS | 722 passed、23 skipped |
| 离线 Agent 评估 | PASS | 5/5 |
| wheel 构建 | PASS | `neil_agent-0.1.0-py3-none-any.whl` |
| 隔离安装 | PASS | Python 3.13.13；共安装 42 个包（含项目）；入口版本 0.1.0 |
| WebSocket 运行时 | PASS | wheel 显式安装 `websockets` 17.0.1；启动器固定 `websockets-sansio` |
| 内嵌静态资源 | PASS | 3 个资源、267424 bytes，通过 SHA-256 清单验证 |
| HTTP 发布冒烟 | PASS | health/root 200；snapshot 未认证 401 |
| 安全响应头 | PASS | HTML/API no-store；CSP 生效；哈希资源 immutable |
| 固定安全边界 | PASS | Host 400；恶意 Origin 403；编码路径穿越 404 |
| 端口冲突 | PASS | 第二实例安全拒绝，未占用未知服务 |
| 已安装实时 UI | PASS | live、控制租约、模型、fragment 清除、刷新和重载均通过 |
| 浏览器运行边界 | PASS | 无页面异常、外部请求或页面级水平溢出 |
| 服务停止 | PASS | 正式 CLI 单次 Ctrl+C 返回 0；无 traceback、监听或残留进程 |
| 日志凭据边界 | PASS | bootstrap 和已消费 WebSocket ticket 均不出现在终端输出 |

现有 Python 测试仍产生一条 Starlette/httpx2 迁移提示；它不影响本轮结果，应在依赖维护阶段处理。

## 3. 阻塞项复验

### BA-01：wheel 缺少 WebSocket 运行实现

状态：**CLOSED**

整改与证据：

1. 发布依赖显式声明 `websockets>=13`，锁文件和干净 wheel 安装解析为 17.0.1；
2. 启动器在生成 bootstrap、监听端口前检查 WebSocket 运行时，缺失时 fail closed；
3. Uvicorn 明确使用 `websockets-sansio`，避免环境相关的协议实现选择；
4. 全新隔离环境完成 bootstrap、CSRF、单次 ticket、真实 WebSocket upgrade、控制租约、刷新与页面重载；
5. 页面稳定进入 `P7 live Agent`，bootstrap fragment 被移除，无页面异常或外部请求。

因此健康接口的 `realtime=true` 现在与已安装 wheel 的实际能力一致。

### BA-02：Ctrl+C 停止路径不干净

状态：**CLOSED**

整改与证据：

1. 启动器把 Uvicorn 完成优雅关闭后重新抛出的 `KeyboardInterrupt` 作为正常操作员停止处理；
2. Uvicorn 优雅关闭上限固定为 10 秒，避免连接或后台任务无限等待；
3. 自动化覆盖正常中断、真实 loopback 服务、活动 worker 取消，以及服务关闭时待审批 fail closed；
4. 保持活动 WebSocket 时停止服务，页面进入 offline/reconnecting，无页面异常，端口和进程立即释放；
5. 最终 wheel 的正式 `neil-agent-web.exe` 入口在 Windows `cmd` PTY 中单次 Ctrl+C 于约 0.2 秒返回 0，无 traceback 和残留进程。

PowerShell 包装 `python -c` 的验收辅助进程会把送给整条管线的 Ctrl+C 记为外层退出码 1；正式 console-script 入口已独立复验为 0，因此不把包装器状态误记为产品退出码。

### BA-03：复验中发现 WebSocket ticket 进入 INFO 日志

状态：**CLOSED**

Uvicorn 的 WebSocket INFO 消息包含完整请求目标，可能把短时、单次 ticket 写入终端。发布配置现在使用 warning 级协议日志，同时保留不含凭据的启动摘要。真实服务和自动化测试均确认 ticket 不出现在 stdout/stderr。

## 4. 验收环境说明

应用内浏览器控制组件因本机插件信任路径错误无法连接。本轮使用仓库锁定的 Chromium/Playwright 完成等价 UI 冒烟；这是验收工具限制，不是产品缺陷。

复验使用系统临时目录中的全新 Python 3.13.13 环境，只安装最终 wheel 及其声明依赖，共 42 个包（含项目）。两套验收环境及其中已消费的临时启动票据均已删除，监听端口和验收进程均已清理。验收阶段未执行合并或推送；后续 Git 交付状态以仓库记录为准。

## 5. 结论

P0–P7 的前端、实时协议、发布 wheel、安全边界和退出生命周期已满足本轮基本验收。BA-01、BA-02 以及复验中新发现的日志凭据问题均已关闭，当前整改工作树从技术验收角度为 **GO**。

本记录支持将修复作为独立整改提交纳入代码审查和合并流程；具体提交、合并与推送状态以仓库记录为准。
