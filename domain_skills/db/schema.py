"""一键初始化所有存储 schema（幂等）。支持 commit-aware 命名空间。"""


def ensure_all(ns: str = "") -> None:
    from .pgvector import PgVectorStore
    from .neo4jgraph import Neo4jStore
    from .meili import MeiliStore

    PgVectorStore().ensure_schema()
    Neo4jStore().ensure_schema()
    MeiliStore().ensure_schema(ns=ns)
