# Aria 开发任务清单

> 最后更新：2026-08-21
> 当前阶段：P6 · 语音与 Live2D 产品化（Batch A～C 代码与自动化已落地，当前进入 M2 真浏览器/真机验收；P5 14 天使用日志尚未开始有效记录）

## 进度概览

| 阶段 | 状态 | 目标 |
|---|---|---|
| P0 Open-LLM-VTuber 集成验证 | 已完成 | 通过自定义 Agent 接入 Aria 实时聊天 |
| P1 结构化回复与 Persona | 已完成 | 统一文本、情绪、语音和动作输出 |
| P2 记忆系统 v1 | 已完成 | 可检索、可溯源、可纠错的长期记忆 |
| P3 删除闭环与记忆后台 | 已完成 | 跨存储删除与可视化管理 |
| P4 正式前端决策 | 已完成 | 决定 Vue 3 与 Open-LLM-VTuber 的边界 |
| P5 文字稳定性闸门 | 使用期 | 自动化闸门全绿；日志未开始，14 天计数待重启（docs/32） |
| P6 语音与 Live2D 产品化 | 进行中 | 完整语音、打断、表情与桌宠体验 |

## 上一批次：P2（已完成）

- [x] 迁移 0007：`memory` + `memory_source`（来源引用 + 摘录哈希，不复制原文）。
- [x] 混合检索：哈希嵌入 + 词汇双路召回、硬过滤、归一化重排、类型配额 Top-K=8。
- [x] 溯源：每条记忆记录来源消息与提取器版本；编辑产生替代版本并保留版本链。
- [x] 沉淀判定：新增 / 支持（重要性提升）/ 冲突（挂起等待裁决）三态；替代仅由显式操作触发。
- [x] 聊天集成：检索注入系统提示、`decision_meta.memory` 记录命中 ID 与策略版本。
- [x] 隐私闸门：L3 无法入库；L2 记忆仅进入 L2（本地路由）上下文；L2/L3 会话不自动沉淀。
- [x] 管理 API：查询、手动添加、编辑、归档、冲突裁决（adopt/keep）与检索调试。
- [x] 记忆子系统故障不影响回合提交；回归测试 12 项含端到端"记住→检索→注入"闭环。

### P2 验收条件

- [x] 记忆命中进入上下文且可追溯到使用的记忆 ID 与策略版本。
- [x] 检索隔离用户、状态、有效期与隐私等级。
- [x] 稳定事实不被模型候选直接覆盖，冲突必须显式裁决。
- [x] 纠错保留历史版本与来源，可回溯旧内容。
- [x] 全部闸门通过：pytest 99 通过、ruff、mypy、迁移升降级验证。

## 上一批次：P3（已完成）

- [x] 删除台账（deletion_ledger）与硬删除：整条版本链 + 来源 + 衍生引用，单事务完成（迁移 0008）。
- [x] `/admin/memory` 可视化后台：统计、过滤列表、溯源详情、手动添加、纠错编辑、冲突裁决、检索调试与台账展示。
- [x] 删除 API：`DELETE /api/v1/admin/memories/{id}` 与 `GET /api/v1/admin/deletion-ledger`。
- [x] L2 脱敏 utility 提取器接入（LLM 结构化提取，utility 路由 + json_mode，坏输出回退规则提取；`ARIA_MEMORY_EXTRACTOR=llm` 启用）。
- [x] 嵌入升级评估：pgvector 向量列双写 + ANN 召回 + 迁移回填（迁移 0009，真实 pgvector 容器验证；真实嵌入模型切换路径见 docs/25）。
- [x] 消息删除级联与备份恢复后的台账重放工具（会话 DELETE API + UI 删除按钮 + `replay_deletions` 脚本与管理端点，dry-run 幂等）。

## 上一批次：P4（已完成）

