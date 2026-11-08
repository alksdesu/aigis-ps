from copy import deepcopy
import hashlib

import pytest

import aigis_ps.request_ledger as module
from aigis_ps.errors import ProtocolError
from aigis_ps.request_ledger import LEGACY_SCHEMA_VERSION, SCHEMA_VERSION, RequestLedger


NAMESPACE = "fixture_history"
FIELDS = ("request", "content", "state", "revision", "response", "note")
FLAT_NAMES = ("first", "second", "third")


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def ledger(**overrides):
    options = {"fields": FIELDS, "response_limit": 64, "check": lambda entry: entry["note"] in ("ok", "")}
    options.update(overrides)
    return RequestLedger(NAMESPACE, "fixture_history_invalid", "Fixture receipts are invalid", **options)


def nesting_ledger(**overrides):
    options = {"fields": (*FIELDS, "extras"), "legacy_nesting": {"extras": FLAT_NAMES},
               "check": lambda entry: entry["note"] in ("ok", "") and isinstance(entry["extras"], dict)}
    options.update(overrides)
    return ledger(**options)


def receipt(index, revision=None, **changes):
    entry = {"request": digest(f"request-{index}"), "content": digest("content"), "state": digest(f"state-{index}"),
             "revision": index if revision is None else revision, "response": f"<DA>{index}</DA>", "note": "ok"}
    entry.update(changes)
    return entry


def recorded(count, target=None):
    target = {} if target is None else target
    service = ledger()
    for index in range(1, count + 1):
        service.record(target, service.read(target, index - 1), receipt(index))
    return target


def invalid(history, revision=10):
    with pytest.raises(ProtocolError) as caught:
        ledger().read({NAMESPACE: history}, revision)
    assert caught.value.code == "fixture_history_invalid" and caught.value.status == 503


def test_missing_namespace_reads_as_empty_current_schema():
    assert ledger().read({}, 0) == {"schema_version": SCHEMA_VERSION, "receipts": [], "processed_keys": []}


def test_record_appends_receipt_and_processed_digest_in_order():
    state = recorded(3)
    history = ledger().read(state, 3)
    assert history["receipts"] == [receipt(1), receipt(2), receipt(3)]
    assert history["processed_keys"] == [receipt(index)["request"] for index in (1, 2, 3)]
    assert ledger().receipt(history, receipt(2)["request"]) == receipt(2)
    assert ledger().receipt(history, digest("unknown")) is None
    assert ledger().processed(history, receipt(3)["request"])
    assert not ledger().processed(history, digest("unknown"))


def test_read_returns_independent_copy():
    state = recorded(1)
    history = ledger().read(state, 1)
    history["receipts"].clear()
    assert len(state[NAMESPACE]["receipts"]) == 1


def test_evicted_receipt_keeps_its_digest_until_the_processed_window_moves(monkeypatch):
    monkeypatch.setattr(module, "HISTORY_LIMIT", 2)
    monkeypatch.setattr(module, "PROCESSED_LIMIT", 3)
    state = recorded(3)
    history = ledger().read(state, 3)
    assert [entry["request"] for entry in history["receipts"]] == [receipt(2)["request"], receipt(3)["request"]]
    assert ledger().receipt(history, receipt(1)["request"]) is None
    assert ledger().processed(history, receipt(1)["request"])
    ledger().record(state, history, receipt(4))
    history = ledger().read(state, 4)
    assert history["processed_keys"] == [receipt(index)["request"] for index in (2, 3, 4)]
    assert not ledger().processed(history, receipt(1)["request"])


