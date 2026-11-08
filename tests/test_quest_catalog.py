from collections import Counter
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
import hashlib
from pathlib import Path
import struct
from types import MappingProxyType
import zlib

import lz4.frame
import pytest

from aigis_ps import quest_modes
from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.drop_bonus import RESOURCE as DROP_BONUS_RESOURCE, SUPPORTED_OPERATION, DropBonus
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.masterdata import MasterDataStore, MasterSnapshot
from aigis_ps.mission_tags import REGISTRY_CATEGORIES, REGISTRY_TABLES, mission_tables
from aigis_ps.possessions import PossessionService
from aigis_ps.quest_catalog import (
    EVENT_PARAM_DROP_TYPE, GIFT_DROP_TYPE, GIFT_EFFECT_TYPE, GIFT_REWARD_TYPES, ITEM_REWARD_KIND, MAX_CANDIDATES,
    SPECIAL_ENEMY_ATTR, DropCandidate, QuestCatalog, Reward, StarReward,
)
from aigis_ps.quest_entries import STAR_UNBOUNDED, EnemyBand
from aigis_ps.quest_mode_subjugation import ROW_VALUES as SCORE_ROW_VALUES
from aigis_ps.quest_modes import SLOT_FLAGS
from aigis_ps.resource_library import ResourceLibrary
from test_drop_bonus import (
    BANNER, CRYSTAL, CRYSTAL_TEXT, CRYSTAL_TYPE, MONDAY, NATIVE_COLUMNS as BONUS_COLUMNS, SUNDAY_NIGHT, row as bonus_row,
    table_bytes as bonus_table,
)


ITEM_NAME = "gift_0002_ruby"
ITEM_HASH = (zlib.crc32(ITEM_NAME.encode()) ^ 0x80000000) - 0x80000000
ENTRY_FIELDS = (("EnemyID", 1), ("PrizeCardID", 1), ("Loop", 1), ("IsAppear", 32),
                ("PrizeEnemySpawnPercent", 1), ("PrizeEnemyDropPercent", 1))
DROP_FIELDS = tuple((name, 1) for name in ("DropID", "DropRange", "DropType", "ItemID", "DropCount"))
TEXT_KIND, DATE_KIND = 32, 9
UNIT_FIELDS = (("ListID", 1), ("CardID", 1), ("CostMod", 1))
MISSION_CONFIG_FIELDS = (("MissionID", 1), ("Enable", 1), ("StartDateTime", DATE_KIND), ("EndDateTime", DATE_KIND))
MISSION_LIST_FIELDS = (("MissionID", 1), ("QuestID", 1))
SYSTEM_TEXT_FIELDS = (("Recode_Index", 1), ("Data_Text", TEXT_KIND))
DAILY_TYPE, TUTORIAL_TYPE = 2, 4
CARD_DROP_TYPE, VARIANT_CARD_DROP_TYPE, STOCKED_DROP_TYPE = 1, 2, 8
PERSON_ARCHIVE, PERSON_TABLE = "person.aar", "Person.atb"
PERSON_ROWS = ({"ListID": 3, "CardID": 4, "CostMod": -1}, {"ListID": 3, "CardID": 1062, "CostMod": 2})
AT_LEAST_THREE = "GetNum_QuestStar() >= 3;"
# Score quests keep a packed MaxRank: the running total above bit 12 and the best run below it.
PACKED_RANK = (1127 << 12) | 1127
STAGE_VARIANT_ENEMY, LAST_STAGE_VARIANT_ENEMY, SPECIAL_ENEMY, DATED_ENEMY = 6000, 6999, 7003, 8000
OVERRIDE_CODE, UNKNOWN_CODE = 1004, 999999


def table(fields, rows):
    offsets, stride = [], 0
    for _, kind in fields:
        offsets.append(stride)
        stride += 8 if kind == DATE_KIND else 4
    schema = bytearray(struct.pack("<4sBBHH", b"ALRD", 1, 0, len(fields), stride))
    for (name, kind), offset in zip(fields, offsets):
        encoded = name.encode()
        parameters = b"" if kind == TEXT_KIND else b"\0" * 8
        schema += struct.pack("<HBBBB", offset, kind, len(parameters), len(encoded), 0) + encoded + b"\0\0"
        schema += b"\0" * (-len(schema) % 4) + parameters
        schema += b"\0" * (-len(schema) % 4)
    records, pool = bytearray(), bytearray()
    for row in rows:
        for name, kind in fields:
            value = row[name]
            if kind == TEXT_KIND:
                records += struct.pack("<I", len(pool))
                if value is not None:
                    pool += value.encode() + b"\0"
            elif kind == DATE_KIND:
                records += struct.pack("<II", *value)
            else:
                records += struct.pack("<i", value)
    if not pool:
        start = 20 + len(schema)
        return struct.pack("<4sBBHHHI4s", b"ALTB", 1, 0x10, len(rows), 20, start, stride, b"test") + schema + records
    start = 28 + len(schema)
    return struct.pack("<4sBBHHHIII4s", b"ALTB", 1, 0x14, len(rows), 28, start, stride, len(pool), start + len(records), b"test") + schema + records + pool


def archive(members):
    start = 32 + 64 * len(members)
    result = bytearray(start)
    struct.pack_into("<4sBBH", result, 0, b"ALAR", 3, 0x41, len(members))
    struct.pack_into("<H", result, 16, start)
    for index, (name, payload) in enumerate(members):
        position = 32 + 64 * index
        struct.pack_into("<H", result, 18 + 2 * index, position)
        struct.pack_into("<IIIIH", result, position, index, len(result), len(payload), 0x80000001, 0)
        encoded = name.encode() + b"\0"
        result[position + 18:position + 18 + len(encoded)] = encoded
        result += payload
    return bytes(result)


def compressed(payload):
    return struct.pack("<4sHHI", b"ALL4", 1, 0, len(payload)) + lz4.frame.compress(payload)


def entry(slot=1, *, enemy=2, loop=1, condition="", spawn=0, drop=0):
    return dict(zip((name for name, _ in ENTRY_FIELDS), (enemy, slot, loop, condition, spawn, drop)))


CONDITIONAL_ROWS = (entry(1, enemy=SPECIAL_ENEMY, condition=AT_LEAST_THREE, drop=OVERRIDE_CODE), entry(2),
                    entry(3, enemy=STAGE_VARIANT_ENEMY), entry(1, enemy=SPECIAL_ENEMY), entry(6))


def drop_rule(identifier=1, upper=1000, kind=1, item_id=0, count=1):
    return dict(zip((name for name, _ in DROP_FIELDS), (identifier, upper, kind, item_id, count)))


def quest(**changes):
    result = {"QuestID": 1001, "Type": 0, "MapNo": 7, "EntryNo": 2, "LocationNo": 1, "Capacity": 5,
              "AppearCondition": 0, "Charisma": 5, "ActionPoint": 0, "Rank": 1, "RankExp": 17,
              "Gold": 450, "UnitLevel": 2, "Treasure1": 4, "Treasure2": 1003, "Treasure3": 0,
              "Treasure4": 0, "Treasure5": 1001062, "QuestTerms": 9, "UnitList": 12,
              "StartOpenDay": 0, "EndOpenDay": 0, "OpenWeek": 0, "IsUseRaidTeam": 0}
    result.update(changes)
    return result


def snapshot(quests=None, *, traits=()):
    quests = [quest(), quest(QuestID=1002, Type=2)] if quests is None else quests
    cards = dumps(Container(children=(Column("CardID", "I", (1, 4, 1062)), Column("InitClassID", "I", (1, 2, 3)),
        Column("ClassLV0SkillID", "I", (0, 0, 0)), Column("ClassLV1SkillID", "I", (0, 0, 0)), *traits)))
    document = dumps(Container(children=tuple(Column(name, "S" if isinstance(quests[0][name], str) else "I",
                                                     tuple(row[name] for row in quests)) for name in quests[0])))
    return MasterSnapshot("fixture-master", "a" * 64, {"CardData": cards, "QuestTable": document})


@pytest.fixture
def setup(tmp_path):
    rows = [entry(0, enemy=1000), entry(1, loop=2), entry(2)]
    resources = {"Map0007.aar": archive([("Entry02.atb", table(ENTRY_FIELDS, rows))]),
        "ItemDropConfigTable.atb": table(DROP_FIELDS, [drop_rule(), drop_rule(1000000, 2000000, 2),
                                                     drop_rule(1003, 0, 3, ITEM_HASH, 3)]),
        "ItemConfigTable.atb": table((("ItemID", 32), ("ID", 1), ("Stack", 1)),
                                     [{"ItemID": ITEM_NAME, "ID": ITEM_HASH, "Stack": 999}])}
    possessions = PossessionService(tmp_path, asset_reader=resources.__getitem__)
    return resources, possessions


