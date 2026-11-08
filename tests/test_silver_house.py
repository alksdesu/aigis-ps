from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import json
import logging
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import MAX_PLAYER_UNITS, UNIT_FIELDS
from aigis_ps.silver_house import SilverHouseRules, SilverHouseService
from aigis_ps.storage import Store
from test_quest_service import bundle, request


@pytest.fixture
def case(bundle, tmp_path):
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    return bundle.silver_house, Store(tmp_path / "silver.sqlite3", state)


def test_empty_legacy_inventory_loads_without_modifying_save(case):
    service, store = case
    before = store.profile()
    data = {child.name: child for child in loads(service.load(request(store, 126, ()), store)).children}
    assert set(data) == {"SYSTEM", "PSH"}
    assert data["PSH"].children[0].values == data["PSH"].children[1].values == ()
    assert store.profile() == before


def test_full_snapshot_preserves_zero_rows_and_is_independent_of_saved_order(case):
    service, store = case
    before = store.profile()
    before.state.update(silver_house_schema=1)
    before.state["tables"]["silver_house"] = [{"CardID": 8, "Count": 0}, {"CardID": 3, "Count": 12}]
    store.update_state(before.state, before.revision)
    saved = store.profile()
    response = service.response(saved.state)
    assert response.children[0].values == (3, 8)
    assert response.children[1].values == (12, 0)
    rows = service.read(saved.state)
    rows[0]["Count"] = 0
    assert store.profile() == saved


@pytest.mark.parametrize("schema,rows", [(None, [{"CardID": 3, "Count": 1}]), (True, []), (2, []),
    (1, [{"CardID": 3, "Count": -1}]), (1, [{"CardID": 3, "Count": True}]),
    (1, [{"CardID": 3, "Count": 1}, {"CardID": 3, "Count": 2}]),
    (1, [{"CardID": 2147483647, "Count": 1}]), (1, [{"CardID": 3}])])
def test_invalid_inventory_is_not_discarded_or_normalized(case, schema, rows):
    service, store = case
    profile = store.profile()
    state = deepcopy(profile.state)
    if schema is not None:
        state["silver_house_schema"] = schema
    state["tables"]["silver_house"] = rows
    store.update_state(state, profile.revision)
    before = store.profile()
    with pytest.raises(ProtocolError):
        service.load(request(store, 126, ()), store)
    assert store.profile() == before


@pytest.fixture(scope="module")
def writable(bundle):
    return SilverHouseService(bundle.bootstrap, bundle.player_state, bundle.gifts.catalog,
                              bundle.gifts, lambda name: bundle.native_cdn.assets.read(name)[0])


@pytest.fixture
def writing(bundle, writable, tmp_path):
    state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    return SimpleNamespace(bundle=bundle, service=writable, store=Store(tmp_path / "silver-write.sqlite3", state))


def card(case, rarity):
    catalog = case.bundle.gifts.catalog
    return next(identifier for identifier in catalog.card_ids
        if identifier not in (1, 309) and catalog.card(identifier)["Rare"] == rarity
        and catalog.card(identifier)["InitClassID"] // 100
        and case.bundle.player_state.classes[catalog.card(identifier)["InitClassID"]]["MaxLevel"] >= 30)


def give(case, rarity=0, level=1, count=1, identifier=None):
    current = case.store.profile()
    state, units = case.bundle.player_state.credit_units(current.state,
        [{"card_id": identifier or card(case, rarity), "level": level, "count": count}])
    case.store.update_state(state, current.revision)
    return units


def mutate(case, action):
    current = case.store.profile()
    state = deepcopy(current.state)
    action(state)
    case.store.update_state(state, current.revision)


def overwrite(case, action):
    state = case.store.profile().state
    action(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))


def stock(case, identifier, count):
    mutate(case, lambda state: (state.update(silver_house_schema=1),
        state["tables"].update(silver_house=[{"CardID": identifier, "Count": count}])))


def deposit(case, units, tick=1000):
    return request(case.store, 127, (Container("LST", (
        Column("A7", "I", tuple(unit["GetIndex"] for unit in units)),
        Column("AB", "I", tuple(unit["UniqueID"] for unit in units)))),), tick=tick)


def withdraw(case, identifier, tick=1000):
    return request(case.store, 128, (Container("Draw", (Column("CardID", "I", (identifier,)),)),), tick=tick)


