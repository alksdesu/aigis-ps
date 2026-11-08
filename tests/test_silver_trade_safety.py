from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from datetime import datetime, timezone
import logging
from threading import Barrier, Lock
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.card_status import CARD_STATUS_FIELDS
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import MAX_PLAYER_UNITS
from aigis_ps.protocol import ProtocolRegistry, encode_wire, prepare_request
from aigis_ps.silver_trade import SilverTradeCatalog
from aigis_ps.storage import Store
from aigis_ps.trade_grants import TradeUnitGrants
from aigis_ps.trade_history import TradeRules
from aigis_ps.trade_service import TradeService
from test_quest_service import bundle, request


PAYMENT = "trade_item_001000"


@pytest.fixture(scope="module")
def silver_catalog(bundle):
    return SilverTradeCatalog(bundle.trades.catalog, bundle.gifts.catalog,
                              lambda name: bundle.native_cdn.assets.read(name)[0])


@pytest.fixture
def case(bundle, silver_catalog, tmp_path):
    bootstrap, players, gifts = copy(bundle.bootstrap), copy(bundle.player_state), copy(bundle.gifts)
    gifts.players = players
    rewards = copy(bundle.evolution_rewards)
    rewards.players = players
    catalog = silver_catalog.base
    clock = SimpleNamespace(now=datetime(2026, 11, 1, 12, tzinfo=timezone.utc))
    grants = TradeUnitGrants(players)
    service = TradeService(bootstrap, players, bundle.possessions, catalog, grants=grants,
        rules=TradeRules("never", "never"), clock=lambda: clock.now, silver=silver_catalog, gifts=gifts,
        evolution_rewards=rewards)
    bootstrap.exchange_history_provider = service.history_response
    saved = players.initialize(bootstrap.initial_state())
    saved, units = players.credit_units(saved, [{"card_id": 8, "level": 1, "count": 1}])
    assert (units[0]["GetIndex"], units[0]["UniqueID"]) == (1, 2)
    saved = bundle.possessions.credit(saved, {PAYMENT: 40})
    required = set(silver_catalog.product(29).disclosure_quest_ids)
    records = {row["QuestID"]: row for row in saved["tables"]["quest_records"]}
    for identifier in required:
        records[identifier] = {"QuestID": identifier, "MaxRank": 3, "AchieveFlag": 0x01000000,
                               "RemainAssault": 0, "FirstDefeatDate": 0, "Shown": True}
    saved["tables"]["quest_records"] = list(records.values())
    saved["unrelated_progress"] = {"keep": [1, 2]}
    return SimpleNamespace(service=service, players=players, gifts=gifts, grants=grants, clock=clock,
                           store=Store(tmp_path / "silver-safety.sqlite3", service.initialize(saved)))


def base(index=1, identity=2):
    return Container("BASE", (Column("A7", "I", (index,)), Column("AB", "I", (identity,))))


def message(case, *, quantity=1, tick=1000, index=1, identity=2, operation=106, columns=None):
    if columns is None:
        columns = (Column("SHOP", "I", (29,)), Column("NUM", "I", (quantity,)), base(index, identity))
    return request(case.store, operation, columns, tick=tick)


def change(case, action):
    profile = case.store.profile()
    saved = deepcopy(profile.state)
    action(saved)
    case.store.update_state(saved, profile.revision)


def balance(state):
    return next(row["Count"] for row in state["tables"]["possessions"] if row["ItemID"] == PAYMENT)


def failure(case, envelope, code):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as error:
        case.service.purchase(envelope, case.store)
    assert error.value.code == code
    assert case.store.profile() == before


def rewrite(envelope, edit):
    tree = ET.fromstring(envelope.xml)
    for name in ("VN", "ST", "HS"):
        tree.remove(tree.find(name))
    edit(tree)
    xml = prepare_request(ET.tostring(tree, encoding="utf-8", short_empty_elements=False), envelope.profile.st)
    return ProtocolRegistry.load().decode(envelope.profile.route, encode_wire(xml, envelope.profile.request_key))


