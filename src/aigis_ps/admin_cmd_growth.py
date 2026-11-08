from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .admin_cmd_units import UNITS, CardUnitParams, distinct_units, protection_details, summary
from .admin_command import (AdminCommand, CommandContext, CommandParams, CommandResult, counted, required_service,
                            requires)
from .admin_fields import EVOLUTION_REWARDS, GIFTS
from .admin_registry import CommandRegistry
from .evolution_protocol import MAX_EVOLUTION_MATERIALS, EvolutionRequest
from .evolution_service import EVOLUTION_FIELDS, FIRST_AWAKENING, SECOND_AWAKENING, SKILL_AWAKENING
from .native_numbers import INT32_MAX
from .player_state import barracks_index
from .training_protocol import MAX_TRAINING_MATERIALS, TrainingRequest
from .training_service import GROWTH_FIELDS, MaterialsProtected
from .unit_reference import FAIRY_FARM_STORAGE, FIRST_BARRACKS, SILVER_HOUSE_STORAGE, UnitReference
from .wallet_money import money_balance


EVOLUTION, TRAINING = "evolution", "training"
GAME_PARAM, POSSESSIONS, MISSIONS, FAIRY, SILVER, PRESENTS, CARD_STATUS = (
    "game_param", "possessions", "missions", "fairy", "silver", "presents", "card_status")
# Aggregate warehouse references carry no barracks, GetIndex or UniqueID.
NO_INSTANCE = 0
WAREHOUSE_STORAGE = MappingProxyType({"silver_house": SILVER_HOUSE_STORAGE, "fairy_farm": FAIRY_FARM_STORAGE})
# Second awakenings pick AwakeType1 or AwakeType2; the other operations use branch zero.
MAX_AWAKENING_BRANCH = 2
SECOND_AWAKENING_BRANCHES = frozenset(range(1, MAX_AWAKENING_BRANCH + 1))
MATERIAL_SUBJECT = "Target and barracks material"


class EvolutionOperation(StrEnum):
    FIRST_AWAKENING = "first_awakening"
    SKILL_AWAKENING = "skill_awakening"
    SECOND_AWAKENING = "second_awakening"


EVOLUTION_REGISTRATIONS = MappingProxyType({EvolutionOperation.FIRST_AWAKENING: FIRST_AWAKENING,
                                            EvolutionOperation.SKILL_AWAKENING: SKILL_AWAKENING,
                                            EvolutionOperation.SECOND_AWAKENING: SECOND_AWAKENING})


class HeldMaterial(CardUnitParams):
    source: Literal["barracks"]


class WarehouseMaterial(CommandParams):
    # Warehouse sources name the growth services' own warehouse attributes.
    source: Literal["silver_house", "fairy_farm"]
    card_id: int = Field(ge=1, le=INT32_MAX)

    def reference(self) -> UnitReference:
        return UnitReference(WAREHOUSE_STORAGE[self.source], NO_INSTANCE, NO_INSTANCE, NO_INSTANCE, self.card_id)


GrowthMaterial = Annotated[HeldMaterial | WarehouseMaterial, Field(discriminator="source")]


def require_single_use(target: CardUnitParams, materials: Iterable[GrowthMaterial]) -> None:
    distinct_units((target, *(material for material in materials if isinstance(material, HeldMaterial))),
                   MATERIAL_SUBJECT)


class EvolveParams(CommandParams):
    target: CardUnitParams
    operation: EvolutionOperation
    branch: int = Field(default=0, ge=0, le=MAX_AWAKENING_BRANCH)
    materials: list[GrowthMaterial] = Field(min_length=1, max_length=MAX_EVOLUTION_MATERIALS)

    @model_validator(mode="after")
    def _native_request(self) -> EvolveParams:
        if (self.operation == EvolutionOperation.SECOND_AWAKENING) != (self.branch in SECOND_AWAKENING_BRANCHES):
            raise ValueError("Second awakenings pick branch 1 or 2, and the other operations use branch 0")
        require_single_use(self.target, self.materials)
        return self


class TrainParams(CommandParams):
    target: CardUnitParams
    materials: list[GrowthMaterial] = Field(min_length=1, max_length=MAX_TRAINING_MATERIALS)

    @model_validator(mode="after")
    def _native_request(self) -> TrainParams:
        require_single_use(self.target, self.materials)
        return self


def material_reference(owned: Mapping[int, Mapping], material: GrowthMaterial) -> UnitReference:
    if isinstance(material, WarehouseMaterial):
        return material.reference()
    unit = owned.get(material.get_index)
    # Like the native client, a barracks material names the barracks it is listed in.
    return material.reference(FIRST_BARRACKS if unit is None else barracks_index(unit))


