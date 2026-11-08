from collections import Counter
from datetime import datetime, timedelta
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import struct
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps import quest_modes
from aigis_ps.al_format import parse_table
from aigis_ps.alxml import dumps, Container
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.possessions import PossessionService
from aigis_ps.tower_catalog import (
    CONFIG_TABLE, CRYSTAL_REWARD, DESCRIPTIONS, FLOAT_RESULTS, FLOOR_TABLE, GATE_QUEST, INTEGER_RESULTS, ITEM_TABLE,
    LEGACY_REWARD_POLICY, MISSING_TEMPLATE, REWARD_POLICY, SCORE_SOURCE, SCORING_POLICY, SCORING_TABLE, STAR_SOURCE,
    TEMPLATE_TABLE, TOWER_TYPE, UNIT_REWARD, UNKNOWN_REWARD, TowerCatalog,
)
from test_al_format import schema_bytes
from test_quest_service import bundle
from test_trade_catalog import name_hash, native_date


INT, FLOAT, TEXT, DATE = 1, 4, 32, 9
PARAMETER_BYTES = {INT: 8, FLOAT: 12, TEXT: 0, DATE: 8}
PACKING = {INT: "<i", FLOAT: "<f", TEXT: "<I", DATE: "<II"}
CONFIG_FIELDS = {"MissionID": INT, "Name": TEXT, "Enable": INT, "DisplayOrder": INT, "StartDateTime": DATE,
                 "EndDateTime": DATE, "TowerCrystalID": TEXT, "ConsumeItemID": TEXT, "DailyAutoAddCount": INT,
                 "TopFloor": INT, "DiscloseQuestID": INT}
FLOOR_FIELDS = {"MissionID": INT, "QuestID": INT, "QuestName": TEXT}
SCORING_FIELDS = {"QuestID": INT, "StarCountPoint1": INT, "StarCountPoint2": INT, "StarCountPoint3": INT,
                  "ConsumedCostFactor1": FLOAT, "ConsumedCostFactor2": FLOAT, "MaxConsumedCostPoint": FLOAT,
                  "MinConsumedCostPoint": FLOAT, "MemberCountFactor1": FLOAT, "MemberCountFactor2": FLOAT,
                  "MemberCountFactorSupport": TEXT, "MaxMemberCountPoint": FLOAT, "MinMemberCountPoint": FLOAT,
                  "MaxScore": INT, "MinScore": INT, "SallyCountFactor1": FLOAT, "SallyCountFactor2": FLOAT,
                  "SallyCountFactorSupport": TEXT, "MaxSallyCountPoint": FLOAT, "MinSallyCountPoint": FLOAT}
SCORE_REWARD_FIELDS = {"ID": INT, "QuestID": INT, "RewardID": INT, "RewardType": INT, "RequiredScore": INT,
                       "GeneralItemID": TEXT, "CardID": INT, "Count": INT}
STAR_REWARD_FIELDS = {"RequiredStarCount": INT, "RewardType": INT, "GeneralItemID": TEXT, "CardID": INT, "Count": INT}
GENERATION_FIELDS = {"Score": INT, "GenerationCount": INT, "PossessionLimit": INT}
ITEM_FIELDS = {"ItemID": TEXT, "ID": INT, "Stack": INT, "EffectType": INT}
TEMPLATE_FIELDS = {"PresentID": INT, "CardID": INT}
TABLE_FIELDS = {CONFIG_TABLE: CONFIG_FIELDS, FLOOR_TABLE: FLOOR_FIELDS, SCORING_TABLE: SCORING_FIELDS,
                ITEM_TABLE: ITEM_FIELDS, TEMPLATE_TABLE: TEMPLATE_FIELDS}
PREFIX_FIELDS = {"TowerScoreRewardConfig": SCORE_REWARD_FIELDS, "TowerTotalStarRewardConfig": STAR_REWARD_FIELDS,
                 "TowerCrystalGenerationConfig": GENERATION_FIELDS}

HERO, GENERAL = 110001, 110002
HERO_FLOORS, GENERAL_FLOORS = (10001, 10002, 10003), (11001, 11002)
OUTSIDE_FLOOR = 10099
HERO_CRYSTAL, GENERAL_CRYSTAL = "tower_crystal_110001", "tower_crystal_110002"
CONSUME = "tower_consume_item_110001"
STAR_ITEM, TICKET, GIFT = "trade_item_001000", "item_smn_ticket_18001", "gift_1000_beer"
CRYSTAL_STACK, CONSUME_STACK = 999999, 30
ALWAYS = (0, 0)
ENABLED = 8191
HERO_DAILY = 3
GENERATION = ((-1, 250, 1500), (150000, 260, 1560), (300000, 270, 1620))
CARD_TEMPLATES = ((0, 1), (2, 56), (3, 57), (840, 60), (6, 60))
UNTEMPLATED_CARD = 999
QUEST_TYPES = {GATE_QUEST: 0, **dict.fromkeys(HERO_FLOORS + GENERAL_FLOORS, TOWER_TYPE)}
HERO_TERMS = {"ConsumedCostFactor1": 7.5, "ConsumedCostFactor2": 0.02, "MinConsumedCostPoint": 1.0,
              "MemberCountFactor1": 2.5, "MemberCountFactor2": 0.1, "MemberCountFactorSupport": ".25",
              "SallyCountFactor1": 0.0, "SallyCountFactor2": 0.0, "SallyCountFactorSupport": "1",
              "MinSallyCountPoint": 1.0}
GENERAL_TERMS = {"ConsumedCostFactor1": 0.0, "ConsumedCostFactor2": 0.0, "MinConsumedCostPoint": 1.0,
                 "MemberCountFactor1": 2.5, "MemberCountFactor2": 0.1, "MemberCountFactorSupport": "0.25",
                 "SallyCountFactor1": 7.5, "SallyCountFactor2": 0.45, "SallyCountFactorSupport": "0.25",
                 "MinSallyCountPoint": 1.0}
