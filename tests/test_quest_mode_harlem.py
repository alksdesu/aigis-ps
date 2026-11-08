from types import MappingProxyType

import pytest

from aigis_ps import quest_modes
from aigis_ps.alxml import Column, Container, loads
from aigis_ps.card_status import CARD_STATUS_FIELDS, THIRD_EPISODE
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.errors import ProtocolError
from aigis_ps.quest_catalog import HARMONY_CRYSTALS, TREASURE_SLOTS, StarReward
from aigis_ps.quest_mode_harlem import (
    AWAKENED_STAGE, FEMALE_KIND, HARLEM_TYPE, KIND_TRAIT, PERSON_ARCHIVE, PERSON_TABLE, POLICY,
    HarlemPolicy, PersonConfig,
)
from aigis_ps.quest_modes import CLEAR_FLAG, SLOT_FLAGS, ModeServices, StartContext, THREE_STAR_FLAG
from aigis_ps.storage import Store
from test_mission_hooks import local
from test_quest_battle_modes import (
    MISSION_NOW, Recorder, card_ids, charisma, gold_events, hard_start, mutate, new_cards, put_record, record,
    three_star_gold, update_game_param,
)
from test_quest_service import allow_quest, bundle, finish_request, nodes, setup, start_request
from test_quest_settlement import drops, explicit


GATE_QUEST = 1003
HARLEM_MISSION = 600001
HARLEM_QUESTS = tuple(range(3200, 3652))
QUEST, PARTNER, SPIRIT = 3200, 17, 335
OTHER_QUEST, OTHER_PARTNER = 3201, 32
SPIRITS = frozenset((333, 334, 335, 491))
PRIZE_SLOTS = frozenset((1, 2, 3, 4, 5, 8, 41))
CHARISMA = 1000
DAILY_CLEARS, MONTHLY_CLEARS = 1605, 1632
FEMALES = frozenset((PARTNER, OTHER_PARTNER))
TEAM_COLUMNS = ("A1", "A7", "A8", "AB", "AA")
# 4CF530 numbers the prince copy UX+1000+n and 6785B0 numbers the fixed members from UX+3001.
AVATAR_OFFSET, FIXED_OFFSET = 1001, 3001
KEPT_STATUS = MappingProxyType({field: index for index, field in enumerate(CARD_STATUS_FIELDS)
                                if field not in ("CardID", "A7")})


def assert_refused(code, status, callback):
    with pytest.raises(ProtocolError) as error:
        callback()
    assert (error.value.code, error.value.status) == (code, status)


def recruit(bundle, store, card_id, stage=AWAKENED_STAGE):
    def change(state):
        state, units = bundle.player_state.credit_units(
            state, [{"card_id": card_id, "level": 1, "count": 1, "evolution_count": AWAKENED_STAGE}])
        unit = next(unit for unit in state["player_state"]["units"] if unit["GetIndex"] == units[0]["GetIndex"])
        unit["ClassChangeCount"] = stage
        return state
    mutate(store, change)


def partner_unit(state):
    return next(unit for unit in state["player_state"]["units"] if unit["CardID"] == PARTNER)


def saved_status(store, card_id):
    return next((row for row in store.profile().state["tables"]["card_status"] if row["CardID"] == card_id), None)


def sent_status(tree):
    columns = tree["CardStatus"].children
    return [dict(zip((column.name for column in columns), values)) for values in zip(*(column.values for column in columns))]


def person(card_id, quest_id, **fields):
    return {"_CardID": card_id, "_QuestID": quest_id, "_InfoTextID": 406, "_AppearQuestID": 0,
            "_AppearExpression": None, **fields}


def team(name, rows):
    return Container(name, tuple(Column(column, "I", tuple(row[index] for row in rows))
                                 for index, column in enumerate(TEAM_COLUMNS)))


@pytest.fixture
def harlem(setup, bundle):
    service, store = setup
    service.card_status = bundle.card_status
    allow_quest(store, GATE_QUEST)
    update_game_param(store, NowCharisma=CHARISMA)
    return service, store


