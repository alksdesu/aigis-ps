from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.mission_catalog import MISSION_FIELDS, SEQUENCE_FIELDS, MissionCatalog
from aigis_ps.mission_state import MissionProgress
import aigis_ps.mission_state as module
from aigis_ps.player_state import UNIT_BOOLEANS, UNIT_FIELDS
from aigis_ps.quest_clear_spec import QuestClearSpec, QuestClearSpecs, TeamRule
from test_mission_catalog import current, mission, native_date, sequential
from test_trade_catalog import table


NOW = datetime(2026, 11, 1, 4, tzinfo=JST)
EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)
LAST_MILLISECOND = timedelta(milliseconds=1)
BOUNDARIES = [datetime(2026, 11, 1, 4, tzinfo=JST) - LAST_MILLISECOND, datetime(2026, 11, 1, 4, tzinfo=JST),
              datetime(2027, 1, 1, 4, tzinfo=JST) - LAST_MILLISECOND, datetime(2027, 1, 1, 4, tzinfo=JST),
              datetime(2028, 3, 1, 4, tzinfo=JST) - LAST_MILLISECOND, datetime(2028, 3, 1, 4, tzinfo=JST)]


def catalog(rows, *, sequence=None, version="fixture"):
    rows = list(rows)
    if sequence is None:
        rows.append(mission(10001, PeriodType=100, ConditionType=15, DispTabType=0))
        sequence = [10001]
    payloads = {"GloryConditionConfig.atb": table(rows, MISSION_FIELDS),
                "ProgressiveBeginnerMissionConfig.atb": table(
                    [sequential(identifier, index + 1) for index, identifier in enumerate(sequence)], SEQUENCE_FIELDS)}
    return MissionCatalog(payloads.__getitem__, resource_version=version)


def progress(*rows, sequence=None):
    return MissionProgress(catalog(rows, sequence=sequence))


def saved():
    return {"unrelated": {"keep": [1, 2]}, "player_state": {"units": [{"Love": 150, "Exp": 999999}]}}


def unit(uid=10, **changes):
    row = {name: False if name in UNIT_BOOLEANS else 0 for name, _ in UNIT_FIELDS}
    row.update(CardID=7, ClassID=100, UniqueID=uid, GetIndex=uid - 1, SkillLevel=1, SettingSkillID=1)
    row.update(changes)
    return row


def training(uid=10, *, level=1, **changes):
    return {"before": unit(uid), "after": unit(uid, **changes), "level": level}


def gift(uid=10, before=0, after=1):
    return {"before": unit(uid, Love=before), "after": unit(uid, Love=after)}


def evolve(uid=10, *, operation=7, before=0, after=2, skilled_before=0, skilled_after=0):
    return {"before": unit(uid, ClassChangeCount=before, SkillEvolved=skilled_before),
            "after": unit(uid, ClassChangeCount=after, SkillEvolved=skilled_after), "operation": operation}


def quest(**changes):
    data = {"quest_id": 1108, "quest_type": 0, "stars": 3, "hard": False}
    data.update(changes)
    return data


def rows(engine, state, now=NOW):
    return {row["ID"]: row for row in engine.snapshot(state, now)}


def fail(code, operation):
    with pytest.raises(ProtocolError) as error:
        operation()
    assert error.value.code == code


def test_initialize_is_independent_and_never_backfills_owned_growth():
    engine = progress(mission(1, ConditionType=4))
    original = saved()
    initialized = engine.initialize(original)
    assert "mission_state" not in original
    assert initialized["mission_state"] == {"schema_version": 1, "entries": [], "last_time": None, "last_login_day": None}
    assert engine.initialize(initialized) == initialized
    assert all(row["Status"] == row["Progress"] == 0 for row in engine.snapshot(initialized, NOW))
    initialized["unrelated"]["keep"].append(3)
    assert original["unrelated"]["keep"] == [1, 2]


def test_daily_and_monthly_roll_over_at_jst_four_and_keep_claimed_history():
    engine = progress(mission(1, ConditionType=1),
                      mission(2, ConditionType=1, PeriodType=2, DispTabType=2),
                      mission(3, ConditionType=9, PeriodType=4, DispTabType=4))
    before_boundary = NOW - timedelta(seconds=1)
    first = engine.record(saved(), "login", {}, before_boundary)
    first = engine.claim(first, [1, 2, 3], before_boundary)
    assert all(rows(engine, first, before_boundary)[key]["Status"] == 2 for key in (1, 2, 3))
    second = engine.record(first, "login", {}, NOW)
    snapshot = rows(engine, second)
    assert snapshot[1]["Status"] == snapshot[2]["Status"] == 1
    assert snapshot[3]["Status"] == 2
    history = second["mission_state"]["entries"]
    assert {entry["period"] for entry in history if entry["condition_id"] == 1} == {"day:2026-10-31", "day:2026-11-01"}
    assert {entry["period"] for entry in history if entry["condition_id"] == 2} == {"month:2026-10", "month:2026-11"}
    assert sum(entry["status"] == 2 for entry in history) == 3


@pytest.mark.parametrize("offset", [-7, 0, 8, 9, 14])
def test_same_instant_timezone_and_naive_jst_share_the_period(offset):
    engine = progress(mission(1, ConditionType=1))
    instant = NOW.astimezone(timezone(timedelta(hours=offset)))
    first = engine.record(saved(), "login", {}, instant)
    assert engine.record(first, "login", {}, NOW.replace(tzinfo=None)) == first
    assert first["mission_state"]["last_login_day"] == "2026-11-01"


def test_same_day_login_does_not_advance_watermark_or_duplicate_monthly_count():
    engine = progress(mission(1, ConditionType=1, PeriodType=2, DispTabType=2, ClearCountCondition=3))
    first = engine.record(saved(), "login", {}, NOW)
    assert engine.record(first, "login", {}, NOW + timedelta(hours=23)) == first
    second = engine.record(first, "login", {}, NOW + timedelta(days=1))
    assert rows(engine, second, NOW + timedelta(days=1))[1]["Progress"] == 2
    fail("mission_clock_backwards", lambda: engine.record(second, "login", {}, NOW))
    fail("mission_clock_backwards", lambda: engine.claim(second, [1], NOW))
    assert rows(engine, second, NOW + timedelta(days=1))[1]["Status"] == 0


