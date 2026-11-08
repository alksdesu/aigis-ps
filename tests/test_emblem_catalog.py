from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
from pathlib import Path
import struct
from types import SimpleNamespace
import zlib

import lz4.frame
import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.al_format import decompress_all4
from aigis_ps.asset_service import IndexedAssetService
import aigis_ps.emblem_catalog as module
from aigis_ps.emblem_catalog import CATEGORY, CLEAR_FLAG, EmblemCatalog, ITEM_FIELDS, TITLE_FIELDS
from aigis_ps.errors import ProtocolError
from aigis_ps.possessions import STORED_ITEM_NAME_BYTES, PossessionService
from aigis_ps.present_items import PresentItemCatalog
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.system_literals import encode_command
from test_native_numbers import BOUNDARY_VALUES, fits_int32
from test_present_items import item_table


ITEM = "get_emblem_test"
VOTE = "item_vote_test"
STORED = ITEM + "x" * (STORED_ITEM_NAME_BYTES - len(ITEM))
SECOND_EFFECT = [{"_ResID": CATEGORY, "_TypeID": 10}, {"_ResID": 0, "_TypeID": 11}]


def signed_hash(text):
    return (zlib.crc32(text.encode()) ^ 0x80000000) - 0x80000000


def item(name=ITEM, **changes):
    result = dict.fromkeys(ITEM_FIELDS, 0)
    result.update(ItemID=name, ID=signed_hash(name), EventID="", EffectType=10, Category=CATEGORY, Stack=1, EmblemID=3)
    result.update(changes)
    return result


def title(group=1, expression="0xe52a3bdd(0)", **changes):
    result = dict.fromkeys(TITLE_FIELDS, 0)
    result.update(PlayerTitle="Title", _ExpressionValue=expression, Explan="description", ConditionQuestList="",
                  GroupId=group, EmblemLevel=1, Effect=1, EffectPower=10)
    result.update(changes)
    return result


def resources(*, items=None, titles=None, command='CMenuTaskSendMessage(0,"DoGetItem" );', effects=None):
    return {
        "ItemConfigTable.atb": item_table(items if items is not None else [item(),
            item(VOTE, Category=1925062086, Stack=999, CanAllGet=1, EmblemID=0)], tuple(ITEM_FIELDS.items())),
        "PlayerTitle.atb": item_table(titles if titles is not None else [title(), title(2, "0xe52a3bdd(10)"),
            title(3, "0x441add5b(42)"), title(4, f'HasPossession("ItemId={ITEM}");', Effect=5, EffectPower=99)],
            tuple(TITLE_FIELDS.items())),
        "SystemItemEffectType.atb": item_table(effects if effects is not None else [{"_ResID": CATEGORY, "_TypeID": 10}],
            (("_ResID", 1), ("_TypeID", 1))),
        "OnCommandMenuPresentList.atb": item_table([{"_ID": signed_hash(module.RECEIVE_COMMAND), "_Cmnd": encode_command(command)}],
            (("_ID", 1), ("_Cmnd", 32))),
    }


def components(payloads=None):
    payloads = resources() if payloads is None else payloads
    possessions = PossessionService(Path("."), asset_reader=payloads.__getitem__)
    return SimpleNamespace(catalog=EmblemCatalog(possessions, payloads.__getitem__),
                           possessions=possessions, payloads=payloads)


def state(rank=1, count=0, flags=0):
    return {"bootstrap_schema": 1, "tables": {"game_param": [{"Rank": rank}],
        "quest_records": [{"QuestID": 42, "AchieveFlag": flags}],
        "possessions": [{"ItemID": ITEM, "Count": count}]}, "unrelated": {"values": [1]}}


def decoded_cell(monkeypatch, marker, field, value, row=0):
    original = module.parse_table
    def altered(data, limits):
        result = original(data, limits)
        if marker in {entry["name"] for entry in result["fields"]}:
            result["rows"][row][field] = value
        return result
    monkeypatch.setattr(module, "parse_table", altered)


@pytest.fixture(scope="module")
def current():
    project = Path(__file__).resolve().parents[1]
    root = project / "runtime/resources"
    _, entries = ResourceLibrary(root, client_hash=
        CURRENT_CLIENT_SHA256).load()
    assets = IndexedAssetService(entries, root, project.parent / "aigisr_exe")
    reader = lambda name: assets.read(name)[0]
    possessions = PossessionService(project.parent / "aigisr_exe", asset_reader=reader)
    return SimpleNamespace(catalog=EmblemCatalog(possessions, reader), possessions=possessions, reader=reader)


