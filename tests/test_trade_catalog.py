from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import struct
from types import SimpleNamespace
import zlib

import pytest

from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.barracks_service import EXTRA_SHIFT, MAXIMUM_EXTRA_BARRACKS, PURCHASE_FIELD
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.gift_catalog import GiftCatalog
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.player_state import BARRACKS_SHIFT
from aigis_ps.possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES, PossessionService
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.trade_catalog import MAX_EARLIER_RESOURCES, PRODUCT_UNKNOWN, SHOP_FIELDS, SHOP_RESOURCE, TradeCatalog
from test_al_format import schema_bytes


def name_hash(name):
    return (zlib.crc32(name.encode()) ^ 0x80000000) - 0x80000000


def table(rows, fields):
    offset, schema = 0, []
    for name, kind in fields.items():
        schema.append((name, offset, kind, b"" if kind == 32 else b"\0" * 8))
        offset += 8 if kind == 9 else 4
    encoded_schema = schema_bytes(schema, offset)
    records, pool = bytearray(), bytearray()
    for row in rows:
        for name, kind in fields.items():
            value = row[name]
            if kind == 32:
                records += struct.pack("<I", len(pool))
                pool += value.encode() + b"\0"
            elif kind == 9:
                records += struct.pack("<II", *value)
            else:
                records += struct.pack("<i", value)
    start = 28 + len(encoded_schema)
    return struct.pack("<4sBBHHHIII4s", b"ALTB", 1, 0x14, len(rows), 28, start, offset,
                       len(pool), start + len(records), b"shop") + encoded_schema + records + pool


def product(shop_id=1, **changes):
    result = {name: "" if kind == 32 else [0, 0] if kind == 9 else 0 for name, kind in SHOP_FIELDS.items()}
    result.update(ShopID=shop_id, CardID=7, ProductType=1, Type=4, TradeCount=1, RequiredValue=100)
    result.update(changes)
    return result


def native_date(value):
    milliseconds = ((value.replace(tzinfo=None) - datetime(1, 1, 1)).days * 86400000
                    + value.hour * 3600000 + value.minute * 60000 + value.second * 1000)
    return [milliseconds & 0xFFFFFFFF, milliseconds >> 32]


def resources(rows=None, *, inventory=None, specialty=None, stacks=None):
    stacks = {"coin": 10000, "reward": 99, "blocked": 0} if stacks is None else stacks
    item_rows = [{"ItemID": name, "ID": name_hash(name), "Stack": stack} for name, stack in stacks.items()]
    if inventory is None:
        inventory = [{"ConfigID": kind, "ShopType": 0, "TradeType": kind, "ConsumeItemID": name_hash("coin")}
                     for kind in (1, 2, 4, 5, 6, 9)]
    specialty = [] if specialty is None else specialty
    return {
        "Shop2ItemV2.atb": table([product()] if rows is None else rows, SHOP_FIELDS),
        "InventoryConfig.atb": table(inventory, {"ConfigID": 1, "ShopType": 1, "TradeType": 1, "ConsumeItemID": 1}),
        "UnitSpecialty.atb": table(specialty, {"ID_Card": 1, "Type_Specialty": 1, "Value_Specialty": 1,
            "Value_Param1": 1, "Value_Param2": 1, "Value_Param3": 1, "Value_Param4": 1, "Command": 32}),
        "ItemConfigTable.atb": table(item_rows, {"ItemID": 32, "ID": 1, "Stack": 1}),
    }


def effect(card=7, value=-50, kind=5):
    return {"ID_Card": card, "Type_Specialty": kind, "Value_Specialty": value,
            "Value_Param1": 0, "Value_Param2": 0, "Value_Param3": 0, "Value_Param4": 0, "Command": ""}


def swapped_fields(first="StartDateTime", second="EndDateTime"):
    names = list(SHOP_FIELDS)
    left, right = names.index(first), names.index(second)
    names[left], names[right] = names[right], names[left]
    return {name: SHOP_FIELDS[name] for name in names}


