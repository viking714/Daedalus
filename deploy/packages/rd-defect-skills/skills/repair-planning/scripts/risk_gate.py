"""repair-planning 核心脚本 — 风险闸门。

从 skills.py 提取，默认拒绝原则：
敏感模块（auth/payment/db_schema/security/crypto）+ 高风险 → block，
需要人工审批。
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)

# 敏感模块名单
_SENSITIVE_MODULES = {"auth", "payment", "db_schema", "security", "crypto"}


def check_risk_gate(risk_level: str = "low", touches: list = None,
                    approval_required: bool = False,
                    approved: bool = False) -> dict:
    """风险闸门检查。

    Args:
        risk_level: 风险等级（low/medium/high/critical）
        touches: 涉及的模块列表
        approval_required: 是否需要审批
        approved: 是否已审批

    Returns:
        {allowed: bool, reason: str, touched_sensitive: list}
    """
    touches = touches or []
    risk_level = (risk_level or "low").lower()

    touched_sensitive = [t for t in touches if t in _SENSITIVE_MODULES]

    reasons = []
    if approval_required and not approved:
        reasons.append("requires human approval but not approved")
    if touched_sensitive and risk_level in ("high", "critical"):
        reasons.append(
            f"sensitive modules touched with {risk_level} risk: {touched_sensitive}"
        )

    return {
        "allowed": len(reasons) == 0,
        "reason": "; ".join(reasons) or "passed default-deny check",
        "touched_sensitive": touched_sensitive,
    }