def test_current_catalog_retains_all_titles_and_excludes_vote_items(current):
    catalog = current.catalog
    assert len(catalog.titles) == 125
    assert tuple(catalog.by_id) == tuple(range(125))
    assert len(catalog.item_ids) == 83
    assert all(name.startswith("get_emblem_") for name in catalog.item_ids)
    assert not any(name.startswith("item_vote_") for name in catalog.item_ids)
    assert catalog.status()["predicate_counts"] == {"rank": 20, "quest": 23, "possession": 82}
    assert catalog.status()["group_count"] == 59
    assert catalog.status()["unsupported_titles"] == []
    assert catalog.item_resource_sha256 == current.possessions.asset_sha256
    raw = current.reader("PlayerTitle.atb")
    decoded = decompress_all4(raw) if raw.startswith(b"ALL4") else raw
    assert catalog.title_resource_sha256 == hashlib.sha256(decoded).hexdigest()


def test_every_real_title_uses_its_own_predicate_and_preserves_state(current):
    catalog = current.catalog
    for definition in catalog.titles:
        snapshot = state()
        snapshot["tables"]["quest_records"] = []
        snapshot["tables"]["possessions"] = []
        if definition.predicate == "rank":
            snapshot["tables"]["game_param"][0]["Rank"] = max(1, definition.argument)
        elif definition.predicate == "quest":
            snapshot["tables"]["quest_records"] = [{"QuestID": definition.argument, "AchieveFlag": CLEAR_FLAG}]
        else:
            snapshot["tables"]["possessions"] = [{"ItemID": definition.argument, "Count": 1, "Enabled": 0}]
        original = deepcopy(snapshot)
        assert catalog.eligible(definition.index, snapshot)
        assert catalog.require_eligible(definition.index, snapshot) is definition
        assert snapshot == original
        if definition.predicate != "rank":
            snapshot["tables"]["quest_records"] = []
            snapshot["tables"]["possessions"] = []
            assert catalog.eligible(definition.index, snapshot) is False


def test_current_display_mapping_does_not_override_unlock_expression(current):
    catalog = current.catalog
    assert catalog.items["get_emblem_00034"].emblem_id == 70
    snapshot = state()
    snapshot["tables"]["possessions"] = [{"ItemID": "get_emblem_00034", "Count": 1}]
    snapshot["tables"]["quest_records"] = []
    assert not catalog.eligible(70, snapshot)
    assert catalog.eligible(71, snapshot)
    snapshot["tables"]["possessions"] = [{"ItemID": "get_emblem_10018", "Count": 1}]
    assert catalog.items["get_emblem_10018"].emblem_id == 123
    assert not catalog.eligible(123, snapshot)
    snapshot["tables"]["quest_records"] = [{"QuestID": 45002, "AchieveFlag": CLEAR_FLAG}]
    assert catalog.eligible(123, snapshot)


def test_current_condition_metadata_is_not_an_additional_and(current):
    catalog = current.catalog
    target = catalog.get(19)
    assert target.condition_quest_list == "6312"
    assert target.condition_clear_rank == 1
    snapshot = state()
    snapshot["tables"]["possessions"] = [{"ItemID": "get_emblem_10001", "Count": 1}]
    snapshot["tables"]["quest_records"] = []
    assert catalog.eligible(19, snapshot)


def test_default_and_rank_only_titles_require_no_items(current):
    snapshot = state()
    snapshot["tables"]["possessions"] = []
    assert current.catalog.eligible(0, snapshot)
    assert current.catalog.eligible(1, snapshot)
    assert not current.catalog.eligible(2, snapshot)
    assert current.catalog.get(0).prince_card_id == 1
    assert current.catalog.get(18).prince_card_id == 309
    assert current.catalog.get(33).prince_card_id == 1377
    snapshot["tables"]["game_param"][0]["Rank"] = 501
    assert current.catalog.eligible(20, snapshot)
    assert current.catalog.eligible(25, snapshot)


def test_removed_quest_master_entries_still_use_persisted_record(current):
    snapshot = state()
    for index, quest in ((27, 6726), (108, 8514)):
        snapshot["tables"]["quest_records"] = [{"QuestID": quest, "AchieveFlag": CLEAR_FLAG}]
        assert current.catalog.eligible(index, snapshot)


@pytest.mark.parametrize("rank,expected", [(1, False), (9, False), (10, True), (11, True), (0x7FFFFFFF, True)])
def test_rank_predicate_is_inclusive(rank, expected):
    assert components().catalog.eligible(1, state(rank=rank)) is expected


