from copy import deepcopy
from dataclasses import replace
import hashlib
import logging
from pathlib import Path
from xml.etree import ElementTree as ET
import zlib

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, loads
from aigis_ps.app import create_app
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.bootstrap import BootstrapService
from aigis_ps.errors import ProtocolError
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.peruse_episode import EPISODE_RESOURCE, MAX_CONFIG_ID_BYTES, EpisodeCatalog, PeruseEpisodeService
from aigis_ps.peruse_state import (
    HASH_COLLISION, HOME_EVENT_RESOURCE, MAX_EVENT_ID_BYTES, NUMERIC_HYPHEN, PeruseCatalog, PeruseService,
    peruse_response, read_perused, set_perused_flags,
)
from aigis_ps.protocol import decode_wire, encode_wire
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.storage import Store
from test_bootstrap import asset_root
from test_peruse_state import TEXT_SCHEMA, archive, command, home_event, mark_request, saving
from test_present_items import item_table
from test_unit_actions import request


EPISODE_SCHEMA = (("ConfigID", 1), ("CardID", 1), ("EventID", 32), ("Exp", 1), ("EpisodeIndex", 1),
                  ("TextIDEventTitle", 1), ("TextIDWinningTitle", 1), ("ConfigIDName", 32), ("DisplayExpression", 32))
UNAVAILABLE_PACKAGE = "Its home event package is excluded: "
NUMERIC_NAME = "The client keeps a numeric ConfigIDName as a number and never sends it"
CONTROL = chr(1)
LINE_BREAK = chr(13) + chr(10)
BROKEN_TABLE = b"ALTB" + bytes([1])


def episode_row(name, event_id, **changes):
    row = {"ConfigID": (zlib.crc32(str(name).encode()) ^ 0x80000000) - 0x80000000, "CardID": 23,
           "EventID": event_id, "Exp": 500, "EpisodeIndex": 1, "TextIDEventTitle": 0, "TextIDWinningTitle": 0,
           "ConfigIDName": name, "DisplayExpression": ""}
    row.update(changes)
    return row


def episode(parameters=""):
    return home_event(command("PlayBgm", "BgmID=BgmEpisode"), command("DoPeruseSaveEpisode", parameters))


PACKAGES = {
    "_evAnv11Episode_101.aar": episode(),
    "_evAnv11Episode_201.aar": episode(),
    "_evNoSave.aar": home_event(command("PlayBgm")),
    "_evSampleEpisode.aar": episode(),
    "_evTrustFemale150.aar": saving(),
}
ROWS = (episode_row("ep_anv11_101", "evAnv11Episode_101"), episode_row("ep_anv11_201", "evAnv11Episode_201"),
        episode_row("ep_nosave", "evNoSave"), episode_row("ep_missing", "evMissingPackage"))
EPISODES = {"ep_anv11_101": "evAnv11Episode_101", "ep_anv11_201": "evAnv11Episode_201"}


def resources(packages=None, rows=ROWS, *, schema=EPISODE_SCHEMA):
    return {HOME_EVENT_RESOURCE: archive(PACKAGES if packages is None else packages),
            EPISODE_RESOURCE: item_table(list(rows), schema)}


def catalog(packages=None, rows=ROWS):
    return EpisodeCatalog(resources(packages, rows).__getitem__)


@pytest.fixture
def episodes(asset_root):
    return PeruseEpisodeService(BootstrapService(asset_root, peruse_provider=peruse_response),
                                asset_reader=resources().__getitem__)


@pytest.fixture
def store(tmp_path, episodes):
    state = episodes.bootstrap.initial_state()
    state["unrelated"] = [{"preserve": [1, 2]}]
    return Store(tmp_path / "episodes.sqlite3", state)


def save_request(store, config_id="ep_anv11_101"):
    return request(store, 136, (Column("ConfigID", "S", (config_id,)),))


def rejected(service, store, envelope, code=None):
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        service.save(envelope, store)
    if code is not None:
        assert error.value.code == code
    assert store.profile() == before
    return error.value


def test_catalog_maps_each_saving_episode_to_the_event_it_launches():
    result = catalog()
    assert result.episodes == EPISODES and result.excluded == {}
    assert result.unlaunched_packages == ["_evSampleEpisode.aar"]
    status = result.status()
    assert status["ready"] is True and status["episode_count"] == len(ROWS)
    assert status["supported_episode_count"] == 2 and status["supported_episodes"] == EPISODES
    assert status["excluded_episode_count"] == 0 and status["excluded_episodes"] == []
    raw = resources()
    assert status["resource_hash"] == hashlib.sha256(raw[HOME_EVENT_RESOURCE]).hexdigest()
    assert status["config_hash"] == hashlib.sha256(raw[EPISODE_RESOURCE]).hexdigest()


