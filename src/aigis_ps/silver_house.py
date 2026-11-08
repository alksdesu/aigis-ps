from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import re
import unicodedata

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, dumps, loads
from .errors import REFUSAL_AFTERMATH as _REFUSAL_AFTERMATH, REFUSAL_POLICY, ProtocolError
from .native_numbers import int32
from .player_state import (LOCK_FLAG, MAX_PLAYER_UNITS, PRINCE_CARD_IDS, UNIT_BOOLEANS, UNIT_FIELDS,
                           barracks_index)
from .protocol import ProtocolRegistry, encode_response
from .request_fields import fields, invalid, registered, scalar, values
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .system_literals import decode_command
from .training_math import derived_level, experience_threshold
from .unit_removal import consume_units


RESOURCE_LIMITS = ParseLimits(max_input_bytes=16 * 1024 * 1024, max_decompressed_bytes=16 * 1024 * 1024)
RESPONSE_LIMIT = 256 * 1024
HISTORY_NAMESPACE = "silver_house_history"
LOAD, DEPOSIT, WITHDRAW = 126, 127, 128
_STRING = r'"(?:[^"\\]|\\.)*"'
_PARAMETERS = re.compile(rf'\s*MnObj_SetParam\s*\(((?:\s*{_STRING}\s*,?)*)\)\s*;')
log = logging.getLogger("aigis_ps")


@dataclass(frozen=True)
class SilverHouseRules:
    reward_count: int = 1

    def __post_init__(self):
        if type(self.reward_count) is not int or not 0 <= self.reward_count <= MAX_PLAYER_UNITS:
            raise ValueError("Silver warehouse reward count must be a bounded nonnegative integer")


def _integer(value, minimum=0):
    if not int32(value) or value < minimum:
        raise ValueError("Expected a bounded native integer")
    return value


def _deposits(indexes, identities, maximum):
    if (not 1 <= len(indexes) <= maximum or len(indexes) != len(identities)
            or any(not int32(value) or value <= 0 for value in (*indexes, *identities))
            or len(set(indexes)) != len(indexes) or len(set(identities)) != len(identities)):
        invalid("Silver deposit requires parallel distinct positive identities")
    return tuple(zip(indexes, identities))


