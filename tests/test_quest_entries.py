import re
from types import MappingProxyType

import pytest

from aigis_ps.quest_entries import (
    ENEMY_ROW_SPAN, STAGE_VARIANT_MAX_STARS, STAR_UNBOUNDED, UNCONDITIONAL, EnemyBand, StarInterval,
    UnsupportedAppearance, appear_interval, classify_enemy, expected_enemies, row_interval,
)
from test_quest_service import bundle


INT32_MIN, INT32_MAX = -(2 ** 31), 2 ** 31 - 1
THREE_STARS = 3
AT_LEAST_THREE, AT_MOST_TWO = "GetNum_QuestStar() >= 3;", "GetNum_QuestStar() <= 2;"
NEVER, UNSUPPORTED = "GetNum_QuestStar() < 0;", "GetNum_QuestStar() != 3;"
# Score quests pack the running total above bit 12 and the best run below it.
PACKED_RANK = (100 << 12) | 100
FULL_SCORE_RANK = (1127 << 12) | 1127
SUBJUGATION_TYPE, REPRODUCE_TYPE = 6, 8
REPRODUCE_QUESTS = 207
STAGE_VARIANT_ROWS = 8
SPECIAL_PRIZE_ENEMIES = MappingProxyType({4807: 7005, 4808: 7003, 4809: 7003, 4810: 7004, 4811: 7002, 4812: 7006,
                                         4813: 7004})
SPECIAL_PRIZE_SLOT, SPECIAL_PRIZE_OVERRIDE = 1, 437
SUBJUGATION_ENEMIES = MappingProxyType({
    6401: 100, 6402: 100, 6403: 300, 6404: 400, 6405: 500,
    6611: 100, 6612: 100, 6613: 300, 6614: 400, 6615: 500,
    30018: 100, 30019: 100, 30020: 300, 30021: 500, 30022: 750, 30023: 751,
})
# First quests have no start button and announce no enemy count.
FIRST_QUESTS = frozenset((6401, 6611, 30018))
SUBJUGATION_QUEST_LIST, ENEMY_TABLE = "SubjugationMissionQuestList.atb", "Enemy.atb"
ANNOUNCED_COUNT = re.compile(r"[0-9]+")


def entry(enemy_id, *, loop=1, appear=None):
    return MappingProxyType({"EnemyID": enemy_id, "Loop": loop, "IsAppear": appear})


def enemy(attribute):
    return MappingProxyType({"_Attribute": attribute})


@pytest.mark.parametrize("enemy_id,band", [
    (0, EnemyBand.ORDINARY), (999, EnemyBand.ORDINARY), (1000, EnemyBand.SKIPPED), (5999, EnemyBand.SKIPPED),
    (6000, EnemyBand.STAGE_VARIANT), (6999, EnemyBand.STAGE_VARIANT), (7000, EnemyBand.SPECIAL),
    (7999, EnemyBand.SPECIAL), (8000, EnemyBand.DATED), (8999, EnemyBand.DATED), (9000, EnemyBand.SKIPPED),
    (-1, EnemyBand.SKIPPED), (INT32_MIN, EnemyBand.SKIPPED), (INT32_MAX, EnemyBand.SKIPPED),
])
def test_enemy_bands_compare_identifiers_unsigned_like_the_row_filter(enemy_id, band):
    assert classify_enemy(enemy_id) is band


@pytest.mark.parametrize("enemy_id", [None, True, 1.0, "7000", INT32_MAX + 1, INT32_MIN - 1])
def test_enemy_bands_require_a_native_int32(enemy_id):
    with pytest.raises(ValueError) as error:
        classify_enemy(enemy_id)
    assert not isinstance(error.value, UnsupportedAppearance)


@pytest.mark.parametrize("text,interval", [
    (None, (0, STAR_UNBOUNDED)), ("", (0, STAR_UNBOUNDED)),
    (AT_LEAST_THREE, (3, STAR_UNBOUNDED)), ("GetNum_QuestStar() > 3;", (4, STAR_UNBOUNDED)),
    (AT_MOST_TWO, (0, 2)), ("GetNum_QuestStar() < 2;", (0, 1)), ("GetNum_QuestStar() == 5;", (5, 5)),
    (" GetNum_QuestStar( ) >=3 ", (3, STAR_UNBOUNDED)), ("GetNum_QuestStar()<=0;\n", (0, 0)),
    ("GetNum_QuestStar() >= 0;", (0, STAR_UNBOUNDED)),
    ("GetNum_QuestStar() == 2147483647;", (STAR_UNBOUNDED, STAR_UNBOUNDED)),
    ("GetNum_QuestStar() <= 2147483647;", (0, STAR_UNBOUNDED)),
])
def test_star_conditions_become_closed_star_intervals(text, interval):
    result = appear_interval(text)
    assert result == interval and isinstance(result, StarInterval)


