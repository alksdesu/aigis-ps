from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
import logging
from pathlib import Path
import re
import time
from types import MappingProxyType
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from .admin_command import (MAX_CONFIRM_PHRASE_CHARS, AdminError, PlanStore, Preflight, RestartCheck, error_payload,
                            field_errors, require_op_id)
from .admin_commit import CommitPipeline
from .admin_gates import ClientGate
from .admin_registry import AdminCatalog, discover
from .admin_security import (ADMIN_ROOT, API_ROOT, BODY_STATE, DOCUMENT_STATE, STATIC_ROOT, AdminSecurityMiddleware,
                             authority, require_bind_host, require_port, require_token)
from .business_calendar import civil_day
from .energy_clock import JST, instant
from .errors import ProtocolError
from .generation import Generation, GenerationRuntime
from .local_io import checked_path
from .storage import MAX_ADMIN_PAGE_SIZE, MAX_REVISION, AdminPage, PlayerProfile, Store


log = logging.getLogger("aigis_ps")
ModelT = TypeVar("ModelT", bound=BaseModel)
ConfirmPhrase = Annotated[str, Field(max_length=MAX_CONFIRM_PHRASE_CHARS)]
# Cursors are SQLite integer keys, so values past the signed 64-bit bound would fail inside the query.
Cursor = Annotated[int | None, Query(ge=1, le=MAX_REVISION)]
PageSize = Annotated[int, Query(ge=1, le=MAX_ADMIN_PAGE_SIZE)]
TITLE = "Aigis local admin console"
UI_DIRECTORY = Path(__file__).resolve().parent / "admin_ui"
INDEX_FILE = "index.html"
PAGES_DIRECTORY = "pages"
PAGE_SUFFIX = ".js"
PAGE_NAME = re.compile(r"[a-z][a-z0-9-]*\Z")
UI_MISSING = "admin_ui_missing"
BACKUP_DIRECTORY = "backups"
EXPORT_DIRECTORY = "exports"
ERROR_STATUSES = range(400, 600)
HTTP_ERROR_CODES = {HTTPStatus.NOT_FOUND: "admin_not_found", HTTPStatus.METHOD_NOT_ALLOWED: "admin_method_not_allowed"}
NO_PREFLIGHT_DOMAINS: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class LeasedGeneration:
    generation: Generation

    @property
    def id(self) -> str:
        return self.generation.id

    @property
    def bundle(self) -> Any:
        return self.generation.bundle

    @property
    def content_versions(self) -> Mapping[str, str]:
        return self.generation.identity.content_versions

    def now(self) -> datetime:
        return instant(self.generation.bundle.bootstrap.clock())

    def acquisition_day(self, now: datetime) -> int:
        return civil_day(now)


class LeasedRuntime:
    def __init__(self, runtime: GenerationRuntime):
        self._runtime = runtime

    @contextmanager
    def lease(self) -> Iterator[LeasedGeneration]:
        with self._runtime.lease() as generation:
            yield LeasedGeneration(generation)


@dataclass(frozen=True)
class AdminEndpoint:
    host: str
    port: int
    game_port: int

    def describe(self) -> dict:
        return {"host": self.host, "port": self.port, "game_port": self.game_port,
                "console": f"http://{authority(self.host, self.port)}{ADMIN_ROOT}/"}


def error_status(error: BaseException) -> int:
    status = int(error.status) if isinstance(error, ProtocolError) else HTTPStatus.INTERNAL_SERVER_ERROR
    return status if status in ERROR_STATUSES else HTTPStatus.INTERNAL_SERVER_ERROR


