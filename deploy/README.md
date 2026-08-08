# AgentTeams 部署与资源目录

本目录包含 AgentTeams 平台的完整部署配置：安装脚本、角色模板、运维脚本和数据库编排。

## 目录结构

```
deploy/
├── install/          # 平台安装（环境变量 + 安装脚本）
├── workers/          # Worker 角色定义（5 个角色 YAML）
├── templates/        # Team / Manager / Human 资源模板
├── scripts/          # 运维脚本（一键搭建、一键启动、平台启停、数据库、隧道）
└── db/               # 数据库编排文件与连接配置
```

## 快速开始

### 首次搭建（仅运行一次）

```bash
./scripts/setup.sh <服务器IP> [PEM路径]
```

自动完成：检查依赖 → 生成配置（随机密码）→ 部署远程数据库 → 安装 AgentTeams → 注册 Worker → 初始化 schema。
搭建完成后编辑两个配置文件填入 API Key 即可。

### 日常启动（一条命令）

```bash
./scripts/start.sh [服务器IP] [PEM路径]
```

自动完成：启动远程数据库 → 建立 SSH 隧道 → 启动领域技能服务 → 启动 AgentTeams → 注册并唤醒 Worker。
启动后即可直接开始工作。停止：`./scripts/start.sh stop`。

### 手动分步操作

如需逐步控制，可按以下顺序手动执行：

```bash
# ① 安装 AgentTeams 平台
cp install/agentteams.env.example install/agentteams.env   # 编辑填入 API Key 等
bash install/install_agentteams.sh

# ② 部署云端数据库
cp db/.env.db.example db/.env.db                           # 编辑填入密码
./scripts/deploy-db-ecs.sh

# ③ 建立 SSH 隧道
./scripts/ecs-tunnel.sh                                    # 前台常驻，Ctrl+C 断开

# ④ 启动平台 + Worker
./scripts/agentteams-ctl.sh all start
```

---

## 1. 平台安装 (install/)

安装官方 AgentTeams（HiClaw）Embedded 环境，并对接本仓库的领域技能服务与 Worker 桥接层。

### 前置条件

- macOS / Linux，已安装 Docker Desktop
- 可访问官方安装脚本（`https://higress.ai/hiclaw/install.sh`）
- 已准备大模型 API Key

### 安装步骤

```bash
cp install/agentteams.env.example install/agentteams.env
```

至少填写 `HICLAW_LLM_API_KEY` 和 `HICLAW_ADMIN_PASSWORD`，然后执行：

```bash
bash install/install_agentteams.sh
```

脚本读取 `agentteams.env`，下载官方安装脚本（已存在则跳过），以预置环境变量的方式调起安装流程。

### 安装后的接入点

| 组件 | 仓库路径 |
|------|----------|
| 领域技能 MCP Server | `domain_skills/` |
| Worker 角色定义 | `deploy/workers/` |
| 团队/管理/人类模板 | `deploy/templates/` |

---

## 2. 角色与资源模板

### 2.1 Worker 角色 (workers/)

5 个角色 YAML 定义了每个 Worker 的模型、MCP 技能、MinIO 环境变量：

| 角色 | 文件 | 职责 |
|------|------|------|
| Manager | `workers/manager.yaml` | 任务分发、状态管理、人工移交（Team Leader） |
| Analyzer | `workers/analyzer.yaml` | 根因分析、代码检索、上下文构建 |
| Fixer | `workers/fixer.yaml` | 修复规划、补丁生成、多文件编辑 |
| Tester | `workers/tester.yaml` | 测试执行、结果裁定 |
| Evaluator | `workers/evaluator.yaml` | 波及评估、签名检查、知识挖掘 |

**Worker 工具分层：**

| 操作类型 | 机制 | 示例 |
|----------|------|------|
| MinIO push/pull | Worker 原生 bash（`mc` CLI） | `mc cp repo.tar.gz minio:bucket/` |
| 文件读写/编辑 | Worker 原生工具 | `cat`, `sed`, `echo` |
| Git 操作 | Worker 原生 bash | `git diff`, `git apply` |
| 运行测试 | Worker 原生 bash | `pytest`, `npm test` |
| 语义搜索 | MCP skill（DB-backed） | `semantic_search` → domain_skills |
| KG 查询 | MCP skill（DB-backed） | `kg_query` → Neo4j |
| 影响面分析 | MCP skill（DB-backed） | `dep_graph_analyzer` → Neo4j |

