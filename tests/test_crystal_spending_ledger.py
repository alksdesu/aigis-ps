from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json

import pytest

import aigis_ps.crystal_spending as module
from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.business_calendar import month_start
from aigis_ps.crystal_spending import (
    INT64_MAX,
    LEGACY,
    LEGACY_EXPLICIT,
    LEGACY_ZERO,
    NEW_LOCAL,
    PROJECTION_CAP,
    initialize_spending,
    read_spending,
    settle_spending,
    spend_crystals,
    spending_response,
)
from aigis_ps.crystal_wallet import credit_free_crystals, credit_paid_crystals, wallet_totals
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.native_numbers import INT32_MAX
from test_business_calendar import offset


NOW = datetime(2026, 11, 12, 12, 34, 56, 123000, tzinfo=JST)
NEXT_MONTH = month_start(date(2026, 12, 1))
MONTH_TURNS = (((2026, 12, 1, 3, 59, 59, 999000), "2026-11"), ((2026, 12, 1, 4), "2026-12"),
               ((2027, 1, 1, 3, 59, 59, 999000), "2026-12"), ((2027, 1, 1, 4), "2027-01"))
OUTSIDE_CLIENT_CALENDAR = (datetime(1969, 12, 31, 23, 59, 59, tzinfo=JST), datetime(3001, 1, 20, tzinfo=JST))


def representations(clock):
    moment = datetime(*clock, tzinfo=JST)
    return moment, moment.astimezone(timezone.utc), moment.astimezone(offset(600)), moment.replace(tzinfo=None)


@pytest.fixture
def state():
    return {"bootstrap_schema": 1, "created_local": "2020/01/01 00:00:00", "tables": {
        "game_param2": [{"StampCardGrade": 0, "LastLoginDate": 739000, "LogInStampCount": 9,
                         "GameStartDateTime": "2020/01/01 00:00:00", "Other": [1]}],
        "game_param": [{"CharismaChangeDate": 739000, "CharismaChangeTime": 0.25}],
        "crystals": [{"FreeFlag": False, "Date": 1, "Num": 11},
                     {"FreeFlag": True, "Date": 2, "Num": 3},
                     {"FreeFlag": True, "Date": 1, "Num": 5}],
        "presents": [{"Type": 0, "Content": 5}]}, "sentinel": {"nested": ["unchanged"]}}


def packed(state):
    return state["tables"]["game_param2"][0]["StampCardGrade"]


def set_counter(state, *, current=None, previous=None):
    result = deepcopy(state)
    ledger = result["crystal_spending"]
    if current is not None:
        ledger["current_spent"] = current
    if previous is not None:
        ledger["previous_spent"] = previous
    result["tables"]["game_param2"][0]["StampCardGrade"] = (
        min(ledger["previous_spent"], PROJECTION_CAP) << 16) | min(ledger["current_spent"], PROJECTION_CAP)
    return result


def fail(state, callback, code):
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert caught.value.code == code
    assert state == before
    return caught.value


def test_origins_and_baseline_sources_keep_the_spelling_existing_saves_use():
    assert (LEGACY, NEW_LOCAL, LEGACY_ZERO, LEGACY_EXPLICIT) == (
        "legacy", "new_local", "legacy_zero", "legacy_explicit")


@pytest.mark.parametrize("origin,source", [("legacy", "legacy_zero"), ("new_local", "new_local")])
def test_initialize_zero_creates_current_baseline_without_touching_other_data(state, origin, source):
    before = deepcopy(state)
    updated = initialize_spending(state, NOW, origin=origin)
    assert read_spending(updated) == {"schema_version": 1, "period": "2026-11", "current_spent": 0,
        "previous_spent": 0, "last_time": int(NOW.timestamp() * 1000), "baseline_source": source}
    assert {key: value for key, value in updated.items() if key != "crystal_spending"} == before
    assert state == before
    updated["tables"]["game_param2"][0]["Other"].append(2)
    updated["sentinel"]["nested"].append("isolated")
    assert state == before


