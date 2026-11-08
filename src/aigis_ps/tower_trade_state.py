from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import re
from types import MappingProxyType

from .alxml import Column, Container
from .business_calendar import BUSINESS_DAY_START_HOUR, CALENDAR_MONTH_TEXT, business_month
from .errors import ProtocolError
from .native_numbers import INT32_MAX, int32
from .tower_trade_catalog import TowerTradeProduct


HISTORY_KEY = "tower_trade_history"
SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
MAX_RECORDS = 65535
RESET_POLICY = "business_month"
REBIND_UNNEEDED = "tower_trade_rebind_unneeded"
REBIND_OPERATION = "tower_trade.rebind_product"
PRODUCT_REUSED = "tower_trade_product_reused"
_HISTORY_FIELDS = frozenset(("schema_version", "records"))
_RECORD_FIELDS = frozenset(("source", "period", "count", "lifetime"))
_LEGACY_RECORD_FIELDS = frozenset(("count", "source"))
_ID = re.compile(r"[1-9][0-9]{0,9}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_SHARED_TERMS = {"source_identity": "card_item_currency_effect", "batch_crossing_price_tiers": "unavailable",
                 "date": "both_boundaries_required", "disclosure": "all_titles_and_owned_item"}
# Receipts committed while counts never reset carry the history version built from these terms.
LEGACY_TERMS = MappingProxyType({**_SHARED_TERMS, "schema": LEGACY_SCHEMA_VERSION, "reset_policy": "never",
                                 "price": "current_purchase_ordinal_tier",
                                 "response": "all_currencies_and_retained_records"})
# Receipts bind the version built from these terms, so changing them must keep this version replayable.
TERMS = MappingProxyType({**_SHARED_TERMS, "schema": SCHEMA_VERSION, "reset_policy": RESET_POLICY,
                          "price": "current_month_purchase_ordinal_tier",
                          "response": "all_currencies_and_retained_records_with_current_month_counts",
                          "legacy_records": "lifetime_counts_with_an_empty_current_month",
                          "clock_regression": "saved_month_count_kept_and_its_purchases_refused"})


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _invalid():
    raise ProtocolError("tower_trade_history_invalid", "Tower purchase history is invalid", 503)


