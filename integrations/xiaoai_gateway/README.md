# XiaoAI Gateway

小爱音箱的非官方协议隔离进程。它使用 `@mi-gpt/next` 获取小爱识别文本，
只把匹配 `XIAOAI_TRIGGER_PREFIX` 的 Query 送到 Hub，然后把 Hub 返回的
`reply.sentence` 逐句交给音箱 TTS。

## 配置

在管理后台打开“设备与感知 → HA 实体授权 → 小爱音箱网关”，从 HA
设备列表选择音箱并填写小米账号、中枢用户 UUID 和密码。网关认证密钥可在
页面随机生成。保存后 Hub 会把运行配置写入 `xiaoai-state` 私有共享卷，
不再需要在 `.env` 重复填写小米账号或密钥。

默认触发前缀是“请阿莉娅”。部分型号需要在页面额外填写 TTS SIID/AIID，
具体值从对应型号的 MIoT spec 确认。状态文件默认保存到 `/data/state.json`，
用于跨重启复用 Hub 会话。

这是个人测试用途的非官方接入。建议使用单独的小米家庭成员账号，不要把
小米凭据写入 Hub 配置或提交到仓库。

## 启动

页面中的“中枢用户 UUID”是 Hub 用户 UUID，不是小米账号。保存后执行：

```bash
docker compose --profile xiaoai up -d --build
docker compose logs -f xiaoai-gateway
```

首次成功连接会在持久卷写入 `conversation_id`。之后对音箱说
“小爱同学，请阿莉娅，告诉我今天的安排”即可触发。
