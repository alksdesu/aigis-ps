from pathlib import Path
import json
import subprocess
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from aigis_ps import desktop_controller
from aigis_ps.admin_security import API_ROOT
from aigis_ps.desktop_api import create_desktop_app
from aigis_ps.desktop_controller import DesktopController, DesktopError


TOKEN = "d" * 43
INTERNAL_TOKEN = "i" * 43
PORT = 18105
ORIGIN = f"http://127.0.0.1:{PORT}"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Profiles:
    def __init__(self, root):
        self.items = [{"id": "default", "name": "Current", "data_dir": str(root / "runtime/server"), "selected": True}]

    def list(self):
        return [dict(item) for item in self.items]

    def selected(self):
        return next((dict(item) for item in self.items if item["selected"]), None)

    def create(self, name):
        self.items.append({"id": "a" * 32, "name": name, "data_dir": "unused", "selected": not self.items})

    def select(self, profile_id):
        assert any(item["id"] == profile_id for item in self.items)
        for item in self.items:
            item["selected"] = item["id"] == profile_id


class Pool:
    def __init__(self, **kwargs):
        self.tasks = []
        self.closed = False

    def submit(self, callback, *args):
        assert not self.closed
        self.tasks.append((callback, args))

    def shutdown(self, **kwargs):
        self.closed = True


class Pipe:
    def __init__(self):
        self.written = []
        self.closed = False
        self.flushes = 0

    def write(self, data):
        self.written.append(data)

    def flush(self):
        self.flushes += 1

    def close(self):
        self.closed = True


class Process:
    def __init__(self):
        self.stdin = Pipe()
        self.returncode = None
        self.pid = 22011
        self.wait_hook = lambda: None
        self.waits = []

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.waits.append(timeout)
        self.wait_hook()
        self.returncode = 0
        return 0


@pytest.fixture
def controlled(tmp_path, monkeypatch):
    state = SimpleNamespace(games=[], busy=False, calls=[], processes=[])

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def connect_ex(self, endpoint):
            assert endpoint == ("127.0.0.1", 18103)
            return 0 if state.busy else 1

        def bind(self, endpoint):
            assert endpoint == ("127.0.0.1", 0)

        def getsockname(self):
            return "127.0.0.1", 18199

    def popen(args, **kwargs):
        state.calls.append((args, kwargs))
        process = Process()
        state.processes.append(process)
        return process

    monkeypatch.setattr(desktop_controller, "ThreadPoolExecutor", Pool)
    monkeypatch.setattr(desktop_controller, "socket", SimpleNamespace(socket=Socket))
    monkeypatch.setattr(desktop_controller.subprocess, "Popen", popen)
    monkeypatch.setattr(desktop_controller, "new_token", lambda: INTERNAL_TOKEN)
    monkeypatch.setattr(desktop_controller, "game_processes", lambda path: list(state.games))
    root = tmp_path / "project"
    root.mkdir()
    controller = DesktopController(root, profiles=Profiles(root))
    state.controller = controller
    yield state
    controller._process = None
    controller._pool.shutdown(wait=True)


def ready(controlled):
    controller = controlled.controller
    controller.start_service()
    controller._service_state = "running"
    controller._admin_token = INTERNAL_TOKEN
    return controller


