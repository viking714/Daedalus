"""MCP Server for domain skills.

Exposes all registered skills as MCP tools via the Streamable HTTP transport.
Workers connect through AgentTeams' Higress AI Gateway using mcporter,
so no worker_bridge adapter is needed.

Usage:
    python domain_skills/mcp_server.py                     # default port 8090
    python domain_skills/mcp_server.py --port 9090         # custom port

Architecture:
    Worker (inside container)
      → mcporter (MCP client)
      → Higress AI Gateway (credential proxy)
      → this MCP server (Streamable HTTP, port 8090)
      → skills.py handlers (business logic)
      → PostgreSQL / Neo4j / Meilisearch / Redis (via SSH tunnel)
"""

import json
import logging
import os
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("domain_skills.mcp")

# --------------------------------------------------------------------------- #
# Lazy import: support both `python mcp_server.py` and `python -m` invocation
# --------------------------------------------------------------------------- #
sys.path.insert(0, os.path.dirname(__file__))

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    logger.error(
        "MCP SDK not installed. Run: pip install 'mcp[cli]>=1.0'"
    )
    sys.exit(1)

from skills import list_skill_defs, get_skill, is_registered, _REGISTRY

# --------------------------------------------------------------------------- #
# MCP Server instance
# --------------------------------------------------------------------------- #

mcp = FastMCP(
    "domain-skills",
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", os.getenv("PORT", "8090"))),
)


# --------------------------------------------------------------------------- #
# Dynamic tool registration
#
# Each skill in the registry becomes an MCP tool. The tool's input schema is
# derived from the handler's expected payload keys (documented in description).
# Workers call tools via mcporter, which handles JSON-RPC framing.
# --------------------------------------------------------------------------- #


def _make_tool_handler(skill_name: str):
    """Create a tool handler function for the given skill.

    The MCP SDK requires each tool to be a callable that accepts keyword
    arguments matching its input schema. Since our skills accept a single
    `payload: dict`, we wrap them to accept **kwargs and forward as a dict.
    """

    def handler(**kwargs: Any) -> str:
        skill = get_skill(skill_name)
        if not skill:
            return json.dumps({"status": "error", "reason": f"unknown skill: {skill_name}"})
        try:
            result = skill.handler(kwargs)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            logger.exception("MCP tool %s failed", skill_name)
            return json.dumps({"status": "error", "reason": str(e)})

    return handler


# Register all skills as MCP tools
for _skill_def in list_skill_defs():
    _handler = _make_tool_handler(_skill_def.name)
    _handler.__name__ = _skill_def.name
    _handler.__doc__ = f"[{_skill_def.owner_role}] {_skill_def.description}"
    mcp.tool(name=_skill_def.name, description=_handler.__doc__)(_handler)
    logger.debug("Registered MCP tool: %s (%s)", _skill_def.name, _skill_def.owner_role)


# --------------------------------------------------------------------------- #
# Health check resource
# --------------------------------------------------------------------------- #


@mcp.resource("health://status")
def health_status() -> str:
    """Return service health and registered tool count."""
    return json.dumps({
        "status": "ok",
        "tools": len(_REGISTRY),
        "tool_names": sorted(_REGISTRY.keys()),
    })


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    tool_count = len(_REGISTRY)
    port = int(os.getenv("MCP_PORT", os.getenv("PORT", "8090")))
    host = os.getenv("MCP_HOST", "0.0.0.0")
    logger.info(
        "domain-skills MCP server starting on %s:%d (%d tools registered)",
        host, port, tool_count,
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
