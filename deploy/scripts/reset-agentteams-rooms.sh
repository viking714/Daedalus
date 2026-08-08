#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}/../.."
DEPLOY_DIR="${REPO_ROOT}/deploy"
LOCAL_MANAGER_ENV="${HOME}/hiclaw-manager.env"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[reset-rooms]${NC} $*"; }
ok()    { echo -e "${GREEN}[  ok]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC} $*"; }
fail()  { echo -e "${RED}[fail]${NC} $*" >&2; }

TEAM_NAME="${TEAM_NAME:-rd-defect-team}"
KEEP_ADMIN_ROOM=1
AUTO_YES=0

usage() {
  cat <<'EOF'
用法:
  ./deploy/scripts/reset-agentteams-rooms.sh [--yes] [--drop-admin-room] [--team TEAM_NAME]

作用:
  1. 备份 hiclaw workspace 里的 room 相关 registry
  2. 删除当前 Team / Worker 资源
  3. 清空本地 registry 中缓存的 room_id
  4. 让 admin 离开并 forget 旧房间（默认保留 Admin Room）
  5. 重新 apply 当前 workers / team 配置并唤醒

示例:
  ./deploy/scripts/reset-agentteams-rooms.sh --yes
  ./deploy/scripts/reset-agentteams-rooms.sh --yes --drop-admin-room
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y)
      AUTO_YES=1
      ;;
    --drop-admin-room)
      KEEP_ADMIN_ROOM=0
      ;;
    --team)
      TEAM_NAME="${2:-}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "未知参数: $1"
      usage
      exit 1
      ;;
  esac
  shift
done

if [[ ! -f "${LOCAL_MANAGER_ENV}" ]]; then
  fail "未找到 ${LOCAL_MANAGER_ENV}"
  exit 1
fi

read_env_value() {
  local key="$1"
  grep -E "^${key}=" "${LOCAL_MANAGER_ENV}" 2>/dev/null | tail -1 | cut -d= -f2- || true
}

WORKSPACE_DIR="$(read_env_value HICLAW_WORKSPACE_DIR)"
WORKSPACE_DIR="${WORKSPACE_DIR:-${HOME}/hiclaw-manager}"
ADMIN_USER="$(read_env_value HICLAW_ADMIN_USER)"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASSWORD="$(read_env_value HICLAW_ADMIN_PASSWORD)"
MATRIX_HOMESERVER_VALUE="$(read_env_value MATRIX_HOMESERVER)"
HICLAW_MATRIX_DOMAIN_VALUE="$(read_env_value HICLAW_MATRIX_DOMAIN)"
MATRIX_URL="${MATRIX_HOMESERVER_VALUE:-http://127.0.0.1:${HICLAW_MATRIX_DOMAIN_VALUE##*:}}"

if [[ -z "${ADMIN_PASSWORD}" ]]; then
  fail "hiclaw-manager.env 中未找到 HICLAW_ADMIN_PASSWORD"
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  fail "未找到 jq，请先安装 jq"
  exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -qxF "hiclaw-controller"; then
  fail "hiclaw-controller 未运行，请先启动 AgentTeams 平台"
  exit 1
fi

login_admin() {
  curl -sf -X POST "${MATRIX_URL}/_matrix/client/v3/login" \
    -H 'Content-Type: application/json' \
    -d "{\"type\":\"m.login.password\",\"identifier\":{\"type\":\"m.id.user\",\"user\":\"${ADMIN_USER}\"},\"password\":\"${ADMIN_PASSWORD}\"}" \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["access_token"])'
}

controller_apply_file() {
  local file_path="$1"
  local remote_path="/tmp/$(basename "${file_path}")"
  docker cp "${file_path}" "hiclaw-controller:${remote_path}" >/dev/null
  docker exec hiclaw-controller hiclaw apply -f "${remote_path}" >/dev/null
  docker exec hiclaw-controller rm -f "${remote_path}" >/dev/null 2>&1 || true
}

controller_delete_if_exists() {
  local kind="$1"
  local name="$2"
  docker exec hiclaw-controller hiclaw delete "${kind}" "${name}" >/dev/null 2>&1 || true
}

urlencode() {
  python3 - <<'PY' "$1"
import sys
import urllib.parse

print(urllib.parse.quote(sys.argv[1], safe=''))
PY
}

WORKERS_JSON="${WORKSPACE_DIR}/workers-registry.json"
TEAMS_JSON="${WORKSPACE_DIR}/teams-registry.json"
STATE_JSON="${WORKSPACE_DIR}/state.json"
BACKUP_DIR="${WORKSPACE_DIR}/state/room-reset-$(date +%Y%m%d-%H%M%S)"

