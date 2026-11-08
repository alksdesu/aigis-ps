from copy import deepcopy
import hashlib
import json

import pytest

from aigis_ps.alxml import Container
from aigis_ps.errors import ProtocolError
from aigis_ps.fame_state import COUNT_FIELDS, FameRepository, owned_fame_cards, read_fame
from aigis_ps.startup_diagnostics import StartupDiagnostics


STARTUP = 137
UNREADY = "fame_repository_changed"
DAMAGED = "fame_state_invalid"


class Catalog:
    card_ids = frozenset((3, 4, 7))
    deposit_limit = 99
    content_version = "a" * 64
    absent_versions = ("c" * 64,)
    ready = True

    def is_ready(self):
        return self.ready

    def replayable_content_versions(self):
        return frozenset((self.content_version, *self.absent_versions))


@pytest.fixture
def repository():
    return FameRepository(Catalog())


@pytest.fixture
def state():
    return {"bootstrap_schema": 1, "tables": {"fame_depository": [], "other": [1]}, "sentinel": {"data": [2]}}


def row(card_id=3, **counts):
    return {"CardId": card_id, **dict.fromkeys(COUNT_FIELDS, 0), **counts}


def test_old_empty_or_missing_collection_does_not_gain_a_schema(repository, state):
    original = deepcopy(state)
    assert repository.read(state) == []
    assert repository.response(state) == Container("FameDepository")
    assert repository.adjust(state, {}) == state
    assert repository.initialize(state) == state
    state["tables"].pop("fame_depository")
    assert read_fame(state) == []
    assert repository.initialize(original) == original


def test_deposit_and_withdraw_preserve_independent_stage_buckets_and_zero_rows(repository, state):
    first = repository.adjust(state, {(3, 0): 99, (3, 2): 99, (4, 3): 1})
    assert first["fame_depository_schema"] == 1
    assert repository.read(first) == [row(3, Count0=99, Count2=99), row(4, Count3=1)]
    assert repository.owned_card_ids(first) == frozenset((3, 4))
    second = repository.adjust(first, {(3, 0): -99, (4, 3): -1})
    assert repository.read(second) == [row(3, Count2=99), row(4)]
    assert repository.owned_card_ids(second) == frozenset((3,))
    response = repository.response(second, [4, 3])
    assert tuple(column.name for column in response.children) == ("CardId", *COUNT_FIELDS)
    assert response.children[0].values == (4, 3)
    assert response.children[3].values == (0, 99)
    assert state["tables"]["fame_depository"] == []
    second["sentinel"]["data"].append(3)
    assert state["sentinel"]["data"] == [2]


@pytest.mark.parametrize("changes", [{(3, 0): 100}, {(3, 0): 1, (4, 4): 100}, {(3, 0): -1},
    {(3, 0): 1, (4, 0): -1}, {(999, 0): 1}, {(3, 5): 1}, {(3, -1): 1},
    {(3, 0): 0}, {(3, 0): True}, {(3, False): 1}, {(True, 0): 1}, {3: 1}, [], None])
def test_invalid_or_excessive_batch_is_atomic(repository, state, changes):
    before = deepcopy(state)
    with pytest.raises(ProtocolError):
        repository.adjust(state, changes)
    assert state == before


def test_legacy_over_limit_counts_are_not_truncated_but_cannot_increase(repository, state):
    state.update(fame_depository_schema=1)
    state["tables"]["fame_depository"] = [row(3, Count2=120)]
    assert repository.read(state)[0]["Count2"] == 120
    with pytest.raises(ProtocolError) as error:
        repository.adjust(state, {(3, 2): 1})
    assert error.value.code == "fame_stock_capacity"
    assert repository.adjust(state, {(3, 2): -1})["tables"]["fame_depository"][0]["Count2"] == 119


@pytest.mark.parametrize("rows,schema", [([row()], None), ([], True), ([], 2),
    ([row(999)], 1), ([row(), row()], 1), ([row(Count1=True)], 1),
    ([row(Count0=-1)], 1), ([row(Count4=2147483648)], 1),
    ([{"CardID": 3, **dict.fromkeys(COUNT_FIELDS, 0)}], 1), ({}, 1)])
def test_invalid_rows_and_schema_are_not_silently_repaired(repository, state, rows, schema):
    state["tables"]["fame_depository"] = rows
    if schema is not None:
        state["fame_depository_schema"] = schema
    before = deepcopy(state)
    for callback in (lambda: repository.read(state), lambda: repository.adjust(state, {})):
        with pytest.raises(ProtocolError) as error:
            callback()
        assert error.value.code == "fame_state_invalid"
        assert state == before


def test_explicit_null_schema_is_not_legacy_absence(repository, state):
    state["fame_depository_schema"] = None
    with pytest.raises(ProtocolError):
        repository.read(state)


