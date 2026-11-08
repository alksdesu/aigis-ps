from collections import Counter
from datetime import datetime, timedelta
import json
import math
from types import MappingProxyType

import pytest

from aigis_ps.config_registry import REGISTRY, ConfigError
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.event_mission_rewards import SAVE_TABLE as EVENT_SAVE_TABLE, SCORE_CONDITIONS, EventMissionCatalog, IncentiveRow
from aigis_ps.local_io import MAX_JSON_DEPTH
from aigis_ps.mission_window_revival import COOPERATIVE_REGISTRATIONS, MissionWindowRules, build_mission_window_revival
from aigis_ps.native_numbers import INT32_MAX, float32
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.quest_catalog import QuestCatalog
from aigis_ps.quest_entries import EnemyBand, classify_enemy
from aigis_ps.quest_modes import CLEAR_FLAG, MAX_BOOST
from aigis_ps.quest_protocol import MAX_KILLS
from aigis_ps.subjugation_rules import (
    BEST_LIMIT, COOPERATIVE_LIMIT, COOPERATIVE_TYPE, CONFIG_TABLE, EMC_TABLE, MAX_RULE_BYTES, MAX_TOTAL,
    PROGRESS_NAME, QUEST_LIST_TABLE, RECORDS_ABOVE, RECORDS_BELOW, REWARD_TABLE, SOURCE_TABLES, SUBJUGATION_TYPE,
    TIER_SOURCES, ZERO_SCORE, BoostPolicy, KillsPolicy, RewardRange, RewardTier, ScorePair, SubjugationCatalog,
    SubjugationQuest, SubjugationRules, crossed_rewards, crossings, higher, load_subjugation_rules, next_crossing,
    pack, present, redeliver, reset_progress, resync_watermark, settlement_basis, unpack, watermark,
    watermark_mismatch, with_watermark,
)
from aigis_ps.table_cell_patch import patch_table_cells
from test_quest_service import bundle
from test_storage import NONFINITE


NOW = datetime(2026, 11, 2, 12, tzinfo=JST)
LABEL = "スコアクエスト報酬"
MISSIONS = MappingProxyType({
    400183: (6401, 6402, 6403, 6404, 6405),
    400212: (6611, 6612, 6613, 6614, 6615),
    400587: (30018, 30019, 30020, 30021, 30022, 30023),
})
SUBJUGATION_ENEMIES = MappingProxyType({
    6401: 100, 6402: 100, 6403: 300, 6404: 400, 6405: 500,
    6611: 100, 6612: 100, 6613: 300, 6614: 400, 6615: 500,
    30018: 100, 30019: 100, 30020: 300, 30021: 500, 30022: 750, 30023: 751,
})
LIMITS = MappingProxyType({400183: 10000, 400212: 10000, 400587: 7500})
# Each mission reaches its RewardConfig group through an EmcMissionConfig range whose ID_Mission is not the MissionID.
REWARD_RANGES = MappingProxyType({400183: (183, 123, 16), 400212: (212, 141, 16), 400587: (587, 462, 33)})
CONDITION_COUNTS = MappingProxyType({123: {"4": 10, "6": 4, "7": 2}, 141: {"4": 10, "6": 4, "7": 2},
                                     462: {"4": 20, "6": 11, "7": 2}})
# Repeating tiers as (Param_Condition, Param_ValidCondition, Param_Valid1, Param_Valid2).
REPEATING_TIERS = MappingProxyType({123: ((100, 3, 0, 2000), (200, 1, 2000, 0)),
                                    141: ((100, 3, 0, 2000), (200, 1, 2000, 0)),
                                    462: ((75, 3, 0, 1500), (150, 1, 1500, 0))})
FIRST_QUESTS = frozenset((6401, 6611, 30018))
LAST_MISSION, LAST_QUEST = 400587, 30023
CLEAR_FACTOR, COMPLETE_FACTOR = float32(1.3), 1.5
LAST_QUEST_SCORES = ((0, 0), (1, 2), (2, 3), (3, 4), (7, 10), (10, 13), (100, 130), (375, 488), (500, 650),
                     (749, 974), (750, 975), (751, 1127), (800, 1127))
RAW_OVERKILL, RAW_OVERKILL_SCORE = 800, 1200
TICKET_TIER, TICKET_ITEM = 1127, "item_smn_ticket_85144"
ALL_PLATFORMS = 8191
LAST_WINDOW = {"enable": ALL_PLATFORMS, "start_at": "2026-10-29T15:00:00+09:00", "end_at": "2026-11-12T10:59:59+09:00"}
AFTER_LAST_WINDOW = datetime(2026, 11, 12, 11, tzinfo=JST)
CONSTANT_WINDOW = {"start_at": None, "end_at": None}
# Cooperative missions the resources keep at Enable 0, so the client never lists them.
DORMANT_COOPERATIVE = MappingProxyType({400330: 1, 400346: 1, 400378: 2})
DORMANT_WINDOW = {"start_at": "2022-04-01T00:00:00+09:00", "end_at": "2022-04-07T10:59:59+09:00"}
UTF8_BOM = b"\xef\xbb\xbf"
TIER_ROW = MappingProxyType({"ID_Incentive": 1, "Type_Condition": 6, "Param_Condition": 100, "Param_ValidCondition": 0,
                             "Param_Valid1": 0, "Param_Valid2": 0, "Type_MainIncentive": 5, "Type_SubIncentive": 1,
                             "Param_Incentive": 119, "ID_Item": ""})
FIFTH_TIER, REPEATING_TIER = ("ID_Incentive", 5), ("Type_Condition", 7)


def offline(**rules) -> SubjugationCatalog:
    return SubjugationCatalog(object(), SubjugationRules(**rules))


def synthetic_quest(*, expected=751, clear=CLEAR_FACTOR, complete=COMPLETE_FACTOR, limit=7500) -> SubjugationQuest:
    return SubjugationQuest(quest_id=1, mission_id=1, subjugation_type=0, limit=limit, factor_clear=clear,
                            factor_complete=complete, expected=expected, start_button="WD01", per_run_cap=0,
                            first_quest=1, entry="Map0001_0001.aar/Entry00.atb")


def tier(condition, threshold, *, valid=0, low=0, high=0, sub=14, amount=1, item="", incentive=1) -> RewardTier:
    # One legacy present per crossing is the form every current repeating tier takes, so any condition accepts it.
    return RewardTier.from_row(1, {**TIER_ROW, "ID_Incentive": incentive, "Type_Condition": condition,
                                   "Param_Condition": threshold, "Param_ValidCondition": valid, "Param_Valid1": low,
                                   "Param_Valid2": high, "Type_SubIncentive": sub, "Param_Incentive": amount,
                                   "ID_Item": item})


def scores_on(subject: RewardTier, value: int) -> ScorePair:
    return ScorePair(value, 0) if subject.source == "best" else ScorePair(0, value)


def record(best, total):
    return {"MaxRank": pack(best, total), "AchieveFlag": CLEAR_FLAG}


def refused(call, code, status):
    with pytest.raises(ProtocolError) as caught:
        call()
    assert (caught.value.code, caught.value.status) == (code, status)
    return caught.value


class Overlay:
    def __init__(self, base, tables=None, members=None):
        self._base, self._tables, self._members = base, dict(tables or {}), dict(members or {})

    def resource_rows(self, name):
        return self._tables[name] if name in self._tables else self._base.resource_rows(name)

    def archive_rows(self, archive, member):
        key = (archive, member)
        return self._members[key] if key in self._members else self._base.archive_rows(archive, member)

    def __getattr__(self, name):
        return getattr(self._base, name)


def test_rules_default_to_clamped_kills_and_total_only_boosts():
    rules = SubjugationRules()
    assert (rules.kills_policy, rules.boost_policy) == (KillsPolicy.CLAMP, BoostPolicy.TOTAL_ONLY)
    assert rules.to_dict() == {"kills_policy": "clamp", "boost_policy": "total_only"}
    assert load_subjugation_rules(None) == rules


def test_rules_take_policy_names_and_version_every_combination():
    assert SubjugationRules("raw", "ignore") == SubjugationRules(KillsPolicy.RAW, BoostPolicy.IGNORE)
    assert type(SubjugationRules("raw").to_dict()["kills_policy"]) is str
    versions = {SubjugationRules(kills, boost).content_version for kills in KillsPolicy for boost in BoostPolicy}
    assert len(versions) == len(KillsPolicy) * len(BoostPolicy)
    assert SubjugationRules().content_version == SubjugationRules("clamp", "total_only").content_version


@pytest.mark.parametrize("field", ["kills_policy", "boost_policy"])
@pytest.mark.parametrize("value", ["", "CLAMP", "Total_Only", "double", None, 1, True, ["clamp"]])
def test_rules_refuse_unknown_policies(field, value):
    with pytest.raises(ValueError):
        SubjugationRules(**{field: value})


def test_rule_files_fill_missing_policies_with_their_defaults(tmp_path):
    path = tmp_path / "subjugation-rules.json"
    path.write_bytes(b'{"kills_policy": "raw"}')
    assert load_subjugation_rules(path) == SubjugationRules("raw", "total_only")
    path.write_bytes(UTF8_BOM + b'{"kills_policy": "clamp", "boost_policy": "total_and_best"}')
    assert load_subjugation_rules(path) == SubjugationRules("clamp", "total_and_best")
    path.write_bytes(b"{}")
    assert load_subjugation_rules(path) == SubjugationRules()


