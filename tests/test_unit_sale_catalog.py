from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.errors import ProtocolError
from aigis_ps.gift_catalog import GiftCatalog
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.player_state import MAX_PLAYER_UNITS
from aigis_ps.possessions import PossessionService
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.unit_sale_catalog import AUTO_COMMAND, AUTO_RESOURCE, RATING_RESOURCE, SaleQuote, UnitSaleCatalog
from test_image_aliases import command_id, command_table, encode


class Cards:
    def __init__(self):
        self.rows = {77: {"CardID": 77, "SellPrice": 6000, "_TradePoint": 0},
                     533: {"CardID": 533, "SellPrice": 30000, "_TradePoint": 2},
                     902: {"CardID": 902, "SellPrice": 100000, "_TradePoint": 3},
                     3: {"CardID": 3, "SellPrice": 0, "_TradePoint": 0}}
        self.card_ids = tuple(self.rows)
        self.master = b"complete-card-master"
        self.snapshot = SimpleNamespace(identifier="fixed-master", client_hash="a" * 64, get_xml=lambda _: self.master)
        self.resource_version = "fixed-resources"

    def card(self, identifier):
        return self.rows[identifier]


class Possessions:
    def __init__(self):
        self.asset_sha256 = "b" * 64
        self.name, self.stack_limit = "trade_item_003000", 999999

    def resolve_hash(self, identifier):
        assert identifier == 1939853913
        return self.name

    def stack(self, name):
        assert name == "trade_item_003000"
        return self.stack_limit


def resources(auto='CMenuTaskSendMessage(0,"AutoSailExcute" , 77 , 533 , 902 );', ratings=()):
    return {AUTO_RESOURCE: command_table([(command_id(AUTO_COMMAND), encode(auto))]),
            RATING_RESOURCE: command_table(ratings)}


def catalog(*, cards=None, possessions=None, payloads=None):
    return UnitSaleCatalog(cards or Cards(), possessions or Possessions(),
                           (resources() if payloads is None else payloads).__getitem__)


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
    return UnitSaleCatalog(cards, possessions, reader), cards


def test_actual_full_catalog_and_automatic_prices(native_catalog):
    subject, cards = native_catalog
    assert subject.status()["cards"] == len(cards.card_ids) == 2932
    assert subject.auto_card_ids == (77, 533, 902)
    assert subject.rainbow_item == "trade_item_003000"
    assert subject.quote([{"CardID": identifier} for identifier in subject.auto_card_ids]) == SaleQuote(136000, 0)
    assert set(subject.status()["resource_sha256"]) == {"OnCommandMenuUnitRetire.atb", "OnCommand.atb"}


def test_same_card_instances_and_irrelevant_level_metadata_do_not_change_formula():
    subject = catalog()
    units = [{"CardID": 533, "GetIndex": 1, "UniqueID": 1, "Exp": 0},
             {"CardID": 533, "GetIndex": 2, "UniqueID": 2, "Exp": 99999999, "Love": 150}]
    before = deepcopy(units)
    assert subject.quote(units) == SaleQuote(60000, 4)
    assert units == before
    assert subject.quote(()) == SaleQuote(0, 0)
    assert subject.quote([{"CardID": 3}]) == SaleQuote(0, 0)


def test_prices_auto_list_and_status_are_independent_immutable_snapshots():
    cards = Cards()
    subject = catalog(cards=cards)
    cards.rows[77]["SellPrice"] = 1
    assert subject.quote([{"CardID": 77}]) == SaleQuote(6000, 0)
    with pytest.raises(FrozenInstanceError):
        subject.quote([]).gold = 1
    with pytest.raises(AttributeError):
        subject.auto_card_ids = (3,)
    with pytest.raises(TypeError):
        subject._prices[77] = SaleQuote(1, 1)
    report = subject.status()
    report["auto_card_ids"].clear()
    report["resource_sha256"].clear()
    assert subject.auto_card_ids == (77, 533, 902) and len(subject.status()["resource_sha256"]) == 2


