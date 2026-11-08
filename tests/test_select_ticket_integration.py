from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import UNIT_FIELDS
from aigis_ps.select_ticket_catalog import SelectTicketCatalog
from aigis_ps.select_tickets import SelectTicketService
from test_evolution_integration import post
from test_image_aliases import command_id, command_table, encode
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


@pytest.fixture
def case(bundle, tmp_path):
    assert bundle.selection_tickets is not None, bundle.selection_ticket_error
    bootstrap, players, presents = copy(bundle.bootstrap), copy(bundle.player_state), copy(bundle.presents)
    bootstrap.clock = lambda: NOW
    service = copy(bundle.selection_tickets)
    service.bootstrap, service.players, service.presents = bootstrap, players, presents
    service.grants = copy(service.grants)
    service.grants.players = players
    presents.bootstrap, presents.players, presents.selection_tickets = bootstrap, players, service
    bootstrap.present_provider = presents.response
    initial = bundle.initialize(players.initialize(bootstrap.initial_state()))
    initial["sentinel"] = {"keep": [1]}
    return SimpleNamespace(bundle=bundle, bootstrap=bootstrap, players=players, presents=presents,
                           service=service, initial=initial, directory=tmp_path / "http")


def app_for(case, state=None):
    app = create_app(case.directory, initial_state=case.initial if state is None else state,
        state_initializer=case.bundle.initialize,
        handlers=case.bootstrap.handlers() | case.players.handlers() | case.presents.handlers())
    case.store = app.state.store
    return app


def reward(config_id=1, count=1):
    return {"kind": "selection_ticket", "config_id": config_id, "count": count, "description": "選択チケット"}


def claim(case, identifiers, select_id=1, card_id=None, *, config_id=1, registration=1, tick=1000):
    if card_id is None:
        card_id = case.service.catalog.get(config_id).candidates[select_id - 1].card_id
    return request(case.store, registration, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", tuple(identifiers)), Column("SelectID", "I", (select_id,)),
        Column("SelectCardID", "I", (card_id,)),)),)),), tick=tick)


def columns(node):
    return {column.name: column.values for column in node.children}


def reject(case, client, envelope, status, code=None):
    before = case.store.profile()
    error = post(client, envelope, status).json()["error"]
    if code is not None:
        assert error["code"] == code
    assert case.store.profile() == before


def assert_selected(case, before, result, candidate):
    after = case.store.profile()
    assert set(result) == {"SYSTEM", "PBP", "PPU"}
    assert [column.name for column in result["PPU"].children] == [alias for _, alias in UNIT_FIELDS]
    assert all(len(column.values) == 1 for column in result["PPU"].children)
    old, player = before.state["player_state"], after.state["player_state"]
    added = player["units"][-1]
    assert player["units"][:-1] == old["units"]
    assert added["CardID"] == candidate.card_id
    assert added["GetIndex"] == old["battle"]["UX"] + 1
    assert added["UniqueID"] == old.get("last_unique_id", max(unit["UniqueID"] for unit in old["units"])) + 1
    assert player["battle"]["UX"] == added["GetIndex"]
    assert player["last_unique_id"] == added["UniqueID"]
    assert columns(result["PPU"]) == {alias: (added[name],) for name, alias in UNIT_FIELDS}
    assert result["PBP"].children == (Column("A1", "S", ("UX",)), Column("A2", "I", (added["GetIndex"],)))
    values = case.players.initial_unit_values(candidate.card_id, candidate.level,
                                              evolution_count=candidate.evolution_count)
    assert (added["ClassID"], added["ClassChangeCount"], added["Exp"], added["SettingSkillID"]) == (
        values.class_id, values.stage, values.experience, values.skill_id)
    if candidate.level == 50:
        assert added["Exp"] > 0
    for key in before.state:
        if key not in ("player_state", "tables", "select_ticket_history"):
            assert after.state[key] == before.state[key]
    for key in before.state["tables"]:
        if key != "presents":
            assert after.state["tables"][key] == before.state["tables"][key]
    return after


