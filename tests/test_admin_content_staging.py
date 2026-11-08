from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aigis_ps import admin_routes_config, admin_routes_content, admin_routes_state, masterdata
from aigis_ps.admin_app import create_admin_app
from aigis_ps.admin_command import PlanStore
from aigis_ps.admin_commit import InitializerRestartCheck
from aigis_ps.admin_config_switch import (CONTENT_PHRASE, EMPTY_SET, ConfigSwitch, ContentInputs, config_switch,
                                          install, roll_forward)
from aigis_ps.admin_registry import AdminCatalog, CommandRegistry, RouteRegistry, ViewRegistry
from aigis_ps.admin_security import API_ROOT
from aigis_ps.alxml import Column, Container, ValueType, dumps
from aigis_ps.config_registry import REGISTRY
from aigis_ps.config_sets import ConfigSets, resolve
from aigis_ps.errors import ProtocolError
from aigis_ps.generation import Generation, GenerationIdentity, GenerationInputs, GenerationRuntime
from aigis_ps.local_io import exclusive_file
from aigis_ps.masterdata import ACTIVE_POINTER, MAX_POINTER_BYTES, POINTER_LOCK, SNAPSHOTS_DIRECTORY, MasterDataStore
from aigis_ps.public_tables import REQUIRED_FIELDS
from aigis_ps.startup_diagnostics import INTERNAL_ERROR, StartupDiagnostics
from aigis_ps.storage import Store

from test_admin_config_switch import (AUTH, BASE, DATABASE, DOMAINS, GAME_PORT, GOLD, PORT, RANKS, SHORT, TOKEN, Crash,
                                      Monotonic, ReadbackPreflight, Wall, arguments, audit, battle_journal, error_of,
                                      failures, history, operation, player_state, refreshed, report_for, switch_body,
                                      switched, synchronous, validated)


CAPTURES = "captures"
COLUMNS = {name: sorted(columns) for name, columns in REQUIRED_FIELDS.items()}
FIRST, SECOND, THIRD = 1000, 2000, 3000
# Deeper than the JSON decoder recurses yet well inside the manifest size limit.
DEEP_NESTING = 100_000
RESOURCES = "resources-1"
ROUTE_MODULES = (admin_routes_config, admin_routes_content, admin_routes_state)


def write_capture(root, name, base, fingerprint, *, complete=True):
    directory = root / name
    directory.mkdir(parents=True)
    manifest = {"schema_version": 1, "complete": complete, "input_sha256": fingerprint, "tables": {}}
    for table, columns in COLUMNS.items():
        values = (base + 1, base + 2)
        raw = dumps(Container(children=tuple(Column(column, ValueType.INT, values) for column in columns)))
        (directory / f"{table}.xml").write_bytes(raw)
        manifest["tables"][table] = {"path": f"{table}.xml", "bytes": len(raw),
                                     "sha256": hashlib.sha256(raw).hexdigest()}
    (directory / "manifest.json").write_bytes(json.dumps(manifest).encode("utf-8"))
    return directory


@dataclass
class MasterWorld:
    wall: Wall
    masters: MasterDataStore
    honors_pin: bool = True
    built: list = field(default_factory=list)

    def build(self, inputs, progress):
        progress("loading masters")
        pinned = inputs.master_snapshot if self.honors_pin and isinstance(inputs, ContentInputs) else None
        snapshot = self.masters.snapshot(pinned)
        quest = inputs.config.load_rules()["quest-rules"]
        identity = GenerationIdentity(code_digest="c" * 64, config_set=inputs.config.digest,
                                      content_versions={"main_quests": REGISTRY["quest-rules"].content_version(quest)},
                                      master_snapshot=snapshot.identifier, base_resource_version=RESOURCES,
                                      effective_resource_version=RESOURCES)
        bundle = SimpleNamespace(bootstrap=SimpleNamespace(clock=self.wall, rank_rows=list(RANKS)), initialize=dict,
                                 masters=self.masters)
        self.built.append(inputs)
        return Generation(identity, bundle, {}, {"startup_ready": True, "masterdata": self.masters.status(pinned)})