@pytest.mark.parametrize("parameters,expected", [
    ("Save=ep_anv11_201", EPISODES),
    (" Save = ep_anv11_201 " + LINE_BREAK, EPISODES),
    ("Save=", {**EPISODES, "ep_redirect": "evRedirect"}),
    ("Wait=1", {**EPISODES, "ep_redirect": "evRedirect"}),
])
def test_save_argument_names_the_episode_like_the_client(parameters, expected):
    result = catalog({**PACKAGES, "_evRedirect.aar": episode(parameters)}, (*ROWS, episode_row("ep_redirect", "evRedirect")))
    assert result.episodes == expected and result.excluded == {}


@pytest.mark.parametrize("event_id,member", [("evDotted.aar", "_evDotted.aar"), ("ev.Dotted", "_ev.Dotted")])
def test_dotted_event_ids_open_the_package_named_like_the_client(event_id, member):
    result = catalog({**PACKAGES, member: episode()}, (*ROWS, episode_row("ep_dotted", event_id)))
    assert result.episodes == {**EPISODES, "ep_dotted": event_id} and result.excluded == {}
    assert result.unlaunched_packages == ["_evSampleEpisode.aar"]
    corrupt = archive({"_evseq.atb": BROKEN_TABLE, "_evtxt.atb": item_table([], TEXT_SCHEMA)})
    broken = catalog({**PACKAGES, member: corrupt}, (*ROWS, episode_row("ep_dotted", event_id)))
    assert broken.episodes == EPISODES and broken.excluded["ep_dotted"].startswith(UNAVAILABLE_PACKAGE)


def contract_breaks():
    corrupt = archive({"_evseq.atb": BROKEN_TABLE, "_evtxt.atb": item_table([], TEXT_SCHEMA)})
    long_event = "e" * (MAX_EVENT_ID_BYTES + 1)
    return {
        "numeric_name": ((), (episode_row("123", "evAnv11Episode_301"),), {"_evAnv11Episode_301.aar": episode()},
                         {"123": NUMERIC_NAME}),
        "hyphen_name": ((), (episode_row(NUMERIC_HYPHEN + "ep", "evAnv11Episode_301"),),
                        {"_evAnv11Episode_301.aar": episode()}, {NUMERIC_HYPHEN + "ep": NUMERIC_NAME}),
        "empty_name": ((), (episode_row("", "evAnv11Episode_301"),), {"_evAnv11Episode_301.aar": episode()},
                       {f"#{len(ROWS)}": "ConfigIDName must be a nonempty request-sized string"}),
        "control_name": ((), (episode_row("ep" + CONTROL + "bad", "evAnv11Episode_301"),),
                         {"_evAnv11Episode_301.aar": episode()}, {"ep" + CONTROL + "bad": None}),
        "numeric_event": ((), (episode_row("ep_number", "1024"),), {"_1024.aar": episode()},
                          {"ep_number": "The client only launches episodes under a string EventID"}),
        "oversized_event": ((), (episode_row("ep_long", long_event),), {"_" + long_event + ".aar": episode()},
                            {"ep_long": "Event IDs must be nonempty bounded strings"}),
        "shared_name": (("ep_anv11_101",), (episode_row("ep_anv11_101", "evAnv11Episode_301"),),
                        {"_evAnv11Episode_301.aar": episode()}, {"ep_anv11_101": "ConfigIDName names several episodes"}),
        "numeric_save": (("ep_anv11_201",), (), {"_evAnv11Episode_201.aar": episode("Save=123")},
                         {"ep_anv11_201": "DoPeruseSaveEpisode cannot send a numeric Save argument"}),
        "unknown_save": (("ep_anv11_201",), (), {"_evAnv11Episode_201.aar": episode("Save=ep_unknown")},
                         {"ep_anv11_201": "DoPeruseSaveEpisode sends a ConfigID without a valid episode row: 'ep_unknown'"}),
        "corrupt_package": (("ep_anv11_201",), (), {"_evAnv11Episode_201.aar": corrupt}, {"ep_anv11_201": UNAVAILABLE_PACKAGE}),
        "collision": ((), (episode_row("ep_plumless", "plumless"), episode_row("ep_buckeroo", "buckeroo")),
                      {"_plumless.aar": episode(), "_buckeroo.aar": episode()},
                      {"ep_plumless": HASH_COLLISION, "ep_buckeroo": HASH_COLLISION}),
    }


