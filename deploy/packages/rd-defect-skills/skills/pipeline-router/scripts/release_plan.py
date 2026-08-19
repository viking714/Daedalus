"""pipeline-router 核心脚本 — 灰度发布计划生成。

对齐方案设计 v2.2 §4.2：Evaluator 裁定通过后，Manager 生成
release_plan.json 作为灰度发布意图声明。

本脚本为纯函数：输入 task_id 与可选参数，输出结构化 dict，
由 Worker 持久化为 release_plan.json 写入 MinIO。
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_release_plan(task_id: str, canary_scope: str = None,
                          risk_level: str = None, rollback_point: str = None,
                          approver: str = None, soak_window_min: int = None,
                          promote_threshold: dict = None) -> dict:
    """生成灰度发布计划 release_plan.json（意图声明）。

    Args:
        task_id: 任务 ID（必填）
        canary_scope: 灰度范围（流量比例、地域等），默认 5% 流量
        risk_level: 风险等级 L0–L3，默认 L2（灰度 + 审批）
        rollback_point: 回滚基线 tag
        approver: 审批人，默认 human
        soak_window_min: 观察窗口（分钟），默认 30
        promote_threshold: 灰度通过阈值（如错误率上限）

    Returns:
        结构化的 release_plan dict（供持久化为 release_plan.json）
    """
    if not task_id:
        return {"status": "error", "reason": "task_id required"}

    release_plan = {
        "task_id": task_id,
        "canary_scope": canary_scope or "5% 流量 / region=default",
        "risk_level": risk_level or "L2",
        "rollback_point": rollback_point or f"git tag pre-fix-{task_id}",
        "approver": approver or "human",
        "soak_window_min": int(soak_window_min or 30),
        "promote_threshold": promote_threshold or {"error_rate_max": 0.01},
        "pr_desc_note": "PR 描述禁用 closes/fixes 关键字，关单动作须由 Agent 显式执行",
        "created_at": _now_iso(),
        "status": "pending_approval",
    }
    return {
        "status": "ok",
        "release_plan": release_plan,
        "next_stage": "awaiting_release",
    }
