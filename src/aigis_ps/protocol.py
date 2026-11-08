from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from collections import defaultdict
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from xml.etree import ElementTree as ET

import cramjam
from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException

from .errors import ProtocolError
from .native_numbers import INT32_MAX, UINT32_MASK, int32, signed32


MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
# Native row tables keep each string in a 256-byte cell and cut anything longer at 255 bytes.
NATIVE_CELL_BYTES = 255
RESPONSE_TOO_LARGE = "response_too_large"
CHECKSUM_ELEMENT = re.compile(rb"<HS>(-?[0-9]+)</HS>")
ROUTE_NAME = re.compile(r"[A-Za-z0-9]{8}\Z")


@dataclass(frozen=True)
class WireProfile:
    route: str
    registration_keys: tuple[int, ...]
    request_key: int
    response_key: int
    st: int | None
    flags: int = 1
    name: str = "registered"
    checksum_required: bool = True
    response_limit: int = 0x20000
    request_roots: tuple[str, ...] = ("DA",)
    response_xml_limit: int = MAX_XML_BYTES
    response_node_limit: int = 1_000_000
    response_kind: str = "xml"

    @property
    def identity(self) -> str:
        if self.registration_keys:
            return "registered:" + ",".join(map(str, self.registration_keys))
        return self.name


@dataclass(frozen=True)
class RequestEnvelope:
    profile: WireProfile
    xml: bytes
    document: ET.Element
    digest: str

    def payload_xml(self) -> bytes:
        root = deepcopy(self.document)
        if root.tag == "DA":
            for child in list(root):
                if child.tag in ("VN", "ST", "HS"):
                    root.remove(child)
        return ET.tostring(root, encoding="utf-8", short_empty_elements=False)

    def structure(self) -> dict:
        paths: set[str] = set()
        stack = [(self.document, "")]
        truncated = False
        while stack:
            node, parent = stack.pop()
            path = f"{parent}/{node.tag}"
            paths.add(path)
            if len(paths) >= 512:
                truncated = bool(stack or len(node))
                break
            stack.extend((child, path) for child in node)
        return {"field_paths": sorted(paths), "truncated": truncated, "xml_bytes": len(self.xml)}


