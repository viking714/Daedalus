---
name: repo-index
description: repo-index Skill - repo-index
---
# repo-index

## 类型
AgentTeams Skill — with-scripts（核心脚本：`scripts/index.py`）

## 角色
Manager (主用)

## 功能
增量代码索引：tree-sitter 分块 → 嵌入 → 三库写入（pgvector + Neo4j + Meilisearch）。

## 使用场景
Manager 首次全量索引 + 后续增量更新。收到新任务且 Redis 中 `repo_state` 与目标 commit 不一致时触发。

## 输入参数
| 参数 | 必填 | 说明 |
|------|------|------|
| `repo_path` | 是 | 仓库绝对路径 |
| `commit` | 否 | commit SHA |
| `full_reindex` | 否 | 是否强制全量重建，默认 false |

## 输出结果
索引统计：`chunk_count` / `file_count` / `duration_sec`、Redis 中更新 `repo_state` hash、嵌入缓存命中率。

## 调用条件
Manager 收到新任务且需要建立/更新代码索引时触发。

## 依赖 MCP 原语
`pgvector_upsert_chunk` / `pgvector_delete` / `meili_keyword_search`（写入端）
/ `neo4j_expand_chunks` / `neo4j_impact_stats`（图谱写入）
/ `embed_texts` / `redis_get_repo_state` / `redis_set_repo_state` / `redis_fetch_embedding` / `ast_parse_file`

## 执行方式
脚本 `index.py` 直接调用 `domain_skills/` 的 db/embed/code 模块（绕过 MCP 层，性能更优），由 Worker 通过 bash-exec 触发。

## 增量索引流程
1. 从 Redis 获取当前索引状态
2. 检查三库实际数据状态
3. 若 Redis 与数据库状态不一致，先 reset 当前 ns
4. 扫描新仓库，计算 file_hashes
5. Diff：removed / added / changed / unchanged
6. 删除 removed + changed 文件的旧数据
7. 只解析 added + changed 文件
8. 嵌入（命中缓存则复用）+ 写入 DB
9. 重建关系边
10. 更新 Redis 状态

## 失败处理
- 单文件嵌入失败 → 跳过该文件、记录 `failed_files`、下次增量补充
- 三库部分失败 → 按库独立重试，最终一致性
- P99 目标 ≤ 60s

## 复用价值
**中**。仅 Manager 在调度初期使用；可作为独立"仓库索引"服务。
