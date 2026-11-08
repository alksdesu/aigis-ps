import hashlib
import json
from pathlib import Path
import signal
import threading
from types import SimpleNamespace

import pytest

from client import capture_all as capture
from aigis_ps.alxml import Column, Container, dumps


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def schedule_body(operation, value=17):
    group, field = ("CSS", "LineupID") if operation == 70 else ("CSP", "ConfigID")
    return dumps(Container(children=(Container(group, (Column(field, "I", (value,)),)),)))


@pytest.fixture
def identity(tmp_path):
    executable = tmp_path / "Aigis_r.exe"
    executable.write_bytes(b"fixture")
    return {"path": str(executable), "sha256": capture.masters.EXPECTED_SHA256}


@pytest.fixture
def exporter(tmp_path, identity):
    source = tmp_path / "responses"
    source.mkdir()
    result = capture.ScheduleExport(tmp_path / "schedules", identity, 42, workspace=tmp_path)
    result.observe({"event": "ready", "directory": str(source), "pid": 42})
    return result, source


def add_response(exporter, operation, sequence, *, data=None, patch=None):
    target, source = exporter
    data = schedule_body(operation) if data is None else data
    name = f"{sequence:05d}-{operation}.xml"
    (source / name).write_bytes(data)
    event = {"event": "captured", "operation": operation, "sequence": sequence,
             "path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
             "function_rva": hex(capture.schedule.FUNCTION_RVA),
             "return_rva": hex(capture.schedule.RETURN_RVA),
             "vtable_rva": hex(capture.schedule.VTABLES[operation])}
    target.observe({**event, **(patch or {})})
    return event


def test_first_complete_schedule_round_is_frozen(exporter):
    target, _ = exporter
    add_response(exporter, 71, 1)
    assert not target.writer.complete
    add_response(exporter, 70, 2)
    add_response(exporter, 70, 4, data=schedule_body(70, 28))
    add_response(exporter, 71, 8)
    path = target.finish()
    original = path.read_bytes()
    manifest = read_json(path)
    assert manifest["complete"] is True
    assert manifest["selected_round"] == {"70": "0004-70.xml", "71": "0008-71.xml"}
    assert b"28" in (path.parent / manifest["selected_round"]["70"]).read_bytes()
    add_response(exporter, 70, 10)
    target.observe({"event": "rejected", "operation": 70, "code": "invalid_response_xml"})
    assert target.finish() == path
    assert path.read_bytes() == original


@pytest.mark.parametrize("event", ["rejected", "excluded"])
def test_rejected_or_excluded_new_css_cannot_reuse_previous_css(exporter, event):
    target, _ = exporter
    add_response(exporter, 70, 1)
    target.observe({"event": event, "operation": 70, "code": "invalid_response_xml"})
    add_response(exporter, 71, 3)
    assert not target.writer.complete
    add_response(exporter, 70, 4)
    add_response(exporter, 71, 5)
    manifest = read_json(target.finish())
    assert manifest["complete"] is True
    assert manifest["selected_round"]["70"] == "0004-70.xml"


@pytest.mark.parametrize("data", [b"not xml", b"<DA/>", schedule_body(71)])
def test_invalid_new_css_resets_pairing_without_stopping_later_collection(exporter, data):
    target, _ = exporter
    add_response(exporter, 70, 1)
    add_response(exporter, 70, 2, data=data)
    add_response(exporter, 71, 3)
    assert not target.writer.complete
    add_response(exporter, 70, 4)
    add_response(exporter, 71, 5)
    assert read_json(target.finish())["complete"] is True


@pytest.mark.parametrize("patch", [
    {"function_rva": "0x1"}, {"return_rva": "0x1"}, {"vtable_rva": "0x1"},
    {"sha256": "0" * 64}, {"bytes": 1}, {"path": "../outside.xml"},
])
def test_untrusted_source_metadata_cannot_create_a_schedule_pair(exporter, patch):
    target, source = exporter
    (source.parent / "outside.xml").write_bytes(schedule_body(70))
    add_response(exporter, 70, 1)
    add_response(exporter, 70, 2, patch=patch)
    add_response(exporter, 71, 3)
    assert not target.writer.complete
    manifest = read_json(target.finish())
    assert manifest["complete"] is False
    assert "../outside.xml" not in json.dumps(manifest)


