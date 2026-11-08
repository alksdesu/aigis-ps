from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from threading import Barrier, Lock
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

import aigis_ps.entrust_service as module
import aigis_ps.request_ledger as ledger
from aigis_ps import quest_modes
from aigis_ps.alxml import Column, Container, loads
from aigis_ps.battle_events import EVENT_PARAM_LIMIT, BattleEventGrant
from aigis_ps.crystal_spending import initialize_spending
from aigis_ps.drop_bonus import DropBonusTable
from aigis_ps.energy_clock import JST, energy_seconds, set_anchor
from aigis_ps.entrust_catalog import REQUIRED_MAX_RANK, EntrustCatalog
from aigis_ps.entrust_service import CHECK_ORDER, EntrustRules, EntrustService, Stage, read_history
from aigis_ps.errors import ProtocolError
from aigis_ps.event_mission_rewards import UNSUPPORTED, Family
from aigis_ps.growth_campaign import GrowthCampaign
from aigis_ps.history_disclosure import HISTORY_CATEGORY
from aigis_ps.mission_service import MissionService
from aigis_ps.mission_window_revival import MissionWindowRules, build_mission_window_revival
from aigis_ps.monthly_capacity import initialize_capacity
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.protocol import ProtocolRegistry, encode_wire, prepare_request
from aigis_ps.quest_catalog import QuestCatalog, Reward
from aigis_ps.quest_economy import MONEY_UP_RATE, STAR_GOLD_RATES, _ranks, boosted_experience
from aigis_ps.quest_modes import CLEAR_FLAG, SLOT_FLAGS, THREE_STAR_FLAG, EntrustDraw, EntrustRewards, SettleOutcome
from aigis_ps.quest_service import QuestService
from aigis_ps.rank_rewards import RankRewardService
from aigis_ps.storage import Store
from aigis_ps.vote_quest_tickets import DEPENDENCIES_UNAVAILABLE, SOURCE_INVALID
from aigis_ps.vote_rules import VoteRules
from aigis_ps.wallet_money import GOLD_LIMIT
from test_event_pass_service import item_catalogs
from test_history_disclosure import GROUP_CLOSED, disclose_state
from test_quest_catalog import ENTRY_FIELDS, SPECIAL_ENEMY, STAGE_VARIANT_ENEMY, archive, entry, table
from test_quest_service import (
    GOLDEN, GOLDEN_NOW, LOCAL_CLIENT_PLATFORM, GoldenCase, GoldenStep, GoldenStore, bundle, canonical,
    changed_sections, drained, finish_request, gold_team, golden_base, golden_bundle, golden_steps, money_campaign,
    recorded, recorded_differences, request, settled_state, start_request, tutorial_touched,
)
from test_vote_catalog import build as vote_catalog, vote_sources
from test_vote_quest_tickets import FIRST as VOTE_TICKET, REVIVAL, isolated, quest_tickets


NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
MONDAY_NOON, MONDAY_GRACE = datetime(2026, 11, 2, 12, tzinfo=JST), datetime(2026, 11, 2, 0, 3, tzinfo=JST)
CAMPAIGN_DAY = datetime(2026, 8, 21, 12, tzinfo=JST)
PAST_WINDOW = (datetime(2026, 1, 1, tzinfo=JST), datetime(2026, 1, 2, tzinfo=JST))
BOOST_OPEN, BOOST_CLOSED = "2026/11/01 21:00:01", "2026/11/01 21:00:00"
DEPARTURE_GRACE_SECONDS = 300
PLATFORM, OTHER_PLATFORM, INVALID_PLATFORM = 1, 12, 32
TICKET = "item_consignment_ticket"
STORY_TYPE, DAILY_TYPE, REPRODUCE_TYPE, HISTORY_TYPE, EMPIRE_TYPE = 0, 2, 8, 16, 18
STORY_QUEST, STORY_PREREQUISITE, STORY_MISSION = 1002, 1001, 100001
MODE_GATE = 1004
DAILY_QUEST, DAILY_MISSION = 101, 700101
LIMITED_DAILY, LIMITED_PREREQUISITE = 314, 212
GIFT_DAILY, GIFT_ITEM, GIFT_MISSION = 111, "gift_0000_bouquet", 700104
HISTORY_QUEST, HISTORY_PREREQUISITE, HISTORY_MISSION, TUTORIAL_MISSION = 4125, 4124, 920025, 900001
EMPIRE_QUEST, EMPIRE_PREREQUISITE, EMPIRE_KEY, EMPIRE_MENU_MISSION = 7424, 7423, 7325, 940012
REFUSED_QUEST = 2001
# 9032 is the reproduce quest History mission 920014 keeps open; every reproduce batch is closed by default.
REPRODUCE_QUEST, REPRODUCE_PREREQUISITE, REPRODUCE_MISSION = 9032, 4030, 920014
# 4807 (Gold Rush 13, mission 300130) adds its 7005 prize enemy at RT index 3 once the record holds three stars.
GOLD_RUSH_QUEST, GOLD_RUSH_BATCH, GOLD_RUSH_OVERRIDE, PRIZE_ENEMY_ATTR = 4807, "Reproduce:300084", 437, 2
OPENED = {"start_at": "2026-11-01T15:00:00+09:00", "end_at": "2026-11-16T10:59:59+09:00"}
GOLD_RUSH_RT = ((0, 2, 1), (1, 3, 1), (2, 1, 1), (3, 1, PRIZE_ENEMY_ATTR | 1), (4, 2, 1))
GOLD_RUSH_BELOW_THREE_RT = ((0, 2, 1), (1, 3, 1), (2, 1, 1), (3, 2, 1))
GOLD_RUSH_DROPS = {77: 2, 56: 1, 133: 1, GOLD_RUSH_OVERRIDE: 1}
# Quest 1002 rewards 4, 67, 68 and 15 from slots 1, 2, 3 and 5; a four-star clear lifts an eligible record to MaxRank 4.
RANKED_ROWS = (entry(1), entry(2), entry(3, enemy=STAGE_VARIANT_ENEMY), entry(5, condition="GetNum_QuestStar() == 3;"))
VARIANT_CODE, THREE_STAR_CODE, FOUR_STAR_RANK = 68, 15, 4
# History quest 11 drops one 5002 (three carved crystals) a run; DropBonusConfig scales it by 1.5 on Mondays.
BONUS_QUEST, CARVED_DROP, CARVED_PER_RUN = 11, 5002, 3
CARVED_CRYSTAL, CARVED_TYPE, BONUS_VALUE = "trade_item_002000", 1, 1.5
# Nine crystals of one request floor once to 13, not three times to 12.
BONUS_RUNS, BATCH_BONUS_CRYSTALS = 3, 13
# 3002 is an ItemDropConfig DropType 5 row worth three event points that no real quest lists.
EVENT_CODE, EVENT_POINTS = 3002, 3
PLANNED = [Stage.REQUEST, Stage.PRODUCT, Stage.ELIGIBILITY, Stage.ADMISSION, Stage.UNIT_SPACE, Stage.ENERGY,
           Stage.TICKET, Stage.CHARISMA_CAPACITY, Stage.STAMINA, Stage.CHARISMA]
MISSING_ITEM = "missing_item"
UNLISTED_PROVIDER_ERROR = "fixture_provider_fault"
HOOK_CHILD = Container("HOOK", (Column("A1", "I", (1,)),))
WRONG_RT = Container("RT", (Column("ID", "I", (0,)), Column("TYPE", "I", (1,)), Column("ATTR", "I", (0,))))
ENTRUST_GOLDEN = GOLDEN / "entrust.json"
LEGACY_ENTRUST_GOLDEN = GOLDEN / "legacy" / ENTRUST_GOLDEN.name
# Receipts digest catalog and rule versions other modules own, so recordings name the service's own version.
CONTENT_VERSION_TOKEN = "<content_version>"
# The public gold rates are the only recorded differences from the previous build.
ENTRUST_SETTLEMENT_CHANGES = frozenset({
    "state.entrust_history.receipts[].response.PGP.A1", "state.entrust_history.receipts[].response.REWARD.GOLD",
    "state.entrust_history.receipts[].response_digest", "state.entrust_history.receipts[].state",
    "state.tables.game_param[].Money",
    "steps[].response.PGP.A1", "steps[].response.REWARD.GOLD",
})


def entrust_gold(gold, *, money_up=False):
    # Every run pays the three-star rate of the clear it repeats; the starter roster carries no gold abilities.
    return int(gold * STAR_GOLD_RATES[REQUIRED_MAX_RANK] * (MONEY_UP_RATE if money_up else 1))


@pytest.fixture
def case(bundle, tmp_path):
    return entrust_case(bundle, tmp_path)


def entrust_case(bundle, tmp_path, quest_catalog=None, catalog=None):
    players, bootstrap = copy(bundle.player_state), copy(bundle.bootstrap)
    clock = SimpleNamespace(now=NOW)
    bootstrap.clock = lambda: clock.now
    quests = QuestService(quest_catalog or bundle.quests.catalog, bootstrap, players, bundle.possessions,
                          clock=lambda: clock.now, randbelow=lambda _: 0)
    if catalog is None:
        shared = quest_catalog is None and getattr(bundle, "entrust", None)
        catalog = bundle.entrust.catalog if shared else EntrustCatalog(quests.catalog)
    service = EntrustService(quests, catalog, clock=lambda: clock.now, randbelow=lambda _: 0)
    state = players.initialize(bootstrap.initial_state())
    state["tables"]["game_param"][0]["NowCharisma"] = 10000
    set_anchor(state["tables"]["game_param"][0], energy_seconds(NOW))
    state["retained_progress"] = {"nested": [1, 2]}
    state["tables"]["quest_records"] = [eligible_record(catalog.product(qid).quest) for qid in (1001, 1002)]
    path = tmp_path / "entrust.sqlite3"
    return SimpleNamespace(bundle=bundle, players=players, bootstrap=bootstrap, quests=quests, catalog=catalog,
        service=service, clock=clock, path=path, store=Store(path, service.initialize(state)))


def rebuild(case, **options):
    case.service = EntrustService(case.quests, case.catalog, clock=lambda: case.clock.now, randbelow=lambda _: 0,
                                  **options)
    return case.service


def eligible_record(quest):
    flags = CLEAR_FLAG | THREE_STAR_FLAG | 0x40000000
    flags |= sum(bit for code, bit in zip(quest.treasures, SLOT_FLAGS) if code)
    return {"QuestID": quest.quest_id, "MaxRank": REQUIRED_MAX_RANK, "AchieveFlag": flags,
            "RemainAssault": 0, "FirstDefeatDate": 12345, "Shown": True}


def cleared(quest_id):
    return {"QuestID": quest_id, "MaxRank": 3, "AchieveFlag": CLEAR_FLAG | THREE_STAR_FLAG,
            "RemainAssault": 0, "FirstDefeatDate": 12345, "Shown": True}


def message(case, qid=1002, quantity=1, *, tick=1000, extra=(), revision=None):
    fields = (Column("QI", "I", (qid,)), Column("Rev", "S", (revision or case.quests.catalog.resource_version,)))
    if quantity is not None:
        fields += (Container("Boost", (Column("V", "I", (quantity,)),)),)
    return request(case.store, 76, fields + extra, tick=tick)


def on_platform(envelope, code):
    # 76 requests carry a checksum, so the edited copy has to be signed again.
    document = ET.fromstring(envelope.payload_xml())
    document.find("PlatformCode/V").text = str(code)
    xml = prepare_request(ET.tostring(document, encoding="utf-8", short_empty_elements=False), envelope.profile.st)
    return ProtocolRegistry.load().decode(envelope.profile.route, encode_wire(xml, envelope.profile.request_key))


