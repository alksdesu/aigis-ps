from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import re
from types import MappingProxyType

from .energy_clock import instant
from .errors import ProtocolError
from .masterdata import digest, json_bytes
from .native_numbers import int32
from .quest_economy import _row
from .vote_quest_tickets import DEPENDENCIES_UNAVAILABLE, SETUP_ERRORS, VoteQuestTickets, VoteTicketGrant


SCHEMA = 1
RESULT, ENTRUST = "result", "entrust"
# 460020 rewrites EventParam on every result even without event drops; entrust rewrites it only for event drops.
EVENT_PARAM_REWRITE = MappingProxyType({RESULT: True, ENTRUST: False})
# Native 460020 caps the locally accumulated EventParam at 1800 before the result request.
EVENT_PARAM_LIMIT = 1800
EVENT_PARAM_RULES = MappingProxyType({
    "limit": EVENT_PARAM_LIMIT, "value": "min_previous_plus_points_and_limit",
    **{source: "rewrite_every_settlement" if every else "rewrite_with_event_points"
       for source, every in EVENT_PARAM_REWRITE.items()},
    "changed": "event_points_or_new_value"})
PROVIDERS = ("vote_tickets", "event_param")
RULES = MappingProxyType({"items": "positive_increments_for_the_single_capped_credit",
                          "overflow": "event_increments_discarded_first", "state": "read_only",
                          "drop_bonus": "applied_in_the_drop_ledger",
                          "ticket_clock": "result_attempt_start_entrust_execution"})
NOT_ATTACHED = "vote_quest_tickets_not_attached"
# Receipts committed while the provider was missing for one of these reasons replay once it attaches.
ABSENT_PROVIDER_ERRORS = (NOT_ATTACHED, DEPENDENCIES_UNAVAILABLE, *SETUP_ERRORS)
UNAVAILABLE = "unavailable"
_CODE = re.compile(r"[a-z0-9_]{1,128}\Z")


def _native(value, minimum=0):
    return int32(value) and value >= minimum


def _positive_items(values):
    return isinstance(values, Mapping) and all(
        type(item) is str and item and _native(count, 1) for item, count in values.items())


def _failure(code, message):
    return ProtocolError(code, message, 503)


def _context_invalid():
    return _failure("battle_event_context_invalid",
                    "Battle events require a known source, native counts and a datetime")


def _grant_invalid():
    return _failure("battle_event_grant_invalid",
                    "Battle event grants hold positive increments, a capped EventParam and a JSON receipt")


def _json(value):
    # Receipts are journaled as JSON, so they are kept exactly as a reload returns them.
    return json.loads(json.dumps(value, allow_nan=False))


def _moment(value):
    return value.isoformat(timespec="seconds")


@dataclass(frozen=True)
class BattleEventContext:
    source: str
    quest_id: int
    quest_type: int
    charisma_per_run: int
    runs: int
    event_points: int
    now: datetime
    rewrite_event_param: bool

    def __post_init__(self):
        if (type(self.source) is not str or self.source not in EVENT_PARAM_REWRITE
                or type(self.rewrite_event_param) is not bool
                or self.rewrite_event_param != EVENT_PARAM_REWRITE[self.source]
                or not _native(self.quest_id, 1) or not _native(self.quest_type)
                or not _native(self.charisma_per_run) or not _native(self.runs, 1) or not _native(self.event_points)):
            raise _context_invalid()
        try:
            object.__setattr__(self, "now", instant(self.now))
        except ProtocolError as error:
            raise _context_invalid() from error


@dataclass(frozen=True)
class BattleEventGrant:
    items: Mapping[str, int]
    event_param: int | None
    event_param_changed: bool
    receipt: dict

    def __post_init__(self):
        try:
            receipt = _json(self.receipt) if isinstance(self.receipt, dict) else None
        except (TypeError, ValueError) as error:
            raise _grant_invalid() from error
        value, changed = self.event_param, self.event_param_changed
        counter = value is None or (_native(value) and value <= EVENT_PARAM_LIMIT)
        if (receipt is None or not _positive_items(self.items) or not counter
                or type(changed) is not bool or (value is None and changed)):
            raise _grant_invalid()
        object.__setattr__(self, "items", MappingProxyType(dict(self.items)))
        object.__setattr__(self, "receipt", receipt)


