from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.barracks_service import EXTRA_SHIFT, PURCHASE_FIELD
from test_evolution_integration import post
from test_quest_service import bundle, request


def initial(bundle, count=6):
    state = bundle.initialize(bundle.bootstrap.initial_state())
    state["tables"]["config"][0][PURCHASE_FIELD] = 2 << EXTRA_SHIFT
    return bundle.player_state.credit_units(state, [{"card_id": 136, "level": 1, "count": count}])


def application(bundle, path, state):
    return create_app(path, initial_state=state,
        handlers=bundle.barracks.handlers() | bundle.fairy_farm.handlers() | bundle.player_state.handlers())


def move(store, units, destination, tick=1000):
    return request(store, 118, (Column("DEST", "I", (destination,)), Container("BASE", (
        Column("A7", "I", tuple(unit["GetIndex"] for unit in units)),
        Column("AB", "I", tuple(unit["UniqueID"] for unit in units))))), tick=tick)


def deposit(store, units, tick=1000):
    return request(store, 123, (Container("LST", (
        Column("A7", "I", tuple(unit["GetIndex"] for unit in units)),
        Column("AB", "I", tuple(unit["UniqueID"] for unit in units)))),), tick=tick)


def bulk(store, barracks=None, tick=1000):
    fields = () if barracks is None else (Container("BRK", (Column("IDX", "I", (barracks,)),)),)
    return request(store, 124, fields, tick=tick)


def test_selected_barracks_and_all_barracks_never_overlap_unrequested_units(bundle, tmp_path):
    state, units = initial(bundle)
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, move(app.state.store, units[2:4], 1))
        post(client, move(app.state.store, units[4:], 2, tick=1001))
        _, selected = post(client, bulk(app.state.store, 1))
        assert selected["RML"].children[0].values == tuple(unit["GetIndex"] for unit in units[2:4])
        assert bundle.unit_capacity.usage(app.state.store.profile().state).counts == (3, 0, 2)
        envelope = bulk(app.state.store, tick=1001)
        wire, all_barracks = post(client, envelope)
        assert all_barracks["RML"].children[0].values == tuple(unit["GetIndex"] for unit in units[:2] + units[4:])
        saved = app.state.store.profile()
        assert len(saved.state["player_state"]["units"]) == 1
        assert bundle.fairy_farm.read(saved.state)[bundle.fairy_farm.card_ids.index(136)]["Count"] == 6
        assert post(client, envelope)[0] == wire and app.state.store.profile() == saved
    reopened = application(bundle, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        _, roster = post(client, request(reopened.state.store, 41, ()))
        assert roster["A1"].values == (1,)
        assert reopened.state.store.profile() == saved


def test_selected_extra_deposit_and_withdrawal_preserve_highwater_and_primary_destination(bundle, tmp_path):
    state, units = initial(bundle, 2)
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, move(app.state.store, units, 2))
        post(client, deposit(app.state.store, units))
        before = app.state.store.profile()
        post(client, request(app.state.store, 125, (Container("Draw", (
            Column("CardID", "I", (136,)), Column("Count", "I", (2,)))),), tick=1001))
        after = app.state.store.profile()
        assert bundle.unit_capacity.usage(after.state).counts == (3, 0, 0)
        assert min(unit["GetIndex"] for unit in after.state["player_state"]["units"][1:]) > before.state["player_state"]["battle"]["UX"]
        assert min(unit["UniqueID"] for unit in after.state["player_state"]["units"][1:]) > before.state["player_state"]["last_unique_id"]


def test_full_stock_uses_global_getindex_order_across_barracks(bundle, tmp_path):
    state, units = initial(bundle, 3)
    state["fairy_farm_schema"] = 1
    state["tables"]["fairy_farm"] = [{"CardID": 136, "Count": bundle.fairy_farm.cap - 1}]
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, move(app.state.store, units[:1], 2))
        _, response = post(client, bulk(app.state.store))
        assert response["RML"].children[0].values == (units[0]["GetIndex"],)
        assert response["ERC"].children[0].values == (1,)
        saved = app.state.store.profile()
        assert [unit["GetIndex"] for unit in saved.state["player_state"]["units"]] == [0, units[1]["GetIndex"], units[2]["GetIndex"]]
