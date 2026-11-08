from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import re
from types import MappingProxyType

from .alxml import Column, Container, dumps
from .errors import ProtocolError
from .mission_catalog import MissionDefinition, _digest
from .mission_state import JOURNAL_KEY as MISSION_JOURNAL
from .native_numbers import INT32_MAX, int32
from .possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES


LEDGER_KEY = "mission_ep_bindings"
MAX_BINDINGS = 65535
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[1-9][0-9]{0,9}\Z")
_MALFORMED = (AttributeError, KeyError, TypeError, ValueError, UnicodeError)


def _integer(value, minimum=0):
    return int32(value) and value >= minimum


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _binding(item_id, point, maximum):
    if (type(item_id) is not str or len(item_id.encode("utf-8")) > maximum
            or not _integer(point) or bool(item_id) != (point > 0)):
        raise ValueError("EP bindings require a matched item and positive points, or neither")
    dumps(Container(children=(Column("ItemID", "S", (item_id,)),)))


def _values(definition):
    if not isinstance(definition, MissionDefinition):
        raise ValueError("EP bindings require verified mission definitions")
    result = asdict(definition)
    version = result.pop("definition_version")
    if not _hash(version) or _digest(result) != version:
        raise ValueError("Mission definition fingerprint is inconsistent")
    _binding(definition.event_pass_item_id, definition.event_pass_point, STORED_ITEM_NAME_BYTES)
    return result


def _marker(state):
    if not isinstance(state, dict):
        raise ValueError("EP bindings require a state object")
    if LEDGER_KEY not in state:
        return None
    marker = state[LEDGER_KEY]
    if (not isinstance(marker, dict) or set(marker) != {"schema_version", "bindings"}
            or type(marker["schema_version"]) is not int or marker["schema_version"] != 1
            or not isinstance(marker["bindings"], dict) or len(marker["bindings"]) > MAX_BINDINGS):
        raise ValueError("EP binding history is invalid")
    for key, entry in marker["bindings"].items():
        if (type(key) is not str or not _ID.fullmatch(key) or int(key) > INT32_MAX
                or not isinstance(entry, dict) or set(entry) != {"base_version", "item_id", "point"}
                or not _hash(entry["base_version"])):
            raise ValueError("EP binding entry is invalid")
        _binding(entry["item_id"], entry["point"], STORED_ITEM_NAME_BYTES)
    return deepcopy(marker)


def _history(read, state):
    try:
        return read(state)
    except _MALFORMED as error:
        raise ProtocolError("mission_ep_bindings_invalid", "Mission EP binding history is invalid", 503) from error


def read_bindings(state: dict) -> dict | None:
    return _history(_marker, state)


class MissionEpBindings:
    def __init__(self, base_catalog, effective_catalog):
        self._base_catalog, self._effective_catalog = base_catalog, effective_catalog
        try:
            self._versions = (base_catalog.content_version, effective_catalog.content_version)
            if (any(not _hash(version) for version in self._versions)
                    or set(base_catalog.by_id) != set(effective_catalog.by_id)
                    or len(base_catalog.by_id) > MAX_BINDINGS
                    or tuple(base_catalog.sequence) != tuple(effective_catalog.sequence)
                    or dict(base_catalog.groups) != dict(effective_catalog.groups)):
                raise ValueError("EP overlay changed mission identities, groups or order")
            bases, effective, changed = {}, {}, []
            for identifier, definition in base_catalog.by_id.items():
                if not _integer(identifier, 1) or identifier != definition.condition_id:
                    raise ValueError("Mission identity does not match its index")
                replacement = effective_catalog.by_id[identifier]
                original_values, new_values = _values(definition), _values(replacement)
                base_semantics, new_semantics = dict(original_values), dict(new_values)
                for field in ("event_pass_item_id", "event_pass_point"):
                    base_semantics.pop(field)
                    new_semantics.pop(field)
                if base_semantics != new_semantics:
                    raise ValueError("EP overlay changed non-EP mission semantics")
                if original_values != new_values:
                    if identifier == 1031:
                        raise ValueError("EP overlay must preserve the pinned DGP mission")
                    # Overlay items are written into new marker entries, so they must fit the native item cell.
                    _binding(replacement.event_pass_item_id, replacement.event_pass_point, MAX_ITEM_NAME_BYTES)
                    changed.append(identifier)
                bases[identifier], effective[identifier] = definition, replacement
            self._base = MappingProxyType(bases)
            self._effective = MappingProxyType(effective)
            self.changed_ids = tuple(sorted(changed))
            content = {"schema": 1, "base": {key: value.definition_version for key, value in bases.items()},
                "effective": {key: value.definition_version for key, value in effective.items()},
                "policy": "recompute_previous_binding_preserve_claimed_and_unproven"}
            self.content_version = _digest({**content, "catalogs": self._versions})
            self.legacy_window_content_version = _digest({**content, "catalogs": (
                base_catalog.legacy_window_content_version, effective_catalog.legacy_window_content_version)})
        except _MALFORMED as error:
            raise ProtocolError("mission_ep_catalog_invalid", "Mission EP overlay differs from its verified base", 503) from error

    def _ready(self):
        if (self._base_catalog.content_version, self._effective_catalog.content_version) != self._versions:
            raise ProtocolError("mission_ep_catalog_changed", "Mission EP catalogs changed after setup", 503)

    def prepare(self, state):
        self._ready()
        return _history(self._migrate, state)

    def _migrate(self, state):
        marker = _marker(state)
        if marker is None and not self.changed_ids:
            return deepcopy(state)
        result = deepcopy(state)
        marker = marker or {"schema_version": 1, "bindings": {}}
        proofs = {}
        targets = set(self.changed_ids) | {int(key) for key in marker["bindings"]}
        for identifier in targets & self._base.keys():
            base, effective = self._base[identifier], self._effective[identifier]
            accepted = {base.definition_version, effective.definition_version}
            previous = marker["bindings"].get(str(identifier))
            if previous is not None and previous["base_version"] == base.definition_version:
                values = asdict(base)
                values.pop("definition_version")
                values.update(event_pass_item_id=previous["item_id"], event_pass_point=previous["point"])
                accepted.add(_digest(values))
            proofs[identifier] = accepted
            marker["bindings"][str(identifier)] = {"base_version": base.definition_version,
                "item_id": effective.event_pass_item_id, "point": effective.event_pass_point}
        if len(marker["bindings"]) > MAX_BINDINGS:
            raise ValueError("EP binding history exceeds its capacity")
        if MISSION_JOURNAL in result:
            missions = result[MISSION_JOURNAL]
            if (not isinstance(missions, dict) or type(missions.get("schema_version")) is not int
                    or missions["schema_version"] != 1 or not isinstance(missions.get("entries"), list)
                    or len(missions["entries"]) > MAX_BINDINGS):
                raise ValueError("Mission history cannot be safely migrated")
            for entry in missions["entries"]:
                if (not isinstance(entry, dict) or not _integer(entry.get("condition_id"), 1)
                        or type(entry.get("status")) is not int or entry["status"] not in (0, 1, 2)
                        or not _hash(entry.get("definition_version"))):
                    raise ValueError("Mission history has invalid migration fields")
                identifier = entry["condition_id"]
                if (entry["status"] != 2 and identifier in proofs
                        and entry["definition_version"] in proofs[identifier]):
                    entry["definition_version"] = self._effective[identifier].definition_version
        result[LEDGER_KEY] = marker
        return result
