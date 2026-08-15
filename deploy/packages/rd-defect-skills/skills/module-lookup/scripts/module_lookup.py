"""module-lookup 核心脚本 — 模块定位。

从 skills.py 提取，将领域概念映射到负责模块和关键文件。
使用向量搜索找到与概念最相关的模块。
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    from mcp_server.db.base import DbUnavailable
    from mcp_server.db.pgvector import PgVectorStore
    from mcp_server.embed.embeddings import EmbeddingService
except ImportError:
    from db.base import DbUnavailable
    from db.pgvector import PgVectorStore
    from embed.embeddings import EmbeddingService


def lookup_module(concept: str, ns: str = "") -> dict:
    """将领域概念映射到模块和关键文件。

    Args:
        concept: 领域概念/关键词/Issue 摘要
        ns: 命名空间

    Returns:
        {module, files, key_functions, description, warnings}
    """
    if not concept:
        return {"status": "error", "reason": "concept required"}

    try:
        emb = EmbeddingService()
        qv = emb.embed([concept])[0]
        pg = PgVectorStore()
        results = pg.vector_search(qv, 5, ns=ns)

        if results:
            top = results[0]
            module = (
                top.get("path", "").split("/")[0]
                if "/" in top.get("path", "")
                else "unknown"
            )
            files = list({r.get("path", "") for r in results[:5]})
            return {
                "module": module,
                "files": files,
                "key_functions": [r.get("symbol", "") for r in results[:3]],
                "description": f"Module related to '{concept}'",
                "warnings": [],
            }
        return {
            "module": "unknown",
            "files": [],
            "key_functions": [],
            "description": "No matching module found",
            "warnings": [],
        }
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}