def test_gift_uses_positive_delta_and_distinct_unique_ids_then_stops_growing():
    engine = progress(mission(1, ConditionType=5, ClearCountCondition=2))
    initial = engine.initialize(saved())
    assert engine.record(initial, "gift", gift(before=100, after=100), NOW) == initial
    first = engine.record(initial, "gift", gift(), NOW)
    assert engine.record(first, "gift", gift(before=1, after=5), NOW) == first
    second = engine.record(first, "gift", gift(11), NOW)
    assert rows(engine, second)[1]["Progress"] == 2 and rows(engine, second)[1]["Status"] == 1
    assert engine.record(second, "gift", gift(12), NOW + timedelta(seconds=1)) == second
    assert second["mission_state"]["entries"][0]["uids"] == [10, 11]
    assert initial["mission_state"]["entries"] == []


def test_training_counts_transactions_and_sequential_checks_only_current_item():
    definitions = [mission(1, ConditionType=4, ClearCountCondition=9)]
    definitions += [mission(identifier, PeriodType=100, ConditionType=kind, DispTabType=0, BorderValue=border)
                    for identifier, kind, border in ((10001, 12, 60), (10002, 13, 5), (10003, 14, 1), (10004, 15, 0))]
    engine = MissionProgress(catalog(definitions, sequence=[10001, 10002, 10003, 10004]))
    event = training(level=60, SkillLevel=5, CostModMixValue=-1)
    first = engine.record(saved(), "training", event, NOW)
    assert rows(engine, first)[10001]["Status"] == 1
    assert rows(engine, first)[10002]["Status"] == rows(engine, first)[10003]["Status"] == 0
    fail("mission_sequence_locked", lambda: engine.claim(first, [10002], NOW))
    first = engine.claim(first, [10001], NOW)
    assert rows(engine, first)[10002]["Progress"] == 0
    second = engine.record(first, "training", event, NOW)
    second = engine.claim(second, [10002], NOW)
    assert rows(engine, second)[10003]["Progress"] == 0
    third = engine.record(second, "training", event, NOW)
    assert rows(engine, third)[1]["Progress"] == 3
    third = engine.claim(third, [10003], NOW)
    assert rows(engine, third)[10004]["Progress"] == 0
    final = engine.record(third, "evolution", evolve(operation=8, before=2, after=2, skilled_after=1), NOW)
    assert rows(engine, final)[10004]["Status"] == 1


def test_evolution_requires_real_crossings_and_both_second_awakening_branches():
    engine = progress(mission(1, ConditionType=7, ClearCountCondition=2),
                      mission(2, ConditionType=8, ClearCountCondition=2))
    first = engine.record(saved(), "evolution", evolve(), NOW)
    assert engine.record(first, "evolution", evolve(), NOW) == first
    first = engine.record(first, "evolution", evolve(11), NOW)
    assert rows(engine, first)[1]["Status"] == 1
    first = engine.record(first, "evolution", evolve(operation=9, before=2, after=3), NOW)
    assert (rows(engine, first)[2]["Progress"], rows(engine, first)[2]["Status"]) == (1, 0)
    assert engine.record(first, "evolution", evolve(12, operation=9, before=3, after=4), NOW) == first
    first = engine.record(first, "evolution", evolve(11, operation=9, before=2, after=4), NOW)
    assert rows(engine, first)[2]["Status"] == 1
    untouched = engine.initialize(saved())
    assert engine.record(untouched, "evolution", evolve(operation=7, before=2, after=2), NOW) == untouched


@pytest.mark.parametrize("restriction", [dict(MemberCountCondition=3), dict(KindCondition=0),
    dict(RarityCondition=2), dict(RaceCondition=1), dict(GenusCondition=1), dict(AssignCondition=1),
    dict(IdentityCondition=1), dict(BloodCondition=1), dict(HardClearRankCondition=3), dict(QuestType=15)])
def test_quest_conditions_requiring_missing_evidence_never_count(restriction):
    engine = progress(mission(1, **restriction))
    initial = engine.initialize(saved())
    assert engine.record(initial, "quest_clear", quest(), NOW) == initial


def test_quest_id_type_and_stars_filter_independently():
    engine = progress(mission(1, QuestID=1108, ClearRankCondition=3), mission(2, ClearCountCondition=3),
                      mission(3, QuestType=0, ClearCountCondition=3), mission(4, QuestType=15))

    def progressions(state):
        return [rows(engine, state)[identifier]["Progress"] for identifier in (1, 2, 3, 4)]

    first = engine.record(saved(), "quest_clear", quest(stars=2), NOW)
    assert progressions(first) == [0, 1, 1, 0]
    second = engine.record(first, "quest_clear", quest(quest_id=1208, hard=True, stars=4), NOW)
    assert progressions(second) == [0, 2, 2, 0]
    third = engine.record(second, "quest_clear", quest(quest_id=910001, quest_type=15), NOW)
    assert progressions(third) == [0, 3, 2, 0] and rows(engine, third)[2]["Status"] == 1
    final = engine.record(third, "quest_clear", quest(), NOW)
    assert rows(engine, final)[1]["Status"] == rows(engine, final)[3]["Status"] == 1
    assert rows(engine, final)[4]["Progress"] == 0


def test_beginner_step_without_quest_id_waits_for_the_mode_policy_that_owns_its_text(monkeypatch):
    engine = MissionProgress(catalog([mission(10001, PeriodType=100, DispTabType=0, QuestID=3007),
                                      mission(10002, PeriodType=100, DispTabType=0)], sequence=[10001, 10002]))
    first = engine.record(saved(), "quest_clear", quest(quest_id=3007, quest_type=4), NOW)
    claimed = engine.claim(first, [10001], NOW)
    for event in (quest(), quest(hard=True, stars=4), quest(quest_id=101, quest_type=2)):
        assert engine.record(claimed, "quest_clear", event, NOW) == claimed

    class DailyBeginner:
        @staticmethod
        def mission_matcher(definition, payload, spec):
            if definition.condition_id == 10002 and payload["quest_id"] == 101:
                return True
            return None

    monkeypatch.setattr(module.quest_modes, "policy", lambda quest_type: DailyBeginner)
    assert engine.record(claimed, "quest_clear", quest(), NOW) == claimed
    assert rows(engine, engine.record(claimed, "quest_clear", quest(quest_id=101, quest_type=2), NOW))[10002]["Status"] == 1


