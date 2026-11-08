import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.public_tables import REQUIRED_FIELDS
from test_master_capture import FakeClock


CLIENT = Path(__file__).resolve().parents[1] / "client"
_original_path = sys.path[:]
try:
    sys.path.insert(0, str(CLIENT))
    _spec = importlib.util.spec_from_file_location("capture_content", CLIENT / "capture_content.py")
    content = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(content)
finally:
    sys.path[:] = _original_path


BASE = 0x600000
VERSION = "a" * 40


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    path = tmp_path / "Aigis_r.exe"
    path.write_bytes(b"fixture")
    identity = {"path": str(path), "sha256": content.masters.EXPECTED_SHA256}
    schemas = {name: set(fields) for name, fields in REQUIRED_FIELDS.items()}
    monkeypatch.setattr(content.masters, "verify_original_executable", lambda _: identity)
    monkeypatch.setattr(content, "_prepare", lambda _: (identity, schemas, {"resources": "resources", "masters": "masters"}))
    return path, identity, schemas


def master_ready(base=BASE):
    return "masters", {"type": "send", "payload": {"event": "ready", "base": hex(base),
            "functionAddress": hex(base + content.masters.FUNCTION_RVA), "pointerSize": 4}}, None


def resource_ready(base=BASE):
    return "resources", {"type": "send", "payload": {"event": "ready", "base": hex(base),
            "functionAddress": hex(base + content.resources.FUNCTION_RVA)}}, None


def version_message():
    return "resources", {"type": "send", "payload": {"event": "version", "version": VERSION,
            "returnAddress": hex(BASE + content.resources.CALLERS[0] + 5)}}, None


def master_message(name):
    data = dumps(Container(children=tuple(Column(field, "I", (1, 2)) for field in sorted(REQUIRED_FIELDS[name]))))
    payload = {"event": "master", "table": name, "length": len(data),
               "returnAddress": hex(BASE + content.masters.TABLES[name]["callRva"] + 5),
               "functionAddress": hex(BASE + content.masters.FUNCTION_RVA)}
    return "masters", {"type": "send", "payload": payload}, data


def master_complete():
    return "masters", {"type": "send", "payload": {"event": "complete"}}, None


class FakeScript:
    def __init__(self, session, source):
        self.session = session
        self.source = source
        self.callback = None

    def on(self, signal, callback):
        assert signal == "message"
        self.callback = callback

    def load(self):
        self.session.loaded.append(self.source)
        if len(self.session.loaded) == 2:
            for source, message, data in self.session.events:
                if source == "session":
                    self.session.detached_callback()
                else:
                    self.session.scripts[source].callback(message, data)


class FakeSession:
    def __init__(self, events):
        self.events = events
        self.scripts = {}
        self.loaded = []
        self.detaches = 0
        self.detached_callback = None

    def on(self, signal, callback):
        assert signal == "detached"
        self.detached_callback = callback

    def create_script(self, source):
        script = FakeScript(self, source)
        self.scripts[source] = script
        return script

    def detach(self):
        self.detaches += 1


class FakeDevice:
    def __init__(self, events=()):
        self.session = FakeSession(events)
        self.attached = []

    def attach(self, pid):
        self.attached.append(pid)
        return self.session

    def enumerate_processes(self, **kwargs):
        assert kwargs == {"scope": "minimal"}
        return (SimpleNamespace(pid=1, name="Aigis_r.exe"),)


def run_capture(tmp_path, prepared, events, **kwargs):
    path, _, _ = prepared
    device = FakeDevice(events)
    result = content.capture_bundle(device, 123, path, tmp_path / "captures", 1,
                                    path_query=lambda _: path, workspace=tmp_path, **kwargs)
    return device, result


