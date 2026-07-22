"""Structured JSON formatting for standard-library logging."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, override

from ._context import RequestContext, current_request_context


class LoggingPreset(StrEnum):
    """Provider-compatible JSON field presets."""

    DEFAULT = "default"
    GCP = "gcp"
    AWS = "aws"
    AZURE = "azure"


def _gcp_severity(level: int) -> str:
    if level >= logging.CRITICAL:
        return "CRITICAL"
    if level >= logging.ERROR:
        return "ERROR"
    if level >= logging.WARNING:
        return "WARNING"
    if level >= logging.INFO:
        return "INFO"
    return "DEBUG"


_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)
_ACCESS_FIELDS_KEY = "_fastapi_request_observability_access_fields"


class _AccessFields(dict[str, Any]):
    """Identify access snapshots created by this package, not application extras."""


_APPLICATION_RESERVED_FIELDS = frozenset(
    {
        _ACCESS_FIELDS_KEY,
        "timestamp",
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
        "source",
    }
)
_ACCESS_RESERVED_FIELDS = _APPLICATION_RESERVED_FIELDS | frozenset(
    {
        "method",
        "path",
        "path_template",
        "operation_id",
        "status",
        "duration_ms",
        "terminal_reason",
        "peer_ip",
        "user_agent",
        "error",
    }
)
_PROFILE_APPLICATION_RESERVED_FIELDS: dict[LoggingPreset, frozenset[str]] = {
    LoggingPreset.DEFAULT: frozenset({"level"}),
    LoggingPreset.GCP: frozenset({"severity", "logging.googleapis.com/trace", "logging.googleapis.com/trace_sampled"}),
    LoggingPreset.AWS: frozenset({"level", "xray_trace_id"}),
    LoggingPreset.AZURE: frozenset({"level", "operation_Id", "operation_ParentId"}),
}


def _is_application_reserved_field(key: object, preset: LoggingPreset) -> bool:
    return isinstance(key, str) and (
        key in _APPLICATION_RESERVED_FIELDS or key in _PROFILE_APPLICATION_RESERVED_FIELDS[preset]
    )


def _is_access_reserved_field(key: object, preset: LoggingPreset) -> bool:
    return isinstance(key, str) and (
        key in _ACCESS_RESERVED_FIELDS
        or key in _PROFILE_APPLICATION_RESERVED_FIELDS[preset]
        or (preset is LoggingPreset.GCP and key == "httpRequest")
    )


class JSONFormatter(logging.Formatter):
    """Format one compact JSON object; a stream handler supplies the NDJSON LF."""

    def __init__(
        self,
        preset: LoggingPreset = LoggingPreset.DEFAULT,
        *,
        include_source: bool = False,
    ) -> None:
        """Initialize formatting and provider-specific field behavior."""
        super().__init__()
        self.preset = preset
        self.include_source = include_source

    @override
    def format(self, record: logging.LogRecord) -> str:
        """Return one compact JSON object without a line terminator."""
        level_field = "severity" if self.preset is LoggingPreset.GCP else "level"
        level = _gcp_severity(record.levelno) if self.preset is LoggingPreset.GCP else record.levelname
        data: dict[str, Any] = {
            "timestamp": _timestamp(record.created),
            level_field: level,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if self.include_source:
            data["source"] = {"file": record.pathname, "line": record.lineno, "function": record.funcName}

        data.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _STANDARD_RECORD_FIELDS and not _is_application_reserved_field(key, self.preset)
            }
        )

        context = current_request_context()
        if context is not None:
            data.update(_context_fields(self.preset, context))

        # Access records snapshot their request context at emission time. Apply
        # that trusted snapshot after the formatter's live context so deferred
        # formatting cannot relabel a completed request as a different one.
        access_fields = record.__dict__.get(_ACCESS_FIELDS_KEY)
        if isinstance(access_fields, _AccessFields):
            data.update(access_fields)

        if record.exc_info:
            data["stacktrace"] = self.formatException(record.exc_info)

        return json.dumps(_json_safe(data), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _timestamp(created: float) -> str:
    return datetime.fromtimestamp(created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _context_fields(
    preset: LoggingPreset,
    context: RequestContext,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "request_id": context.request_id,
        "correlation_id": context.correlation_id,
    }
    trace = context.trace_context
    if trace is None:
        return fields

    fields.update(
        {
            "trace_id": trace.trace_id,
            "parent_id": trace.parent_id,
            "trace_flags": trace.flags,
            "trace_sampled": trace.sampled,
        }
    )
    if trace.trace_id_random is not None:
        fields["trace_id_random"] = trace.trace_id_random
    if preset is LoggingPreset.GCP:
        fields["logging.googleapis.com/trace"] = trace.trace_id
        fields["logging.googleapis.com/trace_sampled"] = trace.sampled
    elif preset is LoggingPreset.AWS:
        fields["xray_trace_id"] = f"1-{trace.trace_id[:8]}-{trace.trace_id[8:]}"
    elif preset is LoggingPreset.AZURE:
        fields["operation_Id"] = trace.trace_id
        fields["operation_ParentId"] = trace.parent_id
    return fields


def _json_safe(value: Any, seen: set[int] | None = None) -> Any:  # noqa: ANN401
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else f"<unsupported:{type(value).__module__}.{type(value).__qualname__}>"

    seen = set() if seen is None else seen
    identity = id(value)
    if identity in seen:
        return "<circular>"
    seen.add(identity)
    try:
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                normalized_key = _json_key(key)
                if normalized_key not in normalized:
                    normalized[normalized_key] = _json_safe(item, seen)
            return normalized
        if isinstance(value, (list, tuple)):
            return [_json_safe(item, seen) for item in value]
        return f"<unsupported:{type(value).__module__}.{type(value).__qualname__}>"
    finally:
        seen.remove(identity)


def _json_key(key: Any) -> str:  # noqa: ANN401
    if isinstance(key, str):
        return key
    if key is None:
        return "null"
    if isinstance(key, bool):
        return "true" if key else "false"
    if isinstance(key, int):
        return str(key)
    if isinstance(key, float) and math.isfinite(key):
        return str(key)
    return f"<key:{type(key).__module__}.{type(key).__qualname__}>"
