import hashlib
import json
from pathlib import Path

import pytest

from client import capture_responses as capture
from aigis_ps.alxml import Column, Container, dumps
from test_master_capture import FakeClock, FakeDevice


BASE = 0x600000
SERVANT_VTABLE = 0xA6A2A8
UNIT_LOAD = next(reactor for reactor in capture.LOAD_REACTORS if reactor.name == "CReactorPlayerUnitLoad")


def ready():
    return {"event": "ready", "base": hex(BASE), "pointerSize": 4,
            "consumers": [hex(BASE + rva) for rva in capture.CONSUMER_RVAS]}


def body(name="PPU", extra=()):
    return dumps(Container(children=(Container(name, (Column("CardID", "I", (17,)),)), *extra)))


def message(operation, data, sequence=1, *, consumer=capture.DEFAULT_CONSUMER_RVA, vtable=SERVANT_VTABLE):
    return {"event": "response", "operation": operation, "sequence": sequence, "length": len(data),
            "originalLength": len(data) + 100, "functionAddress": hex(BASE + consumer),
            "returnAddress": hex(BASE + capture.RETURN_RVA), "vtable": hex(BASE + vtable),
            "removed": [], "removedRows": 0}


def unit_message(data, sequence=1):
    return message(41, data, sequence, consumer=UNIT_LOAD.consumer_rva, vtable=UNIT_LOAD.vtable_rva)


def event(payload, data=None):
    return {"type": "send", "payload": payload}, data


