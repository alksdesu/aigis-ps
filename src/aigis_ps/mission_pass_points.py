from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from types import MappingProxyType

from .energy_clock import instant
from .errors import ProtocolError
from .mission_catalog import MissionDefinition
from .native_numbers import INT32_MAX, int32


MAX_DEFINITIONS = 65535
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_AMOUNTS = frozenset(("requested", "credited", "before", "after", "capped", "inactive"))


def _version(policy, catalog_version):
    return hashlib.sha256(json.dumps({**policy, "catalog": catalog_version}, sort_keys=True).encode()).hexdigest()


def _integer(value):
    return int32(value) and value >= 0


def _invalid_definition():
    raise ProtocolError("mission_pass_definition_invalid", "Mission EP requires complete, bounded pass rewards", 503)


def _invalid_receipt():
    raise ProtocolError("mission_pass_receipt_invalid", "Mission EP receipt does not match its reward or inventory", 503)


class MissionPassPoints:
    def __init__(self, event_passes):
        self.event_passes = event_passes
        try:
            self.catalog = event_passes.catalog
            self.possessions = event_passes.possessions
            self._catalog_version = self.catalog.content_version
            self._item_version = self.possessions.asset_sha256
            if any(type(value) is not str or not _HASH.fullmatch(value)
                   for value in (self._catalog_version, self._item_version)):
                raise ValueError("EP dependencies require stable fingerprints")
            entries = tuple(self.catalog.passes)
            self._entries = MappingProxyType({entry.item_id: entry for entry in entries})
            if not entries or len(entries) != len(self._entries):
                raise ValueError("EP requires distinct pass definitions")
            stacks = {identifier: self.possessions.stack(identifier) for identifier in self._entries}
            if any(not _integer(value) or value == 0 for value in stacks.values()):
                raise ValueError("EP requires positive inventory limits")
            self._stacks = MappingProxyType(stacks)
            self._ready()
            policy = {"version": 1, "items": self._item_version, "stacks": stacks, "timing": "claim_only",
                      "inactive": "discard", "overflow": "saturate_without_debt", "old_overflow": "preserve"}
            self._content_version = _version(policy, self._catalog_version)
            self._legacy_window_content_version = _version(policy, self.catalog.legacy_window_content_version)
        except (AttributeError, KeyError, TypeError, ValueError, ProtocolError) as error:
            raise ProtocolError("mission_pass_dependencies_invalid", "Verified mission EP dependencies are unavailable", 503) from error

    @property
    def content_version(self):
        return self._content_version

    @property
    def legacy_window_content_version(self):
        return self._legacy_window_content_version

    def _ready(self):
        try:
            self.catalog._ready()
            # Reads by_id because get() would repeat the catalog check above once per pass.
            if (self.event_passes.catalog is not self.catalog or self.event_passes.possessions is not self.possessions
                    or self.catalog.content_version != self._catalog_version
                    or self.possessions.asset_sha256 != self._item_version
                    or not self.possessions.is_ready()
                    or tuple(self.catalog.passes) != tuple(self._entries.values())
                    or any(self.catalog.by_id.get(identifier) != entry
                           or self.possessions.stack(identifier) != self._stacks[identifier]
                           for identifier, entry in self._entries.items())):
                raise ValueError("Mission EP dependencies changed")
        except (AttributeError, KeyError, TypeError, ValueError, ProtocolError) as error:
            raise ProtocolError("mission_pass_dependencies_changed", "Mission EP definitions changed after setup", 503) from error

    def status(self):
        try:
            self._ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "content_version": self.content_version,
                "pass_count": len(self._entries), "stack_limits": dict(self._stacks),
                "timing": "Successful mission claim only; local policy",
                "overflow": "Saturate active pass inventory; discard excess and inactive rewards without debt",
                "old_overflow": "Preserve existing counts above the resource limit"}

    def _totals(self, definitions):
        if not isinstance(definitions, (list, tuple)) or len(definitions) > MAX_DEFINITIONS:
            _invalid_definition()
        totals, seen = {}, set()
        for definition in definitions:
            if (not isinstance(definition, MissionDefinition) or not _integer(definition.condition_id)
                    or definition.condition_id == 0 or definition.condition_id in seen):
                _invalid_definition()
            seen.add(definition.condition_id)
            identifier, amount = definition.event_pass_item_id, definition.event_pass_point
            if type(identifier) is not str or not _integer(amount):
                _invalid_definition()
            if not identifier and amount == 0:
                continue
            if not identifier or amount == 0:
                _invalid_definition()
            if identifier not in self._entries:
                raise ProtocolError("mission_pass_unknown", "Mission EP refers to an unknown ordinary pass", 501)
            total = totals.get(identifier, 0) + amount
            if total > INT32_MAX:
                raise ProtocolError("mission_pass_points_overflow", "Mission EP total exceeds int32", 503)
            totals[identifier] = total
        return totals

    def award(self, state, definitions, now):
        self._ready()
        totals = self._totals(definitions)
        current = instant(now)
        counts = self.possessions.counts(state)
        audit, grants = [], {}
        for identifier, requested in sorted(totals.items()):
            before = counts.get(identifier, 0)
            active = self.catalog.is_eligible(self._entries[identifier], current)
            credited = min(requested, max(0, self._stacks[identifier] - before)) if active else 0
            inactive = 0 if active else requested
            audit.append({"pass_id": identifier, "requested": requested, "credited": credited,
                          "before": before, "after": before + credited,
                          "capped": requested - credited - inactive, "inactive": inactive})
            if credited:
                grants[identifier] = credited
        updated = self.possessions.credit(state, grants) if grants else deepcopy(state)
        self.validate_receipt(updated, definitions, audit)
        return updated, audit

    def validate_receipt(self, state, definitions, audit):
        self._ready()
        totals = self._totals(definitions)
        counts = self.possessions.counts(state)
        if not isinstance(audit, list) or len(audit) != len(totals):
            _invalid_receipt()
        seen = set()
        for entry in audit:
            if (not isinstance(entry, dict) or set(entry) != {"pass_id", *_AMOUNTS}
                    or type(entry["pass_id"]) is not str or entry["pass_id"] not in totals
                    or entry["pass_id"] in seen or any(not _integer(entry[name]) for name in _AMOUNTS)):
                _invalid_receipt()
            identifier = entry["pass_id"]
            seen.add(identifier)
            requested, before = totals[identifier], entry["before"]
            inactive = entry["inactive"]
            credited = min(requested, max(0, self._stacks[identifier] - before)) if inactive == 0 else 0
            if (entry["requested"] != requested or inactive not in (0, requested)
                    or entry["credited"] != credited or entry["after"] != before + credited
                    or entry["capped"] != requested - credited - inactive
                    or counts.get(identifier, 0) != entry["after"]):
                _invalid_receipt()
