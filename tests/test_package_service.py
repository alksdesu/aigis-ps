from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from datetime import timedelta
import json
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.errors import ProtocolError
import aigis_ps.event_pass_catalog as event_pass_catalog
from aigis_ps.event_pass_catalog import ELIGIBILITY_RULE, ITEM_RESOURCE_NAME, LEGACY_ELIGIBILITY_RULE, EventPassCatalog
from aigis_ps.package_catalog import PackageCatalog
import aigis_ps.package_service as module
from aigis_ps.package_service import PackageService
from aigis_ps.package_state import IDENTITY_UNREPLAYABLE
from aigis_ps.present_items import PresentItemCatalog
from aigis_ps.purchased_login_service import PurchasedLoginService
from aigis_ps.request_ledger import LEGACY_SCHEMA_VERSION, SCHEMA_VERSION
from aigis_ps.storage import Store
from test_event_pass_revival_cli import revived
from test_purchased_package_flow import NOW as PURCHASED_NOW, enabled_bundle
from test_quest_service import request
from test_tower_service import without_enabled


PASS_ID = "event_pass_20261015"
LOGIN_CAMPAIGN = 136
FORGED = "f" * 64
# A purchased login version no committed gift box produced.
FOREIGN = "5" * 64


@pytest.fixture
def package_factory(revived, tmp_path):
    sequence = 0
    def build(*, owned=False, initialize=True):
        nonlocal sequence
        sequence += 1
        assert revived.packages is not None, revived.package_error
        catalog = revived.packages.catalog
        offer = catalog.by_normal_id[PASS_ID]
        bootstrap = copy(revived.bootstrap)
        clock = SimpleNamespace(now=offer.start_at + timedelta(seconds=1), calls=0)
        def current_time():
            clock.calls += 1
            return clock.now
        bootstrap.clock = current_time
        service = PackageService(bootstrap, revived.player_state, revived.possessions, catalog)
        state = revived.initialize(bootstrap.initial_state())
        for key in ("package_purchases", "package_history", "package_delivery"):
            state.pop(key, None)
        if owned:
            state = revived.possessions.credit(state, {offer.item_id: 1})
        if initialize:
            state = service.initialize(state)
        state["package_test_sentinel"] = {"preserve": [1, 2]}
        clock.calls = 0
        return SimpleNamespace(bundle=revived, bootstrap=bootstrap, service=service, offer=offer,
            catalog=catalog, clock=clock, initial=state, store=Store(tmp_path / str(sequence) / "state.sqlite3", state))
    return build


def purchase_request(case, *, tick=1000, columns=None):
    return request(case.store, 50, columns if columns is not None else (Column("SKU", "S", (case.offer.sku,)),), tick=tick)


def nodes(payload):
    return {node.name: node for node in loads(payload).children}


def rows(node):
    return [dict(zip((column.name for column in node.children), values))
            for values in zip(*(column.values for column in node.children))]


def inventory(case):
    return case.bundle.possessions.counts(case.store.profile().state)


def reject(case, callback, *, code=None, status=None):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert case.store.profile() == before
    if code is not None:
        assert caught.value.code == code
    if status is not None:
        assert caught.value.status == status
    return caught.value


@pytest.mark.parametrize("number", [46, 47])
@pytest.mark.parametrize("owned", [False, True])
def test_read_catalog_and_purchase_flags_do_not_write_state(package_factory, number, owned):
    case = package_factory(owned=owned, initialize=False)
    before = case.store.profile()
    response = case.service.handlers()[number](request(case.store, number, ()), case.store)
    result = nodes(response)
    assert {"SYSTEM", "PSC", "PPF", "POSSESSION"} <= set(result)
    assert ("PSR" in result) is (number == 47)
    if number == 47:
        assert not result["PSR"].children
    selected = [row for row in rows(result["PSC"]) if row["PackageID"] == case.offer.package_id]
    assert len(selected) == 1 and selected[0]["Price"] == 0
    assert selected[0]["PackageItemType"] == 9 and selected[0]["ItemID"] == case.offer.item_id
    flag = next(row for row in rows(result["PPF"]) if row["PackageID"] == case.offer.flag_id)
    assert flag["PurchaseFlag"] == int(owned)
    assert case.store.profile() == before


