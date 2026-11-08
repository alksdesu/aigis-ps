import ast
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps import (
    native_numbers, quest_modes, quest_service, subjugation_rules, tower_catalog, tower_service, tower_state,
)
from aigis_ps.drop_bonus import SUPPORTED_OPERATION, DropBonus, bonus_container
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.native_clock import native_clock
from aigis_ps.native_numbers import INT32_MAX, INT32_MIN
from aigis_ps.quest_catalog import HardModeSpec
from aigis_ps.quest_clear_spec import QuestClearSpec
from aigis_ps.quest_modes import (
    BOOSTS, CLEAR_FLAG, CONDITION_FIELDS, DAY_OPEN_GRACE, ENDED, ENTRUST_PARITY_HOOKS, ENTRUST_TYPES, MAX_BOOST,
    MAX_OBTAINED, MISSION_CATEGORIES, MODE_PROGRESS, MODE_SPECS, NO_MISSION_RANK, OPEN, PLATFORM_CODES,
    PLATFORM_MASK_CATEGORIES, THREE_STAR_FLAG, UNAVAILABLE, UNBOOSTED, UPCOMING, EntrustContext, EntrustDraw,
    EntrustRewards, MissionSelector, MissionWindow, MissionWindows, ModePolicy, ModeServices, ScoreContext, ScoreResult,
    SettleContext, SettleOutcome, SpecialCondition, StartContext, UnavailablePolicy, admit, candidate_attributes,
    condition_allows, parse_special_conditions, quest_cleared, quest_open_on, require_platform, require_row_values,
    slot_obtained, special_condition_satisfied, tag_span_end, validate_entrust_parity, validate_event_reward_families,
    validate_window_overrides,
)
from test_drop_bonus import BANNER, CRYSTAL, CRYSTAL_TYPE, MONDAY, RUBY, SUNDAY_NIGHT
from test_quest_catalog import table


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
EPOCH = datetime(1, 1, 1, tzinfo=JST)
ALL_SLOTS = 0x10000 | 0x1000 | 0x100 | 0x1
PLATFORM_MESSAGE = "^Mission availability requires a native platform code$"
HALF_SECOND, CLIENT_TICK = timedelta(milliseconds=500), timedelta(milliseconds=4)
# The client's float32 day fraction reads 12:00:08 as 12:00:07.996.
QUANTIZED_SECOND = datetime(2026, 11, 1, 12, 0, 8, tzinfo=JST)
# Reproduce missions register through the 8D8140 platform mask, Story missions through B09FE0.
MASK_CATEGORY, BOOLEAN_CATEGORY = 3, 0
# Real masks enable platform bits 0-12.
NATIVE_MASK = 8191
MENU_CLOSED = ("entrust_menu_closed", "The entrust menu is closed for every playable mission")
IMPLEMENTED_TYPES = frozenset((0, 1, 2, 4, 9, 10, 15, 16, 18, 19))
STATUS_KEYS = frozenset(("policy", "enabled", "reason", "hard_mode", "timed_conditions", "gate_quest"))
DISABLED_REASON = "Disabled for this test"
ROW_LABELS = MappingProxyType({
    1: "Challenge quest", 2: "Weekday quest", 6: "Subjugation quest", 8: "Reproduce quest", 9: "Devil advent",
    10: "Harlem quest", 11: "Raid quest", 14: "Tower", 15: "Practice quest", 16: "History quest", 18: "Empire quest",
    19: "Goddess revival quest",
})
# Subjugation and goddess revival starts preflight the presents their settlement may mail.
REWARD_PREFLIGHT_TYPES = frozenset((6, 19))


class DisabledPolicy(ModePolicy):
    name = "disabled"
    reason = DISABLED_REASON


def disable_policy(monkeypatch, quest_type):
    # Patch the class so fresh instances of the mode read as disabled too, not only the registered one.
    policy = type(quest_modes.policy(quest_type))
    monkeypatch.setattr(policy, "enabled", False)
    monkeypatch.setattr(policy, "reason", DISABLED_REASON)


def words(moment):
    if moment is None:
        return [0, 0]
    milliseconds = (moment - EPOCH) // timedelta(milliseconds=1)
    return [milliseconds & 0xFFFFFFFF, milliseconds >> 32]


def config(identifier, *, enable=True, start=None, end=None, quests="", rank=0):
    return {"MissionID": identifier, "Enable": enable, "StartDateTime": words(start), "EndDateTime": words(end),
            "RequestQuestIds": quests, "RequestClearRank": rank}


def windows(*configs, members=(), ranks=None, category=0, overrides=None):
    rows = [{"MissionID": mission, "QuestID": quest} for mission, quest in members]
    return MissionWindows({category: (list(configs), rows)}, ranks or {}, overrides=overrides)


def selector(*rejected, choose=None):
    return MissionSelector(lambda mission: mission.mission_id not in rejected, *MENU_CLOSED, choose)


def assert_code(code, callback, status=None):
    with pytest.raises(ProtocolError) as error:
        callback()
    assert error.value.code == code
    if status is not None:
        assert error.value.status == status


def hard_context(*, max_rank=3, flags=ALL_SLOTS | CLEAR_FLAG, rank=50, hard=HardModeSpec(200, 5, 281, 50),
                 treasures=(4, 1003, 2, 0, 1001062), catalog=None):
    quest = SimpleNamespace(quest_id=1002, treasures=treasures)
    mode = SimpleNamespace(quest_type=0, hard=hard)
    return StartContext(catalog, quest, mode, {1002: {"MaxRank": max_rank, "AchieveFlag": flags}}, rank, NOW, True, 1)


def test_registry_covers_every_native_quest_type_with_explicit_availability():
    registry = quest_modes.registry()
    assert tuple(registry) == tuple(sorted(MODE_SPECS))
    enabled = {quest_type for quest_type, entry in registry.items() if entry.enabled}
    assert IMPLEMENTED_TYPES <= enabled and not enabled & set(UNAVAILABLE)
    assert all(quest_type in entry.quest_types for quest_type, entry in registry.items())
    assert all(entry.reason for entry in registry.values() if not entry.enabled)
    assert all(isinstance(registry[quest_type], UnavailablePolicy) and registry[quest_type].reason == reason
               for quest_type, reason in UNAVAILABLE.items())
    assert registry[0].hard_mode and not registry[4].hard_mode
    assert quest_modes.registry() is registry


@pytest.mark.parametrize("value", [-1, True, 1.0, 2 ** 31])
def test_policy_lookup_rejects_values_outside_the_native_type_column(value):
    assert_code("quest_type_invalid", lambda: quest_modes.policy(value), 503)


def test_unregistered_and_reserved_types_refuse_to_start():
    unknown = quest_modes.policy(5)
    assert isinstance(unknown, UnavailablePolicy) and unknown.reason == "Unregistered quest type"
    for entry in (unknown, *map(quest_modes.policy, UNAVAILABLE), DisabledPolicy()):
        with pytest.raises(ProtocolError) as error:
            entry.require_enabled()
        assert (error.value.code, error.value.status) == ("quest_mode_unsupported", 501)
        assert str(error.value) == f"Quest mode is not implemented: {entry.reason}"


def test_mode_specs_follow_native_mission_categories():
    assert MODE_SPECS[0].resources == ("Story",) and MODE_SPECS[4].resources == ("Tutorial",)
    assert MODE_SPECS[8].resources == ("Reproduce", "History") and MODE_SPECS[18].resources == ("Empire", "Emergency")
    assert MODE_SPECS[3].mission_category is None and MODE_SPECS[3].resources == ()
    assert {key for key, spec in MODE_SPECS.items() if not spec.gold_counted} == {3, 6, 7, 11, 13, 14, 15}
    assert {key for key, spec in MODE_SPECS.items() if spec.score_mode} == {6, 7, 13}
    assert {key: spec.hard_rank for key, spec in MODE_SPECS.items() if spec.hard_rank is not None} == {0: 50, 3: 5, 9: 50, 18: 5}
    assert [key for key, spec in MODE_SPECS.items() if spec.entrust] == [0, 2, 8, 16, 18]


