from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.emblem_catalog import EmblemCatalog
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.gift_catalog import GiftCatalog
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES, PossessionService
from aigis_ps.resource_library import ResourceLibrary
import aigis_ps.tower_trade_catalog as module
from aigis_ps.tower_trade_catalog import FIELDS, ITEM_FIELDS, UI_FIELDS, TowerTradeCatalog
from aigis_ps.tower_trade_state import TowerTradeHistory
from test_public_tables import digested
from test_trade_catalog import name_hash, native_date, table


CARD_MASTER = b"frozen-card-master"


def product(identifier=1, **changes):
    result = {name: "" if kind == 32 else [0, 0] if kind == 9 else 0 for name, kind in FIELDS.items()}
    result.update(ID=identifier, TradeCount=1, CardID=7, RequiredGeneralItemID="tower_crystal_110001",
                  RequiredValue=100, CategoryType=1)
    result.update(changes)
    return result


def sources(rows=None, *, items=None):
    rows = [product()] if rows is None else rows
    items = ({"tower_crystal_110001": (999999, 6), "gift": (999, 4), "ticket": (0, 2),
              "emblem": (1, 10)} if items is None else items)
    ui = [{"MissionID": 110001, "CrystalSpriteNode": "SP00", "CrystalNameNode": "TX00", "CrystalValueNode": "TX01"}]
    return {"TowerTradeConfig.atb": table(rows, FIELDS), "TowerTradeConfig110001.atb": table(rows, FIELDS),
            "TowerTradeUIConfig.atb": table(ui, UI_FIELDS), "ItemConfigTable.atb": table([
                {"ItemID": name, "ID": name_hash(name), "Stack": stack, "EffectType": effect}
                for name, (stack, effect) in items.items()], ITEM_FIELDS)}


def build(data=None, *, emblems=None, master=CARD_MASTER):
    data = sources() if data is None else data
    possessions = PossessionService(Path("."), asset_reader=data.__getitem__)
    cards = SimpleNamespace(card_ids=(1, 7, 8), resource_version="fixture-v1",
                            snapshot=SimpleNamespace(get_xml=lambda _: master))
    if emblems is True:
        emblems = SimpleNamespace(content_version="a" * 64, item_resource_sha256=possessions.asset_sha256,
            titles=(SimpleNamespace(index=18), SimpleNamespace(index=19)), item_ids=frozenset({"emblem"}),
            is_ready=lambda: True)
    return TowerTradeCatalog(data.__getitem__, possessions, cards, emblems=emblems)


def invalid(callback, code="tower_trade_catalog_invalid"):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert caught.value.code == code


@pytest.fixture(scope="module")
def native_catalog():
    project = Path(__file__).resolve().parents[1]
    if not (project / "runtime/server/masterdata/active.json").is_file():
        pytest.skip("Verified public masters and local resources are required")
    snapshot = MasterDataStore(project / "runtime/server/masterdata").snapshot()
    manifest, entries = ResourceLibrary(project / "runtime/resources", client_hash=snapshot.client_hash).load()
    assets = IndexedAssetService(entries, project / "runtime/resources", project.parent / "aigisr_exe")
    reader = lambda name: assets.read(name)[0]
    possessions = PossessionService(project.parent / "aigisr_exe", asset_reader=reader)
    cards = GiftCatalog(snapshot, reader, possessions, resource_version=manifest["version"])
    return TowerTradeCatalog(reader, possessions, cards, emblems=EmblemCatalog(possessions, reader))


def test_current_resource_has_all_three_currencies_and_keeps_unsupported_tickets(native_catalog):
    status = native_catalog.status()
    assert status["ready"] and status["products"] == 170
    assert status["kinds"] == {"unit": 90, "item": 80}
    assert status["variants"] == {110001: 176, 110002: 0, 110003: 0}
    assert native_catalog.currencies == tuple(f"tower_crystal_{number}" for number in (110001, 110002, 110003))
    assert status["unsupported"] == {key: "summon_pool_unverified" for key in (10009, 10010, 10011, 12011, 12012, 12013)}
    assert sum(product.item_effect_type == 10 for product in native_catalog.products) == 66
    assert sum(product.item_effect_type == 4 for product in native_catalog.products) == 8
    assert native_catalog.product(10000).tiers == ((1, 20000), (2, 15000))
    assert native_catalog.product(10009).tiers == ((1, 10000), (2, 15000), (3, 20000))
    assert sum(len(product.tiers) for product in native_catalog.products) == 176
    assert all(product.start_at is None and product.end_at is None for product in native_catalog.products)