def catalog(setup, master=None):
    resources, possessions = setup
    return QuestCatalog(master or snapshot(), resources.__getitem__, possessions=possessions, resource_version="fixture-resources")


def with_fixed_team(setup):
    setup[0]["QuestUnitTable.atb"] = table(UNIT_FIELDS, [{"ListID": quest()["UnitList"], "CardID": 4, "CostMod": 0}])
    return setup


def test_catalog_is_lazy_and_reports_map_validation_separately(setup):
    resources, possessions = setup
    reads = []
    def read(name):
        reads.append(name)
        return resources[name]
    subject = QuestCatalog(snapshot(), read, possessions=possessions, resource_version="fixed-version")
    assert subject.all_ids == (1001,)
    assert reads == [] and subject.status()["validated_quests"] == 0
    assert subject.status()["all_maps_validated"] is False
    definition = subject.get(1001)
    assert (definition.quest_id, definition.prerequisite, definition.charisma, definition.action_points,
            definition.rank, definition.rank_exp, definition.gold, definition.unit_level) == (1001, 0, 5, 0, 1, 17, 450, 2)
    assert (definition.map_no, definition.entry_no, definition.quest_terms, definition.unit_list, definition.location_no) == (7, 2, 9, 12, 1)
    assert definition.treasures == (4, 1003, 0, 0, 1001062)
    assert definition.candidates == (DropCandidate(1, 1, 0, 2, 0), DropCandidate(1, 1, 1, 2, 0), DropCandidate(2, 2, 0, 2, 0))
    assert reads == ["ItemDropConfigTable.atb", "Map0007.aar"]
    assert subject.get(1001) is definition
    assert reads == ["ItemDropConfigTable.atb", "Map0007.aar"]
    assert subject.status()["all_maps_validated"] is True
    with pytest.raises(FrozenInstanceError):
        definition.gold = 999
    with pytest.raises(FrozenInstanceError):
        definition.candidates[0].slot = 3


def test_reward_decoding_uses_native_ranges_card_variant_and_canonical_item(setup):
    subject = catalog(setup)
    assert subject.decode_reward(0) == Reward("none", 0, 0)
    assert subject.decode_reward(4) == Reward("unit", 4, 1, card_id=4, drop_type=CARD_DROP_TYPE)
    assert subject.decode_reward(1001062) == Reward("unit", 1001062, 1, card_id=1062, drop_type=VARIANT_CARD_DROP_TYPE)
    reward = subject.decode_reward(1003)
    assert reward == Reward("item", 1003, 3, item_id=ITEM_NAME, drop_type=GIFT_DROP_TYPE)
    with pytest.raises(FrozenInstanceError):
        reward.count = 999
    with pytest.raises(FrozenInstanceError):
        reward.drop_type = STOCKED_DROP_TYPE


def test_rewards_carry_the_drop_type_of_the_row_they_matched(setup):
    setup[0]["ItemDropConfigTable.atb"] = table(DROP_FIELDS, [drop_rule(100, 105, GIFT_DROP_TYPE, ITEM_HASH, 2),
        drop_rule(103, 0, STOCKED_DROP_TYPE, ITEM_HASH, 7), drop_rule(107, 0, STOCKED_DROP_TYPE, ITEM_HASH, 5),
        drop_rule(108, 0, EVENT_PARAM_DROP_TYPE)])
    subject = catalog(setup)
    decoded = {code: subject.decode_reward(code) for code in (0, 100, 103, 105, 107, 108)}
    assert {code: reward.drop_type for code, reward in decoded.items()} == {
        0: None, 100: GIFT_DROP_TYPE, 103: GIFT_DROP_TYPE, 105: GIFT_DROP_TYPE, 107: STOCKED_DROP_TYPE,
        108: EVENT_PARAM_DROP_TYPE}
    assert (decoded[103].count, decoded[107].count, decoded[108].kind) == (2, 5, "event_param")


@pytest.mark.parametrize("outer,inner", [(False, True), (True, False), (True, True)])
def test_map_and_entry_all4_layers_are_independently_supported(setup, outer, inner):
    payload = table(ENTRY_FIELDS, [entry(1)])
    if inner:
        payload = compressed(payload)
    payload = archive([("Entry02.atb", payload)])
    setup[0]["Map0007.aar"] = compressed(payload) if outer else payload
    assert catalog(setup).get(1001).candidates == (DropCandidate(1, 0, 0, 2, 0),)


def test_native_null_appearance_string_is_an_empty_condition(setup):
    setup[0]["Map0007.aar"] = archive([("Entry02.atb", table(ENTRY_FIELDS, [entry(1, condition=None)]))])
    assert catalog(setup).get(1001).candidates == (DropCandidate(1, 0, 0, 2, 0),)


def test_ordinary_enemy_ignores_special_enemy_drop_override(setup):
    setup[0]["Map0007.aar"] = archive([("Entry02.atb", table(ENTRY_FIELDS, [entry(1, drop=100)]))])
    assert catalog(setup).get(1001).candidates == (DropCandidate(1, 0, 0, 2, 0),)


@pytest.mark.parametrize("row", [entry(1, enemy=DATED_ENEMY), entry(1, condition="GetNum_QuestStar() != 3;"),
                                 entry(1, condition="SomeCondition"),
                                 entry(1, enemy=SPECIAL_ENEMY, condition="GetNum_QuestStar(4807) >= 3;"), entry(256)])
def test_unverified_candidate_branches_are_not_silently_omitted(setup, row):
    setup[0]["Map0007.aar"] = archive([("Entry02.atb", table(ENTRY_FIELDS, [entry(1), row]))])
    subject = catalog(setup)
    with pytest.raises(ProtocolError) as error:
        subject.get(1001)
    assert (error.value.code, error.value.status) == ("quest_candidate_unsupported", 501)
    assert subject.status()["validated_quests"] == 0
    assert subject.status()["all_maps_validated"] is False
    assert subject.status()["failed_quests"][1001]["code"] == error.value.code
    assert subject.status()["excluded_prize_rows"] == {}


@pytest.mark.parametrize("row", [entry(-1), entry(1, loop=0), entry(1, loop=-1), entry(1, loop=MAX_CANDIDATES + 1),
                                 entry(1, enemy=0), entry(1, enemy=-1), entry(1, spawn=-1),
                                 entry(1, enemy=SPECIAL_ENEMY, drop=-1)])
def test_invalid_candidate_counts_and_identifiers_are_rejected(setup, row):
    setup[0]["Map0007.aar"] = archive([("Entry02.atb", table(ENTRY_FIELDS, [row]))])
    with pytest.raises(ProtocolError) as error:
        catalog(setup).get(1001)
    assert (error.value.code, error.value.status) == ("quest_entry_invalid", 503)


def candidate_catalog(setup, rows=CONDITIONAL_ROWS):
    resources = setup[0]
    resources["ItemDropConfigTable.atb"] = table(DROP_FIELDS, [drop_rule(), drop_rule(1000000, 2000000, 2),
        drop_rule(1003, 0, GIFT_DROP_TYPE, ITEM_HASH, 3), drop_rule(OVERRIDE_CODE, 0, GIFT_DROP_TYPE, ITEM_HASH, 2)])
    resources["Map0007.aar"] = archive([("Entry02.atb", table(ENTRY_FIELDS, list(rows)))])
    return catalog(setup)


def test_special_enemies_without_an_override_fall_back_to_their_slot_treasure(setup):
    quest = candidate_catalog(setup, [entry(1, enemy=SPECIAL_ENEMY)]).get(1001)
    candidate, = quest.candidates
    assert candidate == DropCandidate(1, 0, 0, SPECIAL_ENEMY, SPECIAL_ENEMY_ATTR)
    assert not quest.conditional and quest.candidates_for(0) == quest.candidates
    assert (quest.treasure_code(candidate), quest.reward_code(candidate), quest.marks_slot(candidate)) == (4, 4, True)


def test_star_conditions_bound_when_a_prize_candidate_appears(setup):
    quest = candidate_catalog(setup, [entry(1, condition=AT_LEAST_THREE)]).get(1001)
    assert quest.candidates == (DropCandidate(1, 0, 0, 2, 0, 3, STAR_UNBOUNDED),)
    assert quest.conditional and quest.candidates_for(2) == ()
    assert quest.candidates_for(3) == quest.candidates_for(PACKED_RANK) == quest.candidates


def test_stage_variant_enemies_drop_only_below_three_stars(setup):
    rows = [entry(1, enemy=STAGE_VARIANT_ENEMY),
            entry(2, enemy=LAST_STAGE_VARIANT_ENEMY, condition="GetNum_QuestStar() == 1;")]
    quest = candidate_catalog(setup, rows).get(1001)
    variant, single = quest.candidates
    assert (variant, single) == (DropCandidate(1, 0, 0, STAGE_VARIANT_ENEMY, 0, 0, 2),
                                 DropCandidate(2, 1, 0, LAST_STAGE_VARIANT_ENEMY, 0, 1, 1))
    assert [quest.candidates_for(stars) for stars in (0, 1, 2, 3)] == [(variant,), (variant, single), (variant,), ()]


