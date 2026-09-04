# Aria 当前任务

> 最后更新：2026-09-04
> 详细设计入口：[00-文档索引与架构总览.md](./00-文档索引与架构总览.md)

本文件只维护当前执行队列、未完成门槛和最新质量基线。历史交付细节留在对应阶段文档，不在这里重复。

## 1. 阶段总览

| 阶段 | 状态 | 当前结论 |
|---|---|---|
| M0～M1 可信文字核心 | 已完成 | 事件、隐私、身份、聊天、Persona、Memory、Timeline 与删除闭环已落地 |
| P5 文字稳定性闸门 | 已完成，待归档证据索引 | 用户已确认 14 天观察完成；仓库报告保留外部日志索引回填项，见 `docs/32` |
| P6 Batch A～C 语音 | 主链完成 | 真浏览器 ASR/LLM/TTS/viseme/打断已打通；延迟继续优化 |
| M3A 地图/天气第一批 | 已完成 | 查询、定位、卡片、Admin 自检、200 条台账与延迟报告已落地，见 `docs/35` |
| M3A 多终端与感知 | 进行中 | Browser Bridge、macOS Desktop 活动窗口/交互选择器与隐私门禁已通过真机验收；主动输出已接入 Web、macOS 通知和在线语音 |
| M3B 认知调度闭环 | v1 完成，Action v2 进行中 | 被动/主动认知闭环已落地；ACT-01～03 已完成，ACT-04 已扩展灯光亮度、媒体和 L0/L1 桌面通知，模型自主 `act` 仍关闭 |
| 形象与主题底座 | 已完成（v1） | 形象包/实例/Persona 绑定、主题同步、自定义立绘与 Live2D 安全导入均已落地 |
| Admin 信息架构重整 | 已完成（v1） | 领域分组、URL 可恢复二级 Tab、真实数据页与浏览器验收已完成 |
| P6 Batch D Live2D/桌宠 | 联动代码完成 | 透明窗口、拖拽、穿透、位置恢复、Hub 同源舞台及签名情绪/动作/口型同步已接通；M2 与真机性能仍是发布门槛 |
| M4B 手机 PWA | 进行中 | 可安装壳、离线降级、移动布局、前后台恢复、移动音频解锁及游标分页补拉/去重底座已完成；待真机验收、通知与多端租约 |
| 个人管家闭环 J1～J8 | J2 完成，J3/J4 进行中 | ACT-01～03 已完成、ACT-04 进行中；J2 四项全部代码闭环；SAT-01/02 确定性核心已落地（音频与真机待硬件）；CAL-01 日历代码闭环、聊天端建提醒/建日程工具已落地，TODO-01 pnkx 真机联调通过；MAIL v1 已落地（真机联调通过）；CONTACT-01 联系人代码闭环（真机验收待做）；J5～J8 未启动，见 `docs/39` |

## 2. 当前执行队列

### 0. 当前功能主线

**当前主线：手机 PWA。** 用户于 2026-08-29 明确将移动端提前；先完成可安装、可聊天、可恢复连接的第一批，再做通知与多端会话漫游。桌宠真机性能和 M2 20+20 计样保留为并行发布门槛，不阻塞移动端开发。

- [x] **macOS 系统内容选择器。** `capture_screen(target=interactive)` 的系统框选/取消、视觉回答、结构化错误码、临时授权与原图销毁已经完成代码、隔离 E2E 和用户确认的真机验收。
- [ ] **硬件到位后恢复：ESP32 + LD2410 第一硬件闭环。** Hub 侧 MQTT → L3 `EphemeralSignal` → 5 秒稳定窗 → L1 `presence.changed` → Perception → Proactive Pipeline 已接通，ESPHome 固件样例与单设备 topic ACL 已落地；因暂时没有硬件，剩余刷写真机与 7 天验收不阻塞软件主线。
- [x] **手机 PWA Batch A：可安装移动壳。** 现有 Chat 已增加 manifest、多尺寸/可遮罩图标、Service Worker 与离线降级页；窄屏改为顶部导航 + 会话/形象双抽屉，补齐刘海屏安全区、44px 触摸目标和输入法友好字号。安装后模式的访问令牌只进入 `sessionStorage`，不写长期 `localStorage`。
- [ ] **手机 PWA Batch B：真机聊天闭环。** 前后台恢复逻辑已完成：回到前台会补拉当前会话并重连/同步 WebSocket，进入后台会释放语音会话，离线时停止发送并在网络恢复后自动同步。移动浏览器会在麦克风、播报开关或发送按钮的可信用户手势内预先解锁 `AudioContext`，后续 TTS 解码/播放失败会结束忙碌态并显示可操作提示。剩余是在 iOS Safari 和 Android Chrome 完成添加到主屏、登录、文字/streaming、麦克风/定位权限、TTS 播放与异常降级真机验收。
- [ ] **手机 PWA Batch C：通知与会话漫游。** 会话游标补拉与消息去重底座已完成：REST 支持 `after_seq` 增量查询和 500 条分页循环，WebSocket 每页 200 条并用 `has_more/next_after_seq` 自动续拉，客户端按消息 ID 去重后按 `seq` 稳定合并。移动通知 endpoint 代码闭环（2026-09-03）：`0032_web_push` 迁移建 `push_subscription` 表并放宽回执 channel 约束、`/api/v1/push`（VAPID 公钥下发/订阅/退订）、`WebPushAdapter` 以 `web_push` 通道接入主动投递（通知正文按 `entity_id` 打 tag 去重、404/410 自动清订阅、L2 由配置校验硬禁）、Chat「🔔移动通知」开关（权限申请在用户手势内）与 SW `push`/`notificationclick`（聚焦/打开聊天回到前台补拉）已接通。多端音频与麦克风租约代码闭环（2026-09-04）：连接级稳定 `device_id` 作租约持有者，"最新获取者抢占、旧持有者尽快停止"——新语音回合抢占 audio_output 并向旧会话发 `voice.audio_preempted` 打断其回合，话语采集开始抢占 microphone 并发 `voice.microphone_preempted` 停止旧采集，speak 逐句续约失持即降级纯文字，桌宠播报纳管租约、逐块检查被抢占即中止；客户端处理两个抢占事件（停本地采集/清播放队列）；修复 interrupt 按 generation 误释放与 recover_after_restart 未接线，启动时恢复不安全回合并清理过期租约。剩余：VAPID 密钥生成与真实推送服务、双端同时语音的真机验收；L2 文本不进入系统通知。
- [ ] **并行门槛：Tauri 透明桌宠真机收口。** 待验收 macOS 热插拔、60fps、常驻内存 <300MB 和长时间运行。
- [ ] **并行门槛：M2 语音延迟与识别质量。** 重置统计窗口后完成 20 个完整回合 + 20 个打断样本，新低延迟模型端点继续暂缓。

### 0.1 并行收口项（不占用下一功能定义）

- [x] 恢复在制分支质量闸门：Ruff **163** 项、mypy **38** 项和 Admin TypeScript **4** 项已清零；非 soak 全量 pytest **532 通过 / 0 失败**（3 个既有全局 Admin token 状态污染失败已用 conftest autouse 重置 fixture 修复）；Chat/Admin/Shared typecheck 与 production build、Alembic 空库升级到 `0022` 单 head 和 `git diff --check` 全部通过。
- [x] 收口当前实现批次：迁移链 `0019 → 0020 → 0021 → bb15882faef4 → 0022 → 0023` 已确认单 head，空库升级复验通过；`.design-qa/`、`.zcode/`、本地截图、`server/assets/` 运行时上传与授权 SDK/Core 已入 `.gitignore`，在制批次已分 9 个逻辑提交入库。
- [x] P5 连续 14 天文字稳定性观察已由用户确认完成；外部每日记录的仓库证据索引仍需回填到 `docs/32`。M2 语音延迟优化作为并行性能专项推进。

### 0.2 PWA 收口后的个人管家能力队列

完整范围、风险边界、验收和预估见 [39-个人管家能力路线图.md](./39-个人管家能力路线图.md)。以下顺序是当前认可的实施队列；J1 开始前必须先完成 PWA Batch B/C、多端租约，并保留 M2 与桌宠发布门槛。

#### J1 安全行动引擎 v2

