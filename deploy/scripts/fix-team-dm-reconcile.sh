#!/usr/bin/env bash
# fix-team-dm-reconcile.sh — 修复 team 卡在
# "provision team rooms: reconcile leader DM membership: ... 403 M_FORBIDDEN" 的场景。
#
# 根因（平台竞态）：controller 的 team reconciler 在 coordinator agent 加入
# leader DM 房间之前就执行成员列表检查，得到 403 后将 team 置为 Failed 且不再
# 自动重试。本脚本：
#   1. 从错误消息中提取 DM 房间 ID；
#   2. 用 coordinator 的 Matrix token 加入该房间；
#   3. 重新 apply team yaml 触发 reconcile。
#
# 用法: ./deploy/scripts/fix-team-dm-reconcile.sh [team-yaml-path]
set -euo pipefail

TEAM_YAML="${1:-/tmp/apply-rd-defect-team.yaml}"
MATRIX_HS="http://127.0.0.1:18080"

msg=$(docker exec hiclaw-controller hiclaw get teams -o json 2>/dev/null \
  | python3 -c "import json,sys; print((json.load(sys.stdin).get('teams') or [{}])[0].get('message',''))")

if [[ "$msg" != *"reconcile leader DM membership"* ]]; then
  echo "team 未处于 DM membership 失败状态，无需修复。"
  exit 0
fi

# 错误消息中的房间 ID 可能被截断；完整 ID 以 :<port> 结尾，这里补全域名端口
room=$(printf '%s' "$msg" | sed -n 's/.*list members of \(![A-Za-z0-9]\{8,\}\):matrix-local\.hiclaw\.io.*/\1/p')
if [[ -z "$room" ]]; then
  echo "无法从 team message 中解析 DM 房间 ID: $msg" >&2
  exit 1
fi
full_room="${room}:matrix-local.hiclaw.io:18080"
echo "DM room: $full_room"

token=$(docker exec hiclaw-controller sh -c \
  'grep WORKER_MATRIX_TOKEN /data/worker-creds/coordinator.env | cut -d= -f2- | tr -d "\"\n"')
if [[ -z "$token" ]]; then
  echo "coordinator.env 中无 WORKER_MATRIX_TOKEN" >&2
  exit 1
fi

enc=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$full_room")
join_resp=$(curl -s -X POST "$MATRIX_HS/_matrix/client/r0/join/$enc" \
  -H "Authorization: Bearer $token" -H "Content-Type: application/json" -d '{}')
echo "join resp: $join_resp"

docker exec hiclaw-controller hiclaw apply -f "$TEAM_YAML"
echo "已重新触发 team reconcile，请稍后轮询: hiclaw get teams -o json"
