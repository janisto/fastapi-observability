import asyncio
import re
from typing import Any

import pytest
from fastapi import FastAPI, Request

from fastapi_request_observability import (
    RequestContext,
    RequestContextConfig,
    RequestContextMiddleware,
    TraceContextLevel,
    correlation_id,
    current_request_context,
    request_id,
    trace_context,
)
from fastapi_request_observability.middleware import _SCOPE_CONTEXT_KEY
from tests._client import asgi_client

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
OTHER_TRACEPARENT = "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-00"
DEFAULT_GENERATED_REQUEST_ID = re.compile(r"[A-Za-z0-9._~-]{32}")


def test_request_context_config_resolves_trace_level_and_rejects_unsupported_values():
    assert RequestContextConfig().trace_context_level is TraceContextLevel.LEVEL_1
    assert RequestContextConfig(trace_context_level=2).trace_context_level is TraceContextLevel.LEVEL_2
    with pytest.raises(ValueError, match="unsupported trace context level"):
        RequestContextConfig(trace_context_level=3)


async def test_fastapi_request_context_state_header_and_accessors():
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/context")
    async def context(request: Request):
        return {"request_id": request_id(), "state": request.state.request_id, "correlation": correlation_id()}

    async with asgi_client(app) as client:
        response = await client.get("/context", headers={"X-Request-ID": "client-id"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "client-id"
    assert response.json() == {"request_id": "client-id", "state": "client-id", "correlation": "client-id"}
    assert request_id() is None


async def test_custom_response_header_replaces_default_header():
    app = FastAPI()
    app.add_middleware(
        RequestContextMiddleware,
        config=RequestContextConfig(response_header="X-Correlation-ID", request_id_generator=lambda: "fixed"),
    )

    @app.get("/")
    async def root():
        return {}

    async with asgi_client(app) as client:
        response = await client.get("/")
    assert response.headers["X-Correlation-ID"] == "fixed"
    assert "X-Request-ID" not in response.headers


async def test_disabled_response_header_injection_adds_no_request_id_header():
    disabled_app = FastAPI()

    @disabled_app.get("/")
    async def disabled_root():
        return {}

    disabled = RequestContextMiddleware(
        disabled_app,
        RequestContextConfig(
            response_header="X-Correlation-ID",
            request_id_generator=lambda: "disabled",
            inject_response_header=False,
        ),
    )
    async with asgi_client(disabled) as client:
        disabled_response = await client.get("/")

    assert "X-Request-ID" not in disabled_response.headers
    assert "X-Correlation-ID" not in disabled_response.headers


async def test_trace_accessors_expose_validated_context_only_during_request():
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/")
    async def root():
        trace = trace_context()
        context = current_request_context()
        assert trace is not None
        assert context is not None
        return {
            "trace_id": trace.trace_id,
            "tracestate": trace.tracestate,
            "context_trace_id": context.trace_context.trace_id if context.trace_context else None,
        }

    async with asgi_client(app) as client:
        response = await client.get("/", headers={"traceparent": TRACEPARENT, "tracestate": "vendor=value"})

    assert response.json() == {
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "tracestate": "vendor=value",
        "context_trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
    }
    assert trace_context() is None
    assert current_request_context() is None


async def test_explicit_level_2_exposes_random_flag_during_request():
    app = FastAPI()
    app.add_middleware(
        RequestContextMiddleware,
        config=RequestContextConfig(trace_context_level=TraceContextLevel.LEVEL_2),
    )

    @app.get("/")
    async def root():
        trace = trace_context()
        assert trace is not None
        return {
            "trace_context_level": trace.trace_context_level,
            "trace_sampled": trace.sampled,
            "trace_id_random": trace.trace_id_random,
        }

    async with asgi_client(app) as client:
        response = await client.get("/", headers={"traceparent": TRACEPARENT[:-2] + "03"})

    assert response.json() == {"trace_context_level": 2, "trace_sampled": True, "trace_id_random": True}


async def test_custom_trace_headers_override_standard_header_names():
    app = FastAPI()
    app.add_middleware(
        RequestContextMiddleware,
        config=RequestContextConfig(
            traceparent_header="X-Trace-Parent",
            tracestate_header="X-Trace-State",
        ),
    )

    @app.get("/")
    async def root():
        trace = trace_context()
        assert trace is not None
        return {
            "correlation_id": correlation_id(),
            "traceparent": trace.traceparent,
            "tracestate": trace.tracestate,
        }

    async with asgi_client(app) as client:
        response = await client.get(
            "/",
            headers={
                "traceparent": OTHER_TRACEPARENT,
                "tracestate": "ignored=value",
                "X-Trace-Parent": TRACEPARENT,
                "X-Trace-State": "custom=value",
            },
        )

    assert response.json() == {
        "correlation_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "traceparent": TRACEPARENT,
        "tracestate": "custom=value",
    }
    assert trace_context() is None


@pytest.mark.asyncio
async def test_response_header_replaces_duplicates_and_preserves_unrelated_headers():
    sent = []

    async def app(_scope, _receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"X-Request-ID", b"application-one"),
                    (b"x-request-id", b"application-two"),
                    (b"x-custom", b"preserved"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def capture(message):
        sent.append(message.copy())

    scope = {"type": "http", "headers": [], "state": {}}
    middleware = RequestContextMiddleware(
        app,
        RequestContextConfig(request_id_generator=lambda: "generated"),
    )
    await middleware(scope, _receive, capture)

    assert sent == [
        {
            "type": "http.response.start",
            "status": 204,
            "headers": [
                (b"content-type", b"application/json"),
                (b"x-custom", b"preserved"),
                (b"x-request-id", b"generated"),
            ],
        },
        {"type": "http.response.body", "body": b""},
    ]


async def test_invalid_custom_generator_cannot_bypass_validation():
    app = FastAPI()
    app.add_middleware(
        RequestContextMiddleware,
        config=RequestContextConfig(request_id_generator=lambda: "bad value"),
    )

    @app.get("/")
    async def root():
        return {}

    async with asgi_client(app) as client:
        value = (await client.get("/")).headers["X-Request-ID"]

    assert value != "bad value"
    assert DEFAULT_GENERATED_REQUEST_ID.fullmatch(value)


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"request_id_header": ""}, "request_id_header"),
        ({"response_header": "bad header"}, "response_header"),
        ({"traceparent_header": "trace:parent"}, "traceparent_header"),
        ({"tracestate_header": "tracéstate"}, "tracestate_header"),
    ],
)
def test_invalid_header_configuration_names_field_at_construction(kwargs, field):
    with pytest.raises(ValueError, match=rf"^{field} must be a non-empty HTTP header name$"):
        RequestContextConfig(**kwargs)


