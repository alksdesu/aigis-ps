from copy import copy, deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.alxml import loads
from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.player_state import PlayerStateService, UNIT_BOOLEANS, UNIT_FIELDS
from aigis_ps.public_tables import MASTER_LIMITS
from aigis_ps.resource_library import ResourceLibrary
from aigis_ps.summon_grants import MAX_GRANTS, SummonGrants
from aigis_ps.summon_pool_catalog import SummonCandidate, SummonPool, SummonPoolCatalog


class Pools:
    content_version = "a" * 64

    def __init__(self, pool):
        self.pool = pool
        self.ready = True

    def is_ready(self):
        return self.ready

    def status(self):
        return {"ready": self.ready}

    def require_complete(self, identifier):
        if not self.pool.complete:
            raise ProtocolError("summon_pool_incomplete", "Incomplete pool", 501)
        return self.pool


@pytest.fixture(scope="module")
def real():
    project = Path(__file__).resolve().parents[1]
    source, resources = project.parent / "aigisr_exe", project / "runtime/resources"
    masters = MasterDataStore(project / "runtime/server/masterdata")
    if not (source / "Aigis_r.exe").is_file() or not (masters.directory / "active.json").is_file():
        pytest.skip("Verified local resources and public masters are required")
    snapshot = masters.snapshot()
    _, entries = ResourceLibrary(resources, client_hash=masters.expected_hash).load()
    assets = IndexedAssetService(entries, resources, source)
    reader = lambda name: assets.read(name)[0]
    players = PlayerStateService(source, snapshot.get_xml, asset_reader=reader)
    cards = {column.name: column.values for column in loads(snapshot.get_xml("CardData"), limits=MASTER_LIMITS).children}
    pools = SummonPoolCatalog(SimpleNamespace(card_ids=cards["CardID"], snapshot=snapshot), reader)
    bundle = SimpleNamespace(player_state=players, summon_pools=pools,
                             bootstrap=SimpleNamespace(initial_state=lambda: {}))
    return SimpleNamespace(bundle=bundle, grants=SummonGrants(bundle.player_state, bundle.summon_pools, (350, 428)),
                           rarities=dict(zip(cards["CardID"], cards["Rare"])))


@pytest.fixture
def case(real):
    players = copy(real.bundle.player_state)
    candidates = real.grants.validate_pool(350).candidates
    first = next(row for row in candidates if row.level == 1 and row.weight > 0)
    second = next(row for row in candidates if row.level == 20 and row.weight > 0)
    third = SummonCandidate(2079, 1, 2, 25.0, 1)
    pool = SummonPool(350, 1, (replace(first, percent=25.0, weight=1),
                              replace(second, percent=50.0, weight=2), third), 4, 100.0, True)
    pools = Pools(pool)
    grants = SummonGrants(players, pools, (350,))
    state = players.initialize(real.bundle.bootstrap.initial_state())
    state["sentinel"] = {"nested": [1, 2]}
    return SimpleNamespace(players=players, pools=pools, grants=grants, state=state)


def fails(case, indices=(0,), code=None, *, state=None):
    state = case.state if state is None else state
    before = deepcopy(state)
    with pytest.raises(ProtocolError) as caught:
        case.grants.grant(state, 350, indices)
    assert state == before
    if code is not None:
        assert caught.value.code == code
    return caught.value


def test_real_pools_are_fully_validated_and_keep_all_levels(real):
    assert set(real.grants.validated_pools) == {350, 428}
    pool = real.grants.validate_pool(350)
    assert len(pool.candidates) == 508
    assert len(real.grants.initial_values[350]) == 508
    assert {row.level for row in pool.candidates} == {1, 10, 20, 30}
    for identifier, validated in real.grants.validated_pools.items():
        assert validated.complete
        assert len(real.grants.initial_values[identifier]) == len(validated.candidates)
        for candidate, initial in zip(validated.candidates, real.grants.initial_values[identifier], strict=True):
            assert initial == real.bundle.player_state.initial_unit_values(candidate.card_id, candidate.level,
                                                                           evolution_count=candidate.evolution_count)


