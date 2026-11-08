from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import struct
from types import MappingProxyType, SimpleNamespace

import lz4.frame
import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.gift_catalog import GiftCatalog
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.native_clock import parse_native_date
from aigis_ps.possessions import STORED_ITEM_NAME_BYTES, PossessionService
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.skin_shop_catalog import LEGACY_WINDOW_RULE, SHOP_FIELDS, WINDOW_RULE, SkinShopCatalog
from aigis_ps.system_literals import command_id
from aigis_ps.unit_skin_catalog import UnitSkinCatalog
from test_present_items import item_table, row
from test_unit_skin_catalog import Cards, card, skin_table


def shop(number=1, **changes):
    values = {"RangeKey": number, "ConfigId": command_id(f"Skin{number:04}"), "DisplayId": -number,
        "PurchaseFlag": command_id(f"PurchasedSkin_{number:04}"), "Price": 15,
        "SkinId": command_id(f"skin_{number:04}"), "ButtonImageId": number,
        "ItemId": command_id(f"get_skin_{number:05}"), "SkinCardId": number + 100,
        "OpenDate": "2026/10/22 11:30:00", "CloseDate": "2026/11/19 11:00:00"}
    values.update(changes)
    return values


def skin_item(number=1, **changes):
    return row(f"get_skin_{number:05}", EffectType=13, Category=1165413830, Stack=1,
               CanAllGet=0, NoAutoDelete=1, **changes)


def components(*, shops=None, items=None, numbers=(1,), mutate=None):
    shops = [shop(number) for number in numbers] if shops is None else shops
    items = [skin_item(number) for number in numbers] if items is None else items
    payloads = {
        "ItemConfigTable.atb": item_table(items + [row("unrelated")]),
        "SkinPackage.atb": skin_table([(f"skin_{number:04}", 0, f"get_skin_{number:05}", False)
                                       for number in numbers]),
        "SkinShop.atb": item_table(shops, tuple(SHOP_FIELDS.items())),
    }
    possessions = PossessionService(Path("."), asset_reader=payloads.__getitem__)
    cards = Cards({number + 100: card(number + 100, f"skin_{number:04}") for number in numbers})
    skins = UnitSkinCatalog(cards, possessions, payloads.__getitem__)
    if mutate:
        mutate(payloads, skins)
    catalog = SkinShopCatalog(possessions, skins, payloads.__getitem__)
    return SimpleNamespace(catalog=catalog, possessions=possessions, skins=skins, payloads=payloads)


@pytest.fixture(scope="module")
def current():
    project = Path(__file__).resolve().parents[1]
    client_hash = CURRENT_CLIENT_SHA256
    manifest, entries = ResourceLibrary(project / "runtime/resources", client_hash=client_hash).load()
    assets = IndexedAssetService(entries, project / "runtime/resources", project.parent / "aigisr_exe")
    reader = lambda name: assets.read(name)[0]
    possessions = PossessionService(project.parent / "aigisr_exe", asset_reader=reader)
    snapshot = MasterDataStore(project / "runtime/server/masterdata", expected_hash=client_hash).snapshot()
    cards = GiftCatalog(snapshot, reader, possessions, resource_version=manifest["version"])
    skins = UnitSkinCatalog(cards, possessions, reader)
    return SimpleNamespace(catalog=SkinShopCatalog(possessions, skins, reader),
                           possessions=possessions, skins=skins, reader=reader)


def test_current_catalog_preserves_all_rows_and_marks_only_missing_items(current):
    catalog = current.catalog
    assert len(catalog.products) == 38
    assert sum(product.supported for product in catalog.products) == 36
    assert {product.item_id for product in catalog.products if not product.supported} == {
        "get_skin_00046", "get_skin_00047"}
    assert {product.range_key for product in catalog.products} == set(range(1, 39))
    assert all(product.display_id == -product.range_key for product in catalog.products)
    assert catalog.status()["flag_name_source"] == "inferred_pattern_verified_all_rows"
    assert catalog.status()["ready"] is True
    assert len(catalog.content_version) == 64


