import importlib.util
import json
from pathlib import Path
import queue
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from aigis_ps import alxml
from aigis_ps import public_tables


_SCRIPT = Path(__file__).resolve().parents[1] / "client" / "capture_masters.py"
_SPEC = importlib.util.spec_from_file_location("capture_masters", _SCRIPT)
capture = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(capture)


@pytest.fixture
def schemas():
    return {name: set(table["required"]) | {"OptionalPublic"} for name, table in capture.TABLES.items()}


def master_xml(name, *, rows=(1, 2), extra=()):
    fields = sorted(capture.TABLES[name]["required"])
    columns = tuple(alxml.Column(field, "I", rows) for field in fields) + tuple(extra)
    return alxml.dumps(alxml.Container(children=columns))


def ready(base=0x600000):
    return {"event": "ready", "base": hex(base), "functionAddress": hex(base + capture.FUNCTION_RVA), "pointerSize": 4}


def master_message(name, data, base=0x600000):
    return {"event": "master", "table": name, "length": len(data),
            "returnAddress": hex(base + capture.TABLES[name]["callRva"] + 5),
            "functionAddress": hex(base + capture.FUNCTION_RVA)}


@pytest.fixture
def executable(tmp_path, monkeypatch):
    path = tmp_path / "Aigis_r.exe"
    path.write_bytes(b"fixture")
    identity = {"path": str(path), "sha256": capture.EXPECTED_SHA256}
    monkeypatch.setattr(capture, "verify_original_executable", lambda candidate: identity)
    return path, identity


class FakeClock:
    def __init__(self, step):
        self.now = 1000.0
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now

    def slow(self, function, seconds):
        def call(*args, **kwargs):
            self.now += seconds
            return function(*args, **kwargs)
        return call

    def slow_setup(self, device, seconds):
        create_script = device.session.create_script

        def create(source):
            script = create_script(source)
            script.load = self.slow(script.load, seconds)
            return script

        device.attach = self.slow(device.attach, seconds)
        device.session.create_script = create


class FakeScript:
    def __init__(self, events):
        self.events = events
        self.callback = None

    def on(self, signal, callback):
        assert signal == "message"
        self.callback = callback

    def load(self):
        for message, data in self.events:
            self.callback(message, data)


class FakeSession:
    def __init__(self, events):
        self.events = events
        self.detach_count = 0
        self.source = None
        self.script = None

    def on(self, signal, callback):
        assert signal == "detached"

    def create_script(self, source):
        self.source = source
        self.script = FakeScript(self.events)
        return self.script

    def detach(self):
        self.detach_count += 1


class FakeDevice:
    def __init__(self, events=(), processes=()):
        self.session = FakeSession(events)
        self.processes = processes
        self.attached = []

    def attach(self, pid):
        self.attached.append(pid)
        return self.session

    def enumerate_processes(self, **kwargs):
        assert kwargs == {"scope": "minimal"}
        return self.processes


def events_for_both_tables():
    events = [({"type": "send", "payload": ready()}, None)]
    for name in capture.TABLES:
        data = master_xml(name)
        events.append(({"type": "send", "payload": master_message(name, data)}, data))
    return events


def test_verifier_rejects_modified_executable_before_pe_parsing(tmp_path):
    path = tmp_path / "Aigis_r.exe"
    path.write_bytes(b"patched client")
    with pytest.raises(capture.CaptureError, match="executable_hash_mismatch"):
        capture.verify_original_executable(path)


def test_only_public_flat_columns_with_matching_rows_are_accepted(schemas):
    result = capture.validate_master("CardData", master_xml("CardData"), schemas)
    assert result["rows"] == 2
    assert result["missing_schema_columns"] == ["OptionalPublic"]
    assert not result["full_schema"]
    with pytest.raises(capture.CaptureError, match="non_public_table_columns"):
        capture.validate_master("CardData", master_xml("CardData", extra=(alxml.Column("UK", "S", ("private", "private")),)), schemas)
    with pytest.raises(capture.CaptureError, match="duplicate_master_ids"):
        capture.validate_master("CardData", master_xml("CardData", rows=(1, 1)), schemas)
    with pytest.raises(capture.CaptureError, match="invalid_master_row_count"):
        capture.validate_master("CardData", master_xml("CardData", rows=()), schemas)
    extra = alxml.Column("OptionalPublic", "I", (1,))
    with pytest.raises(capture.CaptureError, match="invalid_master_row_count"):
        capture.validate_master("CardData", master_xml("CardData", extra=(extra,)), schemas)


