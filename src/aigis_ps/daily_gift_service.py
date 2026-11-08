from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
import re

from .daily_gift_rules import DailyGiftRules
from .daily_gift_state import (LEDGER_KEY, PENDING, begin_daily_gift, complete_daily_gift, daily_gift_clock,
    defer_daily_gift, initialize_daily_gifts, read_daily_gifts, validate_daily_gift_grants, validate_daily_gift_receipt)
from .errors import ProtocolError
from .event_login_service import CAPACITY_ERRORS, GiftBoxVersions
from .native_numbers import INT32_MAX
from .possessions import MAX_ITEM_NAME_BYTES
from .presents import GIFT_BOX_DEPENDENCIES, MAX_PRESENTS, PRESENT_CAPACITY, PendingAction


_HASH = re.compile(r"[a-f0-9]{64}\Z")
REWARD_FIELDS = ("gift_entry_id", "gift_type", "item_type", "parameter", "item_id", "condition_type", "condition_parameter")
SOURCE_UNAVAILABLE = "daily_gift_source_unavailable"
DEFINITION_CHANGED = "daily_gift_definition_changed"
VOIDED = "daily_gift_voided"
RESOLVE_UNNEEDED = "daily_gift_resolve_unneeded"
RESOLVE_OPERATION = "daily_gift.resolve_changed"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def daily_gift_definition(campaign):
    rewards = sorted(({key: getattr(reward, key) for key in REWARD_FIELDS} for reward in campaign.rewards),
                     key=lambda reward: reward["gift_entry_id"])
    return _digest({"schema": 1, "gift_id": campaign.gift_id, "description": campaign.description, "rewards": rewards})


def _awaiting(record):
    # A voided gift keeps its earned day so that the same day is never earned again.
    return record["status"] == PENDING and record["reason"] != VOIDED