- [x] `ACT-01` 动作注册表：已落地独立于 `ToolRegistry` 的动作安全目录，声明参数 Schema、A0～A3 风险、可逆性、确认策略、幂等范围、超时、回读验证器和补偿动作；首批注册灯光/开关开关及空调设温，鉴权目录接口为 `GET /api/v1/cognition/actions/catalog`。
- [x] `ACT-02` 计划—确认—执行：新增持久化 `action_plan/action_step`、`0024_action_plan` 迁移及创建/查询/确认/取消/执行 API；Runner 只接受注册表预编译的工具和参数，顺序执行、部分成功、后续跳过、执行前取消、超时/中断 `unknown_outcome` 和禁止自动重试均已落地。A1 当前默认仍需确认，A2 必须每次确认，模型自主 `act` 继续关闭。
- [x] `ACT-03` 结果回读与撤销：HA 控制响应会刷新状态缓存，Runner 对开关状态和空调目标温度执行写后回读；步骤持久化验证状态、最小证据和验证时间，不一致或验证器不可用统一记为 `unknown_outcome` 且不重试。已完成的可逆步骤可通过 `POST /api/v1/cognition/action-plans/{plan_id}/undo` 逆序生成幂等补偿计划，补偿计划必须重新确认且禁止递归撤销。
- [ ] `ACT-04` 首批真实动作（进行中）：已注册并接通灯光亮度、媒体播放/暂停、音量设置及 L0/L1 桌面通知；亮度及播放控制为 A1 预授权候选，音量为 A2 每次确认，HA 动作具备写后回读。桌面通知复用 `notification.show` 能力，只接受显式 L0/L1、步骤幂等键和设备成功回执。提醒仍依赖 TASK-01 的持久调度，打开应用/网页仍依赖新增桌面白名单能力；这些尚未完成，不通过通用命令提前开放。A3 继续默认禁止。

#### J2 任务与主动管家

- [x] `TASK-01` 提醒与计划任务（代码闭环，待真实投递验收）：新增 `task_item` 表与 `0026_task_reminder` 迁移、`TaskStore`/`TaskScheduler` 与用户 API `/api/v1/tasks`（创建/列表/详情/完成/取消/稍后）。时间触发支持一次性与 daily/weekdays/weekly/interval 周期，错过的周期不补发；事件触发精确匹配 Perception 语义事件（如 `user_arrived_home`）并受 cooldown 限制。exactly-once 由 claim 的 `status+fire_count` 乐观守卫保证；一次性任务触发期短暂处于 firing，投递结束转 done，进程中断遗留的 firing 重启后直接判完成不重复投递。投递复用 `ProactiveDeliveryService`（Web 私聊/桌面通知/在线语音按通道配置仲裁），提醒为用户显式请求不消耗主动每日预算，通道启停与隐私上限仍生效；调度器默认 15s 轮询（`ARIA_TASK_SCHEDULER_INTERVAL` 可调），首个 tick 前等待一个完整间隔。位置触发以 HA person 实体派生的到家/离家事件先行覆盖；聊天端自然语言建提醒已由 `reminder_create` 工具落地（见最近完成）；Admin 任务可视化与真实多通道投递验收尚未完成。
- [x] `GOAL-01` 承诺跟踪（代码闭环，待真实验收）：`cognitive_goal` 新增提醒状态列（`0027_goal_reminders` 迁移）；到期前 24h 与到期后各提醒一次，时间戳列即乐观守卫保证 exactly-once；`GoalReminderScheduler`（默认 60s，`ARIA_GOAL_REMINDER_INTERVAL` 可调）经 `ProactiveDeliveryService` 投递，完成/取消/过期目标不再打扰。`GoalTracker` 在聊天后台用 utility 路由识别第一人称明确承诺（置信度 ≥0.7、疑问/假设/愿望/转述一律不提取、无后端或坏输出不提取、不做规则兜底），以消息 ID 为证据建目标且天然幂等；完成仍只能由用户显式 PATCH，不擅自标记。忽略降频：`POST /api/v1/cognition/goals/{id}/reminder-feedback`（ignored 顺延一天并计数 / snoozed 推迟指定分钟）。
- [x] `BRIEF-01` 每日智能简报（代码闭环，待真实验收）：`daily_brief` 表（`0028_daily_brief` 迁移，`(user_id, brief_date)` 唯一保证每日 exactly-once）+ `DailyBriefService`/`DailyBriefScheduler`。事实采集确定性：天气（高德实时+当日预报，默认城市取 `config.tools.query.default_city` 或 `ARIA_BRIEF_CITY`，失败仅少一条事实）、当日会触发的时间任务（`task:{id}`）、当日到期/已过期承诺（`goal:{id}`，过期标注）；正文用确定性模板拼装而非 LLM——任务/目标标题是用户文本，进模型提示词既有注入面又不可溯源。每条事实持久化来源引用满足"结论可查看来源"；无任务与承诺时正文一行短句。本地时区（`ARIA_DEFAULT_TIMEZONE`）到 `ARIA_BRIEF_TIME`（默认 08:00）后为每个活跃用户投递一次，经 `ProactiveDeliveryService` 走 Web/桌面/语音通道；用户 API `GET /api/v1/briefs(/latest)` 与 `POST /api/v1/briefs/generate`（幂等预览不投递）。日程/家庭状态/通勤待 J4 日历与对应真源接入后扩展，不伪造数据。
- [x] `REVIEW-01` 晚间回顾（代码闭环，待真实验收）：`daily_review` 表（`0029_daily_review` 迁移，`(user_id, review_date)` 唯一保证每晚 exactly-once）+ `DailyReviewService`/`DailyReviewScheduler`。四区块确定性采集（各带来源引用）：完成事项（当日完成的任务与承诺）、未完成计划（到期未完成，逾期标注）、新承诺（当日新建）、明日重点（明天触发的任务/到期承诺）；全部为空时一行短句。逐项修正：`PATCH /api/v1/reviews/{id}/items/{index}` 支持 confirm/remove/附注 note，修正只改回顾条目并重渲染正文，不触碰任务/目标真源状态，也绝不改写 Persona 或记忆。本地时区到 `ARIA_REVIEW_TIME`（默认 21:30）经主动通道投递一次；`GET /api/v1/reviews/latest` 与幂等 `POST /generate` 预览。

#### J3 全屋语音卫星

- [ ] `XIAOAI` 小爱音箱网关（代码闭环，待真机验收）：非官方个人接入，把小爱音箱当作文字终端——音箱保留唤醒词/ASR/TTS 所有权，小米凭据不进 Hub 进程。独立 `integrations/xiaoai_gateway` 容器（`@mi-gpt/next`）只把匹配触发前缀（默认"请阿莉娅"）的识别文本经 `/ws/adapters/xiaoai` 网关令牌鉴权送到 Hub；Hub 侧复用 `ChatService` 语音路由流式分句回传，逐句交给音箱 TTS（MiNA 或型号专用 MIoT TTS action）。配置中心 `integrations.xiaoai` 保存账号/音箱/中枢用户/网关密钥（secret 双模式，Admin GET 脱敏、保存还原掩码），发布即物化到 `xiaoai-state` 共享卷供网关热读取；Admin「HA 实体授权」页新增网关卡片（音箱下拉来自 HA 设备注册表型号、密钥随机生成）。剩余：真实小米账号登录（触发验证码需 passToken）、真机端到端与长期稳定性。
- [ ] `SAT-01` 房间终端协议（进行中）：`app/satellite` 模块已落地确定性核心——`idle/listening/processing/speaking` 四态状态机（非法转移抛 `InvalidSatelliteTransition`，cancel/error 任意态回 idle）、`SatelliteRegistry` 内存会话（重连重置 idle、计数保留观测、掉线注销）与设备命令通道三类签名帧（`satellite.hello` 注册房间、`satellite.wake` 唤醒上报、`satellite.state` 状态上报；`idle→listening` 只能由 Hub 仲裁下发，设备重申 idle 幂等接受），全部要求 `voice.satellite` 能力授权；SAT-02 的仲裁核心已随做（见下）。剩余：唤醒词/VAD 的设备端实现、音频上下行帧与单终端真机全链（旧手机/树莓派验证）。
- [ ] `SAT-02` 就近响应（仲裁核心已随 SAT-01 落地）：`arbitrate_wake` 确定性判定——同 owner 已有进行中会话压制（session_active）、仲裁窗口内重复唤醒压制（arbitration_window，默认 2s）、否则胜出进入 listening；多终端同时听到唤醒词只有一个响应。剩余：普通/紧急播报策略与房间标签路由。
- [ ] `SAT-03` 连续对话：多轮免唤醒、超时、打断和跨设备安全接管。

