from collections import Counter
from dataclasses import asdict, fields
import hashlib
import json
from types import MappingProxyType

import pytest

from aigis_ps import quest_modes
from aigis_ps.errors import ProtocolError
from aigis_ps.event_mission_rewards import (
    COLLECTION_KEY, COLLECTION_KIND, EMC_TABLE, EMERGENCY_CATEGORY, LADDER_HIDDEN, NO_FAMILIES, NOT_PROMISED,
    RESOURCE_INVALID, REWARD_TABLE, RULES, SAVE_TABLE, SAVES_UNREAD, SCORE_CONDITIONS, SOURCE_TABLES, TRIAL_KEY,
    UNSUPPORTED, EventMissionCatalog, Family, IncentiveRow, settled_families,
)
from aigis_ps.quest_mode_subjugation import POLICY as SUBJUGATION_POLICY
from aigis_ps.quest_modes import (
    EVENT_REWARD_FAMILIES, SCORE_TYPES, MissionWindow, MissionWindows, ModePolicy, policy, registry,
)
from aigis_ps.subjugation_rules import SUBJUGATION_TYPE, TIER_SOURCES, RewardTier, SubjugationCatalog
from test_quest_mode_subjugation import OPEN_MISSION, OPENER, scene, start
from test_quest_modes import config
from test_quest_service import bundle, setup


# The reviewed event mission reward contract holds for these unpacked tables; a new fingerprint means reviewing it again.
REVIEWED_SHA256 = MappingProxyType({
    EMC_TABLE: "d1f68a280561e5826759865501bcff414655c87579ded5211260cd5baeba0ab2",
    SAVE_TABLE: "d7358418a12443941a47d52441137d9a642a37256f2a49ef15ed165975cf006d",
    REWARD_TABLE: "bc20bbc594c9a95548536a5efcd6a591cd07348118a49e7638ca7703e222626d",
})
RANGES = MappingProxyType({(6401, 6405): 183, (6611, 6615): 212, (30018, 30023): 587, (4094, 4101): 27,
                           (7894, 7902): 392, (8254, 8262): 450, (4019, 4030): 18})
SUBJUGATION_LADDERS = MappingProxyType({400183: (183, 123, 16), 400212: (212, 141, 16), 400587: (587, 462, 33)})
SCORE_QUEST_COUNT = 16
# The row after entry 18 carries only ID_Save 13, so the entry holds special reward groups 12 and 13.
CONTINUED_ENTRY, CONTINUED_KEYS = 18, (12, 13)
# Repeated heads merge into one group that keeps the Days of the later head row.
MERGED_HEADS = MappingProxyType({262: (491, 20250313), 245: (434, 20240229)})
# Groups and condition rows by the Type_Quest of the entry they attach to.
SAVE_KINDS = MappingProxyType({0: (1, {"13": 11}), 1: (4, {"12": 1, "14": 1, "2": 2}), 2: (39, {"3": 766}),
                               4: (2, {"4": 2}), 5: (151, {"1": 106, "2": 3535}), 7: (12, {"4": 14})})
LADDER_KINDS = MappingProxyType({2: (2, {"5": 86}), 4: (159, {"4": 2807, "6": 1032, "7": 317}), 5: (18, {"9": 342}),
                                 7: (13, {"4": 455, "6": 39, "7": 13})})
# Special reward group 230 as RewardText320390 words it: (incentive, stars, main, sub, amount, item).
REPRODUCE_GROUP, REPRODUCE_ENTRY, REPRODUCE_DAYS, REPRODUCE_TIME = 230, 392, 20230427, 1300
REPRODUCE_TIERS = (
    (1, 2, 1, 0, 1758, ""), (1, 3, 3, 0, 2, ""), (1, 4, 3, 0, 3, ""), (1, 5, 4, 0, 1, ""), (1, 6, 2, 0, 10, ""),
    (1, 7, 3, 0, 4, ""), (1, 8, 3, 0, 5, ""), (1, 9, 4, 0, 2, ""), (1, 10, 3, 0, 6, ""), (1, 11, 2, 0, 25, ""),
    (1, 12, 3, 0, 7, ""), (1, 13, 4, 0, 3, ""), (1, 14, 3, 0, 8, ""), (1, 15, 2, 0, 40, ""), (1, 16, 3, 0, 9, ""),
    (1, 17, 4, 0, 4, ""), (1, 18, 3, 0, 10, ""), (1, 19, 4, 0, 5, ""), (1, 20, 2, 0, 55, ""), (1, 21, 2, 0, 80, ""),
    (2, 1, 1, 0, 750, ""), (3, 22, 1, 0, 136, ""), (4, 23, 1, 0, 433, ""), (5, 24, 5, 6, 2, ""),
    (6, 25, 1, 0, 397, ""), (7, 26, 5, 6, 3, ""), (8, 27, 5, 22, 1, "gift_0105_ring"),
)
STAR_CONDITION = 2
HISTORY_LADDER = MappingProxyType({"emc_id": 27, "group": 1, "first_quest": 4094, "last_quest": 4101,
                                   "reason": LADDER_HIDDEN})
