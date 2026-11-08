from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import zlib

import pytest

from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.errors import ProtocolError
from aigis_ps.table_cell_patch import patch_table_cells
from aigis_ps.vote_cooking_catalog import VoteCookingCatalog
from test_present_items import item_table
from test_vote_catalog import build, vote_sources


def cooking_sources(*, card_ids=(1, 2, 3), kinds=(0, 9, 10), names=None, text_changes=None,
                    pointer_changes=None, card_payload=None, receive_text_id=5006):
    names = ["First", "Second", "Third"] if names is None else names
    texts = [{"Recode_Index": index, "Data_Text": "."} for index in range(max(5007, receive_text_id + 1))]
    for kind in range(11):
        texts[kind]["Data_Text"] = f"Cooking {kind}"
    texts[11]["Data_Text"] = "Eat [ITEM_NAME]? [ITEM_NAME] restores charisma."
    texts[receive_text_id - 1]["Data_Text"] = "最多投票のお礼の手料理"
    texts[receive_text_id]["Data_Text"] = "[UNIT_NAME] sent cooking."
    for index, updates in (text_changes or {}).items():
        texts[index].update(updates)
    pointers = {f"SystemText_HomeCookingItemName_{kind:03}": kind for kind in range(11)}
    pointers.update(SystemText_HomeCookingUseComfirmation=11, VoteGiftInfomationText=receive_text_id)
    pointers.update(pointer_changes or {})
    return {
        "NameText.atb": item_table([{"Message": name} for name in names], (("Message", 32),)),
        "SystemText.atb": item_table(texts, (("Recode_Index", 1), ("Data_Text", 32))),
        "cfgImplantText.atb": item_table([{"_ID": key, "_TextID": value} for key, value in pointers.items()],
                                         (("_ID", 32), ("_TextID", 1))),
        "CardData": card_payload if card_payload is not None else dumps(Container(children=(
            Column("CardID", "I", card_ids), Column("HomeCooking", "I", kinds)))),
    }


def build_cooking(data=None):
    data = cooking_sources() if data is None else data
    return VoteCookingCatalog(data.__getitem__, data.__getitem__)


def relocated_catalog(catalog, receive_text_id=5028):
    resources = dict(catalog._resources)
    resources["SystemText.atb"] = patch_table_cells(resources["SystemText.atb"], {
        receive_text_id - 1: {"Data_Text": catalog.description},
        receive_text_id: {"Data_Text": catalog.information_template},
    })
    raw = resources["cfgImplantText.atb"]
    rows = parse_table(decompress_all4(raw) if raw.startswith(b"ALL4") else raw)["rows"]
    index = next(index for index, row in enumerate(rows) if row["_ID"] == "VoteGiftInfomationText")
    resources["cfgImplantText.atb"] = patch_table_cells(raw, {index: {"_TextID": receive_text_id}})
    return build_cooking(resources)


def test_zero_and_recovery_drug_are_valid_cooking_kinds():
    catalog = build_cooking()
    first = catalog.definition(1)
    assert (first.card_id, first.name, first.cooking_kind, first.cooking_name) == (1, "First", 0, "Cooking 0")
    assert (first.name_text_id, first.cooking_text_id) == (0, 0)
    assert catalog.definition(3).cooking_kind == 10
    assert catalog.definition_from_content(1) is first
    assert catalog.description == "最多投票のお礼の手料理"
    assert catalog.receive_text_id == 5006
    assert (zlib.crc32(b"VoteGiftInfomationText") ^ 0x80000000) - 0x80000000 == -828020215
    assert catalog.status()["ready"] is True
    assert catalog.status()["cards"] == 3


def test_notification_follows_verified_text_mapping_after_resource_rows_move():
    previous = build_cooking()
    current = build_cooking(cooking_sources(receive_text_id=5028))
    assert current.receive_text_id == 5028
    assert current.description == previous.description
    assert current.information_template == previous.information_template
    assert current.content_version != previous.content_version