def test_service_start_uses_fixed_hidden_worker_and_deduplicates(controlled):
    controller = controlled.controller
    assert controller.start_service()["service"]["state"] == "starting"
    controller.start_service()
    assert len(controlled.calls) == 1
    args, options = controlled.calls[0]
    assert args == [str(controller.root / ".venv/Scripts/python.exe"), "-B", "-m", "aigis_ps.desktop_worker",
                    "--data-dir", controller.profiles.selected()["data_dir"],
                    "--asset-dir", str(controller.root.parent / "aigisr_exe"),
                    "--resource-dir", str(controller.root / "runtime/resources"),
                    "--port", "18103", "--admin-port", "18199"]
    assert options["stdin"] == subprocess.PIPE
    assert options["stderr"] == subprocess.STDOUT
    assert not options.get("shell", False) and not options.get("text", False)
    assert options["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert options["env"]["PYTHONPATH"] == str(controller.root / "src")
    assert options["env"]["AIGIS_DESKTOP_ADMIN_TOKEN"] == INTERNAL_TOKEN
    assert len(controller._pool.tasks) == 1
    controller._service_state = "running"
    controller.start_service()
    assert len(controlled.calls) == 1


@pytest.mark.parametrize("action", ["service", "game", "endpoint", "scoped-endpoint"])
def test_empty_profile_requires_creation_before_start_or_proxy_without_side_effects(controlled, monkeypatch, action):
    controller = controlled.controller
    controller.profiles.items.clear()
    initial = controller.status()
    assert initial["profiles"] == [] and initial["selected_profile"] is None
    monkeypatch.setattr(desktop_controller.socket, "socket", lambda: pytest.fail("No profile must not access ports"))
    operation = {"service": controller.start_service, "game": controller.start_game,
                 "endpoint": controller.endpoint, "scoped-endpoint": lambda: controller.endpoint("default")}[action]
    with pytest.raises(DesktopError) as error:
        operation()
    assert (error.value.code, error.value.status) == ("desktop_profile_required", 409)
    assert controller.status() == initial
    assert controlled.calls == [] and controller._pool.tasks == []
    assert controller._log_path is None and not list(controller.directory.glob("server-*.log"))


def test_first_profile_creation_selects_it_without_starting_service(controlled):
    controller = controlled.controller
    controller.profiles.items.clear()
    state = controller.create_profile("第一档")
    assert state["selected_profile"]["id"] == "a" * 32
    assert len(state["profiles"]) == 1 and state["service"]["state"] == "stopped"
    assert controlled.calls == [] and controller._pool.tasks == []


@pytest.mark.parametrize("action", ["start", "create", "select"])
def test_external_game_port_blocks_start_and_profile_changes(controlled, action):
    controlled.busy = True
    controller = controlled.controller
    operation = {"start": controller.start_service, "create": lambda: controller.create_profile("New"),
                 "select": lambda: controller.select_profile("default")}[action]
    with pytest.raises(DesktopError) as error:
        operation()
    assert error.value.code == "desktop_port_busy"
    assert controlled.calls == []


@pytest.mark.parametrize("action", ["create", "select"])
@pytest.mark.parametrize("active", ["starting", "running", "stopping", "game", "game-starting"])
def test_profiles_require_service_and_game_stopped(controlled, action, active):
    controller = controlled.controller
    if active == "game":
        controlled.games = [123]
    elif active == "game-starting":
        controller._game_state = "starting"
    else:
        controller.start_service()
        controller._service_state = active
    operation = lambda: controller.create_profile("New") if action == "create" else controller.select_profile("default")
    with pytest.raises(DesktopError):
        operation()
    assert len(controller.profiles.items) == 1


def test_profile_creation_and_selection_do_not_spawn_service(controlled):
    controller = controlled.controller
    controller.create_profile("New")
    state = controller.select_profile("a" * 32)
    assert state["selected_profile"]["id"] == "a" * 32
    assert state["service"]["state"] == "stopped"
    assert controlled.calls == []


def test_stop_is_graceful_deduplicated_and_allows_restart_after_exit(controlled):
    controller = ready(controlled)
    first = controlled.processes[0]
    assert controller.stop_service()["service"]["state"] == "stopping"
    controller.stop_service()
    assert first.stdin.written == [b"stop\n"] and first.stdin.flushes == 1
    with pytest.raises(DesktopError):
        controller.start_service()
    first.returncode = 0
    assert controller.status()["service"]["state"] == "stopped"
    assert first.stdin.closed
    controller.start_service()
    assert len(controlled.processes) == 2


def test_stop_and_close_refuse_while_game_is_running(controlled):
    controller = ready(controlled)
    controlled.games = [123]
    for operation in (controller.stop_service, controller.close):
        with pytest.raises(DesktopError) as error:
            operation()
        assert error.value.code == "desktop_game_active"
    assert controlled.processes[0].stdin.written == []


def test_game_start_requires_service_and_deduplicates_pending_launch(controlled):
    controller = controlled.controller
    with pytest.raises(DesktopError) as error:
        controller.start_game()
    assert error.value.code == "desktop_service_offline"
    ready(controlled)
    controller.start_game()
    controller.start_game()
    assert [callback.__name__ for callback, args in controller._pool.tasks] == ["_await_service", "_launch_game"]
    controlled.games = [123]
    assert controller.status()["game"]["pid"] == 123
    controller.start_game()
    assert len(controller._pool.tasks) == 2


def test_worker_failure_clears_credentials_and_reports_failure(controlled):
    controller = ready(controlled)
    controlled.processes[0].returncode = 7
    state = controller.status()
    assert state["service"]["state"] == "failed"
    assert controller._admin_token is None and controller._process is None
    with pytest.raises(DesktopError) as error:
        controller.endpoint("default")
    assert error.value.code == "desktop_service_offline"


def error_record(code):
    return b"AIGIS_DESKTOP_ERROR " + json.dumps({"schema_version": 1, "code": code}).encode() + b"\n"


@pytest.mark.parametrize("code,phrase", [
    ("energy_migration_database_busy", "数据库日志文件"),
    ("energy_clock_source_required", "确认原时区"),
    ("energy_clock_invalid", "时钟标记无效"),
    ("energy_clock_anchor_invalid", "时间数据校验失败"),
    ("energy_clock_state_invalid", "状态结构无效"),
    ("energy_migration_database_invalid", "数据库或存档数据校验"),
    ("energy_migration_path_invalid", "路径无效或不可访问"),
    ("energy_migration_stale", "检查期间发生变化"),
])
def test_worker_failure_maps_known_code_without_exposing_log_message(controlled, code, phrase):
    controller = ready(controlled)
    controller._log_path.write_bytes(b"Private diagnostics " + INTERNAL_TOKEN.encode() + b"\n" + error_record(code))
    controlled.processes[0].returncode = 1
    error = controller.status()["service"]["error"]
    assert phrase in error
    assert INTERNAL_TOKEN not in error and "Private diagnostics" not in error
    assert ("迁移" in error) is (code == "energy_clock_source_required")


@pytest.mark.parametrize("record", [
    b"", b"ordinary error\n", error_record("unknown_failure"),
    b'AIGIS_DESKTOP_ERROR {"schema_version":1,"code":"energy_clock_invalid"}',
    b'AIGIS_DESKTOP_ERROR {"schema_version":true,"code":"energy_clock_invalid"}\n',
    b'AIGIS_DESKTOP_ERROR {"schema_version":2,"code":"energy_clock_invalid"}\n',
    b'AIGIS_DESKTOP_ERROR {"schema_version":1,"code":[]}\n',
    b'AIGIS_DESKTOP_ERROR {"schema_version":1,"code":"energy_clock_invalid","path":"secret"}\n',
    b'AIGIS_DESKTOP_ERROR {"schema_version":1,"code":"energy_clock_invalid","code":"unknown"}\n',
    b'AIGIS_DESKTOP_ERROR {\xff}\n',
])
def test_unknown_or_malformed_worker_record_uses_safe_fallback(controlled, record):
    controller = ready(controlled)
    controller._log_path.write_bytes(record)
    controlled.processes[0].returncode = 7
    assert controller.status()["service"]["error"] == "服务已退出（代码 7），请查看启动日志。"


def test_last_worker_record_prevents_old_or_injected_error_selection(controlled):
    controller = ready(controlled)
    controller._log_path.write_bytes(error_record("energy_clock_source_required") + error_record("desktop_worker_failed"))
    controlled.processes[0].returncode = 1
    assert controller.status()["service"]["error"] == "服务已退出（代码 1），请查看启动日志。"


@pytest.mark.parametrize("record_at_end", [True, False])
def test_worker_error_reads_only_bounded_tail(controlled, monkeypatch, record_at_end):
    controller = ready(controlled)
    record = error_record("energy_migration_database_busy")
    padding = b"private old logs\n" * desktop_controller.ERROR_LOG_BYTES
    controller._log_path.write_bytes(padding + record if record_at_end else record + padding)
    original = Path.open
    reads, seeks = [], []

    class BoundedFile:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def fileno(self):
            return self.file.fileno()

        def seek(self, offset):
            seeks.append(offset)
            return self.file.seek(offset)

        def read(self, count):
            reads.append(count)
            return self.file.read(count)

    def opened(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        return BoundedFile(result) if path == controller._log_path and args == ("rb",) else result

    monkeypatch.setattr(Path, "open", opened)
    controlled.processes[0].returncode = 1
    error = controller.status()["service"]["error"]
    assert ("数据库日志文件" in error) is record_at_end
    assert reads == [desktop_controller.ERROR_LOG_BYTES]
    assert seeks == [len(record) + len(padding) - desktop_controller.ERROR_LOG_BYTES]


@pytest.mark.parametrize("invalid", ["missing", "outside", "wrong-name", "link"])
def test_worker_log_lookup_refuses_missing_or_untrusted_paths(controlled, monkeypatch, invalid):
    controller = ready(controlled)
    if invalid == "missing":
        controller._log_path = controller.directory / "server-missing.log"
    elif invalid == "outside":
        controller._log_path = controller.root.parent / "server-outside.log"
    elif invalid == "wrong-name":
        controller._log_path = controller.directory / "not-a-server.log"
    else:
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda path: path == controller._log_path or original(path))
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("Untrusted path must not be opened"))
    controlled.processes[0].returncode = 1
    assert controller.status()["service"]["error"] == "服务已退出（代码 1），请查看启动日志。"


