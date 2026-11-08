import ast
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import aigis_ps.login_stamp_pending as module
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.login_reward_state import advance_login_rewards, initialize_login_rewards
from aigis_ps.login_stamp_pending import PENDING_KEY, hold_stamp, read_pending_stamps, rebind_stamp, release_stamp


START = datetime(2027, 1, 30, 12, tzinfo=JST)
DEFINITION = "a" * 64
CURRENT = "b" * 64


def claimed(days):
    state = {"bootstrap_schema": 1, "tables": {"game_param2": [{"LastLoginDate": 0, "LogInStampCount": 0}]}}
    state = initialize_login_rewards(state, START, new_account=True)
    for offset in range(days):
        state, _ = advance_login_rewards(state, START + timedelta(days=offset))
    return state


def entry(day, ordinal, reason="login_reward_month_unavailable", grade=0, definition=None):
    return {"day": day, "grade": grade, "month": int(day[5:7]), "ordinal": ordinal, "definition": definition,
            "reason": reason}


def hold(state, value):
    return hold_stamp(state, **value)


def error_of(action):
    with pytest.raises(ProtocolError) as caught:
        action()
    return caught.value.code


def test_missing_ledger_reads_as_nothing_waiting():
    assert read_pending_stamps(claimed(1)) == []


def test_holds_follow_claim_order_and_release_removes_only_that_day():
    state = claimed(3)
    state = hold(state, entry("2027-01-30", 1))
    state = hold(state, entry("2027-02-01", 1, "summon_ticket_pool_disabled", 4, DEFINITION))
    assert read_pending_stamps(state) == [entry("2027-01-30", 1),
                                          entry("2027-02-01", 1, "summon_ticket_pool_disabled", 4, DEFINITION)]
    state, released = release_stamp(state, "2027-01-30")
    assert released == entry("2027-01-30", 1)
    state, _ = release_stamp(state, "2027-02-01")
    assert PENDING_KEY not in state


def test_holding_the_same_day_again_only_refreshes_its_reason():
    state = hold(claimed(1), entry("2027-01-30", 1))
    state = hold(state, entry("2027-01-30", 1, "present_capacity"))
    assert read_pending_stamps(state) == [entry("2027-01-30", 1, "present_capacity")]
    for change in ({"grade": 1}, {"definition": DEFINITION}):
        assert error_of(lambda: hold(state, {**entry("2027-01-30", 1), **change})) == "login_stamp_pending_invalid"


def test_holds_cannot_skip_ahead_of_claims_or_back_before_held_days():
    state = claimed(2)
    assert error_of(lambda: hold(state, entry("2027-02-01", 1))) == "login_stamp_pending_invalid"
    assert error_of(lambda: hold(state, entry("2027-01-31", 1))) == "login_stamp_pending_invalid"
    later = hold(state, entry("2027-01-31", 2))
    assert error_of(lambda: hold(later, entry("2027-01-30", 1))) == "login_stamp_pending_invalid"
    assert error_of(lambda: release_stamp(later, "2027-01-30")) == "login_stamp_pending_invalid"


@pytest.mark.parametrize("change", [{"day": "2027-02-30"}, {"day": "20270130"}, {"grade": -1}, {"grade": True},
                                    {"month": 2}, {"ordinal": 0}, {"ordinal": 31}, {"definition": "A" * 64},
                                    {"reason": ""}, {"reason": "Upper"}])
def test_malformed_entries_are_rejected(change):
    value = {**entry("2027-01-30", 1), **change}
    assert error_of(lambda: hold(claimed(1), value)) == "login_stamp_pending_invalid"


@pytest.mark.parametrize("mutate", [
    lambda ledger: ledger.update(schema_version=2),
    lambda ledger: ledger.update(entries=[]),
    lambda ledger: ledger.update(extra=True),
    lambda ledger: ledger["entries"].append(deepcopy(ledger["entries"][0])),
    lambda ledger: ledger["entries"][0].update(ordinal=2),
    lambda ledger: ledger["entries"][0].update(extra=1),
])
def test_stored_ledgers_are_checked_against_claims(mutate):
    state = hold(claimed(1), entry("2027-01-30", 1))
    mutate(state[PENDING_KEY])
    assert error_of(lambda: read_pending_stamps(state)) == "login_stamp_pending_invalid"


def test_pending_stamps_need_a_login_ledger():
    state = hold(claimed(1), entry("2027-01-30", 1))
    del state["login_rewards"]
    assert error_of(lambda: read_pending_stamps(state)) == "login_rewards_required"


def test_capacity_is_reported_instead_of_dropping_a_stamp(monkeypatch):
    monkeypatch.setattr(module, "MAX_PENDING_STAMPS", 1)
    state = hold(claimed(2), entry("2027-01-30", 1))
    assert error_of(lambda: hold(state, entry("2027-01-31", 2))) == module.CAPACITY_EXCEEDED
    assert module.CAPACITY_EXCEEDED == "login_stamp_pending_capacity"


def test_the_capacity_code_is_spelled_once_where_it_is_published():
    # Identifier-like literals are interned, so an identity check cannot tell a copy from the import.
    published = Path(module.__file__).resolve()
    spelled = [path.name for path in sorted(published.parent.glob("*.py"))
               for node in ast.walk(ast.parse(path.read_bytes()))
               if isinstance(node, ast.Constant) and node.value == module.CAPACITY_EXCEEDED]
    assert spelled == [published.name]


def test_rebinding_adopts_a_resolved_definition_for_the_same_claim_day():
    state = hold(claimed(2), entry("2027-01-30", 1, "login_reward_definition_changed", definition=DEFINITION))
    state = hold(state, entry("2027-01-31", 2))
    rebound = rebind_stamp(state, "2027-01-30", CURRENT, "present_capacity")
    assert read_pending_stamps(rebound) == [entry("2027-01-30", 1, "present_capacity", definition=CURRENT),
                                            entry("2027-01-31", 2)]
    for day, definition, reason in (("2027-02-01", CURRENT, "present_capacity"),
                                    ("2027-01-30", None, "present_capacity"),
                                    ("2027-01-30", "B" * 64, "present_capacity"), ("2027-01-30", CURRENT, "")):
        assert error_of(lambda: rebind_stamp(state, day, definition, reason)) == "login_stamp_pending_invalid"
    assert read_pending_stamps(state)[0]["definition"] == DEFINITION
