import ast
import hashlib
import logging
import random
import struct
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
import lz4.frame

from aigis_ps.alxml import Column, Container, ValueType, dumps, loads
from aigis_ps.asset_service import ASSET_CHANGED, ASSET_NOT_CACHED, ASSET_NOT_FOUND
from aigis_ps.assets import asset_key
from aigis_ps.current_squad import ORDINARY_TEAM_COMMANDS, RAID_TEAM_COMMANDS, team_resources
from aigis_ps.errors import ProtocolError
import aigis_ps.player_state as module
from aigis_ps.player_state import (BARRACKS_BITS, DISPLAY_FIELD, LOCK_FLAG, MAX_PLAYER_UNITS, MAX_SQUAD_ROWS, MAX_STANDARD_TEAMS,
                                   PlayerStateService, SKILL_SWITCH_FLAG, SQUAD_FIELDS, UNIT_FIELDS, UNIT_GRANT_UNSUPPORTED,
                                   UNIT_XML_LIMITS)
from aigis_ps.protocol import (RESPONSE_TOO_LARGE, ProtocolRegistry, RequestEnvelope, decode_wire, encode_response,
                               encode_wire, prepare_request)
from aigis_ps.storage import Store
from test_image_aliases import command_table, encode


RAID_GROUP = 64
TEAM_RESOURCES = (*ORDINARY_TEAM_COMMANDS, *RAID_TEAM_COMMANDS)
RESERVED_UNITS = [{"card_id": 2, "level": 1, "count": 2}]


def schema_bytes(fields, stride):
    result = bytearray(struct.pack("<4sBBHH", b"ALRD", 1, 0, len(fields), stride))
    for name, offset, kind, parameters in fields:
        encoded = name.encode()
        result += struct.pack("<HBBBB", offset, kind, len(parameters), len(encoded), 0) + encoded + b"\0\0"
        result += b"\0" * (-len(result) % 4) + parameters
        result += b"\0" * (-len(result) % 4)
    return bytes(result)


def class_table(ids):
    schema = schema_bytes([("ClassID", 0, 1, b"\0" * 8)], 4)
    return struct.pack("<4sBBHHHI4s", b"ALTB", 1, 0x10, len(ids), 20, 20 + len(schema), 4, b"clas") + schema + struct.pack("<" + "i" * len(ids), *ids)


def all4(data):
    return struct.pack("<4sHHI", b"ALL4", 1, 0, len(data)) + lz4.frame.compress(data)


def archive_bytes(members):
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


def team_commands(raid_group=RAID_GROUP):
    def raid_view(view):
        return f'SetSystemStateParam("{view}::UseTeamCount", 3);' + "".join(
            f'SetSystemStateParam("{view}::BT{index:02d}_TeamID", {raid_group});' for index in range(3))
    teams = range(MAX_STANDARD_TEAMS)
    commands = {
        "OnCommandMenuUnitTeamEditor.atb": f'SetSystemStateParam("CMenuUnitTeamEditor::MaxTeamCount", {MAX_STANDARD_TEAMS});'
            + "".join(f'SetSystemStateParam("CMenuUnitTeamEditor::BT{team:02d}_TeamID", {team});' for team in teams),
        "OnCommandMenuUnitTeamDeparture.atb": f'MnObj_SetParam("MaxTeamCount={MAX_STANDARD_TEAMS}", '
            + ", ".join(f'"BT{team:02d}_TeamID={team}"' for team in teams) + ");",
        "OnCommandMenuRaidTeamView.atb": raid_view("CRaidTeamView"),
        "OnCommandMenuRaidSallyView.atb": raid_view("CRaidSallyView"),
    }
    return {name: command_table([(1, encode(command))]) for name, command in commands.items()}


@pytest.fixture
def assets(tmp_path, monkeypatch):
    unit_schema = b"verified player contract fixture"
    monkeypatch.setattr("aigis_ps.player_state.PLAYER_SCHEMA_SHA256", hashlib.sha256(unit_schema).hexdigest())
    result = {
        "PlayerUnitTable.aar": archive_bytes([("PlayerUnit.ard", unit_schema), ("ClassData.atb", class_table((10, 20)))]),
        "BattleParam.ard": schema_bytes([("VN", 0, 33, struct.pack("<I", 16)), ("UX", 16, 1, b"\0" * 8),
                                         ("TN", 20, 1, b"\0" * 8), ("RTN", 24, 1, b"\0" * 8)], 28),
        **team_commands(),
    }
    for name, data in result.items():
        (tmp_path / asset_key(name)).write_bytes(data)
    return tmp_path, result


@pytest.fixture
def master():
    return dumps(Container(children=(Column("CardID", "I", (1, 2)), Column("InitClassID", "I", (10, 20)),
                                     Column("ClassLV0SkillID", "I", (59, 3)), Column("ClassLV1SkillID", "I", (59, 4)))))


@pytest.fixture
def service(assets, master):
    return PlayerStateService(assets[0], lambda name: master)


@pytest.fixture
def store(tmp_path, service):
    return Store(tmp_path / "save.sqlite3", service.initialize({"bootstrap": {"progress": 0}}))


def request(number, store, rows=None, *, tutorial=None):
    account = store.profile()
    children = [Column(name, "S", (value,)) for name, value in (("UI", account.player_id), ("UK", account.user_key), ("MK", account.method_key))]
    if rows is not None:
        children += [Column(wire, kind, tuple(row[name] for row in rows)) for name, wire, kind in SQUAD_FIELDS]
    if tutorial is not None:
        children.append(Column("TL", "I", (tutorial,)))
    raw = dumps(Container(children=tuple(children)))
    return RequestEnvelope(ProtocolRegistry.load().by_registration[number], raw, ET.fromstring(raw), hashlib.sha256(raw).hexdigest())


def row(address=0, unit=0, name="第一隊", flags=0):
    return {"Address": address, "UnitIndex": unit, "Name": name, "Flags": flags}


def columns(document):
    return {child.name: child for child in document.children}


def add_configuration(store, flags=0):
    profile = store.profile()
    profile.state.update(bootstrap_schema=1, tables={"config": [{
        "UseBgm": True, "UseSe": False, "MenuTutorialFlags": flags,
        "_VolumeBgm": 80, "_VolumeSe": 60, "BattlePlaceMagnetFlag": 1,
        "BattleAcceleratorFlag": 0, "BattleDClickSkillDisableFlag": 0, "UserName": "saved name",
    }]})
    store.update_state(profile.state, profile.revision)


def test_load_squads_preserves_empty_state_and_pending_legacy_import(service, store):
    before = store.profile()
    response = loads(service.handlers()[72](request(72, store), store))
    assert [child.name for child in response.children] == ["SYSTEM", "PBP", "TGI"]
    assert all(not column.values for column in response.children[2].children)
    assert store.profile() == before


def test_load_squads_returns_all_saved_groups_after_reopening(service, store):
    rows = [row(0x00000001, 0, "First"), row(0x02000001, -1, "Second")]
    service.import_squads(request(74, store, rows), store)
    before = store.profile()
    reopened = Store(store.path)
    response = loads(service.load_squads(request(72, reopened), reopened))
    squads = columns(columns(response)["TGI"])
    assert squads["A0"].values == (0x00000001, 0x02000001)
    assert squads["A3"].values == ("First", "Second")
    assert reopened.profile() == before


@pytest.mark.parametrize("name", ["A0", "A1", "A3", "A4", "TL", "UnknownMutation"])
def test_load_squads_rejects_write_payloads(service, store, name):
    before = store.profile()
    envelope = request(72, store)
    ET.SubElement(ET.SubElement(envelope.document, name, {"T": "I"}), "V").text = "1"
    with pytest.raises(ProtocolError) as error:
        service.load_squads(envelope, store)
    assert error.value.status == 400
    assert store.profile() == before


def test_load_squads_checks_identity_and_native_response_capacity(service, store):
    before = store.profile()
    envelope = request(72, store)
    envelope.document.find("UK/V").text = "other"
    with pytest.raises(ProtocolError) as error:
        service.load_squads(envelope, store)
    assert error.value.status == 403
    envelope = request(72, store)
    envelope = replace(envelope, profile=replace(envelope.profile, response_limit=1))
    with pytest.raises(ProtocolError):
        service.load_squads(envelope, store)
    assert store.profile() == before


def test_initial_unit_uses_verified_card_and_class_fields(service):
    original = {"bootstrap": {"progress": 17}}
    result = service.initialize(original)
    assert original == {"bootstrap": {"progress": 17}}
    assert result["bootstrap"] == original["bootstrap"]
    player = result["player_state"]
    unit, = player["units"]
    assert len(unit) == 21
    assert (unit["CardID"], unit["ClassID"], unit["SettingSkillID"]) == (1, 10, 59)
    assert (unit["UniqueID"], unit["GetIndex"], unit["OrganizationIndex"], unit["SkillLevel"]) == (1, 0, -1, 1)
    assert unit["Exp"] == unit["Love"] == 0
    assert unit["CanFame"] is unit["Famed"] is False
    assert player["battle"] == {"VN": "1.0.1", "UX": 0, "TN": 0, "RTN": 64}
    assert player["legacy_squad_imported"] is False and player["squads"] == []
    assert service.status()["ready"] is True