STAR_POINTS = (2500, 5000, 10000)
NOW = datetime(2026, 11, 2, 12, tzinfo=JST)
PRINCE, UNIT, OTHER_PRINCE = 1, 2, 309
UX = 100
AVATAR_INDEX = UX + 1000
# RewardType 0 and 4 have no 9590D0 label, so their tiers grant nothing known.
UNLABELED_TYPES = (0, 4)
# The policy text that tower claim receipts saved before RewardType 3 paid were versioned under.
PRE_CRYSTAL_POLICY = ("Score groups follow 95B3C0 continuation rows; units use the lowest PresentUnitData template of the "
                      "card; RewardType 3 stays unknown; delivery is judged when a tier is reached, never at construction")


def table(rows, fields):
    offset, schema = 0, []
    for name, kind in fields.items():
        schema.append((name, offset, kind, b"\0" * PARAMETER_BYTES[kind]))
        offset += struct.calcsize(PACKING[kind])
    records, pool = bytearray(), bytearray()
    for row in rows:
        for name, kind in fields.items():
            if kind == TEXT:
                records += struct.pack("<I", len(pool))
                pool += row[name].encode() + b"\0"
            elif kind == DATE:
                records += struct.pack("<II", *row[name])
            else:
                records += struct.pack(PACKING[kind], row[name])
    encoded = schema_bytes(schema, offset)
    start = 28 + len(encoded)
    return struct.pack("<4sBBHHHIII4s", b"ALTB", 1, 0x14, len(rows), 28, start, offset, len(pool),
                       start + len(records), b"towr") + encoded + records + pool


def fields_of(name):
    if name in TABLE_FIELDS:
        return TABLE_FIELDS[name]
    return next(fields for prefix, fields in PREFIX_FIELDS.items() if name.startswith(prefix))


def config(mission, crystal, daily, disclose, *, enable=ENABLED, start=ALWAYS, end=ALWAYS):
    return {"MissionID": mission, "Name": "tower", "Enable": enable, "DisplayOrder": 2, "StartDateTime": start,
            "EndDateTime": end, "TowerCrystalID": crystal, "ConsumeItemID": CONSUME, "DailyAutoAddCount": daily,
            "TopFloor": 50, "DiscloseQuestID": disclose}


def scoring(quest_id, terms, **changes):
    row = {name: 0.0 for name, kind in SCORING_FIELDS.items() if kind == FLOAT}
    row.update(QuestID=quest_id, StarCountPoint1=STAR_POINTS[0], StarCountPoint2=STAR_POINTS[1],
               StarCountPoint3=STAR_POINTS[2], MaxScore=0, MinScore=0, **terms)
    row.update(changes)
    return row


def score_reward(quest_id, reward_id, required, kind, *, item="", card=0, count=1):
    return {"ID": 0, "QuestID": quest_id, "RewardID": reward_id, "RewardType": kind, "RequiredScore": required,
            "GeneralItemID": item, "CardID": card, "Count": count}


def star_reward(required, kind, *, item="", card=0, count=1):
    return {"RequiredStarCount": required, "RewardType": kind, "GeneralItemID": item, "CardID": card, "Count": count}


def generation(rows=GENERATION):
    return [{"Score": score, "GenerationCount": count, "PossessionLimit": limit} for score, count, limit in rows]


def item(name, stack, effect):
    return {"ItemID": name, "ID": name_hash(name), "Stack": stack, "EffectType": effect}


def world():
    return {
        CONFIG_TABLE: [config(HERO, HERO_CRYSTAL, HERO_DAILY, 0), config(GENERAL, GENERAL_CRYSTAL, 0, HERO_FLOORS[0])],
        FLOOR_TABLE: [{"MissionID": HERO, "QuestID": quest, "QuestName": "floor"} for quest in HERO_FLOORS]
                     + [{"MissionID": GENERAL, "QuestID": quest, "QuestName": "floor"} for quest in GENERAL_FLOORS],
        SCORING_TABLE: [scoring(quest, HERO_TERMS) for quest in HERO_FLOORS + (OUTSIDE_FLOOR,)]
                       + [scoring(quest, GENERAL_TERMS) for quest in GENERAL_FLOORS],
        f"TowerScoreRewardConfig{HERO:06d}.atb": [
            score_reward(10001, 1, 5000, 1, item=HERO_CRYSTAL, count=300), score_reward(0, 2, 10000, 2, card=56),
            score_reward(0, 3, 90000, 3),
            score_reward(10002, 1, 5000, 1, item=HERO_CRYSTAL, count=400), score_reward(0, 2, 20000, 2, card=60),
            score_reward(10003, 1, 5000, 2, card=UNTEMPLATED_CARD),
            score_reward(OUTSIDE_FLOOR, 1, 5000, 1, item=HERO_CRYSTAL)],
        f"TowerScoreRewardConfig{GENERAL:06d}.atb": [
            score_reward(11001, 1, 5000, 1, item=GENERAL_CRYSTAL, count=2000), score_reward(11002, 1, 90000, 3)],
        f"TowerTotalStarRewardConfig{HERO:06d}.atb": [
            star_reward(1, 1, item=STAR_ITEM), star_reward(2, 1, item=TICKET), star_reward(3, 2, card=57)],
        f"TowerTotalStarRewardConfig{GENERAL:06d}.atb": [star_reward(1, 1, item=STAR_ITEM), star_reward(4, 1, item=GIFT, count=5)],
        f"TowerCrystalGenerationConfig{HERO:06d}.atb": generation(),
        f"TowerCrystalGenerationConfig{GENERAL:06d}.atb": generation(),
        ITEM_TABLE: [item(HERO_CRYSTAL, CRYSTAL_STACK, 6), item(GENERAL_CRYSTAL, CRYSTAL_STACK, 6),
                     item(CONSUME, CONSUME_STACK, 0), item(STAR_ITEM, 9999, 6), item(TICKET, 0, 2), item(GIFT, 999, 4)],
        TEMPLATE_TABLE: [{"PresentID": present, "CardID": card} for present, card in CARD_TEMPLATES],
    }


