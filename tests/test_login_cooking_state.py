from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest

from aigis_ps.crystal_spending import initialize_spending, settle_spending
from aigis_ps.energy_clock import JST, instant
from aigis_ps.errors import ProtocolError
from aigis_ps.login_cooking_state import (
    LEDGER_KEY, MAX_COOKING, RULES_CHANGED, advance_cooking, initialize_cooking, migrate_cooking, read_cooking,
    read_current_cooking, sync_cooking,
)
from aigis_ps.login_reward_state import advance_login_rewards, initialize_login_rewards
from test_login_reward_state import (
    BUSINESS_EDGES, OUTSIDE_CALENDAR, epoch_milliseconds, month_text, representations, shifted_day,
)


NOW = datetime(2026, 11, 10, 12, 34, 56, 789123, tzinfo=JST)
BORDER = 250


def base(*, now=NOW, previous=280, new=False):
    state = {"bootstrap_schema": 1, "tables": {"game_param2": [{
        "HomeCookingReceivedAmount": 0, "HomeCookingReceivedDate": 0,
        "LastLoginDate": 0, "LogInStampCount": 0, "StampCardGrade": 0}],
        "presents": [{"saved": [1]}], "game_param": [{"NowCharisma": 123}]},
        "sentinel": {"keep": [2]}}
    state = initialize_spending(state, now, origin="new_local" if new else "legacy")
    state["crystal_spending"]["previous_spent"] = previous
    state["tables"]["game_param2"][0]["StampCardGrade"] = min(previous, 32767) << 16
    return initialize_login_rewards(state, now, new_account=new)


def seeded(**kwargs):
    return initialize_cooking(base(**kwargs), border=BORDER)


def fields(state):
    row = state["tables"]["game_param2"][0]
    return row["HomeCookingReceivedAmount"], row["HomeCookingReceivedDate"]


def unrelated(state):
    result = deepcopy(state)
    result.pop("login_cooking", None)
    result["tables"]["game_param2"][0].pop("HomeCookingReceivedAmount")
    result["tables"]["game_param2"][0].pop("HomeCookingReceivedDate")
    return result


def fail(state, callback, code=None):
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert state == before
    if code is not None:
        assert caught.value.code == code
    return caught.value


@pytest.mark.parametrize("new,source", [(True, "new_local"), (False, "legacy_zero")])
def test_empty_baseline_is_isolated_and_uses_saved_clock(new, source):
    state = base(new=new)
    updated = initialize_cooking(state, border=BORDER)
    assert read_cooking(updated) == {"schema_version": 1, "period": "2026-11", "quota": 2,
        "issued": 0, "last_claim_day": None, "last_civil_day": None,
        "last_time": state["crystal_spending"]["last_time"], "baseline_source": source, "border": 250}
    assert unrelated(updated) == unrelated(state)
    assert "login_cooking" not in state
    updated["sentinel"]["keep"].append(3)
    assert state["sentinel"]["keep"] == [2]


def test_the_ledger_stays_under_the_key_existing_saves_use():
    state = seeded()
    assert LEDGER_KEY == "login_cooking" and state[LEDGER_KEY] == read_cooking(state)


@pytest.mark.parametrize("previous,quota", [(0, 0), (249, 0), (250, 0), (264, 0), (265, 1),
    (279, 1), (280, 2), (624, 24), (625, 25), (626, 25), (32767, 25), (2**63 - 1, 25)])
def test_monthly_quota_is_incremental_then_capped(previous, quota):
    assert MAX_COOKING == 25
    assert read_cooking(seeded(previous=previous))["quota"] == quota


def test_previous_spending_projects_to_signed_half_before_quota_math():
    state = initialize_cooking(base(previous=100000), border=32750)
    assert state["login_cooking"]["quota"] == 1


