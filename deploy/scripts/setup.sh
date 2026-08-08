#!/usr/bin/env bash
# ============================================================
# setup.sh — 首次环境搭建（仅运行一次）
# ============================================================
# 完成从零到可用环境的全部初始化：
#   1. 检查前置依赖（Docker / Python3 / SSH）
#   2. 初始化配置文件（从 .example 模板生成，自动填充随机密码）
#   3. 在远程 ECS 部署数据库栈（PostgreSQL / Redis / Meilisearch / Neo4j）
#   4. 安装 AgentTeams 平台（本地 Docker）
#   5. 初始化数据库 schema
#
# 用法:
#   ./scripts/setup.sh <服务器IP> [PEM路径]
#
# 参数:
#   服务器IP    远程 ECS 的公网 IP（如 8.130.191.237）
#   PEM路径    SSH 私钥路径（默认: secrets/ecs-ssh-key.pem）
#
# 示例:
#   ./scripts/setup.sh 8.130.191.237
#   ./scripts/setup.sh 8.130.191.237 /path/to/my-key.pem
# ============================================================
set -euo pipefail

# ---- 颜色 ----
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[setup]${NC} $*"; }
ok()    { echo -e "${GREEN}[  ok]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC} $*"; }
fail()  { echo -e "${RED}[fail]${NC} $*" >&2; }

# ---- 参数解析 ----
HOST="${1:-}"
if [[ -z "${HOST}" ]]; then
  echo "用法: $0 <服务器IP> [PEM路径]"
  echo ""
  echo "参数:"
  echo "  服务器IP    远程 ECS 的公网 IP"
  echo "  PEM路径    SSH 私钥路径（默认: secrets/ecs-ssh-key.pem）"
  echo ""
  echo "示例:"
  echo "  $0 8.130.191.237"
  echo "  $0 8.130.191.237 ~/.ssh/my-key.pem"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}/../.."
DEPLOY_DIR="${REPO_ROOT}/deploy"
AGENTTEAMS_ENV="${DEPLOY_DIR}/install/agentteams.env"
LOCAL_MANAGER_ENV="${HOME}/hiclaw-manager.env"
PROJECT_PYTHON="${DOMAIN_SKILLS_PYTHON:-/opt/anaconda3/envs/GoAI/bin/python}"
if [[ ! -x "${PROJECT_PYTHON}" ]]; then
  PROJECT_PYTHON="${DOMAIN_SKILLS_PYTHON:-python3}"
fi
LEGACY_WORKERS=(planner reasoner retriever verifier editor impact-analyst)

KEY="${2:-${REPO_ROOT}/secrets/ecs-ssh-key.pem}"
USER="root"
REMOTE_DIR="/opt/agentteams-db"

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15"
SSH_TUNNEL_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes"
SSH="ssh -i ${KEY} ${SSH_OPTS} ${USER}@${HOST}"
SCP="scp -i ${KEY} ${SSH_OPTS}"

# ---- 辅助函数 ----
generate_password() {
  # 生成 20 位随机密码（字母+数字）
  LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom 2>/dev/null | head -c 20 || \
    python3 -c "import secrets,string; print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(20)))"
}

wait_for() {
  local desc="$1" cmd="$2" max_wait="${3:-60}"
  local i=0
  while ! eval "${cmd}" >/dev/null 2>&1; do
    if (( i >= max_wait )); then
      fail "${desc} 在 ${max_wait}s 内未就绪"
      return 1
    fi
    sleep 1; i=$((i + 1))
  done
  ok "${desc} 已就绪"
}

manager_expected_model() {
  if [[ -f "${AGENTTEAMS_ENV}" ]]; then
    grep -E '^HICLAW_DEFAULT_MODEL=' "${AGENTTEAMS_ENV}" | head -1 | sed 's/^HICLAW_DEFAULT_MODEL=//'
  fi
}

manager_expected_password() {
  if docker ps --format '{{.Names}}' | grep -qxF "hiclaw-controller"; then
    docker exec hiclaw-controller sh -lc "sed -n 's/^WORKER_PASSWORD=\\\"\\(.*\\)\\\"$/\\1/p' /data/worker-creds/manager.env 2>/dev/null" | head -1
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
  grep -E "^${key}=" "${LOCAL_MANAGER_ENV}" | head -1 | sed "s/^${key}=//"
}

manager_container_env_value() {
  local key="$1"
  docker inspect hiclaw-manager --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | grep -E "^${key}=" | head -1 | sed "s/^${key}=//"
}

