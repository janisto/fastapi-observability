import asyncio
import json
import logging
from typing import override

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse

from fastapi_request_observability import (
    AccessLogConfig,
    AccessLogMiddleware,
    JSONFormatter,
    LoggingPreset,
    RequestContextMiddleware,
    request_id,
)
from fastapi_request_observability._context import RequestContext, _bind_context, _reset_context
from tests._client import asgi_client

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


class JSONCaptureHandler(logging.Handler):
    def __init__(self, preset=LoggingPreset.DEFAULT):
        super().__init__()
        self.entries = []
        self.setFormatter(JSONFormatter(preset))

    @override
    def emit(self, record):
        self.entries.append(json.loads(self.format(record)))


class FailingHandler(logging.Handler):
    @override
    def emit(self, record):
        raise RuntimeError("logging failed")


class RecordCaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    @override
    def emit(self, record):
        self.records.append(record)


def _logger(handler):
    logger = logging.getLogger(f"test.access.{id(handler)}")
    logger.handlers.clear()
    logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return logger


def _app(
    handler,
    *,
    preset=LoggingPreset.DEFAULT,
    extra_fields=None,
    clock=None,
    status_level=None,
):
    app = FastAPI()
    config = AccessLogConfig(
        logger=_logger(handler),
        preset=preset,
        extra_fields=extra_fields,
        clock=clock or __import__("time").perf_counter,
        status_level=status_level,
    )
    app.add_middleware(AccessLogMiddleware, config=config)
    app.add_middleware(RequestContextMiddleware)

    @app.get("/items/{item_id}", operation_id="get_item")
    async def item(item_id: str, request: Request):
        return {"item": item_id, "request_id": request_id(), "state": request.state.request_id}

    @app.post("/validate")
    async def validate(value: int):
        return {"value": value}

    @app.get("/handled")
    async def handled():
        raise HTTPException(418, "teapot")

    @app.get("/unhandled")
    async def unhandled():
        raise RuntimeError("boom")

    @app.get("/redirect")
    async def redirect():
        return RedirectResponse("/items/1")

    @app.get("/empty", status_code=204)
    async def empty():
        return None

    @app.get("/stream")
    async def stream():
        async def body():
            yield b"first"
            raise RuntimeError("stream failed")

        return StreamingResponse(body())

    @app.get("/stream-success")
    async def stream_success():
        async def body():
            yield b"first"
            yield b"second"

        return StreamingResponse(body())

    @app.get("/background")
    async def background(tasks: BackgroundTasks):
        tasks.add_task(_background_failure)
        return {"ok": True}

    return app


def _background_failure():
    raise RuntimeError("background failed")


@pytest.mark.parametrize(
    ("method", "path", "status", "level"),
    [
        ("get", "/items/a%20b?secret=yes", 200, "INFO"),
        ("get", "/empty", 204, "INFO"),
        ("get", "/redirect", 307, "INFO"),
        ("get", "/missing", 404, "WARNING"),
        ("post", "/items/one", 405, "WARNING"),
        ("post", "/validate", 422, "WARNING"),
        ("get", "/handled", 418, "WARNING"),
    ],
)
async def test_access_status_matrix(method, path, status, level):
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler), follow_redirects=False) as client:
        response = await client.request(method, path)
    assert response.status_code == status
    assert len(handler.entries) == 1
    assert handler.entries[0]["status"] == status
    assert handler.entries[0]["level"] == level


async def test_route_query_operation_remote_and_context_fields():
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler), client=("203.0.113.4", 5000)) as client:
        response = await client.get(
            "/items/a%20b?secret=yes",
            headers={"X-Request-ID": "request-1", "traceparent": TRACEPARENT, "user-agent": "test-agent"},
        )
    entry = handler.entries[0]
    assert response.json() == {"item": "a b", "request_id": "request-1", "state": "request-1"}
    assert response.headers["X-Request-ID"] == "request-1"
    assert entry["method"] == "GET"
    assert entry["path"] == "/items/a%20b"
    assert "secret" not in entry["path"]
    assert entry["path_template"] == "/items/{item_id}"
    assert entry["operation_id"] == "get_item"
    assert entry["remote_ip"] == "203.0.113.4"
    assert entry["user_agent"] == "test-agent"
    assert entry["request_id"] == "request-1"
    assert entry["correlation_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert entry["duration_ms"] >= 0


async def test_application_and_access_records_share_correlation_fields():
    handler = JSONCaptureHandler()
    logger = _logger(handler)
    app = FastAPI()
    app.add_middleware(AccessLogMiddleware, config=AccessLogConfig(logger=logger))
    app.add_middleware(RequestContextMiddleware)

    @app.get("/")
    async def root():
        logger.info("application record", extra={"component": "handler"})
        return {}

    async with asgi_client(app) as client:
        response = await client.get("/", headers={"X-Request-ID": "shared", "traceparent": TRACEPARENT})
    assert response.status_code == 200
    assert [entry["message"] for entry in handler.entries] == ["application record", "request completed"]
    for entry in handler.entries:
        assert entry["request_id"] == "shared"
        assert entry["correlation_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


async def test_generated_operation_id_is_not_logged_and_404_has_no_route():
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler)) as client:
        await client.post("/validate?value=1")
        assert "operation_id" not in handler.entries[-1]
        await client.get("/missing")
    assert "path_template" not in handler.entries[-1]


