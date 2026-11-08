from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import replace
from functools import partial
import hashlib
import json
import logging
import re

from .alxml import Container, dumps
from . import crystal_spending as spending
from .crystal_wallet import free_crystal_response, paid_crystal_response, wallet_totals
from .energy_clock import instant
# Native 7C32B0 runs ExecutePurchaseComplete only after A7FDF0 returns on a 200; 66E8B0 resends refused bodies.
from .errors import REFUSAL_AFTERMATH as _REFUSAL_AFTERMATH, REFUSAL_POLICY, ProtocolError, caught
from .mission_state import JOURNAL_KEY as MISSION_JOURNAL, SKIN_HISTORY_KEY as SKIN_HISTORY
from .native_numbers import int32
from .protocol import ProtocolRegistry, encode_response
from .request_fields import fields, invalid, registered, scalar
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .trade_history import TradeHistory
from .universal_flags import (MAX_FLAG_NAME_BYTES, flag_hash, flag_value, flags_response, read_flags,
                              resolve_flag_name, set_flags)


RESPONSE_LIMIT = 256 * 1024
HISTORY_NAMESPACE = "skin_shop_history"
MISSION_FIELDS = frozenset((MISSION_JOURNAL, SKIN_HISTORY))
_HASH = re.compile(r"[a-f0-9]{64}\Z")
log = logging.getLogger("aigis_ps")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


def _receipt_product_valid(entry):
    flag_hash(entry["flag_name"])
    return int32(entry["shop_id"]) and entry["shop_id"] != 0


