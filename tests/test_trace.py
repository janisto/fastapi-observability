from dataclasses import FrozenInstanceError

import pytest

from fastapi_request_observability import TraceContext, parse_traceparent
from fastapi_request_observability.trace import _with_tracestate

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"


@pytest.mark.parametrize(
    ("flags", "sampled"),
    [
        ("00", False),
        ("01", True),
        ("03", True),
        ("02", False),
        ("0f", True),
        ("0e", False),
        ("1f", True),
        ("1e", False),
        ("ff", True),
        ("fe", False),
    ],
)
def test_parse_traceparent_flags(flags, sampled):
    value = f"00-{TRACE_ID}-{PARENT_ID}-{flags}"
    trace = parse_traceparent(value)
    assert trace == TraceContext(TRACE_ID, PARENT_ID, flags, sampled, value)


def test_future_version_accepts_base_and_dash_delimited_extension():
    base = f"01-{TRACE_ID}-{PARENT_ID}-01"
    assert parse_traceparent(base) is not None
    assert parse_traceparent(f"{base}-future") is not None
    assert parse_traceparent(f"{base}future") is None


def test_future_version_accepts_512_character_limit_and_rejects_513():
    base = f"01-{TRACE_ID}-{PARENT_ID}-01"
    assert parse_traceparent(f"{base}-{'x' * 456}") is not None
    assert parse_traceparent(f"{base}-{'x' * 457}") is None


@pytest.mark.parametrize("separator_index", [2, 35, 52])
def test_each_required_traceparent_separator_is_validated_independently(separator_index):
    value = list(f"00-{TRACE_ID}-{PARENT_ID}-01")
    value[separator_index] = "_"
    assert parse_traceparent("".join(value)) is None


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


def test_tracestate_combines_multiple_headers_in_wire_order():
    trace = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert trace is not None
    assert _with_tracestate(trace, ["one=1", "two=2"]).tracestate == "one=1,two=2"


def test_tracestate_accepts_512_byte_limit_and_rejects_513():
    trace = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert trace is not None
    maximum = f"{'a' * 256}={'b' * 255}"
    assert len(maximum) == 512
    assert _with_tracestate(trace, [maximum]).tracestate == maximum
    assert _with_tracestate(trace, [f"{'a' * 256}={'b' * 256}"]).tracestate is None


def test_empty_tracestate_does_not_create_an_empty_context_field():
    trace = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert trace is not None
    assert _with_tracestate(trace, []).tracestate is None
    assert _with_tracestate(trace, [""]).tracestate is None


def test_tracestate_accepts_exact_32_member_limit():
    trace = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert trace is not None
    members = [f"key{index}=value" for index in range(32)]
    assert _with_tracestate(trace, [",".join(members)]).tracestate == ",".join(members)


def test_tracestate_accepts_maximum_multi_tenant_key_lengths():
    trace = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert trace is not None
    boundary_key = f"{'1' * 241}@{'s' * 14}"
    assert _with_tracestate(trace, [f"{boundary_key}=value"]).tracestate == f"{boundary_key}=value"


@pytest.mark.parametrize("key", ["a", "a_b-*/", "1@a", "tenant_1@system-2"])
def test_tracestate_accepts_valid_simple_and_multi_tenant_key_grammar(key):
    trace = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert trace is not None
    tracestate = f"{key}=value"
    assert _with_tracestate(trace, [tracestate]).tracestate == tracestate


@pytest.mark.parametrize(
    "value",
    [
        "Uppercase=value",
        "1key=value",
        "-key=value",
        "key=",
        "key=value=extra",
        "key=value\n",
        "key=value\tinside",
        "key=välue",
        f"{'a' * 257}=value",
        f"{'1' * 242}@system=value",
        f"tenant@{'s' * 15}=value",
        "Tenant@system=value",
        "tenant@System=value",
        "tenant@1system=value",
        "tenant@system@extra=value",
        "one=1,one=2",
        ",invalid member",
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


@pytest.mark.parametrize("value", ["!", "+", "-", "<", ">", "~", "A", "a b"])
def test_tracestate_accepts_printable_value_boundaries(value):
    trace = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert trace is not None
    tracestate = f"key={value}"
    assert _with_tracestate(trace, [tracestate]).tracestate == tracestate


def test_tracestate_accepts_256_character_value_and_rejects_257():
    trace = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert trace is not None
    maximum = f"key={'v' * 256}"
    assert _with_tracestate(trace, [maximum]).tracestate == maximum
    assert _with_tracestate(trace, [f"key={'v' * 257}"]).tracestate is None


def test_trace_context_is_immutable():
    trace = parse_traceparent(f"00-{TRACE_ID}-{PARENT_ID}-01")
    assert trace is not None
    with pytest.raises(FrozenInstanceError):
        trace.trace_id = "changed"  # ty: ignore[invalid-assignment]
