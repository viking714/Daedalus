#!/usr/bin/env bash
# ============================================================================
# lib/common.sh — 统一部署脚本的共享基础库（AgentTeams v1.2.x）
# 被 install.sh / run.sh 通过 `source` 引入。
# 职责：配置加载、本机执行封装、Docker 保障、数据库栈部署、DB 环境变量生成、
#       技能包构建、AgentTeams 安装（AGENTTEAMS_* 契约）、controller 资源操作、
#       部署校验、状态打印。
# 模式：本机优先——脚本在哪个机器上运行，就在哪个机器上安装/运行（无需 SSH 远程操控）。
# 命名：v1.2.3 全面采用 agentteams-* 命名（agentteams-controller / agt CLI /
#       agentteams-manager / agentteams-worker-* / agentteams-net /
#       ~/agentteams-manager.env / MinIO alias=bucket=agentteams/agentteams-storage）。
# ============================================================================
set -euo pipefail

# ---- 路径 ----
# common.sh 位于 deploy/scripts/lib/，故 SCRIPT_DIR 为该目录；
# deploy/ 需向上两级（lib -> scripts -> deploy），项目根为 deploy 的上一级。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"       # deploy/
REPO_ROOT="$(cd "${DEPLOY_DIR}/.." && pwd)"           # 项目根
export SCRIPT_DIR DEPLOY_DIR REPO_ROOT
# 操作系统（Darwin=macOS / Linux）
UNAME_S="$(uname -s)"

# ---- 常量 ----
# 端到端验证过的平台版本与官方安装器来源（vendored 优先，见 install_agentteams）
AGENTTEAMS_PINNED_VERSION="v1.2.3"
AGENTTEAMS_INSTALLER_URL="https://raw.githubusercontent.com/agentscope-ai/AgentTeams/main/install/agentteams-install.sh"
CONTROLLER="agentteams-controller"
MANAGER="agentteams-manager"
DASHBOARD="agentteams-dashboard"
RD_WORKERS=(coordinator product-owner architect developer tester reviewer ops-analyst)

# open-code-review（ocr）——Reviewer 确定性审查层（delegate 模式）的宿主机依赖。
# 只使用 ocr 的文件选择 + 规则解析能力，绝不在 ocr 侧配置 LLM（否则变成第二个 Reviewer）。
# OCR_MIN_VERSION：delegate 子命令 `--format json` 可用版本（低版本工具会自动降级为 git 兜底清单）。
OCR_PINNED_VERSION="v1.11.2"
OCR_MIN_VERSION="1.9.0"
OCR_RELEASE_BASE="https://github.com/alibaba/open-code-review/releases/download"

# ---- 颜色 ----
if [[ -t 1 ]]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; CYAN=''; BOLD=''; NC=''
fi
info()  { echo -e "${CYAN}[$(basename "$0")]${NC} $*"; }
ok()    { echo -e "${GREEN}[  ok]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC} $*"; }
fail()  { echo -e "${RED}[fail]${NC} $*" >&2; }
step()  { echo -e "\n${BOLD}==> $*${NC}"; }

# ============================================================================
# 发行版识别（跨平台：macOS / Alibaba Cloud Linux / RHEL 系 / Debian 系 / Ubuntu）
# 设置：DISTRO_ID DISTRO_NAME PKG_MGR IS_RHEL_LIKE IS_ALIBABA_CLOUD_LINUX
# 用途：包管理器提示（dnf/yum vs apt vs brew）、Neo4j sysctl / zip / python3
#       安装指引随发行版变化。Alibaba Cloud Linux 属 RHEL 系（rpm/dnf），非 Ubuntu。
# ============================================================================
detect_distro() {
  DISTRO_ID=""; DISTRO_NAME=""; PKG_MGR=""; IS_RHEL_LIKE=0; IS_ALIBABA_CLOUD_LINUX=0
  if [[ "${UNAME_S}" == "Darwin" ]]; then
    DISTRO_ID="macos"; DISTRO_NAME="macOS"; PKG_MGR="brew"; return 0
  fi
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    DISTRO_ID="${ID:-linux}"; DISTRO_NAME="${NAME:-Linux}"
    case ",${ID_LIKE:-},${ID:-}," in
      *,rhel,*|*,centos,*|*,fedora,*|*,alinux,*|*,alios,*) IS_RHEL_LIKE=1 ;;
    esac
    case ",${ID:-},${ID_LIKE:-}," in
      *,alinux,*|*,alios,*) IS_ALIBABA_CLOUD_LINUX=1 ;;
    esac
  fi
  if [[ "${IS_RHEL_LIKE}" == "1" ]]; then
    if command -v dnf >/dev/null 2>&1; then PKG_MGR="dnf";
    elif command -v yum >/dev/null 2>&1; then PKG_MGR="yum"; fi
  elif command -v apt-get >/dev/null 2>&1; then PKG_MGR="apt";
  fi
  export DISTRO_ID DISTRO_NAME PKG_MGR IS_RHEL_LIKE IS_ALIBABA_CLOUD_LINUX
}

# ============================================================================
# 配置加载
# ============================================================================
load_config() {
  local cfg="${DEPLOY_DIR}/config.env"
  if [[ ! -f "${cfg}" ]]; then
    fail "未找到 ${cfg}，请先: cp config.env.example config.env 并填写"
    exit 1
  fi
  set -a
  # shellcheck source=/dev/null
  source "${cfg}"
  set +a

  # 运行时根目录（默认 deploy/）：数据库栈 compose 与生成的运行时 .env 落在此处
  export RUNTIME_DIR="${RUNTIME_DIR:-${DEPLOY_DIR}}"
  export DB_DIR="${RUNTIME_DIR}/db"
  export DB_ENV="${DB_DIR}/.env"
  # db/.env 非空值回填：config.env 中 DB 密码为空占位，且 compose v5 的 --env-file 不参与
  # ${VAR} 插值——必须保证生成/执行 compose 时当前 shell 已持有真实值（仅回填空键，不覆盖显式配置）
  if [[ -f "${DB_ENV}" ]]; then
    local _k _v
    while IFS='=' read -r _k _v; do
      [[ -n "${_k}" && "${_k}" != \#* ]] || continue
      [[ -z "${!_k:-}" ]] && export "${_k}=${_v}"
    done < "${DB_ENV}"
  fi
  # MCP Server 直接运行于仓库 mcp_server/ 源码目录（无需同步/拷贝）
  export MCP_SRC_DIR="${REPO_ROOT}/mcp_server"

  # open-code-review：ocr.env 由 ensure_ocr 生成，记录宿主机上 ocr 的实际绝对路径，
  # 使 MCP Server 在任意 shell（包括 ~/.local/bin 不在 PATH 时）都能找到它。
  [[ -f "${DEPLOY_DIR}/ocr.env" ]] && source "${DEPLOY_DIR}/ocr.env"
  OCR_BIN="${OCR_BIN:-ocr}"
  OCR_SCRATCH_DIR="${OCR_SCRATCH_DIR:-/tmp/dd-ocr}"
  # OCR_RULE_PATH=none 关闭团队规约注入（留空则用仓内默认文件）
  if [[ "${OCR_RULE_PATH:-}" == "none" ]]; then
    OCR_RULE_PATH=""
  else
    OCR_RULE_PATH="${OCR_RULE_PATH:-${REPO_ROOT}/deploy/rules/ocr-rule.json}"
  fi
  # 仓库克隆缓存根：与 swe_bench_runner 同源，也决定 MCP 端允许审查的仓库白名单
  SWE_REPO_CACHE="${SWE_REPO_CACHE:-/tmp/swe-repos}"
  export OCR_BIN OCR_SCRATCH_DIR OCR_RULE_PATH SWE_REPO_CACHE

  # 默认值兜底（官方安装器读取 AGENTTEAMS_* 前缀）
  AGENTTEAMS_VERSION="${AGENTTEAMS_VERSION:-${AGENTTEAMS_PINNED_VERSION}}"
  AGENTTEAMS_NON_INTERACTIVE="${AGENTTEAMS_NON_INTERACTIVE:-1}"
  AGENTTEAMS_LLM_PROVIDER="${AGENTTEAMS_LLM_PROVIDER:-openai-compat}"
  AGENTTEAMS_ADMIN_USER="${AGENTTEAMS_ADMIN_USER:-admin}"
  # manager 必须为 qwenpaw（v1.2.3 默认镜像 agentteams-manager-qwenpaw；
  # 写 openclaw 会因容器内缺二进制而崩溃重启）
  AGENTTEAMS_MANAGER_RUNTIME="${AGENTTEAMS_MANAGER_RUNTIME:-qwenpaw}"
  AGENTTEAMS_DEFAULT_WORKER_RUNTIME="${AGENTTEAMS_DEFAULT_WORKER_RUNTIME:-openclaw}"
  MCP_PORT="${MCP_PORT:-8090}"
  MCP_HOST="${MCP_HOST:-0.0.0.0}"
  MCP_WORKER_HOST="${MCP_WORKER_HOST:-host.docker.internal}"
  AGENTLOOP_ENABLED="${AGENTLOOP_ENABLED:-0}"
  export AGENTTEAMS_VERSION AGENTTEAMS_NON_INTERACTIVE AGENTTEAMS_LLM_PROVIDER \
         AGENTTEAMS_ADMIN_USER AGENTTEAMS_MANAGER_RUNTIME AGENTTEAMS_DEFAULT_WORKER_RUNTIME \
         MCP_PORT MCP_HOST MCP_WORKER_HOST AGENTLOOP_ENABLED

  # 发行版识别（Alibaba Cloud Linux / RHEL 系 / Debian 系 / macOS）
  detect_distro
  # Worker→MCP 有效主机：默认等于 MCP_WORKER_HOST；Linux 下若保持
  # host.docker.internal，将由 resolve_linux_mcp_host（docker 就绪后调用）回退为
  # Docker 网桥网关 IP，使容器内可直达宿主机 MCP Server。
  export MCP_WORKER_HOST_EFF="${MCP_WORKER_HOST}"
}

require_config() {
  local missing=()
  [[ -n "${AGENTTEAMS_LLM_API_KEY:-}" ]] || missing+=("AGENTTEAMS_LLM_API_KEY")
  [[ -n "${AGENTTEAMS_ADMIN_PASSWORD:-}" ]] || missing+=("AGENTTEAMS_ADMIN_PASSWORD")
  [[ "${AGENTTEAMS_VERSION:-}" == "${AGENTTEAMS_PINNED_VERSION}" ]] \
    || warn "AGENTTEAMS_VERSION=${AGENTTEAMS_VERSION} 非端到端验证过的 ${AGENTTEAMS_PINNED_VERSION}，请确认已验证该版本"
  local db need
  for db in POSTGRES REDIS MEILI NEO4J; do
    local ext_var="${db}_EXTERNAL"
    if [[ "${!ext_var:-0}" == "1" ]]; then
      # 外部已存在：必须提供连接信息
      case "$db" in
        POSTGRES) need=(POSTGRES_HOST POSTGRES_PORT POSTGRES_USER POSTGRES_PASSWORD) ;;
        REDIS)    need=(REDIS_HOST REDIS_PORT REDIS_PASSWORD) ;;
        MEILI)    need=(MEILI_HOST MEILI_PORT MEILI_MASTER_KEY) ;;
        NEO4J)    need=(NEO4J_HOST NEO4J_BOLT_PORT NEO4J_USER NEO4J_PASSWORD) ;;
      esac
    else
      # 内部部署：密码留空则首次自动随机生成，仅校验非密码必要项
      case "$db" in
        POSTGRES) need=(POSTGRES_DB POSTGRES_USER) ;;
        REDIS)    need=() ;;
        MEILI)    need=() ;;
        NEO4J)    need=(NEO4J_USER) ;;
      esac
    fi
    for v in ${need[@]+"${need[@]}"}; do
      [[ -z "${!v:-}" ]] && missing+=("$v (${db} 连接信息缺失)")
    done
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    fail "配置缺失或不完整："
    for m in ${missing[@]+"${missing[@]}"}; do echo "  - $m" >&2; done
    exit 1
  fi
  # worker 名会被平台直接用作 MinIO access key，长度限 3-20；越界时平台只报
  # "The access key is invalid"，该角色静默停在 Failed 且不建容器，排查成本极高
  # （实测两字符的角色名就会触发）。把约束在部署前讲清楚，而不是等 MinIO 报错。
  local w name_bad=0
  for w in "${RD_WORKERS[@]}"; do
    if [[ "${#w}" -lt 3 || "${#w}" -gt 20 ]]; then
      fail "worker 名 '${w}' 长度 ${#w} 超出 MinIO access key 允许的 3-20，请改用更长的角色标识符"
      name_bad=1
    fi
  done
  [[ "${name_bad}" == "0" ]] || exit 1
}

