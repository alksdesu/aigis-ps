from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from .admin_app import server_time
from .admin_commit import attempt
from .admin_fields import anchors, decode_row, epoch_milliseconds, logical_day, native_date
from .admin_registry import ViewRegistry
from .admin_views import PageParams, ViewContext, page, projected, redacted_view, service
from .business_calendar import read_marker
from .crystal_spending import read_spending
from .crystal_wallet import wallet_totals
from .energy_clock import MARKER_KEY, validate_clock
from .monthly_capacity import read_capacity
from .quest_economy import settle_energy


ACCOUNT = "account"
GAME_PARAM, CRYSTALS = "game_param", "crystals"


def rank_status(rank_rows: list[dict], row: dict) -> dict | None:
    rank = row["Rank"]
    if not 1 <= rank <= len(rank_rows):
        return None
    current = rank_rows[rank - 1]
    floor = sum(entry["NextExp"] for entry in rank_rows[:rank - 1])
    return {"charisma_max": current["CharismaMax"],
            "charisma_capacity": current["CharismaMax"] + row["ExtendedMaxCharisma"],
            "experience_floor": floor, "experience_next": floor + current["NextExp"]}


def _ledger(ledger: dict) -> dict:
    return {"ledger": ledger, "last_time_utc": epoch_milliseconds(ledger["last_time"])}


def game_param(bootstrap: Any, state: dict) -> dict:
    row = bootstrap.table_rows(state, GAME_PARAM)[0]
    return {"fields": decode_row(GAME_PARAM, row), "anchors": anchors(row),
            "rank": rank_status(bootstrap.rank_rows, row)}


def crystals(bootstrap: Any, state: dict, params: PageParams) -> dict:
    # Buckets accumulate per acquisition day, so long-lived saves page them like the state browser.
    return {"totals": wallet_totals(state),
            "buckets": page(bootstrap.table_rows(state, CRYSTALS), params, lambda row: decode_row(CRYSTALS, row))}


def energy_clock(state: dict) -> dict:
    validate_clock(state)
    return {"marker": deepcopy(state[MARKER_KEY])}


def summary_view(context: ViewContext, params: PageParams) -> dict:
    state, bundle = context.state, context.generation.bundle
    return {"game_param": attempt(lambda: game_param(service(bundle, "bootstrap"), state)),
            "crystals": attempt(lambda: crystals(service(bundle, "bootstrap"), state, params)),
            "spending": attempt(lambda: _ledger(read_spending(state))),
            "capacity": attempt(lambda: _ledger(read_capacity(state))),
            "energy_clock": attempt(lambda: energy_clock(state)),
            "business_calendar": attempt(lambda: {"marker": read_marker(state)})}


def recovery_seconds(bundle: Any) -> int:
    quests = getattr(bundle, "quests", None)
    if quests is not None:
        return quests.rules.charisma_recovery_seconds
    return service(bundle, "recovery").recovery_seconds


def energy_projection(bundle: Any, state: dict, now: datetime) -> dict:
    bootstrap, rate = service(bundle, "bootstrap"), recovery_seconds(bundle)
    before = bootstrap.table_rows(state, GAME_PARAM)[0]
    settled = settle_energy(deepcopy(state), bootstrap.rank_rows, now, recovery_seconds=rate)
    after = bootstrap.table_rows(settled, GAME_PARAM)[0]
    return projected(as_of=server_time(now), recovery_seconds=rate,
                     charisma={"before": before["NowCharisma"], "after": after["NowCharisma"]},
                     anchors={"before": anchors(before), "after": anchors(after)},
                     rank=rank_status(bootstrap.rank_rows, after),
                     capacity=attempt(lambda: _ledger(read_capacity(settled))))


def projection_view(context: ViewContext, params: None) -> dict:
    now = context.now
    day = context.generation.acquisition_day(now)
    return {"energy": attempt(lambda: energy_projection(context.generation.bundle, context.state, now)),
            "calendar": {"logical_day": logical_day(now).isoformat(), "acquisition_day": day,
                         "acquisition_date": native_date(day).isoformat()}}


VIEWS = (
    redacted_view("account.summary", ACCOUNT, summary_view, PageParams),
    redacted_view("account.projection", ACCOUNT, projection_view, live=True),
)


def register(registry: ViewRegistry) -> None:
    for view in VIEWS:
        registry.add(view)