def fixture_cards(version="fixture-v1"):
    return SimpleNamespace(card_ids=(1, 7, 8, 309, 1989, 2079), resource_version=version,
        snapshot=SimpleNamespace(identifier="master", client_hash="a" * 64, get_xml=lambda _: b"card-master"))


def catalog(payloads=None, *, version="fixture-v1", cards=None, possessions=None, earlier_resources=None):
    payloads = resources() if payloads is None else payloads
    cards = cards or fixture_cards()
    possessions = possessions or PossessionService(Path("."), asset_reader=payloads.__getitem__)
    return TradeCatalog(cards, possessions, payloads.__getitem__, resource_version=version,
                        earlier_resources=earlier_resources)


def state():
    return {"bootstrap_schema": 1, "tables": {"config": [{PURCHASE_FIELD: MAXIMUM_EXTRA_BARRACKS << EXTRA_SHIFT}],
        "game_param": [{"Money": 10000}], "possessions": [{"ItemID": "coin", "Count": 1000}],
        "crystals": [{"FreeFlag": True, "Num": 200, "Date": 700000}, {"FreeFlag": False, "Num": 200, "Date": 700001}],
        "quest_records": [], "silver_house": [], "fame_depository": []},
        "player_state": {"schema_version": 1, "units": [{"CardID": 7, "GeneralPurposeFlags": 0}]}}


def quote(subject, saved=None, *, shop_id=1, quantity=1, count=0, now=None, registration=107):
    return subject.quote(state() if saved is None else saved, shop_id, quantity, count,
                         now or datetime(2026, 11, 1, tzinfo=JST), registration)


def fails(code, callback):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert caught.value.code == code


@pytest.fixture(scope="module")
def native_catalog():
    project = Path(__file__).resolve().parents[1]
    if not (project / "runtime/server/masterdata/active.json").is_file():
        pytest.skip("Verified local resources and public masters are required")
    snapshot = MasterDataStore(project / "runtime/server/masterdata").snapshot()
    manifest, entries = ResourceLibrary(project / "runtime/resources", client_hash=snapshot.client_hash).load()
    assets = IndexedAssetService(entries, project / "runtime/resources", project.parent / "aigisr_exe")
    reader = lambda name: assets.read(name)[0]
    possessions = PossessionService(project.parent / "aigisr_exe", asset_reader=reader)
    cards = GiftCatalog(snapshot, reader, possessions, resource_version=manifest["version"])
    return TradeCatalog(cards, possessions, reader, resource_version=manifest["version"])


def test_complete_native_catalog_and_stage_two_metadata(native_catalog):
    status = native_catalog.status()
    assert status["ready"] and status["products"] == 1820 and status["ordinary_products"] == 1791
    assert status["product_types"] == {1: 1560, 3: 255, 2: 1, 6: 4}
    assert len(status["unresolved_currency"]) == 38 and status["unsupported_item_names"] == []
    assert {name for name in status["resource_sha256"]} == {
        "Shop2ItemV2.atb", "InventoryConfig.atb", "UnitSpecialty.atb", "ItemConfigTable.atb"}
    assert (native_catalog.product(768).card_id, native_catalog.product(768).unit_evolution_count) == (2079, 2)
    assert (native_catalog.product(769).card_id, native_catalog.product(769).unit_evolution_count) == (1989, 2)
    assert native_catalog.product(2146).end_at == datetime(2026, 11, 12, 11, tzinfo=JST)
    assert sum(product.variable_price for product in native_catalog.products) == 5


