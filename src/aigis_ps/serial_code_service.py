from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date
import hashlib
from itertools import combinations
import json
import logging
import re
import threading
from types import MappingProxyType

from .alxml import Column, Container, dumps
from .business_calendar import business_date
from .errors import REFUSAL_AFTERMATH as _REFUSAL_AFTERMATH, ProtocolError
from .native_numbers import INT32_MAX, int32
from .presents import GIFT_BOX_DEPENDENCIES, ITEM_CATALOG, MAX_PRESENTS, PRESENT_CAPACITY_ERRORS
from .protocol import ProtocolRegistry, encode_response
from .request_fields import fields, registered, scalar
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .serial_code_rules import CODE_PATTERN, SerialCodeRules, entered_form
from .storage import MAX_REVISION, PlayerProfile


OPERATION = 32
HISTORY_NAMESPACE = "serial_code_history"
REDEMPTION_NAMESPACE = "serial_code_redemptions"
NAMESPACES = (REDEMPTION_NAMESPACE, HISTORY_NAMESPACE)
REDEMPTION_SCHEMA_VERSION = 1
# Removed codes keep their counts, so the journal can outgrow any single code table.
MAX_REDEEMED_CODES = 65535
RESPONSE_LIMIT = 128 * 1024
APPEND_VALUES = (0, 1)
# OnCommandMenuSerialCode rows; other values can hit commands of other menus, and 1/-12 need direct barracks credit.
SILENT, PRESENTS_ADDED, INVALID_CODE, USED_CODE, USED_UNIQUE_CODE = 0, 2, -10, -11, -13
STATUS_NAMES = MappingProxyType({SILENT: "silent", PRESENTS_ADDED: "presents_added", INVALID_CODE: "invalid_code",
                                 USED_CODE: "used_code", USED_UNIQUE_CODE: "used_unique_code"})
# Rewards that cannot reach the present box keep the code unused behind 0, the only result without a dialog.
UNDELIVERABLE = PRESENT_CAPACITY_ERRORS | {"serial_code_response_capacity", "serial_code_journal_capacity"}
PRESENT_DEPENDENCIES = (*GIFT_BOX_DEPENDENCIES, "vote_cooking_catalog")
# Local profiles carry token_hex(16) method keys, which every reply echoes as its ExchangeKey.
_EXCHANGE_KEY_SAMPLE = "0" * 32
_RECEIPT_FIELDS = ("request", "content", "state", "revision", "response", "code", "append", "present_ids")
_RECORD_FIELDS = frozenset(("count", "first_day", "last_day", "last_present_ids"))
_HASH = re.compile(r"[a-f0-9]{64}\Z")
log = logging.getLogger("aigis_ps")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


def _present_ids(value):
    return (isinstance(value, list) and 0 < len(value) <= MAX_PRESENTS
            and all(int32(identifier) and identifier > 0 for identifier in value) and len(set(value)) == len(value))


def _day_text(value):
    try:
        return type(value) is str and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _receipt_valid(entry):
    return (type(entry["code"]) is str and CODE_PATTERN.fullmatch(entry["code"]) is not None
            and type(entry["append"]) is int and entry["append"] in APPEND_VALUES and _present_ids(entry["present_ids"]))


def _invalid_redemptions():
    raise ProtocolError("serial_code_redemptions_invalid", "Stored serial code redemptions are invalid", 503)


def read_redemptions(state) -> dict:
    if not isinstance(state, dict):
        _invalid_redemptions()
    if REDEMPTION_NAMESPACE not in state:
        return {}
    journal = state[REDEMPTION_NAMESPACE]
    if (not isinstance(journal, dict) or set(journal) != {"schema_version", "codes"}
            or type(journal["schema_version"]) is not int or journal["schema_version"] != REDEMPTION_SCHEMA_VERSION
            or not isinstance(journal["codes"], dict) or len(journal["codes"]) > MAX_REDEEMED_CODES):
        _invalid_redemptions()
    for code, record in journal["codes"].items():
        if (type(code) is not str or not CODE_PATTERN.fullmatch(code) or not isinstance(record, dict)
                or set(record) != _RECORD_FIELDS or not int32(record["count"]) or record["count"] < 1
                or not _day_text(record["first_day"]) or not _day_text(record["last_day"])
                or not _present_ids(record["last_present_ids"])):
            _invalid_redemptions()
    return deepcopy(journal["codes"])