@pytest.mark.parametrize("field", ["request_id_generator", "request_id_validator"])
def test_non_callable_request_id_configuration_names_field_at_construction(field):
    kwargs: dict[str, Any] = {field: None}
    with pytest.raises(TypeError, match=rf"^{field} must be callable$"):
        RequestContextConfig(**kwargs)


@pytest.mark.asyncio
async def test_non_http_scope_passes_through_without_context():
    observed = None

    async def inner(inner_scope, inner_receive, inner_send):
        nonlocal observed
        observed = (inner_scope, inner_receive, inner_send, request_id())

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(_message):
        return None

    scope = {"type": "lifespan"}
    await RequestContextMiddleware(inner)(scope, receive, send)
    assert observed == (scope, receive, send, None)


@pytest.mark.parametrize(
    "error",
    [RuntimeError("failure"), asyncio.CancelledError()],
    ids=["exception", "cancellation"],
)
@pytest.mark.asyncio
async def test_context_resets_and_reraises_original_app_failure(error):
    async def broken(_scope, _receive, _send):
        raise error

    scope = {"type": "http", "headers": [], "state": {}}
    with pytest.raises(type(error)) as raised:
        await RequestContextMiddleware(broken)(scope, _receive, _send)

    assert raised.value is error
    assert request_id() is None


@pytest.mark.asyncio
async def test_nested_asgi_request_gets_independent_context_and_restores_parent():
    observed = {}

    async def nested_app(scope, receive, send):
        observed["nested"] = request_id()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    nested_middleware = RequestContextMiddleware(nested_app)

    async def outer_app(scope, receive, send):
        observed["outer_before"] = request_id()
        nested_scope = {"type": "http", "headers": [(b"x-request-id", b"nested")], "state": {}}
        await nested_middleware(nested_scope, _receive, _send)
        observed["outer_after"] = request_id()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    outer_scope = {"type": "http", "headers": [(b"x-request-id", b"outer")], "state": {}}
    await RequestContextMiddleware(outer_app)(outer_scope, _receive, _send)

    assert observed == {"outer_before": "outer", "nested": "nested", "outer_after": "outer"}
    assert request_id() is None
    assert "fastapi_request_observability.request_context" not in outer_scope


@pytest.mark.asyncio
async def test_existing_scope_context_is_reused_without_rebuilding_or_replacing_it():
    upstream = RequestContext("upstream-request", "upstream-correlation")
    observed = None
    sent = []

    async def app(scope, _receive, send):
        nonlocal observed
        observed = (current_request_context(), request_id(), correlation_id(), scope["state"]["request_id"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def capture(message):
        sent.append(message.copy())

    scope = {
        "type": "http",
        "headers": [(b"x-request-id", b"must-not-rebuild")],
        "state": {},
        _SCOPE_CONTEXT_KEY: upstream,
    }
    await RequestContextMiddleware(app)(scope, _receive, capture)

    assert observed == (upstream, "upstream-request", "upstream-correlation", "upstream-request")
    assert sent[0]["headers"] == [(b"x-request-id", b"upstream-request")]
    assert scope[_SCOPE_CONTEXT_KEY] is upstream
    assert current_request_context() is None


@pytest.mark.asyncio
async def test_cleanup_preserves_scope_context_replaced_by_downstream_app():
    replacement = RequestContext("downstream-request", "downstream-correlation")

    async def app(scope, _receive, send):
        assert request_id() == "incoming-request"
        scope[_SCOPE_CONTEXT_KEY] = replacement
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope = {
        "type": "http",
        "headers": [(b"x-request-id", b"incoming-request")],
        "state": {},
    }
    await RequestContextMiddleware(app)(scope, _receive, _send)

    assert scope[_SCOPE_CONTEXT_KEY] is replacement
    assert current_request_context() is None


@pytest.mark.asyncio
async def test_middleware_isolates_concurrent_requests():
    ready = asyncio.Event()
    lock = asyncio.Lock()
    arrivals = 0
    observed = {}

    async def app(scope, receive, send):
        nonlocal arrivals
        key = scope["path"]
        observed[f"{key}_before"] = request_id()
        async with lock:
            arrivals += 1
            if arrivals == 2:
                ready.set()
        await ready.wait()
        observed[f"{key}_after"] = request_id()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestContextMiddleware(app)

    async def invoke(path, value):
        scope = {"type": "http", "path": path, "headers": [(b"x-request-id", value.encode())], "state": {}}
        await middleware(scope, _receive, _send)

    await asyncio.gather(invoke("one", "request-one"), invoke("two", "request-two"))
    assert observed == {
        "one_before": "request-one",
        "one_after": "request-one",
        "two_before": "request-two",
        "two_after": "request-two",
    }


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _send(_message):
    return None
