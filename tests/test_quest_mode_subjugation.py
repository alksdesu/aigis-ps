from datetime import datetime
import json
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps import quest_modes
from aigis_ps.alxml import Column, Container
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.item_summon_tickets import ItemSummonTicketService
from aigis_ps.mission_window_revival import MissionWindowRules, build_mission_window_revival
from aigis_ps.present_backlog import MAX_PRESENT_TEXT_BYTES
from aigis_ps.quest_catalog import QuestCatalog
from aigis_ps.quest_mode_subjugation import POLICY, ROW_VALUES
from aigis_ps.quest_modes import (
    CLEAR_FLAG, ENTRUST_TYPES, MAX_BOOST, MODE_PROGRESS, OPEN, UNBOOSTED, ModeServices, ScoreContext, SettleContext,
)
from aigis_ps.quest_service import CHARGE_KEY, LEGACY_MODE_KEYS, MODE_KEYS, SETTLEMENT_CHARGE, QuestService
from aigis_ps.subjugation_rules import (
    BEST_LIMIT, CONFIG_TABLE, COOPERATIVE_TYPE, DEFAULT_RULES, GOLD_PRESENT, LEGACY_PRESENTS, NAMED_ITEM_PRESENT,
    PROGRESS_NAME, QUEST_LIST_TABLE, RECORDS_ABOVE, RECORDS_BELOW, REWARD_TABLE, SUBJUGATION_TYPE, UNIT_PRESENT,
    BoostPolicy, KillsPolicy, ScorePair, SubjugationCatalog, SubjugationRules, pack, unpack, with_watermark,
)
from aigis_ps.summon_tickets import SummonTicketRules
from test_quest_battle_modes import (
    Recorder, charisma, hard_start, mutate, put_record, record, set_rank, update_game_param,
)
from test_quest_score_settlement import quest_record
from test_quest_service import allow_quest, bundle, exit_request, nodes, request, setup
from test_quest_settlement import last_settlement
from test_subjugation_rules import Overlay


NOW = datetime(2026, 11, 2, 12, tzinfo=JST)
GATE_QUEST = 1004
# 400183 and 400212 list rank 7 quests; 400587 needs rank 5.
SUBJUGATION_RANK = 7
CHARISMA = 1000
NORMAL_START, HARD_START, NORMAL_RESULT = 81, 82, 83
LABEL = "スコアクエスト報酬"
MISSIONS = MappingProxyType({
    400183: (6401, 6402, 6403, 6404, 6405),
    400212: (6611, 6612, 6613, 6614, 6615),
    400587: (30018, 30019, 30020, 30021, 30022, 30023),
})
OPEN_MISSION, ENDED_MISSIONS = 400587, (400183, 400212)
FIRST_QUESTS = tuple(members[0] for members in MISSIONS.values())
OPEN_QUESTS = MISSIONS[OPEN_MISSION][1:]
# The second quest of each mission is the first one with a start button.
ENDED_QUESTS = tuple(MISSIONS[mission_id][1] for mission_id in ENDED_MISSIONS)
OPENER, LAST_QUEST, PENULTIMATE_QUEST = OPEN_QUESTS[0], OPEN_QUESTS[-1], OPEN_QUESTS[-2]
# 400183 and 400212 share their original window, so one batch reopens both.
ENDED_BATCH = "Subjugation:400183"
PERMANENT = {"start_at": None, "end_at": None}
TICKET_ITEM, TICKET_TIER = "item_smn_ticket_85144", 1127
UNIT, GOLD, TICKET = UNIT_PRESENT, GOLD_PRESENT, NAMED_ITEM_PRESENT
LEGACY_SEVEN, LEGACY_FOURTEEN = sorted(LEGACY_PRESENTS)
# Group 462 pays its type 6 tiers at best 100, 200, ..., 750 in table order, then the 1127 ticket.
BEST_TIER_PRESENTS = ((UNIT, 1346, None), (UNIT, 61, None), (UNIT, 6, None), (UNIT, 360, None), (GOLD, 30000, None),
                      (LEGACY_SEVEN, 50, None), (GOLD, 60000, None), (UNIT, 361, None), (GOLD, 90000, None),
                      (UNIT, 362, None))
TICKET_COUNT = 1
TICKET_PRESENT = (TICKET, TICKET_COUNT, TICKET_ITEM)
# Its type 4 tiers at totals 375, 750, ..., 3375.
TOTAL_TIER_PRESENTS = ((UNIT, 360, None), (UNIT, 3, None), (UNIT, 61, None), (UNIT, 4, None), (UNIT, 5, None),
                       (UNIT, 361, None), (GOLD, 30000, None), (UNIT, 4, None), (UNIT, 61, None))
LOCKED, CLOSED = "quest_locked", "quest_closed"
OTHER_PROGRESS = {"goddess": {"granted": 5}}


@pytest.fixture(scope="module")
def reopened(bundle):
    shared = bundle.quests.catalog

    def base(name):
        return bundle.native_cdn.assets.read(name)[0]

    revival = build_mission_window_revival(base, MissionWindowRules(True, {ENDED_BATCH: PERMANENT}))
    assert set(revival.replacements) == {CONFIG_TABLE}

    def read(name):
        return revival.replacements[name] if name in revival.replacements else base(name)

    return QuestCatalog(shared.snapshot, read, possessions=bundle.possessions, resource_version=shared.resource_version)


