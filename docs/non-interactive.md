# 一次性非交互协议

## 目标和边界

一次性入口用于脚本、CI 和编辑器集成：接收一个 prompt、运行一个 Agent 闭环并以明确退出码结束。它不是无确认的“全自动模式”。默认协议 v1 始终只提供工作区文件读取、搜索和只读 Git 工具；协议 v2 才提供显式、两阶段、单项消费的写入审批。

```text
neil-agent -p "概括 src 目录"
neil-agent -p "检查 Git 状态" --output-format json
neil-agent -p "检查 Git 状态" --output-format stream-json
```

默认不保存会话；只有 `--save-session` 会保存成功结果。`--output-format` 和 `--save-session` 必须与 `-p/--print` 一起使用。

## 协议版本

### v1：默认只读

不指定 `--protocol-version` 时使用 v1。它保持原有五个只读工具和字段契约，不接受 approval ID，也不会因为新增 v2 而扩大权限。

### v2：显式审批

非交互写入不能使用全局 `--yes` 或预先授权任意未来调用。v2 使用两个独立进程：

```text
neil-agent -p "更新版本号" \
  --protocol-version 2 \
  --permission-mode request \
  --output-format json

neil-agent -p "更新版本号" \
  --protocol-version 2 \
  --permission-mode approve \
  --approval-id <request-id> \
  --output-format json
```

第一次运行允许模型看到受审批工具，但所有此类调用都会被拒绝执行，并产生 `approval_request`。第二次必须使用完全相同的 prompt 和一个请求 ID；最多只有与该请求完全匹配的一项操作能够执行。

审批模式只支持 `json` 或 `stream-json`。暴露的能力仍局限于工作区文件工具、固定质量检查和本地 Git 暂存/提交；不提供任意 shell、远程 Git、推送或隐式自动批准。

approve 是一次新的模型运行，可能产生额外 API 用量；模型若没有再次提出完全相同的操作，旧授权不会被套用到近似调用，而是失败或返回新的审批请求。

### 审批绑定与重放保护

待审批记录写入 `.neil-agent/approvals/pending/`，有效期为 15 分钟，创建新请求时会清理正常过期记录，最多保留 100 项。记录包含：

- 随机内部 request ID、创建/过期时间和规范工作区。
- prompt 与当前项目指令的 SHA-256，不保存它们的正文。
- 工具名、规范化参数的 SHA-256，不保存独立参数正文。
- 精确预览及其 SHA-256。预览可能包含目标文件 diff，这是用户判断是否批准所必需的内容。

返回给调用方的 approval ID 由内部 request ID 和完整审批记录摘要组成。第二次运行同时校验该摘要，因此即使工作区中的 pending 记录被外部进程修改，也不能把用户看到的授权替换成另一项操作。

批准时重新校验工作区、prompt、项目指令、工具名、参数和当前预览。文件或暂存区在两次运行之间变化时，旧请求不会执行，而是返回新预览。

匹配请求在调用 handler **之前**写入 `.neil-agent/approvals/consumed/` 并从 pending 移除。因此：

- 同一 ID 只能使用一次。
- 消费后进程崩溃或工具失败也不会恢复授权；必须重新生成预览。
- `ToolRegistry` 在消费后、执行前再次生成预览，拒绝更窄时间窗口内的外部变化。
- 一次 approve 运行最多消费一个 ID；后续高风险调用会产生新的审批请求和退出码 `3`。

## 输出格式

### `text`

模型正文增量原样写入标准输出，成功时补一个结尾换行。活动不会混入正文；错误写入标准错误。

### `json`

标准输出只有一个紧凑 JSON 对象和结尾换行。成功对象的稳定字段为：

```json
{"type":"result","protocol_version":1,"success":true,"session_id":"...","result":"...","saved":false,"usage":{"input_tokens":120,"output_tokens":20,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"total_tokens":140},"activities":[]}
```

`usage` 来自本次成功 Agent 回合的服务端响应；多轮工具调用会逐次相加。服务端未提供用量时它为 `null`，不能用历史实测值推算下一次请求费用。

协议 v2 的成功对象还包含 `permission_mode` 和 `approved_request_id`。等待审批时终止对象类型为 `approval_required`，包含有界 `approval_requests` 数组、`success: false` 和退出码 `3`。

### `stream-json`

标准输出是 JSONL，每行都能独立解析。事件顺序为：

1. 一个 `session_start`，包含协议版本、session ID、模型、工作区、工具列表和只读/权限模式。
2. 零个或多个 `activity`、`text_delta`；v2 还可以发送 `approval_request`。
3. 一个 `result`、`approval_required` 或 `error` 终止事件。成功 `result` 同样包含 `usage`，但不重复 `activities` 数组。

活动只描述可观察步骤，不包含隐藏思考。工具正文留在 Agent 的模型上下文中，不额外复制到事件流，避免意外扩大结构化日志的敏感范围。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 请求成功完成 |
| `1` | 模型、Agent、工具或内部运行错误 |
| `2` | 命令参数、空 prompt 或启动配置错误 |
| `3` | 操作尚未执行或只执行了已批准部分，仍有精确预览等待批准 |
| `130` | 用户通过 `Ctrl+C` 中断 |

结构化错误含 `type`、`protocol_version`、`success: false`、安全错误文本、稳定的 `error_code` 和 `exit_code`。当前错误代码为：

- `model_error`、`agent_error`、`tool_error`：模型、编排或工具错误。
- `approval_error`：仅用于 v2，表示审批不存在、过期、已消费、格式无效或无法安全持久化。
- `session_error`、`instruction_error`、`hook_error`、`audit_error`：对应的本地子系统错误。
- `invalid_input`、`configuration_error`：输入或启动配置错误。
- `interrupted`、`internal_error`：用户中断或未分类内部错误。

未知内部异常不会把异常详情写入协议。

## 兼容性规则

- 消费端应先检查 `protocol_version`，并忽略认识的事件中新增的可选字段。
- 依赖事件顺序和 `type`，不要依赖 JSON 键顺序。
- 仓库中的 `tests/fixtures/noninteractive_protocol_v1.json` 和 `noninteractive_protocol_v2.json` 分别固定两个版本的事件、终止对象、usage 和错误代码字段；兼容性测试要求字段变化必须显式更新对应契约。
- v2 是显式选择，不会改变 v1 的工具集合、字段或退出行为。未来不兼容变更必须新增协议版本，不能静默改变现有版本。
- 两个版本都不输出 thinking block。消费端必须把审批预览当作可能含项目代码的敏感输出，避免写入不受保护的公共日志。
