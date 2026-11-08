from __future__ import annotations

from dataclasses import dataclass

from .protocol import RequestEnvelope
from .request_fields import fields, invalid, registered, scalar
from .unit_reference import BARRACKS_STORAGE, UnitReference, parse_unit_references as _references


MAX_TRAINING_MATERIALS = 10


@dataclass(frozen=True)
class TrainingRequest:
    base: UnitReference
    materials: tuple[UnitReference, ...]
    boost_card_id: int
    boost_get_index: int
    boost_percent: int


def parse_training(envelope: RequestEnvelope) -> TrainingRequest:
    if envelope.profile.registration_keys != (5,):
        invalid("Expected the native unit-integration operation")
    request = registered(envelope, (5,), ("BASE", "APPEND", "CMB"), ("BASE", "APPEND", "CMB"))
    base = _references(request["BASE"], 1)[0]
    materials = _references(request["APPEND"], MAX_TRAINING_MATERIALS)
    invite_ids, unique_ids = set(), set()
    for material in materials:
        if material.storage_type != BARRACKS_STORAGE:
            continue
        if material.invite_id in invite_ids or material.unique_id in unique_ids:
            invalid("Training materials contain a repeated owned-unit identity")
        if (base.storage_type == BARRACKS_STORAGE
                and (material.invite_id == base.invite_id or material.unique_id == base.unique_id)):
            invalid("The training target cannot also be a material")
        invite_ids.add(material.invite_id)
        unique_ids.add(material.unique_id)
    boost = fields(request["CMB"], ("A1", "A7", "B0"), ("A1", "A7", "B0"))
    card_id, get_index, percent = (scalar(boost[name], "I") for name in ("A1", "A7", "B0"))
    if card_id < 0 or get_index < -1 or percent < 0:
        invalid("Training boost identifiers and percentage are outside their native range")
    return TrainingRequest(base, materials, card_id, get_index, percent)