#### J4 个人信息连接器

- [x] `CAL-01` 日历（代码闭环，待真实验收）：v1 以 Hub 本地库为日历真源（`calendar_event` 表，`0030_calendar` 迁移同时给 `task_item` 加 `source_ref` 关联列）；外部 CalDAV/Google 后续按同契约接入。`CalendarStore` 半开区间重叠查询（首尾相接不算冲突）；`CalendarService` 提供 `preview`（纯读：规范化展示时间/参与人/目标日历 + 冲突列表，不落库，对应验收「写入前展示」）与显式 create/patch/cancel；会前提醒复用 TASK-01 调度底座（`source_ref=calendar:{id}`，默认提前 10 分钟，事件太近则尽快提醒；改期撤旧建新、取消撤旧，不引入新调度器）。API `/api/v1/calendar/events(/preview)` 全部走聊天会话鉴权；聊天端经 `calendar_create` 受控工具接入（两段式：先预览复述、用户确认后落库，时间冲突服务端硬拦），不开放模型对底层 API 的直呼。剩余：外部日历同步。
- [x] `TODO-01` 单一任务真源（对接 pnkx，2026-09-03 真机联调通过）：**pnkx 待办为唯一真源**，Hub `task_item` 作本地镜像（`0031_todo_sync` 迁移加 `external_updated_at/priority/group_label` 三列）。鉴权按用户决策采用**内部集成令牌**：pnkx 侧新增 `IntegrationTokenFilter`（`X-Integration-Token` 头常量时间比对，绑定 `pnkx.integration.userId` 指定身份，未配置完全不生效），Aria 侧 `ARIA_PNKX_BASE_URL` + `ARIA_PNKX_TOKEN` 两个 env 齐备才启用——无密码存储、无会话过期。同步引擎 `TodoSyncService`：全量分页拉取（跳过子任务）→ 按 `source_ref=pnkx:{id}` upsert 镜像、`external_updated_at` 变更才更新、远端删除→本地取消；本地完成→推送 `status=1`；Aria 手建任务经 `clientUuid=aria:{task_id}` 推送 pnkx，推送后崩溃未回填时按 clientUuid 从拉取快照认领，绝不重复创建。镜像 `next_fire_at` 恒为 NULL 不产生本地提醒（pnkx 自带 Quartz 提醒，避免双端重复打扰）；简报/回顾自动包含镜像任务。调度器默认 300s（`ARIA_TODO_SYNC_INTERVAL`），API `POST /api/v1/todo/sync` 手动触发返回统计。剩余：延期/优先级的反向推送（聊天端自然语言建任务已由 `pnkx_create_life` resource=todo 覆盖）。
- [ ] `MAIL-01` 邮件助手（v1 代码闭环，QQ 邮箱真机联调通过）：`integrations.mail` 配置（SMTP/IMAP 主机端口 + 授权码 secret_value/secret_ref 双模式，未配齐禁启用）；`app/mail` 客户端（smtplib/imaplib 经 asyncio.to_thread，SMTP 登录发送返回 Message-ID，IMAP INBOX 拉取/TEXT 关键词搜索 + MIME 解析摘要：发件人/主题/时间/正文前 200 字）。聊天工具 `mail_read`（只读摘要）与 `mail_send`（两段式：首次调用只返回收件人/主题/正文预览，模型复述、用户确认后 `confirmed=true` 才发送；同回合幂等绝不重复投递）；两者仅 L1 挂载（L2 私密会话禁止外发，执行层兜底拒绝），账号未配置时 available=False 不挂载。`scripts/mail_livecheck.py` 对真实 QQ 邮箱全链通过（SMTP 自发自收 + IMAP 首次搜索命中、摘要解析正确）。剩余：草稿暂存/附件、归类与未读管理、真实模型端的复述确认体验验收。
- [x] `CONTACT-01` 联系人上下文（代码闭环，待真实验收）：`contact` 表（`0033_contacts` 迁移）保存名称、别名、关系、IANA 时区、重要日期（月/日/可选年，按真实年历校验）与授权偏好；`ContactStore` 在用户内做名称/别名 casefold 唯一裁决（冲突拒绝不静默合并）、`find_by_name` 精确匹配、`contacts_with_date` 支撑简报事实。红线「不自动推断敏感关系属性」：relationship 与 preferences 只来自用户显式陈述，无任何后台提取路径；写入只有用户 API 与用户明确请求的 `contact_save` 工具（不传 contact_id 时按名称 upsert）。聊天工具 `contact_save`（同 turn 幂等）与 `contact_query`（返回关系、时区当地当前时间、重要日期倒计时与偏好）仅 L1 挂载、L2 执行层兜底拒绝；用户 API `/api/v1/contacts`（列表/搜索/CRUD）走聊天会话鉴权。BRIEF-01 每日简报新增 `contact_date` 事实（今日重要日期，带 `contact:{id}` 来源引用）。

#### J5 受控电脑操作代理

- [ ] `PC-01` 白名单桌面动作：打开应用/URL、聚焦窗口、文件移动/重命名、剪贴板和音量；删除、覆盖、发送需确认。
- [ ] `WEB-01` 浏览器工作流：读取、定位控件、填写不提交，展示字段和证据后才允许提交。
- [ ] `FLOW-01` 可复用流程：保存上班、会议、睡眠等步骤化流程，保存前展示全部动作与权限。
- [ ] `PC-02` 执行可视化：目标、步骤、进度、证据和立即停止；禁止无限制鼠标键盘权限。

#### J6～J8 后续产品化

- [ ] `MEET-01/FOCUS-01/COMMUTE-01/HOME-01`：会议、专注、出行和家庭情境助手。
- [ ] `IOS-01/AND-01`：PWA 稳定后评估 App Intents、快捷指令、锁屏/Live Activity、小组件和穿戴设备入口。
- [ ] `ID-01` 多人身份：设备身份为主、声纹辅助、访客降级、记忆和播报隔离。
- [ ] `SAFE-01/SAFE-02` 家庭守护：环境异常、分级提醒和预授权紧急联系人升级。

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
- [x] `capture_screen(active_window)` 原生真机验收：用户确认非 Aria 前台窗口真实聊天工具全链、临时资产销毁与视觉回答已完成。
- [x] 系统内容选择器代码闭环、隔离 E2E 与原生真机验收：`target=interactive` 复用 screen.capture 命令与 TCC/锁屏/临时授权门禁，交互框选、超时、Esc 取消、结构化错误码、资产上传、视觉回答与原图销毁均已验证。
- [x] 修复迁移 SQLite 兼容 bug：`0020/bb15882faef4/0022` 迁移中 `server_default=sa.text("now()")` 在 SQLite 上插入即崩（PG 正常；单测走 `create_all` 未暴露），统一改为方言感知的 `sa.func.now()`；真实 PostgreSQL 已应用的迁移不受影响，SQLite 从空库升级 + 启动种子写入已实测通过。
- [x] 浏览器扩展真机闭环：Chrome MV3 配对/签名长连接、`browser.current_tab.read/capture`、受限 JSON/图片临时上传与 Hub `inspect_webpage` 已接入；真 Chrome 已验证单一稳定连接、正文读取和当前页截图。
- [x] 隐私策略：L1 屏幕截图交给 GLM 视觉；L2 强制本地视觉，浏览器读取保持 L2。Desktop 锁屏自动拒绝、“5 分钟内仅下一次截图”的内存态临时授权及 macOS 锁屏/解锁已完成真机验收。
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

