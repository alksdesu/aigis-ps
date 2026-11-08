from argparse import Namespace
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps import cli
from aigis_ps.alxml import Column
from aigis_ps.app import create_app
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.package_service import PackageService
import aigis_ps.package_service as package_module
import aigis_ps.login_reward_service as login_module
from test_evolution_integration import post
from test_quest_service import request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def enabled_bundle(tmp_path_factory):
    rules = tmp_path_factory.mktemp("purchased-packages") / "rules.json"
    rules.write_text(json.dumps({"enabled": True, "campaign_ids": [136], "purchase_limit": 1}), encoding="utf8")
    result = cli.services(Namespace(asset_dir=cli.DEFAULT_ASSETS, data_dir=cli.DEFAULT_DATA,
        resource_dir=cli.DEFAULT_RESOURCES, rules=None, purchased_login_rules=rules,
        event_pass_revival=PROJECT / "config/event-pass-revival.example.json"))
    assert result.purchased_logins is not None, result.purchased_login_error
    assert result.packages is not None, result.package_error
    return result


@pytest.fixture
def package_case(enabled_bundle, tmp_path, monkeypatch):
    bundle = enabled_bundle
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(bundle.bootstrap, "clock", lambda: clock.now)
    monkeypatch.setattr(bundle.login_rewards, "clock", lambda: clock.now)
    state = bundle.initialize(bundle.bootstrap.initial_state())
    handlers = (bundle.bootstrap.handlers() | bundle.presents.handlers() | bundle.packages.handlers()
                | bundle.login_rewards.handlers() | bundle.rank_rewards.handlers())

    def application():
        return create_app(tmp_path / "save", initial_state=state, state_initializer=bundle.initialize, handlers=handlers)

    app = application()
    offer = bundle.purchased_logins.catalog.by_campaign_id[136]
    return SimpleNamespace(bundle=bundle, clock=clock, app=app, store=app.state.store, offer=offer,
                           application=application)


def rows(node):
    return [dict(zip((column.name for column in node.children), values))
            for values in zip(*(column.values for column in node.children))]


def purchase(case, tick=1000, sku=None):
    return request(case.store, 50, (Column("SKU", "S", (case.offer.sku if sku is None else sku,)),), tick=tick)


def login(case, operation=115, tick=1001):
    columns = (Column("SHOP", "I", (0,)), Column("NUM", "I", (0,)))
    if operation == 116:
        columns += (Column("DmmPremiumReceiverId", "I", (0,)),)
    return request(case.store, operation, columns, tick=tick)


def test_two_package_types_share_complete_directory_and_purchase_flags(package_case):
    case = package_case
    premium = case.bundle.packages.catalog.by_normal_id["event_pass_20261015"]
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, listing = post(client, request(case.store, 46, ()))
        indexed = {row["SkuID"]: row for row in rows(listing["PSC"])}
        assert set(indexed) == {case.offer.sku, premium.sku}
        assert indexed[case.offer.sku]["PackageItemType"] == 10
        assert indexed[case.offer.sku]["LoginBonusID"] == 136
        assert all(row["Price"] == 0 for row in indexed.values())
        before = case.store.profile()
        envelope = purchase(case)
        wire, bought = post(client, envelope)
        assert set(bought) == {"SYSTEM", "PPF", "PSR"}
        assert bought["PSR"].children == ()
        assert case.store.profile().state["tables"] == before.state["tables"]
        assert case.store.profile().state["player_state"] == before.state["player_state"]
        saved = case.store.profile()
        assert post(client, envelope)[0] == wire and case.store.profile() == saved
        _, both = post(client, purchase(case, 1001, premium.sku))
        flags = {row["PackageID"]: row["PurchaseFlag"] for row in rows(both["PPF"])}
        assert flags[premium.flag_id] == flags[case.offer.flag_id] == 1
        _, refreshed = post(client, request(case.store, 47, ()))
        assert rows(refreshed["PPF"]) == rows(both["PPF"])
        assert "EBNS" not in refreshed


@pytest.mark.parametrize("operation", [115, 116])
def test_purchase_entitlement_starts_next_day_and_replays_after_restart(package_case, operation):
    case = package_case
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, purchase(case))
        _, same = post(client, login(case, operation))
        assert "EBNS" not in same or 136 not in same["EBNS"].children[0].values
        case.clock.now = datetime(2026, 11, 2, 4, tzinfo=JST)
        envelope = login(case, operation, 1002)
        wire, earned = post(client, envelope)
        result = next(row for row in rows(earned["EBNS"]) if row["LoginBonusID"] == 136)
        assert result == {"LoginBonusID": 136, "LoginCount": 1, "PurchaseTime": "2026/11/01 12:00:00"}
        mails = [row for row in case.bundle.presents.rows(case.store.profile().state)
                 if row["Description"] == "購入パックログインボーナス"]
        assert len(mails) == 1 and mails[0]["ItemID"] == "item_smn_ticket_85143"
    saved = case.store.profile()
    reopened = case.application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
    assert reopened.state.store.profile() == saved