def test_zero_star_outcome_cannot_be_recorded_as_a_successful_quest():
    engine = progress(mission(1))
    initial = engine.initialize(saved())
    fail("mission_event_invalid", lambda: engine.record(initial, "quest_clear", quest(stars=0), NOW))
    assert rows(engine, initial)[1]["Progress"] == 0


def test_daily_aggregate_needs_all_ready_but_not_claimed():
    engine = progress(mission(1, ConditionType=4), mission(2, ConditionType=5),
                      mission(3, ConditionType=1), mission(4, ConditionType=3))
    first = engine.record(saved(), "training", training(), NOW)
    first = engine.record(first, "gift", gift(), NOW)
    assert rows(engine, first)[4]["Status"] == 0
    first = engine.record(first, "login", {}, NOW)
    assert rows(engine, first)[4]["Status"] == 1
    claimed = engine.claim(first, [4], NOW)
    assert rows(engine, claimed)[4]["Status"] == 2
    assert all(rows(engine, claimed)[identifier]["Status"] == 1 for identifier in (1, 2, 3))


def test_attribute_aggregate_uses_catalog_group_dependencies():
    engine = progress(mission(1, PeriodType=4, ConditionType=4, DispTabType=6, DisplayType=1, GroupType=1),
                      mission(2, PeriodType=4, ConditionType=17, DispTabType=6, DisplayType=1, GroupType=1))
    first = engine.record(saved(), "training", training(), NOW)
    assert rows(engine, first)[2]["Status"] == 1


def test_claim_is_atomic_and_read_is_independent_and_idempotent():
    engine = progress(mission(1, ConditionType=4), mission(2, ConditionType=5))
    first = engine.record(saved(), "training", training(), NOW)
    before = deepcopy(first)
    fail("mission_not_ready", lambda: engine.claim(first, [1, 2], NOW))
    assert first == before
    read = engine.mark_read(first, [1, 2], NOW)
    assert rows(engine, read)[1] == {"ID": 1, "Status": 1, "Read": 1, "Progress": 1}
    assert rows(engine, read)[2]["Read"] == 0
    assert engine.mark_read(read, [1, 2], NOW + timedelta(seconds=1)) == read
    claimed = engine.claim(read, [1], NOW)
    assert rows(engine, claimed)[1]["Status"] == 2 and rows(engine, claimed)[1]["Read"] == 1
    fail("mission_not_ready", lambda: engine.claim(claimed, [1], NOW))
    fail("mission_inactive", lambda: engine.claim(claimed, [999], NOW))


def test_read_marks_claimed_unread_missions_but_never_unfinished_ones():
    engine = progress(mission(1, ConditionType=4), mission(2, ConditionType=4), mission(3, ConditionType=5))
    claimed = engine.claim(engine.record(saved(), "training", training(), NOW), [1], NOW)
    assert rows(engine, claimed)[1] == {"ID": 1, "Status": 2, "Read": 0, "Progress": 1}
    read = engine.mark_read(claimed, [1, 2, 3], NOW)
    assert {key: (row["Status"], row["Read"]) for key, row in rows(engine, read).items() if key in (1, 2, 3)} == {
        1: (2, 1), 2: (1, 1), 3: (0, 0)}
    assert engine.initialize(read) == read
    assert engine.mark_read(read, [1, 2, 3], NOW + timedelta(seconds=1)) == read


def test_expired_definitions_leave_history_without_being_claimable():
    engine = progress(mission(1, ConditionType=4))
    first = engine.record(saved(), "training", training(), NOW)
    inactive = progress(mission(2, ConditionType=4))
    assert 1 not in rows(inactive, first)
    assert inactive.initialize(first) == first
    fail("mission_inactive", lambda: inactive.claim(first, [1], NOW))


def test_definition_versions_do_not_reopen_claimed_rewards_or_rewrite_unclaimed_rules():
    old = progress(mission(1, ConditionType=4))
    ready = old.record(saved(), "training", training(), NOW)
    claimed = old.claim(ready, [1], NOW)
    changed = progress(mission(1, ConditionType=4, RewardItemParam=10))
    fail("mission_definition_changed", lambda: changed.snapshot(ready, NOW))
    assert rows(changed, claimed)[1]["Status"] == 2
    fail("mission_not_ready", lambda: changed.claim(claimed, [1], NOW))
    next_day = changed.record(claimed, "training", training(), NOW + timedelta(days=1))
    assert rows(changed, next_day, NOW + timedelta(days=1))[1]["Status"] == 1
    assert len([row for row in next_day["mission_state"]["entries"] if row["condition_id"] == 1]) == 2


def test_unknown_period_changes_cannot_create_an_extra_reward():
    old = progress(mission(1, ConditionType=4, PeriodType=4, DispTabType=4))
    state = old.claim(old.record(saved(), "training", training(), NOW), [1], NOW)
    changed = progress(mission(1, ConditionType=4))
    fail("mission_definition_changed", lambda: changed.snapshot(state, NOW))


def test_unimplemented_hall_and_skin_conditions_never_auto_complete():
    engine = progress(mission(1, PeriodType=4, ConditionType=10, InHallofFameSum=1, ClearCountCondition=0),
                      mission(2, PeriodType=4, ConditionType=11, PurchasedSkinSum=1, ClearCountCondition=0),
                      mission(3, PeriodType=4, ConditionType=16, InHallofFameSupportSum=1, ClearCountCondition=0),
                      mission(4, PeriodType=6, ConditionType=1))
    state = engine.record(saved(), "login", {}, NOW)
    state = engine.record(state, "training", training(level=99), NOW)
    snapshot = rows(engine, state)
    assert 4 not in snapshot and all(snapshot[key]["Status"] == snapshot[key]["Progress"] == 0 for key in (1, 2, 3))
    assert engine.status()["unsupported_types"] == [10, 11, 16]


