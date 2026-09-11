# 44 - 家庭守护 SAFE-01/02 设计方案

> 状态：v1 设计定稿；S1 已实现（2026-09-11），S2～S4 待实施
> 相关：[39-个人管家能力路线图](./39-个人管家能力路线图.md)、[36-Home Assistant集成设计](./36-Home%20Assistant集成设计.md)、[31-记忆时间线与历史回溯设计](./31-记忆时间线与历史回溯设计.md)

## 1. 功能定位

J8 家庭安全守护的最小闭环，分两批：

- **SAFE-01 环境异常**：漏水、烟雾、门窗久开、设备异常、久未活动五类信号的检测与**分级告警**；验收标准"告警包含来源、时间、置信度和建议"。
- **SAFE-02 紧急升级**：本地提醒 → 手机通知（锁屏推送）→ **预授权紧急联系人**的三级升级链；验收红线"第三方联系必须预先配置并全程审计"。

**已有地基（不重复建设）**：

| 已有 | 位置 | SAFE 复用方式 |
|---|---|---|
| 实体阈值规则（漏水/温湿度/PM2.5/灯久亮/设备离线，持续窗+冷却） | `home_assistant/proactive.py` + Admin「HA 实体授权」页 | SAFE-01 直接扩展规则种类 |
| 主动投递通道（Web 私聊/桌面通知/Web Push 锁屏/语音，优先级+隐私上限仲裁） | `ProactiveDeliveryService` | SAFE-02 分级投递的执行层 |
| 联系人（关系/偏好/时区） | `CONTACT-01` contact 表 | 紧急联系人与**授权偏好**载体 |
| 邮件发送（服务端确认后发送） | `MAIL-01` + FIX-01 确认闭环 | 升级到联系人时的外发通道 |
| 感知→认知决策（salience/DND/预算） | Perception + CognitiveCycle | 告警仍过认知闸门，不直接轰炸 |

## 2. 架构决策

1. **分级是规则属性，不是新引擎**：在现有 HA proactive 规则上加 `severity: notice|warning|critical`；critical 才进入升级链。避免再造一套调度器。
2. **升级链是状态机，不是单次投递**：`safety_alert` 记录告警实体级别推进（local→push→contact），每级有确认窗口（默认 5 分钟）；用户任意通道的回应（聊天消息/通知点击回执）即解除。重启后未完成的告警从 DB 恢复继续计时。
3. **第三方联系 = 预授权 + 双重确认 + 全程审计**：
   - 预授权：联系人的 `preferences` 里显式标记 `emergency_contact=true`，且用户在 Admin/聊天里单独确认过一次"允许危急时邮件通知 TA"（授权记录含联系人、通道、时间，可随时撤销）；
   - 触发时不需要用户当场确认（危急场景等不了），但**发送前**在聊天/桌面同时告知用户"正在升级通知 XX"；发送动作、内容、结果全量写 `safety_alert_escalation` 台账 + Timeline；
   - v1 第三方通道只有邮件（复用 MAIL-01）；短信/电话不做。
4. **久未活动检测是新的 Perception 信号**：以 HA person/设备在线/最后交互（聊天、语音、桌面心跳）组合成"最后活动时间"；超阈值（默认 12h，可配）产生 `user.inactive` 语义事件，走 cognition 判定是否提醒（避免睡觉误报：生效时段默认 09:00–22:00）。
5. **置信度是证据组合**：告警附 `confidence`（规则命中=0.6；多传感器同现=0.85；HA 实体属性里的 battery/availability 异常降权），建议文案由确定性模板生成（含来源实体、持续时长、建议动作），不经 LLM——告警链路必须确定性可解释。

## 3. 数据流

```text
HA 状态变化 / 设备离线 / 活动信号
  → HomeAssistantProactiveEngine（现有，扩展规则种类 + severity）
  → SafetyAlertService.handle(rule, entity, state)
      ├─ notice/warning → ProactiveDeliveryService（现有通道，含锁屏推送）
      └─ critical → safety_alert 状态机
            L1 本地通道（Web/桌面/语音，立即）
            L2 手机推送（3 分钟无确认）+ 重复提醒一次
            L3 预授权联系人邮件（再 5 分钟无确认；发送前后全程审计）
  → 用户任一通道回应 → alert ack → 升级链终止
  → Timeline：safety.alert_raised / safety.alert_escalated / safety.alert_acked
```

## 4. 分步改动清单

### 4.1 配置（`config/models.py`）

```python
class SafetyConfig(StrictModel):
    enabled: bool = False
    escalation_enabled: bool = True
    confirm_window_seconds: int = 300      # 每级确认窗口
    push_retry_minutes: int = 3            # L2 重复提醒间隔
    inactivity_hours: float = 12.0         # 久未活动阈值
    inactivity_active_range: str = "09:00-22:00"  # 检测生效时段
```

`HomeAssistantProactiveRule` 增加 `severity` 字段（默认 warning）；新增规则种类：`smoke_detected`、`door_open_too_long`、`user_inactive`（前三者为实体规则，久未活动为全局信号）。

### 4.2 规则扩展（`home_assistant/proactive.py` + models）

- `smoke_detected`：binary_sensor 为 on 即 critical（无持续窗，冷却 30 分钟）；
- `door_open_too_long`：门窗传感器 on 持续超阈值（默认 30 分钟）→ warning；
- 规则命中事件携带 `severity/confidence/evidence`（实体 ID、新旧状态、持续时长）。