def test_existing_initialize_only_validates_and_returns_an_isolated_copy(state):
    source = initialize_spending(state, NOW)
    updated = initialize_spending(source, NOW + timedelta(days=90), origin="new_local", baseline_period="2000-01")
    assert updated == source
    updated["crystal_spending"]["current_spent"] = 10
    updated["sentinel"]["nested"].append("isolated")
    assert read_spending(source)["current_spent"] == 0
    assert source["sentinel"]["nested"] == ["unchanged"]


def test_read_requires_known_baseline_and_returns_a_copy(state):
    fail(state, lambda: read_spending(state), "crystal_spending_required")
    source = initialize_spending(state, NOW)
    result = read_spending(source)
    result["current_spent"] = 3
    assert read_spending(source)["current_spent"] == 0


@pytest.mark.parametrize("clock", [NOW, NOW.astimezone(timezone.utc), NOW.astimezone(timezone(timedelta(hours=8))),
                                  NOW.replace(tzinfo=None)])
def test_same_instant_uses_jst_period_and_utc_milliseconds(state, clock):
    assert initialize_spending(state, clock) == initialize_spending(state, NOW)


@pytest.mark.parametrize("clock,period", [
    (datetime(2026, 11, 1, 3, 59, 59, 999999, tzinfo=JST), "2026-10"),
    (datetime(2026, 11, 1, 4, tzinfo=JST), "2026-11"),
    (datetime(2027, 1, 1, 3, 59, 59, tzinfo=JST), "2026-12"),
    (datetime(2027, 1, 1, 4, tzinfo=JST), "2027-01"),
])
def test_month_boundaries_are_jst_day_one_at_four(state, clock, period):
    assert read_spending(initialize_spending(state, clock))["period"] == period


def test_explicit_legacy_baseline_preserves_both_halves_without_guessing(state):
    state["tables"]["game_param2"][0]["StampCardGrade"] = (800 << 16) | 51
    before = deepcopy(state)
    fail(state, lambda: initialize_spending(state, NOW), "crystal_spending_baseline_required")
    updated = initialize_spending(state, NOW, baseline_period="2026-11")
    ledger = read_spending(updated)
    assert ledger["previous_spent"] == 800 and ledger["current_spent"] == 51
    assert ledger["baseline_source"] == "legacy_explicit"
    assert ledger["last_time"] == int(NOW.timestamp() * 1000)
    assert {key: value for key, value in updated.items() if key != "crystal_spending"} == before


def test_old_baseline_uses_its_own_month_start_then_settles_once(state):
    state["tables"]["game_param2"][0]["StampCardGrade"] = (800 << 16) | 51
    updated = initialize_spending(state, NOW, baseline_period="2026-10")
    ledger = read_spending(updated)
    assert ledger["period"] == "2026-10"
    assert ledger["last_time"] == int(month_start(date(2026, 10, 1)).timestamp() * 1000)
    settled = settle_spending(updated, NOW)
    assert read_spending(settled)["previous_spent"] == 51
    assert read_spending(settled)["current_spent"] == 0
    assert packed(settled) == 51 << 16
    assert settle_spending(settled, NOW) == settled
    assert packed(updated) == (800 << 16) | 51


def test_zero_legacy_baseline_does_not_infer_history_from_old_login_dates(state):
    updated = initialize_spending(state, NOW, baseline_period="2000-01")
    assert read_spending(updated)["period"] == "2026-11"
    assert read_spending(updated)["baseline_source"] == "legacy_zero"
    assert updated["tables"]["game_param2"] == state["tables"]["game_param2"]


@pytest.mark.parametrize("baseline", ["2026-12", "2026-1", "2026/11", "2026-00", "2026-13", "0000-01",
                                      "2026-11\n", "２０２６-11", 202611, [], ""])
def test_invalid_or_future_explicit_baseline_is_rejected(state, baseline):
    state["tables"]["game_param2"][0]["StampCardGrade"] = 50
    fail(state, lambda: initialize_spending(state, NOW, baseline_period=baseline), "crystal_spending_baseline_invalid")


@pytest.mark.parametrize("origin", [None, "unknown", True, [], 1])
def test_unknown_origin_is_rejected(state, origin):
    fail(state, lambda: initialize_spending(state, NOW, origin=origin), "crystal_spending_origin_invalid")


