from __future__ import annotations

from dataclasses import dataclass

from .player_state import MAX_PLAYER_UNITS
from .protocol import RequestEnvelope
from .request_fields import invalid, registered
from .unit_reference import BARRACKS_STORAGE, UnitReference, parse_unit_references


MANUAL_SALE, AUTOMATIC_SALE = 3, 4
SALE_REGISTRATIONS = (MANUAL_SALE, AUTOMATIC_SALE)
MAX_MANUAL_SALE_UNITS = 10


@dataclass(frozen=True)
class SaleRequest:
    registration: int
    units: tuple[UnitReference, ...]


def parse_sale(envelope: RequestEnvelope) -> SaleRequest:
    if envelope.profile.registration_keys not in ((MANUAL_SALE,), (AUTOMATIC_SALE,)):
        invalid("Expected a native unit-sale operation")
    request = registered(envelope, SALE_REGISTRATIONS, ("APPEND",), ("APPEND",))
    registration = envelope.profile.registration_keys[0]
    maximum = MAX_MANUAL_SALE_UNITS if registration == MANUAL_SALE else MAX_PLAYER_UNITS
    units = parse_unit_references(request["APPEND"], maximum)
    invite_ids, unique_ids = set(), set()
    for unit in units:
        if unit.storage_type != BARRACKS_STORAGE:
            continue
        if unit.invite_id in invite_ids or unit.unique_id in unique_ids:
            invalid("Sale units contain a repeated owned-unit identity")
        invite_ids.add(unit.invite_id)
        unique_ids.add(unit.unique_id)
    return SaleRequest(registration, units)