manager_container_state() {
  docker inspect hiclaw-manager --format '{{.State.Status}}' 2>/dev/null || true
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
  image="$(docker inspect hiclaw-manager --format '{{.Config.Image}}' 2>/dev/null || \
    echo 'higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/hiclaw-manager:latest')"
  network_mode="$(docker inspect hiclaw-manager --format '{{.HostConfig.NetworkMode}}' 2>/dev/null || echo 'hiclaw-net')"
  workdir="$(docker inspect hiclaw-manager --format '{{.Config.WorkingDir}}' 2>/dev/null || echo '/root/manager-workspace')"
  restart_name="$(docker inspect hiclaw-manager --format '{{.HostConfig.RestartPolicy.Name}}' 2>/dev/null || echo 'unless-stopped')"
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
import pathlib
import re
import subprocess
import sys

target_path = pathlib.Path(sys.argv[1])
local_env_path = pathlib.Path(sys.argv[2])

env_map = {}
worker_env = {}
controller_env = {}
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
  elif [[ "${state}" != "running" && -n "${state}" ]]; then
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
      --leader-name manager \
      --leader-model "${expected_model}" \
      --workers analyzer,fixer,tester,evaluator >/dev/null 2>&1; then
      ok "Team rd-defect-team 已创建"
    else
      warn "Team 创建失败（可能仍有残留状态）"
    fi
  else
    ok "Team rd-defect-team 已存在且成员完整"
  fi

  docker exec hiclaw-controller hiclaw worker ensure-ready --name manager >/dev/null 2>&1 || true
}

# ============================================================
echo ""
echo "=========================================="
echo "  AgentTeams 环境首次搭建"
echo "=========================================="
echo ""
info "服务器: ${USER}@${HOST}"
info "私钥:   ${KEY}"
info "仓库:   ${REPO_ROOT}"
echo ""

# ---- Step 1: 前置检查 ----
info "Step 1/6: 检查前置依赖..."

command -v bash   >/dev/null 2>&1 || { fail "bash 未安装"; exit 1; }
command -v curl   >/dev/null 2>&1 || { fail "curl 未安装"; exit 1; }
command -v ssh    >/dev/null 2>&1 || { fail "ssh 未安装"; exit 1; }

# Python3
if command -v python3 >/dev/null 2>&1; then
  ok "python3 $(python3 --version 2>&1 | awk '{print $2}')"
else
  fail "python3 未安装（领域技能服务需要）"
  exit 1
fi

# Docker
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    ok "Docker 已安装且守护进程运行中"
  else
    warn "Docker 已安装但守护进程未运行，尝试启动..."
    if [[ "$(uname -s)" == "Darwin" ]]; then
      open -a Docker
    elif command -v systemctl >/dev/null 2>&1; then
      sudo systemctl start docker || sudo service docker start || true
    fi
    wait_for "Docker 守护进程" "docker info" 60 || { fail "Docker 启动失败，请手动启动后重试"; exit 1; }
  fi
else
  warn "Docker 未安装"
  read -r -p "是否自动安装 Docker? [y/N]: " _ans
  case "${_ans}" in
    y|Y)
      if [[ "$(uname -s)" == "Darwin" ]]; then
        if command -v brew >/dev/null 2>&1; then
          brew install --cask docker
        else
          fail "未检测到 Homebrew，请先安装: https://brew.sh"
          fail "或手动下载: https://www.docker.com/products/docker-desktop/"
          exit 1
        fi
      elif [[ "$(uname -s)" == "Linux" ]] && [ -f /etc/os-release ]; then
        sudo apt-get update -qq
        sudo apt-get install -y -qq docker.io
        sudo systemctl enable --now docker
      else
        fail "不支持的系统 $(uname -s)，请手动安装 Docker"
        exit 1
      fi
      wait_for "Docker 守护进程" "docker info" 60 || exit 1
      ;;
    *) fail "已取消，请手动安装 Docker 后重试"; exit 1 ;;
  esac
fi

# SSH 连通性
if [[ ! -f "${KEY}" ]]; then
  fail "SSH 私钥不存在: ${KEY}"
  fail "请将私钥放到 secrets/ecs-ssh-key.pem 或通过第二个参数指定路径"
  exit 1
fi
if ! ${SSH} "echo ok" >/dev/null 2>&1; then
  fail "无法 SSH 连接到 ${USER}@${HOST}（请检查 IP、私钥和安全组）"
  exit 1
