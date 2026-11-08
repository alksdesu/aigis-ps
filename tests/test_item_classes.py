from copy import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import zlib

import pytest

from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.emblem_catalog import CATEGORY as EMBLEM_CATEGORY, EmblemCatalog
from aigis_ps.errors import ProtocolError
from aigis_ps.event_pass_catalog import ITEM_RESOURCE_NAME, EventPassCatalog
from aigis_ps.item_classes import FALLBACK_CLASSES, ITEM_PRESENT_TYPE, ItemClass, ItemClasses
from aigis_ps.item_summon_catalog import CATEGORY as TICKET_CATEGORY, ItemSummonCatalog
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.mission_pass_points import MissionPassPoints
from aigis_ps.possessions import PossessionService
from aigis_ps.present_items import OWNERSHIP_EFFECTS, PresentItemCatalog, PresentItemDefinition
from aigis_ps.present_recovery import PresentRecoveryCatalog
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.vote_catalog import VoteCatalog
from aigis_ps.vote_tickets import DAILY_ITEM, VoteTicketService
from test_present_items import SCHEMA as ITEM_SCHEMA, item_table


SCHEMA = (*ITEM_SCHEMA, ("EmblemID", 1))
VOTE_CATEGORY = 1925062086
EMBLEM_ID = 3
PASSIVE = "gift_0002_ruby"
PASSIVE_ORB = "evolve_orb_test"
ZERO_STOCK = "zero_stock_test"
EVENT_STOCK = "event_stock_test"
BLESSING = "god_bless_test"
ACTION_BLESSING = "god_bless_action_test"
SKIN = "skin_test"
TITLE = "get_emblem_test"
TICKET = "item_smn_ticket_test"
COOKING = "home_cooking_test"
PASS = "item_eventpass_test"
PREMIUM = "item_eventpass_premium_test"
VOTE_GROUP = "item_vote_016001"
QUEST_KEY = "quest_key_test"
MYSTERY = "mystery_zero_test"
EXPECTED = {
    PASSIVE: ItemClass.PASSIVE, PASSIVE_ORB: ItemClass.PASSIVE, ZERO_STOCK: ItemClass.STACK_ZERO,
    EVENT_STOCK: ItemClass.UNKNOWN, BLESSING: ItemClass.OWNERSHIP, ACTION_BLESSING: ItemClass.UNKNOWN,
    SKIN: ItemClass.SKIN, TITLE: ItemClass.EMBLEM, TICKET: ItemClass.ITEM_TICKET, COOKING: ItemClass.RECOVERY,
    PASS: ItemClass.EVENT_PASS, PREMIUM: ItemClass.EVENT_PASS, DAILY_ITEM: ItemClass.VOTE_TICKET,
    VOTE_GROUP: ItemClass.VOTE_TICKET, QUEST_KEY: ItemClass.UNKNOWN, MYSTERY: ItemClass.STACK_ZERO,
}
EXPECTED_COUNTS = {item_class.value: sum(value is item_class for value in EXPECTED.values()) for item_class in ItemClass}
SOURCE_NAMES = ("recovery", "item_tickets", "event_pass", "vote_tickets")
CURRENT_COUNTS = {"passive": 116, "skin": 38, "ownership": 242, "recovery": 172, "item_ticket": 639, "emblem": 83,
                  "event_pass": 12, "vote_ticket": 5, "stack_zero": 1, "unknown": 62}
CURRENT_FALLBACK_EFFECT_TYPES = {"stack_zero": {8: 1}, "unknown": {0: 29, 3: 1, 10: 32}}


def version(seed):
    return hashlib.sha256(seed.encode()).hexdigest()


def item(name, **changes):
    result = {"ItemID": name, "ID": (zlib.crc32(name.encode()) ^ 0x80000000) - 0x80000000, "Stack": 999,
              "EffectType": 4, "ReceiveAction": 0, "EventID": "", "ReceiveConfirmation": 0, "CanPresent": 1,
              "Category": 0, "CanAllGet": 1, "NoAutoDelete": 0, "InvisibleContaint": 0, "EmblemID": 0}
    result.update(changes)
    return result


