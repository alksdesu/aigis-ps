from copy import copy, deepcopy
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, loads
from aigis_ps.app import create_app
from aigis_ps.event_pass_service import EventPassService
from aigis_ps.event_pass_state import read_event_passes
from aigis_ps.package_catalog import PackageCatalog
from aigis_ps.package_service import PackageService
from aigis_ps.protocol import decode_wire, encode_wire, prepare_request
from aigis_ps.table_cell_patch import patch_table_cells
from test_package_service import package_factory, purchase_request, inventory, rows
from test_event_pass_revival_cli import revived
from test_quest_service import request


@pytest.fixture
def package_http(package_factory):
    def build(*, points=0, owned=False):
        case = package_factory(owned=owned)
        local = copy(case.bundle)
        local.bootstrap, local.packages = case.bootstrap, case.service
        local.event_passes = EventPassService(case.bootstrap, local.player_state, local.presents,
                                             local.possessions, local.event_passes.catalog)
        case.bootstrap.event_pass_provider = local.event_passes.response
        state = deepcopy(case.initial)
        if points:
            state = local.possessions.credit(state, {case.offer.normal_pass_id: points})
        directory = case.store.path.parent / "http"
        def application():
            handlers = (case.bootstrap.handlers() | local.player_state.handlers() | local.presents.handlers()
                        | local.event_passes.handlers() | local.packages.handlers())
            return create_app(directory, initial_state=state, state_initializer=local.initialize, handlers=handlers)
        case.bundle, case.application = local, application
        case.app = application()
        case.store = case.app.state.store
        return case
    return build


def post(client, envelope, expected=200):
    response = client.post("/api/" + envelope.profile.route,
                           content=encode_wire(envelope.xml, envelope.profile.request_key))
    assert response.status_code == expected, response.text
    if expected != 200:
        return response
    document = loads(decode_wire(response.content, envelope.profile.response_key))
    return response.content, {node.name: node for node in document.children}


def watermark(case):
    return next(row for row in read_event_passes(case.store.profile().state) if row["ID"] == case.offer.normal_pass_id)


def test_twenty_points_normal_then_free_package_then_premium_and_restart(package_http):
    case = package_http(points=20)
    before = case.store.profile().state
    wallet, money = deepcopy(before["tables"]["crystals"]), deepcopy(before["tables"]["game_param"])
    with TestClient(case.app, base_url='http://127.0.0.1') as client:
        _, normal = post(client, request(case.store, 159,
            (Column("EPI", "S", (case.offer.normal_pass_id,)),), tick=1000))
        normal_ids = {row["PresentID"] for row in case.bundle.presents.rows(case.store.profile().state)}
        assert normal_ids and "PPT" in normal
        assert watermark(case) == {"ID": case.offer.normal_pass_id, "ReceivedNormalPoint": 20, "ReceivedPremiumPoint": 0}
        purchase = purchase_request(case, tick=1001)
        wire, result = post(client, purchase)
        assert set(result) == {"SYSTEM", "PPF", "POSSESSION", "PSR"}
        assert not result["PSR"].children
        assert inventory(case)[case.offer.item_id] == 1
        assert inventory(case)[case.offer.normal_pass_id] == 20
        assert {row["PresentID"] for row in case.bundle.presents.rows(case.store.profile().state)} == normal_ids
    bought = case.store.profile()
    case.app = case.application()
    case.store = case.app.state.store
    assert case.store.profile() == bought
    with TestClient(case.app, base_url='http://127.0.0.1') as client:
        assert post(client, purchase)[0] == wire
        _, premium = post(client, request(case.store, 159,
            (Column("EPI", "S", (case.offer.normal_pass_id,)),), tick=1002))
        all_ids = {row["PresentID"] for row in case.bundle.presents.rows(case.store.profile().state)}
        assert normal_ids < all_ids and "PPT" in premium
        assert watermark(case)["ReceivedPremiumPoint"] == 20
        _, startup = post(client, request(case.store, 137, (), tick=1003))
        pass_row = next(row for row in rows(startup["EventPassStatus"]) if row["ID"] == case.offer.normal_pass_id)
        assert pass_row["ReceivedNormalPoint"] == pass_row["ReceivedPremiumPoint"] == 20
        _, mail = post(client, request(case.store, 42, (), tick=1004))
        assert len(next(column.values for column in mail["PPT"].children if column.name == "PresentID")) == len(all_ids)
    final = case.store.profile()
    assert final.state["tables"]["crystals"] == wallet
    assert final.state["tables"]["game_param"] == money
    assert inventory(case)[case.offer.normal_pass_id] == 20
    assert case.application().state.store.profile() == final


