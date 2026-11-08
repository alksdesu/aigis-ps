from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
import hashlib
import re
from xml.etree import ElementTree as ET

from defusedxml.ElementTree import DefusedXMLParser
from defusedxml.common import DefusedXmlException

from .alxml import AlXmlError, Limits, _BoundedBuilder, loads
from .schedule_capture import CREDENTIAL_NAMES


RESPONSE_LIMITS = Limits(max_bytes=32 * 1024 * 1024, max_depth=32, max_elements=2_000_000,
                         max_values=2_000_000, max_text_bytes=1024 * 1024)
PRIVATE_NAMES = CREDENTIAL_NAMES | frozenset(("exchangekey",))
# LGI carries the player ID in A2/A6/A10 and the user key in A7 under neutral column names.
POSITIONAL_SECRETS = {"LGI": frozenset(("A2", "A6", "A7", "A10"))}
EXCLUDED_OPERATIONS = {
    31: "method_key", 152: "login", 153: "login_token",
    144: "account_link", 145: "account_link", 146: "account_link",
    32: "serial_code", 45: "external_links",
    47: "payment", 50: "payment", 91: "payment", 154: "payment", 155: "payment", 156: "payment",
    102: "anti_cheat", 103: "anti_cheat",
}
RESPONSE_PRESETS = {
    "summon": (12, 13, 35, 108),
    "battle": (66, 67, 68, 76, 77, 78, 80, 81, 82, 83, 116, 130, 143),
    "login": (35, 115, 116),
    "shop": (46,),
    "card-status": (9, 51, 76, 100, 101, 149),
}
MAX_OPERATION = 0xFFFF
_TAG = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
_ATTRIBUTES = frozenset(("T", "N", "I"))
_SEPARATORS = re.compile(r"[_.-]")


class ResponseCaptureError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Node:
    tag: str
    attributes: tuple[tuple[str, str], ...] = ()
    children: tuple[Node, ...] = ()
    text: str = ""

    @property
    def name(self) -> str:
        return dict(self.attributes).get("N") or self.tag

    @property
    def kind(self) -> str | None:
        return dict(self.attributes).get("T")


@dataclass
class _Audit:
    removed: set[str] = field(default_factory=set)
    rows: int = 0


def filter_rules() -> dict:
    return {"privateNames": sorted(PRIVATE_NAMES),
            "positionalSecrets": {group: sorted(names) for group, names in POSITIONAL_SECRETS.items()},
            "maxElements": RESPONSE_LIMITS.max_elements, "maxDepth": RESPONSE_LIMITS.max_depth}


def selected_operations(values: Iterable[int]) -> tuple[int, ...]:
    selected = []
    for value in values:
        if type(value) is not int or not 0 < value <= MAX_OPERATION:
            raise ResponseCaptureError("invalid_operation")
        if value in EXCLUDED_OPERATIONS:
            raise ResponseCaptureError("excluded_operation")
        if value not in selected:
            selected.append(value)
    if not selected:
        raise ResponseCaptureError("empty_operation_selection")
    return tuple(sorted(selected))


def _invalid() -> ResponseCaptureError:
    return ResponseCaptureError("invalid_response_xml")


def _whitespace(text: str | None) -> bool:
    return not (text or "").strip()


def _node(element: ET.Element) -> Node:
    if not isinstance(element.tag, str) or not _TAG.fullmatch(element.tag) or set(element.attrib) - _ATTRIBUTES:
        raise _invalid()
    if any(char in value for value in element.attrib.values() for char in "&<>"):
        raise _invalid()
    kind = element.get("T")
    if kind is not None and kind != "P" and ("N" in element.attrib or "I" in element.attrib):
        raise _invalid()
    if not _whitespace(element.text) or not all(_whitespace(child.tail) for child in element):
        raise _invalid()
    if kind is None:
        children = tuple(_node(child) for child in element)
    else:
        items = []
        for item in element:
            if len(item) or item.attrib or not isinstance(item.tag, str) or not _TAG.fullmatch(item.tag):
                raise _invalid()
            items.append(Node(item.tag, text=item.text or ""))
        children = tuple(items)
    return Node(element.tag, tuple(sorted(element.attrib.items())), children)


