from copy import copy, deepcopy

import pytest

from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.errors import ProtocolError
from aigis_ps.present_items import LEGACY_ITEMS
import aigis_ps.presents as module
from aigis_ps.presents import MAX_PRESENTS
from test_quest_service import bundle


@pytest.fixture
def presents(bundle):
    result = copy(bundle.presents)
    result.issues = dict(result.issues)
    return result


@pytest.fixture
def state():
    return {"bootstrap_schema": 1, "tables": {"presents": []}, "sentinel": {"keep": [1]}}


def unit(count=1):
    return {"kind": "unit", "template_id": 2, "count": count, "description": "unit"}


def all_groups():
    return [
        [unit(2), {"kind": "free_crystal", "amount": 2, "description": "crystal"}],
        [],
        [{"kind": "gold", "amount": 3, "description": "gold"},
         {"kind": "legacy_item", "present_type": 3, "amount": 4, "description": "legacy"},
         {"kind": "item", "item_id": "gift_0002_ruby", "amount": 5, "description": "item"}],
        [{"kind": "charisma_recovery", "description": "recovery"},
         {"kind": "charisma_recovery", "item_id": "item_cook_logbo_00004", "description": "cooking"}],
        [{"kind": "summon_ticket", "pool_id": 350, "count": 2, "description": "summon"},
         {"kind": "selection_ticket", "config_id": 1, "count": 2, "description": "selection"},
         {"kind": "item", "item_id": "item_smn_ticket_19138", "amount": 2, "description": "item ticket"}],
    ]


def expected_rows():
    values = [
        (1, 2, "unit", {}), (1, 2, "unit", {}), (0, 2, "crystal", {}),
        (2, 3, "gold", {}), (3, 4, "legacy", {}), (22, 5, "item", {"ItemID": "gift_0002_ruby"}),
        (8, 0, "recovery", {}), (22, 1, "cooking", {"ItemID": "item_cook_logbo_00004"}),
        (16, 1, "summon", {"SummonID": 350}), (16, 1, "summon", {"SummonID": 350}),
        (20, 1, "selection", {"SelectionConfigID": 1}), (20, 1, "selection", {"SelectionConfigID": 1}),
        (22, 1, "item ticket", {"ItemID": "item_smn_ticket_19138"}),
        (22, 1, "item ticket", {"ItemID": "item_smn_ticket_19138"}),
    ]
    return [{"PresentID": index + 1, "Type": kind, "Content": content, "Description": description, **extra}
            for index, (kind, content, description, extra) in enumerate(values)]


def unchanged(presents, state, groups, code=None):
    before, original = deepcopy(state), deepcopy(groups)
    with pytest.raises(ProtocolError) as error:
        presents.enqueue_groups(state, groups)
    assert state == before and groups == original
    if code is not None:
        assert error.value.code == code
    return error.value


def test_all_reward_kinds_keep_exact_group_to_mail_mapping(presents, state):
    groups = all_groups()
    before, original = deepcopy(state), deepcopy(groups)
    updated, added = presents.enqueue_groups(state, groups)
    assert [[row["PresentID"] for row in group] for group in added] == [
        [1, 2, 3], [], [4, 5, 6], [7, 8], [9, 10, 11, 12, 13, 14]]
    assert [row for group in added for row in group] == expected_rows()
    assert updated["tables"]["presents"] == expected_rows()
    assert updated["last_present_id"] == 14 and updated["presents_schema"] == 1
    assert state == before and groups == original
    added[0][0]["Description"] = "changed output"
    updated["sentinel"]["keep"].append(2)
    assert updated["tables"]["presents"] == expected_rows() and state == before