def test_support_status_reports_counts_with_mode_properties():
    status = quest_modes.support_status({0: 114, 4: 9})
    assert status[0]["enabled"] and status[0]["quests"] == 114 and status[4]["quests"] == 9
    assert status[0]["mission_resources"] == ["Story"] and status[0]["hard_rank"] == 50
    assert status[3]["quests"] == 0 and status[3]["reason"] and not status[3]["enabled"]
    assert status[11]["gold_counted"] is False and status[6]["score_mode"] is True
    assert "quests" not in quest_modes.support_status()[0]
    assert {key for key, entry in status.items() if entry["entrust"]} == ENTRUST_TYPES == {0, 2, 8, 16, 18}


def test_support_status_reports_local_rules_beside_the_unchanged_policy_status(monkeypatch):
    status = quest_modes.support_status()
    assert all(type(entry["local_rules"]) is dict for entry in status.values())
    assert all(status[quest_type]["local_rules"] == {} for quest_type in UNAVAILABLE)
    assert all(set(entry.status()) == STATUS_KEYS for entry in quest_modes.registry().values())
    rules = {"source": "synthetic"}
    monkeypatch.setattr(type(quest_modes.policy(0)), "local_rules", lambda self: rules)
    assert quest_modes.support_status()[0]["local_rules"] == rules
    assert set(quest_modes.policy(0).status()) == STATUS_KEYS


@pytest.mark.parametrize("start,end,expected", [
    (None, None, OPEN), (NOW + timedelta(seconds=1), NOW + timedelta(days=1), UPCOMING),
    (NOW, NOW + timedelta(days=1), OPEN), (NOW - timedelta(days=1), NOW, ENDED),
    (NOW - timedelta(days=1), None, ENDED), (None, NOW + timedelta(days=1), OPEN), (None, NOW, ENDED)])
def test_mission_window_state_compares_raw_native_bounds(start, end, expected):
    assert MissionWindow(1, 0, True, start, end).state(NOW) == expected


@pytest.mark.parametrize("start,end,moment,expected", [
    (NOW + HALF_SECOND, NOW + timedelta(days=1), NOW + 2 * HALF_SECOND - CLIENT_TICK, UPCOMING),
    (NOW + HALF_SECOND, NOW + timedelta(days=1), NOW + 2 * HALF_SECOND, OPEN),
    (None, NOW + HALF_SECOND, NOW + 2 * HALF_SECOND - CLIENT_TICK, OPEN),
    (None, NOW + HALF_SECOND, NOW + 2 * HALF_SECOND, ENDED),
    (QUANTIZED_SECOND, QUANTIZED_SECOND + timedelta(days=1), QUANTIZED_SECOND, UPCOMING),
    (QUANTIZED_SECOND, QUANTIZED_SECOND + timedelta(days=1), QUANTIZED_SECOND + 2 * HALF_SECOND, OPEN),
    (None, QUANTIZED_SECOND, QUANTIZED_SECOND + HALF_SECOND, OPEN),
    (None, QUANTIZED_SECOND, QUANTIZED_SECOND + 2 * HALF_SECOND, ENDED),
], ids=("subsecond_start_waits", "subsecond_start_next_second", "subsecond_end_holds", "subsecond_end_next_second",
        "quantized_start_waits", "quantized_start_next_second", "quantized_end_holds", "quantized_end_next_second"))
def test_mission_windows_compare_the_client_clock_of_whole_seconds_on_its_4_ms_grid(start, end, moment, expected):
    assert native_clock(QUANTIZED_SECOND) == QUANTIZED_SECOND - CLIENT_TICK
    window = MissionWindow(1, 0, True, start, end)
    assert window.state(moment) == window.state(moment.astimezone(timezone.utc)) == expected


def test_dated_mission_windows_require_an_aware_clock():
    dated = MissionWindow(1, 0, True, NOW, NOW + timedelta(days=1))
    assert_code("quest_clock_invalid", lambda: dated.state(NOW.replace(tzinfo=None)), 503)
    assert_code("quest_clock_invalid", lambda: dated.state("2026-11-01"), 503)
    assert MissionWindow(1, 0, True, None, None).state(NOW) == OPEN


def test_mission_enable_accepts_native_booleans_and_platform_masks():
    assert MissionWindow(1, BOOLEAN_CATEGORY, True, None, None).enabled(7) is True
    assert MissionWindow(1, BOOLEAN_CATEGORY, False, None, None).enabled(1) is False
    masked = MissionWindow(1, MASK_CATEGORY, 0b10, None, None)
    assert masked.enabled(1) and not masked.enabled(0)
    for code in (-1, 32, True):
        assert_code("quest_platform_invalid", lambda: masked.enabled(code))


def test_only_the_platform_mask_loaders_read_enable_per_platform():
    # 8C7CE0, 8D9AB0, 8D8140, 8C67A0, 8D3AB0, 8C4190 and 8DB230 test the mask; the other loaders call B09FE0.
    assert PLATFORM_MASK_CATEGORIES == {1, 2, 3, 8, 10, 11, 12} and PLATFORM_MASK_CATEGORIES < set(MISSION_CATEGORIES)


@pytest.mark.parametrize("category,enable,registered", [
    (0, 0b10, PLATFORM_CODES), (14, 1 << 5, PLATFORM_CODES), (16, NATIVE_MASK, PLATFORM_CODES),
    (5, INT32_MIN, PLATFORM_CODES), (7, 0, ()), (13, False, ()), (17, True, PLATFORM_CODES),
    (3, True, (0,)), (8, False, ()), (2, 2047, range(11)), (10, NATIVE_MASK, range(13)), (11, INT32_MIN, (31,)),
    (12, 1, (0,)), (1, 0, ()),
], ids=("story_mask", "history_mask", "empire_mask", "daily_sign_bit", "tutorial_zero", "training_false",
        "goddess_true", "reproduce_true", "devil_false", "subjugation_legacy_mask", "raid_mask", "assault_sign_bit",
        "tower_one", "emergency_zero"))
def test_mission_windows_read_enable_by_their_category_loader(category, enable, registered):
    window = MissionWindows({category: ([config(1, enable=enable)], [])}, {}).mission(1)
    assert [code for code in PLATFORM_CODES if window.enabled(code)] == list(registered)
    assert window.enable is enable and window.listed is bool(enable)


@pytest.mark.parametrize("code", [-1, 32, True, "1", 1.0, None])
def test_platform_codes_must_be_native_integers_below_32(code):
    with pytest.raises(ProtocolError, match=PLATFORM_MESSAGE) as error:
        require_platform(code)
    assert (error.value.code, error.value.status) == ("quest_platform_invalid", 400)


def test_platform_masks_validate_codes_only_for_mask_categories():
    assert [require_platform(code) for code in (0, 31)] == [0, 31]
    sign_bit = MissionWindow(1, MASK_CATEGORY, INT32_MIN, None, None)
    assert sign_bit.enabled(31) and not sign_bit.enabled(30)
    with pytest.raises(ProtocolError, match=PLATFORM_MESSAGE) as error:
        sign_bit.enabled("1")
    assert (error.value.code, error.value.status) == ("quest_platform_invalid", 400)
    assert MissionWindow(1, BOOLEAN_CATEGORY, True, None, None).enabled(32) is True
    assert MissionWindow(1, BOOLEAN_CATEGORY, INT32_MIN, None, None).enabled(32) is True


