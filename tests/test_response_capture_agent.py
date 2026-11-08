import json
from pathlib import Path
import shutil
import subprocess

import pytest

from client import capture_responses
from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.response_capture import ResponseCaptureError, filter_rules, parse_response, sanitize_response


CLIENT = Path(__file__).resolve().parents[1] / "client"
BASE = 0x600000
DEFAULT = 0x6801D0
UNIT_CONSUMER, UNIT_VTABLE = 0x65B420, 0x9D18D8
LOGIN_VTABLE = 0xA68F98
SERVANT_VTABLE = 0xA6A2A8
CONFIG = {
    "exePath": "E:\\original\\Aigis_r.exe", "returnRva": 0x68060A, "maxBytes": 8 * 1024 * 1024,
    "blocks": [{"rva": DEFAULT, "bytes": [0x55, 0x8B, 0xEC]}, {"rva": UNIT_CONSUMER, "bytes": [0x55, 0x8B, 0xEC, 0x6A]}],
    "vtables": [{"rva": UNIT_VTABLE, "slots": [{"offset": 4, "rva": 0x65AC40}, {"offset": 0x58, "rva": UNIT_CONSUMER}]},
                {"rva": LOGIN_VTABLE, "slots": [{"offset": 4, "rva": 0x6649F0}, {"offset": 0x58, "rva": DEFAULT}]}],
    "consumers": [{"rva": DEFAULT}, {"rva": UNIT_CONSUMER}],
    "excludedOperations": [31, 152], "excludedVtables": [LOGIN_VTABLE], "operations": None, "rules": filter_rules(),
}