def test_new_start_does_not_reuse_previous_log_error(controlled):
    controller = ready(controlled)
    previous = controller._log_path
    previous.write_bytes(error_record("energy_clock_source_required"))
    controlled.processes[0].returncode = 1
    assert "确认原时区" in controller.status()["service"]["error"]
    controller.start_service()
    assert controller._log_path != previous
    controlled.processes[-1].returncode = 1
    assert controller.status()["service"]["error"] == "服务已退出（代码 1），请查看启动日志。"


def test_restarting_with_unchanged_clock_creates_distinct_logs(controlled, monkeypatch):
    controller = controlled.controller
    instant = desktop_controller.datetime(2026, 11, 7, tzinfo=desktop_controller.timezone.utc)
    monkeypatch.setattr(desktop_controller, "datetime", SimpleNamespace(now=lambda zone: instant))
    controller.start_service()
    first = controller._log_path
    controlled.processes[0].returncode = 0
    controller.start_service()
    assert controller._log_path != first
    assert first.is_file() and controller._log_path.is_file()
    assert len(controlled.processes) == 2


def test_exited_worker_is_cleared_even_when_pipe_close_fails(controlled):
    controller = ready(controlled)
    process = controlled.processes[0]
    process.returncode = 1

    def broken_close():
        raise BrokenPipeError("worker has exited")

    process.stdin.close = broken_close
    assert controller.status()["service"]["state"] == "failed"
    assert controller._process is None and controller._admin_token is None


