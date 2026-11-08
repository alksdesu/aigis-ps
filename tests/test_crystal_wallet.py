from copy import deepcopy

import pytest

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.crystal_wallet import (
    CRYSTAL_LIMITS, FREE_CRYSTAL_LIMIT, INT32_MAX, PAID_CRYSTAL_LIMIT, TOTAL_CRYSTAL_LIMIT, claim_crystal_space,
    credit_free_crystals, free_crystal_response, free_crystals_fit, paid_crystal_space, wallet_totals,
)
from aigis_ps.errors import ProtocolError


@pytest.fixture
def state():
    return {"bootstrap_schema": 1, "tables": {"crystals": [
        {"FreeFlag": False, "Num": 11, "Date": 739920},
        {"FreeFlag": True, "Num": 3, "Date": 739919},
        {"FreeFlag": True, "Num": 5, "Date": 739920, "metadata": {"source": "existing"}},
    ], "config": [{"MenuTutorialFlags": 7}]}, "player_state": {"units": [{"GetIndex": 9}]}}


def test_credit_updates_same_free_day_and_preserves_paid_and_other_state(state):
    before = deepcopy(state)
    updated = credit_free_crystals(state, 2, 739920)
    expected = deepcopy(before)
    expected["tables"]["crystals"][2]["Num"] = 7
    assert updated == expected
    assert wallet_totals(updated) == {"paid": 11, "free": 10, "total": 21}
    updated["tables"]["crystals"][2]["metadata"]["source"] = "changed"
    updated["tables"]["config"][0]["MenuTutorialFlags"] = 0
    updated["player_state"]["units"][0]["GetIndex"] = 1
    assert state == before


def test_credit_new_day_appends_without_reordering_or_coalescing(state):
    before = deepcopy(state)
    updated = credit_free_crystals(state, 4, 739921)
    assert updated["tables"]["crystals"] == before["tables"]["crystals"] + [
        {"FreeFlag": True, "Num": 4, "Date": 739921}]
    assert state == before


@pytest.mark.parametrize("player_id", [17, "17"])
def test_pfc_is_full_absolute_free_snapshot_and_round_trips(state, player_id):
    before = deepcopy(state)
    updated = credit_free_crystals(state, 2, 739920)
    response = free_crystal_response(updated, player_id)
    assert response == Container("PFC", (
        Column("UserID", "I", (17, 17)), Column("Date", "I", (739919, 739920)), Column("Num", "I", (3, 7))))
    assert loads(dumps(Container(children=(response,)))).children == (response,)
    assert free_crystal_response(state, player_id).children[2].values == (3, 5)
    totals = wallet_totals(state)
    totals["free"] = 0
    assert wallet_totals(state)["free"] == 8
    assert state == before


@pytest.mark.parametrize("rows", [[], [{"FreeFlag": False, "Num": 11, "Date": 739920}]])
def test_no_free_balance_still_returns_three_empty_pfc_columns(state, rows):
    state["tables"]["crystals"] = rows
    before = deepcopy(state)
    response = free_crystal_response(state, "1")
    assert response == Container("PFC", (Column("UserID", "I", ()), Column("Date", "I", ()), Column("Num", "I", ())))
    assert loads(dumps(Container(children=(response,)))).children == (response,)
    updated = credit_free_crystals(state, 3, 739920)
    assert updated["tables"]["crystals"] == rows + [{"FreeFlag": True, "Num": 3, "Date": 739920}]
    assert state == before


def test_legacy_day_zero_and_zero_counts_remain_readable_and_preserved(state):
    state["tables"]["crystals"] = [
        {"FreeFlag": True, "Num": 0, "Date": 0}, {"FreeFlag": False, "Num": 0, "Date": 0}]
    assert wallet_totals(state) == {"paid": 0, "free": 0, "total": 0}
    assert free_crystal_response(state, 1).children[1:] == (Column("Date", "I", (0,)), Column("Num", "I", (0,)))
    updated = credit_free_crystals(state, 1, 739920)
    assert updated["tables"]["crystals"][:2] == state["tables"]["crystals"]


