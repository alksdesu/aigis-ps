from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from threading import Barrier, Lock
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

import aigis_ps.background_service as module
import aigis_ps.request_ledger as ledger
from aigis_ps.alxml import Column, Container, loads
from aigis_ps.background_catalog import LEGACY_WINDOW_RULE, BackgroundCatalog
from aigis_ps.background_service import BackgroundService
from aigis_ps.background_state import background_response, read_backgrounds
from aigis_ps.business_calendar import NATIVE_DATETIME
from aigis_ps.request_ledger import HISTORY_LIMIT
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.protocol import ProtocolRegistry, encode_wire, prepare_request
from aigis_ps.storage import Store
from aigis_ps.system_literals import encode_command
from aigis_ps.table_cell_patch import patch_table_cells
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, 34, 56, 500000, tzinfo=JST)
EVENT_CATEGORY = -1777172756


def real_catalog(bundle):
    return BackgroundCatalog(bundle.gifts.catalog, lambda name: bundle.native_cdn.assets.read(name)[0])


@pytest.fixture(scope="module")
def catalog(bundle):
    return real_catalog(bundle)


@pytest.fixture
def case(bundle, catalog, tmp_path):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.background_provider = background_response
    own_catalog = copy(catalog)
    service = BackgroundService(bootstrap, bundle.player_state, own_catalog, clock=lambda: NOW)
    initial = bundle.player_state.initialize(bootstrap.initial_state())
    initial["tables"]["config"][0]["_GuideUnitIndex"] = 9999
    initial["sentinel"] = {"nested": ["unchanged"]}
    return SimpleNamespace(service=service, store=Store(tmp_path / "backgrounds.sqlite3", initial),
                           bundle=bundle, catalog=own_catalog)


def message(case, name="system_default", card_ids=(0,), *, tick=1000, columns=None, nonce=None):
    if columns is None:
        columns = (Column("CardID", "I", card_ids), Column("ConfigID", "S", (name,) * len(card_ids)))
    envelope = request(case.store, 135, columns, tick=tick)
    if nonce is not None:
        document = deepcopy(envelope.document)
        document.find("GPVI/V").text = str(nonce)
        for child in list(document):
            if child.tag in ("VN", "ST", "HS"):
                document.remove(child)
        xml = prepare_request(ET.tostring(document, encoding="utf-8"), envelope.profile.st)
        envelope = ProtocolRegistry.load().decode(envelope.profile.route, encode_wire(xml, envelope.profile.request_key))
    return envelope


def change(case, callback):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    callback(state)
    case.store.update_state(state, profile.revision)


