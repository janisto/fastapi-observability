import asyncio
import json
import logging
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, override

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from fastapi_request_observability import (
    AccessLogConfig,
    AccessLogMiddleware,
    GcpProfileVersion,
    JSONFormatter,
    LoggingPreset,
    RequestContextConfig,
    RequestContextMiddleware,
    TraceContextLevel,
    parse_traceparent,
    request_id,
)
from fastapi_request_observability._context import RequestContext, _bind_context, _reset_context
from fastapi_request_observability.access import (
    _canonical_route_template,
    _duration_ms,
    _protobuf_duration,
    _request_path,
    _route_fields,
)
from fastapi_request_observability.middleware import _SCOPE_CONTEXT_KEY
from tests._client import asgi_client

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
RESERVED_CALLBACK_FIELDS = {
    "timestamp",
    "level",
    "severity",
    "logger",
    "message",
    "stacktrace",
    "request_id",
    "correlation_id",
    "trace_id",
    "parent_id",
    "trace_flags",
    "trace_sampled",
    "trace_id_random",
    "logging.googleapis.com/trace",
    "logging.googleapis.com/trace_sampled",
    "logging.googleapis.com/spanId",
    "xray_trace_id",
    "operation_Id",
    "operation_ParentId",
    "method",
    "path",
    "path_template",
    "operation_id",
    "status",
    "duration_ms",
    "terminal_reason",
    "peer_ip",
    "remote_ip",
    "user_agent",
    "error",
    "httpRequest",
    "source",
}


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


class FailingStream:
    def write(self, _value):
        raise OSError("diagnostic sink failed")


class PartiallyFailingFields(Mapping[str, str]):
    @override
    def __getitem__(self, key):
        if key == "partial":
            return "must-not-leak"
        raise KeyError(key)

    @override
    def __iter__(self):
        yield "partial"
        raise RuntimeError("mapping iteration failed")

    @override
    def __len__(self):
        return 2


