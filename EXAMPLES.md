# Examples

This guide shows how to wire `fastapi-request-observability` into FastAPI
services while keeping one log contract across Google Cloud, provider-neutral,
AWS, and Azure deployments.

When one configuration is shown, this project uses GCP as the canonical
example. The other runnable applications remain first-class and tested.

| Example | Purpose |
| --- | --- |
| [`examples/gcp`](examples/gcp) | Canonical Google Cloud Logging field shape. |
| [`examples/basic`](examples/basic) | Generic JSON for local or provider-neutral pipelines. |
| [`examples/aws`](examples/aws) | CloudWatch-friendly JSON and a derived X-Ray trace ID. |
| [`examples/azure`](examples/azure) | Azure Monitor and Application Insights operation fields. |
| [`examples/local_wrapper/applog.py`](examples/local_wrapper/applog.py) | Optional application-local logging helpers. |

## Core wiring

Every service follows the same shape:

1. Configure one `JSONFormatter` preset on the application handlers.
2. Add access logging first and request context second. FastAPI applies the
   last-added middleware first, so request context remains active while the
   access record is emitted.
3. Use ordinary `logging` calls in handlers and services.

The canonical GCP wiring is:

```python
handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter(LoggingPreset.GCP))

root_logger = logging.getLogger()
root_logger.handlers.clear()
root_logger.addHandler(handler)
root_logger.setLevel(logging.INFO)

app = FastAPI()
app.add_middleware(
    AccessLogMiddleware,
    config=AccessLogConfig(
        logger=logging.getLogger("http.access"),
        preset=LoggingPreset.GCP,
    ),
)
app.add_middleware(RequestContextMiddleware)
```

No Google Cloud project ID is required. With valid W3C context,
`logging.googleapis.com/trace` contains the raw trace ID.

## Run the canonical GCP example

```bash
uv run uvicorn examples.gcp.main:app --no-access-log
```

Call the health route with request and trace correlation:

```bash
curl -i \
  -H 'X-Request-ID: demo-123' \
  -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' \
  -H 'tracestate: vendor=value' \
  http://127.0.0.1:8000/health
```

The request ID remains `demo-123`; `correlation_id` becomes the W3C trace ID.
The handler and access records contain the same correlation fields. The access
record also contains `httpRequest`, `/health` as the route template, and
`health_check` as the explicit operation ID.

Representative GCP fields:

```json
{"severity":"INFO","message":"request completed","request_id":"demo-123","correlation_id":"4bf92f3577b34da6a3ce929d0e0e4736","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","logging.googleapis.com/trace":"4bf92f3577b34da6a3ce929d0e0e4736","logging.googleapis.com/trace_sampled":true,"method":"GET","path":"/health","path_template":"/health","operation_id":"health_check","status":200}
```

The package does not create spans and therefore does not manufacture
`logging.googleapis.com/spanId` from the incoming parent ID.

## Provider-neutral JSON

```bash
uv run uvicorn examples.basic.main:app --no-access-log
```

The default preset writes `level` and the generic correlation fields without
provider-specific trace aliases.

## AWS

```bash
uv run uvicorn examples.aws.main:app --no-access-log
```

The AWS preset keeps flat JSON. A valid W3C trace ID is also formatted as
`xray_trace_id`, for example
`1-4bf92f35-77b34da6a3ce929d0e0e4736`. The package does not create X-Ray
segments or parse `X-Amzn-Trace-Id`.

## Azure

```bash
uv run uvicorn examples.azure.main:app --no-access-log
```

The Azure preset maps valid W3C values to `operation_Id` and
`operation_ParentId`. It does not initialize an Azure SDK or parse legacy
`Request-Id` headers.

## Optional local wrapper

[`examples/local_wrapper/applog.py`](examples/local_wrapper/applog.py) provides
small `debug`, `info`, `warning`, `error`, and arbitrary-level helpers around
standard-library logging. It is a convenience layer, not required package
configuration. Because `JSONFormatter` reads request metadata from the current
`ContextVar`, helper calls retain request and trace correlation without
accepting a request object or context parameter.

```python
from examples.local_wrapper import applog

applog.info("loading item", item_id=item_id)
applog.error("item load failed", error, item_id=item_id)
```

Tests verify that the wrapper preserves request metadata, structured fields,
levels, and exception information.

## Per-project checklist

- Use Python 3.13 or newer.
- Use GCP when documentation needs one representative configuration.
- Keep runnable examples limited to required package wiring.
- Use the same preset for `JSONFormatter` and `AccessLogConfig`.
- Add access middleware before request-context middleware.
- Group logs by `path_template`, not the concrete request path.
- Disable duplicate ASGI-server access logs when this package owns them.
- Keep provider tracing SDKs separate from this correlation package.
- Never place secrets or raw bodies in log fields.
- Run lint, typing, tests, build inspection, and artifact smoke tests.

## References

- [Google Cloud: Link log entries with traces](https://docs.cloud.google.com/trace/docs/trace-log-integration)
- [Google Cloud Trace release notes](https://docs.cloud.google.com/trace/docs/release-notes)
- [Google Cloud structured logging](https://docs.cloud.google.com/logging/docs/structured-logging)
- [W3C Trace Context](https://www.w3.org/TR/trace-context/)
