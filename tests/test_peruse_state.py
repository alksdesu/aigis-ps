from copy import deepcopy
from dataclasses import replace
import hashlib
import logging
from pathlib import Path
import struct
from xml.etree import ElementTree as ET

import lz4.frame
import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.app import create_app
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.assets import asset_key
from aigis_ps.bootstrap import BootstrapService
from aigis_ps.errors import ProtocolError
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.native_numbers import INT32_MAX, INT32_MIN
import aigis_ps.peruse_state as module
from aigis_ps.peruse_state import (
    HASH_COLLISION, HOME_EVENT_RESOURCE, MAX_EVENT_ID_BYTES, MAX_PERUSED_EVENTS, QUEST_EVENT_RESOURCE,
    STORED_EVENT_ID_BYTES, PeruseCatalog, PeruseService, event_hash, peruse_quest_ids, peruse_response, read_perused,
    set_perused_flags,
)
from aigis_ps.al_format import ParseLimits
from aigis_ps.protocol import NATIVE_CELL_BYTES, decode_wire, encode_wire
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.storage import Store
from test_bootstrap import asset_root
from test_present_items import item_table
from test_unit_actions import request


SEQUENCE_SCHEMA = (("_SceneID", 1), ("_CommandName", 32), ("_ObjectName", 32), ("_AfterExpression", 32),
                   *((f"_Param{index}", 32) for index in range(1, 7)), ("_CmndParam", 32))
TEXT_SCHEMA = (("_TextID", 1),)
GIFT_EVENTS = frozenset(("evTrustFemale150", "evTrustMale150"))
SAVED_EVENTS = GIFT_EVENTS | {"evGoddessRevival"}


def archive(members):
    items = [(name.encode() + b"\0", payload) for name, payload in (
        members.items() if isinstance(members, dict) else members)]
    positions, cursor = [], 18 + 2 * len(items)
    for name, _ in items:
        positions.append(cursor)
        cursor += 18 + len(name)
    entries, data = bytearray(), bytearray()
    for index, (name, payload) in enumerate(items):
        entries += struct.pack("<IIIIH", index, cursor + len(data), len(payload), 0x80000001, 0) + name
        data += payload
    header = struct.pack(f"<4sBBH8xH{len(items)}H", b"ALAR", 3, 0x41, len(items), cursor, *positions)
    return header + bytes(entries) + bytes(data)


def compressed(raw):
    return struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)


def command(name, parameters="", **changes):
    row = {field: 0 if kind == 1 else "" for field, kind in SEQUENCE_SCHEMA}
    row.update(_CommandName=name, _CmndParam=parameters, **changes)
    return row


def home_event(*rows, schema=SEQUENCE_SCHEMA, scripts=("_evseq.atb", "_evtxt.atb")):
    tables = {"_evseq.atb": item_table(list(rows), schema), "_evtxt.atb": item_table([], TEXT_SCHEMA)}
    return archive({name: tables[name] for name in scripts})


def saving(parameters=""):
    return home_event(command("PlayBgm", "BgmID=BgmMenuUnit"), command("DoPeruseSave", parameters))


def quest_event(*parameters, schema=SEQUENCE_SCHEMA, scripts=("_evseq.atb", "_evtxt.atb")):
    return home_event(command("PlayBgm", "BgmID=BgmEvent"),
                      *(command("DoPeruseEvent", _Param1=parameter) for parameter in parameters),
                      schema=schema, scripts=scripts)


def quest_events(packages):
    return {QUEST_EVENT_RESOURCE: archive(packages)}.__getitem__


NATIVE_PACKAGES = {
    "_evTrustFemale150.aar": saving(),
    "_evTrustMale150.aar": saving(),
    "_evGoddessRevival.aar": saving(),
    "_evAnv11Episode_101.aar": home_event(command("DoPeruseSaveEpisode")),
    "_evVoteCook01_01.aar": home_event(command("DoPeruseSavePresent")),
}


def resources(packages=None):
    return {HOME_EVENT_RESOURCE: archive(NATIVE_PACKAGES if packages is None else packages)}


def catalog(packages):
    return PeruseCatalog(resources(packages).__getitem__)


@pytest.fixture
def peruse(asset_root):
    return PeruseService(BootstrapService(asset_root, peruse_provider=peruse_response),
                         asset_reader=resources().__getitem__)


@pytest.fixture
def state(peruse):
    result = peruse.bootstrap.initial_state()
    result["player_state"] = {"units": [{"Love": 150, "WatchLoveEv": 3, "GetIndex": 7}]}
    result["unrelated"] = [{"preserve": [1, 2]}]
    result["gift_reward_history"] = [{"CardID": 56, "threshold": 150}]
    return result


@pytest.fixture
def store(tmp_path, state):
    return Store(tmp_path / "peruse.sqlite3", state)


def mark_request(store, event_id="evTrustFemale150"):
    return request(store, 75, (Column("ID", "S", (event_id,)),))


def rejected(peruse, store, envelope, code=None):
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        peruse.save(envelope, store)
    if code is not None:
        assert error.value.code == code
    assert store.profile() == before
    return error.value


def test_legacy_empty_history_is_readable_without_migration(state):
    before = deepcopy(state)
    assert read_perused(state) == []
    assert peruse_response(state) == Container("PEV", (Column("ID", "S", ()), Column("FLG", "I", ())))
    updated = set_perused_flags(state, {})
    assert updated == before and updated is not state
    assert "perused_events_schema" not in updated
    updated["unrelated"][0]["preserve"].append(3)
    assert state == before


