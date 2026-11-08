from copy import deepcopy
from dataclasses import replace
import logging
import string

import pytest

from aigis_ps.alxml import AlXmlError, Column, Container, dumps, loads
from aigis_ps.bootstrap import STARTUP_COLLECTION_LIMITS
from aigis_ps.errors import ProtocolError
from aigis_ps.filter_state import FilterService, name_hash, read_filters
from aigis_ps.startup_diagnostics import StartupDiagnostics
from aigis_ps.universal_flags import MAX_FLAGS, read_flags
from test_event_pass_service import action, baseline, bundle, change, factory, nodes, request, unchanged, watermark


UNREADY = "event_pass_dependencies_changed"
DAMAGED = "event_pass_state_invalid"
FILTER, SELECTION = "TeamEdit", "1,2"


def save_filter(bootstrap, store):
    group = Container("Filter", (Column("FilterNameHash", "I", (name_hash(FILTER),)),
                                 Column("SaveValue", "S", (SELECTION,))))
    FilterService(bootstrap).save(request(store, 26, (group,)), store)
    return {"FilterName": "", "FilterNameHash": name_hash(FILTER), "SaveValue": SELECTION}


def invalidate_dependencies(case):
    case.service.catalog._content_version = "0" * 64


def unready_case(factory):
    case = factory(10)
    case.service.claim(action(case), case.store)
    case.bootstrap.startup_diagnostics = StartupDiagnostics()
    saved = case.service.response(case.store.profile().state)
    invalidate_dependencies(case)
    return case, saved


def test_dependency_failure_returns_failed_diagnostic_without_rethrow(factory, monkeypatch):
    case = factory(10)
    monkeypatch.setattr(case.service.possessions, "asset_sha256", "0" * 64)
    result = case.service.status()
    assert result["ready"] is False
    assert result["error"] == "event_pass_catalog_changed"
    assert result["catalog"] == {"ready": False, "error": "event_pass_catalog_changed"}
    assert result["implemented_operations"] == [158, 159]


def test_137_serves_the_saved_status_while_dependencies_are_unready(factory, caplog):
    case, saved = unready_case(factory)
    before = case.store.profile()
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    startup = nodes(case.bootstrap.handlers()[137](request(case.store, 137, ()), case.store))
    assert startup["EventPassStatus"] == saved and watermark(case)["ReceivedNormalPoint"] == 10
    assert case.store.profile() == before
    record = case.bootstrap.startup_diagnostics.status()["loads"]["137"]
    assert record["outcome"] == "degraded" and [issue["code"] for issue in record["issues"]] == [UNREADY]
    assert any("Startup load 137" in line and UNREADY in line for line in caplog.messages)


def test_selected_status_158_and_159_keep_refusing_unready_dependencies(factory, caplog):
    case, _ = unready_case(factory)
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    with pytest.raises(ProtocolError) as selected:
        case.service.response(case.store.profile().state, [case.entry.item_id])
    assert selected.value.code == UNREADY
    unchanged(case, lambda: case.service.read(request(case.store, 158, ()), case.store), UNREADY)
    unchanged(case, lambda: case.service.claim(action(case, tick=1001), case.store), UNREADY)
    assert case.bootstrap.startup_diagnostics.status() == {"attention_required": False, "loads": {}}
    assert not any("outside a recorded load" in line for line in caplog.messages)


def test_159_refuses_changed_dependencies_through_its_selected_status(factory, monkeypatch, caplog):
    case = factory(10)
    original = case.service.deliver

    def deliver(state, entry):
        result = original(state, entry)
        invalidate_dependencies(case)
        return result

    monkeypatch.setattr(case.service, "deliver", deliver)
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    unchanged(case, lambda: case.service.claim(action(case), case.store), UNREADY)
    assert any("outside a recorded load" in line and UNREADY in line for line in caplog.messages)


def test_137_still_refuses_a_damaged_saved_status_while_dependencies_are_unready(factory):
    case, _ = unready_case(factory)

    def damage(state):
        row = next(row for row in state["tables"]["event_pass_status"] if row["ID"] == case.entry.item_id)
        row["ReceivedNormalPoint"] += 10

    change(case, damage)
    load = case.bootstrap.handlers()[137]
    unchanged(case, lambda: load(request(case.store, 137, ()), case.store), DAMAGED)
    record = case.bootstrap.startup_diagnostics.status()["loads"]["137"]
    assert record["outcome"] == "refused" and [issue["code"] for issue in record["issues"]] == [UNREADY, DAMAGED]


