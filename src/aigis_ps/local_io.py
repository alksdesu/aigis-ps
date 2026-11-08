from __future__ import annotations

import json
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path


MAX_JSON_DEPTH = 64
_JSON_CONTAINERS = (dict, list)


# Config loaders report RecursionError as their nesting code; every other reader treats it as a malformed value.
class JsonNestingTooDeep(ValueError, RecursionError):
    pass


def checked_path(path: Path, root: Path | None = None) -> Path:
    absolute = Path(os.path.abspath(path))
    if root is not None and not absolute.is_relative_to(Path(os.path.abspath(root))):
        raise ValueError("Path leaves its permitted directory")
    for part in (absolute, *absolute.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError("Links and junctions are not accepted for local data")
    return absolute


def read_bounded(path: Path, limit: int, *, root: Path | None = None) -> bytes:
    path = checked_path(path, root)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Local data file is missing or exceeds its size limit")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Local data file exceeds its size limit")
    return data


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def reject_nonfinite(value: str) -> object:
    raise ValueError("JSON numbers must be finite")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        reject_nonfinite(text)
    return value


def _require_depth(document: object) -> object:
    pending = [(document, 1)] if isinstance(document, _JSON_CONTAINERS) else []
    while pending:
        value, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise JsonNestingTooDeep(f"JSON nesting exceeds {MAX_JSON_DEPTH} levels")
        children = value.values() if isinstance(value, dict) else value
        pending.extend((child, depth + 1) for child in children if isinstance(child, _JSON_CONTAINERS))
    return document


def deserialize(text: str | bytes) -> object:
    return _require_depth(json.loads(text, object_pairs_hook=reject_duplicate_keys, parse_constant=reject_nonfinite,
                                     parse_float=_finite_float))


def read_json_object(path: Path, *, limit: int = 256 * 1024, root: Path | None = None) -> dict:
    value = deserialize(read_bounded(path, limit, root=root))
    if not isinstance(value, dict):
        raise ValueError("Local JSON data must be an object")
    return value


def atomic_replace(source: Path, destination: Path) -> None:
    for attempt in range(10):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(min(0.25, 0.01 * 2 ** attempt))


@contextmanager
def exclusive_file(path: Path):
    path = checked_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("Another resource collection or version update is running") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
