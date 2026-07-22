"""Real-process FastAPI consumer for the central E2E suite."""

from __future__ import annotations

import logging
import os
import secrets
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from fastapi_request_observability import (
    AccessLogConfig,
    AccessLogMiddleware,
    JSONFormatter,
    LoggingPreset,
    RequestContextConfig,
    RequestContextMiddleware,
    TraceContextLevel,
)

_CASES = frozenset({"common_level1", "common_level2", "aws_level1", "azure_level1", "gcp_level1"})
_NESTED_CONFIGURATION = {
    "system_id": "sys-402",
    "server_settings": {
        "nodes": [{"hostname": "srv-01", "port": 8080, "ssl_enabled": True}],
    },
}


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be nonempty")
    return value


_CASE = _required_environment("OBS_E2E_CASE")
if _CASE not in _CASES:
    raise RuntimeError("OBS_E2E_CASE must select one supported E2E case")
_CANARY = _required_environment("OBS_E2E_SECRET_CANARY")
_TRACE_LEVEL = TraceContextLevel.LEVEL_2 if _CASE == "common_level2" else TraceContextLevel.LEVEL_1
_PRESET = {
    "aws_level1": LoggingPreset.AWS,
    "azure_level1": LoggingPreset.AZURE,
    "gcp_level1": LoggingPreset.GCP,
}.get(_CASE, LoggingPreset.DEFAULT)


def _configured_logger(name: str) -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter(_PRESET))
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _extra_fields(_: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"e2e_configuration": _NESTED_CONFIGURATION}


_APPLICATION_LOGGER = _configured_logger("e2e.handler")
_ACCESS_LOGGER = _configured_logger("e2e.access")
app = FastAPI()
app.add_middleware(
    AccessLogMiddleware,
    config=AccessLogConfig(
        logger=_ACCESS_LOGGER,
        preset=_PRESET,
        trace_context_level=_TRACE_LEVEL,
        extra_fields=_extra_fields if _CASE == "gcp_level1" else None,
    ),
)
app.add_middleware(
    RequestContextMiddleware,
    config=RequestContextConfig(trace_context_level=_TRACE_LEVEL),
)


@app.get("/trace", operation_id="trace")
async def trace(request: Request) -> JSONResponse:
    """Validate the canary and emit one request-correlated application event."""
    authorization = request.headers.get("authorization", "")
    if not secrets.compare_digest(authorization, f"Bearer {_CANARY}"):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    _APPLICATION_LOGGER.info("handler", extra={"event": "trace"})
    return JSONResponse(
        content={
            "ok": True,
            "request_id": request.state.request_id,
            "canary_received": True,
        }
    )
