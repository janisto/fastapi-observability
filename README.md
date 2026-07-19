# fastapi-request-observability

[![PyPI version](https://img.shields.io/pypi/v/fastapi-request-observability.svg)](https://pypi.org/project/fastapi-request-observability/)
[![Python versions](https://img.shields.io/pypi/pyversions/fastapi-request-observability.svg)](https://pypi.org/project/fastapi-request-observability/)
[![CI](https://img.shields.io/github/actions/workflow/status/janisto/fastapi-observability/ci.yml?branch=main&label=CI)](https://github.com/janisto/fastapi-observability/actions/workflows/ci.yml)
[![Socket Badge](https://badge.socket.dev/pypi/package/fastapi-request-observability)](https://socket.dev/pypi/package/fastapi-request-observability)

Focused FastAPI middleware for request IDs, W3C trace correlation, contextual
JSON logs, and one structured access record per HTTP response.

## Why this package exists

Managed platforms such as Cloud Run already collect container output.
Applications should only need to write structured JSON to standard output
(`stdout`); the platform can handle ingestion and delivery.

Compared with sending logs through an in-process cloud logging client, this
reduces container CPU, memory, and network use by removing logging API calls,
authentication, buffering, batching, and retry work from the application. Under
sustained logging load, that reduction can provide a noticeable performance
improvement. It also avoids the dependency and maintenance cost of a cloud
logging SDK, including its configuration, credentials, and upgrades.

This package turns that simple pipeline into useful production observability.
It provides validated request IDs, strict W3C trace correlation,
request-scoped fields, and one structured terminal access record. Application
and access logs share the same correlation metadata, making all records from a
request easier to find, filter, and understand.

Cloud presets map the same logging contract to provider-oriented fields without
coupling application code to a cloud logging SDK. The package focuses on
structured logging and request correlation: it does not create spans, configure
OpenTelemetry, or ship logs to a backend.

## Package scope

It uses standard-library `logging` and pure ASGI middleware, with no exporter or
logging-framework dependency, so applications retain control of recovery,
handlers, and deployment policy.

> The PyPI distribution is `fastapi-request-observability` and the import is
> `fastapi_request_observability`. The similarly named
> `fastapi-observability` distribution is an unrelated project.

## Installation

```bash
uv add fastapi-request-observability
```

Python 3.13 or newer and FastAPI 0.130.0 or newer are supported. The Python
compatibility window follows the latest two stable feature releases.

## GCP setup

When this documentation shows one configuration, it uses GCP. Complete
runnable GCP, provider-neutral, AWS, and Azure applications are available in
[`examples`](examples).

```python
import logging
import sys

from fastapi import FastAPI
from fastapi_request_observability import (
    AccessLogConfig,
    AccessLogMiddleware,
    GcpProfileVersion,
    JSONFormatter,
    LoggingPreset,
    RequestContextMiddleware,
)

gcp_profile_version = GcpProfileVersion.V0_1_0
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(
    JSONFormatter(LoggingPreset.GCP, gcp_profile_version=gcp_profile_version)
)

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
    config=AccessLogConfig(
        logger=logging.getLogger("http.access"),
        preset=LoggingPreset.GCP,
        gcp_profile_version=gcp_profile_version,
    ),
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
custom generator falls back to the package's safe format. Custom validators
can only narrow the same URI-unreserved baseline; they cannot admit punctuation
such as `!` or `:`, whitespace, non-ASCII, empty, or oversized values.
Accessors return `None` outside a request; no background-job context is
manufactured.
Invalid or empty configured HTTP header names fail immediately when the config
object is constructed; use `inject_response_header=False` to disable response
header injection.

`traceparent` parsing defaults to the pinned W3C Trace Context Level 1
Recommendation. Invalid, duplicate, uppercase, zero-ID, or oversized values
are ignored. Version `00` must be exactly 55 characters; future versions follow
W3C extension framing and treat every dash-delimited suffix as opaque.
`tracestate` field-lines are combined in wire order,
canonicalized by removing HTTP optional whitespace around members, and retained
only when their selected-level key/value grammar, unique-key rule, 32-member
limit, and 512-byte limit are valid. Empty members are valid and count toward
the limit. An invalid `tracestate` does not invalidate an otherwise valid
`traceparent`. When the trace is valid, its trace ID is the correlation ID;
otherwise the request ID is.

Level 2 is an explicit opt-in. Configure the same immutable level on both
middleware components when both are installed:

```python
from fastapi_request_observability import (
    AccessLogConfig,
    RequestContextConfig,
    TraceContextLevel,
)

trace_level = TraceContextLevel.LEVEL_2
access_config = AccessLogConfig(trace_context_level=trace_level)
request_context_config = RequestContextConfig(trace_context_level=trace_level)
```

Values other than `1` or `2` fail configuration immediately. Both levels
preserve `trace_flags` and derive `trace_sampled` from bit zero. Level 2 also
emits `trace_id_random` from bit one; Level 1 deliberately omits that field.

The incoming parent ID is not a span created by this application. No preset
emits it as a current span ID.

## Log contract

Every JSON record contains `timestamp`, `level` (`severity` on GCP), `logger`,
and `message`. Set `include_source=True` to add source file, line, and function.
Exceptions use `stacktrace`. JSON-serializable `extra` values stay structured;
unsupported values receive a deterministic type marker instead of breaking
logging. Mapping keys are normalized to JSON strings before encoding; if two
native keys normalize to the same JSON name, the first value is retained so a
record never contains duplicate raw member names.

The formatter enriches records in the thread and context where formatting
occurs. Applications using `QueueHandler` should format or copy contextual
fields before a record crosses into a listener thread. Access records snapshot
their correlation fields before emission and remain complete when formatted
later.

During a request, records also contain `request_id` and `correlation_id`. A
valid W3C context adds `trace_id`, `parent_id`, `trace_flags`, and
`trace_sampled`; configured Level 2 additionally adds `trace_id_random`.

The access record message is `request completed` and includes:

| Field | Meaning |
| --- | --- |
| `method` | HTTP method |
| `path` | Opt-in escaped concrete path, without query string |
| `path_template` | Canonical matched template (`{name}` or `{*name}`) when available |
| `operation_id` | Only an explicitly configured `APIRoute.operation_id` |
| `status` | Status accepted by the downstream ASGI sender, when observed |
| `duration_ms` | Handling and streaming time in milliseconds |
| `terminal_reason` | Standard reason for abnormal completion; absent after normal completion |
| `peer_ip` | Opt-in direct ASGI client address from `scope["client"][0]` |
| `user_agent` | Opt-in single incoming user agent |
| `error` | Observed exception type and message |

The default level is `ERROR` for abnormal completion or a normal 5xx,
`WARNING` for a normal 4xx, and `INFO` otherwise.
Package and provider fields are reserved: `extra` values and access callbacks
cannot replace them.

`AccessLogConfig` also accepts independent `capture_path`, `capture_peer_ip`,
and `capture_user_agent` booleans, all defaulting to `False`; a monotonic
`clock`; a `status_level(status)` callback; a synchronous
`extra_fields(scope)` callback; and a custom message. Callback and logging
failures use the default behavior and cannot replace the HTTP response. When
installed without `RequestContextMiddleware`, access middleware creates
default request context and adds `X-Request-ID` itself.

`path_template` is the default aggregation key. Opt-in `path` is useful for
individual-request diagnostics and has unbounded cardinality. Query strings,
forwarded IPs, bodies, authorization, cookies, and arbitrary headers are never
logged.

FastAPI whole-segment path converters are normalized: ordinary converters such
as `{item_id:int}` emit `{item_id}`, while `{path:path}` emits `{*path}`.
Ambiguous native forms are omitted rather than populated with request data.

## Cloud presets

Pass the same preset to the formatter and access configuration:

```python
from fastapi_request_observability import (
    AccessLogConfig,
    GcpProfileVersion,
    JSONFormatter,
    LoggingPreset,
)

preset = LoggingPreset.GCP  # or AWS, AZURE, DEFAULT
version = GcpProfileVersion.V0_1_0
handler.setFormatter(JSONFormatter(preset, gcp_profile_version=version))
access_config = AccessLogConfig(
    logger=logging.getLogger("http.access"),
    preset=preset,
    gcp_profile_version=version,
)
```

- `GCP` uses `severity`, `logging.googleapis.com/trace`,
  `logging.googleapis.com/trace_sampled`, and a structured `httpRequest` access
  field. `logging.googleapis.com/trace` contains the validated raw W3C trace
  ID and requires no project-ID configuration. The preset never emits a fake
  `logging.googleapis.com/spanId`. Its `httpRequest.requestUrl` is the opt-in,
  query-free path only; scheme and authority are never included. Omitting
  `gcp_profile_version` resolves once to the newest profile supported by the
  installed package, currently `0.1.0`; unsupported pins fail initialization
  and no network lookup occurs. The resolved enum remains inspectable on both
  `JSONFormatter.gcp_profile_version` and
  `AccessLogConfig.gcp_profile_version`.
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
- An exception before an accepted `http.response.start` omits status and uses
  terminal reason `service_error`; no synthetic 500 is logged.
- Once response headers are sent, that committed status wins even if streaming
  later fails; the record uses terminal reason `body_error` and level `ERROR`.
- Cancellation uses terminal reason `cancelled`. An application that returns
  without a complete response uses `response_dropped` after response start or
  `unknown_failure` before it.
- Access emission occurs after the final response body event, or after the final
  response-trailers event when trailers were declared, so duration includes
  streaming and trailers but excludes later Starlette background work.
- A background-task failure does not produce a second record.
- Logging and custom-field callback failures are diagnosed to `stderr` and do
  not replace the response.

With normal `app.add_middleware` installation, Starlette's outer
`ServerErrorMiddleware` creates the final unhandled 500 after user middleware
has re-raised. Consequently, the package cannot add `X-Request-ID` to that final
500 response. The access record still contains the request ID, omits the
unobserved outer status, and reports `service_error`.

Services that require the header on the final 500 can wrap the completed
FastAPI application exported to the ASGI server:

```python
fastapi_app = FastAPI()
fastapi_app.add_middleware(
    AccessLogMiddleware,
    config=AccessLogConfig(
        logger=logging.getLogger("http.access"),
        preset=LoggingPreset.GCP,
    ),
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
logging is outside the package scope.

## Proxy trust

Opt-in `peer_ip` comes only from the ASGI scope. The package does not parse
`Forwarded` or `X-Forwarded-For`, because trusting those headers without a known
proxy boundary allows spoofing. Configure the ASGI server so `scope["client"]`
represents the intended direct peer boundary.

## Compatibility

Beginning with v1.0.0, exported APIs, configuration defaults, structured log
fields, and supported runtime versions are compatibility contracts. Breaking
changes require a new major version, explicit changelog coverage, and migration
guidance. The package does not configure logging at import time and does not
claim ownership of exception responses.

Repository tests use HTTPX2 directly with its asynchronous ASGI transport.
Deprecated HTTPX and FastAPI/Starlette `TestClient` are intentionally excluded.
If the package later needs to mock outbound HTTP, use `pytest-httpx2` and its
`httpx2_mock` fixture; do not add `pytest-httpx`.

See [EXAMPLES.md](https://github.com/janisto/fastapi-observability/blob/main/EXAMPLES.md)
for complete configurations.

## Development

Development uses [uv](https://docs.astral.sh/uv/) and
[just](https://github.com/casey/just). On macOS, install the workflow linters:

```sh
brew install actionlint zizmor
```

Then run the repository gates:

```sh
just install
just qa
just package-check
```

`just qa` runs Ruff, Ty, pytest with branch coverage,
[actionlint](https://github.com/rhysd/actionlint), and
[zizmor](https://docs.zizmor.sh/). `just package-check` builds, inspects, and
smoke-tests the wheel and source distribution in isolated environments.

## Mutation testing

The traceparent parser has a focused [mutmut](https://github.com/boxed/mutmut)
campaign. It introduces small changes to `parse_traceparent` and verifies that
the parser tests detect observable behavior changes:

```sh
just mutation
```

This intentionally runs outside `just qa`. Use `uv run mutmut results` to
list surviving mutants and `uv run mutmut show <mutant>` to inspect one. Add a
test when a survivor exposes a contract gap; equivalent transformations do not
need production pragmas or artificial assertions.