def test_pure_updates_preserve_history_and_allow_explicit_zero(state):
    original = deepcopy(state)
    first = set_perused_flags(state, {"unknown-old-event": -3, "evTrustFemale150": 7})
    before = deepcopy(first)
    second = set_perused_flags(first, {"evTrustFemale150": 0, "evTrustMale150": 1})
    assert first == before and state == original
    assert second["perused_events_schema"] == 1
    assert read_perused(second) == [{"ID": "unknown-old-event", "FLG": -3},
        {"ID": "evTrustFemale150", "FLG": 0}, {"ID": "evTrustMale150", "FLG": 1}]
    assert peruse_response(second, ["evTrustMale150", "evTrustFemale150"]).children == (
        Column("ID", "S", ("evTrustMale150", "evTrustFemale150")), Column("FLG", "I", (1, 0)))
    rows = read_perused(second)
    rows[0]["FLG"] = 99
    second["unrelated"][0]["preserve"].append(3)
    assert read_perused(second)[0]["FLG"] == -3 and first == before
    assert peruse_response(second, []).children[0].values == ()


@pytest.mark.parametrize("event_id", sorted(SAVED_EVENTS))
def test_mark_only_changes_account_read_history_and_repeated_mark_is_noop(peruse, store, event_id):
    before = store.profile()
    response = peruse.save(mark_request(store, event_id), store)
    after = store.profile()
    expected = deepcopy(before.state)
    expected["perused_events_schema"] = 1
    expected["tables"]["perused_events"] = [{"ID": event_id, "FLG": 1}]
    assert after.state == expected and after.revision == before.revision + 1
    groups = {node.name: node for node in loads(response).children}
    assert set(groups) == {"SYSTEM", "PEV"}
    assert groups["PEV"] == peruse_response(expected)
    assert peruse.save(mark_request(store, event_id), store) == response
    assert store.profile() == after
    assert set(peruse.handlers()) == {75}
    assert peruse.status()["supported_events"] == sorted(SAVED_EVENTS)


@pytest.mark.parametrize("flag", [-0x80000000, -1, 1, 7, 0x7FFFFFFF])
def test_mark_preserves_nonzero_stored_flags_without_revision(peruse, store, flag):
    profile = store.profile()
    updated = set_perused_flags(profile.state, {"evTrustFemale150": flag, "unrelated-event": -9})
    store.update_state(updated, profile.revision)
    before = store.profile()
    response = peruse.save(mark_request(store), store)
    assert store.profile() == before
    assert loads(response).children[1].children == (
        Column("ID", "S", ("evTrustFemale150",)), Column("FLG", "I", (flag,)))


def test_explicit_revocation_can_be_marked_again(peruse, store):
    peruse.save(mark_request(store), store)
    profile = store.profile()
    store.update_state(set_perused_flags(profile.state, {"evTrustFemale150": 0}), profile.revision)
    before = store.profile()
    peruse.save(mark_request(store), store)
    assert read_perused(store.profile().state) == [{"ID": "evTrustFemale150", "FLG": 1}]
    assert store.profile().revision == before.revision + 1


@pytest.mark.parametrize("event_id", ["evTrustFemale100", "evTrustFemale150_56", "evTrustMale150/56", "Other",
    "EVTrustMale150", "evAnv11Episode_101", "evVoteCook01_01", "_evGoddessRevival.aar", "evGoddessRevival "])
def test_other_events_are_explicitly_unsupported_and_logged(peruse, store, event_id, caplog):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    assert rejected(peruse, store, mark_request(store, event_id), "peruse_event_unsupported").status == 501
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        f"Request 75 refused read history for {event_id!r}: no home event saves it through DoPeruseSave"]


@pytest.mark.parametrize("name", ["a", "猫" * 85, "a" * MAX_EVENT_ID_BYTES, "<&\"' >", "line\nname"])
def test_history_names_use_exact_utf8_strings(state, name):
    updated = set_perused_flags(state, {name: 0})
    response = peruse_response(updated)
    assert loads(dumps(Container(children=(response,)))).children[0] == response
    assert read_perused(updated) == [{"ID": name, "FLG": 0}]


@pytest.mark.parametrize("name", ["", "a" * (MAX_EVENT_ID_BYTES + 1), "猫" * 85 + "a", "a" * 513, "猫" * 171,
                                  "bad\0name", "bad\x01name", "\ud800", "\uffff", 1, None])
def test_invalid_names_are_rejected_without_mutating_input(state, name):
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        set_perused_flags(state, {name: 1})
    assert error.value.code == "peruse_updates_invalid" and state == before


def test_new_history_follows_the_native_cell_while_saves_keep_the_older_bound():
    assert MAX_EVENT_ID_BYTES == NATIVE_CELL_BYTES == 255 and STORED_EVENT_ID_BYTES == 512


@pytest.mark.parametrize("name", ["a" * (MAX_EVENT_ID_BYTES + 1), "猫" * 170 + "aa", "a" * STORED_EVENT_ID_BYTES])
def test_history_saved_under_the_older_bound_keeps_loading_and_updating(state, name):
    legacy = deepcopy(state)
    legacy["perused_events_schema"] = 1
    legacy["tables"]["perused_events"] = [{"ID": name, "FLG": 1}]
    before = deepcopy(legacy)
    assert read_perused(legacy) == [{"ID": name, "FLG": 1}]
    assert peruse_response(legacy, [name]).children[0].values == (name,)
    updated = set_perused_flags(legacy, {name: 0, "evTrustMale150": 1})
    assert read_perused(updated) == [{"ID": name, "FLG": 0}, {"ID": "evTrustMale150", "FLG": 1}]
    with pytest.raises(ProtocolError) as error:
        set_perused_flags(state, {name: 1})
    assert error.value.code == "peruse_updates_invalid" and legacy == before


@pytest.mark.parametrize("flag", [True, False, -0x80000001, 0x80000000, 1.0, "1", None])
def test_invalid_flags_do_not_partially_apply_updates(state, flag):
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        set_perused_flags(state, {"valid": 1, "invalid": flag})
    assert error.value.code == "peruse_updates_invalid" and state == before


@pytest.mark.parametrize("updates", [[], None, ("a", 1), "a"])
def test_updates_require_mapping(state, updates):
    with pytest.raises(ProtocolError) as error:
        set_perused_flags(state, updates)
    assert error.value.code == "peruse_updates_invalid"


