from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
import hashlib
import logging
import re
from types import MappingProxyType

from .alxml import Column, Container, dumps, loads
from .errors import REFUSAL_AFTERMATH as _REFUSAL_AFTERMATH, REFUSAL_POLICY, ProtocolError
from .native_numbers import int32
from .player_state import PRINCE_CARD_IDS, barracks_index
from .protocol import ProtocolRegistry, encode_response
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, SCHEMA_VERSION, RequestLedger
from .training_protocol import TrainingRequest, parse_training
from .unit_reference import (BARRACKS_STORAGE, FAIRY_FARM_STORAGE, FIRST_BARRACKS, SILVER_HOUSE_STORAGE,
                             debit_stored_materials, resolve_barracks_material)
from .unit_removal import Protection, RemovalPolicy, consume_units, refused_protections
from .wallet_money import NATIVE_BALANCE_MESSAGE, MoneyRefusal, MoneyRefusals, money_after_debit


INTEGRATION = 5
GROWTH_FIELDS = {"Exp": "A4", "SkillLevel": "A6", "CostModMixValue": "AA",
                 "SkillExperience": "B2", "CostExperience": "B3"}
RESPONSE_LIMIT = 16384
HISTORY_NAMESPACE = "training_history"
RECEIPT_FIELDS = ("request", "content", "revision", "response")
# Journals written before the shared ledger: v1 kept receipts only, v2 added a client-time watermark.
LEGACY_JOURNALS = {1: (frozenset(("schema_version", "receipts")), frozenset(RECEIPT_FIELDS)),
                   2: (frozenset(("schema_version", "receipts", "last_client_time", "seen_keys")),
                       frozenset((*RECEIPT_FIELDS, "client_time")))}
CONFIG_TABLE = "config"
# The native material menus hide the prince, locked units and squad members from every growth operation.
GROWTH_MATERIAL_POLICY = RemovalPolicy(frozenset({Protection.PRINCE, Protection.LOCKED, Protection.SQUAD}))
TRAINING_MONEY_REFUSALS = MoneyRefusals(
    insufficient=MoneyRefusal("training_money_insufficient", "Training costs exceed the saved money balance"),
    native_balance=MoneyRefusal("training_native_balance", NATIVE_BALANCE_MESSAGE))
_HASH = re.compile(r"[a-f0-9]{64}\Z")
log = logging.getLogger("aigis_ps")


class MaterialsProtected(ProtocolError):
    def __init__(self, code: str, message: str, protected: Mapping[int, tuple[Protection, ...]]):
        super().__init__(code, message, 409)
        self.protected = MappingProxyType(dict(protected))


@dataclass(frozen=True)
class TrainingSelection:
    player: dict
    target: dict
    materials: tuple[dict, ...]
    ordinary: tuple[dict, ...]
    silver: Mapping[int, int]
    fairy: Mapping[int, int]


@dataclass(frozen=True)
class TrainingOutcome:
    state: dict
    before: dict
    after: dict
    price: int
    money: int
    silver: Mapping[int, int]
    fairy: Mapping[int, int]
    consumed: tuple[dict, ...]