@pytest.mark.parametrize("change", [lambda s: s.update(mission_state=None),
    lambda s: s["mission_state"].update(schema_version=True),
    lambda s: s["mission_state"].update(last_time=True),
    lambda s: s["mission_state"].update(last_login_day="9999-01-01"),
    lambda s: s["mission_state"]["entries"].append(deepcopy(s["mission_state"]["entries"][0])),
    lambda s: s["mission_state"]["entries"][0].update(goal=0),
    lambda s: s["mission_state"]["entries"][0].update(progress=True),
    lambda s: s["mission_state"]["entries"][0].update(status=3),
    lambda s: s["mission_state"]["entries"][0].update(read=1),
    lambda s: s["mission_state"]["entries"][0].update(period="day:2026-99-99"),
    lambda s: s["mission_state"]["entries"][0].update(period="day:2026-12-01"),
    lambda s: s["mission_state"]["entries"][0].update(uids=[10, 10]),
    lambda s: s["mission_state"]["entries"][0].update(definition_version="bad"),
    lambda s: s["mission_state"].update(unrecognized=1)])
def test_corrupt_state_is_rejected_without_repair_or_mutation(change):
    engine = progress(mission(1, ConditionType=4))
    state = engine.record(saved(), "training", training(), NOW)
    change(state)
    before = deepcopy(state)
    fail("mission_state_invalid", lambda: engine.initialize(state))
    assert state == before


@pytest.mark.parametrize("payload", [{}, {"before": unit(), "after": unit(), "level": True},
    {"before": unit(), "after": unit(11), "level": 1},
    {"before": unit(), "after": unit(), "level": 1, "amount": 100}])
def test_invalid_events_cannot_forge_counts(payload):
    engine = progress(mission(1, ConditionType=4))
    state = saved()
    before = deepcopy(state)
    fail("mission_event_invalid", lambda: engine.record(state, "training", payload, NOW))
    assert state == before


@pytest.mark.parametrize("value", [None, True, 1, "2026-11-01"])
def test_clock_requires_a_datetime_without_mutating_state(value):
    engine = progress(mission(1, ConditionType=1))
    original = saved()
    before = deepcopy(original)
    fail("mission_clock_invalid", lambda: engine.record(original, "login", {}, value))
    assert original == before


def milliseconds(moment):
    return (moment - EPOCH) // LAST_MILLISECOND


def shifted_period(period_type, now):
    local = now.replace(tzinfo=JST) if now.tzinfo is None else now.astimezone(JST)
    shifted = local - timedelta(hours=4)
    if period_type == module.DAILY_PERIOD:
        return "day:" + shifted.date().isoformat()
    if period_type == module.MONTHLY_PERIOD:
        return "month:" + shifted.strftime("%Y-%m")
    return "permanent"


@pytest.mark.parametrize("moment", BOUNDARIES)
@pytest.mark.parametrize("offset", [None, -8, 0, 8])
@pytest.mark.parametrize("period_type", sorted(module.PERIOD_TYPES))
def test_period_keys_match_the_jst_four_formula_across_day_and_month_boundaries(moment, offset, period_type):
    now = moment.replace(tzinfo=None) if offset is None else moment.astimezone(timezone(timedelta(hours=offset)))
    local, timestamp, day = module._clock(now)
    assert (local, timestamp) == (moment, milliseconds(moment))
    assert day == (moment - timedelta(hours=4)).date()
    assert module.period_key(period_type, day) == shifted_period(period_type, now)


def test_period_keys_turn_over_at_jst_four():
    before, after = (module._clock(moment)[2] for moment in BOUNDARIES[2:4])
    assert [module.period_key(module.DAILY_PERIOD, day) for day in (before, after)] == ["day:2026-12-31", "day:2027-01-01"]
    assert [module.period_key(module.MONTHLY_PERIOD, day) for day in (before, after)] == ["month:2026-12", "month:2027-01"]
    assert module.period_key(module.SEQUENTIAL_PERIOD, after) == "permanent"


@pytest.mark.parametrize("moment,valid", [
    (datetime(1970, 1, 1, tzinfo=JST) - LAST_MILLISECOND, False), (datetime(1970, 1, 1, tzinfo=JST), True),
    (NOW, True), (datetime(3001, 2, 1, tzinfo=JST), False)])
def test_watermarks_outside_the_client_calendar_are_corrupt(moment, valid):
    engine = progress(mission(1, ConditionType=1))
    state = engine.initialize(saved())
    state["mission_state"]["last_time"] = milliseconds(moment)
    before = deepcopy(state)
    if valid:
        assert engine.initialize(state) == state
    else:
        fail("mission_state_invalid", lambda: engine.initialize(state))
    assert state == before


def test_request_times_outside_the_client_calendar_keep_the_mission_clock_code():
    engine = progress(mission(1, ConditionType=1))
    original = saved()
    before = deepcopy(original)
    with pytest.raises(ProtocolError) as error:
        engine.record(original, "login", {}, datetime(3001, 2, 1, tzinfo=JST))
    assert (error.value.code, error.value.status) == ("mission_clock_invalid", 400)
    assert original == before


def test_history_capacity_never_evicts_claimed_periods(monkeypatch):
    engine = progress(mission(1, ConditionType=1))
    monkeypatch.setattr(module, "MAX_ENTRIES", 2)
    first = engine.record(saved(), "login", {}, NOW)
    first = engine.claim(first, [1], NOW)
    second = engine.record(first, "login", {}, NOW + timedelta(days=1))
    before = deepcopy(second)
    fail("mission_history_capacity", lambda: engine.record(second, "login", {}, NOW + timedelta(days=2)))
    assert second == before and len(second["mission_state"]["entries"]) == 2


