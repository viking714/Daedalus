#!/usr/bin/env bash
#
# agentteams-ctl.sh — HiClaw / AgentTeams 本地栈的优雅启停脚本
#
# 两层控制：
#   agents  经 `hiclaw worker ensure-ready / sleep` 启停 Worker（保留状态、释放资源）
#   teams   经 `docker start / stop` 启停整个平台（controller + manager + worker 容器）
#
# 用法:
#   agentteams-ctl.sh <agents|teams|all> <start|stop>
#
set -euo pipefail

CONTROLLER="hiclaw-controller"
MANAGER="hiclaw-manager"
WORKER_PREFIX="hiclaw-worker-"

usage() {
  cat <<'EOF'
用法: agentteams-ctl.sh <agents|teams|all> <start|stop>

  agents  start   唤醒（ensure-ready）所有已注册 Worker
  agents  stop    休眠（sleep）所有 Worker，保留状态、释放资源
  teams   start   启动整个 AgentTeams 平台（controller + manager 容器）
  teams   stop    停止整个平台（manager + controller + 所有 worker 容器）
  all     start   先启动平台，再拉起所有 Worker
  all     stop    先休眠所有 Worker，再停止平台

示例:
  agentteams-ctl.sh agents stop    # 收工：让所有角色 Agent 休眠
  agentteams-ctl.sh teams  stop    # 关机：关停整个 HiClaw 平台
  agentteams-ctl.sh all    start   # 开机：平台 + 所有 Worker 一键就绪
EOF
}

# ---- 参数解析 ----
[ "$#" -ne 2 ] && { usage; exit 1; }
TARGET="$1"; ACTION="$2"
case "$ACTION" in
  start|stop) ;;
  *) echo "未知动作: $ACTION" >&2; usage; exit 1 ;;
esac

require_docker() {
  command -v docker >/dev/null 2>&1 || { echo "未找到 docker，请先安装 Docker。" >&2; exit 1; }
}

require_controller_up() {
  docker ps --format '{{.Names}}' | grep -qxF "$CONTROLLER" || {
    echo "错误: $CONTROLLER 容器未运行，无法操作 Agent。请先: agentteams-ctl.sh teams start" >&2
    exit 1
  }
}

# 从 controller 读取已注册 Worker 名（去重）
list_workers() {
  docker exec "$CONTROLLER" hiclaw get workers -o json 2>/dev/null \
    | python3 -c 'import sys, json
try:
    d = json.load(sys.stdin)
    seen = set()
    for w in d.get("workers", []):
        n = w.get("name")
        if n and n not in seen:
            seen.add(n)
            print(n)
except Exception:
    pass' 2>/dev/null \
    || docker exec "$CONTROLLER" hiclaw get workers 2>/dev/null | awk 'NR>1 && $1!=""{print $1}'
}

wait_controller_ready() {
  echo "等待 $CONTROLLER 就绪..."
  local i=0
  while [ "$i" -lt 30 ]; do
    if docker exec "$CONTROLLER" hiclaw status >/dev/null 2>&1; then
      echo "$CONTROLLER 已就绪。"
      return 0
    fi
    sleep 2; i=$((i + 1))
  done
  echo "警告: $CONTROLLER 在 60 秒内未就绪，请检查 'docker logs $CONTROLLER'。" >&2
}

start_agents() {
  require_controller_up
  local names; names="$(list_workers)"
  if [ -z "$names" ]; then
    echo "没有已注册的 Worker（可先 apply 你的 YAML）。"
    return 0
  fi
  echo ">>> 启动所有 Worker Agent:"
  while IFS= read -r n; do
    [ -z "$n" ] && continue
    if docker exec "$CONTROLLER" hiclaw worker ensure-ready --name "$n" >/dev/null 2>&1; then
      echo "  [ok]   $n"
    else
      echo "  [fail] $n" >&2
    fi
  done <<< "$names"
}

stop_agents() {
  require_controller_up
  local names; names="$(list_workers)"
  if [ -z "$names" ]; then
    echo "没有已注册的 Worker。"
    return 0
  fi
  echo ">>> 休眠所有 Worker Agent（保留状态）:"
  while IFS= read -r n; do
    [ -z "$n" ] && continue
    if docker exec "$CONTROLLER" hiclaw worker sleep --name "$n" >/dev/null 2>&1; then
      echo "  [ok]   $n"
    else
      echo "  [fail] $n" >&2
    fi
  done <<< "$names"
}

start_teams() {
  require_docker
  if ! docker ps -a --format '{{.Names}}' | grep -qxF "$CONTROLLER"; then
    echo "错误: 未检测到 $CONTROLLER 容器，请先运行安装脚本 install_agentteams.sh。" >&2
    exit 1
  fi
  echo ">>> 启动 AgentTeams 平台容器..."
  docker start "$CONTROLLER" "$MANAGER" >/dev/null 2>&1 || true
  wait_controller_ready
  echo "平台已启动:"
  echo "  - Higress Console : http://127.0.0.1:18001"
  echo "  - Element Web     : http://127.0.0.1:18088"
  echo "  - AI 网关         : http://127.0.0.1:18080"
}

stop_teams() {
  require_docker
  # 1) 若 controller 仍在运行，先优雅休眠所有 Agent
  if docker ps --format '{{.Names}}' | grep -qxF "$CONTROLLER"; then
    echo ">>> 先优雅休眠所有 Worker Agent..."
    list_workers 2>/dev/null | while IFS= read -r n; do
      [ -z "$n" ] && continue
      docker exec "$CONTROLLER" hiclaw worker sleep --name "$n" >/dev/null 2>&1 \
        && echo "  [ok]   $n" || true
    done || true
  fi
  # 2) 停止所有 worker 容器 + 平台容器
  echo ">>> 停止平台与 worker 容器..."
  # shellcheck disable=SC2046
  docker stop "$MANAGER" "$CONTROLLER" $(docker ps -a -q --filter "name=${WORKER_PREFIX}") >/dev/null 2>&1 || true
  echo "平台已停止。"
}

# ---- 调度 ----
case "$TARGET" in
  agents)
    case "$ACTION" in
      start) start_agents ;;
      stop)  stop_agents ;;
    esac ;;
  teams)
    case "$ACTION" in
      start) start_teams ;;
      stop)  stop_teams ;;
    esac ;;
  all)
    case "$ACTION" in
      start) start_teams; start_agents ;;
      stop)  stop_agents; stop_teams ;;
    esac ;;
  *)
    echo "未知目标: $TARGET" >&2; usage; exit 1 ;;
esac
