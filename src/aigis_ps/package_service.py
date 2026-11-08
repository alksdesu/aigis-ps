from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from functools import partial
import hashlib
import json
import logging
import re

from .alxml import Column, Container, Limits, dumps, loads
from .bootstrap import STARTUP_COLLECTION_LIMITS
from .energy_clock import JST, instant
from .errors import ProtocolError
from .native_clock import NATIVE_EPOCH
from .native_numbers import int32
from .package_state import IDENTITY_UNREPLAYABLE, LOCAL_FREE, STORED_ITEM_BYTES, PackagePurchases
from .protocol import ProtocolRegistry, encode_response
from .request_fields import registered, scalar
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger


RESPONSE_LIMIT = 512 * 1024
REPLY_LIMITS = Limits(max_bytes=RESPONSE_LIMIT, max_elements=500000, max_values=500000)
HISTORY_NAMESPACE = "package_history"
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_IDENTITY_FIELDS = ("normal_pass_id", "item_id", "package_id", "flag_id", "sku")
_LOGIN_FIELDS = ("kind", "campaign_id", "purchase_id", "package_id", "flag_id", "sku")
_RECEIPT_IDENTITY_FIELDS = tuple(dict.fromkeys((*_IDENTITY_FIELDS, *_LOGIN_FIELDS)))
_CURRENT = object()
log = logging.getLogger("aigis_ps")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


def _invalid():
    raise ProtocolError("package_history_invalid", "Saved package receipt does not match its entitlement", 503)


def _identity(value):
    result = {key: getattr(value, key) for key in _IDENTITY_FIELDS}
    return result


def _valid_identity(value):
    for key in ("package_id", "flag_id"):
        if not int32(value[key]) or value[key] <= 0:
            _invalid()
    for key in ("normal_pass_id", "item_id", "sku"):
        try:
            if not isinstance(value[key], str) or not 0 < len(value[key].encode("utf-8")) <= STORED_ITEM_BYTES:
                _invalid()
        except UnicodeError:
            _invalid()


def _login_identity(offer, purchase_id):
    return {"kind": "login", "campaign_id": offer.campaign_id, "purchase_id": purchase_id,
            "package_id": offer.package_id, "flag_id": offer.flag_id, "sku": offer.sku}


def _valid_login_identity(value):
    if value["kind"] != "login" or any(not int32(value[key]) or value[key] <= 0
            for key in ("campaign_id", "purchase_id", "package_id", "flag_id")):
        _invalid()
    try:
        if type(value["sku"]) is not str or not 0 < len(value["sku"].encode("utf-8")) <= STORED_ITEM_BYTES:
            _invalid()
    except UnicodeError:
        _invalid()


def _receipt_valid(entry):
    identity = entry["identity"]
    if not isinstance(identity, dict) or set(identity) not in (set(_IDENTITY_FIELDS), set(_LOGIN_FIELDS)):
        return False
    (_valid_login_identity if "kind" in identity else _valid_identity)(identity)
    return True


def _merge_tables(left, right, key):
    if (left.name != right.name or [(node.name, node.type) for node in left.children]
            != [(node.name, node.type) for node in right.children]):
        raise ProtocolError("package_directory_invalid", "Package directory schemas differ", 503)
    result = Container(left.name, tuple(Column(a.name, a.type, a.values + b.values)
                                       for a, b in zip(left.children, right.children)))
    identifiers = next(node.values for node in result.children if node.name == key)
    if len(identifiers) != len(set(identifiers)):
        raise ProtocolError("package_directory_conflict", "Local package identities overlap", 503)
    return result


def _native_days(value):
    delta = instant(value).astimezone(JST) - NATIVE_EPOCH
    return delta.days + (delta.seconds + delta.microseconds / 1000000) / 86400


