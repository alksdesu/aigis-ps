import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.present_backlog import (
    EMPTY_MAILBOX, INT32_MAX, MAX_BATCH, MAX_PENDING_PRESENTS, MAX_PRESENT_TEXT_BYTES, PRESENT_CAPACITY_ERRORS,
    STORED_PRESENT_TEXT_BYTES, TEXT_FIELDS, PresentBacklog, require_bounded, require_deliverable, reward_valid,
)
from aigis_ps.protocol import NATIVE_CELL_BYTES


CRYSTAL = {"kind": "free_crystal", "amount": 1, "description": "★３報酬"}
TRIPLE_CRYSTAL = {**CRYSTAL, "amount": 3}
GIFT = {"kind": "item", "amount": 2, "item_id": "gift_0002_ruby", "description": "テスト報酬"}
UNIT = {"kind": "unit", "template_id": 5, "count": 1, "description": "テスト報酬"}
LEGACY = {"kind": "legacy_item", "amount": 3, "present_type": 7, "description": "テスト報酬"}
OTHER_LEGACY = {**LEGACY, "present_type": 14}
UNMAILABLE_ITEM = "item_absent_from_config"
# presents.preview only mails legacy items under their fixed present types; 22 is the ItemID present.
UNMAILABLE_PRESENT_TYPE = 22
EMPTY = {"mailed": ()}
BOUNDED_REWARDS = (CRYSTAL, GIFT, UNIT, LEGACY)


class FakeMailbox:
    def __init__(self, room=0, refused=(), full_code="present_capacity"):
        self.room, self.refused, self.full_code, self.batches = room, frozenset(refused), full_code, []
        self.previews = []

    def preview(self, rewards, existing_count):
        self.previews.append(([dict(reward) for reward in rewards], existing_count))
        if any(reward.get("item_id") == UNMAILABLE_ITEM for reward in rewards):
            raise ProtocolError("present_item_unsupported", "The item cannot be mailed", 501)
        if any(reward.get("present_type") == UNMAILABLE_PRESENT_TYPE for reward in rewards):
            raise ProtocolError("present_reward_invalid", "Legacy item rewards require a fixed present type")
        self._check_native_cells(rewards)
        return list(rewards)

    def enqueue(self, state, rewards):
        self.batches.append(len(rewards))
        if any(reward.get("item_id") in self.refused for reward in rewards):
            raise ProtocolError("present_item_unsupported", "The item is no longer mailable", 501)
        if len(rewards) > self.room:
            raise ProtocolError(self.full_code, "The present box is full", 409)
        self._check_native_cells(rewards)
        self.room -= len(rewards)
        first = len(state["mailed"]) + 1
        return ({"mailed": (*state["mailed"], *map(dict, rewards))},
                [{"PresentID": first + index} for index in range(len(rewards))])

    @staticmethod
    def _check_native_cells(rewards):
        if any(len(reward["item_id"].encode("utf-8")) > NATIVE_CELL_BYTES for reward in rewards if "item_id" in reward):
            raise ProtocolError("present_item_unreplayable",
                                "New present rows need item identities that fit the native cell", 503)


def entry(reward, copies=1):
    return {"reward": reward, "copies": copies}


def sized_gift(field, size):
    text = GIFT[field]
    return {**GIFT, field: text + "x" * (size - len(text.encode("utf-8")))}


def refused(code, status, callback):
    with pytest.raises(ProtocolError) as error:
        callback()
    assert (error.value.code, error.value.status) == (code, status)


@pytest.mark.parametrize("full_code", sorted(PRESENT_CAPACITY_ERRORS))
def test_identical_rewards_wait_as_copies_and_arrive_with_their_own_amount(full_code):
    mailbox = FakeMailbox(full_code=full_code)
    backlog = PresentBacklog(mailbox)
    state = EMPTY
    for reward in (CRYSTAL, CRYSTAL, TRIPLE_CRYSTAL, CRYSTAL):
        state, added, queued = backlog.send(state, dict(reward))
        assert (added, queued) == ([], True)
    assert backlog.entries == [entry(CRYSTAL, 3), entry(TRIPLE_CRYSTAL)]
    mailbox.room = MAX_BATCH
    reloaded = PresentBacklog(mailbox, backlog.entries)
    state, delivered = reloaded.flush(state)
    assert state["mailed"] == (CRYSTAL, CRYSTAL, CRYSTAL, TRIPLE_CRYSTAL) and delivered == [1, 2, 3, 4]
    assert reloaded.entries == [] and mailbox.batches == [1, 3, 1]


