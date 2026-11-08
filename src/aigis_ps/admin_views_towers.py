from __future__ import annotations

from .admin_commit import attempt
from .admin_registry import ViewRegistry
from .admin_views import ViewContext, redacted_view, service


BATTLE = "battle"
TOWERS = "towers"


def overview_view(context: ViewContext, params: None) -> dict:
    towers = service(context.generation.bundle, TOWERS)
    return {"status": attempt(lambda: {"status": towers.status()}),
            "state": attempt(lambda: towers.describe(context.state, context.now))}


VIEWS = (
    # The tower day keys follow the frozen clock, so the answer changes without a revision.
    redacted_view("towers.overview", BATTLE, overview_view, live=True),
)


def register(registry: ViewRegistry) -> None:
    for view in VIEWS:
        registry.add(view)
