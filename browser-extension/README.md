# Aria Browser Bridge

Chrome 116+ Manifest V3 扩展。配对后通过设备 WebSocket 仅声明并执行：

- `browser.current_tab.read`：返回当前 HTTP(S) 标签页的标题、origin、语言和截断后的可见文本；
- `browser.current_tab.capture`：返回当前可见标签页 PNG。

页面正文和截图直接上传到 Hub 的短时内存资产通道，不写入扩展存储或命令回执。浏览器内部页、扩展页和商店页拒绝执行。配对时会由浏览器显式请求网页访问权限。

```bash
pnpm install --frozen-lockfile
pnpm test
pnpm build
```

在 `chrome://extensions` 开启开发者模式，选择“加载已解压的扩展程序”，目录为 `browser-extension/dist`。首次打开扩展先授予网页权限，再重新打开弹窗填写 Hub 与配对码。Hub Admin 创建配对码时需授权上述两项 capability。
