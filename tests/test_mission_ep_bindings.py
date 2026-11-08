from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from types import SimpleNamespace

import pytest

import aigis_ps.mission_ep_bindings as module
from aigis_ps.alxml import AlXmlError
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.mission_catalog import _digest
from aigis_ps.mission_ep_bindings import MissionEpBindings, read_bindings
from aigis_ps.mission_state import MissionProgress, _clock
from aigis_ps.possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES
from test_mission_catalog import catalog, current, mission


FIRST = "event_pass_20260611"
SECOND = "event_pass_20261015"
NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
MARKER_DAMAGE = ("schema", "fields", "structure", "id", "base", "point", "pair", "entry")
INVALID = ("mission_ep_bindings_invalid", 503)
IDENTITY_CHANGED = (ValueError, "EP overlay changed mission identities, groups or order")
UNPAIRED_BINDING = (ValueError, "EP bindings require a matched item and positive points, or neither")
INVALID_TEXT = (AlXmlError, "Text contains an invalid XML character")
STALE_FINGERPRINT = (ValueError, "Mission definition fingerprint is inconsistent")


def catalogs(item=FIRST, point=10):
    base = catalog([mission(ClearCountCondition=3)])
    effective = catalog([mission(ClearCountCondition=3, EventPassItemID=item, EventPassPoint=point)], version="effective")
    return base, effective


def stand_in(effective, **fields):
    return SimpleNamespace(**{"content_version": effective.content_version, "by_id": effective.by_id,
        "sequence": effective.sequence, "groups": effective.groups,
        "legacy_window_content_version": effective.legacy_window_content_version, **fields})


def overlay_refusal(base, replacement):
    with pytest.raises(ProtocolError) as caught:
        MissionEpBindings(base, replacement)
    assert (caught.value.code, caught.value.status) == ("mission_ep_catalog_invalid", 503)
    return type(caught.value.__cause__), str(caught.value.__cause__)


def record(definition, status=0, *, identifier=None, version=None):
    return {"condition_id": definition.condition_id if identifier is None else identifier,
        "period_type": definition.period_type, "period": "day:2026-11-01",
        "definition_version": definition.definition_version if version is None else version,
        "goal": 3, "progress": 1 if status == 0 else 3, "status": status, "read": status == 2,
        "uids": [99]}


def state(*entries):
    return {"mission_state": {"schema_version": 1, "entries": deepcopy(list(entries)),
        "last_time": _clock(NOW)[1], "last_login_day": "2026-11-01"},
        "other": {"preserve": [1]}, "tables": {"presents": []}}


def selected(value, index=0):
    return value["mission_state"]["entries"][index]


def assert_error(code, call):
    with pytest.raises(ProtocolError) as caught:
        call()
    assert caught.value.code == code


def refusal(read, value):
    with pytest.raises(ProtocolError) as caught:
        read(value)
    return caught.value.code, caught.value.status


def damage(marker, kind):
    if kind == "schema":
        marker["schema_version"] = True
    elif kind == "fields":
        marker["extra"] = 1
    elif kind == "structure":
        marker["bindings"] = list(marker["bindings"].values())
    elif kind == "id":
        marker["bindings"]["01"] = marker["bindings"].pop("1")
    elif kind == "base":
        marker["bindings"]["1"]["base_version"] = "unknown"
    elif kind == "point":
        marker["bindings"]["1"]["point"] = -1
    elif kind == "pair":
        marker["bindings"]["1"]["item_id"] = ""
    else:
        marker["bindings"]["1"]["extra"] = 1


def test_no_overlay_or_marker_preserves_state_without_adding_fields():
    base, _ = catalogs()
    original = state(record(base.by_id[1]))
    expected = deepcopy(original)
    prepared = MissionEpBindings(base, base).prepare(original)
    assert prepared == expected and original == expected
    assert "mission_ep_bindings" not in prepared
    prepared["other"]["preserve"].append(2)
    assert original == expected