@pytest.mark.parametrize("raw", [
    b'{"kills_policy": "clamp", "boost": "total_only"}', b'{"kills_policy": "clamp", "kills_policy": "raw"}',
    b'{"boost_policy": "double"}', b'{"kills_policy": NaN}', b'["clamp", "total_only"]', b'"clamp"', b"{", b"",
    b'{"kills_policy": "\xff"}',
])
def test_rule_files_refuse_unknown_keys_duplicates_and_malformed_json(tmp_path, raw):
    path = tmp_path / "subjugation-rules.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        load_subjugation_rules(path)


@pytest.mark.parametrize("number", NONFINITE)
def test_rule_files_refuse_nonfinite_numbers_while_parsing(tmp_path, number):
    path = tmp_path / "subjugation-rules.json"
    path.write_bytes(b'{"kills_policy": ' + number.encode("ascii") + b"}")
    with pytest.raises(ValueError, match="finite"):
        load_subjugation_rules(path)


def test_rule_file_nesting_is_reported_by_the_registry_and_stays_a_value_error(tmp_path):
    path = tmp_path / "subjugation-rules.json"
    path.write_bytes(b'{"kills_policy": ' + b"[" * MAX_JSON_DEPTH + b"]" * MAX_JSON_DEPTH + b"}")
    with pytest.raises(ValueError, match="nesting"):
        load_subjugation_rules(path)
    with pytest.raises(ConfigError) as error:
        REGISTRY["subjugation-rules"].load(path)
    assert (error.value.code, error.value.key) == ("config_nesting_too_deep", "subjugation-rules")


def test_rule_files_are_size_bounded_and_must_exist(tmp_path):
    path = tmp_path / "subjugation-rules.json"
    path.write_bytes(b'{"kills_policy": "raw"' + b" " * MAX_RULE_BYTES + b"}")
    with pytest.raises(ValueError):
        load_subjugation_rules(path)
    with pytest.raises(ValueError):
        load_subjugation_rules(tmp_path / "absent.json")


def test_packed_scores_keep_the_best_run_below_bit_twelve():
    assert pack(1127, 4381) == (4381 << 12) | 1127
    assert unpack(pack(1127, 4381)) == ScorePair(1127, 4381)
    assert unpack(0) == ScorePair(0, 0) and pack(0, 0) == 0
    assert BEST_LIMIT == 0xFFF and MAX_TOTAL == (2 ** 31 - 1) >> 12
    assert pack(BEST_LIMIT, COOPERATIVE_LIMIT) == (99999 << 12) | 0xFFF < 2 ** 31
    assert unpack(pack(BEST_LIMIT, MAX_TOTAL)) == (BEST_LIMIT, MAX_TOTAL) and pack(BEST_LIMIT, MAX_TOTAL) == 2 ** 31 - 1


@pytest.mark.parametrize("best,total", [(BEST_LIMIT + 1, 0), (-1, 0), (0, MAX_TOTAL + 1), (0, -1), (True, 0),
                                        (0, 1.0), (None, 0)])
def test_packed_scores_refuse_values_outside_their_fields(best, total):
    with pytest.raises(ValueError):
        pack(best, total)


@pytest.mark.parametrize("max_rank", [-1, 2 ** 31, True, 1.0, "4096", None])
def test_unpacking_requires_a_nonnegative_native_max_rank(max_rank):
    with pytest.raises(ValueError):
        unpack(max_rank)


def test_scores_multiply_in_single_precision():
    factor = float32(1.1)
    quest = synthetic_quest(expected=10, clear=factor, complete=factor)
    # A double product would round 10 x 1.1f up to 12; the float32 product is exactly 11.
    assert math.ceil(10 * factor) == 12
    assert offline().score(quest, 10).score == 11


def test_factors_follow_the_reported_kills_under_either_kills_policy():
    quest = synthetic_quest()
    for subject in (offline(), offline(kills_policy="raw")):
        assert subject.score(quest, 750).factor == CLEAR_FACTOR
        assert subject.score(quest, 751).factor == subject.score(quest, 800).factor == COMPLETE_FACTOR
    clamped, raw = offline().score(quest, 800), offline(kills_policy="raw").score(quest, 800)
    assert (clamped.score, clamped.kills_scored, clamped.kills_over_expected) == (1127, 751, 49)
    assert (raw.score, raw.kills_scored, raw.kills_over_expected) == (1200, 800, 49)
    assert offline().score(quest, 750) == offline(kills_policy="raw").score(quest, 750)


def test_per_run_caps_cover_a_clear_factor_above_the_complete_factor():
    quest = synthetic_quest(expected=100, clear=2.0, complete=1.0)
    clamped, raw = offline(), offline(kills_policy="raw")
    assert clamped.score(quest, 99).score == 198 > clamped.score(quest, 100).score == 100
    assert clamped.per_run_cap(quest) == 198 == max(clamped.score(quest, kills).score for kills in range(300))
    assert raw.per_run_cap(quest) == MAX_KILLS


def test_per_run_caps_handle_empty_and_unreachable_plans():
    empty = synthetic_quest(expected=0, clear=3.0, complete=1.0)
    assert offline().per_run_cap(empty) == 0 and offline(kills_policy="raw").per_run_cap(empty) == MAX_KILLS
    unreachable = synthetic_quest(expected=MAX_KILLS + 5, clear=1.0, complete=3.0)
    assert offline().per_run_cap(unreachable) == offline(kills_policy="raw").per_run_cap(unreachable) == MAX_KILLS


@pytest.mark.parametrize("kills", [-1, MAX_KILLS + 1, True, 1.0, "10", None])
def test_scores_refuse_kills_outside_the_reported_range(kills):
    with pytest.raises(ValueError):
        offline().score(synthetic_quest(), kills)


@pytest.mark.parametrize("factor", [0.5, 0.0, float("nan"), float("inf"), 1.1, 1, 30000.0])
def test_quests_refuse_factors_that_cannot_score_natively(factor):
    with pytest.raises(ValueError):
        synthetic_quest(clear=factor)


@pytest.mark.parametrize("changes", [{"expected": -1}, {"expected": 2 ** 31}, {"limit": MAX_TOTAL + 1}, {"limit": -1}])
def test_quests_refuse_counts_and_limits_outside_the_packed_fields(changes):
    with pytest.raises(ValueError):
        synthetic_quest(**changes)


def test_scores_and_caps_require_subjugation_quests():
    for call in (lambda: offline().score(MappingProxyType({}), 1), lambda: offline().per_run_cap(None)):
        with pytest.raises(ValueError):
            call()


@pytest.mark.parametrize("policy,expected", [("total_only", (1127, 4381)), ("total_and_best", (3381, 4381)),
                                             ("ignore", (1127, 2127))])
def test_boost_policies_split_total_and_best_gains(policy, expected):
    subject = offline(boost_policy=policy)
    assert subject.accumulate(100, 1000, 1127, MAX_BOOST, 7500) == expected
    assert subject.accumulate(100, 1000, 1127, 1, 7500) == (1127, 2127)


def test_accumulated_scores_stay_within_their_caps():
    both = offline(boost_policy="total_and_best")
    assert both.accumulate(0, 1000, 2000, 3, 7500) == (BEST_LIMIT, 7000)
    assert both.accumulate(0, 1000, 2000, 3, 5000) == (BEST_LIMIT, 5000)
    assert offline().accumulate(4000, 0, 10, 1, 7500) == (4000, 10)
    # A limit below a stored total caps the total like 8CEBC0 caps the mission sum.
    assert offline().accumulate(0, 9000, 0, 1, 7500) == (0, 7500)
    assert isinstance(offline().accumulate(0, 0, 0, 1, 0), ScorePair)


@pytest.mark.parametrize("arguments", [
    (0, 0, 1, 0, 7500), (0, 0, 1, MAX_BOOST + 1, 7500), (0, 0, 1, True, 7500), (0, 0, 1, 2.0, 7500),
    (0, 0, -1, 1, 7500), (0, 0, 1.5, 1, 7500), (0, 0, True, 1, 7500), (0, 0, 1, 1, -1), (0, 0, 1, 1, MAX_TOTAL + 1),
    (BEST_LIMIT + 1, 0, 1, 1, 7500), (-1, 0, 1, 1, 7500), (0, MAX_TOTAL + 1, 1, 1, 7500),
])
def test_accumulate_refuses_values_outside_the_native_ranges(arguments):
    with pytest.raises(ValueError):
        offline().accumulate(*arguments)


def test_catalogs_read_nothing_until_first_use():
    class Untouched:
        def __getattr__(self, name):
            raise AssertionError(f"Construction read {name}")

    subject = SubjugationCatalog(Untouched(), SubjugationRules(boost_policy="total_and_best"))
    assert subject.accumulate(0, 0, 100, 2, 7500) == (200, 200)
    with pytest.raises(ValueError):
        SubjugationCatalog(Untouched(), {"kills_policy": "raw"})


def test_catalogs_share_the_event_mission_catalog_of_their_quests(quests):
    shared = EventMissionCatalog(quests)
    assert SubjugationCatalog(quests, events=shared).events is shared
    default = SubjugationCatalog(quests)
    assert isinstance(default.events, EventMissionCatalog) and default.events.quests is quests
    for events in (object(), EventMissionCatalog(object())):
        with pytest.raises(ValueError):
            SubjugationCatalog(quests, events=events)


