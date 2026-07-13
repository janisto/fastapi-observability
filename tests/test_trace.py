from dataclasses import FrozenInstanceError

import pytest

from fastapi_request_observability import TraceContext, parse_traceparent
from fastapi_request_observability.trace import _with_tracestate

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"


@pytest.mark.parametrize(
    ("flags", "sampled"),
    [("00", False), ("01", True), ("03", True), ("02", False)],
)
def test_parse_traceparent_flags(flags, sampled):
    value = f"00-{TRACE_ID}-{PARENT_ID}-{flags}"
    trace = parse_traceparent(value)
    assert trace == TraceContext(TRACE_ID, PARENT_ID, flags, sampled, value)


def test_future_version_framing():
    base = f"01-{TRACE_ID}-{PARENT_ID}-01"
    assert parse_traceparent(base) is not None
    assert parse_traceparent(f"{base}-future") is not None
    assert parse_traceparent(f"{base}future") is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "invalid",
        f"ff-{TRACE_ID}-{PARENT_ID}-01",
        f"00-{TRACE_ID.upper()}-{PARENT_ID}-01",
        f"00-{'0' * 32}-{PARENT_ID}-01",
        f"00-{TRACE_ID}-{'0' * 16}-01",
        f"00-{TRACE_ID}-{PARENT_ID}-zz",
        f"00-{TRACE_ID}-{PARENT_ID}-01-extra",
        f"00_{TRACE_ID}-{PARENT_ID}-01",
        f"01-{TRACE_ID}-{PARENT_ID}-01-é",
        f"01-{TRACE_ID}-{PARENT_ID}-01-{'x' * 458}",
    ],
)
def test_rejects_invalid_traceparent(value):
    assert parse_traceparent(value) is None


def test_tracestate_combines_wire_order_and_enforces_byte_limit():
    trace = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert trace is not None
    assert _with_tracestate(trace, ["one=1", "two=2"]).tracestate == "one=1,two=2"
    maximum = f"{'a' * 256}={'b' * 255}"
    assert len(maximum) == 512
    assert _with_tracestate(trace, [maximum]).tracestate == maximum
    assert _with_tracestate(trace, [f"{'a' * 256}={'b' * 256}"]).tracestate is None


@pytest.mark.parametrize(
    "value",
    [
        "Uppercase=value",
        "key=",
        "key=value=extra",
        "key=value\n",
        "key=välue",
        f"{'a' * 257}=value",
        f"{'1' * 242}@system=value",
        f"tenant@{'s' * 15}=value",
        "one=1,one=2",
        ",".join(f"key{index}=value" for index in range(33)),
    ],
)
def test_invalid_tracestate_is_discarded_without_invalidating_traceparent(value):
    trace = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert trace is not None
    result = _with_tracestate(trace, [value])
    assert result.trace_id == TRACE_ID
    assert result.tracestate is None


def test_tracestate_accepts_ows_empty_members_and_valid_multi_tenant_key():
    trace = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert trace is not None
    value = " tenant1@system=value with spaces , ,second=two\t"
    assert _with_tracestate(trace, [value]).tracestate == value


def test_trace_context_is_immutable():
    trace = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert trace is not None
    with pytest.raises(FrozenInstanceError):
        trace.trace_id = "changed"  # ty: ignore[invalid-assignment]
