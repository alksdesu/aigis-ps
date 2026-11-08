from argparse import Namespace
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from types import MappingProxyType, SimpleNamespace

import pytest

from aigis_ps import cli
from aigis_ps.admin_app import BACKUP_DIRECTORY
from aigis_ps.admin_command import NoopPreflight, NoopRestartCheck
from aigis_ps.admin_commit import InitializerRestartCheck
from aigis_ps.admin_config_switch import ConfigSwitch, ContentInputs, config_switch
from aigis_ps.admin_offline import CROSS_ACCOUNT_PHRASE
from aigis_ps.admin_preflight import GenerationPreflight
from aigis_ps.admin_security import access_path, new_token, write_access_file
from aigis_ps.app import DATABASE_NAME, create_app
from aigis_ps.config_registry import REGISTRY, ConfigError
from aigis_ps.config_sets import ConfigSets, resolve
from aigis_ps.errors import ProtocolError
from aigis_ps.generation import Generation, GenerationIdentity, GenerationInputs
from aigis_ps.startup_diagnostics import StartupDiagnostics, StartupLoad
from aigis_ps.storage import AUTO_BACKUP_KIND, Store
from aigis_ps.tower_state import DOMAIN as TOWER_STATE
from test_admin_offline import clocked
from test_quest_service import bundle


PROJECT = Path(__file__).resolve().parents[1]
GAME_PORT, ADMIN_PORT = cli.DEFAULT_PORT, 18104
ADMIN_FORWARDING = ("if (-not ($Check -or $NoAdmin)) "
                    "{ $serverArguments += @('--admin-port', $AdminPort, '--admin-host', $AdminHost) }")
ARGUMENT_ERROR = 2
EMPTY = b"<DA></DA>"
SHARED, UNIQUE, STARTUP = 1000, 2000, cli.NATIVE_STARTUP_LOADS[0]
REFERENCE_BASE = ("bootstrap", "session_stubs", "summon_schedule", "player_state", "unit_actions", "filters",
               "presents", "peruse", "peruse_episodes")
REFERENCE_OPTIONAL = ("quests", "quest_records", "entrust", "gifts", "training", "fairy_farm", "sales", "silver_house",
                   "evolution", "unit_detail", "orb_exchange", "guide", "barracks", "recovery", "trades", "tower_trades",
                   "towers", "missions", "blessings", "skin_shop", "universal_flags", "fame", "backgrounds",
                   "login_rewards", "rank_rewards", "current_squad", "sugoroku", "view_events", "event_passes", "votes",
                   "packages", "serial_codes")
ABSENT = ("towers", "votes")
STARTUP_OWNERS = ("bootstrap", "quests")
REFERENCE_REFUSALS = (
    "Required bootstrap resources or starter rules are invalid; run doctor for details",
    "Required player-state resources are invalid; run doctor for details",
    "Required item definitions are invalid; run doctor for details",
    "Required present templates are invalid; run doctor for details",
    "Asset directory does not contain the verified original client version",
    "The effective native resource view is not ready; run doctor for details",
    "Required startup data is unavailable; run doctor for details",
)
GATES = ("bootstrap", "player_state", "possessions", "presents", "client", "native_cdn", "startup")
EXPECTED_CLIENT, OTHER_CLIENT = "c" * 64, "d" * 64
UNKNOWN_SNAPSHOT, UNKNOWN_SNAPSHOT_CODE = "0" * 24, "masterdata_snapshot_unknown"
TRADE_KEY, NEVER_RESET = "trade-rules", {"reset_type_1": "never"}
STOPPED = "Stopped after the generation was built"
NOOP_REFUSAL = "Admin writes need the real preflight and restart checks"
NOT_STARTED = "The admin console did not start; see the log above"
FOREIGN_PLAN = "The restore plan names the database of another data directory"
PLAN_OUTPUT = "Restore plans require a separate JSON output file"
CONFIRMATION_REQUIRED = "admin_restore_confirmation_required"
BROKEN_POINTER, POINTER_INVALID = b'{"set": "broken"}', "config_pointer_invalid"
ADMIN_REPORT = frozenset(("ui_installed", "ui_pages", "preflight_noop", "restart_check_noop", "console"))
UNREADABLE_CONSOLE = {"error": "admin_access_unreadable"}
POLL_SECONDS = 0.01


