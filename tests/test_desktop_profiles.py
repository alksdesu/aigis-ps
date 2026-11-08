from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path

import pytest

from aigis_ps.alxml import Column, Container, ValueType, dumps
from aigis_ps.config_sets import ConfigSets
from aigis_ps.desktop_profiles import MAX_NAME_LENGTH, ProfileStore
from aigis_ps import desktop_profiles
from aigis_ps.local_io import exclusive_file
from aigis_ps.masterdata import MasterDataStore, json_bytes
from aigis_ps.public_tables import REQUIRED_FIELDS


def seed(data_dir, capture_dir, *, first_id=1001):
    masters = MasterDataStore.for_data_dir(data_dir)
    capture_dir.mkdir(parents=True)
    manifest = {"schema_version": 1, "complete": True, "input_sha256": masters.expected_hash, "tables": {}}
    for name, fields in REQUIRED_FIELDS.items():
        document = Container(children=tuple(Column(field, ValueType.INT, (first_id, first_id + 1))
                                            for field in sorted(fields)))
        raw = dumps(document)
        (capture_dir / f"{name}.xml").write_bytes(raw)
        manifest["tables"][name] = {"path": f"{name}.xml", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    (capture_dir / "manifest.json").write_bytes(json_bytes(manifest))
    return masters.import_capture(capture_dir)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    seed(root / "runtime/server", tmp_path / "capture")
    (root / "runtime/server/aigis.sqlite3").write_bytes(b"isolated legacy database sentinel")
    return root


def metadata(store):
    return json.loads(store.path.read_bytes())


def write_metadata(store, value):
    store.path.write_bytes(json_bytes(value))


def no_staging(project):
    root = project / "runtime/saves"
    return not root.exists() or not list(root.glob(".creating-*"))


def test_empty_initialization_never_creates_or_opens_a_player_database(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert path.suffix != ".sqlite3"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    store = ProfileStore(root)
    assert store.list() == [] and store.selected() is None
    assert metadata(store) == {"schema_version": 1, "selected": None, "profiles": []}
    assert not (root / "runtime/server").exists()
    assert ProfileStore(root).list() == []


def test_existing_legacy_database_is_registered_using_metadata_only(project, monkeypatch):
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert path.suffix != ".sqlite3"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    store = ProfileStore(project)
    root = project
    expected = {"id": "default", "name": "当前存档", "data_dir": str(root / "runtime/server"), "selected": True}
    assert store.list() == [expected]
    assert store.selected() == expected
    assert metadata(store)["profiles"][0]["data_dir"] == "runtime/server"
    assert ProfileStore(root).list() == [expected]


@pytest.mark.parametrize("empty_database", [False, True])
def test_public_seed_without_player_does_not_create_a_profile(project, empty_database):
    database = project / "runtime/server/aigis.sqlite3"
    database.unlink()
    if empty_database:
        database.touch()
    store = ProfileStore(project)
    assert store.list() == [] and store.selected() is None
    assert ProfileStore(project).list() == []


def test_first_created_profile_is_selected_and_contains_only_public_data(project):
    (project / "runtime/server/aigis.sqlite3").unlink()
    store = ProfileStore(project)
    first = store.create("第一档")
    assert first["selected"] is True and store.selected() == first
    assert metadata(store)["selected"] == first["id"]
    assert store.list() == [first] and ProfileStore(project).list() == [first]
    target = Path(first["data_dir"])
    assert sorted(path.name for path in target.iterdir()) == ["masterdata"]
    assert not list(target.rglob("*.sqlite3"))
    second = store.create("第二档")
    assert second["selected"] is False and store.selected() == first


def test_existing_registry_and_selection_are_preserved_without_inspecting_legacy_database(project, monkeypatch):
    store = ProfileStore(project)
    second = store.create("第二档")
    store.select(second["id"])
    before = store.path.read_bytes()
    profiles = store.list()
    database = project / "runtime/server/aigis.sqlite3"
    original = Path.stat

    def guarded(path, *args, **kwargs):
        assert path != database
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded)
    reopened = ProfileStore(project)
    assert reopened.list() == profiles and reopened.selected()["id"] == second["id"]
    assert reopened.path.read_bytes() == before


@pytest.mark.parametrize("invalid", ["directory", "hardlink", "junction"])
def test_invalid_legacy_database_path_is_refused_before_registry_creation(project, monkeypatch, invalid):
    database = project / "runtime/server/aigis.sqlite3"
    if invalid == "directory":
        database.unlink()
        database.mkdir()
    elif invalid == "hardlink":
        os.link(database, database.with_name("alias.sqlite3"))
    else:
        original = Path.is_junction
        monkeypatch.setattr(Path, "is_junction", lambda path: path == database or original(path))
    original_open = Path.open

    def guarded(path, *args, **kwargs):
        assert path != database
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    with pytest.raises(ValueError):
        ProfileStore(project)
    assert not (project / "runtime/desktop/profiles.json").exists()


def test_missing_first_profile_seed_has_clear_error_and_preserves_empty_registry(tmp_path):
    store = ProfileStore(tmp_path)
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="公开主表或规则文件缺失"):
        store.create("第一档")
    assert store.selected() is None and store.list() == []
    assert store.path.read_bytes() == before and no_staging(tmp_path)
    assert list((tmp_path / "runtime/saves").iterdir()) == []


