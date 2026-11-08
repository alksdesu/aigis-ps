from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
from pathlib import Path
import struct
from types import SimpleNamespace
import zlib

import lz4.frame
import pytest

from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.emblem_catalog import CATEGORY as EMBLEM_CATEGORY
from aigis_ps.errors import ProtocolError
from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.possessions import ITEM_RESOURCE_LIMIT, STORED_ITEM_NAME_BYTES, PossessionService
from aigis_ps.present_items import LEGACY_ITEMS, PresentItemCatalog, PresentItemDefinition
from aigis_ps.resource_library import ResourceLibrary
from test_al_format import schema_bytes


SCHEMA = (("ItemID", 32), ("ID", 1), ("Stack", 1), ("EffectType", 1),
          ("ReceiveAction", 1), ("EventID", 32), ("ReceiveConfirmation", 1), ("CanPresent", 1),
          ("Category", 1), ("CanAllGet", 1), ("NoAutoDelete", 1), ("InvisibleContaint", 1))
EMBLEM, EMBLEM_ID = "get_emblem_test", 3


def row(name="gift_0002_ruby", **updates):
    result = {"ItemID": name, "ID": (zlib.crc32(name.encode()) ^ 0x80000000) - 0x80000000,
              "Stack": 999, "EffectType": 4, "ReceiveAction": 0, "EventID": "",
              "ReceiveConfirmation": 0, "CanPresent": 1, "Category": 0,
              "CanAllGet": 1, "NoAutoDelete": 0, "InvisibleContaint": 0}
    result.update(updates)
    return result


def item_table(rows, schema=SCHEMA):
    stride = 4 * len(schema)
    fields = [(name, index * 4, kind, b"" if kind == 32 else b"\0" * 8)
              for index, (name, kind) in enumerate(schema)]
    header = schema_bytes(fields, stride)
    records, pool = bytearray(), bytearray()
    for source in rows:
        for name, kind in schema:
            value = source[name]
            if kind == 32:
                records += struct.pack("<I", len(pool))
                pool += str(value).encode() + b"\0"
            else:
                records += struct.pack("<i", value)
    start = 28 + len(header)
    return struct.pack("<4sBBHHHIII4s", b"ALTB", 1, 0x14, len(rows), 28, start, stride,
                       len(pool), start + len(records), b"item") + header + records + pool


def components(rows=None, *, payload=None):
    if payload is None:
        payload = item_table([row()] if rows is None else rows)
    possessions = PossessionService(Path("."), asset_reader=lambda name: payload)
    catalog = PresentItemCatalog(possessions, payload)
    return SimpleNamespace(catalog=catalog, possessions=possessions, payload=payload)


@pytest.fixture(scope="module")
def current_items():
    root = Path(__file__).resolve().parents[2]
    resource_root = root / "aigis_ps/runtime/resources"
    _, entries = ResourceLibrary(resource_root, client_hash=CURRENT_CLIENT_SHA256).load()
    assets = IndexedAssetService(entries, resource_root, root / "aigisr_exe")
    return components(payload=assets.read("ItemConfigTable.atb")[0])