def ownership(name, effect, **changes):
    return item(name, EffectType=effect, Category=OWNERSHIP_EFFECTS[effect], Stack=1, CanAllGet=0, NoAutoDelete=1,
                **changes)


def item_rows():
    return [
        item(PASSIVE), item(PASSIVE_ORB, EffectType=6, Stack=9999), item(ZERO_STOCK, EffectType=5, Stack=0),
        item(EVENT_STOCK, EffectType=5, Stack=99, EventID="event"), ownership(BLESSING, 12),
        ownership(ACTION_BLESSING, 12, ReceiveAction=1), ownership(SKIN, 13),
        item(TITLE, EffectType=10, Category=EMBLEM_CATEGORY, Stack=1, CanAllGet=0, EmblemID=EMBLEM_ID),
        item(TICKET, EffectType=2, Category=TICKET_CATEGORY, Stack=0, CanAllGet=0, NoAutoDelete=1, InvisibleContaint=1),
        item(COOKING, EffectType=11, Stack=0, CanAllGet=0, InvisibleContaint=1),
        item(PASS, EffectType=0, CanPresent=0, CanAllGet=0), item(PREMIUM, EffectType=14, Stack=1, CanAllGet=0),
        item(DAILY_ITEM, EffectType=0, Category=VOTE_CATEGORY, Stack=1),
        item(VOTE_GROUP, EffectType=10, Category=VOTE_CATEGORY),
        item(QUEST_KEY, EffectType=0, Stack=5), item(MYSTERY, EffectType=8, Stack=0),
    ]


def build(case, **changes):
    sources = {"recovery_catalog": case.recovery, "item_tickets": case.item_tickets, "pass_points": case.pass_points,
               "vote_tickets": case.vote_tickets}
    sources.update(changes)
    return ItemClasses(case.items, **sources)


@pytest.fixture
def case():
    payload = item_table(item_rows(), SCHEMA)
    resource = hashlib.sha256(payload).hexdigest()
    possessions = PossessionService(Path("."), asset_reader=lambda name: payload)
    emblems = SimpleNamespace(content_version=version("emblems"), item_ids=frozenset({TITLE}),
                              item_resource_sha256=resource, items={TITLE: SimpleNamespace(emblem_id=EMBLEM_ID)},
                              status=lambda: {"ready": True}, is_ready=lambda: True)
    case = SimpleNamespace(resource=resource, emblems=emblems,
        items=PresentItemCatalog(possessions, payload, emblems=emblems),
        recovery=SimpleNamespace(content_version=version("recovery"), supports_item=lambda name: name == COOKING,
                                 status=lambda: {"ready": True, "resource_hashes": {ITEM_RESOURCE_NAME: resource}}),
        item_tickets=SimpleNamespace(catalog=SimpleNamespace(content_version=version("tickets"),
            item_ids=frozenset({TICKET}), item_resource_sha256=resource, status=lambda: {"ready": True})),
        pass_points=SimpleNamespace(content_version=version("passes"), status=lambda: {"ready": True},
            catalog=SimpleNamespace(passes=(SimpleNamespace(item_id=PASS, premium_item_id=PREMIUM),),
                                    resource_hashes={ITEM_RESOURCE_NAME: resource})),
        vote_tickets=SimpleNamespace(content_version=version("votes"),
            status=lambda: {"ready": True, "resource_sha256": resource},
            catalog=SimpleNamespace(groups=(SimpleNamespace(item_id=VOTE_GROUP),))))
    case.classes = build(case)
    return case


def invalid(callback):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert (caught.value.code, caught.value.status) == ("item_classes_invalid", 503)
    return caught.value.__cause__


def changed(callback):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert (caught.value.code, caught.value.status) == ("item_classes_changed", 503)


def test_every_item_belongs_to_exactly_one_class(case):
    assert {name: case.classes.classify(name) for name in EXPECTED} == EXPECTED
    status = case.classes.status()
    assert status["ready"] is True and status["error"] is None
    assert status["item_count"] == len(EXPECTED) == sum(status["counts"].values())
    assert list(status["counts"]) == [item_class.value for item_class in ItemClass]
    assert status["counts"] == EXPECTED_COUNTS
    assert status["sources"] == {**dict.fromkeys(SOURCE_NAMES, True), "emblems": True}
    assert status["item_resource_sha256"] == case.resource
    assert status["passive_effects"] == [4, 5, 6, 7, 9] and status["ownership_effects"] == [12, 13]