def test_sequence_gaps_are_allowed_but_replays_are_rejected(exporter):
    target, _ = exporter
    add_response(exporter, 70, 8)
    add_response(exporter, 70, 8)
    add_response(exporter, 71, 12)
    assert not target.writer.complete


def test_schedule_capacity_does_not_raise_into_response_collection(exporter, monkeypatch):
    target, _ = exporter
    monkeypatch.setattr(capture.schedule, "MAX_RECORDS", 1)
    add_response(exporter, 70, 1)
    add_response(exporter, 70, 2)
    add_response(exporter, 71, 3)
    assert target.error_code == "schedule_capture_capacity"
    manifest = read_json(target.finish())
    assert manifest["complete"] is False
    assert manifest["status"] == "partial"


def test_read_failure_preserves_partial_schedule_and_never_leaks_exception_text(exporter, monkeypatch):
    target, source = exporter
    add_response(exporter, 70, 1)
    event = add_response(exporter, 71, 2, patch={"event": "ignored"})
    original = Path.open

    def fail_response(path, *args, **kwargs):
        if path.parent == source and args and args[0] == "rb":
            raise OSError("private-token-error")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_response)
    target.observe(event)
    path = target.finish()
    manifest = read_json(path)
    assert manifest["complete"] is False
    assert target.error_code
    assert "private-token" not in path.read_text(encoding="utf-8")


def test_missing_schedule_still_produces_a_manifest(exporter):
    target, _ = exporter
    manifest = read_json(target.finish())
    assert manifest["complete"] is False
    assert manifest["missing_operations"] == [70, 71]


@pytest.mark.parametrize("argv", [
    [], ["--pid", "42"], ["--wait", "--timeout", "0"],
    ["--check", "--timeout", "14401"], ["--check", "--timeout", "nan"],
    ["--wait", "--content-timeout", "0"], ["--check", "--content-timeout", "3601"],
    ["--check", "--content-timeout", "inf"], ["--wait", "--check"],
])
def test_command_line_rejects_unsupported_modes_and_timeouts(argv):
    with pytest.raises(SystemExit) as caught:
        capture.main(argv)
    assert caught.value.code == 2


