# Aria 当前任务

> 最后更新：2026-08-27
> 详细设计入口：[00-文档索引与架构总览.md](./00-文档索引与架构总览.md)

本文件只维护当前执行队列、未完成门槛和最新质量基线。历史交付细节留在对应阶段文档，不在这里重复。

## 1. 阶段总览

| 阶段 | 状态 | 当前结论 |
|---|---|---|
| M0～M1 可信文字核心 | 已完成 | 事件、隐私、身份、聊天、Persona、Memory、Timeline 与删除闭环已落地 |
| P5 文字稳定性闸门 | 使用期未开始 | 14 天从首条有效日志重新起算；当前在制分支须先恢复全量质量闸门，见 `docs/32` |
| P6 Batch A～C 语音 | 主链完成 | 真浏览器 ASR/LLM/TTS/viseme/打断已打通；延迟继续优化 |
| M3A 地图/天气第一批 | 已完成 | 查询、定位、卡片、Admin 自检、200 条台账与延迟报告已落地，见 `docs/35` |
| M3A 多终端与感知 | 进行中 | Browser Bridge 与 macOS Desktop 已通过真机验收；主动输出已接入 Web、macOS 通知和在线语音，活动窗口仍待原生真机验收 |
| M3B 认知调度闭环 | 已完成（v1） | 被动对话与主动事件已统一进入 World State、Attention、结构化决策、反馈和审计闭环；自主写动作保持关闭 |
| 形象与主题底座 | 已完成（v1） | 形象包/实例/Persona 绑定、主题同步、自定义立绘与 Live2D 安全导入均已落地 |
| Admin 信息架构重整 | 已完成（v1） | 领域分组、URL 可恢复二级 Tab、真实数据页与浏览器验收已完成 |
| P6 Batch D Live2D/桌宠 | Web 最小壳完成 | Live2D Web 运行时、真实模型渲染和语音/情绪控制已接通；Tauri 透明桌宠仍受 M2 门槛约束 |

## 2. 当前执行队列

### 0. 当前功能主线

- [ ] **当前功能：macOS 系统内容选择器（代码闭环 + 隔离 E2E 已完成，剩真机人工环节）。** `capture_screen` 新增 `target=interactive`：Desktop 弹出系统选择器由用户当场框选/选窗（Esc 取消）；Hub 命令 TTL 115s、终态等待 116s，设备端选择器 100s 超时自动终止；Rust 结构化错误码（picker_cancelled/picker_timeout 等）经 command.reason_code 透传；幂等键纳入 target。已在隔离环境（8001 端口 + SQLite + 真实 GLM/视觉配置 + 虚拟桌面设备）完成两条 E2E：真实聊天 → interactive 命令（线上确认 TTL 115s 与幂等键）→ PNG 资产上传 → GLM 视觉准确描述合成图 → 回答回注；以及 picker_cancelled 透传 → 模型优雅解释并主动提出重试。剩余真机人工环节：Desktop 窗口点「允许下一次截图」、真实 `screencapture -i` 框选/取消、原图销毁现场确认（与 active_window 真机验收同批）。
- [ ] **随后功能：ESP32 + LD2410 第一硬件闭环。** 单存在传感器接入现有 MQTT → `EphemeralSignal` → Perception → Proactive Pipeline，开始 7 天免打扰与误触发验收。
- [ ] **暂不作为下一功能：Tauri 透明桌宠。** Live2D Web 最小壳已经可用，但桌宠仍需等待 M2 首音频和打断门槛达标；VRM、换装、AI 形象工厂和更多主题也继续后置。

### 0.1 并行收口项（不占用下一功能定义）

- [x] 恢复在制分支质量闸门：Ruff **163** 项、mypy **38** 项和 Admin TypeScript **4** 项已清零；非 soak 全量 pytest **532 通过 / 0 失败**（3 个既有全局 Admin token 状态污染失败已用 conftest autouse 重置 fixture 修复）；Chat/Admin/Shared typecheck 与 production build、Alembic 空库升级到 `0022` 单 head 和 `git diff --check` 全部通过。
- [x] 收口当前实现批次：迁移链 `0019 → 0020 → 0021 → bb15882faef4 → 0022` 已确认单 head，空库升级复验通过；`.design-qa/`、`.zcode/`、本地截图、`server/assets/` 运行时上传与授权 SDK/Core 已入 `.gitignore`，在制批次已分 9 个逻辑提交入库。
- [ ] 写入首条可核验每日日志，启动 P5 连续 14 天文字稳定性观察；M2 语音延迟优化作为并行性能专项推进。

### A. 多终端设备底座

- [x] Device Registry 后端：一次性配对码、每设备独立凭据、命名/别名、所有权、撤销、在线状态、心跳与 capability 授权白名单。
- [x] Device Command Channel 后端：客户端主动连接 `/ws/devices`，支持 HMAC 签名命令、TTL、设备级幂等键、取消、ACK、结果回执与超时状态。
- [x] Capability Registry 后端：终端心跳声明 `screen.capture`、`browser.current_tab.read/capture`、`sensor.read` 等能力；模型只看到在线声明与管理员授权的交集。
- [x] 目标设备解析：UUID/别名/名称精确匹配；通用“我的电脑”仅在唯一在线且有能力时自动选择，歧义时返回候选要求确认，离线时不改选。
- [x] Admin 设备页：一次性配对、在线状态、能力授权、测试命令、最近命令台账与一键撤销。
- [ ] 局域网/VPN 安全接入：每设备独立凭据，不把 Hub 或客户端裸露到公网。

