import json
import logging

import pytest

from fastapi_request_observability import JSONFormatter, LoggingPreset
from fastapi_request_observability._context import RequestContext, _bind_context, _reset_context
from fastapi_request_observability.trace import parse_traceparent

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"


def _record(level=logging.INFO, *, name="test.logger", message="hello %s", args=("world",), **extra):
    record = logging.LogRecord(name, level, "app.py", 42, message, args, None, "handler")
    record.__dict__.update(extra)
    return record


def test_compact_json_base_fields_and_source():
    output = JSONFormatter(include_source=True).format(_record())
    assert "\n" not in output
    parsed = json.loads(output)
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "test.logger"
    assert parsed["message"] == "hello world"
    assert parsed["timestamp"].endswith("Z")
    assert parsed["source"] == {"file": "app.py", "line": 42, "function": "handler"}


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL])
def test_level_names(level):
    assert json.loads(JSONFormatter().format(_record(level)))["level"] == logging.getLevelName(level)


def test_structured_extras_and_deterministic_unsupported_fallback():
    circular = []
    circular.append(circular)
    parsed = json.loads(
        JSONFormatter().format(_record(structured={"items": [1, True]}, unsupported=object(), circular=circular))
    )
    assert parsed["structured"] == {"items": [1, True]}
    assert parsed["unsupported"] == "<unsupported:builtins.object>"
    assert parsed["circular"] == ["<circular>"]


def test_json_serializable_non_string_mapping_keys_are_preserved():
    parsed = json.loads(JSONFormatter().format(_record(structured={1: "one", 2: "two", float("nan"): "bad"})))
    assert parsed["structured"]["1"] == "one"
    assert parsed["structured"]["2"] == "two"
    assert parsed["structured"]["<key:builtins.float>"] == "bad"


def test_reserved_extra_fields_are_ignored():
    parsed = json.loads(JSONFormatter().format(_record(timestamp="bad", method="bad", severity="bad")))
    assert parsed["timestamp"] != "bad"
    assert "method" not in parsed
    assert "severity" not in parsed


def test_exception_stacktrace():
    def fail():
        raise ValueError("broken")

    record = _record()
    try:
        fail()
    except ValueError:
        record.exc_info = __import__("sys").exc_info()
    parsed = json.loads(JSONFormatter().format(record))
    assert "ValueError: broken" in parsed["stacktrace"]


@pytest.mark.parametrize(
    ("preset", "expected"),
    [
        (LoggingPreset.DEFAULT, {"level": "INFO"}),
        (
            LoggingPreset.GCP,
            {
                "severity": "INFO",
                "logging.googleapis.com/trace": f"projects/example-project/traces/{TRACE_ID}",
                "logging.googleapis.com/trace_sampled": True,
            },
        ),
        (LoggingPreset.AWS, {"level": "INFO", "xray_trace_id": f"1-{TRACE_ID[:8]}-{TRACE_ID[8:]}"}),
        (
            LoggingPreset.AZURE,
            {"level": "INFO", "operation_Id": TRACE_ID, "operation_ParentId": PARENT_ID},
        ),
    ],
)
def test_context_and_provider_shapes(preset, expected):
    trace = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert trace is not None
    token = _bind_context(RequestContext("request-1", TRACE_ID, trace))
    try:
        project_id = "example-project" if preset is LoggingPreset.GCP else None
        parsed = json.loads(JSONFormatter(preset, gcp_project_id=project_id).format(_record()))
    finally:
        _reset_context(token)
    assert parsed["request_id"] == "request-1"
    assert parsed["correlation_id"] == TRACE_ID
    assert parsed["trace_id"] == TRACE_ID
    assert parsed["parent_id"] == PARENT_ID
    assert parsed["trace_flags"] == "01"
    assert parsed["trace_sampled"] is True
    assert all(parsed[key] == value for key, value in expected.items())
    assert "logging.googleapis.com/spanId" not in parsed


def test_gcp_trace_resource_is_omitted_without_an_explicit_project_id():
    trace = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert trace is not None
    token = _bind_context(RequestContext("request-1", TRACE_ID, trace))
    try:
        parsed = json.loads(JSONFormatter(LoggingPreset.GCP).format(_record()))
    finally:
        _reset_context(token)
    assert parsed["trace_id"] == TRACE_ID
    assert "logging.googleapis.com/trace" not in parsed
    assert "logging.googleapis.com/trace_sampled" not in parsed


@pytest.mark.parametrize("project_id", ["", "bad/project", "bad project", "é"])
def test_invalid_gcp_project_id_fails_at_formatter_construction(project_id):
    with pytest.raises(ValueError, match="gcp_project_id"):
        JSONFormatter(LoggingPreset.GCP, gcp_project_id=project_id)


def test_no_context_has_no_correlation_fields():
    parsed = json.loads(JSONFormatter().format(_record()))
    assert "request_id" not in parsed
    assert "trace_id" not in parsed
