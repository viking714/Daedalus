"""MCP 数据访问原语层 — 14 个细粒度工具（13 数据原语 + 1 共享服务）。

设计说明（对齐方案设计 v2.2 §3.3）：
- 本层是 MCP 协议层与数据层之间的「数据访问原语」，每个原语只回答"数据怎么取/写"，
  不含业务判断、工作流编排或 Agent 推理逻辑。
- 输入校验：每个原语对输入参数做基础检查（必填/类型/范围），不合法时返回 error。
- 错误处理：区分网络/连接错误（DbUnavailable）和业务参数错误（返回 error status）。
- 版本契约：原语签名保持向后兼容；破坏性变更通过新原语名引入。
- 并发安全：无状态设计，每次调用独立创建 DB 连接，多 Worker 并发安全。

分层架构：
    AgentTeams Skills 层（SKILL.md + scripts/）
        → 编排 MCP 原语完成工作流
            MCP 层（本文件 + mcp_server.py）
                → 14 个数据访问原语
                    数据层（pgvector / Neo4j / Meilisearch / Redis / embedding / AST）
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("mcp_primitives")

from mcp_server.db.base import DbUnavailable, content_hash
from mcp_server.db.pgvector import PgVectorStore
from mcp_server.db.neo4jgraph import Neo4jStore
from mcp_server.db.meili import MeiliStore
from mcp_server.db.redis_cache import RedisCache
from mcp_server.embed.embeddings import EmbeddingService
from mcp_server.code.ast_parser import AstParser

# --------------------------------------------------------------------------- #
# 公共工具
# --------------------------------------------------------------------------- #


def reciprocal_rank_fusion(ranked_lists: List[list], k: int = 60) -> List[dict]:
    """Reciprocal Rank Fusion：多路召回按排名融合。

    各路召回只需给出 [{chunk_id, score?, ...}]，同 chunk_id 跨路累加 1/(k+rank+1)。
    优先保留字段最丰富的元信息。
    """
    scores: Dict[str, float] = {}
    meta: Dict[str, dict] = {}
    for rl in ranked_lists:
        for rank, item in enumerate(rl):
            cid = item["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            if cid not in meta or len(item) > len(meta[cid]):
                meta[cid] = item
    fused = sorted(scores.items(), key=lambda x: -x[1])
    out = []
    for cid, s in fused:
        item = {kk: vv for kk, vv in meta[cid].items() if kk != "chunk_id"}
        item["chunk_id"] = cid
        item["score"] = round(s, 4)
        out.append(item)
    return out


# --------------------------------------------------------------------------- #
# pgvector 向量检索原语（4 个）
# --------------------------------------------------------------------------- #


def pgvector_search(query_text: str, top_k: int = 10, ns: str = "") -> Dict[str, Any]:
    """pgvector 向量语义检索 — 返回 Top-K 最相似代码块。

    Args:
        query_text: 自然语言查询文本
        top_k: 返回条数，默认 10
        ns: 仓库命名空间（repo 名），空串则不限制
    """
    if not query_text:
        return {"status": "error", "reason": "query_text required"}
    try:
        emb = EmbeddingService()
        qv = emb.embed([query_text])[0]
        pg = PgVectorStore()
        results = pg.vector_search(qv, top_k, ns=ns)
        return {"status": "ok", "results": results, "count": len(results)}
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def pgvector_fetch(chunk_ids: List[str], ns: str = "") -> Dict[str, Any]:
    """按 chunk_id 批量取代码块元信息。

    Args:
        chunk_ids: 要查询的 chunk_id 列表
        ns: 仓库命名空间
    """
    if not chunk_ids:
        return {"status": "error", "reason": "chunk_ids required"}
    try:
        pg = PgVectorStore()
        results = pg.fetch_by_ids(chunk_ids, ns=ns)
        return {"status": "ok", "results": results, "count": len(results)}
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def pgvector_upsert_chunk(chunk_id: str, ns: str, repo: str, path: str,
                            symbol: str, kind: str, content: str,
                            embedding: Optional[List[float]] = None) -> Dict[str, Any]:
    """写入/更新单个代码块到 pgvector。

    Args:
        chunk_id: 块唯一标识
        ns: 命名空间
        repo: 仓库名
        path: 文件路径
        symbol: 符号名
        kind: 类型（function/class/method/module）
        content: 代码内容
        embedding: 嵌入向量（可选，未提供时自动生成）
    """
    if not chunk_id or not content:
        return {"status": "error", "reason": "chunk_id and content required"}
    try:
        pg = PgVectorStore()
        if embedding is None:
            emb = EmbeddingService()
            embedding = emb.embed([content])[0]
        chunk = {"chunk_id": chunk_id, "repo": repo, "path": path,
                 "symbol": symbol, "kind": kind, "content": content}
        pg.upsert_chunk(chunk, embedding, ns=ns)
        return {"status": "ok", "chunk_id": chunk_id}
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def pgvector_delete(paths: List[str], ns: str = "") -> Dict[str, Any]:
    """按文件路径删除 pgvector 中的代码块。

    Args:
        paths: 文件路径列表
        ns: 命名空间
    """
    if not paths:
        return {"status": "error", "reason": "paths required"}
    try:
        pg = PgVectorStore()
        deleted = pg.delete_by_paths(paths, ns=ns)
        return {"status": "ok", "deleted": deleted}
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


# --------------------------------------------------------------------------- #
# Meilisearch 全文检索原语（1 个）
# --------------------------------------------------------------------------- #


def meili_keyword_search(query: str, top_k: int = 10, ns: str = "") -> Dict[str, Any]:
    """Meilisearch 全文关键词检索。

    Args:
        query: 关键词查询字符串
        top_k: 返回条数，默认 10
        ns: 命名空间（决定 Meilisearch 索引名）
    """
    if not query:
        return {"status": "error", "reason": "query required"}
    try:
        meili = MeiliStore()
        results = meili.keyword_search(query, top_k, ns=ns)
        return {"status": "ok", "results": results, "count": len(results)}
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


# --------------------------------------------------------------------------- #
# Neo4j 图谱原语（4 个）
# --------------------------------------------------------------------------- #


def neo4j_expand_chunks(seed_fqns: List[str], limit: int = 10, ns: str = "") -> Dict[str, Any]:
    """以种子 chunk 为入口，沿 CALLS/IMPORTS 关系扩展相关代码块。

    Args:
        seed_fqns: 种子 chunk_id 列表（作为图谱扩展入口点）
        limit: 返回相关块上限
        ns: 命名空间
    """
    if not seed_fqns:
        return {"status": "error", "reason": "seed_fqns required"}
    try:
        neo = Neo4jStore()
        related = neo.expand_chunks(seed_fqns, limit=limit, ns=ns)
        return {"status": "ok", "results": related, "count": len(related)}
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def neo4j_impact_stats(paths: List[str], ns: str = "") -> Dict[str, Any]:
    """统计修改文件的影响面：直接调用方数 + 跨文件导入数。

    Args:
        paths: 被修改的文件路径列表
        ns: 命名空间
    """
    if not paths:
        return {"status": "error", "reason": "paths required"}
    try:
        neo = Neo4jStore()
        stats = neo.impact_stats(paths, ns=ns)
        return {"status": "ok", **stats}
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def neo4j_symbol_lookup(query: str, top_k: int = 10, ns: str = "") -> Dict[str, Any]:
    """按符号名（函数/方法名）模糊查询 Neo4j 图谱。

    Args:
        query: 符号名查询字符串（支持部分匹配）
        top_k: 返回条数上限
        ns: 命名空间
    """
    if not query:
        return {"status": "error", "reason": "query required"}
    try:
        neo = Neo4jStore()
        results = neo.symbol_lookup(query, top_k, ns=ns)
        return {"status": "ok", "results": results, "count": len(results)}
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def neo4j_dep_subgraph(seed_fqns: List[str], depth: int = 2, ns: str = "") -> Dict[str, Any]:
    """查询指定符号的依赖子图（沿 CALLS 边遍历）。

    Args:
        seed_fqns: 种子符号 fqn 列表
        depth: 图遍历深度，默认 2
        ns: 命名空间
    """
    if not seed_fqns:
        return {"status": "error", "reason": "seed_fqns required"}
    try:
        neo = Neo4jStore()
        sub = neo.dependency_subgraph(seed_fqns, depth=depth, ns=ns)
        return {"status": "ok", **sub}
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


# --------------------------------------------------------------------------- #
# 嵌入服务原语（1 个）
# --------------------------------------------------------------------------- #


def embed_texts(texts: List[str]) -> Dict[str, Any]:
    """文本向量化 — 将文本列表转为嵌入向量列表。

    Args:
        texts: 待向量化的文本列表
    """
    if not texts:
        return {"status": "error", "reason": "texts required"}
    try:
        emb = EmbeddingService()
        vectors = emb.embed(texts)
        return {"status": "ok", "vectors": vectors, "dim": emb.dim, "count": len(vectors)}
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


# --------------------------------------------------------------------------- #
# Redis 缓存原语（3 个）
# --------------------------------------------------------------------------- #


def redis_get_repo_state(repo: str) -> Dict[str, Any]:
    """获取仓库当前索引状态（commit + 各文件 hash）。

    Args:
        repo: 仓库名
    """
    if not repo:
        return {"status": "error", "reason": "repo required"}
    try:
        cache = RedisCache()
        state = cache.get_repo_state(repo)
        return {"status": "ok", "state": state}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def redis_set_repo_state(repo: str, commit: str, file_hashes: Dict[str, str]) -> Dict[str, Any]:
    """保存仓库索引状态（commit + 各文件 hash）。

    Args:
        repo: 仓库名
        commit: 当前 commit SHA
        file_hashes: {file_path: content_hash}
    """
    if not repo or not commit:
        return {"status": "error", "reason": "repo and commit required"}
    try:
        cache = RedisCache()
        cache.set_repo_state(repo, commit, file_hashes)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def redis_fetch_embedding(content_hashes: List[str]) -> Dict[str, Any]:
    """批量获取缓存的嵌入向量（按 content hash 查询）。

    Args:
        content_hashes: 内容 hash 列表
    """
    if not content_hashes:
        return {"status": "error", "reason": "content_hashes required"}
    try:
        cache = RedisCache()
        cached = cache.get_embeddings(content_hashes)
        return {"status": "ok", "embeddings": cached, "hits": len(cached), "misses": len(content_hashes) - len(cached)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


# --------------------------------------------------------------------------- #
# AST 解析原语（1 个）
# --------------------------------------------------------------------------- #


def ast_parse_file(file_path: str, repo: str = "", display_path: str = "") -> Dict[str, Any]:
    """用 tree-sitter 解析单个文件为函数/类/方法粒度代码块。

    Args:
        file_path: 文件绝对路径
        repo: 仓库名（可选）
        display_path: 展示路径（可选，用于 chunk_id 中的路径前缀）
    """
    if not file_path:
        return {"status": "error", "reason": "file_path required"}
    try:
        import os
        if not os.path.isfile(file_path):
            return {"status": "error", "reason": f"file not found: {file_path}"}
        parser = AstParser()
        chunks = parser.parse_file(file_path, repo=repo, display_path=display_path or file_path)
        return {"status": "ok", "chunks": chunks, "count": len(chunks)}
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


# --------------------------------------------------------------------------- #
# 共享服务：hybrid_search（1 个）
# --------------------------------------------------------------------------- #


def hybrid_search(query: str, top_k: int = 10, ns: str = "",
                  enable_graph_expand: bool = True) -> Dict[str, Any]:
    """混合检索共享服务：向量 + 关键词 RRF 融合 + 可选图谱扩展。

    这是 3 个 Skill（code-search / impact-analysis / root-cause-analysis）共用
    的通用检索服务，整合了三库（pgvector + Meilisearch + Neo4j）的能力。

    Args:
        query: 自然语言查询
        top_k: 返回条数，默认 10
        ns: 命名空间
        enable_graph_expand: 是否对融合结果做 Neo4j 图谱扩展
    """
    if not query:
        return {"status": "error", "reason": "query required"}
    try:
        # 阶段一：两路召回 + RRF 融合
        emb = EmbeddingService()
        qv = emb.embed([query])[0]
        pg = PgVectorStore()
        vec = pg.vector_search(qv, top_k * 2, ns=ns)
        meili = MeiliStore()
        kw = meili.keyword_search(query, top_k * 2, ns=ns)
        fused = reciprocal_rank_fusion([vec, kw], k=60)
        results = fused[:top_k]

        # 阶段二：Neo4j 图谱扩展（可选）
        graph_expansion = []
        if enable_graph_expand and results:
            try:
                neo = Neo4jStore()
                seed_ids = [r["chunk_id"] for r in results]
                related = neo.expand_chunks(seed_ids, limit=top_k, ns=ns)
                if related:
                    metas = pg.fetch_by_ids(
                        [r["chunk_id"] for r in related], ns=ns
                    )
                    metas_map = {m["chunk_id"]: m for m in metas}
                    for r in related:
                        meta = metas_map.get(r["chunk_id"])
                        if meta:
                            graph_expansion.append({
                                **meta,
                                "relation": r["relation"],
                                "via": r["via"],
                            })
            except DbUnavailable:
                pass  # 图谱不可用时降级

        return {
            "status": "ok",
            "results": results,
            "graph_expansion": graph_expansion,
            "mode": "hybrid+graph" if enable_graph_expand else "hybrid",
            "candidates": len(fused),
            "ns": ns,
        }
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}