fi
ok "SSH 连通 ${USER}@${HOST}"

# 远程 Docker
if ! ${SSH} "docker info" >/dev/null 2>&1; then
  warn "远程服务器未安装 Docker，正在安装..."
  ${SSH} bash -s <<'REMOTE_DOCKER'
set -e
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq docker.io
  systemctl enable --now docker
elif command -v yum >/dev/null 2>&1; then
  yum install -y -q docker
  systemctl enable --now docker
else
  echo "不支持的包管理器，请手动安装 Docker" >&2; exit 1
fi
REMOTE_DOCKER
  ok "远程 Docker 已安装"
else
  ok "远程 Docker 已安装"
fi

echo ""

# ---- Step 2: 初始化配置文件 ----
info "Step 2/6: 初始化配置文件..."

# DB 密码配置
DB_ENV="${DEPLOY_DIR}/db/.env.db"
if [[ ! -f "${DB_ENV}" ]]; then
  PG_PASS=$(generate_password)
  REDIS_PASS=$(generate_password)
  MEILI_KEY=$(generate_password)
  NEO4J_PASS=$(generate_password)

  cat > "${DB_ENV}" <<DBEOF
# 云端数据库栈连接配置 (由 setup.sh 自动生成)
POSTGRES_DB=agentteams
POSTGRES_USER=agent
POSTGRES_PASSWORD=${PG_PASS}
REDIS_PASSWORD=${REDIS_PASS}
MEILI_ENV=development
MEILI_MASTER_KEY=${MEILI_KEY}
NEO4J_PASSWORD=${NEO4J_PASS}
EMBED_BACKEND=openai
OPENAI_API_KEY=替换为你的硅基流动密钥
OPENAI_BASE_URL=https://api.siliconflow.cn/v1
EMBED_MODEL=BAAI/bge-m3
EMBED_DIM=1024
DBEOF
  ok "已生成 ${DB_ENV}（随机密码）"
  warn "请编辑 ${DB_ENV} 填写 OPENAI_API_KEY（Embedding 需要）"
else
  ok "${DB_ENV} 已存在，跳过"
fi

# AgentTeams 安装配置
AT_ENV="${DEPLOY_DIR}/install/agentteams.env"
if [[ ! -f "${AT_ENV}" ]]; then
  cp "${DEPLOY_DIR}/install/agentteams.env.example" "${AT_ENV}"
  ok "已复制 ${AT_ENV}"
  warn "请编辑 ${AT_ENV} 填写 HICLAW_LLM_API_KEY 和 HICLAW_ADMIN_PASSWORD"
else
  ok "${AT_ENV} 已存在，跳过"
fi

echo ""

# ---- Step 3: 部署远程数据库 ----
info "Step 3/6: 部署远程数据库栈 → ${HOST}..."

${SSH} "mkdir -p ${REMOTE_DIR}"
${SCP} "${DEPLOY_DIR}/db/docker-compose.db.yml" "${USER}@${HOST}:${REMOTE_DIR}/docker-compose.yml"
${SCP} "${DB_ENV}" "${USER}@${HOST}:${REMOTE_DIR}/.env"
ok "文件已上传"

# 设置 Neo4j 前置参数 + 拉起容器
${SSH} bash -s <<'DEPLOY_DB'
set -e
sysctl -w vm.max_map_count=262144
grep -q '^vm.max_map_count' /etc/sysctl.conf || echo 'vm.max_map_count=262144' >> /etc/sysctl.conf
cd /opt/agentteams-db
docker compose pull
docker compose up -d
DEPLOY_DB
ok "远程数据库已启动"

# 等待数据库就绪
sleep 5
${SSH} "cd /opt/agentteams-db && docker compose ps"
echo ""

# ---- Step 4: 安装 AgentTeams ----
info "Step 4/6: 安装 AgentTeams 平台..."

if docker ps -a --format '{{.Names}}' | grep -qxF "hiclaw-controller"; then
  ok "AgentTeams 已安装（hiclaw-controller 容器存在），跳过"
else
  bash "${DEPLOY_DIR}/install/install_agentteams.sh"
  ok "AgentTeams 安装完成"
fi
echo ""

# ---- Step 5: 注册 Worker 和 Team ----
info "Step 5/6: 注册 Worker 角色与团队..."

