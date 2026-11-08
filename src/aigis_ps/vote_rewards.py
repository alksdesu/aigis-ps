from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from functools import cached_property
import hashlib
import json
import re
from types import MappingProxyType

from .alxml import Column, Container
from .errors import ProtocolError
from .native_numbers import INT32_MAX
from .presents import MAX_PRESENTS
from .vote_expiry import converted_assault_rights
from .vote_state import initialize_vote_state, read_vote_history, vote_key


MAX_ITEMS = 4096
MAX_DELIVERIES = 65535
MAX_MAIL_IDS = 65535
MAX_ASSAULT = 999
MAX_ARCHIVED = MAX_ITEMS
DESCRIPTION = "人気闘兵投票報酬"
ARCHIVED_KEY = "archived"
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_POSITIVE = re.compile(r"[1-9][0-9]{0,9}\Z")
AWARD_FIELDS = frozenset(("GroupID", "Count", "Repeat", "ItemID", "GiftType", "Amount", "SummonID",
                         "CardID", "UnitLevel", "SkillLevel", "ClassChange", "Likable", "Cost"))
_ENTRY_FIELDS = frozenset(("group_id", "event_id", "definition", "baseline_count", "count", "deliveries"))
_INSTANCE_FIELDS = frozenset(("event_id", "definition", "earned_votes", "baseline_count", "count", "deliveries"))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _integer(value, minimum=0, maximum=INT32_MAX):
    return type(value) is int and minimum <= value <= maximum


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _positive_key(value):
    return type(value) is str and _POSITIVE.fullmatch(value) is not None and int(value) <= INT32_MAX


def _invalid(message="Vote reward ledger is invalid"):
    raise ProtocolError("vote_rewards_state_invalid", message, 503)


def _totals(state):
    result = {}
    for row in read_vote_history(state):
        entry = result.setdefault(row["itemId"], {"group_id": row["groupId"], "count": 0})
        entry["count"] += row["count"]
        if entry["count"] > INT32_MAX:
            _invalid("Vote group total exceeds the native integer range")
    if len(result) > MAX_ITEMS:
        _invalid("Vote reward item history exceeds its capacity")
    return result


def group_total(state, item_id):
    try:
        vote_key(item_id, 1)
    except ProtocolError as error:
        raise ProtocolError("vote_reward_item_invalid", "Vote totals require a canonical item identity") from error
    return _totals(state).get(item_id, {}).get("count", 0)


def _award_valid(row, group_id, point):
    if (not isinstance(row, dict) or set(row) != AWARD_FIELDS
            or any(not _integer(row[key]) for key in AWARD_FIELDS - {"ItemID"})
            or row["GroupID"] != group_id or row["Count"] != point
            or row["Repeat"] != 0 or row["GiftType"] != 0 or row["SummonID"] != 0
            or row["Amount"] < 1 or row["Likable"] != 0 or row["Cost"] != 0):
        return False
    if row["CardID"]:
        return (row["ItemID"] == "" and row["UnitLevel"] == 1 and row["SkillLevel"] == 1
                and row["ClassChange"] in (0, 2))
    try:
        vote_key(row["ItemID"], 1)
    except ProtocolError:
        return False
    return all(row[key] == 0 for key in ("UnitLevel", "SkillLevel", "ClassChange"))


def _deliveries(record, group_id, mails):
    for point, delivery in record["deliveries"].items():
        if (not _positive_key(point) or not record["baseline_count"] < int(point) <= record["count"]
                or not isinstance(delivery, dict) or set(delivery) != {"definition", "mail_ids", "award_row"}
                or not _hash(delivery["definition"]) or not isinstance(delivery["mail_ids"], list)
                or not _award_valid(delivery["award_row"], group_id, int(point))
                or not 0 < len(delivery["mail_ids"]) <= MAX_PRESENTS
                or any(not _integer(mail, 1) for mail in delivery["mail_ids"])
                or len(set(delivery["mail_ids"])) != len(delivery["mail_ids"])
                or mails.intersection(delivery["mail_ids"])):
            _invalid("Vote reward delivery has invalid thresholds or repeated mail identities")
        mails.update(delivery["mail_ids"])
    return len(record["deliveries"])


def _earned(record, step):
    return record["count"] // step - record["baseline_count"] // step


def _delivered(entry):
    # Each archived instance ends where its successor starts, so their threshold points never collide.
    return {point: delivery for record in (*entry.get(ARCHIVED_KEY, ()), entry)
            for point, delivery in record["deliveries"].items()}