def overwrite(case, callback):
    state = case.store.profile().state
    callback(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def fail(case, envelope, code=None):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.save(envelope, case.store)
    if code is not None:
        assert caught.value.code == code
    assert case.store.profile() == before
    return caught.value


def groups(payload):
    return {node.name: node for node in loads(payload).children}


def add_guide(case):
    definition = next(row for row in case.catalog.definitions if row.card_id and row.category == -1224326894)
    profile = case.store.profile()
    state, units = case.bundle.player_state.credit_units(profile.state, [
        {"card_id": definition.card_id, "level": 1, "count": 1}])
    state["tables"]["config"][0]["_GuideUnitIndex"] = units[0]["GetIndex"]
    case.store.update_state(state, profile.revision)
    return definition, units[0]


def test_real_status_and_save_preserve_unrelated_state(case):
    status = case.service.status()
    assert status["ready"] and status["catalog"]["definition_count"] == 53
    assert status["implemented_operations"] == [135] and status["native_tested"] is False
    assert case.service.handlers() == {135: case.service.save}
    before = case.store.profile()
    payload = case.service.save(message(case), case.store)
    after = case.store.profile()
    expected = deepcopy(before.state)
    expected["tables"]["backgrounds"] = [{"CardID": 0, "ConfigID": "system_default", "SaveTime": "2026/11/01 12:34:56"}]
    expected["backgrounds_schema"] = 1
    assert {key: value for key, value in after.state.items() if key != "background_history"} == expected
    assert after.revision == before.revision + 1
    assert set(groups(payload)) == {"SYSTEM", "PRM"}
    assert groups(payload)["PRM"] == background_response(after.state)


def test_private_room_saves_global_and_current_guide_without_changing_roster(case):
    definition, unit = add_guide(case)
    before = case.store.profile()
    payload = case.service.save(message(case, definition.name, (0, definition.card_id)), case.store)
    after = case.store.profile()
    assert after.state["player_state"] == before.state["player_state"]
    assert after.state["tables"]["config"] == before.state["tables"]["config"]
    assert groups(payload)["PRM"].children[0].values == (0, definition.card_id)
    assert groups(payload)["PRM"].children[1].values == (definition.name, definition.name)
    assert all(row["SaveTime"] == "2026/11/01 12:34:56" for row in read_backgrounds(after.state))
    assert unit["UniqueID"] > 0


def test_fixed_background_only_changes_global_and_preserves_old_guide_history(case):
    definition, _ = add_guide(case)
    case.service.save(message(case, definition.name, (0, definition.card_id)), case.store)
    payload = case.service.save(message(case, "system_fix", tick=1001), case.store)
    rows = {row["CardID"]: row for row in read_backgrounds(case.store.profile().state)}
    assert rows[0]["ConfigID"] == "system_fix"
    assert rows[definition.card_id]["ConfigID"] == definition.name
    assert groups(payload)["PRM"].children[0].values == (0,)


@pytest.mark.parametrize("mode", ["global_only", "wrong_guide", "fixed_with_guide", "private_no_guide"])
def test_actual_guide_and_fixed_selection_scope_are_enforced(case, mode):
    definition, _ = add_guide(case)
    if mode == "global_only":
        envelope = message(case)
    elif mode == "wrong_guide":
        envelope = message(case, definition.name, (0, 1))
    elif mode == "fixed_with_guide":
        envelope = message(case, "system_fix", (0, definition.card_id))
    else:
        change(case, lambda state: state["tables"]["config"][0].update(_GuideUnitIndex=9999))
        envelope = message(case, definition.name)
    assert fail(case, envelope).code in ("background_guide_mismatch", "background_state_invalid")


@pytest.mark.parametrize("mode", ["unknown", "internal", "expired"])
def test_current_catalog_rejects_unavailable_backgrounds(case, mode):
    if mode == "unknown":
        name, code = "not_in_catalog", "background_unknown"
    elif mode == "internal":
        name = next(row.name for row in case.catalog.definitions if row.category not in (-1524803586, -1777172756, -1224326894, 463396108))
        code = "background_unsupported"
    else:
        definition = next(row for row in case.catalog.definitions if row.end is not None)
        case.service.clock = lambda: definition.end + timedelta(seconds=1)
        name, code = definition.name, "background_unavailable"
    fail(case, message(case, name), code)


@pytest.mark.parametrize("columns", [
    (), (Column("CardID", "I", (0,)),), (Column("ConfigID", "S", ("system_default",)),),
    (Column("CardID", "I", ()), Column("ConfigID", "S", ())),
    (Column("CardID", "I", (0, 1, 2)), Column("ConfigID", "S", ("system_default",) * 3)),
    (Column("CardID", "I", (0, 1)), Column("ConfigID", "S", ("system_default",))),
    (Column("CardID", "I", (0, 1)), Column("ConfigID", "S", ("system_default", "system_fix"))),
    (Column("CardID", "I", (0, 0)), Column("ConfigID", "S", ("system_default",) * 2)),
    (Column("CardID", "I", (1, 0)), Column("ConfigID", "S", ("system_default",) * 2)),
    (Column("CardID", "I", (0, -1)), Column("ConfigID", "S", ("system_default",) * 2)),
    (Column("CardID", "S", ("0",)), Column("ConfigID", "S", ("system_default",))),
    (Column("CardID", "B", (False,)), Column("ConfigID", "S", ("system_default",))),
    (Column("CardID", "I", (0,)), Column("ConfigID", "I", (1,))),
    (Column("CardID", "I", (0,)), Column("ConfigID", "S", ("",))),
    (Column("CardID", "I", (0,)), Column("ConfigID", "S", ("x" * 257,))),
    (Container("CardID", (Column("Value", "I", (0,)),)), Column("ConfigID", "S", ("system_default",))),
])
def test_strict_native_request_shape_rejects_before_writes(case, columns):
    fail(case, message(case, columns=columns), "invalid_request_fields")


@pytest.mark.parametrize("kind", ["extra", "duplicate", "nested", "mixed", "wrong_root", "wrong_operation"])
def test_unknown_duplicate_and_mixed_fields_are_rejected(case, kind):
    envelope = message(case)
    if kind == "extra":
        ET.SubElement(envelope.document, "Unexpected")
    elif kind == "duplicate":
        envelope.document.append(deepcopy(envelope.document.find("CardID")))
    elif kind == "nested":
        ET.SubElement(envelope.document.find("CardID/V"), "Extra")
    elif kind == "mixed":
        envelope.document.find("ConfigID").text = "unexpected"
    elif kind == "wrong_root":
        envelope.document.tag = "Other"
    else:
        envelope = request(case.store, 121, (Column("A7", "I", (0,)),))
    fail(case, envelope, "invalid_request_fields")


@pytest.mark.parametrize("field", ["UI", "UK", "MK"])
def test_account_and_method_identity_are_checked(case, field):
    envelope = message(case)
    envelope.document.find(field + "/V").text = "another_account"
    assert fail(case, envelope).status == 403


@pytest.mark.parametrize("field", ["CT", "UI", "UK", "MK", "PFN", "GPVI", "GPOT", "PlatformCode"])
def test_public_header_types_are_strict(case, field):
    envelope = message(case)
    envelope.document.find(field).set("T", "B")
    fail(case, envelope, "invalid_request_fields")


def test_new_same_value_action_in_same_second_commits_but_exact_retry_does_not_read_clock(case):
    calls = []
    def clock():
        calls.append(1)
        return NOW
    case.service.clock = clock
    first = message(case, nonce="first")
    first_response = case.service.save(first, case.store)
    first_state = case.store.profile()
    assert case.service.save(first, case.store) == first_response
    assert case.store.profile() == first_state and len(calls) == 1
    second = message(case, nonce="second")
    case.service.save(second, case.store)
    second_state = case.store.profile()
    assert second_state.revision == first_state.revision + 1
    assert read_backgrounds(second_state.state) == read_backgrounds(first_state.state)
    assert len(second_state.state["background_history"]["receipts"]) == 2
    assert len(calls) == 2
    fail(case, first, "background_retry_stale")


def test_new_later_same_value_action_refreshes_time(case):
    case.service.save(message(case), case.store)
    case.service.clock = lambda: NOW + timedelta(days=1)
    case.service.save(message(case, tick=1001), case.store)
    assert read_backgrounds(case.store.profile().state)[0]["SaveTime"] == "2026/11/02 12:34:56"


def test_full_readback_and_reopen_replay_keep_first_time(case):
    envelope = message(case)
    payload = case.service.save(envelope, case.store)
    saved = case.store.profile()
    reopened = Store(case.store.path)
    def forbidden_clock():
        raise AssertionError("An exact retry must not read the clock")
    service = BackgroundService(case.service.bootstrap, case.bundle.player_state, case.catalog, clock=forbidden_clock)
    assert service.save(envelope, reopened) == payload
    assert reopened.profile() == saved
    startup = service.bootstrap.configuration_database(request(reopened, 137, ()), reopened)
    assert groups(startup)["PRM"] == background_response(saved.state)


def test_unrelated_and_other_background_actions_invalidate_old_receipts(case):
    first = message(case)
    case.service.save(first, case.store)
    case.service.save(message(case, "system_fix", tick=1001), case.store)
    fail(case, first, "background_retry_stale")
    current = message(case, "system_fix", tick=1002)
    case.service.save(current, case.store)
    change(case, lambda state: state["sentinel"]["nested"].append("later"))
    fail(case, current, "background_retry_stale")


@pytest.mark.parametrize("kind", ["bad_schema", "unversioned", "bad_time", "duplicate", "bad_player", "bad_config"])
def test_invalid_persisted_state_cannot_be_overwritten(case, kind):
    def corrupt(state):
        if kind == "bad_player":
            state["player_state"]["units"] = []
        elif kind == "bad_config":
            state["tables"]["config"][0]["_GuideUnitIndex"] = "bad"
        else:
            state["tables"]["backgrounds"] = [{"CardID": 0, "ConfigID": "old", "SaveTime": "2020/01/01 00:00:00"}]
            state["backgrounds_schema"] = 1
            if kind == "bad_schema":
                state["backgrounds_schema"] = 2
            elif kind == "unversioned":
                state.pop("backgrounds_schema")
            elif kind == "bad_time":
                state["tables"]["backgrounds"][0]["SaveTime"] = "2020/02/30 00:00:00"
            else:
                state["tables"]["backgrounds"] *= 2
    change(case, corrupt)
    fail(case, message(case))


@pytest.mark.parametrize("kind", ["response", "state_digest", "actual_state", "future_revision", "zero_revision",
    "duplicate", "client_time", "processed", "schema", "bad_content", "nonstring_response"])
def test_receipt_corruption_is_rejected_without_writes(case, kind):
    envelope = message(case)
    case.service.save(envelope, case.store)
    def corrupt(state):
        history = state["background_history"]
        entry = history["receipts"][0]
        if kind == "response":
            entry["response"] = entry["response"].replace("system_default", "system_fix")
        elif kind == "state_digest":
            entry["state"] = "0" * 64
        elif kind == "actual_state":
            state["sentinel"]["nested"].append("corruption")
        elif kind == "future_revision":
            entry["revision"] += 1
        elif kind == "zero_revision":
            entry["revision"] = 0
        elif kind == "duplicate":
            history["receipts"].append(deepcopy(entry))
        elif kind == "client_time":
            entry["client_time"] = 1000
        elif kind == "processed":
            history["processed_keys"] = []
        elif kind == "schema":
            history["schema_version"] = True
        elif kind == "bad_content":
            entry["content"] = "not_a_hash"
        else:
            entry["response"] = None
    overwrite(case, corrupt)
    fail(case, envelope, "background_history_invalid")


def test_compatible_shaped_old_content_receipt_is_stale(case):
    envelope = message(case)
    case.service.save(envelope, case.store)
    overwrite(case, lambda state: state["background_history"]["receipts"][0].update(content="0" * 64))
    fail(case, envelope, "background_retry_stale")


def test_dependency_change_rejects_new_action_and_cached_replay(case, monkeypatch):
    envelope = message(case)
    case.service.save(envelope, case.store)
    monkeypatch.setattr(case.catalog, "_content_version", "0" * 64)
    fail(case, envelope, "background_dependencies_changed")
    fail(case, message(case, tick=1001), "background_dependencies_changed")
    assert case.service.status()["ready"] is False


def test_readiness_asks_the_catalog_without_building_its_status(case, monkeypatch):
    monkeypatch.setattr(case.catalog, "status", lambda: pytest.fail("Background readiness built the catalog status"))
    case.service.save(message(case), case.store)
    monkeypatch.setattr(case.catalog, "is_ready", lambda: False)
    fail(case, message(case, "system_fix", tick=1001), "background_dependencies_changed")


@pytest.mark.parametrize("number", [135, 137])
@pytest.mark.parametrize("cached", [False, True])
def test_both_native_response_capacity_checks_are_atomic(case, number, cached):
    envelope = message(case)
    if cached:
        case.service.save(envelope, case.store)
    case.service._profiles[number] = replace(case.service._profiles[number], response_limit=1)
    fail(case, envelope, "response_too_large")


def test_receipt_storage_capacity_is_checked_before_commit(case, monkeypatch):
    monkeypatch.setattr(module, "RESPONSE_LIMIT", 1)
    fail(case, message(case), "background_response_capacity")


@pytest.mark.parametrize("cached", [False, True])
def test_startup_provider_must_match_the_complete_saved_snapshot(case, cached):
    envelope = message(case)
    if cached:
        case.service.save(envelope, case.store)
    case.service.bootstrap.background_provider = lambda _: Container("PRM")
    fail(case, envelope, "background_provider_inconsistent")


def test_clock_rollback_against_any_historical_row_is_atomic(case):
    change(case, lambda state: state.update(backgrounds_schema=1))
    change(case, lambda state: state["tables"].update(backgrounds=[
        {"CardID": 12345, "ConfigID": "retired_unknown", "SaveTime": "2026/11/02 00:00:00"}]))
    fail(case, message(case), "background_clock_rollback")


def test_known_history_is_preserved_even_without_current_character(case):
    old = {"CardID": 12345, "ConfigID": "retired_unknown", "SaveTime": "2020/01/01 00:00:00"}
    change(case, lambda state: state.update(backgrounds_schema=1))
    change(case, lambda state: state["tables"].update(backgrounds=[deepcopy(old)]))
    case.service.save(message(case), case.store)
    assert old in read_backgrounds(case.store.profile().state)


def test_cas_retry_preserves_concurrent_state_and_reads_clock_once(case, monkeypatch):
    original = case.store.update_state
    updates, clocks = [], []
    def clock():
        clocks.append(1)
        return NOW
    case.service.clock = clock
    def conflict(state, revision):
        updates.append(revision)
        if len(updates) == 1:
            current = case.store.profile()
            changed = deepcopy(current.state)
            changed["sentinel"]["nested"].append("concurrent")
            original(changed, current.revision)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    case.service.save(message(case), case.store)
    state = case.store.profile().state
    assert state["sentinel"]["nested"] == ["unchanged", "concurrent"]
    assert len(state["background_history"]["receipts"]) == 1
    assert len(updates) == 2 and len(clocks) == 1


def test_cas_retry_rechecks_changed_guide_instead_of_overwriting_it(case, monkeypatch):
    definition, _ = add_guide(case)
    envelope = message(case, definition.name, (0, definition.card_id))
    original = case.store.update_state
    def conflict(state, revision):
        current = case.store.profile()
        changed = deepcopy(current.state)
        changed["tables"]["config"][0]["_GuideUnitIndex"] = 9999
        original(changed, current.revision)
        raise ValueError("Concurrent guide change")
    monkeypatch.setattr(case.store, "update_state", conflict)
    with pytest.raises(ProtocolError) as caught:
        case.service.save(envelope, case.store)
    assert caught.value.code == "background_guide_mismatch"
    assert read_backgrounds(case.store.profile().state) == []
    assert "background_history" not in case.store.profile().state


def test_repeated_cas_failure_attempts_three_times_without_write(case, monkeypatch):
    calls = []
    def conflict(*args):
        calls.append(1)
        raise ValueError("Concurrent update")
    monkeypatch.setattr(case.store, "update_state", conflict)
    fail(case, message(case), "profile_changed")
    assert len(calls) == 3


def test_overlapping_identical_requests_commit_only_once(case, monkeypatch):
    original = case.store.update_state
    barrier, lock = Barrier(2), Lock()
    calls = []
    def concurrent(state, revision):
        with lock:
            calls.append(revision)
            wait = len(calls) <= 2
        if wait:
            barrier.wait(timeout=10)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", concurrent)
    envelope = message(case)
    before = case.store.profile()
    with ThreadPoolExecutor(max_workers=2) as pool:
        payloads = list(pool.map(lambda _: case.service.save(envelope, case.store), range(2)))
    assert payloads[0] == payloads[1]
    assert case.store.profile().revision == before.revision + 1
    assert len(case.store.profile().state["background_history"]["receipts"]) == 1


def test_history_eviction_preserves_stale_request_protection(case, monkeypatch):
    monkeypatch.setattr(case.service, "_preflight", lambda *args: None)
    first = message(case)
    case.service.save(first, case.store)
    for tick in range(1001, 1001 + HISTORY_LIMIT):
        case.service.save(message(case, tick=tick), case.store)
    assert len(case.store.profile().state["background_history"]["receipts"]) == HISTORY_LIMIT
    fail(case, first, "background_request_stale")


def test_processed_digests_outlive_receipts_and_never_block_new_actions(case, monkeypatch):
    monkeypatch.setattr(case.service, "_preflight", lambda *args: None)
    first = message(case, nonce=0)
    for index in range(ledger.PROCESSED_LIMIT):
        case.service.save(first if index == 0 else message(case, nonce=index), case.store)
    history = case.store.profile().state["background_history"]
    assert len(history["receipts"]) == HISTORY_LIMIT
    assert len(history["processed_keys"]) == ledger.PROCESSED_LIMIT
    fail(case, first, "background_request_stale")
    case.service.save(message(case, nonce="next"), case.store)
    assert len(case.store.profile().state["background_history"]["processed_keys"]) == ledger.PROCESSED_LIMIT


def test_rolled_back_client_clock_saves_a_new_selection(case):
    later = message(case, tick=0x7FFFFFFF)
    case.service.save(later, case.store)
    case.service.save(message(case, "system_fix", tick=-0x80000000), case.store)
    case.service.save(message(case, tick=0x7FFFFFFE), case.store)
    assert read_backgrounds(case.store.profile().state)[0]["ConfigID"] == "system_default"
    fail(case, later, "background_retry_stale")


def test_watermark_history_upgrades_and_replays_its_receipt(case):
    envelope = message(case, tick=2000)
    payload = case.service.save(envelope, case.store)

    def legacy(state):
        history = state["background_history"]
        state["background_history"] = {"schema_version": ledger.LEGACY_SCHEMA_VERSION, "last_client_time": 2000,
            "seen_keys": list(history["processed_keys"]),
            "receipts": [{**entry, "client_time": 2000} for entry in history["receipts"]]}
    overwrite(case, legacy)
    saved = case.store.profile()
    assert case.service.save(envelope, case.store) == payload
    assert case.store.profile() == saved
    case.service.save(message(case, "system_fix", tick=1000), case.store)
    history = case.store.profile().state["background_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION
    assert history["processed_keys"] == [entry["request"] for entry in history["receipts"]]


def event_definition(case):
    return next(row for row in case.catalog.definitions if row.card_id == 0 and row.end is not None
                and row.category == EVENT_CATEGORY)


def test_event_expiry_does_not_reject_an_exact_committed_retry(case):
    definition = event_definition(case)
    case.service.clock = lambda: definition.end
    envelope = message(case, definition.name)
    payload = case.service.save(envelope, case.store)
    saved = case.store.profile()
    case.service.clock = lambda: definition.end + timedelta(seconds=1)
    assert case.service.save(envelope, case.store) == payload
    assert case.store.profile() == saved
    fail(case, message(case, definition.name, tick=1001), "background_unavailable")


def test_event_end_includes_its_whole_client_clock_second(case):
    definition = event_definition(case)
    last = definition.end + timedelta(microseconds=999999)
    case.service.clock = lambda: last
    case.service.save(message(case, definition.name), case.store)
    assert read_backgrounds(case.store.profile().state) == [
        {"CardID": 0, "ConfigID": definition.name, "SaveTime": last.strftime(NATIVE_DATETIME)}]
    case.service.clock = lambda: definition.end + timedelta(seconds=1)
    fail(case, message(case, definition.name, tick=1001), "background_unavailable")


def test_receipt_of_the_jst_window_rule_replays_its_saved_bytes(case, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr("aigis_ps.background_catalog.WINDOW_RULE", LEGACY_WINDOW_RULE)
        legacy = real_catalog(case.bundle)
    earlier = BackgroundService(case.service.bootstrap, case.bundle.player_state, legacy, clock=lambda: NOW)
    assert earlier.content_version in case.service.replayable_content_versions() - {case.service.content_version}
    envelope = message(case)
    payload = earlier.save(envelope, case.store)
    saved = case.store.profile()
    assert saved.state["background_history"]["receipts"][0]["content"] == earlier.content_version
    assert case.service.save(envelope, case.store) == payload
    assert case.store.profile() == saved
    overwrite(case, lambda state: state["background_history"]["receipts"][0].update(content="f" * 64))
    fail(case, envelope, "background_retry_stale")


PAGE_BACKGROUNDS = {"system_unit", "system_temple", "system_bar", "tower_110001", "tower_110002", "tower_110003",
                    "fame_poratal", "fame_library", "fame_deposit", "fame_withdraw"}
BARRACKS_FILTER = ('MnObj_SetParam("FilterType=Category");\n'
                   'MnObj_SetParam("FilterItem=通常,季節背景,人気投票,システム固定,兵舎");')


@pytest.fixture(scope="module")
def barracks_catalog(bundle, catalog):
    script, = catalog.status()["categories"]["scripts"]

    def read(name):
        raw = bundle.native_cdn.assets.read(name)[0]
        if name != script["table"]:
            return raw
        return patch_table_cells(raw, {script["row"]: {"_Cmnd": encode_command(BARRACKS_FILTER)}})
    return BackgroundCatalog(bundle.gifts.catalog, read)


def saved_row(card_id, name):
    return {"CardID": card_id, "ConfigID": name, "SaveTime": "2020/01/01 00:00:00"}


def test_status_explains_that_other_pages_never_save(case):
    status = case.service.status()
    assert "only 135 sender" in status["other_pages"] and "Read-only" in status["diagnostics"]
    assert status["catalog"]["categories"]["source"] == "resource_script"


def test_every_other_page_background_is_refused_with_an_explicit_reason(case):
    pages = [row for row in case.catalog.definitions if row.category not in case.catalog.categories]
    assert {row.name for row in pages} == PAGE_BACKGROUNDS
    for definition in pages:
        error = fail(case, message(case, definition.name), "background_unsupported")
        assert error.status == 501 and "another native page" in str(error)


def test_resource_filter_opening_barracks_saves_and_reloads_it(case, barracks_catalog):
    service = BackgroundService(case.service.bootstrap, case.bundle.player_state, barracks_catalog, clock=lambda: NOW)
    assert service.content_version != case.service.content_version
    envelope = message(case, "system_unit")
    payload = service.save(envelope, case.store)
    state = case.store.profile().state
    assert read_backgrounds(state) == [{"CardID": 0, "ConfigID": "system_unit", "SaveTime": "2026/11/01 12:34:56"}]
    assert groups(payload)["PRM"] == background_response(state)
    startup = service.bootstrap.configuration_database(request(case.store, 137, ()), case.store)
    assert groups(startup)["PRM"] == background_response(state)
    assert service.save(envelope, case.store) == payload
    assert service.diagnose(state)["abnormal_rows"] == 0
    assert case.service.diagnose(state)["kinds"] == {"page_background": 1}
    fail(case, envelope, "background_retry_stale")


def test_saving_keeps_abnormal_rows_of_other_keys_and_diagnose_reports_them(case):
    cards = case.bundle.gifts.catalog
    private = next(row for row in case.catalog.definitions if row.card_id)
    normalized = [card for card in cards.card_ids if cards.card(card)["OriginID"] == 0 and card != private.card_id]
    variant = next(card for card in cards.card_ids if cards.card(card)["OriginID"])
    assert cards.card(private.card_id)["OriginID"] == 0 and 12345 not in cards.card_ids
    page = next(row.name for row in case.catalog.definitions if row.name in PAGE_BACKGROUNDS)
    rows = [saved_row(private.card_id, private.name), saved_row(normalized[0], private.name),
            saved_row(12345, "retired_unknown"), saved_row(variant, "system_default"),
            saved_row(normalized[1], "system_fix"), saved_row(normalized[2], page)]
    change(case, lambda state: state.update(backgrounds_schema=1))
    change(case, lambda state: state["tables"].update(backgrounds=deepcopy(rows)))
    before = case.store.profile()
    report = case.service.diagnose(before.state)
    assert case.store.profile() == before
    assert report == {"rows": 6, "abnormal_rows": 5, "truncated": False,
                      "kinds": {"unknown_name": 1, "page_background": 1, "fixed_outside_global": 1,
                                "non_normalized_key": 2, "private_room_mismatch": 1},
                      "findings": list(case.catalog.audit(rows))}
    case.service.save(message(case), case.store)
    after = case.store.profile().state
    assert [row for row in read_backgrounds(after) if row["CardID"] != 0] == rows
    assert case.service.diagnose(after)["findings"] == report["findings"]
    startup = case.service.bootstrap.configuration_database(request(case.store, 137, ()), case.store)
    assert groups(startup)["PRM"] == background_response(after)


def test_diagnose_bounds_its_listing_and_rejects_invalid_storage(case, monkeypatch):
    rows = [saved_row(0, "retired_0"), *(saved_row(index, f"retired_{index}") for index in range(1, 4))]
    change(case, lambda state: state.update(backgrounds_schema=1))
    change(case, lambda state: state["tables"].update(backgrounds=rows))
    monkeypatch.setattr(module, "DIAGNOSTIC_LIMIT", 2)
    report = case.service.diagnose(case.store.profile().state)
    assert (report["abnormal_rows"], len(report["findings"]), report["truncated"]) == (4, 2, True)
    broken = deepcopy(case.store.profile().state)
    broken.pop("backgrounds_schema")
    with pytest.raises(ProtocolError) as error:
        case.service.diagnose(broken)
    assert error.value.code == "background_state_invalid"
