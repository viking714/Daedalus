# Daedalus 依赖版本清单

> **维护规则**：每次新增依赖、升级版本、或修复与版本相关的 bug 时，必须同步更新本文件。
> 各环境（开发 / 测试 / 生产）的版本必须保持一致。

最后更新：2026-09-04

---

## 1. AgentTeams 平台

| 组件 | 版本 | 镜像 / 来源 |
|------|------|-------------|
| agentteams-manager (qwenpaw) | v1.2.3 | `higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-manager-qwenpaw:v1.2.3` |
| agentteams-controller | v1.2.3 | `higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-embedded:v1.2.3` |
| agentteams-worker | v1.2.3 | `higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-worker:v1.2.3` |
| agentteams-dashboard | v1.2.4 | `higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-dashboard:v1.2.4` |

配置入口：`deploy/config.env` → `AGENTTEAMS_VERSION=v1.2.3`

---

## 2. MCP Server — Python 依赖

运行环境：**Python 3.11.6**

| 包名 | 版本 | 约束文件 | 备注 |
|------|------|----------|------|
| mcp[cli] | **1.9.4** | `mcp_server/requirements.txt` | ⚠️ 必须锁定 1.9.4。1.29+ 重构 FastMCP，2.x 重命名为 MCPServer，API 不兼容 |
| psycopg2-binary | 2.9.12 | `mcp_server/requirements.txt` | |
| redis | 8.1.0 | `mcp_server/requirements.txt` | |
| meilisearch | 0.43.0 | `mcp_server/requirements.txt` | |
| neo4j-driver | 5.28.4 | `mcp_server/requirements.txt` | ⚠️ 必须用 neo4j-driver（5.x），不能写 neo4j（6.x 目录冲突） |
| openai | 3.3.1 | `mcp_server/requirements.txt` | |
| sentence-transformers | 6.0.0 | `mcp_server/requirements.txt` | |
| tree-sitter | 0.26.0 | `mcp_server/requirements.txt` | |
| tree-sitter-python | 0.25.0 | `mcp_server/requirements.txt` | |
| uvicorn | 0.52.4 | （mcp 传递依赖） | |
| opentelemetry-api | 1.44.0 | `mcp_server/requirements.txt` | 可选，不装时 telemetry 降级为 no-op |
| opentelemetry-sdk | 1.44.0 | `mcp_server/requirements.txt` | |
| opentelemetry-exporter-otlp | 1.44.0 | `mcp_server/requirements.txt` | |

---

## 3. 数据库栈（Docker 容器）

版本定义在 `deploy/db/docker-compose.db.yml`：

| 服务 | Docker 镜像 | 端口 |
|------|-------------|------|
| PostgreSQL + pgvector | `ankane/pgvector:latest` | 127.0.0.1:5432 |
| Redis | `redis:7-alpine` | 127.0.0.1:6379 |
| Meilisearch | `getmeili/meilisearch:v1.5` | 127.0.0.1:7700 |
| Neo4j | `neo4j:5.18-community` | 127.0.0.1:7474 / 7687 |

---

## 4. LLM & Embedding

| 用途 | 提供商 | 模型 | 备注 |
|------|--------|------|------|
| 默认 LLM | SiliconFlow (openai-compat) | `deepseek-ai/DeepSeek-V4-Pro` | 配置于 `deploy/config.env` |
| Embedding | SiliconFlow | `BAAI/bge-m3` (dim=1024) | 配置于 `deploy/config.env` |

---

## 5. 基础设施

| 组件 | 版本 |
|------|------|
| Docker | 29.7.2 |
| Python（宿主机） | 3.11.6 |

---

## 6. Worker 运行时

| 角色 | 运行时 | 说明 |
|------|--------|------|
| coordinator | qwenpaw | 内置于 manager 镜像 (v1.2.3) |
| architect / developer / tester / reviewer / product-owner / ops-analyst | openclaw | 内置于 worker 镜像 (v1.2.3) |

---

## 版本变更记录

| 日期 | 变更 | 原因 |
|------|------|------|
| 2026-09-04 | mcp[cli] 锁定为 ==1.9.4 | 开放式约束 >=1.0 导致安装到 2.x，FastMCP 重命名为 MCPServer，API 不兼容 |
| 2026-09-04 | server.py 添加 trailing-slash 中间件 | SDK 1.9.4 路由在 /mcp/，coordinator 客户端 POST /mcp 触发 307 重定向，客户端不跟随 |
