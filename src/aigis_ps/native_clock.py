from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import re

from .business_calendar import mktime_day
from .energy_clock import JST, MILLISECOND, instant
from .errors import ProtocolError
from .native_numbers import float32, int32


NATIVE_EPOCH = datetime(1, 1, 1, tzinfo=JST)
UTC_DAY_ONE = datetime(1, 1, 1, tzinfo=timezone.utc)
MAX_DAY_ONE_MILLISECONDS = (datetime.max.replace(tzinfo=timezone.utc) - UTC_DAY_ONE) // MILLISECOND
DATE_SEPARATORS = ("/", "-")
DATE_PARTS = 6
DAY_MILLISECONDS = 86400000.0
HOUR_DAYS = 0.041666666666666664
# The client's minute factor is one ulp below 1/1440.
MINUTE_DAYS = 0.0006944444444444444
SECOND_DAYS = 0.000011574074074074073
ROUND_HALF = 0.5
CLOCK_QUANTUM = 4
INT32_DIGITS = 10
INT64_LIMIT = 2 ** 63
_SPACE = "[ \t\n\v\f\r]*"
_INTEGER = re.compile(_SPACE + "([+-]?)([0-9]+)")
_HEX_DAYS = re.compile(_SPACE + "[+-]?0[xX]")
_DECIMAL_DAYS = re.compile(_SPACE + r"([+-]?[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?)")


class NativeDateUndetermined(ValueError):
    pass


def _scan_field(sign: str, digits: str) -> int | None:
    significant = digits.lstrip("0") or "0"
    if len(significant) > INT32_DIGITS:
        return None
    value = int(sign + significant)
    return value if int32(value) else None


def _scan(text: str, separator: str) -> tuple[int | None, ...]:
    # MSVC sscanf with "%d<sep>%d<sep>%d %d:%d:%d"; the format space adds nothing because %d skips whitespace.
    values, position = [], 0
    for literal in (separator, separator, "", ":", ":", None):
        match = _INTEGER.match(text, position)
        if match is None:
            break
        values.append(_scan_field(*match.groups()))
        position = match.end()
        if literal is None or not text.startswith(literal, position):
            break
        position += len(literal)
    return tuple(values)


def _truncate(value: float) -> int:
    if not math.isfinite(value) or not -INT64_LIMIT <= value < INT64_LIMIT:
        raise NativeDateUndetermined("The native date overflows the client's 64-bit clock")
    return int(value)


def _clock_milliseconds(day: int, hour: int, minute: int, second: int) -> int:
    # AACF90 and the AAC970 clock share this float32 day fraction and the final 4 ms rounding.
    fraction = float32(float32(minute * MINUTE_DAYS + hour * HOUR_DAYS + float32(second) * SECOND_DAYS)
                       * DAY_MILLISECONDS)
    raw = _truncate(day * DAY_MILLISECONDS + fraction + ROUND_HALF)
    return CLOCK_QUANTUM * ((raw + CLOCK_QUANTUM // 2) // CLOCK_QUANTUM)


def _day_count(text: str) -> int:
    if _HEX_DAYS.match(text):
        raise NativeDateUndetermined("Hexadecimal day counts are not replicated")
    return _truncate(float(_DECIMAL_DAYS.match(text).group(1)) * DAY_MILLISECONDS + ROUND_HALF)


def _native_milliseconds(text: str) -> int:
    if type(text) is not str:
        raise ValueError("Native dates require text")
    # AACF90 receives a C string.
    text = text.partition("\0")[0]
    if not text:
        return 0
    slash, dash = (_scan(text, separator) for separator in DATE_SEPARATORS)
    # AACF90 rescans with whichever format converted more fields, preferring '-' on ties.
    fields = slash if len(slash) > len(dash) else dash
    # The day-count branch reparses the whole text, so its scanned field never matters.
    if len(fields) == 1:
        return _day_count(text)
    if len(fields) < 3:
        raise NativeDateUndetermined("The client would read unset date fields from its stack")
    if None in fields:
        raise NativeDateUndetermined("A native date field overflows int32")
    year, month, day, hour, minute, second = fields + (0,) * (DATE_PARTS - len(fields))
    number = mktime_day(year, month, day)
    if not (hour or minute or second):
        return _truncate(number * DAY_MILLISECONDS + ROUND_HALF)
    # The client widens negative hours and minutes as unsigned; such rows stay unverifiable.
    if min(hour, minute, second) < 0:
        raise NativeDateUndetermined("Negative clock fields are not replicated")
    return _clock_milliseconds(number, hour, minute, second)


def _moment(milliseconds: int) -> datetime:
    try:
        return NATIVE_EPOCH + timedelta(milliseconds=milliseconds)
    except OverflowError as error:
        raise ValueError("Native date is outside the supported calendar") from error


def parse_native_date(text: str) -> datetime | None:
    milliseconds = _native_milliseconds(text)
    # 93BF40 treats a zero bound as unset, which is also what an empty date produces.
    return _moment(milliseconds) if milliseconds else None


def day_one_milliseconds(value: datetime) -> int:
    # The UTC conversion looks redundant but refuses instants the UTC calendar cannot hold.
    return (instant(value).astimezone(timezone.utc) - UTC_DAY_ONE) // MILLISECOND


def native_clock(moment: datetime) -> datetime:
    try:
        if not isinstance(moment, datetime) or moment.utcoffset() is None:
            raise ValueError("The client clock requires an aware datetime")
        local = moment.astimezone(JST)
    except (ValueError, TypeError, OverflowError) as error:
        raise ProtocolError("quest_clock_invalid", "The client clock requires an aware datetime", 503) from error
    return _moment(_clock_milliseconds(mktime_day(local.year, local.month, local.day),
                                       local.hour, local.minute, local.second))
