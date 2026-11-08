from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import logging
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.errors import ProtocolError
from aigis_ps.request_ledger import HISTORY_LIMIT, SCHEMA_VERSION
from aigis_ps.storage import Store
from aigis_ps.training_catalog import TrainingCatalog
from aigis_ps.training_service import GROWTH_FIELDS, TrainingService
from test_quest_service import bundle, request


class FixedQuote:
    content_version = "a" * 64
    price = 400

    def quote(self, player, config, message, target, materials, *, state=None):
        return SimpleNamespace(price=self.price, values={"Exp": 1000, "SkillLevel": 2,
            "CostModMixValue": -1, "SkillExperience": 1, "CostExperience": 1})

    def replayable_content_versions(self):
        return frozenset((self.content_version,))

    def status(self):
        return {"ready": True}


@pytest.fixture(scope="module")
def unfarmed(bundle):
    growth = bundle.training.catalog
    assert growth.fairy_farm is not None
    return TrainingCatalog(growth.cards, bundle.player_state, lambda name: bundle.native_cdn.assets.read(name)[0],
                           growth.aliases, rules=growth.rules, silver_house=growth.silver_house)


@pytest.fixture
def case(bundle, tmp_path):
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    initial, units = bundle.player_state.credit_units(initial, [
        {"card_id": 3, "level": 1, "count": 1}, {"card_id": 2, "level": 1, "count": 2}])
    store = Store(tmp_path / "training.sqlite3", initial)
    catalog = FixedQuote()
    service = TrainingService(bundle.bootstrap, bundle.player_state, catalog, bundle.gifts)
    return SimpleNamespace(store=store, service=service, catalog=catalog, units=units, bundle=bundle)


def columns(units):
    return tuple(Column(name, "I", tuple(value(unit) for unit in units)) for name, value in (
        ("STORAGETYPE", lambda _: 0), ("STORAGEINDEX", lambda _: 0),
        ("INVITEID", lambda unit: unit["GetIndex"]), ("UNIQUEID", lambda unit: unit["UniqueID"]),
        ("CARDID", lambda unit: unit["CardID"]), ("A7", lambda unit: unit["GetIndex"]),
        ("AB", lambda unit: unit["UniqueID"])))


def message(case, *, tick=1000, base=None, materials=None):
    return request(case.store, 5, (
        Container("BASE", columns((base or case.units[0],))),
        Container("APPEND", columns(materials if materials is not None else case.units[1:])),
        Container("CMB", (Column("A1", "I", (0,)), Column("A7", "I", (0,)), Column("B0", "I", (0,))))), tick=tick)


def mutate(case, action):
    current = case.store.profile()
    state = deepcopy(current.state)
    action(state)
    case.store.update_state(state, current.revision)


def rewrite(case, action):
    state = deepcopy(case.store.profile().state)
    action(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state, ensure_ascii=True),))


def credit(case, count):
    current = case.store.profile()
    state, added = case.bundle.player_state.credit_units(current.state, [{"card_id": 2, "level": 1, "count": count}])
    case.store.update_state(state, current.revision)
    return added


def fails(case, envelope, code):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.integrate(envelope, case.store)
    assert caught.value.code == code
    assert case.store.profile() == before


def test_transaction_updates_all_growth_fields_and_preserves_other_domains(case):
    before = case.store.profile()
    response = case.service.integrate(message(case), case.store)
    after = case.store.profile()
    assert after.revision == before.revision + 1
    assert after.state["tables"]["game_param"][0]["Money"] == 600
    assert [unit["GetIndex"] for unit in after.state["player_state"]["units"]] == [0, 1]
    target = after.state["player_state"]["units"][1]
    assert {name: target[name] for name in GROWTH_FIELDS} == case.catalog.quote(None, None, None, None, None).values
    assert after.state["player_state"]["battle"] == before.state["player_state"]["battle"]
    assert after.state["player_state"]["last_unique_id"] == 4
    for table, rows in before.state["tables"].items():
        if table != "game_param":
            assert after.state["tables"][table] == rows
    data = {child.name: child for child in loads(response).children}
    assert set(data) == {"SYSTEM", "PGP", "PPU"}
    assert data["PGP"].children[0].values == ("A1",)
    assert data["PGP"].children[1].values == ("600",)
    assert {column.name: column.values for column in data["PPU"].children} == {
        "A7": (1,), "A4": (1000,), "A6": (2,), "AA": (-1,), "B2": (1,), "B3": (1,)}