- [x] P5：连续 14 天真实文字使用已由用户确认完成；`docs/32` 尚需补入外部每日记录索引与期末闸门结果。
- [ ] M2：低延迟语音专用 LLM 端点评估按用户决定暂时忽略；ASR 已先用 VAD hangover 预取覆盖部分整段识别等待，下一步重置窗口完成 20 个完整回合 + 20 个打断判卷。
- [ ] 本地语音质量：faster-whisper 已支持可配置 `initial_prompt` / `hotwords`，并默认偏置“小艾 / Aria / 只回答”；剩余为真人样本复验，需要时安装并验收 Silero 与 openWakeWord。
- [x] P6 Batch D Web 最小壳：Live2D ZIP 安全导入、官方 Cubism Web Runtime 动态加载、聊天/Admin 真实模型渲染、viseme/说话/情绪/表情/动作控制与缺少运行时时安全降级均已接通。
- [ ] P6 Batch D Tauri 桌宠：透明置顶窗口、点击穿透、拖拽、Hub 同源舞台、`avatar.render` 签名临时事件、设备身份下的 `avatar.chat` 迷你输入与签名 TTS 音频、显示器工作区相对位置恢复及隐藏时销毁渲染 handle 已完成；待完成 macOS 热插拔、60fps、常驻内存 <300MB 与长时间运行验收。M2 首音频 P90 ≤1.8s、打断 P90 ≤300ms 保留为发布门槛。

### F. Admin 信息架构重整

- [x] 一级导航按“概览 / 伴侣核心 / 能力接入 / 运维治理”分组，合并记忆与时间线入口，并保留旧 `/timeline` 兼容跳转。
- [x] 二级 Tab 进入 URL query，可刷新恢复；已有总览、模型、Persona、记忆、时间线、删除台账、设备列表和命令台账复用真实页面。
- [x] 尚无查询接口的 Tab 使用明确的“待接入真实数据”状态页，不展示伪造指标；Admin typecheck 与 production build 通过。
- [x] 真实浏览器验收：登录后逐项检查一级模块、二级 Tab、旧 `/timeline` 跳转、刷新恢复和窄屏导航；发现的 P0/P1 交互问题当批修复。
- [x] 验收后按 `docs/05` 的实施顺序接真实数据：health/activity/usage/quality/conflicts/pairing/diagnostics + logs/privacy/system 三个聚合 dashboard（含实时日志独立 Tab）已全部接入真实数据；Trace、隐私审计、备份恢复和成本能力按 M3B 计划推进。

## 3. 最近完成

