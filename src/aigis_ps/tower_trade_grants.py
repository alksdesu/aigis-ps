from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from .errors import ProtocolError
from .native_numbers import INT32_MAX, int32
from .possessions import MAX_POSSESSION_ITEMS
from .tower_trade_catalog import TowerTradeProduct
from .unit_grants import MAX_GRANTS, UnitGrants, _same


_HASH = re.compile(r"[a-f0-9]{64}\Z")


class TowerTradeGrants:
    def __init__(self, players, possessions, catalog):
        self.players, self.possessions, self.catalog = players, possessions, catalog
        catalog._ready()
        if catalog.possessions is not possessions or not possessions.is_ready():
            raise ProtocolError("tower_trade_grant_dependencies_invalid", "Tower grants require the catalog inventory provider", 503)
        self.units = UnitGrants(players, error_prefix="tower_trade_grant")
        self._catalog_version = catalog.content_version
        self._item_version = possessions.asset_sha256
        if any(type(value) is not str or not _HASH.fullmatch(value)
               for value in (self._catalog_version, self._item_version)):
            raise ProtocolError("tower_trade_grant_dependencies_invalid", "Tower grants require fixed resource fingerprints", 503)
        self.units.initial_batch([{"card_id": product.card_id, "level": 0, "evolution_count": 0}
                                  for product in catalog.products if product.card_id and not product.unsupported_reason])
        self.content_version = hashlib.sha256(json.dumps({"schema": 1, "catalog": self._catalog_version,
            "items": self._item_version, "initialization": self.units.initialization_version,
            "unit_policy": {"level": 0, "evolution_count": 0}, "unit_limit": MAX_GRANTS},
            sort_keys=True).encode()).hexdigest()
        self._snapshot = (players, possessions, catalog, self.units, self.content_version)
        self._ready()

    def _ready(self):
        if (any(left is not right for left, right in zip(
                (self.players, self.possessions, self.catalog, self.units, self.content_version), self._snapshot))
                or self.catalog.possessions is not self.possessions
                or self.units.players is not self.players
                or self.catalog.content_version != self._catalog_version
                or self.possessions.asset_sha256 != self._item_version
                or not self.possessions.is_ready()):
            raise ProtocolError("tower_trade_grant_dependencies_changed", "Tower grant dependencies changed after setup", 503)
        self.catalog._ready()
        self.units.ready()

    def require_supported(self, product):
        self._ready()
        if (type(product) is not TowerTradeProduct or type(product.shop_id) is not int
                or self.catalog.by_id.get(product.shop_id) is not product):
            raise ProtocolError("tower_trade_grant_product_invalid", "Tower grants require the exact catalog product", 400)
        if product.unsupported_reason:
            raise ProtocolError("tower_trade_grant_unsupported", "Tower product delivery is not verified: " + product.unsupported_reason, 501)
        if product.card_id:
            self.units.initial_values(product.card_id, 0, 0)
        elif (not int32(product.item_stack) or product.item_stack <= 0
                or product.item_effect_type not in (4, 10)
                or self.possessions.stack(product.item_id) != product.item_stack):
            raise ProtocolError("tower_trade_grant_unsupported", "Tower items require verified positive-stack delivery", 501)

    def apply(self, state, product, quantity):
        self.require_supported(product)
        if not isinstance(state, dict) or not int32(quantity) or quantity <= 0:
            raise ProtocolError("tower_trade_grant_invalid", "Tower grants require a state and a positive int32 quantity", 400)
        if product.card_id:
            if quantity > MAX_GRANTS:
                raise ProtocolError("tower_trade_grant_invalid", "Tower unit grants exceed the supported instance count", 400)
            updated, units = self.units.grant(state, [{"card_id": product.card_id, "level": 0,
                                                      "evolution_count": 0, "count": quantity}])
            self._ready()
            return updated, units, []
        original = deepcopy(state)
        counts = self.possessions.counts(original)
        current = counts.get(product.item_id)
        total = (current or 0) + quantity
        if total > INT32_MAX or total > product.item_stack:
            raise ProtocolError("possession_stack_exceeded", "Possession grant exceeds the local stack or int32 limit")
        if current is None and len(counts) >= MAX_POSSESSION_ITEMS:
            raise ProtocolError("possession_grant_invalid", "Possession grant exceeds the local item limit")
        expected = deepcopy(original)
        expected_rows = expected["tables"]["possessions"]
        if current is None:
            expected_rows.append({"ItemID": product.item_id, "Count": total})
        else:
            next(row for row in expected_rows if row["ItemID"] == product.item_id)["Count"] = total
        updated = self.possessions.credit(deepcopy(original), {product.item_id: quantity})
        if not _same(updated, expected):
            raise ProtocolError("tower_trade_grant_result_invalid", "Tower item credit changed unrelated state or the selected quantity", 503)
        self.possessions.counts(updated)
        self._ready()
        return deepcopy(updated), [], [product.item_id]

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        unavailable = {product.shop_id: product.unsupported_reason for product in self.catalog.products
                       if product.unsupported_reason}
        return {"ready": error is None, "error": error, "content_version": self.content_version,
            "supported_products": len(self.catalog.products) - len(unavailable), "unavailable": unavailable,
            "unit_quantity_limit": MAX_GRANTS,
            "unit_initialization_policy": "Local default CardData initialization at level 0 and evolution count 0",
            "item_delivery_policy": "Direct POSSESSION credit within verified positive Stack; no mailbox fallback",
            "native_tested": False}
