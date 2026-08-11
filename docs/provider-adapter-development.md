# 多 LLM Provider 协议适配器开发计划

## 1. 文档定位

本文档规划 Neil Agent 的多 LLM Provider 协议适配层，仅覆盖 Provider 抽象、协议转换、配置、错误语义和契约测试。

- 开发分支：`feature/provider-runtime`
- 当前阶段：Phase 2 已完成，准备进入 Phase 3
- 目标 Provider：DeepSeek、Claude、OpenAI、Ollama、vLLM
- 暂不纳入：MCP、评测基准、模型路由与自动降级、可视化、Windows Sandbox 后续主线

这里的“多 Provider”不等于为每个品牌复制一套客户端。计划将五个运行目标收敛为三类线协议：

| 用户侧 Provider | 线协议适配器 | 说明 |
| --- | --- | --- |
| DeepSeek | Anthropic Messages | 保留当前 Anthropic-compatible 接入方式，作为兼容性基线 |
| Claude | Anthropic Messages | 使用 Anthropic 原生能力，复用消息、工具和流式事件转换核心 |
| OpenAI | OpenAI Responses | 使用官方 Responses API，不以旧式 Chat Completions 作为核心抽象 |
| Ollama | OpenAI-compatible | 通过可配置 `base_url` 和显式能力档案接入 |
| vLLM | OpenAI-compatible | 与 Ollama 共享传输实现，但拥有独立能力档案 |

“本地模型”是部署方式，不是一种独立协议。Ollama 和 vLLM 可以共享 OpenAI-compatible 适配器，但不能默认它们完整实现 OpenAI 的全部语义。

## 2. 当前代码基础与剩余问题

现有架构保持了正确的依赖方向：`Agent` 依赖 `ChatModel` Protocol，而不是具体 SDK。Phase 0～2 已完成 Provider 身份/能力/错误/重试契约、条件配置、Anthropic Messages 编解码边界，以及 DeepSeek/Claude 两套运行实现。

当前剩余工作包括：

1. OpenAI Responses 尚未形成独立编解码与流式状态机，不能通过 Anthropic 结构模拟。
2. Ollama 与 vLLM 尚未建立保守的独立 capability profile 和请求前能力拒绝。
3. `/doctor` 尚未完整展示脱敏的 Provider、模型、endpoint 与能力快照。
4. DeepSeek/Claude 目前只有离线合成 fixture；受控在线 smoke test 仍需显式凭据、费用确认和单独记录。
5. 旧 `LLMClient` 仍作为 DeepSeek 兼容 facade，后续需要给出明确迁移期限。

后续阶段继续围绕稳定核心语义和真实协议差异推进，不为目录整齐复制 SDK wrapper。

## 3. 设计目标

### 3.1 必须达成

- `Agent` 继续只依赖 `ChatModel`，核心循环不出现 Provider 分支。
- 每个适配器将 Provider 请求和响应转换为项目统一领域模型。
- Provider 能力可查询、可测试；不支持的能力在发送请求前明确失败。
- DeepSeek 现有行为和默认配置保持兼容，并提供清晰迁移路径。
- 文本、工具调用、停止原因、usage、流式完成状态使用统一语义。
- 错误被归一化为稳定的项目级异常，不让 SDK 异常穿透到 Agent。
- 默认测试不访问公网、不消耗付费额度；在线 smoke test 必须显式开启。

### 3.2 本阶段不做

- 不实现跨 Provider 的自动 fallback、负载均衡或按成本路由。
- 不在一次 Agent turn 执行中途切换 Provider。
- 不承诺跨 Provider 搬运私有推理状态。
- 不把所有 Provider 压缩为“最低公共能力集合”。
- 不把 OpenAI-compatible 等同于 OpenAI 官方接口的完整实现。

## 4. 目标架构

