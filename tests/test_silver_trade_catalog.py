from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.gift_catalog import GiftCatalog
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.player_state import UNIT_BOOLEANS, UNIT_FIELDS
from aigis_ps.possessions import PossessionService
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.silver_trade import PAYMENT_HASH, PAYMENT_ITEM, SilverTradeCatalog
from aigis_ps.system_literals import encode_command
from aigis_ps.trade_catalog import TradeCatalog
from test_public_tables import digested
from test_trade_catalog import name_hash, native_date, product, resources, table


NOW = datetime(2026, 11, 1, tzinfo=JST)
CLASS_SCHEMA = {"ID_Config": 1, "ID_Class": 1}
COMMAND_SCHEMA = {"_ID": 1, "_Cmnd": 32}


def setup_case(*, products=None, groups=None, commands=None):
    rows = {identifier: {"CardID": identifier, "Rare": 2 if identifier in (7, 8) else 5}
            for identifier in (1, 7, 8, 9, 309)}
    cards = SimpleNamespace(card_ids=tuple(rows), card=rows.__getitem__, rows=rows,
        resource_version="fixture-v1", snapshot=SimpleNamespace(identifier="fixture", client_hash="a" * 64,
                                                               get_xml=lambda _: b"frozen-card-master"))
    payloads = resources(products if products is not None else [
        product(1, Type=3, Type_Catalog=27, SingleBuyOnly=1, RequiredValue=20),
        product(2, Type=3, Type_Catalog=42, CardID=8, SingleBuyOnly=1, RequiredValue=20),
        product(3, Type=7, RequiredValue=30)],
        stacks={PAYMENT_ITEM: 9999, "coin": 1000, "reward": 99})
    payloads["ShopCatalogCofig.atb"] = table(groups if groups is not None else [
        {"ID_Config": 0, "ID_Class": 0}, {"ID_Config": 27, "ID_Class": 10000},
        {"ID_Config": 0, "ID_Class": 10100}, {"ID_Config": 42, "ID_Class": 10200},
        {"ID_Config": 27, "ID_Class": 10400}], CLASS_SCHEMA)
    payloads["OnCommand.atb"] = table(commands or [], COMMAND_SCHEMA)
    possessions = PossessionService(Path("."), asset_reader=payloads.__getitem__)
    base = TradeCatalog(cards, possessions, payloads.__getitem__, resource_version=cards.resource_version)
    return SimpleNamespace(cards=cards, base=base, possessions=possessions, payloads=payloads)


def create(case):
    return SilverTradeCatalog(case.base, case.cards, case.payloads.__getitem__)


def unit(card_id=7, *, index=5, unique=9, class_id=10000, flags=1):
    row = {name: False if name in UNIT_BOOLEANS else 0 for name, _ in UNIT_FIELDS}
    row.update(CardID=card_id, GetIndex=index, UniqueID=unique, ClassID=class_id,
               GeneralPurposeFlags=flags, SkillLevel=1, OrganizationIndex=-1)
    return row


def state():
    return {"bootstrap_schema": 1, "tables": {"possessions": [{"ItemID": PAYMENT_ITEM, "Count": 100}],
            "game_param": [{"Money": 100}], "quest_records": []},
            "player_state": {"schema_version": 1, "units": [unit(1, index=0, unique=1), unit()],
                             "squads": [], "battle": {"VN": "1.0.1", "UX": 5, "TN": 0, "RTN": 64},
                             "legacy_squad_imported": True}}


def select(subject, saved=None, *, shop_id=1, index=5, unique=9):
    saved = state() if saved is None else saved
    return subject.material(saved["player_state"], subject.product(shop_id), index, unique, state=saved)


def fails(code, action, status=None):
    with pytest.raises(ProtocolError) as error:
        action()
    assert error.value.code == code
    if status is not None:
        assert error.value.status == status


def test_products_override_payment_without_changing_base_catalog():
    case = setup_case()
    before = case.base.content_version, case.base.products
    subject = create(case)
    assert subject.base is case.base
    assert tuple(row.shop_id for row in subject.products) == (1, 2)
    selected = subject.product(1)
    assert (selected.trade_type, selected.registration, selected.currency_kind, selected.currency_item_id) == (
        3, 106, "possession", PAYMENT_ITEM)
    assert not selected.ordinary
    assert (case.base.content_version, case.base.products) == before
    assert case.base.product(1).registration is None and case.base.product(1).currency_kind is None
    with pytest.raises(FrozenInstanceError):
        selected.required_value = 0
    with pytest.raises(TypeError):
        subject._groups[27] = ()
    status = subject.status()
    status["resource_sha256"].clear()
    assert subject.status()["resource_sha256"]


