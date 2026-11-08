from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

import aigis_ps.blessing_service as module
import aigis_ps.request_ledger as ledger
from aigis_ps.alxml import Column, Container, loads
from aigis_ps.blessing_service import BlessingService, FILTER_FIELD
from aigis_ps.errors import ProtocolError
from aigis_ps.request_ledger import HISTORY_LIMIT
from aigis_ps.protocol import ProtocolRegistry, encode_wire, prepare_request
from aigis_ps.storage import Store
from test_quest_service import bundle, request


@pytest.fixture(scope="module")
def catalog(bundle):
    from aigis_ps.blessing_catalog import BlessingCatalog
    return BlessingCatalog(bundle.possessions, lambda name: bundle.native_cdn.assets.read(name)[0])


@pytest.fixture
def case(bundle, catalog, tmp_path):
    bootstrap = copy(bundle.bootstrap)
    service = BlessingService(bootstrap, bundle.player_state, bundle.possessions, catalog)
    state = bundle.player_state.initialize(bootstrap.initial_state())
    items = tuple(sorted(catalog.item_ids))[:3]
    state["tables"]["possessions"] = [{"ItemID": item, "Count": 1} for item in items]
    state["tables"]["possessions"].append({"ItemID": "gift_0002_ruby", "Count": 8})
    state["sentinel"] = {"nested": ["unchanged"]}
    return SimpleNamespace(service=service, store=Store(tmp_path / "blessings.sqlite3", state),
                           bundle=bundle, catalog=catalog, items=items)


def message(case, settings=None, selected_filter=None, *, tick=1000, nodes=None, number=148, nonce=None):
    if nodes is None:
        nodes = []
        if selected_filter is not None:
            nodes.append(Column("FI", "I", (selected_filter,)))
        if settings is not None:
            nodes.extend((Column("ItemID", "S", tuple(settings)), Column("Enable", "I", tuple(settings.values()))))
    envelope = request(case.store, number, (Container("BGB", tuple(nodes)),), tick=tick)
    if nonce is not None:
        root = deepcopy(envelope.document)
        root.find("GPVI/V").text = str(nonce)
        for child in list(root):
            if child.tag in ("VN", "ST", "HS"):
                root.remove(child)
        xml = prepare_request(ET.tostring(root, encoding="utf-8"), envelope.profile.st)
        envelope = ProtocolRegistry.load().decode(envelope.profile.route, encode_wire(xml, envelope.profile.request_key))
    return envelope


def nodes(payload):
    return {node.name: node for node in loads(payload).children}


def rows(state):
    return {row["ItemID"]: row for row in state["tables"]["possessions"]}


def change(case, callback):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    callback(state)
    case.store.update_state(state, profile.revision)


def fail(case, envelope, code=None):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.save(envelope, case.store)
    if code is not None:
        assert caught.value.code == code
    assert case.store.profile() == before
    return caught.value


def test_real_catalog_and_service_status(case):
    assert len(case.catalog.item_ids) == 242
    assert case.catalog.filter_values == frozenset((0, 1, 2, 3))
    assert case.service.handlers() == {148: case.service.save}
    status = case.service.status()
    assert status["ready"] and status["implemented_operations"] == [148]
    assert status["native_tested"] is False


@pytest.mark.parametrize("selected_filter", (0, 1, 2, 3))
def test_filter_only_preserves_inventory_and_returns_authoritative_filter(case, selected_filter):
    before = case.store.profile()
    response = case.service.save(message(case, selected_filter=selected_filter), case.store)
    after = case.store.profile()
    assert after.state["tables"]["config"][0][FILTER_FIELD] == selected_filter
    assert after.state["tables"]["possessions"] == before.state["tables"]["possessions"]
    result = nodes(response)
    assert set(result) == {"SYSTEM", "PGC", "POSSESSION"}
    assert result["PGC"].children == (Column("A1", "S", ("FE",)), Column("A2", "S", (str(selected_filter),)))
    assert not result["POSSESSION"].children