@pytest.fixture
def configured(tmp_path, identity, monkeypatch):
    monkeypatch.setattr(capture.masters, "verify_process", lambda *_: identity)
    monkeypatch.setattr(capture.masters, "verify_original_executable", lambda *_: identity)
    monkeypatch.setattr(capture.masters, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(capture.content, "_prepare", lambda _: (identity, {}, {"masters": "masters", "resources": "resources"}))
    monkeypatch.setattr(capture.responses, "profile", lambda *_: (identity, {}))
    monkeypatch.setattr(capture.responses, "agent_source", lambda _: "responses")
    monkeypatch.setattr(capture.schedule, "profile", lambda _: (identity, {}))
    monkeypatch.setattr(capture.content, "DEFAULT_OUTPUT", tmp_path / "content")
    monkeypatch.setattr(capture.responses, "DEFAULT_OUTPUT", tmp_path / "responses")
    monkeypatch.setattr(capture.schedule, "DEFAULT_OUTPUT", tmp_path / "schedule")
    return Path(identity["path"])


def save_result(output, name, value):
    output.mkdir(parents=True, exist_ok=True)
    path = output / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def finish_responses(output, report, *, interrupted=False):
    output.mkdir(parents=True, exist_ok=True)
    report({"event": "ready", "directory": str(output), "pid": 42})
    for sequence, operation in enumerate((70, 71), 1):
        data = schedule_body(operation)
        name = f"{sequence:05d}-{operation}.xml"
        (output / name).write_bytes(data)
        report({"event": "captured", "operation": operation, "sequence": sequence,
                "path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "function_rva": hex(capture.schedule.FUNCTION_RVA),
                "return_rva": hex(capture.schedule.RETURN_RVA),
                "vtable_rva": hex(capture.schedule.VTABLES[operation])})
    reason = "interrupted" if interrupted else "timeout"
    path = save_result(output, "manifest.json", {"status": "stopped", "records": 2, "end_reason": reason})
    report({"event": "finished", "status": "stopped", "reason": reason, "manifest": str(path)})
    return path


def test_both_collectors_share_one_pid_and_run_concurrently(configured, tmp_path, monkeypatch):
    barrier = threading.Barrier(2, timeout=3)
    calls = {}

    def content_worker(device, pid, executable, output, timeout, **options):
        calls["content"] = (device, pid, executable, timeout, options["stop_event"])
        barrier.wait()
        return save_result(output, "bundle.json", {"status": "complete", "complete": True})

    def responses_worker(device, pid, executable, output, timeout, selection, **options):
        calls["responses"] = (device, pid, executable, timeout, options["stop_event"])
        assert selection.mode == "all" and selection.operations is None
        barrier.wait()
        return finish_responses(output, options["progress"])

    monkeypatch.setattr(capture.content, "capture_bundle", content_worker)
    monkeypatch.setattr(capture.responses, "capture_process", responses_worker)
    device = object()
    path = capture.capture_run(device, 42, configured, tmp_path / "runs", 60, content_timeout=15, workspace=tmp_path)
    result = read_json(path)
    assert result["status"] == "stopped"
    assert result["coverage_verified"] is False
    assert calls["content"][:3] == calls["responses"][:3] == (device, 42, configured)
    assert calls["content"][4] is calls["responses"][4]
    assert 0 < calls["content"][3] <= 15
    assert 0 < calls["responses"][3] <= 60
    assert result["components"]["content"]["complete"] is True
    assert result["components"]["schedule"]["complete"] is True
    assert result["components"]["responses"]["records"] == 2


def test_content_failure_retains_its_bundle_and_allows_response_collection(configured, tmp_path, monkeypatch):
    barrier = threading.Barrier(2, timeout=3)
    failed = threading.Event()

    def content_worker(device, pid, executable, output, timeout, **options):
        barrier.wait()
        path = save_result(output, "bundle.json", {"status": "partial", "complete": False,
            "error_code": "content_capture_timeout"})
        failed.set()
        raise capture.content.ContentCaptureError("content_capture_timeout", path, "partial")

    def responses_worker(device, pid, executable, output, timeout, selection, **options):
        barrier.wait()
        assert failed.wait(3)
        assert not options["stop_event"].is_set()
        return finish_responses(output, options["progress"])

    monkeypatch.setattr(capture.content, "capture_bundle", content_worker)
    monkeypatch.setattr(capture.responses, "capture_process", responses_worker)
    path = capture.capture_run(object(), 42, configured, tmp_path / "runs", 60, workspace=tmp_path)
    result = read_json(path)
    assert result["status"] == "partial"
    assert result["components"]["content"]["status"] == "partial"
    assert result["components"]["content"]["path"]
    assert result["components"]["content"]["error_code"] == "content_capture_timeout"
    assert result["components"]["responses"]["records"] == 2
    assert result["components"]["schedule"]["complete"] is True


def test_worker_exception_is_sanitized_and_does_not_discard_other_result(configured, tmp_path, monkeypatch):
    def content_worker(device, pid, executable, output, timeout, **options):
        return save_result(output, "bundle.json", {"status": "complete", "complete": True})

    def responses_worker(*args, **options):
        raise RuntimeError("private-token-error")

    monkeypatch.setattr(capture.content, "capture_bundle", content_worker)
    monkeypatch.setattr(capture.responses, "capture_process", responses_worker)
    path = capture.capture_run(object(), 42, configured, tmp_path / "runs", 60, workspace=tmp_path)
    result = read_json(path)
    assert result["status"] == "partial"
    assert result["components"]["content"]["complete"] is True
    assert result["components"]["responses"]["status"] == "failed"
    assert "private-token" not in path.read_text(encoding="utf-8")


def test_shared_cancellation_waits_for_both_manifests(configured, tmp_path, monkeypatch):
    barrier = threading.Barrier(2, timeout=3)
    stop = threading.Event()
    finished = set()

    def content_worker(device, pid, executable, output, timeout, **options):
        assert options["stop_event"] is stop
        barrier.wait()
        assert stop.wait(3)
        path = save_result(output, "bundle.json", {"status": "partial", "complete": False,
            "error_code": "capture_interrupted"})
        finished.add("content")
        raise capture.content.ContentCaptureError("capture_interrupted", path, "partial")

    def responses_worker(device, pid, executable, output, timeout, selection, **options):
        assert options["stop_event"] is stop
        barrier.wait()
        finish_responses(output, options["progress"], interrupted=True)
        stop.set()
        finished.add("responses")
        raise KeyboardInterrupt

    monkeypatch.setattr(capture.content, "capture_bundle", content_worker)
    monkeypatch.setattr(capture.responses, "capture_process", responses_worker)
    path = capture.capture_run(object(), 42, configured, tmp_path / "runs", 60,
        workspace=tmp_path, stop_event=stop)
    result = read_json(path)
    assert result["status"] == "interrupted"
    assert finished == {"content", "responses"}
    assert result["components"]["content"]["path"]
    assert result["components"]["responses"]["path"]


def test_check_does_not_load_frida_or_create_outputs(configured, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(capture.importlib, "import_module", lambda _: pytest.fail("Check must not import Frida"))
    output = tmp_path / "runs"
    assert capture.main(["--check", "--exe", str(configured), "--output", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "checked" and report["attached"] is False
    for path in (output, capture.content.DEFAULT_OUTPUT, capture.responses.DEFAULT_OUTPUT, capture.schedule.DEFAULT_OUTPUT):
        assert not path.exists()


def test_wait_excludes_existing_processes_and_starts_both_collectors_on_the_new_pid(configured, tmp_path, monkeypatch, capsys):
    device = SimpleNamespace(enumerate_processes=lambda **_: [SimpleNamespace(pid=1), SimpleNamespace(pid=2)])
    monkeypatch.setattr(capture.importlib, "import_module", lambda name: SimpleNamespace(get_local_device=lambda: device))
    observed = {}

    def wait_for_process(actual_device, executable, timeout, **options):
        assert actual_device is device and executable == configured
        assert options["excluded_pids"] == {1, 2}
        observed["wait_timeout"] = timeout
        return 42

    def capture_run(actual_device, pid, executable, output, timeout, **options):
        assert actual_device is device and pid == 42 and executable == configured
        assert 0 < timeout <= observed["wait_timeout"]
        assert options["content_timeout"] == 15
        return save_result(output, "run.json", {"status": "stopped"})

    monkeypatch.setattr(capture.masters, "wait_for_process", wait_for_process)
    monkeypatch.setattr(capture, "capture_run", capture_run)
    assert capture.main(["--wait", "--exe", str(configured), "--output", str(tmp_path / "runs"),
                         "--timeout", "60", "--content-timeout", "15"]) == 0
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert messages[0]["event"] == "waiting_for_process"
    assert messages[0]["new_process_only"] is True


def test_ctrl_c_in_coordinator_cancels_workers_and_preserves_their_results(configured, tmp_path, monkeypatch):
    finished = set()
    started = threading.Barrier(3, timeout=3)
    original_wait = capture.wait
    interrupted = False

    def interrupt_once(*args, **kwargs):
        nonlocal interrupted
        if not interrupted:
            started.wait()
            interrupted = True
            raise KeyboardInterrupt
        return original_wait(*args, **kwargs)

    def content_worker(device, pid, executable, output, timeout, **options):
        started.wait()
        assert options["stop_event"].wait(3)
        path = save_result(output, "bundle.json", {"status": "partial", "complete": False,
            "error_code": "capture_interrupted"})
        finished.add("content")
        raise capture.content.ContentCaptureError("capture_interrupted", path, "partial")

    def responses_worker(device, pid, executable, output, timeout, selection, **options):
        started.wait()
        assert options["stop_event"].wait(3)
        path = save_result(output, "manifest.json", {"status": "stopped", "records": 0, "end_reason": "interrupted"})
        options["progress"]({"event": "finished", "status": "stopped", "manifest": str(path)})
        finished.add("responses")
        raise KeyboardInterrupt

    monkeypatch.setattr(capture, "wait", interrupt_once)
    monkeypatch.setattr(capture.content, "capture_bundle", content_worker)
    monkeypatch.setattr(capture.responses, "capture_process", responses_worker)
    path = capture.capture_run(object(), 42, configured, tmp_path / "runs", 60, workspace=tmp_path)
    result = read_json(path)
    assert result["status"] == "interrupted"
    assert finished == {"content", "responses"}
    assert result["components"]["content"]["path"]
    assert result["components"]["responses"]["path"]


def test_repeated_interrupt_signal_keeps_both_results_and_restores_original_handler(configured, tmp_path, monkeypatch):
    previous_handler = signal.getsignal(signal.SIGINT)
    started = threading.Barrier(2, timeout=3)
    finished = set()

    def content_worker(device, pid, executable, output, timeout, **options):
        started.wait()
        assert options["stop_event"].wait(3)
        path = save_result(output, "bundle.json", {"status": "partial", "complete": False,
            "error_code": "capture_interrupted"})
        finished.add("content")
        raise capture.content.ContentCaptureError("capture_interrupted", path, "partial")

    def responses_worker(device, pid, executable, output, timeout, selection, **options):
        started.wait()
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler) and handler is not previous_handler
        handler(signal.SIGINT, None)
        handler(signal.SIGINT, None)
        assert options["stop_event"].is_set()
        path = finish_responses(output, options["progress"], interrupted=True)
        finished.add("responses")
        return path

    monkeypatch.setattr(capture.content, "capture_bundle", content_worker)
    monkeypatch.setattr(capture.responses, "capture_process", responses_worker)
    path = capture.capture_run(object(), 42, configured, tmp_path / "runs", 60, workspace=tmp_path)
    assert signal.getsignal(signal.SIGINT) is previous_handler
    result = read_json(path)
    assert result["status"] == "interrupted"
    assert finished == {"content", "responses"}
    assert result["components"]["content"]["path"]
    assert result["components"]["responses"]["records"] == 2


def test_setup_error_also_restores_original_signal_handler(configured, tmp_path):
    previous_handler = signal.getsignal(signal.SIGINT)
    with pytest.raises(capture.masters.CaptureError, match="invalid_capture_timeout"):
        capture.capture_run(object(), 42, configured, tmp_path / "runs", 0, workspace=tmp_path)
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_schedule_setup_failure_does_not_prevent_either_collector(configured, tmp_path, monkeypatch):
    finished = set()

    def fail_schedule(*args, **kwargs):
        raise OSError("private-token-error")

    def content_worker(device, pid, executable, output, timeout, **options):
        finished.add("content")
        return save_result(output, "bundle.json", {"status": "complete", "complete": True})

    def responses_worker(device, pid, executable, output, timeout, selection, **options):
        finished.add("responses")
        return finish_responses(output, options["progress"])

    monkeypatch.setattr(capture, "ScheduleExport", fail_schedule)
    monkeypatch.setattr(capture.content, "capture_bundle", content_worker)
    monkeypatch.setattr(capture.responses, "capture_process", responses_worker)
    path = capture.capture_run(object(), 42, configured, tmp_path / "runs", 60, workspace=tmp_path)
    result = read_json(path)
    assert result["status"] == "partial"
    assert finished == {"content", "responses"}
    assert result["components"]["content"]["complete"] is True
    assert result["components"]["responses"]["records"] == 2
    assert result["components"]["schedule"]["status"] == "failed"
    assert result["components"]["schedule"]["error_code"] == "schedule_setup_failed"
    assert "private-token" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("source", ["content", "responses"])
@pytest.mark.parametrize("malformed", [
    [], {"status": []}, {"status": "running"}, {"status": "complete", "complete": 1},
    {"status": "stopped", "records": True}, {"status": "stopped", "records": -1},
    {"status": "stopped", "records": "2"}, {"status": "stopped", "end_reason": []},
])
def test_malformed_component_manifest_does_not_discard_other_result_or_leave_run_active(configured, tmp_path, monkeypatch, source, malformed):
    def content_worker(device, pid, executable, output, timeout, **options):
        value = malformed if source == "content" else {"status": "complete", "complete": True}
        return save_result(output, "bundle.json", value)

    def responses_worker(device, pid, executable, output, timeout, selection, **options):
        path = finish_responses(output, options["progress"])
        if source == "responses":
            path.write_text(json.dumps(malformed), encoding="utf-8")
        return path

    monkeypatch.setattr(capture.content, "capture_bundle", content_worker)
    monkeypatch.setattr(capture.responses, "capture_process", responses_worker)
    path = capture.capture_run(object(), 42, configured, tmp_path / "runs", 60, workspace=tmp_path)
    result = read_json(path)
    assert result["status"] == "partial"
    assert result["finished_utc"]
    assert result["components"][source]["status"] == "failed"
    assert result["components"][source]["error_code"] == "capture_manifest_unreadable"
    other = "responses" if source == "content" else "content"
    assert result["components"][other]["status"] == ("stopped" if other == "responses" else "complete")
