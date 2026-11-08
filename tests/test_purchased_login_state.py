from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from aigis_ps.business_calendar import NATIVE_DATETIME
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.purchased_login_state import (DAILY_LIMIT, LEDGER_KEY, add_purchased_login, begin_purchased_reward,
    complete_purchased_reward, defer_purchased_reward, initialize_purchased_logins, purchase_window,
    purchased_login_clock, purchased_login_delivered_day, purchased_login_identity, purchased_login_settled_day,
    purchased_login_time, read_purchased_logins, rebind_purchased_reward, validate_purchased_login_grants,
    validate_purchased_login_receipt, void_purchased_reward)
from test_login_reward_state import (BUSINESS_EDGES, CLIENT_CALENDAR, OUTSIDE_CALENDAR, epoch_milliseconds,
    representations, shifted_day)


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
HASH = "a"*64
CURRENT = "b"*64


def state():
    return {"bootstrap_schema": 1, "tables": {}, "keep": [1]}


def entry(now=NOW):
    timestamp = purchased_login_clock(now)[1]
    start, end = purchase_window(timestamp, "2025-01-01", None, 30)
    return {"campaign_id": 129, "identity": purchased_login_identity(129), "sku": "local_login_129",
        "package_id": 2000100129, "flag_id": 2000100129, "request": HASH, "purchased_at": timestamp,
        "purchase_time": purchased_login_time(timestamp), "source_start": "2025-01-01", "source_end": None,
        "expiration": 30, "start_day": start, "end_day": end, "reward_definitions": [HASH, HASH],
        "deliveries": {}, "pending": None}


def purchased(now=NOW):
    return add_purchased_login(state(), entry(now), now)[0]


def delivered():
    next_day = NOW+timedelta(days=1)
    return complete_purchased_reward(begin_purchased_reward(purchased(), 1, next_day), 1, [1], next_day)


def reject(action, code="purchased_login_state_invalid"):
    with pytest.raises(ProtocolError) as error:
        action()
    assert error.value.code == code


def test_initialize_keeps_absent_domain_and_original_data():
    original = state()
    result = initialize_purchased_logins(original)
    result["keep"].append(2)
    assert original == state() and "purchased_login" not in result


def test_the_journal_stays_under_the_key_existing_saves_use():
    source = purchased()
    assert LEDGER_KEY == "purchased_login" and source[LEDGER_KEY] == read_purchased_logins(source)


@pytest.mark.parametrize("hour,day,end", [(0, "2026-11-01", "2026-12-01"), (3, "2026-11-01", "2026-12-01"),
    (4, "2026-11-02", "2026-12-02"), (12, "2026-11-02", "2026-12-02")])
def test_window_starts_next_logical_day(hour, day, end):
    timestamp = purchased_login_clock(NOW.replace(hour=hour))[1]
    assert purchase_window(timestamp, None, None, 30) == (day, end)


@pytest.mark.parametrize("moment", BUSINESS_EDGES + CLIENT_CALENDAR)
def test_clock_and_purchase_time_follow_the_jst_clock_in_every_representation(moment):
    for now in representations(moment):
        assert purchased_login_clock(now) == (shifted_day(now).isoformat(), epoch_milliseconds(now))
    assert purchased_login_time(epoch_milliseconds(moment)) == moment.strftime(NATIVE_DATETIME)


@pytest.mark.parametrize("moment", OUTSIDE_CALENDAR)
def test_instants_outside_the_client_calendar_are_refused(moment):
    for now in representations(moment):
        reject(lambda: purchased_login_clock(now), "purchased_login_clock_invalid")
    timestamp = epoch_milliseconds(moment)
    reject(lambda: purchased_login_time(timestamp))
    reject(lambda: purchase_window(timestamp, None, None, 30))
    source = purchased()
    source[LEDGER_KEY]["last_time"] = timestamp
    reject(lambda: read_purchased_logins(source))


@pytest.mark.parametrize("moment", OUTSIDE_CALENDAR)
def test_saved_purchase_instant_outside_the_client_calendar_is_refused(moment):
    source = purchased()
    source[LEDGER_KEY]["purchases"]["1"]["purchased_at"] = epoch_milliseconds(moment)
    before = deepcopy(source)
    for action in (read_purchased_logins, initialize_purchased_logins):
        with pytest.raises(ProtocolError, match="calendar") as error:
            action(source)
        assert (error.value.code, error.value.status) == ("purchased_login_state_invalid", 503)
    assert source == before


def test_source_clamps_window_without_moving_expiration_anchor():
    timestamp = purchased_login_clock(NOW)[1]
    assert purchase_window(timestamp, "2026-11-10", None, 30) == ("2026-11-10", "2026-12-02")
    assert purchase_window(timestamp, None, "2026-11-20", 30) == ("2026-11-02", "2026-11-20")


def test_purchase_identity_and_timestamp_are_stable_in_grant():
    result, grant = delivered()
    assert grant["purchase_id"] == 1 and grant["purchase_time"] == "2026/11/01 12:00:00"
    validate_purchased_login_receipt(result, [grant])
    reject(lambda: begin_purchased_reward(result, 1, NOW+timedelta(days=1)), DAILY_LIMIT)


