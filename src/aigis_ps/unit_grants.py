from __future__ import annotations

from copy import deepcopy
import re

from .errors import ProtocolError
from .native_numbers import int32
from .player_state import UNIT_BOOLEANS, UNIT_FIELDS
from .unit_initialization import UnitInitialValues


MAX_GRANTS = 100
MAX_INITIALIZATIONS = 65535
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_PREFIX = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _same(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(value, right[key]) for key, value in left.items())
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


class UnitGrants:
    def __init__(self, players, *, error_prefix="unit_grant"):
        if type(error_prefix) is not str or not _PREFIX.fullmatch(error_prefix):
            raise ValueError("Unit grant errors require a bounded code prefix")
        self.players = players
        self._prefix = error_prefix
        self._initial_values = {}
        self._initializer = players.unit_initializer()
        self._initialization_version = self._initializer.content_version
        if type(self._initialization_version) is not str or not _HASH.fullmatch(self._initialization_version):
            self._error("dependencies_invalid", "Unit initialization requires a fixed fingerprint")
        self.ready()

    @property
    def initialization_version(self):
        return self._initialization_version

    def _error(self, suffix, message, status=503):
        raise ProtocolError(f"{self._prefix}_{suffix}", message, status)

    def ready(self):
        if self.players.initialization_version != self.initialization_version:
            self._error("dependencies_changed", "Unit initialization changed after setup")

    def _initial(self, value):
        if (not isinstance(value, UnitInitialValues) or not int32(value.class_id) or value.class_id <= 0
                or not int32(value.stage) or value.stage not in (0, 2)
                or not int32(value.skill_id) or value.skill_id < 0
                or not int32(value.experience) or value.experience < 0):
            self._error("initialization_unsupported", "Unit initial values are invalid or unsupported", 501)
        return deepcopy(value)

    def initial_values(self, card_id, level, evolution_count=0):
        self.ready()
        return self._resolve_initial(card_id, level, evolution_count)

    def _resolve_initial(self, card_id, level, evolution_count=0):
        self.players.validate_grant_identity(card_id, level)
        if not int32(evolution_count) or evolution_count not in (0, 2):
            self._error("initialization_unsupported", "The requested initial stage is unsupported", 501)
        key = card_id, level, evolution_count
        if key not in self._initial_values:
            self._initial_values[key] = self._initial(
                self._initializer.values(card_id, level, evolution_count=evolution_count))
        return deepcopy(self._initial_values[key])

    def initial_batch(self, specs):
        self.ready()
        if not isinstance(specs, (list, tuple)) or len(specs) > MAX_INITIALIZATIONS:
            self._error("result_invalid", "Unit initialization requires a bounded selection")
        result = []
        for spec in specs:
            if (not isinstance(spec, dict) or not {"card_id", "level"}.issubset(spec)
                    or set(spec) - {"card_id", "level", "evolution_count"}):
                self._error("result_invalid", "Unit initialization fields are invalid")
            result.append(self._resolve_initial(spec["card_id"], spec["level"], spec.get("evolution_count", 0)))
        self.ready()
        return tuple(result)

    def expected_unit(self, card_id, initial, get_index, unique_id):
        self.ready()
        return self._expected(card_id, initial, get_index, unique_id)

    def _expected(self, card_id, initial, get_index, unique_id):
        self.players.validate_grant_identity(card_id, 0)
        initial = self._initial(initial)
        if not int32(get_index) or get_index <= 0 or not int32(unique_id) or unique_id <= 0:
            self._error("result_invalid", "New unit identities require positive native integers")
        unit = {name: False if name in UNIT_BOOLEANS else 0 for name, _ in UNIT_FIELDS}
        unit.update(CardID=card_id, ClassID=initial.class_id, ClassChangeCount=initial.stage,
            Exp=initial.experience, SkillLevel=1, GetIndex=get_index, UniqueID=unique_id,
            OrganizationIndex=-1, SettingSkillID=initial.skill_id, GeneralPurposeFlags=1)
        return unit

    def grant(self, state, specs):
        self.ready()
        if not isinstance(state, dict) or not isinstance(specs, (list, tuple)) or not 1 <= len(specs) <= MAX_GRANTS:
            self._error("result_invalid", "Unit grants require a state and a bounded nonempty selection")
        grants, initial, count = [], [], 0
        for spec in specs:
            if (not isinstance(spec, dict) or not {"card_id", "level"}.issubset(spec)
                    or set(spec) - {"card_id", "level", "evolution_count", "count"}
                    or not int32(spec.get("count", 1)) or spec.get("count", 1) <= 0):
                self._error("result_invalid", "Unit grant fields do not match the selected reward")
            grant = {"card_id": spec["card_id"], "level": spec["level"],
                     "evolution_count": spec.get("evolution_count", 0), "count": spec.get("count", 1)}
            count += grant["count"]
            if count > MAX_GRANTS:
                self._error("result_invalid", "Unit grants exceed the supported instance count")
            initial.append(self._resolve_initial(grant["card_id"], grant["level"], grant["evolution_count"]))
            grants.append(grant)
        original = deepcopy(state)
        player = self.players.validate_player(deepcopy(original.get("player_state")))
        self.players.require_unit_space(deepcopy(original), count)
        get_index = player["battle"]["UX"]
        unique_id = player.get("last_unique_id", max(unit["UniqueID"] for unit in player["units"]))
        expected_units = []
        for grant, values in zip(grants, initial):
            for _ in range(grant["count"]):
                get_index += 1
                unique_id += 1
                expected_units.append(self._expected(grant["card_id"], values, get_index, unique_id))
        updated, added = self.players.credit_units(deepcopy(original), grants)
        expected = deepcopy(original)
        expected["player_state"]["units"].extend(expected_units)
        expected["player_state"]["battle"]["UX"] = get_index
        expected["player_state"]["last_unique_id"] = unique_id
        if not _same(added, expected_units) or not _same(updated, expected):
            self._error("result_invalid", "Unit grant changed unrelated state or selected unit fields")
        self.ready()
        return deepcopy(updated), deepcopy(added)
