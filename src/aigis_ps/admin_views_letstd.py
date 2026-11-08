from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from functools import cached_property
from http import HTTPStatus
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .admin_app import server_time
from .admin_cmd_letstd import LETSTD, strict_choice
from .admin_command import CommandParams, error_payload
from .admin_commit import attempt
from .admin_fields import epoch_milliseconds
from .admin_registry import ViewRegistry
from .admin_views import PageParams, ViewContext, page, projected, redacted_view, service
from .admin_views_events import (LedgerView, Readback, Rendered, definition_check, entry, last_time, ledger_views,
                                 read_ledger, unavailable)
from .admin_views_rules import RUNNING_UNKNOWN, running_config
from .config_registry import REGISTRY, ConfigError, ConfigKey
from .errors import ProtocolError
from .letstd_catalog import FREE_CRYSTAL, GOLD, ITEM, UNIT, LetsTDReward
from .letstd_rules import GROUPS, PARTNER_ORDER, PARTNERS, LetsTDRules
from .letstd_service import DEFINITION_ERRORS, LOCAL_LABEL, SETTLE_CAPACITY_ERRORS, VERIFICATION
from .letstd_state import DELIVERED, ENDED, LEDGER_KEY, PENDING, SOURCES, group_record, read_letstd
from .presents import MAX_PRESENTS
from .system_literals import command_id


PRESENTS = "presents"
KEY_FIELDS = ("partner", "group", "reward_id")
KIND_NAMES = MappingProxyType({ITEM: "item", UNIT: "unit", FREE_CRYSTAL: "free_crystal", GOLD: "gold"})
REWARD_FIELDS = ("config_id", "item_type", "kind", "title", "item_id", "card_id", "count", "start", "end")
DELIVERY_FIELDS = ("start_at", "end_at", "mail_ids", "delivered_at", "via", "popup", "needs_adaptation", "retry")
# Each partner's popups list crystals, gold, general items and then units, four lines to a popup.
POPUP_KINDS = (FREE_CRYSTAL, GOLD, ITEM, UNIT)
POPUP_LINES = 4
SINGLE, MULTI = "single", "multi"
TITLE_FORMAT_KEY, TITLE_NAME_PREFIX = "LetsTDCPTitleFormat", "LetsTDCPTitleName_"
# Settle records this reason for frozen rows the current resource no longer has.
SOURCE_UNAVAILABLE = "letstd_source_unavailable"
# The registry names rule classes by module:attribute references relative to the package.
RULES_REFERENCE = f"{LetsTDRules.__module__.rpartition('.')[2]}:{LetsTDRules.__qualname__}"
RULES_UNREGISTERED = "No configuration key loads the LetsTD rules, so their source is unknown"
NATIVE_POPUP_NOTE = ("The native popup text comes from client resources and cannot carry the local label; "
                     "once it is on, the game shows the same notice as a real collaboration")
POPUP_OFF_NOTE = "While native_popup is off, 115/116 carry no LetsTDRewardParam and the game shows none of these popups"
GROUP_ADVICE = ("Enable one reward group at a time: with every group and native_popup on, a single 115 opens "
                "more than a dozen popups in a row")
PROJECTION_SCOPE = ("Only the LetsTD part of the settlement runs here; the other rewards of the same 115/116 "
                    "settle first and can take mailbox room")


class RowStatus(StrEnum):
    UNARMED = "unarmed"
    ADDED = "added"


class Outcome(StrEnum):
    DELIVERABLE = "deliverable"
    DEFERRED = "deferred"
    FAILING = "failing"


class Retry(StrEnum):
    EVERY_SETTLEMENT = "every_settlement"
    NEXT_SETTLEMENT = "next_settlement"
    DEPENDENCY_CHANGE = "dependency_change"
    PAUSED = "paused"


LoginSource = strict_choice(SOURCES, "LetsTD settlement")


class ProjectionParams(CommandParams):
    source: LoginSource = SOURCES[0]


def simulation() -> dict:
    return {"simulated": True, "native_tested": False, "label": LOCAL_LABEL, "label_configurable": False,
            "verification": VERIFICATION}


def rules_key() -> ConfigKey | None:
    return next((spec for spec in REGISTRY.values() if spec.rules_class == RULES_REFERENCE), None)