def test_each_logical_day_grants_only_one_and_stops_at_month_quota():
    state = seeded()
    first, yes = advance_cooking(state, NOW, border=BORDER, eligible=True)
    assert yes and fields(first) == (1, NOW.date().toordinal() - 1)
    assert unrelated(first) == unrelated(state)
    duplicate, yes = advance_cooking(first, NOW + timedelta(hours=1), border=BORDER, eligible=True)
    assert not yes and fields(duplicate) == fields(first)
    second, yes = advance_cooking(duplicate, NOW + timedelta(days=1), border=BORDER, eligible=True)
    assert yes and second["login_cooking"]["issued"] == 2
    exhausted, yes = advance_cooking(second, NOW + timedelta(days=2), border=BORDER, eligible=True)
    assert not yes and fields(exhausted) == fields(second)


def test_skipped_login_days_do_not_backfill_multiple_meals():
    state = seeded(previous=625)
    later = NOW + timedelta(days=15)
    updated, yes = advance_cooking(state, later, border=BORDER, eligible=True)
    assert yes and updated["login_cooking"]["issued"] == 1
    assert updated["login_cooking"]["last_claim_day"] == "2026-11-25"


def test_already_completed_legacy_login_does_not_get_an_upgrade_backfill():
    state = base()
    state, advance = advance_login_rewards(state, NOW)
    assert advance.new_day
    state = initialize_cooking(state, border=BORDER)
    updated, yes = advance_cooking(state, NOW, border=BORDER, eligible=False)
    assert not yes and fields(updated) == (0, 0)
    tomorrow, advance = advance_login_rewards(updated, NOW + timedelta(days=1))
    updated, yes = advance_cooking(tomorrow, NOW + timedelta(days=1), border=BORDER, eligible=advance.new_day)
    assert yes and fields(updated)[0] == 1


def test_zero_quota_has_no_claim_dates_or_rewards():
    state = seeded(previous=250)
    updated, yes = advance_cooking(state, NOW, border=BORDER, eligible=True)
    assert not yes and updated == state
    assert fields(updated) == (0, 0)


def test_early_morning_uses_previous_logical_day_but_current_civil_date():
    now = datetime(2026, 11, 10, 3, 59, 59, 999999, tzinfo=JST)
    state = seeded(now=now)
    updated, yes = advance_cooking(state, now, border=BORDER, eligible=True)
    assert yes and updated["login_cooking"]["last_claim_day"] == "2026-11-09"
    assert updated["login_cooking"]["last_civil_day"] == "2026-11-10"
    assert fields(updated)[1] == date(2026, 11, 10).toordinal() - 1
    after, yes = advance_cooking(updated, now + timedelta(microseconds=1), border=BORDER, eligible=True)
    assert yes and after["login_cooking"]["last_claim_day"] == "2026-11-10"
    assert fields(after) == (2, fields(updated)[1])


def test_month_boundary_replaces_quota_and_count_but_preserves_real_claim_date():
    now = datetime(2026, 11, 1, 3, 59, 59, 999999, tzinfo=JST)
    state = seeded(now=now)
    state["crystal_spending"]["current_spent"] = 625
    state["tables"]["game_param2"][0]["StampCardGrade"] |= 625
    state, yes = advance_cooking(state, now, border=BORDER, eligible=True)
    assert yes and state["login_cooking"]["period"] == "2026-10"
    boundary = datetime(2026, 11, 1, 4, tzinfo=JST)
    state = settle_spending(state, boundary)
    reset = sync_cooking(state, boundary, border=BORDER)
    assert reset["login_cooking"]["quota"] == 25 and fields(reset) == (0, fields(state)[1])
    assert reset["login_cooking"]["last_claim_day"] == "2026-10-31"
    claim, yes = advance_cooking(reset, boundary, border=BORDER, eligible=True)
    assert yes and fields(claim) == (1, fields(state)[1])


@pytest.mark.parametrize("moment", BUSINESS_EDGES)
def test_claim_days_period_and_watermark_follow_the_shifted_and_civil_jst_clock(moment):
    for now in representations(moment):
        day, civil = shifted_day(now), instant(now).astimezone(JST).date()
        updated, yes = advance_cooking(seeded(now=now), now, border=BORDER, eligible=True)
        ledger = updated[LEDGER_KEY]
        assert yes and (ledger["last_claim_day"], ledger["last_civil_day"]) == (day.isoformat(), civil.isoformat())
        assert (ledger["period"], ledger["last_time"]) == (month_text(day), epoch_milliseconds(now))
        assert fields(updated) == (1, civil.toordinal() - 1)


