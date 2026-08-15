"""pipeline-router 核心脚本 — 循环判定。

检测重复失败模式，触发人工交接。

Args:
    task_history: 任务历史记录列表 [{stage, decision, reason, ...}]
    max_cycles: 最大重复失败次数，默认 3

Returns:
    {should_handoff: bool, reason: str, cycle_count: int}
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)


def judge_loop(task_history: list = None, max_cycles: int = 3) -> dict:
    """循环判定：检测重复失败模式。

    Args:
        task_history: [{stage, decision, reason}] 历史记录
        max_cycles: 最大重复失败次数

    Returns:
        {should_handoff, reason, cycle_count}
    """
    task_history = task_history or []

    # 统计连续测试失败的次数
    consecutive_failures = 0
    for entry in reversed(task_history):
        if entry.get("decision") in ("retry", "fail"):
            consecutive_failures += 1
        else:
            break

    should_handoff = consecutive_failures >= max_cycles
    return {
        "should_handoff": should_handoff,
        "reason": f"consecutive failures: {consecutive_failures} >= {max_cycles}" if should_handoff else "",
        "cycle_count": consecutive_failures,
    }
