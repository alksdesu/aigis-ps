from collections import Counter
from datetime import datetime
import json
from types import MappingProxyType
from xml.etree import ElementTree as ET

import pytest

from aigis_ps import quest_modes
from aigis_ps.business_calendar import business_day
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.protocol import ProtocolRegistry, encode_wire, prepare_request
from aigis_ps.quest_catalog import QuestCatalog
from aigis_ps.quest_mode_raid import POLICY
from aigis_ps.quest_modes import CLEAR_FLAG, MODE_SPECS, PLATFORM_CODES, THREE_STAR_FLAG, StartContext
from test_quest_battle_modes import (
    Recorder, bare_start, card_ids, charisma, gold_events, hard_start, mutate, new_cards, put_record, record, set_rank,
    three_star_gold, update_game_param,
)
from test_quest_mode_devil import DEVIL_CATEGORY, DEVIL_RANK, MENU_MISSION as DEVIL_MENU_MISSION, retag
from test_quest_service import NOW, allow_quest, assert_refusal, bundle, finish_request, nodes, setup
from test_quest_settlement import formation


RAID_TYPE = 11
RAID_CATEGORY = 10
GATE_QUEST = 1004
RAID_RANK = 20
CHARISMA = 1000
PLATFORM, OTHER_PLATFORM = 1, 2
# Every raid mission enables platform bits 0-12 (Enable 8191).
UNLISTED_PLATFORM = 13
INVALID_PLATFORMS = (PLATFORM_CODES.start - 1, PLATFORM_CODES.stop, True, "1", None)
TODAY = datetime(2026, 11, 2, 12, tzinfo=JST)
MENU_TAG = 120
RAID_QUESTS, OPEN_QUESTS, CLOSED_QUESTS = 92, 55, 37
PERMANENT_MISSIONS = (900170, 900184, 900198, 900211, 900225, 900259, 900296, 900312, 900336, 900345, 900368)
ENDED_MISSIONS = (900380, 900405, 900429, 900456, 900483, 900531, 900542, 900580)
# The first permanent raid mission is the first registered one tagged 120, so it sets the menu rank.
MENU_MISSION = PERMANENT_MISSIONS[0]
FIRST_QUEST, ENDED_MISSION_QUEST = 6314, 7806
PLATINUM_ARMOR, BLACK_ARMOR = 133, 250
COMMON_STAR_UNITS = [(PLATINUM_ARMOR, 57), (BLACK_ARMOR, 19)]
UNIQUE_STAR_UNITS = 16
CLEARED = CLEAR_FLAG | THREE_STAR_FLAG
INT32_MAX = 0x7FFFFFFF
ARMOR_STAR_REWARD = {"kind": "unit", "reward_type": 3, "count": 1, "card_id": PLATINUM_ARMOR, "item_id": None}
QUEST_RANK_SHORT = "Player rank is below the quest requirement"
MENU_RANK_SHORT = "Player rank has not reached the raid menu rank of Tag 120"
NO_ENTRUST = "Mode raid overrides check_start without a declared entrust counterpart"
# CRaidSallyView uses three raid groups (UseTeamCount 3) and 88AA20 departs with subteam -1, so one BL may list them all.
RAID_GROUPS = 3
UNITS_PER_GROUP = 2
GROUP_CARD = 67
ABSENT_INDEX = 999999


def refused(code, status, callback):
    with pytest.raises(ProtocolError) as error:
        callback()
    assert (error.value.code, error.value.status) == (code, status)


def refused_start(code, status, service, store, starting):
    before = store.profile()
    refused(code, status, lambda: service.start(starting, store))
    assert store.profile() == before


def cleared(*quest_ids):
    return {quest_id: {"QuestID": quest_id, "MaxRank": 3, "AchieveFlag": CLEARED} for quest_id in quest_ids}


def context(catalog, quest_id, records, *, rank=RAID_RANK, hard=False, platform=PLATFORM):
    return StartContext(catalog, catalog.get(quest_id), catalog.mode(quest_id), records, rank, TODAY, hard, platform)