@pytest.mark.parametrize("status", [0, 1])
def test_activation_migrates_only_definition_hash_and_keeps_progress(status):
    base, effective = catalogs()
    original = state(record(base.by_id[1], status))
    before = deepcopy(original)
    prepared = MissionEpBindings(base, effective).prepare(original)
    expected = deepcopy(before["mission_state"])
    expected["entries"][0]["definition_version"] = effective.by_id[1].definition_version
    assert prepared["mission_state"] == expected
    assert original == before
    assert prepared["mission_ep_bindings"] == {"schema_version": 1, "bindings": {
        "1": {"base_version": base.by_id[1].definition_version, "item_id": FIRST, "point": 10}}}


def test_claimed_records_never_receive_new_definition_or_progress():
    base, effective = catalogs()
    original = state(record(base.by_id[1], 2))
    prepared = MissionEpBindings(base, effective).prepare(original)
    assert prepared["mission_state"] == original["mission_state"]
    assert prepared["tables"] == original["tables"]


def test_switch_points_switch_pass_disable_and_reenable_remain_provable():
    base, first = catalogs()
    _, second = catalogs(SECOND, 25)
    original = state(record(base.by_id[1], 1), record(base.by_id[1], 2))
    initialized = MissionEpBindings(base, first).prepare(original)
    switched = MissionEpBindings(base, second).prepare(initialized)
    assert selected(switched)["definition_version"] == second.by_id[1].definition_version
    disabled = MissionEpBindings(base, base).prepare(switched)
    assert selected(disabled)["definition_version"] == base.by_id[1].definition_version
    assert disabled["mission_ep_bindings"]["bindings"]["1"]["item_id"] == ""
    assert disabled["mission_ep_bindings"]["bindings"]["1"]["point"] == 0
    enabled = MissionEpBindings(base, first).prepare(disabled)
    assert selected(enabled)["definition_version"] == first.by_id[1].definition_version
    assert selected(enabled, 1) == selected(original, 1)
    for value in (initialized, switched, disabled, enabled):
        expected = deepcopy(selected(original))
        expected["definition_version"] = selected(value)["definition_version"]
        assert selected(value) == expected


def test_repeated_prepare_is_idempotent():
    base, effective = catalogs()
    service = MissionEpBindings(base, effective)
    prepared = service.prepare(state(record(base.by_id[1])))
    assert service.prepare(prepared) == prepared


def test_unknown_historical_id_and_valid_retired_marker_are_preserved():
    base, effective = catalogs()
    original = state(record(base.by_id[1]), record(base.by_id[1], identifier=900, version="e" * 64))
    original["mission_ep_bindings"] = {"schema_version": 1, "bindings": {
        "900": {"base_version": "f" * 64, "item_id": "retired_pass", "point": 20}}}
    result = MissionEpBindings(base, effective).prepare(original)
    assert selected(result, 1) == selected(original, 1)
    assert result["mission_ep_bindings"]["bindings"]["900"] == original["mission_ep_bindings"]["bindings"]["900"]


@pytest.mark.parametrize("marker", [False, True])
def test_unprovable_pending_hash_is_preserved_for_progress_to_reject(marker):
    base, effective = catalogs()
    original = state(record(base.by_id[1], version="f" * 64))
    if marker:
        original["mission_ep_bindings"] = {"schema_version": 1, "bindings": {
            "1": {"base_version": base.by_id[1].definition_version, "item_id": SECOND, "point": 100}}}
    result = MissionEpBindings(base, effective).prepare(original)
    assert selected(result) == selected(original)
    assert_error("mission_definition_changed", lambda: MissionProgress(effective).snapshot(result, NOW))


def test_different_old_base_hash_does_not_authorize_migration():
    base, old = catalogs()
    changed_base = catalog([mission(ClearCountCondition=4)])
    changed_effective = catalog([mission(ClearCountCondition=4, EventPassItemID=SECOND, EventPassPoint=30)])
    prepared = MissionEpBindings(base, old).prepare(state(record(base.by_id[1])))
    result = MissionEpBindings(changed_base, changed_effective).prepare(prepared)
    assert selected(result) == selected(prepared)


