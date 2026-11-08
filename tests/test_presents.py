from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import logging
import struct
from types import MappingProxyType, SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.app import create_app
from aigis_ps.bootstrap import BootstrapService
from aigis_ps.business_calendar import civil_day
from aigis_ps.crystal_wallet import wallet_totals
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.player_state import PlayerStateService
from aigis_ps.possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES
from aigis_ps.present_recovery import PresentRecoveryCatalog
from aigis_ps.presents import (GIFT_BOX_DEPENDENCIES, ITEM_CATALOG, MAX_PRESENTS, PRESENT_CAPACITY_ERRORS, PRESENT_LIMITS,
                               SPLIT_UNNEEDED, PresentService)
from aigis_ps.protocol import ProtocolRegistry, decode_wire, encode_response, encode_wire
from aigis_ps.quest_economy import game_param_response
from aigis_ps.recovery_service import RecoveryService
from aigis_ps.storage import Store
from test_bootstrap import asset_root
from test_player_state import assets, growth_setup, schema_bytes
from test_present_items import components, emblem_mail, ownership, row as item_row
from test_present_recovery_catalog import item as recovery_item, resources as recovery_resources
from test_unit_actions import request
from test_vote_cooking_catalog import build_cooking


DAY = civil_day(datetime(2026, 11, 1, 12, tzinfo=JST))
STORED_NAME = "x" * STORED_ITEM_NAME_BYTES
CELL_NAME = "y" * MAX_ITEM_NAME_BYTES
TICKET_ITEM, PLAIN_ITEM = "item_smn_ticket_fixture", "item_plain_fixture"
TICKET_FIELDS = {16: {"SummonID": 350}, 20: {"SelectionConfigID": 1},
                 22: {"ItemID": TICKET_ITEM, "NotificationCategory": 7}}
BUNDLED_ID, BUNDLED_TICKETS = 3, 3


def template_table(*, missing=False, changed=False):
    names = ("PresentID", "CardID", "InitExp", "InitSkillID", "InitSkillLevel", "CostMod", "InitLove",
             "EvolutionCount", "ClassChangeEnable", "ClassChangeStatusMod")
    schema = schema_bytes([(name, index * 4, 5 if index == 8 else 1, b"\xff" if index == 8 else b"\0" * 8)
                           for index, name in enumerate(names)], 40)
    rows = [(i, i + 54, int(changed), -1, 1, 0, 0, 0, False, 0) for i in (0, *range(2, 5 if missing else 6))]
    data = b"".join(struct.pack("<8i?3xi", *row) for row in rows)
    return struct.pack("<4sBBHHHI4s", b"ALTB", 1, 0x10, len(rows), 20, 20 + len(schema), 40, b"pres") + schema + data


@pytest.fixture
def presents(asset_root, growth_setup):
    root, resources, master = growth_setup
    columns = loads(master).children
    master = dumps(Container(children=tuple(Column(column.name, column.type,
        (1, 56, 57, 58, 59) if column.name == "CardID" else column.values[:5]) for column in columns)))
    players = PlayerStateService(root, lambda _: master, asset_reader=resources.__getitem__)
    bootstrap = BootstrapService(asset_root, clock=lambda: datetime(2026, 11, 1, 12))
    return PresentService(bootstrap, players, asset_reader=lambda _: template_table())


@pytest.fixture
def state(presents):
    state = presents.players.initialize(presents.bootstrap.initial_state())
    state["unrelated"] = [{"keep": [1, 2]}]
    return state


@pytest.fixture
def store(tmp_path, state):
    return Store(tmp_path / "presents.sqlite3", state)


def unit(template_id=2, count=1):
    return {"kind": "unit", "template_id": template_id, "count": count, "description": "好感度報酬"}


def crystal(amount=3):
    return {"kind": "free_crystal", "amount": amount, "description": "好感度報酬"}


def queue(presents, store, rewards):
    profile = store.profile()
    updated, rows = presents.enqueue(profile.state, rewards)
    store.update_state(updated, profile.revision)
    return [row["PresentID"] for row in rows]


def claim_request(store, ids, number=1):
    return request(store, number, (Container("RT", (Container("PT2", (Column("PresentID", "I", tuple(ids)),)),)),))


def children(xml):
    return {child.name: child for child in loads(xml).children}


def rejected(presents, store, envelope, code=None):
    before = store.profile()
    with pytest.raises(ProtocolError) as error:
        presents.receive(envelope, store)
    if code:
        assert error.value.code == code
    assert store.profile() == before


def test_enqueue_allocates_unique_mail_ids_and_preserves_state(presents, state):
    before = deepcopy(state)
    updated, rows = presents.enqueue(state, [unit(2, 2), unit(3), unit(4), unit(5), crystal()])
    assert [row["PresentID"] for row in rows] == list(range(1, 7))
    assert [row["Content"] for row in rows] == [2, 2, 3, 4, 5, 3]
    assert updated["last_present_id"] == 6 and updated["presents_schema"] == 1
    assert state == before and updated["player_state"] == before["player_state"]
    assert updated["tables"]["crystals"] == []
    full, delta = presents.response(updated), presents.response(updated, [6, 2])
    assert [column.name for column in full.children] == ["PresentID", "A1", "A2", "A3", "A4", "A6", "A9"]
    assert {column.name: column.values for column in full.children[4:]} == {"A4": (0,) * 6, "A6": ("",) * 6, "A9": (0,) * 6}
    assert [column.name for column in delta.children] == ["PresentID", "A1", "A2", "A3"]
    assert delta.children[0].values == (6, 2) and delta.children[1].values == (0, 1)
    assert all(column.name not in ("A5", "Date") for column in full.children)
    rows[0]["Content"] = 999
    updated["unrelated"][0]["keep"].append(3)
    assert updated["tables"]["presents"][0]["Content"] == 2 and state == before


