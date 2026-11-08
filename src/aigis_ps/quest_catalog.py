from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import hashlib
from importlib.resources import files
import json
import re
import threading
from types import MappingProxyType
from typing import Literal, NamedTuple

from . import quest_modes
from .al_format import ParseLimits, decompress_all4, parse_archive, parse_table
from .drop_bonus import RESOURCE as DROP_BONUS_RESOURCE, DropBonus, DropBonusTable, DropBonusUnavailable
from .errors import ProtocolError
from .masterdata import MasterSnapshot
from .mission_tags import REGISTRY_CATEGORIES, REGISTRY_TABLES, TAG_COLUMN, MissionTagIndex, mission_tables
from .native_numbers import INT32_MAX, INT32_MIN, int32
from .possessions import ITEM_UNKNOWN as POSSESSION_ITEM_UNKNOWN, PossessionService
from .public_tables import validate_public_master
from .quest_entries import (
    STAR_UNBOUNDED, UNCONDITIONAL, EnemyBand, StarInterval, UnsupportedAppearance, classify_enemy, row_interval,
)
from .quest_protocol import MAX_PRIZE_SLOT
from .system_literals import command_id


MAX_CANDIDATES = 4096
# Only prize slots 1–5 map to Treasure1–5 (4412F0) and to record slot bits (65D9F0).
TREASURE_SLOTS = 5
# 4A1A60 sets RT ATTR bit 1 for prize enemies 7000-7999.
SPECIAL_ENEMY_ATTR = 2
ITEM_TABLE = "ItemConfigTable.atb"
RESOURCE_LIMITS = ParseLimits(max_input_bytes=64 * 1024 * 1024, max_decompressed_bytes=64 * 1024 * 1024)
TABLE_SUFFIX, ARCHIVE_SUFFIX = ".atb", ".aar"
PATH_SEPARATORS = ("/", "\\")
UNIT_REWARD_CODES = MappingProxyType({1: range(1, 1001), 2: range(1000001, 2000001)})
GIFT_DROP_TYPE = 3
ITEM_DROP_TYPES = frozenset((4, 6, 7, 8))
EVENT_PARAM_DROP_TYPE = 5
GIFT_REWARD_TYPES = range(100, 108)
GIFT_EFFECT_TYPE = 4
ITEM_REWARD_KIND = 22
HARMONY_TYPE = 10
HARMONY_CRYSTALS = 3
TIMED_FIELDS = ("StartOpenDay", "EndOpenDay", "OpenWeek", "_HoldingParam")
RAID_TEAM_FIELD = "IsUseRaidTeam"
DEFINITION_COLUMNS = MappingProxyType({
    "prerequisite": "AppearCondition", "charisma": "Charisma", "action_points": "ActionPoint", "rank": "Rank",
    "rank_exp": "RankExp", "gold": "Gold", "unit_level": "UnitLevel", "map_no": "MapNo", "entry_no": "EntryNo",
    "quest_terms": "QuestTerms", "unit_list": "UnitList", "location_no": "LocationNo",
})
CARD_TRAITS = ("Genus", "Assign", "Kind")
QUEST_ID_ITEM = re.compile(r"[0-9]{1,10}\Z")


@dataclass(frozen=True)
class Reward:
    kind: Literal["unit", "item", "event_param", "none"]
    code: int
    count: int
    card_id: int | None = None
    item_id: str | None = None
    drop_type: int | None = None


@dataclass(frozen=True)
class DropCandidate:
    slot: int
    entry_row: int
    loop_index: int
    enemy_id: int
    special_attr: int
    min_stars: int = 0
    max_stars: int = STAR_UNBOUNDED
    override_code: int = 0

    @property
    def star_interval(self) -> StarInterval:
        return StarInterval(self.min_stars, self.max_stars)

    @property
    def conditional(self) -> bool:
        return self.star_interval != UNCONDITIONAL


@dataclass(frozen=True)
class QuestDefinition:
    quest_id: int
    prerequisite: int
    charisma: int
    action_points: int
    rank: int
    rank_exp: int
    gold: int
    unit_level: int
    treasures: tuple[int, ...]
    candidates: tuple[DropCandidate, ...]
    map_no: int
    entry_no: int
    quest_terms: int
    unit_list: int
    location_no: int

    @property
    def conditional(self) -> bool:
        return any(candidate.conditional for candidate in self.candidates)

    def candidates_for(self, max_rank: int) -> tuple[DropCandidate, ...]:
        if not int32(max_rank) or max_rank < 0:
            raise ProtocolError("quest_state_invalid", "Prize candidates require the record's raw nonnegative MaxRank", 503)
        return tuple(candidate for candidate in self.candidates if candidate.star_interval.covers(max_rank))

    def treasure_code(self, candidate: DropCandidate) -> int:
        if candidate.conditional or candidate.override_code:
            raise ProtocolError("quest_candidate_unsupported",
                                "Conditional prize candidates require candidates_for and reward_code", 501)
        return self._slot_treasure(candidate.slot)

    def reward_code(self, candidate: DropCandidate) -> int:
        return candidate.override_code or self._slot_treasure(candidate.slot)

    def marks_slot(self, candidate: DropCandidate) -> bool:
        # 460020 skips the 65D9F0 slot bit for a drop that 5D33E0 overrode.
        return not candidate.override_code and candidate.slot <= TREASURE_SLOTS

    def _slot_treasure(self, slot: int) -> int:
        # Only Treasure1-5 exist; later prize slots and empty columns reward nothing.
        return self.treasures[slot - 1] if slot <= TREASURE_SLOTS else 0