def test_account_containers_and_unsafe_xml_are_rejected(schemas):
    nested = alxml.dumps(alxml.Container(children=(alxml.Container("ACCOUNT"),)))
    with pytest.raises(capture.CaptureError, match="non_public_table_shape"):
        capture.validate_master("CardData", nested, schemas)
    with pytest.raises(capture.CaptureError, match="invalid_master_xml"):
        capture.validate_master("CardData", b'<!DOCTYPE DA [<!ENTITY x "bad">]><DA/>', schemas)


def test_shared_validator_returns_document_and_compatible_metadata(schemas):
    data = master_xml("CardData")
    collections = {name: tuple(fields) for name, fields in schemas.items()}
    document, metadata = public_tables.validate_public_master("CardData", data, collections)
    assert isinstance(document, alxml.Container)
    assert metadata == capture.validate_master("CardData", data, schemas)
    assert metadata["rows"] == 2
    assert metadata["missing_schema_columns"] == ["OptionalPublic"]
    assert capture.TABLES["CardData"]["required"] is public_tables.REQUIRED_FIELDS["CardData"]
    assert capture.MAX_BYTES == public_tables.MASTER_LIMITS.max_bytes


@pytest.mark.parametrize("case,code", [
    ("int64_id", "invalid_master_ids"),
    ("zero_id", "invalid_master_ids"),
    ("duplicate_column", "duplicate_master_columns"),
    ("missing_required", "non_public_table_columns"),
    ("unknown_column", "non_public_table_columns"),
    ("property_column", "unsupported_master_type"),
])
def test_shared_validator_and_collector_reject_the_same_contract_violations(schemas, case, code):
    columns = [alxml.Column(name, "I", (1, 2)) for name in sorted(public_tables.REQUIRED_FIELDS["CardData"])]
    if case == "int64_id":
        columns = [alxml.Column(column.name, "I64" if column.name == "CardID" else "I", column.values) for column in columns]
    elif case == "zero_id":
        columns = [alxml.Column(column.name, "I", (0, 2) if column.name == "CardID" else column.values) for column in columns]
    elif case == "duplicate_column":
        columns.append(columns[0])
    elif case == "missing_required":
        columns = [column for column in columns if column.name == "CardID"]
    elif case == "unknown_column":
        columns.append(alxml.Column("PlayerPassword", "S", ("dummy", "dummy")))
    elif case == "property_column":
        columns.append(alxml.Column("OptionalPublic", "P", ("dummy", "dummy")))
    data = alxml.dumps(alxml.Container(children=tuple(columns)))
    with pytest.raises(public_tables.PublicTableError) as shared:
        public_tables.validate_public_master("CardData", data, schemas)
    with pytest.raises(capture.CaptureError) as wrapped:
        capture.validate_master("CardData", data, schemas)
    assert shared.value.code == wrapped.value.code == code
    assert isinstance(wrapped.value.__cause__, public_tables.PublicTableError)


@pytest.mark.parametrize("rows,accepted", [(65535, True), (65536, False)])
def test_shared_native_row_count_boundary(schemas, monkeypatch, rows, accepted):
    values = tuple(range(1, rows + 1))
    document = alxml.Container(children=tuple(alxml.Column(name, "I", values) for name in public_tables.REQUIRED_FIELDS["CardData"]))
    monkeypatch.setattr(public_tables, "loads", lambda data, *, limits: document)
    if accepted:
        assert public_tables.validate_public_master("CardData", b"<DA/>", schemas)[1]["rows"] == rows
    else:
        with pytest.raises(public_tables.PublicTableError, match="invalid_master_row_count"):
            public_tables.validate_public_master("CardData", b"<DA/>", schemas)


