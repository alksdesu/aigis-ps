from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
from pathlib import Path
import queue
import threading
import time
import uuid

if __package__:
    from . import capture_masters as masters
    from . import capture_resources as resources
else:
    import capture_masters as masters
    import capture_resources as resources


DEFAULT_OUTPUT = masters.DEFAULT_OUTPUT
SOURCES = ("resources", "masters")


class ContentCaptureError(masters.CaptureError):
    def __init__(self, code: str, bundle_path: Path, status: str):
        super().__init__(code)
        self.bundle_path = bundle_path
        self.status = status


def _write_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _prepare(executable: Path) -> tuple[dict, dict, dict[str, str]]:
    identity = masters.verify_original_executable(executable)
    verified_path = Path(identity["path"])
    schemas = masters.load_public_schemas(verified_path.parent)
    config = resources.capture_config(verified_path)
    return identity, schemas, {
        "resources": resources.agent_source(config),
        "masters": masters.agent_source(identity),
    }


class BundleWriter:
    def __init__(self, parent: Path, identity: dict, pid: int, schemas: dict, *, workspace: Path):
        parent = masters._safe_output(parent, workspace)
        name = datetime.now(timezone.utc).strftime("content-%Y%m%dT%H%M%SZ") + f"-{pid}-{uuid.uuid4().hex[:8]}"
        self.directory = parent / name
        self.directory.mkdir(exist_ok=False)
        self.identity = identity
        self.pid = pid
        self.started = masters.utc_now()
        self.masters = masters.CaptureWriter(self.directory / "masters", identity, pid, schemas, workspace=workspace)
        self.resource_base: int | None = None
        self.resource_context: Path | None = None
        self.resource_version: str | None = None
        self.finished = False
        self.status = "running"

    @property
    def complete(self) -> bool:
        return len(self.masters.records) == len(masters.TABLES) and self.resource_context is not None

    @property
    def ready(self) -> bool:
        return self.masters.base is not None and self.resource_base is not None

    def source_ready(self, source: str, payload: dict) -> None:
        if source == "masters":
            self.masters.ready(payload)
            if self.resource_base is not None and self.masters.base != self.resource_base:
                raise masters.CaptureError("content_module_mismatch")
        elif source == "resources":
            if self.resource_base is not None:
                raise masters.CaptureError("duplicate_resource_ready")
            base = masters._address(payload.get("base"))
            if base == 0 or base + resources.FUNCTION_RVA > 0xFFFFFFFF:
                raise masters.CaptureError("invalid_resource_ready")
            if masters._address(payload.get("functionAddress")) != base + resources.FUNCTION_RVA:
                raise masters.CaptureError("invalid_resource_ready")
            if self.masters.base is not None and base != self.masters.base:
                raise masters.CaptureError("content_module_mismatch")
            self.resource_base = base
        else:
            raise masters.CaptureError("unexpected_content_source")

    def version(self, payload: dict) -> None:
        if self.resource_base is None or self.resource_context is not None:
            raise masters.CaptureError("unexpected_resource_version")
        version = resources.validate_version(payload, self.resource_base)
        record = {"schema_version": 1, "complete": True, "input_sha256": self.identity["sha256"],
                  "version": version, "index_name": resources.INDEX_NAME, "origin": resources.CDN_ORIGIN,
                  "captured_at": masters.utc_now(), "pid": self.pid, "function_rva": hex(resources.FUNCTION_RVA),
                  "return_rva": hex(int(payload["returnAddress"], 16) - self.resource_base)}
        path = self.directory / "resource-context.json"
        _write_json(path, record)
        self.resource_context = path
        self.resource_version = version

    def finish(self, error_code: str | None = None) -> Path:
        if self.finished:
            raise masters.CaptureError("bundle_already_written")
        master_complete = len(self.masters.records) == len(masters.TABLES)
        manifest = self.masters.finish("complete" if master_complete else "incomplete", None if master_complete else error_code)
        complete = self.complete and error_code is None
        self.status = "complete" if complete else "partial" if self.masters.records or self.resource_context else "failed"
        record = {"schema_version": 1, "input_sha256": self.identity["sha256"], "complete": complete,
                  "status": self.status, "pid": self.pid, "started_utc": self.started, "finished_utc": masters.utc_now(),
                  "master_manifest": manifest.relative_to(self.directory).as_posix(),
                  "master_tables_complete": master_complete,
                  "resource_context": self.resource_context.relative_to(self.directory).as_posix() if self.resource_context else None,
                  "resource_version": self.resource_version,
                  "captured_tables": sorted(self.masters.records),
                  "missing": sorted(set(masters.TABLES) - self.masters.records.keys()) + ([] if self.resource_context else ["AssetPath"])}
        if error_code is not None:
            record["error_code"] = error_code
        path = self.directory / "bundle.json"
        _write_json(path, record)
        self.finished = True
        return path