def test_initialize_existing_state_preserves_progress_without_master(service, assets):
    existing = service.initialize({"quest_progress": [1001]})
    existing["player_state"]["units"][0]["Love"] = 77
    missing = PlayerStateService(assets[0], None)
    assert missing.initialize(existing) == existing
    assert missing.initialize(existing) is not existing
    assert missing.status()["ready"] is False


def test_missing_master_preserves_state_without_seeding(assets, tmp_path):
    service = PlayerStateService(assets[0], None)
    assert service.initialize({"bootstrap": 1}) == {"bootstrap": 1}
    assert service.status()["ready"] is False
    store = Store(tmp_path / "missing.sqlite3", {"bootstrap": 1})
    for number in (39, 41):
        with pytest.raises(ProtocolError) as error:
            service.handlers()[number](request(number, store), store)
        assert error.value.status == 503


@pytest.mark.parametrize("change", ["schema", "class", "duplicate_class"])
def test_bad_local_contract_never_seeds(assets, master, change):
    root, resources = assets
    if change == "schema":
        resources["PlayerUnitTable.aar"] = archive_bytes([("PlayerUnit.ard", b"new"), ("ClassData.atb", class_table((10, 20)))])
    else:
        ids = (20,) if change == "class" else (10, 10)
        resources["PlayerUnitTable.aar"] = archive_bytes([("PlayerUnit.ard", b"verified player contract fixture"), ("ClassData.atb", class_table(ids))])
    service = PlayerStateService(root, lambda name: master, asset_reader=resources.__getitem__)
    assert service.status()["ready"] is False
    assert service.initialize({}) == {}


def test_indexed_asset_reader_loads_archive_members_and_probes_diagnostics_lazily(assets, master, tmp_path):
    names = []
    def read(name):
        names.append(name)
        return assets[1][name]
    service = PlayerStateService(tmp_path / "absent", lambda name: master, asset_reader=read)
    assert names == ["PlayerUnitTable.aar", "BattleParam.ard"]
    status = service.status()
    assert status["ready"] is True
    assert status["unit_initialization"] == {"ready": False, "error": "player_growth_unavailable"}
    assert status["local_rules"]["raid_team"] == {"ready": True, "group": RAID_GROUP}
    assert names[2:] == ["ExperienceTable.atb", *TEAM_RESOURCES]


@pytest.mark.parametrize("raid_group", [RAID_GROUP, 65, 254])
def test_initial_raid_team_comes_from_the_raid_views_the_squad_service_validates(assets, master, raid_group):
    root, resources = assets
    resources.update(team_commands(raid_group))
    service = PlayerStateService(root, lambda name: master, asset_reader=resources.__getitem__)
    assert team_resources(resources.__getitem__)[1] == raid_group
    assert service.initialize({})["player_state"]["battle"]["RTN"] == raid_group
    assert service.status()["local_rules"]["raid_team"] == {"ready": True, "group": raid_group}


@pytest.mark.parametrize("fault", ["missing", "diverged"])
def test_unverified_raid_views_defer_only_new_players(service, assets, master, fault):
    existing = service.initialize({"quest_progress": [1001]})
    root, resources = assets
    if fault == "missing":
        del resources["OnCommandMenuRaidSallyView.atb"]
    else:
        resources["OnCommandMenuRaidSallyView.atb"] = team_commands(65)["OnCommandMenuRaidSallyView.atb"]
    broken = PlayerStateService(root, lambda name: master, asset_reader=resources.__getitem__)
    assert broken.initialize({"bootstrap": 1}) == {"bootstrap": 1}
    assert broken.initialize(existing) == existing
    status = broken.status()
    assert status["ready"] is True
    assert status["local_rules"]["raid_team"] == {"ready": False, "error": "player_raid_group_unavailable"}


def test_battle_param_answers_without_card_data_while_unit_list_still_requires_it(service, store, assets, caplog):
    missing = PlayerStateService(assets[0], None)
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    battle = columns(loads(missing.battle_param(request(39, store), store)))
    assert caplog.records == []
    assert {name: column.values for name, column in battle.items()} == {
        "VN": ("1.0.1",), "UX": (0,), "TN": (0,), "RTN": (RAID_GROUP,)}
    with pytest.raises(ProtocolError) as error:
        missing.player_units(request(41, store), store)
    assert (error.value.code, error.value.status) == ("player_master_unavailable", 503)


def test_battle_param_still_requires_the_verified_battle_schema(store, assets, master, caplog):
    root, resources = assets
    resources["BattleParam.ard"] = schema_bytes([("VN", 0, 33, struct.pack("<I", 16)), ("UX", 16, 1, b"\0" * 8)], 20)
    service = PlayerStateService(root, lambda name: master, asset_reader=resources.__getitem__)
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    with pytest.raises(ProtocolError) as error:
        service.battle_param(request(39, store), store)
    assert (error.value.code, error.value.status) == ("player_contract_unavailable", 503)
    assert [record.getMessage() for record in caplog.records] == [
        "Battle parameters 39 refused with player_contract_unavailable; the native startup loop resends it until it succeeds"]


def test_battle_param_keeps_validating_the_stored_roster_without_card_data(service, store, assets, caplog):
    profile = store.profile()
    profile.state["player_state"]["units"] *= 2
    store.update_state(profile.state, profile.revision)
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    with pytest.raises(ProtocolError) as error:
        PlayerStateService(assets[0], None).battle_param(request(39, store), store)
    assert error.value.code == "player_units_invalid"
    assert [record.getMessage() for record in caplog.records] == [
        "Battle parameters 39 refused with player_units_invalid; the native startup loop resends it until it succeeds"]


def test_nested_compressed_class_member_is_loaded(assets, master):
    root, resources = assets
    resources["PlayerUnitTable.aar"] = archive_bytes([("PlayerUnit.ard", b"verified player contract fixture"),
                                                      ("ClassData.atb", all4(class_table((10, 20))))])
    service = PlayerStateService(root, lambda name: master, asset_reader=resources.__getitem__)
    assert service.status()["ready"] is True
    assert service.initialize({})["player_state"]["units"][0]["ClassID"] == 10


def test_read_asset_unpacks_all4_resources_and_passes_plain_ones_through(assets, master):
    root, resources = assets
    table = class_table((10, 20))
    reader = {**resources, "PackedClassData.atb": all4(table), "ClassData.atb": table}
    service = PlayerStateService(root, lambda name: master, asset_reader=reader.__getitem__)
    assert service.read_asset("PackedClassData.atb") == service.read_asset("ClassData.atb") == table
    assert service.read_asset("BattleParam.ard") == resources["BattleParam.ard"]


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("schema_version", True), ("units", []), ("legacy_squad_imported", 1)])
def test_invalid_existing_state_is_not_silently_replaced(service, field, value):
    state = service.initialize({})
    state["player_state"][field] = value
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        service.initialize(state)
    assert error.value.status == 503
    assert state == before


def test_responses_use_native_flat_aliases_and_types(service, store):
    battle = columns(loads(service.battle_param(request(39, store), store)))
    assert list(battle) == ["VN", "UX", "TN", "RTN"]
    assert battle["VN"].values == ("1.0.1",) and battle["VN"].type is ValueType.STRING
    units = columns(loads(service.player_units(request(41, store), store)))
    assert list(units) == [alias for _, alias in UNIT_FIELDS]
    assert units["A1"].values == (1,) and units["A2"].values == (10,)
    assert units["AD"].values == (59,)
    assert units["B4"].type is units["B5"].type is ValueType.BOOL
    assert units["B4"].values == units["B5"].values == (False,)
    assert all(column.type is ValueType.INT for name, column in units.items() if name not in ("B4", "B5"))
    assert not {"Level", "HP", "MaxHP", "Atk", "Cost"} & units.keys()


def test_display_field_names_the_native_b0_unit_column():
    assert dict(UNIT_FIELDS)[DISPLAY_FIELD] == "B0"


def test_general_purpose_flags_keep_the_native_lock_and_skill_switch_bits():
    assert (LOCK_FLAG, SKILL_SWITCH_FLAG) == (1 << 1, 1 << 2)
    assert not (LOCK_FLAG | SKILL_SWITCH_FLAG) & BARRACKS_BITS


def test_public_responses_equal_the_served_reads(service, store):
    profile = store.profile()
    profile.state["player_state"]["squads"] = [row(address=1), row(address=2, unit=-1)]
    store.update_state(profile.state, profile.revision)
    profile = store.profile()
    player = service.validate(profile.state)
    assert service.battle_param_response(player) == service.battle_param(request(39, store), store)
    assert service.unit_response(player["units"]) == service.player_units(request(41, store), store)
    assert service.squad_response(profile, player) == service.load_squads(request(72, store), store)