def test_mixed_settings_change_only_flags_and_filter(case):
    first, second, _ = case.items
    change(case, lambda state: rows(state)[second].update(Enabled=1))
    before = case.store.profile()
    response = case.service.save(message(case, {first: 1, second: 0}, 3), case.store)
    after = case.store.profile()
    expected = deepcopy(before.state)
    rows(expected)[first]["Enabled"] = 1
    rows(expected)[second]["Enabled"] = 0
    expected["tables"]["config"][0][FILTER_FIELD] = 3
    assert {key: value for key, value in after.state.items() if key != "blessing_history"} == expected
    assert after.revision == before.revision + 1
    assert nodes(response)["POSSESSION"].children == (
        Column("ItemID", "S", (first, second)), Column("Count", "I", (1, 1)), Column("Enabled", "I", (1, 0)))
    assert after.state["player_state"] == before.state["player_state"]


def test_all_current_blessings_can_be_updated_in_one_request(case):
    selected = tuple(sorted(case.catalog.item_ids))
    change(case, lambda state: state["tables"].update(possessions=[{"ItemID": item, "Count": 1} for item in selected]))
    result = nodes(case.service.save(message(case, dict.fromkeys(selected, 1)), case.store))["POSSESSION"]
    assert result.children[0].values == selected
    assert result.children[1].values == (1,) * 242
    assert result.children[2].values == (1,) * 242


@pytest.mark.parametrize("mode", ("empty", "paired_empty", "same_filter", "missing_off"))
def test_noop_never_creates_receipt_or_advances_revision(case, mode):
    before = case.store.profile()
    kwargs = {"settings": {} if mode == "paired_empty" else None,
              "selected_filter": before.state["tables"]["config"][0][FILTER_FIELD] if mode == "same_filter" else None}
    if mode == "missing_off":
        kwargs["settings"] = {case.items[0]: 0}
    envelope = message(case, **kwargs)
    result = case.service.save(envelope, case.store)
    assert case.service.save(envelope, case.store) == result
    assert case.store.profile() == before
    assert "blessing_history" not in before.state
    if mode == "missing_off":
        assert nodes(result)["POSSESSION"].children[2] == Column("Enabled", "I", (0,))


def test_noop_after_change_preserves_existing_history(case):
    case.service.save(message(case, {case.items[0]: 1}), case.store)
    before = case.store.profile()
    case.service.save(message(case, {case.items[0]: 1}, tick=1001), case.store)
    assert case.store.profile() == before


@pytest.mark.parametrize("bad_nodes", [
    (Column("FI", "I", (-1,)),), (Column("FI", "I", (4,)),), (Column("FI", "I", (0, 1)),),
    (Column("FI", "S", ("0",)),), (Column("FI", "B", (False,)),),
    (Column("ItemID", "S", ()),), (Column("Enable", "I", ()),),
    (Column("ItemID", "S", ("a",)), Column("Enable", "I", ())),
    (Column("ItemID", "S", ("a", "a")), Column("Enable", "I", (0, 1))),
    (Column("ItemID", "S", ("",)), Column("Enable", "I", (1,))),
    (Column("ItemID", "S", ("a",)), Column("Enable", "I", (-1,))),
    (Column("ItemID", "S", ("a",)), Column("Enable", "I", (2,))),
    (Column("ItemID", "S", ("a",)), Column("Enable", "B", (True,))),
    (Column("ItemID", "S", ("a",)), Column("Enabled", "I", (1,))),
    (Column("Count", "I", (1,)),), (Column("FI", "I", (0,)), Column("FI", "I", (1,))),
], ids=["negative_filter", "large_filter", "filter_vector", "filter_string", "filter_bool", "names_only",
        "flags_only", "length_mismatch", "duplicate", "empty_name", "negative_flag", "large_flag",
        "bool_flag", "wrong_enabled_name", "count_upload", "duplicate_filter"])
