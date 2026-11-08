from collections.abc import Callable
from copy import copy, deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cache
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from aigis_ps import admin_cmd_growth, admin_views_tools
from aigis_ps.admin_cmd_growth import EVOLUTION_REGISTRATIONS, EVOLVE, TRAIN
from aigis_ps.admin_command import AdminError, PlanStore, missing_service
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, ViewRegistry, discover
from aigis_ps.admin_routes_state import describe_command, view_params
from aigis_ps.alxml import Column, Container
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.evolution_protocol import MAX_EVOLUTION_MATERIALS
from aigis_ps.player_state import BARRACKS_SHIFT, LOCK_FLAG, barracks_index
from aigis_ps.storage import AUTO_BACKUP_KIND
from aigis_ps.training_catalog import TrainingBoost
from aigis_ps.training_math import integration_price
from aigis_ps.training_protocol import MAX_TRAINING_MATERIALS, TrainingRequest
from aigis_ps.unit_removal import Protection
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_admin_preflight import none_reasons, unread_reasons
from test_admin_seams import MemoryStore
from test_evolution_catalog import materials as slot_materials, unit as catalog_unit
from test_quest_service import bundle, request


GOLDEN = Path(__file__).resolve().parent / "golden" / "p23a"
GOLDEN_NOW = datetime(2026, 11, 3, 12, tzinfo=JST)
STARTING_MONEY = 1_000_000
FIRST_AWAKENING, SKILL_AWAKENING, SECOND_AWAKENING, INTEGRATION = 7, 8, 9, 5
BARRACKS, SILVER_HOUSE, FAIRY_FARM = 0, 1, 2
SECOND_BARRACKS = 1
SECOND_BARRACKS_FLAG = SECOND_BARRACKS << BARRACKS_SHIFT
FORGED_IDENTITY = 999999
REFERENCE_COLUMNS = ("STORAGETYPE", "STORAGEINDEX", "INVITEID", "UNIQUEID", "CARDID", "A7", "AB")
BONUS_COLUMNS = ("A1", "A7", "B0")
UNIT_COLUMNS = ("GetIndex", "UniqueID", "CardID", "ClassID", "ClassChangeCount", "Exp", "Love", "SkillLevel",
                "SkillExperience", "CostModMixValue", "CostExperience", "SettingSkillID", "GeneralPurposeFlags",
                "SkillEvolved", "_DisplayType")
SKILL_FAIRY, WRONG_FAIRY = 234, 136
REWARD_CARD = 17
TRAINING_CARD, FAIRY_TARGET_CARD, FARM_FAIRY, SUPPORT_CARD = 3, 8, 56, 416
SUPPORT_BONUS = 10
NO_BONUS = (0, 0, 0)
# The fixture squad row sits in a standard team, so its member counts as organized.
SQUAD_ADDRESS = 0x70000
# A RankUp rank-one experience keeps the native startup check satisfied once all money is spent.
RANK_ONE_EXPERIENCE = 5
UNSUPPORTED_HISTORY = {"schema_version": 99, "receipts": []}
LEGACY_TRAINING_JOURNAL = {"schema_version": 2, "receipts": [], "last_client_time": 5000, "seen_keys": []}


@dataclass(frozen=True)
class Reference:
    storage_type: int
    storage_index: int
    invite_id: int
    unique_id: int
    card_id: int


@dataclass
class Seeded:
    state: dict
    target: dict
    materials: list[dict]
    prince: dict
    base: Reference
    append: list[Reference]
    bonus: tuple[int, int, int] = NO_BONUS


@dataclass(frozen=True)
class GrowthCase:
    number: int
    seed: Callable[[object], Seeded]
    prepare: tuple[Callable[[object, Seeded], None], ...] = ()
    branch: int = 0
    replay: bool = False
    stale: bool = False


def held(unit: dict, **changes) -> Reference:
    barracks = barracks_index(unit)
    return replace(Reference(BARRACKS, barracks, unit["GetIndex"], unit["UniqueID"], unit["CardID"]), **changes)


def stored(storage_type: int, card_id: int) -> Reference:
    return Reference(storage_type, 0, 0, 0, card_id)


def reference_columns(references: list[Reference]) -> tuple[Column, ...]:
    rows = [(item.storage_type, item.storage_index, item.invite_id, item.unique_id, item.card_id, item.invite_id,
             item.unique_id) for item in references]
    return tuple(Column(name, "I", tuple(row[index] for row in rows)) for index, name in enumerate(REFERENCE_COLUMNS))


def production(bundle, state: dict) -> dict:
    # The server initializes the awakening ledger at startup, so every native request already sees it.
    return bundle.evolution_rewards.initialize(state)


def stocked(state: dict, stock: dict[str, list[dict]]) -> dict:
    for table, rows in stock.items():
        state[f"{table}_schema"] = 1
        state["tables"][table] = deepcopy(rows)
    return state


def seeded_units(bundle, rows: list[dict]) -> tuple[dict, list[dict]]:
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    units = [deepcopy(row) for row in rows]
    for index, row in enumerate(units, 1):
        row.update(GetIndex=index, UniqueID=index + 1)
    player = state["player_state"]
    player["units"].extend(units)
    player["battle"]["UX"] = len(units)
    player["last_unique_id"] = len(units) + 1
    state["tables"]["game_param"][0]["Money"] = STARTING_MONEY
    return state, units


def with_orbs(bundle, state: dict, target: dict, materials: list[dict], operation: int, branch: int = 0) -> dict:
    quote = bundle.evolution.catalog.quote(target, materials, operation, branch)
    return bundle.possessions.credit(state, {name: count * 2 for name, count in quote.items.items()})


def awakening(bundle, operation: int, target: dict, quoted: list[dict], barracks: list[dict], append, *,
              branch: int = 0, stock: dict[str, list[dict]] | None = None) -> Seeded:
    state, units = seeded_units(bundle, [target, *barracks])
    state = stocked(with_orbs(bundle, state, units[0], quoted, operation, branch), stock or {})
    return Seeded(production(bundle, state), units[0], units[1:], state["player_state"]["units"][0], held(units[0]),
                  append(units[1:]))


def all_held(units: list[dict]) -> list[Reference]:
    return [held(unit) for unit in units]


def first_awakening_units(bundle) -> tuple[dict, list[dict]]:
    catalog = bundle.evolution.catalog
    target = catalog_unit(catalog, level=999)
    return target, slot_materials(catalog, target, FIRST_AWAKENING)


