from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import struct

import lz4.frame
import pytest

from aigis_ps.client_versions import CURRENT_CLIENT_SHA256
from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
import aigis_ps.mission_catalog as module
from aigis_ps.mission_catalog import MISSION_FIELDS, SEQUENCE_FIELDS, MissionCatalog, MissionDefinition
from aigis_ps.mission_state import MissionProgress
from aigis_ps.native_clock import native_clock
from aigis_ps.possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES
from aigis_ps.resource_library import ResourceLibrary
from test_trade_catalog import table


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
# The default fixture's digests from the build whose mission windows compared JST instants.
PREVIOUS_CATALOG = "6b42ebf01205507941440e8e8020d637dce84da95d1ba0788fdf05f2de711e93"
PREVIOUS_PROGRESS = "2e081738ab2bd9bbac5fbd635456af67334d4a3b8835557091106ae8baa2430e"


def native_date(value):
    local = value.astimezone(JST)
    delta = local - datetime(1, 1, 1, tzinfo=JST)
    milliseconds = delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
    return [milliseconds & 0xFFFFFFFF, milliseconds >> 32]


def mission(identifier=1, **changes):
    row = {name: "" if kind == 32 else [0, 0] if kind == 9 else 0 for name, kind in MISSION_FIELDS.items()}
    row.update(ConditionID=identifier, PeriodType=1, ConditionType=2, QuestType=-2,
               ClearCountCondition=1, KindCondition=-1, RarityCondition=-1, DispTabType=1,
               DisplayOrder=identifier, RewardItemParam=1)
    row.update(changes)
    return row


def sequential(identifier=10001, ordinal=1):
    row = {name: 0 for name in SEQUENCE_FIELDS}
    row.update(ID=ordinal, GloryConditionID=identifier)
    return row


def resources(rows=None, *, sequence=None):
    rows = [mission()] if rows is None else rows
    return {
        "GloryConditionConfig.atb": table([*rows, mission(10001, PeriodType=100, DispTabType=0)], MISSION_FIELDS),
        "ProgressiveBeginnerMissionConfig.atb": table([sequential()] if sequence is None else sequence, SEQUENCE_FIELDS),
    }


def catalog(rows=None, *, payloads=None, sequence=None, version="fixture-v1"):
    return MissionCatalog((resources(rows, sequence=sequence) if payloads is None else payloads).__getitem__,
                          resource_version=version)


def fail(code, callback, status=None):
    with pytest.raises(ProtocolError) as error:
        callback()
    assert error.value.code == code
    if status is not None:
        assert error.value.status == status
    return error.value


@pytest.fixture(scope="module")
def current():
    root = Path(__file__).resolve().parents[2]
    directory = root / "aigis_ps/runtime/resources"
    manifest, entries = ResourceLibrary(directory, client_hash=
        CURRENT_CLIENT_SHA256).load()
    reader = IndexedAssetService(entries, directory, root / "aigisr_exe")
    return MissionCatalog(lambda name: reader.read(name)[0], resource_version=manifest["version"]), reader


def test_all_current_definitions_and_sequence_are_retained(current):
    subject, reader = current
    assert len(subject.definitions) == len(subject.by_id) == 382
    assert all(isinstance(row, MissionDefinition) for row in subject.definitions)
    assert tuple(subject.by_id) == tuple(sorted(subject.by_id))
    assert subject.sequence == tuple(range(10001, 10012))
    assert all(subject.by_id[key].period_type == 100 for key in subject.sequence)
    assert subject.status()["period_counts"] == {1: 21, 2: 8, 3: 4, 4: 226, 5: 112, 100: 11}
    payload = reader.read("GloryConditionConfig.atb")[0]
    rows = parse_table(decompress_all4(payload) if payload.startswith(b"ALL4") else payload)["rows"]
    for source in rows:
        definition = subject.by_id[source["ConditionID"]]
        for name, alias in module.FIELD_NAMES.items():
            if name not in module.DATE_FIELDS:
                assert getattr(definition, alias) == source[name]
    assert all(row.reward_point == 0 for row in subject.definitions)


