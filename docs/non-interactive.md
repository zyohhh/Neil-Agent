# 一次性非交互协议

## 目标和边界

一次性入口用于脚本、CI 和编辑器集成：接收一个 prompt、运行一个 Agent 闭环并以明确退出码结束。它不是无确认的“全自动模式”，当前只提供工作区文件读取、搜索和只读 Git 工具。

```text
neil-agent -p "概括 src 目录"
neil-agent -p "检查 Git 状态" --output-format json
neil-agent -p "检查 Git 状态" --output-format stream-json
```

默认不保存会话；只有 `--save-session` 会保存成功结果。`--output-format` 和 `--save-session` 必须与 `-p/--print` 一起使用。

## 输出格式

### `text`

模型正文增量原样写入标准输出，成功时补一个结尾换行。活动不会混入正文；错误写入标准错误。

### `json`

标准输出只有一个紧凑 JSON 对象和结尾换行。成功对象的稳定字段为：

```json
{"type":"result","protocol_version":1,"success":true,"session_id":"...","result":"...","saved":false,"activities":[]}
```

### `stream-json`

标准输出是 JSONL，每行都能独立解析。事件顺序为：

1. 一个 `session_start`，包含协议版本、session ID、模型、工作区、只读工具列表和 `read_only: true`。
2. 零个或多个 `activity` 与 `text_delta`。
3. 一个 `result` 或 `error` 终止事件。

活动只描述可观察步骤，不包含隐藏思考。工具正文留在 Agent 的模型上下文中，不额外复制到事件流，避免意外扩大结构化日志的敏感范围。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 请求成功完成 |
| `1` | 模型、Agent、工具或内部运行错误 |
| `2` | 命令参数、空 prompt 或启动配置错误 |
| `130` | 用户通过 `Ctrl+C` 中断 |

结构化错误含 `type`、`protocol_version`、`success: false`、安全错误文本和 `exit_code`。未知内部异常不会把异常详情写入协议。

## 兼容性规则

- 消费端应先检查 `protocol_version`，并忽略认识的事件中新增的可选字段。
- 依赖事件顺序和 `type`，不要依赖 JSON 键顺序。
- 版本 1 不输出 thinking block，也不提供交互审批；未来若增加写工具，应定义独立的审批输入协议，而不是默认自动批准。
