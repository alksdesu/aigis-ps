from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import logging
import re
from types import MappingProxyType

from .al_format import MAX_RESOURCE_BYTES, ParseLimits, decompress_all4, parse_table
from .alxml import Container, dumps, loads
from .client_versions import CURRENT_CLIENT_SHA256, require_client_version
from .config_state import configuration_delta, configuration_row, patch_configuration
from .crystal_spending import POLICY_VERSION, PROJECTION_CAP, spend_crystals, spending_response
from .crystal_wallet import free_crystal_response, paid_crystal_response, wallet_totals
from .errors import REFUSAL_AFTERMATH, REFUSAL_POLICY, ProtocolError
from .native_numbers import UINT32_MASK, int32
from .player_state import BARRACKS_BITS, BARRACKS_SHIFT, PRINCE_CARD_IDS, barracks_index, primary_unit_updates
from .protocol import ProtocolRegistry, encode_response
from .request_fields import fields, invalid, registered, scalar, values
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .system_literals import decode_command, rating_command_id


CRYSTAL_PRICE = 30
MAXIMUM_EXTRA_BARRACKS = 2
FIRST_PURCHASE_RANK = 25
MAXIMUM_TRANSFER = 10
RESPONSE_LIMIT = 131072
HISTORY_NAMESPACE = "barracks_history"
PURCHASE, TRANSFER = 17, 118
PURCHASE_FIELD = "HaveUnitMaxSizeBuy"
EXTRA_SHIFT, RETAINED_BITS = 16, 0xFFFF
PRINCE_RULE_COMMAND = 0x7574897F
RESOURCE_LIMITS = ParseLimits(max_input_bytes=MAX_RESOURCE_BYTES, max_decompressed_bytes=MAX_RESOURCE_BYTES)
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_COMMAND_HASHES = {
    "OnCommandMenuUnitHouse.atb": "0c942d7f3eb99305ac8a8481d46de241accd2554b9fe26f09515196c775e9095",
    "OnCommandMenuShop.atb": "8dfc459a133a0efebae0199c320cf13f418293d0078c013434fec18be7349ad1",
}
_DIRECTIONS = frozenset(((0, 1), (0, 2), (1, 0), (2, 0)))
# Native A7FDF0 returns only after a 200 and 66E8B0 resends refused bodies; 897630 moves the rows before sending.
_REFUSALS = {PURCHASE: ("purchase", REFUSAL_AFTERMATH),
             TRANSFER: ("transfer", f"already moved the rows locally, {REFUSAL_AFTERMATH}")}
log = logging.getLogger("aigis_ps")


def _receipt_operation_valid(entry):
    return type(entry["operation"]) is int and entry["operation"] in (PURCHASE, TRANSFER)


def _open_slot(state):
    value = configuration_row(state).get(PURCHASE_FIELD)
    if not int32(value):
        raise ProtocolError("barracks_state_invalid", "Barracks purchase flags require an int32 value", 503)
    extra = (value & UINT32_MASK) >> EXTRA_SHIFT
    if extra >= MAXIMUM_EXTRA_BARRACKS:
        raise ProtocolError("barracks_purchase_limit", "All verified extra barracks are already available", 409)
    return value, extra


def _unlocked(value, extra):
    return {PURCHASE_FIELD: (value & RETAINED_BITS) | ((extra + 1) << EXTRA_SHIFT)}


def _destination(value):
    if not int32(value) or not 0 <= value <= MAXIMUM_EXTRA_BARRACKS:
        invalid("Transfer destination is outside the verified barracks range")
    return value


def _identities(indices, uniques):
    if (not 1 <= len(indices) <= MAXIMUM_TRANSFER or len(indices) != len(uniques)
            or len(set(indices)) != len(indices) or len(set(uniques)) != len(uniques)
            or any(not int32(index) or index < 0 for index in indices)
            or any(not int32(unique) or unique <= 0 for unique in uniques)):
        invalid("Transfer requires aligned distinct unit identities")
    return tuple(zip(indices, uniques))


