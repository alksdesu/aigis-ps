from __future__ import annotations

from copy import deepcopy
import re

from .errors import ProtocolError
from .native_numbers import int32


HISTORY_LIMIT = 16
PROCESSED_LIMIT = 256
SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
REQUEST_ORDER = "Exact payload digests; the client CT is wall-clock time, not an order"
_CLIENT_TIME = "client_time"
_FIELDS = frozenset(("schema_version", "receipts", "processed_keys"))
_LEGACY_FIELDS = frozenset(("schema_version", "receipts", "last_client_time", "seen_keys"))
_RECEIPT_FIELDS = frozenset(("request", "content", "revision", "response"))
_DIGEST_FIELDS = ("request", "content", "state")
_VERIFIED_FIELDS = _RECEIPT_FIELDS | frozenset(_DIGEST_FIELDS)
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def _is_digest(value) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


class RequestLedger:
    def __init__(self, namespace: str, error_code: str, message: str, *, fields, response_limit: int, check=None,
                 legacy_defaults=None, legacy_nesting=None):
        fields = frozenset(fields)
        if not _RECEIPT_FIELDS <= fields or _CLIENT_TIME in fields:
            raise ValueError("Receipts require request, content, revision and response without a client clock")
        legacy_defaults = dict(legacy_defaults or {})
        if not set(legacy_defaults) <= fields - _VERIFIED_FIELDS:
            raise ValueError("Legacy receipt defaults may only fill module receipt fields")
        legacy_nesting = dict(legacy_nesting or {})
        nested = [name for names in legacy_nesting.values() for name in names]
        # Ordered tuples keep the declared order and stop a bare string from nesting its characters.
        if (not all(isinstance(names, tuple) for names in legacy_nesting.values())
                or not all(type(name) is str for name in nested)
                or not set(legacy_nesting) <= fields - _VERIFIED_FIELDS - set(legacy_defaults)
                or len(set(nested)) != len(nested) or not (fields | {_CLIENT_TIME}).isdisjoint(nested)):
            raise ValueError("Legacy receipt nesting may only gather distinct flat names into module receipt fields")
        self.namespace, self.response_limit = namespace, response_limit
        self._error = error_code, message
        self._fields, self._check = fields, check
        self._legacy_defaults = legacy_defaults
        self._legacy_nesting, self._nested_names = legacy_nesting, frozenset(nested)
        self._digests = tuple(name for name in _DIGEST_FIELDS if name in fields)

    def read(self, state: dict, revision: int) -> dict:
        if self.namespace not in state:
            return {"schema_version": SCHEMA_VERSION, "receipts": [], "processed_keys": []}
        history = state[self.namespace]
        if isinstance(history, dict) and set(history) == _LEGACY_FIELDS:
            history = self._upgrade(history)
        self._validate(history, revision)
        return deepcopy(history)

    @staticmethod
    def receipt(history: dict, key: str) -> dict | None:
        return next((entry for entry in history["receipts"] if entry["request"] == key), None)

    @staticmethod
    def processed(history: dict, key: str) -> bool:
        return key in history["processed_keys"]

    def record(self, state: dict, history: dict, receipt: dict) -> None:
        updated = {"schema_version": SCHEMA_VERSION,
                   "receipts": [*history["receipts"], receipt][-HISTORY_LIMIT:],
                   "processed_keys": [*history["processed_keys"], receipt["request"]][-PROCESSED_LIMIT:]}
        self._validate(updated, receipt["revision"])
        state[self.namespace] = updated

    def invalid(self):
        raise ProtocolError(*self._error, 503)

    def _upgrade(self, history: dict) -> dict:
        receipts, seen, last = history["receipts"], history["seen_keys"], history["last_client_time"]
        if (type(history["schema_version"]) is not int or history["schema_version"] != LEGACY_SCHEMA_VERSION
                or not isinstance(receipts, list) or not isinstance(seen, list) or len(seen) > PROCESSED_LIMIT
                or (last is None and seen) or (last is not None and not int32(last))
                or any(not isinstance(entry, dict) or not int32(entry.get(_CLIENT_TIME)) for entry in receipts)):
            self.invalid()
        receipts = [self._legacy_receipt(entry) for entry in receipts]
        keys = [entry.get("request") for entry in receipts]
        # Same-second digests whose receipts were already evicted are older than every retained receipt.
        evicted = [key for key in seen if key not in keys]
        return {"schema_version": SCHEMA_VERSION, "receipts": receipts,
                "processed_keys": [*evicted, *keys][-PROCESSED_LIMIT:]}

    def _legacy_receipt(self, entry: dict) -> dict:
        # Schema 1 receipts kept optional module fields flat beside the common ones.
        common = {name: value for name, value in entry.items()
                  if name != _CLIENT_TIME and name not in self._nested_names}
        nested = {field: {name: entry[name] for name in names if name in entry}
                  for field, names in self._legacy_nesting.items()}
        # Schema 1 receipts predate later module fields, so they read as the values their writer implied.
        return {**self._legacy_defaults, **common, **nested}

    def _validate(self, history, revision: int) -> None:
        if (not isinstance(history, dict) or set(history) != _FIELDS
                or type(history["schema_version"]) is not int or history["schema_version"] != SCHEMA_VERSION):
            self.invalid()
        receipts, processed = history["receipts"], history["processed_keys"]
        if (not isinstance(receipts, list) or len(receipts) > HISTORY_LIMIT
                or not isinstance(processed, list) or len(processed) > PROCESSED_LIMIT
                or not all(_is_digest(key) for key in processed) or len(set(processed)) != len(processed)):
            self.invalid()
        previous = 0
        for entry in receipts:
            if (not isinstance(entry, dict) or set(entry) != self._fields
                    or not all(_is_digest(entry[name]) for name in self._digests)
                    or type(entry["revision"]) is not int or not previous < entry["revision"] <= revision
                    or not self._fits(entry["response"]) or not self._extra_valid(entry)):
                self.invalid()
            previous = entry["revision"]
        retained = [entry["request"] for entry in receipts]
        if len(retained) > len(processed) or processed[len(processed) - len(retained):] != retained:
            self.invalid()

    def _fits(self, response) -> bool:
        try:
            return type(response) is str and 0 < len(response.encode("utf-8")) <= self.response_limit
        except UnicodeError:
            return False

    def _extra_valid(self, entry: dict) -> bool:
        try:
            return self._check is None or self._check(entry) is True
        except (TypeError, ValueError, UnicodeError):
            return False
