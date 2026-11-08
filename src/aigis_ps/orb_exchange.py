from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import logging
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Container, dumps, loads
from .errors import REFUSAL_AFTERMATH as _REFUSAL_AFTERMATH, REFUSAL_POLICY, ProtocolError
from .game_param_groups import money_columns
from .native_numbers import int32
from .possessions import MAX_ITEM_NAME_BYTES, STORED_ITEM_NAME_BYTES
from .protocol import ProtocolRegistry, encode_response
from .request_fields import invalid, registered, scalar
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .system_literals import command_id
from .wallet_money import NATIVE_BALANCE_MESSAGE, MoneyRefusal, MoneyRefusals, money_after_debit


HISTORY_NAMESPACE = "orb_exchange_history"
RESPONSE_LIMIT = 16384
RESOURCE_LIMITS = ParseLimits(max_input_bytes=8 * 1024 * 1024, max_decompressed_bytes=8 * 1024 * 1024)
ORB_CATEGORY = -2007482751
EXCHANGE_MONEY_REFUSALS = MoneyRefusals(
    insufficient=MoneyRefusal("orb_exchange_money_insufficient", "Exchange costs exceed the money balance"),
    native_balance=MoneyRefusal("orb_exchange_native_balance", NATIVE_BALANCE_MESSAGE))
log = logging.getLogger("aigis_ps")


def _name(value):
    return isinstance(value, str) and 0 < len(value.encode("utf-8")) <= STORED_ITEM_NAME_BYTES and "\0" not in value


@dataclass(frozen=True)
class OrbDefinition:
    item_id: str
    price: int
    amount_required: int
    amount_created: int
    stack: int


@dataclass(frozen=True)
class OrbExchangeQuote:
    item_id: str
    base_id: str
    price: int
    amount_required: int
    amount_created: int
    money_after: int
    item_after: int
    base_after: int


