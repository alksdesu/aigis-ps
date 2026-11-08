from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
import threading
import time
from typing import Any, Protocol

from .admin_command import AdminError
from .quest_service import JOURNAL_KEY, active_identity
from .storage import timestamp


# The menu scene heartbeats every 180 seconds, so the quiet window must outlast one period with a margin.
CLIENT_QUIET_SECONDS = 240.0
MAX_RESTART_NOTICES = 16
MAX_NOTICE_REASON = 64
QUEST_JOURNAL = JOURNAL_KEY
ACTIVE_BATTLE = "active"
BATTLE_FIELDS = ("quest_id", "sequence", "start_key", "master_snapshot", "resource_version")


class ClientActivity(Protocol):
    def idle_seconds(self) -> float | None: ...

    def session_observed_since_switch(self) -> bool | None: ...


@dataclass(frozen=True)
class RestartNotice:
    reason: str
    created_at: str
    mark: float

    def describe(self) -> dict:
        return {"reason": self.reason, "created_at": self.created_at}


class ClientGate:
    # The clock must be the runtime's clock: notices compare their marks with the runtime's idle time.
    def __init__(self, activity: ClientActivity, *, quiet_seconds: float = CLIENT_QUIET_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        if any(not callable(getattr(activity, name, None)) for name in ("idle_seconds", "session_observed_since_switch")):
            raise ValueError("Client gates read request activity from the generation runtime")
        if isinstance(quiet_seconds, bool) or not isinstance(quiet_seconds, (int, float)) or quiet_seconds <= 0:
            raise ValueError("The client quiet period must be positive seconds")
        if not callable(clock):
            raise ValueError("Client gates need the runtime clock")
        self._activity, self._quiet, self._clock = activity, float(quiet_seconds), clock
        self._notices: list[RestartNotice] = []
        self._lock = threading.Lock()

    def quiet(self) -> bool:
        return self._quiet_for(self._activity.idle_seconds())

    def _quiet_for(self, idle: float | None) -> bool:
        return idle is None or idle >= self._quiet

    def require_closed(self, acknowledged: object) -> None:
        if acknowledged is not True:
            raise AdminError("admin_client_closed_required", "Confirm that the game client is closed",
                             HTTPStatus.UNPROCESSABLE_ENTITY)
        idle = self._activity.idle_seconds()
        if not self._quiet_for(idle):
            raise AdminError("admin_client_active", "The game client sent a request recently; close it and wait",
                             details={"idle_seconds": idle, "quiet_seconds": self._quiet})

    def notice_restart(self, reason: str) -> None:
        if not isinstance(reason, str) or not 0 < len(reason) <= MAX_NOTICE_REASON or not reason.isprintable():
            raise ValueError("Restart notices need a short printable reason")
        with self._lock:
            self._notices = [*self._pending(), RestartNotice(reason, timestamp(), self._clock())][-MAX_RESTART_NOTICES:]

    def status(self) -> dict:
        with self._lock:
            self._notices = self._pending()
            notices = [notice.describe() for notice in self._notices]
        idle = self._activity.idle_seconds()
        return {"quiet_seconds": self._quiet, "idle_seconds": idle, "quiet": self._quiet_for(idle),
                "session_observed_since_switch": self._activity.session_observed_since_switch(),
                "restart_required": bool(notices), "notices": notices}

    def _pending(self) -> list[RestartNotice]:
        idle, now = self._activity.idle_seconds(), self._clock()
        # The client was confirmed closed first, so the next game request comes from a restarted client.
        return [notice for notice in self._notices if idle is None or idle >= now - notice.mark]


def active_battle(state: Mapping[str, Any]) -> object | None:
    journal = state.get(QUEST_JOURNAL) if isinstance(state, Mapping) else None
    return journal.get(ACTIVE_BATTLE) if isinstance(journal, Mapping) else None


def _running_generation_battle(state: Mapping[str, Any], quests) -> bool:
    identity = active_identity(dict(state))
    return identity is not None and identity[:2] == (quests.catalog.snapshot.identifier,
                                                     quests.catalog.resource_version)


def require_no_active_battle(state: Mapping[str, Any], *, source: str, quests=None) -> None:
    active = active_battle(state)
    if active is None:
        return
    # An attempt frozen on the running snapshots can still settle after the restore, so it never blocks it.
    if quests is not None and _running_generation_battle(state, quests):
        return
    battle = {name: active.get(name) for name in BATTLE_FIELDS} if isinstance(active, Mapping) else {}
    raise AdminError("admin_battle_abort_required",
                     f"The {source} has a battle in progress that cannot be matched to the running generation; "
                     "abort the battle first", details={"source": source, "battle": battle})


def cross_account(current_player_id: str, target_player_id: str) -> bool:
    return current_player_id != target_player_id
