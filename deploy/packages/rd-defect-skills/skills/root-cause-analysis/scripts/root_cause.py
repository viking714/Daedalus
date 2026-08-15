"""root-cause-analysis 核心脚本 — 根因推断。

从 skills.py 提取，结合 Neo4j 依赖图与代码上下文做根因分析。
Neo4j 不可用时降级为启发式推断。
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    from mcp_server.db.base import DbUnavailable
    from mcp_server.db.neo4jgraph import Neo4jStore
except ImportError:
    from db.base import DbUnavailable
    from db.neo4jgraph import Neo4jStore


def analyze_root_cause(context_pack: str = "", suspect_symbol: str = "",
                       ns: str = "") -> dict:
    """根因分析：结合 Neo4j 依赖子图与代码上下文。

    Args:
        context_pack: 来自 code-search 的结构化上下文
        suspect_symbol: 嫌疑符号名（可选，用于图谱查询）
        ns: 命名空间

    Returns:
        {root_cause, evidence, confidence}
    """
    evidence = []

    # 尝试 Neo4j 依赖子图增强
    try:
        if suspect_symbol:
            neo = Neo4jStore()
            subs = neo.dependency_subgraph([suspect_symbol], depth=2, ns=ns)
            evidence.append(f"依赖子图调用方: {subs.get('direct_callers')}")
    except DbUnavailable:
        pass  # 降级为启发式

    # 启发式根因推断
    rc = "（启发式）空指针/越界访问，源于上游未校验返回"
    if "None" in context_pack or "null" in context_pack.lower():
        rc = "（启发式）对可能为 null/None 的返回值缺少校验"

    return {
        "root_cause": rc,
        "evidence": evidence,
        "confidence": 0.6 if evidence else 0.4,
    }