def on_platform(envelope, code):
    # Re-signed copy of the request, so only its PlatformCode differs.
    document = ET.fromstring(envelope.payload_xml())
    document.find("PlatformCode/V").text = str(code)
    xml = prepare_request(ET.tostring(document, encoding="utf-8", short_empty_elements=False), envelope.profile.st)
    return ProtocolRegistry.load().decode(envelope.profile.route, encode_wire(xml, envelope.profile.request_key))


@pytest.fixture
def raid(setup, bundle):
    service, store = setup
    allow_quest(store, GATE_QUEST)
    set_rank(bundle, store, RAID_RANK)
    update_game_param(store, NowCharisma=CHARISMA)
    return service, store


def test_raid_policy_is_registered_with_the_star_mode_contract_and_stays_out_of_entrust():
    assert quest_modes.policy(RAID_TYPE) is POLICY
    assert POLICY.status() == {"policy": "raid", "enabled": True, "reason": None, "hard_mode": False,
                               "timed_conditions": False, "gate_quest": GATE_QUEST}
    spec = MODE_SPECS[RAID_TYPE]
    assert (spec.mission_category, spec.hard_rank, spec.gold_counted, spec.score_mode, spec.entrust) == (
        RAID_CATEGORY, None, False, False, False)
    assert POLICY.entrust_blocker() == NO_ENTRUST


def test_raid_local_rules_name_the_native_start_path_the_menu_tag_and_the_ignored_boost():
    rules = quest_modes.support_status()[RAID_TYPE]["local_rules"]
    assert rules == POLICY.local_rules() and json.loads(json.dumps(rules)) == rules
    assert (rules["menu_tag"], rules["boost"]) == (MENU_TAG, "accepted_and_ignored")
    assert all(step in rules["start_path"] for step in ("77AB70", "DoQuestPlayable::Raid", "DoQuestStart_Raid"))
    rules["menu_tag"] = 0
    assert POLICY.local_rules()["menu_tag"] == MENU_TAG


def test_every_raid_quest_builds_without_prize_candidates_and_pays_one_first_star_unit(bundle):
    catalog = bundle.quests.catalog
    quest_ids = catalog.playable_quests(RAID_TYPE)
    assert len(quest_ids) == RAID_QUESTS and set(quest_ids) <= set(catalog.mode_ids)
    units = Counter()
    for quest_id in quest_ids:
        quest, mode = catalog.get(quest_id), catalog.mode(quest_id)
        assert quest.candidates == () and not any(quest.treasures) and quest.rank == RAID_RANK
        assert (mode.mission_category, mode.hard, mode.gold_counted, mode.score_mode) == (RAID_CATEGORY, None, False, False)
        assert mode.map_mission_id is None and mode.map_archive == f"Map{quest.map_no:04d}.aar"
        assert mode.star_rewards_hard == () and len(mode.star_rewards_normal) == 1
        reward = mode.star_rewards_normal[0]
        assert (reward.kind, reward.reward_type, reward.count) == ("unit", 3, 1)
        units[reward.card_id] += 1
    assert units.most_common(len(COMMON_STAR_UNITS)) == COMMON_STAR_UNITS
    assert len(units) == len(COMMON_STAR_UNITS) + UNIQUE_STAR_UNITS
    assert not set(catalog.status()["mode_failed_quests"]) & set(quest_ids)


