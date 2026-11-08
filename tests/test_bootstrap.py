import hashlib
import struct
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.app import create_app
from aigis_ps.assets import asset_key
from aigis_ps.bootstrap import (BOOLEAN_CONFIGURATION_FIELDS, BootstrapService, COLLECTION_TABLES, LocalRules,
                                STARTUP_COLLECTIONS, TABLES, configuration_updates)
from aigis_ps.business_calendar import LEGACY, MARKED, MARKER, MARKER_KEY, civil_day, describe, status as calendar_status
from aigis_ps.config_state import SOUND_OPTION_FIELDS, configuration_delta, configuration_row, patch_configuration
from aigis_ps.errors import ProtocolError
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.protocol import ProtocolRegistry, RequestEnvelope, decode_wire, encode_response, encode_wire, prepare_request
from aigis_ps.storage import Store
from aigis_ps.wallet_money import GOLD_LIMIT
from test_business_calendar import HostGuardedInstant, guard_host_calendar
from test_native_numbers import BOUNDARY_VALUES, admitted, fits_int32


CREATED = datetime(2026, 10, 31, 15, 5, tzinfo=timezone.utc)
CREATED_TEXT = "2026/11/01 00:05:00"
# Unmarked saves keep the creation text a UTC+8 host wrote for the same instant.
HOST_CREATED_TEXT = "2026/10/31 23:05:00"


def schema_bytes(fields):
    definitions = []
    offset = 0
    for name, code in fields:
        size = 40 if name == "UserName" else 256 if code == 33 else 8 if code == 9 else 1 if code == 5 else 4
        parameters = struct.pack("<I", size) if code == 33 else b"\0" * (1 if code == 5 else 8)
        definitions.append((name, offset, code, parameters))
        offset += size
    data = bytearray(struct.pack("<4sBBHH", b"ALRD", 1, 0, len(definitions), offset))
    for name, position, code, parameters in definitions:
        encoded = name.encode()
        data += struct.pack("<HBBBB", position, code, len(parameters), len(encoded), 0)
        data += encoded + b"\0\0"
        data += b"\0" * (-len(data) % 4)
        data += parameters
        data += b"\0" * (-len(data) % 4)
    return bytes(data), offset


@pytest.fixture
def asset_root(tmp_path):
    source = tmp_path / "assets"
    source.mkdir()
    for spec in TABLES.values():
        schema, _ = schema_bytes((name, code) for name, code, _ in spec.fields)
        (source / asset_key(spec.resource)).write_bytes(schema)
    schema, stride = schema_bytes((("CharismaMax", 1), ("NextExp", 1)))
    start = 20 + len(schema)
    rank_table = struct.pack("<4sBBHHHI4s", b"ALTB", 1, 0x10, 3, 20, start, stride, b"rkup")
    rank_table += schema + struct.pack("<iiiiii", 60, 16, 63, 27, 66, 70)
    (source / asset_key("RankUp.atb")).write_bytes(rank_table)
    return source


@pytest.fixture
def service(asset_root):
    return BootstrapService(asset_root, clock=lambda: datetime(2026, 10, 31, 12, 30), asset_version="a" * 40)


@pytest.fixture
def store(tmp_path, service):
    return Store(tmp_path / "save.sqlite3", service.initial_state())


def request(number, store, **overrides):
    registry = ProtocolRegistry.load()
    profile = registry.by_registration[number] if isinstance(number, int) else next(p for p in registry.profiles if p.name == number)
    account = store.profile()
    if number == "session_flags":
        document = ET.Element("UI")
        document.text = overrides.get("UI", account.player_id)
        xml = ET.tostring(document)
        return RequestEnvelope(profile, xml, document, hashlib.sha256(xml).hexdigest())
    document = ET.Element("DA")
    if number == "game_info":
        values = {"Platform": "local", **overrides}
    elif number == 142:
        values = {"CT": 0, "PFN": "dmm-r18", "PlatformCode": 1,
                  "UI": "", "UK": "", "MK": "", "GPVI": "unkown", "GPOT": "unkown", **overrides}
    elif number == 152:
        values = {"VI": account.player_id, "OT": "local-only", "GPVI": account.player_id, "GPOT": "local-only", **overrides}
    else:
        values = {"UI": account.player_id, "UK": account.user_key, **overrides}
        if number not in ("heartbeat", "quest_record"):
            values = {"MK": account.method_key, **values}
    for name, value in values.items():
        if number == "quest_record":
            ET.SubElement(document, name).text = value
        else:
            ET.SubElement(ET.SubElement(document, name, {"T": "I" if type(value) is int else "S"}), "V").text = str(value)
    xml = ET.tostring(document)
    return RequestEnvelope(profile, xml, document, hashlib.sha256(xml).hexdigest())


def invoke(service, store, number, **overrides):
    return loads(service.handlers()[number](request(number, store, **overrides), store))


def columns(container):
    return {column.name: column for column in container.children}


def test_card_status_provider_returns_flat_native_table(service, store):
    names = ("CardID", "A1", "A3", "A5", "A7", "B0", "B1", "B2", "B3", "B4")
    values = (1, 0, 0, 0, 5, 1793491200, 0, 0, 0, 0)
    service.card_status_provider = lambda _state: Container("CardStatus", tuple(
        Column(name, "I", (value,)) for name, value in zip(names, values)))
    before = store.profile()
    response = invoke(service, store, 51)
    assert tuple(column.name for column in response.children) == names
    assert response.children[4].values == (5,)
    assert store.profile() == before


@pytest.mark.parametrize("value", [None, "xml", Column("CardID", "I", ()), Container("DA"), Container("CardStatus", index="1")])
def test_card_status_provider_rejects_wrong_return_shape(service, store, value):
    service.card_status_provider = lambda _state: value
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, 51)
    assert error.value.code == "card_status_provider_invalid"
    assert store.profile() == before


def test_card_status_provider_must_be_callable(asset_root):
    with pytest.raises(ValueError, match="Card-status provider"):
        BootstrapService(asset_root, card_status_provider=1)


def test_present_provider_keeps_system_and_full_ppt_envelope(service, store):
    present = Container("PPT", (Column("PresentID", "I", (1,)), Column("A1", "I", (1,)),
                                Column("A2", "I", (3,)), Column("A3", "S", ("交流達成報酬",))))
    service.present_provider = lambda _state: present
    before = store.profile()
    response = invoke(service, store, 42)
    assert [child.name for child in response.children] == ["SYSTEM", "PPT"]
    assert response.children[1] == present
    assert store.profile() == before


