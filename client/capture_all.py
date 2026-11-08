from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
from pathlib import Path
import signal
import threading
import time
import uuid

if __package__:
    from . import capture_content as content
    from . import capture_masters as masters
    from . import capture_responses as responses
    from . import capture_schedule as schedule
else:
    import capture_content as content
    import capture_masters as masters
    import capture_responses as responses
    import capture_schedule as schedule


DEFAULT_OUTPUT = masters.PROJECT_ROOT / "runtime" / "capture-runs"
DEFAULT_TIMEOUT = 7200
DEFAULT_CONTENT_TIMEOUT = 600
MAX_CONTENT_TIMEOUT = 3600


def _valid_timeout(value, maximum):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum


def _write_json(path, value):
    temporary = path.with_name(f"{path.stem}-{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


class ScheduleExport:
    def __init__(self, output, identity, pid, *, workspace):
        self.writer = schedule.ScheduleWriter(output, identity, pid, workspace=workspace)
        self.workspace = workspace
        self.source = None
        self.path = None
        self.error_code = None
        self.closed = False

    def _reject(self, operation):
        self.writer.reject({"operation": operation, "code": "invalid_schedule_payload"})

    def _read(self, event):
        operation, sequence = event["operation"], event.get("sequence")
        if self.source is None or type(sequence) is not int or not 0 < sequence <= 0x7FFFFFFF:
            raise masters.CaptureError("invalid_schedule_source")
        name = f"{sequence:05d}-{operation}.xml"
        if event.get("path") != name:
            raise masters.CaptureError("invalid_schedule_source")
        for key, expected in (("function_rva", schedule.FUNCTION_RVA), ("return_rva", schedule.RETURN_RVA),
                              ("vtable_rva", schedule.VTABLES[operation])):
            if event.get(key) != hex(expected):
                raise masters.CaptureError("schedule_address_mismatch")
        path = masters._safe_output(self.source / name, self.workspace, create=False)
        size = event.get("bytes")
        if type(size) is not int or not 0 < size <= schedule.SCHEDULE_LIMITS.max_bytes or path.stat().st_size != size:
            raise masters.CaptureError("invalid_schedule_length")
        with path.open("rb") as stream:
            data = stream.read(size + 1)
        if len(data) != size or hashlib.sha256(data).hexdigest() != event.get("sha256"):
            raise masters.CaptureError("invalid_schedule_source")
        return data, {"source_response": str(path), "source_sequence": sequence,
                      "source_sha256": event["sha256"], "function_rva": hex(schedule.FUNCTION_RVA),
                      "return_rva": hex(schedule.RETURN_RVA), "vtable_rva": hex(schedule.VTABLES[operation])}

    def observe(self, event):
        if self.closed:
            return
        operation = event.get("operation")
        try:
            if event.get("event") == "ready":
                self.source = masters._safe_output(Path(event["directory"]), self.workspace, create=False)
            elif type(operation) is int and operation in schedule.VTABLES:
                if event.get("event") in ("rejected", "excluded"):
                    self._reject(operation)
                elif event.get("event") == "captured":
                    try:
                        data, provenance = self._read(event)
                        self.writer.derive(operation, event["sequence"], data, provenance)
                    except (schedule.ScheduleCaptureError, masters.CaptureError) as error:
                        if error.code == "schedule_capture_capacity":
                            raise
                        self._reject(operation)
                    if self.writer.complete:
                        self.finish()
        except Exception as error:
            self.error_code = error.code if isinstance(error, masters.CaptureError) else "schedule_export_failed"
            self.finish()

    def finish(self):
        if self.closed:
            return self.path
        try:
            self.path = self.writer.finish(self.error_code)
        except Exception:
            self.error_code = "schedule_finalize_failed"
        self.closed = True
        return self.path


def _component(path, error_code=None):
    result = {"path": str(path) if path is not None else None, "status": "failed", "complete": False}
    if path is not None:
        try:
            record = json.loads(Path(path).read_text(encoding="utf-8"))
            if (not isinstance(record, dict) or record.get("status") not in ("complete", "stopped", "partial", "failed")
                    or "complete" in record and type(record["complete"]) is not bool
                    or any(key in record and not isinstance(record[key], str) for key in ("end_reason", "error_code"))
                    or "records" in record and not (isinstance(record["records"], list)
                        or type(record["records"]) is int and record["records"] >= 0)):
                raise ValueError("invalid_capture_manifest")
            for key in ("status", "complete", "end_reason", "error_code"):
                if key in record:
                    result[key] = record[key]
            if type(record.get("records")) is int:
                result["records"] = record["records"]
        except (OSError, ValueError, TypeError):
            error_code = "capture_manifest_unreadable"
    if error_code is not None:
        result["error_code"] = error_code
        result["complete"] = False
        if result["status"] in ("complete", "stopped"):
            result["status"] = "partial"
    return result


def capture_run(device, pid, executable, output, timeout, *, content_timeout=DEFAULT_CONTENT_TIMEOUT,
                progress=None, path_query=masters.query_process_image, workspace=masters.WORKSPACE_ROOT,
                clock=time.monotonic, stop_event=None):
    stopped = stop_event if stop_event is not None else threading.Event()
    previous_handler = None
    if threading.current_thread() is threading.main_thread():
        previous_handler = signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        return _capture_run(device, pid, executable, output, timeout, content_timeout=content_timeout,
                            progress=progress, path_query=path_query, workspace=workspace, clock=clock, stop_event=stopped)
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGINT, previous_handler)


