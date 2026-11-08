from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from aigis_ps import cli
from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.barracks_service import EXTRA_SHIFT, PURCHASE_FIELD
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.protocol import encode_wire
from test_evolution_integration import post
from test_quest_service import bundle, request
from test_training_service import columns


def initial(bundle, count=1):
    state = bundle.initialize(bundle.bootstrap.initial_state())
    cards = bundle.gifts.catalog
    card_id = next(identifier for identifier in cards.card_ids
        if cards.card(identifier)["Kind"] == 1 and cards.card(identifier)["Rare"] == 3
        and cards.card(identifier)["BonusType"] and cards.card(identifier)["BonusType3"]
        and bundle.gifts.aliases.resolve(identifier).image_card_id == identifier)
    return bundle.player_state.credit_units(state, [{"card_id": card_id, "level": 1, "count": count}])


def application(bundle, path, state):
    return create_app(path, initial_state=bundle.initialize(state), state_initializer=bundle.initialize,
        handlers=bundle.bootstrap.handlers() | bundle.player_state.handlers() | bundle.gifts.handlers()
        | bundle.guide.handlers() | bundle.barracks.handlers() | bundle.training.handlers()
        | bundle.sales.handlers())


def guide(store, index, tick=1000):
    return request(store, 121, (Column("A7", "I", (index,)),), tick=tick)


def give(store, target):
    fields = [Container("BASE", (Column("A7", "I", (target["GetIndex"],)),
        Column("AB", "I", (target["UniqueID"],)), Column("ALTERNATIVE", "I", (0,)))), Column("ITEM", "I", (0,))]
    for index in range(4):
        fields.extend((Column(f"ITEM{index}", "S" if index == 0 else "I", ("gift_0003_diamond" if index == 0 else 0,)),
                       Column(f"NUM{index}", "I", (20 if index == 0 else 0,))))
    return request(store, 100, fields)


def move(store, target, destination, tick=1000):
    return request(store, 118, (Column("DEST", "I", (destination,)),
        Container("BASE", (Column("A7", "I", tuple(unit["GetIndex"] for unit in target)),
                           Column("AB", "I", tuple(unit["UniqueID"] for unit in target))))), tick=tick)


def rejected(client, envelope):
    response = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
    assert 400 <= response.status_code < 600, response.text


def test_gifts_unlock_guide_selection_and_cancel_survives_restart(bundle, tmp_path):
    assert bundle.guide is not None, bundle.guide_error
    state, units = initial(bundle)
    target, = units
    state = bundle.possessions.credit(state, {"gift_0003_diamond": 20})
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        rejected(client, guide(app.state.store, target["GetIndex"]))
        assert app.state.store.profile() == before
        post(client, give(app.state.store, target))
        before = app.state.store.profile()
        assert before.state["player_state"]["units"][1]["Love"] == 100
        _, response = post(client, guide(app.state.store, target["GetIndex"]))
        assert set(response) == {"SYSTEM", "PGC"}
        assert response["PGC"].children[0].values == ("C1",)
        assert response["PGC"].children[1].values == (str(target["GetIndex"]),)
        expected = deepcopy(before.state)
        expected["tables"]["config"][0]["_GuideUnitIndex"] = target["GetIndex"]
        chosen = app.state.store.profile()
        assert chosen.state == expected
        post(client, guide(app.state.store, target["GetIndex"]))
        assert app.state.store.profile() == chosen
    reopened = application(bundle, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        _, response = post(client, request(reopened.state.store, 36, ()))
        assert response["C1"].values == (target["GetIndex"],)
        post(client, guide(reopened.state.store, 0))
        cancelled = reopened.state.store.profile()
        assert cancelled.state["tables"]["config"][0]["_GuideUnitIndex"] == 0
        post(client, guide(reopened.state.store, 0))
        assert reopened.state.store.profile() == cancelled


def test_sold_guide_keeps_native_fallback_reference_and_new_unit_cannot_reuse_it(bundle, tmp_path):
    state, units = initial(bundle)
    target, = units
    state["player_state"]["units"][1]["Love"] = 100
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, guide(app.state.store, target["GetIndex"]))
        post(client, request(app.state.store, 3, (Container("APPEND", columns(units)),)))
        saved = app.state.store.profile()
        assert len(saved.state["player_state"]["units"]) == 1
        assert saved.state["tables"]["config"][0]["_GuideUnitIndex"] == target["GetIndex"]
        _, response = post(client, request(app.state.store, 36, ()))
        assert response["C1"].values == (target["GetIndex"],)
        rejected(client, guide(app.state.store, target["GetIndex"]))
        assert app.state.store.profile() == saved
        granted, new_units = bundle.player_state.credit_units(saved.state,
            [{"card_id": target["CardID"], "level": 1, "count": 1}])
        assert new_units[0]["GetIndex"] > target["GetIndex"]
        assert new_units[0]["UniqueID"] > target["UniqueID"]
        assert granted["tables"]["config"][0]["_GuideUnitIndex"] == target["GetIndex"]