UNPROMISED_REPRODUCE = MappingProxyType({"reason": NOT_PROMISED["Reproduce"], "entries": [392, 450], "groups": 2,
                                         "quests": 18, "days": [20230427, 20240613]})
UNPROMISED_HISTORY = (155, 156, 1429)
SCORE_QUEST = 30019
# The event metadata keeps entry 311 after its empire quests leave the current QuestTable.
RETIRED_QUEST, RETIRED_ENTRY = 7317, 311
ROW_FIELDS = tuple(field.name for field in fields(IncentiveRow))
EMERGENCY_MISSION, EMPIRE_MISSION, SUBJUGATION_MISSION = 200001, 940001, 400001
EMPIRE_CATEGORY, SUBJUGATION_CATEGORY = 16, 2
STORY_TYPE, EMPIRE_TYPE = 0, 18
# FIRST is the smallest member of the synthetic emergency mission; CURRENT also belongs to an empire mission.
FIRST, CURRENT, SCORED = 7001, 7002, 6001
TRIAL_KIND = 5
ENTRY_FIELDS = ("ID_Mission", "ID_EventText", "ID_QuestFirst", "ID_QuestLast", "ID_FinishEvent", "Data_EventBegin",
                "Data_EventEnd", "Type_State", "Type_Quest", "Flag_OpeningEvent", "Flag_Blessing", "Flag_Trial",
                "Flag_DropReset", "ID_Save", "ID_ResultReward", "_Command")


def emc(emc_id, first=0, last=None, *, kind=0, state=0, save=0, reward=0):
    row = dict.fromkeys(ENTRY_FIELDS, 0)
    row.update({"ID_Mission": emc_id, "ID_QuestFirst": first, "ID_QuestLast": first if last is None else last,
                "Type_State": state, "Type_Quest": kind, "ID_Save": save, "ID_ResultReward": reward, "_Command": ""})
    return row


def tier(group, incentive, condition, threshold=1, *, sub=2, amount=100, item=""):
    return {"ID_Reward": group, "ID_Incentive": incentive, "Type_Condition": condition, "Param_Condition": threshold,
            "Param_ValidCondition": 0, "Param_Valid1": 0, "Param_Valid2": 0, "Type_MainIncentive": 5,
            "Type_SubIncentive": sub, "Param_Incentive": amount, "ID_Item": item}


def distribution(group, incentive, condition, threshold=1, *, days=0, time=0, main=1, sub=0, amount=1, item=""):
    return {"ID_Reward": group, "Days": days, "Time": time, "ID_Incentive": incentive, "Type_Condition": condition,
            "Param_Condition": threshold, "Type_Incentive": main, "Type_IncentiveItem": sub, "Param_Incentive": amount,
            "ID_Item": item}


def synthetic_windows():
    return MissionWindows({
        EMPIRE_CATEGORY: ([config(EMPIRE_MISSION)], [{"MissionID": EMPIRE_MISSION, "QuestID": CURRENT}]),
        EMERGENCY_CATEGORY: ([config(EMERGENCY_MISSION)],
                             [{"MissionID": EMERGENCY_MISSION, "QuestID": quest_id} for quest_id in (CURRENT, FIRST)]),
    }, {FIRST: 1, CURRENT: 1})


class Resources:
    resource_version = "synthetic-event-mission-resources"

    def __init__(self, *, entries=(), ladders=(), saves=(), types=None):
        self.tables = {EMC_TABLE: [dict(row) for row in entries], REWARD_TABLE: [dict(row) for row in ladders],
                       SAVE_TABLE: [dict(row) for row in saves]}
        self.types = {FIRST: EMPIRE_TYPE, CURRENT: EMPIRE_TYPE, SCORED: SUBJUGATION_TYPE} if types is None else types
        self.windows = synthetic_windows()
        self.reads = Counter()

    def resource_rows(self, name):
        self.reads[name] += 1
        return tuple(MappingProxyType(row) for row in self.tables[name])

    def resource_sha256(self, name):
        return hashlib.sha256(json.dumps(self.tables[name], sort_keys=True).encode("utf-8")).hexdigest()

    def quest_type(self, quest_id):
        return self.types.get(quest_id)

    def mission_windows(self, quest_type):
        return self.windows

    @property
    def all_ids(self):
        return tuple(sorted(quest_id for quest_id, kind in self.types.items() if kind == STORY_TYPE))

    @property
    def mode_ids(self):
        return tuple(sorted(quest_id for quest_id, kind in self.types.items() if kind != STORY_TYPE))