def test_real_pool428_ten_draw_grant_in_original_order(real):
    state = real.bundle.player_state.initialize(real.bundle.bootstrap.initial_state())
    before = deepcopy(state)
    indices = [index for index, row in enumerate(real.grants.validate_pool(428).candidates) if row.weight > 0][:10]
    assert len(indices) == 10
    updated, units = real.grants.grant(state, 428, indices)
    assert [row["CardID"] for row in units] == [real.grants.validated_pools[428].candidates[index].card_id for index in indices]
    assert updated["player_state"]["units"][-10:] == units
    assert state == before


def test_multidraw_calls_credit_once_and_preserves_duplicate_order(case, monkeypatch):
    actual = case.players.credit_units
    calls = []
    def credit(state, grants):
        calls.append(deepcopy(grants))
        return actual(state, grants)
    monkeypatch.setattr(case.players, "credit_units", credit)
    before = deepcopy(case.state)
    updated, units = case.grants.grant(case.state, 350, (2, 1, 0, 1, 2))
    assert len(calls) == 1 and [row["count"] for row in calls[0]] == [1] * 5
    assert [row["card_id"] for row in calls[0]] == [case.pools.pool.candidates[index].card_id for index in (2, 1, 0, 1, 2)]
    assert [row["GetIndex"] for row in units] == list(range(1, 6))
    assert [row["UniqueID"] for row in units] == list(range(2, 7))
    assert units[0]["ClassChangeCount"] == 2
    assert units[1]["Exp"] > 0
    assert units[1]["CardID"] == units[3]["CardID"] and units[1]["UniqueID"] != units[3]["UniqueID"]
    assert updated["player_state"]["battle"]["UX"] == 5
    assert updated["player_state"]["last_unique_id"] == 6
    assert case.state == before


def test_one_draw_matches_expected_unit_and_complete_native_fields(case):
    updated, units = case.grants.grant(case.state, 350, [0])
    assert units == [case.grants.expected_unit(350, 0, 1, 2)]
    assert set(units[0]) == {name for name, _ in UNIT_FIELDS}
    assert all(type(units[0][name]) is bool for name in UNIT_BOOLEANS)
    assert units[0]["OrganizationIndex"] == -1
    assert units[0]["GeneralPurposeFlags"] == 1
    assert updated["sentinel"] == case.state["sentinel"]


def test_high_water_marks_are_not_reused_after_previous_units_removed(case):
    case.state["player_state"]["battle"]["UX"] = 90
    case.state["player_state"]["last_unique_id"] = 400
    updated, units = case.grants.grant(case.state, 350, [0, 0])
    assert [(row["GetIndex"], row["UniqueID"]) for row in units] == [(91, 401), (92, 402)]
    assert updated["player_state"]["battle"]["UX"] == 92


def test_result_units_state_and_input_do_not_alias(case):
    before = deepcopy(case.state)
    updated, units = case.grants.grant(case.state, 350, [0, 0])
    units[0]["Love"] = 50
    updated["sentinel"]["nested"].append(3)
    updated["player_state"]["units"][-1]["Love"] = 99
    assert updated["player_state"]["units"][-2]["Love"] == 0
    assert units[-1]["Love"] == 0
    assert case.state == before


def test_pure_helper_has_no_replay_or_rng_state(case):
    one = case.grants.grant(case.state, 350, (0, 1))
    assert case.grants.grant(case.state, 350, (0, 1)) == one
    assert "summon_ticket_history" not in one[0]


def test_hundred_units_supported_and_hundred_one_rejected(case):
    assert MAX_GRANTS == 100
    case.players.capacity_provider = lambda _: 100
    updated, units = case.grants.grant(case.state, 350, [0] * 100)
    assert len(units) == 100 and updated["player_state"]["battle"]["UX"] == 100
    fails(case, [0] * 101, "summon_ticket_result_invalid")


@pytest.mark.parametrize("indices", [[], (), None, "0", {0}, [True], [False], [-1], [3], [1.0], ["1"], [2**31]])
def test_invalid_candidate_selection_never_grants(case, monkeypatch, indices):
    monkeypatch.setattr(case.players, "credit_units", lambda *_: pytest.fail("invalid selection reached grant"))
    fails(case, indices, "summon_ticket_result_invalid")


@pytest.mark.parametrize("pool_id", [True, 0, -1, 349, 428, 350.0, "350"])
def test_non_enabled_pool_rejected(case, pool_id):
    with pytest.raises(ProtocolError, match="not enabled") as caught:
        case.grants.grant(case.state, pool_id, [0])
    assert caught.value.code == "summon_ticket_pool_disabled"