@pytest.mark.parametrize("value", [None, b"xml", Column("PresentID", "I", ()), Container("DA"), Container("PPT", index="1")])
def test_present_provider_rejects_wrong_return_shape(service, store, value):
    service.present_provider = lambda _state: value
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, 42)
    assert error.value.code == "present_provider_invalid"
    assert store.profile() == before


def test_present_provider_must_be_callable(asset_root):
    with pytest.raises(ValueError, match="Present provider"):
        BootstrapService(asset_root, present_provider=1)


def test_peruse_provider_is_included_in_startup_snapshot(service, store):
    history = Container("PEV", (Column("ID", "S", ("evTrustFemale150",)), Column("FLG", "I", (1,))))
    service.peruse_provider = lambda _state: history
    before = store.profile()
    response = columns(invoke(service, store, 137))
    assert response["PEV"] == history
    assert store.profile() == before


@pytest.mark.parametrize("value", [None, "xml", Column("ID", "S", ()), Container("DA"), Container("PEV", index="1")])
def test_peruse_provider_rejects_wrong_return_shape(service, store, value):
    service.peruse_provider = lambda _state: value
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, 137)
    assert error.value.code == "peruse_provider_invalid"
    assert store.profile() == before


def test_peruse_provider_must_be_callable(asset_root):
    with pytest.raises(ValueError, match="Peruse provider"):
        BootstrapService(asset_root, peruse_provider=1)


def test_optional_configuration_fields_do_not_break_legacy_partial_updates(service):
    state = service.initial_state()
    state["tables"]["config"][0].pop("DamageDispEnable")
    updated = patch_configuration(state, {"MenuTutorialFlags": 6})
    assert configuration_row(updated)["MenuTutorialFlags"] == 6
    assert "DamageDispEnable" not in updated["tables"]["config"][0]
    displayed = patch_configuration(updated, {"DamageDispEnable": 1})
    assert displayed["tables"]["config"][0]["DamageDispEnable"] == 1
    assert "DamageDispEnable" not in state["tables"]["config"][0]
    with pytest.raises(ProtocolError):
        configuration_delta(configuration_row(displayed), ("DamageDispEnable",))


@pytest.mark.parametrize("value", [None, True, "1", 2 ** 31])
def test_present_optional_configuration_fields_remain_strict(service, value):
    state = service.initial_state()
    state["tables"]["config"][0]["DamageDispEnable"] = value
    with pytest.raises(ProtocolError):
        patch_configuration(state, {"MenuTutorialFlags": 6})


@pytest.mark.parametrize("wire", ["A4", "C2", "A2"])
@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_configuration_updates_admit_exactly_the_native_int32_range(wire, value):
    values = {wire: value} if wire == "A4" else {**dict.fromkeys(SOUND_OPTION_FIELDS, 0), wire: value}
    expected = fits_int32(value) and (wire not in BOOLEAN_CONFIGURATION_FIELDS or value in (0, 1))
    assert admitted(lambda: configuration_updates(values), "invalid_configuration_request") == expected


def test_seed_uses_native_rank_table_and_preserves_unfinished_tutorial(service):
    state = service.initial_state()
    player = state["tables"]["game_param"][0]
    assert (player["Money"], player["Experience"], player["Rank"], player["NowCharisma"]) == (1000, 0, 1, 60)
    assert player["NowActionPoint"] == 0
    assert len(player) == 22
    config = state["tables"]["config"][0]
    assert config["MenuTutorialFlags"] == 0
    assert config["ExperienceUpCloseDateTime"] == ""
    assert state["tables"]["game_param2"][0]["_EvCmb"] == 1
    assert state["tables"]["presents"] == state["tables"]["crystals"] == []
    assert service.status()["local_rules"]["official_defaults"] is False
    assert "bootstrap_token" not in service.status()["local_rules"]
    state["tables"]["game_param"][0]["Money"] = 99
    assert service.initial_state()["tables"]["game_param"][0]["Money"] == 1000


def test_new_saves_never_consult_the_host_calendar(service, store, monkeypatch):
    created = HostGuardedInstant(2026, 10, 31, 11, 30, tzinfo=timezone(timedelta(hours=8)))
    with monkeypatch.context() as patched:
        guard_host_calendar(patched)
        patched.setattr(service, "clock", lambda: created)
        state = service.initial_state()
    player = state["tables"]["game_param"][0]
    assert state["created_local"] == state["tables"]["game_param2"][0]["GameStartDateTime"] == "2026/10/31 12:30:00"
    assert player["CharismaChangeDate"] == datetime(2026, 10, 31).toordinal() - 1
    assert player["CharismaChangeTime"] == struct.unpack("<f", struct.pack("<f", 12.5 / 24))[0]
    config = columns(invoke(service, store, 36))
    assert config["A6"].type.value == "DT" and config["A6"].values == ("0",)
    player_columns = columns(invoke(service, store, 34))
    assert player_columns["A6"].type.value == "I"
    assert player_columns["A7"].type.value == "F"


def test_login_and_method_key_have_native_table_shapes(service, store):
    account = store.profile()
    system = invoke(service, store, 31).children[0]
    assert system.name == "SYSTEM"
    assert system.children[0].values == ("ExchangeKey", "PaymentTransaction")
    assert system.children[1].values == (account.method_key, "0")
    login = invoke(service, store, 114).children[1]
    assert login.name == "LGI" and len(login.children) == 13
    assert [login.children[index].values for index in (0, 1, 2, 6, 9, 12)] == [(1,), (1,), (0,), (account.user_key,), ("1",), (0,)]
    assert [column.type.value for column in login.children] == ["I"] * 6 + ["S", "I", "I", "S", "I", "I", "I"]


def test_login_reports_an_account_without_a_second_data_slot(service, store):
    login = columns(invoke(service, store, 114).children[1])
    assert login["A3"].values == (0,)
    assert [login[name].values for name in ("A2", "A6", "A10")] == [(1,), (1,), ("1",)]


@pytest.mark.parametrize("number,count", [(34, 22), (35, 30), (36, 30), (38, 5)])
def test_flat_tables_use_all_verified_columns(service, store, number, count):
    response = invoke(service, store, number)
    assert response.name == "DA" and len(response.children) == count
    assert all(isinstance(column, Column) and len(column.values) == 1 for column in response.children)


def test_convert_response_is_a_key_value_table_with_raw_dates(service, store):
    result = invoke(service, store, 33)
    data = result.children[1]
    assert data.name == "PGP2"
    values = dict(zip(data.children[0].values, data.children[1].values))
    assert values["A5"] == "0" and values["C7"] == "1"
    assert values["ComebackLoginDateTime"] == ""
    assert values["LatestComebackLoginBonusDateTime"] == ""


