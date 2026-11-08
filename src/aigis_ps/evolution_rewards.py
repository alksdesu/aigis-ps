from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from .alxml import Container
from .errors import ProtocolError
from .local_present_templates import LOCAL_TEMPLATE_CARDS
from .native_numbers import int32


LEDGER_KEY = "evolution_rewards"
LEDGER_FIELDS = {"schema_version", "reward_scope", "claimed"}
REWARD_DESCRIPTION = "第二覚醒記念報酬"


def _state_error(message):
    raise ProtocolError("evolution_rewards_state_invalid", message, 503)


class EvolutionRewards:
    def __init__(self, cards, players, card_status, presents):
        self.cards, self.players = cards, players
        self.card_status, self.presents = card_status, presents
        self._card_ids = frozenset(cards.card_ids)
        present_status = presents.status()
        player_status = players.status()
        supported = present_status["supported_templates"]
        if (not isinstance(supported, list) or len(supported) > 65535
                or any(not int32(identifier) or identifier <= 0 for identifier in supported)
                or len(set(supported)) != len(supported)):
            _state_error("Present template fingerprint is invalid")
        templates = [dict(cards.present(identifier)) for identifier in sorted(supported)]
        fingerprint = {
            "master": cards.snapshot.identifier, "client": cards.snapshot.client_hash,
            "resources": cards.resource_version, "present_asset": presents.asset_sha256,
            "templates": templates,
            "policy": {"schema_version": 1, "reward_scope": "card", "rarity": 5, "count": 1,
                       "description": REWARD_DESCRIPTION,
                       "legacy_unit_stage_minimum": 3, "legacy_status_fields": ["B3", "B4"],
                       "legacy_status_test": "nonzero", "legacy_source": "before_state",
                       "eager_state_initialization": True,
                       "no_reward_response": "empty_card_status_group",
                       "first_transition": [2, [3, 4]], "reconcile_existing_ledger": False},
        }
        self.content_version = hashlib.sha256(json.dumps(
            fingerprint, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        template_cards = {row["CardID"] for row in templates}
        self._status = {
            "available": present_status.get("ready") is True and player_status.get("ready") is True,
            "content_version": self.content_version, "policy": fingerprint["policy"],
            "verified_templates": len(templates),
            "known_template_gaps": [identifier for identifier in sorted(LOCAL_TEMPLATE_CARDS.values())
                                    if identifier in self._card_ids and identifier not in template_cards],
            "unavailable": {"presents": present_status.get("unavailable", {}),
                            "players": player_status.get("unavailable", {})},
        }

    def status(self) -> dict:
        return deepcopy(self._status)

    def read(self, state: dict) -> dict:
        if not isinstance(state, dict):
            _state_error("Evolution reward ledger requires a complete state")
        player = self.players.validate_player(state.get("player_state"))
        return self._ledger(state, player, self.card_status.read(state))

    def initialize(self, state: dict) -> dict:
        ledger = self.read(state)
        updated = deepcopy(state)
        updated[LEDGER_KEY] = ledger
        return updated

    def _ledger(self, state, player, status_rows):
        if LEDGER_KEY not in state:
            attained = {unit["CardID"] for unit in player["units"] if unit["ClassChangeCount"] >= 3}
            attained.update(row["CardID"] for row in status_rows if row["B3"] != 0 or row["B4"] != 0)
            return {"schema_version": 1, "reward_scope": "card", "claimed": sorted(attained)}
        ledger = state[LEDGER_KEY]
        if (not isinstance(ledger, dict) or set(ledger) != LEDGER_FIELDS
                or type(ledger["schema_version"]) is not int or ledger["schema_version"] != 1
                or ledger["reward_scope"] != "card"):
            _state_error("Evolution reward ledger schema is unsupported")
        claimed = ledger["claimed"]
        if (not isinstance(claimed, list) or len(claimed) > len(self._card_ids)
                or any(not int32(identifier) or identifier not in self._card_ids for identifier in claimed)
                or claimed != sorted(set(claimed))):
            _state_error("Evolution reward claims must be sorted distinct known CardIDs")
        return deepcopy(ledger)

    def apply(self, before_state: dict, candidate_state: dict, card_id: int) -> tuple[dict, tuple[Container, ...]]:
        if not isinstance(before_state, dict) or not isinstance(candidate_state, dict):
            _state_error("Evolution rewards require complete before and candidate states")
        card = self.cards.card(card_id)
        if not int32(card.get("Rare")) or card["Rare"] < 0:
            _state_error("Evolution reward rarity is unavailable")
        before_player = self.players.validate_player(before_state.get("player_state"))
        originals = [unit for unit in before_player["units"] if unit["CardID"] == card_id]
        if not originals:
            raise ProtocolError("evolution_reward_target_invalid", "Reward CardID is not owned in the original state", 409)
        updated = deepcopy(candidate_state)
        player = self.players.validate_player(updated.get("player_state"))
        if card["Rare"] != 5:
            return updated, (Container("CardStatus"),)
        if ((LEDGER_KEY in before_state) != (LEDGER_KEY in updated)
                or before_state.get(LEDGER_KEY) != updated.get(LEDGER_KEY)):
            _state_error("Candidate changed the evolution reward ledger outside the reward transaction")
        status_rows = self.card_status.read(before_state)
        self.card_status.read(updated)
        ledger = self._ledger(before_state, before_player, status_rows)
        updated[LEDGER_KEY] = ledger
        if card_id in ledger["claimed"]:
            return updated, (Container("CardStatus"),)
        after_units = {(unit["GetIndex"], unit["UniqueID"]): unit for unit in player["units"]}
        if not any(unit["ClassChangeCount"] == 2
                   and (after := after_units.get((unit["GetIndex"], unit["UniqueID"]))) is not None
                   and after["CardID"] == card_id and after["ClassChangeCount"] in (3, 4)
                   for unit in originals):
            raise ProtocolError("evolution_reward_transition_invalid", "First reward requires the same unit to complete second awakening", 409)
        template = self.presents.initial_unit_template(card_id)
        if not int32(template) or template <= 0 or self.cards.present(template)["CardID"] != card_id:
            raise ProtocolError("evolution_reward_template_invalid", "Initial reward template does not match the original CardID", 503)
        updated, mail = self.presents.enqueue(updated, [{
            "kind": "unit", "template_id": template, "count": 1, "description": REWARD_DESCRIPTION,
        }])
        status = next((row for row in self.card_status.read(updated) if row["CardID"] == card_id), None)
        updated = self.card_status.update(updated, [{"CardID": card_id, "A1": status["A1"] if status else 0}])
        updated[LEDGER_KEY]["claimed"] = sorted([*ledger["claimed"], card_id])
        response = (self.presents.response(updated, tuple(row["PresentID"] for row in mail)),
                    self.card_status.response(updated, (card_id,)))
        return updated, response