@pytest.mark.parametrize("order", ["masters_first", "version_first", "interleaved"])
def test_both_event_orders_use_one_attachment_and_preserve_component_formats(tmp_path, prepared, order):
    if order == "masters_first":
        events = [resource_ready(), master_ready(), master_message("CardData"), master_message("QuestTable"),
                  master_complete(), version_message()]
    elif order == "version_first":
        events = [resource_ready(), version_message(), master_ready(), master_message("CardData"), master_message("QuestTable")]
    else:
        events = [resource_ready(), master_ready(), master_message("CardData"), version_message(), master_message("QuestTable")]
    updates = []
    device, path = run_capture(tmp_path, prepared, events, progress=updates.append)
    bundle = json.loads(path.read_text())
    master = json.loads((path.parent / bundle["master_manifest"]).read_text())
    resource = json.loads((path.parent / bundle["resource_context"]).read_text())
    assert bundle["complete"] is True and bundle["missing"] == []
    assert master["schema_version"] == 1 and master["complete"] is True
    assert set(master["tables"]) == {"CardData", "QuestTable"}
    assert resource["schema_version"] == 1 and resource["complete"] is True
    assert resource["version"] == VERSION and resource["index_name"] == content.resources.INDEX_NAME
    assert device.attached == [123]
    assert device.session.loaded == ["resources", "masters"]
    assert device.session.detaches == 1
    assert sum(update["event"] == "ready" for update in updates) == 1
    assert sum(update["event"] == "captured" for update in updates) == 3
    assert all("payload" not in update and "body" not in update for update in updates)


@pytest.mark.parametrize("component", ["masters", "version"])
def test_partial_components_never_report_bundle_success(tmp_path, prepared, component):
    path, _, _ = prepared
    events = [resource_ready(), master_ready()]
    events += [master_message("CardData"), master_message("QuestTable"), master_complete()] if component == "masters" else [version_message()]
    device = FakeDevice(events)
    with pytest.raises(content.ContentCaptureError) as error:
        content.capture_bundle(device, 123, path, tmp_path / "out", 1,
                               path_query=lambda _: path, workspace=tmp_path, clock=FakeClock(step=1))
    assert error.value.code == "content_capture_timeout"
    bundle_path = error.value.bundle_path
    bundle = json.loads(bundle_path.read_text())
    assert bundle["complete"] is False and bundle["status"] == "partial"
    if component == "masters":
        assert bundle["missing"] == ["AssetPath"]
        assert json.loads((bundle_path.parent / bundle["master_manifest"]).read_text())["complete"] is True
        assert bundle["resource_context"] is None
    else:
        assert bundle["missing"] == ["CardData", "QuestTable"]
        assert json.loads((bundle_path.parent / bundle["master_manifest"]).read_text())["complete"] is False
        assert json.loads((bundle_path.parent / bundle["resource_context"]).read_text())["complete"] is True
    assert device.session.detaches == 1


def test_slow_setup_does_not_shorten_the_capture_window(tmp_path, prepared):
    path, _, _ = prepared
    clock = FakeClock(step=0.1)
    device = FakeDevice([resource_ready(), master_ready()])
    clock.slow_setup(device, 5)

    def deliver_content(update):
        if update["event"] == "ready":
            for source, message, data in (master_message("CardData"), master_message("QuestTable"), version_message()):
                device.session.scripts[source].callback(message, data)

    result = content.capture_bundle(device, 123, path, tmp_path / "out", 1, progress=deliver_content,
                                    path_query=lambda _: path, workspace=tmp_path, clock=clock)
    assert json.loads(result.read_text())["complete"] is True
    assert device.session.detaches == 1


def test_messages_queued_before_the_deadline_complete_a_late_checked_bundle(tmp_path, prepared):
    path, _, _ = prepared
    device = FakeDevice([resource_ready(), master_ready(), master_message("CardData"),
                         master_message("QuestTable"), version_message()])
    result = content.capture_bundle(device, 123, path, tmp_path / "out", 1,
                                    path_query=lambda _: path, workspace=tmp_path, clock=FakeClock(step=2))
    assert json.loads(result.read_text())["complete"] is True
    assert device.session.detaches == 1