class Service:
    def __init__(self, *operations):
        self.operations, self.startup_diagnostics = operations, None

    def handlers(self):
        return dict.fromkeys(self.operations, self.answer)

    def answer(self, envelope, store):
        return EMPTY


class AdminConfig:
    def __init__(self, app, **options):
        self.app, self.options = app, options


class AdminServer:
    def __init__(self, config, starts):
        self.config, self.starts = config, starts
        self.started = self.should_exit = False
        self.sockets = self.thread = None

    def run(self, sockets=None):
        self.sockets, self.thread = sockets, threading.current_thread()
        self.started = self.starts
        while self.started and not self.should_exit:
            time.sleep(POLL_SECONDS)


class AdminServers:
    def __init__(self):
        self.starts, self.created = True, []

    def __call__(self, config):
        self.created.append(AdminServer(config, self.starts))
        return self.created[-1]


class RecordingPreflight:
    is_noop = False
    domains = MappingProxyType({"wallet": None})

    def __init__(self):
        self.checked = []

    def check(self, before, candidate, domains, generation):
        self.checked.append((before, deepcopy(candidate), domains, generation.id))


@pytest.fixture
def admin_servers(monkeypatch):
    servers = AdminServers()
    monkeypatch.setattr("uvicorn.Config", AdminConfig)
    monkeypatch.setattr("uvicorn.Server", servers)
    return servers


@pytest.fixture
def saved(tmp_path):
    data = tmp_path / "server"
    store = Store(data / DATABASE_NAME, clocked("backup"))
    backup = Path(store.backup(data / BACKUP_DIRECTORY, "manual snapshot", kind="manual")["path"])
    store.update_state(clocked("current"), store.profile().revision)
    return data, store, backup


def unexpected(*arguments, **keywords):
    raise AssertionError("This step must not run")


def free_port():
    with socket.socket() as probe:
        probe.bind((cli.DEFAULT_HOST, 0))
        return probe.getsockname()[1]


def defaults(data):
    return Namespace(data_dir=data, **{spec.dest: None for spec in REGISTRY.values()})


def missing_inputs(tmp_path, diagnostics):
    data = tmp_path / "server"
    return GenerationInputs(resolve(defaults(data)), data, tmp_path / "client", tmp_path / "resources",
                            cli.DEFAULT_HOST, cli.DEFAULT_PORT, diagnostics)


def reference_handlers(source):
    # Kept apart from cli so a change to the served wiring cannot also change the wiring it is compared with.
    handlers = {}
    for name in REFERENCE_BASE:
        handlers |= getattr(source, name).handlers()
    for name in REFERENCE_OPTIONAL:
        if getattr(source, name):
            handlers.update(getattr(source, name).handlers())
    for operation in cli.NATIVE_STARTUP_LOADS:
        if operation in handlers:
            handlers[operation] = source.bootstrap.startup_diagnostics.wrap(operation, handlers[operation])
    return handlers


def same(left, right):
    if isinstance(left, StartupLoad) or isinstance(right, StartupLoad):
        return (type(left) is type(right) and left.diagnostics is right.diagnostics
                and left.operation == right.operation and same(left.handler, right.handler))
    return left == right


def synthetic_bundle():
    services = {}
    for index, name in enumerate(REFERENCE_BASE + REFERENCE_OPTIONAL):
        operations = (SHARED, UNIQUE + index) + ((STARTUP,) if name in STARTUP_OWNERS else ())
        services[name] = None if name in ABSENT else Service(*operations)
    services["bootstrap"].startup_diagnostics = StartupDiagnostics()
    return SimpleNamespace(**services)


