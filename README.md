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
- [ ] M0.4 并发恢复闸门 —— 多 worker 竞争、进程 kill/recover、持续消息压力与可观测指标

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
- JSON Schema：`contracts/jsonschema/`
- TypeScript 类型：`contracts/types/index.d.ts`

`ARIA_RUN_DISPATCHER` 默认关闭。只有当应用已为所有会产生的 topic 注册命名消费者后才应开启；开启后 `/healthz` 会增加 `dispatcher` 运行状态。Adapter 输入必须经过 `GuardedInputSink`，设备声明的隐私等级只是下限，服务端策略可以上调，L3 数据不会进入 event/outbox 数据库。

## 容器启动

```bash
cp .env.example .env
# 编辑 .env，替换 PostgreSQL 和 MQTT 密码
docker compose up --build -d
curl http://localhost:8000/healthz
docker compose logs -f hub
```

停止服务但保留数据：

```bash
docker compose down
```

开发 Compose 会自动执行 Alembic migration，并显式启用 `/api/v1/dev/events` 测试入口。该入口没有设计为生产 API；共享部署前必须关闭 `ARIA_ENABLE_DEV_ENDPOINTS` 并完成身份模块。
