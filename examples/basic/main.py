"""Minimal provider-neutral FastAPI application."""

import logging

from fastapi import FastAPI

from fastapi_request_observability import (
    AccessLogConfig,
    AccessLogMiddleware,
    JSONFormatter,
    RequestContextConfig,
    RequestContextMiddleware,
    TraceContextLevel,
)

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
root_logger = logging.getLogger()
root_logger.handlers.clear()
root_logger.addHandler(handler)
root_logger.setLevel(logging.INFO)


def create_basic_app(trace_context_level: TraceContextLevel | None = None) -> FastAPI:
    """Create the provider-neutral example, optionally opting in to a W3C level."""
    app = FastAPI()
    access_config = AccessLogConfig(logger=logging.getLogger("http.access"))
    request_config = RequestContextConfig()
    if trace_context_level is not None:
        access_config = AccessLogConfig(
            logger=logging.getLogger("http.access"),
            trace_context_level=trace_context_level,
        )
        request_config = RequestContextConfig(trace_context_level=trace_context_level)
    app.add_middleware(
        AccessLogMiddleware,
        config=access_config,
    )
    app.add_middleware(
        RequestContextMiddleware,
        config=request_config,
    )

    @app.get("/health", operation_id="health_check")
    async def health() -> dict[str, bool]:
        """Return service health."""
        logging.getLogger(__name__).info("health check")
        return {"ok": True}

    return app


def create_default_app() -> FastAPI:
    """Create the example with its default W3C Trace Context Level 1."""
    return create_basic_app()


def create_level_2_app() -> FastAPI:
    """Create the explicit W3C Trace Context Level 2 example."""
    return create_basic_app(TraceContextLevel.LEVEL_2)


app = create_default_app()