def test_instance_capacity_does_not_discard_duplicate_protection(monkeypatch):
    engine = progress(mission(1, ConditionType=5, ClearCountCondition=3))
    monkeypatch.setattr(module, "MAX_INSTANCES", 2)
    state = engine.record(saved(), "gift", gift(), NOW)
    state = engine.record(state, "gift", gift(11), NOW)
    before = deepcopy(state)
    fail("mission_instance_capacity", lambda: engine.record(state, "gift", gift(12), NOW))
    assert state == before


def test_aggregate_without_dependencies_is_not_vacuously_complete():
    engine = progress(mission(1, ConditionType=3))
    fail("mission_dependencies_invalid", lambda: engine.snapshot(saved(), NOW))


def test_actual_catalog_snapshot_and_login_only_advance_matching_current_definitions(current):
    source, _ = current
    engine = MissionProgress(source)
    initial = engine.initialize(saved())
    snapshot = rows(engine, initial)
    assert len(snapshot) == len(source.active(NOW))
    assert {642, 643, 644, 645} <= set(snapshot)
    assert all(item["Status"] == 0 for item in snapshot.values())
    logged = engine.record(initial, "login", {}, NOW)
    ready = {key for key, value in rows(engine, logged).items() if value["Status"] == 1}
    assert ready == {615, 1611}
    assert rows(engine, logged)[10001]["Status"] == 0
    assert initial["mission_state"]["entries"] == []


def test_verified_dgp_capability_enables_only_its_pinned_login_condition(current):
    source, _ = current
    generic = MissionProgress(source)
    dgp = MissionProgress(source, dgp_login=True)
    assert generic.content_version != dgp.content_version
    assert generic.status()["dgp_login"] is False and dgp.status()["dgp_login"] is True
    logged = dgp.record(saved(), "login", {}, NOW)
    ready = {key for key, value in rows(dgp, logged).items() if value["Status"] == 1}
    assert ready == {615, 1031, 1611}
    assert dgp.record(logged, "login", {}, NOW + timedelta(hours=1)) == logged


@pytest.mark.parametrize("dgp_login", [False, True])
def test_changed_dgp_definition_cannot_inherit_platform_eligibility(dgp_login):
    source = catalog([mission(1031, PeriodType=4, ConditionType=1, ConditionText=5725, RewardItemParam=99)])
    engine = MissionProgress(source, dgp_login=dgp_login)
    initial = engine.initialize(saved())
    fail("mission_definition_changed", lambda: engine.record(initial, "login", {}, NOW))
    assert initial["mission_state"]["entries"] == []


def specs(*values):
    return QuestClearSpecs({spec.condition_id: spec for spec in values})


def member(card_id, index):
    return {"card_id": card_id, "get_index": index, "unique_id": index + 100}


def test_claimed_specs_answer_through_the_cleared_type_policy_with_counted_members():
    rule = TeamRule("Genus", 101, "at_least", 2, frozenset({7, 8}))
    source = catalog([mission(1, QuestType=9, GenusCondition=101),
                      mission(2, QuestType=9, GenusCondition=101, ClearCountCondition=2)])
    engine = MissionProgress(source, clear_specs=specs(
        QuestClearSpec(1, "devil", (9,), frozenset({901}), rule),
        QuestClearSpec(2, "devil", (9,), frozenset({901}), replace(rule, mode="only", count=None))))
    devil = quest(quest_id=901, quest_type=9)
    state = engine.record(saved(), "quest_clear", {**devil, "team": [member(7, 1)]}, NOW)
    assert rows(engine, state)[1]["Progress"] == 0 and rows(engine, state)[2]["Progress"] == 1
    state = engine.record(state, "quest_clear", {**devil, "team": [member(7, 1), member(8, 2), member(3, 3)]}, NOW)
    assert rows(engine, state)[1]["Status"] == 1 and rows(engine, state)[2]["Progress"] == 1
    pair = [member(7, 1), member(8, 2)]
    for event in (devil, {**devil, "quest_id": 902, "team": pair}, {**devil, "quest_type": 0, "team": pair}):
        assert engine.record(state, "quest_clear", event, NOW) == state


def test_unresolved_specs_never_count_even_where_the_shared_rule_would():
    engine = MissionProgress(catalog([mission(1)]),
                             clear_specs=specs(QuestClearSpec(1, "default", None, None, problems=("title_unresolved",))))
    initial = engine.initialize(saved())
    assert engine.record(initial, "quest_clear", quest(), NOW) == initial
    assert engine.status()["clear_specs"]["unresolved"] == {1: ["title_unresolved"]}
    plain = MissionProgress(catalog([mission(1)]), clear_specs=specs(QuestClearSpec(1, "default", None, None)))
    assert rows(plain, plain.record(saved(), "quest_clear", quest(), NOW))[1]["Status"] == 1


def test_untyped_specs_apply_their_own_member_limit_and_formation_rule():
    limit = 3
    source = catalog([mission(1, MemberCountCondition=limit), mission(2, GenusCondition=101),
                      mission(3, AssignCondition=2)])
    engine = MissionProgress(source, clear_specs=specs(
        QuestClearSpec(1, "default", None, None, member_limit=limit),
        QuestClearSpec(2, "default", None, None, TeamRule("Genus", 101, "at_least", 2, frozenset({7, 8}))),
        QuestClearSpec(3, "default", None, None, TeamRule("Assign", 2, "only", None, frozenset({11, 12})))))
    initial = engine.initialize(saved())

    def counted(*card_ids):
        event = quest() if not card_ids else {
            **quest(), "team": [member(card_id, index) for index, card_id in enumerate(card_ids, 1)]}
        return {key for key, row in rows(engine, engine.record(initial, "quest_clear", event, NOW)).items()
                if row["Progress"]}

    assert counted() == set()
    assert counted(7, 8, 3) == {1, 2}
    assert counted(7, 8, 3, 4) == {2}
    assert counted(7, 3) == {1}
    assert counted(11, 12) == {1, 3}
    assert counted(11, 12, 7, 8) == {2}


