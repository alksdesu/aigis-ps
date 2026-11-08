from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
import logging
from types import MappingProxyType, SimpleNamespace

import pytest

import aigis_ps.skin_shop_service as module
from aigis_ps.alxml import Column, Container, loads
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.mission_state import JOURNAL_KEY, SKIN_HISTORY_KEY
from aigis_ps.request_ledger import HISTORY_LIMIT, LEGACY_SCHEMA_VERSION, SCHEMA_VERSION
from aigis_ps.skin_shop_catalog import LEGACY_WINDOW_RULE, SkinShopCatalog
from aigis_ps.skin_shop_service import SkinShopService
from aigis_ps.storage import Store
from aigis_ps.universal_flags import MAX_FLAG_NAME_BYTES, STORED_FLAG_NAME_BYTES, flag_hash, flag_value, set_flags
from test_mission_service_replay import configured
from test_quest_service import bundle, request
from test_tower_service import without_enabled


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


class OpenCatalog:
    def __init__(self, source):
        self.products = tuple(replace(product, start_at=NOW - timedelta(days=1), end_at=NOW + timedelta(days=1))
                              for product in source.products)
        self.by_id = MappingProxyType({product.shop_id: product for product in self.products})
        self.content_version = hashlib.sha256((source.content_version + ":test_windows").encode()).hexdigest()
        self.resource_sha256 = source.status()["resource_sha256"]

    def get(self, shop_id):
        if type(shop_id) is not int or shop_id not in self.by_id:
            raise ProtocolError("skin_shop_product_unknown", "Unknown product")
        return self.by_id[shop_id]

    def is_open(self, product, now):
        return product.start_at <= now <= product.end_at

    def is_ready(self):
        return True

    def replayable_content_versions(self):
        return frozenset((self.content_version,))

    def status(self):
        return {"ready": self.is_ready(), "resource_sha256": self.resource_sha256}


def skin_catalog(bundle):
    return SkinShopCatalog(bundle.possessions, bundle.missions.skins, lambda name: bundle.native_cdn.assets.read(name)[0])


@pytest.fixture(scope="module")
def catalog(bundle):
    return skin_catalog(bundle)


def account(bundle, bootstrap, path):
    state = bundle.player_state.initialize(bootstrap.initial_state())
    state["tables"]["crystals"] = [{"FreeFlag": True, "Date": 20, "Num": 30},
                                    {"FreeFlag": False, "Date": 3, "Num": 90},
                                    {"FreeFlag": True, "Date": 10, "Num": 5}]
    state["sentinel"] = {"nested": ["preserved"]}
    return Store(path, state)


@pytest.fixture
def case(bundle, catalog, tmp_path):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    opened = OpenCatalog(catalog)
    service = SkinShopService(bootstrap, bundle.player_state, bundle.possessions, opened, bundle.missions)
    products = tuple(product for product in opened.products if product.supported)
    return SimpleNamespace(service=service, bundle=bundle, catalog=catalog, products=products,
                           product=products[0], store=account(bundle, bootstrap, tmp_path / "skins.sqlite3"))


def message(case, product=None, *, tick=1000, children=None, number=156):
    product = product or case.product
    children = (Column("ShopId", "I", (product.shop_id,)),) if children is None else children
    return request(case.store, number, (Container("SKIN", children),), tick=tick)


def nodes(payload):
    return {node.name: node for node in loads(payload).children}


def fail(case, envelope, code=None):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.purchase(envelope, case.store)
    if code is not None:
        assert caught.value.code == code
    assert case.store.profile() == before
    return caught.value


def change(case, callback):
    before = case.store.profile()
    updated = deepcopy(before.state)
    callback(updated)
    case.store.update_state(updated, before.revision)