def test_actual_ordinary_domain_price_and_currency_coverage(native_catalog):
    outcomes = {}
    for item in native_catalog.products:
        if not item.ordinary:
            continue
        saved = {"bootstrap_schema": 1, "tables": {"config": [{PURCHASE_FIELD: 0}],
            "game_param": [{"Money": 0x7FFFFFFF}], "possessions": [],
            "crystals": [{"FreeFlag": True, "Num": 100000, "Date": 700000}],
            "quest_records": [{"QuestID": identifier, "AchieveFlag": 0x01000000} for identifier in item.disclosure_quest_ids],
            "silver_house": [], "fame_depository": []}, "player_state": {"schema_version": 1, "units": []}}
        if item.currency_item_id:
            saved["tables"]["possessions"] = [{"ItemID": item.currency_item_id, "Count": 0x7FFFFFFF}]
        clock = item.start_at or (item.end_at - timedelta(seconds=1) if item.end_at else datetime(2026, 11, 1, tzinfo=JST))
        try:
            result = native_catalog.quote(saved, item.shop_id, 1, 0, clock, item.registration)
            assert result.unit_price == item.tiers[0][1]
            assert result.amount == item.adding_count
            outcomes[item.shop_id] = "quoted"
        except ProtocolError as error:
            outcomes[item.shop_id] = error.code
    assert len(outcomes) == 1791
    assert set(outcomes.values()) <= {"quoted", "trade_currency_unavailable"}
    assert sum(value == "quoted" for value in outcomes.values()) == 1753


def test_products_quotes_and_diagnostics_are_frozen_independent_snapshots():
    payloads = resources()
    subject = catalog(payloads)
    saved = state()
    before = deepcopy(saved)
    result = quote(subject, saved)
    assert (result.unit_price, result.total_price, result.amount, result.currency_kind, result.currency_item_id) == (
        100, 100, 1, "possession", "coin")
    assert saved == before
    with pytest.raises(FrozenInstanceError):
        result.unit_price = 1
    with pytest.raises(FrozenInstanceError):
        result.product.data_restrict = 0
    with pytest.raises(TypeError):
        subject._products[1] = None
    payloads["Shop2ItemV2.atb"] = b"changed"
    report = subject.status()
    report["resource_sha256"].clear()
    report["unresolved_currency"].append(1)
    assert quote(subject).unit_price == 100 and subject.status()["unresolved_currency"] == []


def test_trade_count_grouping_uses_strict_increase_not_shop_id_zero():
    rows = [product(1, RequiredValue=100), product(999, TradeCount=3, RequiredValue=200),
            product(2, TradeCount=1, RequiredValue=300)]
    subject = catalog(resources(rows))
    assert tuple(item.shop_id for item in subject.products) == (1, 2)
    assert subject.product(1).tiers == ((1, 100), (3, 200))
    assert [quote(subject, count=count).unit_price for count in (0, 1, 2, 3)] == [100, 200, 200, 200]


@pytest.mark.parametrize("single,tiers", [(1, [(1, 100)]), (0, [(1, 100), (2, 200)])])
def test_single_and_variable_price_maps_disallow_batch(single, tiers):
    rows = [product(1 if index == 0 else 0, TradeCount=end, RequiredValue=price, SingleBuyOnly=single)
            for index, (end, price) in enumerate(tiers)]
    subject = catalog(resources(rows))
    fails("trade_single_purchase", lambda: quote(subject, quantity=2))


def test_equal_price_tiers_allow_batch_and_quota_is_total_quantity():
    subject = catalog(resources([product(Data_Restrict=5), product(0, TradeCount=5)]))
    assert quote(subject, count=2, quantity=3).total_price == 300
    fails("trade_limit_reached", lambda: quote(subject, count=3, quantity=3))


def test_discount_uses_first_specialty_five_after_group_continuation():
    subject = catalog(resources([product(Type=2)], specialty=[effect(kind=4), effect(card=0, value=-20),
                        effect(card=0, value=-90), effect(card=8, value=-50)]))
    result = quote(subject, registration=105)
    assert (result.unit_price, result.ownership_discount) == (80, -20)
    saved = state()
    saved["player_state"]["units"][0]["CardID"] = 8
    assert quote(subject, saved, registration=105).unit_price == 100


@pytest.mark.parametrize("barracks,unlocked,discount", [(0, 0, True), (1, 1, True), (2, 2, True), (2, 1, False), (15, 2, False)])
def test_discount_scans_only_unlocked_barracks_and_ignores_lock_bits(barracks, unlocked, discount):
    subject = catalog(resources([product(Type=2)], specialty=[effect()]))
    saved = state()
    saved["tables"]["config"][0][PURCHASE_FIELD] = unlocked << EXTRA_SHIFT
    saved["player_state"]["units"][0]["GeneralPurposeFlags"] = -0x80000000 | barracks << BARRACKS_SHIFT | 6
    assert quote(subject, saved, registration=105).unit_price == (50 if discount else 100)


