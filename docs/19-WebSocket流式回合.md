# WebSocket 流式回合（第一阶段）

> 文档类型：Phase Record / Historical
> 当前设计真源：`09-运行状态机与多端同步.md`、`11-统一数据模型与API契约.md`

## 1. 已实现能力

`/ws/chat` 建立了文字聊天的实时编排边界：

- WebSocket 接受连接后要求 5 秒内发送 `authenticate`，服务端通过数据库短期聊天会话认证；后续每个客户端帧都会重新检查撤销和到期状态；
- `client_hello` 携带每个会话的消息游标，服务端只补发该用户拥有且 `seq > cursor` 的已提交消息；
- `message.send` 先原子持久化用户消息和 `interaction_turn`，再启动模型生成；
- LiteLLM/OpenAI-compatible 适配器使用供应商真实 streaming 响应，不对一次性结果做假分块；
- `reply.delta` 是带 `delta_index` 的瞬时事件，不落为事实；`reply.committed` 写入完整助手消息后才进入补拉历史；
- 同一进程中订阅相同会话的多个客户端会收到同一 committed/delta 事件；
- `turn.cancel` 持久化 cancelled 状态并取消运行任务，提交事务再次检查状态，因此迟到结果不能写助手消息；
- 进程启动时把遗留的 accepted/thinking/streaming 回合标记为 `process_restarted` cancelled，不续跑不安全副作用；
- 浏览器调试页优先使用 WebSocket，连接失败时保留 REST 降级。

## 2. 帧协议

客户端帧：

| type | 关键字段 | 说明 |
|---|---|---|
| `authenticate` | `access_token` | 必须是首帧 |
| `client_hello` | `cursors: {conversation_id: seq}` | 订阅并补拉多个会话 |
| `message.send` | `conversation_id`, `text`, `privacy_level` | 创建持久回合并生成 |
| `turn.cancel` | `generation_id` | 取消当前 generation |
| `sync.request` | `conversation_id`, `after_seq` | 单会话主动补拉 |
| `ping` | — | 返回 `pong` |

服务器事件统一携带 `proto_version`、`stream`、`seq`、`event_id`、`type`、`generation_id`、`sent_at` 和 `payload`。当前 `seq` 是持久消息序号：committed 事件有值，瞬时状态/delta 为 `null` 并使用 `delta_index` 保序。

## 3. 流式失败边界

模型路由允许端点在首个可见 delta 之前失败并切换 fallback。端点已经发送可见内容后若失败，generation 返回 `stream_interrupted`，不会把备用模型的内容拼接到已有半句后面。L2 继续强制本地 private 路由，L3 在创建回合前阻断。

## 4. 验证覆盖

- 无效/过期会话关闭码 4401；
- 首帧认证、游标补拉、delta 顺序、最终提交；
- 断线重连只补 committed，不重放 delta；
- 取消后用户消息保留、助手消息为零、turn 为 cancelled；
- 模型首块前失败可 fallback，首块后失败禁止拼接；
- LiteLLM streaming 参数、delta 聚合、usage 和 finish reason；
- PostgreSQL `0005_interaction_turn` 迁移和真实 SenseNova WebSocket 流。

## 5. 尚未完成

- 当前广播器是单进程内存实现；多 worker/多节点需要 PostgreSQL NOTIFY、Redis Streams 或专用消息层；
- 游标只覆盖持久消息，尚未建立包含状态、配置和设备事件的统一持久 stream seq；
- 未实现每会话并发策略（排队、自动打断旧 generation 或明确 409）；
- 未实现客户端 ACK、发送队列上限、慢消费者断开和端到端背压指标；
- 未实现结构化 `AgentReply`、工具调用、TTS 句段和音频租约；
- 尚未完成 30 轮稳定性、首 delta P90 和网络抖动/代理超时测试。
