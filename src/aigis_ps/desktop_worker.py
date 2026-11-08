from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import BinaryIO, TextIO

from . import cli
from .admin_security import require_token
from .local_io import checked_path


PROJECT_ROOT = cli.PROJECT_ROOT
ERROR_PREFIX = "AIGIS_DESKTOP_ERROR "


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="aigis-desktop-worker", allow_abbrev=False)
    for name in ("data-dir", "asset-dir", "resource-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--admin-port", type=int, required=True)
    selected = parser.parse_args(argv)
    if selected.port != cli.DEFAULT_PORT:
        parser.error(f"The desktop game port must be {cli.DEFAULT_PORT}")
    if not 1 <= selected.admin_port <= cli.MAX_PORT or selected.admin_port == selected.port:
        parser.error("The admin port must be a different valid TCP port")
    values = []
    paths = []
    for name in ("data_dir", "asset_dir", "resource_dir"):
        path = getattr(selected, name)
        if not path.is_absolute():
            raise ValueError("Desktop paths must be absolute")
        path = checked_path(path, PROJECT_ROOT.parent)
        if path == PROJECT_ROOT.parent or not path.is_dir():
            raise ValueError("Desktop paths must name existing workspace directories")
        paths.append(path)
        values.extend(("--" + name.replace("_", "-"), str(path)))
    data, assets, resources = paths
    default = PROJECT_ROOT / "runtime" / "server"
    saves = PROJECT_ROOT / "runtime" / "saves"
    if data != default and (data.parent != saves or not re.fullmatch(r"[a-f0-9]{32}", data.name)):
        raise ValueError("Desktop data must name the default profile or a valid save directory")
    if any(data.is_relative_to(path) or path.is_relative_to(data) for path in (assets, resources)):
        raise ValueError("The profile must be separate from client assets and shared resources")
    values.extend(("--port", str(selected.port), "--admin-port", str(selected.admin_port)))
    return cli.build_parser().parse_args(["serve", *values])


def read_stop(stream: BinaryIO | TextIO, stop_event: threading.Event, failed: threading.Event) -> None:
    try:
        command = stream.readline(6)
        if command not in ("stop\n", b"stop\n", "", b""):
            failed.set()
    except (OSError, ValueError):
        failed.set()
    finally:
        stop_event.set()


def main(argv: list[str] | None = None, *, input_stream: BinaryIO | TextIO | None = None) -> int:
    token = None
    try:
        token = os.environ.pop("AIGIS_DESKTOP_ADMIN_TOKEN", None)
        token = require_token(token)
        args = arguments(argv)
        stop_event, failed = threading.Event(), threading.Event()
        # Unbuffered pipe reads cannot hold sys.stdin's buffered lock during interpreter shutdown.
        stream = sys.stdin.buffer.raw if input_stream is None else input_stream
        reader = threading.Thread(target=read_stop, args=(stream, stop_event, failed),
                                  name="desktop-worker-input", daemon=True)
        reader.start()
        cli.serve(args, stop_event=stop_event, print_admin_url=False, admin_token=token)
        if failed.is_set():
            print("Desktop worker rejected its control input", file=sys.stderr, flush=True)
            return 1
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        message = str(error)
        if isinstance(token, str) and token:
            message = message.replace(token, "[redacted]")
        message = re.sub(r"(?i)([?#&]token=)[^&\s\"'<>]*", r"\1[redacted]", message)
        print(f"Desktop worker failed: {type(error).__name__}: {message}", file=sys.stderr, flush=True)
        code = getattr(error, "code", "desktop_worker_failed")
        if (not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code)
                or isinstance(token, str) and token and token in code):
            code = "desktop_worker_failed"
        record = json.dumps({"schema_version": 1, "code": code}, separators=(",", ":"))
        print(ERROR_PREFIX + record, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
