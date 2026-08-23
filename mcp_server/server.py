"""MCP Server for code-intelligence domain skills（mcp_server/server.py）。

三层架构（对齐方案设计 v2.2 §3.1）：
  Worker (AgentTeams)
    → 调用 MCP Tools（通过 mcporter / Higress AI Gateway）
    → MCP 层（本文件）：暴露两类工具
        ① 数据访问原语（14 个，来自 mcp_primitives.py）— 细粒度，"数据怎么取"
        ② 组合工具（来自 composed_tools.py）— 编排原语，完成工作流
    → 数据层（pgvector / Neo4j / Meilisearch / Redis / embedding / AST）

Usage:
    python mcp_server/server.py                # default port 8090
    python mcp_server/server.py --port 9090    # custom port

Architecture:
    Worker (inside container)
      → mcporter (MCP client)
      → Higress AI Gateway (credential proxy)
      → this MCP server (Streamable HTTP, port 8090)
      → mcp_primitives.py (14 data access primitives)
      → composed_tools.py (composed tool workflows)
      → PostgreSQL / Neo4j / Meilisearch / Redis (via SSH tunnel)
"""

import functools
import json
import logging
import os
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mcp_server")

# --------------------------------------------------------------------------- #
# Import resolution: add project root to sys.path for package imports
# --------------------------------------------------------------------------- #
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    logger.error(
        "MCP SDK not installed. Run: pip install 'mcp[cli]>=1.0'"
    )
    sys.exit(1)

from mcp_server.telemetry import init_telemetry, instrument

# ---- 数据访问原语层（14 个细粒度工具）----
from mcp_server.mcp_primitives import (
    # pgvector 原语
    pgvector_search,
    pgvector_fetch,
    pgvector_upsert_chunk,
    pgvector_delete,
    # Meilisearch 原语
    meili_keyword_search,
    # Neo4j 原语
    neo4j_expand_chunks,
    neo4j_impact_stats,
    neo4j_symbol_lookup,
    neo4j_dep_subgraph,
    # 嵌入原语
    embed_texts,
    # Redis 原语
    redis_get_repo_state,
    redis_set_repo_state,
    redis_fetch_embedding,
    # AST 原语
    ast_parse_file,
    # 共享服务
    hybrid_search,
)

# ---- 组合工具层（编排原语的高层工作流）—— 来自 composed_tools.py ----
from mcp_server.composed_tools import list_skill_defs, get_skill, is_registered, _REGISTRY

# --------------------------------------------------------------------------- #
# MCP Server instance
# --------------------------------------------------------------------------- #

mcp = FastMCP(
    "code-intelligence",
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", os.getenv("PORT", "8090"))),
)

# --------------------------------------------------------------------------- #
# 工具注册：使用统一的注册函数
# --------------------------------------------------------------------------- #

# ---- 数据访问原语定义 ----
_PRIMITIVES = [
    # pgvector（4 个）
    ("pgvector_search", "向量语义检索：查询 Top-K 最相似代码块。", pgvector_search),
    ("pgvector_fetch", "按 chunk_id 批量取代码块元信息。", pgvector_fetch),
    ("pgvector_upsert_chunk", "写入/更新单个代码块到 pgvector。", pgvector_upsert_chunk),
    ("pgvector_delete", "按文件路径删除 pgvector 代码块。", pgvector_delete),
    # Meilisearch（1 个）
    ("meili_keyword_search", "Meilisearch 全文关键词检索。", meili_keyword_search),
    # Neo4j（4 个）
    ("neo4j_expand_chunks", "沿 CALLS/IMPORTS 关系扩展图谱上下文。", neo4j_expand_chunks),
    ("neo4j_impact_stats", "统计影响面：调用方数 + 跨文件导入数。", neo4j_impact_stats),
    ("neo4j_symbol_lookup", "按符号名模糊查询 Neo4j 图谱。", neo4j_symbol_lookup),
    ("neo4j_dep_subgraph", "查询指定符号的依赖子图。", neo4j_dep_subgraph),
    # 嵌入（1 个）
    ("embed_texts", "文本向量化 — 返回嵌入向量列表。", embed_texts),
    # Redis（3 个）
    ("redis_get_repo_state", "获取仓库索引状态（commit + file hashes）。", redis_get_repo_state),
    ("redis_set_repo_state", "保存仓库索引状态。", redis_set_repo_state),
    ("redis_fetch_embedding", "按 content hash 批量取缓存的嵌入向量。", redis_fetch_embedding),
    # AST（1 个）
    ("ast_parse_file", "tree-sitter 解析文件为代码块。", ast_parse_file),
    # 共享服务（1 个）
    ("hybrid_search", "向量+关键词 RRF 融合 + 图谱扩展（通用检索服务）。", hybrid_search),
]


