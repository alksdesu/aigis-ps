from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps import entrust_catalog, quest_modes
from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.bootstrap import MAX_QUEST_RECORDS
from aigis_ps.energy_clock import JST
from aigis_ps.entrust_catalog import (
    AUDITED_TYPES, CONDITION_RESOURCE, LEGACY_PARITY_HOOKS, MENU_CLOSED, NO_MISSION, REQUIRED_MAX_RANK,
    REWARD_UNSUPPORTED, STATUS_LIMIT, Blocker, CatalogCheck, EntrustCatalog, ScopeState,
)
from aigis_ps.errors import ProtocolError
from aigis_ps.growth_campaign import CAMPAIGN_RESOURCE, GrowthCampaign
from aigis_ps.history_disclosure import DISCLOSURE_TABLE, ENTRY_FIELDS as DISCLOSURE_FIELDS
from aigis_ps.masterdata import MasterDataStore, MasterSnapshot
from aigis_ps.mission_tags import TAG_LOOKUP, mission_tables
from aigis_ps.native_commands import MENU_SCOPE_CHAINS, REGISTERED_TABLES, CommandForm, MenuRoute
from aigis_ps.possessions import PossessionService
from aigis_ps.quest_catalog import EVENT_PARAM_DROP_TYPE, GIFT_DROP_TYPE, QuestCatalog, Reward
from aigis_ps.quest_mode_daily import DailyPolicy
from aigis_ps.quest_mode_reproduce import ReproducePolicy
from aigis_ps.quest_modes import (
    CLEAR_FLAG, CONDITION_FIELDS, ENTRUST_PARITY_HOOKS, SLOT_FLAGS, ModePolicy, SettleOutcome, StartContext, admit,
    policy,
)
from aigis_ps.resource_library import ResourceLibrary
from test_growth_campaign import entry as campaign_entry
from test_history_disclosure import entry_row as disclosure_row
from test_native_commands import INT_KIND, commands
from test_quest_catalog import (
    AT_LEAST_THREE, DROP_FIELDS, ENTRY_FIELDS, ITEM_HASH, ITEM_NAME, MISSION_LIST_FIELDS, OVERRIDE_CODE, PACKED_RANK,
    SPECIAL_ENEMY, STAGE_VARIANT_ENEMY, TAGGED_MISSION_FIELDS, TEXT_KIND, archive, drop_rule, entry, mission_config,
    quest, setup, snapshot, table, with_missions,
)
from test_quest_modes import config, disable_policy, windows


PROJECT = Path(__file__).resolve().parents[1]
CONDITION_COLUMNS = tuple((name, 1) for name in CONDITION_FIELDS)
CAMPAIGN_FIELDS = (("MissionID", INT_KIND), ("DateOpen", TEXT_KIND), ("DateClose", TEXT_KIND), ("ItemID", TEXT_KIND),
                   ("ID", INT_KIND), ("QuestID", INT_KIND), ("Count", INT_KIND), ("Ratio", INT_KIND))
STORY_FIELDS = TAGGED_MISSION_FIELDS + (("KeyQuestID", INT_KIND),)
STORY_CONFIG, STORY_LIST = mission_tables(0)
QUEST_COMMON, SYSTEM_COMMANDS = "OnCommandMenuQuestCommon.atb", "OnSystemCommand.atb"
GENERAL_ROUTE = 'SetUpdaterFunc( "Func_GeneralQuestMenu" );'
HISTORY_ROUTE = 'SetUpdaterFunc( "Func_HistoryQuestMenu" );'
TOWER_ROUTE = 'SetUpdaterFunc( "Func_TowerTopMenu" );'
OPEN_TAG = 'IsOpenMission( "Tag" , $Tag );'
EVENT_CODE = 3001
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
PAST_WINDOW = (datetime(2026, 1, 1, tzinfo=JST), datetime(2026, 1, 2, tzinfo=JST))
OPEN_CAMPAIGN = {"opens": "2026/11/1 11:00:00", "closes": "2026/11/8 11:00:00"}
STORY = SimpleNamespace(quest_type=0, hard=None)
ENTRUST_RANK = 99
PLATFORM, OTHER_PLATFORM, UNMASKED_PLATFORM = 1, 2, 20
# AppConfigDmm::init (A9D020) fixes the PlatformCode of the local PC client at 9.
CLIENT_PLATFORM = 9
STORY_MISSION, TUTORIAL_MISSION, TRAINING_MISSION, HISTORY_MISSION = 100001, 900001, 910001, 920001
DAILY_MISSIONS, EMPIRE_MISSION, EMERGENCY_MISSION, SECOND_HISTORY_MISSION = (700101, 700102), 940001, 200384, 920014
EMPIRE_MENU_MISSION, REPRODUCE_TAG_MISSION = 940012, 300001
DAILY_TYPE, HISTORY_TYPE, EMPIRE_TYPE, TUTORIAL_TYPE, REPRODUCE_TYPE = 2, 16, 18, 4, 8
UNSPECIFIED_TYPE = 300
STORY_QUEST, DAILY_QUEST, HISTORY_QUEST, EMPIRE_QUEST, TUTORIAL_QUEST = 1001, 1002, 1003, 1004, 1005
STORY_QUESTS = (STORY_QUEST, 1003, 1006)
HISTORY_MENU_TAG, DAILY_TAG, EMPIRE_TAG, EMPIRE_MENU_TAG = 302, 130, 902, 801
# The empire mode gates on quest 1004, which is the fixture's empire quest itself.
GATE_RECORDS = MappingProxyType({EMPIRE_QUEST: MappingProxyType({"MaxRank": 1, "AchieveFlag": CLEAR_FLAG})})
UNAUDITED_GATE_TYPES = range(100, 100 + STATUS_LIMIT + 8)
STORY_MENU = (("CmnMenu::DoReturn_Type_0", GENERAL_ROUTE), ("FCMenuQuestTypeList::IsEntrust_0", "1;"))
MODE_MENU = (*STORY_MENU, ("CmnMenu::DoReturn_Type_1", GENERAL_ROUTE), ("FCMenuQuestTypeList::IsEntrust_1", "1;"),
             ("CmnMenu::DoReturn_Type_5", GENERAL_ROUTE), ("FCMenuQuestTypeList::IsEntrust_5", "1;"),
             ("CmnMenu::DoReturn_Type_14", HISTORY_ROUTE), ("FCMenuQuestTypeList::IsEntrust_14", OPEN_TAG),
             ("CmnMenu::DoReturn_Type_16", GENERAL_ROUTE), ("FCMenuQuestTypeList::IsEntrust_16", OPEN_TAG))
TYPE_GATES = (("HasEntrustByType_2", "1;"), ("HasEntrustByType_16", "1;"), ("HasEntrustByType_18", "1;"))
# HasAnyDisplayableQuest opens the Tag 801 empire menu only while one of its members is open today.
EMPIRE_MENU = (mission_config(EMPIRE_MENU_MISSION, tag=EMPIRE_MENU_TAG, span=10), (STORY_QUEST,))
MODE_MISSIONS = MappingProxyType({
    0: ((mission_config(STORY_MISSION), (STORY_QUEST,)),),
    1: ((mission_config(EMERGENCY_MISSION, enable=0, tag=EMPIRE_MENU_TAG), (EMPIRE_QUEST,)),),
    5: tuple((mission_config(mission_id, tag=DAILY_TAG, span=1), (DAILY_QUEST,)) for mission_id in DAILY_MISSIONS),
    7: ((mission_config(TUTORIAL_MISSION, tag=HISTORY_MENU_TAG), (TUTORIAL_QUEST,)),),
    13: ((mission_config(TRAINING_MISSION, tag=HISTORY_MENU_TAG), (TUTORIAL_QUEST,)),),
    14: ((mission_config(HISTORY_MISSION, tag=HISTORY_MENU_TAG), (HISTORY_QUEST,)),),
    16: ((mission_config(EMPIRE_MISSION, tag=EMPIRE_TAG), (EMPIRE_QUEST,)), EMPIRE_MENU),
})
SPLIT_HISTORY = MappingProxyType({**MODE_MISSIONS, 14: (
    (mission_config(HISTORY_MISSION, tag=HISTORY_MENU_TAG), (HISTORY_QUEST,)),
    (mission_config(SECOND_HISTORY_MISSION, tag=DAILY_TAG), (HISTORY_QUEST,)))})
DISCLOSURE_COLUMNS = tuple((name, INT_KIND) for name in DISCLOSURE_FIELDS)
DISCLOSURE_GROUP, FIRST_DAY = 1, 0
# History builds read CfgMissionHistoryTable; both missions open on the first day without a prerequisite.
DISCLOSURES = tuple(disclosure_row(mission_id, DISCLOSURE_GROUP, FIRST_DAY)
                    for mission_id in (HISTORY_MISSION, SECOND_HISTORY_MISSION))
PREFIXED_MAPS = ("Map920001_0007.aar", "Map940001_0007.aar")
# 2026-11-01 21:00 JST is a Sunday; this _HoldingParam bit opens the quest on Mondays only.
MONDAY_ONLY = 0b10
# Unit card 1 rewards the slot 3 stage variant, which a record shows only below three stars.
VARIANT_CODE = 1
RANKED_PRIZES = (entry(1, enemy=SPECIAL_ENEMY, condition=AT_LEAST_THREE, drop=OVERRIDE_CODE), entry(2),
                 entry(3, enemy=STAGE_VARIANT_ENEMY))


def main(**changes):
    row = quest(DisableEntrust=0, ConsignmentTicketConsumptionCount=0, ConsignmentTicketID="null", _HardLevel=0,
                BonusType=0, BonusNum=0, NormalMode_RewardItemID="null", _HoldingParam=0)
    row.update(changes)
    return row


def mode_rows(**daily):
    ticketed = {"ConsignmentTicketID": ITEM_NAME, "ConsignmentTicketConsumptionCount": 1}
    return [main(), main(QuestID=DAILY_QUEST, Type=DAILY_TYPE, UnitList=0, **ticketed, **daily),
            main(QuestID=HISTORY_QUEST, Type=HISTORY_TYPE, **ticketed), main(QuestID=EMPIRE_QUEST, Type=EMPIRE_TYPE, **ticketed),
            main(QuestID=TUTORIAL_QUEST, Type=TUTORIAL_TYPE)]


def story_missions(resources, rows, unlisted):
    resources[STORY_CONFIG] = table(STORY_FIELDS, [{**mission_config(STORY_MISSION), "KeyQuestID": 0}])
    resources[STORY_LIST] = table(MISSION_LIST_FIELDS, [{"MissionID": STORY_MISSION, "QuestID": row["QuestID"]}
                                                        for row in rows if row["Type"] == 0 and row["QuestID"] not in unlisted])


def questbook(setup, rows=None, *, missing=(), column_types=None, conditions=(), menu=STORY_MENU, gates=(), campaign=(),
              missions=None, unlisted=(), overrides=None):
    rows = [main()] if rows is None else rows
    cards = snapshot().get_xml("CardData")
    types = {name: "S" if isinstance(value, str) else "I" for name, value in rows[0].items()}
    types.update(column_types or {})
    data = dumps(Container(children=tuple(Column(name, types[name], tuple(row[name] for row in rows))
        for name in rows[0] if name not in missing)))
    master = MasterSnapshot("entrust-fixture", "a" * 64, {"CardData": cards, "QuestTable": data})
    resources, possessions = setup
    resources[CONDITION_RESOURCE] = table(CONDITION_COLUMNS, conditions)
    resources[QUEST_COMMON] = commands(*menu)
    resources[CAMPAIGN_RESOURCE] = table(CAMPAIGN_FIELDS, list(campaign))
    if gates:
        resources[SYSTEM_COMMANDS] = commands(*gates)
    if missions is None:
        story_missions(resources, rows, unlisted)
    else:
        with_missions(setup, missions)
        resources.update(dict.fromkeys(PREFIXED_MAPS, resources["Map0007.aar"]))
        resources[DISCLOSURE_TABLE] = table(DISCLOSURE_COLUMNS, list(DISCLOSURES))
    return QuestCatalog(master, resources.__getitem__, possessions=possessions, resource_version="entrust-v1",
                        window_overrides=overrides)


