from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path

import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.energy_clock import JST
from aigis_ps.event_pass_catalog import EventPassCatalog
from aigis_ps.event_pass_revival import EventPassRevivalRules, build_event_pass_revival, PASS_RESOURCE, MISSION_RESOURCE
from aigis_ps.mission_catalog import MissionCatalog
from aigis_ps.possessions import STORED_ITEM_NAME_BYTES
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.table_cell_patch import patch_table_cells


PASS_ID = "event_pass_20261015"
POINTS = {1611: 2, 1607: 4, 1608: 4, 1609: 4, 1610: 6}


@pytest.fixture(scope="module")
def resources():
    root = Path(__file__).resolve().parents[1]
    manifest, entries = ResourceLibrary(root / "runtime/resources", client_hash=
        CURRENT_CLIENT_SHA256).load()
    assets = IndexedAssetService(entries, root / "runtime/resources", root.parent / "aigisr_exe")
    return {name: assets.read(name)[0] for name in (PASS_RESOURCE, MISSION_RESOURCE,
        "ItemConfigTable.atb", "ProgressiveBeginnerMissionConfig.atb")}, manifest["version"]


def decode(payload):
    return decompress_all4(payload) if payload.startswith(b"ALL4") else payload


def build(resources, rules=None):
    payloads, _ = resources
    return build_event_pass_revival(payloads[PASS_RESOURCE], payloads[MISSION_RESOURCE],
                                   rules or EventPassRevivalRules(PASS_ID, POINTS))


def test_real_resources_open_only_selected_pass_and_keep_all_base_rewards(resources):
    payloads, version = resources
    before = deepcopy(payloads)
    result = build(resources)
    effective = payloads | dict(result.replacements)
    original_pass = EventPassCatalog(payloads.__getitem__)
    final_pass = EventPassCatalog(effective.__getitem__)
    assert len(final_pass.passes) == 6
    for old, new in zip(original_pass.passes, final_pass.passes):
        if old.item_id == PASS_ID:
            assert new.status == 1 and new.start_at == old.start_at and new.end_at == old.end_at
            assert new.time_limit == "ローカル復刻" and new.rewards == old.rewards
        else:
            assert new == old
    old_mission = MissionCatalog(payloads.__getitem__, resource_version=version)
    new_mission = MissionCatalog(effective.__getitem__, resource_version=version)
    assert len(new_mission.definitions) == 382
    assert new_mission.sequence == old_mission.sequence and new_mission.groups == old_mission.groups
    for identifier, definition in old_mission.by_id.items():
        changed = new_mission.by_id[identifier]
        if identifier in POINTS:
            assert changed.event_pass_item_id == PASS_ID and changed.event_pass_point == POINTS[identifier]
            assert changed.reward_item_type == definition.reward_item_type
            assert changed.reward_item_param == definition.reward_item_param
            assert changed.start_at == definition.start_at and changed.end_at == definition.end_at
        else:
            assert changed == definition
    assert payloads == before


def test_source_records_preserve_all_unselected_bytes_and_glory_dates(resources):
    payloads, _ = resources
    result = build(resources)
    for name, fields in ((PASS_RESOURCE, {"StartDate", "EndDate", "TimeLimit", "Status"}),
                         (MISSION_RESOURCE, {"EventPassItemID", "EventPassPoint"})):
        old, new = decode(payloads[name]), decode(result.replacements[name])
        table = parse_table(old)
        offsets = {field["name"]: field["offset"] for field in table["fields"]}
        selected = range(238, 264) if name == PASS_RESOURCE else [change.source_index for change in result.mission_changes]
        allowed = {table["data_offset"] + index * table["record_size"] + offsets[field] + byte
                   for index in selected for field in fields for byte in range(4)}
        for position in range(table["data_offset"], table["string_pool_offset"]):
            if position not in allowed:
                assert old[position] == new[position]


def test_explicit_iso_window_normalizes_to_jst_without_reading_clock(resources):
    rules = EventPassRevivalRules(PASS_ID, POINTS, "2026-11-01T19:00:00+00:00", "2026-11-09T03:59:59+09:00",
                                  "予定はお知らせを参照")
    assert rules.start_at == "2026/11/02 04:00:00"
    result = build(resources, rules)
    effective = resources[0] | dict(result.replacements)
    entry = EventPassCatalog(effective.__getitem__).get(PASS_ID)
    assert entry.start_at.hour == 4 and entry.start_at.tzinfo == JST
    assert entry.end_at.day == 9 and entry.time_limit == rules.display_text


