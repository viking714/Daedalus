# Daedalus

基于 [AgentTeams (HiClaw)](https://hiclaw.io/) 的多 Agent 协同研发系统。命名源自希腊神话中的天才工匠 Daedalus——他打造了自主行走的青铜巨人 Talos，正如本系统的 Agent 团队自动协作完成缺陷修复。

## 目录结构

```
├── deploy/           # AgentTeams 部署脚本、Worker 角色 YAML、Skills 打包、资源模板
├── mcp_server/        # MCP Server（入口 server.py + 14 原语 + 组合工具 + 数据层 db/embed/code）
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
- **MCP Server 说明**：`mcp_server/` 目录
- **详细设计**：`docs/02_详细设计/`

## TODO — 实现对齐设计

以下内容记录了当前实现与 `docs/03_新初赛提交/方案设计.md`（v2.2）之间的主要偏差，需逐步修正以设计为准。条目按优先级排列。

> 已完成项（已从本清单移除）：Skill 分层架构（三层模型）、Skills 打包体系（`deploy/packages/rd-defect-skills/` + SemVer）、6 prompt-only + 9 with-scripts Skills 结构、Skill 命名统一（`knowledge_miner` → `knowledge-extraction`）、`result-judge` 归属（转 prompt-only）、`domain_skills/` 目录收归 `mcp_server/`、经验沉淀闭环（`lessons` 表 + `lesson-lookup`/`knowledge-extraction` 落地 + MERGE/SIMILAR/NEW 去重合并）、全链路监控（AgentLoop 控制台接入凭证已配置 + MCP Server 层 OTel 手动埋点已实现 + `setup-agentloop.sh` 一键接入脚本）。

### P1 — 中优先级（重要功能缺失）

- **[灰度发布] 整个 §4 未实现**
  - 偏差：设计 §4 定义 Manager 生成 `release_plan.json`、进入 `awaiting_release` 状态、事件驱动唤醒、canary 超时哨兵（24h TTL）、回归闭环（`regression_cycle_count`，上限 3 次）。当前均不存在。
  - 建议：① 在 Manager 调度技能（`pipeline-router` Skill）中新增 `release_plan` 生成逻辑；② 扩展状态机 `_PIPELINE`，加入 `awaiting_release` → `confirming` → `resolved`/`analyzing` 分支；③ 在 `pipeline-router/scripts/task_router.py`、`state_manager.py` 中实现超时哨兵。

- **[状态机] 缺少 `received` 和 `awaiting_release` 阶段**
  - 偏差：设计 §2.2 状态机为 `received → analyzing → fixing → testing → evaluating → awaiting_release → resolved / escalated`，当前代码 `_PIPELINE` 仅含 `analyzing → fixing → testing → evaluating` 四阶段。
  - 建议：在 `composed_tools.py` 与 `pipeline-router/scripts/task_router.py` 的 `_PIPELINE` 中补全 `received` 和 `awaiting_release` 状态，并在 `task_router.py`/`state_manager.py` 中实现对应的状态迁移逻辑。

- **[产出物] `release_plan.json` 和 `confirmation_report.json` 缺失**
  - 偏差：设计 §2.4 定义 6 个核心产物，其中 `release_plan.json`（Manager 产出）和 `confirmation_report.json`（外部 CI/CD 产出）未在代码中生成或消费。
  - 建议：在 Manager 流程中新增生成 `release_plan.json`（含 `canary_scope`、`risk_level`、`rollback_point` 等字段）并写入 MinIO 的步骤；预留 `confirmation_report.json` 的消费接口。

## 参考资料

- AgentTeams (HiClaw)：https://hiclaw.io/
- AgentTeams 开源仓库：https://github.com/agentscope-ai/AgentTeams