@dataclass
class Console:
    client: TestClient
    application: FastAPI
    store: Store
    runtime: GenerationRuntime
    world: MasterWorld
    masters: MasterDataStore
    data: Path
    captures: Path

    @property
    def switch(self) -> ConfigSwitch:
        return config_switch(self.application)

    @property
    def sets(self) -> ConfigSets:
        return ConfigSets(self.data)

    def in_use(self):
        return self.runtime.current.identity.master_snapshot

    def disk(self):
        pointer = self.masters.pointer()
        return None if pointer is None else pointer.snapshot

    def get(self, path, **options):
        return self.client.get(API_ROOT + path, headers=AUTH, **options)

    def post(self, path, body):
        return self.client.post(API_ROOT + path, json=body, headers=AUTH)


def build_console(tmp_path, *, state=None, installed=True):
    data, captures = tmp_path / "data", tmp_path / CAPTURES
    wall, clock = Wall(), Monotonic()
    masters = MasterDataStore.for_data_dir(data)
    masters.import_capture(write_capture(captures, "first", FIRST, masters.expected_hash))
    store = Store(data / DATABASE, player_state() if state is None else state, clock=wall)
    inputs = GenerationInputs(resolve(arguments(data)), data, tmp_path / "assets", tmp_path / "resources", "127.0.0.1",
                              GAME_PORT, StartupDiagnostics())
    world = MasterWorld(wall, masters)
    runtime = GenerationRuntime(world.build(inputs, lambda message: None), clock=clock, admit_timeout=SHORT,
                                drain_timeout=SHORT, draining_limit=SHORT, start=synchronous)
    routes = RouteRegistry()
    for module in ROUTE_MODULES:
        module.register(routes)
    catalog = AdminCatalog(CommandRegistry(DOMAINS), ViewRegistry(), routes,
                           tuple(module.__name__.rsplit(".", 1)[-1] for module in ROUTE_MODULES))
    application = create_admin_app(runtime, store, data_dir=data, admin_host="127.0.0.1", admin_port=PORT,
                                   game_port=GAME_PORT, token=TOKEN, preflight=ReadbackPreflight(),
                                   restart_check=InitializerRestartCheck(), plan_store=PlanStore(),
                                   preflight_domains=DOMAINS, catalog=catalog, ui_directory=tmp_path / "ui",
                                   clock=clock)
    if installed:
        install(application, inputs=inputs, builder=world.build)
    return Console(TestClient(application, base_url=BASE), application, store, runtime, world, masters, data, captures)


def staged_import(console, name, base):
    write_capture(console.captures, name, base, console.masters.expected_hash)
    response = console.post("/content/imports", {"capture": name})
    assert response.status_code == 200, response.text
    return response.json()["snapshot"]


def content_report(console, snapshot, *, changes=None, **body):
    response = console.post("/content/validations", {"changes": changes or {}, "master_snapshot": snapshot,
                                                     "op_id": operation(), **body})
    assert response.status_code == 200, response.text
    return report_for(console, response.json()["job"]["job_id"])


def applied(console, report):
    return switched(console, report, mode="apply", client_closed=True,
                    confirmations=[CONTENT_PHRASE.format(revision=report["revision"])])


def crash_on_activation(monkeypatch):
    def crash(self, *arguments, **options):
        raise Crash

    monkeypatch.setattr(MasterDataStore, "activate", crash)


def standalone_store(tmp_path):
    masters = MasterDataStore.for_data_dir(tmp_path / "data")
    captures = tmp_path / CAPTURES
    first = masters.import_capture(write_capture(captures, "first", FIRST, masters.expected_hash))["snapshot"]
    second = masters.import_capture(write_capture(captures, "second", SECOND, masters.expected_hash),
                                    activate=False)["snapshot"]
    return masters, captures, first, second