def build(setup, rows=None, **options):
    return EntrustCatalog(questbook(setup, rows, **options))


def modes(setup, rows=None, *, menu=MODE_MENU, gates=TYPE_GATES, missions=MODE_MISSIONS, **options):
    return EntrustCatalog(questbook(setup, mode_rows() if rows is None else rows, menu=menu, gates=gates,
                                    missions=missions, **options))


def fail(code, callback, status=None):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert caught.value.code == code
    if status is not None:
        assert caught.value.status == status
    return caught.value


def saved(product, *, rank=3, flags=None):
    flags = sum(bit for code, bit in zip(product.quest.treasures, SLOT_FLAGS) if code) if flags is None else flags
    return {"bootstrap_schema": 1, "tables": {"quest_records": [{"QuestID": product.quest_id,
            "MaxRank": rank, "AchieveFlag": flags, "Shown": False}]}}


def battle_start_allows(quests, definition, records):
    open_story = windows(config(1), members=((1, definition.quest_id),), ranks={definition.quest_id: definition.rank})
    catalog = SimpleNamespace(mission_windows=lambda quest_type: open_story, special_conditions=quests.special_conditions)
    try:
        policy(0).check_start(StartContext(catalog, definition, STORY, records, definition.rank, NOW, False, 1))
    except ProtocolError as error:
        assert error.code == "quest_locked"
        return False
    return True


def entrust_allows(subject, state, product):
    try:
        subject.require_eligible(state, product)
    except ProtocolError as error:
        assert error.code == "entrust_special_condition"
        return False
    return True


def extra_drops(setup, *rules):
    setup[0]["ItemDropConfigTable.atb"] = table(DROP_FIELDS, [drop_rule(), drop_rule(1000000, 2000000, 2),
        drop_rule(1003, 0, GIFT_DROP_TYPE, ITEM_HASH, 3), *rules])


def event_drops(setup):
    extra_drops(setup, drop_rule(EVENT_CODE, 0, EVENT_PARAM_DROP_TYPE))


def ranked_prizes(setup):
    extra_drops(setup, drop_rule(OVERRIDE_CODE, 0, GIFT_DROP_TYPE, ITEM_HASH, 2))
    setup[0]["Map0007.aar"] = archive([("Entry02.atb", table(ENTRY_FIELDS, list(RANKED_PRIZES)))])


def mission(subject, quest_type, mission_id):
    return subject.quests.mission_windows(quest_type).mission(mission_id)


def playable(subject, quest_type, quest_id, product, *, platform=PLATFORM):
    return subject.quests.mission_windows(quest_type).require_playable(
        quest_id, now=NOW, rank=ENTRUST_RANK, records={}, platform_code=platform,
        selector=subject.selector(product, now=NOW, platform_code=platform))


def admission(subject, quest_id, records):
    product = subject.product(quest_id)
    context = StartContext(subject.quests, product.quest, product.mode, records, ENTRUST_RANK, NOW, False, PLATFORM,
                           selector=subject.selector(product, now=NOW, platform_code=PLATFORM))
    return admit(policy(product.quest_type), context)


def test_the_default_questbook_offers_story_entrust_through_its_menu_gate(setup):
    subject = build(setup)
    product = subject.product(STORY_QUEST)
    assert product.quest is subject.quests.get(STORY_QUEST) and product.mode is subject.quests.mode(STORY_QUEST)
    assert (product.quest_type, product.entry_label, product.mode.mission_ids) == (0, "Map0007.aar/Entry02.atb", (STORY_MISSION,))
    selector = subject.selector(product, now=NOW, platform_code=PLATFORM)
    story = mission(subject, 0, STORY_MISSION)
    assert selector.accepts(story) and selector.choose is None
    assert (selector.code, selector.message) == (MENU_CLOSED.code, MENU_CLOSED.message)
    assert playable(subject, 0, STORY_QUEST, product) is story
    status = subject.status()
    assert status["ready"] and status["offered_types"] == [0] and status["audited_types"] == sorted(AUDITED_TYPES)
    assert {quest_type: entry["state"] for quest_type, entry in status["types"].items()} == {
        0: "offered", 2: "refused", 8: "refused", 16: "refused", 18: "refused"}
    gate = status["types"][0]["categories"][0]
    assert (gate["route"]["func"], gate["scope"], gate["menu_gate"]["form"]) == (
        "Func_GeneralQuestMenu", "FCMenuQuestTypeList", CommandForm.LITERAL_TRUE)
    assert status["mission_tags"] == {"available": False, "error": "quest_asset_unavailable", "content_version": None}
    assert status["commands"]["missing_tables"][SYSTEM_COMMANDS] == "quest_asset_unavailable"


def test_products_keep_the_exact_quest_objects_and_normalize_nonconsuming_nulls(setup):
    subject = build(setup, [main(), main(QuestID=1003, ConsignmentTicketID="")])
    assert subject.validate((0,)) == {0: {"products": 2, "excluded": {}}}
    for quest_id in (STORY_QUEST, 1003):
        product = subject.product(quest_id)
        assert product.quest is subject.quests.get(quest_id) and product.mode is subject.quests.mode(quest_id)
        assert (product.ticket_item_id, product.ticket_count, product.disabled) == (None, 0, False)
    assert subject.product(STORY_QUEST) is subject.product(STORY_QUEST)
    assert not hasattr(subject.product(STORY_QUEST).quest, "ticket_count")
    status = subject.status()["types"][0]
    assert (status["indexed"], status["validated"], status["all_validated"]) == (2, 2, True)
    assert {"QuestTable", "CardData", "ItemDropConfigTable.atb", "ItemConfigTable.atb", CONDITION_RESOURCE,
            CAMPAIGN_RESOURCE, STORY_CONFIG, STORY_LIST} <= subject.source_hashes.keys()


def test_known_positive_ticket_metadata_is_retained_without_any_debit(setup):
    subject = build(setup, [main(ConsignmentTicketID=ITEM_NAME, ConsignmentTicketConsumptionCount=2)])
    product = subject.product(STORY_QUEST)
    assert product.ticket_item_id == ITEM_NAME and product.ticket_count == 2
    state = saved(product)
    before = deepcopy(state)
    subject.require_eligible(state, product)
    assert state == before
    assert subject.status()["types"][0]["ticket"] == {"items": {ITEM_NAME: 1}, "per_run": {2: 1}}


@pytest.mark.parametrize("changes", [
    {"ConsignmentTicketConsumptionCount": -1}, {"DisableEntrust": -1},
    {"ConsignmentTicketID": "null", "ConsignmentTicketConsumptionCount": 1},
    {"ConsignmentTicketID": "", "ConsignmentTicketConsumptionCount": 1},
    {"ConsignmentTicketID": "unknown"}, {"ConsignmentTicketID": "null "},
    {"ConsignmentTicketID": "x" * 257},
])
def test_invalid_ticket_identity_or_count_excludes_only_that_quest(setup, changes):
    subject = build(setup, [main(**changes), main(QuestID=1003)])
    fail("entrust_source_invalid", lambda: subject.product(STORY_QUEST), 503)
    assert subject.product(1003).quest_id == 1003
    assert subject.status()["types"][0]["excluded"] == {"entrust_source_invalid": {"count": 1, "quests": [STORY_QUEST]}}


@pytest.mark.parametrize("field", ["DisableEntrust", "ConsignmentTicketConsumptionCount", "ConsignmentTicketID"])
def test_missing_entrust_fields_are_not_assumed_free_or_enabled(setup, field):
    fail("entrust_catalog_invalid", lambda: build(setup, missing=(field,)), 503)


def test_ticket_field_wrong_type_is_rejected(setup):
    row = main()
    row["ConsignmentTicketID"] = 0
    fail("entrust_catalog_invalid", lambda: build(setup, [row], column_types={"ConsignmentTicketID": "I"}))


def test_any_positive_disabled_flag_is_preserved_and_rejected(setup):
    subject = build(setup, [main(DisableEntrust=2)])
    product = subject.product(STORY_QUEST)
    assert product.disabled and subject.status()["types"][0]["disabled"] == {"count": 1, "quests": [STORY_QUEST]}
    fail("entrust_disabled", lambda: subject.require_eligible(saved(product), product))


def test_three_star_rank_and_all_nonempty_treasures_are_independent_conditions(setup):
    subject = build(setup)
    product = subject.product(STORY_QUEST)
    full = saved(product)
    subject.require_eligible(full, product)
    assert not full["tables"]["quest_records"][0]["AchieveFlag"] & 0x100000
    fail("entrust_not_eligible", lambda: subject.require_eligible(saved(product, rank=2), product))
    fail("entrust_not_eligible", lambda: subject.require_eligible(saved(product, flags=0x100000), product))
    for code, bit in zip(product.quest.treasures, SLOT_FLAGS):
        if code:
            altered = deepcopy(full)
            altered["tables"]["quest_records"][0]["AchieveFlag"] &= ~bit
            fail("entrust_not_eligible", lambda: subject.require_eligible(altered, product))
    unknown_bits = deepcopy(full)
    unknown_bits["tables"]["quest_records"][0]["AchieveFlag"] -= 0x80000000
    subject.require_eligible(unknown_bits, product)


def test_empty_treasure_slots_do_not_create_collection_requirements(setup):
    subject = build(setup)
    product = subject.product(STORY_QUEST)
    record = saved(product)
    assert product.quest.treasures[2:4] == (0, 0)
    assert record["tables"]["quest_records"][0]["AchieveFlag"] & (SLOT_FLAGS[2] | SLOT_FLAGS[3]) == 0
    subject.require_eligible(record, product)


def test_each_of_five_treasure_flags_is_required_independently(setup):
    subject = build(setup, [main(**{f"Treasure{index}": 4 for index in range(1, 6)})])
    product = subject.product(STORY_QUEST)
    for flag in SLOT_FLAGS:
        state = saved(product, flags=sum(SLOT_FLAGS) ^ flag)
        fail("entrust_not_eligible", lambda: subject.require_eligible(state, product))
    subject.require_eligible(saved(product), product)


@pytest.mark.parametrize("damage", ["missing", "schema"])
def test_missing_or_wrong_condition_schema_cannot_create_unrestricted_catalog(setup, damage):
    quests = questbook(setup)
    if damage == "missing":
        setup[0].pop(CONDITION_RESOURCE)
    else:
        setup[0][CONDITION_RESOURCE] = table(CONDITION_COLUMNS[:-1], [])
    fail("entrust_catalog_invalid", lambda: EntrustCatalog(quests))


def test_source_and_content_versions_include_special_condition_rules(setup):
    before = build(setup)
    changed = build(setup, conditions=[dict(zip(CONDITION_FIELDS, (STORY_QUEST, 1, 1, 2001, 2001)))])
    assert before.content_version != changed.content_version
    assert before.source_hashes[CONDITION_RESOURCE] != changed.source_hashes[CONDITION_RESOURCE]


def test_conditions_are_the_quest_catalog_rows_and_digest_read_once(setup):
    quests = questbook(setup, conditions=[dict(zip(CONDITION_FIELDS, (STORY_QUEST, 1, 1, 2001, 2001)))])
    reader, reads = quests._asset_reader, []
    quests._asset_reader = lambda name: reads.append(name) or reader(name)
    conditions = quests.special_conditions()
    subject = EntrustCatalog(quests)
    assert reads.count(CONDITION_RESOURCE) == 1 and subject._conditions is conditions
    assert subject.source_hashes[CONDITION_RESOURCE] == quests.status()["mode_resource_sha256"][CONDITION_RESOURCE]


@pytest.mark.parametrize("kind,threshold,records", [
    (1, 4, [(2, 0), (2, 0)]), (2, 2, [(0, 0x1000000), (0, 0x1000000)]),
    (3, 0, [(3, 0), (3, 0)])])
