# Home Assistant 集成设计

> 文档类型：专项设计
> 状态：Active
> 最后更新：2026-08-27
> 目标阶段：M3A 设备能力与主动感知
> 相关真源：[02-功能设计](./02-功能设计.md)、[12-安全边界同意与陪伴伦理](./12-安全边界同意与陪伴伦理.md)、[14-输入输出扩展契约](./14-输入输出扩展契约.md)、[34-大模型工具使用与主动感知设计](./34-大模型工具使用与主动感知设计.md)

> 实施进度：HA-0/HA-2 已落地，包括 REST/WebSocket 状态同步、实体白名单、字段脱敏、`home_get_state`、`home_get_history`、`home_control`、动作白名单、服务端确认门禁、脱敏调用台账和后台可视化配置。真实实例 `https://ha.pnkx.top:8` 已联通，当前授权 11 个实体；灯/开关可控，空调写操作要求当前用户消息明确确认。Perception Pipeline 第一批已将 HA `person` 与存在传感器状态接入 M3B 认知调度闭环，主动消息经 `ProactiveDeliveryService` 统一投递至 Web/桌面通知/语音。

## 1. 目标与结论

本方案将 Home Assistant（以下简称 HA）作为物理设备控制平面，将 Aria Hub 作为对话、意图理解、记忆、隐私策略、确认和主动交互平面。Aria 不重复适配 Zigbee、Matter、米家等协议，而是通过 HA 的标准 API 读取实体状态、执行受控动作，并将稳定状态变化转换为中枢语义事件。

最终边界为：

- HA 负责设备接入、实体状态、服务调用、场景和确定性自动化；
- Aria 负责自然语言理解、目标解析、能力暴露、风险判断、用户确认、审计和主动提醒；
- HA WebSocket 用于状态同步和事件订阅；
- HA REST API 用于执行服务；
- MQTT Discovery 仅用于把 Aria 自身状态暴露给 HA，不作为读取全部 HA 实体的主链路；
- 模型永远不能直接传入任意 HA `domain/service` 或请求 URL。

```text
用户文字 / 语音
       │
       ▼
Aria Chat / Voice
       │ Function Calling
       ▼
Home Assistant Tools ──► Confirmation Engine
       │                         │
       └─────────────┬───────────┘
                     ▼
            Home Assistant Bridge
            ├─ REST：执行动作
            ├─ WebSocket：订阅状态
            └─ MQTT：向 HA 暴露 Aria
                     │
                     ▼
              Home Assistant Core
```

## 2. 范围

### 2.1 首版包含

1. 连接一个 HA 实例，支持健康检查、鉴权、状态同步、断线重连；
2. 读取管理员明确授权的灯、开关、风扇、空调、窗帘、媒体播放器和传感器状态；
3. 控制管理员明确授权的灯、开关、风扇、空调、窗帘、媒体播放器和场景；
4. 中文名称、区域、别名和 `entity_id` 的确定性解析；
5. 实体级读权限、动作白名单、隐私等级、风险等级和确认策略；
6. 高风险动作确认、TTL、幂等、结果回读和审计台账；
7. 将稳定 HA 状态变化转换为 Aria 语义事件；
8. Admin 连接状态、实体同步、授权策略和动作台账；
9. 可选 MQTT Discovery，将 Aria 在线状态和 DND 状态暴露给 HA。

### 2.2 首版不包含

- 任意 `shell_command.*`、`command_line.*`、`hassio.*`；
- `homeassistant.restart/stop` 等管理动作；
- 未逐项登记的 `script.*` 和 `automation.*`；
- 动态模板、任意代码或任意 HA 服务调用；
- 摄像头持续监控、麦克风监听和原始健康数据持久化；
- 由 HA 事件直接触发新的设备控制；
- 用 Aria 替代 HA Automation 成为第二套通用自动化引擎；
- 多 HA 实例同时路由。数据模型预留 `instance_id`，实现先支持一个启用实例。

## 3. 通信方案

### 3.1 WebSocket 状态链路

连接 `ws(s)://<ha-host>/api/websocket`：