def test_entries_without_an_appearance_column_are_unconditional(setup):
    fields = tuple(field for field in ENTRY_FIELDS if field[0] != "IsAppear")
    setup[0]["Map0007.aar"] = archive([("Entry02.atb", table(fields, [entry(1), entry(2, enemy=SPECIAL_ENEMY)]))])
    quest = catalog(setup).get(1001)
    assert quest.candidates == (DropCandidate(1, 0, 0, 2, 0), DropCandidate(2, 1, 0, SPECIAL_ENEMY, SPECIAL_ENEMY_ATTR))
    assert not quest.conditional and quest.candidates_for(PACKED_RANK) == quest.candidates


def test_candidates_for_the_record_max_rank_keep_entry_order(setup):
    quest = candidate_catalog(setup).get(1001)
    special, second, variant, fallback, sixth = quest.candidates
    assert special == DropCandidate(1, 0, 0, SPECIAL_ENEMY, SPECIAL_ENEMY_ATTR, 3, STAR_UNBOUNDED, OVERRIDE_CODE)
    assert (variant.max_stars, fallback.override_code, sixth.slot) == (2, 0, 6) and quest.conditional
    assert quest.candidates_for(0) == quest.candidates_for(2) == (second, variant, fallback, sixth)
    assert quest.candidates_for(3) == quest.candidates_for(PACKED_RANK) == (special, second, fallback, sixth)
    assert quest.candidates_for(STAR_UNBOUNDED) == quest.candidates_for(3)


@pytest.mark.parametrize("max_rank", [-1, True, 3.0, None, "3", 2 ** 31])
def test_candidate_selection_requires_a_raw_nonnegative_max_rank(setup, max_rank):
    with pytest.raises(ProtocolError) as error:
        candidate_catalog(setup).get(1001).candidates_for(max_rank)
    assert (error.value.code, error.value.status) == ("quest_state_invalid", 503)


def test_overrides_grant_their_own_code_without_the_slot_bit(setup):
    quest = candidate_catalog(setup).get(1001)
    special, second, variant, fallback, sixth = quest.candidates
    assert [quest.reward_code(candidate) for candidate in quest.candidates] == [OVERRIDE_CODE, 1003, 0, 4, 0]
    assert [quest.marks_slot(candidate) for candidate in quest.candidates] == [False, True, True, True, False]
    assert [quest.treasure_code(candidate) for candidate in (second, fallback, sixth)] == [1003, 4, 0]
    # Callers still on treasure_code must fail rather than grant the slot treasure for an override or a hidden drop.
    for candidate in (special, variant, replace(second, override_code=OVERRIDE_CODE), replace(second, min_stars=1)):
        with pytest.raises(ProtocolError) as error:
            quest.treasure_code(candidate)
        assert (error.value.code, error.value.status) == ("quest_candidate_unsupported", 501)


def test_only_the_five_treasure_slots_grant_a_treasure_and_mark_the_record(setup):
    definition = candidate_catalog(setup, [entry(5), entry(6), entry(5, enemy=SPECIAL_ENEMY, drop=OVERRIDE_CODE)]).get(1001)
    last_treasure, beyond = definition.candidates[:2]
    assert [definition.reward_code(candidate) for candidate in definition.candidates] == [
        quest()["Treasure5"], 0, OVERRIDE_CODE]
    assert [definition.marks_slot(candidate) for candidate in definition.candidates] == [True, False, False]
    assert (definition.treasure_code(last_treasure), definition.treasure_code(beyond)) == (quest()["Treasure5"], 0)


def test_override_codes_are_decoded_with_the_definition(setup):
    subject = candidate_catalog(setup, [entry(1), entry(1, enemy=SPECIAL_ENEMY, drop=UNKNOWN_CODE)])
    with pytest.raises(ProtocolError) as error:
        subject.get(1001)
    assert (error.value.code, error.value.status) == ("quest_reward_unknown", 503)
    assert subject.status()["failed_quests"][1001]["code"] == "quest_reward_unknown"


@pytest.mark.parametrize("row,band", [
    (entry(1, enemy=1000), EnemyBand.SKIPPED), (entry(2, enemy=9000), EnemyBand.SKIPPED),
    (entry(1, enemy=9000, condition="SomeCondition"), EnemyBand.SKIPPED),
    (entry(1, enemy=STAGE_VARIANT_ENEMY, condition=AT_LEAST_THREE), EnemyBand.STAGE_VARIANT),
    (entry(1, condition="GetNum_QuestStar() < 0;"), EnemyBand.ORDINARY),
])
def test_prize_rows_the_native_filter_never_admits_are_counted_not_refused(setup, row, band):
    subject = candidate_catalog(setup, [entry(1), row, row])
    assert subject.get(1001).candidates == (DropCandidate(1, 0, 0, 2, 0),)
    assert subject.status()["excluded_prize_rows"] == {"Map0007.aar/Entry02.atb": {band.value: 2}}
    assert subject.status()["conditional_quests"] == []


def test_status_lists_conditional_quests_and_excluded_rows_once_their_maps_load(setup):
    subject = candidate_catalog(setup, [*CONDITIONAL_ROWS, entry(2, enemy=9000)])
    status = subject.status()
    assert (status["excluded_prize_rows"], status["conditional_quests"]) == ({}, [])
    assert status["drop_bonus"] == {"loaded": False, "available": None, "error": None, "failures": 0, "sha256": None}
    subject.get(1001)
    status = subject.status()
    assert status["excluded_prize_rows"] == {"Map0007.aar/Entry02.atb": {EnemyBand.SKIPPED.value: 1}}
    assert status["conditional_quests"] == [1001] and status["validated_quests"] == 1


def test_candidate_views_show_the_rt_rows_a_record_expects(setup):
    subject = candidate_catalog(setup)
    record = {"QuestID": 1001, "MaxRank": 3, "AchieveFlag": SLOT_FLAGS[0]}
    view = subject.candidate_view(1001, 3, record)
    assert (view["quest_id"], view["max_rank"], view["conditional"], view["withheld"]) == (1001, 3, True, 1)
    assert [(row["id"], row["type"], row["attr"], row["enemy_id"], row["band"], row["entry_row"], row["reward_code"],
             row["marks_slot"], row["min_stars"]) for row in view["candidates"]] == [
        (0, 1, SPECIAL_ENEMY_ATTR | 1, SPECIAL_ENEMY, EnemyBand.SPECIAL.value, 0, OVERRIDE_CODE, False, 3),
        (1, 2, 0, 2, EnemyBand.ORDINARY.value, 1, 1003, True, 0),
        (2, 1, SPECIAL_ENEMY_ATTR | 1, SPECIAL_ENEMY, EnemyBand.SPECIAL.value, 3, 4, True, 0),
        (3, 6, 1, 2, EnemyBand.ORDINARY.value, 4, 0, False, 0)]
    fresh = subject.candidate_view(1001, 0)
    assert [(row["type"], row["attr"], row["band"], row["max_stars"]) for row in fresh["candidates"]] == [
        (2, 0, EnemyBand.ORDINARY.value, STAR_UNBOUNDED), (3, 0, EnemyBand.STAGE_VARIANT.value, 2),
        (1, SPECIAL_ENEMY_ATTR, EnemyBand.SPECIAL.value, STAR_UNBOUNDED), (6, 0, EnemyBand.ORDINARY.value, STAR_UNBOUNDED)]
    looped = candidate_catalog(setup, [entry(2, loop=2)]).candidate_view(1001, 0)
    assert [(row["id"], row["entry_row"], row["loop_index"]) for row in looped["candidates"]] == [(0, 0, 0), (1, 0, 1)]
    for max_rank, saved in ((-1, None), (True, None), (3, {"AchieveFlag": "1"}), (3, [record]), (3, {})):
        with pytest.raises(ProtocolError) as error:
            subject.candidate_view(1001, max_rank, saved)
        assert (error.value.code, error.value.status) == ("quest_candidate_view_invalid", 400)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "bad_archive", "bad_table", "missing_column"])
def test_invalid_map_structure_is_explicit(setup, fault):
    resources = setup[0]
    payload = table(ENTRY_FIELDS, [entry(1)])
    if fault == "missing":
        resources["Map0007.aar"] = archive([("Entry03.atb", payload)])
    elif fault == "duplicate":
        resources["Map0007.aar"] = archive([("Entry02.atb", payload), ("Entry02.atb", payload)])
    elif fault == "bad_archive":
        resources["Map0007.aar"] = b"bad"
    elif fault == "bad_table":
        resources["Map0007.aar"] = archive([("Entry02.atb", b"bad")])
    else:
        resources["Map0007.aar"] = archive([("Entry02.atb", table(ENTRY_FIELDS[1:], [entry(1)]))])
    with pytest.raises(ProtocolError) as error:
        catalog(setup).get(1001)
    assert error.value.code == "quest_entry_invalid"