class PackageService:
    def __init__(self, bootstrap, players, possessions, catalog, *, purchases=None, purchased_logins=None):
        self.bootstrap, self.players = bootstrap, players
        self.possessions, self.catalog = possessions, catalog
        self.purchases = purchases or PackagePurchases(catalog, possessions)
        self.purchased_logins = purchased_logins
        self.legacy_content_version = self._content(purchased=None)
        self.content_version = self._content()
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (46, 47, 50, 129, 137)}
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "package_history_invalid",
                                     "Saved package receipt does not match its entitlement",
                                     fields=("request", "content", "state", "revision", "response", "identity"),
                                     response_limit=RESPONSE_LIMIT, check=_receipt_valid,
                                     legacy_nesting={"identity": _RECEIPT_IDENTITY_FIELDS})

    def _content(self, *, purchased=_CURRENT, catalog=None):
        try:
            if purchased is _CURRENT:
                purchased = None if self.purchased_logins is None else self.purchased_logins.content_version
            catalog = self.catalog.content_version if catalog is None else catalog
            sources = {"catalog": catalog, "purchases": self.purchases.content_version,
                       "inventory": self.possessions.asset_sha256}
            if (self.catalog.possessions is not self.possessions or self.purchases.catalog is not self.catalog
                    or self.purchases.possessions is not self.possessions
                    or any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in sources.values())):
                raise ValueError("Package dependencies must share one resource snapshot")
            content = {"schema": 1, "sources": sources, "price": 0, "limit": "once_per_normal_pass",
                       "credit": "direct_premium_entitlement", "payment": "native_free_package"}
            if purchased is not None:
                if type(purchased) is not str or not _HASH.fullmatch(purchased):
                    raise ValueError("Purchased login packages require a frozen content version")
                content["purchased_logins"] = purchased
            return _digest(content)
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError("package_dependencies_invalid", "Local package dependencies are unavailable", 503) from error

    def _legacy_directory(self, content, identity):
        # Premium receipts committed before login packages joined the digest list only the premium pass flags.
        return "kind" not in identity and content != self.content_version and content in (
            self.legacy_content_version, self._content(purchased=None, catalog=self.catalog.legacy_window_content_version))

    def _replayable(self, content):
        # A receipt keeps the digest of the gift box its login packages fingerprinted when it committed.
        purchased = () if self.purchased_logins is None else tuple(self.purchased_logins.replayable_content_versions())
        if any(self._content(purchased=version) == content for version in purchased):
            return True
        # Receipts committed while pass windows compared JST instants keep that pass catalog digest.
        legacy_window = self.catalog.legacy_window_content_version
        return any(self._content(purchased=version, catalog=legacy_window) == content for version in (_CURRENT, *purchased))

    def _ready(self):
        self.catalog._ready()
        self.purchases._ready()
        if self.purchased_logins is not None:
            self.purchased_logins._ready()
            other = self.purchased_logins.catalog.offers
            for field in ("sku", "package_id", "flag_id"):
                if {getattr(offer, field) for offer in self.catalog.offers}.intersection(
                        getattr(offer, field) for offer in other):
                    raise ProtocolError("package_directory_conflict", "Local package identities overlap", 503)
        if (not self.possessions.is_ready() or self._content() != self.content_version
                or self._content(purchased=None) != self.legacy_content_version):
            raise ProtocolError("package_dependencies_changed", "Local package dependencies changed after setup", 503)

    def handlers(self):
        return {46: self.read, 47: self.refresh, 50: self.purchase}

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "content_version": self.content_version,
                "implemented_operations": [46, 47, 50], "catalog": self.catalog.status(),
                "purchase_limit": "Once per normal pass; old ownership is retained without granting",
                "payment": "Native free package branch; no platform payment request",
                "purchased_logins": self.purchased_logins.status() if self.purchased_logins is not None else None,
                "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "request_order": REQUEST_ORDER, "native_tested": False}

    @staticmethod
    def _delivery(state, request=None, offer=None):
        marker = state.get("package_delivery")
        if marker is None and "package_delivery" not in state and request is None:
            return
        if (not isinstance(marker, dict)
                or set(marker) != {"schema_version", "request", "before", "after", *_IDENTITY_FIELDS}
                or type(marker["schema_version"]) is not int or marker["schema_version"] != 1
                or not isinstance(marker["request"], str) or not _HASH.fullmatch(marker["request"])
                or type(marker["before"]) is not int or marker["before"] != 0
                or type(marker["after"]) is not int or marker["after"] != 1):
            _invalid()
        _valid_identity(marker)
        if request is not None and (marker["request"] != request
                or {key: marker[key] for key in _IDENTITY_FIELDS} != _identity(offer)):
            _invalid()

    def initialize(self, state):
        self._ready()
        self._delivery(state)
        if self.purchased_logins is not None:
            state = self.purchased_logins.initialize(state)
        if not self.catalog.enabled and not any(key in state for key in
                ("package_purchases", "package_history", "package_delivery")):
            return deepcopy(state)
        return self.purchases.initialize(state)

    def _ppf(self, state, *, include_purchased=True):
        entries = self.purchases.read(self.purchases.initialize(state))
        rows = [{"RecordIndex": offer.flag_id, "PackageID": offer.flag_id,
                 "PurchaseFlag": entries.get(offer.normal_pass_id, {}).get("count", 0),
                 "PurchaseStartDateTime": _native_days(offer.start_at),
                 "PurchaseEndDateTime": _native_days(offer.end_at)} for offer in self.catalog.offers]
        result = Container("PPF", tuple(Column(name, kind, tuple(row[name] for row in rows)) for name, kind in (
            ("RecordIndex", "I"), ("PackageID", "I"), ("PurchaseFlag", "I"),
            ("PurchaseStartDateTime", "D"), ("PurchaseEndDateTime", "D"))))
        if include_purchased and self.purchased_logins is not None:
            result = _merge_tables(result, self.purchased_logins.ppf(state), "PackageID")
        return result

    def read_response(self, operation, profile, state, now):
        directory = self.catalog.response(now)
        if self.purchased_logins is not None:
            directory = _merge_tables(directory, self.purchased_logins.catalog.response(now), "PackageID")
        children = (self.bootstrap._system(profile), directory, self._ppf(state),
                    self.possessions.response(state))
        if operation == 47:
            children += (Container("PSR"),)
        payload = dumps(Container(children=children), limits=REPLY_LIMITS)
        encode_response(self._profiles[operation], payload)
        return payload

    def _response(self, profile, state, offer, *, login_package=False, legacy_directory=False, legacy_possessions=False):
        children = [self.bootstrap._system(profile), self._ppf(state, include_purchased=not legacy_directory)]
        if not login_package:
            children.append(self.possessions.response(state, [offer.item_id], legacy=legacy_possessions))
        children.append(Container("PSR"))
        payload = dumps(Container(children=tuple(children)), limits=REPLY_LIMITS)
        encode_response(self._profiles[50], payload)
        return payload

    def _preflight_inventory(self, profile, state):
        candidate = replace(profile, state=state)
        expected = self.possessions.startup_response(state)
        actual = self.bootstrap._possessions(candidate)
        if actual != expected:
            raise ProtocolError("package_provider_inconsistent", "Package inventory differs from its provider", 503)
        encode_response(self._profiles[129], dumps(Container(children=(self.bootstrap._system(candidate), actual))))
        startup = self.bootstrap._configuration_response(candidate)
        groups = [child for child in loads(startup, limits=STARTUP_COLLECTION_LIMITS).children if child.name == "POSSESSION"]
        if groups != [expected]:
            raise ProtocolError("package_provider_inconsistent", "Startup inventory differs from package state", 503)
        encode_response(self._profiles[137], startup)

    def _read(self, operation, envelope, store):
        registered(envelope, {operation}, ())
        self._ready()
        profile = self.bootstrap._authenticate(envelope, store)
        self.players.validate(profile.state)
        self._delivery(profile.state)
        return self.read_response(operation, profile, profile.state, instant(self.bootstrap.clock()))

    def read(self, envelope, store):
        return self._read(46, envelope, store)

    def refresh(self, envelope, store):
        return self._read(47, envelope, store)

    def _receipts(self, state, revision):
        return self._ledger.read(state, revision)

    def _replay(self, entry, profile, sku):
        identity, content = entry["identity"], entry["content"]
        legacy = self._legacy_directory(content, identity)
        if entry["revision"] != profile.revision or not (content == self.content_version or legacy
                                                         or self._replayable(content)):
            raise ProtocolError("package_retry_stale", "Package state or catalog changed after this request", 409)
        if identity["sku"] != sku or entry["state"] != _state_digest(profile.state):
            _invalid()
        login_package = "kind" in identity
        if login_package:
            if self.purchased_logins is None:
                raise ProtocolError("purchased_login_service_unavailable", "Purchase receipt requires its login reward service", 503)
            offer = self.purchased_logins.catalog.get(sku)
            if identity != _login_identity(offer, identity["purchase_id"]):
                _invalid()
            self.purchased_logins.validate_purchase_receipt(profile.state, offer, entry["request"], identity["purchase_id"])
        else:
            offer = self.catalog.get(sku)
            if identity != _identity(offer):
                _invalid()
            self._delivery(profile.state, entry["request"], offer)
            purchase = self.purchases.read(profile.state).get(offer.normal_pass_id)
            if purchase != {"identity": offer.definition_hash, "count": 1, "source": LOCAL_FREE, "request": entry["request"]}:
                _invalid()
            if self.possessions.counts(profile.state).get(offer.item_id) != 1:
                _invalid()
        self._preflight_inventory(profile, profile.state)
        saved = entry["response"].encode("utf-8")
        rebuild = partial(self._response, profile, profile.state, offer, login_package=login_package,
                          legacy_directory=legacy)
        # Receipts saved before every POSSESSION carried Enabled keep their two-column table.
        if rebuild() != saved and rebuild(legacy_possessions=True) != saved:
            _invalid()
        return saved

    def purchase(self, envelope, store):
        values = registered(envelope, {50}, {"SKU"}, {"SKU"})
        sku = scalar(values["SKU"], "S")
        if not sku or len(sku.encode("utf-8")) > STORED_ITEM_BYTES:
            raise ProtocolError("package_sku_invalid", "Package SKU must be a bounded local identifier")
        key = hashlib.sha256(f"50:{envelope.profile.route}:{envelope.profile.st}:".encode() + envelope.xml).hexdigest()
        now = None
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            self.players.validate(profile.state)
            self._delivery(profile.state)
            history = self._receipts(profile.state, profile.revision)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                return self._replay(cached, profile, sku)
            if self._ledger.processed(history, key):
                log.warning("Refused request 50: it repeats a processed request whose receipt was evicted")
                raise ProtocolError("package_request_stale", "Package request was already processed", 409)
            login_package = self.purchased_logins is not None and sku in self.purchased_logins.catalog.by_sku
            offer = self.purchased_logins.catalog.get(sku) if login_package else self.catalog.get(sku)
            if now is None:
                now = instant(self.bootstrap.clock())
            if login_package:
                updated, entitlement = self.purchased_logins.acquire(profile.state, offer, key, now)
                identity = _login_identity(offer, entitlement["purchase_id"])
                self.purchased_logins.validate_purchase_receipt(updated, offer, key, entitlement["purchase_id"])
            else:
                if offer not in self.catalog.available(now):
                    raise ProtocolError("package_unavailable", "Local package is outside its availability window", 409)
                prepared = self.purchases.initialize(profile.state)
                try:
                    updated = self.purchases.mark(prepared, offer, key)
                except ProtocolError as error:
                    if error.code == IDENTITY_UNREPLAYABLE:
                        log.warning("Refused request 50 for package %s: %s", sku, error)
                    raise
                updated = self.possessions.credit(updated, {offer.item_id: 1})
                identity = _identity(offer)
                updated["package_delivery"] = {"schema_version": 1, "request": key, "before": 0, "after": 1, **identity}
            self._preflight_inventory(profile, updated)
            self.read_response(46, profile, updated, now)
            self.read_response(47, profile, updated, now)
            response = self._response(profile, updated, offer, login_package=login_package)
            self._ledger.record(updated, history, {"request": key, "content": self.content_version,
                                                   "state": _state_digest(updated), "revision": profile.revision + 1,
                                                   "response": response.decode("utf-8"), "identity": identity})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving the package entitlement", 409)
