from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import logging
import re
from types import MappingProxyType

from .alxml import Column, Container, dumps, loads
from .errors import REFUSAL_AFTERMATH as _REFUSAL_AFTERMATH, REFUSAL_POLICY, ProtocolError
from .evolution_rewards import EvolutionRewards
from .native_numbers import int32
from .player_state import barracks_index
from .protocol import ProtocolRegistry, encode_response
from .sale_protocol import AUTOMATIC_SALE, MANUAL_SALE, SALE_REGISTRATIONS, parse_sale
from .unit_reference import BARRACKS_STORAGE, FIRST_BARRACKS, UnitReference
from .unit_removal import Protection, RemovalPolicy, UnitIdentity, UnitRemoval, protection_check
from .unit_sale_catalog import SaleQuote
from .wallet_money import GOLD_LIMIT, money_after_capped_credit


HISTORY_KEY = "unit_sale_history"
HISTORY_LIMIT = 16
RESPONSE_LIMIT = 16384
SALE_ERRORS = "sale"
SALE_POLICIES = MappingProxyType({
    MANUAL_SALE: RemovalPolicy(frozenset({Protection.PRINCE, Protection.LOCKED, Protection.SQUAD})),
    # The native automatic collector skips only locked units, while the manual list also hides organized ones.
    AUTOMATIC_SALE: RemovalPolicy(frozenset({Protection.PRINCE, Protection.LOCKED})),
})
_HASH = re.compile(r"[a-f0-9]{64}\Z")
log = logging.getLogger("aigis_ps")


def _history_error():
    raise ProtocolError("sale_history_invalid", "Unit-sale receipts are invalid or from an unsupported schema", 503)


@dataclass(frozen=True)
class SaleResult:
    state: dict
    sold: tuple[dict, ...]
    quote: SaleQuote
    balance: int
    vacated: tuple[int, ...]


class UnitSale:
    def __init__(self, players, possessions, gifts, evolution_rewards, catalog, *, error_prefix: str = SALE_ERRORS):
        self._removal = UnitRemoval(players, gifts, evolution_rewards, error_prefix=f"{error_prefix}_unit")
        self.players, self.possessions, self.catalog = players, possessions, catalog
        self._prefix = error_prefix

    def _error(self, suffix: str, message: str, status: int = 409):
        raise ProtocolError(f"{self._prefix}_{suffix}", message, status)

    def _selected(self, player: dict, references: Sequence[UnitReference], registration: int) -> tuple[dict, ...]:
        owned = {unit["GetIndex"]: unit for unit in player["units"]}
        automatic = registration == AUTOMATIC_SALE
        protected = protection_check(player, SALE_POLICIES[registration].refuse)
        result = []
        # Each unit runs every check before the next one, so the first offending unit decides the refusal.
        for reference in references:
            if reference.storage_type != BARRACKS_STORAGE or reference.storage_index != FIRST_BARRACKS:
                self._error("storage_unsupported", "This storage domain is not implemented for sales", 501)
            unit = owned.get(reference.invite_id)
            if unit is None or unit["UniqueID"] != reference.unique_id or unit["CardID"] != reference.card_id:
                self._error("unit_missing", "Sale identity does not match an owned unit")
            if barracks_index(unit) != FIRST_BARRACKS:
                self._error("storage_unsupported", "Sales only support the first barracks", 501)
            if protected(unit):
                self._error("unit_protected", "A selected unit is protected from sale")
            if automatic:
                if unit["CardID"] not in self.catalog.auto_card_ids:
                    self._error("automatic_ineligible", "Automatic sales only accept configured material cards")
            else:
                self.catalog.check_rating(unit["CardID"])
            result.append(unit)
        return tuple(result)

    def sell(self, state: dict, revision: int, references: Sequence[UnitReference], registration: int) -> SaleResult:
        if registration not in SALE_POLICIES:
            raise ValueError("Sales are either manual or automatic")
        units = self._selected(self.players.validate(state), references, registration)
        quote = self.catalog.quote(units)
        if any(not int32(value) or value < 0 for value in (quote.gold, quote.rainbow)):
            self._error("quote_invalid", "Sale amounts must be nonnegative native integers", 503)
        removal = self._removal.remove(state, revision, [UnitIdentity(unit["GetIndex"], unit["UniqueID"]) for unit in units],
                                       SALE_POLICIES[registration])
        updated = removal.state
        balance = money_after_capped_credit(updated, quote.gold, cap=GOLD_LIMIT)
        updated["tables"]["game_param"][0]["Money"] = balance
        if quote.rainbow:
            updated = self.possessions.credit(updated, {self.catalog.rainbow_item: quote.rainbow})
        return SaleResult(updated, removal.removed, quote, balance, removal.vacated)