def test_close_waits_for_owned_process_and_closes_pipe(controlled):
    controller = ready(controlled)
    process = controlled.processes[0]
    controller.close()
    assert process.stdin.written == [b"stop\n"]
    assert process.stdin.closed and process.waits == [desktop_controller.STOP_TIMEOUT]
    assert controller._closed and controller._pool.closed
    with pytest.raises(DesktopError):
        controller.start_service()


def test_close_blocks_restart_while_waiting_for_old_worker(controlled):
    controller = ready(controlled)
    process = controlled.processes[0]

    def competing_start():
        process.returncode = 0
        with pytest.raises(DesktopError):
            controller.start_service()

    process.wait_hook = competing_start
    controller.close()
    assert len(controlled.processes) == 1


def test_close_timeout_keeps_worker_owned_for_retry(controlled):
    controller = ready(controlled)
    process = controlled.processes[0]

    def timeout():
        raise subprocess.TimeoutExpired("worker", desktop_controller.STOP_TIMEOUT)

    process.wait_hook = timeout
    with pytest.raises(DesktopError):
        controller.close()
    assert controller._process is process and not controller._pool.closed
    process.wait_hook = lambda: None
    controller.close()
    assert controller._process is None and controller._pool.closed


def test_start_timeout_requests_stop_without_terminating_process(controlled, monkeypatch):
    controller = controlled.controller
    controller.start_service()
    monkeypatch.setattr(desktop_controller, "START_TIMEOUT", 0)
    controller._await_service(controlled.processes[0], Path(controller.profiles.selected()["data_dir"]))
    assert controller._service_state == "stopping"
    assert controlled.processes[0].stdin.written == [b"stop\n"]


