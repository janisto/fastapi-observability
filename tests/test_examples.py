import importlib
import json
import logging
from collections.abc import Iterator
from typing import Protocol, cast, override

import pytest
from fastapi import FastAPI, Response

from examples.local_wrapper import applog
from fastapi_request_observability import (
    AccessLogConfig,
    AccessLogMiddleware,
    JSONFormatter,
    LoggingPreset,
    RequestContextConfig,
    RequestContextMiddleware,
)
from tests._client import asgi_client

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"
RANDOM_TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-03"


class _ExampleModule(Protocol):
    app: FastAPI


class _CaptureHandler(logging.Handler):
    def __init__(self, preset=LoggingPreset.DEFAULT) -> None:
        super().__init__()
        self.entries = []
        self.lines = []
        self.setFormatter(JSONFormatter(preset))

    @override
    def emit(self, record):
        line = self.format(record)
        self.lines.append(line)
        self.entries.append(json.loads(line))


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    root_logger = logging.getLogger()
    handlers = list(root_logger.handlers)
    level = root_logger.level
    try:
        yield
    finally:
        root_logger.handlers[:] = handlers
        root_logger.setLevel(level)


@pytest.mark.parametrize(
    "module_name",
    [
        "examples.basic.main",
        "examples.gcp.main",
        "examples.aws.main",
        "examples.azure.main",
    ],
)
async def test_provider_example_is_runnable(module_name, capsys):
    module = cast("_ExampleModule", importlib.import_module(module_name))
    capsys.readouterr()

    async with asgi_client(module.app) as client:
        response = await client.get(
            "/health",
            headers={"X-Request-ID": "example-request", "traceparent": TRACEPARENT},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert response.headers["X-Request-ID"] == "example-request"

    captured = capsys.readouterr()
    if module_name == "examples.gcp.main":
        assert captured.out
        assert captured.err == ""
        output = captured.out
    else:
        output = captured.err

    entries = [json.loads(line) for line in output.splitlines() if line]
    relevant_entries = [entry for entry in entries if entry["logger"] in {module_name, "http.access"}]
    expected_messages = [(module_name, "health check")]
    if module_name == "examples.gcp.main":
        expected_messages.append((module_name, "dependency check"))
    expected_messages.append(("http.access", "request completed"))
    assert [(entry["logger"], entry["message"]) for entry in relevant_entries] == expected_messages

    *application_entries, access = relevant_entries
    for entry in relevant_entries:
        assert entry["request_id"] == "example-request"
        assert entry["correlation_id"] == TRACE_ID

    assert access["path_template"] == "/health"
    assert access["operation_id"] == "health_check"
    if module_name == "examples.gcp.main":
        health, dependency = application_entries
        assert health["severity"] == "INFO"
        assert health["service_name"] == "example-service"
        assert health["service_version"] == "1.0.0"
        assert health["health_status"] == "ok"
        assert dependency["severity"] == "DEBUG"
        assert dependency["dependency"] == "database"
        assert dependency["dependency_status"] == "ok"
        assert dependency["check_duration_ms"] == 3
        assert access["severity"] == "INFO"
        assert access["logging.googleapis.com/trace"] == TRACE_ID
        assert access["httpRequest"]["status"] == 200
    elif module_name == "examples.aws.main":
        assert access["xray_trace_id"] == f"1-{TRACE_ID[:8]}-{TRACE_ID[8:]}"
    elif module_name == "examples.azure.main":
        assert access["operation_Id"] == TRACE_ID
        assert access["operation_ParentId"] == "00f067aa0ba902b7"
    else:
        assert access["level"] == "INFO"


@pytest.mark.parametrize(
    ("factory_name", "expected_random"),
    [
        ("create_default_app", None),
        ("create_level_2_app", True),
    ],
)
async def test_basic_example_demonstrates_default_and_level_2_output(factory_name, expected_random, capsys):
    module = importlib.reload(importlib.import_module("examples.basic.main"))
    capsys.readouterr()
    app = getattr(module, factory_name)()

    async with asgi_client(app) as client:
        response = await client.get(
            "/health",
            headers={"X-Request-ID": "trace-level-example", "traceparent": RANDOM_TRACEPARENT},
        )

    assert response.status_code == 200
    entries = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line]
    relevant_entries = [entry for entry in entries if entry["logger"] in {"examples.basic.main", "http.access"}]
    assert [entry["message"] for entry in relevant_entries] == ["health check", "request completed"]
    for entry in relevant_entries:
        assert entry["trace_flags"] == "03"
        assert entry["trace_sampled"] is True
        assert entry.get("trace_id_random") is expected_random


