from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.errors import ProtocolError
from test_evolution_integration import post
from test_quest_service import bundle, request


def reward(name, amount=1):
    return {"kind": "item", "item_id": name, "amount": amount, "description": "称号獲得"}


def claim(store, identifiers, *, number=1, tick=1000):
    return request(store, number, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", tuple(identifiers)),)),)),), tick=tick)


def initial(bundle):
    return bundle.initialize(bundle.bootstrap.initial_state())


def application(bundle, path, state):
    return create_app(path, initial_state=state, state_initializer=bundle.initialize,
                      handlers=bundle.bootstrap.handlers() | bundle.presents.handlers() | bundle.player_state.handlers())


def test_all_emblem_items_use_verified_individual_inventory_rules(bundle):
    catalog = bundle.presents.emblem_catalog
    assert len(catalog.item_ids) == 83
    for name in catalog.item_ids:
        definition = bundle.presents.item_catalog.definition(22, name)
        assert definition.stack_limit == 1 and definition.single_only
    assert bundle.presents.item_catalog.status()["item_count"] == 479


def test_emblem_claim_unlocks_by_expression_without_switching_team_or_prince(bundle, tmp_path):
    catalog = bundle.presents.emblem_catalog
    name = "get_emblem_00001"
    title = next(title for title in catalog.titles if title.predicate == "possession" and title.argument == name)
    state, mail = bundle.presents.enqueue(initial(bundle), [reward(name)])
    assert not catalog.eligible(title.index, state)
    app = application(bundle, tmp_path, state)
    before = app.state.store.profile()
    envelope = claim(app.state.store, [mail[0]["PresentID"]])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        wire, result = post(client, envelope)
        after = app.state.store.profile()
        assert catalog.eligible(title.index, after.state)
        assert set(result) == {"SYSTEM", "PPU", "POSSESSION"}
        assert not result["PPU"].children[0].values
        assert after.state["player_state"] == before.state["player_state"]
        for table in ("config", "card_status", "crystals", "perused_events", "universal_flags"):
            assert after.state["tables"][table] == before.state["tables"][table]
        assert after.state["mission_state"] == before.state["mission_state"]
        assert post(client, envelope)[0] == wire and app.state.store.profile() == after
    reopened = application(bundle, tmp_path, state)
    assert catalog.eligible(title.index, reopened.state.store.profile().state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        _, loaded = post(client, request(reopened.state.store, 137, ()))
        columns = {col.name: col.values for col in loaded["POSSESSION"].children}
        assert columns["ItemID"] == (name,) and columns["Count"] == (1,)


@pytest.mark.parametrize("name,index,expected", [
    ("get_emblem_00034", 70, False), ("get_emblem_00034", 71, True),
    ("get_emblem_10018", 123, False),
])
def test_emblem_display_mapping_does_not_replace_actual_eligibility(bundle, name, index, expected):
    state, mail = bundle.presents.enqueue(initial(bundle), [reward(name)])
    updated, units, crystals = bundle.presents.claim(state, [mail[0]["PresentID"]], acquisition_day=739920)
    assert not units and crystals == 0
    assert bundle.presents.emblem_catalog.eligible(index, updated) is expected


def test_owned_emblem_keeps_duplicate_mail_and_balance(bundle):
    name = "get_emblem_00001"
    state = bundle.possessions.credit(initial(bundle), {name: 1})
    state, mail = bundle.presents.enqueue(state, [reward(name)])
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        bundle.presents.claim(state, [mail[0]["PresentID"]], acquisition_day=739920)
    assert error.value.code == "present_item_capacity"
    assert state == before


def test_emblem_single_claim_policy_preserves_all_other_mail(bundle, tmp_path):
    state, mail = bundle.presents.enqueue(initial(bundle), [reward("get_emblem_00001"),
        {"kind": "free_crystal", "amount": 1, "description": "贈り物"}])
    app = application(bundle, tmp_path, state)
    before = app.state.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        result = post(client, claim(app.state.store, [row["PresentID"] for row in mail]), 409)
        assert result.json()["error"]["code"] == "present_item_single_only"
    assert app.state.store.profile() == before


@pytest.mark.parametrize("mixed", [False, True])
def test_emblem_batch_claim_keeps_the_emblem_mail(bundle, tmp_path, mixed):
    state, mail = bundle.presents.enqueue(initial(bundle), [reward("get_emblem_00001"),
        {"kind": "free_crystal", "amount": 1, "description": "贈り物"}])
    app = application(bundle, tmp_path, state)
    identifiers = [row["PresentID"] for row in mail] if mixed else [mail[0]["PresentID"]]
    before = app.state.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, result = post(client, claim(app.state.store, identifiers, number=117))
    after = app.state.store.profile()
    assert after.state["tables"]["presents"] == (mail[:1] if mixed else mail)
    assert after.state["tables"]["possessions"] == before.state["tables"]["possessions"]
    assert set(result) == ({"SYSTEM", "PPU", "PFC"} if mixed else {"SYSTEM", "PPU"})
    assert (after == before) is not mixed


def test_vote_items_sharing_effect_ten_are_not_enabled_as_emblems(bundle):
    state = initial(bundle)
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        bundle.presents.enqueue(state, [reward("item_vote_008001")])
    assert error.value.code == "present_item_unsupported"
    assert state == before


def test_member_save_cannot_select_locked_title_but_preserves_existing_legacy_choice(bundle, tmp_path):
    catalog = bundle.presents.emblem_catalog
    name = "get_emblem_00001"
    index = next(title.index for title in catalog.titles if title.predicate == "possession" and title.argument == name)
    other = next(title.index for title in catalog.titles
                 if title.predicate == "possession" and title.argument != name)
    state, mail = bundle.presents.enqueue(initial(bundle), [reward(name)])
    app = application(bundle, tmp_path, state)

    def save(selected, group=0):
        return request(app.state.store, 73, (Column("A0", "I", ((group << 24) | 1,)), Column("A1", "I", (-1,)),
            Column("A3", "S", ("編成",)), Column("A4", "I", (selected,))))

    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        result = post(client, save(index), 409)
        assert result.json()["error"]["code"] == "emblem_locked"
        assert app.state.store.profile() == before
        post(client, claim(app.state.store, [mail[0]["PresentID"]]))
        post(client, save(index))
        saved = app.state.store.profile()
        saved.state["tables"]["possessions"][0]["Count"] = 0
        app.state.store.update_state(saved.state, saved.revision)
        post(client, save(index))
        before = app.state.store.profile()
        result = post(client, save(index, group=1), 409)
        assert result.json()["error"]["code"] == "emblem_locked"
        assert app.state.store.profile() == before
        result = post(client, save(other), 409)
        assert result.json()["error"]["code"] == "emblem_locked"
        assert app.state.store.profile() == before