1. 等待 HA 发送 `auth_required`；
2. 发送 `{"type":"auth","access_token":"..."}`；
3. 收到 `auth_ok` 后获取初始状态；
4. 订阅 `state_changed`；
5. 立即丢弃未授权实体事件；
6. 对授权实体更新内存快照并进入去抖/语义事件管线；
7. 心跳失败或连接断开后按 1、2、4、8、16、30 秒上限退避重连；
8. 重连成功后重新做全量状态同步，但同步流量不得触发主动提醒。

Bridge 只能缓存管理员授权实体。HA 原始属性可能包含位置、图片 URL、人员、设备标识等敏感数据，进入缓存前必须按实体 domain 和字段白名单裁剪，完整 WebSocket payload 不得写日志。

### 3.2 REST 动作链路

动作通过以下接口执行：

```text
POST /api/services/<domain>/<service>
Authorization: Bearer <token>
Content-Type: application/json
```

模型只产生受控语义动作。例如：

```json
{
  "target": "客厅主灯",
  "action": "set_brightness",
  "parameters": {"brightness_pct": 40}
}
```

服务端完成目标解析、授权检查、参数校验和风险判断后，才映射为：

```json
{
  "domain": "light",
  "service": "turn_on",
  "service_data": {
    "entity_id": "light.living_room_main",
    "brightness_pct": 40
  }
}
```

动作调用返回后必须回读目标状态。HTTP 超时不能直接重试写操作，应先回读状态；无法判断结果时记为 `unknown_outcome`。

### 3.3 历史与设备日志

- 连续状态使用 `GET /api/history/period/<start>` 读取 Recorder 历史，如温度、湿度和 PM2.5；
- 人工可读事件使用 `GET /api/logbook/<start>`，如灯的开关记录；
- 后台按实体配置 `history_allowed` 和 `history_max_hours`，默认拒绝；
- 当前实例真实验收：客厅灯 History/Logbook 有记录，家庭温度、湿度和水浸告警 History 可读。连续数值传感器的 Logbook 可以为空，以 History 为准。

### 3.4 MQTT 的职责

HA 控制和状态读取不依赖 MQTT。MQTT 只承担：

- Aria Hub 在线/离线；
- Aria DND 状态和可选开关；
- Aria 主动感知健康状态；
- 将来 ESP32 等原生 MQTT 设备接入。

建议主题：

```text
homeassistant/sensor/aria_hub_status/config
homeassistant/switch/aria_dnd/config
aria/hub/availability
aria/hub/status
aria/hub/dnd/state
aria/hub/dnd/set
```

Discovery 配置包含稳定 `unique_id`、device、origin 和 availability；HA Birth 后重发配置和状态。不得把 `hub/devices/#` 原样桥接到 `homeassistant/#`。

## 4. 配置模型

新增顶层 `integrations`，避免把外部系统连接生命周期混入地图 Query Tools：

```yaml
integrations:
  home_assistant:
    enabled: true
    instance_id: home-main
    base_url: https://ha.pnkx.top:8
    # 连接地址、令牌、运行参数和实体白名单由管理后台保存；
    # YAML 仅保留首次启动的未启用默认值。
    verify_tls: true
    allow_insecure_local_http: false

    connect_timeout_ms: 5000
    request_timeout_ms: 8000
    reconnect_min_seconds: 1
    reconnect_max_seconds: 30
    state_cache_ttl_seconds: 300

    event_subscription:
      enabled: true
      debounce_ms: 1000
      unavailable_grace_seconds: 30

    controls:
      max_targets_per_action: 8
      confirmation_ttl_seconds: 60
      default_policy: deny

    mqtt_discovery:
      enabled: false
      discovery_prefix: homeassistant
      topic_prefix: aria
```

约束：

- token 默认通过后台密码框写入受管理员鉴权保护的数据库配置版本；仍兼容 `secret_ref` 环境变量引用；
- token 在界面中按密码字段遮蔽，不写日志、不进入聊天前端；
- 生产使用 HTTPS 或受控 VPN；本地明文 HTTP 必须显式开启且只允许私网地址；
- 后台保存后立即重连，无需重启 Hub；
- token 轮换后重新鉴权，不要求重启整个 Hub。