class SkinShopService:
    def __init__(self, bootstrap, players, possessions, catalog, missions):
        self.bootstrap, self.players, self.possessions = bootstrap, players, possessions
        self.catalog, self.missions = catalog, missions
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (35, 43, 44, 129, 137, 156)}
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "skin_shop_history_invalid", "Stored skin-shop receipts are invalid",
                                     fields=("request", "content", "state", "revision", "response", "shop_id", "flag_name"),
                                     response_limit=RESPONSE_LIMIT, check=_receipt_product_valid)
        self.content_version = self._content()
        self._ready()

    def _content(self, catalog=None, missions=None):
        try:
            versions = {"catalog": self.catalog.content_version if catalog is None else catalog,
                        "missions": self.missions.content_version if missions is None else missions,
                        "possessions": self.possessions.asset_sha256,
                        "skins": self.missions.skins.content_version}
            if any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in versions.values()):
                raise ValueError("Skin-shop dependencies require verified content versions")
            return _digest({"policy": 1, "dependencies": versions, "payment": "free_first_then_paid_oldest_date",
                            "crystal_spending_policy": spending.POLICY_VERSION,
                            "grant": "one_possession", "purchase_value": 1,
                            "flag_names": "preserve_existing_name_else_catalog_verified_pattern",
                            "result": "empty_SkinShopPurchase", "retry": "same_revision_state_content"})
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError("skin_shop_dependencies_invalid", "Skin-shop dependencies are unavailable", 503) from error

    def _replayable(self, content):
        # A receipt digest keeps the catalog window rule and the optional mission dependencies of its commit.
        return content == self.content_version or any(
            self._content(catalog, missions) == content
            for catalog in self.catalog.replayable_content_versions()
            for missions in self.missions.replayable_content_versions())

    def _missions_ready(self):
        return caught(self.missions._ready) is None

    def _ready(self):
        try:
            skins = self.missions.skins
            counted = skins.counted_item_ids
            supported = {product.item_id for product in self.catalog.products if product.supported}
            if (not self.catalog.is_ready() or not self.possessions.is_ready()
                    or not self._missions_ready() or not skins.is_ready()
                    or not isinstance(counted, frozenset) or not supported <= counted
                    or self.missions.progress.skin_items != counted
                    or self.catalog.resource_sha256["SkinPackage.atb"] != skins.resource_sha256["SkinPackage.atb"]
                    or self._content() != self.content_version):
                raise ValueError("Skin-shop and acquisition definitions differ")
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ProtocolError("skin_shop_dependencies_changed", "Skin-shop dependencies changed after setup", 503) from error

    def handlers(self):
        return {156: self.purchase}

    def status(self):
        try:
            self._ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        return {"ready": ready, "error": error, "implemented_operations": [156],
                "content_version": self.content_version, "catalog": self.catalog.status(),
                "payment_policy": "Free crystals first, then paid; oldest acquisition day first within each kind",
                "duplicate_policy": "Reject existing positive item ownership or any nonzero purchase flag",
                "flag_policy": ("Preserve an existing matching name; otherwise use the catalog's verified readable pattern "
                                f"within the {MAX_FLAG_NAME_BYTES}-byte native cell"),
                "purchase_value": 1, "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "request_order": REQUEST_ORDER,
                "refusals": REFUSAL_POLICY,
                "native_tested": False}

    def _product(self, shop_id):
        product = self.catalog.get(shop_id)
        try:
            if (not int32(product.shop_id) or product.shop_id != shop_id or shop_id == 0
                    or not int32(product.purchase_flag) or not int32(product.price) or product.price < 0
                    or type(product.supported) is not bool
                    or flag_hash(product.flag_name) != product.purchase_flag):
                raise ValueError("Skin-shop product does not match its verified identity")
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError("skin_shop_product_invalid", "Skin-shop product identity is invalid", 503) from error
        if not product.supported:
            raise ProtocolError("skin_shop_product_unsupported", "Skin-shop product requires unavailable item definitions", 501)
        if not isinstance(product.item_id, str) or self.possessions.stack(product.item_id) <= 0:
            raise ProtocolError("skin_shop_product_invalid", "Skin-shop item is not creditable", 503)
        return product

    @staticmethod
    def _flag_name(state, product):
        name = resolve_flag_name(state, product.purchase_flag, default_name=product.flag_name)
        # Only names already saved under the earlier bound may exceed the native cell; new ones must fit it.
        if len(name.encode("utf-8")) > MAX_FLAG_NAME_BYTES and name not in {row["ID"] for row in read_flags(state)}:
            raise ProtocolError("skin_shop_product_unsupported", "Skin-shop purchase flag name does not fit the native cell", 501)
        return name

    def _preflight(self, profile, state, now):
        candidate = replace(profile, state=state)
        self.players.validate(state)
        for number, free in ((43, False), (44, True)):
            rows = [row for row in self.bootstrap._rows(candidate, "crystals", single=False) if row["FreeFlag"] is free]
            encode_response(self._profiles[number], dumps(Container(children=self.bootstrap.columns("crystals", rows))))
        inventory = dumps(Container(children=(self.bootstrap._system(candidate), self.bootstrap._possessions(candidate))))
        encode_response(self._profiles[129], inventory)
        bootstrap = copy(self.bootstrap)
        owner = getattr(bootstrap.exchange_history_provider, "__self__", None)
        if isinstance(getattr(owner, "history", None), TradeHistory):
            bootstrap.exchange_history_provider = lambda saved: owner.history.response(saved, now)
        encode_response(self._profiles[137], bootstrap._configuration_response(candidate))
        self.missions.preflight_snapshot(state, now, candidate)
        self._preflight_spending(profile, state)

    def _preflight_spending(self, profile, state):
        spending.read_spending(state)
        candidate = replace(profile, state=state)
        rows = self.bootstrap._rows(candidate, "game_param2")
        payload = dumps(Container(children=self.bootstrap.columns("game_param2", rows)))
        encode_response(self._profiles[35], payload)

    def _response(self, profile, state, product, flag_name, *, legacy_possessions=False):
        response = dumps(Container(children=(self.bootstrap._system(profile),
            free_crystal_response(state, profile.player_id), paid_crystal_response(state, profile.player_id),
            self.possessions.response(state, (product.item_id,), legacy=legacy_possessions),
            flags_response(state, (flag_name,)),
            Container("SkinShopPurchase"))))
        if len(response) > RESPONSE_LIMIT:
            raise ProtocolError("skin_shop_response_capacity", "Skin-shop response exceeds its receipt limit", 503)
        encode_response(self._profiles[156], response)
        return response

    def purchase(self, envelope, store):
        try:
            return self._purchase(envelope, store)
        except ProtocolError as error:
            log.warning("Skin shop %d refused with %s; the native client %s",
                        envelope.profile.registration_keys[0], error.code, _REFUSAL_AFTERMATH)
            raise

    def _purchase(self, envelope, store):
        if envelope.profile.registration_keys != (156,):
            invalid("Expected the native skin-shop purchase operation")
        root = registered(envelope, {156}, {"SKIN"}, {"SKIN"})
        skin = fields(root["SKIN"], {"ShopId"}, {"ShopId"})
        shop_id = scalar(skin["ShopId"], "I")
        key = hashlib.sha256(f"156:{envelope.profile.route}:{envelope.profile.st}:".encode() + envelope.xml).hexdigest()
        now = None
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            self.players.validate(profile.state)
            wallet_totals(profile.state)
            counts = self.possessions.counts(profile.state)
            product = self._product(shop_id)
            flag_name = self._flag_name(profile.state, product)
            purchased = flag_value(profile.state, flag_name)
            history = self._ledger.read(profile.state, profile.revision)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                if cached["revision"] != profile.revision or not self._replayable(cached["content"]):
                    raise ProtocolError("skin_shop_retry_stale", "Skin-shop state or content changed after this request", 409)
                if (cached["state"] != _state_digest(profile.state)
                        or cached["shop_id"] != shop_id or cached["flag_name"] != flag_name
                        or purchased != 1 or counts.get(product.item_id, 0) != 1):
                    self._ledger.invalid()
                self._preflight_spending(profile, profile.state)
                saved = cached["response"].encode("utf-8")
                rebuild = partial(self._response, profile, profile.state, product, flag_name)
                # Receipts saved before every POSSESSION carried Enabled keep their two-column table.
                if rebuild() != saved and rebuild(legacy_possessions=True) != saved:
                    self._ledger.invalid()
                return saved
            if self._ledger.processed(history, key):
                raise ProtocolError("skin_shop_request_stale", "Skin-shop request was already processed", 409)
            if now is None:
                now = instant(self.bootstrap.clock())
            if not self.catalog.is_open(product, now):
                raise ProtocolError("skin_shop_closed", "Skin-shop product is outside its purchase window", 409)
            if purchased != 0 or counts.get(product.item_id, 0) > 0:
                raise ProtocolError("skin_shop_already_owned", "Skin-shop product is already purchased or owned", 409)
            updated = spending.spend_crystals(profile.state, product.price, now)
            updated = self.possessions.credit(updated, {product.item_id: 1})
            updated = set_flags(updated, {flag_name: 1})
            expected = deepcopy(updated)
            updated = self.missions.record_acquisitions(profile.state, updated, now)
            if (not isinstance(updated, dict)
                    or {name: value for name, value in updated.items() if name not in MISSION_FIELDS}
                    != {name: value for name, value in expected.items() if name not in MISSION_FIELDS}):
                raise ProtocolError("skin_shop_hook_invalid", "Skin acquisition changed unrelated purchase state", 503)
            self._preflight(profile, updated, now)
            response = self._response(profile, updated, product, flag_name)
            self._ledger.record(updated, history, {"request": key, "content": self.content_version,
                                                   "state": _state_digest(updated), "revision": profile.revision + 1,
                                                   "response": response.decode("utf-8"), "shop_id": shop_id,
                                                   "flag_name": flag_name})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while purchasing a skin", 409)
