from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.app import create_app
from aigis_ps.local_present_templates import LOCAL_TEMPLATE_CARDS
from aigis_ps.player_state import PRINCE_CARD_IDS
from aigis_ps.protocol import decode_wire, encode_wire
from test_evolution_catalog import unit, materials
from test_evolution_service import columns
from test_quest_service import bundle, request


def seed(bundle, target, selected):
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    additions = [deepcopy(row) for row in (target, *selected)]
    for index, row in enumerate(additions, 1):
        row.update(GetIndex=index, UniqueID=index + 1)
    player = state["player_state"]
    player["units"].extend(additions)
    player["battle"]["UX"] = len(additions)
    player["last_unique_id"] = len(additions) + 1
    state["tables"]["game_param"][0]["Money"] = 1000000
    return state, additions


def message(store, target, selected, operation=7, branch=0, tick=1000):
    parts = [Container("BASE", columns((target,))), Container("APPEND", columns(selected))]
    if operation == 9:
        parts.append(Column("TYPE", "I", (branch,)))
    return request(store, operation, parts, tick=tick)


def post(client, envelope, expected=200):
    response = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
    assert response.status_code == expected, response.text
    if expected != 200:
        return response
    return response.content, {node.name: node for node in loads(decode_wire(response.content, envelope.profile.response_key)).children}


@pytest.mark.parametrize("operation", [7, 8])
def test_actual_awakening_preserves_identity_and_replays_after_restart(bundle, tmp_path, operation):
    assert bundle.evolution is not None, bundle.evolution_error
    catalog = bundle.evolution.catalog
    target = unit(catalog, level=999 if operation == 7 else 30, stage=0 if operation == 7 else 2)
    if operation == 7:
        target["Exp"] += 123
        selected = materials(catalog, target, 7)
    else:
        target["SkillLevel"] = catalog.growth.describe(target).skill_max
        selected = [unit(catalog, 234)]
    state, rows = seed(bundle, target, selected)
    quote = catalog.quote(rows[0], rows[1:], operation)
    state = bundle.possessions.credit(state, {key: value * 2 for key, value in quote.items.items()})
    def application():
        return create_app(tmp_path, initial_state=state,
            handlers=bundle.evolution.handlers() | bundle.player_state.handlers() | bundle.bootstrap.handlers())
    app = application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = message(app.state.store, rows[0], rows[1:], operation)
        wire, response = post(client, envelope)
        assert set(response) == {"SYSTEM", "PGP", "PPU", "POSSESSION"}
        profile = app.state.store.profile()
        saved = profile.state["player_state"]["units"]
        assert len(saved) == 2 and saved[1] == {**rows[0], **quote.values}
        assert profile.state["tables"]["game_param"][0]["Money"] == 1000000 - quote.price
        assert {row["ItemID"]: row["Count"] for row in profile.state["tables"]["possessions"]} == dict(quote.items)
        assert profile.state["player_state"]["battle"]["UX"] == len(rows)
        assert post(client, envelope)[0] == wire and app.state.store.profile() == profile
    reopened = application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        _, reloaded = post(client, request(reopened.state.store, 41, ()))
        assert reloaded["A1"].values == (1, 7)
        assert reloaded["A4"].values[1] == saved[1]["Exp"]
        assert reopened.state.store.profile() == profile


def test_actual_warehouse_fairy_is_consumed_without_an_unregistered_response(bundle, tmp_path):
    catalog = bundle.evolution.catalog
    target = unit(catalog, stage=2)
    target["SkillLevel"] = catalog.growth.describe(target).skill_max
    fairy = bundle.fairy_farm.material(234)
    fairy["StorageType"] = 2
    state, rows = seed(bundle, target, [])
    state["fairy_farm_schema"] = 1
    state["tables"]["fairy_farm"] = [{"CardID": 234, "Count": 1}]
    quote = catalog.quote(rows[0], [fairy], 8)
    state = bundle.possessions.credit(state, dict(quote.items))
    app = create_app(tmp_path, initial_state=state, handlers=bundle.evolution.handlers() | bundle.fairy_farm.handlers())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, response = post(client, message(app.state.store, rows[0], [fairy], 8))
        assert "PFF" not in response and "PSH" not in response
        _, listing = post(client, request(app.state.store, 122, ()))
        stock = dict(zip(listing["PFF"].children[0].values, listing["PFF"].children[1].values))
        assert stock[234] == 0
        assert len(app.state.store.profile().state["player_state"]["units"]) == 2