### 4.3 告警状态机（新文件 `server/app/safety/`：`service.py` + `store.py` + `models.py`）

- `safety_alert` 表（`0038_safety_alerts` 迁移）：id、user_id、rule_id、entity_id、severity、evidence(JSON)、status（`escalating/acknowledged/expired/escalated_contact`）、当前级别、各级时间戳、ack 来源；
- `SafetyAlertService`：接收 critical 规则 → 建 alert → 按窗口推进级别（asyncio 任务，重启恢复未终态 alert 继续计时）；
- ack 入口：聊天消息（本轮文本任意即视为回应？——**否**，仅当用户点击通知回执或在聊天里确认；v1 用通知点击回执 + 聊天 `safety_ack` 意图（"知道了/已处理"匹配当前活跃告警）；
- L3 邮件：调 MAIL-01 的服务端发送路径（绕过聊天预览确认，但写独立台账）；无预授权联系人或邮件未配置则停在 L2 并告警用户"无法升级"。

### 4.4 久未活动（`safety/activity.py`）

- 活动信号源：HA person 状态变化、聊天/语音回合、桌面设备心跳（复用 device registry last_seen）；
- `ActivityTracker` 内存记录最后活动；`SafetyScheduler`（复用 TASK-01 调度底座，source_ref=safety:inactivity）周期检查，命中且在生效时段 → `user.inactive` 语义事件 → cognition 判定提醒。

### 4.5 预授权与审计

- `contact.preferences` 增加 `emergency_contact`/`emergency_email` 布尔（CONTACT-01 API 扩展）；
- 授权确认：`POST /api/v1/safety/authorizations`（联系人 + 通道 + 用户密码/会话确认）→ `safety_authorization` 表（含撤销）；
- 台账：`safety_alert_escalation`（alert_id、级别、通道、收件人、发送结果、时间）；Timeline 三类事件同步落库。

### 4.6 API 与 UI

- 用户 API `/api/v1/safety/alerts`（活跃/历史/确认）+ `/authorizations`；
- Admin「设备与感知」新增「安全守护」页：总开关、分级窗口配置、活跃告警与升级链可视化、授权管理；
- 聊天端：critical 告警卡片（含来源/时间/置信度/建议 + 确认按钮）。

### 4.7 测试

1. 规则：smoke 即 critical、门窗持续窗、severity 透传与 confidence 组合；
2. 状态机：L1→L2→L3 按窗口推进、ack 任一级终止、重启恢复未终态 alert；
3. 升级：无预授权停在 L2、有授权发送邮件并双台账落库、发送前用户通知；
4. 久未活动：生效时段外不触发、活动刷新重置、cognition 拒绝不打扰；
5. API/UI 回归：授权创建/撤销、告警列表分页（复用分页 envelope）。

## 5. 隐私与安全边界

1. **第三方联系三重闸门**：预授权记录（可撤销）+ 升级窗口内用户无回应 + 全量审计；无授权绝不外发。
2. 告警内容确定性模板生成，不含 L2 内容；联系人邮件正文只含事件摘要与回执链接，不含家庭内部细节。
3. L3 发送前必向用户本人广播"正在升级"，用户可随时一句话中止。
4. 久未活动检测只依赖在场/交互信号，不做摄像头/音频分析。
5. 所有告警事件进 Timeline，用户可像其他记录一样删除（删除闭环沿用）。

## 7. 实施记录

### S1（2026-09-11 已实现）

- 配置：`SafetyConfig`（升级链参数占位，S1 只消费 enabled）；规则新增 `severity`（notice/warning/critical，缺省 warning 兼容存量），新种类 `smoke_detected`（强制 critical）/ `door_open_too_long`；存量水浸规则静默升为 critical 保持免打扰豁免语义。
- 引擎：两类新规则匹配与模板；`_enrich_message` 让 warning+ 告警携带分级标签 + 来源实体/持续时长/置信度/时间（notice 不富化）；`_confidence` 确定性取值（持续窗 0.85 / 瞬时 0.6）；severity 进入 SemanticEvent 属性；静默期豁免从硬编码 water_leak 改为跟随 severity。
- 投递：`ProactiveDeliveryService.deliver(broadcast=True)` 忽略 first_available 短路全通道投递；critical + safety.enabled 时启用。
- Admin：HA 规则行新增 severity 下拉与新种类（supportedRules 按实体域/名称推荐 smoke/door 规则）。
- 回归：规则校验/匹配/富化/置信度/_fire 全链（broadcast 与文案断言）+ 投递广播共 5 项新增；全量 841 通过。

## 6. 工作量

| 阶段 | 内容 | 规模 |
|---|---|---|
| S1 | 规则扩展 + severity + 分级投递（不含联系人） | 约 500 行（含测试），1 天 |
| S2 | 告警状态机 + 重启恢复 + ack + Timeline | 约 600 行，1～1.5 天 |
| S3 | 预授权 + 邮件升级 + 双台账 + Admin/聊天 UI | 约 700 行，1.5 天 |
| S4 | 久未活动 + 真机验收（需 HA 实体配合） | 约 300 行，0.5 天 + 验收 |

顺序 S1 → S2 → S3 → S4；S1+S2 交付后即可在日常 HA 上验证分级告警，S3 是 SAFE-02 的完整闭环。