@pytest.mark.parametrize("invalid", [None, [], {}, {"bootstrap_schema": True, "tables": {"crystals": []}},
    {"bootstrap_schema": 2, "tables": {"crystals": []}}, {"bootstrap_schema": 1, "tables": []},
    {"bootstrap_schema": 1, "tables": {}}, {"bootstrap_schema": 1, "tables": {"crystals": {}}}])
def test_invalid_schema_is_rejected_without_initializing_or_mutating(invalid):
    before = deepcopy(invalid)
    for action in (lambda: wallet_totals(invalid), lambda: free_crystal_response(invalid, 1),
                   lambda: credit_free_crystals(invalid, 1, 739920)):
        with pytest.raises(ProtocolError) as error:
            action()
        assert error.value.code == "invalid_crystal_state" and error.value.status == 503
        assert invalid == before


@pytest.mark.parametrize("row", [None, {}, {"FreeFlag": True, "Num": 0}, {"FreeFlag": True, "Date": 0}])
def test_invalid_row_shape_is_rejected(state, row):
    state["tables"]["crystals"] = [row]
    with pytest.raises(ProtocolError, match="Crystal") as error:
        free_crystal_response(state, 1)
    assert error.value.status == 503


@pytest.mark.parametrize("field, value", [
    ("FreeFlag", 0), ("FreeFlag", 1), ("FreeFlag", "True"), ("FreeFlag", None),
    *[(field, value) for field in ("Num", "Date") for value in (True, False, -1, INT32_MAX + 1, "1", 1.5, None)],
])
@pytest.mark.parametrize("free", [False, True])
def test_invalid_bucket_values_fail_even_when_not_selected_for_pfc(state, field, value, free):
    row = {"FreeFlag": free, "Num": 1, "Date": 739920, field: value}
    state["tables"]["crystals"] = [row]
    before = deepcopy(state)
    for action in (lambda: wallet_totals(state), lambda: free_crystal_response(state, 1),
                   lambda: credit_free_crystals(state, 1, 739920)):
        with pytest.raises(ProtocolError) as error:
            action()
        assert error.value.code == "invalid_crystal_state" and error.value.status == 503
        assert state == before


@pytest.mark.parametrize("free", [False, True])
@pytest.mark.parametrize("amount", [0, 3])
def test_duplicate_bucket_keys_are_rejected_including_zero_count(state, free, amount):
    state["tables"]["crystals"] = [{"FreeFlag": free, "Num": amount, "Date": 739920}] * 2
    before = deepcopy(state)
    for action in (lambda: wallet_totals(state), lambda: free_crystal_response(state, 1),
                   lambda: credit_free_crystals(state, 1, 739921)):
        with pytest.raises(ProtocolError, match="unique") as error:
            action()
        assert error.value.status == 503
        assert state == before


@pytest.mark.parametrize("field", ["amount", "acquisition_day"])
@pytest.mark.parametrize("value", [True, False, 0, -1, INT32_MAX + 1, "1", 1.5, None])
def test_invalid_credit_parameters_do_not_mutate(state, field, value):
    arguments = {"amount": 1, "acquisition_day": 739920, field: value}
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        credit_free_crystals(state, **arguments)
    assert error.value.code == "invalid_crystal_credit" and error.value.status == 400
    assert state == before


@pytest.mark.parametrize("player_id", [True, False, 0, -1, INT32_MAX + 1, None, 1.5, "", "-1", "+1", " 1", "1.0", "１", "1" * 100])
def test_invalid_response_identity_does_not_mutate(state, player_id):
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        free_crystal_response(state, player_id)
    assert error.value.code == "invalid_crystal_player_id" and error.value.status == 503
    assert state == before


