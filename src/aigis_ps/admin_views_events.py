from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any

from .admin_app import server_time
from .admin_command import error_payload
from .admin_fields import (BINDING_LEDGER, BOARD_LEDGER, COMEBACK_LOGINS, DAILY_GIFTS, EVENT_LOGINS, EVENT_PASSES,
                           LOGIN_REWARDS, MISSIONS, PACKAGE_LEDGER, PACKAGES, PASS_AUTOMATION, PASS_LEDGER,
                           PURCHASED_LOGINS, SUGOROKU, TOWER_LEDGER, TOWER_TRADES, TRADE_LEDGER, TRADES,
                           epoch_milliseconds)
from .admin_preflight import PREFLIGHT_DOMAINS, STARTUP_READ
from .admin_registry import AdminView, ViewRegistry
from .admin_views import PageParams, ViewContext, page, projected, redacted_view, service
from .business_calendar import business_month, month_key
from .comeback_login_service import (BAND_UNAVAILABLE as COMEBACK_BAND_UNAVAILABLE,
                                     RESOLVE_OPERATION as COMEBACK_LOGIN_REPAIR, comeback_reward_definition)
from .comeback_login_state import LEDGER_KEY as COMEBACK_LEDGER, comeback_clock, read_comeback_logins
from .crystal_spending import read_spending
from .daily_gift_service import (RESOLVE_OPERATION as DAILY_GIFT_REPAIR,
                                 SOURCE_UNAVAILABLE as DAILY_GIFT_SOURCE_UNAVAILABLE, VOIDED as DAILY_GIFT_VOIDED,
                                 daily_gift_definition)
from .daily_gift_state import LEDGER_KEY as DAILY_GIFT_LEDGER, PENDING as DAILY_GIFT_PENDING, read_daily_gifts
from .errors import ProtocolError
from .event_login_service import RESOLVE_OPERATION as EVENT_LOGIN_REPAIR, event_login_definition
from .event_login_state import LEDGER_KEY as EVENT_LOGIN_LEDGER, read_event_logins, voided_delivery
from .event_pass_service import REBIND_OPERATION as EVENT_PASS_REPAIR, EventPassService, pass_identity, reward_semantics
from .event_pass_state import ARCHIVED_KEY, TRACKS, read_event_passes
from .login_cooking_state import LEDGER_KEY as COOKING_LEDGER, read_cooking
from .login_reward_service import STAMP_RESOLVE_OPERATION as STAMP_REPAIR
from .login_reward_state import LEDGER_KEY as LOGIN_REWARD_LEDGER, read_login_rewards
from .login_stamp_pending import PENDING_KEY, read_pending_stamps
from .package_state import LEGACY_OWNED as LEGACY_PACKAGE
from .purchased_login_service import (CAMPAIGN_UNAVAILABLE as PURCHASED_CAMPAIGN_UNAVAILABLE,
                                      RESOLVE_OPERATION as PURCHASED_LOGIN_REPAIR,
                                      REWARD_MISSING as PURCHASED_REWARD_MISSING)
from .purchased_login_state import LEDGER_KEY as PURCHASED_LEDGER, purchased_login_clock, read_purchased_logins
from .sugoroku_service import CLAIMED, SEEN_STATUSES
from .sugoroku_state import BASELINE as BASELINE_SOURCE, read_sugoroku, snapshot_sugoroku


EVENTS = "events"
EP_BINDINGS = "ep_bindings"
PACKAGE_MEMBERS, TOWER_MEMBERS = "entries", "records"
SHOP_ITEM_COLUMN, EXCHANGE_COUNT_COLUMN = "ShopItemID", "ExchangeCount"
PREMIUM_TRACK = "premium"
# Preflight skips 62 because every board read re-synchronizes the month, yet it is the client's only board read.
BOARD_READ = 62
TOWER_TRADE_STATE = 65
DEFINITION_MISSING = "admin_view_definition_missing"
PACKAGE_OFFER_UNKNOWN = "package_purchase_offer_unknown"
REPAIR_SECTION = "repair"