def rules_source(data_dir: Path, digest: str) -> dict:
    spec = rules_key()
    if spec is None:
        return {"known": False, "key": None, "reason": RULES_UNREGISTERED, "error": None}
    try:
        identifier, config = running_config(data_dir, digest)
    except ConfigError as error:
        return {"known": False, "key": spec.key, "reason": None, "error": error_payload(error)}
    if config is None:
        return {"known": False, "key": spec.key, "reason": RUNNING_UNKNOWN, "error": None}
    return {"known": True, "set": identifier, "reason": None, **config.entries[spec.key].describe()}


def switches(rules: LetsTDRules) -> dict:
    return {"groups": [{"partner": partner, "group": group, "name": name, "enabled": rules.enabled(partner, group)}
                       for partner in PARTNERS for group, name in GROUPS.items()],
            "native_popup": {"enabled": rules.native_popup, "labelable": False, "note": NATIVE_POPUP_NOTE},
            "content_version": rules.content_version, "advice": GROUP_ADVICE}


def overview_view(context: ViewContext, params: None) -> dict:
    bundle, identity = context.generation.bundle, context.generation.generation.identity
    return {"simulation": simulation(), "service": attempt(lambda: service(bundle, LETSTD).status()),
            "switches": attempt(lambda: switches(service(bundle, LETSTD).rules)),
            "source": rules_source(context.services.data_dir, identity.config_set)}


def reward_target(row: LetsTDReward) -> str | int | None:
    return row.item_id if row.item_type == ITEM else row.card_id if row.item_type == UNIT else None


def reward_detail(row: LetsTDReward | None) -> dict:
    if row is None:
        return dict.fromkeys(REWARD_FIELDS)
    return {"config_id": row.config_id, "item_type": row.item_type, "kind": KIND_NAMES.get(row.item_type),
            "title": row.title, "item_id": row.item_id or None, "card_id": row.card_id or None, "count": row.count,
            "start": row.start.isoformat(), "end": None if row.end is None else row.end.isoformat()}


def delivery_detail(row: dict | None, retry: Callable[[dict], Retry | None]) -> dict:
    if row is None:
        return dict.fromkeys(DELIVERY_FIELDS)
    return {"start_at": epoch_milliseconds(row["start_at"]), "end_at": epoch_milliseconds(row["end_at"]),
            "mail_ids": list(row["mail_ids"]), "delivered_at": epoch_milliseconds(row["delivered_at"]),
            "via": row["via"], "popup": row["popup"], "needs_adaptation": row["needs_adaptation"],
            "retry": retry(row)}


def frozen_rows(record: dict | None) -> dict[int, dict]:
    return {} if record is None else {int(key): row for key, row in record["rows"].items()}


def rearm_preview(record: dict | None, current: Mapping[int, LetsTDReward] | None) -> dict | None:
    if record is None or current is None:
        return None
    frozen = frozen_rows(record)
    changed = {reward for reward, row in frozen.items()
               if reward not in current or current[reward].definition != row["definition"]}
    # Rearming adds new rows, restarts undelivered rows whose definition changed and re-marks delivered rows.
    added = sorted(current.keys() - frozen.keys())
    reset = sorted(reward for reward in changed if reward in current and frozen[reward]["status"] != DELIVERED)
    adapted = sorted(reward for reward, row in frozen.items()
                     if row["status"] == DELIVERED and (reward in changed) != row["needs_adaptation"])
    missing = sorted(reward for reward in changed if reward not in current and frozen[reward]["status"] != DELIVERED)
    return {"needed": bool(added or reset or adapted), "added": added, "reset": reset, "adapted": adapted,
            "unavailable": missing}


def current_definition(bundle: Any, partner: str, group: int, reward_id: int) -> str:
    row = service(bundle, LETSTD).catalog.get(partner, group, reward_id)
    if row is None:
        raise unavailable(SOURCE_UNAVAILABLE, "The current LetsTD resource has no row for this reward")
    return row.definition


def catalog_groups(letstd: Any) -> dict[tuple[str, int], dict[int, LetsTDReward]]:
    return {(partner, group): {row.reward_id: row for row in letstd.catalog.rows_for(partner, group)}
            for partner in PARTNERS for group in GROUPS}


