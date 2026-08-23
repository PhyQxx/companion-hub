# Aria 当前任务

> 最后更新：2026-08-23
> 详细设计入口：[docs/00-文档索引与架构总览.md](./docs/00-文档索引与架构总览.md)

本文件只维护当前执行队列、未完成门槛和最新质量基线。历史交付细节留在对应阶段文档，不在这里重复。

## 1. 阶段总览

| 阶段 | 状态 | 当前结论 |
|---|---|---|
| M0～M1 可信文字核心 | 已完成 | 事件、隐私、身份、聊天、Persona、Memory、Timeline 与删除闭环已落地 |
| P5 文字稳定性闸门 | 使用期未开始 | 自动化通过；14 天从首条有效日志重新起算，见 `docs/32` |
| P6 Batch A～C 语音 | 主链完成 | 真浏览器 ASR/LLM/TTS/viseme/打断已打通；延迟继续优化 |
| M3A 地图/天气第一批 | 已完成 | 查询、定位、卡片、Admin 自检、200 条台账与延迟报告已落地，见 `docs/35` |
| M3A 多终端与感知 | 进行中 | Device Registry、Command Channel、Admin 设备页与 Desktop 安全连接壳已落地；下一步实现受控屏幕读取 |
| P6 Batch D Live2D/桌宠 | 等待前置 | M2 延迟达标后再启动 |

## 2. 当前执行队列

### A. 多终端设备底座

- [x] Device Registry 后端：一次性配对码、每设备独立凭据、命名/别名、所有权、撤销、在线状态、心跳与 capability 授权白名单。
- [x] Device Command Channel 后端：客户端主动连接 `/ws/devices`，支持 HMAC 签名命令、TTL、设备级幂等键、取消、ACK、结果回执与超时状态。
- [x] Capability Registry 后端：终端心跳声明 `screen.capture`、`browser.inspect`、`sensor.read` 等能力；模型只看到在线声明与管理员授权的交集。
- [x] 目标设备解析：UUID/别名/名称精确匹配；通用“我的电脑”仅在唯一在线且有能力时自动选择，歧义时返回候选要求确认，离线时不改选。
- [x] Admin 设备页：一次性配对、在线状态、能力授权、测试命令、最近命令台账与一键撤销。
- [ ] 局域网/VPN 安全接入：每设备独立凭据，不把 Hub 或客户端裸露到公网。

### B. 电脑屏幕与网页理解

- [x] Tauri Desktop Client 安全连接壳：开机启动、托盘、系统凭据库、配对、签名长连接、心跳/重连和 `device.ping`。
- [x] 截图临时资产通道：设备鉴权上传、命令/owner 绑定、PNG/JPEG 魔数与 8 MiB 上限、2 分钟 TTL、读取即销毁，数据库仅保留摘要。
- [ ] macOS 屏幕录制授权、隐私暂停与临时截图销毁闭环：实现已落地，待 Rust/Xcode 环境补跑原生编译与真机 TCC 验收后勾选。
- [x] `capture_screen` 主显示器闭环：目标解析、签名命令、终态等待、临时图片 consume-on-read、本地视觉分析和文字结果回注；仅在 L2 + 本地工具模型 + 本地视觉可用时暴露。
- [ ] `capture_screen` 目标扩展：活动窗口、指定显示器与系统内容选择器。
- [ ] 浏览器扩展：提供 `browser.current_tab.capture` 与 `browser.current_tab.read`，优先返回页面结构化文本和当前标签页截图。
- [ ] 隐私策略：默认 L2、本地视觉优先；云视觉必须显式临时授权；锁屏、隐私暂停或客户端离线时拒绝。
- [ ] 跨终端验收：从手机或 Web 对话发起“看一下我的电脑网页”，电脑客户端执行，结果返回原会话。

### C. 传感器与主动感知

- [ ] MQTT 设备接入：每设备凭据、topic ACL、schema/value/rate 校验和在线状态。
- [ ] 第一硬件闭环：ESP32 + LD2410 存在雷达。
- [ ] 原始遥测进入 `EphemeralSignal`，按通道去抖并设置过期时间；L3 原始值不落库、不进日志、不进模型。
- [ ] `read_sensors` 工具读取最新有效状态，支持“现在有人吗”“室温多少”等被动查询。
- [ ] Perception 规则把稳定状态转换为 `presence.changed`、`user_arrived_home` 等语义事件。
- [ ] ProactiveEngine 执行 DND、冷却、每日上限、忽略降频和输出终端仲裁。
- [ ] 完成单存在传感器 7 天验收：免打扰零违规，重复/误触发可解释。

