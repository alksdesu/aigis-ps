from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
import threading
from urllib.error import HTTPError, URLError
import uuid

from .alxml import loads
from .local_io import atomic_replace, checked_path, exclusive_file, read_bounded, read_json_object
from .masterdata import MasterDataStore
from .public_tables import MASTER_LIMITS
from .resource_fetch import CDN_ORIGIN, PC_INDEX_NAME, ResourceFetcher, fetch_index, parse_index


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, value: object, root: Path) -> None:
    path = checked_path(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = checked_path(path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp"), root)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        atomic_replace(temporary, checked_path(path, root))
    finally:
        checked_path(temporary, root).unlink(missing_ok=True)


class ResourceLibrary:
    def __init__(self, root: Path, *, client_hash: str):
        self.root = checked_path(root)
        self.client_hash = client_hash

    def import_context(self, path: Path, *, progress=None) -> dict:
        with exclusive_file(self.root / "operation.lock"):
            return self._import_context(path, progress=progress)

    def _import_context(self, path: Path, *, progress=None) -> dict:
        context = read_json_object(path, limit=16384)
        if context.get("schema_version") != 1 or context.get("complete") is not True or context.get("input_sha256") != self.client_hash:
            raise ValueError("Resource context must be a completed capture for this client")
        version = context.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{40}", version):
            raise ValueError("Invalid captured resource version")
        if context.get("origin") != CDN_ORIGIN or context.get("index_name") != PC_INDEX_NAME:
            raise ValueError("Resource context does not describe the verified PC index")
        if progress:
            progress({"event": "fetching_resource_index", "version": version})
        raw, entries = fetch_index(version)
        if any(entry.native_cache_key is None for entry in entries):
            raise ValueError("A native PC resource index requires six-column cache identities")
        sha256 = hashlib.sha256(raw).hexdigest()
        snapshot = version + "-" + sha256[:16]
        directory = checked_path(self.root / "indexes" / snapshot, self.root)
        directory.mkdir(parents=True, exist_ok=True)
        payload = directory / "index.bin"
        if payload.exists():
            if read_bounded(payload, 32 * 1024 * 1024, root=self.root) != raw:
                raise ValueError("Immutable resource index has changed")
        else:
            with checked_path(payload, self.root).open("xb") as stream:
                stream.write(raw)
        manifest = {"schema_version": 1, "version": version, "snapshot": snapshot,
                    "index_name": PC_INDEX_NAME, "origin": CDN_ORIGIN, "input_sha256": self.client_hash,
                    "index_sha256": sha256, "index_bytes": len(raw), "entries": len(entries),
                    "total_bytes": sum(entry.size for entry in entries), "captured_at": context.get("captured_at"),
                    "activated_at": now(), "source_context": str(Path(path).resolve())}
        write_json(directory / "manifest.json", manifest, self.root)
        write_json(directory / "entries.json", [asdict(entry) for entry in entries], self.root)
        write_json(self.root / "active-index.json", {"snapshot": snapshot, "index_sha256": sha256}, self.root)
        return manifest

    def load(self):
        pointer = read_json_object(self.root / "active-index.json", limit=4096, root=self.root)
        snapshot = pointer.get("snapshot")
        if not isinstance(snapshot, str) or not re.fullmatch(r"[a-f0-9]{40}-[a-f0-9]{16}", snapshot):
            raise ValueError("Invalid active resource index identity")
        directory = self.root / "indexes" / snapshot
        manifest = read_json_object(directory / "manifest.json", root=self.root)
        if manifest.get("snapshot") != snapshot or manifest.get("input_sha256") != self.client_hash or manifest.get("index_name") != PC_INDEX_NAME or manifest.get("version") != snapshot[:40]:
            raise ValueError("Resource index metadata does not match this client")
        raw = read_bounded(directory / "index.bin", 32 * 1024 * 1024, root=self.root)
        if hashlib.sha256(raw).hexdigest() != pointer.get("index_sha256") or pointer["index_sha256"] != manifest.get("index_sha256"):
            raise ValueError("Resource index content has changed")
        entries = parse_index(raw)
        if any(entry.native_cache_key is None for entry in entries):
            raise ValueError("A native PC resource index requires six-column cache identities")
        return manifest, entries

    def status(self) -> dict:
        try:
            manifest, _ = self.load()
            return {"ready": True, **manifest}
        except (OSError, ValueError) as error:
            return {"ready": False, "code": "resource_index_unavailable", "message": str(error)}

    def plan(self, asset_root: Path, masters: MasterDataStore) -> dict:
        manifest, entries = self.load()
        return self._plan_snapshot(asset_root, masters, manifest, entries)

    def _plan_snapshot(self, asset_root, masters, manifest, entries) -> dict:
        from .resource_plan import build_plan
        document = loads(masters.get_xml("QuestTable"), limits=MASTER_LIMITS)
        columns = {column.name: column.values for column in document.children}
        quest_rows = [dict(zip(columns, values)) for values in zip(*columns.values())]
        report = build_plan(entries, asset_root, self.root, quest_rows=quest_rows)
        report.update(resource_version=manifest["version"], index_snapshot=manifest["snapshot"], generated_at=now())
        write_json(self.root / "collection-plan.json", report, self.root)
        return report

    def collect(self, asset_root: Path, masters: MasterDataStore, *, scope: str = "startup", workers: int = 4,
                max_bytes: int | None = None, progress=None) -> dict:
        with exclusive_file(self.root / "operation.lock"):
            return self._collect(asset_root, masters, scope=scope, workers=workers, max_bytes=max_bytes, progress=progress)

    def _collect(self, asset_root, masters, *, scope, workers, max_bytes, progress):
        if scope not in ("startup", "story", "maps", "all") or not 1 <= workers <= 8:
            raise ValueError("Invalid collection scope or worker count")
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
            raise ValueError("Collection byte limit must be positive")
        manifest, entries = self.load()
        plan = self._plan_snapshot(asset_root, masters, manifest, entries)
        by_name = {entry.name: entry for entry in entries}
        candidates = [row for row in plan["entries"] if scope in row["scopes"] and row["status"] not in ("available_source", "available_overlay")]
        candidates.sort(key=lambda row: (row["name"] != "Map9002.aar", "startup" not in row["scopes"], row["size"], row["name"]))
        selected, selected_bytes = [], 0
        for row in candidates:
            if max_bytes is not None and selected_bytes + row["size"] > max_bytes:
                continue
            selected.append(by_name[row["name"]])
            selected_bytes += row["size"]
        self.root.mkdir(parents=True, exist_ok=True)
        if selected_bytes + 2 * 1024 ** 3 > shutil.disk_usage(self.root).free:
            raise ValueError("Collection would leave less than 2 GiB of free disk space")
        job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        job = checked_path(self.root / "jobs" / job_id, self.root)
        job.mkdir(parents=True)
        state = {"schema_version": 1, "job_id": job_id, "status": "running", "scope": scope,
                 "resource_version": manifest["version"], "started_at": now(), "selected": len(selected),
                 "selected_bytes": selected_bytes, "remaining_due_to_budget": len(candidates) - len(selected),
                 "completed": 0, "downloaded_bytes": 0, "failed": [], "path": str(job)}
        write_json(job / "state.json", state, self.root)
        if progress:
            progress({"event": "collection_started", **state})

        worker_state = threading.local()
        connections = []
        connection_lock = threading.Lock()

        def download(entry):
            from .resource_fetch import PooledCdnOpener
            if not hasattr(worker_state, "fetcher"):
                connection = PooledCdnOpener()
                worker_state.fetcher = ResourceFetcher(self.root, opener=connection)
                with connection_lock:
                    connections.append(connection)
            fetcher = worker_state.fetcher
            for attempt in range(3):
                try:
                    return fetcher.download(entry)
                except (HTTPError, URLError, TimeoutError, OSError) as error:
                    retryable = not isinstance(error, HTTPError) or error.code in (408, 429, 500, 502, 503, 504)
                    if attempt == 2 or not retryable:
                        raise
                    delay = 2 ** attempt
                    if isinstance(error, HTTPError) and error.headers:
                        retry_after = error.headers.get("Retry-After", "")
                        if retry_after.isdecimal():
                            delay = max(delay, min(60, int(retry_after)))
                    time.sleep(delay)

        executor = ThreadPoolExecutor(max_workers=workers)
        pending = {}
        iterator = iter(selected)
        try:
            for _ in range(workers):
                entry = next(iterator, None)
                if entry is not None:
                    pending[executor.submit(download, entry)] = entry
            with (job / "events.jsonl").open("a", encoding="utf-8") as events:
                while pending:
                    done, _ = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                    for future in done:
                        entry = pending.pop(future)
                        try:
                            event = {"event": "resource_complete", **future.result()}
                            state["completed"] += 1
                            if event["status"] == "downloaded":
                                state["downloaded_bytes"] += event["bytes"]
                        except Exception as error:
                            event = {"event": "resource_failed", "name": entry.name,
                                     "code": getattr(error, "code", type(error).__name__), "message": str(error)}
                            state["failed"].append(event)
                        events.write(json.dumps({"at": now(), **event}, ensure_ascii=False) + "\n")
                        events.flush()
                        write_json(job / "state.json", state, self.root)
                        if progress and (event["event"] == "resource_failed" or state["completed"] % 25 == 0 or not pending):
                            progress({**event, "completed": state["completed"], "selected": state["selected"], "downloaded_bytes": state["downloaded_bytes"]})
                        entry = next(iterator, None)
                        if entry is not None:
                            pending[executor.submit(download, entry)] = entry
            state["status"] = "complete" if not state["failed"] and not state["remaining_due_to_budget"] else "partial"
        except BaseException:
            state["status"] = "interrupted"
            for future in pending:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            for connection in connections:
                connection.close()
            state["finished_at"] = now()
            write_json(job / "state.json", state, self.root)
        return state
