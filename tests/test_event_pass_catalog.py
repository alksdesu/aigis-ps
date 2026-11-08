from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path
import struct
import zlib

import lz4.frame
import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.possessions import STORED_ITEM_NAME_BYTES, PossessionService
from aigis_ps.resource_library import ResourceLibrary
import aigis_ps.event_pass_catalog as module
from aigis_ps.event_pass_catalog import EventPassCatalog, FIELDS, ITEM_FIELDS, ITEM_RESOURCE_NAME, RESOURCE_NAME
from test_present_items import item_table


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


def item(name="normal", premium=False, **updates):
    row = {key: "" if kind == 32 else 0 for key, kind in ITEM_FIELDS.items()}
    row.update(ItemID=name, ID=(zlib.crc32(name.encode()) ^ 0x80000000) - 0x80000000,
               Category=module.CATEGORY, EffectType=14 if premium else 0, Stack=1 if premium else 999)
    row.update(updates)
    return row


def reward(identifier=1, **updates):
    row = {key: "" if kind == 32 else 0 for key, kind in FIELDS.items()}
    row.update(ID=identifier, StartDate="2026/11/01 10:00:00", EndDate="2026/11/01 14:00:00",
               TimeLimit="2026/11/01 11:00", Status=1, RequiredPoint=10,
               NormalPassItemID="normal", RewardItemType=0, RewardItemParam=1)
    row.update(updates)
    return row


def continuation(identifier=2, **updates):
    return reward(identifier, **({"StartDate": "", "EndDate": "", "TimeLimit": "", "Status": 0,
        "RequiredPoint": 0, "NormalPassItemID": ""} | updates))


def payloads(rows=None, items=None):
    return {RESOURCE_NAME: item_table(rows if rows is not None else [reward()], tuple(FIELDS.items())),
            ITEM_RESOURCE_NAME: item_table(items if items is not None else [item(), item("premium", True),
                item("prize", Stack=100)], tuple(ITEM_FIELDS.items()))}


def catalog(rows=None, items=None, *, possessions=None):
    data = payloads(rows, items)
    return EventPassCatalog(data.__getitem__, possessions)


@pytest.fixture(scope="module")
def current():
    project = Path(__file__).resolve().parents[1]
    root = project / "runtime/resources"
    _, entries = ResourceLibrary(root, client_hash=
        CURRENT_CLIENT_SHA256).load()
    assets = IndexedAssetService(entries, root, project.parent / "aigisr_exe")
    reader = lambda name: assets.read(name)[0]
    possessions = PossessionService(project / "runtime/assets", asset_reader=reader)
    return EventPassCatalog(reader, possessions)


def test_actual_all_six_passes_and_264_rows_are_retained(current):
    assert len(current.passes) == len(current.by_id) == 6
    assert [len(entry.rewards) for entry in current.passes] == [53, 53, 26, 53, 53, 26]
    rows = [row for entry in current.passes for row in entry.rewards]
    assert [row.reward_id for row in rows] == list(range(1, 265))
    assert [row.source_index for row in rows] == list(range(264))
    assert [sum(row.premium for row in entry.rewards) for entry in current.passes] == [27, 27, 13, 27, 27, 13]
    assert [max(row.required_point for row in entry.rewards) for entry in current.passes] == [200, 200, 100, 200, 200, 100]


def test_actual_resources_are_closed_even_inside_their_date_windows(current):
    assert all(entry.status == 2 for entry in current.passes)
    assert current.eligible(NOW) == ()
    assert all(not current.is_eligible(entry, entry.start_at) for entry in current.passes)
    assert current.status()["status_counts"] == {2: 6}
    assert current.status()["native_tested"] is False


def test_actual_reward_metadata_does_not_substitute_unenabled_ticket_pools(current):
    items = {row.item_id for entry in current.passes for row in entry.rewards if row.reward_item_type == 22}
    assert {"item_smn_ticket_19144", "item_smn_ticket_85142", "item_cook_logbo_00004"} <= items
    assert all(entry.premium_item_id == entry.item_id.replace("event_pass_", "event_pass_premium_")
               for entry in current.passes)


def test_continuations_inherit_fields_but_premium_flag_does_not_carry():
    case = catalog([reward(), continuation(PremiumPassItemID="premium"), continuation(3),
                    continuation(4, RequiredPoint=20), continuation(5, PremiumPassItemID="premium")])
    entry = case.get("normal")
    assert [row.required_point for row in entry.rewards] == [10, 10, 10, 20, 20]
    assert [row.premium for row in entry.rewards] == [False, True, False, False, True]
    assert entry.time_limit == "2026/11/01 11:00"