HARNESS = r"""
const vm = require('node:vm');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
if (input.mode === 'filter') {
    const context = vm.createContext({});
    vm.runInContext(input.filter, context, {timeout: 5000});
    const results = input.cases.map(xml => {
        try { return {ok: true, value: context.filterResponseXml(xml, input.config.rules)}; }
        catch (error) { return {ok: false, error: String(error.message)}; }
    });
    process.stdout.write(JSON.stringify({results}));
} else {
    const config = input.config, base = input.base;
    const memory = new Map(), events = [], reads = [], argumentReads = [], handlers = new Map();
    let attached = 0, detached = 0, current = -1, allocation = 0x70000000;
    class Pointer {
        constructor(value, role = 'other') { this.value = value; this.role = role; }
        add(value) { return new Pointer(this.value + value, this.role); }
        equals(other) { return other instanceof Pointer && other.value === this.value; }
        isNull() { return this.value === 0; }
        toString() { return '0x' + this.value.toString(16); }
        toUInt32() { return this.value >>> 0; }
        read(method, length) {
            reads.push({call: current, address: this.value, role: this.role, method, length});
            return memory.get(this.value);
        }
        readU32() { const value = this.read('u32'); if (typeof value !== 'number') throw new Error('unreadable'); return value >>> 0; }
        readPointer() { const value = this.read('pointer'); if (!(value instanceof Pointer)) throw new Error('unreadable'); return value; }
        readByteArray(length) {
            const value = this.read('bytes', length);
            if (!Buffer.isBuffer(value) || length > value.length) throw new Error('unreadable');
            return Uint8Array.from(value.subarray(0, length)).buffer;
        }
        readUtf8String(length) {
            const value = this.read('string', length);
            if (!Buffer.isBuffer(value) || length > value.length) throw new Error('unreadable');
            return value.subarray(0, length).toString('utf8');
        }
    }
    const tablePointer = (rva, role) => new Pointer(base + rva, role);
    for (const block of config.blocks) {
        const bytes = Buffer.from(block.bytes);
        for (const relocation of block.relocations || []) bytes.writeUInt32LE((base + relocation.rva) >>> 0, relocation.offset);
        memory.set(base + block.rva, bytes);
    }
    for (const table of config.vtables) {
        for (const slot of table.slots) memory.set(base + table.rva + slot.offset, new Pointer(base + slot.rva));
    }
    const setup = input.setup || {};
    if (setup.badBlock !== undefined) memory.set(base + config.blocks[setup.badBlock].rva, Buffer.from([0, 0, 0, 0]));
    if (setup.corruptByte !== undefined) {
        const change = setup.corruptByte;
        memory.get(base + config.blocks[change.block].rva)[change.offset] ^= 0x80;
    }
    if (setup.badVtable !== undefined) memory.set(base + config.vtables[setup.badVtable].rva + 0x58, new Pointer(0));
    const source = input.source || ('const RESPONSE_CAPTURE_CONFIG = ' + JSON.stringify(config) + ';\n' + input.signature + '\n' + input.filter + '\n' + input.agent);
    vm.runInNewContext(source, {
        Process: {platform: setup.platform || 'windows', arch: setup.arch || 'ia32', pointerSize: setup.pointerSize || 4,
                  mainModule: {base: new Pointer(base, 'module'), path: setup.path || config.exePath}},
        Interceptor: {attach(target, callbacks) {
            attached++;
            handlers.set(target.value, callbacks.onEnter);
            return {detach() { detached++; }};
        }},
        Memory: {allocUtf8String(text) {
            const pointer = new Pointer(allocation, 'output');
            memory.set(allocation, Buffer.concat([Buffer.from(text), Buffer.from([0])]));
            allocation += Buffer.byteLength(text) + 64;
            return pointer;
        }},
        send(message, data) { events.push({message, bytes: data === undefined ? null : Array.from(new Uint8Array(data))}); }
    }, {timeout: 5000});
    for (const [index, item] of (input.calls || []).entries()) {
        current = index;
        const consumer = item.consumer === undefined ? 0x6801D0 : item.consumer;
        const callback = handlers.get(base + consumer);
        if (callback === undefined) continue;
        const reactor = new Pointer(0x50000000 + index * 0x100, 'reactor');
        const value = new Pointer(0x51000000 + index * 0x100, 'value');
        const data = new Pointer(0x52000000 + index * 0x100, 'payload');
        const vtableRva = item.vtable === undefined ? 0xA6A2A8 : item.vtable;
        const body = item.rawBytes ? Buffer.from(item.rawBytes) : Buffer.from(item.xml === undefined ? '<DA><PPU/></DA>' : item.xml);
        const length = item.length === undefined ? body.length : item.length;
        const capacity = item.capacity === undefined ? Math.max(16, length) : item.capacity;
        memory.set(reactor.value + 0x6C, item.operation === undefined ? 12 : item.operation);
        memory.set(reactor.value, tablePointer(vtableRva, 'vtable'));
        if (!config.vtables.some(table => table.rva === vtableRva)) {
            memory.set(base + vtableRva + 0x58, new Pointer(base + (item.slot === undefined ? consumer : item.slot)));
        }
        memory.set(value.value + 0x10, length);
        memory.set(value.value + 0x14, capacity);
        if (capacity < 16) memory.set(value.value, body);
        else { memory.set(value.value, item.nullData ? new Pointer(0) : data); memory.set(data.value, body); }
        if (item.fault === 'length') memory.delete(value.value + 0x10);
        if (item.fault === 'data') memory.delete(capacity < 16 ? value.value : data.value);
        const args = new Proxy([value], {get(target, property) {
            argumentReads.push({call: index, property: String(property)});
            return target[property];
        }});
        const returnRva = item.returnRva === undefined ? config.returnRva : item.returnRva;
        callback.call({context: {ecx: reactor}, returnAddress: new Pointer(base + returnRva)}, args);
    }
    process.stdout.write(JSON.stringify({events, reads, argumentReads, attached, detached, hooks: Array.from(handlers.keys())}));
}
"""


def run_node(*, calls=(), setup=None, config=None, cases=None, source=None, base=BASE):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the isolated capture-agent harness")
    payload = {"mode": "filter" if cases is not None else "agent", "config": config or CONFIG, "base": base,
               "calls": calls, "setup": setup or {}, "cases": cases, "source": source,
               "signature": (CLIENT / "capture_signature.js").read_text("utf-8"),
               "agent": (CLIENT / "capture_responses.js").read_text("utf-8"),
               "filter": (CLIENT / "capture_response_filter.js").read_text("utf-8")}
    result = subprocess.run([node, "-e", HARNESS], input=json.dumps(payload), text=True, encoding="utf-8",
                            capture_output=True, check=True, timeout=30, cwd=CLIENT.parent)
    return json.loads(result.stdout)


def responses(result):
    return [event for event in result["events"] if event["message"]["event"] == "response"]


def transmitted(result):
    return json.dumps(result["events"], ensure_ascii=False) + "".join(
        bytes(event["bytes"]).decode("utf-8") for event in result["events"] if event["bytes"] is not None)


