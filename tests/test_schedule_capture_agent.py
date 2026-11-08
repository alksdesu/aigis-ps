import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from client import capture_schedule
from aigis_ps.alxml import Column, Container, loads
from aigis_ps.schedule_capture import CREDENTIAL_NAMES, SCHEDULE_GROUPS


CLIENT = Path(__file__).resolve().parents[1] / "client"
BASE = 0x600000
CONFIG = {
    "exePath": "E:\\original\\Aigis_r.exe", "functionRva": 0x6801D0, "returnRva": 0x68060A,
    "maxBytes": 8 * 1024 * 1024,
    "blocks": [{"rva": 0x6801D0, "bytes": [0x55, 0x8B, 0xEC]},
               {"rva": 0x6805FD, "bytes": [0x8B, 0x06, 0x8D]}],
    "operations": {
        "70": {"vtableRva": 0xA69298, "slots": [{"offset": 4, "rva": 0x65DAB0},
                    {"offset": 0x58, "rva": 0x6801D0}], "groups": list(SCHEDULE_GROUPS[70]), "required": "CSS"},
        "71": {"vtableRva": 0xA69238, "slots": [{"offset": 4, "rva": 0x65E080},
                    {"offset": 0x58, "rva": 0x6801D0}], "groups": list(SCHEDULE_GROUPS[71]), "required": "CSP"},
    },
    "privateFields": sorted(CREDENTIAL_NAMES),
}


