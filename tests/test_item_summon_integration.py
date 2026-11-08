from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.app import create_app
from aigis_ps.cli import load_item_ticket_rules
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.item_summon_tickets import DEFAULT_ITEM_TICKET_POOLS, ItemSummonTicketService
from aigis_ps.player_state import UNIT_FIELDS
from aigis_ps.protocol import encode_wire, decode_wire
from aigis_ps.summon_tickets import SummonTicketRules
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, tzinfo=JST)
ITEM = "item_smn_ticket_19138"
MISSIONS = (1411, 1415, 1419, 1428, 1437, 1446, 1451, 1456, 1460, 1465, 1469)


@pytest.fixture
def case(bundle, tmp_path):
    assert bundle.item_summon_tickets is not None, bundle.item_summon_error
    bootstrap, players, presents = copy(bundle.bootstrap), copy(bundle.player_state), copy(bundle.presents)
    bootstrap.clock = lambda: NOW
    service = copy(bundle.item_summon_tickets)
    service.bootstrap, service.players, service.presents = bootstrap, players, presents
    service.grants = copy(service.grants)
    service.grants.players = players
    service.grants.units = copy(service.grants.units)
    service.grants.units.players = players
    calls = []
    def draw(total):
        calls.append(total)
        return 0
    service.randbelow = draw
    presents.bootstrap, presents.players, presents.item_tickets = bootstrap, players, service
    if presents.tickets is not None:
        presents.tickets = copy(presents.tickets)
        presents.tickets.bootstrap, presents.tickets.players, presents.tickets.presents = bootstrap, players, presents
        presents.tickets.grants = copy(presents.tickets.grants)
        presents.tickets.grants.players = players
        presents.tickets.grants.units = copy(presents.tickets.grants.units)
        presents.tickets.grants.units.players = players
    if presents.selection_tickets is not None:
        selection = copy(presents.selection_tickets)
        selection.bootstrap, selection.players, selection.presents = bootstrap, players, presents
        selection.grants = copy(selection.grants)
        selection.grants.players = players
        presents.selection_tickets = selection
    missions = copy(bundle.missions)
    missions.bootstrap, missions.players, missions.presents, missions.item_tickets = bootstrap, players, presents, service
    bootstrap.present_provider = presents.response
    initial = bundle.initialize(players.initialize(bootstrap.initial_state()))
    initial["sentinel"] = {"unchanged": [1]}
    return SimpleNamespace(bundle=bundle, bootstrap=bootstrap, players=players, presents=presents,
        service=service, missions=missions, calls=calls, initial=initial, directory=tmp_path / "http")


def app_for(case, state=None):
    app = create_app(case.directory, initial_state=case.initial if state is None else state,
        state_initializer=case.bundle.initialize,
        handlers=case.bootstrap.handlers() | case.players.handlers() | case.presents.handlers() | case.missions.handlers())
    case.store = app.state.store
    return app


def post(client, envelope, expected=200):
    response = client.post("/api/" + envelope.profile.route,
        content=encode_wire(envelope.xml, envelope.profile.request_key))
    assert response.status_code == expected, response.content
    if expected != 200:
        return response.json()["error"]
    document = loads(decode_wire(response.content, envelope.profile.response_key))
    return response.content, {node.name: node for node in document.children}