def references(player: Mapping, materials: Iterable[GrowthMaterial]) -> tuple[UnitReference, ...]:
    owned = {unit["GetIndex"]: unit for unit in player["units"]}
    return tuple(material_reference(owned, material) for material in materials)


def changes(before: Mapping, after: Mapping, fields: Iterable[str]) -> dict:
    return {name: {"before": before[name], "after": after[name]} for name in fields if before[name] != after[name]}


def debited(stored: Mapping[int, Mapping[int, int]]) -> dict:
    return {source: [{"card_id": card_id, "count": count} for card_id, count in sorted(stored[storage].items())]
            for source, storage in WAREHOUSE_STORAGE.items()}


def reward_presents(rewards, before: dict, after: dict) -> list[int]:
    # Only a first reward mails anything, and its enqueue has already validated both listings.
    if before["tables"].get("presents") == after["tables"].get("presents"):
        return []
    known = {row["PresentID"] for row in rewards.presents.rows(before)}
    return [row["PresentID"] for row in rewards.presents.rows(after) if row["PresentID"] not in known]


def evolve(state: dict, params: EvolveParams, context: CommandContext) -> CommandResult:
    service = required_service(context, EVOLUTION)
    operation = EVOLUTION_REGISTRATIONS[params.operation]
    request = EvolutionRequest(operation, params.target.reference(),
                               references(service.players.validate(state), params.materials), params.branch)
    with protection_details(MaterialsProtected):
        outcome = service.awaken(state, context.revision, request, now=context.now)
    effects = {"operation": str(params.operation), "target": summary(outcome.after),
               "changes": changes(outcome.before, outcome.after, EVOLUTION_FIELDS[operation]),
               "price": outcome.price, "money": {"before": money_balance(state).money, "after": outcome.money},
               "items": dict(outcome.costs), "debited": debited(outcome.stored),
               "consumed": [summary(unit) for unit in outcome.consumed],
               "reward_presents": reward_presents(service.rewards, state, outcome.state),
               "last_unique_id": outcome.state["player_state"]["last_unique_id"]}
    return CommandResult(outcome.state, effects)


def train(state: dict, params: TrainParams, context: CommandContext) -> CommandResult:
    service = required_service(context, TRAINING)
    catalog, player = service.catalog, service.players.validate(state)
    # The console trains with the bonus the native menu offers, which is always the largest owned one.
    source = catalog.ownership_bonus(player, state).source
    request = TrainingRequest(params.target.reference(), references(player, params.materials), source.card_id,
                              source.get_index, source.percent)
    with protection_details(MaterialsProtected):
        outcome = service.train(state, context.revision, request, now=context.now)
    levels = {"before": catalog.describe(outcome.before).level, "after": catalog.describe(outcome.after).level}
    effects = {"target": summary(outcome.after), "changes": changes(outcome.before, outcome.after, GROWTH_FIELDS),
               "level": levels, "bonus": asdict(source), "price": outcome.price,
               "money": {"before": money_balance(state).money, "after": outcome.money},
               "debited": debited({SILVER_HOUSE_STORAGE: outcome.silver, FAIRY_FARM_STORAGE: outcome.fairy}),
               "consumed": [summary(unit) for unit in outcome.consumed],
               "last_unique_id": outcome.state["player_state"]["last_unique_id"]}
    return CommandResult(outcome.state, effects)


def evolve_phrase(params: EvolveParams) -> str:
    return f"EVOLVE UNIT {params.target.get_index} WITH {counted(len(params.materials), 'MATERIAL')}"


def train_phrase(params: TrainParams) -> str:
    return f"TRAIN UNIT {params.target.get_index} WITH {counted(len(params.materials), 'MATERIAL')}"


EVOLVE = AdminCommand("units.evolve", UNITS, EvolveParams, evolve,
                      frozenset({UNITS, GAME_PARAM, POSSESSIONS, GIFTS, MISSIONS, FAIRY, SILVER, PRESENTS, CARD_STATUS,
                                 EVOLUTION_REWARDS}),
                      destructive=True, confirm_phrase=evolve_phrase, unavailable=requires(EVOLUTION))
TRAIN = AdminCommand("units.train", UNITS, TrainParams, train,
                     frozenset({UNITS, GAME_PARAM, GIFTS, MISSIONS, FAIRY, SILVER}),
                     destructive=True, confirm_phrase=train_phrase, unavailable=requires(TRAINING))
COMMANDS = (EVOLVE, TRAIN)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