def first_awakening(bundle) -> Seeded:
    target, materials = first_awakening_units(bundle)
    return awakening(bundle, FIRST_AWAKENING, target, materials, materials, all_held)


def silver_slot_card(bundle, target: dict) -> int:
    catalog = bundle.evolution.catalog
    slot = catalog.materials.slots(catalog.growth.describe(target), FIRST_AWAKENING)[0]
    rarity = catalog.cards.card(target["CardID"])["Rare"]
    return next(card_id for card_id in sorted(bundle.silver_house._card_ids)
                if catalog.cards.card(card_id)["Rare"] == rarity and catalog.cards.card(card_id)["InitClassID"] // 100
                and any(condition._matches((card_id, catalog.cards.card(card_id)["InitClassID"], rarity, 0, 30))
                        for condition in slot))


def first_awakening_from_silver(bundle) -> Seeded:
    target, materials = first_awakening_units(bundle)
    silver = silver_slot_card(bundle, target)
    return awakening(bundle, FIRST_AWAKENING, target, materials, materials[1:],
                     lambda units: [stored(SILVER_HOUSE, silver), *all_held(units)],
                     stock={"silver_house": [{"CardID": silver, "Count": 1}]})


def skill_awakening_units(bundle) -> tuple[dict, list[dict]]:
    catalog = bundle.evolution.catalog
    target = catalog_unit(catalog, stage=2)
    target["SkillLevel"] = catalog.growth.describe(target).skill_max
    return target, [catalog_unit(catalog, SKILL_FAIRY)]


def skill_awakening(bundle) -> Seeded:
    target, fairy = skill_awakening_units(bundle)
    return awakening(bundle, SKILL_AWAKENING, target, fairy, fairy, all_held)


def skill_awakening_with_fairy(card_id: int, count: int, storage_type: int = FAIRY_FARM) -> Callable[[object], Seeded]:
    def seed(bundle) -> Seeded:
        target, fairy = skill_awakening_units(bundle)
        return awakening(bundle, SKILL_AWAKENING, target, fairy, [], lambda units: [stored(storage_type, card_id)],
                         stock={"fairy_farm": [{"CardID": card_id, "Count": count}]})
    return seed


def second_awakening(bundle) -> Seeded:
    catalog = bundle.evolution.catalog
    target = catalog_unit(catalog, REWARD_CARD, stage=2)
    target["Exp"] = 123
    materials = slot_materials(catalog, target, SECOND_AWAKENING)
    return awakening(bundle, SECOND_AWAKENING, target, materials, materials, all_held, branch=1)


def training(bundle, rows: list[dict], *, consumed: slice = slice(1, None),
             append: Callable[[list[dict]], list[Reference]] = all_held,
             stock: dict[str, list[dict]] | None = None) -> Seeded:
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state, units = bundle.player_state.credit_units(state, rows)
    state["tables"]["game_param"][0]["Money"] = STARTING_MONEY
    state = stocked(state, stock or {})
    materials = units[consumed]
    return Seeded(production(bundle, state), units[0], materials, state["player_state"]["units"][0], held(units[0]),
                  append(materials))


def same_card_training(bundle) -> Seeded:
    return training(bundle, [{"card_id": TRAINING_CARD, "level": 1, "count": 3}])


def farm_training(count: int) -> Callable[[object], Seeded]:
    def seed(bundle) -> Seeded:
        return training(bundle, [{"card_id": FAIRY_TARGET_CARD, "level": 1, "count": 1}],
                        append=lambda units: [stored(FAIRY_FARM, FARM_FAIRY)] * 2,
                        stock={"fairy_farm": [{"CardID": FARM_FAIRY, "Count": count}]})
    return seed


def silver_training(bundle) -> Seeded:
    silver = silver_slot_card(bundle, first_awakening_units(bundle)[0])
    return training(bundle, [{"card_id": TRAINING_CARD, "level": 1, "count": 1}],
                    append=lambda units: [stored(SILVER_HOUSE, silver)],
                    stock={"silver_house": [{"CardID": silver, "Count": 2}]})


def supported_training(bundle) -> Seeded:
    seeded = training(bundle, [{"card_id": TRAINING_CARD, "level": 1, "count": 2},
                               {"card_id": SUPPORT_CARD, "level": 1, "count": 1}], consumed=slice(1, 2))
    support = next(unit for unit in seeded.state["player_state"]["units"] if unit["CardID"] == SUPPORT_CARD)
    seeded.bonus = (SUPPORT_CARD, support["GetIndex"], SUPPORT_BONUS)
    return seeded


def unit_in(state: dict, get_index: int) -> dict:
    return next(row for row in state["player_state"]["units"] if row["GetIndex"] == get_index)


def owned(seeded: Seeded, unit: dict) -> dict:
    return unit_in(seeded.state, unit["GetIndex"])


def material_flags(position: int, flags: int) -> Callable[[object, Seeded], None]:
    def prepare(bundle, seeded: Seeded) -> None:
        owned(seeded, seeded.materials[position])["GeneralPurposeFlags"] |= flags
    return prepare


def target_flags(flags: int) -> Callable[[object, Seeded], None]:
    def prepare(bundle, seeded: Seeded) -> None:
        owned(seeded, seeded.target)["GeneralPurposeFlags"] |= flags
    return prepare


def squad_member(unit: Callable[[Seeded], dict]) -> Callable[[object, Seeded], None]:
    def prepare(bundle, seeded: Seeded) -> None:
        seeded.state["player_state"]["squads"].append(
            {"Address": SQUAD_ADDRESS, "UnitIndex": unit(seeded)["GetIndex"], "Name": "", "Flags": 0})
    return prepare


def material_in_squad(position: int) -> Callable[[object, Seeded], None]:
    return squad_member(lambda seeded: seeded.materials[position])


prince_in_squad = squad_member(lambda seeded: seeded.prince)


def reference(position: int | None, **changes) -> Callable[[object, Seeded], None]:
    def prepare(bundle, seeded: Seeded) -> None:
        if position is None:
            seeded.base = replace(seeded.base, **changes)
        else:
            seeded.append[position] = replace(seeded.append[position], **changes)
    return prepare


def prince_material(position: int) -> Callable[[object, Seeded], None]:
    def prepare(bundle, seeded: Seeded) -> None:
        seeded.append[position] = held(seeded.prince)
    return prepare


def repeated(source: int, position: int) -> Callable[[object, Seeded], None]:
    def prepare(bundle, seeded: Seeded) -> None:
        seeded.append[position] = seeded.append[source]
    return prepare


def prince_target(bundle, seeded: Seeded) -> None:
    seeded.base = held(seeded.prince)


