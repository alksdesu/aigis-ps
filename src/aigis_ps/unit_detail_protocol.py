from __future__ import annotations

from dataclasses import dataclass

from .request_fields import fields, invalid, registered, scalar


@dataclass(frozen=True)
class UnitDetailRequest:
    get_index: int
    display_type: int
    toggle_skill: bool


def parse_unit_detail(envelope) -> UnitDetailRequest:
    if envelope.profile.registration_keys != (10,):
        invalid("Expected the native unit-detail operation")
    request = registered(envelope, (10,), ("PU",), ("PU",))
    selection = fields(request["PU"], ("A7", "B0", "SC"), ("A7", "B0", "SC"))
    index, display, toggle = (scalar(selection[name], "I") for name in ("A7", "B0", "SC"))
    if index < 0 or toggle not in (0, 1):
        invalid("Unit-detail selection requires a nonnegative index and a zero-or-one switch")
    return UnitDetailRequest(index, display, bool(toggle))