def test_admission_opens_the_55_quests_of_the_eleven_permanent_missions_on_2026_11_02(bundle):
    catalog = bundle.quests.catalog
    quest_ids = catalog.playable_quests(RAID_TYPE)
    # Every raid record holds three stars, so only mission windows decide.
    records = cleared(GATE_QUEST, *quest_ids)
    outcomes = {}
    for quest_id in quest_ids:
        try:
            outcomes[quest_id] = quest_modes.admit(POLICY, context(catalog, quest_id, records)).mission_id
        except ProtocolError as error:
            outcomes[quest_id] = error.code
    admitted = [quest_id for quest_id, outcome in outcomes.items() if type(outcome) is int]
    assert len(admitted) == OPEN_QUESTS and {outcomes[quest_id] for quest_id in admitted} == set(PERMANENT_MISSIONS)
    assert Counter(outcome for outcome in outcomes.values() if type(outcome) is str) == {"quest_closed": CLOSED_QUESTS}


def test_describe_reports_the_menu_mission_and_the_open_raid_quests_of_a_platform(bundle):
    catalog = bundle.quests.catalog
    view = POLICY.describe(catalog, now=TODAY, platform_code=PLATFORM)
    menu = view["menu"]
    assert (menu["mission"], menu["minimum_rank"], menu["open"]) == (MENU_MISSION, RAID_RANK, True)
    assert menu["candidate_count"] == menu["registered_count"] == len(PERMANENT_MISSIONS) + len(ENDED_MISSIONS)
    assert view["missions"] == {"upcoming": [], "open": list(PERMANENT_MISSIONS), "ended": list(ENDED_MISSIONS),
                                "disabled": []}
    assert view["quests"] == {"open": OPEN_QUESTS, "closed": CLOSED_QUESTS}
    assert json.loads(json.dumps(view)) == view
    unlisted = POLICY.describe(catalog, now=TODAY, platform_code=UNLISTED_PLATFORM)
    assert (unlisted["menu"]["mission"], unlisted["menu"]["reason"]) == (None, "not_registered")
    assert unlisted["missions"]["disabled"] == [*PERMANENT_MISSIONS, *ENDED_MISSIONS]
    assert unlisted["quests"] == {"open": 0, "closed": RAID_QUESTS}
    refused("quest_platform_invalid", 400, lambda: POLICY.describe(catalog, now=TODAY, platform_code=PLATFORM_CODES.stop))
    for clock in (TODAY.replace(tzinfo=None), None):
        refused("quest_clock_invalid", 503, lambda clock=clock: POLICY.describe(catalog, now=clock, platform_code=PLATFORM))


def test_raid_quests_open_after_the_menu_gate_quest_at_the_quest_rank(setup, bundle):
    service, store = setup
    update_game_param(store, NowCharisma=CHARISMA)
    set_rank(bundle, store, RAID_RANK)
    refused_start("quest_locked", 409, service, store, bare_start(service, store, FIRST_QUEST))
    allow_quest(store, GATE_QUEST)
    set_rank(bundle, store, RAID_RANK - 1)
    before = store.profile()
    assert_refusal("quest_rank_required", QUEST_RANK_SHORT,
                   lambda: service.start(bare_start(service, store, FIRST_QUEST), store))
    assert store.profile() == before
    set_rank(bundle, store, RAID_RANK)
    service.start(bare_start(service, store, FIRST_QUEST), store)
    active = store.profile().state["quest_state"]["active"]
    assert (active["quest_id"], active["mission_id"], active["mode"]["quest_type"]) == (FIRST_QUEST, MENU_MISSION, RAID_TYPE)
    assert charisma(store) == CHARISMA