def test_silver_zero_row_still_counts_as_owned_but_card_history_and_mail_do_not():
    subject = catalog(resources([product(Type=2)], specialty=[effect()]))
    saved = state()
    saved["player_state"]["units"] = []
    saved["tables"].update(card_status=[{"CardID": 7}], presents=[{"CardID": 7}])
    assert quote(subject, saved, registration=105).ownership_discount == 0
    saved["silver_house_schema"] = 1
    saved["tables"]["silver_house"] = [{"CardID": 7, "Count": 0}]
    assert quote(subject, saved, registration=105).ownership_discount == -50


def test_nonempty_fame_supplies_verified_ownership_discount():
    saved = state()
    saved["fame_depository_schema"] = 1
    saved["tables"]["fame_depository"] = [{"CardId": 7, "Count0": 0, "Count1": 0, "Count2": 1, "Count3": 0, "Count4": 0}]
    saved["player_state"]["units"] = []
    subject = catalog(resources([product(Type=2)], specialty=[effect()]))
    assert quote(subject, saved, registration=105).ownership_discount == -50


def test_unrelated_prices_do_not_consume_fame_but_ownership_requires_valid_rows():
    saved = state()
    saved["tables"]["fame_depository"] = [{"Unparsed": "preserved"}]
    assert quote(catalog(), saved).unit_price == 100
    subject = catalog(resources([product(Type=2)], specialty=[effect()]))
    fails("fame_state_invalid", lambda: quote(subject, saved, registration=105))


@pytest.mark.parametrize("change,code", [
    (lambda saved: saved.update(bootstrap_schema=True), "trade_state_invalid"),
    (lambda saved: saved["tables"].update(fame_depository=None), "fame_state_invalid"),
    (lambda saved: saved["tables"]["config"][0].update({PURCHASE_FIELD: True}), "trade_state_invalid"),
    (lambda saved: saved["tables"]["config"][0].update({PURCHASE_FIELD: (MAXIMUM_EXTRA_BARRACKS + 1) << EXTRA_SHIFT}),
     "trade_state_invalid"),
    (lambda saved: saved["player_state"]["units"][0].update(GeneralPurposeFlags=True), "trade_state_invalid"),
    (lambda saved: saved["tables"].update(silver_house=[{"CardID": 7, "Count": 0}]), "trade_state_invalid"),
])
def test_bad_state_never_changes_input(change, code):
    subject = catalog(resources([product(Type=2)], specialty=[effect()]))
    saved = state()
    change(saved)
    before = deepcopy(saved)
    fails(code, lambda: quote(subject, saved, registration=105))
    assert saved == before


def test_time_uses_jst_and_exclusive_end_with_aware_input():
    start = datetime(2026, 11, 1, 9, tzinfo=JST)
    end = start + timedelta(hours=1)
    subject = catalog(resources([product(StartDateTime=native_date(start), EndDateTime=native_date(end))]))
    assert quote(subject, now=start.astimezone(timezone.utc)).unit_price == 100
    fails("trade_product_closed", lambda: quote(subject, now=start - timedelta(microseconds=1)))
    fails("trade_product_closed", lambda: quote(subject, now=end))
    fails("trade_time_invalid", lambda: quote(subject, now=datetime(2026, 11, 1)))


@pytest.mark.parametrize("clock", [None, "2026-11-01", datetime.max.replace(tzinfo=timezone.utc)])
def test_invalid_or_overflowing_clock_has_a_protocol_error(clock):
    fails("trade_time_invalid", lambda: catalog().quote(state(), 1, 1, 0, clock, 107))


