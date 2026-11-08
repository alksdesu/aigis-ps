from copy import deepcopy
from pathlib import Path
import struct
from types import SimpleNamespace

import lz4.frame
import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.blessing_catalog import BlessingCatalog, CATEGORY, COMMAND_FIELDS, DISPLAY_FIELDS
from aigis_ps.errors import ProtocolError
from aigis_ps.possessions import STORED_ITEM_NAME_BYTES, PossessionService
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.system_literals import encode_command
from test_present_items import item_table, row


ITEM = "battle_god_bless_test"
STORED = "b" * STORED_ITEM_NAME_BYTES
TAIL = ''.join(f'CMenuTask_CallCommand("{name}");' for name in (
    "ApplySavedFilter", "UpdateFilterButtons", "UpdateCellList"))
COMMAND = ''.join(f'CMenuTask_CallCommand("BindFilterID",{index},'
    f'"CMenuBattleGodBlessList::FilterButton{index}","{expression}");'
    for index, expression in enumerate(("1;", "$category==1;", "$category==2;", "$category==3;"))) + TAIL


def resources(*, items=None, displays=None, command=COMMAND):
    return {
        "ItemConfigTable.atb": item_table(items if items is not None else [row(ITEM, EffectType=12, Category=CATEGORY, Stack=1)]),
        "BattleGodBlessConfigTable.atb": item_table(displays if displays is not None else [
            {"ItemID": ITEM, "Name": "加護", "Category": 1, "Description": "所持している加護"}], tuple(DISPLAY_FIELDS.items())),
        "OnCommandMenuBattleGodBlessList.atb": item_table([
            {"_ID": 1, "_Cmnd": encode_command(command)}], tuple(COMMAND_FIELDS.items())),
    }


def components(payloads=None):
    payloads = resources() if payloads is None else payloads
    possessions = PossessionService(Path("."), asset_reader=payloads.__getitem__)
    return SimpleNamespace(catalog=BlessingCatalog(possessions, payloads.__getitem__),
                           possessions=possessions, payloads=payloads)


def state(count=1):
    return {"bootstrap_schema": 1, "tables": {"possessions": [{"ItemID": ITEM, "Count": count}]}}


@pytest.fixture(scope="module")
def current():
    project = Path(__file__).resolve().parents[1]
    root = project / "runtime/resources"
    _, entries = ResourceLibrary(root, client_hash=
        CURRENT_CLIENT_SHA256).load()
    assets = IndexedAssetService(entries, root, project.parent / "aigisr_exe")
    reader = lambda name: assets.read(name)[0]
    possessions = PossessionService(project.parent / "aigisr_exe", asset_reader=reader)
    return BlessingCatalog(possessions, reader)


def test_current_catalog_covers_all_owned_blessings_and_native_filters(current):
    assert len(current.item_ids) == 242
    assert current.filter_values == frozenset({0, 1, 2, 3})
    assert current.status()["category_counts"] == {1: 51, 2: 117, 3: 74}
    snapshot = {"bootstrap_schema": 1, "tables": {"possessions": [
        {"ItemID": name, "Count": 1} for name in sorted(current.item_ids)]}}
    before = deepcopy(snapshot)
    current.validate(snapshot, {name: 1 for name in current.item_ids})
    current.validate(snapshot, {name: 0 for name in current.item_ids})
    assert snapshot == before


@pytest.mark.parametrize("count", [1, 2, 0x7FFFFFFF])
@pytest.mark.parametrize("enabled", [0, 1])
def test_owned_and_legacy_overstack_items_can_be_switched_without_mutation(count, enabled):
    case = components()
    snapshot = state(count)
    before = deepcopy(snapshot)
    assert case.catalog.validate(snapshot, {ITEM: enabled}) is None
    assert snapshot == before