def test_mission_windows_index_members_by_category_and_minimum_rank():
    subject = windows(config(1), config(2), config(4), members=((1, 1001), (1, 1002), (2, 1002), (3, 1003)),
                      ranks={1001: 5, 1002: 3})
    assert [mission.mission_id for mission in subject.missions(1002)] == [1, 2]
    assert subject.missions(1003) == ()
    assert (subject.minimum_rank(1), subject.minimum_rank(2), subject.minimum_rank(4)) == (3, 3, NO_MISSION_RANK)
    assert_code("quest_mission_unknown", lambda: subject.mission(9), 409)
    split = MissionWindows({0: ([config(1)], []), 7: ([config(2)], [{"MissionID": 1, "QuestID": 3001},
                                                                    {"MissionID": 2, "QuestID": 3001}])}, {3001: 0})
    assert [mission.mission_id for mission in split.missions(3001)] == [2]
    assert split.minimum_rank(2) == 0


def test_mission_windows_expose_categories_sorted_members_and_missions():
    subject = MissionWindows({16: ([config(940002), config(940001)],
                                   [{"MissionID": 940001, "QuestID": 7008}, {"MissionID": 940001, "QuestID": 7001},
                                    {"MissionID": 940001, "QuestID": 7008}, {"MissionID": 300001, "QuestID": 7003}]),
                              1: ([config(300001)], [{"MissionID": 300001, "QuestID": 7004}])}, {})
    assert subject.categories == (16, 1)
    assert [mission.mission_id for mission in subject.ordered()] == [300001, 940001, 940002]
    assert (subject.members(940001), subject.members(940002), subject.members(300001)) == ((7001, 7008), (), (7004,))
    assert_code("quest_mission_unknown", lambda: subject.members(5), 409)


def test_mission_tags_read_as_zero_when_their_columns_are_absent():
    subject = windows({**config(2), "TagID": 302, "TagIDRange": 10}, config(1), {**config(3), "TagID": -5})
    assert [(mission.mission_id, mission.tag, mission.tag_span) for mission in subject.ordered()] == [
        (1, 0, 0), (2, 302, 10), (3, -5, 0)]


@pytest.mark.parametrize("tag,span,inside,outside", [
    (302, 0, (302,), (301, 303)),
    (900, 12, (900, 906, 912), (899, 913)),
    (100, -1, (), (99, 100)),
    (INT32_MAX - 15, 32, (), (INT32_MAX - 15, INT32_MAX, 0)),
    (INT32_MIN + 4, -8, (INT32_MIN + 4, 0, INT32_MAX - 3), (INT32_MIN + 3, INT32_MAX - 2)),
])
def test_mission_tag_spans_wrap_their_32_bit_end_before_the_signed_compare(tag, span, inside, outside):
    mission = MissionWindow(1, 0, True, None, None, tag=tag, tag_span=span)
    assert [mission.covers(value) for value in inside] == [True] * len(inside)
    assert [mission.covers(value) for value in outside] == [False] * len(outside)


def test_tag_span_ends_are_signed_32_bit_sums():
    assert [tag_span_end(*pair) for pair in ((302, 0), (INT32_MAX, 1), (INT32_MIN, -1), (-5, 3))] == [
        302, INT32_MIN, INT32_MAX, -2]


@pytest.mark.parametrize("changes", [{"MissionID": 0}, {"Enable": 1.5}, {"Enable": "1"}, {"StartDateTime": [1]},
                                     {"StartDateTime": [0xFFFFFFFF, 0xFFFFFFFF]}, {"EndDateTime": [0, 0x20000]},
                                     {"RequestQuestIds": "1001,,1002"}, {"RequestQuestIds": "0"},
                                     {"RequestClearRank": -1}, {"RequestQuestIds": 1001}, {"TagID": True},
                                     {"TagID": "302"}, {"TagID": None}, {"TagIDRange": 1.5}, {"TagIDRange": 2 ** 31}])
def test_mission_windows_reject_invalid_native_rows(changes):
    with pytest.raises(ValueError):
        windows({**config(1), **changes})


def test_mission_windows_reject_duplicate_missions_and_invalid_members():
    with pytest.raises(ValueError):
        windows(config(1), config(1))
    with pytest.raises(ValueError):
        windows(config(1), members=((1, 0),))


def test_require_playable_reports_the_most_specific_native_lock():
    subject = windows(config(1, quests="1001"), config(2, enable=False),
                      config(3, start=NOW + timedelta(hours=1), end=NOW + timedelta(days=1)),
                      members=((1, 1002), (2, 1003), (3, 1004)), ranks={1002: 10, 1003: 1, 1004: 1})
    play = {"now": NOW, "platform_code": 1}
    assert_code("quest_rank_required", lambda: subject.require_playable(1002, rank=9, records={}, **play), 409)
    assert_code("quest_locked", lambda: subject.require_playable(1002, rank=10, records={1001: {"MaxRank": 2}}, **play), 409)
    assert subject.require_playable(1002, rank=10, records={1001: {"MaxRank": 3}}, **play).mission_id == 1
    for quest_id in (1003, 1004, 1005):
        assert_code("quest_closed", lambda: subject.require_playable(quest_id, rank=99, records={}, **play), 409)
    ranked = windows(config(1, quests="1001,1002", rank=1), members=((1, 1003),), ranks={1003: 1})
    assert ranked.require_playable(1003, rank=1, records={1001: {"MaxRank": 1}, 1002: {"MaxRank": 1}}, **play).mission_id == 1
    assert_code("quest_locked", lambda: ranked.require_playable(1003, rank=1, records={1001: {"MaxRank": 1}}, **play))


def test_require_playable_uses_any_open_mission_that_lists_the_quest():
    subject = windows(config(1, end=NOW), config(2), members=((1, 1001), (2, 1001)), ranks={1001: 1})
    assert subject.require_playable(1001, now=NOW, rank=1, records={}, platform_code=1).mission_id == 2


STAGED_PLAY = {"now": NOW, "rank": 5, "platform_code": 1}


def staged(*missions):
    # Every mission lists 1002 (rank 10); 1 is disabled and 4 also lists 1009 (rank 1) but requests quest 1001.
    rows = {1: config(1, enable=False), 2: config(2), 3: config(3), 4: config(4, quests="1001")}
    members = [(mission, 1002) for mission in missions] + [(4, 1009)]
    return windows(*(rows[mission] for mission in missions), members=members, ranks={1002: 10, 1009: 1})


@pytest.mark.parametrize("missions,rejected,code", [
    ((1,), (2,), "quest_closed"),
    ((1, 2), (2,), "entrust_menu_closed"),
    ((2, 3), (2,), "quest_rank_required"),
    ((3, 4), (4,), "quest_rank_required"),
    ((1, 2, 3, 4), (2,), "quest_locked"),
])
def test_selectors_refuse_missions_between_the_window_and_rank_stages(missions, rejected, code):
    subject = staged(*missions)
    assert_code(code, lambda: subject.require_playable(1002, records={}, selector=selector(*rejected), **STAGED_PLAY), 409)


def test_selector_refusals_carry_the_selector_code_and_message():
    with pytest.raises(ProtocolError, match=f"^{MENU_CLOSED[1]}$") as error:
        staged(1, 2).require_playable(1002, records={}, selector=selector(2), **STAGED_PLAY)
    assert (error.value.code, error.value.status) == (MENU_CLOSED[0], 409)
    assert_code("quest_rank_required", lambda: staged(1, 2).require_playable(1002, records={}, **STAGED_PLAY), 409)


def test_selected_missions_still_face_rank_and_requested_quest_locks():
    subject, cleared = staged(1, 2, 3, 4), {1001: {"MaxRank": 3}}
    assert subject.require_playable(1002, records=cleared, selector=selector(2), **STAGED_PLAY).mission_id == 4
    assert_code("quest_rank_required", lambda: subject.require_playable(
        1002, records=cleared, selector=selector(2, 4), **STAGED_PLAY), 409)


