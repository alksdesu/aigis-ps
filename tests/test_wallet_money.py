from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.presents import GOLD_LIMIT as PRESENT_GOLD_LIMIT
from aigis_ps.wallet_money import (
    GOLD_LIMIT, MONEY_REFUSALS, NATIVE_BALANCE_MESSAGE, MoneyBalance, MoneyRefusal, MoneyRefusals, credit_money,
    credit_money_capped, credit_space, debit_money, money_after_capped_credit, money_after_credit, money_after_debit,
    money_after_set, money_balance, set_money,
)


CALL_SITE_REFUSALS = MoneyRefusals(
    capacity=MoneyRefusal("present_gold_capacity", "Claim would exceed the native gold capacity"),
    insufficient=MoneyRefusal("evolution_money_insufficient", "Evolution costs exceed the saved money balance"),
    native_balance=MoneyRefusal("evolution_native_balance", NATIVE_BALANCE_MESSAGE),
)


@pytest.fixture
def state():
    return {"bootstrap_schema": 1, "tables": {
        "game_param": [{"Money": 5000, "Experience": 120, "Rank": 3, "NowCharisma": 10, "CharismaChangeTime": 0.25}],
        "crystals": [{"FreeFlag": True, "Num": 3, "Date": 739920}]}, "player_state": {"units": [{"GetIndex": 9}]}}


def with_wallet(state, **values):
    state["tables"]["game_param"][0].update(values)
    return state


def refused(code, status, callback):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert (caught.value.code, caught.value.status) == (code, status)
    return caught.value


def expected_money(state, money):
    expected = deepcopy(state)
    expected["tables"]["game_param"][0]["Money"] = money
    return expected


def test_credit_changes_only_money_and_leaves_the_input_untouched(state):
    before = deepcopy(state)
    updated = credit_money(state, 250, cap=GOLD_LIMIT)
    assert updated == expected_money(before, 5250)
    updated["tables"]["crystals"][0]["Num"] = 0
    updated["player_state"]["units"][0]["GetIndex"] = 1
    updated["tables"]["game_param"][0]["Rank"] = 99
    assert state == before


def test_credit_reaches_the_exact_cap_and_refuses_one_more(state):
    with_wallet(state, Money=GOLD_LIMIT - 10)
    before = deepcopy(state)
    assert credit_money(state, 10, cap=GOLD_LIMIT)["tables"]["game_param"][0]["Money"] == GOLD_LIMIT
    error = refused("money_capacity_exceeded", 409, lambda: credit_money(state, 11, cap=GOLD_LIMIT))
    assert str(error) == "Money would exceed its capacity"
    assert state == before


def test_int32_cap_refuses_overflow(state):
    with_wallet(state, Money=INT32_MAX - 1)
    assert money_after_credit(state, 1, cap=INT32_MAX) == INT32_MAX
    refused("money_capacity_exceeded", 409, lambda: credit_money(state, 2, cap=INT32_MAX))
    refused("money_capacity_exceeded", 409, lambda: credit_money(state, INT32_MAX, cap=INT32_MAX))


def test_legacy_balance_above_the_cap_accepts_no_increase(state):
    with_wallet(state, Money=GOLD_LIMIT + 5)
    before = deepcopy(state)
    unchanged = credit_money(state, 0, cap=GOLD_LIMIT)
    assert unchanged == before and unchanged is not state
    assert unchanged["tables"] is not state["tables"]
    refused("money_capacity_exceeded", 409, lambda: credit_money(state, 1, cap=GOLD_LIMIT))
    assert credit_space(state, cap=GOLD_LIMIT) == 0
    assert credit_money(state, 1, cap=INT32_MAX)["tables"]["game_param"][0]["Money"] == GOLD_LIMIT + 6
    assert state == before


def test_gold_limit_is_the_present_claim_limit():
    assert GOLD_LIMIT == PRESENT_GOLD_LIMIT == 99_999_999


def test_capped_credit_discards_only_the_excess(state):
    with_wallet(state, Money=GOLD_LIMIT - 10)
    before = deepcopy(state)
    assert money_after_capped_credit(state, 7, cap=GOLD_LIMIT) == GOLD_LIMIT - 3
    assert money_after_capped_credit(state, 10, cap=GOLD_LIMIT) == GOLD_LIMIT
    assert money_after_capped_credit(state, 11, cap=GOLD_LIMIT) == GOLD_LIMIT
    assert credit_money_capped(state, INT32_MAX, cap=GOLD_LIMIT) == expected_money(before, GOLD_LIMIT)
    assert state == before
    with_wallet(state, Money=INT32_MAX - 1)
    assert money_after_capped_credit(state, INT32_MAX, cap=INT32_MAX) == INT32_MAX


