from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import aigis_ps.presents as presents_module
from aigis_ps.errors import ProtocolError
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.vote_cooking_catalog import VoteCookingCatalog
from test_evolution_integration import post
from test_present_recovery_integration import (
    NOW, application, claim, columns, game, reject, seed, services as recovery_services,
)
from test_quest_service import bundle, request


@pytest.fixture(scope="module")
def cooking_catalog(bundle):
    return VoteCookingCatalog(lambda name: bundle.native_cdn.assets.read(name)[0],
        MasterDataStore(Path(__file__).resolve().parents[1] / "runtime/server/masterdata"))


def services(bundle, catalog):
    case = recovery_services(bundle)
    case.presents.vote_cooking_catalog = catalog
    return case


def reward(card_id, **changes):
    return {"kind": "vote_cooking", "card_id": card_id, "description": "最多投票のお礼の手料理", **changes}


def zero_kind(catalog):
    return next(card for card, definition in catalog.definitions.items() if definition.cooking_kind == 0)


@pytest.mark.parametrize("operation", [1, 2])
@pytest.mark.parametrize("card_id", [1, 3])
def test_character_cooking_uses_native_single_claim_and_preserves_other_assets(bundle, cooking_catalog, tmp_path, operation, card_id):
    case = services(bundle, cooking_catalog)
    state, mails = case.presents.enqueue(seed(case), [reward(card_id)])
    original = deepcopy(state)
    assert mails == [{"PresentID": 1, "Type": 21, "Content": card_id, "Description": reward(card_id)["description"]}]
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, result = post(client, request(app.state.store, 42, ()))
        assert columns(result["PPT"])["A1"] == (21,)
        assert columns(result["PPT"])["A2"] == (card_id,)
        assert columns(result["PPT"])["A6"] == ("",)
        envelope = claim(app.state.store, operation, [mails[0]["PresentID"]])
        wire, result = post(client, envelope)
        assert set(result) == {"SYSTEM", "PGP", "PPU"}
        saved = app.state.store.profile()
        assert game(saved.state)["NowCharisma"] == 20 + case.bootstrap.rank_rows[0]["CharismaMax"]
        assert saved.state["tables"]["presents"] == []
        for name in ("possessions", "crystals", "perused_events"):
            assert saved.state["tables"][name] == original["tables"][name]
        assert saved.state["player_state"] == original["player_state"]
        assert post(client, envelope)[0] == wire
        assert app.state.store.profile() == saved
        _, result = post(client, request(app.state.store, 42, ()))
        assert not columns(result["PPT"])["PresentID"]
    assert state == original


def test_home_cooking_zero_is_a_real_recipe_and_not_a_missing_definition(bundle, cooking_catalog):
    card_id = zero_kind(cooking_catalog)
    definition = cooking_catalog.definition_from_content(card_id)
    assert definition.cooking_name == "愛情お弁当"
    case = services(bundle, cooking_catalog)
    state, mails = case.presents.enqueue(seed(case), [reward(card_id)])
    final, _, _ = case.presents.claim(state, [mails[0]["PresentID"]], acquisition_day=1)
    assert game(final)["NowCharisma"] == 20 + case.bootstrap.rank_rows[0]["CharismaMax"]


def test_recipe_metadata_never_multiplies_recovery_and_each_gift_has_its_own_mail(bundle, cooking_catalog):
    case = services(bundle, cooking_catalog)
    state, mails = case.presents.enqueue(seed(case), [reward(1), reward(3)])
    assert [row["PresentID"] for row in mails] == [1, 2]
    final, _, _ = case.presents.claim(state, [2], acquisition_day=1)
    assert case.presents.rows(final) == [mails[0]]
    assert game(final)["NowCharisma"] == 20 + case.bootstrap.rank_rows[0]["CharismaMax"]


def test_mixed_recovery_claim_does_not_remove_any_mail(bundle, cooking_catalog, tmp_path):
    case = services(bundle, cooking_catalog)
    state, _ = case.presents.enqueue(seed(case), [reward(1), {"kind": "gold", "amount": 3, "description": "gold"}])
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(client, app.state.store, claim(app.state.store, 1, [1, 2]), 409, "present_recovery_single_only")


