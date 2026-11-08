from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from aigis_ps.business_calendar import NATIVE_DATETIME
from aigis_ps.energy_clock import JST, instant
from aigis_ps.errors import ProtocolError
from aigis_ps.comeback_login_state import (DAILY_LIMIT, DATE_FIELD, DAYS_FIELD, LEDGER_KEY, begin_comeback_reward,
    begin_comeback_round, comeback_clock, complete_comeback_reward, defer_comeback_reward, delivered_comeback_day,
    initialize_comeback_logins, observe_comeback_login, read_comeback_logins, rebind_comeback_reward,
    settled_comeback_day, validate_comeback_login_grants, validate_comeback_login_receipt, void_comeback_reward)
from test_login_reward_state import (BUSINESS_EDGES, CLIENT_CALENDAR, OUTSIDE_CALENDAR, epoch_milliseconds,
    representations, shifted_day)


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
HASH = "a" * 64
CURRENT = "b" * 64


def state():
    return {"bootstrap_schema": 1, "tables": {"game_param2": [{DATE_FIELD: "", DAYS_FIELD: 0,
        "ComebackLoginDateTime": "2025/01/01 00:00:00", "LatestComebackSummonBonusDays": 7}]}, "sentinel": [1]}


def started():
    observed, gap = observe_comeback_login(state(), NOW - timedelta(days=30))
    observed, gap = observe_comeback_login(observed, NOW)
    assert gap == 30
    return begin_comeback_round(observed, NOW, campaign_id=6, pause_period=30, absence_days=gap,
                                expiration=20, reward_definitions=[HASH, HASH])


def delivered():
    return complete_comeback_reward(begin_comeback_reward(started(), 1, NOW), 1, [1], NOW)


def reject(action, code="comeback_login_state_invalid"):
    with pytest.raises(ProtocolError) as error:
        action()
    assert error.value.code == code


def test_initialization_does_not_create_baseline_or_overwrite_old_fields():
    before = state()
    before["tables"]["game_param2"][0].update({DATE_FIELD: "2025/03/01 12:00:00", DAYS_FIELD: 60})
    assert initialize_comeback_logins(before) == before
    observed, gap = observe_comeback_login(before, NOW)
    assert gap is None and observed["tables"] == before["tables"]
    assert observed["comeback_login"]["rounds"] == {}
    assert observed["comeback_login"]["legacy_projection"] == {"date_time": "2025/03/01 12:00:00", "days": 60}


def test_round_updates_only_two_native_fields_and_freezes_window():
    result = started()
    record = read_comeback_logins(result)["rounds"]["1"]
    assert record["start_day"] == "2026-11-01" and record["end_day"] == "2026-11-21"
    row = result["tables"]["game_param2"][0]
    assert row[DATE_FIELD] == "2026/11/01 12:00:00" and row[DAYS_FIELD] == 30
    assert row["ComebackLoginDateTime"] == "2025/01/01 00:00:00"
    assert row["LatestComebackSummonBonusDays"] == 7


def test_the_journal_stays_under_the_key_existing_saves_use():
    result = started()
    assert LEDGER_KEY == "comeback_login" and result[LEDGER_KEY] == read_comeback_logins(result)


def test_day_gap_is_based_on_four_am_boundary():
    before = NOW.replace(hour=3, minute=59, second=59)
    observed, _ = observe_comeback_login(state(), before)
    _, gap = observe_comeback_login(observed, before + timedelta(seconds=1))
    assert gap == 1
    assert comeback_clock(before)[0] == "2026-10-31"


@pytest.mark.parametrize("moment", BUSINESS_EDGES + CLIENT_CALENDAR)
def test_clock_follows_the_shifted_jst_clock_in_every_representation(moment):
    for now in representations(moment):
        assert comeback_clock(now) == (shifted_day(now).isoformat(), epoch_milliseconds(now))


@pytest.mark.parametrize("moment", BUSINESS_EDGES)
def test_round_starts_on_the_business_day_and_ends_after_the_civil_return_date(moment):
    for now in representations(moment):
        observed, gap = observe_comeback_login(observe_comeback_login(state(), now - timedelta(days=30))[0], now)
        result = begin_comeback_round(observed, now, campaign_id=6, pause_period=30, absence_days=gap,
                                      expiration=20, reward_definitions=[HASH])
        record = result[LEDGER_KEY]["rounds"]["1"]
        civil = instant(now).astimezone(JST).date()
        assert (record["start_day"], record["end_day"]) == (shifted_day(now).isoformat(),
                                                            (civil + timedelta(days=20)).isoformat())
        assert result["tables"]["game_param2"][0][DATE_FIELD] == moment.strftime(NATIVE_DATETIME)


