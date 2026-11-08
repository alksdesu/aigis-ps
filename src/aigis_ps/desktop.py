from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import time

from .admin_security import console_url, new_token
from .desktop_api import create_desktop_app
from .desktop_controller import DesktopController, DesktopError
from .local_io import checked_path, exclusive_file


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TITLE = "Aigis 本地控制台"


def prepare_environment(root: Path):
    for key, relative in (("TEMP", "tmp"), ("TMP", "tmp"), ("PYTHONPYCACHEPREFIX", ".cache/pycache")):
        path = checked_path(root / relative, root)
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


@contextmanager
def running_api(controller):
    import uvicorn
    with socket.socket() as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        token = new_token()
        app = create_desktop_app(controller, port=port, token=token)
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_config=None, proxy_headers=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True,
                                  name="desktop-api")
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise DesktopError("控制台界面未能启动。")
                time.sleep(0.05)
            yield console_url("127.0.0.1", port, token)
        finally:
            server.should_exit = True
            thread.join(10)


def show_error(message: str):
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x10)
    else:
        print(message, file=sys.stderr)


def run_window(controller, url: str):
    import webview

    directory = checked_path(controller.directory / "webview", controller.root)
    directory.mkdir(parents=True, exist_ok=True)
    window = webview.create_window(TITLE, url, width=1280, height=850, min_size=(900, 650), text_select=True)

    def closing():
        try:
            controller.close()
        except DesktopError as error:
            show_error(str(error))
            return False
        return True

    def loaded():
        missing = window.evaluate_js('document.querySelector(".page-title")?.textContent === "没有管理令牌"')
        if missing:
            window.load_url(url)

    window.events.closing += closing
    window.events.loaded += loaded
    webview.start(gui="edgechromium", private_mode=False, storage_path=str(directory), debug=False)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Aigis desktop console")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    prepare_environment(PROJECT_ROOT)
    if args.check:
        import webview
        from webview.platforms.edgechromium import EdgeChrome
        print("Desktop window dependencies are available; no game service or save was opened.")
        return 0
    directory = checked_path(PROJECT_ROOT / "runtime" / "desktop", PROJECT_ROOT)
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=directory / "desktop.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        with exclusive_file(directory / "instance.lock"):
            controller = DesktopController(PROJECT_ROOT)
            try:
                with running_api(controller) as url:
                    run_window(controller, url)
            finally:
                controller.close()
        return 0
    except Exception as error:
        if isinstance(error, ValueError) and str(error) == "Another resource collection or version update is running":
            show_error("此项目的控制台已经打开，请使用已有窗口。")
            return 0
        logging.exception("Desktop console failed")
        show_error(f"控制台启动或退出失败：{error}\n日志：{directory / 'desktop.log'}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
