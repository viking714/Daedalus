#!/bin/bash
# AgentTeams 重启后修复脚本
# 用法: bash deploy/scripts/fix-agentteams-runtime.sh
# 功能: 仅在 controller 无法自动处理的环节做兜底修复
#       （Team Room 创建 + 残留插件清理 + SOUL.md 兜底同步）
#
# 设计原则（官方 best practice，见 deploy/docs 与 controller 内官方脚本）：
#   - groupAllowFrom / dm.allowFrom / streaming 现由 controller 的
#     worker-openclaw.json.tmpl 模板在 provisioning 时正确生成
#     （含全部 team 成员 + streaming:off + blockStreaming:true），不再由本脚本覆盖。
#   - peer 权限也可通过 worker YAML 的 channelPolicy.groupAllowExtra 声明式配置
#     （generate-worker-config.sh 会将其 append 到 groupAllowFrom）。
#   - SOUL.md 的权威来源是每个 worker YAML 的 spec.soul 字段；controller 在
#     `hiclaw apply -f deploy/workers/*.yaml` 时据此生成 SOUL.md。本脚本仅做兜底同步。

set -euo pipefail

CONTROLLER="hiclaw-controller"
NETWORK="hiclaw-net"
DOMAIN="matrix-local.hiclaw.io:18080"
ADMIN_USER="admin"
ADMIN_PASS="Transformer123$"

echo "=== AgentTeams Runtime Fix ==="

# 1. 等待 controller 就绪
echo "1. 等待 controller 就绪..."
for i in $(seq 1 30); do
    if docker exec "$CONTROLLER" hiclaw status >/dev/null 2>&1; then
        echo "   Controller ready"
        break
    fi
    sleep 3
done

# 2. 复制 Skills 包到 controller
echo "2. 复制 Skills 包..."
docker exec "$CONTROLLER" mkdir -p /deploy/packages 2>/dev/null || true
docker cp /Users/joeyzhang/Documents/Project/Daedalus/deploy/packages/rd-defect-skills-v0.1.1.zip "$CONTROLLER:/deploy/packages/" 2>/dev/null || true

# 3. 清理 Worker openclaw.json 中残留的失效插件配置
#    groupAllowFrom / dm.allowFrom / streaming 已由 controller 模板在 provisioning 时生成，无需覆盖。
#    此处仅清理历史遗留的 opentelemetry-instrumentation-openclaw 插件引用
#    （镜像更新后该插件已不内置，残留会导致 Config invalid → worker 崩溃循环）。
echo "3. 清理 Worker openclaw.json 残留插件配置..."
for worker in coordinator analyzer fixer tester evaluator; do
    docker exec "$CONTROLLER" mc cat "hiclaw/hiclaw-storage/agents/${worker}/openclaw.json" 2>/dev/null | \
    python3 -c "
import sys, json
data = json.load(sys.stdin)
plugins = data.get('plugins', {})
plugins.get('entries', {}).pop('opentelemetry-instrumentation-openclaw', None)
if 'load' in plugins and isinstance(plugins['load'], dict):
    plugins['load']['paths'] = [p for p in plugins['load'].get('paths', []) if 'opentelemetry-instrumentation-openclaw' not in p]
print(json.dumps(data, indent=2))
" | docker exec -i "$CONTROLLER" mc pipe "hiclaw/hiclaw-storage/agents/${worker}/openclaw.json" 2>/dev/null

    docker exec "hiclaw-worker-${worker}" python3 -c "
import json
with open('/root/hiclaw-fs/agents/${worker}/openclaw.json') as f:
    data = json.load(f)
plugins = data.get('plugins', {})
plugins.get('entries', {}).pop('opentelemetry-instrumentation-openclaw', None)
if 'load' in plugins and isinstance(plugins['load'], dict):
    plugins['load']['paths'] = [p for p in plugins['load'].get('paths', []) if 'opentelemetry-instrumentation-openclaw' not in p]
with open('/root/hiclaw-fs/agents/${worker}/openclaw.json', 'w') as f:
    json.dump(data, f, indent=2)
