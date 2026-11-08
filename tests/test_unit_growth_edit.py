from copy import copy, deepcopy
from types import SimpleNamespace

import pytest

from aigis_ps.admin_commit import InitializerRestartCheck
from aigis_ps.errors import ProtocolError
from aigis_ps.training_catalog import TrainingRules
from aigis_ps.unit_growth_edit import (ADVANCED_TRUST_LIMIT, GROWTH_FIELDS, OVERFLOW_DROPPED, TRUST_LIMIT, GrowthEdit,
                                       GrowthLimits, UnitGrowthEditor)
from aigis_ps.unit_removal import UnitIdentity
from test_unit_removal import PRINCE_INDEX, bundle, granted, identity, mails, new_save, unit_at


# Card 17 grants trust up to 150, card 8 stops at 100, and card 2 has no trust parameter at all.
TRUST_CARD, PLAIN_TRUST_CARD, NO_TRUST_CARD = 17, 8, 2
EXTRA_EXPERIENCE = 1234
STALE_PROGRESS = 3


@pytest.fixture
def editor(bundle):
    return UnitGrowthEditor(bundle.player_state, bundle.training.catalog, bundle.gifts)


@pytest.fixture
def owned(bundle):
    state, added = granted(bundle, new_save(bundle), card_id=TRUST_CARD)
    return state, identity(state, added[0]["GetIndex"])


def target(state, selected):
    return unit_at(state, selected.get_index)


def refused(editor, state, selected, edit, code):
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        editor.edit(state, 0, selected, edit)
    assert state == before
    assert caught.value.code == code
    return caught.value


def test_limits_come_from_the_training_growth_tables_and_trust_parameters(bundle, editor, owned):
    state, selected = owned
    unit = target(state, selected)
    view = bundle.training.catalog.describe(unit)
    assert editor.limits(unit) == GrowthLimits(view.level, view.cap, view.thresholds[view.cap - 1],
                                               bundle.training.catalog.rules.retain_overflow_experience,
                                               ADVANCED_TRUST_LIMIT, view.skill_max)
    assert view.cap > view.level and view.skill_max > 1


def test_experience_beyond_the_level_cap_is_kept_under_the_default_rule(editor, owned):
    state, selected = owned
    limits = editor.limits(target(state, selected))
    assert limits.retains_overflow is True
    result = editor.edit(state, 0, selected, GrowthEdit(experience=limits.cap_experience + EXTRA_EXPERIENCE))
    assert result.after["Exp"] == limits.cap_experience + EXTRA_EXPERIENCE and result.warnings == ()
    assert editor.limits(result.after).level == limits.level_cap


def test_experience_overflow_is_dropped_when_the_training_rule_drops_it(bundle, owned):
    state, selected = owned
    growth = copy(bundle.training.catalog)
    growth.rules = TrainingRules(retain_overflow_experience=False)
    editor = UnitGrowthEditor(bundle.player_state, growth, bundle.gifts)
    limits = editor.limits(target(state, selected))
    result = editor.edit(state, 0, selected, GrowthEdit(experience=limits.cap_experience + EXTRA_EXPERIENCE))
    assert result.after["Exp"] == limits.cap_experience and result.warnings == (OVERFLOW_DROPPED,)
    below = editor.edit(state, 0, selected, GrowthEdit(experience=limits.cap_experience - 1))
    assert below.after["Exp"] == limits.cap_experience - 1 and below.warnings == ()


@pytest.mark.parametrize("card, limit", [(TRUST_CARD, ADVANCED_TRUST_LIMIT), (PLAIN_TRUST_CARD, TRUST_LIMIT),
                                         (NO_TRUST_CARD, TRUST_LIMIT)])
def test_love_stays_within_the_cards_trust_limit(bundle, editor, card, limit):
    state, added = granted(bundle, new_save(bundle), card_id=card)
    selected = identity(state, added[0]["GetIndex"])
    assert editor.edit(state, 0, selected, GrowthEdit(love=limit)).after["Love"] == limit
    error = refused(editor, state, selected, GrowthEdit(love=limit + 1), "unit_growth_limit")
    assert error.status == 409


def test_love_edits_record_the_reached_tiers_without_rewards_or_story_changes(editor, owned):
    state, selected = owned
    assert "gift_state" not in state
    result = editor.edit(state, 0, selected, GrowthEdit(love=TRUST_LIMIT))
    assert result.state["gift_state"]["claims"] == {str(TRUST_CARD): [1, 100]}
    assert mails(result.state) == mails(state)
    assert result.state["tables"]["card_status"] == state["tables"]["card_status"]
    assert result.after["WatchLoveEv"] == result.before["WatchLoveEv"] == 0


def test_lowering_love_keeps_the_tiers_reached_before_the_edit(editor, owned):
    state, selected = owned
    target(state, selected)["Love"] = ADVANCED_TRUST_LIMIT
    result = editor.edit(state, 0, selected, GrowthEdit(love=0))
    assert result.after["Love"] == 0
    assert result.state["gift_state"]["claims"] == {str(TRUST_CARD): [1, 100, 150]}