class EntryStatus(StrEnum):
    DELIVERED = "delivered"
    VOIDED = "voided"
    PENDING = "pending"
    SCHEDULED = "scheduled"
    LAPSED = "lapsed"
    UNCLAIMED = "unclaimed"
    BASELINE = "baseline"
    BOUND = "bound"


class DefinitionState(StrEnum):
    MATCHING = "matching"
    CHANGED = "changed"
    UNAVAILABLE = "unavailable"
    UNFROZEN = "unfrozen"


def unavailable(code: str, message: str) -> ProtocolError:
    return ProtocolError(code, message, HTTPStatus.SERVICE_UNAVAILABLE)


def definition_check(frozen: Any, resolve: Callable[[], Any]) -> dict:
    try:
        current, failure = resolve(), None
    except ProtocolError as error:
        current, failure = None, error_payload(error)
    if frozen is None:
        state = DefinitionState.UNFROZEN
    elif failure is not None:
        state = DefinitionState.UNAVAILABLE
    else:
        state = DefinitionState.MATCHING if current == frozen else DefinitionState.CHANGED
    return {"state": state, "frozen": frozen, "current": current, "error": failure}


def read_ledger(read: Callable[[], Any]) -> tuple[dict, Any]:
    try:
        return {"ok": True}, read()
    except ProtocolError as error:
        return {"ok": False, "error": error_payload(error)}, None


def entry(key: Mapping[str, Any], status: str, *, reason: str | None = None, definition: dict | None = None,
          detail: Mapping[str, Any] | None = None) -> dict:
    return {"key": dict(key), "status": status, "reason": reason, "definition": definition,
            "detail": dict(detail or {})}


def delivered_status(delivery: Mapping[str, Any]) -> str:
    return EntryStatus.VOIDED if voided_delivery(delivery) else EntryStatus.DELIVERED


def delivery_counts(deliveries: Mapping[str, Mapping[str, Any]]) -> dict:
    voided = sum(map(voided_delivery, deliveries.values()))
    return {"delivered": len(deliveries) - voided, "voided": voided}


def without(record: Mapping[str, Any], *names: str) -> dict:
    return {name: value for name, value in record.items() if name not in names}


def numeric(item: tuple[str, Any]) -> int:
    return int(item[0])


def last_time(ledger: Mapping[str, Any]) -> dict:
    value = ledger.get("last_time")
    return {"last_time": value, "last_time_utc": None if value is None else epoch_milliseconds(value)}


def summary(entries: list[dict]) -> dict:
    return {"statuses": dict(Counter(item["status"] for item in entries)),
            "reasons": dict(Counter(item["reason"] for item in entries if item["reason"] is not None)),
            "definitions": dict(Counter(item["definition"]["state"] for item in entries
                                        if item["definition"] is not None))}


def request_order(request: int | str) -> tuple[bool, int | str]:
    return isinstance(request, str), request


@dataclass(frozen=True)
class Readback:
    domains: tuple[str, ...] = ()
    requests: tuple[int | str, ...] = ()

    def __post_init__(self) -> None:
        missing = sorted(set(self.domains) - set(PREFLIGHT_DOMAINS))
        if missing or not (self.domains or self.requests):
            raise ValueError(f"Client readback names preflight domains or explicit requests: {missing}")

    def describe(self) -> dict:
        found = [PREFLIGHT_DOMAINS[name] for name in self.domains]
        requests = {request for domain in found for request in domain.requests} | set(self.requests)
        return {"requests": sorted(requests, key=request_order),
                "none_reasons": [domain.none_reason for domain in found if domain.none_reason is not None]}