def gated(refused):
    def issues(name):
        return ["fixture"] if name in refused else []

    source = SimpleNamespace(bootstrap=SimpleNamespace(status=lambda: {"unavailable": issues("bootstrap")}),
                             player_state=SimpleNamespace(issues=issues("player_state")),
                             possessions=SimpleNamespace(issues=issues("possessions")),
                             presents=SimpleNamespace(issues=issues("presents")),
                             masters=SimpleNamespace(expected_hash=EXPECTED_CLIENT),
                             native_cdn=SimpleNamespace(status=lambda: {"ready": "native_cdn" not in refused}))
    # Callers may replace service_readiness, so the gate reads nothing but startup_ready from it.
    readiness = {"startup_ready": "startup" not in refused}
    return cli.serve_gate(source, OTHER_CLIENT if "client" in refused else EXPECTED_CLIENT, readiness)


def synthetic_builder(built, *, gate_issues=()):
    def build(inputs, progress=None):
        bootstrap = SimpleNamespace(initial_state=dict, startup_diagnostics=inputs.diagnostics)
        source = SimpleNamespace(bootstrap=bootstrap, initialize=dict, native_cdn=SimpleNamespace(assets=None))
        identity = GenerationIdentity(code_digest="c" * 64, config_set=inputs.config_digest, content_versions={})
        generation = Generation(identity, source, {}, {"startup_ready": True}, gate_issues,
                                diagnostics=inputs.diagnostics)
        built.append((inputs, generation))
        return generation

    return build


def recorded_apps(monkeypatch):
    calls = []

    def recording(data_dir, **options):
        calls.append(options)
        return create_app(data_dir, **options)

    monkeypatch.setattr("aigis_ps.app.create_app", recording)
    return calls


def restore_checks(monkeypatch, built):
    preflight = RecordingPreflight()
    monkeypatch.setattr(cli, "admin_checks", lambda registry: (preflight, InitializerRestartCheck()))
    monkeypatch.setattr(cli, "build_generation", synthetic_builder(built))
    return preflight


def restore_plan(data, backup, output):
    return cli.main(["admin-restore-plan", "--data-dir", str(data), "--backup", str(backup), "--output", str(output)])


def run_doctor(tmp_path, data, *options):
    return cli.main(["doctor", "--data-dir", str(data), "--asset-dir", str(tmp_path / "client"), *options])


def test_assembled_handlers_reproduce_the_reference_wiring():
    synthetic = synthetic_bundle()
    assembled, reference = cli.assemble_handlers(synthetic), reference_handlers(synthetic)
    assert cli.HANDLER_SERVICES == REFERENCE_BASE + REFERENCE_OPTIONAL
    assert list(assembled) == list(reference)
    assert all(same(assembled[operation], reference[operation]) for operation in reference)
    assert assembled[SHARED].__self__ is synthetic.serial_codes
    assert isinstance(assembled[STARTUP], StartupLoad) and assembled[STARTUP].handler.__self__ is synthetic.quests
    assert assembled[STARTUP].diagnostics is synthetic.bootstrap.startup_diagnostics
    order = REFERENCE_BASE + REFERENCE_OPTIONAL
    assert not any(UNIQUE + order.index(name) in assembled for name in ABSENT)


@pytest.mark.parametrize("index", range(len(GATES)))
def test_each_serve_gate_refuses_with_its_own_message(index):
    assert gated({GATES[index]}) == (REFERENCE_REFUSALS[index],)


def test_serve_gate_lists_every_refusal_in_the_reference_order():
    assert gated(set(GATES)) == REFERENCE_REFUSALS
    assert gated(set()) == ()


def test_content_versions_keep_every_nested_catalog_version_under_its_path():
    readiness = {"quests": {"content_version": "q1", "catalog": {"content_version": "c1"},
                            "empty": {"content_version": ""}},
                 "votes": {"content_version": 3}, "presents": {"ready": True}, "startup_ready": True}
    assert cli.content_versions(readiness) == {"quests": "q1", "quests.catalog": "c1"}