def test_strict_nested_columns(case, bad_nodes):
    fail(case, message(case, nodes=bad_nodes), "invalid_request_fields")


@pytest.mark.parametrize("kind", ("root_unknown", "missing_bgb", "duplicate_bgb", "mixed_text", "typed_bgb", "wrong_profile"))
def test_strict_envelope_shape(case, kind):
    if kind == "root_unknown":
        envelope = request(case.store, 148, (Container("BGB"), Column("UNKNOWN", "I", (1,))))
    elif kind == "missing_bgb":
        envelope = request(case.store, 148, ())
    elif kind == "duplicate_bgb":
        envelope = request(case.store, 148, (Container("BGB"), Container("BGB")))
    else:
        envelope = message(case, number=139 if kind == "wrong_profile" else 148)
        if kind in ("mixed_text", "typed_bgb"):
            root = deepcopy(envelope.document)
            if kind == "mixed_text":
                root.find("BGB").text = "not a column"
            else:
                root.find("BGB").set("T", "I")
            envelope = replace(envelope, document=root)
    fail(case, envelope, "invalid_request_fields")


@pytest.mark.parametrize("field", ("UI", "UK", "MK"))
def test_authentication_precedes_any_state_change(case, field):
    envelope = message(case, {case.items[0]: 1}, 2)
    root = deepcopy(envelope.document)
    root.find(field + "/V").text = "wrong"
    assert fail(case, replace(envelope, document=root)).status == 403


@pytest.mark.parametrize("kind,flag", [("unknown", 1), ("other_effect", 1), ("not_owned", 1), ("not_owned", 0),
                                       ("zero", 1), ("zero", 0)])
def test_real_ownership_and_item_scope_fail_entire_batch(case, kind, flag):
    item = case.items[1]
    if kind == "unknown":
        item = "not_a_real_item"
    elif kind == "other_effect":
        item = "gift_0002_ruby"
    elif kind == "not_owned":
        item = sorted(set(case.catalog.item_ids) - set(case.items))[0]
    elif kind == "zero":
        change(case, lambda state: rows(state)[item].update(Count=0))
    fail(case, message(case, {case.items[0]: 1, item: flag}, 2))


def test_real_full_readback_and_restart(case):
    selected = {case.items[0]: 1, case.items[1]: 0}
    envelope = message(case, selected, 2)
    response = case.service.save(envelope, case.store)
    before = case.store.profile()
    reopened = Store(case.store.path)
    service = BlessingService(case.service.bootstrap, case.bundle.player_state, case.bundle.possessions, case.catalog)
    assert service.save(envelope, reopened) == response
    assert reopened.profile() == before
    for number, callback in ((129, service.bootstrap.initialize_player), (137, service.bootstrap.configuration_database)):
        possession = nodes(callback(request(reopened, number, ()), reopened))["POSSESSION"]
        columns = {column.name: column.values for column in possession.children}
        actual = dict(zip(columns["ItemID"], columns["Enabled"]))
        assert actual[case.items[0]] == 1 and actual[case.items[1]] == 0
    config = nodes(service.bootstrap.config(request(reopened, 36, ()), reopened))
    assert config["FE"].values == (2,)


def test_later_writes_make_old_receipt_stale(case):
    envelope = message(case, {case.items[0]: 1})
    case.service.save(envelope, case.store)
    change(case, lambda state: state["sentinel"]["nested"].append("later"))
    fail(case, envelope, "blessing_retry_stale")


def test_changed_dependency_blocks_replay(case, monkeypatch):
    envelope = message(case, {case.items[0]: 1})
    case.service.save(envelope, case.store)
    monkeypatch.setattr(case.catalog, "content_version", "f" * 64)
    fail(case, envelope, "blessing_dependencies_changed")
    assert not case.service.status()["ready"]


