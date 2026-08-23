#!/usr/bin/env bash
# ============================================================================
# install.sh — Daedalus / AgentTeams 统一安装（一次性 / 可重跑升级）
# ----------------------------------------------------------------------------
# 将所有组件安装到本机（脚本所在机器）：
#   - 数据库栈（PostgreSQL+pgvector / Redis / Meilisearch / Neo4j，docker compose；
#     声明 XXX_EXTERNAL=1 则跳过安装直接复用外部连接）
#   - 领域技能 MCP Server（本机进程，仓库 mcp_server/ 源码运行）
#   - AgentTeams 平台 v1.2.x（官方安装器，AGENTTEAMS_* 契约）
#   - 技能包推送 + Worker / Manager / Team 资源注册
#   - （可选）AgentLoop 可观测（映射为安装器 AGENTTEAMS_CMS_*）
# 配置全部来自 deploy/config.env（模板：config.env.example）。
#
# 用法: bash deploy/scripts/install.sh
# ============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

load_config
require_config
require_python3
detect_compose

echo -e "\n${BOLD}=========================================="
echo -e "  Daedalus / AgentTeams 统一安装（目标：本机）"
echo -e "  平台版本：${AGENTTEAMS_VERSION}"
echo -e "  系统：${DISTRO_NAME}$([[ "${IS_ALIBABA_CLOUD_LINUX}" == "1" ]] && echo " (Alibaba Cloud Linux)")$([[ "${IS_RHEL_LIKE}" == "1" ]] && echo " / RHEL 系")"
echo -e "==========================================${NC}"

ensure_ecs_docker
resolve_linux_mcp_host
generate_db_env
sync_mcp_code
deploy_db_stack
install_agentteams

step "等待 controller 就绪并推送技能包"
if wait_controller_ready; then
  ok "controller 已就绪"
  push_skills_package
  register_resources
  launch_mcp_server
  verify_deployment
else
  warn "controller 未在预期时间内就绪，请在本机检查 'docker ps'；可稍后运行 ./run.sh start 继续"
fi

print_summary
echo ""
echo "安装完成。日常启停: bash deploy/scripts/run.sh start | stop | restart | status"