- [x] `CONTACT-01` 联系人上下文 v1（代码闭环）：`contact` 表 + `ContactStore`（名称/别名 casefold 唯一裁决、时区 ZoneInfo 校验、重要日期按年历校验、用户隔离）+ `/api/v1/contacts` 用户 API + `contact_save`/`contact_query` 聊天工具（仅 L1、同 turn 幂等、按名称 upsert、查询返回当地当前时间与重要日期倒计时）+ BRIEF-01 简报接入今日重要日期事实。仓库零推断：关系与偏好只能由用户显式陈述写入。
- [x] 修复工具执行层隐私兜底的存量死代码：`StrictModel` 的 `use_enum_values=True` 使 `ToolContext.privacy_level` 在运行时是普通字符串，`reminder_create`/`calendar_create` 里的 `is PrivacyLevel.X` 比较永不成立——L2 兜底拒绝实际失效（挂载门禁仍有效故无泄露）。统一改为 `==` 比较，并把既有 L2 测试改为传运行时真实的普通字符串（此前 `model_copy` 传枚举成员掩盖了问题）。
- [x] `XIAOAI` 小爱音箱网关 v1（代码闭环）：独立 `integrations/xiaoai_gateway` Node 容器用 `@mi-gpt/next` 拿小爱识别文本，仅前缀命中（默认"请阿莉娅"）的 Query 经 `stableEventId` 哈希去重后送 Hub，逐句播放 TTS 并按字数估算等待避免吞句；Hub 侧 `/ws/adapters/xiaoai` 以常量时间比对网关令牌鉴权，`xiaoai.hello` 绑定/复用 owner 会话（库恢复后自动重建），`xiaoai.query` 走 L1 语音路由流式分句 + Markdown 语音清洗回传，新查询抢占旧回合、取消透传 `cancel_turn`，`OrderedDict` LRU 按连接去重 event_id。配置中心 `integrations.xiaoai`（账号/密码/passToken/网关密钥全部 secret_value/secret_ref 双模式，启用时硬性校验账号、音箱、owner 与密钥齐全，TTS SIID/AIID 必须成对）；`XiaoAiConfigMaterializer` 启动与配置发布时把运行配置以 0600 原子写入 `xiaoai-state` 共享卷，网关循环等待热读取；Admin 配置 GET 脱敏三个 secret、PUT/草稿按掩码还原，HA 实体接口新增设备注册表元数据（device_id/名称/厂商/型号）支撑音箱下拉与型号推断。仓库零凭据。
- [x] 个人连接器 `MAIL-01` 邮件助手 v1：配置中心 `integrations.mail`（授权码双模式）+ `app/mail` 客户端（SMTP 发送/IMAP 收取，同步库全部 to_thread）+ `mail_read`/`mail_send` 聊天工具（两段式确认发送、同回合幂等、仅 L1、未配置不挂载）；`scripts/mail_livecheck.py` 对真实 QQ 邮箱联调一次通过。真实凭据只经环境变量进入运行时，仓库零凭据（提交前全仓 grep 复核）。
- [x] 多端音频与麦克风租约（PWA Batch C 收尾项）：`VoiceSession` 增加连接级稳定 `device_id` 作为租约持有者（不再每回合随机生成），统一"最新获取者抢占、旧持有者尽快停止"策略——新语音回合获取 audio_output 后消费抢占结果，向旧持有会话发 `voice.audio_preempted` 并打断其回合；话语采集（PTT 与 VAD 自动两条路径）开始时获取 microphone 租约，抢占方接管、被抢占会话收到 `voice.microphone_preempted` 且丢弃在途话语；speak 逐句续约，失持后剩余句子降级纯文字（delta 照常）；桌宠 `stream_device_speech` 持有 audio_output 逐块检查持有权，被语音回合抢占即发 `audio_preempted` 中止剩余音频；客户端新增两个抢占事件处理（停止本地采集/清空播放队列）。修复 TurnCoordinator.interrupt 按 generation 误当持有者释放租约的 no-op bug（新增 `release_for_generation`）；`recover_after_restart` 与过期租约清理接入 lifespan 启动。新增 `tests/test_voice_multi_device.py` 4 例：麦克风抢占停旧采集且释放后第三方可获取、音频抢占通知+打断旧回合、桌宠播报被抢占中止且不动他人租约、按 generation 释放。
- [x] PWA Batch C 移动通知底座（Web Push）：新增 `app/push` 包（订阅 Store 按 endpoint upsert/失效即删、`WebPushSender` VAPID 签名 + RFC8291 加密经 pywebpush 发送、`WebPushAdapter` 以 `web_push` 通道接入 `ProactiveDeliveryService`）；`0032_web_push` 迁移建 `push_subscription` 表并把回执 channel 约束扩展到 `web_push`；配置中心新增 `integrations.push`（VAPID 公钥明文 + 私钥 secret_value/secret_ref 双模式，未配齐密钥禁止启用）与 `proactive_output.web_push` 通道（默认优先级 70，L2 禁入由校验器硬拦）；用户 API `/api/v1/push/vapid-key|subscribe|unsubscribe`；Chat 输入区新增「🔔移动通知」开关（权限申请严格在用户手势内、状态恢复不触发询问），Service Worker 新增 `push` 展示与 `notificationclick` 点击回流（聚焦已打开窗口，前台后走既有补拉）；Admin 主动通道页新增 Web Push 通道卡。通知正文按事件 ID 打 tag 去重、截断 120 字，推送服务 404/410 自动清理订阅。
- [x] 聊天端自然语言建提醒/建日程工具：新增 `reminder_create`（`app/tasks/tools.py`，写 TASK-01 任务存储，支持 once/daily/weekdays/weekly/interval 周期与到家/离家事件触发，本地时间按 `ARIA_DEFAULT_TIMEZONE` 解释）与 `calendar_create`（`app/calendar/tools.py`，两段式契约：首次调用只做时间规范化 + 冲突预览并要求模型向用户复述，`confirmed=true` 才落库，时间冲突即使确认也服务端硬拦）。两者同回合按 turn 幂等（重复调用返回已建实体不重复写入）；挂载门禁仅 L1 开放（L0 公开模式不写个人数据，L2 私密会话内容不入库，工具执行层兜底拒绝），要求工具模型就绪；`tool.started` 标签与 main.py 按 service 就绪挂载已接通。TODO-01 的聊天建任务已由既有 `pnkx_create_life` resource=todo 覆盖，不另建通道。
- [x] 聊天 Markdown 渲染 + TTS 前文本清洗：Chat 气泡由纯文本改为 markdown-it 安全渲染（`html=false` 转义原始 HTML、链接强制 `noopener noreferrer`、流式期间保持 pre-wrap），新增 `MarkdownContent.vue`/`markdown.ts`；语音侧新增 `speech_text.py`（`MarkdownSpeechFilter` 跨句记住 fenced code 状态、`markdown_to_speech_text` 移除标题/链接/URL/行内代码/表格线/HTML 标签），接入流式分句、桌宠播报与完整语音回复三条路径，清洗后为空则安全跳过或返回 `tts_empty_text`。语音播报不再念出 Markdown 符号与代码块。
- [x] 情景记忆按事件追加（修复屏幕观察重复入库冲突）：`MemoryIngester` 对 EPISODIC 候选直接追加，不再进入稳定事实的"相似但不同即冲突"裁决——相似文本描述的是不同时间的事件可以同时为真；上游事件管线继续按事件 ID、截图哈希与时间窗口去重。`docs/38` 同步更新，新增两条相似屏幕观察各自独立创建的回归。
- [x] Admin 三处修复：设备命令台账与 HA 实体列表加分页（20/50/100(/200) 档位、筛选变化重置页码、HA 表格跨页保留勾选）；修复模型工作区保存时 `structuredClone` 无法克隆 Vue 响应式 Proxy 导致的崩溃——改为递归重建普通对象。
- [x] `TODO-01` 真机联调通过：livecheck 对 `https://admin.pnkx.top:8` 全链验收——分页拉取 109 条、镜像 109 条建立零错误（active 6 条含标题/优先级/分组正确）；本地新建经 `clientUuid=aria:{id}` 推送且远端绑定身份 `createBy` 正确；完成推送后远端 `status/finishTime` 落位；测试任务远端删除成功。livecheck 脚本同步增强为输出全量同步统计。生产 Hub 启用同步仍需部署侧配置 `ARIA_PNKX_BASE_URL/ARIA_PNKX_TOKEN`。
- [x] 个人连接器 `TODO-01` 任务单一真源（对接 pnkx）：集成令牌鉴权（pnkx 侧 IntegrationTokenFilter + X-Integration-Token，Aria 侧双 env 门控）；TodoSyncService 拉取镜像/删检测/推完成/推新建（clientUuid 幂等 + 崩溃认领）；TodoSyncScheduler 300s 循环 + `/api/v1/todo/sync` 手动触发。pnkx 仓同步提交过滤器与配置（编译通过）。
- [x] 个人连接器 `CAL-01` 日历：`calendar_event` 表 + `CalendarStore`（半开区间重叠冲突检查）+ `CalendarService`（preview 纯读展示写入内容与冲突、显式 CRUD、会前提醒经 `task_item.source_ref` 复用 TASK-01 调度并联动改期/取消）+ `/api/v1/calendar` 用户 API 与 main 接线。J4 第一项代码闭环。
- [x] 全屋语音 `SAT-01/SAT-02` 确定性核心：`app/satellite` 状态机 + 内存会话注册表 + 唤醒仲裁（同 owner 单会话、2s 窗口去重、多终端唯一响应），接入 `/ws/devices` 签名帧（hello/wake/state，`voice.satellite` 能力门禁，非法转移/越权上报返回结构化错误帧）。音频上下行与真机全链待硬件。
- [x] 个人管家 `REVIEW-01` 晚间回顾：`DailyReviewService` 四区块采集（完成/未完成/新承诺/明日重点，各带来源引用）+ 逐项修正（confirm/remove/note 只作用于回顾自身并重渲染正文）+ `(user_id, review_date)` 唯一约束每晚一次；`DailyReviewScheduler` 本地时区 21:30（`ARIA_REVIEW_TIME` 可调）经主动通道投递；`/api/v1/reviews` 查看与幂等预览。`CognitiveStore.create_goal/set_goal_status` 补可注入时钟，`goals_created_between/goals_completed_between` 支撑回顾查询。J2 四项（TASK/GOAL/BRIEF/REVIEW）代码闭环全部完成。
- [x] 个人管家 `BRIEF-01` 每日智能简报：`DailyBriefService` 确定性事实采集（天气/当日任务/到期承诺，各带来源引用）+ 模板拼装（无重要内容一行短句）+ `(user_id, brief_date)` 唯一约束每日一次；`DailyBriefScheduler` 本地时区时间门后为活跃用户投递；`/api/v1/briefs` 查看与幂等预览。main.py 完成天气闭包（按需构建高德运行时、任何失败静默降级）与调度器生命周期接线。
- [x] 个人管家 `GOAL-01` 承诺跟踪：目标提醒 pre_due/due 各一次的乐观认领、稍后/忽略降频闸门与反馈 API；`GoalTracker` 聊天后台承诺识别（消息证据 + 幂等 + 宁缺勿滥）；`GoalReminderScheduler` 复用主动通道投递。`ChatService` 新增 `goal_tracker` 参数与后台提取任务，`main.py` 完成 scheduler 生命周期与投递接线。
- [x] 个人管家 `TASK-01` 提醒与计划任务：`TaskStore` 支持一次性/周期（daily/weekdays/weekly/interval）时间触发与事件触发（精确匹配语义事件 + cooldown），稍后/完成/取消全状态守卫；`TaskScheduler` 15s 轮询到期认领并经 `ProactiveDeliveryService` 投递，重启恢复把中断 firing 判完成保证 exactly-once；PerceptionPipeline 新增事件观察口（processed/merged 才通知，异常不外溢）；`/api/v1/tasks` 用户 API 覆盖 CRUD 与稍后。发现并修复调度器启动即写库导致 SQLite :memory: 测试连接分裂的问题（首个 tick 延迟一个间隔）。
- [x] 个人管家 `ACT-04` 首批动作第二批：新增 `desktop.notification.show` 和本地 `DesktopNotifyTool`，Action Registry 当前共 10 个受控动作。通知标题/正文分别限制 80/500 字，只接受 L0/L1 和可选的明确桌面目标；Runner 将持久步骤幂等键交给设备命令网关，设备须具备 `notification.show`、在线并返回 `succeeded` 终态，才把最小 `command_id/device_id/status` 回执记为 `verified`。L2 在 Registry 参数层被拒绝，设备不可用或失败回执不会描述为成功。
- [x] 个人管家 `ACT-04` 首批动作第一批：Action Registry 从 5 个 HA 动作扩展到 9 个，新增 `home.light.set_brightness`、`home.media.play/pause/set_volume`。Home Assistant 配置白名单和工具参数新增 `set_brightness/play/pause/volume_set`，语义动作确定性映射到 `turn_on/media_play/media_pause/volume_set` 服务；亮度限制 1～100%，音量限制 0～1。Runner 分别核对 HA `brightness`、播放状态与 `volume_level`，不匹配继续按 ACT-03 记为 `unknown_outcome`。
- [x] 个人管家 `ACT-03` 结果回读与撤销：新增 `ActionRunResult` 和 `pending/not_required/verified/inconclusive` 验证状态，HA 灯光/开关按最终 `on/off`、空调按目标温度容差回读；工具成功但状态不匹配时步骤转 `unknown_outcome`，保留执行回执与最小回读证据并停止后续步骤。新增 `0025_action_verification` 保存验证证据、原计划/补偿计划和原步骤/补偿步骤关联；撤销只选择已完成且声明可逆的步骤，按逆序创建独立、需确认、不可递归的补偿计划。
- [x] 个人管家 `ACT-02` 计划—确认—执行闭环：`ActionPlanService` 支持 1～10 步计划、owner 隔离、请求哈希幂等、30～3600 秒 TTL、预授权集合、显式确认、执行前取消和状态恢复；`ToolActionRunner` 只把持久步骤中的固定 `tool_name/tool_arguments` 交给 `ToolExecutor`。执行采用逐步 claim，成功继续、失败停止、已完成步骤保留、剩余步骤标记 skipped；超时和协程取消将当前步骤记为 `unknown_outcome`，不重试未知副作用。新增 `POST/GET /api/v1/cognition/action-plans*` 鉴权接口和 `0024_action_plan` 单 head 迁移。
- [x] 个人管家 `ACT-01` 动作注册表：新增 `ActionRegistry`/`ActionDefinition`/`CompiledAction`，确定性校验风险与确认策略、动态参数 Schema、固定工具参数、验证策略和补偿引用；未注册动作、额外参数、Schema 漂移、重复 ID、缺失补偿及 A3 编译均被拒绝。首批 5 个 HA 动作映射到既有 `home_control`，灯/开关为 A1 预授权候选，空调设温为 A2 每次确认，A3 继续禁止。
- [x] PWA 会话游标与去重底座：聊天消息 REST 新增可选 `after_seq`，回到前台和语音回复刷新改为从本地最大 `seq` 增量补拉并按 500 条自动翻页；WebSocket 同步按 200 条分页返回 `has_more/next_after_seq`，客户端自动续拉，不再在大间隔恢复时静默截断。所有入口统一按消息 ID 去重、按 `seq` 排序并单调推进会话游标；新增 201 条消息跨页回归。
- [x] 手机 PWA 移动音频播放加固：在麦克风按钮、文字回复播报开关和已启用播报时的发送手势内，用静音 buffer 创建/恢复 `AudioContext`，满足 iOS/Android 对延迟 TTS 播放的用户手势要求；音频队列补充解码/播放错误回调，失败时清理忙碌态与口型并向用户给出重新授权提示。
- [x] 手机 PWA Batch A：复用 Vue Chat 主链而非新建分叉应用；增加 standalone manifest、180/192/512 图标与 maskable 图标、生产环境 Service Worker、不缓存 API/WS 的运行时策略和明确离线页。移动端使用顶部导航和左右抽屉，会话区保留整屏高度；安装模式令牌改用会话级存储。
- [x] 手机前后台恢复底座：监听 `visibilitychange` / `online` / `offline`；后台时立即停止录音与播放并释放语音连接，回到前台后 REST 补拉当前会话、重置重连次数并恢复 WebSocket `sync`；断网时关闭实时连接和发送入口，恢复后自动同步。登录页增加 Android 安装按钮和 iOS Safari 添加到主屏指引。
- [x] 桌宠文字回复播报：迷你输入新增可持久化“播报”开关，回复复用现有 TTS provider chain 和 `agent_reply.tts_text`；L2 仍只选择本地 TTS，无本地提供方时安全降级文字。音频不创建公开 URL，按 24 KiB 分块、8 MiB 总上限经 `/ws/devices` HMAC 签名帧投递，Desktop 主窗口验签后仅把音频帧转给桌宠 WebView；桌宠校验请求、顺序、分块数和总字节后用 AudioContext 解码 PCM/MP3，隐藏时立即中断并清空缓存。
- [x] 桌宠迷你文字输入：快捷菜单可展开输入框并选择 L1/L2，Enter 发送、Shift+Enter 换行；输入经 Tauri 事件交给持有系统凭据的主窗口，再通过已鉴权 `/ws/devices` 提交，桌宠 WebView 不接触设备令牌。Hub 独立校验 `avatar.chat` 授权、UUID/长度/隐私级别并异步运行完整 `ChatService`，回复进入最近活动会话；L2 只提示已保存到主聊天，不下发桌面文本气泡。accepted/completed/failed 均为签名帧，一台设备同时只运行一条桌宠消息。旧配对设备须在 Admin 明确授权新能力。
- [x] 桌宠第一批交互层：点击 Live2D 形象尝试播放 `TapBody:0`，静态/抽象形象提供轻量点击反馈；本地快捷菜单支持打开主控制台、开启鼠标穿透和隐藏桌宠。聊天与语音回复通过既有签名 `avatar.control` 帧携带压缩后的前 280 字，桌面气泡 12 秒自动关闭；L2 严禁发送文本气泡，只同步非文本情绪/口型。真实 Tauri 窗口已完成菜单视觉检查。
- [x] 桌宠多屏与资源保护代码：位置持久化从绝对坐标升级为“显示器名 + 工作区相对锚点”，分辨率/缩放变化按比例恢复，原显示器移除时吸附到最近可用工作区；主窗口与托盘每次显示都会触发可见区复核。桌宠隐藏时销毁 Live2D handle、作废在途挂载并启用 Tauri `backgroundThrottling=suspend`，恢复显示后重新加载当前形象，避免隐藏窗口继续占用渲染循环。纯函数多屏回归、Desktop 构建、`cargo check` 和完整原生 debug 构建通过；热插拔与资源数值仍待真机计量。
- [x] 桌宠正式联动通道：复用设备 `/ws/devices` 发送 HMAC 签名且不落命令台账的 `avatar.control` 临时帧；只有声明并获授 `avatar.render` 的同 owner 设备接收。文本回复控制块同步情绪/表情/动作，语音事件后台同步说话状态和 50ms 包络口型，不阻塞原语音发送路径；Desktop 验签、序号去旧后经 Tauri 事件转发给 Hub 同源舞台。Admin 已支持新旧设备授权该能力。
- [x] Tauri 透明桌宠代码底座：新增独立透明置顶窗口与 Vite 多页面入口，支持主窗口显隐/鼠标穿透控制、拖拽、物理坐标恢复和托盘强制恢复交互；形象舞台由 Hub 同源提供，复用当前 Live2D/静态/抽象形象且不放宽 `/api/v1/avatar-user-assets/` 安全白名单。M2 保留为发布验收门槛。
- [x] ASR/TTS 首响优化：自动 VAD 进入静音 hangover 的首帧即启动整段 ASR 预取，把约 450ms 断句等待与识别并行；恢复说话会立即作废预取，配置变化、预取失败和 L2 云端限制均安全回退完整转写。`voice.transcript.asr_prefetched`、延迟报告 `asr_prefetched_count` 与 Admin 标签可观测命中率。首回复无强标点时由可热配置的 `voice.first_tts_chunk_chars`（默认 24）提前切出首段，优先开始 TTS，后续仍按完整句/60 字切分。
- [x] 低延迟语音专用 LLM 路由：新增可选 `voice` route，L1 语音回合优先使用该路由，未配置时无缝继承 `dialogue`，L2 仍强制 `private`；文字聊天保持 `dialogue`。独立路由可在旧版 Admin 中选择，首 token 基准脚本会优先测试它；若语音端点不支持工具调用，该回合不挂载工具，避免整条路由因工具契约不兼容而失败。
- [x] M2 自动判卷加固：完成回合数与真实首音频样本数分开计数，必须同时具备 20 个完成回合、20 个非空首音频和 20 个打断样本才可能通过；新增 `overall_pass`，Admin 显示首音频有效样本与总判定，修复少量有效音频被 20 个无音频回合“凑够样本”的漏洞。
- [x] 本地 ASR 术语偏置：`VoiceAsrConfig` 新增 faster-whisper 专用 `initial_prompt` / `hotwords`，配置热更新会重建识别器；默认注入“小艾 / Aria / 只回答”上下文并直接透传模型原生参数，不使用可能误伤普通语句的全局文本替换。
- [x] ESP32 + LD2410 Hub 侧代码闭环：修复 `MqttDeviceClient` 未设置 `on_signal` 导致遥测只进缓存、不进 Perception 的断链；新增 `MqttPresenceBridge`，启动 retained 值只建基线，状态翻转经 5 秒稳定窗派生 L1 `presence.changed`，再进入认知与主动多终端投递；原始 MQTT 信号改为 L3。补充 ESPHome 固件样例、独立设备凭据与单 topic Mosquitto ACL、Compose 启用开关、重连存活修复和端到端回归。剩余为真机刷写与 7 天验收。
- [x] 屏幕事件专用聚合回顾：聊天识别“今天上午/过去 N 小时在电脑上做了什么”等意图，按时间窗只检索 `DEVICE/screen.observed`，合并连续相似观察并注入受控证据上下文；无记录时禁止用长期记忆猜测，审计记录 `recall.mode=screen_activity`。
- [x] 屏幕感知主动链路修复与真机全链验证：注意力显著性、重复惩罚、会议提醒审议和主动降频均按设计工作；用户后续确认 Desktop 睡眠/唤醒连接可靠性收口已完成。
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

