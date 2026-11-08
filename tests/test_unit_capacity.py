from copy import deepcopy

import pytest

from aigis_ps.barracks_service import EXTRA_SHIFT, PURCHASE_FIELD
from aigis_ps.errors import ProtocolError
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.unit_capacity import (CLEAR_BONUS, CLEAR_FLAG, CLEAR_LIMIT_COMMAND, CLEAR_STEP, FIRST_BARRACKS_BASE,
                                    RANK_LIMIT, RANK_STEP, SPECIAL_QUEST_BONUS, SPECIAL_QUESTS, UnitCapacity)
from test_image_aliases import command_table, encode
from test_quest_service import bundle


@pytest.fixture(scope="module")
def capacity(bundle):
    assert bundle.unit_capacity is not None, bundle.unit_capacity_error
    return bundle.unit_capacity


def state(*, rank=1, purchases=0, flags=(0,), records=()):
    return {"bootstrap_schema": 1, "tables": {
        "game_param": [{"Rank": rank}], "config": [{PURCHASE_FIELD: purchases}],
        "quest_records": [{"QuestID": identifier, "AchieveFlag": value} for identifier, value in records]},
        "player_state": {"units": [{"GeneralPurposeFlags": value} for value in flags]}}


def test_current_capacity_uses_verified_settings_and_counts_prince(capacity):
    usage = capacity.usage(state())
    assert usage.capacities == (756,) and usage.counts == (1,)
    assert usage.primary_available == 755
    assert capacity.status()["type1_quest_count"] == 61
    assert capacity.status()["clear_limit"] == 55
    assert capacity.status()["extra_capacity"] == 900


def test_first_barracks_formula_constants_follow_the_native_calculation(capacity):
    assert (FIRST_BARRACKS_BASE, RANK_LIMIT, RANK_STEP) == (198, 200, 10)
    assert (CLEAR_STEP, CLEAR_BONUS, SPECIAL_QUEST_BONUS) == (10, 2, 4)
    status = capacity.status()
    assert status["first_base"] == FIRST_BARRACKS_BASE + capacity.append == 756
    assert status["rank_limit"] == RANK_LIMIT


@pytest.mark.parametrize("rank,bonus", [(1, 0), (9, 0), (10, 1), (199, 19), (200, 20), (201, 20), (INT32_MAX, 20)])
def test_rank_capacity_is_capped_before_division(capacity, rank, bonus):
    assert capacity.usage(state(rank=rank)).capacities == (756 + bonus,)


@pytest.mark.parametrize("count,bonus", [(0, 0), (4, 0), (5, 2), (14, 2), (15, 4), (54, 10), (55, 12), (61, 12)])
def test_only_type1_clears_count_and_limit_is_applied_first(capacity, count, bonus):
    records = [(identifier, CLEAR_FLAG) for identifier in sorted(capacity.counted_quests)[:count]]
    assert capacity.usage(state(records=records)).capacities == (756 + bonus,)


def test_special_quests_count_separately_and_other_flags_do_not_unlock(capacity):
    records = [(identifier, CLEAR_FLAG) for identifier in capacity.counted_quests | {1108, 1208, 1306}]
    assert capacity.usage(state(rank=200, records=records)).capacities == (800,)
    for flags in (0, 0x40000000, 0x00100000, 0x11100, 1):
        assert capacity.usage(state(records=[(identifier, flags) for identifier, _ in records])).capacities == (756,)
    assert capacity.usage(state(records=[(1108, -0x80000000 | CLEAR_FLAG)])).capacities == (760,)


def test_purchase_low_bits_do_not_increase_capacity_and_flags_preserve_groups(capacity):
    before = state(purchases=(2 << EXTRA_SHIFT) | 35, flags=(0, 2, 0x12, 0x24, -0x80000000 | 0x20))
    saved = deepcopy(before)
    usage = capacity.usage(before)
    assert usage.capacities == (756, 900, 900) and usage.counts == (2, 1, 2)
    assert before == saved
    assert capacity.usage(state(purchases=65535)).capacities == (756,)


def test_old_over_capacity_state_is_readable_without_deleting_rows(capacity):
    saved = state(flags=(0,) * 800)
    assert capacity.usage(saved).counts == (800,)
    assert capacity.available(saved) == 0 and len(saved["player_state"]["units"]) == 800


@pytest.mark.parametrize("purchases,flags", [(0, (0x10,)), (1 << EXTRA_SHIFT, (0x20,)), (3 << EXTRA_SHIFT, (0,)), (-1, (0,)),
                                             (2 << EXTRA_SHIFT, (0xF0,))])
def test_unmodeled_or_locked_barracks_do_not_create_free_capacity(capacity, purchases, flags):
    with pytest.raises(ProtocolError) as caught:
        capacity.usage(state(purchases=purchases, flags=flags))
    assert caught.value.code == "unit_capacity_barracks_unsupported"


