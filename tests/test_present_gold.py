from copy import deepcopy
from dataclasses import replace

import pytest

from aigis_ps.alxml import Column
from aigis_ps.errors import ProtocolError
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.wallet_money import GOLD_LIMIT
from test_presents import (DAY, asset_root, assets, children, claim_request, crystal, growth_setup,
                           presents, queue, rejected, state, store, unit)


def gold(amount):
    return {"kind": "gold", "amount": amount, "description": "Gold reward"}


def balance(state):
    return state["tables"]["game_param"][0]["Money"]


def set_balance(store, amount):
    profile = store.profile()
    changed = deepcopy(profile.state)
    changed["tables"]["game_param"][0]["Money"] = amount
    store.update_state(changed, profile.revision)


def test_gold_mail_retains_existing_schema_and_uses_native_type(presents, state):
    before = deepcopy(state)
    queued, rows = presents.enqueue(state, [gold(12345)])
    assert rows == [{"PresentID": 1, "Type": 2, "Content": 12345, "Description": "Gold reward"}]
    assert queued["presents_schema"] == 1 and balance(queued) == balance(before)
    assert presents.response(queued).children[1:3] == (Column("A1", "I", (2,)), Column("A2", "I", (12345,)))
    assert state == before


@pytest.mark.parametrize("amount", [True, 0, -1, 0x80000000, 1.5, "100"])
def test_invalid_gold_reward_does_not_allocate_mail(presents, state, amount):
    before = deepcopy(state)
    with pytest.raises(ProtocolError, match="positive int32"):
        presents.enqueue(state, [unit(), gold(amount)])
    assert state == before


@pytest.mark.parametrize("number", [1, 2, 117])
def test_gold_claim_updates_absolute_balance_and_exact_retry(presents, store, number):
    set_balance(store, GOLD_LIMIT - 10)
    identifiers = queue(presents, store, [gold(10)])
    envelope = claim_request(store, identifiers, number)
    before = store.profile()
    response = presents.receive(envelope, store)
    result = children(response)
    saved = store.profile()
    assert balance(saved.state) == GOLD_LIMIT and not presents.rows(saved.state)
    assert result["PGP"].children == (Column("A1", "S", ("A1",)), Column("A2", "S", (str(GOLD_LIMIT),)))
    assert set(result) == {"SYSTEM", "PGP", "PPU"}
    assert all(not column.values for column in result["PPU"].children)
    assert saved.state["player_state"] == before.state["player_state"]
    assert saved.state["tables"]["crystals"] == before.state["tables"]["crystals"]
    assert presents.receive(envelope, store) == response and store.profile() == saved


@pytest.mark.parametrize("money,amount", [(GOLD_LIMIT, 1), (GOLD_LIMIT - 1, 2), (GOLD_LIMIT + 1, 1), (1, 0x7FFFFFFF)])
def test_single_gold_overflow_preserves_all_mixed_rewards(presents, store, money, amount):
    set_balance(store, money)
    ids = queue(presents, store, [unit(), crystal(), gold(amount)])
    rejected(presents, store, claim_request(store, ids), "present_gold_capacity")


def test_partial_claim_skips_whole_gold_mail_and_continues_in_request_order(presents, store):
    set_balance(store, GOLD_LIMIT - 3)
    ids = queue(presents, store, [gold(3), gold(2), gold(1), crystal(2), unit()])
    response = children(presents.receive(claim_request(store, [ids[2], ids[1], ids[0], ids[3], ids[4]], 117), store))
    saved = store.profile().state
    assert balance(saved) == GOLD_LIMIT
    assert [row["PresentID"] for row in presents.rows(saved)] == [ids[0]]
    assert wallet_totals(saved)["free"] == 2
    assert len(saved["player_state"]["units"]) == 2
    assert {"PGP", "PFC", "PBP", "PPU"} <= response.keys()


def test_partial_claim_does_not_treat_gold_as_free_crystals(presents, store):
    set_balance(store, GOLD_LIMIT)
    ids = queue(presents, store, [gold(1), crystal(2)])
    response = children(presents.receive(claim_request(store, ids, 117), store))
    saved = store.profile().state
    assert balance(saved) == GOLD_LIMIT and wallet_totals(saved)["free"] == 2
    assert [row["PresentID"] for row in presents.rows(saved)] == [ids[0]]
    assert "PGP" not in response and "PFC" in response


@pytest.mark.parametrize("money", [-1, True, "100", 0x80000000])
def test_corrupt_gold_balance_is_not_repaired_by_reward(presents, store, money):
    ids = queue(presents, store, [gold(1)])
    set_balance(store, money)
    rejected(presents, store, claim_request(store, ids))


def test_partial_claim_recomputes_gold_capacity_after_concurrent_change(presents, store, monkeypatch):
    set_balance(store, GOLD_LIMIT - 5)
    ids = queue(presents, store, [gold(4), gold(1)])
    update = store.update_state
    attempted = []

    def concurrent(state, revision):
        if not attempted:
            attempted.append(True)
            profile = store.profile()
            changed = deepcopy(profile.state)
            changed["tables"]["game_param"][0]["Money"] = GOLD_LIMIT - 1
            update(changed, profile.revision)
            raise ValueError("Concurrent gold change")
        return update(state, revision)

    monkeypatch.setattr(store, "update_state", concurrent)
    presents.receive(claim_request(store, ids, 117), store)
    assert balance(store.profile().state) == GOLD_LIMIT
    assert [row["PresentID"] for row in presents.rows(store.profile().state)] == [ids[0]]


def test_gold_readback_capacity_is_checked_before_committing(presents, store):
    ids = queue(presents, store, [gold(1)])
    presents._game_param_profile = replace(presents._game_param_profile, response_limit=1)
    rejected(presents, store, claim_request(store, ids))


def test_gold_claim_preserves_growth_and_energy_state(presents, state):
    queued, mail = presents.enqueue(state, [gold(100)])
    before = deepcopy(queued)
    updated, units, crystals = presents.claim(queued, [mail[0]["PresentID"]], acquisition_day=DAY)
    assert balance(updated) == balance(before) + 100 and not units and crystals == 0
    old_row = before["tables"]["game_param"][0]
    new_row = updated["tables"]["game_param"][0]
    assert {key: value for key, value in new_row.items() if key != "Money"} == {key: value for key, value in old_row.items() if key != "Money"}
    assert queued == before