@pytest.mark.parametrize("mutation", sorted(contract_breaks()))
def test_episodes_outside_the_client_contract_are_excluded_alone(mutation):
    lost, rows, packages, expected = contract_breaks()[mutation]
    result = catalog({**PACKAGES, **packages}, (*ROWS, *rows))
    assert set(result.excluded) == set(expected)
    for key, reason in expected.items():
        if reason is None:
            assert result.excluded[key]
        elif reason == UNAVAILABLE_PACKAGE:
            assert result.excluded[key].startswith(reason) and len(result.excluded[key]) > len(reason)
        else:
            assert result.excluded[key] == reason
    assert result.episodes == {name: event_id for name, event_id in EPISODES.items() if name not in lost}
    status = result.status()
    assert status["excluded_episode_count"] == len(expected)
    assert status["excluded_episodes"] == [{"config_id": key, "reason": result.excluded[key]} for key in sorted(expected)]


@pytest.mark.parametrize("mutation", ["missing_config", "config_schema", "corrupt_config", "missing_home_events",
                                      "corrupt_home_events"])
def test_unreadable_resources_disable_the_catalog(mutation):
    files = resources()
    expected = "peruse_episode_catalog_invalid"
    if mutation == "missing_config":
        del files[EPISODE_RESOURCE]
    elif mutation == "config_schema":
        schema = tuple((name, 1 if name == "ConfigIDName" else kind) for name, kind in EPISODE_SCHEMA)
        files[EPISODE_RESOURCE] = item_table([episode_row(1, "evAnv11Episode_101")], schema)
    elif mutation == "corrupt_config":
        files[EPISODE_RESOURCE] = BROKEN_TABLE
    elif mutation == "missing_home_events":
        del files[HOME_EVENT_RESOURCE]
        expected = "peruse_catalog_invalid"
    else:
        files[HOME_EVENT_RESOURCE] = b"ALAR" + bytes([3]) + bytes([0xFF]) * 32
        expected = "peruse_catalog_invalid"
    with pytest.raises(ProtocolError) as error:
        EpisodeCatalog(files.__getitem__)
    assert error.value.code == expected and error.value.status == 503


def test_save_marks_the_launched_event_read_and_repeats_are_noops(episodes, store):
    before = store.profile()
    response = episodes.save(save_request(store), store)
    after = store.profile()
    expected = deepcopy(before.state)
    expected["perused_events_schema"] = 1
    expected["tables"]["perused_events"] = [{"ID": "evAnv11Episode_101", "FLG": 1}]
    assert after.state == expected and after.revision == before.revision + 1
    groups = {node.name: node for node in loads(response).children}
    assert set(groups) == {"SYSTEM", "PEV"} and groups["PEV"] == peruse_response(expected)
    assert episodes.save(save_request(store), store) == response
    assert store.profile() == after


@pytest.mark.parametrize("flag", [-0x80000000, -1, 7, 0x7FFFFFFF])
def test_save_preserves_nonzero_flags_without_revision(episodes, store, flag):
    profile = store.profile()
    store.update_state(set_perused_flags(profile.state, {"evAnv11Episode_101": flag, "unrelated": -9}), profile.revision)
    before = store.profile()
    response = episodes.save(save_request(store), store)
    assert store.profile() == before
    assert loads(response).children[1].children == (Column("ID", "S", ("evAnv11Episode_101",)), Column("FLG", "I", (flag,)))


def test_revoked_episode_is_marked_again(episodes, store):
    episodes.save(save_request(store), store)
    profile = store.profile()
    store.update_state(set_perused_flags(profile.state, {"evAnv11Episode_101": 0}), profile.revision)
    episodes.save(save_request(store), store)
    assert read_perused(store.profile().state) == [{"ID": "evAnv11Episode_101", "FLG": 1}]