def test_legacy_enqueue_preserves_old_serialized_shape_and_order(presents, state):
    flattened = [reward for group in all_groups() for reward in group]
    updated, added = presents.enqueue(state, flattened)
    expected = expected_rows()
    assert added == expected
    legacy_ppt = Container("PPT", (
        Column("PresentID", "I", tuple(range(1, 15))),
        Column("A1", "I", (1, 1, 0, 2, 3, 22, 8, 22, 16, 16, 20, 20, 22, 22)),
        Column("A2", "I", (2, 2, 2, 3, 4, 5, 0, 1, 1, 1, 1, 1, 1, 1)),
        Column("A3", "S", ("unit", "unit", "crystal", "gold", "legacy", "item", "recovery", "cooking",
                             "summon", "summon", "selection", "selection", "item ticket", "item ticket")),
        Column("A4", "I", (0, 0, 0, 0, 0, 0, 0, 0, 350, 350, 1, 1, 0, 0)),
        Column("A6", "S", ("", "", "", "", "", "gift_0002_ruby", "", "item_cook_logbo_00004", "", "", "", "",
                             "item_smn_ticket_19138", "item_smn_ticket_19138")),
    ))
    assert dumps(Container(children=(presents.response(updated, list(range(1, 15))),))) == dumps(Container(children=(legacy_ppt,)))
    assert presents.response(updated) == Container("PPT", (*legacy_ppt.children, Column("A9", "I", (0,) * 14)))
    grouped_state, grouped = presents.enqueue_groups(state, all_groups())
    assert grouped_state == updated and [row for group in grouped for row in group] == added


@pytest.mark.parametrize("groups", [[], (), [[]], [[], [], []], tuple([] for _ in range(MAX_PRESENTS))])
def test_empty_group_shapes_preserve_legacy_state_and_output_positions(presents, state, groups):
    updated, added = presents.enqueue_groups(state, groups)
    assert updated == state and updated is not state
    assert added == [[] for _ in groups]
    assert "presents_schema" not in updated and "last_present_id" not in updated


def test_old_empty_enqueue_stays_equal_deepcopy(presents, state):
    updated, rows = presents.enqueue(state, [])
    assert updated == state and updated is not state and rows == []


@pytest.mark.parametrize("kind", tuple(LEGACY_ITEMS))
def test_all_existing_legacy_item_types_share_the_normalizer(presents, state, kind):
    updated, groups = presents.enqueue_groups(state, [[
        {"kind": "legacy_item", "present_type": kind, "amount": 1, "description": "legacy"}]])
    assert groups[0][0]["Type"] == kind and groups[0][0]["Content"] == 1
    assert updated["tables"]["presents"] == groups[0]


def test_existing_mail_and_category_are_preserved_and_ids_start_above_highwater(presents, state):
    prior, _ = presents.enqueue(state, [unit()])
    prior = presents.set_notification_category(prior, [1], 7)
    prior["last_present_id"] = 100
    updated, groups = presents.enqueue_groups(prior, [[unit()], [], [unit(2)]])
    assert [[row["PresentID"] for row in group] for group in groups] == [[101], [], [102, 103]]
    assert updated["tables"]["presents"][0] == prior["tables"]["presents"][0]
    assert updated["last_present_id"] == 103


@pytest.mark.parametrize("bad", [None, {}, "groups", [None], [()], [[None]], [[]] * (MAX_PRESENTS + 1),
                                 [[unit()] * (MAX_PRESENTS + 1)]])
def test_bad_group_shapes_and_outer_limits_fail_atomically(presents, state, bad):
    unchanged(presents, state, bad, "present_reward_invalid")


@pytest.mark.parametrize("bad", [
    {"kind": "unknown"}, {"kind": "unit", "template_id": 2, "count": True, "description": "unit"},
    {"kind": "free_crystal", "amount": 0, "description": "bad"},
    {"kind": "gold", "amount": 1, "description": "bad\x01"},
    {"kind": "item", "item_id": "missing", "amount": 1, "description": "bad"},
    {"kind": "unit", "template_id": 2, "count": 1, "description": "unit", "NotificationCategory": 7},
])
def test_invalid_later_group_does_not_commit_earlier_prepared_rows(presents, state, bad):
    unchanged(presents, state, [[unit()], [bad]])