def padded(value):
    return (f"{value.year:04d}/{value.month:02d}/{value.day:02d} "
            f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}")


@pytest.mark.parametrize("year", [1000, 1001, 1969, 1970, 2026, 3001, 9998, 9999])
def test_normalized_dates_keep_the_zero_padded_text_of_every_four_digit_year(year):
    for moment in (datetime(year, 1, 2, 3, 4, 5, tzinfo=JST), datetime(year, 12, 31, 23, 59, 59, tzinfo=JST)):
        native = f"{moment.year}/{moment.month}/{moment.day} {moment.hour}:{moment.minute:02d}:{moment.second:02d}"
        for text in (moment.isoformat(), native):
            rules = EventPassRevivalRules(PASS_ID, POINTS, text, text)
            assert rules.start_at == rules.end_at == padded(moment)


def test_revival_writes_the_normalized_text_into_its_pass_rows_and_metadata(resources):
    start, end = datetime(1000, 1, 2, 3, 4, 5, tzinfo=JST), datetime(9999, 12, 31, 23, 59, 59, tzinfo=JST)
    result = build(resources, EventPassRevivalRules(PASS_ID, POINTS, "1000/1/2 3:04:05", end.isoformat()))
    assert (result.metadata["start_at"], result.metadata["end_at"]) == (padded(start), padded(end))
    first = parse_table(decode(result.replacements[PASS_RESOURCE]))["rows"][result.metadata["pass_source_index"]]
    assert (first["StartDate"], first["EndDate"]) == (padded(start), padded(end))


def test_no_configuration_has_no_replacements(resources):
    payloads, _ = resources
    result = build_event_pass_revival(payloads[PASS_RESOURCE], payloads[MISSION_RESOURCE], None)
    assert result.replacements == {} and result.pass_id is None and result.rules_hash is None
    assert result.metadata["enabled"] is False and result.source_hashes == result.effective_hashes


def test_reapplying_identical_configuration_is_byte_idempotent(resources):
    payloads, _ = resources
    rules = EventPassRevivalRules(PASS_ID, POINTS)
    first = build(resources, rules)
    effective = payloads | dict(first.replacements)
    second = build_event_pass_revival(effective[PASS_RESOURCE], effective[MISSION_RESOURCE], rules)
    assert second.replacements == {} and second.rules_hash == first.rules_hash


def test_rule_and_result_nested_collections_are_immutable(resources):
    source = dict(POINTS)
    rules = EventPassRevivalRules(PASS_ID, source)
    source[1611] = 99
    assert rules.mission_points[1611] == 2
    with pytest.raises(FrozenInstanceError):
        rules.pass_id = "other"
    result = build(resources, rules)
    for mapping in (rules.mission_points, result.replacements, result.metadata, result.source_hashes,
                    result.mission_changes[0].base_row, result.mission_changes[0].effective_row):
        with pytest.raises(TypeError):
            mapping["changed"] = 1
    assert len(result.mission_changes) == 5
    assert all(len(change.unchanged_fields_sha256) == 64 for change in result.mission_changes)


def test_rules_hash_normalizes_json_keys_and_order_but_tracks_policy():
    one = EventPassRevivalRules(PASS_ID, POINTS)
    two = EventPassRevivalRules(PASS_ID, {str(key): value for key, value in reversed(tuple(POINTS.items()))})
    assert one.content_version == two.content_version
    assert EventPassRevivalRules(PASS_ID, POINTS, display_text="別の表示").content_version != one.content_version


@pytest.mark.parametrize("mapping", [{}, {True: 2}, {0: 2}, {"01": 2}, {"x": 2}, {1: True}, {1: 0},
                                   {1: -1}, {1: 0x80000000}, {1: 2, "1": 2}, {1: 0x7FFFFFFF, 2: 1}])
def test_invalid_rule_mission_mapping_is_rejected(mapping):
    with pytest.raises(ValueError):
        EventPassRevivalRules(PASS_ID, mapping)


@pytest.mark.parametrize("updates", [{"pass_id": ""}, {"pass_id": "bad\0name"},
    {"pass_id": "x" * STORED_ITEM_NAME_BYTES}, {"display_text": "x" * 4097},
    {"start_at": "tomorrow"}, {"start_at": "2026-11-01"}, {"end_at": "2026/02/30 04:00"},
    {"start_at": "2026-11-01T04:00:00.001+09:00"}, {"end_at": "9999-12-31T23:00:00+00:00"},
    {"start_at": "2026/11/02 04:00", "end_at": "2026/11/01 04:00"}])