### 4.1 当前目标实例

本项目已有可用 HA 服务，首个集成目标固定为：

```text
https://ha.pnkx.top:8
```

2026-08-24 从当前开发环境完成只读探测：

- 根路径返回 HTTP 200 和 `text/html`；
- `/api/` 在未携带 token 时返回 HTTP 401，符合 HA API 鉴权预期；
- 公网 DNS 可解析且 TLS 主机校验通过；
- 当前证书 CN 为 `*.pnkx.top`，由 Let's Encrypt 签发；
- 当前探测到的证书有效期截止 2026-10-15，部署方应确认自动续期并增加证书到期监控。

因此不再部署新的 HA 容器，Aria Bridge 直接连接该实例。配置中的 `base_url` 去掉末尾 `/`，REST 路径由 client 统一拼接；WebSocket 地址由 client 转换为 `wss://ha.pnkx.top:8/api/websocket`。

该地址是公网入口，落地时额外要求：

- HA token 通过后台配置中心或部署环境密钥注入，不能提交到仓库；
- 使用专门为 Aria 创建的 HA 用户和独立 token，不复用日常管理员 token；
- 反向代理保留 WebSocket Upgrade，并对登录/API 做速率限制；
- 可行时通过 VPN、源 IP 白名单或访问控制层限制入口；
- Aria 发出的日志不得包含 URL query、Authorization header 或响应正文；
- 首次带 token 联调只执行 `GET /api/`、状态读取和订阅，写服务在实体白名单完成前保持关闭。

### 4.2 现有工具配置重构

当前 `ToolsConfig` 将总工具开关和高德 provider 强绑定。实现 HA 前应改为各域独立启停：

```yaml
tools:
  max_tool_rounds: 1
  query:
    enabled: true
    weather_enabled: true
    nearby_enabled: true
    route_enabled: true
  home_assistant:
    enabled: true
```

开启 HA 不得要求高德已配置。旧配置继续兼容，首次保存时迁移为新结构。

## 5. 代码结构与接入点

新增：

```text
server/app/home_assistant/
├── __init__.py
├── client.py
├── models.py
├── bridge.py
├── policy.py
├── resolver.py
├── tools.py
├── events.py
├── confirmations.py
└── capabilities.py

server/app/api/admin_home_assistant.py
server/alembic/versions/0014_home_assistant_bridge.py
```

职责：

| 文件 | 职责 |
|---|---|
| `client.py` | HA REST/WebSocket 协议、鉴权、请求 ID、错误映射 |
| `bridge.py` | 生命周期、健康、重连、状态缓存、初始同步 |
| `policy.py` | 实体授权、动作映射、风险与隐私策略 |
| `resolver.py` | alias/区域/名称/entity_id 解析和歧义返回 |
| `tools.py` | 安全的 LLM ToolHandler |
| `events.py` | 状态变化去抖和语义事件转换 |
| `confirmations.py` | 待确认动作、一次性确认、过期和幂等 |
| `capabilities.py` | HA 实体生成当前真实可用 Runtime Capability |

现有代码接入：

1. `server/app/config/models.py` 增加 HA 配置模型并解除 Tools/高德强绑定；
2. `server/app/main.py` 在 lifespan 启停 Bridge，注册 Admin Router 和工具；
3. `server/app/tools/intent.py` 增加家居意图选择；
4. `server/app/chat/service.py` 按工具隐私能力注入，不能继续把所有设备工具限制在 L2；
5. 新建 `CompositeRuntimeCapabilityProvider`，合并 Device Registry 和 HA 能力；
6. 复用现有 `ToolExecutor`，但动作授权和确认必须在 HA 工具内部再次强制检查；
7. Bridge 健康状态加入 `/healthz`，HA 故障只使集成 degraded，不拖垮聊天服务。

## 6. 数据模型

### 6.1 `home_assistant_entity_policy`