HARNESS = r"""
const vm = require('node:vm');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
if (input.mode === 'filter') {
    const context = vm.createContext({});
    vm.runInContext(input.filter, context, {timeout: 2000});
    const results = input.cases.map(item => {
        try {
            return {ok: true, value: context.filterScheduleXml(item.xml, item.groups, input.config.privateFields)};
        } catch (error) {
            return {ok: false, error: String(error.message)};
        }
    });
    process.stdout.write(JSON.stringify({results}));
} else {
    const config = input.config, base = input.base;
    const memory = new Map(), events = [], reads = [], argumentReads = [];
    let callback = null, attached = 0, detached = 0, current = -1, allocation = 0x70000000;
    function record(pointer, method, length) {
        reads.push({call: current, address: pointer.value, role: pointer.role, method, length});
    }
    class Pointer {
        constructor(value, role = 'other') { this.value = value; this.role = role; }
        add(value) { return new Pointer(this.value + value, this.role); }
        equals(other) { return other instanceof Pointer && other.value === this.value; }
        isNull() { return this.value === 0; }
        toString() { return '0x' + this.value.toString(16); }
        toUInt32() { return this.value >>> 0; }
        readU32() {
            record(this, 'u32');
            const value = memory.get(this.value);
            if (typeof value !== 'number') throw new Error('unreadable');
            return value >>> 0;
        }
        readPointer() {
            record(this, 'pointer');
            const value = memory.get(this.value);
            if (!(value instanceof Pointer)) throw new Error('unreadable');
            return value;
        }
        readByteArray(length) {
            record(this, 'bytes', length);
            const value = memory.get(this.value);
            if (!Buffer.isBuffer(value) || length > value.length) throw new Error('unreadable');
            return Uint8Array.from(value.subarray(0, length)).buffer;
        }
        readUtf8String(length) {
            record(this, 'string', length);
            const value = memory.get(this.value);
            if (!Buffer.isBuffer(value) || length > value.length) throw new Error('unreadable');
            return value.subarray(0, length).toString('utf8');
        }
    }
    for (const block of config.blocks) {
        const bytes = Buffer.from(block.bytes);
        for (const relocation of block.relocations || []) bytes.writeUInt32LE((base + relocation.rva) >>> 0, relocation.offset);
        memory.set(base + block.rva, bytes);
    }
    for (const entry of Object.values(config.operations)) {
        for (const slot of entry.slots) memory.set(base + entry.vtableRva + slot.offset, new Pointer(base + slot.rva));
    }
    const setup = input.setup || {};
    if (setup.badBlock !== undefined) memory.set(base + config.blocks[setup.badBlock].rva, Buffer.from([0, 0, 0]));
    if (setup.corruptByte !== undefined) {
        const change = setup.corruptByte;
        memory.get(base + config.blocks[change.block].rva)[change.offset] ^= 0x80;
    }
    if (setup.missingBlock !== undefined) memory.delete(base + config.blocks[setup.missingBlock].rva);
    if (setup.badVtable !== undefined) {
        const entry = config.operations[setup.badVtable];
        memory.set(base + entry.vtableRva + entry.slots[0].offset, new Pointer(0));
    }
    const source = input.source || ('const SCHEDULE_CAPTURE_CONFIG = ' + JSON.stringify(config) + ';\n' + input.signature + '\n' + input.filter + '\n' + input.agent);
    vm.runInNewContext(source, {
        Process: {platform: setup.platform === undefined ? 'windows' : setup.platform,
                  arch: setup.arch === undefined ? 'ia32' : setup.arch,
                  pointerSize: setup.pointerSize === undefined ? 4 : setup.pointerSize,
                  mainModule: {base: new Pointer(base, 'module'), path: setup.path || config.exePath}},
        Interceptor: {attach(target, handlers) {
            attached++;
            if (target.value !== base + config.functionRva) throw new Error('wrong target');
            callback = handlers.onEnter;
            return {detach() { detached++; }};
        }},
        Memory: {allocUtf8String(text) {
            const pointer = new Pointer(allocation, 'output');
            memory.set(allocation, Buffer.concat([Buffer.from(text), Buffer.from([0])]));
            allocation += Buffer.byteLength(text) + 64;
            return pointer;
        }},
        send(message, data) {
            const bytes = data === undefined ? null : Array.from(new Uint8Array(data));
            events.push({message, bytes});
        }
    }, {timeout: 2000});
    for (const [index, item] of (input.calls || []).entries()) {
        if (callback === null) break;
        current = index;
        const reactor = new Pointer(0x50000000 + index * 0x100, 'reactor');
        const value = new Pointer(0x51000000 + index * 0x100, 'value');
        const data = new Pointer(0x52000000 + index * 0x100, 'payload');
        const operation = item.operation === undefined ? 70 : item.operation;
        const body = item.rawBytes ? Buffer.from(item.rawBytes) : Buffer.from(item.xml || '<DA><CSS/></DA>');
        const length = item.length === undefined ? body.length : item.length;
        const capacity = item.capacity === undefined ? Math.max(16, length) : item.capacity;
        memory.set(reactor.value + 0x6C, operation);
        const tableOperation = item.vtableOperation === undefined ? operation : item.vtableOperation;
        const entry = config.operations[tableOperation];
        memory.set(reactor.value, new Pointer(entry ? base + entry.vtableRva : 0));
        memory.set(value.value + 0x10, length);
        memory.set(value.value + 0x14, capacity);
        if (capacity < 16) memory.set(value.value, body);
        else {
            memory.set(value.value, item.nullData ? new Pointer(0) : data);
            memory.set(data.value, body);
        }
        if (item.fault === 'reactor') memory.delete(reactor.value + 0x6C);
        if (item.fault === 'vtable') memory.delete(reactor.value);
        if (item.fault === 'length') memory.delete(value.value + 0x10);
        if (item.fault === 'capacity') memory.delete(value.value + 0x14);
        if (item.fault === 'data') memory.delete(capacity < 16 ? value.value : data.value);
        const args = new Proxy([value], {get(target, property) {
            argumentReads.push({call: index, property: String(property)});
            if (item.forbidArgs) throw new Error('arguments must not be read');
            return target[property];
        }});
        const context = new Proxy({ecx: reactor}, {get(target, property) {
            if (item.forbidContext) throw new Error('context must not be read');
            return target[property];
        }});
        callback.call({context, returnAddress: new Pointer(base + (item.returnRva === undefined ? config.returnRva : item.returnRva))}, args);
    }
    process.stdout.write(JSON.stringify({events, reads, argumentReads, attached, detached}));
}
"""


