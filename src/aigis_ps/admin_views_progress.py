from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .admin_app import server_time
from .admin_cmd_settings import FINISHED, PROGRESS, unread
from .admin_commit import attempt
from .admin_fields import LOGIN_REWARDS, epoch_milliseconds
from .admin_registry import ViewRegistry
from .admin_views import PageParams, ViewContext, page, projected, redacted_view, service
from .login_cooking_state import read_cooking
from .login_reward_state import read_login_rewards
from .login_stamp_pending import read_pending_stamps
from .mission_service import HISTORY_NAMESPACE as MISSION_HISTORY
from .rank_reward_state import DELIVERED, PENDING, bonus_fingerprint, read_rank_rewards


def clocked(ledger: dict) -> dict:
    return {"ledger": ledger, "last_time_utc": epoch_milliseconds(ledger["last_time"])}


def mission_row(row: dict, definition: Any) -> dict:
    return {**row, "period_type": definition.period_type, "tab": definition.disp_tab_type,
            "claimable": row["Status"] == FINISHED, "unread": unread(row)}


def mission_progress(missions: Any, state: dict, now: datetime, params: PageParams) -> dict:
    rows = missions.progress.snapshot(missions.initialize(state), now)
    definitions = missions.catalog.by_id
    return projected(as_of=server_time(now), **page(rows, params, lambda row: mission_row(row, definitions[row["ID"]])))


def missions_view(context: ViewContext, params: PageParams) -> dict:
    state, bundle = context.state, context.generation.bundle
    return {"missions": attempt(lambda: mission_progress(service(bundle, "missions"), state, context.now, params)),
            "history": state.get(MISSION_HISTORY)}


def rank_entry(bonus_id: int, entry: dict, bonuses: Mapping[int, Any] | None) -> dict:
    bonus = None if bonuses is None else bonuses.get(bonus_id)
    current = None if bonuses is None else bonus is not None and bonus_fingerprint(bonus) == entry["definition"]
    return {"bonus_id": bonus_id, **entry, "definition_current": current}


def rank_rewards(bundle: Any, state: dict) -> dict:
    ledger = read_rank_rewards(state)
    rewards = getattr(bundle, "rank_rewards", None)
    bonuses = None if rewards is None else rewards.catalog.by_id
    entries = sorted((rank_entry(int(key), entry, bonuses) for key, entry in ledger["entries"].items()),
                     key=lambda entry: (entry["rank"], entry["bonus_id"]))
    return {"baseline_rank": ledger["baseline_rank"], "observed_rank": ledger["observed_rank"],
            "definitions_checked": bonuses is not None,
            "pending": [entry for entry in entries if entry["status"] == PENDING],
            "delivered": [entry for entry in entries if entry["status"] == DELIVERED]}


def rank_rewards_view(context: ViewContext, params: None) -> dict:
    return attempt(lambda: rank_rewards(context.generation.bundle, context.state))


def login_service(context: ViewContext) -> Any:
    return service(context.generation.bundle, LOGIN_REWARDS)


def login_rewards_view(context: ViewContext, params: None) -> dict:
    state = context.state
    return {"ledger": attempt(lambda: clocked(read_login_rewards(state))),
            "pending_stamps": attempt(lambda: {"entries": read_pending_stamps(state)}),
            "rules": attempt(lambda: login_service(context).beginner_rules(state)),
            "stamp_capacity": attempt(lambda: login_service(context).pending_stamps(state))}


def login_cooking_view(context: ViewContext, params: None) -> dict:
    state = context.state
    return {**attempt(lambda: clocked(read_cooking(state))),
            "rules": attempt(lambda: login_service(context).cooking_rules(state))}


VIEWS = (
    redacted_view("progress.missions", PROGRESS, missions_view, PageParams, live=True),
    redacted_view("progress.rank_rewards", PROGRESS, rank_rewards_view),
    redacted_view("progress.login_rewards", PROGRESS, login_rewards_view),
    redacted_view("progress.login_cooking", PROGRESS, login_cooking_view),
)


def register(registry: ViewRegistry) -> None:
    for view in VIEWS:
        registry.add(view)