| 字段 | 类型/约束 | 含义 |
|---|---|---|
| `id` | UUIDv7 PK | 策略 ID |
| `user_id` | UUID FK | 所属用户 |
| `instance_id` | string | HA 实例 |
| `entity_id` | string | HA 实体 ID |
| `display_name` | string | Aria 显示名 |
| `aliases` | JSON array | 中文别名 |
| `area_name` | string nullable | 所属区域 |
| `domain` | string | light/switch/... |
| `read_allowed` | boolean | 是否允许读取 |
| `allowed_actions` | JSON array | 允许语义动作 |
| `privacy_level` | L0～L3 | 服务端隐私基线 |
| `risk_level` | R0～R4 | 动作风险 |
| `confirmation_policy` | never/first_time/always | 确认策略 |
| `enabled` | boolean | 总开关 |
| `created_at/updated_at` | timestamp | 审计时间 |

唯一约束：`(user_id, instance_id, entity_id)`。同步发现新实体时默认 `enabled=false`、`read_allowed=false`、无允许动作。

### 6.2 `home_assistant_action_execution`

| 字段 | 含义 |
|---|---|
| `id` | 动作请求 ID |
| `user_id/conversation_id/turn_id` | 请求归属 |
| `instance_id/entity_id` | 执行目标 |
| `semantic_action` | 受控动作枚举 |
| `arguments_redacted` | 脱敏参数 |
| `request_hash` | 完整规范化请求摘要 |
| `idempotency_key` | 幂等键 |
| `risk_level` | R0～R4 |
| `status` | 动作状态 |
| `confirmation_expires_at` | 确认过期时间 |
| `confirmed_at/executed_at` | 生命周期时间 |
| `result_code` | 稳定结果码 |

状态机：

```text
planned ──► awaiting_confirmation ──► confirmed ──► executing
   │                 │                                  │
   │                 ├─► rejected                       ├─► succeeded
   │                 └─► expired                        ├─► failed
   └────────────────────────────────────────────────────└─► unknown_outcome
```

不持久化 HA 全量状态。当前状态只保存在有界内存缓存；需要长期保留的事实必须先转换为允许持久化的 Aria 语义事件。

## 7. LLM 工具

### 7.1 `home_get_state`

```json
{
  "targets": ["客厅主灯", "卧室温度"],
  "attributes": ["state", "brightness", "temperature"]
}
```

- 最多 10 个目标；
- 只返回字段白名单；
- 目标歧义时返回候选，不自动猜测；
- 状态过期或 HA 离线时明确返回 unavailable；
- L3 原始值不进入模型自由文本链路，优先使用服务端确定性模板回答。

### 7.2 `home_control_device`

```json
{
  "target": "客厅主灯",
  "action": "set_brightness",
  "parameters": {"brightness_pct": 40}
}
```

`action` 是固定枚举，参数使用按 domain/action 区分的 Pydantic 联合模型。单次最多 8 个明确目标，禁止通配符和隐式“全屋”。

### 7.3 `home_activate_scene`

```json
{"scene": "睡眠模式"}
```

场景必须逐项授权和标记风险，不能因为 domain 为 `scene` 就自动视为低风险。

### 7.4 `home_confirm_action`

```json
{"request_id": "uuid"}
```

主要确认路径由聊天确认卡片调用服务端 API；文字/语音确认作为辅助路径。确认必须绑定当前用户、会话和原始参数。模型编造或复用 request ID 不能越权。

`home_list_entities` 只供 Admin 使用，不注入模型上下文。

### 7.5 工具选择

新增 `select_home_tools(text, capabilities)`，依据意图词和当前真实能力选择最小工具集。工具是否可见取决于：

```text
HA 在线
∩ 实体存在且可用
∩ 属于当前用户
∩ 管理员已启用
∩ 动作在白名单
∩ 当前隐私路由允许
```

普通灯光和开关工具允许 L0/L1；精确位置、摄像头、卧室存在等维持 L2/L3。不能继续使用“所有设备工具仅在 L2 可见”的统一条件。

## 8. 动作映射与风险

允许的首版映射：

