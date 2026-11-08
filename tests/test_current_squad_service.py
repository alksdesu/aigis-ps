from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime
import json
from threading import Barrier, Lock
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

import aigis_ps.current_squad as module
import aigis_ps.request_ledger as ledger
from aigis_ps.alxml import Column, Container, loads
from aigis_ps.current_squad import CurrentSquadService, raid_group
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import UNIT_FIELDS
from aigis_ps.request_ledger import HISTORY_LIMIT, LEGACY_SCHEMA_VERSION
from aigis_ps.storage import Store
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


@pytest.fixture
def case(bundle, tmp_path):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    emblems = copy(bundle.presents.emblem_catalog)
    reader = lambda name: bundle.native_cdn.assets.read(name)[0]
    service = CurrentSquadService(bootstrap, bundle.player_state, emblems, reader)
    initial = bundle.player_state.initialize(bootstrap.initial_state())
    initial["sentinel"] = {"nested": ["untouched"]}
    return SimpleNamespace(service=service, bundle=bundle, reader=reader,
        store=Store(tmp_path / "current-squad.sqlite3", initial))


def message(case, group=0, name="編成", emblem=0, *, tutorial=None, tick=1000, extra=(), selector="OI"):
    columns = [Column(selector, "I", (group,)), Column("A3", "S", (name,)), Column("A4", "I", (emblem,))]
    if tutorial is not None:
        columns.append(Column("TL", "I", (tutorial,)))
    return request(case.store, 120, (*columns, *extra), tick=tick)


def change(case, update):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    update(state)
    case.store.update_state(state, profile.revision)


