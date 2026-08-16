"""lesson-lookup 核心脚本 — 历史经验查询。

对齐方案设计 §5.4「查询复用」：
- Analyzer 按根因维度查询（mode=analyzer，top_k=5）
- Fixer 按改法维度查询（mode=fixer，top_k=3，仅 success=true）

实现：将 query_text 向量化后调用 LessonsStore.search_similar，
再按相似度 score 三级分流（HIGH ≥ 0.85 / MEDIUM 0.60~0.85 / LOW < 0.60）。

依赖：mcp_server/db/lessons.py（LessonsStore）+ mcp_server/embed（EmbeddingService）。
lessons 表不可用（未建表 / 连接失败）时返回空集并降级提示，不抛异常。
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from mcp_server.db.base import DbUnavailable
from mcp_server.db.lessons import LessonsStore
from mcp_server.embed.embeddings import EmbeddingService

# 查询复用的三级分流阈值（设计 §5.4）
HIGH_THRESHOLD = 0.85
MEDIUM_THRESHOLD = 0.60


def lookup_lessons(query_text: str, mode: str = "analyzer",
                   repo: str = "", top_k: int = 5,
                   success_only: bool = False) -> dict:
    """按角色模式查询 lessons 历史经验并按相似度分级。

    Args:
        query_text: 查询文本（Issue 描述 或 fix_pattern + error_signature）
        mode: "analyzer" 或 "fixer"
        repo: 仓库过滤
        top_k: analyzer 默认 5，fixer 默认 3
        success_only: fixer 模式默认 True

    Returns:
        {
            status, mode, high: [], medium: [], low: [], count
        }
    """
    if not query_text:
        return {"status": "error", "reason": "query_text required"}

    if mode not in ("analyzer", "fixer"):
        return {"status": "error", "reason": f"unknown mode: {mode}"}

    # 模式默认值：fixer 默认 top_k=3、success_only=True
    if mode == "fixer":
        if top_k == 5:  # 未显式指定时用 fixer 默认值
            top_k = 3
        success_only = True

    try:
        emb = EmbeddingService()
        qv = emb.embed([query_text])[0]
        store = LessonsStore()
        results = store.search_similar(
            qv, repo=repo, top_k=top_k, success_only=success_only
        )
    except DbUnavailable as e:
        return {
            "status": "unavailable",
            "reason": str(e),
            "mode": mode,
            "high": [],
            "medium": [],
            "low": [],
            "count": 0,
            "hint": "lessons 表不可用，按标准流程执行分析/修复",
        }

    classified = classify_by_score(results)
    return {
        "status": "ok",
        "mode": mode,
        "high": classified["high"],
        "medium": classified["medium"],
        "low": classified["low"],
        "count": len(results),
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
        if score >= HIGH_THRESHOLD:
            high.append(r)
        elif score >= MEDIUM_THRESHOLD:
            medium.append(r)
        else:
            low.append(r)
    return {"high": high, "medium": medium, "low": low}