```text
Agent
  │
  ▼
ChatModel Protocol（稳定的核心端口）
  │
  ├── ProviderFactory / ProviderConfig
  │
  ├── Anthropic Messages 适配核心
  │     ├── DeepSeekProvider
  │     └── ClaudeProvider
  │
  ├── OpenAI Responses 适配器
  │     └── OpenAIProvider
  │
  └── OpenAI-compatible 适配器
        ├── Ollama profile
        └── vLLM profile
```

建议的代码结构：

```text
src/neil_agent/providers/
  __init__.py
  base.py                 # 能力、描述信息和公共类型
  errors.py               # 统一异常层次
  retry.py                # 公共重试策略与 Retry-After 解析
  anthropic_messages.py   # Anthropic Messages 编解码和流式累积
  deepseek.py             # DeepSeek 配置、能力覆盖和 SDK 装配
  claude.py               # Claude 配置、能力覆盖和 SDK 装配
  openai_responses.py     # OpenAI Responses 编解码和流式累积
  openai_compatible.py    # Ollama/vLLM 共享传输与 profile
  factory.py              # 配置校验与 Provider 构造
```

`ChatModel` 暂时保留为 Agent 的端口，不为了目录整齐进行无价值搬迁。旧 `LLMClient` 在迁移期可保留为 DeepSeek 的兼容 facade，并在文档中标记弃用路径。

## 5. 统一领域模型

协议适配器不得直接向 Agent 返回 SDK 对象。Provider 层需要统一以下概念：

### 5.1 Provider 描述与能力

建议增加：

```python
class ProviderId(StrEnum):
    DEEPSEEK = "deepseek"
    CLAUDE = "claude"
    OPENAI = "openai"
    OLLAMA = "ollama"
    VLLM = "vllm"


@dataclass(frozen=True)
class ProviderCapabilities:
    streaming: bool
    tool_calling: bool
    parallel_tool_calls: bool
    reasoning_state: bool
    structured_output: bool
    usage_reporting: bool
    prompt_caching: bool
```

能力值由“协议适配器默认值 + Provider profile + 必要的用户覆盖”共同确定。能力覆盖必须显式记录，不能通过捕获一次请求错误后静默关闭功能。

### 5.2 标准完成结果

标准完成结果至少包含：

- 文本内容；
- 零个或多个工具调用，保留稳定的 call ID；
- 统一停止原因；
- 输入、输出及可选缓存 token usage；
- 可选 Provider 私有 turn state；
- 请求标识和 Provider 元数据，供诊断使用。

Provider 私有状态必须使用不透明容器，例如：

```python
@dataclass(frozen=True)
class ProviderTurnState:
    provider: ProviderId
    model: str
    schema_version: int
    payload: Mapping[str, object]
```

该状态只能由生成它的 Provider 和兼容模型继续使用。Provider 或模型发生变化时必须丢弃或明确拒绝，禁止将 Claude thinking block、OpenAI reasoning item 等内容互相伪造转换。

### 5.3 停止原因

项目级停止原因建议固定为：

- `end_turn`
- `tool_call`
- `max_tokens`
- `content_filter`
- `cancelled`
- `error`
- `unknown`

适配器负责从 Provider 原始值映射，并保留原始值用于诊断。遇到未知值时可映射为 `unknown`，但不得将其当作成功的 `end_turn`。

## 6. 序列化边界

核心 schema 只描述 Agent 领域语义，具体 API payload 只能在 Provider 适配器内生成。

计划进行以下调整：

1. 停止在 Provider 层之外调用 `Message.to_api_dict()`、`ToolDefinition.to_api_dict()` 等 Anthropic 风格方法。
2. 在 Anthropic Messages 适配器中生成 `input_schema`、content block 和 tool result。
3. 在 OpenAI Responses 适配器中生成 function tool、`parameters`、function call 与 function call output。
4. 在 OpenAI-compatible 适配器中仅生成 profile 声明支持的字段。
5. 工具参数 JSON 必须在适配层做结构校验，解析失败应归类为协议错误，不能将损坏参数交给工具执行器。
6. 流式 delta 必须先由适配器累积成合法领域对象，再交付给 Agent；不能暴露半个 JSON 工具参数。

