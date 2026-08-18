# P4a Vue 聊天前端验收记录

> 日期：2026-08-18
> 范围：`web/` monorepo 脚手架、`packages/shared` API/WS 客户端、`apps/chat` 正式聊天前端
> 关联：ADR-018（docs/26）；TASKS.md P4a

## 交付内容

### monorepo 脚手架（`web/`）

- pnpm workspace：`apps/*` + `packages/*`；构建 `pnpm --dir web run build`，
  类型检查 `pnpm --dir web run typecheck`（vue-tsc，当前全绿）。

### `packages/shared`

- REST 客户端 `ChatApi`：auth status/setup/login/me/logout、会话列表/创建/删除、消息拉取；
- WebSocket 客户端 `ChatSocket`：authenticate 首帧、`message.send`/`turn.cancel`/`sync.request`、
  事件回调（reply.delta / reply.control / committed / cancelled / failed / sync）；
- 协议与领域类型（Conversation/ChatMessage/AgentReplyControl/SocketEvent 等），
  供 chat 与后续 admin 应用共用。

### `apps/chat`（Vue 3 + Vite + TS）

- 登录/首次设置（`setup_required` 自动切换表单，管理 Token 仅首次使用）；
- 会话侧栏：新建、切换、删除（确认提示含"记忆一并删除"）；`token` 持久化于 localStorage；
- 聊天区：流式 delta 渲染、`reply.control` 情绪标签、已提交消息回放（含历史情绪）；
- 发送区：隐私等级选择（L0/L1/L2）、Enter 发送、生成中"停止"（turn.cancel）；
- WS 断线自动重连（最多 3 次），失败时降级提示；`/chat/debug` 入口保留。

### 托管与构建

- FastAPI：`web/apps/chat/dist` 存在时 `/chat` 服务 Vue 构建产物并挂载 `/chat/assets`；
  未构建的源码环境自动回退旧调试页；旧页固定在 `/chat/debug`（资源路径已迁移）。
- Dockerfile 增加 `node:20` 构建阶段（corepack + pnpm build），dist 拷入最终镜像；
  `.dockerignore` 不再排除 `web/`（仍排除 node_modules/dist）。

## 验证

- `vue-tsc` 类型检查与 Vite 构建通过（产物 ~77KB JS / gzip 30.5KB）；
- 本地 uvicorn 冒烟：`/chat`（Vue）、`/chat/debug`、`/chat/assets/*`、
  `/chat/debug/assets/*` 全部 200；
- 后端回归 107 通过 / ruff / mypy 全绿；页面测试兼容"有构建产物/纯源码"两种环境。

## 边界与后续（P4b）

- admin 后台仍为 vanilla 页面，迁移到 `web/apps/admin` 属 P4b；
- 移动端布局为基本响应式，P5 期间按真实使用反馈打磨；
- 桌面壳（Tauri）与形象渲染归 P6（经 Open-LLM-VTuber 桥接）。