def test_catalog_continuations_and_repeated_keys_preserve_all_or_branches():
    subject = create(setup_case())
    assert subject._groups[27] == (10000, 10100, 10400)
    for class_id in (10000, 10001, 10099, 10120, 10450):
        saved = state()
        saved["player_state"]["units"][-1]["ClassID"] = class_id
        assert select(subject, saved) is saved["player_state"]["units"][-1]
    fails("silver_trade_material_ineligible", lambda: select(subject, shop_id=2), 409)


def test_material_returns_owned_row_and_ignores_growth_and_initial_class():
    subject = create(setup_case())
    saved = state()
    selected = saved["player_state"]["units"][-1]
    selected.update(ClassChangeCount=2, ClassID=10120, Exp=100000, Love=150, WatchLoveEv=3,
                    CostModMixValue=5, SkillLevel=5, SkillExperience=12, CostExperience=8)
    before = deepcopy(saved)
    assert select(subject, saved) is selected
    assert saved == before


@pytest.mark.parametrize("flags", [0, 1, 4, 8, -0x80000000, -0x80000000 | 13])
def test_first_barracks_does_not_require_all_flags_zero(flags):
    subject, saved = create(setup_case()), state()
    saved["player_state"]["units"][-1]["GeneralPurposeFlags"] = flags
    assert select(subject, saved)["GeneralPurposeFlags"] == flags


@pytest.mark.parametrize("flags", [16, 33, 0xF0, -0x80000000 | 16])
def test_actual_extra_barracks_is_rejected(flags):
    subject, saved = create(setup_case()), state()
    saved["player_state"]["units"][-1]["GeneralPurposeFlags"] = flags
    fails("silver_trade_storage_unsupported", lambda: select(subject, saved), 501)


@pytest.mark.parametrize("kind", ["locked", "squad", "other_squad", "prince1", "prince309"])
def test_material_protection_covers_locks_and_all_squads(kind):
    subject, saved = create(setup_case()), state()
    selected = saved["player_state"]["units"][-1]
    if kind == "locked":
        selected["GeneralPurposeFlags"] = 3
    elif "squad" in kind:
        saved["player_state"]["squads"] = [{"UnitIndex": 5, "Address": 0 if kind == "squad" else 0x07010000}]
    else:
        selected["CardID"] = 1 if kind == "prince1" else 309
    fails("silver_trade_material_protected", lambda: select(subject, saved), 409)


def test_rarity_is_from_frozen_original_card_not_untrusted_unit_field():
    subject, saved = create(setup_case()), state()
    saved["player_state"]["units"][-1].update(CardID=9, Rare=2)
    fails("silver_trade_material_ineligible", lambda: select(subject, saved), 409)
    saved["player_state"]["units"][-1].update(CardID=7, Rare=5)
    assert select(subject, saved)["CardID"] == 7


@pytest.mark.parametrize("index,unique", [(4, 9), (5, 10), (0, 9)])
def test_identity_pair_must_match_one_owned_instance(index, unique):
    subject = create(setup_case())
    fails("silver_trade_material_missing", lambda: select(subject, index=index, unique=unique), 409)


@pytest.mark.parametrize("index,unique", [(True, 9), (-1, 9), (0x80000000, 9), (5, False), (5, 0), (5, 0x80000000)])
def test_identity_arguments_are_strict_native_integers(index, unique):
    subject = create(setup_case())
    fails("silver_trade_identity_invalid", lambda: select(subject, index=index, unique=unique))


def test_same_card_multiple_instances_are_not_duplicate_materials():
    subject, saved = create(setup_case()), state()
    saved["player_state"]["units"].append(unit(index=6, unique=10))
    assert select(subject, saved, index=6, unique=10) is saved["player_state"]["units"][-1]


@pytest.mark.parametrize("overrides", [{"unit": 1}, [1]])
def test_dynamic_overrides_are_explicitly_unsupported(overrides):
    subject, saved = create(setup_case()), state()
    saved["unit_dynamic_overrides"] = overrides
    fails("silver_trade_overrides_unsupported", lambda: select(subject, saved), 501)


@pytest.mark.parametrize("overrides", [None, {}, []])
def test_absent_or_empty_override_collections_are_allowed(overrides):
    subject, saved = create(setup_case()), state()
    saved["unit_dynamic_overrides"] = overrides
    assert select(subject, saved)["CardID"] == 7


