from __future__ import annotations

from collections import Counter
from copy import deepcopy
from functools import cached_property
import hashlib
import json
import logging
import re

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps, loads
from .errors import ProtocolError
from .native_numbers import int32
from .player_state import BARRACKS_BITS, LOCK_FLAG, MAX_PLAYER_UNITS, UNIT_BOOLEANS, UNIT_FIELDS, barracks_index
from .protocol import ProtocolRegistry, encode_response
from .request_fields import fields, invalid, registered, scalar, values
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .system_literals import system_literals
from .unit_removal import consume_units


RESOURCE_LIMITS = ParseLimits(max_input_bytes=8 * 1024 * 1024, max_decompressed_bytes=8 * 1024 * 1024)
RESPONSE_LIMIT = 256 * 1024
HISTORY_NAMESPACE = "fairy_farm_history"
LOAD, DEPOSIT, BULK_DEPOSIT, WITHDRAW = 122, 123, 124, 125
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_INSTANCE_FIELDS = frozenset(("GetIndex", "UniqueID", "OrganizationIndex", "GeneralPurposeFlags"))
_MATERIAL_FLAGS = (0, 1)
_UNASSIGNED_ORGANIZATION = (-1, 0)
# Native warehouse senders clear their stock view before A7FDF0, which returns only after a 200; 66E8B0 resends refused bodies.
_REFUSAL_AFTERMATH = ("shows a communication error over its cleared stock view and resends the same request "
                      "until the server accepts it")
log = logging.getLogger("aigis_ps")


def _error(message):
    raise ProtocolError("fairy_farm_state_invalid", message, 503)


def _digest(content):
    return hashlib.sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _selection(indexes, identities):
    if (not 1 <= len(indexes) <= MAX_PLAYER_UNITS or len(indexes) != len(identities)
            or any(not int32(value) or value <= 0 for value in (*indexes, *identities))
            or len(set(indexes)) != len(indexes) or len(set(identities)) != len(identities)):
        invalid("Deposit identities must be positive, parallel and distinct")
    return tuple(zip(indexes, identities))


