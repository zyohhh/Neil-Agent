# Neil Agent 简历项目表述

以下表述依据当前代码与离线验收。职责动词按个人实际分工调整。完整门禁为 1120 passed、12 skipped、16 deselected；六任务参考补丁模拟与 15 项全新环境安装验收单独记录，不能混成真实模型修复成功率。

## 可直接使用的版本

**项目：Neil Agent，本地 Coding Agent**

项目简介：面向本地软件开发的 Coding Agent，支持自然语言驱动代码检索、文件修改、质量检查与 Git 操作，提供 CLI、Textual 终端工作台及本地 Web 工作台，支持多模型接入、上下文摘要、会话恢复和人工审批。

技术栈：Python 3.13、Anthropic/OpenAI SDK、Pydantic、Typer、Rich、Textual、FastAPI、WebSocket、pytest、uv。

- 设计统一 Model Provider 适配层，接入 DeepSeek、Claude、OpenAI 及 Ollama/vLLM 本地部署，封装 Anthropic Messages 与 OpenAI Responses 的消息、流式事件、工具调用和异常差异；通过能力声明和 Provider/模型身份绑定校验请求及历史重放。
- 实现 ReAct Agent Runtime，完成 LLM 推理、Tool Call 解析、ToolRegistry 执行与 Observation 回填，通过 `tool_call_id` 关联请求和结果；为父任务、只读子任务和 SDK 重试设置共享请求/token/deadline 预算，结合重复结果检测与终态校验约束执行循环。
- 实现边界感知的上下文摘要，按字符/token 压力和轮数上限压缩早期历史，保留最近完整对话及 Tool Call/Result 配对；将摘要准备与历史提交分离，摘要计入共享预算，任务失败或取消时丢弃候选，检查点随版本化会话保存恢复。
- 设计 Agent 安全执行机制，通过工作区路径边界、敏感路径拦截及 Git 输出过滤约束工具访问；高风险操作先展示预览并请求人工审批，执行前重新校验预览和项目指令，非交互审批绑定上下文并采用一次性消费。
- 实现执行记录与会话并发保护，在副作用工具执行前持久记录操作事实，支持失败或重启后的人工核对；通过跨进程工作区独占锁和会话版本比较，拒绝旧会话覆盖与并发执行，并为受控空会话模型切换实现候选准备、状态复核和失败清理。
- 搭建 Coding Agent 离线回归与分发验收流程，构建 6 个固定代码任务、69 个父进程功能用例，记录任务来源、框架指纹及模型尝试/usage；累计通过 1120 项离线测试及 15 项全新环境 wheel 安装验收，真实模型评测通过显式费用确认入口开启。

一页简历可优先选运行时、上下文、安全、执行一致性四条；应聘模型平台方向补 Provider，应聘 Agent 评测方向补评测。技术点应能结合代码和失败用例口述。

## 图中表述如何采用

| 图中内容 | 当前实现与可用表述 | 代码与验证 |
| --- | --- | --- |
| ReAct、多轮工具、`tool_call_id`、迭代预算 | 可直接借用核心表达；本项目增加父子任务/重试共享账本、失败 usage 完整性与循环检测 | [agent.py](../src/neil_agent/agent.py)、[turn_budget.py](../src/neil_agent/turn_budget.py)、[loop_guard.py](../src/neil_agent/loop_guard.py)、[测试](../tests/test_turn_budget.py) |
| 长上下文摘要、保留最近完整轮次及调用/结果配对 | 可采用；本轮补齐显式开启后的自动压力触发、候选提交和失败保护 | [compaction.py](../src/neil_agent/compaction.py)、[说明](automatic-compaction.md)、[测试](../tests/test_auto_compaction.py) |
| 统一 Provider、多模型流式与异常处理 | 可采用；模型与协议按本项目实际列表写，客户端是同步 SDK，不写 `AsyncClient + async/await` 非阻塞推理 | [providers/](../src/neil_agent/providers/)、[工厂](../src/neil_agent/providers/factory.py)、[契约测试](../tests/test_provider_contract.py) |
| Path Guard、人工审批、工具审计 | 可采用相应能力；更具体的是批准后预览/指令重校验、一次性审批及写前执行事实记录 | [approval.py](../src/neil_agent/approval.py)、[执行恢复](execution-recovery.md)、[安全加固](security-hardening.md) |
| 工具只读/并发安全属性及并行调度 | 尚无该调度器，工具仍串行；Provider 支持多个调用声明不等于本地并发执行 | [注册表](../src/neil_agent/tools/registry.py)、[Agent](../src/neil_agent/agent.py) |
| Planner、SubAgent、DAG 并行依赖调度 | 有可见任务计划、同步只读子任务和事件 DAG；不能写成通用多 Agent DAG 调度器 | [subtask.py](../src/neil_agent/subtask.py)、[运行时说明](runtime-profile.md) |
| MCP SDK、stdio/HTTP 外部工具 | 未实现；可写按 RuntimeProfile 装配内置工具与按需 Skills，不能用 Skill 替代 MCP 的技术含义 | [host-runtime.md](host-runtime.md)、[skills.py](../src/neil_agent/skills.py) |
| SQLite、BM25/向量检索长期记忆 | 未实现；当前是版本化 JSON 会话、检查点和上下文摘要，不写检索式长期知识库 | [session.py](../src/neil_agent/session.py)、[context.py](../src/neil_agent/context.py) |
| SonarQube、SWE-bench、105 缺陷、84.8%/64.8% | 不能采用这些平台经历与数字；替换成六任务/69 功能用例、改编来源和实际安装门禁 | [评测方法](coding-evaluations.md)、[模拟报告](acceptance/2026-11-02-auto-compaction-coding-simulated.json)、[安装报告](acceptance/2026-11-02-auto-compaction-installation.json) |
| Prefix Cache、百万 token 优化与成本降低 | 有缓存 usage 字段和上下文估算，不足以证明该缓存优化和收益，不写百万 token 实测或成本下降比例 | [预算语义](turn-limits.md)、[schemas.py](../src/neil_agent/schemas.py) |

## 项目更适合强调的部分

工作区锁覆盖执行到会话保存，避免先产生副作用再发现旧会话无法保存；会话锁内比较版本并原子替换，防止旧句柄覆盖更新或复活已删除会话。执行日志与成功对话历史分开保存，失败回合的实际文件修改仍有核对依据。这些机制比单纯罗列工具数量更容易展开技术讨论。

自动摘要与工作回合一起提交，可以解释半截流、模型截断、取消、坏摘要、超预算候选以及准备期间历史变化各自怎样处理。预算涵盖 SDK 隐藏重试，未知 usage 明示，也有实际 SDK 加本地 HTTP mock transport 的请求次数验证。现有证据支持这些工程行为，不能由此推断竞品没有同类实现或总体性能更低。

## 表述初稿与复核

初稿：统一适配五种 Provider 并完善流式与异常处理；实现 ReAct 工具循环及多维预算；基于阈值摘要历史并保留协议边界；通过人工审批和路径约束保护执行；增加持久执行记录和会话并发控制；以固定功能用例和新环境安装检查验证交付。

复核：

- 将笼统的“完善”和“保护”替换为预览/指令重校验、一次性消费、写前记录、锁内版本比较等具体行为。
- Provider 数量包括本地部署适配，未声称五家云模型；没有添加异步推理、MCP、BM25 或 DAG 调度等未实现能力。
- 仅保留可复核的测试、用例和安装检查数量，没有把参考补丁模拟写成模型能力提升；最终正文保留在上面的“可直接使用的版本”。
