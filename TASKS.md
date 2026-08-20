# Aria 开发任务清单

> 最后更新：2026-08-20
> 当前阶段：P6 · 语音与 Live2D 产品化（Batch A 语音闭环已落地；P5 的 14 天真实使用并行进行，日志见 docs/32 §5）

## 进度概览

| 阶段 | 状态 | 目标 |
|---|---|---|
| P0 Open-LLM-VTuber 集成验证 | 已完成 | 通过自定义 Agent 接入 Aria 实时聊天 |
| P1 结构化回复与 Persona | 已完成 | 统一文本、情绪、语音和动作输出 |
| P2 记忆系统 v1 | 已完成 | 可检索、可溯源、可纠错的长期记忆 |
| P3 删除闭环与记忆后台 | 已完成 | 跨存储删除与可视化管理 |
| P4 正式前端决策 | 已完成 | 决定 Vue 3 与 Open-LLM-VTuber 的边界 |
| P5 文字稳定性闸门 | 使用期 | 自动化闸门全绿，14 天真实使用进行中（docs/32） |
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

## 当前批次：P5（进行中）

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
- [x] 测试 15 项：VAD/切分/WAV 包装/提供方链/MiMo 契约单测 + 语音 WS 端到端（完整回路、打断、L2 拒收、PTT）；闸门 ruff / mypy 115 文件 / pytest 182 通过。

## 当前批次：P6 待办

- [ ] Batch B：chat 前端麦克风采集与播放 UI（PCM16 采集 + sentence 事件驱动的分句播放 + 打断按钮）；MIMO_API_KEY 实配真连验证。
- [ ] Batch B：faster-whisper 本地流式 ASR（L2 语音路径）+ silero-vad 替换能量 VAD + 唤醒词（openWakeWord）。
- [ ] Batch C：viseme 口型通道（50ms 幅度包络）+ 延迟打点报表面板 + 语音配置中心化（M2.8）。
- [ ] Batch D：OLV Live2D 渲染端接入与 Tauri 桌宠评估。
- [ ] M2 验收：说完 → 首字 ≤1.8s（P90）、打断 ≤300ms、口型肉眼同步、连续 20 轮无积压。

## 下一步执行顺序

按依赖推进，不并行扩展 P6 功能。可开发项已全部完成（2026-08-20），剩余为使用与确认：

1. ~~补齐 docs/30 Batch E/F~~：已完成——L1 真实模型 20/20；reasoning 适配后 L2 可见正文稳定，`l2-isolation` 通过；记忆后台 typecheck/build 全绿；
2. ~~收口 docs/31 第一版体验~~：已完成——后端最小链路 + Timeline 管理页 + L1 真实正/负例 + 浏览器实测（178 条事件、Source 下钻）；
3. ~~完成浏览器 P5 最终联调~~：管理后台/记忆/Timeline/聊天登录页已实测；消息级 `persona_version` 留待用户本人登录确认（docs/32 §5.3）；
4. ~~补齐前端质量闸门~~：admin/chat typecheck 与 build 全绿；后端 migration、pytest、ruff、mypy 全绿（pytest 167 通过）；
5. **进入连续 14 天真实文字使用**（进行中）：只记录和修复稳定性问题，不新增语音、Live2D、传感器或设备控制；日志按 docs/32 §5.2 格式追加；
6. **输出 P5 Gate Report**：草稿已建（docs/32），期满按 §6 条件定稿，达标后进入 P6 语音与 Live2D 产品化。

P6 之前只允许做与 P5 闸门直接相关的底层接口兼容或缺陷修复。真实设备 `RuntimeCapabilityProvider` 接入、ESP32、3D/VRM、静态图动态化继续后置。

## 已完成

- [x] M0 可扩展输入输出协议与 Adapter Registry。
- [x] 可靠 event/outbox/inbox、重试和故障恢复基础。
- [x] L0～L3 隐私分类、L3 非持久化和模型出站闸门。
- [x] 商汤、GLM 预留和本地模型的数据库配置中心。
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
- [x] 模型配置后台与文字聊天调试台。
- [x] 调试台固定视口、回车发送和消息自动贴底。
- [x] Open-LLM-VTuber v1.2.1 macOS 源码安装及真实 HTTP/WebSocket、Live2D、ASR、TTS、Aria PostgreSQL 端到端验证。
- [x] 管理后台导航闭环：总览与规划中模块均有明确页面、状态和后续交付说明。

## 暂缓事项

- Open-LLM-VTuber v1 深度 fork：上游正在规划 v2 全面重写。
- 3D/VRM、静态图片动态化和多端同步：不阻塞核心 v1。
- ESP32 与存在传感器：文字和语音稳定后进入 M3A。
