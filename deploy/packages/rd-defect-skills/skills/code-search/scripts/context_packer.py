"""code-search 核心脚本 — 搜索结果上下文打包。

从 skills.py 提取，负责将 MCP 原语 `hybrid_search` 的融合结果打包为结构化上下文。
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)


def pack_context(chunks: list, issue: dict = None) -> dict:
    """将检索结果打包为 LLM 可消费的结构化上下文。

    Args:
        chunks: [{path, symbol, content, score, ...}] 来自 hybrid_search 的结果
        issue: {title, description} 原始 Issue 信息

    Returns:
        {context_pack: str, chunk_count: int}
    """
    issue = issue or {}
    lines = [
        f"# 缺陷上下文",
        f"标题: {issue.get('title', 'N/A')}",
        f"描述: {issue.get('description', 'N/A')}",
        "",
    ]
    for i, c in enumerate(chunks):
        lines.append(f"## 候选片段 {i + 1} (score={c.get('score', '?')})")
        lines.append(f"来源: {c.get('path', '?')} :: {c.get('symbol', '')}")
        lines.append(c.get("content", ""))
        lines.append("")
    return {"context_pack": "\n".join(lines), "chunk_count": len(chunks)}


def format_semantic_results(results: list) -> list:
    """将 hybrid_search 结果转换为设计文档标准输出格式。

    Args:
        results: 来自 hybrid_search 的原始结果

    Returns:
        [{file, start_line, end_line, content, score, language}]
    """
    formatted = []
    for r in results:
        formatted.append({
            "file": r.get("path", ""),
            "start_line": r.get("start_line", 1),
            "end_line": r.get("end_line", r.get("start_line", 1) + 10),
            "content": r.get("content", ""),
            "score": r.get("score", 0),
            "language": r.get("language", ""),
        })
    return formatted
