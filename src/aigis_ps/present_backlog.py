from __future__ import annotations

import logging
from types import MappingProxyType

from .errors import ProtocolError
from .native_numbers import INT32_MAX, int32
from .presents import MAX_PRESENTS, PRESENT_CAPACITY_ERRORS
from .protocol import NATIVE_CELL_BYTES


# Distinct waiting rewards never need to outnumber the presents the mailbox itself can hold.
MAX_PENDING_PRESENTS = MAX_PRESENTS
MAX_PRESENT_TEXT_BYTES = NATIVE_CELL_BYTES
# Journals saved under the earlier 256-byte bound keep loading; only new battle presents must fit the native cell.
STORED_PRESENT_TEXT_BYTES = 256
# presents.enqueue checks capacity before the reward itself, so only an empty-mailbox preview proves it can ever arrive.
EMPTY_MAILBOX = 0
# presents.enqueue refuses a longer reward list as invalid instead of reporting a full mailbox.
MAX_BATCH = MAX_PRESENTS
UNBOUNDED_PRESENT = "quest_present_unsupported"
REWARD_FIELDS = MappingProxyType({
    "free_crystal": frozenset(("kind", "amount", "description")),
    "gold": frozenset(("kind", "amount", "description")),
    "item": frozenset(("kind", "amount", "item_id", "description")),
    "legacy_item": frozenset(("kind", "amount", "present_type", "description")),
    "unit": frozenset(("kind", "template_id", "count", "description")),
})
TEXT_FIELDS = frozenset(("description", "item_id"))
ENTRY_FIELDS = frozenset(("reward", "copies"))
log = logging.getLogger("aigis_ps")


def _positive_int32(value) -> bool:
    return int32(value) and value > 0


def _bounded_text(value, limit: int) -> bool:
    try:
        return type(value) is str and 0 < len(value.encode("utf-8")) <= limit
    except UnicodeError:
        return False


def _reward_fits(reward, text_bytes: int) -> bool:
    if not isinstance(reward, dict) or type(reward.get("kind")) is not str:
        return False
    fields = REWARD_FIELDS.get(reward["kind"])
    return (fields is not None and set(reward) == fields
            and all(_bounded_text(reward[name], text_bytes) if name in TEXT_FIELDS else _positive_int32(reward[name])
                    for name in fields - {"kind"}))


def reward_valid(reward) -> bool:
    return _reward_fits(reward, MAX_PRESENT_TEXT_BYTES)


def require_bounded(reward) -> None:
    if not reward_valid(reward):
        raise ProtocolError(UNBOUNDED_PRESENT, "Battle presents must keep a bounded journal form", 503)


def require_deliverable(presents, reward) -> None:
    # Settlement queues only rewards that fit the native cells, which an empty-mailbox preview does not check.
    require_bounded(reward)
    presents.preview([reward], EMPTY_MAILBOX)


def _entry(reward, copies) -> dict:
    return {"reward": dict(reward), "copies": copies}


class PresentBacklog:
    def __init__(self, presents, entries=()):
        self.presents = presents
        self._entries = [_entry(entry["reward"], entry["copies"]) for entry in entries]
        self._blocked = False

    @staticmethod
    def valid(entries) -> bool:
        return (isinstance(entries, list) and len(entries) <= MAX_PENDING_PRESENTS
                and all(isinstance(entry, dict) and set(entry) == ENTRY_FIELDS
                        and _reward_fits(entry["reward"], STORED_PRESENT_TEXT_BYTES) and _positive_int32(entry["copies"])
                        for entry in entries)
                and len({frozenset(entry["reward"].items()) for entry in entries}) == len(entries))

    @property
    def entries(self) -> list[dict]:
        return [_entry(entry["reward"], entry["copies"]) for entry in self._entries]

    def flush(self, state: dict) -> tuple[dict, list[int]]:
        if self.presents is None:
            return state, []
        delivered, waiting = [], []
        for entry in self._entries:
            mailed = 0
            if not self._blocked:
                state, present_ids, mailed = self._mail(state, entry["reward"], entry["copies"])
                delivered.extend(present_ids)
            if mailed < entry["copies"]:
                waiting.append(_entry(entry["reward"], entry["copies"] - mailed))
        self._entries = waiting
        return state, delivered

    def send(self, state: dict, reward: dict) -> tuple[dict, list[int], bool]:
        if self.presents is None:
            raise ProtocolError("quest_present_unavailable", "Battle presents require the present service", 409)
        require_bounded(reward)
        try:
            self.presents.preview([reward], EMPTY_MAILBOX)
        except ProtocolError as error:
            raise ProtocolError(UNBOUNDED_PRESENT,
                                "A battle present cannot be mailed even into an empty mailbox", 503) from error
        # After a capacity stop in this settlement, later presents wait behind the earlier ones to keep their order.
        if not self._blocked:
            try:
                state, added = self.presents.enqueue(state, [dict(reward)])
                return state, [row["PresentID"] for row in added], False
            except ProtocolError as error:
                if error.code not in PRESENT_CAPACITY_ERRORS:
                    raise
                self._blocked = True
        self._defer(reward)
        return state, [], True

    def _mail(self, state, reward, copies):
        present_ids, mailed, batch = [], 0, min(copies, MAX_BATCH)
        while mailed < copies:
            size = min(batch, copies - mailed)
            try:
                state, added = self.presents.enqueue(state, [dict(reward) for _ in range(size)])
            except ProtocolError as error:
                if error.code not in PRESENT_CAPACITY_ERRORS:
                    # A present the mailbox stopped accepting stays queued instead of blocking every later settlement.
                    log.warning("Pending battle present of kind %s stays queued after %s", reward["kind"], error.code)
                    break
                if size == 1:
                    self._blocked = True
                    break
                # Each enqueue revalidates the whole mailbox, so a refused batch is halved instead of retried per copy.
                batch = size // 2
                continue
            present_ids.extend(row["PresentID"] for row in added)
            mailed += size
        return state, present_ids, mailed

    def _defer(self, reward):
        entry = next((entry for entry in self._entries if entry["reward"] == reward), None)
        if entry is None:
            if len(self._entries) >= MAX_PENDING_PRESENTS:
                raise ProtocolError("quest_present_backlog",
                                    "Too many different battle presents are waiting for the mailbox", 409)
            self._entries.append(_entry(reward, 1))
        elif entry["copies"] == INT32_MAX:
            raise ProtocolError("quest_present_backlog", "Waiting copies of a battle present exceed the native int32 range", 409)
        else:
            entry["copies"] += 1