@pytest.mark.parametrize("node", [
    None, Column("BASE", "I", (1,)), Container("BASE"),
    Container("BASE", (Column("A7", "I", (1,)),)),
    Container("BASE", (Column("AB", "I", (2,)),)),
    Container("BASE", (Column("A7", "I", ()), Column("AB", "I", (2,)))),
    Container("BASE", (Column("A7", "I", (1, 1)), Column("AB", "I", (2,)))),
    Container("BASE", (Column("A7", "I", (1,)), Column("AB", "I", (2, 2)))),
    Container("BASE", (Column("A7", "S", ("1",)), Column("AB", "I", (2,)))),
    Container("BASE", (Column("A7", "I", (1,)), Column("AB", "B", (True,)))),
    Container("BASE", (Column("A7", "I", (1,)), Column("AB", "I", (2,)), Column("A1", "I", (8,)))),
], ids=["missing", "scalar", "empty", "missing-uid", "missing-index", "empty-index", "vector-index",
        "vector-uid", "string-index", "bool-uid", "extra-card"])
def test_base_requires_exact_scalar_identities(case, node):
    fields = (Column("SHOP", "I", (29,)), Column("NUM", "I", (1,)))
    failure(case, message(case, columns=fields + (() if node is None else (node,))), "invalid_request_fields")


@pytest.mark.parametrize("kind", ["root-field", "base-text", "base-tail", "base-attribute", "duplicate-index"])
def test_unknown_duplicate_and_mixed_fields_are_rejected_before_mutation(case, kind):
    def edit(tree):
        node = tree.find("BASE")
        if kind == "root-field":
            tree.append(ET.fromstring('<PRICE T="I"><V>0</V></PRICE>'))
        elif kind == "base-text":
            node.text = "unexpected"
        elif kind == "base-tail":
            node.tail = "unexpected"
        elif kind == "base-attribute":
            node.set("T", "I")
        else:
            node.append(deepcopy(node.find("A7")))
    failure(case, rewrite(message(case), edit), "invalid_request_fields")


@pytest.mark.parametrize("operation", [104, 105, 107, 109, 110, 111, 112, 113])
def test_ordinary_trade_routes_do_not_accept_silver_base(case, operation):
    failure(case, message(case, operation=operation), "invalid_request_fields")


@pytest.mark.parametrize("quantity,code", [(0, "invalid_request_fields"), (2, "silver_trade_quantity_invalid")])
def test_silver_quantity_is_exactly_one(case, quantity, code):
    failure(case, message(case, quantity=quantity), code)


@pytest.mark.parametrize("index,identity,code", [(-1, 2, "invalid_request_fields"),
    (1, 0, "invalid_request_fields"), (1, -1, "invalid_request_fields"),
    (2, 2, "silver_trade_material_missing"), (1, 3, "silver_trade_material_missing")])
def test_material_identity_cannot_be_forged(case, index, identity, code):
    failure(case, message(case, index=index, identity=identity), code)


def test_nineteen_currency_cannot_pay_twenty(case):
    change(case, lambda saved: saved["tables"]["possessions"][0].update(Count=19))
    failure(case, message(case), "trade_currency_insufficient")


def test_exact_twenty_replaces_only_material_and_preserves_other_state(case):
    change(case, lambda saved: saved["tables"]["possessions"][0].update(Count=20))
    before = case.store.profile()
    response = {node.name: node for node in loads(case.service.purchase(message(case), case.store)).children}
    after = case.store.profile()
    assert balance(after.state) == 0 and after.revision == before.revision + 1
    units = after.state["player_state"]["units"]
    assert units[0] == before.state["player_state"]["units"][0]
    assert [(unit["CardID"], unit["GetIndex"], unit["UniqueID"]) for unit in units[1:]] == [(1122, 2, 3)]
    assert after.state["unrelated_progress"] == before.state["unrelated_progress"]
    assert after.state["tables"]["game_param"] == before.state["tables"]["game_param"]
    assert response["POSSESSION"].children == (Column("ItemID", "S", (PAYMENT,)), Column("Count", "I", (0,)),
                                               Column("Enabled", "I", (0,)))
    assert after.state["trade_history"]["products"]["29"]["lifetime_count"] == 1


@pytest.mark.parametrize("flags,code", [(3, "silver_trade_material_protected"),
    (17, "silver_trade_storage_unsupported"), (33, "silver_trade_storage_unsupported"),
    (-2147483631, "silver_trade_storage_unsupported")])
def test_locked_and_actual_extra_barracks_materials_are_not_consumed(case, flags, code):
    change(case, lambda saved: saved["player_state"]["units"][1].update(GeneralPurposeFlags=flags))
    failure(case, message(case), code)


@pytest.mark.parametrize("group", [0, 1, 7, 8, 64, 127, 254])
def test_every_squad_group_protects_material(case, group):
    address = group << 24
    if address > 0x7FFFFFFF:
        address -= 0x100000000
    change(case, lambda saved: saved["player_state"]["squads"].append(
        {"Address": address, "UnitIndex": 1, "Name": "", "Flags": 0}))
    failure(case, message(case), "silver_trade_material_protected")