def test_auto_literal_accepts_whitespace_and_future_known_card_list():
    subject = catalog(payloads=resources(' \r\nCMenuTaskSendMessage ( 0 , "AutoSailExcute", 3,77 ) ;\t'))
    assert subject.auto_card_ids == (3, 77)


@pytest.mark.parametrize("text", [
    '', 'CMenuTaskSendMessage(0,"AutoSailExcute");',
    'CMenuTaskSendMessage(1,"AutoSailExcute",77);', 'CMenuTaskSendMessage(0,"Other",77);',
    'CMenuTaskSendMessage(0,"AutoSailExcute",77);Other();',
    'if(1){CMenuTaskSendMessage(0,"AutoSailExcute",77);}',
    'CMenuTaskSendMessage(0,"AutoSailExcute",77+0);',
    'CMenuTaskSendMessage(0,"AutoSailExcute",77,77);',
    'CMenuTaskSendMessage(0,"AutoSailExcute",999);',
    'CMenuTaskSendMessage(0,"AutoSailExcute",0);',
    'CMenuTaskSendMessage(0,"AutoSailExcute",-77);',
    'CMenuTaskSendMessage(0,"AutoSailExcute",2147483648);',
])
def test_ambiguous_dynamic_or_invalid_auto_commands_are_not_defaulted(text):
    with pytest.raises(ProtocolError) as error:
        catalog(payloads=resources(text))
    assert error.value.code == "unit_sale_catalog_invalid" and error.value.status == 503


@pytest.mark.parametrize("rows", [[], [(1, encode('CMenuTaskSendMessage(0,"AutoSailExcute",77);'))],
    [(command_id(AUTO_COMMAND), "zz")], [(command_id(AUTO_COMMAND), "0")],
    [(command_id(AUTO_COMMAND), encode('CMenuTaskSendMessage(0,"AutoSailExcute",77);'))] * 2])
def test_auto_command_lookup_requires_one_valid_hash_entry(rows):
    payloads = resources()
    payloads[AUTO_RESOURCE] = command_table(rows)
    with pytest.raises(ProtocolError):
        catalog(payloads=payloads)


def test_rating_missing_or_empty_allows_but_every_nonempty_expression_is_unsupported():
    payloads = resources(ratings=[(command_id("IsCanUseRating_77"), encode(" \t\r\n")),
                                  (command_id("IsCanUseRating_533"), encode("1;")),
                                  (command_id("IsCanUseRating_902"), encode("0;"))])
    subject = catalog(payloads=payloads)
    subject.check_rating(3)
    subject.check_rating(77)
    for identifier in (533, 902):
        with pytest.raises(ProtocolError) as error:
            subject.check_rating(identifier)
        assert error.value.code == "unit_sale_rating_unsupported" and error.value.status == 501


def test_duplicate_rating_command_is_not_silently_selected():
    subject = catalog(payloads=resources(ratings=[(command_id("IsCanUseRating_77"), "")] * 2))
    with pytest.raises(ProtocolError, match="rating condition"):
        subject.check_rating(77)


@pytest.mark.parametrize("field", ["SellPrice", "_TradePoint"])
@pytest.mark.parametrize("value", [True, -1, 0x80000000, 1.0, "1", None])
def test_invalid_price_anywhere_in_master_invalidates_catalog(field, value):
    cards = Cards()
    cards.rows[3][field] = value
    with pytest.raises(ProtocolError) as error:
        catalog(cards=cards)
    assert error.value.code == "unit_sale_catalog_invalid"


@pytest.mark.parametrize("fault", ["empty", "duplicate", "identity", "missing_price", "boolean_id"])
def test_invalid_master_identity_or_missing_price_fails(fault):
    cards = Cards()
    if fault == "empty":
        cards.card_ids = ()
    elif fault == "duplicate":
        cards.card_ids += (77,)
    elif fault == "identity":
        cards.rows[77]["CardID"] = 533
    elif fault == "missing_price":
        del cards.rows[3]["SellPrice"]
    else:
        cards.card_ids = (True,)
    with pytest.raises(ProtocolError):
        catalog(cards=cards)


