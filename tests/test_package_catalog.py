from copy import copy, deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import struct
from types import MappingProxyType
import zlib

import pytest
import lz4.frame

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.alxml import Container, dumps, loads
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.event_pass_catalog import EventPassCatalog
from aigis_ps.event_pass_revival import EventPassRevivalRules, build_event_pass_revival
from aigis_ps.package_catalog import PackageCatalog, PSC_FIELDS, SKU_PREFIX
from aigis_ps.package_state import package_identity
from aigis_ps.package_text_overlay import (CAPTION_KEY, DESCRIPTION_KEY, build_package_text_overlay,
                                          event_pass_package_ids)
from aigis_ps.possessions import PossessionService
from aigis_ps.protocol import NATIVE_CELL_BYTES
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.table_cell_patch import patch_table_cells
from test_present_items import item_table


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
PASS = "event_pass_20261015"
NAMES = ("EventPassConfig.atb", "GloryConditionConfig.atb", "ItemConfigTable.atb",
         "cfgImplantText.atb", "SystemText.atb", "CfgMenuList.atb", "CfgTextureList.atb", "ShopMenu.aar",
         "OnCommandMenuShop.atb")


def decode(raw):
    return decompress_all4(raw) if raw.startswith(b"ALL4") else raw


def rows(node):
    return [dict(zip((column.name for column in node.children), values))
            for values in zip(*(column.values for column in node.children))]


@pytest.fixture(scope="module")
def sources():
    project = Path(__file__).resolve().parents[1]
    root = project / "runtime/resources"
    _, entries = ResourceLibrary(root, client_hash=
        CURRENT_CLIENT_SHA256).load()
    assets = IndexedAssetService(entries, root, project.parent / "aigisr_exe")
    raw = {name: assets.read(name)[0] for name in NAMES}
    texts = build_package_text_overlay(raw["cfgImplantText.atb"], raw["SystemText.atb"],
                                      event_pass_package_ids(raw["EventPassConfig.atb"]))
    effective = raw | dict(texts.replacements)
    pos = PossessionService(project / "runtime/assets", asset_reader=raw.__getitem__)
    return raw, effective, texts, pos


@pytest.fixture
def factory(sources):
    raw, effective, texts, pos = sources

    def make(*, enabled=True, active=PASS, changes=None, plain=False, end="2026/11/02 00:00:00"):
        data = dict(raw if plain else effective)
        if active:
            revival = build_event_pass_revival(data["EventPassConfig.atb"], data["GloryConditionConfig.atb"],
                EventPassRevivalRules(active, {1611: 2}, start_at="2026/11/01 00:00:00", end_at=end))
            data.update(revival.replacements)
        ep = EventPassCatalog(data.__getitem__, pos)
        if changes:
            data.update(changes)
        catalog = PackageCatalog(ep, pos, data.__getitem__, enabled=enabled)
        return catalog, ep, data
    return make


def test_disabled_catalog_preserves_six_identities_without_requiring_local_text_or_ui(sources):
    raw, _, _, pos = sources
    minimal = {name: raw[name] for name in ("EventPassConfig.atb", "ItemConfigTable.atb")}
    catalog = PackageCatalog(EventPassCatalog(minimal.__getitem__, pos), pos, minimal.__getitem__)
    assert catalog.status()["ready"] is True and catalog.status()["enabled"] is False
    assert len(catalog.offers) == 6
    assert catalog.available(NOW) == () and rows(catalog.response(NOW)) == []
    assert all(offer.caption_key is None and offer.description_key is None for offer in catalog.offers)
    assert all(catalog.get(offer.sku) == offer for offer in catalog.offers)


def test_original_closed_passes_never_become_free_offers_merely_by_enabling_catalog(factory):
    catalog, _, _ = factory(active=None)
    assert len(catalog.offers) == 6 and catalog.available(NOW) == ()
    assert rows(catalog.response(NOW)) == []


@pytest.mark.parametrize("identifier,head", [("20260611", 1), ("20260702", 54), ("20260716", 107),
                                           ("20260723", 133), ("20260806", 186), ("20261015", 239)])
def test_each_real_pass_has_a_stable_distinct_native_free_offer(factory, identifier, head):
    catalog, ep, _ = factory(active="event_pass_" + identifier)
    offer, = catalog.available(NOW)
    assert offer.normal_pass_id == "event_pass_" + identifier
    assert offer.item_id == "event_pass_premium_" + identifier
    assert offer.package_id == offer.flag_id == 2_000_000_000 + head
    assert offer.sku == "local_" + offer.item_id
    assert offer.definition_hash == package_identity(offer.normal_pass_id, offer.item_id)
    assert ep.get(offer.normal_pass_id).premium_item_id == offer.item_id
    row, = rows(catalog.response(NOW))
    assert row["PackageItemType"] == 9 and row["PackageItemNum"] == 1
    assert row["ItemID"] == offer.item_id and row["SkuID"] == offer.sku
    assert row["Price"] == 0 and row["PackageRepeatCount"] == 1