def test_current_shop_id_is_signed_config_id_and_all_names_match(current):
    for product in current.catalog.products:
        assert current.catalog.get(product.config_id) == product
        number = int(product.item_id.removeprefix("get_skin_"))
        assert product.flag_name == f"PurchasedSkin_{number:04}"
        assert command_id(product.flag_name) == product.purchase_flag
        assert command_id(f"Skin{number:04}") == product.shop_id
        assert command_id(f"skin_{number:04}") == product.skin_id
    assert current.catalog.get(-1642864718).supported is False


def test_current_windows_keep_expired_rows_and_only_eight_supported_now(current):
    catalog = current.catalog
    active = [p for p in catalog.products if catalog.is_open(p, datetime(2026, 11, 1, 12, tzinfo=JST))]
    assert len(active) == 10
    assert [p.range_key for p in active if p.supported] == [7, 8, 9, 10, 33, 34, 35, 36]
    assert {"get_skin_00003", "get_skin_00017"}.isdisjoint(p.item_id for p in catalog.products)


def test_metadata_and_products_are_immutable_and_inputs_untouched():
    case = components()
    before = deepcopy(case.payloads)
    product = case.catalog.products[0]
    with pytest.raises(FrozenInstanceError):
        product.price = 9
    with pytest.raises(TypeError):
        case.catalog.by_id[product.shop_id] = product
    with pytest.raises(AttributeError):
        case.catalog.products = ()
    state = {"bootstrap_schema": 1, "tables": {"possessions": []}}
    state_before = deepcopy(state)
    assert case.catalog.is_open(product, datetime(2026, 11, 1))
    assert case.payloads == before and state == state_before


@pytest.mark.parametrize("value", [True, False, 0, 1, -1, 2**31, -(2**31)-1, "1679527701", None])
def test_shop_lookup_rejects_other_domains_and_non_integers(value):
    with pytest.raises(ProtocolError, match="verified skin product"):
        components().catalog.get(value)


@pytest.mark.parametrize("shift, expected", [(-1, False), (0, True), (1, True)])
def test_sale_start_is_inclusive(shift, expected):
    catalog = components().catalog
    product = catalog.products[0]
    assert catalog.is_open(product, product.start_at + timedelta(seconds=shift)) is expected


@pytest.mark.parametrize("shift, expected", [(-1, True), (0, True), (1, False)])
def test_sale_end_is_inclusive(shift, expected):
    catalog = components().catalog
    product = catalog.products[0]
    assert catalog.is_open(product, product.end_at + timedelta(seconds=shift)) is expected


@pytest.mark.parametrize("bound, shift, expected", [
    ("end", timedelta(milliseconds=500), True), ("end", timedelta(milliseconds=999), True),
    ("end", timedelta(microseconds=999999), True), ("end", timedelta(seconds=1), False),
    ("start", -timedelta(milliseconds=500), False), ("start", timedelta(milliseconds=500), True)])
def test_sale_window_compares_whole_seconds_of_the_client_clock(bound, shift, expected):
    catalog = components().catalog
    product = catalog.products[0]
    moment = product.end_at if bound == "end" else product.start_at
    assert catalog.is_open(product, moment + shift) is expected


def test_sale_bounds_and_clock_share_the_client_float32_grid():
    opening, closing = "2026/10/22 06:00:07", "2026/11/19 12:00:08"
    catalog = components(shops=[shop(OpenDate=opening, CloseDate=closing)]).catalog
    product = catalog.products[0]
    assert (product.start_at, product.end_at) == (parse_native_date(opening), parse_native_date(closing))
    assert product.start_at - datetime(2026, 10, 22, 6, 0, 7, tzinfo=JST) == timedelta(milliseconds=4)
    assert datetime(2026, 11, 19, 12, 0, 8, tzinfo=JST) - product.end_at == timedelta(milliseconds=4)
    assert catalog.is_open(product, datetime(2026, 10, 22, 6, 0, 7, tzinfo=JST))
    assert not catalog.is_open(product, datetime(2026, 10, 22, 6, 0, 6, 999999, tzinfo=JST))
    assert catalog.is_open(product, datetime(2026, 11, 19, 12, 0, 8, 999999, tzinfo=JST))
    assert not catalog.is_open(product, datetime(2026, 11, 19, 12, 0, 9, tzinfo=JST))