def diagnose(read: Callable[[dict], Any], state: dict, key: str, member: str) -> list[dict] | None:
    # Readers that enforce current identities stop at the first mismatch, so each member is checked on its own.
    ledger = state.get(key)
    members = ledger.get(member) if isinstance(ledger, dict) else None
    if not isinstance(members, dict):
        return None
    checked = ((name, read_ledger(lambda: read({**state, key: {**ledger, member: {name: value}}}))[0])
               for name, value in sorted(members.items(), key=lambda item: str(item[0])))
    return [{"key": name, "error": status["error"]} for name, status in checked if not status["ok"]]


@dataclass(frozen=True)
class Rendered:
    ledger: dict
    entries: list[dict] = field(default_factory=list)
    sections: dict = field(default_factory=dict)
    diagnosis: list[dict] | None = None


Render = Callable[[ViewContext], Rendered]
Target = Callable[[Mapping[str, Any]], dict]
REPAIRABLE = frozenset((DefinitionState.CHANGED, DefinitionState.UNAVAILABLE))


def awaits_repair(item: Mapping[str, Any]) -> bool:
    return (item["status"] == EntryStatus.PENDING and item["definition"] is not None
            and item["definition"]["state"] in REPAIRABLE)


def key_fields(*names: str) -> Target:
    return lambda key: {name: key[name] for name in names}


def stamp_month(key: Mapping[str, Any]) -> dict:
    return {"month": month_key(date.fromisoformat(key["day"]))}


def repair_section(command: str, targets: list[dict], section: str = REPAIR_SECTION) -> dict:
    return {section: {"command": command, "targets": targets}} if targets else {}


@dataclass(frozen=True)
class PendingRepair:
    command: str
    target: Target
    section: str = REPAIR_SECTION

    def describe(self, entries: Iterable[dict]) -> dict:
        targets = []
        for item in entries:
            if awaits_repair(item) and (found := self.target(item["key"])) not in targets:
                targets.append(found)
        return repair_section(self.command, targets, self.section)


@dataclass(frozen=True)
class LedgerView:
    name: str
    service: str
    keys: tuple[str, ...]
    readback: Readback
    render: Render
    live: bool = False
    repair: PendingRepair | None = None

    def view(self, context: ViewContext, params: PageParams) -> dict:
        state, bundle = context.state, context.generation.bundle
        found = getattr(bundle, self.service, None)
        rendered = self.render(context)
        result = {"service": {"name": self.service, "available": found is not None,
                              "content_version": getattr(found, "content_version", None)},
                  "ledger": {"keys": {key: key in state for key in self.keys}, **rendered.ledger},
                  "summary": summary(rendered.entries), "entries": page(rendered.entries, params),
                  "readback": self.readback.describe(), **rendered.sections}
        if self.repair is not None and found is not None:
            result.update(self.repair.describe(rendered.entries))
        if rendered.diagnosis is not None:
            result["diagnosis"] = page(rendered.diagnosis, params)
        return result


def ledger_views(domain: str, ledgers: Iterable[LedgerView]) -> tuple[AdminView, ...]:
    return tuple(redacted_view(f"{domain}.{ledger.name}", domain, ledger.view, PageParams, live=ledger.live)
                 for ledger in ledgers)