def error_response(error: BaseException, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": error_payload(error)}, status_code=error_status(error), headers=headers)


def server_time(now: datetime) -> dict:
    return {"utc": now.astimezone(timezone.utc).isoformat(), "jst": now.astimezone(JST).isoformat()}


def admin_page(page: AdminPage) -> dict:
    return {"items": [dict(item) for item in page.items], "next_cursor": page.next_cursor}


def parse_model(model: type[ModelT], raw: bytes | str) -> ModelT:
    try:
        return model.model_validate_json(raw)
    except ValidationError as error:
        raise AdminError("admin_params_invalid", "The request parameters are invalid", HTTPStatus.UNPROCESSABLE_ENTITY,
                         fields=field_errors(error)) from error


def request_body(request: Request, model: type[ModelT]) -> ModelT:
    raw = getattr(request.state, BODY_STATE, None)
    if not isinstance(raw, bytes):
        raise AdminError("admin_body_unchecked", "The request body did not pass the admin security checks",
                         HTTPStatus.INTERNAL_SERVER_ERROR)
    return parse_model(model, raw)


def request_op_id(request: Request) -> str | None:
    document = getattr(request.state, DOCUMENT_STATE, None)
    try:
        return require_op_id(document.get("op_id") if isinstance(document, dict) else None)
    except AdminError:
        return None


@dataclass(frozen=True)
class AdminServices:
    runtime: GenerationRuntime
    generations: LeasedRuntime
    store: Store
    catalog: AdminCatalog
    pipeline: CommitPipeline
    preflight: Preflight
    restart_check: RestartCheck
    preflight_domains: frozenset[str]
    gate: ClientGate
    data_dir: Path
    endpoint: AdminEndpoint

    @property
    def backup_directory(self) -> Path:
        return self.data_dir / BACKUP_DIRECTORY

    @property
    def export_directory(self) -> Path:
        return self.data_dir / EXPORT_DIRECTORY

    def lease(self):
        return self.generations.lease()

    def current(self) -> LeasedGeneration:
        return LeasedGeneration(self.runtime.current)

    def checks(self) -> dict:
        return {"preflight_noop": self.preflight.is_noop, "restart_check_noop": self.restart_check.is_noop}

    def record_failure(self, kind: str, error: BaseException, op_id: str | None) -> None:
        payload = error_payload(error)
        try:
            self.store.record_admin_failure(kind, payload["code"], error_status(error), payload["message"],
                                            op_id=op_id, detail=payload)
        except Exception:
            # The failure log is diagnostic; it must never replace the error the operator is about to see.
            log.exception("Admin failure record could not be written: %s", kind)

    @contextmanager
    def recording_failures(self, kind: str, request: Request) -> Iterator[None]:
        try:
            yield
        except Exception as error:
            self.record_failure(kind, error, request_op_id(request))
            raise


@dataclass(frozen=True)
class ViewRequest:
    services: AdminServices
    profile: PlayerProfile
    generation: LeasedGeneration
    now: datetime

    @property
    def state(self) -> dict:
        return self.profile.state

    @property
    def revision(self) -> int:
        return self.profile.revision


async def _protocol_error(request: Request, error: ProtocolError) -> JSONResponse:
    return error_response(error)


async def _http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
    code = HTTP_ERROR_CODES.get(error.status_code, f"admin_http_{error.status_code}")
    return error_response(AdminError(code, str(error.detail), error.status_code), error.headers)


async def _validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
    fields = [{"loc": list(item.get("loc", ())), "type": item.get("type", ""), "msg": item.get("msg", "")}
              for item in error.errors()]
    return error_response(AdminError("admin_params_invalid", "The request parameters are invalid",
                                     HTTPStatus.UNPROCESSABLE_ENTITY, fields=fields))


def ui_pages(directory: Path) -> list[dict]:
    pages = directory / PAGES_DIRECTORY
    if not pages.is_dir():
        return []
    return [{"name": path.stem, "module": f"{STATIC_ROOT}/{PAGES_DIRECTORY}/{path.name}"}
            for path in sorted(pages.glob("*" + PAGE_SUFFIX))
            if PAGE_NAME.fullmatch(path.stem) and path.is_file() and not path.is_symlink()]


def _ui_missing() -> AdminError:
    return AdminError(UI_MISSING, "The console pages are not installed", HTTPStatus.NOT_FOUND)


class ConsoleFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except OSError as error:
            # StaticFiles turns only missing or overlong names into 404; Windows rejects names like a|b with other errors.
            raise StarletteHTTPException(HTTPStatus.NOT_FOUND) from error


def _mount_ui(application: FastAPI, directory: Path) -> None:
    @application.get(ADMIN_ROOT + "/", include_in_schema=False)
    def console() -> FileResponse:
        try:
            index = checked_path(directory / INDEX_FILE, directory)
        except ValueError as error:
            raise _ui_missing() from error
        if not index.is_file():
            raise _ui_missing()
        return FileResponse(index, media_type="text/html")

    @application.get(API_ROOT + "/ui-pages")
    def pages() -> dict:
        return {"pages": ui_pages(directory)}

    if directory.is_dir():
        application.mount(STATIC_ROOT, ConsoleFiles(directory=directory), name="admin-static")
        return

    @application.get(STATIC_ROOT + "/{path:path}", include_in_schema=False)
    def missing(path: str) -> None:
        raise _ui_missing()


def _claim(routes: list, prefix: str, owner: str, claimed: dict[tuple[str, str], str]) -> None:
    # Discovered modules never edit a shared list, so a repeated path would otherwise shadow another module silently.
    for route in routes:
        for method in sorted(getattr(route, "methods", None) or ()):
            key = (prefix + route.path, method)
            if key in claimed:
                raise ValueError(f"Admin route {owner} repeats {method} {key[0]} from {claimed[key]}")
            claimed[key] = owner


def create_admin_app(runtime: GenerationRuntime, store: Store, *, data_dir: Path, admin_host: str, admin_port: int,
                     game_port: int, token: str, preflight: Preflight, restart_check: RestartCheck, plan_store: PlanStore,
                     preflight_domains: Mapping[str, Any] = NO_PREFLIGHT_DOMAINS, catalog: AdminCatalog | None = None,
                     ui_directory: Path = UI_DIRECTORY, clock: Callable[[], float] = time.monotonic) -> FastAPI:
    if not isinstance(runtime, GenerationRuntime) or not isinstance(store, Store):
        raise ValueError("The admin console needs the generation runtime and the local store")
    data_dir = Path(data_dir).resolve()
    if data_dir != store.path.parent:
        raise ValueError("The admin data directory must be the directory of the store database")
    endpoint = AdminEndpoint(require_bind_host(admin_host), require_port(admin_port), require_port(game_port))
    if endpoint.port == endpoint.game_port:
        raise ValueError("The admin console needs a port of its own")
    require_token(token)
    catalog = discover(preflight_domains) if catalog is None else catalog
    if not isinstance(catalog, AdminCatalog):
        raise ValueError("The admin catalog must come from admin module discovery")
    generations = LeasedRuntime(runtime)
    pipeline = CommitPipeline(store, plan_store, preflight, restart_check, generations,
                              backup_directory=data_dir / BACKUP_DIRECTORY)
    services = AdminServices(runtime=runtime, generations=generations, store=store, catalog=catalog, pipeline=pipeline,
                             preflight=preflight, restart_check=restart_check,
                             preflight_domains=frozenset(preflight_domains), gate=ClientGate(runtime, clock=clock),
                             data_dir=data_dir, endpoint=endpoint)
    application = FastAPI(title=TITLE, docs_url=None, redoc_url=None, openapi_url=None)
    application.state.admin = services
    application.add_middleware(AdminSecurityMiddleware, token=token, port=endpoint.port)
    application.add_exception_handler(ProtocolError, _protocol_error)
    application.add_exception_handler(StarletteHTTPException, _http_error)
    application.add_exception_handler(RequestValidationError, _validation_error)
    _mount_ui(application, Path(ui_directory))
    claimed: dict[tuple[str, str], str] = {}
    _claim([route for route in application.routes if route.path.startswith(API_ROOT)], "", TITLE, claimed)
    for route in catalog.routes:
        router = route.build(services)
        if not isinstance(router, APIRouter):
            raise ValueError(f"Admin route {route.name} must build an APIRouter")
        _claim(router.routes, API_ROOT, route.name, claimed)
        application.include_router(router, prefix=API_ROOT)
    return application