@pytest.mark.parametrize("change", [
    {"ClearCountCondition": 4}, {"RewardItemParam": 2}, {"RewardItemType": 2},
    {"ConditionType": 4}, {"QuestID": 1001}, {"DisplayOrder": 10}, {"ConditionText": 20},
    {"StartDateTime": [86400000, 0]}, {"PeriodType": 2, "DispTabType": 2},
])
def test_non_ep_definition_changes_are_not_allowed(change):
    base, _ = catalogs()
    source = mission(ClearCountCondition=3, EventPassItemID=FIRST, EventPassPoint=10)
    source.update(change)
    effective = catalog([source])
    assert_error("mission_ep_catalog_invalid", lambda: MissionEpBindings(base, effective))


def test_stand_in_catalog_is_accepted_like_the_catalog_it_copies():
    base, effective = catalogs()
    real, copied = MissionEpBindings(base, effective), MissionEpBindings(base, stand_in(effective))
    assert (copied.changed_ids, copied.content_version, copied.legacy_window_content_version) == (
        real.changed_ids, real.content_version, real.legacy_window_content_version)
    assert real.changed_ids == (1,)


def test_identity_set_and_sequence_or_groups_must_match():
    base, effective = catalogs()
    for changes in ({"by_id": {10001: effective.by_id[10001]}},
                    {"sequence": (1, 10001)}, {"groups": {1: (1,)}}):
        assert overlay_refusal(base, stand_in(effective, **changes)) == IDENTITY_CHANGED


@pytest.mark.parametrize("item,point,cause", [
    (FIRST, 0, UNPAIRED_BINDING), ("", 1, UNPAIRED_BINDING), (FIRST, True, UNPAIRED_BINDING),
    ("x" * 257, 2, UNPAIRED_BINDING), ("bad\x00", 2, INVALID_TEXT),
])
def test_illegal_ep_binding_is_rejected_even_if_definition_hash_matches(item, point, cause):
    base, effective = catalogs()
    definition = replace(effective.by_id[1], event_pass_item_id=item, event_pass_point=point)
    fields = asdict(definition)
    fields.pop("definition_version")
    definition = replace(definition, definition_version=_digest(fields))
    assert overlay_refusal(base, stand_in(effective, by_id={**effective.by_id, 1: definition})) == cause


def test_pinned_dgp_definition_is_not_relaxed():
    base = catalog([mission(1031)])
    effective = catalog([mission(1031, EventPassItemID=FIRST, EventPassPoint=10)])
    assert_error("mission_ep_catalog_invalid", lambda: MissionEpBindings(base, effective))


@pytest.mark.parametrize("kind", MARKER_DAMAGE)
def test_bad_marker_is_rejected_and_input_remains_unchanged(kind):
    base, effective = catalogs()
    original = MissionEpBindings(base, effective).prepare(state(record(base.by_id[1])))
    damage(original["mission_ep_bindings"], kind)
    expected = deepcopy(original)
    assert_error("mission_ep_bindings_invalid", lambda: MissionEpBindings(base, effective).prepare(original))
    assert original == expected


def test_reader_returns_none_without_a_marker_and_copies_only_the_marker(monkeypatch):
    base, effective = catalogs()
    assert read_bindings(state(record(base.by_id[1]))) is None
    prepared = MissionEpBindings(base, effective).prepare(state(record(base.by_id[1])))
    expected = deepcopy(prepared)
    copied = []
    monkeypatch.setattr(module, "deepcopy", lambda value: copied.append(value) or deepcopy(value))
    marker = read_bindings(prepared)
    assert marker == prepared["mission_ep_bindings"] and marker is not prepared["mission_ep_bindings"]
    assert copied == [prepared["mission_ep_bindings"]]
    marker["bindings"].clear()
    assert prepared == expected


@pytest.mark.parametrize("kind", MARKER_DAMAGE)
def test_reader_refuses_damaged_markers_with_the_code_prepare_answers(kind):
    base, effective = catalogs()
    original = MissionEpBindings(base, effective).prepare(state(record(base.by_id[1])))
    damage(original["mission_ep_bindings"], kind)
    expected = deepcopy(original)
    assert refusal(read_bindings, original) == refusal(MissionEpBindings(base, effective).prepare, original) == INVALID
    assert original == expected