def test_once_tiers_pay_exactly_when_their_threshold_is_reached():
    best, total = tier(6, 300), tier(4, 375)
    steps = ((0, 299), (0, 300), (299, 300), (300, 300), (300, BEST_LIMIT), (299, BEST_LIMIT))
    assert [crossings(best, (old, 0), (new, 0)) for old, new in steps] == [0, 1, 1, 0, 0, 1]
    assert crossings(best, (0, 0), (0, 7500)) == 0
    assert crossings(total, (0, 374), (0, 375)) == 1 and crossings(total, (0, 375), (0, 375)) == 0
    assert crossings(total, (0, 0), (BEST_LIMIT, 0)) == 0


def test_repeating_tiers_pay_per_crossed_multiple():
    every = tier(7, 100)
    steps = ((0, 99), (0, 100), (50, 250), (100, 199), (199, 200), (0, 7500))
    assert [crossings(every, (0, old), (0, new)) for old, new in steps] == [0, 1, 2, 0, 1, 75]


def test_repeating_tiers_below_a_ceiling_stop_at_it():
    capped = tier(7, 75, valid=3, high=1500)
    steps = ((0, 1500), (0, 1600), (1499, 1500), (1490, 3000), (1500, 3000), (1600, 3000))
    assert [crossings(capped, (0, old), (0, new)) for old, new in steps] == [20, 20, 1, 1, 0, 0]
    window = tier(7, 100, valid=3, low=500, high=1000)
    assert [crossings(window, (0, old), (0, new)) for old, new in ((400, 900), (500, 900), (999, 2000))] == [0, 4, 1]


def test_repeating_tiers_above_a_floor_count_from_it():
    floor = tier(7, 150, valid=1, low=1500)
    steps = ((0, 1500), (0, 1649), (0, 1650), (1499, 1800), (1650, 1799), (3000, 7500))
    assert [crossings(floor, (0, old), (0, new)) for old, new in steps] == [0, 0, 1, 2, 0, 30]
    shifted = tier(7, 40, valid=1, low=-25)
    assert crossings(shifted, (0, 0), (0, 15)) == 1 and crossings(shifted, (0, 14), (0, 15)) == 1


def test_falling_scores_never_pay():
    for subject in (tier(4, 375), tier(6, 300), tier(7, 100), tier(7, 75, valid=3, high=1500),
                    tier(7, 150, valid=1, low=1500)):
        assert crossings(subject, scores_on(subject, 4000), scores_on(subject, 0)) == 0


@pytest.mark.parametrize("subject", [
    tier(6, 300), tier(4, 375), tier(4, 0), tier(7, 100), tier(7, 75, valid=3, high=1500),
    tier(7, 100, valid=3, low=500, high=1000), tier(7, 150, valid=1, low=1500), tier(7, 40, valid=1, low=-25),
])
def test_next_crossings_are_the_first_scores_that_pay(subject):
    cap = 1200 if subject.source == "best" else 1800
    for value in range(0, cap + 1, 17):
        first = next((score for score in range(value + 1, cap + 1)
                      if crossings(subject, scores_on(subject, value), scores_on(subject, score))), None)
        assert next_crossing(subject, value, cap) == first


def test_next_crossings_stop_at_the_reachable_score():
    assert next_crossing(tier(6, BEST_LIMIT + 1), 0, BEST_LIMIT) is None
    assert next_crossing(tier(4, 7501), 0, 7500) is None and next_crossing(tier(4, 7500), 0, 7500) == 7500
    assert next_crossing(tier(7, 100), 7450, 7500) == 7500 and next_crossing(tier(7, 100), 7500, 7500) is None
    with pytest.raises(ValueError):
        next_crossing(tier(7, 100), -1, 7500)


@pytest.mark.parametrize("column,value", [("Type_Condition", 5), ("Type_Condition", 8), ("Param_ValidCondition", 2),
                                          ("Type_MainIncentive", 4), ("Type_SubIncentive", 3)])
def test_reward_values_outside_the_native_contract_are_refused(column, value):
    error = refused(lambda: RewardTier.from_row(462, {**TIER_ROW, column: value}),
                    "quest_subjugation_reward_invalid", 503)
    assert column in str(error)


@pytest.mark.parametrize("changes", [{"Param_Condition": 0, "Type_Condition": 7}, {"Param_Condition": -75, "Type_Condition": 7},
                                     {"Param_Incentive": 1.0}, {"Param_Valid1": True}, {"ID_Item": None},
                                     {"ID_Incentive": 2 ** 31}])
def test_reward_rows_with_unusable_cells_are_refused(changes):
    refused(lambda: RewardTier.from_row(462, {**TIER_ROW, **changes}), "quest_subjugation_reward_invalid", 503)


def test_reward_rows_missing_a_column_are_refused():
    row = {key: value for key, value in TIER_ROW.items() if key != "Param_Valid2"}
    refused(lambda: RewardTier.from_row(462, row), "quest_subjugation_reward_invalid", 503)


def incentive_row(**changes):
    values = {"incentive": 1, "condition": 6, "threshold": 100, "main_incentive": 5, "sub_incentive": 1,
              "amount": 119, "item_id": "", "valid_condition": 0, "valid_low": 0, "valid_high": 0}
    return IncentiveRow(**{**values, **changes})


def test_incentive_rows_convert_with_the_checks_of_the_native_columns():
    assert RewardTier.from_incentive(462, incentive_row()) == RewardTier.from_row(462, TIER_ROW)
    assert frozenset(TIER_SOURCES) == SCORE_CONDITIONS


@pytest.mark.parametrize("changes", [
    {"condition": 5}, {"condition": 8}, {"valid_condition": 2}, {"main_incentive": 4}, {"sub_incentive": 3},
    {"condition": 7, "threshold": 0}, {"condition": 7, "sub_incentive": 1}, {"amount": 1.0}, {"valid_low": True},
    {"item_id": None}, {"incentive": 2 ** 31},
])
def test_incentive_rows_outside_the_native_contract_are_refused(changes):
    refused(lambda: RewardTier.from_incentive(462, incentive_row(**changes)), "quest_subjugation_reward_invalid", 503)


def test_rows_that_are_not_incentive_rows_are_refused():
    refused(lambda: RewardTier.from_incentive(462, dict(TIER_ROW)), "quest_subjugation_reward_invalid", 503)


def test_tiers_name_the_score_they_follow():
    assert [tier(condition, 100).source for condition in (4, 6, 7)] == ["total", "best", "total"]
    assert tier(6, 100).to_dict()["source"] == "best"


@pytest.mark.parametrize("sub,amount,item,expected", [
    (1, 119, "", {"kind": "unit", "template_id": 119, "count": 1, "description": LABEL}),
    (2, 30000, "", {"kind": "gold", "amount": 30000, "description": LABEL}),
    (7, 50, "", {"kind": "legacy_item", "amount": 50, "present_type": 7, "description": LABEL}),
    (14, 1, "", {"kind": "legacy_item", "amount": 1, "present_type": 14, "description": LABEL}),
    (14, 0, "", {"kind": "legacy_item", "amount": 0, "present_type": 14, "description": LABEL}),
    (22, 0, TICKET_ITEM, {"kind": "item", "amount": 1, "item_id": TICKET_ITEM, "description": LABEL}),
    (22, 3, TICKET_ITEM, {"kind": "item", "amount": 3, "item_id": TICKET_ITEM, "description": LABEL}),
])
def test_presents_follow_the_incentive_forms(sub, amount, item, expected):
    assert present(tier(6, 100, sub=sub, amount=amount, item=item), 1, LABEL) == expected


@pytest.mark.parametrize("sub,amount,item", [(2, 1, ""), (7, 1, ""), (14, 1, ""), (22, 0, TICKET_ITEM),
                                             (22, 1, TICKET_ITEM), (22, 3, TICKET_ITEM)])
def test_repeated_crossings_merge_into_the_amount_the_result_page_shows(sub, amount, item):
    # 633810 shows max(1, Param_Incentive) per crossing for a named item and the bare crossing count otherwise.
    times = 4
    shown = max(1, amount) * times if sub == 22 else times
    assert present(tier(7, 75, sub=sub, amount=amount, item=item), times, LABEL)["amount"] == shown


@pytest.mark.parametrize("sub,amount", [(1, 1), (1, 360), (2, 0), (2, 30000), (7, 50), (14, 0), (14, 2)])
def test_repeating_tiers_the_result_page_counts_differently_are_refused(sub, amount):
    error = refused(lambda: tier(7, 75, sub=sub, amount=amount), "quest_subjugation_reward_invalid", 503)
    assert "Type_SubIncentive" in str(error)
    # Once tiers show Param_Incentive itself, so every amount of these presents stays accepted there.
    assert tier(6, 75, sub=sub, amount=amount).amount == tier(4, 75, sub=sub, amount=amount).amount == amount


@pytest.mark.parametrize("times,description", [(0, LABEL), (-1, LABEL), (True, LABEL), (1.0, LABEL), (1, ""),
                                               (1, None)])
def test_presents_require_a_crossing_and_the_reward_label(times, description):
    with pytest.raises(ValueError):
        present(tier(6, 100), times, description)


def test_watermarks_live_under_the_policy_name():
    state = {"quest_state": {"schema_version": 1, "mode_progress": {"goddess": {"granted": 3}}}}
    assert watermark(state, LAST_MISSION) is None
    updated = with_watermark(state, LAST_MISSION, ScorePair(1127, 4381))
    assert updated["quest_state"]["mode_progress"] == {
        "goddess": {"granted": 3}, PROGRESS_NAME: {str(LAST_MISSION): {"best": 1127, "total": 4381}}}
    assert state["quest_state"]["mode_progress"] == {"goddess": {"granted": 3}}
    again = with_watermark(updated, 400183, (5, 6))
    assert again["quest_state"]["mode_progress"][PROGRESS_NAME] == {
        str(LAST_MISSION): {"best": 1127, "total": 4381}, "400183": {"best": 5, "total": 6}}
    assert watermark(again, 400183) == ScorePair(5, 6) and watermark(again, LAST_MISSION) == (1127, 4381)
    assert watermark({}, LAST_MISSION) is None and watermark({"quest_state": None}, LAST_MISSION) is None


