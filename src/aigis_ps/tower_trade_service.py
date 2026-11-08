from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import logging

from .alxml import Column, Container, Limits, dumps, loads
from .errors import REFUSAL_AFTERMATH as _REFUSAL_AFTERMATH, REFUSAL_POLICY, ProtocolError
from .native_numbers import int32
from .player_state import UNIT_BOOLEANS, UNIT_FIELDS
from .possessions import STORED_ITEM_NAME_BYTES
from .protocol import ProtocolRegistry, encode_response
from .request_fields import invalid, registered, scalar
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .storage import MAX_REVISION
from .tower_trade_state import REBIND_OPERATION, TowerTradeHistory, history_digest, rebind
from .unit_grants import MAX_GRANTS


RECEIPT_NAMESPACE = "tower_trade_receipts"
RECEIPT_SCHEMA_VERSION = 2
LEDGER_KEY = "requests"
RESPONSE_LIMIT = 1024 * 1024
CONTENT_POLICY = 1
XML_LIMITS = Limits(max_values=500000, max_elements=500000)
_RECEIPT_FIELDS = frozenset(("schema_version", "history_digest", LEDGER_KEY))
_LEGACY_RECEIPT_FIELDS = frozenset(("schema_version", "receipts", "last_client_time", "seen_keys", "history_digest"))
_LEDGER_RECEIPT_FIELDS = ("request", "content", "revision", "response", "shop_id", "quantity", "purchased_count", "unit_indices")
log = logging.getLogger("aigis_ps")


def _receipt_error():
    raise ProtocolError("tower_trade_receipts_invalid", "Tower purchase receipts are inconsistent", 503)


def _receipt_shape_valid(entry):
    indices = entry["unit_indices"]
    return (all(int32(entry[name]) and entry[name] > 0 for name in ("shop_id", "quantity", "purchased_count"))
            and entry["quantity"] <= entry["purchased_count"]
            and isinstance(indices, list) and len(indices) <= MAX_GRANTS
            and all(int32(index) and index >= 0 for index in indices) and len(set(indices)) == len(indices))


def _upgrade(value):
    ledger = {name: value[name] for name in _LEGACY_RECEIPT_FIELDS if name != "history_digest"}
    return {"schema_version": RECEIPT_SCHEMA_VERSION, "history_digest": value["history_digest"], LEDGER_KEY: ledger}