class UnitSaleService:
    def __init__(self, bootstrap, players, possessions, gifts, catalog, *, evolution_rewards=None):
        self.bootstrap, self.players, self.possessions = bootstrap, players, possessions
        self.gifts, self.catalog = gifts, catalog
        if evolution_rewards is None and gifts is not None:
            # The awakening ledger reads the same card master, card status and presents as the trust ledger.
            evolution_rewards = EvolutionRewards(gifts.catalog, players, gifts.card_status, gifts.presents)
        self.evolution_rewards = evolution_rewards
        self.kernel = UnitSale(players, possessions, gifts, evolution_rewards, catalog)
        registry = ProtocolRegistry.load()
        self._unit_profile, self._configuration_profile = registry.by_registration[41], registry.by_registration[137]
        self._squad_profile = registry.by_registration[72]

    def handlers(self):
        return dict.fromkeys(SALE_REGISTRATIONS, self.sell)

    def status(self):
        return {"implemented_operations": list(SALE_REGISTRATIONS), "catalog": self.catalog.status(), "native_tested": False,
                "history_limit": HISTORY_LIMIT, "gold_limit": GOLD_LIMIT,
                "overflow_policy": "Gold beyond gold_limit is discarded and a balance saved above it stays unchanged; "
                                   "rainbow currency beyond its Stack rejects the complete batch without consuming units",
                "local_protection": "Locked and prince instances are never sold; manual sales also keep organized instances",
                "automatic_squads": "Automatic sales follow the native collector and empty the squad slots of organized instances",
                "refusals": REFUSAL_POLICY,
                "replay_policy": "Exact request at its committed revision and content version; consumed identities are never reused",
                "native_exclusions": ["Warehouse and additional barrack references, which the native sale lists never send"]}

    @staticmethod
    def _history(state, revision):
        if HISTORY_KEY not in state:
            return {"schema_version": 1, "receipts": []}
        history = state[HISTORY_KEY]
        if (not isinstance(history, dict) or set(history) != {"schema_version", "receipts"}
                or type(history["schema_version"]) is not int or history["schema_version"] != 1
                or not isinstance(history["receipts"], list) or len(history["receipts"]) > HISTORY_LIMIT):
            _history_error()
        previous, keys = 0, set()
        for receipt in history["receipts"]:
            if (not isinstance(receipt, dict) or set(receipt) != {"request", "content", "revision", "response"}
                    or any(not isinstance(receipt[name], str) or not _HASH.fullmatch(receipt[name]) for name in ("request", "content"))
                    or type(receipt["revision"]) is not int or not previous < receipt["revision"] <= revision
                    or not isinstance(receipt["response"], str) or len(receipt["response"].encode("utf-8")) > RESPONSE_LIMIT
                    or receipt["request"] in keys):
                _history_error()
            previous = receipt["revision"]
            keys.add(receipt["request"])
        return deepcopy(history)

    def sell(self, envelope, store):
        request = parse_sale(envelope)
        try:
            return self._commit(envelope, store, request)
        except ProtocolError as error:
            log.warning("Unit sale %d refused with %s; the native client %s", request.registration, error.code,
                        _REFUSAL_AFTERMATH)
            raise

    def _commit(self, envelope, store, request):
        key = hashlib.sha256(envelope.xml).hexdigest()
        for _ in range(3):
            profile = self.players.authenticate(envelope, store)
            self.players.validate(profile.state)
            self.bootstrap._player_row(profile)
            history = self._history(profile.state, profile.revision)
            cached = next((row for row in history["receipts"] if row["request"] == key), None)
            if cached is not None:
                if cached["revision"] != profile.revision or cached["content"] != self.catalog.content_version:
                    raise ProtocolError("sale_retry_stale", "Player state or sale content changed after this request", 409)
                response = cached["response"].encode("utf-8")
                try:
                    loads(response)
                    encode_response(envelope.profile, response)
                except (ValueError, ProtocolError) as error:
                    raise ProtocolError("sale_history_invalid", "Saved sale response is invalid", 503) from error
                return response
            result = self.kernel.sell(profile.state, profile.revision, request.units, request.registration)
            updated = result.state
            candidate = replace(profile, state=updated)
            self.bootstrap._player_row(candidate)
            saved = updated["player_state"]
            encode_response(self._unit_profile, self.players.unit_response(saved["units"]))
            encode_response(self._configuration_profile, self.bootstrap._configuration_response(candidate))
            if result.vacated:
                encode_response(self._squad_profile, self.players.squad_response(profile, saved))
            children = (self.bootstrap._system(profile), Container("PGP", (
                Column("A1", "S", ("A1",)), Column("A2", "S", (str(result.balance),)))))
            if result.quote.rainbow:
                children += (self.possessions.response(updated, (self.catalog.rainbow_item,)),)
            response = dumps(Container(children=children))
            encode_response(envelope.profile, response)
            if len(response) > RESPONSE_LIMIT:
                raise ProtocolError("sale_response_capacity", "Sale response exceeds the receipt limit", 503)
            history["receipts"] = [*history["receipts"], {"request": key, "content": self.catalog.content_version,
                "revision": profile.revision + 1, "response": response.decode("utf-8")}][-HISTORY_LIMIT:]
            updated[HISTORY_KEY] = history
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player data changed while saving the unit sale", 409)
