import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from fastapi_request_observability import (
    JSONFormatter,
    LoggingPreset,
    TraceContextLevel,
)
from fastapi_request_observability._context import RequestContext, _bind_context, _reset_context
from fastapi_request_observability.trace import parse_traceparent

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"
PROVIDER_FIELDS = {
    "logging.googleapis.com/trace",
    "logging.googleapis.com/trace_sampled",
    "xray_trace_id",
    "operation_Id",
    "operation_ParentId",
}


class UnsupportedValue:
    pass


def test_standard_stream_handler_writes_each_event_as_one_lf_terminated_ndjson_object():
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("test.ndjson")
    previous_handlers = list(logger.handlers)
    previous_level = logger.level
    previous_propagate = logger.propagate
    try:
        logger.handlers[:] = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.info("first ✓\nlogical message")
        logger.error("second message")
    finally:
        logger.handlers[:] = previous_handlers
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    raw = output.getvalue()
    assert raw.endswith("\n")
    assert "\r" not in raw
    lines = raw.removesuffix("\n").split("\n")
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert all(isinstance(record, dict) for record in records)
    assert [record["message"] for record in records] == ["first ✓\nlogical message", "second message"]


def test_concurrent_stream_handler_writes_are_complete_and_unique():
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("test.concurrent-ndjson")
    previous_handlers = list(logger.handlers)
    previous_level = logger.level
    previous_propagate = logger.propagate
    try:
        logger.handlers[:] = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(
                executor.map(
                    lambda index: logger.info("concurrent", extra={"record_id": f"record-{index}"}),
                    range(200),
                )
            )
    finally:
        logger.handlers[:] = previous_handlers
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    lines = output.getvalue().splitlines()
    assert len(lines) == 200
    records = [json.loads(line) for line in lines]
    assert {record["record_id"] for record in records} == {f"record-{index}" for index in range(200)}
    assert {record["message"] for record in records} == {"concurrent"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        assert key not in result, f"duplicate raw JSON key: {key}"
        result[key] = value
    return result


def _record(level=logging.INFO, *, name="test.logger", message="hello %s", args=("world",), **extra):
    record = logging.LogRecord(name, level, "app.py", 42, message, args, None, "handler")
    record.__dict__.update(extra)
    return record


def test_compact_json_base_fields_and_source():
    output = JSONFormatter(include_source=True).format(_record())
    parsed = json.loads(output)
    assert output == json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "test.logger"
    assert parsed["message"] == "hello world"
    assert parsed["timestamp"].endswith("Z")
    assert parsed["source"] == {"file": "app.py", "line": 42, "function": "handler"}


def test_default_formatter_omits_opt_in_source_field():
    assert "source" not in json.loads(JSONFormatter().format(_record()))


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL])
def test_level_names(level):
    assert json.loads(JSONFormatter().format(_record(level)))["level"] == logging.getLevelName(level)


@pytest.mark.parametrize(
    ("level", "severity"),
    [
        (-5, "DEBUG"),
        (logging.DEBUG, "DEBUG"),
        (logging.INFO, "INFO"),
        (logging.WARNING, "WARNING"),
        (35, "WARNING"),
        (logging.ERROR, "ERROR"),
        (logging.CRITICAL, "CRITICAL"),
        (100, "CRITICAL"),
    ],
)
def test_gcp_levels_are_folded_into_the_portable_severity_vocabulary(level, severity):
    parsed = json.loads(JSONFormatter(LoggingPreset.GCP).format(_record(level)))
    assert parsed["severity"] == severity
    assert "level" not in parsed


def test_structured_extras_and_deterministic_unsupported_fallback():
    circular = []
    circular.append(circular)
    circular_mapping = {}
    circular_mapping["self"] = circular_mapping
    parsed = json.loads(
        JSONFormatter().format(
            _record(
                structured={"items": [1, True]},
                unsupported=UnsupportedValue(),
                circular=circular,
                circular_mapping=circular_mapping,
                not_a_number=float("nan"),
                infinity=float("inf"),
            )
        )
    )
    unsupported_type = f"{UnsupportedValue.__module__}.{UnsupportedValue.__qualname__}"
    assert parsed["structured"] == {"items": [1, True]}
    assert parsed["unsupported"] == f"<unsupported:{unsupported_type}>"
    assert parsed["circular"] == ["<circular>"]
    assert parsed["circular_mapping"] == {"self": "<circular>"}
    assert parsed["not_a_number"] == "<unsupported:builtins.float>"
    assert parsed["infinity"] == "<unsupported:builtins.float>"


def test_json_serializable_non_string_mapping_keys_are_preserved():
    custom_key = UnsupportedValue()
    parsed = json.loads(
        JSONFormatter().format(
            _record(
                structured={
                    1: "one",
                    2: "two",
                    1.5: "finite-float",
                    float("nan"): "nonfinite-float",
                    custom_key: "custom",
                }
            )
        )
    )
    unsupported_type = f"{UnsupportedValue.__module__}.{UnsupportedValue.__qualname__}"
    assert parsed["structured"]["1"] == "one"
    assert parsed["structured"]["2"] == "two"
    assert parsed["structured"]["1.5"] == "finite-float"
    assert parsed["structured"]["<key:builtins.float>"] == "nonfinite-float"
    assert parsed["structured"][f"<key:{unsupported_type}>"] == "custom"


def test_mapping_keys_are_normalized_before_encoding_without_raw_duplicates():
    output = JSONFormatter().format(_record(structured={1: {2: "first", "2": "nested-conflict"}, "1": "top-conflict"}))

    parsed = json.loads(output, object_pairs_hook=_unique_object)
    assert parsed["structured"] == {"1": {"2": "first"}}


