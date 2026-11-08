from argparse import Namespace
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from aigis_ps.admin_commit import InitializerRestartCheck
from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.cli import services
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import EMPTY_SQUAD_SLOT, LOCK_FLAG, PlayerStateService, squad_group
from aigis_ps.unit_grants import UnitGrants
from aigis_ps.unit_removal import (SAVE_PROTECTIONS, STRICT_REMOVAL, Protection, RemovalPolicy, UnitIdentity, UnitRemoval,
                                   UnitsProtected, consume_units, protection_check, refused_protections, unit_protections)
from test_player_state import assets


CONFIG = {"UseBgm": True, "UseSe": True, "MenuTutorialFlags": 0, "_VolumeBgm": 80, "_VolumeSe": 80,
          "BattlePlaceMagnetFlag": 0, "BattleAcceleratorFlag": 0, "BattleDClickSkillDisableFlag": 0, "_GuideUnitIndex": 0}
LOCKED, SQUADDED, LEADING, SECRETARY, RESERVED, FREE = range(1, 7)
ROSTER_CARDS = {LOCKED: 2, SQUADDED: 2, LEADING: 3, SECRETARY: 3, RESERVED: 4, FREE: 2}
RESERVED_CARD = ROSTER_CARDS[RESERVED]
OTHER_GROUP = 3
SECOND_SUB_TEAM = 1 << 16
PRINCE_INDEX = 0
EXPECTED_PROTECTIONS = {
    PRINCE_INDEX: (Protection.PRINCE,),
    LOCKED: (Protection.LOCKED,),
    SQUADDED: (Protection.SQUAD,),
    LEADING: (Protection.SQUAD, Protection.CURRENT_SQUAD),
    SECRETARY: (Protection.SECRETARY,),
    RESERVED: (Protection.BATTLE_RESERVATION,),
    FREE: (),
}
PERMISSIVE = RemovalPolicy(frozenset({Protection.PRINCE, Protection.LOCKED}))
OPEN = RemovalPolicy(frozenset())
ROSTER_ONLY = RemovalPolicy(frozenset({Protection.PRINCE, Protection.LOCKED, Protection.SQUAD}))
LEGACY_QUEST = 1001
LIVE_CARD, TRUST_TIERS = 17, [1, 100]


@pytest.fixture
def master():
    cards = (1, 2, 3, 4)
    return dumps(Container(children=(Column("CardID", "I", cards), Column("InitClassID", "I", (10, 20, 20, 20)),
                                     Column("ClassLV0SkillID", "I", (59, 3, 3, 3)),
                                     Column("ClassLV1SkillID", "I", (59, 4, 4, 4)))))


@pytest.fixture
def players(assets, master):
    return PlayerStateService(assets[0], lambda name: master)


class Awakening:
    def __init__(self):
        self.rosters = []

    def initialize(self, state):
        self.rosters.append(roster_indices(state))
        updated = deepcopy(state)
        updated.setdefault("evolution_rewards", {"claimed": []})
        return updated


class Gifts:
    def __init__(self):
        self.calls = []

    def preserve_history(self, state, revision):
        self.calls.append((roster_indices(state), revision, "evolution_rewards" in state))
        updated = deepcopy(state)
        updated.setdefault("gift_state", {"claims": {}})
        return updated


def roster_indices(state):
    return sorted(unit["GetIndex"] for unit in state["player_state"]["units"])


def squad_row(group, slot, unit):
    return {"Address": group << 24 | slot, "UnitIndex": unit, "Name": f"team {group}", "Flags": 0}


@pytest.fixture
def roster(players):
    state = players.initialize({"bootstrap_schema": 1, "tables": {"config": [dict(CONFIG)]}})
    player = state["player_state"]
    prince = player["units"][0]
    for index, card in ROSTER_CARDS.items():
        player["units"].append({**prince, "CardID": card, "ClassID": 20, "GetIndex": index, "UniqueID": index + 1,
                                "GeneralPurposeFlags": 1, "SettingSkillID": 3})
    player["units"][LOCKED]["GeneralPurposeFlags"] |= LOCK_FLAG
    player["battle"]["UX"] = max(ROSTER_CARDS)
    player["squads"] = [squad_row(OTHER_GROUP, 0, SQUADDED), squad_row(player["battle"]["TN"], 0, LEADING),
                        squad_row(player["battle"]["TN"], 1, EMPTY_SQUAD_SLOT)]
    state["tables"]["config"][0]["_GuideUnitIndex"] = SECRETARY
    state["quest_state"] = {"active": {"unit_reservations": [{"card_id": RESERVED_CARD, "level": 1, "count": 1}]}}
    return state


@pytest.fixture
def ledgers():
    return Awakening(), Gifts()


