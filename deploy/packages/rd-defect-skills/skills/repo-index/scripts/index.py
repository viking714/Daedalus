"""repo-index 核心脚本 — 增量代码索引。

从 composed_tools.py 提取，直接调用 mcp_server/db/embed/code 模块（不经过 MCP 层），
性能更优且可独立测试。

流程：分块 → 嵌入 → 三库写入（pgvector + Neo4j + Meilisearch）
"""

import os
import sys
import logging

logger = logging.getLogger("repo_index")

# 确保可 import mcp_server 包
_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    from mcp_server.db.base import DbUnavailable, content_hash
    from mcp_server.db.pgvector import PgVectorStore
    from mcp_server.db.neo4jgraph import Neo4jStore
    from mcp_server.db.meili import MeiliStore
    from mcp_server.db.redis_cache import RedisCache
    from mcp_server.db.schema import ensure_all
    from mcp_server.embed.embeddings import EmbeddingService
    from mcp_server.code.ast_parser import AstParser
except ImportError:
    # fallback: mcp_tools dir 已在 sys.path 中
    from db.base import DbUnavailable, content_hash
    from db.pgvector import PgVectorStore


def index_repo(repo_path: str, commit: str = "", full_reindex: bool = False) -> dict:
    """增量代码索引主入口。

    Args:
        repo_path: 仓库绝对路径
        commit: commit SHA
        full_reindex: 是否强制全量重建

    Returns:
        索引统计 dict
    """
    if not repo_path:
        return {"status": "error", "reason": "repo_path required"}

    try:
        cache = RedisCache()
        repo_name = os.path.basename(os.path.abspath(repo_path))
        ns = repo_name
        pg = PgVectorStore()
        neo = Neo4jStore()
        meili = MeiliStore()
        ensure_all(ns=ns)

        # 获取当前索引状态
        old_state = cache.get_repo_state(repo_name)
        old_file_hashes = old_state.get("file_hashes", {}) if not full_reindex else {}
        old_commit = old_state.get("commit", "") if not full_reindex else ""

        # 检查三库实际状态
        backend_state = {
            "pgvector": pg.has_namespace_data(ns=ns),
            "neo4j": neo.has_namespace_data(ns=ns),
            "meili": meili.has_namespace_data(ns=ns),
        }
        backend_ready = all(backend_state.values())

        if old_commit == commit and old_file_hashes and backend_ready:
            return {
                "status": "already_indexed",
                "repo": repo_name,
                "commit": commit[:8] or "HEAD",
                "ns": ns,
            }

        # 状态不一致时执行全量重建
        backend_has_any = any(backend_state.values())
        if (old_file_hashes and not backend_ready) or (not old_file_hashes and backend_has_any):
            pg.delete_all(ns=ns)
            neo.delete_all(ns=ns)
            meili.delete_all(ns=ns)
            cache.clear_repo_state(repo_name)
            old_file_hashes = {}
            old_commit = ""

        # 扫描文件
        repo_path = os.path.abspath(repo_path)
        skip_dirs = {".git", "node_modules", "__pycache__", "venv", ".venv", "build", "dist"}
        ext_lang = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".rb", ".rs", ".c", ".h", ".cpp", ".cc"}

        new_file_hashes = {}
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in ext_lang:
                    continue
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, repo_path)
                try:
                    with open(full, "rb") as f:
                        data = f.read()
                    new_file_hashes[rel] = content_hash(data.decode("utf-8", "replace"))
                except Exception:
                    continue

        if not new_file_hashes:
            return {"status": "no_source", "repo": repo_name}

        # Diff
        old_paths = set(old_file_hashes.keys())
        new_paths = set(new_file_hashes.keys())
        removed_paths = old_paths - new_paths
        added_paths = new_paths - old_paths
        changed_paths = {p for p in (old_paths & new_paths) if old_file_hashes[p] != new_file_hashes[p]}

        needs_update = bool(removed_paths or added_paths or changed_paths)
        if not needs_update and old_commit:
            cache.set_repo_state(repo_name, commit, new_file_hashes)
            cache.mark_indexed(repo_name, commit)
            return {"status": "already_indexed", "repo": repo_name, "ns": ns, "mode": "incremental"}

        # 删除旧数据
        delete_paths = list(removed_paths | changed_paths)
        if delete_paths:
            pg.delete_by_paths(delete_paths, ns=ns)
            neo.delete_by_paths(delete_paths, ns=ns)
            meili.delete_by_paths(delete_paths, ns=ns)

        # 解析新/变更文件
        parser = AstParser()
        new_chunks = []
        for rel in sorted(added_paths | changed_paths):
            full = os.path.join(repo_path, rel)
            try:
                chunks = parser.parse_file(full, repo=repo_name, display_path=rel)
                new_chunks.extend(chunks)
            except Exception:
                continue

        if not new_chunks:
            cache.set_repo_state(repo_name, commit, new_file_hashes)
            return {"status": "indexed", "repo": repo_name, "ns": ns, "new_chunks": 0}

        # 嵌入 + 写入
        emb = EmbeddingService()
        vecs = [None] * len(new_chunks)
        chunk_hashes = [content_hash(c["content"]) for c in new_chunks]
        cached_embeddings = cache.get_embeddings(chunk_hashes)

        cache_hits = 0
        uncached_indices = []
        uncached_texts = []
        for idx, (c, ch) in enumerate(zip(new_chunks, chunk_hashes)):
            cached = cached_embeddings.get(ch)
            if cached is not None:
                vecs[idx] = cached
                cache_hits += 1
            else:
                uncached_indices.append(idx)
                uncached_texts.append(c["content"])

        if uncached_texts:
            uncached_vecs = emb.embed(uncached_texts)
            cache_updates = {}
            for idx, v in zip(uncached_indices, uncached_vecs):
                cache_updates[chunk_hashes[idx]] = v
                vecs[idx] = v
            cache.put_embeddings(cache_updates)

        for c in new_chunks:
            c["ns"] = ns
        pg.batch_upsert_chunks(new_chunks, vecs, ns=ns)
        neo.batch_upsert_symbols(new_chunks, ns=ns)
        meili.batch_upsert(new_chunks, ns=ns)

        # 重建关系边
        call_rows, method_rows, import_rows = [], [], []
        seen_files = set()
        for c in new_chunks:
            if c.get("calls") and c.get("kind") in ("function", "method"):
                call_rows.append((c["chunk_id"], c["calls"], c.get("repo"), ns))
            if c.get("kind") == "method" and c.get("parent_class"):
                method_rows.append((c["chunk_id"], c["parent_class"], c["path"], c.get("repo"), ns))
            if c["path"] not in seen_files:
                seen_files.add(c["path"])
                import_rows.append((c["path"], c.get("file_imports") or [], ns))
        try:
            neo.batch_link_calls(call_rows)
        except Exception:
            pass
        try:
            neo.batch_link_methods(method_rows)
        except Exception:
            pass
        try:
            neo.batch_link_imports(import_rows)
        except Exception:
            pass

        cache.set_repo_state(repo_name, commit, new_file_hashes)
        cache.mark_indexed(repo_name, commit)

        return {
            "status": "indexed",
            "repo": repo_name,
            "commit": commit[:8] or "HEAD",
            "ns": ns,
            "mode": "incremental",
            "removed": len(removed_paths),
            "added": len(added_paths),
            "changed": len(changed_paths),
            "new_chunks": len(new_chunks),
            "cache_hits": cache_hits,
        }
    except DbUnavailable as e:
        return {"status": "unavailable", "reason": str(e)}
    except Exception as e:
        return {"status": "error", "reason": str(e)}