@pytest.mark.parametrize("text", ["", " \t\n"])
def test_empty_rating_commands_default_to_allowed(text):
    subject = create(setup_case(commands=[{"_ID": name_hash("IsCanUseRating_7"), "_Cmnd": encode_command(text)}]))
    assert select(subject)["CardID"] == 7


@pytest.mark.parametrize("rows", [
    [{"_ID": name_hash("IsCanUseRating_7"), "_Cmnd": encode_command("1;")}],
    [{"_ID": name_hash("IsCanUseRating_7"), "_Cmnd": encode_command("0;")}],
    [{"_ID": name_hash("IsCanUseRating_7"), "_Cmnd": ""}] * 2,
])
def test_nonempty_or_ambiguous_rating_never_executes_or_defaults_allow(rows):
    subject = create(setup_case(commands=rows))
    assert subject.status()["rating_conditions_unavailable"] == [7]
    fails("silver_trade_rating_unsupported", lambda: select(subject), 501)
    saved = state()
    saved["player_state"]["units"][-1]["CardID"] = 8
    assert select(subject, saved)["CardID"] == 8


@pytest.mark.parametrize("kind", ["schema", "units", "duplicate_index", "duplicate_uid", "unknown_card",
                                    "bool_flag", "bad_class", "squads", "missing_member", "overrides"])
def test_invalid_material_state_fails_without_mutation(kind):
    subject, saved = create(setup_case()), state()
    player = saved["player_state"]
    if kind == "schema":
        player["schema_version"] = True
    elif kind == "units":
        player["units"] = []
    elif kind == "duplicate_index":
        player["units"].append(unit(unique=10))
    elif kind == "duplicate_uid":
        player["units"].append(unit(index=6))
    elif kind == "unknown_card":
        player["units"][-1]["CardID"] = 999
    elif kind == "bool_flag":
        player["units"][-1]["GeneralPurposeFlags"] = False
    elif kind == "bad_class":
        player["units"][-1]["ClassID"] = 0
    elif kind == "squads":
        player["squads"] = [{"UnitIndex": True}]
    elif kind == "missing_member":
        player["squads"] = [{"UnitIndex": 999}]
    else:
        saved["unit_dynamic_overrides"] = "invalid"
    before = deepcopy(saved)
    fails("silver_trade_state_invalid", lambda: select(subject, saved), 503)
    assert saved == before


def test_mismatched_state_or_forged_product_is_not_accepted():
    subject, saved = create(setup_case()), state()
    forged = replace(subject.product(1), type_catalog=42)
    fails("silver_trade_product_invalid", lambda: subject.material(saved["player_state"], forged, 5, 9, state=saved))
    fails("silver_trade_state_invalid", lambda: subject.material(saved["player_state"], subject.product(1), 5, 9, state={}))


def test_other_operations_and_unknown_products_are_not_reinterpreted():
    subject = create(setup_case())
    fails("silver_trade_product_unsupported", lambda: subject.product(3), 501)
    fails("trade_product_unknown", lambda: subject.product(999))
    case = setup_case(products=[product(Type=3, Type_Catalog=27, AddingCount=2)])
    fails("silver_trade_product_unsupported", lambda: create(case).product(1), 501)


@pytest.mark.parametrize("fault", ["catalog_schema", "catalog_truncated", "group_absent", "class_zero",
                                    "negative_group", "command_schema", "bad_command", "missing_payment",
                                    "payment_zero", "unknown_reward", "bool_rare", "missing_rare", "duplicate_card"])
def test_constructor_rejects_invalid_or_inconsistent_resources(fault):
    case = setup_case()
    if fault == "catalog_schema":
        case.payloads["ShopCatalogCofig.atb"] = table([], {"ID_Config": 1})
    elif fault == "catalog_truncated":
        case.payloads["ShopCatalogCofig.atb"] = b"ALTB"
    elif fault == "group_absent":
        case.payloads["ShopCatalogCofig.atb"] = table([], CLASS_SCHEMA)
    elif fault == "class_zero":
        case.payloads["ShopCatalogCofig.atb"] = table([{"ID_Config": 27, "ID_Class": 0}], CLASS_SCHEMA)
    elif fault == "negative_group":
        case.payloads["ShopCatalogCofig.atb"] = table([{"ID_Config": -1, "ID_Class": 100}], CLASS_SCHEMA)
    elif fault == "command_schema":
        case.payloads["OnCommand.atb"] = table([], {"_ID": 1})
    elif fault == "bad_command":
        case.payloads["OnCommand.atb"] = table([{"_ID": name_hash("IsCanUseRating_7"), "_Cmnd": "zz"}], COMMAND_SCHEMA)
    elif fault == "missing_payment":
        case.possessions._names_by_hash.pop(PAYMENT_HASH)
    elif fault == "payment_zero":
        case.possessions._definitions[PAYMENT_ITEM] = 0
    elif fault == "unknown_reward":
        case.cards.card_ids = tuple(value for value in case.cards.card_ids if value != 7)
    elif fault == "bool_rare":
        case.cards.rows[7]["Rare"] = True
    elif fault == "missing_rare":
        del case.cards.rows[7]["Rare"]
    else:
        case.cards.card_ids += (7,)
    fails("silver_trade_catalog_invalid", lambda: create(case), 503)