def synthetic(**tables):
    return EventMissionCatalog(Resources(**tables))


def emergency(subject):
    return subject.quests.windows.mission(EMERGENCY_MISSION)


def empire(subject):
    return subject.quests.windows.mission(EMPIRE_MISSION)


def subjugation_mission():
    return MissionWindow(SUBJUGATION_MISSION, SUBJUGATION_CATEGORY, True, None, None)


def reward(incentive, condition, threshold, main=1, amount=1):
    return IncentiveRow(incentive, condition, threshold, main, 0, amount, "")


def refused(call, code, status):
    with pytest.raises(ProtocolError) as error:
        call()
    assert (error.value.code, error.value.status) == (code, status)
    return error.value


@pytest.fixture(scope="module")
def catalog(bundle):
    return EventMissionCatalog(bundle.quests.catalog)


@pytest.fixture(scope="module")
def subjugation(bundle, catalog):
    return SubjugationCatalog(bundle.quests.catalog, events=catalog)


def test_new_nonzero_ids_open_entries_and_other_rows_add_their_keys():
    subject = synthetic(entries=[emc(0, save=9), emc(5, 10, 19, kind=TRIAL_KIND, save=1), emc(0, save=2),
                                 emc(5, 30, 39, reward=3), emc(6, 20, 29)])
    assert [entry.to_dict() for entry in subject.entries] == [
        {"emc_id": 5, "first_quest": 10, "last_quest": 19, "kind": TRIAL_KIND, "state": 0, "save_keys": [1, 2],
         "ladder_keys": [3]},
        {"emc_id": 6, "first_quest": 20, "last_quest": 29, "kind": 0, "state": 0, "save_keys": [], "ladder_keys": []}]
    assert (subject.entry(15).emc_id, subject.entry(35)) == (5, None)
    assert subject.status()["rows"][EMC_TABLE] == {"rows": 5, "continuations": 2, "skipped": 1}


def test_a_known_id_mission_later_in_the_file_rewrites_its_columns_and_keeps_every_key():
    subject = synthetic(entries=[emc(5, 10, 19, kind=TRIAL_KIND, save=1), emc(6, 20, 29),
                                 emc(5, 30, 39, kind=COLLECTION_KIND, reward=3)])
    rewritten = subject.entries[0]
    assert (rewritten.emc_id, rewritten.first_quest, rewritten.last_quest, rewritten.kind) == (5, 30, 39, COLLECTION_KIND)
    assert (rewritten.save_keys, rewritten.ladder_keys) == ((1,), (3,))
    assert (subject.entry(15), subject.entry(35).emc_id) == (None, 5)
    assert subject.status()["rows"][EMC_TABLE] == {"rows": 3, "continuations": 0, "skipped": 0}


def test_the_first_entry_by_id_mission_covers_a_quest_and_claims_its_group():
    subject = synthetic(entries=[emc(9, 1, 20, reward=1), emc(3, 10, 30, reward=1)], ladders=[tier(1, 1, 4, 100)])
    assert [entry.emc_id for entry in subject.entries] == [3, 9]
    assert (subject.entry(15).emc_id, subject.entry(5).emc_id) == (3, 9)
    assert subject.reward_group(1).emc_id == 3 and subject.ladder(subject.entry(15)) == (
        IncentiveRow(1, 4, 100, 5, 2, 100, ""),)
    assert subject.ladder_group(subject.entry(5)) is None and subject.ladder(subject.entry(5)) == ()


def test_groups_no_entry_names_are_dropped_and_split_into_rank_and_orphaned_groups():
    subject = synthetic(entries=[emc(1, CURRENT, reward=2)],
                        ladders=[tier(7, 1, 4), tier(0, 2, 6), tier(2, 1, 5), tier(8, 1, 10), tier(0, 2, 4)])
    assert (subject.reward_group(7), subject.reward_group(8)) == (None, None)
    assert [row.incentive for row in subject.ladder(subject.entry(CURRENT))] == [1]
    status = subject.status()
    assert status["ladder_groups"] == {"attached": 1, "rank": 1, "orphaned": 1}
    assert status["rows"][REWARD_TABLE] == {"rows": 5, "skipped": 4}