@pytest.mark.parametrize("journal", [
    {"mode_progress": []}, {"mode_progress": {PROGRESS_NAME: []}}, "journal",
    {"mode_progress": {PROGRESS_NAME: {"400587": {"best": 1}}}},
    {"mode_progress": {PROGRESS_NAME: {"400587": {"best": BEST_LIMIT + 1, "total": 0}}}},
    {"mode_progress": {PROGRESS_NAME: {"400587": {"best": 1, "total": 2, "granted": 3}}}},
    {"mode_progress": {PROGRESS_NAME: {"400587": [1, 2]}}},
    {"mode_progress": {PROGRESS_NAME: {"400587": {"best": 1, "total": -2}}}},
])
def test_watermarks_refuse_malformed_progress(journal):
    refused(lambda: watermark({"quest_state": journal}, LAST_MISSION), "quest_state_invalid", 503)


def test_watermarks_are_written_into_a_battle_journal():
    refused(lambda: watermark([], LAST_MISSION), "quest_state_invalid", 503)
    refused(lambda: with_watermark({"tables": {}}, LAST_MISSION, (1, 2)), "quest_state_invalid", 503)
    refused(lambda: with_watermark(MappingProxyType({"quest_state": {}}), LAST_MISSION, (1, 2)),
            "quest_state_invalid", 503)
    for mission_id, scores in ((0, (1, 2)), ("400587", (1, 2)), (LAST_MISSION, (BEST_LIMIT + 1, 0))):
        with pytest.raises(ValueError):
            with_watermark({"quest_state": {}}, mission_id, scores)


def test_settlements_cross_from_the_higher_of_the_watermark_and_the_records():
    assert higher((100, 3000), (1127, 1427)) == settlement_basis((100, 3000), (1127, 1427)) == ScorePair(1127, 3000)
    assert settlement_basis(None, (1127, 1427)) == ScorePair(1127, 1427)
    assert watermark_mismatch(None, (1127, 1427)) == watermark_mismatch((1127, 1427), (1127, 1427)) == {}
    assert watermark_mismatch((100, 3000), (1127, 1427)) == {"best": RECORDS_ABOVE, "total": RECORDS_BELOW}
    assert watermark_mismatch((1127, 1427), (1127, 1428)) == {"total": RECORDS_ABOVE}
    for call in (lambda: higher((BEST_LIMIT + 1, 0), (0, 0)), lambda: settlement_basis((0, 0), (0, -1)),
                 lambda: settlement_basis(None, None), lambda: watermark_mismatch((0, 0), [0, 0]),
                 lambda: watermark_mismatch(None, (0, MAX_TOTAL + 1))):
        with pytest.raises(ValueError):
            call()


@pytest.fixture(scope="module")
def quests(bundle):
    return bundle.quests.catalog


@pytest.fixture(scope="module")
def catalog(quests):
    return SubjugationCatalog(quests)


@pytest.fixture(scope="module")
def raw_catalog(quests):
    return SubjugationCatalog(quests, SubjugationRules(kills_policy="raw"))


def test_current_missions_list_their_quests_limits_and_first_quests(catalog):
    assert catalog.mission_ids == tuple(MISSIONS)
    for mission_id, members in MISSIONS.items():
        mission = catalog.mission(mission_id)
        assert mission.members == mission.quests == members and mission.first_quest == min(members)
        assert mission.supported and mission.window_source == "resource"
        assert (mission.total_score_limit, mission.limit) == (LIMITS[mission_id], LIMITS[mission_id])
    without_button = {quest_id for quest_id in SUBJUGATION_ENEMIES if not catalog.quest(quest_id).start_button}
    assert without_button == FIRST_QUESTS == {catalog.mission(mission_id).first_quest for mission_id in MISSIONS}


def test_current_quests_plan_the_enemy_counts_of_their_maps(catalog):
    for mission_id, members in MISSIONS.items():
        for quest_id in members:
            quest = catalog.quest(quest_id)
            assert (quest.quest_id, quest.mission_id, quest.expected) == (quest_id, mission_id, SUBJUGATION_ENEMIES[quest_id])
            assert quest.limit == LIMITS[mission_id] and quest.first_quest == min(members)
            assert quest.entry.startswith(f"Map{mission_id}_") and catalog.quest(quest_id) is quest


def test_current_bonus_factors_rise_to_one_except_for_the_last_quest(quests, catalog):
    cells = {row["QuestID"]: (row["ClearBonusFactor"], row["CompleteBonusFactor"])
             for row in quests.resource_rows(QUEST_LIST_TABLE) if row["QuestID"] in SUBJUGATION_ENEMIES}
    assert cells.pop(LAST_QUEST) == (CLEAR_FACTOR, COMPLETE_FACTOR) and set(cells.values()) == {(0.0, 0.0)}
    factors = {quest_id: (catalog.quest(quest_id).factor_clear, catalog.quest(quest_id).factor_complete)
               for quest_id in SUBJUGATION_ENEMIES}
    assert factors.pop(LAST_QUEST) == (CLEAR_FACTOR, COMPLETE_FACTOR) and set(factors.values()) == {(1.0, 1.0)}


def test_the_last_quest_scores_kills_like_the_result_page(catalog, raw_catalog):
    quest = catalog.quest(LAST_QUEST)
    assert tuple((kills, catalog.score(quest, kills).score) for kills, _ in LAST_QUEST_SCORES) == LAST_QUEST_SCORES
    raw = raw_catalog.score(raw_catalog.quest(LAST_QUEST), RAW_OVERKILL)
    assert (raw.score, raw.kills_scored, raw.factor) == (RAW_OVERKILL_SCORE, RAW_OVERKILL, COMPLETE_FACTOR)
    assert catalog.score(quest, RAW_OVERKILL).kills_over_expected == RAW_OVERKILL - SUBJUGATION_ENEMIES[LAST_QUEST]


def test_current_per_run_caps_are_the_highest_score_a_run_can_report(catalog, raw_catalog):
    assert {quest_id: catalog.quest(quest_id).per_run_cap for quest_id in (30018, LAST_QUEST)} == {30018: 100,
                                                                                                    LAST_QUEST: 1127}
    assert {quest_id: raw_catalog.quest(quest_id).per_run_cap for quest_id in (30018, LAST_QUEST)} == {
        30018: MAX_KILLS, LAST_QUEST: math.ceil(MAX_KILLS * COMPLETE_FACTOR)}
    for subject in (catalog, raw_catalog):
        quest = subject.quest(LAST_QUEST)
        highest = max(subject.score(quest, kills).score for kills in range(MAX_KILLS + 1))
        assert subject.per_run_cap(quest) == quest.per_run_cap == highest


def test_current_limits_fit_the_packed_total(catalog):
    for mission_id, limit in LIMITS.items():
        top = catalog.accumulate(BEST_LIMIT, limit, BEST_LIMIT + 1, MAX_BOOST, catalog.mission(mission_id).limit)
        assert top == (BEST_LIMIT, limit) and unpack(pack(*top)) == top


def test_current_reward_groups_follow_the_first_emc_range(catalog):
    for mission_id, (emc_id, group, count) in REWARD_RANGES.items():
        members = MISSIONS[mission_id]
        for quest_id in members:
            assert catalog.reward_range(quest_id) == RewardRange(emc_id, members[0], members[-1], group)
        ladder = catalog.ladder(mission_id)
        assert emc_id != mission_id and catalog.reward_group(mission_id) == group and len(ladder) == count
        assert Counter(str(entry.condition) for entry in ladder) == CONDITION_COUNTS[group]
        assert {entry.group for entry in ladder} == {group} and {entry.main_incentive for entry in ladder} == {5}
        repeating = tuple((entry.threshold, entry.valid_condition, entry.valid_low, entry.valid_high)
                          for entry in ladder if entry.condition == 7)
        assert repeating == REPEATING_TIERS[group]


def test_current_repeating_tiers_cross_at_their_valid_bounds(catalog):
    for mission_id in MISSIONS:
        capped, floor = (entry for entry in catalog.ladder(mission_id) if entry.condition == 7)
        step, high = capped.threshold, capped.valid_high
        assert crossings(capped, (0, 0), (0, high)) == crossings(capped, (0, 0), (0, high + step)) == high // step
        assert crossings(capped, (0, high - 1), (0, high)) == 1 and crossings(capped, (0, high), (0, high + step)) == 0
        step, low = floor.threshold, floor.valid_low
        assert crossings(floor, (0, 0), (0, low)) == crossings(floor, (0, 0), (0, low + step - 1)) == 0
        assert crossings(floor, (0, 0), (0, low + step)) == 1 and crossings(floor, (0, low - 1), (0, low + 2 * step)) == 2
        assert crossings(floor, (0, low + step), (0, low + 2 * step - 1)) == 0


def test_current_once_tiers_cross_at_their_threshold(catalog):
    ladder = catalog.ladder(LAST_MISSION)
    best = next(entry for entry in ladder if entry.condition == 6)
    total = next(entry for entry in ladder if entry.condition == 4)
    assert (best.threshold, total.threshold) == (100, 375)
    assert crossings(best, (99, 0), (100, 0)) == 1 and crossings(best, (100, 0), (100, 0)) == 0
    assert crossings(best, (100, 0), (BEST_LIMIT, 0)) == 0 and crossings(best, (0, 0), (0, 7500)) == 0
    assert crossings(total, (0, 374), (0, 375)) == 1 and crossings(total, (0, 375), (0, 375)) == 0


