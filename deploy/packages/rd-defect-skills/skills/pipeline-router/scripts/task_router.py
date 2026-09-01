"""pipeline-router 核心脚本 — 任务路由。

从 skills.py 提取，根据任务当前阶段路由到下一个 Worker。
支持 incident / bug / feature（含 greenfield）三类任务。
达 MAX_ROUND=3 时转人工移交。
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)

MAX_ROUND = 3

# 终态集合
_TERMINAL_STAGES = {"resolved", "escalated"}

# 任务类型 -> 流水线阶段序列 (stage, agent)
_PIPELINES = {
    "incident": [
        ("received", "manager"),
        ("triaging", "manager"),
        ("ops_diagnosing", "ops-analyst"),
        ("ops_remediation", "ops-analyst"),
    ],
    "bug": [
        ("received", "manager"),
        ("triaging", "manager"),
        ("analyzing", "architect"),
        ("fixing", "developer"),
        ("testing", "tester"),
        ("evaluating", "reviewer"),
        ("awaiting_release", "manager"),
    ],
    "feature": [
        ("received", "manager"),
        ("triaging", "manager"),
        ("clarifying", "po"),
        ("prd_drafting", "po"),
        ("prd_review", "reviewer"),
        ("designing", "architect"),
        ("design_review", "reviewer"),
        ("developing", "developer"),
        ("test_designing", "tester"),
        ("test_executing", "tester"),
        ("evaluating", "reviewer"),
        ("awaiting_release", "manager"),
    ],
    "greenfield": [
        ("received", "manager"),
        ("triaging", "manager"),
        ("clarifying", "po"),
        ("prd_drafting", "po"),
        ("prd_review", "reviewer"),
        ("designing", "architect"),
        ("design_review", "reviewer"),
        ("bootstrapping", "developer"),
        ("developing", "developer"),
        ("test_executing", "tester"),
        ("evaluating", "reviewer"),
        ("awaiting_release", "manager"),
    ],
}

# failure_class -> 回退目标阶段
_FAILURE_ROLLBACK = {
    "code": ("developing", "developer"),
    "design": ("designing", "architect"),
    "requirement": ("prd_drafting", "po"),
    "environment": ("ops_diagnosing", "ops-analyst"),
    "visual": ("developing", "developer"),
}


def route_task(
    current_stage: str = None,
    round_num: int = None,
    task_type: str = None,
    failure_class: str = None,
    greenfield: bool = False,
) -> dict:
    """任务路由决策。

    Args:
        current_stage: 当前阶段
        round_num: 当前轮次
        task_type: incident / bug / feature
        failure_class: 失败类别，用于回退仲裁
        greenfield: feature 且 repo 为空时设为 True

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

    # 回退仲裁
    if failure_class and failure_class in _FAILURE_ROLLBACK:
        target_stage, target_agent = _FAILURE_ROLLBACK[failure_class]
        return {
            "next_agent": target_agent,
            "next_stage": target_stage,
            "reason": f"rollback by failure_class={failure_class}",
        }

    # 入口：received / 无状态 -> triaging
    if not current_stage or current_stage == "received":
        return {
            "next_agent": "manager",
            "next_stage": "triaging",
            "reason": "entry from received",
        }

    # 选择流水线
    if task_type == "incident":
        pipeline = _PIPELINES["incident"]
    elif task_type == "bug":
        pipeline = _PIPELINES["bug"]
    else:
        pipeline = _PIPELINES["greenfield"] if greenfield else _PIPELINES["feature"]

    # 常规流水线推进
    idx = next(
        (i for i, (s, _) in enumerate(pipeline) if s == current_stage), None
    )
    if idx is not None and idx + 1 < len(pipeline):
        nxt_stage, nxt_agent = pipeline[idx + 1]
        return {
            "next_agent": nxt_agent,
            "next_stage": nxt_stage,
            "reason": "pipeline advance",
        }

    # awaiting_release 之后由灰度结果事件驱动
    return {
        "next_agent": None,
        "next_stage": "awaiting_release",
        "reason": "awaiting canary result; release decision required",
    }


if __name__ == "__main__":
    import json

    print(json.dumps(route_task(current_stage="received", task_type="bug")))
