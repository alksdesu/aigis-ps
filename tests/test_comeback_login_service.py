from copy import copy, deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container
from aigis_ps.comeback_login_catalog import ComebackLoginCatalog
from aigis_ps.comeback_login_rules import ComebackLoginRules
from aigis_ps.comeback_login_service import ComebackLoginService, combine_event_login_notifications
from aigis_ps.comeback_login_state import DATE_FIELD, DAYS_FIELD, read_comeback_logins
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.event_login_catalog import EventLoginCatalog, RESOURCE_NAME
from aigis_ps.possessions import STORED_ITEM_NAME_BYTES
from aigis_ps.table_cell_patch import patch_table_cells
from test_event_login_service import assert_gift_box_receipts_replay
from test_event_pass_service import accept_stored_item, item_catalogs
from test_quest_service import bundle


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


@pytest.fixture(scope="module")
def source(bundle):
    raw = bundle.native_cdn.assets.read(RESOURCE_NAME)[0]
    catalog = ComebackLoginCatalog(EventLoginCatalog(lambda _: raw))
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    state = bundle.initialize(bootstrap.initial_state())
    state["tables"]["game_param2"][0]["ComebackLoginDateTime"] = "2024/01/01 00:00:00"
    state["tables"]["game_param2"][0]["LatestComebackSummonBonusDateTime"] = "2025/01/01 00:00:00"
    state["tables"]["game_param2"][0]["LatestComebackSummonBonusDays"] = 365
    return raw, catalog, state


@pytest.fixture
def factory(bundle, source):
    raw, catalog, state = source
    def make(*, gap=30, minimum=30, enabled=True, updates=None):
        actual = ComebackLoginCatalog(EventLoginCatalog(lambda _: patch_table_cells(raw, updates))) if updates else catalog
        presents = copy(bundle.presents)
        service = ComebackLoginService(actual, presents, ComebackLoginRules(enabled=enabled, min_absence_days=minimum))
        initial = deepcopy(state)
        baseline = service.observe(initial, NOW - timedelta(days=gap))
        current = service.observe(baseline, NOW)
        return SimpleNamespace(service=service, catalog=actual, presents=presents,
                               initial=initial, baseline=baseline, state=current)
    return make


def reject(case, action, code):
    before = deepcopy(case.state)
    with pytest.raises(ProtocolError) as caught:
        action()
    assert caught.value.code == code
    assert case.state == before


def round_record(state, number=1):
    return state["comeback_login"]["rounds"][str(number)]


def test_first_observation_and_settlement_cannot_invent_a_return(factory):
    case = factory(gap=0)
    assert read_comeback_logins(case.state)["rounds"] == {}
    assert case.service.settle(case.state, NOW) == (case.state, [])
    assert case.service.settle(case.initial, NOW) == (case.initial, [])
    assert case.service.initialize(case.initial) == case.initial


@pytest.mark.parametrize("gap,band", [(1,None), (29,None), (30,30), (59,30), (60,60),
    (119,90), (120,120), (180,180), (365,365), (500,365)])
def test_real_absence_thresholds_select_largest_source_band(factory, gap, band):
    case = factory(gap=gap)
    rounds = read_comeback_logins(case.state)["rounds"]
    if band is None:
        assert rounds == {}
    else:
        assert rounds["1"]["pause_period"] == band
        assert rounds["1"]["absence_days"] == gap
        assert rounds["1"]["end_day"] == "2026-11-21"
        assert len(rounds["1"]["reward_definitions"]) == case.catalog.bands[band].max_ordinal


def test_real_first_reward_projects_gap_and_not_reward_ordinal(factory):
    case = factory(gap=60)
    result, grants = case.service.settle(case.state, NOW)
    grant, = grants
    assert grant["round_id"] == 1 and grant["login_count"] == 1 and grant["notify"] is True
    assert grant["campaign_id"] == 6 and grant["mail_ids"] == [1]
    expected = case.catalog.bands[60].rewards_for(1)[0]
    assert case.presents.rows(result) == [{"PresentID": 1, "Type": 0,
        "Content": expected.item_param, "Description": "カムバックログインボーナス"}]
    assert case.service.pgp2_pairs(result) == ((DATE_FIELD, "2026/11/01 12:00:00"), (DAYS_FIELD, "60"))
    assert case.service.response(grants) == Container("EBNS", (Column("LoginBonusID", "I", (6,)),
        Column("LoginCount", "I", (1,)), Column("PurchaseTime", "S", ("",))))
    for name in ("ComebackLoginDateTime", "LatestComebackSummonBonusDateTime", "LatestComebackSummonBonusDays"):
        assert result["tables"]["game_param2"][0][name] == case.initial["tables"]["game_param2"][0][name]