def test_empty_enqueue_and_legacy_mailbox_do_not_create_schema(presents, state):
    updated, rows = presents.enqueue(state, [])
    assert updated == state and updated is not state and rows == []
    assert "presents_schema" not in updated
    updated["unrelated"][0]["keep"].append(3)
    assert state["unrelated"][0]["keep"] == [1, 2]
    assert all(not column.values for column in presents.response(state).children)


def test_zero_template_definition_does_not_become_a_supported_mail(presents, state):
    assert presents.status()["ready"] is True
    assert presents.status()["supported_templates"] == [2, 3, 4, 5]
    with pytest.raises(ProtocolError):
        presents.enqueue(state, [unit(0)])


def test_templates_grant_once_and_mail_ids_are_never_reused(presents, state):
    queued, rows = presents.enqueue(state, [unit(i) for i in range(2, 6)])
    before = deepcopy(queued)
    updated, granted, crystals = presents.claim(queued, [row["PresentID"] for row in rows], acquisition_day=DAY)
    assert [row["CardID"] for row in granted] == [56, 57, 58, 59]
    assert all(row["Exp"] == 0 and row["SkillLevel"] == 1 and row["Love"] == 0 for row in granted)
    assert crystals == 0 and updated["tables"]["presents"] == [] and queued == before
    again, new = presents.enqueue(updated, [unit()])
    assert new[0]["PresentID"] == 5 and again["last_present_id"] == 5
    with pytest.raises(ProtocolError, match="already claimed"):
        presents.claim(updated, [1], acquisition_day=DAY)
    granted[0]["CardID"] = 999
    assert updated["player_state"]["units"][1]["CardID"] == 56
    assert "present_claim_history" not in updated


@pytest.mark.parametrize("number", [1, 2, 117])
def test_claim_commits_units_crystals_and_receipt_once_with_native_response(presents, store, number):
    profile = store.profile()
    profile.state["tables"]["crystals"] = [{"FreeFlag": False, "Num": 7, "Date": DAY},
                                             {"FreeFlag": True, "Num": 11, "Date": DAY - 1},
                                             {"FreeFlag": True, "Num": 4, "Date": DAY}]
    store.update_state(profile.state, profile.revision)
    ids = queue(presents, store, [unit(), crystal(3), unit(3)])
    before = store.profile()
    reply = presents.receive(claim_request(store, ids[:2], number), store)
    after = store.profile()
    assert after.revision == before.revision + 1
    assert [row["PresentID"] for row in presents.rows(after.state)] == [ids[2]]
    groups = children(reply)
    assert set(groups) == {"SYSTEM", "PBP", "PPU", "PFC"}
    assert groups["PBP"].children == (Column("A1", "S", ("UX",)),
        Column("A2", "I", (after.state["player_state"]["battle"]["UX"],)))
    assert after.state["player_state"]["battle"]["UX"] == before.state["player_state"]["battle"]["UX"] + 1
    assert len(groups["PPU"].children) == 21 and groups["PPU"].children[0].values == (56,)
    pfc = groups["PFC"]
    if number == 117:
        assert [column.name for column in pfc.children] == ["UserID", "Date", "Num"]
        assert pfc.children[1].values == (DAY - 1, DAY) and pfc.children[2].values == (11, 7)
    else:
        assert pfc.children == (Column("key", "S", ("A2", "Date")), Column("value", "I", (7, DAY)))
    assert wallet_totals(after.state) == {"paid": 7, "free": 18, "total": 25}
    assert after.state["tables"]["crystals"][0] == before.state["tables"]["crystals"][0]
    assert after.state["present_claim_history"][0]["revision"] == after.revision
    handlers = presents.handlers()
    assert handlers[1] is handlers[2] is handlers[117]


def test_unit_claim_does_not_emit_empty_wallet_updates(presents, store):
    ids = queue(presents, store, [unit()])
    assert set(children(presents.receive(claim_request(store, ids), store))) == {"SYSTEM", "PBP", "PPU"}


def test_exact_retry_survives_service_restart_and_rejects_stale_revision(presents, store):
    ids = queue(presents, store, [unit()])
    envelope = claim_request(store, ids)
    response = presents.receive(envelope, store)
    saved = store.profile()
    restarted = PresentService(presents.bootstrap, presents.players, asset_reader=lambda _: template_table())
    assert restarted.receive(envelope, Store(store.path)) == response and store.profile() == saved
    changed = deepcopy(saved.state)
    changed["unrelated"].append({"later": True})
    store.update_state(changed, saved.revision)
    rejected(restarted, store, envelope, "present_replay_stale")