def test_staged_imports_keep_the_active_pointer_until_an_explicit_activation(tmp_path):
    masters = MasterDataStore.for_data_dir(tmp_path / "data")
    captures = tmp_path / CAPTURES
    first = masters.import_capture(write_capture(captures, "first", FIRST, masters.expected_hash))["snapshot"]
    before = masters.pointer()
    inspected = masters.inspect_capture(write_capture(captures, "second", SECOND, masters.expected_hash))
    assert (inspected["importable"], inspected["imported"]) == (True, False)
    second = masters.import_capture(captures / "second", activate=False)["snapshot"]
    assert inspected["snapshot"] == second != first and masters.inspect_capture(captures / "second")["imported"] is True
    assert masters.pointer() == before and masters.snapshot().identifier == first
    assert masters.snapshot(second).identifier == second and masters.status(second)["snapshot"] == second
    listed = {entry["snapshot"]: entry for entry in masters.list_snapshots()}
    assert set(listed) == {first, second} and listed[second]["valid"] is True
    assert listed[second]["tables"]["CardData"]["rows"] == 2 and listed[second]["imported_at"] is not None
    with pytest.raises(ProtocolError) as conflict:
        masters.activate(second, expected=None)
    assert conflict.value.code == "masterdata_pointer_conflict" and masters.pointer() == before
    moved = masters.activate(second, expected=before.digest)
    assert moved == masters.pointer() and moved.snapshot == second and masters.snapshot().identifier == second
    assert masters.import_capture(captures / "first", activate=False)["snapshot"] == first
    assert masters.pointer() == moved and masters.get_xml("CardData") == masters.snapshot(second).get_xml("CardData")


def test_unknown_or_damaged_snapshots_never_move_the_pointer(tmp_path):
    masters, captures, first, second = standalone_store(tmp_path)
    pointer = masters.pointer()
    for identifier in ("0" * 24, "../" + first):
        with pytest.raises(ProtocolError) as unknown:
            masters.activate(identifier, expected=pointer.digest)
        assert unknown.value.code == "masterdata_snapshot_unknown" and unknown.value.status == 404
    table = masters.directory / SNAPSHOTS_DIRECTORY / second / "QuestTable.xml"
    table.write_bytes(table.read_bytes() + b" ")
    with pytest.raises(ProtocolError) as damaged:
        masters.activate(second, expected=pointer.digest)
    assert damaged.value.code == "masterdata_invalid" and masters.pointer() == pointer
    assert masters.status(second)["code"] == "masterdata_invalid"
    (masters.directory / SNAPSHOTS_DIRECTORY / ("pending-" + "0" * 32)).mkdir()
    (masters.directory / SNAPSHOTS_DIRECTORY / ("f" * 24)).mkdir()
    # Listing reads manifests only; table digests are verified when a snapshot is read or activated.
    assert {entry["snapshot"]: entry["valid"] for entry in masters.list_snapshots()} == {
        first: True, second: True, "f" * 24: False}
    partial = masters.inspect_capture(write_capture(captures, "partial", THIRD, masters.expected_hash, complete=False))
    assert (partial["importable"], partial["snapshot"], partial["started_utc"]) == (False, None, None)
    assert set(partial) == set(masters.inspect_capture(captures / "second"))
    with pytest.raises(ValueError):
        masters.import_capture(captures / "partial", activate=False)


def test_pointer_writes_wait_briefly_for_the_lock_and_then_refuse(tmp_path, monkeypatch):
    masters, captures, first, second = standalone_store(tmp_path)
    monkeypatch.setattr(masterdata, "POINTER_LOCK_SECONDS", SHORT)
    capture = write_capture(captures, "third", THIRD, masters.expected_hash)
    with exclusive_file(masters.directory / POINTER_LOCK):
        with pytest.raises(ProtocolError) as busy:
            masters.activate(second, expected=masters.pointer_digest())
        with pytest.raises(ProtocolError) as importing:
            masters.import_capture(capture)
    assert busy.value.code == importing.value.code == "masterdata_pointer_busy"
    third = masters.inspect_capture(capture)
    assert third["imported"] is True and masters.pointer().snapshot == first
    assert masters.activate(second, expected=masters.pointer_digest()).snapshot == second
    assert masters.import_capture(capture)["snapshot"] == third["snapshot"] == masters.pointer().snapshot


