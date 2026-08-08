#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 官方 HiClaw 安装脚本地址（higress.ai 为国内可访问的官方域名）
INSTALLER_URL="https://higress.ai/hiclaw/install.sh"
INSTALLER_PATH="${SCRIPT_DIR}/hiclaw-install.sh"
# 实际生效的环境变量文件（已被 .gitignore 忽略，含真实密钥）
ENV_FILE="${1:-${SCRIPT_DIR}/agentteams.env}"

if ! command -v bash >/dev/null 2>&1; then
  echo "bash 未安装，无法执行 HiClaw 安装脚本。" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl 未安装，无法下载 HiClaw 安装脚本。" >&2
  exit 1
fi

# 确认 Docker：已安装则确保守护进程运行（否则启动）；未安装则按系统自动安装。
ensure_docker() {
  # 1) 未安装 Docker CLI：按系统提示/执行安装
  if ! command -v docker >/dev/null 2>&1; then
    local os
    os="$(uname -s)"
    echo "[Docker] 未检测到 Docker，准备安装（需要管理员权限）。"
    local _ans
    read -r -p "是否现在自动安装 Docker? [y/N]: " _ans
    case "${_ans}" in
      y|Y) ;;
      *) echo "已取消。请手动安装 Docker 后重试。" >&2; exit 1 ;;
    esac

    if [ "${os}" = "Darwin" ]; then
      if command -v brew >/dev/null 2>&1; then
        brew install --cask docker
      else
        echo "未检测到 Homebrew。请先安装：https://brew.sh ，或手动下载 Docker Desktop：" >&2
        echo "  https://www.docker.com/products/docker-desktop/" >&2
        exit 1
      fi
    elif [ "${os}" = "Linux" ] && [ -f /etc/os-release ] && grep -qi "ubuntu" /etc/os-release; then
      sudo apt-get update
      sudo apt-get install -y docker.io
      sudo systemctl enable --now docker
    else
      echo "不支持的系统：${os}。请手动安装 Docker 后重试。" >&2
      exit 1
    fi
    echo "[Docker] 安装完成，继续启动守护进程..."
  fi

  # 2) 已安装：确认守护进程是否运行，否则启动
  if docker info >/dev/null 2>&1; then
    echo "[Docker] 已安装且守护进程运行中。"
    return 0
  fi

  echo "[Docker] Docker 已安装，但守护进程未运行，尝试启动..."
  local os
  os="$(uname -s)"
  if [ "${os}" = "Darwin" ]; then
    open -a Docker
  elif command -v systemctl >/dev/null 2>&1; then
    sudo systemctl start docker || sudo service docker start
  fi

  # 3) 等待守护进程就绪（最多 60 秒）
  echo "[Docker] 等待 Docker 守护进程就绪..."
  local i=0
  while [ "${i}" -lt 60 ]; do
    if docker info >/dev/null 2>&1; then
      echo "[Docker] Docker 已就绪。"
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  echo "Docker 守护进程在 60 秒内未就绪，请手动启动 Docker 后重试。" >&2
  exit 1
}

ensure_docker

# 读取 HICLAW_* 环境变量（官方安装器从这些变量读取 LLM key 等配置）
if [ -f "${ENV_FILE}" ]; then
  set -a
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  set +a
fi

# 本地已存在安装脚本则跳过下载（便于网络不佳时手动放置后重跑）
if [ ! -f "${INSTALLER_PATH}" ]; then
  echo "[HiClaw] 下载官方安装脚本到 ${INSTALLER_PATH}"
  if ! curl -fsSL "${INSTALLER_URL}" -o "${INSTALLER_PATH}"; then
    echo "下载失败。可手动下载并放到 ${INSTALLER_PATH} 后再运行：" >&2
    echo "  curl -fsSL ${INSTALLER_URL} -o ${INSTALLER_PATH}" >&2
    exit 1
  fi
fi
chmod +x "${INSTALLER_PATH}"

