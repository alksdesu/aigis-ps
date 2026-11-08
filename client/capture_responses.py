from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
from pathlib import Path
import queue
import struct
import threading
import time
import uuid

if __package__:
    from . import capture_masters as masters
    from . import capture_schedule as schedule
    from .capture_pe import signature_blocks
else:
    import capture_masters as masters
    import capture_schedule as schedule
    from capture_pe import signature_blocks
from aigis_ps.response_capture import (EXCLUDED_OPERATIONS, RESPONSE_LIMITS, RESPONSE_PRESETS, ResponseCaptureError,
                                       filter_rules, inspect_filtered_response, selected_operations)


@dataclass(frozen=True)
class LoadReactor:
    name: str
    vtable_rva: int
    initializer_rva: int
    consumer_rva: int
    prologue: str


@dataclass(frozen=True)
class ExcludedReactor:
    name: str
    operations: tuple[int, ...]
    vtable_rva: int
    initializer_rva: int


@dataclass(frozen=True)
class Selection:
    mode: str
    operations: tuple[int, ...] | None = None
    preset: str | None = None

    def describe(self) -> dict:
        return {"mode": self.mode, "operations": list(self.operations) if self.operations is not None else None,
                "preset": self.preset}


DEFAULT_CONSUMER_RVA = schedule.FUNCTION_RVA
RETURN_RVA = schedule.RETURN_RVA
DISPATCHER_SLOT = 0x14
DISPATCHER_RVA = 0x67E0F0
RELEASE_SLOT = 0x50
RELEASE_RVA = 0x408E0
CONSUMER_SLOT = 0x58
LOAD_REACTORS = (
    LoadReactor("CReactorGameParamLoad", 0x9D1998, 0x65AC40, 0x65AC50, "558bec6aff682f2ecc0064a1000000005083ec1c535657a1"),
    LoadReactor("CReactorGameParam2Load", 0x9D1938, 0x65AC40, 0x65AD90, "558bec6aff685f2ecc0064a10000000050535657a1a8e8f1"),
    LoadReactor("CReactorGameConfigLoad", 0x9D1878, 0x65AC40, 0x65AE70, "558bec6aff68a72ecc0064a1000000005081ec80000000a1"),
    LoadReactor("CReactorReproductionItemLoad", 0x9D1818, 0x65AC40, 0x65B250, "558bec6aff685f2ecc0064a10000000050535657a1a8e8f1"),
    LoadReactor("CReactorBattleParamLoad", 0x9D17B8, 0x65AC40, 0x65B340, "558bec6aff685f2ecc0064a10000000050535657a1a8e8f1"),
    LoadReactor("CReactorPlayerUnitLoad", 0x9D18D8, 0x65AC40, 0x65B420, "558bec6aff68df2ecc0064a1000000005051535657a1a8e8"),
    LoadReactor("CReactorCrystalLoad", 0xA69BB0, 0x64DEA0, 0x65B800, "558bec6aff685f2fcc0064a1000000005083ec48535657a1"),
)
EXCLUDED_REACTORS = (
    ExcludedReactor("CReactorMethodKey", (31,), 0xA69FEC, 0x650BA0),
    ExcludedReactor("CReactorSerialCode", (32,), 0xA69F8C, 0x650C80),
    ExcludedReactor("CReactorLoadSchemeUrl", (45,), 0xA69D98, 0x65A8E0),
    ExcludedReactor("CReactorReLoadPayment", (47,), 0xA69358, 0x65BE80),
    ExcludedReactor("CDistributePaymentFreeItem", (50,), 0xA68AF4, 0x6638E0),
    ExcludedReactor("CReactorPreparePayment", (91,), 0xA6A488, 0x655FB0),
    ExcludedReactor("CReactorCheatCheaker", (102, 103), 0xA696C0, 0x64DEA0),
    ExcludedReactor("CRemoveDataLink", (144,), 0xA68A94, 0x6509F0),
    ExcludedReactor("CLoadRemoveAccountInfo", (145,), 0xA69058, 0x663CD0),
    ExcludedReactor("CSaveDmmPremiumReceiverId", (146,), 0xA69118, 0x6509F0),
    ExcludedReactor("CLoginGamePlayerExe", (152,), 0xA68F98, 0x6649F0),
    ExcludedReactor("CExtendOnetimeTokenExpiration", (153,), 0xA68F38, 0x664CF0),
    ExcludedReactor("CReactorPurchasePackageGamePlayerExe", (154,), 0xA69A2C, 0x656340),
    ExcludedReactor("CGetDmmPoint", (155,), 0xA68ED8, 0x664F90),
    ExcludedReactor("CSkinShopPurchaseApiTransceiver", (156,), 0xA6DBD0, 0x6845F0),
)
CONSUMER_RVAS = (DEFAULT_CONSUMER_RVA, *(reactor.consumer_rva for reactor in LOAD_REACTORS))
LOAD_BY_CONSUMER = {reactor.consumer_rva: reactor for reactor in LOAD_REACTORS}
MAX_RECORDS = 8192
MAX_CAPTURE_BYTES = 1024 * 1024 * 1024
MAX_TIMEOUT = 4 * 3600
DEFAULT_TIMEOUT = 1800
EVENT_QUEUE_SIZE = 256
RECENT_REJECTIONS = 32
MAX_REMOVED_LABELS = 256
DEFAULT_OUTPUT = masters.PROJECT_ROOT / "runtime" / "response-captures"
AGENT_ERRORS = frozenset(("unsupported_process", "signature_mismatch", "vtable_mismatch", "setup_failed"))
AGENT_REJECTIONS = frozenset(("invalid_length", "invalid_response_xml", "not_xml", "output_too_large", "invalid_response_payload"))


