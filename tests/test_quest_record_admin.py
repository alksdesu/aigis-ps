import ast
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pytest

import aigis_ps.quest_record_admin as record_admin_module
from aigis_ps.bootstrap import QUEST_RECORD_COUNTERS
from aigis_ps.business_calendar import civil_day
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.native_numbers import INT32_MIN, signed32
from aigis_ps.quest_modes import CLEAR_FLAG, THREE_STAR_FLAG
from aigis_ps.quest_record_admin import (ARCHIVE_KEY, COUNTER_FIELDS, EDITABLE_FIELDS, QUEST_ID, RECORD_TABLE, UNPLAYED,
                                         archive_missing, edit_record, grant_clear_rewards, master_quest_ids,
                                         read_archive, restore_archived)
from aigis_ps.quest_record_service import RECORD_TABLE as SERVED_TABLE, UNPLAYED as SERVED_UNPLAYED
from test_quest_service import bundle


NOW = datetime(2026, 11, 5, 12, tzinfo=JST)
ACQUISITION_DAY = civil_day(NOW)
FOREIGN_QUEST = 999_999_999
THREE_STAR_FLAGS = CLEAR_FLAG | THREE_STAR_FLAG


@pytest.fixture(scope="module")
def master_ids(bundle):
    return master_quest_ids(bundle.bootstrap)


@pytest.fixture(scope="module")
def story_quest(bundle, master_ids):
    def rewardable(quest_id):
        mode = bundle.quests.catalog.mode(quest_id)
        return (mode.quest_type == 0 and not mode.score_mode and mode.star_rewards_normal
                and all(reward.kind != "unsupported" for reward in mode.star_rewards_normal))

    return next(quest_id for quest_id in sorted(master_ids) if rewardable(quest_id))


@pytest.fixture
def state(bundle):
    return bundle.initialize(bundle.player_state.initialize(bundle.bootstrap.initial_state()))


class RecordingMissions:
    def __init__(self, real):
        self.real, self.calls = real, []

    def record(self, state, kind, payload, now):
        self.calls.append((kind, payload, now))
        return self.real.record(state, kind, payload, now)


class RecordingRankRewards:
    def __init__(self, real):
        self.real, self.calls = real, []

    def apply(self, before, candidate, profile=None):
        self.calls.append((before, candidate))
        return self.real.apply(before, candidate, profile)


def record_row(quest_id, **changes):
    return {QUEST_ID: quest_id, **UNPLAYED, **changes}


def rewarded(bundle, state, quest_id, *, stars=3, hard=False, previous_flags=0, current_flags=THREE_STAR_FLAGS):
    missions, rank = RecordingMissions(bundle.missions), RecordingRankRewards(bundle.rank_rewards)
    updated, receipt, warnings = grant_clear_rewards(
        state, quests=bundle.quests, missions=missions, rank_rewards=rank, players=bundle.player_state,
        possessions=bundle.possessions, presents=bundle.presents, quest_id=quest_id, stars=stars, hard=hard,
        previous_flags=previous_flags, current_flags=current_flags, now=NOW, acquisition_day=ACQUISITION_DAY)
    return updated, receipt, warnings, missions, rank


def test_the_mirrored_constants_match_the_record_route():
    assert RECORD_TABLE == SERVED_TABLE and UNPLAYED is SERVED_UNPLAYED
    assert EDITABLE_FIELDS == tuple(UNPLAYED)
    assert COUNTER_FIELDS is QUEST_RECORD_COUNTERS
    # Identifier-like literals are interned, so only the import itself proves a single source for the table key.
    tree = ast.parse(Path(record_admin_module.__file__).read_bytes())
    imported = {alias.name for node in tree.body if isinstance(node, ast.ImportFrom)
                and node.module == "quest_record_service" for alias in node.names}
    assigned = {target.id for node in tree.body if isinstance(node, ast.Assign)
                for target in node.targets if isinstance(target, ast.Name)}
    assert {"RECORD_TABLE", "UNPLAYED"} <= imported and not {"RECORD_TABLE", "UNPLAYED"} & assigned


def test_a_save_holding_an_archive_reinitializes_unchanged(bundle, state, story_quest):
    archived, _ = archive_missing(seeded(state, story_quest), bundle.bootstrap, master_quest_ids(bundle.bootstrap))
    assert bundle.initialize(deepcopy(archived)) == archived


