from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
from itertools import combinations
import json
import logging
import re
from types import MappingProxyType

from .alxml import Column, Container, dumps, loads
from .errors import REFUSAL_AFTERMATH as _REFUSAL_AFTERMATH, REFUSAL_POLICY, ProtocolError
from .evolution_protocol import EvolutionRequest, parse_evolution
from .native_numbers import int32
from .player_state import DISPLAY_FIELD, SKILL_SWITCH_FLAG, barracks_index
from .protocol import ProtocolRegistry, encode_response
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .training_service import GROWTH_MATERIAL_POLICY, MaterialsProtected
from .unit_reference import (BARRACKS_STORAGE, FAIRY_FARM_STORAGE, FIRST_BARRACKS, SILVER_HOUSE_STORAGE,
                             debit_stored_materials, resolve_barracks_material)
from .unit_removal import consume_units, refused_protections
from .wallet_money import NATIVE_BALANCE_MESSAGE, MoneyRefusal, MoneyRefusals, money_after_debit


FIRST_AWAKENING, SKILL_AWAKENING, SECOND_AWAKENING = 7, 8, 9
EVOLUTION_FIELDS = {FIRST_AWAKENING: {"ClassID": "A2", "ClassChangeCount": "A3", "Exp": "A4", DISPLAY_FIELD: "B0"},
                    SKILL_AWAKENING: {"SettingSkillID": "AD", "GeneralPurposeFlags": "AE", "SkillEvolved": "B1"},
                    SECOND_AWAKENING: {"ClassID": "A2", "ClassChangeCount": "A3", "Exp": "A4", DISPLAY_FIELD: "B0"}}
RESPONSE_LIMIT = 16384
HISTORY_NAMESPACE = "evolution_history"
# A receipt may predate any of these; the digest records each one as None while it is missing.
OPTIONAL_SOURCES = ("fairy_farm", "silver_house", "rewards", "barracks")
EVOLUTION_MONEY_REFUSALS = MoneyRefusals(
    insufficient=MoneyRefusal("evolution_money_insufficient", "Evolution costs exceed the saved money balance"),
    native_balance=MoneyRefusal("evolution_native_balance", NATIVE_BALANCE_MESSAGE))
_HASH = re.compile(r"[a-f0-9]{64}\Z")
log = logging.getLogger("aigis_ps")


def _receipt_operation_valid(entry):
    return type(entry["operation"]) is int and entry["operation"] in EVOLUTION_FIELDS


@dataclass(frozen=True)
class EvolutionSelection:
    player: dict
    target: dict
    materials: tuple[dict, ...]
    ordinary: tuple[dict, ...]
    stored: Mapping[int, Mapping[int, int]]


@dataclass(frozen=True)
class EvolutionOutcome:
    state: dict
    before: dict
    after: dict
    price: int
    money: int
    costs: Mapping[str, int]
    stored: Mapping[int, Mapping[int, int]]
    consumed: tuple[dict, ...]
    rewards: tuple[Container, ...]