def test_present_and_crystal_empty_inventories_are_explicit(service, store):
    result = invoke(service, store, 42)
    assert result.children[1] == Container("PPT")
    for number in (43, 44):
        result = invoke(service, store, number)
        assert set(columns(result)) == {"A1", "A2", "Date"}
        assert all(column.values == () for column in result.children)


def test_paid_and_free_crystal_rows_are_separate(service, store):
    account = store.profile()
    account.state["tables"]["crystals"] = [
        {"FreeFlag": True, "Num": 4, "Date": 0},
        {"FreeFlag": False, "Num": 7, "Date": 0},
    ]
    store.update_state(account.state, account.revision)
    assert columns(invoke(service, store, 43))["A2"].values == (7,)
    assert columns(invoke(service, store, 44))["A2"].values == (4,)


def test_reads_persisted_state_without_resetting_profile(service, store):
    account = store.profile()
    account.state["tables"]["game_param"][0]["Money"] = 9876
    store.update_state(account.state, account.revision)
    reopened = Store(store.path, service.initial_state())
    assert columns(invoke(service, reopened, 34))["A1"].values == (9876,)
    assert reopened.profile().revision == 1


@pytest.mark.parametrize("override", [{"UI": "2"}, {"UK": "wrong"}, {"UK": "非法"}, {"MK": "wrong"}, {"UK": "local-only"}])
def test_post_login_handlers_validate_account_and_method_key(service, store, override):
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, 34, **override)
    assert error.value.status == 403


def test_duplicate_identity_is_rejected(service, store):
    envelope = request(31, store)
    ET.SubElement(envelope.document, "UI").text = "1"
    with pytest.raises(ProtocolError) as error:
        service.method_key(envelope, store)
    assert error.value.status == 400


@pytest.mark.parametrize("table,field,value,number", [
    ("game_param", "Money", 0, 34),
    ("game_param", "Rank", 2, 51),
    ("game_param", "CharismaChangeTime", 12.5, 34),
    ("config", "UserName", "x" * 40, 36),
    ("config", "ExperienceUpCloseDateTime", "2026-10-31T00:00:00Z", 36),
    ("game_param2", "GameStartDateTime", "2026-10-31T00:00:00Z", 33),
])
def test_invalid_state_fails_instead_of_success(service, store, table, field, value, number):
    account = store.profile()
    account.state["tables"][table][0][field] = value
    store.update_state(account.state, account.revision)
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, number)
    assert error.value.status == 503


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_saved_native_dates_admit_nonnegative_int32_days(service, store, value):
    account = store.profile()
    account.state["tables"]["game_param2"][0]["BonusPremiumSummonDate"] = value
    store.update_state(account.state, account.revision)
    assert admitted(lambda: invoke(service, store, 35), "invalid_bootstrap_value") == (fits_int32(value) and value >= 0)


def test_missing_schema_or_state_does_not_create_a_success_response(tmp_path, store):
    service = BootstrapService(tmp_path)
    assert service.status()["unavailable"]
    assert invoke(service, store, 31).children[0].name == "SYSTEM"
    for number in (34, 35, 36, 38, 43, 114):
        with pytest.raises(ProtocolError) as error:
            invoke(service, store, number)
        assert error.value.status == 503


def test_nonempty_unknown_present_rows_are_not_discarded(service, store):
    account = store.profile()
    account.state["tables"]["presents"] = [{"PresentID": 1}]
    store.update_state(account.state, account.revision)
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, 42)
    assert error.value.status == 503
    assert store.profile().state["tables"]["presents"] == [{"PresentID": 1}]


@pytest.mark.parametrize("state", [{}, {"bootstrap_schema": 1, "tables": []}, {"bootstrap_schema": True, "tables": {}}])
def test_invalid_saved_container_has_an_explicit_unavailable_error(service, store, state):
    account = store.profile()
    store.update_state(state, account.revision)
    for number in (34, 35, 42):
        with pytest.raises(ProtocolError) as error:
            invoke(service, store, number)
        assert error.value.status == 503


def test_empty_startup_collections_have_verified_consumers(service, store):
    assert columns(invoke(service, store, 51))["CardID"].values == ()
    assert invoke(service, store, 129).children[1] == Container("POSSESSION")
    result = invoke(service, store, 137)
    assert [group.name for group in result.children] == ["SYSTEM", *STARTUP_COLLECTIONS]
    assert all(group.children == () for group in result.children[1:])
    assert store.profile().revision == 0


@pytest.mark.parametrize("number,name", [(51, "card_status"), (129, "possessions"), (137, "possessions"), (137, "event_pass_status")])
def test_unknown_nonempty_collection_is_not_discarded(service, store, number, name):
    account = store.profile()
    account.state["tables"][name] = [{"unknown": 1}]
    store.update_state(account.state, account.revision)
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, number)
    assert error.value.status == 503
    assert store.profile().state["tables"][name] == [{"unknown": 1}]


@pytest.mark.parametrize("number", [129, 137])
def test_possession_provider_returns_persisted_absolute_counts_over_native_wire(tmp_path, asset_root, number):
    received = []
    def possessions(state):
        received.append(deepcopy(state))
        rows = state["tables"]["possessions"]
        return Container("POSSESSION", (Column("ItemID", "S", tuple(row["ItemID"] for row in rows)),
                                        Column("Count", "I", tuple(row["Count"] for row in rows))))
    service = BootstrapService(asset_root, possession_provider=possessions)
    state = service.initial_state()
    state["tables"]["possessions"] = [{"ItemID": "gift_0002_ruby", "Count": 7},
                                      {"ItemID": "gift_1002_fineLiquor", "Count": 3}]
    app = create_app(tmp_path / "possession-http", handlers=service.handlers(), initial_state=state)
    before = app.state.store.profile()
    profile = app.state.registry.by_registration[number]
    envelope = request(number, app.state.store)
    xml = prepare_request(ET.tostring(envelope.document), profile.st)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/api/" + profile.route, content=encode_wire(xml, profile.request_key))
    assert response.status_code == 200
    result = columns(loads(decode_wire(response.content, profile.response_key)))
    assert result["POSSESSION"].children == (Column("ItemID", "S", ("gift_0002_ruby", "gift_1002_fineLiquor")),
                                              Column("Count", "I", (7, 3)))
    if number == 137:
        assert list(result) == ["SYSTEM", *STARTUP_COLLECTIONS]
        assert all(not group.children for name, group in result.items() if name not in ("SYSTEM", "POSSESSION"))
    assert received == [before.state]
    assert app.state.store.profile() == before
    assert service.status()["possession_provider_configured"] is True


