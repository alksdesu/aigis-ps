from __future__ import annotations

from collections.abc import Mapping
import hashlib
import re
from pathlib import Path
from types import MappingProxyType

from .al_format import MAX_RESOURCE_BYTES
from .assets import asset_key
from .errors import ProtocolError
from .local_io import checked_path, read_bounded


RESOURCE_MAGICS = {"ALL4", "ALLZ", "ALAR", "ALTB", "ALRD", "ALTX", "ALFT", "OggS", "ALIG", "ALOD", "ALMT"}
ASSET_NOT_FOUND = "asset_not_found"
ASSET_NOT_CACHED = "asset_not_cached"
ASSET_TOO_LARGE = "asset_too_large"
ASSET_CHANGED = "asset_changed"


class AssetService:
    def __init__(self, manifest: dict):
        if not isinstance(manifest, dict) or not isinstance(manifest.get("source_root"), str) or not isinstance(manifest.get("entries"), list):
            raise ValueError("Resource manifest must contain a source directory and entries")
        self.root = checked_path(Path(manifest["source_root"]))
        if not self.root.is_dir():
            raise ValueError("Resource source directory does not exist")
        self.entries = {}
        for entry in manifest["entries"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("magic"), str) or not isinstance(entry.get("md5"), dict) or not isinstance(entry["md5"].get("status"), str):
                raise ValueError("Invalid resource manifest entry")
            if entry["magic"] not in RESOURCE_MAGICS or entry["md5"]["status"] in ("mismatch", "invalid"):
                continue
            if any(not isinstance(entry.get(field), str) or not re.fullmatch(r"[a-f0-9]{64}", entry[field]) for field in ("filename_hash", "sha256")):
                raise ValueError("Invalid resource manifest hash")
            if type(entry.get("size")) is not int or entry["size"] < 0 or not isinstance(entry.get("source_path"), str):
                raise ValueError("Invalid resource manifest path or size")
            checked_path(self.root / entry["source_path"], self.root)
            key = entry["filename_hash"]
            if key in self.entries:
                raise ValueError("Ambiguous resource hash in manifest")
            self.entries[key] = entry

    def read(self, name: str) -> tuple[bytes, str]:
        key = name if len(name) == 64 and all(c in "0123456789abcdef" for c in name) else asset_key(name)
        entry = self.entries.get(key)
        if entry is None:
            raise ProtocolError(ASSET_NOT_FOUND, "No approved local resource for this name", 404)
        if entry["size"] > MAX_RESOURCE_BYTES:
            raise ProtocolError(ASSET_TOO_LARGE, "Resource exceeds the in-memory serving limit", 413)
        try:
            content = read_bounded(self.root / entry["source_path"], entry["size"], root=self.root)
        except (ValueError, OSError) as error:
            raise ProtocolError(ASSET_CHANGED, "Resource differs from the indexed version", 409) from error
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != entry["size"] or digest != entry["sha256"]:
            raise ProtocolError(ASSET_CHANGED, "Resource differs from the indexed version", 409)
        return content, digest


class IndexedAssetService:
    def __init__(self, entries, resource_root: Path, source_root: Path, *, replacements=None):
        self.entries = {entry.cache_key: entry for entry in entries}
        self.resource_root = checked_path(resource_root)
        self.source_root = checked_path(source_root)
        if replacements is not None and not isinstance(replacements, Mapping):
            raise ValueError("Local replacements must be a resource mapping")
        overridden = {}
        for name, content in (replacements or {}).items():
            if not isinstance(name, str):
                raise ValueError("Local replacement names must be strings")
            key = asset_key(name)
            entry = self.entries.get(key)
            if (entry is None or entry.name != name or not isinstance(content, bytes)
                    or len(content) > MAX_RESOURCE_BYTES or len(content) != entry.size
                    or hashlib.md5(content, usedforsecurity=False).hexdigest() != entry.md5):
                raise ValueError("Local replacement differs from the effective resource index")
            overridden[key] = content
        self.replacements = MappingProxyType(overridden)

    def read(self, name: str) -> tuple[bytes, str]:
        key = name if re.fullmatch(r"[a-f0-9]{64}", name) else asset_key(name)
        entry = self.entries.get(key)
        if entry is None:
            raise ProtocolError(ASSET_NOT_FOUND, "Resource is not in the current public index", 404)
        return self.read_entry(entry)

    def read_entry(self, entry) -> tuple[bytes, str]:
        key = entry.cache_key
        if entry.size > MAX_RESOURCE_BYTES:
            raise ProtocolError(ASSET_TOO_LARGE, "Resource exceeds the in-memory serving limit", 413)
        if key in self.replacements:
            content = self.replacements[key]
            if self.entries[key] != entry:
                raise ProtocolError(ASSET_CHANGED, "Requested replacement differs from the effective index", 409)
            return content, hashlib.sha256(content).hexdigest()
        candidates = ((self.resource_root / ".cache" / key, self.resource_root),
                      (self.source_root / ".cache" / key, self.source_root),
                      (self.source_root / key, self.source_root))
        invalid = False
        for path, root in candidates:
            if not path.exists():
                continue
            try:
                content = read_bounded(path, entry.size, root=root)
                if len(content) != entry.size or hashlib.md5(content, usedforsecurity=False).hexdigest() != entry.md5:
                    raise ValueError("Resource content differs from the active index")
            except (OSError, ValueError):
                invalid = True
                continue
            return content, hashlib.sha256(content).hexdigest()
        if invalid:
            raise ProtocolError(ASSET_CHANGED, "Cached resource differs from the active public index", 409)
        raise ProtocolError(ASSET_NOT_CACHED, "Indexed resource has not been cached yet", 404)


class AdditionalAssetService:
    def __init__(self, indexed: IndexedAssetService, additional: AssetService):
        self.indexed = indexed
        self.additional = additional

    def read(self, name: str) -> tuple[bytes, str]:
        key = name if re.fullmatch(r"[a-f0-9]{64}", name) else asset_key(name)
        if key in self.indexed.entries:
            return self.indexed.read(name)
        return self.additional.read(name)

