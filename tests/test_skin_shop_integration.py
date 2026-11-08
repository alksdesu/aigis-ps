from copy import copy, deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.app import create_app
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.energy_clock import JST
from aigis_ps.skin_shop_service import SkinShopService
from aigis_ps.universal_flags import flag_value, set_flags
from test_evolution_integration import post
from test_quest_service import bundle, request
from test_skin_shop_service import message


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


@pytest.fixture
def local(bundle):
    assert bundle.skin_shop is not None, bundle.skin_shop_error
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    service = SkinShopService(bootstrap, bundle.player_state, bundle.possessions,
                              bundle.skin_shop.catalog, bundle.missions)
    state = bundle.initialize(bootstrap.initial_state())
    state["tables"]["crystals"] = [{"FreeFlag": True, "Date": 20, "Num": 6},
                                  {"FreeFlag": False, "Date": 5, "Num": 20},
                                  {"FreeFlag": True, "Date": 10, "Num": 4}]
    state = set_flags(state, {"UnrelatedFlag": -3})
    product = next(product for product in service.catalog.products
                   if product.supported and service.catalog.is_open(product, NOW))
    return SimpleNamespace(bundle=bundle, bootstrap=bootstrap, service=service, state=state, product=product)


def application(local, path):
    return create_app(path, initial_state=local.state, state_initializer=local.bundle.initialize,
                      handlers=local.bootstrap.handlers() | local.service.handlers() | local.bundle.missions.handlers())


def buy(local, store, product=None, tick=1000):
    return message(SimpleNamespace(store=store, product=product or local.product), tick=tick)


def test_native_purchase_updates_wallet_flags_unlock_and_missions_then_replays_after_restart(local, tmp_path):
    product, bundle = local.product, local.bundle
    skins = bundle.missions.skins
    display = next(link.display_type for link in skins._links[product.skin_card_id] if link.item_id == product.item_id)
    assert display not in skins.allowed_displays(product.skin_card_id, local.state, smartphone_images=False)
    app = application(local, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        store = app.state.store
        before = store.profile()
        envelope = buy(local, store)
        wire, result = post(client, envelope)
        assert set(result) == {"SYSTEM", "PFC", "PPC", "POSSESSION", "GF", "SkinShopPurchase"}
        assert not result["SkinShopPurchase"].children
        assert result["GF"].children[0].values == (product.flag_name,)
        assert result["GF"].children[1].values == (1,)
        assert result["POSSESSION"].children[0].values == (product.item_id,)
        assert result["POSSESSION"].children[1].values == (1,)
        saved = store.profile()
        assert saved.revision == before.revision + 1
        assert wallet_totals(saved.state) == {"free": 0, "paid": 15, "total": 15}
        assert saved.state["player_state"] == before.state["player_state"]
        assert flag_value(saved.state, "UnrelatedFlag") == -3
        assert flag_value(saved.state, product.flag_name) == 1
        assert saved.state["skin_acquisition_history"]["seen"] == [product.item_id]
        assert display in skins.allowed_displays(product.skin_card_id, saved.state, smartphone_images=False)
        skin_tasks = {row.condition_id for row in bundle.missions.catalog.definitions if row.condition_type == 11}
        progress = {row["ID"]: row for row in bundle.missions.progress.snapshot(saved.state, NOW)}
        assert all(progress[identifier]["Progress"] == 1 for identifier in skin_tasks)
        _, loaded = post(client, request(store, 137, ()))
        assert set(loaded["GF"].children[0].values) == {"UnrelatedFlag", product.flag_name}
        assert post(client, envelope)[0] == wire and store.profile() == saved
    reopened = application(local, tmp_path)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == saved


@pytest.mark.parametrize("existing", ["ownership", "positive_flag", "negative_flag"])
def test_existing_ownership_or_any_nonzero_purchase_flag_prevents_payment(local, tmp_path, existing):
    if existing == "ownership":
        local.state = local.bundle.possessions.credit(local.state, {local.product.item_id: 1})
    else:
        local.state = set_flags(local.state, {local.product.flag_name: 1 if existing == "positive_flag" else -1})
    app = application(local, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        response = post(client, buy(local, app.state.store), 409)
        assert response.json()["error"]["code"] == "skin_shop_already_owned"
        assert app.state.store.profile() == before


def test_expired_offer_and_missing_item_never_take_crystals(local, tmp_path):
    expired = next(product for product in local.service.catalog.products
                   if product.supported and not local.service.catalog.is_open(product, NOW))
    missing = [product for product in local.service.catalog.products if not product.supported]
    assert len(missing) == 2
    app = application(local, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        response = post(client, buy(local, app.state.store, expired), 409)
        assert response.json()["error"]["code"] == "skin_shop_closed"
        for product in missing:
            response = post(client, buy(local, app.state.store, product), 501)
            assert response.json()["error"]["code"] == "skin_shop_product_unsupported"
        assert app.state.store.profile() == before


def test_current_catalog_has_eight_grantable_open_offers_and_two_missing(local):
    opened = [product for product in local.service.catalog.products if local.service.catalog.is_open(product, NOW)]
    assert len(opened) == 10
    assert sum(product.supported for product in opened) == 8
    assert any(product.shop_id < 0 for product in local.service.catalog.products)


def test_missing_gf_provider_rejects_nonempty_flags_instead_of_silently_dropping_them(local, tmp_path):
    local.bootstrap.universal_flag_provider = None
    app = application(local, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        response = post(client, buy(local, app.state.store), 503)
        assert response.json()["error"]["code"] != "internal_error"
        assert app.state.store.profile() == before


def test_gf_provider_rejects_wrong_group_without_mutating_the_save(local, tmp_path):
    from aigis_ps.alxml import Container
    local.bootstrap.universal_flag_provider = lambda state: Container("Wrong")
    app = application(local, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        response = post(client, request(app.state.store, 137, ()), 503)
        assert response.json()["error"]["code"] == "universal_flag_provider_invalid"
        assert app.state.store.profile() == before
