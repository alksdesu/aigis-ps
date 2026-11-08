from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
import re

from .alxml import Column, Container
from .comeback_login_rules import ComebackLoginRules
from .comeback_login_state import (DAILY_LIMIT, DATE_FIELD, DAYS_FIELD, LEDGER_KEY, begin_comeback_reward,
    begin_comeback_round, comeback_clock, complete_comeback_reward, defer_comeback_reward, delivered_comeback_day,
    initialize_comeback_logins, observe_comeback_login, read_comeback_logins, rebind_comeback_reward,
    settled_comeback_day, validate_comeback_login_grants, validate_comeback_login_receipt, void_comeback_reward)
from .errors import ProtocolError
from .event_login_service import CAPACITY_ERRORS, GiftBoxVersions
from .event_login_state import validate_event_login_grants
from .native_numbers import int32
from .possessions import MAX_ITEM_NAME_BYTES
from .present_items import LEGACY_ITEMS
from .presents import GIFT_BOX_DEPENDENCIES, MAX_PRESENTS, PRESENT_CAPACITY, PendingAction


DESCRIPTION = "カムバックログインボーナス"
_HASH = re.compile(r"[a-f0-9]{64}\Z")
BAND_UNAVAILABLE = "comeback_login_band_unavailable"
REWARD_MISSING = "comeback_login_reward_missing"
DEFINITION_CHANGED = "comeback_login_definition_changed"
DEFINITION_MISSING = "comeback_login_definition_missing"
RESOLVE_UNNEEDED = "comeback_login_resolve_unneeded"
RESOLVE_OPERATION = "comeback_login.resolve_changed"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def comeback_reward_definition(band, ordinal):
    rewards = band.rewards_for(ordinal)
    if not rewards:
        raise ProtocolError(REWARD_MISSING, "Comeback reward ordinal is absent", 503)
    fields = ("ordinal", "config_id", "item_type", "item_id", "item_param", "param1", "evolution_count", "pause_period")
    return _digest({"campaign_id": band.campaign_id, "pause_period": band.pause_period,
                    "rewards": [{key: getattr(reward, key) for key in fields} for reward in rewards]})


def combine_event_login_notifications(public_grants, comeback_grants):
    public_ids = validate_event_login_grants(public_grants)
    comeback_ids = validate_comeback_login_grants(comeback_grants)
    if set(public_ids).intersection(comeback_ids):
        raise ProtocolError("login_notification_duplicate_mail", "Login notification sources reuse mail identities", 503)
    grants = [*public_grants, *(grant for grant in comeback_grants if grant["notify"])]
    if len({grant["campaign_id"] for grant in grants}) != len(grants):
        raise ProtocolError("login_notification_duplicate_campaign", "One response cannot notify two rounds of one campaign", 503)
    grants.sort(key=lambda grant: grant["campaign_id"])
    return Container("EBNS", (Column("LoginBonusID", "I", tuple(grant["campaign_id"] for grant in grants)),
        Column("LoginCount", "I", tuple(grant["login_count"] for grant in grants)),
        Column("PurchaseTime", "S", tuple(grant["purchase_time"] for grant in grants))))


