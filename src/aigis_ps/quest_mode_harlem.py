from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import threading
from types import MappingProxyType
import weakref

from .card_status import THIRD_EPISODE
from .errors import ProtocolError
from .native_numbers import int32
from .quest_catalog import TREASURE_SLOTS
from .quest_modes import (
    MissionWindow, ModePolicy, ModeServices, SettleContext, SettleOutcome, StartContext, quest_cleared,
    require_row_values,
)


HARLEM_TYPE = 10
NO_HARD_LEVEL = 0
SPIRIT_REWARD_TYPE = 3
NO_TREASURE = 0
ROW_VALUES = MappingProxyType({"Type": HARLEM_TYPE, "_HardLevel": NO_HARD_LEVEL, "BonusType": SPIRIT_REWARD_TYPE,
                               **{f"Treasure{slot}": NO_TREASURE for slot in range(1, TREASURE_SLOTS + 1)}})
PERSON_ARCHIVE = "pevconfig.aar"
PERSON_TABLE = "EventPersonConfig03.atb"
KIND_TRAIT = "Kind"
FEMALE_KIND = 1
# CMenuPersonEventFemaleUnitSelect::IsAppend lists units with GetClassChange() >= 2; its platform locks are constant false.
AWAKENED_STAGE = 2


@dataclass(frozen=True)
class PersonConfig:
    cards: Mapping[int, tuple[int, ...]]
    quests: Mapping[int, int]
    conditional: frozenset[int]
    females: frozenset[int]

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping], females: Iterable[int]) -> PersonConfig:
        cards, quests, conditional = {}, {}, set()
        for row in rows:
            card_id, quest_id = row.get("_CardID"), row.get("_QuestID")
            if not int32(card_id) or card_id <= 0 or not int32(quest_id) or quest_id <= 0:
                raise ProtocolError("quest_mode_resource_invalid",
                                    "EventPersonConfig03 rows require positive card and quest identifiers", 503)
            cards.setdefault(quest_id, []).append(card_id)
            # 9E0E90 keys the rows by _CardID, so a later row for the same card replaces the earlier quest.
            quests[card_id] = quest_id
            if row.get("_AppearQuestID", 0) or row.get("_AppearExpression") not in (None, ""):
                conditional.add(quest_id)
        return cls(MappingProxyType({key: tuple(value) for key, value in cards.items()}),
                   MappingProxyType(quests), frozenset(conditional), frozenset(females))

    def card(self, quest_id: int) -> int:
        cards = self.cards.get(quest_id, ())
        if len(cards) != 1 or self.quests[cards[0]] != quest_id:
            raise ProtocolError("quest_mode_resource_invalid", "EventPersonConfig03 must pair the quest with exactly one card", 503)
        if quest_id in self.conditional:
            raise ProtocolError("quest_condition_unsupported", "Harlem appearance conditions are not implemented", 501)
        if cards[0] not in self.females:
            raise ProtocolError("quest_mode_resource_invalid", "The paired harlem card must be a known female card", 503)
        return cards[0]


def _owns_awakened(state, card_id: int) -> bool:
    player = state.get("player_state") if isinstance(state, Mapping) else None
    units = player.get("units") if isinstance(player, Mapping) else None
    if not isinstance(units, list) or not all(isinstance(unit, Mapping) for unit in units):
        raise ProtocolError("player_units_invalid", "Harlem starts require the saved unit roster", 503)
    return any(unit.get("CardID") == card_id and int32(unit.get("ClassChangeCount"))
               and unit["ClassChangeCount"] >= AWAKENED_STAGE for unit in units)


class HarlemPolicy(ModePolicy):
    name = "harlem"
    quest_types = (HARLEM_TYPE,)
    enabled = True
    # 9B76C0 greys out the harlem menu until main quest 1003 is cleared.
    gate_quest = 1003

    def __init__(self):
        self._lock = threading.Lock()
        self._persons = weakref.WeakKeyDictionary()

    def _paired_card(self, catalog, quest_id: int) -> int:
        with self._lock:
            persons = self._persons.get(catalog)
        if persons is None:
            persons = PersonConfig.from_rows(catalog.archive_rows(PERSON_ARCHIVE, PERSON_TABLE),
                                             catalog.card_ids_with(KIND_TRAIT, FEMALE_KIND))
            with self._lock:
                persons = self._persons.setdefault(catalog, persons)
        return persons.card(quest_id)

    @staticmethod
    def _require_card_status(services: ModeServices):
        if services.card_status is None:
            raise ProtocolError("quest_card_status_unavailable", "Harlem first clears require the card status service", 409)
        return services.card_status

    def validate_row(self, row, catalog) -> None:
        require_row_values(row, ROW_VALUES, "Harlem quest")
        unit_list = row.get("UnitList")
        if not int32(unit_list) or unit_list <= 0 or not catalog.fixed_units(unit_list):
            raise ProtocolError("quest_definition_invalid", "Harlem quests require their fixed native team", 503)
        # star_rewards refuses a BonusNum that names no known card.
        catalog.star_rewards(row, False)
        self._paired_card(catalog, row.get("QuestID"))

    def check_start(self, context: StartContext) -> MissionWindow:
        mission = super().check_start(context)
        # Only the first clear writes CardStatus, so repeat clears start without the service.
        if not quest_cleared(context.records, context.quest.quest_id):
            self._require_card_status(context.services)
        card_id = self._paired_card(context.catalog, context.quest.quest_id)
        if not _owns_awakened(context.state, card_id):
            raise ProtocolError("quest_locked", "Harlem quests require an awakened unit of their paired card", 409)
        return mission

    def settle(self, context: SettleContext) -> SettleOutcome:
        if not context.first_clear:
            return SettleOutcome(context.state)
        status = self._require_card_status(context.services)
        card_id = self._paired_card(context.catalog, context.quest.quest_id)
        row = next((row for row in status.read(context.state) if row["CardID"] == card_id), None)
        if row is not None and row["A7"] & THIRD_EPISODE:
            return SettleOutcome(context.state)
        state = status.update(context.state, [{"CardID": card_id, "A7": (row["A7"] if row else 0) | THIRD_EPISODE}])
        return SettleOutcome(state, (status.response(state, [card_id]),))


POLICY = HarlemPolicy()