def test_multiple_missed_months_clear_old_quota_without_carrying_unissued_meals():
    state, _ = advance_cooking(seeded(previous=625), NOW, border=BORDER, eligible=True)
    now = datetime(2027, 2, 5, 12, tzinfo=JST)
    rolled = settle_spending(state, now)
    updated = sync_cooking(rolled, now, border=BORDER)
    assert updated["login_cooking"]["period"] == "2027-02"
    assert updated["login_cooking"]["quota"] == updated["login_cooking"]["issued"] == 0
    assert fields(updated)[1] == fields(state)[1]


def test_same_month_read_sync_keeps_equal_state_unless_clock_touch_requested():
    state = seeded()
    later = NOW + timedelta(hours=1)
    unchanged = sync_cooking(state, later, border=BORDER)
    assert unchanged == state and unchanged is not state
    changed = sync_cooking(state, later, border=BORDER, touch_clock=True)
    assert changed["login_cooking"]["last_time"] == state["login_cooking"]["last_time"] + 3600000
    assert fields(changed) == fields(state)


def test_saved_initializer_does_not_read_other_ledgers_or_replace_origin():
    state = seeded()
    state.pop("crystal_spending")
    state.pop("login_rewards")
    updated = initialize_cooking(state, border=BORDER, legacy_period="bad")
    assert updated == state and updated is not state


def test_initializer_takes_later_existing_login_watermark():
    state = base()
    state, _ = advance_login_rewards(state, NOW + timedelta(hours=2))
    updated = initialize_cooking(state, border=BORDER)
    assert updated["login_cooking"]["last_time"] == state["login_rewards"]["last_time"]


@pytest.mark.parametrize("amount,encoded", [(1, 0), (0, 739929), (25, 739929)])
def test_nonzero_legacy_native_fields_require_explicit_period(amount, encoded):
    state = base()
    state["tables"]["game_param2"][0].update(HomeCookingReceivedAmount=amount, HomeCookingReceivedDate=encoded)
    fail(state, lambda: initialize_cooking(state, border=BORDER), "login_cooking_baseline_required")


@pytest.mark.parametrize("amount,encoded", [(1, 0), (0, 739929), (25, 739929)])
def test_new_local_seed_cannot_import_nonzero_legacy_native_fields(amount, encoded):
    state = base(new=True)
    state["tables"]["game_param2"][0].update(HomeCookingReceivedAmount=amount, HomeCookingReceivedDate=encoded)
    fail(state, lambda: initialize_cooking(state, border=BORDER, legacy_period="2026-11"), "login_cooking_baseline_invalid")


def test_explicit_current_legacy_baseline_preserves_overquota_count_without_award():
    state = base(previous=265)
    state["tables"]["game_param2"][0].update(HomeCookingReceivedAmount=5,
        HomeCookingReceivedDate=date(2026, 11, 8).toordinal() - 1)
    updated = initialize_cooking(state, border=BORDER, legacy_period="2026-11")
    assert updated["login_cooking"]["issued"] == 5 and updated["login_cooking"]["quota"] == 1
    assert updated["tables"] == state["tables"]
    after, yes = advance_cooking(updated, NOW, border=BORDER, eligible=True)
    assert not yes and fields(after) == fields(updated)


def test_explicit_older_legacy_baseline_is_preserved_until_sync():
    state = base(previous=625)
    state["tables"]["game_param2"][0].update(HomeCookingReceivedAmount=20,
        HomeCookingReceivedDate=date(2026, 10, 1).toordinal() - 1)
    updated = initialize_cooking(state, border=BORDER, legacy_period="2026-09")
    assert updated["login_cooking"]["period"] == "2026-09"
    assert updated["login_cooking"]["quota"] == 0 and fields(updated)[0] == 20
    assert updated["login_cooking"]["last_claim_day"] == "2026-09-30"
    assert updated["login_cooking"]["last_civil_day"] == "2026-10-01"
    reset = sync_cooking(updated, NOW, border=BORDER)
    assert reset["login_cooking"]["quota"] == 25 and fields(reset) == (0, fields(updated)[1])