wait_for "AgentTeams controller" "docker exec hiclaw-controller hiclaw status" 120 || {
  warn "controller 未就绪，跳过 Worker 注册（可稍后手动执行）"
  echo ""
  warn "手动注册命令:"
  echo "  hiclaw apply -f deploy/workers/manager.yaml"
  echo "  hiclaw apply -f deploy/workers/analyzer.yaml"
  echo "  hiclaw apply -f deploy/workers/fixer.yaml"
  echo "  hiclaw apply -f deploy/workers/tester.yaml"
  echo "  hiclaw apply -f deploy/workers/evaluator.yaml"
  echo "  hiclaw apply -f deploy/templates/rd-defect-team.yaml"
}

if docker exec hiclaw-controller hiclaw status >/dev/null 2>&1; then
  expected_password=""
  expected_model="$(manager_expected_model)"
  if [[ -n "${expected_model}" ]]; then
    sync_local_manager_env_value "HICLAW_DEFAULT_MODEL" "${expected_model}"
    docker exec hiclaw-controller hiclaw update manager --name default --model "${expected_model}" --runtime openclaw >/dev/null 2>&1 || true
  fi

  for yaml in "${DEPLOY_DIR}"/workers/*.yaml; do
    name=$(basename "${yaml}" .yaml)
    info "  注册 Worker: ${name}"
    controller_apply_file "${yaml}" 2>/dev/null && \
      ok "  ${name} 已注册" || warn "  ${name} 注册失败（可能已存在）"
  done

  info "  注册 Manager: default"
  controller_apply_file "${DEPLOY_DIR}/templates/default-manager.yaml" 2>/dev/null && \
    ok "Manager default 已注册" || warn "Manager 注册失败"

  info "  初始化 Team: rd-defect-team"
  if [[ -n "${expected_model}" ]]; then
    ensure_rd_defect_team "${expected_model}"
  else
    warn "缺少默认模型，跳过 Team 初始化"
  fi

  expected_password="$(manager_expected_password)"
  if [[ -n "${expected_password}" ]]; then
    sync_local_manager_env_value "HICLAW_MANAGER_PASSWORD" "${expected_password}"
  fi

  ensure_hiclaw_manager_aligned

  for worker in "${LEGACY_WORKERS[@]}"; do
    controller_delete_worker_if_exists "${worker}"
  done
fi
echo ""

# ---- Step 6: 初始化数据库 schema ----
info "Step 6/6: 初始化数据库 schema..."

# 先确保本地能连到远程 DB（需要临时建隧道）
TUNNEL_PID=""
cleanup() {
  if [[ -n "${TUNNEL_PID}" ]] && kill -0 "${TUNNEL_PID}" 2>/dev/null; then
    kill "${TUNNEL_PID}" 2>/dev/null || true
    wait "${TUNNEL_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# 检查隧道是否已存在
if ! nc -z 127.0.0.1 5432 2>/dev/null; then
  info "  建立临时 SSH 隧道..."
  ssh -i "${KEY}" ${SSH_TUNNEL_OPTS} -N -f \
    -L 5432:127.0.0.1:5432 \
    -L 6379:127.0.0.1:6379 \
    -L 7474:127.0.0.1:7474 \
    -L 7687:127.0.0.1:7687 \
    -L 7700:127.0.0.1:7700 \
    "${USER}@${HOST}" && ok "隧道已建立" || { fail "隧道建立失败"; exit 1; }
  TUNNEL_PID=$(pgrep -f "ssh.*${HOST}.*-N" | head -1 || true)
fi

# 加载 DB 环境变量并运行 schema 初始化
set -a; source "${DB_ENV}"; set +a
cd "${REPO_ROOT}"
"${PROJECT_PYTHON}" -c "
import sys; sys.path.insert(0, 'domain_skills')
from db.schema import ensure_all
ensure_all(ns='init')
print('schema 初始化完成')
" 2>/dev/null && ok "数据库 schema 已初始化" || warn "schema 初始化跳过（依赖未安装时正常）"

echo ""
echo "=========================================="
echo -e "  ${GREEN}环境搭建完成!${NC}"
echo "=========================================="
echo ""
echo "后续步骤:"
echo "  1. 编辑 deploy/db/.env.db         → 填写 OPENAI_API_KEY"
echo "  2. 编辑 deploy/install/agentteams.env → 填写 HICLAW_LLM_API_KEY"
echo "  3. 运行 ./scripts/start.sh         → 一键启动全部"
echo ""
echo "日常启动只需: ./scripts/start.sh"
