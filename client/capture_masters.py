from __future__ import annotations

import argparse
from collections.abc import Callable, Collection, Iterator
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import queue
import struct
import sys
import threading
import time
import uuid

from aigis_ps.al_format import parse_schema
from aigis_ps.assets import resolve_asset
from aigis_ps.public_tables import MASTER_IDS, MASTER_LIMITS, REQUIRED_FIELDS, PublicTableError, validate_public_master


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
DEFAULT_EXE = WORKSPACE_ROOT / "aigisr_exe" / "Aigis_r.exe"
DEFAULT_OUTPUT = PROJECT_ROOT / "runtime" / "captures"
EXPECTED_SHA256 = "babe8210a8652fb25b1c61056ee500b05bd9a3ae37f55d1682a2fb8b850802ba"
SUPPORTED_SHA256 = frozenset((EXPECTED_SHA256, "d0be510fb38b1d6dfe63c3562c97e45f49d318bf7e155dee309d88efe963f92b"))
FUNCTION_RVA = 0x722E10
FUNCTION_BYTES = bytes.fromhex("55 8b ec 51 56 8b f1 6a 01 89 75 fc e8 2f 02 00 00 8b 45 0c 8a 4e 1f 89 46 2c 80 e1 fd 89 46 10")
MAX_BYTES = MASTER_LIMITS.max_bytes
TABLES = {
    "CardData": {"callRva": 0x4E7DC, "callBytes": [0xE8, 0x2F, 0x46, 0x6D, 0],
                 "id": MASTER_IDS["CardData"], "required": REQUIRED_FIELDS["CardData"], "fields": 103},
    "QuestTable": {"callRva": 0x2782D4, "callBytes": [0xE8, 0x37, 0xAB, 0x4A, 0],
                   "id": MASTER_IDS["QuestTable"], "required": REQUIRED_FIELDS["QuestTable"], "fields": 54},
}
_AGENT_ERRORS = {"unsupported_process", "signature_mismatch", "invalid_length", "read_failed", "setup_failed"}
EVENT_POLL_SECONDS = 0.5
PROCESS_POLL_SECONDS = 0.25


class CaptureError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path_key(path: Path | str) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _image_bytes(data: bytes, rva: int, size: int) -> bytes:
    try:
        pe, = struct.unpack_from("<I", data, 0x3C)
        if data[:2] != b"MZ" or data[pe:pe + 4] != b"PE\0\0":
            raise CaptureError("invalid_executable")
        machine, count = struct.unpack_from("<HH", data, pe + 4)
        optional_size, = struct.unpack_from("<H", data, pe + 20)
        if machine != 0x14C:
            raise CaptureError("invalid_executable")
        section_start = pe + 24 + optional_size
        for index in range(count):
            virtual_size, virtual, raw_size, raw = struct.unpack_from("<IIII", data, section_start + 40 * index + 8)
            if virtual <= rva and rva + size <= virtual + raw_size:
                offset = raw + rva - virtual
                result = data[offset:offset + size]
                if len(result) == size:
                    return result
    except struct.error as exc:
        raise CaptureError("invalid_executable") from exc
    raise CaptureError("invalid_executable")


def verify_original_executable(path: Path | str) -> dict:
    path = Path(path).resolve(strict=True)
    if path.name.lower() != "aigis_r.exe" or path.stat().st_size > 64 * 1024 * 1024:
        raise CaptureError("unexpected_executable")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest not in SUPPORTED_SHA256:
        raise CaptureError("executable_hash_mismatch")
    if _image_bytes(data, FUNCTION_RVA, len(FUNCTION_BYTES)) != FUNCTION_BYTES:
        raise CaptureError("executable_signature_mismatch")
    for table in TABLES.values():
        if _image_bytes(data, table["callRva"], 5) != bytes(table["callBytes"]):
            raise CaptureError("executable_signature_mismatch")
    return {"path": str(path), "sha256": digest}