def test_catalog_identity_and_successful_atomic_purchase(case):
    assert len(case.catalog.products) == 38
    assert case.service.handlers() == {156: case.service.purchase}
    assert case.service.status()["ready"]
    before = case.store.profile()
    reply = nodes(case.service.purchase(message(case), case.store))
    after = case.store.profile()
    assert after.revision == before.revision + 1
    assert wallet_totals(before.state)["total"] - wallet_totals(after.state)["total"] == case.product.price
    assert set(reply) == {"SYSTEM", "PFC", "PPC", "POSSESSION", "GF", "SkinShopPurchase"}
    assert not reply["SkinShopPurchase"].children
    assert reply["POSSESSION"].children == (Column("ItemID", "S", (case.product.item_id,)), Column("Count", "I", (1,)),
                                            Column("Enabled", "I", (0,)))
    assert reply["GF"].children == (Column("ID", "S", (case.product.flag_name,)), Column("Value", "I", (1,)))
    assert flag_hash(case.product.flag_name) == case.product.purchase_flag
    assert flag_value(after.state, case.product.flag_name) == 1
    assert after.state["player_state"] == before.state["player_state"]
    assert after.state["sentinel"] == before.state["sentinel"]
    assert "mission_state" in after.state
    assert case.product.item_id in after.state["skin_acquisition_history"]["seen"]
    assert "trade_history" not in after.state and "trade_receipts" not in after.state


def test_free_then_paid_and_oldest_dates_with_full_wallet_response(case):
    change(case, lambda state: state["tables"].update(crystals=[
        {"FreeFlag": False, "Date": 8, "Num": 9}, {"FreeFlag": True, "Date": 5, "Num": 4},
        {"FreeFlag": False, "Date": 2, "Num": 10}, {"FreeFlag": True, "Date": 1, "Num": 3}]))
    reply = nodes(case.service.purchase(message(case), case.store))
    assert case.product.price == 15
    assert [row["Num"] for row in case.store.profile().state["tables"]["crystals"]] == [9, 0, 2, 0]
    assert reply["PFC"].children == (Column("UserID", "I", (1, 1)), Column("Date", "I", (5, 1)), Column("Num", "I", (0, 0)))
    assert reply["PPC"].children == (Column("UserID", "I", (1, 1)), Column("Date", "I", (8, 2)), Column("Num", "I", (9, 2)))


def test_exact_balance_can_reach_zero_and_empty_paid_snapshot_is_explicit(case):
    change(case, lambda state: state["tables"].update(crystals=[{"FreeFlag": True, "Date": 1, "Num": 15}]))
    reply = nodes(case.service.purchase(message(case), case.store))
    assert wallet_totals(case.store.profile().state)["total"] == 0
    assert reply["PPC"].children == tuple(Column(name, "I", ()) for name in ("UserID", "Date", "Num"))


@pytest.mark.parametrize("kind", ("item", "positive_flag", "negative_flag"))
def test_duplicate_ownership_never_charges(case, kind):
    if kind == "item":
        change(case, lambda state: state["tables"]["possessions"].append({"ItemID": case.product.item_id, "Count": 1}))
    else:
        profile = case.store.profile()
        state = set_flags(profile.state, {case.product.flag_name: 3 if kind == "positive_flag" else -1})
        case.store.update_state(state, profile.revision)
    fail(case, message(case), "skin_shop_already_owned")


def test_second_new_request_for_same_product_is_rejected(case):
    case.service.purchase(message(case), case.store)
    fail(case, message(case, tick=1001), "skin_shop_already_owned")


def test_zero_history_and_zero_possession_can_be_purchased(case):
    profile = case.store.profile()
    state = set_flags(profile.state, {case.product.flag_name: 0, "OtherFeatureFlag": -9})
    state["tables"]["possessions"].append({"ItemID": case.product.item_id, "Count": 0, "Enabled": 0})
    case.store.update_state(state, profile.revision)
    reply = nodes(case.service.purchase(message(case), case.store))
    assert reply["POSSESSION"].children[-1] == Column("Enabled", "I", (0,))
    assert flag_value(case.store.profile().state, "OtherFeatureFlag") == -9


