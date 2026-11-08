from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from .errors import ProtocolError
from .native_numbers import INT32_MAX, INT32_MIN, int32
from .possessions import MAX_ITEM_NAME_BYTES


MAX_ENTRIES = 65535
MAX_MAIL_IDS = 65535
MAX_REASON_BYTES = 256
_ID = re.compile(r"[1-9][0-9]{0,9}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
LEDGER_KEY = "rank_rewards"
PENDING, DELIVERED = "pending", "delivered"
RESPONSE_CAPACITY = "rank_reward_response_capacity"
_BONUS_FIELDS = ("bonus_id", "rank", "incentive_id", "reward_main_type", "reward_sub_type", "parameter", "item_id")


def _integer(value, minimum=0):
    return int32(value) and value >= minimum


def _invalid():
    raise ProtocolError("rank_rewards_invalid", "Rank reward state is invalid or unsupported", 503)


def _reason(value):
    if type(value) is not str or not value or any(ord(char) < 32 for char in value):
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_REASON_BYTES
    except UnicodeError:
        return False


def _rank(state):
    if not isinstance(state, dict) or not _integer(state.get("bootstrap_schema"), 1) or state["bootstrap_schema"] != 1:
        _invalid()
    tables = state.get("tables")
    rows = tables.get("game_param") if isinstance(tables, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or not _integer(rows[0].get("Rank"), 1):
        _invalid()
    return rows[0]["Rank"]


def read_rank_rewards(state: dict) -> dict:
    _rank(state)
    if LEDGER_KEY not in state:
        raise ProtocolError("rank_rewards_required", "Rank rewards require an initialized baseline", 503)
    ledger = state[LEDGER_KEY]
    if (not isinstance(ledger, dict) or set(ledger) != {"schema_version", "baseline_rank", "observed_rank", "entries"}
            or not _integer(ledger["schema_version"], 1) or ledger["schema_version"] != 1
            or not _integer(ledger["baseline_rank"]) or not _integer(ledger["observed_rank"])
            or ledger["baseline_rank"] > ledger["observed_rank"]
            or not isinstance(ledger["entries"], dict) or len(ledger["entries"]) > MAX_ENTRIES):
        _invalid()
    seen_mails = set()
    for key, entry in ledger["entries"].items():
        if (type(key) is not str or not _ID.fullmatch(key) or int(key) > INT32_MAX
                or not isinstance(entry, dict) or set(entry) != {"rank", "definition", "status", "mail_ids", "reason"}
                or not _integer(entry["rank"]) or not ledger["baseline_rank"] < entry["rank"] <= ledger["observed_rank"]
                or type(entry["definition"]) is not str or not _HASH.fullmatch(entry["definition"])
                or type(entry["status"]) is not str or entry["status"] not in (PENDING, DELIVERED)
                or not isinstance(entry["mail_ids"], list)
                or any(not _integer(identifier, 1) for identifier in entry["mail_ids"])
                or len(set(entry["mail_ids"])) != len(entry["mail_ids"])
                or seen_mails.intersection(entry["mail_ids"])
                or entry["reason"] is not None and not _reason(entry["reason"])):
            _invalid()
        seen_mails.update(entry["mail_ids"])
        if (len(seen_mails) > MAX_MAIL_IDS or entry["status"] == PENDING and entry["mail_ids"]
                or entry["status"] == DELIVERED and (not entry["mail_ids"] or entry["reason"] is not None)):
            _invalid()
    return deepcopy(ledger)


def _replace(state, ledger):
    updated = deepcopy(state)
    updated[LEDGER_KEY] = deepcopy(ledger)
    read_rank_rewards(updated)
    return updated


def initialize_rank_rewards(state: dict, *, new_account=False) -> dict:
    if type(new_account) is not bool:
        raise ProtocolError("rank_reward_origin_invalid", "New-account eligibility must be a boolean")
    current = _rank(state)
    if LEDGER_KEY in state:
        read_rank_rewards(state)
        return deepcopy(state)
    baseline = 0 if new_account else current
    return _replace(state, {"schema_version": 1, "baseline_rank": baseline,
                            "observed_rank": baseline, "entries": {}})


def bonus_fingerprint(bonus) -> str:
    try:
        semantic = {name: getattr(bonus, name) for name in _BONUS_FIELDS}
        if (any(not _integer(semantic[name], 1) for name in ("bonus_id", "rank", "incentive_id"))
                or any(not _integer(semantic[name], INT32_MIN)
                       for name in ("reward_main_type", "reward_sub_type", "parameter"))
                or type(semantic["item_id"]) is not str
                or len(semantic["item_id"].encode("utf-8")) > MAX_ITEM_NAME_BYTES):
            raise ValueError("Invalid reward semantics")
        payload = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (AttributeError, ValueError, TypeError, UnicodeError) as error:
        raise ProtocolError("rank_reward_definition_invalid", "Rank reward semantics are invalid or unsupported", 503) from error
    return hashlib.sha256(payload).hexdigest()


def observe_rank_rewards(state: dict, catalog) -> dict:
    ledger = read_rank_rewards(state)
    current = _rank(state)
    observed = ledger["observed_rank"]
    if current < observed:
        raise ProtocolError("rank_reward_rank_backwards", "Player rank is below the saved reward observation", 409)
    if current == observed:
        return deepcopy(state)
    bonuses = catalog.between(observed, current)
    if not isinstance(bonuses, (list, tuple)) or len(bonuses) > MAX_ENTRIES:
        raise ProtocolError("rank_reward_definition_invalid", "Rank reward range must be a bounded sequence", 503)
    seen = set()
    for bonus in bonuses:
        definition = bonus_fingerprint(bonus)
        if bonus.bonus_id in seen or not observed < bonus.rank <= current:
            raise ProtocolError("rank_reward_definition_invalid", "Rank reward range contains duplicate or out-of-range entries", 503)
        seen.add(bonus.bonus_id)
        key = str(bonus.bonus_id)
        if key not in ledger["entries"]:
            ledger["entries"][key] = {"rank": bonus.rank, "definition": definition,
                "status": PENDING, "mail_ids": [], "reason": None}
    if len(ledger["entries"]) > MAX_ENTRIES:
        raise ProtocolError("rank_reward_capacity", "Rank reward history exceeds its supported capacity", 409)
    ledger["observed_rank"] = current
    return _replace(state, ledger)


def mark_rank_reward(state: dict, bonus_id: int, *, mail_ids=None, reason=None) -> dict:
    ledger = read_rank_rewards(state)
    if not _integer(bonus_id, 1) or str(bonus_id) not in ledger["entries"]:
        raise ProtocolError("rank_reward_unknown", "Rank reward has not been observed", 409)
    if (reason is not None and not _reason(reason)
            or mail_ids is not None and reason is not None):
        raise ProtocolError("rank_reward_mark_invalid", "Rank reward delivery and pending reason are inconsistent")
    if mail_ids is not None:
        if (not isinstance(mail_ids, (tuple, list)) or not mail_ids
                or any(not _integer(identifier, 1) for identifier in mail_ids)
                or len(set(mail_ids)) != len(mail_ids)):
            raise ProtocolError("rank_reward_mark_invalid", "Delivered rank rewards require distinct positive mail identities")
        mail_ids = list(mail_ids)
    entry = ledger["entries"][str(bonus_id)]
    if entry["status"] == DELIVERED:
        if reason is not None or mail_ids is not None and mail_ids != entry["mail_ids"]:
            raise ProtocolError("rank_reward_already_delivered", "Delivered rank rewards cannot be changed", 409)
        return deepcopy(state)
    if mail_ids is not None:
        other_mails = {identifier for key, row in ledger["entries"].items() if key != str(bonus_id)
                       for identifier in row["mail_ids"]}
        if other_mails.intersection(mail_ids):
            raise ProtocolError("rank_reward_mail_conflict", "Mail identities already belong to another rank reward", 409)
        if len(other_mails) + len(mail_ids) > MAX_MAIL_IDS:
            raise ProtocolError("rank_reward_capacity", "Rank reward mail history exceeds its supported capacity", 409)
        entry.update(status=DELIVERED, mail_ids=mail_ids, reason=None)
    elif reason is not None:
        entry["reason"] = reason
    return _replace(state, ledger)