def test_new_groups_use_actual_inherited_values_and_keep_source_order():
    case = catalog([reward(8), continuation(3, NormalPassItemID="other", RequiredPoint=20)],
                   [item(), item("other")])
    assert [entry.item_id for entry in case.passes] == ["normal", "other"]
    assert case.get("other").start_at == case.get("normal").start_at
    assert case.get("other").rewards[0].reward_id == 3
    assert case.get("other").rewards[0].source_index == 1


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 11, 1, 9, 59, 59, 999999, tzinfo=JST), False),
    (datetime(2026, 11, 1, 10, tzinfo=JST), True),
    (datetime(2026, 11, 1, 14, tzinfo=JST), True),
    (datetime(2026, 11, 1, 14, 0, 0, 999999, tzinfo=JST), True),
    (datetime(2026, 11, 1, 14, 0, 1, tzinfo=JST), False),
])
def test_both_availability_boundaries_are_inclusive(now, expected):
    case = catalog()
    assert case.is_eligible(case.get("normal"), now) is expected
    assert bool(case.eligible(now)) is expected


def test_time_limit_does_not_gate_claims_and_aware_clock_keeps_instant():
    case = catalog()
    assert case.eligible(NOW)
    assert case.eligible(NOW) == case.eligible(NOW.astimezone(timezone.utc)) == case.eligible(NOW.replace(tzinfo=None))


@pytest.mark.parametrize("text", ["2026/11/1 10:00", "2026-11-01 10:00:00", "2026/11/01 10:00:00"])
def test_minute_second_and_unpadded_native_dates(text):
    assert catalog([reward(StartDate=text)]).get("normal").start_at == datetime(2026, 11, 1, 10, tzinfo=JST)


def test_inclusive_zero_duration_and_empty_display_limit_are_valid():
    case = catalog([reward(EndDate="2026/11/01 10:00:00", TimeLimit="")])
    assert case.eligible(datetime(2026, 11, 1, 10, tzinfo=JST))
    assert case.get("normal").time_limit == ""


@pytest.mark.parametrize("field", ["StartDate", "EndDate"])
@pytest.mark.parametrize("text", ["2026/11/01", "2026/02/30 10:00", "2026/11/01 24:00",
                                    "2026/11-01 10:00", "2026/11/01 10:00Z"])
def test_malformed_dates_are_not_silently_unbounded(field, text):
    with pytest.raises(ProtocolError, match="verified contract") as error:
        catalog([reward(**{field: text})])
    assert error.value.code == "event_pass_catalog_invalid" and error.value.status == 503


@pytest.mark.parametrize("text", ["メンテナンス開始まで", "  次回更新まで\n予定はお知らせを参照  ",
                                  "2026/11/01", "2026/02/30 10:00", "2026/11/01 24:00"])
def test_time_limit_is_verbatim_display_text_with_empty_continuation_inheritance(text):
    case = catalog([reward(TimeLimit=text), continuation(),
                    continuation(3, NormalPassItemID="other")], [item(), item("other")])
    assert all(entry.time_limit == text for entry in case.passes)
    assert case.eligible(NOW) == case.passes
    assert all(case.is_eligible(entry, NOW) for entry in case.passes)