def test_heads_incentives_and_conditions_follow_the_native_loader():
    subject = synthetic(entries=[emc(1, 1, 10, save=1), emc(2, 11, 20, save=2)], saves=[
        distribution(0, 1, STAR_CONDITION),
        distribution(1, 2, STAR_CONDITION, 1, days=20230101, time=1300, amount=750),
        distribution(0, 1, 0, 2, amount=1758),
        distribution(0, 0, 0, 3, main=3, amount=2),
        distribution(0, 2, 0, 4, amount=5),
        # A head without an incentive loads nothing, so its Type_Condition is never read.
        distribution(2, 0, 7, 1, days=20230202),
        distribution(0, 1, 0, 2),
        distribution(1, 3, 4, 9, days=20240101, time=1000),
    ])
    merged = subject.save_group(1)
    assert merged.rows == (reward(1, STAR_CONDITION, 2, amount=1758), reward(1, STAR_CONDITION, 3, 3, 2),
                           reward(2, STAR_CONDITION, 1, amount=750), reward(2, STAR_CONDITION, 4, amount=5),
                           reward(3, 4, 9))
    assert (merged.emc_id, merged.days, merged.time) == (1, 20240101, 1000)
    carried = subject.save_group(2)
    assert (carried.rows, carried.days, carried.time) == ((reward(1, STAR_CONDITION, 2),), 20230202, 0)
    assert subject.status()["rows"][SAVE_TABLE] == {"rows": 8, "skipped": 2}
    assert subject.save_groups(subject.entry(5)) == (merged,) and subject.save_groups(subject.entry(15)) == (carried,)


def valid_resources():
    return Resources(entries=[emc(1, CURRENT, save=4, reward=10)], ladders=[tier(10, 1, 4)],
                     saves=[distribution(4, 1, 3)])


@pytest.mark.parametrize("table,column,value", [
    (EMC_TABLE, "ID_Mission", "1"), (EMC_TABLE, "ID_QuestLast", 2 ** 31), (EMC_TABLE, "ID_Save", None),
    (REWARD_TABLE, "Type_Condition", True), (REWARD_TABLE, "ID_Item", 0), (REWARD_TABLE, "Param_Valid2", 1.0),
    (SAVE_TABLE, "Days", 1.5), (SAVE_TABLE, "Param_Incentive", -2 ** 31 - 1), (SAVE_TABLE, "ID_Item", None),
], ids=["text_id", "wide_range", "empty_save", "boolean_condition", "numeric_item", "float_valid", "float_days",
        "narrow_amount", "empty_item"])
def test_cells_outside_the_native_types_refuse_the_whole_catalog(table, column, value):
    resources = valid_resources()
    resources.tables[table][0][column] = value
    subject = EventMissionCatalog(resources)
    refused(lambda: subject.entries, RESOURCE_INVALID, 503)
    status = subject.status()
    assert (status["ready"], status["error"]["code"]) == (False, RESOURCE_INVALID)


@pytest.mark.parametrize("table,column", [(EMC_TABLE, "Type_Quest"), (REWARD_TABLE, "Param_ValidCondition"),
                                          (SAVE_TABLE, "Time")])
def test_missing_columns_refuse_the_whole_catalog(table, column):
    resources = valid_resources()
    del resources.tables[table][0][column]
    refused(lambda: EventMissionCatalog(resources).entries, RESOURCE_INVALID, 503)


@pytest.mark.parametrize("first_kind,ladder_quest,condition,current,first", [
    (COLLECTION_KIND, CURRENT, COLLECTION_KEY, {Family.EMERGENCY}, set()),
    (COLLECTION_KIND, CURRENT, TRIAL_KEY, set(), set()),
    (TRIAL_KIND, CURRENT, TRIAL_KEY, {Family.EMERGENCY}, set()),
    (TRIAL_KIND, CURRENT, COLLECTION_KEY, set(), set()),
    (COLLECTION_KIND, FIRST, COLLECTION_KEY, set(), {Family.EMERGENCY}),
])
def test_emergency_ladders_come_from_the_current_quest_and_the_key_from_the_smallest_member(first_kind, ladder_quest,
                                                                                         condition, current, first):
    subject = synthetic(entries=[emc(1, FIRST, kind=first_kind, reward=10 if ladder_quest == FIRST else 0),
                                 emc(2, CURRENT, reward=10 if ladder_quest == CURRENT else 0)],
                        ladders=[tier(10, 1, condition)])
    assert subject.decision_key((CURRENT, FIRST)) == (COLLECTION_KEY if first_kind == COLLECTION_KIND else TRIAL_KEY)
    assert (subject.families(CURRENT, emergency(subject)), subject.families(FIRST, emergency(subject))) == (current, first)
    assert subject.families(CURRENT, empire(subject)) == NO_FAMILIES