def fail(case, message, code):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.handlers()[message.profile.registration_keys[0]](message, case.store)
    assert caught.value.code == code
    assert case.store.profile() == before


def data(response):
    return {child.name: child for child in loads(response).children}


def test_writes_remain_optional_for_old_constructor(bundle):
    read_only = SilverHouseService(bundle.bootstrap, bundle.player_state, bundle.gifts.catalog)
    assert set(read_only.handlers()) == {126}
    assert read_only.status()["write_ready"] is False


def test_actual_menu_and_confirmation_resources_define_rules(writable):
    assert writable.cap == 200 and writable.max_selected == 10
    assert writable.reward_tiers == {2: {"minimum_level": 30, "card_id": 460},
                                    3: {"minimum_level": 20, "card_id": 660}}
    assert writable.status()["local_rules"] == {"reward_count": 1}


@pytest.mark.parametrize("value", [True, False, -1, MAX_PLAYER_UNITS + 1, 1.0, "1", None])
def test_reward_rule_rejects_noninteger_and_unbounded_counts(value):
    with pytest.raises(ValueError):
        SilverHouseRules(reward_count=value)


def test_reward_rules_are_immutable():
    rule = SilverHouseRules()
    with pytest.raises(FrozenInstanceError):
        rule.reward_count = 2


@pytest.mark.parametrize("rarity,level,expected", [(0, 30, None), (1, 30, None), (2, 29, None),
    (2, 30, 460), (2, 31, 460), (3, 19, None), (3, 20, 660), (3, 21, 660)])
def test_deposit_thresholds_and_empty_ppu_are_exact(writing, rarity, level, expected):
    units = give(writing, rarity, level)
    before = writing.store.profile()
    response = data(writing.service.deposit(deposit(writing, units), writing.store))
    saved = writing.store.profile()
    assert saved.state["tables"]["silver_house"] == [{"CardID": units[0]["CardID"], "Count": 1}]
    assert response["RML"].children[0].values == (units[0]["GetIndex"],)
    assert response["RML"].children[1].values == (units[0]["UniqueID"],)
    assert len(response["PPU"].children) == len(UNIT_FIELDS)
    assert response["PPU"].children[0].values == (() if expected is None else (expected,))
    assert ("PBP" in response) is (expected is not None)
    assert saved.state["tables"]["game_param"] == before.state["tables"]["game_param"]
    assert saved.state["player_state"]["last_unique_id"] >= units[0]["UniqueID"]


def test_float32_threshold_minus_one_does_not_qualify(writing):
    units = give(writing, 2, 30)
    mutate(writing, lambda state: state["player_state"]["units"][-1].update(Exp=units[0]["Exp"] - 1))
    response = data(writing.service.deposit(deposit(writing, units), writing.store))
    assert response["PPU"].children[0].values == ()