@pytest.mark.parametrize("offset", [0, 480, 540, -300])
def test_same_instant_has_same_window_in_every_timezone(offset):
    catalog = components().catalog
    product = catalog.products[0]
    zone = timezone(timedelta(minutes=offset))
    assert catalog.is_open(product, product.start_at.astimezone(zone))
    assert not catalog.is_open(product, (product.start_at - timedelta(seconds=1)).astimezone(zone))


def test_naive_clock_is_explicitly_jst_and_empty_dates_are_unbounded():
    catalog = components(shops=[shop(OpenDate="", CloseDate="")]).catalog
    product = catalog.products[0]
    assert product.start_at is None and product.end_at is None
    assert catalog.is_open(product, datetime(2020, 1, 1))
    assert catalog.is_open(product, datetime(2050, 1, 1))
    normal = components().catalog
    product = normal.products[0]
    assert normal.is_open(product, product.start_at.replace(tzinfo=None))


@pytest.mark.parametrize("stamp", ["2026/9/1 11:00:00", "2026/09/01 11:00:00", "2026-9-1 11:00:00"])
def test_supported_native_date_spellings(stamp):
    product = components(shops=[shop(OpenDate=stamp)]).catalog.products[0]
    assert product.start_at == datetime(2026, 9, 1, 11, tzinfo=JST)


@pytest.mark.parametrize("stamp", ["2026/2/30 11:00:00", "2026/13/1 11:00:00", "2026/1/1", " 2026/1/1 11:00:00",
                                  "2026/1-1 11:00:00", "2026/1/1 24:00:00", "1", "2026/1/1 11:00:00Z"])
def test_invalid_or_unverified_date_syntax_is_not_silently_open(stamp):
    with pytest.raises(ProtocolError) as error:
        components(shops=[shop(OpenDate=stamp)])
    assert error.value.code == "skin_shop_catalog_invalid"


def test_inverted_window_is_rejected_but_zero_length_is_valid():
    with pytest.raises(ProtocolError):
        components(shops=[shop(OpenDate="2027/1/1 00:00:00")])
    catalog = components(shops=[shop(OpenDate="2026/1/1 00:00:00", CloseDate="2026/1/1 00:00:00")]).catalog
    assert catalog.is_open(catalog.products[0], datetime(2026, 1, 1))


@pytest.mark.parametrize("field", ["ConfigId", "PurchaseFlag", "SkinId", "ItemId", "SkinCardId", "ButtonImageId"])
def test_inconsistent_id_links_are_rejected(field):
    original = shop()
    with pytest.raises(ProtocolError):
        components(shops=[dict(original, **{field: original[field] + 1})])


def test_duplicate_config_is_rejected_instead_of_discarding_earlier_item():
    with pytest.raises(ProtocolError):
        components(shops=[shop(), shop(RangeKey=2)])


@pytest.mark.parametrize("field,value", [("EffectType", 12), ("Category", 0), ("Stack", 2), ("ReceiveAction", 1),
    ("EventID", "event"), ("ReceiveConfirmation", 1), ("CanAllGet", 1), ("NoAutoDelete", 0), ("InvisibleContaint", 1)])
def test_owned_item_behavior_changes_are_explicitly_rejected(field, value):
    changed = skin_item()
    changed[field] = value
    with pytest.raises(ProtocolError):
        components(items=[changed])


def test_missing_definition_keeps_other_product_available():
    case = components(numbers=(1, 2), items=[skin_item(1)])
    assert case.catalog.get(command_id("Skin0001")).supported is True
    assert case.catalog.get(command_id("Skin0002")).supported is False