@pytest.mark.parametrize("field,value", [("index", -1), ("index", True), ("get_index", 0), ("get_index", True),
                                         ("unique_id", 0), ("unique_id", 2**31)])
def test_expected_unit_validates_indices_and_native_identities(case, field, value):
    args = {"index": 0, "get_index": 1, "unique_id": 2}
    args[field] = value
    with pytest.raises(ProtocolError) as caught:
        case.grants.expected_unit(350, args["index"], args["get_index"], args["unique_id"])
    assert caught.value.code == "summon_ticket_result_invalid"


def test_zero_weight_rows_can_be_validated_but_never_granted(case):
    pool = replace(case.pools.pool, candidates=(*case.pools.pool.candidates,
        replace(case.pools.pool.candidates[0], percent=0.0, weight=0)))
    case.pools.pool = pool
    case.grants = SummonGrants(case.players, case.pools, (350,))
    assert len(case.grants.initial_values[350]) == 4
    fails(case, [3], "summon_ticket_result_invalid")


@pytest.mark.parametrize("change", ["pool_fingerprint", "initialization_fingerprint", "not_ready", "candidates"])
def test_resource_drift_fails_before_grant(case, monkeypatch, change):
    if change == "pool_fingerprint":
        case.pools.content_version = "b" * 64
    elif change == "initialization_fingerprint":
        initial = copy(case.players._initialization())
        initial.content_version = "c" * 64
        monkeypatch.setattr(case.players, "_initialization", lambda: initial)
    elif change == "not_ready":
        case.pools.ready = False
    else:
        case.pools.pool = replace(case.pools.pool, name_id=99)
    monkeypatch.setattr(case.players, "credit_units", lambda *_: pytest.fail("changed resource reached grant"))
    fails(case, code="summon_ticket_dependencies_changed")


def test_pool_readiness_is_checked_without_building_the_pool_status(case, monkeypatch):
    monkeypatch.setattr(case.pools, "status", lambda: pytest.fail("Readiness built the full pool status"))
    helper = SummonGrants(case.players, case.pools, (350,))
    assert helper.grant(case.state, 350, [0])[1] == [helper.expected_unit(350, 0, 1, 2)]


def test_fingerprints_and_cached_definitions_are_read_only(case):
    for name in ("enabled_pools", "pool_version", "initialization_version"):
        with pytest.raises(AttributeError):
            setattr(case.grants, name, "changed")
    with pytest.raises(TypeError):
        case.grants.validated_pools[350] = None
    with pytest.raises(TypeError):
        case.grants.initial_values[350] = ()
    with pytest.raises(FrozenInstanceError):
        case.grants.initial_values[350][0].stage = 2
    with pytest.raises(FrozenInstanceError):
        case.grants.validated_pools[350].candidates[0].card_id = 1


@pytest.mark.parametrize("enabled", [None, {350}, [350, 350], [True], [0], [-1], [2**31], ["350"]])
def test_bad_configuration_rejected(case, enabled):
    with pytest.raises(ValueError):
        SummonGrants(case.players, case.pools, enabled)


def test_no_enabled_pools_is_safe_disabled_configuration(case):
    helper = SummonGrants(case.players, case.pools, ())
    assert dict(helper.validated_pools) == {} and dict(helper.initial_values) == {}
    with pytest.raises(ProtocolError) as caught:
        helper.validate_pool(350)
    assert caught.value.code == "summon_ticket_pool_disabled"


@pytest.mark.parametrize("field", ["pool", "initialization"])
def test_bad_fingerprint_rejected_at_setup(case, monkeypatch, field):
    if field == "pool":
        case.pools.content_version = "invalid"
    else:
        initial = copy(case.players._initialization())
        initial.content_version = "invalid"
        monkeypatch.setattr(case.players, "_initialization", lambda: initial)
    with pytest.raises(ProtocolError) as caught:
        SummonGrants(case.players, case.pools, (350,))
    assert caught.value.code == "summon_ticket_dependencies_invalid"


def test_incomplete_pool_refused_at_setup(case):
    case.pools.pool = replace(case.pools.pool, complete=False)
    with pytest.raises(ProtocolError) as caught:
        SummonGrants(case.players, case.pools, (350,))
    assert caught.value.code == "summon_pool_incomplete"


