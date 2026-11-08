from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from .business_calendar import civil_date, civil_day
from .errors import ProtocolError


# AACF20 returns gmtime's tm_wday + 1, so the native week starts at Sunday = 1.
SUNDAY = 1
DAYS_PER_WEEK = 7
Reading = TypeVar("Reading")


def _client_calendar(read: Callable[[datetime], Reading], moment: datetime) -> Reading:
    try:
        if not isinstance(moment, datetime) or moment.utcoffset() is None:
            raise ValueError("Native calendar values require an aware datetime")
        return read(moment)
    except (ValueError, TypeError, OverflowError, ProtocolError) as error:
        raise ProtocolError("quest_clock_invalid",
                            "Native calendar values require an aware datetime inside the client calendar", 503) from error


def native_day(moment: datetime) -> int:
    # 63EBA0 truncates milliseconds since 0001-01-01 JST to whole days.
    return _client_calendar(civil_day, moment)


def native_weekday(moment: datetime) -> int:
    return _client_calendar(civil_date, moment).isoweekday() % DAYS_PER_WEEK + SUNDAY
