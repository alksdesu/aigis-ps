from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import struct
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import lz4.frame
import pytest

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.bootstrap import BootstrapService
from aigis_ps.errors import ProtocolError
from aigis_ps.guide_service import ELIGIBILITY_COMMAND, ELIGIBILITY_EXPRESSION, GuideService
from aigis_ps.storage import Store
from test_bootstrap import asset_root
from test_image_aliases import command_id, command_table, encode
from test_player_state import assets, master, row, service
from test_quest_service import bundle
from test_unit_actions import request


@pytest.fixture
def resources():
    return {"OnCommandMenuEvent.atb": command_table([(command_id(ELIGIBILITY_COMMAND), encode(ELIGIBILITY_EXPRESSION))]),
            "OnCommand.atb": command_table([])}


@pytest.fixture
def cards():
    rows = {1: {"CardID": 1, "BonusType": 0}, 2: {"CardID": 2, "BonusType": 1}}
    return SimpleNamespace(card_ids=(1, 2), card=lambda key: rows[key],
                           snapshot=SimpleNamespace(identifier="fixed-master", client_hash="a" * 64))


@pytest.fixture
def guide(asset_root, service, cards, resources):
    bootstrap = BootstrapService(asset_root, clock=lambda: datetime(2026, 11, 1, 12), asset_version="a" * 40)
    return GuideService(bootstrap, service, cards, resources.__getitem__)


@pytest.fixture
def store(guide, tmp_path):
    state = guide.players.initialize(guide.bootstrap.initial_state())
    unit = deepcopy(state["player_state"]["units"][0])
    unit.update(CardID=2, ClassID=20, SettingSkillID=3, GetIndex=1, UniqueID=2, Love=100)
    state["player_state"]["units"].append(unit)
    state["player_state"]["battle"]["UX"] = 1
    return Store(tmp_path / "guide.sqlite3", state)


def select(store, index=1, number=121):
    return request(store, number, (Column("A7", "I", (index,)),))


def assert_no_write(store, callback, code):
    before = store.profile()
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert caught.value.code == code
    assert store.profile() == before
    return caught.value


def test_select_changes_only_existing_configuration_and_is_idempotent(guide, store):
    before = store.profile()
    envelope = select(store)
    response = guide.save(envelope, store)
    after = store.profile()
    expected = deepcopy(before.state)
    expected["tables"]["config"][0]["_GuideUnitIndex"] = 1
    assert after.state == expected
    assert after.revision == before.revision + 1
    document = loads(response)
    assert tuple(group.name for group in document.children) == ("SYSTEM", "PGC")
    assert document.children[1] == Container("PGC", (Column("A1", "S", ("C1",)), Column("A2", "S", ("1",))))
    assert guide.save(envelope, store) == response
    assert store.profile() == after


def test_zero_clears_and_never_selects_prince_index_zero(guide, store):
    initial = store.profile()
    guide.save(select(store, 0), store)
    assert store.profile() == initial
    guide.save(select(store), store)
    guide.save(select(store, 0), store)
    assert store.profile().state == initial.state


@pytest.mark.parametrize("love,accepted", [(0, False), (99, False), (100, True), (150, True)])
def test_trust_threshold(guide, store, love, accepted):
    current = store.profile()
    current.state["player_state"]["units"][1]["Love"] = love
    store.update_state(current.state, current.revision)
    if accepted:
        guide.save(select(store), store)
        assert store.profile().state["tables"]["config"][0]["_GuideUnitIndex"] == 1
    else:
        assert_no_write(store, lambda: guide.save(select(store), store), "guide_ineligible")


def test_original_card_bonus_is_required_even_with_artificial_trust(guide, store, cards, resources):
    cards.card(2)["BonusType"] = 0
    denied = GuideService(guide.bootstrap, guide.players, cards, resources.__getitem__)
    assert_no_write(store, lambda: denied.save(select(store), store), "guide_ineligible")


@pytest.mark.parametrize("flags,organized", [(2, False), (0x23, False), (0x3, True)])
def test_locks_other_barracks_and_squad_membership_do_not_add_restrictions(guide, store, flags, organized):
    current = store.profile()
    player = current.state["player_state"]
    player["units"][1]["GeneralPurposeFlags"] = flags
    if organized:
        player["squads"] = [row(unit=1)]
    store.update_state(current.state, current.revision)
    guide.save(select(store), store)
    assert store.profile().state["tables"]["config"][0]["_GuideUnitIndex"] == 1


@pytest.mark.parametrize("index,code", [(-1, "invalid_request_fields"), (2, "guide_not_owned"), (2147483647, "guide_not_owned")])
def test_rejects_invalid_or_unknown_instance(guide, store, index, code):
    assert_no_write(store, lambda: guide.save(select(store, index), store), code)


