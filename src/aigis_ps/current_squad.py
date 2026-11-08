from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import re

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps
from .config_state import configuration_row, patch_configuration
from .errors import ProtocolError
from .native_numbers import int32
from .player_state import (EMPTY_SQUAD_SLOT, MAX_SQUAD_NAME_BYTES, MAX_STANDARD_TEAMS, RESERVED_SQUAD_GROUP,
                           SQUAD_TUTORIAL_FLAGS, UNIT_BOOLEANS, UNIT_FIELDS, squad_address, squad_group)
from .protocol import ProtocolRegistry, encode_response
from .request_fields import invalid, registered, scalar
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .system_literals import decode_command


MAX_NEW_NAME_CHARACTERS = 12
FORBIDDEN_NAME_CHARACTERS = "\r\n\\'"
EMPTY_TEAM_SLOTS = 15
RESPONSE_LIMIT = 16384
HISTORY_NAMESPACE = "current_squad_history"
LIMITS = ParseLimits(max_input_bytes=8 * 1024 * 1024, max_decompressed_bytes=8 * 1024 * 1024)
# Native 54BE40 sends RTN instead of OI when the selection is the raid group; each names one BattleParam column.
SELECTORS = {"OI": "TN", "RTN": "RTN"}
ORDINARY_TEAM_COMMANDS = {
    "OnCommandMenuUnitTeamEditor.atb": (
        r'SetSystemStateParam\s*\(\s*"CMenuUnitTeamEditor::MaxTeamCount"\s*,\s*([0-9]+)\s*\)',
        r'SetSystemStateParam\s*\(\s*"CMenuUnitTeamEditor::BT([0-9]{2})_TeamID"\s*,\s*([0-9]+)\s*\)'),
    "OnCommandMenuUnitTeamDeparture.atb": (r'"MaxTeamCount=([0-9]+)"', r'"BT([0-9]{2})_TeamID=([0-9]+)"'),
}
RAID_TEAM_COMMANDS = {
    "OnCommandMenuRaidTeamView.atb": (
        r'SetSystemStateParam\s*\(\s*"CRaidTeamView::UseTeamCount"\s*,\s*([0-9]+)\s*\)',
        r'SetSystemStateParam\s*\(\s*"CRaidTeamView::BT([0-9]{2})_TeamID"\s*,\s*([0-9]+)\s*\)'),
    "OnCommandMenuRaidSallyView.atb": (
        r'SetSystemStateParam\s*\(\s*"CRaidSallyView::UseTeamCount"\s*,\s*([0-9]+)\s*\)',
        r'SetSystemStateParam\s*\(\s*"CRaidSallyView::BT([0-9]{2})_TeamID"\s*,\s*([0-9]+)\s*\)'),
}
_HASH = re.compile(r"[a-f0-9]{64}\Z")


@dataclass(frozen=True)
class Selection:
    group: int
    name: str
    emblem: int
    tutorial: bool


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


def _commands(asset_reader, name):
    raw = asset_reader(name)
    if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
        raise ValueError("Team commands require bounded bytes")
    data = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
    table = parse_table(data, LIMITS)
    fields = {field["name"]: field["type_code"] for field in table["fields"]}
    if fields != {"_ID": 1, "_Cmnd": 32} or len(table["fields"]) != 2:
        raise ValueError("Team command schema differs from the native contract")
    ids, commands = set(), []
    for row in table["rows"]:
        if not int32(row["_ID"]) or row["_ID"] in ids or type(row["_Cmnd"]) is not str:
            raise ValueError("Team command identities are invalid")
        ids.add(row["_ID"])
        commands.append(decode_command(row["_Cmnd"]))
    return hashlib.sha256(raw).hexdigest(), commands


def _buttons(commands, count_pattern, id_pattern):
    initializers = [(counts, re.findall(id_pattern, command)) for command in commands
                    if (counts := re.findall(count_pattern, command))]
    if len(initializers) != 1 or len(initializers[0][0]) != 1:
        raise ValueError("Team commands must declare their team count once")
    counts, bindings = initializers[0]
    buttons = {int(button): int(team) for button, team in bindings}
    if len(buttons) != len(bindings):
        raise ValueError("Team buttons are declared more than once")
    return int(counts[0]), buttons


def raid_group(commands, patterns):
    used, buttons = _buttons(commands, *patterns)
    groups = {buttons.get(button) for button in range(used)}
    if not used or len(groups) != 1 or None in groups:
        raise ValueError("Raid team buttons must share one declared group")
    group = groups.pop()
    if not MAX_STANDARD_TEAMS <= group < RESERVED_SQUAD_GROUP:
        raise ValueError("Raid group overlaps ordinary or reserved squad groups")
    return group


