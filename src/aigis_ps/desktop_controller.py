from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import os
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid

import httpx

from .admin_security import new_token, require_token
from .desktop_processes import game_processes
from .desktop_profiles import ProfileStore
from .local_io import checked_path, deserialize, read_json_object


GAME_PORT = 18103
START_TIMEOUT = 180
STOP_TIMEOUT = 30
ERROR_LOG_BYTES = 16 * 1024
WORKER_ERROR_PREFIX = b"AIGIS_DESKTOP_ERROR "
STARTUP_ERRORS = {
    "energy_migration_database_busy": "当前存档可能仍被其他服务占用，或留有数据库日志文件。请先正常关闭相关服务，再检查日志；不要直接删除日志文件。也可以建立新档，保留旧档。",
    "energy_clock_source_required": "当前存档缺少能量时钟标记，需要先确认原时区并离线迁移。也可以建立新档，保留旧档。",
    "energy_clock_invalid": "当前存档的能量时钟标记无效，已停止启动。请检查存档或选择其他存档。",
    "energy_clock_anchor_invalid": "当前存档的能量时间数据校验失败，已停止启动。请检查存档或选择其他存档。",
    "energy_clock_state_invalid": "当前存档的能量状态结构无效，已停止启动。请检查存档或选择其他存档。",
    "energy_migration_database_invalid": "当前存档未通过数据库或存档数据校验，已停止启动。请检查存档或选择其他存档。",
    "energy_migration_path_invalid": "当前存档路径无效或不可访问，已停止启动。请检查存档目录或选择其他存档。",
    "energy_migration_stale": "存档在启动检查期间发生变化，已停止启动。请正常关闭相关服务后重试。",
}


