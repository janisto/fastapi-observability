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


class GcpProfileVersion(StrEnum):
    """Specification-defined Google Cloud structured-stdout profiles."""

    V0_1_0 = "0.1.0"


class AwsProfileVersion(StrEnum):
    """Specification-defined AWS structured-stdout profiles."""

    V0_1_0 = "0.1.0"


class AzureProfileVersion(StrEnum):
    """Specification-defined Azure structured-stdout profiles."""

    V0_1_0 = "0.1.0"


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


def _resolve_gcp_profile_version(
    preset: LoggingPreset,
    version: GcpProfileVersion | str | None,
) -> GcpProfileVersion | None:
    if preset is not LoggingPreset.GCP:
        if version is not None:
            raise ValueError("gcp_profile_version requires LoggingPreset.GCP")
        return None
    if version is None:
        return GcpProfileVersion.V0_1_0
    try:
        return GcpProfileVersion(version)
    except ValueError as error:
        raise ValueError("unsupported GCP profile version; expected 0.1.0") from error


def _resolve_provider_profile_versions(
    preset: LoggingPreset,
    gcp: GcpProfileVersion | str | None,
    aws: AwsProfileVersion | str | None,
    azure: AzureProfileVersion | str | None,
) -> tuple[GcpProfileVersion | None, AwsProfileVersion | None, AzureProfileVersion | None]:
    if preset is not LoggingPreset.GCP and gcp is not None:
        raise ValueError("gcp_profile_version requires LoggingPreset.GCP")
    if preset is not LoggingPreset.AWS and aws is not None:
        raise ValueError("aws_profile_version requires LoggingPreset.AWS")
    if preset is not LoggingPreset.AZURE and azure is not None:
        raise ValueError("azure_profile_version requires LoggingPreset.AZURE")
    try:
        resolved_gcp = GcpProfileVersion("0.1.0" if gcp is None else gcp) if preset is LoggingPreset.GCP else None
        resolved_aws = AwsProfileVersion("0.1.0" if aws is None else aws) if preset is LoggingPreset.AWS else None
        resolved_azure = (
            AzureProfileVersion("0.1.0" if azure is None else azure) if preset is LoggingPreset.AZURE else None
        )
    except ValueError as error:
        raise ValueError(f"unsupported {preset.value.upper()} profile version; expected 0.1.0") from error
    return resolved_gcp, resolved_aws, resolved_azure


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
        "trace_id_random",
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
        "terminal_reason",
        "peer_ip",
        "remote_ip",
        "user_agent",
        "error",
        "httpRequest",
        "source",
    }
)


class JSONFormatter(logging.Formatter):
    """Format one compact JSON object; a stream handler supplies the NDJSON LF."""

    def __init__(
        self,
        preset: LoggingPreset = LoggingPreset.DEFAULT,
        *,
        include_source: bool = False,
        gcp_profile_version: GcpProfileVersion | str | None = None,
        aws_profile_version: AwsProfileVersion | str | None = None,
        azure_profile_version: AzureProfileVersion | str | None = None,
    ) -> None:
        """Initialize formatting and provider-specific field behavior."""
        super().__init__()
        self.preset = preset
        self.include_source = include_source
        self.gcp_profile_version, self.aws_profile_version, self.azure_profile_version = (
            _resolve_provider_profile_versions(preset, gcp_profile_version, aws_profile_version, azure_profile_version)
        )

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
                if key not in _STANDARD_RECORD_FIELDS and key not in _RESERVED_FIELDS and not key.startswith("_")
            }
        )

        context = current_request_context()
        if context is not None:
            data.update(_context_fields(self.preset, context))

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
