#!/usr/bin/env bash
# 在本地 OpenClaw / AgentTeams 上接入阿里云 AgentLoop 可观测。
# 本脚本从 deploy/install/agentteams.env 读取凭证，执行官方 OpenClaw 接入脚本。
# 注意：agentteams.env 含真实密钥，已被 .gitignore 忽略，请勿提交。
#
# 用法：
#   ./deploy/install/setup-agentloop.sh
#
# 前置条件：
#   1. 已在阿里云 AgentLoop 控制台开通服务并创建 AgentSpace
#   2. 已在「接入中心 > OpenClaw」复制 Endpoint / LicenseKey / Project / Workspace / serviceName
#   3. 已将上述值填入 deploy/install/agentteams.env

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/agentteams.env"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "错误：未找到 ${ENV_FILE}，请先复制 agentteams.env.example 并填写凭证。" >&2
    exit 1
fi

# 读取环境变量（仅导出 AGENTLOOP_* 相关变量）
set -a
# shellcheck source=/dev/null
source "${ENV_FILE}"
set +a

missing=()
for var in AGENTLOOP_ENDPOINT AGENTLOOP_LICENSE_KEY AGENTLOOP_PROJECT AGENTLOOP_WORKSPACE AGENTLOOP_SERVICE_NAME; do
    if [[ -z "${!var:-}" ]]; then
        missing+=("${var}")
    fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "错误：agentteams.env 中以下 AgentLoop 变量未填写：${missing[*]}" >&2
    exit 1
fi

echo "即将使用以下配置接入 AgentLoop："
echo "  Endpoint:      ${AGENTLOOP_ENDPOINT}"
echo "  Project:       ${AGENTLOOP_PROJECT}"
echo "  Workspace:     ${AGENTLOOP_WORKSPACE}"
echo "  ServiceName:   ${AGENTLOOP_SERVICE_NAME}"
echo "  LicenseKey:    ${AGENTLOOP_LICENSE_KEY:0:8}..."
echo ""

# 执行官方 OpenClaw 接入脚本
# 参考：https://help.aliyun.com/zh/document_detail/3042581.html
curl -fsSL "https://arms-apm-cn-hangzhou-pre.oss-cn-hangzhou.aliyuncs.com/opentelemetry-instrumentation-openclaw/install.sh" | bash -s -- \
    --x-arms-license-key "${AGENTLOOP_LICENSE_KEY}" \
    --x-arms-project "${AGENTLOOP_PROJECT}" \
    --x-cms-workspace "${AGENTLOOP_WORKSPACE}" \
    --serviceName "${AGENTLOOP_SERVICE_NAME}" \
    --endpoint "${AGENTLOOP_ENDPOINT}"

echo ""
echo "AgentLoop 接入脚本执行完成。"
echo "建议重启 OpenClaw gateway / AgentTeams controller 使配置生效。"
