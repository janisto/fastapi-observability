"""Request-context ASGI middleware."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

from ._context import (
    RequestContext,
    RequestIDGenerator,
    RequestIDValidator,
    _bind_context,
    _build_context,
    _default_request_id,
    _default_validate_request_id,
    _reset_context,
    current_request_context,
)

type _Scope = MutableMapping[str, Any]
type _Message = MutableMapping[str, Any]
type _Receive = Callable[[], Awaitable[_Message]]
type _Send = Callable[[_Message], Awaitable[None]]
type _ASGIApp = Callable[[_Scope, _Receive, _Send], Awaitable[None]]
_SCOPE_CONTEXT_KEY = "fastapi_request_observability.request_context"
_MISSING = object()
_HEADER_NAME_CHARACTERS = frozenset("!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


@dataclass(frozen=True, slots=True)
class RequestContextConfig:
    """Configure request-ID and trace-context extraction."""

    request_id_header: str = "X-Request-ID"
    response_header: str | None = None
    traceparent_header: str = "traceparent"
    tracestate_header: str = "tracestate"
    request_id_generator: RequestIDGenerator = _default_request_id
    request_id_validator: RequestIDValidator = _default_validate_request_id
    inject_response_header: bool = True

    def __post_init__(self) -> None:
        _validate_header_name(self.request_id_header, "request_id_header")
        _validate_header_name(self.traceparent_header, "traceparent_header")
        _validate_header_name(self.tracestate_header, "tracestate_header")
        if self.response_header is not None:
            _validate_header_name(self.response_header, "response_header")
        if not callable(self.request_id_generator):
            raise TypeError("request_id_generator must be callable")
        if not callable(self.request_id_validator):
            raise TypeError("request_id_validator must be callable")


class RequestContextMiddleware:
    """Bind validated correlation metadata to each HTTP request."""

    def __init__(self, app: _ASGIApp, config: RequestContextConfig | None = None) -> None:
        self.app = app
        self.config = config or RequestContextConfig()

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        previous_scope_context = scope.get(_SCOPE_CONTEXT_KEY, _MISSING)
        context = _scope_request_context(scope)
        created_context = context is None
        if created_context:
            context = _context_from_scope(scope, self.config)
            scope[_SCOPE_CONTEXT_KEY] = context

        token = None
        if current_request_context() is not context:
            token = _bind_context(context)
        _set_request_state(scope, context.request_id)

        async def send_with_request_id(message: _Message) -> None:
            if message["type"] == "http.response.start" and self.config.inject_response_header:
                response_header = self.config.response_header or self.config.request_id_header
                _set_header(message, response_header, context.request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            if token is not None:
                _reset_context(token)
            if created_context:
                _restore_scope_context(scope, previous_scope_context, context)


def _context_from_scope(scope: _Scope, config: RequestContextConfig) -> RequestContext:
    return _build_context(
        scope.get("headers", []),
        request_id_header=config.request_id_header,
        traceparent_header=config.traceparent_header,
        tracestate_header=config.tracestate_header,
        generator=config.request_id_generator,
        validator=config.request_id_validator,
    )


def _scope_request_context(scope: _Scope) -> RequestContext | None:
    context = scope.get(_SCOPE_CONTEXT_KEY)
    return context if isinstance(context, RequestContext) else None


def _restore_scope_context(scope: _Scope, previous: object, context: RequestContext) -> None:
    if scope.get(_SCOPE_CONTEXT_KEY) is not context:
        return
    if previous is _MISSING:
        scope.pop(_SCOPE_CONTEXT_KEY, None)
    else:
        scope[_SCOPE_CONTEXT_KEY] = previous


def _set_request_state(scope: _Scope, request_id: str) -> None:
    state = scope.setdefault("state", {})
    if isinstance(state, MutableMapping):
        state["request_id"] = request_id


def _set_header(message: _Message, name: str, value: str) -> None:
    encoded_name = name.lower().encode("latin-1")
    headers = list(message.get("headers", []))
    headers = [(key, current) for key, current in headers if key.lower() != encoded_name]
    headers.append((encoded_name, value.encode("ascii")))
    message["headers"] = headers


def _validate_header_name(value: str, field: str) -> None:
    if not value or any(character not in _HEADER_NAME_CHARACTERS for character in value):
        raise ValueError(f"{field} must be a non-empty HTTP header name")