@pytest.mark.parametrize("stage", [True, 1, 3, -1])
def test_unknown_initial_stage_refused_at_setup(case, stage):
    case.pools.pool = replace(case.pools.pool, candidates=(replace(case.pools.pool.candidates[0], evolution_count=stage),))
    with pytest.raises(ProtocolError) as caught:
        SummonGrants(case.players, case.pools, (350,))
    assert caught.value.code == "summon_ticket_initialization_unsupported"


def test_capacity_failure_is_atomic(case, monkeypatch):
    case.players.capacity_provider = lambda _: 9
    monkeypatch.setattr(case.players, "credit_units", lambda *_: pytest.fail("full barracks reached grant"))
    fails(case, [0] * 10, "unit_roster_capacity")


def test_reserved_slots_are_kept_for_active_battle(case):
    case.players.capacity_provider = lambda _: 4096
    case.state["player_state"]["battle"]["UX"] = 0x7FFFFFFF - 2
    candidate = case.pools.pool.candidates[0]
    case.state["quest_state"] = {"active": {"unit_reservations": [
        {"card_id": candidate.card_id, "level": candidate.level, "count": 2}]}}
    fails(case, [0], "unit_roster_capacity")


@pytest.mark.parametrize("fault", ["top_state", "player_metadata", "old_unit", "new_unit", "missing_field", "extra_field",
                                  "bool_as_int", "int_as_bool", "wrong_uid", "wrong_index", "order", "short", "extra"])
def test_credit_output_is_verified_before_any_candidate_can_be_committed(case, monkeypatch, fault):
    actual = case.players.credit_units
    def credit(state, grants):
        updated, added = actual(state, grants)
        if fault == "top_state":
            updated["sentinel"]["nested"].append(4)
        elif fault == "player_metadata":
            updated["player_state"]["legacy_squad_imported"] = True
        elif fault == "old_unit":
            updated["player_state"]["units"][0]["Love"] = 100
        elif fault == "new_unit":
            added[0]["Love"] = 100
        elif fault == "missing_field":
            del added[0]["CostExperience"]
        elif fault == "extra_field":
            added[0]["unknown"] = 0
        elif fault == "bool_as_int":
            added[0]["CanFame"] = 0
        elif fault == "int_as_bool":
            added[0]["Love"] = False
        elif fault == "wrong_uid":
            added[0]["UniqueID"] += 1
        elif fault == "wrong_index":
            added[0]["GetIndex"] += 1
        elif fault == "order":
            added.reverse()
        elif fault == "short":
            added.pop()
        else:
            added.append(deepcopy(added[0]))
        return updated, added
    monkeypatch.setattr(case.players, "credit_units", credit)
    fails(case, [0, 1], "summon_ticket_result_invalid")


def test_failing_dependency_cannot_mutate_original_input(case, monkeypatch):
    def credit(state, grants):
        state["sentinel"]["nested"].clear()
        raise ProtocolError("unit_roster_capacity", "Injected native response capacity failure", 409)
    monkeypatch.setattr(case.players, "credit_units", credit)
    fails(case, code="unit_roster_capacity")


def test_resource_drift_during_credit_invalidates_candidate(case, monkeypatch):
    actual = case.players.credit_units
    def credit(state, grants):
        result = actual(state, grants)
        case.pools.content_version = "d" * 64
        return result
    monkeypatch.setattr(case.players, "credit_units", credit)
    fails(case, code="summon_ticket_dependencies_changed")


@pytest.mark.parametrize("fault", ["saved_unit", "old_unit", "metadata", "unrelated"])
def test_equal_numeric_values_with_different_types_cannot_evade_state_guard(case, monkeypatch, fault):
    actual = case.players.credit_units
    def credit(state, grants):
        updated, added = actual(state, grants)
        if fault == "saved_unit":
            updated["player_state"]["units"][-1]["CanFame"] = 0
        elif fault == "old_unit":
            updated["player_state"]["units"][0]["Love"] = False
        elif fault == "metadata":
            updated["player_state"]["legacy_squad_imported"] = 0
        else:
            updated["sentinel"]["nested"][0] = 1.0
        return updated, added
    monkeypatch.setattr(case.players, "credit_units", credit)
    fails(case, code="summon_ticket_result_invalid")