@pytest.mark.parametrize("text", [NEVER, "GetNum_QuestStar() > 2147483647;"])
def test_unsatisfiable_star_conditions_share_the_empty_interval(text):
    interval = appear_interval(text)
    assert interval.empty and interval == appear_interval(NEVER)
    assert not any(interval.covers(stars) for stars in (0, THREE_STARS, PACKED_RANK, STAR_UNBOUNDED))


@pytest.mark.parametrize("text", [
    UNSUPPORTED, "GetNum_QuestStar(4807) >= 3;", "GetNum_QuestStar() >= 3; IsOpenMission();",
    "GetNum_QuestStar() >= 3;;", "GetNum_QuestStar() >= 2147483648;", "GetNum_QuestStar() >= 12345678901;",
    "GetNum_QuestStar() >= -1;", "GetNum_QuestStar() = 3;", "GetNum_QuestStar() => 3;",
    "GetNum_QuestStar() >= 3.0;", "GetNum_QuestStar() >= ３;", "getnum_queststar() >= 3;",
    "GetNum_QuestStar >= 3;", "3 <= GetNum_QuestStar();", "SomeCondition", " ", ";",
])
def test_other_appearance_expressions_are_unsupported(text):
    with pytest.raises(UnsupportedAppearance):
        appear_interval(text)


@pytest.mark.parametrize("text", [3, True, AT_LEAST_THREE.encode(), [AT_LEAST_THREE]])
def test_appearance_must_be_native_text(text):
    with pytest.raises(ValueError) as error:
        appear_interval(text)
    assert not isinstance(error.value, UnsupportedAppearance)


def test_star_intervals_intersect_and_cover_as_closed_sets():
    variant, prize = StarInterval(0, STAGE_VARIANT_MAX_STARS), appear_interval(AT_LEAST_THREE)
    assert variant.intersection(prize).empty and variant.intersection(prize) == prize.intersection(variant)
    assert UNCONDITIONAL == (0, STAR_UNBOUNDED) and UNCONDITIONAL.intersection(prize) == prize
    assert prize.intersection(StarInterval(2, 5)) == (3, 5)
    assert prize.covers(THREE_STARS) and prize.covers(PACKED_RANK) and not prize.covers(STAGE_VARIANT_MAX_STARS)


@pytest.mark.parametrize("row,interval", [
    (MappingProxyType({"EnemyID": 5}), UNCONDITIONAL), (entry(0), UNCONDITIONAL),
    (entry(999, appear=""), UNCONDITIONAL), (entry(1, appear=AT_MOST_TWO), (0, 2)),
    (entry(6000), (0, STAGE_VARIANT_MAX_STARS)), (entry(6999, appear=AT_MOST_TWO), (0, STAGE_VARIANT_MAX_STARS)),
    (entry(6001, appear="GetNum_QuestStar() == 1;"), (1, 1)), (entry(6003, appear=AT_LEAST_THREE), None),
    (entry(7000), UNCONDITIONAL), (entry(7005, appear=AT_LEAST_THREE), (THREE_STARS, STAR_UNBOUNDED)),
    (entry(1, appear=NEVER), None),
])
def test_row_intervals_intersect_the_enemy_band_with_the_condition(row, interval):
    assert row_interval(row) == interval


@pytest.mark.parametrize("enemy_id", [1000, 5999, 9000, -1, INT32_MIN])
def test_rows_outside_the_native_bands_never_evaluate_their_condition(enemy_id):
    assert row_interval(entry(enemy_id)) is None
    assert row_interval(entry(enemy_id, appear=UNSUPPORTED)) is None


@pytest.mark.parametrize("appear", [None, AT_LEAST_THREE, NEVER, UNSUPPORTED])
def test_dated_enemies_are_unsupported_whatever_their_condition(appear):
    with pytest.raises(UnsupportedAppearance):
        row_interval(entry(8000, appear=appear))


@pytest.mark.parametrize("enemy_id", [1, 6001, 7001])
def test_unsupported_conditions_on_admitted_bands_are_refused(enemy_id):
    with pytest.raises(UnsupportedAppearance):
        row_interval(entry(enemy_id, appear=UNSUPPORTED))