def _event_param(state, context):
    points = context.event_points
    if not context.rewrite_event_param and not points:
        return None, False, {"rewritten": False, "points": points, "previous": None, "value": None, "changed": False}
    previous = _row(state).get("EventParam")
    if not _native(previous):
        raise _failure("quest_economy_state", "GameParam EventParam must be a nonnegative int32 value")
    value = min(previous + points, EVENT_PARAM_LIMIT)
    # The client adds dropped points itself, so AF is resent even when the cap keeps the value.
    changed = bool(points) or value != previous
    return value, changed, {"rewritten": True, "points": points, "previous": previous, "value": value,
                            "changed": changed}


def _version(provider, error):
    tickets = {"content_version": provider.content_version} if provider is not None else {"error": error}
    return digest(json_bytes({"schema": SCHEMA, "vote_tickets": tickets, "event_param": dict(EVENT_PARAM_RULES)}))


def _tickets(provider, error, state, context):
    if provider is None:
        return {"error": error, "item_id": None, "count": 0, "reason": UNAVAILABLE}
    grant = provider.evaluate(state, charisma_per_run=context.charisma_per_run, runs=context.runs, now=context.now)
    if not isinstance(grant, VoteTicketGrant):
        raise _failure("battle_event_provider_invalid", "Quest ticket providers must return a ticket grant")
    return {"error": None, **asdict(grant)}


class BattleEventRewards:
    def __init__(self):
        self._vote_tickets = (None, NOT_ATTACHED)

    def attach_vote_tickets(self, provider, error=None):
        if provider is None:
            if type(error) is not str or not _CODE.fullmatch(error):
                raise ValueError("A missing quest ticket provider must report its error code")
        elif (error is not None or not callable(getattr(provider, "evaluate", None))
              or not callable(getattr(provider, "status", None))
              or type(getattr(provider, "content_version", None)) is not str):
            raise ValueError("Quest ticket providers need evaluate, status and a content version, and no error")
        self._vote_tickets = (provider, error)

    @property
    def vote_quest_tickets(self) -> VoteQuestTickets | None:
        return self._vote_tickets[0]

    @property
    def content_version(self) -> str:
        return _version(*self._vote_tickets)

    def replayable_content_versions(self) -> frozenset[str]:
        return frozenset((self.content_version, *(_version(None, error) for error in ABSENT_PROVIDER_ERRORS)))

    def evaluate(self, state, context: BattleEventContext) -> BattleEventGrant:
        if not isinstance(context, BattleEventContext):
            raise _context_invalid()
        provider, error = self._vote_tickets
        tickets = _tickets(provider, error, state, context)
        event_param, changed, event_receipt = _event_param(state, context)
        items = {tickets["item_id"]: tickets["count"]} if tickets["count"] else {}
        return BattleEventGrant(items, event_param, changed, {
            "schema": SCHEMA, "content_version": _version(provider, error),
            "context": {**asdict(context), "now": _moment(context.now)},
            "items": items, "vote_tickets": tickets, "event_param": event_receipt})

    def preview(self, state, context: BattleEventContext) -> BattleEventGrant:
        # Console previews run on a private copy, so no provider can reach the caller's state.
        return self.evaluate(deepcopy(state), context)

    def receipt(self, grant: BattleEventGrant, discarded: Mapping[str, int]) -> dict:
        if not isinstance(grant, BattleEventGrant) or not _positive_items(discarded):
            raise _failure("battle_event_receipt_invalid", "Battle event receipts need a grant and positive discards")
        # Tickets are the only possession increments and join the drops last, so overflow is charged to them first.
        tickets = {item: min(count, discarded[item]) for item, count in grant.items.items() if item in discarded}
        return {**_json(grant.receipt), "discarded": {"vote_tickets": tickets}}

    def status(self, now=None) -> dict:
        try:
            moment = None if now is None else instant(now)
        except ProtocolError as failure:
            raise _failure("battle_event_clock_invalid", "Battle event status requires a datetime") from failure
        provider, error = self._vote_tickets
        return {"schema": SCHEMA, "content_version": _version(provider, error), "providers": list(PROVIDERS),
                "vote_tickets": {"attached": provider is not None, "error": error,
                                 "status": None if provider is None else provider.status(moment)},
                "event_param": dict(EVENT_PARAM_RULES), "rules": dict(RULES),
                "now": None if moment is None else _moment(moment), "native_tested": False}
