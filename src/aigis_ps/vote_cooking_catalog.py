from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, ValueType, dumps, loads
from .errors import ProtocolError, caught
from .native_numbers import INT32_MAX
from .public_tables import MASTER_LIMITS


LIMITS = ParseLimits(max_input_bytes=16 * 1024 * 1024, max_decompressed_bytes=16 * 1024 * 1024,
                    max_rows=65535, max_fields=128, max_cells=2_000_000)
MAX_CARD_ID = 9999
COOKING_KINDS = range(11)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _integer(value, minimum=0, maximum=INT32_MAX):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Cooking values require bounded native integers")
    return value


def _text(value, maximum, *, empty=False):
    if type(value) is not str or (not empty and not value) or len(value.encode("utf-8")) > maximum:
        raise ValueError("Cooking text requires a bounded native string")
    dumps(Container(children=(Column("Text", "S", (value,)),)))
    return value


@dataclass(frozen=True)
class VoteCookingDefinition:
    card_id: int
    name: str
    cooking_kind: int
    cooking_name: str
    name_text_id: int
    cooking_text_id: int


class VoteCookingCatalog:
    def __init__(self, asset_reader, card_provider):
        try:
            if not callable(asset_reader):
                raise ValueError("Cooking catalog requires a resource reader")
            resources, hashes = {}, {}

            def table(name, expected):
                raw = asset_reader(name)
                if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
                    raise ValueError("Cooking resource must be bounded immutable bytes")
                decoded = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
                parsed = parse_table(decoded, LIMITS)
                fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
                if len(fields) != len(parsed["fields"]) or any(fields.get(key) != kind for key, kind in expected.items()):
                    raise ValueError("Cooking resource schema differs from the verified fields")
                resources[name], hashes[name] = raw, hashlib.sha256(raw).hexdigest()
                return parsed["rows"]

            names = table("NameText.atb", {"Message": 32})
            texts = table("SystemText.atb", {"Recode_Index": 1, "Data_Text": 32})
            implants = table("cfgImplantText.atb", {"_ID": 32, "_TextID": 1})
            pointers = {}
            for row in implants:
                key = _text(row["_ID"], 512)
                pointer = _integer(row["_TextID"])
                if key in pointers or pointer >= len(texts):
                    raise ValueError("Cooking text mappings repeat or point outside the text table")
                pointers[key] = pointer

            def literal(key, maximum=4096):
                pointer = pointers[key]
                row = texts[pointer]
                if type(row["Recode_Index"]) is not int or row["Recode_Index"] != pointer:
                    raise ValueError("Cooking text pointer differs from its resource index")
                return pointer, _text(row["Data_Text"], maximum)

            cooking_names = {kind: literal(f"SystemText_HomeCookingItemName_{kind:03}", 512) for kind in COOKING_KINDS}
            _, confirmation = literal("SystemText_HomeCookingUseComfirmation")
            receive_text_id, information = literal("VoteGiftInfomationText")
            _integer(receive_text_id, 1)
            if confirmation.count("[ITEM_NAME]") != 2 or information.count("[UNIT_NAME]") != 1:
                raise ValueError("Cooking name placeholders differ from the native display contract")
            description_id = receive_text_id - 1
            description = _text(texts[description_id]["Data_Text"], 512)
            if (texts[description_id]["Recode_Index"] != description_id
                    or description != "最多投票のお礼の手料理"):
                raise ValueError("Vote cooking description differs from its verified source")
            getter = card_provider if callable(card_provider) else card_provider.get_xml
            raw_cards = getter("CardData")
            if type(raw_cards) is not bytes or not 0 < len(raw_cards) <= MASTER_LIMITS.max_bytes:
                raise ValueError("Cooking public cards must be bounded immutable bytes")
            document = loads(raw_cards, limits=MASTER_LIMITS)
            if any(not isinstance(column, Column) for column in document.children):
                raise ValueError("Cooking public cards require scalar columns")
            columns = {column.name: column for column in document.children}
            if (len(columns) != len(document.children) or not {"CardID", "HomeCooking"} <= columns.keys()
                    or any(columns[key].type is not ValueType.INT for key in ("CardID", "HomeCooking"))
                    or len({len(column.values) for column in columns.values()}) != 1):
                raise ValueError("Cooking public card fields must be aligned native integer columns")
            definitions = {}
            for identifier, kind in zip(columns["CardID"].values, columns["HomeCooking"].values):
                _integer(identifier, 1, MAX_CARD_ID)
                _integer(kind, 0, 10)
                if identifier in definitions or identifier > len(names):
                    raise ValueError("Cooking card identities repeat or have no name row")
                name = _text(names[identifier - 1]["Message"], 512)
                pointer, cooking_name = cooking_names[kind]
                definitions[identifier] = VoteCookingDefinition(identifier, name, kind, cooking_name, identifier - 1, pointer)
            if not definitions:
                raise ValueError("Cooking catalog requires public card definitions")
            resources["CardData"], hashes["CardData"] = raw_cards, hashlib.sha256(raw_cards).hexdigest()
            self.definitions = MappingProxyType(definitions)
            self.cooking_names = MappingProxyType(cooking_names)
            self.description, self.use_confirmation, self.information_template = description, confirmation, information
            self.receive_text_id = receive_text_id
            self.resource_hashes, self._resources = MappingProxyType(hashes), MappingProxyType(resources)
            self.content_version = _digest({"schema": 1, "resources": hashes, "card_name": "NameText[CardID-1]",
                "kind_name": "SystemText_HomeCookingItemName_%03d", "kind_zero": "valid",
                "content": "positive_card_id_without_high_digits", "maximum_card_id": MAX_CARD_ID,
                "description_text_id": description_id, "receive_text_id": receive_text_id})
            self._snapshot = tuple(getattr(self, key) for key in self._snapshot_names())
        except (AttributeError, KeyError, IndexError, TypeError, ValueError, OSError, OverflowError) as error:
            raise ProtocolError("vote_cooking_catalog_invalid", "Vote cooking resources differ from their verified contract", 503) from error

    @staticmethod
    def _snapshot_names():
        return ("definitions", "cooking_names", "description", "use_confirmation", "information_template", "receive_text_id",
                "resource_hashes", "_resources", "content_version")

    def _ready(self):
        if any(getattr(self, key) is not value for key, value in zip(self._snapshot_names(), self._snapshot)):
            raise ProtocolError("vote_cooking_catalog_changed", "Vote cooking resources changed after setup", 503)

    def is_ready(self) -> bool:
        return caught(self._ready) is None

    def definition(self, card_id):
        self._ready()
        if type(card_id) is not int or card_id not in self.definitions:
            raise ProtocolError("vote_cooking_card_unknown", "Vote cooking requires a verified card identity", 503)
        return self.definitions[card_id]

    def definition_from_content(self, content):
        self._ready()
        if type(content) is not int or not 1 <= content <= MAX_CARD_ID:
            raise ProtocolError("vote_cooking_content_invalid", "Vote cooking content requires a card ID without unknown high digits", 503)
        return self.definition(content)

    def status(self):
        failure = caught(self._ready)
        return {"ready": failure is None, "error": None if failure is None else failure.code,
                "content_version": self.content_version,
                "cards": len(self.definitions), "cooking_kinds": len(self.cooking_names),
                "resource_hashes": dict(self.resource_hashes), "native_tested": False}
