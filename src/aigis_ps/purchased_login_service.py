from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from .alxml import Column, Container
from .comeback_login_service import combine_event_login_notifications
from .comeback_login_state import validate_comeback_login_grants
from .errors import ProtocolError
from .event_login_service import CAPACITY_ERRORS, GiftBoxVersions, event_login_definition
from .event_login_state import validate_event_login_grants
from .login_reward_catalog import LoginReward
from .native_numbers import int32
from .possessions import MAX_ITEM_NAME_BYTES
from .presents import GIFT_BOX_DEPENDENCIES, MAX_PRESENTS, PRESENT_CAPACITY, PendingAction
from .purchased_login_rules import PurchasedLoginRules
from .purchased_login_state import (DAILY_LIMIT, add_purchased_login, begin_purchased_reward,
    complete_purchased_reward, defer_purchased_reward, initialize_purchased_logins, purchase_window,
    purchased_login_clock, purchased_login_delivered_day, purchased_login_identity, purchased_login_settled_day,
    purchased_login_time, read_purchased_logins, rebind_purchased_reward, validate_purchased_login_grants,
    validate_purchased_login_receipt, void_purchased_reward)


DESCRIPTION = "購入パックログインボーナス"
_HASH = re.compile(r"[a-f0-9]{64}\Z")
CAMPAIGN_UNAVAILABLE = "purchased_login_campaign_unavailable"
REWARD_MISSING = "purchased_login_reward_missing"
DEFINITION_CHANGED = "purchased_login_definition_changed"
DEFINITION_MISSING = "purchased_login_definition_missing"
RESOLVE_UNNEEDED = "purchased_login_resolve_unneeded"
RESOLVE_OPERATION = "purchased_login.resolve_changed"
PPF_FIELDS = (("RecordIndex", "I"), ("PackageID", "I"), ("PurchaseFlag", "I"),
              ("PurchaseStartDateTime", "D"), ("PurchaseEndDateTime", "D"))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def combine_login_notifications(public_grants, comeback_grants, purchased_grants):
    base = combine_event_login_notifications(public_grants, comeback_grants)
    previous_ids = validate_event_login_grants(public_grants) + validate_comeback_login_grants(comeback_grants)
    ids = validate_purchased_login_grants(purchased_grants)
    if set(previous_ids).intersection(ids):
        raise ProtocolError("login_notification_duplicate_mail", "Login notification sources reuse mail identities", 503)
    rows = list(zip(*(column.values for column in base.children)))
    rows.extend((grant["campaign_id"], grant["login_count"], grant["purchase_time"]) for grant in purchased_grants)
    if len({row[0] for row in rows}) != len(rows):
        raise ProtocolError("login_notification_duplicate_campaign", "One response cannot notify two instances of one campaign", 503)
    rows.sort(key=lambda row: row[0])
    return Container("EBNS", tuple(Column(name, kind, tuple(row[index] for row in rows))
        for index, (name, kind) in enumerate((("LoginBonusID", "I"), ("LoginCount", "I"), ("PurchaseTime", "S")))))