def test_native_psc_schema_uses_physical_text_rows_and_verified_button(factory, sources):
    catalog, _, _ = factory()
    document = loads(dumps(Container(children=(catalog.response(NOW),)))).children[0]
    assert tuple((node.name, node.type) for node in document.children) == PSC_FIELDS
    row, = rows(document)
    assert row["PackageDisplayScene"] == 2 and row["BtnPackObjectID"] == 4203149
    assert row["PackageCaption"] == sources[2].text_indices[CAPTION_KEY] == 6433
    assert row["PackageDescription"] == sources[2].text_indices[DESCRIPTION_KEY] == 6434
    assert row["PackageExpression"] == "" and row["PackageIconPath"] == ""
    assert row["PackageTextuerID"] == "" and row["WidgetID"] == 0
    assert all(row[field] == 0 for field in ("PackagePayCrystalNum", "PackageFreeCrystalNum",
               "PackageCardID", "PackageUnitLevel", "PackageUnitSkillLevel", "PackageUnitCost",
               "PackageUnitClassChange", "PackageSummonConfigID", "LoginBonusID"))
    assert row["OpenDays"] == "2026/11/01 00:00:00" and row["CloseDays"] == "2026/11/02 00:00:00"


@pytest.mark.parametrize("offset,expected", [(-1, False), (0, True), (1, True), (86400 - 1, True),
                                           (86400, False), (86401, False)])
def test_package_window_has_inclusive_start_and_exclusive_end(factory, offset, expected):
    catalog, _, _ = factory()
    when = datetime(2026, 11, 1, tzinfo=JST) + timedelta(seconds=offset)
    assert bool(catalog.available(when)) is expected
    assert catalog.available(when) == catalog.available(when.astimezone(timezone.utc))
    assert catalog.available(when) == catalog.available(when.replace(tzinfo=None))


def test_window_change_updates_policy_fingerprint_without_resetting_purchase_identity(factory):
    old, _, _ = factory()
    changed, _, _ = factory(end="2026/12/02 00:00:00")
    assert old.content_version != changed.content_version
    assert [row.definition_hash for row in old.offers] == [row.definition_hash for row in changed.offers]
    assert [row.package_id for row in old.offers] == [row.package_id for row in changed.offers]


def test_zero_duration_offer_is_not_available(factory):
    catalog, _, _ = factory(end="2026/11/01 00:00:00")
    assert catalog.available(datetime(2026, 11, 1, tzinfo=JST)) == ()


@pytest.mark.parametrize("sku", [None, 1, True, "", "event_pass_premium_20261015", "local_unknown"])
def test_unknown_or_wrong_type_sku_is_not_treated_as_an_offer(factory, sku):
    catalog, _, _ = factory()
    with pytest.raises(ProtocolError) as error:
        catalog.get(sku)
    assert (error.value.code, error.value.status) == ("package_unknown", 404)


def test_enabled_catalog_requires_local_text_overlay(factory):
    with pytest.raises(ProtocolError, match="resources"):
        factory(plain=True)


@pytest.mark.parametrize("resource", ["cfgImplantText.atb", "SystemText.atb", "CfgMenuList.atb",
                                      "CfgTextureList.atb", "ShopMenu.aar"])
def test_required_ui_resources_cannot_be_malformed(factory, resource):
    with pytest.raises(ProtocolError) as error:
        factory(changes={resource: b"broken"})
    assert error.value.code == "package_catalog_invalid"


@pytest.mark.parametrize("resource,key,value", [("CfgMenuList.atb", "_CfgID", 1186983849),
                                                ("CfgTextureList.atb", "_CfgID", -145076)])
def test_template_and_item_icon_mappings_cannot_be_missing_or_ambiguous(factory, sources, resource, key, value):
    data = sources[1][resource]
    parsed = parse_table(decode(data))
    index, row = next((index, row) for index, row in enumerate(parsed["rows"]) if row[key] == value)
    missing = bytearray(decode(data))
    offset = next(field["offset"] for field in parsed["fields"] if field["name"] == "_ResID")
    struct.pack_into("<i", missing, parsed["data_offset"] + index * parsed["record_size"] + offset, 0)
    missing = bytes(missing)
    if data.startswith(b"ALL4"):
        missing = data[:12] + lz4.frame.compress(missing)
    fields = tuple((field["name"], field["type_code"]) for field in parsed["fields"])
    for changed in (missing, item_table([*parsed["rows"], row], fields)):
        with pytest.raises(ProtocolError) as error:
            factory(changes={resource: changed})
        assert error.value.code == "package_catalog_invalid"