@dataclass(frozen=True)
class GroupLedger:
    partner: str
    group: int
    record: dict | None
    current: Mapping[int, LetsTDReward] | None
    letstd: Any

    @property
    def enabled(self) -> bool | None:
        return None if self.letstd is None else self.letstd.rules.enabled(self.partner, self.group)

    @cached_property
    def rearm(self) -> dict | None:
        return rearm_preview(self.record, self.current)

    def retry(self, row: dict) -> Retry | None:
        if row["status"] != PENDING or self.letstd is None:
            return None
        # Settle passes over a switched-off group, so its pending rows wait for the switch instead.
        if not self.enabled:
            return Retry.PAUSED
        # The same rule settle applies before it previews a pending row again.
        retried = (row["reason"] is None or row["reason"] in SETTLE_CAPACITY_ERRORS
                   or row["dependency_version"] != self.letstd.dependency_version)
        return Retry.NEXT_SETTLEMENT if retried else Retry.DEPENDENCY_CHANGE

    def entries(self, bundle: Any) -> list[dict]:
        frozen, known, preview = frozen_rows(self.record), self.current or {}, self.rearm
        marked = set() if preview is None else {*preview["added"], *preview["reset"], *preview["adapted"]}
        result = []
        for reward in sorted(frozen.keys() | known.keys()):
            stored = frozen.get(reward)
            status = (stored["status"] if stored is not None
                      else RowStatus.UNARMED if self.record is None else RowStatus.ADDED)
            check = definition_check(None if stored is None else stored["definition"],
                                     lambda: current_definition(bundle, self.partner, self.group, reward))
            result.append(entry(dict(zip(KEY_FIELDS, (self.partner, self.group, reward))), status,
                                reason=None if stored is None else stored["reason"], definition=check,
                                detail={**reward_detail(known.get(reward)), **delivery_detail(stored, self.retry),
                                        "rearm": reward in marked}))
        return result

    def summary(self, bundle: Any) -> dict:
        record = self.record or {}
        fingerprints = {"rules_version": lambda: service(bundle, LETSTD).rules.content_version,
                        "resource_sha256": lambda: service(bundle, LETSTD).catalog.resource_sha256}
        return {"partner": self.partner, "group": self.group, "name": GROUPS[self.group], "enabled": self.enabled,
                "armed": self.record is not None, "rows": None if self.current is None else len(self.current),
                "armed_at": epoch_milliseconds(record.get("armed_at")),
                "rearmed_at": epoch_milliseconds(record.get("rearmed_at")),
                **{name: None if self.record is None else definition_check(self.record[name], resolve)
                   for name, resolve in fingerprints.items()},
                "statuses": dict(Counter(row["status"] for row in frozen_rows(self.record).values())),
                "rearm": self.rearm}


def ledger(context: ViewContext) -> Rendered:
    state, bundle = context.state, context.generation.bundle
    status, stored = read_ledger(lambda: read_letstd(state))
    if stored is None:
        return Rendered(status)
    catalog_status, current = read_ledger(lambda: catalog_groups(service(bundle, LETSTD)))
    letstd = getattr(bundle, LETSTD, None)
    groups = [GroupLedger(partner, group, group_record(stored, partner, group),
                          None if current is None else current[partner, group], letstd)
              for partner in PARTNERS for group in GROUPS]
    return Rendered({**status, **last_time(stored)}, [item for group in groups for item in group.entries(bundle)],
                    {"groups": [group.summary(bundle) for group in groups], "catalog": catalog_status})


def outcome(error: ProtocolError) -> Outcome:
    # Settle defers rows that fail like this and lets every other failure abort the whole 115/116.
    deferred = error.code in DEFINITION_ERRORS or error.code in SETTLE_CAPACITY_ERRORS
    return Outcome.DEFERRED if deferred or error.status == HTTPStatus.NOT_IMPLEMENTED else Outcome.FAILING


