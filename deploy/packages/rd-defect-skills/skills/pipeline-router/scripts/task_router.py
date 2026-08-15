"""pipeline-router 核心脚本 — 任务路由。

从 skills.py 提取，根据任务当前阶段路由到下一个 Worker。
达 MAX_ROUND=3 时转人工移交。
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)

MAX_ROUND = 3

# 流水线阶段 → 负责 Agent
_PIPELINE = [
    ("analyzing", "analyzer"),
    ("fixing", "fixer"),
    ("testing", "tester"),
    ("evaluating", "evaluator"),
]


def route_task(current_stage: str = None, round_num: int = None) -> dict:
    """任务路由决策。

    Args:
        current_stage: 当前阶段（analyzing/fixing/testing/evaluating）
        round_num: 当前轮次

    Returns:
        {next_agent, next_stage, reason}
    """
    # 阈值检查
    if round_num is not None and round_num >= MAX_ROUND:
        return {
            "next_agent": None,
            "next_stage": "handoff",
            "reason": f"round {round_num} >= max_round {MAX_ROUND}",
        }

    # 流水线推进
    if current_stage and current_stage != "received":
        idx = next(
            (i for i, (s, _) in enumerate(_PIPELINE) if s == current_stage), None
        )
        if idx is not None and idx + 1 < len(_PIPELINE):
            nxt_stage, nxt_agent = _PIPELINE[idx + 1]
            return {
                "next_agent": nxt_agent,
                "next_stage": nxt_stage,
                "reason": "pipeline advance",
            }
        return {"next_agent": None, "next_stage": "done", "reason": "pipeline complete"}

    # 默认入口
    return {
        "next_agent": "analyzer",
        "next_stage": "analyzing",
        "reason": "default entry",
    }
