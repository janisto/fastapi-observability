import asyncio

import pytest

from fastapi_request_observability import (
    TraceContextLevel,
    correlation_id,
    current_request_context,
    request_id,
    trace_context,
)
from fastapi_request_observability._context import (
    _bind_context,
    _build_context,
    _default_request_id,
    _default_validate_request_id,
    _new_valid_request_id,
    _reset_context,
)

TRACEPARENT = b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _assert_default_generated_request_id(value):
    assert len(value) == 32, f"expected a 32-character default ID, got {value!r}"
    assert _default_validate_request_id(value), f"expected a header-safe default ID, got {value!r}"


def test_default_request_id_accepts_every_unreserved_ascii_character():
    for character in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~":
        assert _default_validate_request_id(character), f"expected {character!r} to be accepted"


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


def test_context_builds_explicit_level_2_trace_projection():
    context = _build_context(
        [(b"traceparent", TRACEPARENT[:-2] + b"03")],
        request_id_header="X-Request-ID",
        traceparent_header="traceparent",
        tracestate_header="tracestate",
        generator=lambda: "generated",
        validator=_default_validate_request_id,
        trace_context_level=TraceContextLevel.LEVEL_2,
    )
    assert context.trace_context is not None
    assert context.trace_context.trace_context_level is TraceContextLevel.LEVEL_2
    assert context.trace_context.trace_id_random is True


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


def test_invalid_generator_is_retried_then_safe_fallback_is_used():
    calls = 0

    def invalid_generator():
        nonlocal calls
        calls += 1
        return "invalid value"

    generated = _new_valid_request_id(invalid_generator)
    assert calls == 2
    _assert_default_generated_request_id(generated)


def test_custom_validator_rejects_only_caller_input_not_generated_candidates():
    context = _build_context(
        [(b"x-request-id", b"caller")],
        request_id_header="X-Request-ID",
        traceparent_header="traceparent",
        tracestate_header="tracestate",
        generator=lambda: "generated",
        validator=lambda _value: False,
    )

    assert context.request_id == "generated"


def test_generator_exception_is_retried_before_falling_back():
    attempts = iter([RuntimeError("temporary failure"), "recovered"])

    def flaky_generator():
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    assert _new_valid_request_id(flaky_generator) == "recovered"


def test_custom_generator_exception_uses_fallback():
    def broken_generator():
        raise RuntimeError("broken")

    _assert_default_generated_request_id(_new_valid_request_id(broken_generator))


@pytest.mark.parametrize("candidate", [None, 42, b"bytes"])
def test_non_string_generator_results_are_retried_then_replaced(candidate):
    calls = 0

    def generator():
        nonlocal calls
        calls += 1
        return candidate

    generated = _new_valid_request_id(generator)

    assert calls == 2
    _assert_default_generated_request_id(generated)


def test_entropy_failure_uses_unique_safe_emergency_ids(monkeypatch):
    def broken_entropy(_size):
        raise OSError("entropy unavailable")

    monkeypatch.setattr("fastapi_request_observability._context.secrets.token_hex", broken_entropy)
    monkeypatch.setattr("fastapi_request_observability._context.time.time_ns", lambda: 1)
    monkeypatch.setattr("fastapi_request_observability._context.os.getpid", lambda: 2)
    first = _default_request_id()
    second = _default_request_id()

    assert first != second
    _assert_default_generated_request_id(first)
    _assert_default_generated_request_id(second)


def test_validator_exception_rejects_caller_and_uses_configured_generator():
    def broken_validator(_value):
        raise RuntimeError("validator failed")

    context = _build_context(
        [(b"x-request-id", b"caller")],
        request_id_header="X-Request-ID",
        traceparent_header="traceparent",
        tracestate_header="tracestate",
        generator=lambda: "generated",
        validator=broken_validator,
    )

    assert context.request_id == "generated"


def test_invalid_package_fallback_is_replaced_by_last_resort_safe_id(monkeypatch):
    monkeypatch.setattr("fastapi_request_observability._context._default_request_id", lambda: "bad value")

    generated = _new_valid_request_id(lambda: "also invalid")

    assert generated not in {"bad value", "also invalid"}
    _assert_default_generated_request_id(generated)


@pytest.mark.parametrize("value", ["", " ", "\x7f", "é"])
def test_custom_validator_cannot_admit_values_outside_the_native_header_boundary(value):
    context = _build_context(
        [(b"x-request-id", value.encode("latin-1"))],
        request_id_header="X-Request-ID",
        traceparent_header="traceparent",
        tracestate_header="tracestate",
        generator=lambda: "safe-id",
        validator=lambda _value: True,
    )
    assert context.request_id == "safe-id"


@pytest.mark.parametrize("value", ["!", "id:42", "x" * 129])
def test_custom_validator_can_broaden_visible_ascii_caller_ids(value):
    context = _build_context(
        [(b"x-request-id", value.encode("ascii"))],
        request_id_header="X-Request-ID",
        traceparent_header="traceparent",
        tracestate_header="tracestate",
        generator=lambda: "safe-id",
        validator=lambda _value: True,
    )
    assert context.request_id == value


@pytest.mark.parametrize("value", ["A", "~"])
def test_custom_validator_can_admit_baseline_boundaries(value):
    context = _build_context(
        [(b"x-request-id", value.encode())],
        request_id_header="X-Request-ID",
        traceparent_header="traceparent",
        tracestate_header="tracestate",
        generator=lambda: "fallback",
        validator=lambda _value: True,
    )
    assert context.request_id == value


def test_empty_custom_generated_value_uses_safe_fallback():
    context = _build_context(
        [],
        request_id_header="X-Request-ID",
        traceparent_header="traceparent",
        tracestate_header="tracestate",
        generator=lambda: "",
        validator=lambda _value: True,
    )
    _assert_default_generated_request_id(context.request_id)


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
