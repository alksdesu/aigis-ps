import hashlib
import json
from pathlib import Path

import pytest

from client import capture_schedule as capture
from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.schedule_capture import ScheduleCaptureError
from test_master_capture import FakeClock, FakeDevice


BASE = 0x600000


def ready():
    return {"event": "ready", "base": hex(BASE), "functionAddress": hex(BASE + capture.FUNCTION_RVA), "pointerSize": 4}


def body(operation, extra=()):
    return dumps(Container(children=(Container("CSS" if operation == 70 else "CSP", (
        Column("LineupID" if operation == 70 else "ConfigID", "I", (17,)),)), *extra)))


def message(operation, data, sequence=1):
    return {"event": "response", "operation": operation, "sequence": sequence, "length": len(data),
        "originalLength": len(data) + 100, "functionAddress": hex(BASE + capture.FUNCTION_RVA),
        "returnAddress": hex(BASE + capture.RETURN_RVA), "vtable": hex(BASE + capture.VTABLES[operation])}


def event(payload, data=None):
    return {"type": "send", "payload": payload}, data


@pytest.fixture
def identity(tmp_path):
    executable = tmp_path / "Aigis_r.exe"
    executable.write_bytes(b"fixture")
    return {"path": str(executable), "sha256": capture.masters.EXPECTED_SHA256}


@pytest.fixture
def writer(tmp_path, identity):
    result = capture.ScheduleWriter(tmp_path / "captures", identity, 42, workspace=tmp_path)
    result.ready(ready())
    return result


def add(writer, operation, sequence, extra=()):
    data = body(operation, extra)
    return writer.record(message(operation, data, sequence), data)


def test_one_complete_round_is_saved_with_only_sanitized_data(writer):
    one = add(writer, 70, 1)
    two = add(writer, 71, 2)
    assert writer.complete
    manifest = json.loads(writer.finish().read_text(encoding="utf-8"))
    assert manifest["complete"] and manifest["raw_response_saved"] is False
    assert manifest["selected_round"] == {"70": one["path"], "71": two["path"]}
    assert manifest["missing_operations"] == []
    assert manifest["records"][0]["missing_groups"] == ["FPR", "SH", "GacyaStatus", "LCC", "PLC"]
    assert "SYSTEM" not in (writer.directory / one["path"]).read_text(encoding="utf-8")


def test_csp_before_css_cannot_complete_and_new_css_resets_the_pair(writer):
    add(writer, 71, 1)
    assert not writer.complete and writer.round == {}
    add(writer, 70, 2, (Container("FPR", (Column("Count", "I", (1,)),)),))
    add(writer, 71, 3)
    assert writer.complete
    latest = add(writer, 70, 4)
    assert not writer.complete and set(writer.round) == {70}
    add(writer, 71, 5)
    assert writer.complete
    assert writer.round[70] == latest["path"]
    assert "FPR" in latest["missing_groups"]


def test_rejected_new_css_cannot_pair_old_css_with_new_csp(writer):
    add(writer, 70, 1)
    writer.reject({"operation": 70, "code": "invalid_schedule_payload"})
    add(writer, 71, 2)
    assert not writer.complete and writer.round == {}
    latest = add(writer, 70, 3)
    add(writer, 71, 4)
    assert writer.complete and writer.round[70] == latest["path"]


@pytest.mark.parametrize("patch", [{"operation": 69}, {"operation": True}, {"sequence": 0},
    {"length": 1}, {"length": True}, {"originalLength": -1}, {"originalLength": 2 ** 30},
    {"functionAddress": "0x1"}, {"returnAddress": "0x1"}, {"vtable": hex(BASE + capture.VTABLES[71])}])
def test_bad_agent_metadata_never_writes_a_table(writer, patch):
    data = body(70)
    payload = message(70, data)
    payload.update(patch)
    with pytest.raises(capture.masters.CaptureError):
        writer.record(payload, data)
    assert writer.records == []
    assert list(writer.directory.glob("*.xml")) == []


