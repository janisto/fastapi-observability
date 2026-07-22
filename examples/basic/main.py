"""Minimal provider-neutral FastAPI application."""

import logging

from fastapi import FastAPI

from fastapi_request_observability import (
    AccessLogConfig,
    AccessLogMiddleware,
    JSONFormatter,
    RequestContextMiddleware,
)

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
root_logger = logging.getLogger()
root_logger.handlers.clear()
root_logger.addHandler(handler)
root_logger.setLevel(logging.INFO)


app = FastAPI()
app.add_middleware(
    AccessLogMiddleware,
    config=AccessLogConfig(logger=logging.getLogger("http.access")),
)
app.add_middleware(RequestContextMiddleware)


@app.get("/health", operation_id="health_check")
async def health() -> dict[str, bool]:
    """Return service health."""
    logging.getLogger(__name__).info("health check")
    return {"ok": True}
