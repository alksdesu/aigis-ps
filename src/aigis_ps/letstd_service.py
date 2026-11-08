from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import NamedTuple

from .alxml import Column, Container
from .errors import ProtocolError
from .event_login_service import CAPACITY_ERRORS, GiftBoxVersions
from .letstd_catalog import FREE_CRYSTAL, GOLD, ITEM, UNIT
from .letstd_rules import GROUPS, PARTNERS, LetsTDRules
from .letstd_state import (PENDING, SOURCES, arm_group, complete_rows, defer_rows, due_rows, group_record,
    initialize_letstd, letstd_clock, letstd_time, read_letstd, rearm_group, schedule, validate_letstd_grants,
    validate_letstd_receipt)
from .presents import ITEM_CATALOG, MAX_PRESENTS, PRESENT_CAPACITY


LOCAL_LABEL = "【ローカル模擬】"
POLICY_VERSION = "local-letstd-simulation-1"
ITEM_CHANNEL = "present_mail_type22"
RESPONSE_NAME = "LetsTDRewardParam"
SERVICE_COLUMN = "ServiceId"
VERIFICATION = "No real cross-game account, partner progress or purchase has been verified"
DEFINITION_ERRORS = frozenset(("present_reward_invalid", "present_template_unsupported"))
SETTLE_CAPACITY_ERRORS = CAPACITY_ERRORS | {"letstd_journal_capacity"}
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def mail_description(row):
    return LOCAL_LABEL + "".join(line.strip() for line in row.title.splitlines())


class _Candidate(NamedTuple):
    key: tuple
    rewards: list
    mails: int


