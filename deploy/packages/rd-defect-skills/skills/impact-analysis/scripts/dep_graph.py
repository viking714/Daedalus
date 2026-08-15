"""impact-analysis 核心脚本 — 依赖图分析。

从 skills.py 提取，基于 Neo4j CALLS/IMPORTS 子图估算真实波及范围与风险等级。
"""

import re
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


def analyze_impact(changed_files: list = None, patch_text: str = None,
                   ns: str = "") -> dict:
    """依赖图影响分析：估算真实波及范围与风险等级。

    优先用 Neo4j 依赖图；图库不可用/未索引时降级为启发式估算。

    Args:
        changed_files: 修改文件路径列表
        patch_text: diff 文本（可选，用于解析变更符号）
        ns: 命名空间

    Returns:
        {impact_scope, risk_level, note}
    """
    changed_files = changed_files or []

    if patch_text:
        parsed = _parse_patch_text(patch_text)
        changed_files = [f for f, _, _ in parsed] if parsed else changed_files

    modules = {f.split("/")[0] for f in changed_files if "/" in f}
    cross_module = len(modules) > 1

    # 优先用 Neo4j 依赖图
    direct_callers = None
    imported_files = None
    try:
        neo = Neo4jStore()
        stats = neo.impact_stats(changed_files, ns=ns)
        direct_callers = stats["direct_callers"]
        imported_files = stats["imported_files"]
    except Exception:
        direct_callers = None

    if direct_callers is None:
        direct_callers = max(1, len(changed_files) * 2)
        imported_files = len(modules)
        note = "heuristic estimate; Neo4j 不可用，已降级"
    elif direct_callers == 0 and imported_files == 0:
        direct_callers = max(1, len(changed_files) * 2)
        imported_files = len(modules)
        note = "heuristic fallback; 依赖图未匹配到改动文件（可能未索引）"
    else:
        note = "real Neo4j dependency graph"

    risk_level = "high" if (cross_module or direct_callers >= 10 or len(changed_files) >= 5) else "medium"

    return {
        "impact_scope": {
            "changed_files": changed_files,
            "direct_callers": direct_callers,
            "cross_module_edges": imported_files,
        },
        "risk_level": risk_level,
        "need_extra_tests": True,
        "note": note,
    }


_SIGNATURE_RE = re.compile(r"^\s*(def |public |private |protected |function |=>|\w+\s*\([^)]*\)\s*\{?)\s*\w+")


def _parse_patch_text(patch_text: str) -> list:
    """解析 unified diff 文本，返回 [(file, added_lines, removed_lines)]."""
    try:
        from unidiff import PatchSet
    except ImportError:
        return _parse_patch_manual(patch_text)
    ps = PatchSet.from_string(patch_text or "")
    return [(p.path,
             [l.value for h in p.hunks for l in h.target_lines()],
             [l.value for h in p.hunks for l in h.source_lines()]) for p in ps]


def _parse_patch_manual(patch_text: str) -> list:
    """手动的 unidiff 解析（unidiff 库不可用时降级）。"""
    files = []
    cur_file = "<unknown>"
    added, removed = [], []
    for line in (patch_text or "").splitlines():
        if line.startswith("+++ b/"):
            if cur_file != "<unknown>":
                files.append((cur_file, added, removed))
            cur_file = line[len("+++ b/"):].strip()
            added, removed = [], []
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    if cur_file != "<unknown>":
        files.append((cur_file, added, removed))
    return files
