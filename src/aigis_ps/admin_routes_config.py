from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import Field

from .admin_app import AdminServices, ConfirmPhrase, request_body
from .admin_command import CommandParams
from .admin_config_switch import CONFIG_KIND, MAX_CONFIRMATIONS, SwitchMode, config_switch
from .admin_registry import AdminRoute, RouteRegistry
from .config_registry import REGISTRY
from .config_sets import SET_ID_LENGTH


ROUTE_NAME = "config"
SET_ID_PATTERN = rf"^[a-f0-9]{{{SET_ID_LENGTH}}}$"
REPORT_ID_CHARS = 64


class ValidationRequest(CommandParams):
    changes: dict[str, dict[str, Any] | None] = Field(default_factory=dict, max_length=len(REGISTRY))
    base_set: str | None = Field(default=None, pattern=SET_ID_PATTERN)
    op_id: str


class SwitchConfirmation(CommandParams):
    client_closed: bool = False
    confirmations: list[ConfirmPhrase] = Field(default_factory=list, max_length=MAX_CONFIRMATIONS)


class SwitchRequest(CommandParams):
    report_id: str = Field(max_length=REPORT_ID_CHARS)
    op_id: str
    mode: SwitchMode | None = None
    confirm: SwitchConfirmation = Field(default_factory=SwitchConfirmation)


class UnstageRequest(CommandParams):
    staged_set: str = Field(pattern=SET_ID_PATTERN)
    op_id: str


def register(registry: RouteRegistry) -> None:
    registry.add(AdminRoute(ROUTE_NAME, build))


def build(services: AdminServices) -> APIRouter:
    router = APIRouter()

    @router.get("/config")
    def config(request: Request) -> dict:
        return config_switch(request.app).describe()

    @router.post("/config/validations")
    def validations(request: Request) -> dict:
        body = request_body(request, ValidationRequest)
        return config_switch(request.app).validate(body.changes, base_set=body.base_set, op_id=body.op_id)

    @router.get("/jobs/{job_id}")
    def job(job_id: str, request: Request) -> dict:
        return config_switch(request.app).job(job_id)

    @router.post("/config/switches")
    def switches(request: Request) -> dict:
        with services.recording_failures(CONFIG_KIND, request):
            body = request_body(request, SwitchRequest)
            return config_switch(request.app).switch(report_id=body.report_id, op_id=body.op_id, mode=body.mode,
                                                      client_closed=body.confirm.client_closed,
                                                      confirmations=tuple(body.confirm.confirmations))

    @router.post("/config/unstage")
    def unstage(request: Request) -> dict:
        with services.recording_failures(CONFIG_KIND, request):
            body = request_body(request, UnstageRequest)
            return config_switch(request.app).unstage(staged_set=body.staged_set, op_id=body.op_id)

    return router