def _capture_run(device, pid, executable, output, timeout, *, content_timeout, progress, path_query,
                 workspace, clock, stop_event):
    if not _valid_timeout(timeout, responses.MAX_TIMEOUT) or not _valid_timeout(content_timeout, MAX_CONTENT_TIMEOUT):
        raise masters.CaptureError("invalid_capture_timeout")
    deadline = clock() + timeout
    identity = masters.verify_process(pid, executable, path_query)
    parent = masters._safe_output(output, workspace)
    directory = parent / (datetime.now(timezone.utc).strftime("capture-%Y%m%dT%H%M%SZ") + f"-{pid}-{uuid.uuid4().hex[:8]}")
    directory.mkdir(exist_ok=False)
    path = directory / "run.json"
    summary = {"schema_version": 1, "status": "running", "pid": pid, "source_executable": identity,
               "started_utc": masters.utc_now(), "coverage_verified": False, "components": {}}
    _write_json(path, summary)
    try:
        exported = ScheduleExport(schedule.DEFAULT_OUTPUT, identity, pid, workspace=workspace)
    except Exception:
        exported = None
    progress_lock = threading.Lock()

    def report(source, event):
        if progress is not None:
            with progress_lock:
                progress({**event, "source": source})

    def collect(source):
        manifest = None

        def update(event):
            nonlocal manifest
            if source == "responses":
                if "manifest" in event:
                    manifest = Path(event["manifest"])
                if exported is not None:
                    exported.observe(event)
            report(source, event)

        try:
            if stop_event.is_set():
                raise KeyboardInterrupt
            remaining = deadline - clock()
            if remaining <= 0:
                raise masters.CaptureError("capture_timeout")
            common = {"progress": update, "path_query": path_query, "workspace": workspace,
                      "clock": clock, "stop_event": stop_event, "capture_deadline": deadline}
            if source == "content":
                manifest = content.capture_bundle(device, pid, executable, content.DEFAULT_OUTPUT,
                                                  min(content_timeout, remaining), **common)
            else:
                manifest = responses.capture_process(device, pid, executable, responses.DEFAULT_OUTPUT,
                                                     remaining, responses.Selection("all"), **common)
            return _component(manifest)
        except content.ContentCaptureError as error:
            return _component(error.bundle_path, error.code)
        except KeyboardInterrupt:
            return _component(manifest, None if manifest is not None else "capture_interrupted")
        except Exception as error:
            code = error.code if isinstance(error, (masters.CaptureError, responses.ResponseCaptureError)) else "capture_worker_failed"
            return _component(manifest, code)

    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="capture")
    futures = {}
    try:
        for source in ("content", "responses"):
            futures[source] = executor.submit(collect, source)
        pending = set(futures.values())
        while pending:
            try:
                _, pending = wait(pending, timeout=masters.EVENT_POLL_SECONDS)
            except KeyboardInterrupt:
                stop_event.set()
        summary["components"] = {source: future.result() for source, future in futures.items()}
    finally:
        executor.shutdown(wait=True)
    summary["components"]["schedule"] = (_component(exported.finish(), exported.error_code) if exported is not None
                                          else _component(None, "schedule_setup_failed"))
    components = summary["components"]
    if stop_event.is_set():
        summary["status"] = "interrupted"
    elif (components["content"]["complete"] and components["schedule"]["complete"]
          and components["responses"]["status"] == "stopped" and components["responses"].get("records", 0) > 0):
        summary["status"] = "stopped"
    else:
        summary["status"] = "partial" if any(item["status"] in ("complete", "partial")
            or item.get("records", 0) > 0 for item in components.values()) else "failed"
    summary["finished_utc"] = masters.utc_now()
    _write_json(path, summary)
    report("run", {"event": "finished", "status": summary["status"], "manifest": str(path), "coverage_verified": False})
    return path