@pytest.mark.parametrize("reset", [0, 1, 2])
def test_reset_type_is_retained_without_calendar_inference(reset):
    subject = catalog(resources([product(ResetType=reset, Data_Restrict=3)]))
    assert subject.product(1).reset_type == reset
    assert quote(subject, count=2, now=datetime(2030, 1, 1, tzinfo=JST)).exchange_count == 2
    fails("trade_limit_reached", lambda: quote(subject, count=3))


def test_disclosure_requires_every_completed_quest():
    subject = catalog(resources([product(DisclosureQuestIdList="1001,1002,")]))
    saved = state()
    saved["tables"]["quest_records"] = [{"QuestID": 1001, "AchieveFlag": 0x01000000},
        {"QuestID": 1002, "AchieveFlag": 0x40000000}]
    fails("trade_quest_required", lambda: quote(subject, saved))
    saved["tables"]["quest_records"][1]["AchieveFlag"] |= 0x01000000
    assert quote(subject, saved).unit_price == 100


@pytest.mark.parametrize("kind,registration,currency", [(1, 104, "possession"), (2, 105, "possession"),
    (4, 107, "possession"), (5, 109, "possession"), (6, 110, "possession"),
    (7, 111, "money"), (8, 112, "crystals"), (9, 113, "possession")])
def test_all_currency_operations_and_balance_checks(kind, registration, currency):
    subject = catalog(resources([product(Type=kind, ConsumeItemID="coin")]))
    assert quote(subject, registration=registration).currency_kind == currency
    fails("trade_registration_mismatch", lambda: quote(subject, registration=108))
    saved = state()
    saved["tables"]["possessions"][0]["Count"] = 0
    saved["tables"]["game_param"][0]["Money"] = 0
    saved["tables"]["crystals"] = []
    fails("trade_currency_insufficient", lambda: quote(subject, saved, registration=registration))


def test_equivalent_inventory_rows_are_allowed_conflicts_rejected():
    row = {"ConfigID": 1, "ShopType": 0, "TradeType": 4, "ConsumeItemID": name_hash("coin")}
    subject = catalog(resources(inventory=[row, {**row, "ConfigID": 2}]))
    assert quote(subject).currency_item_id == "coin"
    fails("trade_catalog_invalid", lambda: catalog(resources(inventory=[row,
        {**row, "ConfigID": 2, "ConsumeItemID": name_hash("reward")} ])))


def test_missing_currency_is_preserved_as_product_and_rejected_on_quote():
    subject = catalog(resources([product(Type=9, ConsumeItemID="old_currency")]))
    assert subject.product(1).ordinary and subject.product(1).currency_kind is None
    assert subject.status()["unresolved_currency"] == [1]
    fails("trade_currency_unavailable", lambda: quote(subject, registration=113))


def test_item_adding_count_and_full_stock_limits():
    subject = catalog(resources([product(ProductType=3, CardID=0, ItemID="reward", AddingCount=3)]))
    saved = state()
    saved["tables"]["possessions"].append({"ItemID": "reward", "Count": 93})
    result = quote(subject, saved, quantity=2)
    assert result.amount == 6 and result.total_price == 200
    fails("trade_item_capacity", lambda: quote(subject, saved, quantity=3))
    subject = catalog(resources([product(ProductType=3, CardID=0, ItemID="blocked")]))
    fails("trade_item_capacity", lambda: quote(subject))


@pytest.mark.parametrize("stored,fits", [("r" * STORED_ITEM_NAME_BYTES, "r" * MAX_ITEM_NAME_BYTES),
                                         (chr(0x1F600) * 64, chr(0x1F600) * 63 + "rrr")], ids=["ascii", "utf8"])
def test_item_reward_named_beyond_the_native_cell_is_listed_and_refused(stored, fits):
    assert (len(stored.encode("utf-8")), len(fits.encode("utf-8"))) == (STORED_ITEM_NAME_BYTES, MAX_ITEM_NAME_BYTES)
    rows = [product(1, ProductType=3, CardID=0, ItemID=stored), product(2, ProductType=3, CardID=0, ItemID=fits)]
    subject = catalog(resources(rows, stacks={"coin": 10000, stored: 99, fits: 99}))
    status = subject.status()
    assert status["ready"] is True and status["unsupported_item_names"] == [stored]
    assert status["unresolved_item"] == [] and subject.product(1).ordinary
    with pytest.raises(ProtocolError) as caught:
        quote(subject, shop_id=1)
    assert (caught.value.code, caught.value.status) == ("trade_product_unsupported", 501)
    assert quote(subject, shop_id=2).amount == 1
    assert catalog().status()["unsupported_item_names"] == []