def wallet(money: int | Callable[[object, Seeded], int],
           experience: int | None = None) -> Callable[[object, Seeded], None]:
    def prepare(bundle, seeded: Seeded) -> None:
        row = seeded.state["tables"]["game_param"][0]
        row["Money"] = money(bundle, seeded) if callable(money) else money
        if experience is not None:
            row["Experience"] = experience
    return prepare


def awakening_price(bundle, seeded: Seeded) -> int:
    return bundle.evolution.catalog.quote(seeded.target, seeded.materials, FIRST_AWAKENING).price


def below_awakening_price(bundle, seeded: Seeded) -> int:
    return awakening_price(bundle, seeded) - 1


def training_price(bundle, seeded: Seeded) -> int:
    level = bundle.training.catalog.describe(seeded.target).level
    return integration_price(level, len(seeded.append))


def without_orbs(bundle, seeded: Seeded) -> None:
    tables = seeded.state["tables"]
    tables["possessions"] = [row for row in tables["possessions"] if not row["ItemID"].startswith("evolve_orb_")]


def history(namespace: str, journal: dict) -> Callable[[object, Seeded], None]:
    def prepare(bundle, seeded: Seeded) -> None:
        seeded.state[namespace] = deepcopy(journal)
    return prepare


def target_values(**values) -> Callable[[object, Seeded], None]:
    def prepare(bundle, seeded: Seeded) -> None:
        owned(seeded, seeded.target).update(values)
    return prepare


def claimed(card_id: int) -> Callable[[object, Seeded], None]:
    def prepare(bundle, seeded: Seeded) -> None:
        ledger = seeded.state["evolution_rewards"]
        ledger["claimed"] = sorted({*ledger["claimed"], card_id})
    return prepare


def ordinary_materials(bundle, seeded: Seeded) -> None:
    ordinary = catalog_unit(bundle.evolution.catalog, TRAINING_CARD, level=30)
    for position, unit in enumerate(seeded.materials):
        row = owned(seeded, unit)
        row.update({name: ordinary[name] for name in ("CardID", "ClassID", "SettingSkillID", "Exp")})
        seeded.append[position] = held(row)


def dynamic_overrides(bundle, seeded: Seeded) -> None:
    seeded.state["unit_dynamic_overrides"] = {"0": {"Exp": 1}}


def bonus(card_id: int, get_index: int, percent: int) -> Callable[[object, Seeded], None]:
    def prepare(bundle, seeded: Seeded) -> None:
        seeded.bonus = (card_id, get_index, percent)
    return prepare


