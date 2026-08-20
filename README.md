# Aria · 伴侣中枢（Companion Hub）

> 一个部署在自有设备上的 AI 伴侣中枢。它把聊天、长期记忆、历史回溯、模型路由、隐私边界和未来设备能力放在同一个可扩展运行时中。

当前阶段：**P5 · 文字稳定性闸门**。核心文字链路已经具备，多主体长期记忆已打通数据模型、检索、完整回合沉淀、冲突保护和跨会话助手自我事实闭环；Timeline / History Recall 的第一版后端最小链路也已落地。20 组 L1 真实模型用例已经逐项通过，其中包括“新伴侣首次建立身高/体重/三围 → 三槽位持久化 → 新会话一致召回”的角色自我档案用例。当前主要剩余 L2 本地模型验收、管理前端/浏览器验收和连续 14 天真实使用。语音、Live2D 和真实设备控制属于后续阶段。

当前后端质量基线：完整 pytest、`ruff check server`、严格 `mypy server/app server/tests`（102 个 source files）和 `git diff --check` 均保持全绿。机器上存在 nvm Node v22/pnpm，但当前 Coding MCP 禁止执行 workspace 外可执行文件，因此本会话无法完成前端 typecheck/build；这属于工具执行边界，不代表前端代码已经通过或失败。

P5 收口阶段统一使用 `make p5-backend`、`make p5-frontend` 和 `make p5-real`。真实模型矩阵还可拆成 `make p5-real-l1` 与 `make p5-real-l2`：前者只跑云端允许的 L0/L1 用例，后者只跑强制 `local_private` 的 L2 隔离用例。脚本默认从 `.env.local` 安全加载模型环境变量、使用 `config/hub.example.yaml` 路由，不打印 secret，也不写入项目业务数据库。示例配置的本地 private 基线已切到 LM Studio：原生管理 API 为 `http://127.0.0.1:1234/api/v1/*`，Hub 当前 OpenAI-compatible 运行时 Base URL 使用 `http://127.0.0.1:1234/v1`。当前实际数据库配置仍需在管理页保存后才会切换。报告路径分别可用 `P5_REPORT`、`P5_L1_REPORT`、`P5_L2_REPORT` 覆盖。

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
- **管理后台**：Vue 3 + Element Plus，覆盖模型、Persona、记忆和基础观测；
- **正式 Chat 前端**：Vue 3 + Vite；
- **现实能力边界**：模型只能把已上报的真实在线/授权能力当作可执行动作。

## 当前正在开发

### 多主体长期记忆

长期记忆从默认“关于用户”扩展为：

```text
user       关于用户
assistant  关于助手自身
shared     关于双方共同经历/约定
```

并增加 `fact_key` 稳定事实槽位、冲突检测、助手复述防自我强化和生成一致性检查。

设计见 [docs/30-多主体持久化记忆设计.md](./docs/30-多主体持久化记忆设计.md)。当前 Batch A～D 后端已落地并通过回归：Admin API 可管理主体，默认检索覆盖 user / assistant / shared，`fact_key` 支持精确槽位召回，已完成回合会沉淀助手自述和受证据约束的 shared 约定，并抑制“记忆注入后复述”造成的自我强化；同主体同槽位走确定性冲突，`MemoryConsistencyGuard` 会在提交前 repair 冲突事实，流式 exact fact 回合也不会先泄露未校验的错误值。Batch E 的 Vue 记忆后台主体筛选/字段/手动创建代码已落地；机器上存在 nvm Node v22/pnpm，但当前 Coding MCP 禁止执行 workspace 外可执行文件，因此前端 typecheck/build 尚待可执行环境验证。

### 时间线与历史回溯

系统不要求把所有历史都提升为长期 Memory。新的上位设计允许 AI “忘记”普通细节，但在用户提供大致时间、主题、人物或设备线索时，受控地检索 Timeline 和原始 Source。

设计见 [docs/31-记忆时间线与历史回溯设计.md](./docs/31-记忆时间线与历史回溯设计.md)。当前 Phase A/B 后端最小闭环已落地：`0011_timeline_event` 会为已有 message/event 做安全回填，完成回合与后续 EventBus 事件持续进入 Timeline；历史意图、用户时区相对时间解析、有界检索、有限 Source Expansion、无证据不编造、L2 索引壳与 `decision_meta.recall` 已接入 ChatService。Timeline Vue 管理页也已接入时间、actor/source/event_type、隐私与 conversation 筛选、事件详情和受控 Source 下钻；当前主要剩 Node 环境下的 typecheck/build、浏览器/真实历史数据验收，以及后续设备聚合/retention 和 Memory Promotion。

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

当前执行清单以 [TASKS.md](./TASKS.md) 为准。P5 主要目标是：

1. 收口 LM Studio/Qwen3.6 的 reasoning 输出适配，并把当前数据库 `local_private` 更新到本机 LM Studio `/v1` 配置后补齐 Batch F 的 L2 隐私真实调用；
2. 补齐 Memory/Timeline 管理前端的构建、浏览器和真实数据验收；
3. 完成 Chat Persona 浏览器最终联调；
4. 在 Node/pnpm 可用环境补齐前端 typecheck/build；
5. 进入连续 14 天真实文字使用；
6. 只根据真实问题修复，不在稳定性闸门期间无边界扩功能。

P5 通过后，再进入语音、Live2D 和真实设备能力产品化。

## 文档维护约定

- README 只做项目入口，不复制详细架构；
- `docs/00` 是文档地图；
- Active 文档定义当前规则；
- Phase Record 只记录某一阶段当时的实现和验收；
- `TASKS.md` 只维护执行状态；
- 实际数据库结构以 Alembic migration 为准；
- 代码、界面文案和注释以中文为主，必要的协议/标识保留英文。
