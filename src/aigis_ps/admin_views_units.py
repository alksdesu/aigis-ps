from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .admin_cmd_units import REMOVE_OVERRIDES, UNITS, override_report, protection_names
from .admin_command import error_payload
from .admin_commit import attempt
from .admin_diff import DEFAULT_DIFF_LIMITS, pointer
from .admin_fields import AWAKENING_LEDGER, EVOLUTION_REWARDS, GIFT_LEDGER, GIFTS, OVERRIDES_KEY, UNIT_FLAG_BITS
from .admin_registry import ViewRegistry
from .admin_views import PageParams, ViewContext, page, projected, redacted_view, service
from .errors import ProtocolError
from .gift_service import HISTORY_FIELD
from .player_state import BARRACKS_BITS, BARRACKS_MASK, BARRACKS_SHIFT, EMPTY_SQUAD_SLOT, squad_group, squad_position
from .shop_growth import REGISTRATION as SHOP_GROWTH
from .silver_trade import REGISTRATION as SILVER_TRADE
from .training_service import INTEGRATION
from .unit_growth_edit import UnitGrowthEditor
from .unit_removal import unit_protections


OVERRIDE_GUARDS = MappingProxyType({"training": INTEGRATION, "silver_trade": SILVER_TRADE, "shop_growth": SHOP_GROWTH})
# Removal plans list a value up to this size in full, so the preview shows what the plan diff will show.
OVERRIDE_PREVIEW_BYTES = DEFAULT_DIFF_LIMITS.max_value_bytes


def unit_flags(value: int) -> dict:
    # Bits 4-7 hold the barracks index, which is a number rather than four independent flags.
    return {"barracks": value >> BARRACKS_SHIFT & BARRACKS_MASK,
            "bits": UNIT_FLAG_BITS.decode(value & ~BARRACKS_BITS)}


def checked(read) -> tuple[Any, dict]:
    try:
        return read(), {"ok": True}
    except ProtocolError as error:
        return None, {"ok": False, "error": error_payload(error)}


def growth_editor(bundle: Any) -> UnitGrowthEditor:
    return UnitGrowthEditor(service(bundle, "player_state"), service(bundle, "training").catalog, service(bundle, GIFTS))


def roster_entry(unit: dict, protections: Mapping[int, tuple] | None, editor: UnitGrowthEditor | None) -> dict:
    return {"unit": dict(unit), "flags": unit_flags(unit["GeneralPurposeFlags"]),
            "protections": None if protections is None else protection_names(protections[unit["GetIndex"]]),
            "growth": None if editor is None else attempt(lambda: editor.limits(unit).to_dict())}


def roster_view(context: ViewContext, params: PageParams) -> dict:
    state, bundle = context.state, context.generation.bundle
    players = service(bundle, "player_state")
    player = players.validate(state, references=False)
    protections, protection_status = checked(lambda: unit_protections(players, state))
    editor, growth_status = checked(lambda: growth_editor(bundle))
    return {"battle": dict(player["battle"]), "last_unique_id": player.get("last_unique_id"),
            "protections": protection_status, "growth": growth_status,
            "units": page(player["units"], params, lambda unit: roster_entry(unit, protections, editor))}


def barracks_usage(capacity: Any, state: dict) -> dict:
    usage = capacity.usage(state)
    return {"capacities": list(usage.capacities), "counts": list(usage.counts), "primary_available": capacity.available(state)}


def barracks_view(context: ViewContext, params: None) -> dict:
    state, bundle = context.state, context.generation.bundle
    players = service(bundle, "player_state")
    return {"barracks": attempt(lambda: barracks_usage(service(bundle, "unit_capacity"), state)),
            "grantable": attempt(lambda: {"units": players.available_unit_slots(state)}),
            "battle_reservations": attempt(lambda: {"grants": players.quest_reservations(state)})}