- [x] 前端技术决策：ADR-018 —— Vue 3 自建 chat/admin 正式前端，Open-LLM-VTuber 仅作 P6 渲染/语音端（详见 docs/26）。
- [x] P4a：`web/` monorepo 脚手架（Vite + TS + pnpm workspace）+ chat MVP（登录、会话、流式、取消、删除、隐私等级），FastAPI 托管构建产物，调试页保留为 `/chat/debug`（详见 docs/27）。
- [x] P4b：admin 迁移（总览、模型路由、Persona、记忆库、占位页）到 `web/apps/admin`；FastAPI 双模式托管；迁移 BIGINT 主键 sqlite 变体修复；浏览器实测（详见 docs/28）。
- [x] Dockerfile pnpm build 阶段；`packages/shared` 抽取 API 客户端与类型。
- [x] 注释与文案规范确立：界面文案与代码注释统一中文，memory 包与前端核心文件已补齐。

## 并行使用期：P5（14 天真实文字使用进行中）

- [x] P5 前置联调收口：管理后台统一 Vue 3 + Element Plus 浅色主题；模型配置、路由与日志设置重新分层；聊天页补当前 Persona 元信息展示。
- [x] 模型配置交互改为“保存即生效”：后台不再暴露草稿/发布/版本历史；`PUT /api/v1/admin/config/current` 完成校验、持久化与切换。
- [x] 模型配置页新增“测试连接”：本地 LM Studio 通过 `GET /api/v1/models` 检查服务、模型存在与加载状态，云端/OpenAI-compatible 继续使用最小 provider probe；测试不触发模型加载或下载。当前本机 `127.0.0.1:1234` 已实测可达并识别到已加载模型。
- [x] 智谱免费模型按能力更新并接入当前运行配置：文本 `glm-4.7-flash` 进入 dialogue/utility 最末级 fallback；视觉 `glm-4.6v-flash`、生图 `cogview-3-flash`、视频 `cogvideox-flash` 进入独立 `capability_models`。新增模型 `kind`、文本 `thinking_mode`、`CapabilityModelService` 以及视觉/生图/视频认证 API；L2/L3 仍由 EgressGuard 阻断云端能力。GLM 文本真实探针已到达智谱服务，当前响应为免费模型访问量过大的 RateLimitError，非本地配置解析错误。
- [x] Persona 跨 worker 运行时同步修复：新聊天回合开始前刷新数据库当前 Persona，避免管理端已发布 v2、聊天 worker 仍持有 v1 内存快照。
- [x] 新增运行时元信息接口 `GET /api/v1/meta/runtime`，聊天前端通过现有 `/api` 代理读取当前 Persona 名称与版本。
- [x] 现实能力边界：聊天转移话题或提出行动建议时，只允许引用运行时明确上报的在线/授权设备能力；空能力列表时禁止虚构训练场、后院、做饭等现实行动。
- [x] 文档体系收口：重写根目录 README；新增 docs/00 文档地图；区分 Active / ADR / Phase Record / Current Record；补齐 docs/31 Timeline/History Recall 上位设计并清理关键重复定义。
- [x] 多主体持久化记忆完成详细设计：明确 user / assistant / shared 主体、`fact_key` 稳定槽位、完整回合提取、防回声、自述冲突、一致性检查与现实能力边界（详见 docs/30）。
- [x] 实现多主体持久化记忆：Batch A～D 后端已完成并通过回归；Admin API 支持主体管理，默认检索覆盖 user/assistant/shared，`fact_key` 可精确召回，完整已完成回合可沉淀助手自述/shared 约定并抑制回声；同槽位冲突、`MemoryConsistencyGuard`、repair once、fallback 与 exact fact 流式提交前保护均已落地。Batch E Vue 记忆后台 typecheck/build 已通过（3 个 admin 类型错误已修复）。Batch F 的 20 组 L1 真实模型用例已逐项通过，新增长期伴侣“首次建立身高/体重/三围 → 三槽位持久化 → 新会话一致召回”用例。L2 本地链路已收口：新增端点配置 `reasoning_overhead_tokens`（线上 max_tokens 叠加思考开销）与 120s 超时，LM Studio Qwen3.6 可稳定产出可见正文；真实 `l2-isolation` 用例通过——L1 不泄漏、L2 本地召回。收口过程中发现并修复真实隐私漏洞：`_assistant_profile_overrides` 此前不过滤隐私等级，L2 助手档案（如 secret_code）会随系统提示进入 L1 云端上下文，现已按回合隐私等级过滤并补充回归测试（详见 docs/30）。
- [x] 记忆时间线与历史回溯完成详细设计：普通细节允许遗忘；需要时依据时间/主题/设备等线索查询 Timeline 和 Source；查不到必须明确无证据（详见 docs/31）。
- [x] Timeline / History Recall 第一版后端最小链路：0011 Timeline 索引与历史回填、completed turn/EventBus 索引、用户时区时间解析、历史意图、有界检索、受控 Source Expansion、无证据不回答、L2 隔离、删除联动、`decision_meta.recall` 与管理调试 API 已完成并通过自动化回归（详见 docs/31）。
- [x] P5 后端质量闸门升级：`ruff check server`、完整 `pytest`、严格 `mypy server/app server/tests` 与 `git diff --check` 作为固定闸门；测试层旧类型债已清理，后续不再退回只检查 app 的 mypy 口径。
- [x] Timeline 管理前端与真实数据验收：Vue 页面支持时间范围、actor/source/event_type、隐私、conversation、关键词查询，Timeline 详情与 Source 下钻；typecheck/build 已通过，浏览器实测真实数据（178 条事件、证据下钻展示原始消息）渲染正常。L1 真实模型已覆盖“刚才”“昨天晚上”和无证据负例。真实回归期间发现并修复了 importance 无相关性保底、中文单字假相关、L2 索引壳被 L1 问句遮挡，以及弱 Memory Top-K 错抬 `recall.mode` 四类问题。连续真实使用校准待补。
- [ ] 浏览器最终联调确认：后端合同已自动化覆盖“发布 Persona v2 → `/api/v1/meta/runtime` 为 v2 → REST Chat 新助手消息 `decision_meta.persona_version` 也为 v2”；chat/admin typecheck 与 build 全绿。浏览器实测已完成：管理后台登录/总览（Persona 小艾 v7 与模型配置 v15 一致展示）、记忆后台主体筛选与四类操作、Timeline 查询与 Source 证据下钻、聊天登录页 runtime 元信息（Persona 名称）展示。剩余消息级 `persona_version`/Recall 标签需用户本人聊天密码登录确认（`setup_required=false`，同一链路已由自动化合同覆盖）。
- [x] 自我记忆回归：确定性自动化已覆盖“第一会话建立助手身高 → 第二会话 exact fact 召回 → 相同复述不重复沉淀”以及同主体 fact_key 冲突/跨主体隔离；`server/scripts/p5_real_model_regression.py` 已实际使用 SenseNova 跑完 20 组 L1 用例，数字、日期、名字、偏好、shared、冲突、Timeline、删除、completed-turn 提取，以及“无既有档案时自然建立身高/体重/三围并跨会话保持一致”均已逐项通过。L2 真实调用已通过：本地专用路由 + 敏感记忆命中 + reasoning 开销适配后可见正文稳定，`l2-isolation`（L1 不泄漏 / L2 召回）逐项验证。
- [ ] 以 Vue chat 前端为载体连续 14 天真实文字使用（记录问题清单：检索质量、沉淀误判、隐私路由、前端体验；日志与检查单见 docs/32 §5）。
- [x] 记忆正/负例/冲突/删除/敏感隔离回归集在真实数据上的评估（docs/03 §1.8 的最小版）：`server/tests/memory_eval/` 确定性评估集已落地——正例 20/20（阈值 ≥16）、负例 0/20 错引（阈值 ≤1，无关/过期/已替换三类，正例语料作干扰）、冲突裁决 8/8、删除不可再现 4/4（自然语言 + fact_key + 台账）、敏感隔离 4/4；真实模型链路由 `make p5-real` 持续覆盖。配套增强：`_infer_fact_keys` 新增 city/job/pet/sport 通用槽位推断。
- [ ] 只修问题不扩功能；结束时输出 P5 闸门报告（草稿已建：docs/32，含 Release Criteria 对照、缺陷清单与 14 天使用期日志格式，期满定稿）。

