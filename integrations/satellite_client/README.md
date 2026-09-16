# SAT-01~03 全屋语音卫星客户端

把树莓派/旧手机（Termux）变成 Aria 的房间语音终端：设备凭据鉴权连接、
唤醒仲裁、PCM 话语上传、TTS 分片播放、免唤醒连续对话与跨房间接管。

## 与 Hub 的契约

- 状态机：`idle → listening → processing → speaking → idle`；进入 listening
  必须等 Hub 仲裁下发的 `satellite.state.set`，设备不得自行进入。
- 上行：`satellite.audio.start/chunk/end`，PCM16/16k/mono，分片序号连续、
  结束带字节数与 SHA-256，单话语 ≤4MiB。
- 下行：`voice.sentence`（mime/采样率/文本）+ `satellite.audio.chunk`
  （base64 分片）；PCM 直接播放，MP3 需配置 `--player`（如 `ffplay`）。
- 帧签名：与 Hub `sign_device_frame` 相同的 HMAC-SHA256 规范化 JSON。

## 安装（树莓派 / Termux）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 唤醒词可选（不用时回车键触发唤醒）：
pip install openwakeword
```

系统依赖：PortAudio（`apt install portaudio19-dev` / Termux 自带）；
MP3 播放可选 `ffmpeg` 或 `mpv`。

## 使用

```bash
# 0. Admin 设备页生成配对码 → 完成配对 → 授权 voice.satellite 能力，
#    复制设备访问令牌（--token）

# 1. 手动唤醒模式（回车 = 唤醒；t = 接管其他房间的连续会话）
python satellite.py --hub http://hub.lan:8000 --token <token> \
    --room bedroom --wake none

# 2. 唤醒词模式（openwakeword 常开麦克风）
python satellite.py --hub http://hub.lan:8000 --token <token> \
    --room bedroom --wake openwakeword --player "ffplay -nodisp -autoexit -"
```

参数：`--privacy L1|L2`（本房间允许的隐私上限，L2 强制 Hub 走本地
ASR/TTS）、`--continuous 8`（免唤醒窗口秒数）、`--vad-threshold`（现场
噪音调误触发）、`--debug-dir`（保存上行话语 WAV 供排查）。

## 红线

- 设备只持自己的访问令牌，无用户账号信息；令牌泄露在 Admin 设备页
  单独撤销本台；
- 音频仅在 listening（含免唤醒窗口）或 barge-in 时上传，idle 常开麦克风
  只用于本地唤醒词检测，不外发；
- L2 房间的音频经 Hub 强制本地处理，不上云。