def test_type20_split_mail_and_a4_coexist_with_type16_and_ordinary_mail(case):
    initial, mail = case.presents.enqueue(case.initial, [reward(148, 2),
        {"kind": "summon_ticket", "pool_id": 350, "count": 1, "description": "random"},
        {"kind": "gold", "amount": 123, "description": "ordinary"}])
    assert len(mail) == 4 and len({row["PresentID"] for row in mail}) == 4
    assert [(row["Type"], row["Content"], row.get("SelectionConfigID")) for row in mail[:2]] == [
        (20, 1, 148), (20, 1, 148)]
    app = app_for(case, initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, result = post(client, request(case.store, 42, ()))
        assert columns(result["PPT"])["A4"] == (148, 148, 350, 0)
        assert columns(result["PPT"])["A1"] == (20, 20, 16, 2)
        before = case.store.profile()
        _, selected = post(client, claim(case, [mail[0]["PresentID"]], config_id=148))
        assert_selected(case, before, selected, case.service.catalog.get(148).candidates[0])
        assert case.store.profile().state["tables"]["presents"] == mail[1:]
        _, result = post(client, request(case.store, 42, ()))
        assert columns(result["PPT"])["A4"] == (148, 350, 0)


@pytest.mark.parametrize("config_id", [1, 2, 148, 190, 202, 223])
@pytest.mark.parametrize("edge", ["first", "last"])
def test_real_first_and_last_candidates_claim_exact_card_and_growth(case, config_id, edge):
    config = case.service.catalog.get(config_id)
    select_id = 1 if edge == "first" else len(config.candidates)
    candidate = config.candidates[select_id - 1]
    initial, mail = case.presents.enqueue(case.initial, [reward(config_id)])
    app = app_for(case, initial)
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = claim(case, [mail[0]["PresentID"]], select_id, config_id=config_id)
        wire, result = post(client, envelope)
        saved = assert_selected(case, before, result, candidate)
        assert not saved.state["tables"]["presents"]
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved
        _, units = post(client, request(case.store, 41, ()))
        assert units["A1"].values[-1] == candidate.card_id
        _, listing = post(client, request(case.store, 42, ()))
        assert all(not column.values for column in listing["PPT"].children)


def test_repeat_same_card_allocates_new_identity_and_no_rainbow_compensation(case):
    initial, mail = case.presents.enqueue(case.initial, [reward(1, 2)])
    app = app_for(case, initial)
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, claim(case, [mail[0]["PresentID"]]))
        post(client, claim(case, [mail[1]["PresentID"]], tick=1001))
        final = case.store.profile().state
        units = final["player_state"]["units"][-2:]
        assert units[0]["CardID"] == units[1]["CardID"] == 17
        assert units[1]["GetIndex"] == units[0]["GetIndex"] + 1
        assert units[1]["UniqueID"] == units[0]["UniqueID"] + 1
        assert final["tables"]["possessions"] == before.state["tables"]["possessions"]
        assert final["tables"]["crystals"] == before.state["tables"]["crystals"]