def test_native_crc_collision_rejected_in_old_and_new_rows(state):
    assert event_hash("plumless") == event_hash("buckeroo") == 0x4DDB0C25
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        set_perused_flags(state, {"plumless": 1, "buckeroo": 2})
    assert error.value.code == "peruse_name_conflict" and state == before
    existing = set_perused_flags(state, {"plumless": -1})
    before = deepcopy(existing)
    with pytest.raises(ProtocolError) as error:
        set_perused_flags(existing, {"buckeroo": 0})
    assert error.value.code == "peruse_name_conflict" and existing == before
    existing["tables"]["perused_events"].append({"ID": "buckeroo", "FLG": 0})
    with pytest.raises(ProtocolError) as error:
        read_perused(existing)
    assert error.value.code == "peruse_state_invalid"


@pytest.mark.parametrize("mutation", ["bootstrap", "tables", "missing", "not_list", "unknown_version", "bool_version",
    "none_version", "unversioned", "duplicate", "extra", "missing_flag", "bad_name", "oversized_name", "bool_flag"])
def test_invalid_stored_history_is_never_replaced(state, mutation):
    state = set_perused_flags(state, {"existing": 3})
    if mutation == "bootstrap":
        state["bootstrap_schema"] = True
    elif mutation == "tables":
        state["tables"] = []
    elif mutation == "missing":
        del state["tables"]["perused_events"]
    elif mutation == "not_list":
        state["tables"]["perused_events"] = {}
    elif mutation == "unknown_version":
        state["perused_events_schema"] = 2
    elif mutation == "bool_version":
        state["perused_events_schema"] = True
    elif mutation == "none_version":
        state["perused_events_schema"] = None
    elif mutation == "unversioned":
        del state["perused_events_schema"]
    elif mutation == "duplicate":
        state["tables"]["perused_events"] *= 2
    elif mutation == "extra":
        state["tables"]["perused_events"][0]["CardID"] = 56
    elif mutation == "missing_flag":
        del state["tables"]["perused_events"][0]["FLG"]
    elif mutation == "bad_name":
        state["tables"]["perused_events"][0]["ID"] = ""
    elif mutation == "oversized_name":
        state["tables"]["perused_events"][0]["ID"] = "a" * (STORED_EVENT_ID_BYTES + 1)
    else:
        state["tables"]["perused_events"][0]["FLG"] = True
    before = deepcopy(state)
    for action in (lambda: read_perused(state), lambda: peruse_response(state), lambda: set_perused_flags(state, {})):
        with pytest.raises(ProtocolError) as error:
            action()
        assert error.value.code == "peruse_state_invalid" and error.value.status == 503
        assert state == before


@pytest.mark.parametrize("ids", ["existing", ["existing", "existing"], ["plumless", "buckeroo"], [""], [True], ["missing"]])
def test_response_selection_is_strict(state, ids):
    state = set_perused_flags(state, {"existing": 1})
    before = deepcopy(state)
    with pytest.raises(ProtocolError):
        peruse_response(state, ids)
    assert state == before


def test_event_count_limit_preserves_existing_history(state, monkeypatch):
    assert MAX_PERUSED_EVENTS == 65535
    monkeypatch.setattr("aigis_ps.peruse_state.MAX_PERUSED_EVENTS", 2)
    state = set_perused_flags(state, {"a": 1, "b": 1})
    before = deepcopy(state)
    assert read_perused(set_perused_flags(state, {"b": 0}))[1]["FLG"] == 0
    with pytest.raises(ProtocolError) as error:
        set_perused_flags(state, {"c": 1})
    assert error.value.code == "peruse_capacity" and state == before
    state["tables"]["perused_events"].append({"ID": "c", "FLG": 1})
    with pytest.raises(ProtocolError) as error:
        read_perused(state)
    assert error.value.code == "peruse_state_invalid"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "wrong_type", "empty", "multiple", "empty_name",
    "long_name", "nested", "root_attribute", "mixed_text", "missing_header", "bad_integer", "bad_metadata", "wrong_profile"])
def test_malformed_requests_never_mark_history(peruse, store, mutation):
    envelope = mark_request(store)
    root, field = envelope.document, envelope.document.find("ID")
    if mutation == "missing":
        root.remove(field)
    elif mutation == "duplicate":
        root.append(deepcopy(field))
    elif mutation == "unknown":
        ET.SubElement(root, "CardID", T="I")
    elif mutation == "wrong_type":
        field.set("T", "I")
    elif mutation == "empty":
        field.remove(field[0])
    elif mutation == "multiple":
        ET.SubElement(field, "V").text = "evTrustMale150"
    elif mutation == "empty_name":
        field[0].text = ""
    elif mutation == "long_name":
        field[0].text = "a" * (MAX_EVENT_ID_BYTES + 1)
    elif mutation == "nested":
        ET.SubElement(field[0], "V").text = "evTrustMale150"
    elif mutation == "root_attribute":
        root.set("T", "P")
    elif mutation == "mixed_text":
        field.tail = "junk"
    elif mutation == "missing_header":
        root.remove(root.find("UI"))
    elif mutation == "bad_integer":
        root.find("CT/V").text = "true"
    elif mutation == "bad_metadata":
        root.find("VN").set("T", "S")
    else:
        envelope = replace(envelope, profile=replace(envelope.profile, registration_keys=(76,)))
    rejected(peruse, store, envelope, "invalid_request_fields")


@pytest.mark.parametrize("field", ["UI", "UK", "MK"])
def test_authentication_is_required_even_for_repeated_event(peruse, store, field):
    peruse.save(mark_request(store), store)
    envelope = mark_request(store)
    envelope.document.find(field + "/V").text = "other"
    rejected(peruse, store, envelope)