def mutate(case, action):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    action(state)
    case.store.update_state(state, profile.revision)


def overwrite(case, action):
    # The saved row keeps its revision, so the receipt committed at that revision can still replay.
    state = deepcopy(case.store.profile().state)
    action(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def restamp(case, content):
    overwrite(case, lambda state: state["entrust_history"]["receipts"][-1].update(content=content))


def forge_content(case):
    restamp(case, "f" * 64)


def set_rank(case, state, rank):
    state["tables"]["game_param"][0].update(
        Rank=rank, Experience=sum(row["NextExp"] for row in case.bootstrap.rank_rows[:rank - 1]))


def hold(state, item_id, count):
    rows = [row for row in state["tables"]["possessions"] if row["ItemID"] != item_id]
    state["tables"]["possessions"] = rows + ([{"ItemID": item_id, "Count": count}] if count else [])


def held(state, item_id):
    return next((row["Count"] for row in state["tables"]["possessions"] if row["ItemID"] == item_id), 0)


def history_missions(catalog, quest_id):
    windows = catalog.mission_windows(catalog.quest_type(quest_id))
    return tuple(mission.mission_id for mission in windows.missions(quest_id) if mission.category == HISTORY_CATEGORY)


def disclosed(state, catalog, quest_id):
    # A History menu offers its quests only once the mission is disclosed on the saved login day.
    for mission_id in history_missions(catalog, quest_id):
        disclose_state(state, catalog, mission_id)
    return state


def prepare(case, quest_id, *, cleared_ids=(), tickets=0, disclose=True):
    quest = case.catalog.product(quest_id).quest
    def change(state):
        if disclose:
            disclosed(state, case.quests.catalog, quest_id)
        records = {row["QuestID"]: row for row in state["tables"]["quest_records"]}
        records.update((identifier, cleared(identifier)) for identifier in cleared_ids)
        records[quest_id] = eligible_record(quest)
        state["tables"]["quest_records"] = list(records.values())
        set_rank(case, state, quest.rank)
        hold(state, TICKET, tickets)
    mutate(case, change)


def item_grants(case, product, runs=1):
    grants = Counter()
    for code in product.reward_codes(REQUIRED_MAX_RANK):
        reward = case.quests.catalog.decode_reward(code)
        if reward.kind == "item":
            grants[reward.item_id] += reward.count * runs
    return grants


def unit_grants(case, product, runs=1):
    grants = Counter()
    for code in product.reward_codes(REQUIRED_MAX_RANK):
        reward = case.quests.catalog.decode_reward(code)
        if reward.kind == "unit":
            grants[reward.card_id] += reward.count * runs
    return grants


def nodes(xml):
    return {node.name: node for node in loads(xml).children}


def possession_counts(result):
    columns = {column.name: column.values for column in result["POSSESSION"].children}
    return dict(zip(columns["ItemID"], columns["Count"]))


def fail(case, envelope, code=None):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.execute(envelope, case.store)
    assert case.store.profile() == before
    if code is not None:
        assert caught.value.code == code
    return caught.value


def rt_rows(rows):
    return Container("RT", tuple(Column(name, "I", tuple(row[index] for row in rows))
                                 for index, name in enumerate(("ID", "TYPE", "ATTR"))))


def rt(case, qid=1002, attributes=None, *, max_rank=REQUIRED_MAX_RANK):
    candidates = case.catalog.product(qid).quest.candidates_for(max_rank)
    attributes = [1 | candidate.special_attr for candidate in candidates] if attributes is None else attributes
    return rt_rows([(index, candidate.slot, attribute)
                    for index, (candidate, attribute) in enumerate(zip(candidates, attributes, strict=True))])


def treasure_info(result):
    columns = result["TreasureInfo"].children
    return dict(zip(columns[1].values, columns[2].values))


def bonus_values(result):
    return tuple(column.values[0] for column in result["BONUS"].children)


def game_params(result):
    keys, values = (column.values for column in result["PGP"].children)
    return dict(zip(keys, values))


def slotted_catalog(bundle, rows, quest_id=1002):
    original = bundle.quests.catalog
    quest = original.get(quest_id)
    map_name, entry_name = f"Map{quest.map_no:04d}.aar", f"Entry{quest.entry_no:02d}.atb"
    assert [qid for qid in original.all_ids if original.get(qid).map_no == quest.map_no] == [quest_id]
    replacement = archive([(entry_name, table(ENTRY_FIELDS, list(rows)))])
    reader = lambda name: replacement if name == map_name else original._asset_reader(name)
    return QuestCatalog(original.snapshot, reader, possessions=bundle.possessions, resource_version=original.resource_version)


def closed_catalog(bundle, overrides):
    original = bundle.quests.catalog
    return QuestCatalog(original.snapshot, original._asset_reader, possessions=bundle.possessions,
                        resource_version=original.resource_version, window_overrides=overrides)


def reopened_catalog(bundle, *batches):
    original = bundle.quests.catalog
    base = original._asset_reader
    revival = build_mission_window_revival(base, MissionWindowRules(True, {batch: OPENED for batch in batches}))
    reader = lambda name: revival.replacements[name] if name in revival.replacements else base(name)
    return QuestCatalog(original.snapshot, reader, possessions=bundle.possessions,
                        resource_version=original.resource_version)


def entrusting(quests):
    return SimpleNamespace(quests=quests, catalog=EntrustCatalog(quests))


@pytest.fixture(scope="module")
def gold_rush(bundle):
    return entrusting(reopened_catalog(bundle, GOLD_RUSH_BATCH))


@pytest.fixture(scope="module")
def ranked(bundle):
    return entrusting(slotted_catalog(bundle, RANKED_ROWS))


@pytest.fixture(scope="module")
def event_drop(bundle):
    original = bundle.quests.catalog
    quests = QuestCatalog(original.snapshot, original._asset_reader, possessions=bundle.possessions,
                          resource_version=original.resource_version)
    plain, story = quests.get, quests.get(STORY_QUEST)
    rewarding = replace(story, treasures=(*story.treasures[:2], EVENT_CODE, *story.treasures[3:]))
    # Every catalog lookup goes through get, so quest 1002 answers with the event drop as its slot 3 treasure.
    quests.get = lambda quest_id: rewarding if quest_id == STORY_QUEST else plain(quest_id)
    return entrusting(quests)


@pytest.fixture(scope="module")
def ticket_provider(bundle, vote_sources):
    return quest_tickets(isolated(bundle), vote_catalog(vote_sources, VoteRules(revival_start=REVIVAL)), vote_sources)


def with_tickets(case, provider):
    case.quests.event_rewards.attach_vote_tickets(provider)
    return rebuild(case)


class RecordedMissions:
    content_version = "entrust-mission-fixture"

    def __init__(self, players, bootstrap):
        self.players, self.bootstrap, self.events = players, bootstrap, []

    def _ready(self):
        return None

    def replayable_content_versions(self):
        return frozenset((self.content_version,))

    def record(self, state, kind, payload, now):
        self.events.append((kind, payload))
        return state

    def preflight_snapshot(self, state, now, profile=None):
        return None


def recording(case):
    missions = RecordedMissions(case.players, case.bootstrap)
    case.quests.missions = missions
    rebuild(case)
    return missions


def test_initializer_preserves_existing_state_and_cli_registers_76(case):
    before = case.store.profile().state
    original = deepcopy(before)
    assert case.service.initialize(before) == original and before == original
    assert "entrust_history" not in before
    assert set(case.service.handlers()) == {76}
    assert case.service.status()["ready"] is True
    assert case.bundle.entrust is not None
    assert 76 in case.bundle.entrust.handlers()


@pytest.mark.parametrize("quantity", [None, 1, 2, 3])
def test_native_batch_costs_rewards_and_units_without_first_rewards(case, quantity):
    before = case.store.profile()
    quest = case.catalog.product(1002).quest
    count = quantity or 1
    result = nodes(case.service.execute(message(case, quantity=quantity), case.store))
    after = case.store.profile()
    old, current = (profile.state["tables"]["game_param"][0] for profile in (before, after))
    assert after.revision == before.revision + 1
    assert current["NowCharisma"] == old["NowCharisma"] - quest.charisma * count
    assert current["NowActionPoint"] == old["NowActionPoint"] - quest.action_points * count
    assert current["Experience"] == old["Experience"] + quest.rank_exp * count
    assert current["Money"] == old["Money"] + entrust_gold(quest.gold) * count
    for table in ("quest_records", "crystals", "config", "possessions"):
        assert after.state["tables"][table] == before.state["tables"][table]
    assert "quest_state" not in after.state and "PFC" not in result and "QR" not in result
    assert after.state["retained_progress"] == before.state["retained_progress"]
    units = after.state["player_state"]["units"][len(before.state["player_state"]["units"]):]
    assert len(units) == 3 * count
    assert [unit["CardID"] for unit in units] == [card for card in (4, 67, 68) for _ in range(count)]
    assert len({unit["UniqueID"] for unit in units}) == len(units)
    for unit in units:
        assert unit["Exp"] == case.players.experience_for_level(unit["CardID"], quest.unit_level)
    assert result["TreasureInfo"].children == (Column("Index", "I", (0, 1, 2)),
        Column("DropID", "I", (4, 67, 68)), Column("Count", "I", (count,) * 3))
    assert result["REWARD"].children == (Column("EXPERIENCE", "I", (quest.rank_exp * count,)),
                                          Column("GOLD", "I", (entrust_gold(quest.gold) * count,)))
    assert result["PBP"].children[1].values == (after.state["player_state"]["battle"]["UX"],)
    assert result["PPU"].children[0].values == tuple(unit["CardID"] for unit in units)
    assert len(result["PPU"].children) == 21
    assert "LOTTERY" not in result and not result["TREASURE"].children
    assert "POSSESSION" not in result and "IOF" not in result


@pytest.mark.parametrize("quantity", [0, -1, 4, 0x7FFFFFFF])
def test_native_count_bounds_reject_without_write(case, quantity):
    fail(case, message(case, quantity=quantity), "entrust_request_invalid")


@pytest.mark.parametrize("damage", ["rank", "slot", "missing_record", "duplicate_record"])
def test_eligibility_requires_three_stars_and_every_treasure_slot(case, damage):
    def damage_record(state):
        records = state["tables"]["quest_records"]
        record = next(row for row in records if row["QuestID"] == 1002)
        if damage == "rank":
            record["MaxRank"] = 2
        elif damage == "slot":
            record["AchieveFlag"] &= ~1
        elif damage == "missing_record":
            records.remove(record)
        else:
            records.append(deepcopy(record))
    mutate(case, damage_record)
    fail(case, message(case), "entrust_state_invalid" if damage == "duplicate_record" else "entrust_not_eligible")


def test_resource_revision_and_unknown_quest_reject_without_write(case):
    fail(case, message(case, revision="unverified"), "entrust_resource_mismatch")
    assert fail(case, message(case, qid=0x7FFFFFFF), "quest_not_found").status == 404


def test_optional_native_candidates_and_team_fields_are_accepted(case):
    result = case.service.execute(message(case, extra=(rt(case), Container("BL"),
        Container("GG", (Column("I", "I", (0,)),)))), case.store)
    assert nodes(result)["TreasureInfo"].children[2].values == (1, 1, 1)


def test_mismatched_native_candidates_reject_before_payment(case):
    fail(case, message(case, extra=(WRONG_RT,)), "quest_candidates_mismatch")


def test_entrust_admits_through_the_shared_battle_entry_before_its_candidates(case):
    mutate(case, lambda state: state["tables"]["quest_records"].remove(
        next(row for row in state["tables"]["quest_records"] if row["QuestID"] == STORY_PREREQUISITE)))
    error = fail(case, message(case, extra=(WRONG_RT,)), "quest_locked")
    assert str(error) == "The prerequisite quest has not been cleared"
    preview = case.service.preview(case.store.profile(), STORY_QUEST, platform_code=PLATFORM)
    assert (preview["code"], preview["stage"], preview["mission_id"]) == ("quest_locked", Stage.ADMISSION, None)


def unsettled_families(case, monkeypatch, family):
    decided = []

    def families(quest_id, mission):
        decided.append((quest_id, mission.mission_id))
        return frozenset((family,))

    monkeypatch.setattr(case.quests.event_missions, "families", families)
    return decided


def test_entrust_refuses_unsettled_event_mission_rewards_at_admission_before_charging(case, monkeypatch):
    prepare(case, HISTORY_QUEST, cleared_ids=(MODE_GATE, HISTORY_PREREQUISITE), tickets=1)
    decided = unsettled_families(case, monkeypatch, Family.AFTER_EVENT)
    error = fail(case, message(case, HISTORY_QUEST), UNSUPPORTED)
    assert (str(error), error.status) == (
        f"Quest {HISTORY_QUEST} shows after_event event rewards in mission {HISTORY_MISSION} that no mode settles", 501)
    preview = case.service.preview(case.store.profile(), HISTORY_QUEST, platform_code=PLATFORM)
    assert (preview["code"], preview["status"], preview["stage"], preview["mission_id"], preview["ticket"]["cost"]) == (
        UNSUPPORTED, 501, Stage.ADMISSION, HISTORY_MISSION, None)
    assert decided == [(HISTORY_QUEST, HISTORY_MISSION)] * 2 and held(case.store.profile().state, TICKET) == 1


def test_entrust_and_battle_starts_reach_the_same_event_mission_verdict(case, monkeypatch):
    decided = unsettled_families(case, monkeypatch, Family.EMERGENCY)
    entrusted = fail(case, message(case), UNSUPPORTED)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as started:
        case.quests.start(start_request(case.quests, case.store, STORY_QUEST), case.store)
    verdict = (f"Quest {STORY_QUEST} shows emergency event rewards in mission {STORY_MISSION} that no mode settles", 501)
    assert (str(entrusted), entrusted.status) == (str(started.value), started.value.status) == verdict
    assert decided == [(STORY_QUEST, STORY_MISSION)] * 2 and case.store.profile() == before


def test_entrust_requires_the_quest_rank_before_its_mission_rank(case):
    prepare(case, DAILY_QUEST, tickets=1)
    quest = case.catalog.product(DAILY_QUEST).quest
    assert case.quests.catalog.mission_windows(DAILY_TYPE).minimum_rank(DAILY_MISSION) == quest.rank
    mutate(case, lambda state: set_rank(case, state, quest.rank - 1))
    error = fail(case, message(case, DAILY_QUEST), "quest_rank_required")
    assert str(error) == "Player rank is below the quest requirement"


def test_weekday_entrust_needs_no_scripted_gate_quest(case):
    prepare(case, DAILY_QUEST, tickets=1)
    case.service.execute(message(case, DAILY_QUEST), case.store)
    assert held(case.store.profile().state, TICKET) == 0


def test_prize_slots_past_treasure5_and_on_empty_treasures_roll_and_grant_nothing(bundle, tmp_path):
    case = entrust_case(bundle, tmp_path, slotted_catalog(bundle, [entry(slot) for slot in (1, 4, 8, 5)]))
    quest = case.catalog.product(1002).quest
    assert [candidate.slot for candidate in quest.candidates] == [1, 4, 8, 5]
    assert quest.treasures[3] == 0 and quest.treasures[0] and quest.treasures[4]
    granted = (quest.treasures[0], quest.treasures[4])
    rewards = [case.quests.catalog.decode_reward(code) for code in granted]
    assert all(reward.kind == "unit" for reward in rewards)
    # Like a battle start, the empty slot 4 reports its unset record bit and slot 8 the nonzero flag word.
    fail(case, message(case, quantity=2, extra=(rt(case, attributes=(1, 1, 1, 1)),)), "quest_candidates_mismatch")
    rolls = []
    case.service.randbelow = lambda _: rolls.append(0) or 0
    before = case.store.profile()
    result = nodes(case.service.execute(message(case, quantity=2, extra=(rt(case, attributes=(1, 0, 1, 1)),)), case.store))
    after = case.store.profile()
    assert len(rolls) == 2 * len(granted)
    assert result["TreasureInfo"].children == (Column("Index", "I", (0, 1)), Column("DropID", "I", granted),
                                               Column("Count", "I", (2, 2)))
    units = after.state["player_state"]["units"][len(before.state["player_state"]["units"]):]
    assert [unit["CardID"] for unit in units] == [reward.card_id for reward in rewards for _ in range(2)]


def test_prize_enemy_attributes_join_the_saved_slot_bits_in_entrust_candidates(bundle, tmp_path):
    case = entrust_case(bundle, tmp_path, slotted_catalog(bundle, (entry(1, enemy=SPECIAL_ENEMY), entry(2), entry(3))))
    quest = case.catalog.product(1002).quest
    assert [(candidate.slot, candidate.special_attr) for candidate in quest.candidates] == [(1, 2), (2, 0), (3, 0)]
    fail(case, message(case, extra=(rt(case, attributes=(1, 1, 1)),)), "quest_candidates_mismatch")
    result = nodes(case.service.execute(message(case, extra=(rt(case, attributes=(3, 1, 1)),)), case.store))
    assert result["TreasureInfo"].children[1].values == quest.treasures[:3]


def test_whole_batch_requires_energy_before_rank_up_refill(case):
    threshold = case.bootstrap.rank_rows[0]["NextExp"]
    mutate(case, lambda state: state["tables"]["game_param"][0].update(
        NowCharisma=case.catalog.product(1002).quest.charisma, Experience=threshold - 1))
    fail(case, message(case, quantity=3), "quest_energy_insufficient")


def test_rank_up_refills_after_total_cost_and_grants_all_experience(case):
    quest = case.catalog.product(1002).quest
    threshold = case.bootstrap.rank_rows[0]["NextExp"]
    mutate(case, lambda state: state["tables"]["game_param"][0].update(
        NowCharisma=quest.charisma * 3, Experience=threshold - 1))
    case.service.execute(message(case, quantity=3), case.store)
    saved = case.store.profile().state["tables"]["game_param"][0]
    assert saved["Experience"] == threshold - 1 + quest.rank_exp * 3
    assert saved["Rank"] > 1
    assert saved["NowCharisma"] == case.bootstrap.rank_rows[saved["Rank"] - 1]["CharismaMax"]


@pytest.mark.parametrize("base_capacity,extension,allowed", [(5, 0, False), (6, 0, True), (5, 1, True)])
def test_single_run_capacity_includes_monthly_extension_but_not_total_batch(case, base_capacity, extension, allowed):
    case.bootstrap.rank_rows = deepcopy(case.bootstrap.rank_rows)
    case.bootstrap.rank_rows[0]["CharismaMax"] = base_capacity
    profile = case.store.profile()
    state = initialize_capacity(initialize_spending(profile.state, NOW, origin="new_local"))
    state["monthly_capacity"]["current"] = extension
    state["tables"]["game_param"][0]["ExtendedMaxCharisma"] = extension
    case.store.update_state(state, profile.revision)
    case.service = EntrustService(case.quests, case.catalog, clock=lambda: NOW, randbelow=lambda _: 0)
    quest = case.catalog.product(1002).quest
    assert quest.charisma == 6 and quest.charisma * 3 > base_capacity + extension
    if not allowed:
        fail(case, message(case, quantity=3), "entrust_energy_capacity")
        return
    case.service.execute(message(case, quantity=3), case.store)
    saved = case.store.profile().state
    assert saved["tables"]["game_param"][0]["NowCharisma"] == 10000 - 18
    assert saved["monthly_capacity"]["current"] == extension
    assert len(saved["player_state"]["units"]) == 10


@pytest.mark.parametrize("reward_kind", ["unit", "item"])
def test_drop_display_multiplicity_is_separate_from_resource_grant_count(case, monkeypatch, reward_kind):
    product = case.catalog.product(1002)
    original = case.quests.catalog.decode_reward
    item = next(name for name, stack in case.service.possessions._definitions.items() if stack >= 12)
    replacement = (replace(original(4), count=4) if reward_kind == "unit" else
                   Reward("item", 4, 4, item_id=item))
    monkeypatch.setattr(case.quests.catalog, "decode_reward", lambda code: replacement if code == 4 else original(code))
    before = case.store.profile()
    original_state = deepcopy(before.state)
    counts = case.service._drops(product.reward_codes(REQUIRED_MAX_RANK), 3)
    assert counts == {4: 3, 67: 3, 68: 3}
    draw = case.service._draw(product, counts)
    updated, units, discarded = case.service._grant(before.state, draw.units, draw.items)
    result = nodes(case.service._response(before, updated, units=units, counts=draw.counts, item_ids=tuple(draw.items),
                                          discarded=discarded, mail_ids=(), event_param=False, children=()))
    assert result["TreasureInfo"].children == (Column("Index", "I", (0, 1, 2)),
        Column("DropID", "I", (4, 67, 68)), Column("Count", "I", (3, 3, 3)))
    assert discarded == {}
    if reward_kind == "unit":
        assert sum(unit["CardID"] == 4 for unit in units) == 12
        assert len(units) == 18 and tuple(draw.items) == ()
    else:
        assert len(units) == 6 and tuple(draw.items) == (item,)
        assert next(row["Count"] for row in updated["tables"]["possessions"] if row["ItemID"] == item) == 12
        assert result["POSSESSION"].children[1].values == (12,)
    assert before.state == original_state and case.store.profile() == before


def test_one_free_business_slot_allows_native_multi_unit_reward(case):
    case.players.capacity_provider = lambda _: 1
    case.service.execute(message(case, quantity=3), case.store)
    assert len(case.store.profile().state["player_state"]["units"]) == 10


def test_a_full_barracks_rolls_back_the_batch(case):
    case.players.capacity_provider = lambda _: 0
    fail(case, message(case), "unit_roster_capacity")


@pytest.mark.parametrize("money,credited", [(GOLD_LIMIT - 1, 1), (GOLD_LIMIT, 0), (0x7FFFFFFF, 0)])
def test_gold_past_the_cap_is_discarded_while_the_batch_still_credits_experience(case, money, credited):
    quest, runs = case.catalog.product(STORY_QUEST).quest, 3
    mutate(case, lambda state: state["tables"]["game_param"][0].update(Money=money))
    before = case.store.profile().state["tables"]["game_param"][0]
    result = nodes(case.service.execute(message(case, quantity=runs), case.store))
    after = case.store.profile().state["tables"]["game_param"][0]
    assert entrust_gold(quest.gold) * runs > credited
    assert after["Money"] == money + credited and after["Experience"] == before["Experience"] + quest.rank_exp * runs
    assert result["REWARD"].children == (Column("EXPERIENCE", "I", (quest.rank_exp * runs,)),
                                          Column("GOLD", "I", (credited,)))


def test_batch_totals_past_int32_settle_like_the_last_rank_and_the_gold_cap(case, monkeypatch):
    for reward in ("experience_reward", "gold_reward"):
        monkeypatch.setattr(case.quests, reward, lambda *args, **options: INT32_MAX)
    before = case.store.profile().state["tables"]["game_param"][0]
    result = nodes(case.service.execute(message(case, quantity=3), case.store))
    after = case.store.profile().state["tables"]["game_param"][0]
    thresholds, _ = _ranks(case.bootstrap.rank_rows, after)
    assert (after["Money"], after["Experience"]) == (GOLD_LIMIT, thresholds[-1] - 1)
    assert result["REWARD"].children == (Column("EXPERIENCE", "I", (after["Experience"] - before["Experience"],)),
                                          Column("GOLD", "I", (GOLD_LIMIT - before["Money"],)))


def test_entrust_keeps_active_battle_and_original_definition_settles(case):
    case.quests.start(start_request(case.quests, case.store, 1002, tick=900), case.store)
    before = case.store.profile().state["quest_state"]
    case.service.execute(message(case, quantity=2), case.store)
    assert case.store.profile().state["quest_state"] == before
    case.quests.finish(finish_request(case.store, 1002, tick=1001, obtained=7, selected=7), case.store)
    saved = case.store.profile().state
    assert saved["quest_state"]["active"] is None
    assert len(saved["player_state"]["units"]) == 10


def test_legacy_active_without_reservations_requires_explicit_resolution(case):
    case.quests.start(start_request(case.quests, case.store, 1002, tick=900), case.store)
    mutate(case, lambda state: state["quest_state"]["active"].pop("unit_reservations"))
    fail(case, message(case), "quest_reservation_unavailable")


def test_active_reserved_identity_space_cannot_be_consumed(case):
    case.quests.start(start_request(case.quests, case.store, 1002, tick=900), case.store)
    mutate(case, lambda state: state["player_state"].update(last_unique_id=0x7FFFFFFF - 4))
    fail(case, message(case), "unit_identity_exhausted")


def test_zero_drop_rule_returns_explicit_empty_native_result(case):
    case.quests.rules = replace(case.quests.rules, drop_basis_points=0)
    case.service = EntrustService(case.quests, case.catalog, clock=lambda: NOW, randbelow=lambda _: 0)
    before = case.store.profile().state["player_state"]
    result = nodes(case.service.execute(message(case, quantity=3), case.store))
    assert case.store.profile().state["player_state"] == before
    assert result["TreasureInfo"].children == (Column("Index", "I", ()), Column("DropID", "I", ()), Column("Count", "I", ()))
    assert all(not column.values for column in result["PPU"].children)


@pytest.mark.parametrize("roll", [-1, 10000, True, "0"])
def test_invalid_random_provider_never_commits(case, roll):
    case.service.randbelow = lambda _: roll
    fail(case, message(case), "entrust_random_invalid")


def test_exact_retry_survives_store_reopen_without_clock_or_random(case):
    envelope = message(case, quantity=3)
    response = case.service.execute(envelope, case.store)
    saved = case.store.profile()
    case.store = Store(case.path)
    case.service.clock = lambda: pytest.fail("exact retry read the clock")
    case.service.randbelow = lambda _: pytest.fail("exact retry rerolled rewards")
    assert case.service.execute(envelope, case.store) == response
    assert case.store.profile() == saved


def test_retry_rejects_intervening_profile_changes(case):
    envelope = message(case)
    case.service.execute(envelope, case.store)
    mutate(case, lambda state: state["retained_progress"]["nested"].append(3))
    fail(case, envelope, "entrust_retry_stale")


def test_client_clock_rollback_accepts_new_entrusts_and_keeps_exact_replays_stale(case):
    first = message(case, quantity=1, tick=0x7FFFFFFF)
    case.service.execute(first, case.store)
    for quantity, tick in ((2, 0x7FFFFFFF), (1, -0x80000000), (2, 1000)):
        case.service.execute(message(case, quantity=quantity, tick=tick), case.store)
    fail(case, first, "entrust_retry_stale")
    history = case.store.profile().state["entrust_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION and len(history["processed_keys"]) == 4
    assert all("client_time" not in receipt for receipt in history["receipts"])


def test_an_evicted_receipt_keeps_its_request_processed(case, monkeypatch):
    monkeypatch.setattr(ledger, "HISTORY_LIMIT", 1)
    first = message(case, quantity=1)
    case.service.execute(first, case.store)
    case.service.execute(message(case, quantity=2), case.store)
    history = case.store.profile().state["entrust_history"]
    assert (len(history["receipts"]), len(history["processed_keys"])) == (1, 2)
    assert fail(case, first, "entrust_request_stale").status == 409


def test_read_history_publishes_receipt_copies_bounded_by_the_reader_revision(case):
    empty = read_history(case.store.profile().state)
    assert empty == {"schema_version": ledger.SCHEMA_VERSION, "receipts": [], "processed_keys": []}
    case.service.execute(message(case, quantity=2), case.store)
    profile = case.store.profile()
    history = read_history(profile.state)
    assert [(receipt["quest_id"], receipt["quantity"], receipt["revision"]) for receipt in history["receipts"]] == [
        (STORY_QUEST, 2, profile.revision)]
    history["receipts"].clear()
    assert read_history(profile.state)["receipts"] and profile.state["entrust_history"]["receipts"]
    with pytest.raises(ProtocolError) as error:
        read_history(profile.state, profile.revision - 1)
    assert (error.value.code, error.value.status) == ("entrust_history_invalid", 503)


def test_watermark_history_upgrades_and_replays_its_receipt_exactly(case):
    stage = CHECK_ORDER.index(Stage.PROCESSED)
    assert module.LEGACY_CHECK_ORDER == (*CHECK_ORDER[:stage], "watermark", *CHECK_ORDER[stage + 1:])
    envelope = message(case, quantity=2, tick=2000)
    response = case.service.execute(envelope, case.store)
    legacy_content = case.service.legacy_content_version
    def legacy(state):
        history = state["entrust_history"]
        state["entrust_history"] = {"schema_version": ledger.LEGACY_SCHEMA_VERSION, "last_client_time": 2000,
            "seen_keys": list(history["processed_keys"]),
            "receipts": [{**receipt, "content": legacy_content, "client_time": 2000} for receipt in history["receipts"]]}
    overwrite(case, legacy)
    saved = case.store.profile()
    assert case.service.execute(envelope, case.store) == response and case.store.profile() == saved
    case.service.execute(message(case, tick=1000), case.store)
    history = case.store.profile().state["entrust_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION and len(history["processed_keys"]) == 2
    assert [receipt["content"] for receipt in history["receipts"]] == [legacy_content, case.service.content_version]
    assert all("client_time" not in receipt for receipt in history["receipts"])


@pytest.mark.parametrize("damage", ["processed", "quantity", "xml", "digest", "state", "revision", "schema"])
def test_corrupt_history_cannot_replay_or_regenerate_rewards(case, damage):
    envelope = message(case)
    case.service.execute(envelope, case.store)
    def corrupt(state):
        history = state["entrust_history"]
        receipt = history["receipts"][-1]
        receipt["revision"] += 1
        if damage == "processed":
            history["processed_keys"].clear()
        elif damage == "quantity":
            receipt["quantity"] = 2
        elif damage == "xml":
            # A matching digest leaves the stored reply's XML check as the only refusal.
            receipt["response"] = "<DA>"
            receipt["response_digest"] = hashlib.sha256(receipt["response"].encode()).hexdigest()
        elif damage == "digest":
            receipt["response_digest"] = "0" * 64
        elif damage == "state":
            receipt["state"] = "0" * 64
        elif damage == "schema":
            history["schema_version"] = ledger.LEGACY_SCHEMA_VERSION
        else:
            receipt["revision"] += 10
    mutate(case, corrupt)
    fail(case, envelope, "entrust_history_invalid")


def test_dependency_changes_reject_new_and_cached_requests(case):
    envelope = message(case)
    case.service.execute(envelope, case.store)
    case.quests.rules = replace(case.quests.rules, gold_percent=200)
    fail(case, envelope, "entrust_dependencies_changed")
    fail(case, message(case, tick=1001), "entrust_dependencies_changed")


def test_entrust_rule_changes_make_earlier_receipts_stale(case):
    envelope = message(case)
    case.service.execute(envelope, case.store)
    current = case.service.content_version
    assert rebuild(case, rules=EntrustRules(experience_up=False)).content_version != current
    fail(case, envelope, "entrust_retry_stale")
    assert rebuild(case).content_version == current


def test_battle_event_and_drop_bonus_sources_feed_the_content_version(case, ticket_provider, monkeypatch):
    envelope = message(case)
    response = case.service.execute(envelope, case.store)
    saved, detached = case.store.profile(), case.service.content_version
    case.quests.event_rewards.attach_vote_tickets(ticket_provider)
    fail(case, envelope, "entrust_dependencies_changed")
    attached = rebuild(case).content_version
    assert attached != detached and case.service.status()["reward_sources"]["event_rewards"] == (
        case.quests.event_rewards.content_version)
    assert case.service.execute(envelope, case.store) == response and case.store.profile() == saved
    forge_content(case)
    fail(case, envelope, "entrust_retry_stale")
    monkeypatch.setattr(case.quests.catalog, "drop_bonus_table",
                        lambda: DropBonusTable.unavailable("fixture_table_fault", "Fixture table fault"))
    fail(case, envelope, "entrust_dependencies_changed")
    assert rebuild(case).content_version not in (detached, attached)


def committed_without_tickets(case, error):
    case.quests.event_rewards.attach_vote_tickets(None, error)
    envelope = message(case)
    return envelope, rebuild(case).execute(envelope, case.store), case.store.profile()


@pytest.mark.parametrize("error", [DEPENDENCIES_UNAVAILABLE, SOURCE_INVALID])
def test_receipts_of_a_failed_ticket_provider_setup_replay_once_it_attaches(case, ticket_provider, error):
    envelope, response, saved = committed_without_tickets(case, error)
    with_tickets(case, ticket_provider)
    assert case.service.execute(envelope, case.store) == response and case.store.profile() == saved
    forge_content(case)
    fail(case, envelope, "entrust_retry_stale")


def test_receipts_of_an_unlisted_ticket_provider_failure_stay_stale(case, ticket_provider):
    envelope, _, _ = committed_without_tickets(case, UNLISTED_PROVIDER_ERROR)
    with_tickets(case, ticket_provider)
    fail(case, envelope, "entrust_retry_stale")


def test_watermark_receipt_of_an_unattached_ticket_provider_replays_once_it_attaches(case, ticket_provider):
    envelope = message(case)
    response = case.service.execute(envelope, case.store)
    restamp(case, case.service.legacy_content_version)
    saved = case.store.profile()
    with_tickets(case, ticket_provider)
    assert case.service.execute(envelope, case.store) == response and case.store.profile() == saved


def mission_service(case, **options):
    return MissionService(case.bootstrap, case.players, case.bundle.presents, case.bundle.missions.catalog, **options)


def test_receipt_replays_after_mission_skins_attach(case):
    case.quests.missions = mission_service(case)
    envelope = message(case)
    response = rebuild(case).execute(envelope, case.store)
    saved, earlier = case.store.profile(), case.service.content_version
    case.quests.missions = mission_service(case, skins=case.bundle.missions.skins)
    assert rebuild(case).content_version != earlier
    assert case.service.execute(envelope, case.store) == response
    assert case.store.profile() == saved
    forge_content(case)
    fail(case, envelope, "entrust_retry_stale")


def test_receipt_of_removed_mission_skins_is_stale(case):
    case.quests.missions = mission_service(case, skins=case.bundle.missions.skins)
    envelope = message(case)
    rebuild(case).execute(envelope, case.store)
    case.quests.missions = mission_service(case)
    rebuild(case)
    fail(case, envelope, "entrust_retry_stale")


def rank_reward_service(case, item_catalog):
    presents = copy(case.bundle.presents)
    presents.item_catalog = item_catalog
    return RankRewardService(case.bootstrap, case.players, presents, case.bundle.rank_rewards.catalog)


def test_receipt_replays_after_emblem_mail_reaches_rank_rewards(case, item_catalogs):
    case.quests.rank_rewards = rank_reward_service(case, item_catalogs.plain)
    envelope = message(case)
    response = rebuild(case).execute(envelope, case.store)
    saved, earlier = case.store.profile(), case.service.content_version
    case.quests.rank_rewards = rank_reward_service(case, item_catalogs.mailed)
    assert rebuild(case).content_version != earlier
    assert case.service.execute(envelope, case.store) == response and case.store.profile() == saved
    forge_content(case)
    fail(case, envelope, "entrust_retry_stale")


def test_receipt_of_removed_emblem_mail_is_stale(case, item_catalogs):
    case.quests.rank_rewards = rank_reward_service(case, item_catalogs.mailed)
    envelope = message(case)
    rebuild(case).execute(envelope, case.store)
    case.quests.rank_rewards = rank_reward_service(case, item_catalogs.plain)
    rebuild(case)
    fail(case, envelope, "entrust_retry_stale")


def counting(monkeypatch, owner, name, counted):
    original = getattr(owner, name)

    def counted_call(*args):
        counted[name] += 1
        return original(*args)
    monkeypatch.setattr(owner, name, counted_call)


def test_each_cas_round_checks_the_catalog_and_dependencies_once_with_the_same_verdicts(case, monkeypatch):
    counted = Counter()
    counting(monkeypatch, case.catalog, "_ready", counted)
    counting(monkeypatch, case.service, "_content", counted)
    case.service.execute(message(case), case.store)
    assert counted == {"_ready": 1, "_content": 1}
    original, calls = case.store.update_state, []
    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            before = case.store.profile()
            other = deepcopy(before.state)
            other["retained_progress"]["nested"].append(3)
            original(other, before.revision)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    counted.clear()
    case.service.execute(message(case, tick=1001), case.store)
    assert len(calls) == 2 and counted == {"_ready": 2, "_content": 2}
    counted.clear()
    assert case.service.preview(case.store.profile(), STORY_QUEST, platform_code=PLATFORM)["ok"]
    assert counted == {"_ready": 1, "_content": 1}
    monkeypatch.setattr(case.catalog, "growth", GrowthCampaign.unavailable("changed"))
    fail(case, message(case, tick=1002), "entrust_catalog_changed")
    for refused in (lambda: case.service.preview(case.store.profile(), STORY_QUEST, platform_code=PLATFORM),
                    lambda: rebuild(case)):
        with pytest.raises(ProtocolError) as error:
            refused()
        assert (error.value.code, error.value.status) == ("entrust_catalog_changed", 503)


def test_cas_conflict_preserves_other_mutation_without_reroll(case, monkeypatch):
    original, calls, rolls = case.store.update_state, [], []
    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            before = case.store.profile()
            other = deepcopy(before.state)
            other["retained_progress"]["nested"].append(3)
            original(other, before.revision)
        return original(state, revision)
    case.service.randbelow = lambda _: rolls.append(0) or 0
    monkeypatch.setattr(case.store, "update_state", update)
    case.service.execute(message(case, quantity=3), case.store)
    assert len(rolls) == 9 and len(calls) == 2
    assert case.store.profile().state["retained_progress"]["nested"] == [1, 2, 3]
    assert len(case.store.profile().state["player_state"]["units"]) == 10


def test_cas_conflict_revalidates_energy_without_partial_rewards(case, monkeypatch):
    original, calls = case.store.update_state, []
    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            before = case.store.profile()
            other = deepcopy(before.state)
            other["tables"]["game_param"][0]["NowCharisma"] = 0
            original(other, before.revision)
        return original(state, revision)
    before = case.store.profile()
    monkeypatch.setattr(case.store, "update_state", update)
    with pytest.raises(ProtocolError, match="Quest costs"):
        case.service.execute(message(case), case.store)
    saved = case.store.profile()
    assert saved.revision == before.revision + 1 and "entrust_history" not in saved.state
    assert saved.state["player_state"] == before.state["player_state"]


@pytest.mark.parametrize("same_request", [True, False])
def test_concurrent_requests_commit_exactly_the_accepted_runs(case, monkeypatch, same_request):
    original, calls, lock, barrier = case.store.update_state, [], Lock(), Barrier(2, timeout=30)
    def update(state, revision):
        with lock:
            calls.append(revision)
            wait = len(calls) <= 2
        if wait:
            barrier.wait()
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    first = message(case, quantity=1)
    second = first if same_request else message(case, quantity=2)
    before = case.store.profile()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(case.service.execute, envelope, case.store) for envelope in (first, second)]
        results = [future.result(timeout=60) for future in futures]
    saved = case.store.profile()
    assert saved.revision == before.revision + (1 if same_request else 2)
    assert len(saved.state["player_state"]["units"]) == (4 if same_request else 10)
    if same_request:
        assert results[0] == results[1]


@pytest.mark.parametrize("stage", ["grant", "preflight", "encode"])
def test_failure_after_energy_debit_has_no_committed_side_effects(case, monkeypatch, stage):
    envelope = message(case, quantity=3)
    def unavailable(*_args, **_kwargs):
        raise ProtocolError("injected_failure", "fixture", 503)
    if stage == "grant":
        monkeypatch.setattr(case.players, "credit_units", unavailable)
    elif stage == "preflight":
        monkeypatch.setattr(case.service, "_preflight", unavailable)
    else:
        original = module.encode_response
        def encode(profile, xml):
            if 76 in profile.registration_keys:
                unavailable()
            return original(profile, xml)
        monkeypatch.setattr(module, "encode_response", encode)
    fail(case, envelope, "injected_failure")


def test_story_entrust_records_the_owning_story_mission(case):
    missions = recording(case)
    case.service.execute(message(case, quantity=2), case.store)
    assert missions.events == [("quest_clear", {"quest_id": STORY_QUEST, "quest_type": STORY_TYPE, "stars": 3,
                                                "hard": False, "mission_id": STORY_MISSION, "score_mode": False})] * 2


def test_history_entrust_charges_tickets_and_records_its_type_and_mission(case):
    missions = recording(case)
    prepare(case, HISTORY_QUEST, cleared_ids=(MODE_GATE, HISTORY_PREREQUISITE), tickets=3)
    product = case.catalog.product(HISTORY_QUEST)
    before = case.store.profile()
    result = nodes(case.service.execute(message(case, HISTORY_QUEST, 3), case.store))
    after = case.store.profile()
    drops = item_grants(case, product, 3)
    assert drops and held(after.state, TICKET) == 0
    assert all(held(after.state, item) == held(before.state, item) + count for item, count in drops.items())
    assert possession_counts(result) == {**{item: held(after.state, item) for item in drops}, TICKET: 0}
    units = after.state["player_state"]["units"][len(before.state["player_state"]["units"]):]
    level = product.quest.unit_level
    assert level > 1 and Counter(unit["CardID"] for unit in units) == unit_grants(case, product, 3)
    assert all(unit["Exp"] == case.players.experience_for_level(unit["CardID"], level) for unit in units)
    assert missions.events == [("quest_clear", {"quest_id": HISTORY_QUEST, "quest_type": HISTORY_TYPE, "stars": 3,
                                                "hard": False, "mission_id": HISTORY_MISSION, "score_mode": False})] * 3
    assert "quest_state" not in after.state
    assert after.state["tables"]["quest_records"] == before.state["tables"]["quest_records"]


def test_history_entrust_requires_the_mode_gate_quest(case):
    prepare(case, HISTORY_QUEST, cleared_ids=(HISTORY_PREREQUISITE,), tickets=1)
    error = fail(case, message(case, HISTORY_QUEST), "quest_locked")
    assert str(error) == "The mode entry requires its gate quest to be cleared"


def test_history_entrust_waits_for_the_client_disclosure_without_charging(case):
    prepare(case, HISTORY_QUEST, cleared_ids=(MODE_GATE, HISTORY_PREREQUISITE), tickets=1, disclose=False)
    error = fail(case, message(case, HISTORY_QUEST), "quest_closed")
    assert (str(error), error.status) == (GROUP_CLOSED, 409)
    preview = case.service.preview(case.store.profile(), HISTORY_QUEST, platform_code=PLATFORM)
    assert (preview["code"], preview["stage"], preview["mission_id"]) == ("quest_closed", Stage.ADMISSION, None)
    mutate(case, lambda state: disclosed(state, case.quests.catalog, HISTORY_QUEST))
    case.service.execute(message(case, HISTORY_QUEST, tick=1001), case.store)
    assert held(case.store.profile().state, TICKET) == 0


def test_history_entrust_closes_with_the_mission_its_menu_tag_resolves_to(bundle, tmp_path):
    case = entrust_case(bundle, tmp_path, closed_catalog(bundle, {TUTORIAL_MISSION: PAST_WINDOW}))
    prepare(case, HISTORY_QUEST, cleared_ids=(MODE_GATE, HISTORY_PREREQUISITE), tickets=1)
    error = fail(case, message(case, HISTORY_QUEST), "entrust_menu_closed")
    assert (str(error), error.status) == ("The entrust menu is closed for every playable mission", 409)
    preview = case.service.preview(case.store.profile(), HISTORY_QUEST, platform_code=PLATFORM)
    assert (preview["code"], preview["stage"], preview["mission_id"]) == ("entrust_menu_closed", Stage.ADMISSION, None)


def test_empire_entrust_waits_for_the_key_quest_of_its_mission(case):
    prepare(case, EMPIRE_QUEST, cleared_ids=(MODE_GATE, EMPIRE_PREREQUISITE), tickets=1)
    error = fail(case, message(case, EMPIRE_QUEST), "quest_locked")
    assert str(error) == "The mission requires its requested or key quest results"
    mutate(case, lambda state: state["tables"]["quest_records"].append(cleared(EMPIRE_KEY)))
    case.service.execute(message(case, EMPIRE_QUEST, tick=1001), case.store)
    assert held(case.store.profile().state, TICKET) == 0


def test_empire_entrust_runs_the_menu_check_of_its_battle_start(bundle, tmp_path):
    case = entrust_case(bundle, tmp_path, closed_catalog(bundle, {EMPIRE_MENU_MISSION: PAST_WINDOW}))
    prepare(case, EMPIRE_QUEST, cleared_ids=(MODE_GATE, EMPIRE_PREREQUISITE, EMPIRE_KEY), tickets=1)
    error = fail(case, message(case, EMPIRE_QUEST), "quest_closed")
    assert str(error) == "The empire menu needs an open Tag 801 mission showing a quest today"
    preview = case.service.preview(case.store.profile(), EMPIRE_QUEST, platform_code=PLATFORM)
    assert (preview["code"], preview["message"], preview["stage"]) == ("quest_closed", str(error), Stage.ADMISSION)


@pytest.mark.parametrize("moment,allowed", [(MONDAY_NOON, False), (NOW, True), (MONDAY_GRACE, True)])
def test_weekday_entrust_follows_the_jst_day_with_the_departure_grace(case, moment, allowed):
    prepare(case, LIMITED_DAILY, cleared_ids=(LIMITED_PREREQUISITE,), tickets=1)
    case.clock.now = moment
    if not allowed:
        error = fail(case, message(case, LIMITED_DAILY), "quest_closed")
        assert str(error) == "The quest is not open on the current JST day"
        return
    case.service.execute(message(case, LIMITED_DAILY), case.store)
    assert held(case.store.profile().state, TICKET) == 0


def test_growth_campaign_waives_daily_tickets_only_inside_its_window(case):
    prepare(case, DAILY_QUEST)
    live = case.catalog.describe(DAILY_QUEST, now=CAMPAIGN_DAY, platform_code=PLATFORM)["live"]
    assert live["day_open"] is True and [mission["free"] for mission in live["missions"]] == [True]
    case.clock.now = CAMPAIGN_DAY
    result = nodes(case.service.execute(message(case, DAILY_QUEST, 2), case.store))
    assert held(case.store.profile().state, TICKET) == 0 and "POSSESSION" not in result
    case.clock.now = NOW
    fail(case, message(case, DAILY_QUEST, 2, tick=1001), "entrust_ticket_insufficient")
    mutate(case, lambda state: hold(state, TICKET, 3))
    result = nodes(case.service.execute(message(case, DAILY_QUEST, 2, tick=1001), case.store))
    assert held(case.store.profile().state, TICKET) == 1 and possession_counts(result) == {TICKET: 1}


@pytest.mark.parametrize("tickets,quantity", [(0, 1), (2, 3)])
def test_missing_tickets_refuse_the_whole_batch(case, tickets, quantity):
    prepare(case, DAILY_QUEST, tickets=tickets)
    error = fail(case, message(case, DAILY_QUEST, quantity), "entrust_ticket_insufficient")
    assert error.status == 409


def test_types_outside_the_offered_scope_refuse_without_write(case):
    assert fail(case, message(case, REFUSED_QUEST), "entrust_not_offered").status == 409


def test_the_catalog_publishes_the_ticket_items_its_entrusts_consume(case):
    consumed = {case.catalog.product(quest_id).ticket_item_id
                for quest_id in (DAILY_QUEST, REPRODUCE_QUEST, HISTORY_QUEST, EMPIRE_QUEST)}
    assert isinstance(case.catalog.ticket_item_ids, frozenset)
    assert case.catalog.ticket_item_ids == consumed == {TICKET}


def test_reproduce_entrust_runs_its_history_member_through_mission_920014(case):
    missions = recording(case)
    prepare(case, REPRODUCE_QUEST, cleared_ids=(MODE_GATE, REPRODUCE_PREREQUISITE), tickets=2)
    product = case.catalog.product(REPRODUCE_QUEST)
    # Entrust needs a charisma maximum covering one run, which the quest rank alone does not reach.
    rank = next(rank for rank, row in enumerate(case.bootstrap.rank_rows, start=1)
                if row["CharismaMax"] >= product.quest.charisma)
    mutate(case, lambda state: set_rank(case, state, rank))
    before = case.store.profile()
    result = nodes(case.service.execute(message(case, REPRODUCE_QUEST, 2), case.store))
    after = case.store.profile()
    drops = item_grants(case, product, 2)
    assert drops and held(after.state, TICKET) == 0
    assert all(held(after.state, item) == held(before.state, item) + count for item, count in drops.items())
    assert possession_counts(result) == {**{item: held(after.state, item) for item in drops}, TICKET: 0}
    units = after.state["player_state"]["units"][len(before.state["player_state"]["units"]):]
    assert Counter(unit["CardID"] for unit in units) == unit_grants(case, product, 2)
    assert missions.events == [("quest_clear", {"quest_id": REPRODUCE_QUEST, "quest_type": REPRODUCE_TYPE, "stars": 3,
                                                "hard": False, "mission_id": REPRODUCE_MISSION, "score_mode": False})] * 2
    assert after.state["tables"]["quest_records"] == before.state["tables"]["quest_records"]


def test_reproduce_entrust_requires_the_mode_gate_quest(case):
    prepare(case, REPRODUCE_QUEST, cleared_ids=(REPRODUCE_PREREQUISITE,), tickets=1)
    error = fail(case, message(case, REPRODUCE_QUEST), "quest_locked")
    assert str(error) == "The mode entry requires its gate quest to be cleared"


def test_gold_rush_entrust_from_three_stars_grants_the_override_unit_and_leaves_the_record(bundle, tmp_path, gold_rush):
    case = entrust_case(bundle, tmp_path, gold_rush.quests, gold_rush.catalog)
    prepare(case, GOLD_RUSH_QUEST, cleared_ids=(MODE_GATE,), tickets=2)
    fail(case, message(case, GOLD_RUSH_QUEST, 2, extra=(rt_rows(GOLD_RUSH_BELOW_THREE_RT),)), "quest_candidates_mismatch")
    before = case.store.profile()
    result = nodes(case.service.execute(message(case, GOLD_RUSH_QUEST, 2, extra=(rt_rows(GOLD_RUSH_RT),)), case.store))
    after = case.store.profile()
    doubled = {code: 2 * count for code, count in GOLD_RUSH_DROPS.items()}
    assert treasure_info(result) == doubled
    units = after.state["player_state"]["units"][len(before.state["player_state"]["units"]):]
    # These unit codes name their own cards, so the override grants card 437 itself.
    assert Counter(unit["CardID"] for unit in units) == doubled
    assert after.state["tables"]["quest_records"] == before.state["tables"]["quest_records"]
    assert "QR" not in result and "quest_state" not in after.state and held(after.state, TICKET) == 0


def test_out_of_range_platform_codes_are_rejected_and_not_counted(case):
    error = fail(case, on_platform(message(case), INVALID_PLATFORM), "quest_platform_invalid")
    assert error.status == 400
    assert case.service.status()["platform_codes_seen"] == {"counts": {}, "last": None}


def test_every_parsed_request_counts_its_platform_code_in_memory_only(case):
    case.service.execute(message(case), case.store)
    case.service.execute(on_platform(message(case, tick=1001), OTHER_PLATFORM), case.store)
    fail(case, on_platform(message(case, tick=1002, revision="unverified"), OTHER_PLATFORM), "entrust_resource_mismatch")
    case.service.execute(message(case, tick=1002), case.store)
    assert case.service.status()["platform_codes_seen"] == {"counts": {PLATFORM: 2, OTHER_PLATFORM: 2}, "last": PLATFORM}
    assert rebuild(case).status()["platform_codes_seen"] == {"counts": {}, "last": None}


def test_the_local_client_platform_code_settles_like_the_native_default(case):
    native = nodes(case.service.execute(message(case), case.store))
    local = nodes(case.service.execute(on_platform(message(case, tick=1001), LOCAL_CLIENT_PLATFORM), case.store))
    assert local["REWARD"].children == native["REWARD"].children
    assert local["TreasureInfo"].children == native["TreasureInfo"].children
    assert case.service.status()["platform_codes_seen"] == {
        "counts": {PLATFORM: 1, LOCAL_CLIENT_PLATFORM: 1}, "last": LOCAL_CLIENT_PLATFORM}


def test_drops_beyond_the_stack_are_discarded_and_reported_through_iof(case):
    prepare(case, GIFT_DAILY, tickets=1)
    grants = item_grants(case, case.catalog.product(GIFT_DAILY))
    stack = case.service.possessions.stack(GIFT_ITEM)
    assert grants[GIFT_ITEM] > 1
    mutate(case, lambda state: hold(state, GIFT_ITEM, stack - 1))
    result = nodes(case.service.execute(message(case, GIFT_DAILY), case.store))
    saved = case.store.profile().state
    assert held(saved, GIFT_ITEM) == stack and possession_counts(result)[GIFT_ITEM] == stack
    assert result["IOF"].children == (Column("ItemID", "S", (GIFT_ITEM,)), Column("Count", "I", (grants[GIFT_ITEM] - 1,)))
    assert all(held(saved, item) == count for item, count in grants.items() if item != GIFT_ITEM)


@pytest.mark.parametrize("experience_up", [True, False])
@pytest.mark.parametrize("close,campaign", [(BOOST_OPEN, True), (BOOST_CLOSED, False), ("", False)])
def test_experience_campaigns_follow_the_entrust_rule(case, experience_up, close, campaign):
    mutate(case, lambda state: state["tables"]["config"][0].update(ExperienceUpCloseDateTime=close))
    rebuild(case, rules=EntrustRules(experience_up=experience_up))
    quest = case.catalog.product(1002).quest
    before = case.store.profile().state["tables"]["game_param"][0]["Experience"]
    result = nodes(case.service.execute(message(case, quantity=2), case.store))
    per_run = boosted_experience(quest.rank_exp) if experience_up and campaign else quest.rank_exp
    assert case.store.profile().state["tables"]["game_param"][0]["Experience"] == before + per_run * 2
    assert result["REWARD"].children[0].values == (per_run * 2,)


@pytest.mark.parametrize("close,campaign", [(BOOST_OPEN, True), (BOOST_CLOSED, False), ("", False)])
def test_money_campaigns_follow_the_settlement(case, close, campaign):
    mutate(case, lambda state: state["tables"]["config"][0].update(MoneyUpCloseDateTime=close))
    quest = case.catalog.product(STORY_QUEST).quest
    before = case.store.profile().state["tables"]["game_param"][0]["Money"]
    result = nodes(case.service.execute(message(case, quantity=2), case.store))
    per_run = entrust_gold(quest.gold, money_up=campaign)
    assert case.store.profile().state["tables"]["game_param"][0]["Money"] == before + per_run * 2
    assert result["REWARD"].children[1].values == (per_run * 2,)


def test_reward_hooks_adjust_treasure_info_possessions_and_overflow_once(case, monkeypatch):
    prepare(case, GIFT_DAILY, tickets=1)
    product = case.catalog.product(GIFT_DAILY)
    grants, stack, seen = item_grants(case, product), case.service.possessions.stack(GIFT_ITEM), []
    mutate(case, lambda state: hold(state, GIFT_ITEM, stack - grants[GIFT_ITEM]))
    def double(context, draw):
        seen.append(context)
        context.state["tables"]["possessions"].clear()
        return EntrustRewards(EntrustDraw({code: 2 * count for code, count in draw.counts.items()}, draw.units,
                                          {item: 2 * amount for item, amount in draw.items.items()}), (HOOK_CHILD,))
    monkeypatch.setattr(quest_modes.policy(DAILY_TYPE), "entrust_rewards", double)
    result = nodes(case.service.execute(message(case, GIFT_DAILY), case.store))
    saved = case.store.profile().state
    drops = Counter(product.reward_codes(REQUIRED_MAX_RANK))
    assert result["TreasureInfo"].children == (Column("Index", "I", tuple(range(len(drops)))),
        Column("DropID", "I", tuple(drops)), Column("Count", "I", tuple(2 * count for count in drops.values())))
    assert result["IOF"].children == (Column("ItemID", "S", (GIFT_ITEM,)), Column("Count", "I", (grants[GIFT_ITEM],)))
    assert possession_counts(result) == {**{item: 2 * count for item, count in grants.items()}, GIFT_ITEM: stack, TICKET: 0}
    assert all(held(saved, item) == count for item, count in possession_counts(result).items())
    assert result["HOOK"] == HOOK_CHILD
    context = seen[0]
    assert (context.quest, context.mission_id, context.quantity, context.platform_code) == (
        product.quest, GIFT_MISSION, 1, PLATFORM)


@pytest.mark.parametrize("damage", ["type", "foreign_code", "negative_count", "unknown_item", "zero_item", "unit_fields",
                                    "zero_unit", "child"])
def test_invalid_reward_hook_results_never_commit(case, monkeypatch, damage):
    def hook(context, draw):
        if damage == "type":
            return draw
        counts, units, items, children = dict(draw.counts), list(draw.units), dict(draw.items), ()
        if damage == "foreign_code":
            counts[999999] = 1
        elif damage == "negative_count":
            counts[next(iter(counts))] = -1
        elif damage == "unknown_item":
            items[MISSING_ITEM] = 1
        elif damage == "zero_item":
            items[next(name for name, stack in case.service.possessions._definitions.items() if stack)] = 0
        elif damage == "unit_fields":
            units[0] = {**units[0], "evolution": 1}
        elif damage == "zero_unit":
            units[0] = {**units[0], "count": 0}
        else:
            children = ("PPU",)
        return EntrustRewards(EntrustDraw(counts, tuple(units), items), children)
    monkeypatch.setattr(quest_modes.policy(STORY_TYPE), "entrust_rewards", hook)
    assert fail(case, message(case), "quest_mode_hook_invalid").status == 503


def test_zeroed_reward_hook_counts_leave_treasure_info(case, monkeypatch):
    def hook(context, draw):
        return EntrustRewards(EntrustDraw({**draw.counts, 4: 0}, draw.units, draw.items))
    monkeypatch.setattr(quest_modes.policy(STORY_TYPE), "entrust_rewards", hook)
    result = nodes(case.service.execute(message(case), case.store))
    assert result["TreasureInfo"].children[1].values == (67, 68)


def test_entrust_candidates_and_hook_codes_follow_the_record_max_rank(bundle, tmp_path, ranked, monkeypatch):
    case = entrust_case(bundle, tmp_path, ranked.quests, ranked.catalog)
    product = case.catalog.product(STORY_QUEST)
    assert product.all_reward_codes == (4, 67, VARIANT_CODE, THREE_STAR_CODE)
    assert product.reward_codes(REQUIRED_MAX_RANK) == (4, 67, THREE_STAR_CODE)
    fail(case, message(case, extra=(rt(case, max_rank=0),)), "quest_candidates_mismatch")
    def hook(context, draw):
        return EntrustRewards(EntrustDraw({**draw.counts, VARIANT_CODE: 1}, draw.units, draw.items))
    with monkeypatch.context() as patch:
        patch.setattr(quest_modes.policy(STORY_TYPE), "entrust_rewards", hook)
        assert fail(case, message(case), "quest_mode_hook_invalid").status == 503
    result = nodes(case.service.execute(message(case, extra=(rt(case),)), case.store))
    assert treasure_info(result) == {4: 1, 67: 1, THREE_STAR_CODE: 1}


def test_a_cas_retry_draws_again_only_when_the_record_changes_the_candidate_set(bundle, tmp_path, ranked, monkeypatch):
    case = entrust_case(bundle, tmp_path, ranked.quests, ranked.catalog)
    product = case.catalog.product(STORY_QUEST)
    original, calls, rolls = case.store.update_state, [], []
    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            before = case.store.profile()
            other = deepcopy(before.state)
            next(row for row in other["tables"]["quest_records"] if row["QuestID"] == STORY_QUEST).update(
                MaxRank=FOUR_STAR_RANK)
            original(other, before.revision)
        return original(state, revision)
    case.service.randbelow = lambda _: rolls.append(0) or 0
    monkeypatch.setattr(case.store, "update_state", update)
    result = nodes(case.service.execute(message(case), case.store))
    assert len(calls) == 2 and product.reward_codes(FOUR_STAR_RANK) == (4, 67)
    assert len(rolls) == len(product.reward_codes(REQUIRED_MAX_RANK)) + len(product.reward_codes(FOUR_STAR_RANK))
    assert treasure_info(result) == {4: 1, 67: 1}


@pytest.mark.parametrize("moment,credited,bonus", [
    (MONDAY_NOON, BATCH_BONUS_CRYSTALS, (CARVED_TYPE, BONUS_VALUE)),
    (NOW, BONUS_RUNS * CARVED_PER_RUN, None),
], ids=["monday", "sunday"])
def test_drop_bonus_scales_the_request_total_of_its_item_once_and_leaves_treasure_info(case, moment, credited, bonus):
    prepare(case, BONUS_QUEST, cleared_ids=(MODE_GATE,), tickets=BONUS_RUNS)
    case.clock.now = moment
    held_before = held(case.store.profile().state, CARVED_CRYSTAL)
    result = nodes(case.service.execute(message(case, BONUS_QUEST, BONUS_RUNS), case.store))
    saved = case.store.profile().state
    assert treasure_info(result)[CARVED_DROP] == BONUS_RUNS
    assert held(saved, CARVED_CRYSTAL) == possession_counts(result)[CARVED_CRYSTAL] == held_before + credited
    assert (bonus_values(result) if "BONUS" in result else None) == bonus


@pytest.mark.parametrize("quest_id,gates,quantity", [
    (DAILY_QUEST, (), 3), (HISTORY_QUEST, (MODE_GATE, HISTORY_PREREQUISITE), 2),
], ids=["daily", "history"])
def test_quest_tickets_count_every_run_of_the_batch_inside_the_ticket_window(case, ticket_provider, quest_id, gates,
                                                                            quantity):
    with_tickets(case, ticket_provider)
    prepare(case, quest_id, cleared_ids=gates, tickets=quantity)
    issued = quantity * (case.catalog.product(quest_id).quest.charisma // ticket_provider.rate)
    result = nodes(case.service.execute(message(case, quest_id, quantity), case.store))
    assert issued and held(case.store.profile().state, VOTE_TICKET) == possession_counts(result)[VOTE_TICKET] == issued
    assert "IOF" not in result and "AF" not in game_params(result)


def test_one_entrust_merges_bonus_drops_and_tickets_into_single_containers(case, ticket_provider):
    with_tickets(case, ticket_provider)
    prepare(case, BONUS_QUEST, cleared_ids=(MODE_GATE,), tickets=BONUS_RUNS)
    stack = case.service.possessions.stack(CARVED_CRYSTAL)
    def fill(state):
        hold(state, CARVED_CRYSTAL, stack - 1)
        hold(state, VOTE_TICKET, ticket_provider.cap)
    mutate(case, fill)
    case.clock.now = MONDAY_NOON
    response = case.service.execute(message(case, BONUS_QUEST, BONUS_RUNS), case.store)
    names = [child.name for child in loads(response).children]
    assert [names.count(name) for name in ("POSSESSION", "IOF", "BONUS")] == [1, 1, 1]
    result = nodes(response)
    issued = BONUS_RUNS * (case.catalog.product(BONUS_QUEST).quest.charisma // ticket_provider.rate)
    counts = possession_counts(result)
    assert (counts[CARVED_CRYSTAL], counts[VOTE_TICKET]) == (stack, ticket_provider.cap)
    discarded = dict(zip(*(column.values for column in result["IOF"].children)))
    assert discarded == {CARVED_CRYSTAL: BATCH_BONUS_CRYSTALS - 1, VOTE_TICKET: issued}
    assert bonus_values(result) == (CARVED_TYPE, BONUS_VALUE) and treasure_info(result)[CARVED_DROP] == BONUS_RUNS


@pytest.mark.parametrize("previous,value", [
    (0, 2 * EVENT_POINTS), (EVENT_PARAM_LIMIT - 1, EVENT_PARAM_LIMIT), (EVENT_PARAM_LIMIT + 1, EVENT_PARAM_LIMIT),
], ids=["fresh", "capped", "clamped"])
def test_event_drops_rewrite_event_param_and_resend_it(bundle, tmp_path, event_drop, previous, value):
    case = entrust_case(bundle, tmp_path, event_drop.quests, event_drop.catalog)
    assert case.quests.catalog.decode_reward(EVENT_CODE).count == EVENT_POINTS
    mutate(case, lambda state: state["tables"]["game_param"][0].update(EventParam=previous))
    result = nodes(case.service.execute(message(case, quantity=2), case.store))
    assert case.store.profile().state["tables"]["game_param"][0]["EventParam"] == value
    assert game_params(result)["AF"] == str(value) and treasure_info(result)[EVENT_CODE] == 2


def test_entrusts_without_event_drops_leave_event_param_unread_and_unsent(bundle, tmp_path, event_drop):
    case = entrust_case(bundle, tmp_path, event_drop.quests, event_drop.catalog)
    mutate(case, lambda state: state["tables"]["game_param"][0].update(EventParam=EVENT_PARAM_LIMIT + 1))
    result = nodes(case.service.execute(message(case, STORY_PREREQUISITE), case.store))
    assert case.store.profile().state["tables"]["game_param"][0]["EventParam"] == EVENT_PARAM_LIMIT + 1
    assert "AF" not in game_params(result)
    mutate(case, lambda state: state["tables"]["game_param"][0].update(EventParam=-1))
    case.service.execute(message(case, STORY_PREREQUISITE, tick=1001), case.store)
    assert fail(case, message(case, tick=1002), "quest_economy_state").status == 503


@pytest.mark.parametrize("factor,previous,value", [
    (0, EVENT_PARAM_LIMIT + 1, EVENT_PARAM_LIMIT + 1), (2, 0, 4 * EVENT_POINTS),
], ids=["zeroed", "doubled"])
def test_event_points_come_from_the_counts_the_reward_hook_commits(bundle, tmp_path, event_drop, monkeypatch, factor,
                                                                   previous, value):
    case = entrust_case(bundle, tmp_path, event_drop.quests, event_drop.catalog)
    def scaled(context, draw):
        return EntrustRewards(EntrustDraw({**draw.counts, EVENT_CODE: factor * draw.counts[EVENT_CODE]}, draw.units,
                                          draw.items))
    monkeypatch.setattr(quest_modes.policy(STORY_TYPE), "entrust_rewards", scaled)
    mutate(case, lambda state: state["tables"]["game_param"][0].update(EventParam=previous))
    result = nodes(case.service.execute(message(case, quantity=2), case.store))
    assert treasure_info(result).get(EVENT_CODE, 0) == 2 * factor
    assert case.store.profile().state["tables"]["game_param"][0]["EventParam"] == value
    assert game_params(result).get("AF") == (str(value) if factor else None)


def test_battle_event_grants_credit_only_known_items(case, monkeypatch):
    grant = BattleEventGrant({MISSING_ITEM: 1}, None, False, {})
    monkeypatch.setattr(case.quests.event_rewards, "evaluate", lambda state, context: grant)
    assert fail(case, message(case), "battle_event_grant_invalid").status == 503


@pytest.mark.parametrize("damage", ["progress", "progress_in_place", "active_battle", "presents", "outcome", "child",
                                    "unknown_item"])
def test_invalid_settle_hook_outcomes_never_commit(case, monkeypatch, damage):
    case.quests.start(start_request(case.quests, case.store, 1002, tick=900), case.store)
    policy = quest_modes.policy(STORY_TYPE)
    def settle(context):
        state = context.state if damage == "progress_in_place" else deepcopy(context.state)
        if damage in ("progress", "progress_in_place"):
            state["quest_state"]["mode_progress"] = {policy.name: {"entrusted": context.quantity}}
            return SettleOutcome(state)
        if damage == "active_battle":
            state["quest_state"]["active"] = None
            return SettleOutcome(state)
        if damage == "presents":
            return SettleOutcome(state, presents=({"kind": "free_crystal", "amount": 1, "description": "hook"},))
        if damage == "outcome":
            return state
        if damage == "unknown_item":
            return SettleOutcome(state, item_ids=(MISSING_ITEM,))
        return SettleOutcome(state, children=("PPT",))
    monkeypatch.setattr(policy, "entrust_settle", settle)
    assert fail(case, message(case), "quest_mode_hook_invalid").status == 503


def test_settle_hooks_append_children_and_report_their_items(case, monkeypatch):
    def settle(context):
        state = deepcopy(context.state)
        state["retained_progress"]["entrusted"] = context.quantity
        hold(state, TICKET, 4)
        return SettleOutcome(state, children=(HOOK_CHILD,), item_ids=(TICKET,))
    monkeypatch.setattr(quest_modes.policy(STORY_TYPE), "entrust_settle", settle)
    result = nodes(case.service.execute(message(case, quantity=2), case.store))
    saved = case.store.profile().state
    assert saved["retained_progress"]["entrusted"] == 2 and held(saved, TICKET) == 4
    assert possession_counts(result) == {TICKET: 4} and result["HOOK"] == HOOK_CHILD


def test_preview_reaches_the_same_verdict_as_the_request_without_writing(case):
    prepare(case, DAILY_QUEST)
    product, runs = case.catalog.product(DAILY_QUEST), 2
    charges = {"item": TICKET, "per_run": product.ticket_count, "free": False, "cost": product.ticket_count * runs}
    profile = case.store.profile()
    refused = case.service.preview(profile, DAILY_QUEST, quantity=runs, platform_code=PLATFORM)
    passed = [{"stage": stage, "result": "pass"} for stage in PLANNED[:PLANNED.index(Stage.TICKET)]]
    assert refused == {"ok": False, "code": "entrust_ticket_insufficient", "status": 409, "stage": Stage.TICKET,
                       "message": "Entrust tickets do not cover the requested runs", "quest_type": DAILY_TYPE,
                       "mission_id": DAILY_MISSION, "ticket": charges,
                       "charisma": {"per_run": product.quest.charisma, "total": product.quest.charisma * runs},
                       "checks": passed + [{"stage": Stage.TICKET, "result": "entrust_ticket_insufficient"}]}
    assert case.store.profile() == profile
    fail(case, message(case, DAILY_QUEST, runs), "entrust_ticket_insufficient")
    mutate(case, lambda state: hold(state, TICKET, charges["cost"]))
    profile = case.store.profile()
    accepted = case.service.preview(profile, DAILY_QUEST, quantity=runs, platform_code=PLATFORM)
    assert (accepted["ok"], accepted["code"], accepted["stage"], accepted["ticket"]) == (True, None, None, charges)
    assert accepted["checks"] == [{"stage": stage, "result": "pass"} for stage in PLANNED]
    assert case.store.profile() == profile
    case.service.execute(message(case, DAILY_QUEST, runs), case.store)
    assert held(case.store.profile().state, TICKET) == 0
    campaign = case.service.preview(case.store.profile(), DAILY_QUEST, now=CAMPAIGN_DAY, platform_code=PLATFORM)
    assert campaign["ok"] and campaign["ticket"] == {**charges, "free": True, "cost": 0}


@pytest.mark.parametrize("quest_id,quantity,platform,stage,code", [
    (0x7FFFFFFF, 1, PLATFORM, Stage.PRODUCT, "quest_not_found"),
    (REFUSED_QUEST, 1, PLATFORM, Stage.PRODUCT, "entrust_not_offered"),
    (STORY_QUEST, 4, PLATFORM, Stage.REQUEST, "entrust_request_invalid"),
    (STORY_QUEST, 1, INVALID_PLATFORM, Stage.REQUEST, "quest_platform_invalid"),
])
def test_preview_reports_the_first_failing_stage(case, quest_id, quantity, platform, stage, code):
    result = case.service.preview(case.store.profile(), quest_id, quantity=quantity, platform_code=platform)
    assert (result["ok"], result["stage"], result["code"], result["checks"][-1]) == (
        False, stage, code, {"stage": stage, "result": code})
    assert result["ticket"] == {"item": None, "per_run": None, "free": None, "cost": None}


def test_stamina_quests_refuse_with_501_before_charging_anything(bundle, tmp_path):
    original = bundle.quests.catalog
    quests = QuestCatalog(original.snapshot, original._asset_reader, possessions=bundle.possessions,
                          resource_version=original.resource_version)
    # Every current QuestTable row spends ActionPoint 0, so only a patched definition reaches the stamina guard.
    quests._definitions[STORY_QUEST] = replace(quests.get(STORY_QUEST), action_points=1)
    case = entrust_case(bundle, tmp_path, quests)
    assert fail(case, message(case), "entrust_stamina_capacity_unverified").status == 501
    profile = case.store.profile()
    preview = case.service.preview(profile, STORY_QUEST, platform_code=PLATFORM)
    assert (preview["ok"], preview["stage"], preview["code"], preview["status"]) == (
        False, Stage.STAMINA, "entrust_stamina_capacity_unverified", 501)
    assert case.store.profile() == profile


def test_status_describes_the_local_entrust_rules_and_check_order(case):
    status = rebuild(case, rules=EntrustRules(experience_up=False)).status()
    rules = status["local_rules"]
    assert (rules["quest"], rules["entrust"]) == (asdict(case.quests.rules), {"experience_up": False})
    assert (rules["day_grace_seconds"], rules["platform"]) == (DEPARTURE_GRACE_SECONDS, "require_platform")
    assert rules["check_order"] == list(CHECK_ORDER) and rules["check_order"][:2] == ["request", "resource_version"]
    assert rules["check_order"][2:5] == [Stage.AUTHENTICATION, Stage.REPLAY, Stage.PROCESSED]
    assert (status["history_limit"], status["processed_request_limit"], status["request_order"]) == (
        ledger.HISTORY_LIMIT, ledger.PROCESSED_LIMIT, ledger.REQUEST_ORDER)
    hooks = rules["check_order"].index(Stage.REWARD_HOOK)
    assert rules["check_order"][hooks:hooks + 3] == [Stage.REWARD_HOOK, Stage.EVENT_HOOK, Stage.GRANT]
    assert status["reward_sources"] == {"event_rewards": case.quests.event_rewards.content_version,
                                        "drop_bonus": case.quests.catalog.drop_bonus_table().content_version}
    assert status["catalog"]["offered_types"] == [STORY_TYPE, DAILY_TYPE, REPRODUCE_TYPE, HISTORY_TYPE, EMPIRE_TYPE]
    assert status["native_tested"] is False and status["platform_codes_seen"] == {"counts": {}, "last": None}
    assert isinstance(status["event_mission_reward_policy"], str) and status["event_mission_reward_policy"]
    with pytest.raises(ValueError):
        EntrustRules(experience_up=1)
    with pytest.raises(ValueError):
        rebuild(case, rules={"experience_up": True})


def entrust_step(qid=STORY_QUEST, quantity=1, *, tick=1000, revision=None):
    def envelope(service, store):
        fields = (Column("QI", "I", (qid,)), Column("Rev", "S", (revision or service.quests.catalog.resource_version,)))
        if quantity is not None:
            fields += (Container("Boost", (Column("V", "I", (quantity,)),)),)
        return request(store, 76, fields, tick=tick)
    return GoldenStep(76, envelope)


def entrust_base(bundle) -> dict:
    state = golden_base(bundle)
    state["tables"]["game_param"][0]["NowCharisma"] = 10000
    state["tables"]["quest_records"] = [eligible_record(bundle.entrust.catalog.product(qid).quest)
                                        for qid in (STORY_PREREQUISITE, STORY_QUEST)]
    return state


def receipts_evicted(state):
    state["entrust_history"]["receipts"] = []


def content_forged(state):
    state["entrust_history"]["receipts"][-1]["content"] = "f" * 64


ENTRUST_CASES = {
    "single_run": GoldenCase((entrust_step(), entrust_step(), replace(entrust_step(), prepare=tutorial_touched))),
    "request_stale": GoldenCase((entrust_step(), replace(entrust_step(), prepare=receipts_evicted))),
    "forged_content": GoldenCase((entrust_step(), replace(entrust_step(), rewrite=content_forged))),
    "three_runs": GoldenCase((entrust_step(quantity=3),)),
    "refusals": GoldenCase((entrust_step(revision="stale-revision"), entrust_step(quantity=4),
                            entrust_step(REFUSED_QUEST), entrust_step(tick=1001))),
    "drained": GoldenCase((entrust_step(),), drained),
    "money_campaign": GoldenCase((entrust_step(),), money_campaign),
    "gold_team": GoldenCase((entrust_step(),), gold_team),
}


def entrust_outcome(bundle, base: dict, case: GoldenCase) -> dict:
    start = deepcopy(base)
    if case.stored is not None:
        start = case.stored(bundle, start)
    clock = SimpleNamespace(now=GOLDEN_NOW)
    quests = QuestService(bundle.quests.catalog, bundle.bootstrap, bundle.player_state, bundle.possessions,
                          clock=lambda: clock.now, randbelow=lambda _: 0)
    service = EntrustService(quests, bundle.entrust.catalog, clock=lambda: clock.now, randbelow=lambda _: 0)
    store = GoldenStore(start)
    steps = golden_steps(service.handlers(), store, clock, case.steps, lambda step: step.envelope(service, store))
    state = changed_sections(start, store.profile().state)
    for receipt in state["sections"].get(module.HISTORY_NAMESPACE, {}).get("receipts", ()):
        if receipt["content"] == service.content_version:
            receipt["content"] = CONTENT_VERSION_TOKEN
    return {"steps": steps, "state": state}


def test_entrust_golden_covers_every_case_with_successes_and_refusals():
    golden = recorded(ENTRUST_GOLDEN)
    assert golden["cases"].keys() == ENTRUST_CASES.keys()
    steps = [step for case in golden["cases"].values() for step in case["steps"]]
    assert any("response" in step for step in steps)
    assert {step["error"]["code"] for step in steps if "error" in step} >= {
        "entrust_retry_stale", "entrust_request_stale", "entrust_resource_mismatch", "entrust_request_invalid",
        "quest_energy_insufficient"}


@pytest.mark.parametrize("name", ENTRUST_CASES)
def test_entrust_requests_match_the_recorded_golden_bytes(golden_bundle, name):
    golden = recorded(ENTRUST_GOLDEN)
    result = entrust_outcome(golden_bundle, golden["base"], ENTRUST_CASES[name])
    assert canonical(result) == canonical(golden["cases"][name])


def test_the_entrust_golden_differs_from_the_previous_build_only_in_gold(golden_bundle):
    differences = recorded_differences(golden_bundle, ENTRUST_CASES, recorded(ENTRUST_GOLDEN),
                                       recorded(LEGACY_ENTRUST_GOLDEN))
    assert differences == ENTRUST_SETTLEMENT_CHANGES


def test_an_entrust_receipt_of_the_previous_build_replays_its_original_bytes(golden_bundle):
    legacy, current = (recorded(path)["cases"]["three_runs"] for path in (LEGACY_ENTRUST_GOLDEN, ENTRUST_GOLDEN))
    case = ENTRUST_CASES["three_runs"]
    state = settled_state(golden_bundle, recorded(LEGACY_ENTRUST_GOLDEN)["base"], case, legacy)
    clock = SimpleNamespace(now=GOLDEN_NOW)
    quests = QuestService(golden_bundle.quests.catalog, golden_bundle.bootstrap, golden_bundle.player_state,
                          golden_bundle.possessions, clock=lambda: clock.now, randbelow=lambda _: 0)
    service = EntrustService(quests, golden_bundle.entrust.catalog, clock=lambda: clock.now, randbelow=lambda _: 0)
    previous_content = module._content_digest(module._committed_records(service._content_record())[1], CHECK_ORDER)
    receipts = state[module.HISTORY_NAMESPACE]["receipts"]
    assert [receipt["content"] for receipt in receipts] == [CONTENT_VERSION_TOKEN]
    receipts[0]["content"] = previous_content
    store = GoldenStore(state, revision=sum(step["revisions"] for step in legacy["steps"]))
    envelope = case.steps[0].envelope(service, store)
    assert envelope.xml.decode("utf-8") == legacy["steps"][0]["request"]
    assert legacy["steps"][0]["response"] != current["steps"][0]["response"]
    assert service.execute(envelope, store).decode("utf-8") == legacy["steps"][0]["response"]
    assert store.profile().state == state