def test_verified_zero_stack_item_keeps_only_its_product_unsupported():
    zero_stack = {**skin_item(2), "Stack": 0, "NoAutoDelete": 0, "CanPresent": 0}
    case = components(numbers=(1, 2), items=[skin_item(1), zero_stack])
    assert case.catalog.is_ready()
    assert case.catalog.get(command_id("Skin0001")).supported is True
    assert case.catalog.get(command_id("Skin0002")).supported is False
    assert case.catalog.status()["unsupported_items"] == ["get_skin_00002"]
    assert "get_skin_00002" in case.skins.counted_item_ids
    state = {"bootstrap_schema": 1, "tables": {"possessions": [{"ItemID": "get_skin_00002", "Count": 1}]}}
    assert case.skins.acquired_items(state) == frozenset({"get_skin_00002"})
    assert case.skins.allowed_displays(102, state, smartphone_images=False) == frozenset({5})
    with pytest.raises(ProtocolError) as error:
        case.possessions.credit(state, {"get_skin_00002": 1})
    assert error.value.code == "possession_stack_unsupported"


@pytest.mark.parametrize("field,value", [("EffectType", 12), ("Category", 0), ("ReceiveAction", 1),
    ("EventID", "event"), ("ReceiveConfirmation", 1), ("CanAllGet", 1), ("NoAutoDelete", 1),
    ("InvisibleContaint", 1), ("CanPresent", 1)])
def test_unverified_zero_stack_item_behavior_still_fails_the_catalog(field, value):
    zero_stack = {**skin_item(), "Stack": 0, "NoAutoDelete": 0, "CanPresent": 0, field: value}
    with pytest.raises(ProtocolError) as error:
        components(items=[zero_stack])
    assert error.value.code == "skin_shop_catalog_invalid"


def test_stored_bound_item_name_keeps_its_product_listed_but_unsupported():
    stored = "get_skin_" + "x" * (STORED_ITEM_NAME_BYTES - len("get_skin_"))
    payloads = {
        "ItemConfigTable.atb": item_table([skin_item(1), row(stored, EffectType=13, Category=1165413830, Stack=1,
                                                             CanAllGet=0, NoAutoDelete=1), row("unrelated")]),
        "SkinPackage.atb": skin_table([("skin_0001", 0, "get_skin_00001", False), ("skin_0002", 0, stored, False)]),
        "SkinShop.atb": item_table([shop(1), shop(2, ItemId=command_id(stored))], tuple(SHOP_FIELDS.items())),
    }
    possessions = PossessionService(Path("."), asset_reader=payloads.__getitem__)
    skins = UnitSkinCatalog(Cards({101: card(101, "skin_0001"), 102: card(102, "skin_0002")}), possessions,
                            payloads.__getitem__)
    assert stored in skins.counted_item_ids
    catalog = SkinShopCatalog(possessions, skins, payloads.__getitem__)
    assert catalog.get(command_id("Skin0001")).supported is True
    product = catalog.get(command_id("Skin0002"))
    assert product.item_id == stored and product.supported is False
    status = catalog.status()
    assert status["ready"] and status["supported_count"] == 1 and status["unsupported_items"] == [stored]


@pytest.mark.parametrize("price", [0, 1, 100, 0x7fffffff])
def test_nonnegative_native_prices_are_not_hardcoded_to_fifteen(price):
    assert components(shops=[shop(Price=price)]).catalog.products[0].price == price


def test_negative_price_is_not_treated_as_a_free_purchase():
    with pytest.raises(ProtocolError):
        components(shops=[shop(Price=-1)])


@pytest.mark.parametrize("resource", ["SkinShop.atb", "SkinPackage.atb", "ItemConfigTable.atb"])
def test_missing_truncated_and_schema_changed_resources_fail(resource):
    def corrupt(payloads, _):
        payloads[resource] = payloads[resource][:15]
    with pytest.raises(ProtocolError):
        components(mutate=corrupt)


def test_resource_schema_field_rename_fails():
    def corrupt(payloads, _):
        payloads["SkinShop.atb"] = payloads["SkinShop.atb"].replace(b"Price\0", b"Prico\0")
    with pytest.raises(ProtocolError):
        components(mutate=corrupt)


def test_all4_resource_wrapper_preserves_supported_catalog():
    def compress(payloads, skins):
        raw = payloads["SkinShop.atb"]
        payloads["SkinShop.atb"] = struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)
    assert components(mutate=compress).catalog.products[0].supported


@pytest.mark.parametrize("dependency", ["item_hash", "skin_version", "stack", "skin_counted", "skin_package",
                                        "possessions_unready", "skins_unready"])
