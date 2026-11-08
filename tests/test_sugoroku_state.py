from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

import aigis_ps.sugoroku_state as module
from aigis_ps.crystal_spending import initialize_spending, settle_spending, spend_crystals
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.sugoroku_state import (
    BASELINE,
    EARNED,
    MAX_PERIODS,
    MAX_RECORDS,
    initialize_sugoroku,
    mark_sugoroku,
    read_sugoroku,
    snapshot_sugoroku,
    sync_sugoroku,
)
from test_crystal_spending_ledger import MONTH_TURNS, OUTSIDE_CLIENT_CALENDAR, representations


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


@dataclass(frozen=True)
class Reward:
    reward_id: int
    consume_crystal: int
    unsupported_reason: str | None = None


class Catalog:
    def __init__(self, entries=None):
        self.entries = tuple(entries if entries is not None else (Reward(1, 5), Reward(2, 10), Reward(52, 250)))

    def active(self, now):
        return self.entries


def state(spent=12, *, origin="new_local", now=NOW):
    initial = {"bootstrap_schema": 1, "tables": {"game_param2": [{"StampCardGrade": 0}],
        "crystals": [{"FreeFlag": True, "Num": 100000, "Date": 1}]}, "other": {"keep": [1]}}
    return spend_crystals(initialize_spending(initial, now, origin=origin), spent, now)


def page(source, period="2026-11"):
    return read_sugoroku(source)["periods"][period]


def fail(source, callback, code):
    before = deepcopy(source)
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert caught.value.code == code
    assert source == before
    return caught.value


def test_record_sources_keep_the_spelling_of_earlier_saves():
    assert (EARNED, BASELINE) == ("earned", "baseline")


def test_new_local_baseline_allows_existing_local_spend_to_reach_rewards():
    source = state()
    initialized = initialize_sugoroku(source)
    assert page(initialized) == {"baseline_spent": 0, "records": {}}
    synced = sync_sugoroku(initialized, Catalog(), NOW)
    assert snapshot_sugoroku(synced, Catalog(), NOW) == [{"ID": 1, "Status": 0}, {"ID": 2, "Status": 0}]
    assert page(synced)["records"]["1"] == {"status": 0, "source": EARNED, "mail_ids": []}
    assert "sugoroku" not in source


@pytest.mark.parametrize("origin", ["legacy"])
def test_legacy_baseline_marks_existing_thresholds_claimed_without_awards(origin):
    source = state(origin=origin)
    synced = sync_sugoroku(source, Catalog(), NOW)
    assert page(synced)["baseline_spent"] == 12
    assert page(synced)["records"] == {
        "1": {"status": 2, "source": BASELINE, "mail_ids": []},
        "2": {"status": 2, "source": BASELINE, "mail_ids": []}}
    assert snapshot_sugoroku(synced, Catalog(), NOW) == [{"ID": 1, "Status": 2}, {"ID": 2, "Status": 2}]
    assert synced["tables"] == source["tables"]


def test_explicit_legacy_spending_baseline_is_also_not_backfilled():
    source = state(0)
    source.pop("crystal_spending")
    source["tables"]["game_param2"][0]["StampCardGrade"] = 12
    source = initialize_spending(source, NOW, baseline_period="2026-11")
    assert page(sync_sugoroku(source, Catalog(), NOW))["baseline_spent"] == 12


def test_legacy_baseline_is_fixed_while_later_spending_can_earn_new_rewards():
    source = sync_sugoroku(state(origin="legacy"), Catalog(), NOW)
    later = spend_crystals(source, 250, NOW + timedelta(seconds=1))
    synced = sync_sugoroku(later, Catalog(), NOW + timedelta(seconds=1))
    records = page(synced)["records"]
    assert page(synced)["baseline_spent"] == 12
    assert records["52"] == {"status": 0, "source": EARNED, "mail_ids": []}
    assert records["1"]["source"] == BASELINE


def test_resource_update_new_old_threshold_uses_saved_legacy_baseline():
    source = sync_sugoroku(state(origin="legacy"), Catalog(), NOW)
    later = spend_crystals(source, 10, NOW)
    updated = sync_sugoroku(later, Catalog((Reward(53, 8), Reward(54, 20))), NOW)
    assert page(updated)["records"]["53"] == {"status": 2, "source": BASELINE, "mail_ids": []}
    assert page(updated)["records"]["54"]["status"] == 0
    assert "1" in page(updated)["records"]