def call_reads(result, index=0):
    return [(read["role"], read["method"]) for read in result["reads"] if read["call"] == index]


LOGIN = dumps(Container(children=(Container("LGI", tuple(
    Column(f"A{index + 1}", "S" if index in (6, 9) else "I", (value,)) for index, value in enumerate(
        (1, 900000123, 0, 7, 7, 900000123, "private-user-key", 0, 0, "900000123", 0, 0, 0)))),))).decode()
PARITY = [
    '<DA><SYSTEM><A1 T="S"><V>ExchangeKey</V></A1><A2 T="S"><V>private-1</V></A2></SYSTEM><PPU><ID T="I"><V>1</V></ID></PPU></DA>',
    '<DA><UK T="S"><V>private-2</V></UK><Result T="I"><V>0</V></Result><C N="UserKey[0]"><X T="S"><V>private-3</V></X></C></DA>',
    LOGIN,
    '<DA><History>' + LOGIN[LOGIN.index("<LGI>"):LOGIN.index("</DA>")] + '</History></DA>',
    '<DA><PGP2><A1 T="S"><V>C1</V><V>Exchange&#75;ey</V><V>LastLoginDate</V><V><![CDATA[U_K]]></V></A1>'
    '<A2 T="S"><V>5</V><V>private-4</V><V>20261106</V><V>private-5</V></A2></PGP2></DA>',
    '<DA><PGP2><A1 T="S"><V>MK</V></A1><A2 T="S"><V>private-6</V><V>extra</V></A2></PGP2><Keep/></DA>',
    '<?xml version="1.0"?>\n<DA>\n  <!--x--><PSC><Name T="S"><V><![CDATA[<pack> & more]]></V><V>召喚 &amp; 光 &#x1F31F;</V></Name></PSC>'
    '<C N="_1Prop" T="P" I="3"><V>x</V></C><Empty/><Self T="I"/></DA>',
    '<DA/>', '<DA></DA>', '<DA><Rows><Session-ID T="S"><V>private-7</V></Session-ID><Deep><Gpvi T="S"><V>private-8</V></Gpvi></Deep></Rows></DA>',
]
INVALID = [
    '<!DOCTYPE DA [<!ENTITY x "y">]><DA/>', '<Root/>', '<DA N="x"/>', '<DA/><DA/>', '<DA><G>', '<DA><G bad="1"/></DA>',
    '<DA><G N="a&amp;b"/></DA>', '<DA><Count T="I" N="Other"><V>1</V></Count></DA>', '<DA><G>text</G></DA>',
    '<DA><G T="I">text<V>1</V></G></DA>', '<DA><G T="I"><V N="x">1</V></G></DA>', '<DA><G T="I"><V><W/></V></G></DA>',
    '<DA><G T="S"><V>&#1;</V></G></DA>', '<DA><G T="S"><V>\x01</V></G></DA>', '<DA><A1 T="S"><V>UK</V></A1><G/></DA>',
    '<DA><G T="S"><V>a & b</V></G></DA>', '<DA><G T="S"><V>&nbsp;</V></G></DA>',
    '<DA>' + '<G>' * 32 + '</G>' * 32 + '</DA>',
]


def test_filter_matches_the_python_sanitizer_case_by_case():
    results = run_node(cases=PARITY)["results"]
    for raw, result in zip(PARITY, results):
        expected, report = sanitize_response(raw.encode())
        assert result["ok"], raw
        assert parse_response(result["value"]["xml"].encode()) == expected
        assert result["value"]["removed"] == report["removed"] and result["value"]["removedRows"] == report["removed_rows"]
        assert result["value"]["groups"] == [node.name for node in expected.children]
        assert "private-" not in result["value"]["xml"] and "900000123" not in result["value"]["xml"]


def test_filter_and_python_reject_the_same_malformed_documents():
    for raw, result in zip(INVALID, run_node(cases=INVALID)["results"]):
        assert result == {"ok": False, "error": "invalid_response_xml"}, raw
        with pytest.raises(ResponseCaptureError):
            sanitize_response(raw.encode())