class LetsTDService:
    def __init__(self, catalog, presents, rules=None):
        self.catalog, self.presents = catalog, presents
        self.rules = LetsTDRules() if rules is None else rules
        if not isinstance(self.rules, LetsTDRules):
            raise ValueError("LetsTD requires verified local rules")
        self.dependency_version = self._dependencies()
        self.content_version = self._content(self.dependency_version)
        self._ready()

    def dependency_version_for(self, dependencies):
        try:
            versions = {"catalog": self.catalog.content_version, "presents": self.presents.asset_sha256,
                        ITEM_CATALOG: dependencies[ITEM_CATALOG], "item_tickets": dependencies["item_tickets"]}
            if (any(versions[name] is None for name in ("catalog", "presents", ITEM_CATALOG))
                    or any(value is not None and (type(value) is not str or not _HASH.fullmatch(value))
                           for value in versions.values())):
                raise ValueError("LetsTD dependencies require stable fingerprints")
            return _digest({"policy": POLICY_VERSION, "dependencies": versions, "templates": dict(self.presents.templates),
                            "label": LOCAL_LABEL, "item_channel": ITEM_CHANNEL})
        except (ValueError, TypeError, AttributeError, KeyError) as error:
            raise ProtocolError("letstd_dependencies_invalid", "LetsTD dependencies are unavailable", 503) from error

    def content_version_for(self, dependencies):
        return self._content(self.dependency_version_for(dependencies))

    def replayable_content_versions(self):
        # A gift box without the item catalog never had a LetsTD service to commit a receipt.
        return frozenset(self.content_version_for(configuration)
                         for configuration in self.presents.gift_box_configurations()
                         if configuration[ITEM_CATALOG] is not None)

    def _dependencies(self):
        return self.dependency_version_for(GiftBoxVersions(self.presents))

    def _content(self, dependency_version):
        return _digest({"policy": POLICY_VERSION, "dependencies": dependency_version,
            "rules": self.rules.to_dict(), "eligibility": "each_reward_id_once_after_its_group_is_armed",
            "pending": "retry_on_capacity_or_dependency_change", "disabled_group": "paused_including_pending",
            "time": "jst_start_inclusive_end_checked_when_due", "response": "letstd_reward_param_with_native_popup"})

    def _ready(self):
        self.catalog._ready()
        self.presents._ready()
        if (self._dependencies() != self.dependency_version
                or self._content(self.dependency_version) != self.content_version):
            raise ProtocolError("letstd_dependencies_changed", "LetsTD dependencies changed after setup", 503)

    def initialize(self, state):
        self._ready()
        return initialize_letstd(state)

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        catalog = self.catalog.status()
        return {"ready": error is None, "error": error, "content_version": self.content_version,
                "dependency_version": self.dependency_version, "simulated": True, "native_tested": False,
                "verification": VERIFICATION, "native_popup": self.rules.native_popup, "rules": self.rules.to_dict(),
                "groups": {partner: {name: {"enabled": self.rules.enabled(partner, group),
                    **catalog["groups"][partner][name]} for group, name in GROUPS.items()} for partner in PARTNERS},
                "catalog": catalog, "label": LOCAL_LABEL, "label_configurable": False, "delivery": "mail",
                "item_channel": ITEM_CHANNEL,
                "local_policy": "Each RewardId is delivered at most once after its group switch arms it",
                "pending_policy": "Pending rows retry after capacity failures or dependency changes",
                "disabled_policy": "A disabled group is paused, including its pending rows",
                "unavailable": ["TowerDefenceCampaignRewardParam", "LetsTDServiceIds"]}

    def rewards(self, row):
        description = mail_description(row)
        if row.count <= 0:
            raise ProtocolError("letstd_reward_unsupported", "LetsTD reward quantity must be positive", 501)
        if row.item_type == ITEM:
            return self._item_rewards(row.item_id, row.count, description)
        if row.item_type == UNIT:
            template = self.presents.initial_unit_template(row.card_id)
            return [{"kind": "unit", "template_id": template, "count": row.count, "description": description}]
        if row.item_type in (FREE_CRYSTAL, GOLD):
            kind = "free_crystal" if row.item_type == FREE_CRYSTAL else "gold"
            return [{"kind": kind, "amount": row.count, "description": description}]
        raise ProtocolError("letstd_reward_unsupported", "LetsTD reward type is unsupported", 501)

    @staticmethod
    def _item_rewards(item_id, amount, description):
        # Sole ItemType 1 channel choice: native Type22 present mail, summon tickets opened one per mail.
        return [{"kind": "item", "item_id": item_id, "amount": amount, "description": description}]

    def _specs(self, partner, group):
        return [{"reward_id": row.reward_id, "definition": row.definition, "start_at": letstd_clock(row.start),
                 "end_at": None if row.end is None else letstd_clock(row.end)}
                for row in self.catalog.rows_for(partner, group)]

    def rearm(self, state, partner, group, now):
        self._ready()
        return rearm_group(state, partner, group, self._specs(partner, group), self.rules.content_version,
                           self.catalog.resource_sha256, now)

    def _retry(self, record):
        return (record["reason"] is None or record["reason"] in SETTLE_CAPACITY_ERRORS
                or record["dependency_version"] != self.dependency_version)

    def _candidates(self, state, enabled):
        ledger = read_letstd(state)
        candidates, deferrals = [], []
        for partner, group in enabled:
            records = group_record(ledger, partner, group)["rows"]
            for reward_id in sorted(int(key) for key in records):
                record = records[str(reward_id)]
                if record["status"] != PENDING or not self._retry(record):
                    continue
                row = self.catalog.get(partner, group, reward_id)
                if row is None:
                    deferrals.append((partner, group, reward_id, "letstd_source_unavailable"))
                    continue
                if row.definition != record["definition"]:
                    deferrals.append((partner, group, reward_id, "letstd_definition_changed"))
                    continue
                try:
                    rewards = self.rewards(row)
                    mails = len(self.presents.preview(rewards, 0))
                except ProtocolError as failure:
                    if (failure.status != 501 and failure.code not in DEFINITION_ERRORS
                            and failure.code not in SETTLE_CAPACITY_ERRORS):
                        raise
                    deferrals.append((partner, group, reward_id, failure.code))
                    continue
                candidates.append(_Candidate((partner, group, reward_id), rewards, mails))
        return candidates, deferrals

    def _mail_candidate(self, before, candidate, groups, reward_groups):
        if (not isinstance(candidate, dict) or not isinstance(groups, list) or len(groups) != len(reward_groups)
                or any(not isinstance(group, list) or not group or not all(isinstance(row, dict) for row in group)
                       for group in groups)):
            raise ProtocolError("letstd_delivery_invalid", "LetsTD mail groups are invalid", 503)
        rows = self.presents.rows(before)
        count = len(rows)
        for additions, rewards in zip(groups, reward_groups):
            expected = self.presents.preview(rewards, count)
            if [{key: value for key, value in row.items() if key != "PresentID"} for row in additions] != expected:
                raise ProtocolError("letstd_delivery_invalid", "LetsTD mail differs from its source rewards", 503)
            count += len(additions)
        additions = [row for group in groups for row in group]
        last = before.get("last_present_id", 0)
        if [row.get("PresentID") for row in additions] != list(range(last + 1, last + len(additions) + 1)):
            raise ProtocolError("letstd_delivery_invalid", "LetsTD mail identities are not sequential", 503)
        expected = deepcopy(before)
        expected.update(presents_schema=1, last_present_id=last + len(additions))
        expected["tables"]["presents"] = rows + deepcopy(additions)
        if _digest(expected) != _digest(candidate):
            raise ProtocolError("letstd_delivery_invalid", "LetsTD mail changed unrelated state", 503)
        self.presents.rows(candidate)
        return [[row["PresentID"] for row in group] for group in groups]

    def _commit(self, state, candidates, previous, now, source, validate):
        self._ready()
        reward_groups = [item.rewards for item in candidates]
        candidate, groups = self.presents.enqueue_groups(deepcopy(state), reward_groups)
        mail_groups = self._mail_candidate(state, candidate, groups, reward_groups)
        candidate, grants = complete_rows(candidate, [(*item.key, ids) for item, ids in zip(candidates, mail_groups)],
                                          now, source, self.rules.native_popup)
        combined = [*previous, *grants]
        validate_letstd_grants(combined)
        if validate is not None:
            inspected = deepcopy(candidate)
            validate(inspected, deepcopy(combined))
            if _digest(inspected) != _digest(candidate):
                raise ProtocolError("letstd_validation_invalid", "LetsTD validator changed candidate state", 503)
        return candidate, combined

    def _deliver(self, state, candidates, now, source, validate):
        if not candidates:
            return state, []
        count = len(self.presents.rows(state))
        if len(candidates) > 1 and count + sum(item.mails for item in candidates) <= MAX_PRESENTS:
            try:
                return self._commit(state, candidates, [], now, source, validate)
            except ProtocolError as failure:
                if failure.status != 501 and failure.code not in SETTLE_CAPACITY_ERRORS:
                    raise
        updated, grants = state, []
        for candidate in candidates:
            try:
                # Rows that cannot fit fail exactly as enqueue would, without rebuilding the mailbox.
                if count + candidate.mails > MAX_PRESENTS:
                    raise ProtocolError(PRESENT_CAPACITY, "LetsTD rewards exceed the local mailbox capacity", 409)
                updated, grants = self._commit(updated, [candidate], grants, now, source, validate)
                count += candidate.mails
            except ProtocolError as failure:
                if failure.status != 501 and failure.code not in SETTLE_CAPACITY_ERRORS:
                    raise
                updated = defer_rows(updated, [(*candidate.key, failure.code)], self.dependency_version)
        return updated, grants

    def settle(self, state, now, *, source, validate=None):
        if validate is not None and not callable(validate):
            raise ValueError("LetsTD validation must be callable")
        if type(source) is not int or source not in SOURCES:
            raise ValueError("LetsTD settles only within login reward operations")
        updated = self.initialize(state)
        enabled = self.rules.enabled_groups
        if not enabled:
            return updated, []
        ledger = read_letstd(updated)
        timestamp = letstd_time(ledger, now)
        for partner, group in enabled:
            record = group_record(ledger, partner, group)
            if record is None:
                updated = arm_group(updated, partner, group, self._specs(partner, group),
                                    self.rules.content_version, self.catalog.resource_sha256, now)
            elif due_rows(record, timestamp):
                updated = schedule(updated, partner, group, now)
        candidates, deferrals = self._candidates(updated, enabled)
        if deferrals:
            updated = defer_rows(updated, deferrals, self.dependency_version)
        updated, grants = self._deliver(updated, candidates, now, source, validate)
        self.validate_receipt(updated, grants)
        return updated, grants

    @staticmethod
    def response(grants):
        validate_letstd_grants(grants)
        shown = [grant for grant in grants if grant["popup"]]
        if not shown:
            return None
        partners = tuple(partner for partner in PARTNERS if any(grant["partner"] == partner for grant in shown))
        columns = [Column(SERVICE_COLUMN, "S", partners)]
        for group, name in GROUPS.items():
            columns.append(Column(name, "S", tuple(",".join(str(grant["reward_id"]) for grant in shown
                if (grant["partner"], grant["group"]) == (partner, group)) for partner in partners)))
        return Container(RESPONSE_NAME, tuple(columns))

    def validate_receipt(self, state, grants, *, verify_current=True):
        if type(verify_current) is not bool:
            raise ValueError("LetsTD receipt validation requires a boolean policy")
        self._ready()
        validate_letstd_receipt(state, grants)
        if not verify_current or not grants:
            return
        indexed = {row["PresentID"]: row for row in self.presents.rows(state)}
        for grant in grants:
            row = self.catalog.get(grant["partner"], grant["group"], grant["reward_id"])
            if row is None or row.definition != grant["definition"]:
                raise ProtocolError("letstd_receipt_invalid", "LetsTD receipt differs from its current source", 503)
            expected = self.presents.preview(self.rewards(row), 0)
            if len(expected) != len(grant["mail_ids"]):
                raise ProtocolError("letstd_receipt_invalid", "LetsTD receipt has an incorrect mail count", 503)
            for identifier, mail in zip(grant["mail_ids"], expected):
                if identifier in indexed and indexed[identifier] != {"PresentID": identifier, **mail}:
                    raise ProtocolError("letstd_receipt_invalid", "LetsTD mail differs from the recorded reward", 503)
