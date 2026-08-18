# P3 utility 提取器与 L2 脱敏验收记录

> 日期：2026-08-18
> 范围：LLM 结构化记忆提取（utility 路由）、L2 亲密对话脱敏沉淀、规则提取器回退
> 关联：docs/02 §3.6 沉淀管线、§3.5 双模型路由；TASKS.md P3 第三项

## 交付内容

### 提取器架构（`app/memory/extraction.py`）

- `MemoryExtractor` 协议：`extract(text, message_id, privacy_level, occurred_at, backend)`；
  提取器自行决定能否处理某个隐私等级，沉淀入口（`MemoryIngester.ingest_message`）不再做统一拦截。
- `RuleBasedExtractor`（rule-v1）：保守确定性提取，维持原有行为；
  **L2/L3 一律跳过**——没有脱敏能力的内容不落库。
- `LlmMemoryExtractor`（llm-utility-v1）：utility 路由 + `json_mode` 的结构化提取；
  输入截断 4000 字符，输出经 `ExtractedCandidates` 严格校验（类型枚举、内容 2~200 字），
  最多采纳 4 条候选。

### L2 脱敏约束

- L2 提取提示词强制：content 只能是事件级概括（发生了什么 / 时长 / 用户情绪倾向），
  严禁生理、身体或露骨细节；沉淀出的记忆 `privacy_level=L2`。
- L2 记忆沿用既有检索闸门：只进入 L2（强制本地路由）上下文，不会随 L0/L1 云调用出站。
- 端到端验证：L2 轮次 → utility 提取（提示词含隐私约束、json_mode）→ L2 记忆落库 →
  L1 检索不可见、L2 检索可见。

### 韧性与回退

- 模型输出不是合法 JSON、schema 校验失败或任何网络异常 → 自动降级为规则提取器，
  该轮沉淀不丢失（L0/L1 场景）；回退记忆的 `extractor_version` 保持 rule-v1 可追溯。
- 提取完全失败也不影响已提交回合（沿用 P2 的隔离边界）。

### 接线

- `ChatService` 新增 `memory_extractor` 注入；回复与提取复用同一回合的 router 后端。
- 运行时开关：`ARIA_MEMORY_EXTRACTOR=llm` 启用 LLM 提取，默认 `rule`。
- 提取请求沿用隐私路由强制：utility 请求携带原始隐私等级，L2 由路由器强制转向本地端点，
  云端 utility 模型永远不会收到 L2 内容。

## 验收结论

- [x] L2 亲密对话可沉淀事件级脱敏记忆，且不出现在 L0/L1 上下文。
- [x] utility 提取走 json_mode + 严格 schema 校验，坏输出自动回退规则提取。
- [x] 空候选输出不产生记忆；L3 仍然全链路阻断。
- [x] 回复后端与提取后端复用同一轮 router，不额外构建客户端。
- [x] pytest 104 通过（新增 L2 脱敏、回退、空候选三组测试）；ruff / mypy 全绿。

## 当前边界与后续

- 提取提示词为中文单轮版本；每日反思 / 关系回顾（emotional 记忆批量生产）属记忆 v2。
- 嵌入仍为本地哈希；utility 嵌入 + pgvector 双召回评估与消息删除级联、台账重放工具保持待办。