def test_content_routes_list_snapshots_and_stage_imports_without_activating_them(tmp_path):
    console = build_console(tmp_path)
    running = console.in_use()
    view = console.get("/content").json()
    assert view["masterdata"]["in_use"] == running == view["masterdata"]["disk"]["snapshot"]
    assert view["masterdata"]["staged"] is None and view["side_by_side"]["in_use"]["snapshot"] == running
    write_capture(console.captures, "second", SECOND, console.masters.expected_hash)
    write_capture(console.captures, "partial", THIRD, console.masters.expected_hash, complete=False)
    captures = console.get("/content/captures").json()
    listing = {item["name"]: item for item in captures["items"]}
    assert Path(captures["root"]) == console.data.resolve().parent / CAPTURES
    assert set(listing) == {"first", "second", "partial"}
    assert (listing["first"]["imported"], listing["second"]["imported"], listing["partial"]["importable"]) == (
        True, False, False)
    response = console.post("/content/imports", {"capture": "second"})
    assert response.status_code == 200, response.text
    imported = response.json()
    second = imported["snapshot"]
    assert second == listing["second"]["snapshot"]
    assert (imported["activated"], imported["already_present"]) == (False, False)
    assert imported["masterdata"]["in_use"] == running and console.disk() == running == console.in_use()
    view = console.get("/content").json()
    marks = {item["snapshot"]: (item["in_use"], item["disk"], item["next_start"])
             for item in view["snapshots"]["items"]}
    assert marks == {running: (True, True, True), second: (False, False, False)}
    assert view["snapshots"]["total"] == 2 and view["side_by_side"]["disk"]["snapshot"] == running
    assert console.post("/content/imports", {"capture": "second"}).json()["already_present"] is True
    for body, status, code in (({"capture": "missing"}, 404, "content_capture_unknown"),
                               ({"capture": "../data"}, 422, "admin_params_invalid"),
                               ({"capture": "partial"}, 422, "content_capture_invalid")):
        refused = console.post("/content/imports", body)
        assert refused.status_code == status and error_of(refused)["code"] == code
    assert sorted((item["kind"], item["error_code"]) for item in failures(console)) == [
        ("content", "admin_params_invalid"), ("content", "content_capture_invalid"),
        ("content", "content_capture_unknown")]
    assert history(console) == [] and console.store.profile().revision == 0


def test_master_activation_stages_by_default_and_blocks_other_switches_until_undone(tmp_path):
    console = build_console(tmp_path)
    running = console.in_use()
    second = staged_import(console, "second", SECOND)
    report = content_report(console, second)
    assert report["tier"] == "content" and report["default_mode"] == "stage"
    assert report["reasons"]["identity"] == ["master_snapshot"]
    assert report["generations"]["candidate"]["identity"]["master_snapshot"] == second
    master = report["masterdata"]
    assert (master["requested"], master["target"], master["staged"]) == (second, second, None)
    assert master["activation"] == {"target": second, "before": running,
                                    "pointer_digest": console.masters.pointer_digest()}
    assert report["modes"]["stage"]["allowed"] is True and report["validation"]["master_snapshot"] == second
    assert report["checks"]["master_pin"] == {"ok": True}
    staged = switched(console, report)
    assert (staged["mode"], staged["tier"], staged["master_snapshot"]) == ("stage", "content", second)
    assert staged["masterdata"]["published"] is True and staged["masterdata"]["staged"] == second
    assert console.in_use() == running and console.disk() == second
    assert history(console) == [(1, EMPTY_SET, "staged")] and console.store.list_backups().items == ()
    [record] = audit(console)
    assert record["targets"] == ["masterdata"] and record["result"]["master_activation"]["target"] == second
    view = console.get("/config").json()["masterdata"]
    assert (view["in_use"], view["disk"]["snapshot"], view["staged"]) == (running, second, second)
    assert console.get("/content").json()["side_by_side"]["disk"]["snapshot"] == second
    hot = validated(console, GOLD)
    assert hot["masterdata"]["activation"] is None and console.world.built[-1].master_snapshot == running
    [blocker] = hot["modes"]["apply"]["blockers"]
    assert blocker["code"] == "config_staged_pending" and blocker["details"] == {"staged_master": second}
    refused = console.post("/config/switches", switch_body(hot))
    assert refused.status_code == 409 and error_of(refused)["details"] == {"staged_master": second}
    wrong = console.post("/content/unstage", {"staged_snapshot": running, "op_id": operation()})
    assert wrong.status_code == 409 and error_of(wrong)["code"] == "masterdata_staged_changed"
    op_id = operation()
    response = console.post("/content/unstage", {"staged_snapshot": second, "op_id": op_id})
    assert response.status_code == 200, response.text
    undone = response.json()
    assert (undone["staged_master"], undone["config_set"], undone["master_activation"]["target"]) == (
        second, EMPTY_SET, running)
    assert undone["masterdata"]["published"] is True and console.disk() == running
    assert history(console)[-1] == (2, EMPTY_SET, "unstaged")
    assert console.post("/content/unstage", {"staged_snapshot": second, "op_id": op_id}).json()["replayed"] is True
    nothing = console.post("/content/unstage", {"staged_snapshot": second, "op_id": operation()})
    assert nothing.status_code == 409 and error_of(nothing)["code"] == "masterdata_not_staged"
    fresh = refreshed(console, hot)
    assert fresh["modes"]["apply"]["allowed"] is True and switched(console, fresh)["tier"] == "hot"
    assert console.disk() == running == console.in_use()


