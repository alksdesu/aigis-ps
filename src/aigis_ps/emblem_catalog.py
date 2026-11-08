from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps
from .bootstrap import MAX_QUEST_RECORDS
from .errors import ProtocolError, caught
from .native_numbers import int32
from .possessions import ITEM_RESOURCE_LIMIT, MAX_ITEM_NAME_BYTES, MAX_POSSESSION_ITEMS, STORED_ITEM_NAME_BYTES
from .quest_modes import CLEAR_FLAG
from .system_literals import command_id, decode_command


CATEGORY = 1078486164
LIMITS = ParseLimits(max_input_bytes=ITEM_RESOURCE_LIMIT, max_decompressed_bytes=ITEM_RESOURCE_LIMIT,
                     max_rows=MAX_POSSESSION_ITEMS)
TITLE_FIELDS = {name: 32 for name in ("PlayerTitle", "_ExpressionValue", "Explan", "ConditionQuestList")}
TITLE_FIELDS.update({name: 1 for name in ("Effect", "EffectPower", "GroupId", "EmblemLevel", "ConditionTextId",
    "ConditionPlayerRank", "ConditionClearRank", "ConditionItemId")})
ITEM_FIELDS = {name: 1 for name in ("ID", "EffectType", "Category", "Stack", "CanAllGet", "NoAutoDelete",
    "ReceiveAction", "ReceiveConfirmation", "InvisibleContaint", "CanPresent", "EmblemID")}
ITEM_FIELDS.update(ItemID=32, EventID=32)
RECEIVE_COMMAND = "CMenuPresentList::DoGetCommonItemType_010"
_RANK = re.compile(r"\s*0xe52a3bdd\(\s*([0-9]+)\s*\)\s*;?\s*", re.IGNORECASE)
_QUEST = re.compile(r"\s*0x441add5b\(\s*([0-9]+)\s*\)\s*;?\s*", re.IGNORECASE)
_ITEM = re.compile(r'\s*HasPossession\(\s*"ItemId=([^"\\]+)"\s*\)\s*;\s*')


def _text(value, limit=4096, *, empty=False):
    if type(value) is not str or (not value and not empty) or len(value.encode("utf-8")) > limit:
        raise ValueError("Expected bounded resource text")
    dumps(Container(children=(Column("Value", "S", (value,)),)))
    return value


def _predicate(expression, item_names):
    for kind, pattern in (("rank", _RANK), ("quest", _QUEST)):
        if match := pattern.fullmatch(expression):
            argument = int(match.group(1))
            if not int32(argument) or argument < (1 if kind == "quest" else 0):
                return None, None, "predicate_argument_out_of_range"
            return kind, argument, None
    if match := _ITEM.fullmatch(expression):
        name = _text(match.group(1), STORED_ITEM_NAME_BYTES)
        return "possession", name, None if name in item_names else "predicate_item_unavailable"
    return None, None, "expression_unsupported"


@dataclass(frozen=True)
class EmblemTitle:
    index: int
    name: str
    expression: str
    explanation: str
    group_id: int
    emblem_level: int
    effect: int
    effect_power: int
    condition_text_id: int
    condition_player_rank: int
    condition_clear_rank: int
    condition_quest_list: str
    condition_item_id: int
    predicate: str | None
    argument: int | str | None
    unsupported_reason: str | None

    @property
    def supported(self) -> bool:
        return self.unsupported_reason is None

    @property
    def prince_card_id(self) -> int | None:
        if self.effect == 5:
            return self.effect_power
        return 1 if self.effect in (1, 2, 3, 4) else None


@dataclass(frozen=True)
class EmblemItem:
    item_id: str
    item_hash: int
    emblem_id: int


