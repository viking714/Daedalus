"""repair-planning 核心脚本 — 修复方案规划。

从 skills.py 提取，基于根因和影响面生成修复步骤计划，
受 MAX_FILES=5 约束。
"""

import uuid
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)

MAX_FILES = 5


def _plan_id():
    return f"PLAN-{uuid.uuid4().hex[:8]}"


def generate_repair_plan(root_cause: dict = None, impact: dict = None,
                         suspect_files: list = None,
                         max_files: int = MAX_FILES) -> dict:
    """生成修复计划。

    Args:
        root_cause: 根因分析结果
        impact: 影响面分析结果
        suspect_files: 嫌疑文件列表
        max_files: 单轮文件上限

    Returns:
        {plan_id, steps, files_budget, rollback_plan, based_on}
    """
    root_cause = root_cause or {}
    suspect_files = suspect_files or []

    # 从影响面结果中提取候选文件
    if isinstance(impact, dict):
        cand = impact.get("impact_scope", {}).get("changed_files", []) or suspect_files
    else:
        cand = suspect_files

    cand = cand[:max_files]

    steps = [
        {
            "action": "guard_check",
            "target": f,
            "detail": "增加非空/边界校验",
        }
        for f in cand
    ]

    return {
        "plan_id": _plan_id(),
        "steps": steps,
        "files_budget": max_files,
        "rollback_plan": "git revert 改动并提交回滚评审",
        "based_on": root_cause,
    }