def test_service_ready_accepts_redirector_pid_with_launch_token_and_both_health_endpoints(controlled, monkeypatch):
    controller = controlled.controller
    controller.start_service()
    process = controlled.processes[0]
    requests = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            requests.append((url, kwargs))
            return httpx.Response(200, json={"service": "aigis-ps"})

    monkeypatch.setattr(desktop_controller.httpx, "Client", Client)
    monkeypatch.setattr(desktop_controller, "read_json_object",
                        lambda *args, **kwargs: {"pid": process.pid + 1, "admin_port": 18199, "token": INTERNAL_TOKEN})
    controller._await_service(process, Path(controller.profiles.selected()["data_dir"]))
    assert controller._service_state == "running" and controller._admin_token == INTERNAL_TOKEN
    assert requests == [("http://127.0.0.1:18199/admin/api/v1/session",
                         {"headers": {"Authorization": f"Bearer {INTERNAL_TOKEN}"}}),
                        ("http://127.0.0.1:18103/health", {})]


def test_stale_access_token_cannot_mark_new_worker_ready(controlled, monkeypatch):
    controller = controlled.controller
    controller.start_service()
    process = controlled.processes[0]
    clock = iter((0, 0.1, desktop_controller.START_TIMEOUT + 1))
    monkeypatch.setattr(desktop_controller, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda value: None))
    reads = []

    def stale_access(*args, **kwargs):
        reads.append(args[0])
        return {"pid": process.pid, "admin_port": 18199, "token": "s" * 43}

    monkeypatch.setattr(desktop_controller, "read_json_object", stale_access)
    monkeypatch.setattr(httpx.Client, "get", lambda *args, **kwargs: pytest.fail("A stale token must not be probed"))
    controller._await_service(process, Path(controller.profiles.selected()["data_dir"]))
    assert len(reads) == 1
    assert controller._service_state == "stopping" and controller._admin_token is None
    assert process.stdin.written == [b"stop\n"]


@pytest.fixture
def proxy(controlled):
    requests = []
    reply = SimpleNamespace(status=200, headers={})

    def upstream(request):
        requests.append(request)
        return httpx.Response(reply.status, json={"ok": True}, headers=reply.headers)

    application = create_desktop_app(controlled.controller, port=PORT, token=TOKEN,
                                     transport=httpx.MockTransport(upstream))
    with TestClient(application, base_url=ORIGIN) as client:
        yield SimpleNamespace(client=client, requests=requests, reply=reply, controller=controlled.controller)


def test_desktop_is_available_with_service_stopped(proxy):
    status = proxy.client.get(API_ROOT + "/desktop", headers=AUTH)
    assert status.status_code == 200 and status.json()["service"]["state"] == "stopped"
    session = proxy.client.get(API_ROOT + "/session", headers=AUTH)
    assert session.status_code == 200 and session.json()["desktop_profile_id"] == "default"
    assert proxy.requests == []