class ComebackLoginService:
    def __init__(self, catalog, presents, rules=None):
        self.catalog, self.presents = catalog, presents
        self.rules = ComebackLoginRules() if rules is None else rules
        if not isinstance(self.rules, ComebackLoginRules):
            raise ValueError("Comeback login requires verified rules")
        self.content_version = self._content()
        self._ready()

    def content_version_for(self, dependencies):
        try:
            versions = {"catalog": self.catalog.content_version, "presents": self.presents.asset_sha256,
                        **{name: dependencies[name] for name in GIFT_BOX_DEPENDENCIES}}
            if (any(versions[name] is None for name in ("catalog", "presents"))
                    or any(value is not None and (type(value) is not str or not _HASH.fullmatch(value))
                           for value in versions.values())):
                raise ValueError("Comeback reward sources require stable fingerprints")
            return _digest({"policy": "comeback_rounds_daily_v1", "dependencies": versions,
                "rules": self.rules.to_dict(), "templates": dict(self.presents.templates), "description": DESCRIPTION,
                "gap": "jst04_date_difference", "initial": "observe_only", "overlap": "keep_active_round",
                "daily_limit": "one_actual_delivery_per_campaign", "old_pending": "mail_without_notification",
                "expiration_anchor": "civil_return_date_plus_expiration",
                "disabled_observation": "maintain_existing_baseline_without_new_round"})
        except (TypeError, AttributeError, ValueError, KeyError) as error:
            raise ProtocolError("comeback_login_dependencies_invalid", "Comeback reward dependencies are unavailable", 503) from error

    def replayable_content_versions(self):
        return frozenset(map(self.content_version_for, self.presents.gift_box_configurations()))

    def _content(self):
        return self.content_version_for(GiftBoxVersions(self.presents))

    def _ready(self):
        self.catalog._ready()
        self.presents._ready()
        if self._content() != self.content_version:
            raise ProtocolError("comeback_login_dependencies_changed", "Comeback reward dependencies changed after setup", 503)

    def initialize(self, state):
        self._ready()
        return initialize_comeback_logins(state)

    def pgp2_pairs(self, state):
        self.initialize(state)
        row = state["tables"]["game_param2"][0]
        return ((DATE_FIELD, row[DATE_FIELD]), (DAYS_FIELD, str(row[DAYS_FIELD])))

    def observe(self, state, now):
        updated = self.initialize(state)
        if not self.rules.enabled:
            return observe_comeback_login(updated, now)[0] if LEDGER_KEY in updated else updated
        updated, gap = observe_comeback_login(updated, now)
        if gap is None or gap < self.rules.min_absence_days:
            return updated
        ledger = read_comeback_logins(updated)
        day = ledger["last_seen_day"]
        current = ledger["current_round_id"]
        if current is not None and day < ledger["rounds"][str(current)]["end_day"]:
            return updated
        if not self.catalog.is_available(date.fromisoformat(day)):
            return updated
        band = self.catalog.choose(gap)
        definitions = [comeback_reward_definition(band, ordinal) for ordinal in range(1, band.max_ordinal + 1)]
        return begin_comeback_round(updated, now, campaign_id=self.catalog.campaign_id, pause_period=band.pause_period,
            absence_days=gap, expiration=self.catalog.expiration, reward_definitions=definitions)

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "content_version": self.content_version,
                "rules": self.rules.to_dict(), "catalog": self.catalog.status(),
                "eligibility": "Observed JST04 day gap; first observation only establishes a baseline",
                "round_policy": "Keep an unexpired current round; freeze source duration and reward identities",
                "expiration_anchor": "civil_return_date_plus_expiration",
                "disabled_observation": "Maintain existing presence without creating a baseline or round",
                "delivery": "One ordinal per campaign per day across all rounds",
                "legacy": "Preserve native date and absence fields without granting historical rewards",
                "native_tested": False}

    def _rewards(self, band, ordinal):
        result = []
        for reward in band.rewards_for(ordinal):
            kind, amount, item, parameter = reward.item_type, reward.item_param, reward.item_id, reward.param1
            if (reward.evolution_count or reward.pause_period != band.pause_period or amount <= 0
                    or kind not in (0, 1, 2, 16, 20, 22, *LEGACY_ITEMS) or (kind == 22) != bool(item)
                    or kind not in (16, 20) and parameter or kind in (16, 20) and parameter <= 0):
                raise ProtocolError("comeback_login_reward_unsupported", "Comeback reward shape is unsupported", 501)
            if len(result) >= MAX_PRESENTS:
                raise ProtocolError(PRESENT_CAPACITY, "Comeback rewards exceed mailbox capacity", 409)
            if kind == 0:
                values = {"kind": "free_crystal", "amount": amount}
            elif kind == 1:
                if amount not in self.presents.templates:
                    raise ProtocolError("comeback_login_template_unsupported", "Comeback unit template is unavailable", 501)
                values = {"kind": "unit", "template_id": amount, "count": 1}
            elif kind == 2:
                values = {"kind": "gold", "amount": amount}
            elif kind in (16, 20):
                values = ({"kind": "summon_ticket", "pool_id": parameter, "count": amount} if kind == 16 else
                          {"kind": "selection_ticket", "config_id": parameter, "count": amount})
            elif kind == 22:
                if len(item.encode("utf-8")) > MAX_ITEM_NAME_BYTES:
                    # The mail writer refuses such new rows with 503, which 115 and 116 would not defer.
                    raise ProtocolError("comeback_login_reward_unsupported", "Comeback reward item does not fit the native cell", 501)
                recovery = getattr(self.presents, "recovery_catalog", None)
                if recovery is not None and recovery.supports_item(item):
                    if amount > MAX_PRESENTS - len(result):
                        raise ProtocolError(PRESENT_CAPACITY, "Comeback recovery rewards exceed mailbox capacity", 409)
                    result.extend({"kind": "charisma_recovery", "item_id": item, "description": DESCRIPTION}
                                  for _ in range(amount))
                    continue
                values = {"kind": "item", "item_id": item, "amount": amount}
            else:
                values = {"kind": "legacy_item", "present_type": kind, "amount": amount}
            result.append({**values, "description": DESCRIPTION})
        if not result:
            raise ProtocolError(REWARD_MISSING, "Comeback reward ordinal has no rewards", 503)
        return result

    def _mail_candidate(self, before, candidate, groups, rewards):
        if (not isinstance(candidate, dict) or not isinstance(groups, list) or len(groups) != 1
                or not isinstance(groups[0], list) or not groups[0]):
            raise ProtocolError("comeback_login_delivery_invalid", "Comeback mail provider returned invalid groups", 503)
        additions = groups[0]
        rows = self.presents.rows(before)
        expected_rows = self.presents._prepare_reward_rows(rewards, len(rows))
        if [{key: value for key, value in row.items() if key != "PresentID"} for row in additions] != expected_rows:
            raise ProtocolError("comeback_login_delivery_invalid", "Comeback mail differs from its reward definitions", 503)
        last = before.get("last_present_id", 0)
        ids = [row["PresentID"] for row in additions]
        if ids != list(range(last + 1, last + len(additions) + 1)):
            raise ProtocolError("comeback_login_delivery_invalid", "Comeback mail identities are not sequential", 503)
        expected = deepcopy(before)
        expected.update(presents_schema=1, last_present_id=last + len(additions))
        expected["tables"]["presents"] = rows + deepcopy(additions)
        if _digest(expected) != _digest(candidate):
            raise ProtocolError("comeback_login_delivery_invalid", "Comeback mail changed unrelated state", 503)
        self.presents.rows(candidate)
        return ids

    def _mail(self, state, band, round_id, ordinal, now):
        rewards = self._rewards(band, ordinal)
        candidate, groups = self.presents.enqueue_groups(deepcopy(state), [rewards])
        ids = self._mail_candidate(state, candidate, groups, rewards)
        return complete_comeback_reward(candidate, round_id, ids, now)

    def _band(self, record):
        if record["campaign_id"] != self.catalog.campaign_id or record["pause_period"] not in self.catalog.bands:
            return None
        return self.catalog.require_band(record["pause_period"])

    def _current(self, band, ordinal):
        return None if band is None or not band.rewards_for(ordinal) else comeback_reward_definition(band, ordinal)

    def settle(self, state, now, validate=None):
        if validate is not None and not callable(validate):
            raise ValueError("Comeback reward validation must be callable")
        updated = self.initialize(state)
        ledger = read_comeback_logins(updated)
        if not self.rules.enabled or ledger is None:
            return updated, []
        day, timestamp = comeback_clock(now)
        if timestamp < ledger["last_time"]:
            raise ProtocolError("comeback_login_clock_backwards", "Comeback clock precedes saved progress", 409)
        grants = []
        for round_id in sorted(map(int, ledger["rounds"])):
            self._ready()
            current = read_comeback_logins(updated)
            record = current["rounds"][str(round_id)]
            if delivered_comeback_day(current, record["campaign_id"], day):
                continue
            pending = record["pending"]
            if pending is None:
                if (round_id != current["current_round_id"] or not record["start_day"] <= day < record["end_day"]
                        or len(record["deliveries"]) >= len(record["reward_definitions"])
                        or settled_comeback_day(record, day)):
                    continue
                updated = begin_comeback_reward(updated, round_id, now)
                pending = read_comeback_logins(updated)["rounds"][str(round_id)]["pending"]
            band = self._band(record)
            if band is None:
                updated = defer_comeback_reward(updated, round_id, BAND_UNAVAILABLE)
                continue
            ordinal = pending["ordinal"]
            if not band.rewards_for(ordinal):
                updated = defer_comeback_reward(updated, round_id, REWARD_MISSING)
                continue
            if comeback_reward_definition(band, ordinal) != pending["definition"]:
                updated = defer_comeback_reward(updated, round_id, DEFINITION_CHANGED)
                continue
            try:
                candidate, grant = self._mail(updated, band, round_id, ordinal, now)
                combined = sorted([*grants, grant], key=lambda value: (value["campaign_id"], value["round_id"]))
                validate_comeback_login_grants(combined)
                if validate is not None:
                    inspected = deepcopy(candidate)
                    validate(inspected, deepcopy(combined))
                    if _digest(inspected) != _digest(candidate):
                        raise ProtocolError("comeback_login_validation_invalid", "Comeback validator changed candidate state", 503)
            except ProtocolError as failure:
                if failure.status != 501 and failure.code not in CAPACITY_ERRORS:
                    raise
                updated = defer_comeback_reward(updated, round_id, failure.code)
                continue
            updated, grants = candidate, combined
        self.validate_receipt(updated, grants)
        return updated, grants

    @staticmethod
    def _round(state, round_id):
        if not int32(round_id) or round_id < 1:
            raise ProtocolError("comeback_login_selection_invalid", "Comeback rounds are chosen by a positive int32 round ID")
        ledger = read_comeback_logins(state)
        return None if ledger is None else ledger["rounds"].get(str(round_id))

    def changed_reward(self, state, round_id):
        record = self._round(state, round_id)
        pending = None if record is None else record["pending"]
        if pending is None or self._current(self._band(record), pending["ordinal"]) == pending["definition"]:
            return None
        return pending

    def changed_schedule(self, state, round_id):
        record = self._round(state, round_id)
        if record is None:
            return ()
        band = self._band(record)
        return tuple(index for index, frozen in enumerate(record["reward_definitions"], 1)
                     if index > len(record["deliveries"]) and self._current(band, index) != frozen)

    def resolve_changed(self, state, round_id, action, now):
        if action not in PendingAction:
            raise ProtocolError("comeback_login_action_invalid", "Changed comeback rewards are redelivered or voided")
        updated = self.initialize(state)
        pending = self.changed_reward(updated, round_id)
        if pending is None:
            raise ProtocolError(RESOLVE_UNNEEDED, "This round has no pending reward with a changed definition", 409)
        if action == PendingAction.VOID:
            return void_comeback_reward(updated, round_id), []
        record = read_comeback_logins(updated)["rounds"][str(round_id)]
        band, ordinal = self._band(record), pending["ordinal"]
        if self._current(band, ordinal) is None:
            raise ProtocolError(DEFINITION_MISSING, "The current catalog no longer defines this reward; void it", 409)
        # Rounds freeze their whole schedule, so redelivery also moves the ordinals still to come to the current one.
        schedule = [frozen if index <= len(record["deliveries"]) else self._current(band, index) or frozen
                    for index, frozen in enumerate(record["reward_definitions"], 1)]
        updated = rebind_comeback_reward(updated, round_id, schedule)
        if delivered_comeback_day(read_comeback_logins(updated), record["campaign_id"], comeback_clock(now)[0]):
            return defer_comeback_reward(updated, round_id, DAILY_LIMIT), []
        try:
            updated, grant = self._mail(updated, band, round_id, ordinal, now)
        except ProtocolError as failure:
            if failure.status != 501 and failure.code not in CAPACITY_ERRORS:
                raise
            return defer_comeback_reward(updated, round_id, failure.code), []
        self.validate_receipt(updated, [grant])
        return updated, [grant]

    @staticmethod
    def response(grants):
        return combine_event_login_notifications([], grants)

    def validate_receipt(self, state, grants, *, verify_current=True):
        if type(verify_current) is not bool:
            raise ValueError("Comeback receipt validation requires a boolean policy")
        self._ready()
        validate_comeback_login_receipt(state, grants)
        if not verify_current or not grants:
            return
        ledger = read_comeback_logins(state)
        indexed = {row["PresentID"]: row for row in self.presents.rows(state)}
        for grant in grants:
            record = ledger["rounds"][str(grant["round_id"])]
            if (record["campaign_id"] != self.catalog.campaign_id
                    or grant["notify"] != (grant["round_id"] == ledger["current_round_id"])):
                raise ProtocolError("comeback_login_receipt_invalid", "Comeback campaign changed after delivery", 503)
            band = self.catalog.require_band(record["pause_period"])
            if comeback_reward_definition(band, grant["login_count"]) != grant["definition"]:
                raise ProtocolError("comeback_login_receipt_invalid", "Comeback receipt differs from its current reward", 503)
            rewards = self.presents._prepare_reward_rows(self._rewards(band, grant["login_count"]), 0)
            if len(rewards) != len(grant["mail_ids"]):
                raise ProtocolError("comeback_login_receipt_invalid", "Comeback receipt has an incorrect reward count", 503)
            for identifier, reward in zip(grant["mail_ids"], rewards):
                if identifier in indexed and indexed[identifier] != {"PresentID": identifier, **reward}:
                    raise ProtocolError("comeback_login_receipt_invalid", "Comeback mail differs from its reward", 503)
