# 代码任务评测

默认 `neil-agent-eval` 的六项离线检查验证运行时和协议；`--real-deepseek` 验证在线工具/usage/会话/审批契约。`--coding-tasks` 则让当前配置的 Provider 处理代码任务，由独立功能测试评分。

## 任务与运行

目前内置三个固定小型 Python 任务，所有输入、参考补丁和评分代码随 wheel 分发：

| ID | 目标 | 可修改源码 |
| --- | --- | --- |
| `pagination-boundaries` | 修复一基分页、边界和输入保持 | `pager.py` |
| `slug-collision` | 正确选择最小可用后缀并保留已有集合 | `slugs.py` |
| `config-precedence` | 多文件递归配置合并、无别名污染及显式 false/0/空字符串覆盖 | `config_reader.py`、`cli_options.py` |

先使用参考补丁验收执行链，不读取用户模型配置、不调用网络：

```powershell
uv run neil-agent-eval --coding-tasks --simulate --format json
```

报告明确使用 `mode=simulated`、`provider=fixture`；3/3 表示执行和评分框架可用。真实模型运行需要明确确认 API 费用：

```powershell
uv run neil-agent-eval --coding-tasks --confirm-api-cost --format json
uv run neil-agent-eval --coding-tasks --confirm-api-cost --task config-precedence --trials 2 --format json
```

真实模式使用现有 Provider/模型配置，保留其 endpoint 与凭据选择。最多重复 3 次；每次创建新工作区、新 Agent 和新模型实例，不沿用前一次补丁。`--task` 可筛选内置任务，重复或未知 ID 拒绝执行。代码任务固定使用 `benchmark-minimal`，不接受外部任务清单或更宽的 profile。

## 评分依据

1. 在一次性工作区写入初始源码与约束；评分脚本保存在模型工作区之外。
2. 先运行固定评分脚本，确认原始代码确实失败；基线异常不会创建 Provider 或消耗模型调用。
3. 模型只获得读工具和 `replace_text`。自动批准仅限该任务预先列出的既有源码，仍经过安全内核、指令门禁、审批预览和批准后重校验。评分脚本、项目指令与工作区外文件不获写权限。
4. 模型完成后核对实际文件变化，并运行外部评分脚本；只有允许范围内确有修改且全部功能检查通过才得分。助手声称“已完成/测试通过”不能作为证据。
5. 再检查工作区完整性，记录结果并删除临时工作区。模型失败后仍统计已发生的源码修改。

评分在宿主 Python 子进程执行生成的代码，移除继承凭据和 Python 注入配置，关闭第三方包加载，限制时间与输出。它提供进程级检查边界，**没有 OS 沙箱或网络隔离**；运行真实任务意味着允许在宿主验证生成代码。模型本身没有 shell、质量检查或嵌套子任务工具。

## 资源与结果

每次最多 6 轮工具调用、7 次逻辑模型请求、32 个工具调用声明；每次响应最多 2048 输出 token，上下文使用 40000 字符和 12000 本地估算 token 预算。重试最多一次，单次请求超时最多 30 秒，回合采用 120 秒协作 deadline。SDK 建连阶段仍由请求超时限制。服务端累计报告达到 30000 token 后停止下一次请求；这是停止阈值，不能当作精确费用硬上限，最后一次请求可能跨过阈值。

JSON 报告版本 1 包含：

- Provider/模型、Agent/Python/SDK 版本、执行框架源码哈希与每个任务的哈希；
- 每个任务/重复的状态、耗时、基线结果、评分退出码和实际变化文件；
- 逻辑模型请求、模型声明的工具调用、审批次数与自然重试次数；
- 服务端 usage、是否完整报告、汇总 token 与通过比例。

usage 缺失时为 `null` 或 `usage_complete=false`，不填充估算值冒充实测。`approved_writes`/`denied_writes` 统计审批回调决策，实际修改以 `changed_files` 为准。失败与部分修改仍进入分母；报告不含提示、回答、源码正文、审批预览、异常正文或 API Key。通过比例是当前固定任务集合的功能通过率。

本轮模拟执行记录见 [`acceptance/2026-10-18-coding-simulated.json`](acceptance/2026-10-18-coding-simulated.json)，未记录付费模型成绩。安装后执行链另由 [`installation-acceptance.md`](installation-acceptance.md) 验证。

## 模块边界

`evals.py` 只负责命令模式、费用门禁和输出，保留原有公开入口。`evaluation/catalog.py` 负责离线任务资源，`offline.py` 负责六项协议检查，`acceptance.py` 负责真实 DeepSeek 协议验收，`coding_catalog.py`/`coding.py`/`grading.py` 分别负责代码语料、模型执行和外部评分，`common.py` 共享结果、协议解析与来源指纹，`fixtures.py` 提供旧协议的确定性模型。

Web 的 Agent turn 执行迁入 `web/turn_worker.py`，控制器保留控制租约、命令、审批协调和会话保存职责；原有 `controller.AgentTurnWorker`、`CompletedTurnState` 和 `TurnCancelled` 导入保持兼容。工具装配仍统一由 `host_runtime` 管理。