def test_selected_missing_card_is_explicit_zero_without_writing(repository, state):
    assert all(column.values == (0,) for column in repository.response(state, [7]).children[1:])
    assert state["tables"]["fame_depository"] == []


@pytest.mark.parametrize("selected", ["3", [3, 3], [True], [999], [-1], ["3"]])
def test_invalid_response_selection_is_explicit(repository, state, selected):
    with pytest.raises(ProtocolError):
        repository.response(state, selected)


def test_shared_ownership_ignores_all_zero_rows_and_validates_master_identity(repository, state):
    state = repository.adjust(state, {(3, 1): 1, (4, 2): 1})
    state = repository.adjust(state, {(4, 2): -1})
    assert owned_fame_cards(state, (3, 4, 7)) == frozenset((3,))
    with pytest.raises(ProtocolError):
        owned_fame_cards(state, (4, 7))


def test_dependency_replacement_is_rejected_before_read_or_write(repository, state):
    repository.catalog.content_version = "b" * 64
    with pytest.raises(ProtocolError) as error:
        repository.adjust(state, {(3, 0): 1})
    assert error.value.code == "fame_repository_changed"


def test_readiness_follows_the_catalog_without_building_its_status(repository, state):
    assert repository.is_ready() and repository.status()["ready"] is True
    repository.catalog.ready = False
    assert not repository.is_ready()
    assert (repository.status()["ready"], repository.status()["error"]) == (False, UNREADY)
    with pytest.raises(ProtocolError) as error:
        repository.read(state)
    assert error.value.code == UNREADY


def repository_digest(catalog_version):
    return hashlib.sha256(json.dumps({"schema": 1, "catalog": catalog_version, "limit": Catalog.deposit_limit,
        "capacity": "per_card_and_stage", "zero_rows": "preserved"}, sort_keys=True).encode()).hexdigest()


def test_digest_for_any_catalog_version_matches_a_repository_built_on_it(repository):
    catalog = Catalog()
    catalog.content_version = "c" * 64
    earlier = FameRepository(catalog).content_version
    assert repository.content_version == repository.content_version_for("a" * 64) == repository_digest("a" * 64)
    assert repository.content_version_for("c" * 64) == earlier == repository_digest("c" * 64)
    assert repository.replayable_content_versions() == {repository.content_version, earlier}
    repository.catalog.content_version = "b" * 64
    assert repository.content_version_for("a" * 64) == repository_digest("a" * 64)
    assert repository.content_version == repository_digest("a" * 64)


@pytest.fixture
def diagnostics():
    return StartupDiagnostics()


@pytest.fixture
def stocked(diagnostics, state):
    repository = FameRepository(Catalog(), diagnostics=diagnostics)
    stocked = repository.adjust(state, {(3, 1): 2, (4, 0): 1})
    saved = repository.response(stocked)
    repository.catalog.content_version = "b" * 64
    return repository, stocked, saved


def startup(diagnostics, action):
    with diagnostics.load(STARTUP):
        return action()


def test_full_snapshot_serves_the_saved_stock_while_definitions_are_unready(diagnostics, stocked):
    repository, state, saved = stocked
    assert startup(diagnostics, lambda: repository.response(state)) == saved
    record = diagnostics.status()["loads"][str(STARTUP)]
    assert record["outcome"] == "degraded" and [issue["code"] for issue in record["issues"]] == [UNREADY]


def test_selected_stock_reads_and_writes_keep_refusing_unready_definitions(diagnostics, stocked):
    repository, state, _ = stocked
    for action in (lambda: repository.response(state, [3]), lambda: repository.read(state),
                   lambda: repository.adjust(state, {(3, 1): -1})):
        with pytest.raises(ProtocolError) as error:
            action()
        assert error.value.code == UNREADY
    assert diagnostics.status() == {"attention_required": False, "loads": {}}


def test_full_snapshot_still_refuses_damaged_stock_while_definitions_are_unready(diagnostics, stocked):
    repository, state, _ = stocked
    state["tables"]["fame_depository"][0]["Count0"] = -1
    with pytest.raises(ProtocolError) as error:
        startup(diagnostics, lambda: repository.response(state))
    assert error.value.code == DAMAGED
    record = diagnostics.status()["loads"][str(STARTUP)]
    assert record["outcome"] == "refused" and [issue["code"] for issue in record["issues"]] == [UNREADY, DAMAGED]


def test_full_snapshot_without_diagnostics_keeps_refusing_unready_definitions(repository, state):
    repository.catalog.content_version = "b" * 64
    with pytest.raises(ProtocolError) as error:
        repository.response(state)
    assert error.value.code == UNREADY


def test_diagnostics_must_be_the_startup_registry():
    with pytest.raises(ValueError):
        FameRepository(Catalog(), diagnostics=object())