迁移完成后，可删除核心 schema 上的 Provider 专用序列化方法；如为兼容需要暂留，应添加弃用标记和禁止新增调用的测试。

## 7. Provider 实现规范

### 7.1 DeepSeekProvider

- 以当前 `LLMClient` 行为作为回归基线。
- 复用 Anthropic Messages 编解码核心，但在独立 profile 中维护模型、endpoint 和能力差异。
- 现有 thinking 内容及签名处理必须先通过 fixture 固化，再迁移实现。
- 未提供显式 Provider 时，可在兼容期根据已有 DeepSeek 配置选择 DeepSeek，并输出一次迁移提示。

### 7.2 ClaudeProvider

- 使用 Anthropic 原生 SDK 和 Messages 语义。
- 与 DeepSeek 共享基础消息、工具和流式转换代码，不共享未经验证的能力声明。
- thinking block、签名或缓存相关字段保留为 Provider 私有状态，不进入通用文本字段。
- SDK 新增事件类型时默认 fail closed，并在诊断信息中保留原始事件类型。

### 7.3 OpenAIProvider

- 使用 OpenAI 官方 SDK 的 Responses API。
- 显式处理 message、function call、function call output、usage 和流式事件。
- 不把 OpenAI Responses 强行模拟为 Anthropic content block。
- 多轮状态可以使用项目保存的标准消息重建；若使用 Provider 响应 ID 或推理状态，必须标记为 OpenAI 私有状态。
- 流式事件累积器必须验证终止事件、工具参数完整性和 usage 一致性。

### 7.4 Ollama / vLLM

- 共享 `OpenAICompatibleProvider` 的 HTTP/SDK 装配与基础编解码。
- 分别定义 Ollama 和 vLLM profile，不能只通过不同 `base_url` 假设行为完全一致。
- 工具调用、并行工具、structured output 和 usage 等能力必须通过配置或已验证的模型能力启用。
- 本地模型不支持工具调用时，在请求发出前返回 `UnsupportedCapabilityError`。
- 自定义 header、TLS、超时和 endpoint 只允许通过受控配置进入，不在业务代码中拼接。

## 8. 配置设计

建议新增统一选择项：

```text
LLM_PROVIDER=deepseek|claude|openai|ollama|vllm
LLM_MODEL=<provider model id>
LLM_BASE_URL=<optional override>
```

密钥仍采用 Provider 专用名称：

```text
DEEPSEEK_API_KEY=...
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
```

配置约束：

- 只校验当前选中 Provider 所需的密钥和 endpoint。
- 选择 Ollama/vLLM 时不得要求任何云 Provider 密钥。
- `base_url` 覆盖必须进行 scheme、host 和尾部路径校验。
- 密钥不能出现在 `repr`、日志、审计事件或异常文本中。
- 模型 ID 由配置显式提供，不在协议层硬编码“最新模型”。
- 兼容期内，未设置 `LLM_PROVIDER` 且存在现有 DeepSeek 配置时选择 DeepSeek；兼容期结束时间需在 README 和变更记录中明确。

配置对象可继续使用 Pydantic Settings，但 ProviderFactory 必须执行选择后的条件校验。不要让未选择 Provider 的缺失密钥阻止应用启动。

## 9. 错误与重试语义

建议建立稳定的异常层次：

```text
ProviderError
  ├── ProviderAuthenticationError
  ├── ProviderRateLimitError
  ├── ProviderTimeoutError
  ├── ProviderConnectionError
  ├── ProviderInvalidRequestError
  ├── ProviderContextOverflowError
  ├── UnsupportedCapabilityError
  ├── ProviderProtocolError
  └── ProviderInternalError
```

重试规则：