@pytest.fixture
def removal(players, ledgers):
    awakening, gifts = ledgers
    return UnitRemoval(players, gifts, awakening)


def identity(state, index):
    unit = next(unit for unit in state["player_state"]["units"] if unit["GetIndex"] == index)
    return UnitIdentity(unit["GetIndex"], unit["UniqueID"])


def refused(removal, state, selection, code, policy=STRICT_REMOVAL):
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        removal.remove(state, 7, selection, policy)
    assert state == before
    assert caught.value.code == code
    return caught.value


def test_protections_name_every_reason_a_unit_is_kept(players, roster):
    assert dict(unit_protections(players, roster)) == EXPECTED_PROTECTIONS
    assert squad_group(roster["player_state"]["squads"][0]) == OTHER_GROUP


def test_every_raid_sub_team_counts_as_the_current_squad(players, roster):
    raid = roster["player_state"]["battle"]["RTN"]
    roster["player_state"]["squads"].append(squad_row(raid, SECOND_SUB_TEAM, FREE))
    assert squad_group(roster["player_state"]["squads"][-1]) == raid
    assert unit_protections(players, roster)[FREE] == (Protection.SQUAD, Protection.CURRENT_SQUAD)


def test_the_secretary_needs_a_saved_selection_and_the_battle_needs_reservations(players, roster):
    roster["tables"]["config"][0]["_GuideUnitIndex"] = 0
    del roster["quest_state"]
    protections = unit_protections(players, roster)
    assert protections[SECRETARY] == () and protections[RESERVED] == ()
    roster["quest_state"] = {"active": None}
    assert unit_protections(players, roster)[RESERVED] == ()


def test_strict_removal_refuses_each_protected_unit_before_touching_the_ledgers(removal, roster, ledgers):
    protected = [identity(roster, index) for index in EXPECTED_PROTECTIONS if EXPECTED_PROTECTIONS[index]]
    error = refused(removal, roster, protected, "unit_removal_protected")
    assert isinstance(error, UnitsProtected) and error.status == 409
    assert dict(error.protected) == {index: found for index, found in EXPECTED_PROTECTIONS.items() if found}
    assert ledgers[0].rosters == [] and ledgers[1].calls == []


def test_removal_freezes_both_reward_ledgers_from_the_roster_that_still_holds_the_units(removal, roster, ledgers):
    result = removal.remove(roster, 7, [identity(roster, FREE)])
    awakening, gifts = ledgers
    assert awakening.rosters == [roster_indices(roster)]
    assert gifts.calls == [(roster_indices(roster), 7, True)]
    assert "evolution_rewards" in result.state and "gift_state" in result.state
    assert roster_indices(result.state) == [index for index in roster_indices(roster) if index != FREE]
    assert [unit["GetIndex"] for unit in result.removed] == [FREE] and result.vacated == ()


def test_removal_keeps_the_identity_high_water_mark(removal, roster):
    newest = identity(roster, FREE)
    assert "last_unique_id" not in roster["player_state"]
    result = removal.remove(roster, 7, [newest])
    assert result.state["player_state"]["last_unique_id"] == newest.unique_id
    assert max(unit["UniqueID"] for unit in result.state["player_state"]["units"]) < newest.unique_id
    roster["player_state"]["last_unique_id"] = 900
    assert removal.remove(roster, 7, [newest]).state["player_state"]["last_unique_id"] == 900


def test_relaxed_policies_empty_the_squad_slots_of_removed_members(removal, roster):
    selection = [identity(roster, index) for index in (SQUADDED, LEADING, SECRETARY, RESERVED)]
    result = removal.remove(roster, 7, selection, PERMISSIVE)
    squads = result.state["player_state"]["squads"]
    assert [row["UnitIndex"] for row in squads] == [EMPTY_SQUAD_SLOT] * 3
    assert result.vacated == tuple(row["Address"] for row in roster["player_state"]["squads"][:2])
    # A removed secretary keeps its saved selection, which the native client answers with the default guide.
    assert result.state["tables"]["config"][0]["_GuideUnitIndex"] == SECRETARY
    assert [unit["GetIndex"] for unit in result.removed] == [SQUADDED, LEADING, SECRETARY, RESERVED]


def test_removal_refuses_only_the_protections_its_policy_names(removal, roster):
    error = refused(removal, roster, [identity(roster, LOCKED), identity(roster, LEADING)], "unit_removal_protected",
                    PERMISSIVE)
    assert dict(error.protected) == {LOCKED: (Protection.LOCKED,)}


def test_removal_refuses_selections_that_do_not_match_owned_units(removal, roster):
    free = identity(roster, FREE)
    refused(removal, roster, [UnitIdentity(99, 100)], "unit_removal_missing")
    refused(removal, roster, [UnitIdentity(free.get_index, free.unique_id + 1)], "unit_removal_missing")
    for selection in ([], [free, free], [free.get_index], (free for _ in range(1)), "units"):
        assert refused(removal, roster, selection, "unit_removal_invalid").status == 400


