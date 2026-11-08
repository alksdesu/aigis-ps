from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re

from .alxml import Column, Container
from .energy_clock import JST
from .errors import ProtocolError
from .native_clock import day_one_milliseconds
from .native_numbers import INT32_MAX, int32
from .trade_catalog import PRODUCT_UNKNOWN


MAX_PRODUCTS = 65535
_ID = re.compile(r"[1-9][0-9]{0,9}\Z")


@dataclass(frozen=True)
class TradeRules:
    reset_type_1: str = "month"
    reset_type_2: str = "window"

    def __post_init__(self):
        if self.reset_type_1 not in ("unconfigured", "never", "month"):
            raise ValueError("Reset type 1 must be unconfigured, never or month")
        if self.reset_type_2 not in ("unconfigured", "never", "window"):
            raise ValueError("Reset type 2 must be unconfigured, never or window")


def server_milliseconds(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ProtocolError("trade_clock_invalid", "Trade history requires an aware server clock", 503)
    try:
        return day_one_milliseconds(now)
    except (OverflowError, ValueError) as error:
        raise ProtocolError("trade_clock_invalid", "Trade clock cannot be represented in UTC", 503) from error


class TradeHistory:
    def __init__(self, catalog, rules=None):
        self.catalog = catalog
        self.rules = TradeRules() if rules is None else rules
        if not isinstance(self.rules, TradeRules):
            raise ValueError("Trade history requires validated local rules")

    def period(self, product, now):
        server_milliseconds(now)
        mode = "never" if product.reset_type == 0 else getattr(self.rules, f"reset_type_{product.reset_type}", "unconfigured")
        if mode == "month":
            return "month:" + now.astimezone(JST).strftime("%Y-%m")
        if mode == "window":
            window = [value.isoformat() if value is not None else None for value in (product.start_at, product.end_at)]
            return "window:" + hashlib.sha256(json.dumps(window).encode()).hexdigest()
        return mode

    def _product(self, identifier):
        try:
            return self.catalog.product(identifier)
        except ProtocolError as error:
            if error.code != PRODUCT_UNKNOWN:
                raise
            return None

    @staticmethod
    def _invalid():
        raise ProtocolError("trade_history_invalid", "Trade purchase history is invalid", 503)

    def _native_rows(self, state):
        if (not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int
                or state["bootstrap_schema"] != 1 or not isinstance(state.get("tables"), dict)):
            self._invalid()
        rows = state["tables"].get("exchange_history", [])
        if not isinstance(rows, list) or len(rows) > MAX_PRODUCTS:
            self._invalid()
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"ShopItemID", "ExchangeCount"}
                    or not int32(row["ShopItemID"]) or row["ShopItemID"] <= 0
                    or not int32(row["ExchangeCount"]) or row["ExchangeCount"] < 0
                    or row["ShopItemID"] in seen):
                self._invalid()
            seen.add(row["ShopItemID"])
        return rows

    def records(self, state, now):
        server_milliseconds(now)
        native = self._native_rows(state)
        history = state.get("trade_history")
        if "trade_history" not in state:
            records = {}
            for row in native:
                product = self._product(row["ShopItemID"])
                count = row["ExchangeCount"]
                records[str(row["ShopItemID"])] = {"period": self.period(product, now) if product else "legacy",
                    "count": count, "lifetime_count": count}
            return records
        if (not isinstance(history, dict) or set(history) != {"schema_version", "products", "last_server_time"}
                or type(history["schema_version"]) is not int or history["schema_version"] != 1
                or type(history["last_server_time"]) is not int or not 0 <= history["last_server_time"] < 2 ** 63
                or not isinstance(history["products"], dict) or len(history["products"]) > MAX_PRODUCTS):
            self._invalid()
        for identifier, row in history["products"].items():
            if (not isinstance(identifier, str) or not _ID.fullmatch(identifier) or int(identifier) > INT32_MAX
                    or not isinstance(row, dict) or set(row) != {"period", "count", "lifetime_count"}
                    or any(not int32(row[field]) or row[field] < 0 for field in ("count", "lifetime_count"))
                    or row["lifetime_count"] < row["count"] or not isinstance(row["period"], str)
                    or len(row["period"]) > 80 or row["period"] not in ("legacy", "unconfigured", "never")
                    and re.fullmatch(r"month:[0-9]{4}-[0-9]{2}|window:[a-f0-9]{64}", row["period"]) is None):
                self._invalid()
            if row["period"].startswith("month:"):
                try:
                    datetime.strptime(row["period"], "month:%Y-%m")
                except ValueError:
                    self._invalid()
        return deepcopy(history["products"])

    def _effective_count(self, records, product, now):
        row = records.get(str(product.shop_id))
        if row is None:
            return 0
        current = self.period(product, now)
        previous = row["period"]
        if current == previous or ":" not in previous or current.split(":")[0] != previous.split(":")[0]:
            return row["count"]
        if current.startswith("month:") and current < previous:
            return row["count"]
        return 0

    def count(self, records, product, now):
        return 0 if product.data_restrict == 0 else self._effective_count(records, product, now)

    def _rows(self, records, now):
        result = []
        for identifier, row in sorted(records.items(), key=lambda item: int(item[0])):
            product = self._product(int(identifier))
            count = self.count(records, product, now) if product is not None else row["count"]
            result.append({"ShopItemID": int(identifier), "ExchangeCount": count})
        return result

    def response(self, state, now, identifiers=None):
        rows = self._rows(self.records(state, now), now)
        if identifiers is not None:
            rows = [row for row in rows if row["ShopItemID"] in identifiers]
        return Container("ExchangeHistory", (
            Column("ShopItemID", "I", tuple(row["ShopItemID"] for row in rows)),
            Column("ExchangeCount", "I", tuple(row["ExchangeCount"] for row in rows))))

    def initialize(self, state, now):
        records = self.records(state, now)
        if not records:
            return deepcopy(state)
        changed = "trade_history" not in state
        for identifier, row in records.items():
            product = self._product(int(identifier))
            if product is None:
                continue
            current = self.period(product, now)
            if current != "unconfigured" and current.split(":")[0] != row["period"].split(":")[0]:
                row["period"] = current
                changed = True
        if not changed:
            return deepcopy(state)
        timestamp = server_milliseconds(now)
        if timestamp < state.get("trade_history", {}).get("last_server_time", 0):
            raise ProtocolError("trade_clock_backwards", "The server clock precedes the latest trade", 409)
        updated = deepcopy(state)
        updated["trade_history"] = {"schema_version": 1, "products": records, "last_server_time": timestamp}
        updated["tables"]["exchange_history"] = self._rows(records, now)
        return updated

    def record_purchase(self, state, product, quantity, now):
        if not int32(quantity) or quantity <= 0:
            raise ProtocolError("trade_quantity_invalid", "Trade quantity must be a positive int32")
        period = self.period(product, now)
        if period == "unconfigured":
            raise ProtocolError("trade_reset_unconfigured", "The local reset policy for this product has not been configured", 501)
        records = self.records(state, now)
        current_time = server_milliseconds(now)
        if current_time < state.get("trade_history", {}).get("last_server_time", 0):
            raise ProtocolError("trade_clock_backwards", "The server clock precedes the latest trade", 409)
        previous = records.get(str(product.shop_id), {"count": 0, "lifetime_count": 0, "period": period})
        count = self._effective_count(records, product, now) + quantity
        lifetime = previous["lifetime_count"] + quantity
        if not int32(count) or not int32(lifetime):
            raise ProtocolError("trade_history_capacity", "Trade counters exceed the native integer range", 409)
        if product.data_restrict and count > product.data_restrict:
            raise ProtocolError("trade_limit_exceeded", "The requested quantity exceeds the remaining purchase limit", 409)
        if len(records) >= MAX_PRODUCTS and str(product.shop_id) not in records:
            raise ProtocolError("trade_history_capacity", "Trade product history is full", 409)
        records[str(product.shop_id)] = {"period": period, "count": count, "lifetime_count": lifetime}
        updated = deepcopy(state)
        updated["trade_history"] = {"schema_version": 1, "products": records, "last_server_time": current_time}
        updated["tables"]["exchange_history"] = self._rows(records, now)
        return updated