@pytest.mark.parametrize("money", [GOLD_LIMIT, GOLD_LIMIT + 5, INT32_MAX])
def test_capped_credit_never_lowers_a_balance_at_or_above_the_cap(state, money):
    with_wallet(state, Money=money)
    before = deepcopy(state)
    for amount in (0, 1, INT32_MAX):
        assert money_after_capped_credit(state, amount, cap=GOLD_LIMIT) == money
    unchanged = credit_money_capped(state, 1, cap=GOLD_LIMIT)
    assert unchanged == before and unchanged["tables"] is not state["tables"]
    assert money_after_capped_credit(state, 1, cap=INT32_MAX) == min(money + 1, INT32_MAX)
    assert state == before


def test_credit_space_reports_the_remaining_room_under_each_cap(state):
    assert credit_space(state, cap=GOLD_LIMIT) == GOLD_LIMIT - 5000
    assert credit_space(state, cap=INT32_MAX) == INT32_MAX - 5000
    assert credit_space(with_wallet(state, Money=GOLD_LIMIT), cap=GOLD_LIMIT) == 0


def test_debit_lowers_money_and_preserves_other_state(state):
    before = deepcopy(state)
    updated = debit_money(state, 1200)
    assert updated == expected_money(before, 3800)
    updated["player_state"]["units"].clear()
    assert state == before


def test_debit_refuses_more_than_the_saved_balance(state):
    before = deepcopy(state)
    error = refused("money_insufficient", 409, lambda: debit_money(state, 5001))
    assert str(error) == "Money costs exceed the saved balance"
    assert state == before


def test_spending_the_whole_balance_needs_experience_to_stay_loadable(state):
    assert debit_money(state, 5000)["tables"]["game_param"][0]["Money"] == 0
    with_wallet(state, Experience=0)
    error = refused("money_native_balance", 409, lambda: debit_money(state, 5000))
    assert str(error) == NATIVE_BALANCE_MESSAGE
    assert money_after_debit(state, 4999) == 1
    assert money_after_debit(state, 0) == 5000


def test_debit_keeps_working_for_a_legacy_balance_above_the_cap(state):
    with_wallet(state, Money=GOLD_LIMIT + 100)
    assert debit_money(state, 50)["tables"]["game_param"][0]["Money"] == GOLD_LIMIT + 50


def test_set_moves_within_the_cap_and_refuses_to_raise_beyond_it(state):
    before = deepcopy(state)
    assert set_money(state, 777, cap=GOLD_LIMIT) == expected_money(before, 777)
    assert set_money(state, GOLD_LIMIT, cap=GOLD_LIMIT)["tables"]["game_param"][0]["Money"] == GOLD_LIMIT
    refused("money_capacity_exceeded", 409, lambda: set_money(state, GOLD_LIMIT + 1, cap=GOLD_LIMIT))
    assert set_money(state, GOLD_LIMIT + 1, cap=INT32_MAX)["tables"]["game_param"][0]["Money"] == GOLD_LIMIT + 1
    assert state == before


def test_set_only_lowers_a_legacy_balance_above_the_cap(state):
    with_wallet(state, Money=GOLD_LIMIT + 100)
    assert money_after_set(state, GOLD_LIMIT + 50, cap=GOLD_LIMIT) == GOLD_LIMIT + 50
    assert money_after_set(state, GOLD_LIMIT + 100, cap=GOLD_LIMIT) == GOLD_LIMIT + 100
    assert money_after_set(state, 10, cap=GOLD_LIMIT) == 10
    refused("money_capacity_exceeded", 409, lambda: set_money(state, GOLD_LIMIT + 101, cap=GOLD_LIMIT))


def test_set_to_zero_needs_experience_to_stay_loadable(state):
    assert set_money(state, 0, cap=GOLD_LIMIT)["tables"]["game_param"][0]["Money"] == 0
    with_wallet(state, Experience=0)
    refused("money_native_balance", 409, lambda: set_money(state, 0, cap=GOLD_LIMIT))
    assert money_after_set(state, 1, cap=GOLD_LIMIT) == 1