def test_removal_never_empties_the_roster(removal, roster):
    everything = [identity(roster, index) for index in roster_indices(roster)]
    assert refused(removal, roster, everything, "unit_removal_invalid", OPEN).status == 400


def test_removal_waits_for_legacy_battles_like_grants_do(removal, roster):
    roster["quest_state"] = {"active": {"quest_id": LEGACY_QUEST}}
    refused(removal, roster, [identity(roster, FREE)], "quest_reservation_unavailable")


def test_relaxed_policies_never_read_the_battle_journal(players, removal, roster):
    roster["quest_state"] = {"active": {"quest_id": LEGACY_QUEST}}
    result = removal.remove(roster, 7, [identity(roster, FREE)], PERMISSIVE)
    assert FREE not in roster_indices(result.state)
    assert unit_protections(players, roster, PERMISSIVE.refuse)[LOCKED] == (Protection.LOCKED,)


def test_relaxed_policies_never_read_the_saved_guide(removal, roster):
    roster["tables"]["config"][0]["_GuideUnitIndex"] = str(SECRETARY)
    refused(removal, roster, [identity(roster, FREE)], "invalid_configuration_state")
    result = removal.remove(roster, 7, [identity(roster, FREE)], PERMISSIVE)
    assert FREE not in roster_indices(result.state)


def test_roster_policies_check_selected_units_without_the_save(players, roster):
    player = roster["player_state"]
    selected = [unit_at(roster, index) for index in (LEADING, FREE, LOCKED, SECRETARY, RESERVED, PRINCE_INDEX)]
    found = refused_protections(player, selected, ROSTER_ONLY)
    assert list(found.items()) == [(LEADING, (Protection.SQUAD,)), (LOCKED, (Protection.LOCKED,)),
                                   (PRINCE_INDEX, (Protection.PRINCE,))]
    assert protection_check(player, frozenset())(unit_at(roster, LOCKED)) == ()
    saved = {Protection.SECRETARY: SECRETARY, Protection.BATTLE_RESERVATION: RESERVED}
    assert saved.keys() == SAVE_PROTECTIONS
    for protection, index in saved.items():
        for partial in ({}, {"players": players}, {"state": roster}):
            with pytest.raises(ValueError):
                protection_check(player, frozenset({protection}), **partial)
        check = protection_check(player, frozenset({protection}), players=players, state=roster)
        assert check(unit_at(roster, index)) == (protection,) and check(unit_at(roster, FREE)) == ()


def test_consuming_units_keeps_the_high_water_mark_and_empties_their_squad_slots(roster):
    player = deepcopy(roster["player_state"])
    newest = unit_at(roster, FREE)["UniqueID"]
    assert consume_units(player, ()) == () and player["last_unique_id"] == newest
    assert player["units"] == roster["player_state"]["units"] and player["squads"] == roster["player_state"]["squads"]
    vacated = consume_units(player, [unit_at(roster, LEADING), unit_at(roster, FREE)])
    assert vacated == (roster["player_state"]["squads"][1]["Address"],)
    assert [row["UnitIndex"] for row in player["squads"]] == [SQUADDED, EMPTY_SQUAD_SLOT, EMPTY_SQUAD_SLOT]
    assert [unit["GetIndex"] for unit in player["units"]] == [PRINCE_INDEX, LOCKED, SQUADDED, SECRETARY, RESERVED]
    assert player["last_unique_id"] == newest


def test_removal_returns_new_objects_and_leaves_its_input_alone(removal, roster):
    before = deepcopy(roster)
    result = removal.remove(roster, 7, [identity(roster, FREE)])
    result.removed[0]["Love"] = 77
    result.state["player_state"]["units"][0]["Love"] = 66
    assert roster == before


@pytest.mark.parametrize("missing", ["gifts", "evolution_rewards"])
def test_removal_refuses_to_run_without_either_reward_ledger(players, ledgers, missing):
    awakening, gifts = ledgers
    arguments = {"gifts": gifts, "evolution_rewards": awakening, missing: None}
    with pytest.raises(ProtocolError) as caught:
        UnitRemoval(players, arguments["gifts"], arguments["evolution_rewards"], error_prefix="admin_unit_removal")
    assert (caught.value.code, caught.value.status) == ("admin_unit_removal_dependencies_unavailable", 503)


def test_custom_prefixes_name_every_refusal(players, ledgers, roster):
    awakening, gifts = ledgers
    removal = UnitRemoval(players, gifts, awakening, error_prefix="sale_unit")
    refused(removal, roster, [identity(roster, LOCKED)], "sale_unit_protected")
    refused(removal, roster, [UnitIdentity(99, 100)], "sale_unit_missing")