def manifest_of(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@pytest.fixture
def identity(tmp_path):
    executable = tmp_path / "Aigis_r.exe"
    executable.write_bytes(b"fixture")
    return {"path": str(executable), "sha256": capture.masters.EXPECTED_SHA256}


def make_writer(tmp_path, identity, selection):
    result = capture.ResponseWriter(tmp_path / "captures", identity, 42, selection, workspace=tmp_path)
    result.ready(ready())
    return result


@pytest.fixture
def writer(tmp_path, identity):
    return make_writer(tmp_path, identity, capture.Selection("operations", (12, 41)))


def test_selection_completes_once_every_operation_is_saved(writer):
    first = writer.record(message(12, body("SMN")), body("SMN"))
    assert not writer.complete and writer.missing == [41]
    second = writer.record(unit_message(body(), 2), body())
    assert writer.complete and second["reactor"] == "CReactorPlayerUnitLoad"
    manifest = manifest_of(writer.finish("selection_complete"))
    assert manifest["status"] == "complete" and manifest["raw_response_saved"] is False
    assert manifest["captured"] == {"12": 1, "41": 1} and manifest["missing_operations"] == []
    lines = [json.loads(line) for line in (writer.directory / "records.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [line["path"] for line in lines] == [first["path"], second["path"]] == ["00001-12.xml", "00002-41.xml"]
    assert (writer.directory / first["path"]).read_bytes() == body("SMN")


def test_all_mode_never_completes_by_itself_and_stops_cleanly(tmp_path, identity):
    writer = make_writer(tmp_path, identity, capture.Selection("all"))
    writer.record(message(115, body("LBN")), body("LBN"))
    writer.exclude({"event": "excluded", "operation": 152})
    assert not writer.complete
    manifest = manifest_of(writer.finish("timeout"))
    assert manifest["status"] == "stopped" and manifest["end_reason"] == "timeout"
    assert manifest["excluded_seen"] == {"152": 1} and manifest["captured"] == {"115": 1}


@pytest.mark.parametrize("patch", [{"operation": True}, {"operation": -1}, {"sequence": 0}, {"sequence": True},
    {"length": 1}, {"originalLength": 0}, {"originalLength": 2 ** 30}, {"functionAddress": "0x1"},
    {"returnAddress": hex(BASE + 0x68060B)}, {"vtable": hex(BASE + UNIT_LOAD.vtable_rva)},
    {"vtable": hex(BASE + capture.EXCLUDED_REACTORS[0].vtable_rva)}, {"removed": "uk"}, {"removed": [""]},
    {"removedRows": -1}, {"operation": 13}, {"operation": 152}])
def test_bad_agent_metadata_never_writes_a_response(writer, patch):
    data = body()
    payload = {**message(12, data), **patch}
    with pytest.raises(capture.masters.CaptureError):
        writer.record(payload, data)
    assert writer.records == 0 and list(writer.directory.glob("*.xml")) == []


def test_load_consumer_requires_its_own_vtable(writer):
    data = body()
    with pytest.raises(capture.masters.CaptureError, match="response_address_mismatch"):
        writer.record(message(41, data, consumer=UNIT_LOAD.consumer_rva), data)


@pytest.mark.parametrize("leak", [
    Container("SYSTEM", (Column("A1", "S", ("ExchangeKey",)), Column("A2", "S", ("private-method",)))),
    Container("LGI", tuple(Column(f"A{index}", "S", (f"private-{index}",)) for index in range(1, 11))),
    Container("PGP2", (Column("A1", "S", ("UK",)), Column("A2", "S", ("private-uk",)))),
    Container("Rows", (Column("MethodKey", "S", ("private-nested",)),)),
])
def test_unfiltered_agent_output_is_rejected_without_saving_or_logging_it(writer, leak):
    data = body(extra=(leak,))
    assert writer.record(message(12, data), data) is None
    assert writer.records == 0 and list(writer.directory.glob("*.xml")) == []
    state = (writer.directory / "state.json").read_text(encoding="utf-8")
    assert json.loads(state)["recent_rejections"][-1]["code"] == "response_agent_filter_incomplete"
    assert "private-" not in state
    later = body("SMN")
    assert writer.record(message(12, later, 2), later)["sequence"] == 2


def test_structurally_valid_response_outside_strict_alxml_is_kept_and_flagged(writer):
    data = b'<DA><Count T="I"><V></V></Count></DA>'
    entry = writer.record(message(12, data), data)
    assert entry["alxml_loadable"] is False and (writer.directory / entry["path"]).read_bytes() == data


def test_capacity_limit_ends_the_capture_instead_of_dropping_responses(writer, monkeypatch):
    monkeypatch.setattr(capture, "MAX_RECORDS", 1)
    writer.record(message(12, body()), body())
    with pytest.raises(capture.masters.CaptureError, match="response_capture_capacity"):
        writer.record(message(12, body(), 2), body())
    assert writer.records == 1


@pytest.mark.parametrize("payload", [{"event": "rejected", "operation": 12, "code": "private-text"},
                                     {"event": "rejected", "operation": "12", "code": "invalid_length"}])
def test_unknown_rejections_are_refused(writer, payload):
    with pytest.raises(capture.masters.CaptureError):
        writer.reject(payload)


@pytest.mark.parametrize("patch", [{"pointerSize": 8}, {"base": "0x0"}, {"consumers": []},
                                   {"consumers": [hex(BASE + rva) for rva in reversed(capture.CONSUMER_RVAS)]}])
def test_ready_must_report_every_consumer_at_the_verified_base(tmp_path, identity, patch):
    writer = capture.ResponseWriter(tmp_path / "captures", identity, 42, capture.Selection("all"), workspace=tmp_path)
    with pytest.raises(capture.masters.CaptureError):
        writer.ready({**ready(), **patch})


@pytest.fixture
def configured(identity, monkeypatch):
    monkeypatch.setattr(capture, "profile", lambda *_: (identity, {"fixture": True}))
    monkeypatch.setattr(capture, "agent_source", lambda _: "fixture")
    monkeypatch.setattr(capture.masters, "verify_process", lambda *_: identity)
    return Path(identity["path"])


def run(configured, tmp_path, events, selection, **options):
    device = FakeDevice(events)
    progress = []
    path = capture.capture_process(device, 42, configured, tmp_path / "captures", options.pop("timeout", 1), selection,
                                   progress=progress.append, workspace=tmp_path, **options)
    return device, progress, path


def test_selection_capture_detaches_after_the_last_operation(configured, tmp_path):
    data = body()
    device, progress, path = run(configured, tmp_path, [event(ready()), event(message(12, data), data),
        event({"event": "excluded", "operation": 152}), event(unit_message(data, 2), data)],
        capture.Selection("operations", (12, 41)))
    assert manifest_of(path)["status"] == "complete"
    assert device.session.detach_count == 1 and device.attached == [42]
    assert [row["event"] for row in progress] == ["ready", "captured", "excluded", "captured", "finished"]


def test_all_mode_ends_normally_at_the_deadline(configured, tmp_path):
    data = body()
    device, progress, path = run(configured, tmp_path, [event(ready()), event(message(115, data), data)],
        capture.Selection("all"), clock=FakeClock(step=1))
    manifest = manifest_of(path)
    assert manifest["status"] == "stopped" and manifest["end_reason"] == "timeout" and manifest["records"] == 1
    assert device.session.detach_count == 1


def test_all_mode_treats_the_game_closing_as_a_normal_end(configured, tmp_path):
    data = body()
    _, progress, path = run(configured, tmp_path, [event(ready()), event(message(115, data), data),
        ({"type": "detached"}, None)], capture.Selection("all"))
    assert manifest_of(path)["end_reason"] == "process_exited" and progress[-1]["status"] == "stopped"


def test_selection_mode_keeps_partial_data_when_the_game_closes(configured, tmp_path):
    data = body()
    progress = []
    with pytest.raises(capture.masters.CaptureError, match="response_process_detached"):
        capture.capture_process(FakeDevice([event(ready()), event(message(12, data), data), ({"type": "detached"}, None)]),
            42, configured, tmp_path / "captures", 1, capture.Selection("operations", (12, 41)),
            progress=progress.append, workspace=tmp_path)
    manifest = manifest_of(progress[-1]["manifest"])
    assert manifest["status"] == "partial" and manifest["missing_operations"] == [41]


def test_selection_timeout_reports_missing_operations(configured, tmp_path):
    progress = []
    with pytest.raises(capture.masters.CaptureError, match="response_capture_timeout"):
        capture.capture_process(FakeDevice([event(ready())]), 42, configured, tmp_path / "captures", 1,
            capture.Selection("preset", (46,), "shop"), progress=progress.append, workspace=tmp_path, clock=FakeClock(step=1))
    manifest = manifest_of(progress[-1]["manifest"])
    assert manifest["status"] == "failed" and manifest["missing_operations"] == [46]


def test_interrupt_writes_a_manifest_before_propagating(configured, tmp_path):
    data = body()
    progress = []

    def interrupt(update):
        progress.append(update)
        if update["event"] == "captured":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        capture.capture_process(FakeDevice([event(ready()), event(message(115, data), data)]), 42, configured,
            tmp_path / "captures", 1, capture.Selection("all"), progress=interrupt, workspace=tmp_path)
    manifest = manifest_of(progress[-1]["manifest"])
    assert manifest["status"] == "stopped" and manifest["end_reason"] == "interrupted"


def test_queue_overflow_is_reported_instead_of_dropping_messages(configured, tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "EVENT_QUEUE_SIZE", 2)
    progress = []
    with pytest.raises(capture.masters.CaptureError, match="response_queue_overflow"):
        capture.capture_process(FakeDevice([event(ready())] * 5), 42, configured, tmp_path / "captures", 1,
            capture.Selection("all"), progress=progress.append, workspace=tmp_path, clock=FakeClock(step=2))
    assert manifest_of(progress[-1]["manifest"])["error_code"] == "response_queue_overflow"


def test_agent_error_text_is_never_logged_or_persisted(configured, tmp_path):
    progress = []
    with pytest.raises(capture.masters.CaptureError, match="response_agent_error"):
        capture.capture_process(FakeDevice([({"type": "error", "description": "private-token-error"}, None)]), 42,
            configured, tmp_path / "captures", 1, capture.Selection("all"), progress=progress.append, workspace=tmp_path)
    assert "private-token" not in json.dumps(progress)
    assert "private-token" not in Path(progress[-1]["manifest"]).read_text(encoding="utf-8")


def test_timeout_above_four_hours_is_refused(configured, tmp_path):
    with pytest.raises(capture.masters.CaptureError, match="invalid_response_timeout"):
        capture.capture_process(FakeDevice(), 42, configured, tmp_path, capture.MAX_TIMEOUT + 1, capture.Selection("all"))


def test_real_profile_verifies_every_consumer_and_exclusion_on_disk():
    if not capture.masters.DEFAULT_EXE.is_file():
        pytest.skip("Original client is unavailable")
    identity, config = capture.profile(capture.masters.DEFAULT_EXE, capture.Selection("all"))
    assert identity["sha256"] == hashlib.sha256(capture.masters.DEFAULT_EXE.read_bytes()).hexdigest()
    assert identity["sha256"] in capture.masters.SUPPORTED_SHA256
    assert [item["rva"] for item in config["consumers"]] == list(capture.CONSUMER_RVAS)
    assert len(config["vtables"]) == len(capture.LOAD_REACTORS) + len(capture.EXCLUDED_REACTORS)
    assert config["operations"] is None and 152 in config["excludedOperations"]


def test_check_does_not_import_frida_or_create_output(configured, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(capture.masters, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(capture.importlib, "import_module", lambda _: pytest.fail("Check must not load Frida"))
    output = tmp_path / "uncreated"
    assert capture.main(["--check", "--preset", "summon", "--exe", str(configured), "--output", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "checked" and result["selection"]["operations"] == [12, 13, 35, 108]
    assert not output.exists()


@pytest.mark.parametrize("argv", [["--wait"], ["--wait", "--operations", "152"], ["--wait", "--all", "--preset", "shop"],
                                  ["--wait", "--all", "--timeout", str(capture.MAX_TIMEOUT + 1)]])
def test_capture_modes_require_one_safe_selection(argv):
    with pytest.raises(SystemExit) as caught:
        capture.main(argv)
    assert caught.value.code == 2