def team_resources(asset_reader):
    hashes = {}
    try:
        for name, patterns in ORDINARY_TEAM_COMMANDS.items():
            hashes[name], commands = _commands(asset_reader, name)
            count, buttons = _buttons(commands, *patterns)
            if (count != MAX_STANDARD_TEAMS
                    or {button: team for button, team in buttons.items() if button < count}
                    != dict(enumerate(range(count)))):
                raise ValueError("Team buttons do not map to the verified ordinary groups")
        groups = set()
        for name, patterns in RAID_TEAM_COMMANDS.items():
            hashes[name], commands = _commands(asset_reader, name)
            groups.add(raid_group(commands, patterns))
        if len(groups) != 1:
            raise ValueError("Raid team views declare different raid groups")
        group = groups.pop()
    except (AttributeError, KeyError, TypeError, ValueError, OSError, ProtocolError) as error:
        raise ProtocolError("current_squad_catalog_invalid", "Current-squad resources are unavailable or unsupported", 503) from error
    return hashes, group


class CurrentSquadService:
    def __init__(self, bootstrap, players, emblems, asset_reader):
        self.bootstrap, self.players, self.emblems = bootstrap, players, emblems
        self._hashes, self.raid_group = team_resources(asset_reader)
        self._emblem_version = emblems.content_version
        if not isinstance(self._emblem_version, str) or not _HASH.fullmatch(self._emblem_version):
            raise ProtocolError("current_squad_catalog_invalid", "Current-squad titles require a fixed content version", 503)
        self.content_version = _digest({"policy": 2, "resources": self._hashes, "emblems": self._emblem_version,
            "team_count": MAX_STANDARD_TEAMS, "raid_group": self.raid_group, "empty_slots": EMPTY_TEAM_SLOTS,
            "new_name_scalar_limit": MAX_NEW_NAME_CHARACTERS, "legacy_names": "preserve_bounded_existing",
            "legacy_af": "require_import_when_group_zero_absent", "metadata": "replace_entire_target_group"})
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "current_squad_history_invalid",
                                     "Stored current-squad receipts are invalid",
                                     fields=("request", "content", "state", "revision", "response"),
                                     response_limit=RESPONSE_LIMIT)
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (36, 39, 41, 72, 120)}
        self._ready()

    def _ready(self):
        if self.emblems.content_version != self._emblem_version or not self.emblems.is_ready():
            raise ProtocolError("current_squad_dependencies_changed", "Current-squad title definitions changed after setup", 503)

    def handlers(self):
        return {120: self.save}

    def status(self):
        try:
            self._ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "implemented_operations": [120], "content_version": self.content_version,
            "resource_sha256": dict(self._hashes), "ordinary_team_count": MAX_STANDARD_TEAMS,
            "raid_group": self.raid_group, "reload_operations": [39, 72, 36],
            "new_name_character_limit": MAX_NEW_NAME_CHARACTERS, "history_limit": HISTORY_LIMIT,
            "processed_request_limit": PROCESSED_LIMIT, "empty_group_policy": "Fifteen empty slots in child team zero",
            "request_order": REQUEST_ORDER,
            "native_tested": False, "unsupported": ["Legacy PU updates", "Native font glyph validation"]}

    def _request(self, envelope):
        root = registered(envelope, {120}, {*SELECTORS, "A3", "A4", "TL", "PU"}, {"A3", "A4"})
        if "PU" in root:
            raise ProtocolError("current_squad_branch_unsupported", "Legacy unit-update branches are not supported", 501)
        selectors = [name for name in SELECTORS if name in root]
        if len(selectors) != 1:
            invalid("Current-squad save requires exactly one ordinary or raid group identifier")
        selector = selectors[0]
        group = scalar(root[selector], "I")
        if selector == "OI" and not 0 <= group < MAX_STANDARD_TEAMS:
            invalid("Ordinary team identifier is outside the current client range")
        if selector == "RTN" and group != self.raid_group:
            invalid("Raid team identifier differs from the raid group of the current client")
        name = scalar(root["A3"], "S", max_bytes=MAX_SQUAD_NAME_BYTES)
        emblem = scalar(root["A4"], "I")
        tutorial = "TL" in root
        if tutorial and scalar(root["TL"], "I") != 1:
            invalid("Current-squad tutorial marker must be one")
        return Selection(group, name, emblem, tutorial)

    def column(self, group):
        if type(group) is int:
            if 0 <= group < MAX_STANDARD_TEAMS:
                return SELECTORS["OI"]
            if group == self.raid_group:
                return SELECTORS["RTN"]
        invalid(f"Squad groups are the ordinary teams below {MAX_STANDARD_TEAMS} or the raid group {self.raid_group}")

    @staticmethod
    def _check_imported(player):
        # Until the client's 74 import moves them into group 0, legacy members exist only in OrganizationIndex2.
        if (not player["legacy_squad_imported"] and not any(squad_group(row) == 0 for row in player["squads"])
                and any(unit["OrganizationIndex2"] != 0 for unit in player["units"])):
            raise ProtocolError("current_squad_legacy_pending", "Import the existing legacy squad members before saving metadata", 409)

    def require_imported(self, state):
        self._check_imported(self.players.validate(state))

    def set_metadata(self, state, group, name, emblem):
        self.column(group)
        player = self.players.validate(state)
        current = [row for row in player["squads"] if squad_group(row) == group]
        if name not in {row["Name"] for row in current} and (current or name):
            if not name or len(name) > MAX_NEW_NAME_CHARACTERS or any(char in name for char in FORBIDDEN_NAME_CHARACTERS):
                invalid("New squad names must contain at most twelve Unicode characters and no forbidden delimiters")
        self._check_imported(player)
        self.emblems.require_eligible(emblem, state)
        updated = deepcopy(state)
        target = updated["player_state"]
        target["legacy_squad_imported"] = True
        if current:
            for row in target["squads"]:
                if squad_group(row) == group:
                    row.update(Name=name, Flags=emblem)
        else:
            target["squads"].extend({"Address": squad_address(group, 0, slot), "UnitIndex": EMPTY_SQUAD_SLOT,
                                     "Name": name, "Flags": emblem} for slot in range(1, EMPTY_TEAM_SLOTS + 1))
        return updated

    def select(self, state, group):
        column = self.column(group)
        self._check_imported(self.players.validate(state))
        updated = deepcopy(state)
        updated["player_state"]["battle"][column] = group
        updated["player_state"]["legacy_squad_imported"] = True
        return updated

    def apply(self, state, selection):
        updated = self.set_metadata(state, selection.group, selection.name, selection.emblem)
        updated["player_state"]["battle"][self.column(selection.group)] = selection.group
        if selection.tutorial:
            flags = configuration_row(updated)["MenuTutorialFlags"]
            updated = patch_configuration(updated, {"MenuTutorialFlags": flags | SQUAD_TUTORIAL_FLAGS})
        return updated

    def _preflight(self, profile, state):
        candidate = replace(profile, state=state)
        player = self.players.validate(state)
        encode_response(self._profiles[39], self.players.battle_param_response(player))
        encode_response(self._profiles[72], self.players.squad_response(candidate, player))
        encode_response(self._profiles[41], self.players.unit_response(player["units"]))
        config = self.bootstrap._rows(candidate, "config")
        encode_response(self._profiles[36], dumps(Container(children=self.bootstrap.columns("config", config))))

    def _response(self, profile):
        return dumps(Container(children=(self.bootstrap._system(profile), Container("PPU", tuple(
            Column(alias, "B" if name in UNIT_BOOLEANS else "I", ()) for name, alias in UNIT_FIELDS)))))

    def save(self, envelope, store):
        self._ready()
        selection = self._request(envelope)
        key = hashlib.sha256(f"120:{envelope.profile.route}:{envelope.profile.st}:".encode() + envelope.xml).hexdigest()
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            history = self._ledger.read(profile.state, profile.revision)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                if cached["revision"] != profile.revision or cached["content"] != self.content_version:
                    raise ProtocolError("current_squad_retry_stale", "Current-squad state or content changed after this save", 409)
                if cached["state"] != _state_digest(profile.state) or self.apply(profile.state, selection) != profile.state:
                    self._ledger.invalid()
                self._preflight(profile, profile.state)
                response = self._response(profile)
                if response != cached["response"].encode("utf-8"):
                    self._ledger.invalid()
                encode_response(envelope.profile, response)
                return response
            if self._ledger.processed(history, key):
                raise ProtocolError("current_squad_request_stale", "Current-squad request was already processed", 409)
            updated = self.apply(profile.state, selection)
            self._preflight(profile, updated)
            response = self._response(profile)
            encode_response(envelope.profile, response)
            self._ledger.record(updated, history, {"request": key, "content": self.content_version,
                                                   "state": _state_digest(updated), "revision": profile.revision + 1,
                                                   "response": response.decode("utf-8")})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving the current squad", 409)