def test_running_master_activation_needs_a_closed_client_and_moves_the_pointer_with_the_swap(tmp_path):
    console = build_console(tmp_path)
    running = console.in_use()
    second = staged_import(console, "second", SECOND)
    report = content_report(console, second)
    unconfirmed = console.post("/config/switches", switch_body(report, mode="apply"))
    assert unconfirmed.status_code == 422 and error_of(unconfirmed)["code"] == "admin_client_closed_required"
    assert console.in_use() == running == console.disk()
    assert report["requirements"] == {"backup": False, "fence": True, "client_closed": True}
    result = applied(console, report)
    assert (result["mode"], result["tier"], result["backup_before_switch"]) == ("apply", "content", None)
    assert console.store.list_backups().items == () and console.store.profile().revision == 0
    assert console.in_use() == second == console.disk() and result["generation_after"] == console.runtime.current.id
    assert result["masterdata"]["published"] is True and result["masterdata"]["staged"] is None
    assert history(console) == [(1, EMPTY_SET, "content")]
    assert console.get("/config").json()["client_warning"]["client_actions"]["restart_client"] is True
    follow = validated(console, GOLD)
    assert follow["tier"] == "hot" and console.world.built[-1].master_snapshot == second


def test_configuration_changes_and_a_master_snapshot_switch_in_one_operation(tmp_path):
    console = build_console(tmp_path)
    second = staged_import(console, "second", SECOND)
    report = content_report(console, second, changes=GOLD)
    set_id = report["validation"]["config_set"]["id"]
    assert report["tier"] == "content" and [change["key"] for change in report["keys"]] == ["quest-rules"]
    applied(console, report)
    assert history(console) == [(1, set_id, "content")] and console.sets.read_active().set_id == set_id
    assert console.disk() == second == console.in_use()
    [record] = audit(console)
    assert record["targets"] == ["quest-rules", "masterdata"] and record["result"]["master_snapshot"] == second


def test_undoing_only_the_master_keeps_a_staged_configuration(tmp_path):
    console = build_console(tmp_path)
    running = console.in_use()
    second = staged_import(console, "second", SECOND)
    report = content_report(console, second, changes=GOLD)
    set_id = report["validation"]["config_set"]["id"]
    switched(console, report)
    view = console.get("/config").json()
    assert view["staged"]["set"] == set_id and view["masterdata"]["staged"] == second
    kept = console.post("/content/unstage", {"staged_snapshot": second, "op_id": operation()}).json()
    assert kept["config_set"] == set_id and kept["keys"] == [] and console.disk() == running
    view = console.get("/config").json()
    assert view["staged"]["set"] == set_id and view["masterdata"]["staged"] is None
    assert console.sets.read_active().set_id == set_id
    undone = console.post("/config/unstage", {"staged_set": set_id, "op_id": operation()}).json()
    assert undone["config_set"] == EMPTY_SET and undone["master_activation"] is None
    assert [row[2] for row in history(console)] == ["staged", "unstaged", "unstaged"]


