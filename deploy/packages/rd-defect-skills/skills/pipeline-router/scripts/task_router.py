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

# 流水线阶段 → 负责 Agent（对齐方案设计 v2.2 §2.2 完整状态机）
# received → analyzing → fixing → testing → evaluating → awaiting_release → resolved / escalated
_PIPELINE = [
    ("received", "manager"),
    ("analyzing", "analyzer"),
    ("fixing", "fixer"),
    ("testing", "tester"),
    ("evaluating", "evaluator"),
    ("awaiting_release", "manager"),
]

# 终态集合（任务仅在这些状态真正结束）
_TERMINAL_STAGES = {"resolved", "escalated"}


def route_task(current_stage: str = None, round_num: int = None) -> dict:
    """任务路由决策。

    Args:
        current_stage: 当前阶段（received/analyzing/fixing/testing/evaluating/awaiting_release）
        round_num: 当前轮次

    Returns:
        {next_agent, next_stage, reason}
    """
    # 阈值检查：达最大轮次转人工介入（终态 escalated）
    if round_num is not None and round_num >= MAX_ROUND:
        return {
            "next_agent": None,
            "next_stage": "escalated",
            "reason": f"round {round_num} >= max_round {MAX_ROUND}",
        }

    # 终态不推进
    if current_stage in _TERMINAL_STAGES:
        return {
            "next_agent": None,
            "next_stage": current_stage,
            "reason": "terminal stage",
        }

    # 入口：received / 无状态 → analyzing
    if not current_stage or current_stage == "received":
        return {
            "next_agent": "analyzer",
            "next_stage": "analyzing",
            "reason": "entry from received",
        }

    # 常规流水线推进
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

    # awaiting_release 之后由灰度结果事件驱动（release_decision），不自动推进
    return {
        "next_agent": None,
        "next_stage": "awaiting_release",
        "reason": "awaiting canary result; release decision required",
    }
