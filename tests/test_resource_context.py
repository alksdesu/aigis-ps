import importlib
import json
from pathlib import Path

import pytest

from test_master_capture import FakeClock, FakeDevice


BASE = 0x600000
VERSION = "a" * 40
# capture_context buffers at most 16 agent messages.
QUEUE_LIMIT = 16


@pytest.fixture
def capture(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "client"))
    return importlib.import_module("capture_resources")


@pytest.fixture
def stubbed(capture, tmp_path, monkeypatch):
    identity = {"path": str(tmp_path / "Aigis_r.exe"), "sha256": capture.EXPECTED_SHA256}
    monkeypatch.setattr(capture, "verify_process", lambda *_args, **_kwargs: identity)
    monkeypatch.setattr(capture, "capture_config", lambda _: {"fixture": True})
    monkeypatch.setattr(capture, "WORKSPACE_ROOT", tmp_path)
    return capture


def ready(capture):
    return {"type": "send", "payload": {"event": "ready", "base": hex(BASE),
            "functionAddress": hex(BASE + capture.FUNCTION_RVA)}}, None


def version(capture):
    return {"type": "send", "payload": {"event": "version", "version": VERSION,
            "returnAddress": hex(BASE + capture.CALLERS[0] + 5)}}, None


def collect(capture, tmp_path, device, **kwargs):
    return capture.capture_context(device, 123, tmp_path / "Aigis_r.exe", tmp_path / "contexts", 1, **kwargs)


def test_resource_version_is_only_a_public_hash_from_verified_callers(capture):
    payload = {"version": "a" * 40, "returnAddress": hex(0x560000 + capture.CALLERS[0] + 5)}
    assert capture.validate_version(payload, 0x560000) == "a" * 40
    for value in ("http://localhost/", "UK=private", "../" + "a" * 40, "a" * 41, "A" * 40, None):
        with pytest.raises(capture.CaptureError):
            capture.validate_version({**payload, "version": value}, 0x560000)
    with pytest.raises(capture.CaptureError):
        capture.validate_version({**payload, "returnAddress": "0x1234"}, 0x560000)


def test_current_executable_and_relocated_signature_are_checked(capture):
    config = capture.capture_config(capture.DEFAULT_EXE)
    assert config["functionBytes"] == list(capture.FUNCTION_BYTES)
    assert len(config["callers"]) == 2
    for relocation in config["relocations"]:
        original = int.from_bytes(capture.FUNCTION_BYTES[relocation["offset"]:relocation["offset"] + 4], "little")
        assert original == 0x400000 + relocation["rva"]
    source = capture.agent_source(config)
    assert "readByteArray(41)" in source
    assert "readUtf8String" not in source


def test_slow_setup_does_not_shorten_the_capture_window(stubbed, tmp_path):
    clock = FakeClock(step=0.1)
    device = FakeDevice([ready(stubbed)])
    clock.slow_setup(device, 5)

    def deliver_version(update):
        if update["event"] == "ready":
            device.session.script.callback(*version(stubbed))

    path = collect(stubbed, tmp_path, device, progress=deliver_version, clock=clock)
    assert json.loads(path.read_text())["version"] == VERSION
    assert device.session.detach_count == 1


def test_version_queued_before_the_deadline_is_saved_when_checked_late(stubbed, tmp_path):
    device = FakeDevice([ready(stubbed), version(stubbed)])
    path = collect(stubbed, tmp_path, device, clock=FakeClock(step=2))
    assert json.loads(path.read_text())["version"] == VERSION
    assert device.session.detach_count == 1


def test_timeout_without_a_version_writes_nothing_and_detaches(stubbed, tmp_path):
    device = FakeDevice([ready(stubbed)])
    with pytest.raises(stubbed.CaptureError, match="resource_capture_timeout"):
        collect(stubbed, tmp_path, device, clock=FakeClock(step=2))
    assert list((tmp_path / "contexts").iterdir()) == []
    assert device.session.detach_count == 1


def test_queue_overflow_is_reported_before_any_queued_version_is_saved(stubbed, tmp_path):
    device = FakeDevice([ready(stubbed)] + [version(stubbed)] * QUEUE_LIMIT)
    with pytest.raises(stubbed.CaptureError, match="resource_queue_overflow"):
        collect(stubbed, tmp_path, device)
    assert list((tmp_path / "contexts").iterdir()) == []
    assert device.session.detach_count == 1
