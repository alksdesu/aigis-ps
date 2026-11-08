from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
from pathlib import Path
import struct
import zlib

import lz4.frame
import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.errors import ProtocolError
from aigis_ps.possessions import STORED_ITEM_NAME_BYTES
import aigis_ps.present_recovery as module
from aigis_ps.present_recovery import (
    CONFIRMATION_NAME, CONFIRMATION_TEXT, EVENT_FIELDS, EXPLANATION_NAME, EXPLANATION_TEXT,
    ITEM_FIELDS, PresentRecoveryCatalog, PresentRecoveryDefinition,
)
from aigis_ps.resource_library import ResourceLibrary
from test_al_format import archive_bytes
from test_present_items import item_table


def native_hash(name):
    return (zlib.crc32(name.encode()) ^ 0x80000000) - 0x80000000


def item(name="food", **changes):
    row = {"ItemID": name, "ID": native_hash(name), "EffectType": 11, "Stack": 0,
           "ReceiveAction": 0, "InvisibleContaint": 1, "CanAllGet": 0,
           "ReceiveConfirmation": native_hash(CONFIRMATION_NAME), "EventID": "",
           "TextIDExplanation": 0}
    row.update(changes)
    return row


def command(name="DoPeruseSavePresent", **changes):
    row = {key: 0 if kind == 1 else "" for key, kind in EVENT_FIELDS.items()}
    row["_CommandName"] = name
    row.update(changes)
    return row


def event_archive(rows=None, *, name="evFood"):
    payload = item_table([command()] if rows is None else rows, tuple(EVENT_FIELDS.items()))
    return archive_bytes(archive_bytes(payload, b"_evseq.atb"), f"_{name}.aar".encode())


def resources(rows=None, *, commands=None):
    return {
        "ItemConfigTable.atb": item_table([item()] if rows is None else rows, tuple(ITEM_FIELDS.items())),
        "cfgImplantText.atb": item_table([
            {"_ID": CONFIRMATION_NAME, "_TextID": 1207},
            {"_ID": EXPLANATION_NAME, "_TextID": 6189},
        ], (("_ID", 32), ("_TextID", 1))),
        "SystemText.atb": item_table([
            {"Recode_Index": 1207, "Data_Text": CONFIRMATION_TEXT},
            {"Recode_Index": 6189, "Data_Text": EXPLANATION_TEXT},
        ], (("Recode_Index", 1), ("Data_Text", 32))),
        "EventHomeArc.aar": event_archive(commands),
    }


@pytest.fixture(scope="module")
def current():
    root = Path(__file__).resolve().parents[2]
    resource_root = root / "aigis_ps/runtime/resources"
    _, entries = ResourceLibrary(resource_root, client_hash=
        CURRENT_CLIENT_SHA256).load()
    reader = IndexedAssetService(entries, resource_root, root / "aigisr_exe")
    catalog = PresentRecoveryCatalog(lambda name: reader.read(name)[0])
    payload = reader.read("ItemConfigTable.atb")[0]
    rows = parse_table(decompress_all4(payload) if payload.startswith(b"ALL4") else payload)["rows"]
    return catalog, rows


def test_current_resource_scope_includes_all_normal_and_event_food(current):
    catalog, rows = current
    accepted = []
    for row in rows:
        if row["EffectType"] == 11:
            assert catalog.definition(22, 1, row["ItemID"]) == PresentRecoveryDefinition(row["ItemID"], row["EventID"])
            assert catalog.supports_item(row["ItemID"])
            accepted.append(row)
        else:
            assert not catalog.supports_item(row["ItemID"])
            with pytest.raises(ProtocolError) as error:
                catalog.definition(22, 1, row["ItemID"])
            assert error.value.code == "present_recovery_unsupported"
    status = catalog.status()
    assert len(accepted) == status["item_count"] == status["recognized_recovery_count"] == 172
    assert status["no_event_item_count"] == 114
    assert status["event_item_count"] == 58
    assert status["event_count"] == 54
    assert status["can_all_get"] is False and status["pev"] == "unchanged"
    assert set(status["resource_hashes"]) == {"ItemConfigTable.atb", "cfgImplantText.atb", "SystemText.atb", "EventHomeArc.aar"}