def test_a_tag_120_mission_above_the_quest_rank_keeps_the_raid_menu_closed(raid, bundle, monkeypatch):
    service, store = raid
    catalog = service.catalog
    assert POLICY.menu_reached(catalog, rank=RAID_RANK, platform_code=PLATFORM)
    # A lower devil mission now covers 120 ahead of every raid mission, so its member rank sets the menu rank.
    retag(catalog, monkeypatch, lambda row: {"TagID": MENU_TAG} if row["MissionID"] == DEVIL_MENU_MISSION else {},
          DEVIL_CATEGORY)
    menu = catalog.mission_tags().tag_mission(MENU_TAG, PLATFORM)
    assert (menu.mission_id, catalog.mission_registry().minimum_rank(menu.mission_id)) == (DEVIL_MENU_MISSION, DEVIL_RANK)
    assert DEVIL_RANK > catalog.get(FIRST_QUEST).rank == RAID_RANK
    before = store.profile()
    assert_refusal("quest_rank_required", MENU_RANK_SHORT,
                   lambda: service.start(bare_start(service, store, FIRST_QUEST), store))
    assert store.profile() == before
    assert not POLICY.menu_reached(catalog, rank=DEVIL_RANK - 1, platform_code=PLATFORM)
    set_rank(bundle, store, DEVIL_RANK)
    service.start(bare_start(service, store, FIRST_QUEST, tick=1001), store)
    assert store.profile().state["quest_state"]["active"]["mission_id"] == MENU_MISSION


def test_raid_starts_need_a_registered_mission_carrying_tag_120(bundle, monkeypatch):
    catalog = bundle.quests.catalog
    records = cleared(GATE_QUEST)
    retag(catalog, monkeypatch, lambda row: {"TagID": 0}, RAID_CATEGORY)
    assert catalog.mission_tags().tag_mission(MENU_TAG, PLATFORM) is None
    # IsReachedRequiredRank is false when no mission carries the tag, whatever the player rank.
    assert_refusal("quest_rank_required", MENU_RANK_SHORT,
                   lambda: POLICY.check_start(context(catalog, FIRST_QUEST, records, rank=INT32_MAX)))


def test_raid_starts_resolve_the_menu_tag_on_the_request_platform(bundle, monkeypatch):
    catalog = bundle.quests.catalog
    records = cleared(GATE_QUEST)
    # Only the other platform registers the lower devil mission, so only its requests need that mission's rank.
    retag(catalog, monkeypatch, lambda row: {"TagID": MENU_TAG, "Enable": 1 << OTHER_PLATFORM}
          if row["MissionID"] == DEVIL_MENU_MISSION else {}, DEVIL_CATEGORY)
    assert POLICY.check_start(context(catalog, FIRST_QUEST, records)).mission_id == MENU_MISSION
    assert_refusal("quest_rank_required", MENU_RANK_SHORT,
                   lambda: POLICY.check_start(context(catalog, FIRST_QUEST, records, platform=OTHER_PLATFORM)))
    reached = context(catalog, FIRST_QUEST, records, rank=DEVIL_RANK, platform=OTHER_PLATFORM)
    assert POLICY.check_start(reached).mission_id == MENU_MISSION


def test_raid_starts_check_the_menu_after_the_base_rules_and_pass_registry_failures_through(bundle, monkeypatch):
    catalog = bundle.quests.catalog
    failure = ProtocolError("quest_mode_resource_invalid", "Mission windows do not satisfy the native contract", 503)

    def unavailable():
        raise failure

    monkeypatch.setattr(catalog, "mission_tags", unavailable)
    refused("quest_locked", 409, lambda: POLICY.check_start(context(catalog, FIRST_QUEST, {})))
    refused("quest_closed", 409, lambda: POLICY.check_start(context(catalog, ENDED_MISSION_QUEST, cleared(GATE_QUEST))))
    with pytest.raises(ProtocolError) as error:
        POLICY.check_start(context(catalog, FIRST_QUEST, cleared(GATE_QUEST)))
    assert error.value is failure