def test_choosing_selectors_receive_every_playable_mission_in_mission_order():
    subject = windows(config(6), config(5), config(7, enable=False), members=((5, 1002), (6, 1002), (7, 1002)),
                      ranks={1002: 1})
    play, seen = {"now": NOW, "rank": 1, "records": {}, "platform_code": 1}, []

    def last(playable):
        seen.append(tuple(mission.mission_id for mission in playable))
        return playable[-1]

    assert subject.require_playable(1002, selector=selector(choose=last), **play).mission_id == 6
    assert seen == [(5, 6)]
    assert subject.require_playable(1002, selector=selector(), **play).mission_id == 5
    assert subject.require_playable(1002, selector=selector(5, choose=last), **play).mission_id == 6
    assert subject.require_playable(1002, **play).mission_id == 5


@pytest.mark.parametrize("choose", [lambda playable: replace(playable[0]), lambda playable: None,
                                    lambda playable: MissionWindow(5, 0, True, None, None)])
def test_choosing_selectors_must_return_one_of_the_playable_missions(choose):
    subject = windows(config(5), members=((5, 1002),), ranks={1002: 1})
    assert_code("quest_mission_selector_invalid", lambda: subject.require_playable(
        1002, now=NOW, rank=1, records={}, platform_code=1, selector=selector(choose=choose)), 503)


def test_selectors_without_choose_stop_at_the_first_playable_mission():
    subject, asked = windows(config(5), config(6), members=((5, 1002), (6, 1002)), ranks={1002: 1}), []

    def accepts(mission):
        asked.append(mission.mission_id)
        return True

    play = {"now": NOW, "rank": 1, "records": {}, "platform_code": 1}
    assert subject.require_playable(1002, selector=MissionSelector(accepts, *MENU_CLOSED), **play).mission_id == 5
    assert asked == [5]
    choosing = MissionSelector(accepts, *MENU_CLOSED, lambda playable: playable[-1])
    assert subject.require_playable(1002, selector=choosing, **play).mission_id == 6 and asked == [5, 5, 6]


def test_selectors_pick_the_first_playable_mission_unless_they_choose():
    first, second = MissionWindow(5, 0, True, None, None), MissionWindow(6, 0, True, None, None)
    assert selector().pick((first, second)) is first
    assert selector(choose=lambda playable: playable[1]).pick((first, second)) is second


def test_selector_errors_pass_through_and_closed_missions_skip_the_selector():
    failure = ProtocolError("entrust_source_invalid", "The entrust menu gate is ambiguous", 503)

    def broken(mission):
        raise failure

    play = {"now": NOW, "rank": 1, "records": {}, "platform_code": 1,
            "selector": MissionSelector(broken, *MENU_CLOSED)}
    with pytest.raises(ProtocolError) as error:
        windows(config(5), members=((5, 1002),), ranks={1002: 1}).require_playable(1002, **play)
    assert error.value is failure
    closed = windows(config(5, enable=False), members=((5, 1002),), ranks={1002: 1})
    assert_code("quest_closed", lambda: closed.require_playable(1002, **play), 409)


def test_window_overrides_replace_native_bounds_after_validation():
    expired = config(1, start=NOW - timedelta(days=9), end=NOW - timedelta(days=8))
    reopened = windows(expired, members=((1, 1001),), ranks={1001: 1},
                       overrides={1: (NOW - timedelta(hours=1), NOW + timedelta(hours=1))})
    assert reopened.mission(1).state(NOW) == OPEN
    assert windows(config(1, start=NOW - timedelta(days=1)), overrides={1: (None, None)}).mission(1).state(NOW) == OPEN
    assert validate_window_overrides(None) == {}


@pytest.mark.parametrize("overrides", [[], {0: (None, None)}, {1: (None,)}, {1: (NOW, None)}, {1: (NOW, NOW)},
                                       {1: (NOW.replace(tzinfo=None), NOW + timedelta(days=1))}, {1: ("now", None)}])
def test_window_overrides_require_positive_ids_and_aware_ordered_bounds(overrides):
    with pytest.raises(ValueError):
        validate_window_overrides(overrides)


def test_special_conditions_count_stars_clears_and_perfect_ranges():
    records = {1001: {"MaxRank": 3, "AchieveFlag": CLEAR_FLAG}, 1002: {"MaxRank": 2, "AchieveFlag": CLEAR_FLAG},
               1003: {"MaxRank": 0, "AchieveFlag": 0}, 2001: {"MaxRank": 3, "AchieveFlag": CLEAR_FLAG}}
    assert condition_allows(SpecialCondition(1, 5, 1001, 1003), records)
    assert not condition_allows(SpecialCondition(1, 6, 1001, 1003), records)
    assert condition_allows(SpecialCondition(2, 2, 1001, 1003), records)
    assert not condition_allows(SpecialCondition(2, 3, 1001, 1003), records)
    assert condition_allows(SpecialCondition(3, 0, 2001, 2001), records)
    assert not condition_allows(SpecialCondition(3, 0, 1001, 1002), records)
    assert condition_allows(SpecialCondition(4, 99, 1001, 1001), records)
    assert condition_allows(SpecialCondition(0, 99, 1001, 1001), records)
    assert not condition_allows(SpecialCondition(1, 1, 1003, 1001), records)
    assert condition_allows(SpecialCondition(3, 0, 1003, 1001), records)
    conditions = {5001: (SpecialCondition(1, 5, 1001, 1003), SpecialCondition(3, 0, 2001, 2001))}
    assert special_condition_satisfied(records, 5001, conditions)
    assert special_condition_satisfied({}, 5002, conditions)
    assert not special_condition_satisfied({**records, 2001: {"MaxRank": 2}}, 5001, conditions)


def test_a_quest_counts_as_cleared_only_through_its_saved_clear_flag():
    records = {1001: {"MaxRank": 0, "AchieveFlag": CLEAR_FLAG}, 1002: {"MaxRank": 3, "AchieveFlag": THREE_STAR_FLAG},
               1003: {"MaxRank": 3, "AchieveFlag": -0x80000000 | CLEAR_FLAG}, 1004: {"MaxRank": 3}}
    assert [quest_id for quest_id in (1001, 1002, 1003, 1004, 1005) if quest_cleared(records, quest_id)] == [1001, 1003]


def condition_rows(*values):
    return [dict(zip(CONDITION_FIELDS, row)) for row in values]


def test_special_condition_resource_requires_the_native_schema():
    fields = tuple((name, 1) for name in CONDITION_FIELDS)
    parsed = parse_special_conditions(table(fields, condition_rows((5001, 1, 5, 1001, 1003), (5001, 3, 0, 2001, 2001))))
    assert parsed == {5001: (SpecialCondition(1, 5, 1001, 1003), SpecialCondition(3, 0, 2001, 2001))}
    with pytest.raises(ValueError):
        parse_special_conditions(table(tuple(reversed(fields)), condition_rows((5001, 1, 5, 1001, 1003))))
    unusual = parse_special_conditions(table(fields, condition_rows((5001, 4, -1, 1003, 1001))))
    assert unusual == {5001: (SpecialCondition(4, -1, 1003, 1001),)}


def test_four_star_unlock_requires_three_stars_every_nonempty_treasure_and_rank():
    story = quest_modes.policy(0)
    story.check_hard(hard_context())
    story.check_hard(hard_context(treasures=(4, 0, 0, 0, 0), flags=0x10000))
    for context, message in ((hard_context(max_rank=2), "three stars"),
                             (hard_context(flags=0x11100 | CLEAR_FLAG), "nonempty treasure"),
                             (hard_context(rank=49), "rank is below")):
        with pytest.raises(ProtocolError, match=message) as error:
            story.check_hard(context)
        assert error.value.code == "quest_hard_locked"
    assert_code("quest_hard_unavailable", lambda: story.check_hard(hard_context(hard=None)), 409)
    assert_code("quest_hard_unavailable", lambda: quest_modes.policy(4).check_hard(hard_context()), 409)