def test_missing_native_button_member_is_rejected(factory, sources):
    data = decode(sources[1]["ShopMenu.aar"])
    changed = data.replace(b"MnObjCommonPackage.aod", b"MnObjUnknownButton.aod")
    assert changed != data
    with pytest.raises(ProtocolError):
        factory(changes={"ShopMenu.aar": changed})


def test_caption_text_tampering_is_rejected(factory, sources):
    index = sources[2].text_indices[CAPTION_KEY]
    changed = patch_table_cells(sources[1]["SystemText.atb"], {index: {"Data_Text": "unrelated"}})
    with pytest.raises(ProtocolError):
        factory(changes={"SystemText.atb": changed})


def test_pass_and_inventory_resources_must_match_the_dependencies(factory, sources):
    for name in ("EventPassConfig.atb", "ItemConfigTable.atb"):
        raw = decode(sources[1][name])
        field = "TimeLimit" if name == "EventPassConfig.atb" else "TextID"
        value = "different" if field == "TimeLimit" else 1
        with pytest.raises(ProtocolError):
            factory(changes={name: patch_table_cells(raw, {0: {field: value}})})


@pytest.mark.parametrize("dependency", ["enabled", "catalog", "possession", "pass_version", "passes"])
def test_runtime_dependency_changes_fail_closed(factory, dependency):
    catalog, ep, _ = factory()
    if dependency == "enabled":
        catalog.enabled = False
    elif dependency == "catalog":
        catalog.event_pass_catalog = copy(ep)
    elif dependency == "possession":
        catalog.possessions = copy(catalog.possessions)
    elif dependency == "pass_version":
        ep._content_version = "a" * 64
    else:
        ep._passes = tuple(replace(row, status=1) for row in ep.passes)
        ep._by_id = MappingProxyType({row.item_id: row for row in ep.passes})
    with pytest.raises(ProtocolError) as error:
        catalog.response(NOW)
    assert error.value.code == "package_catalog_changed"
    assert catalog.status()["ready"] is False


def test_readiness_checks_the_pass_catalog_once_and_still_compares_every_entry(factory, monkeypatch):
    catalog, ep, _ = factory()
    checks, original = [], ep._ready
    monkeypatch.setattr(ep, "_ready", lambda: checks.append(None) or original())
    assert catalog.status()["ready"] is True and len(checks) == 1
    assert catalog.get(catalog.offers[0].sku) == catalog.offers[0] and len(checks) == 2
    entry = ep.passes[-1]
    ep._by_id = MappingProxyType({**ep.by_id, entry.item_id: replace(entry, time_limit=entry.time_limit + "changed")})
    assert tuple(ep.passes) == catalog._passes
    with pytest.raises(ProtocolError) as error:
        catalog.response(NOW)
    assert error.value.code == "package_catalog_changed"
    assert catalog.status()["ready"] is False


def test_readiness_never_builds_the_possession_status(factory, monkeypatch):
    catalog, _, _ = factory()
    monkeypatch.setattr(catalog.possessions, "status",
                        lambda: pytest.fail("readiness must not build the possession status"))
    assert catalog.status()["ready"] is True
    assert catalog.get(catalog.offers[0].sku) == catalog.offers[0] and catalog.available(NOW)
    monkeypatch.setattr(catalog.possessions, "issues", {"item_definitions": "unavailable"})
    with pytest.raises(ProtocolError) as error:
        catalog.response(NOW)
    assert error.value.code == "package_catalog_changed"
    assert catalog.status()["error"] == "package_catalog_changed"


def test_public_definitions_and_status_do_not_mutate_sources(factory):
    catalog, _, data = factory()
    before = deepcopy(data)
    with pytest.raises(FrozenInstanceError):
        catalog.offers[0].sku = "changed"
    with pytest.raises(TypeError):
        catalog.by_sku["new"] = catalog.offers[0]
    with pytest.raises(TypeError):
        catalog.by_normal_id["new"] = catalog.offers[0]
    status = catalog.status()
    status["resource_hashes"].clear()
    assert catalog.status()["resource_hashes"] and data == before


@pytest.mark.parametrize("name", ["SystemText.atb", "OnCommandMenuShop.atb", "CfgMenuList.atb", "ShopMenu.aar"])
def test_catalog_never_reloads_changed_ui_resources_into_its_frozen_snapshot(factory, name):
    catalog, ep, data = factory()
    before = catalog.response(NOW), catalog.content_version
    data[name] = b"invalid"
    assert (catalog.response(NOW), catalog.content_version) == before
    with pytest.raises(ProtocolError):
        PackageCatalog(ep, catalog.possessions, data.__getitem__, enabled=True)


