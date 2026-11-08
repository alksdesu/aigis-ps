from copy import copy, deepcopy
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.protocol import ProtocolRegistry
from aigis_ps.unit_grants import MAX_GRANTS, MAX_INITIALIZATIONS, UnitGrants
from aigis_ps.unit_initialization import UnitInitialValues
from test_summon_grants import real


@pytest.fixture
def case(real):
    players = copy(real.bundle.player_state)
    helper = UnitGrants(players)
    state = players.initialize({"sentinel": {"nested": [1, 2]}})
    return SimpleNamespace(players=players, helper=helper, state=state)


def spec(card=17, level=1, *, count=1, stage=0):
    return {"card_id": card, "level": level, "evolution_count": stage, "count": count}


def fail(case, specs, code=None):
    before = deepcopy(case.state)
    with pytest.raises(ProtocolError) as caught:
        case.helper.grant(case.state, specs)
    assert case.state == before
    if code:
        assert caught.value.code == code
    return caught.value


def test_selected_cards_need_no_pool_and_defaults_are_explicit(case):
    initial = case.helper.initial_values(17, 1, 0)
    assert isinstance(initial, UnitInitialValues)
    updated, units = case.helper.grant(case.state, [{"card_id": 17, "level": 1}])
    assert units == [case.helper.expected_unit(17, initial, 1, 2)]
    assert updated["player_state"]["units"][-1] == units[0]
    assert len(units[0]) == 21


def test_grant_preserves_order_and_repeated_instances_with_one_credit_call(case, monkeypatch):
    calls = []
    original = case.players.credit_units
    def credit(state, rows):
        calls.append(deepcopy(rows))
        return original(state, rows)
    monkeypatch.setattr(case.players, "credit_units", credit)
    updated, units = case.helper.grant(case.state, [spec(17, 20, count=2), spec(2079, stage=2), spec(17)])
    assert len(calls) == 1
    assert [unit["CardID"] for unit in units] == [17, 17, 2079, 17]
    assert [unit["GetIndex"] for unit in units] == [1, 2, 3, 4]
    assert [unit["UniqueID"] for unit in units] == [2, 3, 4, 5]
    assert units[0]["Exp"] > 0 and units[-1]["Exp"] == 0
    assert units[2]["ClassChangeCount"] == 2
    assert updated["player_state"]["last_unique_id"] == 5
    assert updated["sentinel"] == case.state["sentinel"]


def test_identity_high_water_marks_are_retained(case):
    case.state["player_state"]["battle"]["UX"] = 100
    case.state["player_state"]["last_unique_id"] = 500
    _, units = case.helper.grant(case.state, [spec(count=2)])
    assert [(u["GetIndex"], u["UniqueID"]) for u in units] == [(101, 501), (102, 502)]


def test_grants_do_not_alias_each_other_or_input(case):
    before = deepcopy(case.state)
    updated, units = case.helper.grant(case.state, [spec(count=2)])
    units[0]["Love"] = 100
    updated["player_state"]["units"][-1]["Love"] = 200
    updated["sentinel"]["nested"].append(3)
    assert units[-1]["Love"] == updated["player_state"]["units"][-2]["Love"] == 0
    assert case.state == before


def test_hundred_instance_limit_is_on_sum_not_spec_count(case):
    assert MAX_GRANTS == 100
    updated, units = case.helper.grant(case.state, (spec(count=99), spec(2079, stage=2)))
    assert len(units) == 100 and updated["player_state"]["battle"]["UX"] == 100
    fail(case, [spec(count=100), spec()], "unit_grant_result_invalid")


@pytest.mark.parametrize("specs", [None, [], (), "x", [None], [{}], [{"card_id": 17}], [{"level": 1}],
    [dict(spec(), unknown=1)], [spec(count=0)], [spec(count=-1)], [spec(count=True)], [spec(count=1.0)],
    [spec(count=101)], [spec()] * 101])
def test_invalid_selection_fails_before_credit(case, monkeypatch, specs):
    monkeypatch.setattr(case.players, "credit_units", lambda *_: pytest.fail("bad selection reached credit"))
    fail(case, specs, "unit_grant_result_invalid")


@pytest.mark.parametrize("card,level", [(True, 1), (1, 1), (-1, 1), (17, True), (17, -1), (17, 1.0)])
def test_identity_errors_preserve_player_service_contract(case, card, level):
    fail(case, [spec(card, level)], "unit_grant_invalid")


@pytest.mark.parametrize("stage", [True, 1, 3, -1, 2.0])
def test_only_verified_stages_are_supported(case, stage):
    fail(case, [spec(stage=stage)], "unit_grant_initialization_unsupported")


