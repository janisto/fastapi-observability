# fastapi-request-observability

Focused FastAPI middleware for request IDs, W3C trace correlation, contextual
JSON logs, and one structured access record per HTTP response.

This package deliberately does not create traces, metrics, or profiles. It has
no OpenTelemetry, provider SDK, exporter, or logging-framework dependency. It
uses standard-library `logging` and pure ASGI middleware so applications retain
control of recovery, handlers, and deployment policy.

> The PyPI distribution is `fastapi-request-observability` and the import is
> `fastapi_request_observability`. The similarly named
> `fastapi-observability` distribution is an unrelated project.

## Installation

```bash
uv add fastapi-request-observability
```

Python 3.10 or newer and FastAPI 0.135.3 or newer are supported.

## Minimal setup

```python
import logging

from fastapi import FastAPI
from fastapi_request_observability import (
    AccessLogConfig,
    AccessLogMiddleware,
    JSONFormatter,
    RequestContextMiddleware,
)

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())

root_logger = logging.getLogger()
root_logger.handlers.clear()
root_logger.addHandler(handler)
root_logger.setLevel(logging.INFO)

app = FastAPI()

# FastAPI applies the last-added middleware first on requests. Add access
# logging first, then request context, so context remains bound during access
# record emission.
app.add_middleware(
    AccessLogMiddleware,
    config=AccessLogConfig(logger=logging.getLogger("http.access")),
)
app.add_middleware(RequestContextMiddleware)


@app.get("/items/{item_id}", operation_id="get_item")
async def get_item(item_id: str) -> dict[str, str]:
    logging.getLogger(__name__).info("loading item", extra={"item_id": item_id})
    return {"item_id": item_id}
```

Normal application loggers inherit request fields when their handler uses
`JSONFormatter`. The package does not replace root handlers or configure
Uvicorn. If the structured access record replaces Uvicorn's access line, run
Uvicorn with `--no-access-log` or explicitly disable the `uvicorn.access`
logger in the application's logging configuration.

## Request and trace context

`RequestContextMiddleware` accepts a single `X-Request-ID` containing 1–128
ASCII URI-unreserved characters (`A-Z`, `a-z`, `0-9`, `-`, `.`, `_`, `~`). A
missing, duplicate, empty, oversized, or invalid value is replaced with 128
bits of randomness. The selected value is available from:

- `request_id()`;
- `correlation_id()` and `trace_context()`;
- `current_request_context()`;
- `request.state.request_id`;
- the response `X-Request-ID` header;
- application and access logs.

The response header, input headers, generator, and validator are configurable
with `RequestContextConfig`. Generated values are validated too, and an invalid
custom generator falls back to the package's safe format. Accessors return
`None` outside a request; no background-job context is manufactured.
Invalid or empty configured HTTP header names fail immediately when the config
object is constructed; use `inject_response_header=False` to disable response
header injection.

`traceparent` parsing is strict. Invalid, duplicate, uppercase, zero-ID, or
oversized values are ignored. Version `00` must be exactly 55 characters;
future versions follow W3C extension framing. `tracestate` fields are combined
in wire order and retained only when their W3C key/value grammar, unique-key
rule, 32-member limit, and 512-byte limit are valid. An invalid `tracestate`
does not invalidate an otherwise valid `traceparent`. When the trace is valid,
its trace ID is the correlation ID; otherwise the request ID is.

The incoming parent ID is not a span created by this application. No preset
emits it as a current span ID.

## Log contract

Every JSON record contains `timestamp`, `level` (`severity` on GCP), `logger`,
and `message`. Set `include_source=True` to add source file, line, and function.
Exceptions use `stacktrace`. JSON-serializable `extra` values stay structured;
unsupported values receive a deterministic type marker instead of breaking
logging.

The formatter enriches records in the thread and context where formatting
occurs. Applications using `QueueHandler` should format or copy contextual
fields before a record crosses into a listener thread. Access records snapshot
their correlation fields before emission and remain complete when formatted
later.

During a request, records also contain `request_id` and `correlation_id`. A
valid W3C context adds `trace_id`, `parent_id`, `trace_flags`, and
`trace_sampled`.

The access record message is `request completed` and includes:

| Field | Meaning |
| --- | --- |
| `method` | HTTP method |
| `path` | Escaped concrete path, without query string |
| `path_template` | Matched FastAPI route template when available |
| `operation_id` | Only an explicitly configured `APIRoute.operation_id` |
| `status` | Status sent on the wire |
| `duration_ms` | Handling and streaming time in milliseconds |
| `remote_ip` | `scope["client"][0]`, when present |
| `user_agent` | Incoming user agent, when present |
| `error` | Observed exception type and message |