def test_deleted_highest_identity_is_never_reused_for_old_saves(case):
    mutate(case, lambda state: state["player_state"].pop("last_unique_id"))
    case.service.integrate(message(case), case.store)
    _, units = case.bundle.player_state.credit_units(case.store.profile().state,
                                                    [{"card_id": 2, "level": 1, "count": 1}])
    assert units[0]["UniqueID"] == 5 and units[0]["GetIndex"] == 4


def test_retry_and_store_reopen_return_identical_response_without_new_debit(case):
    envelope = message(case)
    first = case.service.integrate(envelope, case.store)
    before = case.store.profile()
    assert case.service.integrate(envelope, case.store) == first
    reopened = Store(case.store.path)
    service = TrainingService(case.bundle.bootstrap, case.bundle.player_state, case.catalog, case.bundle.gifts)
    assert service.integrate(envelope, reopened) == first
    assert reopened.profile() == before


def test_new_request_cannot_consume_old_materials_again(case):
    case.service.integrate(message(case), case.store)
    fails(case, message(case, tick=1001), "training_unit_missing")


def test_new_content_or_revision_rejects_stale_success_response(case):
    envelope = message(case)
    case.service.integrate(envelope, case.store)
    case.catalog.content_version = "b" * 64
    fails(case, envelope, "training_retry_stale")
    case.catalog.content_version = "a" * 64
    mutate(case, lambda state: state["tables"]["game_param"][0].update(Money=800))
    fails(case, envelope, "training_retry_stale")


def test_receipt_replays_after_barracks_rules_attach(case):
    envelope = message(case)
    response = case.service.integrate(envelope, case.store)
    saved = case.store.profile()
    assert saved.state["training_history"]["receipts"][-1]["content"] == case.catalog.content_version
    case.service.barracks = case.bundle.unit_capacity
    assert case.service.integrate(envelope, case.store) == response
    assert case.store.profile() == saved


def test_attached_barracks_digest_pairs_the_catalog_with_the_capacity(case):
    case.service.barracks = case.bundle.unit_capacity
    case.service.integrate(message(case), case.store)
    receipt = case.store.profile().state["training_history"]["receipts"][-1]
    capacity = case.bundle.unit_capacity.content_version
    assert receipt["content"] == hashlib.sha256(f"{case.catalog.content_version}:{capacity}".encode()).hexdigest()


def real_case(bundle, tmp_path, service):
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    state, units = bundle.player_state.credit_units(state, [{"card_id": 3, "level": 1, "count": 3}])
    state["tables"]["game_param"][0]["Money"] = 100000
    return SimpleNamespace(store=Store(tmp_path / "real.sqlite3", state), units=units, service=service)


@pytest.mark.parametrize("barracks", [True, False], ids=["with_barracks", "without_barracks"])
def test_receipt_replays_after_the_catalog_fairy_farm_attaches(bundle, unfarmed, tmp_path, barracks):
    current = bundle.training
    assert unfarmed.content_version in current.catalog.replayable_content_versions() - {current.catalog.content_version}
    earlier = TrainingService(bundle.bootstrap, bundle.player_state, unfarmed, bundle.gifts,
                              barracks=current.barracks if barracks else None)
    saved = real_case(bundle, tmp_path, earlier)
    envelope = message(saved)
    response = earlier.integrate(envelope, saved.store)
    committed = saved.store.profile()
    assert current.integrate(envelope, saved.store) == response
    assert saved.store.profile() == committed