def test_one_unstage_undoes_a_staged_configuration_and_master_together(tmp_path):
    console = build_console(tmp_path)
    running = console.in_use()
    second = staged_import(console, "second", SECOND)
    report = content_report(console, second, changes=GOLD)
    set_id = report["validation"]["config_set"]["id"]
    switched(console, report)
    response = console.post("/content/unstage", {"staged_snapshot": second, "staged_set": set_id, "op_id": operation()})
    assert response.status_code == 200, response.text
    undone = response.json()
    assert (undone["config_set"], undone["keys"]) == (EMPTY_SET, ["quest-rules"])
    assert console.sets.read_active().set_id == EMPTY_SET and console.disk() == running
    view = console.get("/config").json()
    assert view["staged"] is None and view["masterdata"]["staged"] is None
    assert history(console) == [(1, set_id, "staged"), (2, EMPTY_SET, "unstaged")]
    assert audit(console)[0]["targets"] == ["quest-rules", "masterdata"]


def test_roll_forward_publishes_a_committed_master_activation_left_behind(tmp_path, monkeypatch):
    console = build_console(tmp_path)
    running = console.in_use()
    second = staged_import(console, "second", SECOND)
    report = content_report(console, second)
    with monkeypatch.context() as patch:
        crash_on_activation(patch)
        with pytest.raises(Crash):
            console.switch.switch(report_id=report["report_id"], op_id=operation())
    assert history(console) == [(1, EMPTY_SET, "staged")] and console.disk() == running
    view = console.get("/content").json()["masterdata"]
    assert (view["roll_forward_pending"], view["next_start"], view["staged"]) == (True, second, second)
    outcome = roll_forward(console.store, console.data)
    assert outcome["masterdata"]["rolled_forward"] is True and outcome["masterdata"]["pointer"]["snapshot"] == second
    assert console.disk() == second
    assert roll_forward(console.store, console.data)["masterdata"]["rolled_forward"] is False


def test_roll_forward_leaves_a_master_pointer_moved_after_the_commit(tmp_path, monkeypatch):
    console = build_console(tmp_path)
    second = staged_import(console, "second", SECOND)
    report = content_report(console, second)
    with monkeypatch.context() as patch:
        crash_on_activation(patch)
        with pytest.raises(Crash):
            console.switch.switch(report_id=report["report_id"], op_id=operation())
    capture = write_capture(console.captures, "third", THIRD, console.masters.expected_hash)
    third = console.masters.import_capture(capture)["snapshot"]
    outcome = roll_forward(console.store, console.data)
    assert outcome["masterdata"]["rolled_forward"] is False and console.disk() == third
    assert console.get("/content").json()["masterdata"]["staged"] == third


def test_the_next_switch_republishes_a_master_pointer_left_unpublished(tmp_path, monkeypatch):
    console = build_console(tmp_path)
    running = console.in_use()
    second = staged_import(console, "second", SECOND)
    report = content_report(console, second)
    monkeypatch.setattr(masterdata, "POINTER_LOCK_SECONDS", SHORT)
    with exclusive_file(console.masters.directory / POINTER_LOCK):
        result = applied(console, report)
    assert result["masterdata"]["published"] is False
    assert result["masterdata"]["publish_error"]["code"] == "masterdata_pointer_busy"
    assert console.in_use() == second and console.disk() == running
    view = console.get("/config").json()["masterdata"]
    assert (view["roll_forward_pending"], view["next_start"], view["staged"]) == (True, second, None)
    follow = validated(console, GOLD)
    assert follow["masterdata"]["activation"]["target"] == second and follow["modes"]["apply"]["allowed"] is True
    republished = switched(console, follow)
    assert republished["masterdata"]["published"] is True and republished["master_activation"]["target"] == second
    assert console.disk() == second and audit(console)[0]["targets"] == ["quest-rules", "masterdata"]


def test_builders_that_ignore_the_pinned_snapshot_cannot_switch(tmp_path):
    console = build_console(tmp_path)
    running = console.in_use()
    second = staged_import(console, "second", SECOND)
    console.world.honors_pin = False
    report = content_report(console, second)
    error = report["checks"]["master_pin"]["error"]
    assert error["code"] == "config_candidate_master_mismatch"
    assert error["details"] == {"pinned": second, "built": running}
    assert not any(mode["allowed"] for mode in report["modes"].values())
    refused = console.post("/config/switches", switch_body(report, mode="stage"))
    assert refused.status_code == 409 and error_of(refused)["code"] == "config_candidate_master_mismatch"
    assert history(console) == [] and console.disk() == running


