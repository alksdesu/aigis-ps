from __future__ import annotations

from http import HTTPStatus
from typing import Any

from .admin_command import AdminError
from .admin_commit import attempt
from .admin_registry import ViewRegistry
from .admin_views import ViewContext, redacted_view, service


BATTLE = "battle"
QUESTS = "quests"
NO_CATALOG = "This generation has no subjugation catalog"


def _catalog(bundle: Any):
    catalog = getattr(service(bundle, QUESTS), "subjugation", None)
    if catalog is None:
        raise AdminError("admin_view_service_unavailable", NO_CATALOG, HTTPStatus.SERVICE_UNAVAILABLE)
    return catalog


def status_view(context: ViewContext, params: None) -> dict:
    catalog = _catalog(context.generation.bundle)
    return {"subjugation": attempt(lambda: catalog.status(context.now))}


def progress_view(context: ViewContext, params: None) -> dict:
    catalog = _catalog(context.generation.bundle)
    return {"progress": attempt(lambda: catalog.describe(context.state, context.now))}


VIEWS = (
    # The catalog and the save it describes follow the running generation, so both answers change without a revision.
    redacted_view("battle.subjugation", BATTLE, status_view, live=True),
    redacted_view("battle.subjugation_progress", BATTLE, progress_view, live=True),
)


def register(registry: ViewRegistry) -> None:
    for view in VIEWS:
        registry.add(view)