@pytest.mark.parametrize("selection", [[1], [1, 2]])
def test_batch_claim_keeps_character_cooking_in_the_mailbox(bundle, cooking_catalog, tmp_path, selection):
    case = services(bundle, cooking_catalog)
    state, mails = case.presents.enqueue(seed(case), [reward(1), {"kind": "gold", "amount": 3, "description": "gold"}])
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        post(client, claim(app.state.store, 117, selection))
        after = app.state.store.profile()
    assert case.presents.rows(after.state) == (mails[:1] if 2 in selection else mails)
    assert game(after.state)["NowCharisma"] == game(before.state)["NowCharisma"]
    assert game(after.state)["Money"] == game(before.state)["Money"] + (3 if 2 in selection else 0)


@pytest.mark.parametrize("success", [True, False])
def test_recovery_cap_retains_unconsumed_character_cooking(bundle, cooking_catalog, tmp_path, success):
    case = services(bundle, cooking_catalog)
    maximum = case.bootstrap.rank_rows[0]["CharismaMax"]
    state, _ = case.presents.enqueue(seed(case, current=2000 - maximum + (not success)), [reward(1)])
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = claim(app.state.store, 1, [1])
        if success:
            post(client, envelope)
            assert game(app.state.store.profile().state)["NowCharisma"] == 2000
        else:
            reject(client, app.state.store, envelope, 409, "recovery_cap_exceeded")


@pytest.mark.parametrize("value", [0, -1, True, "1", 10001, 2147483647])
def test_invalid_or_decorated_card_content_cannot_be_enqueued(bundle, cooking_catalog, value):
    case = services(bundle, cooking_catalog)
    state = seed(case)
    original = deepcopy(state)
    with pytest.raises(ProtocolError):
        case.presents.enqueue(state, [reward(value)])
    assert state == original


@pytest.mark.parametrize("changes", [{"Type": 23}, {"Type": 8, "Content": 1}, {"Content": 10001}, {"ItemID": "item_cook_logbo_00004"}])
def test_unsupported_legacy_and_decorated_rows_remain_rejected(bundle, cooking_catalog, changes):
    case = services(bundle, cooking_catalog)
    state, _ = case.presents.enqueue(seed(case), [reward(1)])
    state["tables"]["presents"][0].update(changes)
    with pytest.raises(ProtocolError):
        case.presents.rows(state)


@pytest.mark.parametrize("dependency", ["vote_cooking_catalog", "recovery", "recovery_catalog"])
def test_missing_dependencies_reject_delivery_without_modifying_state(bundle, cooking_catalog, dependency):
    case = services(bundle, cooking_catalog)
    state = seed(case)
    original = deepcopy(state)
    setattr(case.presents, dependency, None)
    with pytest.raises(ProtocolError) as error:
        case.presents.enqueue(state, [reward(1)])
    assert error.value.code == "vote_cooking_unavailable"
    assert state == original


def test_mailbox_capacity_and_serialization_failure_roll_back_character_cooking(bundle, cooking_catalog, tmp_path, monkeypatch):
    case = services(bundle, cooking_catalog)
    state, _ = case.presents.enqueue(seed(case), [reward(1)] * MAX_PRESENTS)
    original = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        case.presents.enqueue(state, [reward(3)])
    assert error.value.code == "present_capacity"
    assert state == original
    app = application(case, tmp_path, state)
    encode = presents_module.encode_response

    def fail_response(profile, xml):
        if profile.registration_keys == (42,):
            raise ProtocolError("test_response_capacity", "forced bounded-response failure", 409)
        return encode(profile, xml)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        monkeypatch.setattr(presents_module, "encode_response", fail_response)
        reject(client, app.state.store, claim(app.state.store, 1, [1]), 409, "test_response_capacity")


def test_claim_receipt_reopens_without_double_recovery_or_consuming_other_gifts(bundle, cooking_catalog, tmp_path):
    case = services(bundle, cooking_catalog)
    state, _ = case.presents.enqueue(seed(case), [reward(1), reward(3)])
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = claim(app.state.store, 1, [1])
        wire, _ = post(client, envelope)
        saved = app.state.store.profile()
    reopened = services(bundle, cooking_catalog)
    reopened.clock.now = NOW + timedelta(minutes=10)
    app = application(reopened, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert app.state.store.profile() == saved
        assert [row["PresentID"] for row in reopened.presents.rows(saved.state)] == [2]


def test_optional_catalog_advertises_type21_without_changing_recovery_fingerprint(bundle, cooking_catalog):
    case = recovery_services(bundle)
    fingerprint = case.presents.recovery_catalog.content_version
    assert 21 not in case.presents.status()["supported_types"]
    case.presents.vote_cooking_catalog = cooking_catalog
    assert 21 in case.presents.status()["supported_types"]
    assert 23 not in case.presents.status()["supported_types"]
    assert case.presents.recovery_catalog.content_version == fingerprint