def test_int32_total_bound_includes_paid_and_other_free_days(state):
    state["tables"]["crystals"] = [
        {"FreeFlag": False, "Num": INT32_MAX - 5, "Date": 739920},
        {"FreeFlag": True, "Num": 2, "Date": 739919}]
    updated = credit_free_crystals(state, 3, 739920)
    assert wallet_totals(updated)["total"] == INT32_MAX
    assert free_crystal_response(updated, INT32_MAX).children[0].values == (INT32_MAX, INT32_MAX)
    before = deepcopy(updated)
    for day in (739919, 739921):
        with pytest.raises(ProtocolError) as error:
            credit_free_crystals(updated, 1, day)
        assert error.value.code == "crystal_capacity_exceeded" and error.value.status == 400
        assert updated == before


def test_int32_maximum_single_credit_and_day_are_representable(state):
    state["tables"]["crystals"] = []
    updated = credit_free_crystals(state, INT32_MAX, INT32_MAX)
    assert wallet_totals(updated) == {"paid": 0, "free": INT32_MAX, "total": INT32_MAX}
    response = free_crystal_response(updated, str(INT32_MAX))
    assert loads(dumps(Container(children=(response,)))).children == (response,)


def test_persisted_total_overflow_is_rejected_by_all_entry_points(state):
    state["tables"]["crystals"] = [
        {"FreeFlag": False, "Num": INT32_MAX, "Date": 739920}, {"FreeFlag": True, "Num": 1, "Date": 739920}]
    before = deepcopy(state)
    for action in (lambda: wallet_totals(state), lambda: free_crystal_response(state, 1),
                   lambda: credit_free_crystals(state, 1, 739920)):
        with pytest.raises(ProtocolError) as error:
            action()
        assert error.value.code == "invalid_crystal_state" and error.value.status == 503
        assert state == before


def test_star_crystal_capacity_weighs_only_the_free_buckets(state):
    state["tables"]["crystals"][0]["Num"] = 5000
    free = wallet_totals(state)["free"]
    assert free_crystals_fit(state, FREE_CRYSTAL_LIMIT - free)
    assert not free_crystals_fit(state, FREE_CRYSTAL_LIMIT - free + 1)
    for amount in (-1, True, 2 ** 31):
        with pytest.raises(ProtocolError) as error:
            free_crystals_fit(state, amount)
        assert error.value.code == "invalid_crystal_credit"


@pytest.mark.parametrize("paid, free", [
    (0, 0), (PAID_CRYSTAL_LIMIT - 1, 0), (PAID_CRYSTAL_LIMIT - 10, FREE_CRYSTAL_LIMIT), (0, FREE_CRYSTAL_LIMIT + 1),
    (PAID_CRYSTAL_LIMIT, 0), (PAID_CRYSTAL_LIMIT, FREE_CRYSTAL_LIMIT), (PAID_CRYSTAL_LIMIT + 200, 0),
], ids=["empty", "paid_near_limit", "total_near_limit", "free_over_limit", "paid_full", "total_full",
        "paid_over_limit"])
def test_paid_crystal_space_mirrors_the_claim_caps_on_the_paid_side(state, paid, free):
    def wallet(paid_count, free_count):
        updated = deepcopy(state)
        updated["tables"]["crystals"] = [{"FreeFlag": False, "Num": paid_count, "Date": 739920},
                                         {"FreeFlag": True, "Num": free_count, "Date": 739920}]
        return updated

    total = paid + free
    expected = 0 if free > FREE_CRYSTAL_LIMIT else max(0, min(PAID_CRYSTAL_LIMIT - paid, TOTAL_CRYSTAL_LIMIT - total))
    assert paid_crystal_space(wallet(paid, free)) == claim_crystal_space(wallet(free, paid)) == expected


def test_crystal_limits_name_the_three_client_caps():
    assert CRYSTAL_LIMITS == {"paid": PAID_CRYSTAL_LIMIT, "free": FREE_CRYSTAL_LIMIT, "total": TOTAL_CRYSTAL_LIMIT}