def run_node(*, calls=(), setup=None, config=None, cases=None, source=None, base=BASE):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the isolated capture-agent harness")
    payload = {"mode": "filter" if cases is not None else "agent", "config": config or CONFIG, "base": base,
               "calls": calls, "setup": setup or {}, "cases": cases, "source": source,
               "signature": (CLIENT / "capture_signature.js").read_text("utf-8"),
               "agent": (CLIENT / "capture_schedule.js").read_text("utf-8"),
               "filter": (CLIENT / "capture_schedule_filter.js").read_text("utf-8")}
    result = subprocess.run([node, "-e", HARNESS], input=json.dumps(payload), text=True,
                            capture_output=True, check=True, timeout=15, cwd=CLIENT.parent)
    return json.loads(result.stdout)


def responses(result):
    return [event for event in result["events"] if event["message"]["event"] == "response"]


def transmitted(result):
    return json.dumps(result["events"]) + "".join(bytes(event["bytes"]).decode("utf-8")
                                                   for event in result["events"] if event["bytes"] is not None)


@pytest.mark.parametrize("setup,code", [
    ({"platform": "linux"}, "unsupported_process"), ({"arch": "x64"}, "unsupported_process"),
    ({"pointerSize": 8}, "unsupported_process"), ({"path": "E:\\other\\Aigis_r.exe"}, "unsupported_process"),
    ({"badBlock": 0}, "signature_mismatch"), ({"badBlock": 1}, "signature_mismatch"),
    ({"missingBlock": 0}, "setup_failed"), ({"badVtable": "70"}, "vtable_mismatch"),
    ({"badVtable": "71"}, "vtable_mismatch"),
])
def test_invalid_process_or_runtime_signatures_never_attach(setup, code):
    result = run_node(setup=setup, calls=[{}])
    assert result["attached"] == 0 and result["argumentReads"] == []
    assert result["events"] == [{"message": {"event": "error", "code": code}, "bytes": None}]
    if code == "unsupported_process":
        assert result["reads"] == []


def test_path_case_and_slashes_are_normalized_for_the_same_original_executable():
    result = run_node(setup={"path": "e:/ORIGINAL/aigis_R.exe"})
    assert result["attached"] == 1 and result["events"][0]["message"]["event"] == "ready"


@pytest.mark.parametrize("call,expected_reads", [
    ({"returnRva": 0x1234, "forbidContext": True, "forbidArgs": True}, []),
    ({"operation": 11, "forbidArgs": True}, [("reactor", "u32")]),
    ({"operation": 12, "forbidArgs": True}, [("reactor", "u32")]),
    ({"operation": 13, "forbidArgs": True}, [("reactor", "u32")]),
    ({"operation": 70, "vtableOperation": 71, "forbidArgs": True}, [("reactor", "u32"), ("reactor", "pointer")]),
    ({"operation": 71, "vtableOperation": 70, "forbidArgs": True}, [("reactor", "u32"), ("reactor", "pointer")]),
])
def test_return_address_registration_and_vtable_gates_precede_argument_reads(call, expected_reads):
    result = run_node(calls=[call])
    assert result["attached"] == 1 and result["argumentReads"] == []
    assert [(read["role"], read["method"]) for read in result["reads"] if read["call"] == 0] == expected_reads
    assert not responses(result)


@pytest.mark.parametrize("operation,group", [(70, "CSS"), (71, "CSP")])
@pytest.mark.parametrize("capacity", [15, 16, 1024])
def test_empty_required_group_works_with_inline_and_heap_strings(operation, group, capacity):
    xml = f"<DA><{group}/></DA>"
    assert len(xml) == 15
    result = run_node(calls=[{"operation": operation, "xml": xml, "capacity": capacity}])
    captured = responses(result)
    assert len(captured) == 1 and loads(bytes(captured[0]["bytes"])) == Container("DA", (Container(group),))
    reads = [read for read in result["reads"] if read["call"] == 0]
    assert reads[0]["address"] == 0x5000006C and reads[1]["address"] == 0x50000000
    assert [read["address"] for read in reads if read["role"] == "value" and read["method"] == "u32"] == [0x51000010, 0x51000014]
    assert any(read["method"] == "string" and read["length"] == 15
               and read["role"] == ("value" if capacity < 16 else "payload") for read in reads)
    assert any(read["role"] == "value" and read["method"] == "pointer" for read in reads) == (capacity >= 16)