class EmblemCatalog:
    def __init__(self, possessions, asset_reader):
        self._possessions = possessions
        try:
            if not callable(asset_reader):
                raise ValueError("Emblems require a resource reader")
            hashes, decoded_hashes = {}, {}

            def table(name, expected):
                raw = asset_reader(name)
                if type(raw) is not bytes or not 0 < len(raw) <= ITEM_RESOURCE_LIMIT:
                    raise ValueError("Emblem resources must be bounded immutable bytes")
                data = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
                parsed = parse_table(data, LIMITS)
                fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
                if len(fields) != len(parsed["fields"]) or any(fields.get(key) != value for key, value in expected.items()):
                    raise ValueError("Emblem resource fields differ from the native contract")
                hashes[name], decoded_hashes[name] = hashlib.sha256(raw).hexdigest(), hashlib.sha256(data).hexdigest()
                return parsed["rows"]

            item_names, item_hashes, selected = {}, set(), {}
            for row in table("ItemConfigTable.atb", ITEM_FIELDS):
                name = _text(row["ItemID"], STORED_ITEM_NAME_BYTES)
                identifier = row["ID"]
                if (not int32(identifier) or identifier != command_id(name) or name in item_names or identifier in item_hashes
                        or any(not int32(row[field]) for field, kind in ITEM_FIELDS.items() if kind == 1)
                        or row["Stack"] < 0):
                    raise ValueError("Item identities or numeric fields are invalid")
                item_names[name] = row["Stack"]
                item_hashes.add(identifier)
                if not name.startswith("get_emblem_") and not (row["EffectType"] == 10 and row["Category"] == CATEGORY):
                    continue
                if (row["EffectType"] != 10 or row["Category"] != CATEGORY or row["Stack"] != 1
                        or any(row[field] != 0 for field in ("CanAllGet", "NoAutoDelete", "ReceiveAction",
                            "ReceiveConfirmation", "InvisibleContaint", "CanPresent")) or row["EventID"] != ""):
                    raise ValueError("Emblem acquisition differs from the verified passive item contract")
                selected[name] = EmblemItem(name, identifier, row["EmblemID"])
            # Emblem mail writes the name into a native cell; a longer stored emblem still answers title conditions.
            mailable = {name: item for name, item in selected.items() if len(name.encode("utf-8")) <= MAX_ITEM_NAME_BYTES}

            definitions, pairs = [], set()
            for index, row in enumerate(table("PlayerTitle.atb", TITLE_FIELDS)):
                if (any(not int32(row[name]) for name, kind in TITLE_FIELDS.items() if kind == 1)
                        or row["GroupId"] <= 0 or row["EmblemLevel"] <= 0 or row["EffectPower"] < 0
                        or row["ConditionPlayerRank"] < 0 or row["ConditionClearRank"] < 0):
                    raise ValueError("Title metadata requires valid native integers")
                pair = (row["GroupId"], row["EmblemLevel"])
                if pair in pairs:
                    raise ValueError("Title group and level must identify one physical row")
                pairs.add(pair)
                expression = _text(row["_ExpressionValue"], empty=True)
                predicate, argument, reason = _predicate(expression, item_names)
                if row["Effect"] not in (1, 2, 3, 4, 5) or row["Effect"] == 5 and row["EffectPower"] == 0:
                    reason = reason or "effect_unsupported"
                definitions.append(EmblemTitle(index, _text(row["PlayerTitle"]), expression,
                    _text(row["Explan"], empty=True), *pair, row["Effect"], row["EffectPower"],
                    row["ConditionTextId"], row["ConditionPlayerRank"], row["ConditionClearRank"],
                    _text(row["ConditionQuestList"], empty=True), row["ConditionItemId"], predicate, argument, reason))
            if not definitions or not mailable or any(not 0 <= item.emblem_id < len(definitions) for item in selected.values()):
                raise ValueError("Emblem item display references require a nonempty title catalog")

            effects, seen = {}, set()
            for row in table("SystemItemEffectType.atb", {"_ResID": 1, "_TypeID": 1}):
                if not int32(row["_ResID"]) or not int32(row["_TypeID"]) or row["_TypeID"] in seen:
                    raise ValueError("Effect identities are invalid or duplicated")
                seen.add(row["_TypeID"])
                effects[row["_TypeID"]] = row["_ResID"]
            if effects.get(10) != CATEGORY:
                raise ValueError("Emblem effect category has changed")
            commands = {}
            for row in table("OnCommandMenuPresentList.atb", {"_ID": 1, "_Cmnd": 32}):
                identifier = row["_ID"]
                if not int32(identifier) or identifier in commands:
                    raise ValueError("Present command identities are invalid or duplicated")
                commands[identifier] = _text(row["_Cmnd"], 256 * 1024, empty=True)
            command = decode_command(commands[command_id(RECEIVE_COMMAND)])
            if re.fullmatch(r'\s*CMenuTaskSendMessage\s*\(\s*0\s*,\s*"DoGetItem"\s*\)\s*;\s*', command) is None:
                raise ValueError("Emblem claim command has unverified side effects")

            self._titles = tuple(definitions)
            self._by_id = MappingProxyType({title.index: title for title in definitions})
            self._items = MappingProxyType(mailable)
            self._item_ids = frozenset(mailable)
            self._unsupported_items = frozenset(selected.keys() - mailable.keys())
            self._hashes = MappingProxyType(hashes)
            self._decoded_hashes = MappingProxyType(decoded_hashes)
            referenced = self._item_ids | {title.argument for title in definitions if title.predicate == "possession" and title.supported}
            self._referenced_items = MappingProxyType({name: item_names[name] for name in referenced})
            self._ready()
            version = {"schema": 1, "resources": hashes, "predicates": "rank_ge_quest_bit24_positive_possession",
                "title_ids": "physical_zero_based", "acquisition": "passive_possession_single_by_policy",
                "unavailable": "preserve_and_reject", "possessions": self._possessions.asset_sha256}
            self._content_version = hashlib.sha256(json.dumps(version, sort_keys=True).encode()).hexdigest()
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError, ProtocolError) as error:
            raise ProtocolError("emblem_catalog_invalid", "Emblem resources differ from the verified contract", 503) from error

    @property
    def titles(self) -> tuple[EmblemTitle, ...]:
        return self._titles

    @property
    def by_id(self):
        return self._by_id

    @property
    def items(self):
        return self._items

    @property
    def item_ids(self) -> frozenset[str]:
        return self._item_ids

    @property
    def content_version(self) -> str:
        return self._content_version

    @property
    def item_resource_sha256(self) -> str:
        return self._decoded_hashes["ItemConfigTable.atb"]

    @property
    def title_resource_sha256(self) -> str:
        return self._decoded_hashes["PlayerTitle.atb"]

    def _ready(self):
        try:
            if (not self._possessions.is_ready()
                    or self._possessions.asset_sha256 != self._decoded_hashes["ItemConfigTable.atb"]
                    or any(type(self._possessions.stack(name)) is not int
                        or self._possessions.stack(name) != stack
                        or self._possessions.resolve_hash(command_id(name)) != name for name, stack in self._referenced_items.items())):
                raise ValueError("Emblem possession dependencies changed")
        except (AttributeError, ValueError, KeyError, TypeError, ProtocolError) as error:
            raise ProtocolError("emblem_catalog_changed", "Emblem and possession definitions must remain identical", 503) from error

    def is_ready(self) -> bool:
        return caught(self._ready) is None

    def get(self, index: int) -> EmblemTitle:
        self._ready()
        if not int32(index) or index not in self._by_id:
            raise ProtocolError("emblem_unknown", "Title index is not in the verified catalog")
        return self._by_id[index]

    def eligible(self, index: int, state: dict) -> bool:
        title = self.get(index)
        if not title.supported:
            raise ProtocolError("emblem_unsupported", "Title expression or effect requires an additional verified rule", 501)
        if (not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int
                or state["bootstrap_schema"] != 1 or not isinstance(state.get("tables"), dict)):
            raise ProtocolError("emblem_state_invalid", "Title selection requires supported persisted tables", 503)
        tables = state["tables"]
        if title.predicate == "rank":
            rows = tables.get("game_param")
            if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                    or not int32(rows[0].get("Rank")) or rows[0]["Rank"] <= 0):
                raise ProtocolError("emblem_state_invalid", "Title selection requires one valid player rank", 503)
            return rows[0]["Rank"] >= title.argument
        if title.predicate == "quest":
            rows = tables.get("quest_records")
            if not isinstance(rows, list) or len(rows) > MAX_QUEST_RECORDS:
                raise ProtocolError("emblem_state_invalid", "Title selection requires bounded quest records", 503)
            records = {}
            for row in rows:
                if (not isinstance(row, dict) or not int32(row.get("QuestID")) or row["QuestID"] <= 0
                        or row["QuestID"] in records or not int32(row.get("AchieveFlag"))):
                    raise ProtocolError("emblem_state_invalid", "Title quest identities or flags are invalid", 503)
                records[row["QuestID"]] = row["AchieveFlag"]
            return bool(records.get(title.argument, 0) & CLEAR_FLAG)
        return self._possessions.counts(state).get(title.argument, 0) > 0

    def require_eligible(self, index: int, state: dict) -> EmblemTitle:
        if not self.eligible(index, state):
            raise ProtocolError("emblem_locked", "Title acquisition condition is not satisfied", 409)
        return self._by_id[index]

    def status(self) -> dict:
        result = {"ready": True, "title_count": len(self._titles), "item_count": len(self._items),
            "group_count": len({title.group_id for title in self._titles}),
            "predicate_counts": dict(Counter(title.predicate for title in self._titles)),
            "unsupported_titles": [title.index for title in self._titles if not title.supported],
            "unsupported_items": sorted(self._unsupported_items),
            "resource_sha256": dict(self._hashes), "content_version": self.content_version,
            "single_claim_policy": "CanAllGet0 is enforced by the local claim service",
            "name_policy": (f"Emblem mail needs names within the {MAX_ITEM_NAME_BYTES}-byte native cell; stored names "
                            f"up to {STORED_ITEM_NAME_BYTES} bytes stay readable for title conditions"),
            "selection": "Independent expression per physical title index; no automatic selection on acquisition",
            "native_tested": False}
        try:
            self._ready()
        except ProtocolError as error:
            result.update(ready=False, error=error.code)
        return result
