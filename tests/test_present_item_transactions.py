from copy import deepcopy
from dataclasses import replace

import pytest

from aigis_ps import presents as presents_module
from aigis_ps.errors import ProtocolError
from aigis_ps.presents import PresentService
from aigis_ps.storage import Store
from test_presents import DAY, claim_request, crystal, unit
from test_quest_service import bundle


ITEM = "gift_0000_bouquet"


@pytest.fixture
def subject(bundle):
    return PresentService(bundle.bootstrap, bundle.player_state, possessions=bundle.possessions,
                          asset_reader=bundle.player_state.read_asset)


def rewards():
    return [unit(), crystal(3), {"kind": "gold", "amount": 100, "description": "reward"},
            {"kind": "item", "item_id": ITEM, "amount": 2, "description": "reward"}]


def queued(bundle, subject):
    return subject.enqueue(bundle.initialize(bundle.bootstrap.initial_state()), rewards())


@pytest.mark.parametrize("number", [129, 137])
def test_full_item_readback_preflight_failure_rolls_back_every_reward(bundle, subject, tmp_path, monkeypatch, number):
    state, mail = queued(bundle, subject)
    store = Store(tmp_path / "preflight.sqlite3", state)
    before = store.profile()
    encode = presents_module.encode_response

    def bounded(profile, response):
        if number in profile.registration_keys:
            profile = replace(profile, response_limit=1)
        return encode(profile, response)

    monkeypatch.setattr(presents_module, "encode_response", bounded)
    with pytest.raises(ProtocolError):
        subject.receive(claim_request(store, [row["PresentID"] for row in mail], 117), store)
    assert store.profile() == before


def test_item_credit_failure_preserves_gold_units_and_mail(bundle, subject, monkeypatch):
    state, mail = queued(bundle, subject)
    before = deepcopy(state)

    def unavailable(*args):
        raise ProtocolError("test_credit_unavailable", "Item credit unavailable", 503)

    monkeypatch.setattr(subject.possessions, "credit", unavailable)
    with pytest.raises(ProtocolError, match="Item credit unavailable"):
        subject.claim(state, [row["PresentID"] for row in mail], acquisition_day=DAY)
    assert state == before


@pytest.mark.parametrize("mutation", ["missing_id", "wrong_type", "extra_id", "unknown_type"])
def test_invalid_item_mail_does_not_fall_back_to_another_reward(bundle, subject, mutation):
    state, mail = queued(bundle, subject)
    if mutation == "missing_id":
        state["tables"]["presents"][-1].pop("ItemID")
    elif mutation == "wrong_type":
        state["tables"]["presents"][-1]["ItemID"] = 1
    elif mutation == "extra_id":
        state["tables"]["presents"][0]["ItemID"] = ITEM
    else:
        state["tables"]["presents"][-1]["Type"] = 23
    before = deepcopy(state)
    with pytest.raises(ProtocolError):
        subject.claim(state, [row["PresentID"] for row in mail], acquisition_day=DAY, partial=True)
    assert state == before


def test_optional_item_dependency_does_not_disable_original_mail_types(bundle):
    subject = PresentService(bundle.bootstrap, bundle.player_state, asset_reader=bundle.player_state.read_asset)
    state = bundle.initialize(bundle.bootstrap.initial_state())
    state, mail = subject.enqueue(state, rewards()[:3])
    before = deepcopy(state)
    result, units, crystals = subject.claim(state, [row["PresentID"] for row in mail], acquisition_day=DAY)
    assert len(units) == 1 and crystals == 3 and result["tables"]["presents"] == []
    assert result["tables"]["game_param"][0]["Money"] == before["tables"]["game_param"][0]["Money"] + 100
    assert state == before
    with pytest.raises(ProtocolError) as error:
        subject.enqueue(state, rewards()[-1:])
    assert error.value.code == "present_items_unavailable"