def test_checks_match_the_state_transforms_without_copying_state(state):
    before = deepcopy(state)
    pairs = (
        (money_after_credit(state, 40, cap=GOLD_LIMIT), credit_money(state, 40, cap=GOLD_LIMIT)),
        (money_after_capped_credit(state, 40, cap=GOLD_LIMIT), credit_money_capped(state, 40, cap=GOLD_LIMIT)),
        (money_after_debit(state, 40), debit_money(state, 40)),
        (money_after_set(state, 40, cap=GOLD_LIMIT), set_money(state, 40, cap=GOLD_LIMIT)),
    )
    for money, updated in pairs:
        assert updated == expected_money(before, money)
    assert state == before


def test_call_sites_keep_their_own_refusal_codes_and_messages(state):
    with_wallet(state, Money=GOLD_LIMIT, Experience=0)
    cases = (
        (lambda: credit_money(state, 1, cap=GOLD_LIMIT, refusals=CALL_SITE_REFUSALS), CALL_SITE_REFUSALS.capacity),
        (lambda: set_money(state, GOLD_LIMIT + 1, cap=GOLD_LIMIT, refusals=CALL_SITE_REFUSALS),
         CALL_SITE_REFUSALS.capacity),
        (lambda: debit_money(state, GOLD_LIMIT + 1, refusals=CALL_SITE_REFUSALS), CALL_SITE_REFUSALS.insufficient),
        (lambda: money_after_debit(state, GOLD_LIMIT, refusals=CALL_SITE_REFUSALS), CALL_SITE_REFUSALS.native_balance),
        (lambda: set_money(state, 0, cap=GOLD_LIMIT, refusals=CALL_SITE_REFUSALS), CALL_SITE_REFUSALS.native_balance),
    )
    for callback, refusal in cases:
        error = refused(refusal.code, 409, callback)
        assert str(error) == refusal.message


def test_partial_refusals_keep_the_remaining_defaults(state):
    refusals = MoneyRefusals(capacity=CALL_SITE_REFUSALS.capacity)
    assert (refusals.insufficient, refusals.native_balance) == (MONEY_REFUSALS.insufficient,
                                                                MONEY_REFUSALS.native_balance)
    refused("money_insufficient", 409, lambda: money_after_debit(state, 5001, refusals=refusals))
    with_wallet(state, Money=INT32_MAX)
    error = refused(CALL_SITE_REFUSALS.capacity.code, 409,
                    lambda: money_after_credit(state, 1, cap=INT32_MAX, refusals=refusals))
    assert str(error) == CALL_SITE_REFUSALS.capacity.message


def test_default_refusals_are_frozen_and_validated():
    assert MONEY_REFUSALS == MoneyRefusals()
    assert (MONEY_REFUSALS.capacity.code, MONEY_REFUSALS.insufficient.code, MONEY_REFUSALS.native_balance.code) == (
        "money_capacity_exceeded", "money_insufficient", "money_native_balance")
    error = MONEY_REFUSALS.capacity.error()
    assert (error.code, error.status, str(error)) == ("money_capacity_exceeded", 409, "Money would exceed its capacity")
    with pytest.raises(FrozenInstanceError):
        MONEY_REFUSALS.capacity = MONEY_REFUSALS.insufficient
    with pytest.raises(FrozenInstanceError):
        MONEY_REFUSALS.capacity.code = "changed"
    for code, message in (("", "message"), ("code", ""), (1, "message"), ("code", None)):
        with pytest.raises(ValueError):
            MoneyRefusal(code, message)
    with pytest.raises(ValueError):
        MoneyRefusals(capacity=("money_capacity_exceeded", "Money would exceed its capacity"))


def test_money_balance_reads_the_saved_wallet(state):
    assert money_balance(state) == MoneyBalance(5000, 120)
    assert money_balance(with_wallet(state, Money=0)) == MoneyBalance(0, 120)
    assert money_balance(with_wallet(state, Money=INT32_MAX, Experience=0)) == MoneyBalance(INT32_MAX, 0)


