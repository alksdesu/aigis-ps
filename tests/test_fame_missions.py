from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from aigis_ps.card_status import CardStatusService
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.fame_catalog import FameCatalog
from aigis_ps.mission_catalog import MissionCatalog
from aigis_ps.mission_service import MissionService
from aigis_ps.mission_state import MissionProgress
from test_quest_service import bundle


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
STAMP = int(NOW.timestamp())


@pytest.fixture(scope="module")
def context(bundle):
    reader = lambda name: bundle.native_cdn.assets.read(name)[0]
    fame = FameCatalog(bundle.training.catalog, bundle.evolution.catalog, reader)
    catalog = MissionCatalog(reader, resource_version=bundle.training.catalog.cards.resource_version)
    status = CardStatusService(fame.snapshot)
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    service = MissionService(bootstrap, bundle.player_state, bundle.presents, catalog,
                             fame=fame, fame_status=status)
    ordinary = tuple(sorted(fame.card_ids - fame.support_card_ids))
    support = tuple(sorted(fame.card_ids & fame.support_card_ids))
    return SimpleNamespace(bundle=bundle, fame=fame, catalog=catalog, status=status,
                           service=service, progress=service.progress, ordinary=ordinary, support=support)


@pytest.fixture
def state(context):
    result = context.bundle.player_state.initialize(context.service.bootstrap.initial_state())
    result["sentinel"] = {"nested": ["unchanged"]}
    return result


def fame_rows(context, state, now=NOW):
    snapshot = {row["ID"]: row for row in context.progress.snapshot(state, now)}
    return {definition.condition_id: (definition, snapshot[definition.condition_id])
            for definition in context.catalog.active(now)
            if definition.condition_type in (10, 16)}


def assert_progress(context, state, total, support, now=NOW):
    current = fame_rows(context, state, now)
    assert len(current) == 108
    for definition, row in current.values():
        target = total if definition.condition_type == 10 else support
        goal = definition.in_hallof_fame_sum if definition.condition_type == 10 else definition.in_hallof_fame_support_sum
        assert row["Progress"] == min(goal, target), definition.condition_id
        assert row["Status"] == int(target >= goal), definition.condition_id


def add(context, before, changes, now=NOW):
    candidate = context.status.update(before, changes)
    old_before, old_candidate = deepcopy(before), deepcopy(candidate)
    result = context.service.record_fame(before, candidate, now)
    assert before == old_before and candidate == old_candidate
    return result


def test_real_native_counting_sets_and_goals(context):
    assert len(context.fame.counted_card_ids) == 1314
    assert len(context.fame.support_card_ids) == 53
    assert context.progress.fame_cards == context.fame.counted_card_ids
    assert context.progress.fame_support_cards == context.fame.support_card_ids
    assert {context.bundle.training.catalog.cards.card(key)["Kind"] for key in context.fame.support_card_ids} == {100}
    assert len([item for item in context.catalog.definitions if item.condition_type == 10]) == 58
    assert len([item for item in context.catalog.definitions if item.condition_type == 16]) == 50
    assert {10, 16} <= set(context.progress.status()["supported_types"])


def test_support_increases_both_total_and_support_series(context, state):
    result = add(context, state, [{"CardID": context.support[0], "B0": STAMP}])
    assert_progress(context, result, 1, 1)
    result = add(context, result, [{"CardID": context.ordinary[0], "B0": STAMP}])
    assert_progress(context, result, 2, 1)


def test_ordinary_registration_never_increases_support(context, state):
    result = add(context, state, [{"CardID": key, "B0": STAMP} for key in context.ordinary[:5]])
    assert_progress(context, result, 5, 0)


def test_one_card_can_add_both_second_awakening_stages_once(context, state):
    first = add(context, state, [{"CardID": 17, "B3": STAMP}])
    assert_progress(context, first, 1, 0)
    second = add(context, first, [{"CardID": 17, "B4": STAMP + 1}], NOW + timedelta(seconds=1))
    assert_progress(context, second, 2, 0, NOW + timedelta(seconds=1))
    row = context.status.read(second)[0]
    assert row["B0"] == row["B1"] == row["B2"] == 0
    assert (row["B3"], row["B4"]) == (STAMP, STAMP + 1)