@pytest.mark.parametrize("fault", ["base_hash", "card_bytes", "rare", "resource_version", "base_product", "payment_hash"])
def test_changed_dependencies_invalidate_product_material_and_status(fault):
    case = setup_case()
    subject = create(case)
    selected = subject.product(1)
    if fault == "base_hash":
        case.base.content_version = "b" * 64
    elif fault == "card_bytes":
        case.cards.snapshot.get_xml = lambda _: b"changed-card-master"
    elif fault == "rare":
        case.cards.rows[7]["Rare"] = 5
    elif fault == "resource_version":
        case.cards.resource_version = "changed"
    elif fault == "base_product":
        case.base._products = MappingProxyType({**case.base._products, 1: replace(case.base.product(1), adding_count=2)})
    else:
        case.possessions._names_by_hash[PAYMENT_HASH] = "coin"
    fails("silver_trade_content_changed", lambda: subject.product(1), 503)
    saved = state()
    fails("silver_trade_content_changed", lambda: subject.material(saved["player_state"], selected, 5, 9, state=saved), 503)
    assert subject.status()["ready"] is False


@pytest.mark.parametrize("fault", [None, "base_hash", "base_unready", "card_bytes"])
def test_readiness_matches_status_without_building_it(monkeypatch, fault):
    case = setup_case()
    subject = create(case)
    if fault == "base_hash":
        case.base.content_version = "b" * 64
    elif fault == "base_unready":
        case.possessions.issues["item_definitions"] = "Item definitions changed after setup"
    elif fault == "card_bytes":
        case.cards.snapshot.get_xml = lambda _: b"changed-card-master"
    ready = subject.status()["ready"]
    assert ready is (fault is None)
    for provider in (subject, case.base, case.possessions):
        monkeypatch.setattr(provider, "status", lambda: pytest.fail("Readiness built a full status"))
    assert subject.is_ready() is ready


def test_card_master_is_digested_once_per_bytes_object(digested):
    case = setup_case()
    subject = create(case)
    verified = case.cards.snapshot.get_xml("CardData")
    sources = {"equal": bytes(bytearray(verified)), "changed": verified + b"changed", "verified": verified}

    def names():
        return [name for data in digested for name, raw in sources.items() if data is raw]
    for _ in range(2):
        assert subject.is_ready() is subject.status()["ready"] is True
    assert names() == ["verified"]
    case.cards.snapshot.get_xml = lambda _: sources["equal"]
    for _ in range(2):
        assert subject.product(1).shop_id == 1
    assert names() == ["verified", "equal"]
    case.cards.snapshot.get_xml = lambda _: sources["changed"]
    for _ in range(2):
        fails("silver_trade_content_changed", lambda: subject.product(1), 503)
    assert names() == ["verified", "equal", "changed"]
    case.cards.snapshot.get_xml = lambda _: verified
    assert subject.status()["ready"] is True and names() == ["verified", "equal", "changed", "verified"]


def test_card_master_subclass_is_checked_on_every_readiness_check():
    case = setup_case()
    subject = create(case)
    verified = case.cards.snapshot.get_xml("CardData")

    class Master(bytes):
        def __buffer__(self, flags):
            return memoryview(self.shown)
    master = Master(verified)
    master.shown = verified
    case.cards.snapshot.get_xml = lambda _: master
    assert subject.status()["ready"] is True
    master.shown = verified + b"changed"
    assert subject.status()["ready"] is False
    fails("silver_trade_content_changed", lambda: subject.product(1), 503)


def test_frozen_asset_payloads_and_content_version_are_independent():
    case = setup_case()
    subject = create(case)
    version = subject.content_version
    case.payloads["ShopCatalogCofig.atb"] = table([
        {"ID_Config": 27, "ID_Class": 10100}, {"ID_Config": 42, "ID_Class": 10200}], CLASS_SCHEMA)
    assert select(subject)["ClassID"] == 10000 and subject.content_version == version
    assert create(case).content_version != version


