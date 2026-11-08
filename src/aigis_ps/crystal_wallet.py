from __future__ import annotations

from copy import deepcopy
from types import MappingProxyType

from .alxml import Column, Container, ValueType
from .errors import ProtocolError
from .native_numbers import INT32_MAX, int32


FREE_CRYSTAL_LIMIT = 1000
PAID_CRYSTAL_LIMIT = 1000
TOTAL_CRYSTAL_LIMIT = 2000
CRYSTAL_LIMITS = MappingProxyType({"paid": PAID_CRYSTAL_LIMIT, "free": FREE_CRYSTAL_LIMIT,
                                   "total": TOTAL_CRYSTAL_LIMIT})


def _nonnegative_int32(value: object) -> bool:
    return int32(value) and value >= 0


def _validated_wallet(state: dict) -> tuple[list[dict], dict[str, int]]:
    if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1:
        raise ProtocolError("invalid_crystal_state", "Crystal storage schema is missing or unsupported", 503)
    tables = state.get("tables")
    rows = tables.get("crystals") if isinstance(tables, dict) else None
    if not isinstance(rows, list):
        raise ProtocolError("invalid_crystal_state", "Crystal storage requires a persisted bucket list", 503)
    totals = {"paid": 0, "free": 0, "total": 0}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or type(row.get("FreeFlag")) is not bool:
            raise ProtocolError("invalid_crystal_state", "Crystal buckets require a boolean FreeFlag", 503)
        if not _nonnegative_int32(row.get("Num")) or not _nonnegative_int32(row.get("Date")):
            raise ProtocolError("invalid_crystal_state", "Crystal quantities and acquisition days must be nonnegative int32 values", 503)
        key = row["FreeFlag"], row["Date"]
        if key in seen:
            raise ProtocolError("invalid_crystal_state", "Local crystal buckets must be unique by FreeFlag and Date", 503)
        seen.add(key)
        totals["free" if row["FreeFlag"] else "paid"] += row["Num"]
        totals["total"] += row["Num"]
        if totals["total"] > INT32_MAX:
            raise ProtocolError("invalid_crystal_state", "Persisted crystal total exceeds the int32 limit", 503)
    return rows, totals


def wallet_totals(state: dict) -> dict[str, int]:
    return _validated_wallet(state)[1]


def free_crystals_fit(state: dict, amount: int) -> bool:
    if not _nonnegative_int32(amount):
        raise ProtocolError("invalid_crystal_credit", "Crystal capacity checks require a nonnegative int32 amount")
    # 458330 weighs only the free buckets against the cap when choosing wallet or present delivery.
    return wallet_totals(state)["free"] + amount <= FREE_CRYSTAL_LIMIT


def claim_crystals_fit(state: dict, amount: int) -> bool:
    # Summed mail may pass int32; such an amount simply does not fit.
    if type(amount) is not int or amount < 0:
        raise ProtocolError("invalid_crystal_credit", "Crystal capacity checks require a nonnegative integer amount")
    # A claim must pass 65EDE0's three caps, unlike free_crystals_fit's settlement choice of wallet or mail.
    totals = wallet_totals(state)
    return (totals["paid"] <= PAID_CRYSTAL_LIMIT and totals["free"] + amount <= FREE_CRYSTAL_LIMIT
            and totals["total"] + amount <= TOTAL_CRYSTAL_LIMIT)


def _crystal_space(state: dict, *, free: bool) -> int:
    totals = wallet_totals(state)
    credited, other = ("free", "paid") if free else ("paid", "free")
    # 65EDE0 applies its three caps with the amount counted on the credited side.
    if totals[other] > CRYSTAL_LIMITS[other]:
        return 0
    return max(0, min(CRYSTAL_LIMITS[credited] - totals[credited], CRYSTAL_LIMITS["total"] - totals["total"]))


def claim_crystal_space(state: dict) -> int:
    return _crystal_space(state, free=True)


def paid_crystal_space(state: dict) -> int:
    return _crystal_space(state, free=False)


def _credit_crystals(state: dict, amount: int, acquisition_day: int, *, free: bool) -> dict:
    rows, totals = _validated_wallet(state)
    if not _nonnegative_int32(amount) or amount == 0:
        raise ProtocolError("invalid_crystal_credit", "Crystal credits require a positive int32 amount")
    if not _nonnegative_int32(acquisition_day) or acquisition_day == 0:
        raise ProtocolError("invalid_crystal_credit", "New crystal credits require a positive int32 acquisition day")
    if totals["total"] + amount > INT32_MAX:
        raise ProtocolError("crystal_capacity_exceeded", "Crystal credit exceeds the int32 total limit")
    updated = deepcopy(state)
    for index, row in enumerate(rows):
        if row["FreeFlag"] is free and row["Date"] == acquisition_day:
            updated["tables"]["crystals"][index]["Num"] += amount
            break
    else:
        updated["tables"]["crystals"].append({"FreeFlag": free, "Num": amount, "Date": acquisition_day})
    return updated


def credit_free_crystals(state: dict, amount: int, acquisition_day: int) -> dict:
    return _credit_crystals(state, amount, acquisition_day, free=True)


def credit_paid_crystals(state: dict, amount: int, acquisition_day: int) -> dict:
    return _credit_crystals(state, amount, acquisition_day, free=False)


def debit_crystals(state: dict, amount: int, *, paid_only: bool = False, free_first: bool = True) -> dict:
    rows, totals = _validated_wallet(state)
    if not _nonnegative_int32(amount) or type(paid_only) is not bool or type(free_first) is not bool:
        raise ProtocolError("invalid_crystal_debit", "Crystal debit requires a nonnegative amount and boolean policies")
    if amount > totals["paid" if paid_only else "total"]:
        raise ProtocolError("crystal_insufficient", "Crystal debit exceeds the eligible saved balance", 409)
    indices = [index for index, row in enumerate(rows) if not paid_only or row["FreeFlag"] is False]
    indices.sort(key=lambda index: (rows[index]["FreeFlag"] is not free_first, rows[index]["Date"]))
    updated = deepcopy(state)
    remaining = amount
    for index in indices:
        used = min(remaining, rows[index]["Num"])
        updated["tables"]["crystals"][index]["Num"] -= used
        remaining -= used
        if remaining == 0:
            break
    return updated


def _crystal_response(state: dict, player_id: int | str, *, free: bool) -> Container:
    rows, _ = _validated_wallet(state)
    if isinstance(player_id, str) and 1 <= len(player_id) <= 10 and player_id.isascii() and player_id.isdecimal():
        player_id = int(player_id)
    if not _nonnegative_int32(player_id) or player_id == 0:
        raise ProtocolError("invalid_crystal_player_id", "Crystal responses require a positive int32 player ID", 503)
    selected = [row for row in rows if row["FreeFlag"] is free]
    # Each receiver replaces its entire category, including an empty bucket list.
    return Container("PFC" if free else "PPC", (
        Column("UserID", ValueType.INT, (player_id,) * len(selected)),
        Column("Date", ValueType.INT, tuple(row["Date"] for row in selected)),
        Column("Num", ValueType.INT, tuple(row["Num"] for row in selected)),
    ))


def free_crystal_response(state: dict, player_id: int | str) -> Container:
    return _crystal_response(state, player_id, free=True)


def paid_crystal_response(state: dict, player_id: int | str) -> Container:
    return _crystal_response(state, player_id, free=False)
