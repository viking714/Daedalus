#!/usr/bin/env bash
# ============================================================================
# run.sh — Daedalus / AgentTeams 日常运行（启动 / 停止 / 状态）
# ----------------------------------------------------------------------------
# 所有组件运行在「本机」（脚本所在机器）；本机优先，无需 SSH 远程操控。
# 子命令:
#   start    启动数据库 + MCP Server + AgentTeams 平台，注册资源并校验部署
#   stop     优雅停止 AgentTeams 平台 + MCP Server + 数据库（外部库不停止）
#   restart  stop 后 start
#   status   打印部署状态汇总
#
# 用法: bash deploy/scripts/run.sh <start|stop|restart|status>
# ============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

load_config
require_python3
detect_compose

usage() {
  cat <<'EOF'
用法: bash deploy/scripts/run.sh <start|stop|restart|status>

  start    启动 数据库 + MCP Server + AgentTeams，注册资源并校验部署
  stop     优雅停止 平台 + MCP Server + 数据库（外部库不停止）
  restart  stop 后 start
  status   打印部署状态汇总
EOF
}

do_start() {
  resolve_linux_mcp_host
  db_stack_start
  launch_mcp_server
  platform_start
  if wait_controller_ready; then
    register_resources
    patch_worker_runtime
    verify_deployment
    expose_minio_console
  else
    warn "controller 未就绪，跳过资源注册与校验；请检查本机 'docker ps'"
  fi
  print_summary
}

do_stop() {
  platform_stop
  stop_mcp_server
  db_stack_stop
  ok "全部已停止（外部数据库不在此管理）"
}

cmd="${1:-}"
case "${cmd}" in
  start)   ensure_ecs_docker; do_start ;;
  stop)    do_stop ;;
  restart) do_stop; ensure_ecs_docker; do_start ;;
  status)  print_summary ;;
  *) usage; exit 1 ;;
esac
