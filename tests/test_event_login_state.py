from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.event_login_state import (DAILY_LIMIT, LEDGER_KEY, begin_event_login, complete_event_login,
    defer_event_login, event_login_clock, initialize_event_logins, read_event_logins, rebind_event_login,
    validate_event_login_grants, validate_event_login_receipt, void_event_login, voided_delivery)
from test_login_reward_state import (BUSINESS_EDGES, CLIENT_CALENDAR, OUTSIDE_CALENDAR, epoch_milliseconds,
    representations, shifted_day)


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
DEFINITION = "a" * 64
CURRENT = "b" * 64


def state():
    return {"bootstrap_schema": 1, "tables": {}, "unrelated": [1, 2]}


def pending():
    return begin_event_login(state(), 135, 1, DEFINITION, NOW)


def delivered():
    return complete_event_login(pending(), 135, [10, 11], NOW)


def reject(callback, code="event_login_state_invalid"):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert caught.value.code == code


def test_absent_journal_remains_absent_and_independent():
    source = state()
    result = initialize_event_logins(source)
    result["unrelated"].append(3)
    assert source["unrelated"] == [1, 2]
    assert "event_login" not in result


def test_the_journal_stays_under_the_key_existing_saves_use():
    saved, _ = delivered()
    assert LEDGER_KEY == "event_login" and saved[LEDGER_KEY] == read_event_logins(saved)


def test_jst_four_am_boundary():
    midnight = NOW.replace(hour=4, minute=0, second=0)
    assert event_login_clock(midnight - timedelta(microseconds=1))[0] == "2026-10-31"
    assert event_login_clock(midnight)[0] == "2026-11-01"


@pytest.mark.parametrize("moment", BUSINESS_EDGES + CLIENT_CALENDAR)
def test_clock_follows_the_shifted_jst_clock_in_every_representation(moment):
    for now in representations(moment):
        assert event_login_clock(now) == (shifted_day(now).isoformat(), epoch_milliseconds(now))


@pytest.mark.parametrize("moment", OUTSIDE_CALENDAR)
def test_instants_outside_the_client_calendar_are_refused(moment):
    for now in representations(moment):
        reject(lambda: event_login_clock(now), "event_login_clock_invalid")
    saved, _ = delivered()
    saved[LEDGER_KEY]["last_time"] = epoch_milliseconds(moment)
    reject(lambda: read_event_logins(saved))


def test_pending_retry_preserves_original_earned_day():
    source = pending()
    postponed = defer_event_login(source, 135, "present_capacity")
    assert postponed["event_login"]["campaigns"]["135"]["pending"]["reason"] == "present_capacity"
    retried = begin_event_login(postponed, 135, 1, DEFINITION, NOW + timedelta(days=5))
    assert retried == postponed
    saved, grant = complete_event_login(retried, 135, [20], NOW + timedelta(days=5))
    assert grant["earned_day"] == "2026-11-01" and grant["delivery_day"] == "2026-11-06"
    assert source["event_login"]["campaigns"]["135"]["pending"]["reason"] is None
    validate_event_login_receipt(saved, [grant])


def test_delivered_day_consumes_daily_limit_and_skipped_days_do_not_create_ordinals():
    saved, _ = complete_event_login(pending(), 135, [10], NOW + timedelta(days=5))
    reject(lambda: begin_event_login(saved, 135, 2, DEFINITION, NOW + timedelta(days=5)), DAILY_LIMIT)
    advanced = begin_event_login(saved, 135, 2, DEFINITION, NOW + timedelta(days=8))
    assert advanced["event_login"]["campaigns"]["135"]["pending"]["ordinal"] == 2


@pytest.mark.parametrize("operation", ["begin", "complete"])
def test_backward_clock_cannot_modify_progress(operation):
    source = pending()
    before = deepcopy(source)
    callback = (lambda: begin_event_login(source, 135, 1, DEFINITION, NOW - timedelta(seconds=1))) if operation == "begin" else (
        lambda: complete_event_login(source, 135, [1], NOW - timedelta(seconds=1)))
    reject(callback, "event_login_clock_backwards")
    assert source == before


@pytest.mark.parametrize("field,value", [("campaign_id", True), ("campaign_id", 0), ("login_count", 0),
    ("purchase_time", "2026-11-01"), ("definition", "short"), ("earned_day", "2026-11-02"),
    ("delivery_day", "2026-02-30"), ("mail_ids", []), ("mail_ids", [1, 1]), ("mail_ids", [True])])