@pytest.mark.parametrize("field,code", [("UI", "invalid_identity"), ("UK", "invalid_identity"), ("MK", "invalid_method_key")])
def test_authentication_precedes_the_native_save_check(peruse, store, field, code):
    envelope = mark_request(store, "evAnv11Episode_101")
    envelope.document.find(field + "/V").text = "other"
    assert rejected(peruse, store, envelope, code).status == 403


@pytest.mark.parametrize("target", ["save", "startup"])
def test_response_capacity_is_checked_before_commit(peruse, store, target):
    envelope = mark_request(store)
    if target == "save":
        envelope = replace(envelope, profile=replace(envelope.profile, response_limit=1))
    else:
        peruse._startup_profile = replace(peruse._startup_profile, response_limit=1)
    rejected(peruse, store, envelope, "response_too_large")
    assert "perused_events_schema" not in store.profile().state


def test_retry_preserves_concurrent_history_and_nonzero_flag(peruse, store, monkeypatch):
    original = store.update_state
    calls = []
    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            profile = store.profile()
            concurrent = set_perused_flags(profile.state, {"unknown-concurrent": 2, "evTrustFemale150": -7})
            concurrent["unrelated"].append({"concurrent": True})
            original(concurrent, profile.revision)
        return original(state, revision)
    monkeypatch.setattr(store, "update_state", update)
    response = peruse.save(mark_request(store), store)
    assert len(calls) == 1
    assert read_perused(store.profile().state) == [{"ID": "unknown-concurrent", "FLG": 2}, {"ID": "evTrustFemale150", "FLG": -7}]
    assert store.profile().state["unrelated"][-1] == {"concurrent": True}
    assert loads(response).children[1].children[1].values == (-7,)


def test_retry_merges_separate_concurrent_event(peruse, store, monkeypatch):
    original = store.update_state
    calls = []
    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            profile = store.profile()
            original(set_perused_flags(profile.state, {"evTrustMale150": 1}), profile.revision)
        return original(state, revision)
    monkeypatch.setattr(store, "update_state", update)
    peruse.save(mark_request(store), store)
    assert len(calls) == 2
    assert {row["ID"] for row in read_perused(store.profile().state)} == GIFT_EVENTS


def test_exhausted_cas_keeps_original_state(peruse, store, monkeypatch):
    calls = []
    def fail(state, revision):
        calls.append(revision)
        raise ValueError("revision changed")
    monkeypatch.setattr(store, "update_state", fail)
    rejected(peruse, store, mark_request(store), "profile_changed")
    assert len(calls) == 3


def test_native_http_mark_and_137_reload_after_store_reopen(peruse, state, tmp_path):
    state = set_perused_flags(state, {"unknown-history": -2})
    def application():
        return create_app(tmp_path / "peruse-http", initial_state=state,
                          handlers=peruse.bootstrap.handlers() | peruse.handlers())
    app = application()
    envelope = mark_request(app.state.store)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reply = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert reply.status_code == 200
        assert loads(decode_wire(reply.content, envelope.profile.response_key)).children[1].children[1].values == (1,)
    saved = app.state.store.profile()
    reopened = application()
    envelope = request(reopened.state.store, 137, ())
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        reply = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert reply.status_code == 200
        groups = {node.name: node for node in loads(decode_wire(reply.content, envelope.profile.response_key)).children}
        assert groups["PEV"] == peruse_response(saved.state)
    assert reopened.state.store.profile() == saved


def test_status_reports_the_home_events_derived_from_native_saves(peruse):
    status = peruse.status()
    assert status["ready"] is True and status["implemented_operations"] == [75]
    assert status["home_event_count"] == len(NATIVE_PACKAGES)
    assert status["supported_event_count"] == len(SAVED_EVENTS)
    assert status["supported_events"] == sorted(SAVED_EVENTS)
    assert status["resource_hash"] == hashlib.sha256(resources()[HOME_EVENT_RESOURCE]).hexdigest()
    assert status["excluded_package_count"] == 0 and status["excluded_packages"] == []
    assert status["local_limits"] == {"events": MAX_PERUSED_EVENTS, "event_id_bytes": MAX_EVENT_ID_BYTES,
                                      "stored_event_id_bytes": STORED_EVENT_ID_BYTES}


@pytest.mark.parametrize("member,event_id", [
    ("_evTrustFemale150.aar", "evTrustFemale150"), ("_ev_vh000016_3.aar", "ev_vh000016_3"),
    ("_ev.special.aar", "ev.special.aar"), ("_evNoSuffix.bin", "evNoSuffix.bin"),
    ("_evDotless", None), ("evTrustFemale150.aar", None),
])
def test_home_event_ids_invert_the_client_package_name(member, event_id):
    assert module._home_event_id(member) == event_id


@pytest.mark.parametrize("parameters,arguments", [
    ("", {}),
    ("Save=evShared", {"Save": "evShared"}),
    (" Save = ev Shared\r\n", {"Save": "evShared"}),
    ("Prio=-5,MotionIndex=0", {"Prio": "-5", "MotionIndex": "0"}),
    ("Archive=a.aar,b.aar,\nTexture=t.atx", {"Archive": "a.aar,b.aar", "Texture": "t.atx"}),
    ("Save=", {}),
    ("Save=a,", {"Save": "a"}),
    ("Save=a,=b", {"Save": "a,"}),
    (",Save=evShared", {}),
    ("Wait=1,,Save=evShared", {"Wait": "1"}),
    ("Save=evFirst,Save=evSecond", {"Save": "evSecond"}),
    ("Save=evFirst,Save=evSecond,Wait=1", {"Save": "evSecond", "Wait": "1"}),
    ("Save=evShared=extra", {"Save": "evShared=extra"}),
    ("Save==evShared", {"Save": "=evShared"}),
    ("Save=a=b,c,Wait=1=2", {"Save": "a=b,c", "Wait": "1=2"}),
    ("Save=a,b=,c", {"Save": "a", "b": "c"}),
    ("evOrphan,Save=evShared", {"Save": "evShared"}),
    ("=evShared", {}),
])
def test_command_parameters_follow_the_client_grammar(parameters, arguments):
    assert module.command_arguments(parameters) == arguments