class Quests:
    resource_version = "fixture-v1"
    snapshot = SimpleNamespace(identifier="fixture-master")

    def __init__(self, data, quest_types):
        self.data, self.quest_types = data, quest_types

    def _parsed(self, name):
        if name not in self.data:
            raise ProtocolError("quest_asset_unavailable", f"Fixture resource is missing: {name}", 503)
        return parse_table(self.data[name])

    def resource_rows(self, name):
        return tuple(MappingProxyType(row) for row in self._parsed(name)["rows"])

    def resource_sha256(self, name):
        self._parsed(name)
        return hashlib.sha256(self.data[name]).hexdigest()

    def mission_windows(self, quest_type):
        ranks = {quest: 70 for quest in self.quest_types}
        category = quest_modes.MODE_SPECS[quest_type].mission_category
        return quest_modes.MissionWindows({category: (self.resource_rows(CONFIG_TABLE), self.resource_rows(FLOOR_TABLE))},
                                          ranks)

    def quest_type(self, quest_id):
        return self.quest_types.get(quest_id)


def encode(tables, schemas=None):
    schemas = schemas or {}
    return {name: table(rows, schemas.get(name, fields_of(name))) for name, rows in tables.items()}


def build(tables=None, *, schemas=None, quest_types=None, items=None):
    data = encode(world() if tables is None else tables, schemas)
    item_data = data if items is None else encode({ITEM_TABLE: items})
    possessions = PossessionService(Path("."), asset_reader=item_data.__getitem__)
    return TowerCatalog(Quests(data, QUEST_TYPES if quest_types is None else quest_types), possessions)


def invalid(callback, code="tower_catalog_invalid", status=503):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert (caught.value.code, caught.value.status) == (code, status)
    return caught.value


def member(card_id, get_index, slot):
    return SimpleNamespace(card_id=card_id, get_index=get_index, slot=slot)


def sally(cost, *, token=False, guest=False):
    return SimpleNamespace(sally_cost=cost, token_unit=token, guest_unit=guest)


def rules(prince=False):
    return SimpleNamespace(member_count_prince=prince)


def cleared(*quests):
    return {quest: {"QuestID": quest, "MaxRank": 3, "AchieveFlag": quest_modes.CLEAR_FLAG} for quest in quests}


def test_synthetic_catalog_reads_towers_floors_and_items():
    catalog = build()
    assert [tower.mission_id for tower in catalog.towers] == [HERO, GENERAL]
    hero, general = catalog.towers
    assert (hero.floors, hero.crystal_item_id, hero.consume_item_id, hero.daily_add, hero.disclose_quest) == (
        HERO_FLOORS, HERO_CRYSTAL, CONSUME, HERO_DAILY, 0)
    assert (general.floors, general.disclose_quest, general.top_floor) == (GENERAL_FLOORS, HERO_FLOORS[0], 50)
    assert catalog.floor_tower(11002) is general and catalog.tower(HERO) is hero
    assert set(catalog.by_floor) == set(HERO_FLOORS + GENERAL_FLOORS)
    assert [tier.threshold for tier in hero.star_rewards] == [1, 2, 3]
    assert hero.generation_tier(0).limit == GENERATION[0][2]
    invalid(lambda: catalog.tower(999999), "tower_request_invalid", 400)
    invalid(lambda: catalog.floor_tower(OUTSIDE_FLOOR), "tower_request_invalid", 400)
    invalid(lambda: catalog.tower(True), "tower_request_invalid", 400)


def test_score_continuation_rows_join_the_preceding_floor():
    hero = build().tower(HERO)
    assert sorted(hero.score_rewards) == [*HERO_FLOORS, OUTSIDE_FLOOR]
    assert [(tier.key, tier.threshold) for tier in hero.score_rewards[10001]] == [
        ((10001, 1), 5000), ((10001, 2), 10000), ((10001, 3), 90000)]
    assert [tier.key for tier in hero.score_rewards[10002]] == [(10002, 1), (10002, 2)]
    assert hero.reward(SCORE_SOURCE, (10002, 2)).card_id == 60
    assert hero.reward(STAR_SOURCE, (2,)).item_id == TICKET
    assert hero.reward(SCORE_SOURCE, (10002,)) is None and hero.reward("other", (1,)) is None
    assert hero.score_rewards[OUTSIDE_FLOOR] and hero.reward(SCORE_SOURCE, (OUTSIDE_FLOOR, 1)) is None


@pytest.mark.parametrize("mutate", [
    lambda rows: rows.insert(0, score_reward(0, 9, 1000, 1, item=HERO_CRYSTAL)),
    lambda rows: rows.append(score_reward(10001, 4, 95000, 1, item=HERO_CRYSTAL)),
    lambda rows: rows.insert(2, score_reward(0, 1, 15000, 1, item=HERO_CRYSTAL)),
    lambda rows: rows.insert(2, score_reward(0, 4, 10000, 1, item=HERO_CRYSTAL)),
], ids=["leading-continuation", "reopened-floor", "repeated-reward", "flat-score"])
def test_score_groups_reject_ambiguous_layouts(mutate):
    tables = world()
    mutate(tables[f"TowerScoreRewardConfig{HERO:06d}.atb"])
    invalid(lambda: build(tables))