def test_invalid_grants_are_rejected(field, value):
    _, grant = delivered()
    grant[field] = value
    reject(lambda: validate_event_login_grants([grant]))


def test_grant_order_and_cross_campaign_mail_reuse_are_rejected():
    _, first = delivered()
    second = {**first, "campaign_id": 136, "mail_ids": [12]}
    assert validate_event_login_grants([first, second]) == [10, 11, 12]
    reject(lambda: validate_event_login_grants([second, first]))
    second["mail_ids"] = [10]
    reject(lambda: validate_event_login_grants([first, second]))


@pytest.mark.parametrize("mutation", [
    lambda x: x["event_login"].update(schema_version=True),
    lambda x: x["event_login"].update(last_time=None),
    lambda x: x["event_login"]["campaigns"].update({"0135": x["event_login"]["campaigns"].pop("135")}),
    lambda x: x["event_login"]["campaigns"]["135"].update(pending={}),
    lambda x: x["event_login"]["campaigns"]["135"]["deliveries"].update({"2": x["event_login"]["campaigns"]["135"]["deliveries"].pop("1")}),
])
def test_invalid_saved_state_is_not_repaired(mutation):
    saved, _ = delivered()
    mutation(saved)
    before = deepcopy(saved)
    reject(lambda: initialize_event_logins(saved))
    assert saved == before


def test_receipt_checks_permanent_record_without_requiring_mailbox():
    saved, grant = delivered()
    validate_event_login_receipt(saved, [grant])
    grant["mail_ids"] = [999]
    reject(lambda: validate_event_login_receipt(saved, [grant]))


def test_retired_campaign_history_is_preserved():
    saved, grant = complete_event_login(begin_event_login(state(), 2000, 1, DEFINITION, NOW), 2000, [1], NOW)
    assert initialize_event_logins(saved) == saved
    validate_event_login_receipt(saved, [grant])


def test_missing_or_conflicting_pending_cannot_complete_or_skip():
    reject(lambda: complete_event_login(state(), 135, [1], NOW))
    reject(lambda: begin_event_login(state(), 135, 2, DEFINITION, NOW))
    reject(lambda: begin_event_login(pending(), 135, 1, "b" * 64, NOW))
    reject(lambda: defer_event_login(state(), 135, "present_capacity"))


def voided():
    return void_event_login(pending(), 135)


def test_voiding_uses_up_the_pending_count_without_mail():
    source = voided()
    record = read_event_logins(source)["campaigns"]["135"]
    assert record == {"pending": None, "deliveries": {"1": {"definition": DEFINITION, "earned_day": "2026-11-01",
                                                            "delivery_day": "2026-11-01", "mail_ids": []}}}
    assert voided_delivery(record["deliveries"]["1"]) and initialize_event_logins(source) == source
    reject(lambda: begin_event_login(source, 135, 2, DEFINITION, NOW), DAILY_LIMIT)
    following = begin_event_login(source, 135, 2, DEFINITION, NOW + timedelta(days=1))
    assert following["event_login"]["campaigns"]["135"]["pending"]["ordinal"] == 2
    grant = {"campaign_id": 135, "login_count": 1, "purchase_time": "", **record["deliveries"]["1"]}
    reject(lambda: validate_event_login_receipt(source, [grant]))
    reject(lambda: void_event_login(source, 135))
    reject(lambda: void_event_login(pending(), True))


@pytest.mark.parametrize("change", [{"earned_day": "2026-10-31"}, {"definition": "short"}, {"mail_ids": {}}])
def test_a_voided_count_keeps_its_definition_and_earned_day(change):
    source = voided()
    source["event_login"]["campaigns"]["135"]["deliveries"]["1"].update(change)
    reject(lambda: read_event_logins(source))


def test_rebinding_replaces_only_the_pending_definition():
    postponed = defer_event_login(pending(), 135, "event_login_definition_changed")
    rebound = rebind_event_login(postponed, 135, CURRENT)
    assert read_event_logins(rebound)["campaigns"]["135"] == {"deliveries": {}, "pending": {
        "ordinal": 1, "definition": CURRENT, "earned_day": "2026-11-01", "reason": None}}
    reject(lambda: rebind_event_login(postponed, 135, "short"))
    reject(lambda: rebind_event_login(postponed, 0, CURRENT))
    reject(lambda: rebind_event_login(state(), 135, CURRENT))
