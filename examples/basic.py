"""Runnable generic FastAPI application."""

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


@app.get("/items/{item_id}", operation_id="get_item")
async def get_item(item_id: str) -> dict[str, str]:
    """Return one example item."""
    logging.getLogger(__name__).info("loading item", extra={"item_id": item_id})
    return {"item_id": item_id}
