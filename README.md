# Aria · 伴侣中枢（Companion Hub）

> 一个部署在自有设备上的 AI 伴侣中枢。它把聊天、长期记忆、历史回溯、模型路由、隐私边界和未来设备能力放在同一个可扩展运行时中。

当前已具备文字、记忆、时间线、语音及地图/天气工具能力；多终端 Device Registry、Device Command Channel、Admin 设备工作区、Desktop Client 与主显示器受控读取代码闭环也已落地。下一条主线是原生真机验收、活动窗口/指定显示器和浏览器当前页读取。实时进度、未完成门槛和最新质量基线统一见 [TASKS.md](./TASKS.md)。

质量闸门统一使用 `make p5-backend`（ruff/mypy/pytest/diff-check）、`make p5-frontend`（web typecheck/build）、`make desktop-check`（Desktop 协议测试/build）和 `make p5-real`。真实模型矩阵可拆 `make p5-real-l1` 与 `make p5-real-l2`：前者只跑云端允许的 L0/L1 用例，后者只跑强制 `local_private` 的 L2 隔离用例。脚本默认从 `.env.local` 安全加载模型环境变量、使用 `config/hub.example.yaml` 路由，不打印 secret，也不写入项目业务数据库。示例配置的本地 private 基线为 LM Studio（OpenAI-compatible `http://127.0.0.1:1234/v1`，思考开销 `reasoning_overhead_tokens: 2048`、超时 120s）。报告路径可用 `P5_REPORT`、`P5_L1_REPORT`、`P5_L2_REPORT` 覆盖。

## 项目定位

Aria 不是单一聊天 UI，而是一个长期运行的本地 Companion Hub：

- 可插拔接入云端和本地大模型；
- 统一处理文字、语音、传感器和未来工具/设备输入；
- 通过 Persona、Memory、Timeline 和 Runtime Capability 分离“她是谁、她记得什么、过去发生过什么、现在能做什么”；
- 使用 L0～L3 隐私分级控制持久化和模型出站；
- 通过统一 Input/Output/Adapter 契约扩展新的终端和设备。

## 当前已经具备

- **文字聊天**：REST + WebSocket 流式回复、取消、消息持久化和多连接广播；
- **本地聊天身份**：首次设置、密码哈希、短期会话、资源归属和撤销；
- **模型路由**：`dialogue / utility / private` 三类路由，L2 强制本地，L3 不进入持久聊天；
- **模型配置**：后台“保存并生效”，区分文本/视觉/生图/视频模型；文本走三类路由，视觉/生成走独立能力槽位。当前智谱免费能力映射为 GLM-4.7-Flash、GLM-4.6V-Flash、CogView-3-Flash、CogVideoX-Flash；
- **Persona**：数据库草稿、发布、回滚，聊天新回合跨 worker 刷新当前版本；
- **结构化回复**：字幕、TTS 文本、情绪、表达和动作控制块；
- **Memory v1**：持久化、混合检索、来源、冲突、纠错、pgvector、删除台账；
- **多主体记忆**：user / assistant / shared 三主体、`fact_key` 稳定槽位精确召回、助手自述与 shared 约定沉淀、一致性守卫与回声抑制；
- **时间线与历史回溯**：Timeline 索引、相对时间解析、有界检索、Source 下钻、无证据不编造、L2 索引壳隔离；
- **记忆评估集**：`server/tests/memory_eval/` 确定性回归（正例 20 / 负例 20 / 冲突 / 删除 / 隔离），阈值对齐 Release Criteria；
- **语音闭环（P6 Batch A）**：`/ws/voice` 双向音频通道、VAD 断句、PTT、MiMo ASR/TTS + edge-tts 故障转移链、句级流式合成、barge-in 打断、L2 拒绝云端出站；语音配置在管理后台「模型与路由 → 语音」可视化管理并保存即生效；
- **地图与天气 Query Tools**：Function Calling、真实高德天气/附近 POI/路线、路网距离复核、浏览器临时精确定位、模糊候选、跨轮次 TTL 缓存、结构化结果卡片、Admin 独立自检和最近 200 次脱敏调用台账；
- **管理后台**：Vue 3 + Element Plus，覆盖模型、路由、语音、Persona、记忆、时间线和基础观测；
- **正式 Chat 前端**：Vue 3 + Vite；
- **现实能力边界**：模型只能把已上报的真实在线/授权能力当作可执行动作。

## 当前正在开发

### M3A 多终端与主动感知