def test_build_generation_shares_the_process_startup_diagnostics(tmp_path):
    diagnostics, progress = StartupDiagnostics(), []
    inputs = missing_inputs(tmp_path, diagnostics)
    generation = cli.build_generation(inputs, progress.append)
    loads = [handler for handler in generation.handlers.values() if isinstance(handler, StartupLoad)]
    assert generation.bundle.bootstrap.startup_diagnostics is diagnostics is generation.diagnostics
    assert loads and all(load.diagnostics is diagnostics for load in loads)
    identity = generation.identity
    assert (identity.config_set, identity.master_snapshot) == (inputs.config_digest, None)
    assert (identity.base_resource_version, identity.effective_resource_version) == (None, None)
    assert generation.gate_issues[0] == REFERENCE_REFUSALS[0] and progress
    assert REFERENCE_REFUSALS[GATES.index("native_cdn")] in generation.gate_issues
    assert set(generation.readiness) == set(cli.service_readiness(generation.bundle))
    assert not inputs.data_dir.exists()


def test_content_inputs_pin_the_build_to_their_master_snapshot(tmp_path, monkeypatch):
    plain, seen, services = missing_inputs(tmp_path, StartupDiagnostics()), [], cli.services

    def recording(namespace):
        seen.append(namespace)
        return services(namespace)

    monkeypatch.setattr(cli, "services", recording)
    with pytest.raises(ProtocolError) as caught:
        cli.build_generation(ContentInputs.pinned(plain, config=plain.config, master_snapshot=UNKNOWN_SNAPSHOT))
    assert caught.value.code == UNKNOWN_SNAPSHOT_CODE
    assert cli.build_generation(plain).identity.master_snapshot is None
    assert [namespace.master_snapshot for namespace in seen] == [UNKNOWN_SNAPSHOT, None]
    assert all(namespace.startup_diagnostics is plain.diagnostics for namespace in seen)


def test_serve_without_an_admin_port_serves_one_fixed_generation(tmp_path, monkeypatch, admin_servers):
    data, built = tmp_path / "server", []
    calls = recorded_apps(monkeypatch)
    monkeypatch.setattr(cli, "build_generation", synthetic_builder(built))
    monkeypatch.setattr("uvicorn.run", lambda application, **options: None)
    assert cli.main(["serve", "--data-dir", str(data)]) == 0
    [(_, generation)], [options] = built, calls
    assert "runtime" not in options and options["handlers"] is generation.handlers
    assert options["status_provider"] == generation.live_status and options["state_initializer"] is dict
    assert admin_servers.created == [] and not access_path(data).exists()


@pytest.mark.parametrize("state,code,status,phrase", [
    ("stop_service_required", "energy_migration_database_busy", 409, "journal files or open connections"),
    ("migration_required", "energy_clock_source_required", 503, "energy-clock-plan"),
    ("invalid", "energy_clock_invalid", 503, "startup validation"),
    ("invalid", "energy_clock_anchor_invalid", 503, "startup validation"),
    ("invalid", "energy_migration_database_invalid", 503, "startup validation"),
    ("invalid", "energy_migration_stale", 503, "startup validation"),
])
def test_serve_reports_exact_saved_database_refusal_before_startup(tmp_path, monkeypatch, capsys, state, code, status, phrase):
    monkeypatch.setattr("aigis_ps.energy_migration.inspect_database",
                        lambda path: {"ready": False, "exists": True, "status": state, "error": code})
    monkeypatch.setattr(cli, "build_generation", unexpected)
    monkeypatch.setattr(cli, "loopback_listener", unexpected)
    assert cli.main(["serve", "--data-dir", str(tmp_path / "server")]) == 1
    result = json.loads(capsys.readouterr().out)
    assert (result["error"], result["code"], result["status"]) == ("ProtocolError", code, status)
    assert phrase in result["message"]
    assert ("energy-clock-plan" in result["message"]) is (state == "migration_required")
    assert not (tmp_path / "server").exists()