@pytest.mark.parametrize("full_code", sorted(PRESENT_CAPACITY_ERRORS))
def test_a_capacity_stop_keeps_the_remaining_copies_and_queues_new_presents_behind_them(full_code):
    mailbox = FakeMailbox(room=3, full_code=full_code)
    backlog = PresentBacklog(mailbox, [entry(CRYSTAL, 5), entry(GIFT)])
    state, delivered = backlog.flush(EMPTY)
    assert state["mailed"] == (CRYSTAL,) * 3 and delivered == [1, 2, 3]
    attempts = len(mailbox.batches)
    mailbox.room = MAX_BATCH
    state, added, queued = backlog.send(state, dict(UNIT))
    assert (added, queued, len(mailbox.batches)) == ([], True, attempts)
    assert backlog.entries == [entry(CRYSTAL, 2), entry(GIFT), entry(UNIT)]


def test_a_refused_entry_stays_queued_without_delaying_later_presents(caplog):
    mailbox = FakeMailbox(room=MAX_BATCH, refused={GIFT["item_id"]})
    backlog = PresentBacklog(mailbox, [entry(GIFT, 2), entry(CRYSTAL)])
    with caplog.at_level("WARNING", logger="aigis_ps"):
        state, delivered = backlog.flush(EMPTY)
    state, added, queued = backlog.send(state, dict(UNIT))
    assert state["mailed"] == (CRYSTAL, UNIT) and delivered == [1] and (added, queued) == ([2], False)
    assert backlog.entries == [entry(GIFT, 2)]
    assert [record.getMessage() for record in caplog.records] == [
        "Pending battle present of kind item stays queued after present_item_unsupported"]


def test_a_partly_free_mailbox_is_filled_with_few_bounded_batches():
    copies, room = 1000, 300
    mailbox = FakeMailbox(room=room)
    backlog = PresentBacklog(mailbox, [entry(CRYSTAL, copies)])
    state, delivered = backlog.flush(EMPTY)
    assert len(state["mailed"]) == len(delivered) == room
    assert backlog.entries == [entry(CRYSTAL, copies - room)]
    assert max(mailbox.batches) <= MAX_BATCH and len(mailbox.batches) <= 2 * MAX_BATCH.bit_length()


def test_copy_and_entry_limits_refuse_with_a_backlog_conflict():
    saturated = PresentBacklog(FakeMailbox(), [entry(CRYSTAL, INT32_MAX)])
    refused("quest_present_backlog", 409, lambda: saturated.send(EMPTY, dict(CRYSTAL)))
    assert saturated.entries == [entry(CRYSTAL, INT32_MAX)]
    crowded = [entry({**CRYSTAL, "amount": amount}) for amount in range(1, MAX_PENDING_PRESENTS + 1)]
    backlog = PresentBacklog(FakeMailbox(), crowded)
    refused("quest_present_backlog", 409, lambda: backlog.send(EMPTY, {**CRYSTAL, "amount": MAX_PENDING_PRESENTS + 1}))
    _, added, queued = backlog.send(EMPTY, dict(CRYSTAL))
    assert (added, queued) == ([], True) and backlog.entries == [entry(CRYSTAL, 2), *crowded[1:]]


def test_legacy_items_are_previewed_then_wait_as_copies_of_their_own_present_type():
    mailbox = FakeMailbox()
    backlog = PresentBacklog(mailbox)
    state = EMPTY
    for reward in (LEGACY, OTHER_LEGACY, LEGACY):
        state, added, queued = backlog.send(state, dict(reward))
        assert (added, queued) == ([], True)
    assert mailbox.previews == [([reward], EMPTY_MAILBOX) for reward in (LEGACY, OTHER_LEGACY, LEGACY)]
    assert backlog.entries == [entry(LEGACY, 2), entry(OTHER_LEGACY)]
    mailbox.room = MAX_BATCH
    state, delivered = PresentBacklog(mailbox, backlog.entries).flush(state)
    assert state["mailed"] == (LEGACY, LEGACY, OTHER_LEGACY) and delivered == [1, 2, 3]