def _redeemed(state, code, day, present_ids):
    codes = read_redemptions(state)
    record = codes.get(code)
    if record is None and len(codes) >= MAX_REDEEMED_CODES:
        raise ProtocolError("serial_code_journal_capacity", "The serial code journal has no room for another code", 409)
    today = day.isoformat()
    codes[code] = {"count": 1 if record is None else record["count"] + 1,
                   "first_day": today if record is None else record["first_day"],
                   "last_day": today, "last_present_ids": list(present_ids)}
    updated = {**state, REDEMPTION_NAMESPACE: {"schema_version": REDEMPTION_SCHEMA_VERSION, "codes": codes}}
    read_redemptions(updated)
    return updated


def _business_day(now) -> date:
    try:
        return business_date(now)
    except (OverflowError, ValueError, TypeError, ProtocolError) as error:
        raise ProtocolError("serial_code_clock_invalid", "Serial codes require a representable instant", 503) from error


def _result(status):
    return Container("SERIALCODE", (Column("A1", "S", ("STATUS",)), Column("A2", "I", (status,))))


class SerialCodeService:
    def __init__(self, bootstrap, presents, rules=None):
        self.bootstrap, self.presents = bootstrap, presents
        self.rules = SerialCodeRules() if rules is None else rules
        if not isinstance(self.rules, SerialCodeRules):
            raise ValueError("Serial codes require verified local rules")
        self._profile = ProtocolRegistry.load().by_registration[OPERATION]
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "serial_code_history_invalid",
                                     "Stored serial code receipts are invalid", fields=_RECEIPT_FIELDS,
                                     response_limit=RESPONSE_LIMIT, check=_receipt_valid)
        self._lock = threading.Lock()
        self._answers, self._kept_unused = Counter(), Counter()
        self.content_version = self._content()
        self._ready()
        self.unavailable_codes = self._unavailable_codes()
        for code, reason in self.unavailable_codes.items():
            log.warning("Serial code %r cannot reach the present box (%s); redeeming it answers 0 and keeps it unused",
                        code, reason)

    def _dependencies(self, absent, items=None):
        versions = {"presents": self.presents.asset_sha256}
        for name in PRESENT_DEPENDENCIES:
            dependency = None if name in absent else getattr(self.presents, name)
            versions[name] = None if dependency is None else dependency.content_version
        if items is not None and versions[ITEM_CATALOG] is not None:
            versions[ITEM_CATALOG] = items
        return versions

    def _content(self, absent=frozenset(), items=None):
        try:
            dependencies = self._dependencies(absent, items) if self.rules.codes else {}
            if any(value is not None and (type(value) is not str or not _HASH.fullmatch(value))
                   for value in dependencies.values()):
                raise ValueError("Serial code rewards require stable present fingerprints")
            return _digest({"policy": 1, "rules": self.rules.content_version, "dependencies": dependencies,
                            "delivery": "present_box_status_2_with_ppt_delta", "day": "JST_04",
                            "window": "inclusive_business_days", "undeliverable": "status_0_code_unused",
                            "repeated": "current_status_without_redeeming_again"})
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError("serial_code_dependencies_invalid", "Serial code reward dependencies are unavailable",
                                503) from error

    def _ready(self):
        if self.rules.codes:
            self.presents._ready()
        if self._content() != self.content_version:
            raise ProtocolError("serial_code_dependencies_changed", "Serial code reward dependencies changed after setup",
                                503)

    def _replayable(self, content):
        if content == self.content_version:
            return True
        # A receipt keeps the digest of its commit, which presents dependencies attached since then cannot reach.
        attached = [name for name in PRESENT_DEPENDENCIES if getattr(self.presents, name) is not None]
        items = self.presents.item_catalog
        variants = (None,) if items is None else items.replayable_content_versions()
        return any(self._content(frozenset(absent), version) == content
                   for size in range(len(attached) + 1) for absent in combinations(attached, size)
                   for version in variants)

    def _undeliverable(self, rewards):
        try:
            rows = self.presents.preview(rewards, 0)
            # The widest reply carries the largest present identities a mailbox can still assign.
            state = {"bootstrap_schema": 1, "tables": {"presents": []}, "presents_schema": 1,
                     "last_present_id": INT32_MAX - len(rows)}
            updated, granted = self.presents.enqueue(state, rewards)
            self._granted(PlayerProfile("", "", _EXCHANGE_KEY_SAMPLE, updated, 0), updated,
                          [row["PresentID"] for row in granted])
        except ProtocolError as error:
            return error.code
        return None

    def _unavailable_codes(self):
        verdicts, unavailable = {}, {}
        for entry in self.rules.codes:
            # Batches of codes usually share one reward list, and the verdict depends on the rewards alone.
            rewards = tuple(tuple(sorted(reward.items())) for reward in entry.rewards)
            if rewards not in verdicts:
                verdicts[rewards] = self._undeliverable(entry.reward_list())
            if verdicts[rewards] is not None:
                unavailable[entry.code] = verdicts[rewards]
        return MappingProxyType(unavailable)

    def handlers(self):
        return {OPERATION: self.redeem}

    def initialize(self, state):
        self._ready()
        read_redemptions(state)
        # Startup has no stored revision to bound receipts, so the store's ceiling stands in for it.
        self._ledger.read(state, MAX_REVISION)
        return deepcopy(state)

    def status(self):
        try:
            self._ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        with self._lock:
            answers = {name: self._answers[status] for status, name in STATUS_NAMES.items()}
            kept_unused = dict(self._kept_unused)
        return {"ready": ready, "error": error, "implemented_operations": [OPERATION],
                "content_version": self.content_version, "rules_version": self.rules.content_version,
                "code_count": len(self.rules.codes), "unavailable_codes": dict(self.unavailable_codes),
                "statuses": {name: status for status, name in STATUS_NAMES.items()}, "native_tested": False,
                "default_policy": "Codes outside the local table answer -10; no official code table is imitated",
                "delivery_policy": "Present box only: 2 with the new mail rows, never direct barracks credit (1 or -12)",
                "limit_policy": "Each code redeems up to its limit (default 1), then answers -13, or -11 when single_use",
                "window_policy": "Optional inclusive window of JST 04:00 business days; outside it the code answers -10",
                "undeliverable_policy": ("Rewards that cannot reach the present box (full box, exhausted identities, "
                                         "oversized listing or reply, full journal) keep the code unused and answer 0"),
                "replay_policy": ("Exact retries replay their receipt, also once presents dependencies attach later; "
                                  "repeated requests with a stale or evicted receipt answer the code's current status "
                                  "without redeeming again"),
                "request_policy": "Only the native SC shape (CE grouped by five, APPEND 0 or 1) is accepted; others answer 400",
                "refusals": "Logged, because the native client resends a refused request until it receives a 200",
                "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "request_order": REQUEST_ORDER, "response_limit": RESPONSE_LIMIT,
                "answers": answers, "kept_unused": kept_unused}

    @staticmethod
    def _request(envelope):
        request = registered(envelope, {OPERATION}, {"SC"}, {"SC"})
        container = fields(request["SC"], {"CE", "APPEND"}, {"CE", "APPEND"})
        entered, append = scalar(container["CE"], "S"), scalar(container["APPEND"], "I")
        code = entered.replace("-", "")
        if append not in APPEND_VALUES or not CODE_PATTERN.fullmatch(code) or entered != entered_form(code):
            raise ProtocolError("serial_code_request_invalid", "Serial code request does not match its native format")
        return code, append

    @staticmethod
    def _settled(entry, record, day, repeated):
        if entry is None or not entry.open_on(day):
            return INVALID_CODE
        if repeated or (record is not None and record["count"] >= entry.limit):
            return USED_CODE if entry.single_use else USED_UNIQUE_CODE
        return None

    def _reply(self, profile, status, *groups):
        response = dumps(Container(children=(self.bootstrap._system(profile), _result(status), *groups)))
        encode_response(self._profile, response)
        return response

    def _granted(self, profile, state, present_ids):
        response = self._reply(profile, PRESENTS_ADDED, self.presents.response(state, present_ids))
        if len(response) > RESPONSE_LIMIT:
            raise ProtocolError("serial_code_response_capacity", "The serial code reply exceeds its receipt limit", 503)
        return response

    def _count(self, status):
        with self._lock:
            self._answers[status] += 1

    def _answer(self, profile, status):
        response = self._reply(profile, status)
        self._count(status)
        return response

    def _keep_unused(self, profile, code, reason):
        response = self._answer(profile, SILENT)
        with self._lock:
            self._kept_unused[reason] += 1
        log.warning("Serial code %d kept %r unused because of %s; the native client shows no dialog",
                    OPERATION, code, reason)
        return response

    def _replay(self, profile, cached, code, append):
        response = cached["response"].encode("utf-8")
        try:
            consistent = (cached["state"] == _state_digest(profile.state) and cached["code"] == code
                          and cached["append"] == append
                          and self._granted(profile, profile.state, cached["present_ids"]) == response)
        except ProtocolError:
            consistent = False
        if not consistent:
            self._ledger.invalid()
        self._count(PRESENTS_ADDED)
        return response

    def _deliver(self, state, entry, day):
        updated, granted = self.presents.enqueue(state, entry.reward_list())
        present_ids = [row["PresentID"] for row in granted]
        return _redeemed(updated, entry.code, day, present_ids), present_ids

    def redeem(self, envelope, store):
        try:
            return self._redeem(envelope, store)
        except ProtocolError as error:
            log.warning("Serial code %d refused with %s; the native client %s", OPERATION, error.code, _REFUSAL_AFTERMATH)
            raise

    def _redeem(self, envelope, store):
        self._ready()
        code, append = self._request(envelope)
        key = hashlib.sha256(f"{OPERATION}:{envelope.profile.route}:{envelope.profile.st}:".encode()
                             + envelope.xml).hexdigest()
        entry = self.rules.get(code)
        day = None if entry is None else _business_day(self.bootstrap.clock())
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            history = self._ledger.read(profile.state, profile.revision)
            redemptions = read_redemptions(profile.state)
            cached = self._ledger.receipt(history, key)
            if cached is not None and cached["revision"] == profile.revision and self._replayable(cached["content"]):
                return self._replay(profile, cached, code, append)
            repeated = cached is not None or self._ledger.processed(history, key)
            status = self._settled(entry, redemptions.get(code), day, repeated)
            if status is not None:
                if repeated:
                    log.warning("Serial code %d answered %d to a repeated request for %r instead of redeeming it again",
                                OPERATION, status, code)
                return self._answer(profile, status)
            reason = self.unavailable_codes.get(code)
            if reason is None:
                try:
                    updated, present_ids = self._deliver(profile.state, entry, day)
                    response = self._granted(profile, updated, present_ids)
                except ProtocolError as error:
                    if error.code not in UNDELIVERABLE:
                        raise
                    reason = error.code
            if reason is not None:
                return self._keep_unused(profile, code, reason)
            self._ledger.record(updated, history, {"request": key, "content": self.content_version,
                                                   "state": _state_digest(updated), "revision": profile.revision + 1,
                                                   "response": response.decode("utf-8"), "code": code,
                                                   "append": append, "present_ids": present_ids})
            try:
                store.update_state(updated, profile.revision)
            except ValueError:
                continue
            self._count(PRESENTS_ADDED)
            return response
        raise ProtocolError("profile_changed", "Player state changed while redeeming a serial code", 409)