AWAKENING_CASES = {
    "first_awakening": GrowthCase(FIRST_AWAKENING, first_awakening, replay=True, stale=True),
    "first_awakening_from_the_silver_house": GrowthCase(FIRST_AWAKENING, first_awakening_from_silver, replay=True),
    "first_awakening_spends_all_money_with_experience": GrowthCase(
        FIRST_AWAKENING, first_awakening, (wallet(awakening_price, RANK_ONE_EXPERIENCE),)),
    "skill_awakening": GrowthCase(SKILL_AWAKENING, skill_awakening, replay=True),
    "skill_awakening_from_the_fairy_farm": GrowthCase(SKILL_AWAKENING, skill_awakening_with_fairy(SKILL_FAIRY, 1),
                                                      replay=True),
    "second_awakening_sends_the_first_reward": GrowthCase(SECOND_AWAKENING, second_awakening, branch=1, replay=True,
                                                          stale=True),
    "second_awakening_after_the_reward_was_claimed": GrowthCase(SECOND_AWAKENING, second_awakening,
                                                                (claimed(REWARD_CARD),), branch=1),
    "locked_material": GrowthCase(FIRST_AWAKENING, first_awakening, (material_flags(1, LOCK_FLAG),)),
    "squad_material": GrowthCase(FIRST_AWAKENING, first_awakening, (material_in_squad(1),)),
    "prince_material": GrowthCase(SKILL_AWAKENING, skill_awakening, (prince_material(0),)),
    "prince_target": GrowthCase(FIRST_AWAKENING, first_awakening, (prince_target,)),
    "repeated_material": GrowthCase(FIRST_AWAKENING, first_awakening, (repeated(0, 1),)),
    "forged_target_identity": GrowthCase(FIRST_AWAKENING, first_awakening,
                                         (reference(None, unique_id=FORGED_IDENTITY),)),
    "forged_material_card": GrowthCase(FIRST_AWAKENING, first_awakening, (reference(2, card_id=FORGED_IDENTITY),)),
    "target_in_the_second_barracks": GrowthCase(FIRST_AWAKENING, first_awakening,
                                                (target_flags(SECOND_BARRACKS_FLAG),)),
    "target_named_in_the_second_barracks": GrowthCase(FIRST_AWAKENING, first_awakening,
                                                      (reference(None, storage_index=SECOND_BARRACKS),)),
    "material_moved_from_its_barracks": GrowthCase(FIRST_AWAKENING, first_awakening,
                                                   (reference(1, storage_index=SECOND_BARRACKS),)),
    "material_flagged_for_a_missing_barracks": GrowthCase(FIRST_AWAKENING, first_awakening, (
        material_flags(1, SECOND_BARRACKS_FLAG), reference(1, storage_index=SECOND_BARRACKS))),
    "money_insufficient": GrowthCase(FIRST_AWAKENING, first_awakening, (wallet(below_awakening_price),)),
    "money_native_balance": GrowthCase(FIRST_AWAKENING, first_awakening, (wallet(awakening_price),)),
    "orbs_insufficient": GrowthCase(FIRST_AWAKENING, first_awakening, (without_orbs,)),
    "target_below_its_level_cap": GrowthCase(FIRST_AWAKENING, first_awakening, (target_values(Exp=0),)),
    "materials_outside_the_slots": GrowthCase(FIRST_AWAKENING, first_awakening, (ordinary_materials,)),
    "wrong_fairy": GrowthCase(SKILL_AWAKENING, skill_awakening_with_fairy(WRONG_FAIRY, 1)),
    "fairy_farm_shortage": GrowthCase(SKILL_AWAKENING, skill_awakening_with_fairy(SKILL_FAIRY, 0)),
    "fairy_named_as_a_silver_house_card": GrowthCase(SKILL_AWAKENING,
                                                     skill_awakening_with_fairy(SKILL_FAIRY, 1, SILVER_HOUSE)),
    "protected_before_money": GrowthCase(FIRST_AWAKENING, first_awakening, (material_flags(1, LOCK_FLAG), wallet(1))),
    "protected_before_target_eligibility": GrowthCase(FIRST_AWAKENING, first_awakening,
                                                      (material_flags(1, LOCK_FLAG), target_values(Exp=0))),
    "missing_before_earlier_protected": GrowthCase(FIRST_AWAKENING, first_awakening, (
        material_flags(0, LOCK_FLAG), reference(2, unique_id=FORGED_IDENTITY))),
    "unsupported_history": GrowthCase(FIRST_AWAKENING, first_awakening,
                                      (history("evolution_history", UNSUPPORTED_HISTORY),)),
    "negative_money": GrowthCase(FIRST_AWAKENING, first_awakening, (wallet(-1),)),
}
TRAINING_CASES = {
    "same_card": GrowthCase(INTEGRATION, same_card_training, replay=True, stale=True),
    "fairy_farm_materials": GrowthCase(INTEGRATION, farm_training(2), replay=True),
    "silver_house_material": GrowthCase(INTEGRATION, silver_training, replay=True),
    "ownership_bonus": GrowthCase(INTEGRATION, supported_training, replay=True),
    "ownership_bonus_changed": GrowthCase(INTEGRATION, supported_training, (bonus(*NO_BONUS),)),
    "spends_all_money_with_experience": GrowthCase(INTEGRATION, same_card_training,
                                                   (wallet(training_price, RANK_ONE_EXPERIENCE),)),
    "prince_target": GrowthCase(INTEGRATION, same_card_training, (prince_target,)),
    "locked_material": GrowthCase(INTEGRATION, same_card_training, (material_flags(0, LOCK_FLAG),)),
    "squad_material": GrowthCase(INTEGRATION, same_card_training, (material_in_squad(1),)),
    "prince_material": GrowthCase(INTEGRATION, same_card_training, (prince_material(1),)),
    "repeated_material": GrowthCase(INTEGRATION, same_card_training, (repeated(0, 1),)),
    "forged_target_identity": GrowthCase(INTEGRATION, same_card_training,
                                         (reference(None, unique_id=FORGED_IDENTITY),)),
    "forged_material_card": GrowthCase(INTEGRATION, same_card_training, (reference(0, card_id=FORGED_IDENTITY),)),
    "target_in_the_second_barracks": GrowthCase(INTEGRATION, same_card_training, (target_flags(SECOND_BARRACKS_FLAG),)),
    "material_moved_from_its_barracks": GrowthCase(INTEGRATION, same_card_training,
                                                   (reference(0, storage_index=SECOND_BARRACKS),)),
    "money_insufficient": GrowthCase(INTEGRATION, same_card_training, (wallet(1),)),
    "money_native_balance": GrowthCase(INTEGRATION, same_card_training, (wallet(training_price),)),
    "experience_capacity": GrowthCase(INTEGRATION, same_card_training, (target_values(Exp=0x7FFFFFFF),)),
    "fairy_farm_shortage": GrowthCase(INTEGRATION, farm_training(1)),
    "dynamic_unit_overrides": GrowthCase(INTEGRATION, same_card_training, (dynamic_overrides,)),
    "protected_before_money": GrowthCase(INTEGRATION, same_card_training, (material_flags(0, LOCK_FLAG), wallet(1))),
    "bonus_before_money": GrowthCase(INTEGRATION, supported_training, (bonus(*NO_BONUS), wallet(1))),
    "unsupported_history": GrowthCase(INTEGRATION, same_card_training,
                                      (history("training_history", UNSUPPORTED_HISTORY),)),
    "legacy_history_upgrades": GrowthCase(INTEGRATION, same_card_training,
                                          (history("training_history", LEGACY_TRAINING_JOURNAL),), replay=True),
    "negative_money": GrowthCase(INTEGRATION, same_card_training, (wallet(-1),)),
}
SUITES = {"awakenings": AWAKENING_CASES, "integrations": TRAINING_CASES}
NATIVE_REFUSALS = {
    "awakenings": frozenset(("evolution_material_protected", "evolution_unit_missing", "evolution_storage_unsupported",
                             "evolution_storage_mismatch", "unit_capacity_barracks_unsupported",
                             "evolution_money_insufficient", "evolution_native_balance", "possession_insufficient",
                             "evolution_target_ineligible", "evolution_retry_stale", "evolution_history_invalid",
                             "fairy_farm_quantity")),
    "integrations": frozenset(("training_material_protected", "training_target_ineligible", "training_unit_missing",
                               "training_storage_unsupported", "training_storage_mismatch",
                               "training_money_insufficient", "training_native_balance", "training_bonus_changed",
                               "training_experience_capacity", "training_recipe_unsupported", "training_retry_stale",
                               "training_history_invalid", "fairy_farm_quantity")),
}
EVOLVE_DOMAINS = frozenset(("units", "game_param", "possessions", "gifts", "missions", "fairy", "silver", "presents",
                            "card_status", "evolution_rewards"))
TRAIN_DOMAINS = frozenset(("units", "game_param", "gifts", "missions", "fairy", "silver"))
RECEIPT_NAMESPACES = {"awakenings": "evolution_history", "integrations": "training_history"}
WAREHOUSE_SOURCES = {SILVER_HOUSE: "silver_house", FAIRY_FARM: "fairy_farm"}
OPERATIONS = {registration: operation for operation, registration in EVOLUTION_REGISTRATIONS.items()}
# Console parameters derive the barracks and the bonus from the save and reject repeated units before any kernel runs.
NATIVE_REQUEST_SHAPES = frozenset(("repeated_material", "target_named_in_the_second_barracks",
                                   "material_moved_from_its_barracks", "ownership_bonus_changed", "bonus_before_money"))
# The native handlers check receipts and the GameParam row before their kernels; console commands keep no receipts.
HANDLER_CHECKS = frozenset(("unsupported_history", "legacy_history_upgrades", "negative_money"))
CONSOLE_CASES = [(suite, name) for suite, cases in SUITES.items() for name in cases
                 if name not in NATIVE_REQUEST_SHAPES | HANDLER_CHECKS]
CONSOLE_TARGET = {"get_index": 3, "unique_id": 4, "card_id": TRAINING_CARD}
CONSOLE_HELD = {"source": "barracks", "get_index": 5, "unique_id": 6, "card_id": TRAINING_CARD}
CONSOLE_FAIRY = {"source": "fairy_farm", "card_id": SKILL_FAIRY}


def golden_services(bundle) -> SimpleNamespace:
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: GOLDEN_NOW
    services = SimpleNamespace(evolution=copy(bundle.evolution), training=copy(bundle.training))
    for service in vars(services).values():
        service.bootstrap = bootstrap
    return services