def test_full_five_day_purchased_schedule_and_cross_interface_limit(package_case):
    case = package_case
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, purchase(case))
        for ordinal in range(1, 6):
            case.clock.now = NOW + timedelta(days=ordinal)
            primary, secondary = (115, 116) if ordinal % 2 else (116, 115)
            _, result = post(client, login(case, primary, 1000 + ordinal * 2))
            row = next(row for row in rows(result["EBNS"]) if row["LoginBonusID"] == 136)
            assert row["LoginCount"] == ordinal
            _, second = post(client, login(case, secondary, 1001 + ordinal * 2))
            assert "EBNS" not in second
        case.clock.now += timedelta(days=1)
        _, ended = post(client, login(case, 115, 1200))
        assert "EBNS" not in ended
    mails = [row for row in case.bundle.presents.rows(case.store.profile().state)
             if row["Description"] == "購入パックログインボーナス"]
    assert len(mails) == 5
    saved = case.store.profile()
    assert case.application().state.store.profile() == saved


@pytest.mark.parametrize("registration", [46, 47, 50, 129, 137])
def test_purchase_encoding_failure_does_not_leave_entitlement(package_case, monkeypatch, registration):
    case = package_case
    original = package_module.encode_response

    def encode(profile, payload):
        if registration in profile.registration_keys:
            raise ProtocolError("fixture_purchase_capacity", "Response capacity failed", 503)
        return original(profile, payload)

    monkeypatch.setattr(package_module, "encode_response", encode)
    before = case.store.profile()
    with pytest.raises(ProtocolError):
        case.bundle.packages.purchase(purchase(case), case.store)
    assert case.store.profile() == before


def test_later_purchased_reward_failure_rolls_back_all_login_sources(package_case, monkeypatch):
    case = package_case
    case.bundle.packages.purchase(purchase(case), case.store)
    case.clock.now += timedelta(days=1)
    before = case.store.profile()
    monkeypatch.setattr(case.bundle.purchased_logins, "settle", lambda *args, **kwargs: (_ for _ in ()).throw(
        ProtocolError("purchased_login_state_invalid", "Invalid purchase history", 503)))
    with pytest.raises(ProtocolError):
        case.bundle.login_rewards.receive(login(case), case.store)
    assert case.store.profile() == before


@pytest.mark.parametrize("domain,field", [("purchase", "purchase_id"), ("purchase", "campaign_id"),
    ("login", "purchase_id"), ("login", "purchase_time")])
def test_purchase_sources_cannot_be_reassigned_in_receipts(package_case, domain, field):
    case = package_case
    envelope = purchase(case)
    case.bundle.packages.purchase(envelope, case.store)
    if domain == "login":
        case.clock.now += timedelta(days=1)
        envelope = login(case)
        case.bundle.login_rewards.receive(envelope, case.store)
    profile = case.store.profile()
    state = deepcopy(profile.state)
    if domain == "purchase":
        entry = state["package_history"]["receipts"][-1]
        entry["identity"][field] += 1
        entry["state"] = package_module._state_digest(state)
        callback = case.bundle.packages.purchase
    else:
        entry = state["login_reward_history"]["receipts"][-1]
        value = 2 if field == "purchase_id" else "2026/10/01 12:00:00"
        entry["sources"]["purchased_login_grants"][0][field] = value
        state["login_reward_delivery"]["purchased_login_grants"][0][field] = value
        entry["state"] = login_module._state_digest(state)
        callback = case.bundle.login_rewards.receive
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    before = case.store.profile()
    with pytest.raises(ProtocolError):
        callback(envelope, case.store)
    assert case.store.profile() == before


def test_premium_purchase_before_daily_package_keeps_both_flags(package_case):
    case = package_case
    premium = case.bundle.packages.catalog.by_normal_id["event_pass_20261015"]
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, purchase(case, sku=premium.sku))
        _, response = post(client, purchase(case, 1001))
        flags = {row["PackageID"]: row["PurchaseFlag"] for row in rows(response["PPF"])}
        assert flags[premium.flag_id] == flags[case.offer.flag_id] == 1
        before = case.store.profile()
        rejected = post(client, purchase(case, 1002), expected=409)
        assert rejected.json()["error"]["code"] == "purchased_login_purchase_limit"
        assert case.store.profile() == before


def test_legacy_premium_receipt_survives_installing_daily_package_service(package_case):
    case = package_case
    current = case.bundle.packages
    old = PackageService(current.bootstrap, current.players, current.possessions, current.catalog)
    premium = current.catalog.by_normal_id["event_pass_20261015"]
    envelope = purchase(case, sku=premium.sku)
    wire = old.purchase(envelope, case.store)
    before = case.store.profile()
    assert current.purchase(envelope, case.store) == wire
    assert case.store.profile() == before
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, refreshed = post(client, request(case.store, 47, ()))
        flags = {row["PackageID"]: row["PurchaseFlag"] for row in rows(refreshed["PPF"])}
        assert flags[premium.flag_id] == 1 and flags[case.offer.flag_id] == 0
