from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps
from .errors import ProtocolError
from .native_clock import NATIVE_EPOCH, native_clock
from .native_numbers import INT32_MIN, UINT32_MASK, int32
from .possessions import STORED_ITEM_NAME_BYTES


RESOURCE_LIMIT = 64 * 1024 * 1024
LIMITS = ParseLimits(max_input_bytes=RESOURCE_LIMIT, max_decompressed_bytes=RESOURCE_LIMIT,
                     max_rows=65535, max_fields=64, max_cells=3_000_000)
FIELD_NAMES = {
    "ConditionID": "condition_id", "PeriodType": "period_type", "ConditionType": "condition_type",
    "QuestType": "quest_type", "QuestID": "quest_id", "ClearRankCondition": "clear_rank_condition",
    "HardClearRankCondition": "hard_clear_rank_condition", "ClearCountCondition": "clear_count_condition",
    "MemberCountCondition": "member_count_condition", "KindCondition": "kind_condition",
    "RaceCondition": "race_condition", "GenusCondition": "genus_condition", "AssignCondition": "assign_condition",
    "IdentityCondition": "identity_condition", "BloodCondition": "blood_condition", "RarityCondition": "rarity_condition",
    "InHallofFameSum": "in_hallof_fame_sum", "InHallofFameSupportSum": "in_hallof_fame_support_sum",
    "PurchasedSkinSum": "purchased_skin_sum", "RewardPoint": "reward_point",
    "RewardItemType": "reward_item_type", "RewardItemParam": "reward_item_param", "RewardItemID": "reward_item_id",
    "DispTabType": "disp_tab_type", "DisplayType": "display_type", "GroupType": "group_type",
    "DisplayOrder": "display_order", "BorderValue": "border_value", "EventPassItemID": "event_pass_item_id",
    "EventPassPoint": "event_pass_point", "StartDateTime": "start_at", "EndDateTime": "end_at",
    "ConditionText": "condition_text", "RewardTypeDispText": "reward_type_disp_text", "GroupNameTextId": "group_name_text_id",
}
DATE_FIELDS = frozenset(("StartDateTime", "EndDateTime"))
TEXT_FIELDS = frozenset(("RewardItemID", "EventPassItemID"))
NONNEGATIVE_FIELDS = frozenset((
    "QuestID", "ClearRankCondition", "HardClearRankCondition", "ClearCountCondition", "MemberCountCondition",
    "InHallofFameSum", "InHallofFameSupportSum", "PurchasedSkinSum", "RewardPoint", "RewardItemType",
    "RewardItemParam", "DispTabType", "DisplayType", "GroupType", "BorderValue", "EventPassPoint",
))
MISSION_FIELDS = {name: 9 if name in DATE_FIELDS else 32 if name in TEXT_FIELDS else 1 for name in FIELD_NAMES}
SEQUENCE_FIELDS = {name: 1 for name in ("ID", "GloryConditionID", "DescriptionTextID", "EventCharaID",
                                       "CharaOffsetX", "CharaOffsetY", "MessageTextID", "AchievedMessageTextID")}
PAGES = frozenset(((1, 1), (2, 2), (3, 3), (4, 4), (4, 7), (5, 5)))
WINDOW_RULE = "inclusive_start_and_end_native_clock"
# Receipts committed while mission windows compared JST instants keep this rule in their digest.
LEGACY_WINDOW_RULE = "inclusive_start_and_end_jst"


@dataclass(frozen=True)
class MissionDefinition:
    condition_id: int
    period_type: int
    condition_type: int
    quest_type: int
    quest_id: int
    clear_rank_condition: int
    hard_clear_rank_condition: int
    clear_count_condition: int
    member_count_condition: int
    kind_condition: int
    race_condition: int
    genus_condition: int
    assign_condition: int
    identity_condition: int
    blood_condition: int
    rarity_condition: int
    in_hallof_fame_sum: int
    in_hallof_fame_support_sum: int
    purchased_skin_sum: int
    reward_point: int
    reward_item_type: int
    reward_item_param: int
    reward_item_id: str
    disp_tab_type: int
    display_type: int
    group_type: int
    display_order: int
    border_value: int
    event_pass_item_id: str
    event_pass_point: int
    start_at: datetime | None
    end_at: datetime | None
    condition_text: int
    reward_type_disp_text: int
    group_name_text_id: int
    definition_version: str


def _integer(value, minimum=INT32_MIN):
    if not int32(value) or value < minimum:
        raise ValueError("Expected a native int32")
    return value


def _text(value, *, maximum, empty=True):
    if not isinstance(value, str) or (not value and not empty) or len(value.encode("utf-8")) > maximum:
        raise ValueError("Expected bounded resource text")
    dumps(Container(children=(Column("Value", "S", (value,)),)))
    return value