@pytest.mark.parametrize("code,status", [(-1, 400), (True, 400), (2 ** 31, 400), (999999, 503),
                                        (1000000, 501), (1100004, 501), (2000000, 503), (999, 503)])
def test_reward_boundaries_do_not_invent_cards(setup, code, status):
    with pytest.raises(ProtocolError) as error:
        catalog(setup).decode_reward(code)
    assert error.value.status == status


@pytest.mark.parametrize("exact_code", [103, 105])
def test_drop_range_is_an_absolute_upper_bound_and_takes_priority_over_exact_rows(setup, exact_code):
    setup[0]["ItemDropConfigTable.atb"] = table(DROP_FIELDS, [drop_rule(100, 105, 3, ITEM_HASH, 2),
        drop_rule(exact_code, 0, 3, ITEM_HASH, 7), drop_rule(107, 0, 3, ITEM_HASH, 5)])
    subject = catalog(setup)
    assert subject.decode_reward(100).count == subject.decode_reward(105).count == 2
    assert subject.decode_reward(exact_code).count == 2
    assert subject.decode_reward(107).count == 5
    with pytest.raises(ProtocolError) as error:
        subject.decode_reward(106)
    assert error.value.code == "quest_reward_unknown"


@pytest.mark.parametrize("rules,code,expected", [([drop_rule(1003, 0, 8)], 1003, "quest_reward_unsupported"),
    ([drop_rule(1003, 0, 3, 1234)], 1003, "possession_item_unknown"),
    ([drop_rule(100, 110, 3, ITEM_HASH), drop_rule(101, 120, 3, ITEM_HASH)], 105, "quest_reward_unknown"),
    ([drop_rule(100, 110, 3, ITEM_HASH), drop_rule(101, 120, 3, ITEM_HASH), drop_rule(105, 0, 3, ITEM_HASH)], 105, "quest_reward_unknown"),
    ([drop_rule(), drop_rule()], 4, "quest_reward_config_invalid"),
    ([drop_rule(1003, 1002, 3, ITEM_HASH)], 1003, "quest_reward_config_invalid"),
    ([drop_rule(1003, 0, 3, ITEM_HASH, 0)], 1003, "quest_reward_config_invalid")])
def test_unsupported_or_ambiguous_reward_rules_are_explicit(setup, rules, code, expected):
    setup[0]["ItemDropConfigTable.atb"] = table(DROP_FIELDS, rules)
    with pytest.raises(ProtocolError) as error:
        catalog(setup).decode_reward(code)
    assert error.value.code == expected


@pytest.mark.parametrize("changes,expected", [({"AppearCondition": 9999}, "quest_prerequisite_unsupported"),
    ({"AppearCondition": 1001}, "quest_definition_invalid"), ({"Charisma": -1}, "quest_definition_invalid"),
    ({"UnitLevel": -1}, "quest_definition_invalid"), ({"MapNo": 0}, "quest_definition_invalid"),
    ({"OpenWeek": 1}, "quest_condition_unsupported"), ({"_HoldingParam": -1}, "quest_condition_unsupported"),
    ({"StartOpenDay": 10}, "quest_condition_unsupported"), ({"IsUseRaidTeam": 1}, "quest_condition_unsupported"),
    ({"IsUseRaidTeam": -1}, "quest_condition_unsupported")])
def test_unverified_quest_participation_fields_cannot_disappear(setup, changes, expected):
    with pytest.raises(ProtocolError) as error:
        catalog(setup, snapshot([quest(**changes)])).get(1001)
    assert error.value.code == expected


@pytest.mark.parametrize("slot,spawn", [(4, 0), (5, 0), (8, 0), (41, 0), (1, 5)])
def test_native_prize_slots_and_unread_spawn_column_build_candidates(setup, slot, spawn):
    setup[0]["Map0007.aar"] = archive([("Entry02.atb", table(ENTRY_FIELDS, [entry(1), entry(slot, spawn=spawn)]))])
    assert catalog(setup).get(1001).candidates == (DropCandidate(1, 0, 0, 2, 0), DropCandidate(slot, 1, 0, 2, 0))


def test_candidate_on_an_empty_treasure_slot_is_kept_for_its_record_bit(setup):
    definition = catalog(setup, snapshot([quest(Treasure1=0)])).get(1001)
    assert definition.treasures[0] == 0 and definition.candidates[0].slot == 1


def test_missing_assets_can_be_retried_without_claiming_prior_validation(setup):
    resources = setup[0]
    saved = resources.pop("Map0007.aar")
    subject = catalog(setup)
    with pytest.raises(ProtocolError) as error:
        subject.get(1001)
    assert error.value.code == "quest_asset_unavailable" and not subject.status()["ready"]
    resources["Map0007.aar"] = saved
    subject.get(1001)
    assert subject.status()["ready"] and subject.status()["failed_quests"] == {}


def test_catalog_rejects_other_modes_and_uses_frozen_master_snapshot(setup):
    disabled = min(quest_type for quest_type, entry in quest_modes.registry().items() if not entry.enabled)
    master = snapshot([quest(), quest(QuestID=1002, Type=disabled)])
    before = deepcopy(dict(master.documents))
    subject = catalog(setup, master)
    with pytest.raises(ProtocolError) as error:
        subject.get(1002)
    assert error.value.code == "quest_mode_unsupported"
    assert subject.status()["mode_failed_quests"] == {}
    with pytest.raises(ProtocolError) as error:
        subject.get(9999)
    assert error.value.code == "quest_not_found"
    subject.get(1001)
    assert dict(master.documents) == before


def test_main_and_mode_rows_build_the_same_definition(setup):
    subject = catalog(with_fixed_team(setup), snapshot([quest(), quest(QuestID=1002, Type=TUTORIAL_TYPE)]))
    main, mode = subject.get(1001), subject.get(1002)
    assert replace(mode, quest_id=main.quest_id) == main
    status = subject.status()
    assert status["entry_sha256"] == status["mode_entry_sha256"] and list(status["entry_sha256"]) == ["Map0007.aar/Entry02.atb"]
    assert (status["validated_quests"], status["validated_mode_quests"]) == (1, 1)


@pytest.mark.parametrize("changes,main_code,mode_code", [
    ({"Rank": 0}, "quest_definition_invalid", None),
    ({"AppearCondition": 1003}, "quest_prerequisite_unsupported", None),
    ({"OpenWeek": 8}, "quest_condition_unsupported", "quest_condition_unsupported"),
    ({"IsUseRaidTeam": 1}, "quest_condition_unsupported", "quest_condition_unsupported"),
    ({"IsUseRaidTeam": -1}, "quest_condition_unsupported", "quest_condition_unsupported"),
    ({"Charisma": -1}, "quest_definition_invalid", "quest_definition_invalid"),
])
def test_main_quests_add_only_rank_and_prerequisite_rules_to_the_shared_contract(setup, changes, main_code, mode_code):
    rows = [quest(**changes), quest(QuestID=1002, Type=TUTORIAL_TYPE, **changes), quest(QuestID=1003, Type=TUTORIAL_TYPE)]
    subject = catalog(with_fixed_team(setup), snapshot(rows))
    for quest_id, code, failures in ((1001, main_code, "failed_quests"), (1002, mode_code, "mode_failed_quests")):
        if code is None:
            assert subject.get(quest_id).quest_id == quest_id
            continue
        with pytest.raises(ProtocolError) as error:
            subject.get(quest_id)
        assert error.value.code == code and subject.status()[failures][quest_id]["code"] == code


def test_only_timed_modes_keep_their_schedule_columns(setup):
    rows = [quest(), quest(QuestID=1002, Type=DAILY_TYPE, UnitList=0, OpenWeek=8)]
    assert catalog(setup, snapshot(rows)).get(1002).quest_id == 1002


def test_mode_failures_are_recorded_and_cleared_like_main_failures(setup):
    resources = with_fixed_team(setup)[0]
    saved = resources.pop("Map0007.aar")
    subject = catalog(setup, snapshot([quest(), quest(QuestID=1002, Type=TUTORIAL_TYPE)]))
    for quest_id in (1001, 1002):
        with pytest.raises(ProtocolError) as error:
            subject.get(quest_id)
        assert error.value.code == "quest_asset_unavailable"
    status = subject.status()
    assert status["failed_quests"][1001]["code"] == status["mode_failed_quests"][1002]["code"] == "quest_asset_unavailable"
    resources["Map0007.aar"] = saved
    assert subject.get(1001).candidates == subject.get(1002).candidates
    assert subject.status()["failed_quests"] == subject.status()["mode_failed_quests"] == {}


