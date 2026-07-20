"""Structured HTTP access-log ASGI middleware."""

from __future__ import annotations

import logging
import math
import re
import sys
import time
from asyncio import CancelledError
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import Any, Literal
from urllib.parse import quote_from_bytes

from fastapi.routing import APIRoute

from ._context import _bind_context, _reset_context, current_request_context
from .logging import (
    _ACCESS_FIELDS_KEY,
    _RESERVED_FIELDS,
    GcpProfileVersion,
    LoggingPreset,
    _context_fields,
    _resolve_gcp_profile_version,
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
from .trace import TraceContextLevel, resolve_trace_context_level

StatusLevel = Callable[[int], int]
ExtraFields = Callable[[_Scope], Mapping[str, Any]]
Clock = Callable[[], float]
TerminalReason = Literal[
    "service_error",
    "body_error",
    "cancelled",
    "client_disconnect",
    "response_dropped",
    "timeout",
    "panic",
    "unknown_failure",
]
_CLIENT_ERROR_STATUS = 400
_SERVER_ERROR_STATUS = 500
_FIRST_CONTROL_CODEPOINT = 0x20
_DELETE_CODEPOINT = 0x7F
_MAX_PROTOBUF_DURATION_MILLISECONDS_EXCLUSIVE = 315_576_000_001_000
_SAFE_STATUS_LEVELS = frozenset({logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL})


@dataclass(frozen=True, slots=True, kw_only=True)
class AccessLogConfig:
    """Configure access-record emission."""

    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("fastapi_request_observability.access"))
    preset: LoggingPreset = LoggingPreset.DEFAULT
    clock: Clock = time.perf_counter
    status_level: StatusLevel | None = None
    extra_fields: ExtraFields | None = None
    gcp_profile_version: GcpProfileVersion | str | None = None
    trace_context_level: TraceContextLevel | int = TraceContextLevel.LEVEL_1
    capture_path: bool = False
    capture_peer_ip: bool = False
    capture_user_agent: bool = False
    capture_error: bool = False

    def __post_init__(self) -> None:
        """Validate and freeze effective profile and privacy settings."""
        object.__setattr__(
            self,
            "gcp_profile_version",
            _resolve_gcp_profile_version(self.preset, self.gcp_profile_version),
        )
        object.__setattr__(
            self,
            "trace_context_level",
            resolve_trace_context_level(self.trace_context_level),
        )
        for name in ("capture_path", "capture_peer_ip", "capture_user_agent", "capture_error"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")
        if not callable(self.clock):
            raise TypeError("clock must be callable")


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
            request_config = RequestContextConfig(
                trace_context_level=self.config.trace_context_level,
            )
            context = _context_from_scope(scope, request_config)
            scope[_SCOPE_CONTEXT_KEY] = context
        elif context.trace_context_level is not self.config.trace_context_level:
            raise RuntimeError("trace_context_level mismatch between RequestContextMiddleware and AccessLogMiddleware")

        token = None
        if current_request_context() is not context:
            token = _bind_context(context)
        _set_request_state(scope, context.request_id)

        clock, start = _start_clock(self.config.clock)
        status: int | None = None
        emitted = False
        trailers_pending = False

        def emit(
            error: BaseException | None = None,
            terminal_reason: TerminalReason | None = None,
        ) -> None:
            nonlocal emitted
            if emitted:
                return
            emitted = True
            duration_ms = _duration_ms(clock, start)
            fields = _access_fields(scope, status, duration_ms, self.config)
            fields.update(_context_fields(self.config.preset, context))
            if terminal_reason is not None:
                fields["terminal_reason"] = terminal_reason
            if error is not None and self.config.capture_error:
                fields["error"] = _exception_summary(error)
            if self.config.extra_fields is not None:
                try:
                    custom_fields = self.config.extra_fields(scope)
                    fields.update({key: value for key, value in custom_fields.items() if key not in _RESERVED_FIELDS})
                except Exception as callback_error:  # noqa: BLE001 - application callbacks are untrusted
                    _diagnostic("access extra-fields callback failed", callback_error)
            try:
                self.config.logger.log(
                    _status_level(self.config, status, terminal_reason),
                    "request completed",
                    extra={_ACCESS_FIELDS_KEY: fields},
                )
            except Exception as logging_error:  # noqa: BLE001 - logging must never alter the response
                _diagnostic("access log emission failed", logging_error)

        async def send_with_observation(message: _Message) -> None:
            nonlocal status, trailers_pending
            if message["type"] == "http.response.start" and created_context:
                _set_header(message, "X-Request-ID", context.request_id)
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
                trailers_pending = bool(message.get("trailers", False))
            if message["type"] == "http.response.body" and not message.get("more_body", False) and not trailers_pending:
                emit()
            if message["type"] == "http.response.trailers":
                trailers_pending = bool(message.get("more_trailers", False))
                if not trailers_pending:
                    emit()

        try:
            await self.app(scope, receive, send_with_observation)
            if not emitted:
                emit(terminal_reason="response_dropped" if status is not None else "unknown_failure")
        except BaseException as error:
            if isinstance(error, CancelledError):
                terminal_reason: TerminalReason = "cancelled"
            elif status is None:
                terminal_reason = "service_error"
            else:
                terminal_reason = "body_error"
            emit(error, terminal_reason)
            raise
        finally:
            if token is not None:
                _reset_context(token)
            if created_context:
                _restore_scope_context(scope, previous_scope_context, context)


def _status_level(
    config: AccessLogConfig,
    status: int | None,
    terminal_reason: TerminalReason | None,
) -> int:
    if terminal_reason is not None:
        return logging.ERROR
    if status is None:
        return logging.INFO
    if config.status_level is not None:
        try:
            level = config.status_level(status)
        except Exception as error:  # noqa: BLE001 - application callbacks are untrusted
            _diagnostic("access status-level callback failed", error)
        else:
            if isinstance(level, int) and not isinstance(level, bool) and level in _SAFE_STATUS_LEVELS:
                return level
            _diagnostic(
                "access status-level callback failed",
                TypeError("status-level callback must return a standard nonterminal logging level"),
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
        if not math.isfinite(duration) or duration <= 0 or duration >= _MAX_PROTOBUF_DURATION_MILLISECONDS_EXCLUSIVE:
            return 0
        return int(duration) if duration.is_integer() else duration
    except Exception as error:  # noqa: BLE001 - application callbacks are untrusted
        _diagnostic("access clock callback failed", error)
        return 0


_ROUTE_PARAMETER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ROUTE_PLACEHOLDER = re.compile(r"^\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::(?P<converter>[A-Za-z_][A-Za-z0-9_]*))?\}$")


def _canonical_route_template(native_template: str) -> str | None:
    if not native_template.startswith("/") or "?" in native_template or "#" in native_template:
        return None
    canonical: list[str] = []
    segments = native_template[1:].split("/")
    for index, segment in enumerate(segments):
        placeholder = _ROUTE_PLACEHOLDER.fullmatch(segment)
        if placeholder is not None:
            name = placeholder.group("name")
            if not _ROUTE_PARAMETER.fullmatch(name):
                return None
            catch_all = placeholder.group("converter") == "path"
            if catch_all and index != len(segments) - 1:
                return None
            prefix = "*" if catch_all else ""
            canonical.append(f"{{{prefix}{name}}}")
            continue
        if any(token in segment for token in ("{", "}", "*")):
            return None
        canonical.append(segment)
    return f"/{'/'.join(canonical)}"


def _route_fields(scope: _Scope) -> dict[str, str]:
    fields: dict[str, str] = {}
    try:
        route = scope.get("route")
        if route is not None:
            route_path = getattr(route, "path", None)
            if isinstance(route_path, str) and route_path:
                path_template = _canonical_route_template(route_path)
                if path_template is not None:
                    fields["path_template"] = path_template
            if isinstance(route, APIRoute) and _valid_metadata_string(route.operation_id):
                fields["operation_id"] = route.operation_id
    except Exception as error:  # noqa: BLE001 - framework metadata must not affect traffic
        _diagnostic("access route metadata failed", error)
        fields = {}
    return fields


def _access_fields(
    scope: _Scope,
    status: int | None,
    duration_ms: float,
    config: AccessLogConfig,
) -> dict[str, Any]:
    path = _request_path(scope) if config.capture_path else None
    fields: dict[str, Any] = {
        "method": scope.get("method", ""),
        "duration_ms": duration_ms,
    }
    if status is not None:
        fields["status"] = status
    if path is not None:
        fields["path"] = path

    fields.update(_route_fields(scope))

    client = scope.get("client") if config.capture_peer_ip else None
    peer_ip = _canonical_peer_ip(client[0]) if client else None
    if peer_ip is not None:
        fields["peer_ip"] = peer_ip
    user_agent = _single_valid_header(scope, "user-agent") if config.capture_user_agent else None
    if user_agent:
        fields["user_agent"] = user_agent

    if config.preset is LoggingPreset.GCP:
        http_request: dict[str, Any] = {
            "requestMethod": fields["method"],
            "latency": _protobuf_duration(duration_ms),
        }
        if status is not None:
            http_request["status"] = status
        if path is not None:
            http_request["requestUrl"] = path
        if peer_ip is not None:
            http_request["remoteIp"] = peer_ip
        if user_agent:
            http_request["userAgent"] = user_agent
        fields["httpRequest"] = http_request
    return fields


def _request_path(scope: _Scope) -> str | None:
    raw_path = scope.get("raw_path")
    if isinstance(raw_path, bytes) and raw_path:
        path = quote_from_bytes(raw_path, safe="/%:@-._~!$&'()*+,;=")
        if not path.startswith("/") or "?" in path or "#" in path:
            return None
        index = 0
        while (index := path.find("%", index)) != -1:
            if index + 2 >= len(path) or not all(
                character in "0123456789abcdefABCDEF" for character in path[index + 1 : index + 3]
            ):
                return None
            index += 3
        return path
    return None


def _canonical_peer_ip(value: object) -> str | None:
    if not isinstance(value, str) or not value or "%" in value:
        return None
    try:
        return str(ip_address(value))
    except ValueError:
        return None


def _valid_metadata_string(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not any(
            ord(character) < _FIRST_CONTROL_CODEPOINT or ord(character) == _DELETE_CODEPOINT for character in value
        )
    )


def _single_valid_header(scope: _Scope, name: str) -> str | None:
    target = name.encode("latin-1")
    values = [value.decode("latin-1") for key, value in scope.get("headers", []) if key.lower() == target]
    if len(values) != 1 or not _valid_metadata_string(values[0]):
        return None
    return values[0]


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
