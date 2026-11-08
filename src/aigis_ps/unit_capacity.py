from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.resources import files
import json
import re

from .al_format import ParseLimits, decompress_all4, parse_table
from .barracks_service import EXTRA_SHIFT, MAXIMUM_EXTRA_BARRACKS, PURCHASE_FIELD
from .errors import ProtocolError
from .masterdata import MasterSnapshot
from .native_numbers import UINT32_MASK, int32
from .player_state import barracks_index
from .public_tables import validate_public_master
from .quest_modes import CLEAR_FLAG
from .system_literals import decode_command, system_literals


RESOURCE_LIMITS = ParseLimits(max_input_bytes=8 * 1024 * 1024, max_decompressed_bytes=8 * 1024 * 1024)
SPECIAL_QUESTS = frozenset((1108, 1208, 1306))
CLEAR_LIMIT_COMMAND = 0x724270C7
# Native 65FDF0 hardcodes these; resources only supply the append and the clear-count limit.
FIRST_BARRACKS_BASE = 198
RANK_LIMIT = 200
RANK_STEP = 10
CLEAR_STEP = 10
CLEAR_BONUS = 2
SPECIAL_QUEST_BONUS = 4
_CLEAR_COMMAND = re.compile(r'\s*SetSystemStateParam\s*\(\s*"ClearQuestCountLimit"\s*,\s*(0|[1-9][0-9]*)\s*\)\s*;\s*')


def _integer(value, minimum=0):
    return int32(value) and value >= minimum


def _invalid(message):
    raise ProtocolError("unit_capacity_state_invalid", message, 503)


def _first_barracks_bonus(rank, clears, specials):
    return (min(rank, RANK_LIMIT) // RANK_STEP + CLEAR_BONUS * ((clears + CLEAR_STEP // 2) // CLEAR_STEP)
            + SPECIAL_QUEST_BONUS * specials)


@dataclass(frozen=True)
class BarracksUsage:
    capacities: tuple[int, ...]
    counts: tuple[int, ...]

    @property
    def primary_available(self) -> int:
        return max(0, self.capacities[0] - self.counts[0])


