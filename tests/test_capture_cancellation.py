import json
from pathlib import Path
import queue
import threading

import pytest

from client import capture_content as content
from client import capture_masters as masters
from client import capture_responses as responses
from test_master_capture import FakeClock, FakeDevice
from test_response_collector import body, event, message, ready


def test_receive_until_cancels_before_consuming_a_queued_message():
    events = queue.Queue()
    events.put("pending")
    stop = threading.Event()
    stop.set()
    with pytest.raises(KeyboardInterrupt):
        list(masters.receive_until(events, 100, clock=lambda: 0, stop_event=stop))
    assert events.get_nowait() == "pending"


def test_receive_until_cancels_between_continuously_arriving_messages():
    events = queue.Queue()
    events.put(1)
    stop = threading.Event()
    iterator = masters.receive_until(events, 100, clock=lambda: 0, stop_event=stop)
    assert next(iterator) == 1
    events.put(2)
    stop.set()
    with pytest.raises(KeyboardInterrupt):
        next(iterator)
    assert events.get_nowait() == 2


def test_receive_until_cancellation_also_interrupts_deadline_draining():
    events = queue.Queue()
    events.put(1)
    events.put(2)
    stop = threading.Event()
    iterator = masters.receive_until(events, 0, clock=lambda: 1, stop_event=stop)
    assert next(iterator) == 1
    stop.set()
    with pytest.raises(KeyboardInterrupt):
        next(iterator)
    assert events.get_nowait() == 2


def test_receive_until_without_cancellation_keeps_bounded_deadline_draining():
    events = queue.Queue()
    for index in range(3):
        events.put(index)
    clock = FakeClock(step=1)
    result = []
    for item in masters.receive_until(events, clock.now + 2, clock=clock):
        result.append(item)
        if item < 3:
            events.put(item + 3)
    assert result == [0, 1, 2, 3]
    assert events.qsize() == 2


@pytest.fixture
def identity(tmp_path, monkeypatch):
    path = tmp_path / "Aigis_r.exe"
    path.write_bytes(b"fixture")
    identity = {"path": str(path), "sha256": masters.EXPECTED_SHA256}
    monkeypatch.setattr(masters, "verify_process", lambda *_: identity)
    monkeypatch.setattr(masters, "verify_original_executable", lambda *_: identity)
    return identity