@pytest.mark.parametrize("changes", [
    {"pointer_changes": {"VoteGiftInfomationText": 5006}},
    {"text_changes": {5028: {"Recode_Index": 5006}}},
    {"text_changes": {5028: {"Data_Text": "Unknown sender"}}},
    {"text_changes": {5027: {"Data_Text": "Different description"}}},
    {"text_changes": {5027: {"Recode_Index": 5005}}},
])
def test_moved_notification_still_requires_the_native_index_and_text_contract(changes):
    with pytest.raises(ProtocolError) as error:
        build_cooking(cooking_sources(receive_text_id=5028, **changes))
    assert error.value.code == "vote_cooking_catalog_invalid"


def test_notification_at_zero_cannot_use_the_last_text_row_as_its_description():
    with pytest.raises(ProtocolError) as error:
        build_cooking(cooking_sources(receive_text_id=0))
    assert error.value.code == "vote_cooking_catalog_invalid"


@pytest.mark.parametrize("content", [True, False, "1", 1.0, None, 0, -1, 10000, 10029, 0x7FFFFFFF])
def test_unknown_content_encoding_is_rejected(content):
    with pytest.raises(ProtocolError) as error:
        build_cooking().definition_from_content(content)
    assert error.value.code == "vote_cooking_content_invalid"


@pytest.mark.parametrize("card_id", [True, "1", 0, 4, 9999])
def test_unknown_card_identity_is_rejected(card_id):
    with pytest.raises(ProtocolError) as error:
        build_cooking().definition(card_id)
    assert error.value.code == "vote_cooking_card_unknown"


def test_data_and_definitions_are_frozen_and_exposed_status_is_detached():
    data = cooking_sources()
    catalog = build_cooking(data)
    version = catalog.content_version
    with pytest.raises(FrozenInstanceError):
        catalog.definition(1).cooking_kind = 8
    with pytest.raises(TypeError):
        catalog.definitions[1] = catalog.definition(2)
    with pytest.raises(TypeError):
        catalog.cooking_names[0] = (0, "Changed")
    data["CardData"] = b"changed"
    catalog.status()["resource_hashes"]["CardData"] = "changed"
    assert catalog.definition(1).name == "First"
    assert catalog.content_version == version
    assert catalog.status()["resource_hashes"]["CardData"] != "changed"


@pytest.mark.parametrize("attribute", ["definitions", "cooking_names", "description", "receive_text_id",
                                     "use_confirmation", "information_template", "resource_hashes", "_resources", "content_version"])
def test_replacing_published_catalog_objects_is_rejected(attribute):
    catalog = build_cooking()
    setattr(catalog, attribute, object())
    with pytest.raises(ProtocolError) as error:
        catalog.definition(1)
    assert error.value.code == "vote_cooking_catalog_changed"
    assert catalog.is_ready() is False


@pytest.mark.parametrize("attribute,value", [("description", "Changed"), ("receive_text_id", 5007),
                                             ("content_version", "0" * 64)])
def test_readiness_reaches_the_status_conclusion(attribute, value):
    catalog = build_cooking()
    assert catalog.is_ready() is catalog.status()["ready"] is True
    setattr(catalog, attribute, value)
    assert catalog.is_ready() is catalog.status()["ready"] is False
    assert catalog.status()["error"] == "vote_cooking_catalog_changed"


def test_readiness_never_builds_the_status(monkeypatch):
    catalog = build_cooking()
    monkeypatch.setattr(catalog, "status", lambda: pytest.fail("Readiness must not build the catalog status"))
    assert catalog.is_ready() is True


