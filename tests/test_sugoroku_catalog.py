from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import struct

import lz4.frame
import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.possessions import MAX_ITEM_NAME_BYTES
from aigis_ps.resource_library import ResourceLibrary
import aigis_ps.sugoroku_catalog as module
from aigis_ps.sugoroku_catalog import FIELDS, RESOURCE_NAME, SugorokuCatalog
from test_present_items import item_table


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


def reward(identifier=1, kind=4, **updates):
    row = {"RewardID": identifier, "StartDateTime": "2020/04/01 04:00:00", "EndDateTime": "",
        "ConsumeCrystal": 10, "RewardType": kind, "GeneralItemID": "item" if kind == 1 else "",
        "CardID": 17 if kind == 2 else 0, "Count": 1}
    row.update(updates)
    return row


def catalog(rows=None, *, version=None, fields=None, payload=None):
    if payload is None:
        payload = item_table(rows if rows is not None else [reward()], fields or tuple(FIELDS.items()))
    return SugorokuCatalog(lambda name: payload if name == RESOURCE_NAME else b"", resource_version=version)


@pytest.fixture(scope="module")
def current():
    project = Path(__file__).resolve().parents[1]
    root = project / "runtime/resources"
    manifest, entries = ResourceLibrary(root, client_hash=
        CURRENT_CLIENT_SHA256).load()
    assets = IndexedAssetService(entries, root, project.parent / "aigisr_exe")
    return SugorokuCatalog(lambda name: assets.read(name)[0], resource_version=manifest["version"])


def test_real_catalog_retains_all_fifty_nine_records(current):
    assert len(current.entries) == 59
    assert len(current.by_id) == 59
    assert [entry.source_order for entry in current.entries] == list(range(59))
    assert current.status()["type_counts"] == {1: 17, 2: 27, 4: 3, 5: 9, 6: 3}
    assert current.status()["ready"] and current.status()["native_tested"] is False


def test_every_built_catalog_is_ready_without_building_its_status(current, monkeypatch):
    built = catalog()
    for case in (built, current):
        assert case.is_ready() is True and case.status()["ready"] is True
    monkeypatch.setattr(built, "status", lambda: pytest.fail("Readiness must not build the status report"))
    assert built.is_ready() is True


def test_current_active_rows_are_sorted_by_threshold_and_keep_replacement_ids(current):
    entries = current.active(NOW)
    assert len(entries) == 56
    assert not {26, 32, 41} & {entry.reward_id for entry in entries}
    assert {52, 53, 54} <= {entry.reward_id for entry in entries}
    assert [(entry.consume_crystal, entry.source_order) for entry in entries] == sorted(
        (entry.consume_crystal, entry.source_order) for entry in entries)
    assert entries[0].consume_crystal == 5
    assert entries[-1].consume_crystal == 800
    assert next(entry for entry in entries if entry.consume_crystal == 250).reward_id == 52


def test_real_type_four_is_unscaled_gold_count(current):
    entries = [entry for entry in current.entries if entry.reward_type == 4]
    assert [(entry.reward_id, entry.consume_crystal, entry.count) for entry in entries] == [
        (42, 520, 30000), (47, 620, 30000), (55, 720, 30000)]
    assert all(entry.supported and entry.item_id == "" and entry.card_id == 0 for entry in entries)


def test_current_item_tickets_are_not_replaced_with_other_rewards(current):
    assert (current.get(1).item_id, current.get(1).count) == ("item_smn_ticket_18003", 1)
    assert (current.get(10).item_id, current.get(10).count) == ("item_smn_ticket_18002", 1)
    assert current.get(1).reward_type == 1
    assert current.get(10).card_id == 0


def test_real_historical_end_is_exclusive_and_replacement_begin_inclusive(current):
    end = datetime(2023, 4, 1, 3, 59, 59, tzinfo=JST)
    begin = datetime(2023, 4, 1, 4, tzinfo=JST)
    for old, new in ((26, 52), (32, 53), (41, 54)):
        assert current.is_active(current.get(old), end - timedelta(microseconds=1))
        assert not current.is_active(current.get(old), end)
        assert not current.is_active(current.get(new), end)
        assert not current.is_active(current.get(new), begin - timedelta(microseconds=1))
        assert current.is_active(current.get(new), begin)


def test_same_threshold_multiple_rewards_preserve_source_order_not_id_order():
    case = catalog([reward(20, ConsumeCrystal=30), reward(9, ConsumeCrystal=10),
                    reward(3, ConsumeCrystal=10), reward(1, ConsumeCrystal=20)])
    assert [entry.reward_id for entry in case.entries] == [20, 9, 3, 1]
    assert [entry.reward_id for entry in case.active(NOW)] == [9, 3, 1, 20]


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 11, 1, 3, 59, 59, 999999, tzinfo=JST), False),
    (datetime(2026, 11, 1, 4, tzinfo=JST), True),
    (datetime(2026, 11, 1, 4, 59, 59, 999999, tzinfo=JST), True),
    (datetime(2026, 11, 1, 5, tzinfo=JST), False),
])
def test_window_uses_exact_instant(now, expected):
    case = catalog([reward(StartDateTime="2026/11/01 04:00:00", EndDateTime="2026/11/01 05:00:00")])
    assert case.is_active(case.get(1), now) is expected
    assert bool(case.active(now)) is expected


