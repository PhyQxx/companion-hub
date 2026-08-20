# P3 嵌入升级评估：pgvector 落地验收记录

> 文档类型：Phase Record / Historical
> 说明：记录 pgvector 落地基线；新的 Memory/Timeline 召回策略分别以 `30/31` 为准。

> 日期：2026-08-18
> 范围：pgvector 向量列、双写与 ANN 召回、迁移回填、真实 PostgreSQL 验证
> 关联：docs/02 §3.6 检索策略与向量版本管理；TASKS.md P3 最后一项

## 评估结论

混合检索的向量通道在 PostgreSQL 上切换为 pgvector ANN 召回，其余方言（含测试用
sqlite）保持进程内余弦 —— 两条路径喂给同一套合并/重排逻辑，行为一致。
docker-compose 自始使用 `pgvector/pgvector:pg16` 镜像，生产环境无需变更。

## 落地内容

### 迁移 0009_embedding_vector（仅 PostgreSQL）

- `CREATE EXTENSION IF NOT EXISTS vector`；
- `memory.embedding_vec vector(256)`（当前哈希嵌入维度）+ HNSW 余弦索引；
- 回填：`UPDATE memory SET embedding_vec = (embedding::text)::vector …`
  （jsonb 数组的 text 形式即是合法 vector 字面量，纯 SQL 完成历史数据回填）；
- sqlite 等方言自动跳过，保持 JSON 列与进程内计算。

### 双写与召回（`app/memory/store.py`）

- 写路径（`add` / `edit`）在 PostgreSQL 上同事务双写 JSON 列与 `embedding_vec`；
- `vector_recall`：按用户/状态/有效期/隐私/嵌入版本过滤后
  `ORDER BY embedding_vec <=> CAST(:qv AS vector) LIMIT k`，返回 (id, 相似度)；
- 检索器在 `vector_sql_enabled` 时用 ANN 替换全量扫描，词法通道与重排不变；
  ANN 结果超出候选窗口（importance Top-500）的记忆会被安全跳过。

### 真实环境验证

- 临时 `pgvector/pgvector:pg16` 容器上：迁移链 0001→0009 全程通过，
  pgvector 0.8.2、HNSW 索引确认建立；
- 集成测试 `server/tests/integration/test_memory_postgres.py`
  （`ARIA_TEST_DATABASE_URL` 门控）：双写落列、ANN 命中目标记忆、
  编辑替代版本同样落列、硬删除后检索干净 —— 全部通过。

## 嵌入模型升级路径（后续批次）

1. 选定真实嵌入模型（utility 路由，OpenAI-compatible `/embeddings`）后，
   因维度不同（如 1024），需新增 `embedding_vec_v2` 列并双写新版本；
2. 以新版本嵌入回填历史记忆（`embedding_version` 过滤已就位，新旧向量互不比较）；
3. 回填完成后检索切读新列，观察一个稳定性窗口再删除旧列；
4. 词法通道的 PostgreSQL tsvector 化暂缓：v1 规模下进程内字符二元组余弦
   与向量通道已形成互补召回，等记忆量或延迟指标提出需求再升级。

## 验收结论

- [x] PostgreSQL 上向量召回走 pgvector ANN + HNSW 索引，其余环境零影响。
- [x] 历史数据迁移内纯 SQL 回填；双写与召回同事务一致。
- [x] 真实 pgvector 环境迁移链与集成测试全部通过（2 passed）。
- [x] sqlite 全量回归 107 通过；ruff / mypy 全绿。