- 2026-09-04 `CONTACT-01` 联系人上下文：新增 `tests/test_contacts.py` **14 通过**（创建规范化/别名去重/按别名 casefold 查找、名称与别名冲突拒绝（含大小写）、非法时区与 2/30 等非法日期拒绝、更新替换与空串清时区/自我更新不误判冲突、删除释放名称与用户隔离、next_occurrence 含 2/29 跨闰年、contacts_with_date 月日过滤、工具建后同 turn 幂等、按名称 upsert、L2/缺 turn/非法参数拒绝、查询返回当地时间与倒计时、简报 contact_date 事实与正文分区、API 401/201/422/搜索/改/删全链）；非 soak 全量 pytest **690 通过 / 0 失败**（1 个 soak 用例 deselect）；Ruff、严格 mypy（292 source files）、`git diff --check` 通过；Alembic SQLite 空库升级到单 head `0033_contacts`。同批修复 `reminder_create`/`calendar_create` 执行层 `is PrivacyLevel` 死代码（改 `==`，测试改传普通字符串复现运行时路径），`tests/test_assistant_tools.py` 12 例复验通过。真实模型端的联系人保存/查询体验待真机验收。
- 2026-09-04 `XIAOAI` 小爱音箱网关：新增 `tests/test_xiaoai_config.py` **4 通过**（配置物化含密钥/权限 0600、启用硬校验、passToken 登录路径、TTS SIID/AIID 成对）与 `tests/test_xiaoai_websocket.py` **3 通过**（鉴权+分句流式+event_id 去重全链、错误令牌 4401、L2 拒绝），`test_home_assistant.py` 新增设备注册表元数据解析 1 例；非 soak 全量 pytest **677 通过 / 0 失败**（1 个 soak 用例 deselect）；Ruff、严格 mypy（286 source files，含修复 `170f3ad` 遗留的 mail 收据 walrus 类型回归）、Shared/Chat/Admin typecheck 与 production build、网关 `tsc` 构建与产物语法检查、`git diff --check` 全部通过；无新迁移。真实小米账号登录（验证码/passToken）与音箱真机端到端待验收。
- 2026-09-04 `MAIL-01` 邮件助手 v1：新增 `tests/test_mail.py` **11 通过**（配置缺账号禁启用、secret 双模式解析、工具随配置切换 available；发送构造 MIME 头/认证失败映射；两段式预览不发送、确认后发送且同回合幂等、非法地址与 L2 拒绝；读取摘要结构与 L2 拒绝；地址模式；示例 yaml 加载）；非 soak 全量 pytest **668 通过 / 0 失败**；Ruff、严格 mypy（282 source files）与 `git diff --check` 通过。真实 QQ 邮箱 livecheck 全链通过（SMTP message_id 回执 + IMAP 搜索命中）。
- 2026-09-04 多端音频与麦克风租约：新增 `tests/test_voice_multi_device.py` **4 通过**（麦克风抢占停旧采集并通知、释放后第三方无旧持有者、音频抢占通知+打断旧回合、桌宠播报被抢占中止且不动他人租约、按 generation 释放往返）；既有 `test_voice_websocket.py` + `test_runtime.py` **39 通过**确认无回归；非 soak 全量 pytest **657 通过 / 0 失败**（1 个 soak 用例 deselect）；Ruff、严格 mypy（278 source files）、Chat typecheck 与 production build、`git diff --check` 全部通过。双端同时语音/桌宠并发播报的真机行为待验收。
- 2026-09-03 Web Push 通知底座：新增 `tests/test_push.py` **14 通过**（endpoint upsert 重绑、owner 级退订、失败计数重置；VAPID 配置缺钥禁启用、L2 通道禁入、secret_value/env 双模式解析；发送器 201/410 状态映射、生成脚本私钥 d 值格式契约；适配器未配置/无订阅/投递成功清失效订阅/全失败带原因/长正文截断；API 401/422/201/204 全链）；非 soak 全量 pytest **653 通过 / 0 失败**（1 个 soak 用例 deselect）；Ruff、严格 mypy（277 source files）、Chat/Admin typecheck 与 production build、SW 语法、`git diff --check` 全部通过；Alembic SQLite 空库升级到单 head `0032_web_push`。VAPID 密钥生成（`server/scripts/generate_vapid_keys.py`）与真实推送服务（含 iOS Safari 已安装 PWA）真机验收待做。
- 2026-09-03 聊天端建提醒/建日程工具：新增 `tests/test_assistant_tools.py` **12 通过**（本地 naive 时间转时区、周期与事件触发映射、同回合幂等、过期触发拒绝、L2/缺 turn 拒绝；日历两段式预览不落库、确认后建事件并关联提醒任务、冲突即使确认也硬拦、倒置窗口拒绝、幂等；L1 挂载/L0/L2 不挂载、无工具能力模型不挂载）；非 soak 全量 pytest **639 通过 / 0 失败**（1 个 soak 用例 deselect）；Ruff、严格 mypy（271 source files）与 `git diff --check` 通过。真实模型端的自然语言解析效果（模型是否先追问/复述再调用）待真机验收。
- 2026-09-03 Markdown 渲染/TTS 清洗/情景记忆/Admin 修复批次收口：新增 `test_voice.py` Markdown 清洗 3 例、`test_memory.py` 情景事件独立追加 1 例；非 soak 全量 pytest **627 通过 / 0 失败**（1 个 soak 用例 deselect）；Ruff、严格 mypy（268 source files）、Chat/Admin typecheck 与 production build、`git diff --check` 全部通过；分 5 个逻辑提交入库。同日 `TODO-01` livecheck 对真实 pnkx 实例全链 4 阶段零错误（拉取 109/镜像/新建推送/完成推送/清理）。语音真机播报效果仍待验收。
- 2026-09-02 `TODO-01` pnkx 任务对接：新增 `tests/test_todo_sync.py` **10 通过**（令牌头校验与 401 拒绝、创建/更新载荷、镜像建立与子任务跳过、二次同步稳定与远端删除检测、完成推送且不重复、手建推送一次与崩溃认领、拉取失败不产生半写、调度器首 tick 延迟、API 401/统计）；非 soak 全量 pytest **602 通过 / 0 失败**；Ruff、严格 mypy（app 194 files）与 `git diff --check` 通过；Alembic SQLite 空库升级到单 head `0031_todo_sync`；pnkx-framework `mvn compile` 通过。真实 pnkx 实例联调（配置集成令牌后）待验收。
- 2026-09-02 `CAL-01` 日历：新增 `tests/test_calendar.py` **8 通过**（预览冲突且不落库、首尾相接非冲突、窗口/提前量校验、创建关联提醒任务、取消联动撤提醒、改期撤旧建新、临近事件立即提醒、API 全流含 401/404/422 与冲突预览）；非 soak 全量 pytest **592 通过 / 0 失败**；Ruff、严格 mypy（app 189 files）与 `git diff --check` 通过；Alembic SQLite 空库升级到单 head `0030_calendar`。真实 PostgreSQL 尚未应用 `0024～0030`；聊天端自然语言建日程与外部日历接入待做。

