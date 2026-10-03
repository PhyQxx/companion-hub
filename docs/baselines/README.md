# Harness 离线基线

运行 `make harness-bench` 可重建两个报告。每份报告使用 20 次预热、200 次采样，记录环境、p50/p95/p99 和原始配对样本；并发为 1 和 4。生产配置、凭据、数据库、模型及设备都不会被加载。

当前 fixture 只比较固定 stub completion 直接调用与零工具 AgentLoop 的配对增量，并在临时 SQLite 中测量没有关联工作时的持久停止受理。计时包括协作式调度；停止计时包括数据库提交，不包括 fixture 创建和建表。配对执行交替先后次序，报告不去除异常值。并发场景的 semaphore 排队不在计时内，数据库锁等待包含在计时内。

2026-10-02 实测：Darwin 27 / arm64 / Python 3.11.15 / 10 个逻辑 CPU。并发 1：循环增量 p95 0.0035ms，停止 p95 6.84ms；并发 4：循环增量 p95 0.006125ms，停止 p95 18.97ms。报告保留原始样本便于复算。50ms / 1000ms 仅是本 fixture 的参考目标；结果不代表完整聊天、上下文检索、工具执行、并发用户吞吐、真实模型/设备延迟或生产 PostgreSQL 性能。

配置回退演练使用 `server/tests/test_run_budget.py::test_configuration_rollback_preserves_live_budget_and_unknown_evidence`：真实 DatabaseConfigStore 发布更紧预算、领取 Job 并产生未知 usage，再回退配置、重建配置 Store、续用原 Run；原预算快照、次数、未知扣占、领取身份与 unknown_outcome 动作保持，旧运行仍受原上限约束。只执行测试数据库，不改变部署配置或撤销外部动作。应用二进制回退、真实 provider 账单核对和部署环境压力验收仍需分别执行。

## 本地分词计数示例

`tokenizer-synthetic.json` 使用附带的四词 WordLevel JSON 与明确声明的 synthetic corpus；库版本和指纹均在报告。只是离线计数流水线复算，不含真实模型用量，不可用该演示词表配置真实端点。命令、范围与失败解释见 [分词校准说明](../tokenizer-calibration.md)。


## 规则感知持久准入

`perception-admission-{sqlite,pg}-c{1,4}.json` 保存固定空世界、纯规则 IGNORE 路径的原方法与持久准入样本。每模式 100 次/10 预热，每份复核 220 个决策/审计；正式数据依次在全量测试结束后执行。源码指纹用于定位当时实现，后续源码变更不改写历史测量。两个 fixture 分阶段运行，sample_index_increment 仅是序号对应的差值，不是交替同请求的配对因果估计。

复算 SQLite（并发改为 1 或 4）：

```sh
uv run python server/scripts/benchmark_perception_admission.py --samples 100 --warmup 10 --concurrency 4 --output /tmp/perception-sqlite-c4.json
```

PostgreSQL 需操作员明确提供专用、名字以 `_test` 结尾的 asyncpg URL，运行时只创建/删除自己的随机 schema；已安装 pgvector 的测试库沿用共享 fixture 存储工具，报告不保存连接地址：

```sh
uv run python server/scripts/benchmark_perception_admission.py --samples 100 --warmup 10 --concurrency 4 --postgres-test-url "$ARIA_TEST_DATABASE_URL" --output /tmp/perception-pg-c4.json
```

SQLite 并发 1/4 的差值 p95 为 9.344/88.756ms；PostgreSQL 16.14 为 15.947/24.930ms。SQLite 尾部仍需优化。范围不含模型、观察者/handler、通知、HTTP/设备、历史大库及完整 Harness，不据此宣布整体 50ms 目标验收。


## 原子领取与写入锁序重测（2026-10-03）

`perception-atomic-claim-{sqlite,pg}-c{1,4}.json` 与 `chat-harness-write-first-{sqlite,pg}-c4.json` 在全量验证结束后串行测量；每模式 100 样本、10 预热，每报告复核 220 提交。六份报告共同完整源指纹 `82d653cfd251d8f484825188d35122766ed1fa55dc439a127c3a103cc3f8df42`，覆盖 server/app 与 server/scripts 的 418 个 Python 文件内容及相对名。新指纹算法与旧感知部分文件摘要不同，不直接比较摘要；测量中源变动会清理 fixture 后拒绝报告。

感知样本序号差值 p95（c1/c4）：SQLite 10.275/105.255ms，PG 23.731/43.069ms；并发 4 聊天交替配对预算增量 p95：SQLite 67.272ms、PG 54.725ms，p99 为 74.095/72.903ms。感知分阶段样本仍不构成因果配对，聊天两侧仍都有 Harness。数据库争锁与调度尾部全部保留；这些切片没有完成整体 50ms 目标，不能把历史不同时间数据解释为受控的性能提升。