@pytest.mark.parametrize("private", [Container("SYSTEM", (Column("UK", "S", ("private-key",)),)),
    Container("OtherUser", (Column("Name", "S", ("private-user",)),))])
def test_host_rejects_an_agent_that_did_not_filter_unknown_or_private_groups(writer, private):
    data = body(70, (private,))
    with pytest.raises(capture.masters.CaptureError) as caught:
        writer.record(message(70, data), data)
    assert caught.value.code == "schedule_agent_filter_incomplete"
    assert "private" not in str(caught.value)
    assert list(writer.directory.glob("*.xml")) == []


def test_host_rejects_remaining_nested_credentials(writer):
    data = dumps(Container(children=(Container("CSS", (Column("LineupID", "I", (1,)),
        Column("MethodKey", "S", ("private-method",)))),)))
    with pytest.raises(capture.masters.CaptureError, match="schedule_agent_filter_incomplete"):
        writer.record(message(70, data), data)
    assert list(writer.directory.glob("*.xml")) == []


def test_missing_required_group_is_not_treated_as_empty(writer):
    data = dumps(Container(children=(Container("FPR"),)))
    with pytest.raises(ScheduleCaptureError, match="missing_schedule_group"):
        writer.record(message(70, data), data)
    manifest = json.loads(writer.finish("missing_schedule_group").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed" and manifest["missing_operations"] == [70, 71]


@pytest.fixture
def configured(identity, monkeypatch):
    monkeypatch.setattr(capture, "profile", lambda _: (identity, {"fixture": True}))
    monkeypatch.setattr(capture.masters, "verify_process", lambda *_: identity)
    return Path(identity["path"])


def test_capture_detaches_after_paired_responses(configured, tmp_path):
    first, second = body(70), body(71)
    device = FakeDevice([event(ready()), event(message(70, first), first), event(message(71, second, 2), second)])
    progress = []
    path = capture.capture_process(device, 42, configured, tmp_path / "captures", 1,
        progress=progress.append, workspace=tmp_path)
    assert json.loads(path.read_text(encoding="utf-8"))["complete"]
    assert device.session.detach_count == 1 and device.attached == [42]
    assert [row["event"] for row in progress] == ["ready", "captured", "captured"]
    assert progress[-1]["missing_operations"] == []


def test_timeout_preserves_partial_data_and_reports_a_safe_manifest(configured, tmp_path):
    first = body(70)
    device = FakeDevice([event(ready()), event(message(70, first), first)])
    progress = []
    with pytest.raises(capture.masters.CaptureError, match="schedule_capture_timeout"):
        capture.capture_process(device, 42, configured, tmp_path / "captures", 1,
            progress=progress.append, workspace=tmp_path, clock=FakeClock(step=1))
    manifest = json.loads(Path(progress[-1]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "partial" and manifest["missing_operations"] == [71]
    assert len(manifest["records"]) == 1 and device.session.detach_count == 1


def test_slow_setup_does_not_shorten_the_capture_window(configured, tmp_path):
    first, second = body(70), body(71)
    clock = FakeClock(step=0.1)
    device = FakeDevice([event(ready())])
    clock.slow_setup(device, 5)

    def open_menu(update):
        if update["event"] == "ready":
            device.session.script.callback(*event(message(70, first), first))
            device.session.script.callback(*event(message(71, second, 2), second))

    path = capture.capture_process(device, 42, configured, tmp_path / "captures", 1,
        progress=open_menu, workspace=tmp_path, clock=clock)
    assert json.loads(path.read_text(encoding="utf-8"))["complete"]
    assert device.session.detach_count == 1


def test_messages_queued_before_the_deadline_complete_a_late_checked_capture(configured, tmp_path):
    first, second = body(70), body(71)
    device = FakeDevice([event(ready()), event(message(70, first), first), event(message(71, second, 2), second)])
    path = capture.capture_process(device, 42, configured, tmp_path / "captures", 1,
        workspace=tmp_path, clock=FakeClock(step=2))
    assert json.loads(path.read_text(encoding="utf-8"))["complete"]
    assert device.session.detach_count == 1


def test_messages_arriving_after_the_deadline_cannot_extend_the_capture(configured, tmp_path):
    rejection = event({"event": "rejected", "operation": 70, "code": "invalid_length"})
    device = FakeDevice([event(ready()), rejection, rejection])
    follow_ups = [rejection] * 10
    progress = []

    def keep_rejecting(update):
        progress.append(update)
        if update["event"] == "rejected" and follow_ups:
            device.session.script.callback(*follow_ups.pop())

    with pytest.raises(capture.masters.CaptureError, match="schedule_capture_timeout"):
        capture.capture_process(device, 42, configured, tmp_path / "captures", 1,
            progress=keep_rejecting, workspace=tmp_path, clock=FakeClock(step=2))
    manifest = json.loads(Path(progress[-1]["manifest"]).read_text(encoding="utf-8"))
    assert len(manifest["rejections"]) == 2 and len(follow_ups) == 8
    assert device.session.detach_count == 1


def test_queue_overflow_is_reported_instead_of_the_timeout(configured, tmp_path):
    device = FakeDevice([event(ready())] * 10)
    progress = []
    with pytest.raises(capture.masters.CaptureError, match="schedule_queue_overflow"):
        capture.capture_process(device, 42, configured, tmp_path / "captures", 1,
            progress=progress.append, workspace=tmp_path, clock=FakeClock(step=2))
    manifest = json.loads(Path(progress[-1]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["error_code"] == "schedule_queue_overflow" and manifest["status"] == "failed"
    assert device.session.detach_count == 1


def test_agent_error_text_is_never_logged_or_persisted(configured, tmp_path):
    device = FakeDevice([({"type": "error", "description": "private-token-error"}, None)])
    progress = []
    with pytest.raises(capture.masters.CaptureError) as caught:
        capture.capture_process(device, 42, configured, tmp_path / "captures", 1,
            progress=progress.append, workspace=tmp_path)
    assert caught.value.code == "schedule_agent_error"
    assert "private-token" not in json.dumps(progress)
    assert "private-token" not in Path(progress[-1]["manifest"]).read_text(encoding="utf-8")
    assert device.session.detach_count == 1


def test_rejected_payload_can_be_followed_by_a_successful_round(configured, tmp_path):
    first, second = body(70), body(71)
    device = FakeDevice([event(ready()), event({"event": "rejected", "operation": 70, "code": "invalid_length"}),
        event(message(70, first), first), event(message(71, second, 2), second)])
    path = capture.capture_process(device, 42, configured, tmp_path / "captures", 1, workspace=tmp_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["complete"] and len(manifest["rejections"]) == 1


def test_real_profile_verifies_disk_without_attaching():
    if not capture.masters.DEFAULT_EXE.is_file():
        pytest.skip("Original client is unavailable")
    identity, config = capture.profile(capture.masters.DEFAULT_EXE)
    assert identity["sha256"] == hashlib.sha256(capture.masters.DEFAULT_EXE.read_bytes()).hexdigest()
    assert identity["sha256"] in capture.masters.SUPPORTED_SHA256
    assert config["operations"]["70"]["vtableRva"] == 0xA69298
    assert "userkey" in config["privateFields"] and "methodkey" in config["privateFields"]


def test_check_does_not_import_frida_or_create_output(configured, identity, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(capture.masters, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(capture.importlib, "import_module", lambda _: pytest.fail("Check must not load Frida"))
    output = tmp_path / "uncreated"
    assert capture.main(["--check", "--exe", str(configured), "--output", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "checked" and result["attached"] is False
    assert not output.exists()