def test_time_limit_display_text_has_a_utf8_byte_limit():
    text = "界" * (module.MAX_TIME_LIMIT_BYTES // 3) + "x" * (module.MAX_TIME_LIMIT_BYTES % 3)
    assert catalog([reward(TimeLimit=text)]).get("normal").time_limit == text
    with pytest.raises(ProtocolError) as error:
        catalog([reward(TimeLimit=text + "x")])
    assert error.value.code == "event_pass_catalog_invalid"


def test_real_time_limit_is_preserved_without_datetime_normalization(current):
    assert [entry.time_limit for entry in current.passes] == ["2026/6/25 11:00", "2026/7/16 11:00",
        "2026/7/23 11:00", "2026/08/06 11:00", "2026/08/20 11:00", "2026/10/22 11:00"]


@pytest.mark.parametrize("updates", [{"StartDate": ""}, {"EndDate": ""}, {"Status": 0},
    {"RequiredPoint": 0}, {"EndDate": "2026/10/01 10:00"}, {"NormalPassItemID": ""}])
def test_missing_initial_context_is_rejected(updates):
    with pytest.raises(ProtocolError):
        catalog([reward(**updates)])


@pytest.mark.parametrize("updates", [{"StartDate": "2026/11/01 11:00"}, {"EndDate": "2026/11/01 15:00"},
    {"TimeLimit": "2026/11/01 13:00"}, {"Status": 2}])
def test_mixed_availability_within_one_group_is_explicitly_unsupported(updates):
    with pytest.raises(ProtocolError):
        catalog([reward(), continuation(**updates)])


@pytest.mark.parametrize("field,value", [("ID", 0), ("ID", -1), ("RequiredPoint", -1),
    ("Status", -1), ("RewardItemType", -1), ("RewardItemParam", -1)])
def test_negative_and_sentinel_identifiers_are_rejected(field, value):
    with pytest.raises(ProtocolError):
        catalog([reward(**{field: value})])


@pytest.mark.parametrize("kind,param,item_id", [(0, 0, ""), (1, 0, ""), (2, 0, ""),
    (0, 1, "prize"), (1, 1, "prize"), (2, 1, "prize"), (22, 0, "prize"), (22, 1, ""), (22, 1, "missing"), (17, 1, "")])
def test_unknown_reward_shapes_do_not_enter_supported_paths(kind, param, item_id):
    with pytest.raises(ProtocolError):
        catalog([reward(RewardItemType=kind, RewardItemParam=param, RewardItemID=item_id)])


def test_item_parameter_is_preserved_without_changing_mail_amount():
    row = catalog([reward(RewardItemType=22, RewardItemParam=4, RewardItemID="prize")]).get("normal").rewards[0]
    assert row.param == row.reward_item_param == 4
    assert row.item_id == row.reward_item_id == "prize"


def test_item_names_share_the_stored_bound_of_possessions(tmp_path):
    normal, premium, prize = (prefix * STORED_ITEM_NAME_BYTES for prefix in "npr")
    data = payloads([reward(NormalPassItemID=normal, PremiumPassItemID=premium, RewardItemType=22,
                            RewardItemID=prize)], [item(normal), item(premium, True), item(prize, Stack=100)])
    case = EventPassCatalog(data.__getitem__, PossessionService(tmp_path, asset_reader=data.__getitem__))
    entry = case.get(normal)
    assert (entry.item_id, entry.premium_item_id, entry.rewards[0].item_id) == (normal, premium, prize)
    assert case.status()["ready"] and case.eligible(NOW) == (entry,)


@pytest.mark.parametrize("updates,extra", [
    ({"NormalPassItemID": "y" * (STORED_ITEM_NAME_BYTES + 1)}, ()),
    ({"PremiumPassItemID": "y" * (STORED_ITEM_NAME_BYTES + 1)}, ()),
    ({"RewardItemType": 22, "RewardItemID": "y" * (STORED_ITEM_NAME_BYTES + 1)}, ()),
    ({}, (item("y" * (STORED_ITEM_NAME_BYTES + 1)),)),
], ids=["normal", "premium", "reward", "item"])
def test_item_names_past_the_stored_bound_fail(updates, extra):
    with pytest.raises(ProtocolError) as error:
        catalog([reward(**updates)], [item(), item("premium", True), item("prize", Stack=100), *extra])
    assert error.value.code == "event_pass_catalog_invalid"


@pytest.mark.parametrize("rows", [[], [reward(), continuation(1)], [reward(), reward(2)]])
def test_empty_or_duplicate_reward_and_group_identities_fail(rows):
    with pytest.raises(ProtocolError):
        catalog(rows)


def test_conflicting_premium_item_per_group_and_shared_premium_groups_fail():
    items = [item(), item("other"), item("premium", True), item("premium2", True)]
    for rows in ([reward(PremiumPassItemID="premium"), continuation(PremiumPassItemID="premium2")],
                 [reward(PremiumPassItemID="premium"), reward(2, NormalPassItemID="other", PremiumPassItemID="premium")]):
        with pytest.raises(ProtocolError):
            catalog(rows, items)


@pytest.mark.parametrize("premium,field,value", [(False, "Stack", 998), (True, "Stack", 2),
    (False, "EffectType", 14), (True, "EffectType", 0), (False, "Category", 0),
    (False, "CanPresent", 1), (False, "CanAllGet", 1), (True, "ReceiveAction", 1),
    (True, "NoAutoDelete", 1), (False, "ReceiveConfirmation", 1), (False, "InvisibleContaint", 1),
    (True, "SummonCount", 1), (True, "SummonConfigID", 1), (False, "LotteryRuleID", 1),
    (True, "EventID", "event")])
def test_pass_inventory_contract_is_strict(premium, field, value):
    rows = [item(), item("premium", True)]
    rows[int(premium)][field] = value
    with pytest.raises(ProtocolError):
        catalog([reward(PremiumPassItemID="premium")], rows)


@pytest.mark.parametrize("items", [[item(ID=1)], [item(), item()], []])
def test_inventory_identity_crc_duplicates_and_empty_table_fail(items):
    with pytest.raises(ProtocolError):
        catalog(items=items)


@pytest.mark.parametrize("resource", [RESOURCE_NAME, ITEM_RESOURCE_NAME])
@pytest.mark.parametrize("mutation", ["missing", "wrong_type", "duplicate"])
def test_schema_mismatches_fail(resource, mutation):
    data = payloads()
    fields = list((FIELDS if resource == RESOURCE_NAME else ITEM_FIELDS).items())
    if mutation == "missing":
        fields.pop()
    elif mutation == "wrong_type":
        fields[0] = (fields[0][0], 32 if fields[0][1] == 1 else 1)
    else:
        fields.append(fields[0])
    rows = [reward()] if resource == RESOURCE_NAME else [item()]
    if mutation == "wrong_type":
        rows[0][fields[0][0]] = "1" if fields[0][1] == 32 else 1
    data[resource] = item_table(rows, tuple(fields))
    with pytest.raises(ProtocolError):
        EventPassCatalog(data.__getitem__)


def test_native_numeric_bool_is_not_accepted_after_parsing(monkeypatch):
    original = module.parse_table
    def parsed(data, limits):
        result = original(data, limits)
        if "NormalPassItemID" in result["rows"][0]:
            result["rows"][0]["Status"] = True
        return result
    monkeypatch.setattr(module, "parse_table", parsed)
    with pytest.raises(ProtocolError):
        catalog()


def test_catalog_is_immutable_and_fingerprint_tracks_both_resources():
    case = catalog()
    with pytest.raises(TypeError):
        case.by_id["other"] = case.get("normal")
    with pytest.raises(FrozenInstanceError):
        case.get("normal").status = 2
    with pytest.raises(FrozenInstanceError):
        case.get("normal").rewards[0].param = 2
    with pytest.raises(TypeError):
        case.resource_hashes[RESOURCE_NAME] = "changed"
    assert case.content_version == catalog().content_version
    assert case.content_version != catalog([reward(RewardItemParam=2)]).content_version
    assert case.content_version != catalog(items=[item(), item("unused")]).content_version


def test_all4_wrapping_does_not_change_decoded_identity():
    data = payloads()
    plain = EventPassCatalog(data.__getitem__)
    wrapped = {name: struct.pack("<4sHHI", b"ALL4", 1, 0, len(value)) + lz4.frame.compress(value)
               for name, value in data.items()}
    assert EventPassCatalog(wrapped.__getitem__).content_version == plain.content_version


def test_optional_possession_dependency_requires_identical_source_and_stacks(tmp_path):
    data = payloads()
    possessions = PossessionService(tmp_path, asset_reader=data.__getitem__)
    case = EventPassCatalog(data.__getitem__, possessions)
    assert case.status()["ready"]
    possessions._definitions["normal"] = 998
    with pytest.raises(ProtocolError) as error:
        case.eligible(NOW)
    assert error.value.code == "event_pass_catalog_changed"
    possessions._definitions["normal"] = 999
    possessions.asset_sha256 = "0" * 64
    with pytest.raises(ProtocolError):
        EventPassCatalog(data.__getitem__, possessions)


def test_readiness_never_builds_the_possession_status(tmp_path, monkeypatch):
    data = payloads()
    possessions = PossessionService(tmp_path, asset_reader=data.__getitem__)
    case = EventPassCatalog(data.__getitem__, possessions)
    monkeypatch.setattr(possessions, "status", lambda: pytest.fail("readiness must not build the possession status"))
    assert case.status()["ready"] is True and case.eligible(NOW) == case.passes
    monkeypatch.setattr(possessions, "issues", {"item_definitions": "unavailable"})
    for check in (case.status, lambda: case.eligible(NOW), lambda: case.get("normal")):
        with pytest.raises(ProtocolError) as error:
            check()
        assert (error.value.code, error.value.status) == ("event_pass_catalog_changed", 503)


@pytest.mark.parametrize("identifier", [None, 1, [], "missing"])
def test_unknown_group_is_explicit(identifier):
    with pytest.raises(ProtocolError) as error:
        catalog().get(identifier)
    assert error.value.code == "event_pass_unknown"


def test_foreign_definition_is_rejected():
    case = catalog()
    with pytest.raises(ProtocolError):
        case.is_eligible(replace(case.get("normal"), status=2), NOW)


@pytest.mark.parametrize("value", [None, bytearray(b"ALTB"), b"", b"not a table"])
def test_invalid_resource_payload_fails(value):
    data = payloads()
    data[RESOURCE_NAME] = value
    with pytest.raises(ProtocolError) as error:
        EventPassCatalog(data.__getitem__)
    assert error.value.status == 503