def _register_primitive_tool(name: str, description: str, handler_fn):
    """将原语函数注册为 MCP tool，并自动包一层 OTel Span。

    关键：用 functools.wraps 继承 handler_fn 的真实签名，使 FastMCP 生成
    正确的 inputSchema（如 query_text / top_k / ns），而不是把参数错误地
    包进一个名为 `kwargs` 的字段（这会导致 worker 端调用时参数无法透传）。
    """
    @instrument(name=f"mcp.primitive.{name}")
    @functools.wraps(handler_fn)
    def tool_handler(*args: Any, **kwargs: Any) -> str:
        try:
            result = handler_fn(*args, **kwargs)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            logger.exception("MCP primitive %s failed", name)
            return json.dumps({"status": "error", "reason": str(e)})

    # functools.wraps 会继承 handler_fn 的返回注解（如 -> Dict[str, Any]），
    # 这会让 FastMCP 据此生成 outputSchema 并把返回值当作 dict 校验，
    # 与 tool_handler 实际返回的 JSON 字符串冲突。清除返回注解，保留参数签名。
    tool_handler.__annotations__.pop("return", None)

    tool_handler.__name__ = name
    tool_handler.__doc__ = description
    mcp.tool(name=name, description=description)(tool_handler)
    logger.debug("Registered MCP primitive: %s", name)


def _register_skill_tool(skill_name: str):
    """将 composed_tools.py 中的组合工具注册为 MCP tool，并自动包一层 OTel Span。

    组合工具的 handler 统一签名为 `payload: dict`，因此这里用 functools.wraps
    继承 handler 的真实签名，使 FastMCP 生成 `{payload: {...}}` 的 inputSchema，
    而非把参数错误地包进 `kwargs` 字段（双重嵌套会导致 worker 端参数无法透传）。
    """
    skill_def = get_skill(skill_name)
    if skill_def is None:
        return

    handler_fn = skill_def.handler

    @instrument(name=f"mcp.skill.{skill_name}")
    @functools.wraps(handler_fn)
    def tool_handler(*args: Any, **kwargs: Any) -> str:
        try:
            # 兼容两种调用形态：位置参数 dict，或具名 payload
            if args and isinstance(args[0], dict) and not kwargs:
                payload = args[0]
            elif kwargs.get("payload") is not None and len(kwargs) == 1:
                payload = kwargs["payload"]
            else:
                payload = kwargs
            result = handler_fn(payload)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            logger.exception("MCP skill tool %s failed", skill_name)
            return json.dumps({"status": "error", "reason": str(e)})

    # 清除 handler 的返回注解（-> dict），避免 FastMCP 生成 outputSchema 校验
    tool_handler.__annotations__.pop("return", None)

    tool_handler.__name__ = skill_name
    tool_handler.__doc__ = skill_def.description
    desc = f"[{skill_def.owner_role}] {skill_def.description}"
    mcp.tool(name=skill_name, description=desc)(tool_handler)


# ---- 注册全部原语 ----
for _name, _desc, _fn in _PRIMITIVES:
    _register_primitive_tool(_name, _desc, _fn)

# ---- 注册全部组合工具（来自 composed_tools.py） ----
for _skill_def in list_skill_defs():
    _register_skill_tool(_skill_def.name)

_primitive_count = len(_PRIMITIVES)
_skill_count = len(_REGISTRY)
logger.info(
    "Registered %d MCP primitives + %d composed Skill tools = %d total",
    _primitive_count, _skill_count, _primitive_count + _skill_count,
)

# --------------------------------------------------------------------------- #
# Health check resource
# --------------------------------------------------------------------------- #


@mcp.resource("health://status")
def health_status() -> str:
    """Return service health and registered tool counts."""
    return json.dumps({
        "status": "ok",
        "mcp_primitives": _primitive_count,
        "skill_tools": _skill_count,
        "total_tools": _primitive_count + _skill_count,
        "primitive_names": [p[0] for p in _PRIMITIVES],
        "skill_names": sorted(_REGISTRY.keys()),
    })


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    # MCP 层使用独立 service name，与 Worker 层（daedalus-agent-teams）分离，
    # 避免 AgentLoop 调用链里 MCP 工具调用与 Worker 模型调用混在一起。
    init_telemetry(os.getenv("MCP_OTEL_SERVICE_NAME", "daedalus-mcp-server"))
    port = int(os.getenv("MCP_PORT", os.getenv("PORT", "8090")))
    host = os.getenv("MCP_HOST", "0.0.0.0")
    logger.info(
        "code-intelligence MCP server starting on %s:%d (%d primitives + %d skills)",
        host, port, _primitive_count, _skill_count,
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