def _slots(raw: bytes, table: int, slots: dict[int, int]) -> list[dict]:
    for offset, target in slots.items():
        value, = struct.unpack("<I", masters._image_bytes(raw, table + offset, 4))
        if value != 0x400000 + target:
            raise masters.CaptureError("response_vtable_mismatch")
    return [{"offset": offset, "rva": target} for offset, target in slots.items()]


def profile(executable: Path, selection: Selection | None = None) -> tuple[dict, dict]:
    identity = masters.verify_original_executable(executable)
    raw = Path(identity["path"]).read_bytes()
    blocks = [*schedule.BLOCKS, *((reactor.consumer_rva, reactor.prologue) for reactor in LOAD_REACTORS)]
    blocks = signature_blocks(raw, blocks)
    vtables = [{"rva": reactor.vtable_rva, "slots": _slots(raw, reactor.vtable_rva, {
        4: reactor.initializer_rva, DISPATCHER_SLOT: DISPATCHER_RVA, RELEASE_SLOT: RELEASE_RVA, CONSUMER_SLOT: reactor.consumer_rva})}
        for reactor in LOAD_REACTORS]
    vtables += [{"rva": reactor.vtable_rva, "slots": _slots(raw, reactor.vtable_rva, {
        4: reactor.initializer_rva, CONSUMER_SLOT: DEFAULT_CONSUMER_RVA})} for reactor in EXCLUDED_REACTORS]
    operations = None if selection is None else selection.operations
    config = {"exePath": identity["path"], "returnRva": RETURN_RVA, "maxBytes": RESPONSE_LIMITS.max_bytes,
              "blocks": blocks,
              "vtables": vtables, "consumers": [{"rva": rva} for rva in CONSUMER_RVAS],
              "excludedOperations": sorted(EXCLUDED_OPERATIONS),
              "excludedVtables": [reactor.vtable_rva for reactor in EXCLUDED_REACTORS],
              "operations": list(operations) if operations is not None else None, "rules": filter_rules()}
    return identity, config


def agent_source(config: dict) -> str:
    directory = Path(__file__).parent
    return ("const RESPONSE_CAPTURE_CONFIG = " + json.dumps(config) + ";\n"
            + (directory / "capture_signature.js").read_text(encoding="utf-8") + "\n"
            + (directory / "capture_response_filter.js").read_text(encoding="utf-8") + "\n"
            + (directory / "capture_responses.js").read_text(encoding="utf-8"))


def _operation(payload: dict) -> int:
    operation = payload.get("operation")
    if type(operation) is not int or not 0 <= operation <= 0xFFFFFFFF:
        raise masters.CaptureError("invalid_response_operation")
    return operation


