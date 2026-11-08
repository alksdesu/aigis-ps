from __future__ import annotations

from dataclasses import dataclass
import logging

from .errors import ProtocolError
from .player_state import barracks_index
from .request_fields import fields, invalid, values


UNIT_REFERENCE_COLUMNS = ("STORAGETYPE", "STORAGEINDEX", "INVITEID", "UNIQUEID", "CARDID", "A7", "AB")
BARRACKS_STORAGE, SILVER_HOUSE_STORAGE, FAIRY_FARM_STORAGE = 0, 1, 2
FIRST_BARRACKS = 0
log = logging.getLogger("aigis_ps")


@dataclass(frozen=True)
class UnitReference:
    storage_type: int
    storage_index: int
    invite_id: int
    unique_id: int
    card_id: int


def resolve_barracks_material(reference, owned, state, capacity, *, operation):
    if reference.storage_type != BARRACKS_STORAGE:
        raise ProtocolError(f"{operation}_storage_unsupported", "Expected an ordinary barracks material", 501)
    unit = owned.get(reference.invite_id)
    if unit is None or unit["UniqueID"] != reference.unique_id or unit["CardID"] != reference.card_id:
        raise ProtocolError(f"{operation}_unit_missing", "Material identity does not match an owned unit", 409)
    actual = barracks_index(unit)
    if reference.storage_index != actual:
        raise ProtocolError(f"{operation}_storage_mismatch", "The material moved from its requested barracks", 409)
    usage = capacity.usage(state)
    if actual >= len(usage.capacities):
        raise ProtocolError(f"{operation}_storage_locked", "The material barracks is unavailable", 409)
    return unit


def debit_stored_materials(warehouse, state, counts, *, storage_type, label, registration):
    try:
        return warehouse.debit(state, counts)
    except ProtocolError as error:
        # Warehouse debits refuse a shortage with 409 only after their stored rows are validated.
        if error.status == 409:
            held = {row["CardID"]: row["Count"] for row in warehouse.read(state)}
            for card_id, requested in sorted(counts.items()):
                if requested > held.get(card_id, 0):
                    log.warning("%s %d selected %d of card %d from storage type %d, which holds %d",
                                label, registration, requested, card_id, storage_type, held.get(card_id, 0))
        raise


def parse_unit_references(node, maximum: int) -> tuple[UnitReference, ...]:
    columns = fields(node, UNIT_REFERENCE_COLUMNS, UNIT_REFERENCE_COLUMNS)
    vectors = tuple(values(columns[name], "I", minimum=1, maximum=maximum) for name in UNIT_REFERENCE_COLUMNS)
    if len({len(vector) for vector in vectors}) != 1:
        invalid("Unit-reference columns must have equal lengths")
    result = []
    for storage_type, storage_index, invite_id, unique_id, card_id, a7, ab in zip(*vectors):
        if a7 != invite_id or ab != unique_id:
            invalid("Unit-reference aliases must match their canonical identities")
        if storage_index < 0 or invite_id < 0 or unique_id < 0 or card_id <= 0:
            invalid("Unit-reference identifiers are outside their native range")
        if storage_type == BARRACKS_STORAGE:
            if unique_id == 0:
                invalid("Owned units require a positive UniqueID")
        elif storage_index != 0 or invite_id != 0 or unique_id != 0:
            invalid("Aggregate storage references require zero instance identifiers")
        result.append(UnitReference(storage_type, storage_index, invite_id, unique_id, card_id))
    return tuple(result)