@pytest.mark.parametrize("prefix", [None, "", "has space", "UPPER", "x" * 65])
def test_error_prefixes_are_bounded(players, ledgers, prefix):
    with pytest.raises(ValueError):
        UnitRemoval(players, ledgers[1], ledgers[0], error_prefix=prefix)


def test_policies_and_identities_validate_their_shape(removal, roster):
    assert STRICT_REMOVAL.refuse == frozenset(Protection)
    for refuse in ({"prince"}, [Protection.PRINCE], "prince", None):
        with pytest.raises(ValueError):
            RemovalPolicy(refuse)
    for values in ((-1, 1), (1, 0), (True, 1), (1, 2 ** 31), (1.0, 1)):
        with pytest.raises(ValueError):
            UnitIdentity(*values)
    with pytest.raises(FrozenInstanceError):
        STRICT_REMOVAL.refuse = frozenset()
    with pytest.raises(ValueError):
        removal.remove(roster, 7, [identity(roster, FREE)], policy=frozenset())


@pytest.fixture(scope="module")
def bundle():
    project = Path(__file__).resolve().parents[1]
    args = Namespace(asset_dir=project.parent / "aigisr_exe", data_dir=project / "runtime/server",
                     resource_dir=project / "runtime/resources", rules=None)
    if not (args.asset_dir / "Aigis_r.exe").is_file() or not (args.data_dir / "masterdata/active.json").is_file():
        pytest.skip("Verified local client resources and public masters are required")
    return services(args)


def new_save(bundle):
    return bundle.initialize(bundle.bootstrap.initial_state())


def granted(bundle, state, **spec):
    return UnitGrants(bundle.player_state).grant(state, [{"level": 1, **spec}])


def unit_at(state, index):
    return next(unit for unit in state["player_state"]["units"] if unit["GetIndex"] == index)


def mails(state):
    return state["tables"]["presents"]


def live_removal(bundle):
    return UnitRemoval(bundle.player_state, bundle.gifts, bundle.evolution_rewards)


def test_old_saves_keep_the_first_awakening_reward_of_a_removed_unit_claimed(bundle):
    state, added = granted(bundle, new_save(bundle), card_id=LIVE_CARD, evolution_count=2, count=2)
    awakened, sibling = (unit["GetIndex"] for unit in added)
    unit_at(state, awakened)["ClassChangeCount"] = 3
    del state["evolution_rewards"]
    result = live_removal(bundle).remove(state, 0, [identity(state, awakened)])
    assert result.state["evolution_rewards"]["claimed"] == [LIVE_CARD]
    candidate = deepcopy(result.state)
    unit_at(candidate, sibling)["ClassChangeCount"] = 3
    rewarded, _ = bundle.evolution_rewards.apply(result.state, candidate, LIVE_CARD)
    assert mails(rewarded) == mails(result.state)
    # Rebuilding the baseline after the unit is gone would pay the same first reward a second time.
    unfrozen = deepcopy(state)
    unfrozen["player_state"]["units"] = [unit for unit in unfrozen["player_state"]["units"] if unit["GetIndex"] != awakened]
    candidate = deepcopy(unfrozen)
    unit_at(candidate, sibling)["ClassChangeCount"] = 3
    rewarded, _ = bundle.evolution_rewards.apply(unfrozen, candidate, LIVE_CARD)
    assert len(mails(rewarded)) == len(mails(unfrozen)) + 1


def test_removal_preserves_the_trust_tiers_of_the_removed_unit(bundle):
    state, added = granted(bundle, new_save(bundle), card_id=LIVE_CARD)
    unit_at(state, added[0]["GetIndex"])["Love"] = 100
    assert "gift_state" not in state
    result = live_removal(bundle).remove(state, 0, [identity(state, added[0]["GetIndex"])])
    assert result.state["gift_state"]["claims"] == {str(LIVE_CARD): TRUST_TIERS}
    assert mails(result.state) == mails(state)


def test_live_removal_results_survive_a_restart_unchanged(bundle):
    state, added = granted(bundle, new_save(bundle), card_id=LIVE_CARD, count=3)
    result = live_removal(bundle).remove(state, 0, [identity(state, unit["GetIndex"]) for unit in added[:2]])
    InitializerRestartCheck(bundle.initialize).check(result.state)
    assert result.state["player_state"]["last_unique_id"] == added[-1]["UniqueID"]
    assert roster_indices(result.state) == [PRINCE_INDEX, added[-1]["GetIndex"]]


def test_live_new_saves_protect_only_the_prince(bundle):
    assert dict(unit_protections(bundle.player_state, new_save(bundle))) == {PRINCE_INDEX: (Protection.PRINCE,)}
