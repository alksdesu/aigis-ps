from contextlib import contextmanager, nullcontext
import io
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from aigis_ps import cli, desktop_worker
from aigis_ps.config_sets import ResolvedConfig
from aigis_ps.errors import ProtocolError


FAILURE_RECORD = 'AIGIS_DESKTOP_ERROR {"schema_version":1,"code":"desktop_worker_failed"}\n'


@pytest.fixture
def worker_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_worker, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setenv("AIGIS_DESKTOP_ADMIN_TOKEN", "w" * 43)
    values = []
    for option in ("data-dir", "asset-dir", "resource-dir"):
        path = tmp_path / "project/runtime/server" if option == "data-dir" else tmp_path / option
        path.mkdir(parents=True)
        values.extend(("--" + option, str(path)))
    return [*values, "--port", str(cli.DEFAULT_PORT), "--admin-port", "18104"]


def test_worker_uses_cli_serve_defaults(worker_argv):
    actual = desktop_worker.arguments(worker_argv)
    expected = cli.build_parser().parse_args(["serve", *worker_argv])
    assert vars(actual) == vars(expected)
    assert actual.host == cli.DEFAULT_HOST
    assert actual.rules is None and actual.asset_manifest is None


def test_worker_accepts_separate_save_directory(worker_argv, tmp_path):
    path = tmp_path / "project/runtime/saves" / ("a" * 32)
    path.mkdir(parents=True)
    worker_argv[1] = str(path)
    assert desktop_worker.arguments(worker_argv).data_dir == path


@pytest.mark.parametrize("name", ["other", "A" * 32, "a" * 31, "a" * 33])
def test_worker_rejects_non_profile_data_directories(worker_argv, tmp_path, name):
    path = tmp_path / "project/runtime/saves" / name
    path.mkdir(parents=True)
    worker_argv[1] = str(path)
    with pytest.raises(ValueError, match="valid save directory"):
        desktop_worker.arguments(worker_argv)


@pytest.mark.parametrize("extra", [
    ["--host", "0.0.0.0"], ["--rules", "other.json"], ["--data", "other"],
    ["--port", "18105"], ["--admin-port", "18103"], ["--admin-port", "65536"],
])
def test_worker_rejects_unapproved_options_and_ports(worker_argv, extra):
    with pytest.raises(SystemExit) as error:
        desktop_worker.arguments([*worker_argv, *extra])
    assert error.value.code == 2


@pytest.mark.parametrize("replacement", ["relative", "outside", "missing", "workspace", "overlap"])
def test_worker_rejects_invalid_data_paths(worker_argv, tmp_path, replacement):
    paths = {"relative": Path("relative"), "outside": tmp_path.parent,
             "missing": tmp_path / "missing", "workspace": tmp_path,
             "overlap": tmp_path / "asset-dir"}
    worker_argv[1] = str(paths[replacement])
    with pytest.raises(ValueError):
        desktop_worker.arguments(worker_argv)


def test_worker_rejects_linked_ancestor(worker_argv, tmp_path, monkeypatch):
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == tmp_path or original(path))
    with pytest.raises(ValueError, match="Links and junctions"):
        desktop_worker.arguments(worker_argv)


@pytest.mark.parametrize("command", ["stop\n", b"stop\n", "", b""])
def test_worker_stops_on_exact_command_or_parent_eof(worker_argv, monkeypatch, command):
    calls = []

    def serve(args, *, stop_event, print_admin_url, admin_token):
        calls.append((args.command, print_admin_url))
        assert admin_token == "w" * 43
        assert "AIGIS_DESKTOP_ADMIN_TOKEN" not in os.environ
        assert stop_event.wait(1)

    monkeypatch.setattr(cli, "serve", serve)
    stream = io.BytesIO(command) if isinstance(command, bytes) else io.StringIO(command)
    assert desktop_worker.main(worker_argv, input_stream=stream) == 0
    assert calls == [("serve", False)]