**MinIO 共享存储：**

每个 Worker YAML 通过 `env:` 声明 MinIO 连接信息，用于 Worker 间文件交换：

```yaml
env:
  - name: MINIO_ENDPOINT
    value: ${MINIO_ENDPOINT:-http://minio:9000}
  - name: MINIO_ACCESS_KEY
    value: ${MINIO_ACCESS_KEY}
  - name: MINIO_SECRET_KEY
    value: ${MINIO_SECRET_KEY}
  - name: MINIO_BUCKET
    value: ${MINIO_BUCKET:-shared-tasks}
```

数据流：Manager `git clone` → `tar czf` → `mc cp` 推送到 MinIO → Worker 拉取 → 本地工作 → 推送产物 → 下游 Worker 拉取。

### 2.2 资源模板 (templates/)

| 文件 | 用途 |
|------|------|
| `templates/rd-defect-team.yaml` | Team 资源模板 — 定义团队组成与协作流程 |
| `templates/default-manager.yaml` | Manager 资源模板 — 定义管理者配置 |
| `templates/admin-human.yaml` | Human 资源模板 — 定义人类参与者配置 |

### 2.3 架构分层

在 AgentTeams 体系下，协同逻辑分为三层：

1. **控制面**（Manager / HiClaw）— 任务分发、房间协作管理、人类介入
2. **运行面**（Worker Runtime）— 解析 `soul` 并执行 Agent 逻辑
   - `runtime: openclaw`：基于 OpenClaw，通用逻辑 Agent（所有 Worker 统一使用）
3. **能力面**（Skills）— 由本仓库提供 MCP Server，Worker 通过 `mcpServers` 声明直接连接

---

## 3. 运维脚本 (scripts/)

### 3.1 一键搭建 — setup.sh（首次使用）

从零搭建完整环境，只需一条命令：

```bash
./scripts/setup.sh <服务器IP> [PEM路径]
```

自动完成：
1. 检查前置依赖（Docker / Python3 / SSH），缺失时引导安装
2. 从 `.example` 模板生成配置文件（自动填充随机密码）
3. 在远程 ECS 部署数据库栈（PostgreSQL / Redis / Meilisearch / Neo4j）
4. 安装 AgentTeams 平台（本地 Docker）
5. 注册 Worker 角色与团队模板
6. 初始化数据库 schema

搭建完成后需手动编辑两个文件填入 API Key：
- `deploy/db/.env.db` → `OPENAI_API_KEY`（Embedding 语义向量）
- `deploy/install/agentteams.env` → `HICLAW_LLM_API_KEY`（LLM 调用）

### 3.2 一键启动 — start.sh（日常使用）

一条命令拉起全部环境，即可开始工作：

```bash
./scripts/start.sh [服务器IP] [PEM路径]
```

自动完成：
1. 启动远程数据库（经 SSH）
2. 建立 SSH 隧道（后台常驻）
3. 启动领域技能服务（后台常驻，端口 8090）
4. 启动 AgentTeams 平台
5. 注册 Worker 角色并唤醒
6. 健康检查 + 状态汇总

停止全部：`./scripts/start.sh stop`

### 3.3 平台启停 — agentteams-ctl.sh

本地 AgentTeams 栈的优雅启停脚本，支持 Worker 和平台两层控制。

```bash
./scripts/agentteams-ctl.sh <agents|teams|all> <start|stop>
```

| 命令 | 作用 |
|------|------|
| `agents start` | 唤醒所有 Worker（`hiclaw worker ensure-ready`） |
| `agents stop` | 休眠所有 Worker（保留状态、释放资源） |
| `teams start` | 启动平台容器，等待 controller 就绪，打印控制台地址 |
| `teams stop` | 先优雅休眠 Agent，再停平台容器（避免状态丢失） |
| `all start` | 先起平台，再拉起所有 Worker（一键开机） |
| `all stop` | 先休眠 Agent，再关平台（一键收工） |

**说明：**
- Worker 列表从 `hiclaw get workers -o json` 动态读取，无需硬编码角色名。
- `teams stop` 先经 controller 优雅休眠 Agent 再停容器，避免直接停止造成状态丢失。
- 平台启动后可访问：Higress Console `:18001` / Element Web `:18088` / AI 网关 `:18080`。

---