- SDK 异常先映射为项目异常，再由公共 retry policy 决策。
- 认证失败、参数错误、上下文溢出和能力不支持不得重试。
- 限流、连接中断和明确的服务端临时错误可按有界指数退避重试。
- `Retry-After` 必须设置最大上限，并允许取消。
- 在尚未向 Agent 交付任何流式内容时才允许自动重试。
- 一旦交付文本 delta 或工具调用 delta，连接失败必须作为部分流失败返回，禁止透明重试造成重复输出或重复工具调用。
- 每次重试记录 Provider、模型、错误类别、attempt 和延迟，但不记录密钥或完整敏感 prompt。

## 10. 能力矩阵与校验方式

下表描述适配器目标，不代表所有模型当前都支持对应能力：

| 能力 | Anthropic Messages | OpenAI Responses | OpenAI-compatible | 验证方式 |
| --- | --- | --- | --- | --- |
| 非流式文本 | 必须 | 必须 | 必须 | Provider 契约测试 |
| 流式文本 | 必须 | 必须 | 必须 | delta 重放与终止事件测试 |
| 工具调用 | 按 Provider profile | 按模型能力 | 默认关闭，显式开启 | request/response golden fixture |
| 并行工具调用 | 按 Provider profile | 按模型能力 | 默认关闭，显式开启 | 多 call ID 契约测试 |
| 推理私有状态 | 不透明保存 | 不透明保存 | 默认不支持 | 同 Provider 回放测试 |
| structured output | 显式声明 | 显式声明 | 默认关闭 | schema 合规测试 |
| usage | 尽可能标准化 | 尽可能标准化 | 缺失时标记 unknown | usage fixture |
| prompt caching | Provider 私有 | Provider 私有 | 默认不支持 | 专用集成测试 |

Provider 初始化时应生成最终能力快照。Agent 请求某项能力前先检查快照，并将拒绝原因展示给用户或调用方。

## 11. 实施阶段

### Phase 0：冻结现有行为与协议契约（1～2 天）

- 为当前 DeepSeek 请求、普通响应、工具调用和流式事件建立脱敏 fixture。
- 固化 `ChatModel` 当前对 Agent 的输入输出契约。
- 定义统一停止原因、usage、错误分类和 Provider 私有状态。
- 建立 Provider contract test 骨架。

交付物：协议决策记录、DeepSeek golden fixture、首版契约测试。

完成记录（2026-08-10）：

- 新增 `docs/provider-protocol-contract.md`，冻结 ChatModel v1 输入、完成、流式、usage、停止原因、错误分类和私有状态语义。
- 新增版本化、脱敏且明确标记为合成数据的 DeepSeek Anthropic Messages golden fixture。
- 新增可复用 Provider 契约断言，并用普通完成、文本流和工具调用流三个场景固定当前 DeepSeek 行为。
- Phase 0 未修改生产运行路径，ProviderFactory、配置解耦与公共异常实现仍属于 Phase 1。

### Phase 1：建立核心边界与配置（2～3 天）

- 新增 `providers/base.py`、`errors.py`、`retry.py` 和 `factory.py`。
- 将 Provider 序列化从核心 schema 的调用路径移入适配器。
- 改造 Settings，使密钥按选中 Provider 条件校验。
- 保留旧 DeepSeek 启动方式的兼容路径。

交付物：ProviderFactory、能力快照、统一异常、配置回归测试。

完成记录（2026-08-10）：

- 新增 `providers/base.py`、`errors.py`、`retry.py`、`factory.py` 和 `anthropic_messages.py`，落地 Provider 身份、能力快照、停止原因、私有状态、统一错误与重试策略。
- `Message`、`ToolCall`、`ToolResult` 和 `ToolDefinition` 不再携带 Anthropic `to_api_dict()`；协议编码集中到 Anthropic Messages 适配边界，上下文预算与压缩改用 provider-neutral 领域 JSON。
- Settings 默认兼容 DeepSeek，同时支持 `LLM_PROVIDER`、`LLM_MODEL`、`LLM_BASE_URL` 及按选中 Provider 条件校验的密钥；Ollama/vLLM 不再要求云 API Key。
- ProviderFactory 当前仅注册 DeepSeek；选择尚未实现的 Claude/OpenAI/Ollama/vLLM 会在网络请求前 fail closed，不会静默回落。
- `ModelResponse` 已加入统一 `stop_reason` 和可选 `provider_state`，旧 `LLMClient` 继续作为 DeepSeek 兼容入口。

