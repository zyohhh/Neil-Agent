# 代码任务评测

默认 `neil-agent-eval` 的六项离线检查验证运行时和协议；`--real-deepseek` 验证在线工具/usage/会话/审批契约。`--coding-tasks` 则让当前配置的 Provider 处理代码任务，由独立功能测试评分。

## 任务与运行

目前内置六个固定 Python 任务、69 个声明式功能用例，所有输入、参考补丁和用例随 wheel 分发。其中前三项为合成任务，后三项从本仓库已提交的回归契约改编；报告记录来源提交与文件。这些改编 fixture 不运行整个仓库，不等同于外部真实 issue、SonarQube 或 SWE-bench 评测。

| ID | 目标 | 可修改源码 |
| --- | --- | --- |
| `pagination-boundaries` | 修复一基分页、边界和输入保持 | `pager.py` |
| `slug-collision` | 正确选择最小可用后缀并保留已有集合 | `slugs.py` |
| `config-precedence` | 多文件递归配置合并、无别名污染及显式 false/0/空字符串覆盖 | `config_reader.py`、`cli_options.py` |
| `terminal-state-history` | 不完整终态拒绝、待处理工具调用不提交历史、历史不变及无别名 | `terminal.py`、`conversation.py` |
| `session-revision-save` | 规范摘要、旧版本/删除/重命名冲突和模型绑定检查 | `versions.py`、`store.py` |
| `approval-context-consume` | 完整审批上下文绑定、过期和已消费拒绝 | `binding.py`、`approval.py` |

先使用参考补丁验收执行链，不读取用户模型配置、不调用网络：

```powershell
uv run neil-agent-eval --coding-tasks --simulate --format json
```

报告明确使用 `mode=simulated`、`provider=fixture`；6/6 表示参考补丁执行和评分框架可用，不是模型修复成功率。真实模型运行需要明确确认 API 费用：

```powershell
uv run neil-agent-eval --coding-tasks --confirm-api-cost --format json
uv run neil-agent-eval --coding-tasks --confirm-api-cost --task config-precedence --trials 2 --format json
```

真实模式使用现有 Provider/模型配置，保留其 endpoint 与凭据选择。最多重复 3 次；每次创建新工作区、新 Agent 和新模型实例，不沿用前一次补丁。`--task` 可筛选内置任务，重复或未知 ID 拒绝执行。代码任务固定使用 `benchmark-minimal`，不接受外部任务清单或更宽的 profile。

## 评分依据

1. 在一次性工作区写入初始源码与约束；功能用例的期望值保留在父进程。
2. 先调用独立 worker，确认原始代码产生完整调用证据且不满足功能断言；worker 崩溃、缺少证据或原始代码已通过均不会创建 Provider 或消耗模型调用。
3. 模型只获得读工具和 `replace_text`。自动批准仅限该任务预先列出的既有源码，仍经过安全内核、指令门禁、审批预览和批准后重校验。评分脚本、项目指令与工作区外文件不获写权限。
4. 模型完成后核对实际文件变化，worker 仅调用目标函数并回传结构化结果；父进程检查本次 challenge、完整用例数量、返回值、预期异常、参数保持及结果与输入的可变对象别名。只有允许范围内确有修改且全部断言通过才得分。
5. 再检查工作区完整性，记录结果并删除临时工作区。模型失败后仍统计已发生的源码修改。

完成标记和进程退出码不再直接决定通过：源码只打印 `NEIL_GRADER_COMPLETED`、伪造标记或提前退出会因调用证据缺失而失败。断言和最终通过结果由父进程生成，不导入模型生成的源码。参考补丁验收覆盖相同用例，且保持 `mode=simulated`。

worker 在宿主 Python 子进程执行生成代码，移除继承凭据和 Python 注入配置，关闭第三方包加载，限制时间与输出，并清理子进程树，见 [`host-processes.md`](host-processes.md)。它**没有 OS 沙箱或网络隔离**；同用户恶意代码仍可访问宿主资源，结构化证据不能替代 OS 安全隔离。运行真实任务意味着允许在宿主验证生成代码。模型本身没有 shell、质量检查或嵌套子任务工具。