@pytest.mark.parametrize("flags,expected", [(0, False), (1, False), (3, False), (0x00010000, False),
    (CLEAR_FLAG, True), (CLEAR_FLAG | 3, True), (-0x80000000, False), (-0x7F000000, True), (-1, True)])
def test_quest_predicate_checks_bit24_not_star_count(flags, expected):
    assert components().catalog.eligible(2, state(flags=flags)) is expected


@pytest.mark.parametrize("count,expected", [(0, False), (1, True), (2, True), (0x7FFFFFFF, True)])
@pytest.mark.parametrize("enabled", [None, 0, 1])
def test_possession_predicate_is_positive_count_including_old_overstack(count, expected, enabled):
    snapshot = state(count=count)
    if enabled is not None:
        snapshot["tables"]["possessions"][0]["Enabled"] = enabled
    assert components().catalog.eligible(3, snapshot) is expected


def test_required_locked_title_raises_without_mutation():
    snapshot = state()
    original = deepcopy(snapshot)
    with pytest.raises(ProtocolError) as error:
        components().catalog.require_eligible(3, snapshot)
    assert (error.value.code, error.value.status) == ("emblem_locked", 409)
    assert snapshot == original


@pytest.mark.parametrize("index", [True, False, -1, 4, 0x80000000, 1.0, "1", None])
def test_index_is_strict_and_zero_is_not_a_sentinel(index):
    with pytest.raises(ProtocolError) as error:
        components().catalog.get(index)
    assert error.value.code == "emblem_unknown"


@pytest.mark.parametrize("expression,reason", [("", "expression_unsupported"), ("DoSomething();", "expression_unsupported"),
    ("0xe52a3bdd(1)||1", "expression_unsupported"), ("0xe52a3bdd(2147483648)", "predicate_argument_out_of_range"),
    ("0x441add5b(0)", "predicate_argument_out_of_range"), ('HasPossession("ItemId=missing");', "predicate_item_unavailable"),
    (f'HasPossession("ItemId={ITEM},OR=1");', "predicate_item_unavailable")])
def test_unknown_expression_is_preserved_but_never_granted(expression, reason):
    rows = [title(i + 1) for i in range(4)] + [title(5, expression)]
    case = components(resources(titles=rows))
    assert case.catalog.get(4).expression == expression
    assert case.catalog.get(4).unsupported_reason == reason
    assert case.catalog.status()["ready"]
    assert case.catalog.status()["unsupported_titles"] == [4]
    with pytest.raises(ProtocolError) as error:
        case.catalog.eligible(4, state(rank=0x7FFFFFFF, count=1, flags=-1))
    assert error.value.code == "emblem_unsupported"


def test_new_valid_rows_and_group_levels_are_not_hardcoded():
    rows = [title(i + 1) for i in range(4)] + [title(1, "0xe52a3bdd(5)", EmblemLevel=2)]
    case = components(resources(titles=rows))
    assert case.catalog.get(4).group_id == 1
    assert case.catalog.get(4).emblem_level == 2
    assert case.catalog.eligible(4, state(rank=5))


def test_catalog_objects_and_status_are_independent():
    case = components()
    with pytest.raises(FrozenInstanceError):
        case.catalog.get(0).name = "other"
    with pytest.raises(FrozenInstanceError):
        case.catalog.items[ITEM].emblem_id = 0
    with pytest.raises(TypeError):
        case.catalog.by_id[9] = case.catalog.get(0)
    with pytest.raises(TypeError):
        case.catalog.items[ITEM] = None
    with pytest.raises(AttributeError):
        case.catalog.titles = ()
    status = case.catalog.status()
    status["resource_sha256"].clear()
    assert len(case.catalog.status()["resource_sha256"]) == 4


@pytest.mark.parametrize("field,value", [("Category", 0), ("EffectType", 12), ("Stack", 0), ("Stack", 2),
    ("CanAllGet", 1), ("NoAutoDelete", 1), ("ReceiveAction", 1), ("ReceiveConfirmation", 1),
    ("InvisibleContaint", 1), ("CanPresent", 1), ("EventID", "event"), ("EmblemID", -1),
    ("EmblemID", 4), ("ID", 12)])
def test_item_acquisition_contract_is_strict(field, value):
    with pytest.raises(ProtocolError) as error:
        components(resources(items=[item(**{field: value})]))
    assert error.value.code == "emblem_catalog_invalid"


