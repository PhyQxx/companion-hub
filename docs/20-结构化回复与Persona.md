# P1 · 结构化回复与 Persona

## 结论

P1 将模型的自然语言回复与机器控制信息拆开，同时保持现有文字客户端兼容。Persona 不再是
代码里的固定提示词，而是数据库中的不可变版本；草稿只有发布后才影响新回合，历史消息会记录
当时使用的 Persona 版本。

## AgentReply v1

模型先输出正常回复，末尾追加一个 `<aria_control>...</aria_control>` 控制块。服务端的流式过滤器
会跨 WebSocket chunk 识别标记，只广播可见文本。最终控制内容经过严格校验后写入
`message.decision_meta.agent_reply`，并通过 `reply.control` 单独广播。

契约标识为 `aria.agent-reply/1`，主要字段：

- `text`：字幕和持久化正文。
- `tts_text`：TTS 朗读文本，默认等于 `text`。
- `emotion`：受控情绪枚举。
- `expressions`：输出端可识别的表情名称。
- `actions`：`expression / animation / sound / picture` 扩展动作。
- `parse_status`：`structured` 或 `fallback`，便于观测模型协议遵循率。

若模型没有控制块、JSON 无效或字段越界，服务端保留标记前的正常文本并生成 neutral/default
降级结果。控制块不会进入字幕、TTS 或消息正文。

## Persona 版本

`persona_version` 保存不可变内容，`persona_pointer` 原子指向当前发布版本。首次连接数据库会建立
默认 Aria Persona。管理 API：

- `GET /api/v1/admin/personas/current`
- `GET /api/v1/admin/personas/versions`
- `POST /api/v1/admin/personas/validate`
- `POST /api/v1/admin/personas/versions`
- `POST /api/v1/admin/personas/versions/{version}/publish`
- `POST /api/v1/admin/personas/versions/{version}/rollback`

这些 API 与模型配置后台共用 `ARIA_ADMIN_TOKEN`。可视化入口是 `/admin/personas`。

## Open-LLM-VTuber 映射

桥接默认启用 `structured_reply: true`，收齐 `reply.delta` 和 `reply.control` 后创建原生
`SentenceOutput`：`DisplayText` 使用字幕，`tts_text` 独立传给 TTS，表情、图片和音效写入
`Actions`。这保证输出语义正确，但首段语音需等待整轮完成。要求最低首字延迟时可关闭结构化模式，
退回 P0 的 token 流式管线。

## 扩展边界

新增输入方式仍进入 `InputEnvelope`；新增输出端消费 `AgentReply → OutputIntent`，不应直接解析
模型原文。动作类型新增时应升级 schema version，旧消费者继续忽略未知 WebSocket 事件。
