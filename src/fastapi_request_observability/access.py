"""Structured HTTP access-log ASGI middleware."""

from __future__ import annotations

import logging
import math
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, quote_from_bytes

from fastapi.routing import APIRoute

from ._context import _bind_context, _reset_context, current_request_context
from .logging import (
    _ACCESS_FIELDS_KEY,
    _RESERVED_FIELDS,
    LoggingPreset,
    _context_fields,
)
from .middleware import (
    _MISSING,
    _SCOPE_CONTEXT_KEY,
    RequestContextConfig,
    _ASGIApp,
    _context_from_scope,
    _Message,
    _Receive,
    _restore_scope_context,
    _Scope,
    _scope_request_context,
    _Send,
    _set_header,
    _set_request_state,
)

StatusLevel = Callable[[int], int]
ExtraFields = Callable[[_Scope], Mapping[str, Any]]
Clock = Callable[[], float]
_CLIENT_ERROR_STATUS = 400
_SERVER_ERROR_STATUS = 500


@dataclass(frozen=True, slots=True)
class AccessLogConfig:
    """Configure access-record emission."""

    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("fastapi_request_observability.access"))
    preset: LoggingPreset = LoggingPreset.DEFAULT
    clock: Clock = time.perf_counter
    status_level: StatusLevel | None = None
    extra_fields: ExtraFields | None = None
    message: str = "request completed"


