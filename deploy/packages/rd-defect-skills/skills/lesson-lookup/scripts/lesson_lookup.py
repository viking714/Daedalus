"""lesson-lookup 核心脚本 — 历史经验查询。

按角色模式查询 lessons 历史经验并分级。

注意：此功能依赖 lessons 表（设计 §5.2），计划在第二步中实现。
当前为占位脚本，lessons 表不可用时返回空集并降级提示。
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)


def lookup_lessons(query_text: str, mode: str = "analyzer",
                   repo: str = "", top_k: int = 5,
                   success_only: bool = False) -> dict:
    """按角色模式查询 lessons 历史经验。

    Args:
        query_text: 查询文本（Issue 描述 或 fix_pattern + error_signature）
        mode: "analyzer" 或 "fixer"
        repo: 仓库过滤
        top_k: analyzer 默认 5，fixer 默认 3
        success_only: fixer 模式默认 True

    Returns:
        按相似度分级的匹配 lessons 列表：
        {high: [], medium: [], low: [], status}
    """
    if not query_text:
        return {"status": "error", "reason": "query_text required"}

    if mode not in ("analyzer", "fixer"):
        return {"status": "error", "reason": f"unknown mode: {mode}"}

    # 默认 top_k 按模式
    if top_k == 5 and mode == "fixer":
        top_k = 3

    # TODO: 对接 lessons 表（第二步实现）
    # 当前 lessons 表不存在，返回空集并降级提示
    return {
        "status": "unavailable",
        "reason": "lessons table not yet implemented; returning empty result set",
        "high": [],
        "medium": [],
        "low": [],
        "hint": "按标准流程执行分析/修复",
        "mode": mode,
    }


def classify_by_score(results: list) -> dict:
    """按相似度 score 三级分流。

    Args:
        results: [{id, root_cause, fix_pattern, score, ...}]

    Returns:
        {high: [], medium: [], low: []}
    """
    high, medium, low = [], [], []
    for r in results:
        score = r.get("score", 0)
        if score >= 0.85:
            high.append(r)
        elif score >= 0.60:
            medium.append(r)
        else:
            low.append(r)
    return {"high": high, "medium": medium, "low": low}