def test_item_ids_list_each_class_by_member_or_name(case):
    for item_class in ItemClass:
        expected = frozenset(name for name, value in EXPECTED.items() if value is item_class)
        assert case.classes.item_ids(item_class) == case.classes.item_ids(item_class.value) == expected
    for value in ("Passive", "", None, 10, ["passive"]):
        with pytest.raises(ProtocolError) as caught:
            case.classes.item_ids(value)
        assert (caught.value.code, caught.value.status) == ("item_class_invalid", 400)


def test_only_verified_positive_stock_is_passive(case):
    rejected = (SKIN, COOKING, TICKET, TITLE, PASS, PREMIUM, DAILY_ITEM, VOTE_GROUP, BLESSING, ZERO_STOCK,
                EVENT_STOCK, ACTION_BLESSING, QUEST_KEY, MYSTERY)
    assert all(case.classes.classify(name) is not ItemClass.PASSIVE for name in rejected)
    assert case.classes.item_ids(ItemClass.PASSIVE) == {PASSIVE, PASSIVE_ORB}
    for name in case.classes.item_ids(ItemClass.PASSIVE):
        assert case.items.definition(ITEM_PRESENT_TYPE, name).single_only is False


def test_zero_stack_stays_out_of_passive_even_when_mail_accepts_it(case):
    verified = case.items.definition
    case.items.definition = lambda kind, name: (PresentItemDefinition(name, 0) if name == ZERO_STOCK
                                                else verified(kind, name))
    assert build(case).classify(ZERO_STOCK) is ItemClass.STACK_ZERO


@pytest.mark.parametrize(("rows", "reason"), [
    (lambda rows: tuple(rows.values()), "verified item definitions"),
    (lambda rows: {**rows, PASSIVE: {**rows[PASSIVE], "EffectType": "4"}}, "integer effect types"),
    (lambda rows: {**rows, PASSIVE: {**rows[PASSIVE], "Stack": True}}, "integer effect types"),
])
def test_item_definitions_keep_their_verified_shape(case, rows, reason):
    case.items._rows = rows(case.items.rows)
    assert reason in str(invalid(lambda: build(case)))


def test_classes_compare_as_their_stable_names(case):
    assert case.classes.classify(PASSIVE) == "passive"
    assert case.classes.classify(VOTE_GROUP) == "vote_ticket"
    assert [item_class.value for item_class in FALLBACK_CLASSES] == ["stack_zero", "unknown"]


@pytest.mark.parametrize("item_id", ["item_absent_test", "", None, 1, b"gift_0002_ruby"])
def test_unknown_item_identities_are_rejected(case, item_id):
    with pytest.raises(ProtocolError) as caught:
        case.classes.classify(item_id)
    assert (caught.value.code, caught.value.status) == ("item_class_unknown_item", 400)


def test_missing_sources_fall_back_without_creating_passive_stock(case):
    classes = ItemClasses(case.items)
    fallback = {TICKET: ItemClass.STACK_ZERO, COOKING: ItemClass.STACK_ZERO, PASS: ItemClass.UNKNOWN,
                PREMIUM: ItemClass.UNKNOWN, DAILY_ITEM: ItemClass.UNKNOWN, VOTE_GROUP: ItemClass.UNKNOWN}
    assert {name: classes.classify(name) for name in EXPECTED} == {**EXPECTED, **fallback}
    assert classes.item_ids(ItemClass.PASSIVE) == case.classes.item_ids(ItemClass.PASSIVE)
    assert classes.status()["sources"] == {**dict.fromkeys(SOURCE_NAMES, False), "emblems": True}
    assert classes.content_version != case.classes.content_version


def test_fallback_effect_types_explain_unowned_items(case):
    assert case.classes.status()["fallback_effect_types"] == {"stack_zero": {5: 1, 8: 1}, "unknown": {0: 1, 5: 1, 12: 1}}