def test_legacy_watermark_history_upgrades_without_client_clock():
    evicted = digest("same-second-evicted")
    legacy = {"schema_version": LEGACY_SCHEMA_VERSION, "last_client_time": -0x80000000,
              "seen_keys": [evicted, receipt(2)["request"]],
              "receipts": [{**receipt(1), "client_time": 0x7FFFFFFF}, {**receipt(2), "client_time": -0x80000000}]}
    history = ledger().read({NAMESPACE: deepcopy(legacy)}, 2)
    assert history == {"schema_version": SCHEMA_VERSION, "receipts": [receipt(1), receipt(2)],
                       "processed_keys": [evicted, receipt(1)["request"], receipt(2)["request"]]}
    state = {NAMESPACE: legacy}
    ledger().record(state, history, receipt(3))
    assert state[NAMESPACE]["processed_keys"][-1] == receipt(3)["request"]
    assert "last_client_time" not in state[NAMESPACE]


def test_legacy_receipts_read_missing_module_fields_as_their_defaults():
    service = ledger(legacy_defaults={"note": ""})
    old = {name: value for name, value in receipt(1).items() if name != "note"}
    legacy = {"schema_version": LEGACY_SCHEMA_VERSION, "last_client_time": 7, "seen_keys": [receipt(2)["request"]],
              "receipts": [{**old, "client_time": 7}, {**receipt(2), "client_time": 7}]}
    history = service.read({NAMESPACE: deepcopy(legacy)}, 2)
    assert history["receipts"] == [{**old, "note": ""}, receipt(2)]
    state = {NAMESPACE: legacy}
    service.record(state, history, receipt(3))
    assert state[NAMESPACE]["receipts"] == [{**old, "note": ""}, receipt(2), receipt(3)]


def test_defaults_never_fill_current_schema_receipts():
    current = {"schema_version": SCHEMA_VERSION, "processed_keys": [receipt(1)["request"]],
               "receipts": [{name: value for name, value in receipt(1).items() if name != "note"}]}
    with pytest.raises(ProtocolError) as caught:
        ledger(legacy_defaults={"note": ""}).read({NAMESPACE: current}, 1)
    assert caught.value.code == "fixture_history_invalid"


@pytest.mark.parametrize("defaults", [{"unknown": None}, {"client_time": 0}, {"request": digest(1)},
                                      {"content": digest(1)}, {"state": digest(1)}, {"revision": 1},
                                      {"response": "<DA/>"}])
def test_legacy_defaults_cannot_fill_unknown_or_ledger_verified_fields(defaults):
    with pytest.raises(ValueError, match="Legacy receipt defaults"):
        ledger(legacy_defaults=defaults)


def test_upgrade_without_nesting_keeps_the_recorded_receipt_order():
    legacy = {"schema_version": LEGACY_SCHEMA_VERSION, "last_client_time": 7, "seen_keys": [],
              "receipts": [{"note": "ok", "client_time": 7, **receipt(1)}]}
    upgraded = ledger().read({NAMESPACE: legacy}, 1)["receipts"][0]
    assert list(upgraded) == ["note", "request", "content", "state", "revision", "response"]


def test_legacy_nesting_gathers_present_flat_names_after_the_common_fields():
    legacy = {"schema_version": LEGACY_SCHEMA_VERSION, "last_client_time": 7, "seen_keys": [receipt(2)["request"]],
              "receipts": [{**receipt(1), "client_time": 7, "third": [3], "first": {"one": 1}},
                           {**receipt(2), "client_time": 7}]}
    history = nesting_ledger().read({NAMESPACE: deepcopy(legacy)}, 2)
    assert history["receipts"] == [{**receipt(1), "extras": {"first": {"one": 1}, "third": [3]}},
                                   {**receipt(2), "extras": {}}]
    assert [list(entry) for entry in history["receipts"]] == [[*receipt(1), "extras"]] * 2
    assert list(history["receipts"][0]["extras"]) == ["first", "third"]
    state = {NAMESPACE: legacy}
    nesting_ledger().record(state, history, {**receipt(3), "extras": {}})
    assert state[NAMESPACE]["receipts"][:2] == history["receipts"]
    assert "last_client_time" not in state[NAMESPACE]


