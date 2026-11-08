from __future__ import annotations

import re

from .errors import ProtocolError
from .native_numbers import int32
from .protocol import RequestEnvelope


COMMON_FIELDS = frozenset(("UI", "UK", "MK", "CT", "PFN", "PlatformCode", "GPVI", "GPOT", "VN", "ST", "HS"))
# Native A56F90 (116) and A62B70 (137) append the same PlatformCode again after the common header.
DUPLICATED_PLATFORM_CODE = frozenset((116, 137))
MAX_ROWS = 4096
MAX_STRING_BYTES = 4096
_INTEGER = re.compile(r"[+-]?[0-9]{1,10}\Z")


def invalid(message: str):
    raise ProtocolError("invalid_request_fields", message)


def fields(node, allowed, required=()):
    if node.attrib or (node.text or "").strip() or (node.tail or "").strip():
        invalid("Expected an untyped container without mixed text")
    result = {}
    for child in node:
        if child.tag in result or child.tag not in allowed or (child.tail or "").strip():
            invalid("Request contains duplicate or unknown fields")
        result[child.tag] = child
    if set(required) - result.keys():
        invalid("Request is missing required fields")
    return result


def values(node, kind: str, *, minimum=0, maximum=MAX_ROWS, max_bytes=MAX_STRING_BYTES):
    if kind not in ("I", "S", "B"):
        raise ValueError("Unsupported request field type")
    if (node.attrib != {"T": kind} or (node.text or "").strip() or (node.tail or "").strip()
            or not minimum <= len(node) <= maximum):
        invalid("Request field type or value count is invalid")
    result = []
    for child in node:
        if child.tag != "V" or child.attrib or len(child) or (child.tail or "").strip():
            invalid("Request values must be plain V elements")
        text = child.text or ""
        if kind == "I":
            if not _INTEGER.fullmatch(text):
                invalid("Expected a decimal int32 value")
            value = int(text)
            if not int32(value):
                invalid("Integer exceeds the native int32 range")
        elif kind == "B":
            if text not in ("true", "false"):
                invalid("Expected a native boolean value")
            value = text == "true"
        else:
            if len(text.encode("utf-8")) > max_bytes:
                invalid("String exceeds the request field limit")
            value = text
        result.append(value)
    return tuple(result)


def scalar(node, kind: str, *, max_bytes=MAX_STRING_BYTES):
    return values(node, kind, minimum=1, maximum=1, max_bytes=max_bytes)[0]


def registered(envelope: RequestEnvelope, numbers, business, required=()):
    profile = envelope.profile
    if (not profile.registration_keys or set(profile.registration_keys) - set(numbers)
            or profile.name != "registered" or envelope.document.tag != "DA"):
        invalid("Unexpected registered operation")
    result = fields(envelope.document, COMMON_FIELDS | set(business), COMMON_FIELDS | set(required))
    for name in ("UI", "UK", "MK", "PFN", "GPVI", "GPOT"):
        scalar(result[name], "S")
    scalar(result["CT"], "I")
    copies = 2 if set(profile.registration_keys) <= DUPLICATED_PLATFORM_CODE else 1
    if len(set(values(result["PlatformCode"], "I", minimum=1, maximum=copies))) != 1:
        invalid("Repeated PlatformCode values must match")
    for name in ("VN", "ST", "HS"):
        node = result[name]
        if node.attrib or len(node) or not node.text or len(node.text.encode("utf-8")) > 256:
            invalid("Protocol metadata must be a bounded plain scalar")
    return result
