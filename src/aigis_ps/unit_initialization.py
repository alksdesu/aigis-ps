from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .errors import ProtocolError
from .native_numbers import INT32_MAX, INT32_MIN, float32, int32
from .player_state import PRINCE_CARD_IDS
from .system_literals import command_id


CARD_FIELDS = ("CardID", "InitClassID", "ClassLV0SkillID", "ClassLV1SkillID", "Rare", "Blood",
               "ExcludeFameDisplayFirstClass")
CLASS_FIELDS = ("ClassID", "MaxLevel", "JobChange")
RARITY_FIELDS = ("ConfigID", "RequireExpRatio", "LevelCap00", "LevelCap02", "EvolutionConfig")
LIMITS = ParseLimits(max_input_bytes=1024 * 1024, max_decompressed_bytes=1024 * 1024,
                     max_rows=4096, max_fields=16)
# Native 4CA400 hardcodes this card's stage-one class instead of following its InitClassID.
CLASSIC_ROUTE_CARD_ID, CLASSIC_ROUTE_CLASS_ID = 80, 710


@dataclass(frozen=True)
class UnitInitialValues:
    class_id: int
    stage: int
    skill_id: int
    experience: int


def stage_one_class_id(card):
    return CLASSIC_ROUTE_CLASS_ID if card["CardID"] == CLASSIC_ROUTE_CARD_ID else card["InitClassID"]


def _integer(value, minimum=0):
    if not int32(value) or value < minimum:
        raise ValueError("Expected a bounded native integer")
    return value


def _freeze_rows(source, fields):
    if not isinstance(source, Mapping) or len(source) > 65536:
        raise ValueError("Expected bounded indexed resource rows")
    result = {}
    for key, row in source.items():
        _integer(key, 1)
        if not isinstance(row, Mapping):
            raise ValueError("Expected an indexed resource record")
        selected = {name: row[name] for name in fields if name in row}
        if any(value is not None and type(value) not in (int, float, str, bool) for value in selected.values()):
            raise ValueError("Resource fields must be scalar values")
        result[key] = MappingProxyType(selected)
    return MappingProxyType(result)


class NativeUnitInitializer:
    def __init__(self, cards, classes, experience, rarities, blood_payload):
        try:
            self._cards = _freeze_rows(cards, CARD_FIELDS)
            self._classes = _freeze_rows(classes, CLASS_FIELDS)
            self._rarities = _freeze_rows(rarities, RARITY_FIELDS)
            if not isinstance(experience, Sequence) or not 1 <= len(experience) <= 65536:
                raise ValueError("Expected a bounded cumulative experience sequence")
            self._experience = tuple(_integer(value) for value in experience)
            if self._experience[0] != 0 or any(left >= right for left, right in zip(self._experience, self._experience[1:])):
                raise ValueError("Experience thresholds must increase from zero")
            if not isinstance(blood_payload, bytes) or len(blood_payload) > LIMITS.max_input_bytes:
                raise ValueError("Blood definitions must be bounded bytes")
            payload = decompress_all4(blood_payload, LIMITS) if blood_payload.startswith(b"ALL4") else blood_payload
            table = parse_table(payload, LIMITS)
            fields = {field["name"]: field["type_code"] for field in table["fields"]}
            if fields != {"_ResID": 1, "_TypeID": 1, "_SystemTextID": 1} or len(table["fields"]) != 3:
                raise ValueError("Unexpected blood definition schema")
            definitions, types = {}, set()
            for row in table["rows"]:
                key = _integer(row["_ResID"], INT32_MIN)
                identifier = _integer(row["_TypeID"])
                if key in definitions or identifier in types:
                    raise ValueError("Duplicate blood definition")
                definitions[key] = identifier
                types.add(identifier)
            self._hero_blood = definitions[command_id("英傑")]
            self._blood_types = frozenset(types)
            self.blood_sha256 = hashlib.sha256(blood_payload).hexdigest()
            content = {"version": 1, "cards": {key: dict(row) for key, row in self._cards.items()},
                       "classes": {key: dict(row) for key, row in self._classes.items()},
                       "rarities": {key: dict(row) for key, row in self._rarities.items()},
                       "experience": self._experience, "blood": self.blood_sha256}
            self.content_version = hashlib.sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest()
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            raise ProtocolError("unit_initialization_catalog", "Native unit initialization resources are invalid", 503) from error

    def _class(self, class_id):
        row = self._classes[class_id]
        for name in CLASS_FIELDS:
            _integer(row[name])
        if row["ClassID"] != class_id:
            raise ValueError("Class identifier does not match its resource key")
        return row

    def values(self, card_id, level, *, evolution_count=0):
        try:
            _integer(card_id, 1)
            _integer(level)
            _integer(evolution_count)
        except ValueError as error:
            raise ProtocolError("unit_initialization_invalid", "Unit initialization requires native nonnegative integers") from error
        if evolution_count not in (0, 2):
            raise ProtocolError("unit_initialization_unsupported", "This requested initial class stage is not supported", 501)
        if card_id in PRINCE_CARD_IDS:
            raise ProtocolError("unit_initialization_unsupported", "Prince units require their dedicated rank-based initialization", 501)
        try:
            card = self._cards[card_id]
            for name in CARD_FIELDS:
                _integer(card[name], INT32_MIN if name == "ExcludeFameDisplayFirstClass" else 0)
            if card["CardID"] != card_id:
                raise ValueError("Card identifier does not match its resource key")
            if card["Blood"] not in self._blood_types:
                raise ValueError("Card refers to an unknown blood definition")
            class_id = _integer(card["InitClassID"], 1)
            self._class(class_id)
            rarity_id = _integer(card["Rare"] + 1, 1)
            rarity = self._rarities[rarity_id]
            for name in RARITY_FIELDS:
                _integer(rarity[name])
            if rarity["ConfigID"] != rarity_id:
                raise ValueError("Rarity identifier does not match its resource key")
            forced = card["Blood"] == self._hero_blood or card["ExcludeFameDisplayFirstClass"] & 0xFF
            desired = max(evolution_count, 2 if forced else 0)
            stage = 0
            if desired == 2:
                # Native 4CE0C0 promotes to the JobChange of the stage-one class that 4CB1F0 just checked.
                route = self._class(stage_one_class_id(card))
                if route["JobChange"] and rarity["EvolutionConfig"]:
                    class_id, stage = route["JobChange"], 2
            selected = self._class(class_id)
            cap = min(_integer(selected["MaxLevel"], 1), _integer(rarity[f"LevelCap{stage:02}"], 1))
            if cap > len(self._experience):
                raise ValueError("Unit level cap exceeds the experience table")
            index = min(max(1, level), cap) - 1
            ratio = float32(float32(_integer(rarity["RequireExpRatio"], 1)) / float32(100))
            exp = int(float32(float32(float32(self._experience[index]) * ratio) + float32(0.5)))
            if not 0 <= exp <= INT32_MAX:
                raise ProtocolError("unit_initialization_overflow", "Initial unit experience exceeds the native field", 503)
            return UnitInitialValues(class_id, stage, card["ClassLV0SkillID"], exp)
        except ProtocolError:
            raise
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            raise ProtocolError("unit_initialization_reference", "Selected unit initialization references are invalid", 503) from error