@pytest.mark.parametrize("name,rows", [
    (f"TowerTotalStarRewardConfig{HERO:06d}.atb", [star_reward(2, 1, item=STAR_ITEM), star_reward(2, 1, item=STAR_ITEM)]),
    (f"TowerTotalStarRewardConfig{HERO:06d}.atb", [star_reward(0, 1, item=STAR_ITEM)]),
    (f"TowerCrystalGenerationConfig{HERO:06d}.atb", generation(((0, 250, 1500),))),
    (f"TowerCrystalGenerationConfig{HERO:06d}.atb", generation(((-1, 250, 1500), (5, 260, 1560), (5, 270, 1620)))),
    (f"TowerCrystalGenerationConfig{HERO:06d}.atb", generation(((-1, -250, 1500),))),
    (f"TowerCrystalGenerationConfig{HERO:06d}.atb", []),
], ids=["repeated-star", "zero-star", "missing-minus-one", "flat-generation", "negative-count", "empty-generation"])
def test_star_and_generation_tables_reject_invalid_layouts(name, rows):
    tables = world()
    tables[name] = rows
    invalid(lambda: build(tables))


def test_reward_forms_are_parsed_without_judging_delivery():
    hero = build().tower(HERO)
    item_tier, unit, crystal = hero.score_rewards[10001]
    assert item_tier.present() == {"kind": "item", "item_id": HERO_CRYSTAL, "amount": 300,
                                   "description": DESCRIPTIONS[SCORE_SOURCE]}
    assert unit.present() == {"kind": "unit", "template_id": 2, "count": 1, "description": DESCRIPTIONS[SCORE_SOURCE]}
    assert hero.reward(SCORE_SOURCE, (10002, 2)).template_id == 6
    assert hero.reward(STAR_SOURCE, (2,)).present()["item_id"] == TICKET
    assert hero.reward(STAR_SOURCE, (3,)).present()["description"] == DESCRIPTIONS[STAR_SOURCE]
    missing = hero.reward(SCORE_SOURCE, (10003, 1))
    assert (missing.card_id, missing.template_id, missing.unsupported) == (UNTEMPLATED_CARD, None, MISSING_TEMPLATE)
    invalid(missing.present, MISSING_TEMPLATE, 501)
    assert (crystal.reward_type, crystal.count, crystal.item_id, crystal.card_id, crystal.unsupported) == (
        CRYSTAL_REWARD, 1, None, None, None)


@pytest.mark.parametrize("source,count", [(SCORE_SOURCE, 1), (STAR_SOURCE, 5)])
def test_crystal_tiers_mail_their_count_as_free_divine_crystals(source, count):
    tables = world()
    tables[f"TowerScoreRewardConfig{HERO:06d}.atb"][2] = score_reward(0, 3, 90000, CRYSTAL_REWARD, count=count)
    tables[f"TowerTotalStarRewardConfig{HERO:06d}.atb"][0] = star_reward(1, CRYSTAL_REWARD, count=count)
    hero = build(tables).tower(HERO)
    tier = hero.reward(source, (10001, 3) if source == SCORE_SOURCE else (1,))
    assert tier.present() == {"kind": "free_crystal", "amount": count, "description": DESCRIPTIONS[source]}


@pytest.mark.parametrize("kind", UNLABELED_TYPES)
def test_reward_types_without_a_label_wait_as_unknown(kind):
    tables = world()
    tables[f"TowerScoreRewardConfig{HERO:06d}.atb"][2] = score_reward(0, 3, 90000, kind)
    unknown = build(tables).tower(HERO).reward(SCORE_SOURCE, (10001, 3))
    assert (unknown.reward_type, unknown.unsupported) == (kind, UNKNOWN_REWARD)
    invalid(unknown.present, UNKNOWN_REWARD, 501)


@pytest.mark.parametrize("row", [
    score_reward(10001, 1, 5000, 1, item="", count=1), score_reward(10001, 1, 5000, 1, item=HERO_CRYSTAL, count=0),
    score_reward(10001, 1, 5000, 2, card=0), score_reward(10001, 1, 5000, 2, card=56, count=0),
    score_reward(10001, 1, 5000, CRYSTAL_REWARD, count=0), score_reward(10001, 1, 5000, CRYSTAL_REWARD, card=56),
    score_reward(10001, 1, 5000, CRYSTAL_REWARD, item=HERO_CRYSTAL),
], ids=["item-without-id", "item-without-count", "unit-without-card", "unit-without-count", "crystal-without-count",
        "crystal-with-card", "crystal-with-item"])
def test_malformed_item_unit_and_crystal_rows_are_rejected(row):
    tables = world()
    tables[f"TowerScoreRewardConfig{HERO:06d}.atb"][0] = row
    invalid(lambda: build(tables))


@pytest.mark.parametrize("items", [
    [item(HERO_CRYSTAL, CRYSTAL_STACK, 4), item(GENERAL_CRYSTAL, CRYSTAL_STACK, 6), item(CONSUME, CONSUME_STACK, 0)],
    [item(HERO_CRYSTAL, 0, 6), item(GENERAL_CRYSTAL, CRYSTAL_STACK, 6), item(CONSUME, CONSUME_STACK, 0)],
    [item(HERO_CRYSTAL, CRYSTAL_STACK, 6), item(GENERAL_CRYSTAL, CRYSTAL_STACK, 6), item(CONSUME, 0, 0)],
    [item(GENERAL_CRYSTAL, CRYSTAL_STACK, 6), item(CONSUME, CONSUME_STACK, 0)],
    [item(HERO_CRYSTAL, CRYSTAL_STACK, 6), item(GENERAL_CRYSTAL, CRYSTAL_STACK, 6)],
], ids=["crystal-not-currency", "crystal-without-stack", "consume-without-stack", "crystal-unknown", "consume-unknown"])
def test_crystals_and_challenge_items_need_stackable_definitions(items):
    tables = world()
    tables[ITEM_TABLE] = items
    invalid(lambda: build(tables))