@pytest.mark.parametrize("kwargs", [
    {"card_ids": (1, 1, 3)}, {"card_ids": (0, 2, 3)}, {"card_ids": (1, 2, 10000)},
    {"card_ids": (1, 2, 4)}, {"kinds": (-1, 9, 10)}, {"kinds": (0, 9, 11)},
    {"names": ["", "Second", "Third"]}, {"names": ["x" * 513, "Second", "Third"]},
    {"names": ["Bad\x01Name", "Second", "Third"]},
    {"text_changes": {0: {"Recode_Index": 1}}}, {"text_changes": {0: {"Data_Text": ""}}},
    {"text_changes": {11: {"Data_Text": "[ITEM_NAME]"}}},
    {"text_changes": {5006: {"Data_Text": "Unknown sender"}}},
    {"text_changes": {5005: {"Data_Text": "Different description"}}},
    {"text_changes": {5005: {"Recode_Index": 1}}},
    {"pointer_changes": {"VoteGiftInfomationText": 5005}},
    {"pointer_changes": {"SystemText_HomeCookingItemName_000": 6000}},
    {"pointer_changes": {"SystemText_HomeCookingItemName_010": -1}},
    {"card_ids": (), "kinds": ()},
])
def test_invalid_resource_content_is_rejected(kwargs):
    with pytest.raises(ProtocolError) as error:
        build_cooking(cooking_sources(**kwargs))
    assert error.value.code == "vote_cooking_catalog_invalid"


@pytest.mark.parametrize("columns", [
    (Column("CardID", "I", (1,)), Column("HomeCooking", "I", (0, 1))),
    (Column("CardID", "S", ("1",)), Column("HomeCooking", "I", (0,))),
    (Column("CardID", "I", (1,)), Column("HomeCooking", "S", ("0",))),
    (Column("CardID", "I", (1,)),),
    (Column("CardID", "I", (1,)), Column("CardID", "I", (2,)), Column("HomeCooking", "I", (0,))),
])
def test_public_card_schema_is_validated(columns):
    with pytest.raises(ProtocolError) as error:
        build_cooking(cooking_sources(card_payload=dumps(Container(children=columns))))
    assert error.value.code == "vote_cooking_catalog_invalid"


@pytest.mark.parametrize("name", ["CardData", "NameText.atb", "SystemText.atb", "cfgImplantText.atb"])
def test_mutable_resource_payloads_are_rejected(name):
    data = cooking_sources()
    data[name] = bytearray(data[name])
    with pytest.raises(ProtocolError) as error:
        build_cooking(data)
    assert error.value.code == "vote_cooking_catalog_invalid"


def test_duplicate_text_alias_and_missing_mapping_are_rejected():
    data = cooking_sources()
    data["cfgImplantText.atb"] = item_table([
        {"_ID": "same", "_TextID": 0}, {"_ID": "same", "_TextID": 1}], (("_ID", 32), ("_TextID", 1)))
    with pytest.raises(ProtocolError) as error:
        build_cooking(data)
    assert error.value.code == "vote_cooking_catalog_invalid"
    data["cfgImplantText.atb"] = item_table([{"_ID": "other", "_TextID": 0}], (("_ID", 32), ("_TextID", 1)))
    with pytest.raises(ProtocolError):
        build_cooking(data)


def test_card_provider_object_and_source_versions():
    data = cooking_sources()
    first = VoteCookingCatalog(data.__getitem__, SimpleNamespace(get_xml=data.__getitem__))
    second = build_cooking(cooking_sources(names=["Renamed", "Second", "Third"]))
    assert first.content_version != second.content_version
    assert first.resource_hashes["CardData"] == second.resource_hashes["CardData"]
    assert first.definition(1).name == "First"
    assert second.definition(1).name == "Renamed"


def test_every_current_public_card_and_voting_candidate_has_cooking(vote_sources):
    catalog = build_cooking(vote_sources)
    voting = build(vote_sources)
    assert len(catalog.definitions) == 2932
    assert set(catalog.definitions) == set(range(1, 2933))
    assert len(voting.candidates) == 956
    assert all(catalog.definition(candidate.card_id) for candidate in voting.candidates)
    assert sum(catalog.definition(candidate.card_id).cooking_kind == 0 for candidate in voting.candidates) == 47
    assert catalog.definition(470).cooking_kind == 3
    assert catalog.definition(2089).cooking_kind == 7
    assert catalog.definition(29).cooking_name == "愛情お弁当"
    assert catalog.receive_text_id == 5028