@pytest.mark.parametrize("number", [46, 47])
def test_public_read_response_is_the_reply_of_the_read_handlers(package_factory, number):
    case = package_factory()
    case.service.purchase(purchase_request(case), case.store)
    reply = case.service.handlers()[number](request(case.store, number, ()), case.store)
    profile = case.store.profile()
    assert case.service.read_response(number, profile, profile.state, case.clock.now) == reply
    assert case.store.profile() == profile


def test_readiness_never_builds_the_possession_status(package_factory, monkeypatch):
    case = package_factory()
    possessions = case.bundle.possessions
    monkeypatch.setattr(possessions, "status", lambda: pytest.fail("readiness must not build the possession status"))
    assert case.service.status()["ready"] is True
    reply = nodes(case.service.read(request(case.store, 46, ()), case.store))
    assert any(row["PackageID"] == case.offer.package_id for row in rows(reply["PSC"]))
    monkeypatch.setattr(possessions, "issues", {"item_definitions": "unavailable"})
    assert case.service.status()["ready"] is False
    reject(case, lambda: case.service.read(request(case.store, 46, ()), case.store), status=503)


def test_purchase_only_credits_entitlement_and_persists_exact_receipt(package_factory):
    case = package_factory()
    before = case.store.profile()
    response = case.service.purchase(purchase_request(case), case.store)
    after = case.store.profile()
    result = nodes(response)
    assert set(result) == {"SYSTEM", "PPF", "POSSESSION", "PSR"}
    assert not result["PSR"].children
    assert rows(result["POSSESSION"]) == [{"ItemID": case.offer.item_id, "Count": 1, "Enabled": 0}]
    assert after.revision == before.revision + 1
    assert inventory(case)[case.offer.item_id] == 1
    changed = {key for key in before.state.keys() | after.state.keys() if before.state.get(key) != after.state.get(key)}
    assert changed == {"tables", "package_purchases", "package_history", "package_delivery"}
    assert {key for key in before.state["tables"] if before.state["tables"][key] != after.state["tables"][key]} == {"possessions"}
    receipt = after.state["package_history"]["receipts"][0]
    assert receipt["response"] == response.decode() and receipt["revision"] == after.revision


def test_reopened_exact_retry_never_reads_clock_or_regrants(package_factory):
    case = package_factory()
    envelope = purchase_request(case)
    response = case.service.purchase(envelope, case.store)
    before = case.store.profile()
    store = Store(case.store.path)
    case.bootstrap.clock = lambda: pytest.fail("Exact retry consulted current time")
    reopened = PackageService(case.bootstrap, case.bundle.player_state, case.bundle.possessions, case.catalog)
    assert reopened.purchase(envelope, store) == response
    assert store.profile() == before


@pytest.mark.parametrize("owned", [False, True])
def test_owned_or_already_purchased_entitlement_cannot_be_acquired_again(package_factory, owned):
    case = package_factory(owned=owned)
    if not owned:
        case.service.purchase(purchase_request(case), case.store)
    reject(case, lambda: case.service.purchase(purchase_request(case, tick=1001), case.store),
           code="package_already_purchased", status=409)


@pytest.mark.parametrize("boundary", ["before", "start", "before_end", "end", "after"])
def test_purchase_window_includes_start_and_excludes_end(package_factory, boundary):
    case = package_factory()
    case.clock.now = {"before": case.offer.start_at - timedelta(microseconds=1),
        "start": case.offer.start_at, "before_end": case.offer.end_at - timedelta(microseconds=1),
        "end": case.offer.end_at, "after": case.offer.end_at + timedelta(seconds=1)}[boundary]
    if boundary in ("start", "before_end"):
        case.service.purchase(purchase_request(case), case.store)
        assert inventory(case)[case.offer.item_id] == 1
    else:
        reject(case, lambda: case.service.purchase(purchase_request(case), case.store), code="package_unavailable")