def receipts(state: dict, namespace: str) -> dict | None:
    journal = state.get(namespace)
    if not isinstance(journal, dict):
        return None
    return {"schema_version": journal.get("schema_version"),
            "receipts": [[entry.get("revision"), entry.get("operation")] for entry in journal.get("receipts", ())],
            "processed": len(journal.get("processed_keys", ()))}


def snapshot(state: dict) -> dict:
    tables, player = state["tables"], state["player_state"]
    game = tables["game_param"][0]
    return {"keys": sorted(state), "money": game["Money"], "experience": game["Experience"],
            "last_unique_id": player.get("last_unique_id"),
            "units": [[unit[name] for name in UNIT_COLUMNS] for unit in player["units"]],
            "squads": [[row["Address"], row["UnitIndex"]] for row in player["squads"]],
            "possessions": {row["ItemID"]: row["Count"] for row in tables["possessions"]},
            "fairy_farm": tables.get("fairy_farm"), "silver_house": tables.get("silver_house"),
            "presents": tables.get("presents"), "card_status": tables.get("card_status"),
            "trust_claims": state.get("gift_state", {}).get("claims"),
            "awakening_ledger": state.get("evolution_rewards"),
            "awakening_receipts": receipts(state, "evolution_history"),
            "integration_receipts": receipts(state, "training_history")}


def growth_attempt(call: Callable[[], bytes], store: MemoryStore) -> dict:
    before = store.profile()
    try:
        result = {"response": call().decode("utf-8")}
    except ProtocolError as error:
        result = {"error": {"code": error.code, "message": str(error), "status": error.status}}
    after = store.profile()
    return {**result, "revisions": after.revision - before.revision, "save": snapshot(after.state)}


def bump_revision(store: MemoryStore) -> None:
    profile = store.profile()
    profile.state["tables"]["game_param"][0]["Money"] += 1
    store.update_state(profile.state, profile.revision)


def growth_request(store: MemoryStore, case: GrowthCase, seeded: Seeded):
    children = [Container("BASE", reference_columns([seeded.base])),
                Container("APPEND", reference_columns(seeded.append))]
    if case.number == SECOND_AWAKENING:
        children.append(Column("TYPE", "I", (case.branch,)))
    if case.number == INTEGRATION:
        children.append(Container("CMB", tuple(Column(name, "I", (value,))
                                               for name, value in zip(BONUS_COLUMNS, seeded.bonus))))
    return request(store, case.number, tuple(children))


def native_call(services: SimpleNamespace, case: GrowthCase, envelope, store: MemoryStore) -> Callable[[], bytes]:
    if case.number == INTEGRATION:
        return lambda: services.training.integrate(envelope, store)
    return lambda: services.evolution.evolve(envelope, store)


def growth_outcome(bundle, services: SimpleNamespace, case: GrowthCase) -> dict:
    seeded = case.seed(bundle)
    for change in case.prepare:
        change(bundle, seeded)
    store = MemoryStore(seeded.state)
    envelope = growth_request(store, case, seeded)
    call = native_call(services, case, envelope, store)
    outcome = {"request": envelope.xml.decode("utf-8"), **growth_attempt(call, store)}
    if case.replay:
        outcome["replay"] = growth_attempt(call, store)
    if case.stale:
        bump_revision(store)
        outcome["stale"] = growth_attempt(call, store)
    return outcome


@cache
def recorded(suite: str) -> dict:
    return json.loads((GOLDEN / f"{suite}.json").read_bytes())["cases"]


def canonical(value: object) -> str:
    # Plain equality treats True as 1, so recorded outcomes are compared through JSON text.
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def refusals(outcome: dict) -> set[str]:
    attempts = (outcome, *(outcome[name] for name in ("replay", "stale") if name in outcome))
    return {attempt["error"]["code"] for attempt in attempts if "error" in attempt}


@pytest.fixture(scope="module")
def native(bundle):
    return golden_services(bundle)


def test_golden_covers_every_case_and_native_refusal():
    for suite, cases in SUITES.items():
        assert recorded(suite).keys() == cases.keys()
        assert NATIVE_REFUSALS[suite] <= set().union(*map(refusals, recorded(suite).values()))


@pytest.mark.parametrize("suite,name", [(suite, name) for suite, cases in SUITES.items() for name in cases])
def test_native_growth_matches_the_recorded_golden(bundle, native, suite, name):
    assert canonical(growth_outcome(bundle, native, SUITES[suite][name])) == canonical(recorded(suite)[name])


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture(scope="module")
def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_growth.register(commands)
    return commands


def console_seed(bundle, case: GrowthCase) -> Seeded:
    seeded = case.seed(bundle)
    for change in case.prepare:
        change(bundle, seeded)
    # Saves reach the console only after a server start has initialized them.
    seeded.state = bundle.initialize(seeded.state)
    return seeded


def console(services, state: dict, preflight):
    store = FakeStore(state)
    generation = FakeGeneration(id="gen-growth", bundle=services, clock=GOLDEN_NOW)
    return store, CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                                 backup_directory=Path("backups"))


def commit(subject, plan, confirmations=()):
    return subject.commit(plan_id=plan.plan_id, plan_digest=plan.plan_digest, expected_revision=plan.expected_revision,
                          op_id=str(uuid.uuid4()), confirmations=confirmations)


def confirmed(subject, plan):
    return commit(subject, plan, tuple(item.phrase for item in plan.confirmations))


def preview_error(subject, batch, code, status):
    with pytest.raises(AdminError) as caught:
        subject.preview(batch)
    assert (caught.value.code, caught.value.status) == (code, status)
    return caught.value


def unit_selector(item: Reference) -> dict:
    return {"get_index": item.invite_id, "unique_id": item.unique_id, "card_id": item.card_id}


def material_selector(item: Reference) -> dict:
    if item.storage_type == BARRACKS:
        return {"source": "barracks", **unit_selector(item)}
    return {"source": WAREHOUSE_SOURCES[item.storage_type], "card_id": item.card_id}


def growth_batch(registry, case: GrowthCase, seeded: Seeded):
    params = {"target": unit_selector(seeded.base), "materials": [material_selector(item) for item in seeded.append]}
    if case.number == INTEGRATION:
        return registry.batch([{"operation": TRAIN.operation, "params": params}])
    params.update(operation=str(OPERATIONS[case.number]), branch=case.branch)
    return registry.batch([{"operation": EVOLVE.operation, "params": params}])