def test_two_nonempty_operations_keep_only_their_native_groups_and_utf8():
    raw70 = ('<DA><SYSTEM><UK T="S"><V>system-credential</V></UK></SYSTEM>'
             '<CSS><LineupID T="I"><V>12</V></LineupID><Name T="S"><V>召喚 🌟 &amp; 光</V></Name></CSS>'
             '<FPR/><SH/><GacyaStatus/><LCC/><PLC/>'
             '<CSP><Ignored T="S"><V>wrong-operation-value</V></Ignored></CSP>'
             '<PrivateAccount><Anything T="S"><V>unknown-group-credential</V></Anything></PrivateAccount></DA>')
    raw71 = '<DA><CSP><ID T="I"><V>9</V></ID></CSP><CSS><X T="S"><V>other-operation-value</V></X></CSS></DA>'
    result = run_node(calls=[{"xml": raw70}, {"operation": 71, "xml": raw71}])
    captured = responses(result)
    assert [item["message"]["operation"] for item in captured] == [70, 71]
    assert [item["message"]["sequence"] for item in captured] == [1, 2]
    for item in captured:
        metadata = item["message"]
        assert metadata["length"] == len(item["bytes"])
        assert metadata["functionAddress"] == hex(BASE + CONFIG["functionRva"])
        assert metadata["returnAddress"] == hex(BASE + CONFIG["returnRva"])
    document = loads(bytes(captured[0]["bytes"]))
    assert tuple(node.name for node in document.children) == SCHEDULE_GROUPS[70]
    assert document.children[0].children[1].values == ("召喚 🌟 & 光",)
    assert loads(bytes(captured[1]["bytes"])) == Container("DA", (Container("CSP", (Column("ID", "I", (9,)),)),))
    for secret in ("system-credential", "unknown-group-credential", "wrong-operation-value", "other-operation-value", "PrivateAccount", "SYSTEM"):
        assert secret not in transmitted(result)


@pytest.mark.parametrize("field", ["UserKey", "MethodKey", "USER_KEY", "METHOD-KEY", "uSeR.kEy",
                                    "UI", "UK", "MK", "UserID", "Authorization", "Cookie", "Credentials", "SYSTEM"])
def test_recursive_credentials_never_reach_any_send_payload(field):
    raw = (f'<DA><CSS><{field} T="S"><V>outer-private-value</V></{field}>'
           f'<Rows><{field}><Inner T="S"><V>nested-private-value</V></Inner></{field}>'
           '<LineupID T="I"><V>7</V></LineupID></Rows></CSS></DA>')
    result = run_node(calls=[{"xml": raw}])
    captured = responses(result)
    assert len(captured) == 1
    expected = Container("DA", (Container("CSS", (Container("Rows", (Column("LineupID", "I", (7,)),)),)),))
    assert loads(bytes(captured[0]["bytes"])) == expected
    assert "outer-private-value" not in transmitted(result) and "nested-private-value" not in transmitted(result)


@pytest.mark.parametrize("call", [
    {"length": 0}, {"length": CONFIG["maxBytes"] + 1}, {"length": 0xFFFFFFFF},
    {"length": 15, "capacity": 14}, {"length": 16, "capacity": 15},
])
def test_invalid_lengths_are_rejected_before_reading_any_string_or_data_pointer(call):
    result = run_node(calls=[call])
    assert not responses(result)
    assert result["events"][-1]["message"] == {"event": "rejected", "operation": 70, "code": "invalid_length"}
    assert not any(read["call"] == 0 and (read["method"] == "string"
        or read["role"] == "value" and read["method"] == "pointer") for read in result["reads"])


def test_configured_byte_limit_is_inclusive_for_a_valid_inline_document():
    config = {**CONFIG, "maxBytes": 15}
    result = run_node(config=config, calls=[{"xml": "<DA><CSS/></DA>", "capacity": 15}])
    assert len(responses(result)) == 1
    result = run_node(config={**CONFIG, "maxBytes": 14}, calls=[{"xml": "<DA><CSS/></DA>", "capacity": 15}])
    assert not responses(result)