## 当前批次：P6 Batch A（语音闭环垂直切片，已完成）

- [x] ADR-018 P6 入口复核：语音链路自建（与隐私闸门/回合取消/记忆耦合太深，不交给 OLV 进程）；OLV 仅作后续 Live2D/桌宠壳；C 方案（全量自建 pixi）继续后置（详见 docs/33 §1）。
- [x] `/ws/voice` 双向音频通道：与聊天 WS 同鉴权；hello 协商 PCM16/16k 单声道；上行二进制音频 + PTT 显式边界 + 客户端打断帧（详见 docs/33）。
- [x] 能量 VAD 断句：RMS 阈值 + 3 帧起始 + 15 帧 hangover + 30s 超长强制断句；打断判定用无 hangover 高灵敏单帧。
- [x] MiMo 语音接入：ASR（`mimo-v2.5-asr`，PCM 包 WAV 头 base64 上传）+ TTS（`mimo-v2.5-tts`，SSE 流式 PCM16 24kHz 直出）；L2 音频/文本禁止出站（云端 ASR 拒收、云端 TTS 拒合成，降级文字）。
- [x] TTS 提供方链：MiMo 为主、edge-tts 兜底；逐句选择、首块前失败无感切换、60s 失败冷却、L2 只选本地提供方；`voice.sentence` 按实际提供方声明 mime/sample_rate。
- [x] 句级流式切分 + 首句即合成；打断仲裁（barge-in → cancel 回合 + 中止 TTS）；延迟打点（ASR/首 token/首音频，M2.7 埋点）。
- [x] 语音配置中心化（M2.8 提前完成）：`HubConfig.voice` 节进配置中心，后台「模型与路由 → 语音」页可视化管理（ASR 开关/模型/语种/密钥、TTS 链增删排序/提供方切换/音色/密钥），保存校验与后端 HubConfig 规则对齐，浏览器实测通过；`ConfigVoiceSource` 每条话语解析一次提供方，改配置即时生效，不再依赖 .env.local。
- [x] 语音专项当前 32 项：VAD/切分/WAV 包装/提供方链/MiMo 契约/voice 配置节校验与工厂 + 语音 WS 端到端（完整回路、打断、L2 拒收、PTT、provider readiness）；全量闸门见下方质量基线。

