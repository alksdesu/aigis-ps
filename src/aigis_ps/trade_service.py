from __future__ import annotations

from copy import copy, deepcopy
from collections.abc import Mapping
from dataclasses import asdict, replace
import hashlib
import json
import logging
import re

from .alxml import Column, Container, Limits, dumps, loads
from . import crystal_spending as spending
from .crystal_wallet import free_crystal_response, paid_crystal_response, wallet_totals
from .energy_clock import UNIX_EPOCH
from .errors import REFUSAL_AFTERMATH as _REFUSAL_AFTERMATH, REFUSAL_POLICY, ProtocolError
from .game_param_groups import money_columns
from .native_numbers import int32
from .player_state import DISPLAY_FIELD, MAX_PLAYER_UNITS, UNIT_BOOLEANS, UNIT_FIELDS
from .protocol import ProtocolRegistry, encode_response
from .request_fields import fields, invalid, registered, scalar
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .shop_growth import CLASSIC_RESULT_CARD_ID
from .trade_catalog import REGISTRATIONS
from .trade_history import TradeHistory, TradeRules, server_milliseconds
from .unit_initialization import CLASSIC_ROUTE_CARD_ID
from .unit_removal import consume_units
from .wallet_money import NATIVE_BALANCE_MESSAGE, MoneyRefusal, MoneyRefusals, money_after_debit


RECEIPT_NAMESPACE = "trade_receipts"
RESPONSE_LIMIT = 1024 * 1024
TRADE_LIMITS = Limits(max_values=500000, max_elements=500000)
ORDINARY_OPERATIONS = frozenset(REGISTRATIONS.values())
OPERATIONS = ORDINARY_OPERATIONS | {106}
GROWTH_FIELDS = frozenset(("ClassID", "ClassChangeCount", "Exp", "Love", "CostModMixValue", "SettingSkillID", DISPLAY_FIELD))
TRADE_MONEY_REFUSALS = MoneyRefusals(
    insufficient=MoneyRefusal("trade_currency_insufficient", "Trade price exceeds the available currency"),
    native_balance=MoneyRefusal("trade_native_balance", NATIVE_BALANCE_MESSAGE))
_HASH = re.compile(r"[a-f0-9]{64}\Z")
log = logging.getLogger("aigis_ps")


def _receipt_operation_valid(entry):
    return type(entry["operation"]) is int and entry["operation"] in OPERATIONS


