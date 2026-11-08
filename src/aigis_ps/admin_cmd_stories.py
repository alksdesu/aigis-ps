from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated

from pydantic import AfterValidator, Field, model_validator

from .admin_cmd_settings import SETTINGS
from .admin_command import (AdminCommand, CommandContext, CommandParams, CommandResult, counted, require_distinct,
                            required_service, requires)
from .admin_fields import GIFT_LEDGER, GIFTS, STORY_BITS
from .admin_registry import CommandRegistry
from .card_status import FIRST_EPISODE, MAX_CARD_STATUS_ROWS, SECOND_EPISODE


CARD_STATUS = "card_status"
STORY_FIELD = "A7"
STORY_WRITES = frozenset({f"{CARD_STATUS}.{STORY_FIELD}"})
# Without a gift ledger, the first gift builds its claimed tiers from these two bits.
GIFT_EPISODES = FIRST_EPISODE | SECOND_EPISODE
STORY_INDICES = tuple(bit.index for bit in STORY_BITS.bits)
UNLOCK, CLEAR = "UNLOCK", "CLEAR"
GIFT_SERVICE_REASON = ("Story bits 0 and 1 seed the trust reward baseline until the gift ledger exists, "
                       "so changing them needs the gift service")


def _open_bit(index: int) -> int:
    if index not in STORY_INDICES:
        raise ValueError(f"Only story bits {', '.join(map(str, STORY_INDICES))} can change")
    return index


StoryBit = Annotated[int, AfterValidator(_open_bit), Field(json_schema_extra={"enum": list(STORY_INDICES)})]


def mask(indices: Iterable[int]) -> int:
    result = 0
    for index in indices:
        result |= 1 << index
    return result


def story_bits(value: int) -> list[int]:
    return [index for index in STORY_INDICES if value >> index & 1]


class StoryChange(CommandParams):
    CardID: int
    set_bits: list[StoryBit] = Field(default_factory=list, max_length=len(STORY_INDICES))
    clear_bits: list[StoryBit] = Field(default_factory=list, max_length=len(STORY_INDICES))

    @model_validator(mode="after")
    def _consistent(self) -> StoryChange:
        require_distinct(self.set_bits, "Bits to set")
        require_distinct(self.clear_bits, "Bits to clear")
        if not self.set_bits and not self.clear_bits:
            raise ValueError("A story change sets or clears at least one bit")
        if set(self.set_bits) & set(self.clear_bits):
            raise ValueError("A story bit cannot be set and cleared at once")
        return self

    def apply_to(self, value: int) -> int:
        return (value | mask(self.set_bits)) & ~mask(self.clear_bits)


class StoryParams(CommandParams):
    cards: list[StoryChange] = Field(min_length=1, max_length=MAX_CARD_STATUS_ROWS)

    @model_validator(mode="after")
    def _distinct(self) -> StoryParams:
        require_distinct((card.CardID for card in self.cards), "CardIDs")
        return self


def set_unlock(state: dict, params: StoryParams, context: CommandContext) -> CommandResult:
    status = required_service(context, CARD_STATUS)
    stored = {row["CardID"]: row[STORY_FIELD] for row in status.read(state)}
    fresh = [change.CardID for change in params.cards if change.CardID not in stored]
    if fresh:
        # Only update checks CardData identities, so cards without a row are checked on a discarded copy first.
        status.update(state, [{"CardID": identifier, STORY_FIELD: 0} for identifier in fresh])
    cards, patches = [], []
    for change in params.cards:
        before = stored.get(change.CardID, 0)
        after = change.apply_to(before)
        cards.append({"CardID": change.CardID, "before": before, "after": after,
                      "set": story_bits(after & ~before), "cleared": story_bits(before & ~after)})
        if after != before:
            patches.append({"CardID": change.CardID, STORY_FIELD: after})
    frozen = GIFT_LEDGER not in state and any((card["before"] ^ card["after"]) & GIFT_EPISODES for card in cards)
    if frozen:
        # Trust tiers the save reached are frozen before the edit, so story bits can neither forfeit nor reopen them.
        state = required_service(context, GIFTS, GIFT_SERVICE_REASON).preserve_history(state, context.revision)
    return CommandResult(status.update(state, patches), {"cards": cards, "gift_baseline_frozen": frozen})


def clears_story(params: StoryParams) -> bool:
    # Setting a bit only opens stories, so only a clear can take progress away.
    return any(card.clear_bits for card in params.cards)


def story_phrase(params: StoryParams) -> str:
    verb = CLEAR if clears_story(params) else UNLOCK
    return f"{verb} STORIES {counted(len(params.cards), 'CARD')}"


STORIES = AdminCommand("stories.set_unlock", SETTINGS, StoryParams, set_unlock, frozenset({CARD_STATUS, GIFTS}),
                       writes=STORY_WRITES, destructive=clears_story, confirm_phrase=story_phrase,
                       unavailable=requires(CARD_STATUS))
COMMANDS = (STORIES,)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
