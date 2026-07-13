import asyncio

import pytest
from fastapi import FastAPI, Request

from fastapi_request_observability import (
    RequestContextConfig,
    RequestContextMiddleware,
    correlation_id,
    request_id,
)
from tests._client import asgi_client


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


async def test_custom_and_disabled_response_headers():
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

    disabled = RequestContextMiddleware(app, RequestContextConfig(inject_response_header=False))
    async with asgi_client(disabled) as client:
        assert "X-Request-ID" not in (await client.get("/")).headers


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
    assert len(value) == 32


@pytest.mark.parametrize(
    "kwargs",
    [
        {"request_id_header": ""},
        {"response_header": "bad header"},
        {"traceparent_header": "trace:parent"},
        {"tracestate_header": "tracéstate"},
    ],
)
def test_invalid_header_configuration_fails_at_construction(kwargs):
    with pytest.raises(ValueError, match="HTTP header name"):
        RequestContextConfig(**kwargs)


@pytest.mark.parametrize("kwargs", [{"request_id_generator": None}, {"request_id_validator": None}])
def test_non_callable_request_id_configuration_fails_at_construction(kwargs):
    with pytest.raises(TypeError, match="must be callable"):
        RequestContextConfig(**kwargs)


@pytest.mark.asyncio
async def test_non_http_scope_passes_through_without_context():
    observed = None

    async def inner(scope, receive, send):
        nonlocal observed
        observed = request_id()

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(_message):
        return None

    await RequestContextMiddleware(inner)({"type": "lifespan"}, receive, send)
    assert observed is None


@pytest.mark.asyncio
async def test_context_resets_after_exception_and_cancellation():
    async def broken(scope, receive, send):
        raise RuntimeError("failure")

    scope = {"type": "http", "headers": [], "state": {}}
    with pytest.raises(RuntimeError, match="failure"):
        await RequestContextMiddleware(broken)(scope, _receive, _send)
    assert request_id() is None

    async def cancelled(scope, receive, send):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await RequestContextMiddleware(cancelled)(scope, _receive, _send)
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