def test_continuations_preserve_product_head_metadata_and_skip_display_fields():
    rows = [product(Limitation=3, SingleBuyOnly=1), product(0, CardID=0, RequiredGeneralItemID="",
        TradeCount=2, RequiredValue=150, SingleBuyOnly=0, CategoryType=0), product(2, CardID=8)]
    subject = build(sources(rows))
    assert subject.product(1).tiers == ((1, 100), (2, 150))
    assert subject.product(1).limitation == 3 and subject.product(1).single_buy_only is True
    assert subject.product(2).source_row == 2


def test_item_and_title_metadata_preserve_disclosure_and_delivery_restrictions():
    rows = [product(1, CardID=0, GeneralItemID="gift"), product(2, CardID=0, GeneralItemID="ticket"),
            product(3, CardID=0, GeneralItemID="emblem", DiscloseEmblemIDs="18,19,", DiscloseGeneralItemID="gift")]
    subject = build(sources(rows), emblems=True)
    assert (subject.product(1).kind, subject.product(1).item_stack) == ("item", 999)
    assert subject.product(2).unsupported_reason == "summon_pool_unverified"
    assert subject.product(3).disclose_emblem_ids == (18, 19)
    assert subject.product(3).disclose_item_id == "gift"
    assert subject.product(3).unsupported_reason is None
    assert build(sources(rows)).product(3).unsupported_reason == "emblem_catalog_unavailable"


@pytest.mark.parametrize("stored,fits", [("n" * STORED_ITEM_NAME_BYTES, "n" * MAX_ITEM_NAME_BYTES),
                                         (chr(0x1F600) * 64, chr(0x1F600) * 63 + "nnn")], ids=["ascii", "utf8"])
def test_item_named_beyond_the_native_cell_is_unsupported_and_refused(stored, fits):
    assert (len(stored.encode("utf-8")), len(fits.encode("utf-8"))) == (STORED_ITEM_NAME_BYTES, MAX_ITEM_NAME_BYTES)
    items = {"tower_crystal_110001": (999999, 6), stored: (99, 4), fits: (99, 4)}
    subject = build(sources([product(1, CardID=0, GeneralItemID=stored), product(2, CardID=0, GeneralItemID=fits)],
                            items=items))
    status = subject.status()
    assert status["ready"] is True and status["unsupported"] == {1: "item_name_exceeds_native_cell"}
    assert subject.product(2).unsupported_reason is None
    history, saved = TowerTradeHistory(subject), {"bootstrap_schema": 1, "tables": {}}
    now = datetime(2026, 11, 1, tzinfo=JST)
    with pytest.raises(ProtocolError) as caught:
        history.quote(saved, subject.product(1), 1, now)
    assert (caught.value.code, caught.value.status) == ("tower_trade_unsupported", 501)
    assert history.quote(saved, subject.product(2), 1, now).total_price == 100


def test_dates_are_frozen_jst_values_with_independent_empty_boundaries():
    start, end = datetime(2026, 11, 1, tzinfo=JST), datetime(2026, 12, 1, tzinfo=JST)
    subject = build(sources([product(StartDateTime=native_date(start)), product(2, EndDateTime=native_date(end))]))
    assert subject.product(1).start_at == start and subject.product(1).end_at is None
    assert subject.product(2).start_at is None and subject.product(2).end_at == end


@pytest.mark.parametrize("rows", [
    [], [product(0, CardID=0, RequiredGeneralItemID="", TradeCount=2)],
    [product(), product()], [product(TradeCount=2)], [product(CardID=999)],
    [product(CardID=0)], [product(GeneralItemID="gift")], [product(CardID=0, GeneralItemID="missing")],
    [product(RequiredGeneralItemID="gift")], [product(CategoryType=2)], [product(SingleBuyOnly=2)],
    [product(DiscloseGeneralItemID="missing")], [product(DiscloseEmblemIDs="18,18,")],
    [product(DiscloseEmblemIDs="code()")], [product(RequiredValue=0)],
    [product(), product(0, CardID=0, RequiredGeneralItemID="", TradeCount=1)],
    [product(), product(0, CardID=0, RequiredGeneralItemID="", TradeCount=2, Limitation=5)],
    [product(StartDateTime=native_date(datetime(2026, 12, 1)), EndDateTime=native_date(datetime(2026, 11, 1)))],
])
def test_ambiguous_or_invalid_catalog_rows_fail_closed(rows):
    invalid(lambda: build(sources(rows)))


def test_different_nonempty_variant_requires_explicit_contract_and_emblem_references_are_verified():
    data = sources()
    data["TowerTradeConfig110001.atb"] = table([product(2)], FIELDS)
    invalid(lambda: build(data))
    invalid(lambda: build(sources([product(DiscloseEmblemIDs="99,")]), emblems=True))


@pytest.mark.parametrize("identifier", [True, False, 0, -1, "1", 1.0, None, 999])
def test_product_identity_rejects_unknown_and_coerced_values(identifier):
    subject = build()
    invalid(lambda: subject.product(identifier), "tower_trade_product_unknown")