def claim(case, ids, *, number=1, tick=1000):
    return request(case.store, number, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", tuple(ids)),)),)),), tick=tick)


def task_request(case, operation, identifier=0, *, period=4, tab=0, group=0, tick=900):
    return request(case.store, operation, (Container("GL", tuple(Column(name, "I", (value,)) for name, value in (
        ("ID", identifier), ("PT", period), ("TabGroupId", tab), ("TaskGroupId", group)))),), tick=tick)


def ticket_reward(amount=1):
    return {"kind": "item", "item_id": ITEM, "amount": amount, "description": "ticket"}


def column_rows(group):
    return [dict(zip((column.name for column in group.children), values))
            for values in zip(*(column.values for column in group.children))]


def assert_ten_units(case, before, result):
    after = case.store.profile()
    assert set(result) == {"SYSTEM", "PBP", "PPU"}
    assert len(result["PPU"].children) == 21
    assert [column.name for column in result["PPU"].children] == [alias for _, alias in UNIT_FIELDS]
    assert all(len(column.values) == 10 for column in result["PPU"].children)
    old = before.state["player_state"]
    player = after.state["player_state"]
    added = player["units"][-10:]
    assert player["units"][:-10] == old["units"]
    assert [unit["GetIndex"] for unit in added] == list(range(old["battle"]["UX"] + 1, old["battle"]["UX"] + 11))
    assert len({unit["UniqueID"] for unit in player["units"]}) == len(player["units"])
    assert result["PBP"].children[0].values == ("UX",)
    assert result["PBP"].children[1].values == (player["battle"]["UX"],)
    pool = case.service.grants.validate_pool(428)
    candidate = pool.candidates[0]
    for unit in added:
        assert unit == case.service.grants.expected_unit(428, 0, unit["GetIndex"], unit["UniqueID"])
        assert unit["CardID"] == candidate.card_id
    for table in ("game_param", "game_param2", "crystals", "possessions", "card_status", "config"):
        assert after.state["tables"][table] == before.state["tables"][table]
    assert after.state["sentinel"] == before.state["sentinel"]
    assert len(case.calls) == 10
    return after


@pytest.mark.parametrize("identifier", MISSIONS)
def test_all_eleven_real_task_rewards_enqueue_draw_and_reopen(case, identifier):
    rewards = case.missions._rewards((identifier,))
    assert rewards[0]["kind"] == "item" and rewards[0]["item_id"] == ITEM
    initial, mail = case.presents.enqueue(case.initial, rewards)
    assert len(mail) == 1 and mail[0]["Type"] == 22 and mail[0]["Content"] == 1 and mail[0]["ItemID"] == ITEM
    app = app_for(case, initial)
    envelope = claim(case, (mail[0]["PresentID"],))
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, listing = post(client, request(case.store, 42, ()))
        assert next(column.values for column in listing["PPT"].children if column.name == "A6") == (ITEM,)
        wire, result = post(client, envelope)
        saved = assert_ten_units(case, before, result)
        assert saved.state["tables"]["presents"] == []
        _, units = post(client, request(case.store, 41, ()))
        assert len(units["A1"].values) == len(saved.state["player_state"]["units"])
        _, empty = post(client, request(case.store, 42, ()))
        assert all(not column.values for column in empty["PPT"].children)
    replacement = ItemSummonTicketService(case.bootstrap, case.players, case.presents,
        case.service.pools, case.service.catalog, randbelow=lambda _: pytest.fail("Replay drew again"))
    case.service, case.presents.item_tickets, case.missions.item_tickets = replacement, replacement, replacement
    reopened = app_for(case, initial)
    assert case.store.profile() == saved
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved
        assert post(client, claim(case, (mail[0]["PresentID"],), tick=1001), 409)["code"] == "present_not_found"
        assert case.store.profile() == saved


def complete_dependencies(case, state, identifier):
    result, _, timestamp, shifted, active, indexed = case.missions.progress._context(state, NOW)
    definition = case.missions.catalog.by_id[identifier]
    dependencies = case.missions.catalog.dependencies(definition, NOW)
    assert dependencies
    for key in dependencies:
        row = case.missions.progress._entry(indexed, active[key], shifted)
        row.update(progress=row["goal"], status=1)
        indexed[(key, row["period"])] = row
    result["mission_state"]["entries"] = [indexed[key] for key in sorted(indexed)]
    result["mission_state"]["last_time"] = timestamp
    status = next(row for row in case.missions.progress.snapshot(result, NOW) if row["ID"] == identifier)
    assert status["Status"] == 1
    return result


def test_real_aggregate_mission_58_claim_then_ten_pull(case):
    initial = complete_dependencies(case, case.initial, MISSIONS[0])
    app = app_for(case, initial)
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = task_request(case, 58, MISSIONS[0])
        wire, response = post(client, envelope)
        assert set(response) == {"SYSTEM", "GLRW", "PPT"}
        mail = case.presents.rows(case.store.profile().state)
        assert len(mail) == 1 and mail[0]["ItemID"] == ITEM
        mission_saved = case.store.profile()
        assert mission_saved.state["player_state"] == before.state["player_state"]
        assert post(client, envelope)[0] == wire
        _, snapshot = post(client, request(case.store, 54, ()))
        row = next(row for row in column_rows(snapshot["GL"]) if row["ID"] == MISSIONS[0])
        assert row["Status"] == 2
        _, result = post(client, claim(case, (mail[0]["PresentID"],)))
        assert_ten_units(case, mission_saved, result)
        assert case.store.profile().state["tables"]["presents"] == []


def test_multiple_coupons_split_into_one_ticket_per_mail(case):
    initial, mail = case.presents.enqueue(case.initial, [ticket_reward(3)])
    assert len(mail) == 3 and len({row["PresentID"] for row in mail}) == 3
    assert all(row["Content"] == 1 and row["Type"] == 22 and row["ItemID"] == ITEM for row in mail)
    app = app_for(case, initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, claim(case, (mail[0]["PresentID"],)))
        assert len(case.calls) == 10
        assert case.store.profile().state["tables"]["presents"] == mail[1:]


@pytest.mark.parametrize("available", [0, 1, 9])
def test_less_than_ten_spaces_retains_ticket_and_never_draws(case, available):
    initial, mail = case.presents.enqueue(case.initial, [ticket_reward()])
    case.players.capacity_provider = lambda state: available
    app = app_for(case, initial)
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert post(client, claim(case, (mail[0]["PresentID"],)), 409)["code"] == "unit_roster_capacity"
    assert case.store.profile() == before
    assert case.calls == []


def test_single_claim_mixing_a_ticket_is_rejected_before_random(case):
    initial, mail = case.presents.enqueue(case.initial, [ticket_reward(), {"kind": "gold", "amount": 10, "description": "other"}])
    app = app_for(case, initial)
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        error = post(client, claim(case, [row["PresentID"] for row in mail]), 501)
    assert error["code"] == "item_summon_single_required"
    assert case.store.profile() == before and case.calls == []


@pytest.mark.parametrize("mixed", [False, True])
def test_batch_keeps_tickets_for_their_single_claim_without_random(case, mixed):
    initial, mail = case.presents.enqueue(case.initial, [ticket_reward(), {"kind": "gold", "amount": 10, "description": "other"}])
    app = app_for(case, initial)
    ids = [row["PresentID"] for row in mail] if mixed else [mail[0]["PresentID"]]
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        wire, result = post(client, claim(case, ids, number=117))
        saved = case.store.profile()
        assert case.presents.rows(saved.state) == (mail[:1] if mixed else mail) and case.calls == []
        if mixed:
            assert set(result) == {"SYSTEM", "PGP", "PPU"}
            assert saved.state["tables"]["game_param"][0]["Money"] == before.state["tables"]["game_param"][0]["Money"] + 10
            assert saved.revision == before.revision + 1
        else:
            assert set(result) == {"SYSTEM", "PPU"} and saved == before
        assert all(not column.values for column in result["PPU"].children)
        assert post(client, claim(case, ids, number=117))[0] == wire and case.store.profile() == saved
        assert_ten_units(case, saved, post(client, claim(case, ids[:1], tick=1001))[1])


def test_content_larger_than_one_never_multiplies_draws(case):
    initial, mail = case.presents.enqueue(case.initial, [ticket_reward()])
    initial["tables"]["presents"][0]["Content"] = 2
    app = app_for(case, initial)
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert post(client, claim(case, (mail[0]["PresentID"],)), 501)["code"] == "item_summon_mail_unsupported"
    assert case.store.profile() == before and case.calls == []


def test_all_other_twenty_seven_task_tickets_stay_unavailable_and_atomic(case):
    definitions = [row for row in case.missions.catalog.definitions if row.reward_item_type == 22
        and case.service.catalog.supports_item(row.reward_item_id) and row.reward_item_id != ITEM]
    assert len(definitions) == 27
    original = deepcopy(case.initial)
    for definition in definitions:
        rewards = [{"kind": "gold", "amount": 25, "description": "first"}, *case.missions._rewards((definition.condition_id,))]
        with pytest.raises(ProtocolError) as error:
            case.presents.enqueue(case.initial, rewards)
        assert error.value.status == 501
        assert case.initial == original


def test_monthly_59_keeps_incomplete_ticket_claimable_and_claims_other_rewards(case):
    state = case.initial
    for _ in range(30):
        state = case.missions.record(state, "quest_clear", {"quest_id": 1001, "quest_type": 0, "stars": 3, "hard": False}, NOW)
    app = app_for(case, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, result = post(client, task_request(case, 59, period=2, tab=2))
        claimed = result["GLRW"].children[0].values
        saved = case.store.profile()
        statuses = {row["ID"]: row["Status"] for row in case.missions.progress.snapshot(saved.state, NOW)}
        assert claimed and 1634 not in claimed and statuses[1634] == 1
        assert all(statuses[identifier] == 2 for identifier in claimed)
        incomplete = case.missions.catalog.by_id[1634].reward_item_id
        assert all(row.get("ItemID") != incomplete for row in case.presents.rows(saved.state))
    assert case.calls == []


def test_default_and_disabled_rule_configuration_preserve_old_mail(case, tmp_path):
    assert load_item_ticket_rules(None).enabled_pools == DEFAULT_ITEM_TICKET_POOLS
    path = tmp_path / "disabled-item-tickets.json"
    path.write_text(json.dumps({"enabled_pools": []}), encoding="utf-8")
    rules = load_item_ticket_rules(path)
    assert rules.enabled_pools == ()
    initial, mail = case.presents.enqueue(case.initial, [ticket_reward()])
    disabled = ItemSummonTicketService(case.bootstrap, case.players, case.presents, case.service.pools,
        case.service.catalog, rules=rules, randbelow=lambda _: pytest.fail("Disabled pool drew"))
    case.service = disabled
    case.presents.item_tickets = disabled
    case.missions.item_tickets = disabled
    app = app_for(case, initial)
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, response = post(client, request(case.store, 42, ()))
        assert next(column.values for column in response["PPT"].children if column.name == "A6") == (ITEM,)
        assert post(client, claim(case, (mail[0]["PresentID"],)), 501)["code"] == "summon_ticket_pool_disabled"
    assert case.store.profile() == before


def test_alias_one_and_two_return_same_receipt(case):
    initial, mail = case.presents.enqueue(case.initial, [ticket_reward()])
    app = app_for(case, initial)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        one, two = claim(case, (mail[0]["PresentID"],), number=1), claim(case, (mail[0]["PresentID"],), number=2)
        assert one.profile == two.profile and one.xml == two.xml
        wire, _ = post(client, one)
        saved = case.store.profile()
        assert post(client, two)[0] == wire
        assert case.store.profile() == saved and len(case.calls) == 10


@pytest.mark.parametrize("number", [1, 41, 42])
def test_preflight_encoding_failure_does_not_consume_mail(case, number):
    initial, mail = case.presents.enqueue(case.initial, [ticket_reward()])
    case.service._profiles = dict(case.service._profiles)
    case.service._profiles[number] = replace(case.service._profiles[number], response_limit=1)
    app = app_for(case, initial)
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert post(client, claim(case, (mail[0]["PresentID"],)), 500)["code"] == "response_too_large"
    assert case.store.profile() == before


def test_cas_conflict_keeps_same_ten_draws_and_merges_other_state(case, monkeypatch):
    initial, mail = case.presents.enqueue(case.initial, [ticket_reward()])
    app = app_for(case, initial)
    original = case.store.update_state
    attempts = 0
    def conflict(state, revision):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            profile = case.store.profile()
            other = deepcopy(profile.state)
            other["sentinel"]["unchanged"].append(2)
            original(other, profile.revision)
            raise ValueError("conflict")
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, claim(case, (mail[0]["PresentID"],)))
    assert attempts == 2 and len(case.calls) == 10
    assert case.store.profile().state["sentinel"]["unchanged"] == [1, 2]
    assert len(case.store.profile().state["player_state"]["units"]) == len(initial["player_state"]["units"]) + 10


def test_exactly_ten_available_slots_allows_complete_draw(case):
    initial, mail = case.presents.enqueue(case.initial, [ticket_reward()])
    case.players.capacity_provider = lambda state: 10
    app = app_for(case, initial)
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, result = post(client, claim(case, (mail[0]["PresentID"],)))
    assert_ten_units(case, before, result)


def test_legacy_incomplete_pool_mail_is_readable_but_not_consumed(case):
    names = sorted({row.reward_item_id for row in case.missions.catalog.definitions if row.reward_item_type == 22
        and case.service.catalog.supports_item(row.reward_item_id) and row.reward_item_id != ITEM})
    assert len(names) == 5
    initial, mail = case.presents.enqueue(case.initial, [ticket_reward(len(names))])
    for row, name in zip(initial["tables"]["presents"], names):
        row["ItemID"] = name
    app = app_for(case, initial)
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, result = post(client, request(case.store, 42, ()))
        assert next(column.values for column in result["PPT"].children if column.name == "A6") == tuple(names)
        for index, row in enumerate(mail):
            assert post(client, claim(case, (row["PresentID"],), tick=1000 + index), 501)["code"] == "summon_ticket_pool_disabled"
            assert case.store.profile() == before
    assert case.calls == []


def test_changed_multi_draw_receipt_never_replays_different_results(case):
    initial, mail = case.presents.enqueue(case.initial, [ticket_reward()])
    app = app_for(case, initial)
    envelope = claim(case, (mail[0]["PresentID"],))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, envelope)
        altered = case.store.profile().state
        altered["item_summon_history"]["receipts"][0]["candidate_indices"][1] = 1
        with case.store.connection() as connection:
            connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(altered),))
        before = case.store.profile()
        assert post(client, envelope, 503)["code"] == "item_summon_history_invalid"
        assert case.store.profile() == before
    assert len(case.calls) == 10