def test_items_must_share_the_possession_source():
    invalid(lambda: build(items=world()[ITEM_TABLE] + [item("extra_item", 1, 0)]))


def test_possession_readiness_is_read_without_building_its_status(monkeypatch):
    monkeypatch.setattr(PossessionService, "status", lambda _: pytest.fail("readiness must not build the possession status"))
    assert [tower.mission_id for tower in build().towers] == [HERO, GENERAL]


def test_unready_possessions_refuse_the_catalog(monkeypatch):
    monkeypatch.setattr(PossessionService, "is_ready", lambda _: False)
    invalid(build)


@pytest.mark.parametrize("mutate", [
    lambda tables: tables[SCORING_TABLE].pop(0),
    lambda tables: tables[SCORING_TABLE].append(scoring(HERO_FLOORS[0], HERO_TERMS)),
    lambda tables: tables[FLOOR_TABLE].append({"MissionID": GENERAL, "QuestID": HERO_FLOORS[0], "QuestName": "floor"}),
    lambda tables: tables[CONFIG_TABLE].append(config(110003, HERO_CRYSTAL, 0, 0)),
    lambda tables: tables[CONFIG_TABLE].__setitem__(1, config(GENERAL, GENERAL_CRYSTAL, 0, 77777)),
    lambda tables: tables.pop(f"TowerTotalStarRewardConfig{GENERAL:06d}.atb"),
], ids=["unscored-floor", "repeated-scoring", "shared-floor", "floorless-tower", "unknown-disclosure", "missing-table"])
def test_floors_must_be_scored_quests_of_one_tower(mutate):
    tables = world()
    mutate(tables)
    invalid(lambda: build(tables))


def test_floors_must_be_tower_quests():
    invalid(lambda: build(quest_types={**QUEST_TYPES, HERO_FLOORS[1]: 0}))


@pytest.mark.parametrize("support,expected", [(".25", Fraction(1, 4)), ("0.25", Fraction(1, 4)), ("1", Fraction(1)),
                                              ("1.0", Fraction(1)), ("1.", Fraction(1))])
def test_support_text_is_a_plain_decimal(support, expected):
    tables = world()
    tables[SCORING_TABLE] = [scoring(quest, {**HERO_TERMS, "MemberCountFactorSupport": support})
                             for quest in HERO_FLOORS + GENERAL_FLOORS]
    assert build(tables).scoring(HERO_FLOORS[0]).member_support == expected


@pytest.mark.parametrize("support", ["1/4", "-0.25", " 0.25", "0.25 ", "2.5e-1", "", "0..25"])
def test_support_text_rejects_anything_but_unsigned_decimals(support):
    tables = world()
    tables[SCORING_TABLE][0] = scoring(HERO_FLOORS[0], {**HERO_TERMS, "SallyCountFactorSupport": support})
    invalid(lambda: build(tables))


@pytest.mark.parametrize("changes", [
    {"MinMemberCountPoint": -1.0}, {"MaxMemberCountPoint": 0.5, "MinMemberCountPoint": 1.0},
    {"MaxScore": 10, "MinScore": 20}, {"StarCountPoint3": -1},
    {"StarCountPoint3": 0x7FFFFFFF, "MemberCountFactor1": 9.0},
], ids=["negative-minimum", "inverted-point-bounds", "inverted-score-bounds", "negative-star-points", "total-overflow"])
def test_scoring_rows_reject_unbounded_or_inverted_parameters(changes):
    tables = world()
    tables[SCORING_TABLE][0] = scoring(HERO_FLOORS[0], HERO_TERMS, **changes)
    invalid(lambda: build(tables))


def test_score_factor_columns_must_be_float32():
    tables = world()
    tables[SCORING_TABLE] = [{**row, "ConsumedCostFactor2": 0} for row in tables[SCORING_TABLE]]
    invalid(lambda: build(tables, schemas={SCORING_TABLE: {**SCORING_FIELDS, "ConsumedCostFactor2": INT}}))


def test_scoring_uses_the_authored_decimals_of_float32_factors():
    catalog = build()
    formation = [member(PRINCE, 1, 0)] + [member(UNIT, index, index) for index in range(2, 7)]
    # Exact arithmetic on the float32 cells would give 2.5 - 0.10000000149 x 5 < 2 and floor 149999.
    score = catalog.score_floor(HERO_FLOORS[0], 3, sallies=(), members=formation, visitors=(), ux=UX, rules=rules())
    assert (score.score, score.member_count, score.member_count_score, score.consume_cost_score) == (150000, 5.0, 2.0, 7.5)
    costed = catalog.score_floor(HERO_FLOORS[0], 3, sallies=[sally(30), sally(30)], members=formation,
                                 visitors=[member(UNIT, UX + 1500, 0), member(UNIT, UX + 1501, 1)], ux=UX, rules=rules())
    assert (costed.consume_cost, costed.consume_cost_score, costed.member_count, costed.member_count_score) == (
        60, 6.3, 5.5, 1.95)
    assert (costed.sally_count, costed.sally_count_score, costed.star_score, costed.score) == (2.0, 1.0, 10000, 122850)