def _date(value):
    if (not isinstance(value, (tuple, list)) or len(value) != 2
            or any(type(part) is not int or not 0 <= part <= UINT32_MASK for part in value)):
        raise ValueError("Mission dates require two native uint32 words")
    milliseconds = value[0] | value[1] << 32
    if not milliseconds:
        return None
    try:
        return NATIVE_EPOCH + timedelta(milliseconds=milliseconds)
    except OverflowError as error:
        raise ValueError("Mission date is outside the supported calendar") from error


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                     default=lambda item: item.isoformat()).encode("utf-8")).hexdigest()


def _clock(now):
    try:
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError("Mission time must identify an absolute instant")
        # 6F77C0 compares the dates with the whole-second client clock.
        return native_clock(now)
    except (ValueError, TypeError, OverflowError) as error:
        raise ProtocolError("mission_clock_invalid", "Mission time requires an aware datetime") from error


def _active(definition, now):
    return (definition.start_at is None or definition.start_at <= now) and (
        definition.end_at is None or now <= definition.end_at)


def _page_fields(*values):
    try:
        for value in values:
            _integer(value, 0)
    except ValueError as error:
        raise ProtocolError("mission_page_invalid", "Mission page fields must be nonnegative native integers") from error


def _unsupported_page():
    raise ProtocolError("mission_page_unsupported", "This mission page requires an unsupported native workflow", 501)