class BrokenStringError(RuntimeError):
    @override
    def __str__(self):
        raise RuntimeError("string conversion failed")


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
    message="request completed",
):
    app = FastAPI()
    config = AccessLogConfig(
        logger=_logger(handler),
        preset=preset,
        capture_path=True,
        capture_peer_ip=True,
        capture_user_agent=True,
        extra_fields=extra_fields,
        clock=clock or __import__("time").perf_counter,
        status_level=status_level,
        message=message,
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

    @app.get("/bad-request")
    async def bad_request():
        raise HTTPException(400, "bad request")

    @app.get("/unhandled")
    async def unhandled():
        raise RuntimeError("boom")

    @app.get("/redirect")
    async def redirect():
        return RedirectResponse("/items/1")

    @app.get("/status/{status_code}")
    async def status(status_code: int):
        return Response(status_code=status_code)

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


def test_access_config_resolves_latest_preserves_pin_and_validates_new_options():
    latest = AccessLogConfig(preset=LoggingPreset.GCP)
    pinned = AccessLogConfig(preset=LoggingPreset.GCP, gcp_profile_version="0.1.0")
    assert latest.gcp_profile_version is GcpProfileVersion.V0_1_0
    assert pinned.gcp_profile_version is GcpProfileVersion.V0_1_0
    assert AccessLogConfig().gcp_profile_version is None
    assert AccessLogConfig().trace_context_level is TraceContextLevel.LEVEL_1
    assert AccessLogConfig(trace_context_level=2).trace_context_level is TraceContextLevel.LEVEL_2
    with pytest.raises(ValueError, match="unsupported GCP profile version"):
        AccessLogConfig(preset=LoggingPreset.GCP, gcp_profile_version="0.2.0")
    with pytest.raises(ValueError, match=r"requires LoggingPreset\.GCP"):
        AccessLogConfig(gcp_profile_version="0.1.0")
    invalid: Any = 1
    for name in ("capture_path", "capture_peer_ip", "capture_user_agent"):
        kwargs: Any = {name: invalid}
        with pytest.raises(TypeError, match=rf"{name} must be a boolean"):
            AccessLogConfig(**kwargs)
    with pytest.raises(TypeError, match="clock must be callable"):
        AccessLogConfig(clock=invalid)
    with pytest.raises(ValueError, match="unsupported trace context level"):
        AccessLogConfig(trace_context_level=3)


def _background_failure():
    raise RuntimeError("background failed")


@pytest.mark.parametrize(
    ("method", "path", "status", "level"),
    [
        ("get", "/items/a%20b?secret=yes", 200, "INFO"),
        ("get", "/empty", 204, "INFO"),
        ("get", "/redirect", 307, "INFO"),
        ("get", "/status/399", 399, "INFO"),
        ("get", "/status/499", 499, "WARNING"),
        ("get", "/status/500", 500, "ERROR"),
        ("get", "/missing", 404, "WARNING"),
        ("get", "/bad-request", 400, "WARNING"),
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


async def test_route_query_operation_peer_and_context_fields():
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
    assert entry["peer_ip"] == "203.0.113.4"
    assert entry["user_agent"] == "test-agent"
    assert entry["request_id"] == "request-1"
    assert entry["correlation_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert entry["duration_ms"] >= 0


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        ("/health", "/health"),
        ("/items/{item_id}", "/items/{item_id}"),
        ("/items/{item_id:int}", "/items/{item_id}"),
        ("/files/{path:path}", "/files/{*path}"),
        ("/files/{path:path}/suffix", None),
        ("/items/{item_id?}", None),
    ],
)
def test_route_template_canonicalizes_current_fastapi_forms(native, expected):
    assert _canonical_route_template(native) == expected


def test_hostile_route_metadata_is_omitted_without_escaping(capsys):
    class HostileRoute:
        @property
        def path(self):
            raise RuntimeError("private route metadata")

    scope: Any = {"route": HostileRoute()}
    assert _route_fields(scope) == {}
    diagnostic = capsys.readouterr().err
    assert "access route metadata failed: RuntimeError" in diagnostic
    assert "private route metadata" not in diagnostic


async def test_representative_route_identity_has_stable_cardinality():
    handler = JSONCaptureHandler()
    app = FastAPI()
    app.add_middleware(AccessLogMiddleware, config=AccessLogConfig(logger=_logger(handler)))
    app.add_middleware(RequestContextMiddleware)

    @app.get("/items/{item_id}", operation_id="get_item")
    async def items(item_id: str):
        return {"item_id": item_id}

    @app.get("/files/{path:path}", operation_id="get_file")
    async def files(path: str):
        return {"path": path}

    async with asgi_client(app) as client:
        assert (await client.get("/items/tenant-a")).status_code == 200
        assert (await client.get("/items/tenant-b")).status_code == 200
        assert (await client.get("/files/tenant-a/one")).status_code == 200
        assert (await client.get("/files/tenant-b/two")).status_code == 200

    assert [(entry["path_template"], entry["operation_id"]) for entry in handler.entries] == [
        ("/items/{item_id}", "get_item"),
        ("/items/{item_id}", "get_item"),
        ("/files/{*path}", "get_file"),
        ("/files/{*path}", "get_file"),
    ]


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


async def test_custom_access_message_replaces_default_message_once():
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler, message="http request finished")) as client:
        response = await client.get("/items/one")

    assert response.status_code == 200
    assert [entry["message"] for entry in handler.entries] == ["http request finished"]


async def test_access_middleware_reuses_custom_request_context_instead_of_rebuilding_defaults():
    handler = JSONCaptureHandler()
    app = FastAPI()
    app.add_middleware(AccessLogMiddleware, config=AccessLogConfig(logger=_logger(handler)))
    app.add_middleware(
        RequestContextMiddleware,
        config=RequestContextConfig(
            request_id_header="X-Correlation-ID",
            response_header="X-Correlation-ID",
            request_id_generator=lambda: "custom-generated",
        ),
    )

    @app.get("/")
    async def root():
        return {"request_id": request_id()}

    async with asgi_client(app) as client:
        response = await client.get("/", headers={"X-Correlation-ID": "custom-incoming"})

    assert response.json() == {"request_id": "custom-incoming"}
    assert response.headers["X-Correlation-ID"] == "custom-incoming"
    assert "X-Request-ID" not in response.headers
    assert handler.entries[0]["request_id"] == "custom-incoming"


async def test_generated_operation_id_is_not_logged():
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler)) as client:
        await client.post("/validate?value=1")

    assert "operation_id" not in handler.entries[0]


async def test_404_access_record_has_no_route_template():
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler)) as client:
        await client.get("/missing")

    assert "path_template" not in handler.entries[0]


async def test_405_retains_matched_route_template():
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler)) as client:
        await client.post("/items/one")
    assert handler.entries[0]["path_template"] == "/items/{item_id}"