def test_initialization_and_reads_are_isolated_without_clock_or_new_baseline():
    source = sync_sugoroku(state(), Catalog(), NOW)
    later = spend_crystals(source, 10, NOW + timedelta(days=1))
    initialized = initialize_sugoroku(later)
    assert initialized == later
    initialized["other"]["keep"].append(2)
    read = read_sugoroku(source)
    read["periods"]["2026-11"]["records"]["1"]["status"] = 2
    assert page(source)["records"]["1"]["status"] == 0
    assert later["other"] == {"keep": [1]}


def test_snapshot_preserves_catalog_order_and_does_not_omit_unsupported_reached_rewards():
    catalog = Catalog((Reward(2, 10, "unsupported_ticket"), Reward(1, 5)))
    source = state()
    assert snapshot_sugoroku(source, catalog, NOW) == [{"ID": 2, "Status": 0}, {"ID": 1, "Status": 0}]
    assert "sugoroku" not in source


def test_unreached_rows_are_absent_instead_of_unread_status_zero():
    assert snapshot_sugoroku(state(4), Catalog(), NOW) == []
    assert snapshot_sugoroku(state(5), Catalog(), NOW) == [{"ID": 1, "Status": 0}]


def test_read_acknowledgement_only_changes_zero_and_never_downgrades_claimed():
    source = sync_sugoroku(state(), Catalog(), NOW)
    read = mark_sugoroku(source, [1, 2], deliveries={})
    assert [row["Status"] for row in snapshot_sugoroku(read, Catalog(), NOW)] == [1, 1]
    claimed = mark_sugoroku(read, [1], claimed=True, deliveries={1: [11, 12]})
    assert mark_sugoroku(claimed, [1, 2]) == claimed
    assert page(claimed)["records"]["1"] == {"status": 2, "source": EARNED, "mail_ids": [11, 12]}
    assert page(source)["records"]["1"]["status"] == 0


def test_claim_mapping_is_idempotent_and_cannot_reassign_old_delivery():
    source = sync_sugoroku(state(), Catalog(), NOW)
    claimed = mark_sugoroku(source, [1, 2], claimed=True, deliveries={1: [11], 2: [12]})
    assert mark_sugoroku(claimed, [2, 1], claimed=True, deliveries={1: [11], 2: [12]}) == claimed
    fail(claimed, lambda: mark_sugoroku(claimed, [1], claimed=True, deliveries={1: [13]}), "sugoroku_already_claimed")


def test_explicit_empty_delivery_supports_nonmail_reward_without_implicit_claims():
    source = sync_sugoroku(state(), Catalog(), NOW)
    claimed = mark_sugoroku(source, [1], claimed=True, deliveries={1: []})
    assert page(claimed)["records"]["1"] == {"status": 2, "source": EARNED, "mail_ids": []}
    fail(source, lambda: mark_sugoroku(source, [1], claimed=True), "sugoroku_deliveries_invalid")


def test_historical_baseline_cannot_receive_new_mail_and_empty_ack_is_stable():
    source = sync_sugoroku(state(origin="legacy"), Catalog(), NOW)
    fail(source, lambda: mark_sugoroku(source, [1], claimed=True, deliveries={1: [22]}), "sugoroku_already_claimed")
    assert mark_sugoroku(source, [1], claimed=True, deliveries={1: []}) == source
    assert mark_sugoroku(source, [], deliveries={}) == source


def test_claimed_old_id_stays_claimed_across_removal_and_reintroduction():
    source = sync_sugoroku(state(), Catalog(), NOW)
    source = mark_sugoroku(source, [1], claimed=True, deliveries={1: [88]})
    hidden = sync_sugoroku(source, Catalog((Reward(2, 10),)), NOW)
    assert snapshot_sugoroku(hidden, Catalog((Reward(2, 10),)), NOW) == [{"ID": 2, "Status": 0}]
    restored = sync_sugoroku(hidden, Catalog((Reward(1, 8),)), NOW)
    assert snapshot_sugoroku(restored, Catalog((Reward(1, 8),)), NOW) == [{"ID": 1, "Status": 2}]
    assert page(restored)["records"]["1"]["mail_ids"] == [88]


