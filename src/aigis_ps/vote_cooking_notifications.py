from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re

from .energy_clock import instant
from .errors import ProtocolError
from .vote_cooking_service import DELIVERED, MAX_GROUPS, validate_vote_cooking_grants


_HASH = re.compile(r"[a-f0-9]{64}\Z")
_FIELDS = frozenset(("grant", "notified_at", "source"))


def _invalid(message="Stored vote cooking notifications are invalid"):
    raise ProtocolError("vote_cooking_notifications_invalid", message, 503)


def _time(value):
    try:
        return instant(value).astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (TypeError, ValueError, OverflowError, ProtocolError) as error:
        raise ProtocolError("vote_cooking_notification_clock_invalid", "Cooking notifications require a valid instant") from error


def _saved_time(value):
    try:
        if type(value) is not str or len(value) != 32 or _time(datetime.fromisoformat(value)) != value:
            raise ValueError("Noncanonical notification time")
    except (ValueError, TypeError, OverflowError, ProtocolError):
        _invalid("Cooking notification timestamps require canonical UTC instants")
    return value


class VoteCookingNotifications:
    def __init__(self, cooking):
        self.cooking = cooking
        self.presents = cooking.presents
        self.content_version = self._content()
        self._snapshot = (self.cooking, self.presents, self.content_version)
        self._ready()

    def _content(self):
        value = {"schema": 1, "cooking": self.cooking.content_version,
            "delivery": "existing_atomic_cooking_mail", "notification": "one_delivered_group_per_116",
            "selection": "delivered_at_then_group_then_item", "legacy": "delivered_mail_can_notify_once",
            "empty": "typed_VoteGiftParam", "proof": "permanent_delivery_grant"}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _ready(self):
        if (any(left is not right for left, right in zip(
                (self.cooking, self.presents, self.content_version), self._snapshot))
                or self.presents is not self.cooking.presents or self._content() != self.content_version):
            raise ProtocolError("vote_cooking_notifications_changed", "Cooking notification dependencies changed", 503)
        self.cooking._ready()

    def ledger(self, state):
        self._ready()
        self.cooking.ledger(state)
        if "vote_cooking_notifications" not in state:
            return {}
        journal = state["vote_cooking_notifications"]
        if ("vote_cooking" not in state or not isinstance(journal, dict)
                or set(journal) != {"schema_version", "last_time", "records"}
                or type(journal["schema_version"]) is not int or journal["schema_version"] != 1
                or not isinstance(journal["records"], dict) or not 0 < len(journal["records"]) <= MAX_GROUPS):
            _invalid()
        last = _saved_time(journal["last_time"])
        identifiers = set()
        latest = None
        for item, record in journal["records"].items():
            if (not isinstance(record, dict) or set(record) != _FIELDS
                    or type(record["source"]) is not str or not _HASH.fullmatch(record["source"])):
                _invalid()
            grant = record["grant"]
            ids = validate_vote_cooking_grants([grant])
            if item != grant["item_id"] or identifiers.intersection(ids):
                _invalid("Cooking notifications reuse a group or mail identity")
            self.cooking.validate_receipt(state, [grant])
            stamp = _saved_time(record["notified_at"])
            delivered = state["vote_cooking"]["groups"][item]["delivered_at"]
            if not delivered <= stamp <= last:
                _invalid("Cooking notification precedes its mail delivery")
            latest = stamp if latest is None else max(latest, stamp)
            identifiers.update(ids)
        if latest != last:
            _invalid("Cooking notification watermark differs from its latest record")
        return journal["records"]

    def initialize(self, state):
        self.ledger(state)
        return deepcopy(state)

    def settle(self, state, now):
        records = self.ledger(state)
        stamp = _time(now)
        if records and stamp < state["vote_cooking_notifications"]["last_time"]:
            raise ProtocolError("vote_cooking_notification_clock_reversed", "Cooking notification clock moved backwards", 409)
        updated, deliveries = self.cooking.settle(state, now)
        self.cooking.validate_receipt(updated, deliveries)
        if len(deliveries) > 1 or updated.get("vote_cooking_notifications") != state.get("vote_cooking_notifications"):
            _invalid("Cooking delivery changed notification ownership")
        groups = updated.get("vote_cooking", {}).get("groups", {})
        pending = [(group["delivered_at"], group["group_id"], item) for item, group in groups.items()
                   if group["status"] == DELIVERED and item not in records]
        notices = []
        if pending:
            item = min(pending)[2]
            group = groups[item]
            grant = {"item_id": item, "group_id": group["group_id"], "definition": group["definition"],
                "card_id": group["winner"], "count": group["copies"], "receive_text_id": group["receive_text_id"],
                "mail_ids": list(group["mail_ids"])}
            self.cooking.validate_receipt(updated, [grant])
            if len(records) >= MAX_GROUPS:
                raise ProtocolError("vote_cooking_notification_capacity", "Cooking notification history is full", 409)
            ledger = updated.setdefault("vote_cooking_notifications", {"schema_version": 1, "last_time": stamp, "records": {}})
            ledger["records"][item] = {"grant": deepcopy(grant), "notified_at": stamp, "source": self.content_version}
            ledger["last_time"] = stamp
            notices.append(grant)
        self.validate_receipt(updated, deliveries, notices)
        return updated, deliveries, notices

    def validate_receipt(self, state, deliveries, notices):
        records = self.ledger(state)
        validate_vote_cooking_grants(deliveries)
        validate_vote_cooking_grants(notices)
        if len(deliveries) > 1 or len(notices) > 1:
            _invalid("A cooking notification operation can process only one group")
        self.cooking.validate_receipt(state, deliveries)
        for grant in notices:
            saved = records.get(grant["item_id"])
            if saved is None or saved["grant"] != grant:
                _invalid("Cooking notice lost its permanent notification proof")

    def response(self, notices):
        self._ready()
        validate_vote_cooking_grants(notices)
        if len(notices) > 1:
            _invalid("A cooking response can notify only one group")
        return self.cooking.response(notices)

    def status(self):
        self._ready()
        return {"ready": True, "content_version": self.content_version,
            "cooking_content_version": self.cooking.content_version,
            "delivery": "One group through the shared cooking delivery service",
            "notification": "116, one previously delivered group per request",
            "legacy": "Previously delivered or claimed cooking mail can notify once", "native_tested": False}