def test_current_item_identities_fit_the_native_cell(current):
    subject, _ = current
    for alias in (module.FIELD_NAMES[name] for name in module.TEXT_FIELDS):
        assert max(len(getattr(row, alias).encode("utf-8")) for row in subject.definitions) <= MAX_ITEM_NAME_BYTES


def test_actual_attribute_groups_and_collisions_match_native_order(current):
    subject, _ = current
    root = Path(__file__).resolve().parents[2]
    evidence = json.loads((root / "aigis_research/permanent-20261101/permanent-missions-runtime-evidence/group-members.json").read_text("utf-8"))
    expected = {int(key): tuple(value["members"]) for key, value in evidence["groups"].items()}
    assert subject.groups == expected
    assert set(subject.groups) == set(range(1, 12))
    assert all(len(members) == 10 for members in subject.groups.values())
    assert subject.status()["display_order_collisions"] == evidence["collisions"]
    for group, members in subject.groups.items():
        assert tuple(row.condition_id for row in subject.page(4, 0, group, NOW)) == members
        summary = next(subject.by_id[key] for key in members if subject.by_id[key].condition_type == 17)
        assert subject.dependencies(summary, NOW) == tuple(key for key in members if key != summary.condition_id)


def test_ordinary_skin_page_does_not_lose_attribute_order_collisions(current):
    subject, _ = current
    skin = {row.condition_id for row in subject.page(4, 7, 0, NOW)}
    assert {1192, 1193} <= skin
    grouped = {key for values in subject.groups.values() for key in values}
    assert 1192 not in grouped and 1193 not in grouped
    assert {1340, 1466} <= grouped
    assert not skin & grouped


def test_current_daily_summary_only_depends_on_current_daily_page(current):
    subject, _ = current
    assert set(subject.dependencies(subject.by_id[1610], NOW)) == {1605, 1606, 1607, 1608, 1609, 1611}
    assert {row.condition_id for row in subject.page(1, 1, 0, NOW)} == {1605, 1606, 1607, 1608, 1609, 1610, 1611}
    assert not subject.is_active(subject.by_id[1223], NOW)
    assert not subject.is_active(subject.by_id[1603], NOW)
    fail("mission_definition_inactive", lambda: subject.dependencies(subject.by_id[1603], NOW), 409)


def test_current_pages_list_their_expired_versions_and_partition_the_catalog(current):
    subject, _ = current
    listed = {page: subject.listed(*page) for page in sorted(module.PAGES)}
    for (period, tab), members in listed.items():
        assert {row.condition_id for row in subject.page(period, tab, 0, NOW)} <= members
        assert all((row.period_type, row.disp_tab_type, row.display_type) == (period, tab, 0)
                   for row in map(subject.by_id.__getitem__, members))
    assert {1223, 1603} <= listed[1, 1] and len(listed[1, 1]) == 21
    grouped = [key for members in subject.groups.values() for key in members]
    every = [key for members in listed.values() for key in members] + grouped + list(subject.sequence)
    assert sorted(every) == sorted(subject.by_id)


@pytest.mark.parametrize("bound, shift, expected", [
    ("start", -timedelta(milliseconds=500), False), ("start", timedelta(), True),
    ("end", timedelta(), True), ("end", timedelta(milliseconds=500), True),
    ("end", timedelta(microseconds=999999), True), ("end", timedelta(seconds=1), False)])
def test_date_boundaries_are_inclusive_whole_seconds_of_the_client_clock(bound, shift, expected):
    start, end = NOW, NOW + timedelta(hours=1)
    subject = catalog([mission(StartDateTime=native_date(start), EndDateTime=native_date(end))])
    row = subject.by_id[1]
    assert (row.start_at, row.end_at) == (start, end)
    moment = (start if bound == "start" else end) + shift
    assert subject.is_active(row, moment.astimezone(timezone.utc)) is expected
    assert subject.is_active(row, moment.astimezone(timezone(timedelta(hours=-8)))) is expected
    assert (row in subject.active(moment)) is expected
    assert subject.is_active(subject.by_id[10001], datetime(1, 1, 1, tzinfo=JST))