@pytest.mark.parametrize("command", ["stop", "stop \n", "STOP\n", "stop\r\n", "\n", "x" * 10000])
def test_unknown_input_requests_shutdown_without_echo(worker_argv, monkeypatch, capsys, command):
    monkeypatch.setattr(cli, "serve", lambda args, *, stop_event, print_admin_url, admin_token: stop_event.wait(1))
    stream = io.StringIO(command)
    assert desktop_worker.main(worker_argv, input_stream=stream) == 1
    assert stream.tell() <= 6
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Desktop worker rejected its control input\n"


def test_broken_control_pipe_stops_worker():
    class BrokenPipe:
        def readline(self, limit):
            raise OSError("private control input")

    stop, failed = threading.Event(), threading.Event()
    desktop_worker.read_stop(BrokenPipe(), stop, failed)
    assert stop.is_set() and failed.is_set()


def test_worker_failure_does_not_print_exception_secrets(worker_argv, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise ValueError("http://127.0.0.1/#token=private-value")

    monkeypatch.setattr(cli, "serve", fail)
    assert desktop_worker.main(worker_argv, input_stream=io.BytesIO(b"")) == 1
    assert capsys.readouterr().err == "Desktop worker failed: ValueError: http://127.0.0.1/#token=[redacted]\n" + FAILURE_RECORD


def test_worker_error_keeps_diagnostics_but_redacts_launch_token(worker_argv, monkeypatch, capsys):
    token = os.environ["AIGIS_DESKTOP_ADMIN_TOKEN"]

    def fail(*args, **kwargs):
        raise ValueError(f"Missing resource configuration; credential {token}, repeated {token}")

    monkeypatch.setattr(cli, "serve", fail)
    assert desktop_worker.main(worker_argv, input_stream=io.BytesIO(b"")) == 1
    output = capsys.readouterr()
    assert output.out == "" and token not in output.err
    assert output.err == ("Desktop worker failed: ValueError: Missing resource configuration; "
                          "credential [redacted], repeated [redacted]\n" + FAILURE_RECORD)


def test_worker_reports_environment_failure_before_token_assignment(worker_argv, monkeypatch, capsys):
    class BrokenEnvironment:
        def pop(self, *args):
            raise OSError("Unable to read launch settings")

    monkeypatch.setattr(desktop_worker, "os", SimpleNamespace(environ=BrokenEnvironment()))
    assert desktop_worker.main(worker_argv, input_stream=io.BytesIO(b"")) == 1
    assert capsys.readouterr().err == "Desktop worker failed: OSError: Unable to read launch settings\n" + FAILURE_RECORD


@pytest.mark.parametrize("token", [None, "invalid-secret"])
def test_worker_rejects_missing_or_invalid_launch_token(worker_argv, monkeypatch, capsys, token):
    if token is None:
        monkeypatch.delenv("AIGIS_DESKTOP_ADMIN_TOKEN")
    else:
        monkeypatch.setenv("AIGIS_DESKTOP_ADMIN_TOKEN", token)
    monkeypatch.setattr(cli, "serve", lambda *args, **kwargs: pytest.fail("Invalid launch credential"))
    assert desktop_worker.main(worker_argv, input_stream=io.BytesIO(b"")) == 1
    assert "AIGIS_DESKTOP_ADMIN_TOKEN" not in os.environ
    output = capsys.readouterr().err
    assert output == ("Desktop worker failed: ValueError: Admin tokens are URL-safe random text of at least 32 characters\n"
                      + FAILURE_RECORD)
    assert "invalid-secret" not in output


@pytest.mark.parametrize("code", ["energy_migration_database_busy", "energy_clock_source_required", "energy_clock_invalid"])
def test_worker_emits_code_only_record_after_redacted_diagnostics(worker_argv, monkeypatch, capsys, code):
    token = os.environ["AIGIS_DESKTOP_ADMIN_TOKEN"]

    def fail(*args, **kwargs):
        raise ProtocolError(code, f"Startup check failed with {token}", 503)

    monkeypatch.setattr(cli, "serve", fail)
    assert desktop_worker.main(worker_argv, input_stream=io.BytesIO(b"")) == 1
    output = capsys.readouterr().err
    assert token not in output
    assert output.splitlines() == ["Desktop worker failed: ProtocolError: Startup check failed with [redacted]",
                                  'AIGIS_DESKTOP_ERROR {"schema_version":1,"code":"' + code + '"}']


@pytest.mark.parametrize("code", ["w" * 43, "invalid\ncode", None, {"message": "private"}])
def test_worker_does_not_emit_secret_or_malformed_error_codes(worker_argv, monkeypatch, capsys, code):
    def fail(*args, **kwargs):
        raise ProtocolError(code, "Startup check failed", 503)

    monkeypatch.setattr(cli, "serve", fail)
    assert desktop_worker.main(worker_argv, input_stream=io.BytesIO(b"")) == 1
    assert capsys.readouterr().err == "Desktop worker failed: ProtocolError: Startup check failed\n" + FAILURE_RECORD


def test_admin_console_rejects_invalid_override_before_creation(tmp_path, monkeypatch):
    from aigis_ps import admin_app

    monkeypatch.setattr(admin_app, "create_admin_app", lambda *args, **kwargs: pytest.fail("Invalid credential"))
    console = cli.AdminConsole(cli.DEFAULT_HOST, 18104, cli.DEFAULT_PORT, tmp_path, object(), object())
    with pytest.raises(ValueError, match="Admin tokens"):
        with console.running(object(), object(), object(), object(), token="invalid-secret"):
            pytest.fail("Invalid credential")


@pytest.mark.parametrize("print_url", [True, False])
@pytest.mark.parametrize("override", [None, "o" * 43])
def test_console_token_output_is_optional_and_access_file_is_removed(tmp_path, monkeypatch, capsys, print_url, override):
    from aigis_ps import admin_app, admin_config_switch, admin_security

    actions = []
    token = override or "t" * 43
    monkeypatch.setattr(admin_app, "create_admin_app", lambda *args, **kwargs: object())
    monkeypatch.setattr(admin_config_switch, "install", lambda *args, **kwargs: None)
    monkeypatch.setattr(admin_security, "new_token", lambda: "t" * 43)
    monkeypatch.setattr(admin_security, "write_access_file", lambda *args, **kwargs: actions.append("write"))
    monkeypatch.setattr(admin_security, "remove_access_file", lambda *args: actions.append("remove"))
    monkeypatch.setattr(cli, "background_server", lambda *args: nullcontext())
    console = cli.AdminConsole(cli.DEFAULT_HOST, 18104, cli.DEFAULT_PORT, tmp_path,
                               SimpleNamespace(domains={}), object())
    with pytest.raises(RuntimeError, match="stop"):
        with console.running(object(), object(), object(), object(), print_url=print_url, token=override):
            raise RuntimeError("stop")
    assert actions == ["write", "remove"]
    output = capsys.readouterr().out
    assert (token in output) is print_url
    if not print_url:
        assert output == ""


@pytest.fixture
def fake_serve(monkeypatch, tmp_path):
    from aigis_ps import admin_config_switch, app, energy_migration

    operations = []
    listeners = {}
    args = cli.build_parser().parse_args(["serve", "--data-dir", str(tmp_path), "--admin-port", "18104"])
    bundle = SimpleNamespace(native_cdn=SimpleNamespace(assets=object()), initialize=lambda state: state,
                             bootstrap=SimpleNamespace(initial_state=lambda: {}))
    generation = SimpleNamespace(gate_issues=(), readiness={}, bundle=bundle)
    application = SimpleNamespace(state=SimpleNamespace(store=object()))

    @contextmanager
    def listener(host, port, *, label="admin console"):
        prefix = "game-" if port == cli.DEFAULT_PORT else ""
        operations.append(prefix + "listener-open")
        listeners[port] = object()
        try:
            yield listeners[port]
        finally:
            operations.append(prefix + "listener-closed")

    @contextmanager
    def running(*args, **kwargs):
        operations.append(("console-open", kwargs))
        try:
            yield "private-url"
        finally:
            operations.append("console-closed")

    console = SimpleNamespace(host=cli.DEFAULT_HOST, port=18104, running=running)
    monkeypatch.setattr(cli, "AdminConsole", SimpleNamespace(requested=lambda args: console))
    monkeypatch.setattr(cli, "loopback_listener", listener)
    monkeypatch.setattr(cli, "resolve", lambda args: ResolvedConfig(None, None, {}))
    monkeypatch.setattr(cli, "build_generation", lambda inputs: generation)
    monkeypatch.setattr(cli, "warn_auto_request_gaps", lambda value: None)
    monkeypatch.setattr(cli, "GenerationRuntime", lambda generation: object())
    monkeypatch.setattr(cli, "game_source", lambda *args: {})
    monkeypatch.setattr(energy_migration, "inspect_database", lambda path: {"ready": True, "exists": False})
    monkeypatch.setattr(app, "create_app", lambda *args, **kwargs: application)
    monkeypatch.setattr(admin_config_switch, "roll_forward", lambda *args: pytest.fail("Unexpected database recovery"))
    return SimpleNamespace(args=args, operations=operations, generation=generation, application=application,
                           listeners=listeners)


def test_already_stopped_service_does_not_inspect_database(fake_serve, monkeypatch):
    monkeypatch.setattr("aigis_ps.energy_migration.inspect_database", lambda path: pytest.fail("Database read"))
    stop = threading.Event()
    stop.set()
    cli.serve(fake_serve.args, stop_event=stop, print_admin_url=False)
    assert fake_serve.operations == []


@pytest.mark.parametrize("stage", ["inspect", "generation", "application", "console"])
def test_stopping_during_setup_leaves_no_running_service(fake_serve, monkeypatch, stage):
    stop = threading.Event()

    def stopped(value):
        stop.set()
        return value

    if stage == "inspect":
        monkeypatch.setattr("aigis_ps.energy_migration.inspect_database",
                            lambda path: stopped({"ready": True, "exists": False}))
    elif stage == "generation":
        monkeypatch.setattr(cli, "build_generation", lambda inputs: stopped(fake_serve.generation))
    elif stage == "application":
        monkeypatch.setattr("aigis_ps.app.create_app", lambda *args, **kwargs: stopped(fake_serve.application))
    else:
        @contextmanager
        def running(*args, **kwargs):
            stop.set()
            try:
                yield "private-url"
            finally:
                fake_serve.operations.append("console-closed")

        console = SimpleNamespace(host=cli.DEFAULT_HOST, port=18104, running=running)
        monkeypatch.setattr(cli, "AdminConsole", SimpleNamespace(requested=lambda args: console))
    monkeypatch.setattr("uvicorn.Server", lambda *args, **kwargs: pytest.fail("Server started after cancellation"))
    cli.serve(fake_serve.args, stop_event=stop, print_admin_url=False)
    if stage != "inspect":
        assert fake_serve.operations[-2:] == ["listener-closed", "game-listener-closed"]
    if stage == "console":
        assert "console-closed" in fake_serve.operations


def test_worker_server_closes_console_on_failure(fake_serve, monkeypatch):
    def fail(*args):
        raise RuntimeError("fake server failure")

    monkeypatch.setattr(cli, "stoppable_server", fail)
    with pytest.raises(RuntimeError, match="fake server failure"):
        cli.serve(fake_serve.args, stop_event=threading.Event(), print_admin_url=False)
    assert fake_serve.operations == ["game-listener-open", "listener-open", ("console-open", {"print_url": False}),
                                      "console-closed", "listener-closed", "game-listener-closed"]


def test_game_port_collision_fails_before_admin_publication(fake_serve, monkeypatch):
    @contextmanager
    def occupied(host, port, *, label="admin console"):
        assert port == cli.DEFAULT_PORT and label == "game server"
        raise ValueError("The game server port is already occupied")
        yield

    monkeypatch.setattr(cli, "loopback_listener", occupied)
    monkeypatch.setattr(cli, "build_generation", lambda *args: pytest.fail("Generation should not be built"))
    monkeypatch.setattr(cli.AdminConsole, "requested", lambda args: pytest.fail("Admin should not be configured"))
    with pytest.raises(ValueError, match="already occupied"):
        cli.serve(fake_serve.args, stop_event=threading.Event(), print_admin_url=False)
    assert fake_serve.operations == []


def test_worker_keeps_same_game_socket_from_before_admin_start_through_shutdown(fake_serve, monkeypatch):
    def run(application, args, stop_event, listener):
        assert listener is fake_serve.listeners[cli.DEFAULT_PORT]
        assert fake_serve.operations == ["game-listener-open", "listener-open", ("console-open", {"print_url": False})]
        assert len(fake_serve.listeners) == 2

    monkeypatch.setattr(cli, "stoppable_server", run)
    cli.serve(fake_serve.args, stop_event=threading.Event(), print_admin_url=False)
    assert fake_serve.operations[-3:] == ["console-closed", "listener-closed", "game-listener-closed"]


def test_serve_forwards_only_explicit_admin_token(fake_serve, monkeypatch):
    monkeypatch.setenv("AIGIS_DESKTOP_ADMIN_TOKEN", "e" * 43)
    monkeypatch.setattr(cli, "stoppable_server", lambda *args: None)
    cli.serve(fake_serve.args, stop_event=threading.Event(), print_admin_url=False, admin_token="t" * 43)
    assert ("console-open", {"print_url": False, "token": "t" * 43}) in fake_serve.operations
    assert os.environ["AIGIS_DESKTOP_ADMIN_TOKEN"] == "e" * 43


def test_direct_serve_preserves_existing_runner_and_console_output(fake_serve, monkeypatch):
    monkeypatch.setenv("AIGIS_DESKTOP_ADMIN_TOKEN", "e" * 43)
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append((app, kwargs)))
    monkeypatch.setattr(cli, "stoppable_server", lambda *args: pytest.fail("Unexpected desktop runner"))
    cli.serve(fake_serve.args)
    assert ("console-open", {}) in fake_serve.operations
    assert calls == [(fake_serve.application, {"host": cli.DEFAULT_HOST, "port": cli.DEFAULT_PORT, "access_log": False})]