@pytest.mark.parametrize("card,index,identity", [(1, 0, 1), (309, 1, 2)])
def test_prince_variants_cannot_be_material(case, card, index, identity):
    if card == 309:
        change(case, lambda saved: saved["player_state"]["units"][1].update(CardID=309))
    failure(case, message(case, index=index, identity=identity), "silver_trade_material_protected")


def test_refusal_keeps_the_material_and_is_logged_because_the_client_resends_it(case, caplog):
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        failure(case, message(case, index=0, identity=1), "silver_trade_material_protected")
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        "Trade registered:106 refused with silver_trade_material_protected; the native client shows a "
        "communication error and resends the same request until the server accepts it"]


@pytest.mark.parametrize("field", ["UX", "last_unique_id"])
def test_saved_high_watermarks_survive_material_deletion(case, field):
    def high(saved):
        player = saved["player_state"]
        (player["battle"] if field == "UX" else player)[field] = 5000
    change(case, high)
    case.service.purchase(message(case), case.store)
    player = case.store.profile().state["player_state"]
    assert player["units"][-1]["GetIndex" if field == "UX" else "UniqueID"] == 5001
    assert (player["battle"] if field == "UX" else player)[field] == 5001


def test_removed_highest_uid_is_not_reused_when_old_counter_is_absent(case):
    def legacy(saved):
        saved["player_state"].pop("last_unique_id", None)
        saved["player_state"]["units"][1]["UniqueID"] = 5000
    change(case, legacy)
    case.service.purchase(message(case, identity=5000), case.store)
    player = case.store.profile().state["player_state"]
    assert player["units"][-1]["UniqueID"] == player["last_unique_id"] == 5001


def test_removed_int32_max_uid_cannot_be_recycled(case):
    def exhausted(saved):
        saved["player_state"].pop("last_unique_id", None)
        saved["player_state"]["units"][1]["UniqueID"] = 0x7FFFFFFF
    change(case, exhausted)
    failure(case, message(case, identity=0x7FFFFFFF), "unit_roster_capacity")


def test_consumed_card_keeps_legacy_gift_and_awakening_progress(case):
    def history(saved):
        saved["card_status_schema"] = 1
        saved["tables"]["card_status"] = [{**dict.fromkeys(CARD_STATUS_FIELDS, 0), "CardID": 8, "A7": 3, "B3": 1}]
        saved.pop("gift_state", None)
        saved.pop("evolution_rewards", None)
    change(case, history)
    before = case.store.profile()
    case.service.purchase(message(case), case.store)
    after = case.store.profile().state
    assert after["gift_state"]["claims"]["8"] == [1, 100]
    assert 8 in after["evolution_rewards"]["claimed"]
    assert after["tables"]["card_status"] == before.state["tables"]["card_status"]
    assert after["tables"]["presents"] == before.state["tables"]["presents"]


def test_same_consumed_identity_with_new_ct_cannot_buy_again(case):
    case.service.purchase(message(case), case.store)
    failure(case, message(case, tick=1001), "silver_trade_material_missing")
    after = case.store.profile().state
    assert balance(after) == 20 and after["trade_history"]["products"]["29"]["lifetime_count"] == 1


def test_grant_failure_after_candidate_changes_does_not_consume_payment_or_material(case, monkeypatch):
    def broken(saved, product, count):
        saved["player_state"]["units"].clear()
        saved["tables"]["possessions"].clear()
        raise ProtocolError("fixture_grant", "Grant failed", 503)
    monkeypatch.setattr(case.grants, "grant", broken)
    failure(case, message(case), "fixture_grant")


@pytest.mark.parametrize("operation,occurrence", [(41, 1), (41, 2), (137, 1), (106, 1)])
def test_peak_final_configuration_and_current_responses_preflight_before_commit(case, monkeypatch, operation, occurrence):
    import aigis_ps.trade_service as module

    original, seen = module.encode_response, []
    def reject(profile, xml):
        if profile.registration_keys == (operation,):
            seen.append(1)
            if len(seen) == occurrence:
                raise ProtocolError("fixture_preflight", "Response cannot fit", 503)
        return original(profile, xml)
    monkeypatch.setattr(module, "encode_response", reject)
    failure(case, message(case), "fixture_preflight")
    assert len(seen) == occurrence