def test_special_conditions_require_range_star_sum_or_clear_flags(setup, kind, threshold, records):
    condition = dict(zip(CONDITION_FIELDS, (STORY_QUEST, kind, threshold, 2001, 2002)))
    subject = build(setup, conditions=[condition])
    product = subject.product(STORY_QUEST)
    state = saved(product)
    fail("entrust_special_condition", lambda: subject.require_eligible(state, product))
    state["tables"]["quest_records"] += [dict(QuestID=2001+i, MaxRank=rank, AchieveFlag=flags)
                                         for i, (rank, flags) in enumerate(records)]
    original = deepcopy(state)
    subject.require_eligible(state, product)
    assert state == original
    state["tables"]["quest_records"].pop()
    fail("entrust_special_condition", lambda: subject.require_eligible(state, product))


def test_multiple_special_conditions_are_anded_and_unrelated_rows_do_not_restrict_main(setup):
    conditions = [dict(zip(CONDITION_FIELDS, values)) for values in
                  [(STORY_QUEST, 1, 3, 2001, 2001), (STORY_QUEST, 2, 1, 2001, 2001), (9999, 3, 3, 3001, 3001)]]
    subject = build(setup, conditions=conditions)
    state = saved(subject.product(STORY_QUEST))
    state["tables"]["quest_records"].append(dict(QuestID=2001, MaxRank=3, AchieveFlag=0))
    fail("entrust_special_condition", lambda: subject.require_eligible(state, subject.product(STORY_QUEST)))
    state["tables"]["quest_records"][-1]["AchieveFlag"] = 0x1000000
    subject.require_eligible(state, subject.product(STORY_QUEST))


@pytest.mark.parametrize("condition,allowed", [
    ((4, 99, 2001, 2001), True), ((0, 99, 2001, 2001), True), ((4, 99, 2001, 2002), True),
    ((-1, 99, 2001, 2002), True), ((1, 1, 2002, 2001), False), ((3, 0, 2002, 2001), True),
    ((1, -1, 2001, 2001), True), ((1, 3, 2001, 2001), True), ((2, 1, 2001, 2001), False),
    ((3, 0, 2001, 2002), False)])
def test_one_condition_row_gives_battle_start_and_entrust_the_same_verdict(setup, condition, allowed):
    subject = build(setup, conditions=[dict(zip(CONDITION_FIELDS, (STORY_QUEST, *condition)))])
    product = subject.product(STORY_QUEST)
    state = saved(product)
    state["tables"]["quest_records"].append(dict(QuestID=2001, MaxRank=3, AchieveFlag=0))
    records = {row["QuestID"]: row for row in state["tables"]["quest_records"]}
    assert battle_start_allows(subject.quests, product.quest, records) is allowed
    assert entrust_allows(subject, state, product) is allowed


def test_event_point_drops_are_entrust_rewards_like_units_and_items(setup):
    event_drops(setup)
    subject = build(setup, [main(), main(QuestID=1003, Treasure2=EVENT_CODE), main(QuestID=1004, Treasure3=EVENT_CODE)])
    assert subject.quests.decode_reward(EVENT_CODE).kind == "event_param"
    events = subject.product(1003)
    assert events.reward_codes(REQUIRED_MAX_RANK) == events.all_reward_codes == (4, 4, EVENT_CODE)
    subject.require_eligible(saved(events), events)
    unrolled = subject.product(1004)
    assert EVENT_CODE in unrolled.quest.treasures and EVENT_CODE not in unrolled.all_reward_codes
    assert subject.validate((0,)) == {0: {"products": 3, "excluded": {}}}
    assert subject.status()["types"][0]["excluded"] == {}


def test_reward_kinds_without_a_verified_grant_keep_only_their_quest_out_of_entrust(setup, monkeypatch):
    event_drops(setup)
    quests = questbook(setup, [main(), main(QuestID=1003, Treasure2=EVENT_CODE)])
    decode = quests.decode_reward
    monkeypatch.setattr(quests, "decode_reward",
                        lambda code: replace(decode(code), kind="none") if code == EVENT_CODE else decode(code))
    subject = EntrustCatalog(quests)
    fail(REWARD_UNSUPPORTED.code, lambda: subject.product(1003), REWARD_UNSUPPORTED.status)
    original, asked = subject.quests.get, []
    monkeypatch.setattr(subject.quests, "get", lambda quest_id: asked.append(quest_id) or original(quest_id))
    fail(REWARD_UNSUPPORTED.code, lambda: subject.product(1003), REWARD_UNSUPPORTED.status)
    assert 1003 not in asked
    assert subject.validate((0,)) == {0: {"products": 1, "excluded": {REWARD_UNSUPPORTED.code: 1}}}
    assert subject.status()["types"][0]["excluded"] == {REWARD_UNSUPPORTED.code: {"count": 1, "quests": [1003]}}


def test_reward_codes_follow_the_record_max_rank_while_builds_decode_every_static_prize(setup):
    ranked_prizes(setup)
    subject = build(setup, [main(Treasure3=VARIANT_CODE)])
    product = subject.product(STORY_QUEST)
    assert product.reward_codes(0) == product.reward_codes(REQUIRED_MAX_RANK - 1) == (1003, VARIANT_CODE)
    assert product.reward_codes(REQUIRED_MAX_RANK) == product.reward_codes(PACKED_RANK) == (OVERRIDE_CODE, 1003)
    assert product.all_reward_codes == (OVERRIDE_CODE, 1003, VARIANT_CODE)
    assert set(subject._rewards) == {OVERRIDE_CODE, 1003, VARIANT_CODE}
    assert subject.describe(STORY_QUEST)["product"] == {
        "built": True, "entry_label": "Map0007.aar/Entry02.atb", "reward_codes": sorted(product.all_reward_codes),
        "conditional": True, "candidates": len(RANKED_PRIZES)}
    fail("quest_state_invalid", lambda: product.reward_codes(-1), 503)
    subject.quests._rewards[OVERRIDE_CODE] = Reward("item", OVERRIDE_CODE, 9, item_id=ITEM_NAME)
    fail("entrust_catalog_changed", lambda: subject.product(STORY_QUEST), 503)


@pytest.mark.parametrize("records", [None, {}, [None], [{"QuestID": 1}],
    [{"QuestID": True, "MaxRank": 3, "AchieveFlag": 0}],
    [{"QuestID": 1, "MaxRank": True, "AchieveFlag": 0}],
    [{"QuestID": 1, "MaxRank": 3, "AchieveFlag": "1"}],
    [{"QuestID": 1, "MaxRank": 3, "AchieveFlag": 0}] * 2])
def test_bad_stored_records_fail_without_mutation(setup, records):
    subject = build(setup)
    state = {"bootstrap_schema": 1, "tables": {"quest_records": records}}
    original = deepcopy(state)
    fail("entrust_state_invalid", lambda: subject.require_eligible(state, subject.product(STORY_QUEST)))
    assert state == original



def test_stored_records_are_bounded_by_the_persisted_quest_table_limit(setup):
    subject = build(setup)
    product = subject.product(STORY_QUEST)
    state = saved(product)
    state["tables"]["quest_records"] += [{"QuestID": quest_id, "MaxRank": 0, "AchieveFlag": 0}
                                         for quest_id in range(STORY_QUEST + 1, STORY_QUEST + MAX_QUEST_RECORDS)]
    assert len(state["tables"]["quest_records"]) == MAX_QUEST_RECORDS
    subject.require_eligible(state, product)
    state["tables"]["quest_records"].append({"QuestID": 1, "MaxRank": 0, "AchieveFlag": 0})
    fail("entrust_state_invalid", lambda: subject.require_eligible(state, product), 503)


def test_unknown_quests_are_absent_and_unaudited_types_are_not_offered(setup):
    subject = build(setup, [main(), main(QuestID=1002, Type=DAILY_TYPE)])
    fail("entrust_not_eligible", lambda: subject.require_eligible(
        {"bootstrap_schema": 1, "tables": {"quest_records": []}}, subject.product(STORY_QUEST)))
    error = fail("entrust_not_offered", lambda: subject.product(1002), 409)
    assert str(error) == "Entrust does not offer this quest type: 2"
    for identifier in (True, "1001", 1001.0, None, 0, 0x7FFFFFFF):
        fail("quest_not_found", lambda: subject.product(identifier), 404)


def test_frozen_products_diagnostics_and_product_identity(setup):
    subject = build(setup)
    product = subject.product(STORY_QUEST)
    with pytest.raises(FrozenInstanceError):
        product.disabled = True
    with pytest.raises(TypeError):
        subject.excluded[STORY_QUEST] = None
    subject.status()["sources"]["resources"].clear()
    assert subject.source_hashes
    copy = replace(product)
    story = mission(subject, 0, STORY_MISSION)
    for call in (lambda: subject.require_eligible(saved(product), copy), lambda: subject.ticket_free(copy, story, NOW),
                 lambda: subject.ticket_cost(copy, story, 1, NOW),
                 lambda: subject.selector(copy, now=NOW, platform_code=PLATFORM)):
        fail("entrust_product_invalid", call, 503)


@pytest.mark.parametrize("attribute", EntrustCatalog._snapshot_names())
def test_replacing_frozen_catalog_components_is_rejected(setup, attribute):
    subject = build(setup)
    setattr(subject, attribute, object())
    fail("entrust_catalog_changed", lambda: subject.product(STORY_QUEST))
    fail("entrust_catalog_changed", subject.check)


def test_one_readiness_check_serves_every_lookup_of_a_request(setup, monkeypatch):
    subject = build(setup)
    checks, original = [], subject._ready
    monkeypatch.setattr(subject, "_ready", lambda: checks.append(1) or original())
    check = subject.check()
    product = subject.product(STORY_QUEST, check=check)
    subject.require_eligible(saved(product), product, check=check)
    fail("entrust_not_eligible", lambda: subject.require_eligible(saved(product, rank=2), product, check=check), 409)
    fail("quest_not_found", lambda: subject.product(9999, check=check), 404)
    assert len(checks) == 1
    assert subject.product(STORY_QUEST) is product and len(checks) == 2
    for foreign in (build(setup).check(), CatalogCheck(subject, object()), SimpleNamespace(catalog=subject), True):
        fail("entrust_check_invalid", lambda: subject.product(STORY_QUEST, check=foreign), 503)
        fail("entrust_check_invalid", lambda: subject.require_eligible(saved(product), product, check=foreign), 503)
    assert len(checks) == 2


@pytest.mark.parametrize("damage", ["version", "definition", "mode", "entry", "rewards", "conditions", "item_stack",
                                    "windows", "policy", "policy_settings", "policy_hooks"])
def test_shared_quest_dependencies_cannot_drift_silently(setup, monkeypatch, damage):
    subject = build(setup)
    product = subject.product(STORY_QUEST)
    quests = subject.quests
    if damage == "version":
        quests.resource_version = "changed"
    elif damage == "definition":
        quests._definitions[STORY_QUEST] = replace(product.quest, gold=0)
    elif damage == "mode":
        quests._modes[STORY_QUEST] = replace(product.mode, unit_list=0)
    elif damage == "entry":
        quests._entry_digests[product.entry_label] = "0" * 64
    elif damage == "rewards":
        quests._rewards[4] = Reward("unit", 4, 9, card_id=4)
    elif damage == "conditions":
        quests._special_conditions = MappingProxyType({})
    elif damage == "item_stack":
        subject.possessions._definitions[ITEM_NAME] = 1
    elif damage == "windows":
        quests._windows.clear()
    elif damage == "policy":
        monkeypatch.setattr(policy(0), "enabled", False)
    elif damage == "policy_settings":
        monkeypatch.setattr(policy(REPRODUCE_TYPE), "reason", "Reproduce settlement is under audit")
    else:
        monkeypatch.setattr(DailyPolicy, "settle", lambda self, context: SettleOutcome(context.state))
    fail("entrust_catalog_changed", lambda: subject.product(STORY_QUEST), 503)
    fail("entrust_catalog_changed", subject.check, 503)


def test_possession_readiness_never_builds_the_possession_status(setup, monkeypatch):
    quests, possessions = questbook(setup), setup[1]
    monkeypatch.setattr(possessions, "status", lambda: pytest.fail("Readiness must not build the possession status"))
    subject = EntrustCatalog(quests)
    product = subject.product(STORY_QUEST, check=subject.check())
    assert subject.product(STORY_QUEST) is product
    monkeypatch.setattr(possessions, "issues", {"item_definitions": "unavailable"})
    fail("entrust_catalog_changed", subject.check, 503)
    fail("entrust_catalog_invalid", lambda: EntrustCatalog(quests), 503)