mkdir -p "${BACKUP_DIR}"
for f in "${WORKERS_JSON}" "${TEAMS_JSON}" "${STATE_JSON}"; do
  [[ -f "${f}" ]] && cp "${f}" "${BACKUP_DIR}/"
done
ok "已备份 registry 到 ${BACKUP_DIR}"

TARGET_WORKERS=()
while IFS= read -r yaml; do
  [[ -n "${yaml}" ]] || continue
  TARGET_WORKERS+=("$(basename "${yaml}" .yaml)")
done < <(find "${DEPLOY_DIR}/workers" -maxdepth 1 -type f -name '*.yaml' | sort)
TEAM_TEMPLATE="${DEPLOY_DIR}/templates/${TEAM_NAME}.yaml"

if [[ ${#TARGET_WORKERS[@]} -eq 0 ]]; then
  fail "deploy/workers 下未找到 worker yaml"
  exit 1
fi

if [[ ! -f "${TEAM_TEMPLATE}" ]]; then
  fail "未找到 team 模板: ${TEAM_TEMPLATE}"
  exit 1
fi

info "将重建 team=${TEAM_NAME}，workers=${TARGET_WORKERS[*]}"
if [[ "${AUTO_YES}" != "1" ]]; then
  echo ""
  read -r -p "继续执行 room 重置？[y/N] " reply
  case "${reply}" in
    y|Y|yes|YES) ;;
    *)
      warn "已取消"
      exit 0
      ;;
  esac
fi

ADMIN_TOKEN="$(login_admin)"
ok "admin Matrix 登录成功"

ROOM_IDS_FILE="$(mktemp)"
python3 - <<'PY' "${WORKERS_JSON}" "${TEAMS_JSON}" "${STATE_JSON}" "${ROOM_IDS_FILE}"
import json
import sys
from pathlib import Path

workers_path = Path(sys.argv[1])
teams_path = Path(sys.argv[2])
state_path = Path(sys.argv[3])
out_path = Path(sys.argv[4])
room_ids = set()

def load(path: Path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}

workers = load(workers_path).get("workers", {})
for item in workers.values():
    room_id = item.get("room_id")
    if room_id:
        room_ids.add(room_id)

teams = load(teams_path).get("teams", {})
for item in teams.values():
    for key in ("team_room_id", "leader_dm_room_id"):
        room_id = item.get(key)
        if room_id:
            room_ids.add(room_id)

admin_dm = load(state_path).get("admin_dm_room_id")
if admin_dm:
    room_ids.add(admin_dm)

out_path.write_text("\n".join(sorted(room_ids)) + ("\n" if room_ids else ""))
PY

SYSTEM_ROOMS_JSON="$(mktemp)"
curl -sf "${MATRIX_URL}/_matrix/client/v3/joined_rooms" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  | python3 -c '
import json
import sys
import urllib.parse
import urllib.request

matrix_url, token, keep_admin_room, out_path = sys.argv[1:5]
joined = json.load(sys.stdin).get("joined_rooms", [])
keep_admin_room = keep_admin_room == "1"
selected = []

def room_name(room_id: str) -> str:
    url = f"{matrix_url}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/state/m.room.name"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
        return body.get("name", "")
    except Exception:
        return ""

for room_id in joined:
    name = room_name(room_id)
    lowered = name.lower()
    is_admin_room = "admin room" in lowered
    is_system_room = lowered.startswith("worker: ") or lowered.startswith("leader dm: ") or lowered.startswith("team: ") or lowered.startswith("manager: ")
    if is_admin_room and keep_admin_room:
        continue
    if is_system_room:
        selected.append({"room_id": room_id, "name": name})

with open(out_path, "w") as fh:
    json.dump(selected, fh)
' "${MATRIX_URL}" "${ADMIN_TOKEN}" "${KEEP_ADMIN_ROOM}" "${SYSTEM_ROOMS_JSON}"

REGISTRY_ROOM_IDS=()
if [[ -f "${ROOM_IDS_FILE}" ]]; then
  while IFS= read -r room_id; do
    [[ -n "${room_id}" ]] && REGISTRY_ROOM_IDS+=("${room_id}")
  done < "${ROOM_IDS_FILE}"
fi

TARGET_SYSTEM_ROOM_IDS=()
SYSTEM_ROOM_IDS_FILE="$(mktemp)"
python3 - <<'PY' "${SYSTEM_ROOMS_JSON}" "${SYSTEM_ROOM_IDS_FILE}"
import json
import sys

src_path, out_path = sys.argv[1:3]
with open(src_path) as fh:
    rows = json.load(fh)
with open(out_path, "w") as fh:
    for row in rows:
        room_id = row.get("room_id")
        if room_id:
            fh.write(room_id + "\n")
PY

while IFS= read -r room_id; do
  [[ -n "${room_id}" ]] && TARGET_SYSTEM_ROOM_IDS+=("${room_id}")
done < "${SYSTEM_ROOM_IDS_FILE}"

info "删除 controller 中的 team / worker 资源"
controller_delete_if_exists team "${TEAM_NAME}"
for worker in "${TARGET_WORKERS[@]}"; do
  controller_delete_if_exists worker "${worker}"
done
ok "controller 资源删除完成"

if [[ -f "${WORKERS_JSON}" ]]; then
  tmp="$(mktemp)"
  jq '(.workers // {}) |= with_entries(.value.room_id = null) | .updated_at = (now | todate)' "${WORKERS_JSON}" > "${tmp}"
  mv "${tmp}" "${WORKERS_JSON}"
fi

if [[ -f "${TEAMS_JSON}" ]]; then
  tmp="$(mktemp)"
  jq --arg team "${TEAM_NAME}" 'if .teams[$team] then (.teams[$team].team_room_id, .teams[$team].leader_dm_room_id) |= null else . end | .updated_at = (now | todate)' "${TEAMS_JSON}" > "${tmp}"
  mv "${tmp}" "${TEAMS_JSON}"
fi

if [[ -f "${STATE_JSON}" ]]; then
  tmp="$(mktemp)"
  jq '.admin_dm_room_id = null | .updated_at = ""' "${STATE_JSON}" > "${tmp}"
  mv "${tmp}" "${STATE_JSON}"
fi
ok "本地 registry 里的 room_id 已清空"

leave_and_forget() {
  local room_id="$1"
  [[ -n "${room_id}" ]] || return 0
  local encoded
  encoded="$(urlencode "${room_id}")"
  curl -sf -X POST "${MATRIX_URL}/_matrix/client/v3/rooms/${encoded}/leave" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H 'Content-Type: application/json' \
    -d '{}' >/dev/null 2>&1 || true

  curl -sf -X POST "${MATRIX_URL}/_matrix/client/v3/rooms/${encoded}/forget" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H 'Content-Type: application/json' \
    -d '{}' >/dev/null 2>&1 || true
}

info "让 admin 离开旧系统房间"
ALL_TARGET_ROOMS=()
for room_id in "${REGISTRY_ROOM_IDS[@]:-}"; do
  [[ -n "${room_id}" ]] && ALL_TARGET_ROOMS+=("${room_id}")
done
for room_id in "${TARGET_SYSTEM_ROOM_IDS[@]:-}"; do
  [[ -n "${room_id}" ]] && ALL_TARGET_ROOMS+=("${room_id}")
done

if [[ ${#ALL_TARGET_ROOMS[@]} -gt 0 ]]; then
  printf '%s\n' "${ALL_TARGET_ROOMS[@]}" | awk '!seen[$0]++' | while IFS= read -r room_id; do
    leave_and_forget "${room_id}"
  done
fi
ok "admin 已离开旧系统房间"

info "重新 apply workers / manager / team 配置"
for yaml in "${DEPLOY_DIR}"/workers/*.yaml; do
  controller_apply_file "${yaml}"
done
controller_apply_file "${DEPLOY_DIR}/templates/default-manager.yaml"
controller_apply_file "${TEAM_TEMPLATE}"
ok "controller 配置已重建"

bash "${SCRIPT_DIR}/agentteams-ctl.sh" agents start >/dev/null 2>&1 || true
ok "Worker 已唤醒"

sleep 5

info "当前 team / worker room 状态"
docker exec hiclaw-controller hiclaw get teams -o json | python3 -c '
import json
import sys

data = json.load(sys.stdin)
for team in data.get("teams", []):
    print(f"TEAM {team.get(\"name\")}: room={team.get(\"teamRoomID\")} leader_dm={team.get(\"leaderDMRoomID\")}")
'

docker exec hiclaw-controller hiclaw get workers -o json | python3 -c '
import json
import sys

data = json.load(sys.stdin)
seen = set()
for worker in data.get("workers", []):
    name = worker.get("name")
    room_id = worker.get("roomID")
    key = (name, room_id)
    if not name or key in seen:
        continue
    seen.add(key)
    print(f"WORKER {name}: room={room_id}")
'

echo ""
ok "room 重置完成。现在重新运行测试时，会使用新建的 team room。"