def test_duplicate_candidate_and_same_dates_do_not_count_again(context, state):
    first = add(context, state, [{"CardID": 17, "B3": STAMP}])
    before = deepcopy(first)
    same = context.status.update(first, [{"CardID": 17, "B3": STAMP}])
    assert context.service.record_fame(first, same, NOW + timedelta(days=1)) == first
    assert context.service.record_fame(first, deepcopy(first), NOW) == first
    assert first == before
    assert_progress(context, first, 1, 0)


def test_legacy_nonzero_history_is_preserved_without_backfill(context, state):
    legacy = context.status.update(state, [{"CardID": 17, "B3": STAMP - 100, "A1": 77, "A7": 3},
                                          {"CardID": 2695, "B0": STAMP - 90},
                                          {"CardID": 2696, "B0": STAMP - 80}])
    initialized = context.service.initialize(legacy)
    assert_progress(context, initialized, 0, 0)
    assert context.service.record_fame(initialized, deepcopy(initialized), NOW) == initialized
    result = add(context, initialized, [{"CardID": 17, "B4": STAMP}])
    assert_progress(context, result, 1, 0)
    rows = {row["CardID"]: row for row in context.status.read(result)}
    assert rows[17]["B3"] == STAMP - 100 and rows[17]["A1"] == 77 and rows[17]["A7"] == 3
    assert rows[2695]["B0"] == STAMP - 90 and rows[2696]["B0"] == STAMP - 80


def test_raw_counted_support_set_includes_two_historical_only_cards(context, state):
    assert {2695, 2696} <= context.fame.support_card_ids
    assert not {2695, 2696} & context.fame.card_ids
    result = context.progress.record(state, "fame_registered", {"registrations": [(2695, 0), (2696, 0)]}, NOW)
    assert_progress(context, result, 2, 2)


@pytest.mark.parametrize("kind", ("new_zero", "other_fields", "deposit", "withdraw", "instance_flags"))
def test_nonregistration_changes_do_not_generate_fame_progress(context, state, kind):
    before = add(context, state, [{"CardID": 17, "B3": STAMP}])
    candidate = deepcopy(before)
    if kind == "new_zero":
        candidate = context.status.update(candidate, [{"CardID": context.ordinary[0], "B0": 0}])
    elif kind == "other_fields":
        candidate = context.status.update(candidate, [{"CardID": 17, "A1": 99, "A3": 1, "A5": 2, "A7": 3}])
    elif kind == "deposit":
        candidate["tables"]["fame_depository"] = [{"CardId": 17, "Count0": 0, "Count1": 0,
                                                  "Count2": 0, "Count3": 1, "Count4": 0}]
    elif kind == "withdraw":
        candidate["player_state"]["battle"]["UX"] += 1
    else:
        candidate["player_state"]["units"][0].update(CanFame=True, Famed=True)
    expected = deepcopy(candidate)
    result = context.service.record_fame(before, candidate, NOW + timedelta(seconds=1))
    assert result == expected
    assert result["mission_state"] == before["mission_state"]
    assert_progress(context, result, 1, 0, NOW + timedelta(seconds=1))


@pytest.mark.parametrize("change", ("replace_date", "clear_date", "delete_row", "replace_card"))
def test_existing_nonzero_registration_dates_cannot_change(context, state, change):
    before = context.status.update(state, [{"CardID": 17, "B3": STAMP - 10}])
    candidate = deepcopy(before)
    row = candidate["tables"]["card_status"][0]
    if change == "replace_date":
        row["B3"] = STAMP
    elif change == "clear_date":
        row["B3"] = 0
    elif change == "delete_row":
        candidate["tables"]["card_status"] = []
    else:
        row["CardID"] = 18
    old_before, old_candidate = deepcopy(before), deepcopy(candidate)
    with pytest.raises(ProtocolError) as failure:
        context.service.record_fame(before, candidate, NOW)
    assert failure.value.code == "mission_fame_history_changed"
    assert before == old_before and candidate == old_candidate


def test_unrelated_historical_card_does_not_count(context, state):
    result = add(context, state, [{"CardID": 1, "B0": STAMP}])
    assert "mission_state" not in result
    assert_progress(context, result, 0, 0)
    assert context.status.read(result)[0]["B0"] == STAMP