class DesktopError(ValueError):
    def __init__(self, message: str, code: str = "desktop_unavailable", status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


class DesktopController:
    def __init__(self, project_root: Path, *, profiles: ProfileStore | None = None):
        self.root = checked_path(project_root)
        self.directory = checked_path(self.root / "runtime" / "desktop", self.root)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.profiles = profiles or ProfileStore(self.root)
        self.client = checked_path(self.root / "runtime" / "client", self.root)
        self.executable = self.client / "Aigis_r.exe"
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desktop-launch")
        self._process = None
        self._service_state = "stopped"
        self._service_error = None
        self._game_state = "stopped"
        self._game_error = None
        self._admin_port = None
        self._admin_token = None
        self._launch_token = None
        self._log_path = None
        self._closed = False

    def _refresh(self):
        if self._process is not None and self._process.poll() is not None:
            code = self._process.returncode
            expected = self._service_state == "stopping" or code == 0
            self._service_state = "stopped" if expected else "failed"
            if not expected:
                self._service_error = self._worker_error() or f"服务已退出（代码 {code}），请查看启动日志。"
            try:
                self._process.stdin.close()
            except OSError:
                pass
            self._process = None
            self._admin_token = None
            self._launch_token = None

    def _worker_error(self) -> str | None:
        if self._log_path is None:
            return None
        try:
            path = checked_path(self._log_path, self.directory)
            if path.parent != self.directory or not path.name.startswith("server-") or path.suffix != ".log":
                return None
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return None
            with path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    return None
                start = max(0, info.st_size - ERROR_LOG_BYTES)
                stream.seek(start)
                tail = stream.read(ERROR_LOG_BYTES)
            if start:
                tail = tail.partition(b"\n")[2]
            for line in reversed(tail.splitlines(keepends=True)):
                if not line.startswith(WORKER_ERROR_PREFIX):
                    continue
                if not line.endswith(b"\n"):
                    return None
                value = deserialize(line[len(WORKER_ERROR_PREFIX):])
                if (not isinstance(value, dict) or set(value) != {"schema_version", "code"}
                        or type(value["schema_version"]) is not int or value["schema_version"] != 1
                        or not isinstance(value["code"], str)):
                    return None
                return STARTUP_ERRORS.get(value["code"])
        except (OSError, ValueError, RecursionError):
            return None
        return None

    def status(self) -> dict:
        with self._lock:
            self._refresh()
            games = game_processes(self.executable)
            state = "running" if games else self._game_state
            if not games and state == "running":
                state = self._game_state = "stopped"
            return {
                "service": {"state": self._service_state, "pid": self._process.pid if self._process else None,
                            "error": self._service_error},
                "game": {"running": bool(games), "state": state, "pid": games[0] if games else None,
                         "error": self._game_error},
                "profiles": self.profiles.list(), "selected_profile": self.profiles.selected(),
                "log_path": str(self._log_path) if self._log_path else None,
            }

    def _require_idle_game(self):
        if self._game_state == "starting" or game_processes(self.executable):
            raise DesktopError("请先关闭游戏，等待启动任务完成。", "desktop_game_active")

    def _require_profile(self) -> dict:
        profile = self.profiles.selected()
        if profile is None:
            raise DesktopError("请先新建存档，再启动服务和游戏。", "desktop_profile_required")
        return profile

    def _require_stopped(self):
        self._refresh()
        if self._closed:
            raise DesktopError("控制台正在关闭。")
        if self._process is not None or self._service_state in ("starting", "stopping", "running"):
            raise DesktopError("请先停止当前服务。", "desktop_service_active")
        self._require_idle_game()
        with socket.socket() as connection:
            connection.settimeout(0.2)
            if connection.connect_ex(("127.0.0.1", GAME_PORT)) == 0:
                raise DesktopError("18103 端口已有服务运行；请先关闭外部启动的服务。", "desktop_port_busy")

    def create_profile(self, name: str) -> dict:
        with self._lock:
            self._require_stopped()
            self.profiles.create(name)
            return self.status()

    def select_profile(self, profile_id: str) -> dict:
        with self._lock:
            self._require_stopped()
            self.profiles.select(profile_id)
            self._service_error = None
            self._service_state = "stopped"
            return self.status()

    def start_service(self) -> dict:
        with self._lock:
            if self._closed:
                raise DesktopError("控制台正在关闭。")
            profile = self._require_profile()
            self._refresh()
            if self._process is not None and self._service_state in ("starting", "running"):
                return self.status()
            self._require_stopped()
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                self._admin_port = listener.getsockname()[1]
            python = checked_path(self.root / ".venv" / "Scripts" / "python.exe", self.root)
            arguments = [str(python), "-B", "-m", "aigis_ps.desktop_worker", "--data-dir", str(profile["data_dir"]),
                         "--asset-dir", str(self.root.parent / "aigisr_exe"), "--resource-dir",
                         str(self.root / "runtime" / "resources"), "--port", str(GAME_PORT),
                         "--admin-port", str(self._admin_port)]
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            self._log_path = checked_path(self.directory / f"server-{stamp}-{uuid.uuid4().hex}.log", self.root)
            self._service_state, self._service_error = "starting", None
            self._admin_token = None
            self._launch_token = new_token()
            environment = self.environment()
            environment["AIGIS_DESKTOP_ADMIN_TOKEN"] = self._launch_token
            try:
                with self._log_path.open("xb") as log:
                    self._process = subprocess.Popen(arguments, cwd=self.root, stdin=subprocess.PIPE, stdout=log,
                                                     stderr=subprocess.STDOUT, env=environment,
                                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                self._service_state = "failed"
                self._service_error = "无法启动服务进程，请检查本地 Python 环境。"
                raise
            self._pool.submit(self._await_service, self._process, Path(profile["data_dir"]))
            return self.status()

    def environment(self) -> dict:
        environment = os.environ.copy()
        environment.update({"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
                            "PYTHONPATH": str(self.root / "src"), "TEMP": str(self.root / "tmp"),
                            "TMP": str(self.root / "tmp"), "PYTHONPYCACHEPREFIX": str(self.root / ".cache" / "pycache")})
        return environment

    def _await_service(self, process, data_dir: Path):
        deadline = time.monotonic() + START_TIMEOUT
        with httpx.Client(trust_env=False, timeout=2) as client:
            while time.monotonic() < deadline:
                with self._lock:
                    self._refresh()
                    if self._process is not process or self._service_state != "starting":
                        return
                    port = self._admin_port
                try:
                    access = read_json_object(data_dir / "admin" / "access.json", limit=4096, root=data_dir)
                    token = require_token(access.get("token"))
                    if access.get("admin_port") == port and secrets.compare_digest(token, self._launch_token or ""):
                        response = client.get(f"http://127.0.0.1:{port}/admin/api/v1/session",
                                              headers={"Authorization": f"Bearer {token}"})
                        health = client.get(f"http://127.0.0.1:{GAME_PORT}/health")
                        if response.status_code == 200 and health.status_code == 200 and health.json().get("service") == "aigis-ps":
                            with self._lock:
                                if self._process is process and self._service_state == "starting" and process.poll() is None:
                                    self._admin_token, self._service_state = token, "running"
                            return
                except (OSError, ValueError, KeyError, httpx.HTTPError):
                    pass
                time.sleep(0.2)
        with self._lock:
            if self._process is process and self._service_state == "starting":
                self._service_error = "服务启动超时，已请求正常停止；请查看启动日志。"
                self._send_stop()

    def _send_stop(self):
        self._service_state = "stopping"
        try:
            self._process.stdin.write(b"stop\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError):
            self._refresh()

    def stop_service(self) -> dict:
        with self._lock:
            self._refresh()
            self._require_idle_game()
            if self._process is not None and self._service_state != "stopping":
                self._send_stop()
            return self.status()

    def start_game(self) -> dict:
        with self._lock:
            if self._closed:
                raise DesktopError("控制台正在关闭。")
            self._require_profile()
            self._refresh()
            if self._game_state == "starting" or game_processes(self.executable):
                return self.status()
            if self._service_state != "running":
                raise DesktopError("请先启动服务并等待就绪。", "desktop_service_offline")
            self._game_state, self._game_error = "starting", None
            self._pool.submit(self._launch_game)
            return self.status()

    def _launch_game(self):
        try:
            name = "aigis_desktop_client"
            module = sys.modules.get(name)
            if module is None:
                spec = importlib.util.spec_from_file_location(name, self.root / "client" / "prepare_client.py")
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
            module.launch(self.client)
            with self._lock:
                self._game_state = "running"
        except Exception as error:
            with self._lock:
                self._game_state, self._game_error = "failed", str(error)

    def endpoint(self, profile_id: str | None = None) -> tuple[str, str]:
        with self._lock:
            profile = self._require_profile()
            self._refresh()
            if self._service_state != "running" or not self._admin_token:
                raise DesktopError("服务尚未运行，请从启动页启动。", "desktop_service_offline", 503)
            if profile_id is not None and profile_id != profile["id"]:
                raise DesktopError("当前存档已切换，请重新打开页面。", "desktop_profile_changed")
            return f"http://127.0.0.1:{self._admin_port}", self._admin_token

    def close(self):
        with self._lock:
            self._require_idle_game()
            self._refresh()
            self._closed = True
            process = self._process
            if process is not None:
                self._send_stop()
        if process is not None:
            try:
                process.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired as error:
                raise DesktopError("服务仍在退出，请等待后再关闭控制台。") from error
        with self._lock:
            self._refresh()
            self._closed = True
        self._pool.shutdown(wait=True, cancel_futures=True)