def test_check_start_applies_windows_special_conditions_then_four_star_rules():
    catalog = SimpleNamespace(mission_windows=lambda quest_type: windows(config(1), members=((1, 1002),), ranks={1002: 1}),
                              special_conditions=lambda: {1002: (SpecialCondition(2, 1, 1001, 1001),)})
    context = hard_context(catalog=catalog)
    assert_code("quest_locked", lambda: quest_modes.policy(0).check_start(context), 409)
    cleared = replace(context, records={**context.records, 1001: {"MaxRank": 1, "AchieveFlag": CLEAR_FLAG}})
    quest_modes.policy(0).check_start(cleared)
    assert_code("quest_hard_locked", lambda: quest_modes.policy(0).check_start(replace(cleared, rank=49)), 409)
    quest_modes.policy(0).check_start(replace(cleared, rank=1, hard=False))
    assert_code("quest_mode_unsupported", lambda: DisabledPolicy().check_start(cleared), 501)
    closed = replace(cleared, catalog=SimpleNamespace(mission_windows=lambda quest_type: windows(config(1, enable=False),
        members=((1, 1002),), ranks={1002: 1}), special_conditions=lambda: {}))
    assert_code("quest_closed", lambda: quest_modes.policy(0).check_start(closed), 409)


def test_check_start_hands_its_context_selector_to_mission_selection():
    catalog = SimpleNamespace(special_conditions=lambda: {}, mission_windows=lambda quest_type: windows(
        config(1), config(2), members=((1, 1002), (2, 1002)), ranks={1002: 1}))
    context, story = replace(hard_context(catalog=catalog), hard=False), quest_modes.policy(0)
    assert story.check_start(context).mission_id == 1
    assert story.check_start(replace(context, selector=selector(1))).mission_id == 2
    assert_code(MENU_CLOSED[0], lambda: story.check_start(replace(context, selector=selector(1, 2))), 409)


class RecordingPolicy(ModePolicy):
    name = "recording"
    enabled = True

    def __init__(self):
        self.contexts = []

    def check_start(self, context):
        self.contexts.append(context)
        return MissionWindow(7, 0, True, None, None)


def admission(*, prerequisite=1001, quest_rank=5, rank=5, records=None, catalog=None):
    quest = SimpleNamespace(quest_id=1002, prerequisite=prerequisite, rank=quest_rank, treasures=(0,) * 5)
    return StartContext(catalog, quest, SimpleNamespace(quest_type=0, hard=None), records or {}, rank, NOW, False, 1)


PREREQUISITE_CLEARED = {1001: {"MaxRank": 0, "AchieveFlag": CLEAR_FLAG}}


@pytest.mark.parametrize("context,code,message", [
    (admission(rank=4), "quest_locked", "The prerequisite quest has not been cleared"),
    (admission(records={1001: {"MaxRank": 3, "AchieveFlag": THREE_STAR_FLAG}}), "quest_locked",
     "The prerequisite quest has not been cleared"),
    (admission(rank=4, records=PREREQUISITE_CLEARED), "quest_rank_required", "Player rank is below the quest requirement"),
])
def test_admit_checks_the_prerequisite_then_quest_rank_before_the_mode(context, code, message):
    policy = RecordingPolicy()
    with pytest.raises(ProtocolError, match=f"^{message}$") as error:
        admit(policy, context)
    assert (error.value.code, error.value.status) == (code, 409)
    assert policy.contexts == []


def test_admit_returns_the_mission_chosen_by_the_mode_entry_rules():
    policy, allowed = RecordingPolicy(), admission(records=PREREQUISITE_CLEARED)
    assert admit(policy, allowed).mission_id == 7 and policy.contexts[0] is allowed
    assert admit(policy, admission(prerequisite=0)).mission_id == 7
    closed = SimpleNamespace(special_conditions=lambda: {}, mission_windows=lambda quest_type: windows(
        config(1, enable=False), members=((1, 1002),), ranks={1002: 1}))
    assert_code("quest_closed", lambda: admit(quest_modes.policy(0), admission(prerequisite=0, catalog=closed)), 409)


NATIVE_DAY = NOW.date().toordinal() - 1


@pytest.mark.parametrize("fields,expected", [
    ({}, True), ({"_HoldingParam": 0b1}, True), ({"_HoldingParam": 0b10}, False),
    ({"_HoldingParam": 0b10, "OpenWeek": 1}, False), ({"_HoldingParam": -1}, True),
    ({"OpenWeek": 1}, True), ({"OpenWeek": 2}, False), ({"OpenWeek": 7}, False),
    ({"OpenWeek": 8}, True), ({"OpenWeek": 9}, False), ({"OpenWeek": 10}, False),
    ({"StartOpenDay": NATIVE_DAY, "EndOpenDay": NATIVE_DAY + 1}, True),
    ({"StartOpenDay": NATIVE_DAY + 1, "EndOpenDay": NATIVE_DAY + 2}, False),
    ({"StartOpenDay": NATIVE_DAY - 1, "EndOpenDay": NATIVE_DAY}, False),
    ({"StartOpenDay": NATIVE_DAY + 1, "EndOpenDay": 0}, True),
])
def test_daily_opening_follows_the_holding_mask_open_week_and_half_open_days(fields, expected):
    # 2026-11-01 is a Sunday, the first native weekday.
    assert quest_open_on(fields, NOW) is expected


WEEKDAYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
NATIVE_WEEK = {name: NOW + timedelta(days=offset) for offset, name in enumerate(WEEKDAYS)}


@pytest.mark.parametrize("week,open_days", [
    *((number, {name}) for number, name in enumerate(WEEKDAYS, start=1)),
    (8, {"sunday", "saturday"}), (9, {"tuesday", "friday"}), (10, {"thursday", "friday"}),
])
def test_open_week_values_open_on_their_native_weekdays(week, open_days):
    assert [moment.isoweekday() for moment in NATIVE_WEEK.values()] == [7, 1, 2, 3, 4, 5, 6]
    assert {name for name, moment in NATIVE_WEEK.items() if quest_open_on({"OpenWeek": week}, moment)} == open_days


def test_daily_opening_uses_the_jst_calendar_and_native_integers():
    assert quest_open_on({"OpenWeek": 1}, datetime(2026, 10, 31, 16, tzinfo=timezone.utc))
    assert not quest_open_on({"OpenWeek": 1}, datetime(2026, 10, 31, 14, tzinfo=timezone.utc))
    assert_code("quest_clock_invalid", lambda: quest_open_on({}, NOW.replace(tzinfo=None)), 503)
    for value in (True, 1.0, 2 ** 31):
        assert_code("quest_definition_invalid", lambda: quest_open_on({"OpenWeek": value}, NOW), 503)


MONDAY_PAST_MIDNIGHT = datetime(2026, 11, 2, 0, 3, tzinfo=JST).astimezone(timezone.utc)
SUNDAY_LAST_SECOND = datetime(2026, 11, 1, 23, 59, 59, tzinfo=JST).astimezone(timezone.utc)
MONDAY_BIT, SUNDAY_BIT = 0b10, 0b1


def test_daily_opening_turns_the_native_day_and_weekday_at_jst_midnight():
    monday = NATIVE_DAY + 1
    assert quest_open_on({"OpenWeek": 2}, MONDAY_PAST_MIDNIGHT) and not quest_open_on({"OpenWeek": 1}, MONDAY_PAST_MIDNIGHT)
    assert quest_open_on({"OpenWeek": 1}, SUNDAY_LAST_SECOND) and not quest_open_on({"OpenWeek": 2}, SUNDAY_LAST_SECOND)
    assert quest_open_on({"_HoldingParam": MONDAY_BIT}, MONDAY_PAST_MIDNIGHT)
    assert not quest_open_on({"_HoldingParam": SUNDAY_BIT}, MONDAY_PAST_MIDNIGHT)
    assert quest_open_on({"StartOpenDay": monday, "EndOpenDay": monday + 1}, MONDAY_PAST_MIDNIGHT)
    assert not quest_open_on({"StartOpenDay": monday, "EndOpenDay": monday + 1}, SUNDAY_LAST_SECOND)
    assert not quest_open_on({"StartOpenDay": monday - 1, "EndOpenDay": monday}, MONDAY_PAST_MIDNIGHT)