class OrbExchangeService:
    def __init__(self, bootstrap, players, possessions, asset_reader):
        self.bootstrap, self.players, self.possessions = bootstrap, players, possessions
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "orb_exchange_history_invalid", "Orb exchange receipt history is invalid",
                                     fields=("request", "content", "revision", "response"), response_limit=RESPONSE_LIMIT)
        hashes = {}

        def table(name, required):
            raw = asset_reader(name)
            if not isinstance(raw, bytes) or len(raw) > RESOURCE_LIMITS.max_input_bytes:
                raise ValueError("Exchange resource must be bounded bytes")
            payload = decompress_all4(raw, RESOURCE_LIMITS) if raw.startswith(b"ALL4") else raw
            parsed = parse_table(payload, RESOURCE_LIMITS)
            fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
            if len(fields) != len(parsed["fields"]) or any(fields.get(key) != kind for key, kind in required.items()):
                raise ValueError("Exchange resource fields differ from the native contract")
            hashes[name] = hashlib.sha256(raw).hexdigest()
            return parsed["rows"], hashlib.sha256(payload).hexdigest()

        try:
            if not callable(asset_reader) or not possessions.is_ready():
                raise ValueError("Exchange requires verified resources and possession definitions")
            items, item_hash = table("ItemConfigTable.atb", {"ItemID": 32, "ID": 1, "Category": 1, "Stack": 1})
            if possessions.asset_sha256 != item_hash:
                raise ValueError("Exchange and possession item definitions differ")
            item_rows, identifiers = {}, set()
            for row in items:
                name, identifier = row["ItemID"], row["ID"]
                if (not _name(name) or not int32(identifier) or identifier != command_id(name)
                        or name in item_rows or identifier in identifiers
                        or not int32(row["Category"]) or not int32(row["Stack"]) or row["Stack"] < 0):
                    raise ValueError("Item identities, categories or stack limits are invalid")
                item_rows[name] = row
                identifiers.add(identifier)
            rows, _ = table("ItemOrbExchangeConfig.atb", {"ItemID": 1, "ItemName": 32, "Category": 1,
                            "Price": 1, "AmountOfRequire": 1, "AmountOfCreate": 1})
            definitions = {}
            for row in rows:
                name = row["ItemName"]
                if (not _name(name) or not int32(row["ItemID"]) or row["ItemID"] != command_id(name)
                        or name in definitions or not int32(row["Category"])
                        or any(not int32(row[key]) or row[key] <= 0 for key in
                               ("Price", "AmountOfRequire", "AmountOfCreate"))):
                    raise ValueError("Orb exchange definitions are invalid or duplicated")
                item = item_rows.get(name)
                if (item is None or item["Category"] != ORB_CATEGORY or item["Stack"] <= 0
                        or possessions.resolve_hash(row["ItemID"]) != name):
                    raise ValueError("Orb exchange does not resolve to an eligible item definition")
                definitions[name] = OrbDefinition(name, row["Price"], row["AmountOfRequire"],
                                                   row["AmountOfCreate"], item["Stack"])
            if not definitions:
                raise ValueError("Orb exchange definitions are empty")
            self._definitions = MappingProxyType(definitions)
            # Credit may open a new target row, and new rows must fit the native cell.
            self._unsupported_items = frozenset(name for name in definitions
                                                if len(name.encode("utf-8")) > MAX_ITEM_NAME_BYTES)
            self._item_hash = item_hash
            self._hashes = MappingProxyType(hashes)
            content = {"policy": 2, "resources": hashes, "possession_definitions": item_hash}
            self.content_version = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
            self._configuration_profile = ProtocolRegistry.load().by_registration[137]
        except (AttributeError, KeyError, TypeError, ValueError, OSError, ProtocolError) as error:
            raise ProtocolError("orb_exchange_catalog_invalid", "Orb exchange resources are unavailable or invalid", 503) from error

    @property
    def definitions(self):
        return tuple(self._definitions.values())

    def handlers(self):
        return {139: self.exchange}

    def status(self):
        return {"ready": self.possessions.asset_sha256 == self._item_hash and self.possessions.is_ready(),
                "implemented_operations": [139], "definitions": len(self._definitions),
                "content_version": self.content_version, "resource_sha256": dict(self._hashes),
                "unsupported_item_names": sorted(self._unsupported_items),
                "name_policy": (f"Exchange targets need names within the {MAX_ITEM_NAME_BYTES}-byte native cell; "
                                f"stored source names up to {STORED_ITEM_NAME_BYTES} bytes stay exchangeable"),
                "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "request_order": REQUEST_ORDER,
                "refusals": REFUSAL_POLICY,
                "rule": "One distinct-orb exchange; target price/create amount and source required amount",
                "native_tested": False}

    def _check_catalog(self):
        if self.possessions.asset_sha256 != self._item_hash or not self.possessions.is_ready():
            raise ProtocolError("orb_exchange_catalog_changed", "Possession definitions changed after exchange setup", 503)

    def quote(self, state, item_id, base_id) -> OrbExchangeQuote:
        self._check_catalog()
        if (not isinstance(item_id, str) or not isinstance(base_id, str)
                or item_id not in self._definitions or base_id not in self._definitions):
            raise ProtocolError("orb_exchange_item_unknown", "Exchange items must be verified orb definitions")
        if item_id == base_id:
            raise ProtocolError("orb_exchange_same_item", "Exchange requires different source and target orbs")
        if item_id in self._unsupported_items:
            raise ProtocolError("orb_exchange_item_unsupported", "Exchange target name exceeds the native cell", 501)
        target, source = self._definitions[item_id], self._definitions[base_id]
        self.bootstrap.player_row(state)
        response = self.possessions.response(state, (base_id, item_id))
        values = {column.name: column.values for column in response.children}
        counts = dict(zip(values["ItemID"], values["Count"]))
        money_after = money_after_debit(state, target.price, refusals=EXCHANGE_MONEY_REFUSALS)
        if counts[base_id] < source.amount_required:
            raise ProtocolError("orb_exchange_material_insufficient", "Exchange requires more source orbs", 409)
        result_count = counts[item_id] + target.amount_created
        if not int32(result_count) or result_count > target.stack:
            raise ProtocolError("orb_exchange_stack_exceeded", "Exchange would exceed the target orb stack", 409)
        return OrbExchangeQuote(item_id, base_id, target.price, source.amount_required, target.amount_created,
                                money_after, result_count, counts[base_id] - source.amount_required)

    def exchange(self, envelope, store):
        try:
            return self._exchange(envelope, store)
        except ProtocolError as error:
            log.warning("Orb exchange 139 refused with %s; the native client %s", error.code, _REFUSAL_AFTERMATH)
            raise

    def _exchange(self, envelope, store):
        if envelope.profile.registration_keys != (139,):
            invalid("Expected the native orb exchange operation")
        fields = registered(envelope, (139,), ("ITEM", "BASE"), ("ITEM", "BASE"))
        item_id = scalar(fields["ITEM"], "S", max_bytes=STORED_ITEM_NAME_BYTES)
        base_id = scalar(fields["BASE"], "S", max_bytes=STORED_ITEM_NAME_BYTES)
        key = hashlib.sha256(envelope.profile.name.encode() + b"\0" + envelope.xml).hexdigest()
        for _ in range(3):
            self._check_catalog()
            profile = self.players.authenticate(envelope, store)
            self.players.validate(profile.state)
            history = self._ledger.read(profile.state, profile.revision)
            receipt = self._ledger.receipt(history, key)
            if receipt is not None:
                if receipt["revision"] != profile.revision or receipt["content"] != self.content_version:
                    raise ProtocolError("orb_exchange_retry_stale", "Exchange state or content changed after this request", 409)
                response = receipt["response"].encode("utf-8")
                try:
                    loads(response)
                    encode_response(envelope.profile, response)
                except (ValueError, ProtocolError) as error:
                    raise ProtocolError("orb_exchange_history_invalid", "Saved exchange response is invalid", 503) from error
                return response
            if self._ledger.processed(history, key):
                raise ProtocolError("orb_exchange_request_stale", "Exchange request was already processed", 409)
            quote = self.quote(profile.state, item_id, base_id)
            updated = self.possessions.debit(profile.state, {base_id: quote.amount_required})
            updated = self.possessions.credit(updated, {item_id: quote.amount_created})
            updated["tables"]["game_param"][0]["Money"] = quote.money_after
            candidate = replace(profile, state=updated)
            self.bootstrap._player_row(candidate)
            encode_response(self._configuration_profile, self.bootstrap._configuration_response(candidate))
            response = dumps(Container(children=(self.bootstrap._system(profile), money_columns(quote.money_after),
                self.possessions.response(updated, (base_id, item_id)))))
            encode_response(envelope.profile, response)
            if len(response) > RESPONSE_LIMIT:
                raise ProtocolError("orb_exchange_response_capacity", "Exchange response exceeds its receipt limit", 503)
            self._ledger.record(updated, history, {"request": key, "content": self.content_version,
                                                   "revision": profile.revision + 1, "response": response.decode("utf-8")})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving orb exchange", 409)