def test_current_resource_passive_scope_is_complete(current_items):
    case = current_items
    raw = decompress_all4(case.payload) if case.payload.startswith(b"ALL4") else case.payload
    source = parse_table(raw)["rows"]
    accepted = []
    for item in source:
        ordinary = item["EffectType"] in (4, 5, 6, 7, 9) and item["Stack"] > 0
        ownership = (item["EffectType"] in (12, 13)
                     and item["Category"] == {12: 190425180, 13: 1165413830}[item["EffectType"]]
                     and item["Stack"] == 1 and item["CanAllGet"] == 0
                     and item["NoAutoDelete"] == 1 and item["InvisibleContaint"] == 0)
        supported = ((ordinary or ownership)
                     and item["ReceiveAction"] == 0 and item["EventID"] == ""
                     and item["ReceiveConfirmation"] == 0)
        if supported:
            expected = PresentItemDefinition(item["ItemID"], item["Stack"], single_only=ownership)
            assert case.catalog.definition(22, item["ItemID"]) == expected
            accepted.append(item)
        else:
            with pytest.raises(ProtocolError, match="unsupported"):
                case.catalog.definition(22, item["ItemID"])
    assert len(source) == 1377
    assert len(accepted) == 396
    assert sum(item["EffectType"] in (4, 5, 6, 7, 9) for item in accepted) == 116
    assert sum(item["EffectType"] == 12 for item in accepted) == 242
    assert sum(item["EffectType"] == 13 for item in accepted) == 38
    assert case.catalog.status()["item_count"] == 396
    assert case.catalog.status()["single_only_count"] == 280
    assert case.catalog.status()["skin_item_count"] == 38
    assert case.catalog.skin_item_ids == frozenset(item["ItemID"] for item in accepted if item["EffectType"] == 13)
    assert case.catalog.status()["legacy_count"] == 11
    assert case.catalog.supported_types == (3, 4, 5, 6, 7, 10, 11, 12, 13, 14, 19, 22)


@pytest.mark.parametrize("kind,name,limit", [
    (3, "gift_0000_bouquet", 999), (4, "gift_0001_quartz", 999),
    (5, "gift_0002_ruby", 999), (6, "gift_0003_diamond", 999),
    (7, "trade_item_001000", 9999), (10, "gift_1000_beer", 999),
    (11, "gift_1001_wine", 999), (12, "gift_1002_fineLiquor", 999),
    (13, "gift_1003_millenniumWine", 999), (14, "crystal_item_001000", 9999),
    (19, "trade_item_003000", 999999),
])
def test_current_native_legacy_bindings(current_items, kind, name, limit):
    assert current_items.catalog.definition(kind) == PresentItemDefinition(name, limit)
    assert current_items.possessions.stack(name) == limit


@pytest.mark.parametrize("name", ["trade_item_001000", "crystal_item_001000", "evolve_orb_000001"])
def test_can_present_zero_does_not_block_native_stock_items(current_items, name):
    assert current_items.catalog.definition(22, name).item_id == name


def test_compressed_and_raw_definitions_have_the_same_identity():
    raw = item_table([row()])
    packed = struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)
    first, second = components(payload=raw), components(payload=packed)
    assert first.catalog.asset_sha256 == second.catalog.asset_sha256 == hashlib.sha256(raw).hexdigest()
    assert first.catalog.content_version == second.catalog.content_version
    assert first.catalog.definition(5) == second.catalog.definition(22, "gift_0002_ruby")


@pytest.mark.parametrize("stack,expected", [(1, 1), (998, 998), (999, 999), (1000, 999), (0x7FFFFFFF, 999)])
def test_legacy_uses_the_smaller_native_and_resource_limits(stack, expected):
    case = components([row(Stack=stack)])
    assert case.catalog.definition(5).stack_limit == expected
    assert case.catalog.definition(22, "gift_0002_ruby").stack_limit == stack


@pytest.mark.parametrize("changes", [
    {"Stack": 0}, {"EffectType": 0}, {"EffectType": 1}, {"EffectType": 2}, {"EffectType": 3},
    {"EffectType": 8}, {"EffectType": 10}, {"EffectType": 11}, {"EffectType": 12},
    {"EffectType": 13}, {"EffectType": 14}, {"EffectType": 99}, {"ReceiveAction": 1},
    {"ReceiveAction": 3}, {"EventID": "event"}, {"EventID": " "}, {"ReceiveConfirmation": 1},
    {"ReceiveConfirmation": -1},
])
def test_unsupported_actions_stay_unavailable_for_both_forms(changes):
    case = components([row(**changes)])
    assert case.catalog.status()["item_count"] == 0
    assert case.catalog.supported_types == ()
    for arguments in ((5,), (22, "gift_0002_ruby")):
        with pytest.raises(ProtocolError) as error:
            case.catalog.definition(*arguments)
        assert error.value.code == "present_item_unsupported" and error.value.status == 501