def test_text_narrowed_specs_count_only_through_the_policy_of_their_type():
    engine = MissionProgress(catalog([mission(1), mission(2)]), clear_specs=specs(
        QuestClearSpec(1, "history", (16,), None), QuestClearSpec(2, "type12", (12,), None)))
    initial = engine.initialize(saved())
    assert engine.record(initial, "quest_clear", quest(), NOW) == initial
    state = engine.record(initial, "quest_clear", quest(quest_id=160001, quest_type=16), NOW)
    assert rows(engine, state)[1]["Status"] == 1 and rows(engine, state)[2]["Progress"] == 0


def test_untyped_beginner_specs_without_a_quest_id_still_wait_for_their_policy():
    source = catalog([mission(10001, PeriodType=100, DispTabType=0, QuestID=1108),
                      mission(10002, PeriodType=100, DispTabType=0)], sequence=[10001, 10002])
    engine = MissionProgress(source, clear_specs=specs(QuestClearSpec(10001, "default", None, frozenset({1108})),
                                                       QuestClearSpec(10002, "default", None, None)))
    claimed = engine.claim(engine.record(saved(), "quest_clear", quest(), NOW), [10001], NOW)
    assert engine.record(claimed, "quest_clear", quest(), NOW) == claimed


def test_specs_must_cover_exactly_the_quest_clear_missions_and_enter_the_content_version():
    source = catalog([mission(1), mission(2, ConditionType=4)])
    for invalid in (specs(), specs(QuestClearSpec(1, "default", None, None), QuestClearSpec(2, "default", None, None)),
                    {1: QuestClearSpec(1, "default", None, None)}):
        with pytest.raises(ValueError):
            MissionProgress(source, clear_specs=invalid)
    plain = MissionProgress(source)
    derived = MissionProgress(source, clear_specs=specs(QuestClearSpec(1, "default", None, None)))
    narrowed = MissionProgress(source, clear_specs=specs(QuestClearSpec(1, "daily", (2,), frozenset({101}))))
    assert len({plain.content_version, derived.content_version, narrowed.content_version}) == 3
    assert plain.status()["clear_specs"] is None and derived.status()["clear_specs"]["specs"] == 1


def test_hard_clear_rank_applies_after_the_type_and_quest_checks():
    engine = progress(mission(1, QuestType=0, HardClearRankCondition=4),
                      mission(2, QuestID=1108, HardClearRankCondition=4, ClearRankCondition=3))
    initial = engine.initialize(saved())
    assert engine.record(initial, "quest_clear", quest(quest_id=901, quest_type=9, hard=True, stars=4), NOW) == initial
    assert engine.record(initial, "quest_clear", quest(stars=3), NOW) == initial
    state = engine.record(initial, "quest_clear", quest(hard=True, stars=4), NOW)
    assert rows(engine, state)[1]["Status"] == rows(engine, state)[2]["Status"] == 1


@pytest.mark.parametrize("changes", [
    {"team": "7"}, {"team": [{"card_id": 7, "get_index": 1}]}, {"team": [member(7, 1), member(8, 1)]},
    {"team": [{"card_id": 0, "get_index": 1, "unique_id": 1}]}, {"team": [{"card_id": 7, "get_index": -1, "unique_id": 1}]},
    {"team": [{"card_id": 7, "get_index": 1, "unique_id": True}]}, {"kills": 100001}, {"kills": -1}, {"kills": "1"},
    {"mission_id": 0}, {"mission_id": "1"}, {"score_mode": 1}, {"stars": 0}, {"stars": 0, "score_mode": False},
    {"stars": 5, "score_mode": True}, {"members": []}])
def test_quest_clear_extensions_are_validated(changes):
    engine = progress(mission(1))
    initial = engine.initialize(saved())
    fail("mission_event_invalid", lambda: engine.record(initial, "quest_clear", {**quest(), **changes}, NOW))


def test_score_results_and_extended_clears_count_like_plain_clears():
    engine = progress(mission(1, ClearCountCondition=2))
    first = engine.record(saved(), "quest_clear", {**quest(stars=0), "score_mode": True, "kills": 0}, NOW)
    final = engine.record(first, "quest_clear", {**quest(), "mission_id": 1, "team": [], "score_mode": False,
                                                  "kills": 100000}, NOW)
    assert rows(engine, final)[1]["Status"] == 1


def test_empire_tab_missions_accumulate_permanently():
    source = catalog([mission(642, PeriodType=3, DispTabType=3, QuestType=18, ClearCountCondition=2)])
    engine = MissionProgress(source, clear_specs=specs(QuestClearSpec(642, "empire", (18,), None, kind_conflict=True)))
    first = engine.record(saved(), "quest_clear", quest(quest_id=94001, quest_type=18), NOW)
    assert (rows(engine, first)[642]["Progress"], rows(engine, first)[642]["Status"]) == (1, 0)
    later = NOW + timedelta(days=40)
    assert engine.record(first, "quest_clear", quest(), later) == first
    final = engine.record(first, "quest_clear", quest(quest_id=94002, quest_type=18), later)
    assert rows(engine, final, later)[642]["Status"] == 1
    assert [entry["period"] for entry in final["mission_state"]["entries"]] == ["permanent"]
    assert tuple(row.condition_id for row in source.page(3, 3, 0, NOW)) == (642,)


STEP = dict(PeriodType=100, ConditionType=4, DispTabType=0)


def journal(state):
    return state[module.JOURNAL_KEY]


def by_condition(state):
    return {entry["condition_id"]: entry for entry in journal(state)["entries"]}


def expected_change(action, entry, definition, goal, rebuilt=None):
    after = rebuilt or {}
    return {"condition_id": entry["condition_id"], "period": entry["period"], "action": action,
            "period_type": {"before": entry["period_type"], "after": definition.period_type},
            "definition_version": {"before": entry["definition_version"], "after": definition.definition_version},
            "goal": {"before": entry["goal"], "after": goal},
            "progress": {"before": entry["progress"], "after": after.get("progress")},
            "status": {"before": entry["status"], "after": after.get("status")},
            "read": {"before": entry["read"], "after": after.get("read")}}


