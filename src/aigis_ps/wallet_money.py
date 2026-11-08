from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields

from .errors import ProtocolError
from .native_numbers import int32


# 6C5870 refuses a gold present once Money would pass 0x5F5E0FF (DoFailed_OverFlow_Gold).
GOLD_LIMIT = 99_999_999
NATIVE_BALANCE_MESSAGE = "Native startup requires money or player experience to remain positive"


def _nonnegative_int32(value: object) -> bool:
    return int32(value) and value >= 0


@dataclass(frozen=True)
class MoneyRefusal:
    code: str
    message: str

    def __post_init__(self):
        if type(self.code) is not str or not self.code or type(self.message) is not str or not self.message:
            raise ValueError("Money refusals require a nonempty code and message")

    def error(self) -> ProtocolError:
        return ProtocolError(self.code, self.message, 409)


@dataclass(frozen=True)
class MoneyRefusals:
    capacity: MoneyRefusal = MoneyRefusal("money_capacity_exceeded", "Money would exceed its capacity")
    insufficient: MoneyRefusal = MoneyRefusal("money_insufficient", "Money costs exceed the saved balance")
    native_balance: MoneyRefusal = MoneyRefusal("money_native_balance", NATIVE_BALANCE_MESSAGE)

    def __post_init__(self):
        if any(not isinstance(getattr(self, field.name), MoneyRefusal) for field in fields(self)):
            raise ValueError("Money refusals must be MoneyRefusal definitions")


@dataclass(frozen=True)
class MoneyBalance:
    money: int
    experience: int


MONEY_REFUSALS = MoneyRefusals()


def money_balance(state: dict) -> MoneyBalance:
    if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1:
        raise ProtocolError("invalid_money_state", "GameParam storage schema is missing or unsupported", 503)
    tables = state.get("tables")
    rows = tables.get("game_param") if isinstance(tables, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ProtocolError("invalid_money_state", "Expected one persisted GameParam row", 503)
    money, experience = rows[0].get("Money"), rows[0].get("Experience")
    if not _nonnegative_int32(money) or not _nonnegative_int32(experience):
        raise ProtocolError("invalid_money_state", "GameParam money and experience must be nonnegative int32 values", 503)
    if money == experience == 0:
        raise ProtocolError("invalid_money_state",
                            "The native GameParam loader treats zero money and experience as a new account", 503)
    return MoneyBalance(money, experience)


def _checked_cap(cap: object, code: str) -> int:
    if not _nonnegative_int32(cap) or cap == 0:
        raise ProtocolError(code, "Money capacity must be a positive int32 value")
    return cap


def _checked_refusals(refusals: object, code: str) -> MoneyRefusals:
    if not isinstance(refusals, MoneyRefusals):
        raise ProtocolError(code, "Money refusals must be a MoneyRefusals definition")
    return refusals


def credit_space(state: dict, *, cap: int) -> int:
    limit = _checked_cap(cap, "invalid_money_credit")
    return max(0, limit - money_balance(state).money)


def _money_before_credit(state: dict, amount: int) -> int:
    if not _nonnegative_int32(amount):
        raise ProtocolError("invalid_money_credit", "Money credits require a nonnegative int32 amount")
    return money_balance(state).money


def money_after_credit(state: dict, amount: int, *, cap: int, refusals: MoneyRefusals = MONEY_REFUSALS) -> int:
    limit = _checked_cap(cap, "invalid_money_credit")
    refusals = _checked_refusals(refusals, "invalid_money_credit")
    money = _money_before_credit(state, amount)
    # A zero credit adds nothing, so it never refuses a legacy balance saved above the cap.
    if amount and money + amount > limit:
        raise refusals.capacity.error()
    return money + amount


def money_after_capped_credit(state: dict, amount: int, *, cap: int) -> int:
    limit = _checked_cap(cap, "invalid_money_credit")
    money = _money_before_credit(state, amount)
    # Like items over Stack the excess is discarded; a credit never lowers a balance saved above the cap.
    return max(money, min(money + amount, limit))


def money_after_debit(state: dict, amount: int, *, refusals: MoneyRefusals = MONEY_REFUSALS) -> int:
    refusals = _checked_refusals(refusals, "invalid_money_debit")
    if not _nonnegative_int32(amount):
        raise ProtocolError("invalid_money_debit", "Money debits require a nonnegative int32 amount")
    balance = money_balance(state)
    if amount > balance.money:
        raise refusals.insufficient.error()
    if amount == balance.money and balance.experience == 0:
        raise refusals.native_balance.error()
    return balance.money - amount


def money_after_set(state: dict, value: int, *, cap: int, refusals: MoneyRefusals = MONEY_REFUSALS) -> int:
    limit = _checked_cap(cap, "invalid_money_set")
    refusals = _checked_refusals(refusals, "invalid_money_set")
    if not _nonnegative_int32(value):
        raise ProtocolError("invalid_money_set", "Money balances must be nonnegative int32 values")
    balance = money_balance(state)
    # A balance saved above the cap stays readable and may only move down.
    if value > limit and value > balance.money:
        raise refusals.capacity.error()
    if value == 0 and balance.experience == 0:
        raise refusals.native_balance.error()
    return value


def _with_money(state: dict, money: int) -> dict:
    updated = deepcopy(state)
    updated["tables"]["game_param"][0]["Money"] = money
    return updated


def credit_money(state: dict, amount: int, *, cap: int, refusals: MoneyRefusals = MONEY_REFUSALS) -> dict:
    return _with_money(state, money_after_credit(state, amount, cap=cap, refusals=refusals))


def credit_money_capped(state: dict, amount: int, *, cap: int) -> dict:
    return _with_money(state, money_after_capped_credit(state, amount, cap=cap))


def debit_money(state: dict, amount: int, *, refusals: MoneyRefusals = MONEY_REFUSALS) -> dict:
    return _with_money(state, money_after_debit(state, amount, refusals=refusals))


def set_money(state: dict, value: int, *, cap: int, refusals: MoneyRefusals = MONEY_REFUSALS) -> dict:
    return _with_money(state, money_after_set(state, value, cap=cap, refusals=refusals))