def test_members_without_an_entry_compare_the_trial_key():
    subject = synthetic(entries=[emc(2, CURRENT, reward=10)], ladders=[tier(10, 1, TRIAL_KEY)])
    assert subject.decision_key((FIRST, CURRENT)) == subject.decision_key(()) == TRIAL_KEY
    assert subject.families(CURRENT, emergency(subject)) == {Family.EMERGENCY}


def test_special_rewards_on_any_member_entry_mark_emergency_starts_as_after_event():
    entries = [emc(1, FIRST, save=4), emc(2, CURRENT, save=5)]
    # Group 5 has a head but no incentive, so it loads no row to distribute.
    saves = [distribution(4, 1, 3, 25), distribution(5, 0, 3, 25)]
    subject = synthetic(entries=entries, saves=saves)
    assert subject.families(CURRENT, emergency(subject)) == {Family.AFTER_EVENT}
    assert subject.families(CURRENT, empire(subject)) == NO_FAMILIES
    assert subject.save_groups(subject.entry(CURRENT)) == ()
    hollow = synthetic(entries=entries, saves=saves[1:])
    assert hollow.families(CURRENT, emergency(hollow)) == NO_FAMILIES


def test_starts_outside_score_quests_and_emergency_missions_never_read_the_tables():
    resources = Resources(entries=[emc(1, CURRENT, save=4)], saves=[distribution(4, 1, 3)])
    subject = EventMissionCatalog(resources)
    assert subject.families(CURRENT, empire(subject)) == NO_FAMILIES
    subject.require_settled(CURRENT, MissionWindow(100001, 0, True, None, None))
    assert not resources.reads
    resources.tables[EMC_TABLE][0]["ID_Mission"] = "1"
    refused(lambda: subject.families(CURRENT, emergency(subject)), RESOURCE_INVALID, 503)
    assert set(resources.reads) == set(SOURCE_TABLES)
    with pytest.raises(ValueError):
        subject.families(CURRENT, EMERGENCY_MISSION)


def test_unsettled_families_refuse_with_501_while_the_settled_score_ladder_passes(monkeypatch):
    subject = synthetic(entries=[emc(1, FIRST, save=4), emc(2, CURRENT), emc(3, SCORED, reward=10)],
                        ladders=[tier(10, 1, 6, 100)], saves=[distribution(4, 1, 3, 25)])
    error = refused(lambda: subject.require_settled(CURRENT, emergency(subject)), UNSUPPORTED, 501)
    assert str(error) == f"Quest {CURRENT} shows after_event event rewards in mission {EMERGENCY_MISSION} that no mode settles"
    scored = subjugation_mission()
    assert subject.families(SCORED, scored) == {Family.SCORE} and subject.unsettled(SCORED, scored) == NO_FAMILIES
    subject.require_settled(SCORED, scored)
    monkeypatch.setattr(SUBJUGATION_POLICY, "event_reward_families", frozenset())
    error = refused(lambda: subject.require_settled(SCORED, scored), UNSUPPORTED, 501)
    assert str(error) == f"Quest {SCORED} shows score event rewards in mission {SUBJUGATION_MISSION} that no mode settles"
    unscored = synthetic(entries=[emc(3, SCORED, reward=10)], ladders=[tier(10, 1, COLLECTION_KEY)])
    assert unscored.families(SCORED, scored) == NO_FAMILIES


def test_settled_families_name_only_the_subjugation_score_ladder():
    assert SUBJUGATION_POLICY.event_reward_families == {"score"} and EVENT_REWARD_FAMILIES == frozenset(Family)
    assert {quest_type for quest_type, entry in registry().items() if entry.event_reward_families} == {SUBJUGATION_TYPE}
    assert policy(SUBJUGATION_TYPE) is SUBJUGATION_POLICY and SUBJUGATION_TYPE in SCORE_TYPES
    assert SCORE_CONDITIONS == frozenset(TIER_SOURCES) and settled_families(SUBJUGATION_TYPE) == {Family.SCORE}
    assert all(settled_families(quest_type) == NO_FAMILIES for quest_type in (None, STORY_TYPE, 7, 13, EMPIRE_TYPE))


