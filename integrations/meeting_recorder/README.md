# MEET-01 会议录音客户端

本机录音 + 本地转写的会议采集端。Hub 不接收、不保存任何原始音频；
只有授权后的文本分段（说话人 + 文本 + 时间戳）会进入 Hub。

## 红线

1. 原始音频在本机转写后立即删除（`chunk-*.wav` 用完即焚）；
2. 上传前必须终端显式授权（`record` 命令的 yes 确认 → `/transcription/authorize`）；
3. 说话人必须是会议声明参与人或本人，录音者段间切换标注，不自动声纹推断；
4. 撤销授权（`revoke` 或 Hub 端）后必须停止上传。

## 安装

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

macOS 首次使用需授予终端“麦克风”权限。

## 使用

```bash
# 1. 创建会议（参与人即允许的说话人名单）
python recorder.py prepare --title "周会" --participants 裴先生,张三

# 2. 录音：授权 → 分段录音/本地转写 → 段间切换说话人 → 上传文本
python recorder.py record --meeting <meeting-id> --speakers 裴先生,张三

# 3. 结束并触发摘要/行动项（行动项逐项确认后写入任务）
python recorder.py finish --meeting <meeting-id>
```

其他命令：`list`（会议列表）、`revoke`（撤销转写授权）。

转写偏置（人名/术语）通过 `--hotwords` 注入 faster-whisper `initial_prompt`；
隐私会议在 `prepare` 时用 `--privacy L2`（Hub 侧走本地模型生成摘要）。
