from __future__ import annotations

from collections.abc import Iterable
from http import HTTPStatus
from typing import Any

from pydantic import Field, create_model, model_validator

from .admin_command import (BOOLEAN_HINT, AdminCommand, AdminError, CommandContext, CommandParams, CommandResult, counted,
                            require_distinct, required_service, requires)
from .admin_registry import CommandRegistry
from .background_state import MAX_BACKGROUNDS, merge_backgrounds, read_backgrounds
from .bootstrap import BOOLEAN_CONFIGURATION_FIELDS, configuration_updates
from .business_calendar import NATIVE_DATETIME
from .config_state import CONFIGURATION_ALIASES, CONFIGURATION_FIELDS, SOUND_OPTION_FIELDS, patch_configuration
from .mission_service import MAX_MISSIONS
from .mission_state import SKIN_HISTORY_KEY as SKIN_HISTORY
from .peruse_state import MAX_PERUSED_EVENTS, read_perused, set_perused_flags
from .quest_protocol import validate_damage_display
from .skin_shop_catalog import SkinProduct
from .universal_flags import MAX_FLAGS, flag_hash, read_flags, set_flags


SETTINGS, PROGRESS = "settings", "progress"
CONFIG, FLAGS_DOMAIN, PERUSED_DOMAIN, BACKGROUNDS_DOMAIN, MISSIONS_DOMAIN = (
    "config", "universal_flags", "perused", "backgrounds", "missions")
DAMAGE_DISPLAY_FIELD = "DamageDispEnable"
DAMAGE_DISPLAY_WRITES = frozenset({f"{CONFIG}.{DAMAGE_DISPLAY_FIELD}"})
# Native SaveTime is zero-padded JST wall-clock text, so text order is time order.
SAVE_TIME_FORMAT = NATIVE_DATETIME
PURCHASE_FLAG_ACK = "purchase_flag_ack"
PURCHASE_FLAG_ACK_REQUIRED = "admin_purchase_flag_ack_required"
BACKGROUND_ROWS_UNSUPPORTED = "admin_background_rows_unsupported"
BACKGROUND_SAVE_TIME_FUTURE = "admin_background_save_time_future"
SKIN_SHOP_REASON = "Purchase flags can only be told apart with the verified skin shop catalog"
NOTHING_MARKED = "Only finished unread missions, claimed or not, are marked read"
# Snapshot status 1 is a finished mission whose reward is still unclaimed; status 2 has been claimed.
FINISHED, CLAIMED = 1, 2


def _sound_field(wire: str) -> tuple[type, Any]:
    hint = BOOLEAN_HINT if wire in BOOLEAN_CONFIGURATION_FIELDS else None
    return int, Field(title=CONFIGURATION_ALIASES[wire], description=hint)


# The parameters are the complete 21/22 sound group, the only set the native kernel accepts besides tutorial flags.
SoundParams = create_model("SoundParams", __base__=CommandParams,
                           **{wire: _sound_field(wire) for wire in sorted(SOUND_OPTION_FIELDS)})
SOUND_WRITES = frozenset(f"{CONFIG}.{CONFIGURATION_ALIASES[wire]}" for wire in SOUND_OPTION_FIELDS)


def set_sound(state: dict, params: CommandParams, context: CommandContext) -> CommandResult:
    updates = configuration_updates(params.model_dump())
    before = required_service(context, "bootstrap").table_rows(state, CONFIG)[0]
    updated = patch_configuration(state, updates)
    changed = [{"field": name, "wire": CONFIGURATION_FIELDS[name][0], "before": before[name], "after": value}
               for name, value in updates.items() if before[name] != value]
    return CommandResult(updated, {"fields": changed})


class DamageDisplayParams(CommandParams):
    enabled: int = Field(title=DAMAGE_DISPLAY_FIELD, description=BOOLEAN_HINT)


def set_damage_display(state: dict, params: DamageDisplayParams, context: CommandContext) -> CommandResult:
    value = validate_damage_display(params.enabled)
    before = required_service(context, "bootstrap").table_rows(state, CONFIG)[0][DAMAGE_DISPLAY_FIELD]
    updated = patch_configuration(state, {DAMAGE_DISPLAY_FIELD: value})
    changed = [{"field": DAMAGE_DISPLAY_FIELD, "wire": CONFIGURATION_FIELDS[DAMAGE_DISPLAY_FIELD][0],
                "before": before, "after": value}] if before != value else []
    return CommandResult(updated, {"fields": changed})


