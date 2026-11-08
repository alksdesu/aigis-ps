from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from .asset_service import IndexedAssetService
from .errors import ProtocolError
from .local_io import read_bounded, read_json_object
from .resource_fetch import PC_INDEX_NAME, ResourceEntry


LOCAL_CDN_PATH = "/client-assets"


@dataclass(frozen=True)
class CdnPayload:
    content: bytes
    etag: str


@dataclass(frozen=True)
class CdnResponse:
    status: int
    content: bytes
    headers: dict[str, str]


def load_bootstrap(directory: Path, client_hash: str) -> tuple[dict, bytes]:
    manifest = read_json_object(directory / "manifest.json", root=directory)
    if manifest.get("schema_version") != 1 or manifest.get("input_sha256") != client_hash or not isinstance(manifest.get("objects"), list) or len(manifest["objects"]) != 6:
        raise ValueError("Invalid fixed native resource manifest")

    def read_entry(entry):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or Path(entry["path"]).name != entry["path"]:
            raise ValueError("Invalid fixed resource path")
        raw = read_bounded(directory / entry["path"], 64 * 1024 * 1024, root=directory)
        if len(raw) != entry.get("size") or hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
            raise ValueError("Fixed resource content differs from its manifest")
        return raw

    objects = {}
    for entry in manifest["objects"]:
        raw = read_entry(entry)
        group, md5 = entry.get("group"), entry.get("md5")
        if not isinstance(group, str) or not re.fullmatch(r"[a-f0-9]{40}", group) or hashlib.md5(raw, usedforsecurity=False).hexdigest() != md5:
            raise ValueError("Invalid fixed resource address or content")
        if (group, md5) in objects:
            raise ValueError("Duplicate fixed resource address")
        objects[(group, md5)] = CdnPayload(raw, entry["sha256"])
    info = read_entry(manifest.get("game_info_text"))
    if not info.startswith(b"ALTB"):
        raise ValueError("Game information must retain its native ALTB container")
    return objects, info


class NativeCdnService:
    def __init__(self, version: str, index: bytes, entries, resource_root: Path, source_root: Path,
                 *, extra_objects=None, game_info_text: bytes | None = None, replacements=None,
                 base_version: str | None = None):
        if not re.fullmatch(r"[a-f0-9]{40}", version):
            raise ValueError("Invalid native resource version")
        if base_version is not None and not re.fullmatch(r"[a-f0-9]{40}", base_version):
            raise ValueError("Invalid base resource version")
        self.version = version
        self.base_version = base_version or version
        self.index = CdnPayload(index, hashlib.sha256(index).hexdigest())
        self.assets = IndexedAssetService(entries, resource_root, source_root, replacements=replacements)
        self.objects: dict[tuple[str, str], list[ResourceEntry]] = defaultdict(list)
        for entry in entries:
            self.objects[(entry.group, entry.md5)].append(entry)
        self.extra_objects = dict(extra_objects or {})
        self.game_info_text = None if game_info_text is None else CdnPayload(game_info_text, hashlib.sha256(game_info_text).hexdigest())

    def status(self) -> dict:
        return {"version": self.version, "path": LOCAL_CDN_PATH + "/", "index_name": PC_INDEX_NAME,
                "base_version": self.base_version, "local_replacements": len(self.assets.replacements),
                "objects": len(self.objects), "game_info_text": self.game_info_text is not None,
                "bootstrap_objects": len(self.extra_objects),
                "ready": self.game_info_text is not None and len(self.extra_objects) == 6}

    def read(self, group: str, object_name: str) -> CdnPayload:
        if group == self.version and object_name == PC_INDEX_NAME:
            return self.index
        if group == "000" and object_name == "_GameInfoText":
            if self.game_info_text is None:
                raise ProtocolError("game_info_text_unavailable", "Local game information has not been configured", 503)
            return self.game_info_text
        if (group, object_name) in self.extra_objects:
            return self.extra_objects[(group, object_name)]
        candidates = self.objects.get((group, object_name))
        if not candidates:
            raise ProtocolError("cdn_object_not_found", "Object is not in the selected native resource snapshot", 404)
        errors = []
        for entry in candidates:
            try:
                content, digest = self.assets.read_entry(entry)
                return CdnPayload(content, digest)
            except ProtocolError as error:
                errors.append(error)
        raise next((error for error in errors if error.status != 404), errors[0])


def respond(payload: CdnPayload, *, method: str = "GET", range_header: str | None = None,
            if_none_match: str | None = None, if_range: str | None = None) -> CdnResponse:
    etag = f'"{payload.etag}"'
    size = len(payload.content)
    headers = {"ETag": etag, "Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    if if_none_match == etag:
        return CdnResponse(304, b"", headers)
    selected = payload.content
    status = 200
    if range_header is not None and (if_range is None or if_range == etag):
        match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", range_header)
        invalid = match is None or size == 0
        if not invalid:
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            elif last and int(last) > 0:
                start, end = max(0, size - int(last)), size - 1
            else:
                start, end = 1, 0
            invalid = start > end or start >= size
        if invalid:
            headers.update({"Content-Range": f"bytes */{size}", "Content-Length": "0"})
            return CdnResponse(416, b"", headers)
        selected = payload.content[start:end + 1]
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        status = 206
    headers["Content-Length"] = str(len(selected))
    return CdnResponse(status, b"" if method == "HEAD" else selected, headers)