def test_insufficient_crystals_preserves_everything(case):
    change(case, lambda state: state["tables"].update(crystals=[{"FreeFlag": True, "Date": 1, "Num": 14}]))
    fail(case, message(case), "crystal_insufficient")


@pytest.mark.parametrize("children", [(), (Column("ShopId", "S", ("1",)),), (Column("ShopId", "I", ()),),
    (Column("ShopId", "I", (1, 2)),), (Column("ShopID", "I", (1,)),),
    (Column("ShopId", "I", (1,)), Column("NUM", "I", (1,))),
    (Column("ShopId", "I", (1,)), Column("Price", "I", (0,))),
    (Column("ShopId", "I", (1,)), Column("ShopId", "I", (2,)))])
def test_strict_business_fields(case, children):
    fail(case, message(case, children=children), "invalid_request_fields")


@pytest.mark.parametrize("kind", ("unknown_root", "missing_skin", "duplicate_skin", "mixed", "typed", "wrong_profile"))
def test_strict_root_and_container(case, kind):
    if kind == "unknown_root":
        envelope = request(case.store, 156, (Container("SKIN"), Column("NUM", "I", (1,))))
    elif kind == "missing_skin":
        envelope = request(case.store, 156, ())
    elif kind == "duplicate_skin":
        envelope = request(case.store, 156, (Container("SKIN"), Container("SKIN")))
    else:
        envelope = message(case, number=148 if kind == "wrong_profile" else 156)
        if kind in ("mixed", "typed"):
            root = deepcopy(envelope.document)
            if kind == "mixed":
                root.find("SKIN").text = "mixed"
            else:
                root.find("SKIN").set("T", "I")
            envelope = replace(envelope, document=root)
    fail(case, envelope, "invalid_request_fields")


@pytest.mark.parametrize("field", ("UI", "UK", "MK"))
def test_wrong_credentials_never_charge(case, field):
    envelope = message(case)
    root = deepcopy(envelope.document)
    root.find(field + "/V").text = "wrong"
    assert fail(case, replace(envelope, document=root)).status == 403


def test_signed_negative_shop_id_works_and_zero_or_unknown_does_not(case):
    product = next(product for product in case.products if product.shop_id < 0)
    case.service.purchase(message(case, product), case.store)
    for shop_id in (0, 1, -1):
        fail(case, message(case, children=(Column("ShopId", "I", (shop_id,)),)), "skin_shop_product_unknown")


def test_missing_item_products_remain_explicitly_unsupported(case):
    products = [product for product in case.service.catalog.products if not product.supported]
    assert products
    for product in products:
        fail(case, message(case, product), "skin_shop_product_unsupported")


@pytest.mark.parametrize("position,allowed", (("before", False), ("start", True), ("end", True),
                                              ("end_second", True), ("after", False)))
def test_real_catalog_purchase_windows_include_whole_client_clock_seconds(case, tmp_path, position, allowed):
    product = next(product for product in case.catalog.products if product.supported and product.start_at and product.end_at)
    times = {"before": product.start_at - timedelta(seconds=1), "start": product.start_at,
             "end": product.end_at, "end_second": product.end_at + timedelta(milliseconds=999),
             "after": product.end_at + timedelta(seconds=1)}
    # The monthly spending ledger rejects purchases dated before the account's saved instant
    case.service.bootstrap.clock = lambda: times["before"].replace(tzinfo=None)
    case.store = account(case.bundle, case.service.bootstrap, tmp_path / "window.sqlite3")
    case.service.bootstrap.clock = lambda: times[position].replace(tzinfo=None)
    case.service = SkinShopService(case.service.bootstrap, case.bundle.player_state, case.bundle.possessions, case.catalog, case.bundle.missions)
    if allowed:
        case.service.purchase(message(case, product), case.store)
    else:
        fail(case, message(case, product), "skin_shop_closed")