def test_other_writes_log_the_degraded_startup_check_and_commit(factory, caplog):
    case, saved = unready_case(factory)
    before = case.store.profile()
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    expected = save_filter(case.bootstrap, case.store)
    after = case.store.profile()
    assert after.revision == before.revision + 1 and read_filters(after.state) == [expected]
    assert after.state["tables"]["event_pass_status"] == before.state["tables"]["event_pass_status"]
    assert any("outside a recorded load" in line and UNREADY in line for line in caplog.messages)
    assert case.bootstrap.startup_diagnostics.status()["loads"] == {}
    assert case.service.response(after.state) == saved


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("kind", ["empty", "old", "wrong_value", "wrong_type"])
def test_wrong_provider_blocks_new_claim_and_cached_replay(factory, cached, kind):
    case = factory(10, legacy=(0, 0))
    envelope = action(case)
    old_state = deepcopy(case.store.profile().state)
    canonical = case.service.response
    if cached:
        case.service.claim(envelope, case.store)
    before = case.store.profile()

    def provider(state):
        if kind == "empty":
            return Container("EventPassStatus")
        if kind == "old":
            return canonical(old_state)
        result = canonical(state)
        values = list(result.children)
        normal = values[1]
        if kind == "wrong_value":
            values[1] = Column(normal.name, "I", tuple(value + 1 for value in normal.values))
        else:
            values[1] = Column(normal.name, "S", tuple(str(value) for value in normal.values))
        return Container("EventPassStatus", tuple(values))

    case.bootstrap.event_pass_provider = provider
    with pytest.raises(ProtocolError) as caught:
        case.service.claim(envelope, case.store)
    assert caught.value.code == "event_pass_provider_inconsistent"
    assert case.store.profile() == before


@pytest.mark.parametrize("kind", ["missing", "duplicate"])
def test_startup_requires_exactly_one_event_pass_group(factory, monkeypatch, kind):
    case = factory(10)
    original = case.bootstrap._configuration_response

    def response(profile):
        root = loads(original(profile), limits=STARTUP_COLLECTION_LIMITS)
        groups = [child for child in root.children if child.name == "EventPassStatus"]
        remaining = [child for child in root.children if child.name != "EventPassStatus"]
        if kind == "duplicate":
            remaining.extend(groups * 2)
        return dumps(Container(children=tuple(remaining)), limits=STARTUP_COLLECTION_LIMITS)

    monkeypatch.setattr(case.bootstrap, "_configuration_response", response)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.claim(action(case), case.store)
    assert caught.value.code == "event_pass_provider_inconsistent"
    assert case.store.profile() == before


def test_startup_uses_collection_xml_limits_for_large_valid_flag_snapshot(factory):
    case = factory(10)
    profile = case.store.profile()
    state = deepcopy(profile.state)
    alphabet = string.ascii_letters + string.digits + "_-"
    count = 32769
    assert count < MAX_FLAGS
    names = ["".join(alphabet[(index >> shift) & 63] for shift in (12, 6, 0)) for index in range(count)]
    state["universal_flags_schema"] = 1
    state["tables"]["universal_flags"] = [{"ID": name, "Value": 0} for name in names]
    assert len(read_flags(state)) == count
    case.store.update_state(state, profile.revision)
    raw = case.bootstrap._configuration_response(case.store.profile())
    with pytest.raises(AlXmlError):
        loads(raw)
    parsed = loads(raw, limits=STARTUP_COLLECTION_LIMITS)
    assert next(group for group in parsed.children if group.name == "GF").children[0].values == tuple(names)
    # Isolate XML collection limits from the smaller native compressed transport limit.
    case.service._profiles[137] = replace(case.service._profiles[137], response_limit=512 * 1024)
    before = case.store.profile()
    case.service.claim(action(case), case.store)
    saved = case.store.profile()
    assert saved.revision == before.revision + 1
    assert saved.state["tables"]["universal_flags"] == before.state["tables"]["universal_flags"]
    assert saved.state["tables"]["event_pass_status"][0]["ReceivedNormalPoint"] == 10