@pytest.mark.parametrize("text,separator,limit,parts", [
    ("", "=", 1, []),
    ("Save", "=", 1, ["Save"]),
    ("=Save", "=", 1, [""]),
    ("Save=a=b", "=", 1, ["Save", "a=b"]),
    ("Save=", "=", 1, ["Save", "", ""]),
    ("Save=a=b", "=", None, ["Save", "a", "b"]),
    ("a,b,", ",", None, ["a", "b", ""]),
    ("a,,b", ",", None, ["a", ""]),
])
def test_splitter_matches_the_client_splitter(text, separator, limit, parts):
    assert module._split(text, separator, limit) == parts


@pytest.mark.parametrize("parameters,saved", [
    ("", "evOwn"), ("BgmID=BgmMenuUnit", "evOwn"), ("Save=", "evOwn"), ("Save=evShared", "evShared"),
    ("Save=1-2", "1-2"), ("Save=ev.1", "ev.1"), ("Save=a\u2010", "a\u2010"),
    ("Save=123", ""), ("Save=-7", ""), ("Save=-", ""), ("Save=+.5", ""), ("Save=1.2.3", ""), ("Save=\u2010abc", ""),
])
def test_save_argument_follows_the_client_value_types(parameters, saved):
    assert module._saved_event_id("evOwn", parameters) == saved


@pytest.mark.parametrize("value,string", [
    ("", True), ("ev", True), ("1-2", True), ("+-1", True), ("5e3", True), ("a\u2010", True), ("1\u2010", True),
    ("\u20111", True), ("1", False), ("-", False), ("+.5", False), ("1.2.3", False), (".", False), ("\u2010", False),
])
def test_value_types_follow_the_client_script_parser(value, string):
    assert module.client_string(value) is string


@pytest.mark.parametrize("event_id", ["2026Event", "ev-1", "1-2", "ev\u2010", "ev.1", "-ev"])
def test_string_event_ids_save_under_their_own_name(event_id):
    assert module._saved_event_id(event_id, "") == event_id


@pytest.mark.parametrize("parameters", ["", "Save=evShared"])
@pytest.mark.parametrize("event_id", ["1024", "-1", "+", "1.5", ".", "\u2010ev"])
def test_events_the_client_cannot_launch_by_name_are_outside_the_contract(event_id, parameters):
    with pytest.raises(ValueError):
        module._saved_event_id(event_id, parameters)


def test_save_arguments_and_compressed_packages_are_derived_like_the_client():
    packages = {
        "_evOwn.aar": compressed(saving()),
        "_evRedirected.aar": saving("Save=evShared"),
        "_evAlias.aar": saving(" Save = evShared "),
        "_evEquals.aar": saving("Save=evShared=Copy"),
        "_ev.special.aar": saving(),
        "_2026Event.aar": saving(),
        "_1024.aar": home_event(command("PlayBgm", "BgmID=BgmMenuUnit")),
        "_evEpisode.aar": home_event(command("DoPeruseSaveEpisode", "Save=evEpisodeOnly")),
        "_evPresent.aar": home_event(command("DoPeruseSavePresent")),
        "_evExpression.aar": home_event(command("PlayBgm", _AfterExpression="DoPeruseSave();")),
    }
    raw = archive(packages)
    result = PeruseCatalog(lambda name: compressed(raw))
    assert result.event_ids == {"evOwn", "evShared", "evShared=Copy", "ev.special.aar", "2026Event"}
    assert result.home_event_count == len(packages)
    assert result.resource_hash == hashlib.sha256(raw).hexdigest()


def test_packages_the_client_cannot_play_are_not_accepted():
    result = catalog({
        "_evNoText.aar": home_event(command("DoPeruseSave"), scripts=("_evseq.atb",)),
        "_evNoSequence.aar": home_event(scripts=("_evtxt.atb",)),
        "evNoPrefix.aar": saving(),
        "_evDotless": saving(),
        "_evPlayable.aar": saving(),
    })
    assert result.event_ids == {"evPlayable"} and result.home_event_count == 1


def test_empty_home_event_archive_accepts_nothing():
    result = catalog({})
    assert result.event_ids == frozenset() and result.status()["supported_events"] == []


@pytest.mark.parametrize("mutation", ["missing", "not_callable", "empty", "not_bytes", "corrupt"])
def test_unreadable_home_event_archives_disable_the_catalog(mutation):
    readers = {"missing": {}.__getitem__, "not_callable": HOME_EVENT_RESOURCE, "empty": lambda name: b"",
               "not_bytes": lambda name: "EventHomeArc", "corrupt": lambda name: b"ALAR\x03" + b"\xff" * 32}
    with pytest.raises(ProtocolError) as error:
        PeruseCatalog(readers[mutation])
    assert error.value.code == "peruse_catalog_invalid" and error.value.status == 503


SEQUENCE_DRIFT = "Home event sequences differ from the native command table"
NUMERIC_LAUNCH = "Saving home events must launch under a string EventID"
EMPTY_SAVE = "DoPeruseSave sends an empty ID for a numeric Save argument"
GODDESS = "_evGoddessRevival.aar"
OVERSIZED = "_" + "e" * (MAX_EVENT_ID_BYTES + 1) + ".aar"


