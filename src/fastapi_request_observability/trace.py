"""W3C Trace Context parsing."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum

_BASE_TRACEPARENT_LENGTH = 55
_MAX_TRACEPARENT_LENGTH = 512
_MAX_TRACESTATE_LENGTH = 512
_MAX_TRACESTATE_MEMBERS = 32
_MAX_TRACESTATE_KEY_LENGTH = 256
_MAX_TRACESTATE_TENANT_ID_LENGTH = 241
_MAX_TRACESTATE_SYSTEM_ID_LENGTH = 14
_MAX_TRACESTATE_VALUE_LENGTH = 256
_TRACESTATE_KEY_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-*/")
_TRACESTATE_LEVEL_2_KEY_CHARACTERS = _TRACESTATE_KEY_CHARACTERS | {"@"}
_ASCII_SPACE = 0x20
_TRACESTATE_VALUE_FIRST_RANGE_START = 0x21
_TRACESTATE_VALUE_FIRST_RANGE_END = 0x2B
_TRACESTATE_VALUE_SECOND_RANGE_START = 0x2D
_TRACESTATE_VALUE_SECOND_RANGE_END = 0x3C
_TRACESTATE_VALUE_THIRD_RANGE_START = 0x3E
_TRACESTATE_VALUE_THIRD_RANGE_END = 0x7E


class TraceContextLevel(IntEnum):
    """Select W3C Trace Context grammar and flag semantics."""

    LEVEL_1 = 1
    LEVEL_2 = 2


def resolve_trace_context_level(value: TraceContextLevel | int | None) -> TraceContextLevel:
    """Resolve an omitted level to Level 1 and reject unsupported values."""
    if value is None:
        return TraceContextLevel.LEVEL_1
    if isinstance(value, bool):
        raise TypeError("unsupported trace context level; expected 1 or 2")
    try:
        return TraceContextLevel(value)
    except (TypeError, ValueError) as error:
        raise ValueError("unsupported trace context level; expected 1 or 2") from error


@dataclass(frozen=True, slots=True)
class TraceContext:
    """A validated incoming W3C ``traceparent`` value."""

    trace_id: str
    parent_id: str
    flags: str
    sampled: bool
    traceparent: str
    tracestate: str | None = None
    trace_context_level: TraceContextLevel = TraceContextLevel.LEVEL_1
    trace_id_random: bool | None = None


def parse_traceparent(
    value: str,
    trace_context_level: TraceContextLevel | int | None = None,
) -> TraceContext | None:
    """Parse a W3C traceparent without creating any tracing state."""
    resolved_level = resolve_trace_context_level(trace_context_level)
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if len(value) < _BASE_TRACEPARENT_LENGTH or encoded_length > _MAX_TRACEPARENT_LENGTH:
        return None
    if value[2] != "-" or value[35] != "-" or value[52] != "-":
        return None

    version = value[:2]
    if not _is_lower_hex(version) or version == "ff":
        return None
    if version == "00" and len(value) != _BASE_TRACEPARENT_LENGTH:
        return None
    if len(value) > _BASE_TRACEPARENT_LENGTH and value[_BASE_TRACEPARENT_LENGTH] != "-":
        return None

    trace_id = value[3:35]
    parent_id = value[36:52]
    flags = value[53:55]
    if not all(_is_lower_hex(part) for part in (trace_id, parent_id, flags)):
        return None
    if _is_all_zero(trace_id) or _is_all_zero(parent_id):
        return None

    return TraceContext(
        trace_id=trace_id,
        parent_id=parent_id,
        flags=flags,
        sampled=bool(int(flags, 16) & 0x01),
        traceparent=value,
        trace_context_level=resolved_level,
        trace_id_random=(
            bool(int(flags, 16) & 0x02) if resolved_level is TraceContextLevel.LEVEL_2 and version == "00" else None
        ),
    )


def _with_tracestate(trace: TraceContext, values: list[str]) -> TraceContext:
    if not values:
        return trace
    tracestate = ",".join(values)
    if not tracestate.isascii() or len(tracestate) > _MAX_TRACESTATE_LENGTH:
        return trace
    valid, canonical = _parse_tracestate(tracestate, trace.trace_context_level)
    return replace(trace, tracestate=canonical) if valid else trace


def _parse_tracestate(value: str, level: TraceContextLevel) -> tuple[bool, str]:
    members = value.split(",")
    if len(members) > _MAX_TRACESTATE_MEMBERS:
        return False, ""

    keys: set[str] = set()
    canonical_members: list[str] = []
    for raw_member in members:
        member = raw_member.strip(" \t")
        if not member:
            canonical_members.append("")
            continue
        if member.count("=") != 1:
            return False, ""
        key, opaque_value = member.split("=", 1)
        if key in keys or not _valid_tracestate_key(key, level) or not _valid_tracestate_value(opaque_value):
            return False, ""
        keys.add(key)
        canonical_members.append(f"{key}={opaque_value}")
    return True, ",".join(canonical_members)


def _valid_tracestate_key(key: str, level: TraceContextLevel) -> bool:
    if level is TraceContextLevel.LEVEL_2:
        return (
            1 <= len(key) <= _MAX_TRACESTATE_KEY_LENGTH
            and key[0] in "abcdefghijklmnopqrstuvwxyz0123456789"
            and all(character in _TRACESTATE_LEVEL_2_KEY_CHARACTERS for character in key)
        )
    if "@" not in key:
        return (
            1 <= len(key) <= _MAX_TRACESTATE_KEY_LENGTH
            and key[0] in "abcdefghijklmnopqrstuvwxyz"
            and all(character in _TRACESTATE_KEY_CHARACTERS for character in key)
        )

    if key.count("@") != 1:
        return False
    tenant_id, system_id = key.split("@", 1)
    return (
        1 <= len(tenant_id) <= _MAX_TRACESTATE_TENANT_ID_LENGTH
        and tenant_id[0] in "abcdefghijklmnopqrstuvwxyz0123456789"
        and all(character in _TRACESTATE_KEY_CHARACTERS for character in tenant_id)
        and 1 <= len(system_id) <= _MAX_TRACESTATE_SYSTEM_ID_LENGTH
        and system_id[0] in "abcdefghijklmnopqrstuvwxyz"
        and all(character in _TRACESTATE_KEY_CHARACTERS for character in system_id)
    )


def _valid_tracestate_value(value: str) -> bool:
    if not 1 <= len(value) <= _MAX_TRACESTATE_VALUE_LENGTH:
        return False
    return _is_tracestate_value_character(value[-1], allow_space=False) and all(
        _is_tracestate_value_character(character, allow_space=True) for character in value
    )


def _is_tracestate_value_character(character: str, *, allow_space: bool) -> bool:
    codepoint = ord(character)
    return (
        (allow_space and codepoint == _ASCII_SPACE)
        or _TRACESTATE_VALUE_FIRST_RANGE_START <= codepoint <= _TRACESTATE_VALUE_FIRST_RANGE_END
        or _TRACESTATE_VALUE_SECOND_RANGE_START <= codepoint <= _TRACESTATE_VALUE_SECOND_RANGE_END
        or _TRACESTATE_VALUE_THIRD_RANGE_START <= codepoint <= _TRACESTATE_VALUE_THIRD_RANGE_END
    )


def _is_lower_hex(value: str) -> bool:
    return bool(value) and all(character in "0123456789abcdef" for character in value)


def _is_all_zero(value: str) -> bool:
    return all(character == "0" for character in value)
