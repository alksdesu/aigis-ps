from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass
from enum import Enum
from xml.etree import ElementTree as ET

from defusedxml.ElementTree import DefusedXMLParser
from defusedxml.common import DefusedXmlException

from .native_numbers import float32


class AlXmlError(ValueError):
    pass


class ValueType(str, Enum):
    INT = "I"
    BOOL = "B"
    STRING = "S"
    FLOAT = "F"
    DOUBLE = "D"
    DATETIME = "DT"
    INT64 = "I64"
    PROP = "P"


_LONG_TYPES = dict(zip(
    ("Int", "Bool", "String", "Float", "Double", "DateTime", "Int64", "Prop"),
    ValueType,
))
# Native A7EE90/A7F380 receivers keep only I/B/S/F/D values here, and an empty column shifts every later one.
GENERIC_ROW_TABLES = frozenset((
    "ASSAULT", "ASSIGNBONUS", "CONFIG", "CardStatus", "DmmPeopleApiResponseParam", "DmmPremiumRewardReceivedParam",
    "EventPassStatus", "ExchangeHistory", "FameDepository", "GF", "IOF", "ItemExchange", "LI", "LetsTDRewardParam",
    "LotteryResult", "PEV", "PLC", "POSSESSION", "PPF", "PPT", "PPU", "PRM", "PSC", "PSH", "PSR", "PVH",
    "PlayerFilterParam", "QR", "RecommendPurchaseList", "SkinShopPurchase", "TowerDefenceCampaignRewardParam",
    "TowerTradeHistory", "TrustReward", "VCC", "VEC", "VGC", "VRC", "VoteGiftParam",
))
ROW_TABLE_TYPES = frozenset((ValueType.INT, ValueType.BOOL, ValueType.STRING, ValueType.FLOAT, ValueType.DOUBLE))
_TAG = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
_INTEGER = re.compile(r"[+-]?[0-9]+\Z")
_FLOAT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 8 * 1024 * 1024
    max_depth: int = 32
    max_elements: int = 100_000
    max_values: int = 65_536
    max_text_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in vars(self).values()):
            raise ValueError("AL XML limits must be positive integers")
        if self.max_depth > 128:
            raise ValueError("AL XML depth cannot exceed 128")


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class Column:
    name: str
    type: ValueType
    values: tuple[int | float | bool | str, ...]
    index: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", ValueType(self.type))
        object.__setattr__(self, "values", tuple(self.values))
        if self.index is not None and self.type is not ValueType.PROP:
            raise AlXmlError("Native numeric and string columns do not consume Index")


@dataclass(frozen=True)
class Container:
    name: str = "DA"
    children: tuple[Column | Container, ...] = ()
    index: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "children", tuple(self.children))


def effective_name(entry: Column | Container) -> str:
    name = entry.name
    if isinstance(entry, Container) or entry.type is ValueType.PROP:
        if len(name) > 1 and name[0] == "_" and "0" <= name[1] <= "9":
            name = name[1:]
        if entry.index is not None:
            name = f"{name}[{entry.index}]"
    return name


def _check_text(value: str, limits: Limits) -> None:
    if not isinstance(value, str):
        raise AlXmlError("Expected XML text")
    if any(not (c in "\t\n\r" or "\x20" <= c <= "\ud7ff" or "\ue000" <= c <= "\ufffd" or "\U00010000" <= c <= "\U0010ffff") for c in value):
        raise AlXmlError("Text contains an invalid XML character")
    if len(value.encode("utf-8")) > limits.max_text_bytes:
        raise AlXmlError("XML text exceeds the configured limit")


def _check_name(entry: Column | Container, limits: Limits) -> None:
    _check_text(entry.name, limits)
    if not entry.name:
        raise AlXmlError("AL XML names cannot be empty")
    if entry.index is not None:
        _check_text(entry.index, limits)
    if isinstance(entry, Container) or entry.type is ValueType.PROP:
        if len(effective_name(entry).encode("utf-8")) > 63:
            raise AlXmlError("Container and property names must fit the native 64-byte buffer")
    elif not _TAG.fullmatch(entry.name):
        raise AlXmlError("Scalar column names must be XML tags; native Name overrides are ignored")