@pytest.mark.parametrize("end", [[0xFFFFFFFF, 0xFFFFFFFF], [0, 0x20000]])
def test_mission_dates_past_the_calendar_are_mode_resource_errors(setup, end):
    resources = setup[0]
    resources["StoryMissionConfig.atb"] = table(MISSION_CONFIG_FIELDS, [
        {"MissionID": 1, "Enable": 1, "StartDateTime": [0, 0], "EndDateTime": end}])
    resources["StoryMissionQuestList.atb"] = table(MISSION_LIST_FIELDS, [{"MissionID": 1, "QuestID": 1001}])
    subject = catalog(setup)
    for action in (lambda: subject.mission_windows(0), lambda: subject.playable_quests(0), lambda: subject.mode(1001)):
        with pytest.raises(ProtocolError) as error:
            action()
        assert (error.value.code, error.value.status) == ("quest_mode_resource_invalid", 503)
    assert subject.status()["mode_failed_quests"][1001]["code"] == "quest_mode_resource_invalid"


@pytest.mark.parametrize("kind,identifier", [(1, 101), (TEXT_KIND, None)])
def test_gift_star_rewards_reject_implant_identifiers_that_are_not_text(setup, kind, identifier):
    resources = setup[0]
    resources["cfgImplantText.atb"] = table((("_ID", kind), ("_TextID", 1)), [{"_ID": identifier, "_TextID": 0}])
    resources["SystemText.atb"] = table(SYSTEM_TEXT_FIELDS, [{"Recode_Index": 0, "Data_Text": "gift"}])
    row = {**quest(), "BonusType": GIFT_REWARD_TYPES[0], "BonusNum": 1, "NormalMode_RewardItemID": "null"}
    with pytest.raises(ProtocolError) as error:
        catalog(setup).star_rewards(row, False)
    assert (error.value.code, error.value.status) == ("quest_reward_config_invalid", 503)


def test_quest_id_lists_split_on_commas_and_skip_empty_items():
    rows = [{"MissionID": 16000, "QuestIdList": "16001, 16002,"}, {"MissionID": 16001, "QuestIdList": ""}]
    assert QuestCatalog._quest_id_list_members(rows) == [{"MissionID": 16000, "QuestID": 16001},
                                                         {"MissionID": 16000, "QuestID": 16002}]
    for text in ("16001;16002", None, "１", "16001 16002,", "16001,+16002"):
        with pytest.raises(ProtocolError) as error:
            QuestCatalog._quest_id_list_members([{"MissionID": 1, "QuestIdList": text}])
        assert error.value.code == "quest_mode_resource_invalid"


def test_system_texts_follow_row_positions_and_card_traits_are_optional_columns(setup):
    resources, _ = setup
    resources["SystemText.atb"] = table(SYSTEM_TEXT_FIELDS,
                                        [{"Recode_Index": 0, "Data_Text": "zero"}, {"Recode_Index": 5, "Data_Text": "one"}])
    plain = catalog(setup)
    assert (plain.system_text(1), plain.system_text(5), plain.system_text(-1), plain.system_text(True)) == ("one", None, None, None)
    for field in ("Genus", "Kind"):
        with pytest.raises(ProtocolError) as error:
            plain.card_ids_with(field, 0)
        assert error.value.code == "quest_card_trait_unknown"
    traits = (Column("Assign", "I", (1, 2, 2)), Column("Genus", "I", (0, 101, 0)), Column("Kind", "I", (0, 1, 1)))
    described = catalog(setup, snapshot(traits=traits))
    assert described.card_ids_with("Genus", 101) == {4} and described.card_ids_with("Assign", 2) == {4, 1062}
    assert described.card_ids_with("Kind", 1) == {4, 1062} and described.card_ids_with("Kind", 0) == {1}
    assert described.card_ids_with("Genus", 7) == frozenset()
    assert (described.quest_type(1002), described.quest_type(9999), described.quest_type("1002")) == (2, None, None)


def test_stack_zero_drop_items_resolve_for_uncapped_credit(setup, tmp_path):
    resources, _ = setup
    ticket = "item_smn_ticket_test"
    ticket_hash = (zlib.crc32(ticket.encode()) ^ 0x80000000) - 0x80000000
    resources = {**resources,
                 "ItemDropConfigTable.atb": table(DROP_FIELDS, [drop_rule(), drop_rule(1000000, 2000000, 2),
                                                                drop_rule(1003, 0, 8, ticket_hash, 1)]),
                 "ItemConfigTable.atb": table((("ItemID", 32), ("ID", 1), ("Stack", 1)),
                                              [{"ItemID": ITEM_NAME, "ID": ITEM_HASH, "Stack": 999},
                                               {"ItemID": ticket, "ID": ticket_hash, "Stack": 0}])}
    possessions = PossessionService(tmp_path, asset_reader=resources.__getitem__)
    subject = QuestCatalog(snapshot(), resources.__getitem__, possessions=possessions, resource_version="fixture-resources")
    assert subject.decode_reward(1003) == Reward("item", 1003, 1, item_id=ticket, drop_type=STOCKED_DROP_TYPE)


def test_archive_rows_read_one_member_once_and_record_the_archive_digest(setup):
    resources, possessions = setup
    unpacked = archive([("Other.atb", table(UNIT_FIELDS, PERSON_ROWS[:1])),
                        (PERSON_TABLE, compressed(table(UNIT_FIELDS, PERSON_ROWS)))])
    reads = []

    def read(name):
        reads.append(name)
        return resources[name]

    subject = QuestCatalog(snapshot(), read, possessions=possessions, resource_version="fixture-resources")
    with pytest.raises(ProtocolError) as error:
        subject.archive_rows(PERSON_ARCHIVE, PERSON_TABLE)
    assert error.value.code == "quest_asset_unavailable"
    resources[PERSON_ARCHIVE] = compressed(unpacked)
    rows = subject.archive_rows(PERSON_ARCHIVE, PERSON_TABLE)
    assert rows == PERSON_ROWS and subject.archive_rows(PERSON_ARCHIVE, PERSON_TABLE) is rows
    assert reads == [PERSON_ARCHIVE, PERSON_ARCHIVE]
    assert subject.status()["mode_resource_sha256"] == {PERSON_ARCHIVE: hashlib.sha256(unpacked).hexdigest()}
    with pytest.raises(TypeError):
        rows[0]["CardID"] = 1


@pytest.mark.parametrize("archive_name,member,code", [
    ("person.atb", PERSON_TABLE, "quest_mode_resource_invalid"),
    ("maps/person.aar", PERSON_TABLE, "quest_mode_resource_invalid"),
    (PERSON_ARCHIVE, "Person.aar", "quest_mode_resource_invalid"),
    (PERSON_ARCHIVE, "maps\\Person.atb", "quest_mode_resource_invalid"),
    (None, PERSON_TABLE, "quest_mode_resource_invalid"),
    (PERSON_ARCHIVE, 7, "quest_mode_resource_invalid"),
    ("absent.aar", PERSON_TABLE, "quest_asset_unavailable"),
    (PERSON_ARCHIVE, "Absent.atb", "quest_mode_resource_invalid"),
    ("twice.aar", PERSON_TABLE, "quest_mode_resource_invalid"),
    ("broken.aar", PERSON_TABLE, "quest_mode_resource_invalid"),
    ("garbage.aar", PERSON_TABLE, "quest_mode_resource_invalid"),
])
def test_archive_rows_refuse_unnamed_missing_or_malformed_members(setup, archive_name, member, code):
    resources = setup[0]
    payload = table(UNIT_FIELDS, PERSON_ROWS)
    resources.update({PERSON_ARCHIVE: archive([(PERSON_TABLE, payload)]),
                      "twice.aar": archive([(PERSON_TABLE, payload), (PERSON_TABLE, payload)]),
                      "broken.aar": archive([(PERSON_TABLE, b"bad")]), "garbage.aar": b"bad"})
    subject = catalog(setup)
    with pytest.raises(ProtocolError) as error:
        subject.archive_rows(archive_name, member)
    assert (error.value.code, error.value.status) == (code, 503)
    assert subject.status()["mode_resource_sha256"] == {}


@pytest.mark.parametrize("name", ["Person.aar", "maps/Person.atb", "maps\\Person.atb", None, 7])
def test_resource_rows_refuse_names_that_are_not_top_level_tables(setup, name):
    with pytest.raises(ProtocolError) as error:
        catalog(setup).resource_rows(name)
    assert (error.value.code, error.value.status) == ("quest_mode_resource_invalid", 503)