@pytest.mark.parametrize("amount", [-1, INT32_MAX + 1, True, 1.0, None, "1"])
def test_invalid_amounts_are_argument_errors(state, amount):
    before = deepcopy(state)
    refused("invalid_money_credit", 400, lambda: credit_money(state, amount, cap=GOLD_LIMIT))
    refused("invalid_money_credit", 400, lambda: money_after_credit(state, amount, cap=GOLD_LIMIT))
    refused("invalid_money_credit", 400, lambda: credit_money_capped(state, amount, cap=GOLD_LIMIT))
    refused("invalid_money_credit", 400, lambda: money_after_capped_credit(state, amount, cap=GOLD_LIMIT))
    refused("invalid_money_debit", 400, lambda: debit_money(state, amount))
    refused("invalid_money_set", 400, lambda: set_money(state, amount, cap=GOLD_LIMIT))
    assert state == before


@pytest.mark.parametrize("cap", [0, -1, INT32_MAX + 1, True, None, 1.5, "99999999"])
def test_invalid_caps_are_argument_errors(state, cap):
    refused("invalid_money_credit", 400, lambda: credit_money(state, 1, cap=cap))
    refused("invalid_money_credit", 400, lambda: credit_money_capped(state, 1, cap=cap))
    refused("invalid_money_credit", 400, lambda: credit_space(state, cap=cap))
    refused("invalid_money_set", 400, lambda: set_money(state, 1, cap=cap))


def test_cap_must_be_given_explicitly(state):
    for callback in (lambda: credit_money(state, 1), lambda: money_after_credit(state, 1),
                     lambda: credit_money_capped(state, 1), lambda: money_after_capped_credit(state, 1),
                     lambda: set_money(state, 1), lambda: money_after_set(state, 1), lambda: credit_space(state)):
        with pytest.raises(TypeError):
            callback()


@pytest.mark.parametrize("refusals", [None, "money_insufficient", MONEY_REFUSALS.capacity, {"capacity": None}])
def test_refusals_must_be_a_refusal_set(state, refusals):
    refused("invalid_money_credit", 400, lambda: credit_money(state, 1, cap=GOLD_LIMIT, refusals=refusals))
    refused("invalid_money_debit", 400, lambda: debit_money(state, 1, refusals=refusals))
    refused("invalid_money_set", 400, lambda: set_money(state, 1, cap=GOLD_LIMIT, refusals=refusals))


@pytest.mark.parametrize("invalid", [
    None, [], {}, {"bootstrap_schema": True, "tables": {"game_param": [{"Money": 1, "Experience": 1}]}},
    {"bootstrap_schema": 2, "tables": {"game_param": [{"Money": 1, "Experience": 1}]}},
    {"bootstrap_schema": 1, "tables": []}, {"bootstrap_schema": 1, "tables": {}},
    {"bootstrap_schema": 1, "tables": {"game_param": {}}}, {"bootstrap_schema": 1, "tables": {"game_param": []}},
    {"bootstrap_schema": 1, "tables": {"game_param": [{"Money": 1, "Experience": 1}, {"Money": 1, "Experience": 1}]}},
    {"bootstrap_schema": 1, "tables": {"game_param": [[1, 1]]}},
    {"bootstrap_schema": 1, "tables": {"game_param": [{"Experience": 1}]}},
    {"bootstrap_schema": 1, "tables": {"game_param": [{"Money": 1}]}},
    {"bootstrap_schema": 1, "tables": {"game_param": [{"Money": -1, "Experience": 1}]}},
    {"bootstrap_schema": 1, "tables": {"game_param": [{"Money": True, "Experience": 1}]}},
    {"bootstrap_schema": 1, "tables": {"game_param": [{"Money": 1.0, "Experience": 1}]}},
    {"bootstrap_schema": 1, "tables": {"game_param": [{"Money": INT32_MAX + 1, "Experience": 1}]}},
    {"bootstrap_schema": 1, "tables": {"game_param": [{"Money": 1, "Experience": -1}]}},
    {"bootstrap_schema": 1, "tables": {"game_param": [{"Money": 0, "Experience": 0}]}},
])
def test_invalid_wallet_state_is_unavailable(invalid):
    before = deepcopy(invalid)
    for callback in (lambda: money_balance(invalid), lambda: credit_space(invalid, cap=GOLD_LIMIT),
                     lambda: credit_money(invalid, 1, cap=GOLD_LIMIT),
                     lambda: credit_money_capped(invalid, 1, cap=GOLD_LIMIT), lambda: debit_money(invalid, 0),
                     lambda: set_money(invalid, 1, cap=GOLD_LIMIT)):
        refused("invalid_money_state", 503, callback)
    assert invalid == before
