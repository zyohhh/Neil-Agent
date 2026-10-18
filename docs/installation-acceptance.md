# 安装包验收

`neil-agent-install-check` 对一个明确指定的本地 wheel 创建全新虚拟环境，并在不含项目源码的目录运行验收。默认使用系统临时目录，不复用当前 `.venv`，不启用 system site-packages，不使用 editable 安装，不跳过依赖。临时环境在结束后删除，不改变已安装的用户工具。

```powershell
uv build --wheel
uv run neil-agent-install-check --wheel .\dist\neil_agent-0.1.0-py3-none-any.whl --format json
```

默认禁止依赖下载；离线缓存必须包含所需 wheel 与索引信息。缓存使用其他索引时可明确指定：

```powershell
uv run neil-agent-install-check --wheel .\dist\neil_agent-0.1.0-py3-none-any.whl --index-url https://pypi.tuna.tsinghua.edu.cn/simple --format json
```

需要下载依赖时添加 `--allow-network-install`，只开放安装依赖所需的网络。模型 API 在安装验收中始终不调用。工具要求本机存在 `uv` 和当前可用的 Python，不自动下载解释器。`--work-dir <已存在目录>` 可指定临时环境的父目录。

## 检查内容

| 检查 | 验收依据 |
| --- | --- |
| wheel 元数据 | 包名、版本和必需评测/前端资源存在，记录 wheel SHA-256 |
| 全新环境与完整依赖 | 实际 `uv venv`、安装 wheel 与依赖、`uv pip check` |
| 四个命令入口 | CLI、eval、Web、install-check 的实际安装脚本均能运行 `--help` |
| 已安装资源 | 独立解释器核对包来自新环境、版本一致、两类任务目录和前端清单完整 |
| 默认评测 | 控制台入口和 `python -I -m neil_agent.evals` 均输出真实非空 JSON，6 项通过 |
| 代码任务框架 | 安装后运行 3 个任务的确定性参考补丁流程，明确标记 `simulated` |
| API 费用门禁 | 两种真实评测模式缺少费用确认时必须以退出码 2 拒绝 |
| Web 本地服务 | 启动已安装的 launcher，确认 `ready` 健康协议、HTML 安全头、匿名 snapshot 返回 401，再正常停止 |

共 15 项。进程退出码为 0 不足以让评测通过：JSON 必须声明成功、任务数量非零且符合预期。安装失败后的依赖检查标记 `skipped`，不会视为通过。验收自身失败以非零退出码返回。记录包含安装后的 Python 和关键依赖版本；SDK 与配置库声明明确的大版本边界，开发锁文件保持原有依赖版本。

子进程移除模型凭据、`PYTHONPATH`、`VIRTUAL_ENV` 与继承的索引注入配置，使用固定命令与有界运行时间、输出；报告不打印安装器原始输出、bootstrap、用户配置或临时目录。Web 探针直接连接固定 loopback 地址并禁用 HTTP 代理。

评测清单位于 `neil_agent/evaluation/` 包内，通过 `importlib.resources` 读取。`evals/tasks.json` 已迁移为 `src/neil_agent/evaluation/tasks.json`，安装后的默认命令无需仓库目录。质量检查调用的 `python -m neil_agent.evals` 与控制台脚本执行同一入口。

本轮实际安装验收记录见 [`acceptance/2026-10-18-installation.json`](acceptance/2026-10-18-installation.json)。代码任务方法与真实运行方式见 [`coding-evaluations.md`](coding-evaluations.md)。

评分、子进程生命周期和执行独占修复后的新 wheel 验收记录见 [`acceptance/2026-10-18-execution-hardening-installation.json`](acceptance/2026-10-18-execution-hardening-installation.json)。验收调用的固定命令使用 [`host-processes.md`](host-processes.md) 中的进程树执行器，生成代码的 OS 隔离仍待补齐。