def test_battle_param_response_needs_only_the_persisted_battle_row(store, assets):
    missing = PlayerStateService(assets[0], None)
    battle = loads(missing.battle_param_response(missing.validate(store.profile().state, references=False)))
    assert [(column.name, column.type, column.values) for column in battle.children] == [
        ("VN", ValueType.STRING, ("1.0.1",)), ("UX", ValueType.INT, (0,)), ("TN", ValueType.INT, (0,)),
        ("RTN", ValueType.INT, (RAID_GROUP,))]


@pytest.mark.parametrize("references", [True, False])
def test_validate_player_is_the_check_validate_applies_to_the_saved_player(service, store, references):
    state = store.profile().state
    player = service.validate_player(state["player_state"], references=references)
    assert player is state["player_state"]
    assert service.validate(state, references=references) is player


def test_validate_player_checks_card_references_only_when_asked(service, assets):
    player = service.initialize({})["player_state"]
    player["units"][0]["CardID"] = 99
    missing = PlayerStateService(assets[0], None)
    for players, code in ((service, "player_reference_invalid"), (missing, "player_master_unavailable")):
        assert players.validate_player(player, references=False) is player
        with pytest.raises(ProtocolError) as error:
            players.validate_player(player)
        assert (error.value.code, error.value.status) == (code, 503)


@pytest.mark.parametrize("saved", [[], None, "player_state"], ids=["list", "none", "text"])
@pytest.mark.parametrize("number", [39, 41, 72])
def test_saved_reads_refuse_a_state_that_is_not_an_object(service, store, saved, number):
    store.update_state(saved, store.profile().revision)
    with pytest.raises(ProtocolError) as error:
        service.handlers()[number](request(number, store), store)
    assert (error.value.code, error.value.status) == ("player_state_schema", 503)
    assert store.profile().state == saved


@pytest.mark.parametrize("state,expected", [
    ({}, []), ({"quest_state": {"active": None}}, []),
    ({"quest_state": {"active": {"unit_reservations": RESERVED_UNITS}}}, RESERVED_UNITS)], ids=["none", "idle", "active"])
def test_quest_reservations_list_the_active_battle_unit_rewards(service, state, expected):
    assert service.quest_reservations(state) == expected


@pytest.mark.parametrize("journal,code,status", [
    ({"active": {"quest_id": 1}}, "quest_reservation_unavailable", 409),
    ([], "quest_state_invalid", 503),
    ({"active": []}, "quest_state_invalid", 503),
    ({"active": {"unit_reservations": [{**RESERVED_UNITS[0], "card_id": 1}]}}, "quest_state_invalid", 503),
    ({"active": {"unit_reservations": [{**RESERVED_UNITS[0], "count": MAX_PLAYER_UNITS + 1}]}}, "quest_state_invalid", 503),
], ids=["legacy", "journal", "active", "prince", "over_limit"])
def test_quest_reservations_refuse_legacy_or_invalid_battles(service, journal, code, status):
    with pytest.raises(ProtocolError) as caught:
        service.quest_reservations({"quest_state": journal})
    assert (caught.value.code, caught.value.status) == (code, status)


def test_maximum_roster_uses_bounded_full_column_serialization(service, store):
    account = store.profile()
    template = account.state["player_state"]["units"][0]
    account.state["player_state"]["units"] = [{**template, "GetIndex": i, "UniqueID": i + 1} for i in range(MAX_PLAYER_UNITS)]
    account.state["player_state"]["battle"]["UX"] = MAX_PLAYER_UNITS - 1
    store.update_state(account.state, account.revision)
    envelope = request(41, store)
    response = service.player_units(envelope, store)
    document = loads(response, limits=UNIT_XML_LIMITS)
    assert len(document.children) == 21
    assert all(len(column.values) == MAX_PLAYER_UNITS for column in document.children)
    assert columns(document)["A7"].values == tuple(range(MAX_PLAYER_UNITS))
    assert len(encode_response(envelope.profile, response)[0]) <= envelope.profile.response_limit
    assert store.profile().revision == 1


def test_high_entropy_roster_exceeding_native_buffer_is_rejected_without_truncation(service, store):
    account = store.profile()
    template = account.state["player_state"]["units"][0]
    rng = random.Random(4174)
    rows = [{**template, "GetIndex": i, "UniqueID": i + 1,
             **{name: rng.randrange(1, 2 ** 31) for name in ("Exp", "Love", "SkillExperience", "CostExperience",
                                                           "WatchLoveEv", "CostModMixValue", "GeneralPurposeFlags")}}
            for i in range(MAX_PLAYER_UNITS)]
    account.state["player_state"]["units"] = rows
    account.state["player_state"]["battle"]["UX"] = MAX_PLAYER_UNITS - 1
    store.update_state(account.state, account.revision)
    with pytest.raises(ProtocolError) as error:
        service.player_units(request(41, store), store)
    assert error.value.code == RESPONSE_TOO_LARGE
    assert store.profile().state["player_state"]["units"] == rows
    assert store.profile().revision == 1


@pytest.mark.parametrize("malformed", ["plain", "int", "duplicate", "parent_text", "value_tail", "field_tail", "extra_value", "nested"])
@pytest.mark.parametrize("number", [39, 41, 73, 74])
def test_identity_requires_one_typed_string_without_mixed_text(service, store, malformed, number):
    envelope = request(number, store, [row()] if number in (73, 74) else None)
    node = envelope.document.find("UI")
    if malformed == "plain":
        node.clear()
        node.text = "1"
    elif malformed == "int":
        node.set("T", "I")
    elif malformed == "duplicate":
        envelope.document.append(deepcopy(node))
    elif malformed == "parent_text":
        node.text = "bad"
    elif malformed == "value_tail":
        node[0].tail = "bad"
    elif malformed == "field_tail":
        node.tail = "bad"
    elif malformed == "extra_value":
        ET.SubElement(node, "V").text = "1"
    else:
        ET.SubElement(node[0], "V").text = "1"
    with pytest.raises(ProtocolError) as error:
        service.handlers()[number](envelope, store)
    assert error.value.status == 400
    assert store.profile().revision == 0


@pytest.mark.parametrize("name", ["UI", "UK", "MK"])
@pytest.mark.parametrize("number", [41, 73, 74])
def test_account_authentication_applies_to_new_operations(service, store, name, number):
    envelope = request(number, store, [row()] if number in (73, 74) else None)
    envelope.document.find(name)[0].text = "wrong"
    with pytest.raises(ProtocolError) as error:
        service.handlers()[number](envelope, store)
    assert error.value.status == 403


def test_authenticate_returns_the_stored_profile_for_the_local_account(service, store):
    before = store.profile()
    assert service.authenticate(request(41, store), store) == before
    assert store.profile() == before


@pytest.mark.parametrize("name,code", [("UI", "invalid_identity"), ("UK", "invalid_identity"), ("MK", "invalid_method_key")])
def test_authenticate_refuses_a_foreign_account_or_method_key(service, store, name, code):
    envelope = request(41, store)
    envelope.document.find(name)[0].text = "wrong"
    with pytest.raises(ProtocolError) as error:
        service.authenticate(envelope, store)
    assert (error.value.code, error.value.status) == (code, 403)
    assert store.profile().revision == 0


@pytest.mark.parametrize("target,value", [("VN", "1.0.0"), ("UX", -1), ("TN", 20), ("RTN", 256)])
def test_invalid_battle_contract_returns_unavailable(service, store, target, value):
    profile = store.profile()
    profile.state["player_state"]["battle"][target] = value
    store.update_state(profile.state, profile.revision)
    with pytest.raises(ProtocolError) as error:
        service.battle_param(request(39, store), store)
    assert error.value.code == "battle_param_invalid"


def test_duplicate_unit_identity_and_unknown_class_are_rejected(service, store):
    profile = store.profile()
    profile.state["player_state"]["units"][0]["ClassID"] = 999
    store.update_state(profile.state, profile.revision)
    with pytest.raises(ProtocolError) as error:
        service.player_units(request(41, store), store)
    assert error.value.code == "player_reference_invalid"
    state = service.initialize({})
    state["player_state"]["units"] *= 2
    with pytest.raises(ProtocolError) as error:
        service.initialize(state)
    assert error.value.code == "player_units_invalid"


def test_squad_import_persists_native_getindex_rows_and_response(service, store):
    rows = [row(), row(1, -1, "第一隊", 7), row(1 << 24, 0, "第二隊")]
    envelope = request(74, store, rows)
    raw = service.import_squads(envelope, store)
    response = columns(loads(raw))
    assert list(response) == ["SYSTEM", "PBP", "TGI"]
    assert response["SYSTEM"].children[0].values == ("ExchangeKey", "PaymentTransaction")
    assert response["PBP"].children == (Column("A1", "S", ("UX",)), Column("A2", "I", (0,)))
    tgi = columns(response["TGI"])
    assert tgi["A0"].values == (0, 1, 1 << 24) and tgi["A1"].values == (0, -1, 0)
    assert tgi["A3"].values == ("第一隊", "第一隊", "第二隊")
    wire, _ = encode_response(envelope.profile, raw)
    assert decode_wire(wire, envelope.profile.response_key) == raw
    persisted = Store(store.path).profile()
    assert persisted.revision == 1
    assert persisted.state["bootstrap"] == {"progress": 0}
    assert persisted.state["player_state"]["squads"] == rows
    assert persisted.state["player_state"]["legacy_squad_imported"] is True