### D. 并行门槛与优化

- [ ] P5：从首条可核验每日日志开始连续 14 天真实文字使用；期满复跑闸门并定稿 `docs/32`。
- [ ] M2：评估流式 ASR 与低延迟语音专用 LLM，达到可行下限后重置窗口完成 20 个完整回合 + 20 个打断判卷。
- [ ] 本地语音质量：校准“小艾/只回答”等音近词；需要时安装并验收 Silero 与 openWakeWord。
- [ ] P6 Batch D：仅在 M2 首音频 P90 ≤1.8s、打断 P90 ≤300ms 后进入 OLV Live2D 最小壳与 Tauri 桌宠。

## 3. 最近完成

- [x] Hub Screen Capture Tool：命令终态事件唤醒、设备歧义候选、本地 OpenAI-compatible 视觉 data URL、原图单次消费及 ChatService 三重可用性门控已接入。
- [x] Desktop `screen.capture` 实现：仅在 macOS TCC 已授权且隐私暂停关闭时声明能力，单次截取主显示器、鉴权上传，RAII 清理本机临时文件；原生验收仍待工具链。
- [x] Ephemeral Device Asset Store：截图不进入命令 JSON 或数据库，上传内容只在有界进程内存中短暂存在并 consume-on-read。
- [x] Device Target Resolver：按 owner 隔离，支持精确目标、通用桌面目标、capability/在线复核、歧义候选与禁止静默 fallback。
- [x] Desktop Client 安全连接壳：Tauri 2、OS keyring、配对、托盘/开机启动、签名验签、心跳/重连、TTL/取消/幂等和 `device.ping` 已接入；原生真机验收等待本机 Rust/Xcode 工具链。
- [x] Admin 设备工作区：设备统计/筛选、配对码、能力交集、revision 冲突保护、测试命令、命令状态与撤销交互已接入 Vue 后台。
- [x] Device Command Channel 第一批：`0013_device_command`、鉴权长连接、HMAC-SHA256 命令签名、脱敏命令台账、离线失败、TTL/超时、幂等冲突、取消与 ACK/结果回执已接入。
- [x] Device Registry 第一批：`0012_device_registry`、一次性配对、凭据哈希、心跳、撤销、乐观 revision 与授权能力交集已接入；在线有效能力已进入聊天现实能力边界。
- [x] 地图/天气工具：真实高德天气、附近 POI、路线、浏览器临时定位、模糊候选、TTL 缓存和结构化卡片。
- [x] 地图运维：独立自检、`ToolLedger(maxlen=200)`、成功率/P50/P90/缓存命中率与失败聚合、Admin 展示。
- [x] 语音 Batch A～C：本地 faster-whisper、MiMo/edge TTS、流式分句、viseme、打断、延迟滑窗与真浏览器全链。
- [x] M2 诊断：打断 82ms 达标；当前组合理论首音频下限约 3.3s，1.8s 指标需要架构优化而非继续机械计样。
- [x] P5 可开发项：多主体记忆、Timeline、L2 隔离、删除闭环、前后端闸门和真实模型回归。

## 4. 最新质量基线

- 2026-08-23 当前工作树：pytest **275 通过 / 2 跳过**，Desktop 协议测试 **4 通过**，Ruff、全量 mypy、Alembic 单 head、`git diff --check`、Chat/Admin/Shared/Desktop typecheck 与 production build 全部通过；
- CI 的 mypy 范围已与本地发布闸门对齐为 `server/app server/tests`；
- 运行配置：v39；本地 ASR 为 faster-whisper `base/cpu/int8`；
- PostgreSQL/pgvector 两项集成测试在本地无 `ARIA_TEST_DATABASE_URL` 时跳过，推送后由 CI PostgreSQL 服务执行。

## 5. 暂缓

- M2 未达标前的 Live2D/桌宠产品化；
- 持续后台屏幕监控、摄像头和高风险健康推断；
- 完整形象中心、多形象、主题增强、VRM 和静态图动态化；
- Open-LLM-VTuber 深度 fork；
- 多传感器扩展，先完成单存在传感器闭环。