def squads_view(context: ViewContext, params: None) -> dict:
    player = service(context.generation.bundle, "player_state").validate(context.state, references=False)
    battle, cards = player["battle"], {unit["GetIndex"]: unit["CardID"] for unit in player["units"]}
    groups: dict[int, dict] = {}
    for row in player["squads"]:
        group = squad_group(row)
        entry = groups.setdefault(group, {"group": group, "name": row["Name"], "flags": row["Flags"],
                                          "selected": group == battle["TN"], "raid": group == battle["RTN"], "members": []})
        member = row["UnitIndex"]
        # The raid group spreads its members over three sub-teams that reuse the same slot numbers.
        team, slot = squad_position(row)
        entry["members"].append({"team": team, "slot": slot,
                                 "GetIndex": None if member == EMPTY_SQUAD_SLOT else member, "CardID": cards.get(member)})
    for entry in groups.values():
        entry["members"].sort(key=lambda member: (member["team"], member["slot"]))
    return {"battle": dict(battle), "legacy_squad_imported": player["legacy_squad_imported"],
            "squads": [groups[group] for group in sorted(groups)]}


def fame_stock(repository: Any, state: dict) -> dict:
    return {"rows": repository.read(state), "deposit_limit": repository.status()["deposit_limit"]}


def warehouse_stock(warehouse: Any, state: dict) -> dict:
    return {"rows": warehouse.read(state), "per_card_capacity": warehouse.status()["per_card_capacity"]}


def storage_view(context: ViewContext, params: None) -> dict:
    state, bundle = context.state, context.generation.bundle
    return {"fame": attempt(lambda: fame_stock(service(bundle, "fame_repository"), state)),
            "fairy": attempt(lambda: warehouse_stock(service(bundle, "fairy_farm"), state)),
            "silver": attempt(lambda: warehouse_stock(service(bundle, "silver_house"), state))}


def stored_or_projected(state: dict, key: str, ledger: dict) -> dict:
    # Without a stored ledger the services build the baseline the next write would save; showing it saves nothing.
    return {"projected": False, "persisted": True, "ledger": ledger} if key in state else projected(ledger=ledger)


def awakening_ledger(rewards: Any, state: dict) -> dict:
    return stored_or_projected(state, AWAKENING_LEDGER, rewards.read(state))


def trust_ledger(gifts: Any, state: dict, revision: int) -> dict:
    journal = gifts.read(state, revision)
    history = journal[HISTORY_FIELD]
    return stored_or_projected(state, GIFT_LEDGER, {"claims": journal["claims"], "receipts": len(history["receipts"]),
                                                    "processed_requests": len(history["processed_keys"])})


def ledgers_view(context: ViewContext, params: None) -> dict:
    state, bundle = context.state, context.generation.bundle
    return {EVOLUTION_REWARDS: attempt(lambda: awakening_ledger(service(bundle, EVOLUTION_REWARDS), state)),
            GIFTS: attempt(lambda: trust_ledger(service(bundle, GIFTS), state, context.revision))}


def overrides_view(context: ViewContext, params: None) -> dict:
    state = context.state
    report = override_report(state)
    truncated = report.present and report.bytes > OVERRIDE_PREVIEW_BYTES
    return {**report.to_dict(), "pointer": pointer((OVERRIDES_KEY,)), "truncated": truncated,
            "value": None if truncated else state.get(OVERRIDES_KEY), "guards": dict(OVERRIDE_GUARDS),
            "removal": REMOVE_OVERRIDES.operation}


VIEWS = (
    redacted_view("units.roster", UNITS, roster_view, PageParams),
    redacted_view("units.barracks", UNITS, barracks_view),
    redacted_view("units.squads", UNITS, squads_view),
    redacted_view("units.storage", UNITS, storage_view),
    redacted_view("units.ledgers", UNITS, ledgers_view),
    redacted_view("units.overrides", UNITS, overrides_view),
)


def register(registry: ViewRegistry) -> None:
    for view in VIEWS:
        registry.add(view)