def test_response_cancellation_writes_manifest_and_detaches_after_preserving_data(tmp_path, identity, monkeypatch):
    monkeypatch.setattr(responses, "profile", lambda *_: (identity, {}))
    monkeypatch.setattr(responses, "agent_source", lambda _: "fixture")
    data = body()
    device = FakeDevice([event(ready()), event(message(115, data), data)])
    stop = threading.Event()
    progress = []

    def report(update):
        progress.append(update)
        if update["event"] == "captured":
            stop.set()

    with pytest.raises(KeyboardInterrupt):
        responses.capture_process(device, 42, Path(identity["path"]), tmp_path / "out", 10,
            responses.Selection("all"), progress=report, workspace=tmp_path, stop_event=stop)
    manifest = json.loads(Path(progress[-1]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["records"] == 1
    assert manifest["end_reason"] == "interrupted"
    assert device.session.detach_count == 1


def test_content_cancellation_keeps_bundle_and_detaches(tmp_path, identity, monkeypatch):
    from test_capture_content import FakeDevice as ContentDevice
    from test_capture_content import master_ready, resource_ready

    monkeypatch.setattr(content, "_prepare", lambda _: (identity, {}, {"resources": "resources", "masters": "masters"}))
    device = ContentDevice([resource_ready(), master_ready()])
    stop = threading.Event()

    def report(update):
        if update["event"] == "ready":
            stop.set()

    with pytest.raises(content.ContentCaptureError) as caught:
        content.capture_bundle(device, 42, Path(identity["path"]), tmp_path / "out", 10,
            progress=report, workspace=tmp_path, stop_event=stop)
    manifest = json.loads(caught.value.bundle_path.read_text(encoding="utf-8"))
    assert caught.value.code == "capture_interrupted"
    assert manifest["complete"] is False
    assert device.session.detaches == 1


@pytest.mark.parametrize("collector", ["content", "responses"])
@pytest.mark.parametrize("deadline", [True, "1000", float("nan"), float("inf"), float("-inf")])
def test_invalid_absolute_deadline_is_rejected_before_attachment(tmp_path, identity, collector, deadline):
    device = FakeDevice()
    with pytest.raises(masters.CaptureError, match="invalid_capture_deadline"):
        if collector == "content":
            content.capture_bundle(device, 42, Path(identity["path"]), tmp_path / "out", 10,
                workspace=tmp_path, capture_deadline=deadline)
        else:
            responses.capture_process(device, 42, Path(identity["path"]), tmp_path / "out", 10,
                responses.Selection("all"), workspace=tmp_path, capture_deadline=deadline)
    assert device.attached == []
    assert not (tmp_path / "out").exists()


@pytest.fixture
def forbid_late_wait(monkeypatch):
    original = queue.Queue.get

    def get(events, block=True, timeout=None):
        if block:
            pytest.fail("An expired absolute deadline must not wait for additional events")
        return original(events, block=block, timeout=timeout)

    monkeypatch.setattr(queue.Queue, "get", get)


def test_content_setup_cannot_extend_an_expired_absolute_deadline(tmp_path, identity, monkeypatch, forbid_late_wait):
    from test_capture_content import FakeDevice as ContentDevice
    from test_capture_content import master_ready, resource_ready

    monkeypatch.setattr(content, "_prepare", lambda _: (identity, {}, {"resources": "resources", "masters": "masters"}))
    device = ContentDevice([resource_ready(), master_ready()])
    clock = FakeClock(step=0.1)
    deadline = clock.now + 1
    clock.slow_setup(device, 5)
    with pytest.raises(content.ContentCaptureError) as caught:
        content.capture_bundle(device, 42, Path(identity["path"]), tmp_path / "out", 10,
            workspace=tmp_path, clock=clock, capture_deadline=deadline)
    result = json.loads(caught.value.bundle_path.read_text(encoding="utf-8"))
    assert caught.value.code == "capture_deadline_expired"
    assert result["complete"] is False
    assert device.session.detaches == 1
    assert device.session.loaded == []


def test_response_setup_cannot_extend_an_expired_absolute_deadline(tmp_path, identity, monkeypatch, forbid_late_wait):
    monkeypatch.setattr(responses, "profile", lambda *_: (identity, {}))
    monkeypatch.setattr(responses, "agent_source", lambda _: "fixture")
    device = FakeDevice([event(ready())])
    clock = FakeClock(step=0.1)
    deadline = clock.now + 1
    clock.slow_setup(device, 5)
    progress = []
    with pytest.raises(masters.CaptureError, match="capture_deadline_expired"):
        responses.capture_process(device, 42, Path(identity["path"]), tmp_path / "out", 10,
            responses.Selection("all"), workspace=tmp_path, clock=clock, capture_deadline=deadline,
            progress=progress.append)
    result = json.loads(Path(progress[-1]["manifest"]).read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["error_code"] == "capture_deadline_expired"
    assert device.session.detach_count == 1
    assert device.session.script is None


@pytest.mark.parametrize("collector", ["content", "responses"])
@pytest.mark.parametrize("reason", ["cancelled", "expired"])
def test_cancelled_or_expired_capture_never_attaches_but_preserves_a_manifest(tmp_path, identity, monkeypatch, collector, reason):
    monkeypatch.setattr(content, "_prepare", lambda _: (identity, {}, {"resources": "resources", "masters": "masters"}))
    monkeypatch.setattr(responses, "profile", lambda *_: (identity, {}))
    monkeypatch.setattr(responses, "agent_source", lambda _: "fixture")
    device = FakeDevice()
    stop = threading.Event()
    if reason == "cancelled":
        stop.set()
    options = {"workspace": tmp_path, "stop_event": stop, "clock": lambda: 10,
               "capture_deadline": 20 if reason == "cancelled" else 10}
    if collector == "content":
        with pytest.raises(content.ContentCaptureError) as caught:
            content.capture_bundle(device, 42, Path(identity["path"]), tmp_path / "out", 10, **options)
        path = caught.value.bundle_path
        assert caught.value.code == ("capture_interrupted" if reason == "cancelled" else "capture_deadline_expired")
    else:
        progress = []
        expected = KeyboardInterrupt if reason == "cancelled" else masters.CaptureError
        with pytest.raises(expected):
            responses.capture_process(device, 42, Path(identity["path"]), tmp_path / "out", 10,
                responses.Selection("all"), progress=progress.append, **options)
        path = Path(progress[-1]["manifest"])
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] in ("failed", "stopped")
    assert device.attached == []
    assert device.session.detach_count == 0