@dataclass(frozen=True)
class StarReward:
    kind: Literal["crystal", "unit", "item", "none", "unsupported"]
    reward_type: int
    count: int
    card_id: int | None = None
    item_id: str | None = None


@dataclass(frozen=True)
class HardModeSpec:
    level: int
    condition: int
    information: int
    rank: int


@dataclass(frozen=True)
class FixedUnit:
    card_id: int
    cost_mod: int


@dataclass(frozen=True)
class QuestMode:
    quest_type: int
    mission_category: int | None
    mission_ids: tuple[int, ...]
    hard: HardModeSpec | None
    star_rewards_normal: tuple[StarReward, ...]
    star_rewards_hard: tuple[StarReward, ...]
    unit_list: int
    gold_counted: bool
    score_mode: bool
    map_archive: str
    map_mission_id: int | None
    drop_bonus: DropBonus | None = None


class _MissionTable(NamedTuple):
    rows: tuple[Mapping, ...]
    columns: frozenset[str]
    sha256: str


def integer(value: object, *, minimum: int = 0) -> int:
    if not int32(value) or value < minimum:
        raise ValueError("Expected a bounded native integer")
    return value


def _item_name(value: object) -> str | None:
    if not isinstance(value, str) or len(value.encode("utf-8")) >= 32:
        raise ValueError("Reward item identifiers must be bounded native strings")
    return None if value in ("", "null") else value


def _resource_name(value: object, suffix: str) -> bool:
    return isinstance(value, str) and value.endswith(suffix) and not any(mark in value for mark in PATH_SEPARATORS)


def _trait_index(rows: list[dict], field: str) -> Mapping[int, frozenset[int]]:
    groups = {}
    for row in rows:
        groups.setdefault(integer(row.get(field), minimum=INT32_MIN), set()).add(row["CardID"])
    return MappingProxyType({key: frozenset(value) for key, value in groups.items()})