- 2026-09-02 `SAT-01/SAT-02` 卫星确定性核心：新增 `tests/test_satellite.py` **13 通过**（合法生命周期、cancel/error 任意态回 idle、非法转移拒绝、首唤醒胜出/窗口内压制/窗口后放行、注销与未注册拒绝、重连重置保留计数、网关 hello/wake/state 全流、双卫星唯一响应、能力门禁、设备不能自行进入 listening、非法跳转与未注册错误帧、idle 重申幂等、断开注销）；非 soak 全量 pytest **584 通过 / 0 失败**；Ruff、严格 mypy（app 184 files）与 `git diff --check` 通过；无新迁移（内存态注册表），Alembic 保持单 head `0029_daily_review`。唤醒词/VAD/音频上下行待真机硬件验证。

- 2026-09-01 `REVIEW-01` 晚间回顾（J2 收口）：新增 `tests/test_review.py` **7 通过**（四区块采集、空/移除/附注拼装、幂等不覆盖修正、修正不改任务真源状态、每晚投递一次、时间门、API 含 401/404/422）；非 soak 全量 pytest **571 通过 / 0 失败**；Ruff、严格 mypy（app 181 files）与 `git diff --check` 通过；Alembic SQLite 空库升级到单 head `0029_daily_review`。真实 PostgreSQL 尚未应用 `0024～0029`；真实多通道投递待验收。