def test_content_version_is_stable_and_follows_every_source(case):
    assert build(case).content_version == case.classes.content_version
    case.vote_tickets.content_version = version("other votes")
    assert build(case).content_version != case.classes.content_version


@pytest.mark.parametrize("claim", [
    lambda case: setattr(case.recovery, "supports_item", lambda name: name in (COOKING, PASSIVE)),
    lambda case: setattr(case.item_tickets.catalog, "item_ids", frozenset({TICKET, SKIN})),
    lambda case: setattr(case.pass_points.catalog, "passes", (SimpleNamespace(item_id=PASS, premium_item_id=BLESSING),)),
    lambda case: setattr(case.vote_tickets.catalog, "groups", (SimpleNamespace(item_id=TITLE),)),
    lambda case: setattr(case.item_tickets.catalog, "item_ids", frozenset({TICKET, COOKING})),
])
def test_items_claimed_by_two_classes_are_invalid(case, claim):
    claim(case)
    assert "claimed by both" in str(invalid(lambda: build(case)))


def test_claims_outside_the_item_resource_are_invalid(case):
    case.item_tickets.catalog.item_ids = frozenset({TICKET, "item_smn_ticket_absent"})
    invalid(lambda: build(case))


@pytest.mark.parametrize("mismatch", [
    lambda case, other: setattr(case.recovery, "status",
                                lambda: {"ready": True, "resource_hashes": {ITEM_RESOURCE_NAME: other}}),
    lambda case, other: setattr(case.item_tickets.catalog, "item_resource_sha256", other),
    lambda case, other: setattr(case.pass_points.catalog, "resource_hashes", {ITEM_RESOURCE_NAME: other}),
    lambda case, other: setattr(case.vote_tickets, "status", lambda: {"ready": True, "resource_sha256": other}),
])
def test_sources_must_share_the_item_resource(case, mismatch):
    mismatch(case, version("other items"))
    assert "different item resource" in str(invalid(lambda: build(case)))


@pytest.mark.parametrize("source", ["recovery", "item_tickets", "pass_points", "vote_tickets", "emblems"])
def test_unavailable_sources_are_invalid(case, source):
    target = {"recovery": case.recovery, "item_tickets": case.item_tickets.catalog, "pass_points": case.pass_points,
              "vote_tickets": case.vote_tickets, "emblems": case.emblems}[source]
    target.status = lambda: {"ready": False, "resource_hashes": {}, "resource_sha256": case.resource}
    target.is_ready = lambda: False
    invalid(lambda: build(case))


@pytest.mark.parametrize("fingerprint", [None, "", "A" * 64, "a" * 63, 1])
def test_sources_require_stable_fingerprints(case, fingerprint):
    case.recovery.content_version = fingerprint
    assert "fingerprints" in str(invalid(lambda: build(case)))


def test_items_catalog_is_required():
    invalid(lambda: ItemClasses(None))


def single_only_definition(kind, name):
    return PresentItemDefinition(name, 999, single_only=True)


def changed_definition(kind, name):
    raise ProtocolError("present_item_dependencies_changed", "Present and possession definitions must match", 503)


@pytest.mark.parametrize("definition", [single_only_definition, changed_definition])
def test_mail_definitions_must_agree_with_their_effect_type(case, definition):
    case.items.definition = definition
    invalid(lambda: build(case))


@pytest.mark.parametrize("change", [
    lambda case: setattr(case.item_tickets, "catalog", copy(case.item_tickets.catalog)),
    lambda case: setattr(case.pass_points, "catalog", copy(case.pass_points.catalog)),
    lambda case: setattr(case.vote_tickets, "catalog", copy(case.vote_tickets.catalog)),
    lambda case: setattr(case.item_tickets.catalog, "content_version", version("new tickets")),
    lambda case: setattr(case.recovery, "content_version", version("new recovery")),
    lambda case: setattr(case.pass_points, "content_version", "unstable"),
    lambda case: setattr(case.items, "content_version", version("new items")),
])
def test_changed_sources_make_the_classes_unavailable(case, change):
    change(case)
    changed(lambda: case.classes.classify(PASSIVE))
    changed(lambda: case.classes.item_ids(ItemClass.PASSIVE))
    status = case.classes.status()
    assert (status["ready"], status["error"]) == (False, "item_classes_changed")
    assert status["counts"] == EXPECTED_COUNTS