def test_legacy_nesting_and_defaults_fill_different_module_fields():
    old = {name: value for name, value in receipt(1).items() if name != "note"}
    legacy = {"schema_version": LEGACY_SCHEMA_VERSION, "last_client_time": 7, "seen_keys": [],
              "receipts": [{**old, "client_time": 7, "second": None}]}
    history = nesting_ledger(legacy_defaults={"note": ""}).read({NAMESPACE: legacy}, 1)
    assert history["receipts"] == [{**old, "note": "", "extras": {"second": None}}]


@pytest.mark.parametrize("entry", [{**receipt(1), "first": 1}, {**receipt(1), "first": 1, "extras": {}}])
def test_nesting_never_reshapes_current_schema_receipts(entry):
    current = {"schema_version": SCHEMA_VERSION, "processed_keys": [receipt(1)["request"]], "receipts": [entry]}
    with pytest.raises(ProtocolError) as caught:
        nesting_ledger().read({NAMESPACE: current}, 1)
    assert caught.value.code == "fixture_history_invalid"


@pytest.mark.parametrize("nesting,defaults", [
    ({"unknown": ("first",)}, None), ({"request": ("first",)}, None), ({"response": ("first",)}, None),
    ({"extras": ("first",)}, {"extras": {}}), ({"extras": ("note",)}, None), ({"extras": ("revision",)}, None),
    ({"extras": ("extras",)}, None), ({"extras": ("client_time",)}, None), ({"extras": ("first", "first")}, None),
    ({"extras": ("first",), "note": ("first",)}, None), ({"extras": "first"}, None), ({"extras": {"first"}}, None),
    ({"extras": (1,)}, None)])
def test_legacy_nesting_only_gathers_distinct_flat_names_into_module_fields(nesting, defaults):
    with pytest.raises(ValueError, match="Legacy receipt nesting"):
        nesting_ledger(legacy_nesting=nesting, legacy_defaults=defaults)


def test_upgrade_caps_processed_digests_to_the_window(monkeypatch):
    monkeypatch.setattr(module, "PROCESSED_LIMIT", 2)
    legacy = {"schema_version": LEGACY_SCHEMA_VERSION, "last_client_time": 5,
              "seen_keys": [digest("older")], "receipts": [{**receipt(1), "client_time": 5}, {**receipt(2), "client_time": 5}]}
    history = ledger().read({NAMESPACE: legacy}, 2)
    assert history["processed_keys"] == [receipt(1)["request"], receipt(2)["request"]]


@pytest.mark.parametrize("history", [
    None, [], {}, {"schema_version": SCHEMA_VERSION, "receipts": []},
    {"schema_version": True, "receipts": [], "processed_keys": []},
    {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [], "processed_keys": []},
    {"schema_version": SCHEMA_VERSION, "receipts": None, "processed_keys": []},
    {"schema_version": SCHEMA_VERSION, "receipts": [], "processed_keys": ["a"]},
    {"schema_version": SCHEMA_VERSION, "receipts": [], "processed_keys": [digest(1), digest(1)]},
    {"schema_version": SCHEMA_VERSION, "receipts": [], "processed_keys": [{"nested": True}]},
    {"schema_version": SCHEMA_VERSION, "receipts": [receipt(1)], "processed_keys": []},
    {"schema_version": SCHEMA_VERSION, "receipts": [receipt(1)], "processed_keys": [receipt(1)["request"], digest(9)]},
    {"schema_version": SCHEMA_VERSION, "receipts": [receipt(1), receipt(2)],
     "processed_keys": [receipt(2)["request"], receipt(1)["request"]]},
    {"schema_version": SCHEMA_VERSION, "receipts": [{**receipt(1), "client_time": 1}], "processed_keys": [receipt(1)["request"]]},
    {"schema_version": SCHEMA_VERSION, "receipts": [receipt(2), receipt(1, revision=2)],
     "processed_keys": [receipt(2)["request"], receipt(1)["request"]]},
    {"schema_version": SCHEMA_VERSION, "receipts": [receipt(11)], "processed_keys": [receipt(11)["request"]]},
    {"schema_version": SCHEMA_VERSION, "receipts": [receipt(1, revision=0)], "processed_keys": [receipt(1)["request"]]},
    {"schema_version": SCHEMA_VERSION, "receipts": [receipt(1, revision=True)], "processed_keys": [receipt(1)["request"]]},
    {"schema_version": SCHEMA_VERSION, "receipts": [receipt(1, response="")], "processed_keys": [receipt(1)["request"]]},
    {"schema_version": SCHEMA_VERSION, "receipts": [receipt(1, response="x" * 65)], "processed_keys": [receipt(1)["request"]]},
    {"schema_version": SCHEMA_VERSION, "receipts": [receipt(1, response="\ud800")], "processed_keys": [receipt(1)["request"]]},
    {"schema_version": SCHEMA_VERSION, "receipts": [receipt(1, response=None)], "processed_keys": [receipt(1)["request"]]},
    {"schema_version": SCHEMA_VERSION, "receipts": [receipt(1, state="0")], "processed_keys": [receipt(1)["request"]]},
    {"schema_version": SCHEMA_VERSION, "receipts": [receipt(1, note="rejected")], "processed_keys": [receipt(1)["request"]]},
    {"schema_version": SCHEMA_VERSION, "receipts": [receipt(1, note=None)], "processed_keys": [receipt(1)["request"]]},
])
def test_invalid_current_histories_are_never_reset(history):
    invalid(history)