def test_pending_can_finish_after_expiration_without_accruing_more_rewards():
    pending = begin_purchased_reward(purchased(), 1, NOW+timedelta(days=1))
    pending = defer_purchased_reward(pending, 1, "present_capacity")
    result, grant = complete_purchased_reward(pending, 1, [1], NOW+timedelta(days=40))
    assert grant["earned_day"] == "2026-11-02" and grant["delivery_day"] == "2026-12-11"
    reject(lambda: begin_purchased_reward(result, 1, NOW+timedelta(days=41)), "purchased_login_inactive")


@pytest.mark.parametrize("field,value", [("purchase_id", True), ("purchase_time", ""), ("purchase_time", "0"),
    ("login_count", 0), ("definition", "bad"), ("mail_ids", [1, 1]), ("mail_ids", [True]), ("earned_day", "2026-12-31")])
def test_bad_grant_fields_are_rejected(field, value):
    _, grant = delivered()
    grant[field] = value
    reject(lambda: validate_purchased_login_grants([grant]))


@pytest.mark.parametrize("mutation", [
    lambda s: s["purchased_login"].update(next_purchase_id=2147483647),
    lambda s: s["purchased_login"]["purchases"]["1"].update(identity="b"*64),
    lambda s: s["purchased_login"]["purchases"]["1"].update(purchase_time="2020/01/01 00:00:00"),
    lambda s: s["purchased_login"]["purchases"]["1"].update(end_day="2026-12-31"),
    lambda s: s["purchased_login"]["purchases"]["1"]["deliveries"].update({"3": {}}),
])
def test_malformed_history_is_not_repaired(mutation):
    source, _ = delivered()
    mutation(source)
    before = deepcopy(source)
    reject(lambda: initialize_purchased_logins(source))
    assert source == before


def test_clock_backwards_and_missing_qualification_cannot_claim():
    reject(lambda: begin_purchased_reward(state(), 1, NOW))
    reject(lambda: begin_purchased_reward(purchased(), 1, NOW-timedelta(seconds=1)), "purchased_login_clock_backwards")


def test_completed_mail_id_cannot_be_reused_on_later_day():
    result, _ = delivered()
    pending = begin_purchased_reward(result, 1, NOW+timedelta(days=2))
    reject(lambda: complete_purchased_reward(pending, 1, [1], NOW+timedelta(days=2)))


def test_overlapping_stored_qualifications_are_rejected():
    source = purchased()
    duplicate = entry(NOW + timedelta(days=1))
    duplicate["request"] = "b" * 64
    reject(lambda: add_purchased_login(source, duplicate, NOW + timedelta(days=1)))


def test_invalid_unicode_sku_is_a_state_error():
    source = purchased()
    source["purchased_login"]["purchases"]["1"]["sku"] = "bad\ud800"
    reject(lambda: read_purchased_logins(source))


def voided():
    next_day = NOW+timedelta(days=1)
    return void_purchased_reward(begin_purchased_reward(purchased(), 1, next_day), 1)


def test_voiding_uses_up_the_ordinal_without_counting_as_a_delivery():
    source = voided()
    record = read_purchased_logins(source)["purchases"]["1"]
    assert record["pending"] is None and record["deliveries"] == {"1": {
        "definition": HASH, "earned_day": "2026-11-02", "delivery_day": "2026-11-02", "mail_ids": []}}
    assert initialize_purchased_logins(source) == source
    assert not purchased_login_delivered_day(read_purchased_logins(source), 129, "2026-11-02")
    assert purchased_login_settled_day(record, "2026-11-02") and not purchased_login_settled_day(record, "2026-11-03")
    reject(lambda: begin_purchased_reward(source, 1, NOW+timedelta(days=1)), DAILY_LIMIT)
    following = begin_purchased_reward(source, 1, NOW+timedelta(days=2))
    assert following["purchased_login"]["purchases"]["1"]["pending"]["ordinal"] == 2
    reject(lambda: void_purchased_reward(source, 1))
    reject(lambda: void_purchased_reward(purchased(), 1))


@pytest.mark.parametrize("change", [{"earned_day": "2026-11-01"}, {"earned_day": "2026-11-31"}, {"definition": CURRENT}])
def test_a_voided_ordinal_keeps_its_frozen_definition_and_earned_day(change):
    source = voided()
    source["purchased_login"]["purchases"]["1"]["deliveries"]["1"].update(change)
    reject(lambda: read_purchased_logins(source))


def test_rebinding_moves_only_the_schedule_still_to_come():
    source = begin_purchased_reward(delivered()[0], 1, NOW+timedelta(days=2))
    rebound = rebind_purchased_reward(source, 1, [HASH, CURRENT])
    record = read_purchased_logins(rebound)["purchases"]["1"]
    assert record["reward_definitions"] == [HASH, CURRENT]
    assert record["pending"] == {"ordinal": 2, "definition": CURRENT, "earned_day": "2026-11-03", "reason": None}
    for schedule in ([CURRENT, CURRENT], [HASH], [HASH, "short"], (HASH, CURRENT)):
        reject(lambda: rebind_purchased_reward(source, 1, schedule))
    reject(lambda: rebind_purchased_reward(delivered()[0], 1, [HASH, CURRENT]))