def test_utc_and_naive_jst_are_the_same_instant():
    case = catalog([reward(StartDateTime="2026-11-01 04:00:00", EndDateTime="2026-11-01 05:00:00")])
    local = datetime(2026, 11, 1, 4, 30, tzinfo=JST)
    assert case.active(local) == case.active(local.astimezone(timezone.utc)) == case.active(local.replace(tzinfo=None))


def test_empty_dates_are_unbounded_and_zero_threshold_is_valid():
    case = catalog([reward(StartDateTime="", EndDateTime="", ConsumeCrystal=0)])
    assert case.active(datetime(1, 1, 1, tzinfo=JST)) == case.entries
    assert case.active(datetime(9999, 12, 31, tzinfo=JST)) == case.entries


@pytest.mark.parametrize("kind,reason", [(3, "unverified_paid_free"),
    (6, "capacity_delivery_unverified"), (7, "reward_type_unsupported"), (0, "reward_type_unsupported")])
def test_unsupported_types_stay_in_active_metadata(kind, reason):
    case = catalog([reward(kind=kind, Count=30)])
    entry = case.get(1)
    assert entry.unsupported_reason == reason and not entry.supported
    assert case.active(NOW) == (entry,)
    assert case.status()["unsupported_rewards"] == {1: reason}


def test_type_five_declares_supported_local_capacity_delivery():
    case = catalog([reward(kind=5, Count=30)])
    entry = case.get(1)
    assert entry.supported and entry.unsupported_reason is None
    assert entry.count == 30 and entry.item_id == "" and entry.card_id == 0
    assert case.active(NOW) == (entry,)


def test_unknown_type_preserves_uninterpreted_identifiers_and_zero_count():
    case = catalog([reward(kind=20, GeneralItemID="unresolved", CardID=99, Count=0)])
    entry = case.get(1)
    assert (entry.item_id, entry.card_id, entry.count) == ("unresolved", 99, 0)
    assert entry.unsupported_reason == "reward_type_unsupported"


@pytest.mark.parametrize("kind", [1, 2, 3, 4, 5, 6])
def test_known_types_reject_zero_count(kind):
    with pytest.raises(ProtocolError) as error:
        catalog([reward(kind=kind, Count=0)])
    assert error.value.code == "sugoroku_catalog_invalid"


@pytest.mark.parametrize("kind,updates", [
    (1, {"GeneralItemID": ""}), (1, {"CardID": 9}), (2, {"CardID": 0}),
    (2, {"GeneralItemID": "unexpected"}), (3, {"CardID": 1}), (3, {"GeneralItemID": "x"}),
    (4, {"CardID": 1}), (4, {"GeneralItemID": "x"}), (5, {"CardID": 1}),
    (5, {"GeneralItemID": "x"}), (6, {"CardID": 1}), (6, {"GeneralItemID": "x"}),
])
def test_known_type_identifier_shapes_are_strict(kind, updates):
    with pytest.raises(ProtocolError) as error:
        catalog([reward(kind=kind, **updates)])
    assert error.value.code == "sugoroku_catalog_invalid"


@pytest.mark.parametrize("field,value", [("RewardID", 0), ("RewardID", -1), ("ConsumeCrystal", -1),
    ("RewardType", -1), ("CardID", -1), ("Count", -1)])
def test_negative_numeric_resource_values_are_invalid(field, value):
    with pytest.raises(ProtocolError):
        catalog([reward(**{field: value})])


@pytest.mark.parametrize("value", ["2026/11/01", "2026/11/01 24:00:00", "2026/02/29 00:00:00",
    "0000/01/01 00:00:00", "2026/11-01 00:00:00", "2026/11/01 00:00:00Z", "not a date"])
def test_malformed_dates_do_not_become_unbounded(value):
    with pytest.raises(ProtocolError) as error:
        catalog([reward(StartDateTime=value)])
    assert error.value.code == "sugoroku_catalog_invalid"


@pytest.mark.parametrize("end", ["2026/11/01 04:00:00", "2026/10/31 04:00:00"])
def test_empty_or_inverted_windows_rejected(end):
    with pytest.raises(ProtocolError):
        catalog([reward(StartDateTime="2026/11/01 04:00:00", EndDateTime=end)])


@pytest.mark.parametrize("rows", [[], [reward(), reward()]])
def test_empty_and_duplicate_reward_ids_rejected(rows):
    with pytest.raises(ProtocolError):
        catalog(rows)


