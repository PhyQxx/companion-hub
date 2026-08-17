# Aria × Open-LLM-VTuber v1

这是一个独立安装的 Agent 桥接包。Open-LLM-VTuber 继续负责麦克风、ASR、TTS、Live2D
和前端交互，Aria 负责身份、会话、隐私策略、模型路由、流式生成和 PostgreSQL 持久化。

模型供应商 API Key 不会进入 Open-LLM-VTuber。桥接端只读取本地聊天密码对应的环境变量。

## 兼容范围

- Open-LLM-VTuber v1.2.1（Python 3.10～3.12）
- Aria WebSocket protocol v1
- 当前支持文字输入、流式文字输出和打断；输出会继续经过 Open-LLM-VTuber 的分句、
  TTS 和 Live2D action 提取管线
- 图片和附件会转成明确的未启用提示，后续由 Aria 多模态输入适配器扩展
- 当前只接管单人会话；群聊暂不启用 Aria Agent

## 安装

先启动 Aria，确认 `http://127.0.0.1:8000/healthz` 可访问。然后在
Open-LLM-VTuber 仓库目录运行：

```bash
uv pip install -e /Users/peihaoyu/PHY/companion-hub/integrations/open_llm_vtuber
git apply /Users/peihaoyu/PHY/companion-hub/integrations/open_llm_vtuber/patches/open-llm-vtuber-v1.2.1.patch
```

将 [examples/aria-agent.yaml](examples/aria-agent.yaml) 的 `agent_config` 合并到
Open-LLM-VTuber 的 `conf.yaml`，再通过环境变量提供 Aria 本地聊天密码：

```bash
export ARIA_CHAT_PASSWORD='你的 Aria 本地聊天密码'
uv run run_server.py
```

不要把密码写进 `conf.yaml` 或提交到 Git。若 Open-LLM-VTuber 不是从仓库根目录启动，
请把 `mapping_path` 改为绝对路径。

## 协议映射

| Open-LLM-VTuber | Aria |
|---|---|
| `BatchInput.texts` | `message.send.text` |
| history UID | PostgreSQL conversation ID（本地仅保存 ID 映射） |
| Agent token stream | `reply.delta` |
| 字幕、TTS 与 Live2D 动作 | `reply.control.agent_reply` |
| interrupt / task cancel | `turn.cancel(generation_id)` |
| TTS / Live2D | 保持在 Open-LLM-VTuber 输出管线 |

`structured_reply: true` 默认启用。桥接端会等待最终控制事件，再生成 Open-LLM-VTuber
原生 `SentenceOutput`，因此字幕、朗读文本、表情、图片和音效可以分别控制。若某个场景更重视
最低首字延迟，可设为 `false`，继续使用旧的 token 流和 Open-LLM-VTuber 分句管线。

补丁中的 `manages_history` 检查会关闭 Open-LLM-VTuber 对该 Agent 的消息正文 JSON 副本，
避免形成第二份聊天记录；会话正文仍以 Aria PostgreSQL 为准。

## 验证

在 Aria 仓库执行：

```bash
uv run pytest server/tests/test_open_llm_vtuber_bridge.py
```

测试覆盖登录、会话映射、增量回复、密钥边界和 generation 取消。实际联调时，在
Open-LLM-VTuber 页面发送一条文字，并在 Aria `/chat` 中确认同一会话消息已持久化；再发一条
长回复并中途打断，确认 Aria 不产生迟到的助手消息。