def test_exact_retry_after_restart_and_closed_window(case):
    envelope = message(case)
    response = case.service.purchase(envelope, case.store)
    before = case.store.profile()
    reopened = Store(case.store.path)
    case.service.bootstrap.clock = lambda: NOW + timedelta(days=3)
    rebuilt = SkinShopService(case.service.bootstrap, case.bundle.player_state, case.bundle.possessions,
                             case.service.catalog, case.bundle.missions)
    assert rebuilt.purchase(envelope, reopened) == response
    assert reopened.profile() == before


def test_two_column_inventory_receipt_replays_its_saved_bytes(case):
    envelope = message(case)
    payload = case.service.purchase(envelope, case.store)
    legacy = without_enabled(payload)
    assert legacy != payload
    assert [column.name for column in nodes(legacy)["POSSESSION"].children] == ["ItemID", "Count"]
    revision = case.store.profile().revision + 1
    change(case, lambda state: state["skin_shop_history"]["receipts"][0].update(response=legacy.decode(),
                                                                                 revision=revision))
    saved = case.store.profile()
    assert case.service.purchase(envelope, case.store) == legacy
    assert case.store.profile() == saved


def test_receipt_replays_after_optional_mission_dependencies_attach(case):
    assert case.bundle.missions.fame is not None
    plain = configured(case.bundle, case.service.bootstrap, ("fame",))
    earlier = SkinShopService(case.service.bootstrap, case.bundle.player_state, case.bundle.possessions,
                              case.service.catalog, plain)
    assert earlier.content_version != case.service.content_version
    envelope = message(case)
    response = earlier.purchase(envelope, case.store)
    saved = case.store.profile()
    attached = SkinShopService(case.service.bootstrap, case.bundle.player_state, case.bundle.possessions,
                               case.service.catalog, case.bundle.missions)
    assert attached.purchase(envelope, case.store) == response
    assert case.store.profile() == saved
    change(case, lambda state: state["skin_shop_history"]["receipts"][0].update(content="f" * 64,
                                                                                 revision=saved.revision + 1))
    fail(case, envelope, "skin_shop_retry_stale")


@pytest.mark.parametrize("detached", [(), ("fame",)], ids=["same_missions", "fame_attached_later"])
def test_receipt_of_the_jst_window_rule_replays_its_saved_bytes(case, monkeypatch, detached):
    with monkeypatch.context() as patch:
        patch.setattr("aigis_ps.skin_shop_catalog.WINDOW_RULE", LEGACY_WINDOW_RULE)
        legacy = skin_catalog(case.bundle)
    assert legacy.content_version in case.catalog.replayable_content_versions() - {case.catalog.content_version}
    missions = configured(case.bundle, case.service.bootstrap, detached) if detached else case.bundle.missions
    earlier = SkinShopService(case.service.bootstrap, case.bundle.player_state, case.bundle.possessions,
                              legacy, missions)
    case.service = SkinShopService(case.service.bootstrap, case.bundle.player_state, case.bundle.possessions,
                                   case.catalog, case.bundle.missions)
    assert earlier.content_version != case.service.content_version
    product = next(product for product in case.catalog.products if product.supported and case.catalog.is_open(product, NOW))
    envelope = message(case, product)
    response = earlier.purchase(envelope, case.store)
    saved = case.store.profile()
    assert saved.state["skin_shop_history"]["receipts"][0]["content"] == earlier.content_version
    assert case.service.purchase(envelope, case.store) == response
    assert case.store.profile() == saved
    change(case, lambda state: state["skin_shop_history"]["receipts"][0].update(content="f" * 64,
                                                                                 revision=saved.revision + 1))
    fail(case, envelope, "skin_shop_retry_stale")


def test_mission_hook_fields_are_the_mission_journal_and_skin_history():
    assert module.MISSION_FIELDS == {JOURNAL_KEY, SKIN_HISTORY_KEY} == {"mission_state", "skin_acquisition_history"}