@pytest.mark.parametrize("reward", [
    {**GIFT, "item_id": UNMAILABLE_ITEM}, {**GIFT, "description": "x" * (MAX_PRESENT_TEXT_BYTES + 1)},
    {**CRYSTAL, "amount": 0}, {**UNIT, "count": True},
    {"kind": "summon_ticket", "pool_id": 1, "count": 1, "description": "テスト報酬"},
    {**LEGACY, "present_type": 0}, {**LEGACY, "present_type": INT32_MAX + 1}, {**LEGACY, "present_type": "7"},
    {**LEGACY, "present_type": UNMAILABLE_PRESENT_TYPE}, {**LEGACY, "item_id": "trade_item_001000"},
])
def test_rewards_that_can_never_wait_or_arrive_are_refused_before_queueing(reward):
    mailbox = FakeMailbox()
    backlog = PresentBacklog(mailbox, [entry(CRYSTAL)])
    refused("quest_present_unsupported", 503, lambda: backlog.send(EMPTY, reward))
    assert backlog.entries == [entry(CRYSTAL)] and mailbox.batches == []


def test_without_a_present_service_waiting_presents_stay_and_new_ones_are_refused():
    backlog = PresentBacklog(None, [entry(CRYSTAL, 2)])
    assert backlog.flush(EMPTY) == (EMPTY, [])
    refused("quest_present_unavailable", 409, lambda: backlog.send(EMPTY, dict(GIFT)))
    assert backlog.entries == [entry(CRYSTAL, 2)]


def test_new_presents_follow_the_native_cell_and_saved_journals_keep_256_bytes():
    assert MAX_PRESENT_TEXT_BYTES == NATIVE_CELL_BYTES == 255 and STORED_PRESENT_TEXT_BYTES == 256


def test_a_saved_description_past_the_native_cell_still_loads_and_arrives():
    saved = sized_gift("description", STORED_PRESENT_TEXT_BYTES)
    assert PresentBacklog.valid([entry(saved, 2)])
    backlog = PresentBacklog(FakeMailbox(room=MAX_BATCH), [entry(saved, 2)])
    state, delivered = backlog.flush(EMPTY)
    assert state["mailed"] == (saved, saved) and delivered == [1, 2] and backlog.entries == []


def test_a_saved_item_past_the_native_cell_still_loads_but_waits_without_delaying_later_presents(caplog):
    saved = sized_gift("item_id", STORED_PRESENT_TEXT_BYTES)
    queue = [entry(saved, 2), entry(CRYSTAL)]
    assert PresentBacklog.valid(queue)
    backlog = PresentBacklog(FakeMailbox(room=MAX_BATCH), queue)
    with caplog.at_level("WARNING", logger="aigis_ps"):
        state, delivered = backlog.flush(EMPTY)
    assert state["mailed"] == (CRYSTAL,) and delivered == [1] and backlog.entries == [entry(saved, 2)]
    assert [record.getMessage() for record in caplog.records] == [
        "Pending battle present of kind item stays queued after present_item_unreplayable"]


@pytest.mark.parametrize("field", sorted(TEXT_FIELDS))
def test_new_presents_must_fit_the_native_cell_even_beside_a_saved_longer_copy(field):
    saved, fitting = sized_gift(field, STORED_PRESENT_TEXT_BYTES), sized_gift(field, MAX_PRESENT_TEXT_BYTES)
    assert not reward_valid(saved) and reward_valid(fitting)
    mailbox = FakeMailbox()
    backlog = PresentBacklog(mailbox, [entry(saved)])
    refused("quest_present_unsupported", 503, lambda: backlog.send(EMPTY, dict(saved)))
    assert backlog.entries == [entry(saved)] and mailbox.previews == mailbox.batches == []
    _, added, queued = backlog.send(EMPTY, dict(fitting))
    assert (added, queued) == ([], True) and backlog.entries == [entry(saved), entry(fitting)]
    assert PresentBacklog.valid(backlog.entries)


