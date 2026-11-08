from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from .config_sets import ResolvedConfig
from .errors import ProtocolError
from .startup_diagnostics import StartupDiagnostics, StartupIssue


log = logging.getLogger("aigis_ps")
GENERATION_ID_LENGTH = 24
ADMIT_TIMEOUT_SECONDS = 10.0
DRAIN_TIMEOUT_SECONDS = 5.0
DRAINING_LIMIT_SECONDS = 30.0
CANDIDATE_TTL_SECONDS = 600.0
MAX_PROGRESS_ENTRIES = 32
MAX_PROGRESS_TEXT = 256
MAX_PORT = 65535
SOURCE_ROOT = Path(__file__).resolve().parent
# The client only sends these title-flow requests after a restart, so one proves a fresh session.
STARTUP_SEQUENCE = frozenset((31, 114, 142, 152))
DIAGNOSTICS_SECTION, DIAGNOSTICS_FIELD = "bootstrap", "startup_diagnostics"
_STARTUP_ROUTES = frozenset(map(str, STARTUP_SEQUENCE))


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def code_digest(root: Path = SOURCE_ROOT) -> str:
    files = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(Path(root).rglob("*.py"))}
    if not files:
        raise ValueError("Code digests need at least one source file")
    return hashlib.sha256(_canonical(files)).hexdigest()


def _optional_text(value, name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"Generation identity {name} must be a non-empty string or None")


@dataclass(frozen=True)
class GenerationIdentity:
    code_digest: str
    config_set: str
    content_versions: Mapping[str, str]
    client_fingerprint: str | None = None
    master_snapshot: str | None = None
    base_resource_version: str | None = None
    effective_resource_version: str | None = None

    def __post_init__(self):
        for name in ("code_digest", "config_set"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"Generation identity {name} must be a non-empty string")
        for name in ("client_fingerprint", "master_snapshot", "base_resource_version", "effective_resource_version"):
            _optional_text(getattr(self, name), name)
        if not isinstance(self.content_versions, Mapping) or any(
                not isinstance(domain, str) or not domain or not isinstance(version, str) or not version
                for domain, version in self.content_versions.items()):
            raise ValueError("Generation content versions map domain names to version strings")
        object.__setattr__(self, "content_versions", MappingProxyType(dict(sorted(self.content_versions.items()))))

    def to_dict(self) -> dict:
        result = {item.name: getattr(self, item.name) for item in fields(self)}
        result["content_versions"] = dict(self.content_versions)
        return result

    @property
    def id(self) -> str:
        return hashlib.sha256(_canonical(self.to_dict())).hexdigest()[:GENERATION_ID_LENGTH]

    def differences(self, other: GenerationIdentity) -> tuple[str, ...]:
        if not isinstance(other, GenerationIdentity):
            raise ValueError("Generation identities compare only with each other")
        changed = [item.name for item in fields(self)
                   if item.name != "content_versions" and getattr(self, item.name) != getattr(other, item.name)]
        domains = set(self.content_versions) | set(other.content_versions)
        changed.extend(f"content_versions.{domain}" for domain in sorted(domains)
                       if self.content_versions.get(domain) != other.content_versions.get(domain))
        return tuple(changed)


@dataclass(frozen=True)
class Generation:
    identity: GenerationIdentity
    bundle: object
    handlers: Mapping[int | str, Callable]
    readiness: Mapping[str, object]
    gate_issues: tuple[str, ...] = ()
    diagnostics: StartupDiagnostics | None = None

    def __post_init__(self):
        if not isinstance(self.identity, GenerationIdentity):
            raise ValueError("Generations need a generation identity")
        if not isinstance(self.handlers, Mapping) or any(not callable(handler) for handler in self.handlers.values()):
            raise ValueError("Generation handlers must map operations to callables")
        if not isinstance(self.readiness, Mapping):
            raise ValueError("Generation readiness must be a mapping")
        if self.diagnostics is not None and not isinstance(self.diagnostics, StartupDiagnostics):
            raise ValueError("Generation diagnostics must be startup diagnostics")
        object.__setattr__(self, "handlers", MappingProxyType(dict(self.handlers)))
        object.__setattr__(self, "readiness", MappingProxyType(deepcopy(dict(self.readiness))))
        object.__setattr__(self, "gate_issues", tuple(self.gate_issues))
        if any(not isinstance(issue, str) for issue in self.gate_issues):
            raise ValueError("Generation gate issues must be strings")

    @property
    def id(self) -> str:
        return self.identity.id

    def live_status(self) -> dict:
        # Startup diagnostics change while the generation serves, so they never come from the build-time cache.
        status = deepcopy(dict(self.readiness))
        section = status.get(DIAGNOSTICS_SECTION)
        if self.diagnostics is not None and isinstance(section, dict):
            section[DIAGNOSTICS_FIELD] = self.diagnostics.status()
        return status