@pytest.mark.parametrize("prefix", ["select_ticket", "summon_ticket"])
def test_custom_error_prefix_is_preserved(case, prefix):
    case.helper = UnitGrants(case.players, error_prefix=prefix)
    fail(case, [spec(stage=3)], f"{prefix}_initialization_unsupported")
    fail(case, [], f"{prefix}_result_invalid")


@pytest.mark.parametrize("prefix", [None, "", "has space", "UPPER", "x" * 65])
def test_error_prefix_is_bounded_and_valid(case, prefix):
    with pytest.raises(ValueError):
        UnitGrants(case.players, error_prefix=prefix)


def test_initial_values_are_frozen_and_cached(case, monkeypatch):
    values = case.helper.initial_values(17, 1)
    with pytest.raises(FrozenInstanceError):
        values.experience = 3
    current = case.players._initialization()
    monkeypatch.setattr(case.players, "_initialization", lambda: SimpleNamespace(
        content_version=current.content_version, values=lambda *_, **__: pytest.fail("cached initial values were recomputed")))
    assert case.helper.initial_values(17, 1) == values


def test_cached_initialization_catches_unexpected_same_version_new_grant(case, monkeypatch):
    frozen = case.helper.initial_values(17, 1)
    current = case.players._initialization()
    monkeypatch.setattr(case.players, "_initialization", lambda: SimpleNamespace(
        content_version=current.content_version, values=lambda *_, **__: replace(frozen, experience=1)))
    fail(case, [spec()], "unit_grant_result_invalid")


def test_changed_initialization_version_rejected_before_credit(case, monkeypatch):
    current = case.players._initialization()
    monkeypatch.setattr(case.players, "_initialization", lambda: SimpleNamespace(
        content_version="e" * 64, values=current.values))
    fail(case, [spec()], "unit_grant_dependencies_changed")


def test_initialization_fingerprint_must_be_valid(case, monkeypatch):
    monkeypatch.setattr(case.players, "_initialization", lambda: SimpleNamespace(content_version="bad"))
    with pytest.raises(ProtocolError) as caught:
        UnitGrants(case.players)
    assert caught.value.code == "unit_grant_dependencies_invalid"


def test_ready_and_initialization_version_are_public(case):
    assert case.helper.ready() is None
    assert len(case.helper.initialization_version) == 64
    with pytest.raises(AttributeError):
        case.helper.initialization_version = "0" * 64


@pytest.mark.parametrize("values", [None, UnitInitialValues(0, 0, 0, 0), UnitInitialValues(1, 1, 0, 0),
    UnitInitialValues(1, 0, -1, 0), UnitInitialValues(1, 0, 0, -1), UnitInitialValues(1, False, 0, 0)])
def test_expected_unit_rejects_unsupported_initializer_values(case, values):
    with pytest.raises(ProtocolError) as caught:
        case.helper.expected_unit(17, values, 1, 2)
    assert caught.value.code == "unit_grant_initialization_unsupported"


def test_capacity_failure_does_not_credit_partial_selection(case, monkeypatch):
    case.players.capacity_provider = lambda _: 1
    monkeypatch.setattr(case.players, "credit_units", lambda *_: pytest.fail("insufficient capacity reached credit"))
    fail(case, [spec(count=2)], "unit_roster_capacity")


def test_native_roster_encoding_capacity_is_preserved(case):
    case.players._unit_response_profile = replace(ProtocolRegistry.load().by_registration[41], response_limit=1)
    fail(case, [spec()], "unit_roster_capacity")


@pytest.mark.parametrize("fault", ["top", "old_unit", "extra", "missing", "order", "bool_int", "int_bool", "float_int", "short"])
def test_only_complete_expected_state_is_accepted(case, monkeypatch, fault):
    original = case.players.credit_units
    def credit(state, rows):
        updated, added = original(state, rows)
        if fault == "top":
            updated["unexpected"] = 0
        elif fault == "old_unit":
            updated["player_state"]["units"][0]["Love"] = 1
        elif fault == "extra":
            added[0]["extra"] = 0
        elif fault == "missing":
            del added[0]["Love"]
        elif fault == "order":
            added.reverse()
        elif fault == "bool_int":
            updated["player_state"]["units"][-1]["CanFame"] = 0
        elif fault == "int_bool":
            added[0]["Love"] = False
        elif fault == "float_int":
            updated["sentinel"]["nested"][0] = 1.0
        else:
            added.pop()
        return updated, added
    monkeypatch.setattr(case.players, "credit_units", credit)
    fail(case, [spec(count=2)], "unit_grant_result_invalid")