### B. 电脑屏幕与网页理解

- [x] Tauri Desktop Client 安全连接壳：开机启动、托盘、系统凭据库、配对、签名长连接、心跳/重连和 `device.ping`。
- [x] 截图临时资产通道：设备鉴权上传、命令/owner 绑定、PNG/JPEG 魔数与 8 MiB 上限、2 分钟 TTL、读取即销毁，数据库仅保留摘要。
- [x] macOS 屏幕录制授权、隐私暂停与临时截图销毁闭环：原生 `.app` 已完成配对、钥匙串、TCC 授权和真实截图上传验收。
- [x] `capture_screen` 主显示器闭环：目标解析、签名命令、终态等待、临时图片 consume-on-read、视觉分析和文字结果回注；L1 使用支持工具调用的 dialogue 路由与当前 GLM 视觉，L2 仍强制本地工具模型与本地视觉。
- [x] `capture_screen(active_window)` 代码闭环：Hub/LLM 契约、Desktop 双端参数校验、macOS 前台应用与 CoreGraphics 活动窗口解析、按窗口 ID 截图和原有隐私门禁复用已落地；Python/TypeScript 回归、`cargo check` 与原生链接通过。
- [ ] `capture_screen(active_window)` 原生真机验收：在非 Aria 前台窗口上完成一次真实聊天工具全链，确认窗口定位、阴影剔除、临时资产销毁与视觉回答。
- [x] 系统内容选择器代码闭环与隔离 E2E：`target=interactive` 复用 screen.capture 命令与 TCC/锁屏/临时授权门禁，`screencapture -i` 交互框选、100s 选择器超时、用户取消（Esc 无产物即 picker_cancelled）、命令 TTL 115s/等待 116s、结构化错误码透传与幂等键按 target 隔离均已落地。隔离 E2E（真实 GLM 对话 + 虚拟桌面设备 + 真实视觉端点）验证：模型正确选择 interactive 工具、线上 TTL 115s、资产上传与视觉回答回注、picker_cancelled 优雅透传；同时发现弱祈使句下模型可能只描述动作而不调用工具（提示词/模型行为，非代码缺陷，真机验收时注意话术）。原生真机人工环节待做（与 active_window 验收同批）。
- [x] 修复迁移 SQLite 兼容 bug：`0020/bb15882faef4/0022` 迁移中 `server_default=sa.text("now()")` 在 SQLite 上插入即崩（PG 正常；单测走 `create_all` 未暴露），统一改为方言感知的 `sa.func.now()`；真实 PostgreSQL 已应用的迁移不受影响，SQLite 从空库升级 + 启动种子写入已实测通过。
- [x] 浏览器扩展真机闭环：Chrome MV3 配对/签名长连接、`browser.current_tab.read/capture`、受限 JSON/图片临时上传与 Hub `inspect_webpage` 已接入；真 Chrome 已验证单一稳定连接、正文读取和当前页截图。
- [ ] 隐私策略：当前按用户决定先允许 L1 屏幕截图交给 GLM 视觉；L2 仍强制本地视觉，浏览器读取仍保持 L2。Desktop 已实现锁屏自动拒绝和“5 分钟内仅下一次截图”的内存态临时授权，待 macOS 真机锁屏/解锁验收后勾选。
- [x] Web 对话跨终端验收：L2 会话发起“看一下我的电脑网页”，Hub 调用本地 Qwen 的 `inspect_webpage`，Browser Bridge 读取当前页并把结果返回原会话。

### C. 传感器与主动感知