def test_every_low_band_day_is_once_and_skipped_days_do_not_backfill(factory):
    case = factory(gap=30)
    result = case.state
    for number in range(1, 11):
        now = NOW + timedelta(days=number - 1)
        result, grants = case.service.settle(result, now)
        assert grants[0]["login_count"] == number
        assert case.service.settle(result, now + timedelta(seconds=1)) == (result, [])
    assert case.service.settle(result, NOW + timedelta(days=11)) == (result, [])
    assert len(case.presents.rows(result)) == 10
    assert result["tables"]["crystals"] == case.initial["tables"]["crystals"]


def test_repeated_observation_cannot_reset_active_or_completed_round(factory):
    case = factory(gap=1, minimum=1)
    current = case.state
    for number in range(10):
        now = NOW + timedelta(days=number)
        current = case.service.observe(current, now)
        current, _ = case.service.settle(current, now)
    current = case.service.observe(current, NOW + timedelta(days=11))
    assert list(read_comeback_logins(current)["rounds"]) == ["1"]
    assert round_record(current)["started_at"] == round_record(case.state)["started_at"]


def test_same_day_observation_leaves_state_unchanged_until_the_next_logical_day(factory):
    case = factory(gap=1, minimum=1)
    disabled = factory(enabled=False).service
    closing = datetime(2026, 11, 2, 3, 59, 59, tzinfo=JST)
    for later in (NOW + timedelta(seconds=1), closing):
        assert case.service.observe(case.state, later) == case.state
        assert disabled.observe(case.state, later) == case.state
    following = case.service.observe(case.state, closing + timedelta(seconds=1))
    assert following["comeback_login"]["last_seen_day"] == "2026-11-02"
    assert following["comeback_login"]["rounds"] == case.state["comeback_login"]["rounds"]


def test_expired_without_pending_does_not_award_missed_days(factory):
    case = factory()
    result, grants = case.service.settle(case.state, NOW + timedelta(days=20))
    assert result == case.state and not grants


def test_old_round_pending_new_return_and_global_daily_limit(factory, monkeypatch):
    case = factory()
    original = case.presents.enqueue_groups
    def no_space(*_):
        raise ProtocolError("present_capacity", "No room", 409)
    monkeypatch.setattr(case.presents, "enqueue_groups", no_space)
    pending, grants = case.service.settle(case.state, NOW)
    assert grants == [] and round_record(pending)["pending"]["reason"] == "present_capacity"
    later = NOW + timedelta(days=30)
    current = case.service.observe(pending, later)
    assert list(read_comeback_logins(current)["rounds"]) == ["1", "2"]
    monkeypatch.setattr(case.presents, "enqueue_groups", original)
    result, grants = case.service.settle(current, later)
    assert grants[0]["round_id"] == 1 and grants[0]["notify"] is False
    assert case.service.response(grants).children[0].values == ()
    assert round_record(result,2)["deliveries"] == {}
    assert case.service.settle(result, later + timedelta(seconds=1)) == (result, [])
    result, grants = case.service.settle(result, later + timedelta(days=1))
    assert grants[0]["round_id"] == 2 and grants[0]["notify"] is True
    assert case.service.pgp2_pairs(result)[0][1] == "2026/12/01 12:00:00"


def test_old_nonzero_native_projection_is_preserved_without_backfill(factory):
    case = factory(gap=0)
    old = deepcopy(case.initial)
    old["tables"]["game_param2"][0].update({DATE_FIELD:"2023/02/03 04:05:06", DAYS_FIELD:365})
    observed = case.service.observe(old, NOW)
    assert observed["tables"] == old["tables"]
    assert case.service.settle(observed, NOW) == (observed, [])


