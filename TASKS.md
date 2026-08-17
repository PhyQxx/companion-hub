# Aria 开发任务清单

> 最后更新：2026-08-17
> 当前阶段：P1 · 结构化回复与 Persona

## 进度概览

| 阶段 | 状态 | 目标 |
|---|---|---|
| P0 Open-LLM-VTuber 集成验证 | 已完成 | 通过自定义 Agent 接入 Aria 实时聊天 |
| P1 结构化回复与 Persona | 待开始 | 统一文本、情绪、语音和动作输出 |
| P2 记忆系统 v1 | 待开始 | 可检索、可溯源、可纠错的长期记忆 |
| P3 删除闭环与记忆后台 | 待开始 | 跨存储删除与可视化管理 |
| P4 正式前端决策 | 待开始 | 决定 Vue 3 与 Open-LLM-VTuber 的边界 |
| P5 文字稳定性闸门 | 待开始 | 连续 14 天真实使用验证 |
| P6 语音与 Live2D 产品化 | 待开始 | 完整语音、打断、表情与桌宠体验 |

## 当前批次：P0

### 正在进行

- [x] 核对 Open-LLM-VTuber v1 Agent 抽象、输入输出类型和生命周期。
- [x] 核对 Open-LLM-VTuber 配置注册方式和 WebSocket 中断语义。

### 接下来

- [x] 设计 Aria 与 Open-LLM-VTuber 之间的协议映射。
- [x] 实现独立 `AriaAgent` 适配器。
- [x] 支持 Aria 聊天身份登录和 Token 生命周期。
- [x] 支持文字输入与流式文字回复。
- [x] 支持 Open-LLM-VTuber interrupt 向 Aria generation cancel 传播。
- [x] 保证模型密钥仅保留在 Aria 服务端。
- [x] 增加单元测试、配置示例和运行文档。
- [x] 运行本地端到端验证。
- [x] 提交 GitHub PR，CI 与 compose smoke 全部通过。

### P0 验收条件

- [x] Open-LLM-VTuber 与 Aria 可以独立启动和升级。
- [x] 文字输入经 Aria `/ws/chat` 获得真实流式回复。
- [x] 回复可进入 Open-LLM-VTuber 的后续 TTS/Live2D 管线。
- [x] 取消生成不会提交迟到的助手消息。
- [x] 会话和消息仍由 Aria PostgreSQL 持久化。
- [x] 模型选择仍由 Aria 数据库配置中心控制。
- [x] Open-LLM-VTuber 不持有模型供应商 API Key。

## 已完成

- [x] M0 可扩展输入输出协议与 Adapter Registry。
- [x] 可靠 event/outbox/inbox、重试和故障恢复基础。
- [x] L0～L3 隐私分类、L3 非持久化和模型出站闸门。
- [x] 商汤、GLM 预留和本地模型的数据库配置中心。
- [x] 本地聊天身份、登录限流、会话撤销和资源归属。
- [x] PostgreSQL 会话与消息持久化。
- [x] WebSocket 真实流式回复、取消和消息补拉。
- [x] 模型配置后台与文字聊天调试台。
- [x] 调试台固定视口、回车发送和消息自动贴底。
- [x] Open-LLM-VTuber v1.2.1 macOS 源码安装及真实 HTTP/WebSocket、Live2D、ASR、TTS、Aria PostgreSQL 端到端验证。

## 暂缓事项

- Vue 3 全量前端重写：等待 P0/P4 决策。
- Open-LLM-VTuber v1 深度 fork：上游正在规划 v2 全面重写。
- 3D/VRM、静态图片动态化和多端同步：不阻塞核心 v1。
- ESP32 与存在传感器：文字和语音稳定后进入 M3A。