@pytest.mark.parametrize(
    ("preset", "threshold", "expected_messages"),
    [
        (LoggingPreset.DEFAULT, logging.DEBUG, ["health check", "dependency check", "request completed"]),
        (LoggingPreset.DEFAULT, logging.INFO, ["health check", "request completed"]),
        (LoggingPreset.GCP, logging.DEBUG, ["health check", "dependency check", "request completed"]),
        (LoggingPreset.GCP, logging.INFO, ["health check", "request completed"]),
    ],
)
async def test_health_scenarios_have_exact_portable_projection(preset, threshold, expected_messages):
    handler = _CaptureHandler(preset)
    application_logger = logging.getLogger(f"test.{preset.value}.health.application.{threshold}")
    access_logger = logging.getLogger(f"test.{preset.value}.health.access.{threshold}")
    for logger in (application_logger, access_logger):
        logger.handlers[:] = [handler]
        logger.propagate = False
    application_logger.setLevel(threshold)
    access_logger.setLevel(logging.DEBUG)
    clock_values = iter([0.0, 0.0125])

    app = FastAPI()
    app.add_middleware(
        AccessLogMiddleware,
        config=AccessLogConfig(
            logger=access_logger,
            preset=preset,
            clock=lambda: next(clock_values),
        ),
    )
    app.add_middleware(RequestContextMiddleware)

    @app.get("/health", operation_id="health_check")
    async def health():
        application_logger.info(
            "health check",
            extra={
                "service_name": "example-service",
                "service_version": "1.0.0",
                "health_status": "ok",
            },
        )
        application_logger.debug(
            "dependency check",
            extra={
                "dependency": "database",
                "dependency_status": "ok",
                "check_duration_ms": 3,
            },
        )
        return "ok"

    async with asgi_client(app) as client:
        response = await client.get("/health", headers={"X-Request-ID": "health-example"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "health-example"
    assert [entry["message"] for entry in handler.entries] == expected_messages
    assert all(entry["request_id"] == "health-example" for entry in handler.entries)
    assert all(entry["correlation_id"] == "health-example" for entry in handler.entries)
    health_entry = handler.entries[0]
    assert (
        health_entry
        | {
            "message": "health check",
            "service_name": "example-service",
            "service_version": "1.0.0",
            "health_status": "ok",
        }
        == health_entry
    )
    access = handler.entries[-1]
    assert access["method"] == "GET"
    assert access["status"] == 200
    assert access["duration_ms"] == 12.5
    assert access["path_template"] == "/health"
    assert access["operation_id"] == "health_check"
    assert not {"path", "peer_ip", "user_agent", "service_name", "service_version", "health_status"} & access.keys()
    if preset is LoggingPreset.GCP:
        assert health_entry["severity"] == "INFO"
        assert access["severity"] == "INFO"
        assert access["httpRequest"] == {
            "requestMethod": "GET",
            "status": 200,
            "latency": "0.012500s",
        }
    else:
        assert health_entry["level"] == "INFO"
        assert access["level"] == "INFO"
        assert "severity" not in health_entry
        assert "severity" not in access
        assert "httpRequest" not in access
    if threshold == logging.DEBUG:
        dependency = handler.entries[1]
        assert dependency["severity" if preset is LoggingPreset.GCP else "level"] == "DEBUG"
        assert dependency["dependency"] == "database"
        assert dependency["dependency_status"] == "ok"
        assert dependency["check_duration_ms"] == 3
    else:
        assert "dependency check" not in "\n".join(handler.lines)
        assert "check_duration_ms" not in "\n".join(handler.lines)


async def test_request_id_scenarios_replace_ambiguous_values_before_terminal_output():
    handler = _CaptureHandler()
    logger = logging.getLogger("test.core.request-id")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    generated = iter(["duplicate-replaced", "generated-safe"])
    clock_values = iter([0.0, 0.002, 0.0, 0.001])

    app = FastAPI()
    app.add_middleware(
        AccessLogMiddleware,
        config=AccessLogConfig(logger=logger, clock=lambda: next(clock_values)),
    )
    app.add_middleware(
        RequestContextMiddleware,
        config=RequestContextConfig(request_id_generator=lambda: next(generated)),
    )

    @app.get("/request-id", status_code=204)
    async def request_id_route():
        return Response(status_code=204)

    async with asgi_client(app) as client:
        duplicate = await client.get(
            "/request-id",
            headers=[("x-request-id", "caller-one"), ("x-request-id", "caller-two")],
        )
        invalid = await client.get("/request-id", headers={"x-request-id": "bad value"})

    assert duplicate.status_code == 204
    assert duplicate.headers["x-request-id"] == "duplicate-replaced"
    assert invalid.status_code == 204
    assert invalid.headers["x-request-id"] == "generated-safe"
    assert len(handler.entries) == 2
    expected = [
        ("duplicate-replaced", 2),
        ("generated-safe", 1),
    ]
    for entry, (request_id_value, duration_ms) in zip(handler.entries, expected, strict=True):
        assert entry["message"] == "request completed"
        assert entry["request_id"] == request_id_value
        assert entry["correlation_id"] == request_id_value
        assert entry["method"] == "GET"
        assert entry["duration_ms"] == duration_ms
        assert entry["status"] == 204
        assert entry["path_template"] == "/request-id"
        assert "severity" not in entry
        assert "httpRequest" not in entry
    raw = "\n".join(handler.lines)
    for forbidden in ("caller-one", "caller-two", "bad value"):
        assert forbidden not in raw


async def test_local_wrapper_preserves_request_context_and_structured_fields():
    handler = _CaptureHandler()
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG)

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/wrapper")
    async def wrapper():
        applog.debug("debug helper", component="worker")
        applog.info("info helper", component="worker")
        applog.warning("warning helper", component="worker")
        applog.error("error helper", ValueError("boom"), component="worker")
        applog.log(logging.WARNING, "log helper", component="worker")
        return {"ok": True}

    async with asgi_client(app) as client:
        response = await client.get(
            "/wrapper",
            headers={"X-Request-ID": "wrapper-request", "traceparent": TRACEPARENT},
        )

    assert response.status_code == 200
    entries = [entry for entry in handler.entries if entry["logger"] == "application"]
    assert [entry["message"] for entry in entries] == [
        "debug helper",
        "info helper",
        "warning helper",
        "error helper",
        "log helper",
    ]
    assert [entry["level"] for entry in entries] == ["DEBUG", "INFO", "WARNING", "ERROR", "WARNING"]
    assert all(entry["component"] == "worker" for entry in entries)
    assert all(entry["request_id"] == "wrapper-request" for entry in entries)
    assert all(entry["correlation_id"] == TRACE_ID for entry in entries)
    assert "ValueError: boom" in entries[3]["stacktrace"]
