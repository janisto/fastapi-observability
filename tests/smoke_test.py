from fastapi_request_observability import (
    AccessLogConfig,
    AccessLogMiddleware,
    JSONFormatter,
    LoggingPreset,
    RequestContext,
    RequestContextConfig,
    RequestContextMiddleware,
    TraceContext,
    correlation_id,
    current_request_context,
    parse_traceparent,
    request_id,
    trace_context,
)

assert AccessLogConfig
assert AccessLogMiddleware
assert JSONFormatter
assert LoggingPreset.DEFAULT.value == "default"
assert RequestContext
assert RequestContextConfig
assert RequestContextMiddleware
assert TraceContext
assert request_id() is None
assert correlation_id() is None
assert trace_context() is None
assert current_request_context() is None
assert parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01") is not None