class DailyGiftService:
    def __init__(self, catalog, presents, rules=None):
        self.catalog, self.presents = catalog, presents
        self.rules = catalog.rules if rules is None else rules
        if not isinstance(self.rules, DailyGiftRules) or self.rules != catalog.rules:
            raise ValueError("Daily gift rules must match the catalog")
        self.content_version = self._content()
        self._ready()

    def content_version_for(self, dependencies):
        try:
            versions = {"catalog": self.catalog.content_version, "presents": self.presents.asset_sha256,
                        **{name: dependencies[name] for name in GIFT_BOX_DEPENDENCIES}}
            if (any(versions[key] is None for key in ("catalog", "presents"))
                    or any(value is not None and (type(value) is not str or not _HASH.fullmatch(value))
                           for value in versions.values())):
                raise ValueError("Daily gift sources require stable fingerprints")
            return _digest({"schema": 1, "dependencies": versions, "templates": dict(self.presents.templates),
                "rules": self.rules.to_dict(), "eligibility": "one_per_gift_per_observed_jst04_day_inclusive",
                "missing_days": "no_backfill", "pending": "retain_each_earned_day_across_windows_and_selection",
                "zero_item": "verified_nonstack_recovery_or_item_ticket_only"})
        except (ValueError, TypeError, AttributeError, KeyError) as error:
            raise ProtocolError("daily_gift_dependencies_invalid", "Daily gift dependencies are unavailable", 503) from error

    def replayable_content_versions(self):
        return frozenset(map(self.content_version_for, self.presents.gift_box_configurations()))

    def _content(self):
        return self.content_version_for(GiftBoxVersions(self.presents))

    def _ready(self):
        self.catalog._ready()
        self.presents._ready()
        if self.rules != self.catalog.rules or self._content() != self.content_version:
            raise ProtocolError("daily_gift_dependencies_changed", "Daily gift dependencies changed after setup", 503)

    def initialize(self, state):
        self._ready()
        return initialize_daily_gifts(state)

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "content_version": self.content_version,
            "rules": self.rules.to_dict(), "catalog": self.catalog.status(), "delivery": "mail",
            "local_policy": "Each observed active JST04 day earns one gift; absent days are not backfilled",
            "pending_policy": "Earned gifts survive expiry, selection changes and disabled eligibility",
            "native_tested": False}

    def _rewards(self, campaign):
        result = []
        for reward in sorted(campaign.rewards, key=lambda value: value.gift_entry_id):
            if reward.gift_type != 5 or reward.condition_type != 0 or reward.condition_parameter != 0:
                raise ProtocolError("daily_gift_reward_unsupported", "Daily gift conditions or delivery type are unsupported", 501)
            kind, amount, item = reward.item_type, reward.parameter, reward.item_id
            if type(amount) is not int or amount < 0 or amount > INT32_MAX or kind not in (0, 1, 2, 22):
                raise ProtocolError("daily_gift_reward_unsupported", "Daily gift reward type or amount is unsupported", 501)
            if (kind == 22 and not item) or (kind != 22 and item):
                raise ProtocolError("daily_gift_reward_unsupported", "Daily gift item identity is inconsistent", 501)
            description = campaign.description
            if kind == 22:
                if len(item.encode("utf-8")) > MAX_ITEM_NAME_BYTES:
                    # The mail writer refuses such new rows with 503, which 115 and 116 would not defer.
                    raise ProtocolError("daily_gift_reward_unsupported", "Daily gift item does not fit the native cell", 501)
                recovery = self.presents.recovery_catalog.supports_item(item)
                tickets = self.presents.item_tickets
                ticket = tickets is not None and tickets.catalog.supports_item(item)
                if amount == 0:
                    if not recovery and not ticket:
                        raise ProtocolError("daily_gift_zero_item_unsupported", "Zero quantity requires a verified nonstack gift", 501)
                    amount = 1
                if recovery:
                    if amount > MAX_PRESENTS - len(result):
                        raise ProtocolError(PRESENT_CAPACITY, "Daily gift recovery rewards exceed mailbox capacity", 409)
                    result.extend({"kind": "charisma_recovery", "item_id": item, "description": description}
                                  for _ in range(amount))
                else:
                    result.append({"kind": "item", "item_id": item, "amount": amount, "description": description})
            elif amount == 0:
                raise ProtocolError("daily_gift_reward_unsupported", "Daily gift quantity must be positive", 501)
            elif kind == 1:
                if amount not in self.presents.templates:
                    raise ProtocolError("daily_gift_template_unsupported", "Daily gift unit template is unavailable", 501)
                result.append({"kind": "unit", "template_id": amount, "count": 1, "description": description})
            else:
                result.append({"kind": "free_crystal" if kind == 0 else "gold", "amount": amount, "description": description})
            if len(result) > MAX_PRESENTS:
                raise ProtocolError(PRESENT_CAPACITY, "Daily gift rewards exceed mailbox capacity", 409)
        if not result:
            raise ProtocolError("daily_gift_reward_unsupported", "Daily gift has no reward", 501)
        return result

    def _mail_candidate(self, before, candidate, groups, rewards):
        if (not isinstance(candidate, dict) or not isinstance(groups, list) or len(groups) != 1
                or not isinstance(groups[0], list) or not groups[0]):
            raise ProtocolError("daily_gift_delivery_invalid", "Daily gift mail groups are invalid", 503)
        additions = groups[0]
        rows = self.presents.rows(before)
        expected_rows = self.presents._prepare_reward_rows(rewards, len(rows))
        if [{key: value for key, value in row.items() if key != "PresentID"} for row in additions] != expected_rows:
            raise ProtocolError("daily_gift_delivery_invalid", "Daily gift mail differs from its source rewards", 503)
        last = before.get("last_present_id", 0)
        ids = [row["PresentID"] for row in additions]
        if ids != list(range(last + 1, last + len(additions) + 1)):
            raise ProtocolError("daily_gift_delivery_invalid", "Daily gift mail identities are not sequential", 503)
        expected = deepcopy(before)
        expected.update(presents_schema=1, last_present_id=last + len(additions))
        expected["tables"]["presents"] = rows + deepcopy(additions)
        if _digest(expected) != _digest(candidate):
            raise ProtocolError("daily_gift_delivery_invalid", "Daily gift mail changed unrelated state", 503)
        self.presents.rows(candidate)
        return ids

    def _mail(self, state, campaign, earned, now):
        rewards = self._rewards(campaign)
        candidate, groups = self.presents.enqueue_groups(deepcopy(state), [rewards])
        ids = self._mail_candidate(state, candidate, groups, rewards)
        return complete_daily_gift(candidate, campaign.gift_id, earned, ids, now)

    def settle(self, state, now, validate=None):
        if validate is not None and not callable(validate):
            raise ValueError("Daily gift validation must be callable")
        updated = self.initialize(state)
        ledger = read_daily_gifts(updated)
        if not self.rules.enabled and not any(_awaiting(record)
                for records in ledger["gifts"].values() for record in records.values()):
            return updated, []
        day, timestamp = daily_gift_clock(now)
        if ledger["last_time"] is not None and timestamp < ledger["last_time"]:
            raise ProtocolError("daily_gift_clock_backwards", "Daily gift clock precedes its watermark", 409)
        for campaign in self.catalog.active(date.fromisoformat(day)):
            updated = begin_daily_gift(updated, campaign.gift_id, daily_gift_definition(campaign),
                campaign.start_date.isoformat(), campaign.end_date.isoformat(), now)
        pending = [(int(key), earned) for key, records in read_daily_gifts(updated)["gifts"].items()
                   for earned, record in records.items() if _awaiting(record)]
        grants = []
        for gift_id, earned in sorted(pending):
            self._ready()
            campaign = self.catalog.by_id.get(gift_id)
            record = read_daily_gifts(updated)["gifts"][str(gift_id)][earned]
            if campaign is None:
                updated = defer_daily_gift(updated, gift_id, earned, SOURCE_UNAVAILABLE)
                continue
            if daily_gift_definition(campaign) != record["definition"]:
                updated = defer_daily_gift(updated, gift_id, earned, DEFINITION_CHANGED)
                continue
            try:
                candidate, grant = self._mail(updated, campaign, earned, now)
                combined = [*grants, grant]
                validate_daily_gift_grants(combined)
                if validate is not None:
                    inspected = deepcopy(candidate)
                    validate(inspected, deepcopy(combined))
                    if _digest(inspected) != _digest(candidate):
                        raise ProtocolError("daily_gift_validation_invalid", "Daily gift validator changed candidate state", 503)
            except ProtocolError as failure:
                if failure.status != 501 and failure.code not in CAPACITY_ERRORS:
                    raise
                updated = defer_daily_gift(updated, gift_id, earned, failure.code)
                continue
            updated, grants = candidate, combined
        self.validate_receipt(updated, grants)
        return updated, grants

    def _changed_days(self, state, gift_id):
        if type(gift_id) is not int or not 1 <= gift_id <= INT32_MAX:
            raise ProtocolError("daily_gift_selection_invalid", "Daily gifts are chosen by a positive int32 gift ID")
        campaign = self.catalog.by_id.get(gift_id)
        definition = None if campaign is None else daily_gift_definition(campaign)
        records = read_daily_gifts(state)["gifts"].get(str(gift_id), {})
        return tuple(earned for earned, record in sorted(records.items())
                     if _awaiting(record) and record["definition"] != definition)

    def resolve_changed(self, state, gift_id, action, now):
        if action not in PendingAction:
            raise ProtocolError("daily_gift_action_invalid", "Changed daily gifts are redelivered or voided")
        updated = self.initialize(state)
        changed = self._changed_days(updated, gift_id)
        if not changed:
            raise ProtocolError(RESOLVE_UNNEEDED, "No pending gift of this ID has a changed definition", 409)
        if action == PendingAction.VOID:
            for earned in changed:
                updated = defer_daily_gift(updated, gift_id, earned, VOIDED)
            return updated, []
        campaign = self.catalog.by_id.get(gift_id)
        if campaign is None:
            raise ProtocolError(SOURCE_UNAVAILABLE, "The current daily gift source no longer has this gift; void it", 409)
        ledger, definition = read_daily_gifts(updated), daily_gift_definition(campaign)
        for earned in changed:
            ledger["gifts"][str(gift_id)][earned].update(definition=definition, reason=None)
        updated[LEDGER_KEY] = ledger
        read_daily_gifts(updated)
        grants = []
        for earned in changed:
            try:
                updated, grant = self._mail(updated, campaign, earned, now)
            except ProtocolError as failure:
                if failure.status != 501 and failure.code not in CAPACITY_ERRORS:
                    raise
                updated = defer_daily_gift(updated, gift_id, earned, failure.code)
                continue
            grants.append(grant)
        self.validate_receipt(updated, grants)
        return updated, grants

    def validate_receipt(self, state, grants, *, verify_current=True):
        if type(verify_current) is not bool:
            raise ValueError("Daily gift receipt validation requires a boolean")
        self._ready()
        validate_daily_gift_receipt(state, grants)
        if not verify_current or not grants:
            return
        indexed = {row["PresentID"]: row for row in self.presents.rows(state)}
        for grant in grants:
            campaign = self.catalog.by_id.get(grant["gift_id"])
            if campaign is None or daily_gift_definition(campaign) != grant["definition"]:
                raise ProtocolError("daily_gift_receipt_invalid", "Daily gift receipt differs from its current source", 503)
            expected = self.presents._prepare_reward_rows(self._rewards(campaign), 0)
            if len(expected) != len(grant["mail_ids"]):
                raise ProtocolError("daily_gift_receipt_invalid", "Daily gift receipt has an incorrect mail count", 503)
            for identifier, reward in zip(grant["mail_ids"], expected):
                if identifier in indexed and indexed[identifier] != {"PresentID": identifier, **reward}:
                    raise ProtocolError("daily_gift_receipt_invalid", "Daily gift mail differs from the recorded reward", 503)