The default level is `ERROR` for 5xx, `WARNING` for 4xx, and `INFO` otherwise.
Package and provider fields are reserved: `extra` values and access callbacks
cannot replace them.

`AccessLogConfig` also accepts a monotonic `clock`, a `status_level(status)`
callback, a synchronous `extra_fields(scope)` callback, and a custom message.
Callback and logging failures use the default behavior and cannot replace the
HTTP response. When installed without `RequestContextMiddleware`, access
middleware creates default request context and adds `X-Request-ID` itself.

`path_template` is the aggregation key; `path` is useful for individual-request
diagnostics and has unbounded cardinality. Query strings are omitted because
they frequently carry secrets and high-cardinality values. Bodies,
authorization, cookies, and arbitrary headers are never logged.

## Cloud presets

Pass the same preset and provider configuration to the formatter and access
configuration:

```python
from fastapi_request_observability import AccessLogConfig, JSONFormatter, LoggingPreset

preset = LoggingPreset.GCP  # or AWS, AZURE, DEFAULT
gcp_project_id = "example-project"
handler.setFormatter(JSONFormatter(preset, gcp_project_id=gcp_project_id))
access_config = AccessLogConfig(
    logger=logging.getLogger("http.access"),
    preset=preset,
    gcp_project_id=gcp_project_id,
)
```

- `GCP` uses `severity`, `logging.googleapis.com/trace`,
  `logging.googleapis.com/trace_sampled`, and a structured `httpRequest` access
  field. Set `gcp_project_id` to the actual Google Cloud project ID so the
  special trace field is the required
  `projects/PROJECT_ID/traces/TRACE_ID` resource name. Without a project ID,
  generic `trace_id` remains available but the Google special trace fields are
  omitted. The preset never emits a fake `logging.googleapis.com/spanId`.
- `AWS` adds `xray_trace_id` in `1-8hex-24hex` form. It does not create an X-Ray
  segment.
- `AZURE` adds `operation_Id` and `operation_ParentId`. It does not start or
  export Application Insights telemetry.

Provider fields correlate logs only. Trace creation and export remain the
application's responsibility.

## Response and exception behavior

The middleware observes exceptions, emits once, and re-raises the original
exception unchanged. It never synthesizes a replacement 500 response.

- Handled exceptions and validation errors use the emitted status.
- An exception before `http.response.start` logs status 500.
- Once response headers are sent, that committed status wins even if streaming
  later fails.
- Access emission occurs after the final response body event, so duration
  includes streaming but excludes later Starlette background work.
- A background-task failure does not produce a second record.
- Logging and custom-field callback failures are diagnosed to `stderr` and do
  not replace the response.

With normal `app.add_middleware` installation, Starlette's outer
`ServerErrorMiddleware` creates the final unhandled 500 after user middleware
has re-raised. Consequently, the package cannot add `X-Request-ID` to that final
500 response. The access record still contains the request ID and status 500.

Services that require the header on the final 500 can wrap the completed
FastAPI application exported to the ASGI server:

```python
fastapi_app = FastAPI()
fastapi_app.add_middleware(
    AccessLogMiddleware,
    config=AccessLogConfig(logger=logging.getLogger("http.access")),
)

# Add routes and other FastAPI middleware to fastapi_app first.
app = RequestContextMiddleware(fastapi_app)
```

This wrapper is outside FastAPI's recovery middleware and therefore observes
the final 500 response. The trade-off is that the exported `app` is an ASGI
wrapper rather than the `FastAPI` object; retain `fastapi_app` for application
configuration and test setup.

## Streaming, concurrency, and non-HTTP scopes

All request state is local to a pure ASGI `__call__` and the `ContextVar` token
is reset in `finally`. Concurrent and sequential requests cannot share package
context. WebSocket and lifespan scopes pass through unchanged; WebSocket access
logging is outside the v0.1 scope.

## Proxy trust

`remote_ip` comes only from the ASGI scope. The package does not parse
`Forwarded` or `X-Forwarded-For`, because trusting those headers without a known
proxy boundary allows spoofing. Configure trusted proxy handling in the ASGI
server or deployment so `scope["client"]` is already normalized.

## Compatibility

The v0.x line may evolve, but logged field names are treated as a compatibility
contract. Field changes require changelog review. The package does not configure
logging at import time and does not claim ownership of exception responses.

Repository tests use HTTPX2 directly with its asynchronous ASGI transport.
Deprecated HTTPX and FastAPI/Starlette `TestClient` are intentionally excluded.
If the package later needs to mock outbound HTTP, use `pytest-httpx2` and its
`httpx2_mock` fixture; do not add `pytest-httpx`.

See [EXAMPLES.md](https://github.com/janisto/fastapi-observability/blob/main/EXAMPLES.md)
for complete configurations.