def test_rebuilt_mission_tags_and_ticket_stacks_are_detected(setup):
    subject = modes(setup)
    subject.quests._tags = None
    fail("entrust_catalog_changed", lambda: subject.product(STORY_QUEST), 503)
    stocked = modes(setup)
    assert dict(stocked._ticket_stacks) == {ITEM_NAME: 999}
    stocked.possessions._definitions[ITEM_NAME] = 5
    fail("entrust_catalog_changed", lambda: stocked.product(STORY_QUEST), 503)


@pytest.mark.parametrize("gates,state,code", [
    ((), ScopeState.REFUSED, "entrust_not_offered"),
    ((("HasEntrustByType_2", "0;"),), ScopeState.REFUSED, "entrust_not_offered"),
    ((("HasEntrustByType_2", 'IsCleardQuest( 1003 );'),), ScopeState.REFUSED, "entrust_not_offered"),
    ((("HasEntrustByType_2", " 1 ;"),), ScopeState.OFFERED, None),
    ((("HasEntrustByType_2", "1;"), ("HasEntrustByType_2", "1;")), ScopeState.PENDING, "entrust_source_invalid"),
])
def test_daily_entrust_follows_its_native_type_gate(setup, gates, state, code):
    subject = modes(setup, gates=gates)
    scope = subject.scopes[DAILY_TYPE]
    assert scope.state is state and subject.scopes[0].state is ScopeState.OFFERED
    if code is None:
        assert subject.product(DAILY_QUEST).ticket_count == 1 and scope.gate.text == " 1 ;"
        return
    fail(code, lambda: subject.product(DAILY_QUEST))
    if state is ScopeState.PENDING:
        assert subject.status()["pending_types"][DAILY_TYPE]["code"] == code
        assert subject.status()["types"][DAILY_TYPE]["native_gate"]["error"]["occurrences"] == 2
        assert subject.status()["types"][DAILY_TYPE]["excluded"] == {code: {"count": 1, "quests": [DAILY_QUEST]}}


def test_native_gates_outside_the_audited_scope_are_reported_but_refused(setup):
    gates = (*TYPE_GATES, ("HasEntrustByType_3", "1;"), ("HasEntrustByType_4", "1;"), ("HasEntrustByType_17", "1;"),
             ("HasEntrustByType_200", "1;"), ("HasEntrustByType_0", "0;"), ("HasEntrustByType_0", "1;"))
    subject = modes(setup, gates=gates)
    status = subject.status()
    assert status["offered_types"] == [0, DAILY_TYPE, HISTORY_TYPE, EMPIRE_TYPE]
    assert status["unaudited_native_offers"] == [3, TUTORIAL_TYPE, 17, 200]
    assert status["refused_types"][TUTORIAL_TYPE] == {"quests": 1, "code": "entrust_not_offered", "native_gate": "literal_true"}
    assert status["refused_types"][200] == {"quests": 0, "code": "entrust_not_offered", "native_gate": "literal_true"}
    assert status["types"][0]["native_gate"]["error"]["occurrences"] == 2
    fail("entrust_not_offered", lambda: subject.product(TUTORIAL_QUEST), 409)
    assert subject.product(STORY_QUEST).quest_type == 0


def test_disabled_modes_keep_their_audited_type_pending(setup, monkeypatch):
    disable_policy(monkeypatch, REPRODUCE_TYPE)
    monkeypatch.setattr(ReproducePolicy, "settle", lambda self, context: SettleOutcome(context.state))
    rows = [*mode_rows(), main(QuestID=1006, Type=REPRODUCE_TYPE)]
    subject = modes(setup, rows, gates=(*TYPE_GATES, ("HasEntrustByType_8", "1;")))
    scope = subject.scopes[REPRODUCE_TYPE]
    assert (scope.enabled, scope.reason, scope.blocker is None) == (False, policy(REPRODUCE_TYPE).reason, False)
    assert (subject.scopes[0].enabled, subject.scopes[0].reason) == (True, None)
    error = fail("quest_mode_unsupported", lambda: subject.product(1006), 501)
    assert str(error) == f"Quest mode is not implemented: {policy(REPRODUCE_TYPE).reason}"
    status = subject.status()
    assert status["pending_types"] == {REPRODUCE_TYPE: {"code": "quest_mode_unsupported", "status": 501, "reason": str(error)}}
    assert status["types"][REPRODUCE_TYPE]["excluded"] == {"quest_mode_unsupported": {"count": 1, "quests": [1006]}}
    assert subject.validate((REPRODUCE_TYPE,)) == {REPRODUCE_TYPE: {"products": 0, "excluded": {"quest_mode_unsupported": 1}}}


def test_quest_types_without_a_mode_specification_are_refused_without_failing_the_catalog(setup):
    assert UNSPECIFIED_TYPE not in quest_modes.MODE_SPECS and UNSPECIFIED_TYPE not in entrust_catalog.GATE_SCAN_TYPES
    subject = build(setup, [main(), main(QuestID=1003, Type=UNSPECIFIED_TYPE)],
                    gates=((f"HasEntrustByType_{UNSPECIFIED_TYPE}", "1;"),))
    error = fail("entrust_not_offered", lambda: subject.product(1003), 409)
    assert str(error) == f"Entrust does not offer this quest type: {UNSPECIFIED_TYPE}"
    status = subject.status()
    assert status["unaudited_native_offers"] == [UNSPECIFIED_TYPE]
    assert status["refused_types"][UNSPECIFIED_TYPE] == {"quests": 1, "code": "entrust_not_offered",
                                                        "native_gate": CommandForm.LITERAL_TRUE}
    assert subject.describe(1003)["missions"] == [] and subject.product(STORY_QUEST).quest_type == 0


@pytest.mark.parametrize("hook", quest_modes.ENTRUST_PARITY_HOOKS)
def test_undeclared_battle_hooks_hold_their_type_pending_without_running(setup, monkeypatch, hook):
    def reads_the_battle_request(self, context, *state):
        return context.request.columns

    monkeypatch.setattr(DailyPolicy, hook, reads_the_battle_request)
    subject = modes(setup)
    error = fail("quest_mode_unsupported", lambda: subject.product(DAILY_QUEST), 501)
    assert hook in str(error) and subject.scopes[DAILY_TYPE].blocker == str(error)
    assert subject.product(STORY_QUEST).quest_type == 0
    monkeypatch.setattr(DailyPolicy, "entrust_parity", frozenset({hook}))
    assert modes(setup).product(DAILY_QUEST).quest_type == DAILY_TYPE


def test_unreadable_type_windows_hold_only_that_type_pending(setup):
    quests = questbook(setup, mode_rows(), menu=MODE_MENU, gates=TYPE_GATES, missions=MODE_MISSIONS)
    setup[0].pop("EmpireMissionQuestList.atb")
    subject = EntrustCatalog(quests)
    scope = subject.scopes[EMPIRE_TYPE]
    assert (scope.state, scope.windows_error) == (ScopeState.PENDING, "quest_asset_unavailable")
    fail("entrust_source_invalid", lambda: subject.product(EMPIRE_QUEST), 503)
    assert subject.product(DAILY_QUEST).quest_type == DAILY_TYPE
    assert subject.status()["mission_tags"]["error"] == "quest_asset_unavailable"


def test_quests_no_mission_lists_are_excluded_statically(setup):
    subject = build(setup, [main(), main(QuestID=1003)], unlisted=(1003,))
    fail("entrust_no_mission", lambda: subject.product(1003), 409)
    assert subject.status()["types"][0]["excluded"] == {"entrust_no_mission": {"count": 1, "quests": [1003]}}
    assert subject.product(STORY_QUEST).quest_id == STORY_QUEST


def test_menu_gates_follow_the_route_scope_chain_and_global_tag_resolution(setup):
    subject = modes(setup)
    history = subject.menu_gates[14]
    assert (history.route.func, history.route.chain, history.scope, history.form) == (
        "Func_HistoryQuestMenu", MENU_SCOPE_CHAINS["Func_HistoryQuestMenu"], "FCMenuQuestTypeList", CommandForm.OPEN_MISSION_TAG)
    gated = [mission(subject, 0, STORY_MISSION), mission(subject, DAILY_TYPE, DAILY_MISSIONS[0]),
             mission(subject, HISTORY_TYPE, HISTORY_MISSION), mission(subject, EMPIRE_TYPE, EMPIRE_MISSION)]
    assert all(subject.menu_open(entry, now=NOW, platform_code=PLATFORM) for entry in gated)
    assert subject._tags.tag_mission(HISTORY_MENU_TAG, PLATFORM).mission_id == TUTORIAL_MISSION
    product = subject.product(HISTORY_QUEST)
    assert playable(subject, HISTORY_TYPE, HISTORY_QUEST, product).mission_id == HISTORY_MISSION
    assert subject.menu_open(mission(subject, EMPIRE_TYPE, EMERGENCY_MISSION), now=NOW, platform_code=PLATFORM)


def test_tag_gates_close_with_the_mission_their_tag_resolves_to(setup):
    subject = modes(setup, overrides={TUTORIAL_MISSION: PAST_WINDOW})
    history, empire = mission(subject, HISTORY_TYPE, HISTORY_MISSION), mission(subject, EMPIRE_TYPE, EMPIRE_MISSION)
    assert not subject.menu_open(history, now=NOW, platform_code=PLATFORM)
    assert subject.menu_open(empire, now=NOW, platform_code=PLATFORM)
    error = fail("entrust_menu_closed", lambda: playable(subject, HISTORY_TYPE, HISTORY_QUEST, subject.product(HISTORY_QUEST)), 409)
    assert str(error) == MENU_CLOSED.message


def test_tag_gates_ask_whether_the_owner_is_open_not_whether_it_shows_a_quest_today(setup):
    rows = [*mode_rows()[:-1], main(QuestID=TUTORIAL_QUEST, Type=TUTORIAL_TYPE, _HoldingParam=MONDAY_ONLY)]
    subject = modes(setup, rows)
    assert subject._tags.is_open(TAG_LOOKUP, HISTORY_MENU_TAG, now=NOW, platform_code=PLATFORM)
    assert not subject._tags.displayable(TAG_LOOKUP, HISTORY_MENU_TAG, now=NOW, platform_code=PLATFORM)
    history = mission(subject, HISTORY_TYPE, HISTORY_MISSION)
    assert subject.menu_open(history, now=NOW, platform_code=PLATFORM)
    assert playable(subject, HISTORY_TYPE, HISTORY_QUEST, subject.product(HISTORY_QUEST)) is history
    assert subject.availability(now=NOW, platform_code=PLATFORM)["types"][HISTORY_TYPE]["open"] == 1


@pytest.mark.parametrize("closed,blocked", [
    ((), {}),
    ((SECOND_HISTORY_MISSION,), {"entrust_menu_closed": {"count": 1, "quests": [HISTORY_QUEST]}}),
], ids=("second_tag_open", "second_window_closed"))
def test_availability_judges_each_mission_tag_and_reports_the_furthest_stage(setup, closed, blocked):
    subject = modes(setup, missions=SPLIT_HISTORY, overrides={TUTORIAL_MISSION: PAST_WINDOW, **dict.fromkeys(closed, PAST_WINDOW)})
    first, second = (mission(subject, HISTORY_TYPE, mission_id) for mission_id in (HISTORY_MISSION, SECOND_HISTORY_MISSION))
    assert not subject.menu_open(first, now=NOW, platform_code=PLATFORM)
    assert subject.menu_open(second, now=NOW, platform_code=PLATFORM)
    assert subject._tags.tag_mission(DAILY_TAG, PLATFORM).mission_id == DAILY_MISSIONS[0]
    history = subject.availability(now=NOW, platform_code=PLATFORM)["types"][HISTORY_TYPE]
    assert (history["open"], history["blocked"]) == (0 if blocked else 1, blocked)