@pytest.mark.parametrize("code,status", [("present_capacity",409), ("item_summon_pool_disabled",501),
    ("login_reward_response_capacity",503)])
def test_recoverable_error_keeps_one_earned_ordinal(factory, monkeypatch, code, status):
    case = factory()
    def unavailable(*_):
        raise ProtocolError(code, "Unavailable", status)
    monkeypatch.setattr(case.presents, "enqueue_groups", unavailable)
    result, grants = case.service.settle(case.state, NOW)
    assert not grants and case.presents.rows(result) == []
    assert round_record(result)["pending"]["ordinal"] == 1
    again, _ = case.service.settle(result, NOW + timedelta(days=2))
    assert round_record(again)["pending"]["earned_day"] == "2026-11-01"


def test_fatal_and_mutating_callback_roll_back_candidate(factory):
    case = factory()
    def fatal(*_):
        raise ProtocolError("broken_state", "Broken", 503)
    reject(case, lambda: case.service.settle(case.state, NOW, fatal), "broken_state")
    def mutation(candidate, _):
        candidate["tampered"] = True
    reject(case, lambda: case.service.settle(case.state, NOW, mutation), "comeback_login_validation_invalid")


def test_reply_capacity_pending_can_be_retried_without_observing(factory):
    case = factory()
    def oversized(*_):
        raise ProtocolError("rank_reward_response_capacity", "Too big", 503)
    result, grants = case.service.settle(case.state, NOW, oversized)
    assert not grants
    later, grants = case.service.settle(result, NOW + timedelta(days=22))
    assert grants[0]["earned_day"] == "2026-11-01"
    assert later["comeback_login"]["last_seen_day"] == "2026-11-01"


def test_disabled_service_preserves_rewards_and_does_not_create_baseline(factory):
    active = factory()
    state, _ = active.service.settle(active.state, NOW)
    disabled = factory(enabled=False)
    assert disabled.service.observe(disabled.initial, NOW + timedelta(days=90)) == disabled.initial
    assert disabled.service.settle(state, None) == (state, [])


def test_disabled_observations_prevent_false_absence_after_reenable(factory):
    active = factory(gap=0)
    disabled = factory(enabled=False)
    observed = active.state
    for day in (10, 20, 30, 40):
        observed = disabled.service.observe(observed, NOW + timedelta(days=day))
        assert observed["comeback_login"]["rounds"] == {}
    resumed = active.service.observe(observed, NOW + timedelta(days=41))
    assert resumed["comeback_login"]["rounds"] == {}
    assert resumed["comeback_login"]["last_seen_day"] == "2026-12-12"
    assert active.service.settle(resumed, NOW + timedelta(days=41)) == (resumed, [])


def test_disabled_observation_does_not_reset_current_round(factory):
    active = factory()
    current, _ = active.service.settle(active.state, NOW)
    disabled = factory(enabled=False)
    observed = disabled.service.observe(current, NOW + timedelta(days=5))
    assert observed["comeback_login"]["rounds"] == current["comeback_login"]["rounds"]
    assert observed["comeback_login"]["current_round_id"] == 1
    assert observed["tables"] == current["tables"]
    assert observed["comeback_login"]["last_seen_day"] == "2026-11-06"
    assert disabled.service.settle(observed, NOW + timedelta(days=5)) == (observed, [])


def test_changed_reward_preserves_pending_and_old_history(factory, monkeypatch, source):
    case = factory()
    original = case.presents.enqueue_groups
    monkeypatch.setattr(case.presents, "enqueue_groups", lambda *_: (_ for _ in ()).throw(
        ProtocolError("present_capacity", "No room",409)))
    pending, _ = case.service.settle(case.state, NOW)
    first = case.catalog.bands[30].rewards_for(1)[0].source_index
    changed = factory(updates={first:{"ItemParam":999}})
    result, grants = changed.service.settle(pending, NOW)
    assert not grants and round_record(result)["pending"]["reason"] == "comeback_login_definition_changed"
    monkeypatch.setattr(case.presents, "enqueue_groups", original)
    delivered, grants = case.service.settle(pending, NOW)
    changed.service.validate_receipt(delivered, grants, verify_current=False)
    reject(changed, lambda: changed.service.validate_receipt(delivered, grants), "comeback_login_receipt_invalid")


