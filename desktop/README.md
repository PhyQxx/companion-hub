# Aria Desktop Client

Tauri 2 最小设备客户端。当前只声明并执行低风险的 `device.ping`，用于先验证设备配对、独立凭据、长连接和命令回执闭环；屏幕和浏览器能力尚未启用。

## 已实现

- 使用 Admin 生成的一次性配对码注册桌面设备；
- 长期设备令牌只保存到操作系统安全凭据库，非敏感设备信息保存到本机 `localStorage`；
- `/ws/devices` 鉴权、30 秒心跳、指数退避重连和手动隐私暂停；
- 校验 Hub 发出的 HMAC-SHA256 签名帧，支持 TTL、取消和进程内幂等；
- 托盘显示/退出、关闭窗口时隐藏、可选开机启动；
- 只执行 `device.ping`，未知命令明确拒绝。

## 本地检查

```bash
make desktop-check
```

启动原生开发窗口需要 Node 20+、Rust 1.88+ 和对应平台的 Tauri 系统依赖：

```bash
pnpm --dir desktop install
pnpm --dir desktop tauri dev
```

当前机器若没有 Rust/Xcode，只能完成 TypeScript 协议测试与 WebView 生产构建；原生 `cargo check` 和真机托盘/钥匙串验收需在工具链齐备的 macOS 环境补跑。

## 配对

1. 在 Admin「设备」页生成配对码，并授权 `device.ping`。
2. 在客户端填写 Hub URL、一次性配对码、设备名和可选别名。
3. 配对成功后客户端自动连接；Admin 中应显示设备在线，可发送 `device.ping` 测试命令。

忘记设备只清除本机凭据。若需要使旧令牌立即失效，还要在 Admin 中撤销对应设备。