@pytest.mark.parametrize("payloads", [resources(items=[item(), item()]), resources(items=[]), resources(titles=[]),
    resources(titles=[title(), title(), title(3), title(4)]), resources(effects=[]),
    resources(effects=[{"_ResID": 0, "_TypeID": 10}]),
    resources(command='CMenuTaskSendMessage(0,"DoGetItem"); SetTeamEmblemID(3);')],
    ids=["duplicate-item", "no-item", "no-title", "duplicate-level", "no-effect", "wrong-effect", "extra-command"])
def test_resource_identity_and_behavior_fail_closed(payloads):
    with pytest.raises(ProtocolError) as error:
        components(payloads)
    assert error.value.code == "emblem_catalog_invalid"


def test_stored_bound_emblems_answer_titles_but_are_never_mailed():
    titles = [title(), title(2, "0xe52a3bdd(10)"), title(3, "0x441add5b(42)"),
              title(4, f'HasPossession("ItemId={STORED}");', Effect=5, EffectPower=99)]
    case = components(resources(items=[item(), item(STORED)], titles=titles))
    assert case.catalog.item_ids == frozenset({ITEM}) and set(case.catalog.items) == {ITEM}
    status = case.catalog.status()
    assert status["ready"] and status["unsupported_items"] == [STORED] and status["item_count"] == 1
    assert case.catalog.get(3).supported and case.catalog.get(3).argument == STORED
    mail_items = PresentItemCatalog(case.possessions, case.payloads["ItemConfigTable.atb"], emblems=case.catalog)
    assert mail_items.emblem_item_ids == frozenset({ITEM}) and mail_items.definition(22, ITEM).single_only
    with pytest.raises(ProtocolError) as error:
        mail_items.definition(22, STORED)
    assert (error.value.code, error.value.status) == ("present_item_unsupported", 501)
    snapshot = state()
    snapshot["tables"]["possessions"].append({"ItemID": STORED, "Count": 1})
    before = deepcopy(snapshot)
    assert case.catalog.eligible(3, snapshot) and snapshot == before
    snapshot["tables"]["possessions"][-1]["Count"] = 0
    assert case.catalog.eligible(3, snapshot) is False


@pytest.mark.parametrize("payloads", [
    resources(items=[item(STORED)]),
    resources(items=[item(), item(STORED + "x")]),
    resources(titles=[title(), title(2), title(3), title(4, f'HasPossession("ItemId={STORED}x");')]),
], ids=["no-mailable-emblem", "past-stored-bound", "condition-past-stored-bound"])
def test_stored_bound_emblems_keep_the_catalog_contract(payloads):
    with pytest.raises(ProtocolError) as error:
        components(payloads)
    assert error.value.code == "emblem_catalog_invalid"


@pytest.mark.parametrize("name", list(resources()))
def test_missing_resources_fail_closed(name):
    case = components()
    payloads = resources()
    del payloads[name]
    with pytest.raises(ProtocolError) as error:
        EmblemCatalog(case.possessions, payloads.__getitem__)
    assert error.value.code == "emblem_catalog_invalid"


def test_all4_resources_and_fingerprint_follow_content():
    first = components()
    compressed = {name: struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)
                  for name, raw in first.payloads.items()}
    second = components(compressed)
    assert second.catalog.item_resource_sha256 == first.catalog.item_resource_sha256
    assert second.catalog.title_resource_sha256 == first.catalog.title_resource_sha256
    assert second.catalog.content_version != first.catalog.content_version
    changed = resources(command=' CMenuTaskSendMessage ( 0 , "DoGetItem" ); ')
    assert components(changed).catalog.content_version != first.catalog.content_version
    assert components(first.payloads).catalog.content_version == first.catalog.content_version


@pytest.mark.parametrize("field,value", [("asset_sha256", "0" * 64), ("_definitions", {ITEM: True, VOTE: 999}),
    ("_names_by_hash", {}), ("issues", {"test": "changed"})])
def test_mutated_possession_dependencies_rejected(field, value):
    case = components()
    setattr(case.possessions, field, value)
    assert not case.catalog.status()["ready"]
    assert case.catalog.is_ready() is False
    with pytest.raises(ProtocolError) as error:
        case.catalog.get(0)
    assert error.value.code == "emblem_catalog_changed"


def test_readiness_follows_the_possessions_without_building_their_status():
    case = components()
    case.possessions.status = lambda: pytest.fail("readiness must not build the possession status")
    assert case.catalog.is_ready() is True and case.catalog.status()["ready"] is True
    case.possessions.issues["test"] = "changed"
    assert case.catalog.is_ready() is False
    assert case.catalog.status()["error"] == "emblem_catalog_changed"