### Phase 2：完成 Anthropic Messages 家族（2～3 天）

- 抽取当前 DeepSeek 编解码和流式累积逻辑。
- 实现 `DeepSeekProvider` 并通过全部旧测试。
- 实现 `ClaudeProvider`，补齐 Provider 差异 fixture。
- 验证工具调用 ID、thinking 私有状态和取消语义。

交付物：DeepSeek/Claude 两套可选 Provider，共享一套可审计的协议核心。

完成记录（2026-08-11）：

- 新增共享 `AnthropicMessagesProvider` runtime，将请求构造、消息与工具编码、完成/流式累积、usage、停止原因、错误归一化和有界重试从旧 `LLMClient` 中抽离；`LLMClient` 仅保留为 DeepSeek 兼容 facade。
- 新增独立 `DeepSeekProvider` 与 `ClaudeProvider` profile，并在 `ProviderFactory` 中同时注册。DeepSeek 保留兼容 endpoint 与 thinking 开关；Claude 使用原生 endpoint，关闭 thinking 时省略字段，开启时支持 adaptive 与显式手动预算。
- `Message.provider_state` 与 `ModelResponse.provider_state` 现在保存递归冻结、绑定 Provider/模型/schema version 的完整 Anthropic assistant content-block 顺序。普通 thinking、redacted-thinking 与 interleaved tool block 可跨工具轮次和会话 JSON 原样往返；公开内容不一致、跨 Provider 或模型回放会在网络前 fail closed。
- 新增 Claude v1 脱敏合成 golden fixture，覆盖非流式文本、文本流、并行工具 ID、thinking/redacted-thinking 与 usage；DeepSeek fixture 同步纳入权威私有状态。
- 新增主动关闭流的取消回归：SDK stream context 必须释放，不重试、不更新 usage、不产生伪造终态；同时覆盖重复工具 ID、未知 content block 与 Claude endpoint override。
- 全量 pytest 为 624 项通过、19 项目标 Windows 平台条件跳过；Ruff lint/format、mypy 48 个源文件和 5 个内置离线评测全部通过。
- 默认离线测试与评测未调用真实 DeepSeek/Claude API；在线 smoke test 仍需显式凭据、费用确认与单独记录。

### Phase 3：完成 OpenAI Responses（3～4 天）

- 实现 Responses 请求、响应和流式事件适配。
- 实现 function call 与 function call output 往返转换。
- 处理 usage、停止原因、部分流失败和私有 turn state。
- 增加 OpenAI fake transport 与可选在线 smoke test。

交付物：OpenAIProvider 及完整契约测试。

### Phase 4：完成本地 OpenAI-compatible 接入（2～3 天）

- 实现共享 OpenAI-compatible 传输层。
- 增加 Ollama/vLLM 独立 profile 和能力覆盖。
- 验证无密钥启动、自定义 base URL、超时和工具能力拒绝。
- 在可用环境中分别执行一次显式开启的本地 smoke test。

交付物：Ollama/vLLM 可配置接入与兼容性说明。

### Phase 5：集成收口（2 天）

- 运行全量测试、静态检查和非交互 CLI 回归。
- 更新 README 的 Provider 配置示例和能力矩阵。
- 为 `/doctor` 增加脱敏的当前 Provider、模型和 endpoint 检查。
- 记录已验证的 SDK 版本与在线 smoke test 日期。

交付物：可发布的多 Provider 协议层与用户文档。

