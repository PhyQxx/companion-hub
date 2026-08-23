# Aria Desktop Client

Tauri 2 受控设备客户端。除低风险 `device.ping` 外，macOS 在用户授予系统屏幕录制权限且未开启隐私暂停时可声明 `screen.capture`；截图支持主显示器与显式编号的显示器。

## 已实现

- 使用 Admin 生成的一次性配对码注册桌面设备；
- 长期设备令牌及其 Hub URL 绑定保存到操作系统安全凭据库，非敏感设备显示信息保存到本机 `localStorage`；截图上传不信任 WebView 传入的目标地址；
- `/ws/devices` 鉴权、30 秒心跳、指数退避重连和手动隐私暂停；
- 校验 Hub 发出的 HMAC-SHA256 签名帧，支持 TTL、取消和进程内幂等；
- 托盘显示/退出、关闭窗口时隐藏、可选开机启动；
- macOS 单次主显示器截图；原图直接上传到 Hub 的短 TTL 临时资产通道，本机临时文件由 RAII 清理；
- 只执行 `device.ping` 与 `screen.capture`，未知命令明确拒绝。

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

1. 在 Admin「设备」页生成配对码，并授权 `device.ping`；需要截图时同时授权 `screen.capture`。
2. 在客户端填写 Hub URL、一次性配对码、设备名和可选别名。
3. 配对成功后客户端自动连接；需要截图时点击屏幕录制权限按钮，并在 macOS 系统设置中允许。
4. Admin 中设备只有在系统权限已授予且隐私暂停关闭时才会实时声明 `screen.capture`。

忘记设备只清除本机凭据。若需要使旧令牌立即失效，还要在 Admin 中撤销对应设备。