@pytest.fixture(scope="module")
def current():
    project = Path(__file__).resolve().parents[1]
    resource_root, masters = project / "runtime/resources", project / "runtime/server/masterdata"
    client = project.parent / "aigisr_exe"
    if (not (resource_root / "active-index.json").is_file() or not (masters / "active.json").is_file()
            or not client.is_dir()):
        pytest.skip("Verified local resources and public masters are required")
    master = MasterDataStore(masters).snapshot()
    _, entries = ResourceLibrary(resource_root, client_hash=master.client_hash).load()
    assets = IndexedAssetService(entries, resource_root, client)
    reader = lambda name: assets.read(name)[0]
    possessions = PossessionService(Path("."), asset_reader=reader)
    items = PresentItemCatalog(possessions, reader(ITEM_RESOURCE_NAME), emblems=EmblemCatalog(possessions, reader))
    pools = SimpleNamespace(content_version=hashlib.sha256(reader("SummonConfig.atb")).hexdigest(),
                            status=lambda: {"ready": True}, is_ready=lambda: True)
    passes = EventPassCatalog(reader, possessions)
    votes = VoteCatalog(reader, master.get_xml)
    sources = {"recovery_catalog": PresentRecoveryCatalog(reader),
               "item_tickets": SimpleNamespace(catalog=ItemSummonCatalog(pools, reader)),
               "pass_points": MissionPassPoints(SimpleNamespace(catalog=passes, possessions=possessions)),
               "vote_tickets": VoteTicketService(possessions, votes)}
    return SimpleNamespace(classes=ItemClasses(items, **sources), items=items, possessions=possessions,
                           passes=passes, **sources)


def test_current_resources_report_the_verified_class_counts(current):
    status = current.classes.status()
    assert status["ready"] is True and status["error"] is None
    assert status["counts"] == CURRENT_COUNTS
    assert status["item_count"] == current.possessions.status()["count"] == sum(CURRENT_COUNTS.values())
    assert status["sources"] == {**dict.fromkeys(SOURCE_NAMES, True), "emblems": True}
    assert status["fallback_effect_types"] == CURRENT_FALLBACK_EFFECT_TYPES


def test_current_classes_agree_with_their_owning_catalogs(current):
    classes, counts = current.classes, CURRENT_COUNTS
    assert (counts["passive"] + counts["skin"] + counts["ownership"] + counts["emblem"]
            == current.items.status()["item_count"])
    assert (counts["recovery"] + counts["item_ticket"] + counts["stack_zero"]
            == current.possessions.status()["unsupported_stack_count"])
    assert classes.item_ids(ItemClass.SKIN) == current.items.skin_item_ids
    assert classes.item_ids(ItemClass.EMBLEM) == current.items.emblem_item_ids
    assert classes.item_ids(ItemClass.ITEM_TICKET) == current.item_tickets.catalog.item_ids
    assert classes.item_ids(ItemClass.EVENT_PASS) == {name for entry in current.passes.passes
                                                     for name in (entry.item_id, entry.premium_item_id) if name}
    assert classes.item_ids(ItemClass.VOTE_TICKET) == {DAILY_ITEM, *(group.item_id for group in
                                                                     current.vote_tickets.catalog.groups)}
    assert all(current.recovery_catalog.supports_item(name) for name in classes.item_ids(ItemClass.RECOVERY))
    for name in classes.item_ids(ItemClass.PASSIVE) | classes.item_ids(ItemClass.OWNERSHIP):
        single_only = classes.classify(name) is ItemClass.OWNERSHIP
        assert current.items.definition(ITEM_PRESENT_TYPE, name).single_only is single_only
    assert classes.classify(PASSIVE) is ItemClass.PASSIVE
    assert classes.classify("item_smn_ticket_19138") is ItemClass.ITEM_TICKET
    assert classes.classify(DAILY_ITEM) is ItemClass.VOTE_TICKET
