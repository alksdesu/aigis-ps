from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.package_text_overlay import (CAPTION_KEY, CAPTION_TEXT, CONFIRMATION_TEXT, DESCRIPTION_KEY,
    DESCRIPTION_TEXT, IMPLANT_RESOURCE, SYSTEM_TEXT_RESOURCE, build_package_text_overlay,
    caption_key, description_key, confirmation_key, event_pass_package_ids, local_package_id)
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.table_cell_patch import patch_table_cells
from aigis_ps.table_row_append import append_table_rows
from test_present_items import item_table
import aigis_ps.package_text_overlay as module


IMPLANT_SCHEMA = (("_ID", 32), ("_TextID", 1))
SYSTEM_SCHEMA = (("Recode_Index", 1), ("Data_Text", 32), ("GroupID", 1), ("GroupType", 1), ("GroupRecordID", 1))


def decode(raw):
    return decompress_all4(raw) if raw.startswith(b"ALL4") else raw


def tagged(rows, schema, tag):
    result = bytearray(item_table(rows, schema))
    result[24:28] = tag.encode()
    return bytes(result)


def system_row(index, text):
    return {"Recode_Index": index, "Data_Text": text, "GroupID": 0, "GroupType": 0, "GroupRecordID": 0}


def small_sources():
    return (tagged([{"_ID": "Original", "_TextID": 1}], IMPLANT_SCHEMA, "itcg"),
            tagged([system_row(0, "original zero"), system_row(0, "original duplicate record ID")], SYSTEM_SCHEMA, "sytx"))


@pytest.fixture(scope="module")
def package_resources():
    root = Path(__file__).resolve().parents[1]
    _, entries = ResourceLibrary(root / "runtime/resources", client_hash=
        CURRENT_CLIENT_SHA256).load()
    assets = IndexedAssetService(entries, root / "runtime/resources", root.parent / "aigisr_exe")
    return {name: assets.read(name)[0] for name in (IMPLANT_RESOURCE, SYSTEM_TEXT_RESOURCE, "EventPassConfig.atb")}


def test_actual_resources_add_six_confirmations_and_three_physical_rows(package_resources):
    ids = event_pass_package_ids(package_resources["EventPassConfig.atb"])
    assert ids == (2000000001, 2000000054, 2000000107, 2000000133, 2000000186, 2000000239)
    result = build_package_text_overlay(package_resources[IMPLANT_RESOURCE], package_resources[SYSTEM_TEXT_RESOURCE], ids)
    for name, added in ((IMPLANT_RESOURCE, 20), (SYSTEM_TEXT_RESOURCE, 3)):
        old_raw, new_raw = decode(package_resources[name]), decode(result.replacements[name])
        old, new = parse_table(old_raw), parse_table(new_raw)
        assert new["record_count"] == old["record_count"] + added
        assert new["rows"][:old["record_count"]] == old["rows"]
        assert old_raw[old["data_offset"]:old["string_pool_offset"]] == new_raw[new["data_offset"]:old["string_pool_offset"]]
        assert old_raw[old["string_pool_offset"]:old["string_pool_offset"] + old["string_pool_size"]] == \
            new_raw[new["string_pool_offset"]:new["string_pool_offset"] + old["string_pool_size"]]
    assert result.text_indices[CAPTION_KEY] == 6433
    assert result.text_indices[DESCRIPTION_KEY] == 6434
    assert all(result.text_indices[confirmation_key(identifier)] == 6435 for identifier in ids)
    assert all(result.text_indices[caption_key(identifier)] == 6433 for identifier in ids)
    assert all(result.text_indices[description_key(identifier)] == 6434 for identifier in ids)
    json.dumps(result.metadata)


def test_reapply_is_idempotent_and_package_extension_only_adds_a_key():
    source = small_sources()
    first = build_package_text_overlay(*source, [local_package_id(1)])
    effective = [first.replacements[name] for name in (IMPLANT_RESOURCE, SYSTEM_TEXT_RESOURCE)]
    assert not build_package_text_overlay(*effective, [local_package_id(1)]).replacements
    next_result = build_package_text_overlay(*effective, [local_package_id(1), local_package_id(2)])
    assert set(next_result.replacements) == {IMPLANT_RESOURCE}
    assert next_result.text_indices[confirmation_key(local_package_id(2))] == 4


def test_base_table_growth_recalculates_physical_indices_without_changing_package_identity():
    implant, system = small_sources()
    old = build_package_text_overlay(implant, system, [local_package_id(1)])
    grown = append_table_rows(system, [system_row(2, "future official row")])
    new = build_package_text_overlay(implant, grown, [local_package_id(1)])
    assert old.package_ids == new.package_ids
    assert old.text_indices[CAPTION_KEY] == 2 and new.text_indices[CAPTION_KEY] == 3