@pytest.mark.parametrize("kind", [True, False, -1, 0, 1, 2, 8, 9, 15, 17, 23, "22", 22.0, None, 0x80000000])
def test_invalid_or_nonitem_types_are_explicit(kind):
    with pytest.raises(ProtocolError) as error:
        components().catalog.definition(kind)
    assert error.value.code == "present_item_unsupported"


@pytest.mark.parametrize("item_id", [None, "", "unknown", "GIFT_0002_RUBY", 1832285076, "1832285076", [], True])
def test_common_item_requires_exact_canonical_string(item_id):
    with pytest.raises(ProtocolError) as error:
        components().catalog.definition(22, item_id)
    assert error.value.code == "present_item_unsupported"


def test_legacy_identity_cannot_be_overridden():
    case = components()
    for name in ("gift_0002_ruby", "other", ""):
        with pytest.raises(ProtocolError) as error:
            case.catalog.definition(5, name)
        assert error.value.code == "present_item_identity_invalid"
    with pytest.raises(ProtocolError):
        case.catalog.definition(6)


def test_frozen_definitions_status_and_input_are_independent():
    source = [row()]
    before = deepcopy(source)
    case = components(source)
    definition = case.catalog.definition(5)
    with pytest.raises(FrozenInstanceError):
        definition.stack_limit = 0
    status = case.catalog.status()
    status["supported_types"].clear()
    status["passive_effects"].append(11)
    assert case.catalog.supported_types == (5, 22)
    assert case.catalog.status()["passive_effects"] == [4, 5, 6, 7, 9]
    assert source == before
    with pytest.raises(TypeError):
        LEGACY_ITEMS[5] = ("other", 1)


@pytest.mark.parametrize("rows", [[], [row(), row()], [row(ID=1)], [row(ItemID="")],
    [row("x" * (STORED_ITEM_NAME_BYTES + 1))], [row(Stack=-1)], [row(EffectType=-1)], [row(ReceiveAction=-1)]])
def test_invalid_resource_rows_fail_without_mutation(rows):
    raw = item_table(rows)
    before = bytes(raw)
    possessions = PossessionService(Path("."), asset_reader=lambda name: raw)
    with pytest.raises(ProtocolError) as error:
        PresentItemCatalog(possessions, raw)
    assert (error.value.code, error.value.status) == ("present_item_catalog_invalid", 503)
    assert raw == before


def test_item_names_share_the_stored_bound_of_possessions():
    name = "x" * STORED_ITEM_NAME_BYTES
    case = components([row(name)])
    assert case.catalog.definition(22, name) == PresentItemDefinition(name, 999)
    assert case.possessions.stack(name) == 999


@pytest.mark.parametrize("schema", [SCHEMA[:-1][:-1], tuple((name, 32 if name == "Stack" else kind) for name, kind in SCHEMA),
                                    (*SCHEMA, ("Stack", 1))])
def test_missing_wrong_or_duplicate_fields_fail(schema):
    raw = item_table([row()], schema)
    possessions = PossessionService(Path("."), asset_reader=lambda name: raw)
    with pytest.raises(ProtocolError) as error:
        PresentItemCatalog(possessions, raw)
    assert error.value.status == 503


@pytest.mark.parametrize("payload", [None, b"", b"ALTB", bytearray(b"ALTB"), b"x" * (ITEM_RESOURCE_LIMIT + 1)],
                         ids=["none", "empty", "truncated", "mutable", "oversized"])
def test_payload_contract_is_bounded_bytes(payload):
    with pytest.raises(ProtocolError) as error:
        PresentItemCatalog(None, payload)
    assert error.value.code == "present_item_catalog_invalid"