def test_type_6_tiers_cross_like_the_result_page_reading_the_played_quest(catalog):
    # 460B20 compares the mission best before a run with the best of the played quest alone after it.
    best_tiers = tuple(entry for entry in catalog.ladder(LAST_MISSION) if entry.condition == 6)
    records, rising = {}, Counter()
    for kills in (0, 99, 100, 300, 120, 751, 500):
        for quest_id in MISSIONS[LAST_MISSION]:
            quest = catalog.quest(quest_id)
            held = unpack(records[quest_id]["MaxRank"]) if quest_id in records else ZERO_SCORE
            before = catalog.mission_score(records, LAST_MISSION)
            played = catalog.accumulate(*held, catalog.score(quest, kills).score, 1, quest.limit)
            records[quest_id] = record(*played)
            after = catalog.mission_score(records, LAST_MISSION)
            rising[played.best > before.best] += 1
            for entry in best_tiers:
                assert crossings(entry, before, after) == crossings(entry, before, (played.best, after.total))
    assert rising[True] and rising[False]


def test_current_reward_presents_are_mailable_unless_their_ticket_pool_is_disabled(bundle, catalog):
    description = catalog.description()
    assert description == LABEL
    for mission_id in MISSIONS:
        for entry in catalog.ladder(mission_id):
            reward = present(entry, 1, description)
            if entry.sub_incentive == 22:
                assert (mission_id, entry.threshold) == (LAST_MISSION, TICKET_TIER)
                assert reward == {"kind": "item", "amount": 1, "item_id": TICKET_ITEM, "description": LABEL}
                continue
            assert bundle.presents.preview([reward], 0)


def test_status_reports_rules_sources_windows_and_rewards(quests, catalog):
    status = catalog.status()
    json.dumps(status, ensure_ascii=False, allow_nan=False)
    assert status["ready"] is True and status["rules"] == SubjugationRules().to_dict()
    assert status["rules_version"] == SubjugationRules().content_version
    assert status["source_sha256"] == {name: quests.resource_sha256(name) for name in SOURCE_TABLES}
    assert status["resource_version"] == quests.resource_version and status["description"] == {"text": LABEL}
    missions = {entry["mission_id"]: entry for entry in status["missions"]}
    assert tuple(missions) == tuple(MISSIONS)
    last = missions[LAST_MISSION]
    assert last["window"] == {**LAST_WINDOW, "source": "resource"} and last["members"] == list(MISSIONS[LAST_MISSION])
    assert [entry["per_run_cap"] for entry in last["quests"]] == [100, 100, 300, 500, 750, 1127]
    assert last["rewards"] == {"group": 462, "ranges": [{"emc_id": 587, "first_quest": 30018, "last_quest": 30023,
                                                          "group": 462}], "tiers": 33,
                               "conditions": CONDITION_COUNTS[462],
                               "sub_incentives": {"1": 23, "2": 6, "7": 1, "14": 2, "22": 1}}
    assert all(entry["ready"] and entry["supported"] for entry in missions.values())
    assert status["cooperative"] == catalog.status(NOW)["cooperative"] == []


def test_content_versions_follow_rules(quests, catalog, raw_catalog):
    assert SubjugationCatalog(quests).content_version == catalog.content_version
    versions = {SubjugationCatalog(quests, SubjugationRules(kills, boost)).content_version
                for kills in KillsPolicy for boost in BoostPolicy}
    assert len(versions) == len(KillsPolicy) * len(BoostPolicy) and raw_catalog.content_version in versions


def test_content_versions_include_the_shared_event_catalog(quests, catalog):
    class Changed(Overlay):
        def resource_sha256(self, name):
            digest = self._base.resource_sha256(name)
            # SpecialMissionRewardConfig feeds only the shared catalog, so its version must still move this one.
            return digest[::-1] if name == EVENT_SAVE_TABLE else digest

    subject = SubjugationCatalog(Changed(quests))
    assert subject.events.content_version != catalog.events.content_version
    assert subject.content_version != catalog.content_version
    assert subject.status()["source_sha256"] == catalog.status()["source_sha256"]


@pytest.mark.parametrize("kind,limit", [(COOPERATIVE_TYPE, COOPERATIVE_LIMIT), (2, 7500)])
def test_cooperative_missions_are_unsupported_and_keep_their_native_limit(quests, kind, limit):
    rows = tuple({**row, "SubjugationType": kind} if row["MissionID"] == LAST_MISSION else row
                 for row in quests.resource_rows(CONFIG_TABLE))
    subject = SubjugationCatalog(Overlay(quests, {CONFIG_TABLE: rows}))
    mission = subject.mission(LAST_MISSION)
    assert not mission.supported and (mission.limit, mission.total_score_limit) == (limit, 7500)
    refused(lambda: subject.quest(30019), "quest_mode_unsupported", 501)
    status = subject.status()
    entry = next(item for item in status["missions"] if item["mission_id"] == LAST_MISSION)
    assert entry["supported"] is False and entry["ready"] is True and "quests" not in entry
    assert status["ready"] is True and subject.describe({"tables": {"quest_records": []}}, NOW)["missions"][2][
        "supported"] is False
    listed = {"mission_id": LAST_MISSION, "subjugation_type": kind, "registration": COOPERATIVE_REGISTRATIONS[kind],
              "window": {**LAST_WINDOW, "source": "resource"}}
    assert status["cooperative"] == [{**listed, "state": None}]
    assert subject.status(NOW)["cooperative"] == [{**listed, "state": "open"}]
    assert subject.status(AFTER_LAST_WINDOW)["cooperative"] == []


def enabled_catalog(bundle, quests, missions, overrides):
    index = {row["MissionID"]: position for position, row in enumerate(quests.resource_rows(CONFIG_TABLE))}
    payload = patch_table_cells(quests._asset_reader(CONFIG_TABLE),
                                {index[mission_id]: {"Enable": ALL_PLATFORMS} for mission_id in missions})

    def read(name):
        return payload if name == CONFIG_TABLE else quests._asset_reader(name)

    return QuestCatalog(quests.snapshot, read, possessions=bundle.possessions,
                        resource_version=f"{quests.resource_version}-cooperative", window_overrides=overrides)


@pytest.fixture(scope="module")
def listed_cooperative(bundle, quests):
    upcoming = (NOW + timedelta(days=1), NOW + timedelta(days=8))
    return enabled_catalog(bundle, quests, DORMANT_COOPERATIVE, {400330: upcoming, 400378: (None, None)})


def config_failure(mission_id):
    return {"code": "quest_mode_resource_invalid",
            "message": f"{CONFIG_TABLE} mission {mission_id} differs from the native contract"}


def test_resource_cooperative_missions_are_all_unlisted(quests):
    rows = {row["MissionID"]: row for row in quests.resource_rows(CONFIG_TABLE)}
    kinds = {mission_id: row["SubjugationType"] for mission_id, row in rows.items()
             if row["SubjugationType"] in COOPERATIVE_REGISTRATIONS}
    assert kinds.items() >= DORMANT_COOPERATIVE.items() and not any(rows[mission_id]["Enable"] for mission_id in kinds)


def test_status_lists_listed_cooperative_missions_until_their_windows_end(listed_cooperative, catalog):
    subject = SubjugationCatalog(listed_cooperative)
    timeless = {entry["mission_id"]: entry for entry in subject.status()["cooperative"]}
    assert {mission_id: (entry["subjugation_type"], entry["registration"], entry["state"])
            for mission_id, entry in timeless.items()} == {400330: (1, 143, None), 400346: (1, 143, None),
                                                           400378: (2, 147, None)}
    assert timeless[400330]["window"] == {"enable": ALL_PLATFORMS, "start_at": "2026-11-03T12:00:00+09:00",
                                          "end_at": "2026-11-10T12:00:00+09:00", "source": "override"}
    assert timeless[400346]["window"] == {"enable": ALL_PLATFORMS, **DORMANT_WINDOW, "source": "resource"}
    assert timeless[400378]["window"] == {"enable": ALL_PLATFORMS, **CONSTANT_WINDOW, "source": "override"}
    status = subject.status(NOW)
    assert {entry["mission_id"]: entry["state"] for entry in status["cooperative"]} == {400330: "upcoming",
                                                                                          400378: "open"}
    # Their members are absent from QuestTable, so the scored scope and its readiness stay as they were.
    assert subject.mission_ids == catalog.mission_ids and status["ready"] is True
    assert status["missions"] == catalog.status()["missions"]


def test_an_unreadable_listed_row_outside_the_scored_scope_fails_readiness(listed_cooperative, catalog):
    rows = tuple({**row, "SubjugationType": 1.5} if row["MissionID"] == 400330 else row
                 for row in listed_cooperative.resource_rows(CONFIG_TABLE))
    status = SubjugationCatalog(Overlay(listed_cooperative, {CONFIG_TABLE: rows})).status(NOW)
    # Every scored mission stays ready, so only the row that may send 143 or 147 fails the catalog.
    assert status["missions"] == catalog.status()["missions"]
    assert [entry for entry in status["cooperative"] if "error" in entry] == [
        {"mission_id": 400330, "error": config_failure(400330)}]
    assert status["ready"] is False


