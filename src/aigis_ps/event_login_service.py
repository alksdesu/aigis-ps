from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import date
import hashlib
import json
import re

from .alxml import Column, Container
from .errors import ProtocolError
from .event_login_rules import EventLoginRules
from .event_login_state import (
    begin_event_login, complete_event_login, defer_event_login, event_login_clock, initialize_event_logins,
    read_event_logins, rebind_event_login, validate_event_login_grants, validate_event_login_receipt, void_event_login,
)
from .login_reward_state import RESPONSE_CAPACITY as LOGIN_REWARD_RESPONSE_CAPACITY
from .native_numbers import int32
from .possessions import MAX_ITEM_NAME_BYTES
from .present_items import LEGACY_ITEMS
from .presents import (GIFT_BOX_DEPENDENCIES, MAX_PRESENTS, PRESENT_CAPACITY, PRESENT_CAPACITY_ERRORS,
                       PRESENT_RESPONSE_CAPACITY, PendingAction)
from .rank_reward_state import RESPONSE_CAPACITY as RANK_REWARD_RESPONSE_CAPACITY


DESCRIPTION = "イベントログインボーナス"
POLICY_VERSION = "public-event-login-daily-1"
_HASH = re.compile(r"[a-f0-9]{64}\Z")
CAPACITY_ERRORS = PRESENT_CAPACITY_ERRORS | {PRESENT_RESPONSE_CAPACITY, RANK_REWARD_RESPONSE_CAPACITY,
                                             LOGIN_REWARD_RESPONSE_CAPACITY}
RETIRED_CAMPAIGN_ERRORS = frozenset(("event_login_campaign_unknown", "event_login_campaign_unsupported"))
CAMPAIGN_UNAVAILABLE = "event_login_campaign_unavailable"
REWARD_MISSING = "event_login_reward_missing"
DEFINITION_CHANGED = "event_login_definition_changed"
DEFINITION_MISSING = "event_login_definition_missing"
RESOLVE_UNNEEDED = "event_login_resolve_unneeded"
RESOLVE_OPERATION = "event_login.resolve_changed"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def event_login_definition(campaign, ordinal):
    rewards = campaign.rewards_for(ordinal)
    fields = ("ordinal", "config_id", "item_type", "item_id", "item_param", "param1",
              "evolution_count", "pause_period")
    if not rewards:
        raise ProtocolError(REWARD_MISSING, "Event login ordinal has no reward definitions", 503)
    values = [{field: getattr(reward, field) for field in fields} for reward in rewards]
    return _digest({"campaign_id": campaign.campaign_id, "login_bonus_type": campaign.login_bonus_type,
                    "rewards": sorted(values, key=lambda value: json.dumps(value, sort_keys=True))})


class GiftBoxVersions(Mapping):
    # Reading on lookup lets each fingerprint check refuse a dependency without a version under its own code.
    def __init__(self, presents):
        self._presents = presents

    def __getitem__(self, name):
        if name not in GIFT_BOX_DEPENDENCIES:
            raise KeyError(name)
        dependency = getattr(self._presents, name, None)
        return None if dependency is None else dependency.content_version

    def __contains__(self, name):
        return name in GIFT_BOX_DEPENDENCIES

    def __iter__(self):
        return iter(GIFT_BOX_DEPENDENCIES)

    def __len__(self):
        return len(GIFT_BOX_DEPENDENCIES)