def test_changed_revision_or_dependency_rejects_replay(case, monkeypatch):
    envelope = message(case)
    case.service.purchase(envelope, case.store)
    monkeypatch.setattr(case.service.catalog, "content_version", "f" * 64)
    fail(case, envelope, "skin_shop_dependencies_changed")
    monkeypatch.undo()
    change(case, lambda state: state["sentinel"]["nested"].append("later"))
    fail(case, envelope, "skin_shop_retry_stale")


@pytest.mark.parametrize("kind", ("missing_skins", "missing_counted_item", "wrong_progress_items", "wrong_package"))
def test_acquisition_dependencies_are_required_even_if_mission_status_is_ready(case, monkeypatch, kind):
    missions = case.bundle.missions
    if kind == "missing_skins":
        monkeypatch.setattr(missions, "skins", None)
    elif kind == "missing_counted_item":
        monkeypatch.setattr(missions.skins, "_counted_item_ids", missions.skins.counted_item_ids - {case.product.item_id})
    elif kind == "wrong_progress_items":
        monkeypatch.setattr(missions.progress, "_skin_items", frozenset())
    else:
        case.service.catalog.resource_sha256 = {**case.service.catalog.resource_sha256, "SkinPackage.atb": "f" * 64}
    monkeypatch.setattr(missions, "_ready", lambda: None)
    fail(case, message(case), "skin_shop_dependencies_changed")


def test_readiness_asks_missions_without_building_their_status(case, monkeypatch):
    missions = case.bundle.missions
    monkeypatch.setattr(missions, "status", lambda: pytest.fail("Skin-shop readiness built the full mission status"))
    case.service._ready()
    def unready():
        raise ProtocolError("mission_dependencies_changed", "Mission dependencies changed after setup", 503)
    monkeypatch.setattr(missions, "_ready", unready)
    assert (case.service.status()["ready"], case.service.status()["error"]) == (False, "skin_shop_dependencies_changed")
    fail(case, message(case), "skin_shop_dependencies_changed")


def test_readiness_asks_catalogs_and_possessions_without_building_their_status(case, monkeypatch):
    missions = case.bundle.missions
    monkeypatch.setattr(missions, "_ready", lambda: None)
    for dependency in (case.service.catalog, missions.skins, case.bundle.possessions):
        monkeypatch.setattr(dependency, "status", lambda: pytest.fail("Skin-shop readiness built a dependency status"))
    case.service._ready()


@pytest.mark.parametrize("dependency", ["catalog", "skins", "possessions"])
def test_any_unready_dependency_refuses_the_purchase(case, monkeypatch, dependency):
    target = {"catalog": case.service.catalog, "skins": case.bundle.missions.skins,
              "possessions": case.bundle.possessions}[dependency]
    monkeypatch.setattr(target, "is_ready", lambda: False)
    assert (case.service.status()["ready"], case.service.status()["error"]) == (False, "skin_shop_dependencies_changed")
    fail(case, message(case), "skin_shop_dependencies_changed")


@pytest.mark.parametrize("part", ("response", "state", "flag_name", "shop_id", "history_null", "processed"))
def test_corrupt_replay_never_returns_unchecked_xml(case, monkeypatch, part):
    envelope = message(case)
    case.service.purchase(envelope, case.store)
    profile = case.store.profile()
    state = deepcopy(profile.state)
    history = state["skin_shop_history"]
    if part == "history_null":
        state["skin_shop_history"] = None
    elif part == "processed":
        history["processed_keys"].insert(0, True)
    elif part == "response":
        history["receipts"][0][part] = "<DA/>"
    elif part == "state":
        history["receipts"][0][part] = "0" * 64
    elif part == "flag_name":
        history["receipts"][0][part] = "WrongFeature"
    else:
        history["receipts"][0][part] = -1
    monkeypatch.setattr(case.store, "profile", lambda: replace(profile, state=state))
    fail(case, envelope, "skin_shop_history_invalid")


