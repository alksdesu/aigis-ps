from __future__ import annotations

from collections.abc import Mapping
import csv
from dataclasses import dataclass, replace
import hashlib
import io
import json
import re
from types import MappingProxyType

from .resource_fetch import ResourceEntry, ResourceFetchError, parse_index


MAX_REPLACEMENTS = 256
MAX_REPLACEMENT_BYTES = 64 * 1024 * 1024
_XOR_TABLE = bytes(value ^ 0xDA for value in range(256))


@dataclass(frozen=True)
class ResourceOverlay:
    version: str
    index: bytes
    entries: tuple[ResourceEntry, ...]
    replacements: Mapping[str, bytes]
    object_overrides: Mapping[tuple[str, str], bytes]
    base_version: str

    def __post_init__(self):
        object.__setattr__(self, "entries", tuple(self.entries))
        object.__setattr__(self, "replacements", MappingProxyType(dict(self.replacements)))
        object.__setattr__(self, "object_overrides", MappingProxyType(dict(self.object_overrides)))


def _validate_entries(entries):
    names, sizes = {}, {}
    for entry in entries:
        if names.setdefault(entry.cache_key, entry.name) != entry.name:
            raise ResourceFetchError("overlay_name_collision", "Resource names share a cache identity")
        address = entry.group, entry.md5
        if sizes.setdefault(address, entry.size) != entry.size:
            raise ResourceFetchError("overlay_object_conflict", "One resource object has inconsistent byte sizes")


def _encode_index(entries):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for entry in entries:
        suffix = (entry.native_cache_key, entry.name) if entry.native_cache_key is not None else (entry.name,)
        writer.writerow((entry.group, entry.md5, entry.flags, f"{entry.size:012d}", *suffix))
    raw = output.getvalue().encode("utf-8").translate(_XOR_TABLE)
    if parse_index(raw) != entries:
        raise ResourceFetchError("overlay_index_mismatch", "Effective resource index failed its round-trip check")
    return raw


def build_resource_overlay(base_version: str, base_index: bytes, replacements: Mapping[str, bytes]) -> ResourceOverlay:
    if not isinstance(base_version, str) or re.fullmatch(r"[0-9a-f]{40}", base_version) is None:
        raise ResourceFetchError("invalid_overlay_version", "Base resource version must be a lowercase 40-digit hash")
    entries = parse_index(base_index)
    _validate_entries(entries)
    if not isinstance(replacements, Mapping) or len(replacements) > MAX_REPLACEMENTS:
        raise ResourceFetchError("invalid_overlay_replacements", "Resource replacements must be a bounded mapping")
    by_name = {entry.name: entry for entry in entries}
    changed, hashes = {}, {}
    for name, payload in replacements.items():
        if not isinstance(name, str) or name not in by_name:
            raise ResourceFetchError("overlay_resource_unknown", "Only indexed resource names can be replaced")
        if not isinstance(payload, bytes) or len(payload) > MAX_REPLACEMENT_BYTES:
            raise ResourceFetchError("invalid_overlay_payload", "Replacement content must be bounded immutable bytes")
        digest = hashlib.md5(payload, usedforsecurity=False).hexdigest()
        original = by_name[name]
        if original.size == len(payload) and original.md5 == digest:
            continue
        changed[name] = payload
        hashes[name] = digest
    if not changed:
        return ResourceOverlay(base_version, base_index, entries, {}, {}, base_version)
    fingerprint = {"schema_version": 1, "base_version": base_version,
                   "base_index_sha256": hashlib.sha256(base_index).hexdigest(),
                   "replacements": {name: hashlib.sha256(payload).hexdigest() for name, payload in sorted(changed.items())}}
    version = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:40]
    effective = tuple(replace(entry, group=version, md5=hashes[entry.name], size=len(changed[entry.name]))
                      if entry.name in changed else entry for entry in entries)
    _validate_entries(effective)
    objects = {}
    for name, payload in changed.items():
        address = version, hashes[name]
        if address in objects and objects[address] != payload:
            raise ResourceFetchError("overlay_object_collision", "Different replacement bytes share one resource address")
        objects[address] = payload
    return ResourceOverlay(version, _encode_index(effective), effective, changed, objects, base_version)