先建设设备注册、配对、撤销、在线心跳和命令通道，再交付桌面客户端与浏览器扩展。目标体验是：用户可从手机、Web 或其他终端对 Aria 说“看一下我的电脑网页”，Hub 根据设备别名与实时能力向已授权电脑发出短时命令，由电脑本地截图或读取当前标签页，再把临时结果返回原会话。随后接入 MQTT 传感器和受 DND、冷却、每日上限约束的主动感知。详细设计见 [docs/34-大模型工具使用与主动感知设计.md](./docs/34-大模型工具使用与主动感知设计.md)。

### P6 语音化（Batch A～C 已完成，M2 延迟非阻塞优化）

设计见 [docs/33-P6语音化第一批设计.md](./docs/33-P6语音化第一批设计.md)。Batch A 已落地完整后端垂直切片：`/ws/voice` 通道（与聊天同鉴权、PCM16/16k）、能量 VAD 断句、MiMo 云端 ASR（PCM 包 WAV 头上传）、`TtsProviderChain` 提供方链（MiMo PCM 直出为主、edge-tts 免费兜底；逐句选择、首块前失败无感切换、60s 冷却）、LLM 流式按句切分首句即合成、播放中人声打断（cancel 回合 + 中止 TTS）、ASR/首token/首音频延迟打点。语音配置（密钥/音色/语种/链顺序）全部在管理后台配置，每条话语开始前从配置中心刷新，改配置即时生效。

Batch B/C 的浏览器采集、PTT、分句播放、整链打断、Silero/openWakeWord 可选适配、50ms viseme、延迟滑窗、Admin M2 卡片和判卷脚本均已落地，真浏览器麦克风、ASR、LLM、TTS、口型与打断全链已验收。当前 config v39 使用 faster-whisper `base/cpu/int8` 本地 ASR；真人暖态样本为 ASR 0.832s / 首 token 4.861s / 首音频 5.707s，打断 82ms。M2 延迟作为非阻塞优化项保留，20+20 真机判卷暂停到架构优化完成后。

### P5 使用期（并行）

以 Vue chat 前端为载体连续 14 天真实文字使用，问题按 docs/32 §5.2 格式记录；截至 2026-08-21 日志仍为空，应从首条可核验记录重新起算 14 天。期间只修问题不扩文字功能。

## 架构速览

```text
输入源
聊天 / 语音 / 设备 / 工具
          │
          ▼
InputGateway / Adapter
          │
          ▼
CognitionCore
  ├─ Persona             她是谁、怎么表达
  ├─ Working Context     当前发生什么
  ├─ Long-term Memory    平时就记得什么
  ├─ Timeline Recall     忘记后可回查什么
  └─ Runtime Capability  现在真正能做什么
          │
          ▼
LLM Router
dialogue / utility / private
          │
          ▼
AgentReply → OutputIntent → Output Adapter
```

完整总体架构见 [docs/02-功能设计.md](./docs/02-功能设计.md)。

## 文档入口

不要从 README 继续向下考古所有 Markdown。统一从：

**[docs/00-文档索引与架构总览.md](./docs/00-文档索引与架构总览.md)**

进入文档体系。它会标明：

- 哪份是当前设计真源；
- 哪些只是历史验收记录；
- 哪些 ADR 仍然有效；
- 同一个概念在多份文档出现时应该以哪份为准。

最常用入口：

| 文档 | 用途 |
|---|---|
| [01-需求分析](./docs/01-需求分析.md) | 产品范围、FR/NFR、风险和发布标准 |
| [02-功能设计](./docs/02-功能设计.md) | 总体架构与模块关系 |
| [03-开发规划](./docs/03-开发规划.md) | 里程碑与质量闸门 |
| [05-管理后台与可观测性](./docs/05-管理后台与可观测性.md) | Admin、指标、日志和配置语义 |
| [11-统一数据模型与 API 契约](./docs/11-统一数据模型与API契约.md) | 数据和 API 通用规则 |
| [12-安全边界同意与陪伴伦理](./docs/12-安全边界同意与陪伴伦理.md) | 隐私、同意和安全边界 |
| [14-输入输出扩展契约](./docs/14-输入输出扩展契约.md) | Adapter、能力协商和 I/O 协议 |
| [16-模型路由与配置中心](./docs/16-M0模型路由与配置中心.md) | 当前模型/路由配置语义 |
| [20-结构化回复与 Persona](./docs/20-结构化回复与Persona.md) | AgentReply、Persona、现实能力边界 |
| [30-多主体持久化记忆设计](./docs/30-多主体持久化记忆设计.md) | user / assistant / shared 长期记忆 |
| [31-记忆时间线与历史回溯设计](./docs/31-记忆时间线与历史回溯设计.md) | Timeline、History Recall、按时间回查 |
| [34-工具使用与主动感知](./docs/34-大模型工具使用与主动感知设计.md) | 多终端、屏幕/网页、传感器与主动感知 |
| [35-地图与天气工具设计](./docs/35-地图与天气工具设计.md) | Function Calling、高德天气/POI/路线、隐私与验收 |