class TradeService:
    def __init__(self, bootstrap, players, possessions, catalog, *, grants, rules=None, clock=None,
                 silver=None, gifts=None, evolution_rewards=None, silver_error=None,
                 growth_shop=None, growth_shop_error=None):
        if rules is not None and not isinstance(rules, TradeRules):
            raise ValueError("Trade rules must use the versioned rule type")
        if clock is not None and not callable(clock):
            raise ValueError("Trade clock must be callable")
        if not callable(getattr(grants, "grant", None)):
            raise ValueError("Trade units require a grant provider")
        self.bootstrap, self.players, self.possessions = bootstrap, players, possessions
        self.catalog, self.grants = catalog, grants
        self.silver, self.gifts, self.silver_error = silver, gifts, silver_error
        self.evolution_rewards = evolution_rewards
        self.growth_shop, self.growth_shop_error = growth_shop, growth_shop_error
        self.history = TradeHistory(catalog, rules)
        self.clock = bootstrap.clock if clock is None else clock
        self._ledger = RequestLedger(RECEIPT_NAMESPACE, "trade_receipts_invalid", "Trade request receipts are invalid",
                                     fields=("request", "content", "revision", "response", "operation"),
                                     response_limit=RESPONSE_LIMIT, check=_receipt_operation_valid)
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (33, 34, 35, 41, 43, 44, 72, 137)}
        self._content_version()
        if silver is not None:
            self._content_version(106)
        if growth_shop is not None:
            self._content_version(113, grow=True)

    @property
    def rules(self):
        return self.history.rules

    def handlers(self):
        handler = self.purchase
        operations = OPERATIONS if self.silver is not None else ORDINARY_OPERATIONS
        return {number: handler for number in sorted(operations)}

    def _now(self):
        value = self.clock()
        server_milliseconds(value)
        return value

    def initialize(self, state):
        return self.history.initialize(state, self._now())

    def history_response(self, state):
        return self.history.response(state, self._now())

    def status(self):
        return {"implemented_operations": sorted(self.handlers()), "ready": True,
                "content_version": self._content_version(), "rules": asdict(self.rules),
                "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "receipt_xml_limit": RESPONSE_LIMIT, "native_tested": False,
                "unconfigured_resets": [number for number in (1, 2)
                                        if getattr(self.rules, f"reset_type_{number}") == "unconfigured"],
                "replay_policy": ("Exact request at its committed revision and content version, also ordinary trades "
                                  "committed under an earlier trade resource version that the catalog still names"),
                "request_order": REQUEST_ORDER,
                "refusals": REFUSAL_POLICY,
                "crystal_policy": "Free before paid, oldest acquisition dates first",
                "silver_trade": self.silver.status() if self.silver is not None else {
                    "ready": False, "error": self.silver_error},
                "growth_shop": self.growth_shop.status() if self.growth_shop is not None else {
                    "ready": False, "error": self.growth_shop_error}}

    def _content_version(self, operation=None, *, grow=False, catalog=None):
        sources = {"catalog": getattr(self.catalog, "content_version", None) if catalog is None else catalog,
                   "grants": getattr(self.grants, "content_version", None),
                   "possessions": self.possessions.asset_sha256}
        if (not self.possessions.is_ready() or self.catalog.possessions.asset_sha256 != sources["possessions"]
                or any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in sources.values())
                or not self.catalog.is_ready()):
            raise ProtocolError("trade_dependencies_invalid", "Trade requires matching verified dependencies", 503)
        growth = getattr(self.grants, "growth", None)
        if growth is not None:
            sources["growth"] = getattr(growth, "content_version", None)
            if not isinstance(sources["growth"], str) or not _HASH.fullmatch(sources["growth"]):
                raise ProtocolError("trade_dependencies_invalid", "Trade growth content identity is unavailable", 503)
        sources.update(rules=asdict(self.rules), policy=2)
        if operation == 111:
            sources["money_response"] = "columns"
        if operation == 112:
            sources["crystal_spending_policy"] = spending.POLICY_VERSION
        if operation == 106:
            if (self.silver is None or self.gifts is None or self.evolution_rewards is None
                    or self.silver.base is not self.catalog or self.gifts.players is not self.players
                    or self.evolution_rewards.players is not self.players
                    or not self.silver.is_ready()):
                raise ProtocolError("silver_trade_unavailable", "Silver trades require verified products and unit history", 503)
            sources.update(silver=self.silver.content_version, gift_history=self.gifts.content_version,
                           evolution_history=self.evolution_rewards.content_version, silver_policy=1)
        if grow:
            if (operation != 113 or self.growth_shop is None or self.gifts is None or self.evolution_rewards is None
                    or self.growth_shop.base is not self.catalog or self.gifts.players is not self.players
                    or self.evolution_rewards.players is not self.players or not self.growth_shop.is_ready()):
                raise ProtocolError("shop_growth_unavailable", "Growth trades require verified resources and reward history", 503)
            sources.update(growth_shop=self.growth_shop.content_version,
                           growth_gift_history=self.gifts.content_version,
                           growth_evolution_history=self.evolution_rewards.content_version, growth_policy=1)
        return hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()

    def _replayable(self, committed, operation, *, grow=False):
        # Silver and growth contents also name the resource version through their own catalogs and reward histories.
        if grow or operation == 106:
            return False
        return any(self._content_version(operation, catalog=version) == committed
                   for version in self.catalog.replayable_content_versions() - {self.catalog.content_version})

    def _grant(self, state, product, count, before):
        retained = deepcopy({name: value for name, value in state.items() if name != "player_state"})
        updated, units = self.grants.grant(state, product, count)
        if (not isinstance(updated, dict)
                or {name: value for name, value in updated.items() if name != "player_state"} != retained):
            raise ProtocolError("trade_grant_result_invalid", "Unit grant changed the payment or purchase state", 503)
        saved = self.players.validate_player(updated.get("player_state") if isinstance(updated, dict) else None)
        old = {unit["GetIndex"]: unit for unit in before["units"]}
        current = {unit["GetIndex"]: unit for unit in saved["units"]}
        if (not isinstance(units, list) or len(units) != count or len(current) != len(old) + count
                or any(current.get(index) != unit for index, unit in old.items())
                or any(not isinstance(unit, dict) or not int32(unit.get("GetIndex")) for unit in units)):
            raise ProtocolError("trade_grant_result_invalid", "Trade grant did not preserve the existing unit roster", 503)
        indices = {unit["GetIndex"] for unit in units}
        highest_uid = before.get("last_unique_id", max(unit["UniqueID"] for unit in before["units"]))
        if (len(indices) != count or indices != current.keys() - old.keys()
                or any(current[unit["GetIndex"]] != unit or unit["CardID"] != product.card_id
                       or unit["GetIndex"] <= before["battle"]["UX"] or unit["UniqueID"] <= highest_uid for unit in units)):
            raise ProtocolError("trade_grant_result_invalid", "Trade grant returned inconsistent new unit identities", 503)
        return updated, units

    def _preflight(self, profile, state, now):
        candidate = replace(profile, state=state)
        player = self.players.validate(state)
        wallet = self.bootstrap._player_row(candidate)
        encode_response(self._profiles[41], self.players.unit_response(player["units"]))
        encode_response(self._profiles[34], dumps(Container(children=self.bootstrap.columns("game_param", [wallet]))))
        wallet_totals(state)
        rows = self.bootstrap._rows(candidate, "crystals", single=False)
        for number, free in ((43, False), (44, True)):
            selected = [row for row in rows if row["FreeFlag"] is free]
            encode_response(self._profiles[number], dumps(Container(children=self.bootstrap.columns("crystals", selected))))
        bootstrap = copy(self.bootstrap)
        bootstrap.exchange_history_provider = lambda saved: self.history.response(saved, now)
        encode_response(self._profiles[137], bootstrap._configuration_response(candidate))

    def _preflight_spending(self, profile, state):
        spending.read_spending(state)
        candidate = replace(profile, state=state)
        rows = self.bootstrap._rows(candidate, "game_param2")
        encode_response(self._profiles[35], dumps(Container(children=self.bootstrap.columns("game_param2", rows))))
        encode_response(self._profiles[33], self.bootstrap.game_param2_fields_response(candidate))

    def _validate_spending_receipt(self, profile, document):
        expected = (spending.spending_response(profile.state, style="columns"),
                    paid_crystal_response(profile.state, profile.player_id),
                    free_crystal_response(profile.state, profile.player_id))
        if any([node for node in document.children if node.name == value.name] != [value] for value in expected):
            self._ledger.invalid()
        self._preflight_spending(profile, profile.state)

    def _response(self, profile, state, quote, units, now, *, new_units=True):
        children = [self.bootstrap._system(profile), self.history.response(state, now)]
        changed_items = set()
        if quote.currency_kind == "money":
            children.append(money_columns(state["tables"]["game_param"][0]["Money"]))
        elif quote.currency_kind == "crystals":
            children.extend((paid_crystal_response(state, profile.player_id), free_crystal_response(state, profile.player_id)))
            children.append(spending.spending_response(state, style="columns"))
        else:
            changed_items.add(quote.currency_item_id)
        if quote.product.product_type == 3:
            changed_items.add(quote.product.item_id)
        if changed_items:
            children.append(self.possessions.response(state, tuple(sorted(changed_items))))
        if units and new_units:
            children.append(Container("PBP", (Column("A1", "S", ("UX",)),
                Column("A2", "I", (state["player_state"]["battle"]["UX"],)))))
        children.append(Container("PPU", tuple(Column(alias, "B" if name in UNIT_BOOLEANS else "I",
                                                      tuple(unit[name] for unit in units)) for name, alias in UNIT_FIELDS)))
        if not new_units:
            children.append(Container("TrustReward", (Column("ID", "I", ()),)))
        return dumps(Container(children=tuple(children)), limits=TRADE_LIMITS)

    def purchase(self, envelope, store):
        operations = envelope.profile.registration_keys
        operation = operations[0] if len(operations) == 1 else None
        try:
            if operation is None:
                invalid("Trade requires one exact native operation")
            return self._purchase(envelope, store, operation)
        except ProtocolError as error:
            log.warning("Trade %s refused with %s; the native client %s", envelope.profile.identity, error.code,
                        _REFUSAL_AFTERMATH)
            raise

    def _purchase(self, envelope, store, operation):
        required = ("SHOP", "NUM", "BASE") if operation == 106 else ("SHOP", "NUM")
        allowed = (*required, "UnitID") if operation == 113 else required
        columns = registered(envelope, OPERATIONS, allowed, required)
        grow = "UnitID" in columns
        shop_id, quantity = (scalar(columns[name], "I") for name in ("SHOP", "NUM"))
        if shop_id <= 0 or quantity <= 0:
            invalid("Trade requires positive product and quantity values")
        if grow:
            target_index = scalar(columns["UnitID"], "I")
            if target_index < 0:
                invalid("Growth trades require an owned unit identity")
        if operation == 106:
            base = fields(columns["BASE"], ("A7", "AB"), ("A7", "AB"))
            material_index, material_uid = (scalar(base[name], "I") for name in ("A7", "AB"))
            if material_index < 0 or material_uid <= 0:
                invalid("Silver trades require an existing unit identity")
        identity = f"{operation}:{envelope.profile.route}:{envelope.profile.st}".encode()
        key = hashlib.sha256(identity + b"\0" + envelope.xml).hexdigest()
        now = None
        for _ in range(3):
            content = self._content_version(operation, grow=grow)
            profile = self.players.authenticate(envelope, store)
            player = self.players.validate(profile.state)
            self.bootstrap._player_row(profile)
            history = self._ledger.read(profile.state, profile.revision)
            receipt = self._ledger.receipt(history, key)
            if receipt is not None:
                if (receipt["revision"] != profile.revision or receipt["operation"] != operation
                        or not (receipt["content"] == content or self._replayable(receipt["content"], operation, grow=grow))):
                    raise ProtocolError("trade_retry_stale", "Trade state or content changed after this request", 409)
                response = receipt["response"].encode("utf-8")
                try:
                    document = loads(response, limits=TRADE_LIMITS)
                    self.history.records(profile.state, UNIX_EPOCH)
                    if operation == 112:
                        self._validate_spending_receipt(profile, document)
                    encode_response(envelope.profile, response)
                except (ValueError, ProtocolError) as error:
                    raise ProtocolError("trade_receipts_invalid", "Saved trade response is invalid", 503) from error
                return response
            if self._ledger.processed(history, key):
                raise ProtocolError("trade_request_stale", "Trade request was already processed", 409)
            if now is None:
                now = self._now()
            records = self.history.records(profile.state, now)
            catalog = self.growth_shop if grow else self.silver if operation == 106 else self.catalog
            product = catalog.product(shop_id)
            if not grow and product.shop_type == 2 and product.product_type == 6:
                invalid("Growth trades require UnitID")
            if self.history.period(product, now) == "unconfigured":
                raise ProtocolError("trade_reset_unconfigured", "The local reset policy for this product has not been configured", 501)
            count = self.history.count(records, product, now)
            quote = catalog.quote(profile.state, shop_id, quantity, count, now, operation)
            updated = profile.state
            if grow:
                values = self.growth_shop.values(player, product, target_index, state=profile.state)
                if (not isinstance(values, Mapping) or not GROWTH_FIELDS <= values.keys()
                        or values.keys() - (GROWTH_FIELDS | {"CardID"})
                        or any(not int32(value) for value in values.values())
                        or values["ClassChangeCount"] != 2 or values[DISPLAY_FIELD] != 0):
                    raise ProtocolError("shop_growth_result_invalid", "Growth trade returned unsupported unit changes", 503)
                updated = self.gifts.preserve_history(profile.state, profile.revision)
                updated = self.evolution_rewards.initialize(updated)
            if operation == 106:
                material = self.silver.material(player, product, material_index, material_uid, state=profile.state)
                self.players.require_barracks_space(profile.state, 1)
                updated = self.gifts.preserve_history(profile.state, profile.revision)
                updated = self.evolution_rewards.initialize(updated)
                saved = updated["player_state"]
                consume_units(saved, (material,))
                self.players.validate_player(saved)
            updated = self.history.record_purchase(updated, product, quantity, now)
            if quote.currency_kind == "money":
                updated["tables"]["game_param"][0]["Money"] = money_after_debit(updated, quote.total_price,
                                                                             refusals=TRADE_MONEY_REFUSALS)
            elif quote.currency_kind == "crystals":
                updated = spending.spend_crystals(updated, quote.total_price, now, free_first=True)
            elif quote.currency_kind == "possession":
                if quote.total_price:
                    updated = self.possessions.debit(updated, {quote.currency_item_id: quote.total_price})
            else:
                raise ProtocolError("trade_currency_unavailable", "Trade quote has an unsupported payment currency", 503)
            units = []
            if grow:
                target = next((unit for unit in updated["player_state"]["units"] if unit["GetIndex"] == target_index), None)
                if target is None:
                    raise ProtocolError("shop_growth_target_missing", "Growth trade target is not owned", 409)
                if (values.get("CardID", target["CardID"]) != target["CardID"]
                        and (target["CardID"], values.get("CardID")) != (CLASSIC_ROUTE_CARD_ID, CLASSIC_RESULT_CARD_ID)):
                    raise ProtocolError("shop_growth_result_invalid", "Growth trade cannot replace this card identity", 503)
                target.update(values)
                updated = self.gifts.record_current_thresholds(updated, target_index, profile.revision)
                units = [next(unit for unit in updated["player_state"]["units"] if unit["GetIndex"] == target_index)]
            elif product.product_type == 1:
                before_grant = self.players.validate_player(updated["player_state"]) if operation == 106 else player
                updated, units = self._grant(updated, product, quote.amount, before_grant)
            elif product.product_type == 3:
                updated = self.possessions.credit(updated, {product.item_id: quote.amount})
            else:
                raise ProtocolError("trade_product_unsupported", "Trade reward requires a separate native transaction", 501)
            try:
                if grow:
                    current = self.players.validate_player(updated["player_state"])
                    encode_response(self._profiles[72], self.players.squad_response(replace(profile, state=updated), current))
                    reserved = self.players.quest_reservations(updated)
                    if reserved:
                        trial = deepcopy(updated)
                        trial["quest_state"]["active"] = None
                        self.players.credit_units(trial, reserved)
                if operation == 106:
                    peak = deepcopy(player)
                    peak["units"].extend(deepcopy(units))
                    peak["battle"]["UX"] = updated["player_state"]["battle"]["UX"]
                    peak["last_unique_id"] = updated["player_state"]["last_unique_id"]
                    if len(peak["units"]) > MAX_PLAYER_UNITS:
                        raise ProtocolError("trade_unit_peak_capacity", "Native silver exchange cannot temporarily hold the new unit", 409)
                    self.players.validate_player(peak)
                    encode_response(self._profiles[41], self.players.unit_response(peak["units"]))
                self._preflight(profile, updated, now)
                if quote.currency_kind == "crystals":
                    self._preflight_spending(profile, updated)
                response = self._response(profile, updated, quote, units, now, new_units=not grow)
                encode_response(envelope.profile, response)
            except ProtocolError:
                raise
            except ValueError as error:
                raise ProtocolError("trade_response_invalid", "Trade response cannot satisfy the native XML contract", 503) from error
            if len(response) > RESPONSE_LIMIT:
                raise ProtocolError("trade_response_capacity", "Trade response exceeds its receipt limit", 503)
            self._ledger.record(updated, history, {"request": key, "content": content, "revision": profile.revision + 1,
                                                   "response": response.decode("utf-8"), "operation": operation})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving the trade", 409)