@pytest.fixture(scope="module")
def ticket_pools(bundle):
    tickets = bundle.item_summon_tickets
    pool = tickets.catalog.definition(TICKET_ITEM).pool_id

    def service(pools):
        return ItemSummonTicketService(bundle.bootstrap, bundle.player_state, bundle.presents, bundle.summon_pools,
                                       tickets.catalog, rules=SummonTicketRules(tuple(sorted(pools))))

    enabled = set(tickets.rules.enabled_pools)
    return SimpleNamespace(pool=pool, open=service(enabled | {pool}), closed=service(enabled - {pool}))


@pytest.fixture
def scene(setup, bundle):
    _, store = setup
    allow_quest(store, GATE_QUEST)
    set_rank(bundle, store, SUBJUGATION_RANK)
    update_game_param(store, NowCharisma=CHARISMA)

    def service(catalog=None, *, rules=DEFAULT_RULES, subjugation=True, presents=True):
        catalog = bundle.quests.catalog if catalog is None else catalog
        made = QuestService(catalog, bundle.bootstrap, bundle.player_state, bundle.possessions, clock=lambda: NOW,
                            randbelow=lambda _: 0)
        made.presents = bundle.presents if presents else None
        made.missions = Recorder()
        made.subjugation = SubjugationCatalog(catalog, rules) if subjugation else None
        return made

    return SimpleNamespace(store=store, service=service)


def open_pool(monkeypatch, bundle, ticket_pools, enabled=True):
    monkeypatch.setattr(bundle.presents, "item_tickets", ticket_pools.open if enabled else ticket_pools.closed)


def start(service, store, quest_id, tick=1000, *, boost=None, number=NORMAL_START):
    return hard_start(service, store, quest_id, tick, number, boost=boost)


def result(store, quest_id, kills, tick=1001):
    columns = (Container("RT", (
        Container("LG", (Column("TG", "S", ("finishQuest",)), Column("TT", "S", (f"{quest_id} {kills} {kills}",)))),
        Container("DropResult", (Column("DP", "I", (0,)), Column("DPLIST", "I", (0,)), Column("GoldGet", "I", (0,)))),
    )),)
    return request(store, NORMAL_RESULT, columns, tick=tick)


def play(service, store, quest_id, kills, tick=1000, *, boost=None):
    service.start(start(service, store, quest_id, tick, boost=boost), store)
    return nodes(service.finish(result(store, quest_id, kills, tick + 1), store))


def refused(code, status, store, call):
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        call()
    assert (error.value.code, error.value.status) == (code, status)
    assert store.profile() == before
    return error.value


def active(store):
    return store.profile().state["quest_state"]["active"]


def progress(store):
    return store.profile().state["quest_state"].get(MODE_PROGRESS, {})


def saved_watermark(store, mission_id=OPEN_MISSION):
    return progress(store).get(PROGRESS_NAME, {}).get(str(mission_id))


def save_progress(store, best, total, *, others=None):
    def change(state):
        journal = QuestService._journal(state)
        journal[MODE_PROGRESS] = dict(others or {})
        return with_watermark({**state, "quest_state": journal}, OPEN_MISSION, ScorePair(best, total))
    mutate(store, change)


def mailed(bundle, store):
    state = store.profile().state
    rows = {row["PresentID"]: row for row in bundle.presents.rows(state)}
    presents = [rows[present_id] for present_id in last_settlement(store)["mode_presents"]]
    assert all(row["Description"] == LABEL for row in presents)
    return [(row["Type"], row["Content"], row.get("ItemID")) for row in presents]


def described(service, store, mission_id=OPEN_MISSION):
    view = service.subjugation.describe(store.profile().state, NOW)
    return next(entry for entry in view["missions"] if entry["mission_id"] == mission_id)


def ticket_on(catalog, tier):
    rows, group = [], None
    for row in catalog.resource_rows(REWARD_TABLE):
        # RewardConfig rows with ID_Reward 0 continue the group above them.
        group = row["ID_Reward"] or group
        if (group, row["ID_Incentive"]) == (tier.group, tier.incentive):
            row = {**row, "Type_SubIncentive": TICKET, "Param_Incentive": TICKET_COUNT, "ID_Item": TICKET_ITEM}
        rows.append(row)
    return rows


def disagreement(quest_id, basis, saved, recorded):
    return (f"Subjugation mission {OPEN_MISSION} settles quest {quest_id} from best {basis[0]} and total {basis[1]}: "
            f"its watermark holds {saved[0]} and {saved[1]} while its records held {recorded[0]} and {recorded[1]}")


def disagreements(caplog):
    return [entry.getMessage() for entry in caplog.records if entry.getMessage().startswith("Subjugation mission")]


def test_subjugation_policy_scores_type_6_behind_the_menu_gate():
    assert quest_modes.policy(SUBJUGATION_TYPE) is POLICY and POLICY.name == PROGRESS_NAME
    assert POLICY.status() == {"policy": "subjugation", "enabled": True, "reason": None, "hard_mode": False,
                               "timed_conditions": False, "gate_quest": GATE_QUEST}
    support = quest_modes.support_status()[SUBJUGATION_TYPE]
    assert (support["mission_resources"], support["score_mode"], support["gold_counted"], support["hard_rank"],
            support["entrust"]) == (["Subjugation"], True, False, None, False)
    # Entrust never reaches Type 6, so the overridden battle hooks need no entrust counterpart.
    assert SUBJUGATION_TYPE not in ENTRUST_TYPES
    assert POLICY.entrust_blocker() == (
        "Mode subjugation overrides check_start, start_boost, score_result, settle "
        "without a declared entrust counterpart")


