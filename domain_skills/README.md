# 领域技能 MCP Server（Python）

研发缺陷闭环协同系统的「业务/领域能力」层：以 MCP Server 暴露数据库驱动的智能技能，
供 AgentTeams 的 Worker 通过 `mcpServers` 声明直接连接（无需 worker_bridge 适配层）。

## 架构说明

```
Worker 容器内                        宿主机
┌──────────────────┐     MCP      ┌────────────────────────┐
│ 原生工具:         │   (HTTP)     │ domain_skills/         │
│  • read file     │─────────────→│  mcp_server.py         │
│  • run bash      │  mcporter    │    ↓                   │
│  • git diff      │              │  skills.py (16 个技能)  │
│  • write file    │              │    ↓                   │
│                  │              │  db/ embed/ code/      │
│                  │              └────────────────────────┘
└──────────────────┘                        │
                                     SSH tunnel → ECS
                                     (PG/Neo4j/Meili/Redis)
```

- **Worker 原生处理**：文件读写、bash 执行、git 操作、测试运行（在容器沙箱内）
- **MCP Server 处理**：需要数据库的智能技能（语义搜索、图谱查询、影响面分析等）
- **Manager 负责**：git clone 目标仓库 → repo_indexer 建索引 → 打包推送到 MinIO
- **MinIO 共享存储**：Manager push 仓库 → Worker pull → 本地工作 → push 产物 → 下游 Worker pull

Worker 通过 `env:` 声明 MinIO 连接信息（`MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` / `MINIO_BUCKET`），
在容器内用 `mc` CLI 执行所有文件传输，不经过 MCP Server。

## 分层架构

```
mcp_server.py      MCP 入口（Streamable HTTP，Worker 通过 mcporter 连接）
   │
skills.py          业务层（Registry 模式：@register 注册 16 个技能）
   │
   ├─ db/          数据访问层（Repository 模式，懒连接 + 优雅降级）
   │    ├ config.py        连接配置（读 .env.db / 环境变量）
   │    ├ pgvector.py     PostgreSQL + pgvector（向量检索）
   │    ├ neo4jgraph.py  Neo4j（代码依赖关系图）
   │    ├ meili.py        Meilisearch（全文检索）
   │    ├ redis_cache.py  Redis（索引新鲜度 / 嵌入缓存）
   │    └ schema.py       一键初始化所有 schema
   ├─ embed/        嵌入层（Strategy 模式）
   │    └ embeddings.py   EmbeddingService：fastembed 本地（默认）/ OpenAI 可切换
   └─ code/         解析层
        └ ast_parser.py  tree-sitter 多语言 AST 切分
```

## 运行

```bash
cd domain_skills
pip install -r requirements.txt
export AGENTTEAMS_ENV_FILE=/abs/path/to/.env.db   # 含 DB 密码

# MCP Server（Worker 连接入口）
python mcp_server.py                                      # 监听 :8090/mcp
```

## 16 个 MCP 技能（v2.0）

| 技能 | 角色 | 说明 |
|------|------|------|
| `task_router` | Manager | 流水线推进 / 达 `MAX_ROUND=3` 转 handoff |
| `state_manager` | Manager | 状态迁移 + 强制阈值（轮次/文件数/Token/超时闸门） |
| `handoff_manager` | Manager | 生成人工移交包 |
| `repo_indexer` | Analyzer | tree-sitter 切分→嵌入→写入 pgvector/Neo4j/Meili |
| `hybrid_search` | Analyzer | 向量+关键词两路 RRF 融合 + Neo4j 图扩展 |
| `semantic_search` | Analyzer/Evaluator | 三库融合语义检索 |
| `kg_query` | Analyzer | 知识图谱结构关系查询 |
| `module_lookup` | Analyzer/Evaluator | 领域概念到模块映射 |
| `context_packer` | Analyzer | 拼接结构化上下文 |
| `root_cause_analyzer` | Analyzer | Neo4j 依赖子图 + 启发式降级 |
| `repair_planner` | Fixer | 受影响面约束，单轮 ≤ `MAX_FILES=5` |
| `risk_gate` | Fixer | 默认拒绝 + 敏感模块/高危审批 |
| `dep_graph_analyzer` | Evaluator | Neo4j 波及范围 / 风险等级 |
| `contract_checker` | Evaluator | 检测函数签名改动 |
| `result_judge` | Tester | success / retry / handoff 裁定 |
| `knowledge_miner` | Evaluator | 根因模式 / 标签抽取 |

> **已移除的技能**（Worker 容器内原生执行）：`read_code`、`run_bash`、`write_reproduction`、
> `patch_generator`、`test_runner`、`multi_file_editor`。这些技能需要文件系统访问，
> 由 Worker 在其容器沙箱内原生处理。

## 闭环阈值（设计硬指标，在 `state_manager`/`task_router` 强制）

- `MAX_ROUND = 3`：超过则 handoff
- `MAX_FILES = 5`：单轮修改文件数上限
- `TOKEN_BUDGET = 100000`：超预算触发二次压缩
- `TASK_TIMEOUT_MIN = 30`：超时转 handoff