@pytest.mark.parametrize("removed", ["barracks", "fairy_farm"])
def test_receipt_of_removed_optional_inputs_is_stale(bundle, unfarmed, tmp_path, removed):
    current = bundle.training
    assert current.barracks is not None
    later = TrainingService(bundle.bootstrap, bundle.player_state,
                            unfarmed if removed == "fairy_farm" else current.catalog, bundle.gifts,
                            barracks=None if removed == "barracks" else current.barracks)
    saved = real_case(bundle, tmp_path, later)
    envelope = message(saved)
    current.integrate(envelope, saved.store)
    fails(saved, envelope, "training_retry_stale")


@pytest.mark.parametrize("catalog_version", [None, "c" * 64], ids=["unknown_digest", "unknown_catalog"])
def test_forged_receipt_digest_is_stale(case, catalog_version):
    envelope = message(case)
    case.service.integrate(envelope, case.store)
    case.service.barracks = case.bundle.unit_capacity
    capacity = case.bundle.unit_capacity.content_version
    forged = ("f" * 64 if catalog_version is None
              else hashlib.sha256(f"{catalog_version}:{capacity}".encode()).hexdigest())
    rewrite(case, lambda state: state["training_history"]["receipts"][-1].update(content=forged))
    fails(case, envelope, "training_retry_stale")


def test_two_simultaneous_retries_charge_once(case):
    envelope = message(case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: case.service.integrate(envelope, case.store), range(2)))
    assert results[0] == results[1]
    assert case.store.profile().revision == 1
    assert case.store.profile().state["tables"]["game_param"][0]["Money"] == 600


@pytest.mark.parametrize("protection", ["locked", "different_squad", "prince"])
def test_protected_materials_are_never_consumed(case, protection):
    materials = None
    if protection == "locked":
        mutate(case, lambda state: state["player_state"]["units"][2].update(GeneralPurposeFlags=3))
    elif protection == "different_squad":
        mutate(case, lambda state: state["player_state"]["squads"].append(
            {"Address": 0x70000, "UnitIndex": 2, "Name": "", "Flags": 0}))
    else:
        materials = (case.store.profile().state["player_state"]["units"][0],)
    fails(case, message(case, materials=materials), "training_material_protected")


def test_locked_target_is_allowed(case):
    mutate(case, lambda state: state["player_state"]["units"][1].update(GeneralPurposeFlags=3))
    case.service.integrate(message(case), case.store)
    assert case.store.profile().state["player_state"]["units"][1]["GeneralPurposeFlags"] == 3


def test_prince_target_is_refused_before_any_quote(case):
    prince = case.store.profile().state["player_state"]["units"][0]
    fails(case, message(case, base=prince), "training_target_ineligible")


@pytest.mark.parametrize("position", [1, 2, 3])
@pytest.mark.parametrize("flags", [0x11, 0x21, -0x80000000 | 0x81])
def test_old_primary_reference_rejects_unit_moved_to_other_barracks(case, position, flags):
    envelope = message(case)
    mutate(case, lambda state: state["player_state"]["units"][position].update(GeneralPurposeFlags=flags))
    fails(case, envelope, "training_storage_unsupported")


def test_status_separates_native_exclusions_from_missing_wiring(case):
    status = case.service.status()
    assert status["native_exclusions"] == ["Additional barrack targets, which the native target menus never select"]
    assert status["unsupported"] == ["Additional barrack materials"]


def test_target_in_additional_barracks_is_refused_until_it_returns(case):
    envelope = message(case)
    mutate(case, lambda state: state["player_state"]["units"][1].update(GeneralPurposeFlags=0x11))
    fails(case, envelope, "training_storage_unsupported")
    mutate(case, lambda state: state["player_state"]["units"][1].update(GeneralPurposeFlags=1))
    case.service.integrate(envelope, case.store)
    assert case.store.profile().state["tables"]["game_param"][0]["Money"] == 600


def test_primary_barracks_allows_unrelated_flag_bits(case):
    flags = -0x80000000 | 0x105
    def change(state):
        for unit in state["player_state"]["units"][1:]:
            unit["GeneralPurposeFlags"] = flags
    mutate(case, change)
    case.service.integrate(message(case), case.store)
    after = case.store.profile().state
    assert after["player_state"]["units"][1]["GeneralPurposeFlags"] == flags
    assert len(after["player_state"]["units"]) == 2
    assert after["tables"]["game_param"][0]["Money"] == 600