echo "[HiClaw] 启动官方安装流程（已预置 HICLAW_* 环境变量）"
if ! bash "${INSTALLER_PATH}" "$@"; then
  # 官方安装器在「欢迎消息 300s 内未发送」等软失败场景会返回非 0，
  # 但服务实际已就绪（容器已在运行）。此时不阻断 make，避免误报 Error 2。
  echo "[HiClaw] 提示: 官方安装器返回非 0（可能为软失败，例如欢迎消息超时）。" >&2
  echo "[HiClaw] 若上方已打印成功横幅且 'docker ps' 显示容器在运行，则安装已完成，可忽略。" >&2
fi

# ---------------------------------------------------------------
# 后处理：对齐 controller 内部 Manager 模型字段
#   hiclaw-manager.env 存的是用户期望的模型；controller 内部还
#   维护了一份 Manager CRD（model 字段）。两者不一致时 controller
#   重建容器会优先用自己的旧值覆盖新期待模型（即白改 env）。
#   这里用 hiclaw update manager 将 controller 内的 model 对齐。
# ---------------------------------------------------------------
sync_manager_model() {
  local manager_env_path="${HOME}/hiclaw-manager.env"
  local controller_container="hiclaw-controller"
  local expected_model="${HICLAW_DEFAULT_MODEL:-}"

  [ -z "${expected_model}" ] && return 0
  docker ps --format '{{.Names}}' | grep -qxF "${controller_container}" || return 0

  # 先把本机 hiclaw-manager.env 的默认模型改成项目配置，避免 controller 重建后又回退。
  if [ -f "${manager_env_path}" ]; then
    local local_model
    local_model="$(grep -E '^HICLAW_DEFAULT_MODEL=' "${manager_env_path}" | head -1 | sed 's/^HICLAW_DEFAULT_MODEL=//' || true)"
    if [ "${local_model}" != "${expected_model}" ]; then
      MANAGER_ENV_PATH="${manager_env_path}" LOCAL_MODEL="${local_model}" EXPECTED_MODEL="${expected_model}" python3 - <<'PY'
import os
import re
from pathlib import Path

path = Path(os.environ["MANAGER_ENV_PATH"])
text = path.read_text()
local_model = os.environ.get("LOCAL_MODEL", "")
expected_model = os.environ["EXPECTED_MODEL"]
new_line = f"HICLAW_DEFAULT_MODEL={expected_model}"

if local_model and f"HICLAW_DEFAULT_MODEL={local_model}" in text:
    text = text.replace(f"HICLAW_DEFAULT_MODEL={local_model}", new_line, 1)
elif "HICLAW_DEFAULT_MODEL=" in text:
    text = re.sub(r"^HICLAW_DEFAULT_MODEL=.*$", new_line, text, count=1, flags=re.M)
else:
    text += "\n" + new_line + "\n"

path.write_text(text)
PY
      echo "[HiClaw] 已同步 ${manager_env_path} 的默认模型为「${expected_model}」"
    fi
  fi

  # 从 controller 读取当前模型
  local current_model
  current_model="$(docker exec "${controller_container}" hiclaw get managers default 2>/dev/null | grep -E '^Model:' | sed 's/^Model:[[:space:]]*//' || true)"
  [ -z "${current_model}" ] && return 0

  if [ "${current_model}" != "${expected_model}" ]; then
    echo "[HiClaw] controller 内部 Manager 模型为「${current_model}」，"
    echo "         但 hiclaw-manager.env 中为「${expected_model}」— 执行对齐..."
    if docker exec "${controller_container}" hiclaw update manager \
      --name default --model "${expected_model}" >/dev/null 2>&1; then
      echo "[HiClaw] controller 模型已同步为「${expected_model}」（容器即将自动重建）"
    else
      echo "[HiClaw] ⚠️ 模型对齐失败（hiclaw update 返回非 0），请手动检查。"
    fi
  fi
}

sync_manager_model