@pytest.mark.parametrize("change,listed", [
    ({"SubjugationType": 1.5}, True),
    ({"SubjugationType": COOPERATIVE_TYPE, "EndDateTime": [0]}, True),
    ({"EndDateTime": [0]}, False),
])
def test_listed_rows_of_unreadable_type_or_cooperative_dates_join_the_cooperative_status(quests, change, listed):
    rows = tuple({**row, **change} if row["MissionID"] == LAST_MISSION else row
                 for row in quests.resource_rows(CONFIG_TABLE))
    status = SubjugationCatalog(Overlay(quests, {CONFIG_TABLE: rows})).status(NOW)
    failure = config_failure(LAST_MISSION)
    # A row whose type cannot be read may still send 143 or 147, while a standard row needs no readable dates here.
    assert status["cooperative"] == ([{"mission_id": LAST_MISSION, "error": failure}] if listed else [])
    entry = next(item for item in status["missions"] if item["mission_id"] == LAST_MISSION)
    assert entry["error"] == failure and status["ready"] is False


def entry_overlay(quests, catalog, quest_id, change):
    archive, member = catalog.quest(quest_id).entry.split("/")
    rows = list(quests.archive_rows(archive, member))
    # Copying an admitted ordinary enemy leaves the changed cells alone to decide what the row filter makes of it.
    template = next(row for row in rows if classify_enemy(row["EnemyID"]) is EnemyBand.ORDINARY and row["Loop"] > 0)
    rows.append(MappingProxyType({**template, **change}))
    return Overlay(quests, members={(archive, member): tuple(rows)})


@pytest.mark.parametrize("change,code", [
    ({"IsAppear": "GetNum_QuestStar() >= 3;"}, "quest_definition_invalid"),
    ({"EnemyID": 6001, "IsAppear": ""}, "quest_definition_invalid"),
    ({"EnemyID": 8001, "IsAppear": ""}, "quest_definition_invalid"),
    ({"IsAppear": "GetNum_QuestStar() != 3;"}, "quest_definition_invalid"),
    ({"Loop": -1, "IsAppear": ""}, "quest_entry_invalid"),
    ({"EnemyID": 1.5}, "quest_entry_invalid"),
])
def test_conditional_or_malformed_entries_are_refused_for_score_quests(quests, catalog, change, code):
    subject = SubjugationCatalog(entry_overlay(quests, catalog, 30019, change))
    refused(lambda: subject.quest(30019), code, 503)
    entry = next(item for item in subject.status()["missions"] if item["mission_id"] == LAST_MISSION)
    assert entry["ready"] is False and entry["quests"][1]["error"]["code"] == code


@pytest.mark.parametrize("change", [{"EnemyID": 1500, "IsAppear": "GetNum_QuestStar() != 3;"},
                                    {"IsAppear": "GetNum_QuestStar() < 0;"}])
def test_entries_the_row_filter_never_admits_leave_the_plan_unchanged(quests, catalog, change):
    subject = SubjugationCatalog(entry_overlay(quests, catalog, 30019, change))
    assert subject.quest(30019).expected == SUBJUGATION_ENEMIES[30019]


def emc_overlay(quests, *extra):
    # Added ranges go to the end of the table, so only their ID_Mission can put them first.
    rows = quests.resource_rows(EMC_TABLE)
    template = next(row for row in rows if row["ID_Mission"] == REWARD_RANGES[LAST_MISSION][0])
    added = tuple(MappingProxyType({**template, "ID_Mission": emc_id, "ID_QuestFirst": first, "ID_QuestLast": last,
                                    "ID_ResultReward": group}) for emc_id, first, last, group in extra)
    return Overlay(quests, {EMC_TABLE: (*rows, *added)})


def smallest_emc_id(quests):
    return min(row["ID_Mission"] for row in quests.resource_rows(EMC_TABLE))


def test_overlapping_emc_ranges_resolve_to_the_smallest_id_mission(quests):
    first = smallest_emc_id(quests) - 1
    subject = SubjugationCatalog(emc_overlay(quests, (first, 30018, 30023, 123), (INT32_MAX, 30018, 30023, 141)))
    assert subject.reward_range(30019) == RewardRange(first, 30018, 30023, 123)
    assert subject.reward_group(LAST_MISSION) == 123 and len(subject.ladder(LAST_MISSION)) == 16
    # Group 123 now belongs to the added entry, so the mission that reached it through entry 183 loses its ladder.
    assert subject.reward_range(6401) == RewardRange(REWARD_RANGES[400183][0], 6401, 6405, 0)
    assert subject.reward_group(400183) is None and subject.ladder(400183) == ()
    later = SubjugationCatalog(emc_overlay(quests, (INT32_MAX, 30018, 30023, 141)))
    assert later.reward_range(30019).emc_id == REWARD_RANGES[LAST_MISSION][0] and later.reward_group(LAST_MISSION) == 462


def test_members_naming_different_reward_groups_are_refused(quests):
    first = smallest_emc_id(quests) - 1
    split = SubjugationCatalog(emc_overlay(quests, (first, 30018, 30020, 123)))
    refused(lambda: split.ladder(LAST_MISSION), "quest_subjugation_reward_invalid", 503)
    unrewarded = SubjugationCatalog(emc_overlay(quests, (first, 30018, 30023, 0)))
    assert unrewarded.reward_group(LAST_MISSION) is None and unrewarded.ladder(LAST_MISSION) == ()


def test_repeated_emc_mission_identifiers_merge_like_the_native_loader(quests, catalog):
    emc_id = REWARD_RANGES[LAST_MISSION][0]
    # The native table ends on this entry, so the repeat adjoins it: keys accumulate while the columns stay (612E60).
    adjoined = SubjugationCatalog(emc_overlay(quests, (emc_id, 1, 2, 123)))
    assert adjoined.mission_ids == tuple(MISSIONS)
    # The entry now holds keys 123 and 462, but its first one stays claimed by the earlier entry 183.
    assert adjoined.reward_range(30019) == RewardRange(emc_id, 30018, 30023, 0)
    assert adjoined.reward_group(LAST_MISSION) is None and adjoined.ladder(LAST_MISSION) == ()
    status = adjoined.status()
    entry = next(item for item in status["missions"] if item["mission_id"] == LAST_MISSION)
    assert status["ready"] is True and entry["rewards"] == {
        "group": None, "ranges": [{"emc_id": emc_id, "first_quest": 30018, "last_quest": 30023, "group": 0}],
        "tiers": 0, "conditions": {}, "sub_incentives": {}}
    # An ID_Mission reappearing further down rewrites its entry, and the earlier entry claims the shared group.
    rewritten = SubjugationCatalog(emc_overlay(quests, (REWARD_RANGES[400183][0], 1, 2, 462)))
    assert rewritten.reward_range(6401) is None and rewritten.reward_group(400183) is None
    assert rewritten.reward_range(30019) == RewardRange(emc_id, 30018, 30023, 0)
    assert rewritten.reward_group(LAST_MISSION) is None and rewritten.ladder(LAST_MISSION) == ()
    assert rewritten.ladder(400212) == catalog.ladder(400212) and rewritten.status()["ready"] is True


@pytest.mark.parametrize("chosen,column,value", [
    (FIFTH_TIER, "Type_Condition", 5), (FIFTH_TIER, "Type_Condition", 8), (FIFTH_TIER, "Param_ValidCondition", 2),
    (FIFTH_TIER, "Type_MainIncentive", 4), (FIFTH_TIER, "Type_SubIncentive", 3),
    (REPEATING_TIER, "Type_SubIncentive", 1), (REPEATING_TIER, "Param_Incentive", 2),
])
def test_invalid_current_tiers_fail_only_their_mission(quests, chosen, column, value):
    (key, selected), rows, current, edited = chosen, [], 0, False
    for row in quests.resource_rows(REWARD_TABLE):
        current = row["ID_Reward"] or current
        if current == REWARD_RANGES[LAST_MISSION][1] and row[key] == selected and not edited:
            row, edited = MappingProxyType({**row, column: value}), True
        rows.append(row)
    subject = SubjugationCatalog(Overlay(quests, {REWARD_TABLE: tuple(rows)}))
    assert edited and column in str(refused(lambda: subject.ladder(LAST_MISSION), "quest_subjugation_reward_invalid", 503))
    status = subject.status()
    ready = {entry["mission_id"]: entry["ready"] for entry in status["missions"]}
    assert ready == {400183: True, 400212: True, LAST_MISSION: False} and status["ready"] is False
    assert subject.quest(LAST_QUEST).per_run_cap == 1127


def group_rows(quests, group):
    rows, current, members = quests.resource_rows(REWARD_TABLE), 0, []
    for index, row in enumerate(rows):
        current = row["ID_Reward"] or current
        if current == group:
            members.append(index)
    return rows, members


def test_a_restarted_group_merges_keeping_its_repeated_head_row(quests, catalog):
    group = REWARD_RANGES[LAST_MISSION][1]
    rows, members = group_rows(quests, group)
    head = rows[members[0]]
    assert head["ID_Reward"] == group
    # 611C00 finds or inserts the group, so a repeated head files its row under the one already loaded.
    subject = SubjugationCatalog(Overlay(quests, {REWARD_TABLE: (*rows, head)}))
    ladder, base = subject.ladder(LAST_MISSION), catalog.ladder(LAST_MISSION)
    assert len(ladder) == len(base) + 1
    assert Counter(ladder) - Counter(base) == Counter((RewardTier.from_row(group, head),))