def test_invalid_state_remains_unchanged(case):
    fails(case, state={"player_state": {}}, code="player_state_schema")


def scripted(values):
    calls = []
    def source(limit):
        calls.append(limit)
        return values.pop(0)
    return source, calls


def test_subset_draw_follows_candidate_weights_in_selection_order(case):
    source, calls = scripted([0, 1, 2, 0, 1])
    assert [case.grants.draw(350, (1, 2), source) for _ in range(3)] == [1, 1, 2]
    assert [case.grants.draw(350, (2, 0), source) for _ in range(2)] == [2, 0]
    assert calls == [3, 3, 3, 2, 2]


@pytest.mark.parametrize("indices", [[1], (), None, (1, 1), (3,), (-1,), (True,), (1.0,), ("1",), (2**31,)])
def test_subset_draw_rejects_invalid_selection_before_random(case, indices):
    with pytest.raises(ProtocolError) as caught:
        case.grants.draw(350, indices, lambda _: pytest.fail("invalid selection reached the random source"))
    assert caught.value.code == "summon_ticket_result_invalid"


def test_subset_draw_never_selects_zero_weight_rows(case):
    case.pools.pool = replace(case.pools.pool, candidates=(*case.pools.pool.candidates,
        replace(case.pools.pool.candidates[0], percent=0.0, weight=0)))
    case.grants = SummonGrants(case.players, case.pools, (350,))
    with pytest.raises(ProtocolError) as caught:
        case.grants.draw(350, (0, 3), lambda _: 0)
    assert caught.value.code == "summon_ticket_result_invalid"


@pytest.mark.parametrize("value", [True, -1, 3, None, 1.5])
def test_subset_draw_rejects_out_of_range_random_values(case, value):
    with pytest.raises(ProtocolError) as caught:
        case.grants.draw(350, (1, 2), lambda _: value)
    assert (caught.value.code, caught.value.status) == ("summon_random_invalid", 503)


def test_subset_draw_requires_a_callable_source_and_an_enabled_pool(case):
    with pytest.raises(ValueError):
        case.grants.draw(350, (0,), None)
    with pytest.raises(ProtocolError) as caught:
        case.grants.draw(428, (0,), lambda _: 0)
    assert caught.value.code == "summon_ticket_pool_disabled"


def test_candidate_rarities_follow_card_data_and_are_cached(real):
    pool = real.grants.validate_pool(428)
    rarities = real.grants.candidate_rarities(428)
    assert rarities == tuple(real.rarities[candidate.card_id] for candidate in pool.candidates)
    assert real.grants.candidate_rarities(428) is rarities
    assert real.bundle.player_state.card_rarities() == real.rarities


def test_candidate_rarities_follow_the_initialization_fingerprint(case, monkeypatch):
    case.grants.candidate_rarities(350)
    initial = copy(case.players._initialization())
    initial.content_version = "c" * 64
    monkeypatch.setattr(case.players, "_initialization", lambda: initial)
    with pytest.raises(ProtocolError) as caught:
        case.grants.candidate_rarities(350)
    assert caught.value.code == "summon_ticket_dependencies_changed"


@pytest.mark.parametrize("fault", ["missing", "negative", "bool", "oversized"])
def test_unverified_candidate_rarity_is_unavailable(case, monkeypatch, fault):
    rarities = dict(case.players.card_rarities())
    card_id = case.pools.pool.candidates[0].card_id
    if fault == "missing":
        del rarities[card_id]
    else:
        rarities[card_id] = {"negative": -1, "bool": True, "oversized": 2 ** 31}[fault]
    monkeypatch.setattr(case.players, "card_rarities", lambda: rarities)
    with pytest.raises(ProtocolError) as caught:
        case.grants.candidate_rarities(350)
    assert (caught.value.code, caught.value.status) == ("summon_ticket_rarity_invalid", 503)


def test_lowest_card_rarity_is_a_verified_value(case, monkeypatch):
    rarities = {**case.players.card_rarities(), case.pools.pool.candidates[0].card_id: 0}
    monkeypatch.setattr(case.players, "card_rarities", lambda: rarities)
    assert case.grants.candidate_rarities(350)[0] == 0