async def test_unhandled_exception_omits_unobserved_outer_500_and_logs_service_error_once():
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler), raise_app_exceptions=False) as client:
        response = await client.get("/unhandled")
    assert response.status_code == 500
    assert "X-Request-ID" not in response.headers
    assert len(handler.entries) == 1
    assert "status" not in handler.entries[0]
    assert handler.entries[0]["terminal_reason"] == "service_error"
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
    assert handler.entries[0]["terminal_reason"] == "body_error"
    assert handler.entries[0]["level"] == "ERROR"
    assert handler.entries[0]["error"] == "RuntimeError: stream failed"


async def test_streaming_success_preserves_body_and_logs_once():
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler)) as client:
        response = await client.get("/stream-success")
    assert response.status_code == 200
    assert response.content == b"firstsecond"
    assert len(handler.entries) == 1
    assert handler.entries[0]["status"] == 200
    assert "error" not in handler.entries[0]


@pytest.mark.asyncio
async def test_access_record_is_emitted_after_final_stream_body_reaches_downstream_send():
    handler = JSONCaptureHandler()
    observed = {}

    async def app(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        observed["after_first_chunk"] = len(handler.entries)
        await send({"type": "http.response.body", "body": b"second"})
        observed["after_final_chunk"] = len(handler.entries)

    async def capture(message):
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            observed["during_final_send"] = len(handler.entries)

    scope = {"type": "http", "method": "GET", "path": "/stream", "headers": [], "state": {}}
    await AccessLogMiddleware(app, AccessLogConfig(logger=_logger(handler)))(scope, _empty_receive, capture)

    assert observed == {
        "after_first_chunk": 0,
        "during_final_send": 0,
        "after_final_chunk": 1,
    }
    assert len(handler.entries) == 1
    assert handler.entries[0]["status"] == 200


@pytest.mark.asyncio
async def test_access_record_waits_for_final_response_trailers():
    handler = JSONCaptureHandler()
    observed = {}
    sent = []

    async def app(_scope, _receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"trailer", b"digest, x-checksum")],
                "trailers": True,
            }
        )
        await send({"type": "http.response.body", "body": b"payload"})
        observed["after_body"] = len(handler.entries)
        await send(
            {
                "type": "http.response.trailers",
                "headers": [(b"digest", b"sha-256=first")],
                "more_trailers": True,
            }
        )
        observed["after_first_trailers"] = len(handler.entries)
        await send(
            {
                "type": "http.response.trailers",
                "headers": [(b"x-checksum", b"second")],
            }
        )
        observed["after_final_trailers"] = len(handler.entries)

    async def capture(message):
        sent.append(message.copy())
        if message["type"] == "http.response.trailers" and not message.get("more_trailers", False):
            observed["during_final_trailers_send"] = len(handler.entries)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/trailers",
        "headers": [(b"x-request-id", b"trailers-request")],
        "extensions": {"http.response.trailers": {}},
        "state": {},
    }
    await AccessLogMiddleware(app, AccessLogConfig(logger=_logger(handler)))(scope, _empty_receive, capture)

    assert observed == {
        "after_body": 0,
        "after_first_trailers": 0,
        "during_final_trailers_send": 0,
        "after_final_trailers": 1,
    }
    assert sent == [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"trailer", b"digest, x-checksum"),
                (b"x-request-id", b"trailers-request"),
            ],
            "trailers": True,
        },
        {"type": "http.response.body", "body": b"payload"},
        {
            "type": "http.response.trailers",
            "headers": [(b"digest", b"sha-256=first")],
            "more_trailers": True,
        },
        {
            "type": "http.response.trailers",
            "headers": [(b"x-checksum", b"second")],
        },
    ]
    assert len(handler.entries) == 1
    assert handler.entries[0]["request_id"] == "trailers-request"
    assert handler.entries[0]["status"] == 200


@pytest.mark.asyncio
async def test_final_response_trailer_send_failure_is_logged_with_committed_status():
    handler = JSONCaptureHandler()
    error = OSError("client disconnected before trailers")

    async def app(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 206, "headers": [], "trailers": True})
        await send({"type": "http.response.body", "body": b"partial"})
        await send({"type": "http.response.trailers", "headers": [(b"digest", b"sha-256=value")]})

    async def fail_on_trailers(message):
        if message["type"] == "http.response.trailers":
            raise error

    scope = {"type": "http", "method": "GET", "path": "/trailers", "headers": [], "state": {}}
    with pytest.raises(OSError, match=r"^client disconnected before trailers$") as raised:
        await AccessLogMiddleware(app, AccessLogConfig(logger=_logger(handler)))(
            scope,
            _empty_receive,
            fail_on_trailers,
        )

    assert raised.value is error
    assert len(handler.entries) == 1
    assert handler.entries[0]["status"] == 206
    assert handler.entries[0]["terminal_reason"] == "body_error"
    assert handler.entries[0]["level"] == "ERROR"
    assert handler.entries[0]["error"] == "OSError: client disconnected before trailers"
    assert request_id() is None


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