@pytest.mark.parametrize("item_id", ["x" * 129, "x" * 255, "券" * 43, "券" * 85],
    ids=["ascii129", "ascii255", "utf8-129", "utf8-255"])
def test_long_item_identity_claim_and_restart_retry_preserve_full_name(case, item_id):
    from aigis_ps.item_summon_catalog import ItemSummonCatalog
    from aigis_ps.possessions import PossessionService
    from aigis_ps.present_items import PresentItemCatalog
    from test_item_summon_catalog import payloads, row as item_row

    assert len(item_id.encode("utf-8")) in (129, 255)
    source = payloads([item_row(item_id)])
    catalog = ItemSummonCatalog(case.service.pools, source.__getitem__)
    possessions = PossessionService(case.bootstrap.asset_root, asset_reader=source.__getitem__)
    case.presents.possessions = possessions
    case.presents.item_catalog = PresentItemCatalog(possessions, source["ItemConfigTable.atb"])
    service = ItemSummonTicketService(case.bootstrap, case.players, case.presents,
        case.service.pools, catalog, randbelow=case.service.randbelow)
    case.service, case.presents.item_tickets = service, service
    case.bootstrap.present_provider = case.presents.response
    initial, mail = case.presents.enqueue(case.initial, [{
        "kind": "item", "item_id": item_id, "amount": 1, "description": "long identity"}])

    def application():
        app = create_app(case.directory, initial_state=initial,
            handlers=case.bootstrap.handlers() | case.players.handlers() | case.presents.handlers())
        case.store = app.state.store
        return app

    app = application()
    envelope = claim(case, (mail[0]["PresentID"],))
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, listing = post(client, request(case.store, 42, ()))
        assert next(column.values for column in listing["PPT"].children if column.name == "A6") == (item_id,)
        wire, result = post(client, envelope)
        saved = assert_ten_units(case, before, result)
        receipt = saved.state["item_summon_history"]["receipts"][0]
        assert receipt["item_id"] == item_id
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved
    replacement = ItemSummonTicketService(case.bootstrap, case.players, case.presents,
        service.pools, catalog, randbelow=lambda _: pytest.fail("Long identity retry drew again"))
    case.service, case.presents.item_tickets = replacement, replacement
    reopened = application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved


@pytest.mark.parametrize("item_id,expected_count", [("item_smn_ticket_85127", 1), ("item_smn_ticket_85128", 1)])
def test_other_default_pool428_items_grant_their_actual_count(case, item_id, expected_count):
    assert {name for name in case.service.status()["enabled_items"]
            if case.service.catalog.definition(name).pool_id == 428} == {
        "item_smn_ticket_19138", "item_smn_ticket_85127", "item_smn_ticket_85128"}
    definition = case.service.validate_item(item_id)
    assert definition.pool_id == 428 and definition.count == expected_count
    initial, mail = case.presents.enqueue(case.initial, [{
        "kind": "item", "item_id": item_id, "amount": 1, "description": "pool428 single"}])
    assert len(mail) == 1 and mail[0]["Content"] == 1 and mail[0]["ItemID"] == item_id
    app = app_for(case, initial)
    envelope = claim(case, (mail[0]["PresentID"],))
    before = case.store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        wire, response = post(client, envelope)
        assert set(response) == {"SYSTEM", "PBP", "PPU"}
        assert len(response["PPU"].children) == 21
        assert all(len(column.values) == expected_count for column in response["PPU"].children)
        saved = case.store.profile()
        player = saved.state["player_state"]
        assert player["units"][:-expected_count] == before.state["player_state"]["units"]
        added = player["units"][-expected_count:]
        for unit in added:
            assert unit == case.service.grants.expected_unit(428, 0, unit["GetIndex"], unit["UniqueID"])
        assert player["battle"]["UX"] == before.state["player_state"]["battle"]["UX"] + expected_count
        assert response["PBP"].children[1].values == (player["battle"]["UX"],)
        assert saved.state["tables"]["presents"] == []
        for table in ("game_param", "game_param2", "crystals", "possessions", "card_status"):
            assert saved.state["tables"][table] == before.state["tables"][table]
        assert len(case.calls) == expected_count
        assert post(client, envelope)[0] == wire
        assert case.store.profile() == saved and len(case.calls) == expected_count