def test_harlem_policy_is_registered_behind_the_1003_menu_gate():
    assert quest_modes.policy(HARLEM_TYPE) is POLICY
    assert POLICY.status() == {"policy": "harlem", "enabled": True, "reason": None, "hard_mode": False,
                               "timed_conditions": False, "gate_quest": GATE_QUEST}


def test_every_harlem_quest_builds_from_the_current_resources(bundle):
    catalog = bundle.quests.catalog
    quest_ids = catalog.playable_quests(HARLEM_TYPE)
    assert quest_ids == HARLEM_QUESTS and set(quest_ids) <= set(catalog.mode_ids)
    spirits, slots = set(), set()
    for quest_id in quest_ids:
        quest, mode = catalog.get(quest_id), catalog.mode(quest_id)
        assert mode.hard is None and mode.gold_counted and mode.mission_ids == (HARLEM_MISSION,)
        assert quest.treasures == (0,) * TREASURE_SLOTS and catalog.fixed_units(quest.unit_list)
        spirit, crystals = mode.star_rewards_normal
        assert spirit.kind == "unit" and crystals == StarReward("crystal", 0, HARMONY_CRYSTALS)
        spirits.add(spirit.card_id)
        slots.update(candidate.slot for candidate in quest.candidates)
    assert spirits == SPIRITS and slots == PRIZE_SLOTS


def test_the_1003_clear_gates_every_harlem_start(setup, bundle):
    service, store = setup
    service.card_status = bundle.card_status
    update_game_param(store, NowCharisma=CHARISMA)
    recruit(bundle, store, PARTNER)
    for flags in (None, 0):
        if flags is not None:
            put_record(store, GATE_QUEST, max_rank=0, flags=flags)
        before = store.profile()
        assert_refused("quest_locked", 409, lambda: service.start(start_request(service, store, QUEST), store))
        assert store.profile() == before
    put_record(store, GATE_QUEST, max_rank=1, flags=CLEAR_FLAG)
    service.start(start_request(service, store, QUEST), store)
    active = store.profile().state["quest_state"]["active"]
    assert (active["quest_id"], active["mission_id"], active["mode"]["quest_type"]) == (QUEST, HARLEM_MISSION, HARLEM_TYPE)
    assert charisma(store) == CHARISMA


@pytest.mark.parametrize("stages,opens", [
    ((), False), ((0,), False), ((AWAKENED_STAGE - 1,), False), ((AWAKENED_STAGE,), True), ((1, 4), True)])
def test_only_an_awakened_unit_of_the_paired_card_opens_its_quest(harlem, bundle, stages, opens):
    service, store = harlem
    for stage in stages:
        recruit(bundle, store, PARTNER, stage)
    before = store.profile()
    starting = lambda: service.start(start_request(service, store, QUEST), store)
    if opens:
        starting()
        assert store.profile().state["quest_state"]["active"]["quest_id"] == QUEST
        assert charisma(store) == CHARISMA
    else:
        assert_refused("quest_locked", 409, starting)
        assert store.profile() == before


def test_each_awakened_partner_opens_only_its_own_quest(harlem, bundle):
    service, store = harlem
    recruit(bundle, store, OTHER_PARTNER)
    before = store.profile()
    assert_refused("quest_locked", 409, lambda: service.start(start_request(service, store, QUEST), store))
    assert store.profile() == before
    service.start(start_request(service, store, OTHER_QUEST), store)
    assert store.profile().state["quest_state"]["active"]["quest_id"] == OTHER_QUEST


@pytest.mark.parametrize("flags", [None, 0, THREE_STAR_FLAG])
def test_starts_awaiting_the_first_clear_require_the_card_status_service_before_charging(setup, bundle, flags):
    service, store = setup
    allow_quest(store, GATE_QUEST)
    update_game_param(store, NowCharisma=CHARISMA)
    recruit(bundle, store, PARTNER)
    if flags is not None:
        put_record(store, QUEST, max_rank=0, flags=flags)
    before = store.profile()
    assert_refused("quest_card_status_unavailable", 409, lambda: service.start(start_request(service, store, QUEST), store))
    assert store.profile() == before