class ProtocolRegistry:
    def __init__(self, records: list[dict], *, include_direct: bool = True):
        by_signature: dict[tuple, list[int]] = defaultdict(list)
        response_signatures: dict[tuple, tuple] = {}
        ids: set[int] = set()
        for row in records:
            route = row["route_literal"]
            if not isinstance(route, str) or not ROUTE_NAME.fullmatch(route):
                raise ValueError("Invalid protocol route")
            values = [row[k] for k in ("registration_key", "request_xor_key", "response_xor_key", "st", "response_flags")]
            if any(type(value) is not int for value in values):
                raise ValueError("Protocol numbers must be integers")
            number, request_key, response_key, st, flags = values
            if number <= 0 or number in ids or not 0 <= request_key <= 255 or not 0 <= response_key <= 255:
                raise ValueError("Invalid or duplicate protocol identity")
            if not 0 <= st <= INT32_MAX or not 0 <= flags <= UINT32_MASK:
                raise ValueError("Protocol number out of range")
            ids.add(number)
            request_signature = (route, request_key, st)
            response_signature = (response_key, flags)
            previous = response_signatures.setdefault(request_signature, response_signature)
            if previous != response_signature:
                raise ValueError("Indistinguishable requests have different response contracts")
            by_signature[(route, request_key, response_key, st, flags)].append(number)
        self.profiles = [
            WireProfile(route, tuple(sorted(numbers)), request_key, response_key, st, flags)
            for (route, request_key, response_key, st, flags), numbers in by_signature.items()
        ]
        if include_direct:
            self.profiles.extend([
                WireProfile("GRs733a4", (), 0, 0x7D, None, name="master_cards", checksum_required=False, response_limit=40 * 1024 * 1024, request_roots=("Rev",), response_xml_limit=32 * 1024 * 1024, response_node_limit=2_000_000),
                WireProfile("QxZpjdfV", (), 0, 0x7D, None, name="master_quests", checksum_required=False, response_limit=40 * 1024 * 1024, request_roots=("Rev",), response_xml_limit=32 * 1024 * 1024, response_node_limit=2_000_000),
                WireProfile("jWbtv5NR", (), 0x4F, 0x27, 0, name="heartbeat", response_limit=0x1000),
                WireProfile("foi6moes", (), 0, 0, None, name="quest_record", checksum_required=False, response_limit=16 * 1024 * 1024, response_xml_limit=16 * 1024 * 1024),
                WireProfile("ntwh57oy", (), 0, 0, None, name="quest_exit", checksum_required=False),
                WireProfile("AekvKZk6", (), 0, 0, None, name="game_info", checksum_required=False, response_limit=0x400),
                WireProfile("E935RTof", (), 0, 0, None, name="session_flags", checksum_required=False,
                            response_limit=31, request_roots=("UI",), response_kind="integer_pair"),
            ])
        self.by_route: dict[str, list[WireProfile]] = defaultdict(list)
        self.by_registration: dict[int, WireProfile] = {}
        for profile in self.profiles:
            self.by_route[profile.route].append(profile)
            for number in profile.registration_keys:
                self.by_registration[number] = profile

    @classmethod
    def load(cls, path: Path | None = None) -> ProtocolRegistry:
        source = path or files("aigis_ps").joinpath("data/protocol-contracts.json")
        return cls(json.loads(source.read_text(encoding="utf-8"))["records"])

    def decode(self, route: str, wire: bytes) -> RequestEnvelope:
        profiles = self.by_route.get(route)
        if not profiles:
            raise ProtocolError("unknown_route", "No native contract for this route", 404)
        if len(wire) > MAX_REQUEST_BYTES:
            raise ProtocolError("request_too_large", "Request exceeds the wire size limit", 413)
        decoded: dict[int, tuple[bytes, ET.Element] | ProtocolError] = {}
        candidates = []
        errors = []
        for profile in profiles:
            if profile.request_key not in decoded:
                try:
                    xml = decode_wire(wire, profile.request_key)
                    decoded[profile.request_key] = (xml, parse_document(xml, allowed_roots=profile.request_roots))
                except ProtocolError as error:
                    decoded[profile.request_key] = error
            value = decoded[profile.request_key]
            if isinstance(value, ProtocolError):
                errors.append(value)
                continue
            xml, document = value
            try:
                if read_st(document) != profile.st:
                    continue
                if profile.checksum_required:
                    verify_checksum(xml, document)
                candidates.append(RequestEnvelope(profile, xml, document, hashlib.sha256(xml).hexdigest()))
            except ProtocolError as error:
                errors.append(error)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise ProtocolError("ambiguous_request", "Request matches multiple native contracts")
        if errors and all(error.code == errors[0].code for error in errors):
            raise errors[0]
        raise ProtocolError("contract_mismatch", "Encoding, ST or checksum does not match the route")


def xor_bytes(data: bytes, key: int) -> bytes:
    if not 0 <= key <= 255:
        raise ValueError("XOR key out of range")
    return data.translate(bytes(value ^ key for value in range(256))) if key else data


def validate_snappy(data: bytes, limit: int = MAX_XML_BYTES) -> int:
    size = 0
    offset = 0
    for shift in range(0, 35, 7):
        if offset >= len(data):
            raise ProtocolError("invalid_snappy", "Truncated Snappy length")
        byte = data[offset]
        offset += 1
        if shift == 28 and byte >= 16:
            raise ProtocolError("invalid_snappy", "Snappy length exceeds uint32")
        size |= (byte & 127) << shift
        if byte < 128:
            break
    if size > limit:
        raise ProtocolError("xml_too_large", "Uncompressed content exceeds the size limit", 413)
    produced = 0
    while offset < len(data):
        tag = data[offset]
        offset += 1
        kind = tag & 3
        if kind == 0:
            length = tag >> 2
            if length < 60:
                length += 1
            else:
                width = length - 59
                if offset + width > len(data):
                    raise ProtocolError("invalid_snappy", "Truncated literal length")
                length = int.from_bytes(data[offset:offset + width], "little") + 1
                offset += width
            if offset + length > len(data):
                raise ProtocolError("invalid_snappy", "Truncated literal")
            offset += length
        else:
            width = (1, 2, 4)[kind - 1]
            if offset + width > len(data):
                raise ProtocolError("invalid_snappy", "Truncated copy offset")
            distance = int.from_bytes(data[offset:offset + width], "little")
            offset += width
            if kind == 1:
                length = 4 + ((tag >> 2) & 7)
                distance |= (tag & 0xE0) << 3
            else:
                length = 1 + (tag >> 2)
            if distance == 0 or distance > produced:
                raise ProtocolError("invalid_snappy", "Invalid copy distance")
        produced += length
        if produced > size:
            raise ProtocolError("invalid_snappy", "Snappy output exceeds the declared length")
    if produced != size:
        raise ProtocolError("invalid_snappy", "Snappy output length mismatch")
    return size