@pytest.mark.parametrize("operation", [46, 47])
def test_listing_protocol_reads_complete_catalog_flags_inventory_without_writes(package_http, operation):
    case = package_http(points=20)
    before = case.store.profile()
    with TestClient(case.app, base_url='http://127.0.0.1') as client:
        _, result = post(client, request(case.store, operation, ()))
    assert {"SYSTEM", "PSC", "PPF", "POSSESSION"} <= set(result)
    assert any(row["SkuID"] == case.offer.sku for row in rows(result["PSC"]))
    assert len(rows(result["PPF"])) == len(case.catalog.offers)
    if operation == 47:
        assert result["PSR"].children == ()
    assert case.store.profile() == before


@pytest.mark.parametrize("operation", [46, 47, 50])
def test_invalid_authentication_is_rejected_before_business_mutation(package_http, operation):
    case = package_http()
    envelope = purchase_request(case) if operation == 50 else request(case.store, operation, ())
    raw = envelope.payload_xml().replace(case.store.profile().user_key.encode(), b"invalid-user-key")
    if envelope.profile.checksum_required:
        raw = prepare_request(raw, envelope.profile.st)
    before = case.store.profile()
    with TestClient(case.app, base_url='http://127.0.0.1') as client:
        result = post(client, replace(envelope, xml=raw), 403)
    assert result.json()["error"]["code"] == "invalid_identity"
    assert case.store.profile() == before


@pytest.mark.parametrize("operation", [46, 47, 50])
def test_extra_business_fields_do_not_become_empty_success(package_http, operation):
    case = package_http()
    fields = [Column("Unknown", "I", (1,))]
    if operation == 50:
        fields.append(Column("SKU", "S", (case.offer.sku,)))
    before = case.store.profile()
    with TestClient(case.app, base_url='http://127.0.0.1') as client:
        response = post(client, request(case.store, operation, fields), 400)
    assert response.json()["error"]["code"] == "invalid_request_fields"
    assert case.store.profile() == before


def test_effective_text_resource_change_rejects_old_receipt(package_http):
    case = package_http()
    envelope = purchase_request(case)
    with TestClient(case.app, base_url='http://127.0.0.1') as client:
        post(client, envelope)
    reader = lambda name: case.bundle.native_cdn.assets.read(name)[0]
    original = reader("SystemText.atb")
    updated_text = patch_table_cells(original, {0: {"Data_Text": "fixture resource revision"}})
    updated_catalog = PackageCatalog(case.catalog.event_pass_catalog, case.bundle.possessions,
        lambda name: updated_text if name == "SystemText.atb" else reader(name), enabled=True)
    assert updated_catalog.content_version != case.catalog.content_version
    case.bundle.packages = PackageService(case.bootstrap, case.bundle.player_state, case.bundle.possessions, updated_catalog)
    case.app = case.application()
    case.store = case.app.state.store
    before = case.store.profile()
    with TestClient(case.app, base_url='http://127.0.0.1') as client:
        response = post(client, envelope, 409)
    assert response.json()["error"]["code"] == "package_retry_stale"
    assert case.store.profile() == before


def test_old_owned_entitlement_is_not_granted_or_charged_on_purchase(package_http):
    case = package_http(owned=True)
    before = case.store.profile()
    assert before.state["package_purchases"]["entries"][case.offer.normal_pass_id]["source"] == "legacy_owned"
    with TestClient(case.app, base_url='http://127.0.0.1') as client:
        response = post(client, purchase_request(case), 409)
    assert response.json()["error"]["code"] == "package_already_purchased"
    assert case.store.profile() == before