- [x] Home Assistant HA-0/HA-2：状态同步、实体/字段白名单、`home_get_state`、`home_get_history`、`home_control`、动作白名单、空调确认门禁、脱敏工具台账和后台可视化配置已接入。
- [x] Home Assistant 真实实例联调：`https://ha.pnkx.top:8` 已完成 token 鉴权、11 个实体白名单、历史/Logbook 读取与健康验收；真实写动作仍需用户指定设备后现场验收。
- [x] MQTT 设备接入：`MqttDeviceClient` 客户端（`aiomqtt`）、遥测 topic 订阅、JSON schema 解析、`MqttTelemetryBuffer` 内存缓存（按 sensor_key 最近 100 条、TTL 去抖、freshness 判断）、`to_ephemeral_signals` 转换和自动重连已落地；`read_sensors` 工具已接入 MQTT 遥测。激活需设置 `ARIA_MQTT_ENABLED=true` 及 `ARIA_MQTT_HOST/PORT/USER/PASS`。
- [ ] 第一硬件闭环：ESP32 + LD2410 存在雷达。
- [x] 原始遥测进入 `EphemeralSignal`：MQTT 遥测经 `MqttTelemetryBuffer` 按 sensor_key 去抖，过期信号自动丢弃；L3 原始值仍保持不落库、不进日志、不进模型。
- [x] `read_sensors` 工具：统一读取 Home Assistant 实体和 MQTT 设备遥测；支持实体名称/别名、`device_id:sensor_type` 查询、`all` 读取全部 MQTT 传感器；HA/MQTT 结果统一为 readings 列表，TTL 缓存 freshness 标注；已注册到 `ToolRegistry`。
- [x] Perception Pipeline 第一批：HA `person` 与授权存在传感器经稳定窗口转换为 `user_arrived_home`、`user_left_home`、`presence.changed`；支持 TTL、重放幂等、并发跨来源合并和 L3 零审计。
- [x] Home Assistant 规则式主动引擎：持续时间判定、安静时段、冷却、每日上限、固定模板、审计台账和 WebSocket 主动投递已落地。
- [x] 通用 Proactive Policy v1：统一 DND/安静时段/每日预算、反馈降频、5 分钟跨来源合并、过期不补发；主动消息按事件 owner 选择其最近活动 Web 会话，禁止跨用户误投。
- [x] 主动多终端仲裁 v1：真实接入 Web 私聊、macOS 原生通知和在线语音；后台可控制总开关、逐通道启停、优先级、隐私上限、仅紧急与“全部/首个可用”，并持久化逐通道 DeliveryReceipt。
- [x] 通用 OutputRouter 收敛：抽象 `OutputAdapter` 协议（`name/available/deliver`）、`DeliveryIntent` 与 `DeliveryReceipt` 统一契约；Web 私聊、桌面通知、语音三条链分别实现 `WebChatAdapter`、`DesktopNotificationAdapter`、`VoiceAdapter`；`ProactiveDeliveryService` 按适配器优先级统一调度，原有 `_web/_desktop/_voice` 内部方法保留兼容。
- [ ] 完成单存在传感器 7 天验收：免打扰零违规，重复/误触发可解释。

### D. M3B 认知调度闭环

- [x] 定义 `SemanticEvent`、`WorldState`、`CognitiveDecision` 和 `ActionResult` 契约；决策只保存证据 ID、原因码、信心度、策略/模型版本，不持久化自由文本“内心活动”。
- [x] World State Builder：按事件有界组装当前环境、最近交互、真实在线能力、相关 Memory/Timeline、DND 和近期主动次数，不向模型倾倒全量原始输入。
- [x] Attention Engine：使用确定性规则完成紧急度、目标相关度、重复与反馈惩罚、打扰成本评分；低分静默，达阈值才调用模型。
- [x] CognitiveCycle：被动消息与 Home Assistant 主动事件进入同一认知管线，结构化输出 `ignore / record / inform / ask / suggest / escalate`；模型请求 `act` 时安全回退。
- [x] Goal/Commitment Store：区分用户明确目标、共享承诺和系统维护目标，支持状态、期限、来源、完成、取消与失效；用户/共享目标必须有手动或归属当前用户的消息证据。
- [x] Action Engine v1 安全边界：自主写动作保持关闭，非动作决策返回可审计结果，任何 `act` 输出都会被契约拒绝或安全回退；风险分级、幂等、结果回读和 `unknown_outcome` 是未来开放写动作前的硬门槛。
- [x] Feedback/Reflection：记录接受、忽略、稍后、禁止反馈，用于实时降频并生成带反馈证据、需确认的偏好候选；不直接改写 Memory、Persona 或安全策略。
- [x] 最小验收集覆盖“用户回家”“灯长时间开启”“水浸告警”，验证 DND 静默、询问、紧急升级、重复降频、L3 不落库和无证据禁止进入主动链路。

### E. 并行门槛与优化

- [ ] P5：从首条可核验每日日志开始连续 14 天真实文字使用；期满复跑闸门并定稿 `docs/32`。
- [ ] M2：评估流式 ASR 与低延迟语音专用 LLM，达到可行下限后重置窗口完成 20 个完整回合 + 20 个打断判卷。
- [ ] 本地语音质量：校准“小艾/只回答”等音近词；需要时安装并验收 Silero 与 openWakeWord。
- [x] P6 Batch D Web 最小壳：Live2D ZIP 安全导入、官方 Cubism Web Runtime 动态加载、聊天/Admin 真实模型渲染、viseme/说话/情绪/表情/动作控制与缺少运行时时安全降级均已接通。
- [ ] P6 Batch D Tauri 桌宠：仅在 M2 首音频 P90 ≤1.8s、打断 P90 ≤300ms 后进入透明置顶窗口、点击穿透、拖拽、性能与多屏验收。

### F. Admin 信息架构重整

- [x] 一级导航按“概览 / 伴侣核心 / 能力接入 / 运维治理”分组，合并记忆与时间线入口，并保留旧 `/timeline` 兼容跳转。
- [x] 二级 Tab 进入 URL query，可刷新恢复；已有总览、模型、Persona、记忆、时间线、删除台账、设备列表和命令台账复用真实页面。
- [x] 尚无查询接口的 Tab 使用明确的“待接入真实数据”状态页，不展示伪造指标；Admin typecheck 与 production build 通过。
- [x] 真实浏览器验收：登录后逐项检查一级模块、二级 Tab、旧 `/timeline` 跳转、刷新恢复和窄屏导航；发现的 P0/P1 交互问题当批修复。
- [x] 验收后按 `docs/05` 的实施顺序接真实数据：health/activity/usage/quality/conflicts/pairing/diagnostics + logs/privacy/system 三个聚合 dashboard（含实时日志独立 Tab）已全部接入真实数据；Trace、隐私审计、备份恢复和成本能力按 M3B 计划推进。