def event_logins(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, ledger = read_ledger(lambda: read_event_logins(state))
    if ledger is None:
        return Rendered(status)

    def current(campaign_id: int, ordinal: int) -> str:
        campaign = service(bundle, EVENT_LOGINS).catalog.require_public(campaign_id)
        return event_login_definition(campaign, ordinal)

    entries = []
    for key, record in sorted(ledger["campaigns"].items(), key=numeric):
        campaign_id = int(key)
        for ordinal, delivery in sorted(record["deliveries"].items(), key=numeric):
            entries.append(entry({"campaign_id": campaign_id, "ordinal": int(ordinal)}, delivered_status(delivery),
                                 definition=definition_check(delivery["definition"],
                                                             lambda: current(campaign_id, int(ordinal))),
                                 detail=without(delivery, "definition")))
        pending = record["pending"]
        if pending is not None:
            entries.append(entry({"campaign_id": campaign_id, "ordinal": pending["ordinal"]}, EntryStatus.PENDING,
                                 reason=pending["reason"],
                                 definition=definition_check(pending["definition"],
                                                             lambda: current(campaign_id, pending["ordinal"])),
                                 detail={"earned_day": pending["earned_day"]}))
    return Rendered({**status, **last_time(ledger)}, entries)


def schedule(round_key: Mapping[str, Any], record: Mapping[str, Any], resolve: Callable[[int], str], *,
             lapsed: bool) -> list[dict]:
    # Settlement still retries a pending ordinal after its window, but begins no new ordinal once it has closed.
    waiting = EntryStatus.LAPSED if lapsed else EntryStatus.SCHEDULED
    pending, entries = record["pending"], []
    for ordinal, frozen in enumerate(record["reward_definitions"], 1):
        key = {**round_key, "ordinal": ordinal}
        check = definition_check(frozen, lambda: resolve(ordinal))
        delivery = record["deliveries"].get(str(ordinal))
        if delivery is not None:
            entries.append(entry(key, delivered_status(delivery), definition=check,
                                 detail=without(delivery, "definition")))
        elif pending is not None and pending["ordinal"] == ordinal:
            entries.append(entry(key, EntryStatus.PENDING, reason=pending["reason"], definition=check,
                                 detail={"earned_day": pending["earned_day"]}))
        else:
            entries.append(entry(key, waiting, definition=check))
    return entries


def comeback_logins(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, ledger = read_ledger(lambda: read_comeback_logins(state))
    if ledger is None:
        return Rendered(status, sections={"rounds": [], "observation": None})
    day = comeback_clock(context.now)[0]

    def current(record: Mapping[str, Any], ordinal: int) -> str:
        catalog = service(bundle, COMEBACK_LOGINS).catalog
        if record["campaign_id"] != catalog.campaign_id or record["pause_period"] not in catalog.bands:
            raise unavailable(COMEBACK_BAND_UNAVAILABLE,
                              "The current comeback catalog has no reward band for this round")
        return comeback_reward_definition(catalog.require_band(record["pause_period"]), ordinal)

    entries, rounds = [], []
    for key, record in sorted(ledger["rounds"].items(), key=numeric):
        round_id = int(key)
        ongoing = round_id == ledger["current_round_id"]
        entries.extend(schedule({"round_id": round_id, "campaign_id": record["campaign_id"]}, record,
                                lambda ordinal: current(record, ordinal),
                                lapsed=not ongoing or day >= record["end_day"]))
        rounds.append({"round_id": round_id, "current": ongoing, "ordinals": len(record["reward_definitions"]),
                       **delivery_counts(record["deliveries"]),
                       **without(record, "reward_definitions", "deliveries", "pending")})
    observation = {name: ledger[name] for name in ("legacy_projection", "last_seen_day", "current_round_id",
                                                     "next_round_id")}
    return Rendered({**status, **last_time(ledger), "logical_day": day}, entries,
                    {"rounds": rounds, "observation": observation})


def purchased_logins(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, ledger = read_ledger(lambda: read_purchased_logins(state))
    if ledger is None:
        return Rendered(status, sections={"purchases": []})
    day = purchased_login_clock(context.now)[0]

    def current(campaign_id: int, ordinal: int) -> str:
        offer = service(bundle, PURCHASED_LOGINS).catalog.by_campaign_id.get(campaign_id)
        if offer is None:
            raise unavailable(PURCHASED_CAMPAIGN_UNAVAILABLE, "The current sale catalog no longer offers this campaign")
        if not offer.campaign.rewards_for(ordinal):
            raise unavailable(PURCHASED_REWARD_MISSING, "The current campaign has no reward for this login count")
        return event_login_definition(offer.campaign, ordinal)

    entries, purchases = [], []
    for key, record in sorted(ledger["purchases"].items(), key=numeric):
        purchase_id, campaign_id = int(key), record["campaign_id"]
        entries.extend(schedule({"purchase_id": purchase_id, "campaign_id": campaign_id}, record,
                                lambda ordinal: current(campaign_id, ordinal), lapsed=day >= record["end_day"]))
        purchases.append({"purchase_id": purchase_id, "ordinals": len(record["reward_definitions"]),
                          **delivery_counts(record["deliveries"]),
                          **without(record, "reward_definitions", "deliveries", "pending")})
    return Rendered({**status, **last_time(ledger), "next_purchase_id": ledger["next_purchase_id"],
                     "logical_day": day}, entries, {"purchases": purchases})


def gift_status(record: Mapping[str, Any]) -> str:
    if record["status"] != DAILY_GIFT_PENDING:
        return EntryStatus.DELIVERED
    return EntryStatus.VOIDED if record["reason"] == DAILY_GIFT_VOIDED else EntryStatus.PENDING


def daily_gifts(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, ledger = read_ledger(lambda: read_daily_gifts(state))
    if ledger is None:
        return Rendered(status)

    def current(gift_id: int) -> str:
        campaign = service(bundle, DAILY_GIFTS).catalog.by_id.get(gift_id)
        if campaign is None:
            raise unavailable(DAILY_GIFT_SOURCE_UNAVAILABLE, "The current daily gift source no longer has this gift")
        return daily_gift_definition(campaign)

    entries = []
    for key, records in sorted(ledger["gifts"].items(), key=numeric):
        gift_id = int(key)
        for earned_day, record in sorted(records.items()):
            entries.append(entry({"gift_id": gift_id, "earned_day": earned_day}, gift_status(record),
                                 reason=record["reason"],
                                 definition=definition_check(record["definition"], lambda: current(gift_id)),
                                 detail=without(record, "definition", "status", "reason")))
    return Rendered({**status, **last_time(ledger)}, entries)


def login_rewards(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, held = read_ledger(lambda: read_pending_stamps(state))
    progress_status, progress = read_ledger(lambda: read_login_rewards(state))
    cooking_status, cooking = read_ledger(lambda: read_cooking(state))

    def rewards() -> Any:
        return service(bundle, LOGIN_REWARDS)

    def current(stamp: Mapping[str, Any]) -> str:
        catalog = rewards().catalog
        return catalog.stamp(stamp["grade"], stamp["month"], stamp["ordinal"]).definition

    entries = [entry({"day": stamp["day"]}, EntryStatus.PENDING, reason=stamp["reason"],
                     definition=definition_check(stamp["definition"], lambda: current(stamp)),
                     detail=without(stamp, "day", "definition", "reason"))
               for stamp in held or ()]
    frozen, extras = {}, {}
    if progress is not None:
        frozen["beginner_total"] = definition_check(progress["beginner_total"], lambda: rewards().beginner_total)
        if frozen["beginner_total"]["state"] == DefinitionState.CHANGED:
            rules = rewards().beginner_rules(state)
            extras["repair"] = {"command": rules["repair"], "refused_operations": rules["refused_operations"],
                                "client": rules["client"]}
    if cooking is not None:
        frozen["cooking_border"] = definition_check(cooking["border"], lambda: rewards().cooking_border)
        if frozen["cooking_border"]["state"] == DefinitionState.CHANGED:
            rules = rewards().cooking_rules(state)
            extras["cooking_repair"] = {"command": rules["repair"], "offline": rules["offline_repair"],
                                        "refused_operations": rules["refused_operations"], "client": rules["client"]}
    capacity_status, capacity = read_ledger(lambda: rewards().pending_stamps(state))
    return Rendered(status, entries, {
        "progress": {**progress_status, "ledger": progress, **({} if progress is None else last_time(progress))},
        "cooking": {**cooking_status, "ledger": cooking, **({} if cooking is None else last_time(cooking))},
        "frozen_rules": frozen, "pending_stamps": {**capacity_status, **(capacity or {})}, **extras})


def event_passes(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, rows = read_ledger(lambda: read_event_passes(state))
    automation, pending = read_ledger(lambda: EventPassService.pending(state))

    def catalog_entry(pass_id: str) -> Any:
        return service(bundle, EVENT_PASSES).catalog.get(pass_id)

    def current(pass_id: str, track: str, point: int) -> str:
        premium = track == PREMIUM_TRACK
        rewards = [reward for reward in catalog_entry(pass_id).rewards
                   if reward.premium == premium and reward.required_point == point]
        if not rewards:
            raise unavailable(DEFINITION_MISSING, "The current pass has no rewards at this delivered threshold")
        return reward_semantics(rewards)

    # read_event_passes validates the journal against the native rows before returning them.
    journal = state.get(PASS_LEDGER, {}).get("passes", {}) if rows is not None else {}
    entries, passes = [], []
    for row in rows or ():
        pass_id, saved = row["ID"], journal.get(row["ID"])
        for track in TRACKS:
            for point, delivery in sorted((saved or {}).get("deliveries", {}).get(track, {}).items(), key=numeric):
                entries.append(entry({"pass_id": pass_id, "track": track, "point": int(point)}, EntryStatus.DELIVERED,
                                     definition=definition_check(delivery["definition"],
                                                                 lambda: current(pass_id, track, int(point))),
                                     detail=without(delivery, "definition")))
        passes.append({"pass_id": pass_id, "received": {track: row[column] for track, column in TRACKS.items()},
                       "baseline": None if saved is None else {track: saved[f"baseline_{track}"] for track in TRACKS},
                       "identity": definition_check(None if saved is None else saved["definition"],
                                                    lambda: pass_identity(catalog_entry(pass_id))),
                       "archived": [] if saved is None else [record["definition"]
                                                             for record in saved.get(ARCHIVED_KEY, ())]})
    for pass_id, reason in sorted((pending or {}).items()):
        saved = journal.get(pass_id)
        entries.append(entry({"pass_id": pass_id}, EntryStatus.PENDING, reason=reason,
                             definition=definition_check(None if saved is None else saved["definition"],
                                                         lambda: pass_identity(catalog_entry(pass_id)))))
    # The rebind refuses passes that now match or left the catalog, so only changed identities are targets.
    rebinds = [{"pass_id": listed["pass_id"]} for listed in passes
               if listed["identity"]["state"] == DefinitionState.CHANGED]
    return Rendered({**status, "journal": PASS_LEDGER in state}, entries,
                    {"passes": passes, "automation": automation, **repair_section(EVENT_PASS_REPAIR, rebinds)})


def mission_ep_bindings(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, bindings = read_ledger(lambda: service(service(bundle, MISSIONS), EP_BINDINGS))
    if bindings is None:
        return Rendered(status)
    status, prepared = read_ledger(lambda: bindings.prepare(state))
    if prepared is None:
        return Rendered(status)
    stored = state.get(BINDING_LEDGER, {}).get("bindings", {})
    after = prepared.get(BINDING_LEDGER, {}).get("bindings", {})
    entries = [entry({"condition_id": int(key)}, EntryStatus.BOUND,
                     definition=definition_check(stored.get(key), lambda: after.get(key)))
               for key in sorted(stored.keys() | after.keys(), key=int)]
    return Rendered({**status, "stored": BINDING_LEDGER in state}, entries,
                    {"projection": projected(changed_missions=list(bindings.changed_ids), bindings_after=len(after))})


def board_projection(bundle: Any, state: dict, now: datetime) -> dict:
    board = service(bundle, SUGOROKU)
    prepared = board.prepare_month(deepcopy(state), now)
    return projected(as_of=server_time(now), period=read_spending(prepared)["period"],
                     rows=snapshot_sugoroku(prepared, board.catalog, now))


def sugoroku(context: ViewContext) -> Rendered:
    state, bundle, now = context.state, context.generation.bundle, context.now
    catalog_status, catalog = read_ledger(lambda: service(bundle, SUGOROKU).catalog)
    projection_status, projection = read_ledger(lambda: board_projection(bundle, state, now))
    sections = {"catalog": catalog_status, "projection": {**projection_status, **(projection or {})}}
    # The service creates the board baseline on its first read, so a missing ledger is not yet a broken one.
    if BOARD_LEDGER not in state:
        return Rendered({"ok": True}, sections=sections)
    status, ledger = read_ledger(lambda: read_sugoroku(state))
    if ledger is None:
        return Rendered(status, sections=sections)

    def reward(identifier: int) -> dict | None:
        if catalog is None:
            return None
        found = catalog.by_id.get(identifier)
        return {"present": found is not None, "unsupported_reason": None if found is None else found.unsupported_reason}

    month, entries = business_month(now), []
    for period, sheet in sorted(ledger["periods"].items()):
        # Claims reach only the current month's board, so an earlier month's unclaimed rewards are never mailed.
        waiting = EntryStatus.UNCLAIMED if period >= month else EntryStatus.LAPSED
        for key, record in sorted(sheet["records"].items(), key=numeric):
            kind = (EntryStatus.BASELINE if record["source"] == BASELINE_SOURCE
                    else EntryStatus.DELIVERED if record["status"] == CLAIMED else waiting)
            entries.append(entry({"period": period, "reward_id": int(key)}, kind,
                                 detail={"board_status": record["status"], "seen": record["status"] in SEEN_STATUSES,
                                         "mail_ids": record["mail_ids"], "reward": reward(int(key))}))
    baselines = {period: sheet["baseline_spent"] for period, sheet in sorted(ledger["periods"].items())}
    return Rendered({**status, **last_time(ledger), "baselines": baselines, "current_period": month}, entries,
                    sections)


def packages(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, shop = read_ledger(lambda: service(bundle, PACKAGES))
    if shop is None:
        return Rendered(status)

    def current(name: str) -> str:
        offer = shop.catalog.by_normal_id.get(name)
        if offer is None:
            raise unavailable(PACKAGE_OFFER_UNKNOWN, "The current package catalog no longer offers this pass")
        return offer.definition_hash

    status, purchased = read_ledger(lambda: shop.purchases.read(state))
    if purchased is None:
        return Rendered(status, diagnosis=diagnose(shop.purchases.read, state, PACKAGE_LEDGER, PACKAGE_MEMBERS))
    entries = [entry({"normal_pass_id": name},
                     EntryStatus.BASELINE if record["source"] == LEGACY_PACKAGE else EntryStatus.DELIVERED,
                     definition=definition_check(record["identity"], lambda: current(name)),
                     detail=without(record, "identity"))
               for name, record in sorted(purchased.items())]
    return Rendered(status, entries)


def served_counts(history: Any, state: dict, now: datetime) -> dict:
    columns = {column.name: column.values for column in history.response(state, now).children}
    return dict(zip(columns[SHOP_ITEM_COLUMN], columns[EXCHANGE_COUNT_COLUMN]))


def trades(context: ViewContext) -> Rendered:
    state, bundle, now = context.state, context.generation.bundle, context.now
    status, history = read_ledger(lambda: service(bundle, TRADES).history)
    if history is None:
        return Rendered(status)
    status, records = read_ledger(lambda: history.records(state, now))
    if records is None:
        return Rendered(status)
    served_status, served = read_ledger(lambda: served_counts(history, state, now))
    entries = []
    for key, row in sorted(records.items(), key=numeric):
        shop_id = int(key)
        # The stored period is the reset window its count belongs to, not a reward definition.
        period = definition_check(row["period"], lambda: history.period(history.catalog.product(shop_id), now))
        entries.append(entry({"shop_item_id": shop_id}, EntryStatus.DELIVERED,
                             detail={"count": row["count"], "lifetime_count": row["lifetime_count"], "period": period,
                                     "served_count": None if served is None else served.get(shop_id)}))
    return Rendered({**status, "journal": TRADE_LEDGER in state, "served": served_status}, entries)


def tower_trades(context: ViewContext) -> Rendered:
    state, bundle, now = context.state, context.generation.bundle, context.now
    status, history = read_ledger(lambda: service(bundle, TOWER_TRADES).history)
    if history is None:
        return Rendered(status)
    status, records = read_ledger(lambda: history.records(state))
    if records is None:
        return Rendered(status, diagnosis=diagnose(history.records, state, TOWER_LEDGER, TOWER_MEMBERS))
    clock, period = read_ledger(lambda: history.period(now))
    entries = [entry({"shop_id": shop_id}, EntryStatus.DELIVERED,
                     definition=definition_check(record.source,
                                                 lambda: history.source(history.catalog.product(shop_id))),
                     detail={"count": None if period is None else record.current(period), "period": record.period,
                             "lifetime": record.lifetime})
               for shop_id, record in sorted(records.items())]
    return Rendered({**status, "current_period": period, "clock": clock}, entries)


# Live ledgers render against the server clock, so their answers are never served from a cache.
LEDGERS = (
    LedgerView("event_login", EVENT_LOGINS, (EVENT_LOGIN_LEDGER,), Readback(("event_login",)), event_logins,
               repair=PendingRepair(EVENT_LOGIN_REPAIR, key_fields("campaign_id"))),
    LedgerView("comeback_login", COMEBACK_LOGINS, (COMEBACK_LEDGER,), Readback(("comeback_login",)), comeback_logins,
               live=True, repair=PendingRepair(COMEBACK_LOGIN_REPAIR, key_fields("round_id"))),
    LedgerView("purchased_login", PURCHASED_LOGINS, (PURCHASED_LEDGER,), Readback(("purchased_login",)),
               purchased_logins, live=True, repair=PendingRepair(PURCHASED_LOGIN_REPAIR, key_fields("purchase_id"))),
    LedgerView("daily_gift", DAILY_GIFTS, (DAILY_GIFT_LEDGER,), Readback(("daily_gift",)), daily_gifts,
               repair=PendingRepair(DAILY_GIFT_REPAIR, key_fields("gift_id"))),
    LedgerView("login_rewards", LOGIN_REWARDS, (LOGIN_REWARD_LEDGER, PENDING_KEY, COOKING_LEDGER),
               Readback(("login_rewards", "login_cooking")), login_rewards,
               repair=PendingRepair(STAMP_REPAIR, stamp_month, "stamp_repair")),
    LedgerView("event_pass", EVENT_PASSES, (PASS_LEDGER, PASS_AUTOMATION), Readback(("event_pass",)), event_passes),
    LedgerView("mission_ep_bindings", MISSIONS, (BINDING_LEDGER,), Readback(("missions",)), mission_ep_bindings),
    LedgerView("sugoroku", SUGOROKU, (BOARD_LEDGER,), Readback(("sugoroku",), requests=(BOARD_READ,)), sugoroku,
               live=True),
    LedgerView("packages", PACKAGES, (PACKAGE_LEDGER,), Readback(("packages",)), packages),
    # Trade purchases return ExchangeHistory as they write; the startup collections are the only read carrying it.
    LedgerView("trades", TRADES, (TRADE_LEDGER,), Readback(requests=(STARTUP_READ.request,)), trades, live=True),
    LedgerView("tower_trades", TOWER_TRADES, (TOWER_LEDGER,), Readback(requests=(TOWER_TRADE_STATE,)), tower_trades,
               live=True),
)
VIEWS = ledger_views(EVENTS, LEDGERS)


def register(registry: ViewRegistry) -> None:
    for view in VIEWS:
        registry.add(view)
