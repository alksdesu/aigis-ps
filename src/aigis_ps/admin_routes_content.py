from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
import re
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import Field

from .admin_app import AdminServices, request_body
from .admin_command import AdminError, CommandParams
from .admin_config_switch import CONFIG_KIND, MasterState, config_switch, master_state
from .admin_registry import AdminRoute, RouteRegistry
from .admin_routes_config import SET_ID_PATTERN, ValidationRequest
from .admin_views import DEFAULT_PAGE_ITEMS, MAX_PAGE_ITEMS, PageParams, page
from .errors import ProtocolError
from .local_io import checked_path
from .masterdata import MASTER_TABLES, SNAPSHOT_ID_LENGTH, MasterDataStore, table_summary


ROUTE_NAME = "content"
CONTENT_KIND = "content"
# The capture tool writes beside the default server data directory, so captures never leave the project runtime.
CAPTURES_DIRECTORY = "captures"
SNAPSHOT_PATTERN = rf"^[a-f0-9]{{{SNAPSHOT_ID_LENGTH}}}$"
MAX_CAPTURE_NAME_CHARS = 128
CAPTURE_PATTERN = rf"^[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_CAPTURE_NAME_CHARS - 1}}}$"
_CAPTURE_NAME = re.compile(CAPTURE_PATTERN)
_UNREADABLE = (ValueError, OSError, RecursionError)
_INVALID_CAPTURE = (ValueError, RecursionError)
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE_ITEMS)]


class ImportRequest(CommandParams):
    capture: str = Field(pattern=CAPTURE_PATTERN)


class ContentValidationRequest(ValidationRequest):
    master_snapshot: str = Field(pattern=SNAPSHOT_PATTERN)


class ContentUnstageRequest(CommandParams):
    staged_snapshot: str = Field(pattern=SNAPSHOT_PATTERN)
    staged_set: str | None = Field(default=None, pattern=SET_ID_PATTERN)
    op_id: str


def register(registry: RouteRegistry) -> None:
    registry.add(AdminRoute(ROUTE_NAME, build))


def captures_root(data_dir: Path) -> Path:
    try:
        return checked_path(Path(data_dir).parent / CAPTURES_DIRECTORY)
    except ValueError as error:
        raise AdminError("content_captures_unreadable", "The capture directory is a link or junction",
                         HTTPStatus.SERVICE_UNAVAILABLE) from error


def capture_names(root: Path) -> list[str]:
    try:
        if not root.is_dir():
            return []
        return sorted((path.name for path in root.iterdir() if _CAPTURE_NAME.fullmatch(path.name) and path.is_dir()),
                      reverse=True)
    except OSError as error:
        raise AdminError("content_captures_unreadable", "The capture directory cannot be listed",
                         HTTPStatus.SERVICE_UNAVAILABLE) from error


def capture_directory(root: Path, name: str) -> Path:
    try:
        directory = checked_path(root / name, root)
        present = directory.is_dir()
    except _UNREADABLE as error:
        raise AdminError("content_capture_invalid", "The capture directory cannot be used",
                         HTTPStatus.UNPROCESSABLE_ENTITY) from error
    if not present:
        raise AdminError("content_capture_unknown", f"No capture directory named {name} exists", HTTPStatus.NOT_FOUND)
    return directory


def running_masters(services: AdminServices, masters: MasterDataStore) -> MasterState:
    return master_state(services.store, masters, services.runtime.current.identity.master_snapshot)


def marked(entry: dict, state: MasterState) -> dict:
    identifier = entry["snapshot"]
    return {**entry, "in_use": identifier == state.in_use, "disk": identifier == state.disk,
            "next_start": identifier == state.next_start}


def content_view(services: AdminServices, masters: MasterDataStore, params: PageParams) -> dict:
    state = running_masters(services, masters)
    snapshots = masters.list_snapshots()
    known = {entry["snapshot"]: entry for entry in snapshots}
    return {"masterdata": state.describe(),
            "side_by_side": {"in_use": known.get(state.in_use), "disk": known.get(state.disk)},
            "snapshots": page(snapshots, params, lambda entry: marked(entry, state))}


def captures_view(services: AdminServices, masters: MasterDataStore, params: PageParams) -> dict:
    root = captures_root(services.data_dir)
    return {"root": str(root),
            **page(capture_names(root), params, lambda name: {"name": name, **masters.inspect_capture(root / name)})}


def staged_import(services: AdminServices, masters: MasterDataStore, name: str) -> dict:
    directory = capture_directory(captures_root(services.data_dir), name)
    present = masters.inspect_capture(directory)["imported"]
    try:
        manifest = masters.import_capture(directory, activate=False)
    except ProtocolError:
        raise
    except OSError as error:
        raise AdminError("content_import_failed", "The capture could not be read or stored in the data directory",
                         HTTPStatus.SERVICE_UNAVAILABLE) from error
    except _INVALID_CAPTURE as error:
        raise AdminError("content_capture_invalid", f"The capture cannot be imported: {error}",
                         HTTPStatus.UNPROCESSABLE_ENTITY) from error
    return {"capture": name, "snapshot": manifest["snapshot"], "imported_at": manifest["imported_at"],
            "tables": {table: table_summary(manifest["tables"][table]) for table in MASTER_TABLES},
            "already_present": present, "activated": False, "masterdata": running_masters(services, masters).describe()}


def build(services: AdminServices) -> APIRouter:
    router = APIRouter()
    masters = MasterDataStore.for_data_dir(services.data_dir)

    @router.get("/content")
    def content(offset: Offset = 0, limit: Limit = DEFAULT_PAGE_ITEMS) -> dict:
        return content_view(services, masters, PageParams(offset=offset, limit=limit))

    @router.get("/content/captures")
    def captures(offset: Offset = 0, limit: Limit = DEFAULT_PAGE_ITEMS) -> dict:
        return captures_view(services, masters, PageParams(offset=offset, limit=limit))

    @router.post("/content/imports")
    def imports(request: Request) -> dict:
        with services.recording_failures(CONTENT_KIND, request):
            body = request_body(request, ImportRequest)
            return staged_import(services, masters, body.capture)

    @router.post("/content/validations")
    def validations(request: Request) -> dict:
        body = request_body(request, ContentValidationRequest)
        return config_switch(request.app).validate(body.changes, base_set=body.base_set, op_id=body.op_id,
                                                   master_snapshot=body.master_snapshot)

    @router.post("/content/unstage")
    def unstage(request: Request) -> dict:
        with services.recording_failures(CONFIG_KIND, request):
            body = request_body(request, ContentUnstageRequest)
            return config_switch(request.app).unstage(op_id=body.op_id, staged_set=body.staged_set,
                                                      staged_master=body.staged_snapshot)

    return router