def test_edits_touch_only_the_quest_record_rows(bundle, state, story_quest):
    untouched = deepcopy(state)
    updated, before, after = edit_record(state, bundle.bootstrap, story_quest,
                                         {"MaxRank": 3, "AchieveFlag": THREE_STAR_FLAGS, "Shown": True})
    assert state == untouched and before is None
    assert after == record_row(story_quest, MaxRank=3, AchieveFlag=THREE_STAR_FLAGS, Shown=True)
    assert after in updated["tables"][RECORD_TABLE]
    assert {name: value for name, value in updated.items() if name != "tables"} == {
        name: value for name, value in state.items() if name != "tables"}
    assert {name: value for name, value in updated["tables"].items() if name != RECORD_TABLE} == {
        name: value for name, value in state["tables"].items() if name != RECORD_TABLE}


def test_edits_write_the_achieve_flag_in_its_stored_signed_form(bundle, state, story_quest):
    updated, _, after = edit_record(state, bundle.bootstrap, story_quest, {"AchieveFlag": 0x80000000})
    assert after["AchieveFlag"] == signed32(0x80000000) == INT32_MIN
    assert next(row for row in updated["tables"][RECORD_TABLE] if row[QUEST_ID] == story_quest) == after


def test_edits_refuse_unknown_quests_unknown_fields_and_unchanged_rows(bundle, state, story_quest):
    with pytest.raises(ProtocolError) as unknown:
        edit_record(state, bundle.bootstrap, FOREIGN_QUEST, {"MaxRank": 3})
    assert (unknown.value.code, unknown.value.status) == ("quest_not_found", 404)
    for changes in ({}, {"QuestID": 1}, {"MaxRank": 1, "Stars": 1}):
        with pytest.raises(ProtocolError) as invalid:
            edit_record(state, bundle.bootstrap, story_quest, changes)
        assert invalid.value.code == "quest_record_edit_invalid"
    edited, _, _ = edit_record(state, bundle.bootstrap, story_quest, {"Shown": True})
    with pytest.raises(ProtocolError) as unchanged:
        edit_record(edited, bundle.bootstrap, story_quest, {"Shown": True})
    assert (unchanged.value.code, unchanged.value.status) == ("quest_record_unchanged", 409)


def seeded(state, story_quest):
    rows = [record_row(story_quest, MaxRank=3, AchieveFlag=THREE_STAR_FLAGS),
            record_row(FOREIGN_QUEST, MaxRank=2, AchieveFlag=CLEAR_FLAG, Shown=True)]
    return {**state, "tables": {**state["tables"], RECORD_TABLE: rows}}


def test_archiving_moves_only_the_rows_absent_from_the_target_master(bundle, state, story_quest, master_ids):
    held = seeded(state, story_quest)
    updated, moved = archive_missing(held, bundle.bootstrap, master_ids)
    assert moved == [record_row(FOREIGN_QUEST, MaxRank=2, AchieveFlag=CLEAR_FLAG, Shown=True)]
    assert [row[QUEST_ID] for row in updated["tables"][RECORD_TABLE]] == [story_quest]
    assert read_archive(updated) == moved
    same, nothing = archive_missing(updated, bundle.bootstrap, master_ids)
    assert same is updated and nothing == []


def test_restores_return_archived_rows_only_for_the_running_master(bundle, state, story_quest, master_ids):
    archived, moved = archive_missing(seeded(state, story_quest), bundle.bootstrap, master_ids)
    skipped_state, restored, skipped = restore_archived(archived, bundle.bootstrap, [FOREIGN_QUEST], master_ids)
    assert skipped_state is archived and restored == [] and skipped == [FOREIGN_QUEST]
    updated, restored, skipped = restore_archived(archived, bundle.bootstrap, [FOREIGN_QUEST],
                                                  frozenset((*master_ids, FOREIGN_QUEST)))
    assert restored == moved and skipped == []
    assert ARCHIVE_KEY not in updated and read_archive(updated) == []
    assert record_row(FOREIGN_QUEST, MaxRank=2, AchieveFlag=CLEAR_FLAG, Shown=True) in updated["tables"][RECORD_TABLE]