" 2>/dev/null || true

    echo "   $worker: plugin cleanup done"
done

# 3b. 删除残留插件的实体目录（MinIO + 容器本地）
#     仅清理配置引用不够：插件实体目录（含 node_modules，单 worker >1GiB）留在
#     MinIO 会导致每次容器重启 pull/push 巨量文件；且若未来配置被再次注入引用，
#     实体仍在就会被重新加载。必须 MinIO 与本地同时删除（本地 rm -rf 对运行中
#     容器安全：配置已不加载该插件）。
echo "3b. 删除残留插件实体目录 (MinIO + 本地)..."
for worker in coordinator analyzer fixer tester evaluator; do
    docker exec "$CONTROLLER" mc rm --recursive --force \
        "hiclaw/hiclaw-storage/agents/${worker}/.openclaw/extensions/opentelemetry-instrumentation-openclaw" \
        >/dev/null 2>&1 \
        && echo "   $worker: MinIO 插件目录已删" || echo "   $worker: MinIO 无此目录(跳过)"
    docker exec "hiclaw-worker-${worker}" rm -rf \
        "/root/hiclaw-fs/agents/${worker}/.openclaw/extensions/opentelemetry-instrumentation-openclaw" \
        2>/dev/null \
        && echo "   $worker: 本地插件目录已删" || echo "   $worker: 本地无此目录/容器未运行(跳过)"
done

# 4. 同步 SOUL.md（来源：worker YAML 的 spec.soul，单一可信源）
#    controller 在 `hiclaw apply -f deploy/workers/*.yaml` 时会从 spec.soul 生成 SOUL.md；
#    此处作为兜底，确保运行中容器的 SOUL.md 与 YAML 一致。
echo "4. 同步 SOUL.md (来源: worker YAML spec.soul)..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKERS_DIR="${SCRIPT_DIR}/../workers"
for role in coordinator analyzer fixer tester evaluator; do
    yaml_file="${WORKERS_DIR}/${role}.yaml"
    [ -f "$yaml_file" ] || { echo "   ! 未找到 $yaml_file, 跳过"; continue; }
    soul=$(python3 - "$yaml_file" <<'PY' || true
import yaml, sys
try:
    d = yaml.safe_load(open(sys.argv[1]))
    print(d.get("spec", {}).get("soul", "") or "")
except Exception as e:
    sys.stderr.write("yaml 解析失败: %s\n" % e); sys.exit(2)
PY
)
    [ -z "$soul" ] && { echo "   ! ${role}.yaml 的 spec.soul 为空, 跳过"; continue; }
    container="hiclaw-worker-${role}"
    docker ps --format '{{.Names}}' | grep -q "^${container}$" || { echo "   ! 容器 ${container} 未运行, 跳过"; continue; }
    printf '%s\n' "$soul" | docker exec -i "$container" sh -c "cat > /root/hiclaw-fs/agents/${role}/SOUL.md" 2>/dev/null
    docker exec "$container" cat "/root/hiclaw-fs/agents/${role}/SOUL.md" 2>/dev/null | \
        docker exec -i "$CONTROLLER" mc pipe "hiclaw/hiclaw-storage/agents/${role}/SOUL.md" 2>/dev/null
    echo "   ${role}: SOUL.md 已同步 ($(printf '%s\n' "$soul" | wc -l) 行)"
done

# 5. 创建 Team Room（如果不存在）
#    说明：controller 因 Tuwunel 403 无法自动建/绑 Team Room，此处手动补（遗留项 B 的止血）。
echo "5. 检查/创建 Team Room..."
ADMIN_TOKEN=$(docker exec "$CONTROLLER" curl -s -X POST "http://127.0.0.1:6167/_matrix/client/r0/login" \
  -H "Content-Type: application/json" \
  -d "{\"type\":\"m.login.password\",\"identifier\":{\"type\":\"m.id.user\",\"user\":\"${ADMIN_USER}\"},\"password\":\"${ADMIN_PASS}\"}" 2>&1 | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)