@pytest.mark.parametrize("moment", OUTSIDE_CALENDAR)
def test_instants_outside_the_client_calendar_are_refused(moment):
    for now in representations(moment):
        reject(lambda: comeback_clock(now), "comeback_login_clock_invalid")
        reject(lambda: observe_comeback_login(state(), now), "comeback_login_clock_invalid")
    source, _ = delivered()
    source[LEDGER_KEY]["last_time"] = epoch_milliseconds(moment)
    reject(lambda: read_comeback_logins(source))


@pytest.mark.parametrize("field", ["last_seen_at", "started_at"])
@pytest.mark.parametrize("moment", OUTSIDE_CALENDAR)
def test_saved_observation_and_round_start_outside_the_client_calendar_are_refused(moment, field):
    source, _ = delivered()
    journal = source[LEDGER_KEY]
    (journal if field in journal else journal["rounds"]["1"])[field] = epoch_milliseconds(moment)
    before = deepcopy(source)
    for action in (read_comeback_logins, initialize_comeback_logins):
        with pytest.raises(ProtocolError, match="calendar") as error:
            action(source)
        assert (error.value.code, error.value.status) == ("comeback_login_state_invalid", 503)
    assert source == before


def test_same_day_observation_records_nothing_until_the_next_logical_day():
    observed, _ = observe_comeback_login(state(), NOW)
    again, gap = observe_comeback_login(observed, NOW + timedelta(hours=10))
    assert gap == 0 and again == observed and again is not observed
    again["sentinel"].append(2)
    assert observed["sentinel"] == [1]
    following, gap = observe_comeback_login(observed, NOW + timedelta(hours=16))
    assert gap == 1
    assert following["comeback_login"]["last_seen_day"] == "2026-11-02"
    assert following["comeback_login"]["last_time"] == comeback_clock(NOW + timedelta(hours=16))[1]


@pytest.mark.parametrize("operation", ["observe", "begin", "complete"])
def test_clock_backwards_cannot_change_state(operation):
    source = begin_comeback_reward(started(), 1, NOW)
    before = deepcopy(source)
    previous = NOW - timedelta(seconds=1)
    action = {"observe": lambda: observe_comeback_login(source, previous),
              "begin": lambda: begin_comeback_reward(source, 1, previous),
              "complete": lambda: complete_comeback_reward(source, 1, [1], previous)}[operation]
    reject(action, "comeback_login_clock_backwards")
    assert source == before


def test_active_round_and_reward_repetition_are_rejected():
    result, _ = delivered()
    reject(lambda: begin_comeback_reward(result, 1, NOW), DAILY_LIMIT)
    observed, _ = observe_comeback_login(result, NOW + timedelta(days=1))
    reject(lambda: begin_comeback_round(observed, NOW + timedelta(days=1), campaign_id=6, pause_period=0,
        absence_days=1, expiration=20, reward_definitions=[HASH]), "comeback_login_round_active")


def test_old_pending_survives_new_round_and_uses_global_daily_limit():
    pending = defer_comeback_reward(begin_comeback_reward(started(), 1, NOW), 1, "present_capacity")
    observed, gap = observe_comeback_login(pending, NOW + timedelta(days=30))
    second = begin_comeback_round(observed, NOW + timedelta(days=30), campaign_id=6, pause_period=30,
        absence_days=gap, expiration=20, reward_definitions=[HASH])
    result, old = complete_comeback_reward(second, 1, [1], NOW + timedelta(days=30))
    assert old["round_id"] == 1 and old["notify"] is False
    assert result["tables"]["game_param2"][0][DATE_FIELD] == "2026/12/01 12:00:00"
    reject(lambda: begin_comeback_reward(result, 2, NOW + timedelta(days=30)), DAILY_LIMIT)
    next_day = begin_comeback_reward(result, 2, NOW + timedelta(days=31))
    result, fresh = complete_comeback_reward(next_day, 2, [2], NOW + timedelta(days=31))
    assert fresh["notify"] is True and fresh["round_id"] == 2
    validate_comeback_login_receipt(result, [fresh])
    validate_comeback_login_receipt(result, [old])


def test_only_earned_pending_may_be_delivered_after_expiration():
    pending = begin_comeback_reward(started(), 1, NOW)
    result, grant = complete_comeback_reward(pending, 1, [1], NOW + timedelta(days=25))
    assert grant["earned_day"] == "2026-11-01" and grant["delivery_day"] == "2026-11-26"
    reject(lambda: begin_comeback_reward(result, 1, NOW + timedelta(days=26)), "comeback_login_round_inactive")


@pytest.mark.parametrize("field,value", [("round_id", True), ("campaign_id", 0), ("notify", 1),
    ("login_count", 0), ("purchase_time", "2026/11/01"), ("definition", "x"),
    ("mail_ids", []), ("mail_ids", [1, 1]), ("mail_ids", [True]), ("earned_day", "2026-11-02")])