def test_dependency_must_use_the_exact_same_resource():
    first = components()
    changed = item_table([row(Stack=100)])
    with pytest.raises(ProtocolError) as error:
        PresentItemCatalog(first.possessions, changed)
    assert error.value.code == "present_item_dependencies_changed"


@pytest.mark.parametrize("mutation", ["fingerprint", "stack", "missing", "bool_stack", "unavailable"])
def test_changed_dependency_is_rejected_before_exposing_definition(mutation):
    case = components()
    if mutation == "fingerprint":
        case.possessions.asset_sha256 = "0" * 64
    elif mutation == "stack":
        case.possessions._definitions["gift_0002_ruby"] = 500
    elif mutation == "missing":
        del case.possessions._definitions["gift_0002_ruby"]
    elif mutation == "bool_stack":
        case.possessions._definitions["gift_0002_ruby"] = True
    else:
        case.possessions.issues["changed"] = "unavailable"
    for action in (lambda: case.catalog.definition(5), lambda: case.catalog.definition(22, "gift_0002_ruby")):
        with pytest.raises(ProtocolError) as error:
            action()
        assert error.value.code == "present_item_dependencies_changed"
    assert case.catalog.status()["ready"] is False
    assert case.catalog.status()["error"] == "present_item_dependencies_changed"


def test_dependency_rows_are_checked_when_catalog_is_built():
    raw = item_table([row(), row("inactive", EffectType=11, Stack=0)])
    possessions = PossessionService(Path("."), asset_reader=lambda name: raw)
    possessions._definitions["inactive"] = 1
    with pytest.raises(ProtocolError) as error:
        PresentItemCatalog(possessions, raw)
    assert error.value.code == "present_item_dependencies_changed"


def test_rule_resource_changes_change_the_content_fingerprint():
    original = components()
    changed = components([row(ReceiveAction=3)])
    assert original.catalog.content_version != changed.catalog.content_version
    assert len(original.catalog.content_version) == 64


def test_legacy_binding_requires_its_native_effect_type():
    case = components([row(EffectType=5)])
    assert case.catalog.definition(22, "gift_0002_ruby").stack_limit == 999
    assert case.catalog.supported_types == (22,)
    with pytest.raises(ProtocolError) as error:
        case.catalog.definition(5)
    assert error.value.code == "present_item_unsupported"


def ownership(effect, **updates):
    return row("blessing" if effect == 12 else "skin", EffectType=effect,
               Category={12: 190425180, 13: 1165413830}[effect], Stack=1,
               CanAllGet=0, NoAutoDelete=1, **updates)


@pytest.mark.parametrize("effect", [12, 13])
def test_verified_ownership_items_require_individual_claims(effect):
    source = ownership(effect, CanPresent=0)
    case = components([source])
    result = case.catalog.definition(22, source["ItemID"])
    assert result == PresentItemDefinition(source["ItemID"], 1, True)
    assert case.catalog.status()["single_only_count"] == 1
    assert case.catalog.skin_item_ids == (frozenset({source["ItemID"]}) if effect == 13 else frozenset())
    with pytest.raises(FrozenInstanceError):
        result.single_only = False
    with pytest.raises(AttributeError):
        case.catalog.skin_item_ids = frozenset()


@pytest.mark.parametrize("effect", [12, 13])
@pytest.mark.parametrize("field,value", [("Category", 0), ("Stack", 0), ("Stack", 2),
    ("CanAllGet", 1), ("NoAutoDelete", 0), ("InvisibleContaint", 1),
    ("ReceiveAction", 1), ("EventID", "event"), ("ReceiveConfirmation", 1)])
def test_unverified_ownership_variants_are_not_silently_granted(effect, field, value):
    source = ownership(effect)
    source[field] = value
    case = components([source])
    assert case.catalog.status()["single_only_count"] == 0
    assert not case.catalog.skin_item_ids
    with pytest.raises(ProtocolError) as error:
        case.catalog.definition(22, source["ItemID"])
    assert error.value.code == "present_item_unsupported"