@pytest.mark.parametrize("snapshot", [None, {}, {"bootstrap_schema": True, "tables": {}},
    {"bootstrap_schema": 1, "tables": []}])
def test_invalid_top_level_state(snapshot):
    with pytest.raises(ProtocolError) as error:
        components().catalog.eligible(0, snapshot)
    assert error.value.code == "emblem_state_invalid"


@pytest.mark.parametrize("rank", [True, 0, -1, 0x80000000, "10", None])
def test_invalid_rank_does_not_unlock_default(rank):
    with pytest.raises(ProtocolError) as error:
        components().catalog.eligible(0, state(rank=rank))
    assert error.value.code == "emblem_state_invalid"


@pytest.mark.parametrize("records", [None, {}, [{"QuestID": True, "AchieveFlag": CLEAR_FLAG}],
    [{"QuestID": 42, "AchieveFlag": True}], [{"QuestID": 42, "AchieveFlag": 0x80000000}],
    [{"QuestID": 42, "AchieveFlag": 0}, {"QuestID": 42, "AchieveFlag": CLEAR_FLAG}], [{}]])
def test_invalid_quest_history_not_ignored(records):
    snapshot = state()
    snapshot["tables"]["quest_records"] = records
    with pytest.raises(ProtocolError) as error:
        components().catalog.eligible(2, snapshot)
    assert error.value.code == "emblem_state_invalid"


@pytest.mark.parametrize("count", [True, -1, "1", 0x80000000])
def test_invalid_possession_uses_shared_storage_validation(count):
    with pytest.raises(ProtocolError) as error:
        components().catalog.eligible(3, state(count=count))
    assert error.value.code == "possession_state_invalid"


@pytest.mark.parametrize("field", ["GroupId", "EmblemLevel", "Effect", "ConditionItemId"])
def test_decoded_boolean_is_not_a_native_integer(monkeypatch, field):
    decoded_cell(monkeypatch, "PlayerTitle", field, True)
    with pytest.raises(ProtocolError) as error:
        components()
    assert error.value.code == "emblem_catalog_invalid"


@pytest.mark.parametrize("marker,field,row,effects", [("ItemID", "ReceiveAction", 1, None),
    ("PlayerTitle", "ConditionTextId", 0, None), ("_TypeID", "_ResID", 1, SECOND_EFFECT),
    ("_TypeID", "_TypeID", 1, SECOND_EFFECT)], ids=["item", "title", "effect_resource", "effect_type"])
@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_decoded_integer_cells_admit_exactly_the_native_int32_range(monkeypatch, marker, field, row, effects, value):
    decoded_cell(monkeypatch, marker, field, value, row)
    if fits_int32(value):
        assert components(resources(effects=effects)).catalog.status()["ready"]
    else:
        with pytest.raises(ProtocolError) as error:
            components(resources(effects=effects))
        assert error.value.code == "emblem_catalog_invalid"


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_saved_quest_flags_admit_exactly_the_native_int32_range(value):
    if fits_int32(value):
        assert components().catalog.eligible(2, state(flags=value)) is bool(value & CLEAR_FLAG)
    else:
        with pytest.raises(ProtocolError) as error:
            components().catalog.eligible(2, state(flags=value))
        assert error.value.code == "emblem_state_invalid"


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_saved_ranks_and_quest_identities_admit_positive_native_int32_values(value):
    catalog, records = components().catalog, state()
    records["tables"]["quest_records"] = [{"QuestID": value, "AchieveFlag": CLEAR_FLAG}]
    for index, snapshot, eligible in ((0, state(rank=value), True), (2, records, value == 42)):
        if fits_int32(value) and value > 0:
            assert catalog.eligible(index, snapshot) is eligible
        else:
            with pytest.raises(ProtocolError) as error:
                catalog.eligible(index, snapshot)
            assert error.value.code == "emblem_state_invalid"


@pytest.mark.parametrize("expression,reason", [("0xe52a3bdd(2147483647)", None), ("0x441add5b(2147483647)", None),
    ("0xe52a3bdd(2147483648)", "predicate_argument_out_of_range"),
    ("0x441add5b(2147483648)", "predicate_argument_out_of_range")])
def test_predicate_arguments_reach_the_native_int32_maximum(expression, reason):
    rows = [title(i + 1) for i in range(4)] + [title(5, expression)]
    assert components(resources(titles=rows)).catalog.get(4).unsupported_reason == reason