class AccessLogMiddleware:
    """Emit exactly one access record after an HTTP response completes."""

    def __init__(self, app: _ASGIApp, config: AccessLogConfig | None = None) -> None:
        """Initialize the middleware around an ASGI application."""
        self.app = app
        self.config = config or AccessLogConfig()

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        """Observe one ASGI request and emit its access record."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        previous_scope_context = scope.get(_SCOPE_CONTEXT_KEY, _MISSING)
        context = _scope_request_context(scope)
        created_context = context is None
        if created_context:
            request_config = RequestContextConfig()
            context = _context_from_scope(scope, request_config)
            scope[_SCOPE_CONTEXT_KEY] = context

        token = None
        if current_request_context() is not context:
            token = _bind_context(context)
        _set_request_state(scope, context.request_id)

        clock, start = _start_clock(self.config.clock)
        status: int | None = None
        emitted = False

        def emit(error: BaseException | None = None) -> None:
            nonlocal emitted
            if emitted:
                return
            emitted = True
            resolved_status = status if status is not None else (500 if error is not None else 200)
            duration_ms = _duration_ms(clock, start)
            fields = _access_fields(scope, resolved_status, duration_ms, self.config.preset)
            fields.update(_context_fields(self.config.preset, context))
            if error is not None:
                fields["error"] = _exception_summary(error)
            if self.config.extra_fields is not None:
                try:
                    custom_fields = self.config.extra_fields(scope)
                    fields.update({key: value for key, value in custom_fields.items() if key not in _RESERVED_FIELDS})
                except Exception as callback_error:  # noqa: BLE001 - application callbacks are untrusted
                    _diagnostic("access extra-fields callback failed", callback_error)
            try:
                self.config.logger.log(
                    _status_level(self.config, resolved_status),
                    self.config.message,
                    extra={_ACCESS_FIELDS_KEY: fields},
                )
            except Exception as logging_error:  # noqa: BLE001 - logging must never alter the response
                _diagnostic("access log emission failed", logging_error)

        async def send_with_observation(message: _Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start" and created_context:
                _set_header(message, "X-Request-ID", context.request_id)
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                emit()

        try:
            await self.app(scope, receive, send_with_observation)
            if not emitted:
                emit()
        except BaseException as error:
            emit(error)
            raise
        finally:
            if token is not None:
                _reset_context(token)
            if created_context:
                _restore_scope_context(scope, previous_scope_context, context)


def _status_level(config: AccessLogConfig, status: int) -> int:
    if config.status_level is not None:
        try:
            level = config.status_level(status)
        except Exception as error:  # noqa: BLE001 - application callbacks are untrusted
            _diagnostic("access status-level callback failed", error)
        else:
            if isinstance(level, int) and not isinstance(level, bool):
                return level
            _diagnostic(
                "access status-level callback failed",
                TypeError("status-level callback must return an integer logging level"),
            )
    if status >= _SERVER_ERROR_STATUS:
        return logging.ERROR
    if status >= _CLIENT_ERROR_STATUS:
        return logging.WARNING
    return logging.INFO


def _start_clock(clock: Clock) -> tuple[Clock, float]:
    try:
        started = float(clock())
    except Exception as error:  # noqa: BLE001 - application callbacks are untrusted
        _diagnostic("access clock callback failed", error)
    else:
        if math.isfinite(started):
            return clock, started
        _diagnostic("access clock callback failed", ValueError("clock must return a finite number"))
    return time.perf_counter, time.perf_counter()


def _duration_ms(clock: Clock, started: float) -> float:
    try:
        duration = (float(clock()) - started) * 1000
        return max(duration, 0.0) if math.isfinite(duration) else 0.0
    except Exception as error:  # noqa: BLE001 - application callbacks are untrusted
        _diagnostic("access clock callback failed", error)
        return 0.0


def _access_fields(scope: _Scope, status: int, duration_ms: float, preset: LoggingPreset) -> dict[str, Any]:
    path = _request_path(scope)
    fields: dict[str, Any] = {
        "method": scope.get("method", ""),
        "path": path,
        "status": status,
        "duration_ms": duration_ms,
    }

    route = scope.get("route")
    if route is not None:
        route_path = getattr(route, "path", None)
        if route_path:
            fields["path_template"] = route_path
        if isinstance(route, APIRoute) and route.operation_id:
            fields["operation_id"] = route.operation_id

    client = scope.get("client")
    if client:
        fields["remote_ip"] = client[0]
    user_agent = _first_header(scope, "user-agent")
    if user_agent:
        fields["user_agent"] = user_agent

    if preset is LoggingPreset.GCP:
        http_request: dict[str, Any] = {
            "requestMethod": fields["method"],
            "requestUrl": _request_url(scope, path),
            "status": status,
            "latency": _protobuf_duration(duration_ms),
        }
        if client:
            http_request["remoteIp"] = client[0]
        if user_agent:
            http_request["userAgent"] = user_agent
        fields["httpRequest"] = http_request
    return fields


def _request_path(scope: _Scope) -> str:
    raw_path = scope.get("raw_path")
    if raw_path:
        return quote_from_bytes(raw_path, safe="/%:@-._~!$&'()*+,;=")
    return quote(scope.get("path") or "/", safe="/:@-._~!$&'()*+,;=")


def _request_url(scope: _Scope, path: str) -> str:
    host = _first_header(scope, "host")
    if not host:
        server = scope.get("server")
        if server:
            host = f"{server[0]}:{server[1]}"
    return f"{scope.get('scheme', 'http')}://{host}{path}" if host else path


def _first_header(scope: _Scope, name: str) -> str | None:
    target = name.encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == target:
            return value.decode("latin-1")
    return None


def _protobuf_duration(duration_ms: float) -> str:
    nanoseconds = max(round(duration_ms * 1_000_000), 0)
    seconds, nanos = divmod(nanoseconds, 1_000_000_000)
    if nanos == 0:
        return f"{seconds}s"
    return f"{seconds}.{nanos:09d}".rstrip("0") + "s"


def _diagnostic(message: str, error: Exception) -> None:
    with suppress(Exception):
        sys.stderr.write(f"fastapi-request-observability: {message}: {type(error).__name__}\n")


def _exception_summary(error: BaseException) -> str:
    with suppress(Exception):
        message = str(error)
        return f"{type(error).__name__}: {message}" if message else type(error).__name__
    return type(error).__name__