def test_possession_provider_does_not_enable_other_unknown_collections(asset_root, store):
    service = BootstrapService(asset_root, possession_provider=lambda _: Container("POSSESSION"))
    profile = store.profile()
    profile.state["tables"]["event_pass_status"] = [{"unknown": 1}]
    store.update_state(profile.state, profile.revision)
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, 137)
    assert error.value.code == "collection_rows_unavailable"
    assert store.profile() == before


@pytest.mark.parametrize("number", [129, 137])
def test_invalid_possession_provider_result_fails_without_changing_save(asset_root, store, number):
    service = BootstrapService(asset_root, possession_provider=lambda _: Container("OTHER"))
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, number)
    assert error.value.code == "possession_provider_invalid"
    assert error.value.status == 503
    assert store.profile() == before


def test_provider_is_optional_and_empty_master_data_is_not_accepted(asset_root, store):
    for service in (BootstrapService(asset_root), BootstrapService(asset_root, lambda name: b"<DA />")):
        with pytest.raises(ProtocolError) as error:
            service.delegated(request("master_cards", store), store)
        assert error.value.status == 503
    data = dumps(Container(children=tuple(Column(name, "I", (1, 2)) for name in ("CardID", "InitClassID", "ClassLV0SkillID", "ClassLV1SkillID"))))
    calls = []
    service = BootstrapService(asset_root, lambda name: calls.append(name) or data)
    assert service.delegated(request("master_cards", store), store) == data
    assert calls == ["CardData"]


def test_local_rules_use_verified_rank_thresholds(asset_root):
    service = BootstrapService(asset_root, local_rules=LocalRules(rank=2, experience=16, money=0))
    row = service.initial_state()["tables"]["game_param"][0]
    assert (row["Rank"], row["NowCharisma"]) == (2, 63)
    invalid = BootstrapService(asset_root, local_rules=LocalRules(rank=2, experience=0))
    assert "ranks" in invalid.status()["unavailable"]
    assert "game_param" not in invalid.initial_state()["tables"]
    with pytest.raises(ValueError):
        LocalRules(money=0, experience=0)


@pytest.mark.parametrize("name, limit", [("money", GOLD_LIMIT), ("experience", INT32_MAX), ("rank", INT32_MAX)])
def test_local_rules_cap_starting_money_at_the_gold_limit_and_counters_at_int32(name, limit):
    assert getattr(LocalRules(**{name: limit}), name) == limit
    with pytest.raises(ValueError, match=f"Invalid local rule: {name}"):
        LocalRules(**{name: limit + 1})


@pytest.mark.parametrize("minutes", [-600, 0, 480, 540, 840])
def test_new_saves_write_the_jst_creation_wall_clock_and_the_business_calendar_marker(asset_root, minutes):
    moment = CREATED.astimezone(timezone(timedelta(minutes=minutes)))
    state = BootstrapService(asset_root, clock=lambda: moment).initial_state()
    assert state["created_local"] == state["tables"]["game_param2"][0]["GameStartDateTime"] == CREATED_TEXT
    assert state[MARKER_KEY] == dict(MARKER) and describe(state)["status"] == MARKED
    assert state["tables"]["game_param"][0]["CharismaChangeDate"] == civil_day(moment)


def test_unmarked_legacy_saves_load_unchanged_and_never_gain_a_marker(service, tmp_path):
    legacy = service.initial_state()
    del legacy[MARKER_KEY]
    legacy["created_local"] = legacy["tables"]["game_param2"][0]["GameStartDateTime"] = HOST_CREATED_TEXT
    store = Store(tmp_path / "legacy.sqlite3", legacy)
    for number in (33, 34, 36, 43, 44):
        invoke(service, store, number)
    assert columns(invoke(service, store, 35))["GameStartDateTime"].values == (HOST_CREATED_TEXT,)
    saved = store.profile()
    assert saved.revision == 0 and saved.state == legacy and describe(saved.state)["status"] == LEGACY


def test_status_reports_the_business_calendar_policy(service):
    assert service.status()["business_calendar"] == calendar_status()


def test_wire_dispatch_preserves_checksum_and_response_keys(tmp_path, service):
    app = create_app(tmp_path / "http", handlers=service.handlers(), initial_state=service.initial_state())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        exchange = request(152, app.state.store)
        exchange_wire = encode_wire(prepare_request(exchange.xml, exchange.profile.st), exchange.profile.request_key)
        response = client.post("/api/" + exchange.profile.route, content=exchange_wire)
        assert response.status_code == 200
        identity = columns(loads(decode_wire(response.content, exchange.profile.response_key)).children[0])
        assert identity["UK"].values == (app.state.store.profile().user_key,)
        envelope = request(31, app.state.store, UI=identity["UI"].values[0], UK=identity["UK"].values[0], MK="")
        profile = envelope.profile
        xml = prepare_request(envelope.xml, profile.st)
        response = client.post("/api/" + profile.route, content=encode_wire(xml, profile.request_key))
        assert response.status_code == 200
        system = loads(decode_wire(response.content, profile.response_key)).children[0]
        assert system.children[1].values[0] == app.state.store.profile().method_key


def test_game_info_is_public_integer_column_and_fits_native_buffer(service, store):
    profile = next(p for p in ProtocolRegistry.load().profiles if p.name == "game_info")
    xml = b'<DA><Platform T="S"><V>local</V></Platform></DA>'
    envelope = RequestEnvelope(profile, xml, ET.fromstring(xml), hashlib.sha256(xml).hexdigest())
    data = service.game_info(envelope, store)
    result = loads(data)
    assert result.children[0].values == ("Major", "Minor", "Maintenance", "LimitType", "Priority")
    assert result.children[1].type.value == "I"
    assert result.children[1].values == (2, 400, 0, 0, 0)
    wire, _ = encode_response(profile, data)
    assert len(wire) <= 1024


def test_heartbeat_has_all_groups_resource_version_and_needs_no_method_key(asset_root, store):
    service = BootstrapService(asset_root, asset_base_url="http://127.0.0.1:19999/assets/", asset_version="b" * 40)
    envelope = request("heartbeat", store)
    data = service.heartbeat(envelope, store)
    result = loads(data)
    assert [group.name for group in result.children] == ["AssetPath", "GameInfo", "SYSTEM", "MBL"]
    assert result.children[0].children[0].values == ("b" * 40,)
    assert result.children[1].children[1].type.value == "I"
    assert result.children[-1].children == ()
    wire, _ = encode_response(envelope.profile, data)
    assert len(wire) <= 4096