@pytest.mark.parametrize("events,code", [
    ([resource_ready(), master_ready(), master_complete()], "premature_master_completion"),
    ([resource_ready(), master_ready(BASE + 0x100000)], "content_module_mismatch"),
    ([resource_ready(), ("resources", {"type": "error", "description": "private-token"}, None)], "content_agent_error"),
    ([resource_ready(), ("session", {}, None)], "process_detached"),
    ([resource_ready()] * 20, "content_queue_overflow"),
])
def test_failures_are_sanitized_and_always_detach(tmp_path, prepared, events, code):
    path, _, _ = prepared
    device = FakeDevice(events)
    with pytest.raises(content.ContentCaptureError) as error:
        content.capture_bundle(device, 123, path, tmp_path / "out", 0.1,
                               path_query=lambda _: path, workspace=tmp_path)
    assert error.value.code == code
    bundle_text = error.value.bundle_path.read_text()
    assert json.loads(bundle_text)["complete"] is False
    assert "private-token" not in bundle_text
    assert device.session.detaches == 1


def test_resource_callbacks_cannot_smuggle_binary_responses(tmp_path, prepared):
    path, _, _ = prepared
    source, message, _ = version_message()
    device = FakeDevice([resource_ready(), (source, message, b"private-response")])
    with pytest.raises(content.ContentCaptureError, match="unexpected_content_body") as error:
        content.capture_bundle(device, 123, path, tmp_path / "out", 0.1,
                               path_query=lambda _: path, workspace=tmp_path)
    assert not (error.value.bundle_path.parent / "resource-context.json").exists()
    assert b"private-response" not in error.value.bundle_path.read_bytes()


def test_identity_is_rechecked_after_single_attach(tmp_path, prepared):
    path, _, _ = prepared
    calls = iter((path, tmp_path / "different.exe"))
    device = FakeDevice()
    with pytest.raises(content.ContentCaptureError, match="process_path_mismatch"):
        content.capture_bundle(device, 123, path, tmp_path / "out", 0.1,
                               path_query=lambda _: next(calls), workspace=tmp_path)
    assert device.attached == [123] and device.session.detaches == 1
    assert device.session.scripts == {}


def test_check_does_not_import_frida_or_create_output(tmp_path, prepared, monkeypatch, capsys):
    path, _, _ = prepared
    monkeypatch.setattr(content.masters, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(content.importlib, "import_module", lambda _: pytest.fail("Check must not import Frida"))
    output = tmp_path / "out"
    assert content.main(["--check", "--exe", str(path), "--output", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["attached"] is False and set(result["agent_sha256"]) == {"resources", "masters"}
    assert not output.exists()


def test_wait_excludes_existing_pids_and_uses_remaining_budget(tmp_path, prepared, monkeypatch, capsys):
    path, _, _ = prepared
    monkeypatch.setattr(content.masters, "WORKSPACE_ROOT", tmp_path)
    device = FakeDevice()
    monkeypatch.setattr(content.importlib, "import_module", lambda _: SimpleNamespace(get_local_device=lambda: device))

    def wait(device_arg, executable, timeout, *, excluded_pids):
        assert device_arg is device and executable == path and excluded_pids == {1}
        assert 0 < timeout <= 60
        return 2

    def capture(device_arg, pid, executable, output, timeout, *, progress):
        assert device_arg is device and pid == 2 and executable == path
        assert 0 < timeout <= 60
        return tmp_path / "bundle.json"

    monkeypatch.setattr(content.masters, "wait_for_process", wait)
    monkeypatch.setattr(content, "capture_bundle", capture)
    assert content.main(["--wait", "--exe", str(path), "--output", str(tmp_path / "out"), "--timeout", "60"]) == 0
    result = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert result[0]["event"] == "waiting_for_process" and result[-1]["complete"] is True


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), 3601])
def test_invalid_timeout_does_not_attach(tmp_path, prepared, timeout):
    path, _, _ = prepared
    device = FakeDevice()
    with pytest.raises(content.masters.CaptureError, match="invalid_capture_timeout"):
        content.capture_bundle(device, 123, path, tmp_path / "out", timeout, workspace=tmp_path)
    assert device.attached == []


def test_powershell_wrapper_parses_without_execution():
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell parser is unavailable")
    path = str(CLIENT / "capture-content.ps1").replace("'", "''")
    command = "$tokens=$null; $errors=$null; [System.Management.Automation.Language.Parser]::ParseFile('" + path + "', [ref]$tokens, [ref]$errors) > $null; if ($errors.Count) { $errors | ForEach-Object Message; exit 1 }"
    result = subprocess.run([shell, "-NoProfile", "-Command", command], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