def silver_slot_app(bundle, tmp_path, rarity):
    catalog = bundle.evolution.catalog
    target = unit(catalog, level=999)
    selected = materials(catalog, target, 7)
    slot = catalog.materials.slots(catalog.growth.describe(target), 7)[0]
    silver_id = next(card_id for card_id in sorted(bundle.silver_house._card_ids)
                     if card_id not in PRINCE_CARD_IDS and catalog.cards.card(card_id)["Rare"] == rarity
                     and catalog.cards.card(card_id)["InitClassID"] // 100
                     and any(condition._matches((card_id, catalog.cards.card(card_id)["InitClassID"], rarity, 0, 30))
                             for condition in slot))
    quote = catalog.quote(target, selected, 7)
    state, rows = seed(bundle, target, selected[1:])
    state["silver_house_schema"] = 1
    state["tables"]["silver_house"] = [{"CardID": silver_id, "Count": 1}]
    state = bundle.possessions.credit(state, {key: value * 2 for key, value in quote.items.items()})
    app = create_app(tmp_path, initial_state=state, handlers=bundle.evolution.handlers() | bundle.silver_house.handlers())
    warehouse = {**bundle.silver_house.material(silver_id), "StorageType": 1}
    return app, silver_id, lambda: message(app.state.store, rows[0], [warehouse, *rows[1:]])


def test_warehouse_rarity_three_silver_template_fills_an_ordinary_slot(bundle, tmp_path):
    app, silver_id, envelope = silver_slot_app(bundle, tmp_path, 3)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, response = post(client, envelope())
        assert "PSH" not in response and "PFF" not in response
        _, listing = post(client, request(app.state.store, 126, ()))
        assert dict(zip(listing["PSH"].children[0].values, listing["PSH"].children[1].values)) == {silver_id: 0}


def test_warehouse_rarity_two_silver_template_stays_below_the_level_thirty_slot(bundle, tmp_path):
    app, _, envelope = silver_slot_app(bundle, tmp_path, 2)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        assert post(client, envelope(), 409).json()["error"]["code"] == "evolution_materials_mismatch"
        assert app.state.store.profile() == before


def second_scenario(bundle, *, card_id=17, twice=False):
    catalog = bundle.evolution.catalog
    target = unit(catalog, card_id, stage=2)
    target["Exp"] = 123
    selected = materials(catalog, target, 9)
    state, rows = seed(bundle, target, [*selected, target, *selected] if twice else selected)
    quote = catalog.quote(rows[0], rows[1:5], 9, 1)
    state = bundle.possessions.credit(state, {key: value * 2 for key, value in quote.items.items()})
    return state, rows, quote


def second_app(bundle, tmp_path, state):
    return create_app(tmp_path, initial_state=bundle.initialize(state), state_initializer=bundle.initialize,
        handlers=bundle.evolution.handlers() | bundle.player_state.handlers() | bundle.bootstrap.handlers()
                 | bundle.presents.handlers())