def test_state_keys_and_report_actions_keep_their_names():
    assert (module.JOURNAL_KEY, module.SKIN_HISTORY_KEY, module.SUPERSEDED_KEY) == (
        "mission_state", "skin_acquisition_history", "superseded")
    assert (module.REBUILT, module.DELETED, module.SUPERSEDED) == ("rebuilt", "deleted", "superseded")


def test_rebuild_rewrites_changed_unclaimed_rows_of_the_current_period_only():
    old = progress(mission(1, ConditionType=5, ClearCountCondition=3), mission(2, ConditionType=4),
                   mission(3, ConditionType=4), mission(4, ConditionType=4, ClearCountCondition=5),
                   mission(5, ConditionType=4))
    state = old.record(saved(), "training", training(), NOW)
    for uid in (10, 11, 12):
        state = old.record(state, "gift", gift(uid), NOW)
    state = old.claim(old.mark_read(state, [1, 2, 3, 5], NOW), [3], NOW)
    changed = progress(mission(1, ConditionType=5, ClearCountCondition=2),
                       mission(2, ConditionType=4, ClearCountCondition=3),
                       mission(3, ConditionType=4, RewardItemParam=10),
                       mission(4, ConditionType=4, ClearCountCondition=5), mission(5, ConditionType=4))
    fail("mission_definition_changed", lambda: changed.snapshot(state, NOW))
    fail("mission_clock_backwards", lambda: changed.rebuild_changed(state, NOW - LAST_MILLISECOND))
    original, entries, definitions = deepcopy(state), by_condition(state), changed.catalog.by_id
    assert (entries[3]["status"], entries[3]["read"], entries[5]["status"], entries[5]["read"]) == (2, True, 1, True)
    later = NOW + timedelta(hours=1)
    rebuilt, report = changed.rebuild_changed(state, later)
    gifts = {**entries[1], "definition_version": definitions[1].definition_version, "goal": 2, "progress": 2,
             "status": 1, "read": False, "uids": [10, 11]}
    trained = {**entries[2], "definition_version": definitions[2].definition_version, "goal": 3, "progress": 1,
               "status": 0, "read": False}
    assert report == [expected_change(module.REBUILT, entries[1], definitions[1], 2, gifts),
                      expected_change(module.REBUILT, entries[2], definitions[2], 3, trained)]
    assert by_condition(rebuilt) == {1: gifts, 2: trained, 3: entries[3], 4: entries[4], 5: entries[5]}
    assert journal(rebuilt)["last_time"] == milliseconds(later) and module.SUPERSEDED_KEY not in journal(rebuilt)
    assert {key: value for key, value in rebuilt.items() if key != module.JOURNAL_KEY} == {
        key: value for key, value in original.items() if key != module.JOURNAL_KEY}
    assert state == original and changed.initialize(rebuilt) == rebuilt
    snapshot = rows(changed, rebuilt, later)
    assert (snapshot[1], snapshot[2], snapshot[3]["Status"], snapshot[5]["Read"]) == (
        {"ID": 1, "Status": 1, "Read": 0, "Progress": 2}, {"ID": 2, "Status": 0, "Read": 0, "Progress": 1}, 2, 1)
    assert rows(changed, changed.claim(rebuilt, [1], later), later)[1]["Status"] == 2


def test_rebuild_refuses_a_catalog_that_changed_after_setup(monkeypatch):
    old = progress(mission(1, ConditionType=4))
    state = old.record(saved(), "training", training(), NOW)
    changed = progress(mission(1, ConditionType=4, RewardItemParam=10))
    monkeypatch.setattr(changed.catalog, "content_version", "f" * 64)
    before = deepcopy(state)
    fail("mission_catalog_changed", lambda: changed.rebuild_changed(state, NOW))
    assert state == before


def test_rebuild_without_blocking_rows_returns_the_original_state():
    old = progress(mission(1, ConditionType=4))
    state = old.record(saved(), "training", training(), NOW)
    changed = progress(mission(1, ConditionType=4, RewardItemParam=10))
    next_day = NOW + timedelta(days=1)
    assert rows(changed, state, next_day)[1]["Progress"] == 0
    for engine, moment in ((old, NOW), (changed, next_day)):
        result, report = engine.rebuild_changed(state, moment)
        assert result is state and report == []
    fresh = saved()
    result, report = changed.rebuild_changed(fresh, NOW)
    assert result is fresh and report == [] and module.JOURNAL_KEY not in fresh


def test_period_type_change_deletes_unclaimed_rows_and_supersedes_claimed_ones():
    daily = progress(mission(1, ConditionType=4), mission(2, ConditionType=4))
    state = daily.claim(daily.record(saved(), "training", training(), NOW), [1], NOW)
    later = NOW + timedelta(days=1)
    state = daily.mark_read(daily.record(state, "training", training(), later), [1], later)
    assert module.SUPERSEDED_KEY not in journal(state)
    monthly = progress(mission(1, ConditionType=4, PeriodType=2, DispTabType=2), mission(2, ConditionType=4))
    fail("mission_definition_changed", lambda: monthly.snapshot(state, later))
    original = deepcopy(state)
    claimed, ready = (entry for entry in journal(state)["entries"] if entry["condition_id"] == 1)
    rebuilt, report = monthly.rebuild_changed(state, later)
    definition = monthly.catalog.by_id[1]
    assert report == [expected_change(module.SUPERSEDED, claimed, definition, 1),
                      expected_change(module.DELETED, ready, definition, 1)]
    assert journal(rebuilt)["superseded"] == [claimed]
    assert [entry["condition_id"] for entry in journal(rebuilt)["entries"]] == [2, 2]
    assert state == original and monthly.initialize(rebuilt) == rebuilt
    assert rows(monthly, rebuilt, later)[1] == {"ID": 1, "Status": 0, "Read": 0, "Progress": 0}
    recorded = monthly.record(rebuilt, "training", training(), later)
    assert [(entry["period"], entry["period_type"], entry["status"]) for entry in journal(recorded)["entries"]
            if entry["condition_id"] == 1] == [("month:2026-11", 2, 1)]
    assert journal(recorded)["superseded"] == [claimed] and monthly.initialize(recorded) == recorded
    assert rows(monthly, monthly.claim(recorded, [1], later), later)[1]["Status"] == 2


