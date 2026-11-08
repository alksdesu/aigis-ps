from copy import copy, deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
import aigis_ps.presents as module
from aigis_ps.presents import MAX_PRESENTS, PRESENT_LIMITS
from aigis_ps.protocol import encode_response
from aigis_ps.storage import Store
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)


@pytest.fixture(scope="module")
def source(bundle):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    initial = bundle.initialize(bundle.player_state.initialize(bootstrap.initial_state()))
    initial["unrelated"] = {"keep": [1]}
    rewards = [
        {"kind": "unit", "template_id": 2, "count": 1, "description": "unit"},
        {"kind": "free_crystal", "amount": 1, "description": "free"},
        {"kind": "gold", "amount": 100, "description": "gold"},
        {"kind": "item", "item_id": "gift_0002_ruby", "amount": 1, "description": "item"},
        {"kind": "summon_ticket", "pool_id": 350, "count": 1, "description": "summon"},
        {"kind": "selection_ticket", "config_id": 1, "count": 1, "description": "selection"},
        {"kind": "item", "item_id": "item_smn_ticket_19138", "amount": 1, "description": "item ticket"},
        {"kind": "charisma_recovery", "item_id": "item_cook_logbo_00004", "description": "cooking"},
    ]
    state, rows = bundle.presents.enqueue(initial, rewards)
    return SimpleNamespace(bundle=bundle, bootstrap=bootstrap, initial=initial, state=state, rows=rows)


@pytest.fixture
def case(source):
    bootstrap = copy(source.bootstrap)
    presents = copy(source.bundle.presents)
    presents.bootstrap = bootstrap
    for name in ("tickets", "item_tickets", "selection_tickets"):
        adapter = copy(getattr(presents, name))
        adapter.bootstrap, adapter.presents = bootstrap, presents
        if name != "selection_tickets":
            adapter.randbelow = lambda _: 0
        setattr(presents, name, adapter)
    bootstrap.present_provider = presents.response
    return SimpleNamespace(presents=presents, bootstrap=bootstrap, state=deepcopy(source.state),
                           initial=deepcopy(source.initial), rows=source.rows)


def columns(group):
    return {column.name: column.values for column in group.children}


def claim(store, present_id, *, selection=None, registration=1):
    fields = [Column("PresentID", "I", (present_id,))]
    if selection is not None:
        fields.extend((Column("SelectID", "I", (1,)), Column("SelectCardID", "I", (selection,))))
    return request(store, registration, (Container("RT", (Container("PT2", tuple(fields)),)),))


def unchanged(case, ids, category, code):
    before = deepcopy(case.state)
    with pytest.raises(ProtocolError) as error:
        case.presents.set_notification_category(case.state, ids, category)
    assert case.state == before
    assert error.value.code == code


def test_legacy_mail_serialization_is_unchanged_and_does_not_add_defaults(case):
    before = deepcopy(case.state)
    expected = Container("PPT", (
        Column("PresentID", "I", (1, 2, 3, 4, 5, 6, 7, 8)),
        Column("A1", "I", (1, 0, 2, 22, 16, 20, 22, 22)),
        Column("A2", "I", (2, 1, 100, 1, 1, 1, 1, 1)),
        Column("A3", "S", ("unit", "free", "gold", "item", "summon", "selection", "item ticket", "cooking")),
        Column("A4", "I", (0, 0, 0, 0, 350, 1, 0, 0)),
        Column("A6", "S", ("", "", "", "gift_0002_ruby", "", "", "item_smn_ticket_19138", "item_cook_logbo_00004")),
    ))
    assert dumps(Container(children=(case.presents.response(case.state, list(range(1, 9))),))) == dumps(Container(children=(expected,)))
    assert case.presents.response(case.state) == Container("PPT", (*expected.children, Column("A9", "I", (0,) * 8)))
    assert case.state == before
    assert all("NotificationCategory" not in row for row in case.presents.rows(case.state))
    assert [column.name for column in case.presents.response(case.initial, []).children] == ["PresentID", "A1", "A2", "A3"]
    assert [column.name for column in case.presents.response(case.initial).children] == ["PresentID", "A1", "A2", "A3", "A4", "A6", "A9"]


