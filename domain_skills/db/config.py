"""连接配置。

来源（按优先级）：
1. 环境变量；
2. 若设置了 AGENTTEAMS_ENV_FILE，则先加载该文件（期望为 deploy/db/.env.db）。

访问方式：经 SSH 隧道，所有库在本地回环 127.0.0.1（见 docker-compose.db.yml 端口绑定）。
变量名与 .env.db 保持一致（POSTGRES_DB / NEO4J_PASSWORD / MEILI_MASTER_KEY / REDIS_PASSWORD）。
"""

import os

# .env.db 中提供的变量名
_ENV_DB_NAMES = (
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "NEO4J_PASSWORD",
    "MEILI_MASTER_KEY",
    "REDIS_PASSWORD",
)


def _load_env_file(path: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())
    except FileNotFoundError:
        pass


def load_config() -> dict:
    env_file = os.getenv("AGENTTEAMS_ENV_FILE")
    if env_file:
        _load_env_file(env_file)
    return {
        # PostgreSQL + pgvector
        "pghost": os.getenv("PGHOST", "127.0.0.1"),
        "pgport": int(os.getenv("PGPORT", "5432")),
        "pgdb": os.getenv("POSTGRES_DB", "agentteams"),
        "pguser": os.getenv("POSTGRES_USER", "agent"),
        "pgpassword": os.getenv("POSTGRES_PASSWORD", "changeme"),
        # Neo4j
        "neo4j_uri": os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"),
        "neo4j_user": os.getenv("NEO4J_USER", "neo4j"),
        "neo4j_password": os.getenv("NEO4J_PASSWORD", "changeme"),
        # Meilisearch
        "meili_url": os.getenv("MEILI_URL", "http://127.0.0.1:7700"),
        "meili_key": os.getenv("MEILI_MASTER_KEY", "changeme-master-key"),
        # Redis
        "redis_url": os.getenv("REDIS_URL", "redis://127.0.0.1:6379"),
        "redis_password": os.getenv("REDIS_PASSWORD", ""),
        # Embedding
        "embed_dim": int(os.getenv("EMBED_DIM", "384")),
        "embed_backend": os.getenv("EMBED_BACKEND", "fastembed"),
        "embed_model": os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5"),
        "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
        "openai_base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    }


_CONFIG = None


def get_config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG
