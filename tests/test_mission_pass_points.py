from copy import copy, deepcopy
from dataclasses import fields, replace
from datetime import timedelta
import hashlib
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.event_pass_catalog import EventPassCatalog
from aigis_ps.mission_catalog import MissionDefinition
import aigis_ps.mission_pass_points as module
from aigis_ps.mission_pass_points import MissionPassPoints
from aigis_ps.possessions import PossessionService
from test_event_pass_catalog import NOW, current, item, payloads, reward


def definition(identifier=1, pass_id="normal", point=10):
    values = {field.name: 0 for field in fields(MissionDefinition)}
    values.update(condition_id=identifier, period_type=1, condition_type=1, clear_count_condition=1,
                  disp_tab_type=1, reward_item_id="", event_pass_item_id=pass_id,
                  event_pass_point=point, start_at=None, end_at=None, definition_version="a" * 64)
    return MissionDefinition(**values)


def state(count=None):
    rows = [{"ItemID": "prize", "Count": 7, "Enabled": 1}]
    if count is not None:
        rows.append({"ItemID": "normal", "Count": count, "Enabled": 1})
    return {"bootstrap_schema": 1, "tables": {"possessions": rows, "presents": [],
            "event_pass_status": [{"ID": "normal", "ReceivedNormalPoint": 5, "ReceivedPremiumPoint": 0}]},
            "mission_state": {"keep": [1]}, "player_state": {"keep": [2]}, "sentinel": [3]}


@pytest.fixture
def case(tmp_path):
    resources = payloads(rows=[reward(), reward(2, NormalPassItemID="second")],
        items=[item(), item("second"), item("premium", True), item("prize", Stack=100)])
    possessions = PossessionService(tmp_path, asset_reader=resources.__getitem__)
    catalog = EventPassCatalog(resources.__getitem__, possessions)
    service = SimpleNamespace(catalog=catalog, possessions=possessions)
    return SimpleNamespace(points=MissionPassPoints(service), service=service,
                           possessions=possessions, catalog=catalog)


def error(code, callback):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert caught.value.code == code


def test_aggregates_per_pass_and_only_changes_corresponding_counts(case, monkeypatch):
    original = state(3)
    before = deepcopy(original)
    calls = []
    credit = case.possessions.credit
    def observed(saved, grants):
        calls.append(dict(grants))
        return credit(saved, grants)
    monkeypatch.setattr(case.possessions, "credit", observed)
    definitions = [definition(3, "second", 9), definition(2, point=7), definition(1, point=5)]
    updated, audit = case.points.award(original, definitions, NOW)
    assert original == before and updated is not original
    assert calls == [{"normal": 12, "second": 9}]
    assert audit == [
        {"pass_id": "normal", "requested": 12, "credited": 12, "before": 3, "after": 15, "capped": 0, "inactive": 0},
        {"pass_id": "second", "requested": 9, "credited": 9, "before": 0, "after": 9, "capped": 0, "inactive": 0}]
    expected = deepcopy(original)
    expected["tables"]["possessions"][1]["Count"] = 15
    expected["tables"]["possessions"].append({"ItemID": "second", "Count": 9})
    assert updated == expected
    case.points.validate_receipt(updated, definitions, audit)
    assert case.points.award(original, list(reversed(definitions)), NOW) == (updated, audit)


@pytest.mark.parametrize("before,credited", [(None, 10), (0, 10), (990, 9), (995, 4), (999, 0), (1001, 0), (0x7FFFFFFF, 0)])
def test_saturates_without_debt_or_truncating_legacy_counts(case, before, credited, monkeypatch):
    saved = state(before)
    original = deepcopy(saved)
    if credited == 0:
        monkeypatch.setattr(case.possessions, "credit", lambda *_: pytest.fail("Zero grant must not call credit"))
    updated, audit = case.points.award(saved, [definition()], NOW)
    assert audit[0] == {"pass_id": "normal", "requested": 10, "credited": credited,
        "before": before or 0, "after": (before or 0) + credited, "capped": 10 - credited, "inactive": 0}
    assert saved == original
    if credited == 0:
        assert updated == saved and updated is not saved
    case.points.validate_receipt(updated, [definition()], audit)


@pytest.mark.parametrize("boundary,offset,active", [("start_at", -1, False), ("start_at", 0, True),
    ("end_at", 0, True), ("end_at", 1, False)])
def test_window_is_inclusive_and_inactive_rewards_are_discarded(case, boundary, offset, active):
    now = getattr(case.catalog.get("normal"), boundary) + timedelta(seconds=offset)
    original = state(998)
    updated, audit = case.points.award(original, [definition()], now)
    assert audit[0]["credited"] == (1 if active else 0)
    assert audit[0]["capped"] == (9 if active else 0)
    assert audit[0]["inactive"] == (0 if active else 10)
    if not active:
        assert updated == original
    case.points.validate_receipt(updated, [definition()], audit)


def test_naive_time_uses_jst(case):
    assert case.points.award(state(), [definition()], NOW.replace(tzinfo=None)) == case.points.award(state(), [definition()], NOW)


@pytest.mark.parametrize("definitions", [[], [definition(pass_id="", point=0)]])
def test_no_ep_is_equal_independent_state(case, definitions, monkeypatch):
    monkeypatch.setattr(case.possessions, "credit", lambda *_: pytest.fail("No EP must not call credit"))
    original = state()
    updated, audit = case.points.award(original, definitions, NOW)
    assert updated == original and updated is not original and audit == []
    updated["sentinel"].append(4)
    assert original["sentinel"] == [3]


@pytest.mark.parametrize("definitions", [None, {}, "normal", [object()], [definition(), definition()],
    [definition(identifier=True)], [definition(identifier=0)], [definition(pass_id=None)],
    [definition(point=-1)], [definition(point=True)], [definition(point="10")],
    [definition(point=0x80000000)], [definition(pass_id="", point=1)], [definition(point=0)]])
def test_invalid_definition_pairs_are_rejected_without_changes(case, definitions):
    saved = state()
    before = deepcopy(saved)
    error("mission_pass_definition_invalid", lambda: case.points.award(saved, definitions, NOW))
    assert saved == before


@pytest.mark.parametrize("identifier", ["missing", "prize", "premium", "Normal"])
def test_only_exact_ordinary_pass_item_identity_is_eligible(case, identifier):
    error("mission_pass_unknown", lambda: case.points.award(state(), [definition(pass_id=identifier)], NOW))


def test_aggregate_overflow_is_rejected_before_credit(case, monkeypatch):
    monkeypatch.setattr(case.possessions, "credit", lambda *_: pytest.fail("Overflow must be rejected first"))
    error("mission_pass_points_overflow", lambda: case.points.award(state(),
        [definition(point=0x7FFFFFFF), definition(2, point=1)], NOW))


def test_receipt_validation_never_reads_clock_or_rechecks_current_window(case, monkeypatch):
    definitions = [definition()]
    updated, audit = case.points.award(state(997), definitions, NOW)
    before = deepcopy((updated, definitions, audit))
    monkeypatch.setattr(module, "instant", lambda *_: pytest.fail("Replay must not interpret time"))
    monkeypatch.setattr(case.catalog, "is_eligible", lambda *_: pytest.fail("Replay must not recalculate eligibility"))
    case.points.validate_receipt(updated, definitions, audit)
    assert (updated, definitions, audit) == before


@pytest.mark.parametrize("field,value", [("requested", 11), ("credited", 3), ("before", 996),
    ("after", 998), ("capped", 7), ("inactive", 1), ("inactive", 10), ("pass_id", "second"),
    ("credited", True), ("capped", -1), ("before", 0x80000000), ("after", "999")])
def test_receipt_arithmetic_and_identity_tampering_is_rejected(case, field, value):
    updated, audit = case.points.award(state(997), [definition()], NOW)
    audit[0][field] = value
    error("mission_pass_receipt_invalid", lambda: case.points.validate_receipt(updated, [definition()], audit))


@pytest.mark.parametrize("shape", ["missing", "duplicate", "extra_field", "missing_field", "tuple", "none"])
def test_receipt_requires_exact_unique_pass_rows(case, shape):
    updated, audit = case.points.award(state(), [definition()], NOW)
    if shape == "missing":
        audit = []
    elif shape == "duplicate":
        audit *= 2
    elif shape == "extra_field":
        audit[0]["debt"] = 1
    elif shape == "missing_field":
        del audit[0]["inactive"]
    elif shape == "tuple":
        audit = tuple(audit)
    else:
        audit = None
    error("mission_pass_receipt_invalid", lambda: case.points.validate_receipt(updated, [definition()], audit))