@pytest.mark.parametrize("field,value", [("name", "wrong"), ("stack_limit", 0), ("stack_limit", -1),
                                           ("stack_limit", True), ("asset_sha256", None), ("asset_sha256", "bad")])
def test_rainbow_identity_positive_stack_and_resource_hash_are_required(field, value):
    possessions = Possessions()
    setattr(possessions, field, value)
    with pytest.raises(ProtocolError) as error:
        catalog(possessions=possessions)
    assert error.value.code == "unit_sale_catalog_invalid" and type(error.value.__cause__) is ValueError


@pytest.mark.parametrize("field", ["SellPrice", "_TradePoint"])
def test_total_proceeds_overflow_is_rejected_without_mutating_inputs(field):
    cards = Cards()
    cards.rows[3][field] = 0x7fffffff
    subject = catalog(cards=cards)
    single = subject.quote([{"CardID": 3}])
    assert (single.gold if field == "SellPrice" else single.rainbow) == 0x7fffffff
    rows = [{"CardID": 3, "GetIndex": 1}, {"CardID": 3, "GetIndex": 2}]
    before = deepcopy(rows)
    with pytest.raises(ProtocolError) as error:
        subject.quote(rows)
    assert error.value.code == "unit_sale_amount_overflow" and error.value.status == 409
    assert rows == before


@pytest.mark.parametrize("units", [None, "77", b"77", {"CardID": 77}, iter([{"CardID": 77}]),
                                   [77], [{}], [{"CardID": True}], [{"CardID": 0}], [{"CardID": 999}]])
def test_quote_requires_known_unit_instance_sequence(units):
    with pytest.raises(ProtocolError):
        catalog().quote(units)


def test_quote_has_bounded_instance_count():
    subject = catalog()
    assert subject.quote([{"CardID": 3}] * MAX_PLAYER_UNITS) == SaleQuote(0, 0)
    with pytest.raises(ProtocolError):
        subject.quote([{"CardID": 3}] * (MAX_PLAYER_UNITS + 1))


@pytest.mark.parametrize("fault", ["master_id", "client", "version", "master_content", "price", "auto", "rating", "item"])
def test_content_version_covers_each_authoritative_dependency(fault):
    baseline = catalog().content_version
    cards, possessions, payloads = Cards(), Possessions(), resources()
    if fault == "master_id":
        cards.snapshot.identifier = "changed-master"
    elif fault == "client":
        cards.snapshot.client_hash = "c" * 64
    elif fault == "version":
        cards.resource_version = "new-resources"
    elif fault == "master_content":
        cards.master += b"other-field-changed"
    elif fault == "price":
        cards.rows[3]["SellPrice"] = 1
    elif fault == "auto":
        payloads = resources('CMenuTaskSendMessage(0,"AutoSailExcute",77);')
    elif fault == "rating":
        payloads = resources(ratings=[(123, encode("Unrelated();"))])
    else:
        possessions.asset_sha256 = "d" * 64
    assert catalog(cards=cards, possessions=possessions, payloads=payloads).content_version != baseline
    assert catalog().content_version == baseline


@pytest.mark.parametrize("resource", [AUTO_RESOURCE, RATING_RESOURCE])
@pytest.mark.parametrize("payload", [b"bad", "not bytes"])
def test_broken_resource_fails_catalog_creation(resource, payload):
    payloads = resources()
    payloads[resource] = payload
    with pytest.raises(ProtocolError):
        catalog(payloads=payloads)


def test_command_schema_is_strict():
    payloads = resources()
    payloads[RATING_RESOURCE] = command_table([], command_field="Other")
    with pytest.raises(ProtocolError):
        catalog(payloads=payloads)