def test_repeated_legacy_import_does_not_overwrite_or_increment_revision(service, store):
    first = service.import_squads(request(74, store, [row()]), store)
    repeated = service.import_squads(request(74, store, [row(1, -1, "replacement")]), store)
    assert repeated == first
    assert store.profile().revision == 1
    assert store.profile().state["player_state"]["squads"] == [row()]


def add_units(store, organization):
    profile = store.profile()
    state = deepcopy(profile.state)
    player = state["player_state"]
    template = player["units"][0]
    for index, value in enumerate(organization, start=1):
        player["units"].append({**template, "GetIndex": index, "UniqueID": index + 1, "OrganizationIndex2": value})
    player["battle"]["UX"] = len(organization)
    store.update_state(state, profile.revision)
    return player["units"]


def native_nibble(value, position):
    mask = 0xF << (4 * position)
    mask -= (mask & 0x80000000) << 1
    return (value & mask) >> (4 * position)


def native_legacy_rows(units, name="", flags=0):
    groups = {}
    for unit in units:
        for group in range(8):
            slot = native_nibble(unit["OrganizationIndex2"], group)
            if slot > 0:
                groups.setdefault(group, {})[slot] = unit["GetIndex"]
    rows = []
    for group, members in sorted(groups.items()):
        rows += [row((group << 24) | slot, unit, name, flags) for slot, unit in sorted(members.items())]
        rows += [row((group << 24) | slot, -1, name, flags) for slot in range(len(members), 15)]
    return rows


def collapsed(rows):
    members = {value["Address"] for value in rows if value["UnitIndex"] != -1}
    return [value for value in rows if value["UnitIndex"] != -1 or value["Address"] not in members]


def test_native_legacy_padding_overlap_collapses_into_member_rows(service, store):
    add_units(store, (1, 2, 3))
    rows = native_legacy_rows(store.profile().state["player_state"]["units"], "第一隊")
    assert [value["Address"] for value in rows].count(3) == 2
    raw = service.import_squads(request(74, store, rows), store)
    saved = store.profile().state["player_state"]
    expected = [row(1, 1, "第一隊"), row(2, 2, "第一隊"), row(3, 3, "第一隊")] + [
        row(slot, -1, "第一隊") for slot in range(4, 15)]
    assert saved["squads"] == expected and saved["legacy_squad_imported"] is True
    tgi = columns(columns(loads(raw))["TGI"])
    assert tgi["A0"].values == tuple(value["Address"] for value in expected)
    assert tgi["A1"].values == tuple(value["UnitIndex"] for value in expected)


@pytest.mark.parametrize("seed", range(8))
def test_native_legacy_rows_from_random_organization_index2_layouts_import(service, store, seed):
    generator = random.Random(seed)
    units = add_units(store, [generator.randrange(-0x80000000, 0x80000000) for _ in range(6)])
    rows = native_legacy_rows(units)
    assert len({value["Address"] for value in rows}) < len(rows)
    service.import_squads(request(74, store, rows), store)
    saved = store.profile().state["player_state"]["squads"]
    assert saved == collapsed(rows)
    assert len({value["Address"] for value in saved}) == len(saved)
    assert {value["UnitIndex"] for value in saved} - {-1} == {value["UnitIndex"] for value in rows} - {-1}


@pytest.mark.parametrize("rows", [
    [row(1, 1), row(1, 2)], [row(1, -1), row(1, 1)], [row(1, -1), row(1, -1)], [row(1, 1), row(1, -1), row(1, -1)],
    [row(1, 1, "第一隊"), row(1, -1, "別名")], [row(1, 1, flags=1), row(1, -1, flags=2)],
])
def test_legacy_import_rejects_duplicates_the_native_padding_cannot_produce(service, store, rows):
    add_units(store, (1, 2))
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.import_squads(request(74, store, rows), store)
    assert error.value.status == 400 and error.value.code == "squad_rows_invalid"
    assert store.profile() == before


def test_normal_squad_save_keeps_rejecting_duplicate_addresses(service, store):
    add_units(store, (1,))
    with pytest.raises(ProtocolError) as error:
        service.save_squads(request(73, store, [row(1, 1), row(1, -1)]), store)
    assert error.value.status == 400 and error.value.code == "squad_rows_invalid"


def consume(store, index):
    profile = store.profile()
    state = deepcopy(profile.state)
    player = state["player_state"]
    player["units"] = [unit for unit in player["units"] if unit["GetIndex"] != index]
    player["squads"] = [{**value, "UnitIndex": -1} if value["UnitIndex"] == index else value for value in player["squads"]]
    store.update_state(state, profile.revision)


def test_squad_save_empties_slots_of_identities_already_consumed(service, store, caplog):
    add_units(store, (0, 0, 0))
    rows = [row(1, 1), row(2, 2), row(3, 3), row(1 << 24 | 1, 2, "第二隊")]
    service.save_squads(request(73, store, rows), store)
    consume(store, 2)
    before = store.profile()
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        raw = service.save_squads(request(73, store, rows), store)
    expected = [row(1, 1), row(2, -1), row(3, 3), row(1 << 24 | 1, -1, "第二隊")]
    assert store.profile().state["player_state"]["squads"] == expected
    assert store.profile().revision == before.revision
    tgi = columns(columns(loads(raw))["TGI"])
    assert tgi["A1"].values == (1, -1, 3, -1)
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        "Squad save 73 emptied slots of consumed units [2] that the native squad storage still listed; "
        "the reply snapshot clears them"]
    renamed = [{**value, "Name": "改名"} for value in rows[:3]]
    service.save_squads(request(73, store, renamed), store)
    assert [value["UnitIndex"] for value in store.profile().state["player_state"]["squads"]] == [-1, 1, -1, 3]


def test_squad_save_empties_the_slot_of_the_most_recently_issued_identity(service, store):
    add_units(store, (0, 0, 0))
    consume(store, 3)
    service.save_squads(request(73, store, [row(1, 1), row(2, 3)]), store)
    assert [value["UnitIndex"] for value in store.profile().state["player_state"]["squads"]] == [1, -1]


@pytest.mark.parametrize("index", [4, 2 ** 31 - 1, -2])
def test_squad_save_still_rejects_identities_that_were_never_issued(service, store, index):
    add_units(store, (0, 0, 0))
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.save_squads(request(73, store, [row(1, 1), row(2, index)]), store)
    assert (error.value.code, error.value.status) == ("squad_unit_missing", 400)
    assert store.profile() == before


def test_legacy_import_stays_strict_about_absent_members(service, store):
    add_units(store, (0, 0))
    consume(store, 2)
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.import_squads(request(74, store, [row(1, 1), row(2, 2)]), store)
    assert (error.value.code, error.value.status) == ("squad_unit_missing", 400)
    assert store.profile() == before


@pytest.mark.parametrize("rows", [[row(unit=1)], [row(16)], [row(-1)], [row(), row()], [row(name="x" * 129)]])
@pytest.mark.parametrize("number", [73, 74])
def test_invalid_squad_request_cannot_modify_save(service, store, rows, number):
    with pytest.raises(ProtocolError) as error:
        service.handlers()[number](request(number, store, rows), store)
    assert error.value.status == 400
    assert store.profile().revision == 0
    assert store.profile().state["player_state"]["squads"] == []


@pytest.mark.parametrize("malformed", ["missing", "mismatch", "plain", "wrong_type", "mixed_text", "empty", "duplicate", "nested", "field_tail"])
@pytest.mark.parametrize("number", [73, 74])
def test_squad_request_shape_is_validated(service, store, malformed, number):
    envelope = request(number, store, [] if malformed == "empty" else [row()])
    node = envelope.document.find("A0")
    if malformed == "missing":
        envelope.document.remove(node)
    elif malformed == "mismatch":
        ET.SubElement(node, "V").text = "1"
    elif malformed == "plain":
        node.clear()
        node.text = "0"
    elif malformed == "wrong_type":
        node.set("T", "S")
    elif malformed == "mixed_text":
        node.text = "bad"
    elif malformed == "duplicate":
        envelope.document.append(deepcopy(node))
    elif malformed == "nested":
        ET.SubElement(node[0], "V").text = "1"
    elif malformed == "field_tail":
        node.tail = "bad"
    with pytest.raises(ProtocolError) as error:
        service.handlers()[number](envelope, store)
    assert error.value.code == "squad_request_invalid"
    assert store.profile().revision == 0


def test_squad_import_retries_cas_without_losing_concurrent_changes(service, store, monkeypatch):
    original = store.update_state
    calls = []
    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            current = store.profile()
            current.state["bootstrap"]["progress"] = 55
            original(current.state, current.revision)
        return original(state, revision)
    monkeypatch.setattr(store, "update_state", update)
    service.import_squads(request(74, store, [row()]), store)
    assert calls == [0, 1]
    assert store.profile().revision == 2
    assert store.profile().state["bootstrap"]["progress"] == 55


