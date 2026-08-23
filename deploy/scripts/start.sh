#!/usr/bin/env bash
# ============================================================
# start.sh — 一键启动（日常使用）
# ============================================================
# 一条命令拉起全部环境，即可开始工作：
#   1. 启动远程数据库（经 SSH）
#   2. 建立 SSH 隧道（后台常驻）
#   3. 启动领域技能服务（后台常驻）
#   4. 启动 AgentTeams 平台
#   5. 注册 Worker 角色并唤醒
#   6. 健康检查 + 状态汇总
#
# 用法:
#   ./scripts/start.sh [服务器IP] [PEM路径]
#
# 参数（均可省略，使用默认值）:
#   服务器IP    远程 ECS IP（默认: 从 db/.env.db 读取或 8.130.191.237）
#   PEM路径    SSH 私钥路径（默认: secrets/ecs-ssh-key.pem）
#
# 停止:
#   ./scripts/start.sh stop
# ============================================================
set -euo pipefail

# ---- 颜色 ----
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[start]${NC} $*"; }
ok()    { echo -e "${GREEN}[  ok]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC} $*"; }
fail()  { echo -e "${RED}[fail]${NC} $*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}/../.."
DEPLOY_DIR="${REPO_ROOT}/deploy"
AGENTTEAMS_ENV="${DEPLOY_DIR}/install/agentteams.env"
LOCAL_MANAGER_ENV="${HOME}/hiclaw-manager.env"
PROJECT_PYTHON="${CODE_INTEL_PYTHON:-/opt/anaconda3/envs/GoAI/bin/python}"
if [[ ! -x "${PROJECT_PYTHON}" ]]; then
  PROJECT_PYTHON="${CODE_INTEL_PYTHON:-python3}"
fi
LEGACY_WORKERS=(planner reasoner retriever verifier editor impact-analyst)

manager_expected_model() {
  if [[ -f "${AGENTTEAMS_ENV}" ]]; then
    grep -E '^HICLAW_DEFAULT_MODEL=' "${AGENTTEAMS_ENV}" | head -1 | sed 's/^HICLAW_DEFAULT_MODEL=//'
  fi
}

manager_expected_password() {
  if docker ps --format '{{.Names}}' | grep -qxF "hiclaw-controller"; then
    # 注意：/data/worker-creds/manager.env 在 manager 容器尚未重建时不存在，
    # 直接读取会在 set -euo pipefail 下让整段脚本退出，跳过
    # ensure_hiclaw_manager_aligned（导致 manager 无法重建）。这里必须容错。
    docker exec hiclaw-controller sh -lc "sed -n 's/^WORKER_PASSWORD=\\\"\\(.*\\)\\\"$/\\1/p' /data/worker-creds/manager.env 2>/dev/null" 2>/dev/null | head -1 || true
  fi
}

sync_local_manager_env_value() {
  local key="$1"
  local expected_value="$2"
  [[ -z "${key}" || -z "${expected_value}" ]] && return 0
  [[ -f "${LOCAL_MANAGER_ENV}" ]] || return 0
  MANAGER_ENV_PATH="${LOCAL_MANAGER_ENV}" ENV_KEY="${key}" EXPECTED_VALUE="${expected_value}" python3 - <<'PY'
import os
import re
from pathlib import Path

path = Path(os.environ["MANAGER_ENV_PATH"])
key = os.environ["ENV_KEY"]
expected = os.environ["EXPECTED_VALUE"]
text = path.read_text()
line = f"{key}={expected}"
if f"{key}=" in text:
    text = re.sub(rf"^{re.escape(key)}=.*$", line, text, count=1, flags=re.M)
else:
    text += "\n" + line + "\n"
path.write_text(text)
PY
}

local_manager_env_value() {
  local key="$1"
  [[ -z "${key}" || ! -f "${LOCAL_MANAGER_ENV}" ]] && return 0
  # 在 set -euo pipefail 下，key 缺失时 grep 返回 1 会让整条管道返回 1，
  # 进而导致调用处的 `local x="$(local_manager_env_value ...)"` 触发 set -e 中断。
  # 追加 `|| true` 保证 key 缺失时返回 0，让上层 `${var:-default}` 兜底逻辑生效。
  grep -E "^${key}=" "${LOCAL_MANAGER_ENV}" | head -1 | sed "s/^${key}=//" || true
}

manager_container_env_value() {
  local key="$1"
  docker inspect hiclaw-manager --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | grep -E "^${key}=" | head -1 | sed "s/^${key}=//"
}

find_agentteams_tunnel_pid() {
  local pid=""
  for port in 5432 6379 7474 7687 7700; do
    while IFS= read -r candidate; do
      [[ -z "${candidate}" ]] && continue
      if ps -p "${candidate}" -o comm= 2>/dev/null | grep -qx "ssh"; then
        pid="${candidate}"
        break 2
      fi
    done < <(lsof -t -nP -iTCP:${port} -sTCP:LISTEN 2>/dev/null | sort -u)
  done
  [[ -n "${pid}" ]] && echo "${pid}"
}

stop_agentteams_tunnels() {
  local pids=()
  local pid
  if [[ -f /tmp/agentteams-tunnel.pid ]]; then
    pid="$(cat /tmp/agentteams-tunnel.pid 2>/dev/null || true)"
    [[ -n "${pid}" ]] && pids+=("${pid}")
  fi
  while IFS= read -r pid; do
    [[ -n "${pid}" ]] && pids+=("${pid}")
  done < <(for port in 5432 6379 7474 7687 7700; do
    lsof -t -nP -iTCP:${port} -sTCP:LISTEN 2>/dev/null || true
  done | sort -u)

  if [[ "${#pids[@]}" -eq 0 ]]; then
    rm -f /tmp/agentteams-tunnel.pid
    return 0
  fi

  local stopped_any=0
  local seen=""
  for pid in "${pids[@]}"; do
    [[ -z "${pid}" || " ${seen} " == *" ${pid} "* ]] && continue
    seen="${seen} ${pid}"
    if ps -p "${pid}" -o comm= 2>/dev/null | grep -qx "ssh"; then
      kill "${pid}" 2>/dev/null || true
      stopped_any=1
    fi
  done

  if [[ "${stopped_any}" -eq 1 ]]; then
    sleep 1
    for pid in ${seen}; do
      if ps -p "${pid}" -o comm= 2>/dev/null | grep -qx "ssh"; then
        kill -9 "${pid}" 2>/dev/null || true
      fi
    done
  fi
  rm -f /tmp/agentteams-tunnel.pid
}

manager_container_state() {
  docker inspect hiclaw-manager --format '{{.State.Status}}' 2>/dev/null || true
}

manager_gateway_snapshot() {
  python3 - <<'PY'
import json, subprocess

def run(cmd):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return {
            "code": proc.returncode,
            "stdout": (proc.stdout or "").strip()[:300],
            "stderr": (proc.stderr or "").strip()[:300],
        }
    except Exception as exc:
        return {"error": str(exc)}

print(json.dumps({
    "host_gateway_18080": run(["sh", "-lc", "nc -z 127.0.0.1 18080"]),
    "manager_local_8080": run(["docker", "exec", "hiclaw-manager", "sh", "-lc", "curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8080"]),
    "manager_aigw_8080": run(["docker", "exec", "hiclaw-manager", "sh", "-lc", "curl -sS -o /dev/null -w '%{http_code}' http://aigw-local.hiclaw.io:8080"]),
}))
PY
}

manager_container_drift_reason() {
  if ! docker ps -a --format '{{.Names}}' | grep -qxF "hiclaw-manager"; then
    echo "missing"
    return 0
  fi

  local local_password local_model local_runtime
  local container_password container_model container_runtime
  local container_hiclaw_runtime container_gateway_url container_matrix_url
  local_password="$(local_manager_env_value HICLAW_MANAGER_PASSWORD)"
  local_model="$(local_manager_env_value HICLAW_DEFAULT_MODEL)"
  local_runtime="$(local_manager_env_value HICLAW_MANAGER_RUNTIME)"
  container_password="$(manager_container_env_value HICLAW_MANAGER_PASSWORD)"
  container_model="$(manager_container_env_value HICLAW_DEFAULT_MODEL)"
  container_runtime="$(manager_container_env_value HICLAW_MANAGER_RUNTIME)"
  container_hiclaw_runtime="$(manager_container_env_value HICLAW_RUNTIME)"
  container_gateway_url="$(manager_container_env_value HICLAW_AI_GATEWAY_URL)"
  container_matrix_url="$(manager_container_env_value HICLAW_MATRIX_URL)"

  if [[ -n "${local_password}" && "${local_password}" != "${container_password}" ]]; then
    echo "password_mismatch"
  elif [[ -n "${local_model}" && "${local_model}" != "${container_model}" ]]; then
    echo "model_mismatch"
  elif [[ -n "${local_runtime}" && "${local_runtime}" != "${container_runtime}" ]]; then
    echo "runtime_mismatch"
  elif [[ -z "${container_hiclaw_runtime}" || -z "${container_gateway_url}" || -z "${container_matrix_url}" ]]; then
    echo "missing_platform_env"
  else
    echo ""
  fi
}

recreate_hiclaw_manager() {
  local image network_mode workdir restart_name workspace_dir host_share_dir manager_runtime
  local merged_env_file
  # 注意：manager 容器可能已被删除（docker inspect 失败）。多行命令替换
  # `$(docker inspect ... || echo ...)` 会捕获到前导换行符，导致 docker run
  # 报 "invalid reference format"。这里改用 `|| true` + `${var:-default}` 回退。
  local _img _net _wd _restart
  _img="$(docker inspect hiclaw-manager --format '{{.Config.Image}}' 2>/dev/null || true)"
  _net="$(docker inspect hiclaw-manager --format '{{.HostConfig.NetworkMode}}' 2>/dev/null || true)"
  _wd="$(docker inspect hiclaw-manager --format '{{.Config.WorkingDir}}' 2>/dev/null || true)"
  _restart="$(docker inspect hiclaw-manager --format '{{.HostConfig.RestartPolicy.Name}}' 2>/dev/null || true)"
  image="${_img:-higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/hiclaw-manager:latest}"
  network_mode="${_net:-hiclaw-net}"
  workdir="${_wd:-/root/manager-workspace}"
  restart_name="${_restart:-unless-stopped}"
  workspace_dir="$(local_manager_env_value HICLAW_WORKSPACE_DIR)"
  host_share_dir="$(local_manager_env_value HICLAW_HOST_SHARE_DIR)"
  manager_runtime="$(local_manager_env_value HICLAW_MANAGER_RUNTIME)"
  workspace_dir="${workspace_dir:-${HOME}/hiclaw-manager}"
  host_share_dir="${host_share_dir:-${HOME}}"
  manager_runtime="${manager_runtime:-openclaw}"
  merged_env_file="$(mktemp)"

  mkdir -p "${workspace_dir}"

  python3 - "${merged_env_file}" "${LOCAL_MANAGER_ENV}" <<'PY'
import json
import os
import pathlib
import re
import subprocess
import sys

target_path = pathlib.Path(sys.argv[1])
local_env_path = pathlib.Path(sys.argv[2])

env_map = {}
worker_env = {}
controller_env = {}

# 继承所有 HICLAW_* 环境变量（由上方 `source ${AGENTTEAMS_ENV}` 注入）。
# 关键：manager 容器可能已不存在（docker inspect 返回空），此时
# HICLAW_MATRIX_DOMAIN / HICLAW_ADMIN_USER / HICLAW_CMS_* 等变量只能从
# agentteams.env 继承，否则 manager 启动会报 "HICLAW_MATRIX_DOMAIN is required"。
for _key, _val in os.environ.items():
    if _key.startswith("HICLAW_"):
        env_map[_key] = _val
try:
    out = subprocess.check_output(
        ["docker", "inspect", "hiclaw-manager", "--format", "{{json .Config.Env}}"],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()
    existing = json.loads(out) if out else []
except Exception:
    existing = []

for item in existing:
    if "=" not in item:
        continue
    key, value = item.split("=", 1)
    env_map[key] = value

for container_name, target_env in [("hiclaw-worker-manager", worker_env), ("hiclaw-controller", controller_env)]:
    try:
        out = subprocess.check_output(
            ["docker", "inspect", container_name, "--format", "{{json .Config.Env}}"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        items = json.loads(out) if out else []
    except Exception:
        items = []
    for item in items:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        target_env[key] = value

if local_env_path.exists():
    pattern = re.compile(r"^([A-Z0-9_]+)=(.*)$")
    for raw in local_env_path.read_text().splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        match = pattern.match(raw)
        if not match:
            continue
        key, value = match.groups()
        if key in {
            "HICLAW_MANAGER_PASSWORD",
            "HICLAW_DEFAULT_MODEL",
            "HICLAW_MANAGER_RUNTIME",
            "HICLAW_HOST_SHARE_DIR",
            "HICLAW_WORKSPACE_DIR",
            "HICLAW_LLM_PROVIDER",
            "HICLAW_LLM_API_KEY",
            "HICLAW_OPENAI_BASE_URL",
            "HICLAW_MODEL_CONTEXT_WINDOW",
            "HICLAW_MODEL_MAX_TOKENS",
            "HICLAW_MODEL_REASONING",
            "HICLAW_MODEL_VISION",
            "HICLAW_ADMIN_USER",
            "HICLAW_ADMIN_PASSWORD",
            "HICLAW_MANAGER_GATEWAY_KEY",
            "HICLAW_AI_GATEWAY_DOMAIN",
            "HICLAW_MATRIX_DOMAIN",
            "HICLAW_MATRIX_CLIENT_DOMAIN",
            "HICLAW_FS_DOMAIN",
            "HICLAW_CONSOLE_DOMAIN",
            "HICLAW_MINIO_USER",
            "HICLAW_MINIO_PASSWORD",
            "HICLAW_GITHUB_TOKEN",
            "HICLAW_CMS_TRACES_ENABLED",
            "HICLAW_CMS_ENDPOINT",
            "HICLAW_CMS_LICENSE_KEY",
            "HICLAW_CMS_PROJECT",
            "HICLAW_CMS_WORKSPACE",
            "HICLAW_CMS_SERVICE_NAME",
        }:
            env_map[key] = value

platform_defaults = {
    "HICLAW_RUNTIME": "k8s",
    "HICLAW_MATRIX_URL": worker_env.get("HICLAW_MATRIX_URL", "http://hiclaw-controller:6167"),
    "HICLAW_AI_GATEWAY_URL": worker_env.get("HICLAW_AI_GATEWAY_URL", "http://aigw-local.hiclaw.io:8080"),
    "HICLAW_CONTROLLER_URL": worker_env.get("HICLAW_CONTROLLER_URL", "http://hiclaw-controller:8090"),
    "HICLAW_FS_ENDPOINT": worker_env.get("HICLAW_FS_ENDPOINT", "http://hiclaw-controller:9000"),
    "HICLAW_FS_BUCKET": worker_env.get("HICLAW_FS_BUCKET", "hiclaw-storage"),
    "HICLAW_FS_ACCESS_KEY": worker_env.get("HICLAW_FS_ACCESS_KEY", "manager"),
    "HICLAW_FS_SECRET_KEY": worker_env.get("HICLAW_FS_SECRET_KEY", ""),
    "HICLAW_STORAGE_PREFIX": worker_env.get("HICLAW_STORAGE_PREFIX", "hiclaw/hiclaw-storage"),
    "HICLAW_AUTH_TOKEN": worker_env.get("HICLAW_AUTH_TOKEN", ""),
}
for key, value in platform_defaults.items():
    if value:
        env_map[key] = value

for key in {"HICLAW_LOCAL_ONLY", "HICLAW_ELEMENT_HOMESERVER_URL"}:
    env_map.pop(key, None)

lines = [f"{key}={value}" for key, value in sorted(env_map.items()) if key not in {"HOSTNAME"}]
target_path.write_text("\n".join(lines) + "\n")
PY

  docker stop hiclaw-manager >/dev/null 2>&1 || true
  docker rm hiclaw-manager >/dev/null 2>&1 || true
  docker run -d \
    --name hiclaw-manager \
    --env-file "${merged_env_file}" \
    -e HOME=/root/manager-workspace \
    -w "${workdir}" \
    -e HOST_ORIGINAL_HOME="${host_share_dir}" \
    -e HICLAW_MANAGER_RUNTIME="${manager_runtime}" \
    -v "${workspace_dir}:/root/manager-workspace" \
    -v "${host_share_dir}:/host-share" \
    --network "${network_mode}" \
    --restart "${restart_name}" \
    "${image}" >/dev/null
  rm -f "${merged_env_file}"
}

ensure_hiclaw_manager_aligned() {
  local drift_reason state
  drift_reason="$(manager_container_drift_reason)"
  state="$(manager_container_state)"

  if [[ -n "${drift_reason}" ]]; then
    recreate_hiclaw_manager
    ok "hiclaw-manager 已按最新本地 env 重建"
  elif [[ -z "${state}" ]]; then
    # 容器不存在（被删除/从未创建）：docker start 会静默失败，必须重建。
    recreate_hiclaw_manager
    ok "hiclaw-manager 容器缺失，已重建"
  elif [[ "${state}" != "running" ]]; then
    docker start hiclaw-manager >/dev/null 2>&1 || true
  fi
}

controller_apply_file() {
  local file_path="$1"
  local remote_path="/tmp/$(basename "${file_path}")"
  docker cp "${file_path}" "hiclaw-controller:${remote_path}" >/dev/null
  docker exec hiclaw-controller hiclaw apply -f "${remote_path}"
  docker exec hiclaw-controller rm -f "${remote_path}" >/dev/null 2>&1 || true
}

controller_delete_worker_if_exists() {
  local worker_name="$1"
  if docker exec hiclaw-controller hiclaw get workers -o json 2>/dev/null | grep -q "\"name\": \"${worker_name}\""; then
    docker exec hiclaw-controller hiclaw delete worker "${worker_name}" >/dev/null 2>&1 || true
  fi
}

controller_team_state() {
  docker exec hiclaw-controller hiclaw get teams -o json 2>/dev/null | \
    python3 -c 'import json,sys
data=json.load(sys.stdin)
team=next((t for t in data.get("teams", []) if t.get("name")=="rd-defect-team" or t.get("teamName")=="rd-defect-team"), None)
if not team:
    print("missing")
elif int(team.get("totalWorkers") or 0) <= 0:
    print("incomplete")
else:
    print("ready")'
}

ensure_rd_defect_team() {
  local expected_model="$1"
  local team_state
  team_state="$(controller_team_state 2>/dev/null || echo missing)"

  if [[ "${team_state}" != "ready" ]]; then
    docker exec hiclaw-controller hiclaw delete team rd-defect-team >/dev/null 2>&1 || true
    if docker exec hiclaw-controller hiclaw create team \
      --name rd-defect-team \
      --leader-name coordinator \
      --leader-model "${expected_model}" \
      --workers analyzer,fixer,tester,evaluator >/dev/null 2>&1; then
      ok "Team 已创建: rd-defect-team"
    else
      warn "Team 创建失败: rd-defect-team"
    fi
  else
    ok "Team 已存在且成员完整: rd-defect-team"
  fi

  docker exec hiclaw-controller hiclaw worker ensure-ready --name coordinator >/dev/null 2>&1 || true
}

sync_controller_resources() {
  local expected_model
  local expected_password
  expected_model="$(manager_expected_model)"
  if [[ -n "${expected_model}" ]]; then
    sync_local_manager_env_value "HICLAW_DEFAULT_MODEL" "${expected_model}"
    docker exec hiclaw-controller hiclaw update manager --name default --model "${expected_model}" --runtime openclaw >/dev/null 2>&1 || true
  fi

  for yaml in "${DEPLOY_DIR}"/workers/*.yaml; do
    local name
    name=$(basename "${yaml}" .yaml)
    if controller_apply_file "${yaml}" >/dev/null 2>&1; then
      ok "Worker 已注册/更新: ${name}"
    else
      warn "Worker 注册失败: ${name}"
    fi
  done

  if controller_apply_file "${DEPLOY_DIR}/teams/default-manager.yaml" >/dev/null 2>&1; then
    ok "Manager 已注册/更新: default"
  else
    warn "Manager 注册失败: default"
  fi

  if [[ -n "${expected_model}" ]]; then
    ensure_rd_defect_team "${expected_model}"
  else
    warn "缺少默认模型，跳过 Team 重建"
  fi

  expected_password="$(manager_expected_password)"
  if [[ -n "${expected_password}" ]]; then
    sync_local_manager_env_value "HICLAW_MANAGER_PASSWORD" "${expected_password}"
  fi

  for worker in "${LEGACY_WORKERS[@]}"; do
    controller_delete_worker_if_exists "${worker}"
  done
}

# ---- 停止模式 ----
if [[ "${1:-}" == "stop" ]]; then
  info "停止全部服务..."

  # 停止领域技能服务
  SKILL_PID=$(cat /tmp/agentteams-skills.pid 2>/dev/null || true)
  if [[ -n "${SKILL_PID}" ]] && kill -0 "${SKILL_PID}" 2>/dev/null; then
    kill "${SKILL_PID}" 2>/dev/null || true
    ok "领域技能服务已停止 (PID ${SKILL_PID})"
  fi

  # 停止 SSH 隧道
  TUNNEL_PID="$(find_agentteams_tunnel_pid || true)"
  if [[ -n "${TUNNEL_PID}" ]]; then
    stop_agentteams_tunnels
    ok "SSH 隧道已停止"
  else
    rm -f /tmp/agentteams-tunnel.pid
  fi

  # 停止 AgentTeams
  if docker ps --format '{{.Names}}' | grep -qxF "hiclaw-controller"; then
    bash "${SCRIPT_DIR}/agentteams-ctl.sh" all stop 2>/dev/null || true
    ok "AgentTeams 已停止"
  fi

  rm -f /tmp/agentteams-skills.pid /tmp/agentteams-tunnel.pid
  echo ""
  ok "全部服务已停止"
  exit 0
fi

# ---- 参数 ----
HOST="${1:-8.130.191.237}"
KEY="${2:-${REPO_ROOT}/secrets/ecs-ssh-key.pem}"
USER="root"
REMOTE_DIR="/opt/agentteams-db"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15"
SSH_TUNNEL_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes"

# macOS OpenSSH 10.x 兼容性: 使用 nc 做 ProxyCommand 绕过 TLS-in-TCP 分段问题
_ssh_cmd() {
  ssh -i "${KEY}" ${SSH_OPTS} -o "ProxyCommand nc %h %p" "${USER}@${HOST}" "$@"
}

DB_ENV="${DEPLOY_DIR}/db/.env.db"

# ---- 前置检查 ----
echo ""
echo "=========================================="
echo "  AgentTeams 一键启动"
echo "=========================================="
echo ""
info "服务器: ${USER}@${HOST}"
info "私钥:   ${KEY}"
info "Python: ${PROJECT_PYTHON}"
echo ""

# 检查必要文件
for f in "${KEY}" "${DB_ENV}"; do
  if [[ ! -f "${f}" ]]; then
    fail "文件不存在: ${f}"
    fail "请先运行 ./scripts/setup.sh 完成首次环境搭建"
    exit 1
  fi
done

# ============================================================
# Step 1: 启动远程数据库
# ============================================================
info "Step 1/5: 启动远程数据库..."

_ssh_cmd bash -s <<'START_DB'
set -e
sysctl -w vm.max_map_count=262144
grep -q '^vm.max_map_count' /etc/sysctl.d/99-neo4j.conf 2>/dev/null || \
  echo 'vm.max_map_count=262144' > /etc/sysctl.d/99-neo4j.conf
cd /opt/agentteams-db
docker compose up -d
START_DB
ok "远程数据库已启动"
echo ""

# ============================================================
# Step 2: 建立 SSH 隧道（后台）
# ============================================================
info "Step 2/5: 建立 SSH 隧道..."

TUNNEL_PID="$(find_agentteams_tunnel_pid || true)"
if [[ -n "${TUNNEL_PID}" ]]; then
  echo "${TUNNEL_PID}" > /tmp/agentteams-tunnel.pid
  ok "SSH 隧道已在运行 (PID ${TUNNEL_PID})"
else
  stop_agentteams_tunnels
  ssh -i "${KEY}" ${SSH_TUNNEL_OPTS} -o "ProxyCommand nc %h %p" -N -f \
    -L 5432:127.0.0.1:5432 \
    -L 6379:127.0.0.1:6379 \
    -L 7474:127.0.0.1:7474 \
    -L 7687:127.0.0.1:7687 \
    -L 7700:127.0.0.1:7700 \
    "${USER}@${HOST}"

  # 记录 PID
  TUNNEL_PID="$(find_agentteams_tunnel_pid || true)"
  if [[ -n "${TUNNEL_PID}" ]]; then
    echo "${TUNNEL_PID}" > /tmp/agentteams-tunnel.pid
    ok "SSH 隧道已建立 (PID ${TUNNEL_PID})"
  else
    warn "隧道进程可能未启动成功，请检查端口"
  fi
fi

# 等待隧道可用
sleep 2
if nc -z 127.0.0.1 5432 2>/dev/null; then
  ok "数据库端口可达 (127.0.0.1:5432)"
else
  warn "数据库端口暂不可达，隧道可能仍在建立中"
fi
echo ""

# ============================================================
# Step 3: 启动领域技能服务（后台）
# ============================================================
info "Step 3/5: 启动领域技能 MCP Server..."

SKILL_PID=$(cat /tmp/agentteams-skills.pid 2>/dev/null || true)
if [[ -n "${SKILL_PID}" ]] && kill -0 "${SKILL_PID}" 2>/dev/null; then
  ok "领域技能 MCP Server 已在运行 (PID ${SKILL_PID})"
else
  # 加载 DB 环境变量
  set -a; source "${DB_ENV}"; set +a
  # 加载 AgentLoop / OTel 环境变量（MCP_OTEL_ENABLED + AGENTLOOP_* 凭证）
  # 否则 telemetry.py 的 init_telemetry 读不到开关，自动降级为 no-op，不上报 Span
  if [[ -f "${AGENTTEAMS_ENV}" ]]; then
    set -a; source "${AGENTTEAMS_ENV}"; set +a
  fi

  cd "${REPO_ROOT}"
  nohup "${PROJECT_PYTHON}" mcp_server/server.py > /tmp/agentteams-skills.log 2>&1 &
  SKILL_PID=$!
  echo "${SKILL_PID}" > /tmp/agentteams-skills.pid
  ok "领域技能 MCP Server 已启动 (PID ${SKILL_PID}, 端口 8090)"
fi

# 等待服务就绪（MCP Server 使用 JSON-RPC，通过端口探测 + 进程检查）
for i in $(seq 1 15); do
  if nc -z 127.0.0.1 8090 2>/dev/null && kill -0 "${SKILL_PID}" 2>/dev/null; then
    ok "领域技能 MCP Server 健康检查通过（端口 8090 可达）"
    break
  fi
  if (( i == 15 )); then
    warn "领域技能 MCP Server 未在 15s 内就绪，请检查 /tmp/agentteams-skills.log"
  fi
  sleep 1
done
echo ""

# ============================================================
# Step 4: 启动 AgentTeams 平台
# ============================================================
info "Step 4/5: 启动 AgentTeams 平台..."

if docker ps --format '{{.Names}}' | grep -qxF "hiclaw-controller"; then
  ok "AgentTeams 平台已在运行"
else
  bash "${SCRIPT_DIR}/agentteams-ctl.sh" all start 2>/dev/null || {
    warn "AgentTeams 启动异常，请检查 docker ps"
  }
fi
echo ""

# ============================================================
# Step 5: 注册 Worker 并唤醒
# ============================================================
info "Step 5/5: 注册 Worker 角色并唤醒..."

# 确保 HICLAW_CMS_* / AGENTLOOP_* 等变量已加载。
# 注意：上面的 else 分支只在 MCP Server 未运行时才 source agentteams.env，
# MCP 已运行时不会执行；这里补一次，保证后续 manager 重建能继承 CMS 配置。
if [[ -f "${AGENTTEAMS_ENV}" ]]; then
  set -a; source "${AGENTTEAMS_ENV}"; set +a
fi

# 等待 controller 就绪
for i in $(seq 1 30); do
  if docker exec hiclaw-controller hiclaw status >/dev/null 2>&1; then
    break
  fi
  if (( i == 30 )); then
    warn "controller 未就绪，跳过 Worker 注册"
    break
  fi
  sleep 2
done

if docker exec hiclaw-controller hiclaw status >/dev/null 2>&1; then
  sync_controller_resources
  ensure_hiclaw_manager_aligned

  # 唤醒所有 Worker
  bash "${SCRIPT_DIR}/agentteams-ctl.sh" agents start 2>/dev/null || true
  ok "Worker 已唤醒"

  # 修复运行时配置（controller 重启会覆盖 worker 的 openclaw.json / SOUL.md，
  # 且 Team Room 需手动重建）。失败不阻断，可稍后单独重跑该脚本。
  if [[ -f "${SCRIPT_DIR}/fix-agentteams-runtime.sh" ]]; then
    info "Step 5.5: 修复运行时配置（openclaw.json / SOUL.md / Team Room）..."
    bash "${SCRIPT_DIR}/fix-agentteams-runtime.sh" || \
      warn "运行时配置修复失败，请稍后手动执行: bash deploy/scripts/fix-agentteams-runtime.sh"
  fi
fi
echo ""

# ============================================================
# 状态汇总
# ============================================================
echo "=========================================="
echo -e "  ${GREEN}全部就绪，可以开始工作!${NC}"
echo "=========================================="
echo ""
echo "服务状态:"

# 领域技能服务
if nc -z 127.0.0.1 8090 2>/dev/null; then
  echo -e "  ${GREEN}✓${NC} MCP Server      http://127.0.0.1:8090/mcp"
else
  echo -e "  ${RED}✗${NC} MCP Server      未响应"
fi

# SSH 隧道
if nc -z 127.0.0.1 5432 2>/dev/null; then
  echo -e "  ${GREEN}✓${NC} SSH 隧道        PostgreSQL:5432 Redis:6379 Neo4j:7474/7687 Meili:7700"
else
  echo -e "  ${RED}✗${NC} SSH 隧道        未连通"
fi

# AgentTeams 平台
if docker ps --format '{{.Names}}' | grep -qxF "hiclaw-controller"; then
  echo -e "  ${GREEN}✓${NC} AgentTeams       Console :18001  Element :18088  Gateway :18080"
else
  echo -e "  ${RED}✗${NC} AgentTeams       未运行"
fi

GATEWAY_SNAPSHOT="$(manager_gateway_snapshot)"
if echo "${GATEWAY_SNAPSHOT}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("ok" if d.get("manager_local_8080", {}).get("code") == 0 else "bad")' 2>/dev/null | grep -qx "ok"; then
  echo -e "  ${GREEN}✓${NC} Manager→Gateway  hiclaw-manager 内部可访问 127.0.0.1:8080"
else
  echo -e "  ${YELLOW}!${NC} Manager→Gateway  hiclaw-manager 内部访问 127.0.0.1:8080 异常"
  echo "    snapshot: ${GATEWAY_SNAPSHOT}"
fi

# Worker 状态
if docker exec hiclaw-controller hiclaw status >/dev/null 2>&1; then
  WORKER_COUNT=$(docker exec hiclaw-controller hiclaw get workers -o json 2>/dev/null | \
    python3 -c "import sys,json; print(len(json.load(sys.stdin).get('workers',[])))" 2>/dev/null || echo "?")
  echo -e "  ${GREEN}✓${NC} Worker 角色      ${WORKER_COUNT} 个已注册"
else
  echo -e "  ${YELLOW}?${NC} Worker 角色      controller 未就绪"
fi

echo ""
echo "停止全部: ./scripts/start.sh stop"
echo ""
