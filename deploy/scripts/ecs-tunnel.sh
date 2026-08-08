#!/usr/bin/env bash
# 建立 本地 -> 阿里云 ECS 的 SSH 隧道
# 把云端数据库端口映射到本地 127.0.0.1, 这样本地 AgentTeams / 脚本连 127.0.0.1:5432
# 等就像连本地, 且云端 DB 端口不暴露公网 (安全组只开 22)。
#
# 用法: ./scripts/ecs-tunnel.sh   (常驻, Ctrl+C 断开)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 私钥位于仓库根目录 secrets/ecs-ssh-key.pem (从 deploy/scripts 上溯两级)
KEY="${SCRIPT_DIR}/../../secrets/ecs-ssh-key.pem"
HOST="8.130.191.237"
USER="root"

if [[ ! -f "${KEY}" ]]; then
  echo "错误: 找不到私钥 ${KEY}" >&2
  exit 1
fi

echo "建立 SSH 隧道 -> ${USER}@${HOST}"
echo "本地映射:"
echo "  5432  -> PostgreSQL"
echo "  6379  -> Redis"
echo "  7474  -> Neo4j (HTTP, 浏览器可开)"
echo "  7687  -> Neo4j (Bolt)"
echo "  7700  -> Meilisearch"
echo "按 Ctrl+C 断开。"
echo

exec ssh -i "${KEY}" \
  -o StrictHostKeyChecking=no \
  -o ConnectTimeout=15 \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -N \
  -L 5432:127.0.0.1:5432 \
  -L 6379:127.0.0.1:6379 \
  -L 7474:127.0.0.1:7474 \
  -L 7687:127.0.0.1:7687 \
  -L 7700:127.0.0.1:7700 \
  "${USER}@${HOST}"
