"""Redis 仓储（缓存 / 锁 / 索引新鲜度标记 / 仓库状态追踪）。

用于缓存嵌入向量、索引标记和仓库状态（对应详细设计 v2.1 §3.1）。
关键设计：
- 单命名空间 per repo：所有 commit 共享同一 ns（= repo_name），数据库只保留最新状态。
- Redis `repo_state:{repo}` 存储 {commit, file_hashes}，repo_indexer 据此做增量 diff。
- 索引标记 key 为 `indexed:{repo}:{commit[:8]}`，标记某 commit 是否已索引过。
- 嵌入缓存 key 使用内容 hash（`emb:{content_hash}`），天然 commit-safe，
  且跨 commit 未变更的文件会命中同一缓存，等效于增量更新优化。
- 第三方客户端 redis 懒加载。
"""

import hashlib
import logging

from .base import DbUnavailable
from .config import get_config

logger = logging.getLogger("redis_cache")


class RedisCache:
    def __init__(self) -> None:
        self._client = None
        # 降级标记：一旦确认 Redis 不可用，后续调用直接短路，避免每次重连超时
        self._degraded = False

    def _connect(self):
        try:
            import redis
        except ImportError as e:
            raise DbUnavailable("redis 客户端未安装：pip install redis") from e
        cfg = get_config()
        try:
            self._client = redis.Redis.from_url(
                cfg["redis_url"],
                password=cfg["redis_password"] or None,
                socket_connect_timeout=5,
                socket_timeout=5,
                health_check_interval=30,
            )
            self._client.ping()
        except Exception as e:
            raise DbUnavailable(f"无法连接 Redis：{e}") from e

    def _ensure_client(self):
        if self._client is None:
            self._connect()
        return self._client

    def _degrade(self, action: str, error: Exception):
        logger.warning("Redis %s 失败，降级跳过缓存：%s", action, error)
        self._degraded = True

    def ping(self) -> bool:
        self._connect()
        self._client.ping()
        return True

    # ------------------------------------------------------------------ #
    # 索引新鲜度标记（commit-aware）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _index_key(repo: str, commit: str = "") -> str:
        """生成 commit 感知的索引标记 key。

        对齐设计文档 §7.6 的 collection 命名 `{repo_short}_{commit[:8]}`。
        """
        suffix = commit[:8] if commit else "HEAD"
        return f"indexed:{repo}:{suffix}"

    def is_indexed(self, repo: str, commit: str = "") -> bool:
        """检查指定 repo+commit 是否已建索引。

        commit 为空时退化为 repo 级别检查（兼容旧调用）。
        """
        if self._degraded:
            return False
        try:
            return bool(self._ensure_client().get(self._index_key(repo, commit)))
        except Exception as e:
            self._degrade("读取 indexed 标记", e)
            return False

    def mark_indexed(self, repo: str, commit: str = "", ttl: int = 86400) -> None:
        """标记指定 repo+commit 已完成索引。

        ttl=0 表示不设过期（适合批量评测场景避免重复建索引）。
        """
        if self._degraded:
            return
        try:
            client = self._ensure_client()
            key = self._index_key(repo, commit)
            if ttl > 0:
                client.setex(key, ttl, "1")
            else:
                client.set(key, "1")
        except Exception as e:
            self._degrade("写入 indexed 标记", e)

    # ------------------------------------------------------------------ #
    # 嵌入缓存（content-hash aware）
    # ------------------------------------------------------------------ #

    @staticmethod
    def content_hash(content: str) -> str:
        """计算代码块内容的 SHA256 hash，用作嵌入缓存 key。
        与 base.content_hash() 保持一致（全 64 位 hex）。
        """
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def get_embedding(self, key: str):
        """按 key 获取缓存的嵌入向量。key 建议使用 content_hash()。"""
        if self._degraded:
            return None
        try:
            raw = self._ensure_client().get(f"emb:{key}")
            return None if raw is None else [float(x) for x in raw.decode().split(",")]
        except Exception as e:
            self._degrade("读取单条 embedding 缓存", e)
            return None

    def get_embeddings(self, keys: list) -> dict:
        """批量获取嵌入缓存，返回 {content_hash: embedding}。"""
        if self._degraded or not keys:
            return {}
        try:
            raws = self._ensure_client().mget([f"emb:{key}" for key in keys])
            out = {}
            for key, raw in zip(keys, raws):
                if raw is not None:
                    out[key] = [float(x) for x in raw.decode().split(",")]
            return out
        except Exception as e:
            self._degrade("批量读取 embedding 缓存", e)
            return {}

    def put_embedding(self, key: str, vec, ttl: int = 86400) -> None:
        """按 key 缓存嵌入向量。key 建议使用 content_hash()。"""
        if self._degraded:
            return
        try:
            self._ensure_client().setex(f"emb:{key}", ttl, ",".join(str(x) for x in vec))
        except Exception as e:
            self._degrade("写入单条 embedding 缓存", e)

    def put_embeddings(self, items: dict, ttl: int = 86400) -> None:
        """批量写入嵌入缓存。items 形如 {content_hash: embedding}。"""
        if self._degraded or not items:
            return
        try:
            pipe = self._ensure_client().pipeline(transaction=False)
            for key, vec in items.items():
                pipe.setex(f"emb:{key}", ttl, ",".join(str(x) for x in vec))
            pipe.execute()
        except Exception as e:
            self._degrade("批量写入 embedding 缓存", e)

    # ------------------------------------------------------------------ #
    # 增量索引：仓库状态追踪（file-hash aware）
    # ------------------------------------------------------------------ #

    def get_repo_state(self, repo: str) -> dict:
        """获取仓库当前索引状态（current_commit + 各文件 hash）。

        返回 {"commit": str, "file_hashes": {path: hash}}，
        未索引过则返回空 dict。
        """
        if self._degraded:
            return {}
        try:
            raw = self._ensure_client().get(f"repo_state:{repo}")
            if raw is None:
                return {}
            import json
            return json.loads(raw.decode())
        except Exception as e:
            self._degrade("读取 repo_state", e)
            return {}

    def set_repo_state(self, repo: str, commit: str, file_hashes: dict) -> None:
        """保存仓库索引状态（当前 commit + 各文件 hash）。

        file_hashes: {file_path: content_hash}，用于增量 diff。
        """
        if self._degraded:
            return
        try:
            import json
            self._ensure_client().set(f"repo_state:{repo}", json.dumps({
                "commit": commit,
                "file_hashes": file_hashes,
            }))
        except Exception as e:
            self._degrade("写入 repo_state", e)

    def clear_repo_state(self, repo: str) -> None:
        """清除仓库索引状态（reset 时使用）。"""
        if self._degraded:
            return
        try:
            self._ensure_client().delete(f"repo_state:{repo}")
        except Exception as e:
            self._degrade("清理 repo_state", e)