def preview_row(letstd: Any, row: LetsTDReward) -> dict:
    try:
        mails = len(letstd.presents.preview(letstd.rewards(row), 0))
    except ProtocolError as error:
        result = outcome(error)
        retry = (None if result is Outcome.FAILING
                 else Retry.EVERY_SETTLEMENT if error.code in SETTLE_CAPACITY_ERRORS else Retry.DEPENDENCY_CHANGE)
        return {"outcome": result, "mails": None, "error": error_payload(error), "retry": retry}
    return {"outcome": Outcome.DELIVERABLE, "mails": mails, "error": None, "retry": None}


def stored_status(stored: dict, row: LetsTDReward) -> str:
    record = group_record(stored, row.partner, row.group)
    if record is None:
        return RowStatus.UNARMED
    frozen = record["rows"].get(str(row.reward_id))
    return RowStatus.ADDED if frozen is None else frozen["status"]


def deliverability(letstd: Any, state: dict, params: PageParams) -> dict:
    stored, held = read_letstd(state), len(letstd.presents.rows(state))
    entries = [{"key": dict(zip(KEY_FIELDS, (row.partner, row.group, row.reward_id))),
                "enabled": letstd.rules.enabled(row.partner, row.group), "status": stored_status(stored, row),
                "config_id": row.config_id, "kind": KIND_NAMES.get(row.item_type), "target": reward_target(row),
                "count": row.count, **preview_row(letstd, row)} for row in letstd.catalog.rows]
    waiting = [item for item in entries if item["enabled"] and item["status"] not in (DELIVERED, ENDED)]
    mails = sum(item["mails"] for item in waiting if item["outcome"] is Outcome.DELIVERABLE)
    room = MAX_PRESENTS - held
    return {"mailbox": {"held": held, "capacity": MAX_PRESENTS, "room": room},
            "outcomes": dict(Counter(item["outcome"] for item in entries)),
            "enabled_waiting": {"rows": len(waiting), "mails": mails, "fits": mails <= room,
                                "statuses": dict(Counter(item["status"] for item in waiting)),
                                "outcomes": dict(Counter(item["outcome"] for item in waiting))},
            "entries": page(entries, params)}


def deliverability_view(context: ViewContext, params: PageParams) -> dict:
    state, bundle = context.state, context.generation.bundle
    return attempt(lambda: deliverability(service(bundle, LETSTD), state, params))


@dataclass
class PopupLine:
    kind: int
    target: str | int | None
    count: int = 0
    single: int = 0
    multi: int = 0
    rewards: list[dict] = field(default_factory=list)

    def add(self, row: LetsTDReward) -> None:
        # Rows of one line are read in ConfigId order, so the last one supplies the receive texts.
        self.count += row.count
        self.single, self.multi = row.receive_single, row.receive_multi
        self.rewards.append({"group": row.group, "reward_id": row.reward_id, "config_id": row.config_id})

    def describe(self) -> dict:
        # Gold always uses its single text; the other kinds switch to the multiple text above one.
        text = SINGLE if self.count == 1 or self.kind == GOLD else MULTI
        return {"kind": KIND_NAMES[self.kind], "target": self.target, "count": self.count, "text": text,
                "text_key": self.single if text == SINGLE else self.multi, "rewards": list(self.rewards)}


def line_key(row: LetsTDReward) -> tuple[int, int]:
    # General items are kept in a map keyed by the signed CRC32 of their ID, units in one keyed by CardId.
    order = command_id(row.item_id) if row.item_type == ITEM else row.card_id if row.item_type == UNIT else 0
    return POPUP_KINDS.index(row.item_type), order


def popup_lines(rows: Iterable[LetsTDReward]) -> list[PopupLine]:
    lines: dict[tuple[int, int], PopupLine] = {}
    # The client skips item types it cannot distribute.
    for row in sorted((row for row in rows if row.item_type in POPUP_KINDS), key=lambda row: row.config_id):
        lines.setdefault(line_key(row), PopupLine(row.item_type, reward_target(row))).add(row)
    return [lines[key] for key in sorted(lines) if lines[key].count]


def popup_windows(rows: Iterable[LetsTDReward]) -> list[dict]:
    shown, windows = list(rows), []
    for partner in PARTNERS:
        lines = popup_lines(row for row in shown if row.partner == partner)
        for start in range(0, len(lines), POPUP_LINES):
            windows.append({"partner": partner, "window": start // POPUP_LINES + 1,
                            "title": {"format_key": TITLE_FORMAT_KEY, "name_key": TITLE_NAME_PREFIX + partner},
                            "lines": [line.describe() for line in lines[start:start + POPUP_LINES]]})
    return windows