def test_readiness_never_builds_the_dependency_status(case, monkeypatch):
    for dependency in (case.catalog, case.bundle.possessions):
        monkeypatch.setattr(dependency, "status", lambda: pytest.fail("readiness must not build a dependency status"))
    service = BlessingService(case.service.bootstrap, case.bundle.player_state, case.bundle.possessions, case.catalog)
    updated = service.set_settings(case.store.profile().state, 1, {case.items[0]: 1})
    assert rows(updated)[case.items[0]]["Enabled"] == 1
    assert updated["tables"]["config"][0][FILTER_FIELD] == 1


@pytest.mark.parametrize("dependency", ("catalog", "possessions"))
def test_unready_dependencies_keep_the_changed_code(case, monkeypatch, dependency):
    target = case.catalog if dependency == "catalog" else case.bundle.possessions
    monkeypatch.setattr(target, "is_ready", lambda: False)
    fail(case, message(case, selected_filter=1), "blessing_dependencies_changed")
    assert case.service.status()["error"] == "blessing_dependencies_changed"


@pytest.mark.parametrize("kind", ("bad_player", "zero_owned", "bad_filter", "history_null", "bad_schema"))
def test_replay_still_validates_current_dependencies_and_state(case, kind):
    envelope = message(case, {case.items[0]: 1})
    case.service.save(envelope, case.store)
    if kind == "bad_player":
        change(case, lambda state: state["player_state"].update(units=[]))
    elif kind == "zero_owned":
        change(case, lambda state: rows(state)[case.items[0]].update(Count=0))
    elif kind == "bad_filter":
        change(case, lambda state: state["tables"]["config"][0].update({FILTER_FIELD: 99}))
    elif kind == "history_null":
        change(case, lambda state: state.update(blessing_history=None))
    else:
        change(case, lambda state: state["blessing_history"].update(schema_version=True))
    assert fail(case, envelope).code != "blessing_retry_stale"


@pytest.mark.parametrize("kind", ("response", "state_digest", "future_revision", "duplicate", "client_time", "processed"))
def test_receipt_corruption_is_rejected(case, monkeypatch, kind):
    envelope = message(case, {case.items[0]: 1})
    case.service.save(envelope, case.store)
    profile = case.store.profile()
    state = deepcopy(profile.state)
    history = state["blessing_history"]
    receipt = history["receipts"][0]
    if kind == "response":
        receipt["response"] = receipt["response"].replace("<V>1</V>", "<V>0</V>")
    elif kind == "state_digest":
        receipt["state"] = "0" * 64
    elif kind == "future_revision":
        receipt["revision"] += 1
    elif kind == "duplicate":
        history["receipts"].append(deepcopy(receipt))
    elif kind == "client_time":
        receipt["client_time"] = 1000
    else:
        history["processed_keys"] = []
    fake = replace(profile, state=state)
    monkeypatch.setattr(case.store, "profile", lambda: fake)
    fail(case, envelope, "blessing_history_invalid")


def test_evicted_receipt_cannot_reapply_old_settings(case):
    first = message(case, selected_filter=1)
    case.service.save(first, case.store)
    for i in range(1, HISTORY_LIMIT + 2):
        case.service.save(message(case, selected_filter=(i + 1) % 4, tick=1000 + i), case.store)
    history = case.store.profile().state["blessing_history"]
    assert len(history["receipts"]) == HISTORY_LIMIT
    fail(case, first, "blessing_request_stale")


def test_rolled_back_client_clock_saves_new_settings(case):
    later = message(case, selected_filter=1, tick=0x7fffffff)
    case.service.save(later, case.store)
    case.service.save(message(case, selected_filter=2, tick=-0x80000000), case.store)
    case.service.save(message(case, selected_filter=3, tick=-0x80000000 + 86400), case.store)
    case.service.save(message(case, selected_filter=0, tick=0x7ffffffe), case.store)
    case.service.save(message(case, selected_filter=2, tick=-0x80000000, nonce="rolled-back"), case.store)
    assert case.store.profile().state["tables"]["config"][0][FILTER_FIELD] == 2
    fail(case, later, "blessing_retry_stale")