@pytest.mark.parametrize("mutation", ["missing", "extra", "wrong_type"])
def test_schema_mismatch_is_not_ignored(mutation):
    fields = dict(FIELDS)
    row = reward()
    if mutation == "missing":
        fields.pop("Count")
    elif mutation == "extra":
        fields["Extra"] = 1
        row["Extra"] = 0
    else:
        fields["Count"] = 32
        row["Count"] = "1"
    with pytest.raises(ProtocolError) as error:
        catalog([row], fields=tuple(fields.items()))
    assert error.value.code == "sugoroku_catalog_invalid"


@pytest.mark.parametrize("field", ["RewardID", "ConsumeCrystal", "RewardType", "CardID", "Count"])
def test_boolean_never_aliases_integer(monkeypatch, field):
    parse = module.parse_table
    def patched(data, limits):
        result = parse(data, limits)
        result["rows"][0][field] = True
        return result
    monkeypatch.setattr(module, "parse_table", patched)
    with pytest.raises(ProtocolError):
        catalog()


@pytest.mark.parametrize("name", ["x" * (MAX_ITEM_NAME_BYTES + 1), "x\x01", "😀" * 64])
def test_item_name_byte_and_xml_limits(name):
    with pytest.raises(ProtocolError):
        catalog([reward(kind=1, GeneralItemID=name)])


def test_valid_maximum_item_name_preserved():
    name = "😀" * 63 + "xxx"
    assert len(name.encode("utf-8")) == MAX_ITEM_NAME_BYTES
    assert catalog([reward(kind=1, GeneralItemID=name)]).get(1).item_id == name


@pytest.mark.parametrize("identifier", [False, True, 0, -1, 2, 0x80000000, "1", None])
def test_get_requires_known_strict_integer_id(identifier):
    with pytest.raises(ProtocolError) as error:
        catalog().get(identifier)
    assert error.value.code == "sugoroku_reward_unknown"


@pytest.mark.parametrize("now", [None, "2026/11/01", 0, True])
def test_invalid_clock_rejected(now):
    case = catalog()
    with pytest.raises(ProtocolError) as error:
        case.active(now)
    assert error.value.code == "sugoroku_time_invalid"
    with pytest.raises(ProtocolError):
        case.is_active(case.get(1), now)


def test_foreign_or_modified_entry_rejected():
    case = catalog()
    assert case.is_active(replace(case.get(1)), NOW)
    for entry in (None, 1, replace(case.get(1), count=999)):
        with pytest.raises(ProtocolError) as error:
            case.is_active(entry, NOW)
        assert error.value.code == "sugoroku_reward_unknown"


def test_catalog_is_immutable_and_does_not_reread_mutating_source():
    rows = [reward()]
    payload = {RESOURCE_NAME: item_table(rows, tuple(FIELDS.items()))}
    case = SugorokuCatalog(payload.__getitem__)
    original = deepcopy(case.status())
    payload[RESOURCE_NAME] = b"changed"
    rows[0]["Count"] = 99
    assert case.get(1).count == 1 and case.status() == original
    with pytest.raises(FrozenInstanceError):
        case.get(1).count = 9
    with pytest.raises(TypeError):
        case.by_id[1] = None
    with pytest.raises(AttributeError):
        case.entries = ()
    report = case.status()
    report["type_counts"].clear()
    assert case.status() == original


def test_raw_and_all4_payloads_have_same_entries_but_distinct_source_fingerprints():
    raw = item_table([reward()], tuple(FIELDS.items()))
    packed = struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)
    first, second = catalog(payload=raw), catalog(payload=packed)
    assert first.entries == second.entries
    assert first.status()["decoded_sha256"] == second.status()["decoded_sha256"]
    assert first.resource_sha256 != second.resource_sha256
    assert first.content_version != second.content_version


def test_fingerprint_includes_payload_and_declared_version():
    first = catalog(version="a" * 40)
    assert first.content_version == catalog(version="a" * 40).content_version
    assert first.content_version != catalog(version="b" * 40).content_version
    assert first.content_version != catalog([reward(Count=2)], version="a" * 40).content_version
    assert first.resource_version == "a" * 40


@pytest.mark.parametrize("version", [True, 1, "", "v" * 129, "x\x01"])
def test_bad_versions_rejected(version):
    with pytest.raises(ProtocolError):
        catalog(version=version)


@pytest.mark.parametrize("payload", [b"", b"broken", bytearray(b"mutable"), None])
def test_invalid_payloads_rejected(payload):
    with pytest.raises(ProtocolError):
        SugorokuCatalog(lambda _: payload)


def test_oversized_resource_rejected_before_parsing(monkeypatch):
    monkeypatch.setattr(module, "RESOURCE_LIMIT", 1)
    with pytest.raises(ProtocolError):
        SugorokuCatalog(lambda _: b"xx")


def test_missing_resource_has_domain_error():
    with pytest.raises(ProtocolError) as error:
        SugorokuCatalog({}.__getitem__)
    assert error.value.code == "sugoroku_catalog_invalid"
