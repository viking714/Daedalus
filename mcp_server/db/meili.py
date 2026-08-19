"""Meilisearch 仓储（全文/关键词检索）。

支持单命名空间 per repo（对应详细设计 v2.1 §3.1）：
- 索引名为 repo 名（ns），所有 commit 共享同一份索引数据；
- Redis 追踪 file hash，repo_indexer 做增量更新（删除+新增变更文件）；
- 未指定 ns 时退化为默认索引 `code_chunks`。

对外方法：
- ensure_schema(ns)：建索引 + 可搜索字段（幂等）
- upsert(chunk, ns)：写入文档
- keyword_search(query, top_k, ns)：全文检索
- delete_by_paths(paths, ns)：按文件路径删除（增量更新时清理旧数据）
- delete_all(ns)：清空整个索引（reset 时使用）

第三方 SDK meilisearch 懒加载。
"""

import hashlib

from .base import DbUnavailable
from .config import get_config

DEFAULT_INDEX = "code_chunks"


def _index_name(ns: str = "") -> str:
    """按命名空间生成索引名。ns 为空时用默认名。"""
    return ns if ns else DEFAULT_INDEX


def _meili_key(chunk_id: str) -> str:
    """生成 Meilisearch 合法主键。

    Meilisearch 主键仅允许 [a-zA-Z0-9_-]，而 chunk_id 形如
    `docs/conf.py:github_link:69`，含 / : . 等非法字符，直接作主键会导致
    add_documents 失败（Document identifier ... is invalid）。
    这里用 sha1 生成安全主键，原始 chunk_id 保留为普通字段供检索结果返回。
    """
    return hashlib.sha1(chunk_id.encode("utf-8")).hexdigest()


class MeiliStore:
    def __init__(self) -> None:
        self._client = None

    def _connect(self):
        try:
            from meilisearch import Client
        except ImportError as e:
            raise DbUnavailable("meilisearch SDK 未安装：pip install meilisearch") from e
        cfg = get_config()
        try:
            self._client = Client(cfg["meili_url"], cfg["meili_key"])
            # 触发一次请求以验证连通
            self._client.health()
        except Exception as e:
            raise DbUnavailable(f"无法连接 Meilisearch：{e}") from e

    def ping(self) -> bool:
        self._connect()
        self._client.health()
        return True

    def _idx(self, ns: str = ""):
        if self._client is None:
            self._connect()
        return self._client.index(_index_name(ns))

    def ensure_schema(self, ns: str = "") -> None:
        if self._client is None:
            self._connect()
        idx_name = _index_name(ns)
        task = self._client.create_index(idx_name, {"primaryKey": "meili_key"})
        self._client.wait_for_task(task.task_uid)
        task = self._client.index(idx_name).update_searchable_attributes(
            ["content", "symbol", "path"]
        )
        self._client.wait_for_task(task.task_uid)
        task = self._client.index(idx_name).update_filterable_attributes(["path"])
        self._client.wait_for_task(task.task_uid)

    def upsert(self, chunk: dict, ns: str = "") -> None:
        self.batch_upsert([chunk], ns=ns)

    def batch_upsert(self, chunks: list, ns: str = "") -> None:
        """批量写入文档（一次 add_documents 请求，避免逐条往返）。

        主键为 meili_key（chunk_id 的 sha1），原始 chunk_id 保留为普通字段。
        写入后 wait_for_task，确保异步任务真正落盘（否则 add_documents 失败
        会静默丢失，表现为索引 0 文档）。
        """
        if not chunks:
            return
        docs = [
            {
                "meili_key": _meili_key(c["chunk_id"]),
                "chunk_id": c["chunk_id"],
                "ns": ns,
                "repo": c.get("repo"),
                "path": c["path"],
                "symbol": c.get("symbol"),
                "kind": c.get("kind"),
                "content": c["content"],
            }
            for c in chunks
        ]
        task = self._idx(ns).add_documents(docs)
        self._client.wait_for_task(task.task_uid)

    def keyword_search(self, query: str, top_k: int = 10, ns: str = "") -> list:
        hits = self._idx(ns).search(query, {"limit": top_k}).get("hits", [])
        return [{"chunk_id": h["chunk_id"], "score": round(h.get("_score", 0) / 100.0, 4)} for h in hits]

    def has_namespace_data(self, ns: str = "") -> bool:
        """检查命名空间索引下是否已有文档。"""
        stats = self._idx(ns).get_stats()
        if isinstance(stats, dict):
            count = stats.get("numberOfDocuments", stats.get("number_of_documents", 0))
        else:
            count = getattr(stats, "number_of_documents", getattr(stats, "numberOfDocuments", 0))
        return int(count or 0) > 0

    # ------------------------------------------------------------------ #
    # 增量索引：按文件删除
    # ------------------------------------------------------------------ #

    def delete_by_paths(self, paths: list, ns: str = "") -> int:
        """删除指定文件路径的文档（增量更新时清理旧数据）。返回删除数。"""
        if not paths:
            return 0
        # Meilisearch 不支持直接按字段值批量删除，需要先查询再删
        deleted = 0
        idx = self._idx(ns)
        for path in paths:
            # 查询该路径下的所有文档，取主键 meili_key 删除
            hits = idx.search("", {"filter": f'path = "{path}"', "limit": 1000}).get("hits", [])
            if hits:
                ids = [h["meili_key"] for h in hits]
                idx.delete_documents(ids)
                deleted += len(ids)
        return deleted

    def delete_all(self, ns: str = "") -> int:
        """删除整个索引的所有文档（reset 时使用）。返回任务 ID。"""
        task = self._idx(ns).delete_all_documents()
        return task.task_uid