def test_processed_digests_preserve_evicted_keys_without_blocking_new_actions(case, monkeypatch):
    monkeypatch.setattr(case.service, "_preflight", lambda *args: None)
    first = None
    for index in range(ledger.PROCESSED_LIMIT):
        envelope = message(case, selected_filter=(index + 1) % 4, nonce=index)
        first = first or envelope
        case.service.save(envelope, case.store)
    history = case.store.profile().state["blessing_history"]
    assert len(history["processed_keys"]) == ledger.PROCESSED_LIMIT
    assert len(history["receipts"]) == HISTORY_LIMIT
    fail(case, first, "blessing_request_stale")
    case.service.save(message(case, selected_filter=1, nonce="next"), case.store)
    assert case.store.profile().state["tables"]["config"][0][FILTER_FIELD] == 1


def test_watermark_history_upgrades_and_replays_its_receipt(case):
    envelope = message(case, {case.items[0]: 1}, tick=2000)
    payload = case.service.save(envelope, case.store)
    profile = case.store.profile()
    state = deepcopy(profile.state)
    history = state["blessing_history"]
    state["blessing_history"] = {"schema_version": ledger.LEGACY_SCHEMA_VERSION, "last_client_time": 2000,
                                 "seen_keys": list(history["processed_keys"]),
                                 "receipts": [{**entry, "client_time": 2000} for entry in history["receipts"]]}
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    saved = case.store.profile()
    assert case.service.save(envelope, case.store) == payload
    assert case.store.profile() == saved
    case.service.save(message(case, {case.items[0]: 0}, tick=1000), case.store)
    history = case.store.profile().state["blessing_history"]
    assert history["schema_version"] == ledger.SCHEMA_VERSION
    assert history["processed_keys"] == [entry["request"] for entry in history["receipts"]]


@pytest.mark.parametrize("number", (36, 129, 137, 148))
def test_each_serialized_response_is_checked_before_commit(case, monkeypatch, number):
    original = module.encode_response
    def checked(profile, payload):
        if profile.registration_keys == (number,):
            raise ProtocolError("injected_capacity", "Cannot encode selected response", 503)
        return original(profile, payload)
    monkeypatch.setattr(module, "encode_response", checked)
    fail(case, message(case, {case.items[0]: 1}, 2), "injected_capacity")


def test_receipt_size_is_checked_before_commit(case, monkeypatch):
    monkeypatch.setattr(module, "RESPONSE_LIMIT", 8)
    fail(case, message(case, {case.items[0]: 1}, 2), "blessing_response_capacity")


def test_cas_retry_preserves_concurrent_unrelated_write(case, monkeypatch):
    original = case.store.update_state
    calls = 0
    def conflict(state, revision):
        nonlocal calls
        calls += 1
        if calls == 1:
            current = case.store.profile()
            changed = deepcopy(current.state)
            changed["sentinel"]["nested"].append("concurrent")
            original(changed, current.revision)
            raise ValueError("Concurrent write")
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    case.service.save(message(case, {case.items[0]: 1}, 2), case.store)
    state = case.store.profile().state
    assert state["sentinel"]["nested"] == ["unchanged", "concurrent"]
    assert rows(state)[case.items[0]]["Enabled"] == 1
    assert len(state["blessing_history"]["receipts"]) == 1
    assert calls == 2


def test_repeated_cas_failure_leaves_state_untouched(case, monkeypatch):
    calls = 0
    def conflict(*args):
        nonlocal calls
        calls += 1
        raise ValueError("Concurrent write")
    monkeypatch.setattr(case.store, "update_state", conflict)
    fail(case, message(case, {case.items[0]: 1}, 2), "profile_changed")
    assert calls == 3


def test_concurrent_same_request_commits_once(case):
    envelope = message(case, {case.items[0]: 1}, 2)
    before = case.store.profile()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: case.service.save(envelope, case.store), range(2)))
    assert responses[0] == responses[1]
    assert case.store.profile().revision == before.revision + 1
