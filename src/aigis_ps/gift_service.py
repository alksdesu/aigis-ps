from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import logging
import math
import re

from .alxml import Column, Container, dumps
from .card_status import FIRST_EPISODE, SECOND_EPISODE
from .config_state import configuration_delta, configuration_row, patch_configuration
from .energy_clock import instant
# Native 6ECF50 continues to CompleteWaitDoGift only after A7FDF0 returns on a 200; 66E8B0 resends refused bodies.
from .errors import REFUSAL_AFTERMATH as _REFUSAL_AFTERMATH, REFUSAL_POLICY, ProtocolError
from .gift_protocol import GIFT_MENU_GENDERS, GIFT_OPERATIONS, parse_gift
from .native_numbers import INT32_MAX, int32
from .player_state import GIFT_TUTORIAL_FLAG
from .presents import PRESENT_LIMITS
from .protocol import ProtocolRegistry, RequestEnvelope, encode_response
from .request_ledger import HISTORY_LIMIT, LEGACY_SCHEMA_VERSION, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .storage import Store


RESPONSE_CACHE_LIMIT = 256 * 1024
CLAIM_TIERS = frozenset((1, 100, 150))
CATEGORIES = {(0, False): 1886645389, (1, False): 1829622658,
              (0, True): 1638469264, (1, True): 789107626}
JOURNAL_SCHEMA_VERSION = 2
LEGACY_JOURNAL_SCHEMA_VERSION = 1
HISTORY_FIELD = "history"
JOURNAL_FIELDS = frozenset(("schema_version", "reward_scope", "claims", HISTORY_FIELD))
LEGACY_JOURNAL_FIELDS = frozenset(("schema_version", "reward_scope", "claims", "last_client_time", "seen_keys", HISTORY_FIELD))
LEGACY_ENTRY_FIELDS = frozenset(("key", "request_key", "client_time", "response_revision", "committed_at",
                                 "content_version", "response"))
_CARD = re.compile(r"[1-9][0-9]{0,9}\Z")
log = logging.getLogger("aigis_ps")


def _integer(value, minimum=0):
    return int32(value) and value >= minimum


def _state_error(message):
    raise ProtocolError("gift_state_invalid", message, 503)


def _conflict(code, message):
    raise ProtocolError(code, message, 409)


def _receipt_commit_time_valid(entry):
    return type(entry["committed_at"]) in (int, float) and math.isfinite(entry["committed_at"])


