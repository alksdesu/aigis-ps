from __future__ import annotations

from dataclasses import dataclass

from .energy_clock import instant
from .native_clock import native_clock
from .native_numbers import INT32_MAX


@dataclass(frozen=True)
class EventPassAutomationRules:
    enabled: bool = True
    closed_statuses: tuple[int, ...] = (2,)

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Event pass automation requires a boolean enabled flag")
        statuses = self.closed_statuses
        if (not isinstance(statuses, (tuple, list)) or len(statuses) > 32
                or any(type(value) is not int or not 2 <= value <= INT32_MAX for value in statuses)
                or len(set(statuses)) != len(statuses)):
            raise ValueError("Closed event pass statuses must be distinct native integers above one")
        object.__setattr__(self, "closed_statuses", tuple(sorted(statuses)))

    def ended(self, entry, now):
        if not self.enabled:
            return False
        current = native_clock(instant(now))
        return (current > entry.native_end_at
                or entry.status in self.closed_statuses and current >= entry.native_start_at)