## 3. 最近完成

- [x] 屏幕感知 v1（用户显式推翻原「暂缓持续后台屏幕监控」决策，授权模型改为纯配置开关）：新增 `ScreenAwarenessLoop`（默认 60s、15~600s 可配、每 tick 热读配置）周期截取配置的各显示器 → Pillow 感知哈希变化检测（不变跳过分析）→ GLM 视觉输出结构化 JSON（summary/notable/memory_worthy/topic）→ Timeline 全量沉淀（`index_screen_observation`，event_type=screen.observed）+ memory_worthy 升级长期记忆（screen-v1）+ notable 经 Perception 管线走主动话题（DND/安静时段/预算/反馈降频全复用）；Desktop 新增 `screen.monitor` 命令（不消费单次授权，TCC/锁屏/隐私暂停三闸门保留）与能力声明；原图即焚（consume-on-read）；连续失败 10 分钟冷却；Admin 新增「屏幕感知」工作区（状态+观察记录）；12 个新回归全绿。详见 `docs/38`。
- [x] 工具挂载改为能力就绪制、选择交给模型：`send_message` 不再用文本关键词预筛工具——设备工具按「在线能力满足（`DEVICE_TOOL_REQUIREMENTS`）+ 隐私/模型/视觉就绪」全量挂载，查询工具按配置开关挂载，何时调用由模型依据工具描述自行判断；关键词表仅保留给确定性 HA 读回退（`_deterministic_home_read_call`）与选择器单测。起因是真机验收中「圈选屏幕内容」未命中词表导致 `capture_screen` 缺席；已同步在上一提交补充词表作为确定性路径的覆盖。
- [x] 修复撤销设备永久占用别名的缺陷：`device_client` 的 `(owner, alias)` 唯一约束改为部分唯一索引（仅约束 `revoked_at IS NULL` 的活跃行），撤销后别名自动释放、可用原别名重新配对，活跃设备之间仍强唯一；新增 `0023_device_alias_reuse` 迁移（batch 兼容 SQLite）并已应用到真实 PostgreSQL（现处 head），新增撤销重配与活跃冲突双向回归测试。
- [x] macOS 系统内容选择器代码闭环：`CaptureScreenTool` 新增 `target=interactive` 并在工具描述中与可无人值守的 `active_window` 明确区分（仅在用户明确要求选择/分享时使用）；Hub 侧 interactive 命令 TTL 115s、终态等待 116s，幂等键纳入 target；Desktop Rust 端 `screencapture -i` 交互框选、100 秒轮询超时自动 kill、Esc 取消（无产物判定）与 `CaptureError{code,message}` 结构化错误码；TS 端解析 interactive 并把结构化错误码作为 command.result reason_code 透传，旧字符串错误按锁屏/权限归类。新增 4 个 Python 回归与 2 个 vitest 用例；Ruff、mypy、非 soak 全量 pytest、Desktop typecheck/build/test、`cargo check` 全部通过。
- [x] 在制批次收口与质量闸门恢复：9 个逻辑提交（db 基座/TurnCoordinator/Jobs+AssetStore/Avatar/Theme/Live2D/HA 区域映射/Admin 实体接口/集成注册）入库；`.gitignore` 排除本地 QA 产物、agent 会话与 `server/assets/` 运行时上传；Ruff 163、mypy 38、Admin TS 4 清零；全量 pytest 532 通过且修复 3 个全局 Admin token 污染失败；TurnCoordinator `create_turn` 契约对齐真实 `ChatService.start_turn`；空库升级复验到 `0022`。
- [x] Live2D Web 最小壳与形象导入：`AvatarAssetImporter` 支持静态图片净化和 Live2D ZIP 安全校验，发行包多 runtime 时优先 PRO 并忽略 `.cmo3`/`.can3` 等工程源文件；聊天端和 Admin 通过同源运行时加载真实模型，转发 TTS viseme、说话状态、回复情绪、显式表情和动作指令。官方 Cubism Core 不入库，由本机已授权运行时目录提供；Hiyori 真模型已在聊天三栏界面渲染并完成 1920/1024/720 px 视觉验收。
- [x] 主题中心 v1：新增 `ui_theme`/`ui_preference` 与 `0022_ui_theme` 迁移，内置“纯净明亮”“静夜紫”和“跟随系统”选择；Admin 新增“外观与主题”工作区，账户偏好经 `/api/v1/admin/ui/*` 保存；聊天端经 `/api/v1/ui/preferences` 登录同步、本机回退、窗口聚焦刷新和同源 `BroadcastChannel` 即时切换；真实 PostgreSQL 已升级到 `0022`，浏览器验证 Admin 保存后 Chat 从浅色即时切到深色并可恢复。
- [x] M3B 认知调度闭环 v1 文档补齐与 Action/Reflection 代码完善：新增 `docs/37-M3B认知调度闭环设计.md` 系统性阐述六层架构（Semantic Event、World State、Attention、Deliberation、Action、Reflection）数据契约、流程与安全边界；Action Engine 实现 A0～A3 分级执行与结果回读闭环，v1 仅开放 A0/A1，任何 `act` 均被阻断并记录 `blocked` 结果；Reflection Engine 实现确定性反馈分析，按 trigger_kind 聚合近 30 天反馈生成带证据、需确认的偏好候选；新增 `action_result` 与 `reflection_candidate` 表及 `0019` 迁移，扩展 API `/action-results` 与 `/reflection-candidates`，新增 8 个对应回归测试。
- [x] 主动多终端输出 v1：Home Assistant/M3B 决策不再只进入 Web 调试台，可按后台热配置投递至 Web 私聊、macOS Desktop 系统通知和在线空闲语音会话；离线、锁屏、隐私暂停、能力未授权和云端 L2 TTS 均安全降级，每次实际尝试写入统一投递回执。真实 PostgreSQL 已从 `0014` 升级至 `0017`，Admin 主动测试接口返回成功；实测回执为 Web delivered、无空闲会话的 Voice failed，符合降级预期。
- [x] Perception Pipeline 第一批：新增隐私安全的语义事件审计、全局主动门禁、稳定窗口和跨来源幂等合并；HA 人员/存在状态已能自动进入 M3B CognitiveCycle，主动 Web 投递绑定事件 owner。
- [x] M3B 认知调度闭环 v1：有界 World State、确定性 Attention、结构化模型决策与安全回退、目标/承诺、反馈降频与反思候选、被动聊天和 Home Assistant 主动事件统一管线已落地；自主写动作保持关闭。
- [x] Desktop 屏幕安全闸门代码闭环：macOS 原生会话锁定状态进入 capability 声明与截图执行前双重检查；临时授权仅保存在进程内存、5 分钟过期且只消费一次，锁屏立即撤销；Desktop 协议测试增至 6 通过，TypeScript/Vite build 与原生 `cargo check` 通过。
- [x] Admin 信息架构骨架：集中式模块/Tab 定义、分组侧栏、URL 可恢复 Tab、工作区路由和 planned 状态页已接入；当前改动已通过 diff-check、Admin typecheck 与 production build，真实浏览器验收仍在执行队列。
- [x] L1 macOS 屏幕全链验收：真实 Web 会话触发 `capture_screen`，Desktop Client 上传截图，GLM Vision 识别当前聊天界面并生成准确最终回复；运行视觉由拥堵的 `glm-4.6v-flash` 切换为已实测的 `glm-4v-flash`，并增加 429/5xx 重试与 endpoint 输出上限。
- [x] L1 屏幕工具门控：`capture_screen` 可在 L1 使用支持工具调用的 dialogue 候选与已配置 GLM 视觉；L0 仍拒绝，L2 仍要求本地工具/视觉，`inspect_webpage` 未随之放宽；补充 L1/L0 与浏览器边界回归测试。
- [x] macOS Desktop 真机验收：安装 Rust 1.98、补齐 Tauri 应用图标，完成 debug `.app` 原生编译、一次性配对、系统钥匙串、屏幕录制 TCC 授权与 `screen.capture(main_display)` 实机命令；命令约 279ms 完成并上传 1.55 MB PNG 临时资产。
- [x] Browser Bridge 聊天全链验收：真实 Web 会话在 L2 下触发本地 Qwen 标准 `tool_calls`，通用“我的电脑”正确解析到唯一在线 browser 设备，当前页正文经临时资产回注并生成准确最终回复；同时修复通用目标只筛 desktop、private 双层 30 秒超时和 `reply.committed` 后状态文案不复位。
- [x] Browser Bridge 真 Chrome 验收：扩展完成配对并稳定保持单一 WebSocket；当前页 `read` 在约 45ms 内返回 188 B JSON，`capture` 在约 0.56s 内返回 2.74 MiB PNG；同时修复权限弹窗打断配对与旧连接 close 回调触发的重连风暴。
- [x] 网页工具门控解耦：`inspect_webpage` 的结构化正文读取只要求 L2 本地工具模型；网页截图分析仍要求 L2 本地视觉模型。`capture_screen` 后续按用户决定增加 L1 + GLM 视觉路径，L2 边界不变。LM Studio 的 `qwen3.6-35b-a3b-uncensored` 已实测返回标准 `tool_calls`。
- [x] Browser Bridge 第一批：Chrome 116+ MV3 扩展、显式网页权限、受信存储、20 秒心跳、命令白名单、当前页可见正文/截图与本地分析回注已完成。
- [x] Hub Screen Capture Tool：命令终态事件唤醒、设备歧义候选、本地 OpenAI-compatible 视觉 data URL、原图单次消费及 ChatService 三重可用性门控已接入。
- [x] 指定显示器截图：Hub / Desktop 双端限制目标与 1～32 显示器编号，映射 macOS `screencapture -D<n>`；默认仍为主显示器。
- [x] Desktop `screen.capture` 实现：仅在 macOS TCC 已授权且隐私暂停关闭时声明能力，单次截取主显示器或指定编号显示器、鉴权上传，RAII 清理本机临时文件；原生真机验收已通过。
- [x] Ephemeral Device Asset Store：截图不进入命令 JSON 或数据库，上传内容只在有界进程内存中短暂存在并 consume-on-read。
- [x] Device Target Resolver：按 owner 隔离，支持精确目标、通用桌面目标、capability/在线复核、歧义候选与禁止静默 fallback。
- [x] Desktop Client 安全连接壳：Tauri 2、OS keyring、配对、托盘/开机启动、签名验签、心跳/重连、TTL/取消/幂等和 `device.ping` 已接入并通过 macOS 原生真机验收。
- [x] Admin 设备工作区：设备统计/筛选、配对码、能力交集、revision 冲突保护、测试命令、命令状态与撤销交互已接入 Vue 后台。
- [x] Device Command Channel 第一批：`0013_device_command`、鉴权长连接、HMAC-SHA256 命令签名、脱敏命令台账、离线失败、TTL/超时、幂等冲突、取消与 ACK/结果回执已接入。
- [x] Device Registry 第一批：`0012_device_registry`、一次性配对、凭据哈希、心跳、撤销、乐观 revision 与授权能力交集已接入；在线有效能力已进入聊天现实能力边界。
- [x] 地图/天气工具：真实高德天气、附近 POI、路线、浏览器临时定位、模糊候选、TTL 缓存和结构化卡片。
- [x] 地图运维：独立自检、`ToolLedger(maxlen=200)`、成功率/P50/P90/缓存命中率与失败聚合、Admin 展示。
- [x] 语音 Batch A～C：本地 faster-whisper、MiMo/edge TTS、流式分句、viseme、打断、延迟滑窗与真浏览器全链。
- [x] M2 诊断：打断 82ms 达标；当前组合理论首音频下限约 3.3s，1.8s 指标需要架构优化而非继续机械计样。
- [x] P5 可开发项：多主体记忆、Timeline、L2 隔离、删除闭环、前后端闸门和真实模型回归。
- [x] Admin 全部 22 个"待接入真实数据"页面接入真实数据：overview（health/usage/activity）、memory（quality/conflicts）、devices（pairing/diagnostics）、logs/privacy/system 三个聚合 dashboard + 15 个 tab 视图。
- [x] 管理后台实时日志：后端 `LogBroadcastHandler` + SSE 流 (`/api/v1/admin/logs/stream`) + 独立 `LiveLogsView` Tab；支持历史预加载、级别过滤、暂停/清空/重连。
- [x] 管理后台安全设置：支持在 `system → 身份与会话` 中修改 Admin Token（运行时即时生效，无需重启）和重置聊天密码（自动撤销所有活跃会话，强制重新登录）。
- [x] MQTT 设备接入、OutputRouter 收敛与 `read_sensors` 工具：新增 `app/devices/mqtt_client.py`、`app/output/adapter.py`、`app/output/protocols.py`、三个 `OutputAdapter` 实现、`app/tools/sensors.py`；修复 `SourceRef`/`EphemeralSignal` 构造以符合 schema 契约；新增 35 个单元测试全部通过。
- [x] TurnCoordinator 运行状态机与多端同步骨架：`app/runtime/turn_coordinator.py` + `lease.py` + `turn_recovery.py` 实现 9 状态回合生命周期（CAS 转移、打断传播、generation 活性检查、stale 拒绝）、音频输出/麦克风租约仲裁、`UserMode` 优先级管理、重启后不安全回合自动恢复；集成到 `ChatWebSocketManager` 和 `VoiceWebSocketManager`，文本/语音回合在 `thinking → streaming → completed/failed/cancelled` 全链路上报状态；新增 22 个单元测试全部通过。
- [x] Job System + Asset Store 长任务底座：`app/jobs/engine.py` 实现 Job 提交（幂等键）、Worker 领取/续约/释放/心跳/过期清理、Step 生命周期（开始/完成/失败与重试退避）、取消与确认、成功终态；9 状态转移图 + 幂等提交 + 租约仲裁；`app/jobs/asset_store.py` 实现内容寻址存储（SHA-256 去重）、引用计数生命周期（staging → active → unreferenced → deleted）、派生关系与 staging/未引用 GC；Admin 后台路由 `/api/v1/admin/jobs` 支持列表/详情/取消/过期租约清理；集成到 `main.py`；新增 `job`/`job_step`/`job_artifact`/`asset`/`asset_reference`/`asset_derivation` 表；新增 28 个单元测试全部通过。
- [x] 伴侣形象与角色系统 v1 骨架：`app/avatar/store.py` 实现 AvatarStore（形象包管理、实例创建/更新/删除、人格绑定与默认形象查询）；内置 `warm-daily`（静态）与 `light-core`（抽象）两个种子形象包；新增 `avatar_pack`/`avatar_instance`/`persona_avatar_binding` 表；Admin 后台路由 `/api/v1/admin/avatars` 支持包列表/实例列表/创建/更新/删除/绑定/查询默认形象；`ChatService` 在 `decision_meta` 中注入当前人格默认形象的 `avatar_instance_id` 与 `avatar_pack_id`；新增 11 个单元测试全部通过。