def test_later_disabled_pool_failure_does_not_lose_prior_group(presents, state):
    bad = {"kind": "item", "item_id": "item_smn_ticket_10001", "amount": 1, "description": "unsupported"}
    assert unchanged(presents, state, [[unit()], [bad]]).status == 501


@pytest.mark.parametrize("last", [unit(2),
    {"kind": "summon_ticket", "pool_id": 350, "count": 2, "description": "summon"},
    {"kind": "selection_ticket", "config_id": 1, "count": 2, "description": "selection"},
    {"kind": "item", "item_id": "item_smn_ticket_19138", "amount": 2, "description": "item ticket"},
])
def test_split_reward_counts_accumulate_capacity_across_groups(presents, state, last):
    unchanged(presents, state, [[unit(MAX_PRESENTS - 1)], [last]], "present_capacity")


def test_single_row_reward_also_observes_capacity_after_previous_group(presents, state):
    unchanged(presents, state, [[unit(MAX_PRESENTS)], [{"kind": "charisma_recovery", "description": "recovery"}]],
              "present_capacity")


def test_exact_total_capacity_succeeds_without_extra_rows(presents, state):
    updated, groups = presents.enqueue_groups(state, [[unit(MAX_PRESENTS - 1)], [unit()]])
    assert [len(group) for group in groups] == [MAX_PRESENTS - 1, 1]
    assert len(updated["tables"]["presents"]) == updated["last_present_id"] == MAX_PRESENTS


def test_later_group_id_exhaustion_is_atomic(presents, state):
    state.update(presents_schema=1, last_present_id=0x7FFFFFFE)
    unchanged(presents, state, [[unit()], [unit()]], "present_identity_exhausted")
    updated, groups = presents.enqueue_groups(state, [[unit()]])
    assert updated["last_present_id"] == groups[0][0]["PresentID"] == 0x7FFFFFFF


def test_one_final_encoding_and_two_mailbox_scans_for_two_hundred_groups(presents, state, monkeypatch):
    original_encode, original_rows = module.encode_response, presents.rows
    encodings, scans = [], []
    def encode(*args, **kwargs):
        encodings.append(1)
        return original_encode(*args, **kwargs)
    def rows(value):
        scans.append(1)
        return original_rows(value)
    monkeypatch.setattr(module, "encode_response", encode)
    monkeypatch.setattr(presents, "rows", rows)
    updated, groups = presents.enqueue_groups(state, [[unit()] for _ in range(200)])
    assert len(groups) == 200 and updated["last_present_id"] == 200
    assert len(encodings) == 1 and len(scans) == 2


def test_final_encoding_failure_preserves_entire_input(presents, state, monkeypatch):
    def fail(*args, **kwargs):
        raise ProtocolError("test_encoding", "test", 503)
    monkeypatch.setattr(module, "encode_response", fail)
    unchanged(presents, state, [[unit()], [unit()]], "test_encoding")


def test_late_prepare_failure_happens_before_any_output_encoding(presents, state, monkeypatch):
    original = presents._prepare_reward_rows
    count = 0
    def prepare(rewards, existing_count):
        nonlocal count
        count += 1
        if count == 2:
            raise ProtocolError("late_group", "test", 501)
        return original(rewards, existing_count)
    monkeypatch.setattr(presents, "_prepare_reward_rows", prepare)
    monkeypatch.setattr(module, "encode_response", lambda *_: pytest.fail("Encoded before all groups prepared"))
    unchanged(presents, state, [[unit()], [unit()]], "late_group")


def test_preparation_cannot_hide_dependency_failure_from_final_validation(presents, state, monkeypatch):
    original = presents._prepare_reward_rows
    def prepare(rewards, existing_count):
        result = original(rewards, existing_count)
        presents.issues["templates"] = "changed"
        return result
    monkeypatch.setattr(presents, "_prepare_reward_rows", prepare)
    unchanged(presents, state, [[unit()]], "present_templates_unavailable")