| Domain | 语义动作 | HA Service |
|---|---|---|
| `light` | turn_on/turn_off/set_brightness/set_color_temp | `light.turn_on/turn_off` |
| `switch` | turn_on/turn_off | `switch.turn_on/turn_off` |
| `fan` | turn_on/turn_off/set_percentage | `fan.turn_on/turn_off/set_percentage` |
| `climate` | turn_on/turn_off/set_temperature/set_hvac_mode | 对应 `climate.*` |
| `cover` | open/close/stop/set_position | 对应 `cover.*` |
| `media_player` | play/pause/volume_set | 对应 `media_player.*` |
| `scene` | activate | `scene.turn_on` |

风险基线：

| 风险 | 示例 | 默认策略 |
|---|---|---|
| R0 | 授权状态读取 | 直接执行 |
| R1 | 灯、普通开关、媒体播放 | 显式授权后直接执行 |
| R2 | 空调、窗帘、风扇、普通场景 | first_time 或 always |
| R3 | 门锁、车库门、报警、阀门 | 每次确认；首版可整体关闭 |
| R4 | HA 管理、Shell、任意脚本 | 永久拒绝 |

门锁、报警和阀门若在首版启用，只能走 R3 每次确认，且需要 Admin 单独打开 feature flag。`toggle` 默认不开放，因为重试和状态过期时不容易保证期望终态；优先使用明确的 `turn_on/turn_off`。

## 9. 确认、幂等和对账

1. 模型产生语义动作候选；
2. 服务端解析目标并验证策略；
3. 规范化参数并计算 `request_hash`；
4. 生成与 user/conversation/target/action 绑定的幂等键；
5. 需要确认时仅写 `awaiting_confirmation`，不调用 HA；
6. 确认卡片展示目标、动作和关键参数；
7. 确认在 60 秒后失效且只能消费一次；
8. 执行前重新验证连接、实体可用性和策略版本；
9. 执行后回读状态并判定是否达到期望终态；
10. HTTP 超时先回读，不能直接重复写；仍无法确定则进入 `unknown_outcome`；
11. 取消、拒绝、过期和失败都返回稳定 reason code。

任何确认都不能扩大原请求：例如用户确认“客厅灯亮度 40%”不能被复用为“全屋灯 100%”。

## 10. 入站事件与主动感知

### 10.1 事件处理流水线

```text
HA state_changed
  → 实体授权过滤
  → 属性白名单与服务端隐私重分类
  → 内存快照
  → 去抖/阈值/恢复同步抑制
  → EphemeralSignal 或 InputEnvelope
  → Perception 语义事件
  → ProactiveEngine 策略
  → 允许时输出提醒
```

推荐映射：

| HA 变化 | Aria 事件 |
|---|---|
| `person.* → home` | `user_arrived_home` |
| `person.* → not_home` | `user_left_home` |
| 授权门窗持续打开 | `home.entry_open_too_long` |
| 授权实体 unavailable 超过宽限期 | `device.offline` |
| 人体存在稳定变化 | `presence.changed` |
| 温湿度越过配置阈值 | `environment.threshold_crossed` |

禁止将每次 `state_changed` 都持久化。数值抖动不产生 durable event；HA 或 Hub 重启后的初始同步不产生主动提醒。

### 10.2 主动控制边界

首版 HA 事件只能触发提醒，不能触发新的设备写操作。用户希望“回家后开灯”等确定性规则时，优先创建或指导创建 HA Automation。以后如允许 Aria 创建规则，必须使用单独的规则草稿、预览、确认、发布和回滚流程。

## 11. 隐私

沿用现有 L0～L3：

| 数据 | 建议级别 | 处理 |
|---|---|---|
| 灯光、普通开关 | L0 | 可用于普通对话 |
| 环境聚合、设备在线 | L0/L1 | 可转语义事件 |
| 人员在家状态 | L0～L2，由管理员配置 | 最小化持久化 |
| 精确位置、门锁历史 | L2 | 本地模型和严格审计 |
| 摄像头、麦克风、卧室原始存在 | L3 | 不落库、不进日志 |
| 健康和高频原始遥测 | L3 | 仅临时处理 |