class MissionCatalog:
    def __init__(self, asset_reader, *, resource_version: str):
        try:
            self.resource_version = _text(resource_version, empty=False, maximum=128)
            if not callable(asset_reader):
                raise ValueError("Mission catalog requires a resource reader")
            hashes = {}

            def table(name, schema):
                payload = asset_reader(name)
                if not isinstance(payload, bytes) or not 0 < len(payload) <= RESOURCE_LIMIT:
                    raise ValueError("Mission resources must be bounded immutable bytes")
                data = decompress_all4(payload, LIMITS) if payload.startswith(b"ALL4") else payload
                parsed = parse_table(data, LIMITS)
                fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
                if fields != schema or len(fields) != len(parsed["fields"]):
                    raise ValueError("Mission resource fields differ from the verified schema")
                hashes[name] = hashlib.sha256(data).hexdigest()
                return parsed["rows"]

            indexed = {}
            for source in table("GloryConditionConfig.atb", MISSION_FIELDS):
                values = {}
                for name, target in FIELD_NAMES.items():
                    value = source[name]
                    if name in DATE_FIELDS:
                        values[target] = _date(value)
                    elif name in TEXT_FIELDS:
                        # Same tolerance as ItemConfigTable names; mail and EP writers refuse new rows past the native cell.
                        values[target] = _text(value, maximum=STORED_ITEM_NAME_BYTES)
                    else:
                        minimum = 1 if name in ("ConditionID", "PeriodType", "ConditionType") else (
                            0 if name in NONNEGATIVE_FIELDS else INT32_MIN)
                        values[target] = _integer(value, minimum)
                if (values["start_at"] is not None and values["end_at"] is not None
                        and values["end_at"] < values["start_at"]):
                    raise ValueError("Mission validity dates are reversed")
                identifier = values["condition_id"]
                if identifier in indexed:
                    raise ValueError("Mission identifiers are duplicated")
                indexed[identifier] = MissionDefinition(**values, definition_version=_digest(values))
            if not indexed:
                raise ValueError("Mission definitions cannot be empty")
            sequence_rows = {}
            seen = set()
            for source in table("ProgressiveBeginnerMissionConfig.atb", SEQUENCE_FIELDS):
                for name in SEQUENCE_FIELDS:
                    _integer(source[name])
                ordinal = _integer(source["ID"], 1)
                identifier = _integer(source["GloryConditionID"], 1)
                if (ordinal in sequence_rows or identifier in seen or identifier not in indexed
                        or indexed[identifier].period_type != 100):
                    raise ValueError("Beginner sequence references are missing, duplicated or not sequential missions")
                sequence_rows[ordinal] = identifier
                seen.add(identifier)
            if (not sequence_rows or sorted(sequence_rows) != list(range(1, len(sequence_rows) + 1))
                    or seen != {row.condition_id for row in indexed.values() if row.period_type == 100}):
                raise ValueError("Beginner sequence must cover every sequential mission in contiguous order")
            self._definitions = tuple(indexed[key] for key in sorted(indexed))
            self._by_id = MappingProxyType({row.condition_id: row for row in self._definitions})
            self._sequence = tuple(sequence_rows[key] for key in sorted(sequence_rows))
            by_order, collisions = {}, []
            for row in self._definitions:
                if row.period_type == 4:
                    if row.display_order in by_order:
                        collisions.append((row.display_order, by_order[row.display_order].condition_id, row.condition_id))
                    by_order[row.display_order] = row
            groups = {}
            for order in sorted(by_order):
                row = by_order[order]
                if row.display_type and row.group_type:
                    groups.setdefault(row.group_type, []).append(row.condition_id)
            self._groups = MappingProxyType({key: tuple(value) for key, value in sorted(groups.items())})
            self._listed = MappingProxyType({page: frozenset(row.condition_id for row in self._definitions
                if (row.period_type, row.disp_tab_type) == page and row.display_type == 0) for page in sorted(PAGES)})
            self._collisions = tuple(collisions)
            self._hashes = MappingProxyType(hashes)
            content = {"schema": 1, "resource_version": self.resource_version, "resources": hashes,
                "definitions": [row.definition_version for row in self._definitions], "sequence": self._sequence,
                "groups": dict(self._groups), "dependency_policy": "active_same_page_or_display_group"}
            self.content_version = _digest({**content, "dates": WINDOW_RULE})
            self.legacy_window_content_version = _digest({**content, "dates": LEGACY_WINDOW_RULE})
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError) as error:
            raise ProtocolError("mission_catalog_invalid", "Mission resources differ from the verified contract", 503) from error

    @property
    def definitions(self) -> tuple[MissionDefinition, ...]:
        return self._definitions

    @property
    def by_id(self):
        return self._by_id

    @property
    def sequence(self) -> tuple[int, ...]:
        return self._sequence

    @property
    def groups(self):
        return self._groups

    def replayable_content_versions(self) -> frozenset[str]:
        return frozenset((self.content_version, self.legacy_window_content_version))

    def _definition(self, definition):
        if not isinstance(definition, MissionDefinition) or self._by_id.get(definition.condition_id) != definition:
            raise ProtocolError("mission_definition_invalid", "Mission definition does not belong to this catalog", 503)
        return definition

    def active(self, now) -> tuple[MissionDefinition, ...]:
        now = _clock(now)
        return tuple(row for row in self._definitions if _active(row, now))

    def is_active(self, definition: MissionDefinition, now) -> bool:
        return _active(self._definition(definition), _clock(now))

    def listed(self, period: int, tab: int) -> frozenset[int]:
        _page_fields(period, tab)
        if (period, tab) not in PAGES:
            _unsupported_page()
        return self._listed[period, tab]

    def page(self, period: int, tab: int, task_group: int, now) -> tuple[MissionDefinition, ...]:
        _page_fields(period, tab, task_group)
        return self._page(period, tab, task_group, _clock(now))

    # A reading converted again can land on another second of the float32 grid, so callers pass the reading itself.
    def _page(self, period, tab, task_group, clock):
        if (period, tab) in PAGES and task_group == 0:
            return tuple(sorted((self._by_id[key] for key in self._listed[period, tab] if _active(self._by_id[key], clock)),
                                key=lambda row: (row.display_order, row.condition_id)))
        if (period, tab) == (4, 0) and task_group > 0:
            if task_group not in self._groups:
                raise ProtocolError("mission_group_unknown", "This mission group is not present in the catalog", 409)
            return tuple(self._by_id[key] for key in self._groups[task_group] if _active(self._by_id[key], clock))
        _unsupported_page()

    def dependencies(self, definition: MissionDefinition, now) -> tuple[int, ...]:
        definition = self._definition(definition)
        now = _clock(now)
        if not _active(definition, now):
            raise ProtocolError("mission_definition_inactive", "Mission definition is outside its validity dates", 409)

        def direct(row):
            if row.condition_type == 3:
                if row.period_type != 1 or row.disp_tab_type != 1 or row.display_type != 0:
                    raise ValueError("Daily aggregates require the daily page")
                return tuple(item.condition_id for item in self._page(1, 1, 0, now) if item.condition_type != 3)
            if row.condition_type == 17:
                if (row.period_type != 4 or not row.display_type
                        or row.condition_id not in self._groups.get(row.group_type, ())):
                    raise ValueError("Attribute aggregates require actual displayed group membership")
                return tuple(item.condition_id for item in self._page(4, 0, row.group_type, now)
                             if item.condition_type != 17)
            return ()

        visiting, visited = set(), set()

        def visit(row):
            if row.condition_id in visiting:
                raise ValueError("Mission dependencies contain a cycle")
            if row.condition_id in visited:
                return
            visiting.add(row.condition_id)
            result = direct(row)
            if row.condition_type in (3, 17) and not result:
                raise ValueError("Mission aggregate has no active dependencies")
            for key in result:
                visit(self._by_id[key])
            visiting.remove(row.condition_id)
            visited.add(row.condition_id)

        try:
            visit(definition)
            return direct(definition)
        except (ValueError, KeyError, RecursionError) as error:
            raise ProtocolError("mission_dependencies_invalid", "Mission dependencies are empty, cyclic or inconsistent", 503) from error

    def is_ready(self) -> bool:
        return True

    def status(self) -> dict:
        return {"ready": self.is_ready(), "content_version": self.content_version, "resource_version": self.resource_version,
                "resource_sha256": dict(self._hashes), "definition_count": len(self._definitions),
                "sequence_count": len(self._sequence), "group_count": len(self._groups),
                "period_counts": dict(Counter(row.period_type for row in self._definitions)),
                "condition_counts": dict(Counter(row.condition_type for row in self._definitions)),
                "display_order_collisions": [{"order": order, "replaced": old, "kept": new}
                                             for order, old, new in self._collisions],
                "dates": WINDOW_RULE, "event_period_supported": False}