## 当前批次：P6 待办

- [~] Batch B：浏览器麦克风/PTT/分句播放/整链打断代码已落地，L2 + 云 ASR 会在申请麦克风权限前阻断，配置热更新可在下次连接生效。当前 config v32 已启用 MiMo ASR + MiMo→edge TTS；2026-08-21 已复验 chat/admin/shared typecheck 与 production build 全绿。仍待真浏览器麦克风/音箱验收。
- [~] Batch B：faster-whisper 本地 ASR 的配置契约、延迟加载运行时、L2 路由、后台 provider 选择和错误语义已落地；新增“检查本地环境”只读自检，不加载/下载模型。当前产品决策先以 MiMo 云 ASR 为主，faster-whisper 保留为后续可选的 L2/离线能力，不再阻塞 P6 当前主线。
- [x] Batch B：silero-vad + openWakeWord 唤醒完善。`VoiceActivityDetector` 统一 `feed/force_end/is_voiced/backend`；检测到 `silero_vad + torch` 时优先使用 16k/512-sample Silero 概率判定，依赖/模型/推理异常一次性降级 Energy VAD；barge-in 继续走独立 RMS，避免推进 Silero 隐状态。openWakeWord 采用可选运行时：依赖存在时启用待命门，未唤醒音频不进入 VAD/ASR，命中后开放一次话语并发送 `voice.wake_detected`；PTT 始终绕过待命门，运行失败会发 `voice.wake_unavailable` 并退回原自动 VAD/PTT。当前 `.venv` 未安装 silero-vad/torch/onnxruntime/openWakeWord，因此真实运行仍为 Energy VAD + 原自动监听/PTT，不阻塞 MiMo 云语音主链。
- [x] Batch C：viseme 与延迟报表代码已落地。PCM TTS 按 50ms 窗口推送 `voice.viseme`；Chat 按真实播放时钟驱动口型强度条；最近 200 条语音回合/打断可聚合 count/P50/P90/max，Admin 已接入 M2 阈值卡片，`make p6-voice-m2` 可一键判卷。2026-08-21 已通过本轮前端 typecheck/build；剩余真浏览器音频与肉眼口型同步验收。
- [x] 流式超时看门狗 + 语音上下文裁剪（2026-08-21 线上 `stream_interrupted` 修复）：旧实现用 `asyncio.timeout` 给整段流式生成套总时长上限，flash-lite 在 20 条历史下 14s 出首句、持续出字至 20s 被硬掐断，已播出的 4 句音频无法收回且回复未落库。现 `stream()` 改为首 chunk 看门狗（`stream_first_chunk_timeout_ms`，缺省回退 `timeout_ms`）+ 出字后按相邻 chunk 间隔看护（`stream_idle_timeout_ms`，缺省 10s），出字后不再限制总时长；超时日志可区分「首 chunk 未到」与「出字后断流」。配套把语音回合上下文收窄到最近 8 条（`VOICE_CONTEXT_MESSAGES`，`start_turn(max_context_messages=…)`），压低 lite 模型首 token 延迟。新增回归：长于首 chunk 时限的持续出流必须完整生成、首 chunk 迟到快速降级、出字后断流不 fallback（`stream_interrupted`）与语音窗口接线。
- [~] M2 语音验收：确定性后端 soak 已通过同一 `/ws/voice` 连接的 20 完成 + 20 打断。2026-08-21 运行中指标窗口实际为 **0 完成 / 0 打断**，四项 acceptance 全 False；尚未开始真机 M2 计样。
- [ ] Batch D（M2 达标后）：OLV Live2D 最小渲染壳接入与 Tauri 桌宠评估；完整形象中心/多形象/主题仍属于 M3B。
- [~] M3A 地图/天气 Query Tools 基础实现已落地（`docs/35`）：Function Calling 完整/流式契约与真能力 probe、工具端点筛选、单工具回注、高德固定域名客户端、`get_weather/search_nearby/plan_route`、路网距离复核、L2/L3 零出站、文字/语音工具状态、Admin 配置、真实高德 Key 端到端验收，以及浏览器临时精确定位/WGS84→GCJ-02 均已完成。待办：模糊候选交互、TTL 缓存、结构化结果卡片/导航按钮、Admin 独立自检、最近 200 次工具台账与延迟报告。