def test_type8_zero_and_content_is_never_a_multiplier():
    catalog = PresentRecoveryCatalog(resources().__getitem__)
    assert catalog.definition(8, 0) == PresentRecoveryDefinition(None)
    assert catalog.definition(22, 1, "food") == catalog.definition(22, 0x7FFFFFFF, "food")
    assert not hasattr(catalog.definition(22, 9, "food"), "amount")
    assert catalog.status()["quantity"] == "one_current_maximum_per_mail"
    assert catalog.status()["recovery_limit"] == 2000


@pytest.mark.parametrize("kind", [True, False, 0, 1, 9, 11, 23, -1, 22.0, "22", None])
def test_unsupported_types_are_explicit(kind):
    with pytest.raises(ProtocolError) as error:
        PresentRecoveryCatalog(resources().__getitem__).definition(kind, 1, "food")
    assert error.value.code == "present_recovery_unsupported" and error.value.status == 501


@pytest.mark.parametrize("kind,content,item_id", [
    (8, 1, None), (8, 10000, None), (8, 0, "food"), (8, 0, ""),
    (22, 1, "unknown"), (22, 1, None), (22, 1, []),
])
def test_unsupported_content_or_identity_is_not_downgraded(kind, content, item_id):
    with pytest.raises(ProtocolError) as error:
        PresentRecoveryCatalog(resources().__getitem__).definition(kind, content, item_id)
    assert error.value.code == "present_recovery_unsupported"


@pytest.mark.parametrize("content", [True, False, -1, 0x80000000, 1.0, "1", None])
@pytest.mark.parametrize("kind", [8, 22])
def test_content_requires_native_integer(kind, content):
    with pytest.raises(ProtocolError) as error:
        PresentRecoveryCatalog(resources().__getitem__).definition(kind, content, "food" if kind == 22 else None)
    assert error.value.code == "present_recovery_content_invalid" and error.value.status == 400


def test_food_zero_is_an_explicit_local_metadata_boundary():
    with pytest.raises(ProtocolError) as error:
        PresentRecoveryCatalog(resources().__getitem__).definition(22, 0, "food")
    assert error.value.code == "present_recovery_content_invalid"


@pytest.mark.parametrize("changes", [
    {"EffectType": 1}, {"EffectType": 13}, {"Stack": 1}, {"ReceiveAction": 3},
    {"InvisibleContaint": 0}, {"CanAllGet": 1}, {"ReceiveConfirmation": 0},
    {"ReceiveConfirmation": 123}, {"TextIDExplanation": 123},
])
def test_new_rules_are_recognized_but_not_silently_supported(changes):
    catalog = PresentRecoveryCatalog(resources([item(**changes)]).__getitem__)
    assert not catalog.supports_item("food")
    with pytest.raises(ProtocolError) as error:
        catalog.definition(22, 1, "food")
    assert error.value.code == "present_recovery_unsupported"


@pytest.mark.parametrize("rows", [
    [], [item(), item()], [item("plumless"), item("buckeroo")], [item(ID=5)],
    [item(EffectType=-1)], [item(Stack=-1)], [item(CanAllGet=-1)],
    [item(ReceiveAction=-1)], [item(InvisibleContaint=-1)], [item("bad\x01name")],
    [item("x" * 257)], [item(EventID="bad\x01name")], [item(EventID="x" * 513)],
])
def test_invalid_item_definitions_fail_closed(rows):
    with pytest.raises(ProtocolError) as error:
        PresentRecoveryCatalog(resources(rows).__getitem__)
    assert error.value.code == "present_recovery_catalog_invalid" and error.value.status == 503


@pytest.mark.parametrize("resource", ["ItemConfigTable.atb", "cfgImplantText.atb", "SystemText.atb"])
@pytest.mark.parametrize("payload", [b"", b"invalid", None, bytearray(b"ALTB")])
def test_invalid_or_mutable_resources_are_rejected(resource, payload):
    data = resources()
    data[resource] = payload
    with pytest.raises(ProtocolError) as error:
        PresentRecoveryCatalog(data.__getitem__)
    assert error.value.code == "present_recovery_catalog_invalid"


