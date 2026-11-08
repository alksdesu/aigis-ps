import base64
from copy import copy, deepcopy
from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.quest_service import QuestService
from aigis_ps.storage import Store
from test_evolution_catalog import materials, unit
from test_evolution_integration import message as evolution_message, seed
from test_gift_service import choose, message as gift_message
from test_present_items_integration import claim
from test_quest_service import bundle, finish_request, request, start_request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
BLESSING = "battle_god_bless_001001"
BOUQUET = "gift_0000_bouquet"
ORB_SOURCE = "evolve_orb_100002"
ORB_TARGET = "evolve_orb_100001"
ACTIONS = ("gift", "orb", "trade", "present", "quest", "evolution", "sale")
POSSESSION_COLUMNS = ("ItemID", "Count", "Enabled")


def inventory(state):
    return {row["ItemID"]: row for row in state["tables"]["possessions"]}


def tree(response):
    return {node.name: node for node in loads(response).children}


def wire_rows(node):
    columns = {column.name: column.values for column in node.children}
    assert tuple(columns) == POSSESSION_COLUMNS
    assert len({len(values) for values in columns.values()}) == 1
    assert all(column.type == ("S" if column.name == "ItemID" else "I") for column in node.children)
    return {name: {key: values[index] for key, values in columns.items()}
            for index, name in enumerate(columns["ItemID"])}


@pytest.fixture
def make_case(bundle, tmp_path):
    serial = 0

    def build(action, *, touched_enabled=False):
        nonlocal serial
        serial += 1
        bootstrap = copy(bundle.bootstrap)
        bootstrap.clock = lambda: NOW
        state = bundle.player_state.initialize(bootstrap.initial_state())
        state["tables"]["game_param"][0]["Money"] = 1000000
        rows, expected, prefix = [], {}, None
        if action == "gift":
            service = copy(bundle.gifts)
            service.clock = lambda: NOW
            card = choose(SimpleNamespace(catalog=service.catalog, aliases=service.aliases), gender=1)
            state, added = bundle.player_state.credit_units(state, [{"card_id": card, "level": 1, "count": 1}])
            rows = [{"ItemID": BOUQUET, "Count": 10}]
            expected = {BOUQUET: 9}
            invoke = service.send
            envelope = lambda store: gift_message(SimpleNamespace(service=service, store=store,
                indices=[added[0]["GetIndex"]]), slots=[(BOUQUET, 1)])
        elif action == "orb":
            service = copy(bundle.orb_exchange)
            rows = [{"ItemID": ORB_SOURCE, "Count": 5}, {"ItemID": ORB_TARGET, "Count": 2}]
            expected = {ORB_SOURCE: 0, ORB_TARGET: 3}
            invoke = service.exchange
            envelope = lambda store: request(store, 139, (Column("ITEM", "S", (ORB_TARGET,)),
                                                         Column("BASE", "S", (ORB_SOURCE,))))
        elif action == "trade":
            service = copy(bundle.trades)
            service.clock = lambda: NOW
            product = service.catalog.product(575)
            rows = [{"ItemID": product.currency_item_id, "Count": 100},
                    {"ItemID": product.item_id, "Count": 0}]
            quote = service.catalog.quote({**state, "tables": {**state["tables"], "possessions": rows}},
                product.shop_id, 1, 0, NOW, product.registration)
            expected = {product.currency_item_id: 100 - quote.total_price, product.item_id: quote.amount}
            bootstrap.exchange_history_provider = service.history_response
            invoke = service.purchase
            envelope = lambda store: request(store, product.registration,
                (Column("SHOP", "I", (product.shop_id,)), Column("NUM", "I", (1,))))
        elif action == "present":
            service = copy(bundle.presents)
            rows = [{"ItemID": BOUQUET, "Count": 7}]
            expected = {BOUQUET: 9}
            state, mail = service.enqueue(state, [{"kind": "legacy_item", "present_type": 3,
                                                  "amount": 2, "description": "花束"}])
            invoke = service.receive
            envelope = lambda store: claim(store, 1, [mail[0]["PresentID"]])
        elif action == "quest":
            service = QuestService(bundle.quests.catalog, bootstrap, bundle.player_state, bundle.possessions,
                                   clock=lambda: NOW, randbelow=lambda _: 0)
            quest = service.catalog.get(1938)
            rewards = [service.catalog.decode_reward(code) for code in quest.treasures[:2]]
            assert all(reward.kind == "item" for reward in rewards)
            rows = [{"ItemID": reward.item_id, "Count": index + 2} for index, reward in enumerate(rewards)]
            expected = {row["ItemID"]: row["Count"] + reward.count for row, reward in zip(rows, rewards)}
            state["tables"]["quest_records"] = [{"QuestID": quest.prerequisite, "MaxRank": 3,
                "AchieveFlag": 0x01000000, "RemainAssault": 0, "FirstDefeatDate": 0, "Shown": False}]
            prefix = lambda store: service.start(start_request(service, store, quest.quest_id), store)
            invoke = service.finish
            envelope = lambda store: finish_request(store, quest.quest_id, obtained=3, selected=7)
        elif action == "evolution":
            service = copy(bundle.evolution)
            target = unit(service.catalog, level=999)
            state, added = seed(bundle, target, materials(service.catalog, target, 7))
            quote = service.catalog.quote(added[0], added[1:], 7)
            rows = [{"ItemID": name, "Count": count * 2} for name, count in quote.items.items()]
            expected = dict(quote.items)
            invoke = service.evolve
            envelope = lambda store: evolution_message(store, added[0], added[1:])
        else:
            assert action == "sale"
            service = copy(bundle.sales)
            state, added = bundle.player_state.credit_units(state, [{"card_id": 7, "level": 1, "count": 1}])
            quote = service.catalog.quote(added)
            assert quote.rainbow > 0
            item = service.catalog.rainbow_item
            rows = [{"ItemID": item, "Count": 17}]
            expected = {item: 17 + quote.rainbow}
            invoke = service.sell
            from test_evolution_service import columns
            envelope = lambda store: request(store, 3, (Container("APPEND", columns(added)),))
        service.bootstrap = bootstrap
        if touched_enabled:
            rows[0]["Enabled"] = 1
        rows.append({"ItemID": BLESSING, "Count": 1, "Enabled": 1})
        state["tables"]["possessions"] = rows
        path = tmp_path / f"{action}-{serial}.sqlite3"
        store = Store(path, state)
        if prefix is not None:
            prefix(store)
        return SimpleNamespace(service=service, bootstrap=bootstrap, store=store, path=path,
            invoke=invoke, envelope=envelope(store), expected=expected, initial=state)

    return build


