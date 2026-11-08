from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .admin_cmd_settings import CONFIG, SETTINGS, purchase_details, recorded_skins, skin_holdings
from .admin_cmd_stories import CARD_STATUS, STORY_FIELD
from .admin_commit import attempt
from .admin_fields import STORY_BITS, decode_row, epoch_milliseconds
from .admin_registry import ViewRegistry
from .admin_views import PageParams, ViewContext, page, redacted_view, service
from .background_state import read_backgrounds
from .card_status import CARD_STATUS_FIELDS
from .peruse_state import read_perused
from .universal_flags import flag_hash, read_flags
from .view_events import EVENT_VIEW_FIELD, parse_view_request


TUTORIAL_FIELD, USER_NAME = "MenuTutorialFlags", "UserName"
# B0..B4 hold the Unix seconds of the five collection stages; zero means not reached.
REGISTRATION_FIELDS = ("B0", "B1", "B2", "B3", "B4")
UNCONFIRMED_FIELDS = ("A1", "A3", "A5")
NO_COMMAND = "No console command writes this field"
UNCONFIRMED = "The meaning is unconfirmed, so the stored value is only shown"
READ_ONLY_FIELDS: Mapping[str, str] = MappingProxyType({
    f"{CONFIG}.{TUTORIAL_FIELD}": "Not every tutorial bit is verified, and resetting tutorial hints must not reopen rewards",
    f"{CONFIG}.{USER_NAME}": "Player names have no shared native validator",
    **{f"{CARD_STATUS}.{name}": UNCONFIRMED for name in UNCONFIRMED_FIELDS},
})


def ownership(reference: str, owners: Mapping[str, str]) -> dict:
    policy, owner = READ_ONLY_FIELDS.get(reference), owners.get(reference)
    return {"owner": owner, "read_only": policy is not None or owner is None,
            "reason": policy or (NO_COMMAND if owner is None else None)}


def field_owners(context: ViewContext) -> Mapping[str, str]:
    return context.services.catalog.commands.field_owners()


def config_fields(bootstrap: Any, state: dict, owners: Mapping[str, str]) -> dict:
    row = bootstrap.table_rows(state, CONFIG)[0]
    return {"fields": {name: {**entry, **ownership(f"{CONFIG}.{name}", owners)}
                       for name, entry in decode_row(CONFIG, row).items()}}


def config_view(context: ViewContext, params: None) -> dict:
    owners = field_owners(context)
    return attempt(lambda: config_fields(service(context.generation.bundle, "bootstrap"), context.state, owners))


def event_view_pointer(bootstrap: Any, state: dict, owners: Mapping[str, str]) -> dict:
    value = bootstrap.table_rows(state, CONFIG)[0][EVENT_VIEW_FIELD]
    return {"value": value, "request": attempt(lambda: {"fields": parse_view_request(value)}) if value else None,
            **ownership(f"{CONFIG}.{EVENT_VIEW_FIELD}", owners)}


def event_view_view(context: ViewContext, params: None) -> dict:
    owners = field_owners(context)
    return attempt(lambda: event_view_pointer(service(context.generation.bundle, "bootstrap"), context.state, owners))


def purchase_listing(bundle: Any, state: dict) -> dict:
    products = service(bundle, "skin_shop").catalog.products
    stored = {flag_hash(row["ID"]): row for row in read_flags(state)}
    holdings = skin_holdings(service(bundle, "possessions"), state, products)
    recorded = recorded_skins(bundle, state)
    listing = []
    for product in products:
        row = stored.get(product.purchase_flag)
        listing.append({"flag": product.flag_name if row is None else row["ID"], "stored": row is not None,
                        "value": 0 if row is None else row["Value"], **purchase_details(product, holdings, recorded)})
    return {"products": listing}


def flags_view(context: ViewContext, params: PageParams) -> dict:
    state, bundle = context.state, context.generation.bundle
    return {"flags": attempt(lambda: page(read_flags(state), params, lambda row: {**row, "hash": flag_hash(row["ID"])})),
            "purchases": attempt(lambda: purchase_listing(bundle, state))}


def perused_view(context: ViewContext, params: PageParams) -> dict:
    return {"events": attempt(lambda: page(read_perused(context.state), params))}


def backgrounds_view(context: ViewContext, params: PageParams) -> dict:
    state, bundle = context.state, context.generation.bundle
    return {"rows": attempt(lambda: page(read_backgrounds(state), params)),
            "diagnostics": attempt(lambda: service(bundle, "backgrounds").diagnose(state))}


def registered_at(seconds: int) -> str | None:
    return None if seconds == 0 else epoch_milliseconds(seconds * 1000)


def card_row(row: dict) -> dict:
    return {"CardID": row["CardID"],
            STORY_FIELD: {"value": row[STORY_FIELD], "bits": STORY_BITS.decode(row[STORY_FIELD])},
            **{name: {"value": row[name], "utc": registered_at(row[name])} for name in REGISTRATION_FIELDS},
            **{name: {"value": row[name]} for name in UNCONFIRMED_FIELDS}}


def card_status_view(context: ViewContext, params: PageParams) -> dict:
    owners, state, bundle = field_owners(context), context.state, context.generation.bundle
    return {"fields": {name: ownership(f"{CARD_STATUS}.{name}", owners) for name in CARD_STATUS_FIELDS if name != "CardID"},
            "story_bits": STORY_BITS.describe(),
            "rows": attempt(lambda: page(service(bundle, CARD_STATUS).read(state), params, card_row))}


VIEWS = (
    redacted_view("settings.config", SETTINGS, config_view),
    redacted_view("settings.event_view", SETTINGS, event_view_view),
    redacted_view("settings.flags", SETTINGS, flags_view, PageParams),
    redacted_view("settings.perused", SETTINGS, perused_view, PageParams),
    redacted_view("settings.backgrounds", SETTINGS, backgrounds_view, PageParams),
    redacted_view("settings.card_status", SETTINGS, card_status_view, PageParams),
)


def register(registry: ViewRegistry) -> None:
    for view in VIEWS:
        registry.add(view)
