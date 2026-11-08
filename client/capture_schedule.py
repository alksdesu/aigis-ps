from __future__ import annotations

import argparse
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
    from .capture_pe import signature_blocks
else:
    import capture_masters as masters
    from capture_pe import signature_blocks
from aigis_ps.alxml import loads
from aigis_ps.schedule_capture import CREDENTIAL_NAMES, SCHEDULE_GROUPS, SCHEDULE_LIMITS, ScheduleCaptureError, sanitize_schedule_response


FUNCTION_RVA = 0x6801D0
RETURN_RVA = 0x68060A
BLOCKS = (
    (0x6801D0, "558bec83ec08538bd9895df8e88feaffff8b038bcbff75088b4014ffd084c00f84cf000000"),
    (0x6805FD, "8b068d4dd4518bce8b4058ffd0"),
    (0x67FE30, "8b43088d4e5466c746740000c74670000000008b0089466c"),
    (0x67E11E, "8b75088b7e1085ff750732c0e966030000837e141072028b36"),
    (0x6805EE, "e8ad49baff"),
)
VTABLES = {70: 0xA69298, 71: 0xA69238}
INITIALIZERS = {70: 0x65DAB0, 71: 0x65E080}
MAX_RECORDS = 16
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
DEFAULT_OUTPUT = masters.PROJECT_ROOT / "runtime" / "schedule-captures"
AGENT_ERRORS = frozenset(("unsupported_process", "signature_mismatch", "vtable_mismatch", "setup_failed"))
REJECTION_CODES = frozenset(("invalid_length", "missing_schedule_group", "invalid_schedule_payload"))


def profile(executable: Path) -> tuple[dict, dict]:
    identity = masters.verify_original_executable(executable)
    raw = Path(identity["path"]).read_bytes()
    blocks = signature_blocks(raw, BLOCKS)
    operations = {}
    for operation, table in VTABLES.items():
        slots = {4: INITIALIZERS[operation], 0x14: 0x67E0F0, 0x50: 0x408E0, 0x58: FUNCTION_RVA}
        for offset, target in slots.items():
            value, = struct.unpack("<I", masters._image_bytes(raw, table + offset, 4))
            if value != 0x400000 + target:
                raise masters.CaptureError("schedule_vtable_mismatch")
        operations[str(operation)] = {"vtableRva": table, "groups": list(SCHEDULE_GROUPS[operation]),
            "required": "CSS" if operation == 70 else "CSP",
            "slots": [{"offset": offset, "rva": target} for offset, target in slots.items()]}
    config = {"exePath": identity["path"], "functionRva": FUNCTION_RVA, "returnRva": RETURN_RVA,
              "maxBytes": SCHEDULE_LIMITS.max_bytes, "privateFields": sorted(CREDENTIAL_NAMES),
              "blocks": blocks,
              "operations": operations}
    return identity, config


def agent_source(config: dict) -> str:
    directory = Path(__file__).parent
    return ("const SCHEDULE_CAPTURE_CONFIG = " + json.dumps(config) + ";\n"
            + (directory / "capture_signature.js").read_text(encoding="utf-8") + "\n"
            + (directory / "capture_schedule_filter.js").read_text(encoding="utf-8") + "\n"
            + (directory / "capture_schedule.js").read_text(encoding="utf-8"))