@pytest.mark.asyncio
async def test_access_middleware_temporarily_replaces_and_restores_invalid_scope_context():
    handler = JSONCaptureHandler()
    previous = object()
    observed = None

    async def app(scope, _receive, send):
        nonlocal observed
        observed = (scope[_SCOPE_CONTEXT_KEY], request_id())
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-request-id", b"generated-context")],
        "state": {},
        _SCOPE_CONTEXT_KEY: previous,
    }
    await AccessLogMiddleware(app, AccessLogConfig(logger=_logger(handler)))(scope, _empty_receive, _discard)

    assert observed is not None
    assert isinstance(observed[0], RequestContext)
    assert observed[0].request_id == "generated-context"
    assert observed[1] == "generated-context"
    assert scope[_SCOPE_CONTEXT_KEY] is previous
    assert request_id() is None


@pytest.mark.asyncio
async def test_concurrent_access_records_keep_request_context_and_status_isolated():
    handler = JSONCaptureHandler()
    ready = asyncio.Event()
    lock = asyncio.Lock()
    arrivals = 0
    observed = {}
    statuses = {"/one": 201, "/two": 503}

    async def app(scope, _receive, send):
        nonlocal arrivals
        path = scope["path"]
        observed[f"{path}:before"] = (request_id(), scope["state"]["request_id"])
        await send({"type": "http.response.start", "status": statuses[path], "headers": []})
        async with lock:
            arrivals += 1
            if arrivals == len(statuses):
                ready.set()
        await ready.wait()
        observed[f"{path}:after"] = request_id()
        await send({"type": "http.response.body", "body": path.encode()})

    middleware = AccessLogMiddleware(app, AccessLogConfig(logger=_logger(handler), capture_path=True))

    async def invoke(path, incoming_request_id):
        sent = []

        async def capture(message):
            sent.append(message.copy())

        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "headers": [(b"x-request-id", incoming_request_id.encode())],
            "state": {},
        }
        await middleware(scope, _empty_receive, capture)
        assert _SCOPE_CONTEXT_KEY not in scope
        return sent

    first_messages, second_messages = await asyncio.gather(
        invoke("/one", "request-one"),
        invoke("/two", "request-two"),
    )

    assert observed == {
        "/one:before": ("request-one", "request-one"),
        "/one:after": "request-one",
        "/two:before": ("request-two", "request-two"),
        "/two:after": "request-two",
    }
    assert first_messages[0]["headers"] == [(b"x-request-id", b"request-one")]
    assert second_messages[0]["headers"] == [(b"x-request-id", b"request-two")]
    assert len(handler.entries) == 2
    assert {entry["request_id"]: (entry["path"], entry["status"], entry["level"]) for entry in handler.entries} == {
        "request-one": ("/one", 201, "INFO"),
        "request-two": ("/two", 503, "ERROR"),
    }
    assert request_id() is None


