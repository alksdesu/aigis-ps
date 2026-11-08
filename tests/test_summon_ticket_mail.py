from copy import copy, deepcopy

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.cli import load_ticket_rules
from aigis_ps.errors import ProtocolError
from aigis_ps.summon_tickets import SummonTicketService
from test_evolution_integration import post
from test_quest_service import bundle, request


@pytest.fixture
def setup(bundle):
    bootstrap = copy(bundle.bootstrap)
    presents = copy(bundle.presents)
    draws = []

    def random_value(bound):
        draws.append(bound)
        return 0

    tickets = SummonTicketService(bootstrap, bundle.player_state, presents, bundle.summon_pools,
                                 randbelow=random_value)
    presents.bootstrap, presents.tickets = bootstrap, tickets
    bootstrap.present_provider = presents.response
    state = bundle.initialize(bootstrap.initial_state())
    return bootstrap, presents, tickets, state, draws


def reward(count=1, pool=350):
    return {"kind": "summon_ticket", "pool_id": pool, "count": count, "description": "ログインボーナス"}


def claim(store, identifiers, *, number=1, tick=1000):
    return request(store, number, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", tuple(identifiers)),)),)),), tick=tick)


def test_ticket_count_is_split_into_individual_mails_with_native_pool_column(setup):
    _, presents, _, state, _ = setup
    before = deepcopy(state)
    updated, added = presents.enqueue(state, [reward(2), {"kind": "free_crystal", "amount": 3, "description": "贈り物"}])
    assert state == before
    assert [row["Type"] for row in added] == [16, 16, 0]
    assert [row["Content"] for row in added] == [1, 1, 3]
    columns = {column.name: column.values for column in presents.response(updated).children}
    assert columns["A4"] == (350, 350, 0)
    assert columns["A1"] == (16, 16, 0)
    assert columns["A2"] == (1, 1, 3)


@pytest.mark.parametrize("mutation", [
    {"count": 0}, {"count": True}, {"count": -1}, {"pool_id": True}, {"pool_id": -1},
    {"extra": 1}, {"pool_id": 1},
])
def test_invalid_or_disabled_ticket_grants_leave_state_unchanged(setup, mutation):
    _, presents, _, state, _ = setup
    before = deepcopy(state)
    with pytest.raises(ProtocolError):
        presents.enqueue(state, [{**reward(), **mutation}])
    assert state == before


def test_ticket_grant_requires_the_claim_service(setup):
    _, presents, _, state, _ = setup
    presents.tickets = None
    with pytest.raises(ProtocolError) as error:
        presents.enqueue(state, [reward()])
    assert error.value.code == "summon_ticket_service_unavailable"


def test_ticket_cannot_be_consumed_by_generic_nontransactional_claim(setup):
    _, presents, _, state, _ = setup
    updated, added = presents.enqueue(state, [reward()])
    before = deepcopy(updated)
    with pytest.raises(ProtocolError) as error:
        presents.claim(updated, [added[0]["PresentID"]], acquisition_day=739000)
    assert error.value.code == "present_ticket_transaction_required"
    assert updated == before


def test_native_ticket_claim_mail_reload_and_restart_do_not_redraw(setup, bundle, tmp_path):
    bootstrap, presents, _, state, draws = setup
    state, mail = presents.enqueue(state, [reward(2)])

    def application():
        return create_app(tmp_path, initial_state=state, state_initializer=bundle.initialize,
                          handlers=bootstrap.handlers() | presents.handlers())

    app = application()
    original = app.state.store.profile()
    envelope = claim(app.state.store, [mail[0]["PresentID"]])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        wire, response = post(client, envelope)
        assert set(response) == {"SYSTEM", "PBP", "PPU"}
        unit = {column.name: column.values for column in response["PPU"].children}
        assert len(unit) == 21 and unit["A1"] == (8,)
        assert unit["A4"] == (bundle.player_state.experience_for_level(8, 30),)
        saved = app.state.store.profile()
        assert len(saved.state["player_state"]["units"]) == len(original.state["player_state"]["units"]) + 1
        for table in ("crystals", "possessions", "game_param", "card_status"):
            assert saved.state["tables"][table] == original.state["tables"][table]
        assert saved.state["tables"]["presents"] == [mail[1]]
        assert len(draws) == 1
        _, remaining = post(client, request(app.state.store, 42, ()))
        assert {column.name: column.values for column in remaining["PPT"].children}["PresentID"] == (mail[1]["PresentID"],)
        assert post(client, envelope)[0] == wire
        assert app.state.store.profile() == saved and len(draws) == 1
    reopened = application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == saved and len(draws) == 1


def test_single_claim_mixing_a_ticket_is_rejected_without_consuming_other_rewards(setup, tmp_path):
    bootstrap, presents, _, state, draws = setup
    state, mail = presents.enqueue(state, [reward(), {"kind": "free_crystal", "amount": 3, "description": "贈り物"}])
    app = create_app(tmp_path, initial_state=state, handlers=bootstrap.handlers() | presents.handlers())
    before = app.state.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = post(client, claim(app.state.store, [row["PresentID"] for row in mail]), 501)
        assert response.json()["error"]["code"] == "summon_ticket_single_required"
    assert app.state.store.profile() == before and not draws


@pytest.mark.parametrize("mixed", [False, True])
def test_batch_keeps_tickets_for_their_single_claim_and_claims_other_rewards(setup, tmp_path, mixed):
    bootstrap, presents, _, state, draws = setup
    state, mail = presents.enqueue(state, [reward(), {"kind": "free_crystal", "amount": 3, "description": "贈り物"}])
    app = create_app(tmp_path, initial_state=state, handlers=bootstrap.handlers() | presents.handlers())
    selected = [row["PresentID"] for row in mail] if mixed else [mail[0]["PresentID"]]
    before = app.state.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        wire, response = post(client, claim(app.state.store, selected, number=117))
        saved = app.state.store.profile()
        assert saved.state["tables"]["presents"] == (mail[:1] if mixed else mail) and not draws
        assert not response["PPU"].children[0].values
        if mixed:
            assert set(response) == {"SYSTEM", "PPU", "PFC"} and saved.revision == before.revision + 1
            assert sum(row["Num"] for row in saved.state["tables"]["crystals"]) == 3
        else:
            assert set(response) == {"SYSTEM", "PPU"} and saved == before
        assert post(client, claim(app.state.store, selected, number=117))[0] == wire
        assert app.state.store.profile() == saved and not draws
        _, single = post(client, claim(app.state.store, [mail[0]["PresentID"]], tick=1001))
        assert set(single) == {"SYSTEM", "PBP", "PPU"} and len(draws) == 1
        assert app.state.store.profile().state["tables"]["presents"] == ([] if mixed else mail[1:])


def test_ordinary_claim_with_ticket_in_mailbox_keeps_its_previous_behavior(setup, tmp_path):
    bootstrap, presents, _, state, draws = setup
    state, mail = presents.enqueue(state, [reward(), {"kind": "free_crystal", "amount": 3, "description": "贈り物"}])
    app = create_app(tmp_path, initial_state=state, handlers=bootstrap.handlers() | presents.handlers())
    envelope = claim(app.state.store, [mail[1]["PresentID"]])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        wire, response = post(client, envelope)
        assert "PFC" in response
        assert not response["PPU"].children[0].values
        saved = app.state.store.profile()
        assert saved.state["tables"]["presents"] == [mail[0]]
        assert sum(row["Num"] for row in saved.state["tables"]["crystals"]) == 3
        assert post(client, envelope)[0] == wire
        assert app.state.store.profile() == saved and not draws


def test_known_disabled_pool_remains_readable_but_cannot_be_claimed(setup, tmp_path):
    bootstrap, presents, _, state, draws = setup
    state, mail = presents.enqueue(state, [reward()])
    state["tables"]["presents"][0]["SummonID"] = 1
    assert {c.name: c.values for c in presents.response(state).children}["A4"] == (1,)
    app = create_app(tmp_path, initial_state=state, handlers=bootstrap.handlers() | presents.handlers())
    before = app.state.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        result = post(client, claim(app.state.store, [mail[0]["PresentID"]]), 501)
        assert result.json()["error"]["code"] == "summon_ticket_pool_disabled"
    assert app.state.store.profile() == before and not draws


def test_cli_ticket_rules_load_explicit_pool_configuration(tmp_path):
    assert load_ticket_rules(None).enabled_pools == (350,)
    path = tmp_path / "tickets.json"
    path.write_text('{"enabled_pools":[350]}', encoding="utf-8")
    assert load_ticket_rules(path).enabled_pools == (350,)
    path.write_text('{"enabled_pools":[]}', encoding="utf-8")
    assert load_ticket_rules(path).enabled_pools == ()
    for source in ('{"enabled_pools":[true]}', '{"enabled_pools":[350,350]}',
                   '{"enabled_pools":[-1]}', '{"unknown":1}'):
        path.write_text(source, encoding="utf-8")
        with pytest.raises(ValueError):
            load_ticket_rules(path)