def assert_reload(case, bundle):
    saved = case.store.profile()
    expected = bundle.possessions.response(saved.state)
    for number in (129, 137):
        response = case.bootstrap.handlers()[number](request(case.store, number, ()), case.store)
        assert tree(response)["POSSESSION"] == expected
        rows = wire_rows(tree(response)["POSSESSION"])
        assert rows[BLESSING]["Enabled"] == 1
        assert all(row["Enabled"] == inventory(saved.state)[name].get("Enabled", 0)
                   for name, row in rows.items())
    assert case.store.profile() == saved


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("touched_enabled", [False, True])
def test_real_transactions_preserve_flags_and_replay_after_reopen(bundle, make_case, action, touched_enabled):
    case = make_case(action, touched_enabled=touched_enabled)
    before = case.store.profile()
    response = case.invoke(case.envelope, case.store)
    saved = case.store.profile()
    expected = deepcopy(inventory(before.state))
    for name, count in case.expected.items():
        expected[name]["Count"] = count
    assert inventory(saved.state) == expected
    assert saved.revision == before.revision + 1
    node = tree(response)["POSSESSION"]
    rows = wire_rows(node)
    assert set(rows) == set(case.expected)
    assert all(row["Count"] == case.expected[name] for name, row in rows.items())
    assert all(row["Enabled"] == expected[name].get("Enabled", 0) for name, row in rows.items())
    assert_reload(case, bundle)
    reopened = Store(case.path)
    assert case.invoke(case.envelope, reopened) == response
    assert reopened.profile() == saved


@pytest.mark.parametrize("action", ACTIONS)
def test_response_failure_leaves_counts_flags_and_other_state_unchanged(bundle, make_case, monkeypatch, action):
    case = make_case(action, touched_enabled=True)
    before = case.store.profile()
    original = bundle.possessions.response

    def fail_after_changes(state, *args, **kwargs):
        rows = inventory(state)
        if all(rows[name]["Count"] == count for name, count in case.expected.items()):
            raise ProtocolError("test_possession_output", "Output preflight failed", 503)
        return original(state, *args, **kwargs)

    monkeypatch.setattr(bundle.possessions, "response", fail_after_changes)
    with pytest.raises(ProtocolError, match="Output preflight failed") as caught:
        case.invoke(case.envelope, case.store)
    assert caught.value.code == "test_possession_output"
    assert case.store.profile() == before


