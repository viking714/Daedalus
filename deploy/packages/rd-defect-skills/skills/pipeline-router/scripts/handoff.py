"""pipeline-router 核心脚本 — 人工交接包生成。

从 skills.py 提取，在达到阈值/高风险时生成结构化的人工移交包。
"""

import uuid
from datetime import datetime, timezone
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)


def _rand(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_handoff(task_id: str = None, rounds: int = None,
                     last_context_pack: str = None,
                     last_failure_reason: str = None) -> dict:
    """生成人工移交包。

    Args:
        task_id: 任务 ID
        rounds: 已执行轮次
        last_context_pack: 最后一轮上下文
        last_failure_reason: 最后失败原因

    Returns:
        {handoff_id, task_id, status, included}
    """
    task_id = task_id or _rand("TASK")
    return {
        "handoff_id": _rand("HO"),
        "task_id": task_id,
        "status": "pending_human_review",
        "included": {
            "rounds": rounds,
            "last_context_pack": last_context_pack,
            "last_failure_reason": last_failure_reason,
            "generated_at": _now_iso(),
        },
    }
