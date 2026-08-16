"""MCP Server 层 OpenTelemetry 埋点（可选）。

方案设计 v2.2 §6.3.3 要求：MCP Server 作为独立进程，需手动接入 OTel，
将 `pgvector_search`、`hybrid_search` 等 MCP 原语调用生成 Span，
通过 OTLP 协议上报到 AgentLoop（或任何兼容后端），与 Worker 层 Trace 串联。

本模块为可选依赖：如果未安装 opentelemetry 相关包，则自动降级为 no-op，
不影响 MCP Server 正常运行。
"""

import functools
import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger("mcp_telemetry")

# --------------------------------------------------------------------------- #
# 可选依赖：未安装时整体降级为 no-op
# --------------------------------------------------------------------------- #
try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _OTEL_AVAILABLE = False
    trace = None  # type: ignore


_tracer: Optional[Any] = None


def _is_enabled() -> bool:
    """根据环境变量判断是否启用 OTel。"""
    return os.getenv("MCP_OTEL_ENABLED", "").lower() in ("1", "true", "yes", "on")


def init_telemetry(service_name: Optional[str] = None) -> None:
    """初始化 MCP Server 的 OTel Tracer。

    从环境变量读取 OTLP endpoint 与鉴权信息：
      - AGENTLOOP_ENDPOINT / OTEL_EXPORTER_OTLP_ENDPOINT
      - AGENTLOOP_LICENSE_KEY / OTEL_EXPORTER_OTLP_HEADERS 中的 x-arms-license-key
      - AGENTLOOP_SERVICE_NAME / OTEL_SERVICE_NAME

    未安装 opentelemetry 或未开启开关时，本函数为空操作。
    """
    global _tracer

    if not _OTEL_AVAILABLE:
        logger.debug("opentelemetry not installed; telemetry disabled")
        return

    if not _is_enabled():
        logger.debug("MCP_OTEL_ENABLED is off; telemetry disabled")
        return

    service_name = service_name or os.getenv("AGENTLOOP_SERVICE_NAME") or os.getenv("OTEL_SERVICE_NAME") or "daedalus-mcp-server"
    endpoint = os.getenv("AGENTLOOP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint:
        logger.warning("MCP_OTEL_ENABLED=true but no OTLP endpoint found; telemetry disabled")
        return

    resource = Resource(attributes={SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)

    headers = {}
    license_key = os.getenv("AGENTLOOP_LICENSE_KEY", "")
    if license_key:
        headers["x-arms-license-key"] = license_key

    # 允许通过 OTEL_EXPORTER_OTLP_HEADERS 覆盖或补充 headers
    extra_headers = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
    if extra_headers:
        for part in extra_headers.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                headers[k.strip()] = v.strip()

    exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("daedalus.mcp_server")
    logger.info("MCP Server OTel tracer initialized: service=%s endpoint=%s", service_name, endpoint)


def instrument(name: Optional[str] = None) -> Callable:
    """装饰器：为函数调用生成一个 OTel Span。

    用法：
        @instrument("pgvector_search")
        def pgvector_search(...) -> dict: ...

    未启用或 opentelemetry 未安装时，原样调用函数（零开销）。
    """
    def decorator(fn: Callable) -> Callable:
        span_name = name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if _tracer is None:
                return fn(*args, **kwargs)
            with _tracer.start_as_current_span(span_name) as span:
                try:
                    result = fn(*args, **kwargs)
                    if isinstance(result, dict):
                        status = result.get("status")
                        if status:
                            span.set_attribute("mcp.result.status", str(status))
                        if result.get("reason"):
                            span.set_attribute("mcp.result.reason", str(result["reason"]))
                    return result
                except Exception as exc:
                    span.set_attribute("error", True)
                    span.set_attribute("error.message", str(exc))
                    raise

        return wrapper

    return decorator


def current_span() -> Optional[Any]:
    """返回当前激活的 Span（未启用时返回 None）。"""
    if _tracer is None or trace is None:
        return None
    return trace.get_current_span()