class FlagValue(CommandParams):
    ID: str
    Value: int


class FlagParams(CommandParams):
    flags: list[FlagValue] = Field(min_length=1, max_length=MAX_FLAGS)
    purchase_flag_ack: bool = False

    @model_validator(mode="after")
    def _distinct(self) -> FlagParams:
        require_distinct((flag.ID for flag in self.flags), "Flag names")
        return self


def skin_holdings(possessions: Any, state: dict, products: Iterable[SkinProduct]) -> dict[str, int]:
    items = sorted({product.item_id for product in products if product.supported})
    if not items:
        return {}
    columns = {column.name: column.values for column in possessions.response(state, items).children}
    return dict(zip(columns["ItemID"], columns["Count"]))


def recorded_skins(services: Any, state: dict) -> frozenset[str] | None:
    missions = getattr(services, "missions", None)
    if missions is None:
        return None
    # Mission initialization validates the acquisition history before it is read.
    history = missions.initialize(state).get(SKIN_HISTORY)
    return frozenset() if history is None else frozenset(history["seen"])


def purchase_details(product: SkinProduct, holdings: dict[str, int], recorded: frozenset[str] | None) -> dict:
    return {"shop_id": product.shop_id, "item_id": product.item_id, "supported": product.supported,
            "holding": holdings.get(product.item_id),
            "acquisition_recorded": None if recorded is None else product.item_id in recorded}


def touched_purchase_flags(context: CommandContext, state: dict, updates: dict[str, int],
                           stored: dict[str, int]) -> list[dict]:
    catalog = required_service(context, "skin_shop", SKIN_SHOP_REASON).catalog
    products = {product.purchase_flag: product for product in catalog.products}
    touched = [(name, value, products[flag_hash(name)]) for name, value in updates.items() if flag_hash(name) in products]
    if not touched:
        return []
    holdings = skin_holdings(required_service(context, "possessions"), state, (product for _, _, product in touched))
    recorded = recorded_skins(context.services, state)
    return [{"ID": name, "before": stored.get(name), "after": value, **purchase_details(product, holdings, recorded)}
            for name, value, product in touched]


def set_flag_values(state: dict, params: FlagParams, context: CommandContext) -> CommandResult:
    updates = {flag.ID: flag.Value for flag in params.flags}
    stored = {row["ID"]: row["Value"] for row in read_flags(state)}
    updated = set_flags(state, updates)
    purchases = touched_purchase_flags(context, state, updates, stored)
    if purchases and not params.purchase_flag_ack:
        message = "Skin shop purchase flags change only with purchase_flag_ack set to true"
        raise AdminError(PURCHASE_FLAG_ACK_REQUIRED, message, HTTPStatus.UNPROCESSABLE_ENTITY,
                         fields=({"loc": [PURCHASE_FLAG_ACK], "type": PURCHASE_FLAG_ACK_REQUIRED, "msg": message},),
                         details={"purchase_flags": purchases})
    # The shop refuses any product whose flag is set, so a set flag without the item leaves the skin unobtainable.
    warnings = tuple(f"{entry['ID']} marks shop product {entry['shop_id']} as purchased while {entry['item_id']} is not held"
                     for entry in purchases if entry["after"] != 0 and not entry["holding"])
    effects = {"flags": [{"ID": name, "before": stored.get(name), "after": value} for name, value in updates.items()],
               "purchase_flags": purchases}
    return CommandResult(updated, effects, warnings)


def acknowledges_purchase(params: FlagParams) -> bool:
    # Touching a 156 purchase flag without the ack is already a 422, so the ack marks exactly the destructive edits.
    return params.purchase_flag_ack


def flag_phrase(params: FlagParams) -> str:
    noun = "PURCHASE FLAG" if params.purchase_flag_ack else "FLAG"
    return f"SET {counted(len(params.flags), noun)}"


class PerusedFlag(CommandParams):
    ID: str
    FLG: int


class PerusedParams(CommandParams):
    events: list[PerusedFlag] = Field(min_length=1, max_length=MAX_PERUSED_EVENTS)

    @model_validator(mode="after")
    def _distinct(self) -> PerusedParams:
        require_distinct((event.ID for event in self.events), "Event IDs")
        return self