@pytest.mark.parametrize("outcome", ["stop-before-started", "stop-running", "return", "raise"])
def test_event_runner_waits_for_startup_and_joins_its_monitor(monkeypatch, outcome):
    stop = threading.Event()
    listener = object()

    class Server:
        started = should_exit = False

        def __init__(self, config):
            pass

        def run(self, *, sockets):
            assert sockets == [listener]
            if outcome == "return":
                return
            if outcome == "raise":
                raise RuntimeError("failed startup")
            if outcome == "stop-before-started":
                stop.set()
                time.sleep(0.03)
                assert not self.should_exit
            self.started = True
            stop.set()
            deadline = time.monotonic() + 1
            while not self.should_exit and time.monotonic() < deadline:
                time.sleep(0.001)
            assert self.should_exit

    monkeypatch.setattr("uvicorn.Config", lambda *args, **kwargs: object())
    monkeypatch.setattr("uvicorn.Server", Server)
    args = SimpleNamespace(host=cli.DEFAULT_HOST, port=cli.DEFAULT_PORT)
    if outcome == "raise":
        with pytest.raises(RuntimeError, match="failed startup"):
            cli.stoppable_server(object(), args, stop, listener)
    else:
        cli.stoppable_server(object(), args, stop, listener)
    assert not any(thread.name == "desktop-server-stop" for thread in threading.enumerate())
