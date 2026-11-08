from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
from importlib.resources import files
from itertools import chain
import json
import logging
from operator import is_
from pathlib import Path
import re
import secrets
import threading
from types import MappingProxyType
from xml.etree import ElementTree as ET

from .al_format import decompress_all4, parse_archive, parse_schema, parse_table
from .alxml import Column, Container, Limits, ValueType, dumps, loads
from .assets import resolve_asset
from .config_state import configuration_row, patch_configuration
from .errors import ProtocolError
from .local_io import read_bounded
from .native_numbers import INT32_MAX, UINT32_MASK, int32, signed32
from .protocol import RESPONSE_TOO_LARGE, ProtocolRegistry, RequestEnvelope, encode_response
from .public_tables import validate_public_master
from .request_fields import COMMON_FIELDS
from .storage import PlayerProfile, Store


PLAYER_SCHEMA_SHA256 = "c5367629f66a4433b5b8431a39dc0cc68ea90660816f010e6b2ee5f798c73c22"
DISPLAY_FIELD = "_DisplayType"
# Native 65A2E0 and B39510 remove columns outside this alias map before serializing unit data.
UNIT_FIELDS = (
    ("CardID", "A1"), ("ClassID", "A2"), ("ClassChangeCount", "A3"), ("Exp", "A4"),
    ("Love", "A5"), ("SkillLevel", "A6"), ("GetIndex", "A7"), ("OrganizationIndex", "A8"),
    ("WatchLoveEv", "A9"), ("CostModMixValue", "AA"), ("UniqueID", "AB"),
    ("ClassChangeStatusMod", "AC"), ("SettingSkillID", "AD"), ("GeneralPurposeFlags", "AE"),
    ("OrganizationIndex2", "AF"), (DISPLAY_FIELD, "B0"), ("SkillEvolved", "B1"),
    ("SkillExperience", "B2"), ("CostExperience", "B3"), ("CanFame", "B4"), ("Famed", "B5"),
)
UNIT_BOOLEANS = frozenset(("CanFame", "Famed"))
MAX_PLAYER_UNITS = 4096
MAX_SQUAD_ROWS = 4096
MAX_STANDARD_TEAMS = 20
UNIT_XML_LIMITS = Limits(max_values=MAX_PLAYER_UNITS * len(UNIT_FIELDS))
CLIENT_RUNTIME_FIELDS = (
    "Cost", "Level", "HP", "MaxHP", "MaxHPOrig", "Atk", "AtkOrig", "Def", "DefOrig", "SkillState",
    "SkillDelay", "SkillDelayStFrm", "SKillStateWaitTime", "SetFlag", "SelectIndex", "NewFlag", "StatusParam",
    "Rare", "SexKind", "MagicResistance", "ExtendRange", "ExtendAtkSpd", "_TypeRace", "_TypeBigRace",
    "_AppearAbilityLevel", "VoteCount", "ExtendSkillTime", "ExtendSkillFastRecast", "ExtendAvoidPhysical",
    "ExtendLifeDrain", "ExtendPierce", "ExtendBarrier", "ExtendCostFix", "DateOfImplementation",
)
BATTLE_PARAM_FIELDS = (("VN", ValueType.STRING), ("UX", ValueType.INT), ("TN", ValueType.INT), ("RTN", ValueType.INT))
SQUAD_FIELDS = (("Address", "A0", ValueType.INT), ("UnitIndex", "A1", ValueType.INT),
                ("Name", "A3", ValueType.STRING), ("Flags", "A4", ValueType.INT))
EMPTY_SQUAD_SLOT = -1
RESERVED_SQUAD_GROUP = 255
SQUAD_ADDRESS_MASK = UINT32_MASK
SQUAD_GROUP_SHIFT, SQUAD_TEAM_SHIFT = 24, 16
SQUAD_TEAM_MASK, SQUAD_SLOT_MASK = 0xFF, 0xFFFF
MAX_SQUAD_SLOT = 15
MAX_SQUAD_NAME_BYTES = 128
# Native 6E49A0 sets tutorial bit 0 locally when the gift menu opens; the server persists it after a successful gift.
GIFT_TUTORIAL_FLAG = 1
# Native 54B600 sets tutorial bits 1 and 2 together when a squad save carries TL.
SQUAD_TUTORIAL_FLAGS = 6
PRINCE_CARD_IDS = frozenset((1, 309))
LOCK_FLAG = 2
# GeneralPurposeFlags bit 2 records whether the awakened skill is in effect.
SKILL_SWITCH_FLAG = 4
BARRACKS_SHIFT, BARRACKS_MASK = 4, 0xF
BARRACKS_BITS = BARRACKS_MASK << BARRACKS_SHIFT
UNIT_GRANT_UNSUPPORTED = "unit_grant_unsupported"
log = logging.getLogger("aigis_ps")


def unavailable(code: str, message: str) -> ProtocolError:
    return ProtocolError(code, message, 503)


def barracks_index(unit: dict) -> int:
    return unit["GeneralPurposeFlags"] >> BARRACKS_SHIFT & BARRACKS_MASK


def squad_group(row: dict) -> int:
    return (row["Address"] & SQUAD_ADDRESS_MASK) >> SQUAD_GROUP_SHIFT


def squad_position(row: dict) -> tuple[int, int]:
    address = row["Address"] & SQUAD_ADDRESS_MASK
    return address >> SQUAD_TEAM_SHIFT & SQUAD_TEAM_MASK, address & SQUAD_SLOT_MASK


