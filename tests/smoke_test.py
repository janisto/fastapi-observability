import asyncio
import json
import logging
from typing import override

from fastapi_request_observability import (
    AccessLogConfig,
    AccessLogMiddleware,
    JSONFormatter,
    LoggingPreset,
    RequestContext,
    RequestContextConfig,
    RequestContextMiddleware,
    TraceContext,
    correlation_id,
    current_request_context,
    parse_traceparent,
    request_id,
    trace_context,
)

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


class CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.entries = []
        self.setFormatter(JSONFormatter())

    @override
    def emit(self, record):
        self.entries.append(json.loads(self.format(record)))


async def _smoke_built_middleware():
    handler = CaptureHandler()
    logger = logging.getLogger("smoke.access")
    logger.handlers.clear()
    logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    observed = {}
    sent = []

    async def endpoint(_scope, _receive, send):
        context = current_request_context()
        trace = trace_context()
        observed.update(
            {
                "context": context,
                "request_id": request_id(),
                "correlation_id": correlation_id(),
                "trace": trace,
            }
        )
        await send(
            {
                "type": "http.response.start",
                "status": 201,
                "headers": [(b"x-application", b"preserved")],
            }
        )
        await send({"type": "http.response.body", "body": b"created"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def capture(message):
        sent.append(message.copy())

    app = RequestContextMiddleware(
        AccessLogMiddleware(endpoint, AccessLogConfig(logger=logger)),
        RequestContextConfig(),
    )
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "https",
        "path": "/smoke",
        "raw_path": b"/smoke",
        "headers": [
            (b"host", b"example.test"),
            (b"x-request-id", b"smoke-request"),
            (b"traceparent", TRACEPARENT.encode()),
        ],
        "client": ("192.0.2.1", 1234),
        "server": ("example.test", 443),
        "state": {},
    }
    await app(scope, receive, capture)

    assert LoggingPreset.DEFAULT.value == "default"
    assert isinstance(observed["context"], RequestContext)
    assert observed["request_id"] == "smoke-request"
    assert observed["correlation_id"] == TRACE_ID
    assert isinstance(observed["trace"], TraceContext)
    assert sent == [
        {
            "type": "http.response.start",
            "status": 201,
            "headers": [(b"x-application", b"preserved"), (b"x-request-id", b"smoke-request")],
        },
        {"type": "http.response.body", "body": b"created"},
    ]
    assert len(handler.entries) == 1
    assert {
        key: handler.entries[0][key]
        for key in ("logger", "message", "method", "path", "status", "request_id", "correlation_id")
    } == {
        "logger": "smoke.access",
        "message": "request completed",
        "method": "POST",
        "path": "/smoke",
        "status": 201,
        "request_id": "smoke-request",
        "correlation_id": TRACE_ID,
    }


parsed_trace = parse_traceparent(TRACEPARENT)
assert isinstance(parsed_trace, TraceContext)
asyncio.run(_smoke_built_middleware())
assert request_id() is None
assert correlation_id() is None
assert trace_context() is None
assert current_request_context() is None