def test_first_three_star_raid_clear_grants_its_unit_once_without_gold_events(raid):
    service, store = raid
    quest = service.catalog.get(FIRST_QUEST)
    owned = card_ids(store).count(PLATINUM_ARMOR)
    response = service.start(bare_start(service, store, FIRST_QUEST), store)
    assert not nodes(response)["LOTTERY"].children[0].values
    active = store.profile().state["quest_state"]["active"]
    assert (active["mission_id"], active["registration"], active["selected_mask"]) == (MENU_MISSION, 81, 0)
    assert (active["mode"]["quest_type"], active["mode"]["difficulty"], active["mode"]["boost_ignored"]) == (
        RAID_TYPE, "normal", None)
    assert active["mode"]["star_rewards"] == [ARMOR_STAR_REWARD]
    assert active["unit_reservations"] == [{"card_id": PLATINUM_ARMOR, "level": quest.unit_level, "count": 1}]
    finishing = finish_request(store, FIRST_QUEST, gold=2, extra=gold_events(2))
    response = service.finish(finishing, store)
    completed = store.profile()
    assert service.finish(finishing, store) == response and store.profile() == completed
    tree = nodes(response)
    assert tree["REWARD"].children[0].values == (quest.rank_exp,)
    assert tree["REWARD"].children[1].values == (three_star_gold(quest.gold),)
    assert new_cards(tree) == (PLATINUM_ARMOR,) and card_ids(store).count(PLATINUM_ARMOR) == owned + 1
    assert not tree["TreasureInfo"].children[0].values and not {"PFC", "POSSESSION", "PPT"} & set(tree)
    row = record(store, FIRST_QUEST)
    assert (row["MaxRank"], row["AchieveFlag"], row["FirstDefeatDate"]) == (3, CLEARED, business_day(NOW))
    history = completed.state["quest_state"]["history"][-1]
    assert (history["gold_events"], history["mission_id"], history["star_rewards"]) == (0, MENU_MISSION, [ARMOR_STAR_REWARD])
    service.start(bare_start(service, store, FIRST_QUEST, tick=1002), store)
    assert store.profile().state["quest_state"]["active"]["unit_reservations"] == []
    assert not new_cards(nodes(service.finish(finish_request(store, FIRST_QUEST, tick=1003), store)))
    assert card_ids(store).count(PLATINUM_ARMOR) == owned + 1 and record(store, FIRST_QUEST)["AchieveFlag"] == CLEARED


@pytest.mark.parametrize("number", [78, 82])
def test_raid_quests_have_no_four_star_challenge(raid, number):
    service, store = raid
    put_record(store, FIRST_QUEST, flags=CLEARED)
    refused_start("quest_hard_unavailable", 409, service, store, hard_start(service, store, FIRST_QUEST, number=number))


def test_residual_boost_is_ignored_and_the_entry_cost_is_charged_once(raid):
    service, store = raid
    starting = hard_start(service, store, FIRST_QUEST, number=81, boost=3)
    response = service.start(starting, store)
    assert service.start(starting, store) == response
    assert charisma(store) == CHARISMA
    assert store.profile().state["quest_state"]["active"]["mode"]["boost_ignored"] == 3
    assert new_cards(nodes(service.finish(finish_request(store, FIRST_QUEST), store))) == (PLATINUM_ARMOR,)
    assert charisma(store) == CHARISMA - service.catalog.get(FIRST_QUEST).charisma
    assert record(store, FIRST_QUEST)["MaxRank"] == 3


def test_raid_results_list_every_raid_group_and_missions_count_only_saved_units(raid, bundle, caplog):
    service, store = raid
    grant = {"card_id": GROUP_CARD, "level": 1, "count": RAID_GROUPS * UNITS_PER_GROUP}
    mutate(store, lambda state: bundle.player_state.credit_units(state, [grant])[0])
    prince, *units = store.profile().state["player_state"]["units"]
    assert prince["CardID"] in (1, 309) and len(units) == grant["count"]
    recorder = Recorder()
    service.missions = recorder
    service.start(bare_start(service, store, FIRST_QUEST), store)
    groups = [units[index:index + UNITS_PER_GROUP] for index in range(0, len(units), UNITS_PER_GROUP)]
    groups[0] = [prince, *groups[0]]
    # Formation slots restart in every group, and one row names a unit missing from the saved roster.
    rows = [(unit["CardID"], unit["GetIndex"], slot, unit["UniqueID"], 0) for group in groups for slot, unit in enumerate(group)]
    rows.append((units[0]["CardID"], ABSENT_INDEX, len(groups[-1]), ABSENT_INDEX, 0))
    with caplog.at_level("WARNING", logger="aigis_ps"):
        service.finish(finish_request(store, FIRST_QUEST, extra=formation(*rows)), store)
    assert [entry.getMessage() for entry in caplog.records] == [
        f"Quest {FIRST_QUEST} result lists 1 formation rows absent from the saved roster"]
    [(kind, payload)] = recorder.events
    assert (kind, payload["quest_type"], payload["mission_id"], payload["score_mode"]) == (
        "quest_clear", RAID_TYPE, MENU_MISSION, False)
    assert payload["team"] == [{"card_id": unit["CardID"], "get_index": unit["GetIndex"], "unique_id": unit["UniqueID"]}
                               for unit in units]