@pytest.mark.parametrize("field,value", [("_ID", "LocalEventPassPackageName"), ("_TextID", -1), ("_TextID", 2)])
def test_conflicting_or_invalid_original_keys_fail(field, value):
    implant, system = small_sources()
    implant = patch_table_cells(implant, {0: {field: value}})
    with pytest.raises(ValueError):
        build_package_text_overlay(implant, system, [local_package_id(1)])


def test_text_or_index_tampering_is_not_silently_overwritten():
    first = build_package_text_overlay(*small_sources(), [local_package_id(1)])
    implant, system = [first.replacements[name] for name in (IMPLANT_RESOURCE, SYSTEM_TEXT_RESOURCE)]
    for changed in (patch_table_cells(system, {2: {"Data_Text": "different"}}),
                    patch_table_cells(system, {2: {"Recode_Index": 999}})):
        with pytest.raises(ValueError, match="conflicts"):
            build_package_text_overlay(implant, changed, [local_package_id(1)])


def test_existing_record_id_collision_is_rejected_without_overwriting():
    implant, system = small_sources()
    system = patch_table_cells(system, {0: {"Recode_Index": 2}})
    with pytest.raises(ValueError, match="collide"):
        build_package_text_overlay(implant, system, [local_package_id(1)])


def test_duplicate_names_and_hashes_are_rejected():
    implant, system = small_sources()
    implant = append_table_rows(implant, [{"_ID": "Original", "_TextID": 1}])
    with pytest.raises(ValueError, match="unique"):
        build_package_text_overlay(implant, system, [local_package_id(1)])


@pytest.mark.parametrize("ids", [[], {}, None, [2000000001, 2000000001], [True], [1], [0x80000000], [2000000001.0]])
def test_package_identity_validation(ids):
    with pytest.raises(ValueError):
        build_package_text_overlay(*small_sources(), ids)


@pytest.mark.parametrize("identifier", [0, -1, True, 1.0, "1", 147483648])
def test_local_package_identity_cannot_overflow(identifier):
    with pytest.raises(ValueError):
        local_package_id(identifier)


def test_result_and_metadata_are_independent_and_immutable():
    result = build_package_text_overlay(*small_sources(), [local_package_id(1)])
    with pytest.raises(TypeError):
        result.text_indices[CAPTION_KEY] = 0
    with pytest.raises(FrozenInstanceError):
        result.package_ids = ()
    metadata = result.metadata
    metadata["text_indices"][CAPTION_KEY] = 0
    assert result.text_indices[CAPTION_KEY] == 2


def test_every_new_text_is_explicitly_local_and_free():
    result = build_package_text_overlay(*small_sources(), [local_package_id(1)])
    rows = parse_table(result.replacements[SYSTEM_TEXT_RESOURCE])["rows"][-3:]
    assert [row["Data_Text"] for row in rows] == [CAPTION_TEXT, DESCRIPTION_TEXT, CONFIRMATION_TEXT]
    assert "ローカル" in CAPTION_TEXT and "無料" in DESCRIPTION_TEXT and "無料" in CONFIRMATION_TEXT


def test_unknown_table_schema_is_rejected():
    implant, system = small_sources()
    changed = bytearray(implant)
    changed[24:28] = b"nope"
    with pytest.raises(ValueError, match="schema"):
        build_package_text_overlay(bytes(changed), system, [local_package_id(1)])


def test_distinct_key_crc_collision_is_rejected(monkeypatch):
    original = module.zlib.crc32
    existing = original(b"Original")
    monkeypatch.setattr(module.zlib, "crc32", lambda value: existing if value == CAPTION_KEY.encode() else original(value))
    with pytest.raises(ValueError, match="collides"):
        build_package_text_overlay(*small_sources(), [local_package_id(1)])


def test_new_aliases_cannot_collide_with_each_other(monkeypatch):
    original = module.zlib.crc32
    monkeypatch.setattr(module.zlib, "crc32", lambda value: 123 if value in (CAPTION_KEY.encode(), DESCRIPTION_KEY.encode())
                        else original(value))
    with pytest.raises(ValueError, match="colliding"):
        build_package_text_overlay(*small_sources(), [local_package_id(1)])


def test_preexisting_confirmation_cannot_reuse_an_official_row():
    implant, system = small_sources()
    implant = append_table_rows(implant, [{"_ID": confirmation_key(local_package_id(1)), "_TextID": 0}])
    with pytest.raises(ValueError, match="different physical row"):
        build_package_text_overlay(implant, system, [local_package_id(1)])


def test_stale_local_indices_after_base_reordering_are_rejected():
    result = build_package_text_overlay(*small_sources(), [local_package_id(1)])
    implant = result.replacements[IMPLANT_RESOURCE]
    system = result.replacements[SYSTEM_TEXT_RESOURCE]
    rows = parse_table(system)["rows"]
    rows.insert(0, system_row(99, "new official row"))
    changed = tagged(rows, SYSTEM_SCHEMA, "sytx")
    with pytest.raises(ValueError, match="conflicts"):
        build_package_text_overlay(implant, changed, [local_package_id(1)])