class TimedPolicy(ModePolicy):
    name = "timed"
    quest_types = (2,)
    enabled = True
    timed_conditions = True


def timed_context(now, **fields):
    catalog = SimpleNamespace(mission_windows=lambda quest_type: windows(config(5), members=((5, 101),), ranks={101: 1}),
                              row=lambda quest_id: fields, special_conditions=lambda: {})
    return StartContext(catalog, SimpleNamespace(quest_id=101, treasures=(0,) * 5),
                        SimpleNamespace(quest_type=2, hard=None), {}, 1, now, False, 1)


def test_timed_modes_accept_a_start_just_after_the_jst_day_changes():
    monday = datetime(2026, 11, 2, tzinfo=JST)
    policy = TimedPolicy()
    assert policy.check_start(timed_context(monday + timedelta(seconds=299), OpenWeek=1)).mission_id == 5
    assert_code("quest_closed", lambda: policy.check_start(timed_context(monday + timedelta(seconds=300), OpenWeek=1)), 409)
    assert policy.check_start(timed_context(monday + timedelta(hours=1), OpenWeek=2)).mission_id == 5


def test_day_open_extends_the_previous_jst_day_by_the_departure_grace():
    monday, policy = datetime(2026, 11, 2, tzinfo=JST), TimedPolicy()
    sunday = SimpleNamespace(row=lambda quest_id: {"OpenWeek": 1})
    monday_only = SimpleNamespace(row=lambda quest_id: {"OpenWeek": 2})
    assert policy.day_open(sunday, 101, monday + timedelta(minutes=3))
    assert not policy.day_open(sunday, 101, monday + DAY_OPEN_GRACE)
    assert not policy.day_open(monday_only, 101, monday - timedelta(seconds=1))
    assert policy.day_open(monday_only, 101, monday + timedelta(minutes=3))
    assert quest_modes.policy(0).day_open(None, 101, monday + DAY_OPEN_GRACE)


def test_key_quests_lock_story_and_empire_missions_until_cleared():
    keyed = {**config(1), "KeyQuestID": 1001}
    play = {"now": NOW, "rank": 1, "platform_code": 1}
    story = windows(keyed, members=((1, 1002),), ranks={1002: 1})
    assert story.mission(1).key_quest == 1001
    assert_code("quest_locked", lambda: story.require_playable(
        1002, records={1001: {"MaxRank": 3, "AchieveFlag": 0}}, **play), 409)
    assert story.require_playable(1002, records={1001: {"MaxRank": 0, "AchieveFlag": CLEAR_FLAG}}, **play).mission_id == 1
    assert windows(keyed, members=((1, 1002),), ranks={1002: 1}, category=16).mission(1).key_quest == 1001
    challenge = windows(keyed, members=((1, 1002),), ranks={1002: 1}, category=6)
    assert challenge.mission(1).key_quest == 0 and challenge.require_playable(1002, records={}, **play).mission_id == 1
    with pytest.raises(ValueError):
        windows({**config(1), "KeyQuestID": -1})
    assert windows({**config(1), "KeyQuestID": -1}, category=6).mission(1).key_quest == 0


def test_prefixed_categories_name_maps_after_their_single_listed_owner():
    subjugation = windows(config(9001), config(9002, enable=False), members=((9001, 5001), (9002, 5001)), category=2)
    assert subjugation.archive(5001, 12) == ("Map9001_0012.aar", 9001)
    assert windows(config(9001, enable=0b10), members=((9001, 5001),), category=2).archive(5001, 3) == ("Map9001_0003.aar", 9001)
    assert windows(config(5), members=((5, 101),), category=5).archive(101, 12) == ("Map0012.aar", None)
    closed = windows(config(9001, enable=False), members=((9001, 5001),), category=2)
    assert_code("quest_closed", lambda: closed.archive(5001, 12), 409)
    shared = windows(config(9001), config(9002), members=((9001, 5001), (9002, 5001)), category=2)
    assert_code("quest_mission_ambiguous", lambda: shared.archive(5001, 12), 503)
    assert (quest_modes.map_archive(12), quest_modes.map_archive(12, 9001)) == ("Map0012.aar", "Map9001_0012.aar")


def test_policies_claim_specs_of_their_own_quest_type_and_evaluate_them():
    spec = QuestClearSpec(1, "devil", (9,), frozenset({901}))
    devil, story = quest_modes.policy(9), quest_modes.policy(0)
    payload = {"quest_id": 901, "quest_type": 9, "stars": 3, "hard": False}
    assert devil.claims(spec) and not story.claims(spec)
    assert devil.mission_matcher(None, payload, spec) is True
    assert devil.mission_matcher(None, {**payload, "quest_id": 902}, spec) is False
    assert story.mission_matcher(None, payload, spec) is None and devil.mission_matcher(None, payload, None) is None
    assert not devil.claims(QuestClearSpec(2, "default", None, None))
    reserved = QuestClearSpec(3, "unavailable", (3,), None)
    assert quest_modes.policy(3).claims(reserved) and not quest_modes.policy(7).claims(reserved)


def test_mode_hooks_default_to_the_unchanged_attempt():
    policy, state = quest_modes.policy(0), {"tables": {}}
    assert policy.prepare_start(None, state) == (state, ())
    assert policy.settle(SimpleNamespace(state=state)) == SettleOutcome(state)
    assert policy.status()["gate_quest"] is None and quest_modes.policy(4).status()["gate_quest"] == 1004


def test_start_contexts_keep_their_positional_battle_fields():
    context = StartContext("catalog", "quest", "mode", {}, 7, NOW, False, 1)
    assert (context.state, context.request, context.services, context.selector) == (None, None, ModeServices(), None)
    chosen = selector()
    assert StartContext("catalog", "quest", "mode", {}, 7, NOW, False, 1, {}, "request", ModeServices(),
                        chosen).selector is chosen


def test_score_and_boost_hooks_default_to_a_plain_run_without_a_score():
    policy = ModePolicy()
    assert policy.start_boost(hard_context()) == UNBOOSTED == 1
    scoring = ScoreContext("catalog", "quest", "mode", "request", {"MaxRank": 0, "AchieveFlag": 0}, UNBOOSTED, NOW, None)
    with pytest.raises(ProtocolError, match="^Score modes require a score hook$") as error:
        policy.score_result(scoring)
    assert (error.value.code, error.value.status) == ("quest_mode_hook_invalid", 503)
    assert policy.local_rules() == {} and policy.local_rules() is not policy.local_rules()


def test_mode_entry_gates_default_open_and_only_the_empire_mode_adds_one():
    assert ModePolicy().entry_open(None, now=NOW, platform_code=PLATFORM_CODES.stop) is True
    assert quest_modes.policy(0).entry_open(SimpleNamespace(), now=NOW.replace(tzinfo=None), platform_code=1) is True
    gated = {quest_type for quest_type, entry in quest_modes.registry().items()
             if type(entry).entry_open is not ModePolicy.entry_open}
    assert gated == {18}


def test_numeric_limits_are_the_shared_native_number_primitives():
    assert all(getattr(quest_modes, name) is getattr(native_numbers, name)
               for name in ("UINT32_MASK", "int32", "signed32"))
    assert all(value is getattr(native_numbers, name) for name, value in vars(quest_modes).items()
               if not name.startswith("_") and hasattr(native_numbers, name))


def test_hook_constants_keep_their_saved_values_wherever_the_service_and_the_modes_read_them():
    assert (MODE_PROGRESS, MAX_BOOST, BOOSTS, MAX_OBTAINED) == ("mode_progress", 3, range(UNBOOSTED, 4), 7)
    assert all(getattr(module, name) is getattr(quest_modes, name)
               for module, names in ((quest_service, ("MODE_PROGRESS", "MAX_BOOST", "BOOSTS", "MAX_OBTAINED")),
                                     (subjugation_rules, ("MODE_PROGRESS", "MAX_BOOST")))
               for name in names)