@pytest.mark.parametrize("resource,rows,schema", [
    ("cfgImplantText.atb", [{"_ID": CONFIRMATION_NAME, "_TextID": 1207}], (("_ID", 32), ("_TextID", 1))),
    ("cfgImplantText.atb", [{"_ID": "plumless", "_TextID": 1207}, {"_ID": "buckeroo", "_TextID": 6189}], (("_ID", 32), ("_TextID", 1))),
    ("SystemText.atb", [{"Recode_Index": 1207, "Data_Text": "Changed recovery amount"}], (("Recode_Index", 1), ("Data_Text", 32))),
    ("SystemText.atb", [{"Recode_Index": 1207, "Data_Text": CONFIRMATION_TEXT}] * 2, (("Recode_Index", 1), ("Data_Text", 32))),
])
def test_text_identity_and_explanation_chain_cannot_drift(resource, rows, schema):
    data = resources()
    data[resource] = item_table(rows, schema)
    with pytest.raises(ProtocolError) as error:
        PresentRecoveryCatalog(data.__getitem__)
    assert error.value.code == "present_recovery_catalog_invalid"


@pytest.mark.parametrize("rows", [
    [], [command("DoPeruseSave")], [command("DoPeruseSaveEpisode")],
    [command("DoUnknownReward")], [command(), command()],
    [command(), command("WaitFrame")], [command(_CmndParam="Save=other")],
    [command(_Param1="1")], [command(_SceneID=1)],
    [command(_AfterExpression="DoPeruseSave();")],
])
def test_event_requires_exactly_one_unmodified_final_present_claim(rows):
    data = resources([item(EventID="evFood")], commands=rows)
    with pytest.raises(ProtocolError) as error:
        PresentRecoveryCatalog(data.__getitem__)
    assert error.value.code == "present_recovery_catalog_invalid"


def test_event_uses_named_package_and_retains_event_identity_without_pev():
    data = resources([item(EventID="evFood"), item("food2", EventID="evFood")],
                     commands=[command("CreateChara", _AfterExpression="DoAttachImageOfs();"), command()])
    catalog = PresentRecoveryCatalog(data.__getitem__)
    assert catalog.definition(22, 1, "food") == PresentRecoveryDefinition("food", "evFood", False)
    assert catalog.status()["event_count"] == 1
    assert catalog.status()["event_item_count"] == 2
    data["EventHomeArc.aar"] = event_archive(name="different")
    with pytest.raises(ProtocolError):
        PresentRecoveryCatalog(data.__getitem__)


def test_no_event_catalog_does_not_require_unrelated_event_archive():
    data = resources()
    del data["EventHomeArc.aar"]
    catalog = PresentRecoveryCatalog(data.__getitem__)
    assert catalog.status()["event_count"] == 0
    assert "EventHomeArc.aar" not in catalog.status()["resource_hashes"]


def test_unrelated_system_text_groups_may_repeat_their_record_index():
    data = resources()
    rows = parse_table(data["SystemText.atb"])["rows"]
    rows.extend([{"Recode_Index": 0, "Data_Text": "first"}, {"Recode_Index": 0, "Data_Text": "second"}])
    data["SystemText.atb"] = item_table(rows, (("Recode_Index", 1), ("Data_Text", 32)))
    assert PresentRecoveryCatalog(data.__getitem__).supports_item("food")


def test_missing_required_event_archive_is_not_a_normal_food_fallback():
    data = resources([item(EventID="evFood")])
    del data["EventHomeArc.aar"]
    with pytest.raises(ProtocolError) as error:
        PresentRecoveryCatalog(data.__getitem__)
    assert error.value.code == "present_recovery_catalog_invalid"


def test_catalog_is_frozen_from_reader_and_status_mutation():
    data = resources()
    before = deepcopy(data)
    catalog = PresentRecoveryCatalog(data.__getitem__)
    snapshot = catalog.status()
    assert data == before
    with pytest.raises(FrozenInstanceError):
        catalog.definition(22, 1, "food").can_all_get = True
    with pytest.raises(TypeError):
        catalog._rows["food"]["Stack"] = 1
    data["ItemConfigTable.atb"] = b"changed"
    status = catalog.status()
    status["resource_hashes"].clear()
    status["supported_types"].append(9)
    assert catalog.status() == snapshot
    assert catalog.supports_item("food") and not catalog.supports_item([])