def squad_address(group: int, team: int, slot: int) -> int:
    # A0 carries the packed unsigned address as a signed int32.
    return signed32((group << SQUAD_GROUP_SHIFT) | (team << SQUAD_TEAM_SHIFT) | slot)


def primary_unit_updates(units) -> Container:
    # Native CUpdateUnitList (A67C00) only searches the primary barracks; an unmatched GetIndex overwrites its last row.
    rows = [unit for unit in units if not barracks_index(unit)]
    return Container("PPU", tuple(Column(alias, "B" if name in UNIT_BOOLEANS else "I", tuple(unit[name] for unit in rows))
                                  for name, alias in UNIT_FIELDS))


@dataclass(frozen=True)
class SquadReplacement:
    state: dict
    vacated: tuple[int, ...]


@dataclass(frozen=True)
class _VerifiedCards:
    source: bytes
    digest: str
    cards: dict[int, Mapping]
    rarities: Mapping[int, int]


@dataclass(frozen=True)
class _GrowthTables:
    key: tuple[bytes, bytes]
    experience: tuple[int, ...]
    rarities: dict[int, dict]


@dataclass(frozen=True)
class _ClassInputs:
    members: tuple
    rows: dict[int, dict]
    digest: bytes


class PlayerStateService:
    def __init__(self, asset_root: Path, master_provider: Callable[[str], bytes] | None,
                 *, asset_reader: Callable[[str], bytes] | None = None,
                 capacity_provider: Callable[[dict], int] | None = None,
                 squad_emblem_validator: Callable[[int, dict], object] | None = None):
        self.asset_root = Path(asset_root)
        self.asset_reader = asset_reader
        self.master_provider = master_provider
        self.capacity_provider = capacity_provider
        if squad_emblem_validator is not None and not callable(squad_emblem_validator):
            raise ValueError("Squad emblem validator must be callable")
        self.squad_emblem_validator = squad_emblem_validator
        self.issues = {}
        self.class_ids = set()
        self._classes = {}
        self._cards_cache = None
        self._cards_lock = threading.Lock()
        self._growth_cache = None
        self._growth_lock = threading.Lock()
        self._class_cache = None
        self._initialization_cache = None
        self._initialization_lock = threading.Lock()
        self._unit_response_profile = None
        schemas = json.loads(files("aigis_ps").joinpath("data/master-schemas.json").read_text(encoding="utf-8"))
        self.master_schemas = {name: [field["name"] for field in table["fields"]] for name, table in schemas["tables"].items()}
        unit_members = {}
        try:
            data = self.read_asset("PlayerUnitTable.aar")
            archive = parse_archive(data)
            for name in ("PlayerUnit.ard", "ClassData.atb"):
                members = [member for member in archive.members if member.name == name]
                if len(members) != 1:
                    raise ValueError(f"{name} archive member is missing or ambiguous")
                member = members[0]
                unit_members[name] = data[member.offset:member.offset + member.size]
            schema = unit_members["PlayerUnit.ard"]
            if hashlib.sha256(schema).hexdigest() != PLAYER_SCHEMA_SHA256:
                raise ValueError("PlayerUnit schema differs from the verified native contract")
        except (ValueError, OSError) as error:
            self.issues["player_unit_schema"] = str(error)
        try:
            schema = parse_schema(self.read_asset("BattleParam.ard"))
            if [(field.name, field.type_code, field.size) for field in schema.fields] != [("VN", 33, 16), ("UX", 1, 4), ("TN", 1, 4), ("RTN", 1, 4)]:
                raise ValueError("BattleParam schema differs from the verified native contract")
        except (ValueError, OSError) as error:
            self.issues["battle_param_schema"] = str(error)
        try:
            data = unit_members.get("ClassData.atb", b"")
            rows = parse_table(decompress_all4(data) if data.startswith(b"ALL4") else data)["rows"]
            if not rows or any(type(row.get("ClassID")) is not int or row["ClassID"] <= 0 for row in rows):
                raise ValueError("ClassData does not contain valid class identifiers")
            self.class_ids = {row["ClassID"] for row in rows}
            if len(self.class_ids) != len(rows):
                raise ValueError("ClassData contains duplicate class identifiers")
            self._classes = {row["ClassID"]: MappingProxyType(row) for row in rows}
        except (ValueError, OSError) as error:
            self.issues["class_data"] = str(error)

    def _raw_asset(self, name: str) -> bytes:
        data = (self.asset_reader(name) if self.asset_reader is not None else
                read_bounded(resolve_asset(self.asset_root, name), 64 * 1024 * 1024, root=self.asset_root))
        if not isinstance(data, bytes) or len(data) > 64 * 1024 * 1024:
            raise ValueError("Asset reader returned an invalid or oversized payload")
        return data

    def read_asset(self, name: str) -> bytes:
        data = self._raw_asset(name)
        return decompress_all4(data) if data.startswith(b"ALL4") else data

    def raid_group(self) -> int:
        # Deferred because current_squad imports this module's squad constants at load time.
        from .current_squad import team_resources
        try:
            return team_resources(self._raw_asset)[1]
        except ProtocolError as error:
            raise unavailable("player_raid_group_unavailable", "The raid team views do not declare a verified raid group") from error

    def raid_group_status(self) -> dict:
        try:
            return {"ready": True, "group": self.raid_group()}
        except ProtocolError as error:
            return {"ready": False, "error": error.code}

    def _verified_cards(self) -> _VerifiedCards:
        if self.issues:
            raise unavailable("player_contract_unavailable", "Verified player schemas or class data are unavailable")
        if self.master_provider is None:
            raise unavailable("player_master_unavailable", "A verified CardData provider is required")
        try:
            raw = self.master_provider("CardData")
            if not isinstance(raw, bytes):
                raise ValueError("CardData provider must return bytes")
            verified = self._cards_cache
            # Bytes never change, so the object already verified needs no new digest.
            if verified is not None and verified.source is raw:
                return verified
            digest = hashlib.sha256(raw).hexdigest()
            with self._cards_lock:
                if self._cards_cache is not None and self._cards_cache.digest == digest:
                    self._cards_cache = replace(self._cards_cache, source=raw)
                    return self._cards_cache
                document, _ = validate_public_master("CardData", raw, self.master_schemas)
                columns = {column.name: column.values for column in document.children}
                names = ("CardID", "InitClassID", "ClassLV0SkillID", "ClassLV1SkillID")
                rows = [dict(zip(names, row)) for row in zip(*(columns[name] for name in names))]
                if any(any(type(value) is not int or value < 0 for value in row.values()) for row in rows):
                    raise ValueError("CardData contains invalid unit references")
                for name in ("Rare", "Blood", "ExcludeFameDisplayFirstClass"):
                    if name in columns:
                        for row, value in zip(rows, columns[name]):
                            row[name] = value
                cards = {row["CardID"]: MappingProxyType(row) for row in rows}
                if 1 not in cards or cards[1]["InitClassID"] not in self.class_ids:
                    raise ValueError("The native default unit is absent from verified master data")
                rarities = MappingProxyType({identifier: row["Rare"] for identifier, row in cards.items() if "Rare" in row})
                self._cards_cache = _VerifiedCards(raw, digest, cards, rarities)
                return self._cards_cache
        except (ValueError, OSError, KeyError) as error:
            raise unavailable("player_master_unavailable", "Verified CardData is unavailable for player units") from error

    @property
    def classes(self) -> Mapping[int, Mapping]:
        return MappingProxyType(self._classes)

    def _cards(self) -> dict[int, Mapping]:
        return self._verified_cards().cards

    def cards(self) -> Mapping[int, Mapping]:
        return MappingProxyType(self._cards())

    def card_rarities(self) -> Mapping[int, int]:
        return self._verified_cards().rarities

    def status(self) -> dict:
        issues = dict(self.issues)
        try:
            self._cards()
        except ProtocolError as error:
            issues[error.code] = str(error)
        return {"ready": not issues, "unavailable": issues, "implemented_operations": [39, 41, 72, 73, 74],
                "player_schema_sha256": PLAYER_SCHEMA_SHA256, "persisted_unit_columns": 21,
                "client_runtime_columns": list(CLIENT_RUNTIME_FIELDS), "derived_master_columns": 64,
                "unit_initialization": self.initialization_status(),
                "battle_param_references": "Request 39 validates the stored roster without CardData; 41 keeps card references",
                "local_rules": {"official_starter_grants": False, "default_card_id": 1, "unique_id": 1,
                                "get_index": 0, "initial_exp": 0, "initial_love": 0, "initial_skill_level": 1,
                                "battle_version": "1.0.1", "selected_team": 0, "raid_team": self.raid_group_status(),
                                "squad_import": "One-time validated legacy import; native padding rows repeating a member slot collapse into that member; repeat calls preserve stored squads",
                                "squad_save": "Replace submitted top-level squads; preserve other squads; TL sets tutorial bits 1 and 2; "
                                              "members whose GetIndex was already consumed become empty slots",
                                "unit_grants": {"over_cap": "clamp_to_cap_before_exp", "zero_level": "level_1_minimum_exp",
                                                "general_purpose_flags": 1, "skill_level": 1,
                                                "existing_units": "preserve", "default_unit_grants": False}}}

    def _growth_tables(self) -> _GrowthTables:
        try:
            experience_raw = self.read_asset("ExperienceTable.atb")
            rarity_raw = self.read_asset("cfgRarity.atb")
            key = hashlib.sha256(experience_raw).digest(), hashlib.sha256(rarity_raw).digest()
            with self._growth_lock:
                if self._growth_cache is not None and self._growth_cache.key == key:
                    return self._growth_cache
                experience_rows = parse_table(experience_raw)["rows"]
                if (not experience_rows or any(not int32(row.get("Level")) or row["Level"] != index + 1
                                              or not int32(row.get("Exp")) or row["Exp"] < 0
                                              for index, row in enumerate(experience_rows))):
                    raise ValueError("ExperienceTable must contain ordered positive levels and int32 experience")
                experience = tuple(row["Exp"] for row in experience_rows)
                if experience[0] != 0 or any(previous >= current for previous, current in zip(experience, experience[1:])):
                    raise ValueError("ExperienceTable must contain increasing cumulative thresholds from zero")
                rarity_rows = parse_table(rarity_raw)["rows"]
                if (not rarity_rows or any(not int32(row.get("ConfigID")) or row["ConfigID"] <= 0
                                          or any(not int32(row.get(name)) or row[name] < 0
                                                 for name in ("RequireExpRatio", "LevelCap00")) for row in rarity_rows)):
                    raise ValueError("cfgRarity is missing valid experience ratios or initial level caps")
                rarities = {row["ConfigID"]: row for row in rarity_rows}
                if len(rarities) != len(rarity_rows):
                    raise ValueError("cfgRarity contains duplicate configuration identifiers")
                self._growth_cache = _GrowthTables(key, experience, rarities)
                return self._growth_cache
        except (ValueError, OSError, KeyError, ProtocolError) as error:
            raise unavailable("player_growth_unavailable", "Verified unit growth tables are unavailable") from error

    def growth_status(self) -> dict:
        try:
            growth = self._growth_tables()
        except ProtocolError as error:
            return {"ready": False, "unavailable": {error.code: str(error)}}
        return {"ready": True, "unavailable": {}, "experience_levels": len(growth.experience),
                "rarity_rows": len(growth.rarities), "references_checked_per_grant": True}

    def _class_inputs(self) -> _ClassInputs:
        from .unit_initialization import CLASS_FIELDS
        members = tuple(chain.from_iterable(self._classes.items()))
        cached = self._class_cache
        # Rows are read-only views, so the same keys and row objects still hold the values hashed before.
        if cached is not None and len(members) == len(cached.members) and all(map(is_, members, cached.members)):
            return cached
        rows = {identifier: {name: row[name] for name in CLASS_FIELDS if name in row}
                for identifier, row in self._classes.items()}
        cached = _ClassInputs(members, rows, hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).digest())
        self._class_cache = cached
        return cached

    def _initialization(self):
        from .unit_initialization import NativeUnitInitializer
        verified, growth = self._verified_cards(), self._growth_tables()
        try:
            blood = self.read_asset("PlayerBloodType.atb")
            classes = self._class_inputs()
            key = verified.digest, growth.key, classes.digest, hashlib.sha256(blood).digest()
            with self._initialization_lock:
                if self._initialization_cache is None or self._initialization_cache[0] != key:
                    self._initialization_cache = key, NativeUnitInitializer(verified.cards, classes.rows, growth.experience,
                                                                            growth.rarities, blood)
                return self._initialization_cache[1]
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise unavailable("player_growth_initialization_unavailable", "Native unit initialization resources are unavailable") from error

    def unit_initializer(self):
        return self._initialization()

    @property
    def initialization_version(self):
        return self._initialization().content_version

    def initialization_status(self):
        try:
            version = self.initialization_version
        except ProtocolError as error:
            return {"ready": False, "error": error.code}
        return {"ready": True, "content_version": version, "existing_units": "preserve"}

    def initial_unit_values(self, card_id, level, *, evolution_count=0):
        self.validate_grant_identity(card_id, level)
        if not int32(evolution_count) or evolution_count not in (0, 2):
            raise ProtocolError("unit_grant_invalid", "The requested initial stage is not supported")
        if card_id not in self._cards():
            raise ProtocolError("unit_card_missing", "Unit grant does not reference a verified card")
        try:
            return self._initialization().values(card_id, level, evolution_count=evolution_count)
        except ProtocolError as error:
            if error.code.startswith("player_"):
                raise
            if error.status == 501:
                raise ProtocolError(UNIT_GRANT_UNSUPPORTED, "Unit grant requires an unverified initialization variant", 501) from error
            raise unavailable("player_growth_reference", "Unit initialization does not resolve to verified growth data") from error

    @staticmethod
    def validate_grant_identity(card_id: int, level: int) -> None:
        if not int32(card_id) or card_id <= 1 or not int32(level) or level < 0:
            raise ProtocolError("unit_grant_invalid", "Expected a non-default card identifier and a nonnegative int32 level")

    def experience_for_level(self, card_id: int, level: int) -> int:
        return self.initial_unit_values(card_id, level).experience

    def available_unit_slots(self, state: dict) -> int:
        player = self.validate(state)
        reserved = sum(grant["count"] for grant in self.quest_reservations(state))
        identity = player.get("last_unique_id", max(unit["UniqueID"] for unit in player["units"]))
        available = max(0, min(MAX_PLAYER_UNITS - len(player["units"]),
                               INT32_MAX - identity, INT32_MAX - player["battle"]["UX"]) - reserved)
        return min(available, self._barracks_slots(state))

    def _barracks_slots(self, state: dict) -> int:
        if self.capacity_provider is not None:
            native_available = self.capacity_provider(state)
            if not int32(native_available) or native_available < 0:
                raise unavailable("unit_capacity_invalid", "Capacity provider must return a nonnegative integer")
            return native_available
        return INT32_MAX

    def require_barracks_space(self, state: dict, count: int) -> None:
        if not int32(count) or count < 0:
            raise ProtocolError("unit_grant_invalid", "Required unit slots must be a nonnegative integer")
        if count and count > self._barracks_slots(state):
            raise ProtocolError("unit_roster_capacity", "Unit grant exceeds the available barracks capacity", 409)

    def require_unit_space(self, state: dict, count: int) -> None:
        if not int32(count) or count < 0:
            raise ProtocolError("unit_grant_invalid", "Required unit slots must be a nonnegative integer")
        if count and count > self.available_unit_slots(state):
            raise ProtocolError("unit_roster_capacity", "Unit grant exceeds the available barracks capacity", 409)

    @staticmethod
    def quest_reservations(state: dict) -> list[dict]:
        journal = state.get("quest_state")
        if journal is None:
            return []
        if not isinstance(journal, dict):
            raise unavailable("quest_state_invalid", "Battle journal must be an object")
        active = journal.get("active")
        if active is None:
            return []
        if not isinstance(active, dict):
            raise unavailable("quest_state_invalid", "Active battle must be an object")
        if "unit_reservations" not in active:
            raise ProtocolError("quest_reservation_unavailable", "Settle, exit or restart the legacy battle before receiving other units", 409)
        grants = active["unit_reservations"]
        if not isinstance(grants, list) or len(grants) > MAX_PLAYER_UNITS:
            raise unavailable("quest_state_invalid", "Reserved battle rewards must be a bounded list")
        for grant in grants:
            if (not isinstance(grant, dict) or set(grant) != {"card_id", "level", "count"}
                    or not int32(grant["card_id"]) or grant["card_id"] <= 1
                    or not int32(grant["level"]) or grant["level"] < 0
                    or not int32(grant["count"]) or grant["count"] <= 0):
                raise unavailable("quest_state_invalid", "Reserved battle reward is invalid")
        if sum(grant["count"] for grant in grants) > MAX_PLAYER_UNITS:
            raise unavailable("quest_state_invalid", "Reserved battle rewards exceed the technical unit limit")
        return grants

    def credit_units(self, state: dict, grants: list[dict]) -> tuple[dict, list[dict]]:
        if not isinstance(state, dict):
            raise unavailable("player_state_schema", "Player state must be an object")
        if not isinstance(grants, list) or len(grants) > MAX_PLAYER_UNITS:
            raise ProtocolError("unit_grant_invalid", "Unit grants must be a bounded list")
        total = 0
        for grant in grants:
            if (not isinstance(grant, dict) or set(grant) not in (
                    {"card_id", "level", "count"}, {"card_id", "level", "count", "evolution_count"})):
                raise ProtocolError("unit_grant_invalid", "Unit grants require card_id, level, count and an optional initial stage")
            self.validate_grant_identity(grant["card_id"], grant["level"])
            if not int32(grant.get("evolution_count", 0)) or grant.get("evolution_count", 0) not in (0, 2):
                raise ProtocolError("unit_grant_invalid", "The requested initial stage is not supported")
            if not int32(grant["count"]) or grant["count"] <= 0:
                raise ProtocolError("unit_grant_invalid", "Unit grant counts must be positive int32 values")
            total += grant["count"]
        player = self.validate_player(state.get("player_state"))
        if not grants:
            return deepcopy(state), []
        if len(player["units"]) + total > MAX_PLAYER_UNITS:
            raise ProtocolError("unit_roster_capacity", "Unit grant exceeds the local roster capacity", 409)
        get_index = player["battle"]["UX"]
        unique_id = player.get("last_unique_id", max(unit["UniqueID"] for unit in player["units"]))
        if not int32(get_index + total) or not int32(unique_id + total):
            raise ProtocolError("unit_identity_exhausted", "Unit grant would overflow a native identity field", 409)
        new_units = []
        for grant in grants:
            initial = self.initial_unit_values(grant["card_id"], grant["level"],
                                               evolution_count=grant.get("evolution_count", 0))
            for _ in range(grant["count"]):
                get_index += 1
                unique_id += 1
                unit = {name: False if name in UNIT_BOOLEANS else 0 for name, _ in UNIT_FIELDS}
                unit.update(CardID=grant["card_id"], ClassID=initial.class_id, ClassChangeCount=initial.stage,
                            Exp=initial.experience, SkillLevel=1,
                            GetIndex=get_index, UniqueID=unique_id, OrganizationIndex=-1,
                            SettingSkillID=initial.skill_id, GeneralPurposeFlags=1)
                new_units.append(unit)
        result = deepcopy(state)
        updated = result["player_state"]
        updated["units"].extend(deepcopy(new_units))
        updated["battle"]["UX"] = get_index
        updated["last_unique_id"] = unique_id
        self.validate_player(updated)
        if self._unit_response_profile is None:
            self._unit_response_profile = ProtocolRegistry.load().by_registration[41]
        try:
            encode_response(self._unit_response_profile, self.unit_response(updated["units"]))
        except ProtocolError as error:
            if error.code == RESPONSE_TOO_LARGE:
                raise ProtocolError("unit_roster_capacity", "Unit grant would exceed the native roster response capacity", 409) from error
            raise
        reserved = self.quest_reservations(state)
        if reserved:
            trial = deepcopy(result)
            trial["quest_state"]["active"] = None
            self.credit_units(trial, reserved)
        return result, new_units

    def initialize(self, state: dict) -> dict:
        if not isinstance(state, dict):
            raise ValueError("Player state must be an object")
        result = deepcopy(state)
        if "player_state" in result:
            self.validate_player(result["player_state"], references=False)
            return result
        try:
            card = self._cards()[1]
            raid_group = self.raid_group()
        except ProtocolError:
            return result
        unit = {name: False if name in UNIT_BOOLEANS else 0 for name, _ in UNIT_FIELDS}
        unit.update(CardID=1, ClassID=card["InitClassID"], SkillLevel=1, UniqueID=1,
                    SettingSkillID=card["ClassLV0SkillID"], OrganizationIndex=-1)
        result["player_state"] = {"schema_version": 1, "units": [unit],
                                  "battle": {"VN": "1.0.1", "UX": 0, "TN": 0, "RTN": raid_group},
                                  "legacy_squad_imported": False, "squads": []}
        self.validate_player(result["player_state"])
        return result

    @staticmethod
    def _identity(envelope: RequestEnvelope, name: str) -> str:
        nodes = envelope.document.findall(name)
        if len(nodes) != 1:
            raise ProtocolError("invalid_identity", f"Expected one {name}")
        node = nodes[0]
        if (node.attrib != {"T": "S"} or len(node) != 1 or (node.text or "").strip()
                or (node.tail or "").strip()):
            raise ProtocolError("invalid_identity", f"Expected one string value in {name}")
        value = node[0]
        if value.tag != "V" or value.attrib or len(value) or (value.tail or "").strip():
            raise ProtocolError("invalid_identity", f"Expected one string value in {name}")
        return value.text or ""

    def authenticate(self, envelope: RequestEnvelope, store: Store) -> PlayerProfile:
        profile = store.profile()
        if self._identity(envelope, "UI") != profile.player_id:
            raise ProtocolError("invalid_identity", "Request does not match the local player", 403)
        if not secrets.compare_digest(self._identity(envelope, "UK").encode(), profile.user_key.encode()):
            raise ProtocolError("invalid_identity", "Request does not match the local account", 403)
        if not secrets.compare_digest(self._identity(envelope, "MK").encode(), profile.method_key.encode()):
            raise ProtocolError("invalid_method_key", "Request does not match the local method key", 403)
        return profile

    def validate(self, state: dict, *, references: bool = True) -> dict:
        if not isinstance(state, dict):
            raise unavailable("player_state_schema", "Player state must be an object")
        return self.validate_player(state.get("player_state"), references=references)

    def validate_player(self, player, *, references: bool = True) -> dict:
        if not isinstance(player, dict) or type(player.get("schema_version")) is not int or player["schema_version"] != 1:
            raise unavailable("player_state_schema", "Player-state schema is missing or unsupported")
        units = player.get("units")
        if not isinstance(units, list) or not 1 <= len(units) <= MAX_PLAYER_UNITS:
            raise unavailable("player_units_invalid", "A bounded nonempty persisted player roster is required")
        fields = {name for name, _ in UNIT_FIELDS}
        for unit in units:
            if not isinstance(unit, dict) or set(unit) != fields:
                raise unavailable("player_units_invalid", "Persisted unit columns do not match the native contract")
            for name, value in unit.items():
                if (name in UNIT_BOOLEANS and type(value) is not bool) or (name not in UNIT_BOOLEANS and not int32(value)):
                    raise unavailable("player_units_invalid", "Persisted unit contains invalid typed values")
            if (unit["CardID"] <= 0 or unit["ClassID"] <= 0 or unit["UniqueID"] <= 0 or unit["GetIndex"] < 0
                    or any(unit[name] < 0 for name in ("ClassChangeCount", "Exp", "Love", "SkillLevel", "SettingSkillID", "SkillExperience", "CostExperience"))):
                raise unavailable("player_units_invalid", "Persisted unit identifiers or growth values are invalid")
        if len({row["UniqueID"] for row in units}) != len(units) or len({row["GetIndex"] for row in units}) != len(units):
            raise unavailable("player_units_invalid", "Player-unit identities must be unique")
        if "last_unique_id" in player and (not int32(player["last_unique_id"])
                                           or player["last_unique_id"] < max(row["UniqueID"] for row in units)):
            raise unavailable("player_units_invalid", "Unit identity high-water mark is invalid")
        battle = player.get("battle")
        if (not isinstance(battle, dict) or set(battle) != {"VN", "UX", "TN", "RTN"}
                or not isinstance(battle["VN"], str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", battle["VN"])
                or len(battle["VN"].encode()) >= 16 or battle["VN"] < "1.0.1"
                or any(not int32(battle[name]) or battle[name] < 0 for name in ("UX", "TN", "RTN"))
                or battle["UX"] < max(row["GetIndex"] for row in units)
                or battle["TN"] >= MAX_STANDARD_TEAMS or battle["RTN"] > 255):
            raise unavailable("battle_param_invalid", "Battle parameters cannot satisfy the native startup contract")
        if type(player.get("legacy_squad_imported")) is not bool:
            raise unavailable("squad_state_invalid", "Legacy squad import marker is missing")
        self._validate_squads(player.get("squads"), {unit["GetIndex"] for unit in units}, stored=True)
        if references:
            cards = self._cards()
            if any(unit["CardID"] not in cards or unit["ClassID"] not in self.class_ids for unit in units):
                raise unavailable("player_reference_invalid", "Player-unit references do not match current verified master data")
        return player

    def handlers(self) -> dict:
        return {39: self.battle_param, 41: self.player_units, 72: self.load_squads, 73: self.save_squads, 74: self.import_squads}

    def battle_param(self, envelope: RequestEnvelope, store: Store) -> bytes:
        try:
            if "battle_param_schema" in self.issues:
                raise unavailable("player_contract_unavailable", "Verified BattleParam schema is unavailable")
            # 39 carries no card data, and the native startup loop resends it while it fails.
            player = self.validate(self.authenticate(envelope, store).state, references=False)
        except ProtocolError as error:
            log.warning("Battle parameters 39 refused with %s; the native startup loop resends it until it succeeds",
                        error.code)
            raise
        return self.battle_param_response(player)

    @staticmethod
    def battle_param_response(player: dict) -> bytes:
        battle = player["battle"]
        return dumps(Container(children=tuple(Column(name, kind, (battle[name],)) for name, kind in BATTLE_PARAM_FIELDS)))

    def player_units(self, envelope: RequestEnvelope, store: Store) -> bytes:
        units = self.validate(self.authenticate(envelope, store).state)["units"]
        response = self.unit_response(units)
        encode_response(envelope.profile, response)
        return response

    @staticmethod
    def unit_response(units: list[dict]) -> bytes:
        return dumps(Container(children=tuple(Column(alias, ValueType.BOOL if name in UNIT_BOOLEANS else ValueType.INT,
                                                     tuple(unit[name] for unit in units)) for name, alias in UNIT_FIELDS)),
                     limits=UNIT_XML_LIMITS)

    def validate_squads(self, rows, known: set[int], *, stored: bool = False) -> None:
        self._validate_squads(rows, known, stored=stored)

    @staticmethod
    def _validate_squads(rows, known_units: set[int], *, stored: bool = False) -> None:
        status = 503 if stored else 400
        if not isinstance(rows, list) or len(rows) > MAX_SQUAD_ROWS:
            raise ProtocolError("squad_rows_invalid", "Squad rows must be a bounded list", status)
        addresses = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {name for name, _, _ in SQUAD_FIELDS}:
                raise ProtocolError("squad_rows_invalid", "Squad row columns are incomplete", status)
            if any(not int32(row[name]) for name in ("Address", "UnitIndex", "Flags")):
                raise ProtocolError("squad_rows_invalid", "Squad identifiers and flags must be int32", status)
            address = row["Address"] & SQUAD_ADDRESS_MASK
            if (squad_group(row) == RESERVED_SQUAD_GROUP or squad_position(row)[1] > MAX_SQUAD_SLOT
                    or address in addresses):
                raise ProtocolError("squad_rows_invalid", "Invalid or duplicate native squad address", status)
            addresses.add(address)
            if row["UnitIndex"] != EMPTY_SQUAD_SLOT and row["UnitIndex"] not in known_units:
                raise ProtocolError("squad_unit_missing", "Squad member does not reference an owned GetIndex", status)
            if not isinstance(row["Name"], str) or len(row["Name"].encode("utf-8")) > MAX_SQUAD_NAME_BYTES:
                raise ProtocolError("squad_rows_invalid", "Squad name exceeds the local text limit", status)

    @staticmethod
    def _validate_squad_request(envelope: RequestEnvelope, *, allow_tutorial: bool = False, allow_rows: bool = True) -> None:
        root = envelope.document
        if root.tag != "DA" or root.attrib or (root.text or "").strip() or (root.tail or "").strip():
            raise ProtocolError("squad_request_invalid", "Expected an untyped squad request container")
        allowed = set(COMMON_FIELDS)
        if allow_rows:
            allowed.update(wire for _, wire, _ in SQUAD_FIELDS)
        if allow_tutorial:
            allowed.add("TL")
        if any(node.tag not in allowed or (node.tail or "").strip() for node in root):
            raise ProtocolError("squad_request_invalid", "Unexpected squad request field or mixed text")

    @staticmethod
    def _request_squads(envelope: RequestEnvelope, *, allow_absent: bool = False) -> list[dict]:
        if allow_absent and not any(envelope.document.findall(wire) for _, wire, _ in SQUAD_FIELDS):
            return []
        columns = []
        for _, wire, kind in SQUAD_FIELDS:
            nodes = envelope.document.findall(wire)
            if len(nodes) != 1:
                raise ProtocolError("squad_request_invalid", f"Expected one {wire} column")
            node = nodes[0]
            if node.attrib != {"T": kind.value}:
                raise ProtocolError("squad_request_invalid", "Unexpected squad column type")
            root = ET.Element("DA")
            root.append(deepcopy(node))
            try:
                column = loads(ET.tostring(root, encoding="utf-8")).children[0]
            except ValueError as error:
                raise ProtocolError("squad_request_invalid", "Invalid typed squad column") from error
            columns.append(column.values)
        lengths = {len(column) for column in columns}
        if len(lengths) != 1 or not 0 < len(columns[0]) <= MAX_SQUAD_ROWS:
            raise ProtocolError("squad_request_invalid", "Squad columns must have equal nonzero row counts")
        return [dict(zip((name for name, _, _ in SQUAD_FIELDS), row)) for row in zip(*columns)]

    @staticmethod
    def _request_squad_tutorial(envelope: RequestEnvelope) -> bool:
        nodes = envelope.document.findall("TL")
        if not nodes:
            return False
        if len(nodes) != 1 or nodes[0].attrib != {"T": "I"}:
            raise ProtocolError("squad_tutorial_invalid", "Expected one integer TL value")
        root = ET.Element("DA")
        root.append(deepcopy(nodes[0]))
        try:
            column = loads(ET.tostring(root, encoding="utf-8")).children[0]
        except ValueError as error:
            raise ProtocolError("squad_tutorial_invalid", "Invalid typed TL value") from error
        if column.values != (1,):
            raise ProtocolError("squad_tutorial_invalid", "TL must contain exactly one value equal to 1")
        return True

    @staticmethod
    def squad_response(profile: PlayerProfile, player: dict) -> bytes:
        system = Container("SYSTEM", (Column("A1", ValueType.STRING, ("ExchangeKey", "PaymentTransaction")),
                                      Column("A2", ValueType.STRING, (profile.method_key, "0"))))
        battle = Container("PBP", (Column("A1", ValueType.STRING, ("UX",)), Column("A2", ValueType.INT, (player["battle"]["UX"],))))
        rows = player["squads"]
        squads = Container("TGI", tuple(Column(wire, kind, tuple(row[name] for row in rows)) for name, wire, kind in SQUAD_FIELDS))
        return dumps(Container(children=(system, battle, squads)))

    def load_squads(self, envelope: RequestEnvelope, store: Store) -> bytes:
        profile = self.authenticate(envelope, store)
        self._validate_squad_request(envelope, allow_rows=False)
        response = self.squad_response(profile, self.validate(profile.state))
        encode_response(envelope.profile, response)
        return response

    def replace_squads(self, state: dict, rows: list[dict], *, tutorial: bool = False,
                       vacate_consumed: bool = True) -> SquadReplacement:
        groups = self._squad_metadata(rows)
        player = self.validate(state)
        known_units = {unit["GetIndex"] for unit in player["units"]}
        saved_rows, vacated = (self._vacate_consumed(rows, known_units, player["battle"]["UX"]) if vacate_consumed
                               else (rows, set()))
        self._validate_squads(saved_rows, known_units)
        if self.squad_emblem_validator is not None:
            for group, (_, emblem) in groups.items():
                if emblem not in {row["Flags"] for row in player["squads"] if squad_group(row) == group}:
                    self.squad_emblem_validator(emblem, state)
        stored_units = {unit["GetIndex"] for unit in player["units"] if barracks_index(unit)}
        if any(row["UnitIndex"] in stored_units for row in saved_rows):
            raise ProtocolError("squad_unit_in_barracks", "Move stored units to the first barracks before assigning them", 409)
        updated = deepcopy(state)
        if saved_rows:
            # Native 54D6F0 restores every changed top-level squad before serializing it.
            retained = [row for row in player["squads"] if squad_group(row) not in groups]
            updated["player_state"].update(squads=retained + saved_rows, legacy_squad_imported=True)
            self._validate_squads(updated["player_state"]["squads"], known_units)
        if tutorial:
            flags = configuration_row(updated)["MenuTutorialFlags"]
            updated = patch_configuration(updated, {"MenuTutorialFlags": flags | SQUAD_TUTORIAL_FLAGS})
        return SquadReplacement(updated, tuple(sorted(vacated)))

    def save_squads(self, envelope: RequestEnvelope, store: Store) -> bytes:
        profile = self.authenticate(envelope, store)
        tutorial = self._request_squad_tutorial(envelope)
        self._validate_squad_request(envelope, allow_tutorial=True)
        rows = self._request_squads(envelope, allow_absent=True)
        for _ in range(3):
            replacement = self.replace_squads(profile.state, rows, tutorial=tutorial)
            response = self.squad_response(profile, replacement.state["player_state"])
            encode_response(envelope.profile, response)
            try:
                if replacement.state != profile.state:
                    store.update_state(replacement.state, profile.revision)
            except ValueError:
                profile = self.authenticate(envelope, store)
                continue
            if replacement.vacated:
                log.warning("Squad save 73 emptied slots of consumed units %s that the native squad storage still listed; "
                            "the reply snapshot clears them", list(replacement.vacated))
            return response
        raise ProtocolError("profile_changed", "Player state changed during squad save", 409)

    @staticmethod
    def _squad_metadata(rows: list[dict]) -> dict[int, tuple[str, int]]:
        groups = {}
        for row in rows:
            metadata = row["Name"], row["Flags"]
            if groups.setdefault(squad_group(row), metadata) != metadata:
                raise ProtocolError("squad_metadata_invalid", "Rows in one squad must share name and flags")
        return groups

    @staticmethod
    def _vacate_consumed(rows: list[dict], known_units: set[int], highest: int) -> tuple[list[dict], set[int]]:
        # GetIndex values never repeat, so an absent member at or below UX was sold, deposited or used as material.
        vacated = {row["UnitIndex"] for row in rows
                   if row["UnitIndex"] not in known_units and 0 <= row["UnitIndex"] <= highest}
        return [{**row, "UnitIndex": EMPTY_SQUAD_SLOT} if row["UnitIndex"] in vacated else row for row in rows], vacated

    @staticmethod
    def _legacy_squads(rows: list[dict]) -> list[dict]:
        # Native 54D260 pads a migrated team with empty rows from its member count, overlapping occupied slots.
        squads, padded = {}, set()
        for row in rows:
            address = row["Address"] & SQUAD_ADDRESS_MASK
            first = squads.setdefault(address, row)
            if first is row:
                continue
            if (address in padded or row["UnitIndex"] != EMPTY_SQUAD_SLOT or first["UnitIndex"] == EMPTY_SQUAD_SLOT
                    or (row["Name"], row["Flags"]) != (first["Name"], first["Flags"])):
                raise ProtocolError("squad_rows_invalid", "Invalid or duplicate native squad address")
            padded.add(address)
        return list(squads.values())

    def import_legacy_squads(self, state: dict, rows: list[dict]) -> dict:
        player = self.validate(state)
        if player["legacy_squad_imported"]:
            return state
        squads = self._legacy_squads(rows)
        self._validate_squads(squads, {unit["GetIndex"] for unit in player["units"]})
        updated = deepcopy(state)
        updated["player_state"].update(squads=squads, legacy_squad_imported=True)
        return updated

    def import_squads(self, envelope: RequestEnvelope, store: Store) -> bytes:
        profile = self.authenticate(envelope, store)
        self._validate_squad_request(envelope)
        rows = self._request_squads(envelope)
        for _ in range(3):
            updated = self.import_legacy_squads(profile.state, rows)
            response = self.squad_response(profile, updated["player_state"])
            # A save that already imported answers its stored squads as they are, without the encoding check of a new import.
            if updated is profile.state:
                return response
            encode_response(envelope.profile, response)
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                profile = self.authenticate(envelope, store)
        raise ProtocolError("profile_changed", "Player state changed during legacy squad import", 409)