@pytest.mark.parametrize("columns,status", [([], 400), ([Column("SKU", "I", (1,))], 400),
    ([Column("SKU", "S", ())], 400), ([Column("SKU", "S", ("x", "y"))], 400),
    ([Column("SKU", "S", ("",))], 400), ([Column("SKU", "S", ("unknown",))], 404),
    ([Column("SKU", "S", ("x",)), Column("NUM", "I", (1,))], 400)])
def test_invalid_request_shapes_never_change_state(package_factory, columns, status):
    case = package_factory()
    reject(case, lambda: case.service.purchase(purchase_request(case, columns=columns), case.store), status=status)


@pytest.mark.parametrize("registration", [46, 47, 50, 129, 137])
def test_every_encoded_response_failure_rolls_back_entire_purchase(package_factory, monkeypatch, registration):
    case = package_factory()
    original = module.encode_response
    def fail(profile, payload):
        if profile is case.service._profiles[registration]:
            raise ProtocolError("fixture_encoding_failed", "Expected serialization failure", 503)
        return original(profile, payload)
    monkeypatch.setattr(module, "encode_response", fail)
    reject(case, lambda: case.service.purchase(purchase_request(case), case.store), code="fixture_encoding_failed")


def test_wrong_startup_inventory_provider_fails_before_commit(package_factory, monkeypatch):
    case = package_factory()
    monkeypatch.setattr(case.bootstrap, "_possessions", lambda _: Container("POSSESSION"))
    reject(case, lambda: case.service.purchase(purchase_request(case), case.store), code="package_provider_inconsistent")