def test_current_month_ambiguous_old_civil_date_blocks_duplicate_conservatively():
    now = datetime(2026, 11, 10, 2, tzinfo=JST)
    state = base(now=now)
    state["tables"]["game_param2"][0].update(HomeCookingReceivedAmount=1,
        HomeCookingReceivedDate=date(2026, 11, 10).toordinal() - 1)
    updated = initialize_cooking(state, border=BORDER, legacy_period="2026-11")
    assert updated["login_cooking"]["last_claim_day"] == "2026-11-10"
    later, yes = advance_cooking(updated, now + timedelta(hours=10), border=BORDER, eligible=True)
    assert not yes and fields(later) == fields(updated)


def test_legacy_count_without_date_is_preserved_not_given_an_invented_day():
    state = base()
    state["tables"]["game_param2"][0]["HomeCookingReceivedAmount"] = 1
    updated = initialize_cooking(state, border=BORDER, legacy_period="2026-11")
    assert fields(updated) == (1, 0) and updated["login_cooking"]["last_claim_day"] is None
    after, yes = advance_cooking(updated, NOW, border=BORDER, eligible=False)
    assert not yes and fields(after) == (1, 0)


def test_old_date_can_survive_reset_count_zero():
    state = base()
    state["tables"]["game_param2"][0]["HomeCookingReceivedDate"] = date(2026, 8, 9).toordinal() - 1
    updated = initialize_cooking(state, border=BORDER, legacy_period="2026-11")
    assert fields(updated) == fields(state)


@pytest.mark.parametrize("period", ["2026-12", "2026-13", "2026-00", "2026-1", "0000-01", "abc", 1])
def test_invalid_or_future_legacy_period_is_atomic(period):
    state = base()
    state["tables"]["game_param2"][0]["HomeCookingReceivedAmount"] = 1
    fail(state, lambda: initialize_cooking(state, border=BORDER, legacy_period=period), "login_cooking_invalid")


@pytest.mark.parametrize("claimed", [date(2026, 12, 1), date(2026, 11, 12), date(2026, 10, 9)])
def test_inconsistent_legacy_date_is_rejected(claimed):
    state = base()
    state["tables"]["game_param2"][0].update(HomeCookingReceivedAmount=1, HomeCookingReceivedDate=claimed.toordinal()-1)
    fail(state, lambda: initialize_cooking(state, border=BORDER, legacy_period="2026-11"), "login_cooking_invalid")


@pytest.mark.parametrize("value", [-1, True, False, 1.5, None, "250", 2**31])
def test_invalid_border_rejected(value):
    state = base()
    fail(state, lambda: initialize_cooking(state, border=value), "login_cooking_border_invalid")
    state = seeded()
    fail(state, lambda: read_current_cooking(state, border=value), "login_cooking_border_invalid")


def test_a_changed_border_still_loads_but_needs_an_explicit_migration():
    state = seeded()
    loaded = initialize_cooking(state, border=200)
    assert loaded == state and loaded is not state
    failure = fail(state, lambda: sync_cooking(state, NOW, border=200), RULES_CHANGED)
    assert failure.status == 503
    fail(state, lambda: advance_cooking(state, NOW, border=200, eligible=True), RULES_CHANGED)


def test_the_current_reader_accepts_only_the_running_border():
    state = seeded()
    ledger = read_current_cooking(state, border=BORDER)
    assert ledger == read_cooking(state) and ledger is not state[LEDGER_KEY]
    assert fail(state, lambda: read_current_cooking(state, border=200), RULES_CHANGED).status == 503
    migrated = migrate_cooking(state, NOW, border=200)
    assert read_current_cooking(migrated, border=200) == read_cooking(migrated)
    fail(migrated, lambda: read_current_cooking(migrated, border=BORDER), RULES_CHANGED)


def test_changed_same_month_previous_spending_cannot_expand_saved_quota():
    state = seeded()
    state["crystal_spending"]["previous_spent"] = 625
    state["tables"]["game_param2"][0]["StampCardGrade"] = 625 << 16
    fail(state, lambda: sync_cooking(state, NOW, border=BORDER), "login_cooking_invalid")