@pytest.mark.parametrize("platform", INVALID_PLATFORMS)
def test_raid_admission_refuses_platform_codes_outside_the_native_range(bundle, platform):
    catalog = bundle.quests.catalog
    records = cleared(GATE_QUEST)
    assert POLICY.check_start(context(catalog, FIRST_QUEST, records)).mission_id == MENU_MISSION
    refused("quest_platform_invalid", 400, lambda: POLICY.check_start(context(catalog, FIRST_QUEST, records, platform=platform)))
    refused("quest_platform_invalid", 400, lambda: POLICY.menu_reached(catalog, rank=RAID_RANK, platform_code=platform))


@pytest.mark.parametrize("platform", [PLATFORM_CODES.start - 1, PLATFORM_CODES.stop])
def test_raid_start_requests_on_a_platform_outside_the_native_range_change_nothing(raid, platform):
    service, store = raid
    refused_start("quest_platform_invalid", 400, service, store,
                  on_platform(bare_start(service, store, FIRST_QUEST), platform))
    service.start(on_platform(bare_start(service, store, FIRST_QUEST), PLATFORM), store)
    assert store.profile().state["quest_state"]["active"]["mission_id"] == MENU_MISSION


@pytest.mark.parametrize("field,value", [
    ("Type", 9), ("Type", 11.0), ("Type", True), ("Treasure1", 67), ("Treasure3", None), ("Treasure5", -1),
    ("_HardLevel", 200), ("BonusType", 0), ("BonusType", 4), ("UnitList", 1), ("UnitList", False),
])
def test_rows_outside_the_raid_contract_are_refused(bundle, field, value):
    catalog = bundle.quests.catalog
    with pytest.raises(ProtocolError) as error:
        POLICY.validate_row(MappingProxyType({**catalog.row(FIRST_QUEST), field: value}), catalog)
    assert (error.value.code, error.value.status) == ("quest_definition_invalid", 503)
    assert field in str(error.value)


def test_rows_missing_a_contract_column_are_refused_and_every_native_row_passes(bundle):
    catalog = bundle.quests.catalog
    for field in ("Treasure5", "_HardLevel", "BonusType", "UnitList"):
        row = MappingProxyType({name: value for name, value in catalog.row(FIRST_QUEST).items() if name != field})
        refused("quest_definition_invalid", 503, lambda row=row: POLICY.validate_row(row, catalog))
    assert all(POLICY.validate_row(catalog.row(quest_id), catalog) is None
               for quest_id in catalog.playable_quests(RAID_TYPE))


def test_raid_team_quests_stay_refused_by_the_catalog(bundle):
    base = bundle.quests.catalog
    catalog = QuestCatalog(base.snapshot, base._asset_reader, possessions=bundle.possessions,
                           resource_version=base.resource_version)
    catalog._all_rows[FIRST_QUEST] = {**catalog._all_rows[FIRST_QUEST], "IsUseRaidTeam": 1}
    refused("quest_condition_unsupported", 501, lambda: catalog.get(FIRST_QUEST))
    assert catalog.status()["mode_failed_quests"][FIRST_QUEST]["code"] == "quest_condition_unsupported"