def test_existing_missing_guide_is_preserved_until_explicit_selection(guide, store):
    current = store.profile()
    current.state["tables"]["config"][0]["_GuideUnitIndex"] = 4321
    store.update_state(current.state, current.revision)
    before = store.profile()
    response = guide.bootstrap.config(request(store, 36, ()), store)
    assert next(column.values for column in loads(response).children if column.name == "C1") == (4321,)
    assert store.profile() == before
    guide.save(select(store, 0), store)
    assert store.profile().state["tables"]["config"][0]["_GuideUnitIndex"] == 0


def test_selected_instance_consumption_does_not_retarget_saved_index(guide, store):
    guide.save(select(store), store)
    current = store.profile()
    player = current.state["player_state"]
    player["units"] = player["units"][:1]
    store.update_state(current.state, current.revision)
    assert_no_write(store, lambda: guide.save(select(store), store), "guide_not_owned")
    assert store.profile().state["tables"]["config"][0]["_GuideUnitIndex"] == 1


def test_restart_restores_selection_through_full_config(guide, store):
    guide.save(select(store), store)
    reopened = Store(store.path, guide.bootstrap.initial_state())
    response = guide.bootstrap.config(request(reopened, 36, ()), reopened)
    assert next(column.values for column in loads(response).children if column.name == "C1") == (1,)
    assert reopened.profile().state == store.profile().state


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_type", "extra_value", "unknown_field", "nested",
                                     "mixed", "root_type", "overflow", "missing_ct", "bad_header"])
def test_strict_native_request_shape(guide, store, mutation):
    envelope = select(store)
    document = envelope.document
    node = document.find("A7")
    if mutation == "missing":
        document.remove(node)
    elif mutation == "duplicate":
        document.append(deepcopy(node))
    elif mutation == "wrong_type":
        node.set("T", "S")
    elif mutation == "extra_value":
        ET.SubElement(node, "V").text = "1"
    elif mutation == "unknown_field":
        ET.SubElement(document, "AB", T="I")
    elif mutation == "nested":
        ET.SubElement(node[0], "Child")
    elif mutation == "mixed":
        node.tail = "unexpected"
    elif mutation == "root_type":
        document.set("T", "S")
    elif mutation == "overflow":
        node[0].text = "2147483648"
    elif mutation == "missing_ct":
        document.remove(document.find("CT"))
    else:
        document.find("UI").set("T", "I")
    assert_no_write(store, lambda: guide.save(envelope, store), "invalid_request_fields")


@pytest.mark.parametrize("name,code", [("UI", "invalid_identity"), ("UK", "invalid_identity"), ("MK", "invalid_method_key")])
def test_authenticates_before_writing(guide, store, name, code):
    envelope = select(store)
    envelope.document.find(name + "/V").text = "different-account"
    assert assert_no_write(store, lambda: guide.save(envelope, store), code).status == 403


def test_wrong_operation_does_not_dispatch(guide, store):
    assert_no_write(store, lambda: guide.save(select(store, number=119), store), "invalid_request_fields")


@pytest.mark.parametrize("full_load", [False, True])
def test_capacity_failure_has_no_partial_write(guide, store, full_load):
    envelope = select(store)
    if full_load:
        guide._config_profile = replace(guide._config_profile, response_limit=1)
    else:
        envelope = replace(envelope, profile=replace(envelope.profile, response_limit=1))
    assert_no_write(store, lambda: guide.save(envelope, store), "response_too_large")


def test_cas_retries_preserve_concurrent_changes(guide, store, monkeypatch):
    original = store.update_state
    calls = 0

    def update(state, revision):
        nonlocal calls
        calls += 1
        if calls == 1:
            concurrent = store.profile()
            concurrent.state["tables"]["config"][0]["MenuTutorialFlags"] = 0x101
            original(concurrent.state, concurrent.revision)
        return original(state, revision)

    monkeypatch.setattr(store, "update_state", update)
    guide.save(select(store), store)
    assert calls == 2
    row = store.profile().state["tables"]["config"][0]
    assert row["MenuTutorialFlags"] == 0x101 and row["_GuideUnitIndex"] == 1


def test_cas_rechecks_current_eligibility(guide, store, monkeypatch):
    original = store.update_state
    calls = 0

    def update(state, revision):
        nonlocal calls
        calls += 1
        concurrent = store.profile()
        concurrent.state["player_state"]["units"][1]["Love"] = 99
        original(concurrent.state, concurrent.revision)
        return original(state, revision)

    monkeypatch.setattr(store, "update_state", update)
    with pytest.raises(ProtocolError, match="100 or more"):
        guide.save(select(store), store)
    assert calls == 1
    assert store.profile().state["tables"]["config"][0]["_GuideUnitIndex"] == 0