class EvolutionService:
    def __init__(self, bootstrap, players, catalog, gifts, *, fairy_farm=None, silver_house=None, rewards=None, barracks=None):
        self.bootstrap, self.players, self.catalog, self.gifts = bootstrap, players, catalog, gifts
        self.possessions = catalog.possessions
        self.fairy_farm, self.silver_house = fairy_farm, silver_house
        self.rewards = rewards
        self.barracks = barracks
        self.missions = None
        registry = ProtocolRegistry.load()
        self._unit_profile = registry.by_registration[41]
        self._present_profile = registry.by_registration[42]
        self._warehouse_profiles = {SILVER_HOUSE_STORAGE: registry.by_registration[126],
                                    FAIRY_FARM_STORAGE: registry.by_registration[122]}
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "evolution_history_invalid", "Stored evolution receipts are invalid",
                                     fields=("request", "content", "revision", "response", "operation"),
                                     response_limit=RESPONSE_LIMIT, check=_receipt_operation_valid)

    def handlers(self):
        handler = self.evolve
        operations = (FIRST_AWAKENING, SKILL_AWAKENING) + ((SECOND_AWAKENING,) if self.rewards is not None else ())
        return {number: handler for number in operations}

    def status(self):
        return {"implemented_operations": sorted(self.handlers()), "catalog": self.catalog.status(), "native_tested": False,
                "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "replay_policy": ("Exact request at its committed revision and content version, also once optional "
                                  "sources absent at its commit attach later, including those nested in the catalog "
                                  "or the fairy farm"),
                "request_order": REQUEST_ORDER,
                "refusals": REFUSAL_POLICY,
                "warehouse_refresh": "Authoritative stock is available through 122/126; no unregistered warehouse response",
                "rewards": self.rewards.status() if self.rewards is not None else {"available": False},
                "extra_barracks_materials": self.barracks is not None,
                "native_exclusions": ["Additional barrack targets, which the native target menus never select"],
                "unsupported": ([] if self.barracks is not None else ["Additional barrack materials"])
                    + ([] if self.rewards is not None else ["Second awakening reward mail"])}

    def _content_version(self, absent=frozenset(), catalog=None, fairy=None):
        sources = {"catalog": self.catalog.content_version if catalog is None else catalog,
                   "possessions": self.possessions.asset_sha256, "gifts": self.gifts.content_version}
        for name in OPTIONAL_SOURCES:
            source = None if name in absent else getattr(self, name)
            sources[name] = None if source is None else source.content_version
        if fairy is not None and sources["fairy_farm"] is not None:
            sources["fairy_farm"] = fairy
        if any(value is not None and (not isinstance(value, str) or not _HASH.fullmatch(value)) for value in sources.values()):
            raise ProtocolError("evolution_dependencies_invalid", "Evolution requires verified content identities", 503)
        if any(sources[key] is None for key in ("catalog", "possessions", "gifts")):
            raise ProtocolError("evolution_dependencies_invalid", "Evolution content identity is unavailable", 503)
        return hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()

    def _replayable(self, committed, current):
        # A receipt keeps the digest of its commit, which sources attached since then, even nested, cannot reach.
        if committed == current:
            return True
        attached = [name for name in OPTIONAL_SOURCES if getattr(self, name) is not None]
        catalogs = self.catalog.replayable_content_versions()
        fairies = (None,) if self.fairy_farm is None else tuple(self.fairy_farm.replayable_content_versions())
        return any(self._content_version(frozenset(absent), catalog, fairy) == committed
                   for size in range(len(attached) + 1) for absent in combinations(attached, size)
                   for catalog in catalogs
                   for fairy in ((None,) if "fairy_farm" in absent else fairies))

    def _require_operation(self, operation):
        if operation == SECOND_AWAKENING and self.rewards is None:
            raise ProtocolError("evolution_operation_unsupported", "Second awakening reward mail is not implemented", 501)

    def _warehouse(self, storage_type):
        return {SILVER_HOUSE_STORAGE: self.silver_house, FAIRY_FARM_STORAGE: self.fairy_farm}.get(storage_type)

    def _sources(self, state, request):
        self.possessions.response(state, ())
        for storage_type in {reference.storage_type for reference in request.materials} - {BARRACKS_STORAGE}:
            warehouse = self._warehouse(storage_type)
            if warehouse is None:
                raise ProtocolError("evolution_storage_unsupported", "This evolution warehouse is not implemented", 501)
            warehouse.read(state)

    @staticmethod
    def _owned(reference, owned):
        if reference.storage_type != BARRACKS_STORAGE or reference.storage_index != FIRST_BARRACKS:
            raise ProtocolError("evolution_storage_unsupported", "This evolution storage domain is not implemented", 501)
        unit = owned.get(reference.invite_id)
        if unit is None or unit["UniqueID"] != reference.unique_id or unit["CardID"] != reference.card_id:
            raise ProtocolError("evolution_unit_missing", "Evolution identity does not match an owned unit", 409)
        if barracks_index(unit) != FIRST_BARRACKS:
            raise ProtocolError("evolution_storage_unsupported", "Evolution only supports the first barracks", 501)
        return unit

    def _material(self, reference, owned, state):
        if self.barracks is None:
            return self._owned(reference, owned)
        return resolve_barracks_material(reference, owned, state, self.barracks, operation="evolution")

    @staticmethod
    def protect(player: Mapping, target: Mapping, materials: Iterable[Mapping]) -> None:
        materials = tuple(materials)
        protected = refused_protections(player, materials, GROWTH_MATERIAL_POLICY)
        if protected or any(material["GetIndex"] == target["GetIndex"] for material in materials):
            raise MaterialsProtected("evolution_material_protected", "A selected evolution material is protected", protected)

    @staticmethod
    def _quote(quote, target, operation, count):
        try:
            expected_count = 1 if operation == SKILL_AWAKENING else 4
            fields = EVOLUTION_FIELDS[operation]
            if (not int32(quote.price) or quote.price <= 0 or not isinstance(quote.values, Mapping)
                    or set(quote.values) != set(fields) or any(not int32(value) for value in quote.values.values())
                    or not isinstance(quote.items, Mapping) or any(not isinstance(name, str) or not name
                        or not int32(value) or value <= 0 for name, value in quote.items.items())
                    or (quote.reward_card_id is not None
                        and (operation != SECOND_AWAKENING or quote.reward_card_id != target["CardID"]))
                    or count != expected_count
                    or not isinstance(quote.material_assignment, tuple)
                    or any(type(index) is not int for index in quote.material_assignment)
                    or sorted(quote.material_assignment) != list(range(count))):
                raise ValueError("Invalid evolution quote")
            values = dict(quote.values)
            if operation in (FIRST_AWAKENING, SECOND_AWAKENING):
                if (values["ClassID"] <= 0 or values["Exp"] < 0
                        or values[DISPLAY_FIELD] != 0):
                    raise ValueError("Invalid awakening values")
                if (operation == FIRST_AWAKENING and values["ClassChangeCount"] != 2
                        or operation == SECOND_AWAKENING and (values["ClassChangeCount"] not in (3, 4)
                                                              or values["Exp"] != target["Exp"])):
                    raise ValueError("Invalid awakening stage or experience")
            elif (values["SettingSkillID"] <= 0 or values["SkillEvolved"] != 1
                    or values["GeneralPurposeFlags"] != target["GeneralPurposeFlags"] | SKILL_SWITCH_FLAG):
                raise ValueError("Invalid skill awakening values")
            return quote.price, dict(quote.items), values
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ProtocolError("evolution_result_invalid", "Evolution calculation produced an invalid result", 503) from error

    def select(self, state: dict, request: EvolutionRequest) -> EvolutionSelection:
        player = self.players.validate(state)
        self._sources(state, request)
        owned = {unit["GetIndex"]: unit for unit in player["units"]}
        target = self._owned(request.base, owned)
        materials, ordinary = [], []
        stored = {SILVER_HOUSE_STORAGE: Counter(), FAIRY_FARM_STORAGE: Counter()}
        for reference in request.materials:
            if reference.storage_type != BARRACKS_STORAGE:
                materials.append(self._warehouse(reference.storage_type).material(reference.card_id))
                stored[reference.storage_type][reference.card_id] += 1
            else:
                material = self._material(reference, owned, state)
                materials.append(material)
                ordinary.append(material)
        return EvolutionSelection(player, target, tuple(materials), tuple(ordinary), MappingProxyType(
            {storage_type: MappingProxyType(dict(counts)) for storage_type, counts in stored.items()}))

    def awaken(self, state: dict, revision: int, request: EvolutionRequest, *, now: datetime | None,
               system: Container | None = None) -> EvolutionOutcome:
        self._require_operation(request.operation)
        selection = self.select(state, request)
        target = selection.target
        self.protect(selection.player, target, selection.ordinary)
        quote = self.catalog.quote(deepcopy(target), deepcopy(list(selection.materials)), request.operation, request.branch)
        price, costs, values = self._quote(quote, target, request.operation, len(selection.materials))
        money = money_after_debit(state, price, refusals=EVOLUTION_MONEY_REFUSALS)
        updated = self.gifts.preserve_history(state, revision)
        updated = self.possessions.debit(updated, costs)
        for storage_type, counts in selection.stored.items():
            if counts:
                updated = debit_stored_materials(self._warehouse(storage_type), updated, dict(counts),
                                                 storage_type=storage_type, label="Evolution",
                                                 registration=request.operation)
        updated["tables"]["game_param"][0]["Money"] = money
        consume_units(updated["player_state"], selection.ordinary)
        after = next(unit for unit in updated["player_state"]["units"] if unit["GetIndex"] == target["GetIndex"])
        after.update(values)
        rewards = ()
        if request.operation == SECOND_AWAKENING:
            updated, rewards = self.rewards.apply(state, updated, target["CardID"])
            after = next(unit for unit in updated["player_state"]["units"] if unit["GetIndex"] == target["GetIndex"])
            # The client lists the reward mail through 42 next; callers without a session check that listing themselves.
            if system is not None:
                encode_response(self._present_profile, dumps(Container(children=(
                    system, self.rewards.presents.response(updated)))))
        if self.missions is not None:
            updated = self.missions.record(updated, "evolution", {
                "before": target, "after": after, "operation": request.operation}, now)
        self.players.validate(updated)
        return EvolutionOutcome(updated, dict(target), dict(after), price, money, costs, selection.stored,
                                tuple(dict(unit) for unit in selection.ordinary), rewards)

    def evolve(self, envelope, store):
        try:
            return self._evolve(envelope, store)
        except ProtocolError as error:
            log.warning("Evolution %d refused with %s; the native client %s",
                        envelope.profile.registration_keys[0], error.code, _REFUSAL_AFTERMATH)
            raise

    def _evolve(self, envelope, store):
        request = parse_evolution(envelope)
        self._require_operation(request.operation)
        request_key = hashlib.sha256(envelope.profile.name.encode() + b"\0" + envelope.xml).hexdigest()
        now = self.bootstrap.clock() if self.missions is not None else None
        for _ in range(3):
            profile = self.players.authenticate(envelope, store)
            # A damaged save must be refused the same way whether or not the request has a receipt.
            self.players.validate(profile.state)
            self.bootstrap._player_row(profile)
            content = self._content_version()
            self._sources(profile.state, request)
            history = self._ledger.read(profile.state, profile.revision)
            receipt = self._ledger.receipt(history, request_key)
            if receipt is not None:
                if (receipt["revision"] != profile.revision or not self._replayable(receipt["content"], content)
                        or receipt["operation"] != request.operation):
                    raise ProtocolError("evolution_retry_stale", "Evolution state or content changed after this request", 409)
                response = receipt["response"].encode("utf-8")
                try:
                    loads(response)
                    encode_response(envelope.profile, response)
                except (ValueError, ProtocolError) as error:
                    raise ProtocolError("evolution_history_invalid", "Saved evolution response is invalid", 503) from error
                return response
            if self._ledger.processed(history, request_key):
                raise ProtocolError("evolution_request_stale", "Evolution request was already processed", 409)
            outcome = self.awaken(profile.state, profile.revision, request, now=now,
                                  system=self.bootstrap._system(profile))
            updated = outcome.state
            self.bootstrap.player_row(updated)
            encode_response(self._unit_profile, self.players.unit_response(updated["player_state"]["units"]))
            for storage_type, counts in outcome.stored.items():
                if counts:
                    encode_response(self._warehouse_profiles[storage_type], dumps(Container(children=(
                        self.bootstrap._system(profile), self._warehouse(storage_type).response(updated)))))
            children = (self.bootstrap._system(profile), Container("PGP", (
                Column("A1", "S", ("A1",)), Column("A2", "S", (str(outcome.money),)))),
                Container("PPU", (Column("A7", "I", (outcome.after["GetIndex"],)),
                    *(Column(alias, "I", (outcome.after[name],)) for name, alias in EVOLUTION_FIELDS[request.operation].items()))),
                self.possessions.response(updated, tuple(outcome.costs))) + outcome.rewards
            response = dumps(Container(children=children))
            encode_response(envelope.profile, response)
            if len(response) > RESPONSE_LIMIT:
                raise ProtocolError("evolution_response_capacity", "Evolution response exceeds the receipt limit", 503)
            self._ledger.record(updated, history, {"request": request_key, "content": content,
                                                   "revision": profile.revision + 1, "response": response.decode("utf-8"),
                                                   "operation": request.operation})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving evolution", 409)