## 资源与结果

每次最多 6 轮工具调用、7 次逻辑模型请求、14 次模型/SDK 尝试和 32 个工具调用声明；每次响应最多 2048 输出 token，上下文使用 40000 字符和 12000 本地估算 token 预算。重试最多一次，单次请求超时最多 30 秒，Agent 使用 120 秒协作 deadline。共享尝试、已报告 token 和时限取用户设置与评测上限中的较小值；SDK 超时受剩余 deadline 约束。累计报告达到 30000 token 后停止下一次请求，并在工具后续门禁检查；这是停止阈值，不能当作精确费用硬上限，最后一次响应可能跨过阈值。预算与停滞语义见 [`turn-limits.md`](turn-limits.md)。

JSON 报告版本 2 包含：

- Provider/模型、Agent/Python/SDK 版本、执行框架源码哈希与每个任务的哈希；
- 每个任务/重复的状态、耗时、基线结果、评分退出码和实际变化文件；
- 逻辑模型请求、模型声明的工具调用、审批次数与重试次数；
- 共享账本的 `model_attempts`、`unreported_attempts`、`aggregate_usage`、`aggregate_usage_complete` 和 `budget_stop_reason`，区分逻辑请求与重试尝试；
- 每项任务的功能用例数量；改编任务的 `origin` 明确标记 `repository-regression`/`adapted`，附来源提交和文件；
- 服务端 usage、是否完整报告、汇总 token 与通过比例；`loop_detected` 单独区分停滞退出。

usage 缺失时为 `null` 或完整性为 false，不填充估算值冒充实测。每项原有 `usage`/`usage_complete` 保留逻辑终态口径；版本 2 顶层 `reported_usage`/`usage_complete` 从共享账本汇总，覆盖 SDK 失败/重试，不能与旧 subtotal 相加。`approved_writes`/`denied_writes` 统计审批回调决策，实际修改以 `changed_files` 为准。失败与部分修改仍进入分母；报告不含提示、回答、源码正文、审批预览、异常正文或 API Key。通过比例是当前固定 fixture 集合的功能通过率。

本轮模拟执行记录见 [`acceptance/2026-10-18-coding-simulated.json`](acceptance/2026-10-18-coding-simulated.json)，未记录付费模型成绩。安装后执行链另由 [`installation-acceptance.md`](installation-acceptance.md) 验证。

评分与执行边界修复后的模拟记录见 [`acceptance/2026-10-18-execution-hardening-coding-simulated.json`](acceptance/2026-10-18-execution-hardening-coding-simulated.json)，采用新的任务与 harness 指纹，仍不表示真实模型成绩。

2026-11-02 的六任务/共享预算模拟验收见 [`acceptance/2026-11-02-turn-limits-coding-simulated.json`](acceptance/2026-11-02-turn-limits-coding-simulated.json)。旧报告保留原始 schema 和三任务基线，不与新语料混算。

## 模块边界

`evals.py` 只负责命令模式、费用门禁和输出，保留原有公开入口。`evaluation/catalog.py` 负责离线任务资源，`offline.py` 负责六项协议检查，`acceptance.py` 负责真实 DeepSeek 协议验收，`coding_catalog.py`/`coding.py`/`grading.py` 分别负责代码语料、模型执行和父进程评分；`grading_worker.py` 只执行函数调用并报告结果。`common.py` 共享结果、协议解析与来源指纹，`fixtures.py` 提供旧协议的确定性模型。

Web 的 Agent turn 执行迁入 `web/turn_worker.py`，控制器保留控制租约、命令、审批协调和会话保存职责；原有 `controller.AgentTurnWorker`、`CompletedTurnState` 和 `TurnCancelled` 导入保持兼容。工具装配仍统一由 `host_runtime` 管理。
