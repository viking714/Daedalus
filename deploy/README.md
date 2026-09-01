# Daedalus / AgentTeams 统一部署目录

单机部署：AgentTeams 平台 + 数据库栈 + 领域技能 MCP Server 全部安装在**同一台机器**
（本地笔记本或一台服务器），本机优先，无需 SSH 隧道或远程编排。

## 目录结构

```
deploy/
├── config.env.example   # 唯一配置模板（复制为 config.env 填写；含密钥，已忽略）
├── scripts/
│   ├── install.sh       # 唯一安装脚本（一次性 / 可重跑升级）
│   ├── run.sh           # 唯一运行脚本（start / stop / restart / status）
│   └── lib/common.sh    # 共享库（配置加载、DB 栈、MCP、平台安装与资源注册）
├── install/
│   └── agentteams-install.sh  # vendored 官方安装器（v1.2.3，AGENTTEAMS_* 契约）
├── workers/             # Worker 角色定义（7 个角色 YAML）
├── teams/               # Team / Manager / Human 资源模板
├── packages/            # 领域技能包源码与构建产物（rd-defect-skills）
├── db/                  # 数据库栈编排（docker-compose.db.yml）
└── archive/             # 已废弃的历史脚本（仅存档，勿使用）
```

## 快速开始

```bash
# 1. 生成配置并填写（至少填 AGENTTEAMS_LLM_API_KEY / AGENTTEAMS_ADMIN_PASSWORD / OPENAI_API_KEY）
cp deploy/config.env.example deploy/config.env

# 2. 一键安装（数据库栈 + MCP Server + AgentTeams 平台 + 技能包 + 资源注册）
bash deploy/scripts/install.sh

# 3. 日常启停
bash deploy/scripts/run.sh start | stop | restart | status
```

以上均有根目录 `make` 快捷入口：`make install / start / stop / restart / status`（SWE-bench 相关见 `make swe-bench*`）。

`install.sh` 幂等可重跑：数据库密码首次随机生成并持久化到 `${RUNTIME_DIR}/db/.env`，
后续运行复用；已安装平台则进入升级流程。

## 组件说明

### AgentTeams 平台（scripts/lib/common.sh::install_agentteams）

- 版本固定为端到端验证过的 **v1.2.3**（`AGENTTEAMS_VERSION`，勿随意改）。
- 安装器读取 `AGENTTEAMS_*` 环境变量（由 `config.env` 生成）；生成 `~/agentteams-manager.env`。
- manager runtime 必须为 `qwenpaw`（`agentteams-manager-qwenpaw` 镜像），普通 worker 为 `openclaw`。
- v1.2.x 命名：`agentteams-controller`（CLI `agt`）/ `agentteams-manager` /
  `agentteams-worker-*` / MinIO alias `agentteams`（bucket `agentteams-storage`）。

### 数据库栈（docker compose）

| 容器 | 数据库 | 端口 | 用途 |
|------|--------|------|------|
| `at-postgres` | PostgreSQL + pgvector | 5432 | 主关系库 + 向量检索 |
| `at-redis` | Redis | 6379 | 缓存 / 队列 / 会话 |
| `at-meili` | Meilisearch | 7700 | 全文/关键词检索 |
| `at-neo4j` | Neo4j | 7474 / 7687 | 代码知识图谱 |

- 端口默认仅绑 `127.0.0.1`；每个库可声明 `XXX_EXTERNAL=1` 复用外部实例（跳过安装）。
- Neo4j 依赖 `vm.max_map_count>=262144`，脚本自动设置（Linux sysctl / macOS 特权容器）。

### 领域技能 MCP Server

- 直接运行仓库 `mcp_server/` 源码（端口 `MCP_PORT`，默认 8090，绑 `MCP_HOST`）。
- 连接配置来自 `${RUNTIME_DIR}/db/.env`（安装脚本生成，DB 连接 + Embedding 一体）。
- Worker 容器访问 MCP 的主机名：macOS 用 `host.docker.internal`；Linux 容器内不解析该名，
  脚本自动回退为 Docker 网桥网关 IP（`resolve_linux_mcp_host`）。
- Worker YAML 与技能包 manifest 中的 MCP 端点为 `__MCP_WORKER_HOST__:__MCP_PORT__`
  占位符，部署/打包时渲染——改 `MCP_PORT` 即全局生效。

### Worker 角色与资源注册

| 角色 | 文件 | 职责 |
|------|------|------|
| Team Leader (coordinator) | `workers/coordinator.yaml` | 任务信封解析、流水线路由、回退仲裁、最终 Verdict |
| PO | `workers/po.yaml` | Gate0 需求澄清、PRD 产出 |
| Architect | `workers/architect.yaml` | Bug 根因分析；feature/greenfield 架构设计（ADD） |
| Developer | `workers/developer.yaml` | 代码实现、补丁生成、前端视觉自检 |
| Tester | `workers/tester.yaml` | 测试设计前置、测试执行、视觉回归 |
| Reviewer | `workers/reviewer.yaml` | 质量门禁、failure_class 输出、经验沉淀 |
| Ops Analyst | `workers/ops-analyst.yaml` | incident 诊断、环境资产读取、转 bug 分流 |

`register_resources` 依次 `agt apply` workers/*.yaml 与 teams/*.yaml，并唤醒全部 Worker。
MinIO 共享存储由平台内置（controller 注入 `agentteams` alias 的 mc CLI），worker yaml 不再声明凭据。

## 接入点速查（默认端口）

| 服务 | 地址 |
|------|------|
| MCP Server | `http://127.0.0.1:8090/mcp` |
| Higress 网关（Matrix） | `:18080` |
| Higress Console | `:18001` |
| Element Web | `:18088` |
| PostgreSQL / Redis / Meilisearch / Neo4j | `5432 / 6379 / 7700 / 7687(Bolt)+7474(HTTP)` |

## 跑一个端到端任务

```bash
python scripts/swe_bench_runner.py --issue-index 1
```

runner 自动读取 `deploy/config.env` 与 `~/agentteams-manager.env`（admin 凭据、Matrix 域名）。

## 安全须知

- `config.env`、`db/.env`（含密码）、`secrets/` 均已加入 `.gitignore`，**切勿提交**；本地权限建议 `600`。
- 数据库端口默认仅绑 `127.0.0.1`，**禁止对公网开放**；云服务器安全组只需开放平台网关等必要端口。