def native_state(services: SimpleNamespace, case: GrowthCase, seeded: Seeded) -> dict:
    store = MemoryStore(seeded.state)
    native_call(services, case, growth_request(store, case, seeded), store)()
    return store.profile().state


def without_receipts(state: dict) -> dict:
    return {key: value for key, value in state.items() if key not in RECEIPT_NAMESPACES.values()}


def test_growth_commands_register_against_the_preflight_table():
    catalog = discover(PREFLIGHT_DOMAINS)
    assert {EVOLVE.operation, TRAIN.operation} <= {command.operation for command in catalog.commands}
    assert "admin_cmd_growth" in catalog.modules and (EVOLVE.domain, TRAIN.domain) == ("units", "units")
    assert (EVOLVE.touched, TRAIN.touched) == (EVOLVE_DOMAINS, TRAIN_DOMAINS)
    assert not EVOLVE.preflight_none and not TRAIN.preflight_none
    assert EVOLVE.touched | TRAIN.touched <= PREFLIGHT_DOMAINS.keys()
    assert none_reasons(EVOLVE.touched) == unread_reasons("gifts", "evolution_rewards")
    assert none_reasons(TRAIN.touched) == unread_reasons("gifts")
    assert EVOLVE.destructive and TRAIN.destructive
    # Units, money and warehouse stock belong to many game flows, so neither command claims exclusive fields.
    assert not EVOLVE.writes and not TRAIN.writes


def test_growth_phrases_name_the_target_and_count_the_materials():
    skill = EVOLVE.parse({"target": CONSOLE_TARGET, "operation": "skill_awakening", "materials": [CONSOLE_FAIRY]})
    first = EVOLVE.parse({"target": CONSOLE_TARGET, "operation": "first_awakening",
                          "materials": [CONSOLE_HELD, *[CONSOLE_FAIRY] * 3]})
    trained = TRAIN.parse({"target": CONSOLE_TARGET, "materials": [CONSOLE_HELD, CONSOLE_FAIRY]})
    assert (EVOLVE.phrase(skill), EVOLVE.phrase(first), TRAIN.phrase(trained)) == (
        "EVOLVE UNIT 3 WITH 1 MATERIAL", "EVOLVE UNIT 3 WITH 4 MATERIALS", "TRAIN UNIT 3 WITH 2 MATERIALS")


def skill_awakening_params(target: dict = CONSOLE_TARGET, material: dict = CONSOLE_FAIRY) -> dict:
    return {"target": target, "operation": "skill_awakening", "materials": [material]}


@pytest.mark.parametrize("command, params", [
    (EVOLVE, {"target": CONSOLE_TARGET, "operation": "second_awakening", "materials": [CONSOLE_HELD]}),
    (EVOLVE, {"target": CONSOLE_TARGET, "operation": "second_awakening", "branch": 3, "materials": [CONSOLE_HELD]}),
    (EVOLVE, {"target": CONSOLE_TARGET, "operation": "first_awakening", "branch": 1, "materials": [CONSOLE_HELD]}),
    (EVOLVE, {"target": CONSOLE_TARGET, "operation": "class_change", "materials": [CONSOLE_HELD]}),
    (EVOLVE, {"target": CONSOLE_TARGET, "operation": "first_awakening", "materials": []}),
    (EVOLVE, {"target": CONSOLE_TARGET, "operation": "first_awakening",
              "materials": [CONSOLE_FAIRY] * (MAX_EVOLUTION_MATERIALS + 1)}),
    (EVOLVE, skill_awakening_params(target={**CONSOLE_TARGET, "card_id": 0})),
    (EVOLVE, skill_awakening_params(target={"get_index": 3, "unique_id": 4})),
    (EVOLVE, skill_awakening_params(material={**CONSOLE_FAIRY, "get_index": 5})),
    (EVOLVE, skill_awakening_params(material={**CONSOLE_FAIRY, "source": "squad"})),
    (EVOLVE, skill_awakening_params(material={**CONSOLE_HELD, "get_index": 3})),
    (EVOLVE, skill_awakening_params(material={**CONSOLE_HELD, "unique_id": 4})),
    (TRAIN, {"target": CONSOLE_TARGET, "materials": []}),
    (TRAIN, {"target": CONSOLE_TARGET, "materials": [CONSOLE_FAIRY] * (MAX_TRAINING_MATERIALS + 1)}),
    (TRAIN, {"target": CONSOLE_TARGET, "materials": [CONSOLE_HELD, {**CONSOLE_HELD, "unique_id": 7}]}),
    (TRAIN, {"target": CONSOLE_TARGET, "materials": [CONSOLE_HELD, {**CONSOLE_HELD, "get_index": 7}]}),
    (TRAIN, {"target": CONSOLE_TARGET, "materials": [{**CONSOLE_HELD, "card_id": True}]}),
    (TRAIN, {"target": CONSOLE_TARGET, "materials": [CONSOLE_HELD], "bonus": SUPPORT_BONUS}),
])
def test_growth_parameters_are_strict(command, params):
    with pytest.raises(AdminError) as caught:
        command.parse(params)
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


@pytest.mark.parametrize("suite,name", CONSOLE_CASES)
def test_console_growth_changes_the_save_exactly_like_the_native_kernel(bundle, native, preflight, registry, suite,
                                                                        name):
    case, expected = SUITES[suite][name], recorded(suite)[name]
    seeded = console_seed(bundle, case)
    store, subject = console(bundle, seeded.state, preflight)
    batch = growth_batch(registry, case, seeded)
    if "error" in expected:
        refusal = expected["error"]
        assert str(preview_error(subject, batch, refusal["code"], refusal["status"])) == refusal["message"]
        assert store.revision == 0 and store.backups == []
        return
    confirmed(subject, subject.preview(batch))
    assert store.revision == 1 and RECEIPT_NAMESPACES[suite] not in store.state
    assert canonical(store.state) == canonical(without_receipts(native_state(native, case, seeded)))