def _message(bundle: BundleWriter, source: str, message, data, progress) -> None:
    if source == "session":
        raise masters.CaptureError("process_detached")
    if source not in SOURCES or not isinstance(message, dict) or message.get("type") != "send":
        raise masters.CaptureError("content_agent_error")
    payload = message.get("payload")
    if not isinstance(payload, dict):
        raise masters.CaptureError("invalid_content_message")
    event = payload.get("event")
    if data is not None and not (source == "masters" and event == "master"):
        raise masters.CaptureError("unexpected_content_body")
    if event == "ready":
        was_ready = bundle.ready
        bundle.source_ready(source, payload)
        if bundle.ready and not was_ready and progress is not None:
            progress({"event": "ready", "pid": bundle.pid, "scope": "public_content"})
    elif source == "masters" and event == "master":
        bundle.masters.record(payload, data)
        if progress is not None:
            name = payload["table"]
            record = bundle.masters.records[name]
            progress({"event": "captured", "table": name, "bytes": record["bytes"], "rows": record["rows"]})
    elif source == "masters" and event == "complete":
        if len(bundle.masters.records) != len(masters.TABLES):
            raise masters.CaptureError("premature_master_completion")
    elif source == "resources" and event == "version":
        bundle.version(payload)
        if progress is not None:
            progress({"event": "captured", "scope": "public_resource_version", "version": bundle.resource_version})
    elif event == "error":
        allowed = masters._AGENT_ERRORS if source == "masters" else resources.ERRORS
        code = payload.get("code")
        raise masters.CaptureError(code if isinstance(code, str) and code in allowed else "content_agent_error")
    else:
        raise masters.CaptureError("unexpected_content_message")