class ResponseWriter:
    def __init__(self, output: Path, identity: dict, pid: int, selection: Selection, *, workspace: Path):
        parent = masters._safe_output(output, workspace)
        self.directory = parent / (datetime.now(timezone.utc).strftime("responses-%Y%m%dT%H%M%SZ") + f"-{pid}-{uuid.uuid4().hex[:8]}")
        self.directory.mkdir(exist_ok=False)
        self.journal = self.directory / "records.jsonl"
        self.identity, self.pid, self.selection = identity, pid, selection
        self.started = masters.utc_now()
        self.base = None
        self.counts: Counter[int] = Counter()
        self.excluded: Counter[int] = Counter()
        self.rejections = []
        self.rejected = 0
        self.sequence = 0
        self.records = 0
        self.total_bytes = 0
        self.last = None
        self.status = "attaching"
        self.finished = False
        self.write_state("attaching")

    @property
    def missing(self) -> list[int]:
        return sorted(set(self.selection.operations or ()) - self.counts.keys())

    @property
    def complete(self) -> bool:
        return self.selection.operations is not None and not self.missing

    def write_state(self, status, reason=None, error=None):
        record = {"schema_version": 1, "status": status, "selection": self.selection.describe(),
                  "source_executable": self.identity, "pid": self.pid, "started_utc": self.started,
                  "updated_utc": masters.utc_now(), "raw_response_saved": False, "records": self.records,
                  "output_bytes": self.total_bytes, "records_file": self.journal.name,
                  "captured": {str(operation): count for operation, count in sorted(self.counts.items())},
                  "missing_operations": self.missing,
                  "excluded_seen": {str(operation): count for operation, count in sorted(self.excluded.items())},
                  "rejected": self.rejected, "recent_rejections": self.rejections, "last_record": self.last}
        if reason is not None:
            record["end_reason"] = reason
        if error is not None:
            record["error_code"] = error
        temporary = self.directory / ("state-" + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.directory / "state.json")
        self.status = status
        return record

    def ready(self, payload):
        base = masters._address(payload.get("base"))
        if (self.base is not None or payload.get("pointerSize") != 4
                or not 0 < base <= 0xFFFFFFFF - max(RETURN_RVA, *CONSUMER_RVAS)):
            raise masters.CaptureError("invalid_response_ready")
        consumers = payload.get("consumers")
        if not isinstance(consumers, list) or [masters._address(item) for item in consumers] != [base + rva for rva in CONSUMER_RVAS]:
            raise masters.CaptureError("invalid_response_ready")
        self.base = base
        self.write_state("capturing")

    def _reject(self, operation, code):
        self.rejected += 1
        self.rejections = [*self.rejections, {"operation": operation, "code": code, "at": masters.utc_now()}][-RECENT_REJECTIONS:]
        self.write_state("capturing")

    def reject(self, payload):
        operation, code = _operation(payload), payload.get("code")
        if self.base is None or not isinstance(code, str) or code not in AGENT_REJECTIONS:
            raise masters.CaptureError("invalid_response_rejection")
        self._reject(operation, code)

    def exclude(self, payload):
        operation = _operation(payload)
        if self.base is None:
            raise masters.CaptureError("invalid_response_message")
        self.excluded[operation] += 1
        self.write_state("capturing")

    def _check_addresses(self, payload, operation) -> tuple[int, LoadReactor | None]:
        function = masters._address(payload.get("functionAddress")) - self.base
        if function not in CONSUMER_RVAS or masters._address(payload.get("returnAddress")) != self.base + RETURN_RVA:
            raise masters.CaptureError("response_address_mismatch")
        vtable = masters._address(payload.get("vtable")) - self.base
        reactor = LOAD_BY_CONSUMER.get(function)
        if reactor is not None and vtable != reactor.vtable_rva:
            raise masters.CaptureError("response_address_mismatch")
        if reactor is None and vtable in {item.vtable_rva for item in (*LOAD_REACTORS, *EXCLUDED_REACTORS)}:
            raise masters.CaptureError("response_address_mismatch")
        if operation in EXCLUDED_OPERATIONS or self.selection.operations is not None and operation not in self.selection.operations:
            raise masters.CaptureError("unexpected_response_operation")
        return vtable, reactor

    def record(self, payload, data):
        operation, sequence = _operation(payload), payload.get("sequence")
        if self.finished or self.base is None or type(sequence) is not int or not self.sequence < sequence <= 0x7FFFFFFF:
            raise masters.CaptureError("invalid_response_message")
        if (not isinstance(data, bytes) or type(payload.get("length")) is not int or len(data) != payload["length"]
                or type(payload.get("originalLength")) is not int or not 0 < payload["originalLength"] <= RESPONSE_LIMITS.max_bytes):
            raise masters.CaptureError("invalid_response_length")
        removed, removed_rows = payload.get("removed"), payload.get("removedRows")
        if (not isinstance(removed, list) or len(removed) > MAX_REMOVED_LABELS
                or any(not isinstance(label, str) or not 0 < len(label) <= 128 for label in removed)
                or type(removed_rows) is not int or removed_rows < 0):
            raise masters.CaptureError("invalid_response_message")
        vtable, reactor = self._check_addresses(payload, operation)
        self.sequence = sequence
        try:
            metadata = inspect_filtered_response(data)
        except ResponseCaptureError as error:
            self._reject(operation, error.code)
            return None
        if self.records >= MAX_RECORDS or self.total_bytes + len(data) > MAX_CAPTURE_BYTES:
            raise masters.CaptureError("response_capture_capacity")
        name = f"{sequence:05d}-{operation}.xml"
        with (self.directory / name).open("xb") as stream:
            stream.write(data)
        entry = {"sequence": sequence, "operation": operation, "path": name, "captured_utc": masters.utc_now(),
                 "original_response_bytes": payload["originalLength"], "function_rva": hex(masters._address(payload["functionAddress"]) - self.base),
                 "return_rva": hex(RETURN_RVA), "vtable_rva": hex(vtable), "reactor": reactor.name if reactor else None,
                 "agent_removed": removed, "agent_removed_rows": removed_rows, **metadata}
        with self.journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.records += 1
        self.total_bytes += len(data)
        self.counts[operation] += 1
        self.last = {key: entry[key] for key in ("sequence", "operation", "path", "output_bytes")}
        self.write_state("capturing")
        return entry

    def finish(self, reason, error=None):
        if self.finished:
            raise masters.CaptureError("response_already_finished")
        if error is None and self.complete:
            status = "complete"
        elif error is None and self.selection.operations is None:
            status = "stopped"
        else:
            status = "partial" if self.records else "failed"
        record = self.write_state(status, reason, error)
        record["finished_utc"] = masters.utc_now()
        path = self.directory / "manifest.json"
        with path.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        self.finished = True
        return path