def test_second_awake_gives_original_card_once_and_claim_survives_reopen(bundle, tmp_path):
    state, rows, quote = second_scenario(bundle)
    state = bundle.card_status.update(state, [{"CardID": 17, "A1": 7, "A7": 3, "B0": 1234}])
    app = second_app(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = message(app.state.store, rows[0], rows[1:], 9, 1)
        wire, response = post(client, envelope)
        assert "PPT" in response and "CardStatus" in response
        fields = {column.name: column.values for column in response["PPT"].children}
        assert fields["A2"] == (208,)
        status = {column.name: column.values for column in response["CardStatus"].children}
        assert status["CardID"] == (17,) and status["A1"] == (7,) and status["A7"] == (3,) and status["B0"] == (1234,)
        profile = app.state.store.profile()
        assert profile.state["player_state"]["units"][1] == {**rows[0], **quote.values}
        assert profile.state["evolution_rewards"]["claimed"] == [17]
        assert post(client, envelope)[0] == wire and app.state.store.profile() == profile
    reopened = second_app(bundle, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        identifier = fields["PresentID"][0]
        claim = request(reopened.state.store, 1, (Container("RT", (Container("PT2", (
            Column("PresentID", "I", (identifier,)),)),)),))
        _, response = post(client, claim)
        unit_rows = reopened.state.store.profile().state["player_state"]["units"]
        assert len(unit_rows) == 3
        assert unit_rows[-1]["CardID"] == 17 and unit_rows[-1]["ClassChangeCount"] == 0
        assert unit_rows[-1]["Exp"] == 0 and unit_rows[-1]["SkillLevel"] == 1
        assert unit_rows[-1]["UniqueID"] > max(row["UniqueID"] for row in rows)


def test_second_copy_can_awaken_without_repeating_mail_or_first_time_notice(bundle, tmp_path):
    state, rows, _ = second_scenario(bundle, twice=True)
    app = second_app(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, message(app.state.store, rows[0], rows[1:5], 9, 1))
        _, second = post(client, message(app.state.store, rows[5], rows[6:], 9, 2, tick=1001))
        assert "PPT" not in second and second["CardStatus"].children == ()
        assert len(bundle.presents.rows(app.state.store.profile().state)) == 1
        assert [row["ClassChangeCount"] for row in app.state.store.profile().state["player_state"]["units"]] == [0, 3, 4]


@pytest.mark.parametrize("card_id,template_id", [(card_id, template_id) for template_id, card_id in LOCAL_TEMPLATE_CARDS.items()])
def test_local_first_reward_template_can_be_awarded_and_claimed(bundle, tmp_path, card_id, template_id):
    state, rows, _ = second_scenario(bundle, card_id=card_id)
    app = second_app(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, response = post(client, message(app.state.store, rows[0], rows[1:], 9, 1))
        mail = bundle.presents.rows(app.state.store.profile().state)
        assert len(mail) == 1 and mail[0]["Content"] == template_id
        claim = request(app.state.store, 1, (Container("RT", (Container("PT2", (
            Column("PresentID", "I", (mail[0]["PresentID"],)),)),)),))
        post(client, claim)
        obtained = app.state.store.profile().state["player_state"]["units"][-1]
        assert obtained["CardID"] == card_id and obtained["ClassChangeCount"] == obtained["Exp"] == 0
        assert obtained["SkillLevel"] == 1


def test_old_second_awake_history_is_initialized_before_an_instance_is_removed(bundle, tmp_path):
    state, rows, _ = second_scenario(bundle, twice=True)
    old = rows[5]
    old.update(ClassID=bundle.evolution.catalog.route(bundle.evolution.catalog.cards.card(17), 3)["ClassID"],
               ClassChangeCount=3)
    state["player_state"]["units"][6] = deepcopy(old)
    initialized = bundle.initialize(state)
    assert initialized["evolution_rewards"]["claimed"] == [17]
    initialized["player_state"]["units"] = [row for row in initialized["player_state"]["units"]
        if row["UniqueID"] != old["UniqueID"]]
    app = second_app(bundle, tmp_path, initialized)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, response = post(client, message(app.state.store, rows[0], rows[1:5], 9, 1))
        assert "PPT" not in response and response["CardStatus"].children == ()
        assert bundle.presents.rows(app.state.store.profile().state) == []
