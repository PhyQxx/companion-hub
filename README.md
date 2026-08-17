# Aria · 伴侣中枢（Companion Hub）

> 一个部署在自有设备上的 AI 伴侣中枢：可插拔接入大模型，具备四层记忆与主动思考能力，
> 通过统一协议接入多种输入源（文字 / 语音 / 传感器）与输出端（聊天 / 语音 / Live2D / 3D / 投影）。

## 项目文档

| 文档 | 内容 |
|---|---|
| [docs/01-需求分析.md](./docs/01-需求分析.md) | 愿景与定位、假设约束、用户故事、功能/非功能需求清单（FR/NFR 编号）、范围外事项、风险分析、v1 验收标准 |
| [docs/02-功能设计.md](./docs/02-功能设计.md) | 总体架构、核心协议（InputEnvelope / AgentReply / OutputIntent / 设备描述）、九大模块设计、关键流程时序、数据库 DDL、REST/WS/MQTT 接口清单、隐私分级与安全设计 |
| [docs/03-开发规划.md](./docs/03-开发规划.md) | 技术栈定稿、里程碑（M0~M5）任务分解与验收标准、仓库结构、硬件采购清单、测试策略、部署方案、成本预估、启动检查单 |
| [docs/04-视觉示意与原型.md](./docs/04-视觉示意与原型.md) | 产品概念图、总体架构、可靠事件链路、隐私分流、记忆生命周期、桌面端线框与阶段路线 |
| [docs/05-管理后台与可观测性.md](./docs/05-管理后台与可观测性.md) | 后台原型、配置中心、指标口径、统计分析、日志追踪、隐私审计、告警与备份恢复 |
| [docs/06-主题与视觉系统.md](./docs/06-主题与视觉系统.md) | 多主题原型、Design Token、自定义、自动切换、形象联动、无障碍、数据模型与 API |
| [docs/07-伴侣形象与角色系统.md](./docs/07-伴侣形象与角色系统.md) | 多种典型形象、自定义/导入、用户形象实例、默认 Aria 示例、跨引擎能力、Live2D 与安全验收 |
| [docs/08-静态图片动态化.md](./docs/08-静态图片动态化.md) | 单图生成轻量 2D/神经 2.5D 伴侣的流水线、实时驱动、Live2D 精修路线、API 与隐私验收 |
| [docs/09-运行状态机与多端同步.md](./docs/09-运行状态机与多端同步.md) | 统一回合状态、取消打断、音频租约、多端同步、降级体验与本地身份闭环 |
| [docs/10-任务资产与升级运维.md](./docs/10-任务资产与升级运维.md) | Job/Scheduler、Asset Store、保留配额、备份升级、许可证、工具安全与运维验收 |
| [docs/11-统一数据模型与API契约.md](./docs/11-统一数据模型与API契约.md) | 统一实体、ID、外键、身份、工具执行、REST/WS、分页、幂等、上传与版本兼容 |
| [docs/12-安全边界同意与陪伴伦理.md](./docs/12-安全边界同意与陪伴伦理.md) | 用户控制、访客同意、录音/传感器、真人素材、高风险建议、非操纵关系与红队验收 |
| [docs/13-需求追踪与架构决策.md](./docs/13-需求追踪与架构决策.md) | FR/NFR→设计→里程碑→测试追踪矩阵、发布定义、ADR、待决问题与设计冻结规则 |
| [docs/14-输入输出扩展契约.md](./docs/14-输入输出扩展契约.md) | 统一多模态输入/输出协议、L3 临时信号管道、Adapter 生命周期、能力协商、路由降级与 M0 契约测试 |
| [docs/15-M0输入输出骨架验收报告.md](./docs/15-M0输入输出骨架验收报告.md) | I/O 扩展、隐私隔离、可靠事件、故障恢复和容器鉴权的实现验收矩阵 |
| [docs/16-M0模型路由与配置中心.md](./docs/16-M0模型路由与配置中心.md) | 商汤 / GLM / 本地模型分工、隐私路由、配置热更新、回滚、观测与连通性验证 |
| [docs/17-M1文字聊天闭环.md](./docs/17-M1文字聊天闭环.md) | 会话与消息持久化、聊天 API、动态模型路由、隐私约束、调试页与当前边界 |
| [docs/18-本地聊天身份边界.md](./docs/18-本地聊天身份边界.md) | 首次设置、密码哈希、短期聊天会话、资源归属、撤销、限流和后续身份演进 |
| [docs/19-WebSocket流式回合.md](./docs/19-WebSocket流式回合.md) | 真实模型流、回合状态、取消隔离、多端广播、消息游标补拉和协议边界 |
| [docs/20-结构化回复与Persona.md](./docs/20-结构化回复与Persona.md) | AgentReply 控制协议、Persona 数据库版本、管理后台与 Open-LLM-VTuber 映射 |