def test_every_built_catalog_is_ready_without_reading_its_resources_again():
    data = resources()
    catalog = PresentRecoveryCatalog(data.__getitem__)
    data.clear()
    assert catalog.is_ready() is True
    assert catalog.status()["ready"] is catalog.is_ready()


def test_compression_has_the_same_decoded_identity_and_other_content_changes_version():
    data = resources()
    first = PresentRecoveryCatalog(data.__getitem__)
    raw = data["ItemConfigTable.atb"]
    data["ItemConfigTable.atb"] = struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)
    assert PresentRecoveryCatalog(data.__getitem__).content_version == first.content_version
    changed = resources([item(), item("unused", EffectType=13)])
    second = PresentRecoveryCatalog(changed.__getitem__)
    assert second.content_version != first.content_version
    assert first.status()["resource_hashes"]["ItemConfigTable.atb"] == hashlib.sha256(raw).hexdigest()


def test_event_payload_is_included_in_content_identity():
    data = resources([item(EventID="evFood")])
    first = PresentRecoveryCatalog(data.__getitem__)
    data["EventHomeArc.aar"] = event_archive([command("WaitFrame", _Param1="60"), command()])
    second = PresentRecoveryCatalog(data.__getitem__)
    assert first.definition(22, 1, "food") == second.definition(22, 1, "food")
    assert first.content_version != second.content_version


def test_resource_size_bound_checked_before_parser(monkeypatch):
    monkeypatch.setattr(module, "RESOURCE_LIMIT", 3)
    with pytest.raises(ProtocolError) as error:
        PresentRecoveryCatalog(lambda name: b"ALTB")
    assert error.value.code == "present_recovery_catalog_invalid"


def test_overlapping_archive_members_cannot_expand_past_total_budget(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(module, "RESOURCE_LIMIT", 100)
    monkeypatch.setattr(module, "parse_archive", lambda *_: SimpleNamespace(members=(
        SimpleNamespace(name="one", offset=0, size=60),
        SimpleNamespace(name="two", offset=0, size=60),
    )))
    with pytest.raises(ValueError, match="total byte limit"):
        module._archive(b"x" * 80)


@pytest.mark.parametrize("field,value", [("ID", True), ("Stack", False), ("EffectType", 11.0),
                                        ("EventID", None), ("ReceiveConfirmation", True)])
def test_decoded_type_errors_do_not_pass_integer_or_string_checks(monkeypatch, field, value):
    original = module.parse_table
    data = resources()

    def parsed(payload, limits):
        result = original(payload, limits)
        if payload == data["ItemConfigTable.atb"]:
            result["rows"][0][field] = value
        return result

    monkeypatch.setattr(module, "parse_table", parsed)
    with pytest.raises(ProtocolError) as error:
        PresentRecoveryCatalog(data.__getitem__)
    assert error.value.code == "present_recovery_catalog_invalid"


@pytest.mark.parametrize(("event_id", "member", "opened"), [
    ("ev.Food", b"_ev.Food", True), ("ev.Food", b"_ev.Food.aar", False),
    ("evFood", b"_evFood.aar", True), ("evFood", b"_evFood", False),
])
def test_event_package_name_follows_the_client_suffix_rule(event_id, member, opened):
    payload = item_table([command()], tuple(EVENT_FIELDS.items()))
    data = resources([item(EventID=event_id)])
    data["EventHomeArc.aar"] = archive_bytes(archive_bytes(payload, b"_evseq.atb"), member)
    if opened:
        catalog = PresentRecoveryCatalog(data.__getitem__)
        assert catalog.definition(22, 1, "food") == PresentRecoveryDefinition("food", event_id, False)
        assert catalog.status()["event_count"] == 1
        return
    with pytest.raises(ProtocolError) as error:
        PresentRecoveryCatalog(data.__getitem__)
    assert error.value.code == "present_recovery_catalog_invalid"


def test_item_names_keep_the_earlier_stored_bound():
    stored = "f" * STORED_ITEM_NAME_BYTES
    assert PresentRecoveryCatalog(resources([item(stored)]).__getitem__).supports_item(stored)
    with pytest.raises(ProtocolError) as error:
        PresentRecoveryCatalog(resources([item(stored + "f")]).__getitem__)
    assert error.value.code == "present_recovery_catalog_invalid"
