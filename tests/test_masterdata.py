import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from aigis_ps.alxml import Column, Container, ValueType, dumps
from aigis_ps.errors import ProtocolError
from aigis_ps.local_io import MAX_JSON_DEPTH
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.public_tables import REQUIRED_FIELDS, MASTER_IDS, MASTER_LIMITS


CLIENT_HASH = "1" * 64
SCHEMAS = {name: sorted(fields) for name, fields in REQUIRED_FIELDS.items()}


def capture(path, *, empty=False, bad_second=False, fingerprint=CLIENT_HASH):
    path.mkdir()
    tables = {}
    for name, fields in SCHEMAS.items():
        values = () if empty and name == "CardData" else (1001, 1001) if bad_second and name == "QuestTable" else (1001, 1002)
        tables[name] = Container(children=tuple(Column(field, ValueType.INT, values) for field in fields))
    manifest = {"schema_version": 1, "complete": True, "input_sha256": fingerprint, "tables": {}}
    for name, table in tables.items():
        raw = dumps(table)
        (path / f"{name}.xml").write_bytes(raw)
        manifest["tables"][name] = {"path": f"{name}.xml", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    (path / "manifest.json").write_text(json.dumps(manifest))
    return path


def store(path):
    return MasterDataStore(path, expected_hash=CLIENT_HASH, schema_fields=SCHEMAS)


def alternate_capture(path):
    source = capture(path)
    manifest = json.loads((source / "manifest.json").read_text())
    for name in MASTER_IDS:
        target = source / f"{name}.xml"
        raw = target.read_bytes().replace(b">1001<", b">2001<").replace(b">1002<", b">2002<")
        target.write_bytes(raw)
        manifest["tables"][name].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    (source / "manifest.json").write_text(json.dumps(manifest))
    return source


def test_snapshot_remains_immutable_after_active_data_changes(tmp_path):
    target = store(tmp_path / "masters")
    first = target.import_capture(capture(tmp_path / "first"))
    snapshot = target.snapshot()
    original = dict(snapshot.documents)
    second = target.import_capture(alternate_capture(tmp_path / "second"))
    assert snapshot.identifier == first["snapshot"] != second["snapshot"]
    assert snapshot.client_hash == CLIENT_HASH
    assert dict(snapshot.documents) == original
    assert target.snapshot().get_xml("CardData") != original["CardData"]
    with pytest.raises(TypeError):
        snapshot.documents["CardData"] = b"changed"


def test_snapshot_cannot_mix_tables_during_an_import(tmp_path, monkeypatch):
    target = store(tmp_path / "masters")
    first = target.import_capture(capture(tmp_path / "first"))
    original = dict(target.snapshot().documents)
    next_capture = alternate_capture(tmp_path / "second")
    read_table = target._table_xml
    switched = False

    def read_after_import(directory, manifest, name):
        nonlocal switched
        if not switched:
            switched = True
            target.import_capture(next_capture)
        return read_table(directory, manifest, name)

    monkeypatch.setattr(target, "_table_xml", read_after_import)
    snapshot = target.snapshot()
    assert switched
    assert snapshot.identifier == first["snapshot"]
    assert dict(snapshot.documents) == original
    assert target.snapshot().identifier != snapshot.identifier


def test_snapshot_rejects_a_corrupted_second_table(tmp_path):
    target = store(tmp_path / "masters")
    manifest = target.import_capture(capture(tmp_path / "capture"))
    path = target.directory / "snapshots" / manifest["snapshot"] / "QuestTable.xml"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ProtocolError) as failure:
        target.snapshot()
    assert failure.value.code == "masterdata_invalid"


def test_complete_capture_import_and_reopen(tmp_path):
    source = capture(tmp_path / "capture")
    target = store(tmp_path / "masters")
    manifest = target.import_capture(source)
    assert manifest["tables"]["CardData"]["rows"] == 2
    assert store(tmp_path / "masters").get_xml("CardData").startswith(b'<?xml version="1.0"?>')
    assert target.status()["ready"]
    assert target.import_capture(source)["snapshot"] == manifest["snapshot"]


@pytest.mark.parametrize("options", [{"empty": True}, {"bad_second": True}, {"fingerprint": "2" * 64}])
def test_invalid_capture_does_not_activate_partial_data(tmp_path, options):
    target = store(tmp_path / "masters")
    with pytest.raises(ValueError):
        target.import_capture(capture(tmp_path / "capture", **options))
    assert not (tmp_path / "masters" / "active.json").exists()
    with pytest.raises(ProtocolError) as error:
        target.get_xml("CardData")
    assert error.value.code == "masterdata_missing"


def test_failed_update_preserves_previous_snapshot(tmp_path):
    target = store(tmp_path / "masters")
    initial = target.import_capture(capture(tmp_path / "good"))
    with pytest.raises(ValueError):
        target.import_capture(capture(tmp_path / "bad", bad_second=True))
    assert target.status()["snapshot"] == initial["snapshot"]


