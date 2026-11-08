import pytest

from aigis_ps import quest_modes
from aigis_ps.errors import ProtocolError
from aigis_ps.quest_catalog import HARMONY_CRYSTALS, StarReward
from test_quest_modes import disable_policy
from test_quest_service import bundle


MAIN_TYPE, TUTORIAL_TYPE, HISTORY_TYPE = 0, 4, 16


@pytest.fixture(scope="module")
def catalog(bundle):
    return bundle.quests.catalog


def assert_code(code, callback):
    with pytest.raises(ProtocolError) as error:
        callback()
    assert error.value.code == code


@pytest.mark.parametrize("changes,expected", [
    ({"BonusType": 0, "BonusNum": 5}, (StarReward("crystal", 0, 5),)),
    ({"BonusType": 0, "BonusNum": 0}, (StarReward("crystal", 0, 0),)),
    ({"BonusType": 3, "BonusNum": 395}, (StarReward("unit", 3, 1, card_id=395),)),
    ({"BonusType": 4, "BonusNum": 0}, (StarReward("none", 4, 0),)),
    ({"BonusType": 102, "BonusNum": 2}, (StarReward("item", 102, 2, item_id="gift_0002_ruby"),)),
    ({"BonusType": 107, "BonusNum": 1}, (StarReward("item", 107, 1, item_id="gift_1003_millenniumWine"),)),
    ({"BonusType": 102, "BonusNum": 0}, (StarReward("unsupported", 102, 0),)),
    ({"BonusType": 1, "BonusNum": 1000}, (StarReward("unsupported", 1, 1000),)),
    ({"BonusType": 2, "BonusNum": 1}, (StarReward("unsupported", 2, 1),)),
])
def test_normal_star_rewards_follow_bonus_type_and_number(catalog, changes, expected):
    assert catalog.star_rewards({**catalog.row(1001), **changes}, False) == expected


def test_item_kind_star_rewards_require_a_stocked_item(catalog):
    row = catalog.row(8204)
    item_id, count = row["NormalMode_RewardItemID"], row["BonusNum"]
    assert catalog.star_rewards(row, False) == (StarReward("item", 100022, count, item_id=item_id),)
    for changes in ({"NormalMode_RewardItemID": ""}, {"NormalMode_RewardItemID": "null"},
                    {"NormalMode_RewardItemID": "item_absent_from_config"}, {"BonusType": 100021}, {"BonusNum": 0}):
        assert catalog.star_rewards({**row, **changes}, False)[0].kind == "unsupported"


def test_four_star_rewards_read_their_own_fields_and_default_to_one_crystal(catalog):
    story = catalog.row(1002)
    assert catalog.star_rewards(story, True) == (StarReward("crystal", 0, 1),)
    assert catalog.star_rewards({**story, "HardMode_RewardCount": 3}, True) == (StarReward("crystal", 0, 3),)
    assert catalog.star_rewards({**story, "HardMode_RewardType": 3, "HardMode_RewardCardID": 395,
                                 "HardMode_RewardCount": 1}, True) == (StarReward("unit", 3, 1, card_id=395),)
    assert catalog.star_rewards(catalog.row(8633), True) == (StarReward("crystal", 0, 3),)
    empire = catalog.row(7689)
    assert catalog.star_rewards(empire, True) == (StarReward("item", 100022, 100, item_id=empire["HardMode_RewardItemID"]),)


def test_harmony_quests_append_three_crystals_to_either_difficulty(catalog):
    row = catalog.row(3200)
    normal = catalog.star_rewards(row, False)
    assert len(normal) == 2 and normal[0].kind == "unit" and normal[1] == StarReward("crystal", 0, HARMONY_CRYSTALS)
    assert catalog.star_rewards({**row, "HardMode_RewardType": 0, "HardMode_RewardCount": 0}, True) == (
        StarReward("crystal", 0, 1), StarReward("crystal", 0, HARMONY_CRYSTALS))


def test_star_reward_failures_keep_their_specific_codes(catalog):
    row = catalog.row(1001)
    for number in (0, 1, 999999):
        assert_code("quest_reward_unknown", lambda: catalog.star_rewards({**row, "BonusType": 3, "BonusNum": number}, False))
    assert_code("quest_definition_invalid", lambda: catalog.star_rewards({**row, "BonusNum": "5"}, False))
    assert_code("quest_definition_invalid", lambda: catalog.star_rewards({**row, "NormalMode_RewardItemID": "x" * 32}, False))
    assert_code("quest_definition_invalid", lambda: catalog.star_rewards({**row, "HardMode_RewardCount": None}, True))
    assert_code("quest_reward_invalid", lambda: catalog.star_rewards(row, 1))
    assert_code("quest_reward_invalid", lambda: catalog.star_rewards(None, False))


def test_catalog_describes_story_and_tutorial_modes_from_native_rows(catalog):
    story = catalog.mode(1002)
    assert (story.quest_type, story.mission_category, story.gold_counted, story.score_mode) == (0, 0, True, False)
    assert story.mission_ids and story.hard is not None and story.hard.level > 0 and story.hard.rank == 50
    assert story.star_rewards_normal == (StarReward("crystal", 0, 0),)
    assert story.star_rewards_hard == (StarReward("crystal", 0, 1),)
    assert catalog.mode(1001).hard is None and catalog.mode(1001).star_rewards_hard == ()
    assert catalog.mode(1002) is story
    tutorial = catalog.mode(3001)
    assert (tutorial.quest_type, tutorial.mission_category, tutorial.hard) == (4, 7, None)
    assert tutorial.star_rewards_normal == (StarReward("unit", 3, 1, card_id=395),)
    assert tutorial.unit_list > 0 and catalog.fixed_units(tutorial.unit_list)
    assert {catalog.mode(quest_id).star_rewards_normal[0].card_id for quest_id in (3007, 3009)} == {114, 28}


def test_reserved_quest_types_stay_out_of_main_indexes(catalog, monkeypatch):
    enabled = {quest_type for quest_type, entry in quest_modes.registry().items() if entry.enabled} - {MAIN_TYPE}
    rows = catalog._all_rows
    assert catalog.mode_ids == tuple(sorted(quest_id for quest_id, row in rows.items() if row["Type"] in enabled))
    assert tuple(quest_id for quest_id in catalog.mode_ids if rows[quest_id]["Type"] == TUTORIAL_TYPE) == tuple(range(3001, 3010))
    assert all(catalog.row(quest_id)["Type"] == MAIN_TYPE for quest_id in catalog.all_ids)
    before = dict(catalog.status()["entry_sha256"])
    catalog.get(3001)
    assert catalog.status()["entry_sha256"] == before
    disable_policy(monkeypatch, HISTORY_TYPE)
    reserved = min(quest_id for quest_id, row in rows.items() if row["Type"] == HISTORY_TYPE)
    assert reserved not in catalog.mode_ids
    assert_code("quest_mode_unsupported", lambda: catalog.get(reserved))
    assert_code("quest_mode_unsupported", lambda: catalog.mode(reserved))
