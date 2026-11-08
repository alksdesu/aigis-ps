from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .business_calendar import DAY_TEXT, business_date, month_key
from .energy_clock import UNIX_EPOCH, instant, unix_milliseconds
from .errors import ProtocolError
from .native_numbers import int32


LEDGER_KEY = "login_rewards"
REBASE_UNNEEDED = "login_rewards_rebase_unneeded"
RULES_CHANGED = "login_rewards_rules_changed"
RESPONSE_CAPACITY = "login_reward_response_capacity"
NEW_LOCAL, LEGACY = "new_local", "legacy"
MAX_BEGINNER_REWARDS = 366
INT64_MAX = (1 << 63) - 1
_FIELDS = frozenset(("schema_version", "last_claim_day", "stamp_month", "stamp_count", "beginner_eligible",
                     "beginner_claimed", "beginner_total", "last_time", "baseline_source"))


@dataclass(frozen=True)
class LoginAdvance:
    day: str
    year: int
    month: int
    ordinal: int
    new_day: bool
    beginner_ordinal: int
    beginner_remaining: int


def _invalid():
    raise ProtocolError("login_rewards_invalid", "Login reward state is invalid or inconsistent", 503)


def _total(value):
    if type(value) is not int or not 1 <= value <= MAX_BEGINNER_REWARDS:
        raise ProtocolError("login_rewards_count_invalid", "Beginner reward count must be an integer from 1 to 366")
    return value


def _clock(now):
    try:
        current = instant(now)
        return business_date(current), unix_milliseconds(current)
    except (ValueError, TypeError, OverflowError, ProtocolError) as error:
        raise ProtocolError("login_rewards_clock_invalid", "Login rewards require a representable server instant") from error


def _parse_day(value):
    if type(value) is not str or DAY_TEXT.fullmatch(value) is None:
        raise ValueError("Claim day must be a canonical calendar date")
    return date.fromisoformat(value)


