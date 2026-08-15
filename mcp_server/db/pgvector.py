"""PostgreSQL + pgvector 仓储（向量检索）。

支持单命名空间 per repo（对应详细设计 v2.1 §3.1）：
- 表的 `ns` 列值为 repo 名，所有 commit 共享同一份数据；
- Redis 追踪 file hash，repo_indexer 做增量更新（删除+新增变更文件）；
- 未指定 ns 时为空串（生产场景跟随 HEAD）。

对外方法：
- ensure_schema()：建扩展/表/索引（幂等）
- upsert_chunk(chunk, embedding, ns)：写入代码块 + 向量
- vector_search(query_vec, top_k, ns)：余弦相似度检索（按 ns 过滤）
- fetch_by_ids(chunk_ids, ns)：批量取块元信息
- delete_by_paths(paths, ns)：按文件路径删除（增量更新时清理旧数据）
- delete_all(ns)：清空整个命名空间（reset 时使用）

第三方客户端 psycopg2 懒加载；不可用时抛 DbUnavailable。
"""

import logging

from .base import DbUnavailable
from .config import get_config

logger = logging.getLogger("pgvector")


class PgVectorStore:
    def __init__(self) -> None:
        self._conn = None

    def _connect(self):
        try:
            import psycopg2
        except ImportError as e:  # 未安装客户端
            raise DbUnavailable("psycopg2 未安装：pip install psycopg2-binary") from e
        cfg = get_config()
        try:
            self._conn = psycopg2.connect(
                host=cfg["pghost"],
                port=cfg["pgport"],
                dbname=cfg["pgdb"],
                user=cfg["pguser"],
                password=cfg["pgpassword"],
                connect_timeout=5,
            )
        except Exception as e:  # 连接失败（隧道未开 / 库未起）
            raise DbUnavailable(f"无法连接 PostgreSQL：{e}") from e

    def ping(self) -> bool:
        self._connect()
        cur = self._conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        return True

    def _cur(self):
        if self._conn is None or self._conn.closed:
            self._connect()
        return self._conn.cursor()

    def ensure_schema(self) -> None:
        cur = self._cur()
        dim = get_config()["embed_dim"]
        # 表与扩展必须先于索引提交；索引为可选加速，失败不能回滚掉表。
        # ns 列用于 commit-aware 命名空间隔离（对应设计文档 §7.6）。
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS code_chunks (
                    id      TEXT PRIMARY KEY,
                    ns      TEXT NOT NULL DEFAULT '',
                    repo    TEXT,
                    path    TEXT,
                    symbol  TEXT,
                    kind    TEXT,
                    content TEXT,
                    embedding vector({dim})
                );
                """
            )
            # 兼容旧表结构：早期版本没有 ns 列，这里就地补齐，避免重复部署后索引直接失败。
            cur.execute(
                "ALTER TABLE code_chunks ADD COLUMN IF NOT EXISTS ns TEXT NOT NULL DEFAULT '';"
            )
            # 为 ns 列建 B-tree 索引，加速按命名空间过滤
            cur.execute(
                "CREATE INDEX IF NOT EXISTS code_chunks_ns_idx ON code_chunks (ns);"
            )
            self._conn.commit()
        except Exception as e:
            self._conn.rollback()
            raise DbUnavailable(f"初始化 pgvector schema 失败：{e}") from e
        # HNSW 索引为可选加速：旧版 pgvector 仅支持 ivfflat/无索引时降级跳过，
        # 小数据量下顺序扫描（<=*> 余弦距离）无感知差异。
        try:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS code_chunks_embedding_idx "
                "ON code_chunks USING hnsw (embedding vector_cosine_ops);"
            )
            self._conn.commit()
        except Exception as e:  # noqa: BLE001
            self._conn.rollback()
            logger.warning("pgvector HNSW 索引不可用（%s），降级为无索引顺序扫描。", e)

    def upsert_chunk(self, chunk: dict, embedding, ns: str = "") -> None:
        cur = self._cur()
        cur.execute(
            """
            INSERT INTO code_chunks (id, ns, repo, path, symbol, kind, content, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                ns=EXCLUDED.ns, repo=EXCLUDED.repo, path=EXCLUDED.path, symbol=EXCLUDED.symbol,
                kind=EXCLUDED.kind, content=EXCLUDED.content, embedding=EXCLUDED.embedding;
            """,
            (
                chunk["chunk_id"],
                ns,
                chunk.get("repo"),
                chunk["path"],
                chunk.get("symbol"),
                chunk.get("kind"),
                chunk["content"],
                str(list(embedding)),
            ),
        )
        self._conn.commit()

    def batch_upsert_chunks(self, chunks: list, embeddings: list, ns: str = "") -> None:
        """批量写入代码块 + 向量（一次事务，避免逐条 commit 的 SSH 隧道往返开销）。"""
        if not chunks:
            return
        cur = self._cur()
        rows = []
        for chunk, embedding in zip(chunks, embeddings):
            rows.append(
                (
                    chunk["chunk_id"],
                    ns,
                    chunk.get("repo"),
                    chunk["path"],
                    chunk.get("symbol"),
                    chunk.get("kind"),
                    chunk["content"],
                    str(list(embedding)),
                )
            )
        try:
            cur.executemany(
                """
                INSERT INTO code_chunks (id, ns, repo, path, symbol, kind, content, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    ns=EXCLUDED.ns, repo=EXCLUDED.repo, path=EXCLUDED.path, symbol=EXCLUDED.symbol,
                    kind=EXCLUDED.kind, content=EXCLUDED.content, embedding=EXCLUDED.embedding;
                """,
                rows,
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def fetch_by_ids(self, chunk_ids: list, ns: str = "") -> list:
        """按主键批量取块元信息（供图谱扩充结果补齐内容）。按 ns 过滤。"""
        if not chunk_ids:
            return []
        cur = self._cur()
        if ns:
            cur.execute(
                "SELECT id, path, symbol, kind, content FROM code_chunks WHERE id = ANY(%s) AND ns = %s;",
                (list(chunk_ids), ns),
            )
        else:
            cur.execute(
                "SELECT id, path, symbol, kind, content FROM code_chunks WHERE id = ANY(%s);",
                (list(chunk_ids),),
            )
        return [
            {"chunk_id": r[0], "path": r[1], "symbol": r[2], "kind": r[3], "content": r[4]}
            for r in cur.fetchall()
        ]

    def vector_search(self, query_vec, top_k: int = 10, ns: str = "") -> list:
        cur = self._cur()
        if ns:
            cur.execute(
                """
                SELECT id, path, symbol, content,
                       1 - (embedding <=> %s::vector) AS score
                FROM code_chunks
                WHERE ns = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s;
                """,
                (str(list(query_vec)), ns, str(list(query_vec)), top_k),
            )
        else:
            cur.execute(
                """
                SELECT id, path, symbol, content,
                       1 - (embedding <=> %s::vector) AS score
                FROM code_chunks
                ORDER BY embedding <=> %s::vector
                LIMIT %s;
                """,
                (str(list(query_vec)), str(list(query_vec)), top_k),
            )
        return [
            {"chunk_id": r[0], "path": r[1], "symbol": r[2], "content": r[3], "score": round(r[4], 4)}
            for r in cur.fetchall()
        ]

    def has_namespace_data(self, ns: str = "") -> bool:
        """检查命名空间下是否已有代码块。"""
        cur = self._cur()
        if ns:
            cur.execute("SELECT 1 FROM code_chunks WHERE ns = %s LIMIT 1;", (ns,))
        else:
            cur.execute("SELECT 1 FROM code_chunks LIMIT 1;")
        return cur.fetchone() is not None

    # ------------------------------------------------------------------ #
    # 增量索引：按文件删除
    # ------------------------------------------------------------------ #

    def delete_by_paths(self, paths: list, ns: str = "") -> int:
        """删除指定文件路径的代码块（增量更新时清理旧数据）。返回删除行数。"""
        if not paths:
            return 0
        cur = self._cur()
        if ns:
            cur.execute(
                "DELETE FROM code_chunks WHERE ns = %s AND path = ANY(%s);",
                (ns, list(paths)),
            )
        else:
            cur.execute(
                "DELETE FROM code_chunks WHERE path = ANY(%s);",
                (list(paths),),
            )
        deleted = cur.rowcount
        self._conn.commit()
        return deleted

    def delete_all(self, ns: str = "") -> int:
        """删除整个命名空间的代码块（reset 时使用）。返回删除行数。"""
        cur = self._cur()
        if ns:
            cur.execute("DELETE FROM code_chunks WHERE ns = %s;", (ns,))
        else:
            cur.execute("DELETE FROM code_chunks;")
        deleted = cur.rowcount
        self._conn.commit()
        return deleted