总工程量预计 10～15 个有效开发日。若先交付可面试演示的最小版本，建议完成 Phase 0～3：这能展示协议抽象、两类真实协议、工具调用、流式状态机和可测试性；Ollama/vLLM 随后补齐。

## 12. 测试策略

### 12.1 Provider 契约测试

所有实现必须通过同一组参数化测试：

- 普通文本完成；
- Unicode 和空内容边界；
- 单个及多个工具调用；
- 工具参数分片与非法 JSON；
- 停止原因映射；
- usage 完整、部分缺失和未知；
- 流式正常终止、取消、超时和中途断连；
- 认证、限流、上下文溢出和不可重试错误；
- 不支持能力的请求前拒绝；
- Provider 私有状态不得跨 Provider 回放。

### 12.2 Golden fixture

- 每类线协议保存脱敏的请求与响应 fixture。
- fixture 标记来源 SDK 版本、采集日期和对应能力。
- 测试同时校验出站 payload 和归一化领域结果。
- SDK 升级导致 fixture 变化时必须人工审查，禁止无条件覆盖快照。

### 12.3 在线测试

- 默认测试套件不访问公网。
- 在线测试使用独立 marker 和显式环境开关。
- 设置 token、时间和费用上限。
- CI 仅在受保护的定时任务或人工触发任务中使用密钥。
- 在线失败必须区分产品回归、Provider 暂时不可用和模型能力变化。

## 13. 完成标准

满足以下条件后，本阶段才算完成：

- 同一个 Agent 核心循环可使用 DeepSeek、Claude、OpenAI、Ollama 和 vLLM 配置启动。
- 核心领域 schema 不再承担 Anthropic/OpenAI payload 序列化职责。
- 选择非 DeepSeek Provider 时不要求 `DEEPSEEK_API_KEY`。
- Provider SDK 异常不会穿透 Agent 边界。
- 不支持的能力在请求发送前明确失败，没有静默降级。
- 流式重试不会产生重复文本或重复工具调用。
- 现有测试全部通过，新增契约测试覆盖三类线协议。
- README 包含配置示例、能力矩阵和已验证版本说明。
- 日志、诊断命令和测试产物不泄露 API key。

## 14. 主要风险与决策

| 风险 | 决策 |
| --- | --- |
| 为统一接口丢失 Provider 高级能力 | 使用公共核心语义加显式 capabilities 和不透明私有状态 |
| OpenAI-compatible 实现差异大 | 使用 Ollama/vLLM 独立 profile，能力默认保守 |
| 流式重试产生重复副作用 | 首个 delta 交付后禁止透明重试 |
| Provider 状态跨模型不可移植 | 状态绑定 Provider、模型和 schema version |
| SDK 升级改变事件结构 | golden fixture 加版本元数据并人工审查 |
| 在线测试不稳定或产生费用 | 默认离线，在线 smoke test 显式开启并设置预算 |
| 旧 DeepSeek 用户配置被破坏 | 保留一个明确期限的兼容入口并提供迁移提示 |

## 15. 后续方向（不在本文实施范围）

协议层稳定后，再分别建立以下独立计划：

- MCP Client 与 ToolRegistry 动态工具发现；
- HumanEval、自建 coding-agent benchmark 等标准评测；
- Provider fallback、成本/延迟路由与模型选择策略；
- Provider 运行指标、调用链和可视化分析。

这些能力应依赖本协议层提供的稳定接口，但不应反向污染 Provider 适配器。

## 16. 参考资料

- [OpenAI：Model guidance（Responses API、tool calling 与多轮状态）](https://developers.openai.com/api/docs/guides/latest-model)
- [Anthropic：Streaming Messages](https://platform.claude.com/docs/en/build-with-claude/streaming)
- [Ollama：OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility)
- [vLLM：OpenAI-compatible server](https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/)

实现期间应以官方文档和锁定的 SDK 版本为准；本文不固定具体模型 ID，避免将会变化的模型命名写入协议设计。