强制规则：

- 上游 HA 分类只是输入，服务端策略可以上调，不能由消息自行下调；
- L3 不写通用 event、Timeline、日志、备份或动作参数；
- L3 不发送云端 LLM；
- L3 尽量由服务端确定性格式化，不进入模型自由文本上下文；
- 快照按 TTL 删除；
- Admin 页面默认不显示敏感原值，只显示通道健康和最近更新时间；
- token、Authorization header、完整 HA payload 和精确位置永不记录。

## 12. Admin 与 API

Admin 新增“Home Assistant”工作区：

- 连接状态、HA 版本、最近同步和最近错误 reason code；
- 测试连接、重新同步和安全断开；
- 按区域/domain 筛选实体；
- 开启/关闭实体，编辑显示名和别名；
- 配置读权限、动作白名单、隐私、风险和确认策略；
- 最近 200 条脱敏动作台账；
- 待确认动作；
- token 轮换说明，但不回显 token。

API：

```text
GET    /api/v1/admin/home-assistant/status
POST   /api/v1/admin/home-assistant/test
POST   /api/v1/admin/home-assistant/sync
GET    /api/v1/admin/home-assistant/entities
PATCH  /api/v1/admin/home-assistant/entities/{entity_id}/policy
GET    /api/v1/admin/home-assistant/actions
POST   /api/v1/home-assistant/actions/{id}/confirm
POST   /api/v1/home-assistant/actions/{id}/reject
```

修改实体策略和确认 R3 动作要求有效用户会话；修改全局集成配置要求 Admin 权限。跨用户访问统一返回 not found，避免泄露实体存在性。

## 13. MQTT 部署安全

当前开发 Mosquitto 只有共享账号和密码文件，生产接入前必须升级：

- `aria_hub` 独立账号；
- `homeassistant` 独立账号；
- 每个 ESP32/设备独立账号；
- topic ACL；
- Docker secret 或受限文件注入密码；
- TLS 或仅允许受控 VPN/私网；
- 禁止匿名；
- 为 Home Assistant 使用唯一 MQTT client ID；
- Discovery/状态主题和原始设备遥测主题严格分离。

最小 ACL 意图：

```text
aria_hub:
  read  homeassistant/status
  write homeassistant/sensor/aria_+/config
  write homeassistant/switch/aria_+/config
  readwrite aria/hub/#

homeassistant:
  read  homeassistant/#
  readwrite aria/hub/#
```

实际 ACL 文件不得使用过宽 `#` 代替已知前缀，并通过 Compose smoke 证明匿名、越权 topic 和错误凭据均被拒绝。

## 14. 健康、错误和可观测性

Bridge 健康状态：

```text
disabled / connecting / ready / degraded / auth_failed / stopped
```

稳定 reason code 至少包含：

```text
ha_disabled
ha_offline
ha_auth_failed
ha_timeout
ha_entity_not_found
ha_target_ambiguous
ha_entity_unavailable
ha_read_denied
ha_action_denied
ha_confirmation_required
ha_confirmation_expired
ha_confirmation_rejected
ha_policy_changed
ha_invalid_arguments
ha_unknown_outcome
```

指标：

- WebSocket 在线状态和重连次数；
- 授权实体数和状态缓存命中；
- REST 延迟、错误率、unknown outcome；
- 动作按 risk/status 计数；
- 事件过滤、去抖和语义事件计数；
- 主动消息被 DND/冷却/上限拦截计数。

日志仅包含 request ID、entity ID 的不可逆摘要、动作枚举、风险、结果和延迟，不记录 token、原始敏感状态或自由文本参数。

## 15. 测试方案

### 15.1 单元测试

1. WebSocket auth、消息 ID、订阅和重连；
2. REST 服务调用与 HA 错误映射；
3. 名称/别名/区域解析和歧义；
4. 默认拒绝及实体/动作白名单；
5. domain/action 参数联合校验；
6. R0～R4 风险矩阵；
7. 确认过期、拒绝、重复消费、跨用户和参数篡改；
8. 幂等与 unknown outcome 对账；
9. 初始同步不触发主动消息；
10. 高频状态去抖；
11. L2/L3 不错误出站和落库；
12. token、Authorization 和敏感状态不进入日志。