def test_settled_families_follow_the_policy_each_quest_type_resolves_to(monkeypatch):
    subject = synthetic(entries=[emc(1, FIRST, save=4), emc(2, CURRENT)], saves=[distribution(4, 1, 3, 25)])
    refused(lambda: subject.require_settled(CURRENT, emergency(subject)), UNSUPPORTED, 501)
    declared = type("AfterEventPolicy", (ModePolicy,), {"event_reward_families": frozenset({"after_event"})})()
    resolved = quest_modes.policy
    monkeypatch.setattr(quest_modes, "policy",
                        lambda quest_type: declared if quest_type == EMPIRE_TYPE else resolved(quest_type))
    assert settled_families(EMPIRE_TYPE) == {Family.AFTER_EVENT}
    assert subject.unsettled(CURRENT, emergency(subject)) == NO_FAMILIES
    subject.require_settled(CURRENT, emergency(subject))


def test_content_version_follows_the_sources_and_the_settled_families(monkeypatch):
    def version(**changes):
        return synthetic(entries=[emc(1, CURRENT, **changes)]).content_version

    original = version()
    assert original == version() and original != version(kind=COLLECTION_KIND)
    monkeypatch.setattr(SUBJUGATION_POLICY, "event_reward_families", frozenset())
    assert version() != original


def test_status_and_describe_report_refusals_and_emergency_decisions():
    resources = Resources(entries=[emc(1, FIRST, kind=COLLECTION_KIND, save=4), emc(2, CURRENT, reward=10)],
                          ladders=[tier(10, 1, COLLECTION_KEY), tier(11, 1, 10)],
                          saves=[distribution(4, 1, 3, 25, days=20261015)],
                          types={FIRST: EMPIRE_TYPE, CURRENT: EMPIRE_TYPE})
    subject = EventMissionCatalog(resources)
    status = subject.status()
    assert (status["ready"], status["quests"], status["failures"]) == (True, 2, {})
    assert status["refused"] == [
        {"quest_id": FIRST, "mission_id": EMERGENCY_MISSION, "listed": True, "families": ["after_event"],
         "unsettled": ["after_event"]},
        {"quest_id": CURRENT, "mission_id": EMERGENCY_MISSION, "listed": True, "families": ["after_event", "emergency"],
         "unsettled": ["after_event", "emergency"]}]
    assert status["families"] == {"after_event": 2, "emergency": 1, "score": 0}
    assert status["ladder_groups"] == {"attached": 1, "rank": 1, "orphaned": 0}
    assert status["not_promised"] == {"ladders": [], "saves": {}}
    assert (status["resource_version"], status["source_sha256"]) == (
        Resources.resource_version, {name: resources.resource_sha256(name) for name in SOURCE_TABLES})
    view = subject.describe(CURRENT)
    assert view["missions"] == [
        {"mission_id": EMERGENCY_MISSION, "category": "Emergency", "listed": True,
         "families": ["after_event", "emergency"], "unsettled": ["after_event", "emergency"],
         "decision_key": COLLECTION_KEY, "member_entry": 1},
        {"mission_id": EMPIRE_MISSION, "category": "Empire", "listed": True, "families": [], "unsettled": [],
         "decision_key": None, "member_entry": None}]
    assert (view["entry"]["emc_id"], view["ladder"]["group"], view["saves"], view["not_promised"]) == (2, 10, [], {})
    assert subject.describe(FIRST)["saves"] == [{"group": 4, "emc_id": 1, "rows": 1, "incentives": 1,
                                                 "conditions": {"3": 1}, "main_incentives": {"1": 1},
                                                 "days": 20261015, "time": 0}]
    refused(lambda: subject.describe(SCORED), "quest_not_found", 404)
    status["refused"].clear()
    assert subject.status()["refused"]


def test_special_rewards_of_quests_outside_every_mission_window_report_no_unpromised_reason():
    resources = Resources(entries=[emc(1, FIRST, save=4), emc(2, CURRENT, save=5)],
                          saves=[distribution(4, 1, 3, days=20261015), distribution(5, 1, 3, days=20261016)],
                          types={FIRST: EMPIRE_TYPE, CURRENT: EMPIRE_TYPE})
    resources.windows = MissionWindows({EMPIRE_CATEGORY: (
        [config(EMPIRE_MISSION)], [{"MissionID": EMPIRE_MISSION, "QuestID": CURRENT}])}, {CURRENT: 1})
    subject = EventMissionCatalog(resources)
    windowless, windowed = subject.describe(FIRST), subject.describe(CURRENT)
    assert (windowless["missions"], [group["group"] for group in windowless["saves"]]) == ([], [4])
    assert windowless["not_promised"] == {}
    assert windowed["not_promised"] == {"saves": {"Empire": SAVES_UNREAD}}
    assert subject.status()["not_promised"] == {"ladders": [], "saves": {"Empire": {
        "reason": SAVES_UNREAD, "entries": [2], "groups": 1, "quests": 1, "days": [20261016, 20261016]}}}