def test_dates_and_the_clock_share_the_client_float32_grid():
    second = datetime(2026, 11, 1, 6, 0, 7, tzinfo=JST)
    grid = native_clock(second)
    assert grid - second == timedelta(milliseconds=4)
    subject = catalog([mission(StartDateTime=native_date(grid), EndDateTime=native_date(grid))])
    row = subject.by_id[1]
    assert subject.is_active(row, second) and subject.is_active(row, second + timedelta(microseconds=999999))
    assert not subject.is_active(row, second - timedelta(microseconds=1))
    assert not subject.is_active(row, second + timedelta(seconds=1))
    # The client clock reads 06:00:07.004 for the whole second, so an exact 06:00:07.000 window never opens.
    exact = catalog([mission(StartDateTime=native_date(second), EndDateTime=native_date(second))])
    assert not any(exact.is_active(exact.by_id[1], second + timedelta(milliseconds=shift)) for shift in (-1, 0, 999))


def test_dates_after_the_last_native_millisecond_are_value_errors():
    last = datetime.max.replace(microsecond=999000, tzinfo=JST)
    words = native_date(last)
    assert module._date(words) == last
    later = (words[0] | words[1] << 32) + 1
    for value in ([later & 0xFFFFFFFF, later >> 32], [0, 0x20000], [0xFFFFFFFF, 0xFFFFFFFF]):
        with pytest.raises(ValueError):
            module._date(value)


def test_same_start_and_end_is_a_valid_second():
    subject = catalog([mission(StartDateTime=native_date(NOW), EndDateTime=native_date(NOW))])
    assert subject.is_active(subject.by_id[1], NOW)
    assert subject.is_active(subject.by_id[1], NOW + timedelta(milliseconds=999))
    assert not subject.is_active(subject.by_id[1], NOW + timedelta(seconds=1))


@pytest.mark.parametrize("now", [None, 0, "2026-11-01", datetime(2026, 11, 1)])
def test_clocks_require_an_absolute_instant(now):
    subject = catalog()
    for action in (lambda: subject.active(now), lambda: subject.is_active(subject.by_id[1], now),
                   lambda: subject.page(1, 1, 0, now), lambda: subject.dependencies(subject.by_id[1], now)):
        fail("mission_clock_invalid", action, 400)


@pytest.mark.parametrize("period,tab", [(1, 1), (2, 2), (3, 3), (4, 4), (4, 7), (5, 5)])
def test_supported_pages_match_period_tab_and_plain_display_only(period, tab):
    subject = catalog([
        mission(1, PeriodType=period, DispTabType=tab, DisplayOrder=10),
        mission(2, PeriodType=period, DispTabType=tab, DisplayOrder=1),
        mission(3, PeriodType=period, DispTabType=tab + 1),
        mission(4, PeriodType=period, DispTabType=tab, DisplayType=1),
        mission(5, PeriodType=period, DispTabType=tab, EndDateTime=native_date(NOW - timedelta(days=1))),
        mission(6, PeriodType=period, DispTabType=tab, StartDateTime=native_date(NOW + timedelta(days=1))),
    ])
    assert tuple(row.condition_id for row in subject.page(period, tab, 0, NOW)) == (2, 1)
    assert subject.listed(period, tab) == frozenset((1, 2, 5, 6))


@pytest.mark.parametrize("period,tab,group", [(3, 4, 0), (100, 0, 0), (4, 0, 0), (4, 6, 0),
                                           (1, 1, 1), (2, 1, 0), (99, 99, 0)])
def test_unsupported_pages_are_not_empty_success(period, tab, group):
    fail("mission_page_unsupported", lambda: catalog().page(period, tab, group, NOW), 501)


@pytest.mark.parametrize("period,tab", [(3, 4), (100, 0), (4, 0), (4, 6), (2, 1), (99, 99)])
def test_unsupported_listed_pages_are_not_empty_success(period, tab):
    fail("mission_page_unsupported", lambda: catalog().listed(period, tab), 501)


@pytest.mark.parametrize("parts", [(True, 1, 0), (1, 1.0, 0), (1, 1, False), (-1, 1, 0), (1, 1, 0x80000000)])
def test_invalid_page_values_reject_bool_and_out_of_range(parts):
    fail("mission_page_invalid", lambda: catalog().page(*parts, NOW), 400)


