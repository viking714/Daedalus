# Daedalus

基于 [AgentTeams (HiClaw)](https://hiclaw.io/) 的多 Agent 协同研发系统。命名源自希腊神话中的天才工匠 Daedalus——他打造了自主行走的青铜巨人 Talos，正如本系统的 Agent 团队自动协作完成缺陷修复。

## 目录结构

```
├── deploy/           # AgentTeams 部署脚本、Worker 角色 YAML、资源模板
├── domain_skills/    # 领域技能 MCP Server（16 个 DB-backed 智能技能）
├── docs/             # 比赛材料、架构设计与交付文档
├── scripts/          # SWE-bench 自动化测试脚本
├── results/          # 测试结果输出
├── secrets/          # SSH 私钥（已被 .gitignore 忽略）
└── Makefile          # 常用命令入口
```

## 快速开始

### 首次搭建

```bash
./deploy/scripts/setup.sh <服务器IP> [PEM路径]
```

### 日常启动

```bash
./deploy/scripts/start.sh [服务器IP] [PEM路径]
./deploy/scripts/start.sh stop    # 停止全部
```

### 手动启动领域技能服务

```bash
make run-domain-skills    # MCP Server 监听 :8090
```

## 架构概要

系统分为三层：

1. **控制面**（AgentTeams Manager）— 任务分发、多 Agent 协作编排、人工介入
2. **运行面**（Worker Runtime）— 5 个 Worker 角色：Manager / Analyzer / Fixer / Tester / Evaluator
3. **能力面**（Domain Skills）— MCP Server 暴露数据库驱动的智能技能（语义搜索、图谱查询、影响面分析等），Worker 通过 MCP 协议直连

Worker 间通过 MinIO 共享存储交换文件（仓库拉取、产物传递），文件操作与测试执行在 Worker 容器沙箱内原生处理。

## 相关文档

- **架构设计**：`docs/03_新初赛提交/方案设计.md`
- **Agent 清单**：`docs/03_新初赛提交/AgentIdentity清单.md`
- **Skill 清单**：`docs/03_新初赛提交/Skill清单.md`
- **部署详情**：`deploy/README.md`
- **领域技能说明**：`domain_skills/README.md`
- **详细设计**：`docs/02_详细设计/`

## TODO — 实现对齐设计

以下内容记录了当前实现与 `docs/03_新初赛提交/方案设计.md`（v2.2）之间的主要偏差，需逐步修正以设计为准。条目按优先级排列。

### P0 — 高优先级（架构核心差异）

- **[Skill 分层架构] 三层模型未落地**
  - 偏差：设计 §3.1 要求三层（AgentTeams Skills → MCP 原语 → 数据层），当前 `domain_skills/mcp_server.py` 直接将 16 个技能函数注册为 MCP tool，扁平化为单层。
  - 建议：将现有 `skills.py` 中的业务逻辑拆分为两组——① 细粒度 MCP 数据访问原语（如 `pgvector_search`、`neo4j_expand_chunks`、`meili_keyword_search` 等，共 13 个），在 `mcp_server.py` 中暴露；② AgentTeams Skills 层（SKILL.md + scripts/），编排 MCP 原语完成工作流。

- **[Skills 打包体系] `deploy/packages/` 完全缺失**
  - 偏差：设计 §3.7 定义 `deploy/packages/rd-defect-skills/`（含 `manifest.json`、`SKILL.md`、`scripts/`），且 SemVer 版本控制（Git tag + manifest + ZIP 文件名三处同步），当前 `deploy/packages/` 目录不存在。
  - 建议：创建 `deploy/packages/rd-defect-skills/` 目录，按 6 prompt-only + 9 with-scripts 拆分 SKILL.md；引入 `manifest.json` 和版本号；Worker YAML 改用 `spec.package` 引用。

- **[经验沉淀闭环] 整个 §5 基本未实现**
  - 偏差：设计 §5 定义 `lessons` 表（PostgreSQL + pgvector）、`lesson-lookup` Skill（Analyzer/Fixer 查询历史经验）、`knowledge-extraction` Skill（Evaluator 写入经验）、去重策略（MERGE/SIMILAR/NEW 三级相似度判断）。当前均不存在。
  - 建议：① 在 `db/schema.py` 新增 `lessons` 建表语句；② 在 `skills.py` 中新增 `lesson_lookup` 技能（支持 mode=analyzer/fixer 两种查询维度）；③ 将现有 `knowledge_miner` 扩展为 `knowledge_extraction`，实现结构化写入 `lessons` 表和去重合并逻辑。

- **[prompt-only Skills] 6 个 Skill 未以结构化形式实现**
  - 偏差：设计 §3.2.1 定义 6 个 prompt-only Skills（`code-read`、`bash-exec`、`test-run`、`patch-generate`、`result-judge`、`multi-file-edit`），通过 SKILL.md 封装使用规范。当前这些能力仅在 Worker YAML 的 `soul`/`agents` 字段中以自然语言描述，无结构化 Skill 定义。
  - 建议：为 6 个 prompt-only Skill 各创建 `SKILL.md`，放入 `deploy/packages/rd-defect-skills/skills/`；Worker YAML 保留 soul 但引用 Skill 名称。

- **[with-scripts Skills] 9 个 Skill 无独立 Script 拆分**
  - 偏差：设计 §3.2.2 定义 9 个 with-scripts Skills（含 `scripts/*.py`），如 `pipeline-router` 含 `task_router.py`/`state_manager.py`/`handoff.py`/`loop_judge.py`。当前全部实现在 `skills.py` 单文件中，无独立脚本目录。
  - 建议：将 `skills.py` 中的函数按 Skill 粒度拆分为独立 `scripts/` 文件，每个 Skill 目录含 `SKILL.md` 和 `scripts/`，从 `skills.py` 迁移业务逻辑。

### P1 — 中优先级（重要功能缺失）

- **[全链路监控] 整个 §6 未实现**
  - 偏差：设计 §6 要求通过 AgentLoop/OpenTelemetry 实现链路追踪、Agent 总览、异常审计。当前 `mcp_server.py` 无 OTel 埋点，`start.sh` 无 AgentLoop 相关启动配置，`deploy/agentloop.env` 不存在。
  - 建议：① 创建 `deploy/agentloop.env` 配置 OTLP 端点与凭证；② 在 `mcp_server.py` 统一入口为每个 MCP 原语调用生成 Span；③ 在 `start.sh` 中用 `opentelemetry-instrument` 包装 MCP Server 启动。

- **[灰度发布] 整个 §4 未实现**
  - 偏差：设计 §4 定义 Manager 生成 `release_plan.json`、进入 `awaiting_release` 状态、事件驱动唤醒、canary 超时哨兵（24h TTL）、回归闭环（`regression_cycle_count`，上限 3 次）。当前均不存在。
  - 建议：① 在 `skills.py` 的 Manager 技能中新增 `release_plan` 生成逻辑；② 扩展状态机 `_PIPELINE`，加入 `awaiting_release` → `confirming` → `resolved`/`analyzing` 分支；③ 在 `task_router`/`state_manager` 中实现超时哨兵。

- **[状态机] 缺少 `received` 和 `awaiting_release` 阶段**
  - 偏差：设计 §2.2 状态机为 `received → analyzing → fixing → testing → evaluating → awaiting_release → resolved / escalated`，当前代码 `_PIPELINE` 仅含 `analyzing → fixing → testing → evaluating` 四阶段。
  - 建议：在 `skills.py` 的 `_PIPELINE` 中补全 `received` 和 `awaiting_release` 状态，并在 `task_router`/`state_manager` 中实现对应的状态迁移逻辑。

- **[产出物] `release_plan.json` 和 `confirmation_report.json` 缺失**
  - 偏差：设计 §2.4 定义 6 个核心产物，其中 `release_plan.json`（Manager 产出）和 `confirmation_report.json`（外部 CI/CD 产出）未在代码中生成或消费。
  - 建议：在 Manager 流程中新增生成 `release_plan.json`（含 `canary_scope`、`risk_level`、`rollback_point` 等字段）并写入 MinIO 的步骤；预留 `confirmation_report.json` 的消费接口。

### P2 — 低优先级（命名/路径/细节不一致）

- **[Skill 命名] `knowledge_miner` 与设计命名不一致**
  - 偏差：设计中 Evaluator 的经验沉淀 Skill 名为 `knowledge-extraction`，代码中为 `knowledge_miner`。
  - 建议：将 `knowledge_miner` 重命名为 `knowledge-extraction`，同步更新 `skills.py` 中注册名和 Worker YAML 中的引用。

- **[目录路径] 设计文档引用路径与代码实际不符**
  - 偏差：设计 §8.1.1 开源范围引用 `ref_impl/workers/` 和 `ref_impl/mcp_tools/`，实际路径为 `deploy/workers/` 和 `domain_skills/`（无独立 mcp_tools 目录）；§6.3.3 引用 `mcp_tools/mcp_server.py`，实际为 `domain_skills/mcp_server.py`。
  - 建议：统一设计文档中的路径引用为实际代码路径，或将代码目录调整为设计文档中的命名（前者更轻量，推荐）。

- **[Skill 归属] `result-judge` 分层不一致**
  - 偏差：设计 §3.2.1 将 `result-judge` 定义为 prompt-only Skill（由 Worker runtime 直接提供），但代码中 `result_judge` 作为 MCP tool 实现在 `skills.py` 中。
  - 建议：将 `result_judge` 从 `skills.py`/MCP Server 中移除，改为 prompt-only Skill 的 `SKILL.md`，由 Tester/Evaluator Worker 在 soul 中引用。

## 参考资料

- AgentTeams (HiClaw)：https://hiclaw.io/
- AgentTeams 开源仓库：https://github.com/agentscope-ai/AgentTeams