@pytest.mark.parametrize("call", [{"nullData": True}, {"fault": "length"}, {"fault": "capacity"},
                                   {"fault": "data"}, {"rawBytes": [0xFF] * 15},
                                   {"xml": "<DA><CSS/></DA>\x00"}])
def test_unreadable_null_or_invalid_utf8_payloads_do_not_escape(call):
    result = run_node(calls=[call])
    assert not responses(result)
    assert result["events"][-1]["message"] == {"event": "rejected", "operation": 70, "code": "invalid_schedule_payload"}
    assert all(event["bytes"] is None for event in result["events"])


def test_rejected_candidate_does_not_disable_later_valid_read_response():
    result = run_node(calls=[{"xml": "<DA><CSS>unclosed-sensitive-value"}, {"xml": "<DA><CSS/></DA>"}])
    assert len(responses(result)) == 1 and responses(result)[0]["message"]["sequence"] == 1
    assert "unclosed-sensitive-value" not in transmitted(result)


@pytest.mark.parametrize("operation,raw", [(70, "<DA><SH/></DA>"), (71, "<DA><CSS/></DA>")])
def test_missing_required_group_is_not_sent_as_empty_success(operation, raw):
    result = run_node(calls=[{"operation": operation, "xml": raw}])
    assert not responses(result)
    assert result["events"][-1]["message"]["code"] == "missing_schedule_group"


@pytest.mark.parametrize("raw", [
    '<!DOCTYPE DA [<!ENTITY secret "private-value">]><DA><CSS/></DA>',
    '<DA><CSS/><CSS/></DA>', '<DA><CSS T="S"><V>private-value</V></CSS></DA>',
    '<DA><CSS></CSP></DA>', '<DA><CSS/></DA><DA><CSS/></DA>',
    '<DA><CSS><Rows bad="private-value"/></CSS></DA>', '<DA><CSS/>',
    '<DA N="private-value"><CSS/></DA>',
])
def test_malformed_or_ambiguous_groups_never_send_raw_content(raw):
    result = run_node(calls=[{"xml": raw}])
    assert not responses(result)
    assert all(event["bytes"] is None for event in result["events"])
    assert "private-value" not in transmitted(result)


def test_filter_keeps_cdata_and_business_counters_but_discards_private_comments():
    raw = ('<?xml version="1.0"?><DA><!--private-comment--><CSS>'
           '<Text T="S"><V><![CDATA[<pool> & banner]]></V></Text>'
           '<SessionCount T="I"><V>5</V></SessionCount><TokenCount T="I"><V>6</V></TokenCount>'
           '</CSS></DA>')
    result = run_node(cases=[{"xml": raw, "groups": CONFIG["operations"]["70"]["groups"]}])["results"][0]
    assert result["ok"] and "private-comment" not in result["value"]["xml"]
    values = loads(result["value"]["xml"].encode()).children[0].children
    assert [column.values for column in values] == [("<pool> & banner",), (5,), (6,)]


def test_filter_supports_native_short_name_and_index_attributes_without_leaking_alias_credentials():
    raw = ('<DA><C N="CSS"><Rows I="0"><C N="UserKey"><Value T="S"><V>aliased-private-value</V></Value></C>'
           '<ID T="I"><V>1</V></ID></Rows><Rows I="1"><ID T="I"><V>2</V></ID></Rows></C></DA>')
    result = run_node(calls=[{"xml": raw}])
    captured = responses(result)
    assert len(captured) == 1
    assert "aliased-private-value" not in transmitted(result)
    assert loads(bytes(captured[0]["bytes"])) == Container("DA", (Container("CSS", (
        Container("Rows", (Column("ID", "I", (1,)),), "0"),
        Container("Rows", (Column("ID", "I", (2,)),), "1"))),))