# 检查是否已有 Team Room
TEAM_ROOM=""
for room_id_enc in $(docker exec "$CONTROLLER" curl -s "http://127.0.0.1:6167/_matrix/client/r0/joined_rooms" \
  -H "Authorization: Bearer $ADMIN_TOKEN" 2>&1 | python3 -c "import sys,json; print('\n'.join(json.load(sys.stdin).get('joined_rooms',[])))" 2>/dev/null); do
    member_count=$(docker exec "$CONTROLLER" curl -s "http://127.0.0.1:6167/_matrix/client/r0/rooms/$room_id_enc/members" \
      -H "Authorization: Bearer $ADMIN_TOKEN" 2>&1 | python3 -c "
import sys, json
ms = [ev.get('state_key','') for ev in json.load(sys.stdin).get('chunk',[])]
workers = sum(1 for m in ms if any(w in m for w in ['@coordinator','@analyzer','@fixer','@tester','@evaluator']))
print(workers)
" 2>/dev/null)
    if [ "$member_count" -ge 3 ] 2>/dev/null; then
        TEAM_ROOM="$room_id_enc"
        echo "   Found existing Team Room: $TEAM_ROOM ($member_count workers)"
        break
    fi
done

if [ -z "$TEAM_ROOM" ]; then
    echo "   Creating new Team Room..."
    TEAM_ROOM=$(docker exec "$CONTROLLER" curl -s -X POST "http://127.0.0.1:6167/_matrix/client/r0/createRoom" \
      -H "Authorization: Bearer $ADMIN_TOKEN" \
      -H "Content-Type: application/json" \
      -d "{
        \"name\": \"Team: rd-defect-team\",
        \"topic\": \"SWE-bench defect repair closed-loop team room\",
        \"preset\": \"trusted_private_chat\",
        \"invite\": [
          \"@coordinator:${DOMAIN}\",
          \"@analyzer:${DOMAIN}\",
          \"@fixer:${DOMAIN}\",
          \"@tester:${DOMAIN}\",
          \"@evaluator:${DOMAIN}\"
        ]
      }" 2>&1 | python3 -c "import sys,json; print(json.load(sys.stdin).get('room_id',''))" 2>/dev/null)
    echo "   Team Room created: $TEAM_ROOM"
    sleep 10
fi

# 6. 重启所有 Worker 使配置生效
echo "6. 重启 Workers 使配置生效..."
for worker in coordinator analyzer fixer tester evaluator; do
    docker restart "hiclaw-worker-${worker}" 2>/dev/null || true
done
# 等待 worker 完成启动并上报 readiness；controller provisioning 会在此窗口内
# 从模板正确生成 openclaw.json（含全部 team 成员），无需脚本再覆盖。
echo "   等待 worker 就绪（45s，等待 provisioning 完成）..."
sleep 45

# 7. 验证配置
echo "7. 验证配置..."
for worker in coordinator analyzer fixer tester evaluator; do
    count=$(docker exec "hiclaw-worker-${worker}" python3 -c "
import json
with open('/root/hiclaw-fs/agents/${worker}/openclaw.json') as f:
    data = json.load(f)
print(len(data['channels']['matrix'].get('groupAllowFrom',[])))
" 2>/dev/null)
    echo "   $worker: groupAllowFrom=$count members"
done

# 8. 启动 MinIO Web UI 代理
echo "8. 启动 MinIO Web UI 代理..."
docker rm -f minio-proxy 2>/dev/null || true
docker run -d --name minio-proxy \
    --network "$NETWORK" \
    -p 127.0.0.1:19000:19000 \
    alpine/socat \
    TCP-LISTEN:19000,fork,reuseaddr TCP-CONNECT:"$CONTROLLER":9001 2>/dev/null || true
echo "   MinIO Web UI: http://127.0.0.1:19000"

echo ""
echo "=== Runtime fix complete ==="
echo "Team Room: $TEAM_ROOM"
echo "MinIO UI: http://127.0.0.1:19000 (admin / ${ADMIN_PASS})"