def test_switches_refuse_reports_once_the_master_pointer_moved(tmp_path):
    console = build_console(tmp_path)
    second = staged_import(console, "second", SECOND)
    report = content_report(console, second)
    capture = write_capture(console.captures, "third", THIRD, console.masters.expected_hash)
    third = console.masters.import_capture(capture)["snapshot"]
    moved = console.post("/config/switches", switch_body(report))
    assert moved.status_code == 409 and error_of(moved)["code"] == "config_report_stale"
    assert error_of(moved)["details"] == {"changed": ["masterdata"]} and history(console) == []
    fresh = refreshed(console, report)
    assert fresh["report_id"] != report["report_id"] and fresh["masterdata"]["disk"]["snapshot"] == third
    [blocker] = fresh["modes"]["stage"]["blockers"]
    assert blocker["code"] == "config_staged_pending" and blocker["details"] == {"staged_master": third}


def test_running_master_switches_recheck_the_pointer_inside_the_fence(tmp_path):
    console = build_console(tmp_path)
    running = console.in_use()
    second = staged_import(console, "second", SECOND)
    report = content_report(console, second)
    capture = write_capture(console.captures, "third", THIRD, console.masters.expected_hash)
    fence = console.runtime.fence

    def moving():
        console.masters.import_capture(capture)
        return fence()

    console.runtime.fence = moving
    confirmations = [CONTENT_PHRASE.format(revision=report["revision"])]
    moved = console.post("/config/switches", switch_body(report, mode="apply", client_closed=True,
                                                         confirmations=confirmations))
    assert moved.status_code == 409 and error_of(moved)["code"] == "config_report_stale"
    assert error_of(moved)["details"] == {"changed": ["masterdata"]}
    assert history(console) == [] and console.in_use() == running and console.disk() not in (running, second)
    assert console.runtime.status()["fenced"] is False and console.store.list_backups().items == ()


def test_switches_verify_the_target_snapshot_before_recording_anything(tmp_path):
    console = build_console(tmp_path)
    running = console.in_use()
    second = staged_import(console, "second", SECOND)
    report = content_report(console, second)
    table = console.masters.directory / SNAPSHOTS_DIRECTORY / second / "CardData.xml"
    table.write_bytes(table.read_bytes() + b" ")
    damaged = console.post("/config/switches", switch_body(report))
    assert damaged.status_code == 503 and error_of(damaged)["code"] == "masterdata_invalid"
    assert history(console) == [] and console.disk() == running


def test_master_activations_wait_for_a_readable_pointer(tmp_path):
    console = build_console(tmp_path)
    second = staged_import(console, "second", SECOND)
    (console.masters.directory / ACTIVE_POINTER).write_bytes(b" " * (MAX_POINTER_BYTES + 1))
    report = content_report(console, second)
    assert report["masterdata"]["activation"] == {"target": second, "before": None, "pointer_digest": None}
    for mode in report["modes"].values():
        assert "masterdata_invalid" in [item["code"] for item in mode["blockers"]] and mode["allowed"] is False
    refused = console.post("/config/switches", switch_body(report))
    assert refused.status_code == 503 and error_of(refused)["code"] == "masterdata_invalid"
    assert history(console) == []


def test_master_pointer_failures_after_the_commit_are_reported(tmp_path, monkeypatch):
    console = build_console(tmp_path)
    running = console.in_use()
    second = staged_import(console, "second", SECOND)
    report = content_report(console, second)

    def linked(self, *arguments, **options):
        raise ValueError("Links and junctions are not accepted for local data")

    monkeypatch.setattr(MasterDataStore, "activate", linked)
    result = applied(console, report)
    assert result["masterdata"]["published"] is False
    assert result["masterdata"]["publish_error"]["code"] == INTERNAL_ERROR
    assert console.in_use() == second and console.disk() == running and history(console) == [(1, EMPTY_SET, "content")]


