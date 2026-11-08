from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import logging
import re

from .alxml import Column, Container, Limits, dumps, loads
from .bootstrap import STARTUP_COLLECTION_LIMITS
from .energy_clock import instant
from .errors import REFUSAL_AFTERMATH, ProtocolError
from .event_pass_rules import EventPassAutomationRules
from .event_pass_state import (DEFINITION_CHANGED, IDENTITY_UNREPLAYABLE, MAX_ITEM_BYTES, MAX_PASSES, RESPONSE_CAPACITY,
                               STORED_ITEM_BYTES, delivered_mail_ids, initialize_event_passes, mark_event_pass,
                               read_event_passes)
from .native_numbers import int32
from .possessions import MAX_ITEM_NAME_BYTES
from .presents import MAX_PRESENTS, PRESENT_LIMITS
from .protocol import RESPONSE_TOO_LARGE, ProtocolRegistry, encode_response
from .rank_reward_state import RESPONSE_CAPACITY as RANK_REWARD_RESPONSE_CAPACITY
from .request_fields import registered, values
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger


RESPONSE_LIMIT = 256 * 1024
HISTORY_NAMESPACE = "event_pass_history"
DELIVERY_KEY = "event_pass_delivery"
AUTOMATION_KEY = "event_pass_automation"
REBIND_OPERATION = "event_pass.rebind_definition"
STATUS_LIMITS = Limits(max_values=MAX_PASSES * 3 + 16, max_elements=MAX_PASSES * 3 + 32)
REPLY_LIMITS = Limits(max_bytes=RESPONSE_LIMIT, max_values=MAX_PRESENTS * 9 + MAX_PASSES * 3 + 32,
                      max_elements=MAX_PRESENTS * 9 + MAX_PASSES * 3 + 64)
DESCRIPTION = "イベントパス報酬"
NOTHING_CLAIMABLE = "event_pass_nothing_claimable"
_HASH = re.compile(r"[a-f0-9]{64}\Z")
log = logging.getLogger("aigis_ps")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


def pass_identity(entry):
    return _digest({"normal": entry.item_id, "premium": entry.premium_item_id})


def reward_semantics(rewards):
    return _digest([{"id": reward.reward_id, "point": reward.required_point, "premium": reward.premium,
                     "type": reward.reward_item_type, "param": reward.param, "item": reward.item_id}
                    for reward in sorted(rewards, key=lambda item: item.reward_id)])


def _identifier(value):
    try:
        return (type(value) is str and 0 < len(value.encode("utf-8")) <= STORED_ITEM_BYTES
                and bool(dumps(Container(children=(Column("ID", "S", (value,)),)))))
    except (ValueError, TypeError, UnicodeError):
        return False


def _ids(value):
    return (isinstance(value, list) and 0 < len(value) <= MAX_PRESENTS
            and all(int32(item) and item > 0 for item in value) and len(set(value)) == len(value))


def _history_invalid():
    raise ProtocolError("event_pass_history_invalid", "Stored event pass receipts are invalid", 503)


def _automation_changed():
    raise ProtocolError("event_pass_automation_changed", "Automatic event pass rules changed after setup", 503)


def _receipt_valid(entry):
    return _identifier(entry["pass_id"]) and _ids(entry["mail_ids"])