def test_new_month_keeps_old_claims_and_can_earn_same_id_again():
    source = sync_sugoroku(state(origin="legacy"), Catalog(), NOW)
    after_month = spend_crystals(source, 12, datetime(2026, 12, 1, 4, tzinfo=JST))
    synced = sync_sugoroku(after_month, Catalog(), datetime(2026, 12, 1, 4, tzinfo=JST))
    assert page(synced) == page(source)
    assert page(synced, "2026-12")["baseline_spent"] == 0
    assert snapshot_sugoroku(synced, Catalog(), datetime(2026, 12, 1, 4, tzinfo=JST)) == [
        {"ID": 1, "Status": 0}, {"ID": 2, "Status": 0}]


def test_stale_spending_period_requires_refresh_before_any_board_change():
    source = state()
    next_month = datetime(2026, 12, 1, 4, tzinfo=JST)
    fail(source, lambda: sync_sugoroku(source, Catalog(), next_month), "sugoroku_refresh_required")
    refreshed = settle_spending(source, next_month)
    assert snapshot_sugoroku(refreshed, Catalog(), next_month) == []


def test_mark_requires_a_synced_current_period():
    source = sync_sugoroku(state(), Catalog(), NOW)
    refreshed = settle_spending(source, datetime(2026, 12, 1, 4, tzinfo=JST))
    fail(refreshed, lambda: mark_sugoroku(refreshed, [1]), "sugoroku_refresh_required")


def test_both_spending_and_board_watermarks_prevent_time_rollback():
    source = state(now=NOW + timedelta(hours=1))
    fail(source, lambda: sync_sugoroku(source, Catalog(), NOW), "sugoroku_clock_backwards")
    source = sync_sugoroku(state(), Catalog(), NOW + timedelta(hours=2))
    fail(source, lambda: snapshot_sugoroku(source, Catalog(), NOW + timedelta(hours=1)), "sugoroku_clock_backwards")


@pytest.mark.parametrize("clock,month", MONTH_TURNS)
def test_the_board_month_turns_at_four_on_the_first_in_every_representation(clock, month):
    synced = [sync_sugoroku(state(now=moment), Catalog(), moment) for moment in representations(clock)]
    assert all(result == synced[0] for result in synced) and list(read_sugoroku(synced[0])["periods"]) == [month]


@pytest.mark.parametrize("clock", OUTSIDE_CLIENT_CALENDAR)
def test_instants_outside_the_client_calendar_are_board_clock_errors(clock):
    source = state()
    error = fail(source, lambda: sync_sugoroku(source, Catalog(), clock), "sugoroku_clock_invalid")
    assert error.__cause__.code == "business_clock_invalid"


def test_native_projection_caps_thresholds_even_when_exact_spend_is_larger():
    source = state(40000)
    catalog = Catalog((Reward(1, 32767), Reward(2, 32768), Reward(3, 40000)))
    assert snapshot_sugoroku(source, catalog, NOW) == [{"ID": 1, "Status": 0}]


@pytest.mark.parametrize("ids", [None, {}, "1", [True], [0], [-1], [1, 1], [0x80000000], [1.0]])
def test_selection_requires_distinct_positive_native_ids(ids):
    source = sync_sugoroku(state(), Catalog(), NOW)
    fail(source, lambda: mark_sugoroku(source, ids), "sugoroku_selection_invalid")


@pytest.mark.parametrize("deliveries", [None, {}, {1: [1], 2: [2]}, {"1": [1]}, {1: (1,)}, {1: [0]}, {1: [True]},
                                        {1: [1, 1]}, {1: [0x80000000]}])
def test_claim_delivery_mapping_is_strict(deliveries):
    source = sync_sugoroku(state(), Catalog(), NOW)
    fail(source, lambda: mark_sugoroku(source, [1], claimed=True, deliveries=deliveries), "sugoroku_deliveries_invalid")


def test_mixed_unknown_claim_and_duplicate_cross_reward_mail_are_atomic():
    source = sync_sugoroku(state(), Catalog(), NOW)
    fail(source, lambda: mark_sugoroku(source, [1, 999], claimed=True, deliveries={1: [1], 999: [2]}), "sugoroku_not_reached")
    fail(source, lambda: mark_sugoroku(source, [1, 2], claimed=True, deliveries={1: [1], 2: [1]}), "sugoroku_state_invalid")
    fail(source, lambda: mark_sugoroku(source, [1], deliveries={1: [1]}), "sugoroku_deliveries_invalid")