def test_a_repeated_incentive_merges_into_the_one_already_loaded(quests, catalog):
    group = REWARD_RANGES[LAST_MISSION][1]
    rows, members = group_rows(quests, group)
    second = rows[members[1]]
    assert not second["ID_Reward"] and second["ID_Incentive"]
    # 613050 finds or inserts the incentive, so the repeated row joins its rows instead of restarting them.
    doubled = (*rows[:members[1] + 1], second, *rows[members[1] + 1:])
    subject = SubjugationCatalog(Overlay(quests, {REWARD_TABLE: doubled}))
    ladder, base = subject.ladder(LAST_MISSION), catalog.ladder(LAST_MISSION)
    assert len(ladder) == len(base) + 1
    [(duplicate, extra)] = (Counter(ladder) - Counter(base)).items()
    assert extra == 1 and duplicate in base and duplicate.incentive == second["ID_Incentive"]


def test_continuations_before_the_first_group_are_skipped(quests, catalog):
    rows = quests.resource_rows(REWARD_TABLE)
    assert rows[0]["ID_Reward"] and not rows[1]["ID_Reward"]
    # 006101F2 skips rows that precede the first head, so the catalog loads as if the leading row were absent.
    subject = SubjugationCatalog(Overlay(quests, {REWARD_TABLE: (rows[1], *rows)}))
    for mission_id in MISSIONS:
        assert subject.ladder(mission_id) == catalog.ladder(mission_id)


def test_a_zero_type_condition_keeps_the_last_nonzero_value(quests, catalog):
    group = REWARD_RANGES[LAST_MISSION][1]
    rows, members = group_rows(quests, group)
    previous, chosen = next((previous, index) for previous, index in zip(members, members[1:])
                            if rows[previous]["Type_Condition"] in (4, 6)
                            and rows[index]["Type_Condition"] not in (0, rows[previous]["Type_Condition"]))
    inherited = rows[previous]["Type_Condition"]
    edited = list(rows)
    edited[chosen] = MappingProxyType({**rows[chosen], "Type_Condition": 0})
    subject = SubjugationCatalog(Overlay(quests, {REWARD_TABLE: tuple(edited)}))
    ladder = {tier.incentive: tier for tier in subject.ladder(LAST_MISSION)}
    base = {tier.incentive: tier for tier in catalog.ladder(LAST_MISSION)}
    incentive = rows[chosen]["ID_Incentive"]
    # 006104F1 keeps the last nonzero Type_Condition, so the zeroed cell reads as the row above it.
    assert ladder[incentive].condition == inherited != base[incentive].condition
    assert {key: tier for key, tier in ladder.items() if key != incentive} == {
        key: tier for key, tier in base.items() if key != incentive}


def test_a_missing_reward_label_fails_the_status(quests, catalog):
    class Unlabeled(Overlay):
        def system_text(self, index):
            return None

    subject = SubjugationCatalog(Unlabeled(quests))
    refused(subject.description, "quest_subjugation_reward_invalid", 503)
    status = subject.status()
    assert status["ready"] is False and status["description"]["error"]["code"] == "quest_subjugation_reward_invalid"
    assert len(subject.ladder(LAST_MISSION)) == len(catalog.ladder(LAST_MISSION))


def test_quests_and_missions_outside_the_enabled_scope_are_refused(catalog):
    for quest_id in (4807, 1, "6401", True, 10 ** 12):
        refused(lambda: catalog.quest(quest_id), "quest_not_found", 404)
    for mission_id in (400115, 400330, "400587", 4807):
        refused(lambda: catalog.mission(mission_id), "quest_mission_unknown", 409)
        refused(lambda: catalog.ladder(mission_id), "quest_mission_unknown", 409)


def test_the_scope_needs_an_enabled_mission_with_subjugation_quests(quests):
    windows = quests.mission_windows(SUBJUGATION_TYPE)
    hidden = next(window.mission_id for window in windows.ordered()
                  if not window.listed and windows.members(window.mission_id))
    # Members of the hidden mission become subjugation quests, while 400212 loses its quests from QuestTable.
    retyped = {**dict.fromkeys(windows.members(hidden), SUBJUGATION_TYPE), **dict.fromkeys(MISSIONS[400212])}

    class Retyped(Overlay):
        def quest_type(self, quest_id):
            return retyped.get(quest_id, self._base.quest_type(quest_id))

    assert SubjugationCatalog(Retyped(quests)).mission_ids == (400183, LAST_MISSION)


def test_mission_scores_read_member_records_like_the_client(catalog):
    records = {30019: record(100, 300), LAST_QUEST: record(1127, 7300), 6402: record(BEST_LIMIT, 9000)}
    assert catalog.mission_best(records, LAST_MISSION) == 1127 and catalog.mission_total(records, LAST_MISSION) == 7500
    assert catalog.mission_score(records, 400183) == (BEST_LIMIT, 9000) and catalog.mission_score(records, 400212) == (0, 0)
    assert catalog.first_quest_record(records, LAST_MISSION) == {"quest_id": 30018, "recorded": False, "best": 0, "total": 0}
    records[30018] = record(37, 41)
    assert catalog.first_quest_record(records, LAST_MISSION) == {"quest_id": 30018, "recorded": True, "best": 37,
                                                                 "total": 41}
    assert catalog.mission_score(records, LAST_MISSION) == (1127, 7500)


@pytest.mark.parametrize("records", [{30019: {"MaxRank": -1}}, {30019: {}}, {30019: {"MaxRank": 1.0}}, {30019: 7},
                                     [{"QuestID": 30019, "MaxRank": 0}]])
def test_records_without_a_packed_max_rank_are_refused(catalog, records):
    refused(lambda: catalog.mission_score(records, LAST_MISSION), "quest_state_invalid", 503)


DESCRIBED_RECORDS = MappingProxyType({30019: (100, 300), LAST_QUEST: (1127, 1127)})


def describe_state(*, journal=None, records=DESCRIBED_RECORDS):
    rows = [{"QuestID": quest_id, **record(*scores)} for quest_id, scores in records.items()]
    state = {"tables": {"quest_records": rows}}
    if journal is not None:
        state["quest_state"] = journal
    return state


def saved_progress(best, total):
    return {"mode_progress": {PROGRESS_NAME: {str(LAST_MISSION): {"best": best, "total": total}}}}


def described_mission(view):
    return next(entry for entry in view["missions"] if entry["mission_id"] == LAST_MISSION)


def test_describe_reports_records_watermarks_tiers_and_first_quests(catalog):
    saved = describe_state(journal=saved_progress(100, 300), records={30019: (100, 260), LAST_QUEST: (40, 40)})
    view = catalog.describe(saved, NOW)
    json.dumps(view, ensure_ascii=False, allow_nan=False)
    assert view["rules"] == SubjugationRules().to_dict() and view["content_version"] == catalog.content_version
    missions = {entry["mission_id"]: entry for entry in view["missions"]}
    assert {mission_id: entry["state"] for mission_id, entry in missions.items()} == {
        400183: "ended", 400212: "ended", LAST_MISSION: "open"}
    last = missions[LAST_MISSION]
    assert (last["best"], last["total"], last["limit"]) == (100, 300, 7500)
    assert (last["watermark"], last["basis"], last["mismatch"]) == (
        {"best": 100, "total": 300}, {"best": 100, "total": 300}, {})
    assert [(entry["quest_id"], entry["recorded"], entry["best"], entry["total"]) for entry in last["records"]] == [
        (30018, False, 0, 0), (30019, True, 100, 260), (30020, False, 0, 0), (30021, False, 0, 0),
        (30022, False, 0, 0), (LAST_QUEST, True, 40, 40)]
    assert last["first_quest"] == {"quest_id": 30018, "recorded": False, "best": 0, "total": 0}
    tiers = {entry["incentive"]: entry for entry in last["tiers"]}
    assert len(tiers) == 33 and (tiers[1]["state"], tiers[1]["times"], tiers[1]["next_at"]) == ("reached", 1, None)
    assert (tiers[2]["state"], tiers[2]["next_at"], tiers[2]["remaining"]) == ("unreached", 200, 100)
    assert (tiers[12]["times"], tiers[12]["next_at"], tiers[13]["next_at"], tiers[14]["next_at"]) == (4, 375, 1650, 375)
    assert last["next"] == {"best": {"incentive": 2, "condition": 6, "next_at": 200, "remaining": 100},
                            "total": {"incentive": 14, "condition": 4, "next_at": 375, "remaining": 75}}


def test_describe_starts_from_the_records_without_a_watermark(catalog):
    last = described_mission(catalog.describe(describe_state(), NOW))
    assert (last["watermark"], last["basis"], last["mismatch"]) == (None, {"best": 1127, "total": 1427}, {})
    tiers = {entry["incentive"]: entry for entry in last["tiers"]}
    assert tiers[11]["state"] == "reached" and tiers[16]["state"] == "reached" and tiers[17]["state"] == "unreached"
    assert (tiers[12]["next_at"], tiers[17]["next_at"]) == (1500, 1500)
    # 6359A0 lets the later of two equally close tiers replace the earlier one, and every best tier is reached.
    assert last["next"] == {"best": None, "total": {"incentive": 17, "condition": 4, "next_at": 1500, "remaining": 73}}