def test_empty_profile_api_stays_available_and_refuses_start_or_upstream(controlled, proxy):
    controlled.controller.profiles.items.clear()
    status = proxy.client.get(API_ROOT + "/desktop", headers=AUTH)
    assert status.status_code == 200 and status.json()["selected_profile"] is None
    session = proxy.client.get(API_ROOT + "/session", headers=AUTH)
    assert session.status_code == 200 and session.json()["desktop_profile_id"] is None
    assert session.json()["desktop_profile_name"] is None
    for path in ("/desktop/service/start", "/desktop/game/start", "/plans"):
        response = proxy.client.post(API_ROOT + path, json={},
                                     headers={**AUTH, "Origin": ORIGIN, "X-Aigis-Profile": "default"})
        assert response.status_code == 409 and response.json()["error"]["code"] == "desktop_profile_required"
    assert proxy.requests == [] and controlled.calls == []


def test_status_api_exposes_only_mapped_worker_error(controlled, proxy):
    controller = ready(controlled)
    controller._log_path.write_bytes(b"Private " + INTERNAL_TOKEN.encode() + b"\n"
                                     + error_record("energy_migration_database_busy"))
    controlled.processes[0].returncode = 1
    response = proxy.client.get(API_ROOT + "/desktop", headers=AUTH)
    assert response.status_code == 200
    assert "数据库日志文件" in response.json()["service"]["error"]
    assert INTERNAL_TOKEN not in response.text and "Private" not in response.text
    assert proxy.requests == []


@pytest.mark.parametrize("profile", [None, "b" * 32])
def test_proxy_refuses_missing_or_changed_profile_before_forwarding(controlled, proxy, profile):
    ready(controlled)
    headers = {**AUTH, "Origin": ORIGIN}
    if profile is not None:
        headers["X-Aigis-Profile"] = profile
    response = proxy.client.post(API_ROOT + "/plans", json={}, headers=headers)
    assert response.status_code == 409
    expected = "desktop_profile_required" if profile is None else "desktop_profile_changed"
    assert response.json()["error"]["code"] == expected
    assert proxy.requests == []


def test_proxy_forwards_only_fixed_origin_and_internal_credential(controlled, proxy):
    ready(controlled)
    response = proxy.client.post(API_ROOT + "/plans?limit=2", json={"value": 1},
                                 headers={**AUTH, "Origin": ORIGIN, "X-Aigis-Profile": "default"})
    assert response.status_code == 200
    request, = proxy.requests
    assert str(request.url) == "http://127.0.0.1:18199/admin/api/v1/plans?limit=2"
    assert request.headers["Authorization"] == f"Bearer {INTERNAL_TOKEN}"
    assert request.headers["Origin"] == "http://127.0.0.1:18199"
    assert request.content == b'{"value":1}'
    assert "x-aigis-profile" not in request.headers


def test_proxy_refuses_redirect_without_following_it(controlled, proxy):
    ready(controlled)
    proxy.reply.status, proxy.reply.headers = 307, {"Location": "https://example.invalid/"}
    response = proxy.client.get(API_ROOT + "/state", headers={**AUTH, "X-Aigis-Profile": "default"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "desktop_redirect_refused"
    assert len(proxy.requests) == 1


def test_desktop_rejects_unauthenticated_and_cross_origin_commands(controlled, proxy):
    unauthorized = proxy.client.post(API_ROOT + "/desktop/service/start", json={}, headers={"Origin": ORIGIN})
    assert unauthorized.status_code == 401
    cross_origin = proxy.client.post(API_ROOT + "/desktop/service/start", json={},
                                     headers={**AUTH, "Origin": "https://example.invalid"})
    assert cross_origin.status_code == 403
    assert controlled.calls == []


def test_desktop_rejects_extra_command_fields(controlled, proxy):
    response = proxy.client.post(API_ROOT + "/desktop/service/start", json={"command": "anything"},
                                 headers={**AUTH, "Origin": ORIGIN})
    assert response.status_code == 422 and controlled.calls == []