@pytest.mark.parametrize("value", [None, {}, [], {"schema_version": 2}, {"schema_version": True}])
def test_unknown_or_malformed_existing_ledger_is_not_replaced(value):
    source = state()
    source["sugoroku"] = value
    fail(source, lambda: initialize_sugoroku(source), "sugoroku_state_invalid")


@pytest.mark.parametrize("mutation", ["future", "bad_period", "baseline_too_high", "record_extra", "bad_id", "bool_status",
    "unknown_source", "baseline_unclaimed", "baseline_mail", "unclaimed_mail", "mail_bool", "bad_time", "missing_spending"])
def test_saved_state_schema_and_identity_boundaries_are_strict(mutation):
    source = sync_sugoroku(state(), Catalog(), NOW)
    ledger = source["sugoroku"]
    records = ledger["periods"]["2026-11"]["records"]
    if mutation == "future":
        ledger["periods"]["2026-12"] = {"baseline_spent": 0, "records": {}}
    elif mutation == "bad_period":
        ledger["periods"]["2026-1"] = ledger["periods"].pop("2026-11")
    elif mutation == "baseline_too_high":
        ledger["periods"]["2026-11"]["baseline_spent"] = 13
    elif mutation == "record_extra":
        records["1"]["extra"] = 0
    elif mutation == "bad_id":
        records["01"] = records.pop("1")
    elif mutation == "bool_status":
        records["1"]["status"] = True
    elif mutation == "unknown_source":
        records["1"]["source"] = "unknown"
    elif mutation == "baseline_unclaimed":
        records["1"]["source"] = BASELINE
    elif mutation == "baseline_mail":
        ledger["periods"]["2026-11"]["baseline_spent"] = 5
        records["1"].update(source=BASELINE, status=2, mail_ids=[1])
    elif mutation == "unclaimed_mail":
        records["1"]["mail_ids"] = [1]
    elif mutation == "mail_bool":
        records["1"].update(status=2, mail_ids=[True])
    elif mutation == "bad_time":
        ledger["last_time"] = True
    else:
        source.pop("crystal_spending")
    fail(source, lambda: read_sugoroku(source), "crystal_spending_required" if mutation == "missing_spending" else "sugoroku_state_invalid")


@pytest.mark.parametrize("entries", [(Reward(1, 5), Reward(1, 10)), (Reward(True, 5),), (Reward(0, 5),),
                                      (Reward(1, -1),), (Reward(1, True),), (None,)])
def test_invalid_catalog_snapshot_does_not_initialize_or_write(entries):
    source = state()
    fail(source, lambda: sync_sugoroku(source, Catalog(entries), NOW), "sugoroku_catalog_invalid")


def test_total_record_limit_spans_all_periods_and_never_truncates(monkeypatch):
    assert MAX_RECORDS == 65535
    monkeypatch.setattr(module, "MAX_RECORDS", 3)
    source = sync_sugoroku(state(), Catalog(), NOW)
    next_month = datetime(2026, 12, 1, 4, tzinfo=JST)
    source = spend_crystals(source, 12, next_month)
    fail(source, lambda: sync_sugoroku(source, Catalog(), next_month), "sugoroku_capacity")
    assert len(page(source)["records"]) == 2


def test_period_limit_preserves_old_periods_and_refuses_another(monkeypatch):
    assert MAX_PERIODS == 1200
    monkeypatch.setattr(module, "MAX_PERIODS", 1)
    source = sync_sugoroku(state(), Catalog(), NOW)
    next_month = datetime(2026, 12, 1, 4, tzinfo=JST)
    refreshed = spend_crystals(source, 12, next_month)
    fail(refreshed, lambda: sync_sugoroku(refreshed, Catalog(), next_month), "sugoroku_capacity")
    assert page(refreshed) == page(source)


def test_mail_identity_cannot_be_reused_across_monthly_rewards():
    source = sync_sugoroku(state(), Catalog(), NOW)
    source = mark_sugoroku(source, [1], claimed=True, deliveries={1: [88]})
    next_month = datetime(2026, 12, 1, 4, tzinfo=JST)
    source = sync_sugoroku(spend_crystals(source, 12, next_month), Catalog(), next_month)
    fail(source, lambda: mark_sugoroku(source, [1], claimed=True, deliveries={1: [88]}), "sugoroku_state_invalid")