def test_tag_gates_resolve_per_platform_mask(setup):
    # Reproduce missions register per platform bit, so only PLATFORM resolves Tag 302 to this lower mission.
    reproduce = mission_config(REPRODUCE_TAG_MISSION, enable=1 << PLATFORM, tag=HISTORY_MENU_TAG)
    masked = {**MODE_MISSIONS, 3: ((reproduce, (TUTORIAL_QUEST,)),)}
    subject = modes(setup, missions=masked, overrides={TUTORIAL_MISSION: PAST_WINDOW})
    history = mission(subject, HISTORY_TYPE, HISTORY_MISSION)
    assert subject._tags.tag_mission(HISTORY_MENU_TAG, PLATFORM).mission_id == REPRODUCE_TAG_MISSION
    assert subject.menu_open(history, now=NOW, platform_code=PLATFORM)
    assert subject._tags.tag_mission(HISTORY_MENU_TAG, OTHER_PLATFORM).mission_id == TUTORIAL_MISSION
    assert not subject.menu_open(history, now=NOW, platform_code=OTHER_PLATFORM)
    boolean = modes(setup, missions={**MODE_MISSIONS, 7: ((mission_config(TUTORIAL_MISSION, enable=1 << PLATFORM,
                                                                           tag=HISTORY_MENU_TAG), (TUTORIAL_QUEST,)),)})
    # The tutorial loader reads Enable through B09FE0, so any nonzero mask registers it on every platform.
    assert boolean._tags.tag_mission(HISTORY_MENU_TAG, OTHER_PLATFORM).mission_id == TUTORIAL_MISSION
    fail("quest_platform_invalid", lambda: subject.menu_open(history, now=NOW, platform_code=32), 400)
    fail("quest_clock_invalid", lambda: subject.menu_open(history, now=NOW.replace(tzinfo=None), platform_code=PLATFORM), 503)
    product = subject.product(HISTORY_QUEST)
    fail("quest_platform_invalid", lambda: subject.selector(product, now=NOW, platform_code=32), 400)
    fail("quest_clock_invalid", lambda: subject.selector(product, now=NOW.replace(tzinfo=None), platform_code=PLATFORM), 503)
    fail("quest_clock_invalid", lambda: subject.ticket_free(product, history, NOW.replace(tzinfo=None)), 503)
    with pytest.raises(ValueError):
        subject.menu_open(SimpleNamespace(category=14, tag=HISTORY_MENU_TAG), now=NOW, platform_code=PLATFORM)


@pytest.mark.parametrize("menu,func,scope,form", [
    ((*MODE_MENU, ("CHistoryQuestMenu::IsEntrust_14", "0;")), "Func_HistoryQuestMenu", "CHistoryQuestMenu",
     CommandForm.LITERAL_FALSE),
    (tuple(entry for entry in MODE_MENU if entry[0] != "CmnMenu::DoReturn_Type_14") + (
        ("CmnMenu::DoReturn_Type_14", TOWER_ROUTE),), "Func_TowerTopMenu", None, CommandForm.ABSENT),
    (tuple(entry for entry in MODE_MENU if entry[0] != "CmnMenu::DoReturn_Type_14"), None, None, CommandForm.ABSENT),
])
def test_history_menu_scopes_and_routes_can_close_history_entrust(setup, menu, func, scope, form):
    subject = modes(setup, menu=menu)
    gate = subject.menu_gates[14]
    assert (None if gate.route is None else gate.route.func, gate.scope, gate.form) == (func, scope, form)
    assert not subject.menu_open(mission(subject, HISTORY_TYPE, HISTORY_MISSION), now=NOW, platform_code=PLATFORM)
    fail("entrust_menu_closed", lambda: playable(subject, HISTORY_TYPE, HISTORY_QUEST, subject.product(HISTORY_QUEST)), 409)
    assert subject.menu_open(mission(subject, EMPIRE_TYPE, EMPIRE_MISSION), now=NOW, platform_code=PLATFORM)


def test_an_unavailable_registry_only_suspends_tag_gated_categories(setup):
    quests = questbook(setup, mode_rows(), menu=MODE_MENU, gates=TYPE_GATES, missions=MODE_MISSIONS)
    setup[0].pop("ColosseumMissionQuestList.atb")
    subject = EntrustCatalog(quests)
    assert subject.status()["mission_tags"] == {"available": False, "error": "quest_asset_unavailable", "content_version": None}
    assert subject.status()["offered_types"] == [0, DAILY_TYPE, HISTORY_TYPE, EMPIRE_TYPE]
    for quest_type, mission_id in ((HISTORY_TYPE, HISTORY_MISSION), (EMPIRE_TYPE, EMPIRE_MISSION)):
        error = fail("entrust_source_invalid", lambda: subject.menu_open(
            mission(subject, quest_type, mission_id), now=NOW, platform_code=PLATFORM), 503)
        assert "quest_asset_unavailable" in str(error)
    fail("entrust_source_invalid", lambda: playable(subject, HISTORY_TYPE, HISTORY_QUEST, subject.product(HISTORY_QUEST)), 503)
    assert subject.menu_open(mission(subject, DAILY_TYPE, DAILY_MISSIONS[0]), now=NOW, platform_code=PLATFORM)
    assert playable(subject, 0, STORY_QUEST, subject.product(STORY_QUEST)).mission_id == STORY_MISSION


@pytest.mark.parametrize("duplicate,category", [
    (("FCMenuQuestTypeList::IsEntrust_5", "1;"), 5), (("CmnMenu::DoReturn_Type_5", GENERAL_ROUTE), 5),
])
def test_ambiguous_menu_keys_only_suspend_their_category(setup, duplicate, category):
    subject = modes(setup, menu=(*MODE_MENU, duplicate))
    gate = subject.menu_gates[category]
    assert gate.error is not None and gate.error.code == "native_command_ambiguous" and gate.form is CommandForm.ABSENT
    fail("entrust_source_invalid", lambda: subject.menu_open(
        mission(subject, DAILY_TYPE, DAILY_MISSIONS[0]), now=NOW, platform_code=PLATFORM), 503)
    assert subject.status()["types"][DAILY_TYPE]["categories"][category]["menu_gate"]["error"]["occurrences"] == 2
    assert subject.menu_open(mission(subject, 0, STORY_MISSION), now=NOW, platform_code=PLATFORM)


def test_unverifiable_growth_entries_only_suspend_their_missions_ticketed_quests(setup):
    campaign = [campaign_entry(DAILY_MISSIONS[0], 1, DAILY_QUEST, opens="$B4AE"),
                campaign_entry(DAILY_MISSIONS[1], 2, DAILY_QUEST, **OPEN_CAMPAIGN)]
    subject = modes(setup, campaign=campaign)
    product = subject.product(DAILY_QUEST)
    first, second = (mission(subject, DAILY_TYPE, mission_id) for mission_id in DAILY_MISSIONS)
    fail("entrust_source_invalid", lambda: subject.ticket_free(product, first, NOW), 503)
    assert subject.ticket_free(product, second, NOW) and subject.ticket_cost(product, second, 3, NOW) == 0
    history = subject.product(HISTORY_QUEST)
    assert not subject.ticket_free(history, mission(subject, HISTORY_TYPE, HISTORY_MISSION), NOW)
    story = subject.product(STORY_QUEST)
    assert subject.ticket_cost(story, mission(subject, 0, STORY_MISSION), 3, NOW) == 0
    fail("entrust_source_invalid", lambda: playable(subject, DAILY_TYPE, DAILY_QUEST, product), 503)


def test_an_unreadable_growth_table_suspends_every_ticketed_free_check(setup):
    quests = questbook(setup, mode_rows(), menu=MODE_MENU, gates=TYPE_GATES, missions=MODE_MISSIONS)
    setup[0].pop(CAMPAIGN_RESOURCE)
    subject = EntrustCatalog(quests)
    assert subject.growth.status()["unverified"]["code"] == "quest_asset_unavailable"
    assert subject.source_hashes[CAMPAIGN_RESOURCE] is None
    daily = subject.product(DAILY_QUEST)
    fail("entrust_source_invalid", lambda: subject.ticket_cost(daily, mission(subject, DAILY_TYPE, DAILY_MISSIONS[0]), 1, NOW), 503)
    story, story_mission = subject.product(STORY_QUEST), mission(subject, 0, STORY_MISSION)
    assert subject.ticket_cost(story, story_mission, 1, NOW) == 0 and subject.ticket_free(story, story_mission, NOW) is False
    with pytest.raises(ValueError):
        subject.ticket_free(story, mission(subject, DAILY_TYPE, DAILY_MISSIONS[0]), NOW)
    fail("quest_clock_invalid", lambda: subject.ticket_free(story, story_mission, NOW.replace(tzinfo=None)), 503)
    types = subject.availability(now=NOW, platform_code=PLATFORM)["types"]
    assert types[0] == {"indexed": 1, "open": 1, "free": 0, "blocked": {}}
    assert all(types[quest_type]["blocked"] == {"source_invalid": {"count": 1, "quests": [quest_id]}}
               for quest_type, quest_id in ((DAILY_TYPE, DAILY_QUEST), (HISTORY_TYPE, HISTORY_QUEST), (EMPIRE_TYPE, EMPIRE_QUEST)))


def test_selectors_prefer_the_lowest_ticket_waiving_mission(setup):
    subject = modes(setup, campaign=[campaign_entry(DAILY_MISSIONS[1], 1, DAILY_QUEST, **OPEN_CAMPAIGN)])
    product = subject.product(DAILY_QUEST)
    chosen = playable(subject, DAILY_TYPE, DAILY_QUEST, product)
    assert chosen.mission_id == DAILY_MISSIONS[1] and subject.ticket_cost(product, chosen, 3, NOW) == 0
    first = mission(subject, DAILY_TYPE, DAILY_MISSIONS[0])
    assert subject.ticket_cost(product, first, 3, NOW) == 3
    assert subject.selector(product, now=NOW, platform_code=PLATFORM).choose((first,)) is first
    plain = modes(setup)
    assert playable(plain, DAILY_TYPE, DAILY_QUEST, plain.product(DAILY_QUEST)).mission_id == DAILY_MISSIONS[0]
    for quantity in (0, -1, True, 1.0):
        with pytest.raises(ValueError):
            subject.ticket_cost(product, chosen, quantity, NOW)
    with pytest.raises(ValueError):
        subject.ticket_free(product, mission(subject, HISTORY_TYPE, HISTORY_MISSION), NOW)


def test_content_versions_ignore_time_and_on_demand_builds(setup):
    subject = modes(setup)
    version = subject.content_version
    assert subject.validate()[DAILY_TYPE] == {"products": 1, "excluded": {}}
    subject.availability(now=NOW, platform_code=PLATFORM)
    subject.availability(now=NOW + timedelta(days=400), platform_code=OTHER_PLATFORM)
    assert subject.content_version == version == EntrustCatalog(subject.quests).content_version == modes(setup).content_version


@pytest.mark.parametrize("change", [
    {"gates": (("HasEntrustByType_2", " 1 ;"), *TYPE_GATES[1:])},
    {"gates": (*TYPE_GATES, ("HasEntrustByType_17", "1;"))},
    {"menu": (*MODE_MENU, ("CHistoryQuestMenu::IsEntrust_14", "1;"))},
    {"menu": tuple(entry for entry in MODE_MENU if entry[0] != "CmnMenu::DoReturn_Type_16")
     + (("CmnMenu::DoReturn_Type_16", HISTORY_ROUTE),)},
    {"campaign": [campaign_entry(DAILY_MISSIONS[1], 1, DAILY_QUEST, **OPEN_CAMPAIGN)]},
    {"overrides": {TUTORIAL_MISSION: PAST_WINDOW}},
    {"overrides": {DAILY_MISSIONS[0]: PAST_WINDOW}},
], ids=("gate_text", "unaudited_gate", "scope_shadow", "route", "campaign", "tag_override", "type_override"))
def test_content_versions_follow_gates_routes_campaigns_and_window_overrides(setup, change):
    assert modes(setup, **change).content_version != modes(setup).content_version


def test_window_overrides_change_versions_without_the_global_registry(setup):
    assert build(setup, overrides={STORY_MISSION: PAST_WINDOW}).content_version != build(setup).content_version


