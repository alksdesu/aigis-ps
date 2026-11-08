from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType

from .alxml import Column, Container, dumps
from .errors import ProtocolError
from .native_numbers import INT32_MAX
# Journals saved under the earlier 256-byte bound keep loading; only new entitlements must fit the native cell.
from .possessions import STORED_ITEM_NAME_BYTES as STORED_ITEM_BYTES
from .protocol import NATIVE_CELL_BYTES


MAX_PACKAGES = 4096
MAX_ITEM_BYTES = NATIVE_CELL_BYTES
IDENTITY_UNREPLAYABLE = "package_identity_unreplayable"
POLICY_VERSION = "local-free-pass-once-1"
LEGACY_OWNED, LOCAL_FREE = "legacy_owned", "local_free"
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _invalid(message="Package purchase state is invalid"):
    raise ProtocolError("package_purchase_state_invalid", message, 503)


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _text(value):
    try:
        if type(value) is not str or not value or len(value.encode("utf-8")) > STORED_ITEM_BYTES:
            raise ValueError("Invalid package identity")
        dumps(Container(children=(Column("ID", "S", (value,)),)))
    except (ValueError, TypeError, UnicodeError):
        _invalid("Package identities require bounded valid text")
    return value


def _native(offer):
    return all(len(name.encode("utf-8")) <= MAX_ITEM_BYTES for name in (offer.normal_pass_id, offer.item_id))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode("utf-8")).hexdigest()


def package_identity(normal_pass_id, item_id):
    return _digest({"schema_version": 1, "normal": _text(normal_pass_id), "premium": _text(item_id)})


@dataclass(frozen=True)
class _Offer:
    normal_pass_id: str
    item_id: str
    package_id: int
    flag_id: int
    sku: str
    definition_hash: str


def _offer(value):
    try:
        result = _Offer(*[getattr(value, field) for field in _Offer.__dataclass_fields__])
    except (AttributeError, TypeError):
        _invalid("Package offer metadata is incomplete")
    _text(result.sku)
    identity = package_identity(result.normal_pass_id, result.item_id)
    if (any(type(value) is not int or not 1 <= value <= INT32_MAX
            for value in (result.package_id, result.flag_id))
            or result.normal_pass_id == result.item_id or result.definition_hash != identity):
        _invalid("Package offer identity is inconsistent")
    return result