def test_item_beyond_the_native_cell_stays_pending_instead_of_failing_115(factory, source, monkeypatch):
    item = "x" * STORED_ITEM_NAME_BYTES
    first = source[1].bands[30].rewards_for(1)[0].source_index
    case = factory(updates={first: {"ItemType": 22, "ItemParam": 1, "ItemID": item}})
    accept_stored_item(monkeypatch, case.presents, item)
    result, grants = case.service.settle(case.state, NOW)
    assert not grants and not case.presents.rows(result)
    assert round_record(result)["pending"]["reason"] == "comeback_login_reward_unsupported"


@pytest.mark.parametrize("mutation", ["notify", "mail"])
def test_receipt_checks_current_context_and_reward_content(factory, mutation):
    case = factory()
    state, grants = case.service.settle(case.state, NOW)
    if mutation == "notify":
        grants[0]["notify"] = False
        round_record(state)["deliveries"]["1"]["notify"] = False
    else:
        state["tables"]["presents"][0]["Content"] += 1
    reject(case, lambda: case.service.validate_receipt(state, grants), "comeback_login_receipt_invalid")


def test_notification_merge_preserves_sources_and_rejects_duplicates(factory):
    case = factory()
    _, grants = case.service.settle(case.state, NOW)
    public = {key:value for key,value in grants[0].items() if key not in ("round_id","notify")}
    public.update(campaign_id=135,mail_ids=[2])
    result = combine_event_login_notifications([public],grants)
    assert result.children[0].values == (6,135)
    public["mail_ids"]=[1]
    reject(case,lambda:combine_event_login_notifications([public],grants),"login_notification_duplicate_mail")
    public.update(campaign_id=6,mail_ids=[2])
    reject(case,lambda:combine_event_login_notifications([public],grants),"login_notification_duplicate_campaign")


@pytest.mark.parametrize("hour,minute", [(0,0), (3,59), (4,0)])
def test_return_civil_date_anchors_expiration_while_claims_use_four_am(factory, hour, minute):
    case = factory()
    returned = NOW.replace(hour=hour, minute=minute)
    baseline = case.service.observe(case.initial, returned - timedelta(days=30))
    current = case.service.observe(baseline, returned)
    record = round_record(current)
    assert record["end_day"] == "2026-11-21"
    assert record["start_day"] == ("2026-10-31" if hour < 4 else "2026-11-01")
    assert case.service.pgp2_pairs(current)[0][1] == returned.strftime("%Y/%m/%d %H:%M:%S")
    last_day = datetime(2026, 11, 21, 3, 59, 59, tzinfo=JST)
    result, grants = case.service.settle(current, last_day)
    assert grants[0]["login_count"] == 1
    assert case.service.settle(result, last_day + timedelta(seconds=1)) == (result, [])


def test_stored_round_deadline_is_not_recomputed_under_new_anchor(factory):
    case = factory()
    returned = NOW.replace(hour=2)
    baseline = case.service.observe(case.initial, returned - timedelta(days=30))
    old = case.service.observe(baseline, returned)
    round_record(old)["end_day"] = "2026-11-20"
    before = deepcopy(old)
    assert case.service.initialize(old) == before
    observed = case.service.observe(old, returned + timedelta(days=1))
    assert round_record(observed)["end_day"] == "2026-11-20"
    deadline = datetime(2026, 11, 20, 4, tzinfo=JST)
    assert case.service.settle(observed, deadline) == (observed, [])
    assert old == before


def test_receipts_of_every_gift_box_configuration_replay_and_others_stay_stale(factory, item_catalogs):
    case = factory()
    service = assert_gift_box_receipts_replay(
        lambda presents: ComebackLoginService(case.catalog, presents, case.service.rules),
        case.presents, item_catalogs, "comeback_login_dependencies_invalid")
    assert factory(minimum=60).service.content_version not in service.replayable_content_versions()