genpw_local() {
  python3 -c "import secrets,string;print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(20)))"
}

# ============================================================================
# 本地执行封装（本机模式）
# 保留 run_local / ssh_exec 命名以复用既有逻辑；本机模式下直接本地执行，
# 上层所有逻辑（装 DB、起 MCP、装 AgentTeams、注册资源、启停平台）无需改动。
# ============================================================================
run_local() {
  # run_local "cmd" — 在本机执行命令（stdin 透传，供 heredoc 形式复用）
  bash -c "$@"
}
ssh_exec() { bash -s; }          # 从 stdin 读取脚本，本地执行（环境变量经继承传递）
scp_to()   { cp -f "$1" "$2"; }  # 本机复制

ensure_ecs_docker() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    ok "本机已安装 Docker 且守护进程运行中"
    return 0
  fi
  if [[ "${PKG_MGR}" == "dnf" || "${PKG_MGR}" == "yum" ]]; then
    fail "本机未就绪 Docker：Alibaba Cloud Linux / RHEL 请先安装并启动 Docker（如 sudo ${PKG_MGR} install -y docker-ce && sudo systemctl start docker）"
  elif [[ "${PKG_MGR}" == "apt" ]]; then
    fail "本机未就绪 Docker：Ubuntu/Debian 请先安装并启动 Docker（sudo apt-get install -y docker.io && sudo systemctl start docker），或安装 Docker Desktop"
  else
    fail "本机未就绪 Docker：请先安装 Docker 并启动守护进程（Docker Desktop 或 systemd docker）"
  fi
  exit 1
}

# ============================================================================
# 前置依赖检查（跨平台）
# ============================================================================
require_python3() {
  # macOS 默认不含 python3（需 Xcode CLT 或 python.org）；Linux 各发行版最小化安装也可能缺失。
  if command -v python3 >/dev/null 2>&1; then
    return 0
  fi
  if [[ "${UNAME_S}" == "Darwin" ]]; then
    fail "未找到 python3：macOS 请先安装 Xcode Command Line Tools（xcode-select --install）或 python.org 版本"
  elif [[ "${PKG_MGR}" == "apt" ]]; then
    fail "未找到 python3：Debian/Ubuntu 请先执行  sudo apt-get update && sudo apt-get install -y python3"
  elif [[ "${PKG_MGR}" == "dnf" || "${PKG_MGR}" == "yum" ]]; then
    fail "未找到 python3：Alibaba Cloud Linux / RHEL 系请先执行  sudo ${PKG_MGR} install -y python3"
  else
    fail "未找到 python3：请先安装 python3（macOS: xcode-select --install；Linux: 对应发行版包管理器）"
  fi
  exit 1
}

# ============================================================================
# Playwright 依赖（visual-check 技能需要）
# ----------------------------------------------------------------------------
# Playwright 用于前端视觉回归：Python 包 + Chromium 浏览器二进制。
# 这里只尝试安装，失败仅警告，不阻塞整体部署（visual_check.py 会优雅降级）。
# ============================================================================
ensure_playwright() {
  step "检查 Playwright（visual-check 依赖）"
  if ! command -v python3 >/dev/null 2>&1; then
    warn "未找到 python3，跳过 Playwright 安装"
    return 0
  fi

  # 先装 Python 包
  if ! python3 -c "import playwright" >/dev/null 2>&1; then
    info "安装 playwright Python 包"
    if ! python3 -m pip install -q playwright 2>/tmp/pw-pip.err; then
      warn "playwright Python 包安装失败：$(tail -n 1 /tmp/pw-pip.err 2>/dev/null)"
      warn "visual_check 将降级为不可用，但不影响其它流程"
      return 0
    fi
  fi

  # 再检查/安装 Chromium 浏览器二进制
  if ! python3 -m playwright install --help >/dev/null 2>&1; then
    warn "playwright CLI 不可用，跳过浏览器安装"
    return 0
  fi

  if ! python3 -m playwright install chromium >/dev/null 2>&1; then
    warn "Playwright Chromium 浏览器下载失败（可能与网络有关）"
    warn "visual_check 将降级为不可用，但不影响其它流程"
    return 0
  fi

  ok "Playwright 就绪"
}