def test_quote_reuses_shared_prices_without_mutating_input():
    case = setup_case(products=[product(Type=3, Type_Catalog=27, RequiredValue=20),
                                product(0, Type=0, TradeCount=3, RequiredValue=30)])
    subject, saved = create(case), state()
    before = deepcopy(saved)
    result = subject.quote(saved, 1, 1, 1, NOW)
    assert (result.unit_price, result.total_price, result.amount, result.registration) == (30, 30, 1, 106)
    assert result.currency_item_id == PAYMENT_ITEM and result.ownership_discount == 0
    assert result.product.trade_type == 3 and saved == before
    fails("trade_product_unsupported", lambda: case.base.quote(saved, 1, 1, 1, NOW, 106), 501)


@pytest.mark.parametrize("quantity", [0, 2, -1, True, 1.0, 0x80000000])
def test_quote_only_accepts_one_native_integer_purchase(quantity):
    subject = create(setup_case())
    fails("silver_trade_quantity_invalid", lambda: subject.quote(state(), 1, quantity, 0, NOW))


def test_quote_shared_balance_time_disclosure_and_registration_rules():
    start = NOW + timedelta(days=1)
    case = setup_case(products=[product(Type=3, Type_Catalog=27, RequiredValue=20,
        Data_Restrict=2, StartDateTime=native_date(start), EndDateTime=native_date(start + timedelta(days=1)),
        DisclosureQuestIdList="1001")])
    subject, saved = create(case), state()
    fails("trade_registration_mismatch", lambda: subject.quote(saved, 1, 1, 0, start, 104))
    fails("trade_product_closed", lambda: subject.quote(saved, 1, 1, 0, NOW))
    fails("trade_quest_required", lambda: subject.quote(saved, 1, 1, 0, start))
    saved["tables"]["quest_records"] = [{"QuestID": 1001, "AchieveFlag": 0x01000000}]
    assert subject.quote(saved, 1, 1, 0, start).total_price == 20
    fails("trade_limit_reached", lambda: subject.quote(saved, 1, 1, 2, start))
    saved["tables"]["possessions"][0]["Count"] = 19
    fails("trade_currency_insufficient", lambda: subject.quote(saved, 1, 1, 0, start))


@pytest.fixture(scope="module")
def native():
    project = Path(__file__).resolve().parents[1]
    if not (project / "runtime/server/masterdata/active.json").is_file():
        pytest.skip("Verified local resources and public masters are required")
    snapshot = MasterDataStore(project / "runtime/server/masterdata").snapshot()
    manifest, entries = ResourceLibrary(project / "runtime/resources", client_hash=snapshot.client_hash).load()
    assets = IndexedAssetService(entries, project / "runtime/resources", project.parent / "aigisr_exe")
    reader = lambda name: assets.read(name)[0]
    possessions = PossessionService(project.parent / "aigisr_exe", asset_reader=reader)
    cards = GiftCatalog(snapshot, reader, possessions, resource_version=manifest["version"])
    base = TradeCatalog(cards, possessions, reader, resource_version=manifest["version"])
    return SimpleNamespace(subject=SilverTradeCatalog(base, cards, reader), cards=cards, base=base)


def test_all_24_current_products_and_actual_silver_material_families(native):
    subject = native.subject
    assert len(subject.products) == 24 and subject.status()["ready"]
    assert subject.status()["rating_conditions_unavailable"] == []
    assert len(native.base.products) == 1820 and native.base.status()["ordinary_products"] == 1791
    for product_row in subject.products:
        assert (product_row.registration, product_row.adding_count, product_row.tiers) == (106, 1, ((1, 20),))
        candidates = [native.cards.card(identifier) for identifier in native.cards.card_ids
                      if native.cards.card(identifier)["Rare"] == 2
                      and native.cards.card(identifier)["InitClassID"] // 100 in
                      {value // 100 for value in subject._groups[product_row.type_catalog]}]
        assert candidates, product_row.shop_id
        saved = state()
        saved["player_state"]["units"][-1] = unit(candidates[0]["CardID"], class_id=candidates[0]["InitClassID"])
        assert select(subject, saved, shop_id=product_row.shop_id) is saved["player_state"]["units"][-1]


def test_quote_all_24_current_products(native):
    for product_row in native.subject.products:
        result = native.subject.quote(state(), product_row.shop_id, 1, 0, NOW)
        assert (result.total_price, result.amount, result.currency_item_id) == (20, 1, PAYMENT_ITEM)