def test_support_units_count_at_their_factor_in_members_and_sallies():
    catalog = build()
    formation = [member(PRINCE, 1, 0), member(UNIT, 2, 1), member(UNIT, 3, 2), member(UNIT, AVATAR_INDEX, 3),
                 member(UNIT, 4, -1), member(UNIT, 2, 1), member(UNIT, 5, 4)]
    records = [sally(10), sally(10), sally(10), sally(5, guest=True), sally(5, guest=True), sally(3, token=True)]
    score = catalog.score_floor(GENERAL_FLOORS[0], 2, sallies=records, members=formation,
                                visitors=[member(UNIT, UX + 1500, 0), member(UNIT, UX + 1501, 1)], ux=UX, rules=rules())
    assert (score.consume_cost, score.consume_cost_score) == (43, 1.0)
    assert (score.member_count, score.member_count_score, score.sally_count, score.sally_count_score) == (
        3.5, 2.15, 3.5, 5.925)
    assert (score.star_score, score.score) == (5000, 63693)


def test_prince_counting_follows_the_rule_switch_and_skips_him_by_default():
    catalog = build()
    formation = [member(PRINCE, 1, 0), member(OTHER_PRINCE, 7, 1), member(UNIT, 2, 2)]
    skipped = catalog.score_floor(HERO_FLOORS[0], 1, sallies=(), members=formation, visitors=(), ux=UX, rules=rules())
    counted = catalog.score_floor(HERO_FLOORS[0], 1, sallies=(), members=formation, visitors=(), ux=UX,
                                  rules=rules(prince=True))
    assert (skipped.member_count, counted.member_count) == (1.0, 3.0)


def test_the_published_sample_scores_a_prince_only_hero_clear():
    catalog = build()
    # 階層スコア = 1250 x 2 ** stars x (2.5 - 0.1 x members) x (7.5 - 0.02 x cost); the prince pays 10 and counts 0.
    assert STAR_POINTS == tuple(1250 * 2 ** stars for stars in (1, 2, 3))
    score = catalog.score_floor(HERO_FLOORS[0], 3, sallies=[sally(10)], members=[member(PRINCE, 1, 0)],
                                visitors=(), ux=UX, rules=rules())
    assert (score.score, score.consume_cost, score.member_count) == (182500, 10, 0.0)
    assert (score.member_count_score, score.consume_cost_score, score.sally_count_score) == (2.5, 7.3, 1.0)


def test_the_portal_gate_stays_the_first_emergency_quest_alone():
    # Rank 70 guards each floor through the quest rank check, so the portal needs quest 1004 and nothing else.
    assert GATE_QUEST == 1004
    catalog = build()
    assert catalog.accessible(catalog.towers[0], cleared(GATE_QUEST), NOW)


def test_score_bounds_caps_and_disabled_terms():
    tables = world()
    tables[SCORING_TABLE][0] = scoring(HERO_FLOORS[0], {**HERO_TERMS, "MaxConsumedCostPoint": 5.0}, MinScore=30000,
                                       MaxScore=40000)
    tables[SCORING_TABLE][1] = scoring(HERO_FLOORS[1], {**HERO_TERMS, "MemberCountFactor2": 0.0})
    catalog = build(tables)
    capped = catalog.score_floor(HERO_FLOORS[0], 3, sallies=(), members=(), visitors=(), ux=UX, rules=rules())
    assert (capped.consume_cost_score, capped.member_count_score, capped.score) == (5.0, 2.5, 40000)
    floored = catalog.score_floor(HERO_FLOORS[0], 1, sallies=[sally(400)], members=[member(UNIT, 2, 0)] * 30,
                                  visitors=(), ux=UX, rules=rules())
    assert (floored.consume_cost_score, floored.member_count_score, floored.score) == (1.0, 2.4, 30000)
    disabled = catalog.score_floor(HERO_FLOORS[1], 1, sallies=(), members=[member(UNIT, 2, 0)], visitors=(), ux=UX,
                                   rules=rules())
    assert (disabled.member_count_score, disabled.score) == (1.0, 18750)
    crowded = catalog.score_floor(HERO_FLOORS[2], 3, sallies=(), visitors=(), ux=UX, rules=rules(),
                                  members=[member(UNIT, index, 0) for index in range(30)])
    assert (crowded.member_count_score, crowded.score) == (0.0, 0)


@pytest.mark.parametrize("arguments,code,status", [
    ({"stars": 0}, "tower_score_invalid", 400), ({"stars": 4}, "tower_score_invalid", 400),
    ({"stars": True}, "tower_score_invalid", 400), ({"ux": -1}, "tower_score_invalid", 400),
    ({"sallies": [sally(-1)]}, "tower_score_invalid", 400), ({"sallies": [sally(1, token=1)]}, "tower_score_invalid", 400),
    ({"sallies": [SimpleNamespace(sally_cost=1)]}, "tower_score_invalid", 400),
    ({"members": [member("1", 1, 0)]}, "tower_score_invalid", 400),
    ({"sallies": [sally(0x7FFFFFFF), sally(1)]}, "tower_score_invalid", 400),
    ({"rules": SimpleNamespace()}, "tower_rules_invalid", 503), ({"quest_id": OUTSIDE_FLOOR}, "tower_request_invalid", 400),
])
def test_score_inputs_are_validated(arguments, code, status):
    catalog = build()
    values = {"quest_id": HERO_FLOORS[0], "stars": 3, "sallies": (), "members": (), "visitors": (), "ux": UX,
              "rules": rules(), **arguments}
    quest_id, stars = values.pop("quest_id"), values.pop("stars")
    invalid(lambda: catalog.score_floor(quest_id, stars, **values), code, status)


def test_tower_result_carries_nine_native_columns():
    score = build().score_floor(GENERAL_FLOORS[0], 3, sallies=[sally(12), sally(4, guest=True)],
                                members=[member(UNIT, 2, 0)], visitors=[member(UNIT, UX + 1500, 0)], ux=UX, rules=rules())
    result = score.result(123456)
    assert [(column.name, column.type.value) for column in result.children] == (
        [(name, "I") for name in INTEGER_RESULTS] + [(name, "F") for name in FLOAT_RESULTS])
    assert [column.values[0] for column in result.children] == [
        16, 10000, score.score, 123456, 1.25, 1.0, score.member_count_score, 1.25, score.sally_count_score]
    assert b"<TowerResult>" in dumps(Container(children=(result,)))
    invalid(lambda: score.result(-1), "tower_score_invalid", 503)
    invalid(lambda: score.result(0x80000000), "tower_score_invalid", 503)