def test_shared_limits_and_invalid_schema_contract():
    assert vars(public_tables.MASTER_LIMITS) == {
        "max_bytes": 32 * 1024 * 1024, "max_depth": 4, "max_elements": 2_000_000,
        "max_values": 1_500_000, "max_text_bytes": 1024 * 1024,
    }
    for schemas in ({}, {"CardData": "CardID"}, {"CardData": ["CardID"]}, [], None):
        with pytest.raises(public_tables.PublicTableError, match="schema_mismatch"):
            public_tables.validate_public_master("CardData", master_xml("CardData"), schemas)


def test_writer_validates_before_creating_payload_and_never_overwrites(tmp_path, schemas, executable):
    _, identity = executable
    writer = capture.CaptureWriter(tmp_path / "out", identity, 42, schemas, workspace=tmp_path)
    writer.ready(ready())
    data = master_xml("CardData")
    message = master_message("CardData", data)
    wrong = dict(message, returnAddress="0x1234")
    with pytest.raises(capture.CaptureError, match="agent_address_mismatch"):
        writer.record(wrong, data)
    assert not (writer.directory / "CardData.xml").exists()
    writer.record(message, data)
    with pytest.raises(capture.CaptureError, match="unexpected_master_message"):
        writer.record(message, data)
    assert (writer.directory / "CardData.xml").read_bytes() == data
    path = writer.finish("incomplete")
    manifest = json.loads(path.read_text())
    assert manifest["missing_tables"] == ["QuestTable"]
    assert manifest["source_executable"]["sha256"] == capture.EXPECTED_SHA256
    with pytest.raises(capture.CaptureError, match="manifest_already_written"):
        writer.finish("complete")


def test_capture_detaches_after_two_valid_messages(tmp_path, schemas, executable, capsys):
    path, _ = executable
    device = FakeDevice(events_for_both_tables())
    output = capture.capture_process(device, 123, path, tmp_path / "captures", schemas, 1,
                                     path_query=lambda _: path, workspace=tmp_path)
    manifest = json.loads(output.read_text())
    assert manifest["status"] == "complete"
    assert manifest["missing_tables"] == []
    assert manifest["complete"] is True
    assert manifest["schema_version"] == 1
    assert manifest["input_sha256"] == capture.EXPECTED_SHA256
    assert set(manifest["tables"]) == {"CardData", "QuestTable"}
    assert manifest["tables"]["CardData"]["path"] == "CardData.xml"
    assert device.attached == [123]
    assert device.session.detach_count == 1
    assert capsys.readouterr().out == ""


def test_capture_reports_only_safe_progress_metadata(tmp_path, schemas, executable):
    path, _ = executable
    device = FakeDevice(events_for_both_tables())
    updates = []
    capture.capture_process(device, 123, path, tmp_path / "captures", schemas, 1,
                            path_query=lambda _: path, workspace=tmp_path, progress=updates.append)
    assert updates == [
        {"event": "ready", "pid": 123},
        {"event": "captured", "table": "CardData", "bytes": len(master_xml("CardData")), "rows": 2},
        {"event": "captured", "table": "QuestTable", "bytes": len(master_xml("QuestTable")), "rows": 2},
    ]
    assert device.session.detach_count == 1


def test_capture_rejects_process_path_before_attach(tmp_path, schemas, executable):
    path, _ = executable
    device = FakeDevice()
    with pytest.raises(capture.CaptureError, match="process_path_mismatch"):
        capture.capture_process(device, 123, path, tmp_path / "out", schemas, 1,
                                path_query=lambda _: tmp_path / "patched" / "Aigis_r.exe", workspace=tmp_path)
    assert device.attached == []
    assert not (tmp_path / "out").exists()


