from copy import copy, deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import loads
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps import cli
from aigis_ps.mission_service import MissionService
from aigis_ps.startup_diagnostics import StartupDiagnostics
from aigis_ps.storage import Store
from test_evolution_catalog import materials, unit
from test_evolution_integration import message as evolution_message, seed
from test_gift_service import choose, message as gift_message
from test_quest_service import bundle, finish_request, request, start_request
from test_training_service import message as training_message


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


@pytest.fixture
def local(bundle):
    assert bundle.missions is not None, bundle.mission_error
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    missions = MissionService(bootstrap, bundle.player_state, bundle.presents, bundle.missions.catalog)
    bootstrap.missions = missions
    result = SimpleNamespace(bundle=bundle, bootstrap=bootstrap, missions=missions)
    for name in ("training", "gifts", "evolution", "quests"):
        service = copy(getattr(bundle, name))
        service.bootstrap, service.missions = bootstrap, missions
        if name in ("gifts", "quests"):
            service.clock = lambda: NOW
        setattr(result, name, service)
    return result


def progress(local, store, identifier, now=NOW):
    rows = local.missions.progress.snapshot(store.profile().state, now)
    return next(row for row in rows if row["ID"] == identifier)


def scenario(local, tmp_path, kind):
    data = local.bundle
    state = data.player_state.initialize(local.bootstrap.initial_state())
    state["tables"]["game_param"][0]["Money"] = 1000000
    if kind == "training":
        state, units = data.player_state.credit_units(state, [{"card_id": 3, "level": 1, "count": 3}])
    elif kind == "gift":
        card = choose(SimpleNamespace(catalog=data.gifts.catalog, aliases=data.gifts.aliases), rare=3)
        state, units = data.player_state.credit_units(state, [{"card_id": card, "level": 1, "count": 1}])
        state = data.possessions.credit(state, {"gift_0003_diamond": 4})
    elif kind in ("first_awake", "second_awake"):
        operation = 7 if kind == "first_awake" else 9
        target = unit(data.evolution.catalog, 7 if operation == 7 else 17,
                      stage=0 if operation == 7 else 2, level=999 if operation == 7 else 1)
        selected = materials(data.evolution.catalog, target, operation)
        state, units = seed(data, target, selected)
        quote = data.evolution.catalog.quote(units[0], units[1:], operation, 1 if operation == 9 else 0)
        state = data.possessions.credit(state, dict(quote.items))
    store = Store(tmp_path / (kind + ".sqlite3"), local.missions.initialize(state))
    if kind == "login":
        envelope = request(store, 114, ())
        command, identifier = lambda: local.bootstrap.login(envelope, store), 1611
    elif kind == "training":
        case = SimpleNamespace(store=store, units=units)
        envelope = training_message(case)
        command, identifier = lambda: local.training.integrate(envelope, store), 1609
    elif kind == "gift":
        case = SimpleNamespace(store=store, indices=[units[0]["GetIndex"]], service=local.gifts)
        envelope = gift_message(case)
        command, identifier = lambda: local.gifts.send(envelope, store), 1608
    elif kind in ("first_awake", "second_awake"):
        envelope = evolution_message(store, units[0], units[1:], operation, branch=1 if operation == 9 else 0)
        condition_type = 7 if operation == 7 else 8
        identifier = next(row.condition_id for row in local.missions.catalog.active(NOW)
                          if row.period_type == 5 and row.condition_type == condition_type)
        command = lambda: local.evolution.evolve(envelope, store)
    else:
        local.quests.start(start_request(local.quests, store), store)
        envelope = finish_request(store)
        command, identifier = lambda: local.quests.finish(envelope, store), 1605
    return SimpleNamespace(store=store, command=command, identifier=identifier)


KINDS = ("login", "training", "gift", "first_awake", "second_awake", "quest")


@pytest.mark.parametrize("kind", KINDS)
def test_success_and_exact_retry_count_once_in_the_same_transaction(local, tmp_path, kind):
    case = scenario(local, tmp_path, kind)
    before = case.store.profile()
    response = case.command()
    after = case.store.profile()
    assert after.revision == before.revision + 1
    assert progress(local, case.store, case.identifier)["Progress"] == 1
    assert case.command() == response
    assert case.store.profile() == after