class PackagePurchases:
    def __init__(self, catalog, possessions):
        self.catalog, self.possessions = catalog, possessions
        self._catalog_version = catalog.content_version
        if not _hash(self._catalog_version) or not callable(getattr(possessions, "counts", None)):
            _invalid("Package purchase dependencies are unavailable")
        offers = self._current_offers()
        self._offers = MappingProxyType({offer.normal_pass_id: offer for offer in offers})
        self._snapshot = offers
        self.content_version = _digest({"policy": POLICY_VERSION,
            "identities": {key: value.definition_hash for key, value in self._offers.items()}})

    def _current_offers(self):
        try:
            values = self.catalog.offers
            if not isinstance(values, (list, tuple)) or len(values) > MAX_PACKAGES:
                _invalid("Package offers must be a bounded collection")
            offers = tuple(_offer(value) for value in values)
        except AttributeError:
            _invalid("Package offers are unavailable")
        for field in ("normal_pass_id", "item_id", "package_id", "flag_id", "sku"):
            if len({getattr(value, field) for value in offers}) != len(offers):
                _invalid("Package offer identities must be distinct")
        return offers

    def _ready(self):
        ready = getattr(self.catalog, "_ready", None)
        if callable(ready):
            ready()
        if (self.catalog.content_version != self._catalog_version
                or self._current_offers() != self._snapshot):
            raise ProtocolError("package_purchase_definition_changed", "Package catalog changed after initialization", 503)

    def status(self):
        try:
            self._ready()
            error = None
        except (ProtocolError, AttributeError, ValueError):
            error = "package_purchase_dependencies_unavailable"
        return {"ready": error is None, "error": error, "content_version": self.content_version,
                "local_rules": {"policy": POLICY_VERSION, "purchase_scope": "normal_pass_id",
                                LEGACY_OWNED: "record_without_grant", "purchase_limit": 1},
                "identity_policy": (f"New entitlements need identities within the {MAX_ITEM_BYTES}-byte native cell; "
                                    f"stored identities up to {STORED_ITEM_BYTES} bytes stay readable")}

    def read(self, state):
        self._ready()
        if (not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int
                or state["bootstrap_schema"] != 1 or not isinstance(state.get("tables"), dict)):
            _invalid("Package purchases require a verified bootstrap state")
        if "package_purchases" not in state:
            return {}
        ledger = state["package_purchases"]
        if (not isinstance(ledger, dict) or set(ledger) != {"schema_version", "entries"}
                or type(ledger["schema_version"]) is not int or ledger["schema_version"] != 1
                or not isinstance(ledger["entries"], dict) or len(ledger["entries"]) > MAX_PACKAGES):
            _invalid("Package purchase journal is invalid")
        requests = set()
        for normal, entry in ledger["entries"].items():
            _text(normal)
            if (not isinstance(entry, dict) or set(entry) != {"identity", "count", "source", "request"}
                    or not _hash(entry["identity"]) or type(entry["count"]) is not int or entry["count"] != 1
                    or entry["source"] not in (LEGACY_OWNED, LOCAL_FREE)
                    or entry["source"] == LEGACY_OWNED and entry["request"] is not None
                    or entry["source"] == LOCAL_FREE and not _hash(entry["request"])):
                _invalid("Package purchase record is invalid")
            if entry["source"] == LOCAL_FREE:
                if entry["request"] in requests:
                    _invalid("Package purchase requests must not be shared by distinct passes")
                requests.add(entry["request"])
            offer = self._offers.get(normal)
            if offer is not None and entry["identity"] != offer.definition_hash:
                raise ProtocolError("package_purchase_identity_changed", "Saved package entitlement identity changed", 503)
        return deepcopy(ledger["entries"])

    def initialize(self, state):
        entries = self.read(state)
        counts = self.possessions.counts(state)
        for normal, offer in self._offers.items():
            if normal not in entries and counts.get(offer.item_id, 0) > 0:
                entries[normal] = {"identity": offer.definition_hash, "count": 1,
                                   "source": LEGACY_OWNED, "request": None}
        if len(entries) > MAX_PACKAGES:
            _invalid("Package purchase journal exceeds its storage limit")
        result = deepcopy(state)
        result["package_purchases"] = {"schema_version": 1, "entries": entries}
        self.read(result)
        return result

    def mark(self, state, offer, request):
        entries = self.read(state)
        if "package_purchases" not in state:
            _invalid("Package purchase journal must be initialized before awarding entitlements")
        selected = _offer(offer)
        if self._offers.get(selected.normal_pass_id) != selected:
            raise ProtocolError("package_purchase_offer_unknown", "Package offer is not in the frozen catalog", 503)
        if not _hash(request):
            _invalid("Package purchase request identity is invalid")
        previous = entries.get(selected.normal_pass_id)
        if previous is not None:
            if previous["source"] == LOCAL_FREE and previous["request"] == request:
                return deepcopy(state)
            raise ProtocolError("package_already_purchased", "This pass entitlement was already acquired", 409)
        if any(entry["request"] == request for entry in entries.values()):
            _invalid("Package purchase request belongs to another pass")
        if not _native(selected):
            raise ProtocolError(IDENTITY_UNREPLAYABLE, "New package entitlements need identities that fit the native cell", 503)
        if len(entries) >= MAX_PACKAGES:
            _invalid("Package purchase journal exceeds its storage limit")
        entries[selected.normal_pass_id] = {"identity": selected.definition_hash, "count": 1,
                                            "source": LOCAL_FREE, "request": request}
        result = deepcopy(state)
        result["package_purchases"]["entries"] = entries
        self.read(result)
        return result
