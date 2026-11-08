from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
import logging
import threading
import time
import uuid

from fastapi import APIRouter, Request

from .admin_app import AdminServices, request_body
from .admin_command import AdminError, CommandParams, error_payload
from .admin_registry import AdminRoute, RouteRegistry
from .generation import BuildState


ROUTE_NAME = "entrust"
VALIDATIONS_PATH = "/entrust/validations"
VALIDATION_THREAD = "aigis-entrust-validate"
log = logging.getLogger("aigis_ps")


class StartValidationRequest(CommandParams):
    pass


def _start_daemon(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name=VALIDATION_THREAD, daemon=True).start()


@dataclass
class ValidationJob:
    job_id: str
    generation_id: str
    content_version: str
    started_at: float
    state: BuildState = BuildState.RUNNING
    progress: dict[int, dict] = field(default_factory=dict)
    result: dict | None = None
    error: dict | None = None
    finished_at: float | None = None

    def describe(self, now: float, current_generation_id: str) -> dict:
        return {"job_id": self.job_id, "state": self.state, "generation_id": self.generation_id,
                "content_version": self.content_version,
                "stale": self.generation_id != current_generation_id,
                "progress": {str(quest_type): dict(entry) for quest_type, entry in sorted(self.progress.items())},
                "result": self.result, "error": self.error,
                "elapsed_seconds": (self.finished_at if self.finished_at is not None else now) - self.started_at}


class EntrustValidationRunner:
    def __init__(self, services: AdminServices, *, clock: Callable[[], float] = time.monotonic,
                 start: Callable[[Callable[[], None]], None] = _start_daemon):
        if not callable(clock) or not callable(start):
            raise ValueError("Entrust validation jobs need a clock and a thread starter")
        self._services, self._clock, self._start = services, clock, start
        self._lock = threading.Lock()
        self._job: ValidationJob | None = None

    def start(self) -> dict:
        generation = self._services.runtime.current
        entrust = getattr(generation.bundle, "entrust", None)
        if entrust is None:
            raise AdminError("admin_entrust_unavailable", "This generation has no entrust service",
                             HTTPStatus.SERVICE_UNAVAILABLE)
        with self._lock:
            if self._job is not None and self._job.state is BuildState.RUNNING:
                raise AdminError("admin_entrust_validation_busy", "An entrust validation job is already running")
            job = self._job = ValidationJob(uuid.uuid4().hex, generation.id, entrust.catalog.content_version,
                                            self._clock())
        self._start(lambda: self._run(job, entrust.catalog))
        return self.status()

    def _run(self, job: ValidationJob, catalog) -> None:
        def report(entry: dict) -> None:
            with self._lock:
                if job.state is BuildState.RUNNING:
                    job.progress[entry["quest_type"]] = {"done": entry["done"], "total": entry["total"]}

        try:
            result = catalog.validate(progress=report)
        except Exception as error:
            log.warning("Entrust validation job %s failed", job.job_id, exc_info=True)
            with self._lock:
                job.state, job.error, job.finished_at = BuildState.FAILED, error_payload(error), self._clock()
            return
        with self._lock:
            job.state, job.finished_at = BuildState.SUCCEEDED, self._clock()
            job.result = {str(quest_type): dict(entry) for quest_type, entry in sorted(result.items())}

    def status(self) -> dict:
        current_id = self._services.runtime.current.id
        with self._lock:
            job = self._job
            described = None if job is None else job.describe(self._clock(), current_id)
        return {"generation_id": current_id, "validation": described}


def register(registry: RouteRegistry) -> None:
    registry.add(AdminRoute(ROUTE_NAME, build))


def build(services: AdminServices) -> APIRouter:
    router, runner = APIRouter(), EntrustValidationRunner(services)

    @router.get(VALIDATIONS_PATH)
    def validation_status() -> dict:
        return runner.status()

    @router.post(VALIDATIONS_PATH)
    def start_validation(request: Request) -> dict:
        request_body(request, StartValidationRequest)
        return runner.start()

    return router