def test_console_awakening_reports_its_effects_and_needs_its_phrase(bundle, preflight, registry):
    case = AWAKENING_CASES["first_awakening"]
    seeded = console_seed(bundle, case)
    target = owned(seeded, seeded.target)
    store, subject = console(bundle, seeded.state, preflight)
    plan = subject.preview(growth_batch(registry, case, seeded))
    assert plan.committable and [item.phrase for item in plan.confirmations] == [
        f"EVOLVE UNIT {target['GetIndex']} WITH 4 MATERIALS"]
    materials = [deepcopy(owned(seeded, unit)) for unit in seeded.materials]
    quote = bundle.evolution.catalog.quote(deepcopy(target), materials, FIRST_AWAKENING)
    effects = plan.steps[0].effects
    assert (effects["operation"], effects["price"]) == ("first_awakening", quote.price)
    assert effects["items"] == dict(quote.items)
    assert effects["money"] == {"before": STARTING_MONEY, "after": STARTING_MONEY - quote.price}
    assert effects["changes"] == {name: {"before": target[name], "after": value}
                                  for name, value in quote.values.items() if target[name] != value}
    assert [unit["GetIndex"] for unit in effects["consumed"]] == [unit["GetIndex"] for unit in seeded.materials]
    assert effects["debited"] == {"silver_house": [], "fairy_farm": []} and effects["reward_presents"] == []
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "admin_confirmation_required" and store.revision == 0
    outcome = confirmed(subject, plan)
    assert outcome.result["backup_before_commit"]["kind"] == AUTO_BACKUP_KIND and len(store.backups) == 1
    assert effects["last_unique_id"] == store.state["player_state"]["last_unique_id"]
    assert unit_in(store.state, target["GetIndex"])["ClassChangeCount"] == quote.values["ClassChangeCount"]


def test_console_warehouse_materials_are_debited_by_count(bundle, preflight, registry):
    case = TRAINING_CASES["fairy_farm_materials"]
    seeded = console_seed(bundle, case)
    _, subject = console(bundle, seeded.state, preflight)
    effects = subject.preview(growth_batch(registry, case, seeded)).steps[0].effects
    assert effects["debited"] == {"silver_house": [], "fairy_farm": [{"card_id": FARM_FAIRY, "count": 2}]}
    assert effects["consumed"] == [] and effects["price"] == training_price(bundle, seeded)


@pytest.mark.parametrize("suite,name", [("awakenings", "first_awakening"), ("integrations", "same_card")])
def test_console_growth_names_every_protected_material(bundle, preflight, registry, suite, name):
    case = replace(SUITES[suite][name], prepare=(material_flags(0, LOCK_FLAG), material_in_squad(1)))
    seeded = console_seed(bundle, case)
    _, subject = console(bundle, seeded.state, preflight)
    code = "evolution_material_protected" if suite == "awakenings" else "training_material_protected"
    error = preview_error(subject, growth_batch(registry, case, seeded), code, 409)
    operation = EVOLVE.operation if suite == "awakenings" else TRAIN.operation
    assert error.details == {"step": 0, "operation": operation, "protected": [
        {"GetIndex": seeded.materials[0]["GetIndex"], "protections": [Protection.LOCKED]},
        {"GetIndex": seeded.materials[1]["GetIndex"], "protections": [Protection.SQUAD]}]}


def test_console_second_awakening_mails_the_first_reward_only(bundle, preflight, registry):
    case = AWAKENING_CASES["second_awakening_sends_the_first_reward"]
    seeded = console_seed(bundle, case)
    store, subject = console(bundle, seeded.state, preflight)
    plan = subject.preview(growth_batch(registry, case, seeded))
    mailed = plan.steps[0].effects["reward_presents"]
    # Console callers have no session, so the presents readback is what checks the mail listing.
    assert len(mailed) == 1 and plan.preflight["readback"]["presents"] == [42]
    confirmed(subject, plan)
    assert [row["PresentID"] for row in store.state["tables"]["presents"]][-1:] == mailed
    assert REWARD_CARD in store.state["evolution_rewards"]["claimed"]
    claimed_case = AWAKENING_CASES["second_awakening_after_the_reward_was_claimed"]
    claimed_seed = console_seed(bundle, claimed_case)
    _, again = console(bundle, claimed_seed.state, preflight)
    assert again.preview(growth_batch(registry, claimed_case, claimed_seed)).steps[0].effects["reward_presents"] == []


def test_console_second_awakening_needs_the_reward_mail(bundle, preflight, registry):
    evolution = copy(bundle.evolution)
    evolution.rewards = None
    case = AWAKENING_CASES["second_awakening_sends_the_first_reward"]
    seeded = console_seed(bundle, case)
    store, subject = console(replace(bundle, evolution=evolution), seeded.state, preflight)
    preview_error(subject, growth_batch(registry, case, seeded), "evolution_operation_unsupported", 501)
    assert store.revision == 0


def test_console_training_takes_the_bonus_the_native_menu_offers(bundle, preflight, registry):
    case = TRAINING_CASES["ownership_bonus"]
    seeded = console_seed(bundle, case)
    _, subject = console(bundle, seeded.state, preflight)
    effects = subject.preview(growth_batch(registry, case, seeded)).steps[0].effects
    card_id, get_index, percent = seeded.bonus
    assert effects["bonus"] == {"percent": percent, "card_id": card_id, "get_index": get_index}
    level = bundle.training.catalog.describe(owned(seeded, seeded.target)).level
    assert effects["price"] == training_price(bundle, seeded) and effects["level"]["before"] == level


@pytest.mark.parametrize("suite,name", [("awakenings", "unsupported_history"),
                                        ("integrations", "legacy_history_upgrades")])
def test_console_growth_leaves_native_receipt_journals_alone(bundle, preflight, registry, suite, name):
    case, namespace = SUITES[suite][name], RECEIPT_NAMESPACES[suite]
    seeded = console_seed(bundle, case)
    store, subject = console(bundle, seeded.state, preflight)
    confirmed(subject, subject.preview(growth_batch(registry, case, seeded)))
    assert store.revision == 1 and store.state[namespace] == seeded.state[namespace]


@pytest.mark.parametrize("command, missing, case", [
    (EVOLVE, "evolution", AWAKENING_CASES["first_awakening"]), (TRAIN, "training", TRAINING_CASES["same_card"])])
def test_growth_commands_need_their_services(bundle, preflight, registry, command, missing, case):
    services = replace(bundle, **{missing: None})
    assert describe_command(command, bundle)["available"] is True
    listed = describe_command(command, services)
    assert (listed["available"], listed["unavailable_reason"]) == (False, missing_service(missing))
    seeded = console_seed(bundle, case)
    _, subject = console(services, seeded.state, preflight)
    preview_error(subject, growth_batch(registry, case, seeded), "admin_command_unavailable", 503)


def tool_preview(name: str, services, state: dict, params: dict) -> dict:
    views = ViewRegistry()
    admin_views_tools.register(views)
    view = views.get(name)
    context = SimpleNamespace(state=state, revision=0, now=GOLDEN_NOW, generation=SimpleNamespace(bundle=services))
    return view.read(context, view_params(view, json.dumps(params)))