class QuestCatalog:
    def __init__(self, snapshot: MasterSnapshot, asset_reader: Callable[[str], bytes], *,
                 possessions: PossessionService, resource_version: str, window_overrides=None):
        if not isinstance(snapshot, MasterSnapshot) or not callable(asset_reader):
            raise ValueError("A fixed master snapshot and asset reader are required")
        if not isinstance(resource_version, str) or not resource_version or len(resource_version) > 128:
            raise ValueError("A bounded resource version is required")
        self.snapshot = snapshot
        self.resource_version = resource_version
        self._asset_reader = asset_reader
        self._possessions = possessions
        self._window_overrides = quest_modes.validate_window_overrides(window_overrides)
        self._lock = threading.RLock()
        self._definitions: dict[int, QuestDefinition] = {}
        self._entries: dict[tuple[str, str], tuple[DropCandidate, ...]] = {}
        self._excluded: dict[str, Mapping[str, int]] = {}
        self._entry_hashes: dict[str, str] = {}
        self._rewards: dict[int, Reward] = {0: Reward("none", 0, 0)}
        self._drop_rows = None
        self._drop_bonus: DropBonusTable | None = None
        self._failures = {}
        self._mode_definitions: dict[int, QuestDefinition] = {}
        self._mode_failures = {}
        self._mode_entry_hashes: dict[str, str] = {}
        self._entry_digests: dict[str, str] = {}
        self._modes: dict[int, QuestMode] = {}
        self._windows = {}
        self._mission_rows: dict[str, _MissionTable] = {}
        self._tags: MissionTagIndex | None = None
        self._resource_hashes: dict[str, str] = {}
        self._special_conditions = None
        self._unit_lists = None
        self._gift_items = None
        self._texts = None
        self._quest_names: dict[int, tuple] = {}
        self._playable: dict[int, tuple[int, ...]] = {}
        self._resource_rows: dict[str, tuple[Mapping, ...]] = {}
        self._member_rows: dict[tuple[str, str], tuple[Mapping, ...]] = {}
        schemas = json.loads(files("aigis_ps").joinpath("data/master-schemas.json").read_text("utf-8"))
        schema_fields = {name: [field["name"] for field in table["fields"]] for name, table in schemas["tables"].items()}
        try:
            tables = {}
            for name in ("CardData", "QuestTable"):
                document, _ = validate_public_master(name, snapshot.get_xml(name), schema_fields)
                names = [column.name for column in document.children]
                tables[name] = [dict(zip(names, values)) for values in zip(*(column.values for column in document.children))]
            cards = tables["CardData"]
            self._card_ids = frozenset(row["CardID"] for row in cards)
            self._card_traits = MappingProxyType({field: _trait_index(cards, field) for field in CARD_TRAITS
                                                  if cards and field in cards[0]})
            self._all_rows = {row["QuestID"]: row for row in tables["QuestTable"]}
            self._rows = {key: row for key, row in self._all_rows.items() if integer(row.get("Type")) == 0}
        except (ValueError, KeyError) as error:
            raise ProtocolError("quest_master_invalid", "Quest catalog requires verified public master columns", 503) from error

    @property
    def all_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._rows))

    @property
    def mode_ids(self) -> tuple[int, ...]:
        return tuple(sorted(key for key, row in self._all_rows.items()
                            if row["Type"] != 0 and quest_modes.policy(row["Type"]).enabled))

    def row(self, quest_id: int) -> Mapping:
        if type(quest_id) is not int or quest_id not in self._all_rows:
            raise ProtocolError("quest_not_found", "Quest is absent from the fixed master snapshot", 404)
        return MappingProxyType(self._all_rows[quest_id])

    def quest_type(self, quest_id: int) -> int | None:
        row = self._all_rows.get(quest_id) if type(quest_id) is int else None
        return None if row is None else row["Type"]

    def card_ids_with(self, field: str, value: int) -> frozenset[int]:
        if field not in self._card_traits:
            raise ProtocolError("quest_card_trait_unknown", "CardData does not provide this card trait", 503)
        return self._card_traits[field].get(value, frozenset())

    def resource_rows(self, name: str) -> tuple[Mapping, ...]:
        if not _resource_name(name, TABLE_SUFFIX):
            raise ProtocolError("quest_mode_resource_invalid", "Mode resources must be named native tables", 503)
        with self._lock:
            if name not in self._resource_rows:
                self._resource_rows[name] = tuple(MappingProxyType(dict(row)) for row in self._table(name))
            return self._resource_rows[name]

    def archive_rows(self, archive: str, member: str) -> tuple[Mapping, ...]:
        if not _resource_name(archive, ARCHIVE_SUFFIX) or not _resource_name(member, TABLE_SUFFIX):
            raise ProtocolError("quest_mode_resource_invalid", "Mode resources must name a native archive and table", 503)
        key = (archive, member)
        with self._lock:
            if key not in self._member_rows:
                data = self._asset(archive)
                try:
                    _, rows = self._member_table(data, member)
                except ValueError as error:
                    raise ProtocolError("quest_mode_resource_invalid",
                                        f"Quest mode resource is not a native table: {archive}/{member}", 503) from error
                self._resource_hashes[archive] = hashlib.sha256(data).hexdigest()
                self._member_rows[key] = tuple(MappingProxyType(dict(row)) for row in rows)
            return self._member_rows[key]

    def _system_texts(self) -> tuple:
        with self._lock:
            if self._texts is None:
                rows = self._table("SystemText.atb")
                try:
                    self._texts = tuple(row["Data_Text"] for row in rows)
                except KeyError as error:
                    raise ProtocolError("quest_mode_resource_invalid", "SystemText rows require their Data_Text column", 503) from error
            return self._texts

    def system_text(self, index: int) -> str | None:
        # Text identifiers address SystemText rows by position; ten rows carry a different Recode_Index.
        texts = self._system_texts()
        text = texts[index] if type(index) is int and 0 <= index < len(texts) else None
        return text if isinstance(text, str) else None

    def _quest_name_rows(self, mission_id: int) -> tuple:
        with self._lock:
            if mission_id not in self._quest_names:
                rows = self._table(f"QuestNameText{mission_id:06d}.atb")
                try:
                    self._quest_names[mission_id] = tuple(row["Message"] for row in rows)
                except KeyError as error:
                    raise ProtocolError("quest_mode_resource_invalid", "QuestNameText rows require their Message column", 503) from error
            return self._quest_names[mission_id]

    def quest_titles(self, quest_id: int) -> tuple[str, ...]:
        row = self.row(quest_id)
        try:
            index = integer(row.get("QuestTitle"))
        except ValueError as error:
            raise ProtocolError("quest_definition_invalid", "Quest title index must be a nonnegative native integer", 503) from error
        titles = []
        # Titles live in each mission's own QuestNameText, so a quest listed by several missions may show several.
        for mission in self.mission_windows(row["Type"]).missions(quest_id):
            names = self._quest_name_rows(mission.mission_id) if mission.listed else ()
            title = names[index] if index < len(names) else None
            if isinstance(title, str) and title not in titles:
                titles.append(title)
        return tuple(titles)

    def playable_quests(self, quest_type: int) -> tuple[int, ...]:
        with self._lock:
            if quest_type not in self._playable:
                windows = self.mission_windows(quest_type)
                self._playable[quest_type] = tuple(sorted(
                    quest_id for quest_id, row in self._all_rows.items()
                    if row["Type"] == quest_type and any(mission.listed for mission in windows.missions(quest_id))))
            return self._playable[quest_type]

    def status(self) -> dict:
        with self._lock:
            counts = {}
            for row in self._all_rows.values():
                counts[row["Type"]] = counts.get(row["Type"], 0) + 1
            return {"ready": not self._failures, "master_snapshot": self.snapshot.identifier,
                    "client_hash": self.snapshot.client_hash, "resource_version": self.resource_version,
                    "indexed_main_quests": len(self._rows), "validated_quests": len(self._definitions),
                    "validated_entries": len(self._entries), "decoded_rewards": len(self._rewards),
                    "all_maps_validated": len(self._definitions) == len(self._rows) and not self._failures,
                    "validation_scope": "Type 0 master rows indexed; map and reward validation occurs on get",
                    "failed_quests": {key: dict(value) for key, value in self._failures.items()},
                    "entry_sha256": dict(self._entry_hashes),
                    "indexed_mode_quests": len(self.mode_ids), "validated_mode_quests": len(self._mode_definitions),
                    "described_modes": len(self._modes),
                    "mode_failed_quests": {key: dict(value) for key, value in self._mode_failures.items()},
                    "mode_entry_sha256": dict(self._mode_entry_hashes),
                    "mode_resource_sha256": dict(self._resource_hashes),
                    "window_overrides": sorted(self._window_overrides),
                    "modes": quest_modes.support_status(counts),
                    "excluded_prize_rows": {label: dict(bands) for label, bands in sorted(self._excluded.items())},
                    "conditional_quests": sorted(quest_id for cache in (self._definitions, self._mode_definitions)
                                                 for quest_id, quest in cache.items() if quest.conditional),
                    "drop_bonus": self._drop_bonus_summary()}

    def _drop_bonus_summary(self) -> dict:
        if self._drop_bonus is None:
            return {"loaded": False, "available": None, "error": None, "failures": 0, "sha256": None}
        status = self._drop_bonus.status()
        return {"loaded": True, "available": status["available"], "error": status["error"],
                "failures": len(status["failures"]), "sha256": status["sha256"]}

    @staticmethod
    def _unpack(data: bytes) -> bytes:
        if not isinstance(data, bytes) or len(data) > RESOURCE_LIMITS.max_input_bytes:
            raise ValueError("Resource payload must be bounded bytes")
        return decompress_all4(data, RESOURCE_LIMITS) if data.startswith(b"ALL4") else data

    def _asset(self, name: str) -> bytes:
        try:
            return self._unpack(self._asset_reader(name))
        except (ValueError, OSError, KeyError, ProtocolError) as error:
            raise ProtocolError("quest_asset_unavailable", f"Verified quest resource is unavailable: {name}", 503) from error

    @classmethod
    def _member_table(cls, data: bytes, name: str) -> tuple[bytes, list[dict]]:
        members = [member for member in parse_archive(data, RESOURCE_LIMITS).members if member.name == name]
        if len(members) != 1:
            raise ValueError("Archive member is missing or ambiguous")
        member = members[0]
        payload = cls._unpack(data[member.offset:member.offset + member.size])
        return payload, parse_table(payload, RESOURCE_LIMITS)["rows"]

    def _parsed_table(self, name: str) -> tuple[dict, str]:
        data = self._asset(name)
        try:
            parsed = parse_table(data, RESOURCE_LIMITS)
        except ValueError as error:
            raise ProtocolError("quest_mode_resource_invalid", f"Quest mode resource is not a native table: {name}", 503) from error
        digest = hashlib.sha256(data).hexdigest()
        self._resource_hashes[name] = digest
        return parsed, digest

    def _table(self, name: str) -> list[dict]:
        return self._parsed_table(name)[0]["rows"]

    def _drop_table(self) -> tuple[dict, ...]:
        if self._drop_rows is None:
            try:
                rows = parse_table(self._asset("ItemDropConfigTable.atb"), RESOURCE_LIMITS)["rows"]
                seen = set()
                for row in rows:
                    identifier = integer(row.get("DropID"), minimum=1)
                    upper = integer(row.get("DropRange"))
                    integer(row.get("DropType"), minimum=1)
                    integer(row.get("DropCount"), minimum=1)
                    integer(row.get("ItemID"), minimum=INT32_MIN)
                    if identifier in seen or upper and upper < identifier:
                        raise ValueError("Reward ranges or identifiers are invalid")
                    seen.add(identifier)
                if not rows:
                    raise ValueError("Reward configuration is empty")
                self._drop_rows = tuple(rows)
            except ProtocolError:
                raise
            except ValueError as error:
                raise ProtocolError("quest_reward_config_invalid", "ItemDropConfig does not satisfy the native reward contract", 503) from error
        return self._drop_rows

    def _stocked_item(self, item_hash: int) -> str:
        try:
            item_id = self._possessions.resolve_hash(item_hash)
        except ProtocolError as error:
            if error.code != POSSESSION_ITEM_UNKNOWN:
                raise
            raise ProtocolError("quest_reward_unsupported", "Reward item does not resolve to a verified definition", 501) from error
        return item_id

    def decode_reward(self, code: int) -> Reward:
        try:
            integer(code)
        except ValueError as error:
            raise ProtocolError("quest_reward_invalid", "Reward code must be a nonnegative native integer") from error
        with self._lock:
            if code in self._rewards:
                return self._rewards[code]
            rows = self._drop_table()
            matched = [row for row in rows if row["DropRange"] > row["DropID"] and row["DropID"] <= code <= row["DropRange"]]
            if not matched:
                matched = [row for row in rows if row["DropID"] == code]
            if len(matched) != 1:
                raise ProtocolError("quest_reward_unknown", "Reward code has no unambiguous native configuration", 503)
            row = matched[0]
            drop_type, card_id, item_id = row["DropType"], None, None
            if drop_type in UNIT_REWARD_CODES:
                if code not in UNIT_REWARD_CODES[drop_type]:
                    raise ProtocolError("quest_reward_unsupported", "Unit reward encoding is outside the verified ranges", 501)
                if code % 1000000 // 100000 != 0:
                    raise ProtocolError("quest_reward_unsupported", "Promoted unit reward variants are not implemented", 501)
                kind, card_id = "unit", code % 10000
                if card_id not in self._card_ids:
                    raise ProtocolError("quest_reward_unknown", "Unit reward does not reference a known card", 503)
            elif drop_type == GIFT_DROP_TYPE:
                kind, item_id = "item", self._possessions.resolve_hash(row["ItemID"])
            elif drop_type in ITEM_DROP_TYPES:
                kind, item_id = "item", self._stocked_item(row["ItemID"])
            elif drop_type == EVENT_PARAM_DROP_TYPE:
                kind = "event_param"
            else:
                raise ProtocolError("quest_reward_unsupported", f"Reward type {drop_type} is not implemented", 501)
            reward = Reward(kind, code, row["DropCount"], card_id=card_id, item_id=item_id, drop_type=drop_type)
            self._rewards[code] = reward
            return reward

    def _map_archive(self, quest_id: int, map_no: int, spec: quest_modes.ModeSpec) -> tuple[str, int | None]:
        if not quest_modes.PREFIXED_MAP_CATEGORIES.intersection(spec.categories):
            return quest_modes.map_archive(map_no), None
        return self.mission_windows(spec.quest_type).archive(quest_id, map_no)

    def _candidates(self, map_name: str, entry_no: int, *, main: bool = True) -> tuple[DropCandidate, ...]:
        entry_name = f"Entry{entry_no:02d}.atb"
        key, label = (map_name, entry_name), f"{map_name}/{entry_name}"
        if key in self._entries:
            (self._entry_hashes if main else self._mode_entry_hashes)[label] = self._entry_digests[label]
            return self._entries[key]
        data = self._asset(map_name)
        excluded = Counter()
        try:
            payload, rows = self._member_table(data, entry_name)
            candidates = []
            for index, row in enumerate(rows):
                slot = integer(row.get("PrizeCardID"))
                if slot == 0:
                    continue
                enemy_id = integer(row.get("EnemyID"), minimum=1)
                count = integer(row.get("Loop"), minimum=1)
                # The client never reads PrizeEnemySpawnPercent; only its integer form is part of the entry contract.
                integer(row.get("PrizeEnemySpawnPercent"))
                drop_override = integer(row.get("PrizeEnemyDropPercent"))
                if slot > MAX_PRIZE_SLOT:
                    raise ProtocolError("quest_candidate_unsupported", "Prize rows name a slot beyond the native RT range", 501)
                band, interval = classify_enemy(enemy_id), row_interval(row)
                if interval is None:
                    excluded[band.value] += 1
                    continue
                if len(candidates) + count > MAX_CANDIDATES:
                    raise ValueError("Map exceeds the bounded native candidate count")
                special = band is EnemyBand.SPECIAL
                # Native 5D33E0 consumes the drop override only for enemies 7000–7999.
                attribute, override = (SPECIAL_ENEMY_ATTR, drop_override) if special else (0, 0)
                candidates.extend(DropCandidate(slot, index, loop_index, enemy_id, attribute, interval.minimum,
                                                interval.maximum, override) for loop_index in range(count))
        except ProtocolError:
            raise
        except UnsupportedAppearance as error:
            raise ProtocolError("quest_candidate_unsupported", f"{error}: {label}", 501) from error
        except ValueError as error:
            raise ProtocolError("quest_entry_invalid", f"Map entry does not satisfy the verified contract: {map_name}/{entry_name}", 503) from error
        result = tuple(candidates)
        self._entries[key] = result
        if excluded:
            self._excluded[label] = MappingProxyType(dict(sorted(excluded.items())))
        self._entry_digests[label] = hashlib.sha256(payload).hexdigest()
        (self._entry_hashes if main else self._mode_entry_hashes)[label] = self._entry_digests[label]
        return result

    def get(self, quest_id: int) -> QuestDefinition:
        try:
            integer(quest_id, minimum=1)
        except ValueError as error:
            raise ProtocolError("quest_id_invalid", "Quest identifier must be a positive native integer") from error
        with self._lock:
            if quest_id in self._definitions:
                return self._definitions[quest_id]
            if quest_id in self._mode_definitions:
                return self._mode_definitions[quest_id]
            if quest_id in self._rows:
                return self._recorded(self._definitions, self._failures, quest_id, "Main quest contains invalid native fields",
                                      lambda: self._definition(quest_id, self._rows[quest_id], main=True))
            if quest_id not in self._all_rows:
                raise ProtocolError("quest_not_found", "Quest is absent from the fixed master snapshot", 404)
            row = self._all_rows[quest_id]
            quest_modes.policy(row["Type"]).require_enabled()
            return self._recorded(self._mode_definitions, self._mode_failures, quest_id, "Mode quest contains invalid native fields",
                                  lambda: self._definition(quest_id, row, main=False))

    def _definition(self, quest_id: int, row: dict, *, main: bool) -> QuestDefinition:
        policy = quest_modes.policy(row["Type"])
        values = {field: integer(row.get(column)) for field, column in DEFINITION_COLUMNS.items()}
        prerequisite = values["prerequisite"]
        if values["map_no"] == 0 or prerequisite == quest_id or (main and values["rank"] == 0):
            raise ValueError("Quest map, rank or prerequisite is invalid")
        if prerequisite and prerequisite not in (self._rows if main else self._all_rows):
            raise ProtocolError("quest_prerequisite_unsupported", "Quest prerequisite is not indexed alongside this quest", 501)
        if not policy.timed_conditions and any(integer(row.get(name, 0), minimum=INT32_MIN) for name in TIMED_FIELDS):
            raise ProtocolError("quest_condition_unsupported", "Timed quest conditions require a mode that implements them", 501)
        if integer(row.get(RAID_TEAM_FIELD, 0), minimum=INT32_MIN):
            raise ProtocolError("quest_condition_unsupported", "Raid team quests are not implemented", 501)
        policy.validate_row(MappingProxyType(row), self)
        treasures = tuple(integer(row.get(f"Treasure{slot}")) for slot in range(1, TREASURE_SLOTS + 1))
        for code in treasures:
            self.decode_reward(code)
        archive, _ = self._map_archive(quest_id, values["map_no"], self._mode_spec(row["Type"]))
        candidates = self._candidates(archive, values["entry_no"], main=main)
        for code in dict.fromkeys(candidate.override_code for candidate in candidates if candidate.override_code):
            self.decode_reward(code)
        return QuestDefinition(quest_id=quest_id, treasures=treasures, candidates=candidates, **values)

    @staticmethod
    def _recorded[T](cache: dict[int, T], failures: dict, quest_id: int, invalid_message: str,
                     build: Callable[[], T]) -> T:
        try:
            result = build()
        except ProtocolError as error:
            failures[quest_id] = {"code": error.code, "message": str(error)}
            raise
        except ValueError as error:
            failure = ProtocolError("quest_definition_invalid", invalid_message, 503)
            failures[quest_id] = {"code": failure.code, "message": str(failure)}
            raise failure from error
        cache[quest_id] = result
        failures.pop(quest_id, None)
        return result

    @staticmethod
    def _mode_spec(quest_type: int) -> quest_modes.ModeSpec:
        spec = quest_modes.MODE_SPECS.get(quest_type)
        if spec is None:
            raise ProtocolError("quest_mode_unsupported", "Quest type has no verified mode specification", 501)
        return spec

    def mode(self, quest_id: int) -> QuestMode:
        quest = self.get(quest_id)
        with self._lock:
            if quest_id in self._modes:
                return self._modes[quest_id]
            row = self._all_rows[quest_id]
            spec = self._mode_spec(row["Type"])
            return self._recorded(self._modes, self._mode_failures, quest_id, "Quest mode fields are invalid",
                                  lambda: self._mode_description(quest, row, spec))

    def _mode_description(self, quest: QuestDefinition, row: Mapping, spec: quest_modes.ModeSpec) -> QuestMode:
        windows = self.mission_windows(spec.quest_type)
        level = integer(row.get("_HardLevel"))
        hard = None
        if level and spec.hard_rank is not None:
            hard = HardModeSpec(level, integer(row.get("_HardCondition"), minimum=INT32_MIN),
                                integer(row.get("_HardInfomation"), minimum=INT32_MIN), spec.hard_rank)
        archive, owner = self._map_archive(quest.quest_id, quest.map_no, spec)
        # 461E80 records no star bit for the score types 45A7D0 flags, so score quests never reach a first-star reward.
        normal = () if spec.score_mode else self.star_rewards(row, False)
        return QuestMode(spec.quest_type, spec.mission_category,
                         tuple(mission.mission_id for mission in windows.missions(quest.quest_id)), hard,
                         normal, self.star_rewards(row, True) if hard is not None else (),
                         quest.unit_list, spec.gold_counted, spec.score_mode, archive, owner,
                         self.drop_bonus(quest.quest_id))

    def drop_bonus_table(self) -> DropBonusTable:
        with self._lock:
            if self._drop_bonus is None:
                self._drop_bonus = self._load_drop_bonus()
            return self._drop_bonus

    def _load_drop_bonus(self) -> DropBonusTable:
        # A table fault grants no bonus anywhere until restart; only row faults close their own quest.
        try:
            parsed, digest = self._parsed_table(DROP_BONUS_RESOURCE)
        except ProtocolError as error:
            return DropBonusTable.unavailable(error.code, str(error))
        try:
            return DropBonusTable.from_table(parsed, quest_ids=self._all_rows, item_rows=self._table(ITEM_TABLE),
                                             sha256=digest)
        except (ProtocolError, DropBonusUnavailable) as error:
            return DropBonusTable.unavailable(error.code, str(error), sha256=digest)

    def drop_bonus(self, quest_id: int) -> DropBonus | None:
        self.row(quest_id)
        return self.drop_bonus_table().lookup(quest_id)

    def drop_bonus_status(self) -> dict:
        table = self.drop_bonus_table()
        coverage = Counter(self._all_rows[quest_id]["Type"] for quest_id in table.quest_ids)
        return {**table.status(), "coverage_by_type": dict(sorted(coverage.items()))}

    def candidate_view(self, quest_id: int, max_rank: int, record: Mapping | None = None) -> dict:
        if not int32(max_rank) or max_rank < 0 or (record is not None and (
                not isinstance(record, Mapping) or not int32(record.get("AchieveFlag")))):
            raise ProtocolError("quest_candidate_view_invalid",
                                "Candidate views take a raw nonnegative MaxRank and an optional quest record")
        quest = self.get(quest_id)
        candidates = quest.candidates_for(max_rank)
        rows = quest_modes.candidate_attributes(record, candidates)
        return {"quest_id": quest.quest_id, "max_rank": max_rank, "conditional": quest.conditional,
                "withheld": len(quest.candidates) - len(candidates),
                "candidates": [{"id": index, "type": slot, "attr": attribute, "enemy_id": candidate.enemy_id,
                                "band": classify_enemy(candidate.enemy_id).value, "entry_row": candidate.entry_row,
                                "loop_index": candidate.loop_index, "min_stars": candidate.min_stars,
                                "max_stars": candidate.max_stars, "reward_code": quest.reward_code(candidate),
                                "marks_slot": quest.marks_slot(candidate)}
                               for (index, slot, attribute), candidate in zip(rows, candidates, strict=True)]}

    def star_rewards(self, row: Mapping, hard: bool) -> tuple[StarReward, ...]:
        if not isinstance(row, Mapping) or type(hard) is not bool:
            raise ProtocolError("quest_reward_invalid", "Star rewards require a quest row and a difficulty")
        try:
            if hard:
                kind = integer(row.get("HardMode_RewardType"), minimum=INT32_MIN)
                count = integer(row.get("HardMode_RewardCount"), minimum=INT32_MIN)
                card_id = integer(row.get("HardMode_RewardCardID"), minimum=INT32_MIN)
                item_id = _item_name(row.get("HardMode_RewardItemID"))
                if kind == 0 and count == 0:
                    count = 1
            else:
                kind = integer(row.get("BonusType"), minimum=INT32_MIN)
                number = integer(row.get("BonusNum"), minimum=INT32_MIN)
                item_id = _item_name(row.get("NormalMode_RewardItemID"))
                count, card_id = (1, number) if kind == 3 else (number, 0)
            rewards = [self._star_reward(kind, count, card_id, item_id)]
            # 679D70 appends the harmony crystals after either difficulty branch.
            if integer(row.get("Type")) == HARMONY_TYPE:
                rewards.append(StarReward("crystal", 0, HARMONY_CRYSTALS))
        except ProtocolError:
            raise
        except ValueError as error:
            raise ProtocolError("quest_definition_invalid", "Quest star rewards contain invalid native fields", 503) from error
        return tuple(rewards)

    def _star_reward(self, kind: int, count: int, card_id: int, item_id: str | None) -> StarReward:
        if kind == 0:
            return StarReward("crystal", kind, count)
        if kind == 3:
            if card_id <= 1 or card_id not in self._card_ids:
                raise ProtocolError("quest_reward_unknown", "Star unit reward does not reference a known card", 503)
            return StarReward("unit", kind, count, card_id=card_id) if count > 0 else StarReward("unsupported", kind, count)
        if kind == 4:
            return StarReward("none", kind, count)
        if kind in GIFT_REWARD_TYPES:
            gift = self._gift_reward_items()[kind]
            return StarReward("item", kind, count, item_id=gift) if count > 0 else StarReward("unsupported", kind, count)
        if kind >= 100000 and kind % 1000 == ITEM_REWARD_KIND and count > 0 and self._possessions.known(item_id):
            return StarReward("item", kind, count, item_id=item_id)
        return StarReward("unsupported", kind, count)

    def _gift_reward_items(self) -> Mapping[int, str]:
        with self._lock:
            if self._gift_items is not None:
                return self._gift_items
            implants, items = (self._table(name) for name in ("cfgImplantText.atb", ITEM_TABLE))
            self._system_texts()
            try:
                keys = {row["_ID"]: row["_TextID"] for row in implants}
                names = {}
                for key, index in keys.items():
                    names.setdefault(command_id(key), set()).add(self.system_text(index))
                result = {}
                # SystemText_QuestRewardName_1xx and the gift item names share the same displayed text.
                for kind in GIFT_REWARD_TYPES:
                    name = self.system_text(keys.get(f"SystemText_QuestRewardName_{kind}"))
                    if not isinstance(name, str) or not name:
                        raise ValueError("Gift reward name is missing")
                    matches = [row["ItemID"] for row in items
                               if row.get("EffectType") == GIFT_EFFECT_TYPE and name in names.get(row.get("TextID"), ())]
                    if len(matches) != 1 or self._possessions.stack(matches[0]) <= 0:
                        raise ValueError("Gift reward name does not identify one stocked gift")
                    result[kind] = matches[0]
            except (KeyError, TypeError, ValueError, ProtocolError) as error:
                raise ProtocolError("quest_reward_config_invalid", "Gift star rewards do not resolve to verified items", 503) from error
            self._gift_items = MappingProxyType(result)
            return self._gift_items

    @staticmethod
    def _quest_id_list_members(configs: Iterable[Mapping]) -> list[dict]:
        members = []
        for row in configs:
            text = row.get("QuestIdList")
            if not isinstance(text, str) or not quest_modes.QUEST_ID_LIST_PATTERN.fullmatch(text):
                raise ProtocolError("quest_mode_resource_invalid", "Mission QuestIdList must be a comma separated quest list", 503)
            # A428C0 splits on commas and skips empty items, which covers the trailing comma in "16000,".
            items = [part.strip() for part in text.split(",") if part.strip()]
            if any(not QUEST_ID_ITEM.fullmatch(item) for item in items):
                raise ProtocolError("quest_mode_resource_invalid", "Mission QuestIdList items must be single quest identifiers", 503)
            members.extend({"MissionID": row.get("MissionID"), "QuestID": int(item)} for item in items)
        return members

    def _mission_table(self, name: str) -> _MissionTable:
        with self._lock:
            if name not in self._mission_rows:
                parsed, digest = self._parsed_table(name)
                self._mission_rows[name] = _MissionTable(tuple(MappingProxyType(dict(row)) for row in parsed["rows"]),
                                                         frozenset(field["name"] for field in parsed["fields"]), digest)
            return self._mission_rows[name]

    def _mission_tables(self, categories: tuple[int, ...]) -> dict[int, tuple]:
        # A row cache apart from resource_rows, so substituting a mode policy's rows never changes mission windows.
        tables = {}
        with self._lock:
            for category in categories:
                names = mission_tables(category)
                configs = self._mission_table(names[0]).rows
                if category in quest_modes.QUEST_ID_LIST_CATEGORIES:
                    tables[category] = (configs, self._quest_id_list_members(configs))
                else:
                    tables[category] = (configs, self._mission_table(names[1]).rows)
        return tables

    def _build_windows(self, categories: tuple[int, ...], overrides) -> quest_modes.MissionWindows:
        with self._lock:
            tables = self._mission_tables(categories)
            try:
                ranks = {key: integer(row.get("Rank")) for key, row in self._all_rows.items()}
                return quest_modes.MissionWindows(tables, ranks, overrides=overrides)
            except (KeyError, TypeError, ValueError) as error:
                raise ProtocolError("quest_mode_resource_invalid", "Mission windows do not satisfy the native contract", 503) from error

    def _cached_windows(self, categories: tuple[int, ...]) -> quest_modes.MissionWindows:
        with self._lock:
            if categories not in self._windows:
                self._windows[categories] = self._build_windows(categories, self._window_overrides)
            return self._windows[categories]

    def mission_windows(self, quest_type: int) -> quest_modes.MissionWindows:
        return self._cached_windows(self._mode_spec(quest_type).categories)

    def mission_registry(self) -> quest_modes.MissionWindows:
        return self._cached_windows(REGISTRY_CATEGORIES)

    def mission_tags(self) -> MissionTagIndex:
        with self._lock:
            if self._tags is None:
                self._tags = self._tag_index(self.mission_registry(), self._window_overrides)
            return self._tags

    def preview_mission_tags(self, overrides) -> MissionTagIndex:
        overrides = quest_modes.validate_window_overrides(overrides)
        with self._lock:
            return self._tag_index(self._build_windows(REGISTRY_CATEGORIES, overrides), overrides)

    def _tag_index(self, registry: quest_modes.MissionWindows, overrides) -> MissionTagIndex:
        tables = {name: self._mission_table(name) for name in REGISTRY_TABLES}
        untagged = frozenset(category for category in REGISTRY_CATEGORIES
                             if TAG_COLUMN not in tables[mission_tables(category)[0]].columns)
        return MissionTagIndex(registry, quest_rows=MappingProxyType(self._all_rows),
                               source_sha256={name: table.sha256 for name, table in tables.items()},
                               master_snapshot=self.snapshot.identifier, overrides=overrides,
                               categories_without_tag_column=untagged)

    def entry_sha256(self, labels: Iterable[str]) -> dict[str, str | None]:
        names = None if isinstance(labels, str) or not isinstance(labels, Iterable) else tuple(labels)
        if names is None or not all(isinstance(name, str) for name in names):
            raise ProtocolError("quest_mode_resource_invalid", "Entry digests require a sequence of entry labels", 503)
        with self._lock:
            return {name: self._entry_digests.get(name) for name in names}

    def resource_sha256(self, name: str) -> str:
        if not _resource_name(name, TABLE_SUFFIX):
            raise ProtocolError("quest_mode_resource_invalid", "Mode resources must be named native tables", 503)
        with self._lock:
            if name not in self._resource_hashes:
                self._table(name)
            return self._resource_hashes[name]

    def special_conditions(self) -> Mapping[int, tuple[quest_modes.SpecialCondition, ...]]:
        with self._lock:
            if self._special_conditions is None:
                raw = self._asset("SpecialMissionPlayCondition.atb")
                try:
                    conditions = quest_modes.parse_special_conditions(raw)
                except (KeyError, TypeError, ValueError) as error:
                    raise ProtocolError("quest_mode_resource_invalid", "Special play conditions differ from the native contract", 503) from error
                self._resource_hashes["SpecialMissionPlayCondition.atb"] = hashlib.sha256(raw).hexdigest()
                self._special_conditions = conditions
            return self._special_conditions

    def fixed_units(self, list_id: int) -> tuple[FixedUnit, ...]:
        if type(list_id) is not int or not 0 < list_id <= INT32_MAX:
            raise ProtocolError("quest_unit_list_invalid", "Fixed team lists require a positive native identifier")
        with self._lock:
            if self._unit_lists is None:
                groups = {}
                rows = self._table("QuestUnitTable.atb")
                try:
                    for row in rows:
                        groups.setdefault(integer(row.get("ListID"), minimum=1), []).append(
                            (integer(row.get("CardID"), minimum=1), integer(row.get("CostMod"), minimum=INT32_MIN)))
                except ValueError as error:
                    raise ProtocolError("quest_mode_resource_invalid", "QuestUnitTable differs from the native contract", 503) from error
                self._unit_lists = MappingProxyType({key: tuple(value) for key, value in groups.items()})
            # 6785B0 builds CardID above 10000 through its guest path; HL rows carry CardID % 10000.
            units = tuple(FixedUnit(card_id % 10000 if card_id > 10000 else card_id, cost)
                          for card_id, cost in self._unit_lists.get(list_id, ()))
            if any(unit.card_id <= 1 or unit.card_id not in self._card_ids for unit in units):
                raise ProtocolError("quest_definition_invalid", "Fixed team references an unknown card", 503)
            return units
