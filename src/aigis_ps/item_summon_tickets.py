from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import logging
import re
import secrets
from types import MappingProxyType

from .alxml import Column, Container, dumps
from .errors import ProtocolError
from .item_summon_catalog import ENSURED_RULE, UNOWNED_RULE
from .native_numbers import int32
from .player_state import UNIT_BOOLEANS, UNIT_FIELDS
from .possessions import STORED_ITEM_NAME_BYTES
from .presents import CLAIM_LIMITS, MAX_PRESENTS, PRESENT_LIMITS
from .protocol import ProtocolRegistry, encode_response
from .request_fields import fields, invalid, registered, values
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .summon_grants import SummonGrants
from .summon_tickets import SummonTicketRules


RESPONSE_LIMIT = 256 * 1024
MAX_DRAWS = 100
DEFAULT_ITEM_TICKET_POOLS = (194, 212, 218, 248, 253, 275, 300, 325, 334, 335, 338, 340, 341, 343, 344, 346, 364, 365,
                             367, 375, 376, 386, 403, 415, 416, 417, 418, 419, 420, 423, 426, 428, 443, 448, 451, 452,
                             454, 460, 461, 463, 464, 470, 471, 472)
RULE_POOL_UNSUPPORTED = "lottery_rule_pool_unsupported"
HISTORY_NAMESPACE = "item_summon_history"
HISTORY_INVALID = ("item_summon_history_invalid", "Stored item summon receipts are invalid")
RECEIPT_FIELDS = ("request", "profile", "content", "state", "revision", "response", "mail_id", "item_id", "pool_id",
                  "candidate_indices", "units")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_UNIT_NAMES = frozenset(name for name, _ in UNIT_FIELDS)
log = logging.getLogger("aigis_ps")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _state_digest(state):
    return _digest({key: value for key, value in state.items() if key != HISTORY_NAMESPACE})


def _bad_history():
    raise ProtocolError(*HISTORY_INVALID, 503)


def _unit_valid(unit):
    return (isinstance(unit, dict) and unit.keys() == _UNIT_NAMES
            and all(type(value) is bool if name in UNIT_BOOLEANS else int32(value) for name, value in unit.items()))


def _receipt_valid(entry):
    indices, units, item_id = entry["candidate_indices"], entry["units"], entry["item_id"]
    return (type(entry["profile"]) is str and _HASH.fullmatch(entry["profile"]) is not None
            and all(int32(entry[name]) and entry[name] > 0 for name in ("mail_id", "pool_id"))
            and type(item_id) is str and 0 < len(item_id.encode("utf-8")) <= STORED_ITEM_NAME_BYTES
            and isinstance(indices, list) and 1 <= len(indices) <= MAX_DRAWS
            and all(int32(index) and index >= 0 for index in indices)
            and isinstance(units, list) and len(units) == len(indices) and all(_unit_valid(unit) for unit in units))


def _guaranteed_indices(rule, pool, groups):
    drawable = tuple(index for index, candidate in enumerate(pool.candidates) if candidate.weight > 0)
    matched = tuple(index for index in drawable if groups[index] == rule.rarity_group)
    chosen = [pool.candidates[index] for index in matched]
    # Ratios stand in for the native "equal chance within the rarity" text only while they agree.
    if (not matched or len({candidate.weight for candidate in chosen}) != 1
            or len({candidate.card_id for candidate in chosen}) != len(chosen)
            or (rule.rule_type == UNOWNED_RULE and matched != drawable)):
        return None
    return matched


def _honours(rule, matched, pool, indices, owned):
    if rule is None:
        return True
    if rule.rule_type == ENSURED_RULE:
        return all(index in matched for index in indices[len(indices) - rule.ensured_count:])
    return all(index in matched and pool.candidates[index].card_id not in owned for index in indices)