@pytest.mark.parametrize("changes,registration", [({"Type": 3}, 106), ({"ProductType": 2}, 108),
    ({"ShopType": 2, "ProductType": 6, "Type": 9}, 113)])
def test_special_products_are_preserved_and_explicitly_rejected(changes, registration):
    subject = catalog(resources([product(**changes)]))
    assert len(subject.products) == 1 and not subject.product(1).ordinary
    fails("trade_product_unsupported", lambda: quote(subject, registration=registration))


def test_unproved_nonempty_condition_is_not_treated_as_free_permission():
    subject = catalog(resources([product(BuyConditionItemID="coin")]))
    fails("trade_condition_unsupported", lambda: quote(subject))


@pytest.mark.parametrize("quantity,count", [(0, 0), (-1, 0), (True, 0), (1, True), (1, -1),
    (0x80000000, 0), (1, 0x7FFFFFFF)])
def test_quantity_types_and_arithmetic_are_bounded(quantity, count):
    fails("trade_quantity_invalid", lambda: quote(catalog(), quantity=quantity, count=count))


def test_price_and_output_amount_overflow_are_rejected_without_clamping():
    subject = catalog(resources([product(RequiredValue=0x7FFFFFFF)]))
    fails("trade_amount_invalid", lambda: quote(subject, quantity=2))
    subject = catalog(resources([product(AddingCount=0x7FFFFFFF)]))
    fails("trade_amount_invalid", lambda: quote(subject, quantity=2))
    subject = catalog(resources([product(Type=2, RequiredValue=20)], specialty=[effect(value=-50)]))
    fails("trade_amount_invalid", lambda: quote(subject, registration=105))


@pytest.mark.parametrize("rows", [[], [product(), product()], [product(ShopID=0)],
    [product(TradeCount=0)], [product(RequiredValue=-1)], [product(ResetType=3)],
    [product(DisclosureQuestIdList="1;2")], [product(StartDateTime=[0, 0xFFFFFFFF])],
    [product(StartDateTime=[1000, 0], EndDateTime=[1000, 0])]])
def test_invalid_catalog_rows_fail_before_exposing_products(rows):
    fails("trade_catalog_invalid", lambda: catalog(resources(rows)))


def test_fingerprint_tracks_resource_version_master_and_payload():
    first = catalog()
    changed = catalog(resources([product(RequiredValue=101)]))
    assert first.content_version != changed.content_version
    fails("trade_catalog_invalid", lambda: catalog(version="other"))
    cards = SimpleNamespace(card_ids=(7,), resource_version="fixture-v1",
        snapshot=SimpleNamespace(identifier="other-master", client_hash="b" * 64, get_xml=lambda _: b"new-master"))
    assert catalog(cards=cards).content_version != first.content_version


def test_shop_columns_must_keep_the_native_order_the_client_reads_by_position():
    payloads = resources()
    payloads[SHOP_RESOURCE] = table([product()], swapped_fields())
    fails("trade_catalog_invalid", lambda: catalog(payloads))
    payloads[SHOP_RESOURCE] = table([product()], swapped_fields("TradeCount", "RequiredValue"))
    fails("trade_catalog_invalid", lambda: catalog(payloads))