@pytest.mark.parametrize("value,baseline", [(1, None), (0, "2026-11")])
def test_new_local_origin_cannot_import_spending_history(state, value, baseline):
    state["tables"]["game_param2"][0]["StampCardGrade"] = value
    fail(state, lambda: initialize_spending(state, NOW, origin="new_local", baseline_period=baseline),
         "crystal_spending_baseline_invalid")


@pytest.mark.parametrize("value", [True, False, -1, INT32_MAX + 1, 32768, 65535, (12 << 16) | 32768,
                                   "0", 0.0, None])
def test_invalid_legacy_packed_halves_are_never_silently_rewritten(state, value):
    state["tables"]["game_param2"][0]["StampCardGrade"] = value
    fail(state, lambda: initialize_spending(state, NOW, baseline_period="2026-11"), "crystal_spending_invalid")


@pytest.mark.parametrize("kind", ["missing", "empty", "two_rows", "wrong_table", "wrong_row", "wrong_bootstrap"])
def test_missing_or_invalid_game_param2_is_rejected(state, kind):
    if kind == "missing":
        state["tables"]["game_param2"][0].pop("StampCardGrade")
    elif kind == "empty":
        state["tables"]["game_param2"] = []
    elif kind == "two_rows":
        state["tables"]["game_param2"] *= 2
    elif kind == "wrong_table":
        state["tables"] = None
    elif kind == "wrong_row":
        state["tables"]["game_param2"] = [None]
    else:
        state["bootstrap_schema"] = True
    fail(state, lambda: initialize_spending(state, NOW), "crystal_spending_invalid")


@pytest.mark.parametrize("clock", [None, "2026-11-12", 0, datetime(1, 1, 1)])
def test_bad_clock_rejects_without_initializing(state, clock):
    fail(state, lambda: initialize_spending(state, clock), "crystal_spending_clock_invalid")


@pytest.mark.parametrize("clock,month", MONTH_TURNS)
def test_the_spending_month_turns_at_four_on_the_first_in_every_representation(state, clock, month):
    ledgers = [initialize_spending(state, moment, origin="new_local")["crystal_spending"]
               for moment in representations(clock)]
    assert all(ledger == ledgers[0] for ledger in ledgers) and ledgers[0]["period"] == month


@pytest.mark.parametrize("clock", OUTSIDE_CLIENT_CALENDAR)
def test_instants_outside_the_client_calendar_are_spending_clock_errors(state, clock):
    error = fail(state, lambda: initialize_spending(state, clock), "crystal_spending_clock_invalid")
    assert error.__cause__.code == "business_clock_invalid"


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 2}, {"extra": 1}, {"period": "2026-1"},
    {"period": "2026-12"}, {"current_spent": True}, {"current_spent": -1}, {"current_spent": INT64_MAX + 1},
    {"previous_spent": -1}, {"previous_spent": INT64_MAX + 1}, {"last_time": True}, {"last_time": 0.0},
    {"last_time": INT64_MAX}, {"last_time": INT64_MAX + 1}, {"baseline_source": "unknown"},
])
def test_malformed_ledger_and_period_time_mismatch_are_rejected(state, changes):
    source = initialize_spending(state, NOW)
    source["crystal_spending"].update(changes)
    fail(source, lambda: read_spending(source), "crystal_spending_invalid")
    fail(source, lambda: settle_spending(source, NOW), "crystal_spending_invalid")


@pytest.mark.parametrize("value", [None, [], {}, {"schema_version": 1}])
def test_present_but_unsupported_ledger_is_not_initialized_over(state, value):
    state["crystal_spending"] = value
    fail(state, lambda: initialize_spending(state, NOW), "crystal_spending_invalid")


def test_projection_mismatch_is_rejected_in_every_path(state):
    source = initialize_spending(state, NOW)
    source["tables"]["game_param2"][0]["StampCardGrade"] = 1
    for action in (lambda: read_spending(source), lambda: initialize_spending(source, NOW),
                   lambda: settle_spending(source, NOW), lambda: spend_crystals(source, 1, NOW),
                   lambda: spending_response(source)):
        fail(source, action, "crystal_spending_invalid")