class UnitCapacity:
    def __init__(self, snapshot: MasterSnapshot, asset_reader):
        if not isinstance(snapshot, MasterSnapshot) or not callable(asset_reader):
            raise ValueError("A fixed master snapshot and resource reader are required")
        self._hashes = {}
        try:
            schemas = json.loads(files("aigis_ps").joinpath("data/master-schemas.json").read_text("utf-8"))
            fields = {name: [field["name"] for field in table["fields"]] for name, table in schemas["tables"].items()}
            raw = snapshot.get_xml("QuestTable")
            document, _ = validate_public_master("QuestTable", raw, fields)
            columns = {column.name: column.values for column in document.children}
            types = columns["Type"]
            if any(not _integer(value) for value in types):
                raise ValueError("Quest types must be nonnegative integers")
            self.quest_ids = frozenset(columns["QuestID"])
            self.counted_quests = frozenset(identifier for identifier, kind in zip(columns["QuestID"], types) if kind == 1)
            self._hashes["QuestTable"] = hashlib.sha256(raw).hexdigest()
            settings = system_literals(self._table(asset_reader, "OnSystemCommand.atb"),
                                       {"AppendOfFirstBarrakcsCapacity": 0, "BarracksCapacity": 250})
            self.append = settings["AppendOfFirstBarrakcsCapacity"]
            self.extra_capacity = settings["BarracksCapacity"]
            if not _integer(self.append) or not _integer(self.extra_capacity, 1):
                raise ValueError("Barracks capacity settings must be bounded integers")
            commands = [row for row in self._table(asset_reader, "OnCommand.atb") if row["_ID"] == CLEAR_LIMIT_COMMAND]
            if len(commands) > 1:
                raise ValueError("Quest capacity command is ambiguous")
            command = decode_command(commands[0]["_Cmnd"]) if commands else ""
            self.clear_limit = None
            if command.strip():
                match = _CLEAR_COMMAND.fullmatch(command)
                if match is None or not _integer(int(match[1])):
                    raise ValueError("Quest capacity command is not a supported literal assignment")
                self.clear_limit = int(match[1])
            largest = FIRST_BARRACKS_BASE + self.append + _first_barracks_bonus(
                RANK_LIMIT, len(self.counted_quests), len(SPECIAL_QUESTS))
            if not _integer(largest, 1):
                raise ValueError("First barracks capacity exceeds the native integer range")
            content = {"client": snapshot.client_hash, "snapshot": snapshot.identifier,
                       "resources": self._hashes, "calculation_version": 1}
            self.content_version = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        except (ValueError, KeyError, TypeError, OSError) as error:
            raise ProtocolError("unit_capacity_resources_invalid", "Barracks resources differ from the verified contract", 503) from error

    def _table(self, reader, name):
        raw = reader(name)
        if not isinstance(raw, bytes) or len(raw) > RESOURCE_LIMITS.max_input_bytes:
            raise ValueError("Capacity resource must be bounded bytes")
        self._hashes[name] = hashlib.sha256(raw).hexdigest()
        payload = decompress_all4(raw, RESOURCE_LIMITS) if raw.startswith(b"ALL4") else raw
        table = parse_table(payload, RESOURCE_LIMITS)
        fields = {field["name"]: field["type_code"] for field in table["fields"]}
        if fields != {"_ID": 1, "_Cmnd": 32} or len(fields) != len(table["fields"]):
            raise ValueError("Capacity command schema changed")
        return table["rows"]

    def status(self):
        return {"available": True, "content_version": self.content_version,
                "first_base": FIRST_BARRACKS_BASE + self.append, "rank_limit": RANK_LIMIT,
                "type1_quest_count": len(self.counted_quests), "clear_limit": self.clear_limit,
                "special_quests": sorted(SPECIAL_QUESTS), "extra_capacity": self.extra_capacity,
                "maximum_extra_barracks": MAXIMUM_EXTRA_BARRACKS, "native_tested": False,
                "resource_sha256": dict(self._hashes)}

    def usage(self, state: dict) -> BarracksUsage:
        if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1:
            _invalid("Capacity requires a supported bootstrap state")
        tables = state.get("tables")
        if not isinstance(tables, dict):
            _invalid("Capacity requires persisted tables")
        rows = []
        for name in ("game_param", "config"):
            value = tables.get(name)
            if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
                _invalid("Capacity requires one game parameter and configuration row")
            rows.append(value[0])
        rank, purchases = rows[0].get("Rank"), rows[1].get(PURCHASE_FIELD)
        if not _integer(rank, 1) or not int32(purchases):
            _invalid("Rank or barracks purchase flags are invalid")
        extra = (purchases & UINT32_MASK) >> EXTRA_SHIFT
        if extra > MAXIMUM_EXTRA_BARRACKS:
            raise ProtocolError("unit_capacity_barracks_unsupported", "Additional barracks exceed the verified purchase range", 409)
        records = tables.get("quest_records")
        if not isinstance(records, list) or len(records) > len(self.quest_ids):
            _invalid("Capacity requires bounded quest records")
        seen, cleared = set(), set()
        for record in records:
            if not isinstance(record, dict):
                _invalid("Quest capacity record must be an object")
            identifier, flags = record.get("QuestID"), record.get("AchieveFlag")
            if (not _integer(identifier, 1) or identifier not in self.quest_ids or identifier in seen
                    or not int32(flags)):
                _invalid("Quest capacity records contain invalid or duplicate values")
            seen.add(identifier)
            if flags & CLEAR_FLAG:
                cleared.add(identifier)
        count = len(cleared & self.counted_quests)
        if self.clear_limit is not None:
            count = min(count, self.clear_limit)
        primary = FIRST_BARRACKS_BASE + self.append + _first_barracks_bonus(rank, count, len(cleared & SPECIAL_QUESTS))
        player = state.get("player_state")
        units = player.get("units") if isinstance(player, dict) else None
        if not isinstance(units, list):
            _invalid("Capacity requires persisted unit rows")
        counts = [0] * (extra + 1)
        for unit in units:
            flags = unit.get("GeneralPurposeFlags") if isinstance(unit, dict) else None
            if not int32(flags):
                _invalid("Unit barracks flags are invalid")
            index = barracks_index(unit)
            if index > extra:
                raise ProtocolError("unit_capacity_barracks_unsupported", "Unit points to an unmodeled or locked barracks", 409)
            counts[index] += 1
        return BarracksUsage((primary,) + (self.extra_capacity,) * extra, tuple(counts))

    def available(self, state: dict) -> int:
        return self.usage(state).primary_available