def set_perused(state: dict, params: PerusedParams, context: CommandContext) -> CommandResult:
    updates = {event.ID: event.FLG for event in params.events}
    stored = {row["ID"]: row["FLG"] for row in read_perused(state)}
    updated = set_perused_flags(state, updates)
    return CommandResult(updated, {"events": [{"ID": event_id, "before": stored.get(event_id), "after": flag}
                                              for event_id, flag in updates.items()]})


class BackgroundRow(CommandParams):
    CardID: int
    ConfigID: str
    SaveTime: str


class BackgroundParams(CommandParams):
    rows: list[BackgroundRow] = Field(min_length=1, max_length=MAX_BACKGROUNDS)


def merge_background_rows(state: dict, params: BackgroundParams, context: CommandContext) -> CommandResult:
    rows = [row.model_dump() for row in params.rows]
    updated = merge_backgrounds(state, rows)
    findings = required_service(context, "backgrounds").catalog.audit(rows)
    if findings:
        raise AdminError(BACKGROUND_ROWS_UNSUPPORTED, "Background rows must name selectable verified backgrounds",
                         details={"findings": list(findings)})
    # The native save refuses any selection older than a stored one, so a future SaveTime would lock the menu.
    now = context.now_jst.strftime(SAVE_TIME_FORMAT)
    later = [row["CardID"] for row in rows if row["SaveTime"] > now]
    if later:
        raise AdminError(BACKGROUND_SAVE_TIME_FUTURE, "Background save times cannot be later than the server's JST time",
                         details={"now": now, "card_ids": later})
    stored = {row["CardID"]: row for row in read_backgrounds(state)}
    return CommandResult(updated, {"rows": [{"CardID": row["CardID"], "before": stored.get(row["CardID"]), "after": row}
                                            for row in rows]})


class MissionReadParams(CommandParams):
    ids: list[int] = Field(min_length=1, max_length=MAX_MISSIONS)


def unread(row: dict) -> bool:
    return row["Status"] in (FINISHED, CLAIMED) and row["Read"] == 0


def mark_missions_read(state: dict, params: MissionReadParams, context: CommandContext) -> CommandResult:
    missions = required_service(context, "missions")
    prepared = missions.initialize(state)
    # The kernel refuses malformed or inactive selections before anything is marked.
    missions.progress.mark_read(prepared, params.ids, context.now)
    shown = {row["ID"]: row for row in missions.progress.snapshot(prepared, context.now)}
    # Like 60, only what the 54 snapshot shows as finished and unread is marked, so locked sequential steps stay unread.
    marked, skipped = [], []
    for identifier in params.ids:
        row = shown[identifier]
        if unread(row):
            marked.append(identifier)
        else:
            skipped.append({"ID": identifier, "Status": row["Status"], "Read": row["Read"]})
    # Like 60, a read that marks nothing saves nothing, not even progress the read would otherwise settle.
    updated = missions.progress.mark_read(prepared, marked, context.now) if marked else state
    return CommandResult(updated, {"marked": marked, "skipped": skipped}, (NOTHING_MARKED,) if skipped else ())


SOUND = AdminCommand("config.sound.set", SETTINGS, SoundParams, set_sound, frozenset({CONFIG}), writes=SOUND_WRITES)
DAMAGE_DISPLAY = AdminCommand("config.damage_display.set", SETTINGS, DamageDisplayParams, set_damage_display,
                              frozenset({CONFIG}), writes=DAMAGE_DISPLAY_WRITES)
FLAGS = AdminCommand("flags.set", SETTINGS, FlagParams, set_flag_values, frozenset({FLAGS_DOMAIN}),
                     destructive=acknowledges_purchase, confirm_phrase=flag_phrase,
                     unavailable=requires("skin_shop", SKIN_SHOP_REASON))
PERUSED = AdminCommand("perused.set", SETTINGS, PerusedParams, set_perused, frozenset({PERUSED_DOMAIN}))
BACKGROUNDS = AdminCommand("backgrounds.merge", SETTINGS, BackgroundParams, merge_background_rows,
                           frozenset({BACKGROUNDS_DOMAIN}), unavailable=requires("backgrounds"))
MISSIONS_READ = AdminCommand("missions.mark_read", PROGRESS, MissionReadParams, mark_missions_read,
                             frozenset({MISSIONS_DOMAIN}), unavailable=requires("missions"))
COMMANDS = (SOUND, DAMAGE_DISPLAY, FLAGS, PERUSED, BACKGROUNDS, MISSIONS_READ)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