class GiftService:
    def __init__(self, bootstrap, players, possessions, card_status, presents, catalog, aliases,
                 *, clock=None, replay_seconds=300):
        if (clock is not None and not callable(clock)) or type(replay_seconds) is not int or not 1 <= replay_seconds <= 86400:
            raise ValueError("Gift replay requires a clock and bounded positive lifetime")
        self.bootstrap, self.players, self.possessions = bootstrap, players, possessions
        self.card_status, self.presents, self.catalog, self.aliases = card_status, presents, catalog, aliases
        self.clock, self.replay_seconds = bootstrap.clock if clock is None else clock, replay_seconds
        self.missions = None
        registry = ProtocolRegistry.load()
        self._unit_profile, self._configuration_profile = registry.by_registration[41], registry.by_registration[137]
        self._present_profile = registry.by_registration[42]
        content = {"master": catalog.snapshot.identifier, "client": catalog.snapshot.client_hash,
                   "resources": catalog.resource_version, "gifts": catalog.status()["resource_sha256"],
                   "aliases": aliases.status()["resource_sha256"],
                   "presents": presents.asset_sha256, "possessions": possessions.asset_sha256}
        self.content_version = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        self._ledger = RequestLedger(HISTORY_FIELD, "gift_state_invalid", "Gift replay history is invalid",
                                     fields=("request", "content", "revision", "response", "committed_at"),
                                     response_limit=RESPONSE_CACHE_LIMIT, check=_receipt_commit_time_valid)

    def handlers(self):
        return dict.fromkeys(GIFT_OPERATIONS, self.send)

    def status(self):
        return {"implemented_operations": sorted(GIFT_OPERATIONS),
                "native_exclusions": ["Operation 99, which both native gift item menus override with 100 or 101"],
                "official_server_rules": False,
                "content_version": self.content_version, "reward_scope": "original CardID and reward tier",
                "migration_policy": "Existing Love, first-two-episode history and known event stages form a conservative baseline; no backfill",
                "overflow_policy": "Consume selected quantities and clamp Love to this operation's 100/150 scope",
                "reward_policy": "All trust rewards enter the present box; no direct units or wallet credit",
                "event_policy": "Advance WatchLoveEv only; unlock first two character episodes, never infer the third",
                "tutorial_policy": "A successful gift persists MenuTutorialFlags bit 0",
                "replay_seconds": self.replay_seconds, "history_limit": HISTORY_LIMIT,
                "processed_request_limit": PROCESSED_LIMIT,
                "replay_policy": "Exact body replays only at its committed revision, content version and lifetime",
                "request_order": REQUEST_ORDER,
                "refusals": REFUSAL_POLICY,
                "timestamp_limit": "Identical bodies within one native CT second cannot distinguish another gift from retry",
                "unsupported": ["nonzero ITEM", "hidden gift categories", "nonzero gift bonus rules"]}

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime):
            raise ValueError("Gift clock must return datetime")
        return instant(value).timestamp()

    def read(self, state, revision):
        player = self.players.validate_player(state.get("player_state"))
        return self._journal(state, player, revision)

    def preserve_history(self, state, revision):
        journal = self.read(state, revision)
        result = deepcopy(state)
        result["gift_state"] = journal
        return result

    def record_current_thresholds(self, state, get_index, revision):
        result = self.preserve_history(state, revision)
        if not _integer(get_index):
            raise ProtocolError("gift_unit_missing", "Reward history requires a valid owned unit identity")
        target = next((row for row in result["player_state"]["units"] if row["GetIndex"] == get_index), None)
        if target is None:
            raise ProtocolError("gift_unit_missing", "Reward history target is not owned", 409)
        card = self._card(target["CardID"])
        if card["BonusType"]:
            self._new_rewards(result["gift_state"], target, card, target["Love"])
        return result

    def _card(self, card_id):
        card = self.catalog.card(card_id)
        fields = ("Rare", "LoveEv1", "BonusType", "BonusType3")
        if any(not _integer(card.get(field)) for field in fields):
            _state_error("Gift card is missing verified trust attributes")
        if card["BonusType"] and not 0 < card["LoveEv1"] < 100:
            _state_error("Gift card has an unsupported first-event threshold")
        return card

    def _rewards(self, card):
        result = tuple(reward for reward in self.catalog.rewards if reward.rarity == card["Rare"])
        if any(reward.condition not in CLAIM_TIERS for reward in result):
            raise ProtocolError("gift_reward_unsupported", "A trust reward threshold is not supported", 503)
        return result

    @staticmethod
    def _threshold(card, reward):
        return card["LoveEv1"] if reward.condition == 1 else reward.condition

    def _journal(self, state, player, revision):
        if "gift_state" not in state:
            claims = {}
            for row in self.card_status.read(state):
                attained = ({1} if row["A7"] & FIRST_EPISODE else set()) | ({100} if row["A7"] & SECOND_EPISODE else set())
                if attained:
                    claims[str(row["CardID"])] = sorted(attained)
            for unit in player["units"]:
                card = self._card(unit["CardID"])
                if not card["BonusType"]:
                    continue
                watch = unit["WatchLoveEv"] if _integer(unit["WatchLoveEv"]) and unit["WatchLoveEv"] <= 3 else 0
                attained = {tier for tier, threshold, stage in ((1, card["LoveEv1"], 1), (100, 100, 2), (150, 150, 3))
                            if unit["Love"] >= threshold or watch >= stage}
                if attained:
                    key = str(unit["CardID"])
                    claims[key] = sorted(set(claims.get(key, ())) | attained)
            return {"schema_version": JOURNAL_SCHEMA_VERSION, "reward_scope": "card", "claims": claims,
                    HISTORY_FIELD: self._ledger.read({}, revision)}
        journal = state["gift_state"]
        if (isinstance(journal, dict) and set(journal) == LEGACY_JOURNAL_FIELDS
                and type(journal["schema_version"]) is int and journal["schema_version"] == LEGACY_JOURNAL_SCHEMA_VERSION):
            journal = self._ledger_journal(journal)
        if (not isinstance(journal, dict) or set(journal) != JOURNAL_FIELDS
                or type(journal["schema_version"]) is not int or journal["schema_version"] != JOURNAL_SCHEMA_VERSION
                or journal["reward_scope"] != "card"):
            _state_error("Gift journal schema is unsupported")
        claims = journal["claims"]
        if not isinstance(claims, dict) or len(claims) > 65535:
            _state_error("Gift claim ledger is invalid")
        for key, tiers in claims.items():
            if (not isinstance(key, str) or not _CARD.fullmatch(key) or int(key) > INT32_MAX
                    or not isinstance(tiers, list) or len(tiers) > len(CLAIM_TIERS)
                    or any(type(tier) is not int or tier not in CLAIM_TIERS for tier in tiers)
                    or len(set(tiers)) != len(tiers)):
                _state_error("Gift claim ledger contains invalid card or tier keys")
        return {"schema_version": JOURNAL_SCHEMA_VERSION, "reward_scope": "card", "claims": deepcopy(claims),
                HISTORY_FIELD: self._ledger.read(journal, revision)}

    @staticmethod
    def _ledger_journal(journal):
        # Version 1 kept its replay entries beside a client-time watermark that the shared ledger upgrades.
        entries = journal[HISTORY_FIELD]
        if (not isinstance(entries, list)
                or any(not isinstance(entry, dict) or set(entry) != LEGACY_ENTRY_FIELDS
                       or entry["key"] != GiftService._entry_key(entry["request_key"], entry["response_revision"],
                                                                 entry["content_version"])
                       for entry in entries)):
            _state_error("Gift replay history is invalid")
        receipts = [{"request": entry["request_key"], "content": entry["content_version"],
                     "revision": entry["response_revision"], "response": entry["response"],
                     "committed_at": entry["committed_at"], "client_time": entry["client_time"]} for entry in entries]
        return {"schema_version": JOURNAL_SCHEMA_VERSION, "reward_scope": journal["reward_scope"], "claims": journal["claims"],
                HISTORY_FIELD: {"schema_version": LEGACY_SCHEMA_VERSION, "receipts": receipts,
                                "last_client_time": journal["last_client_time"], "seen_keys": journal["seen_keys"]}}

    @staticmethod
    def _entry_key(request_key, revision, content):
        return hashlib.sha256(f"{request_key}:{revision}:{content}".encode()).hexdigest()

    def _retry(self, envelope, profile, history, key, now):
        receipt = self._ledger.receipt(history, key)
        if receipt is None:
            if self._ledger.processed(history, key):
                _conflict("gift_request_stale", "This gift request was already processed")
            return None
        if receipt["content"] != self.content_version:
            _conflict("gift_content_changed", "Gift content changed after this request")
        if receipt["revision"] != profile.revision:
            _conflict("gift_response_stale", "Player data changed after this gift; reload current data")
        if not 0 <= now - receipt["committed_at"] <= self.replay_seconds:
            _conflict("gift_response_expired", "The gift retry lifetime has expired")
        response = receipt["response"].encode("utf-8")
        encode_response(envelope.profile, response)
        return response

    def _target(self, request, player):
        target = next((unit for unit in player["units"] if unit["GetIndex"] == request.unit_index), None)
        if target is None or target["UniqueID"] != request.unique_id:
            _conflict("gift_target_mismatch", "Gift target does not match an owned unit instance")
        card = self._card(target["CardID"])
        if not card["BonusType"]:
            raise ProtocolError("gift_target_ineligible", "This unit has no native trust parameter")
        if target["Love"] > 150 or not _integer(target["WatchLoveEv"]) or target["WatchLoveEv"] > 3:
            _state_error("Gift target has unsupported trust progression")
        alias = self.aliases.resolve(target["CardID"], rating="R18")
        expected = alias.image_card_id if alias.image_card_id != target["CardID"] else 0
        if request.alternative != expected:
            _conflict("gift_alternative_mismatch", "Gift image identity does not match the verified card alias")
        gender = self.catalog.card(alias.image_card_id).get("Kind")
        if type(gender) is not int or gender not in GIFT_MENU_GENDERS.values():
            raise ProtocolError("gift_gender_unsupported", "Gift image card has an unsupported gender", 503)
        if gender != GIFT_MENU_GENDERS[request.registration]:
            raise ProtocolError("gift_gender_mismatch", "Gift menu does not match the effective card gender")
        return target, card, gender

    def _selection(self, request, target, card, gender):
        if request.single_item != 0:
            raise ProtocolError("gift_single_item_unsupported", "Nonzero ITEM has no verified current menu behavior", 501)
        advanced = bool(card["BonusType3"]) and target["Love"] >= 100
        maximum, minimum = (150, 100) if advanced else (100, 0)
        if target["Love"] >= maximum:
            _conflict("gift_trust_maximum", "This unit has reached its available trust limit")
        costs, gain = {}, 0
        for selected in request.selections:
            if not selected.count:
                continue
            gift = self.catalog.gift(selected.item)
            if gift.item_id in costs:
                raise ProtocolError("gift_duplicate_item", "One gift item cannot occupy multiple selected slots")
            if (gift.category != CATEGORIES[gender, advanced] or gift.gender != gender
                    or (card["Rare"] not in gift.rarities and -1 not in gift.rarities)):
                raise ProtocolError("gift_item_ineligible", "Gift category, gender or rarity does not match this unit")
            if (gift.bonus_ratio or gift.bonus_value or gift.add_value <= 0
                    or (gift.minimum, gift.maximum) != (minimum, maximum)):
                raise ProtocolError("gift_rule_unsupported", "Gift growth or range differs from the supported local rule", 501)
            if not gift.minimum <= target["Love"] < gift.maximum:
                raise ProtocolError("gift_scope_mismatch", "Gift is outside the unit's current trust interval")
            costs[gift.item_id] = selected.count
            gain += gift.add_value * selected.count
        if not costs:
            raise ProtocolError("gift_selection_empty", "Select at least one positive gift quantity")
        return costs, min(maximum, target["Love"] + gain)

    def _new_rewards(self, journal, target, card, love):
        key = str(target["CardID"])
        claims = set(journal["claims"].get(key, ()))
        rewards, grants = [], []
        for reward in self._rewards(card):
            threshold = self._threshold(card, reward)
            if target["Love"] >= threshold:
                claims.add(reward.condition)
            if reward.condition in claims or not target["Love"] < threshold <= love:
                continue
            if reward.reward_type == 1:
                template = int(reward.reward_id)
                if template not in (2, 3, 4, 5):
                    raise ProtocolError("gift_reward_unsupported", "Trust unit reward uses an unsupported present template", 501)
                grants.append({"kind": "unit", "template_id": template, "count": reward.amount, "description": "交流達成報酬"})
            elif reward.reward_type == 0 and reward.reward_id == "":
                grants.append({"kind": "free_crystal", "amount": reward.amount, "description": "交流達成報酬"})
            else:
                raise ProtocolError("gift_reward_unsupported", "Trust reward type is not supported", 501)
            rewards.append(reward)
        claims.update(reward.condition for reward in rewards)
        if claims:
            journal["claims"][key] = sorted(claims)
        return rewards, grants

    def send(self, envelope: RequestEnvelope, store: Store) -> bytes:
        try:
            return self._send(envelope, store)
        except ProtocolError as error:
            log.warning("Gift %d refused with %s; the native client %s",
                        envelope.profile.registration_keys[0], error.code, _REFUSAL_AFTERMATH)
            raise

    def _send(self, envelope, store):
        request = parse_gift(envelope)
        key = hashlib.sha256(f"{envelope.profile.identity}:{envelope.profile.route}:{envelope.digest}".encode()).hexdigest()
        now = self._now()
        mission_now = self.bootstrap.clock() if self.missions is not None else None
        for _ in range(3):
            profile = self.bootstrap._authenticate(envelope, store)
            player = self.players.validate(profile.state)
            journal = self._journal(profile.state, player, profile.revision)
            cached = self._retry(envelope, profile, journal[HISTORY_FIELD], key, now)
            if cached is not None:
                return cached
            target, card, gender = self._target(request, player)
            costs, love = self._selection(request, target, card, gender)
            rewards, grants = self._new_rewards(journal, target, card, love)
            updated = self.possessions.debit(profile.state, costs)
            unit = next(row for row in updated["player_state"]["units"] if row["GetIndex"] == target["GetIndex"])
            attained = 3 if love >= 150 else 2 if love >= 100 else 1 if love >= card["LoveEv1"] else 0
            unit.update(Love=love, WatchLoveEv=max(target["WatchLoveEv"], attained))
            old_status = self.card_status.read(updated)
            old_row = next((row for row in old_status if row["CardID"] == target["CardID"]), None)
            episodes = FIRST_EPISODE | SECOND_EPISODE if love >= 100 else FIRST_EPISODE if love >= card["LoveEv1"] else 0
            flags = (old_row["A7"] if old_row else 0) | episodes
            updated = self.card_status.update(updated, [{"CardID": target["CardID"], "A7": flags}])
            updated, new_mail = self.presents.enqueue(updated, grants)
            old_tutorial = configuration_row(updated)["MenuTutorialFlags"]
            updated = patch_configuration(updated, {"MenuTutorialFlags": old_tutorial | GIFT_TUTORIAL_FLAG})
            updated["gift_state"] = journal
            if self.missions is not None:
                updated = self.missions.record(updated, "gift", {"before": target, "after": unit}, mission_now)
                journal = updated["gift_state"]
            self.players.validate_player(updated["player_state"])
            encode_response(self._unit_profile, self.players.unit_response(updated["player_state"]["units"]))
            encode_response(self._configuration_profile, self.bootstrap._configuration_response(replace(profile, state=updated)))
            encode_response(self._present_profile, dumps(Container(children=(self.bootstrap._system(profile),
                            self.presents.response(updated))), limits=PRESENT_LIMITS))
            children = [self.bootstrap._system(profile),
                        Container("PPU", (Column("A7", "I", (target["GetIndex"],)), Column("A5", "I", (love,)),
                                          Column("A9", "I", (unit["WatchLoveEv"],)))),
                        self.possessions.response(updated, costs),
                        Container("TrustReward", (Column("ID", "I", tuple(reward.id for reward in rewards)),))]
            if old_row is None or old_row["A7"] != flags:
                children.append(self.card_status.response(updated, [target["CardID"]]))
            if new_mail:
                children.append(self.presents.response(updated, [row["PresentID"] for row in new_mail]))
            if not old_tutorial & GIFT_TUTORIAL_FLAG:
                children.append(configuration_delta(configuration_row(updated), ("MenuTutorialFlags",)))
            response = dumps(Container(children=tuple(children)))
            encode_response(envelope.profile, response)
            if len(response) > RESPONSE_CACHE_LIMIT:
                raise ProtocolError("gift_response_too_large", "Gift response exceeds the persisted replay limit", 503)
            self._ledger.record(journal, journal[HISTORY_FIELD], {"request": key, "content": self.content_version,
                                                                  "revision": profile.revision + 1,
                                                                  "response": response.decode("utf-8"), "committed_at": now})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        _conflict("profile_changed", "Player data changed while saving the gift")