@pytest.mark.parametrize("number", [73, 74])
def test_exhausted_cas_does_not_report_squad_success(service, store, monkeypatch, number):
    def update(state, revision):
        raise ValueError("Profile revision changed")
    monkeypatch.setattr(store, "update_state", update)
    with pytest.raises(ProtocolError) as error:
        service.handlers()[number](request(number, store, [row()]), store)
    assert error.value.status == 409
    assert store.profile().revision == 0


@pytest.mark.parametrize("number", [73, 74])
def test_native_response_limit_is_checked_before_commit(service, store, number):
    envelope = request(number, store, [row()])
    envelope = replace(envelope, profile=replace(envelope.profile, response_limit=8))
    with pytest.raises(ProtocolError) as error:
        service.handlers()[number](envelope, store)
    assert error.value.code == RESPONSE_TOO_LARGE
    assert store.profile().revision == 0


def test_squad_save_replaces_whole_submitted_groups_and_returns_full_state(service, store):
    profile = store.profile()
    player = profile.state["player_state"]
    player["units"].append({**player["units"][0], "GetIndex": 9, "UniqueID": 10})
    player["battle"]["UX"] = 17
    retained = [row(1 << 24, 0, "untouched"), row(-0x80000000, -1, "signed address")]
    player["squads"] = [row(), row(1, 9), row((2 << 16) | 15, 9), *retained]
    store.update_state(profile.state, profile.revision)
    incoming = [row(1, 9, "edited", 7), row(2, -1, "edited", 7)]
    envelope = request(73, store, incoming)
    raw = service.handlers()[73](envelope, store)
    response = columns(loads(raw))
    assert list(response) == ["SYSTEM", "PBP", "TGI"]
    assert response["SYSTEM"].children == (Column("A1", "S", ("ExchangeKey", "PaymentTransaction")),
                                             Column("A2", "S", (profile.method_key, "0")))
    assert response["PBP"].children == (Column("A1", "S", ("UX",)), Column("A2", "I", (17,)))
    merged = retained + incoming
    for name, wire, kind in SQUAD_FIELDS:
        assert columns(response["TGI"])[wire] == Column(wire, kind, tuple(item[name] for item in merged))
    persisted = Store(store.path).profile()
    assert persisted.state["player_state"]["squads"] == merged
    assert persisted.state["player_state"]["legacy_squad_imported"] is True
    assert persisted.state["player_state"]["units"] == player["units"]
    assert persisted.state["player_state"]["battle"] == player["battle"]
    assert persisted.state["bootstrap"] == profile.state["bootstrap"]
    wire, _ = encode_response(envelope.profile, raw)
    assert decode_wire(wire, envelope.profile.response_key) == raw
    assert service.save_squads(envelope, store) == raw
    assert store.profile().revision == persisted.revision


def test_squad_save_all_absent_columns_is_noop_and_keeps_legacy_import_open(service, store):
    before = store.profile()
    response = columns(loads(service.save_squads(request(73, store), store)))
    assert all(not column.values for column in response["TGI"].children)
    assert store.profile() == before
    service.import_squads(request(74, store, [row()]), store)
    before = store.profile()
    service.save_squads(request(73, store), store)
    assert store.profile() == before


def test_normal_squad_save_takes_precedence_over_late_legacy_import(service, store):
    incoming = [row(1, 0), row(2, -1)]
    first = service.save_squads(request(73, store, incoming), store)
    before = store.profile()
    assert service.import_squads(request(74, store, [row(1 << 24, -1, "legacy")]), store) == first
    assert store.profile() == before


@pytest.mark.parametrize("flags", [0, 1, 0x4000, -0x80000000, -1])
def test_tutorial_only_save_preserves_all_other_config_bits_and_fields(service, store, flags):
    add_configuration(store, flags)
    before = store.profile()
    envelope = request(73, store, tutorial=1)
    response = columns(loads(service.save_squads(envelope, store)))
    expected = deepcopy(before.state)
    expected["tables"]["config"][0]["MenuTutorialFlags"] = flags | 6
    after = Store(store.path).profile()
    assert after.state == expected
    assert after.revision == before.revision + (flags | 6 != flags)
    assert list(response) == ["SYSTEM", "PBP", "TGI"]
    service.save_squads(envelope, store)
    assert store.profile() == after
    assert after.state["player_state"]["legacy_squad_imported"] is False


@pytest.mark.parametrize("malformed", ["plain", "wrong_type", "duplicate", "empty", "extra_value", "nested", "mixed_text", "field_tail", "zero", "two"])
def test_malformed_tutorial_field_cannot_write_squads(service, store, malformed):
    envelope = request(73, store, [row()], tutorial=1)
    node = envelope.document.find("TL")
    if malformed == "plain":
        node.clear()
        node.text = "1"
    elif malformed == "wrong_type":
        node.set("T", "B")
    elif malformed == "duplicate":
        envelope.document.append(deepcopy(node))
    elif malformed == "empty":
        node.remove(node[0])
    elif malformed == "extra_value":
        ET.SubElement(node, "V").text = "1"
    elif malformed == "nested":
        ET.SubElement(node[0], "V").text = "1"
    elif malformed == "mixed_text":
        node.text = "bad"
    elif malformed == "field_tail":
        node.tail = "bad"
    else:
        node[0].text = "0" if malformed == "zero" else "2"
    with pytest.raises(ProtocolError) as error:
        service.save_squads(envelope, store)
    assert error.value.code == "squad_tutorial_invalid"
    assert store.profile().revision == 0


@pytest.mark.parametrize("config", ["missing", "bad_flags", "missing_field"])
def test_tutorial_and_squad_changes_fail_atomically_on_invalid_configuration(service, store, config):
    if config != "missing":
        add_configuration(store)
        profile = store.profile()
        if config == "bad_flags":
            profile.state["tables"]["config"][0]["MenuTutorialFlags"] = True
        else:
            del profile.state["tables"]["config"][0]["UseBgm"]
        store.update_state(profile.state, profile.revision)
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.save_squads(request(73, store, [row()], tutorial=1), store)
    assert error.value.code == "invalid_configuration_state"
    assert error.value.status == 503
    assert store.profile() == before


def test_squad_save_retries_merge_and_tutorial_bits_from_latest_revision(service, store, monkeypatch):
    add_configuration(store, 1)
    original = store.update_state
    calls = []
    concurrent = row(1 << 24, -1, "concurrent")
    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            current = store.profile()
            current.state["tables"]["config"][0]["MenuTutorialFlags"] |= 0x100
            current.state["player_state"]["squads"] = [concurrent]
            current.state["bootstrap"]["progress"] = 55
            original(current.state, current.revision)
        return original(state, revision)
    monkeypatch.setattr(store, "update_state", update)
    incoming = [row(1, -1)]
    raw = service.save_squads(request(73, store, incoming, tutorial=1), store)
    assert calls == [1, 2]
    state = store.profile().state
    assert state["player_state"]["squads"] == [concurrent, *incoming]
    assert state["tables"]["config"][0]["MenuTutorialFlags"] == 0x107
    assert state["bootstrap"]["progress"] == 55
    assert columns(columns(loads(raw))["TGI"])["A0"].values == (1 << 24, 1)


def test_squad_response_limit_prevents_tutorial_write(service, store):
    add_configuration(store)
    before = store.profile()
    envelope = request(73, store, [row()], tutorial=1)
    envelope = replace(envelope, profile=replace(envelope.profile, response_limit=8))
    with pytest.raises(ProtocolError) as error:
        service.save_squads(envelope, store)
    assert error.value.code == RESPONSE_TOO_LARGE
    assert store.profile() == before