def overwrite(case, update):
    state = case.store.profile().state
    update(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def failure(case, envelope, code=None):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.save(envelope, case.store)
    if code is not None:
        assert caught.value.code == code
    assert case.store.profile() == before
    return caught.value


def nodes(payload):
    return {node.name: node for node in loads(payload).children}


def row(group, team=0, slot=1, unit=-1, name="旧隊", emblem=0):
    address = (group << 24) | (team << 16) | slot
    if address >= 0x80000000:
        address -= 0x100000000
    return {"Address": address, "UnitIndex": unit, "Name": name, "Flags": emblem}


def target_rows(case, group):
    return [value for value in case.store.profile().state["player_state"]["squads"]
            if (value["Address"] & 0xFFFFFFFF) >> 24 == group]


def test_real_status_and_new_empty_group_change_only_metadata(case):
    status = case.service.status()
    assert status["ready"] and status["ordinary_team_count"] == 20
    assert status["implemented_operations"] == [120]
    assert case.service.handlers() == {120: case.service.save}
    before = case.store.profile()
    payload = case.service.save(message(case, 19, "最終隊"), case.store)
    after = case.store.profile()
    expected = deepcopy(before.state)
    expected["player_state"]["battle"]["TN"] = 19
    expected["player_state"]["legacy_squad_imported"] = True
    expected["player_state"]["squads"] = [row(19, slot=slot, name="最終隊") for slot in range(1, 16)]
    assert {key: value for key, value in after.state.items() if key != "current_squad_history"} == expected
    assert after.revision == before.revision + 1
    assert set(nodes(payload)) == {"SYSTEM", "PPU"}
    assert [column.name for column in nodes(payload)["PPU"].children] == [alias for _, alias in UNIT_FIELDS]
    assert all(column.values == () for column in nodes(payload)["PPU"].children)


def test_all_existing_subteams_receive_metadata_and_keep_members(case):
    rows = [row(9, unit=0), row(9, team=2, slot=5), row(9, team=3, slot=15),
            row(1, name="外部", emblem=0x12340000), row(200, name="特殊", emblem=-1)]
    change(case, lambda state: state["player_state"].update(squads=deepcopy(rows)))
    before = case.store.profile()
    case.service.save(message(case, 9, "変更", 1), case.store)
    after = case.store.profile().state["player_state"]
    assert after["units"] == before.state["player_state"]["units"]
    assert after["battle"] == {**before.state["player_state"]["battle"], "TN": 9}
    assert after["squads"] == [{**value, "Name": "変更", "Flags": 1} if index < 3 else value
                              for index, value in enumerate(rows)]


def test_flags_are_entire_emblem_id_not_bitwise_merge(case):
    change(case, lambda state: state["player_state"].update(squads=[row(0, emblem=0x7FFFFF00)]))
    case.service.save(message(case, 0, emblem=1), case.store)
    assert target_rows(case, 0)[0]["Flags"] == 1
    case.service.save(message(case, 0, emblem=0, tick=1001), case.store)
    assert target_rows(case, 0)[0]["Flags"] == 0


def test_switch_preserves_all_prince_fields_even_when_locked_and_in_other_barracks(case):
    def update(state):
        state["player_state"]["units"][0].update(GeneralPurposeFlags=-0x7FFFFFCD,
            OrganizationIndex=123, OrganizationIndex2=0x12345678, SkillExperience=7, CostExperience=8)
        state["player_state"].update(squads=[row(0, unit=0)])
    change(case, update)
    before = case.store.profile().state["player_state"]
    case.service.save(message(case, 1), case.store)
    after = case.store.profile().state["player_state"]
    assert after["units"] == before["units"]
    assert after.get("last_unique_id") == before.get("last_unique_id")
    assert after["battle"]["UX"] == before["battle"]["UX"]


@pytest.mark.parametrize("flags", [0, 1, 6, 0x40000000, -0x80000000, -1])
def test_tutorial_or6_is_atomic_and_preserves_signed_bits(case, flags):
    change(case, lambda state: state["tables"]["config"][0].update(MenuTutorialFlags=flags))
    before = case.store.profile()
    case.service.save(message(case, 2, tutorial=1), case.store)
    assert case.store.profile().state["tables"]["config"][0]["MenuTutorialFlags"] == flags | 6
    expected = deepcopy(before.state["tables"])
    expected["config"][0]["MenuTutorialFlags"] = flags | 6
    assert case.store.profile().state["tables"] == expected


def test_tutorial_absence_does_not_change_configuration(case):
    before = deepcopy(case.store.profile().state["tables"]["config"])
    case.service.save(message(case), case.store)
    assert case.store.profile().state["tables"]["config"] == before


def test_legacy_pending_is_not_hidden_by_creating_empty_group(case):
    change(case, lambda state: state["player_state"]["units"][0].update(OrganizationIndex2=1))
    failure(case, message(case), "current_squad_legacy_pending")


@pytest.mark.parametrize("ready", ["marker", "group_zero"])
def test_completed_or_unneeded_legacy_import_allows_selection_without_af_rewrite(case, ready):
    def update(state):
        state["player_state"]["units"][0]["OrganizationIndex2"] = -1
        if ready == "marker":
            state["player_state"]["legacy_squad_imported"] = True
        else:
            state["player_state"]["squads"] = [row(0)]
    change(case, update)
    case.service.save(message(case, 3), case.store)
    assert case.store.profile().state["player_state"]["units"][0]["OrganizationIndex2"] == -1
    assert case.store.profile().state["player_state"]["legacy_squad_imported"] is True


def test_successful_new_metadata_blocks_late_legacy_import(case):
    case.service.save(message(case, 8, "保持"), case.store)
    before = case.store.profile()
    legacy = request(case.store, 74, (
        Column("A0", "I", (1,)), Column("A1", "I", (0,)),
        Column("A3", "S", ("遅延",)), Column("A4", "I", (1,))))
    case.service.players.import_squads(legacy, case.store)
    assert case.store.profile() == before


@pytest.mark.parametrize("name", ["十二文字あいうえおかきく", "😀" * 12, " " * 12, "全角　空白"])
def test_new_names_count_unicode_scalars_not_utf8_bytes(case, name):
    assert len(name) <= 12
    case.service.save(message(case, name=name), case.store)
    assert {value["Name"] for value in target_rows(case, 0)} == {name}


@pytest.mark.parametrize("name", ["x" * 13, "😀" * 13, "line\rbreak", "line\nbreak", "path\\name", "can't"])
def test_new_invalid_names_reject_without_sanitizing(case, name):
    failure(case, message(case, name=name), "invalid_request_fields")


def test_existing_long_name_is_allowed_only_as_exact_return(case):
    name = "長" * 42
    change(case, lambda state: state["player_state"].update(squads=[row(4, name=name)]))
    case.service.save(message(case, 4, name), case.store)
    assert target_rows(case, 4)[0]["Name"] == name
    failure(case, message(case, 4, name[:-1], tick=1001), "invalid_request_fields")


def test_initial_empty_name_is_preserved_but_existing_nonempty_not_cleared(case):
    case.service.save(message(case, 0, ""), case.store)
    assert {value["Name"] for value in target_rows(case, 0)} == {""}
    case.service.save(message(case, 0, "名前", tick=1001), case.store)
    failure(case, message(case, 0, "", tick=1002), "invalid_request_fields")


def test_locked_emblem_rejects_and_owned_emblem_does_not_consume_item(case):
    failure(case, message(case, emblem=33), "emblem_locked")
    change(case, lambda state: state["tables"].update(possessions=[{"ItemID": "get_emblem_00001", "Count": 1}]))
    before = deepcopy(case.store.profile().state["tables"])
    case.service.save(message(case, emblem=33), case.store)
    assert target_rows(case, 0)[0]["Flags"] == 33
    assert case.store.profile().state["tables"] == before


def test_emblem_display_mapping_cannot_unlock_wrong_title(case):
    change(case, lambda state: state["tables"].update(possessions=[{"ItemID": "get_emblem_00034", "Count": 1}]))
    failure(case, message(case, emblem=70), "emblem_locked")
    case.service.save(message(case, emblem=71), case.store)
    assert target_rows(case, 0)[0]["Flags"] == 71


@pytest.mark.parametrize("group", [-1, 20, 64, 255, 0x7FFFFFFF])
def test_group_bounds_are_current_twenty_ordinary_teams(case, group):
    failure(case, message(case, group), "invalid_request_fields")


@pytest.mark.parametrize("emblem", [-1, 125, 0x7FFFFFFF])
def test_unknown_emblem_indices_are_rejected(case, emblem):
    failure(case, message(case, emblem=emblem), "emblem_unknown")


@pytest.mark.parametrize("extra", [(Container("PU"),), (Container("PU", (Column("A7", "I", (0,)), Column("AF", "I", (1,)))),)])
def test_unproduced_unit_update_branch_rejected(case, extra):
    assert failure(case, message(case, extra=extra), "current_squad_branch_unsupported").status == 501


def test_raid_selection_saves_raid_metadata_without_changing_ordinary_team(case):
    change(case, lambda state: state["player_state"]["battle"].update(TN=7))
    before = case.store.profile()
    payload = case.service.save(message(case, 64, "突撃", selector="RTN"), case.store)
    after = case.store.profile().state["player_state"]
    assert case.service.raid_group == case.service.status()["raid_group"] == 64
    assert after["battle"] == {**before.state["player_state"]["battle"], "RTN": 64}
    assert after["battle"]["TN"] == 7
    assert target_rows(case, 64) == [row(64, slot=slot, name="突撃") for slot in range(1, 16)]
    assert after["legacy_squad_imported"] is True and after["units"] == before.state["player_state"]["units"]
    assert set(nodes(payload)) == {"SYSTEM", "PPU"}
    assert all(column.values == () for column in nodes(payload)["PPU"].children)
    battle = nodes(case.service.players.battle_param(request(case.store, 39, ()), case.store))
    assert battle["TN"].values == (7,) and battle["RTN"].values == (64,)


def test_raid_selection_renames_every_raid_subteam_and_keeps_members(case):
    rows = [row(64, 0, 1, unit=0, name="旧突撃"), row(64, 1, 3, name="旧突撃"), row(64, 2, 15, name="旧突撃"),
            row(3, name="普通")]
    change(case, lambda state: state["player_state"].update(squads=deepcopy(rows), legacy_squad_imported=True))
    case.service.save(message(case, 64, "新突撃", 1, tutorial=1, selector="RTN"), case.store)
    after = case.store.profile().state
    assert after["player_state"]["squads"] == [{**value, "Name": "新突撃", "Flags": 1} if index < 3 else value
                                               for index, value in enumerate(rows)]
    assert after["tables"]["config"][0]["MenuTutorialFlags"] & 6 == 6


def test_raid_selection_keeps_pending_legacy_import_guard(case):
    change(case, lambda state: state["player_state"]["units"][0].update(OrganizationIndex2=1))
    failure(case, message(case, 64, selector="RTN"), "current_squad_legacy_pending")


@pytest.mark.parametrize("group", [-1, 0, 19, 20, 63, 65, 245, 255])
def test_raid_identifier_must_match_the_client_raid_group(case, group):
    failure(case, message(case, group, selector="RTN"), "invalid_request_fields")


@pytest.mark.parametrize("selectors", ["neither", "both"])
def test_selection_requires_exactly_one_group_identifier(case, selectors):
    if selectors == "both":
        envelope = message(case, extra=(Column("RTN", "I", (64,)),))
    else:
        envelope = message(case)
        envelope.document.remove(envelope.document.find("OI"))
    failure(case, envelope, "invalid_request_fields")


RAID_VIEW = ('SetSystemStateParam( "{view}::MaxTeamCount" , 20 );\n'
             'SetSystemStateParam( "{view}::UseTeamCount" , {used} );\n{buttons}')
TEAM_VIEW = module.RAID_TEAM_COMMANDS["OnCommandMenuRaidTeamView.atb"]
SALLY_VIEW = module.RAID_TEAM_COMMANDS["OnCommandMenuRaidSallyView.atb"]


def raid_commands(teams, used=None, view="CRaidTeamView"):
    buttons = "".join(f'SetSystemStateParam( "{view}::BT{index:02d}_TeamID"  , {team} );\n'
                      for index, team in enumerate(teams))
    return ["MnObj_SetParam( \"x\" );",
            RAID_VIEW.format(view=view, used=len(teams) if used is None else used, buttons=buttons)]


def test_raid_group_is_the_team_shared_by_every_used_raid_button():
    assert raid_group(raid_commands((64, 64, 64)), TEAM_VIEW) == 64
    assert raid_group(raid_commands((64, 64, 64, 9), used=3), TEAM_VIEW) == 64
    assert raid_group(raid_commands((254,)), TEAM_VIEW) == 254
    assert raid_group(raid_commands((64, 64, 64), view="CRaidSallyView"), SALLY_VIEW) == 64
    with pytest.raises(ValueError):
        raid_group(raid_commands((64, 64, 64)), SALLY_VIEW)


def test_both_raid_views_are_verified_and_versioned(case):
    hashes = case.service.status()["resource_sha256"]
    assert set(module.RAID_TEAM_COMMANDS) <= set(hashes)
    assert all(len(hashes[name]) == 64 for name in module.RAID_TEAM_COMMANDS)
    assert module.team_resources(case.reader) == (hashes, case.service.raid_group)


def test_raid_views_declaring_different_raid_groups_disable_the_service(case, monkeypatch):
    original = module._commands

    def diverged(reader, name):
        digest, commands = original(reader, name)
        if name == "OnCommandMenuRaidSallyView.atb":
            commands = [command.replace("64", "65") for command in commands]
        return digest, commands

    monkeypatch.setattr(module, "_commands", diverged)
    with pytest.raises(ProtocolError) as caught:
        CurrentSquadService(case.service.bootstrap, case.service.players, case.service.emblems, case.reader)
    assert caught.value.code == "current_squad_catalog_invalid" and caught.value.status == 503


@pytest.mark.parametrize("commands", [
    raid_commands((64, 65, 64)), raid_commands((64, 64), used=3), raid_commands((64,), used=0),
    raid_commands((19, 19, 19)), raid_commands((255, 255, 255)), raid_commands((64, 64, 64)) * 2,
    [raid_commands((64,))[1].replace("BT00", "BT01") + 'SetSystemStateParam( "CRaidTeamView::BT01_TeamID" , 64 );'],
    ["SetUpdaterFunc( \"Func_RaidTeamView\" );"],
])
def test_unverified_raid_team_resources_are_rejected(commands):
    with pytest.raises(ValueError):
        raid_group(commands, TEAM_VIEW)


def test_raid_group_above_signed_range_keeps_int32_addresses(case, monkeypatch):
    monkeypatch.setattr(case.service, "raid_group", 200)
    case.service.save(message(case, 200, "高位", selector="RTN"), case.store)
    assert target_rows(case, 200) == [row(200, slot=slot, name="高位") for slot in range(1, 16)]
    assert case.store.profile().state["player_state"]["battle"]["RTN"] == 200


@pytest.mark.parametrize("mutation", ["no_oi", "no_name", "no_emblem", "no_ct", "duplicate", "unknown",
    "oi_string", "name_integer", "many_emblems", "tl_zero", "tl_string", "root_attr", "mixed", "nested"])
def test_invalid_request_shapes_are_never_empty_success(case, mutation):
    envelope = message(case)
    document = envelope.document
    missing = {"no_oi": "OI", "no_name": "A3", "no_emblem": "A4", "no_ct": "CT"}
    if mutation in missing:
        document.remove(document.find(missing[mutation]))
    elif mutation == "duplicate":
        document.append(deepcopy(document.find("OI")))
    elif mutation == "unknown":
        ET.SubElement(document, "Gold", T="I")
    elif mutation == "oi_string":
        document.find("OI").set("T", "S")
    elif mutation == "name_integer":
        document.find("A3").set("T", "I")
    elif mutation == "many_emblems":
        ET.SubElement(document.find("A4"), "V").text = "1"
    elif mutation in ("tl_zero", "tl_string"):
        column = ET.SubElement(document, "TL", T="S" if mutation == "tl_string" else "I")
        ET.SubElement(column, "V").text = "0" if mutation == "tl_zero" else "1"
    elif mutation == "root_attr":
        document.set("T", "S")
    elif mutation == "mixed":
        document.find("A3").tail = "unexpected"
    elif mutation == "nested":
        ET.SubElement(document.find("A4/V"), "I").text = "1"
    failure(case, envelope, "invalid_request_fields")


@pytest.mark.parametrize("field", ["UI", "UK", "MK"])
def test_account_authentication_before_mutation(case, field):
    envelope = message(case)
    envelope.document.find(field + "/V").text = "different-account"
    failure(case, envelope)


def test_all_five_response_profiles_preflight_before_commit(case, monkeypatch):
    original = module.encode_response
    seen = []
    def check(profile, payload):
        seen.append(profile.registration_keys)
        return original(profile, payload)
    monkeypatch.setattr(module, "encode_response", check)
    case.service.save(message(case), case.store)
    assert {36, 39, 41, 72, 120} <= {number for keys in seen for number in keys}


def test_preflight_encodes_exactly_the_player_reads_the_client_receives(case, monkeypatch):
    original, payloads = module.encode_response, {}
    numbers = {id(profile): number for number, profile in case.service._profiles.items()}
    def record(profile, payload):
        payloads[numbers.get(id(profile))] = payload
        return original(profile, payload)
    monkeypatch.setattr(module, "encode_response", record)
    case.service.save(message(case, 19, "十九", 1, tutorial=1), case.store)
    players, store = case.service.players, case.store
    assert payloads[39] == players.battle_param(request(store, 39, ()), store)
    assert payloads[72] == players.load_squads(request(store, 72, ()), store)
    assert payloads[41] == players.player_units(request(store, 41, ()), store)


@pytest.mark.parametrize("encoder", ["battle_param_response", "squad_response", "unit_response"])
def test_preflight_refusals_come_from_the_public_player_encoders(case, monkeypatch, encoder):
    refused = ProtocolError("fixture_player_encoding", "The fixture refused this player read", 503)
    def refuse(*args):
        raise refused
    monkeypatch.setattr(case.service.players, encoder, refuse)
    assert failure(case, message(case, tutorial=1)) is refused


@pytest.mark.parametrize("number", [36, 39, 41, 72, 120])
def test_any_encoding_failure_rolls_back_metadata_and_tutorial(case, number):
    envelope = message(case, tutorial=1)
    if number == 120:
        envelope = replace(envelope, profile=replace(envelope.profile, response_limit=1))
    else:
        case.service._profiles[number] = replace(case.service._profiles[number], response_limit=1)
    failure(case, envelope, "response_too_large")


def test_readers_and_reopen_observe_same_authoritative_state(case):
    case.service.save(message(case, 19, "十九", 1, tutorial=1), case.store)
    before = case.store.profile()
    reopened = Store(case.store.path, {})
    assert reopened.profile() == before
    battle = nodes(case.service.players.battle_param(request(reopened, 39, ()), reopened))
    assert battle["TN"].values == (19,)
    squads = nodes(case.service.players.load_squads(request(reopened, 72, ()), reopened))["TGI"]
    assert squads.children[0].values == tuple((19 << 24) | slot for slot in range(1, 16))
    assert squads.children[2].values == ("十九",) * 15
    assert squads.children[3].values == (1,) * 15
    units = case.service.players.player_units(request(reopened, 41, ()), reopened)
    assert units == case.service.players.unit_response(before.state["player_state"]["units"])
    assert reopened.profile() == before


def test_exact_retry_and_reopen_do_not_commit_again(case):
    envelope = message(case, 19)
    response = case.service.save(envelope, case.store)
    before = case.store.profile()
    case.service = CurrentSquadService(case.service.bootstrap, case.service.players, case.service.emblems, case.reader)
    case.store = Store(case.store.path, {})
    assert case.service.save(envelope, case.store) == response
    assert case.store.profile() == before


def test_every_new_action_including_noop_gets_receipt(case):
    case.service.save(message(case, name="同一"), case.store)
    before = case.store.profile()
    case.service.save(message(case, name="同一", tick=1001), case.store)
    after = case.store.profile()
    assert after.revision == before.revision + 1
    assert after.state["player_state"] == before.state["player_state"]
    assert len(after.state["current_squad_history"]["receipts"]) == 2


def test_old_exact_packet_never_reverts_newer_team(case):
    envelope = message(case, 1, "前")
    case.service.save(envelope, case.store)
    case.service.save(message(case, 2, "後", tick=1001), case.store)
    failure(case, envelope, "current_squad_retry_stale")
    assert case.store.profile().state["player_state"]["battle"]["TN"] == 2


def test_client_clock_rollback_accepts_new_actions_and_rejects_exact_replays(case):
    later = message(case, 1, "後", tick=0x7FFFFFFF)
    case.service.save(later, case.store)
    case.service.save(message(case, 2, "戻", tick=-0x80000000), case.store)
    case.service.save(message(case, 3, "戻", tick=1000), case.store)
    case.service.save(message(case, 4, "同秒", tick=1000), case.store)
    assert case.store.profile().state["player_state"]["battle"]["TN"] == 4
    failure(case, later, "current_squad_retry_stale")
    history = case.store.profile().state["current_squad_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION and len(history["processed_keys"]) == 4
    assert all("client_time" not in entry for entry in history["receipts"])


def test_evicted_receipt_stays_stale(case):
    envelope = message(case, 0, "最初", tick=100)
    case.service.save(envelope, case.store)
    for index in range(HISTORY_LIMIT):
        case.service.save(message(case, index % 20, "継続", tick=101 + index), case.store)
    assert len(case.store.profile().state["current_squad_history"]["receipts"]) == HISTORY_LIMIT
    failure(case, envelope, "current_squad_request_stale")


def test_processed_window_bounds_replay_protection_without_blocking_new_actions(case, monkeypatch):
    monkeypatch.setattr(ledger, "HISTORY_LIMIT", 1)
    monkeypatch.setattr(ledger, "PROCESSED_LIMIT", 2)
    first = message(case, 0, "最初")
    case.service.save(first, case.store)
    case.service.save(message(case, 1, "次"), case.store)
    failure(case, first, "current_squad_request_stale")
    case.service.save(message(case, 2, "三番"), case.store)
    history = case.store.profile().state["current_squad_history"]
    assert len(history["receipts"]) == 1 and len(history["processed_keys"]) == 2
    case.service.save(first, case.store)
    assert case.store.profile().state["player_state"]["battle"]["TN"] == 0


def test_watermark_history_upgrades_and_keeps_replaying_its_receipts(case):
    envelope = message(case, 5, "旧式", tick=2000)
    response = case.service.save(envelope, case.store)

    def legacy(state):
        history = state["current_squad_history"]
        state["current_squad_history"] = {"schema_version": LEGACY_SCHEMA_VERSION, "last_client_time": 2000,
            "seen_keys": list(history["processed_keys"]),
            "receipts": [{**entry, "client_time": 2000} for entry in history["receipts"]]}
    overwrite(case, legacy)
    before = case.store.profile()
    assert case.service.save(envelope, case.store) == response
    assert case.store.profile() == before
    case.service.save(message(case, 6, "巻戻", tick=1000), case.store)
    history = case.store.profile().state["current_squad_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION
    assert history["processed_keys"] == [entry["request"] for entry in history["receipts"]]
    assert all("client_time" not in entry for entry in history["receipts"])
    assert case.store.profile().state["player_state"]["battle"]["TN"] == 6


@pytest.mark.parametrize("mutation", ["null", "schema", "hash", "response", "state", "team"])
def test_corrupt_history_and_same_revision_state_never_replay(case, mutation):
    envelope = message(case)
    case.service.save(envelope, case.store)
    def update(state):
        history = state["current_squad_history"]
        if mutation == "null":
            state["current_squad_history"] = None
        elif mutation == "schema":
            history["schema_version"] = True
        elif mutation == "hash":
            history["receipts"][0]["state"] = "x"
        elif mutation == "response":
            history["receipts"][0]["response"] = "<DA />"
        elif mutation == "state":
            state["sentinel"]["nested"].append("changed")
        else:
            state["player_state"]["battle"]["TN"] = 1
    overwrite(case, update)
    failure(case, envelope, "current_squad_history_invalid")


def test_changed_emblem_version_blocks_new_calls_and_cached_receipt(case):
    envelope = message(case)
    case.service.save(envelope, case.store)
    case.service.emblems._content_version = "0" * 64
    failure(case, envelope, "current_squad_dependencies_changed")
    failure(case, message(case, 1, tick=1001), "current_squad_dependencies_changed")


def test_readiness_checks_do_not_build_the_title_status(case, monkeypatch):
    monkeypatch.setattr(case.service.emblems, "status", lambda: pytest.fail("Readiness must not build the title status"))
    CurrentSquadService(case.service.bootstrap, case.service.players, case.service.emblems, case.reader)
    assert case.service.status()["ready"] is True
    case.service.save(message(case), case.store)
    assert case.store.profile().state["current_squad_history"]["receipts"]


def test_unready_titles_refuse_service_and_saves_with_the_dependency_code(case, monkeypatch):
    monkeypatch.setattr(case.service.emblems, "is_ready", lambda: False)
    status = case.service.status()
    assert status["ready"] is False and status["error"] == "current_squad_dependencies_changed"
    assert failure(case, message(case), "current_squad_dependencies_changed").status == 503
    with pytest.raises(ProtocolError) as caught:
        CurrentSquadService(case.service.bootstrap, case.service.players, case.service.emblems, case.reader)
    assert caught.value.code == "current_squad_dependencies_changed" and caught.value.status == 503


def test_cas_rereads_without_losing_other_state(case, monkeypatch):
    original = case.store.update_state
    calls = 0
    def conflict(state, revision):
        nonlocal calls
        calls += 1
        if calls == 1:
            profile = case.store.profile()
            other = deepcopy(profile.state)
            other["sentinel"]["nested"].append("concurrent")
            original(other, profile.revision)
            raise ValueError("conflict")
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    case.service.save(message(case, 19), case.store)
    assert calls == 2
    assert case.store.profile().state["sentinel"]["nested"] == ["untouched", "concurrent"]
    assert case.store.profile().state["player_state"]["battle"]["TN"] == 19


def test_exhausted_cas_does_not_partially_apply(case, monkeypatch):
    calls = []
    def conflict(state, revision):
        calls.append(revision)
        raise ValueError("conflict")
    monkeypatch.setattr(case.store, "update_state", conflict)
    failure(case, message(case, tutorial=1), "profile_changed")
    assert len(calls) == 3


@pytest.mark.parametrize("lost_condition", ["emblem", "legacy"])
def test_cas_rechecks_eligibility_after_concurrent_change(case, monkeypatch, lost_condition):
    change(case, lambda state: state["tables"].update(possessions=[{"ItemID": "get_emblem_00001", "Count": 1}]))
    original = case.store.update_state
    calls, committed = 0, None
    def conflict(state, revision):
        nonlocal calls, committed
        calls += 1
        profile = case.store.profile()
        other = deepcopy(profile.state)
        if lost_condition == "emblem":
            other["tables"]["possessions"][0]["Count"] = 0
        else:
            other["player_state"]["units"][0]["OrganizationIndex2"] = 1
        original(other, profile.revision)
        committed = case.store.profile()
        raise ValueError("conflict")
    monkeypatch.setattr(case.store, "update_state", conflict)
    with pytest.raises(ProtocolError) as error:
        case.service.save(message(case, 19, emblem=33), case.store)
    assert error.value.code == ("emblem_locked" if lost_condition == "emblem" else "current_squad_legacy_pending")
    assert calls == 1
    assert case.store.profile() == committed
    assert "current_squad_history" not in committed.state


@pytest.mark.parametrize("tutorial", [None, 1])
def test_invalid_config_does_not_commit_even_without_tutorial_change(case, tutorial):
    change(case, lambda state: state["tables"].update(config=[]))
    error = failure(case, message(case, tutorial=tutorial))
    assert error.status == 503


def test_concurrent_identical_request_commits_once(case, monkeypatch):
    original = case.store.update_state
    barrier, lock = Barrier(2), Lock()
    calls = 0
    def synchronized(state, revision):
        nonlocal calls
        with lock:
            calls += 1
            position = calls
        if position <= 2:
            barrier.wait(timeout=10)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", synchronized)
    envelope = message(case, 19, tutorial=1)
    before = case.store.profile()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(case.service.save, envelope, case.store) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0] == results[1]
    after = case.store.profile()
    assert after.revision == before.revision + 1
    assert len(after.state["current_squad_history"]["receipts"]) == 1
    assert len(target_rows(case, 19)) == 15