def test_missing_fixed_team_table_reports_the_missing_asset(setup):
    with pytest.raises(ProtocolError) as error:
        catalog(setup).fixed_units(12)
    assert error.value.code == "quest_asset_unavailable"


TAGGED_MISSION_FIELDS = MISSION_CONFIG_FIELDS + (("TagID", 1), ("TagIDRange", 1))
QUEST_ID_LIST_FIELDS = TAGGED_MISSION_FIELDS + (("QuestIdList", TEXT_KIND),)
# TowerMissionConfig carries no tag columns in the client resources.
UNTAGGED_CATEGORIES = frozenset((12,))
# Real masks enable platform bits 0-12.
NATIVE_MASK = 8191
MISSION_NOW = datetime(2026, 11, 2, 12, tzinfo=JST)
PAST_WINDOW = (datetime(2026, 1, 1, tzinfo=JST), datetime(2026, 1, 2, tzinfo=JST))
MISSION_QUESTS = [quest(), quest(QuestID=1002, Type=2), quest(QuestID=1003, Type=16), quest(QuestID=1004, Type=18),
                  quest(QuestID=1005, Type=TUTORIAL_TYPE, Rank=3)]


def mission_config(identifier, *, enable=NATIVE_MASK, tag=0, span=0):
    return {"MissionID": identifier, "Enable": enable, "StartDateTime": [0, 0], "EndDateTime": [0, 0],
            "TagID": tag, "TagIDRange": span}


REGISTRY_MISSIONS = MappingProxyType({
    0: ((mission_config(100001), (1001,)),),
    1: ((mission_config(200384, enable=0, tag=801), (1004,)),),
    5: ((mission_config(700101, tag=130, span=1), (1002,)),),
    7: ((mission_config(900001, tag=302), (1005,)),),
    14: ((mission_config(920001, tag=302), (1003,)), (mission_config(920014, tag=302), ())),
    16: ((mission_config(940001, tag=902), (1004,)), (mission_config(940012, tag=801, span=10), (1004,))),
})


def with_missions(setup, missions=REGISTRY_MISSIONS):
    resources = setup[0]
    for category in REGISTRY_CATEGORIES:
        listed = missions.get(category, ())
        config, *quest_list = mission_tables(category)
        if quest_list:
            fields = MISSION_CONFIG_FIELDS if category in UNTAGGED_CATEGORIES else TAGGED_MISSION_FIELDS
            resources[config] = table(fields, [row for row, _ in listed])
            resources[quest_list[0]] = table(MISSION_LIST_FIELDS, [{"MissionID": row["MissionID"], "QuestID": quest_id}
                                                                   for row, quest_ids in listed for quest_id in quest_ids])
        else:
            resources[config] = table(QUEST_ID_LIST_FIELDS, [{**row, "QuestIdList": ",".join(map(str, quest_ids))}
                                                            for row, quest_ids in listed])
    return setup


def mission_catalog(setup, missions=REGISTRY_MISSIONS):
    return catalog(with_missions(setup, missions), snapshot(MISSION_QUESTS))


def name_hash(text):
    return (zlib.crc32(text.encode()) ^ 0x80000000) - 0x80000000


def test_the_mission_registry_matches_every_type_window_mission_by_mission(setup):
    subject = mission_catalog(setup)
    registry = subject.mission_registry()
    assert registry.categories == REGISTRY_CATEGORIES and subject.mission_registry() is registry
    assert [mission.mission_id for mission in registry.ordered()] == [
        100001, 200384, 700101, 900001, 920001, 920014, 940001, 940012]
    compared = set()
    for quest_type in quest_modes.MODE_SPECS:
        windows = subject.mission_windows(quest_type)
        for mission in windows.ordered():
            assert registry.mission(mission.mission_id) == mission
            assert registry.members(mission.mission_id) == windows.members(mission.mission_id)
            assert registry.minimum_rank(mission.mission_id) == windows.minimum_rank(mission.mission_id)
            compared.add(mission.mission_id)
    assert compared == {mission.mission_id for mission in registry.ordered()}
    assert (registry.mission(940012).tag, registry.mission(940012).tag_span, registry.minimum_rank(900001)) == (801, 10, 3)
    assert registry.members(920001) == (1003,) and subject.mission_windows(18).mission(200384).enable == 0


def test_mission_tags_are_cached_and_previews_touch_no_catalog_cache(setup):
    subject = mission_catalog(setup)
    preview = subject.preview_mission_tags({900001: PAST_WINDOW})
    assert subject._tags is None and REGISTRY_CATEGORIES not in subject._windows
    assert dict(preview.overrides) == {900001: PAST_WINDOW}
    assert not preview.is_open("Tag", 302, now=MISSION_NOW, platform_code=1)
    tags = subject.mission_tags()
    assert subject.mission_tags() is tags and tags.registry is subject.mission_registry()
    assert tags.is_open("Tag", 302, now=MISSION_NOW, platform_code=1) and tags.tag_mission(801, 1).mission_id == 940012
    assert tags.categories_without_tag_column == UNTAGGED_CATEGORIES and tags.master_snapshot == "fixture-master"
    assert dict(tags.source_sha256) == {name: subject.resource_sha256(name) for name in REGISTRY_TABLES}
    cached = dict(subject._windows)
    again = subject.preview_mission_tags({900001: PAST_WINDOW})
    assert again is not preview and again.content_version == preview.content_version != tags.content_version
    assert subject.mission_tags() is tags and subject._windows.keys() == cached.keys()
    assert all(subject._windows[key] is windows for key, windows in cached.items())
    assert subject.preview_mission_tags(None).content_version == tags.content_version
    for overrides in ({900001: PAST_WINDOW[::-1]}, {0: (None, None)}, [900001]):
        with pytest.raises(ValueError):
            subject.preview_mission_tags(overrides)
    resources, possessions = setup
    overridden = QuestCatalog(snapshot(MISSION_QUESTS), resources.__getitem__, possessions=possessions,
                              resource_version="fixture-resources", window_overrides={900001: PAST_WINDOW})
    assert overridden.mission_tags().content_version == preview.content_version


def test_resource_row_substitutes_never_reach_mission_windows_or_the_registry(setup, monkeypatch):
    subject = mission_catalog(setup)
    original = subject.resource_rows

    def retagged(name):
        rows = original(name)
        if not name.endswith("MissionConfig.atb"):
            return rows
        return tuple(MappingProxyType({**row, "TagID": 999, "Enable": 0}) for row in rows)

    monkeypatch.setattr(subject, "resource_rows", retagged)
    assert subject.resource_rows("TutorialMissionConfig.atb")[0]["TagID"] == 999
    assert subject.mission_windows(TUTORIAL_TYPE).mission(900001).tag == 302
    assert subject.mission_registry().mission(900001).enabled(1)
    assert subject.mission_tags().tag_mission(302, 1).mission_id == 900001
    assert original("TutorialMissionConfig.atb")[0]["TagID"] == 302


def test_config_rows_absent_from_their_quest_list_never_resolve(setup):
    unlisted = (mission_config(800001, tag=302), ())
    subject = mission_catalog(setup, {**REGISTRY_MISSIONS, 7: (unlisted, *REGISTRY_MISSIONS[7])})
    tags = subject.mission_tags()
    assert tags.registry.mission(800001).enabled(1) and tags.registry.members(800001) == ()
    assert tags.tag_mission(302, 1).mission_id == 900001 and tags.lookup("ID", 800001, 1) is None
    assert tags.lookup("ID", 920014, 1).mission_id == 920014 and tags.status()["config_only"] == [800001]


def test_registry_failures_are_retried_and_cross_category_duplicates_refused(setup):
    resources = with_missions(setup)[0]
    saved = resources.pop("ColosseumMissionQuestList.atb")
    subject = catalog(setup, snapshot(MISSION_QUESTS))
    assert subject.mission_windows(0).mission(100001).listed
    for action in (subject.mission_registry, subject.mission_tags):
        with pytest.raises(ProtocolError) as error:
            action()
        assert (error.value.code, error.value.status) == ("quest_asset_unavailable", 503)
    assert subject._tags is None and REGISTRY_CATEGORIES not in subject._windows
    resources["ColosseumMissionQuestList.atb"] = saved
    assert subject.mission_tags().registry is subject.mission_registry()
    subject = mission_catalog(setup, {**REGISTRY_MISSIONS, 5: ((mission_config(100001, tag=130), (1002,)),)})
    assert subject.mission_windows(0).mission(100001).category == 0
    assert subject.mission_windows(DAILY_TYPE).mission(100001).category == 5
    for action in (subject.mission_registry, subject.mission_tags):
        with pytest.raises(ProtocolError) as error:
            action()
        assert (error.value.code, error.value.status) == ("quest_mode_resource_invalid", 503)