# ============================================================================
# open-code-review（ocr）安装与版本校验
# ----------------------------------------------------------------------------
# 定位：Reviewer 的「确定性约束工具」，仅用 delegate 模式（ocr 端零 LLM）。
# 因此只装到宿主机（MCP Server 所在机器）：ocr_delegate_* 组合工具在宿主机
# scratch 目录重建 base+patch 后调用 ocr，Worker 容器无需自带。
# 与 Playwright 同级：失败只告警，不阻断部署——工具会返回 status=unavailable，
# Reviewer 降级为纯 LLM 审查（但必须在 verdict 里声明 coverage 不可证）。
# ============================================================================
_ocr_ver_ge() {
  # _ocr_ver_ge "<任意版本文本>" "<x.y.z>" —— 文本里提不出版本号则返回失败。
  # 纯 shell 比较：不依赖 python3（Windows Store 假 alias 会静默报错）也不依赖 sort -V（BSD sort 不支持）。
  local cur min i a b
  cur="$(printf '%s' "${1:-}" | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)"
  [[ -n "${cur}" ]] || return 1
  min="${2}"
  case "${cur}" in *.*.*) ;; *) cur="${cur}.0" ;; esac
  case "${min}" in *.*.*) ;; *) min="${min}.0" ;; esac
  local IFS=.
  # shellcheck disable=SC2206
  local -a A=( ${cur} ) B=( ${min} )
  for i in 0 1 2; do
    a=$((10#${A[i]:-0})); b=$((10#${B[i]:-0}))
    (( a > b )) && return 0
    (( a < b )) && return 1
  done
  return 0
}

_ocr_exe() {
  # 把 OCR_BIN 的三种可能形态（PATH 裸名 / POSIX 绝对路径 / cygpath 的 Windows 形式）
  # 统一解析成当前 bash 可直接执行的路径；不可用则返回非 0。
  local cand="${1:-}"
  [[ -n "${cand}" ]] || return 1
  if [[ "${cand}" == *\\* ]] && command -v cygpath >/dev/null 2>&1; then
    cand="$(cygpath -u "${cand}")"
  fi
  if [[ "${cand}" == */* ]]; then
    [[ -x "${cand}" ]] || return 1
    printf '%s\n' "${cand}"
    return 0
  fi
  command -v "${cand}" 2>/dev/null
}

_ocr_report() {
  # 统一打印版本与降级提示，并把实际路径写入 ocr.env（跨 shell 固定 OCR_BIN）
  local bin="$1" txt env_bin="${1}"
  txt="$("${bin}" --version 2>&1 | head -1 || true)"
  if _ocr_ver_ge "${txt}" "${OCR_MIN_VERSION}"; then
    ok "ocr 就绪: ${bin} (${txt:-版本未知})"
  else
    warn "ocr 版本不符要求（${txt:-无法解析} < ${OCR_MIN_VERSION}）：delegate --format json 可能不可用；"
    warn "ocr_delegate_* 会自动降级为 git 兜底文件清单（来源字段会标注为 git-fallback）"
  fi
  # Git Bash / MSYS：宿主机 python 是 Windows 版，认不了 /c/... 路径，必须存 Windows 形式
  if command -v cygpath >/dev/null 2>&1; then
    env_bin="$(cygpath -w "${bin}")"
  fi
  cat > "${DEPLOY_DIR}/ocr.env" <<ENV
# 由 ensure_ocr 生成（请勿手动编辑；重跑 install.sh 会更新）
OCR_BIN=${env_bin}
ENV
  ok "已写入 ${DEPLOY_DIR}/ocr.env（OCR_BIN=${env_bin}）"
  # 只写文件不够：load_config 早于 ensure_ocr，那时 ocr.env 尚不存在，OCR_BIN 已被
  # 定成默认裸名并 export；与本函数同进程的 launch_mcp_server / verify_deployment
  # 会继承这个错值（宿主机 ~/.local/bin 不在 PATH 时，首次安装必然踩空）。Python 侧
  # 只 os.getenv("OCR_BIN")、不读 ocr.env，所以必须在这里同步刷新当前进程。
  OCR_BIN="${env_bin}"
  export OCR_BIN
}

ensure_ocr() {
  step "检查 open-code-review（Reviewer 确定性审查层依赖）"
  if [[ "${OCR_INSTALL_SKIP:-0}" == "1" ]]; then
    warn "OCR_INSTALL_SKIP=1：跳过安装；ocr_delegate_* 将返回 unavailable，Reviewer 降级为纯 LLM 审查"
    return 0
  fi

  # 1) 已可用（PATH 中、或显式指定、或上一次装到 ~/.local/bin）则不重装
  local cand probe
  for cand in "${OCR_BIN:-ocr}" ocr "${HOME}/.local/bin/ocr" "${HOME}/.local/bin/ocr.exe"; do
    if probe="$(_ocr_exe "${cand}")"; then
      _ocr_report "${probe}"
      return 0
    fi
  done

  # 2) 平台识别（官方发布为单文件二进制，无额外依赖，仅需 Git >= 2.41）
  local os arch ext
  case "${UNAME_S}" in
    Darwin)            os="darwin";  ext="" ;;
    Linux)             os="linux";   ext="" ;;
    MINGW*|MSYS*|CYGWIN*) os="windows"; ext=".exe" ;;
    *) warn "未知系统 ${UNAME_S}，跳过 ocr 安装（Reviewer 降级为纯 LLM 审查）"; return 0 ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64)  arch="amd64" ;;
    arm64|aarch64) arch="arm64" ;;
    *) warn "未知架构 $(uname -m)，跳过 ocr 安装"; return 0 ;;
  esac
  if ! command -v git >/dev/null 2>&1; then
    warn "未找到 git（ocr 要求 Git >= 2.41），跳过 ocr 安装"
    return 0
  fi

  local want="${OCR_VERSION:-${OCR_PINNED_VERSION}}"
  local base="${OCR_RELEASE_BASE_URL:-${OCR_RELEASE_BASE}}"   # 国内网络可指向镜像站
  local bin_dir="${OCR_INSTALL_DIR:-${HOME}/.local/bin}"
  local target="${bin_dir}/ocr${ext}"
  local asset="opencodereview-${os}-${arch}${ext}"
  mkdir -p "${bin_dir}" || { warn "无法写入 ${bin_dir}，跳过 ocr 安装"; return 0; }

  info "下载 ocr ${want} (${asset}) -> ${target}"
  if ! curl -fsSL --retry 3 -o "${target}.tmp" "${base}/${want}/${asset}"; then
    warn "ocr 下载失败（网络/镜像问题）；可设 OCR_RELEASE_BASE_URL 指向镜像站，或手工下载后重跑"
    warn "本次部署继续：ocr_delegate_* 将返回 unavailable，Reviewer 降级为纯 LLM 审查"
    rm -f "${target}.tmp"
    return 0
  fi

  # 3) 完整性校验（尽力而为：取不到 sha256sum.txt 或无校验工具则只告警不阻断）
  local shasum_bin=""
  if command -v sha256sum >/dev/null 2>&1; then shasum_bin="sha256sum"
  elif command -v shasum >/dev/null 2>&1; then shasum_bin="shasum -a 256"; fi
  if [[ -n "${shasum_bin}" ]] \
     && curl -fsSL --retry 2 -o "${target}.sha256" "${base}/${want}/sha256sum.txt"; then
    local want_sha got_sha
    # 不假设列顺/分隔符：从命中行里提 64 位十六进制串
    want_sha="$(grep -wF "${asset}" "${target}.sha256" | head -1 | grep -oE '[0-9a-fA-F]{64}' | head -1)"
    got_sha="$(cd "${bin_dir}" && ${shasum_bin} "${target}.tmp" | awk '{print $1}')"
    rm -f "${target}.sha256"
    if [[ -z "${want_sha}" ]]; then
      warn "sha256sum.txt 中未找到 ${asset}，跳过校验"
    elif [[ "${want_sha}" != "${got_sha}" ]]; then
      warn "ocr 下载产物 sha256 不匹配（期望 ${want_sha:0:12}…，实际 ${got_sha:0:12}…），已丢弃"
      rm -f "${target}.tmp"
      return 0
    else
      ok "sha256 校验通过"
    fi
  else
    warn "未做 sha256 校验（缺校验工具或 sha256sum.txt 不可达）"
  fi

  chmod +x "${target}.tmp" && mv -f "${target}.tmp" "${target}" \
    || { warn "无法安装到 ${target}，请设 OCR_INSTALL_DIR 指向可写目录"; return 0; }

  if ! command -v ocr >/dev/null 2>&1; then
    warn "${bin_dir} 不在 PATH；已写入 ocr.env，MCP Server 会经 OCR_BIN 绝对路径调用（命令行手工调用需自行加 PATH）"
  fi
  _ocr_report "${target}"
}

# ============================================================================
# Docker Compose 封装：兼容 v2 插件（docker compose）与 v1（docker-compose）
# DOCKER_COMPOSE_BIN 导出供 ssh_exec 子进程（bash -s）的 heredoc 内引用。
# ============================================================================
detect_compose() {
  if docker compose version >/dev/null 2>&1; then
    DOCKER_COMPOSE_BIN="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    DOCKER_COMPOSE_BIN="docker-compose"
  else
    fail "未找到 Docker Compose：请安装 v2 插件（docker compose）或 v1（docker-compose）"
    return 1
  fi
  export DOCKER_COMPOSE_BIN
}
dc() {
  detect_compose || return 1
  ${DOCKER_COMPOSE_BIN} "$@"
}

# ============================================================================
# Neo4j 内核参数：vm.max_map_count>=262144
# - Linux（含 Alibaba Cloud Linux / RHEL 系 / Ubuntu）：直接 sysctl -w（需 root），
#   并持久化到 /etc/sysctl.d（可写时）。
# - macOS：宿主机 sysctl 无此 oid，须在 Docker Desktop 的 Linux VM 内设置，
#          通过一次性 privileged 容器完成；失败仅告警（避免中断部署）
# 全程非致命，避免 set -e 下中断数据库栈部署。
# ============================================================================
set_neo4j_sysctl() {
  if [[ "${UNAME_S}" == "Linux" ]]; then
    if sysctl -w vm.max_map_count=262144 >/dev/null 2>&1; then
      if [[ -w /etc/sysctl.d ]]; then
        grep -q '^vm.max_map_count' /etc/sysctl.d/99-neo4j.conf 2>/dev/null \
          || echo 'vm.max_map_count=262144' > /etc/sysctl.d/99-neo4j.conf 2>/dev/null || true
      fi
    else
      warn "无法设置 vm.max_map_count（可能缺少 root）；若 Neo4j 启动失败请手动执行: sudo sysctl -w vm.max_map_count=262144"
    fi
  elif [[ "${UNAME_S}" == "Darwin" ]]; then
    info "macOS: 在 Docker Desktop VM 内设置 vm.max_map_count（privileged 容器，重启 Docker 后需重设）"
    docker run --rm --privileged alpine sysctl -w vm.max_map_count=262144 >/dev/null 2>&1 \
      || warn "macOS 设置 VM vm.max_map_count 失败：请在 Docker Desktop『设置→Resources→Advanced』中增加，否则 Neo4j 可能无法启动"
  else
    warn "未知系统 ${UNAME_S}，跳过 vm.max_map_count 设置（若运行 Neo4j 请自行确认）"
  fi
}

# ============================================================================
# 打包技能包：优先 zip 命令；最小化安装可能不含 zip，回退 python3 zipfile
# 用法: zip_dir <源目录> <输出zip>
# ============================================================================
zip_dir() {
  local src="$1" out="$2"
  if command -v zip >/dev/null 2>&1; then
    ( cd "$(dirname "${src}")" && zip -r "${out}" "$(basename "${src}")" -x '*__pycache__*' '*.pyc' >/dev/null )
  else
    warn "系统未安装 zip 命令，改用 python3 打包"
    python3 - "${src}" "${out}" <<'PY'
import sys, zipfile, os
src, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(src):
        if '__pycache__' in root:
            continue
        for f in files:
            if f.endswith('.pyc'):
                continue
            p = os.path.join(root, f)
            z.write(p, os.path.relpath(p, os.path.dirname(src)))
PY
  fi
}

# ============================================================================
# 数据库栈
# ============================================================================
# 生成运行时 .env（DB 连接 + Embedding + OTel），供 compose 与 MCP Server 共用。
# 内部库：若 .env 不存在则随机生成强密码并持久化；外部库：直接写入提供的连接。
generate_db_env() {
  mkdir -p "${DB_DIR}"
  if [[ -f "${DB_ENV}" ]]; then
    ok "复用已有 ${DB_ENV}（含已生成的密码）"
    return 0
  fi
  step "生成数据库环境变量 ${DB_ENV}"
  local pg_pw redis_pw meili_key neo4j_pw
  local pg_host pg_port redis_host redis_port meili_host meili_port neo4j_host neo4j_bolt neo4j_http
  pg_pw="${POSTGRES_PASSWORD:-$(genpw_local)}"
  redis_pw="${REDIS_PASSWORD:-$(genpw_local)}"
  meili_key="${MEILI_MASTER_KEY:-$(genpw_local)}"
  neo4j_pw="${NEO4J_PASSWORD:-$(genpw_local)}"
  # 内部库绑定 127.0.0.1；外部库使用配置中的连接信息
  if [[ "${POSTGRES_EXTERNAL:-0}" == "1" ]]; then pg_host="${POSTGRES_HOST}"; pg_port="${POSTGRES_PORT}"; else pg_host=127.0.0.1; pg_port=5432; fi
  if [[ "${REDIS_EXTERNAL:-0}" == "1" ]];    then redis_host="${REDIS_HOST}"; redis_port="${REDIS_PORT}"; else redis_host=127.0.0.1; redis_port=6379; fi
  if [[ "${MEILI_EXTERNAL:-0}" == "1" ]];    then meili_host="${MEILI_HOST}"; meili_port="${MEILI_PORT}"; else meili_host=127.0.0.1; meili_port=7700; fi
  if [[ "${NEO4J_EXTERNAL:-0}" == "1" ]];    then neo4j_host="${NEO4J_HOST}"; neo4j_bolt="${NEO4J_BOLT_PORT}"; neo4j_http="${NEO4J_HTTP_PORT}"; else neo4j_host=127.0.0.1; neo4j_bolt=7687; neo4j_http=7474; fi

  cat > "${DB_ENV}" <<ENV
# 由统一部署脚本生成（请勿手动编辑；删除后重跑会重新随机密码）
POSTGRES_DB=${POSTGRES_DB:-agentteams}
POSTGRES_USER=${POSTGRES_USER}
POSTGRES_PASSWORD=${pg_pw}
POSTGRES_HOST=${pg_host}
POSTGRES_PORT=${pg_port}
REDIS_PASSWORD=${redis_pw}
REDIS_HOST=${redis_host}
REDIS_PORT=${redis_port}
MEILI_ENV=development
MEILI_MASTER_KEY=${meili_key}
MEILI_HOST=${meili_host}
MEILI_PORT=${meili_port}
NEO4J_PASSWORD=${neo4j_pw}
NEO4J_USER=${NEO4J_USER}
NEO4J_HOST=${neo4j_host}
NEO4J_BOLT_PORT=${neo4j_bolt}
NEO4J_HTTP_PORT=${neo4j_http}
EMBED_BACKEND=${EMBED_BACKEND:-openai}
OPENAI_API_KEY=${OPENAI_API_KEY:-}
OPENAI_BASE_URL=${OPENAI_BASE_URL:-https://api.siliconflow.cn/v1}
EMBED_MODEL=${EMBED_MODEL:-BAAI/bge-m3}
EMBED_DIM=${EMBED_DIM:-1024}
MCP_OTEL_ENABLED=${MCP_OTEL_ENABLED:-false}
ENV
  chmod 600 "${DB_ENV}"
  ok "已生成 ${DB_ENV}（外部库使用配置连接，内部库已生成随机密码）"
}

db_any_internal() {
  for db in POSTGRES REDIS MEILI NEO4J; do
    local ext="${db}_EXTERNAL"
    [[ "${!ext:-0}" != "1" ]] && return 0
  done
  return 1
}

deploy_db_stack() {
  [[ -n "${DOCKER_COMPOSE_BIN:-}" ]] || detect_compose
  if ! db_any_internal; then
    info "全部数据库声明为外部已存在，跳过 compose 部署（仅使用配置的外部连接）"
    return 0
  fi
  step "部署数据库栈（本机 ${RUNTIME_DIR}），跳过已声明外部的数据库"
  mkdir -p "${DB_DIR}"
  # 生成本地过滤后的 compose：去除 EXTERNAL=1 的 service，并把 ${VAR:-default} 占位符
  # 替换为字面值（compose v5 的 --env-file 不参与插值，运行时文件必须不依赖任何外部变量）
  local filtered; filtered="$(mktemp)"
  POSTGRES_EXTERNAL="${POSTGRES_EXTERNAL:-0}" REDIS_EXTERNAL="${REDIS_EXTERNAL:-0}" \
  MEILI_EXTERNAL="${MEILI_EXTERNAL:-0}" NEO4J_EXTERNAL="${NEO4J_EXTERNAL:-0}" \
  python3 - "${DEPLOY_DIR}/db/docker-compose.db.yml" "${filtered}" <<'PY'
import sys, re, os
src, outp = sys.argv[1], sys.argv[2]
text = open(src, encoding='utf-8').read()
external = {svc for db, svc in [("POSTGRES","postgres"),("REDIS","redis"),
                                 ("MEILI","meilisearch"),("NEO4J","neo4j")]
            if os.environ.get(db+"_EXTERNAL") == "1"}
out, in_ext = [], False
for ln in text.splitlines():
    m = re.match(r'^  ([A-Za-z0-9_-]+):\s*$', ln)
    if m:
        in_ext = m.group(1) in external
        if not in_ext: out.append(ln)
        continue
    if re.match(r'^[A-Za-z]', ln):   # 顶层键（volumes: 等），不过滤
        in_ext = False
        out.append(ln); continue
    if not in_ext: out.append(ln)
text = "\n".join(out) + "\n"
def _sub(m):
    name, dflt = m.group(1), m.group(3)
    val = os.environ.get(name, "")
    return val if val else (dflt or "")
text = re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}', _sub, text)
open(outp, 'w', encoding='utf-8').write(text)
PY
  scp_to "${filtered}" "${DB_DIR}/docker-compose.yml"
  rm -f "${filtered}"
  set_neo4j_sysctl
  ssh_exec <<'LOCAL'
set -e
cd ${DB_DIR}
# pull 失败不阻断：本地已有镜像则继续（Docker Hub 国内网络不稳定）
${DOCKER_COMPOSE_BIN} pull || echo "[warn] 部分镜像拉取失败；若本地已有镜像则继续，否则请配置镜像加速后重跑"
${DOCKER_COMPOSE_BIN} up -d
sleep 3
${DOCKER_COMPOSE_BIN} ps
LOCAL
  sync_db_passwords
  ok "数据库栈已部署"
}

# 存量数据卷的密码同步：PG/Neo4j 只在卷首次初始化时消费 *_PASSWORD，后续改密码不生效；
# 检测到 db/.env 与实际库密码不一致时，用库内命令把实际密码改为 db/.env 中的值（零数据丢失）。
sync_db_passwords() {
  local pg_pw neo4j_pw redis_pw meili_key
  pg_pw=$(grep '^POSTGRES_PASSWORD=' "${DB_ENV}" 2>/dev/null | cut -d= -f2-)
  neo4j_pw=$(grep '^NEO4J_PASSWORD=' "${DB_ENV}" 2>/dev/null | cut -d= -f2-)
  redis_pw=$(grep '^REDIS_PASSWORD=' "${DB_ENV}" 2>/dev/null | cut -d= -f2-)
  meili_key=$(grep '^MEILI_MASTER_KEY=' "${DB_ENV}" 2>/dev/null | cut -d= -f2-)
  if [[ "${POSTGRES_EXTERNAL:-0}" != "1" && -n "${pg_pw}" ]]; then
    # 全新卷首次 initdb 需时较长（entrypoint 会临时起停一次），期间 TCP 连接必失败；
    # 不等就绪就测，会把「还在初始化」误判成「密码不一致」（下方 Neo4j 已按此处理）。
    local _pg_ok=0 _p
    for _p in $(seq 1 24); do
      docker exec at-postgres pg_isready -h 127.0.0.1 -q >/dev/null 2>&1 && { _pg_ok=1; break; }
      sleep 5
    done
    if [[ "${_pg_ok}" != "1" ]]; then
      warn "PostgreSQL 未在 120s 内就绪，跳过密码同步检查（稍后可 ./run.sh start 重试）"
    elif
      # 必须走 TCP（-h 127.0.0.1）才能触发密码认证；容器内本地 socket 是免密的，测不出差异
      ! docker exec -e PGPASSWORD="${pg_pw}" at-postgres psql -h 127.0.0.1 -U agent -d agentteams -tAc "select 1" >/dev/null 2>&1; then
      if docker exec -e PGPASSWORD="changeme" at-postgres psql -h 127.0.0.1 -U agent -d agentteams -tAc "select 1" >/dev/null 2>&1; then
        # 卷初始化时密码变量未注入而用了 compose 默认值：经免密 socket 把库内密码改为 db/.env 的值
        docker exec at-postgres psql -U agent -d agentteams -tAc "ALTER USER agent PASSWORD '${pg_pw}'" >/dev/null 2>&1 \
          && ok "PostgreSQL 存量卷密码已同步为 ${DB_ENV} 中的值" \
          || warn "PostgreSQL 密码同步失败，请手工执行 ALTER USER"
      else
        warn "PostgreSQL 无法以 ${DB_ENV} 密码或默认密码经 TCP 登录，请人工排查"
      fi
    fi
  fi
  if [[ "${NEO4J_EXTERNAL:-0}" != "1" && -n "${neo4j_pw}" ]]; then
    # Neo4j 启动较慢（刚重建时需等待），最多等 60s 再判定不一致，避免误报/误修
    local _neo_ok=0 _i
    for _i in $(seq 1 12); do
      if docker exec at-neo4j cypher-shell -u neo4j -p "${neo4j_pw}" "RETURN 1" >/dev/null 2>&1; then _neo_ok=1; break; fi
      sleep 5
    done
    if [[ "${_neo_ok}" != "1" ]]; then
      if docker exec at-neo4j cypher-shell -u neo4j -p "changeme" "RETURN 1" >/dev/null 2>&1; then
        # 存量卷用了 compose 默认密码：Neo4j 5 支持在线改密（不丢数据）
        docker exec at-neo4j cypher-shell -u neo4j -p "changeme" "ALTER CURRENT USER SET PASSWORD FROM 'changeme' TO '${neo4j_pw}'" >/dev/null 2>&1 \
          && ok "Neo4j 存量卷密码已同步为 ${DB_ENV} 中的值" \
          || warn "Neo4j 密码同步失败，请手工执行 ALTER CURRENT USER SET PASSWORD"
      else
        warn "Neo4j 密码与 ${DB_ENV} 不一致且无法自动修复；如需重建请删除其卷后重跑 install.sh"
      fi
    fi
  fi
  # Meilisearch master key 仅运行时生效：容器 env 与 db/.env 不一致时直接按正确 key 重建容器即可（无持久化副作用）
  if [[ "${MEILI_EXTERNAL:-0}" != "1" && -n "${meili_key}" ]]; then
    if ! curl -sf --max-time 5 -H "Authorization: Bearer ${meili_key}" "http://127.0.0.1:${MEILI_PORT:-7700}/indexes" >/dev/null 2>&1; then
      (cd "${DB_DIR}" && ${DOCKER_COMPOSE_BIN} up -d --force-recreate meilisearch >/dev/null 2>&1) \
        && { sleep 3; ok "Meilisearch 已按 ${DB_ENV} 的 master key 重建容器"; } \
        || warn "Meilisearch 重建失败，请检查 ${DB_DIR}/docker-compose.yml"
    fi
  fi
  # Redis 同理：密码仅由启动参数注入，不一致时重建容器（数据为缓存，可丢）
  if [[ "${REDIS_EXTERNAL:-0}" != "1" && -n "${redis_pw}" ]]; then
    if ! docker exec at-redis redis-cli -a "${redis_pw}" --no-auth-warning ping 2>/dev/null | grep -q PONG; then
      (cd "${DB_DIR}" && ${DOCKER_COMPOSE_BIN} up -d --force-recreate redis >/dev/null 2>&1) \
        && { sleep 2; ok "Redis 已按 ${DB_ENV} 的密码重建容器"; } \
        || warn "Redis 重建失败，请检查 ${DB_DIR}/docker-compose.yml"
    fi
  fi
}

db_stack_start() {
  [[ -n "${DOCKER_COMPOSE_BIN:-}" ]] || detect_compose
  db_any_internal || return 0
  [[ -f "${DB_DIR}/docker-compose.yml" ]] || { warn "数据库栈未部署（缺 ${DB_DIR}/docker-compose.yml），请先运行 install.sh"; return 0; }
  set_neo4j_sysctl
  ssh_exec <<'LOCAL'
set -e
cd ${DB_DIR}
${DOCKER_COMPOSE_BIN} up -d
LOCAL
  sync_db_passwords
}
db_stack_stop() {
  [[ -n "${DOCKER_COMPOSE_BIN:-}" ]] || detect_compose
  db_any_internal || { info "数据库为外部管理，跳过停止"; return 0; }
  run_local "cd ${DB_DIR} && ${DOCKER_COMPOSE_BIN} stop" >/dev/null 2>&1 || true
}
db_stack_status() {
  [[ -n "${DOCKER_COMPOSE_BIN:-}" ]] || detect_compose
  db_any_internal || { echo "  (数据库为外部连接，不在本机管理)"; return 0; }
  # 不指定 --format：compose v2/v5 的表格字段名不一致（.Names vs .Name），默认输出两者兼容
  run_local "cd ${DB_DIR} && ${DOCKER_COMPOSE_BIN} ps" 2>/dev/null || true
}

# ============================================================================
# Linux（含 Alibaba Cloud Linux / Ubuntu）下 host.docker.internal 解析修复
# ----------------------------------------------------------------------------
# macOS/Windows Docker Desktop 原生解析 host.docker.internal → 宿主机；
# Linux 内核容器内不自动解析该名。Worker 容器由平台创建、无 extraHosts 字段，
# 无法从本仓库向容器注入 --add-host。因此 docker 就绪后探测 Docker 网桥网关 IP，
# 将其作为 Worker→MCP 的有效主机（容器经网桥网关可达宿主机 MCP Server，
# 后者绑定 0.0.0.0）。若用户显式将 MCP_WORKER_HOST 设为其它值，则尊重该值。
# 需在 ensure_ecs_docker 之后调用（依赖 docker 与网桥）。
# ============================================================================
resolve_linux_mcp_host() {
  if [[ "${UNAME_S}" != "Linux" ]]; then
    MCP_WORKER_HOST_EFF="${MCP_WORKER_HOST:-host.docker.internal}"
    export MCP_WORKER_HOST_EFF
    return 0
  fi
  local eff="${MCP_WORKER_HOST:-host.docker.internal}"
  if [[ "${eff}" == "host.docker.internal" ]]; then
    local gw=""
    # 优先取默认 bridge 网络网关；取不到再回退到 docker0 接口地址
    gw="$(docker network inspect bridge -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null | head -1)"
    if [[ -z "${gw}" ]]; then
      gw="$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)"
    fi
    if [[ -n "${gw}" ]]; then
      warn "Linux 容器内不解析 host.docker.internal；已将 Worker→MCP 主机自动回退为 Docker 网桥网关 ${gw}（容器经此可达宿主机 MCP Server :${MCP_PORT}）"
      eff="${gw}"
    else
      warn "Linux 容器内不解析 host.docker.internal，且未能探测网桥网关；请将 MCP_WORKER_HOST 设为可达地址（如 docker0 网关 172.17.0.1）"
    fi
  fi
  MCP_WORKER_HOST_EFF="${eff}"
  export MCP_WORKER_HOST_EFF
}

# ============================================================================
# 模板渲染：将 MCP 端点占位符替换为运行时配置
# ============================================================================
# 替换 __MCP_WORKER_HOST__ / __MCP_PORT__ 为 config.env 中的真实值。
# 用于 worker YAML（部署时经 controller 注入）与技能包 manifest.json（打包时注入），
# 使「改 MCP_PORT 即全局生效」；Linux 下渲染值取 MCP_WORKER_HOST_EFF（可能为网桥网关）。
# 用法: render_mcp_template <src> <dst>
render_mcp_template() {
  local src="$1" dst="$2"
  local wh="${MCP_WORKER_HOST_EFF:-${MCP_WORKER_HOST:-host.docker.internal}}" pt="${MCP_PORT:-8090}"
  sed -e "s#__MCP_WORKER_HOST__#${wh}#g" \
      -e "s#__MCP_PORT__#${pt}#g" \
      "${src}" > "${dst}"
}

# ============================================================================
# 领域技能包
# 两条分发路径，职责不同：
#   1) zip 产物（build_skills_package）——版本化构件，供整包导入与存档；
#      平台只认 “agt apply worker --zip”（manifest.json 在 zip 根），且整包不分角色。
#   2) canonical 目录（stage_worker_skills + push_worker_skills）——按角色装配的正路：
#      Manager 的 ~/worker-skills/<skill>/SKILL.md + Worker CR 的 spec.skills 按名引用。
#      只有这条能保持 §2.3 的角色技能划分（例如 ocr-delegate-review 仅 Reviewer）。
# ============================================================================
build_skills_package() {
  # 按 manifest 版本号打包技能包（源码未变更则跳过），输出稳定名 + 版本名两个 zip
  local pkg_dir="${DEPLOY_DIR}/packages/rd-defect-skills"
  local manifest="${pkg_dir}/manifest.json"
  [[ -f "${manifest}" ]] || { fail "未找到 ${manifest}"; exit 1; }
  local version
  version="$(python3 -c "import json;print(json.load(open('${manifest}'))['version'])")"
  local out_dir="${DEPLOY_DIR}/packages"
  local zip_ver="${out_dir}/rd-defect-skills-v${version}.zip"
  local zip_stable="${out_dir}/rd-defect-skills.zip"
  # 仅当源码比已有产物更新时重新打包。
  # 注意：版本名 zip 不存在时（全新机器、或刚抬过版本号）find -newer 会以退出码 1 报错，
  # 经 pipefail 传给 wc -l 再经行赋值返回 1，set -e 会直接打断 install.sh 且不打任何日志
  # （现象：rc=1，日志最后一行停在“controller 已就绪”）。因此先判存在再比较。
  local newest=0
  if [[ -f "${zip_ver}" ]]; then
    newest="$(find "${pkg_dir}/skills" -type f -name 'SKILL.md' -newer "${zip_ver}" 2>/dev/null | wc -l || true)"
  fi
  if [[ -f "${zip_ver}" && "${newest}" -eq 0 ]]; then
    ok "技能包已是最新 (rd-defect-skills-v${version}.zip)，跳过打包"
  else
    step "构建技能包 rd-defect-skills-v${version}.zip"
    local tmp; tmp="$(mktemp -d)"
    cp -r "${pkg_dir}" "${tmp}/rd-defect-skills"
    # 渲染 manifest 中的 MCP 端点占位符（__MCP_WORKER_HOST__ / __MCP_PORT__）
    if [[ -f "${tmp}/rd-defect-skills/manifest.json" ]]; then
      render_mcp_template "${pkg_dir}/manifest.json" "${tmp}/rd-defect-skills/manifest.json"
    fi
    find "${tmp}/rd-defect-skills" -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
    find "${tmp}/rd-defect-skills" -name '*.pyc' -delete 2>/dev/null || true
    # 校验 SKILL.md 含 YAML front matter
    while IFS= read -r md; do
      [[ "$(head -1 "$md")" == "---" ]] || warn "缺少 front matter: $(dirname "$md" | xargs basename)/SKILL.md"
    done < <(find "${tmp}/rd-defect-skills" -name 'SKILL.md')
    zip_dir "${tmp}/rd-defect-skills" "${zip_ver}"
    rm -rf "${tmp}"
    ok "已构建 ${zip_ver}"
  fi
  # 稳定名副本（与版本名同内容，便于存档/手工导入时不记版本号）
  cp -f "${zip_ver}" "${zip_stable}"
  SKILLS_ZIP_BASENAME="$(basename "${zip_ver}")"
  SKILLS_ZIP_PATH="${zip_ver}"
}

push_skills_package() {
  # zip 只作为构件存档并送到 controller 备用（手工整包导入：agt apply worker --zip）；
  # 角色级装配不靠它，见 stage_worker_skills。
  build_skills_package
  step "归档技能包到 controller"
  docker exec "${CONTROLLER}" mkdir -p /deploy/packages 2>/dev/null || true
  docker cp "${SKILLS_ZIP_PATH}" "${CONTROLLER}:/deploy/packages/${SKILLS_ZIP_BASENAME}" 2>/dev/null || true
  docker exec "${CONTROLLER}" cp -f "/deploy/packages/${SKILLS_ZIP_BASENAME}" "/deploy/packages/rd-defect-skills.zip" 2>/dev/null || true
  ok "技能包已归档: ${SKILLS_ZIP_BASENAME} (+ 稳定名 rd-defect-skills.zip)"
}

manager_home() {
  docker exec "${MANAGER}" sh -c 'printf %s "$HOME"' 2>/dev/null
}

stage_worker_skills() {
  # 把技能源目录投放到平台的 canonical 位置（Manager 容器内的 ~/worker-skills/<skill>/）。
  # 平台只从这个目录按名取技能；worker YAML 里写 spec.package: file:// 是无效的——
  # 安装器只支持 nacos://|http://|oss://，file:// 会被“apply 成功但什么都没发生”地静默丢弃。
  step "投放领域技能到 Manager canonical 目录（按角色装配的前提）"
  local src="${DEPLOY_DIR}/packages/rd-defect-skills/skills"
  [[ -d "${src}" ]] || { warn "未找到 ${src}，跳过技能投放"; return 0; }
  local home; home="$(manager_home)"
  [[ -n "${home}" ]] || { warn "无法解析 ${MANAGER} 的 HOME，跳过技能投放"; return 0; }
  docker exec "${MANAGER}" mkdir -p "${home}/worker-skills" >/dev/null 2>&1 \
    || { warn "无法创建 ${MANAGER}:${home}/worker-skills，跳过技能投放"; return 0; }
  local d s staged=0 skipped=0
  for d in "${src}"/*/; do
    [[ -f "${d}SKILL.md" ]] || { skipped=$((skipped+1)); continue; }
    s="$(basename "${d}")"
    # 名合规前绝不执行容器内 rm -rf
    [[ "${s}" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { warn "技能目录名不合规，跳过：${s}"; skipped=$((skipped+1)); continue; }
    docker exec "${MANAGER}" rm -rf "${home}/worker-skills/${s}" >/dev/null 2>&1 || true
    if docker cp "${d%/}" "${MANAGER}:${home}/worker-skills/${s}" >/dev/null 2>&1; then
      staged=$((staged+1))
    else
      warn "技能 ${s} 投放失败"
      skipped=$((skipped+1))
    fi
  done
  [[ "${skipped}" -eq 0 ]] || warn "${skipped} 个技能未投放（缺 SKILL.md 或投放失败）"
  ok "已投放 ${staged} 个技能 -> ${MANAGER}:${home}/worker-skills"
}

push_worker_skills() {
  # 交给平台自己的装配脚本：按各 Worker 的 spec.skills 上传到该角色存储、回读校验、
  # 并经 Matrix 通知 file-sync（通知失败不影响投递，Worker 最长 5 分钟内自行同步）。
  step "按角色装配技能（spec.skills -> Worker）"
  local push_sh="${AGENTTEAMS_PUSH_SKILLS_SCRIPT:-/opt/agentteams/agent/skills/worker-management/scripts/push-worker-skills.sh}"
  if ! docker exec "${MANAGER}" test -f "${push_sh}" 2>/dev/null; then
    warn "平台未提供 ${push_sh}（版本差异？）；技能已投放到 canonical 目录，"
    warn "Worker 会按 spec.skills 自行拉取，可稍后手动执行：docker exec ${MANAGER} bash ${push_sh} --worker <name>"
    return 0
  fi
  local w ok_n=0 bad_n=0 out
  for w in "${RD_WORKERS[@]}"; do
    if out="$(docker exec "${MANAGER}" bash "${push_sh}" --worker "${w}" 2>&1)"; then
      ok_n=$((ok_n+1))
    else
      bad_n=$((bad_n+1))
      warn "worker ${w} 技能装配失败：$(printf '%s' "${out}" | tail -n 2 | tr '\n' ' ')"
    fi
  done
  ok "技能装配完成（成功 ${ok_n} / 失败 ${bad_n}）"
}

# 读 worker YAML 的 spec.skills（部署期的唯一事实源）。
# 不用 pyyaml：目标机 python3 未必带它，而这几行结构是我们自己生成的，按行取即可。
yaml_declared_skills() {
  python3 -c '
import sys
out, inside = [], False
try:
    lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
except OSError:
    sys.exit(1)
for ln in lines:
    if ln.startswith("  skills:"):
        inside = True
        continue
    if inside:
        if ln.startswith("    - "):
            out.append(ln[6:].strip())
        elif ln.startswith("  "):
            break
print(" ".join(out))' "$1" 2>/dev/null || true
}

# 读 controller 里该 Worker CR 实际记录的 skills；读不到（controller 未就绪、CR 不存在）
# 时以非 0 返回，以便调用方把「查不到」和「查到了但为空」区分开。
# 两步分开写而不是一条管道：否则返回码要靠 pipefail 才能传出来，不该依赖调用方的 set 选项。
cr_recorded_skills() {
  local raw
  raw="$(docker exec "${CONTROLLER}" agt get workers "$1" -o json 2>/dev/null)" || return 2
  printf '%s' "${raw}" | python3 -c '
import json, sys
try:
    w = json.load(sys.stdin)
except Exception:
    sys.exit(2)
w = w.get("worker", w) if isinstance(w, dict) else w
sk = (w or {}).get("skills") or []
if isinstance(sk, str):
    sk = [s for s in sk.split(",") if s.strip()]
print(" ".join(sk))' || return 2
}

_norm_set() { tr ' \t' '\n' | sed '/^$/d' | LC_ALL=C sort | tr '\n' ' '; }

verify_worker_skills() {
  # 为什么必须有这道校验：agt apply -f 对任何字段一律回「worker/X configured」+ 退出 0，
  # 被丢弃的字段（例如曾写过的 spec.package: file://）不会留下任何痕迹。CR 里没记
  # spec.skills 的角色就是「裸角色」——只有平台内置技能，§2.3 的领域流程根本跑不起来。
  # 所以这里逐个角色比对 YAML 与 CR，不一致显式 FAIL，而不是等运行时才发现。
  local w want got got_raw missing extra bad=0 n
  for w in "${RD_WORKERS[@]}"; do
    want="$(yaml_declared_skills "${DEPLOY_DIR}/workers/${w}.yaml" | _norm_set)"
    n="$(printf '%s' "${want}" | wc -w)"
    if [[ "${n}" -eq 0 ]]; then
      fail "worker ${w}: ${DEPLOY_DIR}/workers/${w}.yaml 未声明 spec.skills（该角色只会有平台内置技能）"
      bad=1
      continue
    fi
    if ! got_raw="$(cr_recorded_skills "${w}")"; then
      fail "worker ${w}: 无法从 controller 读取 CR，spec.skills 装配状态未验证"
      bad=1
      continue
    fi
    got="$(printf '%s' "${got_raw}" | _norm_set)"
    if [[ -z "${got// /}" ]]; then
      fail "worker ${w}: CR 未记录任何 spec.skills（YAML 声明了 ${n} 项）—— 裸角色，领域流程不可用"
      bad=1
      continue
    fi
    missing="$(comm -23 <(printf '%s\n' ${want}) <(printf '%s\n' ${got}) | tr '\n' ' ')"
    extra="$(comm -13 <(printf '%s\n' ${want}) <(printf '%s\n' ${got}) | tr '\n' ' ')"
    if [[ -n "${missing// /}" || -n "${extra// /}" ]]; then
      fail "worker ${w}: spec.skills 与 YAML 不一致（缺失 [${missing}] 多余 [${extra}]）"
      bad=1
    else
      ok "worker ${w}: 领域技能已按角色装配（${n} 项）"
    fi
  done
  return "${bad}"
}

# ============================================================================
# AgentTeams 安装（官方安装器，AGENTTEAMS_* 契约）
# ============================================================================
install_agentteams() {
  step "安装 AgentTeams 平台（${AGENTTEAMS_VERSION}）"
  # 1) 从 config.env 生成安装器入参（仅写非空值，空值交由安装器默认逻辑）
  local env_local; env_local="$(mktemp)"
  {
    local k
    for k in AGENTTEAMS_VERSION AGENTTEAMS_NON_INTERACTIVE AGENTTEAMS_LANGUAGE \
             AGENTTEAMS_LLM_PROVIDER AGENTTEAMS_DEFAULT_MODEL AGENTTEAMS_LLM_API_KEY \
             AGENTTEAMS_OPENAI_BASE_URL AGENTTEAMS_ADMIN_USER AGENTTEAMS_ADMIN_PASSWORD \
             AGENTTEAMS_PORT_GATEWAY AGENTTEAMS_PORT_CONSOLE AGENTTEAMS_PORT_ELEMENT_WEB \
             AGENTTEAMS_LOCAL_ONLY AGENTTEAMS_MATRIX_DOMAIN AGENTTEAMS_ELEMENT_HOMESERVER_URL AGENTTEAMS_MANAGER_RUNTIME AGENTTEAMS_DEFAULT_WORKER_RUNTIME \
             AGENTTEAMS_DATA_DIR AGENTTEAMS_WORKSPACE_DIR AGENTTEAMS_REGISTRY \
             AGENTTEAMS_MINIO_ENDPOINT AGENTTEAMS_MINIO_USER AGENTTEAMS_MINIO_PASSWORD AGENTTEAMS_MINIO_BUCKET; do
      [[ -n "${!k:-}" ]] && printf '%s=%s\n' "${k}" "${!k}"
    done
    # AgentLoop 可观测（可选）：映射为安装器 AGENTTEAMS_CMS_* 并注入 controller
    if [[ "${AGENTLOOP_ENABLED:-0}" == "1" ]]; then
      for k in AGENTLOOP_ENDPOINT AGENTLOOP_LICENSE_KEY AGENTLOOP_PROJECT AGENTLOOP_WORKSPACE AGENTLOOP_SERVICE_NAME; do
        [[ -n "${!k:-}" ]] || { fail "AgentLoop 已启用但缺少 ${k}"; exit 1; }
      done
      printf 'AGENTTEAMS_CMS_TRACES_ENABLED=true\n'
      printf 'AGENTTEAMS_CMS_ENDPOINT=%s\n' "${AGENTLOOP_ENDPOINT}"
      printf 'AGENTTEAMS_CMS_LICENSE_KEY=%s\n' "${AGENTLOOP_LICENSE_KEY}"
      printf 'AGENTTEAMS_CMS_PROJECT=%s\n' "${AGENTLOOP_PROJECT}"
      printf 'AGENTTEAMS_CMS_WORKSPACE=%s\n' "${AGENTLOOP_WORKSPACE}"
      printf 'AGENTTEAMS_CMS_SERVICE_NAME=%s\n' "${AGENTLOOP_SERVICE_NAME}"
    fi
  } > "${env_local}"
  chmod 600 "${env_local}"

  # 2) 获取官方安装器：vendored 副本优先（国内网络更稳），缺失则从 GitHub 下载
  local installer="${DEPLOY_DIR}/install/agentteams-install.sh"
  if [[ ! -f "${installer}" ]]; then
    info "vendored 安装器缺失，从官方源下载: ${AGENTTEAMS_INSTALLER_URL}"
    curl -fsSL "${AGENTTEAMS_INSTALLER_URL}" -o "${installer}.tmp" || { fail "下载官方安装器失败（可手动放置 ${installer}）"; exit 1; }
    mv "${installer}.tmp" "${installer}"
  fi
  chmod +x "${installer}"

  # 3) 静默安装（幂等：已安装则进入升级流程；安装器生成 ~/agentteams-manager.env）
  #    AGENTTEAMS_REINSTALL=1：强制全新安装——非交互升级不会重建容器且会用旧 env 覆盖新参数，
  #    因此先备份并移除安装器 env 文件、停掉平台容器（保留数据卷），使其走全新安装分支。
  if [[ "${AGENTTEAMS_REINSTALL:-0}" == "1" ]]; then
    info "AGENTTEAMS_REINSTALL=1：移除旧安装器 env 并停掉平台容器（数据卷保留），走全新安装"
    ssh_exec <<'RESET'
docker ps -a --format '{{.Names}}' | grep -E '^agentteams-(controller|manager|worker-|dashboard)' | while read -r c; do docker rm -f "$c" >/dev/null 2>&1 || true; done
if [ -f "${HOME}/agentteams-manager.env" ]; then
  cp -f "${HOME}/agentteams-manager.env" "${HOME}/agentteams-manager.env.bak-$(date +%Y%m%d%H%M%S)"
  rm -f "${HOME}/agentteams-manager.env"
fi
RESET
  fi
  ssh_exec <<LOCAL
set -e
set -a; source "${env_local}"; set +a
bash "${installer}" || echo "[warn] 安装器返回非0（可能是软失败，如欢迎消息超时）；若 docker ps 显示 agentteams-controller 运行则视为成功"
LOCAL
  rm -f "${env_local}"
  ok "AgentTeams 安装流程结束（请确认 ${CONTROLLER} 容器已运行）"
}

# ============================================================================
# controller 资源操作
# ============================================================================
wait_controller_ready() {
  local i=0
  while (( i < 60 )); do
    if docker exec "${CONTROLLER}" agt status >/dev/null 2>&1; then
      return 0
    fi
    sleep 2; i=$((i+1))
  done
  return 1
}

controller_apply_file() {
  local file_path="$1"
  # 渲染 MCP 端点占位符（worker YAML 含 __MCP_WORKER_HOST__/__MCP_PORT__；其他资源无占位符则原样透传）
  local tmp; tmp="$(mktemp)"
  render_mcp_template "${file_path}" "${tmp}"
  docker cp "${tmp}" "${CONTROLLER}:/tmp/$(basename "${file_path}")" >/dev/null 2>&1 || true
  docker exec "${CONTROLLER}" agt apply -f "/tmp/$(basename "${file_path}")" >/dev/null 2>&1 || true
  docker exec "${CONTROLLER}" rm -f "/tmp/$(basename "${file_path}")" >/dev/null 2>&1 || true
  rm -f "${tmp}"
}

get_registered_workers() {
  docker exec "${CONTROLLER}" agt get workers -o json 2>/dev/null \
    | python3 -c 'import sys,json
try:
    for w in json.load(sys.stdin).get("workers",[]):
        n=w.get("name")
        if n: print(n)
except Exception: pass' 2>/dev/null || true
}

register_resources() {
  step "注册 Worker / Manager / Team 资源"
  local expected_model="${AGENTTEAMS_DEFAULT_MODEL:-}"
  local yaml
  for yaml in "${DEPLOY_DIR}"/workers/*.yaml; do
    [[ -f "${yaml}" ]] || continue
    controller_apply_file "${yaml}" && ok "  apply $(basename "${yaml}")" || warn "  apply 失败 $(basename "${yaml}")"
  done
  for yaml in "${DEPLOY_DIR}"/teams/*.yaml; do
    [[ -f "${yaml}" ]] || continue
    controller_apply_file "${yaml}" && ok "  apply $(basename "${yaml}")" || warn "  apply 失败 $(basename "${yaml}")"
  done
  # 对齐 manager 默认模型（勿改 runtime：manager 必须为 qwenpaw）
  if [[ -n "${expected_model}" ]]; then
    docker exec "${CONTROLLER}" agt update manager --name default --model "${expected_model}" >/dev/null 2>&1 || true
  fi
  # 技能装配的顺序不能调：canonical 目录先就位 -> push（按 CR 的 spec.skills 上传+回读）
  # -> 才唤醒 worker，否则 worker 首次启动会看到「零技能」并要等最长 5 分钟的周期 file-sync。
  stage_worker_skills
  push_worker_skills
  # 唤醒所有 worker
  local w
  while IFS= read -r w; do
    [[ -z "${w}" ]] && continue
    docker exec "${CONTROLLER}" agt worker ensure-ready --name "${w}" >/dev/null 2>&1 || true
  done < <(get_registered_workers)
  ok "资源注册并唤醒完成"
}

# ============================================================================
# 运行时修补：修复 agentteams 平台容器内的已知问题（幂等，可重复运行）
# ============================================================================
# 修复清单：
#   1. merge-openclaw-config.sh 无条件重写 openclaw.json → 触发 gateway inotify
#      重启 → Matrix 加密清空 → 消息重复。改为 compare-before-write。
patch_worker_runtime() {
  step "修补 worker 容器运行时脚本（平台已知问题）"
  local patched=0 skipped=0
  local merge_file="/opt/agentteams/scripts/lib/merge-openclaw-config.sh"
  local marker="AGENTTEAMS_PATCH_CBW_V1"

  # 将 Python 修补脚本写入临时文件（避免多层引号转义问题）
  local patch_py; patch_py="$(mktemp /tmp/agentteams-patch-XXXXXX.py)"
  cat > "${patch_py}" << 'PATCH_PYEOF'
import sys

f = "/opt/agentteams/scripts/lib/merge-openclaw-config.sh"
marker = "AGENTTEAMS_PATCH_CBW_V1"

try:
    with open(f, 'r') as fh:
        content = fh.read()
except FileNotFoundError:
    sys.exit(0)

if marker in content:
    print("ALREADY_PATCHED")
    sys.exit(0)

with open(f + '.bak', 'w') as fh:
    fh.write(content)

old_line = "    printf '%s\\n' \"${merged}\" > \"${output_path}\""

new_block = """    # [AGENTTEAMS_PATCH_CBW_V1] Only write if content actually changed (prevent unnecessary gateway reload)
    if [ "${output_path}" = "${local_path}" ] && [ -f "${local_path}" ]; then
        printf '%s\\n' "${merged}" > /tmp/openclaw-merge-tmp.json
        if ! cmp -s /tmp/openclaw-merge-tmp.json "${local_path}"; then
            mv /tmp/openclaw-merge-tmp.json "${output_path}"
        else
            rm -f /tmp/openclaw-merge-tmp.json
        fi
    else
        printf '%s\\n' "${merged}" > "${output_path}"
    fi"""

if old_line in content:
    content = content.replace(old_line, new_block, 1)
    with open(f, 'w') as fh:
        fh.write(content)
    print("PATCHED")
else:
    print("OLD_PATTERN_NOT_FOUND")
PATCH_PYEOF

  for w in "${RD_WORKERS[@]}"; do
    local container="agentteams-worker-${w}"
    # 跳过未运行的容器
    docker ps --format '{{.Names}}' | grep -qF "${container}" || { skipped=$((skipped+1)); continue; }
    # 幂等：已修补则跳过
    if docker exec "${container}" grep -q "${marker}" "${merge_file}" 2>/dev/null; then
      skipped=$((skipped+1)); continue
    fi
    # 将修补脚本拷入容器并执行
    docker cp "${patch_py}" "${container}:/tmp/patch-merge.py" 2>/dev/null || { skipped=$((skipped+1)); continue; }
    local result
    result=$(docker exec "${container}" python3 /tmp/patch-merge.py 2>&1) || true
    case "${result}" in
      *PATCHED*) ok "  ${w}: merge-openclaw-config.sh → compare-before-write"; patched=$((patched+1)) ;;
      *) skipped=$((skipped+1)) ;;
    esac
  done
  rm -f "${patch_py}"
  if [[ "${patched}" -gt 0 ]]; then
    ok "已修补 ${patched} 个 worker 容器（防止 gateway 无谓重启）"
  else
    ok "运行时修补：无需修补或已修补（跳过 ${skipped}）"
  fi
}

# ============================================================================
# 部署校验（v1.2.3 已修复团队绑定/成员裁剪问题，无需运行时修复脚本；仅做轻量校验）
# ============================================================================
verify_deployment() {
  step "校验部署"
  local bad=0 name i
  wait_controller_ready && ok "controller 就绪" || { fail "controller 未就绪"; bad=1; }
  # worker 容器由 controller 按需拉起，首次启动较慢：轮询等待最多 90s
  for name in "${RD_WORKERS[@]}"; do
    local up=0
    for i in $(seq 1 18); do
      if docker ps --format '{{.Names}}' | grep -qF "agentteams-worker-${name}"; then up=1; break; fi
      sleep 5
    done
    if [[ "${up}" == "1" ]]; then
      ok "worker 容器运行中: agentteams-worker-${name}"
    else
      warn "worker 容器未运行: agentteams-worker-${name}（可稍后 ./run.sh start 或 agt worker ensure-ready）"
      bad=1
    fi
  done
  if docker exec "${CONTROLLER}" agt get teams -o json 2>/dev/null | grep -q 'rd-defect-team'; then
    ok "team 已注册: rd-defect-team"
  else
    warn "team rd-defect-team 未在 agt get teams 中找到"
    bad=1
  fi
  # ocr 仅影响 Reviewer 审查完整性证据，缺失不判为部署失败
  local ocr_exe
  if ocr_exe="$(_ocr_exe "${OCR_BIN:-ocr}")"; then
    ok "ocr 可用: ${ocr_exe} ($("${ocr_exe}" --version 2>&1 | head -1))"
  else
    warn "ocr 不可用（OCR_BIN=${OCR_BIN:-ocr}）：ocr_delegate_* 返回 unavailable，"
    warn "Reviewer 仅能纯 LLM 审查，且必须在 verdict 中声明 coverage 不可证；可重跑 install.sh 安装"
  fi
  # 角色技能是领域流程的前提，缺了等于部署了一个空壳团队，必须显式报失败
  verify_worker_skills || bad=1
  mcp_is_running && ok "MCP Server 监听中 (127.0.0.1:${MCP_PORT})" || { warn "MCP Server 未运行"; bad=1; }
  if [[ "${bad}" == "0" ]]; then
    VERIFY_BAD=0
    ok "部署校验通过"
  else
    VERIFY_BAD=1
    warn "部署校验存在告警项，请按上方提示处理"
  fi
  return 0
}

# ============================================================================
# MCP Server（本机运行于仓库 mcp_server/ 源码目录）
# ============================================================================
sync_mcp_code() {
  step "校验 MCP Server 代码（本机运行，源码目录 ${MCP_SRC_DIR}）"
  if [[ ! -d "${MCP_SRC_DIR}" ]]; then
    fail "未找到 ${MCP_SRC_DIR}，请确认仓库 mcp_server/ 存在"
    exit 1
  fi
  if [[ -f "${MCP_SRC_DIR}/requirements.txt" ]]; then
    # 系统 pip 受外部管理（PEP 668）时安装会失败，此处给出可操作提示但不阻断（依赖可能已满足）
    if ! python3 -m pip install -q -r "${MCP_SRC_DIR}/requirements.txt" 2>/tmp/mcp-pip.err; then
      warn "MCP 依赖安装未完全成功（系统 pip 可能受外部管理限制）。若启动失败请手动安装："
      warn "  python3 -m pip install -r ${MCP_SRC_DIR}/requirements.txt"
      [[ -s /tmp/mcp-pip.err ]] && warn "  $(tail -n 1 /tmp/mcp-pip.err 2>/dev/null)"
    fi
  fi
  ok "MCP 代码就绪（本机）"
}

mcp_is_running() {
  # 跨平台端口探测（bash 内置 /dev/tcp）
  (exec 3<>"/dev/tcp/127.0.0.1/${MCP_PORT}") 2>/dev/null
}

launch_mcp_server() {
  if mcp_is_running; then
    ok "MCP Server 已在运行 (端口 ${MCP_PORT})"
    return 0
  fi
  step "启动领域技能 MCP Server（本机 :${MCP_PORT}）"
  set -a; [[ -f "${DB_ENV}" ]] && source "${DB_ENV}"; set +a
  cd "${MCP_SRC_DIR}"
  MCP_PORT="${MCP_PORT}" MCP_HOST="${MCP_HOST}" AGENTTEAMS_ENV_FILE="${DB_ENV}" \
    nohup python3 server.py > /tmp/agentteams-mcp.log 2>&1 &
  echo $! > /tmp/agentteams-mcp.pid
  sleep 2
  local i
  for i in $(seq 1 15); do
    if mcp_is_running; then ok "MCP Server 健康检查通过 (127.0.0.1:${MCP_PORT})"; cd "${REPO_ROOT}"; return 0; fi
    sleep 1
  done
  cd "${REPO_ROOT}"
  warn "MCP Server 未在 15s 内就绪，请检查 /tmp/agentteams-mcp.log"
}

stop_mcp_server() {
  if [[ -f /tmp/agentteams-mcp.pid ]]; then
    kill "$(cat /tmp/agentteams-mcp.pid)" 2>/dev/null \
      || pkill -f "python3 server.py" 2>/dev/null || true
    rm -f /tmp/agentteams-mcp.pid
  fi
  ok "MCP Server 已停止"
}

# ============================================================================
# 平台启停
# ============================================================================
platform_start() {
  if docker ps --format '{{.Names}}' | grep -qxF "${CONTROLLER}" 2>/dev/null; then
    ok "AgentTeams 平台已在运行"
  else
    docker start "${CONTROLLER}" "${MANAGER}" >/dev/null 2>&1 || true
    # dashboard 可能未安装（AGENTTEAMS_DASHBOARD=0），存在才启动
    if docker ps -a --format '{{.Names}}' | grep -qxF "${DASHBOARD}" 2>/dev/null; then
      docker start "${DASHBOARD}" >/dev/null 2>&1 || true
    fi
  fi
  wait_controller_ready && ok "controller 就绪" || warn "controller 未就绪"
}
# ============================================================================
# MinIO 控制台暴露（内嵌于 controller 容器，端口未映射到宿主机；用 socat 转发）
# ============================================================================
expose_minio_console() {
  local console_port="${MINIO_CONSOLE_PORT:-9001}" ip p
  ip="$(docker inspect "${CONTROLLER}" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null)"
  if [[ -z "${ip}" ]]; then
    warn "无法获取 controller IP，跳过 MinIO 控制台暴露"
    return 0
  fi
  # 清理旧转发（容器重建后 IP 可能漂移），按监听端口精确匹配，避免误杀其它 socat
  for p in $(pgrep -x socat 2>/dev/null); do
    if ps -p "${p}" -o args= 2>/dev/null | grep -q "TCP-LISTEN:${console_port}"; then
      kill "${p}" 2>/dev/null || true
    fi
  done
  sleep 0.5
  setsid socat "TCP-LISTEN:${console_port},fork,reuseaddr" "TCP:${ip}:${console_port}" \
    >/var/log/minio-console-socat.log 2>&1 &
  sleep 1
  if curl -sf -o /dev/null "http://127.0.0.1:${console_port}/login"; then
    ok "MinIO 控制台已暴露: http://<本机>:${console_port}（凭据见 controller 的 AGENTTEAMS_MINIO_USER/PASSWORD）"
  else
    warn "MinIO 控制台暴露失败，请检查 /var/log/minio-console-socat.log"
  fi
}

stop_minio_console() {
  local console_port="${MINIO_CONSOLE_PORT:-9001}" p
  for p in $(pgrep -x socat 2>/dev/null); do
    if ps -p "${p}" -o args= 2>/dev/null | grep -q "TCP-LISTEN:${console_port}"; then
      kill "${p}" 2>/dev/null || true
    fi
  done
  ok "MinIO 控制台转发已停止"
}

platform_stop() {
  # 先优雅休眠 worker，再停平台容器
  local w
  while IFS= read -r w; do
    [[ -z "${w}" ]] && continue
    docker exec "${CONTROLLER}" agt worker sleep --name "${w}" >/dev/null 2>&1 || true
  done < <(get_registered_workers)
  docker stop "${MANAGER}" "${CONTROLLER}" >/dev/null 2>&1 || true
  if docker ps --format '{{.Names}}' | grep -qxF "${DASHBOARD}" 2>/dev/null; then
    docker stop "${DASHBOARD}" >/dev/null 2>&1 || true
  fi
  stop_minio_console
  ok "AgentTeams 平台已停止"
}

print_summary() {
  echo ""
  echo -e "${BOLD}===== 部署状态汇总 =====${NC}"
  echo "本机: $(hostname 2>/dev/null || echo unknown)"
  echo ""
  echo "数据库栈:"
  db_stack_status
  echo ""
  if mcp_is_running; then echo -e "  ${GREEN}✓${NC} MCP Server      http://127.0.0.1:${MCP_PORT}/mcp (本机)"; else echo -e "  ${RED}✗${NC} MCP Server      未运行"; fi
  if docker ps --format '{{.Names}}' | grep -qxF "${CONTROLLER}" 2>/dev/null; then
    echo -e "  ${GREEN}✓${NC} AgentTeams      Console :${AGENTTEAMS_PORT_CONSOLE:-18001}  Element :${AGENTTEAMS_PORT_ELEMENT_WEB:-18088}  Gateway :${AGENTTEAMS_PORT_GATEWAY:-18080}  MinIO :${MINIO_CONSOLE_PORT:-9001}"
  else
    echo -e "  ${RED}✗${NC} AgentTeams       未运行"
  fi
  if [[ "${VERIFY_BAD:-0}" == "1" ]]; then
    echo -e "  ${RED}✗${NC} 部署校验存在硬性失败项（见上方 [FAIL]；角色缺 spec.skills 时领域流程无法运行）"
  fi
  echo ""
  echo "本地访问: http://127.0.0.1:${MCP_PORT}/mcp   控制台 :${AGENTTEAMS_PORT_CONSOLE:-18001} / :${AGENTTEAMS_PORT_ELEMENT_WEB:-18088} / :${AGENTTEAMS_PORT_GATEWAY:-18080}"
}