def test_cas_conflict_reloads_other_changes_and_does_not_duplicate_grant(package_factory, monkeypatch):
    case = package_factory()
    original = case.store.update_state
    calls = []
    def race(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            profile = case.store.profile()
            concurrent = deepcopy(profile.state)
            concurrent["independent_change"] = 42
            original(concurrent, profile.revision)
            raise ValueError("Concurrent write")
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", race)
    case.service.purchase(purchase_request(case), case.store)
    assert len(calls) == 2 and case.store.profile().state["independent_change"] == 42
    assert inventory(case)[case.offer.item_id] == 1
    assert len(case.store.profile().state["package_history"]["receipts"]) == 1


def test_cas_exhaustion_does_not_commit_candidate(package_factory, monkeypatch):
    case = package_factory()
    monkeypatch.setattr(case.store, "update_state", lambda *_: (_ for _ in ()).throw(ValueError("Conflict")))
    reject(case, lambda: case.service.purchase(purchase_request(case), case.store), code="profile_changed")


def test_concurrent_same_request_has_one_commit_and_identical_response(package_factory, monkeypatch):
    case = package_factory()
    envelope = purchase_request(case)
    barrier, lock, calls = Barrier(2), Lock(), []
    original = case.store.update_state
    def synchronize(state, revision):
        with lock:
            calls.append(revision)
            first_two = len(calls) <= 2
        if first_two:
            barrier.wait(timeout=10)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", synchronize)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: case.service.purchase(envelope, case.store), range(2)))
    assert replies[0] == replies[1] and case.store.profile().revision == 1
    assert inventory(case)[case.offer.item_id] == 1


@pytest.mark.parametrize("part", ["marker_count", "marker_identity", "ledger_count", "ledger_identity", "inventory", "response"])
def test_recomputed_digest_does_not_hide_corrupt_receipt_facts(package_factory, part):
    case = package_factory()
    envelope = purchase_request(case)
    case.service.purchase(envelope, case.store)
    state = case.store.profile().state
    if part == "marker_count":
        state["package_delivery"]["after"] = 2
    elif part == "marker_identity":
        state["package_delivery"]["sku"] = "different"
    elif part == "ledger_count":
        state["package_purchases"]["entries"][PASS_ID]["count"] = 0
    elif part == "ledger_identity":
        state["package_purchases"]["entries"][PASS_ID]["identity"] = "0" * 64
    elif part == "inventory":
        next(row for row in state["tables"]["possessions"] if row["ItemID"] == case.offer.item_id)["Count"] = 0
    else:
        state["package_history"]["receipts"][0]["response"] = dumps(Container(children=(Container("SYSTEM"),))).decode()
    state["package_history"]["receipts"][0]["state"] = module._state_digest(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    reject(case, lambda: case.service.purchase(envelope, case.store), status=503)


def overwrite(case, state):
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def test_watermark_receipts_nest_their_identity_and_still_replay(package_factory):
    case = package_factory()
    envelope = purchase_request(case, tick=2000)
    response = case.service.purchase(envelope, case.store)
    state = case.store.profile().state
    history = state["package_history"]
    state["package_history"] = {"schema_version": 1, "last_client_time": 2000,
        "seen_keys": list(history["processed_keys"]),
        "receipts": [{**{key: value for key, value in entry.items() if key != "identity"}, **entry["identity"],
                      "client_time": 2000} for entry in history["receipts"]]}
    overwrite(case, state)
    saved = case.store.profile()
    assert case.service.purchase(envelope, case.store) == response
    assert case.store.profile() == saved
    upgraded = case.service._receipts(saved.state, saved.revision)
    assert upgraded["schema_version"] == 2 and upgraded["receipts"][0]["identity"] == history["receipts"][0]["identity"]
    reject(case, lambda: case.service.purchase(purchase_request(case, tick=1000), case.store),
           code="package_already_purchased")


def test_two_column_inventory_receipt_replays_its_saved_bytes(package_factory):
    case = package_factory()
    envelope = purchase_request(case)
    payload = case.service.purchase(envelope, case.store)
    legacy = without_enabled(payload)
    assert legacy != payload
    assert [column.name for column in nodes(legacy)["POSSESSION"].children] == ["ItemID", "Count"]
    state = case.store.profile().state
    state["package_history"]["receipts"][0]["response"] = legacy.decode()
    overwrite(case, state)
    saved = case.store.profile()
    assert case.service.purchase(envelope, case.store) == legacy
    assert case.store.profile() == saved


@pytest.mark.parametrize("identity", [
    {"normal_pass_id": PASS_ID, "item_id": "item_pass", "package_id": 1, "flag_id": 2, "sku": "sku_pass"},
    {"kind": "login", "campaign_id": 136, "purchase_id": 1, "package_id": 3, "flag_id": 4, "sku": "sku_login"}])
def test_recorded_watermark_receipts_of_both_identity_layouts_read_nested(package_factory, identity):
    case = package_factory()
    common = {"request": "1" * 64, "content": "2" * 64, "state": "3" * 64, "revision": 1, "response": "<DA/>"}
    recorded = {"schema_version": LEGACY_SCHEMA_VERSION, "last_client_time": 2000, "seen_keys": ["1" * 64],
                "receipts": [{**common, "client_time": 2000, **identity}]}
    assert case.service._receipts({"package_history": recorded}, 1) == {
        "schema_version": SCHEMA_VERSION, "processed_keys": ["1" * 64], "receipts": [{**common, "identity": identity}]}


def test_evicted_purchase_stays_refused_and_is_logged(package_factory, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    case = package_factory()
    envelope = purchase_request(case)
    case.service.purchase(envelope, case.store)
    state = case.store.profile().state
    state["package_history"]["receipts"] = []
    overwrite(case, state)
    reject(case, lambda: case.service.purchase(envelope, case.store), code="package_request_stale", status=409)
    assert any("request 50" in record.getMessage() and record.levelname == "WARNING" for record in caplog.records)


def test_identity_beyond_the_native_cell_is_refused_and_logged(package_factory, monkeypatch, caplog):
    caplog.set_level("WARNING", logger="aigis_ps")
    case = package_factory()

    def oversized(*_):
        raise ProtocolError(IDENTITY_UNREPLAYABLE, "New package entitlements need identities that fit the native cell", 503)

    monkeypatch.setattr(case.service.purchases, "mark", oversized)
    reject(case, lambda: case.service.purchase(purchase_request(case), case.store), code=IDENTITY_UNREPLAYABLE, status=503)
    assert any("request 50" in record.getMessage() and record.levelname == "WARNING" for record in caplog.records)


def test_later_state_write_makes_old_receipt_stale(package_factory):
    case = package_factory()
    envelope = purchase_request(case)
    case.service.purchase(envelope, case.store)
    profile = case.store.profile()
    changed = deepcopy(profile.state)
    changed["later"] = True
    case.store.update_state(changed, profile.revision)
    reject(case, lambda: case.service.purchase(envelope, case.store), code="package_retry_stale")


@pytest.fixture
def gift_box_packages(enabled_bundle, tmp_path, monkeypatch):
    bundle = enabled_bundle
    monkeypatch.setattr(bundle.bootstrap, "clock", lambda: PURCHASED_NOW)
    monkeypatch.setattr(bundle.login_rewards, "clock", lambda: PURCHASED_NOW)
    logins = bundle.purchased_logins
    without_emblems = copy(bundle.presents)
    items = bundle.native_cdn.assets.read(ITEM_RESOURCE_NAME)[0]
    without_emblems.item_catalog = PresentItemCatalog(bundle.possessions, items)

    def build(presents):
        return PackageService(bundle.bootstrap, bundle.player_state, bundle.possessions, bundle.packages.catalog,
                              purchased_logins=PurchasedLoginService(logins.catalog, presents, logins.rules))

    earlier, current = build(without_emblems), build(bundle.presents)
    assert earlier.content_version != current.content_version
    skus = {"normal": bundle.packages.catalog.by_normal_id[PASS_ID].sku,
            "login": logins.catalog.by_campaign_id[LOGIN_CAMPAIGN].sku}
    store = Store(tmp_path / "gift-box.sqlite3", bundle.initialize(bundle.bootstrap.initial_state()))
    return SimpleNamespace(earlier=earlier, current=current, skus=skus, store=store)


def committed_purchase(case, kind):
    envelope = request(case.store, 50, (Column("SKU", "S", (case.skus[kind],)),))
    return envelope, case.earlier.purchase(envelope, case.store)


@pytest.mark.parametrize("kind", ["normal", "login"])
def test_receipt_committed_before_emblem_mail_replays_its_bytes_after_it_attaches(gift_box_packages, kind, monkeypatch):
    case = gift_box_packages
    envelope, wire = committed_purchase(case, kind)
    saved = case.store.profile()
    assert saved.state["package_history"]["receipts"][-1]["content"] == case.earlier.content_version
    monkeypatch.setattr(case.current.bootstrap, "clock", lambda: pytest.fail("Exact retry consulted current time"))
    assert case.current.purchase(envelope, case.store) == wire
    assert case.store.profile() == saved


@pytest.mark.parametrize("kind,digest", [("normal", "forged"), ("login", "forged"), ("normal", "foreign"),
                                         ("login", "foreign"), ("login", "legacy")])
def test_forged_or_mixed_package_digest_stays_stale(gift_box_packages, kind, digest):
    case = gift_box_packages
    envelope, _ = committed_purchase(case, kind)
    state = case.store.profile().state
    state["package_history"]["receipts"][-1]["content"] = {
        "forged": FORGED, "foreign": case.current._content(purchased=FOREIGN),
        "legacy": case.current.legacy_content_version}[digest]
    overwrite(case, state)
    reject(case, lambda: case.current.purchase(envelope, case.store), code="package_retry_stale", status=409)


def test_embedded_login_version_is_verified_and_none_means_the_legacy_layout(gift_box_packages):
    service = gift_box_packages.current
    assert service._content(purchased=None) == service.legacy_content_version
    assert service._content(purchased=service.purchased_logins.content_version) == service.content_version
    with pytest.raises(ProtocolError) as caught:
        service._content(purchased="X" * 64)
    assert (caught.value.code, caught.value.status) == ("package_dependencies_invalid", 503)


@pytest.fixture
def pass_window_packages(enabled_bundle, tmp_path, monkeypatch):
    bundle = enabled_bundle
    monkeypatch.setattr(bundle.bootstrap, "clock", lambda: PURCHASED_NOW)
    monkeypatch.setattr(bundle.login_rewards, "clock", lambda: PURCHASED_NOW)
    reader = lambda name: bundle.native_cdn.assets.read(name)[0]

    def build(rule, purchased_logins=bundle.purchased_logins):
        # Rebuilt from the resources under one pass eligibility rule, as that build digested them.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(event_pass_catalog, "ELIGIBILITY_RULE", rule)
            passes = EventPassCatalog(reader, bundle.possessions)
        return PackageService(bundle.bootstrap, bundle.player_state, bundle.possessions,
                              PackageCatalog(passes, bundle.possessions, reader, enabled=True),
                              purchased_logins=purchased_logins)

    skus = {"normal": bundle.packages.catalog.by_normal_id[PASS_ID].sku,
            "login": bundle.purchased_logins.catalog.by_campaign_id[LOGIN_CAMPAIGN].sku}
    store = Store(tmp_path / "pass-window.sqlite3", bundle.initialize(bundle.bootstrap.initial_state()))
    return SimpleNamespace(build=build, earlier=build(LEGACY_ELIGIBILITY_RULE), current=build(ELIGIBILITY_RULE),
                           skus=skus, store=store)


def assert_replays(case, envelope, wire, content):
    saved = case.store.profile()
    assert saved.state["package_history"]["receipts"][-1]["content"] == content != case.current.content_version
    assert case.current.purchase(envelope, case.store) == wire
    assert case.store.profile() == saved


@pytest.mark.parametrize("kind", ["normal", "login"])
def test_receipt_committed_while_pass_windows_compared_jst_instants_replays_its_bytes(pass_window_packages, kind):
    case = pass_window_packages
    envelope, wire = committed_purchase(case, kind)
    assert_replays(case, envelope, wire, case.earlier.content_version)


def test_premium_receipt_of_the_jst_window_before_login_packages_replays_its_bytes(pass_window_packages):
    case = pass_window_packages
    case.earlier = case.build(LEGACY_ELIGIBILITY_RULE, None)
    envelope, wire = committed_purchase(case, "normal")
    assert_replays(case, envelope, wire, case.earlier.legacy_content_version)


@pytest.mark.parametrize("kind,digest", [("normal", "forged"), ("login", "forged"), ("normal", "foreign"),
                                         ("login", "foreign"), ("login", "legacy")])
def test_forged_or_mixed_digest_of_the_jst_window_stays_stale(pass_window_packages, kind, digest):
    case = pass_window_packages
    envelope, _ = committed_purchase(case, kind)
    legacy_window = case.current.catalog.legacy_window_content_version
    state = case.store.profile().state
    state["package_history"]["receipts"][-1]["content"] = {
        "forged": FORGED, "foreign": case.current._content(purchased=FOREIGN, catalog=legacy_window),
        "legacy": case.current._content(purchased=None, catalog=legacy_window)}[digest]
    overwrite(case, state)
    reject(case, lambda: case.current.purchase(envelope, case.store), code="package_retry_stale", status=409)