def test_entry_and_resource_digests_report_what_the_catalog_read(setup, monkeypatch):
    resources, possessions = setup
    reads = []

    def read(name):
        reads.append(name)
        return resources[name]

    subject = QuestCatalog(snapshot(), read, possessions=possessions, resource_version="fixture-resources")
    label, unread = "Map0007.aar/Entry02.atb", "Map0001.aar/Entry01.atb"
    assert subject.entry_sha256([label, unread]) == {label: None, unread: None} and subject.entry_sha256(()) == {}
    subject.get(1001)
    assert subject.entry_sha256((label, unread)) == {label: subject.status()["entry_sha256"][label], unread: None}
    monkeypatch.setattr(subject, "resource_rows", lambda name: pytest.fail("Digests never read through resource_rows"))
    reads.clear()
    drops = hashlib.sha256(resources["ItemDropConfigTable.atb"]).hexdigest()
    assert subject.resource_sha256("ItemDropConfigTable.atb") == drops == subject.resource_sha256("ItemDropConfigTable.atb")
    assert reads == ["ItemDropConfigTable.atb"]
    payload = table(UNIT_FIELDS, PERSON_ROWS)
    resources.update({"Packed.atb": compressed(payload), "Broken.atb": b"bad"})
    assert subject.resource_sha256("Packed.atb") == hashlib.sha256(payload).hexdigest()
    for labels in (label, [label, 7], None, 7):
        with pytest.raises(ProtocolError) as error:
            subject.entry_sha256(labels)
        assert (error.value.code, error.value.status) == ("quest_mode_resource_invalid", 503)
    for name, code in (("Map0007.aar", "quest_mode_resource_invalid"), ("maps/Person.atb", "quest_mode_resource_invalid"),
                       (None, "quest_mode_resource_invalid"), ("Broken.atb", "quest_mode_resource_invalid"),
                       ("Absent.atb", "quest_asset_unavailable")):
        with pytest.raises(ProtocolError) as error:
            subject.resource_sha256(name)
        assert (error.value.code, error.value.status) == (code, 503)
    resources["Absent.atb"] = payload
    assert subject.resource_sha256("Absent.atb") == hashlib.sha256(payload).hexdigest()


def test_gift_star_rewards_resolve_implant_names_through_native_name_hashes(setup, tmp_path):
    resources = dict(setup[0])
    implants, texts = [], []
    items = [{"ItemID": ITEM_NAME, "ID": ITEM_HASH, "Stack": 999, "EffectType": 0, "TextID": 0}]
    for index, kind in enumerate(GIFT_REWARD_TYPES):
        gift, label = f"gift_{kind}", f"ItemName_gift_{kind}"
        texts.append({"Recode_Index": index, "Data_Text": f"Gift {kind}"})
        implants += [{"_ID": f"SystemText_QuestRewardName_{kind}", "_TextID": index}, {"_ID": label, "_TextID": index}]
        items.append({"ItemID": gift, "ID": name_hash(gift), "Stack": 9, "EffectType": GIFT_EFFECT_TYPE,
                      "TextID": name_hash(label)})
    resources.update({"cfgImplantText.atb": table((("_ID", TEXT_KIND), ("_TextID", 1)), implants),
                      "SystemText.atb": table(SYSTEM_TEXT_FIELDS, texts),
                      "ItemConfigTable.atb": table((("ItemID", TEXT_KIND), ("ID", 1), ("Stack", 1), ("EffectType", 1),
                                                    ("TextID", 1)), items)})
    possessions = PossessionService(tmp_path, asset_reader=resources.__getitem__)
    subject = QuestCatalog(snapshot(), resources.__getitem__, possessions=possessions, resource_version="fixture-resources")
    assert any(name_hash(f"ItemName_gift_{kind}") < 0 for kind in GIFT_REWARD_TYPES)
    for kind in GIFT_REWARD_TYPES:
        row = {**quest(), "BonusType": kind, "BonusNum": 2, "NormalMode_RewardItemID": "null"}
        assert subject.star_rewards(row, False) == (StarReward("item", kind, 2, item_id=f"gift_{kind}"),)


STAR_FIELDS = MappingProxyType({"BonusType": 0, "BonusNum": 1, "NormalMode_RewardItemID": "null", "_HardLevel": 0})
FIRST_STAR_CRYSTAL = StarReward("crystal", 0, 1)
ITEM_STAR_KIND = 100000 + ITEM_REWARD_KIND
STORY_TYPE, STORY_CATEGORY, SCORE_CATEGORY = 0, 0, 2
STORY_MISSION, SCORE_MISSION = 1, 2
BONUS_QUESTS = (1001, 1003)
SCORE_TYPE, SCORE_QUEST = 6, 1006
ITEM_TEXT_FIELDS = (("ItemID", TEXT_KIND), ("ID", 1), ("Stack", 1), ("TextID", 1))
UNSUPPORTED_OPERATION = 2


def star_quest(**changes):
    return quest(**{**STAR_FIELDS, **changes})


def test_item_star_rewards_name_a_known_possession(setup):
    subject = catalog(setup)
    row = star_quest(BonusType=ITEM_STAR_KIND, BonusNum=2, NormalMode_RewardItemID=ITEM_NAME)
    assert subject.star_rewards(row, False) == (StarReward("item", ITEM_STAR_KIND, 2, item_id=ITEM_NAME),)
    for item_id in ("null", "", "item_absent_from_config"):
        assert subject.star_rewards({**row, "NormalMode_RewardItemID": item_id}, False) == (
            StarReward("unsupported", ITEM_STAR_KIND, 2),)


def mission_resources(category, mission_id, quest_ids):
    config, quest_list = mission_tables(category)
    return {config: table(MISSION_CONFIG_FIELDS, [{"MissionID": mission_id, "Enable": 1, "StartDateTime": [0, 0],
                                                   "EndDateTime": [0, 0]}]),
            quest_list: table(MISSION_LIST_FIELDS, [{"MissionID": mission_id, "QuestID": quest_id}
                                                    for quest_id in quest_ids])}


def bonus_resources(setup, bonus=None):
    resources = setup[0]
    resources.update(mission_resources(STORY_CATEGORY, STORY_MISSION, BONUS_QUESTS))
    resources["ItemConfigTable.atb"] = table(ITEM_TEXT_FIELDS, [
        {"ItemID": ITEM_NAME, "ID": ITEM_HASH, "Stack": 999, "TextID": 0},
        {"ItemID": CRYSTAL, "ID": name_hash(CRYSTAL), "Stack": 9999, "TextID": CRYSTAL_TEXT}])
    if bonus is not None:
        resources[DROP_BONUS_RESOURCE] = bonus
    return resources


def bonus_catalog(resources, tmp_path, *, quests=None, read=None):
    rows = [star_quest(QuestID=quest_id) for quest_id in BONUS_QUESTS] if quests is None else quests
    possessions = PossessionService(tmp_path, asset_reader=resources.__getitem__)
    return QuestCatalog(snapshot(rows), read or resources.__getitem__, possessions=possessions,
                        resource_version="fixture-resources")


@pytest.mark.parametrize("fault,code,sourced", [
    ("missing", "quest_asset_unavailable", False), ("garbage", "quest_mode_resource_invalid", False),
    ("columns", "quest_drop_bonus_table_invalid", True),
])
def test_drop_bonus_table_faults_leave_every_quest_playable_without_a_bonus(setup, tmp_path, fault, code, sourced):
    payloads = {"missing": None, "garbage": b"bad", "columns": bonus_table([bonus_row(1001)], BONUS_COLUMNS[:-1])}
    resources = bonus_resources(setup, payloads[fault])
    subject = bonus_catalog(resources, tmp_path)
    assert all(subject.mode(quest_id).drop_bonus is None for quest_id in BONUS_QUESTS)
    assert subject.drop_bonus(1001) is None and subject.status()["mode_failed_quests"] == {}
    digest = hashlib.sha256(payloads[fault]).hexdigest() if sourced else None
    summary = subject.status()["drop_bonus"]
    assert (summary["loaded"], summary["available"], summary["error"]["code"], summary["failures"], summary["sha256"]) == (
        True, False, code, 0, digest)
    status = subject.drop_bonus_status()
    assert (status["available"], status["error"], status["coverage_by_type"]) == (False, summary["error"], {})


def test_drop_bonus_table_loads_once_for_the_catalog_lifetime(setup, tmp_path):
    resources = bonus_resources(setup)
    reads = Counter()

    def read(name):
        reads[name] += 1
        return resources[name]

    subject = bonus_catalog(resources, tmp_path, read=read)
    loaded = subject.drop_bonus_table()
    assert not loaded.available
    resources[DROP_BONUS_RESOURCE] = bonus_table([bonus_row(1001)])
    assert [subject.mode(quest_id).drop_bonus for quest_id in BONUS_QUESTS] == [None, None]
    assert subject.drop_bonus_table() is loaded and reads[DROP_BONUS_RESOURCE] == 1