@pytest.mark.parametrize("saved,basis,marked,total_next", [
    ((100, 300), (1127, 1427), {"best": RECORDS_ABOVE, "total": RECORDS_ABOVE}, (17, 4, 1500, 73)),
    ((BEST_LIMIT, 7500), (BEST_LIMIT, 7500), {"best": RECORDS_BELOW, "total": RECORDS_BELOW}, None),
    ((100, 3000), (1127, 3000), {"best": RECORDS_ABOVE, "total": RECORDS_BELOW}, (13, 7, 3150, 1723)),
])
def test_describe_marks_a_disagreeing_watermark_and_counts_tiers_from_the_higher_scores(catalog, saved, basis, marked,
                                                                                       total_next):
    last = described_mission(catalog.describe(describe_state(journal=saved_progress(*saved)), NOW))
    assert (last["best"], last["total"], last["watermark"]) == (1127, 1427, ScorePair(*saved)._asdict())
    assert (last["basis"], last["mismatch"]) == (ScorePair(*basis)._asdict(), marked)
    # Tiers below the basis count as reached because no settlement mails them again.
    assert {entry["incentive"]: entry["times"] for entry in last["tiers"]} == {
        entry.incentive: crossings(entry, ZERO_SCORE, basis) for entry in catalog.ladder(LAST_MISSION)}
    # The next tier still waits for the records, so its remaining points count from them rather than the watermark.
    nearest = None if total_next is None else dict(zip(("incentive", "condition", "next_at", "remaining"), total_next))
    assert last["next"] == {"best": None, "total": nearest}


def test_describe_requires_an_aware_clock_and_readable_records(catalog):
    refused(lambda: catalog.describe(describe_state(), datetime(2026, 11, 2, 12)), "quest_clock_invalid", 503)
    refused(lambda: catalog.status(datetime(2026, 11, 2, 12)), "quest_clock_invalid", 503)
    for state in ({"tables": {"quest_records": {}}}, {"tables": []}, [], {"tables": {"quest_records": [1]}},
                  {"tables": {"quest_records": [{"QuestID": 30019, "MaxRank": 0}] * 2}}):
        refused(lambda: catalog.describe(state, NOW), "quest_state_invalid", 503)


@pytest.fixture
def live_state(bundle):
    return bundle.initialize(bundle.player_state.initialize(bundle.bootstrap.initial_state()))


def put_records(state, records):
    rows = [row for row in state["tables"]["quest_records"] if row["QuestID"] not in records]
    rows += [{"QuestID": quest_id, "MaxRank": pack(*scores), "AchieveFlag": CLEAR_FLAG, "RemainAssault": 0,
              "FirstDefeatDate": 0, "Shown": False} for quest_id, scores in records.items()]
    return {**state, "tables": {**state["tables"], "quest_records": rows}, "quest_state": {}}


def test_resynced_watermarks_follow_the_records_in_either_direction(catalog, live_state):
    state = put_records(live_state, {30019: (100, 300), LAST_QUEST: (1127, 7300)})
    updated, recorded = resync_watermark(catalog, state, LAST_MISSION)
    assert recorded == ScorePair(1127, 7500) and watermark(updated, LAST_MISSION) == recorded
    assert watermark(state, LAST_MISSION) is None
    # Resync realigns the watermark in either direction, so a lowered one lets settled tiers mail again.
    lowered, recorded = resync_watermark(catalog, with_watermark(state, LAST_MISSION, (BEST_LIMIT, 7500)), LAST_MISSION)
    assert recorded == ScorePair(1127, 7500) and watermark(lowered, LAST_MISSION) == recorded


def test_redelivery_mails_the_tiers_between_the_watermark_and_the_records(bundle, catalog, live_state):
    state = with_watermark(put_records(live_state, {30019: (300, 600)}), LAST_MISSION, (100, 100))
    updated, mailed, reached = redeliver(catalog, state, LAST_MISSION, bundle.presents)
    assert reached == ScorePair(300, 600) and watermark(updated, LAST_MISSION) == reached
    rewards = [reward for _, reward in crossed_rewards(catalog, LAST_MISSION, ScorePair(100, 100), reached)]
    existing = len(live_state["tables"]["presents"])
    assert [{name: value for name, value in row.items() if name != "PresentID"} for row in mailed] == \
        bundle.presents.preview(rewards, existing)
    assert updated["tables"]["presents"][existing:] == mailed
    assert all(row["Description"] == LABEL for row in mailed)
    again, nothing, kept = redeliver(catalog, updated, LAST_MISSION, bundle.presents)
    assert (nothing, kept, again) == ([], reached, updated)


def test_redelivery_without_a_watermark_starts_from_the_records(bundle, catalog, live_state):
    state = put_records(live_state, {30019: (300, 600)})
    updated, mailed, reached = redeliver(catalog, state, LAST_MISSION, bundle.presents)
    assert mailed == [] and reached == ScorePair(300, 600)
    assert watermark(updated, LAST_MISSION) == reached
    assert updated["tables"]["presents"] == live_state["tables"]["presents"]


def test_redelivery_crosses_each_score_from_its_own_watermark_and_never_lowers_it(bundle, catalog, live_state):
    state = with_watermark(put_records(live_state, {30019: (100, 600)}), LAST_MISSION, (750, 100))
    updated, mailed, reached = redeliver(catalog, state, LAST_MISSION, bundle.presents)
    assert reached == ScorePair(750, 600) and watermark(updated, LAST_MISSION) == reached
    rewards = [reward for _, reward in crossed_rewards(catalog, LAST_MISSION, ScorePair(750, 100), ScorePair(750, 600))]
    assert [row["Description"] for row in mailed] == [LABEL] * len(rewards) and rewards
    ahead = with_watermark(put_records(live_state, {30019: (100, 100)}), LAST_MISSION, (750, 3000))
    kept, nothing, held = redeliver(catalog, ahead, LAST_MISSION, bundle.presents)
    assert nothing == [] and held == ScorePair(750, 3000) and watermark(kept, LAST_MISSION) == held


def test_redelivery_refuses_the_whole_delivery_when_the_mailbox_cannot_take_it(bundle, catalog, live_state):
    filler = [{"kind": "gold", "amount": 1, "description": LABEL}] * (MAX_PRESENTS - 1)
    full, _ = bundle.presents.enqueue(live_state, filler)
    state = with_watermark(put_records(full, {30019: (300, 600)}), LAST_MISSION, (100, 100))
    refused(lambda: redeliver(catalog, state, LAST_MISSION, bundle.presents), "present_capacity", 409)
    assert watermark(state, LAST_MISSION) == ScorePair(100, 100)


def test_reset_progress_clears_member_records_and_the_watermark(catalog, live_state):
    state = put_records(live_state, {30019: (100, 300), LAST_QUEST: (1127, 7300), 6402: (50, 60)})
    state = with_watermark(with_watermark(state, LAST_MISSION, (1127, 7500)), 400183, (50, 60))
    updated, cleared = reset_progress(catalog, state, LAST_MISSION)
    assert cleared == [30019, LAST_QUEST]
    records = {row["QuestID"]: row for row in updated["tables"]["quest_records"]}
    assert records[30019]["MaxRank"] == records[LAST_QUEST]["MaxRank"] == 0
    assert records[30019]["AchieveFlag"] == CLEAR_FLAG and records[6402]["MaxRank"] == pack(50, 60)
    assert watermark(updated, LAST_MISSION) is None and watermark(updated, 400183) == ScorePair(50, 60)
    assert watermark(state, LAST_MISSION) == ScorePair(1127, 7500)
    untouched, nothing = reset_progress(catalog, live_state, LAST_MISSION)
    assert nothing == [] and untouched == live_state
    refused(lambda: reset_progress(catalog, live_state, 4807), "quest_mission_unknown", 409)


def revived_catalog(bundle, quests, batches, version):
    revival = build_mission_window_revival(quests._asset_reader, MissionWindowRules(enabled=True, batches=batches))
    assert set(revival.replacements) == {CONFIG_TABLE}

    def read(name):
        return revival.replacements[name] if name in revival.replacements else quests._asset_reader(name)

    return QuestCatalog(quests.snapshot, read, possessions=bundle.possessions, resource_version=version)


def test_patched_windows_reach_the_status_through_the_effective_resource(bundle, quests, catalog):
    subject = SubjugationCatalog(revived_catalog(bundle, quests, {"Subjugation:400183": CONSTANT_WINDOW},
                                                 f"{quests.resource_version}-windows"))
    status = subject.status()
    windows = {entry["mission_id"]: entry["window"] for entry in status["missions"]}
    assert windows == {400183: {"enable": 2047, **CONSTANT_WINDOW, "source": "resource"},
                       400212: {"enable": 2047, **CONSTANT_WINDOW, "source": "resource"},
                       LAST_MISSION: {**LAST_WINDOW, "source": "resource"}}
    base = catalog.status()["source_sha256"]
    assert status["source_sha256"][CONFIG_TABLE] != base[CONFIG_TABLE]
    assert {name: digest for name, digest in status["source_sha256"].items() if name != CONFIG_TABLE} == {
        name: digest for name, digest in base.items() if name != CONFIG_TABLE}
    assert subject.content_version != catalog.content_version and status["ready"] is True
    states = {entry["mission_id"]: entry["state"] for entry in subject.describe(describe_state(), NOW)["missions"]}
    assert states == {400183: "open", 400212: "open", LAST_MISSION: "open"}


def test_window_overrides_are_reported_as_their_source(bundle, quests, catalog):
    overridden = QuestCatalog(quests.snapshot, quests._asset_reader, possessions=bundle.possessions,
                              resource_version=quests.resource_version, window_overrides={400212: (None, None)})
    subject = SubjugationCatalog(overridden)
    assert subject.mission(400212).window_source == "override" and subject.mission(400183).window_source == "resource"
    windows = {entry["mission_id"]: entry["window"] for entry in subject.status()["missions"]}
    assert windows[400212] == {"enable": 2047, **CONSTANT_WINDOW, "source": "override"}
    assert subject.content_version != catalog.content_version