def test_deeply_nested_pointers_and_capture_manifests_read_as_invalid(tmp_path):
    console = build_console(tmp_path)
    running = console.in_use()
    nested = write_capture(console.captures, "nested", SECOND, console.masters.expected_hash)
    (nested / "manifest.json").write_bytes(b"[" * DEEP_NESTING)
    captures = console.get("/content/captures")
    assert captures.status_code == 200, captures.text
    [listed] = [item for item in captures.json()["items"] if item["name"] == "nested"]
    assert (listed["importable"], listed["snapshot"], listed["started_utc"]) == (False, None, None)
    refused = console.post("/content/imports", {"capture": "nested"})
    assert refused.status_code == 422 and error_of(refused)["code"] == "content_capture_invalid"
    (console.masters.directory / ACTIVE_POINTER).write_bytes(b"[" * MAX_POINTER_BYTES)
    assert console.masters.status()["code"] == "masterdata_invalid"
    view = console.get("/content")
    assert view.status_code == 200, view.text
    master = view.json()["masterdata"]
    assert (master["in_use"], master["disk"], master["error"]["code"]) == (running, None, "masterdata_invalid")


def test_capture_imports_that_cannot_be_stored_fail_as_unavailable(tmp_path, monkeypatch):
    console = build_console(tmp_path)
    write_capture(console.captures, "second", SECOND, console.masters.expected_hash)

    def unwritable(self, *arguments, **options):
        raise PermissionError("The data directory refused the snapshot")

    monkeypatch.setattr(MasterDataStore, "import_capture", unwritable)
    refused = console.post("/content/imports", {"capture": "second"})
    assert refused.status_code == 503 and error_of(refused)["code"] == "content_import_failed"
    assert [(item["kind"], item["error_code"]) for item in failures(console)] == [("content", "content_import_failed")]


def test_running_master_switches_wait_for_battles_but_staging_does_not(tmp_path):
    console = build_console(tmp_path, state=player_state(quest_state=battle_journal()))
    second = staged_import(console, "second", SECOND)
    report = content_report(console, second)
    assert [item["code"] for item in report["modes"]["apply"]["blockers"]] == ["admin_battle_abort_required"]
    assert switched(console, report)["mode"] == "stage" and console.disk() == second


@pytest.mark.parametrize("body, status, code", [
    ({"master_snapshot": "0" * 24}, 404, "masterdata_snapshot_unknown"),
    ({"master_snapshot": "XYZ"}, 422, "admin_params_invalid"),
    ({"master_snapshot": "0" * 24, "unexpected": 1}, 422, "admin_params_invalid"),
    ({}, 422, "admin_params_invalid"),
], ids=("unknown", "malformed", "extra", "missing"))
def test_master_validations_are_strict(tmp_path, body, status, code):
    console = build_console(tmp_path)
    response = console.post("/content/validations", {"op_id": operation(), **body})
    assert response.status_code == status and error_of(response)["code"] == code
    assert len(console.world.built) == 1 and console.get("/config").json()["validation"] is None


def test_content_switching_routes_need_the_installed_switch(tmp_path):
    console = build_console(tmp_path, installed=False)
    assert console.get("/content").status_code == 200
    for response in (console.post("/content/validations", {"master_snapshot": "0" * 24, "op_id": operation()}),
                     console.post("/content/unstage", {"staged_snapshot": "0" * 24, "op_id": operation()})):
        assert response.status_code == 503 and error_of(response)["code"] == "config_switch_unavailable"
    empty = build_console(tmp_path / "installed")
    refused = empty.post("/content/unstage", {"staged_snapshot": "0" * 24, "op_id": operation()})
    assert refused.status_code == 409 and error_of(refused)["code"] == "masterdata_not_staged"


def test_content_inputs_carry_the_pinned_master_snapshot(tmp_path):
    config = resolve(arguments(tmp_path))
    inputs = GenerationInputs(config, tmp_path, tmp_path / "assets", tmp_path / "resources", "127.0.0.1", GAME_PORT,
                              StartupDiagnostics())
    pinned = ContentInputs.pinned(inputs, config=config, master_snapshot="a" * 24)
    assert isinstance(pinned, GenerationInputs) and pinned.master_snapshot == "a" * 24
    assert pinned.describe() == {**inputs.describe(), "master_snapshot": "a" * 24}
    assert replace(pinned, port=GAME_PORT + 1).master_snapshot == "a" * 24
    assert ContentInputs.pinned(pinned, config=config, master_snapshot=None).master_snapshot is None
    with pytest.raises(ValueError):
        ContentInputs.pinned(inputs, config=config, master_snapshot="")
