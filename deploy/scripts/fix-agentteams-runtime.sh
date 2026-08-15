#!/bin/bash
# AgentTeams 重启后修复脚本
# 用法: bash deploy/scripts/fix-agentteams-runtime.sh
# 功能: 修复 controller provisioning 覆盖的运行时配置

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

# 3. 更新所有 Worker 的 openclaw.json (groupAllowFrom + streaming off)
TEAM_MEMBERS='["@admin:matrix-local.hiclaw.io:18080","@manager:matrix-local.hiclaw.io:18080","@coordinator:matrix-local.hiclaw.io:18080","@analyzer:matrix-local.hiclaw.io:18080","@fixer:matrix-local.hiclaw.io:18080","@tester:matrix-local.hiclaw.io:18080","@evaluator:matrix-local.hiclaw.io:18080"]'

echo "3. 更新 Worker openclaw.json (groupAllowFrom + streaming off)..."
for worker in coordinator analyzer fixer tester evaluator; do
    # 更新 MinIO 中的配置
    docker exec "$CONTROLLER" mc cat "hiclaw/hiclaw-storage/agents/${worker}/openclaw.json" 2>/dev/null | \
    python3 -c "
import sys, json
data = json.load(sys.stdin)
mtx = data['channels']['matrix']
mtx['dm']['allowFrom'] = ${TEAM_MEMBERS}
mtx['groupAllowFrom'] = ${TEAM_MEMBERS}
mtx['streaming'] = 'off'
mtx['blockStreaming'] = True
print(json.dumps(data, indent=2))
" | docker exec -i "$CONTROLLER" mc pipe "hiclaw/hiclaw-storage/agents/${worker}/openclaw.json" 2>/dev/null

    # 更新容器内本地配置
    docker exec "hiclaw-worker-${worker}" python3 -c "
import json
with open('/root/hiclaw-fs/agents/${worker}/openclaw.json') as f:
    data = json.load(f)
mtx = data['channels']['matrix']
mtx['dm']['allowFrom'] = ${TEAM_MEMBERS}
mtx['groupAllowFrom'] = ${TEAM_MEMBERS}
mtx['streaming'] = 'off'
mtx['blockStreaming'] = True
with open('/root/hiclaw-fs/agents/${worker}/openclaw.json', 'w') as f:
    json.dump(data, f, indent=2)
" 2>/dev/null || true

    echo "   $worker: updated"
done

# 4. 更新 SOUL.md
echo "4. 更新 SOUL.md..."
COORD="@coordinator:${DOMAIN}"

# Coordinator SOUL
docker exec hiclaw-worker-coordinator python3 -c "
soul = '''# Coordinator - SWE-bench Pipeline Team Leader

You are the Team Leader of rd-defect-team in this Matrix room. Delegate ALL work to specialist Workers via @mentions.

## Team Members (use full Matrix ID in @mentions)
- @analyzer:${DOMAIN} - Root cause analysis
- @fixer:${DOMAIN} - Code fix implementation
- @tester:${DOMAIN} - Test execution
- @evaluator:${DOMAIN} - Patch evaluation

## CRITICAL OUTPUT
Your FINAL message MUST contain exactly: Verdict: SUCCESS or Verdict: FAIL

## Workflow (USE MATRIX MENTIONS, NOT sessions_spawn)
1. Receive task from admin. Parse repo path, commit, problem.
2. Send message mentioning @analyzer:${DOMAIN} with task: analyze root cause
3. WAIT for @analyzer to reply.
4. Send message mentioning @fixer:${DOMAIN} with analysis: implement fix
5. WAIT for @fixer to reply with patch.
6. Send message mentioning @tester:${DOMAIN} with patch: run tests
7. WAIT for @tester to reply with results.
8. Send message mentioning @evaluator:${DOMAIN}: evaluate patch
9. WAIT for @evaluator to reply.
10. Output: Verdict: SUCCESS or Verdict: FAIL with patch.

## ABSOLUTE RULES
- ALWAYS use Matrix @mentions to delegate. NEVER use sessions_spawn, exec, or process tools.
- You are a COORDINATOR only. Do NOT analyze, fix, test, or evaluate code yourself.
- WAIT for each worker to reply before moving to next phase.
- The final message MUST have Verdict: SUCCESS or Verdict: FAIL.
'''
with open('/root/hiclaw-fs/agents/coordinator/SOUL.md', 'w') as f:
    f.write(soul)
" 2>/dev/null || true

docker exec hiclaw-worker-coordinator cat /root/hiclaw-fs/agents/coordinator/SOUL.md 2>/dev/null | \
  docker exec -i "$CONTROLLER" mc pipe "hiclaw/hiclaw-storage/agents/coordinator/SOUL.md" 2>/dev/null

# Sub-worker SOULs
for worker in analyzer fixer tester evaluator; do
    role_desc=""
    case $worker in
        analyzer) role_desc="Analyze root cause of the defect. Do NOT fix code." ;;
        fixer) role_desc="Implement code fix. Save patch to shared/tasks/{id}/patch.diff." ;;
        tester) role_desc="Apply patch to repo. Run tests. Report pass/fail." ;;
        evaluator) role_desc="Evaluate patch quality. Give PASS/FAIL assessment." ;;
    esac
    
    docker exec "hiclaw-worker-${worker}" python3 -c "
soul = '''# ${worker^} - SWE-bench Specialist

You receive tasks from the Coordinator. When done, you MUST @mention ${COORD} with results.

## Your Job
${role_desc}

## Rules
- ALWAYS @mention ${COORD} when done with your results
- Be concise
'''
with open('/root/hiclaw-fs/agents/${worker}/SOUL.md', 'w') as f:
    f.write(soul)
" 2>/dev/null || true

    docker exec "hiclaw-worker-${worker}" cat "/root/hiclaw-fs/agents/${worker}/SOUL.md" 2>/dev/null | \
      docker exec -i "$CONTROLLER" mc pipe "hiclaw/hiclaw-storage/agents/${worker}/SOUL.md" 2>/dev/null
    echo "   $worker: SOUL.md updated"
done

# 5. 创建 Team Room（如果不存在）
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
sleep 20

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