async def test_access_record_snapshots_context_before_deferred_formatting():
    handler = RecordCaptureHandler()
    async with asgi_client(_app(handler, preset=LoggingPreset.GCP)) as client:
        response = await client.get("/items/one", headers={"X-Request-ID": "deferred", "traceparent": TRACEPARENT})
    assert response.status_code == 200
    assert request_id() is None

    entry = json.loads(JSONFormatter(LoggingPreset.GCP).format(handler.records[0]))
    assert entry["request_id"] == "deferred"
    assert entry["correlation_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert entry["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert entry["logging.googleapis.com/trace"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert entry["logging.googleapis.com/trace_sampled"] is True


async def test_deferred_access_record_cannot_be_overwritten_by_another_request_context():
    handler = RecordCaptureHandler()
    async with asgi_client(_app(handler)) as client:
        await client.get("/items/one", headers={"X-Request-ID": "original", "traceparent": TRACEPARENT})

    other_trace = parse_traceparent("00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-00")
    assert other_trace is not None
    token = _bind_context(RequestContext("other", other_trace.trace_id, other_trace))
    try:
        entry = json.loads(JSONFormatter().format(handler.records[0]))
    finally:
        _reset_context(token)
    assert entry["request_id"] == "original"
    assert entry["correlation_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert entry["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert entry["parent_id"] == "00f067aa0ba902b7"
    assert entry["trace_flags"] == "01"
    assert entry["trace_sampled"] is True


@pytest.mark.asyncio
async def test_response_start_send_failure_omits_uncommitted_status():
    handler = JSONCaptureHandler()

    async def app(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})

    async def failing_send(_message):
        raise RuntimeError("send failed")

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "state": {}}
    with pytest.raises(RuntimeError, match="send failed"):
        await AccessLogMiddleware(app, AccessLogConfig(logger=_logger(handler)))(scope, _empty_receive, failing_send)
    assert "status" not in handler.entries[0]
    assert handler.entries[0]["terminal_reason"] == "service_error"
    assert handler.entries[0]["level"] == "ERROR"
    assert handler.entries[0]["error"] == "RuntimeError: send failed"


@pytest.mark.asyncio
async def test_response_body_send_failure_preserves_committed_status_and_reraises():
    handler = JSONCaptureHandler()
    start_sent = False

    async def app(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b"accepted"})

    async def failing_body_send(message):
        nonlocal start_sent
        if message["type"] == "http.response.start":
            start_sent = True
            return
        raise RuntimeError("client disconnected")

    scope = {"type": "http", "method": "POST", "path": "/", "headers": [], "state": {}}
    with pytest.raises(RuntimeError, match="client disconnected"):
        await AccessLogMiddleware(app, AccessLogConfig(logger=_logger(handler)))(
            scope, _empty_receive, failing_body_send
        )

    assert start_sent
    assert len(handler.entries) == 1
    assert handler.entries[0]["status"] == 202
    assert handler.entries[0]["terminal_reason"] == "body_error"
    assert handler.entries[0]["level"] == "ERROR"
    assert handler.entries[0]["error"] == "RuntimeError: client disconnected"
    assert request_id() is None


@pytest.mark.asyncio
async def test_exception_with_broken_string_conversion_is_logged_without_replacing_original_error():
    handler = JSONCaptureHandler()
    error = BrokenStringError()

    async def broken(_scope, _receive, _send):
        raise error

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "state": {}}
    with pytest.raises(BrokenStringError) as raised:
        await AccessLogMiddleware(broken, AccessLogConfig(logger=_logger(handler)))(scope, _empty_receive, _discard)

    assert raised.value is error
    assert "status" not in handler.entries[0]
    assert handler.entries[0]["terminal_reason"] == "service_error"
    assert handler.entries[0]["level"] == "ERROR"
    assert handler.entries[0]["error"] == "BrokenStringError"


@pytest.mark.asyncio
async def test_cancellation_logs_once_reraises_and_resets_context():
    handler = JSONCaptureHandler()

    async def cancelled(_scope, _receive, _send):
        raise asyncio.CancelledError

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "state": {}}
    with pytest.raises(asyncio.CancelledError):
        await AccessLogMiddleware(cancelled, AccessLogConfig(logger=_logger(handler)))(scope, _empty_receive, _discard)
    assert len(handler.entries) == 1
    assert "status" not in handler.entries[0]
    assert handler.entries[0]["terminal_reason"] == "cancelled"
    assert handler.entries[0]["level"] == "ERROR"
    assert handler.entries[0]["error"] == "CancelledError"
    assert request_id() is None


@pytest.mark.asyncio
async def test_return_without_response_is_unknown_failure_without_inferred_status():
    handler = JSONCaptureHandler(LoggingPreset.GCP)

    async def incomplete(_scope, _receive, _send):
        return

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "state": {}}
    await AccessLogMiddleware(
        incomplete,
        AccessLogConfig(logger=_logger(handler), preset=LoggingPreset.GCP),
    )(scope, _empty_receive, _discard)

    entry = handler.entries[0]
    assert entry["terminal_reason"] == "unknown_failure"
    assert entry["severity"] == "ERROR"
    assert "status" not in entry
    assert "status" not in entry["httpRequest"]


@pytest.mark.asyncio
async def test_started_response_returned_without_final_body_is_response_dropped():
    handler = JSONCaptureHandler()

    async def incomplete(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "state": {}}
    await AccessLogMiddleware(incomplete, AccessLogConfig(logger=_logger(handler)))(
        scope,
        _empty_receive,
        _discard,
    )

    assert handler.entries[0]["status"] == 204
    assert handler.entries[0]["terminal_reason"] == "response_dropped"
    assert handler.entries[0]["level"] == "ERROR"