@pytest.mark.parametrize("row", [None, (("EnemyID", 1),), MappingProxyType({"Loop": 1}), entry("1"), entry(True)])
def test_row_intervals_require_native_entry_rows(row):
    with pytest.raises(ValueError) as error:
        row_interval(row)
    assert not isinstance(error.value, UnsupportedAppearance)


def test_planned_enemies_add_the_loops_of_rows_appearing_at_the_raw_max_rank():
    enemies = (enemy("アーマー"),)
    entries = (
        entry(1, loop=10), MappingProxyType({"EnemyID": 2, "Loop": 1}), entry(6001, loop=100),
        entry(7001, loop=1000, appear=AT_LEAST_THREE), entry(3, loop=10000, appear="GetNum_QuestStar() == 1;"),
        entry(1000, loop=100000), entry(9000, loop=100000), entry(-1, loop=100000),
    )
    ranks = (0, 1, STAGE_VARIANT_MAX_STARS, THREE_STARS, PACKED_RANK, STAR_UNBOUNDED)
    assert [expected_enemies(entries, enemies, rank) for rank in ranks] == [111, 10111, 111, 1011, 1011, 1011]


@pytest.mark.parametrize("attributes,counted", [
    ({}, True), ({"_Attribute": None}, True), ({"_Attribute": ""}, True), ({"_Attribute": "アーマー,機械"}, True),
    ({"_Attribute": "置物"}, False), ({"_Attribute": "分身"}, False), ({"_Attribute": "ステルス,置物"}, False),
    ({"_Attribute": "置物,ステルスII,ブロック不可"}, False), ({"_Attribute": "アーマー、分身"}, False),
    ({"_Attribute": "ステルス 置物"}, False), ({"_Attribute": "置物師,分身術"}, True),
    ({"_Attribute": "エスネア＆エフネのファン"}, True),
])
def test_placed_objects_and_clones_stay_out_of_the_planned_count(attributes, counted):
    entries = (entry(1, loop=7), entry(7001, loop=5))
    planned = sum(row["Loop"] for row in entries) if counted else 0
    assert expected_enemies(entries, (MappingProxyType(attributes),), 0) == planned


def test_enemy_zero_and_rows_past_the_enemy_table_still_count():
    placed = enemy("置物")
    # A plain Python modulo would wrap EnemyID 0 onto the same last row as 7000.
    full_table = (placed,) * ENEMY_ROW_SPAN
    assert expected_enemies((entry(0, loop=4),), full_table, 0) == 4
    assert expected_enemies((entry(7000, loop=4),), full_table, 0) == 0
    assert expected_enemies((entry(1, loop=8), entry(3, loop=6)), (placed, placed), 0) == 6


@pytest.mark.parametrize("band_start", [0, 6000, 7000])
def test_every_band_reads_attributes_from_the_base_enemy_row(band_start):
    # Only a 1000-row span maps 601, 6601 and 7601 onto the same row 600.
    placed_row = 600
    enemies = (enemy(""),) * placed_row + (enemy("置物"),)
    assert expected_enemies((entry(band_start + placed_row + 1, loop=3),), enemies, 0) == 0
    assert expected_enemies((entry(band_start + placed_row, loop=3),), enemies, 0) == 3


def test_rows_left_out_by_the_filter_are_never_read():
    unread = ("not an enemy row",)
    skipped = (entry(1001, loop=-1), entry(9001, loop=None), entry(6001, loop="x"), entry(1, loop=1.5, appear=NEVER))
    assert expected_enemies((entry(2, loop=2), *skipped), unread, THREE_STARS) == 2


@pytest.mark.parametrize("loop", [None, -1, True, 1.5, "1", INT32_MAX + 1])
def test_appearing_rows_require_a_native_loop(loop):
    with pytest.raises(ValueError) as error:
        expected_enemies((entry(1, loop=loop),), (), 0)
    assert not isinstance(error.value, UnsupportedAppearance)


@pytest.mark.parametrize("max_rank", [-1, None, True, 3.0, INT32_MAX + 1])
def test_planned_enemies_require_a_raw_native_max_rank(max_rank):
    with pytest.raises(ValueError):
        expected_enemies((entry(1),), (), max_rank)


@pytest.mark.parametrize("enemies", [
    None, "置物", iter(()), {0: enemy("")}, ("置物",), (MappingProxyType({"_Attribute": 5}),),
])
def test_planned_enemies_require_native_enemy_rows(enemies):
    with pytest.raises(ValueError) as error:
        expected_enemies((entry(1),), enemies, 0)
    assert not isinstance(error.value, UnsupportedAppearance)