class ScheduleWriter:
    def __init__(self, output: Path, identity: dict, pid: int, *, workspace: Path):
        parent = masters._safe_output(output, workspace)
        self.directory = parent / (datetime.now(timezone.utc).strftime("schedule-%Y%m%dT%H%M%SZ") + f"-{pid}-{uuid.uuid4().hex[:8]}")
        self.directory.mkdir(exist_ok=False)
        self.identity, self.pid = identity, pid
        self.started = masters.utc_now()
        self.base = None
        self.records = []
        self.round = {}
        self.rejections = []
        self.sequence = 0
        self.total_bytes = 0
        self.finished = False
        self.write_state("attaching")

    @property
    def complete(self):
        return set(self.round) == {70, 71}

    def write_state(self, status, error=None):
        record = {"schema_version": 1, "status": status, "complete": status == "complete" and self.complete,
            "source_executable": self.identity, "pid": self.pid, "started_utc": self.started,
            "updated_utc": masters.utc_now(), "raw_response_saved": False,
            "records": self.records, "selected_round": {str(key): value for key, value in self.round.items()},
            "missing_operations": sorted({70, 71} - self.round.keys()), "rejections": self.rejections,
            "pairing": "Latest operation 70 followed by operation 71 in the same attached process"}
        if error is not None:
            record["error_code"] = error
        temporary = self.directory / ("state-" + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.directory / "state.json")
        return record

    def ready(self, payload):
        base = masters._address(payload.get("base"))
        if (self.base is not None or payload.get("pointerSize") != 4 or not 0 < base <= 0xFFFFFFFF - max(VTABLES.values())
                or masters._address(payload.get("functionAddress")) != base + FUNCTION_RVA):
            raise masters.CaptureError("invalid_schedule_ready")
        self.base = base
        self.write_state("awaiting_menu")

    def reject(self, payload):
        operation, code = payload.get("operation"), payload.get("code")
        if type(operation) is not int or operation not in VTABLES or not isinstance(code, str) or code not in REJECTION_CODES:
            raise masters.CaptureError("invalid_schedule_rejection")
        if operation == 70:
            self.round = {}
        self.rejections = [*self.rejections, {"operation": operation, "code": code, "at": masters.utc_now()}][-16:]
        self.write_state("awaiting_menu")

    def record(self, payload, data):
        operation, sequence = payload.get("operation"), payload.get("sequence")
        if (self.finished or self.base is None or type(operation) is not int or operation not in VTABLES
                or type(sequence) is not int or not self.sequence < sequence <= 0x7FFFFFFF):
            raise masters.CaptureError("invalid_schedule_message")
        if (not isinstance(data, bytes) or type(payload.get("length")) is not int or len(data) != payload["length"]
                or type(payload.get("originalLength")) is not int or not 0 < payload["originalLength"] <= SCHEDULE_LIMITS.max_bytes):
            raise masters.CaptureError("invalid_schedule_length")
        for name, expected in (("functionAddress", FUNCTION_RVA), ("returnAddress", RETURN_RVA), ("vtable", VTABLES[operation])):
            if masters._address(payload.get(name)) != self.base + expected:
                raise masters.CaptureError("schedule_address_mismatch")
        clean, metadata = sanitize_schedule_response(operation, data)
        if loads(data, limits=SCHEDULE_LIMITS) != loads(clean, limits=SCHEDULE_LIMITS):
            raise masters.CaptureError("schedule_agent_filter_incomplete")
        return self._store(operation, sequence, clean, metadata, {
            "original_response_bytes": payload["originalLength"], "function_rva": hex(FUNCTION_RVA),
            "return_rva": hex(RETURN_RVA), "vtable_rva": hex(VTABLES[operation])})

    def derive(self, operation, sequence, data, provenance):
        if (self.finished or type(operation) is not int or operation not in VTABLES
                or type(sequence) is not int or not self.sequence < sequence <= 0x7FFFFFFF):
            raise masters.CaptureError("invalid_schedule_message")
        if not isinstance(data, bytes) or not 0 < len(data) <= SCHEDULE_LIMITS.max_bytes or not isinstance(provenance, dict):
            raise masters.CaptureError("invalid_schedule_length")
        clean, metadata = sanitize_schedule_response(operation, data)
        return self._store(operation, sequence, clean, metadata, provenance)

    def _store(self, operation, sequence, clean, metadata, provenance):
        if len(self.records) >= MAX_RECORDS or self.total_bytes + len(clean) > MAX_CAPTURE_BYTES:
            raise masters.CaptureError("schedule_capture_capacity")
        name = f"{sequence:04d}-{operation}.xml"
        with (self.directory / name).open("xb") as stream:
            stream.write(clean)
        record = {**metadata, "sequence": sequence, "path": name, "captured_utc": masters.utc_now(), **provenance}
        self.records.append(record)
        self.sequence = sequence
        self.total_bytes += len(clean)
        if operation == 70:
            self.round = {70: name}
        elif 70 in self.round:
            self.round[71] = name
        self.write_state("captured" if self.complete else "awaiting_menu")
        return record

    def finish(self, error=None):
        if self.finished:
            raise masters.CaptureError("schedule_already_finished")
        status = "complete" if error is None and self.complete else "partial" if self.records else "failed"
        record = self.write_state(status, error)
        path = self.directory / "manifest.json"
        with path.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        self.finished = True
        return path


