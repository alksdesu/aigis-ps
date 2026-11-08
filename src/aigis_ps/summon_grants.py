from __future__ import annotations

from copy import deepcopy
import re
from types import MappingProxyType

from .errors import ProtocolError
from .native_numbers import int32
from .summon_pool_catalog import MAX_POOLS, SummonPool
from .unit_grants import MAX_GRANTS, UnitGrants


_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _invalid_result():
    raise ProtocolError("summon_ticket_result_invalid", "Unit grant changed unrelated state or ticket identity", 503)


class SummonGrants:
    def __init__(self, players, pools, enabled_pools):
        if (not isinstance(enabled_pools, (tuple, list)) or len(enabled_pools) > MAX_POOLS
                or any(not int32(value) or value <= 0 for value in enabled_pools)
                or len(set(enabled_pools)) != len(enabled_pools)):
            raise ValueError("Enabled ticket pools must be bounded distinct positive integers")
        self.players, self.pools = players, pools
        self._enabled_pools = tuple(sorted(enabled_pools))
        self._validated_pools, self._initial_values, self._rarities = {}, {}, {}
        self._pool_version = pools.content_version
        self.units = UnitGrants(players, error_prefix="summon_ticket")
        self._initialization_version = self.units.initialization_version
        if any(type(value) is not str or not _HASH.fullmatch(value)
               for value in (self.pool_version, self.initialization_version)):
            raise ProtocolError("summon_ticket_dependencies_invalid", "Ticket resources require fixed fingerprints", 503)
        self.ready()
        for pool_id in self.enabled_pools:
            self.validate_pool(pool_id)

    @property
    def enabled_pools(self):
        return self._enabled_pools

    @property
    def pool_version(self):
        return self._pool_version

    @property
    def initialization_version(self):
        return self._initialization_version

    @property
    def validated_pools(self):
        return MappingProxyType(self._validated_pools)

    @property
    def initial_values(self):
        return MappingProxyType(self._initial_values)

    def ready(self):
        self.units.ready()
        if (not self.pools.is_ready() or self.pools.content_version != self.pool_version
                or self.players is not self.units.players):
            raise ProtocolError("summon_ticket_dependencies_changed", "Ticket resources changed after setup", 503)

    def validate_pool(self, pool_id: int) -> SummonPool:
        self.ready()
        if not int32(pool_id) or pool_id not in self.enabled_pools:
            raise ProtocolError("summon_ticket_pool_disabled", "This ticket pool is not enabled", 501)
        pool = self.pools.require_complete(pool_id)
        if (not isinstance(pool, SummonPool) or pool.pool_id != pool_id or not pool.candidates
                or not isinstance(pool.candidates, tuple) or pool.complete is not True):
            raise ProtocolError("summon_ticket_pool_invalid", "Ticket pool identity is invalid", 503)
        if pool_id in self._validated_pools:
            if self._validated_pools[pool_id] != pool:
                raise ProtocolError("summon_ticket_dependencies_changed", "Ticket candidates changed after validation", 503)
            return self._validated_pools[pool_id]
        resolved = self.units.initial_batch([{"card_id": candidate.card_id, "level": candidate.level,
                                             "evolution_count": candidate.evolution_count}
                                            for candidate in pool.candidates])
        self.ready()
        if self.pools.require_complete(pool_id) != pool:
            raise ProtocolError("summon_ticket_dependencies_changed", "Ticket candidates changed during validation", 503)
        self._validated_pools[pool_id] = deepcopy(pool)
        self._initial_values[pool_id] = tuple(resolved)
        return self._validated_pools[pool_id]

    def _expected(self, pool_id, candidate_index, get_index, unique_id):
        pool = self._validated_pools[pool_id]
        if (not int32(candidate_index) or not 0 <= candidate_index < len(pool.candidates)
                or type(pool.candidates[candidate_index].weight) is not int or pool.candidates[candidate_index].weight <= 0
                or not int32(get_index) or get_index <= 0 or not int32(unique_id) or unique_id <= 0):
            _invalid_result()
        candidate = pool.candidates[candidate_index]
        initial = self._initial_values[pool_id][candidate_index]
        return self.units.expected_unit(candidate.card_id, initial, get_index, unique_id)

    def expected_unit(self, pool_id, candidate_index, get_index, unique_id):
        self.validate_pool(pool_id)
        return self._expected(pool_id, candidate_index, get_index, unique_id)

    def candidate_rarities(self, pool_id):
        pool = self.validate_pool(pool_id)
        if pool_id not in self._rarities:
            verified = self.players.card_rarities()
            rarities = tuple(verified.get(candidate.card_id) for candidate in pool.candidates)
            if any(not int32(rare) or rare < 0 for rare in rarities):
                raise ProtocolError("summon_ticket_rarity_invalid", "Ticket candidates lack verified card rarities", 503)
            self._rarities[pool_id] = rarities
        return self._rarities[pool_id]

    def draw(self, pool_id, candidate_indices, randbelow):
        pool = self.validate_pool(pool_id)
        if (not isinstance(candidate_indices, tuple) or not candidate_indices
                or len(set(candidate_indices)) != len(candidate_indices)
                or any(not int32(index) or not 0 <= index < len(pool.candidates)
                       or type(pool.candidates[index].weight) is not int or pool.candidates[index].weight <= 0
                       for index in candidate_indices)):
            _invalid_result()
        if not callable(randbelow):
            raise ValueError("Summon sampling requires a callable random source")
        weights = tuple(pool.candidates[index].weight for index in candidate_indices)
        value = randbelow(sum(weights))
        if type(value) is not int or not 0 <= value < sum(weights):
            raise ProtocolError("summon_random_invalid", "Random source returned an invalid pool index", 503)
        for index, weight in zip(candidate_indices, weights):
            if value < weight:
                return index
            value -= weight
        _invalid_result()

    def grant(self, state, pool_id, candidate_indices):
        pool = self.validate_pool(pool_id)
        if (not isinstance(candidate_indices, (tuple, list)) or not 1 <= len(candidate_indices) <= MAX_GRANTS
                or any(not int32(index) or not 0 <= index < len(pool.candidates)
                       or type(pool.candidates[index].weight) is not int or pool.candidates[index].weight <= 0
                       for index in candidate_indices)):
            _invalid_result()
        grants = [{"card_id": pool.candidates[index].card_id, "level": pool.candidates[index].level,
                   "evolution_count": pool.candidates[index].evolution_count, "count": 1}
                  for index in candidate_indices]
        updated, added = self.units.grant(state, grants)
        self.validate_pool(pool_id)
        return deepcopy(updated), deepcopy(added)
