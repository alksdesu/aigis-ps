from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace

from .alxml import Container, dumps
from .bootstrap import BootstrapService
from .config_state import CONFIGURATION_STRING_SIZES, configuration_delta, configuration_row, patch_configuration
from .errors import ProtocolError
from .player_state import LOCK_FLAG, MAX_PLAYER_UNITS, UNIT_XML_LIMITS, PlayerStateService, primary_unit_updates
from .protocol import ProtocolRegistry, RequestEnvelope, encode_response
from .request_fields import fields, invalid, registered, scalar, values
from .storage import Store


CONFIG = "config"
SORT_FIELDS = {"F1": "_FlagSort", "F4": "_FlagMissionFilter", "F5": "TypeOfSortOrder"}
# Native fixed string cells keep one byte for the terminator.
SORT_BYTE_LIMITS = {wire: CONFIGURATION_STRING_SIZES[name] - 1 for wire, name in SORT_FIELDS.items()}
SORT_REPLY_FIELDS = ("_FlagSort", "_FlagMissionFilter")
AUTO_SELECT_FIELDS = {"F6": "AutoSelectConfig", "FB": "IntegrationAutoSelectConfig"}
UNIT_ACTION_LIMITS = replace(UNIT_XML_LIMITS, max_values=UNIT_XML_LIMITS.max_values + 32)


@dataclass(frozen=True)
class LockChange:
    state: dict
    units: tuple[dict, ...]


class UnitActionService:
    def __init__(self, bootstrap: BootstrapService, players: PlayerStateService):
        self.bootstrap = bootstrap
        self.players = players
        self._configuration_profile = ProtocolRegistry.load().by_registration[36]

    def handlers(self):
        return {25: self.save_sort, 27: self.save_auto_select, 119: self.save_locks}

    def status(self):
        return {"implemented_operations": [25, 27, 119], "sort_fields": dict(SORT_FIELDS),
                "auto_select_fields": dict(AUTO_SELECT_FIELDS),
                "auto_select_policy": "Replace both native int32 preference values; preserve unrelated configuration",
                "separate_filter_operation": 26,
                "sort_response": "PGC returns F1/F4; the full Config load returns F5",
                "lock_policy": "Set bit 1 from the request; preserve all other saved unit attributes",
                "lock_response": "Complete affected primary-barracks rows for the existing-unit PPU receiver"}

    def set_locks(self, state: dict, locks: Mapping[int, bool]) -> LockChange:
        owned = {unit["GetIndex"]: unit for unit in self.players.validate(state)["units"]}
        if locks.keys() - owned.keys():
            raise ProtocolError("unit_not_owned", "Unit lock references an unknown owned instance", 409)
        updates = {}
        for index, locked in locks.items():
            unit = deepcopy(owned[index])
            unit["GeneralPurposeFlags"] = (unit["GeneralPurposeFlags"] & ~LOCK_FLAG) | (LOCK_FLAG if locked else 0)
            updates[index] = unit
        updated = deepcopy(state)
        updated["player_state"]["units"] = [updates.get(unit["GetIndex"], unit) for unit in updated["player_state"]["units"]]
        self.players.validate(updated)
        return LockChange(updated, tuple(updates.values()))

    def set_sort(self, state: dict, preferences: Mapping[str, str]) -> dict:
        return self._replace_preferences(state, SORT_FIELDS, preferences)

    def set_auto_select(self, state: dict, preferences: Mapping[str, int]) -> dict:
        return self._replace_preferences(state, AUTO_SELECT_FIELDS, preferences)

    def _replace_preferences(self, state: dict, aliases: Mapping[str, str], preferences: Mapping[str, object]) -> dict:
        if not isinstance(preferences, Mapping) or preferences.keys() != aliases.keys():
            raise ValueError("Preference saves replace their complete native field group")
        self.bootstrap.table_rows(state, CONFIG)
        return patch_configuration(state, {name: preferences[wire] for wire, name in aliases.items()})

    def save_locks(self, envelope: RequestEnvelope, store: Store) -> bytes:
        request = registered(envelope, {119}, {"PU"}, {"PU"})
        columns = fields(request["PU"], {"A7", "AE"})
        indices, flags = (), ()
        if columns:
            if set(columns) != {"A7", "AE"}:
                invalid("Unit lock columns must include both A7 and AE")
            indices = values(columns["A7"], "I", maximum=MAX_PLAYER_UNITS)
            flags = values(columns["AE"], "I", maximum=MAX_PLAYER_UNITS)
            if len(indices) != len(flags) or len(set(indices)) != len(indices) or any(index < 0 for index in indices):
                invalid("Unit lock columns require matching rows and distinct nonnegative indices")
        locks = {index: bool(requested & LOCK_FLAG) for index, requested in zip(indices, flags)}
        for _ in range(3):
            profile = self.players.authenticate(envelope, store)
            change = self.set_locks(profile.state, locks)
            response = dumps(Container(children=(self.bootstrap._system(profile), primary_unit_updates(change.units))),
                             limits=UNIT_ACTION_LIMITS)
            encode_response(envelope.profile, response)
            if change.state == profile.state:
                return response
            try:
                store.update_state(change.state, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving unit locks", 409)

    def save_sort(self, envelope: RequestEnvelope, store: Store) -> bytes:
        request = registered(envelope, {25}, SORT_FIELDS, SORT_FIELDS)
        preferences = {wire: scalar(request[wire], "S", max_bytes=SORT_BYTE_LIMITS[wire]) for wire in SORT_FIELDS}
        for _ in range(3):
            profile = self.bootstrap._authenticate(envelope, store)
            updated = self.set_sort(profile.state, preferences)
            response = dumps(Container(children=(self.bootstrap._system(profile), configuration_delta(
                configuration_row(updated), SORT_REPLY_FIELDS))))
            encode_response(envelope.profile, response)
            if updated == profile.state:
                return response
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Configuration changed while saving sort preferences", 409)

    def save_auto_select(self, envelope: RequestEnvelope, store: Store) -> bytes:
        request = registered(envelope, {27}, AUTO_SELECT_FIELDS, AUTO_SELECT_FIELDS)
        preferences = {wire: scalar(request[wire], "I") for wire in AUTO_SELECT_FIELDS}
        for _ in range(3):
            profile = self.bootstrap._authenticate(envelope, store)
            updated = self.set_auto_select(profile.state, preferences)
            config = dumps(Container(children=self.bootstrap.columns(CONFIG, self.bootstrap.table_rows(updated, CONFIG))))
            encode_response(self._configuration_profile, config)
            response = dumps(Container(children=(self.bootstrap._system(profile), configuration_delta(
                configuration_row(updated), tuple(AUTO_SELECT_FIELDS.values())))))
            encode_response(envelope.profile, response)
            if updated == profile.state:
                return response
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Configuration changed while saving automatic selection preferences", 409)
