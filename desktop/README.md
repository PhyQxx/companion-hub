# Aria Desktop Client

Tauri 2 受控设备客户端。除低风险 `device.ping` 外，macOS 只有在系统屏幕录制权限已授予、未锁屏、隐私暂停关闭且用户临时授权下一次截图时才声明 `screen.capture`；截图支持活动窗口、主显示器与显式编号的显示器。

## 已实现

- 使用 Admin 生成的一次性配对码注册桌面设备；
- 长期设备令牌及其 Hub URL 绑定保存到操作系统安全凭据库，非敏感设备显示信息保存到本机 `localStorage`；截图上传不信任 WebView 传入的目标地址；
- `/ws/devices` 鉴权、30 秒心跳、指数退避重连和手动隐私暂停；
- 每 5 秒读取 macOS 会话锁定状态；锁屏时撤销临时授权、移除 `screen.capture` 声明，并在原生截图执行前再次拒绝；
- “允许下一次截图”授权只存在于当前进程内存，5 分钟过期、仅消费一次，不写入 `localStorage` 或系统凭据库；
- 校验 Hub 发出的 HMAC-SHA256 签名帧，支持 TTL、取消和进程内幂等；
- 托盘显示/退出、关闭窗口时隐藏、可选开机启动；
- macOS 单次活动窗口、主显示器或显式编号显示器截图；原图直接上传到 Hub 的短 TTL 临时资产通道，本机临时文件由 RAII 清理；
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

2026-08-24 已在 Apple Silicon macOS 上使用 Rust 1.98 与 Command Line Tools 完成 debug `.app` 原生编译、系统钥匙串、配对、TCC 授权及真实主显示器截图上传验收。

## 配对

1. 在 Admin「设备」页生成配对码，并授权 `device.ping`；需要截图时同时授权 `screen.capture`。
2. 在客户端填写 Hub URL、一次性配对码、设备名和可选别名。
3. 配对成功后客户端自动连接；需要截图时点击屏幕录制权限按钮，并在 macOS 系统设置中允许。
4. 在 Desktop 点击“允许下一次截图”；授权在 5 分钟内只允许一条有效截图命令。
5. Admin 中设备只有在系统权限已授予、未锁屏、隐私暂停关闭且临时授权有效时才会实时声明 `screen.capture`。

忘记设备只清除本机凭据。若需要使旧令牌立即失效，还要在 Admin 中撤销对应设备。