def test_access_needs_the_gate_disclosure_and_an_open_window():
    catalog = build()
    hero, general = catalog.towers
    assert not catalog.accessible(hero, {}, NOW)
    assert catalog.accessible(hero, cleared(GATE_QUEST), NOW) and not catalog.accessible(general, cleared(GATE_QUEST), NOW)
    assert not catalog.disclosed(general, cleared(GATE_QUEST)) and catalog.disclosed(hero, {})
    assert catalog.accessible(general, cleared(GATE_QUEST, HERO_FLOORS[0]), NOW)
    tables = world()
    tables[CONFIG_TABLE][0] = config(HERO, HERO_CRYSTAL, HERO_DAILY, 0, start=native_date(NOW - timedelta(days=2)),
                                     end=native_date(NOW - timedelta(days=1)))
    tables[CONFIG_TABLE][1] = config(GENERAL, GENERAL_CRYSTAL, 0, HERO_FLOORS[0], enable=0)
    closed = build(tables)
    assert not any(closed.accessible(tower, cleared(GATE_QUEST, HERO_FLOORS[0]), NOW) for tower in closed.towers)
    invalid(lambda: catalog.accessible(hero, cleared(GATE_QUEST), NOW.replace(tzinfo=None)), "tower_clock_invalid", 503)


def test_generation_tiers_and_stock_bounds():
    hero = build().tower(HERO)
    assert [hero.generation_tier(total).score for total in (0, 149999, 150000, 299999, 300000, 10 ** 9)] == [
        -1, -1, 150000, 150000, 300000, 300000]
    assert hero.stack_bound(0) == hero.stack_bound(300000) == 1620
    tables = world()
    tables[f"TowerCrystalGenerationConfig{HERO:06d}.atb"] = generation(((-1, 250, 2000), (100, 260, 1500)))
    lowered = build(tables).tower(HERO)
    assert (lowered.stack_bound(0), lowered.stack_bound(150)) == (2000, 1500)
    invalid(lambda: hero.generation_tier(-1), "tower_score_invalid", 503)
    assert hero.total_stars({**cleared(*HERO_FLOORS), GENERAL_FLOORS[0]: {"MaxRank": 3}, 10002: {"MaxRank": 2}}) == 8


def test_content_version_follows_sources_and_windows():
    first, second = build(), build()
    assert first.content_version == second.content_version and len(first.content_version) == 64
    assert set(first.source_hashes) == {CONFIG_TABLE, FLOOR_TABLE, SCORING_TABLE, ITEM_TABLE, TEMPLATE_TABLE} | {
        pattern.format(mission) for mission in (HERO, GENERAL)
        for pattern in ("TowerScoreRewardConfig{:06d}.atb", "TowerTotalStarRewardConfig{:06d}.atb",
                        "TowerCrystalGenerationConfig{:06d}.atb")}
    tables = world()
    tables[f"TowerTotalStarRewardConfig{GENERAL:06d}.atb"][1]["Count"] = 6
    recounted = build(tables)
    assert recounted.content_version != first.content_version
    assert recounted.legacy_content_version != first.legacy_content_version
    tables = world()
    tables[CONFIG_TABLE][1] = config(GENERAL, GENERAL_CRYSTAL, 0, HERO_FLOORS[0], end=native_date(NOW))
    assert build(tables).content_version != first.content_version


