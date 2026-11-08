import hashlib
import json
from pathlib import Path

import pytest

from aigis_ps.assets import asset_key
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.errors import ProtocolError
from aigis_ps import resource_library as module
from aigis_ps.resource_fetch import CDN_ORIGIN, PC_INDEX_NAME, ResourceEntry
from aigis_ps.resource_library import ResourceLibrary


CLIENT = "c" * 64
VERSION = "b" * 40


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    entries = tuple(ResourceEntry(name, "a" * 40, hashlib.md5(b"0123456789").hexdigest(), 5, 10, asset_key(name))
                    for name in ("Map9002.aar", "Map1001.aar"))
    text = "\n".join(f"{e.group},{e.md5},5,10,{e.cache_key},{e.name}" for e in entries)
    raw = bytes(value ^ 0xDA for value in text.encode())
    monkeypatch.setattr(module, "fetch_index", lambda version: (raw, entries))
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"schema_version": 1, "complete": True, "input_sha256": CLIENT,
                                  "version": VERSION, "origin": CDN_ORIGIN, "index_name": PC_INDEX_NAME}))
    library = ResourceLibrary(tmp_path / "library", client_hash=CLIENT)
    library.import_context(context)
    return library, entries, context


def test_context_activates_verified_pc_index_and_rejects_other_client(prepared):
    library, entries, context = prepared
    assert library.status()["ready"]
    assert library.load()[1] == entries
    data = json.loads(context.read_text())
    data["input_sha256"] = "d" * 64
    context.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        library.import_context(context)
    assert library.status()["version"] == VERSION


def test_index_tampering_and_invalid_pointer_are_not_ready(prepared):
    library, _, _ = prepared
    manifest, _ = library.load()
    (library.root / "indexes" / manifest["snapshot"] / "index.bin").write_bytes(b"bad")
    assert not library.status()["ready"]
    (library.root / "active-index.json").write_text("[]")
    assert not library.status()["ready"]


def plan(entries):
    return {"entries": [{"name": e.name, "size": e.size, "scopes": ["all", "startup"], "status": "missing"} for e in entries]}


def test_collection_budget_and_finished_files_are_recorded(prepared, monkeypatch, tmp_path):
    library, entries, _ = prepared
    monkeypatch.setattr(library, "_plan_snapshot", lambda *_: plan(entries))
    names = []

    class Fetcher:
        def __init__(self, _, **kwargs):
            pass

        def download(self, entry):
            names.append(entry.name)
            return {"status": "downloaded", "name": entry.name, "bytes": entry.size}

    monkeypatch.setattr(module, "ResourceFetcher", Fetcher)
    result = library.collect(tmp_path, None, max_bytes=10)
    assert result["status"] == "partial"
    assert result["completed"] == 1 and result["remaining_due_to_budget"] == 1
    assert names == ["Map9002.aar"]
    assert json.loads((Path(result["path"]) / "state.json").read_text())["downloaded_bytes"] == 10


def test_failed_resources_are_not_reported_as_complete(prepared, monkeypatch, tmp_path):
    library, entries, _ = prepared
    monkeypatch.setattr(library, "_plan_snapshot", lambda *_: plan(entries))

    class Fetcher:
        def __init__(self, _, **kwargs):
            pass

        def download(self, entry):
            raise ValueError("bad content")

    monkeypatch.setattr(module, "ResourceFetcher", Fetcher)
    result = library.collect(tmp_path, None)
    assert result["status"] == "partial" and result["completed"] == 0
    assert len(result["failed"]) == 2


def test_indexed_service_uses_verified_overlay_and_original_fallback(tmp_path):
    payload = b"ALTB-current-index-content"
    entry = ResourceEntry("Map9002.aar", "a" * 40, hashlib.md5(payload).hexdigest(), 5, len(payload))
    source, overlay = tmp_path / "source", tmp_path / "overlay"
    (source / ".cache").mkdir(parents=True)
    (overlay / ".cache").mkdir(parents=True)
    original = source / ".cache" / entry.cache_key
    original.write_bytes(payload)
    service = IndexedAssetService([entry], overlay, source)
    assert service.read(entry.name)[0] == payload
    copied = overlay / ".cache" / entry.cache_key
    copied.write_bytes(b"outdated")
    assert service.read(entry.name)[0] == payload
    copied.write_bytes(payload)
    original.unlink()
    assert service.read(entry.cache_key)[0] == payload
    copied.write_bytes(b"changed")
    with pytest.raises(ProtocolError) as error:
        service.read(entry.name)
    assert error.value.status == 409
    with pytest.raises(ProtocolError) as error:
        service.read("Unlisted.aar")
    assert error.value.status == 404


def test_resource_updates_and_collection_share_an_exclusive_lock(prepared):
    from aigis_ps.local_io import exclusive_file
    library, _, context = prepared
    with exclusive_file(library.root / "operation.lock"):
        with pytest.raises(ValueError, match="running"):
            library.import_context(context)
        with pytest.raises(ValueError, match="running"):
            library.collect(library.root, None)
    assert library.import_context(context)["version"] == VERSION


def test_atomic_progress_replace_retries_transient_windows_read_lock(tmp_path, monkeypatch):
    from aigis_ps import local_io
    from aigis_ps.resource_library import write_json
    original = local_io.os.replace
    attempts = []

    def locked_once(source, target):
        attempts.append(target)
        if len(attempts) == 1:
            raise PermissionError("transient read lock")
        original(source, target)

    monkeypatch.setattr(local_io.os, "replace", locked_once)
    monkeypatch.setattr(local_io.time, "sleep", lambda _: None)
    write_json(tmp_path / "state.json", {"completed": 1}, tmp_path)
    assert len(attempts) == 2
    assert json.loads((tmp_path / "state.json").read_text())["completed"] == 1
