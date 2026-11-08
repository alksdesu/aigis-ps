from copy import deepcopy
from datetime import datetime, timedelta

import pytest

import aigis_ps.daily_gift_state as module
from aigis_ps.daily_gift_state import (LEDGER_KEY, begin_daily_gift, complete_daily_gift, daily_gift_clock,
    defer_daily_gift, initialize_daily_gifts, read_daily_gifts, validate_daily_gift_grants,
    validate_daily_gift_receipt)
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from test_login_reward_state import (BUSINESS_EDGES, CLIENT_CALENDAR, OUTSIDE_CALENDAR, epoch_milliseconds,
    representations, shifted_day)


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
HASH = "a" * 64


def state():
    return {"bootstrap_schema": 1, "tables": {"presents": []}, "unrelated": {"value": [1]}}


def pending(source=None, now=NOW):
    return begin_daily_gift(state() if source is None else source, 1, HASH, "2026-11-01", "2026-11-10", now)


def record(source, day="2026-11-01"):
    return source["daily_gift"]["gifts"]["1"][day]


def test_initialization_has_no_history_backfill_or_aliases():
    before = state()
    after = initialize_daily_gifts(before)
    assert after == before and "daily_gift" not in after
    after["unrelated"]["value"].append(2)
    assert before["unrelated"]["value"] == [1]


def test_the_journal_stays_under_the_key_existing_saves_use():
    source = pending()
    assert LEDGER_KEY == "daily_gift" and source[LEDGER_KEY] == read_daily_gifts(source)


@pytest.mark.parametrize("hour,minute,day", [(0, 0, "2026-10-31"), (3, 59, "2026-10-31"),
                                          (4, 0, "2026-11-01")])
def test_native_day_boundary(hour, minute, day):
    assert daily_gift_clock(NOW.replace(hour=hour, minute=minute))[0] == day


@pytest.mark.parametrize("moment", BUSINESS_EDGES + CLIENT_CALENDAR)
def test_clock_follows_the_shifted_jst_clock_in_every_representation(moment):
    for now in representations(moment):
        assert daily_gift_clock(now) == (shifted_day(now).isoformat(), epoch_milliseconds(now))


@pytest.mark.parametrize("moment", OUTSIDE_CALENDAR)
def test_instants_outside_the_client_calendar_are_refused(moment):
    for now in representations(moment):
        with pytest.raises(ProtocolError) as error:
            daily_gift_clock(now)
        assert error.value.code == "daily_gift_clock_invalid"
    source = pending()
    source[LEDGER_KEY]["last_time"] = epoch_milliseconds(moment)
    with pytest.raises(ProtocolError) as error:
        read_daily_gifts(source)
    assert error.value.code == "daily_gift_state_invalid"


def test_multiple_observed_days_keep_separate_pending_and_skip_absent_days():
    first = pending()
    second = pending(first, NOW + timedelta(days=3))
    assert set(second["daily_gift"]["gifts"]["1"]) == {"2026-11-01", "2026-11-04"}
    assert all(value["status"] == "pending" for value in second["daily_gift"]["gifts"]["1"].values())
    assert pending(second, NOW + timedelta(days=3)) == second


def test_same_day_changed_definition_cannot_replace_existing_eligibility():
    original = pending()
    assert begin_daily_gift(original, 1, "b" * 64, "2026-11-01", "2026-11-02", NOW) == original


def test_delivery_after_expiry_has_permanent_source_and_mail_proof():
    source = defer_daily_gift(pending(), 1, "2026-11-01", "present_capacity")
    before = deepcopy(source)
    delivered, grant = complete_daily_gift(source, 1, "2026-11-01", [3, 4], NOW + timedelta(days=30))
    assert source == before
    assert grant == {"gift_id": 1, "definition": HASH, "earned_day": "2026-11-01",
                     "delivery_day": "2026-12-01", "mail_ids": [3, 4]}
    validate_daily_gift_receipt(delivered, [grant])
    assert record(delivered)["reason"] is None
    with pytest.raises(ProtocolError):
        complete_daily_gift(delivered, 1, "2026-11-01", [5], NOW + timedelta(days=31))


@pytest.mark.parametrize("operation", ["begin", "complete"])
def test_backward_clock_rejected_without_changes(operation):
    source = pending()
    before = deepcopy(source)
    with pytest.raises(ProtocolError) as error:
        if operation == "begin":
            pending(source, NOW - timedelta(seconds=1))
        else:
            complete_daily_gift(source, 1, "2026-11-01", [1], NOW - timedelta(seconds=1))
    assert error.value.code == "daily_gift_clock_backwards" and source == before


@pytest.mark.parametrize("field,value", [("status", "gone"), ("definition", "x"), ("reason", []),
    ("mail_ids", [1]), ("delivery_day", "2026-11-01"), ("window_start", "2026-11-02"),
    ("window_end", "2026-10-31")])
def test_bad_pending_fields_are_not_silently_repaired(field, value):
    source = pending()
    record(source)[field] = value
    with pytest.raises(ProtocolError):
        initialize_daily_gifts(source)


def test_bool_timestamp_and_noncanonical_dates_are_rejected():
    source = pending()
    source["daily_gift"]["last_time"] = True
    with pytest.raises(ProtocolError):
        read_daily_gifts(source)
    source = pending()
    source["daily_gift"]["gifts"]["1"]["20261101"] = record(source)
    with pytest.raises(ProtocolError):
        read_daily_gifts(source)


def test_state_capacity_and_mail_identity_conflicts_are_rejected(monkeypatch):
    source = pending()
    monkeypatch.setattr(module, "MAX_ENTRIES", 1)
    with pytest.raises(ProtocolError) as error:
        pending(source, NOW + timedelta(days=1))
    assert error.value.code == "daily_gift_journal_capacity"
    monkeypatch.setattr(module, "MAX_ENTRIES", 2)
    source, _ = complete_daily_gift(source, 1, "2026-11-01", [1], NOW)
    source = pending(source, NOW + timedelta(days=1))
    with pytest.raises(ProtocolError):
        complete_daily_gift(source, 1, "2026-11-02", [1], NOW + timedelta(days=1))


def test_grants_require_sorted_unique_sources_and_exact_fields():
    source, first = complete_daily_gift(pending(), 1, "2026-11-01", [1], NOW)
    source, second = complete_daily_gift(pending(source, NOW + timedelta(days=1)), 1, "2026-11-02", [2], NOW + timedelta(days=1))
    assert validate_daily_gift_grants([first, second]) == [1, 2]
    for bad in ([second, first], [first, first], [{**first, "extra": 1}], [{**first, "gift_id": True}]):
        with pytest.raises(ProtocolError):
            validate_daily_gift_grants(bad)
    changed = {**first, "definition": "b" * 64}
    with pytest.raises(ProtocolError):
        validate_daily_gift_receipt(source, [changed])