def test_drop_bonus_row_faults_close_only_their_own_quest(setup, tmp_path):
    rows = [bonus_row(1001, operation=UNSUPPORTED_OPERATION), bonus_row(1003)]
    subject = bonus_catalog(bonus_resources(setup, bonus_table(rows)), tmp_path)
    assert subject.get(1001).quest_id == 1001
    with pytest.raises(ProtocolError) as error:
        subject.mode(1001)
    assert (error.value.code, error.value.status) == ("quest_drop_bonus_unsupported", 501)
    assert subject.status()["mode_failed_quests"][1001]["code"] == error.value.code
    bonus = subject.mode(1003).drop_bonus
    assert bonus == DropBonus(1003, CRYSTAL_TYPE, CRYSTAL, SUPPORTED_OPERATION, BANNER)
    assert subject.drop_bonus(1003) is bonus
    assert (bonus.value_on(MONDAY), bonus.value_on(SUNDAY_NIGHT)) == (1.5, 0.0)
    status = subject.drop_bonus_status()
    assert (status["available"], status["failures"], status["coverage_by_type"]) == (
        True, {1001: error.value.code}, {STORY_TYPE: len(BONUS_QUESTS)})
    assert subject.status()["drop_bonus"]["failures"] == 1
    for quest_id, code in ((9999, "quest_not_found"), ("1003", "quest_not_found")):
        with pytest.raises(ProtocolError) as refused:
            subject.drop_bonus(quest_id)
        assert refused.value.code == code


def test_score_quests_carry_no_first_star_rewards(setup, tmp_path, monkeypatch):
    monkeypatch.setattr(type(quest_modes.policy(SCORE_TYPE)), "enabled", True)
    resources = bonus_resources(setup)
    resources.update(mission_resources(SCORE_CATEGORY, SCORE_MISSION, (SCORE_QUEST,)))
    resources[quest_modes.map_archive(quest()["MapNo"], SCORE_MISSION)] = resources["Map0007.aar"]
    rows = [*(star_quest(QuestID=quest_id) for quest_id in BONUS_QUESTS), star_quest(QuestID=SCORE_QUEST, **SCORE_ROW_VALUES)]
    subject = bonus_catalog(resources, tmp_path, quests=rows)
    score, story = subject.mode(SCORE_QUEST), subject.mode(1001)
    assert score.score_mode and (score.star_rewards_normal, score.star_rewards_hard) == ((), ())
    assert not story.score_mode and story.star_rewards_normal == (FIRST_STAR_CRYSTAL,)
    assert subject.star_rewards(subject.row(SCORE_QUEST), False) == (FIRST_STAR_CRYSTAL,)


# Current resources hold conditional prize rows only in Type 8, so these types keep their unconditional counts.
UNCONDITIONAL_TYPE_COUNTS = MappingProxyType({
    0: (114, 613), 1: (61, 269), 2: (28, 166), 4: (9, 0), 9: (343, 0), 10: (452, 132), 15: (16, 7), 16: (2016, 10441),
    18: (84, 464), 19: (21, 0)})
REPRODUCE_TYPE, HISTORY_TYPE = 8, 16
REPRODUCE_QUESTS = 207
GOLD_RUSH_OVERRIDE = 437
# Quest: (candidates below three stars, RT index of the special prize from three stars).
GOLD_RUSH_SPECIALS = MappingProxyType({4807: (4, 3), 4808: (4, 4), 4809: (3, 0), 4810: (4, 0), 4811: (3, 0),
                                       4812: (3, 3), 4813: (4, 2)})
BONUS_COVERAGE = MappingProxyType({REPRODUCE_TYPE: 19, HISTORY_TYPE: 2003})
HISTORY_BONUS_QUEST, HISTORY_PLAIN_QUEST = 1, 6311


@pytest.fixture(scope="module")
def current():
    project = Path(__file__).resolve().parents[1]
    if not ((project.parent / "aigisr_exe/Aigis_r.exe").is_file()
            and (project / "runtime/server/masterdata/active.json").is_file()):
        pytest.skip("Verified local client resources and public masters are required")
    master = MasterDataStore(project / "runtime/server/masterdata").snapshot()
    manifest, entries = ResourceLibrary(project / "runtime/resources", client_hash=master.client_hash).load()
    assets = IndexedAssetService(entries, project / "runtime/resources", project.parent / "aigisr_exe")
    reader = lambda name: assets.read(name)[0]
    possessions = PossessionService(project.parent / "aigisr_exe", asset_reader=reader)
    return lambda: QuestCatalog(master, reader, possessions=possessions, resource_version=manifest["version"])


def test_current_enabled_types_keep_their_unconditional_candidates(current):
    subject = current()
    quests, candidates = Counter(), Counter()
    for quest_id in (*subject.all_ids, *subject.mode_ids):
        quest_type = subject.quest_type(quest_id)
        if quest_type not in UNCONDITIONAL_TYPE_COUNTS:
            continue
        quest = subject.get(quest_id)
        subject.mode(quest_id)
        assert not quest.conditional
        assert quest.candidates_for(0) == quest.candidates_for(PACKED_RANK) == quest.candidates
        assert all(not candidate.special_attr and not candidate.override_code for candidate in quest.candidates)
        assert list(map(quest.reward_code, quest.candidates)) == list(map(quest.treasure_code, quest.candidates))
        quests[quest_type] += 1
        candidates[quest_type] += len(quest.candidates)
    counts = {quest_type: (quests[quest_type], candidates[quest_type]) for quest_type in UNCONDITIONAL_TYPE_COUNTS}
    assert counts == dict(UNCONDITIONAL_TYPE_COUNTS)
    status = subject.status()
    assert (status["failed_quests"], status["mode_failed_quests"]) == ({}, {})
    assert (status["excluded_prize_rows"], status["conditional_quests"]) == ({}, [])


def test_current_reproduce_quests_all_build_once_enabled(current, monkeypatch):
    monkeypatch.setattr(type(quest_modes.policy(REPRODUCE_TYPE)), "enabled", True)
    subject = current()
    reproduce = [quest_id for quest_id in subject.mode_ids if subject.quest_type(quest_id) == REPRODUCE_TYPE]
    assert len(reproduce) == REPRODUCE_QUESTS
    assert all(subject.mode(quest_id).quest_type == REPRODUCE_TYPE for quest_id in reproduce)
    status = subject.status()
    assert (status["mode_failed_quests"], status["excluded_prize_rows"]) == ({}, {})
    assert status["conditional_quests"] == list(GOLD_RUSH_SPECIALS)


def test_current_gold_rush_specials_join_the_candidates_from_three_stars(current, monkeypatch):
    monkeypatch.setattr(type(quest_modes.policy(REPRODUCE_TYPE)), "enabled", True)
    subject = current()
    for quest_id, (ordinary, position) in GOLD_RUSH_SPECIALS.items():
        quest = subject.get(quest_id)
        below, above = quest.candidates_for(0), quest.candidates_for(3)
        special = above[position]
        assert len(below) == ordinary and quest.candidates_for(2) == below
        assert above[:position] + above[position + 1:] == below and quest.candidates_for(PACKED_RANK) == above
        assert (special.slot, special.special_attr, special.min_stars, quest.reward_code(special),
                quest.marks_slot(special)) == (1, SPECIAL_ENEMY_ATTR, 3, GOLD_RUSH_OVERRIDE, False)
        assert subject.decode_reward(GOLD_RUSH_OVERRIDE).kind == "unit"
    _, position = GOLD_RUSH_SPECIALS[4807]
    row = subject.candidate_view(4807, 3)["candidates"][position]
    assert (row["id"], row["type"], row["attr"], row["band"], row["reward_code"], row["marks_slot"]) == (
        position, 1, SPECIAL_ENEMY_ATTR, EnemyBand.SPECIAL.value, GOLD_RUSH_OVERRIDE, False)


def test_current_drop_bonuses_cover_history_and_reproduce_quests(current):
    subject = current()
    status = subject.drop_bonus_status()
    assert (status["available"], status["failures"], status["coverage_by_type"]) == (True, {}, dict(BONUS_COVERAGE))
    bonus = subject.mode(HISTORY_BONUS_QUEST).drop_bonus
    assert (bonus.item_id, bonus.value_on(MONDAY), bonus.value_on(SUNDAY_NIGHT)) == (CRYSTAL, 1.5, 0.0)
    assert subject.mode(HISTORY_PLAIN_QUEST).drop_bonus is None
    assert {subject.quest_type(quest_id) for quest_id in (HISTORY_BONUS_QUEST, HISTORY_PLAIN_QUEST)} == {HISTORY_TYPE}
    assert subject.status()["drop_bonus"] == {"loaded": True, "available": True, "error": None, "failures": 0,
                                              "sha256": status["sha256"]}
