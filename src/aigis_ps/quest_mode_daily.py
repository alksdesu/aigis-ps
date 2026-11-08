from __future__ import annotations

from types import MappingProxyType

from .errors import ProtocolError
from .native_numbers import INT32_MAX, int32
from .quest_modes import OPEN_WEEK_DAYS, ModePolicy, require_row_values


DAILY_TYPE = 2
NO_FIXED_TEAM = 0
ROW_VALUES = MappingProxyType({"Type": DAILY_TYPE, "UnitList": NO_FIXED_TEAM})
# 679B10 tests one Sunday-first _HoldingParam bit per weekday; OpenWeek beyond its 8–10 weekday pairs never opens.
WEEKDAY_MASK = 0x7F
MAX_OPEN_WEEK = max(OPEN_WEEK_DAYS)
SCHEDULE_RANGES = MappingProxyType({
    "_HoldingParam": (0, WEEKDAY_MASK), "OpenWeek": (0, MAX_OPEN_WEEK),
    "StartOpenDay": (0, INT32_MAX), "EndOpenDay": (0, INT32_MAX),
})


class DailyPolicy(ModePolicy):
    name = "daily"
    quest_types = (DAILY_TYPE,)
    enabled = True
    timed_conditions = True
    # Mission top banners and both orb entries reach weekday quests without a quest check; only the portal banner asks for 1004.
    gate_quest = None

    def validate_row(self, row, catalog) -> None:
        require_row_values(row, ROW_VALUES, "Weekday quest")
        for name, (low, high) in SCHEDULE_RANGES.items():
            # quest_open_on reads an absent schedule column as 0, so validation accepts it the same way.
            value = row.get(name, 0)
            if not int32(value) or not low <= value <= high:
                raise ProtocolError("quest_definition_invalid", f"Weekday quest field {name} is outside the native schedule", 503)


POLICY = DailyPolicy()