def test_source_tables_keep_the_reviewed_fingerprints(catalog):
    assert catalog.status()["source_sha256"] == dict(REVIEWED_SHA256)


def test_the_continuation_row_adds_its_special_reward_group_to_entry_18(catalog):
    entry = next(entry for entry in catalog.entries if entry.emc_id == CONTINUED_ENTRY)
    assert (entry.first_quest, entry.last_quest, entry.save_keys, entry.ladder_keys) == (4019, 4030, CONTINUED_KEYS, ())
    assert tuple(group.group for group in catalog.save_groups(entry)) == CONTINUED_KEYS
    assert all(entry.emc_id for entry in catalog.entries) and catalog.entry(4025) is entry


def test_reward_groups_match_the_reviewed_inventory(catalog):
    status = catalog.status()
    assert status["entries"] == 589 and status["rows"] == {
        EMC_TABLE: {"rows": 590, "continuations": 1, "skipped": 0}, REWARD_TABLE: {"rows": 9177, "skipped": 4086},
        SAVE_TABLE: {"rows": 4438, "skipped": 0}}
    assert status["ladder_groups"] == {"attached": 192, "rank": 121, "orphaned": 160}
    assert status["save_groups"] == {"attached": 209, "unattached": 0}
    for group, (emc_id, days) in MERGED_HEADS.items():
        merged = catalog.save_group(group)
        assert (merged.emc_id, merged.days, len(merged.rows)) == (emc_id, days, 2)


def by_kind(catalog, groups):
    entries, counted = {entry.emc_id: entry for entry in catalog.entries}, {}
    for group in groups:
        count, conditions = counted.get(entries[group.emc_id].kind, (0, Counter()))
        counted[entries[group.emc_id].kind] = (count + 1, conditions + Counter(str(row.condition) for row in group.rows))
    return {kind: (count, dict(sorted(conditions.items()))) for kind, (count, conditions) in sorted(counted.items())}


def test_groups_split_into_families_by_the_kind_of_their_entry(catalog):
    saves = [group for entry in catalog.entries for group in catalog.save_groups(entry)]
    ladders = [group for entry in catalog.entries if (group := catalog.ladder_group(entry)) is not None]
    assert (by_kind(catalog, saves), by_kind(catalog, ladders)) == (dict(SAVE_KINDS), dict(LADDER_KINDS))


def test_special_reward_group_230_follows_its_reward_text_in_incentive_order(catalog):
    group = catalog.save_group(REPRODUCE_GROUP)
    assert (group.emc_id, group.days, group.time) == (REPRODUCE_ENTRY, REPRODUCE_DAYS, REPRODUCE_TIME)
    assert [(row.incentive, row.threshold, row.main_incentive, row.sub_incentive, row.amount, row.item_id)
            for row in group.rows] == list(REPRODUCE_TIERS)
    assert {(row.condition, row.valid_condition, row.valid_low, row.valid_high) for row in group.rows} == {
        (STAR_CONDITION, 0, 0, 0)}


def test_quest_ranges_resolve_to_their_reviewed_entries(catalog):
    assert {(first, last): {catalog.entry(quest_id).emc_id for quest_id in range(first, last + 1)}
            for first, last in RANGES} == {key: {emc_id} for key, emc_id in RANGES.items()}


def test_reward_ranges_project_the_entry_and_its_claimed_ladder_group(bundle, catalog, subjugation):
    assert subjugation.events is catalog
    quests, differences = bundle.quests.catalog, []
    for quest_id in sorted({*quests.all_ids, *quests.mode_ids}):
        entry, projected = catalog.entry(quest_id), subjugation.reward_range(quest_id)
        group = catalog.ladder_group(entry)
        resolved = None if entry is None else (entry.emc_id, entry.first_quest, entry.last_quest,
                                               0 if group is None else group.group)
        if resolved != (None if projected is None else tuple(projected)):
            differences.append(quest_id)
    assert differences == []