def test_invalid_rule_text_and_dates_are_rejected(updates):
    with pytest.raises(ValueError):
        EventPassRevivalRules(**({"pass_id": PASS_ID, "mission_points": POINTS} | updates))


def test_missing_pass_and_missing_mission_fail(resources):
    with pytest.raises(ValueError, match="pass does not exist"):
        build(resources, EventPassRevivalRules("missing", POINTS))
    with pytest.raises(ValueError, match="mission does not exist"):
        build(resources, EventPassRevivalRules(PASS_ID, {2147483647: 1}))


def test_non_daily_mission_cannot_be_repurposed(resources):
    table = parse_table(decode(resources[0][MISSION_RESOURCE]))
    identifier = next(row["ConditionID"] for row in table["rows"] if row["PeriodType"] != 1)
    with pytest.raises(ValueError, match="visible daily"):
        build(resources, EventPassRevivalRules(PASS_ID, {identifier: 1}))


@pytest.mark.parametrize("binding", [("another_pass", 2), (PASS_ID, 99), ("", 2), (PASS_ID, 0)])
def test_existing_foreign_or_changed_point_binding_is_not_overwritten(resources, binding):
    payloads, _ = resources
    changed = patch_table_cells(payloads[MISSION_RESOURCE], {20: {
        "EventPassItemID": binding[0], "EventPassPoint": binding[1]}})
    with pytest.raises(ValueError, match="different event pass binding"):
        build_event_pass_revival(payloads[PASS_RESOURCE], changed, EventPassRevivalRules(PASS_ID, POINTS))


def test_conflicting_active_pass_is_not_silently_disabled(resources):
    payloads, _ = resources
    changed = patch_table_cells(payloads[PASS_RESOURCE], {0: {"Status": 1}})
    with pytest.raises(ValueError, match="another active"):
        build_event_pass_revival(changed, payloads[MISSION_RESOURCE], EventPassRevivalRules(PASS_ID, POINTS))


def test_non_overlapping_active_pass_is_preserved(resources):
    payloads, _ = resources
    changed = patch_table_cells(payloads[PASS_RESOURCE], {0: {"Status": 1, "EndDate": "2026/06/25 11:00:00"}})
    result = build_event_pass_revival(changed, payloads[MISSION_RESOURCE], EventPassRevivalRules(PASS_ID, POINTS))
    effective = payloads | dict(result.replacements)
    first = EventPassCatalog(effective.__getitem__).passes[0]
    assert first.status == 1 and first.end_at.month == 6


def test_cross_group_inheritance_pollution_is_rejected(resources):
    payloads, _ = resources
    changed = patch_table_cells(payloads[PASS_RESOURCE], {53: {"TimeLimit": ""}})
    with pytest.raises(ValueError, match="inheritance in another pass"):
        build_event_pass_revival(changed, payloads[MISSION_RESOURCE],
            EventPassRevivalRules("event_pass_20260611", POINTS, display_text="new label"))


@pytest.mark.parametrize("field", ["NormalPassItemID", "PremiumPassItemID", "RewardItemID"])
def test_unselected_item_names_follow_the_stored_item_bound(resources, field):
    payloads, _ = resources
    rules = EventPassRevivalRules(PASS_ID, POINTS)
    stored = patch_table_cells(payloads[PASS_RESOURCE], {0: {field: "x" * STORED_ITEM_NAME_BYTES}})
    result = build_event_pass_revival(stored, payloads[MISSION_RESOURCE], rules)
    assert result.pass_id == PASS_ID and set(result.replacements) == {PASS_RESOURCE, MISSION_RESOURCE}
    oversized = patch_table_cells(payloads[PASS_RESOURCE], {0: {field: "x" * (STORED_ITEM_NAME_BYTES + 1)}})
    with pytest.raises(ValueError, match="bounded UTF-8"):
        build_event_pass_revival(oversized, payloads[MISSION_RESOURCE], rules)


def test_generic_cell_writer_patches_only_the_named_native_datetime(resources):
    payload = resources[0][MISSION_RESOURCE]
    before = parse_table(decode(payload))["rows"]
    after = parse_table(decode(patch_table_cells(payload, {20: {"StartDateTime": 0}})))["rows"]
    assert before[20]["StartDateTime"] != [0, 0]
    assert after[20] == {**before[20], "StartDateTime": [0, 0]}
    assert after[:20] + after[21:] == before[:20] + before[21:]


def test_unknown_revival_object_is_not_a_truthy_enable_switch(resources):
    payloads, _ = resources
    with pytest.raises(ValueError):
        build_event_pass_revival(payloads[PASS_RESOURCE], payloads[MISSION_RESOURCE], {})