def test_content_versions_follow_mode_policy_settings(setup, monkeypatch):
    version = modes(setup).content_version
    monkeypatch.setattr(policy(REPRODUCE_TYPE), "reason", "Reproduce settlement is under audit")
    audited = modes(setup)
    assert audited.content_version != version
    assert audited.status()["types"][REPRODUCE_TYPE]["policy"]["reason"] == "Reproduce settlement is under audit"


@pytest.mark.parametrize("component,change", [
    ("scopes", lambda subject: {**subject.scopes, 0: replace(subject.scopes[0], windows_error="changed")}),
    ("menu_gates", lambda subject: {**subject.menu_gates, 0: replace(subject.menu_gates[0], scope="Changed")}),
    ("growth", lambda subject: GrowthCampaign.unavailable("changed")),
    ("_index", lambda subject: {quest_id: replace(listing, disabled=True) for quest_id, listing in subject._index.items()}),
    ("excluded", lambda subject: {**subject.excluded, STORY_QUEST: NO_MISSION}),
], ids=("scopes", "menus", "growth", "index", "excluded"))
def test_every_derived_component_feeds_the_content_version(setup, component, change):
    subject = modes(setup)
    assert subject._content_version() == subject.content_version
    setattr(subject, component, change(subject))
    assert subject._content_version() != subject.content_version


def test_content_versions_follow_every_registered_command_table(setup):
    version = build(setup).content_version
    setup[0]["OnCommand.atb"] = commands(("CmnMenu::DoUnrelatedCommand", "1;"))
    assert build(setup).content_version != version


def test_rule_descriptions_feed_the_content_version(setup, monkeypatch):
    subject = build(setup)
    monkeypatch.setattr(entrust_catalog, "RULES", MappingProxyType({**entrust_catalog.RULES, "menu": "changed"}))
    assert subject._content_version() != subject.content_version


def test_source_failures_retry_while_deterministic_refusals_stay_cached(setup):
    closed = {**MODE_MISSIONS, 14: ((mission_config(HISTORY_MISSION, enable=0, tag=HISTORY_MENU_TAG), (HISTORY_QUEST,)),)}
    subject = modes(setup, missions=closed)
    resources = setup[0]
    saved_map = resources.pop("Map0007.aar")
    fail("quest_asset_unavailable", lambda: subject.product(STORY_QUEST), 503)
    assert subject.status()["types"][0]["transient_failures"] == {"quest_asset_unavailable": 1}
    resources["Map0007.aar"] = saved_map
    assert subject.product(STORY_QUEST) is subject.product(STORY_QUEST)
    error = fail("quest_closed", lambda: subject.product(HISTORY_QUEST), 409)
    assert fail("quest_closed", lambda: subject.product(HISTORY_QUEST), 409).args == error.args
    status = subject.status()["types"][HISTORY_TYPE]
    assert status["excluded"] == {"quest_closed": {"count": 1, "quests": [HISTORY_QUEST]}} and status["all_validated"]


def test_validation_builds_each_quest_once_and_counts_outcomes_per_type(setup):
    subject = modes(setup)
    assert subject.validate() == {0: {"products": 1, "excluded": {}}, DAILY_TYPE: {"products": 1, "excluded": {}},
                                  REPRODUCE_TYPE: {"products": 0, "excluded": {}},
                                  HISTORY_TYPE: {"products": 1, "excluded": {}}, EMPIRE_TYPE: {"products": 1, "excluded": {}}}
    assert subject.validate((TUTORIAL_TYPE, 255)) == {TUTORIAL_TYPE: {"products": 0, "excluded": {"entrust_not_offered": 1}},
                                                      255: {"products": 0, "excluded": {}}}
    assert subject.validate(()) == {}
    for selection in ("0", [True], [-1], 0, [1.0]):
        with pytest.raises(ValueError):
            subject.validate(selection)


def test_status_reads_a_snapshot_while_validation_builds(setup, monkeypatch):
    rows = [main(QuestID=quest_id) for quest_id in range(1001, 1061)]
    subject = build(setup, rows)
    original, paused, resumed = subject._product, threading.Event(), threading.Event()

    def pausing(quest_id):
        product = original(quest_id)
        if quest_id == 1030:
            paused.set()
            resumed.wait(10)
        return product

    monkeypatch.setattr(subject, "_product", pausing)
    with ThreadPoolExecutor(1) as pool:
        validation = pool.submit(subject.validate, (0,))
        assert paused.wait(10)
        midway = subject.status()
        resumed.set()
        assert validation.result(10) == {0: {"products": 60, "excluded": {}}}
    assert midway["ready"] and (midway["types"][0]["validated"], midway["types"][0]["all_validated"]) == (30, False)
    assert subject.status()["types"][0]["all_validated"]


def test_status_never_sees_products_change_size_under_concurrent_validation(setup):
    subject = build(setup, [main(QuestID=quest_id) for quest_id in range(1001, 1401)])
    with ThreadPoolExecutor(1) as pool:
        validation = pool.submit(subject.validate, (0,))
        reports = []
        while not validation.done():
            reports.append(subject.status()["types"][0]["validated"])
        assert validation.result() == {0: {"products": 400, "excluded": {}}}
    assert reports == sorted(reports) and subject.status()["types"][0]["validated"] == 400



class ContendedLock:
    def __init__(self, lock):
        self.lock, self.contended = lock, threading.Event()

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            self.contended.set()
            self.lock.acquire()
        return self

    def __exit__(self, *failure):
        self.lock.release()


def test_concurrent_requests_for_one_quest_share_its_single_built_definition(setup, monkeypatch):
    subject = build(setup)
    lock, original, entered, released = ContendedLock(subject._lock), subject.quests.get, threading.Event(), threading.Event()

    def blocking(quest_id):
        entered.set()
        assert released.wait(10)
        return original(quest_id)

    monkeypatch.setattr(subject, "_lock", lock)
    monkeypatch.setattr(subject.quests, "get", blocking)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(subject.product, STORY_QUEST)
        assert entered.wait(10)
        second = pool.submit(subject.product, STORY_QUEST)
        contended = lock.contended.wait(10)
        released.set()
        products = first.result(10), second.result(10)
    assert contended and products[0] is products[1] is subject.product(STORY_QUEST)
    subject.require_eligible(saved(products[0]), products[1])


def test_availability_counts_open_quests_and_the_furthest_blocking_stage(setup):
    campaign = [campaign_entry(DAILY_MISSIONS[1], 1, DAILY_QUEST, **OPEN_CAMPAIGN)]
    subject = modes(setup, campaign=campaign, overrides={EMPIRE_MISSION: PAST_WINDOW})
    result = subject.availability(now=NOW, platform_code=PLATFORM)
    assert (result["platform_code"], result["preview"], result["pending"]) == (PLATFORM, False, {})
    assert result["types"] == {
        0: {"indexed": 1, "open": 1, "free": 0, "blocked": {}},
        DAILY_TYPE: {"indexed": 1, "open": 1, "free": 1, "blocked": {}},
        HISTORY_TYPE: {"indexed": 1, "open": 1, "free": 0, "blocked": {}},
        EMPIRE_TYPE: {"indexed": 1, "open": 0, "free": 0, "blocked": {"mission_window": {"count": 1, "quests": [EMPIRE_QUEST]}}}}
    preview = subject.availability(now=NOW, platform_code=PLATFORM,
                                   tags=subject.quests.preview_mission_tags({TUTORIAL_MISSION: PAST_WINDOW}))
    assert preview["preview"] and preview["types"][HISTORY_TYPE]["blocked"] == {
        "entrust_menu_closed": {"count": 1, "quests": [HISTORY_QUEST]}}
    assert preview["types"][EMPIRE_TYPE]["open"] == 1
    # Story, Daily, History and Empire missions read Enable through B09FE0, so a mask without this bit still registers.
    assert subject.availability(now=NOW, platform_code=UNMASKED_PLATFORM)["types"] == result["types"]
    disabled = {**MODE_MISSIONS, 0: ((mission_config(STORY_MISSION, enable=0), (STORY_QUEST,)),)}
    assert modes(setup, missions=disabled).availability(now=NOW, platform_code=PLATFORM)["types"][0]["blocked"] == {
        "not_registered": {"count": 1, "quests": [STORY_QUEST]}}
    with pytest.raises(ValueError):
        subject.availability(now=NOW, platform_code=PLATFORM, tags=subject._tags.registry)
    fail("quest_platform_invalid", lambda: subject.availability(now=NOW, platform_code=-1), 400)


def test_availability_reports_closed_days_and_unverifiable_sources(setup):
    rows = mode_rows(_HoldingParam=MONDAY_ONLY)
    closed_day = modes(setup, rows).availability(now=NOW, platform_code=PLATFORM)["types"][DAILY_TYPE]
    assert closed_day["blocked"] == {"day_closed": {"count": 1, "quests": [DAILY_QUEST]}}
    closed_missions = modes(setup, rows, overrides=dict.fromkeys(DAILY_MISSIONS, PAST_WINDOW))
    assert closed_missions.availability(now=NOW, platform_code=PLATFORM)["types"][DAILY_TYPE]["blocked"] == {
        "mission_window": {"count": 1, "quests": [DAILY_QUEST]}}
    unverifiable = modes(setup, campaign=[campaign_entry(DAILY_MISSIONS[0], 1, DAILY_QUEST, closes="2026/8")])
    daily = unverifiable.availability(now=NOW, platform_code=PLATFORM)["types"][DAILY_TYPE]
    assert daily["blocked"] == {"source_invalid": {"count": 1, "quests": [DAILY_QUEST]}}
    ambiguous = modes(setup, menu=(*MODE_MENU, ("FCMenuQuestTypeList::IsEntrust_14", OPEN_TAG)))
    assert ambiguous.availability(now=NOW, platform_code=PLATFORM)["types"][HISTORY_TYPE]["blocked"] == {
        "source_invalid": {"count": 1, "quests": [HISTORY_QUEST]}}


@pytest.mark.parametrize("tickets,blocked", [
    ({}, {}),
    ({"ConsignmentTicketID": ITEM_NAME, "ConsignmentTicketConsumptionCount": 1},
     {"source_invalid": {"count": 1, "quests": [EMPIRE_QUEST]}}),
], ids=("ticketless", "ticketed"))
def test_only_ticketed_quests_ask_every_open_mission_for_its_menu_gate(setup, tickets, blocked):
    listed = {**MODE_MISSIONS, 1: ((mission_config(EMERGENCY_MISSION, tag=EMPIRE_MENU_TAG), (EMPIRE_QUEST,)),)}
    # A second IsEntrust_16 row leaves only the Empire menu gate unverifiable; the Emergency gate stays literal.
    menu = (*MODE_MENU, ("FCMenuQuestTypeList::IsEntrust_16", OPEN_TAG))
    quests = questbook(setup, [main(), main(QuestID=EMPIRE_QUEST, Type=EMPIRE_TYPE, **tickets)], menu=menu,
                       gates=TYPE_GATES, missions=listed)
    result = EntrustCatalog(quests).availability(now=NOW, platform_code=PLATFORM)["types"][EMPIRE_TYPE]
    assert (result["open"], result["blocked"]) == (0 if blocked else 1, blocked)


def test_a_closed_empire_menu_blocks_type_18_entrust_like_its_admission(setup):
    opened, closed = modes(setup), modes(setup, overrides={EMPIRE_MENU_MISSION: PAST_WINDOW})
    blocked = {Blocker.MODE_ENTRY_CLOSED: {"count": 1, "quests": [EMPIRE_QUEST]}}
    assert opened.availability(now=NOW, platform_code=PLATFORM)["types"][EMPIRE_TYPE]["open"] == 1
    assert closed.availability(now=NOW, platform_code=PLATFORM)["types"][EMPIRE_TYPE] == {
        "indexed": 1, "open": 0, "free": 0, "blocked": blocked}
    assert blocked == {"mode_entry_closed": {"count": 1, "quests": [EMPIRE_QUEST]}}
    preview = opened.availability(now=NOW, platform_code=PLATFORM,
                                  tags=opened.quests.preview_mission_tags({EMPIRE_MENU_MISSION: PAST_WINDOW}))
    assert preview["types"][EMPIRE_TYPE]["blocked"] == blocked
    assert closed.menu_open(mission(closed, EMPIRE_TYPE, EMPIRE_MISSION), now=NOW, platform_code=PLATFORM)
    assert [subject.describe(EMPIRE_QUEST, now=NOW, platform_code=PLATFORM)["live"]["entry_open"]
            for subject in (opened, closed)] == [True, False]
    assert admission(opened, EMPIRE_QUEST, GATE_RECORDS).mission_id == EMPIRE_MISSION
    fail("quest_closed", lambda: admission(closed, EMPIRE_QUEST, GATE_RECORDS), 409)