@pytest.mark.parametrize("unavailable", ["templates", "unit_schema", "master", "reference", "state"])
def test_exact_retry_validates_current_resources_and_player_state(presents, store, monkeypatch, unavailable):
    ids = queue(presents, store, [unit()])
    envelope = claim_request(store, ids)
    presents.receive(envelope, store)
    if unavailable == "templates":
        presents = PresentService(presents.bootstrap, presents.players, asset_reader=lambda _: b"bad")
        code = "present_templates_unavailable"
    elif unavailable == "unit_schema":
        presents.players.issues["player_unit_schema"] = "Unavailable schema"
        code = "player_contract_unavailable"
    elif unavailable == "master":
        presents.players.master_provider = None
        code = "player_master_unavailable"
    elif unavailable == "reference":
        presents.players.class_ids = set()
        code = "player_reference_invalid"
    else:
        original = store.profile
        def invalid_profile():
            profile = original()
            profile.state["player_state"]["units"][0]["CardID"] = True
            return profile
        monkeypatch.setattr(store, "profile", invalid_profile)
        code = "player_units_invalid"
    rejected(presents, store, envelope, code)


def test_evicted_receipt_cannot_issue_another_grant(presents, store, monkeypatch):
    monkeypatch.setattr("aigis_ps.presents.MAX_RETRIES", 1)
    ids = queue(presents, store, [unit(), unit(3)])
    first = claim_request(store, ids[:1])
    presents.receive(first, store)
    presents.receive(claim_request(store, ids[1:]), store)
    assert len(store.profile().state["present_claim_history"]) == 1
    rejected(presents, store, first, "present_not_found")


@pytest.mark.parametrize("paid,free,amount", [(1001, 0, 1), (1000, 1000, 1), (1, 999, 2)])
def test_wallet_capacity_rejects_entire_mixed_batch(presents, store, paid, free, amount):
    profile = store.profile()
    profile.state["tables"]["crystals"] = [{"FreeFlag": False, "Num": paid, "Date": DAY},
                                             {"FreeFlag": True, "Num": free, "Date": DAY}]
    store.update_state(profile.state, profile.revision)
    ids = queue(presents, store, [unit(), crystal(amount)])
    rejected(presents, store, claim_request(store, ids), "present_crystal_capacity")


def test_wallet_exact_capacity_is_allowed(presents, state):
    state["tables"]["crystals"] = [{"FreeFlag": False, "Num": 1000, "Date": DAY},
                                    {"FreeFlag": True, "Num": 999, "Date": DAY}]
    queued, rows = presents.enqueue(state, [crystal(1)])
    result, _, amount = presents.claim(queued, [rows[0]["PresentID"]], acquisition_day=DAY)
    assert amount == 1 and wallet_totals(result)["total"] == 2000


@pytest.mark.parametrize("reward", [unit(1), unit(True), unit(count=True), unit(count=0), unit(count=-1),
    unit(count=0x80000000), crystal(True), crystal(0), crystal(-1), crystal(0x80000000),
    {"kind": "paid_crystal", "amount": 1, "description": ""}, {**unit(), "A5": 0},
    {**unit(), "description": "\x00"}, {**unit(), "description": "猫" * 1366}])
def test_invalid_enqueue_is_atomic(presents, state, reward):
    before = deepcopy(state)
    with pytest.raises(ProtocolError):
        presents.enqueue(state, [crystal(), reward])
    assert state == before


def test_mail_capacity_and_high_water_overflow(presents, state):
    full, rows = presents.enqueue(state, [unit(count=MAX_PRESENTS)])
    assert len(rows) == MAX_PRESENTS
    assert loads(dumps(Container(children=(presents.response(full),)), limits=PRESENT_LIMITS)).children[0] == presents.response(full)
    with pytest.raises(ProtocolError) as error:
        presents.enqueue(full, [unit()])
    assert error.value.code == "present_capacity"
    state.update(presents_schema=1, last_present_id=0x7FFFFFFF)
    with pytest.raises(ProtocolError) as error:
        presents.enqueue(state, [unit()])
    assert error.value.code == "present_identity_exhausted"


@pytest.mark.parametrize("mutation", ["unversioned_rows", "version", "bool_version", "counter", "duplicate", "unknown_type", "unknown_template", "extra", "bool_id"])
def test_invalid_old_mailbox_is_not_silently_replaced(presents, state, mutation):
    state, _ = presents.enqueue(state, [unit()])
    if mutation == "unversioned_rows":
        state.pop("presents_schema")
    elif mutation == "version":
        state["presents_schema"] = 2
    elif mutation == "bool_version":
        state["presents_schema"] = True
    elif mutation == "counter":
        state["last_present_id"] = 0
    elif mutation == "duplicate":
        state["tables"]["presents"] *= 2
    elif mutation == "unknown_type":
        state["tables"]["presents"][0]["Type"] = 17
    elif mutation == "unknown_template":
        state["tables"]["presents"][0]["Content"] = 1000
    elif mutation == "extra":
        state["tables"]["presents"][0]["Date"] = DAY
    elif mutation == "bool_id":
        state["tables"]["presents"][0]["PresentID"] = True
    before = deepcopy(state)
    for action in (lambda: presents.response(state), lambda: presents.enqueue(state, [unit()]),
                   lambda: presents.claim(state, [1], acquisition_day=DAY)):
        with pytest.raises(ProtocolError) as error:
            action()
        assert error.value.code == "present_state_invalid" and error.value.status == 503
        assert state == before