def test_repeat_clears_of_a_cleared_quest_need_no_card_status_service(setup, bundle):
    service, store = setup
    allow_quest(store, GATE_QUEST)
    update_game_param(store, NowCharisma=CHARISMA)
    recruit(bundle, store, PARTNER)
    put_record(store, QUEST, max_rank=1, flags=CLEAR_FLAG)
    assert service.card_status is None
    service.start(start_request(service, store, QUEST), store)
    assert charisma(store) == CHARISMA
    tree = nodes(service.finish(finish_request(store, QUEST), store))
    assert charisma(store) == CHARISMA - service.catalog.get(QUEST).charisma
    assert "CardStatus" not in tree and new_cards(tree) == (SPIRIT,)
    assert saved_status(store, PARTNER) is None and record(store, QUEST)["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG


@pytest.mark.parametrize("state", [None, {}, {"player_state": {"units": None}}, {"player_state": {"units": [PARTNER]}}])
def test_a_start_without_a_readable_roster_is_refused_as_invalid_state(bundle, state):
    catalog = bundle.quests.catalog
    records = {GATE_QUEST: {"QuestID": GATE_QUEST, "MaxRank": 1, "AchieveFlag": CLEAR_FLAG}}
    context = StartContext(catalog, catalog.get(QUEST), catalog.mode(QUEST), records, 1, MISSION_NOW, False, 1, state,
                           services=ModeServices(card_status=bundle.card_status))
    assert_refused("player_units_invalid", 503, lambda: POLICY.check_start(context))


def test_harlem_quests_have_no_four_star_challenge(harlem, bundle):
    service, store = harlem
    recruit(bundle, store, PARTNER)
    put_record(store, QUEST)
    before = store.profile()
    assert_refused("quest_hard_unavailable", 409, lambda: service.start(hard_start(service, store, QUEST), store))
    assert store.profile() == before


def test_the_first_clear_unlocks_the_third_episode_and_three_stars_grant_the_spirit_once(harlem, bundle):
    service, store = harlem
    recruit(bundle, store, PARTNER)
    initial = store.profile().state
    wallet = wallet_totals(initial)["free"]
    response = service.start(start_request(service, store, QUEST), store)
    assert not nodes(response)["LOTTERY"].children[0].values
    assert [grant["card_id"] for grant in store.profile().state["quest_state"]["active"]["unit_reservations"]] == [SPIRIT]
    finishing = finish_request(store, QUEST)
    response = service.finish(finishing, store)
    completed = store.profile()
    assert service.finish(finishing, store) == response and store.profile() == completed
    assert loads(response).children[-1].name == "CardStatus"
    tree = nodes(response)
    expected = {**dict.fromkeys(CARD_STATUS_FIELDS, 0), "CardID": PARTNER, "A7": THIRD_EPISODE}
    assert sent_status(tree) == [expected] and saved_status(store, PARTNER) == expected
    assert completed.state["card_status_schema"] == 1
    assert new_cards(tree) == (SPIRIT,) and card_ids(store).count(SPIRIT) == 1
    assert "PFC" in tree and wallet_totals(completed.state)["free"] == wallet + HARMONY_CRYSTALS
    quest = service.catalog.get(QUEST)
    assert tree["REWARD"].children[0].values == (quest.rank_exp,)
    assert tree["REWARD"].children[1].values == (three_star_gold(quest.gold),)
    assert record(store, QUEST)["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG and record(store, QUEST)["MaxRank"] == 3
    assert completed.state["tables"]["game_param2"] == initial["tables"]["game_param2"]
    assert partner_unit(completed.state) == partner_unit(initial)
    service.start(start_request(service, store, QUEST, tick=1002), store)
    tree = nodes(service.finish(finish_request(store, QUEST, tick=1003), store))
    assert "CardStatus" not in tree and not new_cards(tree) and "PFC" not in tree
    saved = store.profile().state
    assert saved["tables"]["card_status"] == completed.state["tables"]["card_status"]
    assert wallet_totals(saved)["free"] == wallet + HARMONY_CRYSTALS


def test_a_low_star_first_clear_unlocks_the_episode_before_the_spirit(harlem, bundle):
    service, store = harlem
    recruit(bundle, store, PARTNER)
    service.start(start_request(service, store, QUEST), store)
    tree = nodes(service.finish(finish_request(store, QUEST, stars=1), store))
    assert sent_status(tree)[0]["A7"] == THIRD_EPISODE and not new_cards(tree) and "PFC" not in tree
    assert record(store, QUEST)["AchieveFlag"] == CLEAR_FLAG and record(store, QUEST)["MaxRank"] == 1
    service.start(start_request(service, store, QUEST, tick=1002), store)
    tree = nodes(service.finish(finish_request(store, QUEST, tick=1003), store))
    assert "CardStatus" not in tree and new_cards(tree) == (SPIRIT,) and "PFC" in tree
    assert record(store, QUEST)["AchieveFlag"] == CLEAR_FLAG | THREE_STAR_FLAG


def test_harlem_gold_events_count_toward_the_settled_gold(harlem, bundle):
    service, store = harlem
    recruit(bundle, store, PARTNER)
    service.start(start_request(service, store, QUEST), store)
    tree = nodes(service.finish(finish_request(store, QUEST, gold=2, extra=gold_events(2)), store))
    settled = three_star_gold(service.catalog.get(QUEST).gold + 2 * service.rules.gold_per_event)
    assert tree["REWARD"].children[1].values == (settled,)
    assert store.profile().state["quest_state"]["history"][-1]["gold_events"] == 2


@pytest.mark.parametrize("existing,expected", [(1, 5), (3, 7), (THIRD_EPISODE, None), (7, None)])
def test_the_first_clear_adds_only_the_episode_bit_to_saved_card_status(harlem, bundle, existing, expected):
    service, store = harlem
    recruit(bundle, store, PARTNER)
    saved = {"CardID": PARTNER, **KEPT_STATUS, "A7": existing}
    mutate(store, lambda state: bundle.card_status.update(state, [saved]))
    service.start(start_request(service, store, QUEST), store)
    tree = nodes(service.finish(finish_request(store, QUEST), store))
    if expected is None:
        assert "CardStatus" not in tree and saved_status(store, PARTNER) == saved
    else:
        assert sent_status(tree) == [saved_status(store, PARTNER)] == [{**saved, "A7": expected}]


def test_a_repeated_clear_leaves_a_missing_episode_bit_unwritten(harlem, bundle):
    service, store = harlem
    recruit(bundle, store, PARTNER)
    put_record(store, QUEST)
    service.start(start_request(service, store, QUEST), store)
    tree = nodes(service.finish(finish_request(store, QUEST), store))
    assert "CardStatus" not in tree and saved_status(store, PARTNER) is None


def test_the_episode_follows_the_quest_pairing_rather_than_the_reported_partner(harlem, bundle):
    service, store = harlem
    recruit(bundle, store, PARTNER)
    recruit(bundle, store, OTHER_PARTNER)
    other = next(unit for unit in store.profile().state["player_state"]["units"] if unit["CardID"] == OTHER_PARTNER)
    service.start(start_request(service, store, QUEST), store)
    reported = team("BL", [(OTHER_PARTNER, other["GetIndex"], 0, other["UniqueID"], 0)])
    tree = nodes(service.finish(finish_request(store, QUEST, extra=(reported,)), store))
    assert [row["CardID"] for row in sent_status(tree)] == [PARTNER]
    assert saved_status(store, PARTNER)["A7"] == THIRD_EPISODE and saved_status(store, OTHER_PARTNER) is None


def test_a_first_clear_without_the_card_status_service_keeps_the_attempt(harlem, bundle):
    service, store = harlem
    recruit(bundle, store, PARTNER)
    service.start(start_request(service, store, QUEST), store)
    service.card_status = None
    before = store.profile()
    assert_refused("quest_card_status_unavailable", 409, lambda: service.finish(finish_request(store, QUEST), store))
    assert store.profile() == before
    service.card_status = bundle.card_status
    assert sent_status(nodes(service.finish(finish_request(store, QUEST), store)))[0]["A7"] == THIRD_EPISODE
    service.start(start_request(service, store, QUEST, tick=1002), store)
    service.card_status = None
    tree = nodes(service.finish(finish_request(store, QUEST, tick=1003), store))
    assert "CardStatus" not in tree and saved_status(store, PARTNER)["A7"] == THIRD_EPISODE


@pytest.mark.parametrize("quest_id,partner,bits", [(3294, 357, sum(SLOT_FLAGS)), (3585, 1003, 0)])
def test_prize_slots_mark_harlem_records_without_treasure_rewards(harlem, bundle, quest_id, partner, bits):
    service, store = harlem
    recruit(bundle, store, partner)
    quest = service.catalog.get(quest_id)
    count, mask = len(quest.candidates), (1 << len(quest.candidates)) - 1
    service.start(start_request(service, store, quest_id, candidates=explicit(quest, (0,) * count)), store)
    tree = nodes(service.finish(finish_request(store, quest_id, obtained=mask, selected=mask, stars=1), store))
    assert not drops(tree) and not new_cards(tree)
    assert record(store, quest_id)["AchieveFlag"] == CLEAR_FLAG | bits
    before = store.profile()
    assert_refused("quest_candidates_mismatch", 409, lambda: service.start(
        start_request(service, store, quest_id, tick=1002, candidates=explicit(quest, (0,) * count)), store))
    assert store.profile() == before
    service.start(start_request(service, store, quest_id, tick=1002, candidates=explicit(quest, (1,) * count)), store)


def test_the_selected_partner_and_prince_copy_settle_without_formation_warnings(harlem, bundle, caplog):
    service, store = harlem
    recruit(bundle, store, PARTNER)
    player = store.profile().state["player_state"]
    prince, partner = player["units"]
    ux = player["battle"]["UX"]
    fixed = [(unit.card_id, ux + FIXED_OFFSET + index, 0, 0, unit.cost_mod)
             for index, unit in enumerate(service.catalog.fixed_units(service.catalog.get(QUEST).unit_list))]
    selected = [(PARTNER, partner["GetIndex"], 0, partner["UniqueID"], 0),
                (prince["CardID"], ux + AVATAR_OFFSET, 1, prince["UniqueID"], 0)]
    recorder = Recorder()
    service.missions = recorder
    service.start(start_request(service, store, QUEST), store)
    with caplog.at_level("WARNING", logger="aigis_ps"):
        service.finish(finish_request(store, QUEST, extra=(team("BL", selected), team("HL", fixed))), store)
    assert not caplog.records
    (kind, payload), = recorder.events
    assert kind == "quest_clear" and payload["quest_type"] == HARLEM_TYPE and payload["mission_id"] == HARLEM_MISSION
    assert payload["team"] == [{"card_id": PARTNER, "get_index": partner["GetIndex"], "unique_id": partner["UniqueID"]}]


def test_a_harlem_clear_counts_toward_any_quest_missions(local, tmp_path):
    data = local.bundle
    initial = local.missions.initialize(data.player_state.initialize(local.bootstrap.initial_state()))
    store = Store(tmp_path / "harlem.sqlite3", initial)
    allow_quest(store, GATE_QUEST)
    recruit(data, store, PARTNER)
    quests = local.quests
    quests.start(start_request(quests, store, QUEST), store)
    quests.finish(finish_request(store, QUEST), store)
    rows = {row["ID"]: row for row in local.missions.progress.snapshot(store.profile().state, MISSION_NOW)}
    assert rows[DAILY_CLEARS]["Status"] == 1 and rows[MONTHLY_CLEARS]["Progress"] == 1


@pytest.mark.parametrize("change,code", [
    ({"Type": 9}, "quest_definition_invalid"), ({"_HardLevel": 1}, "quest_definition_invalid"),
    ({"_HardLevel": False}, "quest_definition_invalid"), ({"BonusType": 0}, "quest_definition_invalid"),
    ({"Treasure4": 67}, "quest_definition_invalid"), ({"UnitList": 0}, "quest_definition_invalid"),
    ({"UnitList": 999999}, "quest_definition_invalid"), ({"BonusNum": 999999}, "quest_reward_unknown"),
    ({"QuestID": 1001}, "quest_mode_resource_invalid")])
def test_rows_outside_the_harlem_contract_are_refused(bundle, change, code):
    catalog = bundle.quests.catalog
    POLICY.validate_row(catalog.row(QUEST), catalog)
    changed = MappingProxyType({**catalog.row(QUEST), **change})
    assert_refused(code, 503, lambda: POLICY.validate_row(changed, catalog))


@pytest.mark.parametrize("rows,code,status", [
    ((), "quest_mode_resource_invalid", 503),
    ((person(PARTNER, QUEST), person(OTHER_PARTNER, QUEST)), "quest_mode_resource_invalid", 503),
    ((person(PARTNER, QUEST), person(PARTNER, OTHER_QUEST)), "quest_mode_resource_invalid", 503),
    ((person(2, QUEST),), "quest_mode_resource_invalid", 503),
    ((person(999999, QUEST),), "quest_mode_resource_invalid", 503),
    ((person(PARTNER, QUEST, _AppearQuestID=OTHER_QUEST),), "quest_condition_unsupported", 501),
    ((person(PARTNER, QUEST, _AppearExpression="IsCleardQuest(1004)"),), "quest_condition_unsupported", 501)])
def test_person_rows_must_pair_the_quest_with_one_unconditional_female_card(rows, code, status):
    assert_refused(code, status, lambda: PersonConfig.from_rows(rows, FEMALES).card(QUEST))


def test_a_later_person_row_takes_over_its_card_like_the_native_loader():
    config = PersonConfig.from_rows((person(PARTNER, OTHER_QUEST), person(PARTNER, QUEST),
                                     person(OTHER_PARTNER, 3202)), FEMALES)
    assert config.card(QUEST) == PARTNER and config.card(3202) == OTHER_PARTNER
    assert_refused("quest_mode_resource_invalid", 503, lambda: config.card(OTHER_QUEST))


@pytest.mark.parametrize("row", [
    person(0, QUEST), person(PARTNER, 0), person(True, QUEST), person(PARTNER, 2 ** 31), {"_QuestID": QUEST}])
def test_person_rows_require_positive_native_identifiers(row):
    assert_refused("quest_mode_resource_invalid", 503, lambda: PersonConfig.from_rows((row,), FEMALES))


def test_pairs_come_from_the_catalog_person_table_and_female_card_index(bundle, monkeypatch):
    catalog = bundle.quests.catalog
    archive_rows, card_ids_with, calls = catalog.archive_rows, catalog.card_ids_with, []
    monkeypatch.setattr(catalog, "archive_rows", lambda *names: calls.append(names) or archive_rows(*names))
    monkeypatch.setattr(catalog, "card_ids_with", lambda *trait: calls.append(trait) or card_ids_with(*trait))
    HarlemPolicy().validate_row(catalog.row(QUEST), catalog)
    assert calls == [(PERSON_ARCHIVE, PERSON_TABLE), (KIND_TRAIT, FEMALE_KIND)]
    assert PERSON_ARCHIVE in catalog.status()["mode_resource_sha256"]
    assert {PARTNER, OTHER_PARTNER} <= card_ids_with(KIND_TRAIT, FEMALE_KIND)
    monkeypatch.setattr(catalog, "card_ids_with", lambda *trait: card_ids_with(*trait) - {PARTNER})
    narrowed = HarlemPolicy()
    assert_refused("quest_mode_resource_invalid", 503, lambda: narrowed.validate_row(catalog.row(QUEST), catalog))
    assert narrowed.validate_row(catalog.row(OTHER_QUEST), catalog) is None
