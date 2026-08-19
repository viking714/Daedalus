"""pipeline-router 核心脚本 — 灰度结果确认决策 + 超时哨兵。

对齐方案设计 v2.2 §4.4 / §4.5 / §4.6：
- decide_release：消费 confirmation_report.json，决策关单 / 回滚 / escalated
- check_canary_timeout：awaiting_release 超时哨兵（默认 24h TTL）

本脚本为纯函数，状态由 Manager（LLM agent）在会话上下文中维护。
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from datetime import datetime, timezone

REGRESSION_CYCLE_MAX = 3
CANARY_TIMEOUT_MIN = 24 * 60  # 默认 24h


def decide_release(task_id: str, confirmation: dict = None,
                   regression_cycle_count: int = None) -> dict:
    """灰度结果确认闭环：消费 confirmation_report.json 决策。

    canary OK   → resolved（调用 GitHub API 关单）
    canary FAIL → 回归闭环（regression_cycle_count++，未超限回滚到 analyzing，
                  超限 → escalated 人工介入）。回归不新建 Issue，复用同一 task_id。

    Args:
        task_id: 任务 ID（必填）
        confirmation: confirmation_report.json 内容 {result, passed, ...}
        regression_cycle_count: 当前回归次数

    Returns:
        {decision, action, next_stage, regression_cycle_count?, feedback?, reason}
    """
    if not task_id:
        return {"status": "error", "reason": "task_id required"}

    confirmation = confirmation or {}
    if isinstance(confirmation, dict):
        canary_result = str(confirmation.get("result") or confirmation.get("canary_result") or "").lower()
        canary_passed = confirmation.get("passed")
    else:
        canary_result = str(confirmation).lower()
        canary_passed = None

    if canary_passed is None:
        passed = canary_result in ("ok", "pass", "passed", "success", "succeeded", "true")
    else:
        passed = bool(canary_passed)

    regression_cycle = int(regression_cycle_count or 0)

    if passed:
        return {
            "decision": "resolved",
            "action": "close_issue",
            "next_stage": "resolved",
            "reason": "canary OK; close issue",
        }

    # canary FAIL：回归闭环
    regression_cycle += 1
    if regression_cycle > REGRESSION_CYCLE_MAX:
        return {
            "decision": "escalated",
            "action": "escalate_to_human",
            "next_stage": "escalated",
            "regression_cycle_count": regression_cycle,
            "reason": f"canary FAIL and regression cycle {regression_cycle} exceeds max {REGRESSION_CYCLE_MAX}",
        }

    return {
        "decision": "rollback",
        "action": "feedback_to_analyzer",
        "next_stage": "analyzing",
        "regression_cycle_count": regression_cycle,
        "feedback": confirmation if isinstance(confirmation, dict) else {"result": confirmation},
        "reason": f"canary FAIL; rollback to analyzing (regression cycle {regression_cycle}/{REGRESSION_CYCLE_MAX})",
    }


def check_canary_timeout(entered_at_iso: str, timeout_min: int = None) -> dict:
    """awaiting_release 超时哨兵：巡检是否超时未收到 canary 结果。

    Args:
        entered_at_iso: 进入 awaiting_release 的时间（ISO 8601）
        timeout_min: 超时阈值（分钟），默认 24h

    Returns:
        {status: "waiting"|"escalated", action?, elapsed_min?, timeout_min?, reason}
    """
    timeout_min = int(timeout_min or CANARY_TIMEOUT_MIN)
    if not entered_at_iso:
        return {"status": "waiting", "reason": "no release_entered_at; assume just entered"}

    try:
        entered = datetime.fromisoformat(entered_at_iso)
    except ValueError:
        return {"status": "error", "reason": f"invalid entered_at_iso: {entered_at_iso}"}

    elapsed_min = (datetime.now(timezone.utc) - entered).total_seconds() / 60
    if elapsed_min > timeout_min:
        return {
            "status": "escalated",
            "action": "notify_human",
            "next_stage": "escalated",
            "reason": f"canary timeout: elapsed {elapsed_min:.0f}min > {timeout_min}min",
        }

    return {
        "status": "waiting",
        "elapsed_min": round(elapsed_min, 1),
        "timeout_min": timeout_min,
        "reason": "awaiting canary result",
    }