@pytest.mark.parametrize("ids", [[], [0], [-1], [True], [0x80000000], [1, 1], "1", [1, 999]])
def test_invalid_or_missing_claim_id_is_atomic(presents, state, ids):
    state, _ = presents.enqueue(state, [unit()])
    before = deepcopy(state)
    with pytest.raises(ProtocolError):
        presents.claim(state, ids, acquisition_day=DAY)
    assert state == before


@pytest.mark.parametrize("mutation", ["missing_RT", "extra_root", "extra_RT", "select", "duplicate_ID", "wrong_type", "empty", "negative", "nested", "bad_header"])
def test_malformed_claim_never_consumes_mail(presents, store, mutation):
    ids = queue(presents, store, [unit()])
    envelope = claim_request(store, ids)
    root = envelope.document
    table = root.find("RT/PT2")
    column = table.find("PresentID")
    if mutation == "missing_RT":
        root.remove(root.find("RT"))
    elif mutation == "extra_root":
        ET.SubElement(root, "Unknown")
    elif mutation == "extra_RT":
        ET.SubElement(root.find("RT"), "Unknown")
    elif mutation == "select":
        ET.SubElement(table, "SelectID", T="I")
    elif mutation == "duplicate_ID":
        table.append(deepcopy(column))
    elif mutation == "wrong_type":
        column.set("T", "S")
    elif mutation == "empty":
        column.remove(column[0])
    elif mutation == "negative":
        column[0].text = "-1"
    elif mutation == "nested":
        ET.SubElement(column[0], "V").text = "1"
    elif mutation == "bad_header":
        root.find("CT/V").text = "true"
    rejected(presents, store, envelope, "invalid_request_fields")


@pytest.mark.parametrize("field", ["UI", "UK", "MK"])
def test_claim_authentication_including_retry(presents, store, field):
    ids = queue(presents, store, [unit()])
    envelope = claim_request(store, ids)
    presents.receive(envelope, store)
    envelope.document.find(field + "/V").text = "wrong"
    rejected(presents, store, envelope)


@pytest.mark.parametrize("target", ["list", "unit", "claim", "history"])
def test_all_response_capacity_checks_precede_commit(presents, store, monkeypatch, target):
    ids = queue(presents, store, [unit(), crystal()])
    envelope = claim_request(store, ids)
    if target == "list":
        presents._list_profile = replace(presents._list_profile, response_limit=1)
    elif target == "unit":
        presents._unit_profile = replace(presents._unit_profile, response_limit=1)
    elif target == "claim":
        envelope = replace(envelope, profile=replace(envelope.profile, response_limit=1))
    else:
        monkeypatch.setattr("aigis_ps.presents.MAX_RETRY_BYTES", 1)
    rejected(presents, store, envelope)
    assert "present_claim_history" not in store.profile().state


def test_native_response_overflow_keeps_rewards_pending_like_a_full_mailbox():
    profile = replace(ProtocolRegistry.load().by_registration[34], response_limit=1)
    with pytest.raises(ProtocolError) as caught:
        encode_response(profile, b"<DA><N>1</N></DA>")
    assert caught.value.code in PRESENT_CAPACITY_ERRORS


