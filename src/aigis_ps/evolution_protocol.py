from __future__ import annotations

from dataclasses import dataclass

from .protocol import RequestEnvelope
from .request_fields import invalid, registered, scalar
from .unit_reference import BARRACKS_STORAGE, UnitReference, parse_unit_references


MAX_EVOLUTION_MATERIALS = 4


@dataclass(frozen=True)
class EvolutionRequest:
    operation: int
    base: UnitReference
    materials: tuple[UnitReference, ...]
    branch: int


def parse_evolution(envelope: RequestEnvelope) -> EvolutionRequest:
    if envelope.profile.registration_keys not in ((7,), (8,), (9,)):
        invalid("Expected a native awakening operation")
    operation = envelope.profile.registration_keys[0]
    business = ("BASE", "APPEND", "TYPE") if operation == 9 else ("BASE", "APPEND")
    required = ("BASE", "TYPE") if operation == 9 else ("BASE",)
    request = registered(envelope, (7, 8, 9), business, required)
    base = parse_unit_references(request["BASE"], 1)[0]
    materials = parse_unit_references(request["APPEND"], MAX_EVOLUTION_MATERIALS) if "APPEND" in request else ()
    invite_ids, unique_ids = set(), set()
    for material in materials:
        if material.storage_type != BARRACKS_STORAGE:
            continue
        if material.invite_id in invite_ids or material.unique_id in unique_ids:
            invalid("Awakening materials contain a repeated owned-unit identity")
        if (base.storage_type == BARRACKS_STORAGE
                and (material.invite_id == base.invite_id or material.unique_id == base.unique_id)):
            invalid("The awakening target cannot also be a material")
        invite_ids.add(material.invite_id)
        unique_ids.add(material.unique_id)
    branch = scalar(request["TYPE"], "I") if operation == 9 else 0
    if operation == 9 and branch not in (1, 2):
        invalid("Second awakening requires branch one or two")
    return EvolutionRequest(operation, base, materials, branch)