def test_mode_entry_gates_follow_the_day_gate_like_check_start(setup, monkeypatch):
    asked = []

    def closed(self, catalog, *, now, platform_code):
        asked.append((catalog, now, platform_code))
        return False

    monkeypatch.setattr(DailyPolicy, "entry_open", closed)
    closed_day = modes(setup, mode_rows(_HoldingParam=MONDAY_ONLY)).availability(now=NOW, platform_code=PLATFORM)
    assert closed_day["types"][DAILY_TYPE]["blocked"] == {"day_closed": {"count": 1, "quests": [DAILY_QUEST]}}
    assert asked == []
    subject = modes(setup)
    assert subject.availability(now=NOW, platform_code=PLATFORM)["types"][DAILY_TYPE]["blocked"] == {
        "mode_entry_closed": {"count": 1, "quests": [DAILY_QUEST]}}
    assert asked == [(subject.quests, NOW, PLATFORM)]


def test_mode_entry_gates_are_asked_once_per_type_and_failures_mark_the_source(setup, monkeypatch):
    subject, asked, story = build(setup, [main(QuestID=quest_id) for quest_id in STORY_QUESTS]), [], type(policy(0))

    def opened(self, catalog, *, now, platform_code):
        asked.append((catalog, now, platform_code))
        return True

    def unavailable(self, catalog, *, now, platform_code):
        asked.append((catalog, now, platform_code))
        raise ProtocolError("quest_mode_resource_invalid", "Mode entry sources are unavailable", 503)

    monkeypatch.setattr(story, "entry_open", opened)
    assert subject.availability(now=NOW, platform_code=PLATFORM)["types"][0]["open"] == len(STORY_QUESTS)
    assert asked == [(subject.quests, NOW, PLATFORM)]
    monkeypatch.setattr(story, "entry_open", unavailable)
    assert subject.availability(now=NOW, platform_code=PLATFORM)["types"][0]["blocked"] == {
        "source_invalid": {"count": len(STORY_QUESTS), "quests": list(STORY_QUESTS)}}
    assert len(asked) == 2
    assert subject.describe(STORY_QUEST, now=NOW, platform_code=PLATFORM)["live"]["entry_open"] == {
        "error": "quest_mode_resource_invalid"}


def test_replayable_versions_keep_the_digest_of_the_three_hook_parity_set(setup, monkeypatch):
    subject = modes(setup)
    assert subject.replayable_content_versions() == {subject.content_version, subject.legacy_content_version}
    assert subject.legacy_content_version != subject.content_version
    subjugation = type(policy(6))
    for hook in set(ENTRUST_PARITY_HOOKS) - set(LEGACY_PARITY_HOOKS):
        monkeypatch.setattr(subjugation, hook, getattr(ModePolicy, hook))
    plain = modes(setup)
    assert plain.content_version == plain.legacy_content_version == subject.legacy_content_version
    assert plain.replayable_content_versions() == {subject.legacy_content_version}


def test_validation_reports_each_type_then_each_quest_to_its_progress_listener(setup):
    subject, reports = modes(setup), []
    assert subject.validate(progress=reports.append) == modes(setup).validate()
    totals = ((0, 1), (DAILY_TYPE, 1), (REPRODUCE_TYPE, 0), (HISTORY_TYPE, 1), (EMPIRE_TYPE, 1))
    assert reports == [{"quest_type": quest_type, "done": done, "total": total}
                       for quest_type, total in totals for done in range(total + 1)]
    reports.clear()
    assert subject.validate((DAILY_TYPE, 255), progress=reports.append) == {
        DAILY_TYPE: {"products": 1, "excluded": {}}, 255: {"products": 0, "excluded": {}}}
    assert reports == [{"quest_type": DAILY_TYPE, "done": 0, "total": 1},
                       {"quest_type": DAILY_TYPE, "done": 1, "total": 1}, {"quest_type": 255, "done": 0, "total": 0}]
    with pytest.raises(ValueError):
        subject.validate(progress="reports")


def test_a_failing_progress_listener_stops_validation_after_the_quest_it_heard_about(setup):
    subject = modes(setup)

    def interrupt(report):
        if report["done"]:
            raise RuntimeError("validation interrupted")

    with pytest.raises(RuntimeError, match="^validation interrupted$"):
        subject.validate(progress=interrupt)
    types = subject.status()["types"]
    assert (types[0]["validated"], types[DAILY_TYPE]["validated"], types[EMPIRE_TYPE]["validated"]) == (1, 0, 0)


def test_offered_types_without_quests_report_empty_counts(setup):
    subject = modes(setup, [main()])
    vacant = (DAILY_TYPE, HISTORY_TYPE, EMPIRE_TYPE)
    status = subject.status()
    assert status["offered_types"] == [0, *vacant]
    assert all((status["types"][quest_type]["quests"], status["types"][quest_type]["all_validated"]) == (0, True)
               for quest_type in vacant)
    types = subject.availability(now=NOW, platform_code=PLATFORM)["types"]
    assert all(types[quest_type] == {"indexed": 0, "open": 0, "free": 0, "blocked": {}} for quest_type in vacant)
    assert subject.validate(vacant) == {quest_type: {"products": 0, "excluded": {}} for quest_type in vacant}
    with pytest.raises(ValueError):
        subject.menu_open(replace(mission(subject, 0, STORY_MISSION), category=max(quest_modes.MISSION_CATEGORIES) + 1),
                          now=NOW, platform_code=PLATFORM)


def test_describe_reports_the_static_listing_and_live_gates(setup):
    subject = modes(setup, campaign=[campaign_entry(DAILY_MISSIONS[1], 1, DAILY_QUEST, **OPEN_CAMPAIGN)])
    daily = subject.describe(DAILY_QUEST, now=NOW, platform_code=PLATFORM)
    assert (daily["quest_type"], daily["type_state"], daily["exclusion"], daily["disabled"]) == (
        DAILY_TYPE, "offered", None, False)
    assert daily["ticket"] == {"item": ITEM_NAME, "per_run": 1}
    assert [(entry["mission_id"], entry["menu_gate"], entry["tag"]) for entry in daily["missions"]] == [
        (DAILY_MISSIONS[0], CommandForm.LITERAL_TRUE, DAILY_TAG), (DAILY_MISSIONS[1], CommandForm.LITERAL_TRUE, DAILY_TAG)]
    assert [(entry["menu"], entry["free"], entry["state"]) for entry in daily["live"]["missions"]] == [
        (True, False, "open"), (True, True, "open")]
    assert daily["product"] == {"built": True, "entry_label": "Map0007.aar/Entry02.atb", "reward_codes": [4, 1003],
                                "conditional": False, "candidates": 3}
    history = subject.describe(HISTORY_QUEST, now=NOW, platform_code=PLATFORM)
    assert history["missions"][0]["route"] == "Func_HistoryQuestMenu"
    assert history["live"]["missions"][0]["tag_owner"] == TUTORIAL_MISSION and history["live"]["day_open"] is True
    assert history["live"]["entry_open"] is True
    empire = subject.describe(EMPIRE_QUEST, now=NOW, platform_code=PLATFORM)["live"]
    assert (empire["day_open"], empire["entry_open"]) == (True, True)
    assert "live" not in subject.describe(STORY_QUEST)
    assert subject.describe(STORY_QUEST, now=NOW, platform_code=PLATFORM)["live"]["missions"][0]["free"] is None
    tutorial = subject.describe(TUTORIAL_QUEST)
    assert tutorial["exclusion"]["code"] == "entrust_not_offered" and tutorial["product"] is None
    assert [entry["mission_id"] for entry in tutorial["missions"]] == [TUTORIAL_MISSION]
    fail("quest_not_found", lambda: subject.describe(9999), 404)
    fail("quest_clock_invalid", lambda: subject.describe(DAILY_QUEST, platform_code=PLATFORM), 503)


def test_status_lists_stay_bounded(setup):
    rows = [main(QuestID=quest_id) for quest_id in range(1001, 1002 + STATUS_LIMIT + 8)]
    gates = tuple((f"HasEntrustByType_{quest_type}", "1;") for quest_type in UNAUDITED_GATE_TYPES)
    subject = build(setup, rows, unlisted=tuple(row["QuestID"] for row in rows[1:]), gates=gates)
    status = subject.status()
    excluded = status["types"][0]["excluded"]["entrust_no_mission"]
    assert excluded == {"count": STATUS_LIMIT + 8, "quests": list(range(1002, 1002 + STATUS_LIMIT))}
    refused = [quest_type for quest_type, scope in subject.scopes.items() if scope.state is ScopeState.REFUSED]
    assert status["refused_type_count"] == len(refused) > STATUS_LIMIT
    assert list(status["refused_types"]) == refused[:STATUS_LIMIT]


def test_registered_command_tables_without_rows_close_every_menu(setup):
    quests = questbook(setup)
    setup[0].update(dict.fromkeys(REGISTERED_TABLES, commands()))
    subject = EntrustCatalog(quests)
    status = subject.status()["commands"]
    assert (status["tables"], status["rows"], status["missing_tables"], status["invalid_tables"]) == (len(REGISTERED_TABLES), 0, {}, {})
    gate = subject.menu_gates[0]
    assert (gate.route, gate.hit, gate.form, gate.error) == (MenuRoute(0, None, None, ()), None, CommandForm.ABSENT, None)
    product = subject.product(STORY_QUEST)
    assert not subject.menu_open(mission(subject, 0, STORY_MISSION), now=NOW, platform_code=PLATFORM)
    fail("entrust_menu_closed", lambda: playable(subject, 0, STORY_QUEST, product), 409)


REAL_NOON, REAL_GRACE = datetime(2026, 11, 2, 12, tzinfo=JST), datetime(2026, 11, 2, 0, 3, tzinfo=JST)
CAMPAIGN_DAY = datetime(2026, 8, 21, 12, tzinfo=JST)
CONSIGNMENT_TICKET = "item_consignment_ticket"
INDEXED = MappingProxyType({0: 114, DAILY_TYPE: 28, REPRODUCE_TYPE: 207, HISTORY_TYPE: 2016, EMPIRE_TYPE: 84})
REFUSED_QUESTS = MappingProxyType({1: 61, 4: 9, 6: 16, 9: 343, 10: 452, 11: 92, 14: 123, 15: 16, 19: 21})
LIMITED_DAILY = [314, 324, 334, 344]
# Each Gold Rush quest adds its 7000-band prize enemy, which grants unit 437 instead of its slot treasure, from three stars.
GOLD_RUSH_QUESTS, GOLD_RUSH_OVERRIDE = range(4807, 4814), 437
HISTORY_REPRODUCE_QUEST, HISTORY_REPRODUCE_MISSION = 9032, 920014
REAL_HISTORY_QUEST, REAL_EMPIRE_QUEST, REAL_EMPIRE_MISSION = 4125, 7317, 940007
# The Rance collaboration quests 6385-6387 consume no consignment ticket.
TICKETED_REPRODUCE, UNTICKETED_REPRODUCE = 204, 3


def fresh(real, **options):
    shared = real.quests
    return QuestCatalog(shared.snapshot, shared._asset_reader, possessions=shared._possessions,
                        resource_version=shared.resource_version, **options)


