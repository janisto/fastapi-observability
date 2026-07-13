import asyncio

import pytest

from fastapi_request_observability import correlation_id, current_request_context, request_id, trace_context
from fastapi_request_observability._context import (
    _bind_context,
    _build_context,
    _default_request_id,
    _default_validate_request_id,
    _new_valid_request_id,
    _reset_context,
)

TRACEPARENT = b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.mark.parametrize("character", "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
def test_default_request_id_accepts_unreserved_ascii(character):
    assert _default_validate_request_id(character)


@pytest.mark.parametrize("value", ["", "x" * 129, "space value", "line\nfeed", "é", "/", "?"])
def test_default_request_id_rejects_unsafe_values(value):
    assert not _default_validate_request_id(value)


def test_default_request_id_accepts_128_character_boundary():
    assert _default_validate_request_id("x" * 128)


def test_context_builds_trace_correlation_and_tracestate():
    context = _build_context(
        [
            (b"x-request-id", b"request-1"),
            (b"traceparent", TRACEPARENT),
            (b"tracestate", b"one=1"),
            (b"tracestate", b"two=2"),
        ],
        request_id_header="X-Request-ID",
        traceparent_header="traceparent",
        tracestate_header="tracestate",
        generator=lambda: "generated",
        validator=_default_validate_request_id,
    )
    assert context.request_id == "request-1"
    assert context.correlation_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert context.trace_context is not None
    assert context.trace_context.tracestate == "one=1,two=2"


@pytest.mark.parametrize(
    "headers",
    [
        [(b"x-request-id", b"one"), (b"x-request-id", b"two")],
        [(b"x-request-id", b"bad value")],
        [(b"x-request-id", b"")],
    ],
)
def test_invalid_or_duplicate_request_ids_are_replaced(headers):
    context = _build_context(
        headers,
        request_id_header="X-Request-ID",
        traceparent_header="traceparent",
        tracestate_header="tracestate",
        generator=lambda: "generated",
        validator=_default_validate_request_id,
    )
    assert context.request_id == "generated"


def test_duplicate_traceparent_is_invalid():
    context = _build_context(
        [(b"traceparent", TRACEPARENT), (b"traceparent", TRACEPARENT)],
        request_id_header="X-Request-ID",
        traceparent_header="traceparent",
        tracestate_header="tracestate",
        generator=lambda: "request-2",
        validator=_default_validate_request_id,
    )
    assert context.trace_context is None
    assert context.correlation_id == "request-2"


def test_generator_is_retried_then_safe_fallback_is_used():
    calls = 0

    def invalid_generator():
        nonlocal calls
        calls += 1
        return "invalid value"

    generated = _new_valid_request_id(invalid_generator, _default_validate_request_id)
    assert calls == 2
    assert len(generated) == 32
    assert _default_validate_request_id(generated)

    assert _new_valid_request_id(lambda: "rejected", lambda _value: False) != "rejected"


def test_custom_generator_exception_uses_fallback():
    def broken_generator():
        raise RuntimeError("broken")

    assert len(_new_valid_request_id(broken_generator, _default_validate_request_id)) == 32


def test_entropy_or_validator_exception_uses_safe_fallback(monkeypatch):
    def broken_entropy(_size):
        raise OSError("entropy unavailable")

    def broken_validator(_value):
        raise RuntimeError("validator failed")

    monkeypatch.setattr("fastapi_request_observability._context.secrets.token_hex", broken_entropy)
    assert len(_default_request_id()) == 32
    assert len(_new_valid_request_id(lambda: "candidate", broken_validator)) == 32


def test_custom_validator_cannot_admit_unsafe_header_bytes():
    context = _build_context(
        [(b"x-request-id", "é".encode())],
        request_id_header="X-Request-ID",
        traceparent_header="traceparent",
        tracestate_header="tracestate",
        generator=lambda: "safe-id",
        validator=lambda _value: True,
    )
    assert context.request_id == "safe-id"


def test_accessors_outside_context_are_none():
    assert request_id() is None
    assert correlation_id() is None
    assert trace_context() is None
    assert current_request_context() is None


@pytest.mark.asyncio
async def test_contextvars_isolate_concurrent_tasks():
    async def worker(value):
        context = _build_context(
            [(b"x-request-id", value.encode())],
            request_id_header="X-Request-ID",
            traceparent_header="traceparent",
            tracestate_header="tracestate",
            generator=lambda: "fallback",
            validator=_default_validate_request_id,
        )
        token = _bind_context(context)
        try:
            await asyncio.sleep(0)
            return request_id()
        finally:
            _reset_context(token)

    assert await asyncio.gather(worker("one"), worker("two")) == ["one", "two"]
    assert request_id() is None