def capture_process(device, pid, executable, output, timeout, *, progress=None,
                    path_query=masters.query_process_image, workspace=masters.WORKSPACE_ROOT, clock=time.monotonic):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise masters.CaptureError("invalid_schedule_timeout")
    identity = masters.verify_process(pid, executable, path_query)
    _, config = profile(executable)
    writer = ScheduleWriter(output, identity, pid, workspace=workspace)
    events = queue.Queue(maxsize=8)
    overflow = threading.Event()
    session = None

    def receive(message, data=None):
        try:
            events.put_nowait((message, data))
        except queue.Full:
            overflow.set()

    try:
        session = device.attach(pid)
        masters.verify_process(pid, executable, path_query)
        session.on("detached", lambda *_: receive({"type": "detached"}))
        script = session.create_script(agent_source(config))
        script.on("message", receive)
        script.load()
        deadline = clock() + timeout
        for message, data in masters.receive_until(events, deadline, clock=clock):
            if overflow.is_set():
                raise masters.CaptureError("schedule_queue_overflow")
            if not isinstance(message, dict) or message.get("type") != "send" or not isinstance(message.get("payload"), dict):
                detached = isinstance(message, dict) and message.get("type") == "detached"
                raise masters.CaptureError("schedule_process_detached" if detached else "schedule_agent_error")
            payload = message["payload"]
            event = payload.get("event")
            if event == "ready" and data is None:
                writer.ready(payload)
                if progress:
                    progress({"event": "ready", "pid": pid, "directory": str(writer.directory)})
            elif event == "rejected" and data is None:
                writer.reject(payload)
                if progress:
                    progress({"event": "rejected", "operation": payload["operation"], "code": payload["code"]})
            elif event == "response":
                record = writer.record(payload, data)
                if progress:
                    progress({"event": "captured", "operation": record["operation"], "groups": record["kept_groups"],
                              "bytes": record["output_bytes"], "missing_operations": sorted({70, 71} - writer.round.keys())})
            elif event == "error" and payload.get("code") in AGENT_ERRORS:
                raise masters.CaptureError(payload["code"])
            else:
                raise masters.CaptureError("invalid_schedule_event")
            if writer.complete:
                return writer.finish()
        raise masters.CaptureError("schedule_capture_timeout")
    except BaseException as error:
        code = error.code if isinstance(error, (masters.CaptureError, ScheduleCaptureError)) else "schedule_interrupted" if isinstance(error, KeyboardInterrupt) else "schedule_runtime_error"
        path = writer.finish(code) if not writer.finished else writer.directory / "manifest.json"
        if progress:
            progress({"event": "incomplete", "code": code, "manifest": str(path)})
        raise
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Passively capture verified original-client summoning schedules.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--wait", action="store_true")
    mode.add_argument("--pid", type=int)
    parser.add_argument("--exe", type=Path, default=masters.DEFAULT_EXE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600 or args.pid is not None and args.pid <= 0:
        parser.error("PID must be positive and timeout must be between 0 and 3600 seconds")
    try:
        identity, config = profile(args.exe)
        output = masters._safe_output(args.output, masters.WORKSPACE_ROOT, create=False)
        if args.check:
            print(json.dumps({"status": "checked", "attached": False, "input_sha256": identity["sha256"],
                "operations": [70, 71], "groups": SCHEDULE_GROUPS, "output": str(output),
                "agent_sha256": hashlib.sha256(agent_source(config).encode()).hexdigest()}), flush=True)
            return 0
        device = importlib.import_module("frida").get_local_device()
        deadline = time.monotonic() + args.timeout
        progress = lambda event: print(json.dumps(event), flush=True)
        pid = args.pid
        if args.wait:
            progress({"event": "waiting_for_process", "process": args.exe.name, "operations": [70, 71]})
            pid = masters.wait_for_process(device, args.exe, args.timeout)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise masters.CaptureError("schedule_capture_timeout")
        path = capture_process(device, pid, args.exe, output, remaining, progress=progress)
        print(json.dumps({"status": "complete", "manifest": str(path)}), flush=True)
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"status": "stopped", "code": "schedule_interrupted"}), flush=True)
        return 130
    except Exception as error:
        code = error.code if isinstance(error, (masters.CaptureError, ScheduleCaptureError)) else "missing_frida" if isinstance(error, ModuleNotFoundError) else "schedule_setup_failed"
        print(json.dumps({"status": "failed", "code": code}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