def test_existing_passive_and_legacy_items_keep_their_previous_claim_policy():
    case = components([row(), row("gift_1002_fineLiquor"), ownership(12), ownership(13)])
    assert case.catalog.definition(5).single_only is False
    assert case.catalog.definition(12).single_only is False
    assert case.catalog.definition(22, "gift_0002_ruby").single_only is False
    assert PresentItemDefinition("gift_0002_ruby", 999) == PresentItemDefinition("gift_0002_ruby", 999, False)


@pytest.mark.parametrize("field", ["CanAllGet", "NoAutoDelete", "InvisibleContaint"])
def test_ownership_policy_fields_participate_in_fingerprint(field):
    source = ownership(12)
    first = components([source])
    changed = deepcopy(source)
    changed[field] = 1 - changed[field]
    second = components([changed])
    assert first.catalog.content_version != second.catalog.content_version
    assert first.catalog.definition(22, "blessing").single_only
    with pytest.raises(ProtocolError):
        second.catalog.definition(22, "blessing")


@pytest.mark.parametrize("field", ["Category", "CanAllGet", "NoAutoDelete", "InvisibleContaint"])
def test_new_ownership_schema_fields_are_required(field):
    schema = tuple(column for column in SCHEMA if column[0] != field)
    raw = item_table([ownership(12)], schema)
    possessions = PossessionService(Path("."), asset_reader=lambda name: raw)
    with pytest.raises(ProtocolError) as error:
        PresentItemCatalog(possessions, raw)
    assert error.value.code == "present_item_catalog_invalid"


def emblem_mail():
    payload = item_table([row(EmblemID=0), row(EMBLEM, EffectType=10, Category=EMBLEM_CATEGORY, Stack=1, CanAllGet=0,
                                              EmblemID=EMBLEM_ID)], (*SCHEMA, ("EmblemID", 1)))
    plain = components(payload=payload)
    emblems = SimpleNamespace(content_version=hashlib.sha256(EMBLEM.encode()).hexdigest(),
                              item_ids=frozenset({EMBLEM}), items={EMBLEM: SimpleNamespace(emblem_id=EMBLEM_ID)},
                              item_resource_sha256=plain.catalog.asset_sha256, is_ready=lambda: True)
    return SimpleNamespace(plain=plain.catalog, possessions=plain.possessions, emblems=emblems,
                           catalog=PresentItemCatalog(plain.possessions, payload, emblems=emblems))


def test_catalogs_without_emblem_mail_replay_only_their_own_version():
    catalog = components().catalog
    assert catalog.replayable_content_versions() == frozenset({catalog.content_version})


def test_emblem_mail_also_replays_the_version_of_the_same_items_without_it():
    case = emblem_mail()
    assert case.catalog.emblem_item_ids == frozenset({EMBLEM})
    assert case.catalog.content_version != case.plain.content_version
    assert case.catalog.replayable_content_versions() == frozenset({case.catalog.content_version,
                                                                    case.plain.content_version})
    assert case.plain.replayable_content_versions() == frozenset({case.plain.content_version})


def test_readiness_never_builds_the_dependency_status():
    case = emblem_mail()
    for dependency in (case.emblems, case.possessions):
        dependency.status = lambda: pytest.fail("readiness must not build a dependency status")
    assert case.catalog.definition(22, EMBLEM) == PresentItemDefinition(EMBLEM, 1, True)
    assert case.catalog.status()["ready"] is True
    case.emblems.is_ready = lambda: False
    with pytest.raises(ProtocolError) as error:
        case.catalog.definition(22, EMBLEM)
    assert (error.value.code, error.value.status) == ("present_item_dependencies_changed", 503)
    assert case.catalog.status()["error"] == "present_item_dependencies_changed"