def test_both_native_row_vectors_are_extended_to_their_last_required_slot(factory):
    catalog, _, _ = factory()
    row, = rows(catalog.response(NOW))
    integer_fields = {"RecordID", "PackageID", "PackageDisplayScene", "Price", "PackageRepeatCount",
        "PackageCaption", "PackageDescription", "BtnPackObjectID", "PackageFlag", "DisplayIndex",
        "PackageItemType", "PackageItemNum", "PackagePayCrystalNum", "PackageFreeCrystalNum", "PackageCardID",
        "PackageUnitLevel", "PackageUnitSkillLevel", "PackageUnitCost", "PresentPackageID", "PresentIconTextureID",
        "PackageSummonConfigID", "LoginBonusID", "AppealIconHome", "AppealIconShop", "PackageUnitClassChange", "WidgetID"}
    string_fields = {"SkuID", "PackageIconPath", "PackageExpression", "OpenDays", "CloseDays", "ItemID", "PackageTextuerID"}
    assert set(row) == integer_fields | string_fields
    assert all(type(row[name]) is int for name in integer_fields)
    assert all(type(row[name]) is str for name in string_fields)
    assert row["WidgetID"] == 0


def renamed(raw, premium, replacement):
    passes = parse_table(decode(raw["EventPassConfig.atb"]))["rows"]
    items = parse_table(decode(raw["ItemConfigTable.atb"]))["rows"]
    index = next(index for index, row in enumerate(items) if row["ItemID"] == premium)
    identity = (zlib.crc32(replacement.encode("utf-8")) ^ 0x80000000) - 0x80000000
    return {"EventPassConfig.atb": patch_table_cells(raw["EventPassConfig.atb"], {
                number: {"PremiumPassItemID": replacement}
                for number, row in enumerate(passes) if row["PremiumPassItemID"] == premium}),
            "ItemConfigTable.atb": patch_table_cells(raw["ItemConfigTable.atb"], {
                index: {"ItemID": replacement, "ID": identity}})}


@pytest.fixture
def renamed_factory(sources):
    raw, effective, _, _ = sources
    project = Path(__file__).resolve().parents[1]

    def make(sku_bytes, identifier="20261015"):
        prefix = SKU_PREFIX + "event_pass_premium_"
        replacement = "event_pass_premium_" + "x" * (sku_bytes - len(prefix))
        data = effective | renamed(raw, "event_pass_premium_" + identifier, replacement)
        items = PossessionService(project / "runtime/assets", asset_reader=data.__getitem__)
        revival = build_event_pass_revival(data["EventPassConfig.atb"], data["GloryConditionConfig.atb"],
            EventPassRevivalRules(PASS, {1611: 2}, start_at="2026/11/01 00:00:00", end_at="2026/11/02 00:00:00"))
        data.update(revival.replacements)
        ep = EventPassCatalog(data.__getitem__, items)
        return PackageCatalog(ep, items, data.__getitem__, enabled=True), replacement
    return make


def test_real_skus_fit_the_native_cell(factory):
    catalog, _, _ = factory()
    assert catalog.status()["excluded_offers"] == []
    assert all(len(offer.sku.encode("utf-8")) <= NATIVE_CELL_BYTES for offer in catalog.offers)


@pytest.mark.parametrize("sku_bytes,listed", [(NATIVE_CELL_BYTES, True), (NATIVE_CELL_BYTES + 1, False)])
def test_offer_whose_sku_exceeds_the_native_cell_is_excluded_and_reported(renamed_factory, sku_bytes, listed):
    catalog, premium = renamed_factory(sku_bytes)
    sku = SKU_PREFIX + premium
    assert len(sku.encode("utf-8")) == sku_bytes and catalog.status()["ready"] is True
    assert len(catalog.offers) == (6 if listed else 5)
    assert (sku in catalog.by_sku, PASS in catalog.by_normal_id) == (listed, listed)
    assert [offer.sku for offer in catalog.available(NOW)] == ([sku] if listed else [])
    assert [row["SkuID"] for row in rows(catalog.response(NOW))] == ([sku] if listed else [])
    assert catalog.status()["excluded_offers"] == ([] if listed else [{
        "normal_pass_id": PASS, "item_id": premium, "reason": "sku_exceeds_native_cell",
        "sku_bytes": sku_bytes, "limit": NATIVE_CELL_BYTES}])
    if not listed:
        with pytest.raises(ProtocolError) as error:
            catalog.get(sku)
        assert error.value.code == "package_unknown"


def test_excluding_an_earlier_offer_keeps_later_offers_on_their_own_windows(renamed_factory):
    catalog, premium = renamed_factory(NATIVE_CELL_BYTES + 1, "20260611")
    assert [row["normal_pass_id"] for row in catalog.status()["excluded_offers"]] == ["event_pass_20260611"]
    offer, = catalog.available(NOW)
    assert offer.normal_pass_id == PASS and offer.sku == "local_event_pass_premium_20261015"
    assert [row["SkuID"] for row in rows(catalog.response(NOW))] == [offer.sku]
