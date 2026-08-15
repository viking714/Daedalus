"""pipeline-router 核心脚本 — 状态管理器。

从 skills.py 提取，持久化 TaskState 迁移，
并强制闭环阈值（轮次/文件数/Token/超时）。
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


class _StateStore:
    """TaskState 的轻量内存实现（状态机 + 闭环阈值闸门）。"""

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


def manage_state(task_id: str, from_stage: str = None,
                 to_stage: str = None, owner_agent: str = None,
                 reason: str = "", round_num: int = None,
                 modified_files_count: int = None,
                 tokens_used: int = None) -> dict:
    """TaskState 迁移 + 闭环阈值闸门。

    Args:
        task_id: 任务 ID（必填）
        from_stage: 来源阶段
        to_stage: 目标阶段
        owner_agent: 负责 Agent
        reason: 迁移原因
        round_num: 当前轮次
        modified_files_count: 修改文件数
        tokens_used: 已使用 token 数

    Returns:
        {accepted, state_version, state, compress?, decision?}
    """
    if not task_id:
        return {"accepted": False, "reason": "task_id required"}

    # 闭环阈值闸门
    if round_num is not None and round_num > MAX_ROUND:
        return {
            "accepted": False,
            "decision": "handoff",
            "reason": f"round {round_num} exceeds max_round={MAX_ROUND}",
        }
    if modified_files_count is not None and modified_files_count > MAX_FILES:
        return {
            "accepted": False,
            "reason": f"modified files {modified_files_count} exceeds budget {MAX_FILES}",
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
        if elapsed > TASK_TIMEOUT_MIN * 60:
            return {"accepted": False, "decision": "handoff", "reason": "task timeout"}
    if not st:
        extra["started_at"] = datetime.now(timezone.utc).isoformat()

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