@pytest.mark.parametrize("parts", [(True, 1), (1, 1.0), (-1, 1), (1, 0x80000000)])
def test_invalid_listed_page_values_reject_bool_and_out_of_range(parts):
    fail("mission_page_invalid", lambda: catalog().listed(*parts), 400)


def test_unknown_groups_and_foreign_definitions_fail_explicitly():
    subject = catalog()
    fail("mission_group_unknown", lambda: subject.page(4, 0, 99, NOW), 409)
    changed = replace(subject.by_id[1], reward_item_param=7)
    fail("mission_definition_invalid", lambda: subject.is_active(changed, NOW), 503)
    fail("mission_definition_invalid", lambda: subject.dependencies(1, NOW), 503)


def test_groups_overwrite_before_display_filter_and_before_date_filter():
    subject = catalog([
        mission(1, PeriodType=4, DisplayType=1, GroupType=1, DisplayOrder=10),
        mission(2, PeriodType=4, DisplayType=0, GroupType=0, DisplayOrder=10),
        mission(3, PeriodType=4, DisplayType=1, GroupType=1, DisplayOrder=20),
        mission(4, PeriodType=4, DisplayType=1, GroupType=2, DisplayOrder=20,
                EndDateTime=native_date(NOW - timedelta(days=1))),
    ])
    assert subject.groups == {2: (4,)}
    assert subject.page(4, 0, 2, NOW) == ()


def test_signed_display_order_is_preserved_for_pages_and_groups():
    subject = catalog([
        mission(1, DisplayOrder=0), mission(2, DisplayOrder=-10000),
        mission(3, PeriodType=4, DisplayType=1, GroupType=1, DisplayOrder=-1),
        mission(4, PeriodType=4, DisplayType=1, GroupType=1, DisplayOrder=-20),
    ])
    assert tuple(row.condition_id for row in subject.page(1, 1, 0, NOW)) == (2, 1)
    assert subject.groups[1] == (4, 3)


def test_dependency_sets_use_active_members_and_exclude_all_own_summary_types():
    subject = catalog([
        mission(1), mission(2, ConditionType=3), mission(3, ConditionType=3),
        mission(4, EndDateTime=native_date(NOW - timedelta(days=1))),
        mission(10, PeriodType=4, DisplayType=1, GroupType=1, DisplayOrder=10),
        mission(11, PeriodType=4, ConditionType=17, DisplayType=1, GroupType=1, DisplayOrder=11),
        mission(12, PeriodType=4, ConditionType=17, DisplayType=1, GroupType=1, DisplayOrder=12),
    ])
    assert subject.dependencies(subject.by_id[2], NOW) == (1,)
    assert subject.dependencies(subject.by_id[11], NOW) == (10,)
    assert subject.dependencies(subject.by_id[1], NOW) == ()


@pytest.mark.parametrize("row", [mission(1, ConditionType=3),
    mission(1, PeriodType=4, ConditionType=3, DispTabType=4),
    mission(1, PeriodType=4, ConditionType=17, DisplayType=1, GroupType=1),
    mission(1, PeriodType=4, ConditionType=17, DisplayType=0, GroupType=0)])
def test_empty_or_malformed_aggregate_dependencies_are_rejected(row):
    subject = catalog([row])
    fail("mission_dependencies_invalid", lambda: subject.dependencies(subject.by_id[1], NOW), 503)


def test_dependencies_keep_the_one_client_clock_reading_of_the_request():
    second = NOW.replace(second=8)
    opening = native_clock(second)
    assert second - opening == timedelta(milliseconds=4)
    subject = catalog([mission(1, ConditionType=3), mission(2, StartDateTime=native_date(opening))])
    now = second + timedelta(milliseconds=500)
    assert [row.condition_id for row in subject.page(1, 1, 0, now)] == [1, 2]
    assert subject.dependencies(subject.by_id[1], now) == (2,)