@pytest.fixture(scope="module")
def real():
    if (not (PROJECT / "runtime/server/masterdata/active.json").is_file()
            or not (PROJECT.parent / "aigisr_exe/Aigis_r.exe").is_file()):
        pytest.skip("Verified local client resources and public masters are required")
    master = MasterDataStore(PROJECT / "runtime/server/masterdata").snapshot()
    manifest, entries = ResourceLibrary(PROJECT / "runtime/resources", client_hash=master.client_hash).load()
    assets = IndexedAssetService(entries, PROJECT / "runtime/resources", PROJECT.parent / "aigisr_exe")
    reader = lambda name: assets.read(name)[0]
    possessions = PossessionService(PROJECT.parent / "aigisr_exe", asset_reader=reader)
    quests = QuestCatalog(master, reader, possessions=possessions, resource_version=manifest["version"])
    subject = EntrustCatalog(quests)
    return SimpleNamespace(quests=quests, subject=subject, validated=subject.validate())


def test_real_scope_offers_the_audited_types_whose_native_gates_are_open(real):
    status = real.subject.status()
    assert status["ready"] and status["offered_types"] == [0, DAILY_TYPE, REPRODUCE_TYPE, HISTORY_TYPE, EMPIRE_TYPE]
    assert status["pending_types"] == {}
    assert status["unaudited_native_offers"] == [3, 17]
    assert {quest_type: entry["quests"] for quest_type, entry in status["refused_types"].items() if entry["quests"]} == REFUSED_QUESTS
    assert {quest_type: (entry["native_gate"]["table"], entry["native_gate"]["row"], entry["native_gate"]["text"])
            for quest_type, entry in status["types"].items()} == {
        0: (None, None, None), DAILY_TYPE: (SYSTEM_COMMANDS, 30, "1;"), REPRODUCE_TYPE: (SYSTEM_COMMANDS, 32, "1;"),
        HISTORY_TYPE: (SYSTEM_COMMANDS, 33, "1;"), EMPIRE_TYPE: (SYSTEM_COMMANDS, 40, "1;")}


def test_real_indexes_build_every_offered_quest_including_the_gold_rush_prize_enemies(real):
    types = real.subject.status()["types"]
    assert {quest_type: entry["indexed"] for quest_type, entry in types.items() if entry["indexed"]} == INDEXED
    assert real.validated == {quest_type: {"products": count, "excluded": {}} for quest_type, count in INDEXED.items()}
    assert sum(entry["products"] for entry in real.validated.values()) == 2449
    assert all(types[quest_type]["all_validated"] and not types[quest_type]["excluded"] for quest_type in INDEXED)


def test_real_entrust_tickets_cost_one_consignment_ticket_outside_story(real):
    types = real.subject.status()["types"]
    assert types[0]["ticket"] == {"items": {}, "per_run": {0: 114}}
    for quest_type in (DAILY_TYPE, HISTORY_TYPE, EMPIRE_TYPE):
        assert types[quest_type]["ticket"] == {"items": {CONSIGNMENT_TICKET: INDEXED[quest_type]},
                                               "per_run": {1: INDEXED[quest_type]}}
    assert all(types[quest_type]["disabled"] == {"count": 0, "quests": []} for quest_type in INDEXED)


def test_real_menu_gates_follow_the_general_and_history_scope_chains(real):
    status = real.subject.status()
    gates = {(quest_type, category): (entry["route"]["func"], entry["route"]["row"], entry["scope"],
                                      entry["menu_gate"]["row"], entry["menu_gate"]["form"])
             for quest_type, data in status["types"].items() for category, entry in data["categories"].items()}
    general, history, scope = "Func_GeneralQuestMenu", "Func_HistoryQuestMenu", "FCMenuQuestTypeList"
    assert gates == {
        (0, 0): (general, 0, scope, 34, CommandForm.LITERAL_TRUE), (DAILY_TYPE, 5): (general, 5, scope, 125, CommandForm.LITERAL_TRUE),
        (REPRODUCE_TYPE, 3): (general, 3, scope, 127, CommandForm.LITERAL_TRUE),
        (REPRODUCE_TYPE, 14): (history, 13, scope, 129, CommandForm.OPEN_MISSION_TAG),
        (HISTORY_TYPE, 14): (history, 13, scope, 129, CommandForm.OPEN_MISSION_TAG),
        (EMPIRE_TYPE, 16): (general, 15, scope, 143, CommandForm.OPEN_MISSION_TAG),
        (EMPIRE_TYPE, 1): (general, 1, scope, 126, CommandForm.LITERAL_TRUE)}
    assert real.subject.menu_gates[14].route.chain == MENU_SCOPE_CHAINS[history]
    commands_status = status["commands"]
    assert (commands_status["tables"], commands_status["rows"], commands_status["missing_tables"],
            commands_status["invalid_tables"]) == (106, 4718, {}, {})
    assert status["mission_tags"]["available"] and status["sources"]["errors"] == {}


def test_real_history_quest_4125_belongs_to_mission_920025(real):
    described = real.subject.describe(REAL_HISTORY_QUEST, now=REAL_NOON, platform_code=CLIENT_PLATFORM)
    assert [entry["mission_id"] for entry in described["missions"]] == [920025]
    live = described["live"]["missions"][0]
    assert (live["menu"], live["tag_owner"], live["free"]) == (True, TUTORIAL_MISSION, False)
    assert described["ticket"] == {"item": CONSIGNMENT_TICKET, "per_run": 1}


def test_real_availability_on_a_jst_monday(real):
    noon = real.subject.availability(now=REAL_NOON, platform_code=CLIENT_PLATFORM)["types"]
    assert {quest_type: entry["open"] for quest_type, entry in noon.items()} == {0: 114, DAILY_TYPE: 24, REPRODUCE_TYPE: 1,
                                                                                HISTORY_TYPE: 2016, EMPIRE_TYPE: 84}
    assert noon[DAILY_TYPE]["blocked"] == {"day_closed": {"count": 4, "quests": LIMITED_DAILY}}
    assert all(entry["free"] == 0 for entry in noon.values()) and not any(noon[quest_type]["blocked"] for quest_type in (0, 16, 18))
    grace = real.subject.availability(now=REAL_GRACE, platform_code=CLIENT_PLATFORM)["types"][DAILY_TYPE]
    assert (grace["open"], grace["blocked"]) == (28, {})
    campaign = real.subject.availability(now=CAMPAIGN_DAY, platform_code=CLIENT_PLATFORM)["types"][DAILY_TYPE]
    assert (campaign["open"], campaign["free"]) == (24, 24)


def test_real_client_platform_9_registers_and_opens_like_platform_1(real):
    missions = real.quests.mission_registry().ordered()
    assert [mission.enabled(CLIENT_PLATFORM) for mission in missions] == [mission.enabled(PLATFORM) for mission in missions]
    for moment in (REAL_NOON, REAL_GRACE, CAMPAIGN_DAY):
        client, other = (real.subject.availability(now=moment, platform_code=code) for code in (CLIENT_PLATFORM, PLATFORM))
        assert (client["types"], client["pending"]) == (other["types"], other["pending"])
    for quest_id in (STORY_QUEST, REAL_HISTORY_QUEST, REAL_EMPIRE_QUEST, HISTORY_REPRODUCE_QUEST, *LIMITED_DAILY):
        client, other = (real.subject.describe(quest_id, now=REAL_NOON, platform_code=code)["live"]
                         for code in (CLIENT_PLATFORM, PLATFORM))
        assert {**client, "platform_code": PLATFORM} == other and client["entry_open"] is True


def test_real_history_entrust_closes_with_the_tutorial_mission_900001(real):
    closed = {TUTORIAL_MISSION: PAST_WINDOW}
    subject = EntrustCatalog(fresh(real, window_overrides=closed))
    result = subject.availability(now=REAL_NOON, platform_code=CLIENT_PLATFORM)["types"]
    assert result[HISTORY_TYPE]["open"] == 0 and result[HISTORY_TYPE]["blocked"]["entrust_menu_closed"]["count"] == 2016
    assert result[REPRODUCE_TYPE]["open"] == 0
    assert result[REPRODUCE_TYPE]["blocked"]["entrust_menu_closed"] == {"count": 1, "quests": [HISTORY_REPRODUCE_QUEST]}
    assert (result[EMPIRE_TYPE]["open"], result[0]["open"], result[DAILY_TYPE]["open"]) == (84, 114, 24)
    preview = real.subject.availability(now=REAL_NOON, platform_code=CLIENT_PLATFORM,
                                        tags=real.quests.preview_mission_tags(closed))
    assert preview["types"] == result and subject.content_version != real.subject.content_version


def test_real_empire_entrust_closes_with_the_tag_801_mission_940012(real):
    closed = {EMPIRE_MENU_MISSION: PAST_WINDOW}
    subject = EntrustCatalog(fresh(real, window_overrides=closed))
    empire = subject.availability(now=REAL_NOON, platform_code=CLIENT_PLATFORM)["types"][EMPIRE_TYPE]
    # 940012 lists six empire quests itself; the other 78 lose only the Tag 801 menu entry.
    assert (empire["open"], {blocker: entry["count"] for blocker, entry in empire["blocked"].items()}) == (
        0, {"mission_window": 6, "mode_entry_closed": 78})
    preview = real.subject.availability(now=REAL_NOON, platform_code=CLIENT_PLATFORM,
                                        tags=real.quests.preview_mission_tags(closed))
    assert preview["types"][EMPIRE_TYPE] == empire
    live = subject.describe(REAL_EMPIRE_QUEST, now=REAL_NOON, platform_code=CLIENT_PLATFORM)["live"]
    gates = {entry["mission_id"]: (entry["registered"], entry["state"], entry["menu"]) for entry in live["missions"]}
    assert (gates[REAL_EMPIRE_MISSION], live["entry_open"]) == ((True, "open", True), False)


def test_real_reproduce_entrust_reaches_its_history_member_while_the_batches_stay_closed(real):
    product = real.subject.product(HISTORY_REPRODUCE_QUEST)
    assert (product.mode.mission_ids, product.ticket_item_id, product.ticket_count) == (
        (HISTORY_REPRODUCE_MISSION,), CONSIGNMENT_TICKET, 1)
    for quest_id in GOLD_RUSH_QUESTS:
        gold_rush = real.subject.product(quest_id)
        below, from_three = gold_rush.reward_codes(0), gold_rush.reward_codes(REQUIRED_MAX_RANK)
        assert GOLD_RUSH_OVERRIDE not in below and from_three.count(GOLD_RUSH_OVERRIDE) == 1
        assert sorted(from_three) == sorted((*below, GOLD_RUSH_OVERRIDE)) and gold_rush.all_reward_codes == from_three
        described = real.subject.describe(quest_id)["product"]
        assert described["conditional"] and GOLD_RUSH_OVERRIDE in described["reward_codes"]
    assert real.quests.decode_reward(GOLD_RUSH_OVERRIDE).kind == "unit"
    types = real.subject.status()["types"]
    assert types[REPRODUCE_TYPE]["ticket"] == {"items": {CONSIGNMENT_TICKET: TICKETED_REPRODUCE},
                                               "per_run": {0: UNTICKETED_REPRODUCE, 1: TICKETED_REPRODUCE}}
    reproduce = real.subject.availability(now=REAL_NOON, platform_code=CLIENT_PLATFORM)["types"][REPRODUCE_TYPE]
    assert (reproduce["indexed"], reproduce["open"], reproduce["free"]) == (INDEXED[REPRODUCE_TYPE], 1, 0)
    assert set(reproduce["blocked"]) == {"mission_window"} and reproduce["blocked"]["mission_window"]["count"] == 206


def test_real_story_quests_keep_verified_maps_and_consume_no_tickets(real):
    products = [real.subject.product(quest_id) for quest_id in real.quests.all_ids]
    assert len(products) == 114 and sum(len(product.quest.candidates) for product in products) == 613
    assert sum(len(product.reward_codes(REQUIRED_MAX_RANK)) for product in products) == 613
    assert all(product.reward_codes(0) == product.all_reward_codes for product in products)
    assert all(product.ticket_count == 0 and product.ticket_item_id is None and not product.disabled for product in products)
    assert not set(real.quests.all_ids) & set(real.quests.special_conditions())
    assert real.subject.source_hashes[CONDITION_RESOURCE] == real.quests.status()["mode_resource_sha256"][CONDITION_RESOURCE]
    assert real.quests.status()["all_maps_validated"]
    story = real.subject.product(STORY_QUEST)
    real.subject.require_eligible(saved(story, flags=0), story)
