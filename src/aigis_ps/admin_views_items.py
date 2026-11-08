from __future__ import annotations

from http import HTTPStatus
from typing import Any, Literal

from .admin_cmd_economy import ITEMS, POSSESSIONS, PRESENTS, ItemRoutes, classifier, item_routes
from .admin_command import AdminError
from .admin_commit import attempt
from .admin_registry import ViewRegistry
from .admin_views import PageParams, ViewContext, page, redacted_view, service
from .admin_views_units import checked
from .item_classes import ItemClass, ItemClasses


HOLDING_COLUMNS = ("ItemID", "Count", "Enabled")
CLASSES_UNAVAILABLE = "admin_item_classes_unavailable"
ItemClassName = Literal[tuple(item_class.value for item_class in ItemClass)]


class CatalogParams(PageParams):
    item_class: ItemClassName | None = None


def holdings(possessions: Any, state: dict) -> list[dict]:
    # The startup listing carries Enabled on every row, as 129 and 137 send it to the client.
    columns = {column.name: column.values for column in possessions.startup_response(state).children}
    return [dict(zip(HOLDING_COLUMNS, values)) for values in zip(*(columns.get(name, ()) for name in HOLDING_COLUMNS))]


def item_definitions(bundle: Any) -> Any:
    return service(service(bundle, PRESENTS), "item_catalog")


def described(routes: ItemRoutes, classes: ItemClasses | None, item_id: str) -> dict:
    row = routes.catalog.rows.get(item_id)
    item_class = None if classes is None else classes.classify(item_id)
    return {"stack": None if row is None else row["Stack"], "class": None if item_class is None else item_class.value,
            "command": None if item_class is None else routes.route(item_id, item_class)}


def holdings_page(bundle: Any, state: dict, classes: ItemClasses | None, params: PageParams) -> dict:
    routes = item_routes(bundle, item_definitions(bundle))
    return page(holdings(service(bundle, POSSESSIONS), state), params,
                lambda row: {**row, **described(routes, classes, row["ItemID"])})


def holdings_view(context: ViewContext, params: PageParams) -> dict:
    state, bundle = context.state, context.generation.bundle
    classes, status = checked(lambda: classifier(bundle))
    return {"classes": status, "items": attempt(lambda: holdings_page(bundle, state, classes, params))}


def listed_items(catalog: Any, classes: ItemClasses | None, item_class: str | None) -> list[str]:
    if item_class is None:
        return sorted(catalog.rows)
    if classes is None:
        raise AdminError(CLASSES_UNAVAILABLE, "Filtering by item class needs the item classifier",
                         HTTPStatus.SERVICE_UNAVAILABLE)
    return sorted(classes.item_ids(item_class))


def catalog_page(bundle: Any, state: dict, classes: ItemClasses | None, params: CatalogParams) -> dict:
    routes = item_routes(bundle, item_definitions(bundle))
    counts, rows = service(bundle, POSSESSIONS).counts(state), routes.catalog.rows
    return page(listed_items(routes.catalog, classes, params.item_class), params, lambda item_id: {
        "ItemID": item_id, "EffectType": rows[item_id]["EffectType"], "ReceiveAction": rows[item_id]["ReceiveAction"],
        "held": counts.get(item_id, 0), **described(routes, classes, item_id)})


def catalog_view(context: ViewContext, params: CatalogParams) -> dict:
    state, bundle = context.state, context.generation.bundle
    classes, status = checked(lambda: classifier(bundle))
    return {"classes": status, "items": attempt(lambda: catalog_page(bundle, state, classes, params))}


VIEWS = (
    redacted_view(f"{ITEMS}.holdings", ITEMS, holdings_view, PageParams),
    redacted_view(f"{ITEMS}.catalog", ITEMS, catalog_view, CatalogParams),
)


def register(registry: ViewRegistry) -> None:
    for view in VIEWS:
        registry.add(view)