def _valid_timeout(timeout) -> bool:
    return type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= MAX_TIMEOUT


def capture_process(device, pid, executable, output, timeout, selection, *, progress=None,
                    path_query=masters.query_process_image, workspace=masters.WORKSPACE_ROOT, clock=time.monotonic,
                    stop_event=None, capture_deadline=None):
    if not _valid_timeout(timeout):
        raise masters.CaptureError("invalid_response_timeout")
    if capture_deadline is not None and (type(capture_deadline) not in (int, float) or not math.isfinite(capture_deadline)):
        raise masters.CaptureError("invalid_capture_deadline")
    identity = masters.verify_process(pid, executable, path_query)
    _, config = profile(executable, selection)
    writer = ResponseWriter(output, identity, pid, selection, workspace=workspace)
    events = queue.Queue(maxsize=EVENT_QUEUE_SIZE)
    overflow = threading.Event()
    session = None

    def receive(message, data=None):
        try:
            events.put_nowait((message, data))
        except queue.Full:
            overflow.set()

    def report(event):
        if progress:
            progress(event)

    try:
        masters.check_capture_window(stop_event, capture_deadline, clock=clock)
        session = device.attach(pid)
        masters.verify_process(pid, executable, path_query)
        masters.check_capture_window(stop_event, capture_deadline, clock=clock)
        session.on("detached", lambda *_: receive({"type": "detached"}))
        script = session.create_script(agent_source(config))
        script.on("message", receive)
        masters.check_capture_window(stop_event, capture_deadline, clock=clock)
        script.load()
        masters.check_capture_window(stop_event, capture_deadline, clock=clock)
        deadline = clock() + timeout
        if capture_deadline is not None:
            deadline = min(deadline, capture_deadline)
        for message, data in masters.receive_until(events, deadline, clock=clock, stop_event=stop_event):
            if overflow.is_set():
                raise masters.CaptureError("response_queue_overflow")
            if isinstance(message, dict) and message.get("type") == "detached":
                if selection.operations is None and writer.base is not None:
                    path = writer.finish("process_exited")
                    report({"event": "finished", "status": writer.status, "reason": "process_exited", "manifest": str(path)})
                    return path
                raise masters.CaptureError("response_process_detached")
            if not isinstance(message, dict) or message.get("type") != "send" or not isinstance(message.get("payload"), dict):
                raise masters.CaptureError("response_agent_error")
            payload = message["payload"]
            event = payload.get("event")
            if event == "ready" and data is None:
                writer.ready(payload)
                report({"event": "ready", "pid": pid, "directory": str(writer.directory), "selection": selection.describe()})
            elif event == "response":
                entry = writer.record(payload, data)
                if entry is None:
                    report({"event": "rejected", "operation": payload["operation"], "code": writer.rejections[-1]["code"]})
                else:
                    report({"event": "captured", "operation": entry["operation"], "sequence": entry["sequence"], "path": entry["path"],
                            "bytes": entry["output_bytes"], "removed": entry["agent_removed"], "missing_operations": writer.missing,
                            **{key: entry[key] for key in ("sha256", "function_rva", "return_rva", "vtable_rva")}})
            elif event == "rejected" and data is None:
                writer.reject(payload)
                report({"event": "rejected", "operation": payload["operation"], "code": payload["code"]})
            elif event == "excluded" and data is None:
                writer.exclude(payload)
                report({"event": "excluded", "operation": payload["operation"]})
            elif event == "error" and payload.get("code") in AGENT_ERRORS:
                raise masters.CaptureError(payload["code"])
            else:
                raise masters.CaptureError("invalid_response_event")
            if writer.complete:
                path = writer.finish("selection_complete")
                report({"event": "finished", "status": writer.status, "reason": "selection_complete", "manifest": str(path)})
                return path
        if selection.operations is None and writer.base is not None:
            path = writer.finish("timeout")
            report({"event": "finished", "status": writer.status, "reason": "timeout", "manifest": str(path)})
            return path
        raise masters.CaptureError("response_capture_timeout")
    except KeyboardInterrupt:
        if not writer.finished:
            path = writer.finish("interrupted")
            report({"event": "finished", "status": writer.status, "reason": "interrupted", "manifest": str(path)})
        raise
    except BaseException as error:
        code = error.code if isinstance(error, (masters.CaptureError, ResponseCaptureError)) else "response_runtime_error"
        path = writer.finish(code, code) if not writer.finished else writer.directory / "manifest.json"
        report({"event": "incomplete", "status": writer.status, "code": code, "manifest": str(path)})
        raise
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