def test_bad_grants_are_rejected(field, value):
    _, grant = delivered()
    grant[field] = value
    reject(lambda: validate_comeback_login_grants([grant]))


def test_two_rounds_of_one_campaign_cannot_notify_in_one_batch():
    _, first = delivered()
    second = {**first, "round_id": 2, "mail_ids": [2]}
    reject(lambda: validate_comeback_login_grants([first, second]))


@pytest.mark.parametrize("mutation", [
    lambda s: s["comeback_login"].update(next_round_id=2147483647),
    lambda s: s["comeback_login"].update(current_round_id=2),
    lambda s: s["comeback_login"]["rounds"]["1"].update(absence_days=True),
    lambda s: s["comeback_login"]["rounds"]["1"].update(started_at="bad"),
    lambda s: s["comeback_login"]["rounds"]["1"]["deliveries"].update({"2": {}}),
    lambda s: s["tables"]["game_param2"][0].update({DAYS_FIELD: 90}),
])
def test_invalid_journal_is_not_repaired(mutation):
    source, _ = delivered()
    mutation(source)
    before = deepcopy(source)
    reject(lambda: initialize_comeback_logins(source))
    assert source == before


def test_mail_identity_cannot_be_reused_on_later_day():
    source, _ = delivered()
    pending = begin_comeback_reward(source, 1, NOW + timedelta(days=1))
    reject(lambda: complete_comeback_reward(pending, 1, [1], NOW + timedelta(days=1)))


def test_voiding_uses_up_the_ordinal_without_counting_as_a_delivery():
    voided = void_comeback_reward(begin_comeback_reward(started(), 1, NOW), 1)
    record = read_comeback_logins(voided)["rounds"]["1"]
    assert record["pending"] is None and record["deliveries"] == {"1": {
        "definition": HASH, "earned_day": "2026-11-01", "delivery_day": "2026-11-01", "mail_ids": [], "notify": False}}
    assert initialize_comeback_logins(voided) == voided
    assert not delivered_comeback_day(read_comeback_logins(voided), 6, "2026-11-01")
    assert settled_comeback_day(record, "2026-11-01") and not settled_comeback_day(record, "2026-11-02")
    reject(lambda: begin_comeback_reward(voided, 1, NOW), DAILY_LIMIT)
    following = begin_comeback_reward(voided, 1, NOW + timedelta(days=1))
    assert following["comeback_login"]["rounds"]["1"]["pending"]["ordinal"] == 2
    reject(lambda: void_comeback_reward(voided, 1))
    reject(lambda: void_comeback_reward(state(), 1))


def test_a_voided_ordinal_may_share_its_day_with_a_late_delivery_of_an_earlier_round():
    later = NOW + timedelta(days=30)
    observed, gap = observe_comeback_login(begin_comeback_reward(started(), 1, NOW), later)
    second = begin_comeback_round(observed, later, campaign_id=6, pause_period=30, absence_days=gap,
                                  expiration=20, reward_definitions=[HASH])
    late, _ = complete_comeback_reward(begin_comeback_reward(second, 2, later), 1, [1], later)
    voided = void_comeback_reward(late, 2)
    assert read_comeback_logins(voided)["rounds"]["2"]["deliveries"]["1"]["delivery_day"] == "2026-12-01"
    assert delivered_comeback_day(read_comeback_logins(voided), 6, "2026-12-01")


@pytest.mark.parametrize("change", [{"notify": True}, {"earned_day": "2026-10-31"}, {"definition": CURRENT}])
def test_a_voided_ordinal_keeps_its_frozen_definition_and_earned_day(change):
    voided = void_comeback_reward(begin_comeback_reward(started(), 1, NOW), 1)
    voided["comeback_login"]["rounds"]["1"]["deliveries"]["1"].update(change)
    reject(lambda: read_comeback_logins(voided))


def test_rebinding_moves_only_the_schedule_still_to_come():
    source = begin_comeback_reward(delivered()[0], 1, NOW + timedelta(days=1))
    rebound = rebind_comeback_reward(source, 1, [HASH, CURRENT])
    record = read_comeback_logins(rebound)["rounds"]["1"]
    assert record["reward_definitions"] == [HASH, CURRENT]
    assert record["pending"] == {"ordinal": 2, "definition": CURRENT, "earned_day": "2026-11-02", "reason": None}
    for schedule in ([CURRENT, CURRENT], [HASH], [HASH, "short"], (HASH, CURRENT)):
        reject(lambda: rebind_comeback_reward(source, 1, schedule))
    reject(lambda: rebind_comeback_reward(delivered()[0], 1, [HASH, CURRENT]))
