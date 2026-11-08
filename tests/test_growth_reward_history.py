from copy import deepcopy

import pytest

from aigis_ps.errors import ProtocolError
from test_gift_service import components, make_case, target
from test_quest_service import bundle


def test_growth_records_current_thresholds_without_granting_or_reading_story(make_case):
    case = make_case(7, love=0)
    profile = case.store.profile()
    before = case.service.preserve_history(profile.state, profile.revision)
    unit = next(row for row in before["player_state"]["units"] if row["GetIndex"] == case.indices[0])
    unit["Love"] = 100
    original = deepcopy(before)
    updated = case.service.record_current_thresholds(before, case.indices[0], profile.revision)
    assert updated["gift_state"]["claims"]["7"] == [1, 100]
    assert {key: value for key, value in updated.items() if key != "gift_state"} == {
        key: value for key, value in original.items() if key != "gift_state"}
    assert updated["gift_state"]["history"] == original["gift_state"]["history"]
    assert before == original
    assert case.service.record_current_thresholds(updated, case.indices[0], profile.revision) == updated


def test_recorded_thresholds_survive_later_lower_affection_without_backfill(make_case):
    case = make_case(7, love=150)
    profile = case.store.profile()
    state = case.service.record_current_thresholds(profile.state, case.indices[0], profile.revision)
    assert state["gift_state"]["claims"]["7"] == [1, 100, 150]
    row = next(row for row in state["player_state"]["units"] if row["GetIndex"] == case.indices[0])
    row["Love"] = 0
    pending, grants = case.service._new_rewards(state["gift_state"], row, case.service._card(7), 100)
    assert not pending and not grants
    assert state["gift_state"]["claims"]["7"] == [1, 100, 150]


def test_units_without_affection_rewards_do_not_gain_fake_claims(make_case):
    case = make_case(56, love=100)
    profile = case.store.profile()
    assert case.service._card(56)["BonusType"] == 0
    state = case.service.record_current_thresholds(profile.state, case.indices[0], profile.revision)
    assert "56" not in state["gift_state"]["claims"]


@pytest.mark.parametrize("index", [-1, True, "1", 999999])
def test_invalid_threshold_target_does_not_mutate_progress(make_case, index):
    case = make_case(7, love=100)
    profile = case.store.profile()
    before = deepcopy(profile.state)
    with pytest.raises(ProtocolError):
        case.service.record_current_thresholds(profile.state, index, profile.revision)
    assert profile.state == before