def test_dependency_drift_is_detected_before_lookup_or_window(dependency, monkeypatch):
    case = components()
    product = case.catalog.products[0]
    if dependency == "item_hash":
        case.possessions.asset_sha256 = "0" * 64
    elif dependency == "skin_version":
        case.skins.content_version = "0" * 64
    elif dependency == "stack":
        case.possessions._definitions[product.item_id] = 2
    elif dependency == "skin_counted":
        case.skins._counted_item_ids = frozenset()
    elif dependency == "skin_package":
        case.skins._hashes = MappingProxyType({**case.skins.resource_sha256, "SkinPackage.atb": "0" * 64})
    elif dependency == "possessions_unready":
        case.possessions.issues["item_definitions"] = "changed"
    else:
        monkeypatch.setattr(case.skins, "is_ready", lambda: False)
    for call in (lambda: case.catalog.get(product.shop_id),
                 lambda: case.catalog.is_open(product, datetime(2026, 11, 1))):
        with pytest.raises(ProtocolError) as error:
            call()
        assert error.value.code == "skin_shop_catalog_changed"
    assert case.catalog.is_ready() is case.catalog.status()["ready"] is False


@pytest.mark.parametrize("changed", [False, True])
def test_readiness_matches_status_without_building_any_status(monkeypatch, changed):
    case = components()
    if changed:
        case.skins.content_version = "0" * 64
    ready = case.catalog.status()["ready"]
    assert ready is (not changed)
    for dependency in (case.catalog, case.skins, case.possessions):
        monkeypatch.setattr(dependency, "status", lambda: pytest.fail("Skin shop readiness built a full status"))
    assert case.catalog.is_ready() is ready


def test_resource_hashes_are_read_only_and_shared_with_the_skin_catalog():
    case = components()
    hashes = case.catalog.resource_sha256
    assert dict(hashes) == case.catalog.status()["resource_sha256"]
    assert hashes.keys() == {"ItemConfigTable.atb", "SkinPackage.atb", "SkinShop.atb"}
    assert hashes["SkinShop.atb"] == hashlib.sha256(case.payloads["SkinShop.atb"]).hexdigest()
    assert hashes["SkinPackage.atb"] == case.skins.resource_sha256["SkinPackage.atb"]
    with pytest.raises(TypeError):
        hashes["SkinShop.atb"] = "0" * 64
    with pytest.raises(AttributeError):
        case.catalog.resource_sha256 = {}
    assert case.catalog.is_ready()


def test_metadata_change_creates_different_version_and_old_product_is_rejected():
    before, after = components().catalog, components(shops=[shop(Price=16)]).catalog
    assert before.content_version != after.content_version
    with pytest.raises(ProtocolError):
        after.is_open(before.products[0], datetime(2026, 11, 1))


def test_client_clock_window_rule_keeps_the_jst_rule_version_replayable(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr("aigis_ps.skin_shop_catalog.WINDOW_RULE", LEGACY_WINDOW_RULE)
        earlier = components().catalog.content_version
    case = components()
    current = case.catalog
    committed = {"rule": "skin-shop-v1", "resources": dict(current.resource_sha256),
        "skins": case.skins.content_version, "flag_names": "inferred_pattern_verified_all_rows",
        "window": "inclusive_jst", "wallet_policy": "free_then_paid", "missing_item": "unsupported_product"}
    assert earlier == hashlib.sha256(json.dumps(committed, sort_keys=True).encode()).hexdigest()
    assert current.status()["window"] == WINDOW_RULE == "inclusive_native_clock"
    assert LEGACY_WINDOW_RULE == "inclusive_jst" and earlier != current.content_version
    assert current.replayable_content_versions() == frozenset((current.content_version, earlier))
    assert components(shops=[shop(Price=16)]).catalog.content_version not in current.replayable_content_versions()


@pytest.mark.parametrize("clock", [None, 1, "2026/11/1", True, datetime.max.replace(tzinfo=timezone.utc)])
def test_invalid_clock_is_explicit(clock):
    catalog = components().catalog
    with pytest.raises(ProtocolError) as error:
        catalog.is_open(catalog.products[0], clock)
    assert error.value.code == "skin_shop_clock_invalid"
