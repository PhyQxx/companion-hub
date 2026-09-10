# MCP 整体架构与接入实施方案

> 日期：2026-09-08  
> 状态：已定稿，MCP-C0 已实现（2026-09-08）  
> 范围：Companion Hub 作为 MCP Client 接入外部工具；Aria MCP Server 暂不启动

## 1. 架构结论

MCP 是 Hub 的外部工具适配层，不替换内部工具目录、Action Plan、权限、隐私、确认和审计。模型优先使用 Aria 的稳定工具名与结果结构，MCP Server 的远端名称和 Schema 由适配层转换。

```text
聊天模型
  ↓ 本轮少量工具定义
内部工具目录
  ↓ 身份 / 隐私 / 白名单 / 确认 / 审计
  ├─ 本地工具
  ├─ Home Assistant REST/WebSocket 适配器
  └─ MCP Client 适配器 → 外部 MCP Server
```

Hub 作为 MCP Server 是另一条产品线：只有其他 AI 客户端确实需要复用 Aria 能力时再做，且必须复用同一授权、确认和审计层。

## 2. 模块边界

`server/app/integrations/mcp/` 承担：

- Server 配置、连接生命周期与健康状态；
- `tools/list` 全分页发现、白名单过滤、本地目录缓存与名称空间；
- 远端 Schema 到内部工具定义的转换；
- `tools/call` 执行和有界结果裁剪；
- 稳定错误码和可观测状态。

MCP 层不承担：

- 用户是否已确认写操作；
- 模型是否可在当前隐私级别使用某工具；
- 业务语义下的写后回读、撤销和补偿。

这些继续由 Tool Registry、Egress Guard 和 Action Plan 负责。

## 3. 连接与配置

第一批只开放 Streamable HTTP，默认要求 HTTPS。本地调试需显式允许 loopback HTTP。不开放管理员可自由填写的 stdio shell 命令，避免配置面变成任意代码执行入口。

每个 Server 配置包含：`server_id`、`enabled`、`endpoint`、凭据引用、工具白名单、连接/调用超时、目录缓存时间、最大并发和写工具开关。凭据仅通过 secret 通道解析，不进入模型或管理 API 回包。

## 4. 工具发现与 Token 预算

1. SDK 完成协议协商，Hub 逐页读取工具目录。
2. 远端目录先与本地 `allowed_tools` 求交集，再进入应用缓存。
3. 内部名称使用 `mcp.{server_id}.{remote_name}`，防止多 Server 重名。
4. 普通对话不注入 MCP 目录；根据意图检索后，每轮最多加载 `max_tools_per_turn` 个定义。
5. 工具描述、Schema 和返回结果均设独立体积上限。

工具发现是 Hub 行为，不等于把全部工具告诉模型。

## 5. 安全执行链

```text
用户与会话身份
  → 隐私级别
  → Server 开关
  → 工具白名单
  → JSON Schema 校验
  → 风险分级
  → 确认票据 / Action Plan
  → MCP 调用
  → 结果裁剪与审计
```

远端描述和返回文本都是不可信数据，不得覆盖 Aria 系统规则。`readOnlyHint` 等远端注解只是参考，本地策略才是权威。未显式标记为只读的工具默认按写工具处理。

写操作超时后不自动重发；先回读验证，不能确定时返回 `unknown_outcome`。模型传入 `confirmed=true` 不能代替服务端确认票据。

## 6. 可观测性

Admin 提供 Server 健康、协议版本、服务端身份、工具数、目录刷新时间和最近错误。调用台账后续记录 Server/工具、用户、隐私、风险、延迟和稳定错误码，不保存凭据和未裁剪敏感原文。

## 7. 实施批次

| 批次 | 交付 | 验收 |
|---|---|---|
| MCP-C0 | 配置、SDK 边界、连接管理、分页目录、白名单、错误码、Admin 状态 | 未配置时零连接；假 Server 可验证发现/失败/重名隔离 |
| MCP-C1 | 一个真实只读 Server | 真实认证、发现、调用、裁剪、隐私和审计全链 |
| MCP-C2 | 工具检索与每轮动态加载 | 工具数增加不使普通对话 Token 线性增长 |
| MCP-D | 写工具与 Action Plan | 确认、幂等、回读、未知结果和审计通过 |

HA 继续使用现有 REST/WebSocket。只有目标 HA MCP Server 真实覆盖当前状态、历史、控制参数、授权范围、状态订阅和结果验证后，才评估局部替换。

## 8. MCP-C0 实施记录

- 引入官方 Python SDK v2，SDK 与 `httpx2` 只出现在 `integrations/mcp/client.py` 边界。
- 已落地 HTTPS/loopback 端点校验、secret 引用、总开关、Server 开关、白名单、超时、目录 TTL 和结果体积限额。
- `McpManager` 已支持协议协商信息、分页目录、Server 命名空间、未知工具按写操作处理、稳定错误码和有界只读调用结果。
- 写工具即使被管理员允许进入目录，也不能经直接调用路径执行，必须等 MCP-D 接入 Action Plan。
- Admin API 与「模型与路由 → MCP 工具」页已提供 Server 状态、手动刷新和授权工具目录。
- MCP/配置/API 定向回归 19 项通过；非 soak 全量 819 通过、2 跳过，Ruff、全量严格 mypy 和 Admin production build 通过。

MCP-C1 尚未连接真实凭据。试点已选定 **Context7**：

- 远端：`https://mcp.context7.com/mcp`；
- 认证：`Authorization: Bearer` API Key，经 `env:ARIA_MCP_CONTEXT7_API_KEY` 解析；
- 白名单：`resolve-library-id`、`query-docs`；
- 范围：只查公开软件库文档，禁止 L2 内容和仓库私密上下文外发；
- 验收：真实协议协商、两项工具发现、各一次只读调用、返回裁剪、审计与失效凭据错误码。

2026-09-09 首次真实联调：已协商到 `2026-07-28` 协议，识别服务端 `Context7`，发现的两项白名单工具均声明 `readOnlyHint=true`。匿名限额路径下，`resolve-library-id` 与 `query-docs` 各一次真实调用成功；用户提供的凭据被 Context7 拒绝，远端明确要求 API Key 以 `ctx7sk` 开头。在换入有效凭据并完成鉴权调用/审计前，C1 不标记完成。

GitHub MCP 列为第二试点候选，原因是它对本项目有价值，但涉及账号授权、私有仓库内容和更大工具集，不适合承担第一次连通验证。

## 9. 官方依据

- [MCP Python SDK 客户端](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/index.md)
- [MCP Python SDK 传输](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/transports.md)
- [MCP 2026-07-28 Schema](https://github.com/modelcontextprotocol/python-sdk/blob/main/schema/2026-07-28.json)
