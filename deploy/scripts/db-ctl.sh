#!/usr/bin/env bash
# 本地运行: 通过 SSH 控制阿里云 ECS 上的 AgentTeams 数据库栈 (启/停/重启/状态)
#
# 典型用法:
#   关机 ECS 前:   ./scripts/db-ctl.sh stop     # 优雅停库, 再关 ECS
#   ECS 开机后:   ./scripts/db-ctl.sh start     # 拉起全部库 (自动重设 Neo4j 前置)
#   查看状态:     ./scripts/db-ctl.sh status
#   重启(不停机): ./scripts/db-ctl.sh restart
#
# 说明:
#   - 纯 SSH 控制, 本地不需要 docker。
#   - start 会先重设 vm.max_map_count=262144 (ECS 重启后该项会复位, 否则 Neo4j 起不来)
#     并写入 /etc/sysctl.d/99-neo4j.conf 做持久化。
#   - 容器 restart 策略为 unless-stopped, 若 ECS 异常重启 docker 会自动拉起;
#     此时再跑 start 是幂等的 (已是 running 则无操作)。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 私钥位于仓库根目录 secrets/ecs-ssh-key.pem (从 deploy/scripts 上溯两级)
KEY="${SCRIPT_DIR}/../../secrets/ecs-ssh-key.pem"
HOST="8.130.191.237"
USER="root"
REMOTE_DIR="/opt/agentteams-db"

if [[ ! -f "${KEY}" ]]; then
  echo "错误: 找不到私钥 ${KEY}" >&2
  exit 1
fi

SSH="ssh -i ${KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=15 ${USER}@${HOST}"

usage() {
  echo "用法: $0 {start|stop|restart|status}"
  echo "  start   启动 ECS 上的全部数据库 (自动设置 Neo4j 前置参数)"
  echo "  stop    优雅停止全部数据库 (建议关机 ECS 前执行)"
  echo "  restart 先停后起"
  echo "  status  查看容器状态与健康"
  exit 1
}

case "${1:-}" in
  start)
    echo "==> 启动 ECS 数据库栈 (${HOST})"
    $SSH bash -s <<'EOF'
set -e
# Neo4j 需要 vm.max_map_count >= 262144, 重启后会复位, 这里重设并持久化
sysctl -w vm.max_map_count=262144
if ! grep -q 'vm.max_map_count' /etc/sysctl.d/99-neo4j.conf 2>/dev/null; then
  echo 'vm.max_map_count=262144' > /etc/sysctl.d/99-neo4j.conf
fi
cd /opt/agentteams-db
docker compose up -d
sleep 3
docker compose ps
echo "启动完成。"
EOF
    ;;
  stop)
    echo "==> 优雅停止 ECS 数据库栈 (${HOST})"
    $SSH bash -s <<'EOF'
set -e
cd /opt/agentteams-db
docker compose stop
echo "已停止。可安全关闭 ECS。"
EOF
    ;;
  restart)
    echo "==> 重启 ECS 数据库栈 (${HOST})"
    $SSH bash -s <<'EOF'
set -e
cd /opt/agentteams-db
docker compose stop
sysctl -w vm.max_map_count=262144
docker compose up -d
sleep 3
docker compose ps
EOF
    ;;
  status)
    echo "==> ECS 数据库栈状态 (${HOST})"
    $SSH "docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep at- || echo '(无 at- 容器)'"
    ;;
  *)
    usage
    ;;
esac