def test_rebuilt_service_replays_deleted_selection_mail_without_new_grant(case):
    initial, mail = case.presents.enqueue(case.initial, [reward(202)])
    app = app_for(case, initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = claim(case, [mail[0]["PresentID"]], 2, config_id=202)
        wire, _ = post(client, envelope)
        saved = case.store.profile()
    case.service = SelectTicketService(case.bootstrap, case.players, case.presents, case.service.catalog)
    case.presents.selection_tickets = case.service
    app = app_for(case)
    assert case.store.profile() == saved
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved
        alias = claim(case, [mail[0]["PresentID"]], 2, config_id=202, registration=2)
        assert alias.xml == envelope.xml and alias.profile == envelope.profile
        assert post(client, alias)[0] == wire


@pytest.mark.parametrize("shape", ["missing_both", "missing_card", "missing_index", "rt_siblings", "root_siblings",
                                  "string_index", "vector_index", "unknown_field"])
def test_selection_fields_must_be_single_typed_values_inside_pt2(case, shape):
    initial, mail = case.presents.enqueue(case.initial, [reward()])
    app = app_for(case, initial)
    present = Column("PresentID", "I", (mail[0]["PresentID"],))
    select = Column("SelectID", "I", (1,))
    card = Column("SelectCardID", "I", (17,))
    selected = (select, card)
    if shape == "missing_both":
        selected = ()
    elif shape == "missing_card":
        selected = (select,)
    elif shape == "missing_index":
        selected = (card,)
    elif shape == "string_index":
        selected = (Column("SelectID", "S", ("1",)), card)
    elif shape == "vector_index":
        selected = (Column("SelectID", "I", (1, 2)), card)
    elif shape == "unknown_field":
        selected += (Column("Count", "I", (1,)),)
    if shape == "rt_siblings":
        root = (Container("RT", (Container("PT2", (present,)), *selected)),)
    elif shape == "root_siblings":
        root = (Container("RT", (Container("PT2", (present,)),)), *selected)
    else:
        root = (Container("RT", (Container("PT2", (present, *selected)),)),)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(case, client, request(case.store, 1, root), 400)


@pytest.mark.parametrize("select_id,card_id,status", [(0, 17, 400), (-1, 17, 400), (35, 17, 409), (1, 22, 409)])
def test_pair_must_match_one_based_candidate_index(case, select_id, card_id, status):
    initial, mail = case.presents.enqueue(case.initial, [reward()])
    app = app_for(case, initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(case, client, claim(case, [mail[0]["PresentID"]], select_id, card_id), status)


def test_ordinary_present_cannot_accept_selection_parameters(case):
    initial, mail = case.presents.enqueue(case.initial, [{"kind": "gold", "amount": 10, "description": "gold"}])
    app = app_for(case, initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(case, client, claim(case, [mail[0]["PresentID"]]), 400)


def test_mixed_single_claim_cannot_partially_consume_selection_ticket(case):
    initial, mail = case.presents.enqueue(case.initial, [reward(), {"kind": "gold", "amount": 10, "description": "gold"}])
    app = app_for(case, initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(case, client, claim(case, [row["PresentID"] for row in mail]), 501, "select_ticket_single_required")


def batch(case, identifiers):
    return request(case.store, 117, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", tuple(identifiers)),)),)),))


@pytest.mark.parametrize("mixed", [False, True])
def test_batch_keeps_selection_ticket_for_its_single_claim(case, mixed):
    initial, mail = case.presents.enqueue(case.initial, [reward(), {"kind": "gold", "amount": 10, "description": "gold"}])
    app = app_for(case, initial)
    ids = [row["PresentID"] for row in mail] if mixed else [mail[0]["PresentID"]]
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = case.store.profile()
        wire, result = post(client, batch(case, ids))
        saved = case.store.profile()
        assert saved.state["tables"]["presents"] == (mail[:1] if mixed else mail)
        assert saved.state["player_state"] == before.state["player_state"]
        if mixed:
            assert set(result) == {"SYSTEM", "PGP", "PPU"} and saved.revision == before.revision + 1
            assert saved.state["tables"]["game_param"][0]["Money"] == before.state["tables"]["game_param"][0]["Money"] + 10
        else:
            assert set(result) == {"SYSTEM", "PPU"} and saved == before
        assert post(client, batch(case, ids))[0] == wire and case.store.profile() == saved
        _, selected = post(client, claim(case, ids[:1], tick=1001))
        assert_selected(case, saved, selected, case.service.catalog.get(1).candidates[0])


def test_batch_cannot_carry_selection_fields(case):
    initial, mail = case.presents.enqueue(case.initial, [reward(), {"kind": "gold", "amount": 10, "description": "gold"}])
    app = app_for(case, initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(case, client, claim(case, [row["PresentID"] for row in mail], registration=117), 400,
               "invalid_request_fields")


def test_old_multi_count_selection_mail_remains_readable_but_is_not_partially_claimed(case):
    initial, mail = case.presents.enqueue(case.initial, [reward()])
    initial["tables"]["presents"][0]["Content"] = 2
    app = app_for(case, initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, loaded = post(client, request(case.store, 42, ()))
        assert columns(loaded["PPT"])["A2"] == (2,)
        reject(case, client, claim(case, [mail[0]["PresentID"]]), 501, "select_ticket_mail_unsupported")


def test_full_unit_capacity_retains_selection_and_no_receipt(case):
    initial, mail = case.presents.enqueue(case.initial, [reward()])
    case.players.capacity_provider = lambda _: 0
    app = app_for(case, initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(case, client, claim(case, [mail[0]["PresentID"]]), 409, "unit_roster_capacity")


@pytest.mark.parametrize("number", [1, 41, 42])
def test_technical_response_limit_rolls_back_grant_delete_and_receipt(case, number):
    initial, mail = case.presents.enqueue(case.initial, [reward()])
    case.service._profiles = dict(case.service._profiles)
    case.service._profiles[number] = replace(case.service._profiles[number], response_limit=1)
    app = app_for(case, initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(case, client, claim(case, [mail[0]["PresentID"]]), 500, "response_too_large")


def test_cas_retry_keeps_the_explicit_selection_and_concurrent_state(case, monkeypatch):
    initial, mail = case.presents.enqueue(case.initial, [reward(190)])
    app = app_for(case, initial)
    original, attempts = case.store.update_state, []
    def conflict(state, revision):
        attempts.append(revision)
        if len(attempts) == 1:
            current = case.store.profile()
            other = deepcopy(current.state)
            other["sentinel"]["concurrent"] = True
            original(other, current.revision)
            raise ValueError("concurrent write")
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, claim(case, [mail[0]["PresentID"]], 2, config_id=190))
        final = case.store.profile().state
        assert len(attempts) == 2
        assert final["player_state"]["units"][-1]["CardID"] == 2654
        assert len(final["player_state"]["units"]) == len(initial["player_state"]["units"]) + 1
        assert len(final["select_ticket_history"]["receipts"]) == 1
        assert final["sentinel"]["concurrent"] is True


def test_new_rating_resource_preserves_old_mail_but_blocks_only_affected_groups(case):
    original_catalog = case.service.catalog
    other_id = next(identifier for identifier, config in original_catalog.configs.items()
                    if all(candidate.card_id != 17 for candidate in config.candidates))
    initial, mail = case.presents.enqueue(case.initial, [reward(1), reward(other_id)])
    reader = lambda name: case.bundle.native_cdn.assets.read(name)[0]
    raw = reader("OnCommand.atb")
    parsed = parse_table(decompress_all4(raw) if raw.startswith(b"ALL4") else raw)
    rows = [(row["_ID"], row["_Cmnd"]) for row in parsed["rows"]]
    key = command_id("IsCanUseRating_17")
    assert key not in {identifier for identifier, _ in rows}
    replacement = command_table([*rows, (key, encode("0;"))])
    catalog = SelectTicketCatalog(case.bundle.gifts.catalog,
        lambda name: replacement if name == "OnCommand.atb" else reader(name))
    assert catalog.content_version != original_catalog.content_version
    assert catalog.status()["rating_sha256"] != original_catalog.status()["rating_sha256"]
    assert 1 in catalog.status()["unsupported_config_ids"]
    assert other_id not in catalog.status()["unsupported_config_ids"]
    case.service = SelectTicketService(case.bootstrap, case.players, case.presents, catalog)
    case.presents.selection_tickets = case.service
    unchanged = deepcopy(initial)
    with pytest.raises(ProtocolError) as caught:
        case.presents.enqueue(initial, [reward(1)])
    assert (caught.value.code, caught.value.status) == ("select_ticket_rating_unsupported", 501)
    assert initial == unchanged
    _, allowed = case.presents.enqueue(initial, [reward(other_id)])
    assert allowed[0]["SelectionConfigID"] == other_id
    app = app_for(case, initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, listing = post(client, request(case.store, 42, ()))
        assert columns(listing["PPT"])["A4"] == (1, other_id)
        assert columns(listing["PPT"])["PresentID"] == tuple(row["PresentID"] for row in mail)
        reject(case, client, claim(case, [mail[0]["PresentID"]]), 501, "select_ticket_rating_unsupported")
        before = case.store.profile()
        _, result = post(client, claim(case, [mail[1]["PresentID"]], config_id=other_id, tick=1001))
        assert_selected(case, before, result, catalog.get(other_id).candidates[0])
        assert case.store.profile().state["tables"]["presents"] == mail[:1]
        _, listing = post(client, request(case.store, 42, ()))
        assert columns(listing["PPT"])["A4"] == (1,)
