from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from functools import wraps
from http import HTTPStatus
import re
from typing import Any, Protocol

from pydantic import Field

from .admin_command import AdminError, CommandParams
from .admin_diff import REDACTED, SECRET_FIELDS, pointer, redact
from .admin_registry import AdminView
from .generation import Generation
from .storage import ADMIN_PAGE_SIZE, MAX_ADMIN_PAGE_SIZE, MAX_REVISION, PlayerProfile


MAX_PAGE_ITEMS = 500
DEFAULT_PAGE_ITEMS = 100
MAX_POINTER_CHARS = 1024
PROJECTION_NOTE = "Estimated for the server time; nothing is saved"
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")
_BROKEN_ESCAPE = re.compile(r"~(?![01])")


class LeasedView(Protocol):
    @property
    def generation(self) -> Generation: ...

    @property
    def id(self) -> str: ...

    @property
    def bundle(self) -> Any: ...

    def acquisition_day(self, now: datetime) -> int: ...


class ViewContext(Protocol):
    @property
    def services(self) -> Any: ...

    @property
    def profile(self) -> PlayerProfile: ...

    @property
    def generation(self) -> LeasedView: ...

    @property
    def now(self) -> datetime: ...

    @property
    def state(self) -> dict: ...

    @property
    def revision(self) -> int: ...


ViewRead = Callable[[ViewContext, Any], Any]


class PageParams(CommandParams):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=DEFAULT_PAGE_ITEMS, ge=1, le=MAX_PAGE_ITEMS)


class CursorParams(CommandParams):
    # Cursors are SQLite integer keys, which share the signed 64-bit bound of revisions.
    cursor: int | None = Field(default=None, ge=1, le=MAX_REVISION)
    limit: int = Field(default=ADMIN_PAGE_SIZE, ge=1, le=MAX_ADMIN_PAGE_SIZE)


def page(items: Sequence, params: PageParams, render: Callable[[Any], Any] | None = None) -> dict:
    end = min(params.offset + params.limit, len(items))
    window = items[params.offset:end]
    return {"items": list(window) if render is None else [render(item) for item in window], "total": len(items),
            "offset": params.offset, "limit": params.limit, "next_offset": end if end < len(items) else None}


def parse_pointer(text: str) -> tuple[str, ...]:
    if text == "":
        return ()
    if not text.startswith("/") or _BROKEN_ESCAPE.search(text):
        raise AdminError("admin_pointer_invalid", "State locations are RFC 6901 JSON Pointers",
                         HTTPStatus.UNPROCESSABLE_ENTITY)
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in text[1:].split("/"))


def resolve_pointer(document: Any, path: tuple[str, ...]) -> Any:
    node = document
    for depth, segment in enumerate(path):
        if isinstance(node, dict) and segment in node:
            node = node[segment]
        elif isinstance(node, list) and _ARRAY_INDEX.fullmatch(segment) and int(segment) < len(node):
            node = node[int(segment)]
        else:
            raise AdminError("admin_pointer_missing", f"Nothing is stored at {pointer(path[:depth + 1])}",
                             HTTPStatus.NOT_FOUND)
    return node


def secret_path(path: tuple[str, ...]) -> bool:
    return any(segment in SECRET_FIELDS for segment in path)


def redact_at(path: tuple[str, ...], value: Any) -> Any:
    if secret_path(path):
        return REDACTED
    # Field rules key on member names, so an addressed node is redacted as a member of its parent.
    return redact({path[-1]: value})[path[-1]] if path else redact(value)


def redacted_view(name: str, domain: str, read: ViewRead, params_model: type[CommandParams] | None = None, *,
                  live: bool = False) -> AdminView:
    # Saves, request records and service reports can all carry account secrets, so every view result is redacted.
    @wraps(read)
    def redacted(context: ViewContext, params: Any) -> Any:
        return redact(read(context, params))

    return AdminView(name, domain, redacted, params_model, live=live)


def projected(**fields: Any) -> dict:
    return {"projected": True, "persisted": False, "note": PROJECTION_NOTE, **fields}


def service(bundle: Any, name: str) -> Any:
    found = getattr(bundle, name, None)
    if found is None:
        raise AdminError("admin_view_service_unavailable", f"This generation has no {name} service",
                         HTTPStatus.SERVICE_UNAVAILABLE)
    return found
