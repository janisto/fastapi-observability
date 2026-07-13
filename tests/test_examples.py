import importlib
import json
import logging
from collections.abc import Iterator
from typing import Protocol, cast

import pytest
from fastapi import FastAPI

from tests._client import asgi_client

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


class _ExampleModule(Protocol):
    app: FastAPI


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