def test_response_build_failure_leaves_entire_transaction_unchanged(case, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("Response cannot be serialized")
    monkeypatch.setattr(case.service, "_response", broken)
    failure(case, message(case), "trade_response_invalid")


def test_no_initial_primary_slot_blocks_net_zero_exchange(case):
    case.players.capacity_provider = lambda saved: 0
    failure(case, message(case), "unit_roster_capacity")


def test_cas_retry_keeps_concurrent_progress_and_charges_once(case, monkeypatch):
    original, calls = case.store.update_state, []
    def update(saved, revision):
        calls.append(revision)
        if len(calls) == 1:
            current = case.store.profile()
            changed = deepcopy(current.state)
            changed["concurrent_progress"] = True
            original(changed, current.revision)
            raise ValueError("Concurrent update")
        return original(saved, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    case.service.purchase(message(case), case.store)
    after = case.store.profile().state
    assert len(calls) == 2 and after["concurrent_progress"] is True
    assert balance(after) == 20
    assert [unit["CardID"] for unit in after["player_state"]["units"]] == [1, 1122]
    assert len(after["trade_receipts"]["receipts"]) == 1


def test_cas_retry_rechecks_material_lock_before_consumption(case, monkeypatch):
    original, concurrent = case.store.update_state, []
    def update(saved, revision):
        if not concurrent:
            current = case.store.profile()
            changed = deepcopy(current.state)
            changed["player_state"]["units"][1]["GeneralPurposeFlags"] |= 2
            concurrent.append(original(changed, current.revision))
            raise ValueError("Material locked concurrently")
        return original(saved, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    with pytest.raises(ProtocolError) as error:
        case.service.purchase(message(case), case.store)
    assert error.value.code == "silver_trade_material_protected"
    assert case.store.profile() == concurrent[0]


def test_three_cas_conflicts_do_not_persist_candidate_consumption(case, monkeypatch):
    calls = []
    def conflict(saved, revision):
        calls.append(revision)
        raise ValueError("Concurrent update")
    monkeypatch.setattr(case.store, "update_state", conflict)
    failure(case, message(case), "profile_changed")
    assert len(calls) == 3


def test_concurrent_exact_request_commits_only_one_exchange(case, monkeypatch):
    original, gate, lock = case.store.update_state, Barrier(2), Lock()
    calls = []
    def update(saved, revision):
        with lock:
            calls.append(revision)
            wait = len(calls) <= 2
        if wait:
            gate.wait(timeout=10)
        return original(saved, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    before = case.store.profile()
    envelope = message(case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: case.service.purchase(envelope, case.store), range(2)))
    after = case.store.profile()
    assert outcomes[0] == outcomes[1] and after.revision == before.revision + 1
    assert balance(after.state) == 20
    assert len(after.state["player_state"]["units"]) == 2
    assert len(after.state["trade_receipts"]["receipts"]) == 1


def full_roster(case, count, reservations=0):
    case.players.capacity_provider = lambda saved: 0x7FFFFFFF
    def fill(saved):
        player = saved["player_state"]
        template = player["units"][1]
        player["units"] = [player["units"][0], *[dict(template, GetIndex=index, UniqueID=index + 1)
                            for index in range(1, count)]]
        player["battle"]["UX"] = count - 1
        player["last_unique_id"] = count
        if reservations:
            saved["quest_state"] = {"active": {"unit_reservations": [
                {"card_id": 3, "level": 1, "count": reservations}]}}
    change(case, fill)


def test_native_transient_peak_cannot_exceed_4096(case):
    full_roster(case, MAX_PLAYER_UNITS)
    failure(case, message(case), "trade_unit_peak_capacity")


def test_native_peak_and_final_reserved_roster_are_checked_independently(case):
    full_roster(case, MAX_PLAYER_UNITS - 1, reservations=1)
    before = case.store.profile()
    case.service.purchase(message(case), case.store)
    after = case.store.profile().state
    assert len(after["player_state"]["units"]) == MAX_PLAYER_UNITS - 1
    assert after["quest_state"] == before.state["quest_state"]
    assert sum(unit["CardID"] == 1122 for unit in after["player_state"]["units"]) == 1
    assert all(unit["CardID"] != 3 for unit in after["player_state"]["units"])
    assert balance(after) == 20


def test_final_reserved_roster_cannot_exceed_4096_even_when_peak_fits(case):
    full_roster(case, MAX_PLAYER_UNITS - 1, reservations=2)
    failure(case, message(case), "unit_roster_capacity")