@dataclass(frozen=True)
class GenerationInputs:
    config: ResolvedConfig
    data_dir: Path
    asset_dir: Path
    resource_dir: Path
    host: str
    port: int
    diagnostics: StartupDiagnostics

    def __post_init__(self):
        if not isinstance(self.config, ResolvedConfig):
            raise ValueError("Generation inputs need a resolved configuration")
        for name in ("data_dir", "asset_dir", "resource_dir"):
            if not isinstance(getattr(self, name), Path):
                raise ValueError(f"Generation input {name} must be a path")
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("Generation inputs need a host")
        if type(self.port) is not int or not 0 < self.port <= MAX_PORT:
            raise ValueError("Generation inputs need a TCP port")
        if not isinstance(self.diagnostics, StartupDiagnostics):
            raise ValueError("Generation inputs need the process startup diagnostics")

    @property
    def config_digest(self) -> str:
        return self.config.digest

    def describe(self) -> dict:
        return {"config": self.config.describe(), "data_dir": str(self.data_dir), "asset_dir": str(self.asset_dir),
                "resource_dir": str(self.resource_dir), "host": self.host, "port": self.port}


Progress = Callable[[str], None]
Builder = Callable[[GenerationInputs, Progress], Generation]


class BuildState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class BuildJob:
    job_id: str
    started_at: float
    state: BuildState = BuildState.RUNNING
    progress: list[str] = field(default_factory=list)
    candidate_id: str | None = None
    issue: StartupIssue | None = None
    finished_at: float | None = None

    def describe(self, now: float) -> dict:
        return {"job_id": self.job_id, "state": self.state, "progress": list(self.progress),
                "candidate_id": self.candidate_id,
                "error": None if self.issue is None else {"code": self.issue.code, "message": self.issue.message},
                "elapsed_seconds": (self.finished_at if self.finished_at is not None else now) - self.started_at}


@dataclass
class _Slot:
    generation: Generation
    leases: int = 0
    retired_at: float | None = None


def _start_daemon(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="aigis-generation-build", daemon=True).start()