@pytest.mark.parametrize("days", [1, 1000])
def test_time_cannot_move_backwards_even_with_unchanged_amount(state, days):
    source = initialize_spending(state, NOW)
    fail(source, lambda: settle_spending(source, NOW - timedelta(days=days)), "crystal_spending_clock_backwards")
    fail(source, lambda: spend_crystals(source, 0, NOW - timedelta(days=days)), "crystal_spending_clock_backwards")


def test_same_month_settlement_only_advances_millisecond_watermark(state):
    source = set_counter(initialize_spending(state, NOW), current=123, previous=456)
    settled = settle_spending(source, NOW + timedelta(milliseconds=1))
    expected = deepcopy(source)
    expected["crystal_spending"]["last_time"] += 1
    assert settled == expected
    assert settle_spending(source, NOW) == source


@pytest.mark.parametrize("start,end", [
    (datetime(2026, 10, 31, 12, tzinfo=JST), month_start(date(2026, 11, 1))),
    (datetime(2026, 12, 31, 12, tzinfo=JST), month_start(date(2027, 1, 1))),
    (datetime(2024, 2, 29, 12, tzinfo=JST), month_start(date(2024, 3, 1))),
])
def test_one_month_rollover_moves_only_finished_month_total(state, start, end):
    source = set_counter(initialize_spending(state, start), current=123, previous=456)
    settled = settle_spending(source, end)
    ledger = read_spending(settled)
    assert ledger["previous_spent"] == 123 and ledger["current_spent"] == 0
    assert packed(settled) == 123 << 16
    assert settled["tables"]["crystals"] == source["tables"]["crystals"]
    assert ledger["baseline_source"] == "legacy_zero"


@pytest.mark.parametrize("end", [month_start(date(2027, 1, 1)), month_start(date(2028, 3, 1))])
def test_skipped_months_do_not_reuse_an_older_month_as_previous(state, end):
    source = set_counter(initialize_spending(state, NOW), current=123, previous=456)
    settled = settle_spending(source, end)
    assert read_spending(settled)["previous_spent"] == read_spending(settled)["current_spent"] == 0
    assert packed(settled) == 0


def test_saturation_keeps_exact_actual_counts_and_safe_positive_native_packing(state):
    source = set_counter(initialize_spending(state, NOW), current=32766, previous=INT64_MAX)
    updated = spend_crystals(source, 2, NOW)
    assert read_spending(updated)["current_spent"] == 32768
    assert read_spending(updated)["previous_spent"] == INT64_MAX
    assert packed(updated) == 0x7FFF7FFF
    assert packed(updated) >> 16 == PROJECTION_CAP
    next_month = settle_spending(updated, NEXT_MONTH)
    assert read_spending(next_month)["previous_spent"] == 32768
    assert packed(next_month) == PROJECTION_CAP << 16


def test_total_counter_overflow_does_not_debit_the_wallet(state):
    source = set_counter(initialize_spending(state, NOW), current=INT64_MAX)
    fail(source, lambda: spend_crystals(source, 1, NOW), "crystal_spending_overflow")
    assert spend_crystals(source, 0, NOW) == source
    next_month = spend_crystals(source, 1, NEXT_MONTH)
    assert read_spending(next_month)["current_spent"] == 1
    assert read_spending(next_month)["previous_spent"] == INT64_MAX


@pytest.mark.parametrize("amount,options,nums", [(10, {}, [9, 0, 0]), (13, {"free_first": False}, [0, 3, 3]),
                                               (11, {"paid_only": True}, [0, 3, 5]), (19, {}, [0, 0, 0])])
def test_actual_debit_preserves_wallet_policy_and_counts_paid_plus_free_once(state, amount, options, nums):
    before = deepcopy(state)
    updated = spend_crystals(state, amount, NOW, **options)
    assert [row["Num"] for row in updated["tables"]["crystals"]] == nums
    assert wallet_totals(before)["total"] - wallet_totals(updated)["total"] == amount
    assert read_spending(updated)["current_spent"] == amount and packed(updated) == amount
    assert state == before
    assert updated["tables"]["game_param2"][0]["LastLoginDate"] == 739000
    assert updated["tables"]["game_param2"][0]["LogInStampCount"] == 9
    assert updated["tables"]["game_param"] == state["tables"]["game_param"]
    assert updated["tables"]["presents"] == state["tables"]["presents"]