def mission_record_unavailable(*args, **kwargs):
    raise ProtocolError("mission_test_unavailable", "Mission response cannot be encoded", 503)


@pytest.mark.parametrize("kind", [kind for kind in KINDS if kind != "login"])
def test_failed_mission_preflight_rolls_back_the_original_operation(local, tmp_path, monkeypatch, kind):
    case = scenario(local, tmp_path, kind)
    before = case.store.profile()
    monkeypatch.setattr(local.missions, "record", mission_record_unavailable)
    with pytest.raises(ProtocolError, match="Mission response cannot be encoded"):
        case.command()
    assert case.store.profile() == before


def test_failed_mission_preflight_answers_login_with_stored_values(local, tmp_path, monkeypatch):
    case = scenario(local, tmp_path, "login")
    before = case.store.profile()
    local.bootstrap.startup_diagnostics = StartupDiagnostics()
    monkeypatch.setattr(local.missions, "record", mission_record_unavailable)
    reply = loads(local.bootstrap.handlers()[114](request(case.store, 114, ()), case.store))
    assert [group.name for group in reply.children] == ["SYSTEM", "LGI"]
    assert case.store.profile() == before
    load = local.bootstrap.startup_diagnostics.status()["loads"]["114"]
    assert load["outcome"] == "degraded"
    assert [issue["code"] for issue in load["issues"]] == ["mission_test_unavailable"]


@pytest.mark.parametrize("kind", KINDS)
def test_cas_retry_recomputes_progress_without_double_counting(local, tmp_path, monkeypatch, kind):
    case = scenario(local, tmp_path, kind)
    original = case.store.update_state
    attempted = []

    def retry_once(state, revision):
        attempted.append(deepcopy(state["mission_state"]))
        if len(attempted) == 1:
            raise ValueError("Concurrent state mutation")
        return original(state, revision)

    monkeypatch.setattr(case.store, "update_state", retry_once)
    case.command()
    assert len(attempted) == 2 and attempted[0] == attempted[1]
    assert progress(local, case.store, case.identifier)["Progress"] == 1


def test_repeated_login_same_day_does_not_write_or_reset_other_progress(local, tmp_path):
    case = scenario(local, tmp_path, "login")
    case.command()
    before = case.store.profile()
    local.bootstrap.clock = lambda: NOW + timedelta(hours=1)
    local.bootstrap.login(request(case.store, 114, (), tick=2000), case.store)
    assert case.store.profile() == before


def test_cli_wires_all_existing_success_paths_to_one_mission_service(bundle):
    assert bundle.missions is not None, bundle.mission_error
    assert all(service.missions is bundle.missions for service in
               (bundle.bootstrap, bundle.training, bundle.gifts, bundle.evolution, bundle.quests))
    assert set(bundle.missions.handlers()) == {54, 58, 59, 60}
    assert bundle.missions.progress.dgp_login is True


def test_verified_pc_login_alone_enables_the_dgp_task(bundle, tmp_path, monkeypatch):
    monkeypatch.setattr(bundle.bootstrap, "clock", lambda: NOW)
    state = bundle.initialize(bundle.bootstrap.initial_state())
    store = Store(tmp_path / "dgp.sqlite3", state)
    bundle.bootstrap.login(request(store, 114, ()), store)
    rows = {row["ID"]: row for row in bundle.missions.progress.snapshot(store.profile().state, NOW)}
    assert rows[1031]["Status"] == 1


def test_unknown_client_does_not_inherit_verified_dgp_capability(bundle, monkeypatch):
    monkeypatch.setattr(cli, "client_fingerprint", lambda path: "0" * 64)
    args = SimpleNamespace(asset_dir=cli.DEFAULT_ASSETS, data_dir=cli.DEFAULT_DATA,
                           resource_dir=cli.DEFAULT_RESOURCES, rules=None)
    services = cli.services(args)
    assert services.missions is not None, services.mission_error
    assert services.missions.progress.dgp_login is False


def test_unavailable_catalog_never_discards_an_existing_mission_ledger(local):
    services = copy(local.bundle)
    state = services.initialize(local.bootstrap.initial_state())
    services.missions = None
    with pytest.raises(ProtocolError, match="Existing mission history"):
        services.initialize(state)