@pytest.mark.parametrize("operation,group", [(70, "CSS"), (71, "CSP")])
def test_name_overrides_are_applied_before_top_level_allowlist(operation, group):
    raw = (f'<DA><C N="{group}"><ID T="I"><V>7</V></ID></C>'
           f'<{group} N="SYSTEM"><UK T="S"><V>overridden-private-value</V></UK></{group}></DA>')
    result = run_node(calls=[{"operation": operation, "xml": raw}])
    captured = responses(result)
    assert len(captured) == 1 and "overridden-private-value" not in transmitted(result)
    assert loads(bytes(captured[0]["bytes"])) == Container("DA", (Container(group, (Column("ID", "I", (7,)),)),))


@pytest.mark.parametrize("raw", ['<DA><CSS/><C N="CSS"/></DA>',
    '<DA><CSS><Count T="I" N="Other"><V>1</V></Count></CSS></DA>',
    '<DA><CSS><Count T="I" I="0"><V>1</V></Count></CSS></DA>'])
def test_ambiguous_effective_groups_and_scalar_name_overrides_are_rejected(raw):
    assert not responses(run_node(calls=[{"xml": raw}]))


def test_runner_assembled_agent_uses_the_same_isolated_capture_path(monkeypatch):
    path = CLIENT / "capture_schedule.py"
    assert path.is_file()
    monkeypatch.syspath_prepend(str(CLIENT))
    spec = importlib.util.spec_from_file_location("schedule_agent_runner_fixture", path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    result = run_node(source=runner.agent_source(CONFIG), calls=[
        {"xml": '<DA><CSS/><SYSTEM><UserKey T="S"><V>private-value</V></UserKey></SYSTEM></DA>'},
        {"operation": 71, "xml": "<DA><CSP/></DA>"},
    ])
    assert [event["message"]["operation"] for event in responses(result)] == [70, 71]
    assert "private-value" not in transmitted(result)


def test_filter_rejects_depth_overflow_with_only_a_fixed_error():
    raw = "<DA><CSS>" + "<Rows>" * 31 + "private-value" + "</Rows>" * 31 + "</CSS></DA>"
    result = run_node(cases=[{"xml": raw, "groups": CONFIG["operations"]["70"]["groups"]}])["results"][0]
    assert result == {"ok": False, "error": "invalid_schedule_xml"}


@pytest.fixture(scope="module")
def native_config():
    executable = capture_schedule.masters.DEFAULT_EXE
    if not executable.is_file():
        pytest.skip("Original client is unavailable")
    return capture_schedule.profile(executable)[1]


@pytest.mark.parametrize("base", [0x400000, 0xC20000, 0x100000, 0x62000000])
def test_real_profile_keeps_schedule_filtering_at_each_load_base(native_config, base):
    result = run_node(config=native_config, base=base, source=capture_schedule.agent_source(native_config), calls=[
        {"operation": 70, "xml": '<DA><CSS/><SYSTEM><UK T="S"><V>private-key</V></UK></SYSTEM></DA>'},
        {"operation": 71, "xml": '<DA><CSP/></DA>'},
    ])
    assert result["attached"] == 1 and result["events"][0]["message"]["event"] == "ready"
    captured = responses(result)
    assert [entry["message"]["operation"] for entry in captured] == [70, 71]
    assert all(entry["message"]["functionAddress"] == hex(base + native_config["functionRva"]) for entry in captured)
    assert "private-key" not in transmitted(result)


@pytest.mark.parametrize("base", [0xC20000, 0x100000])
def test_schedule_agent_checks_relocated_absolute_operands_without_skipping_opcodes(base):
    block = {"rva": 0x6801D0, "bytes": [0x55, 0x68, 0x90, 0x50, 0xC8, 0x00, 0x53],
             "relocations": [{"offset": 2, "rva": 0x885090}]}
    config = {**CONFIG, "blocks": [block, *CONFIG["blocks"][1:]]}
    result = run_node(config=config, base=base, calls=[{"operation": 70}])
    assert result["attached"] == 1 and len(responses(result)) == 1
    for offset in (0, 2, 3, 4, 5, 6):
        corrupted = run_node(config=config, base=base, setup={"corruptByte": {"block": 0, "offset": offset}}, calls=[{}])
        assert corrupted["attached"] == 0 and corrupted["argumentReads"] == []
        assert corrupted["events"] == [{"message": {"event": "error", "code": "signature_mismatch"}, "bytes": None}]