def test_subjugation_local_rules_name_the_policies_the_rules_file_can_choose():
    rules = quest_modes.support_status()[SUBJUGATION_TYPE]["local_rules"]
    assert rules == POLICY.local_rules() and json.loads(json.dumps(rules)) == rules
    assert rules["kills_policies"] == [policy.value for policy in KillsPolicy]
    assert rules["boost_policies"] == [policy.value for policy in BoostPolicy]
    assert (rules["default_rules"], rules["best_limit"]) == (SubjugationRules().to_dict(), BEST_LIMIT)
    assert rules["rewards"] == "event_mission_catalog_ladder_tiers_of_conditions_4_6_7"
    rules["kills_policies"].clear()
    rules["default_rules"]["kills_policy"] = KillsPolicy.RAW.value
    assert POLICY.local_rules() == quest_modes.support_status()[SUBJUGATION_TYPE]["local_rules"] != rules


def test_every_subjugation_quest_builds_as_a_score_quest_without_prizes(bundle):
    catalog = bundle.quests.catalog
    quest_ids = catalog.playable_quests(SUBJUGATION_TYPE)
    assert quest_ids == tuple(sorted(quest_id for members in MISSIONS.values() for quest_id in members))
    assert set(quest_ids) <= set(catalog.mode_ids)
    windows = catalog.mission_windows(SUBJUGATION_TYPE)
    for mission_id, members in MISSIONS.items():
        for quest_id in members:
            quest, mode = catalog.get(quest_id), catalog.mode(quest_id)
            assert POLICY.validate_row(catalog.row(quest_id), catalog) is None
            assert (mode.quest_type, mode.score_mode, mode.gold_counted, mode.hard) == (
                SUBJUGATION_TYPE, True, False, None)
            assert (mode.star_rewards_normal, mode.star_rewards_hard, mode.drop_bonus) == ((), (), None)
            assert quest.candidates == () and not any(quest.treasures) and not quest.unit_list
            # Each quest names its map after the one listed mission that owns its scores.
            listed = tuple(mission.mission_id for mission in windows.missions(quest_id) if mission.listed)
            assert listed == mode.mission_ids == (mode.map_mission_id,) == (mission_id,)
    assert not set(catalog.status()["mode_failed_quests"]) & set(quest_ids)


@pytest.mark.parametrize("change", [
    {"Type": 0}, {"Type": float(SUBJUGATION_TYPE)}, {"Treasure1": 1001}, {"Treasure5": 1}, {"BonusType": 3},
    {"BonusType": None}, {"UnitList": 258}, {"UnitList": False},
])
def test_rows_outside_the_score_contract_are_refused(bundle, change):
    catalog = bundle.quests.catalog
    row = {**catalog.row(OPENER), **change}
    assert set(change) <= set(ROW_VALUES)
    with pytest.raises(ProtocolError) as error:
        POLICY.validate_row(row, catalog)
    assert (error.value.code, error.value.status) == ("quest_definition_invalid", 503)


def test_open_subjugation_quests_start_while_ended_batches_stay_closed(scene, bundle, ticket_pools, monkeypatch):
    open_pool(monkeypatch, bundle, ticket_pools)
    service, store = scene.service(), scene.store
    for tick, quest_id in enumerate(OPEN_QUESTS, start=1000):
        service.start(start(service, store, quest_id, tick), store)
        assert (active(store)["quest_id"], active(store)["mission_id"]) == (quest_id, OPEN_MISSION)
    for mission_id in ENDED_MISSIONS:
        for quest_id in MISSIONS[mission_id][:2]:
            refused(CLOSED, 409, store, lambda: service.start(start(service, store, quest_id, 3000), store))
    refused(LOCKED, 409, store, lambda: service.start(start(service, store, MISSIONS[OPEN_MISSION][0], 3000), store))


def test_the_reopened_ended_batch_opens_both_missions_but_not_their_first_quests(scene, reopened):
    service, store = scene.service(reopened), scene.store
    windows = reopened.mission_windows(SUBJUGATION_TYPE)
    assert all(windows.mission(mission_id).state(NOW) == OPEN for mission_id in ENDED_MISSIONS)
    for tick, (mission_id, quest_id) in enumerate(zip(ENDED_MISSIONS, ENDED_QUESTS), start=1000):
        before = charisma(store)
        service.start(start(service, store, quest_id, tick), store)
        assert (active(store)["quest_id"], active(store)["mission_id"]) == (quest_id, mission_id)
        assert charisma(store) == before and active(store)[CHARGE_KEY] == SETTLEMENT_CHARGE
        assert active(store)["definition"]["charisma"] == reopened.get(quest_id).charisma > 0
    for quest_id in FIRST_QUESTS:
        refused(LOCKED, 409, store, lambda: service.start(start(service, store, quest_id, 3000), store))