@pytest.mark.parametrize("field", sorted(TEXT_FIELDS))
def test_require_bounded_refuses_text_past_the_native_cell_before_any_preview(field):
    oversized, mailbox = sized_gift(field, MAX_PRESENT_TEXT_BYTES + 1), FakeMailbox()
    refused("quest_present_unsupported", 503, lambda: require_bounded(oversized))
    refused("quest_present_unsupported", 503, lambda: require_deliverable(mailbox, oversized))
    assert mailbox.previews == []


@pytest.mark.parametrize("reward", [
    *BOUNDED_REWARDS, *(sized_gift(field, MAX_PRESENT_TEXT_BYTES) for field in sorted(TEXT_FIELDS)),
])
def test_require_bounded_accepts_every_reward_kind_up_to_the_native_cell(reward):
    assert require_bounded(dict(reward)) is None


def test_require_deliverable_raises_the_preview_refusal_unchanged():
    mailbox, unmailable = FakeMailbox(), {**GIFT, "item_id": UNMAILABLE_ITEM}
    with pytest.raises(ProtocolError) as error:
        require_deliverable(mailbox, unmailable)
    assert (error.value.code, error.value.status, str(error.value), error.value.__cause__) == (
        "present_item_unsupported", 501, "The item cannot be mailed", None)
    assert mailbox.previews == [([unmailable], EMPTY_MAILBOX)]


@pytest.mark.parametrize("reward", BOUNDED_REWARDS)
def test_require_deliverable_previews_a_bounded_reward_once_against_an_empty_mailbox(reward):
    mailbox = FakeMailbox()
    assert require_deliverable(mailbox, dict(reward)) is None
    assert mailbox.previews == [([reward], EMPTY_MAILBOX)] and mailbox.batches == []


def test_journal_queues_accept_distinct_whole_rewards():
    assert PresentBacklog.valid([])
    assert PresentBacklog.valid([entry(CRYSTAL), entry(TRIPLE_CRYSTAL, INT32_MAX), entry(GIFT, 2), entry(UNIT),
                                 entry({**GIFT, "item_id": "gift_0003_diamond"}), entry(LEGACY, 2),
                                 entry(OTHER_LEGACY), entry({**LEGACY, "present_type": INT32_MAX})])


@pytest.mark.parametrize("entries", [
    [entry(CRYSTAL, 0)], [entry(CRYSTAL, INT32_MAX + 1)], [entry(CRYSTAL, True)], [entry(CRYSTAL, 1.0)],
    [entry(CRYSTAL, "1")], [{"reward": CRYSTAL}], [{**entry(CRYSTAL), "order": 1}], [CRYSTAL],
    [entry(CRYSTAL), entry(dict(CRYSTAL), 2)],
    [entry({**CRYSTAL, "kind": "gold_bar"})], [entry({**CRYSTAL, "kind": ["free_crystal"]})],
    [entry({**UNIT, "count": True})], [entry({**GIFT, "description": "\ud800"})], [entry({**GIFT, "item_id": ""})],
    [entry(sized_gift("description", STORED_PRESENT_TEXT_BYTES + 1))],
    [entry(sized_gift("item_id", STORED_PRESENT_TEXT_BYTES + 1))],
    [entry({**CRYSTAL, "amount": amount}) for amount in range(1, MAX_PENDING_PRESENTS + 2)],
    [entry({**LEGACY, "present_type": 0})], [entry({**LEGACY, "present_type": -1})],
    [entry({**LEGACY, "present_type": True})], [entry({**LEGACY, "present_type": 7.0})],
    [entry({key: value for key, value in LEGACY.items() if key != "present_type"})],
    [entry({**LEGACY, "item_id": "trade_item_001000"})], [entry(LEGACY), entry(dict(LEGACY))],
    {}, None,
])
def test_journal_queues_reject_bad_copies_duplicate_rewards_and_oversized_lists(entries):
    assert not PresentBacklog.valid(entries)