## 4. 云端数据库栈

数据库部署在阿里云 ECS（`8.130.191.237`，8G 规格），本地通过 SSH 隧道访问，DB 端口不暴露公网。

### 4.1 数据库组件

| 容器名 | 数据库 | 端口 | 用途 |
|--------|--------|------|------|
| `at-postgres` | PostgreSQL 15 + pgvector | 5432 | 主关系库 + 向量检索 |
| `at-redis` | Redis 7 | 6379 | 缓存 / 队列 / 会话 |
| `at-meili` | Meilisearch 1.5 | 7700 | 全文/语义混合检索 |
| `at-neo4j` | Neo4j 5.18 | 7474 / 7687 | 代码依赖关系图 |

### 4.2 文件说明

| 文件 | 用途 |
|------|------|
| `db/docker-compose.db.yml` | 数据库栈编排（按 8G 内存配置，端口仅绑 `127.0.0.1`） |
| `db/.env.db.example` | 密码/连接模板 |
| `db/.env.db` | 实际密码（已被 gitignore 忽略，**勿提交**） |

### 4.3 部署与运维脚本

#### deploy-db-ecs.sh — 首次部署

把 `docker-compose.db.yml` 与 `.env` 上传到 ECS，设好 Neo4j 前置参数，拉取镜像并启动。

- **何时执行**：仅首次部署，或修改了 compose / 配置后。日常启停请用 `db-ctl.sh`。
- **前置**：私钥 `secrets/ecs-ssh-key.pem` 可 SSH 登录 ECS；已填好 `db/.env.db`。

```bash
cp db/.env.db.example db/.env.db   # 填写密码
./scripts/deploy-db-ecs.sh
```

#### ecs-tunnel.sh — SSH 隧道

把云端 DB 端口映射到本地 `127.0.0.1`。隧道进程必须保持运行，本地程序才能连库。

```bash
./scripts/ecs-tunnel.sh            # 前台常驻，Ctrl+C 断开
```

端口映射：`5432` PostgreSQL / `6379` Redis / `7474` Neo4j HTTP / `7687` Neo4j Bolt / `7700` Meilisearch。

> 想常驻可改为 `nohup ./scripts/ecs-tunnel.sh &`、用 `tmux`，或加 `autossh` 自动重连。

#### db-ctl.sh — 数据库启停

通过 SSH 控制 ECS 上数据库栈，本地不需要装 docker。

```bash
./scripts/db-ctl.sh <start|stop|restart|status>
```

| 命令 | 作用 |
|------|------|
| `start` | 拉起全部库（自动重设 `vm.max_map_count` 并持久化，解决 ECS 重启后 Neo4j 起不来的问题） |
| `stop` | 优雅停止全部库（建议关机 ECS 前执行） |
| `restart` | 先停后起（不停机重启） |
| `status` | 列出 `at-*` 容器状态 |

### 4.4 日常操作速查

```bash
# 首次部署（仅一次）
cp db/.env.db.example db/.env.db
./scripts/deploy-db-ecs.sh

# 日常（ECS 已开机）
./scripts/db-ctl.sh status                       # 查看状态
./scripts/db-ctl.sh start                        # 开机后拉起
./scripts/ecs-tunnel.sh                          # 另开终端建隧道，本地即可连库

# 关机 ECS 前
./scripts/db-ctl.sh stop                         # 优雅停库
```

### 4.5 本地连接地址（隧道建立后均为 `127.0.0.1`）

| 服务 | 端口 | 备注 |
|------|------|------|
| PostgreSQL + pgvector | 5432 | db=`agentteams`，user=`agent` |
| Redis | 6379 | |
| Meilisearch | 7700 | master key 见 `db/.env.db` |
| Neo4j (Bolt) | 7687 | user=`neo4j` |
| Neo4j (HTTP) | 7474 | 浏览器打开查看 |

---

## 安全须知

- ECS 安全组仅开放 `22` 端口。所有 DB 端口经隧道访问，**禁止对公网开放**。
- 私钥 `secrets/ecs-ssh-key.pem` 和 `db/.env.db` 已加入 `.gitignore`，**切勿提交**。本地权限建议 `600`。
- 实际部署时，YAML 中的字段应根据所安装的 AgentTeams 版本做最终校正。


## 跑一个测试

### 测试
cd /path/to/Daedalus && python scripts/swe_bench_runner.py --issue-index 1

