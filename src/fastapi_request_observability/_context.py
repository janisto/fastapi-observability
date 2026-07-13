"""Private request-context storage and construction."""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from collections.abc import Callable, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import Lock

from .trace import TraceContext, _with_tracestate, parse_traceparent

RequestIDGenerator = Callable[[], str]
RequestIDValidator = Callable[[str], bool]
Header = tuple[bytes, bytes]


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Validated correlation metadata for the current HTTP request."""

    request_id: str
    correlation_id: str
    trace_context: TraceContext | None = None


_request_context: ContextVar[RequestContext | None] = ContextVar(
    "fastapi_request_observability_request_context", default=None
)
_fallback_lock = Lock()
_fallback_counter = 0


def request_id() -> str | None:
    """Return the current request ID, if middleware has bound one."""
    context = _request_context.get()
    return context.request_id if context else None


def correlation_id() -> str | None:
    """Return the current trace ID, or request ID when no trace is valid."""
    context = _request_context.get()
    return context.correlation_id if context else None


def trace_context() -> TraceContext | None:
    """Return the current validated W3C trace context, if present."""
    context = _request_context.get()
    return context.trace_context if context else None


def current_request_context() -> RequestContext | None:
    """Return all current request correlation metadata."""
    return _request_context.get()


def _bind_context(context: RequestContext) -> Token[RequestContext | None]:
    return _request_context.set(context)


def _reset_context(token: Token[RequestContext | None]) -> None:
    _request_context.reset(token)


def _default_request_id() -> str:
    try:
        return secrets.token_hex(16)
    except Exception:  # noqa: BLE001 - entropy failure still needs a header-safe correlation value
        return _emergency_request_id()


def _default_validate_request_id(value: str) -> bool:
    if not 1 <= len(value) <= 128 or not value.isascii():
        return False
    return all(character.isalnum() or character in "-._~" for character in value)


def _new_valid_request_id(generator: RequestIDGenerator, validator: RequestIDValidator) -> str:
    for _ in range(2):
        try:
            candidate = generator()
        except Exception:  # noqa: BLE001, S112 - application callback failures must not break requests
            continue
        if isinstance(candidate, str) and _is_valid(validator, candidate):
            return candidate

    fallback = _default_request_id()
    # A custom validator may reject every safe value. The final value must
    # still satisfy the package's public request-ID format.
    return fallback if _default_validate_request_id(fallback) else "0" * 32


def _is_valid(validator: RequestIDValidator, value: str) -> bool:
    if not value.isascii() or any(not 0x21 <= ord(character) <= 0x7E for character in value):
        return False
    try:
        return validator(value)
    except Exception:  # noqa: BLE001 - application callback failures are treated as invalid input
        return False


def _emergency_request_id() -> str:
    global _fallback_counter
    with _fallback_lock:
        _fallback_counter = (_fallback_counter + 1) % (1 << 128)
        material = f"{time.time_ns()}:{os.getpid()}:{_fallback_counter}".encode()
        return hashlib.sha256(material).hexdigest()[:32]


def _header_values(headers: Sequence[Header], name: str) -> list[str]:
    encoded_name = name.lower().encode("latin-1")
    return [value.decode("latin-1") for key, value in headers if key.lower() == encoded_name]


def _build_context(
    headers: Sequence[Header],
    *,
    request_id_header: str,
    traceparent_header: str,
    tracestate_header: str,
    generator: RequestIDGenerator,
    validator: RequestIDValidator,
) -> RequestContext:
    request_id_values = _header_values(headers, request_id_header)
    incoming_request_id = request_id_values[0] if len(request_id_values) == 1 else ""
    selected_request_id = (
        incoming_request_id
        if _is_valid(validator, incoming_request_id)
        else _new_valid_request_id(generator, validator)
    )

    traceparent_values = _header_values(headers, traceparent_header)
    trace = parse_traceparent(traceparent_values[0]) if len(traceparent_values) == 1 else None
    if trace is not None:
        trace = _with_tracestate(trace, _header_values(headers, tracestate_header))

    return RequestContext(
        request_id=selected_request_id,
        correlation_id=trace.trace_id if trace else selected_request_id,
        trace_context=trace,
    )