@pytest.mark.parametrize("value", [None, [], "mission_ep_bindings", 7, {"mission_ep_bindings": None},
                                   {"mission_ep_bindings": []}])
def test_reader_refuses_states_and_markers_that_are_not_objects(value):
    base, effective = catalogs()
    assert refusal(read_bindings, value) == refusal(MissionEpBindings(base, effective).prepare, value) == INVALID


def test_marker_is_created_without_inventing_missing_mission_history():
    base, effective = catalogs()
    original = {"other": [1]}
    result = MissionEpBindings(base, effective).prepare(original)
    assert "mission_state" not in result
    assert result["mission_ep_bindings"]["bindings"]["1"]["point"] == 10
    assert original == {"other": [1]}


def test_known_effective_hash_needs_no_previous_marker():
    base, effective = catalogs()
    original = state(record(effective.by_id[1], 1))
    result = MissionEpBindings(base, effective).prepare(original)
    assert result["mission_state"] == original["mission_state"]


def test_catalog_fingerprint_change_after_setup_is_rejected():
    base, effective = catalogs()
    service = MissionEpBindings(base, effective)
    effective.content_version = "f" * 64
    assert_error("mission_ep_catalog_changed", lambda: service.prepare({}))


def test_stale_definition_hash_cannot_authorize_field_replacement():
    base, effective = catalogs()
    stale = replace(effective.by_id[1], clear_count_condition=999)
    assert overlay_refusal(base, stand_in(effective, by_id={**effective.by_id, 1: stale})) == STALE_FINGERPRINT


def test_history_limit_does_not_drop_retired_markers(monkeypatch):
    base, effective = catalogs()
    service = MissionEpBindings(base, effective)
    original = {"mission_ep_bindings": {"schema_version": 1, "bindings": {
        "900": {"base_version": "f" * 64, "item_id": FIRST, "point": 1}}}}
    monkeypatch.setattr(module, "MAX_BINDINGS", 1)
    expected = deepcopy(original)
    assert_error("mission_ep_bindings_invalid", lambda: service.prepare(original))
    assert original == expected


def test_all_current_definitions_are_compatible_when_feature_is_disabled(current):
    base, _ = current
    service = MissionEpBindings(base, base)
    assert len(base.by_id) == 382
    assert service.changed_ids == ()
    assert service.prepare({"existing": [1]}) == {"existing": [1]}


@pytest.mark.parametrize("unit", ["x", "投"])
def test_overlay_items_written_into_new_markers_must_fit_the_native_cell(unit):
    fitting = unit * (MAX_ITEM_NAME_BYTES // len(unit.encode("utf-8")))
    oversized = fitting + "x" * (STORED_ITEM_NAME_BYTES - len(fitting.encode("utf-8")))
    assert (len(fitting.encode("utf-8")), len(oversized.encode("utf-8"))) == (MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES)
    base, effective = catalogs(fitting)
    prepared = MissionEpBindings(base, effective).prepare(state(record(base.by_id[1])))
    assert prepared["mission_ep_bindings"]["bindings"]["1"]["item_id"] == fitting
    base, effective = catalogs(oversized)
    assert_error("mission_ep_catalog_invalid", lambda: MissionEpBindings(base, effective))


def test_stored_markers_and_unchanged_resource_items_keep_the_earlier_bound():
    stored = "x" * STORED_ITEM_NAME_BYTES
    base, effective = catalogs()
    original = state(record(base.by_id[1]))
    original["mission_ep_bindings"] = {"schema_version": 1, "bindings": {
        "900": {"base_version": "f" * 64, "item_id": stored, "point": 20}}}
    result = MissionEpBindings(base, effective).prepare(original)
    assert result["mission_ep_bindings"]["bindings"]["900"]["item_id"] == stored
    resource = catalog([mission(ClearCountCondition=3, EventPassItemID=stored, EventPassPoint=10)])
    assert MissionEpBindings(resource, resource).changed_ids == ()
    original["mission_ep_bindings"]["bindings"]["900"]["item_id"] += "x"
    assert_error("mission_ep_bindings_invalid", lambda: MissionEpBindings(base, effective).prepare(original))