def test_barracks_purchase_transfer_and_return_preserve_flags_and_wallet(bundle, tmp_path):
    assert bundle.barracks is not None, bundle.barracks_error
    state, units = initial(bundle, 2)
    state["tables"]["game_param"][0].update(Rank=25,
        Experience=sum(row["NextExp"] for row in bundle.bootstrap.rank_rows[:24]))
    state["tables"]["config"][0][PURCHASE_FIELD] = 7
    state["tables"]["crystals"] = [
        {"FreeFlag": False, "Num": 30, "Date": 100}, {"FreeFlag": True, "Num": 12, "Date": 200},
        {"FreeFlag": False, "Num": 20, "Date": 80}, {"FreeFlag": True, "Num": 8, "Date": 150}]
    state["player_state"]["units"][1]["GeneralPurposeFlags"] = -0x80000000 + 7
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        buying = request(app.state.store, 17, ())
        wire, response = post(client, buying)
        bought = app.state.store.profile()
        assert set(response) == {"SYSTEM", "PPC", "PFC", "PGC", "PGP2"}
        assert wallet_totals(bought.state) == {"free": 0, "paid": 40, "total": 40}
        assert [row["Num"] for row in bought.state["tables"]["crystals"]] == [30, 0, 10, 0]
        assert bought.state["tables"]["config"][0][PURCHASE_FIELD] == (1 << EXTRA_SHIFT) | 7
        assert post(client, buying)[0] == wire and app.state.store.profile() == bought
        post(client, request(app.state.store, 17, (), tick=1001))
        assert wallet_totals(app.state.store.profile().state)["total"] == 10
        before = app.state.store.profile()
        post(client, request(app.state.store, 17, (), tick=1002), 409)
        assert app.state.store.profile() == before
        _, response = post(client, move(app.state.store, units, 2, tick=2000))
        moved = app.state.store.profile()
        assert len(response["PPU"].children) == 21
        assert bundle.unit_capacity.usage(moved.state).counts == (1, 0, 2)
        assert moved.state["player_state"]["units"][1]["GeneralPurposeFlags"] == -0x80000000 + 0x27
        assert moved.state["player_state"]["last_unique_id"] == before.state["player_state"]["last_unique_id"]
        assert moved.state["player_state"]["battle"] == before.state["player_state"]["battle"]
        assert moved.state["tables"] == before.state["tables"]
        post(client, move(app.state.store, units, 0, tick=2001))
        returned = app.state.store.profile()
        assert returned.state["player_state"] == before.state["player_state"]
    reopened = application(bundle, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        _, response = post(client, request(reopened.state.store, 36, ()))
        assert response["A5"].values == ((2 << EXTRA_SHIFT) | 7,)
        assert reopened.state.store.profile() == returned


def test_transferred_units_reject_old_training_sale_and_squad_requests(bundle, tmp_path):
    state, units = initial(bundle, 2)
    state["tables"]["config"][0][PURCHASE_FIELD] = 1 << EXTRA_SHIFT
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        sale = request(app.state.store, 3, (Container("APPEND", columns(units[1:])),))
        training = request(app.state.store, 5, (Container("BASE", columns(units[:1])),
            Container("APPEND", columns(units[1:])), Container("CMB", (
                Column("A1", "I", (0,)), Column("A7", "I", (0,)), Column("B0", "I", (0,))))))
        squad = request(app.state.store, 73, (Column("A0", "I", (0,)),
            Column("A1", "I", (units[1]["GetIndex"],)), Column("A3", "S", ("Stored",)), Column("A4", "I", (0,))))
        post(client, move(app.state.store, units[1:], 1))
        before = app.state.store.profile()
        for envelope, status in ((sale, 501), (training, 409), (squad, 409)):
            post(client, envelope, status)
            assert app.state.store.profile() == before
        post(client, move(app.state.store, units[1:], 0, tick=1001))
        post(client, squad)
        assert app.state.store.profile().state["player_state"]["squads"][0]["UnitIndex"] == units[1]["GetIndex"]


@pytest.mark.parametrize("registration", [21, 22])
@pytest.mark.parametrize("alias", ["A5", "C1"])
def test_general_configuration_cannot_bypass_guide_or_barracks_validation(bundle, tmp_path, registration, alias):
    state, _ = initial(bundle)
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        post(client, request(app.state.store, registration, (Column(alias, "I", (123,)),)), 400)
        assert app.state.store.profile() == before


def test_cli_wires_home_features_without_listener(bundle, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "services", lambda _: bundle)
    monkeypatch.setattr(cli, "client_fingerprint", lambda _: bundle.masters.expected_hash)
    monkeypatch.setattr("aigis_ps.app.create_app", lambda *args, **kwargs: captured.update(kwargs))
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    assert cli.main(["serve", "--data-dir", str(tmp_path / "unused")]) == 0
    assert captured["handlers"][121] == bundle.guide.handlers()[121]
    assert {key: captured["handlers"][key] for key in (17, 118)} == bundle.barracks.handlers()
    assert not (tmp_path / "unused").exists()