def test_serve_runs_the_admin_console_beside_the_game_and_stops_it(tmp_path, monkeypatch, capsys, admin_servers):
    data, port, built, seen = tmp_path / "server", free_port(), [], {}
    calls = recorded_apps(monkeypatch)
    monkeypatch.setattr(cli, "build_generation", synthetic_builder(built))

    def run(application, **options):
        server = admin_servers.created[0]
        seen.update(game=application, options=options, alive=server.thread.is_alive(),
                    bound=server.sockets[0].getsockname()[:2], access=json.loads(access_path(data).read_bytes()))

    monkeypatch.setattr("uvicorn.run", run)
    assert cli.main(["serve", "--data-dir", str(data), "--admin-port", str(port)]) == 0
    [server], [(_, generation)], [options] = admin_servers.created, built, calls
    admin = server.config.app.state.admin
    assert seen["options"] == {"host": cli.DEFAULT_HOST, "port": GAME_PORT, "access_log": False}
    assert seen["alive"] and seen["bound"] == (cli.DEFAULT_HOST, port)
    assert server.config.options == {"access_log": False, "log_config": None, "proxy_headers": False}
    assert (server.thread.name, server.thread.daemon) == (cli.ADMIN_THREAD, True)
    assert "handlers" not in options and admin.runtime is options["runtime"] and admin.runtime.current is generation
    assert admin.store is seen["game"].state.store
    assert (admin.endpoint.port, admin.endpoint.game_port) == (port, GAME_PORT)
    assert isinstance(admin.preflight, GenerationPreflight) and isinstance(admin.restart_check, InitializerRestartCheck)
    assert isinstance(config_switch(server.config.app), ConfigSwitch)
    assert (seen["access"]["admin_port"], seen["access"]["pid"]) == (port, os.getpid())
    assert f"Admin console: {seen['access']['url']}" in capsys.readouterr().out
    assert server.should_exit and not server.thread.is_alive() and server.sockets[0].fileno() == -1
    assert not access_path(data).exists()