@pytest.mark.asyncio
async def test_access_middleware_passes_non_http_scopes_without_logging_or_context():
    handler = JSONCaptureHandler()
    observed = "not-called"

    async def app(inner_scope, inner_receive, inner_send):
        nonlocal observed
        observed = (inner_scope, inner_receive, inner_send, request_id())

    scope = {"type": "lifespan"}
    await AccessLogMiddleware(app, AccessLogConfig(logger=_logger(handler)))(scope, _empty_receive, _discard)
    assert observed == (scope, _empty_receive, _discard, None)
    assert handler.entries == []


@pytest.mark.asyncio
async def test_access_created_context_adds_header_only_to_response_start():
    handler = JSONCaptureHandler()
    sent = []

    async def app(_scope, _receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [(b"x-custom", b"preserved"), (b"X-Request-ID", b"application")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def capture(message):
        sent.append(message.copy())

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-request-id", b"incoming")],
        "state": {},
    }
    await AccessLogMiddleware(app, AccessLogConfig(logger=_logger(handler)))(scope, _empty_receive, capture)

    assert sent == [
        {
            "type": "http.response.start",
            "status": 204,
            "headers": [(b"x-custom", b"preserved"), (b"x-request-id", b"incoming")],
        },
        {"type": "http.response.body", "body": b""},
    ]
    assert len(handler.entries) == 1
    assert _SCOPE_CONTEXT_KEY not in scope


async def test_custom_fields_receive_final_scope_and_cannot_override_reserved_fields():
    handler = JSONCaptureHandler()

    def custom_fields(scope):
        return {
            **dict.fromkeys(RESERVED_CALLBACK_FIELDS, "spoofed"),
            "tenant": scope["path"],
            "callback_path_template": scope["route"].path,
        }

    async with asgi_client(_app(handler, extra_fields=custom_fields)) as client:
        response = await client.get(
            "/items/1",
            headers={"X-Request-ID": "actual-request", "traceparent": TRACEPARENT},
        )
    assert response.status_code == 200
    assert len(handler.entries) == 1
    entry = handler.entries[0]
    assert entry["tenant"] == "/items/1"
    assert entry["callback_path_template"] == "/items/{item_id}"
    assert not {key for key in RESERVED_CALLBACK_FIELDS if entry.get(key) == "spoofed"}
    assert {
        key: entry[key]
        for key in (
            "level",
            "logger",
            "message",
            "request_id",
            "correlation_id",
            "method",
            "path",
            "path_template",
            "status",
        )
    } == {
        "level": "INFO",
        "logger": f"test.access.{id(handler)}",
        "message": "request completed",
        "request_id": "actual-request",
        "correlation_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "method": "GET",
        "path": "/items/1",
        "path_template": "/items/{item_id}",
        "status": 200,
    }


async def test_extra_fields_callback_failure_is_diagnosed_and_nonfatal(capsys):
    def broken_callback(_scope):
        raise RuntimeError("callback failed")

    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler, extra_fields=broken_callback)) as client:
        response = await client.get("/items/1")

    assert response.status_code == 200
    assert len(handler.entries) == 1
    assert capsys.readouterr().err == (
        "fastapi-request-observability: access extra-fields callback failed: RuntimeError\n"
    )


async def test_non_mapping_extra_fields_result_is_diagnosed_and_nonfatal(capsys):
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler, extra_fields=lambda _scope: None)) as client:
        response = await client.get("/items/1")

    assert response.status_code == 200
    assert len(handler.entries) == 1
    assert capsys.readouterr().err == (
        "fastapi-request-observability: access extra-fields callback failed: AttributeError\n"
    )


async def test_partial_extra_fields_iteration_is_rolled_back_and_diagnosed(capsys):
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler, extra_fields=lambda _scope: PartiallyFailingFields())) as client:
        response = await client.get("/items/1")

    assert response.status_code == 200
    assert len(handler.entries) == 1
    assert "partial" not in handler.entries[0]
    assert capsys.readouterr().err == (
        "fastapi-request-observability: access extra-fields callback failed: RuntimeError\n"
    )


async def test_logger_failure_does_not_change_response(capsys):
    async with asgi_client(_app(FailingHandler())) as client:
        response = await client.get("/items/1")
    assert response.status_code == 200
    assert capsys.readouterr().err == "fastapi-request-observability: access log emission failed: RuntimeError\n"