@pytest.mark.parametrize("enabled", [0, 1])
def test_unowned_items_cannot_be_enabled_or_disabled(enabled):
    case = components()
    for snapshot in (state(0), {"bootstrap_schema": 1, "tables": {"possessions": []}}):
        before = deepcopy(snapshot)
        with pytest.raises(ProtocolError) as error:
            case.catalog.validate(snapshot, {ITEM: enabled})
        assert error.value.code == "blessing_not_owned"
        assert snapshot == before


@pytest.mark.parametrize("settings", [{"unknown": 0}, {"unknown": 1}, {1: 1}])
def test_settings_require_catalog_item_identity(settings):
    with pytest.raises(ProtocolError) as error:
        components().catalog.validate(state(), settings)
    assert error.value.code == "blessing_item_unknown"


@pytest.mark.parametrize("value", [True, False, -1, 2, "1", None, 1.0])
def test_enabled_values_are_strict_native_zero_or_one(value):
    with pytest.raises(ProtocolError) as error:
        components().catalog.validate(state(), {ITEM: value})
    assert error.value.code == "blessing_settings_invalid"


def test_filter_only_change_can_validate_an_empty_setting_list():
    case = components()
    assert case.catalog.validate({"bootstrap_schema": 1, "tables": {"possessions": []}}, {}) is None


def test_catalog_sets_and_status_are_isolated():
    case = components()
    assert type(case.catalog.item_ids) is frozenset
    assert type(case.catalog.filter_values) is frozenset
    with pytest.raises(AttributeError):
        case.catalog.item_ids = frozenset()
    status = case.catalog.status()
    status["filters"].clear()
    status["category_counts"].clear()
    assert case.catalog.filter_values == frozenset({0, 1, 2, 3})
    assert case.catalog.status()["category_counts"] == {1: 1}


@pytest.mark.parametrize("mutation", ["hash", "stack", "boolean_stack", "missing", "unavailable"])
def test_changed_possession_dependency_fails_closed(mutation):
    case = components()
    if mutation == "hash":
        case.possessions.asset_sha256 = "0" * 64
    elif mutation == "stack":
        case.possessions._definitions[ITEM] = 2
    elif mutation == "boolean_stack":
        case.possessions._definitions[ITEM] = True
    elif mutation == "missing":
        del case.possessions._definitions[ITEM]
    else:
        case.possessions.issues["broken"] = "unavailable"
    with pytest.raises(ProtocolError) as error:
        case.catalog.validate(state(), {ITEM: 1})
    assert error.value.code == "blessing_catalog_changed"
    assert case.catalog.status()["ready"] is False
    assert case.catalog.is_ready() is False


def test_readiness_follows_the_possessions_without_building_their_status():
    case = components()
    case.possessions.status = lambda: pytest.fail("readiness must not build the possession status")
    assert case.catalog.is_ready() is True and case.catalog.status()["ready"] is True
    case.possessions.issues["broken"] = "unavailable"
    assert case.catalog.is_ready() is False
    assert case.catalog.status()["error"] == "blessing_catalog_changed"


@pytest.mark.parametrize("damage", ["no_items", "wrong_category", "wrong_stack", "missing_display",
    "extra_display", "duplicate_display", "display_category", "unknown_item", "duplicate_item"])
def test_inventory_and_display_tables_must_match_exactly(damage):
    items = [row(ITEM, EffectType=12, Category=CATEGORY, Stack=1)]
    displays = [{"ItemID": ITEM, "Name": "加護", "Category": 1, "Description": "所持している加護"}]
    if damage == "no_items":
        items[0]["EffectType"] = 4
    elif damage == "wrong_category":
        items[0]["Category"] = 0
    elif damage == "wrong_stack":
        items[0]["Stack"] = 2
    elif damage == "missing_display":
        displays = []
    elif damage == "extra_display":
        displays.append({**displays[0], "ItemID": "extra"})
    elif damage == "duplicate_display":
        displays *= 2
    elif damage == "display_category":
        displays[0]["Category"] = 4
    elif damage == "unknown_item":
        items[0]["ID"] += 1
    else:
        items *= 2
    with pytest.raises(ProtocolError) as error:
        components(resources(items=items, displays=displays))
    assert error.value.code == "blessing_catalog_invalid"


