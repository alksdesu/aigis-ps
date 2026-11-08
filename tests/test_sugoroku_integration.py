from copy import copy, deepcopy
from datetime import datetime
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.app import create_app
from aigis_ps.crystal_spending import read_spending
from aigis_ps.energy_clock import JST
from aigis_ps.protocol import encode_wire, decode_wire, prepare_request
from test_quest_service import bundle, request
from test_sugoroku_service import factory, action


def application(case, directory, state):
    presents = copy(case.bundle.presents)
    presents.bootstrap = case.bootstrap
    case.bootstrap.present_provider = presents.response
    return create_app(directory, initial_state=state, state_initializer=case.bundle.initialize,
        handlers=case.bootstrap.handlers() | case.service.handlers() | presents.handlers()
                 | case.bundle.player_state.handlers())


def post(client, envelope, status=200, *, xml=None):
    response = client.post("/api/" + envelope.profile.route,
        content=encode_wire(envelope.xml if xml is None else xml, envelope.profile.request_key))
    assert response.status_code == status, response.content
    if status != 200:
        return response.json()["error"]
    decoded = loads(decode_wire(response.content, envelope.profile.response_key))
    return response.content, {node.name: node for node in decoded.children}


def state_rows(group):
    return dict(zip(*(column.values for column in group.children)))


def claim_mail(store, identifiers, tick):
    return request(store, 1, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", tuple(identifiers)),)),)),), tick=tick)


@pytest.mark.parametrize("reward_id", [2, 3, 42], ids=["unit", "item", "gold"])
def test_reward_board_to_mail_to_actual_reward_and_reopen(factory, tmp_path, reward_id):
    case = factory()
    directory = tmp_path / "http"
    initial = case.store.profile().state
    app = application(case, directory, initial)
    case.store = app.state.store
    definition = case.service.catalog.get(reward_id)
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, loaded = post(client, request(case.store, 62, ()))
        assert state_rows(loaded["REWARD"])[reward_id] == 0
        _, marked = post(client, action(case, 64, (reward_id,), tick=1000))
        assert state_rows(marked["REWARD"]) == {reward_id: 1}
        envelope = action(case, 63, (reward_id,), tick=1001)
        wire, claimed = post(client, envelope)
        assert state_rows(claimed["REWARD"]) == {reward_id: 2}
        assert set(claimed) == {"SYSTEM", "REWARD", "PPT"}
        saved = case.store.profile()
        assert saved.state["player_state"] == before.state["player_state"]
        assert saved.state["tables"]["game_param"] == before.state["tables"]["game_param"]
        assert saved.state["tables"]["possessions"] == before.state["tables"]["possessions"]
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved
        _, listing = post(client, request(case.store, 42, ()))
        mail_ids = next(column.values for column in listing["PPT"].children if column.name == "PresentID")
        assert len(mail_ids) == 1
        _, received = post(client, claim_mail(case.store, mail_ids, 1002))
        final = case.store.profile()
        assert final.state["tables"]["presents"] == []
        if definition.reward_type == 2:
            assert set(received) == {"SYSTEM", "PBP", "PPU"}
            unit = final.state["player_state"]["units"][-1]
            assert unit["CardID"] == definition.card_id
            assert len(final.state["player_state"]["units"]) == len(before.state["player_state"]["units"]) + 1
        elif definition.reward_type == 1:
            assert set(received) == {"SYSTEM", "POSSESSION", "PPU"}
            counts = {row["ItemID"]: row["Count"] for row in final.state["tables"]["possessions"]}
            assert counts[definition.item_id] == definition.count
            assert final.state["player_state"] == before.state["player_state"]
        else:
            assert set(received) == {"SYSTEM", "PGP", "PPU"}
            assert final.state["tables"]["game_param"][0]["Money"] == before.state["tables"]["game_param"][0]["Money"] + definition.count
            assert final.state["player_state"] == before.state["player_state"]
        _, listing = post(client, request(case.store, 42, ()))
        assert all(not column.values for column in listing["PPT"].children)
    saved = case.store.profile()
    reopened = application(case, directory, initial)
    case.store = reopened.state.store
    assert case.store.profile() == saved
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        _, loaded = post(client, request(case.store, 62, ()))
        assert state_rows(loaded["REWARD"])[reward_id] == 2
        _, listing = post(client, request(case.store, 42, ()))
        assert all(not column.values for column in listing["PPT"].children)


@pytest.mark.parametrize("registration", [33, 35])
def test_real_protocol_month_refresh_precedes_empty_board(factory, tmp_path, registration):
    case = factory(now=datetime(2026, 11, 30, 12, tzinfo=JST))
    app = application(case, tmp_path / "http", case.store.profile().state)
    case.store = app.state.store
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, action(case, 63, (2,)))
        mail = deepcopy(case.store.profile().state["tables"]["presents"])
        case.clock.now = datetime(2026, 12, 1, 4, tzinfo=JST)
        before = case.store.profile()
        assert post(client, request(case.store, 62, ()), 409)["code"] == "sugoroku_refresh_required"
        assert case.store.profile() == before
        _, refreshed = post(client, request(case.store, registration, ()))
        spending = read_spending(case.store.profile().state)
        assert (spending["period"], spending["current_spent"], spending["previous_spent"]) == ("2026-12", 0, 800)
        if registration == 35:
            assert refreshed["C1"].values == (800 << 16,)
        else:
            assert str(800 << 16) in refreshed["PGP2"].children[1].values
        _, loaded = post(client, request(case.store, 62, ()))
        assert set(loaded) == {"SYSTEM", "REWARD"}
        assert state_rows(loaded["REWARD"]) == {}
        assert case.store.profile().state["tables"]["presents"] == mail