def test_dependency_error_does_not_mutate_callers_state(case, monkeypatch):
    def credit(state, specs):
        state["sentinel"]["nested"].clear()
        raise ProtocolError("unit_roster_capacity", "Injected error", 409)
    monkeypatch.setattr(case.players, "credit_units", credit)
    fail(case, [spec()], "unit_roster_capacity")


def test_resource_change_during_credit_invalidates_whole_result(case, monkeypatch):
    original = case.players.credit_units
    current = case.players._initialization()
    def credit(state, specs):
        result = original(state, specs)
        monkeypatch.setattr(case.players, "_initialization", lambda: SimpleNamespace(
            content_version="e" * 64, values=current.values))
        return result
    monkeypatch.setattr(case.players, "credit_units", credit)
    fail(case, [spec()], "unit_grant_dependencies_changed")


@pytest.mark.parametrize("count", [0, 1, 508, 1025])
def test_initial_batch_checks_live_resources_only_twice(case, monkeypatch, count):
    initializer = case.players._initialization()
    calls = []
    def current():
        calls.append(1)
        return initializer
    monkeypatch.setattr(case.players, "_initialization", current)
    helper = UnitGrants(case.players)
    calls.clear()
    specs = [{"card_id": 17, "level": index + 1, "evolution_count": 0} for index in range(count)]
    results = helper.initial_batch(specs)
    assert isinstance(results, tuple) and len(results) == count
    assert len(calls) == 2
    assert all(result == initializer.values(17, index + 1, evolution_count=0)
               for index, result in enumerate(results))


def test_public_single_value_and_expected_unit_still_check_live_version(case, monkeypatch):
    initializer = case.players._initialization()
    calls = []
    monkeypatch.setattr(case.players, "_initialization", lambda: (calls.append(1), initializer)[1])
    helper = UnitGrants(case.players)
    calls.clear()
    values = helper.initial_values(17, 1)
    assert len(calls) == 1
    helper.expected_unit(17, values, 1, 2)
    assert len(calls) == 2


def test_batch_version_change_mid_resolution_is_rejected(case, monkeypatch):
    source = case.players._initialization()
    live = {"value": source}
    monkeypatch.setattr(case.players, "_initialization", lambda: live["value"])
    helper = UnitGrants(case.players)
    def resolve(card, level, *, evolution_count=0):
        if level == 2:
            live["value"] = SimpleNamespace(content_version="d" * 64)
        return source.values(card, level, evolution_count=evolution_count)
    helper._initializer = SimpleNamespace(content_version=source.content_version, values=resolve)
    with pytest.raises(ProtocolError) as caught:
        helper.initial_batch([{"card_id": 17, "level": 1}, {"card_id": 17, "level": 2}])
    assert caught.value.code == "unit_grant_dependencies_changed"


@pytest.mark.parametrize("specs", [None, "bad", [None], [{}], [{"card_id": 17}],
    [{"card_id": 17, "level": 1, "count": 1}], [{"card_id": 17, "level": 1}] * (MAX_INITIALIZATIONS + 1)])
def test_initial_batch_is_bounded_and_does_not_accept_grant_counts(case, specs):
    with pytest.raises(ProtocolError) as caught:
        case.helper.initial_batch(specs)
    assert caught.value.code == "unit_grant_result_invalid"


def test_actual_credit_grant_adds_only_two_boundary_resource_checks(case, monkeypatch):
    initializer = case.players._initialization()
    calls = []
    monkeypatch.setattr(case.players, "_initialization", lambda: (calls.append(1), initializer)[1])
    helper = UnitGrants(case.players)
    specs = [spec()] * 100
    calls.clear()
    direct = case.players.credit_units(deepcopy(case.state), specs)
    original_checks = len(calls)
    calls.clear()
    checked = helper.grant(case.state, specs)
    assert checked == direct
    assert len(calls) == original_checks + 2


def test_summon_pool_batch_validation_has_constant_live_resource_checks(case, monkeypatch, real):
    from aigis_ps.summon_grants import SummonGrants
    from test_summon_grants import Pools
    initializer = case.players._initialization()
    calls = []
    monkeypatch.setattr(case.players, "_initialization", lambda: (calls.append(1), initializer)[1])
    pool = real.grants.validate_pool(350)
    assert len(pool.candidates) == 508
    helper = SummonGrants(case.players, Pools(pool), (350,))
    assert len(calls) == 7
    assert len(helper.initial_values[350]) == 508
    calls.clear()
    assert helper.validate_pool(350) == pool
    assert len(calls) == 1