class GenerationRuntime:
    def __init__(self, initial: Generation, *, clock: Callable[[], float] = time.monotonic,
                 admit_timeout: float = ADMIT_TIMEOUT_SECONDS, drain_timeout: float = DRAIN_TIMEOUT_SECONDS,
                 draining_limit: float = DRAINING_LIMIT_SECONDS, candidate_ttl: float = CANDIDATE_TTL_SECONDS,
                 start: Callable[[Callable[[], None]], None] = _start_daemon):
        if not isinstance(initial, Generation):
            raise ValueError("The generation runtime starts from a built generation")
        if not callable(clock) or not callable(start):
            raise ValueError("The generation runtime needs a clock and a thread starter")
        limits = (admit_timeout, drain_timeout, draining_limit, candidate_ttl)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 for value in limits):
            raise ValueError("Generation runtime limits must be positive seconds")
        self._clock, self._start = clock, start
        self._admit_timeout, self._drain_timeout = float(admit_timeout), float(drain_timeout)
        self._draining_limit, self._candidate_ttl = float(draining_limit), float(candidate_ttl)
        self._condition = threading.Condition()
        self._current = _Slot(initial)
        self._draining: _Slot | None = None
        self._fenced = False
        self._job: BuildJob | None = None
        self._candidate: tuple[Generation, float] | None = None
        self._taken: tuple[Generation, float] | None = None
        self._last_request: float | None = None
        self._switched_at: float | None = None
        self._session_observed = False

    @property
    def current(self) -> Generation:
        with self._condition:
            return self._current.generation

    @contextmanager
    def lease(self) -> Iterator[Generation]:
        slot = self._acquire()
        try:
            yield slot.generation
        finally:
            self._release(slot)

    def _acquire(self) -> _Slot:
        with self._condition:
            deadline = self._clock() + self._admit_timeout
            while self._fenced:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise ProtocolError("generation_switching", "The server is switching generations; retry shortly", 503)
                self._condition.wait(remaining)
            self._current.leases += 1
            return self._current

    def _release(self, slot: _Slot) -> None:
        with self._condition:
            slot.leases -= 1
            if slot is self._draining and not slot.leases:
                self._draining = None
            self._condition.notify_all()

    def swap(self, candidate: Generation, expected_current_id: str) -> Generation:
        if not isinstance(candidate, Generation):
            raise ValueError("Only built generations can be swapped in")
        with self._condition:
            if self._current.generation.id != expected_current_id:
                raise ProtocolError("generation_changed", "The running generation changed before the switch", 409)
            self._await_draining()
            retired = self._current
            if retired.leases:
                retired.retired_at = self._clock()
                self._draining = retired
            self._current = _Slot(candidate)
            self._switched_at = self._clock()
            self._session_observed = False
            self._condition.notify_all()
            log.info("Generation %s replaced %s", candidate.id, retired.generation.id)
            return retired.generation

    def _await_draining(self) -> None:
        # Holding a second draining generation would keep two full service bundles alive at once.
        slot = self._draining
        while slot is not None and self._draining is slot:
            remaining = slot.retired_at + self._draining_limit - self._clock()
            if remaining <= 0:
                raise ProtocolError("generation_draining", "The previous generation is still serving requests", 409)
            self._condition.wait(remaining)

    @contextmanager
    def fence(self) -> Iterator[Generation]:
        with self._condition:
            if self._fenced:
                raise ProtocolError("generation_switch_busy", "Another generation switch is in progress", 409)
            self._fenced = True
            try:
                self._drain()
            except BaseException:
                self._open_fence()
                raise
            generation = self._current.generation
        try:
            yield generation
        finally:
            with self._condition:
                self._open_fence()

    def _drain(self) -> None:
        deadline = self._clock() + self._drain_timeout
        while self._in_flight():
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ProtocolError("switch_drain_timeout",
                                    "Requests in flight did not finish in time; the switch was abandoned", 409)
            self._condition.wait(remaining)

    def _open_fence(self) -> None:
        self._fenced = False
        self._condition.notify_all()

    def _in_flight(self) -> int:
        return self._current.leases + (self._draining.leases if self._draining is not None else 0)

    def start_build(self, inputs: GenerationInputs, builder: Builder) -> dict:
        if not isinstance(inputs, GenerationInputs) or not callable(builder):
            raise ValueError("Candidate builds need generation inputs and a builder")
        with self._condition:
            if self._job is not None and self._job.state is BuildState.RUNNING:
                raise ProtocolError("generation_build_busy", "A candidate generation is already being built", 409)
            job = self._job = BuildJob(uuid.uuid4().hex, self._clock())
        self._start(lambda: self._build(job, inputs, builder))
        return self.build_status()

    def _build(self, job: BuildJob, inputs: GenerationInputs, builder: Builder) -> None:
        try:
            candidate = builder(inputs, lambda message: self._progress(job, message))
            if not isinstance(candidate, Generation):
                raise TypeError("Generation builders must return a generation")
        except Exception as error:
            log.warning("Candidate generation build %s failed", job.job_id, exc_info=True)
            with self._condition:
                job.state, job.issue, job.finished_at = BuildState.FAILED, StartupIssue.of(error), self._clock()
                self._condition.notify_all()
            return
        with self._condition:
            job.state, job.candidate_id, job.finished_at = BuildState.SUCCEEDED, candidate.id, self._clock()
            # A fresh build replaces whatever was taken before it, so the old candidate can never be restored.
            self._candidate, self._taken = (candidate, job.finished_at), None
            self._condition.notify_all()

    def _progress(self, job: BuildJob, message: str) -> None:
        with self._condition:
            if job.state is BuildState.RUNNING:
                job.progress.append(str(message)[:MAX_PROGRESS_TEXT])
                del job.progress[:-MAX_PROGRESS_ENTRIES]

    def wait_for_build(self, timeout: float) -> dict | None:
        with self._condition:
            self._condition.wait_for(lambda: self._job is None or self._job.state is not BuildState.RUNNING, timeout)
        return self.build_status()

    def build_status(self) -> dict | None:
        with self._condition:
            return None if self._job is None else self._job.describe(self._clock())

    def candidate(self) -> Generation | None:
        with self._condition:
            return self._live_candidate()

    def take_candidate(self, generation_id: str) -> Generation:
        with self._condition:
            candidate = self._live_candidate()
            if candidate is None or candidate.id != generation_id:
                raise ProtocolError("generation_candidate_missing",
                                    "The candidate generation expired or was replaced; build it again", 409)
            self._taken, self._candidate = self._candidate, None
            return candidate

    def restore_candidate(self, generation: Generation) -> None:
        if not isinstance(generation, Generation):
            raise ValueError("Only taken candidate generations can be restored")
        with self._condition:
            taken = self._taken
            if taken is None or taken[0] is not generation:
                return
            self._taken = None
            # The original build time stays, so a restored candidate expires on its unchanged TTL.
            if self._candidate is None:
                self._candidate = taken

    def _live_candidate(self) -> Generation | None:
        if self._candidate is None:
            return None
        generation, built_at = self._candidate
        if self._clock() - built_at > self._candidate_ttl:
            self._candidate = None
            return None
        return generation

    def observe_request(self, route: int | str) -> None:
        with self._condition:
            self._last_request = self._clock()
            if str(route) in _STARTUP_ROUTES:
                self._session_observed = True

    def idle_seconds(self) -> float | None:
        with self._condition:
            return None if self._last_request is None else self._clock() - self._last_request

    def session_observed_since_switch(self) -> bool | None:
        with self._condition:
            return None if self._switched_at is None else self._session_observed

    def status(self) -> dict:
        with self._condition:
            now = self._clock()
            candidate = self._live_candidate()
            draining = self._draining
            return {
                "current": {"id": self._current.generation.id, "leases": self._current.leases},
                "draining": None if draining is None else {
                    "id": draining.generation.id, "leases": draining.leases,
                    "seconds": now - draining.retired_at},
                "fenced": self._fenced,
                "build": None if self._job is None else self._job.describe(now),
                "candidate": None if candidate is None else {
                    "id": candidate.id, "expires_in_seconds": self._candidate[1] + self._candidate_ttl - now},
                "idle_seconds": None if self._last_request is None else now - self._last_request,
                "switched_seconds_ago": None if self._switched_at is None else now - self._switched_at,
                "session_observed_since_switch": None if self._switched_at is None else self._session_observed,
            }