class ItemSummonTicketService:
    def __init__(self, bootstrap, players, presents, pools, catalog, *, rules=None, randbelow=None):
        self.bootstrap, self.players, self.presents = bootstrap, players, presents
        self.pools, self.catalog = pools, catalog
        self.rules = SummonTicketRules(DEFAULT_ITEM_TICKET_POOLS) if rules is None else rules
        self.randbelow = secrets.randbelow if randbelow is None else randbelow
        if not isinstance(self.rules, SummonTicketRules) or not callable(self.randbelow):
            raise ValueError("Item tickets require verified pool rules and a random source")
        self._enabled_pools = self.rules.enabled_pools
        self.grants = SummonGrants(players, pools, self._enabled_pools)
        self._guarantees = self._plan_guarantees()
        self.content_version = self._content()
        self._ready()
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (1, 41, 42)}
        self._ledger = RequestLedger(HISTORY_NAMESPACE, *HISTORY_INVALID, fields=RECEIPT_FIELDS,
                                     response_limit=RESPONSE_LIMIT, check=_receipt_valid)

    def _content(self):
        try:
            versions = {"catalog": self.catalog.content_version, "pools": self.grants.pool_version,
                        "initialization": self.grants.initialization_version}
            if any(type(value) is not str or not _HASH.fullmatch(value) for value in versions.values()):
                raise ValueError("Item tickets require stable content fingerprints")
            return _digest({"policy": 2, "dependencies": versions, "rules": asdict(self.rules),
                "claim": "one_mail_full_draw_count", "shortage": "reject_without_consumption",
                "duplicate_card": "new_instance_without_rainbow", "response": "SYSTEM_PBP_PPU",
                "content": 1, "max_draws": MAX_DRAWS, "guarantee": {
                    "ensured_slots": "last", "match": "rarity_group_uniform_ratio_distinct_cards",
                    "owned": "barracks_units", "exhausted": "reject_keep_mail", "retry": "frozen_draws_recheck_owned"}})
        except (AttributeError, TypeError, ValueError) as error:
            raise ProtocolError("item_summon_dependencies_invalid", "Item ticket dependencies are unavailable", 503) from error

    def _plan_guarantees(self):
        plans = {}
        for definition in self.catalog.definitions.values():
            key = definition.pool_id, definition.rule
            if (definition.rule is None or not definition.supported or definition.pool_id not in self._enabled_pools
                    or key in plans):
                continue
            pool = self.grants.validate_pool(definition.pool_id)
            groups = tuple(self.catalog.rarity_group(rare) for rare in self.grants.candidate_rarities(pool.pool_id))
            plans[key] = _guaranteed_indices(definition.rule, pool, groups)
        return MappingProxyType(plans)

    def _guarantee(self, definition):
        if definition.rule is None:
            return None
        matched = self._guarantees.get((definition.pool_id, definition.rule))
        if matched is None:
            raise ProtocolError("item_summon_unsupported", RULE_POOL_UNSUPPORTED, 501)
        return matched

    def _ready(self):
        self.grants.ready()
        if (self.grants.players is not self.players or self.grants.pools is not self.pools
                or self.grants.enabled_pools != self._enabled_pools
                or not isinstance(self.rules, SummonTicketRules) or self.rules.enabled_pools != self._enabled_pools
                or not self.catalog.is_ready() or self._content() != self.content_version):
            raise ProtocolError("item_summon_dependencies_changed", "Item ticket definitions changed after setup", 503)
        if (self.presents.item_catalog is None
                or self.catalog.item_resource_sha256 != self.presents.item_catalog.asset_sha256):
            raise ProtocolError("item_summon_dependencies_changed", "Item ticket and present definitions differ", 503)

    def validate_item(self, item_id):
        self._ready()
        definition = self.catalog.definition(item_id)
        if definition.unsupported_reason is not None:
            raise ProtocolError("item_summon_unsupported", definition.unsupported_reason, 501)
        if not int32(definition.count) or not 1 <= definition.count <= MAX_DRAWS:
            raise ProtocolError("item_summon_count_unsupported", "The item draw count is unsupported", 501)
        self.grants.validate_pool(definition.pool_id)
        self._guarantee(definition)
        return definition

    def status(self):
        try:
            self._ready()
            ready, error = True, None
        except ProtocolError as failure:
            ready, error = False, failure.code
        usable = [item for item in self.catalog.definitions.values()
                  if item.unsupported_reason is None and item.pool_id in self._enabled_pools]
        refused = sorted(item.item_id for item in usable
                         if item.rule is not None and self._guarantees.get((item.pool_id, item.rule)) is None)
        return {"ready": ready, "error": error, "enabled_pools": list(self._enabled_pools),
                "enabled_items": sorted({item.item_id for item in usable} - set(refused)),
                "rule_pool_unsupported": refused, "content_version": self.content_version,
                "catalog": self.catalog.status(), "claim_policy": "One mail, complete draw count or no change",
                "guarantee_policy": "Rule type 1 draws its ensured slots last from the guaranteed rarity group; "
                                    "rule type 2 draws a unit no barracks holds and refuses when none remain",
                "duplicate_card_policy": "New instance without rainbow compensation",
                "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "request_order": REQUEST_ORDER, "native_tested": False}

    @staticmethod
    def _key(envelope):
        identity = _digest(asdict(envelope.profile))
        return identity, hashlib.sha256(identity.encode() + b":" + envelope.xml).hexdigest()

    def matches_request(self, profile, envelope, selected):
        _, key = self._key(envelope)
        if self._ledger.processed(self._ledger.read(profile.state, profile.revision), key):
            return True
        return any(row["Type"] == 22 and row["PresentID"] in selected and self.catalog.supports_item(row["ItemID"])
                   for row in self.presents.rows(profile.state))

    @staticmethod
    def _request(envelope, selected):
        if envelope.profile.registration_keys != (1, 2):
            raise ProtocolError("item_summon_batch_unsupported", "Item summon tickets require individual claims", 501)
        root = registered(envelope, {1, 2}, {"RT"}, {"RT"})
        rt = fields(root["RT"], {"PT2"}, {"PT2"})
        table = fields(rt["PT2"], {"PresentID"}, {"PresentID"})
        identifiers = values(table["PresentID"], "I", minimum=1, maximum=MAX_PRESENTS)
        if (not isinstance(selected, (list, tuple)) or tuple(selected) != identifiers
                or any(identifier <= 0 for identifier in identifiers) or len(set(identifiers)) != len(identifiers)):
            invalid("Item ticket selection does not match the request")
        if len(identifiers) != 1:
            raise ProtocolError("item_summon_single_required", "Claim one item summon ticket at a time", 501)
        return identifiers[0]

    def _draw(self, definition, pool, matched, owned):
        rule = definition.rule
        if rule is None:
            slots = (None,) * definition.count
        elif rule.rule_type == ENSURED_RULE:
            slots = (None,) * (definition.count - rule.ensured_count) + (matched,) * rule.ensured_count
        else:
            unowned = tuple(index for index in matched if pool.candidates[index].card_id not in owned)
            if not unowned:
                log.warning("Refused request 1/2: the barracks already hold every unit %s can grant, so the mail stays",
                            definition.item_id)
                raise ProtocolError("item_summon_unowned_exhausted", "Every unit this ticket can grant is already owned", 409)
            slots = (unowned,)
        return tuple(self._draw_one(pool, subset) for subset in slots)

    def _draw_one(self, pool, subset):
        if subset is not None:
            return self.grants.draw(pool.pool_id, subset, self.randbelow)
        chosen = self.pools.draw(pool.pool_id, self.randbelow)
        if chosen not in pool.candidates or chosen.weight <= 0:
            raise ProtocolError("item_summon_result_invalid", "Draw result is not a positive-weight candidate", 503)
        return pool.candidates.index(chosen)

    def _preflight(self, profile, state):
        candidate = replace(profile, state=state)
        player = self.players.validate(state)
        encode_response(self._profiles[41], self.players.unit_response(player["units"]))
        encode_response(self._profiles[42], dumps(Container(children=(
            self.bootstrap._system(candidate), self.presents.response(state))), limits=PRESENT_LIMITS))

    def _response(self, profile, state, units):
        payload = dumps(Container(children=(self.bootstrap._system(profile), Container("PBP", (
            Column("A1", "S", ("UX",)), Column("A2", "I", (state["player_state"]["battle"]["UX"],)))),
            Container("PPU", tuple(Column(alias, "B" if name in UNIT_BOOLEANS else "I",
                tuple(unit[name] for unit in units)) for name, alias in UNIT_FIELDS)))), limits=CLAIM_LIMITS)
        if len(payload) > RESPONSE_LIMIT:
            raise ProtocolError("item_summon_response_capacity", "Item summon response exceeds its receipt limit", 503)
        encode_response(self._profiles[1], payload)
        return payload

    def _replay(self, entry, profile, envelope, mail_id, profile_key):
        if entry["revision"] != profile.revision or entry["content"] != self.content_version:
            raise ProtocolError("item_summon_retry_stale", "Item ticket state or resources changed after this claim", 409)
        if (entry["state"] != _state_digest(profile.state) or entry["profile"] != profile_key
                or entry["mail_id"] != mail_id
                or any(row["PresentID"] == mail_id for row in self.presents.rows(profile.state))):
            _bad_history()
        definition = self.validate_item(entry["item_id"])
        indices, units = entry["candidate_indices"], entry["units"]
        pool = self.grants.validate_pool(entry["pool_id"])
        if definition.pool_id != pool.pool_id or definition.count != len(indices):
            _bad_history()
        player = self.players.validate(profile.state)
        if (player["units"][-len(units):] != units or units[-1]["GetIndex"] != player["battle"]["UX"]
                or units[-1]["UniqueID"] != player.get("last_unique_id")):
            _bad_history()
        # An exact replay matches the post-claim state, so the roster before the new units is the claim-time roster.
        owned = frozenset(unit["CardID"] for unit in player["units"][:-len(units)])
        if not _honours(definition.rule, self._guarantee(definition), pool, indices, owned):
            _bad_history()
        for offset, (index, unit) in enumerate(zip(indices, units)):
            if (index >= len(pool.candidates) or pool.candidates[index].weight <= 0
                    or unit["GetIndex"] != units[0]["GetIndex"] + offset
                    or unit["UniqueID"] != units[0]["UniqueID"] + offset
                    or unit != self.grants.expected_unit(pool.pool_id, index, unit["GetIndex"], unit["UniqueID"])):
                _bad_history()
        self._preflight(profile, profile.state)
        payload = self._response(profile, profile.state, units)
        if payload != entry["response"].encode("utf-8"):
            _bad_history()
        encode_response(envelope.profile, payload)
        return payload

    def receive(self, envelope, store, selected):
        mail_id = self._request(envelope, selected)
        profile_key, key = self._key(envelope)
        frozen = None
        for _ in range(3):
            self._ready()
            profile = self.bootstrap._authenticate(envelope, store)
            player = self.players.validate(profile.state)
            rows = self.presents.rows(profile.state)
            history = self._ledger.read(profile.state, profile.revision)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                return self._replay(cached, profile, envelope, mail_id, profile_key)
            if self._ledger.processed(history, key):
                log.warning("Refused request 1/2: it repeats a processed item summon claim whose receipt was evicted")
                raise ProtocolError("item_summon_ticket_request_stale", "Item ticket claim was already processed", 409)
            mail = next((row for row in rows if row["PresentID"] == mail_id), None)
            if mail is None:
                raise ProtocolError("present_not_found", "Item summon ticket is unavailable or already claimed", 409)
            if mail["Type"] != 22 or mail["Content"] != 1:
                raise ProtocolError("item_summon_mail_unsupported", "One item ticket per mail is required", 501)
            definition = self.validate_item(mail["ItemID"])
            pool = self.grants.validate_pool(definition.pool_id)
            matched = self._guarantee(definition)
            self.players.require_unit_space(profile.state, definition.count)
            owned = frozenset(unit["CardID"] for unit in player["units"])
            if frozen is None:
                frozen = deepcopy(mail), self._draw(definition, pool, matched, owned)
            elif frozen[0] != mail:
                raise ProtocolError("item_summon_changed", "Item ticket changed during the claim", 409)
            indices = frozen[1]
            if not _honours(definition.rule, matched, pool, indices, owned):
                raise ProtocolError("item_summon_changed", "The drawn unit joined the barracks during the claim", 409)
            updated, added = self.grants.grant(profile.state, pool.pool_id, indices)
            updated["tables"]["presents"] = [row for row in rows if row["PresentID"] != mail_id]
            self._preflight(profile, updated)
            payload = self._response(profile, updated, added)
            encode_response(envelope.profile, payload)
            self._ledger.record(updated, history, {
                "request": key, "profile": profile_key, "content": self.content_version,
                "state": _state_digest(updated), "revision": profile.revision + 1, "response": payload.decode("utf-8"),
                "mail_id": mail_id, "item_id": definition.item_id, "pool_id": pool.pool_id,
                "candidate_indices": list(indices), "units": deepcopy(added)})
            try:
                store.update_state(updated, profile.revision)
                return payload
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while claiming an item summon ticket", 409)
