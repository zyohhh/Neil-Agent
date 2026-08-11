# Provider 协议契约 v1

## 状态

- 状态：已接受
- 契约版本：1
- 冻结日期：2026-08-10
- 适用范围：`ChatModel` 核心端口与 Provider 适配器
- 基线实现：共享 Anthropic Messages runtime、`DeepSeekProvider` 与 `ClaudeProvider`

本文档记录多 Provider 改造前必须保持稳定的 Agent 语义。它描述的是 Neil Agent 内部契约，不是任何一家 Provider API 的复制品。

## 1. 核心依赖方向

`Agent` 只依赖 `ChatModel`：

```python
complete(messages, *, system_prompt) -> str
stream(messages, *, system_prompt, tools=()) -> Iterator[str | ModelResponse]
```

Provider 适配器负责把 SDK 请求、响应和异常转换到该端口。以下对象不得穿过边界：

- Provider SDK message、event 或 error；
- HTTP response；
- 未完成的工具参数 JSON；
- Provider API key 或认证 header。

## 2. 输入契约

- `messages` 至少包含一条消息；空序列必须在发出网络请求前拒绝。
- 消息、thinking、工具调用和工具结果使用 `schemas.py` 中的冻结领域对象。
- `system_prompt` 作为独立参数传递，不伪装成普通历史消息。
- 工具定义使用 JSON Schema；Provider 专用的字段名和包装结构由适配器生成。
- 工具调用 ID 在一个响应内必须非空且唯一，并在工具结果往返中保持不变。
- Provider 适配器不得修改调用方传入的消息或工具序列。

Phase 1 已移除核心 schema 的 `to_api_dict()`。Anthropic Messages 编码集中在 `providers/anthropic_messages.py`；上下文预算和压缩转录使用 provider-neutral 领域 JSON，不能将任何一家 Provider 的字段提升为跨 Provider 契约。

## 3. 非流式完成契约

- `complete()` 成功时返回非空白字符串。
- 只有整个请求成功后才能更新 `last_usage`。
- 新请求开始时先清空旧的 `last_usage`，防止失败请求暴露过期统计。
- 没有服务端 usage 时使用 `None`，不能伪造估算值。
- 纯空白或不存在文本的成功响应被视为协议错误。

非流式接口当前用于会话压缩等纯文本场景，不承载工具调用。后续 Provider 若返回工具调用，适配器必须明确拒绝，不能静默丢弃后返回空文本。

## 4. 流式完成契约

一次成功的 `stream()` 必须产生：

```text
零个或多个非空 str delta
恰好一个终态 ModelResponse
迭代结束
```

约束如下：

- `ModelResponse` 只能出现在最后一个位置。
- 终态响应必须包含非空白文本或至少一个工具调用。
- 文本 delta 已交付后发生的错误必须向上报告，禁止透明重试。
- 未交付任何 delta 时，适配器可以按公共重试策略重新建立请求。
- thinking 只在需要回放工具调用时保留；不能作为用户可见文本输出。
- 成功结束后 `last_usage` 与终态 `ModelResponse.usage` 相同。
- 中止或失败的半个响应不能进入 Agent 成功历史。
- 调用方关闭流迭代器时，适配器必须退出 SDK stream context；取消不得重试，也不得伪造终态 `ModelResponse`。

## 5. Usage 语义

v1 使用现有 `TokenUsage` 的四个稳定计数：

- `input_tokens`
- `output_tokens`
- `cache_creation_input_tokens`
- `cache_read_input_tokens`

所有计数必须是非负整数。Provider 未报告某个已知子字段时，该子字段可取 `0`；Provider 完全没有返回 usage 时，整个 usage 必须为 `None`。`total_tokens` 是上述字段的本地求和，不代表 Provider 的额外计费类别。

未知的 Provider usage 字段暂不进入 v1 领域对象，但适配器测试应通过 fixture 记录其出现，以便后续有意识地升级契约。

## 6. 停止原因决策

Phase 1 引入统一停止原因时，允许值冻结为：

| 值 | 语义 |
| --- | --- |
| `end_turn` | Provider 正常结束当前回答 |
| `tool_call` | 响应要求执行一个或多个工具 |
| `max_tokens` | 达到生成或上下文限制 |
| `content_filter` | Provider 安全策略阻止继续生成 |
| `cancelled` | 调用方取消请求 |
| `error` | 请求以已分类错误结束 |
| `unknown` | 收到尚未映射的 Provider 终止值 |

未知原始值映射为 `unknown`，不能当作 `end_turn`。Phase 1 已将该字段接入 `ModelResponse`；工具调用即使缺少 Provider 原始停止值，也会明确归类为 `tool_call`。