def test_subjugation_ladders_are_the_catalog_rows_of_their_entry(catalog, subjugation):
    compared = {}
    for mission_id in subjugation.mission_ids:
        [group] = {catalog.ladder_group(catalog.entry(quest_id)) for quest_id in subjugation.mission(mission_id).quests}
        tiers = subjugation.ladder(mission_id)
        assert group.group == subjugation.reward_group(mission_id) and {tier.group for tier in tiers} == {group.group}
        assert [{name: getattr(tier, name) for name in ROW_FIELDS} for tier in tiers] == [asdict(row) for row in group.rows]
        assert tiers == tuple(RewardTier.from_incentive(group.group, row) for row in group.rows)
        compared[mission_id] = (group.emc_id, group.group, len(group.rows))
    assert compared == dict(SUBJUGATION_LADDERS)


def test_only_the_subjugation_quests_show_a_family_and_the_gate_refuses_none(bundle, catalog, subjugation):
    status = catalog.status()
    assert (status["ready"], status["quests"], status["refused"], status["failures"]) == (True, 3514, [], {})
    assert status["families"] == {"after_event": 0, "emergency": 0, "score": SCORE_QUEST_COUNT}
    windows = bundle.quests.catalog.mission_windows(SUBJUGATION_TYPE)
    members = {quest_id for mission_id in subjugation.mission_ids for quest_id in subjugation.mission(mission_id).quests}
    assert {quest_id for quest_id in members for mission in windows.missions(quest_id)
            if catalog.families(quest_id, mission) == {Family.SCORE} and not catalog.unsettled(quest_id, mission)} == members
    assert len(members) == SCORE_QUEST_COUNT


def test_status_reports_unpromised_history_and_reproduce_rewards(catalog):
    status = catalog.status()
    unpromised = status["not_promised"]
    assert unpromised["ladders"] == [dict(HISTORY_LADDER)] and set(unpromised["saves"]) == {"History", "Reproduce"}
    assert unpromised["saves"]["Reproduce"] == dict(UNPROMISED_REPRODUCE)
    history = unpromised["saves"]["History"]
    assert history["reason"] == NOT_PROMISED["History"]
    assert (len(history["entries"]), history["groups"], history["quests"]) == UNPROMISED_HISTORY
    assert (status["rules"], status["settled"], status["official_server_rules"]) == (
        dict(RULES), {str(SUBJUGATION_TYPE): ["score"]}, False)
    assert status["content_version"] == catalog.content_version and status == catalog.status()


def test_describe_reports_entries_groups_and_mission_decisions(catalog):
    score = catalog.describe(SCORE_QUEST)
    assert (score["entry"]["emc_id"], score["ladder"]["group"], score["settled"], score["not_promised"]) == (
        587, 462, ["score"], {})
    assert [(view["mission_id"], view["category"], view["families"], view["unsettled"]) for view in score["missions"]] == [
        (OPEN_MISSION, "Subjugation", ["score"], [])]
    history = catalog.describe(4094)
    assert (history["ladder"]["conditions"], [group["group"] for group in history["saves"]]) == ({"5": 40}, [19])
    assert history["not_promised"] == {"ladder": LADDER_HIDDEN, "saves": {"History": NOT_PROMISED["History"]}}
    reproduce = catalog.describe(7894)
    assert [(group["group"], group["days"], group["time"]) for group in reproduce["saves"]] == [
        (REPRODUCE_GROUP, REPRODUCE_DAYS, REPRODUCE_TIME)]
    assert reproduce["not_promised"] == {"saves": {"Reproduce": NOT_PROMISED["Reproduce"]}}


def test_retired_quest_keeps_event_metadata_without_a_current_mission_decision(catalog):
    assert catalog.entry(RETIRED_QUEST).emc_id == RETIRED_ENTRY
    refused(lambda: catalog.describe(RETIRED_QUEST), "quest_not_found", 404)


def test_a_score_quest_whose_ladder_no_mode_settles_is_refused_before_charging(scene, monkeypatch):
    service, store = scene.service(), scene.store
    before = store.profile()
    with monkeypatch.context() as patch:
        patch.setattr(SUBJUGATION_POLICY, "event_reward_families", frozenset())
        error = refused(lambda: service.start(start(service, store, OPENER), store), UNSUPPORTED, 501)
    assert str(error) == f"Quest {OPENER} shows score event rewards in mission {OPEN_MISSION} that no mode settles"
    assert store.profile() == before
    service.start(start(service, store, OPENER), store)
    assert store.profile().state["quest_state"]["active"]["quest_id"] == OPENER
