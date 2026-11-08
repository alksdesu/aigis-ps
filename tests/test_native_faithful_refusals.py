from collections import Counter

import pytest
from fastapi.testclient import TestClient

from aigis_ps import cli
from aigis_ps.app import create_app
from aigis_ps.protocol import ProtocolRegistry
from aigis_ps.shop_window_closure import SELECTION
from test_evolution_integration import post
from test_quest_service import bundle, golden_bundle, request


REFUSAL_DECISIONS = {
    # Never sent by this client: 86 lacks a stamina quest or shop-top 1001 button, gift menus override 99, 144/145 are dead login paths.
    "native_unreachable": (86, 99, 144, 145),
    # No direct A7FDF0 call site or dynamic number path of this client produces these.
    "no_native_sender": (11, 28, 30, 37, 40, 48, 49, 52, 53, 56, 61, 92, 93, 94, 95, 96, 97, 98,
                         131, 132, 133, 134, 140, 141),
    # Only coop subjugations send these, none is enabled, and their defeat count is a server-wide total.
    "coop_subjugation": (143, 147),
    # The native classic class-change eligibility chain always returns false.
    "classic_class_change": (6,),
    # The native capacity formula never reads purchased slots, so 16 could only take crystals for nothing.
    "unit_slot_purchase": (16,),
    # The shop top's boost commands are empty and no entry to the old blessing shop is verified.
    "timed_blessing_shop": (18, 19, 20, 87, 88, 89, 90),
    # The payment chain runs only for Price > 0, every local package is free, and a payment is never faked.
    "paid_purchase": (91, 154),
    # 70 answers no lineup, so the summon page has no button that sends 12 or 13.
    "empty_summon_schedule": (12, 13),
    # The served shop table ends the sale window of every product whose purchase sends 108.
    "closed_shop_summon": (108,),
}
REFUSALS = [pytest.param(number, id=f"{decision}-{number}")
            for decision, numbers in REFUSAL_DECISIONS.items() for number in numbers]


@pytest.fixture(scope="module")
def served(bundle, tmp_path_factory):
    captured = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli, "services", lambda _: bundle)
        patch.setattr(cli, "client_fingerprint", lambda _: bundle.masters.expected_hash)
        patch.setattr("aigis_ps.app.create_app", lambda *args, **kwargs: captured.update(kwargs))
        patch.setattr("uvicorn.run", lambda *args, **kwargs: None)
        assert cli.main(["serve", "--data-dir", str(tmp_path_factory.mktemp("serve") / "unused")]) == 0
    return captured


def test_every_refused_registration_has_exactly_one_decision():
    decided = Counter(number for numbers in REFUSAL_DECISIONS.values() for number in numbers)
    assert [number for number, decisions in decided.items() if decisions > 1] == []


def test_every_registration_served_without_a_handler_has_a_decision(served):
    handlers = served["handlers"]
    unhandled = {number for number, profile in ProtocolRegistry.load().by_registration.items()
                 if not set(profile.registration_keys) & set(handlers) and profile.name not in handlers}
    assert unhandled == {number for numbers in REFUSAL_DECISIONS.values() for number in numbers}


@pytest.mark.parametrize("number", REFUSALS)
def test_refused_registration_has_no_handler_on_its_wire(served, number):
    profile = ProtocolRegistry.load().by_registration[number]
    assert not set(profile.registration_keys) & set(served["handlers"])


@pytest.mark.parametrize("number", REFUSALS)
def test_refused_registration_answers_not_implemented_without_touching_the_save(served, tmp_path, number):
    app = create_app(tmp_path, **served)
    store = app.state.store
    before = store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = post(client, request(store, number, ()), 501)
    assert response.json()["error"]["code"] == "not_implemented"
    assert store.profile() == before


def test_coop_subjugation_refusal_holds_while_no_cooperative_mission_is_listed(bundle):
    # 143 and 147 go out as a selected cooperative mission menu loads, so refusing them is safe while none is listed.
    assert bundle.quests.subjugation.status()["cooperative"] == []


def test_the_served_shop_table_ends_the_sale_window_of_every_shop_summon(bundle):
    closure, now = bundle.shop_window_closure, bundle.bootstrap.clock()
    summons = [product for product in bundle.trades.catalog.products
               if (product.product_type, product.trade_type) == (SELECTION["ProductType"], SELECTION["Type"])]
    assert closure["registration"] in REFUSAL_DECISIONS["closed_shop_summon"] and summons
    assert sorted(closed["shop_id"] for closed in closure["products"]) == sorted(product.shop_id for product in summons)
    for product in summons:
        assert product.start_at < product.end_at <= now and not product.ordinary


def test_trade_receipts_committed_before_the_shop_window_closure_still_replay(bundle, golden_bundle):
    catalog, unclosed = bundle.trades.catalog, golden_bundle.trades.catalog
    assert unclosed.content_version != catalog.content_version
    assert catalog.replayable_content_versions() == {catalog.content_version, unclosed.content_version}