async def test_405_retains_matched_route_template():
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler)) as client:
        await client.post("/items/one")
    assert handler.entries[0]["path_template"] == "/items/{item_id}"


async def test_unhandled_exception_logs_500_once_and_standard_middleware_cannot_header_outer_500():
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler), raise_app_exceptions=False) as client:
        response = await client.get("/unhandled")
    assert response.status_code == 500
    assert "X-Request-ID" not in response.headers
    assert len(handler.entries) == 1
    assert handler.entries[0]["status"] == 500
    assert handler.entries[0]["level"] == "ERROR"
    assert handler.entries[0]["error"] == "RuntimeError: boom"


async def test_outer_request_context_wrapper_headers_final_500():
    handler = JSONCaptureHandler()
    wrapped = RequestContextMiddleware(_app_without_context(handler))
    async with asgi_client(wrapped, raise_app_exceptions=False) as client:
        response = await client.get("/unhandled")
    assert response.status_code == 500
    assert len(response.headers["X-Request-ID"]) == 32


def _app_without_context(handler):
    app = FastAPI()
    app.add_middleware(AccessLogMiddleware, config=AccessLogConfig(logger=_logger(handler)))

    @app.get("/unhandled")
    async def unhandled():
        raise RuntimeError("boom")

    return app


async def test_committed_stream_status_is_preserved_and_logged_once():
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler), raise_app_exceptions=False) as client:
        response = await client.get("/stream")
    assert response.status_code == 200
    assert len(handler.entries) == 1
    assert handler.entries[0]["status"] == 200
    assert handler.entries[0]["error"] == "RuntimeError: stream failed"


async def test_streaming_success_logs_after_final_body_once():
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler)) as client:
        response = await client.get("/stream-success")
    assert response.status_code == 200
    assert response.content == b"firstsecond"
    assert len(handler.entries) == 1
    assert handler.entries[0]["status"] == 200
    assert "error" not in handler.entries[0]


async def test_background_failure_does_not_emit_second_record_or_change_completed_record():
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler), raise_app_exceptions=False) as client:
        response = await client.get("/background")
    assert response.status_code == 200
    assert len(handler.entries) == 1
    assert handler.entries[0]["status"] == 200
    assert "error" not in handler.entries[0]


async def test_access_middleware_alone_binds_context_and_adds_header():
    handler = JSONCaptureHandler()
    app = FastAPI()
    app.add_middleware(AccessLogMiddleware, config=AccessLogConfig(logger=_logger(handler)))

    @app.get("/")
    async def root():
        return {"request_id": request_id()}

    async with asgi_client(app) as client:
        response = await client.get("/")
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert handler.entries[0]["request_id"] == response.headers["X-Request-ID"]
    assert request_id() is None


async def test_access_record_snapshots_context_before_deferred_formatting():
    handler = RecordCaptureHandler()
    async with asgi_client(_app(handler)) as client:
        response = await client.get("/items/one", headers={"X-Request-ID": "deferred", "traceparent": TRACEPARENT})
    assert response.status_code == 200
    assert request_id() is None

    entry = json.loads(JSONFormatter().format(handler.records[0]))
    assert entry["request_id"] == "deferred"
    assert entry["correlation_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert entry["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


async def test_deferred_access_record_cannot_be_overwritten_by_another_request_context():
    handler = RecordCaptureHandler()
    async with asgi_client(_app(handler)) as client:
        await client.get("/items/one", headers={"X-Request-ID": "original", "traceparent": TRACEPARENT})

    token = _bind_context(RequestContext("other", "other"))
    try:
        entry = json.loads(JSONFormatter().format(handler.records[0]))
    finally:
        _reset_context(token)
    assert entry["request_id"] == "original"
    assert entry["correlation_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.mark.asyncio
async def test_response_start_send_failure_is_logged_as_uncommitted_500():
    handler = JSONCaptureHandler()

    async def app(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})

    async def failing_send(_message):
        raise RuntimeError("send failed")

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "state": {}}
    with pytest.raises(RuntimeError, match="send failed"):
        await AccessLogMiddleware(app, AccessLogConfig(logger=_logger(handler)))(scope, _empty_receive, failing_send)
    assert handler.entries[0]["status"] == 500
    assert handler.entries[0]["error"] == "RuntimeError: send failed"