@pytest.mark.parametrize("history", [
    {"schema_version": True, "receipts": [], "last_client_time": None, "seen_keys": []},
    {"schema_version": SCHEMA_VERSION, "receipts": [], "last_client_time": None, "seen_keys": []},
    {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [], "last_client_time": None, "seen_keys": [digest(1)]},
    {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [], "last_client_time": True, "seen_keys": []},
    {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [], "last_client_time": 2 ** 31, "seen_keys": [digest(1)]},
    {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": None, "last_client_time": None, "seen_keys": []},
    {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [], "last_client_time": 1, "seen_keys": None},
    {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [], "last_client_time": 1, "seen_keys": ["short"]},
    {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [], "last_client_time": 1, "seen_keys": [digest(1), digest(1)]},
    {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [receipt(1)], "last_client_time": 1,
     "seen_keys": [receipt(1)["request"]]},
    {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [{**receipt(1), "client_time": True}], "last_client_time": 1,
     "seen_keys": [receipt(1)["request"]]},
    {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [{**receipt(1), "client_time": 1, "extra": 1}],
     "last_client_time": 1, "seen_keys": [receipt(1)["request"]]},
])
def test_invalid_legacy_histories_are_never_reset(history):
    invalid(history)


def test_legacy_seen_window_above_the_processed_limit_is_rejected(monkeypatch):
    monkeypatch.setattr(module, "PROCESSED_LIMIT", 1)
    invalid({"schema_version": LEGACY_SCHEMA_VERSION, "receipts": [], "last_client_time": 1,
             "seen_keys": [digest(1), digest(2)]})


def test_record_validates_the_new_receipt_before_returning_state():
    state = {}
    service = ledger()
    with pytest.raises(ProtocolError) as caught:
        service.record(state, service.read(state, 0), receipt(1, response="x" * 65))
    assert caught.value.code == "fixture_history_invalid"
    assert state == {}


def test_fields_without_state_skip_state_digest():
    service = ledger(fields=("request", "content", "revision", "response"), check=None)
    state = {}
    entry = {name: value for name, value in receipt(1).items() if name not in ("state", "note")}
    service.record(state, service.read(state, 0), entry)
    assert service.read(state, 1)["receipts"] == [entry]


@pytest.mark.parametrize("fields", [("request", "content", "revision"), ("request", "content", "revision", "response",
                                                                          "client_time")])
def test_receipt_layout_requires_core_fields_without_client_clock(fields):
    with pytest.raises(ValueError):
        ledger(fields=fields)