class TrainingService:
    def __init__(self, bootstrap, players, catalog, gifts, fairy_farm=None, silver_house=None, *, barracks=None):
        self.bootstrap, self.players, self.catalog, self.gifts = bootstrap, players, catalog, gifts
        self.fairy_farm = fairy_farm if fairy_farm is not None else getattr(catalog, "fairy_farm", None)
        self.silver_house = silver_house if silver_house is not None else getattr(catalog, "silver_house", None)
        self.barracks = barracks
        self.missions = None
        self._unit_profile = ProtocolRegistry.load().by_registration[41]
        self._silver_profile = ProtocolRegistry.load().by_registration[126]
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "training_history_invalid", "Stored training receipts are invalid",
                                     fields=RECEIPT_FIELDS, response_limit=RESPONSE_LIMIT)

    def handlers(self):
        return {INTEGRATION: self.integrate}

    def status(self):
        return {"implemented_operations": [INTEGRATION], "catalog": self.catalog.status(),
                "replay_policy": ("Exact request at its committed revision and content version, also once barracks "
                                  "rules or catalog warehouses absent at its commit attach later"),
                "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "request_order": REQUEST_ORDER, "native_tested": False,
                "refusals": REFUSAL_POLICY,
                "extra_barracks_materials": self.barracks is not None,
                "native_exclusions": ["Additional barrack targets, which the native target menus never select"],
                "unsupported": [] if self.barracks is not None else ["Additional barrack materials"],
                "silver_refresh": "Read 126 returns authoritative stock; native result-tag refresh paths remain unverified"}

    def _history(self, state, revision):
        if HISTORY_NAMESPACE in state:
            state = {HISTORY_NAMESPACE: self._ledger_form(state[HISTORY_NAMESPACE])}
        return self._ledger.read(state, revision)

    def _ledger_form(self, journal):
        version = journal.get("schema_version") if isinstance(journal, dict) else None
        shape = LEGACY_JOURNALS.get(version) if type(version) is int else None
        if shape is None or set(journal) != shape[0]:
            return journal
        receipts, seen, last = journal["receipts"], journal.get("seen_keys", []), journal.get("last_client_time")
        if (not isinstance(receipts, list) or not isinstance(seen, list) or len(seen) > PROCESSED_LIMIT
                or (last is None and seen) or (last is not None and not int32(last))
                or any(not isinstance(entry, dict) or set(entry) != shape[1]
                       or (entry.get("client_time") is not None and not int32(entry["client_time"]))
                       for entry in receipts)):
            self._ledger.invalid()
        receipts = [{name: entry[name] for name in RECEIPT_FIELDS} for entry in receipts]
        retained = [entry["request"] for entry in receipts]
        earlier = [key for key in seen if key not in retained]
        return {"schema_version": SCHEMA_VERSION, "receipts": receipts,
                "processed_keys": [*earlier, *retained][-PROCESSED_LIMIT:]}

    @staticmethod
    def _owned(reference, owned):
        if reference.storage_type != BARRACKS_STORAGE or reference.storage_index != FIRST_BARRACKS:
            raise ProtocolError("training_storage_unsupported", "This storage domain is not implemented", 501)
        unit = owned.get(reference.invite_id)
        if unit is None or unit["UniqueID"] != reference.unique_id or unit["CardID"] != reference.card_id:
            raise ProtocolError("training_unit_missing", "Training identity does not match an owned unit", 409)
        if barracks_index(unit) != FIRST_BARRACKS:
            raise ProtocolError("training_storage_unsupported", "Training only supports the first barracks", 501)
        return unit

    def _material(self, reference, owned, state):
        if self.barracks is None:
            return self._owned(reference, owned)
        return resolve_barracks_material(reference, owned, state, self.barracks, operation="training")

    def _content_version(self, catalog_version=None, capacity=...):
        content = self.catalog.content_version if catalog_version is None else catalog_version
        # None stands for absent barracks rules, so the attached ones are the Ellipsis default.
        if capacity is ...:
            if self.barracks is None:
                return content
            capacity = self.barracks.content_version
        elif capacity is None:
            return content
        if any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in (content, capacity)):
            raise ProtocolError("training_dependencies_invalid", "Training requires verified content identities", 503)
        return hashlib.sha256(f"{content}:{capacity}".encode()).hexdigest()

    def _replayable(self, committed, current):
        # A receipt keeps the digest of its commit, which optional inputs attached since then cannot reach.
        capacities = (None,) if self.barracks is None else (self.barracks.content_version, None)
        return committed == current or any(self._content_version(catalog, capacity) == committed
                                           for catalog in self.catalog.replayable_content_versions()
                                           for capacity in capacities)

    @staticmethod
    def protect(player: Mapping, target: Mapping, materials: Iterable[Mapping]) -> None:
        if target["CardID"] in PRINCE_CARD_IDS:
            raise ProtocolError("training_target_ineligible", "The prince cannot be a training target", 409)
        materials = tuple(materials)
        protected = refused_protections(player, materials, GROWTH_MATERIAL_POLICY)
        if protected or any(material["GetIndex"] == target["GetIndex"] for material in materials):
            raise MaterialsProtected("training_material_protected", "A selected material is protected from consumption",
                                     protected)

    def select(self, state: dict, request: TrainingRequest) -> TrainingSelection:
        player = self.players.validate(state)
        owned = {unit["GetIndex"]: unit for unit in player["units"]}
        target = self._owned(request.base, owned)
        materials, ordinary, silver, fairy = [], [], Counter(), Counter()
        for reference in request.materials:
            if reference.storage_type == SILVER_HOUSE_STORAGE and self.silver_house is not None:
                materials.append(self.silver_house.material(reference.card_id))
                silver[reference.card_id] += 1
            elif reference.storage_type == FAIRY_FARM_STORAGE and self.fairy_farm is not None:
                materials.append(self.fairy_farm.material(reference.card_id))
                fairy[reference.card_id] += 1
            else:
                material = self._material(reference, owned, state)
                materials.append(material)
                ordinary.append(material)
        return TrainingSelection(player, target, tuple(materials), tuple(ordinary),
                                 MappingProxyType(dict(silver)), MappingProxyType(dict(fairy)))

    def train(self, state: dict, revision: int, request: TrainingRequest, *, now: datetime | None) -> TrainingOutcome:
        selection = self.select(state, request)
        self.protect(selection.player, selection.target, selection.ordinary)
        config = self.bootstrap.table_rows(state, CONFIG_TABLE)[0]
        quote = self.catalog.quote(selection.player, config, request, selection.target, list(selection.materials),
                                   state=state)
        if (not int32(quote.price) or quote.price <= 0 or set(quote.values) != set(GROWTH_FIELDS)
                or any(not int32(value) for value in quote.values.values())
                or any(quote.values[name] < 0 for name in GROWTH_FIELDS if name != "CostModMixValue")
                or quote.values["CostModMixValue"] > 0):
            raise ProtocolError("training_result_invalid", "Training calculation produced an invalid result", 503)
        money = money_after_debit(state, quote.price, refusals=TRAINING_MONEY_REFUSALS)
        updated = self.gifts.preserve_history(state, revision)
        for storage_type, warehouse, counts in ((SILVER_HOUSE_STORAGE, self.silver_house, selection.silver),
                                                (FAIRY_FARM_STORAGE, self.fairy_farm, selection.fairy)):
            if counts:
                updated = debit_stored_materials(warehouse, updated, dict(counts), storage_type=storage_type,
                                                 label="Training", registration=INTEGRATION)
        updated["tables"]["game_param"][0]["Money"] = money
        saved = updated["player_state"]
        consume_units(saved, selection.ordinary)
        after = next(unit for unit in saved["units"] if unit["GetIndex"] == selection.target["GetIndex"])
        after.update(quote.values)
        if self.missions is not None:
            updated = self.missions.record(updated, "training", {
                "before": selection.target, "after": after, "level": self.catalog.describe(after).level}, now)
        self.players.validate(updated)
        return TrainingOutcome(updated, dict(selection.target), dict(after), quote.price, money, selection.silver,
                               selection.fairy, tuple(dict(unit) for unit in selection.ordinary))

    def integrate(self, envelope, store):
        try:
            return self._integrate(envelope, store)
        except ProtocolError as error:
            log.warning("Training %d refused with %s; the native client %s",
                        envelope.profile.registration_keys[0], error.code, _REFUSAL_AFTERMATH)
            raise

    def _integrate(self, envelope, store):
        request = parse_training(envelope)
        request_key = hashlib.sha256(envelope.xml).hexdigest()
        now = self.bootstrap.clock() if self.missions is not None else None
        for _ in range(3):
            profile = self.players.authenticate(envelope, store)
            # A damaged save must be refused the same way whether or not the request has a receipt.
            self.players.validate(profile.state)
            self.bootstrap._player_row(profile)
            self.bootstrap._rows(profile, CONFIG_TABLE)
            content = self._content_version()
            history = self._history(profile.state, profile.revision)
            receipt = self._ledger.receipt(history, request_key)
            if receipt is not None:
                if receipt["revision"] != profile.revision or not self._replayable(receipt["content"], content):
                    raise ProtocolError("training_retry_stale", "Training result changed since this request committed", 409)
                response = receipt["response"].encode("utf-8")
                try:
                    loads(response)
                    encode_response(envelope.profile, response)
                except (ValueError, ProtocolError) as error:
                    raise ProtocolError("training_history_invalid", "Saved training response is invalid", 503) from error
                return response
            if self._ledger.processed(history, request_key):
                raise ProtocolError("training_request_stale", "Training request was already processed", 409)
            outcome = self.train(profile.state, profile.revision, request, now=now)
            updated = outcome.state
            self.bootstrap.player_row(updated)
            encode_response(self._unit_profile, self.players.unit_response(updated["player_state"]["units"]))
            if outcome.silver:
                encode_response(self._silver_profile, dumps(Container(children=(
                    self.bootstrap._system(profile), self.silver_house.response(updated)))))
            children = (self.bootstrap._system(profile),
                Container("PGP", (Column("A1", "S", ("A1",)), Column("A2", "S", (str(outcome.money),)))),
                Container("PPU", (Column("A7", "I", (outcome.after["GetIndex"],)),
                    *(Column(alias, "I", (outcome.after[name],)) for name, alias in GROWTH_FIELDS.items()))))
            if outcome.fairy:
                children += (self.fairy_farm.response(updated, tuple(outcome.fairy)),)
            response = dumps(Container(children=children))
            encode_response(envelope.profile, response)
            if len(response) > RESPONSE_LIMIT:
                raise ProtocolError("training_response_capacity", "Training response exceeds the receipt limit", 503)
            self._ledger.record(updated, history, {"request": request_key, "content": content,
                                                   "revision": profile.revision + 1, "response": response.decode("utf-8")})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving training", 409)