@pytest.mark.parametrize("setup,code", [
    ({"platform": "linux"}, "unsupported_process"), ({"arch": "x64"}, "unsupported_process"),
    ({"pointerSize": 8}, "unsupported_process"), ({"path": "E:\\other\\Aigis_r.exe"}, "unsupported_process"),
    ({"badBlock": 0}, "signature_mismatch"), ({"badBlock": 1}, "signature_mismatch"),
    ({"badVtable": 0}, "vtable_mismatch"), ({"badVtable": 1}, "vtable_mismatch"),
])
def test_invalid_process_or_signatures_never_attach(setup, code):
    result = run_node(setup=setup, calls=[{}])
    assert result["attached"] == 0 and result["argumentReads"] == []
    assert result["events"] == [{"message": {"event": "error", "code": code}, "bytes": None}]


def test_every_consumer_is_hooked_and_reported():
    result = run_node()
    assert sorted(result["hooks"]) == sorted(BASE + item["rva"] for item in CONFIG["consumers"])
    assert result["events"][0]["message"]["consumers"] == [hex(BASE + item["rva"]) for item in CONFIG["consumers"]]


DISPATCH_READS = [("reactor", "u32"), ("reactor", "pointer"), ("vtable", "pointer")]


@pytest.mark.parametrize("call,expected", [
    ({"returnRva": 0x1234}, []),
    ({"slot": 0x6801D1}, DISPATCH_READS),
    ({"vtable": UNIT_VTABLE}, DISPATCH_READS),
])
def test_return_address_and_dispatch_slot_gates_precede_argument_reads(call, expected):
    result = run_node(calls=[call])
    assert call_reads(result) == expected
    assert result["argumentReads"] == [] and not responses(result)
    assert [event["message"]["event"] for event in result["events"]] == ["ready"]


@pytest.mark.parametrize("call", [{"operation": 152}, {"operation": 31}, {"operation": 115, "vtable": LOGIN_VTABLE}])
def test_excluded_operations_and_reactors_are_counted_without_reading_the_response(call):
    result = run_node(calls=[call])
    assert result["argumentReads"] == [] and not responses(result)
    assert result["events"][-1] == {"message": {"event": "excluded", "operation": call["operation"]}, "bytes": None}


def test_selected_operations_skip_other_responses_without_reading_them():
    result = run_node(config={**CONFIG, "operations": [41]}, calls=[{"operation": 12},
        {"operation": 41, "consumer": UNIT_CONSUMER, "vtable": UNIT_VTABLE}])
    assert [read["call"] for read in result["argumentReads"]] == [1]
    assert [event["message"]["operation"] for event in responses(result)] == [41]


def test_load_consumer_response_reports_its_own_vtable_and_drops_credentials():
    raw = '<DA><SYSTEM><A1 T="S"><V>ExchangeKey</V></A1><A2 T="S"><V>private-key</V></A2></SYSTEM><PPU><ID T="I"><V>3</V></ID></PPU></DA>'
    result = run_node(calls=[{"operation": 41, "consumer": UNIT_CONSUMER, "vtable": UNIT_VTABLE, "xml": raw}])
    captured, = responses(result)
    metadata = captured["message"]
    assert metadata["vtable"] == hex(BASE + UNIT_VTABLE) and metadata["functionAddress"] == hex(BASE + UNIT_CONSUMER)
    assert metadata["returnAddress"] == hex(BASE + CONFIG["returnRva"]) and metadata["removed"] == ["system"]
    assert metadata["length"] == len(captured["bytes"]) and metadata["originalLength"] == len(raw.encode())
    assert parse_response(bytes(captured["bytes"])) == parse_response(b'<DA><PPU><ID T="I"><V>3</V></ID></PPU></DA>')
    assert "private-key" not in transmitted(result)


def test_login_menu_identity_never_leaves_the_process():
    result = run_node(calls=[{"operation": 114, "xml": LOGIN}])
    captured, = responses(result)
    assert captured["message"]["removed"] == ["LGI.A10", "LGI.A2", "LGI.A6", "LGI.A7"]
    assert "private-user-key" not in transmitted(result) and "900000123" not in transmitted(result)


@pytest.mark.parametrize("capacity", [15, 16, 1024])
def test_inline_and_heap_strings_are_both_read(capacity):
    xml = "<DA><PPU/></DA>"
    result = run_node(calls=[{"xml": xml, "capacity": capacity}])
    assert len(responses(result)) == 1
    reads = [read for read in result["reads"] if read["call"] == 0]
    assert any(read["method"] == "string" and read["role"] == ("value" if capacity < 16 else "payload") for read in reads)