def reward_param(letstd: Any, grants: list[dict]) -> list[dict]:
    # Built as if native_popup were on, so the preview names exactly what the client would receive.
    notice = letstd.response([{**grant, "popup": True} for grant in grants])
    if notice is None:
        return []
    names = [column.name for column in notice.children]
    return [dict(zip(names, values)) for values in zip(*(column.values for column in notice.children))]


def popup_section(letstd: Any, grants: list[dict], rows: list[LetsTDReward]) -> dict:
    shown = letstd.rules.native_popup
    return {"shown": shown, "labelable": False, "note": NATIVE_POPUP_NOTE if shown else POPUP_OFF_NOTE,
            "reward_param": reward_param(letstd, grants), "windows": popup_windows(rows)}


def ordered_rows(ledger: dict) -> dict[tuple[str, int, int], dict]:
    rows = {(partner, int(group), int(reward)): row for partner, groups in ledger["partners"].items()
            for group, record in groups.items() for reward, row in record["rows"].items()}
    return dict(sorted(rows.items(), key=lambda item: (PARTNER_ORDER[item[0][0]], *item[0][1:])))


def stage(row: dict) -> dict:
    return {"status": row["status"], "reason": row["reason"]}


def row_changes(before: dict, after: dict, delivered: set[tuple[str, int, int]]) -> list[dict]:
    previous = ordered_rows(before)
    return [{"key": dict(zip(KEY_FIELDS, key)), "before": None if key not in previous else stage(previous[key]),
             "after": stage(row)}
            for key, row in ordered_rows(after).items()
            if key not in delivered and (key not in previous or stage(previous[key]) != stage(row))]


def grant_key(grant: dict) -> tuple[str, int, int]:
    return tuple(grant[name] for name in KEY_FIELDS)


def grant_row(grant: dict, row: LetsTDReward) -> dict:
    return {**{name: grant[name] for name in KEY_FIELDS}, "config_id": row.config_id,
            "kind": KIND_NAMES.get(row.item_type), "target": reward_target(row), "count": row.count,
            "mails": len(grant["mail_ids"]), "mail_ids": list(grant["mail_ids"])}


def projection(letstd: Any, state: dict, now: datetime, source: int) -> dict:
    before = read_letstd(state)
    settled, grants = letstd.settle(deepcopy(state), now, source=source)
    after = read_letstd(settled)
    keys = [grant_key(grant) for grant in grants]
    rows = [letstd.catalog.get(*key) for key in keys]
    armed = [{"partner": partner, "group": group} for partner in PARTNERS for group in GROUPS
             if group_record(before, partner, group) is None and group_record(after, partner, group) is not None]
    return projected(as_of=server_time(now), source=source, scope=PROJECTION_SCOPE, armed=armed,
                     grants=[grant_row(grant, row) for grant, row in zip(grants, rows)],
                     changes=row_changes(before, after, set(keys)),
                     mailbox={"before": len(letstd.presents.rows(state)), "after": len(letstd.presents.rows(settled)),
                              "capacity": MAX_PRESENTS},
                     popups=popup_section(letstd, grants, rows))


def projection_view(context: ViewContext, params: ProjectionParams) -> dict:
    state, bundle, now = context.state, context.generation.bundle, context.now
    return attempt(lambda: projection(service(bundle, LETSTD), state, now, params.source))


LEDGERS = (LedgerView("ledger", LETSTD, (LEDGER_KEY,), Readback((LETSTD, PRESENTS)), ledger),)
VIEWS = (
    redacted_view(f"{LETSTD}.overview", LETSTD, overview_view),
    *ledger_views(LETSTD, LEDGERS),
    redacted_view(f"{LETSTD}.deliverability", LETSTD, deliverability_view, PageParams),
    redacted_view(f"{LETSTD}.projection", LETSTD, projection_view, ProjectionParams, live=True),
)


def register(registry: ViewRegistry) -> None:
    for view in VIEWS:
        registry.add(view)