def test_claimed_fame_mission_stays_claimed_while_later_goals_advance(context, state):
    first = add(context, state, [{"CardID": 17, "B3": STAMP}])
    first_id = next(item.condition_id for item in context.catalog.definitions
                    if item.condition_type == 10 and item.in_hallof_fame_sum == 1)
    claimed = context.progress.claim(first, [first_id], NOW)
    result = add(context, claimed, [{"CardID": 17, "B4": STAMP + 1}], NOW + timedelta(seconds=1))
    current = fame_rows(context, result, NOW + timedelta(seconds=1))
    assert current[first_id][1]["Status"] == 2
    for definition, row in current.values():
        if definition.condition_type == 10 and definition.in_hallof_fame_sum > 1:
            assert row["Progress"] == 2
    assert claimed["tables"]["card_status"][0]["B4"] == 0


def test_fame_event_preserves_wallet_roster_mail_and_other_ledgers(context, state):
    state["custom_history"] = {"keep": [1, 2, 3]}
    candidate = context.status.update(state, [{"CardID": 17, "B3": STAMP}])
    result = context.service.record_fame(state, candidate, NOW)
    assert {key: value for key, value in result.items() if key != "mission_state"} == candidate
    assert "skin_acquisition_history" not in result


def test_missing_fame_dependency_rejects_even_no_change(context, state):
    service = MissionService(context.service.bootstrap, context.bundle.player_state, context.bundle.presents, context.catalog)
    with pytest.raises(ProtocolError) as failure:
        service.record_fame(state, deepcopy(state), NOW)
    assert failure.value.code == "mission_fame_unavailable"


@pytest.mark.parametrize("kind", ("missing_status", "different_snapshot", "wrong_counted", "wrong_support"))
def test_dependency_mismatch_prevents_service_setup(context, kind):
    status = context.status
    counted, support = context.fame.counted_card_ids, context.fame.support_card_ids
    if kind == "missing_status":
        status = None
    elif kind == "different_snapshot":
        status = copy(status)
        status.snapshot = replace(status.snapshot, identifier="different")
    elif kind == "wrong_counted":
        counted = counted - {17}
    else:
        support = frozenset()
    progress = MissionProgress(context.catalog, fame_cards=counted, fame_support_cards=support)
    with pytest.raises(ProtocolError) as failure:
        MissionService(context.service.bootstrap, context.bundle.player_state, context.bundle.presents,
                       context.catalog, progress=progress, fame=context.fame, fame_status=status)
    assert failure.value.code == "mission_dependencies_unavailable"


@pytest.mark.parametrize("kind", ("missing_schema", "bool_date", "duplicate", "missing_column"))
def test_actual_card_status_schema_is_required(context, state, kind):
    candidate = context.status.update(state, [{"CardID": 17, "B3": STAMP}])
    if kind == "missing_schema":
        candidate.pop("card_status_schema")
    elif kind == "bool_date":
        candidate["tables"]["card_status"][0]["B3"] = True
    elif kind == "duplicate":
        candidate["tables"]["card_status"] *= 2
    else:
        candidate["tables"]["card_status"][0].pop("B0")
    before = deepcopy(candidate)
    with pytest.raises(ProtocolError):
        context.service.record_fame(state, candidate, NOW)
    assert candidate == before


@pytest.mark.parametrize("registrations", ([(17, 3), (17, 3)], [(17, 5)], [(17, -1)], [(True, 3)],
                                            [(17, True)], [(999999, 3)], [17], "17,3"))
def test_bad_fame_event_pairs_are_rejected_by_progress(context, state, registrations):
    before = deepcopy(state)
    with pytest.raises(ProtocolError):
        context.progress.record(state, "fame_registered", {"registrations": registrations}, NOW)
    assert state == before


def test_same_instant_naive_jst_is_accepted_by_hook(context, state):
    candidate = context.status.update(state, [{"CardID": 17, "B3": STAMP}])
    aware = context.service.record_fame(state, candidate, NOW)
    naive = context.service.record_fame(state, candidate, NOW.replace(tzinfo=None))
    assert aware == naive