## 7. Provider 私有状态

某些 Provider 要求在后续 turn 回放签名、reasoning item 或响应标识。该状态不转换为文本，也不在 Provider 间迁移。计划中的容器必须至少绑定：

- Provider ID；
- 模型 ID；
- 私有状态 schema version；
- 不透明 payload。

只有生成状态的 Provider 与兼容模型可以读取它。Provider 或模型变化时必须明确丢弃或拒绝；禁止伪造 Claude thinking signature 或把 OpenAI reasoning item 转换成 Anthropic block。

Phase 2 已让 `ModelResponse.provider_state` 和 `Message.provider_state` 承担权威回放状态。Anthropic Messages runtime 会把含普通 `thinking`、`redacted_thinking`、文本和工具调用的完整 assistant content-block 顺序保存在私有状态中，绑定生成它们的 Provider、模型和 schema version；Agent 在工具轮次中原样携带该状态。编码器会先确认私有块与公开文本/工具调用完全一致，并在网络请求前拒绝内容分歧、跨 Provider 或跨模型回放，因此 interleaved thinking 不会被错误重排。

`ThinkingContent` 暂时保留为普通 thinking 块的只读兼容镜像，供现有活动计数和历史数据使用；它不包含 redacted payload，也不是跨 Provider 推理格式。实际回放优先且只信任已绑定的 `provider_state`。私有 payload 在构造时递归冻结，序列化时转换为 JSON-safe 副本，避免调用方在请求之间篡改签名块。

## 8. 错误分类决策

Phase 1 的公共异常层必须覆盖：

| 类别 | 默认是否重试 | 说明 |
| --- | --- | --- |
| authentication | 否 | 密钥无效或认证失败 |
| rate_limit | 是 | 受本地次数、退避和 `Retry-After` 上限约束 |
| timeout | 是 | 请求超时；输出后不得透明重试 |
| connection | 是 | 连接失败；输出后不得透明重试 |
| invalid_request | 否 | 请求结构或参数错误 |
| context_overflow | 否 | 上下文或生成限制不满足 |
| unsupported_capability | 否 | 当前 Provider/模型不支持请求能力 |
| protocol | 否 | 响应结构、事件顺序或工具参数损坏 |
| provider_internal | 是 | 仅明确的暂时服务端错误可重试 |

Phase 1 已落地上述公共异常层；所有 `ProviderError` 仍继承 `LLMError`，保持现有用户边界和非交互 `model_error` 错误码兼容，SDK 异常不得直接穿透。

## 9. Golden fixture 规则

基线 fixture 位于：

```text
tests/fixtures/providers/deepseek_anthropic_messages_v1.json
tests/fixtures/providers/claude_anthropic_messages_v1.json
```

该 fixture 是从当前适配器行为整理出的脱敏合成 characterization 数据，不声称来自真实在线响应。它固定以下行为：

- 普通文本请求与 usage；
- 流式文本 delta 和唯一终态响应；
- thinking 与工具调用的回放；
- 工具定义的 Anthropic `input_schema` 映射；
- 工具调用 ID 和缓存 token 统计。
- Claude 原生请求在关闭 thinking 时省略该字段，在开启时区分 adaptive 与手动预算模式；
- Claude 多工具调用 ID、普通 thinking、redacted thinking 及绑定状态的往返。

fixture 必须带版本、Provider、线协议、来源类型和脱敏标志，且不得包含 API key。SDK 升级或适配器重构导致 fixture 变化时，需要逐字段人工审查；禁止为了让测试通过而无条件重录。

## 10. 契约测试接入规则

`tests/test_provider_contract.py` 提供所有 Provider 共用的完成与流式断言。新 Provider 至少需要提供三个用例：

1. 普通非流式文本完成；
2. 带文本 delta 的流式完成；
3. 无文本、以工具调用结束的流式完成。

Phase 2 已补入主动取消、部分流失败、重复工具 ID、未知 content block、thinking 私有状态和跨 Provider/模型拒绝。后续 Phase 仍需为新线协议逐步补入超时、非法工具 JSON、完整停止原因和能力拒绝。Provider 专用测试可以增加能力，但不能放宽公共契约。

## 11. v1 变更规则

- 收紧输入验证或新增可选元数据通常可以保持 v1。
- 改变事件顺序、删除字段、改变工具调用 ID 或 usage 语义必须升级契约版本。
- Provider 私有字段变化只升级对应私有状态版本，不应无故升级核心契约。
- 契约版本升级必须同时更新决策记录、fixture、公共断言和迁移说明。