@pytest.mark.parametrize("change", [
    lambda value: value.update(bootstrap_schema=True),
    lambda value: value["tables"]["game_param"][0].update(Rank=0),
    lambda value: value["tables"]["game_param"][0].update(Rank=True),
    lambda value: value["tables"]["config"][0].pop(PURCHASE_FIELD),
    lambda value: value["tables"].update(quest_records=[{"QuestID": 999999, "AchieveFlag": 0}]),
    lambda value: value["tables"].update(quest_records=[{"QuestID": 2001, "AchieveFlag": 0}] * 2),
    lambda value: value["tables"].update(quest_records=[{"QuestID": 2001, "AchieveFlag": True}]),
    lambda value: value["player_state"]["units"][0].update(GeneralPurposeFlags=True),
    lambda value: value["player_state"].update(units=None),
])
def test_invalid_state_fails_without_fabricating_slots(capacity, change):
    saved = state()
    change(saved)
    with pytest.raises(ProtocolError) as caught:
        capacity.usage(saved)
    assert caught.value.code == "unit_capacity_state_invalid"


def custom_capacity(bundle, commands=(), system=()):
    resources = {"OnCommand.atb": command_table([(identifier, encode(text)) for identifier, text in commands]),
                 "OnSystemCommand.atb": command_table([(identifier, encode(text)) for identifier, text in system])}
    return UnitCapacity(bundle.masters.snapshot(), resources.__getitem__)


def test_missing_settings_follow_native_defaults_without_clear_limit(bundle):
    custom = custom_capacity(bundle)
    records = [(identifier, CLEAR_FLAG) for identifier in custom.counted_quests]
    usage = custom.usage(state(purchases=1 << EXTRA_SHIFT, records=records))
    assert usage.capacities == (210, 250)
    assert custom.clear_limit is None


def test_clear_limit_only_comes_from_the_named_callback(bundle):
    custom = custom_capacity(bundle, commands=[(99, 'SetSystemStateParam("ClearQuestCountLimit",0);'),
                                              (CLEAR_LIMIT_COMMAND, 'SetSystemStateParam("ClearQuestCountLimit", 5);')])
    records = [(identifier, CLEAR_FLAG) for identifier in custom.counted_quests]
    assert custom.usage(state(records=records)).capacities == (200,)


@pytest.mark.parametrize("command", ['SetSystemStateParam("ClearQuestCountLimit", -1);',
    'SetSystemStateParam("ClearQuestCountLimit", 2147483648);',
    'SetSystemStateParam("ClearQuestCountLimit", GetValue());',
    'if (1) { SetSystemStateParam("ClearQuestCountLimit",55); }'])
def test_unknown_capacity_commands_are_not_executed_or_silently_ignored(bundle, command):
    with pytest.raises(ProtocolError) as caught:
        custom_capacity(bundle, commands=[(CLEAR_LIMIT_COMMAND, command)])
    assert caught.value.code == "unit_capacity_resources_invalid"


@pytest.mark.parametrize("command", ['SetSystemConfigParam("BarracksCapacity",0);',
    'SetSystemConfigParam("AppendOfFirstBarrakcsCapacity",-1);',
    'SetSystemConfigParam("AppendOfFirstBarrakcsCapacity",2147483647);',
    'SetSystemConfigParam("BarracksCapacity",10.5);'])
def test_invalid_capacity_settings_fail_closed(bundle, command):
    with pytest.raises(ProtocolError) as caught:
        custom_capacity(bundle, system=[(1, command)])
    assert caught.value.code == "unit_capacity_resources_invalid"


def append_setting(value):
    return [(1, f'SetSystemConfigParam("AppendOfFirstBarrakcsCapacity",{value});')]


def test_resource_bound_is_the_first_barracks_capacity_at_maximum_inputs(bundle):
    probe = custom_capacity(bundle)
    records = [(identifier, CLEAR_FLAG) for identifier in probe.counted_quests | SPECIAL_QUESTS]
    peak = probe.usage(state(rank=RANK_LIMIT, records=records)).capacities[0]
    assert peak == 242
    largest = custom_capacity(bundle, system=append_setting(INT32_MAX - peak))
    assert largest.usage(state(rank=INT32_MAX, records=records)).capacities[0] == INT32_MAX
    with pytest.raises(ProtocolError) as caught:
        custom_capacity(bundle, system=append_setting(INT32_MAX - peak + 1))
    assert caught.value.code == "unit_capacity_resources_invalid"


def test_duplicate_capacity_callback_is_rejected(bundle):
    with pytest.raises(ProtocolError):
        custom_capacity(bundle, commands=[(CLEAR_LIMIT_COMMAND, ""), (CLEAR_LIMIT_COMMAND, "")])