def contract_breaks():
    retyped = tuple((name, 1 if name == "_CmndParam" else kind) for name, kind in SEQUENCE_SCHEMA)
    unnamed = tuple(field for field in SEQUENCE_SCHEMA if field[0] != "_CommandName")
    text = item_table([], TEXT_SCHEMA)
    return {
        "field_type": ({GODDESS: home_event(command("DoPeruseSave", 0), schema=retyped)}, {GODDESS: SEQUENCE_DRIFT}),
        "missing_field": ({GODDESS: home_event(command("DoPeruseSave"), schema=unnamed)}, {GODDESS: SEQUENCE_DRIFT}),
        "numeric_save": ({GODDESS: saving("Save=123")}, {GODDESS: EMPTY_SAVE}),
        "hyphen_save": ({GODDESS: saving("Save=\u20107")}, {GODDESS: EMPTY_SAVE}),
        "numeric_event": ({"_1024.aar": saving()}, {"_1024.aar": NUMERIC_LAUNCH}),
        "hyphen_event": ({"_\u2010ev.aar": saving()}, {"_\u2010ev.aar": NUMERIC_LAUNCH}),
        "numeric_redirect": ({"_1024.aar": saving("Save=evShared")}, {"_1024.aar": NUMERIC_LAUNCH}),
        "control_id": ({"_ev\x01Broken.aar": saving()}, {"_ev\x01Broken.aar": None}),
        "oversized_id": ({OVERSIZED: saving()}, {OVERSIZED: "Event IDs must be nonempty bounded strings"}),
        "collision": ({"_evFirst.aar": saving("Save=plumless"), "_evSecond.aar": saving("Save=buckeroo")},
                      {"_evFirst.aar": HASH_COLLISION, "_evSecond.aar": HASH_COLLISION}),
        "corrupt_package": ({GODDESS: b"junk"}, {GODDESS: None}),
        "corrupt_sequence": ({GODDESS: archive({"_evseq.atb": b"ALTB\x01", "_evtxt.atb": text})}, {GODDESS: None}),
        "corrupt_text": ({GODDESS: archive({"_evseq.atb": item_table([command("DoPeruseSave")], SEQUENCE_SCHEMA),
                                            "_evtxt.atb": b"ALTB\x01"})}, {GODDESS: None}),
    }


@pytest.mark.parametrize("mutation", sorted(contract_breaks()))
def test_packages_outside_the_client_contract_are_excluded_alone(mutation):
    changes, expected = contract_breaks()[mutation]
    result = catalog({**NATIVE_PACKAGES, **changes})
    assert set(result.excluded) == set(expected)
    for member, reason in expected.items():
        assert result.excluded[member] == reason if reason is not None else result.excluded[member]
    assert result.event_ids == SAVED_EVENTS - ({"evGoddessRevival"} if GODDESS in changes else set())
    status = result.status()
    assert status["ready"] is True and status["excluded_package_count"] == len(expected)
    assert status["excluded_packages"] == [{"package": member, "reason": result.excluded[member]} for member in sorted(expected)]