def test_spending_must_be_settled_before_cooking_month_rollover():
    state = seeded()
    fail(state, lambda: sync_cooking(state, datetime(2026, 12, 1, 4, tzinfo=JST), border=BORDER),
         "login_cooking_refresh_required")


@pytest.mark.parametrize("clock_owner", ["login_cooking", "crystal_spending", "login_rewards"])
def test_all_saved_clock_watermarks_reject_backwards_time(clock_owner):
    state = seeded()
    state[clock_owner]["last_time"] += 1000
    fail(state, lambda: sync_cooking(state, NOW, border=BORDER), "login_cooking_clock_backwards")


@pytest.mark.parametrize("now", [None, "2026-11-10", 1, date(2026, 11, 10)])
def test_invalid_clock_is_rejected_without_writes(now):
    state = seeded()
    fail(state, lambda: sync_cooking(state, now, border=BORDER), "login_cooking_clock_invalid")


@pytest.mark.parametrize("moment", OUTSIDE_CALENDAR)
def test_instants_outside_the_client_calendar_are_refused(moment):
    state = seeded()
    for now in representations(moment):
        fail(state, lambda: sync_cooking(state, now, border=BORDER), "login_cooking_clock_invalid")
        fail(state, lambda: migrate_cooking(state, now, border=BORDER), "login_cooking_clock_invalid")
    state[LEDGER_KEY]["last_time"] = epoch_milliseconds(moment)
    fail(state, lambda: read_cooking(state), "login_cooking_invalid")


@pytest.mark.parametrize("value", [None, 0, 1, "yes"])
def test_eligibility_is_strict_boolean(value):
    state = seeded()
    fail(state, lambda: advance_cooking(state, NOW, border=BORDER, eligible=value), "login_cooking_eligible_invalid")


def test_same_instant_utc_shanghai_and_naive_jst_agree():
    state = seeded()
    expected = advance_cooking(state, NOW, border=BORDER, eligible=True)
    for now in (NOW.astimezone(timezone.utc), NOW.astimezone(timezone(timedelta(hours=8))), NOW.replace(tzinfo=None)):
        assert advance_cooking(state, now, border=BORDER, eligible=True) == expected


def test_negative_unix_time_is_supported_without_float_rounding():
    now = datetime(1969, 12, 31, 23, 59, 59, 999900, tzinfo=timezone.utc)
    state = seeded(now=now)
    assert state["login_cooking"]["last_time"] == -1
    updated, yes = advance_cooking(state, now, border=BORDER, eligible=True)
    assert yes and fields(updated)[1] == date(1970, 1, 1).toordinal() - 1


@pytest.mark.parametrize("field,value", [("schema_version", True), ("schema_version", 2), ("quota", -1),
    ("quota", 26), ("issued", True), ("issued", 26), ("period", "2026-13"), ("period", "2027-01"),
    ("last_time", True), ("last_time", 2**63), ("baseline_source", "unknown"), ("border", -1),
    ("last_claim_day", "2026-11-10"), ("last_civil_day", "2026-11-10")])
def test_corrupt_ledger_is_not_overwritten(field, value):
    state = seeded()
    state["login_cooking"][field] = value
    fail(state, lambda: initialize_cooking(state, border=BORDER), "login_cooking_invalid")


@pytest.mark.parametrize("field,value", [("HomeCookingReceivedAmount", True), ("HomeCookingReceivedAmount", -1),
    ("HomeCookingReceivedAmount", 26), ("HomeCookingReceivedAmount", 1),
    ("HomeCookingReceivedDate", -1), ("HomeCookingReceivedDate", 739900), ("HomeCookingReceivedDate", False)])
def test_native_projection_corruption_is_rejected(field, value):
    state = seeded()
    state["tables"]["game_param2"][0][field] = value
    fail(state, lambda: read_cooking(state), "login_cooking_invalid")


def test_reader_and_returned_states_are_independent():
    state = seeded()
    ledger = read_cooking(state)
    ledger["issued"] = 12
    updated, yes = advance_cooking(state, NOW, border=BORDER, eligible=True)
    assert yes and state["login_cooking"]["issued"] == 0
    updated["tables"]["presents"][0]["saved"].append(3)
    assert state["tables"]["presents"][0]["saved"] == [1]