def capture_bundle(device, pid: int, executable: Path, output: Path, timeout: float, *,
                   progress: Callable[[dict], None] | None = None,
                   path_query=masters.query_process_image, workspace: Path = masters.WORKSPACE_ROOT,
                   clock: Callable[[], float] = time.monotonic,
                   stop_event: threading.Event | None = None, capture_deadline: float | None = None) -> Path:
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise masters.CaptureError("invalid_capture_timeout")
    if capture_deadline is not None and (type(capture_deadline) not in (int, float) or not math.isfinite(capture_deadline)):
        raise masters.CaptureError("invalid_capture_deadline")
    identity = masters.verify_process(pid, executable, path_query)
    _, schemas, sources = _prepare(Path(identity["path"]))
    bundle = BundleWriter(output, identity, pid, schemas, workspace=workspace)
    events: queue.Queue = queue.Queue(maxsize=16)
    overflow = threading.Event()
    session = None

    def received(source, message, data=None):
        try:
            events.put_nowait((source, message, data))
        except queue.Full:
            overflow.set()

    try:
        masters.check_capture_window(stop_event, capture_deadline, clock=clock)
        session = device.attach(pid)
        masters.verify_process(pid, executable, path_query)
        masters.check_capture_window(stop_event, capture_deadline, clock=clock)
        session.on("detached", lambda *_: received("session", {"type": "detached"}))
        scripts = []
        for source in SOURCES:
            script = session.create_script(sources[source])
            script.on("message", lambda message, data=None, source=source: received(source, message, data))
            scripts.append(script)
        for script in scripts:
            masters.check_capture_window(stop_event, capture_deadline, clock=clock)
            script.load()
        masters.check_capture_window(stop_event, capture_deadline, clock=clock)
        deadline = clock() + timeout
        if capture_deadline is not None:
            deadline = min(deadline, capture_deadline)
        for source, message, data in masters.receive_until(events, deadline, clock=clock, stop_event=stop_event):
            if overflow.is_set():
                raise masters.CaptureError("content_queue_overflow")
            _message(bundle, source, message, data, progress)
            if bundle.complete:
                return bundle.finish()
        raise masters.CaptureError("content_capture_timeout")
    except BaseException as error:
        code = error.code if isinstance(error, masters.CaptureError) else "capture_interrupted" if isinstance(error, KeyboardInterrupt) else "content_capture_runtime_error"
        if bundle.finished:
            raise
        try:
            path = bundle.finish(code)
        except Exception:
            raise ContentCaptureError("capture_finalize_failed", bundle.directory, "failed") from error
        raise ContentCaptureError(code, path, bundle.status) from error
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture public master tables and the resource version in one original-client session")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Validate both agents and schemas without attaching")
    mode.add_argument("--wait", action="store_true", help="Wait for a newly started original client")
    mode.add_argument("--pid", type=int)
    parser.add_argument("--exe", type=Path, default=masters.DEFAULT_EXE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600 or args.pid is not None and args.pid <= 0:
        parser.error("PID must be positive and timeout must be between 0 and 3600 seconds")
    try:
        identity, schemas, sources = _prepare(args.exe)
        output = masters._safe_output(args.output, masters.WORKSPACE_ROOT, create=False)
        if args.check:
            print(json.dumps({"status": "checked", "attached": False, "input_sha256": identity["sha256"],
                              "scope": [*masters.TABLES, "AssetPath"], "output": str(output),
                              "schema_fields": {name: len(fields) for name, fields in schemas.items()},
                              "agent_sha256": {name: hashlib.sha256(source.encode("utf-8")).hexdigest() for name, source in sources.items()}}, indent=2))
            return 0
        device = importlib.import_module("frida").get_local_device()
        deadline = time.monotonic() + args.timeout

        def progress(event: dict) -> None:
            print(json.dumps(event), flush=True)

        if args.wait:
            existing = {process.pid for process in device.enumerate_processes(scope="minimal")}
            progress({"event": "waiting_for_process", "scope": "public_content"})
            pid = masters.wait_for_process(device, args.exe, max(0, deadline - time.monotonic()), excluded_pids=existing)
        else:
            pid = args.pid
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise masters.CaptureError("content_capture_timeout")
        result = capture_bundle(device, pid, args.exe, output, remaining, progress=progress)
        print(json.dumps({"status": "complete", "complete": True, "bundle": str(result)}), flush=True)
        return 0
    except ContentCaptureError as error:
        print(json.dumps({"status": error.status, "complete": False, "code": error.code,
                          "bundle": str(error.bundle_path)}), flush=True)
        return 130 if error.code == "capture_interrupted" else 1
    except KeyboardInterrupt:
        print(json.dumps({"status": "failed", "complete": False, "code": "capture_interrupted"}), flush=True)
        return 130
    except Exception as error:
        code = error.code if isinstance(error, masters.CaptureError) else "missing_frida" if isinstance(error, ModuleNotFoundError) and error.name == "frida" else "content_capture_setup_failed"
        print(json.dumps({"status": "failed", "complete": False, "code": code}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
