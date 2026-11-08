from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math

from .errors import ProtocolError
from .native_numbers import INT32_MAX, float32


UTC_OFFSET_MINUTES = 540
JST = timezone(timedelta(minutes=UTC_OFFSET_MINUTES), "JST")
MARKER_KEY = "energy_clock"
SCHEMA_VERSION = 1
ENCODING = "native_day_fraction_f32_seconds_v1"
NEW_ORIGIN, CONVERTED_ORIGIN = "new_local", "converted"
MAX_SOURCE_OFFSET_MINUTES = 840
SECONDS_PER_DAY = 86400
UNIX_EPOCH_DAY = 719162
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MILLISECOND = timedelta(milliseconds=1)
PREFIXES = ("Charisma", "ActionPoint")
MARKER_FIELDS = frozenset(("schema_version", "encoding", "utc_offset_minutes", "origin"))
CLOCK_SOURCE_REQUIRED = "energy_clock_source_required"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def instant(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ProtocolError("energy_clock_argument", "Energy clocks require a datetime value")
    try:
        return value.replace(tzinfo=JST) if value.utcoffset() is None else value
    except (OverflowError, ValueError, TypeError) as error:
        raise ProtocolError("energy_clock_argument", "Energy clock timezone cannot represent this instant") from error


def energy_seconds(value: datetime) -> int:
    try:
        local = instant(value).astimezone(JST)
        return ((local.date().toordinal() - 1) * SECONDS_PER_DAY
                + local.hour * 3600 + local.minute * 60 + local.second)
    except ProtocolError:
        raise
    except (OverflowError, ValueError, TypeError) as error:
        raise ProtocolError("energy_clock_argument", "Energy clock instant is outside the native calendar") from error


def unix_milliseconds(value: datetime) -> int:
    # The UTC conversion looks redundant but refuses instants the UTC calendar cannot hold.
    return (instant(value).astimezone(timezone.utc) - UNIX_EPOCH) // MILLISECOND


def _prefix(prefix: str) -> None:
    if type(prefix) is not str or prefix not in PREFIXES:
        raise ProtocolError("energy_clock_argument", "Unknown energy anchor prefix")


def anchor_seconds(row: dict, prefix: str = "Charisma") -> int:
    _prefix(prefix)
    if not isinstance(row, dict):
        raise ProtocolError("energy_clock_anchor_invalid", "Energy anchors require a GameParam row", 503)
    day, fraction = row.get(prefix + "ChangeDate"), row.get(prefix + "ChangeTime")
    if (type(day) is not int or not 0 <= day <= INT32_MAX
            or type(fraction) not in (int, float) or not 0 <= fraction < 1 or not math.isfinite(fraction)):
        raise ProtocolError("energy_clock_anchor_invalid", "Energy anchors require an int32 day and finite day fraction", 503)
    fraction = float32(fraction)
    if not 0 <= fraction < 1:
        raise ProtocolError("energy_clock_anchor_invalid", "Energy anchor fraction is outside native float32 bounds", 503)
    return day * SECONDS_PER_DAY + round(fraction * SECONDS_PER_DAY)


def set_anchor(row: dict, seconds: int, prefix: str = "Charisma") -> None:
    _prefix(prefix)
    if not isinstance(row, dict) or type(seconds) is not int or seconds < 0:
        raise ProtocolError("energy_clock_anchor_invalid", "Energy anchors require nonnegative integer seconds", 503)
    day, second = divmod(seconds, SECONDS_PER_DAY)
    if day > INT32_MAX:
        raise ProtocolError("energy_clock_anchor_invalid", "Energy anchor day exceeds the native int32 field", 503)
    fraction = float32(second / SECONDS_PER_DAY)
    row.update({prefix + "ChangeDate": day, prefix + "ChangeTime": fraction})


def _row(state: dict) -> dict:
    if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1:
        raise ProtocolError("energy_clock_state_invalid", "Energy clocks require supported bootstrap storage", 503)
    tables = state.get("tables")
    rows = tables.get("game_param") if isinstance(tables, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ProtocolError("energy_clock_state_invalid", "Energy clocks require one persisted GameParam row", 503)
    return rows[0]


def source_offset_valid(value) -> bool:
    return type(value) is int and -MAX_SOURCE_OFFSET_MINUTES <= value <= MAX_SOURCE_OFFSET_MINUTES


def _source_offset(value) -> int:
    if not source_offset_valid(value):
        raise ProtocolError("energy_clock_source_invalid", "Source UTC offset must be an explicit integer "
                            f"from {-MAX_SOURCE_OFFSET_MINUTES} to {MAX_SOURCE_OFFSET_MINUTES} minutes")
    return value


def _marker(state: dict) -> None:
    if MARKER_KEY not in state:
        raise ProtocolError(CLOCK_SOURCE_REQUIRED, "The saved energy clock requires an explicit source timezone", 503)
    marker = state[MARKER_KEY]
    if isinstance(marker, dict):
        origin = marker.get("origin")
        expected = MARKER_FIELDS | {"source_utc_offset_minutes"} if origin == CONVERTED_ORIGIN else MARKER_FIELDS
        if (set(marker) == expected and type(marker.get("schema_version")) is int
                and marker["schema_version"] == SCHEMA_VERSION
                and marker.get("encoding") == ENCODING and type(marker.get("utc_offset_minutes")) is int
                and marker["utc_offset_minutes"] == UTC_OFFSET_MINUTES and origin in (NEW_ORIGIN, CONVERTED_ORIGIN)):
            if origin == NEW_ORIGIN or source_offset_valid(marker.get("source_utc_offset_minutes")):
                return
    raise ProtocolError("energy_clock_invalid", "The saved energy clock marker is invalid or unsupported", 503)


def validate_clock(state: dict) -> None:
    row = _row(state)
    _marker(state)
    for prefix in PREFIXES:
        anchor_seconds(row, prefix)


def mark_new(state: dict, now: datetime) -> dict:
    _row(state)
    seconds = energy_seconds(now)
    updated = deepcopy(state)
    row = _row(updated)
    for prefix in PREFIXES:
        set_anchor(row, seconds, prefix)
    updated[MARKER_KEY] = {"schema_version": SCHEMA_VERSION, "encoding": ENCODING,
                           "utc_offset_minutes": UTC_OFFSET_MINUTES, "origin": NEW_ORIGIN}
    return updated


def convert_state(state: dict, source_offset_minutes: int) -> dict:
    row = _row(state)
    if MARKER_KEY in state:
        validate_clock(state)
        return deepcopy(state)
    source = _source_offset(source_offset_minutes)
    anchors = {}
    for prefix in PREFIXES:
        seconds = anchor_seconds(row, prefix)
        if seconds == 0:
            raise ProtocolError(CLOCK_SOURCE_REQUIRED, "A zero energy anchor has no recoverable source instant", 503)
        day = row[prefix + "ChangeDate"]
        if day == UNIX_EPOCH_DAY and source != UTC_OFFSET_MINUTES:
            raise ProtocolError("energy_clock_source_ambiguous", "The legacy epoch-day encoding does not identify one source date", 503)
        if day < UNIX_EPOCH_DAY and source < UTC_OFFSET_MINUTES:
            seconds -= SECONDS_PER_DAY
        elif day > UNIX_EPOCH_DAY and source > UTC_OFFSET_MINUTES:
            seconds += SECONDS_PER_DAY
        anchors[prefix] = seconds + (UTC_OFFSET_MINUTES - source) * 60
    updated = deepcopy(state)
    target = _row(updated)
    for prefix, seconds in anchors.items():
        set_anchor(target, seconds, prefix)
    updated[MARKER_KEY] = {"schema_version": SCHEMA_VERSION, "encoding": ENCODING,
                           "utc_offset_minutes": UTC_OFFSET_MINUTES, "origin": CONVERTED_ORIGIN,
                           "source_utc_offset_minutes": source}
    return updated