def test_planned_enemy_counts_stay_within_int32():
    assert expected_enemies((entry(1, loop=INT32_MAX),), (), 0) == INT32_MAX
    with pytest.raises(ValueError):
        expected_enemies((entry(1, loop=INT32_MAX), entry(2, loop=1)), (), 0)


@pytest.mark.parametrize("row", [entry(8000, loop=0), entry(1, appear=UNSUPPORTED)])
def test_planned_enemies_refuse_rows_they_cannot_evaluate(row):
    with pytest.raises(UnsupportedAppearance) as error:
        expected_enemies((entry(1), row), (), 0)
    # Callers that map only ValueError still refuse the quest.
    assert isinstance(error.value, ValueError)


@pytest.fixture(scope="module")
def quests(bundle):
    return bundle.quests.catalog


def quest_map(quests, quest_id):
    row = quests.row(quest_id)
    archive, _ = quests.mission_windows(row["Type"]).archive(quest_id, row["MapNo"])
    return archive, f"Entry{row['EntryNo']:02d}.atb"


def entry_rows(quests, quest_id):
    return quests.archive_rows(*quest_map(quests, quest_id))


def enemy_rows(quests, quest_id):
    archive, _ = quest_map(quests, quest_id)
    return quests.archive_rows(archive, ENEMY_TABLE)


def test_current_gold_rush_specials_appear_from_three_stars_with_their_override_card(quests):
    for quest_id, enemy_id in SPECIAL_PRIZE_ENEMIES.items():
        prizes = [row for row in entry_rows(quests, quest_id) if row["PrizeCardID"] and row.get("IsAppear")]
        found = [(row["PrizeCardID"], row["EnemyID"], row["PrizeEnemyDropPercent"], row["IsAppear"]) for row in prizes]
        assert found == [(SPECIAL_PRIZE_SLOT, enemy_id, SPECIAL_PRIZE_OVERRIDE, AT_LEAST_THREE)]
        assert classify_enemy(enemy_id) is EnemyBand.SPECIAL
        assert row_interval(prizes[0]) == (THREE_STARS, STAR_UNBOUNDED)


def test_current_reproduce_conditions_are_seven_three_star_prizes_and_eight_low_star_variants(quests):
    reproduce = quests.playable_quests(REPRODUCE_TYPE)
    conditional = [(quest_id, row) for quest_id in reproduce for row in entry_rows(quests, quest_id)
                   if row.get("IsAppear")]
    prizes = [(quest_id, row) for quest_id, row in conditional if row["PrizeCardID"]]
    variants = [row for _, row in conditional if not row["PrizeCardID"]]
    assert len(reproduce) == REPRODUCE_QUESTS and len(variants) == STAGE_VARIANT_ROWS
    assert [(quest_id, row["EnemyID"]) for quest_id, row in prizes] == list(SPECIAL_PRIZE_ENEMIES.items())
    assert {row_interval(row) for _, row in prizes} == {(THREE_STARS, STAR_UNBOUNDED)}
    assert ({(row["IsAppear"], classify_enemy(row["EnemyID"]), row_interval(row)) for row in variants}
            == {(AT_MOST_TWO, EnemyBand.STAGE_VARIANT, (0, STAGE_VARIANT_MAX_STARS))})


def test_current_subjugation_quests_plan_their_enemy_counts_at_any_rank(quests):
    assert quests.playable_quests(SUBJUGATION_TYPE) == tuple(SUBJUGATION_ENEMIES)
    for quest_id, planned in SUBJUGATION_ENEMIES.items():
        entries, enemies = entry_rows(quests, quest_id), enemy_rows(quests, quest_id)
        assert expected_enemies(entries, enemies, 0) == expected_enemies(entries, enemies, FULL_SCORE_RANK) == planned


def test_current_subjugation_counts_match_the_enemy_count_the_client_announces(quests):
    texts = {row["QuestID"]: row["NumberOfEnemyText"] for row in quests.resource_rows(SUBJUGATION_QUEST_LIST)
             if row["QuestID"] in SUBJUGATION_ENEMIES}
    announced = {quest_id: int(ANNOUNCED_COUNT.search(text)[0]) for quest_id, text in texts.items() if text}
    assert texts.keys() == SUBJUGATION_ENEMIES.keys() and texts.keys() - announced.keys() == FIRST_QUESTS
    assert all(expected_enemies(entry_rows(quests, quest_id), enemy_rows(quests, quest_id), 0) == count
               for quest_id, count in announced.items())