class SilverHouseService:
    def __init__(self, bootstrap, players, cards, gifts=None, asset_reader=None, *, rules=SilverHouseRules()):
        if not isinstance(rules, SilverHouseRules):
            raise ValueError("Silver warehouse requires validated local rules")
        self.bootstrap, self.players, self.cards, self.gifts, self.rules = bootstrap, players, cards, gifts, rules
        self._card_ids = frozenset(cards.card_ids)
        self._hashes, self._thresholds = {}, {}
        self.cap, self.max_selected, self.reward_tiers = None, None, {}
        self._write_ready = gifts is not None and asset_reader is not None
        registry = ProtocolRegistry.load()
        self._profiles = {number: registry.by_registration[number] for number in (41, LOAD, DEPOSIT, WITHDRAW)}
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "silver_house_state_invalid", "Silver receipt history is invalid",
                                     fields=("request", "content", "revision", "response"), response_limit=RESPONSE_LIMIT)
        self.content_version = None
        if not self._write_ready:
            return
        try:
            commands = self._table(asset_reader, "OnCommandMenuUnitSilverHouse.atb")
            self._menu_parameters(commands)
            text_map = self._table(asset_reader, "cfgImplantText.atb")
            text_ids = [row["_TextID"] for row in text_map if row["_ID"] == "SilverHouse_LeaveConfirmation"]
            if len(text_ids) != 1:
                raise ValueError("Silver confirmation text is missing or ambiguous")
            texts = self._table(asset_reader, "SystemText.atb")
            confirmation = [row["Data_Text"] for row in texts if row["Recode_Index"] == text_ids[0]]
            if len(confirmation) != 1 or not isinstance(confirmation[0], str):
                raise ValueError("Silver confirmation text is invalid")
            tiers = re.findall(r"Lv\.(\d+)以上の★(\d+)ユニット", unicodedata.normalize("NFKC", confirmation[0]))
            if len(tiers) != 2 or tuple(int(stars) - 1 for _, stars in tiers) != (2, 3):
                raise ValueError("Silver reward thresholds changed")
            self.reward_tiers = {rarity: {"minimum_level": _integer(int(level), 1), "card_id": self._reward_cards[index]}
                                 for index, (level, rarity) in enumerate((level, int(stars) - 1) for level, stars in tiers)}
            experience = self._table(asset_reader, "ExperienceTable.atb")
            if not experience or any(_integer(row["Level"], 1) != index + 1 for index, row in enumerate(experience)):
                raise ValueError("Experience levels are not ordered")
            self._experience = tuple(_integer(row["Exp"]) for row in experience)
            derived_level(0, self._experience, 1)
            rarity_rows = self._table(asset_reader, "cfgRarity.atb")
            self._rarities = {_integer(row["ConfigID"], 1): dict(row) for row in rarity_rows}
            if len(self._rarities) != len(rarity_rows):
                raise ValueError("Duplicate growth rarity")
            self._classes = {key: dict(row) for key, row in players.classes.items()}
            content = {"master": cards.snapshot.identifier, "client": cards.snapshot.client_hash,
                       "resources": cards.resource_version, "hashes": self._hashes, "rules": asdict(rules),
                       "classes": self._classes, "reward_tiers": self.reward_tiers}
            self.content_version = hashlib.sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest()
        except (ValueError, TypeError, KeyError, OSError) as error:
            raise ProtocolError("silver_house_resources_invalid", "Silver warehouse resources differ from the verified contract", 503) from error

    def _table(self, reader, name):
        raw = reader(name)
        if not isinstance(raw, bytes) or len(raw) > RESOURCE_LIMITS.max_input_bytes:
            raise ValueError("Silver resource must be bounded bytes")
        self._hashes[name] = hashlib.sha256(raw).hexdigest()
        payload = decompress_all4(raw, RESOURCE_LIMITS) if raw.startswith(b"ALL4") else raw
        return parse_table(payload, RESOURCE_LIMITS)["rows"]

    def _menu_parameters(self, rows):
        matches = []
        for row in rows:
            command = decode_command(row["_Cmnd"])
            if "MenuCategoryID=SilverHouseLeave" not in command:
                continue
            call = _PARAMETERS.match(command)
            if call is None:
                raise ValueError("Silver menu parameters are not literal assignments")
            params = {}
            for literal in re.findall(_STRING, call.group(1)):
                name, separator, value = json.loads(literal).partition("=")
                if not separator or name in params:
                    raise ValueError("Silver menu contains invalid or duplicate parameters")
                params[name] = value
            matches.append(params)
        if len(matches) != 1:
            raise ValueError("Silver menu setup is missing or ambiguous")
        params = matches[0]
        required = ("LeaveLimit", "MaxMultiSelect", "RegCard00", "RegCard01")
        if any(not re.fullmatch(r"[0-9]{1,10}", params.get(name, "")) for name in required):
            raise ValueError("Silver capacities and reward cards must be literal integers")
        self.cap = _integer(int(params["LeaveLimit"]), 1)
        self.max_selected = _integer(int(params["MaxMultiSelect"]), 1)
        if self.max_selected > MAX_PLAYER_UNITS:
            raise ValueError("Silver selection limit exceeds the local roster")
        self._reward_cards = tuple(_integer(int(params[name]), 2) for name in ("RegCard00", "RegCard01"))
        if any(identifier not in self._card_ids for identifier in self._reward_cards):
            raise ValueError("Silver reward references an unknown card")

    def handlers(self):
        return {LOAD: self.load} | ({DEPOSIT: self.deposit, WITHDRAW: self.withdraw} if self._write_ready else {})

    def status(self):
        return {"implemented_operations": list(self.handlers()), "snapshot": "Full PSH replacement", "native_tested": False,
                "write_ready": self._write_ready, "per_card_capacity": self.cap, "max_selected": self.max_selected,
                "reward_tiers": deepcopy(self.reward_tiers), "local_rules": asdict(self.rules),
                "reward_policy": "Configured count per qualifying deposited unit; official server quantity not sampled",
                "withdraw_policy": "New standard level-one instance with fresh identity",
                "content_version": self.content_version, "resource_sha256": dict(self._hashes),
                "client_validation_pending": ["Type1 synthesis result-tag refresh"], "history_limit": HISTORY_LIMIT,
                "processed_request_limit": PROCESSED_LIMIT, "request_order": REQUEST_ORDER,
                "refusals": REFUSAL_POLICY}

    def read(self, state):
        if not isinstance(state, dict) or not isinstance(state.get("tables"), dict):
            raise ProtocolError("silver_house_state_invalid", "Silver warehouse requires stored tables", 503)
        rows = state["tables"].get("silver_house", [])
        schema = state.get("silver_house_schema")
        if (not isinstance(rows, list) or len(rows) > len(self._card_ids)
                or (schema is not None and (type(schema) is not int or schema != 1))
                or (rows and schema != 1)):
            raise ProtocolError("silver_house_state_invalid", "Silver warehouse schema is unsupported", 503)
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"CardID", "Count"}:
                raise ProtocolError("silver_house_state_invalid", "Silver warehouse rows require CardID and Count", 503)
            card_id, count = row["CardID"], row["Count"]
            if (not int32(card_id) or card_id not in self._card_ids or card_id in seen
                    or not int32(count) or count < 0):
                raise ProtocolError("silver_house_state_invalid", "Silver warehouse identities or counts are invalid", 503)
            seen.add(card_id)
            result.append(dict(row))
        return sorted(result, key=lambda row: row["CardID"])

    def response(self, state):
        rows = self.read(state)
        return Container("PSH", tuple(Column(name, "I", tuple(row[name] for row in rows)) for name in ("CardID", "Count")))

    def load(self, envelope, store):
        registered(envelope, (LOAD,), ())
        profile = self.players.authenticate(envelope, store)
        return self._listing(profile, profile.state)

    def _listing(self, profile, state):
        response = dumps(Container(children=(self.bootstrap._system(profile), self.response(state))))
        encode_response(self._profiles[LOAD], response)
        return response

    def _ready(self):
        if not self._write_ready:
            raise ProtocolError("silver_house_dependencies_unavailable", "Silver warehouse writes need verified resources and history support", 503)

    def deposit(self, envelope, store):
        self._ready()
        request = registered(envelope, (DEPOSIT,), ("LST",), ("LST",))
        table = fields(request["LST"], ("A7", "AB"), ("A7", "AB"))
        indexes, identities = (values(table[name], "I", minimum=1, maximum=self.max_selected) for name in ("A7", "AB"))
        return self._transact(envelope, store, _deposits(indexes, identities, self.max_selected))

    def withdraw(self, envelope, store):
        self._ready()
        request = registered(envelope, (WITHDRAW,), ("Draw",), ("Draw",))
        table = fields(request["Draw"], ("CardID",), ("CardID",))
        return self._transact(envelope, store, self._withdrawable(scalar(table["CardID"], "I")))

    def _withdrawable(self, identifier):
        if not int32(identifier) or identifier <= 1 or identifier not in self._card_ids:
            invalid("Silver withdrawal requires a known non-default card")
        return identifier

    def changed_counts(self, state, changes):
        self._ready()
        if (not isinstance(changes, dict) or len(changes) > len(self._card_ids)
                or any(not int32(identifier) or identifier not in self._card_ids or not int32(change) or not change
                       for identifier, change in changes.items())):
            raise ProtocolError("silver_house_changes_invalid", "Silver adjustments require known cards and nonzero counts")
        counts = {row["CardID"]: row["Count"] for row in self.read(state)}
        for identifier, change in changes.items():
            after = counts.get(identifier, 0) + change
            if after < 0 or not int32(after) or (change > 0 and after > self.cap):
                raise ProtocolError("silver_house_quantity", "Silver stock is insufficient or this deposit would exceed capacity", 409)
            counts[identifier] = after
        return counts

    def adjust(self, state, changes):
        counts = self.changed_counts(state, changes)
        result = deepcopy(state)
        result["silver_house_schema"] = 1
        result["tables"]["silver_house"] = [{"CardID": identifier, "Count": count} for identifier, count in sorted(counts.items())]
        return result

    def debit(self, state, costs):
        self._ready()
        if (not isinstance(costs, dict) or len(costs) > len(self._card_ids)
                or any(not int32(identifier) or identifier not in self._card_ids or not int32(count) or count <= 0
                       for identifier, count in costs.items())):
            raise ProtocolError("silver_house_cost_invalid", "Silver costs require known cards and positive quantities")
        if not costs:
            self.read(state)
            return deepcopy(state)
        return self.adjust(state, {identifier: -count for identifier, count in costs.items()})

    def material(self, card_id):
        self._ready()
        if not int32(card_id) or card_id not in self._card_ids or card_id in PRINCE_CARD_IDS:
            raise ProtocolError("silver_house_card_unknown", "Card is not a supported silver warehouse material")
        card = self.cards.card(card_id)
        if not int32(card["Rare"]) or not 0 <= card["Rare"] <= 3 or card["InitClassID"] // 100 == 0:
            raise ProtocolError("silver_house_card_ineligible", "Silver warehouse material does not meet the native entry conditions", 409)
        unit = {name: False if name in UNIT_BOOLEANS else 0 for name, _ in UNIT_FIELDS}
        unit.update(CardID=card_id, ClassID=card["InitClassID"], SkillLevel=1, OrganizationIndex=-1,
                    SettingSkillID=card["ClassLV0SkillID"], GeneralPurposeFlags=1)
        return unit

    def _level(self, unit, card):
        rarity = self._rarities[card["Rare"] + 1]
        cap = min(_integer(self._classes[unit["ClassID"]]["MaxLevel"], 1), _integer(rarity["LevelCap00"], 1))
        rare = card["Rare"]
        if rare not in self._thresholds:
            ratio = _integer(rarity["RequireExpRatio"], 1)
            self._thresholds[rare] = tuple(experience_threshold(exp, ratio) for exp in self._experience)
        return derived_level(unit["Exp"], self._thresholds[rare], cap)

    def deposit_state(self, state, revision, selection):
        self._ready()
        selection = _deposits(tuple(index for index, _ in selection), tuple(identity for _, identity in selection),
                              self.max_selected)
        player = self._validated(state)
        owned = {unit["GetIndex"]: unit for unit in player["units"]}
        organized = {row["UnitIndex"] for row in player["squads"]}
        counts, rewards, removed = Counter(), Counter(), []
        try:
            for index, identity in selection:
                unit = owned.get(index)
                if unit is None or unit["UniqueID"] != identity:
                    raise ProtocolError("silver_house_unit_missing", "Silver deposit identity does not match an owned unit", 409)
                card = self.cards.card(unit["CardID"])
                if (unit["CardID"] in PRINCE_CARD_IDS or unit["GeneralPurposeFlags"] & LOCK_FLAG or index in organized):
                    raise ProtocolError("silver_house_unit_protected", "Prince, locked and organized units cannot be deposited", 409)
                if (not int32(card["Rare"]) or not 0 <= card["Rare"] <= 3
                        or unit["ClassID"] // 100 == 0 or unit["ClassChangeCount"] > 0):
                    raise ProtocolError("silver_house_unit_ineligible", "Silver deposit requires an unevolved ordinary unit of rarity zero through three", 409)
                counts[unit["CardID"]] += 1
                removed.append(unit)
                tier = self.reward_tiers.get(card["Rare"])
                if tier is not None and self.rules.reward_count and self._level(unit, card) >= tier["minimum_level"]:
                    rewards[tier["card_id"]] += self.rules.reward_count
        except ProtocolError:
            raise
        except (KeyError, ValueError, TypeError) as error:
            raise ProtocolError("silver_house_growth_invalid", "Silver deposit growth references cannot be resolved", 503) from error
        updated = self.adjust(self.gifts.preserve_history(state, revision), counts)
        consume_units(updated["player_state"], removed)
        grants = [{"card_id": identifier, "level": 1, "count": count} for identifier, count in sorted(rewards.items())]
        primary_removed = sum(not barracks_index(unit) for unit in removed)
        self.players.require_barracks_space(state, max(0, sum(rewards.values()) - primary_removed))
        updated, added = self.players.credit_units(updated, grants)
        return updated, added, removed

    def withdraw_state(self, state, card_id, count=1):
        self._ready()
        card_id = self._withdrawable(card_id)
        if not int32(count) or count <= 0:
            invalid("Silver withdrawals take a positive number of new instances")
        self._validated(state)
        self.players.require_unit_space(state, count)
        updated = self.adjust(state, {card_id: -count})
        return self.players.credit_units(updated, [{"card_id": card_id, "level": 1, "count": count}])

    def _validated(self, state):
        player = self.players.validate(state)
        # 127 and 128 refuse a damaged warehouse before checking units or roster space.
        self.read(state)
        return player

    def _transact(self, envelope, store, selection):
        operation = envelope.profile.registration_keys[0]
        try:
            return self._commit(envelope, store, operation, selection)
        except ProtocolError as error:
            log.warning("Silver warehouse %d refused with %s; the native client %s",
                        operation, error.code, _REFUSAL_AFTERMATH)
            raise

    def _commit(self, envelope, store, operation, selection):
        key = hashlib.sha256((envelope.profile.identity + ":").encode() + envelope.xml).hexdigest()
        for _ in range(3):
            profile = self.players.authenticate(envelope, store)
            self.players.validate(profile.state)
            self.read(profile.state)
            history = self._ledger.read(profile.state, profile.revision)
            cached = self._ledger.receipt(history, key)
            if cached is not None:
                if cached["revision"] != profile.revision or cached["content"] != self.content_version:
                    raise ProtocolError("silver_house_retry_stale", "Silver state or content changed after the transaction", 409)
                try:
                    response = cached["response"].encode()
                    loads(response)
                    encode_response(envelope.profile, response)
                    return response
                except (ValueError, ProtocolError) as error:
                    raise ProtocolError("silver_house_state_invalid", "Saved silver response is invalid", 503) from error
            if self._ledger.processed(history, key):
                raise ProtocolError("silver_house_request_stale", "Silver request was already processed", 409)
            if operation == DEPOSIT:
                updated, added, removed = self.deposit_state(profile.state, profile.revision, selection)
            else:
                updated, added = self.withdraw_state(profile.state, selection)
                removed = []
            children = [self.bootstrap._system(profile)]
            if added:
                children.append(Container("PBP", (Column("A1", "S", ("UX",)),
                    Column("A2", "I", (updated["player_state"]["battle"]["UX"],)))))
            children.append(Container("PPU", tuple(Column(alias, "B" if name in UNIT_BOOLEANS else "I",
                tuple(unit[name] for unit in added)) for name, alias in UNIT_FIELDS)))
            if removed:
                children.append(Container("RML", (Column("A7", "I", tuple(unit["GetIndex"] for unit in removed)),
                    Column("AB", "I", tuple(unit["UniqueID"] for unit in removed)))))
            children.append(self.response(updated))
            self.players.validate_player(updated["player_state"])
            encode_response(self._profiles[41], self.players.unit_response(updated["player_state"]["units"]))
            self._listing(profile, updated)
            response = dumps(Container(children=tuple(children)))
            encode_response(envelope.profile, response)
            if len(response) > RESPONSE_LIMIT:
                raise ProtocolError("silver_house_response_capacity", "Silver response exceeds receipt capacity", 409)
            self._ledger.record(updated, history, {"request": key, "content": self.content_version,
                                                   "revision": profile.revision + 1, "response": response.decode()})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving silver warehouse", 409)