def test_persistent_contention_is_bounded(guide, store, monkeypatch):
    calls = 0

    def conflict(*_):
        nonlocal calls
        calls += 1
        raise ValueError("conflict")

    monkeypatch.setattr(store, "update_state", conflict)
    assert_no_write(store, lambda: guide.save(select(store), store), "profile_changed")
    assert calls == 3


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "changed", "extra_action", "split_token", "invalid_hex", "schema", "truncated"])
def test_unverified_eligibility_resources_are_rejected(guide, cards, resources, mutation):
    rows = [(command_id(ELIGIBILITY_COMMAND), encode(ELIGIBILITY_EXPRESSION))]
    if mutation == "missing":
        rows = []
    elif mutation == "duplicate":
        rows *= 2
    elif mutation == "changed":
        rows[0] = (rows[0][0], encode(ELIGIBILITY_EXPRESSION.replace("100", "99")))
    elif mutation == "extra_action":
        rows[0] = (rows[0][0], encode(ELIGIBILITY_EXPRESSION + "Grant();"))
    elif mutation == "split_token":
        rows[0] = (rows[0][0], encode(ELIGIBILITY_EXPRESSION.replace("GetLoyalty", "Get Loyalty")))
    elif mutation == "invalid_hex":
        rows[0] = (rows[0][0], "xx")
    resources["OnCommandMenuEvent.atb"] = command_table(rows, command_field="Wrong" if mutation == "schema" else "_Cmnd")
    if mutation == "truncated":
        resources["OnCommandMenuEvent.atb"] = resources["OnCommandMenuEvent.atb"][:-1]
    with pytest.raises(ProtocolError) as error:
        GuideService(guide.bootstrap, guide.players, cards, resources.__getitem__)
    assert error.value.code == "guide_catalog_invalid"


def test_all4_whitespace_and_resource_snapshot(guide, cards, resources):
    expression = " \n(IsHaveTrustParam() == 1) && ( GetLoyalty() >= 100 );\t"
    raw = command_table([(command_id(ELIGIBILITY_COMMAND), encode(expression).upper())])
    resources["OnCommandMenuEvent.atb"] = struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)
    selected = GuideService(guide.bootstrap, guide.players, cards, resources.__getitem__)
    assert selected.status()["resource_sha256"]["OnCommandMenuEvent.atb"] == hashlib.sha256(resources["OnCommandMenuEvent.atb"]).hexdigest()
    assert selected.content_version != guide.content_version
    status = selected.status()
    status["resource_sha256"].clear()
    assert selected.status()["resource_sha256"]


@pytest.mark.parametrize("text,denied", [("", False), (" \n", False), ('IsRating("R18");', True)])
def test_unverified_rating_rule_is_scoped_to_card(guide, store, cards, resources, text, denied):
    resources["OnCommand.atb"] = command_table([(command_id("IsCanUseRating_2"), encode(text))])
    selected = GuideService(guide.bootstrap, guide.players, cards, resources.__getitem__)
    if denied:
        assert_no_write(store, lambda: selected.save(select(store), store), "guide_rating_unsupported")
        selected.save(select(store, 0), store)
    else:
        selected.save(select(store), store)


def test_catalog_bonus_is_frozen(guide, cards, store):
    cards.card(2)["BonusType"] = 0
    guide.save(select(store), store)
    assert store.profile().state["tables"]["config"][0]["_GuideUnitIndex"] == 1


def test_current_resources_original_alias_card_and_restart(bundle, tmp_path):
    reader = lambda name: bundle.native_cdn.assets.read(name)[0]
    selected = GuideService(bundle.bootstrap, bundle.player_state, bundle.gifts.catalog, reader)
    assert selected.status()["rating_conditions_unavailable"] == []
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state, units = bundle.player_state.credit_units(state, [{"card_id": 2700, "level": 1, "count": 1}])
    state["player_state"]["units"][-1]["Love"] = 100
    target = units[0]["GetIndex"]
    store = Store(tmp_path / "real-guide.sqlite3", state)
    selected.save(select(store, target), store)
    assert store.profile().state["player_state"]["units"][-1]["CardID"] == 2700
    reopened = Store(store.path, state)
    response = selected.bootstrap.config(request(reopened, 36, ()), reopened)
    assert next(column.values for column in loads(response).children if column.name == "C1") == (target,)
    assert selected.handlers() == {121: selected.save}
