from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.app import create_app
from aigis_ps.protocol import decode_wire, encode_wire
from aigis_ps.unit_sale import UnitSaleService
from test_quest_service import bundle, request
from test_training_service import columns


def call(client, envelope):
    response = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
    assert response.status_code == 200, response.text
    return response.content, {child.name: child for child in loads(decode_wire(response.content, envelope.profile.response_key)).children}


def test_native_sale_bulk_deposit_and_restart_preserve_every_domain(bundle, tmp_path):
    assert bundle.sales is not None
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    initial, units = bundle.player_state.credit_units(initial, [
        {"card_id": 77, "level": 1, "count": 1}, {"card_id": 533, "level": 1, "count": 1},
        {"card_id": 902, "level": 1, "count": 1}, {"card_id": 54, "level": 1, "count": 2},
        {"card_id": 7, "level": 1, "count": 1}])
    initial["player_state"]["units"][-1].update(Love=150, WatchLoveEv=3)
    initial["tables"]["game_param"][0]["Money"] = 1000000
    def application():
        sales = UnitSaleService(bundle.bootstrap, bundle.player_state, bundle.possessions, bundle.gifts, bundle.sales.catalog)
        return create_app(tmp_path, initial_state=initial, handlers=(bundle.bootstrap.handlers()
            | bundle.player_state.handlers() | sales.handlers() | bundle.fairy_farm.handlers() | bundle.silver_house.handlers()))
    app = application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        store = app.state.store
        _, silver = call(client, request(store, 126, ()))
        assert silver["PSH"].children[0].values == ()
        _, fairies = call(client, request(store, 122, ()))
        assert len(fairies["PFF"].children[0].values) == 54
        auto = request(store, 4, (Container("APPEND", columns(units[:3])),))
        _, result = call(client, auto)
        assert result["PGP"].children[1].values == ("1136000",)
        assert set(result) == {"SYSTEM", "PGP"}
        bulk = request(store, 124, (), tick=1001)
        _, result = call(client, bulk)
        assert result["ERC"].children[0].values == (0,)
        assert result["RML"].children[0].values == tuple(unit["GetIndex"] for unit in units[3:5])
        assert len(result["PFF"].children[0].values) == 54
        manual = request(store, 3, (Container("APPEND", columns(units[-1:])),), tick=1002)
        wire, result = call(client, manual)
        assert result["POSSESSION"].children[0].values == (bundle.sales.catalog.rainbow_item,)
        saved = store.profile()
        assert saved.state["gift_state"]["claims"]["7"] == [1, 100, 150]
        assert [unit["CardID"] for unit in saved.state["player_state"]["units"]] == [1]
        assert saved.state["player_state"]["battle"]["UX"] == 6
        assert saved.state["player_state"]["last_unique_id"] == 7
        assert next(row["Count"] for row in saved.state["tables"]["fairy_farm"] if row["CardID"] == 54) == 2
    reopened = application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert call(client, manual)[0] == wire
        assert reopened.state.store.profile() == saved
        stale = client.post("/api/" + auto.profile.route, content=encode_wire(auto.xml, auto.profile.request_key))
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "sale_retry_stale"
        _, result = call(client, request(reopened.state.store, 122, ()))
        assert dict(zip(result["PFF"].children[0].values, result["PFF"].children[1].values))[54] == 2
        assert reopened.state.store.profile() == saved


def test_native_auto_sale_of_an_organized_gold_armor_reads_back_an_empty_squad_slot(bundle, tmp_path):
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    initial, units = bundle.player_state.credit_units(initial, [{"card_id": 77, "level": 1, "count": 2}])
    initial["player_state"]["squads"] = [{"Address": slot, "UnitIndex": unit["GetIndex"], "Name": "", "Flags": 0}
                                         for slot, unit in enumerate(units, 1)]
    sales = UnitSaleService(bundle.bootstrap, bundle.player_state, bundle.possessions, bundle.gifts, bundle.sales.catalog)
    app = create_app(tmp_path, initial_state=initial, handlers=bundle.player_state.handlers() | sales.handlers())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, result = call(client, request(app.state.store, 4, (Container("APPEND", columns(units[:1])),)))
        assert result["PGP"].children[1].values == (str(initial["tables"]["game_param"][0]["Money"] + 6000),)
        _, squads = call(client, request(app.state.store, 72, ()))
        assert squads["TGI"].children[0].values == (1, 2)
        assert squads["TGI"].children[1].values == (-1, units[1]["GetIndex"])
        _, roster = call(client, request(app.state.store, 41, ()))
        assert roster["A7"].values == (0, units[1]["GetIndex"])


def test_bulk_partial_capacity_reports_exact_remove_list_and_retries_after_restart(bundle, tmp_path):
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    initial, units = bundle.player_state.credit_units(initial, [{"card_id": 54, "level": 1, "count": 2}])
    initial["fairy_farm_schema"] = 1
    initial["tables"]["fairy_farm"] = [{"CardID": 54, "Count": 599}]
    app = create_app(tmp_path, initial_state=initial, handlers=bundle.fairy_farm.handlers())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = request(app.state.store, 124, (Container("BRK", (Column("IDX", "I", (0,)),)),))
        wire, result = call(client, envelope)
        assert result["ERC"].children[0].values == (1,)
        assert result["RML"].children[0].values == (units[0]["GetIndex"],)
        assert result["RML"].children[1].values == (units[0]["UniqueID"],)
        assert dict(zip(result["PFF"].children[0].values, result["PFF"].children[1].values))[54] == 600
        saved = app.state.store.profile()
        assert saved.state["player_state"]["units"][-1] == units[1]
    app = create_app(tmp_path, handlers=bundle.fairy_farm.handlers())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert call(client, envelope)[0] == wire
        assert app.state.store.profile() == saved