@pytest.mark.parametrize("url", ["https://example.com/assets/", "http://127.0.0.1:18103/assets/?redirect=x", "http://user:pass@localhost/assets/", "http://localhost/assets", " http://localhost/assets/"])
def test_asset_base_stays_local_and_unambiguous(asset_root, url):
    with pytest.raises(ValueError):
        BootstrapService(asset_root, asset_base_url=url)


def legacy_collections(store):
    account = store.profile()
    account.state.pop("startup_collections_schema")
    for name in COLLECTION_TABLES:
        account.state["tables"].pop(name, None)
    account.state["tables"]["game_param"][0]["Money"] = 4321
    return store.update_state(account.state, account.revision)


def test_legacy_save_gains_only_missing_collections_once(service, store):
    previous = legacy_collections(store)
    invoke(service, store, 51)
    current = store.profile()
    assert current.revision == previous.revision + 1
    assert current.state["tables"]["game_param"][0]["Money"] == 4321
    assert all(current.state["tables"][name] == [] for name in COLLECTION_TABLES)
    assert current.user_key == previous.user_key and current.method_key == previous.method_key
    invoke(service, store, 137)
    assert store.profile().revision == current.revision


def test_collection_migration_retries_without_losing_concurrent_state(service, store, monkeypatch):
    legacy_collections(store)
    original = store.update_state
    raced = False

    def update(state, revision):
        nonlocal raced
        if not raced:
            raced = True
            concurrent = store.profile()
            concurrent.state["tables"]["game_param"][0]["Money"] = 9876
            original(concurrent.state, concurrent.revision)
        return original(state, revision)

    monkeypatch.setattr(store, "update_state", update)
    invoke(service, store, 51)
    assert raced and store.profile().state["tables"]["game_param"][0]["Money"] == 9876


def quest_master(ids=(1001, 1002)):
    return dumps(Container(children=(Column("QuestID", "I", ids), *(Column(name, "I", (1,) * len(ids)) for name in ("MapNo", "EntryNo", "LocationNo", "Capacity")))))


def test_quest_record_missing_master_returns_503_without_inventing_ids(service, store):
    envelope = request("quest_record", store)
    with pytest.raises(ProtocolError) as error:
        service.quest_record(envelope, store)
    assert error.value.status == 503
    assert store.profile().state["tables"]["quest_records"] == []


def test_quest_records_seed_verified_ids_and_preserve_progress(asset_root, store):
    data = quest_master()
    service = BootstrapService(asset_root, master_provider=lambda name: data)
    result = invoke(service, store, "quest_record")
    assert columns(result)["QuestID"].values == (1001, 1002)
    assert columns(result)["A1"].values == (0, 0)
    assert columns(result)["Shown"].values == (False, False)
    previous = store.profile()
    previous.state["tables"]["quest_records"][0]["MaxRank"] = 3
    store.update_state(previous.state, previous.revision)
    revision = store.profile().revision
    assert columns(invoke(service, store, "quest_record"))["A1"].values == (3, 0)
    assert store.profile().revision == revision
    data = quest_master((1001, 1002, 1003))
    assert columns(invoke(service, store, "quest_record"))["A1"].values == (3, 0, 0)


def test_quest_records_reject_unknown_ids_and_private_master_columns(asset_root, store):
    service = BootstrapService(asset_root, master_provider=lambda name: quest_master())
    invoke(service, store, "quest_record")
    account = store.profile()
    account.state["tables"]["quest_records"][0]["QuestID"] = 9999
    store.update_state(account.state, account.revision)
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, "quest_record")
    assert error.value.code == "quest_record_master_mismatch"
    data = quest_master().replace(b"</DA>", b'<UI T="S"><V>secret</V><V>secret</V></UI></DA>')
    service = BootstrapService(asset_root, master_provider=lambda name: data)
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, "quest_record")
    assert error.value.status == 503


def test_readiness_names_only_real_handlers_and_describes_master_dependency(service, asset_root):
    assert not service.status()["startup_ready"]
    ready = BootstrapService(asset_root, master_provider=lambda name: quest_master(), asset_version="a" * 40).status()
    assert ready["startup_ready"]
    assert ready["unimplemented_operations"] == []
    assert ready["delegated_operations"] == ["master_cards", "master_quests"]
    assert "separately validated public master" in ready["startup_scope"]


def test_remaining_startup_routes_use_their_actual_wire_profiles(tmp_path, asset_root):
    service = BootstrapService(asset_root, master_provider=lambda name: quest_master(),
                               asset_base_url="http://127.0.0.1:19001/assets/", asset_version="a" * 40)
    app = create_app(tmp_path / "complete-http", handlers=service.handlers(), initial_state=service.initial_state())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for operation in ("game_info", "heartbeat", 51, 129, 137, "quest_record"):
            registry = app.state.registry
            profile = registry.by_registration[operation] if isinstance(operation, int) else next(p for p in registry.profiles if p.name == operation)
            envelope = request(operation, app.state.store, Platform="local")
            xml = ET.tostring(envelope.document, short_empty_elements=False)
            if profile.checksum_required:
                xml = prepare_request(xml, profile.st)
            response = client.post("/api/" + profile.route, content=encode_wire(xml, profile.request_key))
            assert response.status_code == 200, (operation, response.text)
            decoded = loads(decode_wire(response.content, profile.response_key))
            if operation == "quest_record":
                assert columns(decoded)["QuestID"].values == (1001, 1002)
    assert len(app.state.store.profile().state["tables"]["quest_records"]) == 2


def test_heartbeat_never_substitutes_a_url_for_missing_asset_version(asset_root, store):
    service = BootstrapService(asset_root)
    with pytest.raises(ProtocolError) as error:
        service.heartbeat(request("heartbeat", store), store)
    assert error.value.code == "asset_version_unavailable"


def test_platform_exchange_returns_native_named_string_columns_without_resetting_save(service, store):
    account = store.profile()
    account.state["tables"]["config"][0]["UserName"] = "Saved Player"
    account.state["tables"]["game_param"][0]["Money"] = 5432
    saved = store.update_state(account.state, account.revision)
    for _ in range(2):
        result = invoke(service, store, 152)
        assert [child.name for child in result.children] == ["LI"]
        values = columns(result.children[0])
        assert list(values) == ["UK", "UI", "NN"]
        assert all(value.type.value == "S" and len(value.values) == 1 for value in values.values())
        assert values["UK"].values == (saved.user_key,)
        assert values["UI"].values == (saved.player_id,)
        assert values["NN"].values == ("Saved Player",)
        assert store.profile() == saved


