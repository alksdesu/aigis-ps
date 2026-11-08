from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from types import MappingProxyType

from .al_format import decompress_all4, parse_table
from .table_cell_patch import LIMITS, MAX_BYTES, patch_table_cells
from .trade_catalog import SHOP_FIELDS, SHOP_RESOURCE, native_milliseconds, native_moment, product_headers


SCHEMA_VERSION = 1
REGISTRATION = 108
POLICY = "ended_sale_window_818200"
START_FIELD, END_FIELD = "StartDateTime", "EndDateTime"
COUNT_FIELD, IDENTITY_FIELD, NAME_FIELD = "TradeCount", "ShopID", "ProductName"
LAYOUT = tuple(SHOP_FIELDS.items())
# 80DBA0 sends 108 only for these products; every other product maps through 81A5F0 to a handled trade.
SELECTION = MappingProxyType({"ProductType": 2, "Type": 4})
# 818200 reads 0 as unbounded, so 1 to 2 ms after 0001-01-01 JST is the earliest window that has already ended.
CLOSED_WINDOW = MappingProxyType({START_FIELD: 1, END_FIELD: 2})
TERM_FIELDS = frozenset(("registration", "policy", "table", "selection", "window"))
METADATA_FIELDS = TERM_FIELDS | {"schema_version", "source_sha256", "effective_sha256", "products"}
PRODUCT_FIELDS = frozenset(("shop_id", "name", "row", "rows", "original"))


def _iso(milliseconds: int) -> str | None:
    moment = native_moment(milliseconds)
    return None if moment is None else moment.isoformat(timespec="milliseconds")


def _window(start: int, end: int) -> dict:
    return {"start_at": _iso(start), "end_at": _iso(end)}


def closure_terms() -> dict:
    return {"registration": REGISTRATION, "policy": POLICY, "table": SHOP_RESOURCE, "selection": dict(SELECTION),
            "window": _window(CLOSED_WINDOW[START_FIELD], CLOSED_WINDOW[END_FIELD])}


@dataclass(frozen=True)
class ClosedProduct:
    shop_id: int
    name: str
    row: int
    rows: int
    original: Mapping[str, str | None]

    def to_dict(self) -> dict:
        return {"shop_id": self.shop_id, "name": self.name, "row": self.row, "rows": self.rows,
                "original": dict(self.original)}


@dataclass(frozen=True)
class ShopWindowClosure:
    replacements: Mapping[str, bytes]
    products: tuple[ClosedProduct, ...]
    source_sha256: str
    effective_sha256: str

    @property
    def targets(self) -> tuple[str, ...]:
        return (SHOP_RESOURCE,)

    @property
    def shop_ids(self) -> tuple[int, ...]:
        return tuple(product.shop_id for product in self.products)

    @property
    def metadata(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, **closure_terms(), "source_sha256": self.source_sha256,
                "effective_sha256": self.effective_sha256, "products": [product.to_dict() for product in self.products]}


def _parse(payload) -> dict:
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_BYTES:
        raise ValueError(f"Shop window closure requires bounded immutable bytes: {SHOP_RESOURCE}")
    try:
        table = parse_table(decompress_all4(payload, LIMITS) if payload.startswith(b"ALL4") else payload, LIMITS)
    except ValueError as error:
        raise ValueError(f"Shop window closure resource is not a native table: {SHOP_RESOURCE}") from error
    if tuple((field["name"], field["type_code"]) for field in table["fields"]) != LAYOUT:
        raise ValueError(f"Shop window closure requires the native column layout of {SHOP_RESOURCE}")
    return table


def _spans(rows) -> tuple[tuple[int, int], ...]:
    counts = [row[COUNT_FIELD] for row in rows]
    # Positive counts keep the signed reading here equal to the unsigned comparison of 819320.
    if any(count < 1 for count in counts):
        raise ValueError(f"Shop window closure groups price rows by positive TradeCount values: {SHOP_RESOURCE}")
    headers = product_headers(counts)
    return tuple(zip(headers, (*headers[1:], len(rows))))


def _selected(rows) -> tuple[ClosedProduct, ...]:
    products, seen = [], set()
    for start, end in _spans(rows):
        row = rows[start]
        if any(row[name] != value for name, value in SELECTION.items()):
            continue
        identifier = row[IDENTITY_FIELD]
        if identifier <= 0 or identifier in seen:
            raise ValueError(f"Shop window closure requires distinct positive ShopIDs: {SHOP_RESOURCE}")
        seen.add(identifier)
        original = _window(*(native_milliseconds(row[name]) for name in (START_FIELD, END_FIELD)))
        products.append(ClosedProduct(identifier, row[NAME_FIELD], start, end - start, MappingProxyType(original)))
    return tuple(products)


def _confirm(source: dict, payload: bytes, products: tuple[ClosedProduct, ...]) -> str:
    result = _parse(payload)
    if len(result["rows"]) != len(source["rows"]):
        raise ValueError(f"Shop window closure changed the table layout: {SHOP_RESOURCE}")
    closed = {product.row for product in products}
    for index, (before, after) in enumerate(zip(source["rows"], result["rows"])):
        expected = dict(before)
        if index in closed:
            if any(native_milliseconds(after[name]) != value for name, value in CLOSED_WINDOW.items()):
                raise ValueError(f"Shop window closure did not end the selected sale windows: {SHOP_RESOURCE}")
            expected.update({name: after[name] for name in CLOSED_WINDOW})
        if after != expected:
            raise ValueError(f"Shop window closure changed cells outside the selected sale windows: {SHOP_RESOURCE}")
    return hashlib.sha256(payload).hexdigest()


def build_shop_window_closure(payload) -> ShopWindowClosure:
    source = _parse(payload)
    products = _selected(source["rows"])
    effective = patch_table_cells(payload, {product.row: dict(CLOSED_WINDOW) for product in products})
    return ShopWindowClosure(MappingProxyType({SHOP_RESOURCE: effective} if effective != payload else {}), products,
                             hashlib.sha256(payload).hexdigest(), _confirm(source, effective, products))
