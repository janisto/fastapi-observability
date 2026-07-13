# Examples

## Runnable basic application

[`examples/basic.py`](examples/basic.py) configures standard-library JSON
logging, both middleware layers, an explicit operation ID, and ordinary module
logging. Run it with:

```bash
uv run uvicorn examples.basic:app --no-access-log
curl -H 'X-Request-ID: example-request' http://127.0.0.1:8000/items/42
```

## GCP

```python
preset = LoggingPreset.GCP
gcp_project_id = "example-project"
handler.setFormatter(JSONFormatter(preset, gcp_project_id=gcp_project_id))
app.add_middleware(
    AccessLogMiddleware,
    config=AccessLogConfig(
        logger=logging.getLogger("http.access"),
        preset=preset,
        gcp_project_id=gcp_project_id,
    ),
)
app.add_middleware(RequestContextMiddleware)
```

This produces GCP `severity`, trace-correlation fields, and `httpRequest`.
Google Cloud requires `logging.googleapis.com/trace` to be a fully qualified
`projects/PROJECT_ID/traces/TRACE_ID` resource name for Logs Explorer and Trace
Viewer correlation, so the project ID is explicit and never guessed. If it is
omitted, the generic validated `trace_id` remains present but the GCP special
trace fields are omitted.

## AWS

Use `LoggingPreset.AWS` in both places. A valid W3C trace ID such as
`4bf92f3577b34da6a3ce929d0e0e4736` becomes
`1-4bf92f35-77b34da6a3ce929d0e0e4736`. This is log correlation only and does not
create a segment.

## Azure

Use `LoggingPreset.AZURE` in both places. `operation_Id` receives the W3C trace
ID and `operation_ParentId` receives the incoming parent ID. This is structured
log metadata, not Application Insights telemetry.

## Custom request header and fields

```python
from fastapi_request_observability import RequestContextConfig

app.add_middleware(
    AccessLogMiddleware,
    config=AccessLogConfig(
        logger=logging.getLogger("http.access"),
        extra_fields=lambda scope: {"deployment": "production"},
    ),
)
app.add_middleware(
    RequestContextMiddleware,
    config=RequestContextConfig(
        request_id_header="X-Correlation-ID",
        response_header="X-Correlation-ID",
    ),
)
```

The extra-fields callback is synchronous and should be fast. Reserved package
or provider keys returned by it are ignored.