def _check_row_table(group: Container) -> None:
    lengths = set()
    for entry in group.children:
        if not isinstance(entry, Column) or entry.type not in ROW_TABLE_TYPES:
            raise AlXmlError(f"Native row table {group.name} keeps only I, B, S, F and D columns")
        lengths.add(len(entry.values))
    if len(lengths) > 1:
        raise AlXmlError(f"Native row table {group.name} requires columns of equal length")


def _value_text(value: int | float | bool | str, kind: ValueType, limits: Limits) -> str:
    if kind in (ValueType.STRING, ValueType.DATETIME, ValueType.PROP):
        _check_text(value, limits)
        if kind is not ValueType.STRING and any(char in value for char in "&<>\"'"):
            raise AlXmlError("DateTime and Prop values must use plain lexical text")
        # Native non-string parsers dereference the child text node even for empty values.
        if not value and kind is ValueType.DATETIME:
            return "0"
        if not value and kind is ValueType.PROP:
            raise AlXmlError("Empty Prop values have no safe native text representation")
        return value
    if kind is ValueType.BOOL:
        if type(value) is not bool:
            raise AlXmlError("Boolean columns require bool values")
        return "true" if value else "false"
    if kind in (ValueType.INT, ValueType.INT64):
        bits = 32 if kind is ValueType.INT else 64
        if type(value) is not int or not -(1 << (bits - 1)) <= value < 1 << (bits - 1):
            raise AlXmlError(f"Integer value is outside signed {bits}-bit range")
        return str(value)
    if type(value) not in (int, float):
        raise AlXmlError("Floating columns require numeric values")
    try:
        number = float(value)
        if kind is ValueType.FLOAT:
            number = float32(number)
    except (OverflowError, struct.error) as exc:
        raise AlXmlError("Floating value is outside its native range") from exc
    if not math.isfinite(number):
        raise AlXmlError("Non-finite floating values are not supported")
    return format(number, ".9g" if kind is ValueType.FLOAT else ".17g")


def _parse_value(text: str, kind: ValueType, limits: Limits) -> int | float | bool | str:
    _check_text(text, limits)
    if kind in (ValueType.STRING, ValueType.DATETIME, ValueType.PROP):
        _value_text(text, kind, limits)
        return text
    if kind is ValueType.BOOL:
        if text.lower() not in ("true", "false"):
            raise AlXmlError("Boolean XML values must be true or false")
        return text.lower() == "true"
    if kind in (ValueType.INT, ValueType.INT64):
        if not _INTEGER.fullmatch(text) or len(text) > 21:
            raise AlXmlError("Invalid integer XML value")
        value = int(text)
    else:
        if not _FLOAT.fullmatch(text):
            raise AlXmlError("Invalid floating XML value")
        value = float(text)
    _value_text(value, kind, limits)
    if kind is ValueType.FLOAT:
        return float32(value)
    return value


class _BoundedBuilder(ET.TreeBuilder):
    def __init__(self, limits: Limits):
        super().__init__()
        self.limits = limits
        self.depth = 0
        self.count = 0

    def start(self, tag, attrs):
        self.depth += 1
        self.count += 1
        if self.depth > self.limits.max_depth or self.count > self.limits.max_elements:
            raise AlXmlError("XML depth or element count exceeds the configured limit")
        return super().start(tag, attrs)

    def end(self, tag):
        result = super().end(tag)
        self.depth -= 1
        return result