def test_dependency_cycle_cannot_be_hidden_by_a_page_resolver(monkeypatch):
    subject = catalog([mission(1, ConditionType=3),
                       mission(2, PeriodType=4, ConditionType=17, DisplayType=1, GroupType=1)])
    monkeypatch.setattr(subject, "_page", lambda *_: (subject.by_id[1], subject.by_id[2]))
    error = fail("mission_dependencies_invalid", lambda: subject.dependencies(subject.by_id[1], NOW), 503)
    assert str(error.__cause__) == "Mission dependencies contain a cycle"


def test_unimplemented_conditions_and_periods_remain_in_catalog_and_active():
    subject = catalog([mission(1, ConditionType=999, PeriodType=77, QuestType=-7,
                               KindCondition=-2, RarityCondition=-5, EventPassItemID="event_token")])
    assert subject.by_id[1].condition_type == 999 and subject.by_id[1].period_type == 77
    assert subject.by_id[1].event_pass_item_id == "event_token"
    assert subject.by_id[1] in subject.active(NOW)


@pytest.mark.parametrize("changes", [{"ConditionID": 0}, {"ConditionID": -1}, {"PeriodType": 0},
    {"ConditionType": 0}, {"ClearCountCondition": -1}, {"RewardItemParam": -1},
    {"RewardItemID": "bad\x01name"}, {"EventPassItemID": "x" * (STORED_ITEM_NAME_BYTES + 1)},
    {"StartDateTime": [0xFFFFFFFF, 0xFFFFFFFF]}, {"EndDateTime": [0, 0x20000]},
    {"StartDateTime": native_date(NOW), "EndDateTime": native_date(NOW - timedelta(days=1))}])
def test_malformed_definition_values_reject_without_partial_catalog(changes):
    fail("mission_catalog_invalid", lambda: catalog([mission(**changes)]), 503)