class EventLoginService:
    def __init__(self, catalog, presents, rules=None):
        self.catalog, self.presents = catalog, presents
        self.rules = EventLoginRules() if rules is None else rules
        if not isinstance(self.rules, EventLoginRules):
            raise ValueError("Event login requires verified local rules")
        if self.rules.campaign_ids is not None:
            for identifier in self.rules.campaign_ids:
                catalog.require_public(identifier)
        self.content_version = self._content()
        self._ready()

    def content_version_for(self, dependencies):
        try:
            versions = {"catalog": self.catalog.content_version, "presents": self.presents.asset_sha256,
                        **{name: dependencies[name] for name in GIFT_BOX_DEPENDENCIES}}
            if any(value is not None and (type(value) is not str or not _HASH.fullmatch(value))
                   for value in versions.values()):
                raise ValueError("Event login dependencies require stable fingerprints")
            return _digest({"policy": POLICY_VERSION, "dependencies": versions,
                "templates": dict(self.presents.templates), "rules": self.rules.to_dict(), "description": DESCRIPTION,
                "day": "JST_04", "pending": "one_earned_ordinal_retry_after_expiry",
                "daily_limit": "delivery_day", "missed_days": "no_backfill"})
        except (ValueError, TypeError, AttributeError, KeyError) as error:
            raise ProtocolError("event_login_dependencies_invalid", "Event login dependencies are unavailable", 503) from error

    def replayable_content_versions(self):
        return frozenset(map(self.content_version_for, self.presents.gift_box_configurations()))

    def _content(self):
        return self.content_version_for(GiftBoxVersions(self.presents))

    def _ready(self):
        self.catalog._ready()
        self.presents._ready()
        if self._content() != self.content_version:
            raise ProtocolError("event_login_dependencies_changed", "Event login dependencies changed after setup", 503)

    def initialize(self, state):
        self._ready()
        return initialize_event_logins(state)

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        try:
            catalog_status = self.catalog.status()
        except ProtocolError as failure:
            catalog_status = {"ready": False, "error": failure.code}
        return {"ready": error is None, "error": error, "content_version": self.content_version,
                "catalog": catalog_status, "rules": self.rules.to_dict(), "delivery": "mail",
                "local_policy": POLICY_VERSION, "daily_limit": "One ordinal per campaign delivery day",
                "pending": "Earned rewards may retry after expiry; missed days are not accrued",
                "unavailable": ["Comeback eligibility", "Purchased daily reward eligibility"], "native_tested": False}

    def _rewards(self, campaign, ordinal):
        result = []
        for reward in campaign.rewards_for(ordinal):
            if len(result) >= MAX_PRESENTS:
                raise ProtocolError(PRESENT_CAPACITY, "Event login rewards exceed mailbox capacity", 409)
            kind, amount, item, parameter = reward.item_type, reward.item_param, reward.item_id, reward.param1
            if (reward.evolution_count or reward.pause_period or amount <= 0
                    or kind not in (0, 1, 2, 16, 20, 22, *LEGACY_ITEMS)
                    or (kind == 22) != bool(item)
                    or kind not in (16, 20) and parameter or kind in (16, 20) and parameter <= 0):
                raise ProtocolError("event_login_reward_unsupported", "Event login reward shape is unsupported", 501)
            if kind == 0:
                values = {"kind": "free_crystal", "amount": amount}
            elif kind == 1:
                if amount not in self.presents.templates:
                    raise ProtocolError("event_login_template_unsupported", "Event login unit template is unavailable", 501)
                values = {"kind": "unit", "template_id": amount, "count": 1}
            elif kind == 2:
                values = {"kind": "gold", "amount": amount}
            elif kind == 16:
                values = {"kind": "summon_ticket", "pool_id": parameter, "count": amount}
            elif kind == 20:
                values = {"kind": "selection_ticket", "config_id": parameter, "count": amount}
            elif kind == 22:
                if len(item.encode("utf-8")) > MAX_ITEM_NAME_BYTES:
                    # The mail writer refuses such new rows with 503, which 115 and 116 would not defer.
                    raise ProtocolError("event_login_reward_unsupported", "Event login reward item does not fit the native cell", 501)
                recovery = getattr(self.presents, "recovery_catalog", None)
                if recovery is not None and recovery.supports_item(item):
                    if amount > MAX_PRESENTS - len(result):
                        raise ProtocolError(PRESENT_CAPACITY, "Event login recovery rewards exceed mailbox capacity", 409)
                    result.extend({"kind": "charisma_recovery", "item_id": item, "description": DESCRIPTION}
                                  for _ in range(amount))
                    continue
                values = {"kind": "item", "item_id": item, "amount": amount}
            else:
                values = {"kind": "legacy_item", "present_type": kind, "amount": amount}
            result.append({**values, "description": DESCRIPTION})
        if not result:
            raise ProtocolError(REWARD_MISSING, "Event login ordinal has no deliverable rewards", 503)
        return result

    def _mail_candidate(self, before, candidate, grouped, rewards):
        if (not isinstance(candidate, dict) or not isinstance(grouped, list) or len(grouped) != 1
                or not isinstance(grouped[0], list) or not grouped[0]):
            raise ProtocolError("event_login_delivery_invalid", "Event login mail delivery returned invalid groups", 503)
        additions = grouped[0]
        expected_rows = self.presents._prepare_reward_rows(rewards, len(self.presents.rows(before)))
        if [{key: value for key, value in row.items() if key != "PresentID"} for row in additions] != expected_rows:
            raise ProtocolError("event_login_delivery_invalid", "Event login mail differs from its reward definitions", 503)
        last = before.get("last_present_id", 0)
        if [row.get("PresentID") for row in additions] != list(range(last + 1, last + len(additions) + 1)):
            raise ProtocolError("event_login_delivery_invalid", "Event login mail identities are not sequential", 503)
        expected = deepcopy(before)
        expected.update(presents_schema=1, last_present_id=last + len(additions))
        expected["tables"]["presents"] = deepcopy(self.presents.rows(before)) + deepcopy(additions)
        if _digest(expected) != _digest(candidate):
            raise ProtocolError("event_login_delivery_invalid", "Event login mail delivery changed unrelated state", 503)
        self.presents.rows(candidate)
        return [row["PresentID"] for row in additions]

    def _mail(self, state, campaign, ordinal, now):
        rewards = self._rewards(campaign, ordinal)
        candidate, grouped = self.presents.enqueue_groups(deepcopy(state), [rewards])
        ids = self._mail_candidate(state, candidate, grouped, rewards)
        return complete_event_login(candidate, campaign.campaign_id, ids, now)

    def _campaign(self, campaign_id, ordinal):
        try:
            campaign = self.catalog.require_public(campaign_id)
        except ProtocolError as failure:
            if failure.code not in RETIRED_CAMPAIGN_ERRORS:
                raise
            return None
        return campaign if ordinal <= campaign.max_ordinal and campaign.rewards_for(ordinal) else None

    def settle(self, state, now, validate=None):
        self._ready()
        if validate is not None and not callable(validate):
            raise ValueError("Event login validation must be callable")
        updated = self.initialize(state)
        ledger = read_event_logins(updated)
        if not self.rules.enabled:
            return updated, []
        day, timestamp = event_login_clock(now)
        if ledger["last_time"] is not None and timestamp < ledger["last_time"]:
            raise ProtocolError("event_login_clock_backwards", "Event login clock precedes saved progress", 409)
        active = {entry.campaign_id for entry in self.catalog.active(date.fromisoformat(day), self.rules.campaign_ids)}
        pending_ids = {int(key) for key, value in ledger["campaigns"].items() if value["pending"] is not None}
        selected = set(self.rules.campaign_ids) if self.rules.campaign_ids is not None else pending_ids
        grants = []
        for identifier in sorted(active | (pending_ids & selected)):
            self._ready()
            try:
                campaign = self.catalog.require_public(identifier)
            except ProtocolError as failure:
                if identifier not in pending_ids or failure.code not in RETIRED_CAMPAIGN_ERRORS:
                    raise
                updated = defer_event_login(updated, identifier, CAMPAIGN_UNAVAILABLE)
                continue
            record = read_event_logins(updated)["campaigns"].get(str(identifier), {"deliveries": {}, "pending": None})
            pending = record["pending"]
            ordinal = pending["ordinal"] if pending else len(record["deliveries"]) + 1
            if pending is None:
                if identifier not in active or ordinal > campaign.max_ordinal:
                    continue
                if record["deliveries"] and record["deliveries"][str(ordinal - 1)]["delivery_day"] >= day:
                    continue
                definition = event_login_definition(campaign, ordinal)
                updated = begin_event_login(updated, identifier, ordinal, definition, now)
            elif ordinal > campaign.max_ordinal or not campaign.rewards_for(ordinal):
                updated = defer_event_login(updated, identifier, REWARD_MISSING)
                continue
            else:
                definition = event_login_definition(campaign, ordinal)
                if pending["definition"] != definition:
                    updated = defer_event_login(updated, identifier, DEFINITION_CHANGED)
                    continue
            try:
                candidate, grant = self._mail(updated, campaign, ordinal, now)
                combined = [*grants, grant]
                validate_event_login_grants(combined)
                if validate is not None:
                    inspected = deepcopy(candidate)
                    validate(inspected, deepcopy(combined))
                    if _digest(inspected) != _digest(candidate):
                        raise ProtocolError("event_login_validation_invalid", "Event login validation changed candidate state", 503)
            except ProtocolError as failure:
                if failure.status != 501 and failure.code not in CAPACITY_ERRORS:
                    raise
                updated = defer_event_login(updated, identifier, failure.code)
                continue
            updated, grants = candidate, combined
        self._ready()
        self.validate_receipt(updated, grants)
        return updated, grants

    def changed_reward(self, state, campaign_id):
        if not int32(campaign_id) or campaign_id < 1:
            raise ProtocolError("event_login_selection_invalid", "Event logins are chosen by a positive int32 campaign ID")
        record = read_event_logins(state)["campaigns"].get(str(campaign_id))
        pending = None if record is None else record["pending"]
        if pending is None:
            return None
        campaign = self._campaign(campaign_id, pending["ordinal"])
        if campaign is not None and event_login_definition(campaign, pending["ordinal"]) == pending["definition"]:
            return None
        return pending

    def resolve_changed(self, state, campaign_id, action, now):
        if action not in PendingAction:
            raise ProtocolError("event_login_action_invalid", "Changed event login rewards are redelivered or voided")
        updated = self.initialize(state)
        pending = self.changed_reward(updated, campaign_id)
        if pending is None:
            raise ProtocolError(RESOLVE_UNNEEDED, "This campaign has no pending reward with a changed definition", 409)
        if action == PendingAction.VOID:
            return void_event_login(updated, campaign_id), []
        campaign = self._campaign(campaign_id, pending["ordinal"])
        if campaign is None:
            raise ProtocolError(DEFINITION_MISSING, "The current catalog no longer defines this reward; void it", 409)
        updated = rebind_event_login(updated, campaign_id, event_login_definition(campaign, pending["ordinal"]))
        try:
            updated, grant = self._mail(updated, campaign, pending["ordinal"], now)
        except ProtocolError as failure:
            if failure.status != 501 and failure.code not in CAPACITY_ERRORS:
                raise
            return defer_event_login(updated, campaign_id, failure.code), []
        self.validate_receipt(updated, [grant])
        return updated, [grant]

    @staticmethod
    def response(grants):
        validate_event_login_grants(grants)
        return Container("EBNS", (Column("LoginBonusID", "I", tuple(grant["campaign_id"] for grant in grants)),
            Column("LoginCount", "I", tuple(grant["login_count"] for grant in grants)),
            Column("PurchaseTime", "S", tuple(grant["purchase_time"] for grant in grants))))

    def validate_receipt(self, state, grants, *, verify_current=True):
        if type(verify_current) is not bool:
            raise ValueError("Event login receipt validation requires a boolean policy")
        self._ready()
        validate_event_login_receipt(state, grants)
        if not verify_current or not grants:
            return
        indexed = {row["PresentID"]: row for row in self.presents.rows(state)}
        for grant in grants:
            campaign = self.catalog.require_public(grant["campaign_id"])
            ordinal = grant["login_count"]
            if event_login_definition(campaign, ordinal) != grant["definition"]:
                raise ProtocolError("event_login_receipt_invalid", "Event login receipt differs from its current reward", 503)
            rewards = self.presents._prepare_reward_rows(self._rewards(campaign, ordinal), 0)
            if len(rewards) != len(grant["mail_ids"]):
                raise ProtocolError("event_login_receipt_invalid", "Event login receipt has an incorrect reward count", 503)
            for identifier, reward in zip(grant["mail_ids"], rewards):
                if identifier in indexed and indexed[identifier] != {"PresentID": identifier, **reward}:
                    raise ProtocolError("event_login_receipt_invalid", "Event login mail differs from the recorded reward", 503)
