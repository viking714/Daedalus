"""数据访问层共享基础类型与命名空间工具。

索引命名空间（ns）约定（对应详细设计 v2.1 §3.1）：
- 单命名空间 per repo：ns = repo_name（不再拼接 commit hash）；
- Redis 追踪 file hash 做增量更新，数据库只保留最新状态；
- 向量库 / 全文库 / 图库三端用同一个 ns 做数据隔离，
  Redis 用 repo_state:{repo} 存当前索引状态、indexed:{repo}:{commit} 做簿记。
- make_ns 保留以兼容外部调用（SWE-bench 适配器等），但 repo_indexer 内部使用单 ns。
"""

import hashlib


class DbUnavailable(Exception):
    """数据库/依赖当前不可用（未安装客户端、连接失败、schema 未初始化等）。

    业务层应捕获此异常并返回结构化降级结果，而非让整个请求 500。
    """

    pass


def make_ns(repo: str, commit: str = "") -> str:
    """生成索引命名空间。SWE-bench 等多 commit 场景必须带 commit，防止跨版本串数据。"""
    repo = (repo or "").strip()
    commit = (commit or "").strip()
    if commit:
        return f"{repo}_{commit[:8]}"
    return repo


def content_hash(text: str) -> str:
    """代码块内容哈希：作为嵌入缓存 key，天然 commit-safe（内容未变则跨 commit 命中）。"""
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()
