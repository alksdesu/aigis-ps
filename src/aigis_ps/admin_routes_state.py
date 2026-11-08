from __future__ import annotations

from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .admin_app import (AdminServices, ConfirmPhrase, Cursor, PageSize, ViewRequest, admin_page, parse_model,
                        request_body, server_time)
from .admin_command import MAX_BATCH, AdminCommand, AdminError, CommandParams
from .admin_commit import COMMAND_KIND, takes_backup
from .admin_diff import redact
from .admin_registry import AdminRoute, AdminView, RouteRegistry
from .admin_security import decode_json
from .storage import ADMIN_ACTOR, ADMIN_PAGE_SIZE


ROUTE_NAME = "state"
MAX_PLAN_ID_CHARS = 64
MAX_VIEW_PARAMS_CHARS = 8 * 1024
EMPTY_PARAMS = "{}"
WEAK_PREFIX = "W/"


class PlanRequest(CommandParams):
    commands: list[Any]


class CommitRequest(CommandParams):
    plan_id: str = Field(max_length=MAX_PLAN_ID_CHARS)
    plan_digest: str
    expected_revision: int
    op_id: str
    confirmations: list[ConfirmPhrase] = Field(default_factory=list, max_length=MAX_BATCH)


def register(registry: RouteRegistry) -> None:
    registry.add(AdminRoute(ROUTE_NAME, build))


def describe_command(command: AdminCommand, services: Any) -> dict:
    reason = command.availability(services)
    return {**command.describe(), "available": reason is None, "unavailable_reason": reason}


def view_params(view: AdminView, text: str | None) -> BaseModel | None:
    if view.params_model is None:
        if text is not None:
            raise AdminError("admin_params_invalid", f"The view {view.name} takes no parameters",
                             HTTPStatus.UNPROCESSABLE_ENTITY)
        return None
    raw = EMPTY_PARAMS if text is None else text
    decode_json(raw)
    return parse_model(view.params_model, raw)


def etag_matches(header: str | None, etag: str) -> bool:
    if header is None:
        return False
    candidates = {item.strip() for item in header.split(",")}
    return bool(candidates & {"*", etag, WEAK_PREFIX + etag})


def view_etag(view: AdminView, revision: int, generation_id: str) -> str | None:
    # Live answers follow the server clock or the admin tables, so they change while the revision and generation stay.
    return None if view.live else f'"{revision}:{generation_id}"'


def build(services: AdminServices) -> APIRouter:
    router = APIRouter()

    @router.get("/session")
    def session() -> dict:
        generation, profile = services.current(), services.store.profile()
        return {"actor": ADMIN_ACTOR, "admin": services.endpoint.describe(), "server_now": server_time(generation.now()),
                "player_id": profile.player_id, "revision": profile.revision, "generation_id": generation.id,
                "checks": services.checks(), "client": services.gate.status()}

    @router.get("/runtime")
    def runtime() -> dict:
        generation = services.runtime.current
        return {"runtime": services.runtime.status(), "identity": generation.identity.to_dict(),
                "gate_issues": list(generation.gate_issues), "readiness_domains": sorted(generation.readiness),
                "checks": services.checks(), "client": services.gate.status()}

    @router.get("/readiness/{domain}")
    def readiness(domain: str) -> dict:
        status = services.runtime.current.live_status()
        if domain not in status:
            raise AdminError("admin_readiness_unknown", "No readiness is reported for this domain", HTTPStatus.NOT_FOUND)
        return {"domain": domain, "readiness": redact(status[domain])}

    @router.get("/commands")
    def commands() -> dict:
        bundle, registry = services.current().bundle, services.catalog.commands
        return {"commands": [describe_command(command, bundle) for command in registry],
                "field_owners": dict(registry.field_owners())}

    @router.post("/plans")
    def plans(request: Request) -> dict:
        plan = services.pipeline.preview(services.catalog.commands.batch(request_body(request, PlanRequest).commands))
        return {**plan.describe(), "auto_backup": takes_backup(plan)}

    @router.post("/commits")
    def commits(request: Request) -> dict:
        with services.recording_failures(COMMAND_KIND, request):
            body = request_body(request, CommitRequest)
            outcome = services.pipeline.commit(plan_id=body.plan_id, plan_digest=body.plan_digest,
                                               expected_revision=body.expected_revision, op_id=body.op_id,
                                               confirmations=tuple(body.confirmations))
        return outcome.to_dict()

    @router.get("/audit")
    def audit(cursor: Cursor = None, limit: PageSize = ADMIN_PAGE_SIZE) -> dict:
        return redact(admin_page(services.store.list_admin_operations(cursor, limit)))

    @router.get("/audit/failures")
    def failures(cursor: Cursor = None, limit: PageSize = ADMIN_PAGE_SIZE) -> dict:
        return redact(admin_page(services.store.list_admin_failures(cursor, limit)))

    @router.get("/requests")
    def requests(limit: PageSize = ADMIN_PAGE_SIZE) -> dict:
        return {"requests": redact(services.store.recent_requests(limit))}

    @router.get("/views")
    def views() -> dict:
        return {"views": services.catalog.views.describe()}

    @router.get("/views/{name}")
    def view(name: str, request: Request,
             params: Annotated[str | None, Query(max_length=MAX_VIEW_PARAMS_CHARS)] = None) -> Response:
        entry = services.catalog.views.get(name)
        parsed = view_params(entry, params)
        with services.lease() as generation:
            profile = services.store.profile()
            etag = view_etag(entry, profile.revision, generation.id)
            if etag is not None and etag_matches(request.headers.get("if-none-match"), etag):
                return Response(status_code=HTTPStatus.NOT_MODIFIED, headers={"ETag": etag})
            now = generation.now()
            data = entry.read(ViewRequest(services, profile, generation, now), parsed)
        return JSONResponse({"view": name, "revision": profile.revision, "generation_id": generation.id,
                             "server_now": server_time(now), "data": data},
                            headers=None if etag is None else {"ETag": etag})

    return router
