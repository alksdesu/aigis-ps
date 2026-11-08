from __future__ import annotations

import hashlib

from .errors import ProtocolError
from .native_numbers import int32
from .player_state import MAX_PLAYER_UNITS, UNIT_GRANT_UNSUPPORTED


class TradeUnitGrants:
    def __init__(self, players):
        self.players = players
        self._initialization_version = players.initialization_version
        self.content_version = hashlib.sha256(("trade-units:3:" + self._initialization_version).encode()).hexdigest()

    def grant(self, state, product, count):
        if self.players.initialization_version != self._initialization_version:
            raise ProtocolError("trade_grant_content_changed", "Unit initialization changed after trade setup", 503)
        if (not int32(count) or not 1 <= count <= MAX_PLAYER_UNITS
                or not int32(product.product_type) or product.product_type != 1):
            raise ProtocolError("trade_grant_invalid", "Unit trade requires a positive bounded quantity")
        values = (product.unit_evolution_count, product.unit_skill_level, product.unit_skill_evolution,
                  product.unit_cost_modify, product.unit_init_trust, product.unit_base_level)
        if (any(not int32(value) for value in values)
                or product.unit_evolution_count not in (0, 2) or product.unit_skill_level not in (0, 1)
                or product.unit_skill_evolution != 0 or product.unit_cost_modify != 0 or product.unit_init_trust != 0
                or product.unit_base_level < 0):
            raise ProtocolError("trade_grant_unsupported", "Product uses an unverified unit initialization variant", 501)
        self.players.require_unit_space(state, count)
        try:
            return self.players.credit_units(state, [{"card_id": product.card_id, "level": product.unit_base_level,
                "count": count, "evolution_count": product.unit_evolution_count}])
        except ProtocolError as error:
            if error.code == UNIT_GRANT_UNSUPPORTED:
                raise ProtocolError("trade_grant_unsupported", "Product uses an unverified unit initialization variant", 501) from error
            raise
