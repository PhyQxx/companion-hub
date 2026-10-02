# Harness 离线基线

运行 `make harness-bench` 可重建两个报告。每份报告使用 20 次预热、200 次采样，记录环境、p50/p95/p99 和原始配对样本；并发为 1 和 4。生产配置、凭据、数据库、模型及设备都不会被加载。

当前 fixture 只比较固定 stub completion 直接调用与零工具 AgentLoop 的配对增量，并在临时 SQLite 中测量没有关联工作时的持久停止受理。计时包括协作式调度；停止计时包括数据库提交，不包括 fixture 创建和建表。配对执行交替先后次序，报告不去除异常值。并发场景的 semaphore 排队不在计时内，数据库锁等待包含在计时内。

2026-10-02 实测：Darwin 27 / arm64 / Python 3.11.15 / 10 个逻辑 CPU。并发 1：循环增量 p95 0.0035ms，停止 p95 6.84ms；并发 4：循环增量 p95 0.006125ms，停止 p95 18.97ms。报告保留原始样本便于复算。50ms / 1000ms 仅是本 fixture 的参考目标；结果不代表完整聊天、上下文检索、工具执行、并发用户吞吐、真实模型/设备延迟或生产 PostgreSQL 性能。

配置回退演练使用 `server/tests/test_run_budget.py::test_configuration_rollback_preserves_live_budget_and_unknown_evidence`：真实 DatabaseConfigStore 发布更紧预算、领取 Job 并产生未知 usage，再回退配置、重建配置 Store、续用原 Run；原预算快照、次数、未知扣占、领取身份与 unknown_outcome 动作保持，旧运行仍受原上限约束。只执行测试数据库，不改变部署配置或撤销外部动作。应用二进制回退、真实 provider 账单核对和部署环境压力验收仍需分别执行。

## 本地分词计数示例

`tokenizer-synthetic.json` 使用附带的四词 WordLevel JSON 与明确声明的 synthetic corpus；库版本和指纹均在报告。只是离线计数流水线复算，不含真实模型用量，不可用该演示词表配置真实端点。命令、范围与失败解释见 [分词校准说明](../tokenizer-calibration.md)。