## 下一步执行顺序

按依赖推进，P5 使用期与 P6 开发并行，但发现 P0/P1 文字/隐私问题时必须优先暂停 P6 新功能并修复：

1. **真浏览器语音闭环**（当前唯一主线阻塞）：麦克风授权/PCM 采集/分句播放/整链打断，同时确认 Admin 延迟卡片与 Chat viseme 按实际播放时序工作；
2. **M2 真机计样与判卷**：先重置指标窗口，完成至少 20 个语音回合与 20 个打断样本，再执行 `make p6-voice-m2`；要求首音频 P90 ≤1.8s、打断 P90 ≤300ms，并人工确认口型同步、连续 20 轮无积压；
3. **地图/天气产品收口**：真实高德 Key、Function Calling 模型、真天气/附近医院/路线与浏览器临时精确定位已经验收；随后补模糊候选交互、TTL 缓存、结构化结果卡片/导航按钮、Admin 独立自检和最近 200 次工具台账/延迟报告；
4. **P6 Batch D（M2 达标后）**：OLV Live2D 最小渲染壳接入与 Tauri 桌宠评估；完整形象中心/多形象/主题仍留在 M3B；
5. **可选本地语音链**：需要 L2/离线语音时再安装并验收 faster-whisper、Silero 与 openWakeWord，不影响当前云端语音主线；
6. **并行重启 P5 14 天真实文字使用计数**：从第一条可核验每日日志开始连续计 14 天；期满复跑 P5 闸门并定稿 docs/32。若出现未处置 P0/P1，P6 暂停扩展直至整改完成。