class FairyFarmService:
    def __init__(self, bootstrap, players, gifts, asset_reader, *, barracks=None):
        self.bootstrap, self.players, self.gifts = bootstrap, players, gifts
        self.barracks = barracks
        self._hashes, self._cards = {}, {}
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (41, LOAD, DEPOSIT, BULK_DEPOSIT, WITHDRAW)}
        try:
            self._barracks_version = None if barracks is None else barracks.content_version
            if barracks is not None and (not isinstance(self._barracks_version, str)
                    or not _HASH.fullmatch(self._barracks_version) or not callable(barracks.usage)):
                raise ValueError("Fairy deposits require verified barracks rules")
            definitions = self._table(asset_reader, "CfgFairyFarm.atb", {"CardID": 1, "DisplayOrder": 1})
            if not definitions or len(definitions) > 4096:
                raise ValueError("Fairy definitions must be bounded and nonempty")
            for row in definitions:
                identifier = row["CardID"]
                if not int32(identifier) or identifier <= 1 or identifier in self._cards or not int32(row["DisplayOrder"]):
                    raise ValueError("Invalid or duplicate fairy definition")
                card = gifts.catalog.card(identifier)
                cls = players.classes.get(card["InitClassID"])
                if (cls is None or not int32(cls.get("MaxLevel")) or cls["MaxLevel"] != 1
                        or not int32(card.get("CostDecValue")) or card["CostDecValue"] != 0
                        or not int32(card.get("ClassLV0SkillID")) or card["ClassLV0SkillID"] < 0):
                    raise ValueError("Fairy growth differs from the verified one-level template")
                self._cards[identifier] = dict(card)
            commands = self._table(asset_reader, "OnSystemCommand.atb", {"_ID": 1, "_Cmnd": 32})
            self.cap = system_literals(commands, {"FairyFarmLeaveMax": 99})["FairyFarmLeaveMax"]
            if not int32(self.cap) or self.cap <= 0:
                raise ValueError("Fairy capacity must be positive")
            self.card_ids = tuple(sorted(self._cards))
            self._content = {"master": gifts.catalog.snapshot.identifier, "client": gifts.catalog.snapshot.client_hash,
                             "version": gifts.catalog.resource_version, "resources": self._hashes, "cap": self.cap,
                             "cards": self._cards, "deposit_policy": 3, "barracks": self._barracks_version}
            self.content_version = _digest(self._content)
        except (AttributeError, ValueError, KeyError, TypeError, OSError) as error:
            raise ProtocolError("fairy_farm_resources_invalid", "Fairy warehouse resources differ from the verified contract", 503) from error
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "fairy_farm_state_invalid", "Warehouse receipt history is invalid",
                                     fields=("request", "content", "revision", "response"), response_limit=RESPONSE_LIMIT)

    def _table(self, reader, name, expected):
        raw = reader(name)
        if not isinstance(raw, bytes) or len(raw) > RESOURCE_LIMITS.max_input_bytes:
            raise ValueError("Warehouse resource must be bounded bytes")
        self._hashes[name] = hashlib.sha256(raw).hexdigest()
        payload = decompress_all4(raw, RESOURCE_LIMITS) if raw.startswith(b"ALL4") else raw
        table = parse_table(payload, RESOURCE_LIMITS)
        schema = {field["name"]: field["type_code"] for field in table["fields"]}
        if schema != expected or len(schema) != len(table["fields"]):
            raise ValueError("Warehouse resource schema changed")
        return table["rows"]

    def status(self):
        return {"implemented_operations": list(self.handlers()), "card_count": len(self.card_ids), "per_card_capacity": self.cap,
                "content_version": self.content_version, "resource_sha256": dict(self._hashes), "native_tested": False,
                "extra_barracks_deposits": self.barracks is not None,
                "local_policies": ["Protect organized units",
                                   "Selected deposits accept locked or grown fairies like the native selection and keep only their count",
                                   "Withdraw standard level-one units with new persistent identities",
                                   "Bulk deposit fills each kind in GetIndex order and skips locked, organized, modified or full entries",
                                   "Selected deposits must share one current barracks; omitted BRK covers all unlocked barracks",
                                   "Refusals are logged because the native client shows a communication error and resends "
                                   "the refused request until it is accepted"],
                "unsupported": [] if self.barracks is not None else ["Additional barrack storage indices"],
                "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "request_order": REQUEST_ORDER,
                "replay_policy": ("Exact body only at its committed revision and content version, also once barracks "
                                  "rules absent at its commit attach later; processed bodies are refused")}

    def replayable_content_versions(self):
        return frozenset((self.content_version, *self._absent_versions))

    @cached_property
    def _absent_versions(self):
        # A receipt committed before the barracks rules attached keeps the digest of that configuration.
        return () if self.barracks is None else (_digest({**self._content, "barracks": None}),)

    def _ready(self):
        if self.barracks is not None and self.barracks.content_version != self._barracks_version:
            raise ProtocolError("fairy_farm_resources_changed", "Barracks rules changed after warehouse setup", 503)

    def _available_barracks(self, state):
        if self.barracks is None:
            return frozenset((0,))
        return frozenset(range(len(self.barracks.usage(state).capacities)))

    def _require_barrack(self, index, available):
        if index not in available:
            if self.barracks is None:
                raise ProtocolError("fairy_farm_barrack_unsupported", "Extra barracks require verified capacity rules", 501)
            raise ProtocolError("fairy_farm_barrack_locked", "The selected barracks has not been unlocked", 409)

    def handlers(self):
        return {LOAD: self.load, DEPOSIT: self.deposit, BULK_DEPOSIT: self.bulk_deposit, WITHDRAW: self.withdraw}

    def read(self, state):
        if not isinstance(state, dict) or not isinstance(state.get("tables"), dict):
            _error("Warehouse state requires a table object")
        rows = state["tables"].get("fairy_farm", [])
        schema = state.get("fairy_farm_schema")
        if (not isinstance(rows, list) or len(rows) > len(self.card_ids)
                or (schema is not None and (type(schema) is not int or schema != 1))
                or (rows and schema != 1)):
            _error("Warehouse schema is unsupported; existing data was preserved")
        counts = dict.fromkeys(self.card_ids, 0)
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"CardID", "Count"}:
                _error("Warehouse rows require CardID and Count")
            identifier, count = row["CardID"], row["Count"]
            if (not int32(identifier) or identifier not in counts or identifier in seen
                    or not int32(count) or not 0 <= count <= self.cap):
                _error("Warehouse identifiers or quantities are invalid")
            seen.add(identifier)
            counts[identifier] = count
        return [{"CardID": identifier, "Count": count} for identifier, count in counts.items()]

    def response(self, state, card_ids=None):
        rows = self.read(state)
        if card_ids is not None:
            if not isinstance(card_ids, (list, tuple)) or len(card_ids) > len(rows):
                raise ProtocolError("fairy_farm_selection_invalid", "Expected a bounded fairy identifier list")
            if any(not int32(identifier) or identifier not in self._cards for identifier in card_ids) or len(set(card_ids)) != len(card_ids):
                raise ProtocolError("fairy_farm_selection_invalid", "Fairy selection is unknown or duplicated")
            selected = set(card_ids)
            rows = [row for row in rows if row["CardID"] in selected]
        return Container("PFF", tuple(Column(name, "I", tuple(row[name] for row in rows)) for name in ("CardID", "Count")))

    def changed_counts(self, state, changes):
        if (not isinstance(changes, dict) or len(changes) > len(self.card_ids)
                or any(not int32(identifier) or identifier not in self._cards or not int32(change) or not change
                       for identifier, change in changes.items())):
            raise ProtocolError("fairy_farm_changes_invalid", "Fairy adjustments require known cards and nonzero counts")
        counts = {row["CardID"]: row["Count"] for row in self.read(state)}
        for identifier, change in changes.items():
            counts[identifier] += change
            if not 0 <= counts[identifier] <= self.cap:
                raise ProtocolError("fairy_farm_quantity", "Fairy stock is insufficient or would exceed capacity", 409)
        return counts

    def adjust(self, state, changes):
        counts = self.changed_counts(state, changes)
        result = deepcopy(state)
        result["fairy_farm_schema"] = 1
        result["tables"]["fairy_farm"] = [{"CardID": identifier, "Count": count} for identifier, count in counts.items()]
        encode_response(self._profiles[LOAD], dumps(Container(children=(self.response(result),))))
        return result

    def debit(self, state, costs):
        if not isinstance(costs, dict) or len(costs) > len(self.card_ids):
            raise ProtocolError("fairy_farm_cost_invalid", "Expected bounded fairy costs")
        if any(not int32(identifier) or identifier not in self._cards or not int32(count) or count <= 0
               for identifier, count in costs.items()):
            raise ProtocolError("fairy_farm_cost_invalid", "Fairy costs must use known cards and positive quantities")
        if not costs:
            self.read(state)
            return deepcopy(state)
        return self.adjust(state, {identifier: -count for identifier, count in costs.items()})

    def _require_material(self, card_id):
        if not int32(card_id) or card_id not in self._cards:
            raise ProtocolError("fairy_farm_card_unknown", "Card is not a verified warehouse material")
        return self._cards[card_id]

    def material(self, card_id):
        card = self._require_material(card_id)
        unit = {name: False if name in UNIT_BOOLEANS else 0 for name, _ in UNIT_FIELDS}
        unit.update(CardID=card_id, ClassID=card["InitClassID"], SkillLevel=1,
                    SettingSkillID=card["ClassLV0SkillID"], OrganizationIndex=-1, GeneralPurposeFlags=1)
        return unit

    def load(self, envelope, store):
        registered(envelope, (LOAD,), ())
        profile = self.players.authenticate(envelope, store)
        return self._listing(profile, profile.state)

    def _listing(self, profile, state):
        response = dumps(Container(children=(self.bootstrap._system(profile), self.response(state))))
        encode_response(self._profiles[LOAD], response)
        return response

    def deposit(self, envelope, store):
        request = registered(envelope, (DEPOSIT,), ("LST",), ("LST",))
        table = fields(request["LST"], ("A7", "AB"), ("A7", "AB"))
        indexes, identities = (values(table[name], "I", minimum=1, maximum=MAX_PLAYER_UNITS) for name in ("A7", "AB"))
        return self._transact(envelope, store, _selection(indexes, identities))

    def withdraw(self, envelope, store):
        request = registered(envelope, (WITHDRAW,), ("Draw",), ("Draw",))
        table = fields(request["Draw"], ("CardID", "Count"), ("CardID", "Count"))
        card_id, count = (scalar(table[name], "I") for name in ("CardID", "Count"))
        return self._transact(envelope, store, self._withdrawal(card_id, count))

    def _withdrawal(self, card_id, count):
        if (not int32(card_id) or card_id not in self._cards
                or not int32(count) or not 1 <= count <= min(self.cap, MAX_PLAYER_UNITS)):
            invalid("Withdrawal requires a known fairy and bounded positive count")
        return card_id, count

    def bulk_deposit(self, envelope, store):
        request = registered(envelope, (BULK_DEPOSIT,), ("BRK",))
        index = None
        if "BRK" in request:
            table = fields(request["BRK"], ("IDX",), ("IDX",))
            index = scalar(table["IDX"], "I")
            if index < 0:
                invalid("A negative barrack index must use the native omitted BRK form")
            if index > 2:
                raise ProtocolError("fairy_farm_barrack_unsupported", "Barracks index exceeds the verified range", 501)
        return self._transact(envelope, store, index)

    def _transact(self, envelope, store, selection):
        operation = envelope.profile.registration_keys[0]
        try:
            return self._commit(envelope, store, operation, selection)
        except ProtocolError as error:
            log.warning("Fairy warehouse %d refused with %s; the native client %s",
                        operation, error.code, _REFUSAL_AFTERMATH)
            raise

    def _commit(self, envelope, store, operation, selection):
        key = hashlib.sha256((envelope.profile.identity + ":").encode() + envelope.xml).hexdigest()
        for _ in range(3):
            self._ready()
            profile = self.players.authenticate(envelope, store)
            player = self.players.validate(profile.state)
            self.read(profile.state)
            history = self._ledger.read(profile.state, profile.revision)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                if cached["revision"] != profile.revision or cached["content"] not in self.replayable_content_versions():
                    raise ProtocolError("fairy_farm_retry_stale", "Warehouse state or content changed after this transaction", 409)
                response = cached["response"].encode()
                try:
                    loads(response)
                    encode_response(envelope.profile, response)
                except (ValueError, ProtocolError) as error:
                    raise ProtocolError("fairy_farm_state_invalid", "Saved warehouse response is invalid", 503) from error
                return response
            if self._ledger.processed(history, key):
                raise ProtocolError("fairy_farm_request_stale", "Warehouse request was already processed", 409)
            if operation == DEPOSIT:
                updated = self._deposit_state(profile.state, profile.revision, player, selection)
                children = (self.bootstrap._system(profile), self.response(updated))
            elif operation == BULK_DEPOSIT:
                updated, removed, code = self._bulk_state(profile.state, profile.revision, player, selection)
                children = (self.bootstrap._system(profile), self.response(updated),
                    Container("RML", (Column("A7", "I", tuple(unit["GetIndex"] for unit in removed)),
                                      Column("AB", "I", tuple(unit["UniqueID"] for unit in removed)))),
                    Container("ERC", (Column("Code", "I", (code,)),)))
            else:
                card_id, count = selection
                updated, units = self._withdraw_state(profile.state, card_id, count)
                children = (self.bootstrap._system(profile),
                    Container("PBP", (Column("A1", "S", ("UX",)), Column("A2", "I", (updated["player_state"]["battle"]["UX"],)))),
                    Container("PPU", tuple(Column(alias, "B" if name in UNIT_BOOLEANS else "I", tuple(unit[name] for unit in units))
                                           for name, alias in UNIT_FIELDS)), self.response(updated))
            self.players.validate_player(updated["player_state"])
            encode_response(self._profiles[41], self.players.unit_response(updated["player_state"]["units"]))
            self._listing(profile, updated)
            response = dumps(Container(children=children))
            encode_response(envelope.profile, response)
            if len(response) > RESPONSE_LIMIT:
                raise ProtocolError("fairy_farm_response_capacity", "Warehouse response exceeds receipt capacity", 409)
            self._ledger.record(updated, history, {"request": key, "content": self.content_version,
                                                   "revision": profile.revision + 1, "response": response.decode()})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving the warehouse", 409)

    def deposit_state(self, state, revision, selection):
        selection = _selection(tuple(index for index, _ in selection), tuple(identity for _, identity in selection))
        return self._deposit_state(state, revision, self._writable(state), selection)

    def withdraw_state(self, state, card_id, count=1):
        card_id, count = self._withdrawal(card_id, count)
        self._writable(state)
        return self._withdraw_state(state, card_id, count)

    def _writable(self, state):
        self._ready()
        player = self.players.validate(state)
        self.read(state)
        return player

    def _withdraw_state(self, state, card_id, count):
        self.players.require_unit_space(state, count)
        updated = self.debit(state, {card_id: count})
        return self.players.credit_units(updated, [{"card_id": card_id, "level": 1, "count": count}])

    def _deposit_state(self, state, revision, player, selection, *, allow_mixed_barracks=False):
        owned = {unit["GetIndex"]: unit for unit in player["units"]}
        organized = {row["UnitIndex"] for row in player["squads"]}
        available = self._available_barracks(state)
        counts, removed, sources = Counter(), [], set()
        for index, identity in selection:
            unit = owned.get(index)
            if unit is None or unit["UniqueID"] != identity:
                raise ProtocolError("fairy_farm_unit_missing", "Deposit identity does not match an owned unit", 409)
            source = barracks_index(unit)
            self._require_barrack(source, available)
            sources.add(source)
            if not allow_mixed_barracks and len(sources) > 1:
                raise ProtocolError("fairy_farm_mixed_barracks", "Selected deposits must belong to one current barracks", 409)
            # The native selection only disables organized units; the count-only stock keeps no instance state.
            if index in organized:
                raise ProtocolError("fairy_farm_unit_protected", "Organized units cannot be deposited", 409)
            self._require_material(unit["CardID"])
            removed.append(unit)
            counts[unit["CardID"]] += 1
        updated = self.adjust(self.gifts.preserve_history(state, revision), counts)
        consume_units(updated["player_state"], removed)
        return updated

    def _bulk_keeps(self, unit, organized):
        standard = self.material(unit["CardID"])
        return (unit["GetIndex"] in organized or bool(unit["GeneralPurposeFlags"] & LOCK_FLAG)
                or any(unit[name] != standard[name] for name, _ in UNIT_FIELDS if name not in _INSTANCE_FIELDS)
                or (unit["GeneralPurposeFlags"] & ~BARRACKS_BITS) not in _MATERIAL_FLAGS
                or unit["OrganizationIndex"] not in _UNASSIGNED_ORGANIZATION)

    def _bulk_state(self, state, revision, player, barrack):
        available = self._available_barracks(state)
        if barrack is None:
            scope = available
            if self.barracks is None and any(barracks_index(unit) for unit in player["units"]):
                raise ProtocolError("fairy_farm_barrack_unsupported", "All-barracks deposits require verified extra barracks rules", 501)
        else:
            self._require_barrack(barrack, available)
            scope = frozenset((barrack,))
        counts = {row["CardID"]: row["Count"] for row in self.read(state)}
        organized = {row["UnitIndex"] for row in player["squads"]}
        selected, full = [], False
        for unit in sorted(player["units"], key=lambda row: row["GetIndex"]):
            if barracks_index(unit) not in scope or unit["CardID"] not in self._cards:
                continue
            # Bulk deposits pick units themselves, so locked, organized and grown instances stay in the roster.
            if self._bulk_keeps(unit, organized):
                continue
            if counts[unit["CardID"]] >= self.cap:
                full = True
                continue
            counts[unit["CardID"]] += 1
            selected.append(unit)
        if not selected:
            return deepcopy(state), [], -1
        identities = tuple((unit["GetIndex"], unit["UniqueID"]) for unit in selected)
        updated = self._deposit_state(state, revision, player, identities, allow_mixed_barracks=True)
        return updated, selected, 1 if full else 0