@pytest.mark.parametrize("call", [{"length": 0}, {"length": CONFIG["maxBytes"] + 1}, {"length": 15, "capacity": 14},
                                  {"length": 16, "capacity": 15}])
def test_invalid_lengths_are_rejected_before_reading_the_string(call):
    result = run_node(calls=[call])
    assert result["events"][-1]["message"] == {"event": "rejected", "operation": 12, "code": "invalid_length"}
    assert not any(read["method"] == "string" for read in result["reads"])


@pytest.mark.parametrize("call,code", [
    ({"xml": "0,0"}, "not_xml"), ({"xml": "<DA><PPU>private-value"}, "invalid_response_xml"),
    ({"nullData": True}, "invalid_response_payload"), ({"fault": "length"}, "invalid_response_payload"),
    ({"fault": "data"}, "invalid_response_payload"), ({"rawBytes": [0xFF] * 20}, "invalid_response_payload"),
])
def test_unusable_payloads_send_only_a_fixed_code(call, code):
    result = run_node(calls=[call, {"xml": "<DA><PPU/></DA>"}])
    rejected = [event["message"] for event in result["events"] if event["message"]["event"] == "rejected"]
    assert rejected == [{"event": "rejected", "operation": 12, "code": code}]
    assert len(responses(result)) == 1 and responses(result)[0]["message"]["sequence"] == 1
    assert "private-value" not in transmitted(result)


def test_runner_assembled_agent_uses_the_same_isolated_capture_path():
    result = run_node(source=capture_responses.agent_source(CONFIG), calls=[
        {"xml": '<DA><PPU/><SYSTEM><UserKey T="S"><V>private-value</V></UserKey></SYSTEM></DA>'},
        {"operation": 41, "consumer": UNIT_CONSUMER, "vtable": UNIT_VTABLE},
    ])
    assert [event["message"]["operation"] for event in responses(result)] == [12, 41]
    assert "private-value" not in transmitted(result)


@pytest.fixture(scope="module")
def native_config():
    executable = capture_responses.masters.DEFAULT_EXE
    if not executable.is_file():
        pytest.skip("Original client is unavailable")
    return capture_responses.profile(executable, capture_responses.Selection("all"))[1]


@pytest.mark.parametrize("base", [0x400000, 0xC20000, 0x100000, 0x62000000])
def test_real_profile_accepts_relocated_code_and_still_filters_responses(native_config, base):
    assert any(block.get("relocations") for block in native_config["blocks"])
    raw = '<DA><SYSTEM><UK T="S"><V>private-key</V></UK></SYSTEM><PPU><ID T="I"><V>3</V></ID></PPU></DA>'
    result = run_node(config=native_config, base=base, source=capture_responses.agent_source(native_config),
        calls=[{"operation": 41, "consumer": UNIT_CONSUMER, "vtable": UNIT_VTABLE, "xml": raw}])
    assert result["attached"] == len(native_config["consumers"])
    assert result["events"][0]["message"]["event"] == "ready"
    captured, = responses(result)
    assert captured["message"]["functionAddress"] == hex(base + UNIT_CONSUMER)
    assert parse_response(bytes(captured["bytes"])) == parse_response(b'<DA><PPU><ID T="I"><V>3</V></ID></PPU></DA>')
    assert "private-key" not in transmitted(result)


@pytest.mark.parametrize("kind", ["relocation", "opcode"])
@pytest.mark.parametrize("base", [0xC20000, 0x100000])
def test_real_profile_rejects_corrupted_relocation_or_opcode_before_any_hook(native_config, kind, base):
    block_index, block = next((index, block) for index, block in enumerate(native_config["blocks"])
                              if block.get("relocations"))
    relocated_offsets = {offset for entry in block["relocations"] for offset in range(entry["offset"], entry["offset"] + 4)}
    offset = block["relocations"][0]["offset"] if kind == "relocation" else next(
        index for index in range(len(block["bytes"])) if index not in relocated_offsets)
    result = run_node(config=native_config, base=base,
        setup={"corruptByte": {"block": block_index, "offset": offset}}, calls=[{}])
    assert result["attached"] == 0 and result["argumentReads"] == []
    assert result["events"] == [{"message": {"event": "error", "code": "signature_mismatch"}, "bytes": None}]
