"""Structured JSON formatting for standard-library logging."""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from ._context import RequestContext, current_request_context


class LoggingPreset(str, Enum):
    """Provider-compatible JSON field presets."""

    DEFAULT = "default"
    GCP = "gcp"
    AWS = "aws"
    AZURE = "azure"


_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)
_ACCESS_FIELDS_KEY = "_fastapi_request_observability_access_fields"
_RESERVED_FIELDS = frozenset(
    {
        _ACCESS_FIELDS_KEY,
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
        "remote_ip",
        "user_agent",
        "error",
        "httpRequest",
        "source",
    }
)


class JSONFormatter(logging.Formatter):
    """Format compact JSON and inject the current request context."""

    def __init__(
        self,
        preset: LoggingPreset = LoggingPreset.DEFAULT,
        *,
        include_source: bool = False,
        gcp_project_id: str | None = None,
    ) -> None:
        super().__init__()
        self.preset = preset
        self.include_source = include_source
        self.gcp_project_id = _validated_gcp_project_id(gcp_project_id)

    def format(self, record: logging.LogRecord) -> str:
        """Return one compact, valid JSON object."""
        level_field = "severity" if self.preset is LoggingPreset.GCP else "level"
        data: dict[str, Any] = {
            "timestamp": _timestamp(record.created),
            level_field: record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if self.include_source:
            data["source"] = {"file": record.pathname, "line": record.lineno, "function": record.funcName}

        data.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _STANDARD_RECORD_FIELDS and key not in _RESERVED_FIELDS and not key.startswith("_")
            }
        )

        context = current_request_context()
        if context is not None:
            data.update(_context_fields(self.preset, context, self.gcp_project_id))

        # Access records snapshot their request context at emission time. Apply
        # that trusted snapshot after the formatter's live context so deferred
        # formatting cannot relabel a completed request as a different one.
        access_fields = record.__dict__.get(_ACCESS_FIELDS_KEY)
        if isinstance(access_fields, dict):
            data.update(access_fields)

        if record.exc_info:
            data["stacktrace"] = self.formatException(record.exc_info)

        return json.dumps(_json_safe(data), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _timestamp(created: float) -> str:
    return datetime.fromtimestamp(created, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _context_fields(
    preset: LoggingPreset,
    context: RequestContext,
    gcp_project_id: str | None = None,
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
    if preset is LoggingPreset.GCP and gcp_project_id is not None:
        fields["logging.googleapis.com/trace"] = f"projects/{gcp_project_id}/traces/{trace.trace_id}"
        fields["logging.googleapis.com/trace_sampled"] = trace.sampled
    elif preset is LoggingPreset.AWS:
        fields["xray_trace_id"] = f"1-{trace.trace_id[:8]}-{trace.trace_id[8:]}"
    elif preset is LoggingPreset.AZURE:
        fields["operation_Id"] = trace.trace_id
        fields["operation_ParentId"] = trace.parent_id
    return fields


def _validated_gcp_project_id(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not value
        or not value.isascii()
        or "/" in value
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        raise ValueError("gcp_project_id must be a non-empty visible ASCII project ID without slashes")
    return value


def _json_safe(value: Any, seen: set[int] | None = None) -> Any:
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
            return {_json_key(key): _json_safe(item, seen) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(item, seen) for item in value]
        return f"<unsupported:{type(value).__module__}.{type(value).__qualname__}>"
    finally:
        seen.remove(identity)


def _json_key(key: Any) -> Any:
    if key is None or isinstance(key, (str, bool, int)):
        return key
    if isinstance(key, float) and math.isfinite(key):
        return key
    return f"<key:{type(key).__module__}.{type(key).__qualname__}>"