@pytest.mark.parametrize("field,value", [("shop_id", 0), ("shop_id", True), ("flag_name", ""), ("flag_name", None)])
def test_retained_receipt_with_invalid_product_identity_is_not_trusted(case, field, value):
    case.service.purchase(message(case), case.store)
    change(case, lambda state: state["skin_shop_history"]["receipts"][0].update({field: value}))
    fail(case, message(case, case.products[1]), "skin_shop_history_invalid")


def test_evicted_receipt_stays_processed_across_signed_client_times(case, monkeypatch):
    change(case, lambda state: state["tables"].update(crystals=[{"FreeFlag": True, "Date": 1, "Num": 1000}]))
    monkeypatch.setattr(case.service, "_preflight", lambda *args: None)
    first = message(case, case.products[0], tick=0x7fffffff)
    case.service.purchase(first, case.store)
    for index, product in enumerate(case.products[1:HISTORY_LIMIT + 2]):
        case.service.purchase(message(case, product, tick=-0x80000000 + index * 86400), case.store)
    history = case.store.profile().state["skin_shop_history"]
    assert history["schema_version"] == SCHEMA_VERSION and len(history["receipts"]) == HISTORY_LIMIT
    assert len(history["processed_keys"]) == HISTORY_LIMIT + 2
    fail(case, first, "skin_shop_request_stale")


def test_client_clock_rollback_still_purchases_another_product(case):
    case.service.purchase(message(case, case.products[0], tick=2000), case.store)
    case.service.purchase(message(case, case.products[1], tick=1000), case.store)
    after = case.store.profile().state
    assert all(flag_value(after, product.flag_name) == 1 for product in case.products[:2])
    assert len(after["skin_shop_history"]["processed_keys"]) == 2


def test_legacy_watermark_receipt_replays_after_upgrade(case):
    envelope = message(case)
    response = case.service.purchase(envelope, case.store)
    receipt = case.store.profile().state["skin_shop_history"]["receipts"][0]
    legacy = {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [{**receipt, "client_time": 1000}],
              "last_client_time": 1000, "seen_keys": [receipt["request"]]}
    state = deepcopy(case.store.profile().state)
    state["skin_shop_history"] = legacy
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state, ensure_ascii=True),))
    assert case.service.purchase(envelope, case.store) == response
    fail(case, message(case, tick=999), "skin_shop_already_owned")


@pytest.mark.parametrize("kind,code", [("owned", "skin_shop_already_owned"), ("malformed", "invalid_request_fields")])
def test_refused_purchase_is_logged_because_the_native_client_resends_it(case, caplog, kind, code):
    envelope = message(case, children=(Column("ShopId", "S", ("1",)),)) if kind == "malformed" else message(case)
    if kind == "owned":
        change(case, lambda state: state["tables"]["possessions"].append({"ItemID": case.product.item_id, "Count": 1}))
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        fail(case, envelope, code)
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        f"Skin shop 156 refused with {code}; the native client shows a communication error and resends the same request until the server accepts it"]


@pytest.mark.parametrize("number", (43, 44, 129, 137, 156))
def test_all_required_encoding_failures_roll_back(case, monkeypatch, number):
    encode = module.encode_response
    def broken(profile, payload):
        if profile.registration_keys == (number,):
            raise ProtocolError("injected_capacity", "Cannot encode response", 503)
        return encode(profile, payload)
    monkeypatch.setattr(module, "encode_response", broken)
    fail(case, message(case), "injected_capacity")


def test_receipt_size_limit_rolls_back(case, monkeypatch):
    monkeypatch.setattr(module, "RESPONSE_LIMIT", 1)
    fail(case, message(case), "skin_shop_response_capacity")