def test_capture_path_escape_and_content_changes_are_rejected(tmp_path):
    target = store(tmp_path / "masters")
    source = capture(tmp_path / "capture")
    p = source / "manifest.json"
    manifest = json.loads(p.read_text())
    manifest["tables"]["CardData"]["path"] = "../outside.xml"
    p.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="leaves"):
        target.import_capture(source)
    source2 = capture(tmp_path / "capture2")
    current = target.import_capture(source2)
    output = tmp_path / "masters" / "snapshots" / current["snapshot"] / "CardData.xml"
    output.write_bytes(output.read_bytes() + b" ")
    assert target.status()["code"] == "masterdata_invalid"


@pytest.mark.parametrize("value", [[], None, {"snapshot": 42}])
def test_invalid_active_structure_reports_unavailable(tmp_path, value):
    target = store(tmp_path / "masters")
    target.directory.mkdir()
    (target.directory / "active.json").write_text(json.dumps(value))
    assert target.status()["code"] == "masterdata_invalid"


def test_missing_required_and_unknown_columns_are_rejected(tmp_path):
    for index, unknown in enumerate((False, True)):
        source = capture(tmp_path / f"capture{index}")
        fields = tuple(Column(field, ValueType.INT, (1,)) for field in SCHEMAS["CardData"])
        data = Container(children=fields + (Column("PlayerPassword", ValueType.STRING, ("value",)),) if unknown else fields[:-1])
        raw = dumps(data)
        (source / "CardData.xml").write_bytes(raw)
        manifest = json.loads((source / "manifest.json").read_bytes())
        manifest["tables"]["CardData"].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        (source / "manifest.json").write_text(json.dumps(manifest))
        with pytest.raises(ValueError):
            store(tmp_path / "masters").import_capture(source)


def test_concurrent_identical_import_is_idempotent(tmp_path, monkeypatch):
    target = store(tmp_path / "masters")
    source = capture(tmp_path / "capture")
    barrier = Barrier(2)
    rename = Path.rename

    def synchronized(path, destination):
        if path.name.startswith("pending-"):
            barrier.wait(timeout=10)
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", synchronized)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: target.import_capture(source), range(2)))
    assert results[0] == results[1]
    assert target.status()["ready"]
    assert not list((target.directory / "snapshots").glob("pending-*"))


def test_oversized_snapshot_is_rejected_before_read(tmp_path, monkeypatch):
    target = store(tmp_path / "masters")
    current = target.import_capture(capture(tmp_path / "capture"))
    path = target.directory / "snapshots" / current["snapshot"] / "CardData.xml"
    with path.open("wb") as stream:
        stream.truncate(MASTER_LIMITS.max_bytes + 1)
    original = Path.open

    def no_read(file, *args, **kwargs):
        if file == path:
            pytest.fail("Oversized table was opened")
        return original(file, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_read)
    assert target.status()["code"] == "masterdata_invalid"


def test_snapshot_junction_is_rejected_before_writing(tmp_path, monkeypatch):
    target = store(tmp_path / "masters")
    link = target.directory / "snapshots"
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda path: path == link or original(path))
    with pytest.raises(ValueError, match="junction"):
        target.import_capture(capture(tmp_path / "capture"))
    assert not target.directory.exists()


def strict_variants(document):
    text = json.dumps(document)[1:-1]
    deep = "[" * (MAX_JSON_DEPTH + 1) + "]" * (MAX_JSON_DEPTH + 1)
    return {"duplicate": f'{{"snapshot": {json.dumps(document["snapshot"])}, {text}}}', "deep": f'{{{text}, "extra": {deep}}}'}


@pytest.mark.parametrize("variant", ["duplicate", "deep"])
def test_active_pointer_is_read_strictly(tmp_path, variant):
    target = store(tmp_path / "masters")
    target.import_capture(capture(tmp_path / "capture"))
    pointer = target.directory / "active.json"
    pointer.write_text(strict_variants(json.loads(pointer.read_bytes()))[variant])
    assert target.status()["code"] == "masterdata_invalid"
    with pytest.raises(ProtocolError) as error:
        target.pointer()
    assert error.value.code == "masterdata_invalid"


@pytest.mark.parametrize("variant", ["duplicate", "deep"])
def test_snapshot_manifests_are_read_strictly(tmp_path, variant):
    target = store(tmp_path / "masters")
    imported = target.import_capture(capture(tmp_path / "capture"))
    path = target.directory / "snapshots" / imported["snapshot"] / "manifest.json"
    path.write_text(strict_variants(json.loads(path.read_bytes()))[variant])
    [listed] = target.list_snapshots()
    assert (listed["snapshot"], listed["valid"]) == (imported["snapshot"], False)