当前任务状态看 [TASKS.md](./TASKS.md)，不要从历史验收文档推断当前进度。

## 配置生命周期

不同配置域不使用同一套 UI 生命周期：

| 配置域 | 当前语义 |
|---|---|
| 模型 / 路由 / 日志运行配置 | **保存即生效** |
| Persona | **草稿 → 发布 → 回滚** |
| Theme / 主动规则等未来配置 | 按风险决定是否版本化发布 |

模型配置底层仍可保留不可变 revision 用于审计，但不向用户暴露“模型配置版本工作流”。

## 隐私底线

| 等级 | 处理原则 |
|---|---|
| L0 | 普通环境/公开信息，可按策略持久化 |
| L1 | 个人信息，仅在用户数据域内持久化和使用 |
| L2 | 敏感/亲密内容，只允许本地模型；持久化必须满足脱敏策略 |
| L3 | 原始高敏传感数据，不进入通用持久化、日志、备份或云端 |

隐私和同意的完整规则见 [docs/12-安全边界同意与陪伴伦理.md](./docs/12-安全边界同意与陪伴伦理.md)。

## 仓库结构

```text
server/        FastAPI 后端、聊天、记忆、模型路由和管理 API
web/           Vue 3 chat/admin monorepo
contracts/     JSON Schema / TypeScript 契约
config/        配置样例
integrations/  Open-LLM-VTuber 等外部集成
firmware/      未来设备/固件
docs/          需求、设计、ADR、阶段验收记录
deploy/        部署相关资源
```

## 本地开发

要求：

- Python 3.11
- `uv`
- Node 20
- pnpm 10
- PostgreSQL + pgvector（完整后端环境）

安装和基础检查：

```bash
make install
make test
make lint
make schemas
pnpm --dir web install
pnpm --dir web run typecheck
pnpm --dir web run build
```

启动后端：

```bash
make run
```

开发前端可分别启动：

```bash
pnpm --dir web --filter @aria/chat dev
pnpm --dir web --filter @aria/admin dev
```

当前 Vite 开发端口：

- Chat：`http://localhost:5175/chat/`
- Admin：`http://localhost:5174/admin/`

后端默认：`http://127.0.0.1:8000/`

常用接口：

- `GET /healthz`
- `GET /api/v1/meta/runtime`
- `GET /api/v1/meta/protocol`
- `GET /api/v1/meta/adapters`
- `GET /api/v1/meta/config`
- `/api/v1/chat/conversations*`
- `WS /ws/chat`
- `WS /ws/voice`
- `/api/v1/model-capabilities/vision/analyze`
- `/api/v1/model-capabilities/images/generate`
- `/api/v1/model-capabilities/videos/generate`
- `/api/v1/admin/memories*`
- `/api/v1/admin/deletion-ledger`

## 容器启动

```bash
cp .env.example .env
# 编辑 .env，替换 PostgreSQL、MQTT 密码和 ARIA_ADMIN_TOKEN
docker compose up --build -d
curl http://localhost:8000/healthz
```

停止但保留数据：

```bash
docker compose down
```

PostgreSQL 默认映射到宿主机 `5433`，可通过 `POSTGRES_PORT` 修改。

## 密钥与本地配置

- 不要提交 `.env.local` 或真实密钥；
- 模型密钥只通过 `env:变量名` 引用，不写入数据库正文；
- `ARIA_ADMIN_TOKEN` 只用于管理能力和首次身份设置，不应成为读取私人聊天的通用 Token；
- GLM Coding Plan 等特定产品授权不要当作普通模型 API Key 复用。

## 当前阶段与下一步

当前执行顺序、并行门槛和暂缓项只维护在 [TASKS.md](./TASKS.md)，README 不再复制一份容易过期的任务清单。

并行约束：P5 的 14 天真实文字使用从 docs/32 首条有效日志重启计数，期间发现的 P0/P1 文字链路问题优先修复；P5 期满达标后输出闸门报告定稿。

## 文档维护约定

- README 只做项目入口，不复制详细架构；
- `docs/00` 是文档地图；
- Active 文档定义当前规则；
- Phase Record 只记录某一阶段当时的实现和验收；
- `TASKS.md` 只维护执行状态；
- 实际数据库结构以 Alembic migration 为准；
- 代码、界面文案和注释以中文为主，必要的协议/标识保留英文。