def test_first_profile_selection_is_committed_with_registration(project, monkeypatch):
    (project / "runtime/server/aigis.sqlite3").unlink()
    store = ProfileStore(project)
    before = store.path.read_bytes()

    def fail(source, destination):
        value = json.loads(source.read_bytes())
        assert value["selected"] == value["profiles"][0]["id"]
        raise OSError("disk failure")

    monkeypatch.setattr(desktop_profiles, "atomic_replace", fail)
    with pytest.raises(OSError, match="disk failure"):
        store.create("第一档")
    assert store.path.read_bytes() == before and store.selected() is None and store.list() == []


def test_creation_copies_only_public_dependencies_and_preserves_the_selected_profile(project, monkeypatch):
    source = project / "runtime/server"
    sets = ConfigSets(source)
    parent = sets.create({"trade-rules": {"reset_type_1": "never"}}, source="console")
    current = sets.create({"trade-rules": {"reset_type_1": "month"}}, source="console", base=parent.identifier)
    sets.activate(current.identifier, 4, expected=None)
    forbidden = [source / "aigis.sqlite3", source / "aigis.sqlite3-wal", source / "backups/private.sqlite3",
                 source / "admin/access.json", source / "exports/private.json", source / "config/staging/private.json",
                 current.directory / "private.sqlite3", source / "masterdata/private.sqlite3"]
    for path in forbidden:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"private sentinel")
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert path not in forbidden
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    store = ProfileStore(project)
    created = store.create("  新档  ")
    target = Path(created["data_dir"])
    assert created == {"id": target.name, "name": "新档", "data_dir": str(target), "selected": False}
    assert len(created["id"]) == 32 and target.parent == project / "runtime/saves"
    assert store.selected()["id"] == "default"
    assert sorted(path.name for path in target.iterdir()) == ["config", "masterdata"]
    assert sorted(path.name for path in (target / "config").iterdir()) == ["active.json", "sets"]
    assert not list(target.rglob("*.sqlite3"))
    assert not list(target.rglob("private*"))
    for relative in ("config/active.json", "masterdata/active.json"):
        assert (target / relative).read_bytes() == (source / relative).read_bytes()
        assert not os.path.samefile(target / relative, source / relative)
    assert ConfigSets(target).read_active() == sets.read_active()
    assert ConfigSets(target).load(parent.identifier).explicit == parent.explicit
    assert MasterDataStore.for_data_dir(target).snapshot() == MasterDataStore.for_data_dir(source).snapshot()
    assert no_staging(project)


def test_selection_is_persistent_and_new_profiles_inherit_the_selected_public_tables(project, tmp_path):
    store = ProfileStore(project)
    first = store.create("第一档")
    assert store.select(first["id"]) == {**first, "selected": True}
    selected_dir = Path(first["data_dir"])
    new_master = seed(selected_dir, tmp_path / "next-capture", first_id=2001)
    second = ProfileStore(project).create("第二档")
    assert MasterDataStore.for_data_dir(Path(second["data_dir"])).snapshot().identifier == new_master["snapshot"]
    assert ProfileStore(project).selected()["id"] == first["id"]
    assert store.select("default")["selected"] is True
    assert sum(item["selected"] for item in store.list()) == 1


