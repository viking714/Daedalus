#!/usr/bin/env bash
# 把云端数据库栈部署到阿里云 ECS (8.130.191.237)
# 本地执行: 上传 compose + .env, 在 ECS 上设好 Neo4j 前置, 拉起服务。
#
# 前置:
#   - ./secrets/ecs-ssh-key.pem 私钥可 SSH 登录 ECS
#   - 已 cp .env.db.example .env.db 并填好密码
#
# 用法: ./scripts/deploy-db-ecs.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 私钥位于仓库根目录 secrets/ecs-ssh-key.pem (从 deploy/scripts 上溯两级)
KEY="${SCRIPT_DIR}/../../secrets/ecs-ssh-key.pem"
HOST="8.130.191.237"
USER="root"
REMOTE_DIR="/opt/agentteams-db"
ENV_FILE="${SCRIPT_DIR}/../db/.env.db"

if [[ ! -f "${KEY}" ]]; then
  echo "错误: 找不到私钥 ${KEY}" >&2
  exit 1
fi
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "错误: 找不到 ${ENV_FILE}, 请先 cp .env.db.example .env.db 并填好密码" >&2
  exit 1
fi

echo "== 1. ECS 创建目录 + 上传文件 =="
ssh -i "${KEY}" "${USER}@${HOST}" "mkdir -p ${REMOTE_DIR}"
scp -i "${KEY}" "${SCRIPT_DIR}/../db/docker-compose.db.yml" "${USER}@${HOST}:${REMOTE_DIR}/docker-compose.yml"
scp -i "${KEY}" "${ENV_FILE}" "${USER}@${HOST}:${REMOTE_DIR}/.env"

echo "== 2. ECS 前置 (vm.max_map_count for Neo4j) + 启动 =="
ssh -i "${KEY}" "${USER}@${HOST}" bash -s <<'EOF'
set -e
sysctl -w vm.max_map_count=262144
grep -q '^vm.max_map_count' /etc/sysctl.conf || echo 'vm.max_map_count=262144' >> /etc/sysctl.conf
cd /opt/agentteams-db
docker compose pull
docker compose up -d
echo "== 容器状态 =="
docker compose ps
EOF

echo
echo "完成. 本地访问: 先跑 ./scripts/ecs-tunnel.sh 建立隧道, 再连 127.0.0.1:<端口>"