class EventPassService:
    def __init__(self, bootstrap, players, presents, possessions, catalog, *, automation_rules=None):
        self.bootstrap, self.players, self.presents = bootstrap, players, presents
        self.possessions, self.catalog = possessions, catalog
        self.automation_rules = EventPassAutomationRules() if automation_rules is None else automation_rules
        if not isinstance(self.automation_rules, EventPassAutomationRules):
            raise ValueError("Event pass automation requires verified rules")
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (42, 137, 158, 159)}
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "event_pass_history_invalid", "Stored event pass receipts are invalid",
                                     fields=("request", "content", "state", "revision", "response", "pass_id", "mail_ids"),
                                     response_limit=RESPONSE_LIMIT, check=_receipt_valid)
        self.content_version = self._content()
        # Receipts committed before emblem mail attached keep the digest of the item catalog without it.
        items = self.presents.item_catalog.replayable_content_versions()
        # Receipts committed while pass windows compared JST instants keep that catalog digest.
        self._replayable_versions = frozenset((self.content_version, *(self.content_version_for(item, catalog)
            for item in items for catalog in self.catalog.replayable_content_versions())))
        self._automation_version = self._automatic_content(self.content_version)
        self._ready()

    def content_version_for(self, items_version, catalog_version=None):
        try:
            catalog = self.catalog.content_version if catalog_version is None else catalog_version
            versions = {"catalog": catalog, "possessions": self.possessions.asset_sha256,
                        "presents": self.presents.asset_sha256, "items": items_version,
                        "recovery": self.presents.recovery_catalog.content_version,
                        "tickets": self.presents.item_tickets.content_version}
            if any(type(value) is not str or not _HASH.fullmatch(value) for value in versions.values()):
                raise ValueError("Event pass dependencies require stable fingerprints")
            return _digest({"policy": 1, "dependencies": versions, "templates": dict(self.presents.templates),
                            "description": DESCRIPTION, "claim": "one_pass_atomic_mail",
                            "watermark": "highest_fully_delivered_threshold", "points": "preserve_inventory"})
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError("event_pass_dependencies_invalid", "Event pass dependencies are unavailable", 503) from error

    def _content(self):
        items = getattr(self.presents, "item_catalog", None)
        return self.content_version_for(getattr(items, "content_version", None))

    def replayable_content_versions(self):
        return self._replayable_versions

    def _ready(self):
        self.catalog._ready()
        self.presents._ready()
        self.presents.item_tickets._ready()
        if self._content() != self.content_version:
            raise ProtocolError("event_pass_dependencies_changed", "Event pass dependencies changed after setup", 503)

    def _automatic_content(self, manual_content):
        if not isinstance(self.automation_rules, EventPassAutomationRules):
            _automation_changed()
        return _digest({"policy": 1, "manual_content": manual_content,
                        "rules": asdict(self.automation_rules), "notification_category": 7,
                        "retry_policy": "whole_pass_pending", "delivery_order": "catalog_source"})

    def automation_content_version_for(self, items_version, catalog_version=None):
        return self._automatic_content(self.content_version_for(items_version, catalog_version))

    @property
    def automation_content_version(self):
        return self._automation_version

    def _automatic_ready(self):
        self._ready()
        if self._automatic_content(self.content_version) != self._automation_version:
            _automation_changed()

    def handlers(self):
        return {158: self.read, 159: self.claim}

    def initialize(self, state):
        self._ready()
        updated = initialize_event_passes(state)
        self.validate_delivery(updated)
        self.pending(updated)
        return updated

    def status(self):
        try:
            catalog_status = self.catalog.status()
        except ProtocolError as failure:
            catalog_status = {"ready": False, "error": failure.code}
        try:
            self._automatic_ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "implemented_operations": [158, 159],
                "content_version": self.content_version, "catalog": catalog_status,
                "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "request_order": REQUEST_ORDER,
                "claim_policy": "One pass, both eligible tracks, atomic mailbox delivery",
                "empty_claim_policy": "Nothing eligible: saved status of that pass, no mail, nothing recorded",
                "watermark_policy": "Highest fully delivered threshold; no point consumption",
                "identity_policy": (f"New passes need IDs within the {MAX_ITEM_BYTES}-byte native cell; "
                                    f"stored IDs up to {STORED_ITEM_BYTES} bytes stay readable"),
                "automation": {**(asdict(self.automation_rules) if isinstance(self.automation_rules, EventPassAutomationRules) else {}),
                               "content_version": self.automation_content_version,
                               "end_policy": "After EndDate, or configured closed status after StartDate; local rule"},
                "unavailable": ["Premium purchase", "Limited mission point sources",
                                "Revival resource schedule"], "native_tested": False}

    @staticmethod
    def pending(state):
        if AUTOMATION_KEY not in state:
            return {}
        saved = state[AUTOMATION_KEY]
        if (not isinstance(saved, dict) or set(saved) != {"schema_version", "pending"}
                or type(saved["schema_version"]) is not int or saved["schema_version"] != 1
                or not isinstance(saved["pending"], dict) or len(saved["pending"]) > MAX_PASSES):
            raise ProtocolError("event_pass_automation_invalid", "Automatic event pass state is invalid", 503)
        for identifier, reason in saved["pending"].items():
            if (not _identifier(identifier) or type(reason) is not str
                    or not re.fullmatch(r"[a-zA-Z0-9_]{1,128}", reason)):
                raise ProtocolError("event_pass_automation_invalid", "Automatic event pass pending reason is invalid", 503)
        return deepcopy(saved["pending"])

    def settle_expired(self, profile, state, now, *, validate=None):
        self._automatic_ready()
        if validate is not None and not callable(validate):
            raise ValueError("Automatic reward validation must be callable")
        updated = self.initialize(state)
        pending = self.pending(updated)
        issued = {}
        if not self.automation_rules.enabled:
            return updated, issued
        current = instant(now)
        for entry in self.catalog.passes:
            if not self.automation_rules.ended(entry, current):
                continue
            try:
                candidate, ids = self.deliver(updated, entry)
                candidate = self.presents.set_notification_category(candidate, ids, 7)
                self._preflight(profile, candidate)
                combined = {**issued, entry.item_id: ids}
                if validate is not None:
                    validate(candidate, deepcopy(combined))
            except ProtocolError as error:
                if error.code == NOTHING_CLAIMABLE:
                    pending.pop(entry.item_id, None)
                    continue
                if error.status not in (409, 501) and error.code not in (
                        RESPONSE_TOO_LARGE, RANK_REWARD_RESPONSE_CAPACITY, RESPONSE_CAPACITY,
                        IDENTITY_UNREPLAYABLE, DEFINITION_CHANGED):
                    raise
                if error.code == IDENTITY_UNREPLAYABLE:
                    log.warning("Automatic settlement keeps expired event pass %s pending: %s", entry.item_id, error)
                elif error.code == DEFINITION_CHANGED:
                    log.warning("Automatic settlement keeps expired event pass %s pending with %s; run %s on the console",
                                entry.item_id, error.code, REBIND_OPERATION)
                pending[entry.item_id] = error.code
                continue
            updated = candidate
            issued = combined
            pending.pop(entry.item_id, None)
        if pending or AUTOMATION_KEY in state:
            updated[AUTOMATION_KEY] = {"schema_version": 1, "pending": pending}
        self.pending(updated)
        self._automatic_ready()
        return updated, issued

    def response(self, state, pass_ids=None):
        try:
            self._ready()
        except ProtocolError as error:
            if pass_ids is not None:
                raise
            # EventPassStatus is the saved progress, so 137 serves it instead of keeping the native startup loop resending.
            self.bootstrap.startup_diagnostics.degrade(error)
        rows = read_event_passes(state)
        if pass_ids is not None:
            if (not isinstance(pass_ids, (list, tuple)) or len(pass_ids) > MAX_PASSES
                    or any(not _identifier(name) for name in pass_ids) or len(set(pass_ids)) != len(pass_ids)):
                raise ProtocolError("event_pass_selection_invalid", "Event pass response requires distinct item identities")
            indexed = {row["ID"]: row for row in rows}
            if set(pass_ids) - indexed.keys():
                raise ProtocolError("event_pass_state_missing", "Selected event pass state is absent", 503)
            rows = [indexed[name] for name in pass_ids]
        return Container("EventPassStatus", (Column("ID", "S", tuple(row["ID"] for row in rows)),
            Column("ReceivedNormalPoint", "I", tuple(row["ReceivedNormalPoint"] for row in rows)),
            Column("ReceivedPremiumPoint", "I", tuple(row["ReceivedPremiumPoint"] for row in rows))))

    def read(self, envelope, store):
        registered(envelope, {158}, ())
        self._ready()
        profile = self.bootstrap._authenticate(envelope, store)
        self.players.validate(profile.state)
        response = dumps(Container(children=(self.bootstrap._system(profile), self.response(profile.state))), limits=STATUS_LIMITS)
        encode_response(envelope.profile, response)
        return response

    def _rewards(self, definitions):
        result = []
        for reward in definitions:
            kind, param, item = reward.reward_item_type, reward.param, reward.item_id
            if kind == 0:
                values_to_add = [{"kind": "free_crystal", "amount": param}]
            elif kind == 1:
                if param not in self.presents.templates:
                    raise ProtocolError("event_pass_reward_unsupported", "Event pass unit template is unavailable", 501)
                values_to_add = [{"kind": "unit", "template_id": param, "count": 1}]
            elif kind == 2:
                values_to_add = [{"kind": "gold", "amount": param}]
            elif kind == 22 and len(item.encode("utf-8")) > MAX_ITEM_NAME_BYTES:
                # The mail writer refuses such new rows with 503, which 116 automation would not keep pending.
                raise ProtocolError("event_pass_reward_unsupported", "Event pass reward item does not fit the native cell", 501)
            elif kind == 22 and self.presents.recovery_catalog.supports_item(item):
                self.presents.recovery_catalog.definition(22, 1, item)
                if param > MAX_PRESENTS - len(result):
                    raise ProtocolError("event_pass_reward_capacity", "Recovery rewards exceed mailbox capacity", 409)
                values_to_add = [{"kind": "charisma_recovery", "item_id": item}] * param
            elif kind == 22:
                values_to_add = [{"kind": "item", "item_id": item, "amount": param}]
            else:
                raise ProtocolError("event_pass_reward_unsupported", "Event pass reward is unsupported", 501)
            if len(result) + len(values_to_add) > MAX_PRESENTS:
                raise ProtocolError("event_pass_reward_capacity", "Event pass rewards exceed mailbox capacity", 409)
            result.extend({**value, "description": DESCRIPTION} for value in values_to_add)
        return result

    def deliver(self, state, entry):
        updated = self.initialize(state)
        counts = self.possessions.counts(updated)
        points = counts.get(entry.item_id, 0)
        premium = counts.get(entry.premium_item_id, 0) > 0
        previous = next((row for row in read_event_passes(updated) if row["ID"] == entry.item_id),
                        {"ReceivedNormalPoint": 0, "ReceivedPremiumPoint": 0})
        grouped = defaultdict(list)
        for reward in entry.rewards:
            field = "ReceivedPremiumPoint" if reward.premium else "ReceivedNormalPoint"
            if (not reward.premium or premium) and previous[field] < reward.required_point <= points:
                grouped[(reward.premium, reward.required_point)].append(reward)
        if not grouped:
            raise ProtocolError(NOTHING_CLAIMABLE, "Event pass has no unclaimed eligible rewards", 409)
        ordered_groups = sorted(grouped.items())
        updated, additions_by_group = self.presents.enqueue_groups(updated,
            [self._rewards(rewards) for _, rewards in ordered_groups])
        if not isinstance(additions_by_group, list) or len(additions_by_group) != len(ordered_groups):
            raise ProtocolError("event_pass_delivery_invalid", "Event pass reward groups are incomplete", 503)
        normal, paid, mail_ids = {}, {}, []
        for ((is_premium, point), rewards), additions in zip(ordered_groups, additions_by_group):
            ids = [row["PresentID"] for row in additions]
            if not _ids(ids):
                raise ProtocolError("event_pass_delivery_invalid", "Event pass reward delivery returned no valid mail identities", 503)
            (paid if is_premium else normal)[point] = {"definition": reward_semantics(rewards),
                "reward_ids": [reward.reward_id for reward in rewards], "mail_ids": ids}
            mail_ids.extend(ids)
        updated = mark_event_pass(updated, entry.item_id, definition=pass_identity(entry),
                                  normal_deliveries=normal, premium_deliveries=paid)
        return updated, mail_ids

    def require_eligible(self, entry, now):
        if not self.catalog.is_eligible(entry, now):
            raise ProtocolError("event_pass_inactive", "Event pass is outside its active reward window", 409)

    def grant_points(self, state, pass_id, amount, now):
        self._ready()
        entry = self.catalog.get(pass_id)
        if not int32(amount) or amount <= 0:
            raise ProtocolError("event_pass_points_invalid", "Event pass point grants require a positive int32 amount")
        before = self.possessions.counts(state).get(entry.item_id, 0)
        # Like mission EP, points for a pass outside its reward window are discarded instead of banked.
        if self.catalog.is_eligible(entry, now):
            updated, discarded = self.possessions.credit_capped(state, {entry.item_id: amount})
            credited, inactive = amount - discarded.get(entry.item_id, 0), 0
        else:
            updated, credited, inactive = deepcopy(state), 0, amount
        return updated, {"pass_id": entry.item_id, "requested": amount, "credited": credited, "before": before,
                         "after": before + credited, "capped": amount - credited - inactive, "inactive": inactive}

    def _preflight(self, profile, state):
        candidate = replace(profile, state=state)
        encode_response(self._profiles[42], dumps(Container(children=(self.bootstrap._system(candidate),
            self.presents.response(state))), limits=PRESENT_LIMITS))
        startup = self.bootstrap._configuration_response(candidate)
        groups = [child for child in loads(startup, limits=STARTUP_COLLECTION_LIMITS).children if child.name == "EventPassStatus"]
        if groups != [self.response(state)]:
            raise ProtocolError("event_pass_provider_inconsistent", "Startup event pass state differs from the saved state", 503)
        encode_response(self._profiles[137], startup)

    def _unclaimed(self, envelope, profile, pass_id):
        # The native claim button counts every active pass while 159 names only the first.
        log.warning("Event pass %s has no claimable rewards; replying with its saved status", pass_id)
        saved = [pass_id] if any(row["ID"] == pass_id for row in read_event_passes(profile.state)) else []
        response = dumps(Container(children=(self.bootstrap._system(profile), self.response(profile.state, saved))),
                         limits=STATUS_LIMITS)
        encode_response(envelope.profile, response)
        return response

    def _response(self, profile, state, pass_id, mail_ids):
        response = dumps(Container(children=(self.bootstrap._system(profile),
            self.presents.response(state, mail_ids), self.response(state, [pass_id]))), limits=REPLY_LIMITS)
        if len(response) > RESPONSE_LIMIT:
            raise ProtocolError(RESPONSE_CAPACITY, "Event pass response exceeds receipt capacity", 503)
        encode_response(self._profiles[159], response)
        return response

    @staticmethod
    def validate_delivery(state, request=None, pass_id=None, mail_ids=None):
        saved = state.get(DELIVERY_KEY)
        if saved is None and DELIVERY_KEY not in state and request is None:
            return
        if (not isinstance(saved, dict) or set(saved) != {"schema_version", "request", "pass_id", "mail_ids"}
                or type(saved["schema_version"]) is not int or saved["schema_version"] != 1
                or type(saved["request"]) is not str or not _HASH.fullmatch(saved["request"])
                or not _identifier(saved["pass_id"]) or not _ids(saved["mail_ids"])
                or request is not None and (saved["request"] != request or saved["pass_id"] != pass_id
                                           or saved["mail_ids"] != mail_ids)):
            _history_invalid()
        if not set(saved["mail_ids"]) <= delivered_mail_ids(state).get(saved["pass_id"], frozenset()):
            _history_invalid()

    def _receipts(self, state, revision):
        history = self._ledger.read(state, revision)
        if history["receipts"]:
            newest = history["receipts"][-1]
            self.validate_delivery(state, newest["request"], newest["pass_id"], newest["mail_ids"])
        elif DELIVERY_KEY in state:
            _history_invalid()
        return history

    def claim(self, envelope, store):
        self._ready()
        request = registered(envelope, {159}, {"EPI"}, {"EPI"})
        pass_id = values(request["EPI"], "S", minimum=1, maximum=1, max_bytes=STORED_ITEM_BYTES)[0]
        entry = self.catalog.get(pass_id)
        key = hashlib.sha256(f"159:{envelope.profile.route}:{envelope.profile.st}:".encode() + envelope.xml).hexdigest()
        now = None
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            self.players.validate(profile.state)
            history = self._receipts(profile.state, profile.revision)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                if cached["revision"] != profile.revision or cached["content"] not in self.replayable_content_versions():
                    raise ProtocolError("event_pass_retry_stale", "Event pass state changed after this request", 409)
                if cached["pass_id"] != pass_id or cached["state"] != _state_digest(profile.state):
                    _history_invalid()
                self._preflight(profile, profile.state)
                response = self._response(profile, profile.state, pass_id, cached["mail_ids"])
                if response != cached["response"].encode("utf-8"):
                    _history_invalid()
                encode_response(envelope.profile, response)
                return response
            if self._ledger.processed(history, key):
                log.warning("Refused request 159: it repeats a processed request whose receipt was evicted")
                raise ProtocolError("event_pass_request_stale", "Event pass request was already processed", 409)
            if now is None:
                now = instant(self.bootstrap.clock())
            self.require_eligible(entry, now)
            try:
                updated, mail_ids = self.deliver(profile.state, entry)
            except ProtocolError as error:
                if error.code == NOTHING_CLAIMABLE:
                    return self._unclaimed(envelope, profile, pass_id)
                if error.code == IDENTITY_UNREPLAYABLE:
                    log.warning("Refused request 159 for event pass %s: %s", pass_id, error)
                elif error.code == DEFINITION_CHANGED:
                    log.warning("Refused request 159 for event pass %s with %s; the native client %s, "
                                "so run %s on the console", pass_id, error.code, REFUSAL_AFTERMATH, REBIND_OPERATION)
                raise
            self._preflight(profile, updated)
            response = self._response(profile, updated, pass_id, mail_ids)
            encode_response(envelope.profile, response)
            updated[DELIVERY_KEY] = {"schema_version": 1, "request": key, "pass_id": pass_id, "mail_ids": mail_ids}
            self._ledger.record(updated, history, {"request": key, "content": self.content_version,
                                                   "state": _state_digest(updated), "revision": profile.revision + 1,
                                                   "response": response.decode("utf-8"), "pass_id": pass_id,
                                                   "mail_ids": mail_ids})
            self._receipts(updated, profile.revision + 1)
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while claiming event pass rewards", 409)
