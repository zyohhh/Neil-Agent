# Web Workbench 安装与本地运维

P5 将生产前端内嵌到 `neil-agent` Python wheel；P6/P7 更新受审视觉资源、回归门禁和前端故障恢复。已安装的 `neil-agent-web` 不需要 Node.js，也不依赖启动时的当前目录中存在 `web/dist`。wheel 会显式安装实时协议所需的 WebSocket 运行时；若运行时缺失，启动器会在监听端口和生成 bootstrap 前失败关闭。

## 1. 从源码构建 wheel

首次构建需安装 Node 依赖；后续使用锁文件复现：

```powershell
cd web
npm ci
npm run lint
npm run test
npm run build
cd ..
uv build --wheel
```

`npm run build` 会先做 TypeScript 类型检查，再把 Vite 生产产物写入 Python 包目录，并生成确定性的 `asset-manifest.json`。Python 启动器会在监听端口前核对清单、文件集合和每个 SHA-256。

## 2. Windows 安装与启动

推荐作为独立工具安装本地 wheel：

```powershell
uv tool install --force .\dist\neil_agent-0.1.0-py3-none-any.whl
neil-agent-web --version
neil-agent-web
```

默认监听 `http://127.0.0.1:8765/` 并在服务真正取得端口后打开浏览器。使用其他端口或不自动打开浏览器：

```powershell
neil-agent-web --port 8877
neil-agent-web --no-browser
```

模型、API Key 与工作区仍使用 Neil Agent 的现有环境变量/`.env` 配置。启动日志不会打印 bootstrap secret 或 WebSocket ticket；bootstrap 只短时放在自动打开页面的 URL fragment 中，交换后立即从地址栏移除。

## 3. 停止、端口冲突与恢复

- 在启动服务的终端按 `Ctrl+C` 停止。正常停止返回成功状态；服务最多等待 10 秒完成优雅关闭，并会取消活动 turn，使待审批、session 和 ticket 全部失效。
- 关闭浏览器标签页不会停止本地进程；需要在终端停止。
- 若端口已占用，启动器会在创建/交付 bootstrap 之前退出并提示选择其他 `--port`，不会打开指向占用进程的浏览器。
- 若提示静态资源缺失或完整性失败，不要绕过检查；停止进程并从可信 wheel 强制重装。

## 4. 升级与回退

先停止旧进程，再安装目标 wheel：

```powershell
uv tool install --force .\dist\neil_agent-0.1.1-py3-none-any.whl
neil-agent-web --version
```

HTML 使用 `no-store`，JS/CSS 文件名包含内容哈希，因此重启并刷新后不会把新 HTML 与旧资源静默混用。需要回退时，对已审核的旧 wheel 执行相同的 `--force` 安装；每个版本都独立验证其资源清单。

## 5. 卸载

```powershell
uv tool uninstall neil-agent
```

卸载工具不会删除用户工作区。若操作方另外创建了 `.env`、费率表或 Neil Agent 会话目录，应按其各自的数据保留策略单独处理；不要用递归删除工作区作为卸载步骤。

## 6. 开发模式与发布模式

- `npm run dev` 是源码开发服务器，不能作为发布安装方式。需要连接真实本地 API 时，以 `uv run neil-agent-web --allow-vite-dev-origin --no-browser` 显式允许固定的 5173 loopback Origin；发布启动不要开启该选项。
- `npm run build` 生成发布资源；wheel 构建前必须执行。
- 发布运行只使用 wheel 内的资源，不从 CDN、远程字体、分析服务或仓库外路径加载前端代码。
- PTY、任意 shell 和聚合 `Approve & Apply` 不属于 P0–P7。