def blessing(name):
    return row(name, EffectType=12, Category=CATEGORY, Stack=1)


def display(name):
    return {"ItemID": name, "Name": "加護", "Category": 1, "Description": "所持している加護"}


def test_stored_bound_blessings_stay_owned_but_are_not_settable():
    case = components(resources(items=[blessing(ITEM), blessing(STORED)], displays=[display(ITEM), display(STORED)]))
    assert case.catalog.item_ids == frozenset({ITEM})
    status = case.catalog.status()
    assert status["ready"] and status["unsupported_items"] == [STORED]
    assert status["item_count"] == 1 and status["category_counts"] == {1: 1}
    snapshot = {"bootstrap_schema": 1, "tables": {"possessions": [
        {"ItemID": ITEM, "Count": 1}, {"ItemID": STORED, "Count": 1}]}}
    before = deepcopy(snapshot)
    assert case.catalog.validate(snapshot, {ITEM: 1}) is None
    with pytest.raises(ProtocolError) as error:
        case.catalog.validate(snapshot, {STORED: 1})
    assert error.value.code == "blessing_item_unknown"
    assert snapshot == before


@pytest.mark.parametrize("items,displays", [
    ([blessing(STORED)], [display(STORED)]),
    ([blessing(ITEM), blessing(STORED)], [display(ITEM)]),
    ([blessing(ITEM), blessing(STORED + "b")], [display(ITEM), display(STORED + "b")]),
], ids=["no-settable-blessing", "missing-stored-display", "past-stored-bound"])
def test_stored_bound_blessings_keep_the_catalog_contract(items, displays):
    with pytest.raises(ProtocolError) as error:
        components(resources(items=items, displays=displays))
    assert error.value.code == "blessing_catalog_invalid"


@pytest.mark.parametrize("command", ["", COMMAND.replace('"BindFilterID",3', '"BindFilterID",4'),
    COMMAND.replace('$category==3;', '$category==2;'), COMMAND.replace('FilterButton3', 'FilterButton4'),
    COMMAND.replace('"BindFilterID",3', '"BindFilterID",2'), 'if (1) {' + COMMAND + '}',
    COMMAND + 'UnknownCall();', COMMAND.replace('"UpdateCellList"', '"Other"')])
def test_only_verified_literal_filter_initializer_is_accepted(command):
    with pytest.raises(ProtocolError) as error:
        components(resources(command=command))
    assert error.value.code == "blessing_catalog_invalid"


def test_filter_whitespace_is_accepted_without_execution():
    command = COMMAND.replace(',', ' , ').replace(';CMenu', ';\nCMenu')
    assert components(resources(command=command)).catalog.filter_values == frozenset({0, 1, 2, 3})


def test_duplicate_filter_initializers_are_rejected():
    payloads = resources()
    payloads["OnCommandMenuBattleGodBlessList.atb"] = item_table([
        {"_ID": 1, "_Cmnd": encode_command(COMMAND)}, {"_ID": 2, "_Cmnd": encode_command(COMMAND)}],
        tuple(COMMAND_FIELDS.items()))
    with pytest.raises(ProtocolError):
        components(payloads)


@pytest.mark.parametrize("resource", ["ItemConfigTable.atb", "BattleGodBlessConfigTable.atb",
                                      "OnCommandMenuBattleGodBlessList.atb"])
def test_missing_resources_fail_without_fallback(resource):
    payloads = resources()
    possessions = PossessionService(Path("."), asset_reader=payloads.__getitem__)
    del payloads[resource]
    with pytest.raises(ProtocolError) as error:
        BlessingCatalog(possessions, payloads.__getitem__)
    assert error.value.code == "blessing_catalog_invalid"


def test_compressed_resource_representation_does_not_change_identity():
    payloads = resources()
    plain = components(payloads).catalog
    packed = {name: struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)
              for name, raw in payloads.items()}
    compressed = components(packed).catalog
    assert compressed.item_ids == plain.item_ids
    assert compressed.content_version == plain.content_version