@pytest.mark.asyncio
async def test_cancellation_logs_once_reraises_and_resets_context():
    handler = JSONCaptureHandler()

    async def cancelled(_scope, _receive, _send):
        raise asyncio.CancelledError

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "state": {}}
    with pytest.raises(asyncio.CancelledError):
        await AccessLogMiddleware(cancelled, AccessLogConfig(logger=_logger(handler)))(scope, _empty_receive, _discard)
    assert len(handler.entries) == 1
    assert handler.entries[0]["status"] == 500
    assert handler.entries[0]["error"] == "CancelledError"
    assert request_id() is None


@pytest.mark.asyncio
async def test_access_middleware_passes_non_http_scopes_without_logging_or_context():
    handler = JSONCaptureHandler()
    observed = "not-called"

    async def app(_scope, _receive, _send):
        nonlocal observed
        observed = request_id()

    await AccessLogMiddleware(app, AccessLogConfig(logger=_logger(handler)))(
        {"type": "lifespan"}, _empty_receive, _discard
    )
    assert observed is None
    assert handler.entries == []


async def test_custom_fields_filter_reserved_and_callback_failure_is_nonfatal():
    handler = JSONCaptureHandler()
    async with asgi_client(
        _app(handler, extra_fields=lambda _scope: {"tenant": "one", "status": 999, "severity": "bad"})
    ) as client:
        response = await client.get("/items/1")
    assert response.status_code == 200
    assert handler.entries[0]["tenant"] == "one"
    assert handler.entries[0]["status"] == 200
    assert handler.entries[0]["level"] == "INFO"

    def broken_callback(_scope):
        raise RuntimeError("callback failed")

    second_handler = JSONCaptureHandler()
    async with asgi_client(_app(second_handler, extra_fields=broken_callback)) as client:
        assert (await client.get("/items/1")).status_code == 200
    assert len(second_handler.entries) == 1


async def test_logger_failure_does_not_change_response():
    async with asgi_client(_app(FailingHandler())) as client:
        response = await client.get("/items/1")
    assert response.status_code == 200


async def test_custom_status_level_and_negative_clock_clamped():
    handler = JSONCaptureHandler()
    values = iter([10.0, 9.0])
    async with asgi_client(
        _app(handler, clock=lambda: next(values), status_level=lambda _status: logging.DEBUG)
    ) as client:
        await client.get("/items/1")
    assert handler.entries[0]["duration_ms"] == 0
    assert handler.entries[0]["level"] == "DEBUG"


async def test_failing_callbacks_fall_back_without_changing_response():
    def broken_clock():
        raise RuntimeError("clock failed")

    def broken_level(_status):
        raise RuntimeError("level failed")

    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler, clock=broken_clock, status_level=broken_level)) as client:
        response = await client.get("/items/1")
    assert response.status_code == 200
    assert handler.entries[0]["duration_ms"] >= 0
    assert handler.entries[0]["level"] == "INFO"


async def test_gcp_http_request_and_trace_shape_omit_query_and_fake_span():
    handler = JSONCaptureHandler(LoggingPreset.GCP)
    async with asgi_client(_app(handler, preset=LoggingPreset.GCP), client=("192.0.2.4", 1)) as client:
        await client.get("/items/one?token=secret", headers={"traceparent": TRACEPARENT, "host": "example.test"})
    entry = handler.entries[0]
    assert entry["severity"] == "INFO"
    assert entry["httpRequest"]["requestUrl"] == "http://example.test/items/one"
    assert entry["httpRequest"]["remoteIp"] == "192.0.2.4"
    assert entry["httpRequest"]["latency"].endswith("s")
    assert entry["logging.googleapis.com/trace"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert "logging.googleapis.com/spanId" not in entry


async def _empty_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _discard(_message):
    return None