### 15.2 集成测试

使用固定 HA 版本的测试容器，不能使用未固定的 `latest`。准备模拟灯、开关、温度、窗帘和高风险实体，覆盖：

- 首次连接和状态同步；
- 状态查询；
- 灯光控制和状态回读；
- Bridge/HA 分别重启；
- 401、超时、断网和恢复；
- 动作超时后的对账；
- MQTT Discovery 和 HA Birth 重发；
- 旧配置迁移；
- Adapter/Bridge 故障不影响普通聊天。

新增质量入口：

```text
make ha-check
```

该入口至少包含 HA 单元测试、类型检查、集成测试和敏感日志扫描。

## 16. 验收标准

- 未授权状态读取成功数为 0；
- 未授权动作成功数为 0；
- 重复设备动作数为 0；
- L3 通用落库数为 0；
- L3 云端出站数为 0；
- R3 未确认执行数为 0；
- HA 断线后网络恢复时 30 秒内重新连接；
- 普通状态查询暖态 P95 小于 1 秒；
- 普通设备控制暖态 P95 小于 2 秒；
- 动作执行后能验证期望终态或明确标记 unknown outcome；
- DND 主动提醒违规数为 0；
- HA/Bridge 重启不会产生伪到家、伪离家或批量提醒；
- 通过现有后端/前端质量闸门和新增 `make ha-check`；
- 完成至少 7 天单家庭真机验收。

## 17. 实施顺序

### Phase HA-0：基础重构

1. Tools/高德配置解耦；
2. 工具按自身隐私能力注入；
3. Composite Runtime Capability Provider；
4. 配置迁移和回归测试。

完成条件：现有地图、屏幕、网页工具回归不退化。

### Phase HA-1：读取闭环

1. HA Client 和 Bridge；
2. 健康、鉴权、初始同步、订阅和重连；
3. 内存快照、字段白名单；
4. 实体同步、默认拒绝、Admin 读权限；
5. `home_get_state`。

完成条件：可稳定回答授权实体当前状态，HA 离线时不猜测。

### Phase HA-2：低风险控制

1. resolver；
2. `home_control_device`；
3. light/switch/fan/media_player 映射；
4. 幂等台账、执行后回读和 unknown outcome；
5. Admin 动作授权。

完成条件：可控制灯和开关，歧义、离线、未授权都稳定拒绝。

### Phase HA-3：确认与扩展设备

1. 确认状态机和聊天确认卡；
2. climate/cover/scene；
3. R3 feature flag 和每次确认；
4. 策略变更后重新验证。

完成条件：风险矩阵、过期、重复和跨用户测试全部通过。

### Phase HA-4：主动感知与 MQTT

1. 入站状态去抖和语义事件；
2. DND、冷却、每日上限；
3. MQTT 独立凭据和 ACL；
4. Aria MQTT Discovery；
5. 7 天真机验收。

完成条件：主动提醒零 DND 违规，无原始 L3 持久化，无重启伪事件。

## 18. 工程量参考

- HA-0～HA-1：约 5～7 个工程日，可交付安全只读闭环；
- HA-2～HA-3：约 5～7 个工程日，可交付受控设备控制和确认；
- HA-4：约 2～4 个工程日加 7 天观察窗口；
- 完整生产版本合计约 12～18 个工程日，不含观察窗口和 HA 侧设备排障。

工程量是用于排期的参考，不替代每个 Phase 的验收门槛。

## 19. 官方协议参考

- Home Assistant WebSocket API：<https://developers.home-assistant.io/docs/api/websocket/>
- Home Assistant REST API：<https://developers.home-assistant.io/docs/api/rest/>
- Home Assistant Authentication API：<https://developers.home-assistant.io/docs/auth_api/>
- Home Assistant MQTT Integration：<https://www.home-assistant.io/integrations/mqtt>