class VoteRewardService:
    def __init__(self, catalog, presents, *, finalists=None):
        self.catalog, self.presents = catalog, presents
        self.finalists = self._finalists = finalists
        self._initializer = presents.players.unit_initializer()
        self.content_version = self._content()
        self._ready()
        self._group_definitions = MappingProxyType({group.group_id: self._group_definition(group) for group in catalog.groups})
        self._event_fingerprint = _digest({"schema": 1, "event_id": catalog.event.event_id,
            "groups": list(self._group_definitions.values()), "maximum": MAX_ASSAULT})

    def content_version_for(self, items_version):
        try:
            versions = {"catalog": self.catalog.content_version, "presents": self.presents.asset_sha256,
                        "items": items_version, "initialization": self.presents.players.initialization_version}
            if any(not _hash(value) for value in versions.values()):
                raise ValueError("Missing reward fingerprints")
            return _digest({"schema": 1, "dependencies": versions, "templates": dict(self.presents.templates),
                "description": DESCRIPTION, "delivery": "atomic_mail", "baseline": "no_backfill",
                "repeat": "after_base_plus_interval", "assault": "group_floor_delta_cap_discard",
                "assault_maximum": MAX_ASSAULT})
        except (AttributeError, ValueError, TypeError) as error:
            raise ProtocolError("vote_rewards_dependencies_invalid", "Vote reward dependencies are unavailable", 503) from error

    def _content(self):
        items = getattr(self.presents, "item_catalog", None)
        return self.content_version_for(getattr(items, "content_version", None))

    def replayable_content_versions(self):
        return self._replayable_versions

    @cached_property
    def _replayable_versions(self):
        # Votes committed before emblem mail attached keep the digest of the item catalog without it.
        items = self.presents.item_catalog.replayable_content_versions()
        return frozenset((self.content_version, *map(self.content_version_for, items)))

    def _ready(self):
        self.catalog._ready()
        self.presents._ready()
        if self.finalists is not self._finalists or self.finalists is not None and self.finalists.catalog is not self.catalog:
            raise ProtocolError("vote_rewards_dependencies_changed", "Vote ranking service changed after setup", 503)
        if self.finalists is not None:
            self.finalists._ready()
        if self._content() != self.content_version:
            raise ProtocolError("vote_rewards_dependencies_changed", "Vote reward dependencies changed after setup", 503)

    def _group(self, group):
        try:
            if self.catalog.groups_by_id.get(group.group_id) != group:
                raise ValueError("Unknown group")
        except (AttributeError, ValueError, TypeError) as error:
            raise ProtocolError("vote_reward_group_invalid", "Vote rewards require the selected catalog group") from error
        return group

    def reward_row(self, reward, threshold=None):
        self._ready()
        if reward not in self.catalog.rewards:
            raise ProtocolError("vote_reward_unknown", "Vote reward is absent from the catalog", 503)
        return self._reward_row(reward, threshold)

    def _reward_row(self, reward, threshold=None):
        point = reward.threshold if threshold is None else threshold
        if not _integer(point, 1):
            raise ProtocolError("vote_reward_threshold_invalid", "Vote reward threshold must be a positive integer")
        stage, skill = reward.class_change, reward.skill_level
        if reward.kind == "unit":
            if self.presents.templates.get(reward.template_id) != reward.card_id:
                raise ProtocolError("vote_reward_template_changed", "Vote template no longer matches its unit", 503)
            initial = self._initializer.values(reward.card_id, 1)
            stage, skill = initial.stage, 1
        return {"GroupID": reward.group_id, "Count": point,
            "Repeat": reward.repeat_every if threshold is None else 0,
            "ItemID": reward.item_id, "GiftType": 0, "Amount": reward.amount, "SummonID": 0,
            "CardID": reward.card_id, "UnitLevel": reward.unit_level, "SkillLevel": skill,
            "ClassChange": stage, "Likable": reward.likable, "Cost": reward.cost}

    def group_definition(self, group):
        self._ready()
        group = self._group(group)
        return self._group_definitions[group.group_id]

    def _group_definition(self, group):
        rewards = [reward for reward in self.catalog.rewards if reward.group_id == group.group_id]
        return _digest({"schema": 1, "event_id": self.catalog.event.event_id,
            "group_id": group.group_id, "item_id": group.item_id, "final": group.final,
            "candidates": [candidate.card_id for candidate in self.catalog.candidates if candidate.group_id == group.group_id],
            "rewards": [{"source": asdict(reward), "display": self._reward_row(reward)} for reward in rewards],
            "earned_votes": self.catalog.rules.earned_votes, "description": DESCRIPTION})

    def _event_definition(self):
        return self._event_fingerprint

    def validated(self, state):
        totals = _totals(state)
        if "vote_rewards" not in state:
            return totals, None
        ledger = state["vote_rewards"]
        if (not isinstance(ledger, dict) or set(ledger) != {"schema_version", "items", "assault"}
                or type(ledger["schema_version"]) is not int or ledger["schema_version"] != 1
                or not isinstance(ledger["items"], dict) or set(ledger["items"]) != set(totals)
                or not isinstance(ledger["assault"], dict) or len(ledger["assault"]) > MAX_ITEMS):
            _invalid("Vote reward ledger identities do not match native totals")
        mails, deliveries, expected_assault = set(), 0, Counter()
        for item_id, entry in ledger["items"].items():
            if (not isinstance(entry, dict) or set(entry) - {ARCHIVED_KEY} != _ENTRY_FIELDS
                    or not _integer(entry["group_id"], 1) or entry["group_id"] != totals[item_id]["group_id"]
                    or not _integer(entry["baseline_count"]) or not _integer(entry["count"])
                    or not entry["baseline_count"] <= entry["count"] == totals[item_id]["count"]
                    or not isinstance(entry["deliveries"], dict)):
                _invalid("Vote reward baseline or count is inconsistent")
            if entry["definition"] is None:
                if entry["event_id"] is not None or entry["baseline_count"] != entry["count"] or entry["deliveries"]:
                    _invalid("Unbound vote baseline cannot have reward or assault progress")
            else:
                if not _hash(entry["definition"]) or not _integer(entry["event_id"], 1):
                    _invalid("Vote reward binding requires an event and definition")
                event = ledger["assault"].get(str(entry["event_id"]))
                if not isinstance(event, dict) or not _integer(event.get("earned_votes"), 1):
                    _invalid("Vote reward binding has no matching assault policy")
                expected_assault[str(entry["event_id"])] += _earned(entry, event["earned_votes"])
            deliveries += _deliveries(entry, entry["group_id"], mails)
            instances = entry.get(ARCHIVED_KEY, [])
            if not isinstance(instances, list) or ARCHIVED_KEY in entry and not 0 < len(instances) <= MAX_ARCHIVED:
                _invalid("Archived vote reward instances are invalid")
            reached = None
            for instance in instances:
                if (not isinstance(instance, dict) or set(instance) != _INSTANCE_FIELDS
                        or not _hash(instance["definition"]) or not _integer(instance["event_id"], 1)
                        or not _integer(instance["earned_votes"], 1)
                        or not _integer(instance["baseline_count"]) or not _integer(instance["count"])
                        or not instance["baseline_count"] <= instance["count"]
                        or reached is not None and instance["baseline_count"] != reached
                        or not isinstance(instance["deliveries"], dict)):
                    _invalid("Archived vote reward instances do not lead to their successor")
                deliveries += _deliveries(instance, entry["group_id"], mails)
                # The saved entitlement was earned at the instance's own interval, which a later rule may change.
                expected_assault[str(instance["event_id"])] += _earned(instance, instance["earned_votes"])
                reached = instance["count"]
            if reached is not None and reached != entry["baseline_count"]:
                _invalid("Archived vote reward instances do not lead to their successor")
        if len(mails) > MAX_MAIL_IDS or deliveries > MAX_DELIVERIES:
            _invalid("Permanent vote reward history exceeds its capacity")
        if set(ledger["assault"]) != set(expected_assault):
            _invalid("Assault history does not match bound vote events")
        for key, event in ledger["assault"].items():
            if (not _positive_key(key) or not isinstance(event, dict)
                    or set(event) != {"definition", "earned_votes", "earned", "spent", "discarded"}
                    or not _hash(event["definition"]) or not _integer(event["earned_votes"], 1)
                    or not _integer(event["earned"], maximum=MAX_ASSAULT)
                    or type(event["spent"]) is not int or event["spent"] != 0
                    or not _integer(event["discarded"])
                    or event["earned"] + event["discarded"] != expected_assault[key]):
                _invalid("Assault entitlement counters disagree with vote progress")
        return totals, ledger

    def initialize(self, state):
        self._ready()
        updated = initialize_vote_state(state)
        totals, ledger = self.validated(updated)
        if totals and ledger is None:
            updated["vote_rewards"] = {"schema_version": 1, "items": {
                item: {"group_id": values["group_id"], "event_id": None, "definition": None,
                       "baseline_count": values["count"], "count": values["count"], "deliveries": {}}
                for item, values in totals.items()}, "assault": {}}
        return updated

    def adopt_current(self, state):
        self._ready()
        _, ledger = self.validated(state)
        updated, items, assault = deepcopy(state), [], None
        if ledger is None:
            return updated, {"items": items, "assault": assault}
        event_id, earned_votes = self.catalog.event.event_id, self.catalog.rules.earned_votes
        rewards = updated["vote_rewards"]
        event = rewards["assault"].get(str(event_id))
        interval_changed = event is not None and event["earned_votes"] != earned_votes
        groups = {group.item_id: group for group in self.catalog.groups}
        for item_id, entry in sorted(rewards["items"].items()):
            if entry["definition"] is None:
                continue
            group = groups.get(item_id)
            stale = ((interval_changed and entry["event_id"] == event_id) or group is not None
                     and (entry["definition"], entry["event_id"]) != (self.group_definition(group), event_id))
            if not stale:
                continue
            instance = {"event_id": entry["event_id"], "definition": entry["definition"],
                        "earned_votes": rewards["assault"][str(entry["event_id"])]["earned_votes"],
                        "baseline_count": entry["baseline_count"], "count": entry["count"],
                        "deliveries": entry["deliveries"]}
            # Thresholds already passed are never backfilled: the item restarts as a baseline at its current total.
            entry.update(event_id=None, definition=None, baseline_count=entry["count"], deliveries={})
            entry[ARCHIVED_KEY] = [*entry.get(ARCHIVED_KEY, ()), instance]
            items.append({"item_id": item_id, "group_id": entry["group_id"], "event_id": instance["event_id"],
                          "count": entry["count"], "archived_deliveries": len(instance["deliveries"])})
        if event is not None and (interval_changed or event["definition"] != self._event_definition()):
            assault = {"event_id": event_id, "earned_votes": {"before": event["earned_votes"], "after": earned_votes}}
            event.update(definition=self._event_definition(), earned_votes=earned_votes)
        self.validated(updated)
        return updated, {"items": items, "assault": assault}

    def _occurrences(self, group, before, after, *, limit=MAX_PRESENTS):
        result = []
        for reward in self.catalog.rewards:
            if reward.group_id != group.group_id:
                continue
            if not reward.repeat_every:
                points = (reward.threshold,) if before < reward.threshold <= after else ()
            else:
                first = max(1, (before - reward.threshold) // reward.repeat_every + 1)
                last = (after - reward.threshold) // reward.repeat_every
                if last >= first and len(result) + last - first + 1 > limit:
                    raise ProtocolError("vote_reward_capacity", "Vote crosses too many reward thresholds", 409)
                points = (reward.threshold + offset * reward.repeat_every for offset in range(first, last + 1))
            for point in points:
                result.append((point, reward))
                if len(result) > limit:
                    raise ProtocolError("vote_reward_capacity", "Vote crosses too many reward thresholds", 409)
        if len({point for point, _ in result}) != len(result):
            raise ProtocolError("vote_reward_definition_invalid", "Vote rewards reuse a threshold", 503)
        return sorted(result, key=lambda value: value[0])

    def _reward(self, reward):
        self._reward_row(reward)
        if reward.kind == "unit":
            return {"kind": "unit", "template_id": reward.template_id, "count": reward.amount, "description": DESCRIPTION}
        if reward.kind == "item":
            return {"kind": "item", "item_id": reward.item_id, "amount": reward.amount, "description": DESCRIPTION}
        raise ProtocolError("vote_reward_unsupported", "Vote reward has no verified delivery path", 501)

    def _delivery_definition(self, point, reward):
        return _digest({"source": asdict(reward), "point": point, "display": self._reward_row(reward, point),
                        "delivery": self._reward(reward)})

    def _mail_candidate(self, before, candidate, groups, rewards):
        if not isinstance(candidate, dict) or not isinstance(groups, list) or len(groups) != len(rewards):
            raise ProtocolError("vote_reward_delivery_invalid", "Vote mail groups are incomplete", 503)
        rows, additions, last = self.presents.rows(before), [], before.get("last_present_id", 0)
        for group, reward in zip(groups, rewards):
            expected = self.presents._prepare_reward_rows(reward, len(rows) + len(additions))
            if (not isinstance(group, list) or not group
                    or any(not isinstance(row, dict) for row in group)
                    or [{key: value for key, value in row.items() if key != "PresentID"} for row in group] != expected
                    or [row.get("PresentID") for row in group] != list(range(last + len(additions) + 1,
                                                                           last + len(additions) + len(group) + 1))):
                raise ProtocolError("vote_reward_delivery_invalid", "Vote mail differs from its frozen rewards", 503)
            additions.extend(group)
        expected = deepcopy(before)
        if additions:
            expected.update(presents_schema=1, last_present_id=last + len(additions))
            expected["tables"]["presents"] = rows + deepcopy(additions)
        if _digest(expected) != _digest(candidate):
            raise ProtocolError("vote_reward_delivery_invalid", "Vote mail changed unrelated state", 503)
        self.presents.rows(candidate)
        return [row["PresentID"] for row in additions]

    def apply_vote(self, before, after_votes, group):
        self._ready()
        group = self._group(group)
        if self.finalists is not None:
            self.finalists.require_open(before, group)
        elif group.final:
            raise ProtocolError("vote_finalists_unavailable", "Final votes require verified qualification snapshots", 503)
        initialized = self.initialize(before)
        old_rows, new_rows = read_vote_history(before), read_vote_history(after_votes)
        old = {row["key"]: row for row in old_rows}
        new = {row["key"]: row for row in new_rows}
        changed = [row for key, row in new.items() if row != old.get(key)]
        if (set(old) - set(new) or len(changed) != 1
                or changed[0]["itemId"] != group.item_id or changed[0]["groupId"] != group.group_id
                or changed[0]["count"] <= old.get(changed[0]["key"], {}).get("count", 0)
                or ("vote_rewards" in before) != ("vote_rewards" in after_votes)
                or before.get("vote_rewards") != after_votes.get("vote_rewards")
                or before.get("vote_finalists") != after_votes.get("vote_finalists")):
            raise ProtocolError("vote_reward_transition_invalid", "Vote reward update must follow one increased candidate total", 409)
        row = changed[0]
        if row["key"] in old and any(row[key] != old[row["key"]][key] for key in row if key != "count"):
            raise ProtocolError("vote_reward_transition_invalid", "Vote update changed its native identity", 409)
        candidate = self.catalog.candidates_by_id.get(row["cardId"])
        eligible = (self.finalists.is_candidate(before, group, row["cardId"]) if group.final else
                    candidate is not None and candidate.group_id == group.group_id)
        if not eligible:
            raise ProtocolError("vote_reward_candidate_invalid", "Vote candidate does not belong to the selected group", 409)
        definition = self.group_definition(group)
        binding = after_votes.get("vote_state", {}).get("entries", {}).get(str(row["key"]), {})
        if binding.get("definition") != definition:
            raise ProtocolError("vote_reward_definition_changed", "Vote update does not bind the current reward definition", 503)
        updated = deepcopy(after_votes)
        ledger = deepcopy(initialized.get("vote_rewards", {"schema_version": 1, "items": {}, "assault": {}}))
        updated["vote_rewards"] = ledger
        previous, total = group_total(before, group.item_id), group_total(after_votes, group.item_id)
        entry = ledger["items"].setdefault(group.item_id, {"group_id": group.group_id, "event_id": None,
            "definition": None, "baseline_count": previous, "count": previous, "deliveries": {}})
        event_id = self.catalog.event.event_id
        event_definition = self._event_definition()
        if entry["definition"] not in (None, definition) or entry["event_id"] not in (None, event_id):
            raise ProtocolError("vote_reward_definition_changed", "Vote reward definition changed while history exists", 503)
        event = ledger["assault"].setdefault(str(event_id), {"definition": event_definition,
            "earned_votes": self.catalog.rules.earned_votes, "earned": 0, "spent": 0, "discarded": 0})
        if event["definition"] != event_definition or event["earned_votes"] != self.catalog.rules.earned_votes:
            raise ProtocolError("vote_reward_definition_changed", "Vote event rules changed while history exists", 503)
        expected = self._occurrences(group, entry["baseline_count"], previous, limit=MAX_DELIVERIES)
        if set(entry["deliveries"]) != {str(point) for point, _ in expected}:
            _invalid("Vote reward history omits an earned threshold")
        for point, reward in expected:
            if entry["deliveries"][str(point)]["definition"] != self._delivery_definition(point, reward):
                raise ProtocolError("vote_reward_definition_changed", "Delivered vote reward definition changed", 503)
        occurrences = self._occurrences(group, previous, total)
        rewards = [[self._reward(reward)] for _, reward in occurrences]
        mailed, groups = self.presents.enqueue_groups(deepcopy(updated), rewards)
        ids = self._mail_candidate(updated, mailed, groups, rewards)
        ledger = mailed["vote_rewards"]
        entry = ledger["items"][group.item_id]
        entry.update(event_id=event_id, definition=definition, count=total)
        for (point, reward), additions in zip(occurrences, groups):
            entry["deliveries"][str(point)] = {"definition": self._delivery_definition(point, reward),
                                              "mail_ids": [row["PresentID"] for row in additions],
                                              "award_row": self._reward_row(reward, point)}
        event = ledger["assault"][str(event_id)]
        earned = total // event["earned_votes"] - previous // event["earned_votes"]
        accepted = min(earned, MAX_ASSAULT - event["earned"] + event["spent"])
        event["earned"] += accepted
        event["discarded"] += earned - accepted
        self.validated(mailed)
        return mailed, [self._reward_row(reward, point) for point, reward in occurrences], ids

    def validate_delivery(self, state, item_id, award_rows, mail_ids, *, verify_current=True):
        self._ready()
        if type(verify_current) is not bool:
            raise ValueError("Vote receipt verification mode must be boolean")
        try:
            vote_key(item_id, 1)
        except ProtocolError as error:
            raise ProtocolError("vote_reward_receipt_invalid", "Vote receipt requires a valid item identity", 503) from error
        _, ledger = self.validated(state)
        if (not isinstance(award_rows, list) or not isinstance(mail_ids, list)
                or len(award_rows) > MAX_PRESENTS or len(mail_ids) > MAX_PRESENTS
                or any(not _integer(identifier, 1) for identifier in mail_ids)
                or len(set(mail_ids)) != len(mail_ids)):
            raise ProtocolError("vote_reward_receipt_invalid", "Vote reward receipt requires bounded reward and mail lists", 503)
        if not award_rows:
            if mail_ids:
                raise ProtocolError("vote_reward_receipt_invalid", "Vote mail has no recorded reward source", 503)
            return
        entry = None if ledger is None else ledger["items"].get(item_id)
        group = next((group for group in self.catalog.groups if group.item_id == item_id), None)
        if entry is None or (verify_current and (group is None or entry["definition"] != self.group_definition(group))):
            raise ProtocolError("vote_reward_receipt_invalid", "Vote reward receipt has no matching item definition", 503)
        occurrences = (dict(self._occurrences(group, entry["baseline_count"], entry["count"], limit=MAX_DELIVERIES))
                       if verify_current else {})
        # Receipts committed before an adoption name deliveries that now sit in archived instances.
        delivered = entry["deliveries"] if verify_current else _delivered(entry)
        flattened, previous = [], 0
        for row in award_rows:
            point = row.get("Count") if isinstance(row, dict) else None
            if not _integer(point, 1) or point <= previous:
                raise ProtocolError("vote_reward_receipt_invalid", "Vote receipt thresholds must be ordered and distinct", 503)
            reward = occurrences.get(point)
            saved = delivered.get(str(point))
            if (saved is None or row != saved["award_row"]
                    or verify_current and (reward is None or row != self._reward_row(reward, point)
                        or saved["definition"] != self._delivery_definition(point, reward))):
                raise ProtocolError("vote_reward_receipt_invalid", "Vote receipt differs from its permanent reward source", 503)
            flattened.extend(saved["mail_ids"])
            previous = point
        if flattened != mail_ids:
            raise ProtocolError("vote_reward_receipt_invalid", "Vote receipt mail belongs to a different reward delivery", 503)

    def assault_rights(self, state):
        self._ready()
        _, ledger = self.validated(state)
        return {} if ledger is None else {key: {"earned": event["earned"], "spent": event["spent"]}
                                          for key, event in ledger["assault"].items()}

    def assault_response(self, state, group=None):
        self._ready()
        _, ledger = self.validated(state)
        event_id = self.catalog.event.event_id
        event = {} if ledger is None else ledger["assault"].get(str(event_id), {})
        remain = event.get("earned", 0) - event.get("spent", 0) - converted_assault_rights(state, event_id)
        if remain < 0:
            _invalid("Converted challenge rights exceed the saved entitlement")
        next_votes = 0
        if group is not None:
            group = self._group(group)
            step = event.get("earned_votes", self.catalog.rules.earned_votes)
            next_votes = step - group_total(state, group.item_id) % step
        return Container("ASSAULT", (Column("Remain", "I", (remain,)), Column("Next", "I", (next_votes,))))