def test_title_configuration_is_available_before_identity_exchange_without_changing_save(service, store):
    saved = store.profile()
    envelope = request(142, store)
    for _ in range(2):
        xml = service.handlers()[142](envelope, store)
        wire, _ = encode_response(envelope.profile, xml)
        response = loads(decode_wire(wire, envelope.profile.response_key))
        assert [child.name for child in response.children] == ["SYSTEM", "CONFIG"]
        assert isinstance(response.children[1], Container) and not response.children[1].children
        system = columns(response.children[0])
        assert system["A1"].values == ("ExchangeKey", "PaymentTransaction")
        assert system["A2"].values == (saved.method_key, "0")
        assert store.profile() == saved
    assert service.status()["title_configuration"]["remote_overrides"] is False


@pytest.mark.parametrize("name,fragment", [
    ("CT", '<CT T="S"><V>0</V></CT>'),
    ("PlatformCode", '<PlatformCode T="I"><V>1</V><V>2</V></PlatformCode>'),
    ("PFN", '<PFN>dmm-r18</PFN>'),
    ("PFN", '<PFN T="S">extra<V>dmm-r18</V></PFN>'),
    ("GPVI", None),
    ("GPOT", '<GPOT T="S"><V>' + 'x' * 257 + '</V></GPOT>'),
])
def test_title_configuration_rejects_malformed_native_fields(service, store, name, fragment):
    envelope = request(142, store)
    envelope.document.remove(envelope.document.find(name))
    if fragment is not None:
        envelope.document.append(ET.fromstring(fragment))
    with pytest.raises(ProtocolError) as error:
        service.title_configuration(envelope, store)
    assert error.value.status == 400


def test_title_configuration_rejects_duplicate_fields(service, store):
    envelope = request(142, store)
    envelope.document.append(ET.fromstring('<PFN T="S"><V>dmm-r18</V></PFN>'))
    with pytest.raises(ProtocolError):
        service.title_configuration(envelope, store)


@pytest.mark.parametrize("overrides", [{"VI": "other"}, {"OT": "other"}, {"GPVI": "other"}, {"GPOT": "other"}])
def test_platform_exchange_rejects_wrong_local_credentials_or_conflicting_aliases(service, store, overrides):
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, 152, **overrides)
    assert error.value.status == 403
    assert error.value.code == "invalid_platform_identity"


def test_platform_exchange_accepts_absent_or_native_unknown_common_context(service, store):
    envelope = request(152, store)
    for name in ("GPVI", "GPOT"):
        envelope.document.remove(envelope.document.find(name))
    assert loads(service.platform_login(envelope, store)).children[0].name == "LI"
    assert invoke(service, store, 152, GPVI="unkown", GPOT="unkown").children[0].name == "LI"


@pytest.mark.parametrize("field", ["UI", "UK", "MK"])
def test_platform_exchange_rejects_game_identity_fields_removed_by_native_writer(service, store, field):
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, 152, **{field: ""})
    assert error.value.status == 400


@pytest.mark.parametrize("operation", [31, 114, "heartbeat"])
def test_platform_token_is_not_accepted_as_game_user_key(service, store, operation):
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, operation, UK="local-only")
    assert error.value.status == 403


@pytest.mark.parametrize("fragment", [
    '<UI>1</UI>', '<UI T="S"></UI>', '<UI T="S"><V>1</V><V>1</V></UI>',
    '<UI T="I"><V>1</V></UI>', '<UI T="S" N="UI"><V>1</V></UI>',
    '<UI T="S">outside<V>1</V></UI>', '<UI T="S"><V extra="x">1</V></UI>',
    '<UI T="S"><V><Nested>1</Nested></V></UI>', '<UI T="S"><Value>1</Value></UI>',
    '<UI T="S"><V>1</V>outside</UI>',
])
def test_registered_identity_requires_one_native_string_value(service, store, fragment):
    envelope = request(34, store)
    envelope.document.remove(envelope.document.find("UI"))
    envelope.document.append(ET.fromstring(fragment))
    with pytest.raises(ProtocolError) as error:
        service.game_param(envelope, store)
    assert error.value.status == 400


@pytest.mark.parametrize("field", ["UI", "UK", "MK"])
def test_method_key_rejects_duplicate_identity_even_when_mk_is_optional(service, store, field):
    envelope = request(31, store, MK="")
    node = ET.fromstring(ET.tostring(envelope.document.find(field)))
    envelope.document.append(node)
    with pytest.raises(ProtocolError) as error:
        service.method_key(envelope, store)
    assert error.value.status == 400


@pytest.mark.parametrize("field", ["VI", "OT", "GPVI", "GPOT"])
def test_platform_exchange_rejects_duplicate_single_value_fields(service, store, field):
    envelope = request(152, store)
    envelope.document.append(ET.fromstring(ET.tostring(envelope.document.find(field))))
    with pytest.raises(ProtocolError) as error:
        service.platform_login(envelope, store)
    assert error.value.status == 400


LAUNCH_MISMATCH = ("invalid_platform_identity", "Platform request does not match the local launch identity", 403)
ALIAS_MISMATCH = ("invalid_platform_identity", "Platform identity aliases disagree", 403)
GAME_FIELDS_PRESENT = ("invalid_platform_identity", "Platform exchange must precede the game identity fields", 400)


def refusal(call):
    with pytest.raises(ProtocolError) as error:
        call()
    return error.value.code, str(error.value), error.value.status


@pytest.mark.parametrize("overrides,expected", [
    ({"VI": "other"}, LAUNCH_MISMATCH), ({"OT": "other"}, LAUNCH_MISMATCH), ({"GPVI": "other"}, ALIAS_MISMATCH),
    ({"GPOT": ""}, ALIAS_MISMATCH), ({"MK": ""}, GAME_FIELDS_PRESENT),
    ({"VI": "other", "UI": ""}, LAUNCH_MISMATCH), ({"GPVI": "other", "UK": ""}, GAME_FIELDS_PRESENT),
], ids=["viewer", "token", "viewer_alias", "empty_token_alias", "game_field", "launch_before_game_fields",
        "game_fields_before_aliases"])
def test_platform_exchange_and_its_identity_helper_refuse_identically(service, store, overrides, expected):
    envelope = request(152, store, **overrides)
    assert refusal(lambda: service.platform_identity(envelope, store)) == expected
    assert refusal(lambda: service.platform_login(envelope, store)) == expected


@pytest.mark.parametrize("field", ["VI", "OT"])
def test_platform_exchange_and_its_identity_helper_require_both_launch_fields(service, store, field):
    envelope = request(152, store)
    envelope.document.remove(envelope.document.find(field))
    expected = ("invalid_identity", f"Expected one {field} field", 400)
    assert refusal(lambda: service.platform_identity(envelope, store)) == expected
    assert refusal(lambda: service.platform_login(envelope, store)) == expected


