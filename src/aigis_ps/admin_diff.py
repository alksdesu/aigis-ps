from __future__ import annotations

import base64
import codecs
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from enum import Enum
import hashlib
import json
import re
from typing import Any

from .alxml import AlXmlError, Column, Container, Limits, ValueType, dumps, loads
from .storage import serialize


REDACTED = "[redacted]"
SECRET_FIELDS = frozenset(("user_key", "method_key", "bootstrap_token"))
RESPONSE_FIELD = "response"
XML_PREFIXES = ("<?xml", "<DA")
XML_PREFIX_BYTES = tuple(prefix.encode("ascii") for prefix in XML_PREFIXES)
TEXT_BOM = codecs.BOM_UTF8.decode("utf-8")
# alxml.loads accepts a byte order mark and then whitespace, comments and processing instructions before the root.
XML_PROLOG_ITEM = r"[ \t\r\n]+|<!--.*?-->|<\?.*?\?>"
_PROLOG_TEXT = re.compile(XML_PROLOG_ITEM, re.DOTALL)
_PROLOG_BYTES = re.compile(XML_PROLOG_ITEM.encode("ascii"), re.DOTALL)
# Key/value containers name each entry in their first column and carry its value in the later columns.
KEYED_XML_SECRETS = {"SYSTEM": frozenset(("ExchangeKey",))}
COLUMN_XML_SECRETS = {"LI": frozenset(("UK",)), "LGI": frozenset(("A7",))}
RESPONSE_LIMITS = Limits(max_bytes=16 * 1024 * 1024, max_elements=2_000_000, max_values=1_000_000)
ADD, REMOVE, REPLACE, REORDER = "add", "remove", "replace", "reorder"
VALUE_FIELDS = ("before", "after")
IDENTITY_FIELDS = ("key", "label")
_SCALARS = (str, int, bool, type(None))


class ResponseView(str, Enum):
    DIGEST = "digest"
    MASKED = "masked"


@dataclass(frozen=True)
class DiffLimits:
    max_entries: int = 500
    max_value_bytes: int = 16 * 1024
    max_bytes: int = 512 * 1024

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in vars(self).values()):
            raise ValueError("Diff limits must be positive integers")


@dataclass(frozen=True)
class KeyedCollection:
    identity: tuple[str, ...]
    labels: tuple[str, ...] = ()


DEFAULT_DIFF_LIMITS = DiffLimits()
KEYED_COLLECTIONS = {
    ("player_state", "units"): KeyedCollection(("UniqueID",), ("GetIndex", "CardID")),
    ("tables", "possessions"): KeyedCollection(("ItemID",)),
    ("tables", "presents"): KeyedCollection(("PresentID",)),
    ("tables", "quest_records"): KeyedCollection(("QuestID",)),
    ("tables", "card_status"): KeyedCollection(("CardID",)),
    ("tables", "crystals"): KeyedCollection(("FreeFlag", "Date")),
    ("tables", "backgrounds"): KeyedCollection(("CardID",)),
    ("tables", "perused_events"): KeyedCollection(("ID",)),
    ("tables", "vote_history"): KeyedCollection(("key",)),
    ("tables", "universal_flags"): KeyedCollection(("ID",)),
    ("tables", "exchange_history"): KeyedCollection(("ShopItemID",)),
    ("tables", "filter_settings"): KeyedCollection(("FilterNameHash",)),
    ("tables", "fame_depository"): KeyedCollection(("CardId",)),
    ("tables", "event_pass_status"): KeyedCollection(("ID",)),
}


def same(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same(value, right[key]) for key, value in left.items())
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(map(same, left, right))
    return left == right


def json_digest(value: Any) -> str:
    return hashlib.sha256(serialize(value).encode("utf-8")).hexdigest()


def canonical(value: Any) -> tuple[Any, str]:
    text = serialize(value)
    return json.loads(text), hashlib.sha256(text.encode("utf-8")).hexdigest()


def pointer(path: tuple) -> str:
    return "".join("/" + str(segment).replace("~", "~0").replace("/", "~1") for segment in path)


def _segments(value: str) -> tuple[str, ...]:
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in value.split("/")[1:]) if value else ()


def diff_states(before: Any, after: Any) -> tuple[dict, ...]:
    entries: list[dict] = []
    _diff((), before, after, entries)
    return tuple(entries)


def diff_digest(entries: tuple[dict, ...]) -> str:
    return json_digest(list(entries))