@pytest.mark.parametrize("field", sorted(module.TEXT_FIELDS))
@pytest.mark.parametrize("unit", ["x", "投"])
def test_item_identities_load_up_to_the_stored_item_name_bound(field, unit):
    name = unit * (STORED_ITEM_NAME_BYTES // len(unit.encode("utf-8")))
    name += "x" * (STORED_ITEM_NAME_BYTES - len(name.encode("utf-8")))
    assert len(name.encode("utf-8")) == STORED_ITEM_NAME_BYTES
    assert getattr(catalog([mission(**{field: name})]).by_id[1], module.FIELD_NAMES[field]) == name
    fail("mission_catalog_invalid", lambda: catalog([mission(**{field: name + "x"})]), 503)


@pytest.mark.parametrize("rows", [[mission(1), mission(1)], [mission(10001)]])
def test_duplicate_mission_ids_are_not_overwritten(rows):
    fail("mission_catalog_invalid", lambda: catalog(rows), 503)


@pytest.mark.parametrize("sequence", [[], [sequential(999)], [sequential(1)],
    [sequential(), sequential()], [sequential(10001, 2)]])
def test_sequence_requires_contiguous_unique_existing_pt100_members(sequence):
    fail("mission_catalog_invalid", lambda: catalog(sequence=sequence), 503)


def test_sequence_sorting_uses_ids_and_rejects_unreferenced_pt100():
    rows = [mission(10002, PeriodType=100, DispTabType=0)]
    subject = catalog(rows, sequence=[sequential(10001, 2), sequential(10002, 1)])
    assert subject.sequence == (10002, 10001)
    fail("mission_catalog_invalid", lambda: catalog(rows), 503)


@pytest.mark.parametrize("resource", ["GloryConditionConfig.atb", "ProgressiveBeginnerMissionConfig.atb"])
@pytest.mark.parametrize("payload", [b"", b"invalid", bytearray(b"ALTB"), None])
def test_resource_bytes_are_strict(resource, payload):
    data = resources()
    data[resource] = payload
    fail("mission_catalog_invalid", lambda: catalog(payloads=data), 503)


def test_empty_definition_resource_and_schema_changes_are_rejected():
    data = resources()
    data["GloryConditionConfig.atb"] = table([], MISSION_FIELDS)
    fail("mission_catalog_invalid", lambda: catalog(payloads=data), 503)
    fields = dict(MISSION_FIELDS)
    del fields["EventPassPoint"]
    data["GloryConditionConfig.atb"] = table([mission()], fields)
    fail("mission_catalog_invalid", lambda: catalog(payloads=data), 503)


@pytest.mark.parametrize("field,value", [("ConditionID", True), ("RewardPoint", False),
    ("ClearCountCondition", 1.0), ("QuestType", 0x80000000),
    ("StartDateTime", [True, 0]), ("EndDateTime", [-1, 0]), ("RewardItemID", None)])
def test_decoded_types_cannot_bypass_native_field_checks(monkeypatch, field, value):
    data = resources()
    parse = module.parse_table

    def changed(payload, limits):
        result = parse(payload, limits)
        if payload == data["GloryConditionConfig.atb"]:
            result["rows"][0][field] = value
        return result

    monkeypatch.setattr(module, "parse_table", changed)
    fail("mission_catalog_invalid", lambda: catalog(payloads=data), 503)


def test_definitions_mappings_and_status_are_independent_immutable_views():
    data = resources([mission(2), mission(1)])
    before = deepcopy(data)
    subject = catalog(payloads=data)
    assert data == before
    assert tuple(row.condition_id for row in subject.definitions) == (1, 2, 10001)
    with pytest.raises(FrozenInstanceError):
        subject.by_id[1].reward_item_param = 50
    with pytest.raises(TypeError):
        subject.by_id[1] = subject.by_id[2]
    with pytest.raises(TypeError):
        subject.groups[1] = (1,)
    snapshot = subject.status()
    changed = subject.status()
    changed["resource_sha256"].clear()
    changed["period_counts"].clear()
    changed["display_order_collisions"].append({})
    data.clear()
    assert subject.status() == snapshot


def test_readiness_is_constant_and_never_builds_the_status(monkeypatch):
    subject = catalog()
    assert subject.status()["ready"] is subject.is_ready() is True
    monkeypatch.setattr(subject, "status", lambda: pytest.fail("Readiness built the full catalog status"))
    assert subject.is_ready() is True


def test_per_definition_hash_is_semantic_and_catalog_tracks_all_resources_and_version():
    first = catalog([mission(1), mission(2)])
    reordered = catalog([mission(2), mission(1)])
    changed = catalog([mission(1, RewardItemParam=2), mission(2)])
    versioned = catalog([mission(1), mission(2)], version="fixture-v2")
    assert first.by_id[1].definition_version == reordered.by_id[1].definition_version
    assert first.by_id[1].definition_version != changed.by_id[1].definition_version
    assert first.by_id[2].definition_version == changed.by_id[2].definition_version
    assert first.content_version != reordered.content_version != changed.content_version
    assert first.content_version != versioned.content_version
    assert first.by_id[1].definition_version == versioned.by_id[1].definition_version


def test_the_window_rule_is_digested_and_the_previous_digests_stay_replayable():
    subject = catalog()
    assert subject.status()["dates"] == module.WINDOW_RULE == "inclusive_start_and_end_native_clock"
    assert subject.legacy_window_content_version == PREVIOUS_CATALOG != subject.content_version
    assert subject.replayable_content_versions() == {subject.content_version, PREVIOUS_CATALOG}
    progress = MissionProgress(subject)
    assert progress.legacy_window_content_version == progress.content_version_for(PREVIOUS_CATALOG) == PREVIOUS_PROGRESS
    assert progress.content_version == progress.content_version_for(subject.content_version) != PREVIOUS_PROGRESS


def test_compressed_and_uncompressed_resources_keep_decoded_identity():
    data = resources()
    first = catalog(payloads=data)
    for name, payload in data.items():
        data[name] = struct.pack("<4sHHI", b"ALL4", 1, 0, len(payload)) + lz4.frame.compress(payload)
    assert catalog(payloads=data).content_version == first.content_version


@pytest.mark.parametrize("version", [None, 7, "", "x" * 129, "bad\x01value"])
def test_invalid_resource_versions_fail_closed(version):
    fail("mission_catalog_invalid", lambda: catalog(version=version), 503)


def test_resource_bound_is_checked_before_decoding(monkeypatch):
    monkeypatch.setattr(module, "RESOURCE_LIMIT", 3)
    fail("mission_catalog_invalid", lambda: MissionCatalog(lambda _: b"ALTB", resource_version="fixture"), 503)