def test_skill_levels_stay_within_the_skill_table_and_restart_their_progress(editor, owned):
    state, selected = owned
    target(state, selected)["SkillExperience"] = STALE_PROGRESS
    maximum = editor.limits(target(state, selected)).skill_level_max
    result = editor.edit(state, 0, selected, GrowthEdit(skill_level=maximum))
    assert (result.after["SkillLevel"], result.after["SkillExperience"]) == (maximum, 0)
    unchanged = editor.edit(state, 0, selected, GrowthEdit(skill_level=target(state, selected)["SkillLevel"]))
    assert unchanged.after["SkillExperience"] == STALE_PROGRESS
    for level in (0, maximum + 1):
        refused(editor, state, selected, GrowthEdit(skill_level=level), "unit_growth_limit")


def test_skills_without_levels_cannot_be_edited(bundle, owned):
    state, selected = owned
    flat = SimpleNamespace(rules=TrainingRules(),
                           describe=lambda unit: SimpleNamespace(level=1, cap=1, thresholds=(0,), skill_max=0))
    editor = UnitGrowthEditor(bundle.player_state, flat, bundle.gifts)
    error = refused(editor, state, selected, GrowthEdit(skill_level=1), "unit_growth_limit")
    assert "no levels" in str(error)


def test_the_prince_is_never_edited(editor, owned):
    state, _ = owned
    prince = identity(state, PRINCE_INDEX)
    error = refused(editor, state, prince, GrowthEdit(experience=1), "unit_growth_unsupported")
    assert error.status == 501


def test_targets_must_match_an_owned_unit(editor, owned):
    state, selected = owned
    refused(editor, state, UnitIdentity(selected.get_index, selected.unique_id + 1), GrowthEdit(love=1), "unit_growth_missing")
    refused(editor, state, UnitIdentity(9999, 9999), GrowthEdit(love=1), "unit_growth_missing")


def test_edits_change_only_growth_fields_and_the_trust_ledger(editor, owned):
    state, selected = owned
    result = editor.edit(state, 0, selected, GrowthEdit(experience=500, love=50, skill_level=2))
    assert {name for name in GROWTH_FIELDS if result.before[name] != result.after[name]} == {"Exp", "Love", "SkillLevel"}
    assert {name: value for name, value in result.after.items() if name not in GROWTH_FIELDS} == {
        name: value for name, value in result.before.items() if name not in GROWTH_FIELDS}
    others = [unit for unit in result.state["player_state"]["units"] if unit["GetIndex"] != selected.get_index]
    assert others == [unit for unit in state["player_state"]["units"] if unit["GetIndex"] != selected.get_index]
    assert ({key: value for key, value in result.state.items() if key not in ("player_state", "gift_state")}
            == {key: value for key, value in state.items() if key != "player_state"})


def test_growth_edits_survive_a_restart_unchanged(bundle, editor, owned):
    state, selected = owned
    result = editor.edit(state, 0, selected, GrowthEdit(experience=20000, love=120, skill_level=3))
    InitializerRestartCheck(bundle.initialize).check(result.state)


def test_edits_leave_their_input_alone(editor, owned):
    state, selected = owned
    before = deepcopy(state)
    result = editor.edit(state, 0, selected, GrowthEdit(love=10))
    result.after["Love"] = 99
    result.state["player_state"]["units"][-1]["Love"] = 98
    assert state == before


@pytest.mark.parametrize("missing", ["growth", "gifts"])
def test_edits_need_the_growth_tables_and_the_trust_ledger(bundle, missing):
    arguments = {"growth": bundle.training.catalog, "gifts": bundle.gifts, missing: None}
    with pytest.raises(ProtocolError) as caught:
        UnitGrowthEditor(bundle.player_state, arguments["growth"], arguments["gifts"], error_prefix="admin_unit_growth")
    assert (caught.value.code, caught.value.status) == ("admin_unit_growth_dependencies_unavailable", 503)


def test_reference_faults_in_the_growth_tables_become_unavailable_errors(bundle, owned):
    state, selected = owned

    def broken(unit):
        raise KeyError(unit["ClassID"])

    editor = UnitGrowthEditor(bundle.player_state, SimpleNamespace(rules=TrainingRules(), describe=broken), bundle.gifts)
    error = refused(editor, state, selected, GrowthEdit(love=1), "unit_growth_reference_invalid")
    assert error.status == 503 and isinstance(error.__cause__, KeyError)


@pytest.mark.parametrize("values", [{}, {"love": -1}, {"experience": True}, {"skill_level": 2 ** 31}, {"love": 1.0}])
def test_growth_edits_set_at_least_one_native_value(values):
    with pytest.raises(ValueError):
        GrowthEdit(**values)


@pytest.mark.parametrize("prefix", [None, "", "has space", "UPPER", "x" * 65])
def test_error_prefixes_are_bounded(bundle, prefix):
    with pytest.raises(ValueError):
        UnitGrowthEditor(bundle.player_state, bundle.training.catalog, bundle.gifts, error_prefix=prefix)


def test_edits_need_an_identity_and_a_growth_edit(editor, owned):
    state, selected = owned
    for arguments in ((selected.get_index, GrowthEdit(love=1)), (selected, {"love": 1})):
        with pytest.raises(ValueError):
            editor.edit(state, 0, *arguments)