async def test_diagnostic_sink_failure_does_not_change_response(monkeypatch):
    monkeypatch.setattr(
        "fastapi_request_observability.access.sys",
        SimpleNamespace(stderr=FailingStream()),
    )

    async with asgi_client(_app(FailingHandler())) as client:
        response = await client.get("/items/1")

    assert response.status_code == 200


async def test_negative_elapsed_clock_is_clamped_to_zero():
    handler = JSONCaptureHandler()
    values = iter([10.0, 9.0])

    async with asgi_client(_app(handler, clock=lambda: next(values))) as client:
        response = await client.get("/items/1")

    assert response.status_code == 200
    assert handler.entries[0]["duration_ms"] == 0


def test_duration_serializes_exact_integers_without_losing_fractional_values():
    integral = _duration_ms(lambda: 0.003, 0.0)
    fractional = _duration_ms(lambda: 0.0125, 0.0)
    assert integral == 3
    assert type(integral) is int
    assert fractional == 12.5
    assert type(fractional) is float


async def test_custom_status_level_receives_resolved_status_and_controls_level():
    handler = JSONCaptureHandler()
    statuses = []

    def status_level(status):
        statuses.append(status)
        return logging.DEBUG

    async with asgi_client(_app(handler, status_level=status_level)) as client:
        response = await client.get("/items/1")

    assert response.status_code == 200
    assert handler.entries[0]["level"] == "DEBUG"
    assert statuses == [200]


async def test_non_integer_status_level_is_diagnosed_and_uses_default_mapping(capsys):
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler, status_level=lambda _status: True)) as client:
        response = await client.get("/handled")

    assert response.status_code == 418
    assert handler.entries[0]["level"] == "WARNING"
    assert capsys.readouterr().err == (
        "fastapi-request-observability: access status-level callback failed: TypeError\n"
    )


async def test_failing_initial_clock_falls_back_without_changing_response(capsys):
    def broken_clock():
        raise RuntimeError("clock failed")

    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler, clock=broken_clock)) as client:
        response = await client.get("/items/1")

    assert response.status_code == 200
    assert handler.entries[0]["duration_ms"] >= 0
    assert handler.entries[0]["level"] == "INFO"
    assert capsys.readouterr().err == "fastapi-request-observability: access clock callback failed: RuntimeError\n"


async def test_failing_status_level_falls_back_without_changing_response(capsys):
    def broken_level(_status):
        raise RuntimeError("level failed")

    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler, status_level=broken_level)) as client:
        response = await client.get("/items/1")

    assert response.status_code == 200
    assert handler.entries[0]["level"] == "INFO"
    assert capsys.readouterr().err == (
        "fastapi-request-observability: access status-level callback failed: RuntimeError\n"
    )


@pytest.mark.parametrize("started", [float("nan"), float("inf"), float("-inf")])
async def test_nonfinite_initial_clock_value_is_diagnosed_and_replaced(started, capsys):
    handler = JSONCaptureHandler()
    async with asgi_client(_app(handler, clock=lambda: started)) as client:
        response = await client.get("/items/1")

    assert response.status_code == 200
    assert handler.entries[0]["duration_ms"] >= 0
    assert capsys.readouterr().err == ("fastapi-request-observability: access clock callback failed: ValueError\n")


async def test_clock_failure_after_start_is_diagnosed_and_clamped(capsys):
    handler = JSONCaptureHandler()
    values = iter([10.0, RuntimeError("clock stopped")])

    def clock():
        value = next(values)
        if isinstance(value, BaseException):
            raise value
        return value

    async with asgi_client(_app(handler, clock=clock)) as client:
        response = await client.get("/items/1")

    assert response.status_code == 200
    assert handler.entries[0]["duration_ms"] == 0
    assert capsys.readouterr().err == ("fastapi-request-observability: access clock callback failed: RuntimeError\n")


@pytest.mark.parametrize("finished", [float("nan"), float("inf"), float("-inf")])
async def test_nonfinite_elapsed_clock_value_is_clamped(finished):
    handler = JSONCaptureHandler()
    values = iter([10.0, finished])
    async with asgi_client(_app(handler, clock=lambda: next(values))) as client:
        response = await client.get("/items/1")

    assert response.status_code == 200
    assert handler.entries[0]["duration_ms"] == 0