- 2026-09-01 `BRIEF-01` 每日智能简报：新增 `tests/test_brief.py` **11 通过**（任务/目标/天气事实采集与来源、天气失败降级、空内容短句、分区拼装、过期标注、按日幂等、投递一次与通道记录、投递失败不重复、时间门与 run_once、API 含 401）；非 soak 全量 pytest **564 通过 / 0 失败**；Ruff、严格 mypy（app 178 files）与 `git diff --check` 通过；Alembic SQLite 空库升级到单 head `0028_daily_brief`。真实 PostgreSQL 尚未应用 `0024～0028`；真实高德天气与多通道投递待验收。
- 2026-09-01 `GOAL-01` 承诺跟踪：新增 `tests/test_goal_tracking.py` **11 通过**（pre_due/due 各一次、窗口未达不发、推迟闸门、忽略降频计数、完成不打扰、按证据去重、低置信/坏输出/无后端/L3 不建目标、幂等、调度器投递与 trigger_kind、反馈 API 含 401/404）；非 soak 全量 pytest **553 通过 / 0 失败**；Ruff、严格 mypy（app 175 files）与 `git diff --check` 通过；Alembic SQLite 空库升级到单 head `0027_goal_reminders`。真实 PostgreSQL 尚未应用 `0024～0027`；真实 utility 模型承诺识别效果与多通道投递待验收。
- 2026-09-01 `TASK-01` 提醒与计划任务：新增 `tests/test_tasks.py` **21 通过**（触发纯函数、创建校验、状态守卫、exactly-once、周期跳过错过、事件 cooldown、重启恢复、调度器投递/异常不重触发、API 全链与非鉴权拒绝）；非 soak 全量 pytest **542 通过 / 0 失败**；Ruff、严格 mypy（app 173 files + 新测试）与 `git diff --check` 通过；Alembic SQLite 空库升级到单 head `0026_task_reminder`。真实 PostgreSQL 尚未应用 `0024/0025/0026`；多通道真实投递与聊天端自然语言建提醒待验收。
- 2026-09-01 `ACT-04` L0/L1 桌面通知：Action Plan/Registry/Cognition/HA/Config/Device Command/Output/API/Schema 扩展回归 **91 通过**；Ruff、严格 mypy 和 `git diff --check` 通过，Alembic 保持单 head `0025_action_verification`。验收覆盖 L2 参数拒绝、步骤幂等键透传、设备能力选择、成功终态回执和验证证据持久化；真实桌面系统通知仍需联机验收。
- 2026-09-01 `ACT-04` HA 动作扩展第一批：Registry/Action Plan/Home Assistant 定向测试 **35 通过**，扩展 Cognition/Config/API/Schema 回归 **69 通过**；覆盖参数越界、领域动作白名单、语义服务映射、服务参数、音量确认以及亮度/播放/音量真实 Runner 回读。Ruff、严格 mypy、Admin typecheck/production build 和 `git diff --check` 通过，Alembic 仍为单 head `0025_action_verification`；尚未做真实 HA 设备场景验收。Admin 构建仅有既有的 VueUse pure annotation 与大 chunk 警告。
- 2026-09-01 `ACT-03` 回读与撤销：新增验证成功、状态不一致、真实 ToolActionRunner 回读、HA 服务响应刷新缓存、逆序补偿和撤销 API 验收；Action Plan/Registry/HA 定向测试 **30 通过**，扩展 Cognition/API/Schema 回归 **56 通过**，Ruff、严格 mypy和 `git diff --check` 通过；Alembic 已验证 SQLite 从 `0024` 升级到单 head `0025_action_verification`，并用既有计划/步骤样本确认数据保留，历史未验证写步骤回填为 `inconclusive`。真实 PostgreSQL 尚未应用 `0024/0025`。
- 2026-09-01 `ACT-02` 行动计划闭环：Action Plan/Registry、Cognition、HA、API 与 Schema 相关测试 **51 通过**；Ruff、严格 mypy、`git diff --check` 通过；Alembic 从 SQLite 空库升级到单 head `0024_action_plan` 成功。真实 PostgreSQL 尚未应用 `0024`，不得把空库迁移测试写成线上升级完成。
- 2026-09-01 `ACT-01` 动作注册表：Action Registry、Cognition 和 Home Assistant 定向测试 **33 通过**；Ruff、严格 mypy 和 `git diff --check` 通过。目录 API 额外覆盖未鉴权拒绝与登录用户读取；本批没有开放新的现实写权限。
- 2026-09-01 PWA 游标补拉与去重底座：完整 `test_chat.py + test_chat_websocket.py` **27 通过**；定向 Ruff、mypy、Shared/Chat/Admin TypeScript、全 Web production build 和 `git diff --check` 通过。Admin 构建仍只有既有的 VueUse pure annotation 与大 chunk 警告，无新增失败。
- 2026-08-31 手机 PWA 移动音频加固：Chat `vue-tsc --noEmit` 与 production build 通过，`git diff --check` 通过；真实移动浏览器的 AudioContext 解锁、TTS 播放和前后台切换仍需纳入 Batch B 真机验收，不把桌面浏览器构建结果当作真机通过。
- 2026-08-29 手机 PWA Batch A：Chat `vue-tsc --noEmit` 与 production build 通过，Service Worker 语法检查和 `git diff --check` 通过；生产包已输出 manifest、offline shell、180/192/512 图标与 maskable 图标。Codex 内置浏览器以 390×844 视口完成登录页 DOM 与视觉验收；鉴权后聊天与 iOS/Android 安装仍待真机。
- 2026-08-27 全量质量闸门已恢复：在制批次分 9 个逻辑提交入库后，Ruff（含 `allowed-confusables` 白名单全角标点）、严格 mypy（214 source files）、Admin/Chat/Shared typecheck 与 production build 全部通过；非 soak 全量 pytest **532 通过 / 0 失败**（新增 conftest autouse fixture 修复 3 个既有全局 Admin token 状态污染失败；TurnCoordinator `create_turn` 已对齐真实 `ChatService.start_turn` 契约）；Alembic 空库升级到单 head `0023_device_alias_reuse`、`git diff --check` 通过。
- 2026-08-27 屏幕感知上线运行验证：配置 v63 开启（60s×三屏）、设备声明+授权 `screen.monitor`、修复上传白名单后（`8d80ebb`）三屏截图与 GLM 视觉分析实测成功（微信/文件系统/编程界面三份摘要入 Timeline），循环无错误；感知哈希变化检测、即焚与降频待长期观察。
- 2026-08-27 设备别名释放修复：Ruff、严格 mypy、非 soak 全量 pytest 通过（含新增撤销重配回归）；`0023_device_alias_reuse` 已在 SQLite 空库与真实 PostgreSQL 双端验证，真实库现处 head `0023`。同批 Admin 交互修复：设备设置保存遇 revision 冲突自动刷新版本号并提示重试（`a9ab560`）、设备注册表行级配对码按钮（`249eae7`）、Desktop debug bundle 过期问题（`ef0420e`，`bundle.active` 已启用）。
- 2026-08-27 选择器隔离 E2E 与迁移修复：迁移 `now()` 默认值修复后 Ruff、严格 mypy、Avatar/Theme/Jobs 定向 pytest 通过；隔离环境（8001 + SQLite + 复制的真实模型配置 + 虚拟桌面设备）两条 E2E 全部闭环——interactive 命令线上 TTL 115s、幂等键含 target、PNG 资产上传后 GLM 视觉准确描述、picker_cancelled 透传后模型优雅重试。已知观察：弱祈使句下模型可能只叙述不调用工具。
- 2026-08-27 系统内容选择器代码闭环：Ruff、严格 mypy、非 soak 全量 pytest **532 通过 / 0 失败**（含 4 个新增 interactive 回归）、Desktop vitest **9 通过**、Desktop typecheck/build、`cargo check` 与 `git diff --check` 全部通过；2026-08-28 用户确认真机验收完成。
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

- M2 未达标前不发布 Live2D/桌宠；代码开发与真机准备可继续；
- ~~持续后台屏幕监控~~：已由「屏幕感知 v1」取代（2026-08-27 用户显式决策，纯配置开关 + 设备端 TCC/锁屏/隐私暂停三闸门，见 `docs/38`）；
- 摄像头和高风险健康推断；
- 完整形象中心、多形象、主题增强、VRM 和静态图动态化；
- Open-LLM-VTuber 深度 fork；
- 多传感器扩展，先完成单存在传感器闭环。