def _diff(path: tuple, before: Any, after: Any, entries: list[dict]) -> None:
    if type(before) is not type(after):
        entries.append({"op": REPLACE, "path": pointer(path), "before": before, "after": after})
    elif isinstance(before, dict):
        for key in sorted(before.keys() | after.keys()):
            if key not in after:
                entries.append({"op": REMOVE, "path": pointer((*path, key)), "before": before[key]})
            elif key not in before:
                entries.append({"op": ADD, "path": pointer((*path, key)), "after": after[key]})
            else:
                _diff((*path, key), before[key], after[key], entries)
    elif isinstance(before, list):
        spec = KEYED_COLLECTIONS.get(path)
        if spec is None or not _keyed(path, spec, before, after, entries):
            _sequence(path, before, after, entries)
    elif before != after:
        entries.append({"op": REPLACE, "path": pointer(path), "before": before, "after": after})


def _index(spec: KeyedCollection, rows: list) -> dict[tuple, dict] | None:
    indexed: dict[tuple, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or any(name not in row or not isinstance(row[name], _SCALARS) for name in spec.identity):
            return None
        # Typed keys keep True and 1 apart, as the stored JSON does.
        key = tuple((type(row[name]).__name__, row[name]) for name in spec.identity)
        if key in indexed:
            return None
        indexed[key] = row
    return indexed


def _identity(spec: KeyedCollection, row: dict) -> dict:
    result = {"key": {name: row[name] for name in spec.identity}}
    labels = {name: row[name] for name in spec.labels if name in row}
    if labels:
        result["label"] = labels
    return result


def _keyed(path: tuple, spec: KeyedCollection, before: list, after: list, entries: list[dict]) -> bool:
    old, new = _index(spec, before), _index(spec, after)
    if old is None or new is None:
        return False
    location = pointer(path)
    for key, row in old.items():
        if key not in new:
            entries.append({"op": REMOVE, "path": location, **_identity(spec, row), "before": row})
    for index, (key, row) in enumerate(new.items()):
        if key not in old:
            entries.append({"op": ADD, "path": location, **_identity(spec, row), "index": index, "after": row})
            continue
        changes: list[dict] = []
        _diff((), old[key], row, changes)
        identity = _identity(spec, row)
        entries.extend({"op": change["op"], "path": location, **identity, "field": change["path"],
                        **{name: change[name] for name in VALUE_FIELDS if name in change}} for change in changes)
    retained_before = [key for key in old if key in new]
    retained_after = [key for key in new if key in old]
    if retained_before != retained_after:
        entries.append({"op": REORDER, "path": location,
                        "before": [_identity(spec, old[key])["key"] for key in retained_before],
                        "after": [_identity(spec, new[key])["key"] for key in retained_after]})
    return True


def _shared_ends(left: list[str], right: list[str]) -> tuple[int, int]:
    limit = min(len(left), len(right))
    head = 0
    while head < limit and left[head] == right[head]:
        head += 1
    tail = 0
    while tail < limit - head and left[-1 - tail] == right[-1 - tail]:
        tail += 1
    return head, tail


def _sequence(path: tuple, before: list, after: list, entries: list[dict]) -> None:
    left, right = [serialize(item) for item in before], [serialize(item) for item in after]
    head, tail = _shared_ends(left, right)
    # Skipping shared ends and treating popular items as junk keeps duplicate-heavy lists near linear.
    matcher = SequenceMatcher(None, left[head:len(left) - tail], right[head:len(right) - tail])
    for tag, start, end, target, target_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        start, end, target, target_end = start + head, end + head, target + head, target_end + head
        if tag == "replace" and end - start == target_end - target:
            for offset in range(end - start):
                _diff((*path, target + offset), before[start + offset], after[target + offset], entries)
            continue
        entries.extend({"op": REMOVE, "path": pointer((*path, index)), "before": before[index]} for index in range(start, end))
        entries.extend({"op": ADD, "path": pointer((*path, index)), "after": after[index]} for index in range(target, target_end))


def is_xml_document(value: str | bytes) -> bool:
    if isinstance(value, str):
        body, prefixes, prolog = value.removeprefix(TEXT_BOM), XML_PREFIXES, _PROLOG_TEXT
    else:
        body, prefixes, prolog = value.removeprefix(codecs.BOM_UTF8), XML_PREFIX_BYTES, _PROLOG_BYTES
    position = 0
    while not body.startswith(prefixes, position):
        item = prolog.match(body, position)
        if item is None:
            return False
        position = item.end()
    return True


def redact(value: Any, *, responses: ResponseView = ResponseView.DIGEST) -> Any:
    if isinstance(value, dict):
        return {key: _field(key, item, responses) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, responses=responses) for item in value]
    if isinstance(value, str) and is_xml_document(value):
        return _document(value.encode("utf-8"), "text", responses)
    return value