## 一图速览

```
输入源(聊天/语音/传感器) → InputEnvelope / EphemeralSignal → 事件或临时信号管道
  → 感知引擎(语义事件) + 主动引擎(分寸控制)
  → 认知核心(人格+记忆检索+LLM) → AgentReply(结构化)
  → 记忆系统(沉淀/反思) + OutputIntent → 输出网关(仲裁/分发/回执)
  → 文字 / TTS语音 / Live2D看板娘 / VRM 3D / 投影端
```

## 快速事实

- **技术栈**：Python 3.11 + FastAPI + PostgreSQL(pgvector) + Mosquitto(MQTT)；前端 Vue3 + pixi-live2d-display / three-vrm；固件 ESP32 + ESPHome
- **周期**：单人业余开发（每周 10~15h），以质量闸门推进；核心 v1 预计约 12~18 个月，形象工厂和多端 3D 属后续增强
- **月成本**：常规路线 ¥30~90（云端 LLM 按量 + 免费 TTS + 家中主机）
- **隐私底线**：数据分 L0~L3 四级，传感器原始数据（L3）永不上云、默认不落库；亲密语境（L2）仅事件级标签且强制走本地模型

## 当前状态

- [x] 产品范围冻结（v1.3，2026-08-17；核心 v1 截止 M3A）
- [x] M0.1 初始工程骨架 —— Python 3.11、FastAPI、uv、ruff、mypy、pytest、CI
- [x] M0.3 协议首版 —— Pydantic → JSON Schema → TypeScript，含 UUIDv7、durable/ephemeral 分流和 mock adapter 测试
- [x] M0.2/M0.4 基础实现 —— PostgreSQL/Alembic、event/outbox/inbox/dead-letter、dispatcher 租约恢复、Mosquitto 和 Docker Compose
- [x] M0.4 单实例集成 —— 真实 PostgreSQL/Mosquitto 容器迁移、鉴权、幂等写入、outbox 发布与回收验证
- [x] 入口隐私闸门 —— 服务端重分类、L3 落库阻断、带 TTL 和背压策略的有界内存信号缓冲
- [x] 常驻事件分发 —— FastAPI 生命周期 worker、本地命名消费者、inbox 去重、失败重试与健康状态
- [x] M0.4 并发恢复闸门 —— PostgreSQL `SKIP LOCKED` 多 worker 竞争和 lease 持有者终止恢复测试
- [x] M0 I/O 契约闸门 —— 显式 registry、四个参考 adapter、能力交集、降级、取消和 unknown outcome
- [x] M0.5 模型适配与双路由 —— OpenAI-compatible 适配、对话/后台/私密路由、重试降级和 L2/L3 出站闸门
- [x] M0.6 配置与最小观测 —— 数据库版本、事务发布/回滚、可视化后台、安全元数据、结构化日志和耗时 span
- [x] M1 文字闭环第一阶段 —— 会话/消息落库、最近 20 条上下文、动态读取已发布模型配置、L2 本地强制路由、L3 持久聊天阻断和调试页
- [x] 本地聊天身份第一阶段 —— 管理员授权首次设置、scrypt 密码哈希、8 小时随机会话、退出撤销、登录限流及会话归属隔离
- [x] M1 WebSocket 回合第一阶段 —— 首帧认证、真实供应商 delta、generation 取消、迟到提交阻断、多连接广播和已提交消息补拉
- [x] P1 结构化回复与 Persona —— 字幕/TTS/情绪/动作协议、数据库草稿发布回滚、管理后台和 Open-LLM-VTuber 原生输出映射