## 4. 最新质量基线

- 2026-08-27 设备别名释放修复：Ruff、严格 mypy、非 soak 全量 pytest 通过（含新增撤销重配回归）；`0023_device_alias_reuse` 已在 SQLite 空库与真实 PostgreSQL 双端验证，真实库现处 head `0023`。同批 Admin 交互修复：设备设置保存遇 revision 冲突自动刷新版本号并提示重试（`a9ab560`）、设备注册表行级配对码按钮（`249eae7`）、Desktop debug bundle 过期问题（`ef0420e`，`bundle.active` 已启用）。
- 2026-08-27 选择器隔离 E2E 与迁移修复：迁移 `now()` 默认值修复后 Ruff、严格 mypy、Avatar/Theme/Jobs 定向 pytest 通过；隔离环境（8001 + SQLite + 复制的真实模型配置 + 虚拟桌面设备）两条 E2E 全部闭环——interactive 命令线上 TTL 115s、幂等键含 target、PNG 资产上传后 GLM 视觉准确描述、picker_cancelled 透传后模型优雅重试。已知观察：弱祈使句下模型可能只叙述不调用工具；Desktop 真机 .app 本次 WS 鉴权未完成（last_seen 不随连接推进，待用户查看窗口状态）。
- 2026-08-27 系统内容选择器代码闭环：Ruff、严格 mypy、非 soak 全量 pytest **532 通过 / 0 失败**（含 4 个新增 interactive 回归）、Desktop vitest **9 通过**、Desktop typecheck/build、`cargo check` 与 `git diff --check` 全部通过；真机验收（真实聊天 → 系统选择器 → 视觉回答 → 原图销毁）待用户在场执行。
- 2026-08-27 质量闸门恢复：在制批次分 9 个逻辑提交入库后，Ruff（含 `allowed-confusables` 白名单全角标点）、严格 mypy（132→214 source files）、Admin/Chat/Shared typecheck 与 production build 全部通过；非 soak 全量 pytest **532 通过 / 0 失败**（新增 conftest autouse fixture 修复 3 个既有全局 Admin token 状态污染失败；ruff/mypy 版本随本批次升级，TurnCoordinator `create_turn` 已对齐真实 `ChatService.start_turn` 契约，原 `input_message_id` 参数为运行时 TypeError 隐患）；Alembic 空库升级到单 head `0022_ui_theme`、`git diff --check` 通过。
- 2026-08-27 当前在制分支：Avatar/Theme 定向 pytest **25 通过**；Shared 与 Chat TypeScript typecheck 通过；Alembic 为单 head `0022_ui_theme`；文档同步后 `git diff --check` 通过。发布闸门尚未恢复：全仓 Ruff 报 **163** 项，严格 mypy 报 **38** 项（5 个文件），Admin typecheck 报 **4** 项；因此此前“全量通过”只代表对应历史快照，不能作为当前分支结论。
- 2026-08-26 主题中心 v1：新增主题 Store、Admin API 与 Chat 会话 API 的 4 个测试全部通过，OpenAPI 路由生成回归通过；Ruff、新 Store mypy、Shared typecheck、Chat production build、Admin production build、Alembic 单 head 与离线 SQL 生成通过；真实 PostgreSQL 已升级到 `0022_ui_theme`；全量 pytest 运行至 97% 时仍复现 3 个既有全局 Admin token 状态污染失败，并挂在既有语音 soak 用例；过程中发现的主题路由 OpenAPI 注解回归已修复并单独复测通过。
- 2026-08-26 伴侣形象与角色系统 v1：Ruff、全量 mypy、pytest **430 通过 / 2 跳过**（新增 11 个测试）、`git diff --check` 全部通过；新增测试覆盖 `AvatarStore`（内置包加载/幂等、实例创建/更新/删除/查询、未知包拦截、状态过滤、人格绑定/解绑/默认查询/列表）；Admin Avatar API 已挂载到 `/api/v1/admin/avatars`；`ChatService.decision_meta` 已注入 `avatar_instance_id` 与 `avatar_pack_id`；
- 2026-08-26 Job System + Asset Store：Ruff、全量 mypy、pytest **419 通过 / 2 跳过**（新增 28 个测试）、`git diff --check` 全部通过；新增测试覆盖 `JobEngine`（提交/幂等/领取/优先级/续约/释放/Step 生命周期/失败重试/最终失败/取消队列中/running 中/确认取消/成功/列表/Worker 心跳/租约过期清理）、`AssetStore`（存储/去重/提交/读取/引用生命周期/重新激活/派生/staging GC/未引用 GC）；Admin Jobs API 已挂载到 `/api/v1/admin/jobs`；
- 2026-08-26 TurnCoordinator 与运行状态机：Ruff、全量 mypy、pytest **391 通过 / 2 跳过**、`git diff --check` 全部通过；新增 22 个单元测试覆盖 `LeaseManager`（获取/抢占/续约/释放/过期）、`TurnCoordinator`（文本/语音回合创建、CAS 状态转移、非法转移拦截、打断与非可打断状态、generation 活性检查、stale 拒绝、重启恢复、用户模式优先级/过期、音频租约与麦克风仲裁）；Chat/Voice WebSocket 全链路已接入状态机与租约管理；
- 2026-08-26 MQTT/OutputRouter/传感器工具：Ruff、全量 mypy、pytest **369 通过 / 2 跳过**、`git diff --check` 全部通过；新增 35 个单元测试覆盖 `MqttTelemetryBuffer`（push/latest/freshness/信号转换）、`MqttDeviceClient`（生命周期/消息解析/错误恢复）、三个 `OutputAdapter`（Web/Desktop/Voice 成功与失败路径）和 `ReadSensorsTool`（HA/MQTT/混合查询/空 provider/参数校验）；
- 2026-08-25 M3B 文档与 Action/Reflection 补齐：Ruff、全量 mypy（132 source files）、pytest **337 通过 / 2 跳过**、Alembic 从空库升级到 `0019_action_and_reflection`、单 head、`git diff --check` 全部通过；新增验收覆盖 Action Engine 分级映射、A2/A3 v1 阻断、结果回读持久化、Reflection Engine `_analyse` 单元、FeedbackSummary 聚合和候选生成；
- 2026-08-25 主动多终端输出 v1：Ruff、全量 mypy、pytest **325 通过 / 2 跳过**、Alembic 从空库和真实 PostgreSQL 均升级到 `0017_proactive_delivery_receipts`、单 head、Admin/Chat/Desktop typecheck 与 production build、Desktop 协议测试 **7 通过**、`cargo check` 和 `git diff --check` 全部通过；真实 Admin 主动测试接口返回 `ok=true`，回执表写入 1 条 delivered 与 1 条预期内 failed；新增验收覆盖三通道仲裁、优先级、隐私/紧急门禁、持久化回执及 L2 语音禁止云 TTS；
- 2026-08-25 Perception Pipeline 第一批：Ruff、全量 mypy、pytest **319 通过 / 2 跳过**、Alembic 从空库升级到 `0016_perception_pipeline`、单 head 与 `git diff --check` 全部通过；验收覆盖并发跨来源合并、事件重放、DND/预算/紧急绕过、稳定窗口失败、TTL、L3 零持久化、HA 语义映射与 owner 投递隔离；
- 2026-08-25 M3B v1：Ruff、全量 mypy、pytest **312 通过 / 2 跳过**、Alembic 从空库升级到 `0015_cognitive_cycle`、单 head 与 `git diff --check` 全部通过；新增验收覆盖三类主动场景、被动聊天审计、DND、重复降频、反馈候选、模型 `act` 安全回退和 L3 不落库；
- 2026-08-25 活动窗口代码闭环：Ruff、全量 mypy、pytest **304 通过 / 2 跳过**、Alembic 单 head、Desktop 协议测试 **6 通过**、Desktop typecheck/build、`cargo check`、原生 `cargo build` 与 `git diff --check` 全部通过；`active_window` 真实聊天全链待原生客户端临时授权后验收；
- 2026-08-24 已提交主线基线：pytest **287 通过 / 2 跳过**，Desktop 协议测试 **5 通过**、Browser Bridge 协议测试 **3 通过**，Ruff、全量 mypy、Alembic 单 head、Chat/Shared/Desktop/Browser typecheck 与 production build 全部通过；macOS Desktop debug `.app` 原生编译、TCC 截图与 L1 GLM 视觉全链验收通过；
- 2026-08-24 当前 Desktop 安全闸门改动：协议测试 **6 通过**、Desktop typecheck/build、原生 `cargo check` 与 `git diff --check` 通过；本机 Rust stable 缺少 `rustfmt` 组件，未运行 `cargo fmt --check`；
- 2026-08-24 当前未提交 Admin 重整：`git diff --check`、Admin typecheck 和 Admin production build 通过；全仓闸门无需在仅前端在制改动阶段重复冒充为当前验证结果，浏览器验收后再复跑并刷新本节；
- 2026-08-24 当前 Home Assistant HA-0/HA-1 改动：全量 mypy 通过，pytest **293 通过 / 2 跳过**；`home_get_state`、默认拒绝、L3 工具隔离和 HA 错误脱敏回归已覆盖，真实实例鉴权等待专用 token；
- CI 的 mypy 范围已与本地发布闸门对齐为 `server/app server/tests`；
- 运行配置：v43；L1 视觉为已实测的 `glm-4v-flash`，本地 `local_private` 工具调用已通过真实探测并启用，端点与 private 路由超时均为 120 秒，本地 ASR 为 faster-whisper `base/cpu/int8`；
- PostgreSQL/pgvector 两项集成测试在本地无 `ARIA_TEST_DATABASE_URL` 时跳过，推送后由 CI PostgreSQL 服务执行。

## 5. 暂缓

- M2 未达标前的 Live2D/桌宠产品化；
- ~~持续后台屏幕监控~~：已由「屏幕感知 v1」取代（2026-08-27 用户显式决策，纯配置开关 + 设备端 TCC/锁屏/隐私暂停三闸门，见 `docs/38`）；
- 摄像头和高风险健康推断；
- 完整形象中心、多形象、主题增强、VRM 和静态图动态化；
- Open-LLM-VTuber 深度 fork；
- 多传感器扩展，先完成单存在传感器闭环。