def _source_valid(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _period_valid(value):
    return value is None or (type(value) is str and CALENDAR_MONTH_TEXT.fullmatch(value) is not None)


@dataclass(frozen=True)
class TowerTradeRecord:
    source: str
    period: str | None
    count: int
    lifetime: int

    def ahead_of(self, period):
        return self.period is not None and period is not None and self.period > period

    def current(self, period):
        # A clock set back before the saved month keeps that month's count instead of restarting it.
        return self.count if self.period == period or self.ahead_of(period) else 0

    def saved(self):
        return {"source": self.source, "period": self.period, "count": self.count, "lifetime": self.lifetime}


@dataclass(frozen=True)
class TowerTradeQuote:
    total_price: int
    quantity: int
    currency_item_id: str
    unit_price: int
    previous_count: int
    period: str


def _legacy_row(row):
    if (set(row) != _LEGACY_RECORD_FIELDS or not int32(row["count"]) or row["count"] <= 0
            or not _source_valid(row["source"])):
        _invalid()
    # Counts saved before the monthly reset are lifetime totals, so their current month starts from zero.
    return TowerTradeRecord(row["source"], None, 0, row["count"])


def _current_row(row):
    if (set(row) != _RECORD_FIELDS or not _source_valid(row["source"]) or not _period_valid(row["period"])
            or not int32(row["count"]) or not int32(row["lifetime"])
            or not 0 <= row["count"] <= row["lifetime"] or row["lifetime"] <= 0
            or (row["period"] is None) != (row["count"] == 0)):
        _invalid()
    return TowerTradeRecord(row["source"], row["period"], row["count"], row["lifetime"])


_ROW_READERS = MappingProxyType({LEGACY_SCHEMA_VERSION: _legacy_row, SCHEMA_VERSION: _current_row})


def read_history(state):
    if (not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int
            or state["bootstrap_schema"] != 1 or not isinstance(state.get("tables"), dict)):
        _invalid()
    if HISTORY_KEY not in state:
        return MappingProxyType({})
    history = state[HISTORY_KEY]
    if (not isinstance(history, dict) or set(history) != _HISTORY_FIELDS
            or type(history["schema_version"]) is not int or history["schema_version"] not in _ROW_READERS
            or not isinstance(history["records"], dict) or len(history["records"]) > MAX_RECORDS):
        _invalid()
    read = _ROW_READERS[history["schema_version"]]
    result = {}
    for key, row in history["records"].items():
        if type(key) is not str or not _ID.fullmatch(key) or int(key) > INT32_MAX or not isinstance(row, dict):
            _invalid()
        result[int(key)] = read(row)
    return MappingProxyType(result)


def history_digest(state):
    read_history(state)
    # Receipts bind the history as saved, so a schema 1 history nobody rewrote keeps the digest they recorded.
    records = state[HISTORY_KEY]["records"] if HISTORY_KEY in state else {}
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _saved_history(records):
    return {"schema_version": SCHEMA_VERSION,
            "records": {str(identifier): record.saved() for identifier, record in records.items()}}


class TowerTradeHistory:
    def __init__(self, catalog):
        self.catalog = catalog
        self._catalog = catalog
        catalog._ready()
        self._catalog_version = catalog.content_version
        if type(self._catalog_version) is not str or not _HASH.fullmatch(self._catalog_version):
            raise ValueError("Tower history requires a fixed catalog fingerprint")
        self.content_version = self._version_for(TERMS)
        self.legacy_content_version = self._version_for(LEGACY_TERMS)
        self._content_version = self.content_version

    def _version_for(self, terms):
        return _digest({**terms, "catalog": self._catalog_version})

    def _ready(self):
        if (self.catalog is not self._catalog or self.catalog.content_version != self._catalog_version
                or self.content_version != self._content_version):
            raise ProtocolError("tower_trade_history_changed", "Tower history definitions changed after setup", 503)
        self.catalog._ready()

    @staticmethod
    def source(product):
        return _digest({"schema": 1, "shop_id": product.shop_id, "card_id": product.card_id,
            "item_id": product.item_id, "currency_item_id": product.currency_item_id,
            "item_effect_type": product.item_effect_type})

    def _product(self, product):
        self._ready()
        if type(product) is not TowerTradeProduct or self.catalog.product(product.shop_id) is not product:
            raise ProtocolError("tower_trade_product_invalid", "Tower history requires the frozen catalog product", 503)
        return product

    @staticmethod
    def _now(now):
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ProtocolError("tower_trade_clock_invalid", "Tower exchange requires an aware server clock", 503)
        try:
            return now.astimezone(timezone.utc)
        except (OverflowError, ValueError) as error:
            raise ProtocolError("tower_trade_clock_invalid", "Tower exchange clock is outside the supported range", 503) from error

    @staticmethod
    def _month(moment):
        try:
            return business_month(moment)
        except ProtocolError as error:
            raise ProtocolError("tower_trade_clock_invalid", "Tower exchange clock is outside the client calendar",
                                503) from error

    def period(self, now):
        return self._month(self._now(now))

    def stored(self, state):
        self._ready()
        return read_history(state)

    def _reused(self, records):
        products = self.catalog.by_id
        return tuple(sorted(identifier for identifier, record in records.items()
                            if identifier in products and record.source != self.source(products[identifier])))

    def reused(self, state):
        return self._reused(self.stored(state))

    def records(self, state):
        found = self.stored(state)
        if self._reused(found):
            raise ProtocolError(PRODUCT_REUSED,
                                "Tower product identity differs from its saved purchase record", 503)
        return found

    def initialize(self, state):
        # A reused product stops only 65 and 69, so the save still loads and the console can rebind it.
        self.stored(state)
        return deepcopy(state)

    def _product_record(self, state, product):
        product = self._product(product)
        return self.records(state).get(product.shop_id)

    def count(self, state, product, now):
        record, period = self._product_record(state, product), self.period(now)
        return 0 if record is None else record.current(period)

    def lifetime(self, state, product):
        record = self._product_record(state, product)
        return 0 if record is None else record.lifetime

    def quote(self, state, product, quantity, now):
        product = self._product(product)
        now = self._now(now)
        period = self._month(now)
        if not int32(quantity) or quantity <= 0:
            raise ProtocolError("tower_trade_quantity_invalid", "Tower purchase quantity must be a positive native integer")
        if product.unsupported_reason is not None:
            raise ProtocolError("tower_trade_unsupported", product.unsupported_reason, 501)
        record = self.records(state).get(product.shop_id)
        if record is not None and record.ahead_of(period):
            raise ProtocolError("tower_trade_clock_backwards",
                                "Tower exchange clock precedes the product's saved purchase month", 409)
        previous, lifetime = (0, 0) if record is None else (record.current(period), record.lifetime)
        if lifetime + quantity > INT32_MAX:
            raise ProtocolError("tower_trade_history_capacity", "Tower purchase count exceeds the native range", 409)
        if product.single_buy_only and quantity != 1:
            raise ProtocolError("tower_trade_single_only", "This tower product requires an individual purchase", 409)
        if product.limitation and previous + quantity > product.limitation:
            raise ProtocolError("tower_trade_limit_exceeded", "Tower purchase exceeds this month's remaining limit",
                                409)
        if (product.start_at is not None and product.end_at is not None
                and not product.start_at <= now < product.end_at):
            raise ProtocolError("tower_trade_not_available", "Tower product is outside its availability window", 409)
        if product.disclose_emblem_ids:
            emblems = self.catalog.emblems
            if emblems is None or not callable(getattr(emblems, "eligible", None)):
                raise ProtocolError("tower_trade_disclosure_unavailable", "Tower product requires verified title predicates", 503)
            eligibility = [emblems.eligible(index, state) for index in product.disclose_emblem_ids]
            if any(type(value) is not bool for value in eligibility):
                raise ProtocolError("tower_trade_disclosure_unavailable", "Tower title predicates returned an invalid result", 503)
            if not all(eligibility):
                raise ProtocolError("tower_trade_locked", "Tower title acquisition conditions are not satisfied", 409)
        if product.disclose_item_id:
            held = self.catalog.possessions.counts(state)
            if held.get(product.disclose_item_id, 0) <= 0:
                raise ProtocolError("tower_trade_locked", "Tower item ownership condition is not satisfied", 409)
        ordinal, final_ordinal = previous + 1, previous + quantity
        unit_price = None
        for threshold, price in product.tiers:
            if threshold <= ordinal:
                unit_price = price
            elif threshold <= final_ordinal and price != unit_price:
                raise ProtocolError("tower_trade_tier_batch_unsupported", "A batch crossing different tower prices is unverified", 501)
        if unit_price is None:
            raise ProtocolError("tower_trade_price_invalid", "Tower purchase has no matching price tier", 503)
        total = unit_price * quantity
        if total <= 0 or total > INT32_MAX:
            raise ProtocolError("tower_trade_price_overflow", "Tower total price exceeds the native range", 409)
        return TowerTradeQuote(total, quantity, product.currency_item_id, unit_price, previous, period)

    def record_purchase(self, state, product, quantity, now):
        quote = self.quote(state, product, quantity, now)
        records = self.records(state)
        if product.shop_id not in records and len(records) >= MAX_RECORDS:
            raise ProtocolError("tower_trade_history_capacity", "Tower purchase history has reached its record limit", 409)
        previous = records.get(product.shop_id)
        lifetime = quantity + (0 if previous is None else previous.lifetime)
        purchased = TowerTradeRecord(self.source(product), quote.period, quote.previous_count + quantity, lifetime)
        updated = deepcopy(state)
        updated[HISTORY_KEY] = _saved_history({**records, product.shop_id: purchased})
        return updated

    def response(self, state, currency_item_id, period):
        return self._response(state, currency_item_id, lambda record: record.current(period))

    def legacy_response(self, state, currency_item_id):
        # Purchases answered before the monthly reset served lifetime totals, which their receipts still hold.
        return self._response(state, currency_item_id, lambda record: record.lifetime)

    def _response(self, state, currency_item_id, count):
        self._ready()
        if type(currency_item_id) is not str or currency_item_id not in self.catalog.currencies:
            raise ProtocolError("tower_trade_currency_unknown", "Tower history requires a current currency identity")
        rows = [(identifier, count(record)) for identifier, record in sorted(self.records(state).items())]
        return Container("TowerTradeHistory", (
            Column("ID", "I", tuple(identifier for identifier, _ in rows)),
            Column("TradeCount", "I", tuple(value for _, value in rows))))

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "content_version": self.content_version,
                "legacy_content_version": self.legacy_content_version, "reset_policy": RESET_POLICY,
                "reset_boundary": f"First day of each month at JST {BUSINESS_DAY_START_HOUR:02d}:00",
                "limits_and_prices": "Follow the current business month's count",
                "clock_regression": "A clock set back before a product's saved month keeps that month's count and "
                                    "refuses the product's purchases until the clock reaches it again",
                "legacy_records": "Schema 1 counts read as lifetime totals with the current month starting from zero; "
                                  "reading never rewrites them",
                "resource_updates": "preserve_counts_for_same_product_identity",
                "reused_products": f"The save still loads; 65 and 69 refuse until {REBIND_OPERATION} binds "
                                   "the record to the current product",
                "removed_products": "preserve_in_full_history", "native_tested": False,
                "refusals": "Rejected before charging; history and balances stay unchanged while the native client "
                            "shows a communication error and resends the request"}


def rebind(state, product, carry):
    if type(product) is not TowerTradeProduct:
        raise ProtocolError("tower_trade_product_invalid", "Tower history requires the frozen catalog product", 503)
    if type(carry) is not bool:
        raise ProtocolError("tower_trade_rebind_invalid", "Rebinding a tower product needs an explicit carry choice")
    records = read_history(state)
    record, source = records.get(product.shop_id), TowerTradeHistory.source(product)
    if record is None:
        raise ProtocolError(REBIND_UNNEEDED, "The tower product has no saved purchase record to rebind", 409)
    if record.source == source:
        raise ProtocolError(REBIND_UNNEEDED, "The saved purchase record already belongs to the current product", 409)
    # Receipts check each retained purchase against the lifetime total, so only the month's count restarts.
    rebound = replace(record, source=source) if carry else TowerTradeRecord(source, None, 0, record.lifetime)
    updated = deepcopy(state)
    updated[HISTORY_KEY] = _saved_history({**records, product.shop_id: rebound})
    return updated
