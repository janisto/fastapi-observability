"""Minimal Google Cloud FastAPI application."""

import logging
import sys

from fastapi import FastAPI

from fastapi_request_observability import (
    AccessLogConfig,
    AccessLogMiddleware,
    JSONFormatter,
    LoggingPreset,
    RequestContextMiddleware,
)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter(LoggingPreset.GCP))
root_logger = logging.getLogger()
root_logger.handlers.clear()
root_logger.addHandler(handler)
root_logger.setLevel(logging.INFO)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

app = FastAPI()
app.add_middleware(
    AccessLogMiddleware,
    config=AccessLogConfig(
        logger=logging.getLogger("http.access"),
        preset=LoggingPreset.GCP,
    ),
)
app.add_middleware(RequestContextMiddleware)


@app.get("/health", operation_id="health_check")
async def health() -> dict[str, bool]:
    """Return service health."""
    logger.info(
        "health check",
        extra={
            "service_name": "example-service",
            "service_version": "1.4.2",
            "health_status": "ok",
        },
    )
    logger.debug(
        "dependency check",
        extra={
            "dependency": "database",
            "dependency_status": "ok",
            "check_duration_ms": 3,
        },
    )
    return {"ok": True}