def test_application_extra_fields_use_contextual_exact_ownership():
    record = _record()
    spoofed_fields = {
        "timestamp": "spoofed",
        "level": "spoofed",
        "severity": "spoofed",
        "logger": "spoofed",
        "message": "spoofed",
        "stacktrace": "spoofed",
        "request_id": "spoofed",
        "correlation_id": "spoofed",
        "trace_id": "spoofed",
        "parent_id": "spoofed",
        "trace_flags": "spoofed",
        "trace_sampled": "spoofed",
        "trace_id_random": "spoofed",
        "logging.googleapis.com/trace": "spoofed",
        "logging.googleapis.com/trace_sampled": "spoofed",
        "logging.googleapis.com/spanId": "spoofed",
        "xray_trace_id": "spoofed",
        "operation_Id": "spoofed",
        "operation_ParentId": "spoofed",
        "method": "spoofed",
        "path": "spoofed",
        "path_template": "spoofed",
        "operation_id": "spoofed",
        "status": "spoofed",
        "duration_ms": "spoofed",
        "terminal_reason": "spoofed",
        "peer_ip": "spoofed",
        "remote_ip": "spoofed",
        "user_agent": "spoofed",
        "error": "spoofed",
        "httpRequest": "spoofed",
        "source": "spoofed",
        "logging.googleapis.com/future": "spoofed",
        "logging.googleapis.com/labels": {"component": "worker"},
        "obs.internal": "spoofed",
        "_obs_internal": "spoofed",
        "_fastapi_request_observability_access_fields": {"trusted_injection": "spoofed"},
    }
    record.__dict__.update(spoofed_fields)

    parsed = json.loads(JSONFormatter().format(record))

    assert parsed["timestamp"] != "spoofed"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "test.logger"
    assert parsed["message"] == "hello world"
    protected = {
        "timestamp",
        "level",
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
    assert not (protected - {"timestamp", "level", "logger", "message"}) & parsed.keys()
    allowed = spoofed_fields.keys() - protected
    assert all(
        parsed[key] == "spoofed"
        for key in allowed
        if key not in {"_fastapi_request_observability_access_fields", "logging.googleapis.com/labels"}
    )
    assert parsed["logging.googleapis.com/labels"] == {"component": "worker"}
    assert "trusted_injection" not in parsed


@pytest.mark.parametrize(
    ("preset", "owned"),
    [
        (LoggingPreset.DEFAULT, {"level": "INFO"}),
        (
            LoggingPreset.GCP,
            {
                "severity": "INFO",
                "logging.googleapis.com/trace": TRACE_ID,
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
def test_active_profile_fields_override_spoofs_and_inactive_fields_are_retained(preset, owned):
    spoofed = {
        "level": "spoofed-level",
        "severity": "spoofed-severity",
        "httpRequest": {"spoofed": True},
        "logging.googleapis.com/trace": "spoofed-gcp-trace",
        "logging.googleapis.com/trace_sampled": False,
        "xray_trace_id": "spoofed-xray-trace",
        "operation_Id": "spoofed-azure-operation",
        "operation_ParentId": "spoofed-azure-parent",
    }
    trace = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert trace is not None
    record = _record()
    record.__dict__.update(spoofed)
    token = _bind_context(RequestContext(request_id="request-1", correlation_id=TRACE_ID, trace_context=trace))
    try:
        parsed = json.loads(JSONFormatter(preset).format(record))
    finally:
        _reset_context(token)

    assert {key: parsed[key] for key in spoofed} == {**spoofed, **owned}


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
                "logging.googleapis.com/trace": TRACE_ID,
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
    token = _bind_context(RequestContext(request_id="request-1", correlation_id=TRACE_ID, trace_context=trace))
    try:
        parsed = json.loads(JSONFormatter(preset).format(_record()))
    finally:
        _reset_context(token)
    assert parsed["request_id"] == "request-1"
    assert parsed["correlation_id"] == TRACE_ID
    assert parsed["trace_id"] == TRACE_ID
    assert parsed["parent_id"] == PARENT_ID
    assert parsed["trace_flags"] == "01"
    assert parsed["trace_sampled"] is True
    assert all(parsed[key] == value for key, value in expected.items())
    assert {key: parsed[key] for key in PROVIDER_FIELDS if key in parsed} == {
        key: value for key, value in expected.items() if key in PROVIDER_FIELDS
    }
    assert "logging.googleapis.com/spanId" not in parsed
    if preset is LoggingPreset.GCP:
        assert "level" not in parsed
    else:
        assert "severity" not in parsed


def test_level_2_context_projects_random_flag_and_level_1_omits_it():
    level_1 = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-03")
    level_2 = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-03", TraceContextLevel.LEVEL_2)
    assert level_1 is not None
    assert level_2 is not None
    for trace, expected in ((level_1, None), (level_2, True)):
        token = _bind_context(RequestContext(request_id="request-1", correlation_id=TRACE_ID, trace_context=trace))
        try:
            parsed = json.loads(JSONFormatter().format(_record()))
        finally:
            _reset_context(token)
        if expected is None:
            assert "trace_id_random" not in parsed
        else:
            assert parsed["trace_id_random"] is expected


@pytest.mark.parametrize("preset", list(LoggingPreset))
def test_no_context_has_no_correlation_or_provider_fields(preset):
    parsed = json.loads(JSONFormatter(preset).format(_record()))
    assert (
        not {
            "request_id",
            "correlation_id",
            "trace_id",
            "parent_id",
            "trace_flags",
            "trace_sampled",
            "trace_id_random",
            *PROVIDER_FIELDS,
        }
        & parsed.keys()
    )