def test_platform_identity_returns_the_stored_profile_without_writing(service, store):
    saved = store.profile()
    assert service.platform_identity(request(152, store), store) == saved
    assert service.platform_identity(request(152, store, GPVI="unkown", GPOT="unkown"), store) == saved
    assert store.profile() == saved


@pytest.mark.parametrize("overrides,expected", [
    ({"VI": "other"}, LAUNCH_MISMATCH), ({"OT": "other"}, LAUNCH_MISMATCH), ({"GPOT": "other"}, ALIAS_MISMATCH),
    ({"GPVI": "other", "UK": ""}, ALIAS_MISMATCH), ({"VI": "other", "UK": "c" * 32}, LAUNCH_MISMATCH),
], ids=["viewer", "token", "token_alias", "alias_with_empty_user_key", "viewer_with_other_user_key"])
def test_platform_identity_keeping_the_game_identity_checks_only_the_launch_identity(service, store, overrides, expected):
    account = store.profile()
    identity = {"UI": account.player_id, "UK": account.user_key, "MK": account.method_key}
    envelope = request(152, store, **{**identity, **overrides})
    assert refusal(lambda: service.platform_identity(envelope, store, keeps_game_identity=True)) == expected


def test_platform_identity_keeping_the_game_identity_ignores_its_values(service, store):
    account = store.profile()
    for identity in ({}, {"UI": "", "UK": "", "MK": ""}, {"UI": "", "UK": "", "MK": account.method_key},
                     {"UI": "2", "UK": "c" * 32, "MK": "c" * 32},
                     {"UI": account.player_id, "UK": account.user_key, "MK": account.method_key}):
        assert service.platform_identity(request(152, store, **identity), store, keeps_game_identity=True) == account
    assert store.profile() == account


ZERO_PROGRESS = ("invalid_player_state", "The native GameParam loader treats zero money and experience as a new account", 503)
INVALID_PROGRESSION = ("invalid_player_state", "Player progression is not a valid native GameParam state", 503)
RANK_MISMATCH = ("invalid_player_rank", "Player rank and experience disagree with RankUp.atb", 503)
CLOCK_SOURCE_REQUIRED = ("energy_clock_source_required", "The saved energy clock requires an explicit source timezone", 503)


def test_player_row_reads_the_saved_game_param_row_like_the_private_reader(service, store):
    profile = store.profile()
    row = service.player_row(profile.state)
    assert row == profile.state["tables"]["game_param"][0] == service._player_row(profile)
    assert (row["Money"], row["Experience"], row["Rank"]) == (1000, 0, 1)


@pytest.mark.parametrize("corrupt,expected", [
    (lambda state: state["tables"]["game_param"][0].update(Money=0, Experience=0), ZERO_PROGRESS),
    (lambda state: state["tables"]["game_param"][0].update(Money=-1), INVALID_PROGRESSION),
    (lambda state: state["tables"]["game_param"][0].update(Rank=2), RANK_MISMATCH),
    (lambda state: state.pop("energy_clock"), CLOCK_SOURCE_REQUIRED),
], ids=["zero_money_and_experience", "negative_money", "rank_ahead_of_experience", "missing_energy_clock"])
def test_player_row_refuses_like_the_private_reader(service, store, corrupt, expected):
    profile = store.profile()
    corrupt(profile.state)
    assert refusal(lambda: service.player_row(profile.state)) == refusal(lambda: service._player_row(profile)) == expected


def test_quest_record_retains_its_verified_plaintext_identity_contract(asset_root, store):
    service = BootstrapService(asset_root, master_provider=lambda name: quest_master())
    envelope = request("quest_record", store)
    assert not envelope.document.find("UI").attrib
    assert loads(service.quest_record(envelope, store)).children[0].values == (1001, 1002)
    envelope.document.find("UI").set("T", "S")
    with pytest.raises(ProtocolError) as error:
        service.quest_record(envelope, store)
    assert error.value.status == 400


def test_session_flags_are_a_bounded_integer_pair_for_the_local_identity(service, store):
    envelope = request("session_flags", store)
    response = service.session_flags(envelope, store)
    assert response == b"0,0"
    assert encode_response(envelope.profile, response)[0] == response
    with pytest.raises(ProtocolError) as error:
        service.session_flags(request("session_flags", store, UI="other"), store)
    assert error.value.status == 403
    envelope.document.set("T", "S")
    with pytest.raises(ProtocolError):
        service.session_flags(envelope, store)


SOUND_OPTIONS = {"A2": 0, "A3": 1, "C2": 37, "C3": 72, "F9": 1, "FA": 0, "FD": 1}


@pytest.mark.parametrize("flags", [0, 1, 3, 7, 0x40000000, -0x80000000, 0x7FFFFFFF])
def test_configuration_save_replaces_complete_mask_and_preserves_existing_state(service, store, flags):
    prior = store.profile()
    prior.state["tables"]["config"][0].update(MenuTutorialFlags=7, UserName="Existing Player", HaveUnitMaxSizeBuy=8)
    prior.state["player_state"] = {"units": [{"GetIndex": 1}], "squads": [{"UnitIndex": 1}]}
    prior = store.update_state(prior.state, prior.revision)
    expected = deepcopy(prior.state)
    expected["tables"]["config"][0]["MenuTutorialFlags"] = flags
    result = invoke(service, store, 22, A4=flags)
    assert store.profile().state == expected
    assert store.profile().revision == prior.revision + 1
    assert [child.name for child in result.children] == ["SYSTEM", "PGC"]
    delta = result.children[1]
    assert [child.type.value for child in delta.children] == ["S", "S"]
    assert dict(zip(delta.children[0].values, delta.children[1].values)) == {
        "A4": str(flags), "C2": "0", "C3": "0", "F9": "0", "FA": "0", "FD": "0",
    }
    reopened = Store(store.path)
    assert columns(invoke(service, reopened, 36))["A4"].values == (flags,)


def test_configuration_sound_options_persist_boolean_types_and_preserve_tutorial_flags(service, store):
    invoke(service, store, 22, A4=0x40000007)
    result = invoke(service, store, 21, **SOUND_OPTIONS)
    row = configuration_row(store.profile().state)
    assert row["UseBgm"] is False and row["UseSe"] is True
    assert row["MenuTutorialFlags"] == 0x40000007
    reloaded = columns(invoke(service, Store(store.path), 36))
    assert reloaded["A2"].type.value == reloaded["A3"].type.value == "B"
    assert reloaded["A2"].values == (False,) and reloaded["A3"].values == (True,)
    for name in ("C2", "C3", "F9", "FA", "FD"):
        assert reloaded[name].values == (SOUND_OPTIONS[name],)
    delta = result.children[1]
    assert not {"A2", "A3"} & set(delta.children[0].values)