def test_start_buttons_come_from_the_quest_list_rather_than_the_member_order(scene, bundle):
    catalog = bundle.quests.catalog
    first, second = MISSIONS[OPEN_MISSION][:2]
    buttons = {row["QuestID"]: row["StartButtonID"] for row in catalog.resource_rows(QUEST_LIST_TABLE)
               if row["MissionID"] == OPEN_MISSION}
    assert not buttons[first] and buttons[second]
    swapped = {first: buttons[second], second: buttons[first]}
    rows = [{**row, "StartButtonID": swapped[row["QuestID"]]}
            if row["MissionID"] == OPEN_MISSION and row["QuestID"] in swapped else row
            for row in catalog.resource_rows(QUEST_LIST_TABLE)]
    service, store = scene.service(), scene.store
    service.subjugation = SubjugationCatalog(Overlay(catalog, {QUEST_LIST_TABLE: rows}))
    refused(LOCKED, 409, store, lambda: service.start(start(service, store, second), store))
    service.start(start(service, store, first), store)
    assert active(store)["quest_id"] == first


def test_missing_services_refuse_starts_without_charging(scene):
    store = scene.store
    without_catalog = scene.service(subjugation=False)
    refused("quest_subjugation_unavailable", 409, store,
            lambda: without_catalog.start(start(without_catalog, store, OPENER), store))
    without_presents = scene.service(presents=False)
    refused("quest_present_unavailable", 409, store,
            lambda: without_presents.start(start(without_presents, store, OPENER), store))


def test_four_star_starts_are_refused(scene):
    service, store = scene.service(), scene.store
    refused("quest_hard_unavailable", 409, store,
            lambda: service.start(start(service, store, OPENER, number=HARD_START), store))


def test_cooperative_missions_refuse_their_quests_as_unsupported(scene, bundle):
    catalog = bundle.quests.catalog
    rows = [{**row, "SubjugationType": COOPERATIVE_TYPE} if row["MissionID"] == OPEN_MISSION else row
            for row in catalog.resource_rows(CONFIG_TABLE)]
    service, store = scene.service(), scene.store
    service.subjugation = SubjugationCatalog(Overlay(catalog, {CONFIG_TABLE: rows}))
    refused("quest_mode_unsupported", 501, store, lambda: service.start(start(service, store, OPENER), store))


def test_a_subjugation_result_packs_max_rank_and_mails_the_tiers_it_crosses(scene, bundle):
    service, store = scene.service(), scene.store
    tree = play(service, store, OPENER, 100)
    assert quest_record(tree)["MaxRank"] == record(store, OPENER)["MaxRank"] == pack(100, 100)
    assert record(store, OPENER)["AchieveFlag"] == CLEAR_FLAG and "PPT" in tree
    # A79200 overwrites only the rows it receives, so the result carries the played quest alone as it was written.
    assert {len(column.values) for column in tree["QR"].children} == {1}
    assert quest_record(tree) == {name: record(store, OPENER)[name] for name in quest_record(tree)}
    assert service.missions.events == [("quest_clear", {
        "quest_id": OPENER, "quest_type": SUBJUGATION_TYPE, "stars": 0, "hard": False, "mission_id": OPEN_MISSION,
        "team": [], "score_mode": True, "kills": 100})]
    settlement = last_settlement(store)
    assert (settlement["stars"], settlement["kills_reported"], settlement["score"], settlement["boost"]) == (
        0, 100, 100, UNBOOSTED)
    assert settlement["score_details"] == {
        "expected": 100, "factor": 1.0, "kills_scored": 100, "kills_over_expected": 0, "best": 100, "total": 100,
        "rules": DEFAULT_RULES.to_dict(), "rules_version": DEFAULT_RULES.content_version}
    assert mailed(bundle, store) == [BEST_TIER_PRESENTS[0], (LEGACY_FOURTEEN, 1, None)]
    assert settlement["mode_presents_pending"] == 0 and saved_watermark(store) == {"best": 100, "total": 100}
    view = described(service, store)
    assert (view["basis"], view["mismatch"], view["watermark"], view["best"], view["total"]) == (
        {"best": 100, "total": 100}, {}, {"best": 100, "total": 100}, 100, 100)
    assert view["first_quest"] == {"quest_id": FIRST_QUESTS[-1], "recorded": False, "best": 0, "total": 0}