def query_process_image(pid: int) -> Path:
    if sys.platform != "win32":
        raise CaptureError("windows_required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise CaptureError("process_image_unavailable")
    try:
        length = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(length.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            raise CaptureError("process_image_unavailable")
        return Path(buffer.value)
    finally:
        kernel.CloseHandle(handle)


def verify_process(pid: int, expected_exe: Path, path_query=query_process_image) -> dict:
    image = path_query(pid)
    if _path_key(image) != _path_key(expected_exe):
        raise CaptureError("process_path_mismatch")
    return verify_original_executable(image)


def load_public_schemas(source_root: Path) -> dict[str, set[str]]:
    schemas = {}
    for name, table in TABLES.items():
        schema = parse_schema(resolve_asset(source_root, name + ".ard").read_bytes())
        fields = {field.name for field in schema.fields}
        if len(fields) != table["fields"] or not table["required"] <= fields:
            raise CaptureError("schema_mismatch")
        schemas[name] = fields
    return schemas


def validate_master(name: str, data: bytes, schemas: dict[str, set[str]]) -> dict:
    try:
        _, metadata = validate_public_master(name, data, schemas)
        return metadata
    except PublicTableError as exc:
        raise CaptureError(exc.code) from exc


def _safe_output(parent: Path, workspace: Path, *, create: bool = True) -> Path:
    absolute = parent.absolute()
    if any(path.is_symlink() or path.is_junction() for path in (absolute, *absolute.parents)):
        raise CaptureError("unsafe_output_path")
    resolved = absolute.resolve()
    if not resolved.is_relative_to(workspace.resolve()):
        raise CaptureError("output_outside_workspace")
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
    return resolved


class CaptureWriter:
    def __init__(self, parent: Path, identity: dict, pid: int, schemas: dict[str, set[str]], *, workspace: Path = WORKSPACE_ROOT):
        parent = _safe_output(parent, workspace)
        self.directory = parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{pid}-{uuid.uuid4().hex[:8]}")
        self.directory.mkdir(exist_ok=False)
        self.identity = identity
        self.pid = pid
        self.schemas = schemas
        self.started = utc_now()
        self.records: dict[str, dict] = {}
        self.base: int | None = None
        self.finished = False

    def ready(self, payload: dict) -> None:
        if self.base is not None or payload.get("pointerSize") != 4:
            raise CaptureError("invalid_agent_ready")
        base = _address(payload.get("base"))
        if base == 0 or base + FUNCTION_RVA > 0xFFFFFFFF:
            raise CaptureError("invalid_agent_ready")
        if _address(payload.get("functionAddress")) != base + FUNCTION_RVA:
            raise CaptureError("agent_address_mismatch")
        self.base = base

    def record(self, payload: dict, data: bytes | None) -> None:
        name = payload.get("table")
        if name not in TABLES or self.base is None or name in self.records:
            raise CaptureError("unexpected_master_message")
        if not isinstance(data, bytes) or type(payload.get("length")) is not int or len(data) != payload["length"]:
            raise CaptureError("invalid_master_length")
        entry = TABLES[name]
        if _address(payload.get("returnAddress")) != self.base + entry["callRva"] + 5:
            raise CaptureError("agent_address_mismatch")
        if _address(payload.get("functionAddress")) != self.base + FUNCTION_RVA:
            raise CaptureError("agent_address_mismatch")
        metadata = validate_master(name, data, self.schemas)
        path = self.directory / (name + ".xml")
        with path.open("xb") as stream:
            stream.write(data)
        self.records[name] = {"path": path.name, "sha256": hashlib.sha256(data).hexdigest(),
                              "bytes": len(data), "captured_utc": utc_now(),
                              "function_rva": hex(FUNCTION_RVA), "function_address": hex(self.base + FUNCTION_RVA),
                              "return_rva": hex(entry["callRva"] + 5),
                              "return_address": hex(self.base + entry["callRva"] + 5), **metadata}

    def finish(self, status: str, error_code: str | None = None) -> Path:
        if self.finished:
            raise CaptureError("manifest_already_written")
        manifest = {"schema_version": 1, "input_sha256": self.identity["sha256"],
                    "complete": status == "complete" and len(self.records) == len(TABLES),
                    "status": status, "source_executable": self.identity,
                    "pid": self.pid, "started_utc": self.started, "finished_utc": utc_now(),
                    "tables": self.records, "missing_tables": sorted(TABLES.keys() - self.records.keys())}
        if error_code is not None:
            manifest["error_code"] = error_code
        target = self.directory / "manifest.json"
        with target.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        self.finished = True
        return target


def _address(value) -> int:
    if not isinstance(value, str) or not value.startswith("0x") or not 3 <= len(value) <= 10:
        raise CaptureError("invalid_agent_address")
    if any(character not in "0123456789abcdefABCDEF" for character in value[2:]):
        raise CaptureError("invalid_agent_address")
    try:
        return int(value, 16)
    except ValueError as exc:
        raise CaptureError("invalid_agent_address") from exc


def agent_source(identity: dict) -> str:
    config = {"exePath": identity["path"], "functionRva": FUNCTION_RVA, "functionBytes": list(FUNCTION_BYTES),
              "maxBytes": MAX_BYTES, "tables": {name: {key: table[key] for key in ("callRva", "callBytes")} for name, table in TABLES.items()}}
    return "const CAPTURE_CONFIG = " + json.dumps(config) + ";\n" + Path(__file__).with_suffix(".js").read_text(encoding="utf-8")


def check_capture_window(stop_event=None, deadline=None, *, clock=time.monotonic):
    if stop_event is not None and stop_event.is_set():
        raise KeyboardInterrupt
    if deadline is not None and clock() >= deadline:
        raise CaptureError("capture_deadline_expired")


def receive_until(events: queue.Queue, deadline: float, *, clock: Callable[[], float] = time.monotonic,
                  stop_event: threading.Event | None = None) -> Iterator:
    while (remaining := deadline - clock()) > 0:
        if stop_event is not None and stop_event.is_set():
            raise KeyboardInterrupt
        try:
            event = events.get(timeout=min(EVENT_POLL_SECONDS, remaining))
        except queue.Empty:
            continue
        if stop_event is not None and stop_event.is_set():
            raise KeyboardInterrupt
        yield event
    # Events already queued at the deadline still count; later ones cannot extend the capture.
    for _ in range(events.qsize()):
        if stop_event is not None and stop_event.is_set():
            raise KeyboardInterrupt
        yield events.get_nowait()
    if stop_event is not None and stop_event.is_set():
        raise KeyboardInterrupt


def capture_process(device, pid: int, expected_exe: Path, output: Path, schemas: dict[str, set[str]],
                    timeout: float, *, path_query=query_process_image, workspace: Path = WORKSPACE_ROOT,
                    progress: Callable[[dict], None] | None = None,
                    clock: Callable[[], float] = time.monotonic) -> Path:
    identity = verify_process(pid, expected_exe, path_query)
    writer = CaptureWriter(output, identity, pid, schemas, workspace=workspace)
    events: queue.Queue = queue.Queue(maxsize=8)
    overflow = threading.Event()
    session = None

    def on_message(message, data=None):
        try:
            events.put_nowait((message, data))
        except queue.Full:
            overflow.set()

    try:
        session = device.attach(pid)
        verify_process(pid, expected_exe, path_query)
        session.on("detached", lambda *_: on_message({"type": "detached"}))
        script = session.create_script(agent_source(identity))
        script.on("message", on_message)
        script.load()
        deadline = clock() + timeout
        for message, data in receive_until(events, deadline, clock=clock):
            if overflow.is_set():
                raise CaptureError("event_queue_overflow")
            if not isinstance(message, dict) or message.get("type") != "send":
                raise CaptureError("process_detached" if isinstance(message, dict) and message.get("type") == "detached" else "agent_runtime_error")
            payload = message.get("payload")
            if not isinstance(payload, dict):
                raise CaptureError("invalid_agent_message")
            event = payload.get("event")
            if event == "ready":
                writer.ready(payload)
                if progress is not None:
                    progress({"event": "ready", "pid": pid})
            elif event == "master":
                writer.record(payload, data)
                if progress is not None:
                    name = payload["table"]
                    record = writer.records[name]
                    progress({"event": "captured", "table": name, "bytes": record["bytes"], "rows": record["rows"]})
            elif event == "error":
                code = payload.get("code")
                raise CaptureError(code if code in _AGENT_ERRORS else "agent_runtime_error")
            else:
                raise CaptureError("unexpected_agent_message")
            if len(writer.records) == len(TABLES):
                return writer.finish("complete")
        raise CaptureError("capture_timeout")
    except BaseException as exc:
        if not writer.finished:
            code = exc.code if isinstance(exc, CaptureError) else "capture_interrupted" if isinstance(exc, KeyboardInterrupt) else "capture_runtime_error"
            writer.finish("incomplete", code)
        raise
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


def wait_for_process(device, expected_exe: Path, timeout: float, *, path_query=query_process_image,
                     excluded_pids: Collection[int] | None = None) -> int:
    excluded = frozenset(excluded_pids or ())
    deadline = time.monotonic() + timeout
    while True:
        matching = []
        for process in device.enumerate_processes(scope="minimal"):
            if process.pid in excluded or process.name.lower() != expected_exe.name.lower():
                continue
            try:
                image = path_query(process.pid)
            except CaptureError:
                continue
            if _path_key(image) == _path_key(expected_exe):
                verify_original_executable(image)
                matching.append(process.pid)
        if len(matching) > 1:
            raise CaptureError("multiple_matching_processes")
        if matching:
            return matching[0]
        if (remaining := deadline - time.monotonic()) <= 0:
            raise CaptureError("process_wait_timeout")
        time.sleep(min(PROCESS_POLL_SECONDS, remaining))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture only the two public master tables from the verified original client.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Validate local files without attaching to any process")
    mode.add_argument("--pid", type=int)
    mode.add_argument("--wait", action="store_true", help="Wait for the user to start the original client")
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600 or args.pid is not None and args.pid <= 0:
        parser.error("PID must be positive and timeout must be between 0 and 3600 seconds")
    try:
        identity = verify_original_executable(args.exe)
        schemas = load_public_schemas(Path(identity["path"]).parent)
        if args.check:
            output = _safe_output(args.output, WORKSPACE_ROOT, create=False)
            source = agent_source(identity)
            print(json.dumps({"status": "checked", "attached": False, "source_executable": identity,
                              "schema_fields": {name: len(fields) for name, fields in schemas.items()},
                              "function_rva": hex(FUNCTION_RVA), "output_parent": str(output),
                              "agent_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest()}, indent=2))
            return 0
        frida = importlib.import_module("frida")
        device = frida.get_local_device()

        def progress(event: dict) -> None:
            print(json.dumps(event), flush=True)

        if args.wait:
            existing_pids = {process.pid for process in device.enumerate_processes(scope="minimal")}
            progress({"event": "waiting_for_process", "process": args.exe.name})
            pid = wait_for_process(device, args.exe, args.timeout, excluded_pids=existing_pids)
        else:
            pid = args.pid
        result = capture_process(device, pid, args.exe, args.output, schemas, args.timeout, progress=progress)
        print(json.dumps({"status": "complete", "manifest": str(result)}), flush=True)
        return 0
    except KeyboardInterrupt:
        print("Capture stopped.", file=sys.stderr)
        return 130
    except Exception as exc:
        code = exc.code if isinstance(exc, CaptureError) else "missing_frida" if isinstance(exc, ModuleNotFoundError) and exc.name == "frida" else "capture_setup_failed"
        print("Capture failed: " + code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