def test_growth_is_reset_on_withdrawal_but_account_history_is_retained(writing):
    identifier = next(i for i in writing.bundle.gifts.catalog.card_ids if i not in (1, 309)
        and writing.bundle.gifts.catalog.card(i)["Rare"] == 2 and writing.bundle.gifts.catalog.card(i)["BonusType"]
        and writing.bundle.gifts.catalog.card(i)["InitClassID"] // 100)
    units = give(writing, identifier=identifier, level=30)
    current = writing.store.profile()
    state = deepcopy(current.state)
    state["player_state"]["units"][-1].update(Love=100, WatchLoveEv=2, SkillLevel=3,
        CostModMixValue=-1, SkillExperience=1, CostExperience=1)
    state = writing.bundle.card_status.update(state, [{"CardID": identifier, "A7": 3, "B0": 1234}])
    writing.store.update_state(state, current.revision)
    writing.service.deposit(deposit(writing, units), writing.store)
    deposited = writing.store.profile()
    assert deposited.state["gift_state"]["claims"][str(identifier)] == [1, 100]
    response = data(writing.service.withdraw(withdraw(writing, identifier, tick=1001), writing.store))
    final = writing.store.profile().state
    actual = final["player_state"]["units"][-1]
    assert actual == {**writing.service.material(identifier), "GetIndex": actual["GetIndex"], "UniqueID": actual["UniqueID"]}
    assert actual["UniqueID"] > units[0]["UniqueID"]
    assert writing.bundle.card_status.read(final) == writing.bundle.card_status.read(state)
    assert final["gift_state"] == deposited.state["gift_state"]
    assert response["PSH"].children[1].values == (0,)


@pytest.mark.parametrize("problem", ["lock", "squad", "prince", "evolved", "rarity", "material"])
def test_ineligible_deposit_is_atomic(writing, problem):
    units = give(writing, rarity=4 if problem == "rarity" else 0, identifier=54 if problem == "material" else None)
    if problem == "prince":
        prince = writing.bundle.gifts.catalog.card(309)
        mutate(writing, lambda state: state["player_state"]["units"][-1].update(CardID=309, ClassID=prince["InitClassID"]))
    if problem == "lock":
        mutate(writing, lambda state: state["player_state"]["units"][-1].update(GeneralPurposeFlags=3))
    if problem == "squad":
        mutate(writing, lambda state: state["player_state"]["squads"].append(
            {"Address": 0, "UnitIndex": units[0]["GetIndex"], "Name": "", "Flags": 0}))
    if problem == "evolved":
        mutate(writing, lambda state: state["player_state"]["units"][-1].update(ClassChangeCount=1))
    fail(writing, deposit(writing, units), "silver_house_unit_protected" if problem in ("lock", "squad", "prince")
         else "silver_house_unit_ineligible")


def test_refused_writes_are_logged_because_the_client_resends_them(writing, caplog):
    units = give(writing)
    mutate(writing, lambda state: state["player_state"]["units"][-1].update(GeneralPurposeFlags=3))
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    fail(writing, deposit(writing, units), "silver_house_unit_protected")
    fail(writing, withdraw(writing, card(writing, 0)), "silver_house_quantity")
    fail(writing, deposit(writing, units * 2), "invalid_request_fields")
    assert [record.getMessage() for record in caplog.records] == [
        "Silver warehouse 127 refused with silver_house_unit_protected; "
        "the native client shows a communication error and resends the same request until the server accepts it",
        "Silver warehouse 128 refused with silver_house_quantity; "
        "the native client shows a communication error and resends the same request until the server accepts it"]


def test_ten_unit_limit_and_duplicate_identity_are_enforced(writing):
    units = give(writing, count=11)
    fail(writing, deposit(writing, units), "invalid_request_fields")
    fail(writing, deposit(writing, units[:1] * 2), "invalid_request_fields")
    fail(writing, deposit(writing, [{**units[0], "UniqueID": 999}]), "silver_house_unit_missing")


def test_deposit_cap_checks_only_modified_card_and_preserves_old_overcap(writing):
    units = give(writing)
    identifier = units[0]["CardID"]
    other = card(writing, 1)
    stock(writing, other, 999)
    writing.service.deposit(deposit(writing, units), writing.store)
    assert writing.service.read(writing.store.profile().state) == sorted(
        [{"CardID": identifier, "Count": 1}, {"CardID": other, "Count": 999}], key=lambda row: row["CardID"])
    writing.service.withdraw(withdraw(writing, other, tick=1001), writing.store)
    assert next(row["Count"] for row in writing.service.read(writing.store.profile().state) if row["CardID"] == other) == 998


def test_full_changed_card_rejects_whole_deposit(writing):
    units = give(writing, 2, 30, count=2)
    stock(writing, units[0]["CardID"], 199)
    fail(writing, deposit(writing, units), "silver_house_quantity")


@pytest.mark.parametrize("reward_count,expected", [(0, 0), (2, 4)])
def test_configurable_reward_count(writing, reward_count, expected):
    writing.service = SilverHouseService(writing.bundle.bootstrap, writing.bundle.player_state,
        writing.bundle.gifts.catalog, writing.bundle.gifts, lambda name: writing.bundle.native_cdn.assets.read(name)[0],
        rules=SilverHouseRules(reward_count=reward_count))
    units = give(writing, 2, 30, count=2)
    response = data(writing.service.deposit(deposit(writing, units), writing.store))
    assert len(response["PPU"].children[0].values) == expected


def test_reward_capacity_error_does_not_remove_units_or_credit_stock(writing):
    writing.service = SilverHouseService(writing.bundle.bootstrap, writing.bundle.player_state,
        writing.bundle.gifts.catalog, writing.bundle.gifts, lambda name: writing.bundle.native_cdn.assets.read(name)[0],
        rules=SilverHouseRules(reward_count=MAX_PLAYER_UNITS))
    units = give(writing, 2, 30)
    fail(writing, deposit(writing, units), "unit_roster_capacity")


def test_reward_credit_sees_vacated_roster_and_old_highwater(writing, monkeypatch):
    units = give(writing, 2, 30)
    original = writing.bundle.player_state.credit_units
    seen = []
    def check(state, grants):
        seen.append(deepcopy(state["player_state"]))
        assert all(unit["GetIndex"] != units[0]["GetIndex"] for unit in state["player_state"]["units"])
        assert state["player_state"]["last_unique_id"] == units[0]["UniqueID"]
        return original(state, grants)
    monkeypatch.setattr(writing.bundle.player_state, "credit_units", check)
    writing.service.deposit(deposit(writing, units), writing.store)
    assert seen


def test_full_roster_can_exchange_for_an_equal_number_of_reward_units(writing, monkeypatch):
    import aigis_ps.player_state as module
    monkeypatch.setattr(module, "MAX_PLAYER_UNITS", 3)
    units = give(writing, 2, 30, count=2)
    assert len(writing.store.profile().state["player_state"]["units"]) == 3
    response = data(writing.service.deposit(deposit(writing, units), writing.store))
    assert response["PPU"].children[0].values == (460, 460)
    assert len(writing.store.profile().state["player_state"]["units"]) == 3


def test_withdrawal_is_one_per_packet_and_zero_stock_is_atomic(writing):
    identifier = card(writing, 0)
    fail(writing, withdraw(writing, identifier), "silver_house_quantity")
    stock(writing, identifier, 2)
    response = data(writing.service.withdraw(withdraw(writing, identifier), writing.store))
    assert response["PPU"].children[0].values == (identifier,)
    assert response["PSH"].children[1].values == (1,)
    message = request(writing.store, 128, (Container("Draw", (Column("CardID", "I", (identifier,)),
        Column("Count", "I", (2,)))),))
    fail(writing, message, "invalid_request_fields")


def test_silver_debit_is_pure_and_accepts_old_overcap(writing):
    identifier = card(writing, 0)
    stock(writing, identifier, 999)
    before = writing.store.profile()
    updated = writing.service.debit(before.state, {identifier: 1})
    assert writing.service.read(updated)[0]["Count"] == 998
    assert writing.store.profile() == before
    assert writing.service.debit(before.state, {}) == before.state


@pytest.mark.parametrize("costs", [{3: 0}, {3: -1}, {3: True}, {True: 1}, {2147483647: 1}, []])
def test_invalid_silver_costs_are_rejected(writing, costs):
    with pytest.raises(ProtocolError):
        writing.service.debit(writing.store.profile().state, costs)


@pytest.mark.parametrize("operation", [127, 128])
def test_exact_retry_and_reopening_do_not_reapply(writing, operation):
    units = give(writing, 2, 30)
    identifier = units[0]["CardID"]
    if operation == 128:
        stock(writing, identifier, 2)
    message = deposit(writing, units) if operation == 127 else withdraw(writing, identifier)
    response = writing.service.handlers()[operation](message, writing.store)
    before = writing.store.profile()
    reopened = Store(writing.store.path)
    assert writing.service.handlers()[operation](message, reopened) == response
    assert reopened.profile() == before
    mutate(writing, lambda state: state["tables"]["game_param"][0].update(Money=999))
    fail(writing, message, "silver_house_retry_stale")


def test_evicted_withdrawal_receipt_remains_processed(writing):
    identifier = card(writing, 0)
    stock(writing, identifier, 30)
    oldest = withdraw(writing, identifier)
    for tick in range(1000, 1018):
        writing.service.withdraw(withdraw(writing, identifier, tick), writing.store)
    history = writing.store.profile().state["silver_house_history"]
    assert len(history["receipts"]) == 16 and len(history["processed_keys"]) == 18
    assert writing.service.read(writing.store.profile().state)[0]["Count"] == 12
    fail(writing, oldest, "silver_house_request_stale")


def test_clock_rollback_accepts_new_deposits_and_withdrawals(writing):
    identifier = card(writing, 0)
    stock(writing, identifier, 2)
    writing.service.withdraw(withdraw(writing, identifier, 2000), writing.store)
    writing.service.withdraw(withdraw(writing, identifier, 1500), writing.store)
    units = give(writing)
    writing.service.deposit(deposit(writing, units, tick=-0x80000000), writing.store)
    after = writing.store.profile().state
    assert writing.service.read(after) == [{"CardID": identifier, "Count": 1}]
    assert set(after["silver_house_history"]) == {"schema_version", "receipts", "processed_keys"}


def test_legacy_watermark_history_upgrades_and_replays(writing):
    identifier = card(writing, 0)
    stock(writing, identifier, 3)
    message = withdraw(writing, identifier, 2000)
    response = writing.service.withdraw(message, writing.store)
    def legacy(state):
        history = state["silver_house_history"]
        receipt, = history["receipts"]
        state["silver_house_history"] = {"schema_version": 1, "last_client_time": 2000, "seen_keys": [receipt["request"]],
                                         "receipts": [{**receipt, "client_time": 2000}]}
    overwrite(writing, legacy)
    before = writing.store.profile()
    assert writing.service.withdraw(message, writing.store) == response
    assert writing.store.profile() == before
    writing.service.withdraw(withdraw(writing, identifier, 1000), writing.store)
    history = writing.store.profile().state["silver_house_history"]
    assert history["schema_version"] == 2 and len(history["receipts"]) == 2
    assert writing.service.read(writing.store.profile().state)[0]["Count"] == 1


@pytest.mark.parametrize("history", [{}, {"schema_version": True, "receipts": []},
    {"schema_version": 1, "last_client_time": None, "seen_keys": [], "receipts": [{}]}])
def test_unknown_or_malformed_receipt_history_preserves_state(writing, history):
    identifier = card(writing, 0)
    stock(writing, identifier, 1)
    mutate(writing, lambda state: state.update(silver_house_history=history))
    fail(writing, withdraw(writing, identifier), "silver_house_state_invalid")


def test_changed_local_reward_rules_cannot_replay_prior_success(writing):
    units = give(writing, 2, 30)
    message = deposit(writing, units)
    writing.service.deposit(message, writing.store)
    writing.service = SilverHouseService(writing.bundle.bootstrap, writing.bundle.player_state,
        writing.bundle.gifts.catalog, writing.bundle.gifts, lambda name: writing.bundle.native_cdn.assets.read(name)[0],
        rules=SilverHouseRules(reward_count=0))
    fail(writing, message, "silver_house_retry_stale")


def test_concurrent_duplicate_withdrawal_grants_once(writing):
    identifier = card(writing, 0)
    stock(writing, identifier, 2)
    message = withdraw(writing, identifier)
    revision = writing.store.profile().revision
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: writing.service.withdraw(message, writing.store), range(2)))
    assert responses[0] == responses[1]
    assert writing.store.profile().revision == revision + 1
    assert writing.service.read(writing.store.profile().state)[0]["Count"] == 1