def test_episode_and_home_event_saves_share_one_history(asset_root, tmp_path):
    bootstrap = BootstrapService(asset_root, peruse_provider=peruse_response)
    files = resources()
    home = PeruseService(bootstrap, asset_reader=files.__getitem__)
    episodes = PeruseEpisodeService(bootstrap, asset_reader=files.__getitem__)
    store = Store(tmp_path / "shared.sqlite3", bootstrap.initial_state())
    home.save(mark_request(store), store)
    episodes.save(save_request(store, "ep_anv11_201"), store)
    assert read_perused(store.profile().state) == [{"ID": "evTrustFemale150", "FLG": 1},
                                                   {"ID": "evAnv11Episode_201", "FLG": 1}]
    groups = {node.name: node for node in loads(bootstrap.handlers()[137](request(store, 137, ()), store)).children}
    assert groups["PEV"] == peruse_response(store.profile().state)


@pytest.mark.parametrize("config_id", ["ep_unknown", "ep_nosave", "ep_missing", "evAnv11Episode_101", "EP_ANV11_101",
                                       "ep_anv11_101 ", "123"])
def test_unsupported_episodes_are_refused_and_logged_with_the_request_number(episodes, store, config_id, caplog):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    assert rejected(episodes, store, save_request(store, config_id), "peruse_episode_unsupported").status == 501
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        f"Request 136 refused episode {config_id!r}: no episode sends it through DoPeruseSaveEpisode"]


def test_excluded_episodes_are_refused_with_their_reason(asset_root, tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    files = resources({**PACKAGES, "_evAnv11Episode_201.aar": episode("Save=123")})
    service = PeruseEpisodeService(BootstrapService(asset_root, peruse_provider=peruse_response), asset_reader=files.__getitem__)
    reason = "DoPeruseSaveEpisode cannot send a numeric Save argument"
    assert [record.getMessage() for record in caplog.records] == [
        f"Request 136 serves episodes without 1 excluded entries: ep_anv11_201: {reason}"]
    store = Store(tmp_path / "excluded.sqlite3", service.bootstrap.initial_state())
    caplog.clear()
    assert rejected(service, store, save_request(store, "ep_anv11_201"), "peruse_episode_excluded").status == 503
    assert [record.getMessage() for record in caplog.records] == [
        f"Request 136 refused episode 'ep_anv11_201': it is excluded ({reason})"]
    service.save(save_request(store), store)
    assert read_perused(store.profile().state) == [{"ID": "evAnv11Episode_101", "FLG": 1}]


def test_unavailable_catalog_refuses_every_save_without_touching_history(asset_root, tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    service = PeruseEpisodeService(BootstrapService(asset_root, peruse_provider=peruse_response), asset_reader={}.__getitem__)
    status = service.status()
    assert status["ready"] is False and status["error"] == "peruse_catalog_invalid"
    assert status["implemented_operations"] == [136] and "supported_episodes" not in status
    store = Store(tmp_path / "unavailable.sqlite3", service.bootstrap.initial_state())
    for config_id in EPISODES:
        caplog.clear()
        assert rejected(service, store, save_request(store, config_id), "peruse_episode_catalog_unavailable").status == 503
        assert [record.getMessage() for record in caplog.records] == [
            f"Request 136 refused episode {config_id!r}: episode resources are unavailable (peruse_catalog_invalid)"]


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "wrong_type", "empty", "multiple", "empty_name",
                                      "long_name", "nested", "missing_header", "wrong_profile"])
def test_malformed_requests_never_mark_history(episodes, store, mutation):
    envelope = save_request(store)
    root, field = envelope.document, envelope.document.find("ConfigID")
    if mutation == "missing":
        root.remove(field)
    elif mutation == "duplicate":
        root.append(deepcopy(field))
    elif mutation == "unknown":
        ET.SubElement(root, "EventID", T="S")
    elif mutation == "wrong_type":
        field.set("T", "I")
    elif mutation == "empty":
        field.remove(field[0])
    elif mutation == "multiple":
        ET.SubElement(field, "V").text = "ep_anv11_201"
    elif mutation == "empty_name":
        field[0].text = ""
    elif mutation == "long_name":
        field[0].text = "e" * (MAX_CONFIG_ID_BYTES + 1)
    elif mutation == "nested":
        ET.SubElement(field[0], "V").text = "ep_anv11_201"
    elif mutation == "missing_header":
        root.remove(root.find("UI"))
    else:
        envelope = replace(envelope, profile=replace(envelope.profile, registration_keys=(75,)))
    rejected(episodes, store, envelope, "invalid_request_fields")