def test_an_admin_port_in_use_stops_serve_before_building(tmp_path, monkeypatch, capsys):
    data = tmp_path / "server"
    monkeypatch.setattr(cli, "build_generation", unexpected)
    with socket.socket() as occupied:
        occupied.bind((cli.DEFAULT_HOST, 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        assert cli.main(["serve", "--data-dir", str(data), "--admin-port", str(port)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "ValueError"
    assert result["message"].startswith(f"The admin console cannot listen on {cli.DEFAULT_HOST} port {port}")
    assert not data.exists()


@pytest.mark.parametrize("checks", [(NoopPreflight, InitializerRestartCheck), (GenerationPreflight, NoopRestartCheck)])
def test_serve_refuses_an_admin_console_without_the_real_checks(tmp_path, monkeypatch, capsys, checks):
    monkeypatch.setattr(cli, "admin_checks", lambda registry: tuple(check() for check in checks))
    monkeypatch.setattr(cli, "build_generation", unexpected)
    assert cli.main(["serve", "--data-dir", str(tmp_path / "server"), "--admin-port", str(free_port())]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "ValueError", "message": NOOP_REFUSAL}


def test_an_admin_console_that_never_starts_stops_serve(tmp_path, monkeypatch, capsys, admin_servers):
    data = tmp_path / "server"
    admin_servers.starts = False
    monkeypatch.setattr(cli, "build_generation", synthetic_builder([]))
    monkeypatch.setattr("uvicorn.run", unexpected)
    assert cli.main(["serve", "--data-dir", str(data), "--admin-port", str(free_port())]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "ValueError", "message": NOT_STARTED}
    [server] = admin_servers.created
    assert server.should_exit and not server.thread.is_alive() and server.sockets[0].fileno() == -1
    assert not access_path(data).exists()


@pytest.mark.parametrize("options", [
    ["--admin-host", cli.DEFAULT_HOST], ["--admin-port", str(GAME_PORT)], ["--admin-port", "0"],
    ["--admin-port", str(cli.MAX_PORT + 1)], ["--admin-port", str(ADMIN_PORT), "--admin-host", "0.0.0.0"]])
def test_admin_console_arguments_are_refused_before_serving(tmp_path, monkeypatch, options):
    monkeypatch.setattr(cli, "serve", unexpected)
    with pytest.raises(SystemExit) as caught:
        cli.main(["serve", "--data-dir", str(tmp_path / "server"), *options])
    assert caught.value.code == ARGUMENT_ERROR


def test_start_script_runs_the_admin_console_unless_disabled_or_checking():
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell parser is unavailable")
    path = PROJECT / "start-server.ps1"
    script = ("$tokens=$null; $errors=$null; "
        "$ast=[System.Management.Automation.Language.Parser]::ParseFile('" + str(path).replace("'", "''")
        + "',[ref]$tokens,[ref]$errors); "
        "if($errors.Count){throw 'PowerShell parse failed'}; "
        "$admin=@{}; foreach($p in $ast.ParamBlock.Parameters){ $name=$p.Name.VariablePath.UserPath; "
        "if($name -like 'Admin*' -or $name -eq 'NoAdmin'){ $admin[$name]=@{type=$p.StaticType.Name; "
        "default=$(if($p.DefaultValue){$p.DefaultValue.SafeGetValue()}else{$null})} } }; "
        "$forward=@($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] "
        "-and $n.Value -eq '--admin-port'},$true)).Count; "
        "@{admin=$admin; forward=$forward} | ConvertTo-Json -Compress -Depth 4")
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", script],
                            check=True, capture_output=True, text=True, timeout=30)
    assert json.loads(result.stdout) == {"admin": {"AdminPort": {"default": ADMIN_PORT, "type": "Int32"},
                                                   "AdminHost": {"default": cli.DEFAULT_HOST, "type": "String"},
                                                   "NoAdmin": {"default": None, "type": "SwitchParameter"}},
                                         "forward": 1}
    assert ADMIN_FORWARDING in path.read_text("utf-8")


def test_serve_rolls_a_committed_switch_forward_before_resolving_its_configuration(tmp_path, monkeypatch, capsys):
    data, built = tmp_path / "server", []
    store = Store(data / DATABASE_NAME, clocked("saved"))
    sets = ConfigSets(data)
    switched = sets.create({TRADE_KEY: NEVER_RESET}, source="console")
    store.record_generation(switched.identifier, "hot")
    monkeypatch.setattr(cli, "build_generation", synthetic_builder(built, gate_issues=(STOPPED,)))
    assert cli.main(["serve", "--data-dir", str(data)]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "ValueError", "message": STOPPED}
    [(inputs, _)] = built
    assert sets.read_active().set_id == inputs.config.config_set.identifier == switched.identifier
    assert inputs.config.arguments() == resolve(defaults(data)).arguments()
    assert inputs.config.arguments()[REGISTRY[TRADE_KEY].dest] is not None


def test_serve_without_a_saved_database_resolves_without_rolling_forward(tmp_path, monkeypatch, capsys):
    data, built = tmp_path / "server", []
    monkeypatch.setattr("aigis_ps.admin_config_switch.roll_forward", unexpected)
    monkeypatch.setattr(cli, "build_generation", synthetic_builder(built, gate_issues=(STOPPED,)))
    assert cli.main(["serve", "--data-dir", str(data)]) == 1
    assert json.loads(capsys.readouterr().out)["message"] == STOPPED
    [(inputs, _)] = built
    assert inputs.config.config_set is None and not data.exists()


def test_offline_restore_rolls_a_committed_switch_forward_before_building(saved, tmp_path, monkeypatch, capsys):
    (data, store, backup), built = saved, []
    restore_checks(monkeypatch, built)
    sets = ConfigSets(data)
    switched = sets.create({TRADE_KEY: NEVER_RESET}, source="console")
    store.record_generation(switched.identifier, "hot")
    assert restore_plan(data, backup, tmp_path / "restore.json") == 0
    assert json.loads(capsys.readouterr().out)["plan"]["expected_revision"] == store.profile().revision
    [(inputs, _)] = built
    assert sets.read_active().set_id == inputs.config.config_set.identifier == switched.identifier
    assert inputs.config.arguments() == resolve(defaults(data)).arguments()


def test_offline_restore_plans_and_applies_a_backup_through_the_commands(saved, tmp_path, monkeypatch, capsys):
    (data, store, backup), built = saved, []
    preflight, plan_path = restore_checks(monkeypatch, built), tmp_path / "plans" / "restore.json"
    assert restore_plan(data, backup, plan_path) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["plan_path"] == str(plan_path.resolve()) and json.loads(plan_path.read_bytes()) == planned["plan"]
    assert cli.main(["admin-restore-apply", "--data-dir", str(data), "--plan", str(plan_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"
    assert store.profile().state == clocked("backup") and len(built) == 2
    assert [item["kind"] for item in store.list_backups().items].count(AUTO_BACKUP_KIND) == 1
    assert preflight.checked and all(before == {} and candidate == clocked("backup") and domains == {"wallet"}
                                     for before, candidate, domains, _ in preflight.checked)


def test_offline_restore_refuses_plans_that_cannot_be_kept_apart_or_name_another_database(
        saved, tmp_path, monkeypatch, capsys):
    data, store, backup = saved
    restore_checks(monkeypatch, [])
    revision, plan_path, other = store.profile().revision, tmp_path / "restore.json", tmp_path / "other"
    assert restore_plan(data, backup, tmp_path / "restore.txt") == 1
    assert json.loads(capsys.readouterr().out)["message"] == PLAN_OUTPUT
    assert restore_plan(data, backup, plan_path) == 0
    capsys.readouterr()
    Store(other / DATABASE_NAME, clocked("other"))
    assert cli.main(["admin-restore-apply", "--data-dir", str(other), "--plan", str(plan_path)]) == 1
    assert json.loads(capsys.readouterr().out)["message"] == FOREIGN_PLAN
    assert store.profile().revision == revision and not (other / BACKUP_DIRECTORY).exists()


def test_offline_restore_forwards_the_confirmation_phrase_for_another_account(saved, tmp_path, monkeypatch, capsys):
    data, store, _ = saved
    restore_checks(monkeypatch, [])
    other = Store(data / "other" / DATABASE_NAME, clocked("other account"))
    with sqlite3.connect(other.path) as connection:
        connection.execute("UPDATE profiles SET player_id='2' WHERE id=1")
    connection.close()
    backup = Path(other.backup(data / "other" / BACKUP_DIRECTORY, "other account", kind="manual")["path"])
    plan_path = tmp_path / "restore.json"
    assert restore_plan(data, backup, plan_path) == 0
    assert json.loads(capsys.readouterr().out)["plan"]["cross_account"] is True
    command = ["admin-restore-apply", "--data-dir", str(data), "--plan", str(plan_path)]
    assert cli.main(command) == 1
    refused = json.loads(capsys.readouterr().out)
    assert refused["error"] == ProtocolError.__name__ and CROSS_ACCOUNT_PHRASE in refused["message"]
    assert (refused["code"], refused["status"]) == (CONFIRMATION_REQUIRED, 409)
    assert cli.main([*command, "--confirm", CROSS_ACCOUNT_PHRASE]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"
    assert store.profile().state == clocked("other account") and store.profile().player_id == "1"


def test_doctor_reports_the_configuration_set_and_the_admin_console_without_its_token(tmp_path, capsys):
    data, token = tmp_path / "server", new_token()
    sets = ConfigSets(data)
    created = sets.create({TRADE_KEY: NEVER_RESET}, source="console")
    sets.activate(created.identifier, 1, expected=sets.pointer_digest())
    write_access_file(data, token=token, host=cli.DEFAULT_HOST, port=ADMIN_PORT)
    assert run_doctor(tmp_path, data) == 0
    printed = capsys.readouterr().out
    report = json.loads(printed)
    assert token not in printed
    assert report["config"] == resolve(defaults(data)).describe() and report["config"]["active"] is not None
    assert set(report["admin"]) == ADMIN_REPORT
    assert (report["admin"]["preflight_noop"], report["admin"]["restart_check_noop"]) == (False, False)
    console = report["admin"]["console"]
    assert set(console) == set(cli.RECORDED_CONSOLE_FIELDS)
    assert (console["admin_host"], console["admin_port"], console["pid"]) == (cli.DEFAULT_HOST, ADMIN_PORT, os.getpid())


def test_doctor_reports_a_missing_or_unreadable_console_record_without_failing(tmp_path, capsys):
    data = tmp_path / "server"
    assert run_doctor(tmp_path, data) == 0
    assert json.loads(capsys.readouterr().out)["admin"]["console"] is None
    access_path(data).parent.mkdir(parents=True)
    access_path(data).write_bytes(b"{unreadable")
    assert run_doctor(tmp_path, data) == 0
    assert json.loads(capsys.readouterr().out)["admin"]["console"] == UNREADABLE_CONSOLE


def test_doctor_reports_an_unreadable_configuration_pointer_that_serve_refuses(tmp_path, monkeypatch, capsys):
    data = tmp_path / "server"
    sets = ConfigSets(data)
    sets.root.mkdir(parents=True)
    sets.active.write_bytes(BROKEN_POINTER)
    assert run_doctor(tmp_path, data) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["config"]["error"]["code"] == POINTER_INVALID and report["readiness"]["startup_ready"] is False
    monkeypatch.setattr(cli, "build_generation", unexpected)
    assert cli.main(["serve", "--data-dir", str(data)]) == 1
    refused = json.loads(capsys.readouterr().out)
    assert (refused["error"], refused["code"], refused["status"]) == (ConfigError.__name__, POINTER_INVALID, 503)


def test_doctor_reports_an_unreadable_override_file_with_its_loader_error(tmp_path, capsys):
    data, missing = tmp_path / "server", tmp_path / "absent.json"
    with pytest.raises((OSError, ValueError)) as expected:
        cli.load_entrust_rules(missing)
    assert run_doctor(tmp_path, data, "--entrust-rules", str(missing)) == 1
    assert json.loads(capsys.readouterr().out) == {"error": type(expected.value).__name__, "message": str(expected.value)}
    assert not data.exists()


def test_serve_wires_the_reference_handlers_readiness_and_initializer_with_and_without_the_console(
        bundle, tmp_path, monkeypatch, admin_servers):
    monkeypatch.setattr(cli, "services", lambda namespace: bundle)
    monkeypatch.setattr(cli, "client_fingerprint", lambda directory: bundle.masters.expected_hash)
    monkeypatch.setattr("uvicorn.run", lambda application, **options: None)
    calls = recorded_apps(monkeypatch)
    assert cli.main(["serve", "--data-dir", str(tmp_path / "fixed")]) == 0
    assert cli.main(["serve", "--data-dir", str(tmp_path / "console"), "--admin-port", str(free_port())]) == 0
    [fixed, console], reference = calls, reference_handlers(bundle)
    current = console["runtime"].current
    for handlers in (fixed["handlers"], current.handlers):
        assert list(handlers) == list(reference)
        assert all(same(handlers[operation], reference[operation]) for operation in reference)
    assert set(fixed["status_provider"]()) == set(current.live_status()) == set(cli.service_readiness(bundle))
    assert fixed["state_initializer"] == console["state_initializer"] == bundle.initialize
    assert (current.identity.base_resource_version, current.identity.effective_resource_version) == (
        bundle.native_cdn.base_version, bundle.native_cdn.version)
    assert len(admin_servers.created) == 1 and not access_path(tmp_path / "console").exists()


def test_a_save_needing_an_unavailable_service_is_refused_before_the_console_starts(
        bundle, tmp_path, monkeypatch, capsys, admin_servers):
    data = tmp_path / "server"
    progress = {**bundle.initialize(bundle.bootstrap.initial_state()), TOWER_STATE: {}}
    Store(data / DATABASE_NAME, progress)
    unavailable = replace(bundle, towers=None)
    with pytest.raises(ProtocolError) as expected:
        unavailable.initialize(deepcopy(progress))
    monkeypatch.setattr(cli, "services", lambda namespace: unavailable)
    monkeypatch.setattr(cli, "client_fingerprint", lambda directory: bundle.masters.expected_hash)
    monkeypatch.setattr("uvicorn.run", unexpected)
    assert cli.main(["serve", "--data-dir", str(data), "--admin-port", str(free_port())]) == 1
    assert (expected.value.code, expected.value.status) == ("tower_service_unavailable", 503)
    assert json.loads(capsys.readouterr().out) == {"error": type(expected.value).__name__, "code": expected.value.code,
                                                   "status": expected.value.status, "message": str(expected.value)}
    assert admin_servers.created == [] and not access_path(data).exists()