def test_claim_cas_retry_retains_concurrent_mail(presents, store, monkeypatch):
    ids = queue(presents, store, [unit()])
    original = store.update_state
    calls = []
    def update(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            profile = store.profile()
            concurrent, _ = presents.enqueue(profile.state, [crystal(5)])
            concurrent["unrelated"].append({"concurrent": True})
            original(concurrent, profile.revision)
        return original(state, revision)
    monkeypatch.setattr(store, "update_state", update)
    response = presents.receive(claim_request(store, ids), store)
    assert len(calls) == 2 and set(children(response)) == {"SYSTEM", "PBP", "PPU"}
    assert presents.rows(store.profile().state)[0]["Type"] == 0
    assert store.profile().state["unrelated"][-1] == {"concurrent": True}


def test_exhausted_cas_does_not_consume_mail_or_save_receipt(presents, store, monkeypatch):
    ids = queue(presents, store, [unit()])
    def fail(*_):
        raise ValueError("revision changed")
    monkeypatch.setattr(store, "update_state", fail)
    rejected(presents, store, claim_request(store, ids), "profile_changed")
    assert "present_claim_history" not in store.profile().state


def test_concurrent_identical_claim_uses_committed_receipt(presents, store, monkeypatch):
    ids = queue(presents, store, [unit()])
    envelope = claim_request(store, ids)
    original = store.update_state
    concurrent_responses = []
    other = PresentService(presents.bootstrap, presents.players, asset_reader=lambda _: template_table())
    def update(state, revision):
        if not concurrent_responses:
            concurrent_responses.append(other.receive(envelope, Store(store.path)))
        return original(state, revision)
    monkeypatch.setattr(store, "update_state", update)
    response = presents.receive(envelope, store)
    assert response == concurrent_responses[0]
    assert len(store.profile().state["player_state"]["units"]) == 2
    assert len(store.profile().state["present_claim_history"]) == 1


@pytest.mark.parametrize("day", [0, -1, True, 0x80000000])
def test_invalid_acquisition_day_cannot_consume_present(presents, state, day):
    state, rows = presents.enqueue(state, [unit()])
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        presents.claim(state, [rows[0]["PresentID"]], acquisition_day=day)
    assert error.value.code == "present_day_invalid" and state == before


def test_roster_capacity_failure_keeps_mail_wallet_and_receipt_unchanged(presents, store, monkeypatch):
    ids = queue(presents, store, [unit(), crystal()])
    monkeypatch.setattr("aigis_ps.player_state.MAX_PLAYER_UNITS", 1)
    rejected(presents, store, claim_request(store, ids), "unit_roster_capacity")


@pytest.mark.parametrize("number", [1, 2, 117])
def test_crystal_claim_clears_previous_unit_append(presents, store, number):
    ids = queue(presents, store, [unit(), crystal()])
    initial = children(presents.receive(claim_request(store, ids[:1], number), store))
    assert initial["PPU"].children[0].values == (56,)
    groups = children(presents.receive(claim_request(store, ids[1:], number), store))
    assert set(groups) == {"SYSTEM", "PPU", "PFC"}
    assert len(groups["PPU"].children) == 21
    assert all(not column.values for column in groups["PPU"].children)


@pytest.mark.parametrize("mutation", ["version", "unversioned", "response", "revision", "duplicate"])
def test_corrupt_claim_history_is_rejected_before_writing(presents, store, mutation):
    ids = queue(presents, store, [unit(), unit(3)])
    presents.receive(claim_request(store, ids[:1]), store)
    profile = store.profile()
    if mutation == "version":
        profile.state["present_claim_history_schema"] = True
    elif mutation == "unversioned":
        profile.state.pop("present_claim_history_schema")
    elif mutation == "response":
        profile.state["present_claim_history"][0]["response"] = "not base64"
    elif mutation == "revision":
        profile.state["present_claim_history"][0]["revision"] = 999
    elif mutation == "duplicate":
        profile.state["present_claim_history"] *= 2
    store.update_state(profile.state, profile.revision)
    rejected(presents, store, claim_request(store, ids[1:]), "present_history_invalid")


@pytest.mark.parametrize("raw", [b"bad", template_table(missing=True), template_table(changed=True)])
def test_unverified_template_resource_is_explicit(presents, state, raw):
    unavailable = PresentService(presents.bootstrap, presents.players, asset_reader=lambda _: raw)
    assert unavailable.status()["ready"] is False
    with pytest.raises(ProtocolError) as error:
        unavailable.enqueue(state, [unit()])
    assert error.value.code == "present_templates_unavailable"


def test_native_http_claim_and_reopened_mailbox(presents, state, tmp_path):
    state, rows = presents.enqueue(state, [unit(), crystal()])
    def listing(envelope, store):
        profile = presents.bootstrap._authenticate(envelope, store)
        return dumps(Container(children=(presents.bootstrap._system(profile), presents.response(profile.state))), limits=PRESENT_LIMITS)
    def application():
        return create_app(tmp_path / "http", initial_state=state, handlers={42: listing, **presents.handlers()})
    app = application()
    envelope = claim_request(app.state.store, [rows[0]["PresentID"]])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reply = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert reply.status_code == 200
        assert children(decode_wire(reply.content, envelope.profile.response_key))["PPU"].children[0].values == (56,)
    saved = app.state.store.profile()
    reopened = application()
    envelope = request(reopened.state.store, 42, ())
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        reply = client.post("/api/" + envelope.profile.route, content=encode_wire(envelope.xml, envelope.profile.request_key))
        assert reply.status_code == 200
        assert children(decode_wire(reply.content, envelope.profile.response_key))["PPT"] == presents.response(saved.state)
    assert reopened.state.store.profile() == saved


def with_items(presents, *names):
    case = components([ownership(12), *(item_row(name) for name in names)])
    presents.possessions, presents.item_catalog = case.possessions, case.catalog
    return presents


def blessing():
    return {"kind": "item", "item_id": "blessing", "amount": 1, "description": "加護"}


def table_request(store, number, columns):
    return request(store, number, (Container("RT", (Container("PT2", tuple(columns)),)),))


def assert_empty_claim(reply):
    groups = children(reply)
    assert set(groups) == {"SYSTEM", "PPU"}
    assert len(groups["PPU"].children) == 21 and all(not column.values for column in groups["PPU"].children)


def test_batch_keeps_individually_claimed_mail_and_claims_the_rest(presents, store, caplog):
    with_items(presents)
    ids = queue(presents, store, [crystal(3), blessing(), unit()])
    before = store.profile()
    envelope = claim_request(store, ids, 117)
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        reply = presents.receive(envelope, store)
    after = store.profile()
    assert set(children(reply)) == {"SYSTEM", "PBP", "PPU", "PFC"}
    assert children(reply)["PPU"].children[0].values == (56,)
    assert [row["PresentID"] for row in presents.rows(after.state)] == [ids[1]]
    assert wallet_totals(after.state)["free"] == 3
    assert after.state["tables"]["possessions"] == before.state["tables"]["possessions"]
    assert after.revision == before.revision + 1 and len(after.state["present_claim_history"]) == 1
    assert any(f"keeps [{ids[1]}]" in message for message in caplog.messages)
    assert presents.receive(envelope, store) == reply and store.profile() == after


def test_batch_fills_capacity_in_request_order_around_kept_mail(presents, store):
    with_items(presents)
    ids = queue(presents, store, [crystal(600), blessing(), crystal(500)])
    presents.receive(claim_request(store, [ids[2], ids[1], ids[0]], 117), store)
    saved = store.profile().state
    assert wallet_totals(saved)["free"] == 500
    assert [row["PresentID"] for row in presents.rows(saved)] == ids[:2]


def test_batch_of_only_individually_claimed_mail_replies_empty_without_a_receipt(presents, store, caplog):
    with_items(presents)
    ids = queue(presents, store, [blessing(), blessing()])
    before = store.profile()
    envelope = claim_request(store, ids, 117)
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        reply = presents.receive(envelope, store)
    assert_empty_claim(reply)
    assert store.profile() == before and "present_claim_history" not in before.state
    assert any(f"claimed nothing among {ids}" in message for message in caplog.messages)
    assert presents.receive(envelope, store) == reply and store.profile() == before


def test_single_claims_still_refuse_individually_claimed_mail_in_a_selection(presents, store):
    with_items(presents)
    ids = queue(presents, store, [blessing(), crystal()])
    rejected(presents, store, claim_request(store, ids), "present_item_single_only")


@pytest.mark.parametrize("columns", [(), (Column("PresentID", "I", ()),)], ids=["without_column", "without_values"])
def test_empty_batch_list_replies_empty_without_a_receipt(presents, store, caplog, columns):
    queue(presents, store, [unit()])
    before = store.profile()
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        reply = presents.receive(table_request(store, 117, columns), store)
    assert_empty_claim(reply)
    assert store.profile() == before
    assert any("claimed nothing among []" in message for message in caplog.messages)


@pytest.mark.parametrize("number", [1, 2])
@pytest.mark.parametrize("columns", [(), (Column("PresentID", "I", ()),)], ids=["without_column", "without_values"])
def test_single_claim_still_requires_a_present_identity(presents, store, number, columns):
    queue(presents, store, [unit()])
    rejected(presents, store, table_request(store, number, columns), "invalid_request_fields")


def test_batch_does_not_accept_selection_fields(presents, store):
    ids = queue(presents, store, [unit()])
    rejected(presents, store, table_request(store, 117, (Column("PresentID", "I", tuple(ids)),
        Column("SelectID", "I", (1,)), Column("SelectCardID", "I", (56,)))), "invalid_request_fields")


def test_capacity_blocked_batch_replies_empty_without_a_receipt(presents, store, monkeypatch):
    ids = queue(presents, store, [unit(), unit(3)])
    monkeypatch.setattr("aigis_ps.player_state.MAX_PLAYER_UNITS", 1)
    before = store.profile()
    envelope = claim_request(store, ids, 117)
    reply = presents.receive(envelope, store)
    assert_empty_claim(reply)
    assert store.profile() == before
    assert presents.receive(envelope, store) == reply and store.profile() == before


def test_partial_claim_accepts_an_empty_selection_without_change(presents, state):
    state, _ = presents.enqueue(state, [unit()])
    before = deepcopy(state)
    updated, units, crystals = presents.claim(state, [], acquisition_day=DAY, partial=True)
    assert updated == before and updated is not state and units == [] and crystals == 0
    for partial in (False, 1):
        with pytest.raises(ProtocolError) as error:
            presents.claim(state, [], acquisition_day=DAY, partial=partial)
        assert error.value.code == "present_selection_invalid"
    assert state == before


@pytest.mark.parametrize("kind", ["item", "charisma_recovery"])
def test_new_mail_needs_item_identities_that_fit_the_native_cell(presents, state, kind):
    if kind == "item":
        with_items(presents, STORED_NAME, CELL_NAME)
    else:
        source = recovery_resources([recovery_item(STORED_NAME), recovery_item(CELL_NAME)])
        presents.recovery_catalog = PresentRecoveryCatalog(source.__getitem__)

    def reward(name):
        result = {"kind": kind, "item_id": name, "description": "long identity"}
        return {**result, "amount": 1} if kind == "item" else result

    before = deepcopy(state)
    for action in (lambda: presents.enqueue(state, [crystal(), reward(STORED_NAME)]),
                   lambda: presents.preview([reward(STORED_NAME)], 0)):
        with pytest.raises(ProtocolError) as error:
            action()
        assert (error.value.code, error.value.status) == ("present_item_unreplayable", 503)
        assert state == before
    _, rows = presents.enqueue(state, [reward(CELL_NAME)])
    assert rows[0]["ItemID"] == CELL_NAME


def test_stored_mail_keeps_the_earlier_item_identity_bound(presents, state):
    with_items(presents, STORED_NAME)
    state, rows = presents.enqueue(state, [crystal()])
    state["tables"]["presents"].append({"PresentID": 2, "Type": 22, "Content": 1, "Description": "stored",
                                        "ItemID": STORED_NAME})
    state["last_present_id"] = 2
    assert presents.rows(state)[-1]["ItemID"] == STORED_NAME
    assert {column.name: column.values for column in presents.response(state).children}["A6"] == ("", STORED_NAME)


def fingerprinted(character):
    return SimpleNamespace(content_version=character * 64)


def gift_box(**versions):
    return {**dict.fromkeys(GIFT_BOX_DEPENDENCIES), **versions}


def masked(configuration, current):
    return tuple(name for name, version in current.items() if version is not None and configuration[name] is None)


def test_gift_box_dependencies_match_the_names_in_committed_mail_digests():
    assert ITEM_CATALOG == "item_catalog"
    assert GIFT_BOX_DEPENDENCIES == ("item_catalog", "recovery_catalog", "tickets", "item_tickets", "selection_tickets")


def test_gift_box_configurations_start_with_the_current_wiring_and_mask_fewest_first(presents):
    with_items(presents)
    presents.tickets, presents.selection_tickets = fingerprinted("1"), fingerprinted("2")
    current = gift_box(item_catalog=presents.item_catalog.content_version, tickets="1" * 64, selection_tickets="2" * 64)
    configurations = presents.gift_box_configurations()
    assert dict(configurations[0]) == current
    assert all(tuple(configuration) == GIFT_BOX_DEPENDENCIES for configuration in configurations)
    assert all(configuration[name] in (None, version)
               for configuration in configurations for name, version in current.items())
    assert [masked(configuration, current) for configuration in configurations] == [
        (), (ITEM_CATALOG,), ("tickets",), ("selection_tickets",), (ITEM_CATALOG, "tickets"),
        (ITEM_CATALOG, "selection_tickets"), ("tickets", "selection_tickets"), (ITEM_CATALOG, "tickets", "selection_tickets")]
    with pytest.raises(TypeError):
        configurations[0]["tickets"] = None


def test_gift_box_configurations_replay_every_item_catalog_version_while_it_is_attached(presents):
    case = emblem_mail()
    presents.possessions, presents.item_catalog = case.possessions, case.catalog
    presents.recovery_catalog = PresentRecoveryCatalog(recovery_resources().__getitem__)
    items = (case.catalog.content_version, case.plain.content_version, None)
    assert case.catalog.content_version != case.plain.content_version
    assert [dict(configuration) for configuration in presents.gift_box_configurations()] == [
        gift_box(item_catalog=item_version, recovery_catalog=recovery_version)
        for recovery_version in (presents.recovery_catalog.content_version, None) for item_version in items]


def test_gift_box_configurations_list_the_current_item_version_before_the_sorted_others(presents):
    versions = ("c" * 64, "a" * 64, "b" * 64)
    presents.item_catalog = SimpleNamespace(content_version=versions[0],
                                            replayable_content_versions=lambda: frozenset(versions))
    assert [dict(configuration) for configuration in presents.gift_box_configurations()] == [
        gift_box(item_catalog=version) for version in (*versions, None)]


def test_gift_box_without_mail_dependencies_has_only_the_empty_configuration(presents):
    assert [dict(configuration) for configuration in presents.gift_box_configurations()] == [gift_box()]


def with_recovery(presents):
    presents.recovery = RecoveryService(presents.bootstrap)
    presents.recovery_catalog = PresentRecoveryCatalog(recovery_resources().__getitem__)
    return presents


def unbuilt_status():
    pytest.fail("Readiness checks must not build a catalog status")


def test_cooking_readiness_never_builds_the_catalog_status(presents, monkeypatch):
    with_recovery(presents).vote_cooking_catalog = build_cooking()
    for catalog in (presents.vote_cooking_catalog, presents.recovery_catalog):
        monkeypatch.setattr(catalog, "status", unbuilt_status)
    reward = {"kind": "vote_cooking", "card_id": 1, "description": "手料理"}
    assert presents.preview([reward], 0) == [{"Type": 21, "Content": 1, "Description": "手料理"}]
    monkeypatch.setattr(presents.vote_cooking_catalog, "description", "changed")
    with pytest.raises(ProtocolError) as error:
        presents.preview([reward], 0)
    assert (error.value.code, error.value.status) == ("vote_cooking_unavailable", 503)


def test_recovery_receipt_readiness_never_builds_the_catalog_status(presents, store, monkeypatch):
    with_recovery(presents)
    ids = queue(presents, store, [{"kind": "charisma_recovery", "description": "回復"}])
    envelope = claim_request(store, ids)
    response = presents.receive(envelope, store)
    saved = store.profile()
    assert children(response)["PGP"] == game_param_response(saved.state)
    monkeypatch.setattr(presents.recovery_catalog, "status", unbuilt_status)
    assert presents.receive(envelope, store) == response and store.profile() == saved
    monkeypatch.setattr(presents.recovery_catalog, "is_ready", lambda: False)
    rejected(presents, store, envelope, "present_recovery_unavailable")


def with_tickets(presents):
    definitions = MappingProxyType({TICKET_ITEM: SimpleNamespace(item_id=TICKET_ITEM)})
    catalog = SimpleNamespace(definitions=definitions, item_ids=frozenset(definitions), _ready=lambda: None,
                              supports_item=definitions.__contains__, definition=definitions.__getitem__)
    presents.item_tickets = SimpleNamespace(catalog=catalog, _ready=lambda: None)
    presents.tickets = SimpleNamespace(pools=SimpleNamespace(get=lambda pool: pool))
    presents.selection_tickets = SimpleNamespace(catalog=SimpleNamespace(get=lambda config: config))
    return presents


def bundled(presents, state, kind, content=BUNDLED_TICKETS, **fields):
    # Stands in for imported mail holding several tickets, which no local writer creates.
    box, _ = presents.enqueue(state, [crystal(), crystal(5)])
    box["tables"]["presents"].insert(1, {"PresentID": BUNDLED_ID, "Type": kind, "Content": content,
                                         "Description": "束ねた券", **(fields or TICKET_FIELDS[kind])})
    box["last_present_id"] = BUNDLED_ID
    return box


def split_refused(presents, box, present_id, code, status):
    before = deepcopy(box)
    with pytest.raises(ProtocolError) as error:
        presents.split_ticket_mail(box, present_id)
    assert (error.value.code, error.value.status) == (code, status) and box == before


@pytest.mark.parametrize("kind", sorted(TICKET_FIELDS))
def test_split_ticket_mail_gives_every_ticket_its_own_mail_in_place(presents, state, kind):
    box = bundled(with_tickets(presents), state, kind)
    before = deepcopy(box)
    earlier, mail, later = box["tables"]["presents"]
    assert presents.bundled_tickets(box) == (BUNDLED_ID,)
    updated, pieces = presents.split_ticket_mail(box, BUNDLED_ID)
    assert box == before
    assert pieces == [{**mail, "PresentID": BUNDLED_ID + offset, "Content": 1} for offset in range(1, BUNDLED_TICKETS + 1)]
    assert updated["tables"]["presents"] == [earlier, *pieces, later]
    assert updated["last_present_id"] == BUNDLED_ID + BUNDLED_TICKETS
    unchanged = deepcopy(updated)
    unchanged["tables"]["presents"], unchanged["last_present_id"] = box["tables"]["presents"], BUNDLED_ID
    assert unchanged == box
    assert presents.rows(updated) == updated["tables"]["presents"] and presents.bundled_tickets(updated) == ()
    pieces[0]["Description"] = "changed"
    assert updated["tables"]["presents"][1]["Description"] == mail["Description"]


def test_only_the_split_reads_summon_mail_that_holds_several_draws(presents, state):
    box = bundled(with_tickets(presents), state, 16)
    for action in (lambda: presents.rows(box), lambda: presents.response(box)):
        with pytest.raises(ProtocolError) as error:
            action()
        assert (error.value.code, error.value.status) == ("present_state_invalid", 503)
    updated, _ = presents.split_ticket_mail(box, BUNDLED_ID)
    assert [(row["Type"], row["Content"]) for row in presents.rows(updated)] == [(0, 3), *[(16, 1)] * BUNDLED_TICKETS,
                                                                                  (0, 5)]


def test_a_split_that_would_overfill_the_mailbox_or_its_identities_is_refused_whole(presents, state):
    with_tickets(presents)
    fitting, _ = presents.split_ticket_mail(bundled(presents, state, 20, MAX_PRESENTS - 2), BUNDLED_ID)
    assert len(presents.rows(fitting)) == MAX_PRESENTS
    split_refused(presents, bundled(presents, state, 20, MAX_PRESENTS - 1), BUNDLED_ID, "present_capacity", 409)
    last = bundled(presents, state, 20)
    last["last_present_id"] = INT32_MAX - BUNDLED_TICKETS
    assert presents.split_ticket_mail(last, BUNDLED_ID)[1][-1]["PresentID"] == INT32_MAX
    last["last_present_id"] += 1
    split_refused(presents, last, BUNDLED_ID, "present_identity_exhausted", 409)


def test_mail_that_holds_one_ticket_or_no_ticket_is_not_split(presents, state):
    with_items(with_tickets(presents), PLAIN_ITEM)
    single, plain = bundled(presents, state, 16, 1), bundled(presents, state, 22, ItemID=PLAIN_ITEM)
    assert presents.bundled_tickets(single) == presents.bundled_tickets(plain) == ()
    for box, present_id in ((single, BUNDLED_ID), (bundled(presents, state, 20), 1), (plain, BUNDLED_ID)):
        split_refused(presents, box, present_id, SPLIT_UNNEEDED, 409)
    box = bundled(presents, state, 20)
    split_refused(presents, box, BUNDLED_ID + 1, "present_not_found", 409)
    split_refused(presents, state, 1, "present_not_found", 409)
    for present_id in (0, -1, True, INT32_MAX + 1):
        split_refused(presents, box, present_id, "present_selection_invalid", 400)


def test_readiness_is_the_absence_of_template_issues(presents):
    assert presents.is_ready() is True and presents.status()["ready"] is True
    presents.issues["present_templates"] = "Unverified templates"
    assert presents.is_ready() is False and presents.status()["ready"] is False
    with pytest.raises(ProtocolError) as error:
        presents.preview([crystal()], 0)
    assert error.value.code == "present_templates_unavailable"