def test_receipt_is_bound_to_definition_totals_and_current_inventory(case):
    updated, audit = case.points.award(state(), [definition()], NOW)
    error("mission_pass_receipt_invalid", lambda: case.points.validate_receipt(updated, [definition(point=11)], audit))
    updated["tables"]["possessions"][-1]["Count"] += 1
    error("mission_pass_receipt_invalid", lambda: case.points.validate_receipt(updated, [definition()], audit))


@pytest.mark.parametrize("drift", ["service_catalog", "service_possessions", "catalog_version", "item_version", "stack", "entry"])
def test_dependency_drift_is_explicit_and_status_is_safe(case, drift):
    if drift == "service_catalog":
        case.service.catalog = copy(case.catalog)
    elif drift == "service_possessions":
        case.service.possessions = copy(case.possessions)
    elif drift == "catalog_version":
        case.catalog._content_version = "b" * 64
    elif drift == "item_version":
        case.possessions.asset_sha256 = "b" * 64
    elif drift == "stack":
        case.possessions._definitions["normal"] = 998
    else:
        case.catalog._by_id = MappingProxyType({**case.catalog.by_id,
            "normal": replace(case.catalog.get("normal"), status=2)})
    assert case.points.status()["ready"] is False
    error("mission_pass_dependencies_changed", lambda: case.points.award(state(), [], NOW))


def test_readiness_never_builds_the_possession_status(case, monkeypatch):
    monkeypatch.setattr(case.possessions, "status", lambda: pytest.fail("readiness must not build the possession status"))
    case.points._ready()
    updated, audit = case.points.award(state(), [definition()], NOW)
    case.points.validate_receipt(updated, [definition()], audit)
    assert case.points.status()["ready"] is True
    monkeypatch.setattr(case.possessions, "issues", {"item_definitions": "changed"})
    error("mission_pass_dependencies_changed", case.points._ready)


def test_readiness_checks_the_pass_catalog_once_for_all_passes(case, monkeypatch):
    checks, original = [], case.catalog._ready
    monkeypatch.setattr(case.catalog, "_ready", lambda: checks.append(1) or original())
    case.points._ready()
    assert len(case.points._entries) == 2 and checks == [1]


def test_mid_credit_dependency_change_is_detected(case, monkeypatch):
    original = case.possessions.credit
    def changed(saved, grants):
        result = original(saved, grants)
        case.possessions.asset_sha256 = "b" * 64
        return result
    monkeypatch.setattr(case.possessions, "credit", changed)
    saved = state()
    before = deepcopy(saved)
    error("mission_pass_dependencies_changed", lambda: case.points.award(saved, [definition()], NOW))
    assert saved == before


def test_actual_six_passes_keep_closed_inventory_unchanged(current):
    engine = MissionPassPoints(SimpleNamespace(catalog=current, possessions=current._possessions))
    definitions = [definition(index + 1, entry.item_id, 9) for index, entry in enumerate(current.passes)]
    original = {"bootstrap_schema": 1, "tables": {"possessions": []}}
    updated, audit = engine.award(original, definitions, NOW)
    assert updated == original
    assert len(audit) == 6 and all(row["inactive"] == 9 and row["credited"] == row["capped"] == 0 for row in audit)
    engine.validate_receipt(updated, definitions, audit)
    active = copy(current)
    active._passes = tuple(replace(entry, status=1) for entry in current.passes)
    active._by_id = MappingProxyType({entry.item_id: entry for entry in active.passes})
    active._content_version = hashlib.sha256((current.content_version + ":active").encode()).hexdigest()
    active_engine = MissionPassPoints(SimpleNamespace(catalog=active, possessions=current._possessions))
    assert active_engine.content_version != engine.content_version
    updated, audit = active_engine.award(original, definitions, NOW)
    assert len(updated["tables"]["possessions"]) == 6
    assert all(row["Count"] == 9 for row in updated["tables"]["possessions"])
    assert all(row["credited"] == 9 and row["inactive"] == row["capped"] == 0 for row in audit)