当前完整质量基线：pytest **242 通过 / 2 跳过**（244 collected）、`ruff check server` 全绿、`mypy server/app server/tests` **136 source files** 全绿；`git diff --check`、Alembic 单 head 与前端 typecheck/build 通过。新增地图/天气专项覆盖完整/流式 Function Calling、端点能力筛选、天气、附近 POI、步行路网排序、路线概要、L2 零调用、工具结果回注、浏览器临时精确定位与脱敏元数据。Uvicorn `health=200`、faster-whisper 配置 200/422 正反校验、MiMo TTS→ASR 真实探针、LM Studio Qwen 本地生成探针、config v32 L1 真模型请求与真实高德 Key 端到端调用均已通过基线。

## 已完成

- [x] M0 可扩展输入输出协议与 Adapter Registry。
- [x] 可靠 event/outbox/inbox、重试和故障恢复基础。
- [x] L0～L3 隐私分类、L3 非持久化和模型出站闸门。
- [x] 商汤文本、智谱文本/视觉/生图/视频与本地 LM Studio 模型的数据库配置中心。
- [x] 本地聊天身份、登录限流、会话撤销和资源归属。
- [x] PostgreSQL 会话与消息持久化。
- [x] WebSocket 真实流式回复、取消和消息补拉。
- [x] P1 结构化回复与 Persona：AgentReply 控制协议、Persona 数据库草稿/发布/回滚、`/admin/personas`、Open-LLM-VTuber 原生输出映射（详见 docs/20）。
- [x] P2 记忆系统 v1：混合检索重排、来源版本链溯源、沉淀判定与冲突裁决、聊天注入、管理 API（详见 docs/21）。
- [x] P3 第一批：硬删除版本链/来源/衍生引用 + 删除台账 + `/admin/memory` 可视化后台（详见 docs/22）。
- [x] P3 第二批：utility 提取器 + L2 事件级脱敏沉淀 + 规则回退（详见 docs/23）。
- [x] P3 第三批：会话删除级联清理记忆 + 台账重放（详见 docs/24）。
- [x] P3 第四批：pgvector 向量列双写 + ANN 召回 + 真实容器验证（详见 docs/25）。
- [x] P4：前端决策 ADR-018 + Vue chat/admin 正式前端（详见 docs/26～28）。
- [x] P5 前置联调：管理后台 Element Plus 统一、模型配置保存即生效、Persona 跨 worker 刷新与聊天运行时元信息（详见 docs/29）。
- [x] P5 收口：多主体记忆 Batch E/F（含 L2 reasoning 适配与隐私漏洞修复）、Timeline 前端、浏览器联调、记忆评估集与闸门报告草稿（详见 docs/30～32）。
- [x] P6 Batch A：`/ws/voice` 语音闭环垂直切片——VAD 断句、MiMo ASR/TTS + edge-tts 故障转移链、句级流式合成、barge-in 打断、语音配置中心化（详见 docs/33）。
- [x] 模型配置后台与文字聊天调试台。
- [x] 调试台固定视口、回车发送和消息自动贴底。
- [x] Open-LLM-VTuber v1.2.1 macOS 源码安装及真实 HTTP/WebSocket、Live2D、ASR、TTS、Aria PostgreSQL 端到端验证。
- [x] 管理后台导航闭环：总览与规划中模块均有明确页面、状态和后续交付说明。

## 暂缓事项

- Open-LLM-VTuber v1 深度 fork：上游正在规划 v2 全面重写。
- 3D/VRM、静态图片动态化和多端同步：不阻塞核心 v1。
- ESP32 与存在传感器：文字和语音稳定后进入 M3A。