def test_process_identity_is_checked_again_after_attach(tmp_path, schemas, executable):
    path, _ = executable
    device = FakeDevice()
    replies = iter((path, tmp_path / "different.exe"))
    with pytest.raises(capture.CaptureError, match="process_path_mismatch"):
        capture.capture_process(device, 123, path, tmp_path / "out", schemas, 1,
                                path_query=lambda _: next(replies), workspace=tmp_path)
    assert device.session.source is None
    assert device.session.detach_count == 1


def test_agent_errors_do_not_write_error_payload_or_sensitive_details(tmp_path, schemas, executable):
    path, _ = executable
    device = FakeDevice([({"type": "error", "description": "secret-login-token"}, None)])
    with pytest.raises(capture.CaptureError, match="agent_runtime_error"):
        capture.capture_process(device, 123, path, tmp_path / "out", schemas, 1,
                                path_query=lambda _: path, workspace=tmp_path)
    files = list((tmp_path / "out").rglob("*"))
    assert [file.name for file in files if file.is_file()] == ["manifest.json"]
    assert "secret-login-token" not in next((tmp_path / "out").rglob("manifest.json")).read_text()
    assert device.session.detach_count == 1


def test_timeout_retains_only_valid_partial_capture(tmp_path, schemas, executable):
    path, _ = executable
    device = FakeDevice(events_for_both_tables()[:2])
    with pytest.raises(capture.CaptureError, match="capture_timeout"):
        capture.capture_process(device, 123, path, tmp_path / "out", schemas, 1,
                                path_query=lambda _: path, workspace=tmp_path, clock=FakeClock(step=1))
    manifest_path = next((tmp_path / "out").rglob("manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["error_code"] == "capture_timeout"
    assert manifest["missing_tables"] == ["QuestTable"]
    assert device.session.detach_count == 1


def test_messages_queued_before_the_deadline_complete_a_late_checked_capture(tmp_path, schemas, executable):
    path, _ = executable
    device = FakeDevice(events_for_both_tables())
    output = capture.capture_process(device, 123, path, tmp_path / "captures", schemas, 1,
                                     path_query=lambda _: path, workspace=tmp_path, clock=FakeClock(step=2))
    assert json.loads(output.read_text())["complete"] is True
    assert device.session.detach_count == 1


def test_slow_setup_does_not_shorten_the_capture_window(tmp_path, schemas, executable):
    path, _ = executable
    clock = FakeClock(step=0.1)
    ready_event, *tables = events_for_both_tables()
    device = FakeDevice([ready_event])
    clock.slow_setup(device, 5)

    def deliver_tables(update):
        if update["event"] == "ready":
            for message, data in tables:
                device.session.script.callback(message, data)

    output = capture.capture_process(device, 123, path, tmp_path / "captures", schemas, 1,
                                     path_query=lambda _: path, workspace=tmp_path, progress=deliver_tables, clock=clock)
    assert json.loads(output.read_text())["complete"] is True
    assert device.session.detach_count == 1


def test_receive_until_drains_only_events_already_queued_at_the_deadline():
    events = queue.Queue()
    for index in range(3):
        events.put(index)
    clock = FakeClock(step=1)
    received = []
    for event in capture.receive_until(events, clock.now + 2, clock=clock):
        received.append(event)
        if event < 3:
            events.put(event + 3)
    assert received == [0, 1, 2, 3]
    assert events.qsize() == 2


def test_check_never_imports_frida_or_creates_capture_directory(tmp_path, schemas, executable, monkeypatch, capsys):
    path, _ = executable
    monkeypatch.setattr(capture, "load_public_schemas", lambda _: schemas)
    monkeypatch.setattr(capture, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(capture.importlib, "import_module", lambda _: pytest.fail("Frida must not be loaded"))
    out = tmp_path / "out"
    assert capture.main(["--check", "--exe", str(path), "--output", str(out)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["attached"] is False
    assert not out.exists()


def test_queue_overflow_is_an_explicit_failure(tmp_path, schemas, executable):
    path, _ = executable
    device = FakeDevice([({"type": "send", "payload": ready()}, None)] * 10)
    with pytest.raises(capture.CaptureError, match="event_queue_overflow"):
        capture.capture_process(device, 123, path, tmp_path / "out", schemas, 1,
                                path_query=lambda _: path, workspace=tmp_path)
    assert device.session.detach_count == 1


@pytest.mark.parametrize("value", ["0x-1", "0x+1", "0x", "0x100000000", 0])
def test_agent_addresses_must_be_unsigned_32_bit_hex(value):
    with pytest.raises(capture.CaptureError, match="invalid_agent_address"):
        capture._address(value)


def test_wait_only_enumerates_minimal_processes_and_checks_path(executable):
    path, _ = executable
    processes = (SimpleNamespace(pid=1, name="other.exe"), SimpleNamespace(pid=2, name="Aigis_r.exe"))
    device = FakeDevice(processes=processes)
    assert capture.wait_for_process(device, path, 0.1, path_query=lambda _: path) == 2
    assert device.attached == []


def test_wait_ignores_existing_pids_before_inspecting_process_images(executable):
    path, _ = executable
    device = FakeDevice(processes=(SimpleNamespace(pid=2, name="Aigis_r.exe"), SimpleNamespace(pid=3, name="Aigis_r.exe")))
    inspected = []

    def image(pid):
        inspected.append(pid)
        return path

    assert capture.wait_for_process(device, path, 0.1, path_query=image, excluded_pids={2}) == 3
    assert inspected == [3]
    assert device.attached == []


def test_wait_inspects_running_processes_before_its_budget_can_expire(executable):
    path, _ = executable
    device = FakeDevice(processes=(SimpleNamespace(pid=2, name="Aigis_r.exe"),))
    assert capture.wait_for_process(device, path, 0, path_query=lambda _: path) == 2
    with pytest.raises(capture.CaptureError, match="process_wait_timeout"):
        capture.wait_for_process(FakeDevice(), path, 0, path_query=lambda _: path)


def test_cli_wait_takes_pid_snapshot_before_waiting(tmp_path, schemas, executable, monkeypatch, capsys):
    path, _ = executable
    device = FakeDevice(processes=(SimpleNamespace(pid=2, name="Aigis_r.exe"), SimpleNamespace(pid=7, name="other.exe")))
    monkeypatch.setattr(capture, "load_public_schemas", lambda _: schemas)
    monkeypatch.setattr(capture.importlib, "import_module", lambda _: SimpleNamespace(get_local_device=lambda: device))
    observed = []

    def wait(device_arg, expected, timeout, *, excluded_pids):
        observed.append((device_arg, expected, excluded_pids))
        return 3

    def collect(device_arg, pid, expected, output, schema_arg, timeout, *, progress):
        assert device_arg is device and pid == 3
        progress({"event": "ready", "pid": pid})
        return tmp_path / "manifest.json"

    monkeypatch.setattr(capture, "wait_for_process", wait)
    monkeypatch.setattr(capture, "capture_process", collect)
    assert capture.main(["--wait", "--exe", str(path)]) == 0
    assert observed == [(device, path, {2, 7})]
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0] == {"event": "waiting_for_process", "process": "Aigis_r.exe"}
    assert lines[1] == {"event": "ready", "pid": 3}
    assert lines[2]["status"] == "complete"


def test_output_must_remain_in_workspace(tmp_path, executable, schemas):
    _, identity = executable
    with pytest.raises(capture.CaptureError, match="output_outside_workspace"):
        capture.CaptureWriter(tmp_path.parent / "outside", identity, 1, schemas, workspace=tmp_path)


def test_windows_image_query_uses_limited_information_without_command_line(monkeypatch):
    calls = []

    class Function:
        def __init__(self, function):
            self.function = function

        def __call__(self, *args):
            return self.function(*args)

    def opened(access, inherit, pid):
        calls.append((access, inherit, pid))
        return 99

    def queried(handle, flags, buffer, length):
        buffer.value = "E:\\original\\Aigis_r.exe"
        return True

    kernel = SimpleNamespace(OpenProcess=Function(opened), QueryFullProcessImageNameW=Function(queried),
                             CloseHandle=Function(lambda handle: calls.append(("close", handle))))
    monkeypatch.setattr(capture.sys, "platform", "win32")
    monkeypatch.setattr(capture.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    assert str(capture.query_process_image(12)) == "E:\\original\\Aigis_r.exe"
    assert calls == [(0x1000, False, 12), ("close", 99)]


@pytest.mark.parametrize("scenario", ["valid", "bad_function", "bad_callsite", "invalid_length"])
def test_javascript_filters_callers_before_reading_arguments(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the isolated JavaScript harness")
    identity = {"path": "E:\\original\\Aigis_r.exe", "sha256": capture.EXPECTED_SHA256}
    source = capture.agent_source(identity)
    harness = r'''
const vm = require('node:vm');
const source = SOURCE;
const scenario = SCENARIO;
const base = 0x600000;
const memory = new Map();
const events = [];
let callback = null;
let detached = 0;
let reads = 0;
class Pointer {
    constructor(value) { this.value = value; }
    add(value) { return new Pointer(this.value + value); }
    toString() { return '0x' + this.value.toString(16); }
    toUInt32() { return this.value >>> 0; }
    isNull() { return this.value === 0; }
    readByteArray(length) {
        reads++;
        const bytes = memory.get(this.value);
        if (bytes === undefined) throw new Error('unregistered memory');
        return Uint8Array.from(bytes.slice(0, length)).buffer;
    }
}
const config = JSON.parse(source.split('const CAPTURE_CONFIG = ')[1].split(';\n')[0]);
memory.set(base + config.functionRva, scenario === 'bad_function' ? [0] : config.functionBytes);
for (const table of Object.values(config.tables)) memory.set(base + table.callRva, table.callBytes);
if (scenario === 'bad_callsite') memory.set(base + config.tables.CardData.callRva, [0]);
vm.runInNewContext(source, {
    Process: { platform: 'windows', arch: 'ia32', pointerSize: 4,
               mainModule: { base: new Pointer(base), path: config.exePath } },
    Interceptor: { attach(target, handlers) { callback = handlers.onEnter; return { detach() { detached++; } }; } },
    send(message, data) { events.push({ message, bytes: data === undefined ? null : Array.from(new Uint8Array(data)) }); }
});
if (callback !== null) {
    const neverRead = new Proxy({}, { get() { throw new Error('unrelated arguments read'); } });
    callback.call({ returnAddress: new Pointer(0x1234) }, neverRead);
    let index = 0;
    for (const table of Object.values(config.tables)) {
        const pointer = new Pointer(0x1000 + index * 16);
        memory.set(pointer.value, [60, 68, 65, 62]);
        const context = { returnAddress: new Pointer(base + table.callRva + 5) };
        callback.call(context, [pointer, new Pointer(scenario === 'invalid_length' ? config.maxBytes + 1 : 4)]);
        callback.call(context, neverRead);
        index++;
    }
}
console.log(JSON.stringify({ events, detached, reads, attached: callback !== null }));
'''.replace("SOURCE", json.dumps(source)).replace("SCENARIO", json.dumps(scenario))
    result = subprocess.run([node], input=harness, text=True, capture_output=True, check=True)
    observed = json.loads(result.stdout)
    if scenario in ("bad_function", "bad_callsite"):
        assert not observed["attached"]
        assert observed["events"] == [{"message": {"event": "error", "code": "signature_mismatch"}, "bytes": None}]
    elif scenario == "invalid_length":
        assert observed["events"][-1]["message"] == {"event": "error", "code": "invalid_length"}
        assert not any(entry["message"]["event"] == "master" for entry in observed["events"])
        assert observed["detached"] == 1
    else:
        masters = [entry for entry in observed["events"] if entry["message"]["event"] == "master"]
        assert [entry["message"]["table"] for entry in masters] == ["CardData", "QuestTable"]
        assert observed["reads"] == 5
        assert observed["detached"] == 1