@pytest.mark.parametrize("operation", [63, 64])
@pytest.mark.parametrize("explicit_empty", [False, True])
def test_empty_reward_vector_stays_empty_on_wire(factory, tmp_path, operation, explicit_empty):
    case = factory()
    app = application(case, tmp_path / "http", case.store.profile().state)
    case.store = app.state.store
    group = Container("REWARD", (Column("ID", "I", ()),) if explicit_empty else ())
    envelope = request(case.store, operation, (group,))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        wire, result = post(client, envelope)
        assert set(result) == {"SYSTEM", "REWARD"}
        assert state_rows(result["REWARD"]) == {}
        saved = case.store.profile()
        assert not saved.state["tables"]["presents"]
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved


@pytest.mark.parametrize("field", ["UI", "UK", "MK"])
def test_invalid_authentication_is_atomic_after_actual_wire_decoding(factory, tmp_path, field):
    case = factory()
    app = application(case, tmp_path / "http", case.store.profile().state)
    case.store = app.state.store
    envelope = action(case)
    document = deepcopy(envelope.document)
    document.find(field + "/V").text = "invalid"
    for node in list(document):
        if node.tag in ("VN", "ST", "HS"):
            document.remove(node)
    xml = prepare_request(ET.tostring(document, encoding="utf-8"), envelope.profile.st)
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        error = post(client, envelope, 403, xml=xml)
    assert error["code"] in ("invalid_identity", "invalid_method_key")
    assert case.store.profile() == before


@pytest.mark.parametrize("operation,columns", [
    (62, (Column("NUM", "I", (1,)),)),
    (63, (Column("RewardID", "I", (2,)),)),
    (63, (Container("REWARD", (Column("ID", "S", ("2",)),)),)),
    (64, (Container("REWARD", (Column("ID", "I", (2,)), Column("Count", "I", (1,)))),)),
])
def test_unknown_business_fields_and_types_are_rejected(factory, tmp_path, operation, columns):
    case = factory()
    app = application(case, tmp_path / "http", case.store.profile().state)
    case.store = app.state.store
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        error = post(client, request(case.store, operation, columns), 400)
    assert error["code"] == "invalid_request_fields"
    assert case.store.profile() == before


@pytest.mark.parametrize("identifiers", [(2, 1), (42, 10)])
def test_supported_and_incomplete_ticket_batch_claims_only_supported_rewards(factory, tmp_path, identifiers):
    case = factory()
    directory = tmp_path / "http"
    app = application(case, directory, case.store.profile().state)
    case.store = app.state.store
    supported, incomplete = identifiers
    envelope = action(case, 63, identifiers)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        wire, claimed = post(client, envelope)
        assert state_rows(claimed["REWARD"]) == {supported: 2, incomplete: 0}
        saved = case.store.profile()
        mail = case.bundle.presents.rows(saved.state)
        assert claimed["PPT"].children[0].values == tuple(row["PresentID"] for row in mail) and mail
        assert post(client, envelope)[0] == wire and case.store.profile() == saved
        post(client, action(case, 63, (incomplete,), tick=1001), 501)
        assert case.store.profile() == saved
    reopened = application(case, directory, saved.state)
    case.store = reopened.state.store
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved


def test_content_change_blocks_cached_reply_without_regrant(factory, tmp_path):
    case = factory()
    app = application(case, tmp_path / "http", case.store.profile().state)
    case.store = app.state.store
    envelope = action(case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, envelope)
        before = case.store.profile()
        case.service.catalog = copy(case.service.catalog)
        case.service.catalog._content_version = "0" * 64
        error = post(client, envelope, 503)
    assert error["code"] == "sugoroku_dependencies_changed"
    assert case.store.profile() == before


def test_capacity_reward_has_no_mail_and_becomes_current_once_next_month(factory, tmp_path):
    case = factory()
    app = application(case, tmp_path / "http", case.store.profile().state)
    case.store = app.state.store
    definition = next(entry for entry in case.service.catalog.active(case.clock.now) if entry.reward_type == 5)
    envelope = action(case, 63, (definition.reward_id,))
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        wire, result = post(client, envelope)
        assert set(result) == {"SYSTEM", "REWARD"}
        saved = case.store.profile()
        assert saved.state["tables"]["presents"] == before.state["tables"]["presents"]
        game = saved.state["tables"]["game_param"][0]
        assert game["ExtendedMaxCharismaNextMonth"] == definition.count
        assert game["ExtendedMaxCharisma"] == 0
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved
        case.clock.now = datetime(2026, 12, 1, 4, tzinfo=JST)
        post(client, request(case.store, 34, ()))
        switched = case.store.profile()
        game = switched.state["tables"]["game_param"][0]
        assert (game["ExtendedMaxCharisma"], game["ExtendedMaxCharismaNextMonth"]) == (definition.count, 0)
        post(client, request(case.store, 34, ()))
        assert case.store.profile() == switched
        assert post(client, envelope, 409)["code"] == "sugoroku_retry_stale"


def test_mixed_capacity_and_gold_request_mails_only_gold(factory, tmp_path):
    case = factory()
    app = application(case, tmp_path / "http", case.store.profile().state)
    case.store = app.state.store
    definition = next(entry for entry in case.service.catalog.active(case.clock.now) if entry.reward_type == 5)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, result = post(client, action(case, 63, (definition.reward_id, 42)))
    assert state_rows(result["REWARD"]) == {definition.reward_id: 2, 42: 2}
    mails = case.store.profile().state["tables"]["presents"]
    assert len(mails) == 1 and mails[0]["Type"] == 2 and mails[0]["Content"] == 30000
    assert case.store.profile().state["tables"]["game_param"][0]["ExtendedMaxCharismaNextMonth"] == definition.count