def test_squad_save_validates_merged_row_limit_before_write(service, store):
    profile = store.profile()
    profile.state["player_state"]["squads"] = [row(((index // 256) << 24) | (((index // 16) % 16) << 16) | (index % 16), -1)
                                                 for index in range(MAX_SQUAD_ROWS)]
    store.update_state(profile.state, profile.revision)
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.save_squads(request(73, store, [row(16 << 24, -1)]), store)
    assert error.value.code == "squad_rows_invalid"
    assert store.profile() == before


@pytest.mark.parametrize("incoming", [[row(0), row(1, name="different")], [row(0), row(1, flags=1)]])
def test_squad_save_rejects_conflicting_metadata_within_one_group(service, store, incoming):
    with pytest.raises(ProtocolError) as error:
        service.save_squads(request(73, store, incoming), store)
    assert error.value.code == "squad_metadata_invalid"
    assert store.profile().revision == 0


@pytest.mark.parametrize("number", [73, 74])
@pytest.mark.parametrize("field", ["A2", "UnknownMutation"])
@pytest.mark.parametrize("with_squads", [False, True])
def test_squad_requests_reject_unknown_fields_before_noop_or_write(service, store, number, field, with_squads):
    envelope = request(number, store, [row()] if with_squads else None)
    node = ET.SubElement(envelope.document, field, {"T": "I"})
    ET.SubElement(node, "V").text = "99"
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.handlers()[number](envelope, store)
    assert error.value.code == "squad_request_invalid"
    assert store.profile() == before


@pytest.mark.parametrize("number", [73, 74])
@pytest.mark.parametrize("malformed", ["root_name", "root_attribute", "root_text", "root_tail", "common_tail"])
def test_squad_requests_reject_invalid_root_and_mixed_text(service, store, number, malformed):
    envelope = request(number, store, [row()])
    if malformed == "root_name":
        envelope.document.tag = "Other"
    elif malformed == "root_attribute":
        envelope.document.set("T", "I")
    elif malformed == "root_text":
        envelope.document.text = "UnknownMutation=99"
    elif malformed == "root_tail":
        envelope.document.tail = "UnknownMutation=99"
    else:
        node = ET.SubElement(envelope.document, "PFN", {"T": "S"})
        ET.SubElement(node, "V").text = "dmm-r18"
        node.tail = "UnknownMutation=99"
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.handlers()[number](envelope, store)
    assert error.value.code == "squad_request_invalid"
    assert store.profile() == before


@pytest.mark.parametrize("field", ["UnknownMutation", "TL"])
@pytest.mark.parametrize("imported", [False, True])
def test_legacy_squad_import_rejects_unsupported_mutations_even_after_import(service, store, field, imported):
    if imported:
        service.import_squads(request(74, store, [row()]), store)
    envelope = request(74, store, [row(1, -1)])
    node = ET.SubElement(envelope.document, field, {"T": "I"})
    ET.SubElement(node, "V").text = "1"
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.import_squads(envelope, store)
    assert error.value.code == "squad_request_invalid"
    assert store.profile() == before


@pytest.mark.parametrize("number,with_squads", [(73, False), (73, True), (74, True)])
def test_squad_requests_accept_verified_common_and_transport_fields(service, store, number, with_squads):
    envelope = request(number, store, [row()] if with_squads else None)
    for name, kind, value in (("CT", "I", "0"), ("PFN", "S", "dmm-r18"), ("PlatformCode", "I", "1"),
                              ("GPVI", "S", "unkown"), ("GPOT", "S", "unkown")):
        node = ET.SubElement(envelope.document, name, {"T": kind})
        ET.SubElement(node, "V").text = value
        node.tail = "\n"
    envelope.document.text = "\n"
    prepared = prepare_request(ET.tostring(envelope.document), envelope.profile.st)
    decoded = ProtocolRegistry.load().decode(envelope.profile.route, encode_wire(prepared, envelope.profile.request_key))
    response = service.handlers()[number](decoded, store)
    assert list(columns(loads(response))) == ["SYSTEM", "PBP", "TGI"]
    assert store.profile().revision == int(with_squads)


def numeric_table(names, values):
    stride = len(names) * 4
    schema = schema_bytes([(name, index * 4, 1, b"\0" * 8) for index, name in enumerate(names)], stride)
    records = b"".join(struct.pack("<" + "i" * len(names), *row) for row in values)
    return struct.pack("<4sBBHHHI4s", b"ALTB", 1, 0x10, len(values), 20, 20 + len(schema), stride, b"grow") + schema + records


def growth_rarity_table(rows):
    return numeric_table(("ConfigID", "RequireExpRatio", "LevelCap00", "LevelCap02", "EvolutionConfig"),
                         [(*row, 8, 1) for row in rows])


@pytest.fixture
def growth_setup(assets):
    root, resources = assets
    resources["PlayerUnitTable.aar"] = archive_bytes([
        ("PlayerUnit.ard", b"verified player contract fixture"),
        ("ClassData.atb", numeric_table(("ClassID", "MaxLevel", "JobChange"), [(10, 99, 0), (20, 8, 0), (30, 1, 0)])),
    ])
    resources["ExperienceTable.atb"] = numeric_table(("Exp", "Level"), [(exp, i + 1) for i, exp in enumerate((0, 32, 65, 100, 135, 172, 210, 250))])
    resources["cfgRarity.atb"] = growth_rarity_table(
        [(i + 1, 100 + i * 10, min(i + 4, 8)) for i in range(6)] + [(1004, 0, 0)])
    resources["PlayerBloodType.atb"] = numeric_table(("_ResID", "_TypeID", "_SystemTextID"), [(0, 0, 0), (163175264, 3, 0)])
    master = dumps(Container(children=(Column("CardID", "I", tuple(range(1, 9))),
        Column("InitClassID", "I", (10, 20, 20, 20, 20, 20, 20, 30)),
        Column("ClassLV0SkillID", "I", (59, 3, 4, 5, 6, 7, 8, 0)),
        Column("ClassLV1SkillID", "I", (59, 13, 14, 15, 16, 17, 18, 0)),
        Column("Rare", "I", (5, 0, 1, 2, 3, 4, 5, 5)),
        Column("Blood", "I", (0,) * 8), Column("ExcludeFameDisplayFirstClass", "I", (0,) * 8))))
    return root, resources, master


@pytest.fixture
def growth_service(growth_setup):
    root, resources, master = growth_setup
    return PlayerStateService(root, lambda name: master, asset_reader=resources.__getitem__)


def recorded_hashes(monkeypatch):
    hashed, digest = [], hashlib.sha256
    monkeypatch.setattr("aigis_ps.player_state.hashlib", SimpleNamespace(sha256=lambda data: hashed.append(data) or digest(data)))
    return hashed


@pytest.mark.parametrize("card_id,expected", [(2, 65), (3, 72), (4, 78), (5, 85), (6, 91), (7, 98)])
def test_grant_experience_uses_native_rarity_ratio_and_rounding(growth_service, card_id, expected):
    assert growth_service.experience_for_level(card_id, 3) == expected


@pytest.mark.parametrize("card_id,level,expected", [(2, 0, 0), (2, 1, 0), (2, 4, 100), (2, 1000, 100),
                                                       (7, 2 ** 31 - 1, 375), (8, 0, 0), (8, 1000, 0)])
def test_grant_experience_clamps_to_class_and_rarity_caps(growth_service, card_id, level, expected):
    assert growth_service.experience_for_level(card_id, level) == expected


def test_credit_units_preserves_existing_state_and_returns_independent_new_rows(growth_service):
    original = growth_service.initialize({"bootstrap": {"progress": 23}, "quest_rewards": {"first_clear": True}})
    original["player_state"]["units"][0].update(Exp=888, Love=777, GeneralPurposeFlags=6)
    before = deepcopy(original)
    result, added = growth_service.credit_units(original, [{"card_id": 3, "level": 3, "count": 2},
                                                          {"card_id": 8, "level": 0, "count": 1}])
    assert original == before
    assert result["bootstrap"] == before["bootstrap"] and result["quest_rewards"] == before["quest_rewards"]
    assert result["player_state"]["units"][0] == before["player_state"]["units"][0]
    assert [unit["GetIndex"] for unit in added] == [1, 2, 3]
    assert [unit["UniqueID"] for unit in added] == [2, 3, 4]
    assert [unit["Exp"] for unit in added] == [72, 72, 0]
    assert [unit["ClassID"] for unit in added] == [20, 20, 30]
    assert [unit["SettingSkillID"] for unit in added] == [4, 4, 0]
    for unit in added:
        assert set(unit) == {name for name, _ in UNIT_FIELDS}
        assert unit["SkillLevel"] == unit["GeneralPurposeFlags"] == 1
        assert unit["OrganizationIndex"] == -1 and unit["OrganizationIndex2"] == 0
        assert unit["CanFame"] is unit["Famed"] is False
    assert result["player_state"]["battle"]["UX"] == 3
    assert result["player_state"]["last_unique_id"] == 4
    added[0]["Love"] = 99
    assert result["player_state"]["units"][1]["Love"] == 0


def test_credit_units_high_water_mark_survives_removal_and_store_reopen(growth_service, tmp_path):
    state = growth_service.initialize({})
    state["player_state"]["battle"]["UX"] = 19
    state["player_state"]["units"][0]["UniqueID"] = 40
    grants = [{"card_id": 2, "level": 1, "count": 2}]
    updated, added = growth_service.credit_units(state, grants)
    assert growth_service.credit_units(state, grants) == (updated, added)
    assert [unit["UniqueID"] for unit in added] == [41, 42]
    assert [unit["GetIndex"] for unit in added] == [20, 21]
    saved = Store(tmp_path / "grant.sqlite3", updated)
    profile = saved.profile()
    profile.state["player_state"]["units"] = profile.state["player_state"]["units"][:1]
    saved.update_state(profile.state, profile.revision)
    reopened = Store(saved.path)
    before = reopened.profile()
    updated, added = growth_service.credit_units(before.state, [{"card_id": 2, "level": 1, "count": 1}])
    assert added[0]["UniqueID"] == 43 and added[0]["GetIndex"] == 22
    assert reopened.profile() == before
    reopened.update_state(updated, before.revision)
    assert Store(saved.path).profile().state["player_state"]["last_unique_id"] == 43


def test_empty_unit_grants_preserve_counters_without_loading_growth_resources(service):
    state = service.initialize({"other": [1]})
    result, added = service.credit_units(state, [])
    assert result == state and added == []
    assert "last_unique_id" not in result["player_state"]
    assert service._growth_cache is None
    result["other"].append(2)
    assert state["other"] == [1]


@pytest.mark.parametrize("grants", [None, {}, (), [{}], [{"card_id": 2, "level": 1}],
    [{"card_id": 2, "level": 1, "count": 1, "extra": 0}],
    [{"card_id": 1, "level": 1, "count": 1}], [{"card_id": True, "level": 1, "count": 1}],
    [{"card_id": 2, "level": True, "count": 1}], [{"card_id": 2, "level": -1, "count": 1}],
    [{"card_id": 2, "level": 2 ** 31, "count": 1}], [{"card_id": 2, "level": 1, "count": True}],
    [{"card_id": 2, "level": 1, "count": 0}], [{"card_id": 2, "level": 1, "count": -1}],
    [{"card_id": 2, "level": 1, "count": 2 ** 31}]])
def test_invalid_unit_grants_do_not_modify_state(growth_service, grants):
    state = growth_service.initialize({})
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        growth_service.credit_units(state, grants)
    assert error.value.code == "unit_grant_invalid"
    assert state == before


def test_unknown_unit_in_later_grant_fails_whole_operation(growth_service):
    state = growth_service.initialize({})
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        growth_service.credit_units(state, [{"card_id": 2, "level": 1, "count": 2}, {"card_id": 999, "level": 1, "count": 1}])
    assert error.value.code == "unit_card_missing" and state == before


@pytest.mark.parametrize("card_id,level", [(1, 1), (0, 1), (True, 2), (2 ** 31, 1), (2, -1), (2, True), (2, 2 ** 31)])
def test_grant_identity_needs_a_non_default_card_and_a_nonnegative_int32_level(service, card_id, level):
    with pytest.raises(ProtocolError) as error:
        service.validate_grant_identity(card_id, level)
    assert (error.value.code, error.value.status) == ("unit_grant_invalid", 400)
    assert service.validate_grant_identity(2, 0) is service.validate_grant_identity(2 ** 31 - 1, 2 ** 31 - 1) is None


def test_the_unsupported_grant_code_is_spelled_once_where_it_is_published():
    assert UNIT_GRANT_UNSUPPORTED == "unit_grant_unsupported"
    published = Path(module.__file__).resolve()
    spelled = [path.name for path in sorted(published.parent.glob("*.py"))
               for node in ast.walk(ast.parse(path.read_bytes()))
               if isinstance(node, ast.Constant) and node.value == UNIT_GRANT_UNSUPPORTED]
    assert spelled == [published.name]


def test_missing_growth_resources_do_not_break_existing_native_reads(service, store):
    before = store.profile()
    assert service.growth_status()["ready"] is False
    assert service.status()["ready"] is True
    assert service.battle_param(request(39, store), store)
    assert service.player_units(request(41, store), store)
    with pytest.raises(ProtocolError) as error:
        service.credit_units(before.state, [{"card_id": 2, "level": 1, "count": 1}])
    assert error.value.code == "player_growth_unavailable" and error.value.status == 503
    assert store.profile() == before


def test_growth_resource_cache_tracks_content_changes(growth_service, growth_setup):
    assert growth_service.experience_for_level(2, 3) == 65
    resources = growth_setup[1]
    resources["cfgRarity.atb"] = growth_rarity_table([(1, 150, 4)])
    assert growth_service.experience_for_level(2, 3) == 98


def test_unit_initializer_hashes_again_only_the_resources_it_rereads(growth_service, growth_setup, monkeypatch):
    resources = growth_setup[1]
    initializer = growth_service.unit_initializer()
    hashed = recorded_hashes(monkeypatch)
    for _ in range(3):
        assert growth_service.unit_initializer() is initializer
        assert growth_service.initialization_version == initializer.content_version
    reread = [resources[name] for name in ("ExperienceTable.atb", "cfgRarity.atb", "PlayerBloodType.atb")]
    assert hashed == reread * 6


def replaced_column(master, name, values):
    return dumps(Container(children=tuple(replace(column, values=values) if column.name == name else column
                                          for column in loads(master).children)))


def test_unit_initializer_follows_replaced_master_and_class_rows(growth_setup, monkeypatch):
    root, resources, master = growth_setup
    source = {"CardData": master}
    service = PlayerStateService(root, source.__getitem__, asset_reader=resources.__getitem__)
    rarities, initializer = service.card_rarities(), service.unit_initializer()
    copied = bytes(bytearray(master))
    assert copied is not master
    source["CardData"] = copied
    hashed = recorded_hashes(monkeypatch)
    for _ in range(3):
        assert service.card_rarities() is rarities and service.unit_initializer() is initializer
    assert sum(data is copied for data in hashed) == 1
    source["CardData"] = bytearray(master)
    with pytest.raises(ProtocolError) as caught:
        service.cards()
    assert (caught.value.code, caught.value.status) == ("player_master_unavailable", 503)
    source["CardData"] = copied
    assert service.card_rarities() is rarities
    with pytest.raises(TypeError):
        service.classes[30]["MaxLevel"] = 2
    original = service.classes[30]
    # True equals the stored 1 yet serializes differently, so only an identical row may skip the class digest.
    monkeypatch.setitem(service._classes, 30, {**original, "MaxLevel": True})
    with pytest.raises(ProtocolError) as caught:
        service.experience_for_level(8, 1)
    assert (caught.value.code, caught.value.status) == ("player_growth_reference", 503)
    monkeypatch.setitem(service._classes, 30, original)
    assert service.experience_for_level(8, 1) == 0
    assert service.initialization_version == initializer.content_version
    source["CardData"] = replaced_column(master, "ClassLV0SkillID", (59, 9, 4, 5, 6, 7, 8, 0))
    assert service.initialization_version != initializer.content_version
    assert service.initial_unit_values(2, 1).skill_id == 9


def test_unit_initializer_is_keyed_by_the_card_data_it_was_built_from(growth_setup, monkeypatch):
    root, resources, master = growth_setup
    source = {"CardData": master}
    service = PlayerStateService(root, source.__getitem__, asset_reader=resources.__getitem__)
    growth = service._growth_tables

    def switched_during_build():
        source["CardData"] = replaced_column(master, "ClassLV0SkillID", (59, 9, 4, 5, 6, 7, 8, 0))
        service.cards()
        return growth()

    monkeypatch.setattr(service, "_growth_tables", switched_during_build)
    assert service.initial_unit_values(2, 1).skill_id == 3
    monkeypatch.setattr(service, "_growth_tables", growth)
    assert service.initial_unit_values(2, 1).skill_id == 9


def test_unit_initializer_is_keyed_by_the_growth_tables_it_was_built_from(growth_service, growth_setup, monkeypatch):
    resources, classes = growth_setup[1], growth_service._class_inputs

    def switched_during_build():
        resources["cfgRarity.atb"] = growth_rarity_table([(1, 150, 4)])
        growth_service.growth_status()
        return classes()

    monkeypatch.setattr(growth_service, "_class_inputs", switched_during_build)
    assert growth_service.experience_for_level(2, 3) == 65
    monkeypatch.setattr(growth_service, "_class_inputs", classes)
    assert growth_service.experience_for_level(2, 3) == 98


def test_card_rarities_are_a_read_only_card_data_view_cached_per_digest(growth_setup, monkeypatch):
    root, resources, master = growth_setup
    source = {"CardData": master}
    service = PlayerStateService(root, source.__getitem__, asset_reader=resources.__getitem__)
    rarities = service.card_rarities()
    assert rarities == {1: 5, 2: 0, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 5}
    with pytest.raises(TypeError):
        rarities[2] = 1
    copied = bytes(bytearray(master))
    source["CardData"] = copied
    hashed = recorded_hashes(monkeypatch)
    for _ in range(3):
        assert service.card_rarities() is rarities
    assert len(hashed) == 1 and hashed[0] is copied
    source["CardData"] = replaced_column(master, "Rare", (5, 1, 1, 2, 3, 4, 5, 5))
    assert service.card_rarities()[2] == 1 and rarities[2] == 0


def test_card_data_without_a_rarity_column_publishes_no_rarities(service):
    assert service.card_rarities() == {}


def test_card_rarities_need_verified_card_data(assets):
    with pytest.raises(ProtocolError) as caught:
        PlayerStateService(assets[0], None).card_rarities()
    assert (caught.value.code, caught.value.status) == ("player_master_unavailable", 503)


def test_class_rows_are_a_read_only_view_of_the_loaded_class_table(growth_service, monkeypatch):
    classes = growth_service.classes
    assert list(classes) == [10, 20, 30] and classes.keys() == growth_service.class_ids
    assert dict(classes[20]) == {"ClassID": 20, "MaxLevel": 8, "JobChange": 0}
    with pytest.raises(TypeError):
        classes[40] = classes[30]
    with pytest.raises(TypeError):
        classes[20]["MaxLevel"] = 9
    replaced = {**classes, 30: {**classes[30], "MaxLevel": 2}}
    monkeypatch.setattr(growth_service, "_classes", replaced)
    assert growth_service.classes == replaced and classes[30]["MaxLevel"] == 1


def test_card_rows_are_a_read_only_view_of_the_verified_card_data(growth_setup):
    root, resources, master = growth_setup
    source = {"CardData": master}
    service = PlayerStateService(root, source.__getitem__, asset_reader=resources.__getitem__)
    cards = service.cards()
    assert list(cards) == list(range(1, 9))
    assert cards[8] == {"CardID": 8, "InitClassID": 30, "ClassLV0SkillID": 0, "ClassLV1SkillID": 0, "Rare": 5,
                        "Blood": 0, "ExcludeFameDisplayFirstClass": 0}
    assert {identifier: row["Rare"] for identifier, row in cards.items()} == service.card_rarities()
    with pytest.raises(TypeError):
        cards[9] = cards[8]
    with pytest.raises(TypeError):
        cards[8]["Rare"] = 4
    with pytest.raises(TypeError):
        del service.cards()[2]["InitClassID"]
    assert service.cards()[8]["Rare"] == 5 and service.cards()[2]["InitClassID"] == 20
    source["CardData"] = replaced_column(master, "ClassLV0SkillID", (59, 9, 4, 5, 6, 7, 8, 0))
    assert service.cards()[2]["ClassLV0SkillID"] == 9 and cards[2]["ClassLV0SkillID"] == 3
    with pytest.raises(ProtocolError) as caught:
        PlayerStateService(root, None, asset_reader=resources.__getitem__).cards()
    assert (caught.value.code, caught.value.status) == ("player_master_unavailable", 503)


def test_unit_initializer_is_the_cached_initializer_of_the_published_version(growth_service, monkeypatch):
    initializer = growth_service.unit_initializer()
    assert growth_service.unit_initializer() is initializer
    assert initializer.content_version == growth_service.initialization_version
    assert growth_service.initialization_status()["content_version"] == initializer.content_version
    replacement = SimpleNamespace(content_version="0" * 64)
    monkeypatch.setattr(growth_service, "_initialization", lambda: replacement)
    assert growth_service.unit_initializer() is replacement
    assert growth_service.initialization_version == replacement.content_version


def test_unit_initializer_needs_the_verified_growth_tables(service):
    with pytest.raises(ProtocolError) as caught:
        service.unit_initializer()
    assert (caught.value.code, caught.value.status) == ("player_growth_unavailable", 503)


@pytest.mark.parametrize("code,status", [(ASSET_NOT_FOUND, 404), (ASSET_NOT_CACHED, 404), (ASSET_CHANGED, 409)])
def test_indexed_growth_resource_failures_are_reported_as_unavailable(growth_setup, code, status):
    root, resources, master = growth_setup
    def read(name):
        if name == "ExperienceTable.atb":
            raise ProtocolError(code, "Resource is not available", status)
        return resources[name]
    service = PlayerStateService(root, lambda name: master, asset_reader=read)
    assert service.status()["ready"]
    assert service.growth_status() == {"ready": False, "unavailable": {"player_growth_unavailable": "Verified unit growth tables are unavailable"}}
    with pytest.raises(ProtocolError) as error:
        service.experience_for_level(2, 1)
    assert error.value.code == "player_growth_unavailable" and error.value.status == 503


@pytest.mark.parametrize("field", ["UX", "last_unique_id"])
def test_unit_identity_overflow_is_rejected_before_state_change(growth_service, field):
    state = growth_service.initialize({})
    target = state["player_state"]["battle"] if field == "UX" else state["player_state"]
    target[field] = 2 ** 31 - 1
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        growth_service.credit_units(state, [{"card_id": 2, "level": 1, "count": 1}])
    assert error.value.code == "unit_identity_exhausted" and state == before


@pytest.mark.parametrize("value", [0, -1, True, "2", 2 ** 31])
def test_invalid_unit_high_water_mark_is_not_silently_replaced(growth_service, value):
    state = growth_service.initialize({})
    state["player_state"]["last_unique_id"] = value
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        growth_service.credit_units(state, [{"card_id": 2, "level": 1, "count": 1}])
    assert error.value.code == "player_units_invalid" and state == before


def test_unit_grant_enforces_roster_limit_and_full_wire_capacity(growth_service):
    state = growth_service.initialize({})
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        growth_service.credit_units(state, [{"card_id": 2, "level": 1, "count": MAX_PLAYER_UNITS}])
    assert error.value.code == "unit_roster_capacity" and state == before
    updated, added = growth_service.credit_units(state, [{"card_id": 2, "level": 1, "count": MAX_PLAYER_UNITS - 1}])
    assert len(updated["player_state"]["units"]) == MAX_PLAYER_UNITS
    assert len(added) == MAX_PLAYER_UNITS - 1
    rng = random.Random(8431)
    template = before["player_state"]["units"][0]
    before["player_state"]["units"] = [{**template, "GetIndex": index, "UniqueID": index + 1,
        **{name: rng.randrange(1, 2 ** 31) for name in ("Exp", "Love", "SkillExperience", "CostExperience", "WatchLoveEv", "GeneralPurposeFlags")}}
        for index in range(MAX_PLAYER_UNITS - 1)]
    before["player_state"]["battle"]["UX"] = MAX_PLAYER_UNITS - 2
    unchanged = deepcopy(before)
    with pytest.raises(ProtocolError) as error:
        growth_service.credit_units(before, [{"card_id": 2, "level": 1, "count": 1}])
    assert error.value.code == "unit_roster_capacity" and error.value.status == 409
    assert before == unchanged


@pytest.mark.parametrize("fault", ["no_rarity", "duplicate_rarity", "zero_ratio", "bad_level_sequence", "bad_exp_order",
                                     "bad_exp_start", "negative_exp", "missing_max_level", "unknown_class", "missing_card_rarity",
                                     "unknown_card_rarity", "boolean_card_rarity", "cap_above_table", "exp_overflow"])
def test_invalid_growth_data_is_rejected_only_when_granting(growth_setup, fault):
    root, resources, master = growth_setup
    rarity_rows = [(i + 1, 100 + i * 10, min(i + 4, 8)) for i in range(6)]
    if fault in {"no_rarity", "duplicate_rarity", "zero_ratio", "cap_above_table", "exp_overflow"}:
        if fault == "no_rarity":
            rarity_rows = rarity_rows[1:]
        elif fault == "duplicate_rarity":
            rarity_rows.append(rarity_rows[0])
        elif fault == "zero_ratio":
            rarity_rows[0] = (1, 0, 4)
        elif fault == "cap_above_table":
            rarity_rows[0] = (1, 100, 9)
            resources["PlayerUnitTable.aar"] = archive_bytes([
                ("PlayerUnit.ard", b"verified player contract fixture"),
                ("ClassData.atb", numeric_table(("ClassID", "MaxLevel", "JobChange"), [(10, 99, 0), (20, 9, 0), (30, 1, 0)]))])
        else:
            rarity_rows[0] = (1, 2 ** 31 - 1, 4)
        resources["cfgRarity.atb"] = growth_rarity_table(rarity_rows)
    elif fault in {"bad_level_sequence", "bad_exp_order", "bad_exp_start", "negative_exp"}:
        values = [(0, 1), (32, 2), (65, 3), (100, 4), (135, 5), (172, 6), (210, 7), (250, 8)]
        if fault == "bad_level_sequence":
            values[1] = (32, 3)
        elif fault == "bad_exp_order":
            values[1] = (65, 2)
        elif fault == "bad_exp_start":
            values[0] = (1, 1)
        else:
            values[1] = (-1, 2)
        resources["ExperienceTable.atb"] = numeric_table(("Exp", "Level"), values)
    elif fault == "missing_max_level":
        resources["PlayerUnitTable.aar"] = archive_bytes([
            ("PlayerUnit.ard", b"verified player contract fixture"), ("ClassData.atb", class_table((10, 20, 30)))])
    else:
        document = loads(master)
        changed = []
        for column in document.children:
            if column.name == "Rare":
                if fault == "missing_card_rarity":
                    continue
                values = list(column.values)
                if fault == "unknown_card_rarity":
                    values[1] = 99
                elif fault == "boolean_card_rarity":
                    column = Column("Rare", "B", (True,) * len(values))
                if fault != "boolean_card_rarity":
                    column = replace(column, values=tuple(values))
            elif column.name == "InitClassID" and fault == "unknown_class":
                values = list(column.values)
                values[1] = 999
                column = replace(column, values=tuple(values))
            changed.append(column)
        master = dumps(Container(children=tuple(changed)))
    service = PlayerStateService(root, lambda name: master, asset_reader=resources.__getitem__)
    state = service.initialize({})
    before = deepcopy(state)
    assert service.status()["ready"] is True
    with pytest.raises(ProtocolError) as error:
        service.credit_units(state, [{"card_id": 2, "level": 4, "count": 1}])
    assert error.value.status == 503
    assert error.value.code.startswith("player_growth_")
    assert state == before