def test_products_and_metadata_are_immutable_and_diagnostics_detached():
    data = sources()
    original = deepcopy(data)
    subject = build(data)
    with pytest.raises(FrozenInstanceError):
        subject.product(1).limitation = 0
    with pytest.raises(TypeError):
        subject.by_id[2] = subject.product(1)
    with pytest.raises(TypeError):
        subject.source_hashes["changed"] = "changed"
    assert subject.product(1) is subject.products[0]
    subject.status()["currencies"].clear()
    subject.status()["source_hashes"].clear()
    data["TowerTradeConfig.atb"] = b"changed"
    assert subject.product(1).shop_id == 1 and subject.status()["source_hashes"]
    assert original["TowerTradeConfig.atb"] == subject._resources["TowerTradeConfig.atb"]


@pytest.mark.parametrize("attribute", TowerTradeCatalog._snapshot_names())
def test_published_catalog_replacement_is_rejected(attribute):
    subject = build()
    setattr(subject, attribute, object())
    invalid(lambda: subject.product(1), "tower_trade_catalog_changed")


@pytest.mark.parametrize("change", ["cards", "master", "version", "stacks", "emblems", "unready_possessions",
                                    "unready_emblems"])
def test_dependency_drift_is_rejected(change):
    subject = build(emblems=True)
    if change == "cards":
        subject.cards.card_ids = (1, 7)
    elif change == "master":
        subject.cards.snapshot.get_xml = lambda _: b"changed-master"
    elif change == "version":
        subject.cards.resource_version = "new-version"
    elif change == "stacks":
        subject.possessions._definitions["gift"] = 10
    elif change == "unready_possessions":
        subject.possessions.is_ready = lambda: False
    elif change == "unready_emblems":
        subject.emblems.is_ready = lambda: False
    else:
        subject.emblems.content_version = "b" * 64
    invalid(lambda: subject.product(1), "tower_trade_catalog_changed")


def test_readiness_never_builds_a_dependency_status(monkeypatch):
    monkeypatch.setattr(PossessionService, "status", lambda _: pytest.fail("readiness must not build the possession status"))
    subject = build(emblems=True)
    subject.emblems.status = lambda: pytest.fail("readiness must not build the title status")
    assert subject.status()["ready"] is True and subject.product(1).shop_id == 1


def test_unready_possessions_refuse_the_catalog(monkeypatch):
    monkeypatch.setattr(PossessionService, "is_ready", lambda _: False)
    invalid(build)


@pytest.mark.parametrize("master", [bytearray(CARD_MASTER), b"", None, CARD_MASTER.decode()],
                         ids=["mutable", "empty", "missing", "text"])
def test_construction_requires_an_immutable_nonempty_card_master(master):
    invalid(lambda: build(master=master))


def test_construction_bounds_the_card_master(monkeypatch):
    monkeypatch.setattr(module, "MAX_MASTER_BYTES", len(CARD_MASTER))
    assert build().product(1).shop_id == 1
    monkeypatch.setattr(module, "MAX_MASTER_BYTES", len(CARD_MASTER) - 1)
    invalid(build)


def test_card_master_is_digested_once_per_bytes_object(digested):
    subject = build()
    verified = subject.cards.snapshot.get_xml("CardData")
    for _ in range(3):
        assert subject.status()["ready"] is True
    assert subject.product(1).shop_id == 1
    assert len(digested) == 1 and digested[0] is verified
    equal = bytes(bytearray(verified))
    subject.cards.snapshot.get_xml = lambda _: equal
    for _ in range(3):
        assert subject.status()["ready"] is True
    assert len(digested) == 2 and digested[1] is equal
    changed = verified + b"changed"
    subject.cards.snapshot.get_xml = lambda _: changed
    for _ in range(2):
        assert subject.status()["error"] == "tower_trade_catalog_changed"
    assert len(digested) == 3 and digested[2] is changed
    invalid(lambda: subject.product(1), "tower_trade_catalog_changed")
    assert len(digested) == 3


def test_mutable_card_master_is_checked_on_every_readiness_check():
    subject = build()
    buffer = bytearray(subject.cards.snapshot.get_xml("CardData"))
    subject.cards.snapshot.get_xml = lambda _: buffer
    assert subject.status()["ready"] is True
    buffer[0] ^= 1
    assert subject.status()["error"] == "tower_trade_catalog_changed"
    invalid(lambda: subject.product(1), "tower_trade_catalog_changed")


def test_real_card_master_is_not_digested_again(native_catalog, digested):
    master = native_catalog.cards.snapshot.get_xml("CardData")
    assert native_catalog.status()["ready"] is True
    assert native_catalog.product(10000).shop_id == 10000
    assert not any(data is master for data in digested)