def test_saves_of_excluded_packages_are_refused_and_logged_with_their_reason(asset_root, tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    packages = {**NATIVE_PACKAGES, GODDESS: saving("Save=123"),
                "_evFirst.aar": saving("Save=plumless"), "_evSecond.aar": saving("Save=buckeroo")}
    service = PeruseService(BootstrapService(asset_root, peruse_provider=peruse_response),
                            asset_reader=resources(packages).__getitem__)
    assert service.status()["excluded_package_count"] == 3
    assert any(record.levelno == logging.WARNING and "Request 75 serves home events without 3 excluded package(s)"
               in record.getMessage() for record in caplog.records)
    store = Store(tmp_path / "excluded.sqlite3", service.bootstrap.initial_state())
    for event_id, reason in (("evGoddessRevival", EMPTY_SAVE), ("plumless", HASH_COLLISION), ("buckeroo", HASH_COLLISION)):
        caplog.clear()
        assert rejected(service, store, mark_request(store, event_id), "peruse_event_excluded").status == 503
        assert [record.getMessage() for record in caplog.records] == [
            f"Request 75 refused read history for {event_id!r}: its home event package is excluded ({reason})"]
    assert rejected(service, store, mark_request(store, "evFirst"), "peruse_event_unsupported").status == 501
    service.save(mark_request(store), store)
    assert read_perused(store.profile().state) == [{"ID": "evTrustFemale150", "FLG": 1}]


def test_duplicate_member_names_resolve_to_the_last_one_like_the_client():
    sequences = archive([("_evseq.atb", item_table([command("PlayBgm")], SEQUENCE_SCHEMA)),
                         ("_evseq.atb", item_table([command("DoPeruseSave")], SEQUENCE_SCHEMA)),
                         ("_evtxt.atb", item_table([], TEXT_SCHEMA))])
    raw = archive([(GODDESS, home_event(command("PlayBgm"))), (GODDESS, saving("Save=evLast")),
                   ("_evTrustMale150.aar", sequences)])
    result = PeruseCatalog(lambda name: raw)
    assert result.event_ids == {"evLast", "evTrustMale150"} and result.excluded == {}
    raw = archive([(GODDESS, saving()), (GODDESS, b"junk")])
    result = PeruseCatalog(lambda name: raw)
    assert result.event_ids == frozenset() and set(result.excluded) == {GODDESS}


def test_exhausted_decoding_budget_excludes_the_remaining_packages(monkeypatch):
    raw = resources()[HOME_EVENT_RESOURCE]
    monkeypatch.setattr(module, "RESOURCE_LIMIT", len(raw) + 1)
    result = PeruseCatalog(lambda name: raw)
    assert result.event_ids == frozenset() and result.home_event_count == 0
    assert result.excluded == dict.fromkeys(NATIVE_PACKAGES, "Home event packages exceed the decoded byte limit")


@pytest.mark.parametrize("packed,reason", [(False, "Home event resources must be bounded bytes"),
                                           (True, "ALL4 declared size exceeds the byte limit")])
def test_one_oversized_package_does_not_hide_the_others(monkeypatch, packed, reason):
    limit = 4096
    monkeypatch.setattr(module, "PACKAGE_LIMITS", ParseLimits(max_input_bytes=limit, max_decompressed_bytes=limit,
                                                              max_rows=65535, max_fields=64, max_members=4096))
    large = home_event(command("DoPeruseSave"), *(command("PlayBgm", "BgmID=" + "x" * 64) for _ in range(64)))
    assert max(len(payload) for payload in NATIVE_PACKAGES.values()) < len(compressed(large)) < limit < len(large)
    result = catalog({**NATIVE_PACKAGES, "_evLarge.aar": compressed(large) if packed else large})
    assert result.event_ids == SAVED_EVENTS and result.excluded == {"_evLarge.aar": reason}


def test_unavailable_catalog_refuses_every_save_without_touching_history(asset_root, tmp_path):
    service = PeruseService(BootstrapService(asset_root, peruse_provider=peruse_response), asset_reader={}.__getitem__)
    status = service.status()
    assert status["ready"] is False and status["error"] == "peruse_catalog_invalid"
    assert status["implemented_operations"] == [75] and "supported_events" not in status
    store = Store(tmp_path / "unavailable.sqlite3", service.bootstrap.initial_state())
    for event_id in sorted(SAVED_EVENTS):
        assert rejected(service, store, mark_request(store, event_id), "peruse_catalog_unavailable").status == 503


def test_service_falls_back_to_the_asset_root_like_other_resource_catalogs(asset_root):
    bootstrap = BootstrapService(asset_root, peruse_provider=peruse_response)
    assert PeruseService(bootstrap).status()["error"] == "peruse_catalog_invalid"
    (asset_root / asset_key(HOME_EVENT_RESOURCE)).write_bytes(resources()[HOME_EVENT_RESOURCE])
    assert PeruseService(bootstrap).status()["supported_events"] == sorted(SAVED_EVENTS)


@pytest.mark.parametrize("flag,valid", [
    (INT32_MIN, True), (INT32_MAX, True), (0, True), (INT32_MIN - 1, False), (INT32_MAX + 1, False),
    (True, False), (False, False), (1.0, False), ("1", False), (None, False),
])
def test_stored_flags_take_exactly_the_native_int32_range(state, flag, valid):
    state = set_perused_flags(state, {"existing": 1})
    state["tables"]["perused_events"][0]["FLG"] = flag
    if valid:
        assert read_perused(state) == [{"ID": "existing", "FLG": flag}]
        return
    with pytest.raises(ProtocolError) as error:
        read_perused(state)
    assert error.value.code == "peruse_state_invalid"


PERUSE_DRIFT = "Quest event sequences differ from the native command table"
NONNUMERIC_PARAMETER = "DoPeruseEvent quest IDs must be numeric script parameters"
WIDE_PARAMETER = "DoPeruseEvent quest IDs must fit the native int32 parameter"
EXCLUDED_QUEST_EVENTS = "Request 84 confirms no quest event of 1 excluded package(s): "


def excluded_quest_events(caplog):
    return [record.getMessage() for record in caplog.records
            if record.name == "aigis_ps" and record.getMessage().startswith("Request 84")]


def test_quest_events_confirm_the_first_parameter_of_each_do_peruse_event():
    packages = {
        "_ev004290.aar": quest_event("4290"),
        "_ev004386.aar": quest_event("4386", "4387"),
        "_ev004183.aar": home_event(command("DoPeruseEvent", "Param1=4184", _Param1="4183", _Param2="4185")),
        "_ev000010.aar": home_event(command("DoPeruseSave", "Save=4291", _Param1="4292"),
                                   command("PlayBgm", _Param1="4293", _AfterExpression="DoPeruseEvent();")),
    }
    assert peruse_quest_ids(quest_events(packages)) == {4290, 4386, 4387, 4183}


@pytest.mark.parametrize("member,opened", [
    ("_ev004290.aar", True), ("_ev1234567.aar", True), ("_ev-00042.aar", True), ("_ev2147483647.aar", True),
    ("_ev4290.aar", False), ("_ev0004290.aar", False), ("_ev-42.aar", False), ("_ev+04290.aar", False),
    ("_ev2147483648.aar", False), ("_ev004290.AAR", False), ("ev004290.aar", False), ("_ev004290", False),
    ("_evTrustFemale150.aar", False),
])
def test_quest_event_packages_open_only_under_the_name_the_client_formats(member, opened):
    assert peruse_quest_ids(quest_events({member: quest_event("4290")})) == ({4290} if opened else set())


@pytest.mark.parametrize("parameter,quest_id", [
    ("4290", 4290), ("+4290", 4290), ("004290", 4290), ("4290-7", 4290), ("4290+", 4290), (str(INT32_MAX), INT32_MAX),
    ("0", None), ("", None), ("-4290", None), ("-", None), ("+-4290", None), ("--4290", None), (str(INT32_MIN), None),
])
def test_quest_event_parameters_convert_like_the_script_loader(parameter, quest_id):
    expected = frozenset() if quest_id is None else frozenset((quest_id,))
    assert peruse_quest_ids(quest_events({"_ev004290.aar": quest_event(parameter)})) == expected


@pytest.mark.parametrize("parameter,reason", [
    ("0x10C2", NONNUMERIC_PARAMETER), (" 4290", NONNUMERIC_PARAMETER), ("4290 ", NONNUMERIC_PARAMETER),
    ("42.90", NONNUMERIC_PARAMETER), ("４２９０", NONNUMERIC_PARAMETER),
    (str(INT32_MAX + 1), WIDE_PARAMETER), (str(INT32_MIN - 1), WIDE_PARAMETER),
])
def test_quest_event_parameters_beyond_the_verified_loader_paths_exclude_their_package_alone(caplog, parameter, reason):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    packages = {"_ev004290.aar": quest_event("4290"), "_ev004183.aar": quest_event("4183", parameter)}
    assert peruse_quest_ids(quest_events(packages)) == {4290}
    assert excluded_quest_events(caplog) == [EXCLUDED_QUEST_EVENTS + f"_ev004183.aar: {reason}"]


def quest_contract_breaks():
    retyped = tuple((name, 1 if name == "_Param1" else kind) for name, kind in SEQUENCE_SCHEMA)
    unparameterized = tuple(field for field in SEQUENCE_SCHEMA if field[0] != "_Param1")
    text = item_table([], TEXT_SCHEMA)
    sequence = item_table([command("DoPeruseEvent", _Param1="4183")], SEQUENCE_SCHEMA)
    return {
        "retyped_parameter": (home_event(command("PlayBgm", _Param1=0), command("DoPeruseEvent", _Param1=4183),
                                         schema=retyped), PERUSE_DRIFT),
        "missing_parameter": (quest_event("4183", schema=unparameterized), PERUSE_DRIFT),
        "corrupt_package": (b"junk", None),
        "corrupt_sequence": (archive({"_evseq.atb": b"ALTB\x01", "_evtxt.atb": text}), None),
        "corrupt_text": (archive({"_evseq.atb": sequence, "_evtxt.atb": b"ALTB\x01"}), None),
    }


@pytest.mark.parametrize("mutation", sorted(quest_contract_breaks()))
def test_quest_event_packages_outside_the_client_contract_are_excluded_alone(caplog, mutation):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    package, reason = quest_contract_breaks()[mutation]
    assert peruse_quest_ids(quest_events({"_ev004290.aar": quest_event("4290"), "_ev004183.aar": package})) == {4290}
    [message] = excluded_quest_events(caplog)
    prefix = EXCLUDED_QUEST_EVENTS + "_ev004183.aar: "
    if reason is None:
        assert message.startswith(prefix) and message != prefix
    else:
        assert message == prefix + reason


def test_quest_event_packages_missing_a_table_confirm_nothing_without_an_exclusion(caplog):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    packages = {"_ev004290.aar": quest_event("4290"), "_ev004183.aar": quest_event("4183", scripts=("_evseq.atb",)),
                "_ev004386.aar": quest_event("4386", scripts=("_evtxt.atb",))}
    assert peruse_quest_ids(quest_events(packages)) == {4290}
    assert excluded_quest_events(caplog) == []


@pytest.mark.parametrize("mutation", ["missing", "not_callable", "empty", "not_bytes", "corrupt", "home_only"])
def test_unreadable_event_archives_disable_quest_confirmation(mutation):
    readers = {"missing": {}.__getitem__, "not_callable": QUEST_EVENT_RESOURCE, "empty": lambda name: b"",
               "not_bytes": lambda name: "EventArc", "corrupt": lambda name: b"ALAR\x03" + b"\xff" * 32,
               "home_only": resources().__getitem__}
    with pytest.raises(ProtocolError) as error:
        peruse_quest_ids(readers[mutation])
    assert (error.value.code, error.value.status) == ("peruse_quest_catalog_invalid", 503)


def test_quest_events_read_compressed_archives_and_resolve_duplicate_members_like_the_client():
    raw = archive([("_ev004290.aar", quest_event("4290")), ("_ev004290.aar", quest_event("4386")),
                   ("_ev004183.aar", compressed(quest_event("4183")))])
    assert peruse_quest_ids(lambda name: compressed(raw)) == {4386, 4183}


def test_exhausted_decoding_budget_confirms_no_further_quest_event(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    raw = archive({"_ev004290.aar": quest_event("4290")})
    monkeypatch.setattr(module, "RESOURCE_LIMIT", len(raw) + 1)
    assert peruse_quest_ids(lambda name: raw) == frozenset()
    assert excluded_quest_events(caplog) == [
        EXCLUDED_QUEST_EVENTS + "_ev004290.aar: Quest event packages exceed the decoded byte limit"]


def test_home_and_quest_event_archives_never_share_their_commands():
    reader = {HOME_EVENT_RESOURCE: archive({"_ev004290.aar": quest_event("4290")}),
              QUEST_EVENT_RESOURCE: archive({"_ev004183.aar": saving()})}.__getitem__
    home = PeruseCatalog(reader)
    assert home.home_event_count == 1 and home.event_ids == frozenset()
    assert peruse_quest_ids(reader) == frozenset()


@pytest.fixture(scope="module")
def native_assets():
    project = Path(__file__).resolve().parents[1]
    if not (project / "runtime/server/masterdata/active.json").is_file():
        pytest.skip("Verified local resources and public masters are required")
    snapshot = MasterDataStore(project / "runtime/server/masterdata").snapshot()
    _, entries = ResourceLibrary(project / "runtime/resources", client_hash=snapshot.client_hash).load()
    return IndexedAssetService(entries, project / "runtime/resources", project.parent / "aigisr_exe")


@pytest.fixture(scope="module")
def native_home_events(native_assets):
    return PeruseCatalog(lambda name: native_assets.read(name)[0]), native_assets.read(HOME_EVENT_RESOURCE)[0]


def test_current_resources_accept_every_native_do_peruse_save(native_home_events):
    current, raw = native_home_events
    status = current.status()
    assert status["home_event_count"] == 317 and status["supported_event_count"] == 135
    assert status["excluded_package_count"] == 0
    assert GIFT_EVENTS <= current.event_ids
    assert {"evGoddessRevival", "evTower110001", "evGohstLabyrinth", "ev_vh000000_0", "ev_vh000016_3",
            "ev_TDfes02_000000", "ev_TDfes05_Notice", "ev_Emergency260604", "ev_AprilFool2025"} <= current.event_ids
    assert not current.event_ids & {"evAnv11Episode_101", "evVoteCook01_01", "evSampleCook"}
    packages = module._members(module.decoded_resource(raw))
    assert {f"_{event_id}.aar" for event_id in current.event_ids} <= packages.keys()


def test_current_event_archive_confirms_every_native_do_peruse_event(native_assets, caplog):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    quest_ids = peruse_quest_ids(lambda name: native_assets.read(name)[0])
    assert len(quest_ids) == 230 and excluded_quest_events(caplog) == []
    assert {4183, 4290, 4497, 10001, 30018, 45004} <= quest_ids
    assert not quest_ids & {1001, 1002, 1003, 1004}
    packages = module._members(module.decoded_resource(native_assets.read(QUEST_EVENT_RESOURCE)[0]))
    assert all(module._quest_event_id(member) is not None for member in packages)