@pytest.mark.parametrize("name", [None, 4, "", "   ", "a\nb", "a\0b", "x" * (MAX_NAME_LENGTH + 1)])
def test_invalid_names_fail_without_creating_a_directory(project, name):
    store = ProfileStore(project)
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.create(name)
    assert store.path.read_bytes() == before
    assert not (project / "runtime/saves").exists()


def test_duplicate_names_and_unknown_ids_are_rejected(project):
    store = ProfileStore(project)
    store.create("Example")
    with pytest.raises(ValueError, match="同名"):
        store.create(" example ")
    for profile_id in (None, "../server", "A" * 32, "0" * 32):
        with pytest.raises(ValueError):
            store.select(profile_id)
    assert store.selected()["id"] == "default"


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(schema_version=True),
    lambda value: value.update(extra=True),
    lambda value: value.update(selected="0" * 32),
    lambda value: value.update(selected=None),
    lambda value: value.update(profiles=[]),
    lambda value: value["profiles"][0].update(data_dir="runtime/server/../server"),
    lambda value: value["profiles"][0].update(data_dir="../foreign"),
    lambda value: value["profiles"].append(dict(value["profiles"][0])),
    lambda value: value["profiles"][0].update(name=" padded "),
    lambda value: value["profiles"][0].update(id="0" * 32, data_dir="runtime/saves/" + "0" * 32),
])
def test_corrupt_metadata_fails_closed_without_replacing_it(project, mutation):
    store = ProfileStore(project)
    value = metadata(store)
    mutation(value)
    write_metadata(store, value)
    corrupt = store.path.read_bytes()
    with pytest.raises(ValueError):
        ProfileStore(project)
    assert store.path.read_bytes() == corrupt


def test_duplicate_json_keys_are_rejected(project):
    store = ProfileStore(project)
    store.path.write_bytes(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(ValueError, match="Duplicate"):
        store.list()


def test_custom_state_directory_stays_within_the_project(project, tmp_path):
    store = ProfileStore(project, Path("private/desktop"))
    assert store.path == project / "private/desktop/profiles.json"
    assert store.selected()["data_dir"] == str(project / "runtime/server")
    outside = tmp_path / "outside"
    for path in (outside, Path("../outside")):
        with pytest.raises(ValueError):
            ProfileStore(project, path)
    assert not outside.exists()


@pytest.mark.parametrize("relative", ["runtime/server", "runtime/server/masterdata/active.json", "runtime/desktop",
                                     "runtime/saves"])
def test_junctions_on_metadata_and_data_paths_are_rejected(project, monkeypatch, relative):
    store = ProfileStore(project)
    link = project / relative
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda path: path == link or original(path))
    with pytest.raises(ValueError, match="Links"):
        store.create("新档")


def test_hardlinked_metadata_and_public_files_are_rejected_before_reading(project, monkeypatch):
    store = ProfileStore(project)
    pointer = project / "runtime/server/masterdata/active.json"
    alias = pointer.with_name("alias.json")
    os.link(pointer, alias)
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert path != pointer
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    with pytest.raises(ValueError, match="普通文件"):
        store.create("新档")
    assert store.selected()["id"] == "default" and no_staging(project)
    os.link(store.path, store.path.with_name("alias.json"))
    with pytest.raises(ValueError, match="普通文件"):
        store.list()


def test_missing_or_corrupt_master_is_not_registered(project):
    store = ProfileStore(project)
    before = store.path.read_bytes()
    pointer = project / "runtime/server/masterdata/active.json"
    pointer.unlink()
    with pytest.raises(ValueError, match="公开主表或规则文件缺失"):
        store.create("新档")
    assert store.path.read_bytes() == before and no_staging(project)


def test_changed_configuration_content_is_rejected_without_copying_a_player(project):
    source = project / "runtime/server"
    sets = ConfigSets(source)
    current = sets.create({"trade-rules": {"reset_type_1": "never"}}, source="console")
    sets.activate(current.identifier, 1, expected=None)
    current.path("trade-rules").write_bytes(b'{"reset_type_1":"month"}')
    store = ProfileStore(project)
    with pytest.raises(ValueError):
        store.create("新档")
    assert len(store.list()) == 1 and no_staging(project)