def imported_names(module) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(Path(module.__file__).read_bytes())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = ".".join(filter(None, (module.__package__ if node.level else None, node.module)))
            names.update((base, *(f"{base}.{alias.name}" for alias in node.names)))
    return names


def test_mode_modules_read_the_hook_conventions_without_importing_the_service():
    # The registry loads the modes lazily, so a mode importing the service never fails, it only reverses the layering.
    modules = (*(importlib.import_module(f"{quest_modes.__package__}.{name}") for name in quest_modes.MODE_MODULES),
               subjugation_rules, tower_catalog, tower_service, tower_state)
    assert [module.__name__ for module in modules if quest_service.__name__ in imported_names(module)] == []


@pytest.mark.parametrize("value", [None, True, 1.0, "1", 0, 2])
def test_row_values_must_match_their_native_integers_in_contract_order(value):
    values = MappingProxyType({"Type": 1, "UnitList": 0})
    assert require_row_values({"Type": 1, "UnitList": 0, "BonusType": "free"}, values, "Synthetic quest") is None
    with pytest.raises(ProtocolError, match="^Synthetic quest field Type differs from the native contract$") as error:
        require_row_values({"Type": value, "UnitList": value}, values, "Synthetic quest")
    assert (error.value.code, error.value.status) == ("quest_definition_invalid", 503)
    with pytest.raises(ProtocolError, match="^Synthetic quest field UnitList differs from the native contract$"):
        require_row_values({"Type": 1}, values, "Synthetic quest")


@pytest.mark.parametrize("quest_type,label", ROW_LABELS.items())
def test_every_strict_row_contract_names_its_mode_and_the_first_differing_field(quest_type, label):
    with pytest.raises(ProtocolError) as error:
        quest_modes.policy(quest_type).validate_row({"Type": None}, None)
    assert (error.value.code, error.value.status, str(error.value)) == (
        "quest_definition_invalid", 503, f"{label} field Type differs from the native contract")


def test_only_modes_that_preflight_their_rewards_answer_the_delivery_hook():
    registry, services = quest_modes.registry(), ModeServices()
    answering = {quest_type for quest_type, entry in registry.items()
                 if type(entry).undeliverable_rewards is not ModePolicy.undeliverable_rewards}
    assert answering == REWARD_PREFLIGHT_TYPES
    assert all(entry.undeliverable_rewards(None, services) is None
               for quest_type, entry in registry.items() if quest_type not in answering)


SCORE, BOOST = 1127, 3
PACKED_SCORE = (SCORE << 12) | SCORE


def test_mode_services_and_settle_contexts_keep_their_positional_fields():
    services = ModeServices("presents", "card_status")
    assert (services.presents, services.card_status) == ("presents", "card_status")
    assert (services.possessions, services.tower, services.subjugation) == (None, None, None)
    context = SettleContext("catalog", "quest", "mode", "request", {}, False, 3, NOW, 0, CLEAR_FLAG, True, 7, services)
    assert (context.stars, context.flags, context.mission_id, context.services) == (3, CLEAR_FLAG, 7, services)
    assert (context.boost, context.score, context.previous_record) == (UNBOOSTED, None, None)
    previous = {"MaxRank": 0, "AchieveFlag": 0}
    scored = replace(context, boost=BOOST, score=SCORE, previous_record=previous)
    assert (scored.boost, scored.score, scored.previous_record) == (BOOST, SCORE, previous)


def test_score_contexts_default_their_services_and_score_results_are_frozen():
    record = {"MaxRank": PACKED_SCORE, "AchieveFlag": CLEAR_FLAG}
    context = ScoreContext("catalog", "quest", "mode", "request", record, BOOST, NOW, 7)
    assert (context.record, context.boost, context.mission_id, context.services) == (record, BOOST, 7, ModeServices())
    result = ScoreResult(PACKED_SCORE, SCORE, {"kills_scored": SCORE})
    assert (result.max_rank, result.score, result.details) == (PACKED_SCORE, SCORE, {"kills_scored": SCORE})
    with pytest.raises(FrozenInstanceError):
        result.score = 0


SLOTTED = tuple(SimpleNamespace(slot=slot, special_attr=attr)
                for slot, attr in ((1, 0), (2, 0), (3, 2), (4, 0), (5, 0), (6, 0), (9, 2)))


@pytest.mark.parametrize("record,expected", [
    ({"AchieveFlag": 0x10000 | 0x100 | 1}, ((0, 1, 1), (1, 2, 0), (2, 3, 3), (3, 4, 0), (4, 5, 1), (5, 6, 1), (6, 9, 3))),
    ({"AchieveFlag": 0x1000 | 0x10}, ((0, 1, 0), (1, 2, 1), (2, 3, 2), (3, 4, 1), (4, 5, 0), (5, 6, 1), (6, 9, 3))),
    ({"AchieveFlag": CLEAR_FLAG}, ((0, 1, 0), (1, 2, 0), (2, 3, 2), (3, 4, 0), (4, 5, 0), (5, 6, 1), (6, 9, 3))),
    ({"AchieveFlag": 0}, ((0, 1, 0), (1, 2, 0), (2, 3, 2), (3, 4, 0), (4, 5, 0), (5, 6, 0), (6, 9, 2))),
    (None, ((0, 1, 0), (1, 2, 0), (2, 3, 2), (3, 4, 0), (4, 5, 0), (5, 6, 0), (6, 9, 2))),
])
def test_candidate_attributes_read_slot_bits_then_any_saved_flag(record, expected):
    assert candidate_attributes(record, SLOTTED) == expected


def test_slots_outside_the_treasure_columns_count_any_saved_flag():
    sign_only = {"AchieveFlag": INT32_MIN}
    assert [slot_obtained(sign_only, slot) for slot in (0, 1, 5, 6, 255)] == [True, False, False, True, True]
    assert [slot_obtained({"AchieveFlag": 0x10000}, slot) for slot in (0, 1, 5)] == [True, True, False]
    assert not slot_obtained(None, 6)


def test_candidate_attributes_merge_the_obtained_bit_into_special_attributes():
    special = (SimpleNamespace(slot=4, special_attr=3), SimpleNamespace(slot=6, special_attr=1))
    assert candidate_attributes({"AchieveFlag": 0x10}, special) == ((0, 4, 3), (1, 6, 1))


class SettlingPolicy(ModePolicy):
    name = "settling"

    def settle(self, context):
        return SettleOutcome(context.state)


class DeclaredSettlingPolicy(SettlingPolicy):
    name = "declared"
    entrust_parity = frozenset({"settle"})


class PreparingPolicy(ModePolicy):
    name = "preparing"

    def prepare_start(self, context, state):
        return state, ()


class RequestGatePolicy(ModePolicy):
    name = "request_gate"

    def check_start(self, context):
        if not context.request.candidates:
            raise ProtocolError("quest_candidates_mismatch", "Battle starts carry their drop candidates", 409)
        return super().check_start(context)


class BoostingPolicy(ModePolicy):
    name = "boosting"

    def start_boost(self, context):
        return context.request.boost


class ScoringPolicy(BoostingPolicy):
    name = "scoring"

    def score_result(self, context):
        return ScoreResult(0, context.request.score, {})


def test_entrust_hooks_keep_the_draw_and_state_of_quests_without_a_drop_bonus():
    state = {"quest_state": {"active": None}}
    context = EntrustContext(None, SimpleNamespace(quest_id=1001), SimpleNamespace(drop_bonus=None), state, 100001, 3,
                             NOW, 1)
    draw = EntrustDraw({4: 3}, ({"CardID": 1},), {"item_consignment_ticket": 1})
    story = quest_modes.policy(0)
    rewards = story.entrust_rewards(context, draw)
    assert rewards == EntrustRewards(draw) and rewards.draw is draw and rewards.children == ()
    assert story.entrust_settle(context) == SettleOutcome(state) and story.entrust_settle(context).state is state
    assert context.services == ModeServices()