@pytest.mark.parametrize("position", [1, 2, 3])
def test_cas_conflict_rechecks_actual_barracks(case, monkeypatch, position):
    update = case.store.update_state
    competing_state = []
    def conflict(state, revision):
        if not competing_state:
            current = case.store.profile()
            current.state["player_state"]["units"][position]["GeneralPurposeFlags"] = 0x11
            update(current.state, current.revision)
            competing_state.append(case.store.profile())
        return update(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    with pytest.raises(ProtocolError) as caught:
        case.service.integrate(message(case), case.store)
    assert caught.value.code == "training_storage_unsupported"
    assert case.store.profile() == competing_state[0]


@pytest.mark.parametrize("cost,code", [(1001, "training_money_insufficient"), (1000, "training_native_balance")])
def test_money_and_native_restart_constraints_preserve_materials(case, cost, code):
    case.catalog.price = cost
    fails(case, message(case), code)


def test_exact_balance_can_be_spent_with_nonzero_account_experience(case):
    mutate(case, lambda state: state["tables"]["game_param"][0].update(Experience=1))
    case.catalog.price = 1000
    case.service.integrate(message(case), case.store)
    assert case.store.profile().state["tables"]["game_param"][0]["Money"] == 0


def test_stale_identity_does_not_match_reused_list_position(case):
    fails(case, message(case, materials=({**case.units[1], "UniqueID": 999},)), "training_unit_missing")


def test_response_preflight_failure_does_not_commit(case, monkeypatch):
    def reject(*args, **kwargs):
        raise ProtocolError("response_too_large", "test")
    monkeypatch.setattr(case.bundle.player_state, "unit_response", reject)
    fails(case, message(case), "response_too_large")


def test_lost_cas_rechecks_material_protection(case, monkeypatch):
    update = case.store.update_state
    attempts = []
    def conflict(state, revision):
        attempts.append(revision)
        if len(attempts) == 1:
            current = case.store.profile()
            current.state["player_state"]["units"][2]["GeneralPurposeFlags"] |= 2
            update(current.state, current.revision)
        return update(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    with pytest.raises(ProtocolError, match="protected"):
        case.service.integrate(message(case), case.store)
    assert case.store.profile().state["tables"]["game_param"][0]["Money"] == 1000
    assert len(case.store.profile().state["player_state"]["units"]) == 4


def test_consuming_old_high_love_instance_preserves_unclaimed_legacy_baseline(case):
    card_id = next(card for card in case.bundle.gifts.catalog.card_ids
                   if case.bundle.gifts.catalog.card(card)["BonusType3"])
    current = case.store.profile()
    state, added = case.bundle.player_state.credit_units(current.state, [{"card_id": card_id, "level": 1, "count": 1}])
    state["player_state"]["units"][-1].update(Love=150, WatchLoveEv=3)
    case.store.update_state(state, current.revision)
    case.service.integrate(message(case, materials=added), case.store)
    after = case.store.profile().state
    assert after["gift_state"]["claims"][str(card_id)] == [1, 100, 150]
    assert after["tables"]["presents"] == current.state["tables"]["presents"]


@pytest.mark.parametrize("journal", [{}, None, {"schema_version": True, "receipts": []},
    {"schema_version": 2, "receipts": []}, {"schema_version": 1, "receipts": [{}]},
    {"schema_version": 2, "receipts": [], "last_client_time": None, "seen_keys": ["a" * 64]},
    {"schema_version": 2, "receipts": [], "last_client_time": True, "seen_keys": []},
    {"schema_version": 2, "receipts": [], "last_client_time": 1, "seen_keys": ["a" * 64] * 2},
    {"schema_version": 2, "receipts": [], "processed_keys": [True]},
    {"schema_version": 3, "receipts": [], "processed_keys": []}])
def test_invalid_history_is_not_silently_reinitialized(case, journal):
    mutate(case, lambda state: state.update(training_history=journal))
    fails(case, message(case), "training_history_invalid")


def test_client_clock_rollback_and_signed_client_times_still_train(case):
    case.catalog.price = 1
    materials = (*case.units[1:], *credit(case, 2))
    for tick, material in zip((0x7FFFFFFF, -0x80000000, -1, 1000), materials):
        case.service.integrate(message(case, tick=tick, materials=(material,)), case.store)
    after = case.store.profile().state
    assert [unit["GetIndex"] for unit in after["player_state"]["units"]] == [0, 1]
    history = after["training_history"]
    assert history["schema_version"] == SCHEMA_VERSION and len(history["processed_keys"]) == 4
    assert all("client_time" not in receipt for receipt in history["receipts"])


def test_evicted_same_second_receipt_stays_processed(case):
    case.catalog.price = 1
    envelopes = [message(case, materials=(material,)) for material in credit(case, HISTORY_LIMIT + 1)]
    responses = [case.service.integrate(envelope, case.store) for envelope in envelopes]
    history = case.store.profile().state["training_history"]
    assert len(history["receipts"]) == HISTORY_LIMIT and len(history["processed_keys"]) == HISTORY_LIMIT + 1
    fails(case, envelopes[0], "training_request_stale")
    fails(case, envelopes[1], "training_retry_stale")
    assert case.service.integrate(envelopes[-1], case.store) == responses[-1]


def test_receipt_only_journal_upgrades_and_replays_its_receipt(case):
    case.catalog.price = 1
    envelope = message(case, materials=case.units[1:2])
    response = case.service.integrate(envelope, case.store)
    receipt = case.store.profile().state["training_history"]["receipts"][0]
    rewrite(case, lambda state: state.update(training_history={"schema_version": 1, "receipts": [receipt]}))
    assert case.service.integrate(envelope, case.store) == response
    case.service.integrate(message(case, tick=999, materials=case.units[2:]), case.store)
    history = case.store.profile().state["training_history"]
    assert history["processed_keys"] == [receipt["request"], history["receipts"][-1]["request"]]


@pytest.mark.parametrize("client_time", [2000, None])
def test_watermark_journal_upgrades_keeps_seen_requests_and_accepts_earlier_time(case, client_time):
    case.catalog.price = 1
    materials = credit(case, 1)
    envelope = message(case, tick=2000, materials=case.units[1:2])
    response = case.service.integrate(envelope, case.store)
    receipt = case.store.profile().state["training_history"]["receipts"][0]
    evicted = message(case, tick=2000, materials=case.units[2:])
    seen = [hashlib.sha256(evicted.xml).hexdigest(), *([receipt["request"]] if client_time is not None else [])]
    rewrite(case, lambda state: state.update(training_history={"schema_version": 2,
        "receipts": [{**receipt, "client_time": client_time}], "last_client_time": 2000, "seen_keys": seen}))
    assert case.service.integrate(envelope, case.store) == response
    fails(case, evicted, "training_request_stale")
    case.service.integrate(message(case, tick=1, materials=materials), case.store)
    history = case.store.profile().state["training_history"]
    assert history["schema_version"] == SCHEMA_VERSION
    assert history["processed_keys"] == [seen[0], receipt["request"], history["receipts"][-1]["request"]]


@pytest.mark.parametrize("kind,code", [("protected", "training_material_protected"),
                                       ("target_as_material", "invalid_request_fields")])
def test_refused_training_is_logged_because_the_native_client_resends_it(case, caplog, kind, code):
    if kind == "protected":
        mutate(case, lambda state: state["player_state"]["units"][2].update(GeneralPurposeFlags=3))
        envelope = message(case)
    else:
        envelope = message(case, materials=case.units[:1])
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        fails(case, envelope, code)
        case.service.integrate(message(case, materials=case.units[2:]), case.store)
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        f"Training 5 refused with {code}; the native client shows a communication error and resends the same request "
        "until the server accepts it"]