def test_conflicting_lock_is_rechecked_before_commit(writing, monkeypatch):
    units = give(writing)
    original = writing.store.update_state
    calls = []
    def conflict(state, revision):
        calls.append(revision)
        if len(calls) == 1:
            current = writing.store.profile()
            current.state["player_state"]["units"][-1]["GeneralPurposeFlags"] |= 2
            original(current.state, current.revision)
        return original(state, revision)
    monkeypatch.setattr(writing.store, "update_state", conflict)
    with pytest.raises(ProtocolError) as caught:
        writing.service.deposit(deposit(writing, units), writing.store)
    assert caught.value.code == "silver_house_unit_protected"
    assert writing.service.read(writing.store.profile().state) == []
    assert len(writing.store.profile().state["player_state"]["units"]) == 2


@pytest.mark.parametrize("operation", [127, 128])
def test_preflight_failure_preserves_all_domains(writing, monkeypatch, operation):
    import aigis_ps.silver_house as module
    units = give(writing)
    identifier = units[0]["CardID"]
    if operation == 128:
        stock(writing, identifier, 1)
    message = deposit(writing, units) if operation == 127 else withdraw(writing, identifier)
    original = module.encode_response
    def reject(profile, xml):
        if profile.registration_keys == (126,):
            raise ProtocolError("silver_preflight_test", "capacity")
        return original(profile, xml)
    monkeypatch.setattr(module, "encode_response", reject)
    fail(writing, message, "silver_preflight_test")