def test_restores_refuse_unknown_archive_ids_and_rows_that_already_exist(bundle, state, story_quest, master_ids):
    archived, _ = archive_missing(seeded(state, story_quest), bundle.bootstrap, master_ids)
    with pytest.raises(ProtocolError) as missing:
        restore_archived(archived, bundle.bootstrap, [story_quest + 1], master_ids)
    assert (missing.value.code, missing.value.status) == ("quest_record_archive_missing", 404)
    doubled = {**archived, "tables": {**archived["tables"],
                                      RECORD_TABLE: [*archived["tables"][RECORD_TABLE],
                                                     record_row(FOREIGN_QUEST)]}}
    with pytest.raises(ProtocolError) as present:
        restore_archived(doubled, bundle.bootstrap, [FOREIGN_QUEST], frozenset((*master_ids, FOREIGN_QUEST)))
    assert (present.value.code, present.value.status) == ("quest_record_exists", 409)


def test_default_edits_never_count_missions_or_pay_rewards(bundle, state, story_quest):
    updated, _, _ = edit_record(state, bundle.bootstrap, story_quest, {"MaxRank": 3,
                                                                       "AchieveFlag": THREE_STAR_FLAGS})
    assert updated.get("mission_state") == state.get("mission_state")
    assert updated.get("rank_rewards") == state.get("rank_rewards")
    assert wallet_totals(updated) == wallet_totals(state)
    assert updated["tables"]["presents"] == state["tables"]["presents"]


def test_explicit_rewards_count_the_clear_and_pay_the_first_star_in_one_pass(bundle, state, story_quest):
    mode = bundle.quests.catalog.mode(story_quest)
    rewards = [asdict(reward) for reward in mode.star_rewards_normal]
    edited, _, after = edit_record(state, bundle.bootstrap, story_quest, {"MaxRank": 3,
                                                                          "AchieveFlag": THREE_STAR_FLAGS})
    updated, receipt, _, missions, rank = rewarded(bundle, edited, story_quest,
                                                   current_flags=after["AchieveFlag"])
    assert receipt["first_star"] is True and receipt["star_rewards"] == rewards
    assert [(kind, payload) for kind, payload, _ in missions.calls] == [("quest_clear", {
        "quest_id": story_quest, "quest_type": 0, "stars": 3, "hard": False,
        "team": [], "score_mode": False, "kills": 0})]
    assert [now for _, _, now in missions.calls] == [NOW]
    assert len(rank.calls) == 1 and rank.calls[0][0] is edited
    crystals = sum(reward["count"] if reward["count"] > 0 else bundle.quests.rules.first_three_star_crystals
                   for reward in rewards if reward["kind"] == "crystal")
    assert receipt["crystals"] == crystals and receipt["crystal_presents"] == []
    assert wallet_totals(updated)["free"] - wallet_totals(state)["free"] == crystals
    expected_units = sum(reward["count"] for reward in rewards if reward["kind"] == "unit")
    assert len(receipt["units"]) == expected_units
    assert receipt["rank_mail_ids"] == []


def test_rewards_without_a_new_first_star_only_count_the_clear(bundle, state, story_quest):
    edited, _, after = edit_record(state, bundle.bootstrap, story_quest, {"MaxRank": 3,
                                                                          "AchieveFlag": THREE_STAR_FLAGS})
    flags = after["AchieveFlag"]
    updated, receipt, warnings, missions, _ = rewarded(bundle, edited, story_quest,
                                                       previous_flags=flags, current_flags=flags)
    assert receipt["first_star"] is False and receipt["star_rewards"] == []
    assert receipt["crystals"] == 0 and receipt["units"] == [] and warnings == ()
    assert wallet_totals(updated) == wallet_totals(edited)
    assert [kind for kind, _, _ in missions.calls] == ["quest_clear"]


def test_score_quests_refuse_manual_clear_rewards(bundle, state, master_ids):
    score_quest = next((quest_id for quest_id in sorted(master_ids)
                        if bundle.quests.catalog.mode(quest_id).score_mode), None)
    if score_quest is None:
        pytest.skip("The fixed master snapshot lists no score quests")
    with pytest.raises(ProtocolError) as caught:
        rewarded(bundle, state, score_quest)
    assert (caught.value.code, caught.value.status) == ("quest_record_rewards_unsupported", 409)