def parse_response(data: bytes) -> Node:
    if not isinstance(data, bytes) or not 0 < len(data) <= RESPONSE_LIMITS.max_bytes:
        raise ResponseCaptureError("invalid_response_payload")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid() from None
    if b"\x00" in data:
        raise _invalid()
    parser = DefusedXMLParser(target=_BoundedBuilder(RESPONSE_LIMITS), forbid_dtd=True, forbid_entities=True, forbid_external=True)
    try:
        parser.feed(data)
        root = parser.close()
    except (ET.ParseError, DefusedXmlException, LookupError, ValueError, RecursionError):
        raise _invalid() from None
    if root.tag != "DA" or root.attrib:
        raise _invalid()
    return _node(root)


def _normalized(name: str) -> str:
    return _SEPARATORS.sub("", name.split("[", 1)[0].lower())


def _private(node: Node, audit: _Audit) -> bool:
    names = (node.tag, *(value for key, value in node.attributes if key == "N"))
    hits = {normalized for normalized in map(_normalized, names) if normalized in PRIVATE_NAMES}
    audit.removed |= hits
    return bool(hits)


def _keyed_rows(node: Node, audit: _Audit) -> Node | None:
    columns = node.children
    if not columns or columns[0].kind != "S" or columns[0].name != "A1":
        return node
    keys = [_normalized(item.text) for item in columns[0].children]
    hits = {index for index, key in enumerate(keys) if key in PRIVATE_NAMES}
    if not hits:
        return node
    audit.removed |= {keys[index] for index in hits}
    if any(column.kind is None or len(column.children) != len(keys) for column in columns):
        audit.removed.add(node.name + ".*")
        return None
    audit.rows += len(hits)
    return replace(node, children=tuple(
        replace(column, children=tuple(item for index, item in enumerate(column.children) if index not in hits))
        for column in columns))


def _filter(node: Node, audit: _Audit) -> Node | None:
    secrets = POSITIONAL_SECRETS.get(node.name, frozenset())
    children = []
    for child in node.children:
        if _private(child, audit):
            continue
        if child.name in secrets:
            audit.removed.add(node.name + "." + child.name)
            continue
        if child.kind is None:
            child = _filter(child, audit)
            if child is None:
                continue
        children.append(child)
    return _keyed_rows(replace(node, children=tuple(children)), audit)


def _sanitize(document: Node) -> tuple[Node, dict]:
    audit = _Audit()
    clean = _filter(document, audit)
    if clean is None:
        raise _invalid()
    return clean, {"removed": sorted(audit.removed), "removed_rows": audit.rows}


def sanitize_response(raw: bytes) -> tuple[Node, dict]:
    return _sanitize(parse_response(raw))


def _describe(document: Node) -> list[dict]:
    entries = []
    for node in document.children:
        if node.kind is not None:
            entries.append({"name": node.name, "type": node.kind, "values": len(node.children)})
            continue
        columns = [child for child in node.children if child.kind is not None]
        entries.append({"name": node.name, "columns": len(columns), "groups": len(node.children) - len(columns),
                        "rows": max((len(column.children) for column in columns), default=0)})
    return entries


def _alxml_status(data: bytes) -> dict:
    try:
        loads(data, limits=RESPONSE_LIMITS)
    except (AlXmlError, ValueError, TypeError, RecursionError) as error:
        return {"alxml_loadable": False, "alxml_error": str(error)[:200]}
    return {"alxml_loadable": True}


def inspect_filtered_response(data: bytes) -> dict:
    document = parse_response(data)
    clean, _ = _sanitize(document)
    if clean != document:
        raise ResponseCaptureError("response_agent_filter_incomplete")
    return {"output_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "groups": _describe(document), **_alxml_status(data)}