def test_cas_retry_preserves_concurrent_flag_change_without_double_exchange(make_case, monkeypatch):
    case = make_case("orb", touched_enabled=True)
    before = case.store.profile()
    update = case.store.update_state
    calls = 0

    def contend(state, revision):
        nonlocal calls
        calls += 1
        if calls == 1:
            external = deepcopy(before.state)
            inventory(external)[BLESSING]["Enabled"] = 0
            update(external, revision)
            raise ValueError("Concurrent setting change")
        return update(state, revision)

    monkeypatch.setattr(case.store, "update_state", contend)
    case.invoke(case.envelope, case.store)
    after = case.store.profile()
    assert calls == 2 and after.revision == before.revision + 2
    assert inventory(after.state)[BLESSING] == {"ItemID": BLESSING, "Count": 1, "Enabled": 0}
    assert inventory(after.state)[ORB_SOURCE] == {"ItemID": ORB_SOURCE, "Count": 0, "Enabled": 1}
    assert inventory(after.state)[ORB_TARGET] == {"ItemID": ORB_TARGET, "Count": 3}
    assert after.state["tables"]["game_param"][0]["Money"] == 985000


@pytest.mark.parametrize("enabled", [True, -1, 2, "1"])
@pytest.mark.parametrize("action", ["orb", "present"])
def test_invalid_unrelated_flag_cannot_be_erased_by_a_transaction(make_case, enabled, action):
    case = make_case(action)
    profile = case.store.profile()
    state = deepcopy(profile.state)
    inventory(state)[BLESSING]["Enabled"] = enabled
    case.store.update_state(state, profile.revision)
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.invoke(case.envelope, case.store)
    assert caught.value.code == "possession_state_invalid"
    assert case.store.profile() == before


def test_mail_cached_enabled_is_checked_against_current_inventory(make_case):
    case = make_case("present", touched_enabled=True)
    response = case.invoke(case.envelope, case.store)
    saved = case.store.profile()
    root = loads(response)
    altered = Container(children=tuple(Container(node.name, tuple(
        Column("Enabled", "I", (0,)) if column.name == "Enabled" else column for column in node.children))
        if node.name == "POSSESSION" else node for node in root.children))
    with pytest.raises(ProtocolError) as caught:
        case.service._validate_cached_rewards(saved, dumps(altered))
    assert caught.value.code == "present_history_invalid"
    assert case.store.profile() == saved


def two_column_receipt(response):
    return dumps(Container(children=tuple(
        Container(node.name, node.children[:2]) if node.name == "POSSESSION" else node
        for node in loads(response).children)))


@pytest.mark.parametrize("touched_enabled", [False, True])
def test_two_column_batch_receipt_replays_only_where_it_was_the_earlier_shape(make_case, touched_enabled):
    case = make_case("present", touched_enabled=touched_enabled)
    mail = case.store.profile().state["tables"]["presents"][0]["PresentID"]
    envelope = claim(case.store, 117, [mail])
    response = case.invoke(envelope, case.store)
    legacy = two_column_receipt(response)
    assert tuple(column.name for column in tree(response)["POSSESSION"].children) == POSSESSION_COLUMNS
    assert tuple(column.name for column in tree(legacy)["POSSESSION"].children) == POSSESSION_COLUMNS[:2]
    profile = case.store.profile()
    state = deepcopy(profile.state)
    state["present_claim_history"][-1]["response"] = base64.b64encode(legacy).decode("ascii")
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state),))
    saved = case.store.profile()
    assert saved.revision == profile.revision
    if touched_enabled:
        with pytest.raises(ProtocolError) as caught:
            case.invoke(envelope, case.store)
        assert caught.value.code == "present_history_invalid"
    else:
        assert case.invoke(envelope, case.store) == legacy
    assert case.store.profile() == saved


def test_enabled_only_change_does_not_change_skin_ownership_or_mission_progress(bundle):
    skins = bundle.unit_detail.skin_catalog
    state = bundle.missions.initialize(bundle.player_state.initialize(bundle.bootstrap.initial_state()))
    card_id, skin = next((card, skin) for card, entries in skins._links.items() for skin in entries
                         if skin.item_id in skins.counted_item_ids)
    state = bundle.possessions.credit(state, {skin.item_id: 1, BLESSING: 1})
    original = deepcopy(state)
    updated = bundle.possessions.set_enabled(state, {skin.item_id: 1, BLESSING: 1})
    assert skins.acquired_items(updated) == skins.acquired_items(state) == frozenset((skin.item_id,))
    assert skins.allowed_displays(card_id, updated, smartphone_images=True) == skins.allowed_displays(
        card_id, state, smartphone_images=True)
    assert bundle.missions.record_acquisitions(state, updated, NOW) == updated
    assert "skin_acquisition_history" not in updated
    assert state == original