## 本地开发

要求：Python 3.11、[uv](https://docs.astral.sh/uv/)、Node 20、pnpm 10。

```bash
uv sync --dev
uv run pytest
uv run ruff check server
uv run mypy server/app
uv run python server/scripts/export_schemas.py
pnpm --dir contracts install --frozen-lockfile
pnpm --dir contracts run generate
pnpm --dir contracts run check
uv run uvicorn app.main:app --app-dir server --reload
```

- 健康检查：`GET /healthz`
- 协议元数据：`GET /api/v1/meta/protocol`
- 已登记 Adapter：`GET /api/v1/meta/adapters`
- 已发布模型配置（不返回密钥引用）：`GET /api/v1/meta/config`
- 模型配置后台：`GET /admin/models`
- Persona 管理后台：`GET /admin/personas`
- 文字聊天调试页：`GET /chat`
- 聊天身份 API：`/api/v1/auth/status|setup|login|me|logout`
- 文字聊天 API：`/api/v1/chat/conversations*`（仅接受独立聊天会话 Token）
- 实时聊天：`WS /ws/chat`（连接后 5 秒内发送 `authenticate` 首帧）
- JSON Schema：`contracts/jsonschema/`
- TypeScript 类型：`contracts/types/index.d.ts`

`ARIA_RUN_DISPATCHER` 默认关闭。只有当应用已为所有会产生的 topic 注册命名消费者后才应开启；开启后 `/healthz` 会增加 `dispatcher` 运行状态。Adapter 输入必须经过 `GuardedInputSink`，设备声明的隐私等级只是下限，服务端策略可以上调，L3 数据不会进入 event/outbox 数据库。

连接数据库时，首次启动从 `config/hub.example.yaml` 引导模型配置版本 1，并自动建立默认 Aria Persona；之后模型与 Persona 的草稿、发布指针和回滚历史都保存在数据库。后台地址为 `/admin/models` 和 `/admin/personas`，管理 API 必须配置 `ARIA_ADMIN_TOKEN` 才会启用；未连接数据库时仍保留 YAML 文件模式。密钥只允许通过 `env:变量名` 引用，不保存明文。手工验证商汤连接：

```bash
set -a
. ./.env.local
set +a
make llm-check
```

不要把 `.env.local` 提交到仓库。若要启用预留的 GLM-5.3 运行时配置，必须使用独立的普通 API 授权密钥；GLM Coding Plan Pro 密钥只用于其官方支持的编码工具。

源码启动后打开 `http://127.0.0.1:8000/chat`。首次使用时通过 `ARIA_ADMIN_TOKEN` 授权创建聊天密码；之后管理 Token 不能读取或发送聊天内容，只能使用 8 小时有效、可撤销的独立聊天会话。页面优先使用 WebSocket 展示真实模型 delta 并支持取消，连接不可用时退回同步 REST。L0/L1 可使用数据库当前发布的云模型；L2 强制走 `private` 本地路由，本地模型不可用时返回明确失败，不会降级到云端。AgentReply 控制块会从可见流中剥离并作为 `reply.control` 单独广播；解析失败自动降级为普通文本。refresh 轮换、设备配对和记忆检索仍属于后续工作。

## 容器启动

```bash
cp .env.example .env
# 编辑 .env，替换 PostgreSQL、MQTT 密码和 ARIA_ADMIN_TOKEN
docker compose up --build -d
curl http://localhost:8000/healthz
docker compose logs -f hub
```

PostgreSQL 默认映射到宿主机 `5433`，可通过 `POSTGRES_PORT` 修改，避免占用本机常见的 `5432`。

停止服务但保留数据：

```bash
docker compose down
```

开发 Compose 会自动执行 Alembic migration，并显式启用 `/api/v1/dev/events` 测试入口。该入口没有设计为生产 API；共享部署前必须关闭 `ARIA_ENABLE_DEV_ENDPOINTS` 并完成身份模块。