def test_mixed_rows_emit_aligned_a9_after_a4_and_a6(case):
    updated = case.presents.set_notification_category(case.state, [2, 5, 6, 8], 7)
    response = case.presents.response(updated)
    assert [column.name for column in response.children] == ["PresentID", "A1", "A2", "A3", "A4", "A6", "A9"]
    assert response.children[-1].type == "I"
    assert columns(response)["A9"] == (0, 7, 0, 0, 7, 7, 0, 7)
    assert columns(response)["A4"] == (0, 0, 0, 0, 350, 1, 0, 0)
    assert columns(case.presents.response(updated, (8, 4, 2)))["A9"] == (7, 0, 7)
    assert "A9" not in columns(case.presents.response(updated, (1, 3, 4, 7)))
    assert "A9" not in columns(case.presents.response(updated, ()))


def test_helper_only_changes_selected_metadata_and_returns_independent_state(case):
    before = deepcopy(case.state)
    updated = case.presents.set_notification_category(case.state, (1, 3), 7)
    expected = deepcopy(before)
    for index in (0, 2):
        expected["tables"]["presents"][index]["NotificationCategory"] = 7
    assert updated == expected and case.state == before
    assert updated["last_present_id"] == case.state["last_present_id"]
    updated["unrelated"]["keep"].append(2)
    updated["tables"]["presents"][0]["Description"] = "changed"
    assert case.state == before


def test_setting_zero_on_missing_field_is_equal_deepcopy(case):
    updated = case.presents.set_notification_category(case.state, [1], 0)
    assert updated == case.state and updated is not case.state
    assert "NotificationCategory" not in updated["tables"]["presents"][0]
    assert "A9" not in columns(case.presents.response(updated, [1]))


def test_explicit_zero_clears_a9_without_dropping_the_column(case):
    marked = case.presents.set_notification_category(case.state, [1], 7)
    cleared = case.presents.set_notification_category(marked, [1], 0)
    assert cleared["tables"]["presents"][0]["NotificationCategory"] == 0
    assert columns(case.presents.response(cleared))["A9"] == (0,) * 8
    repeated = case.presents.set_notification_category(cleared, [1], 0)
    assert repeated == cleared and repeated is not cleared


@pytest.mark.parametrize("category", [0, 1, 3, 7, 0x7FFFFFFF])
def test_nonnegative_native_category_boundaries(case, category):
    updated = case.presents.set_notification_category(case.state, [1], category)
    assert updated["tables"]["presents"][0].get("NotificationCategory", 0) == category


@pytest.mark.parametrize("category", [True, False, None, "7", 7.0, -1, 0x80000000, [], {}])
def test_bad_category_argument_is_atomic(case, category):
    unchanged(case, [1], category, "present_category_invalid")


@pytest.mark.parametrize("ids", [[], (), None, "1", {1}, [True], [0], [-1], [0x80000000], [1, 1],
                              list(range(1, MAX_PRESENTS + 2))])
def test_bad_or_empty_selection_is_atomic(case, ids):
    unchanged(case, ids, 7, "present_selection_invalid")


def test_missing_id_in_batch_does_not_partially_decorate(case):
    unchanged(case, [1, 999], 7, "present_not_found")


@pytest.mark.parametrize("category", [True, None, -1, 0x80000000, "7", 1.5])
def test_bad_existing_category_is_not_repaired(case, category):
    case.state["tables"]["presents"][1]["NotificationCategory"] = category
    unchanged(case, [1], 7, "present_state_invalid")


@pytest.mark.parametrize("change", ["unknown", "missing_item", "missing_pool", "missing_selection", "unversioned"])
def test_optional_category_does_not_weaken_other_row_validation(case, change):
    rows = case.state["tables"]["presents"]
    for row in rows:
        row["NotificationCategory"] = 7
    if change == "unknown":
        rows[0]["Unexpected"] = 1
    elif change == "missing_item":
        del rows[3]["ItemID"]
    elif change == "missing_pool":
        del rows[4]["SummonID"]
    elif change == "missing_selection":
        del rows[5]["SelectionConfigID"]
    else:
        case.state.pop("presents_schema")
    unchanged(case, [1], 0, "present_state_invalid")