@pytest.mark.parametrize("field,code", [("UI", "invalid_identity"), ("UK", "invalid_identity"), ("MK", "invalid_method_key")])
def test_authentication_precedes_the_episode_check(episodes, store, field, code):
    envelope = save_request(store, "ep_unknown")
    envelope.document.find(field + "/V").text = "other"
    assert rejected(episodes, store, envelope, code).status == 403


@pytest.mark.parametrize("target", ["save", "startup"])
def test_response_capacity_is_checked_before_commit(episodes, store, target):
    envelope = save_request(store)
    if target == "save":
        envelope = replace(envelope, profile=replace(envelope.profile, response_limit=1))
    else:
        episodes._startup_profile = replace(episodes._startup_profile, response_limit=1)
    rejected(episodes, store, envelope, "response_too_large")


def test_retry_merges_a_concurrent_history_change(episodes, store, monkeypatch):
    original = store.update_state
    calls = []

    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            profile = store.profile()
            original(set_perused_flags(profile.state, {"evTrustFemale150": 1}), profile.revision)
        return original(state, revision)

    monkeypatch.setattr(store, "update_state", update)
    episodes.save(save_request(store), store)
    assert len(calls) == 2
    assert read_perused(store.profile().state) == [{"ID": "evTrustFemale150", "FLG": 1},
                                                   {"ID": "evAnv11Episode_101", "FLG": 1}]


def test_exhausted_cas_keeps_original_state(episodes, store, monkeypatch):
    calls = []

    def fail(state, revision):
        calls.append(revision)
        raise ValueError("revision changed")

    monkeypatch.setattr(store, "update_state", fail)
    rejected(episodes, store, save_request(store), "profile_changed")
    assert len(calls) == 3


def test_native_http_save_and_137_reload_after_store_reopen(episodes, tmp_path):
    state = set_perused_flags(episodes.bootstrap.initial_state(), {"older-history": -2})

    def application():
        return create_app(tmp_path / "episode-http", initial_state=state,
                          handlers=episodes.bootstrap.handlers() | episodes.handlers())

    app = application()
    envelope = save_request(app.state.store)
    assert envelope.profile.route == "vXxtvTnU" and envelope.profile.registration_keys == (136,)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reply = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert reply.status_code == 200
        groups = {node.name: node for node in loads(decode_wire(reply.content, envelope.profile.response_key)).children}
        assert groups["PEV"].children == (Column("ID", "S", ("evAnv11Episode_101",)), Column("FLG", "I", (1,)))
    saved = app.state.store.profile()
    reopened = application()
    envelope = request(reopened.state.store, 137, ())
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        reply = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        groups = {node.name: node for node in loads(decode_wire(reply.content, envelope.profile.response_key)).children}
        assert groups["PEV"] == peruse_response(saved.state)
    assert read_perused(saved.state) == [{"ID": "older-history", "FLG": -2}, {"ID": "evAnv11Episode_101", "FLG": 1}]


def test_handlers_and_status_describe_the_local_rules(episodes):
    assert set(episodes.handlers()) == {136}
    status = episodes.status()
    assert status["implemented_operations"] == [136] and status["supported_episodes"] == EPISODES
    assert status["local_limits"] == {"config_id_bytes": MAX_CONFIG_ID_BYTES, "event_id_bytes": MAX_EVENT_ID_BYTES}
    assert "not granted" in status["reward_policy"]


@pytest.fixture(scope="module")
def native_resources():
    project = Path(__file__).resolve().parents[1]
    if not (project / "runtime/server/masterdata/active.json").is_file():
        pytest.skip("Verified local resources and public masters are required")
    snapshot = MasterDataStore(project / "runtime/server/masterdata").snapshot()
    _, entries = ResourceLibrary(project / "runtime/resources", client_hash=snapshot.client_hash).load()
    assets = IndexedAssetService(entries, project / "runtime/resources", project.parent / "aigisr_exe")
    return lambda name: assets.read(name)[0]


def test_current_resources_accept_every_native_episode_save(native_resources):
    current = EpisodeCatalog(native_resources)
    status = current.status()
    assert status["episode_count"] == status["supported_episode_count"] == 127
    assert status["excluded_episode_count"] == 0 and status["unlaunched_packages"] == ["_evSampleEpisode.aar"]
    assert current.episodes["ep_anv11_101"] == "evAnv11Episode_101"
    assert current.episodes["ep_vote2_101"] == "evVoteEpisode2_101"
    assert len(set(current.episodes.values())) == 127
    assert not set(current.episodes.values()) & PeruseCatalog(native_resources).event_ids