def test_the_legacy_content_version_keeps_the_policy_older_claims_were_saved_under():
    catalog = build()
    assert LEGACY_REWARD_POLICY == PRE_CRYSTAL_POLICY != REWARD_POLICY
    content = {"schema": 1, "resource_version": Quests.resource_version, "master": Quests.snapshot.identifier,
               "sources": dict(catalog.source_hashes), "scoring": SCORING_POLICY, "gate_quest": GATE_QUEST,
               "windows": {str(mission): {"enable": ENABLED, "start": None, "end": None} for mission in (HERO, GENERAL)}}

    def digest(policy):
        encoded = json.dumps({**content, "rewards": policy}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    assert (catalog.legacy_content_version, catalog.content_version) == (digest(PRE_CRYSTAL_POLICY), digest(REWARD_POLICY))


def test_status_summarizes_each_tower():
    status = build().status()
    assert status["ready"] is True and status["gate_quest"] == GATE_QUEST and status["native_tested"] is False
    hero, general = status["towers"]
    assert (hero["floors"], hero["score_groups"], hero["score_tiers"], hero["star_tiers"], hero["generation_tiers"]) == (
        3, 4, 7, 3, 3)
    assert hero["unsupported_tiers"] == {MISSING_TEMPLATE: 1}
    assert (general["disclose_quest"], general["unsupported_tiers"], general["window"]) == (
        HERO_FLOORS[0], {}, {"enable": ENABLED, "start": None, "end": None})


@pytest.fixture(scope="module")
def native_towers(bundle):
    return TowerCatalog(bundle.quests.catalog, bundle.possessions)


def test_native_towers_floors_and_challenge_items(native_towers, bundle):
    towers = native_towers.towers
    assert [tower.mission_id for tower in towers] == [110001, 110002, 110003]
    assert [(len(tower.floors), tower.floors[0], tower.floors[-1]) for tower in towers] == [
        (45, 10001, 10045), (45, 11001, 11045), (33, 12001, 12033)]
    assert {tower.consume_item_id for tower in towers} == {CONSUME}
    assert bundle.possessions.stack(CONSUME) == CONSUME_STACK
    assert [(tower.daily_add, tower.disclose_quest) for tower in towers] == [(3, 0), (0, 10001), (0, 10001)]
    assert all(bundle.possessions.stack(tower.crystal_item_id) == CRYSTAL_STACK for tower in towers)


def test_native_generation_tables(native_towers):
    for tower in native_towers.towers:
        assert len(tower.generation) == 53
        assert (tower.generation[0].score, tower.generation[0].count, tower.generation[0].limit) == (-1, 250, 1500)
        assert (tower.generation[-1].score, tower.generation[-1].count, tower.generation[-1].limit) == (7800000, 770, 4620)
        assert tower.generation_tier(7799999) == tower.generation[-2]
        assert tower.generation_tier(7800000) == tower.generation[-1]


def test_native_score_tiers_and_reward_forms(native_towers):
    crystals = {}
    for tower in native_towers.towers:
        groups = tower.score_rewards
        assert len(groups) == 45 and {len(tiers) for tiers in groups.values()} == {10}
        tiers = [tier for group in groups.values() for tier in group]
        crystals[tower.mission_id] = [tier for tier in tiers if tier.reward_type == CRYSTAL_REWARD]
        # Every fifth floor pays a unit at its last tier instead, so the divine crystal sits on the other 36.
        assert {(tier.key[1], tier.threshold, tier.count) for tier in crystals[tower.mission_id]} == {(10, 90000, 1)}
        assert all(tier.key[0] % 5 for tier in crystals[tower.mission_id])
        assert not [tier for tier in tiers if tier.unsupported is not None]
    assert [len(tiers) for tiers in crystals.values()] == [36, 36, 36]
    assert sum(len(tiers) for tiers in crystals.values()) == 108
    playable = native_towers.tower(110003).floors
    assert sum(tier.key[0] in playable for tier in crystals[110003]) == 27
    assert crystals[110001][0].present() == {"kind": "free_crystal", "amount": 1, "description": DESCRIPTIONS[SCORE_SOURCE]}
    crystals = [sum(tier.count for floor in tower.floors for tier in tower.score_rewards[floor]
                    if tier.item_id == tower.crystal_item_id) for tower in native_towers.towers]
    assert crystals == [49500, 51500, 37100]
    hero = native_towers.tower(110001)
    assert hero.reward(SCORE_SOURCE, (10001, 6)).present() == {
        "kind": "item", "item_id": "tower_crystal_110001", "amount": 300, "description": DESCRIPTIONS[SCORE_SOURCE]}
    assert hero.reward(SCORE_SOURCE, (10001, 1)).present()["template_id"] == 782
    assert hero.reward(STAR_SOURCE, (54,)).template_id == 6


def test_native_unit_rewards_use_the_initial_present_templates(native_towers, bundle):
    units = [tier for tower in native_towers.towers
             for tier in (*(tier for group in tower.score_rewards.values() for tier in group), *tower.star_rewards)
             if tier.reward_type == UNIT_REWARD]
    assert len({tier.card_id for tier in units}) == 29
    assert all(tier.template_id == bundle.presents.initial_unit_template(tier.card_id) for tier in units)


def test_native_star_tiers(native_towers):
    tickets = {}
    for tower in native_towers.towers:
        assert [tier.threshold for tier in tower.star_rewards] == list(range(1, 136))
        tickets[tower.mission_id] = [(tier.threshold, tier.item_id) for tier in tower.star_rewards
                                     if (tier.item_id or "").startswith("item_smn_ticket_")]
    assert tickets == {110001: [(5, "item_smn_ticket_18003"), (10, "item_smn_ticket_18002"), (50, TICKET), (100, TICKET)],
                       110002: [(50, TICKET), (100, TICKET)], 110003: [(50, TICKET), (100, TICKET)]}
    assert Counter(tier.reward_type for tier in native_towers.tower(110001).star_rewards) == {1: 85, 2: 50}


def test_native_scoring_reads_the_shared_table(native_towers):
    hero = native_towers.scoring(10001)
    assert (hero.cost.factor, hero.cost.slope, hero.cost.minimum, hero.member.slope) == (
        Fraction(15, 2), Fraction(1, 50), 1, Fraction(1, 10))
    assert (hero.member_support, hero.sally_support, hero.sally.enabled, hero.star_points) == (
        Fraction(1, 4), 1, False, STAR_POINTS)
    general = native_towers.scoring(11001)
    assert (general.cost.enabled, general.sally.slope, general.sally_support) == (False, Fraction(9, 20), Fraction(1, 4))
    assert native_towers.scoring(12001).sally.slope == Fraction(17, 200)
    formation = [member(PRINCE, 1, 0)] + [member(UNIT, index, index) for index in range(2, 7)]
    assert native_towers.score_floor(10001, 3, sallies=(), members=formation, visitors=(), ux=UX,
                                     rules=rules()).score == 150000
    sample = native_towers.score_floor(10001, 3, sallies=[sally(10)], members=[member(PRINCE, 1, 0)],
                                       visitors=(), ux=UX, rules=rules())
    assert (sample.score, sample.member_count, sample.consume_cost_score) == (182500, 0.0, 7.3)
    assert [tower["unsupported_tiers"] for tower in native_towers.status()["towers"]] == [{}, {}, {}]


def test_native_floors_carry_the_rank_gate_without_entry_costs(native_towers, bundle):
    quests = bundle.quests.catalog
    for tower in native_towers.towers:
        for floor in tower.floors:
            quest = quests.get(floor)
            assert (quest.rank, quest.charisma, quest.action_points, quest.rank_exp, quest.gold) == (70, 0, 0, 500, 0)
