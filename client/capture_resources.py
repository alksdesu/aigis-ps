from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import queue
import re
import struct
import threading
import time
import uuid

if __package__:
    from .capture_masters import (
        CaptureError, DEFAULT_EXE, EXPECTED_SHA256, PROJECT_ROOT, WORKSPACE_ROOT,
        _image_bytes, _safe_output, query_process_image, receive_until, utc_now,
        verify_original_executable, verify_process, wait_for_process,
    )
else:
    from capture_masters import (
        CaptureError, DEFAULT_EXE, EXPECTED_SHA256, PROJECT_ROOT, WORKSPACE_ROOT,
        _image_bytes, _safe_output, query_process_image, receive_until, utc_now,
        verify_original_executable, verify_process, wait_for_process,
    )


FUNCTION_RVA = 0x239B30
FUNCTION_BYTES = bytes.fromhex("558bec6aff689050c80064a1000000005083ec48a1a8e8f10033c58945ec5356")
CALLERS = (0x23A19F, 0x23A1F1)
RELOCATIONS = ({"offset": 6, "rva": 0x885090}, {"offset": 21, "rva": 0xB1E8A8})
CDN_ORIGIN = "https://drc1bk94f7rq8.cloudfront.net/"
INDEX_NAME = "5nhhyzpc90mlwd9i06s8nfz781im220y"
DEFAULT_OUTPUT = PROJECT_ROOT / "runtime" / "resource-contexts"
ERRORS = {"unsupported_process", "signature_mismatch", "unexpected_asset_version", "read_failed", "setup_failed"}


def capture_config(executable: Path) -> dict:
    identity = verify_original_executable(executable)
    raw = executable.read_bytes()
    if _image_bytes(raw, FUNCTION_RVA, len(FUNCTION_BYTES)) != FUNCTION_BYTES:
        raise CaptureError("resource_signature_mismatch")
    callers = []
    for rva in CALLERS:
        expected = b"\xe8" + struct.pack("<i", FUNCTION_RVA - rva - 5)
        if _image_bytes(raw, rva, 5) != expected:
            raise CaptureError("resource_caller_mismatch")
        callers.append({"rva": rva, "bytes": list(expected)})
    return {"exePath": identity["path"], "input_sha256": identity["sha256"], "functionRva": FUNCTION_RVA,
            "functionBytes": list(FUNCTION_BYTES), "callers": callers, "relocations": RELOCATIONS}


def agent_source(config: dict) -> str:
    return "const RESOURCE_CAPTURE_CONFIG = " + json.dumps(config) + ";\n" + Path(__file__).with_suffix(".js").read_text(encoding="utf-8")


def validate_version(payload: dict, base: int) -> str:
    value = payload.get("version")
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value):
        raise CaptureError("unexpected_asset_version")
    if payload.get("returnAddress") not in {hex(base + rva + 5) for rva in CALLERS}:
        raise CaptureError("resource_caller_mismatch")
    return value


def capture_context(device, pid: int, executable: Path, output: Path, timeout: float, *, progress=None,
                    path_query=query_process_image, clock=time.monotonic) -> Path:
    identity = verify_process(pid, executable, path_query=path_query)
    config = capture_config(executable)
    directory = _safe_output(output, WORKSPACE_ROOT)
    events = queue.Queue(maxsize=16)
    overflow = threading.Event()
    session = None
    base = None
    try:
        session = device.attach(pid)
        script = session.create_script(agent_source(config))

        def received(message, data=None):
            try:
                events.put_nowait(message)
            except queue.Full:
                overflow.set()

        script.on("message", received)
        session.on("detached", lambda *args: received({"type": "detached"}))
        script.load()
        deadline = clock() + timeout
        for message in receive_until(events, deadline, clock=clock):
            if overflow.is_set():
                raise CaptureError("resource_queue_overflow")
            if not isinstance(message, dict) or message.get("type") != "send":
                raise CaptureError("resource_capture_detached")
            payload = message.get("payload")
            if not isinstance(payload, dict):
                raise CaptureError("invalid_resource_message")
            if payload.get("event") == "ready":
                value = payload.get("base")
                if base is not None or not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-f]{1,8}", value):
                    raise CaptureError("invalid_resource_ready")
                base = int(value, 16)
                if base == 0 or payload.get("functionAddress") != hex(base + FUNCTION_RVA):
                    raise CaptureError("invalid_resource_ready")
                if progress:
                    progress({"event": "ready", "pid": pid, "scope": "public_resource_version"})
            elif payload.get("event") == "version" and base is not None:
                version = validate_version(payload, base)
                record = {"schema_version": 1, "complete": True, "input_sha256": identity["sha256"],
                          "version": version, "index_name": INDEX_NAME, "origin": CDN_ORIGIN,
                          "captured_at": utc_now(), "pid": pid, "function_rva": hex(FUNCTION_RVA),
                          "return_rva": hex(int(payload["returnAddress"], 16) - base)}
                path = directory / (version + "-" + uuid.uuid4().hex[:8] + ".json")
                with path.open("x", encoding="utf-8") as stream:
                    json.dump(record, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                return path
            elif payload.get("event") == "error":
                code = payload.get("code")
                raise CaptureError(code if code in ERRORS else "resource_agent_error")
            else:
                raise CaptureError("invalid_resource_message")
        raise CaptureError("resource_capture_timeout")
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Capture the public asset-index version, without account responses")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--wait", action="store_true")
    mode.add_argument("--pid", type=int)
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 3600 or args.pid is not None and args.pid <= 0:
        parser.error("Timeout or process ID is out of range")
    try:
        config = capture_config(args.exe)
        source = agent_source(config)
        if args.check:
            print(json.dumps({"status": "checked", "attached": False, "input_sha256": config["input_sha256"],
                              "output": str(_safe_output(args.output, WORKSPACE_ROOT, create=False)),
                              "function_rva": hex(FUNCTION_RVA), "agent_sha256": hashlib.sha256(source.encode()).hexdigest()}))
            return 0
        device = importlib.import_module("frida").get_local_device()
        progress = lambda message: print(json.dumps(message), flush=True)
        if args.wait:
            existing = {process.pid for process in device.enumerate_processes(scope="minimal")}
            progress({"event": "waiting_for_process", "scope": "public_resource_version"})
            pid = wait_for_process(device, args.exe, args.timeout, excluded_pids=existing)
        else:
            pid = args.pid
        result = capture_context(device, pid, args.exe, args.output, args.timeout, progress=progress)
        print(json.dumps({"status": "complete", "context": str(result)}), flush=True)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(json.dumps({"status": "failed", "code": error.code if isinstance(error, CaptureError) else "resource_capture_failed"}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
