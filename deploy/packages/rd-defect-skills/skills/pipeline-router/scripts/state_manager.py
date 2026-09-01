"""pipeline-router 核心脚本 — 状态管理器。

从 skills.py 提取，持久化 TaskState 迁移，
并强制闭环阈值（轮次/文件数/Token/超时/单阶段重试/PO 回退）。
"""

from datetime import datetime, timezone
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)

MAX_ROUND = 3
MAX_FILES = 5
TOKEN_BUDGET = 100000
TASK_TIMEOUT_MIN = 30
INCIDENT_TIMEOUT_MIN = 15

# 灰度发布阈值
REGRESSION_CYCLE_MAX = 3
CANARY_TIMEOUT_MIN = 24 * 60

# 双闸门阈值
SINGLE_STAGE_RETRY_MAX = 2
PO_ROLLBACK_MAX = 1


class _StateStore:
    """TaskState 的轻量内存实现（状态机 + 闭环阈值闸门 + 双闸门计数）。"""

    def __init__(self):
        self._states = {}
        self._versions = {}

    def transition(self, task_id, from_stage, to_stage, owner_agent,
                   reason, extra=None) -> dict:
        if task_id not in self._states:
            if from_stage not in (None, "received"):
                return {
                    "accepted": False,
                    "reason": "unknown task; expected from_stage=received",
                }
        else:
            cur = self._states[task_id]["stage"]
            if from_stage and cur != from_stage:
                return {
                    "accepted": False,
                    "reason": f"stage mismatch: current={cur} expected={from_stage}",
                }
        state = {
            "stage": to_stage,
            "owner_agent": owner_agent,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            state.update(extra)
        self._states[task_id] = state
        self._versions[task_id] = self._versions.get(task_id, 0) + 1
        return {
            "accepted": True,
            "state_version": self._versions[task_id],
            "state": state,
        }

    def get(self, task_id):
        return self._states.get(task_id)


# 全局单例
_STATE_STORE = _StateStore()


def manage_state(
    task_id: str,
    from_stage: str = None,
    to_stage: str = None,
    owner_agent: str = None,
    reason: str = "",
    round_num: int = None,
    modified_files_count: int = None,
    tokens_used: int = None,
    regression_cycle_count: int = None,
    stage_retry_count: int = None,
    po_rollback_count: int = None,
    task_type: str = None,
) -> dict:
    """TaskState 迁移 + 闭环阈值闸门 + 双闸门计数。

    Args:
        task_id: 任务 ID（必填）
        from_stage: 来源阶段
        to_stage: 目标阶段
        owner_agent: 负责 Agent
        reason: 迁移原因
        round_num: 当前轮次
        modified_files_count: 修改文件数
        tokens_used: 已使用 token 数
        regression_cycle_count: 灰度回归次数
        stage_retry_count: 单阶段重试次数
        po_rollback_count: 回退至 PO 的次数
        task_type: incident / bug / feature

    Returns:
        {accepted, state_version, state, compress?, decision?}
    """
    if not task_id:
        return {"accepted": False, "reason": "task_id required"}

    # 闭环阈值闸门
    if round_num is not None and round_num > MAX_ROUND:
        return {
            "accepted": False,
            "decision": "escalated",
            "reason": f"round {round_num} exceeds max_round={MAX_ROUND}",
        }
    if regression_cycle_count is not None and regression_cycle_count > REGRESSION_CYCLE_MAX:
        return {
            "accepted": False,
            "decision": "escalated",
            "reason": f"regression cycle {regression_cycle_count} exceeds max={REGRESSION_CYCLE_MAX}",
        }
    if modified_files_count is not None and modified_files_count > MAX_FILES:
        return {
            "accepted": False,
            "reason": f"modified files {modified_files_count} exceeds budget {MAX_FILES}",
        }

    # 双闸门
    if stage_retry_count is not None and stage_retry_count > SINGLE_STAGE_RETRY_MAX:
        return {
            "accepted": False,
            "decision": "rollback_one_stage",
            "reason": f"stage retry {stage_retry_count} exceeds {SINGLE_STAGE_RETRY_MAX}",
        }
    if po_rollback_count is not None and po_rollback_count > PO_ROLLBACK_MAX:
        return {
            "accepted": False,
            "decision": "escalated",
            "reason": f"PO rollback {po_rollback_count} exceeds {PO_ROLLBACK_MAX}",
        }

    compress = False
    if tokens_used is not None and tokens_used > TOKEN_BUDGET:
        compress = True

    # 超时判定
    st = _STATE_STORE.get(task_id)
    extra = {}
    if st and st.get("started_at"):
        started = datetime.fromisoformat(st["started_at"])
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        timeout_min = INCIDENT_TIMEOUT_MIN if task_type == "incident" else TASK_TIMEOUT_MIN
        if elapsed > timeout_min * 60:
            return {"accepted": False, "decision": "escalated", "reason": "task timeout"}
    if not st:
        extra["started_at"] = datetime.now(timezone.utc).isoformat()

    # awaiting_release 记录进入时间
    if to_stage == "awaiting_release":
        extra["release_entered_at"] = datetime.now(timezone.utc).isoformat()
    # 回归次数回灌
    if regression_cycle_count is not None:
        extra["regression_cycle_count"] = regression_cycle_count
    # 双闸门计数回灌
    if stage_retry_count is not None:
        extra["stage_retry_count"] = stage_retry_count
    if po_rollback_count is not None:
        extra["po_rollback_count"] = po_rollback_count

    result = _STATE_STORE.transition(
        task_id=task_id,
        from_stage=from_stage,
        to_stage=to_stage,
        owner_agent=owner_agent or "manager",
        reason=reason or "",
        extra=extra,
    )
    result["compress"] = compress
    return result
