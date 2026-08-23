#!/usr/bin/env bash
# guard-team-room-membership.sh — 守护 team 房间成员，防止平台因
# "worker team-binding 丢失"（desiredCount 只算 leader+human）在 reconcile 时
# 把 worker 踢出团队房间（"removed from desired member set"）。
#
# 机制（幂等，可反复执行/常驻）：
#   - 成员缺失 / leave / ban → admin 重新邀请（openclaw worker 自动接受邀请）；
#   - 停留在 invite 状态 → 用该 worker 自己的 Matrix token 加入；
#   - 已 join → 不做任何操作。
#
# 用法:
#   ./deploy/scripts/guard-team-room-membership.sh          # 单次检查
#   ./deploy/scripts/guard-team-room-membership.sh --loop   # 常驻（每 30s 一次）
set -uo pipefail

MATRIX_HS="http://127.0.0.1:18080"
WORKERS="coordinator analyzer fixer tester evaluator"
INTERVAL=30

ADMIN_PW=$(grep HICLAW_ADMIN_PASSWORD ~/hiclaw-manager.env | cut -d= -f2- | tr -d '"\n')
ADMIN_TOK=$(curl -s -X POST "$MATRIX_HS/_matrix/client/r0/login" \
  -H 'Content-Type: application/json' \
  -d "{\"type\":\"m.login.password\",\"identifier\":{\"type\":\"m.id.user\",\"user\":\"admin\"},\"password\":\"$ADMIN_PW\"}" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin).get("access_token",""))')
if [[ -z "$ADMIN_TOK" ]]; then
  echo "guard: admin 登录失败" >&2
  exit 1
fi

ROOM=$(docker exec hiclaw-controller hiclaw get teams -o json 2>/dev/null \
  | python3 -c "import json,sys; print((json.load(sys.stdin).get('teams') or [{}])[0].get('teamRoomID',''))")
if [[ -z "$ROOM" ]]; then
  echo "guard: 无法获取 teamRoomID" >&2
  exit 1
fi
ENC_ROOM=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$ROOM")

worker_token() {
  docker exec hiclaw-controller sh -c \
    "grep WORKER_MATRIX_TOKEN /data/worker-creds/$1.env 2>/dev/null | cut -d= -f2- | tr -d \"\\\"\\\\n\""
}

check_once() {
  local members
  members=$(curl -s "$MATRIX_HS/_matrix/client/r0/rooms/$ENC_ROOM/members" \
    -H "Authorization: Bearer $ADMIN_TOK")
  for w in $WORKERS; do
    local uid="@${w}:matrix-local.hiclaw.io:18080"
    local state
    state=$(echo "$members" | python3 -c "
import json,sys
uid=sys.argv[1]
d=json.load(sys.stdin)
print(next((e['content'].get('membership') for e in d.get('chunk',[]) if e.get('state_key')==uid), 'absent'))
" "$uid")
    case "$state" in
      join) ;;
      invite)
        echo "guard: $w 停留在 invite，用其自身 token 加入"
        local tok; tok=$(worker_token "$w")
        curl -s -X POST "$MATRIX_HS/_matrix/client/r0/join/$ENC_ROOM" \
          -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' -d '{}' >/dev/null
        ;;
      *)
        echo "guard: $w 成员状态=${state}，重新邀请并加入"
        curl -s -X POST "$MATRIX_HS/_matrix/client/r0/rooms/$ENC_ROOM/invite" \
          -H "Authorization: Bearer $ADMIN_TOK" -H 'Content-Type: application/json' \
          -d "{\"user_id\":\"$uid\"}" >/dev/null
        sleep 3
        local tok; tok=$(worker_token "$w")
        curl -s -X POST "$MATRIX_HS/_matrix/client/r0/join/$ENC_ROOM" \
          -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' -d '{}' >/dev/null
        ;;
    esac
  done
}

if [[ "${1:-}" == "--loop" ]]; then
  echo "guard: 常驻模式，房间=${ROOM}，间隔=${INTERVAL}s"
  while true; do
    check_once
    sleep "$INTERVAL"
  done
else
  check_once
  echo "guard: 单次检查完成，房间=$ROOM"
fi