def decode_wire(wire: bytes, key: int, limit: int = MAX_XML_BYTES) -> bytes:
    if key == 0:
        if len(wire) > limit:
            raise ProtocolError("xml_too_large", "Content exceeds the size limit", 413)
        return wire
    block = xor_bytes(wire, key)
    expected = validate_snappy(block, limit)
    try:
        value = bytes(cramjam.snappy.decompress_raw(block))
    except Exception as error:
        raise ProtocolError("invalid_snappy", "Unable to decode native Snappy content") from error
    if len(value) != expected:
        raise ProtocolError("invalid_snappy", "Snappy decoder length mismatch")
    return value


def encode_wire(xml: bytes, key: int) -> bytes:
    return xor_bytes(bytes(cramjam.snappy.compress_raw(xml)), key) if key else xml


def encode_response(profile: WireProfile, xml: bytes) -> tuple[bytes, str]:
    if profile.response_kind == "integer_pair":
        if not re.fullmatch(rb"-?[0-9]{1,10},-?[0-9]{1,10}", xml) or any(not int32(int(value)) for value in xml.split(b",")):
            raise ProtocolError("invalid_response", "Expected the native two-integer session response", 500)
    elif profile.response_kind == "xml":
        parse_document(xml, max_bytes=profile.response_xml_limit, max_nodes=profile.response_node_limit)
    else:
        raise ProtocolError("invalid_response_contract", "Unknown native response format", 500)
    wire = encode_wire(xml, profile.response_key)
    if len(wire) > profile.response_limit:
        raise ProtocolError(RESPONSE_TOO_LARGE, "Response exceeds the native receiver capacity", 500)
    media_type = "text/plain; charset=ascii" if profile.response_kind == "integer_pair" else "application/octet-stream" if profile.response_key else "text/xml; charset=utf-8"
    return wire, media_type


def parse_document(xml: bytes, *, allowed_roots: tuple[str, ...] = ("DA",), max_bytes: int = MAX_XML_BYTES, max_nodes: int = 1_000_000) -> ET.Element:
    if not xml or len(xml) > max_bytes:
        raise ProtocolError("invalid_xml", "Missing or oversized XML document")
    try:
        xml.decode("utf-8-sig")
        root = fromstring(xml, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (UnicodeDecodeError, ET.ParseError, DefusedXmlException, ValueError) as error:
        raise ProtocolError("invalid_xml", "Invalid or unsupported XML document") from error
    if root.tag not in allowed_roots:
        raise ProtocolError("invalid_xml", "Unexpected native XML root")
    stack = [(root, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if depth > 64 or count > max_nodes or len(node.tag) > 256:
            raise ProtocolError("xml_too_complex", "XML structure exceeds the supported limits", 413)
        stack.extend((child, depth + 1) for child in node)
    return root


def read_st(document: ET.Element) -> int | None:
    nodes = document.findall("ST")
    if not nodes:
        return None
    if len(nodes) != 1 or nodes[0].attrib or len(nodes[0]) or not re.fullmatch(r"[0-9]+", nodes[0].text or ""):
        raise ProtocolError("invalid_st", "Expected one integer ST")
    return int(nodes[0].text)


def native_checksum(xml: bytes) -> int:
    return signed32((sum(byte if byte < 128 else byte - 256 for byte in xml[::2]) & UINT32_MASK) ^ 0x773B1E)


def verify_checksum(xml: bytes, document: ET.Element) -> None:
    matches = list(CHECKSUM_ELEMENT.finditer(xml))
    if len(matches) != 1 or len(document.findall("HS")) != 1:
        raise ProtocolError("invalid_checksum", "Expected one native HS field")
    match = matches[0]
    if int(match.group(1)) != native_checksum(xml[:match.start()] + xml[match.end():]):
        raise ProtocolError("invalid_checksum", "Native HS does not match the payload")


def prepare_request(xml: bytes, st: int) -> bytes:
    document = parse_document(xml)
    if any(document.findall(name) for name in ("VN", "ST", "HS")):
        raise ValueError("Request already contains transport fields")
    if b"</DA>" not in xml:
        xml = ET.tostring(document, encoding="utf-8", short_empty_elements=False)
    xml = xml.replace(b"</DA>", b"<VN>5.0.0</VN></DA>")
    xml = xml.replace(b"</DA>", f"<ST>{st}</ST></DA>".encode("ascii"))
    xml = xml.replace(b"<RT></RT>", b"<RT/>")
    return xml.replace(b"</DA>", f"<HS>{native_checksum(xml)}</HS></DA>".encode("ascii"))