def _row(state):
    if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1:
        _invalid()
    tables = state.get("tables")
    rows = tables.get("game_param2") if isinstance(tables, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        _invalid()
    row = rows[0]
    if any(not int32(row.get(name)) or row[name] < 0 for name in ("LastLoginDate", "LogInStampCount")):
        _invalid()
    return row


def read_login_rewards(state: dict) -> dict:
    row = _row(state)
    if LEDGER_KEY not in state:
        raise ProtocolError("login_rewards_required", "Login rewards require an initialized baseline", 503)
    ledger = state[LEDGER_KEY]
    try:
        if (not isinstance(ledger, dict) or ledger.keys() != _FIELDS
                or type(ledger["schema_version"]) is not int or ledger["schema_version"] != 1
                or type(ledger["beginner_eligible"]) is not bool
                or type(ledger["baseline_source"]) is not str or ledger["baseline_source"] not in (NEW_LOCAL, LEGACY)
                or type(ledger["beginner_total"]) is not int or not 1 <= ledger["beginner_total"] <= MAX_BEGINNER_REWARDS
                or not int32(ledger["beginner_claimed"]) or not 0 <= ledger["beginner_claimed"] <= ledger["beginner_total"]
                or ledger["beginner_eligible"] != (ledger["baseline_source"] == NEW_LOCAL)
                or not ledger["beginner_eligible"] and ledger["beginner_claimed"] != 0
                or type(ledger["last_time"]) is not int or not -INT64_MAX - 1 <= ledger["last_time"] <= INT64_MAX
                or not int32(ledger["stamp_count"])):
            _invalid()
        observed_day, _ = _clock(UNIX_EPOCH + timedelta(milliseconds=ledger["last_time"]))
        if ledger["last_claim_day"] is None:
            if (ledger["stamp_month"] is not None or ledger["stamp_count"] != 0
                    or ledger["beginner_claimed"] != 0 or row["LastLoginDate"] != 0 or row["LogInStampCount"] != 0):
                _invalid()
        else:
            claimed = _parse_day(ledger["last_claim_day"])
            if (claimed.toordinal() <= 1 or claimed > observed_day or ledger["stamp_month"] != month_key(claimed)
                    or not 1 <= ledger["stamp_count"] <= claimed.day
                    or row["LastLoginDate"] != claimed.toordinal() - 1
                    or row["LogInStampCount"] != ledger["stamp_count"]):
                _invalid()
    except (ValueError, TypeError, OverflowError, ProtocolError) as error:
        if isinstance(error, ProtocolError) and error.code == "login_rewards_invalid":
            raise
        raise ProtocolError("login_rewards_invalid", "Login reward state is invalid or inconsistent", 503) from error
    return deepcopy(ledger)


def _replace(state, ledger):
    updated = deepcopy(state)
    updated[LEDGER_KEY] = deepcopy(ledger)
    row = updated["tables"]["game_param2"][0]
    row["LastLoginDate"] = 0 if ledger["last_claim_day"] is None else _parse_day(ledger["last_claim_day"]).toordinal() - 1
    row["LogInStampCount"] = ledger["stamp_count"]
    read_login_rewards(updated)
    return updated


def _same_total(ledger, beginner_count):
    if ledger["beginner_total"] != beginner_count:
        raise ProtocolError(RULES_CHANGED, "Saved beginner reward count requires an explicit migration", 503)


def read_current_login_rewards(state: dict, *, beginner_count: int) -> dict:
    _total(beginner_count)
    ledger = read_login_rewards(state)
    _same_total(ledger, beginner_count)
    return ledger


def rebase_beginner(state: dict, *, beginner_count: int) -> dict:
    _total(beginner_count)
    ledger = read_login_rewards(state)
    if ledger["beginner_total"] == beginner_count:
        raise ProtocolError(REBASE_UNNEEDED, "Saved beginner reward count already follows the current rules", 409)
    # Rewards already claimed stay claimed; a smaller total only stops further beginner rewards.
    ledger.update(beginner_total=beginner_count, beginner_claimed=min(ledger["beginner_claimed"], beginner_count))
    return _replace(state, ledger)


def initialize_login_rewards(state: dict, now: datetime, *, new_account=False, beginner_count=7) -> dict:
    _total(beginner_count)
    if type(new_account) is not bool:
        raise ProtocolError("login_rewards_origin_invalid", "New-account eligibility must be an explicit boolean")
    if isinstance(state, dict) and LEDGER_KEY in state:
        # A changed beginner total stops only 115, so the save still loads and the console can rebase it.
        read_login_rewards(state)
        return deepcopy(state)
    row = _row(state)
    current_day, timestamp = _clock(now)
    encoded, count = row["LastLoginDate"], row["LogInStampCount"]
    claimed = None
    if new_account and (encoded or count):
        raise ProtocolError("login_rewards_baseline_invalid", "New accounts require an empty login reward baseline", 503)
    if encoded or count:
        try:
            claimed = date.fromordinal(encoded + 1)
            if encoded == 0 or not 1 <= count <= claimed.day or claimed > current_day:
                _invalid()
        except (ValueError, OverflowError) as error:
            raise ProtocolError("login_rewards_invalid", "Legacy login date or count is invalid", 503) from error
    ledger = {"schema_version": 1, "last_claim_day": claimed.isoformat() if claimed else None,
        "stamp_month": month_key(claimed) if claimed else None, "stamp_count": count,
        "beginner_eligible": new_account, "beginner_claimed": 0, "beginner_total": beginner_count,
        "last_time": timestamp, "baseline_source": NEW_LOCAL if new_account else LEGACY}
    return _replace(state, ledger)


def advance_login_rewards(state: dict, now: datetime, *, beginner_count=7) -> tuple[dict, LoginAdvance]:
    ledger = read_current_login_rewards(state, beginner_count=beginner_count)
    current_day, timestamp = _clock(now)
    if timestamp < ledger["last_time"]:
        raise ProtocolError("login_rewards_clock_backwards", "Login reward clock precedes the saved instant", 409)
    day = current_day.isoformat()
    if ledger["last_claim_day"] is not None and day < ledger["last_claim_day"]:
        raise ProtocolError("login_rewards_clock_backwards", "Login reward day precedes the saved claim", 409)
    new_day = day != ledger["last_claim_day"]
    beginner_ordinal = 0
    if new_day:
        month = month_key(current_day)
        ledger["stamp_count"] = ledger["stamp_count"] + 1 if ledger["stamp_month"] == month else 1
        ledger["stamp_month"] = month
        ledger["last_claim_day"] = day
        if ledger["beginner_eligible"] and ledger["beginner_claimed"] < ledger["beginner_total"]:
            ledger["beginner_claimed"] += 1
            beginner_ordinal = ledger["beginner_claimed"]
    ledger["last_time"] = timestamp
    remaining = ledger["beginner_total"] - ledger["beginner_claimed"] if ledger["beginner_eligible"] else 0
    advance = LoginAdvance(day, current_day.year, current_day.month, ledger["stamp_count"], new_day,
                           beginner_ordinal, remaining)
    return _replace(state, ledger), advance
