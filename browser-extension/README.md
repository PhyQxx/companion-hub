# Aria Browser Bridge

Chrome 116+ Manifest V3 扩展。配对后通过设备 WebSocket 声明并执行：

- `browser.current_tab.read`：返回当前 HTTP(S) 标签页的标题、origin、语言和截断后的可见文本；
- `browser.current_tab.capture`：返回当前可见标签页 PNG。

页面正文和截图直接上传到 Hub 的短时内存资产通道，不写入扩展存储或命令回执。浏览器内部页、扩展页和商店页拒绝执行。配对时会由浏览器显式请求网页访问权限。

```bash
pnpm install --frozen-lockfile
pnpm test
pnpm build
```

在 `chrome://extensions` 开启开发者模式，选择“加载已解压的扩展程序”，目录为 `browser-extension/dist`。首次打开扩展先授予网页权限，再重新打开弹窗填写 Hub 与配对码。Hub Admin 创建配对码时需授权上述两项 capability。

## 表单工作流（FIX-02）

支持 `browser.tab.open`、`browser.form.read`、`browser.form.fill`、`browser.form.submit`，分别需要既有动作授权。`browser.form.snapshot_v1` 只声明协议支持，不是可执行动作，不扩大权限。

1. 先执行读取计划，从命令回执或工具 `data.result` 取得 `snapshot_id` 和字段 `ref/formRef`。
2. 在 5 分钟内携带该 ID 创建填写/提交计划。填写参数为 `{"snapshot_id":"<读取回执中的32位ID>","fields":[{"ref":"f0","value":"aria"}]}`，提交参数为 `{"snapshot_id":"<同一ID>","form_ref":"form0"}`。提交仍为 A2 每次确认。
3. 页面、控件、字段值变化或切换标签后必须重读；填写中事件引起重排会停止后续操作，失败回执的 filled 表示此前已写数量。提交先消费快照，不能复用凭证重试。

快照在扩展隔离世界的内存中保存，导航、重载、新读取、重连或重启使其失效。密码框跳过，密码值不跨注入结果边界；读取回执按字节上限截断，未返回字段不能填写。文本和 select 使用原型 setter 与 input/change 事件，页面自身的事件逻辑仍会执行。

必须同步更新 Hub 和扩展，并在 Chrome 扩展管理页重载构建后的 `dist`。新版 Hub 拒绝不声明快照协议的旧扩展；新版扩展拒绝缺少 snapshot_id 的旧命令。旧工作流不能长期复用固定快照值，需要先读取再创建新动作计划。
