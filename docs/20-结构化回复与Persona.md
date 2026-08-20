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

### Persona 与长期记忆的边界

P5 真实对话发现，同一 Persona 在不同会话可能对“自己”的稳定信息给出不同答案。这里不把身高、生日、过去明确说过的自我信息等运行时事实写死进 Persona：Persona 只负责身份、风格、关系和边界；会随对话形成、需要跨会话保持一致的信息应进入长期持久化记忆。

现有记忆模型主要面向“关于用户”的长期记忆，后续 P5 收口需要扩展记忆主体/归属，使记忆可以明确区分“用户”“助手自身”“共同关系/事件”等主体，再让聊天检索按主体注入。这样角色自身曾明确给出的稳定信息也能像用户偏好一样被持久化、纠错、冲突裁决和删除，而不是塞进 Persona 配置。

### 现实能力边界

Persona 的角色设定也不能被当成现实可执行能力。ChatService 会额外注入运行时现实能力约束：

- 默认没有接入设备/工具动作时，只允许纯对话层面的转移话题；
- 不得建议“我们去训练场 / 后院 / 厨房”等系统无法实际参与的活动，也不得声称能触碰、移动现实物体；
- 只有运行时能力提供器明确返回“当前在线且已授权”的设备动作后，模型才可以据此提出现实行动建议；
- 例如客厅电视控制能力真实存在时，可以说“我们看会儿电视吧，我可以帮你打开电视”；设备未接入时不能这样承诺；
- 每轮实际暴露给模型的能力 ID 会进入 `decision_meta.runtime_capabilities`，便于 P5 排查越界建议。

### 运行时一致性

Persona 发布后不仅要更新处理该管理请求的进程内快照，还必须让其他聊天 worker 在下一回合看到新版本。当前实现中：

- `PersonaStore.refresh()` 从数据库 `persona_pointer` 重新读取当前发布 Persona；
- ChatService 在每个新回合组装 system prompt 前刷新 Persona，因此长连接 WebSocket 不需要重连即可在下一轮使用新版本；
- `/api/v1/admin/personas/current` 与运行时元信息读取也刷新数据库指针，避免管理端与聊天端观察到不同版本；
- 每条助手消息继续在 `decision_meta.persona_version` 中记录生成时实际使用的版本，历史消息不会因新 Persona 发布而被改写。

公开运行时元信息接口 `GET /api/v1/meta/runtime` 返回当前 Persona 的版本、内容哈希和名称，聊天前端用它展示当前角色，而不是硬编码 `Aria`。

## Open-LLM-VTuber 映射

桥接默认启用 `structured_reply: true`，收齐 `reply.delta` 和 `reply.control` 后创建原生
`SentenceOutput`：`DisplayText` 使用字幕，`tts_text` 独立传给 TTS，表情、图片和音效写入
`Actions`。这保证输出语义正确，但首段语音需等待整轮完成。要求最低首字延迟时可关闭结构化模式，
退回 P0 的 token 流式管线。

## 扩展边界

新增输入方式仍进入 `InputEnvelope`；新增输出端消费 `AgentReply → OutputIntent`，不应直接解析
模型原文。动作类型新增时应升级 schema version，旧消费者继续忽略未知 WebSocket 事件。