def _field(key: Any, value: Any, responses: ResponseView) -> Any:
    if key in SECRET_FIELDS:
        return REDACTED
    if key == RESPONSE_FIELD and isinstance(value, str):
        return _response(value, responses)
    return redact(value, responses=responses)


def _response(text: str, responses: ResponseView) -> Any:
    if is_xml_document(text):
        return _document(text.encode("utf-8"), "text", responses)
    try:
        raw = base64.b64decode(text, validate=True)
    except ValueError:
        return text
    return _document(raw, "base64", responses) if is_xml_document(raw) else text


def _document(raw: bytes, encoding: str, responses: ResponseView) -> Any:
    if responses is ResponseView.MASKED:
        masked = _masked(raw)
        if masked is not None:
            return masked
    return {"redacted": RESPONSE_FIELD, "encoding": encoding, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _masked(raw: bytes) -> str | None:
    try:
        masked = _mask(loads(raw, limits=RESPONSE_LIMITS))
        return None if masked is None else dumps(masked, limits=RESPONSE_LIMITS).decode("utf-8")
    except (AlXmlError, ValueError):
        return None


def _mask(container: Container) -> Container | None:
    children: list[Column | Container] | None = []
    for child in container.children:
        masked = _mask(child) if isinstance(child, Container) else child
        if masked is None:
            return None
        children.append(masked)
    if container.name in KEYED_XML_SECRETS:
        children = _mask_keyed(children, KEYED_XML_SECRETS[container.name])
    elif container.name in COLUMN_XML_SECRETS:
        children = _mask_columns(children, COLUMN_XML_SECRETS[container.name])
    return None if children is None else replace(container, children=tuple(children))


def _hidden(column: Column, indices: set[int]) -> Column | None:
    hits = {index for index in indices if index < len(column.values)}
    if not hits:
        return column
    if column.type is not ValueType.STRING:
        return None
    return replace(column, values=tuple(REDACTED if index in hits else value for index, value in enumerate(column.values)))


def _mask_keyed(children: list, names: frozenset[str]) -> list | None:
    if not children or any(not isinstance(child, Column) for child in children):
        return None
    indices = {index for index, value in enumerate(children[0].values) if value in names}
    masked = [children[0]]
    for column in children[1:]:
        hidden = _hidden(column, indices)
        if hidden is None:
            return None
        masked.append(hidden)
    return masked


def _mask_columns(children: list, names: frozenset[str]) -> list | None:
    masked = []
    for child in children:
        if isinstance(child, Column) and child.name in names:
            child = _hidden(child, set(range(len(child.values))))
            if child is None:
                return None
        masked.append(child)
    return masked


def _redact_entry(entry: dict, responses: ResponseView) -> dict:
    segments = _segments(entry.get("path", "")) + _segments(entry.get("field", ""))
    result = {}
    for name, value in entry.items():
        if name in VALUE_FIELDS:
            result[name] = _redact_at(segments, value, responses)
        elif name in IDENTITY_FIELDS:
            result[name] = redact(value, responses=responses)
        else:
            result[name] = value
    return result


def _redact_at(segments: tuple[str, ...], value: Any, responses: ResponseView) -> Any:
    if any(segment in SECRET_FIELDS for segment in segments):
        return REDACTED
    if segments and segments[-1] == RESPONSE_FIELD and isinstance(value, str):
        return _response(value, responses)
    return redact(value, responses=responses)


def _bounded(entry: dict, limit: int) -> dict:
    result = dict(entry)
    for name in VALUE_FIELDS:
        if name in result:
            text = serialize(result[name])
            size = len(text.encode("utf-8"))
            if size > limit:
                result[name] = {"truncated": True, "bytes": size, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
    return result


def render_diff(entries: tuple[dict, ...], limits: DiffLimits = DEFAULT_DIFF_LIMITS, *,
                responses: ResponseView = ResponseView.DIGEST) -> dict:
    rendered, size = [], 0
    for entry in entries:
        if len(rendered) == limits.max_entries:
            break
        item = _bounded(_redact_entry(entry, responses), limits.max_value_bytes)
        cost = len(serialize(item).encode("utf-8"))
        if size + cost > limits.max_bytes:
            break
        rendered.append(item)
        size += cost
    omitted = len(entries) - len(rendered)
    return {"entries": rendered, "total": len(entries), "truncated": omitted > 0, "omitted": omitted}