def test_earlier_resource_identities_replay_the_content_version_they_had():
    previous = resources([product(RequiredValue=90)])
    earlier = catalog(previous, version="fixture-v0", cards=fixture_cards("fixture-v0"))
    calls = []

    def provider():
        calls.append(len(calls))
        return {"fixture-v0": {SHOP_RESOURCE: hashlib.sha256(previous[SHOP_RESOURCE]).hexdigest()}}

    current = catalog(earlier_resources=provider)
    assert calls == [] and current.content_version != earlier.content_version
    replayable = current.replayable_content_versions()
    assert replayable == {current.content_version, earlier.content_version}
    assert current.replayable_content_versions() is replayable and calls == [0]
    assert current.content_version_for("fixture-v1", {}) == current.content_version
    assert current.content_version_for("fixture-v0", {SHOP_RESOURCE: hashlib.sha256(
        previous[SHOP_RESOURCE]).hexdigest()}) == earlier.content_version
    plain = catalog()
    assert plain.content_version == current.content_version
    assert plain.replayable_content_versions() == {plain.content_version}


def unreadable_identities():
    raise ValueError("Earlier resources are unreadable")


@pytest.mark.parametrize("provider", [
    lambda: [], lambda: {"": {}}, lambda: {"fixture-v0": []}, lambda: {"fixture-v0": {"Other.atb": "0" * 64}},
    lambda: {"fixture-v0": {SHOP_RESOURCE: "A" * 64}}, lambda: {f"v{index}": {} for index in range(MAX_EARLIER_RESOURCES + 1)},
    unreadable_identities])
def test_invalid_earlier_identities_refuse_replay_instead_of_matching_nothing(provider):
    subject = catalog(earlier_resources=provider)
    fails("trade_catalog_invalid", subject.replayable_content_versions)


def test_a_failed_earlier_resolution_is_asked_again_on_the_next_replay():
    answers = [unreadable_identities, dict]
    subject = catalog(earlier_resources=lambda: answers.pop(0)())
    fails("trade_catalog_invalid", subject.replayable_content_versions)
    assert subject.replayable_content_versions() == {subject.content_version} and answers == []


def test_earlier_identities_require_a_provider_rather_than_a_value():
    fails("trade_catalog_invalid", lambda: catalog(earlier_resources={"fixture-v0": {}}))


def test_possession_resource_identity_cannot_be_mixed_or_changed_after_setup():
    payloads = resources()
    holdings = PossessionService(Path("."), asset_reader=payloads.__getitem__)
    subject = catalog(payloads, possessions=holdings)
    holdings.asset_sha256 = "0" * 64
    fails("trade_catalog_changed", lambda: quote(subject))
    fails("trade_catalog_invalid", lambda: catalog(payloads, possessions=holdings))


@pytest.mark.parametrize("change", [None, "issues", "asset", "unready"])
def test_readiness_matches_status_without_building_it(monkeypatch, change):
    payloads = resources()
    holdings = PossessionService(Path("."), asset_reader=payloads.__getitem__)
    subject = catalog(payloads, possessions=holdings)
    if change == "issues":
        holdings.issues["item_definitions"] = "Item definitions changed after setup"
    elif change == "asset":
        holdings.asset_sha256 = "0" * 64
    elif change == "unready":
        monkeypatch.setattr(holdings, "is_ready", lambda: False)
    ready = subject.status()["ready"]
    assert ready is (change is None)
    monkeypatch.setattr(subject, "status", lambda: pytest.fail("Readiness built the full catalog status"))
    monkeypatch.setattr(holdings, "status", lambda: pytest.fail("Readiness built the possession status"))
    assert subject.is_ready() is ready
    if ready:
        assert quote(subject).unit_price == 100
    else:
        fails("trade_catalog_changed", lambda: quote(subject))
        fails("trade_catalog_invalid", lambda: catalog(payloads, possessions=holdings))


def test_unknown_shop_identifier_has_stable_error_for_preserving_old_history():
    assert PRODUCT_UNKNOWN == "trade_product_unknown"
    subject = catalog()
    for identifier in (0, 2, True, "1", 0x80000000):
        fails("trade_product_unknown", lambda: subject.product(identifier))


def test_unit_capacity_is_left_to_transaction_service():
    subject = catalog()
    saved = state()
    saved["player_state"]["units"] = [deepcopy(saved["player_state"]["units"][0]) for _ in range(4096)]
    assert quote(subject, saved).amount == 1
