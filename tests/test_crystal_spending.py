from copy import deepcopy

import pytest

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.crystal_wallet import credit_paid_crystals, debit_crystals, paid_crystal_response, wallet_totals
from aigis_ps.errors import ProtocolError
from aigis_ps.native_numbers import INT32_MAX
from test_crystal_wallet import state


def test_default_spend_uses_free_then_paid_and_preserves_bucket_order(state):
    before = deepcopy(state)
    updated = debit_crystals(state, 10)
    assert [row["Num"] for row in updated["tables"]["crystals"]] == [9, 0, 0]
    assert wallet_totals(updated) == {"paid": 9, "free": 0, "total": 9}
    assert state == before
    updated["tables"]["crystals"][2]["metadata"]["source"] = "changed"
    assert state == before


def test_dates_are_sorted_within_each_kind_without_reordering_saved_rows(state):
    state["tables"]["crystals"] = [
        {"FreeFlag": True, "Num": 4, "Date": 10}, {"FreeFlag": False, "Num": 5, "Date": 2},
        {"FreeFlag": True, "Num": 6, "Date": 1}, {"FreeFlag": False, "Num": 3, "Date": 0}]
    updated = debit_crystals(state, 12)
    assert [row["Num"] for row in updated["tables"]["crystals"]] == [0, 5, 0, 1]
    assert [row["Date"] for row in updated["tables"]["crystals"]] == [10, 2, 1, 0]


def test_alternate_priority_is_explicit_and_does_not_change_default(state):
    paid_first = debit_crystals(state, 13, free_first=False)
    assert [row["Num"] for row in paid_first["tables"]["crystals"]] == [0, 1, 5]
    assert [row["Num"] for row in debit_crystals(state, 13)["tables"]["crystals"]] == [6, 0, 0]


def test_paid_only_never_uses_free_currency_to_cover_shortage(state):
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        debit_crystals(state, 12, paid_only=True)
    assert caught.value.code == "crystal_insufficient" and caught.value.status == 409
    assert state == before
    assert [row["Num"] for row in debit_crystals(state, 11, paid_only=True)["tables"]["crystals"]] == [0, 3, 5]


def test_insufficient_balance_and_full_spend_are_atomic(state):
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        debit_crystals(state, 20)
    assert caught.value.code == "crystal_insufficient" and state == before
    updated = debit_crystals(state, 19)
    assert wallet_totals(updated)["total"] == 0
    assert len(updated["tables"]["crystals"]) == len(before["tables"]["crystals"])


def test_zero_spend_returns_an_independent_state_and_keeps_legacy_zero_buckets(state):
    state["tables"]["crystals"].append({"FreeFlag": True, "Num": 0, "Date": 0})
    before = deepcopy(state)
    updated = debit_crystals(state, 0)
    assert updated == before and updated is not state
    updated["tables"]["config"][0]["MenuTutorialFlags"] = 999
    assert state == before


@pytest.mark.parametrize("arguments", [{"amount": True}, {"amount": -1}, {"amount": 1.0},
    {"amount": "1"}, {"amount": INT32_MAX + 1}, {"amount": 1, "paid_only": 1},
    {"amount": 1, "free_first": "free"}])
def test_invalid_debit_arguments_do_not_mutate(state, arguments):
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        debit_crystals(state, **arguments)
    assert caught.value.code == "invalid_crystal_debit" and state == before


def test_paid_credit_uses_its_own_same_date_bucket(state):
    before = deepcopy(state)
    updated = credit_paid_crystals(state, 2, 739920)
    assert [row["Num"] for row in updated["tables"]["crystals"]] == [13, 3, 5]
    appended = credit_paid_crystals(state, 7, 739921)
    assert appended["tables"]["crystals"][-1] == {"FreeFlag": False, "Num": 7, "Date": 739921}
    assert state == before


@pytest.mark.parametrize("action", [lambda state: debit_crystals(state, 0),
                                  lambda state: credit_paid_crystals(state, 1, 1)])
def test_new_paths_validate_the_entire_existing_wallet(state, action):
    state["tables"]["crystals"].append(deepcopy(state["tables"]["crystals"][0]))
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        action(state)
    assert caught.value.code == "invalid_crystal_state" and state == before


def test_paid_response_is_a_full_absolute_category_snapshot(state):
    updated = debit_crystals(state, 13)
    response = paid_crystal_response(updated, "17")
    assert response == Container("PPC", (Column("UserID", "I", (17,)),
        Column("Date", "I", (739920,)), Column("Num", "I", (6,))))
    assert loads(dumps(Container(children=(response,)))).children == (response,)
    updated["tables"]["crystals"] = [row for row in updated["tables"]["crystals"] if row["FreeFlag"]]
    assert paid_crystal_response(updated, 17) == Container("PPC", (
        Column("UserID", "I", ()), Column("Date", "I", ()), Column("Num", "I", ())))


@pytest.mark.parametrize("player_id", [True, 0, -1, INT32_MAX + 1, "-1", "not-an-id"])
def test_paid_response_reuses_the_verified_player_identity_contract(state, player_id):
    with pytest.raises(ProtocolError) as caught:
        paid_crystal_response(state, player_id)
    assert caught.value.code == "invalid_crystal_player_id"
