from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.errors import ProtocolError
from aigis_ps.unit_initialization import UnitInitialValues
from test_evolution_integration import post
from test_quest_service import bundle, request


def application(bundle, path, state):
    return create_app(path, initial_state=bundle.initialize(state), state_initializer=bundle.initialize,
        handlers=bundle.presents.handlers() | bundle.player_state.handlers() | bundle.bootstrap.handlers())


def claim(store, ids, operation=117, tick=1000):
    return request(store, operation, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", tuple(ids)),)),)),), tick=tick)


def reward(template):
    return {"kind": "unit", "template_id": template, "count": 1, "description": "Local test reward"}


def affected_templates(bundle):
    result = []
    for template, identifier in bundle.presents.templates.items():
        card = bundle.gifts.catalog.card(identifier)
        cls = bundle.player_state.classes[card["InitClassID"]]
        rarity = bundle.training.catalog.rarities[card["Rare"] + 1]
        if (card["Blood"] == 3 or card["ExcludeFameDisplayFirstClass"] & 255) and cls["JobChange"] and rarity["EvolutionConfig"]:
            result.append((template, identifier))
    return sorted(result)


def test_all_96_affected_templates_are_received_awakened_and_persist_after_restart(bundle, tmp_path):
    affected = affected_templates(bundle)
    assert len(affected) == 96
    state = bundle.initialize(bundle.bootstrap.initial_state())
    state, mail = bundle.presents.enqueue(state, [reward(template) for template, _ in affected])
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        envelope = claim(app.state.store, [row["PresentID"] for row in mail])
        wire, response = post(client, envelope)
        saved = app.state.store.profile()
        units = saved.state["player_state"]["units"][1:]
        assert [unit["CardID"] for unit in units] == [identifier for _, identifier in affected]
        assert response["PPU"].children[2].values == (2,) * 96
        for unit in units:
            card = bundle.gifts.catalog.card(unit["CardID"])
            assert unit["ClassChangeCount"] == 2
            assert unit["ClassID"] == bundle.player_state.classes[card["InitClassID"]]["JobChange"]
            assert unit["SettingSkillID"] == card["ClassLV0SkillID"]
            assert (unit["Exp"], unit["Love"], unit["SkillLevel"], unit["SkillEvolved"]) == (0, 0, 1, 0)
        assert saved.state["player_state"]["units"][0] == before.state["player_state"]["units"][0]
        assert saved.state["tables"]["game_param"] == before.state["tables"]["game_param"]
        assert saved.state["tables"]["presents"] == []
        assert saved.state["evolution_rewards"] == before.state["evolution_rewards"]
        assert post(client, envelope)[0] == wire and app.state.store.profile() == saved
    reopened = application(bundle, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        _, listing = post(client, request(reopened.state.store, 41, ()))
        assert listing["A3"].values == (0, *(2 for _ in affected))
        assert reopened.state.store.profile() == saved


@pytest.mark.parametrize("operation", [1, 2])
def test_single_mail_aliases_share_default_initialization(bundle, tmp_path, operation):
    template, identifier = affected_templates(bundle)[0]
    state, mail = bundle.presents.enqueue(bundle.initialize(bundle.bootstrap.initial_state()), [reward(template)])
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, response = post(client, claim(app.state.store, [mail[0]["PresentID"]], operation))
        assert response["PPU"].children[0].values == (identifier,)
        assert response["PPU"].children[2].values == (2,)


def test_old_claim_receipt_and_existing_unit_remain_unchanged_while_new_mail_is_fixed(bundle, tmp_path, monkeypatch):
    template, identifier = affected_templates(bundle)[0]
    card = bundle.gifts.catalog.card(identifier)
    state, mail = bundle.presents.enqueue(bundle.initialize(bundle.bootstrap.initial_state()), [reward(template)])
    original = bundle.player_state.initial_unit_values
    def legacy(card_id, level, *, evolution_count=0):
        if card_id == identifier:
            return UnitInitialValues(card["InitClassID"], 0, card["ClassLV0SkillID"], 0)
        return original(card_id, level, evolution_count=evolution_count)
    with monkeypatch.context() as context:
        context.setattr(bundle.player_state, "initial_unit_values", legacy)
        app = application(bundle, tmp_path, state)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            old_request = claim(app.state.store, [mail[0]["PresentID"]], 1)
            old_wire, _ = post(client, old_request)
            old_save = app.state.store.profile()
            old_unit = deepcopy(old_save.state["player_state"]["units"][1])
            assert old_unit["ClassChangeCount"] == 0
    reopened = application(bundle, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, old_request)[0] == old_wire
        assert reopened.state.store.profile() == old_save
        current = reopened.state.store.profile()
        next_state, new_mail = bundle.presents.enqueue(current.state, [reward(template)])
        reopened.state.store.update_state(next_state, current.revision)
        post(client, claim(reopened.state.store, [new_mail[0]["PresentID"]], 1, tick=1001))
        saved = reopened.state.store.profile().state
        assert saved["player_state"]["units"][1] == old_unit
        assert saved["player_state"]["units"][2]["ClassChangeCount"] == 2
        assert saved["player_state"]["units"][2]["UniqueID"] > old_unit["UniqueID"]


def test_default_high_level_and_explicit_shop_stage_share_the_final_class_experience(bundle):
    before = bundle.initialize(bundle.bootstrap.initial_state())
    copied = deepcopy(before)
    updated, units = bundle.player_state.credit_units(before, [
        {"card_id": 903, "level": 999, "count": 1},
        {"card_id": 2079, "level": 1, "count": 1, "evolution_count": 2}])
    assert units[0]["ClassChangeCount"] == units[1]["ClassChangeCount"] == 2
    assert units[0]["Exp"] == bundle.player_state.experience_for_level(903, 999) > 0
    assert units[1]["Exp"] == 0
    assert before == copied and len(updated["player_state"]["units"]) == 3


def test_new_default_awakened_unit_does_not_mark_second_awakening_reward_as_claimed(bundle):
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state, units = bundle.player_state.credit_units(state, [{"card_id": 903, "level": 1, "count": 1}])
    assert units[0]["ClassChangeCount"] == 2
    updated = bundle.evolution_rewards.initialize(state)
    assert updated["evolution_rewards"]["claimed"] == []


@pytest.mark.parametrize("stage", [True, -1, 1, 3, 4, "2"])
def test_grant_stage_argument_is_validated_before_mutation(bundle, stage):
    state = bundle.initialize(bundle.bootstrap.initial_state())
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        bundle.player_state.credit_units(state, [{"card_id": 903, "level": 1, "count": 1, "evolution_count": stage}])
    assert caught.value.code == "unit_grant_invalid"
    assert state == before


def test_prince_variant_uses_no_ordinary_grant_fallback(bundle):
    state = bundle.initialize(bundle.bootstrap.initial_state())
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        bundle.player_state.credit_units(state, [{"card_id": 309, "level": 1, "count": 1}])
    assert caught.value.code == "unit_grant_unsupported"
    assert state == before