BONUS_QUEST, CRYSTAL_CODE, BONUS_VALUE = 1001, 5002, 1.5
BONUS = DropBonus(BONUS_QUEST, CRYSTAL_TYPE, CRYSTAL, SUPPORTED_OPERATION, BANNER)
BANNER_CHILDREN = (bonus_container(BONUS_VALUE, CRYSTAL_TYPE),)


def bonus_context(moment):
    return EntrustContext(None, SimpleNamespace(quest_id=BONUS_QUEST), SimpleNamespace(drop_bonus=BONUS), {}, 100001,
                          3, moment, 1)


def test_entrust_rewards_scale_the_batch_total_of_the_bonus_item_but_never_the_treasure_counts():
    draw = EntrustDraw({CRYSTAL_CODE: 3, 4: 3}, ({"CardID": 4},), {CRYSTAL: 9, RUBY: 2})
    rewards = ModePolicy().entrust_rewards(bonus_context(MONDAY), draw)
    # Three runs of three crystals floor once to 13, not three times to 4.
    assert rewards.draw == EntrustDraw(draw.counts, draw.units, {CRYSTAL: 13, RUBY: 2})
    assert rewards.children == BANNER_CHILDREN


def test_a_bonus_day_without_the_bonus_item_keeps_the_draw_and_still_reports_bonus():
    draw = EntrustDraw({4: 3}, ({"CardID": 4},), {RUBY: 2})
    rewards = ModePolicy().entrust_rewards(bonus_context(MONDAY), draw)
    assert rewards.draw is draw and rewards.children == BANNER_CHILDREN


def test_a_zero_bonus_day_returns_the_same_draw_without_children():
    draw = EntrustDraw({CRYSTAL_CODE: 3}, (), {CRYSTAL: 9})
    rewards = ModePolicy().entrust_rewards(bonus_context(SUNDAY_NIGHT), draw)
    assert rewards == EntrustRewards(draw) and rewards.draw is draw


def test_every_registered_entrust_reward_hook_still_applies_the_drop_bonus():
    draw = EntrustDraw({CRYSTAL_CODE: 3}, (), {CRYSTAL: 9})
    for entry in {id(entry): entry for entry in quest_modes.registry().values()}.values():
        rewards = entry.entrust_rewards(bonus_context(MONDAY), draw)
        assert (dict(rewards.draw.items), rewards.children) == ({CRYSTAL: 13}, BANNER_CHILDREN), (
            f"Mode {entry.name} must call super().entrust_rewards so DropBonus reaches entrust")


def test_entrust_blockers_name_battle_hooks_overridden_without_declared_parity():
    assert ModePolicy().entrust_blocker() is None
    assert SettlingPolicy().entrust_blocker() == "Mode settling overrides settle without a declared entrust counterpart"
    assert DeclaredSettlingPolicy().entrust_blocker() is None
    assert PreparingPolicy().entrust_blocker() == (
        "Mode preparing overrides prepare_start without a declared entrust counterpart")
    assert RequestGatePolicy().entrust_blocker() == (
        "Mode request_gate overrides check_start without a declared entrust counterpart")
    for entry in (ModePolicy(), SettlingPolicy(), DeclaredSettlingPolicy(), PreparingPolicy(), RequestGatePolicy()):
        validate_entrust_parity(entry)


def test_the_boost_and_score_hooks_join_the_parity_list_in_attempt_order():
    assert ENTRUST_PARITY_HOOKS == ("check_start", "start_boost", "prepare_start", "score_result", "settle")
    assert BoostingPolicy().entrust_blocker() == (
        "Mode boosting overrides start_boost without a declared entrust counterpart")
    assert ScoringPolicy().entrust_blocker() == (
        "Mode scoring overrides start_boost, score_result without a declared entrust counterpart")
    assert ScoringPolicy().entrust_blocker(ENTRUST_PARITY_HOOKS[3:]) == (
        "Mode scoring overrides score_result without a declared entrust counterpart")
    assert ScoringPolicy().entrust_blocker(("check_start", "prepare_start", "settle")) is None
    parity = frozenset({"start_boost", "score_result"})
    declared = type("DeclaredScoringPolicy", (ScoringPolicy,), {"entrust_parity": parity})()
    assert declared.entrust_blocker() is None and validate_entrust_parity(declared) is None


def test_entrust_blockers_ignore_battle_hooks_patched_on_a_policy_instance(monkeypatch):
    story = quest_modes.policy(0)
    monkeypatch.setattr(story, "settle", lambda context: SettleOutcome(context.state))
    assert story.entrust_blocker() is None


@pytest.mark.parametrize("base,parity", [
    (ModePolicy, frozenset({"finish"})), (ModePolicy, frozenset({"settle"})),
    (SettlingPolicy, frozenset({"settle", "check_start"})), (SettlingPolicy, {"settle"}), (SettlingPolicy, ("settle",)),
])
def test_entrust_parity_must_be_a_frozenset_of_overridden_battle_hooks(base, parity):
    entry = type("DeclaredPolicy", (base,), {"entrust_parity": parity})()
    assert_code("quest_mode_registry_invalid", lambda: validate_entrust_parity(entry), 503)


def test_registry_rejects_modes_with_inconsistent_entrust_parity(monkeypatch):
    monkeypatch.setattr(type(quest_modes.policy(0)), "entrust_parity", frozenset({"settle"}))
    assert_code("quest_mode_registry_invalid", quest_modes.registry.__wrapped__, 503)


@pytest.mark.parametrize("families", [
    {"score"}, ("score",), frozenset({"score", "ranking"}), frozenset({1}),
], ids=["set", "tuple", "unknown_family", "integer"])
def test_registry_rejects_modes_whose_event_reward_families_are_not_a_frozenset_of_family_names(monkeypatch, families):
    entry = type("DeclaredPolicy", (ModePolicy,), {"event_reward_families": families})()
    assert_code("quest_mode_registry_invalid", lambda: validate_event_reward_families(entry), 503)
    monkeypatch.setattr(type(quest_modes.policy(0)), "event_reward_families", families)
    assert_code("quest_mode_registry_invalid", quest_modes.registry.__wrapped__, 503)


def test_modes_settle_no_event_reward_families_unless_they_declare_them():
    assert type(ModePolicy.event_reward_families) is frozenset and ModePolicy.event_reward_families == frozenset()
    assert validate_event_reward_families(ModePolicy()) is None
    assert not any(quest_modes.policy(quest_type).event_reward_families for quest_type in (5, *UNAVAILABLE))


def test_registered_modes_report_the_battle_hooks_entrust_would_skip():
    blocked = {quest_type: entry.entrust_blocker() for quest_type, entry in quest_modes.registry().items()
               if entry.entrust_blocker() is not None}
    assert not set(blocked) & ENTRUST_TYPES
    assert (blocked[6], blocked[9], blocked[10], blocked[19]) == (
        "Mode subjugation overrides check_start, start_boost, score_result, settle without a declared entrust "
        "counterpart",
        "Mode devil overrides check_start without a declared entrust counterpart",
        "Mode harlem overrides check_start, settle without a declared entrust counterpart",
        "Mode goddess overrides check_start, settle without a declared entrust counterpart",
    )
    assert all(quest_modes.policy(quest_type).entrust_blocker() is None for quest_type in (0, 2, 8, 15, 16, 18))


def test_enabled_entrust_modes_mirror_every_battle_hook_they_override():
    blocked = {quest_type: quest_modes.policy(quest_type).entrust_blocker() for quest_type in sorted(ENTRUST_TYPES)
               if quest_modes.policy(quest_type).enabled}
    assert blocked and all(reason is None for reason in blocked.values()), (
        f"Declare entrust_parity or add entrust_rewards/entrust_settle hooks for these entrust modes: {blocked}")