def test_metadata_cannot_be_smuggled_through_reward_input(case):
    before = deepcopy(case.initial)
    with pytest.raises(ProtocolError) as error:
        case.presents.enqueue(case.initial, [{"kind": "free_crystal", "amount": 1,
            "description": "bad", "NotificationCategory": 7}])
    assert error.value.code == "present_reward_invalid" and case.initial == before


def test_encode_failure_leaves_input_unchanged(case, monkeypatch):
    def fail(*args, **kwargs):
        raise ProtocolError("test_capacity", "Forced native size limit", 503)
    monkeypatch.setattr(module, "encode_response", fail)
    unchanged(case, [1, 2], 7, "test_capacity")


def test_full_mixed_mailbox_fits_existing_seven_column_budget(case):
    state = deepcopy(case.state)
    original = state["tables"]["presents"]
    state["tables"]["presents"] = [{**deepcopy(original[index % len(original)]), "PresentID": index + 1}
                                      for index in range(MAX_PRESENTS)]
    state["last_present_id"] = MAX_PRESENTS
    updated = case.presents.set_notification_category(state, list(range(1, MAX_PRESENTS + 1)), 7)
    response = case.presents.response(updated)
    assert len(response.children) == 7
    assert all(len(column.values) == MAX_PRESENTS for column in response.children)
    xml = dumps(Container(children=(response,)), limits=PRESENT_LIMITS)
    encode_response(case.presents._list_profile, xml)
    assert all(row.get("NotificationCategory") is None for row in state["tables"]["presents"])


@pytest.mark.parametrize("present_id,expected_units", [(1, 1), (2, 0), (3, 0), (4, 0), (5, 1), (6, 1), (7, 10), (8, 0)])
def test_each_mail_kind_claims_and_replays_without_category_side_effects(case, tmp_path, present_id, expected_units):
    updated = case.presents.set_notification_category(case.state, list(range(1, 9)), 7)
    store = Store(tmp_path / "claim.sqlite3", updated)
    selected_card = case.presents.selection_tickets.catalog.get(1).candidates[0].card_id if present_id == 6 else None
    envelope = claim(store, present_id, selection=selected_card)
    before = store.profile()
    response = case.presents.receive(envelope, store)
    after = store.profile()
    assert after.revision == before.revision + 1
    assert len(after.state["player_state"]["units"]) - len(before.state["player_state"]["units"]) == expected_units
    remaining = case.presents.rows(after.state)
    assert len(remaining) == 7 and all(row["NotificationCategory"] == 7 for row in remaining)
    assert present_id not in {row["PresentID"] for row in remaining}
    groups = {node.name: node for node in loads(response).children}
    assert "PPT" not in groups
    assert len(groups["PPU"].children[0].values) == expected_units
    reopened = Store(store.path)
    case.bootstrap.clock = lambda: pytest.fail("Receipt replay read the clock")
    if present_id in (5, 7):
        adapter = case.presents.tickets if present_id == 5 else case.presents.item_tickets
        adapter.randbelow = lambda _: pytest.fail("Receipt replay drew again")
    assert case.presents.receive(envelope, reopened) == response
    assert reopened.profile() == after


def test_legacy_unclassified_claim_receipt_stays_identical(case, tmp_path):
    store = Store(tmp_path / "legacy.sqlite3", case.state)
    envelope = claim(store, 1)
    response = case.presents.receive(envelope, store)
    before = store.profile()
    assert "A9" not in columns(case.presents.response(before.state, [row["PresentID"] for row in case.presents.rows(before.state)]))
    assert case.presents.receive(envelope, Store(store.path)) == response
    assert store.profile() == before


def test_partial_claim_preserves_category_on_retained_letters(case, tmp_path):
    state = case.presents.set_notification_category(case.state, [1, 2], 7)
    state["tables"]["presents"][1]["Content"] = 1001
    store = Store(tmp_path / "partial.sqlite3", state)
    envelope = request(store, 117, (Container("RT", (Container("PT2", (Column("PresentID", "I", (1, 2)),)),)),))
    case.presents.receive(envelope, store)
    rows = {row["PresentID"]: row for row in case.presents.rows(store.profile().state)}
    assert 1 not in rows and rows[2]["NotificationCategory"] == 7
    assert rows[2]["Content"] == 1001