def test_failed_debit_does_not_commit_even_month_rollover(state):
    source = set_counter(initialize_spending(state, NOW), current=123)
    fail(source, lambda: spend_crystals(source, 20, NEXT_MONTH), "crystal_insufficient")
    fail(source, lambda: spend_crystals(source, 12, NOW, paid_only=True), "crystal_insufficient")


def test_zero_debit_can_initialize_or_settle_but_never_adds_consumption(state):
    source = spend_crystals(state, 0, NOW)
    assert read_spending(source)["current_spent"] == 0
    assert source["tables"] == state["tables"]
    source = set_counter(source, current=12)
    settled = spend_crystals(source, 0, NEXT_MONTH)
    assert read_spending(settled)["previous_spent"] == 12
    assert read_spending(settled)["current_spent"] == 0
    assert settled["tables"]["crystals"] == state["tables"]["crystals"]


@pytest.mark.parametrize("amount,options", [(True, {}), (-1, {}), (1.0, {}), ("1", {}), (INT32_MAX + 1, {}),
                                           (1, {"paid_only": 1}), (1, {"free_first": None})])
def test_invalid_debit_arguments_are_rejected_before_initializing(state, amount, options):
    fail(state, lambda: spend_crystals(state, amount, NOW, **options), "invalid_crystal_debit")


def test_bad_wallet_does_not_return_an_initialized_candidate(state):
    state["tables"]["crystals"].append(deepcopy(state["tables"]["crystals"][0]))
    fail(state, lambda: spend_crystals(state, 0, NOW), "invalid_crystal_state")


@pytest.mark.parametrize("kind", ["no_debit", "too_much", "ledger_changed"])
def test_debit_result_must_match_actual_amount_and_preserve_the_ledger(state, monkeypatch, kind):
    original = module.debit_crystals
    def broken(source, amount, **options):
        if kind == "no_debit":
            return deepcopy(source)
        result = original(source, amount + (kind == "too_much"), **options)
        if kind == "ledger_changed":
            result["crystal_spending"]["baseline_source"] = "new_local"
        return result
    monkeypatch.setattr(module, "debit_crystals", broken)
    fail(state, lambda: spend_crystals(state, 1, NOW), "crystal_spending_debit_invalid")


def test_rewards_and_refund_credits_do_not_increase_or_decrease_recorded_spend(state):
    source = spend_crystals(state, 5, NOW)
    credited = credit_free_crystals(source, 5, 739001)
    credited = credit_paid_crystals(credited, 9, 739001)
    assert read_spending(credited) == read_spending(source)
    assert packed(credited) == 5
    assert wallet_totals(credited)["total"] == wallet_totals(source)["total"] + 14


def test_serialized_reopen_preserves_baseline_without_reinitializing_counts(state):
    source = spend_crystals(state, 5, NOW)
    reopened = json.loads(json.dumps(source))
    assert initialize_spending(reopened, NOW + timedelta(days=35)) == source
    assert read_spending(reopened) == read_spending(source)
    assert settle_spending(reopened, NOW) == source


def test_response_styles_have_distinct_native_shapes_and_no_extra_fields(state):
    source = set_counter(initialize_spending(state, NOW), current=17, previous=123)
    value = (123 << 16) | 17
    key_values = Container("PGP2", (Column("A1", "S", ("C1",)), Column("A2", "S", (str(value),))))
    columns = Container("PGP2", (Column("C1", "I", (value,)),))
    assert spending_response(source) == spending_response(source, style="key_values") == key_values
    assert spending_response(source, style="columns") == columns
    assert loads(dumps(Container(children=(key_values,)))).children == (key_values,)
    assert loads(dumps(Container(children=(columns,)))).children == (columns,)


@pytest.mark.parametrize("style", ["", "unknown", "column", None, True, 1, []])
def test_unknown_response_style_is_rejected(state, style):
    source = initialize_spending(state, NOW)
    fail(source, lambda: spending_response(source, style=style), "crystal_spending_style_invalid")