def _prepare(executable):
    identity, schemas, sources = content._prepare(executable)
    response_identity, config = responses.profile(executable, responses.Selection("all"))
    schedule_identity, _ = schedule.profile(executable)
    if identity != response_identity or identity != schedule_identity:
        raise masters.CaptureError("capture_identity_mismatch")
    return identity, schemas, {**sources, "responses": responses.agent_source(config)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Capture public content and filtered responses together from a newly started original client.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--wait", action="store_true")
    parser.add_argument("--exe", type=Path, default=masters.DEFAULT_EXE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--content-timeout", type=float, default=DEFAULT_CONTENT_TIMEOUT)
    args = parser.parse_args(argv)
    if not _valid_timeout(args.timeout, responses.MAX_TIMEOUT) or not _valid_timeout(args.content_timeout, MAX_CONTENT_TIMEOUT):
        parser.error(f"timeout must be between 0 and {responses.MAX_TIMEOUT}, and content timeout between 0 and {MAX_CONTENT_TIMEOUT}")
    try:
        identity, schemas, sources = _prepare(args.exe)
        executable = Path(identity["path"])
        output = masters._safe_output(args.output, masters.WORKSPACE_ROOT, create=False)
        for destination in (content.DEFAULT_OUTPUT, responses.DEFAULT_OUTPUT, schedule.DEFAULT_OUTPUT):
            masters._safe_output(destination, masters.WORKSPACE_ROOT, create=False)
        if args.check:
            print(json.dumps({"status": "checked", "attached": False, "input_sha256": identity["sha256"],
                "output": str(output), "schema_fields": {key: len(value) for key, value in schemas.items()},
                "agent_sha256": {key: hashlib.sha256(value.encode()).hexdigest() for key, value in sources.items()}}, ensure_ascii=False))
            return 0
        device = importlib.import_module("frida").get_local_device()
        existing = {process.pid for process in device.enumerate_processes(scope="minimal")}
        deadline = time.monotonic() + args.timeout
        progress = lambda event: print(json.dumps(event, ensure_ascii=False), flush=True)
        progress({"event": "waiting_for_process", "process": executable.name, "new_process_only": True})
        pid = masters.wait_for_process(device, executable, args.timeout, excluded_pids=existing)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise masters.CaptureError("process_wait_timeout")
        path = capture_run(device, pid, executable, output, remaining,
                           content_timeout=args.content_timeout, progress=progress)
        result = json.loads(path.read_text(encoding="utf-8"))
        return 130 if result["status"] == "interrupted" else 0 if result["status"] == "stopped" else 1
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "code": "capture_interrupted"}), flush=True)
        return 130
    except Exception as error:
        code = error.code if isinstance(error, masters.CaptureError) else "missing_frida" if isinstance(error, ModuleNotFoundError) and error.name == "frida" else "capture_setup_failed"
        print(json.dumps({"status": "failed", "code": code}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
