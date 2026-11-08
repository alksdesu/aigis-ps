from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from .alxml import Column, Container
from .errors import ProtocolError, caught
from .native_numbers import int32
from .startup_diagnostics import StartupDiagnostics


MAX_FAME_ROWS = 65535
COUNT_FIELDS = tuple(f"Count{stage}" for stage in range(5))
FIELDS = ("CardId", *COUNT_FIELDS)
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def read_fame(state, known_card_ids=None):
    if (not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int
            or state["bootstrap_schema"] != 1 or not isinstance(state.get("tables"), dict)):
        raise ProtocolError("fame_state_invalid", "Fame depository requires supported player tables", 503)
    tables = state["tables"]
    rows = tables.get("fame_depository", [])
    schema = state.get("fame_depository_schema")
    marked = "fame_depository_schema" in state
    if (not isinstance(rows, list) or len(rows) > MAX_FAME_ROWS
            or (marked and (type(schema) is not int or schema != 1))
            or (rows and schema != 1) or (marked and "fame_depository" not in tables)):
        raise ProtocolError("fame_state_invalid", "Fame depository schema or collection is invalid", 503)
    known = frozenset(known_card_ids) if known_card_ids is not None else None
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != set(FIELDS)
                or any(not int32(value) for value in row.values()) or row["CardId"] <= 0
                or any(row[name] < 0 for name in COUNT_FIELDS) or row["CardId"] in seen
                or (known is not None and row["CardId"] not in known)):
            raise ProtocolError("fame_state_invalid", "Fame depository rows require known cards and five nonnegative counts", 503)
        seen.add(row["CardId"])
    return deepcopy(rows)


def owned_fame_cards(state, known_card_ids=None):
    return frozenset(row["CardId"] for row in read_fame(state, known_card_ids)
                     if any(row[name] > 0 for name in COUNT_FIELDS))


class FameRepository:
    def __init__(self, catalog, *, diagnostics: StartupDiagnostics | None = None):
        if diagnostics is not None and not isinstance(diagnostics, StartupDiagnostics):
            raise ValueError("Startup diagnostics must be a StartupDiagnostics registry")
        self.catalog, self.diagnostics = catalog, diagnostics
        self._card_ids = frozenset(catalog.card_ids)
        self._limit = catalog.deposit_limit
        self._catalog_version = catalog.content_version
        if (not self._card_ids or len(self._card_ids) > MAX_FAME_ROWS
                or any(not int32(card_id) or card_id <= 0 for card_id in self._card_ids)
                or not int32(self._limit) or self._limit <= 0
                or not isinstance(self._catalog_version, str) or not _HASH.fullmatch(self._catalog_version)):
            raise ProtocolError("fame_repository_invalid", "Fame repository requires verified card definitions", 503)
        self.content_version = self.content_version_for(self._catalog_version)

    def content_version_for(self, catalog_version):
        return hashlib.sha256(json.dumps({"schema": 1, "catalog": catalog_version,
            "limit": self._limit, "capacity": "per_card_and_stage", "zero_rows": "preserved"},
            sort_keys=True).encode()).hexdigest()

    def replayable_content_versions(self):
        return frozenset(map(self.content_version_for, self.catalog.replayable_content_versions()))

    def _ready(self):
        if (self.catalog.content_version != self._catalog_version or self.catalog.deposit_limit != self._limit
                or frozenset(self.catalog.card_ids) != self._card_ids or not self.catalog.is_ready()):
            raise ProtocolError("fame_repository_changed", "Fame repository definitions changed after setup", 503)

    def is_ready(self):
        return caught(self._ready) is None

    def status(self):
        failure = caught(self._ready)
        return {"ready": failure is None, "error": None if failure is None else failure.code,
                "content_version": self.content_version,
                "deposit_limit": self._limit, "capacity_scope": "per_card_and_stage",
                "legacy_over_limit": "readable_and_withdrawable", "zero_rows": "preserved"}

    def read(self, state):
        self._ready()
        return read_fame(state, self._card_ids)

    def initialize(self, state):
        self.read(state)
        return deepcopy(state)

    def owned_card_ids(self, state):
        return frozenset(row["CardId"] for row in self.read(state) if any(row[name] > 0 for name in COUNT_FIELDS))

    def response(self, state, card_ids=None):
        try:
            self._ready()
        except ProtocolError as error:
            if card_ids is not None or self.diagnostics is None:
                raise
            # FameDepository is the saved stock, so 137 serves it instead of keeping the native startup loop resending.
            self.diagnostics.degrade(error)
        rows = read_fame(state, self._card_ids)
        if card_ids is not None:
            if (not isinstance(card_ids, (tuple, list)) or len(card_ids) > MAX_FAME_ROWS
                    or any(not int32(card_id) or card_id not in self._card_ids for card_id in card_ids)
                    or len(set(card_ids)) != len(card_ids)):
                raise ProtocolError("fame_selection_invalid", "Fame response requires distinct known card IDs")
            indexed = {row["CardId"]: row for row in rows}
            rows = [indexed.get(card_id, {"CardId": card_id, **dict.fromkeys(COUNT_FIELDS, 0)}) for card_id in card_ids]
        if not rows:
            return Container("FameDepository")
        return Container("FameDepository", tuple(Column(name, "I", tuple(row[name] for row in rows)) for name in FIELDS))

    def adjust(self, state, changes):
        rows = self.read(state)
        if not isinstance(changes, dict) or len(changes) > len(self._card_ids) * len(COUNT_FIELDS):
            raise ProtocolError("fame_changes_invalid", "Fame adjustments require a bounded mapping")
        indexed = {row["CardId"]: row for row in rows}
        for key, delta in changes.items():
            if (not isinstance(key, tuple) or len(key) != 2 or not int32(key[0]) or key[0] not in self._card_ids
                    or not int32(key[1]) or not 0 <= key[1] < 5 or not int32(delta) or delta == 0):
                raise ProtocolError("fame_changes_invalid", "Fame adjustments require a known card, stage and nonzero count")
            card_id, stage = key
            row = indexed.setdefault(card_id, {"CardId": card_id, **dict.fromkeys(COUNT_FIELDS, 0)})
            value = row[f"Count{stage}"] + delta
            if value < 0:
                raise ProtocolError("fame_stock_insufficient", "Fame withdrawal exceeds the stored stage quantity", 409)
            if not int32(value) or (delta > 0 and value > self._limit):
                raise ProtocolError("fame_stock_capacity", "Fame deposit exceeds the per-stage capacity", 409)
            row[f"Count{stage}"] = value
        updated = deepcopy(state)
        if changes:
            updated["fame_depository_schema"] = 1
            updated["tables"]["fame_depository"] = list(indexed.values())
        return updated