class BarracksService:
    def __init__(self, bootstrap, players, capacity, asset_reader, *, client_sha256=CURRENT_CLIENT_SHA256):
        self.bootstrap, self.players, self.capacity = bootstrap, players, capacity
        hashes = {}
        try:
            self.client_sha256 = require_client_version(client_sha256)
            if not callable(asset_reader) or not _HASH.fullmatch(capacity.content_version):
                raise ValueError("Verified barracks capacity and resources are required")
            for name, expected in _COMMAND_HASHES.items():
                raw = asset_reader(name)
                if not isinstance(raw, bytes) or len(raw) > RESOURCE_LIMITS.max_input_bytes:
                    raise ValueError("Barracks command resource must be bounded bytes")
                hashes[name] = hashlib.sha256(raw).hexdigest()
                if hashes[name] != expected:
                    raise ValueError("Barracks menu differs from the verified native command contract")
            raw = asset_reader("OnCommand.atb")
            if not isinstance(raw, bytes) or len(raw) > RESOURCE_LIMITS.max_input_bytes:
                raise ValueError("Rating command resource must be bounded bytes")
            hashes["OnCommand.atb"] = hashlib.sha256(raw).hexdigest()
            payload = decompress_all4(raw, RESOURCE_LIMITS) if raw.startswith(b"ALL4") else raw
            table = parse_table(payload, RESOURCE_LIMITS)
            columns = {field["name"]: field["type_code"] for field in table["fields"]}
            if len(columns) != len(table["fields"]) or columns != {"_ID": 1, "_Cmnd": 32}:
                raise ValueError("Rating command schema differs from the verified contract")
            commands = {}
            for row in table["rows"]:
                if not int32(row["_ID"]) or not isinstance(row["_Cmnd"], str):
                    raise ValueError("Rating commands require typed identifiers and text")
                commands.setdefault(row["_ID"], []).append(decode_command(row["_Cmnd"]))
            self._commands = MappingProxyType({key: tuple(value) for key, value in commands.items()})
            self._capacity_version = capacity.content_version
            self._hashes = MappingProxyType(hashes)
            content = {"native_client": self.client_sha256, "policy": 2, "resources": hashes,
                       "capacity": self._capacity_version, "price": CRYSTAL_PRICE,
                       "first_rank": FIRST_PURCHASE_RANK, "maximum_extra": MAXIMUM_EXTRA_BARRACKS,
                       "directions": sorted(_DIRECTIONS), "maximum_transfer": MAXIMUM_TRANSFER}
            self.content_version = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
            self.purchase_content_version = hashlib.sha256(json.dumps({"barracks": self.content_version,
                "crystal_spending": POLICY_VERSION, "projection_cap": PROJECTION_CAP}, sort_keys=True).encode()).hexdigest()
            registry = ProtocolRegistry.load()
            self._unit_profile = registry.by_registration[41]
            self._config_profile = registry.by_registration[36]
            self._purchase_profiles = {number: registry.by_registration[number] for number in (35, 43, 44)}
        except (AttributeError, KeyError, TypeError, ValueError, OSError) as error:
            raise ProtocolError("barracks_catalog_invalid", "Barracks resources do not satisfy the verified contract", 503) from error
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "barracks_history_invalid", "Barracks receipt history is invalid",
                                     fields=("request", "content", "revision", "response", "operation"),
                                     response_limit=RESPONSE_LIMIT, check=_receipt_operation_valid)

    def handlers(self):
        return {PURCHASE: self.purchase, TRANSFER: self.transfer}

    def status(self):
        return {"ready": self.capacity.content_version == self._capacity_version,
                "implemented_operations": [PURCHASE, TRANSFER], "content_version": self.content_version,
                "purchase_content_version": self.purchase_content_version,
                "resource_sha256": dict(self._hashes), "native_client_sha256": self.client_sha256,
                "crystal_price": CRYSTAL_PRICE, "first_purchase_rank": FIRST_PURCHASE_RANK,
                "maximum_extra_barracks": MAXIMUM_EXTRA_BARRACKS, "maximum_transfer": MAXIMUM_TRANSFER,
                "directions": [list(pair) for pair in sorted(_DIRECTIONS)], "native_tested": False,
                "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "request_order": REQUEST_ORDER,
                "refusals": f"{REFUSAL_POLICY}; 118 has already moved its rows locally",
                "noop_policy": "Preserve unit values and commit only a request receipt once",
                "purchase_policy": "Free crystals before paid, oldest acquisition dates first",
                "monthly_spending": {"policy_version": POLICY_VERSION, "projection_cap": PROJECTION_CAP}}

    def _ready(self):
        if self.capacity.content_version != self._capacity_version:
            raise ProtocolError("barracks_catalog_changed", "Barracks capacity changed after service setup", 503)

    def _purchase(self, profile, now):
        value, extra = _open_slot(profile.state)
        game = self.bootstrap._player_row(profile)
        if extra == 0 and game["Rank"] < FIRST_PURCHASE_RANK:
            raise ProtocolError("barracks_rank_required", "The first extra barracks requires player rank 25", 409)
        updated = spend_crystals(profile.state, CRYSTAL_PRICE, now, free_first=True)
        updated = patch_configuration(updated, _unlocked(value, extra))
        return updated, self._purchase_response(profile, updated)

    def grant_slot(self, state):
        # The rank and crystal price belong to the shop sale; the capacity rule only reads the purchase count.
        self._ready()
        value, extra = _open_slot(state)
        return patch_configuration(state, _unlocked(value, extra))

    @staticmethod
    def _purchase_response(profile, state):
        return (paid_crystal_response(state, profile.player_id), free_crystal_response(state, profile.player_id),
                configuration_delta(configuration_row(state), (PURCHASE_FIELD,)),
                spending_response(state, style="key_values"))

    def _purchase_preflight(self, profile, state):
        candidate = replace(profile, state=state)
        rows = self.bootstrap._rows(candidate, "game_param2")
        encode_response(self._purchase_profiles[35], dumps(Container(children=self.bootstrap.columns("game_param2", rows))))
        wallet_totals(state)
        wallet = state["tables"]["crystals"]
        for number, free in ((43, False), (44, True)):
            rows = [row for row in wallet if row["FreeFlag"] is free]
            encode_response(self._purchase_profiles[number], dumps(Container(children=self.bootstrap.columns("crystals", rows))))

    def transfer_units(self, state, destination, references):
        # Like 118, the selection is parsed before the readiness and save gates.
        destination = _destination(destination)
        references = _identities(tuple(index for index, _ in references), tuple(unique for _, unique in references))
        self._ready()
        player = self.players.validate(state)
        self.bootstrap.player_row(state)
        usage = self.capacity.usage(state)
        if destination >= len(usage.capacities):
            raise ProtocolError("barracks_locked", "The destination barracks has not been purchased", 409)
        by_index = {unit["GetIndex"]: unit for unit in player["units"]}
        selected = []
        for index, unique in references:
            unit = by_index.get(index)
            if unit is None or unit["UniqueID"] != unique:
                raise ProtocolError("barracks_unit_missing", "Transfer identity does not match an owned unit", 409)
            commands = self._commands.get(rating_command_id(unit["CardID"]), ())
            if len(commands) > 1 or any(command.strip() for command in commands):
                raise ProtocolError("barracks_rating_unsupported", "This card requires an unsupported rating condition", 501)
            selected.append(unit)
        sources = {barracks_index(unit) for unit in selected}
        if len(sources) != 1:
            raise ProtocolError("barracks_mixed_sources", "Transfer selection must belong to one source barracks", 409)
        source = sources.pop()
        if source != destination:
            if (source, destination) not in _DIRECTIONS:
                raise ProtocolError("barracks_direction_unsupported", "This direct barracks transfer is not supported", 501)
            if source == 0:
                if any(command.strip() for command in self._commands.get(PRINCE_RULE_COMMAND, ())):
                    raise ProtocolError("barracks_prince_rule_unsupported", "The Prince classification requires an unsupported command", 501)
                organized = {row["UnitIndex"] for row in player["squads"] if row["UnitIndex"] >= 0}
                if any(unit["CardID"] in PRINCE_CARD_IDS for unit in selected):
                    raise ProtocolError("barracks_prince_protected", "The Prince cannot leave the primary barracks", 409)
                if any(unit["GetIndex"] in organized for unit in selected):
                    raise ProtocolError("barracks_organized", "Organized units cannot leave the primary barracks", 409)
            if usage.counts[destination] + len(selected) > usage.capacities[destination]:
                raise ProtocolError("barracks_capacity_exceeded", "The destination cannot hold the complete transfer", 409)
        updated = deepcopy(state)
        indices = {unit["GetIndex"] for unit in selected}
        results = {}
        for unit in updated["player_state"]["units"]:
            if unit["GetIndex"] in indices:
                unit["GeneralPurposeFlags"] = ((unit["GeneralPurposeFlags"] & ~BARRACKS_BITS)
                                               | (destination << BARRACKS_SHIFT))
                results[unit["GetIndex"]] = unit
        return updated, tuple(results[index] for index, _ in references)

    def purchase(self, envelope, store):
        registered(envelope, (PURCHASE,), ())
        return self._transact(envelope, store, PURCHASE)

    def transfer(self, envelope, store):
        columns = registered(envelope, (TRANSFER,), ("DEST", "BASE"), ("DEST", "BASE"))
        destination = _destination(scalar(columns["DEST"], "I"))
        base = fields(columns["BASE"], ("A7", "AB"), ("A7", "AB"))
        indices = values(base["A7"], "I", minimum=1, maximum=MAXIMUM_TRANSFER)
        uniques = values(base["AB"], "I", minimum=1, maximum=MAXIMUM_TRANSFER)
        return self._transact(envelope, store, TRANSFER, destination, _identities(indices, uniques))

    def _transact(self, envelope, store, operation, *selection):
        try:
            return self._execute(envelope, store, operation, *selection)
        except ProtocolError as error:
            label, aftermath = _REFUSALS[operation]
            log.warning("Barracks %s %d refused with %s; the native client %s", label, operation, error.code, aftermath)
            raise

    def _execute(self, envelope, store, operation, destination=None, references=()):
        identity = f"{operation}:{envelope.profile.route}:{envelope.profile.st}".encode()
        key = hashlib.sha256(identity + b"\0" + envelope.xml).hexdigest()
        content = self.purchase_content_version if operation == PURCHASE else self.content_version
        now = None
        for _ in range(3):
            self._ready()
            profile = self.players.authenticate(envelope, store)
            player = self.players.validate(profile.state)
            self.bootstrap._player_row(profile)
            self.capacity.usage(profile.state)
            history = self._ledger.read(profile.state, profile.revision)
            receipt = self._ledger.receipt(history, key)
            if receipt is not None:
                if (receipt["revision"] != profile.revision or receipt["content"] != content
                        or receipt["operation"] != operation):
                    raise ProtocolError("barracks_retry_stale", "Barracks state or content changed after this request", 409)
                response = receipt["response"].encode("utf-8")
                try:
                    document = loads(response)
                    if operation == PURCHASE:
                        expected = Container(children=(self.bootstrap._system(profile), *self._purchase_response(profile, profile.state)))
                        if document != expected:
                            raise ValueError("Barracks purchase receipt disagrees with current state")
                        self._purchase_preflight(profile, profile.state)
                    encode_response(envelope.profile, response)
                except (ValueError, ProtocolError) as error:
                    raise ProtocolError("barracks_history_invalid", "Saved barracks response is invalid", 503) from error
                return response
            if self._ledger.processed(history, key):
                raise ProtocolError("barracks_request_stale", "Barracks request was already processed", 409)
            if operation == PURCHASE:
                if now is None:
                    now = self.bootstrap.clock()
                updated, nodes = self._purchase(profile, now)
                self._purchase_preflight(profile, updated)
            else:
                updated, moved = self.transfer_units(profile.state, destination, references)
                nodes = (primary_unit_updates(moved),)
            candidate = replace(profile, state=updated)
            self.players.validate(updated)
            self.capacity.usage(updated)
            encode_response(self._unit_profile, self.players.unit_response(updated["player_state"]["units"]))
            if operation == TRANSFER and updated["player_state"]["units"] != player["units"]:
                reserved = self.players.quest_reservations(updated)
                if reserved:
                    trial = deepcopy(updated)
                    trial["quest_state"]["active"] = None
                    self.players.credit_units(trial, reserved)
            config = self.bootstrap._rows(candidate, "config")
            encode_response(self._config_profile, dumps(Container(children=self.bootstrap.columns("config", config))))
            response = dumps(Container(children=(self.bootstrap._system(profile), *nodes)))
            encode_response(envelope.profile, response)
            if len(response) > RESPONSE_LIMIT:
                raise ProtocolError("barracks_response_capacity", "Barracks response exceeds its receipt limit", 503)
            self._ledger.record(updated, history, {"request": key, "content": content, "revision": profile.revision + 1,
                                                   "response": response.decode("utf-8"), "operation": operation})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving barracks", 409)