class TowerTradeService:
    def __init__(self, bootstrap, players, possessions, catalog, *, grants, clock=None):
        self.bootstrap, self.players, self.possessions = bootstrap, players, possessions
        self.catalog, self.grants = catalog, grants
        self.history = TowerTradeHistory(catalog)
        if clock is not None and not callable(clock):
            raise ValueError("Tower clock must be callable")
        self.clock = bootstrap.clock if clock is None else clock
        self._ledger = RequestLedger(LEDGER_KEY, "tower_trade_receipts_invalid", "Tower purchase receipts are inconsistent",
                                     fields=_LEDGER_RECEIPT_FIELDS, response_limit=RESPONSE_LIMIT, check=_receipt_shape_valid)
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (41, 65, 69, 129, 137)}
        self.content_version = self._content_version()
        self._legacy_version = self._version_for(self.history.legacy_content_version)

    def handlers(self):
        return {65: self.state, 69: self.purchase}

    def _content_version(self):
        self.catalog._ready()
        self.grants._ready()
        self.history._ready()
        if (self.catalog.possessions is not self.possessions or self.grants.catalog is not self.catalog
                or self.grants.players is not self.players or self.grants.possessions is not self.possessions):
            raise ProtocolError("tower_trade_dependencies_invalid", "Tower dependencies must share resources and state providers", 503)
        return self._version_for(self.history.content_version)

    def _version_for(self, history_version):
        content = {"catalog": self.catalog.content_version, "grants": self.grants.content_version,
                   "history": history_version, "policy": CONTENT_POLICY}
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()

    def replayable_content_versions(self):
        return frozenset((self.content_version, self._legacy_version))

    def _ready(self):
        if self._content_version() != self.content_version:
            raise ProtocolError("tower_trade_content_changed", "Tower content changed after construction", 503)

    def _receipts(self, state, revision, records):
        digest = history_digest(state)
        if RECEIPT_NAMESPACE not in state:
            if records:
                _receipt_error()
            return {"schema_version": RECEIPT_SCHEMA_VERSION, "history_digest": digest,
                    LEDGER_KEY: self._ledger.read({}, revision)}
        value = state[RECEIPT_NAMESPACE]
        if isinstance(value, dict) and set(value) == _LEGACY_RECEIPT_FIELDS:
            value = _upgrade(value)
        if (not isinstance(value, dict) or set(value) != _RECEIPT_FIELDS
                or type(value["schema_version"]) is not int or value["schema_version"] != RECEIPT_SCHEMA_VERSION
                or value["history_digest"] != digest):
            _receipt_error()
        requests = self._ledger.read(value, revision)
        if not requests["receipts"]:
            _receipt_error()
        counts = {}
        for row in requests["receipts"]:
            record = records.get(row["shop_id"])
            if (record is None or record.lifetime < row["purchased_count"]
                    or row["purchased_count"] < counts.get(row["shop_id"], 0) + row["quantity"]):
                _receipt_error()
            try:
                xml = row["response"].encode("utf-8")
                loads(xml, limits=XML_LIMITS)
                encode_response(self._profiles[69], xml)
            except (ValueError, UnicodeError, ProtocolError) as error:
                raise ProtocolError("tower_trade_receipts_invalid", "Tower receipt XML is invalid", 503) from error
            counts[row["shop_id"]] = row["purchased_count"]
        return {"schema_version": RECEIPT_SCHEMA_VERSION, "history_digest": digest, LEDGER_KEY: requests}

    def initialize(self, state):
        self._ready()
        updated = self.history.initialize(state)
        # Startup has no stored revision to bound receipts, so the store's ceiling stands in for it.
        self._receipts(updated, MAX_REVISION, self.history.stored(updated))
        return updated

    def reuse_report(self, state):
        self._ready()
        return {"reused_products": list(self.history.reused(state)), "refused_operations": sorted(self.handlers()),
                "repair": REBIND_OPERATION, "client": _REFUSAL_AFTERMATH}

    def rebind_product(self, state, revision, product_id, carry):
        self._ready()
        product = self.catalog.product(product_id)
        receipts = self._receipts(state, revision, self.history.stored(state))
        updated = rebind(state, product, carry)
        receipts["history_digest"] = history_digest(updated)
        updated[RECEIPT_NAMESPACE] = receipts
        self._receipts(updated, revision, self.history.stored(updated))
        return updated

    def _refusal_logged(self, operation, action, envelope, store):
        try:
            return action(envelope, store)
        except ProtocolError as error:
            log.warning("Tower trade %d refused with %s; the native client %s", operation, error.code, _REFUSAL_AFTERMATH)
            raise

    def state(self, envelope, store):
        return self._refusal_logged(65, self._read, envelope, store)

    def purchase(self, envelope, store):
        return self._refusal_logged(69, self._purchase, envelope, store)

    def _read(self, envelope, store):
        columns = registered(envelope, {65}, ("ITEM",), ("ITEM",))
        currency = scalar(columns["ITEM"], "S", max_bytes=STORED_ITEM_NAME_BYTES)
        if currency not in self.catalog.currencies:
            invalid("Tower state requires a configured currency")
        self._ready()
        profile = self.players.authenticate(envelope, store)
        self._receipts(profile.state, profile.revision, self.history.records(profile.state))
        history = self.history.response(profile.state, currency, self.history.period(self.clock()))
        response = dumps(Container(children=(self.bootstrap._system(profile), history)))
        encode_response(envelope.profile, response)
        return response

    def _response(self, profile, state, product, units, history):
        children = [self.bootstrap._system(profile), history]
        if units:
            children.append(Container("PBP", (Column("A1", "S", ("UX",)),
                Column("A2", "I", (state["player_state"]["battle"]["UX"],)))))
        children.append(Container("PPU", tuple(Column(alias, "B" if name in UNIT_BOOLEANS else "I",
            tuple(unit[name] for unit in units)) for name, alias in UNIT_FIELDS)))
        items = list(dict.fromkeys([product.currency_item_id] + ([product.item_id] if product.item_id else [])))
        children.append(self.possessions.response(state, items, include_enabled=True))
        return dumps(Container(children=tuple(children)), limits=XML_LIMITS)

    def _preflight(self, profile, state):
        candidate = replace(profile, state=state)
        player = self.players.validate(state)
        encode_response(self._profiles[41], self.players.unit_response(player["units"]))
        encode_response(self._profiles[129], dumps(Container(children=(self.possessions.response(state, include_enabled=True),))))
        encode_response(self._profiles[137], self.bootstrap._configuration_response(candidate))

    def _replay(self, profile, receipt, product):
        if receipt["revision"] != profile.revision or receipt["content"] not in self.replayable_content_versions():
            raise ProtocolError("tower_trade_retry_stale", "Tower state or content changed after the purchase", 409)
        player = self.players.validate(profile.state)
        indexed = {unit["GetIndex"]: unit for unit in player["units"]}
        units = [indexed.get(index) for index in receipt["unit_indices"]]
        record = self.history.records(profile.state).get(product.shop_id)
        if (any(unit is None or unit["CardID"] != product.card_id for unit in units)
                or len(units) != (receipt["quantity"] if product.card_id else 0)
                or record is None or record.lifetime != receipt["purchased_count"]):
            _receipt_error()
        # The purchase saved its own month, so a retry rebuilds that month's counts without reading the clock.
        history = (self.history.response(profile.state, product.currency_item_id, record.period)
                   if receipt["content"] == self.content_version
                   else self.history.legacy_response(profile.state, product.currency_item_id))
        expected = self._response(profile, profile.state, product, units, history)
        if receipt["response"].encode("utf-8") != expected:
            _receipt_error()
        return expected

    def _purchase(self, envelope, store):
        columns = registered(envelope, {69}, ("SHOP", "NUM"), ("SHOP", "NUM"))
        shop_id, quantity = (scalar(columns[key], "I") for key in ("SHOP", "NUM"))
        if shop_id <= 0 or quantity <= 0:
            invalid("Tower purchases require a positive product and quantity")
        key = hashlib.sha256(b"69\0" + envelope.xml).hexdigest()
        now = None
        for _ in range(3):
            self._ready()
            profile = self.players.authenticate(envelope, store)
            receipts = self._receipts(profile.state, profile.revision, self.history.records(profile.state))
            requests = receipts[LEDGER_KEY]
            product = self.catalog.product(shop_id)
            receipt = self._ledger.receipt(requests, key)
            if receipt is not None:
                if (receipt["shop_id"], receipt["quantity"]) != (shop_id, quantity):
                    _receipt_error()
                return self._replay(profile, receipt, product)
            if self._ledger.processed(requests, key):
                raise ProtocolError("tower_trade_request_stale", "Tower request was already processed", 409)
            if now is None:
                now = self.clock()
            self.grants.require_supported(product)
            quote = self.history.quote(profile.state, product, quantity, now)
            updated = self.history.record_purchase(profile.state, product, quantity, now)
            updated = self.possessions.debit(updated, {product.currency_item_id: quote.total_price})
            updated, units, _ = self.grants.apply(updated, product, quantity)
            self._preflight(profile, updated)
            self._ready()
            response = self._response(profile, updated, product, units,
                                      self.history.response(updated, product.currency_item_id, quote.period))
            encode_response(envelope.profile, response)
            if len(response) > RESPONSE_LIMIT:
                raise ProtocolError("tower_trade_response_capacity", "Tower response exceeds its receipt limit", 503)
            self._ledger.record(receipts, requests, {"request": key, "content": self.content_version,
                "revision": profile.revision + 1, "response": response.decode("utf-8"), "shop_id": shop_id,
                "quantity": quantity, "purchased_count": self.history.lifetime(updated, product),
                "unit_indices": [unit["GetIndex"] for unit in units]})
            receipts["history_digest"] = history_digest(updated)
            updated[RECEIPT_NAMESPACE] = receipts
            self._receipts(updated, profile.revision + 1, self.history.records(updated))
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving the tower purchase", 409)

    def status(self):
        self._ready()
        return {"ready": True, "implemented_operations": [65, 69], "content_version": self.content_version,
            "catalog": self.catalog.status(), "grants": self.grants.status(), "history": self.history.status(),
            "native_tested": False,
            "limit_policy": "Limits and price tiers count this business month's purchases; counts restart each month",
            "retry_policy": "Exact request at its committed revision, under the current or the never-reset history "
                            "version, answers its committed bytes",
            "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT, "request_order": REQUEST_ORDER,
            "refusals": f"{REFUSAL_POLICY}; a refused 65 has already cleared its purchase counts"}