def preview_materials(seeded: Seeded) -> list[dict]:
    return [{"source": "barracks", "get_index": item.invite_id} if item.storage_type == BARRACKS
            else {"source": WAREHOUSE_SOURCES[item.storage_type], "card_id": item.card_id} for item in seeded.append]


def test_training_preview_quotes_what_the_console_command_applies(bundle, preflight, registry):
    case = TRAINING_CASES["ownership_bonus"]
    seeded = console_seed(bundle, case)
    saved = deepcopy(seeded.state)
    data = tool_preview("tools.training", bundle, seeded.state,
                        {"target_get_index": seeded.base.invite_id, "materials": preview_materials(seeded)})
    assert seeded.state == saved and data["ok"] is True and data["protection"] == {"ok": True}
    _, subject = console(bundle, seeded.state, preflight)
    effects = subject.preview(growth_batch(registry, case, seeded)).steps[0].effects
    quote, (card_id, get_index, percent) = data["quote"], seeded.bonus
    assert quote["ok"] is True and (quote["price"], quote["money"]) == (effects["price"], effects["money"]["before"])
    assert quote["bonus"] == {"percent": percent, "sources": [{"card_id": card_id, "get_index": get_index,
                                                               "percent": percent}]}
    cap = bundle.training.catalog.describe(owned(seeded, seeded.target)).cap
    assert quote["level"] == {**effects["level"], "cap": cap}
    assert {name: change for name, change in quote["changes"].items()
            if change["before"] != change["after"]} == effects["changes"]
    assert [material["GetIndex"] for material in data["materials"]] == [unit["GetIndex"] for unit in seeded.materials]


def test_growth_previews_report_protected_barracks_materials(bundle):
    seeded = console_seed(bundle, replace(AWAKENING_CASES["first_awakening"], prepare=(material_flags(0, LOCK_FLAG),)))
    materials = preview_materials(seeded)
    awakening_preview = tool_preview("tools.evolution", bundle, seeded.state, {
        "target_get_index": seeded.base.invite_id, "operation": "first_awakening", "materials": materials})
    assert awakening_preview["protection"] == {"ok": False, "error": {
        "code": "evolution_material_protected", "message": "A selected evolution material is protected",
        "details": {"protected": [{"GetIndex": seeded.materials[0]["GetIndex"], "protections": ["locked"]}]}}}
    prince = tool_preview("tools.training", bundle, seeded.state,
                          {"target_get_index": seeded.prince["GetIndex"], "materials": materials[1:2]})
    assert prince["protection"] == {"ok": False, "error": {"code": "training_target_ineligible",
                                                           "message": "The prince cannot be a training target"}}


@pytest.mark.parametrize("suite,name,view", [("awakenings", "skill_awakening_from_the_fairy_farm", "tools.evolution"),
                                             ("integrations", "fairy_farm_materials", "tools.training")])
def test_growth_protects_only_barracks_units_and_not_warehouse_templates(bundle, native, preflight, registry, suite,
                                                                        name, view):
    # Warehouse templates carry GetIndex 0 like the starting prince, so protecting them refuses while he is organized.
    case = replace(SUITES[suite][name], prepare=(prince_in_squad,))
    seeded = console_seed(bundle, case)
    params = {"target_get_index": seeded.base.invite_id, "materials": preview_materials(seeded)}
    if case.number != INTEGRATION:
        params["operation"] = str(OPERATIONS[case.number])
    assert tool_preview(view, bundle, seeded.state, params)["protection"] == {"ok": True}
    store, subject = console(bundle, seeded.state, preflight)
    confirmed(subject, subject.preview(growth_batch(registry, case, seeded)))
    assert canonical(store.state) == canonical(without_receipts(native_state(native, case, seeded)))


def test_training_catalog_quotes_without_a_native_request(bundle):
    seeded = console_seed(bundle, TRAINING_CASES["ownership_bonus"])
    catalog, state, saved = bundle.training.catalog, seeded.state, deepcopy(seeded.state)
    player = bundle.player_state.validate(state)
    target, materials = owned(seeded, seeded.target), [owned(seeded, unit) for unit in seeded.materials]
    bonus = catalog.ownership_bonus(player, state)
    assert (bonus.percent, bonus.source) == (SUPPORT_BONUS, TrainingBoost(*seeded.bonus))
    request = TrainingRequest(seeded.base, tuple(seeded.append), *seeded.bonus)
    native = catalog.quote(player, {}, request, target, materials, state=state)
    assert native == catalog.quote_materials(player, target, materials, state=state)
    assert native == catalog.quote_materials(player, target, materials, state=state, boost=bonus.source)
    with pytest.raises(ProtocolError) as caught:
        catalog.quote_materials(player, target, materials, state=state, boost=TrainingBoost(*NO_BONUS))
    assert (caught.value.code, caught.value.status) == ("training_bonus_changed", 409) and state == saved


def test_training_quotes_resolve_the_public_growth_description(bundle):
    seeded = console_seed(bundle, TRAINING_CASES["ownership_bonus"])
    catalog, state = copy(bundle.training.catalog), seeded.state
    player = bundle.player_state.validate(state)
    target, materials = owned(seeded, seeded.target), [owned(seeded, unit) for unit in seeded.materials]
    described = catalog.describe(target)
    assert (described.card["CardID"], described.class_row["ClassID"]) == (target["CardID"], target["ClassID"])
    quote = catalog.quote_materials(player, target, materials, state=state)
    assert quote.price == integration_price(described.level, len(materials))
    replaced = ProtocolError("fixture_description", "The fixture replaced the growth description", 409)

    def refuse(unit: dict):
        raise replaced

    catalog.describe = refuse
    with pytest.raises(ProtocolError) as caught:
        catalog.quote_materials(player, target, materials, state=state)
    assert caught.value is replaced


def test_ownership_scans_refuse_broken_references_like_quotes(bundle):
    seeded = console_seed(bundle, TRAINING_CASES["ownership_bonus"])
    broken = copy(bundle.training.catalog)
    broken._classes = {}
    player = bundle.player_state.validate(seeded.state)
    target, materials = owned(seeded, seeded.target), [owned(seeded, unit) for unit in seeded.materials]
    for scan in (lambda: broken.ownership_bonus(player, seeded.state),
                 lambda: broken.quote_materials(player, target, materials, state=seeded.state)):
        with pytest.raises(ProtocolError) as caught:
            scan()
        assert (caught.value.code, caught.value.status) == ("training_growth_reference", 503)