def test_consecutive_runs_start_from_the_packed_record_and_mail_only_new_crossings(scene, bundle, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    service, store = scene.service(), scene.store
    play(service, store, OPENER, 100)
    service.start(start(service, store, OPENER, 1002), store)
    assert active(store)["candidate_stars"] == pack(100, 100)
    finishing = result(store, OPENER, 100, 1003)
    response = service.finish(finishing, store)
    assert quest_record(nodes(response))["MaxRank"] == pack(100, 200)
    assert last_settlement(store)["candidate_stars"] == pack(100, 100)
    assert mailed(bundle, store) == [(LEGACY_FOURTEEN, 1, None)]
    assert saved_watermark(store) == {"best": 100, "total": 200}
    completed = store.profile()
    assert service.finish(finishing, store) == response and store.profile() == completed
    # Each run leaves the watermark where the records stand, so ordinary play logs no disagreement.
    assert disagreements(caplog) == []


def test_best_tiers_mail_once_each_and_repeated_crossings_merge_into_one_present(scene, bundle):
    service, store = scene.service(), scene.store
    tree = play(service, store, PENULTIMATE_QUEST, 750)
    assert quest_record(tree)["MaxRank"] == pack(750, 750)
    assert mailed(bundle, store) == [*BEST_TIER_PRESENTS, (LEGACY_FOURTEEN, 10, None), *TOTAL_TIER_PRESENTS[:2]]
    play(service, store, PENULTIMATE_QUEST, 750, tick=1002)
    assert mailed(bundle, store) == [(LEGACY_FOURTEEN, 10, None), *TOTAL_TIER_PRESENTS[2:4]]
    assert saved_watermark(store) == {"best": 750, "total": 1500}


def test_a_boosted_run_mails_every_tier_up_to_its_total_including_the_ticket(scene, bundle, ticket_pools, monkeypatch):
    open_pool(monkeypatch, bundle, ticket_pools)
    assert ticket_pools.pool in ticket_pools.open.rules.enabled_pools
    service, store = scene.service(), scene.store
    tree = play(service, store, LAST_QUEST, 751, boost=MAX_BOOST)
    assert quest_record(tree)["MaxRank"] == pack(TICKET_TIER, TICKET_TIER * MAX_BOOST)
    # The capped repeating tier stops at 1500; the floored one counts every 150 above it.
    assert mailed(bundle, store) == [*BEST_TIER_PRESENTS, TICKET_PRESENT, (LEGACY_FOURTEEN, 20, None),
                                     (LEGACY_FOURTEEN, 12, None), *TOTAL_TIER_PRESENTS]
    assert last_settlement(store)["mode_presents_pending"] == 0


def test_a_disabled_ticket_pool_refuses_only_the_quest_a_clamped_run_lifts_past_its_tier(scene, bundle, ticket_pools,
                                                                                       monkeypatch):
    open_pool(monkeypatch, bundle, ticket_pools, enabled=False)
    service, store = scene.service(), scene.store
    error = refused("quest_subjugation_reward_unsupported", 501, store,
                    lambda: service.start(start(service, store, LAST_QUEST), store))
    assert error.__cause__.code == "summon_ticket_pool_disabled"
    assert ticket_pools.pool not in ticket_pools.closed.rules.enabled_pools
    for tick, quest_id in enumerate(OPEN_QUESTS[:-1], start=1000):
        service.start(start(service, store, quest_id, tick), store)
        assert active(store)["quest_id"] == quest_id


def test_status_lists_the_ladder_tiers_item_ticket_rules_without_their_pool_leave_unmailable(scene, bundle,
                                                                                             ticket_pools, monkeypatch):
    service = scene.service()
    open_pool(monkeypatch, bundle, ticket_pools)
    assert service.status()["mode_reward_delivery"][SUBJUGATION_TYPE] == {"ready": True, "undeliverable": []}
    open_pool(monkeypatch, bundle, ticket_pools, enabled=False)
    report = service.status()["mode_reward_delivery"][SUBJUGATION_TYPE]
    assert report["ready"] is False
    assert [(entry["mission_id"], entry["threshold"], entry["item_id"], entry["reason"])
            for entry in report["undeliverable"]] == [
        (OPEN_MISSION, TICKET_TIER, TICKET_ITEM, "summon_ticket_pool_disabled")]
    assert scene.service(subjugation=False).status()["mode_reward_delivery"][SUBJUGATION_TYPE] == {
        "ready": False, "error": "quest_subjugation_unavailable"}
    assert scene.service(presents=False).status()["mode_reward_delivery"][SUBJUGATION_TYPE] == {
        "ready": False, "error": "quest_present_unavailable"}


def test_reward_labels_past_the_native_cell_refuse_the_start_without_charging(scene, bundle):
    class Relabeled(Overlay):
        def system_text(self, index):
            return "x" * (MAX_PRESENT_TEXT_BYTES + 1)

    service, store = scene.service(), scene.store
    service.subjugation = SubjugationCatalog(Relabeled(bundle.quests.catalog))
    # An empty-mailbox preview takes the label, but the settlement could not queue a present carrying it.
    error = refused("quest_subjugation_reward_unsupported", 501, store,
                    lambda: service.start(start(service, store, OPENER), store))
    assert error.__cause__.code == "quest_present_unsupported"


def test_raw_kills_let_every_run_reach_the_disabled_ticket_tier(scene, bundle, ticket_pools, monkeypatch):
    open_pool(monkeypatch, bundle, ticket_pools, enabled=False)
    service, store = scene.service(rules=SubjugationRules(kills_policy="raw")), scene.store
    for quest_id in OPEN_QUESTS:
        refused("quest_subjugation_reward_unsupported", 501, store,
                lambda: service.start(start(service, store, quest_id), store))
    refused(LOCKED, 409, store, lambda: service.start(start(service, store, FIRST_QUESTS[-1]), store))
    # A watermark already past the ticket tier leaves nothing unmailable within reach.
    save_progress(store, TICKET_TIER, 0)
    service.start(start(service, store, OPENER), store)
    assert active(store)["quest_id"] == OPENER


@pytest.mark.parametrize("boost_policy, reaches_ticket", [("total_only", False), ("total_and_best", True),
                                                          ("ignore", False)])
def test_the_preflight_reaches_as_far_as_the_boost_policy_lets_a_run_score(scene, bundle, ticket_pools, monkeypatch,
                                                                            boost_policy, reaches_ticket):
    open_pool(monkeypatch, bundle, ticket_pools, enabled=False)
    service, store = scene.service(rules=SubjugationRules(boost_policy=boost_policy)), scene.store

    def boosted():
        return service.start(start(service, store, PENULTIMATE_QUEST, boost=MAX_BOOST), store)

    if reaches_ticket:
        refused("quest_subjugation_reward_unsupported", 501, store, boosted)
    else:
        boosted()
        assert active(store)["quest_id"] == PENULTIMATE_QUEST


@pytest.mark.parametrize("kills_policy, kills, score, scored", [
    ("clamp", 751, TICKET_TIER, 751), ("clamp", 800, TICKET_TIER, 751), ("raw", 800, 1200, 800),
])
def test_the_last_quest_scores_kills_by_the_kills_policy(scene, bundle, ticket_pools, monkeypatch, kills_policy, kills,
                                                         score, scored):
    open_pool(monkeypatch, bundle, ticket_pools)
    rules = SubjugationRules(kills_policy=kills_policy)
    service, store = scene.service(rules=rules), scene.store
    tree = play(service, store, LAST_QUEST, kills)
    assert quest_record(tree)["MaxRank"] == pack(score, score)
    settlement = last_settlement(store)
    assert (settlement["kills_reported"], settlement["score"]) == (kills, score)
    assert settlement["score_details"] == {
        "expected": 751, "factor": 1.5, "kills_scored": scored, "kills_over_expected": kills - 751, "best": score,
        "total": score, "rules": rules.to_dict(), "rules_version": rules.content_version}


@pytest.mark.parametrize("rules, kills, best, total", [
    (SubjugationRules(boost_policy="total_only"), 100, 100, 100 * MAX_BOOST),
    (SubjugationRules(boost_policy="total_and_best"), 100, 100 * MAX_BOOST, 100 * MAX_BOOST),
    (SubjugationRules(kills_policy="raw", boost_policy="total_and_best"), 2000, BEST_LIMIT, 2000 * MAX_BOOST),
])
def test_boost_policies_split_a_boosted_score_between_total_and_best(scene, bundle, ticket_pools, monkeypatch, rules,
                                                                     kills, best, total):
    open_pool(monkeypatch, bundle, ticket_pools)
    service, store = scene.service(rules=rules), scene.store
    before = charisma(store)
    service.start(start(service, store, OPENER, boost=MAX_BOOST), store)
    assert active(store)["mode"]["boost"] == MAX_BOOST and set(active(store)["mode"]) == MODE_KEYS
    # 400587 charges no charisma, so even three runs cost nothing.
    assert charisma(store) == before
    tree = nodes(service.finish(result(store, OPENER, kills), store))
    assert quest_record(tree)["MaxRank"] == pack(best, total)
    settlement = last_settlement(store)
    assert (settlement["boost"], settlement["score"]) == (MAX_BOOST, kills)
    assert (settlement["score_details"]["best"], settlement["score_details"]["total"]) == (best, total)


@pytest.mark.parametrize("boost_policy, runs, best", [
    ("total_only", MAX_BOOST, 100), ("total_and_best", MAX_BOOST, 100 * MAX_BOOST), ("ignore", UNBOOSTED, 100),
])
def test_boosted_reopened_runs_charge_at_their_settlement_unless_the_boost_is_ignored(scene, reopened, boost_policy,
                                                                                      runs, best):
    service, store = scene.service(reopened, rules=SubjugationRules(boost_policy=boost_policy)), scene.store
    quest_id = ENDED_QUESTS[0]
    before = charisma(store)
    service.start(start(service, store, quest_id, boost=MAX_BOOST), store)
    # Entry costs are frozen at the start and paid only once 83 settles the clear.
    assert charisma(store) == before and active(store)["charge"] == "settle"
    mode = active(store)["mode"]
    if runs == UNBOOSTED:
        assert set(mode) == LEGACY_MODE_KEYS and mode["boost_ignored"] == MAX_BOOST
    else:
        assert (mode["boost"], mode["boost_ignored"]) == (runs, None)
    tree = nodes(service.finish(result(store, quest_id, 100), store))
    assert before - charisma(store) == reopened.get(quest_id).charisma * runs > 0
    assert quest_record(tree)["MaxRank"] == pack(best, 100 * runs)
    assert last_settlement(store)["boost"] == runs


def test_abandoning_a_subjugation_run_charges_nothing(scene, reopened):
    service, store = scene.service(reopened), scene.store
    quest_id = ENDED_QUESTS[0]
    before = charisma(store)
    service.start(start(service, store, quest_id), store)
    service.exit(exit_request(store, quest_id), store)
    assert charisma(store) == before and active(store) is None
    [entry] = store.profile().state["quest_state"]["history"]
    assert (entry["kind"], entry["quest_id"]) == ("retreat", quest_id)


def test_a_run_that_ends_without_kills_still_settles_and_charges(scene, reopened):
    service, store = scene.service(reopened), scene.store
    quest_id = ENDED_QUESTS[0]
    before = charisma(store)
    service.start(start(service, store, quest_id), store)
    assert charisma(store) == before and active(store)["charge"] == "settle"
    tree = nodes(service.finish(result(store, quest_id, 0), store))
    assert before - charisma(store) == reopened.get(quest_id).charisma > 0
    assert quest_record(tree)["MaxRank"] == pack(0, 0) == 0
    assert last_settlement(store)["score"] == 0 and active(store) is None


def test_boost_values_beyond_the_menu_are_refused_unless_ignored(scene):
    store = scene.store
    service = scene.service()
    refused("quest_boost_invalid", 400, store,
            lambda: service.start(start(service, store, OPENER, boost=MAX_BOOST + 1), store))
    ignoring = scene.service(rules=SubjugationRules(boost_policy="ignore"))
    ignoring.start(start(ignoring, store, OPENER, boost=MAX_BOOST + 1), store)
    assert set(active(store)["mode"]) == LEGACY_MODE_KEYS and active(store)["mode"]["boost_ignored"] == MAX_BOOST + 1


@pytest.mark.parametrize("boost_policy", list(BoostPolicy))
@pytest.mark.parametrize("requested, applied", [
    (None, UNBOOSTED), (UNBOOSTED, UNBOOSTED), (2, 2), (MAX_BOOST, MAX_BOOST), (MAX_BOOST + 1, None), (True, None),
])
def test_start_boosts_follow_the_boost_policy(bundle, boost_policy, requested, applied):
    rules = SubjugationRules(boost_policy=boost_policy)
    context = SimpleNamespace(services=ModeServices(subjugation=SubjugationCatalog(bundle.quests.catalog, rules)),
                              request=SimpleNamespace(boost=requested))
    # The ignore policy charges one run for any Boost.V and leaves the value to boost_ignored.
    applied = UNBOOSTED if boost_policy is BoostPolicy.IGNORE else applied
    if applied is not None:
        assert POLICY.start_boost(context) == applied
        return
    with pytest.raises(ProtocolError) as error:
        POLICY.start_boost(context)
    assert (error.value.code, error.value.status) == ("quest_boost_invalid", 400)


def test_hooks_without_the_subjugation_catalog_refuse_with_a_conflict():
    context = SimpleNamespace(services=ModeServices(), request=None)
    with pytest.raises(ProtocolError) as error:
        POLICY.start_boost(context)
    assert (error.value.code, error.value.status) == ("quest_subjugation_unavailable", 409)


def test_finishing_without_the_subjugation_catalog_keeps_the_attempt_open(scene):
    service, store = scene.service(), scene.store
    service.start(start(service, store, OPENER), store)
    service.subjugation = None
    refused("quest_subjugation_unavailable", 409, store, lambda: service.finish(result(store, OPENER, 100), store))
    assert active(store)["quest_id"] == OPENER


def test_saves_without_a_watermark_start_from_their_records(scene, bundle):
    service, store = scene.service(), scene.store
    put_record(store, OPENER, max_rank=pack(100, 300), flags=CLEAR_FLAG)
    tree = play(service, store, OPEN_QUESTS[1], 300)
    assert quest_record(tree)["MaxRank"] == pack(300, 300)
    # The records already reached best 100 and total 300, so only the tiers above them are mailed.
    assert mailed(bundle, store) == [*BEST_TIER_PRESENTS[1:3], (LEGACY_FOURTEEN, 4, None), TOTAL_TIER_PRESENTS[0]]
    assert saved_watermark(store) == {"best": 300, "total": 600}


def test_a_replayed_quest_without_a_watermark_starts_from_its_own_record(scene, bundle):
    service, store = scene.service(), scene.store
    put_record(store, OPENER, max_rank=pack(100, 100), flags=CLEAR_FLAG)
    tree = play(service, store, OPENER, 100)
    assert quest_record(tree)["MaxRank"] == pack(100, 200)
    # Its own record already reached best 100, so only the repeating tier the run lifts to 200 is mailed.
    assert mailed(bundle, store) == [(LEGACY_FOURTEEN, 1, None)]
    assert saved_watermark(store) == {"best": 100, "total": 200}


def test_a_watermark_above_the_records_keeps_settled_tiers_from_mailing_twice(scene, bundle, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    service, store = scene.service(), scene.store
    save_progress(store, 300, 750, others=OTHER_PROGRESS)
    assert described(service, store)["mismatch"] == {"best": RECORDS_BELOW, "total": RECORDS_BELOW}
    tree = play(service, store, OPENER, 100)
    assert quest_record(tree)["MaxRank"] == pack(100, 100)
    assert mailed(bundle, store) == [] and "PPT" not in tree
    assert progress(store) == {**OTHER_PROGRESS, PROGRESS_NAME: {str(OPEN_MISSION): {"best": 300, "total": 750}}}
    play(service, store, PENULTIMATE_QUEST, 750, tick=1002)
    assert mailed(bundle, store) == [*BEST_TIER_PRESENTS[3:], (LEGACY_FOURTEEN, 1, None)]
    assert progress(store) == {**OTHER_PROGRESS, PROGRESS_NAME: {str(OPEN_MISSION): {"best": 750, "total": 850}}}
    # Both results settle while the records lag behind the watermark, so each one logs it.
    assert disagreements(caplog) == [disagreement(OPENER, (300, 750), (300, 750), (0, 0)),
                                     disagreement(PENULTIMATE_QUEST, (300, 750), (300, 750), (100, 100))]
    assert described(service, store)["mismatch"] == {}


def test_records_raised_above_the_watermark_settle_from_the_records_without_mailing_the_gap(scene, bundle,
                                                                                           ticket_pools, monkeypatch,
                                                                                           caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    service, store = scene.service(), scene.store
    save_progress(store, 100, 100)
    put_record(store, LAST_QUEST, max_rank=pack(TICKET_TIER, TICKET_TIER), flags=CLEAR_FLAG)
    view = described(service, store)
    assert (view["basis"], view["mismatch"]) == ({"best": TICKET_TIER, "total": TICKET_TIER},
                                                 {"best": RECORDS_ABOVE, "total": RECORDS_ABOVE})
    # The result page counts the tiers the raised records passed as reached, so even the ticket is out of reach.
    open_pool(monkeypatch, bundle, ticket_pools, enabled=False)
    play(service, store, OPENER, 100)
    assert mailed(bundle, store) == [(LEGACY_FOURTEEN, 1, None)]
    assert saved_watermark(store) == {"best": TICKET_TIER, "total": TICKET_TIER + 100}
    assert disagreements(caplog) == [
        disagreement(OPENER, (TICKET_TIER, TICKET_TIER), (100, 100), (TICKET_TIER, TICKET_TIER))]
    assert described(service, store)["mismatch"] == {}


def test_a_watermark_above_the_records_adds_no_reach_to_the_preflight(scene, bundle, ticket_pools, monkeypatch):
    open_pool(monkeypatch, bundle, ticket_pools, enabled=False)
    catalog = bundle.quests.catalog
    top = SubjugationCatalog(catalog).ladder(OPEN_MISSION)[-1]
    service, store = scene.service(), scene.store
    service.subjugation = SubjugationCatalog(Overlay(catalog, {REWARD_TABLE: ticket_on(catalog, top)}))
    assert (top.source, top.threshold) == ("total", service.subjugation.mission(OPEN_MISSION).limit)
    save_progress(store, 0, top.threshold - 1)
    # A run lifts only the records, which stay far below the watermark, so the ticket just above it is out of reach.
    play(service, store, OPENER, 100)
    assert mailed(bundle, store) == [BEST_TIER_PRESENTS[0]]
    assert saved_watermark(store) == {"best": 100, "total": top.threshold - 1}
    played = unpack(record(store, OPENER)["MaxRank"])
    put_record(store, PENULTIMATE_QUEST, max_rank=pack(0, top.threshold - played.total - 1), flags=CLEAR_FLAG)
    # Records one point below the ticket let the next run lift them onto it, so that start must be able to mail it.
    refused("quest_subjugation_reward_unsupported", 501, store,
            lambda: service.start(start(service, store, OPENER, 1002), store))


def test_settlements_without_a_previous_record_start_from_the_other_members(bundle):
    catalog = bundle.quests.catalog
    services = ModeServices(subjugation=SubjugationCatalog(catalog))
    rows = [{"QuestID": OPENER, "MaxRank": pack(100, 100)}, {"QuestID": LAST_QUEST, "MaxRank": pack(80, 80)}]
    state = {"tables": {"quest_records": rows}, "quest_state": QuestService._journal({})}

    def settle(previous):
        return POLICY.settle(SettleContext(catalog, catalog.get(OPENER), catalog.mode(OPENER), None, state, False, 0,
                                           NOW, 0, CLEAR_FLAG, True, OPEN_MISSION, services, previous_record=previous))

    absent, empty = settle(None), settle({"QuestID": OPENER, "MaxRank": 0})
    assert absent.presents == empty.presents and absent.state == empty.state
    assert [(reward["kind"], reward.get("template_id")) for reward in absent.presents] == [("unit", 1346),
                                                                                           ("legacy_item", None)]
    assert absent.state["quest_state"][MODE_PROGRESS] == {
        PROGRESS_NAME: {str(OPEN_MISSION): {"best": 100, "total": 180}}}
    assert state["quest_state"] == QuestService._journal({})


def test_each_score_settles_from_the_higher_of_its_watermark_and_its_records(bundle, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    catalog = bundle.quests.catalog
    services = ModeServices(subjugation=SubjugationCatalog(catalog))
    rows = [{"QuestID": OPENER, "MaxRank": pack(100, 250)}, {"QuestID": LAST_QUEST, "MaxRank": pack(80, 80)}]
    state = with_watermark({"tables": {"quest_records": rows}, "quest_state": QuestService._journal({})}, OPEN_MISSION,
                           ScorePair(750, 100))
    outcome = POLICY.settle(SettleContext(catalog, catalog.get(OPENER), catalog.mode(OPENER), None, state, False, 0,
                                          NOW, 0, CLEAR_FLAG, True, OPEN_MISSION, services,
                                          previous_record={"QuestID": OPENER, "MaxRank": pack(100, 100)}))
    # The best starts from the watermark and the total from the records, so only the repeating tier between them pays.
    assert [(reward["kind"], reward["amount"]) for reward in outcome.presents] == [("legacy_item", 2)]
    assert outcome.state["quest_state"][MODE_PROGRESS] == {
        PROGRESS_NAME: {str(OPEN_MISSION): {"best": 750, "total": 330}}}
    assert disagreements(caplog) == [disagreement(OPENER, (750, 180), (750, 100), (100, 180))]


@pytest.mark.parametrize("record_row", [{"MaxRank": -1}, {"MaxRank": 1.5}, {}])
def test_score_results_need_a_packed_quest_record(bundle, record_row):
    catalog = bundle.quests.catalog
    context = ScoreContext(catalog, catalog.get(OPENER), catalog.mode(OPENER), SimpleNamespace(kills=100), record_row,
                           UNBOOSTED, NOW, OPEN_MISSION, ModeServices(subjugation=SubjugationCatalog(catalog)))
    with pytest.raises(ProtocolError) as error:
        POLICY.score_result(context)
    assert (error.value.code, error.value.status) == ("quest_state_invalid", 503)