@pytest.mark.parametrize("mode", ("failure", "changes_economy"))
def test_mission_hook_is_atomic_and_cannot_undo_payment(case, monkeypatch, mode):
    def broken(before, updated, now):
        if mode == "failure":
            raise ProtocolError("injected_mission_failure", "Mission unavailable", 503)
        updated["tables"]["crystals"] = deepcopy(before["tables"]["crystals"])
        return updated
    monkeypatch.setattr(case.bundle.missions, "record_acquisitions", broken)
    fail(case, message(case), "injected_mission_failure" if mode == "failure" else "skin_shop_hook_invalid")


def test_cas_retry_preserves_concurrent_state(case, monkeypatch):
    update = case.store.update_state
    calls = 0
    def competing(state, revision):
        nonlocal calls
        calls += 1
        if calls == 1:
            current = case.store.profile()
            altered = deepcopy(current.state)
            altered["sentinel"]["nested"].append("concurrent")
            update(altered, current.revision)
            raise ValueError("Concurrent write")
        return update(state, revision)
    monkeypatch.setattr(case.store, "update_state", competing)
    case.service.purchase(message(case), case.store)
    assert case.store.profile().state["sentinel"]["nested"] == ["preserved", "concurrent"]
    assert calls == 2


def test_three_cas_failures_leave_state_untouched(case, monkeypatch):
    calls = 0
    def broken(*args):
        nonlocal calls
        calls += 1
        raise ValueError("Concurrent write")
    monkeypatch.setattr(case.store, "update_state", broken)
    fail(case, message(case), "profile_changed")
    assert calls == 3


def test_concurrent_identical_purchase_charges_once(case):
    envelope = message(case)
    before = case.store.profile()
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: case.service.purchase(envelope, case.store), range(2)))
    assert replies[0] == replies[1]
    after = case.store.profile()
    assert after.revision == before.revision + 1
    assert wallet_totals(before.state)["total"] - wallet_totals(after.state)["total"] == 15


def long_flag_product(case, size):
    name = "PurchasedSkin_" + "x" * (size - len("PurchasedSkin_"))
    product = replace(case.product, flag_name=name, purchase_flag=flag_hash(name))
    catalog = copy(case.service.catalog)
    catalog.products = tuple(product if item.shop_id == product.shop_id else item for item in catalog.products)
    catalog.by_id = MappingProxyType({item.shop_id: item for item in catalog.products})
    case.service = SkinShopService(case.service.bootstrap, case.bundle.player_state, case.bundle.possessions,
                                   catalog, case.bundle.missions)
    return product


def test_new_flag_name_within_the_native_cell_is_purchasable(case):
    product = long_flag_product(case, MAX_FLAG_NAME_BYTES)
    reply = nodes(case.service.purchase(message(case, product), case.store))
    assert reply["GF"].children == (Column("ID", "S", (product.flag_name,)), Column("Value", "I", (1,)))
    assert flag_value(case.store.profile().state, product.flag_name) == 1


def test_new_flag_name_beyond_the_native_cell_is_unsupported_before_charging(case, caplog):
    product = long_flag_product(case, MAX_FLAG_NAME_BYTES + 1)
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        error = fail(case, message(case, product), "skin_shop_product_unsupported")
    assert error.status == 501 and "native cell" in str(error)
    assert any("skin_shop_product_unsupported" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("size", [MAX_FLAG_NAME_BYTES + 1, STORED_FLAG_NAME_BYTES])
def test_stored_flag_name_beyond_the_native_cell_keeps_updating(case, size):
    product = long_flag_product(case, size)

    def legacy(state):
        state["universal_flags_schema"] = 1
        state["tables"]["universal_flags"].append({"ID": product.flag_name, "Value": 0})
    change(case, legacy)
    envelope = message(case, product)
    response = case.service.purchase(envelope, case.store)
    assert flag_value(case.store.profile().state, product.flag_name) == 1
    assert case.service.purchase(envelope, case.store) == response
