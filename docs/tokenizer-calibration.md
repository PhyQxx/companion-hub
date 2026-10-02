# 本地 tokenizer 与离线校准

此能力默认关闭。未配置端点继续按 UTF-8 字节估算；启用后对消息 JSON 和工具目录使用指定本地 tokenizer，加安全系数、消息/工具开销与协议预留。提供方实际聊天模板、隐藏控制字段和工具协议仍未核实，结果是估算，不是精确 prompt_tokens。

## 配置真实端点前

1. 从目标模型的可信发行包取得对应 tokenizer JSON。系统不自动查找、下载或运行外部脚本。
2. 放到已存在的 `ARIA_TOKENIZER_DIR`，命名 `<id>.json`，计算文件 SHA-256。ID 不允许路径，文件不能是符号链接，最大 64 MiB。
3. 在 Admin 模型服务的运行参数里启用本地分词，填写 ID、指纹、安全系数与协议预留。安全系数默认 1.25，协议预留至少 256。也可配置端点：

```yaml
context_tokenizer:
  id: actual-model-tokenizer
  sha256: <目标文件的64位小写SHA256>
  safety_multiplier: 1.25
  protocol_reserve_tokens: 256
```

匹配模型版本是操作方责任。文件指纹锁定分词资源，不证明它匹配提供方当前模型/模板。每次配置新指纹会重新载入；进程内最多缓存四个固定资源，不缓存用户正文。编码禁用文件自带的截断、填充；资源错误不静默改用较低计数，Router 只能尝试另一个合法端点，隐私检查保持。

Compose 可显式叠加只读文件挂载：

```sh
ARIA_TOKENIZER_HOST_DIR=/absolute/existing/tokenizers \
  docker compose -f docker-compose.yml -f docker-compose.tokenizers.yml config --quiet
```

该检查只解析配置，不启动或发布。正式启动使用同一组合文件；挂载不会自动创建目录或下载资源。直接运行设置 `ARIA_TOKENIZER_DIR`。

## 离线校准

CLI 只读取明确声明 synthetic 的本地案例和操作方提供的 prompt_tokens，不调用模型、执行工具或写运行配置。输入最多 256 KiB、50 个唯一案例；必须声明 usage_known。只有 total usage 或未知 usage 不能用作输入计数校准。

```json
{
  "version": 1,
  "synthetic": true,
  "provider": "fixture",
  "model": "model-version",
  "cases": [{
    "id": "mixed-language",
    "messages": [{"role": "user", "content": "Synthetic example 你好"}],
    "tools": [],
    "prompt_tokens": 32,
    "usage_known": true
  }]
}
```

这里的 32 只是声明示例，不能当作真实提供方证据。对目标模型使用已授权的自造样例与对应完整输入 usage，覆盖中英文、长文本、工具目录/结果、系统片段和 fallback；不同端点分别校准。

```sh
ARIA_TOKENIZER_DIR=/absolute/existing/tokenizers \
  uv run python server/scripts/calibrate_tokenizer.py \
  --corpus /absolute/synthetic-corpus.json \
  --tokenizer-id actual-model-tokenizer \
  --tokenizer-sha256 <SHA256> --report /absolute/calibration-report.json
```

报告只包含 ID、模型标识、库版本、指纹、计数和不足额；不保存消息或工具参数。退出 0 表示声明样例都在估算内，1 表示有少算，2 表示输入/资源不可用。建议协议余量仅适用于该 corpus，不自动发布配置，也不证明任意输入或实际业务目标通过。校准语义与提供方协议验证保持 unverified，V2 仅指样例计数契约。

可复算示例见 `docs/baselines/tokenizer-synthetic-corpus.json`、`synthetic-word-level.json` 与 `tokenizer-synthetic.json`。该四词词表仅用于软件演示，**不能配置到真实模型**：

```sh
ARIA_TOKENIZER_DIR="$PWD/docs/baselines" \
  uv run python server/scripts/calibrate_tokenizer.py \
  --corpus docs/baselines/tokenizer-synthetic-corpus.json \
  --tokenizer-id synthetic-word-level \
  --tokenizer-sha256 60251ed1edcb3ac0b0d9921a145035c5d1596f11c031f40e7ba12873d6852661
```

真实模型计数验收仍需实际端点、模板版本和已授权合成样例的 usage；本批未访问真实模型。