async def test_gcp_http_request_and_trace_shape_omit_query_and_fake_span():
    handler = JSONCaptureHandler(LoggingPreset.GCP)
    clock_values = iter([10.0, 11.25])
    async with asgi_client(
        _app(handler, preset=LoggingPreset.GCP, clock=lambda: next(clock_values)), client=("192.0.2.4", 1)
    ) as client:
        await client.get(
            "/items/one?token=secret",
            headers={"traceparent": TRACEPARENT, "host": "example.test", "user-agent": "gcp-agent"},
        )
    entry = handler.entries[0]
    assert entry["severity"] == "INFO"
    assert entry["duration_ms"] == 1250
    assert entry["httpRequest"] == {
        "requestMethod": "GET",
        "requestUrl": "/items/one",
        "status": 200,
        "latency": "1.25s",
        "remoteIp": "192.0.2.4",
        "userAgent": "gcp-agent",
    }
    assert entry["logging.googleapis.com/trace"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert "logging.googleapis.com/spanId" not in entry


@pytest.mark.asyncio
async def test_gcp_http_request_defaults_omit_privacy_fields():
    handler = JSONCaptureHandler(LoggingPreset.GCP)
    clock_values = iter([10.0, 10.0])

    async def app(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "https",
        "path": "/health",
        "raw_path": b"/health",
        "headers": [(b"host", b"example.test")],
        "state": {},
    }
    middleware = AccessLogMiddleware(
        app,
        AccessLogConfig(
            logger=_logger(handler),
            preset=LoggingPreset.GCP,
            clock=lambda: next(clock_values),
        ),
    )
    await middleware(scope, _empty_receive, _discard)

    assert len(handler.entries) == 1
    assert handler.entries[0]["httpRequest"] == {
        "requestMethod": "GET",
        "status": 204,
        "latency": "0s",
    }
    assert "path" not in handler.entries[0]
    assert "peer_ip" not in handler.entries[0]
    assert "user_agent" not in handler.entries[0]
    assert "logging.googleapis.com/trace" not in handler.entries[0]
    assert "logging.googleapis.com/trace_sampled" not in handler.entries[0]


@pytest.mark.parametrize(
    ("capture_path", "capture_peer_ip", "capture_user_agent", "expected_field", "expected_http_field"),
    [
        (True, False, False, ("path", "/items/one"), ("requestUrl", "/items/one")),
        (False, True, False, ("peer_ip", "192.0.2.4"), ("remoteIp", "192.0.2.4")),
        (False, False, True, ("user_agent", "agent/1"), ("userAgent", "agent/1")),
    ],
)
async def test_privacy_capture_options_are_independent(
    capture_path,
    capture_peer_ip,
    capture_user_agent,
    expected_field,
    expected_http_field,
):
    handler = JSONCaptureHandler(LoggingPreset.GCP)
    app = FastAPI()
    app.add_middleware(
        AccessLogMiddleware,
        config=AccessLogConfig(
            logger=_logger(handler),
            preset=LoggingPreset.GCP,
            capture_path=capture_path,
            capture_peer_ip=capture_peer_ip,
            capture_user_agent=capture_user_agent,
        ),
    )
    app.add_middleware(RequestContextMiddleware)

    @app.get("/items/{item_id}")
    async def item(item_id: str):
        return {"item": item_id}

    async with asgi_client(
        app,
        client=("192.0.2.4", 1),
    ) as client:
        await client.get("/items/one?secret=yes", headers={"user-agent": "agent/1"})

    entry = handler.entries[0]
    assert entry[expected_field[0]] == expected_field[1]
    assert entry["httpRequest"][expected_http_field[0]] == expected_http_field[1]
    assert len({"path", "peer_ip", "user_agent"} & entry.keys()) == 1
    assert len({"requestUrl", "remoteIp", "userAgent"} & entry["httpRequest"].keys()) == 1


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ({"raw_path": b"/items/%2F%ff", "path": "/items//�"}, "/items/%2F%ff"),
        ({"path": "/items/é"}, "/items/%C3%A9"),
        ({"path": "/items/a:b@c;d=e"}, "/items/a:b@c;d=e"),
        ({"path": ""}, "/"),
    ],
)
def test_request_path_prefers_wire_encoding_and_safely_quotes_fallback(scope, expected):
    assert _request_path(scope) == expected


@pytest.mark.parametrize(
    ("duration_ms", "expected"),
    [(0.0, "0s"), (0.000001, "0.000000001s"), (1000.0, "1s"), (1250.0, "1.25s")],
)
def test_protobuf_duration_serialization_boundaries(duration_ms, expected):
    assert _protobuf_duration(duration_ms) == expected


async def _empty_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _discard(_message):
    return None