def test_period_types_that_change_back_keep_every_superseded_claim():
    type_four = progress(mission(1, ConditionType=4, PeriodType=4, DispTabType=4))
    type_three = progress(mission(1, ConditionType=4, PeriodType=3, DispTabType=3))
    state = type_four.claim(type_four.record(saved(), "training", training(), NOW), [1], NOW)
    first = journal(state)["entries"][0]
    state, _ = type_three.rebuild_changed(state, NOW)
    state = type_three.claim(type_three.record(state, "training", training(), NOW), [1], NOW)
    second = journal(state)["entries"][0]
    state, report = type_four.rebuild_changed(state, NOW)
    assert [row["action"] for row in report] == [module.SUPERSEDED]
    assert journal(state)["superseded"] == [first, second]
    assert first["period"] == second["period"] == "permanent" and first["period_type"] != second["period_type"]
    assert type_four.initialize(state) == state
    assert rows(type_four, state)[1]["Status"] == 0


def test_rebuild_reaches_beginner_steps_outside_their_dates():
    old = MissionProgress(catalog([mission(10001, ClearCountCondition=2, **STEP), mission(10002, **STEP)],
                                  sequence=[10001, 10002]))
    state = old.record(saved(), "training", training(), NOW)
    ended = MissionProgress(catalog([mission(10001, EndDateTime=native_date(NOW), **STEP), mission(10002, **STEP)],
                                    sequence=[10001, 10002]))
    later = NOW + timedelta(days=1)
    fail("mission_definition_changed", lambda: ended.snapshot(state, later))
    step, definition = journal(state)["entries"][0], ended.catalog.by_id[10001]
    rebuilt, report = ended.rebuild_changed(state, later)
    expected = {**step, "definition_version": definition.definition_version, "goal": 1, "progress": 1, "status": 1}
    assert report == [expected_change(module.REBUILT, step, definition, 1, expected)]
    assert journal(rebuilt)["entries"] == [expected]
    assert rows(ended, rebuilt, later) == {10002: {"ID": 10002, "Status": 0, "Read": 0, "Progress": 0}}


def test_inactive_beginner_steps_change_their_period_only_where_their_current_row_blocks():
    ranked = dict(PeriodType=4, ConditionType=4, DispTabType=4)
    old = MissionProgress(catalog([mission(10001, **ranked), mission(10002, **STEP)], sequence=[10002]))
    ready = old.record(saved(), "training", training(), NOW)
    claimed = old.claim(ready, [10001], NOW)
    later = NOW + timedelta(days=1)
    ended = MissionProgress(catalog([mission(10001, EndDateTime=native_date(NOW), **STEP), mission(10002, **STEP)],
                                    sequence=[10001, 10002]))
    fail("mission_definition_changed", lambda: ended.snapshot(ready, later))
    unclaimed = by_condition(ready)[10001]
    rebuilt, report = ended.rebuild_changed(ready, later)
    assert report == [expected_change(module.DELETED, unclaimed, ended.catalog.by_id[10001], 1)]
    assert list(by_condition(rebuilt)) == [10002] and module.SUPERSEDED_KEY not in journal(rebuilt)
    assert set(rows(ended, rebuilt, later)) == {10002}
    result, report = ended.rebuild_changed(claimed, later)
    assert result is claimed and report == [] and rows(ended, claimed, later)[10002]["Status"] == 1
    reopened = MissionProgress(catalog([mission(10001, **STEP), mission(10002, **STEP)], sequence=[10001, 10002]))
    fail("mission_definition_changed", lambda: reopened.snapshot(claimed, later))
    archived = by_condition(claimed)[10001]
    moved, report = reopened.rebuild_changed(claimed, later)
    assert report == [expected_change(module.SUPERSEDED, archived, reopened.catalog.by_id[10001], 1)]
    assert journal(moved)["superseded"] == [archived] and rows(reopened, moved, later)[10001]["Status"] == 0


def superseded_state():
    daily = progress(mission(1, ConditionType=4))
    state = daily.claim(daily.record(saved(), "training", training(), NOW), [1], NOW)
    monthly = progress(mission(1, ConditionType=4, PeriodType=2, DispTabType=2))
    return monthly, monthly.rebuild_changed(state, NOW)[0]


@pytest.mark.parametrize("change", [lambda s: s["mission_state"].update(superseded={}),
    lambda s: s["mission_state"]["superseded"].append(1),
    lambda s: s["mission_state"]["superseded"][0].update(status=1),
    lambda s: s["mission_state"]["superseded"][0].pop("uids"),
    lambda s: s["mission_state"]["superseded"][0].update(goal=0),
    lambda s: s["mission_state"]["superseded"][0].update(period="day:2026-12-01"),
    lambda s: s["mission_state"].update(entries=[], last_time=None, last_login_day=None)])
def test_corrupt_superseded_history_is_rejected_without_repair_or_mutation(change):
    engine, state = superseded_state()
    assert engine.initialize(state) == state
    change(state)
    before = deepcopy(state)
    fail("mission_state_invalid", lambda: engine.initialize(state))
    assert state == before


def test_superseded_history_never_discards_archived_claims(monkeypatch):
    daily = progress(mission(1, ConditionType=4))
    later = NOW + timedelta(days=1)
    state = daily.claim(daily.record(saved(), "training", training(), NOW), [1], NOW)
    state = daily.claim(daily.record(state, "training", training(), later), [1], later)
    journal(state)["superseded"] = [deepcopy(journal(state)["entries"][0])]
    monkeypatch.setattr(module, "MAX_ENTRIES", 2)
    monthly = progress(mission(1, ConditionType=4, PeriodType=2, DispTabType=2))
    assert monthly.initialize(state) == state
    before = deepcopy(state)
    fail("mission_history_capacity", lambda: monthly.rebuild_changed(state, later))
    assert state == before