def test_corrupt_original_master_is_rejected_even_when_served_tables_are_valid(project):
    source = project / "runtime/server"
    masters = MasterDataStore.for_data_dir(source)
    snapshot = masters.snapshot()
    raw = source / "masterdata/snapshots" / snapshot.identifier / "CardData.source.xml"
    raw.write_bytes(raw.read_bytes().replace(b">1001<", b">2001<"))
    store = ProfileStore(project)
    with pytest.raises(ValueError, match="原始公开主表"):
        store.create("新档")
    assert len(store.list()) == 1 and no_staging(project)


def test_a_directory_created_by_another_writer_is_never_deleted(project, monkeypatch):
    store = ProfileStore(project)
    original = store._copy_public
    destination = project / "runtime/saves" / ("1" * 32)
    uuid_values = iter(("1" * 32, "2" * 32))

    class Identifier:
        def __init__(self):
            self.hex = next(uuid_values)

    def occupy(source, staging, written):
        original(source, staging, written)
        destination.mkdir()
        (destination / "owner.txt").write_text("another writer", encoding="utf-8")

    monkeypatch.setattr(desktop_profiles.uuid, "uuid4", Identifier)
    monkeypatch.setattr(store, "_copy_public", occupy)
    with pytest.raises(ValueError, match="已经存在"):
        store.create("新档")
    assert (destination / "owner.txt").read_text(encoding="utf-8") == "another writer"
    assert len(store.list()) == 1 and no_staging(project)


def test_source_pointer_changes_abort_before_publication(project, monkeypatch):
    store = ProfileStore(project)
    pointer = project / "runtime/server/masterdata/active.json"
    raw = pointer.read_bytes()
    original = store._write_public

    def change_pointer(staging, relative, data, written):
        original(staging, relative, data, written)
        if relative == Path("masterdata/active.json"):
            pointer.write_bytes(raw + b"\n")

    monkeypatch.setattr(store, "_write_public", change_pointer)
    with pytest.raises(ValueError, match="指针已变化"):
        store.create("新档")
    assert len(store.list()) == 1 and no_staging(project)
    assert list((project / "runtime/saves").iterdir()) == []


def test_failed_metadata_publication_keeps_previous_registry_and_unregistered_public_directory(project, monkeypatch):
    store = ProfileStore(project)
    before = store.path.read_bytes()

    def fail(source, target):
        raise OSError("disk failure")

    monkeypatch.setattr(desktop_profiles, "atomic_replace", fail)
    with pytest.raises(OSError, match="disk failure"):
        store.create("新档")
    assert store.path.read_bytes() == before
    assert len(store.list()) == 1 and no_staging(project)
    orphan, = (project / "runtime/saves").iterdir()
    assert MasterDataStore.for_data_dir(orphan).status()["ready"]
    assert not list(orphan.rglob("*.sqlite3"))
    assert not list(store.state_dir.glob("*.tmp"))


@pytest.mark.parametrize("legacy", [False, True])
def test_parallel_instances_do_not_lose_profile_registrations(project, legacy):
    if not legacy:
        (project / "runtime/server/aigis.sqlite3").unlink()
    stores = [ProfileStore(project) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        created = list(executor.map(lambda item: item[1].create(f"档位 {item[0]}"), enumerate(stores)))
    expected_ids = {item["id"] for item in created} | ({"default"} if legacy else set())
    assert {item["id"] for item in stores[0].list()} == expected_ids
    assert stores[0].selected()["id"] == ("default" if legacy else next(item["id"] for item in created if item["selected"]))
    assert sum(item["selected"] for item in stores[0].list()) == 1
    assert no_staging(project)


def test_external_metadata_lock_refuses_writes(project):
    store = ProfileStore(project)
    before = store.path.read_bytes()
    with exclusive_file(store.state_dir / "profiles.lock"):
        with pytest.raises(ValueError):
            store.create("新档")
    assert store.path.read_bytes() == before and no_staging(project)