class PurchasedLoginService:
    def __init__(self, catalog, presents, rules=None):
        self.catalog, self.presents = catalog, presents
        self.rules = catalog.rules if rules is None else rules
        if not isinstance(self.rules, PurchasedLoginRules) or self.rules != catalog.rules:
            raise ValueError("Purchased login rules must match the sale catalog")
        self.content_version = self._content()
        self._ready()

    def content_version_for(self, dependencies):
        try:
            versions = {"catalog": self.catalog.content_version, "presents": self.presents.asset_sha256,
                        **{name: dependencies[name] for name in GIFT_BOX_DEPENDENCIES}}
            if (any(versions[key] is None for key in ("catalog", "presents"))
                    or any(value is not None and (type(value) is not str or not _HASH.fullmatch(value))
                           for value in versions.values())):
                raise ValueError("Purchased login sources require stable fingerprints")
            return _digest({"policy": "purchased_login_instances_v1", "dependencies": versions,
                "rules": self.rules.to_dict(), "templates": dict(self.presents.templates), "description": DESCRIPTION,
                "window": "next_jst04_day_clamp_source_duration_from_next", "missed_days": "no_backfill",
                "daily_limit": "one_actual_delivery_per_campaign", "disabled_sales": "honor_existing_qualifications"})
        except (TypeError, AttributeError, ValueError, KeyError) as error:
            raise ProtocolError("purchased_login_dependencies_invalid", "Purchased login dependencies are unavailable", 503) from error

    def replayable_content_versions(self):
        return frozenset(map(self.content_version_for, self.presents.gift_box_configurations()))

    def _content(self):
        return self.content_version_for(GiftBoxVersions(self.presents))

    def _ready(self):
        self.catalog._ready()
        self.presents._ready()
        if self.rules != self.catalog.rules or self._content() != self.content_version:
            raise ProtocolError("purchased_login_dependencies_changed", "Purchased login dependencies changed after setup", 503)

    def initialize(self, state):
        self._ready()
        return initialize_purchased_logins(state)

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "content_version": self.content_version,
                "rules": self.rules.to_dict(), "catalog": self.catalog.status(), "delivery": "mail",
                "sales_disabled": "Existing qualifications remain claimable", "native_tested": False}

    def validate_purchase_receipt(self, state, offer, request, purchase_id):
        self._ready()
        ledger = read_purchased_logins(state)
        if type(purchase_id) is not int or purchase_id <= 0 or type(request) is not str or not _HASH.fullmatch(request):
            raise ProtocolError("purchased_login_purchase_invalid", "Purchase proof identity is invalid", 503)
        record = ledger["purchases"].get(str(purchase_id))
        expected = {"campaign_id": offer.campaign_id, "identity": purchased_login_identity(offer.campaign_id),
                    "sku": offer.sku, "package_id": offer.package_id, "flag_id": offer.flag_id, "request": request}
        if record is None or any(record[key] != value for key, value in expected.items()):
            raise ProtocolError("purchased_login_purchase_invalid", "Purchase proof differs from its permanent qualification", 503)
        return {"purchase_id": purchase_id, **deepcopy(record)}

    def acquire(self, state, offer, request, now):
        updated = self.initialize(state)
        if (self.catalog.get(offer.sku) != offer or offer.definition_hash != purchased_login_identity(offer.campaign_id)
                or type(request) is not str or not _HASH.fullmatch(request)):
            raise ProtocolError("purchased_login_purchase_invalid", "Purchase requires a verified offer and request", 503)
        ledger = read_purchased_logins(updated)
        for key, record in ledger["purchases"].items():
            if record["request"] == request:
                return updated, self.validate_purchase_receipt(updated, offer, request, int(key))
        if not self.rules.enabled or offer not in self.catalog.available(now):
            raise ProtocolError("purchased_login_sale_unavailable", "This local daily reward package is unavailable", 409)
        day, timestamp = purchased_login_clock(now)
        if ledger["last_time"] is not None and timestamp < ledger["last_time"]:
            raise ProtocolError("purchased_login_clock_backwards", "Purchase clock precedes its watermark", 409)
        existing = [record for record in ledger["purchases"].values() if record["campaign_id"] == offer.campaign_id]
        if len(existing) >= offer.purchase_limit:
            raise ProtocolError("purchased_login_purchase_limit", "This daily reward package reached its purchase limit", 409)
        if any(record["pending"] is not None or day < record["end_day"] for record in existing):
            raise ProtocolError("purchased_login_purchase_active", "An existing daily reward qualification is active or pending", 409)
        campaign = offer.campaign
        source_start = campaign.start_date.isoformat() if campaign.start_date is not None else None
        source_end = campaign.end_date.isoformat() if campaign.end_date is not None else None
        start, end = purchase_window(timestamp, source_start, source_end, campaign.expiration)
        if start >= end:
            raise ProtocolError("purchased_login_window_empty", "Purchase leaves no eligible reward days", 409)
        entry = {"campaign_id": offer.campaign_id, "identity": offer.definition_hash, "sku": offer.sku,
            "package_id": offer.package_id, "flag_id": offer.flag_id, "request": request,
            "purchased_at": timestamp, "purchase_time": purchased_login_time(timestamp),
            "source_start": source_start, "source_end": source_end, "expiration": campaign.expiration,
            "start_day": start, "end_day": end,
            "reward_definitions": [event_login_definition(campaign, ordinal) for ordinal in range(1, campaign.max_ordinal+1)],
            "deliveries": {}, "pending": None}
        updated, purchase_id = add_purchased_login(updated, entry, now)
        return updated, self.validate_purchase_receipt(updated, offer, request, purchase_id)

    def ppf(self, state):
        self._ready()
        records = tuple(read_purchased_logins(state)["purchases"].values())
        owned = {record["campaign_id"] for record in records}
        selected = (set(self.rules.campaign_ids) if self.rules.campaign_ids is not None
                    else {offer.campaign_id for offer in self.catalog.offers}) if self.rules.enabled else set()
        rows = []
        for offer in self.catalog.offers:
            if offer.campaign_id not in selected | owned:
                continue
            purchases = [record for record in records if record["campaign_id"] == offer.campaign_id]
            rows.append({"RecordIndex": offer.flag_id, "PackageID": offer.flag_id, "PurchaseFlag": len(purchases),
                "PurchaseStartDateTime": 0.0, "PurchaseEndDateTime": 0.0})
        return Container("PPF", tuple(Column(name, kind, tuple(row[name] for row in rows)) for name, kind in PPF_FIELDS))

    def _rewards(self, campaign, ordinal):
        result = []
        for reward in campaign.rewards_for(ordinal):
            if reward.pause_period or reward.evolution_count or reward.item_param <= 0:
                raise ProtocolError("purchased_login_reward_unsupported", "Purchased login reward shape is unsupported", 501)
            if len(result) >= MAX_PRESENTS:
                raise ProtocolError(PRESENT_CAPACITY, "Purchased login rewards exceed mailbox capacity", 409)
            if reward.item_type == 22 and len(reward.item_id.encode("utf-8")) > MAX_ITEM_NAME_BYTES:
                # The mail writer refuses such new rows with 503, which 115 and 116 would not defer.
                raise ProtocolError("purchased_login_reward_unsupported",
                                    "Purchased login reward item does not fit the native cell", 501)
            if reward.item_type == 22 and self.presents.recovery_catalog.supports_item(reward.item_id):
                if reward.param1:
                    raise ProtocolError("purchased_login_reward_unsupported", "Recovery reward metadata is unsupported", 501)
                if reward.item_param > MAX_PRESENTS-len(result):
                    raise ProtocolError(PRESENT_CAPACITY, "Purchased login recovery rewards exceed capacity", 409)
                result.extend({"kind": "charisma_recovery", "item_id": reward.item_id, "description": DESCRIPTION}
                              for _ in range(reward.item_param))
            elif reward.item_type == 20:
                if reward.item_id or reward.param1 <= 0:
                    raise ProtocolError("purchased_login_reward_unsupported", "Selection reward fields are unsupported", 501)
                result.append({"kind": "selection_ticket", "config_id": reward.param1,
                               "count": reward.item_param, "description": DESCRIPTION})
            else:
                if reward.item_type == 1 and reward.item_param not in self.presents.templates:
                    raise ProtocolError("purchased_login_template_unsupported", "Purchased login unit template is unavailable", 501)
                try:
                    value = LoginReward(reward.item_type, reward.item_id, reward.item_param, reward.param1, reward.evolution_count)
                except (ValueError, TypeError) as error:
                    raise ProtocolError("purchased_login_reward_unsupported", "Purchased login reward metadata is unsupported", 501) from error
                result.append(value.to_present_reward(DESCRIPTION))
        if not result:
            raise ProtocolError(REWARD_MISSING, "Purchased login ordinal has no reward", 503)
        return result

    def _mail_candidate(self, before, candidate, groups, rewards):
        if (not isinstance(candidate, dict) or not isinstance(groups, list) or len(groups) != 1
                or not isinstance(groups[0], list) or not groups[0]):
            raise ProtocolError("purchased_login_delivery_invalid", "Purchased login mail groups are invalid", 503)
        additions = groups[0]
        rows = self.presents.rows(before)
        expected_rows = self.presents._prepare_reward_rows(rewards, len(rows))
        if [{key: value for key, value in row.items() if key != "PresentID"} for row in additions] != expected_rows:
            raise ProtocolError("purchased_login_delivery_invalid", "Purchased login mail differs from its source rewards", 503)
        last = before.get("last_present_id", 0)
        ids = [row["PresentID"] for row in additions]
        if ids != list(range(last+1, last+len(additions)+1)):
            raise ProtocolError("purchased_login_delivery_invalid", "Purchased login mail identities are not sequential", 503)
        expected = deepcopy(before)
        expected.update(presents_schema=1, last_present_id=last+len(additions))
        expected["tables"]["presents"] = rows+deepcopy(additions)
        if _digest(expected) != _digest(candidate):
            raise ProtocolError("purchased_login_delivery_invalid", "Purchased login mail changed unrelated state", 503)
        self.presents.rows(candidate)
        return ids

    def _mail(self, state, campaign, purchase_id, ordinal, now):
        rewards = self._rewards(campaign, ordinal)
        candidate, groups = self.presents.enqueue_groups(deepcopy(state), [rewards])
        ids = self._mail_candidate(state, candidate, groups, rewards)
        return complete_purchased_reward(candidate, purchase_id, ids, now)

    def _current(self, offer, ordinal):
        if offer is None or not offer.campaign.rewards_for(ordinal):
            return None
        return event_login_definition(offer.campaign, ordinal)

    def settle(self, state, now, validate=None):
        if validate is not None and not callable(validate):
            raise ValueError("Purchased login validation must be callable")
        updated = self.initialize(state)
        ledger = read_purchased_logins(updated)
        if not ledger["purchases"]:
            return updated, []
        day, timestamp = purchased_login_clock(now)
        if ledger["last_time"] is not None and timestamp < ledger["last_time"]:
            raise ProtocolError("purchased_login_clock_backwards", "Purchased login clock precedes its watermark", 409)
        grants = []
        for purchase_id in sorted(map(int, ledger["purchases"])):
            self._ready()
            current = read_purchased_logins(updated)
            record = current["purchases"][str(purchase_id)]
            if purchased_login_delivered_day(current, record["campaign_id"], day):
                continue
            pending = record["pending"]
            if pending is None:
                if (not record["start_day"] <= day < record["end_day"]
                        or len(record["deliveries"]) >= len(record["reward_definitions"])
                        or purchased_login_settled_day(record, day)):
                    continue
                updated = begin_purchased_reward(updated, purchase_id, now)
                pending = read_purchased_logins(updated)["purchases"][str(purchase_id)]["pending"]
            offer = self._offer(record)
            if offer is None:
                updated = defer_purchased_reward(updated, purchase_id, CAMPAIGN_UNAVAILABLE)
                continue
            ordinal = pending["ordinal"]
            if not offer.campaign.rewards_for(ordinal):
                updated = defer_purchased_reward(updated, purchase_id, REWARD_MISSING)
                continue
            if event_login_definition(offer.campaign, ordinal) != pending["definition"]:
                updated = defer_purchased_reward(updated, purchase_id, DEFINITION_CHANGED)
                continue
            try:
                candidate, grant = self._mail(updated, offer.campaign, purchase_id, ordinal, now)
                combined = sorted([*grants, grant], key=lambda value: (value["campaign_id"], value["purchase_id"]))
                validate_purchased_login_grants(combined)
                if validate is not None:
                    inspected = deepcopy(candidate)
                    validate(inspected, deepcopy(combined))
                    if _digest(inspected) != _digest(candidate):
                        raise ProtocolError("purchased_login_validation_invalid", "Purchased login validator changed candidate state", 503)
            except ProtocolError as failure:
                if failure.status != 501 and failure.code not in CAPACITY_ERRORS:
                    raise
                updated = defer_purchased_reward(updated, purchase_id, failure.code)
                continue
            updated, grants = candidate, combined
        self.validate_receipt(updated, grants)
        return updated, grants

    @staticmethod
    def _purchase(state, purchase_id):
        if not int32(purchase_id) or purchase_id < 1:
            raise ProtocolError("purchased_login_selection_invalid", "Purchases are chosen by a positive int32 purchase ID")
        return read_purchased_logins(state)["purchases"].get(str(purchase_id))

    def _offer(self, entry):
        return self.catalog.by_campaign_id.get(entry["campaign_id"])

    def changed_reward(self, state, purchase_id):
        record = self._purchase(state, purchase_id)
        pending = None if record is None else record["pending"]
        if pending is None or self._current(self._offer(record), pending["ordinal"]) == pending["definition"]:
            return None
        return pending

    def changed_schedule(self, state, purchase_id):
        record = self._purchase(state, purchase_id)
        if record is None:
            return ()
        offer = self._offer(record)
        return tuple(index for index, frozen in enumerate(record["reward_definitions"], 1)
                     if index > len(record["deliveries"]) and self._current(offer, index) != frozen)

    def resolve_changed(self, state, purchase_id, action, now):
        if action not in PendingAction:
            raise ProtocolError("purchased_login_action_invalid", "Changed purchased login rewards are redelivered or voided")
        updated = self.initialize(state)
        pending = self.changed_reward(updated, purchase_id)
        if pending is None:
            raise ProtocolError(RESOLVE_UNNEEDED, "This purchase has no pending reward with a changed definition", 409)
        if action == PendingAction.VOID:
            return void_purchased_reward(updated, purchase_id), []
        record = read_purchased_logins(updated)["purchases"][str(purchase_id)]
        offer, ordinal = self._offer(record), pending["ordinal"]
        if self._current(offer, ordinal) is None:
            raise ProtocolError(DEFINITION_MISSING, "The current catalog no longer defines this reward; void it", 409)
        # Purchases freeze their whole schedule, so redelivery also moves the ordinals still to come to the current one.
        schedule = [frozen if index <= len(record["deliveries"]) else self._current(offer, index) or frozen
                    for index, frozen in enumerate(record["reward_definitions"], 1)]
        updated = rebind_purchased_reward(updated, purchase_id, schedule)
        if purchased_login_delivered_day(read_purchased_logins(updated), record["campaign_id"],
                                         purchased_login_clock(now)[0]):
            return defer_purchased_reward(updated, purchase_id, DAILY_LIMIT), []
        try:
            updated, grant = self._mail(updated, offer.campaign, purchase_id, ordinal, now)
        except ProtocolError as failure:
            if failure.status != 501 and failure.code not in CAPACITY_ERRORS:
                raise
            return defer_purchased_reward(updated, purchase_id, failure.code), []
        self.validate_receipt(updated, [grant])
        return updated, [grant]

    @staticmethod
    def response(grants):
        return combine_login_notifications([], [], grants)

    def validate_receipt(self, state, grants, *, verify_current=True):
        if type(verify_current) is not bool:
            raise ValueError("Purchased login receipt validation requires a boolean")
        self._ready()
        validate_purchased_login_receipt(state, grants)
        if not verify_current or not grants:
            return
        indexed = {row["PresentID"]: row for row in self.presents.rows(state)}
        for grant in grants:
            offer = self._offer(grant)
            if offer is None or event_login_definition(offer.campaign, grant["login_count"]) != grant["definition"]:
                raise ProtocolError("purchased_login_receipt_invalid", "Purchased login receipt differs from its current source", 503)
            rewards = self.presents._prepare_reward_rows(self._rewards(offer.campaign, grant["login_count"]), 0)
            if len(rewards) != len(grant["mail_ids"]):
                raise ProtocolError("purchased_login_receipt_invalid", "Purchased login receipt has an incorrect reward count", 503)
            for identifier, reward in zip(grant["mail_ids"], rewards):
                if identifier in indexed and indexed[identifier] != {"PresentID": identifier, **reward}:
                    raise ProtocolError("purchased_login_receipt_invalid", "Purchased login mail differs from its source reward", 503)