def test_configuration_variants_share_native_wire_dispatch(tmp_path, service):
    handlers = service.handlers()
    assert handlers[21] is handlers[22]
    app = create_app(tmp_path / "config-http", handlers=handlers, initial_state=service.initial_state())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        profiles = []
        for number, fields in ((22, {"A4": 5}), (21, SOUND_OPTIONS)):
            envelope = request(number, app.state.store, CT=0, PFN="dmm-r18", PlatformCode=1,
                               GPVI="unkown", GPOT="unkown", **fields)
            profiles.append(envelope.profile)
            wire = encode_wire(prepare_request(envelope.xml, envelope.profile.st), envelope.profile.request_key)
            response = client.post("/api/" + envelope.profile.route, content=wire)
            assert response.status_code == 200
            assert loads(decode_wire(response.content, envelope.profile.response_key)).children[1].name == "PGC"
        assert profiles[0] == profiles[1]
        row = configuration_row(app.state.store.profile().state)
        assert row["MenuTutorialFlags"] == 5 and row["UseBgm"] is False


@pytest.mark.parametrize("fields", [
    {}, {"A4": 1, "A5": 20}, {"A4": 1, **SOUND_OPTIONS}, {"C2": 5},
    {"A4": "1"}, {"A4": 0x80000000}, {"A4": -0x80000001},
    {**SOUND_OPTIONS, "A2": 2}, {**SOUND_OPTIONS, "A3": -1},
])
def test_configuration_invalid_shapes_and_values_leave_storage_unchanged(service, store, fields):
    prior = store.profile()
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, 22, **fields)
    assert error.value.status == 400
    assert store.profile() == prior


@pytest.mark.parametrize("fragment", [
    '<A4 T="I"><V>1</V><V>2</V></A4>', '<A4 T="I"><V><N>1</N></V></A4>',
    '<A4 T="B"><V>true</V></A4>', '<A4 T="I"><V extra="x">1</V></A4>',
    '<A4 T="I">extra<V>1</V></A4>', '<A4 T="I"><V>1</V>extra</A4>',
    '<A4 T="I"><Value>1</Value></A4>', '<A4 T="I"><V></V></A4>',
])
def test_configuration_malformed_field_does_not_write(service, store, fragment):
    prior = store.profile()
    envelope = request(22, store)
    envelope.document.append(ET.fromstring(fragment))
    with pytest.raises(ProtocolError) as error:
        service.save_configuration(envelope, store)
    assert error.value.status == 400
    assert store.profile() == prior


def test_configuration_duplicate_field_does_not_write(service, store):
    prior = store.profile()
    envelope = request(22, store, A4=1)
    envelope.document.append(deepcopy(envelope.document.find("A4")))
    with pytest.raises(ProtocolError):
        service.save_configuration(envelope, store)
    assert store.profile() == prior


@pytest.mark.parametrize("identity", ["UI", "UK", "MK"])
def test_configuration_save_requires_account_credentials(service, store, identity):
    prior = store.profile()
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, 22, A4=1, **{identity: "incorrect"})
    assert error.value.status == 403
    assert store.profile() == prior


def test_configuration_retry_preserves_concurrent_options_and_player_state(service, store, monkeypatch):
    original = store.update_state
    attempts = 0

    def update(state, revision):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            concurrent = store.profile()
            concurrent.state["tables"]["config"][0]["_VolumeBgm"] = 85
            concurrent.state["tables"]["game_param"][0]["Money"] = 5432
            original(concurrent.state, concurrent.revision)
        return original(state, revision)

    monkeypatch.setattr(store, "update_state", update)
    result = invoke(service, store, 22, A4=7)
    assert attempts == 2
    state = store.profile().state
    assert state["tables"]["config"][0]["_VolumeBgm"] == 85
    assert state["tables"]["config"][0]["MenuTutorialFlags"] == 7
    assert state["tables"]["game_param"][0]["Money"] == 5432
    assert dict(zip(*(column.values for column in result.children[1].children)))["C2"] == "85"


def test_configuration_conflict_exhaustion_does_not_claim_success(service, store, monkeypatch):
    prior = store.profile()
    calls = []

    def conflict(state, revision):
        calls.append(revision)
        raise ValueError("revision changed")

    monkeypatch.setattr(store, "update_state", conflict)
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, 22, A4=7)
    assert error.value.status == 409 and len(calls) == 3
    assert store.profile() == prior


def test_configuration_encoding_failure_precedes_commit(service, store, monkeypatch):
    prior = store.profile()

    def fail(*args):
        raise ProtocolError("response_too_large", "Cannot encode response")

    monkeypatch.setattr("aigis_ps.bootstrap.encode_response", fail)
    with pytest.raises(ProtocolError):
        invoke(service, store, 22, A4=7)
    assert store.profile() == prior


@pytest.mark.parametrize("row", [None, [], [{}], [{"MenuTutorialFlags": True}]])
def test_configuration_missing_legacy_row_is_not_reset(service, store, row):
    prior = store.profile()
    prior.state["tables"]["config"] = row
    prior = store.update_state(prior.state, prior.revision)
    with pytest.raises(ProtocolError) as error:
        invoke(service, store, 22, A4=7)
    assert error.value.status == 503
    assert store.profile() == prior


def test_configuration_patch_isolated_copy_and_signed_flags(service):
    state = service.initial_state()
    state["tables"]["config"][0]["MenuTutorialFlags"] = -0x80000000
    prior = deepcopy(state)
    updated = patch_configuration(state, {"MenuTutorialFlags": configuration_row(state)["MenuTutorialFlags"] | 6})
    assert state == prior
    assert configuration_row(updated)["MenuTutorialFlags"] == -0x80000000 + 6
    delta = configuration_delta(configuration_row(updated), ("MenuTutorialFlags",))
    assert delta.children[0].values == ("A4",) and delta.children[1].values == (str(-0x80000000 + 6),)
    updated["tables"]["game_param"][0]["Money"] = 99
    assert state == prior


@pytest.mark.parametrize("updates", [{"MenuTutorialFlags": True}, {"MenuTutorialFlags": 0x80000000}, {"UseBgm": 1}, {"UserName": "overwrite"}])
def test_configuration_patch_rejects_invalid_or_unverified_fields(service, updates):
    state = service.initial_state()
    prior = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        patch_configuration(state, updates)
    assert error.value.status == 503
    assert state == prior