def _selection(parser, args) -> Selection | None:
    try:
        if args.all:
            return Selection("all")
        if args.preset is not None:
            return Selection("preset", selected_operations(RESPONSE_PRESETS[args.preset]), args.preset)
        if args.operations is not None:
            return Selection("operations", selected_operations(args.operations))
    except ResponseCaptureError as error:
        excluded = {str(value): EXCLUDED_OPERATIONS[value] for value in args.operations or () if value in EXCLUDED_OPERATIONS}
        parser.error(f"{error.code}: {json.dumps(excluded)}" if excluded else error.code)
    if not args.check:
        parser.error("choose --operations, --preset or --all")
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Passively capture credential-filtered responses from the verified original client.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--wait", action="store_true")
    mode.add_argument("--pid", type=int)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--operations", type=int, nargs="+")
    scope.add_argument("--preset", choices=sorted(RESPONSE_PRESETS))
    scope.add_argument("--all", action="store_true")
    parser.add_argument("--exe", type=Path, default=masters.DEFAULT_EXE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    if not _valid_timeout(args.timeout) or args.pid is not None and args.pid <= 0:
        parser.error(f"PID must be positive and timeout must be between 0 and {MAX_TIMEOUT} seconds")
    selection = _selection(parser, args)
    try:
        identity, config = profile(args.exe, selection)
        output = masters._safe_output(args.output, masters.WORKSPACE_ROOT, create=False)
        if args.check:
            print(json.dumps({"status": "checked", "attached": False, "input_sha256": identity["sha256"],
                "selection": selection.describe() if selection else None, "consumers": len(CONSUMER_RVAS),
                "excluded_operations": {str(key): value for key, value in sorted(EXCLUDED_OPERATIONS.items())},
                "presets": RESPONSE_PRESETS, "output": str(output),
                "agent_sha256": hashlib.sha256(agent_source(config).encode()).hexdigest()}, ensure_ascii=False), flush=True)
            return 0
        device = importlib.import_module("frida").get_local_device()
        deadline = time.monotonic() + args.timeout
        progress = lambda event: print(json.dumps(event, ensure_ascii=False), flush=True)
        pid = args.pid
        if args.wait:
            progress({"event": "waiting_for_process", "process": args.exe.name, "selection": selection.describe()})
            pid = masters.wait_for_process(device, args.exe, args.timeout)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise masters.CaptureError("response_capture_timeout")
        path = capture_process(device, pid, args.exe, output, remaining, selection, progress=progress)
        status = json.loads(path.read_text(encoding="utf-8"))["status"]
        print(json.dumps({"status": status, "manifest": str(path)}), flush=True)
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"status": "stopped", "code": "response_interrupted"}), flush=True)
        return 130
    except Exception as error:
        code = error.code if isinstance(error, (masters.CaptureError, ResponseCaptureError)) else "missing_frida" if isinstance(error, ModuleNotFoundError) else "response_setup_failed"
        print(json.dumps({"status": "failed", "code": code}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
