from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import TypeVar

from .errors import ProtocolError
from .storage import timestamp


log = logging.getLogger("aigis_ps")
REFUSED = "refused"
DEGRADED = "degraded"
INTERNAL_ERROR = "internal_error"
Reply = TypeVar("Reply")


@dataclass(frozen=True)
class StartupIssue:
    code: str
    message: str

    @classmethod
    def of(cls, error: BaseException) -> StartupIssue:
        if isinstance(error, ProtocolError):
            return cls(error.code, str(error))
        return cls(INTERNAL_ERROR, type(error).__name__)


@dataclass
class _LoadRecord:
    outcome: str
    issues: tuple[StartupIssue, ...]
    occurrences: int
    first_seen: str
    last_seen: str


class StartupDiagnostics:
    def __init__(self, clock: Callable[[], str] = timestamp):
        if not callable(clock):
            raise ValueError("Startup diagnostics clock must be callable")
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[str, _LoadRecord] = {}
        self._active: ContextVar[list[StartupIssue] | None] = ContextVar("startup_load_issues", default=None)

    @contextmanager
    def load(self, operation: int | str) -> Iterator[None]:
        issues: list[StartupIssue] = []
        token = self._active.set(issues)
        try:
            yield
        except Exception as error:
            issue = StartupIssue.of(error)
            self._record(str(operation), REFUSED, tuple(issues) if issue in issues else (*issues, issue))
            raise
        else:
            self._record(str(operation), DEGRADED if issues else None, tuple(issues))
        finally:
            self._active.reset(token)

    def wrap(self, operation: int | str, handler: Callable[..., bytes]) -> StartupLoad:
        if isinstance(handler, StartupLoad) and handler.diagnostics is self and handler.operation == str(operation):
            return handler
        return StartupLoad(self, str(operation), handler)

    def degrade(self, error: ProtocolError) -> None:
        issue = StartupIssue.of(error)
        issues = self._active.get()
        if issues is None:
            log.warning("Startup state was served outside a recorded load: %s", _describe((issue,)))
        elif issue not in issues:
            issues.append(issue)

    def _record(self, operation: str, outcome: str | None, issues: tuple[StartupIssue, ...]) -> None:
        now = self._clock()
        with self._lock:
            previous = self._records.get(operation)
            if outcome is None:
                if previous is None:
                    return
                del self._records[operation]
            elif previous is not None and (previous.outcome, previous.issues) == (outcome, issues):
                previous.occurrences += 1
                previous.last_seen = now
                return
            else:
                self._records[operation] = _LoadRecord(outcome, issues, 1, now, now)
        if outcome is None:
            log.info("Startup load %s is served cleanly again", operation)
        elif outcome == REFUSED:
            log.error("Startup load %s was refused: %s", operation, _describe(issues))
        else:
            log.warning("Startup load %s was served from stored state with issues: %s", operation, _describe(issues))

    def status(self) -> dict:
        with self._lock:
            loads = {operation: {"outcome": record.outcome, "issues": [asdict(issue) for issue in record.issues],
                                 "occurrences": record.occurrences, "first_seen": record.first_seen,
                                 "last_seen": record.last_seen}
                     for operation, record in sorted(self._records.items())}
        return {"attention_required": bool(loads), "loads": loads}


class StartupLoad:
    def __init__(self, diagnostics: StartupDiagnostics, operation: str, handler: Callable[..., bytes]):
        if not callable(handler):
            raise ValueError("Startup loads require a callable handler")
        self.diagnostics, self.operation, self.handler = diagnostics, operation, handler

    def __call__(self, *arguments) -> bytes:
        with self.diagnostics.load(self.operation):
            return self.handler(*arguments)

    def serve(self, encode: Callable[[bytes], Reply], *arguments) -> Reply:
        # A reply the client cannot receive stays on record instead of a clean handler run clearing it.
        with self.diagnostics.load(self.operation):
            if isinstance(self.handler, StartupLoad):
                return self.handler.serve(encode, *arguments)
            return encode(self.handler(*arguments))


def _describe(issues: tuple[StartupIssue, ...]) -> str:
    return "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