def loads(data: bytes, *, limits: Limits = DEFAULT_LIMITS, short_names: bool = True) -> Container:
    if not isinstance(data, bytes) or not data or len(data) > limits.max_bytes:
        raise AlXmlError("Expected nonempty XML bytes within the configured limit")
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AlXmlError("AL XML must use UTF-8") from exc
    if b"\x00" in data:
        raise AlXmlError("AL XML cannot contain NUL bytes")
    parser = DefusedXMLParser(target=_BoundedBuilder(limits), forbid_dtd=True, forbid_entities=True, forbid_external=True)
    try:
        parser.feed(data)
        root = parser.close()
    except (ET.ParseError, DefusedXmlException, LookupError, ValueError) as exc:
        if isinstance(exc, AlXmlError):
            raise
        raise AlXmlError("Invalid or forbidden XML") from exc
    if root.tag != "DA" or root.attrib:
        raise AlXmlError("The AL XML root must be an untyped DA element")
    type_key, name_key, index_key = ("T", "N", "I") if short_names else ("Type", "Name", "Index")
    values_seen = 0

    def parse(node: ET.Element) -> Column | Container:
        nonlocal values_seen
        if not isinstance(node.tag, str) or not _TAG.fullmatch(node.tag):
            raise AlXmlError("Unsupported XML tag or namespace")
        if set(node.attrib) - {type_key, name_key, index_key}:
            raise AlXmlError("Unknown AL XML attribute or mismatched name mode")
        if (node.text or "").strip() or (node.tail or "").strip():
            raise AlXmlError("Values must be placed inside child elements")
        raw_kind = node.get(type_key)
        if raw_kind is None:
            result = Container(node.get(name_key, node.tag), tuple(parse(child) for child in node), node.get(index_key))
        else:
            try:
                kind = ValueType(raw_kind) if short_names else _LONG_TYPES[raw_kind]
            except (ValueError, KeyError) as exc:
                raise AlXmlError(f"Unsupported AL XML type: {raw_kind}") from exc
            if kind is not ValueType.PROP and (name_key in node.attrib or index_key in node.attrib):
                raise AlXmlError("Native scalar columns ignore Name and Index attributes")
            values = []
            for child in node:
                if len(child) or child.attrib or not _TAG.fullmatch(child.tag) or (child.tail or "").strip():
                    raise AlXmlError("Each array item must contain only text")
                values_seen += 1
                if values_seen > limits.max_values:
                    raise AlXmlError("Value count exceeds the configured limit")
                values.append(_parse_value(child.text or "", kind, limits))
            result = Column(node.get(name_key, node.tag), kind, tuple(values), node.get(index_key))
        _check_name(result, limits)
        return result

    return parse(root)


def dumps(container: Container, *, limits: Limits = DEFAULT_LIMITS, short_names: bool = True) -> bytes:
    if not isinstance(container, Container) or container.name != "DA" or container.index is not None:
        raise AlXmlError("The AL XML root must be an unindexed DA container")
    for group in container.children:
        if isinstance(group, Container) and group.name in GENERIC_ROW_TABLES:
            _check_row_table(group)
    type_key, name_key, index_key = ("T", "N", "I") if short_names else ("Type", "Name", "Index")
    long_types = {value: key for key, value in _LONG_TYPES.items()}
    elements = values_seen = 0

    def build(entry: Column | Container, depth: int) -> ET.Element:
        nonlocal elements, values_seen
        if not isinstance(entry, (Column, Container)):
            raise AlXmlError("Containers can contain only columns or containers")
        elements += 1
        if depth > limits.max_depth or elements > limits.max_elements:
            raise AlXmlError("XML depth or element count exceeds the configured limit")
        _check_name(entry, limits)
        tag = entry.name if _TAG.fullmatch(entry.name) else "C"
        node = ET.Element(tag)
        if tag != entry.name:
            node.set(name_key, entry.name)
        if entry.index is not None:
            node.set(index_key, entry.index)
        if isinstance(entry, Container):
            for child in entry.children:
                node.append(build(child, depth + 1))
        else:
            node.set(type_key, entry.type.value if short_names else long_types[entry.type])
            for value in entry.values:
                values_seen += 1
                elements += 1
                if values_seen > limits.max_values or elements > limits.max_elements or depth + 1 > limits.max_depth:
                    raise AlXmlError("XML value count or depth exceeds the configured limit")
                ET.SubElement(node, "V").text = _value_text(value, entry.type, limits)
        return node

    root = build(container, 1)
    # This declaration survives the client's exact UTF-8 declaration removal and selects its XML parser.
    data = b'<?xml version="1.0"?>\n' + ET.tostring(root, encoding="utf-8", short_empty_elements=False)
    if len(data) > limits.max_bytes:
        raise AlXmlError("XML bytes exceed the configured limit")
    return data
