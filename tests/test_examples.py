import importlib
import json
import logging
from collections.abc import Iterator
from typing import Protocol, cast, override

import pytest
from fastapi import FastAPI

from examples.local_wrapper import applog
from fastapi_request_observability import JSONFormatter, RequestContextMiddleware
from tests._client import asgi_client

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


class _ExampleModule(Protocol):
    app: FastAPI


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.entries = []
        self.setFormatter(JSONFormatter())

    @override
    def emit(self, record):
        self.entries.append(json.loads(self.format(record)))


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

    entries = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line]
    application = next(entry for entry in entries if entry["message"] == "health check")
    access = next(entry for entry in entries if entry["message"] == "request completed")
    for entry in (application, access):
        assert entry["request_id"] == "example-request"
        assert entry["correlation_id"] == TRACE_ID

    assert access["path_template"] == "/health"
    assert access["operation_id"] == "health_check"
    if module_name == "examples.gcp.main":
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
