from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.app import create_app
from aigis_ps.barracks_service import EXTRA_SHIFT, PURCHASE_FIELD
from aigis_ps.crystal_wallet import credit_free_crystals, wallet_totals
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import BARRACKS_BITS, BARRACKS_SHIFT
from aigis_ps.protocol import decode_wire, encode_wire
from aigis_ps.silver_house import SilverHouseRules, SilverHouseService
from aigis_ps.storage import Store
import aigis_ps.player_state as player_state_module
from test_presents import DAY, crystal, unit
from test_quest_service import bundle, request, setup, start_request, finish_request, allow_quest
from test_silver_house_integration import deposit_request


def roster(bundle, count):
    saved = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    if count > 1:
        saved, _ = bundle.player_state.credit_units(saved, [{"card_id": 3, "level": 1, "count": count - 1}])
    return saved


def capacity_error(store, action, code="unit_roster_capacity"):
    before = store.profile()
    with pytest.raises(ProtocolError) as caught:
        action()
    assert caught.value.code == code
    assert store.profile() == before


def take_fairy(store, identifier, count, tick=1000):
    return request(store, 125, (Container("Draw", (
        Column("CardID", "I", (identifier,)), Column("Count", "I", (count,)))),), tick=tick)


def take_silver(store, identifier=8, tick=1000):
    return request(store, 128, (Container("Draw", (Column("CardID", "I", (identifier,)),)),), tick=tick)


def take_presents(store, identifiers, number=117):
    return request(store, number, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", tuple(identifiers)),)),)),))


def test_fairy_withdrawal_checks_the_whole_batch_and_preserves_stock(bundle, tmp_path):
    saved = roster(bundle, 755)
    card = bundle.fairy_farm.card_ids[0]
    saved["fairy_farm_schema"] = 1
    saved["tables"]["fairy_farm"] = [{"CardID": card, "Count": 2}]
    store = Store(tmp_path / "fairy.sqlite3", saved)
    capacity_error(store, lambda: bundle.fairy_farm.withdraw(take_fairy(store, card, 2), store))
    envelope = take_fairy(store, card, 1)
    result = bundle.fairy_farm.withdraw(envelope, store)
    assert bundle.unit_capacity.usage(store.profile().state).counts == (756,)
    assert next(row["Count"] for row in bundle.fairy_farm.read(store.profile().state) if row["CardID"] == card) == 1
    before = store.profile()
    assert bundle.fairy_farm.withdraw(envelope, store) == result and store.profile() == before


def test_concurrent_last_slot_can_only_be_withdrawn_once(bundle, tmp_path):
    saved = roster(bundle, 755)
    cards = bundle.fairy_farm.card_ids[:2]
    saved["fairy_farm_schema"] = 1
    saved["tables"]["fairy_farm"] = [{"CardID": card, "Count": 1} for card in cards]
    store = Store(tmp_path / "concurrent.sqlite3", saved)
    envelopes = [take_fairy(store, card, 1) for card in cards]
    def withdraw(envelope):
        try:
            bundle.fairy_farm.withdraw(envelope, store)
            return "ok"
        except ProtocolError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(withdraw, envelopes))
    assert sorted(outcomes) == ["ok", "unit_roster_capacity"]
    assert bundle.unit_capacity.usage(store.profile().state).counts == (756,)
    assert sum(row["Count"] for row in bundle.fairy_farm.read(store.profile().state)) == 1


def test_extra_barracks_units_do_not_consume_primary_slots(bundle, tmp_path):
    saved = roster(bundle, 757)
    saved["tables"]["config"][0][PURCHASE_FIELD] = 1 << EXTRA_SHIFT
    for row in saved["player_state"]["units"][1:]:
        row["GeneralPurposeFlags"] |= 1 << BARRACKS_SHIFT
    saved["silver_house_schema"] = 1
    saved["tables"]["silver_house"] = [{"CardID": 8, "Count": 1}]
    store = Store(tmp_path / "extra.sqlite3", saved)
    bundle.silver_house.withdraw(take_silver(store), store)
    assert bundle.unit_capacity.usage(store.profile().state).counts == (2, 756)
    assert store.profile().state["player_state"]["units"][-1]["GeneralPurposeFlags"] & BARRACKS_BITS == 0


def test_full_or_unmodeled_barracks_can_be_read_but_not_withdrawn_into(bundle, tmp_path):
    for kind in ("full", "unknown"):
        saved = roster(bundle, 800 if kind == "full" else 1)
        if kind == "unknown":
            saved["player_state"]["units"][0]["GeneralPurposeFlags"] |= 1 << BARRACKS_SHIFT
        saved["silver_house_schema"] = 1
        saved["tables"]["silver_house"] = [{"CardID": 8, "Count": 1}]
        store = Store(tmp_path / f"{kind}.sqlite3", saved)
        assert bundle.player_state.unit_response(bundle.player_state.validate(store.profile().state)["units"])
        code = "unit_roster_capacity" if kind == "full" else "unit_capacity_barracks_unsupported"
        capacity_error(store, lambda: bundle.silver_house.withdraw(take_silver(store), store), code)


def test_over_capacity_silver_deposit_with_equal_compensation_is_allowed(bundle, tmp_path):
    saved = roster(bundle, 799)
    saved, added = bundle.player_state.credit_units(saved, [{"card_id": 8, "level": 30, "count": 1}])
    store = Store(tmp_path / "deposit.sqlite3", saved)
    bundle.silver_house.deposit(deposit_request(store, added), store)
    assert bundle.unit_capacity.usage(store.profile().state).counts == (800,)
    assert store.profile().state["player_state"]["units"][-1]["CardID"] == 460
    assert bundle.silver_house.read(store.profile().state) == [{"CardID": 8, "Count": 1}]


@pytest.mark.parametrize("cross_barracks", [False, True])
def test_silver_compensation_cannot_increase_full_primary_barracks(bundle, tmp_path, cross_barracks):
    saved = roster(bundle, 756 if cross_barracks else 755)
    saved, added = bundle.player_state.credit_units(saved, [{"card_id": 8, "level": 30, "count": 1}])
    if cross_barracks:
        saved["tables"]["config"][0][PURCHASE_FIELD] = 1 << EXTRA_SHIFT
        saved["player_state"]["units"][-1]["GeneralPurposeFlags"] |= 1 << BARRACKS_SHIFT
    service = SilverHouseService(bundle.bootstrap, bundle.player_state, bundle.gifts.catalog, bundle.gifts,
        lambda name: bundle.native_cdn.assets.read(name)[0], rules=SilverHouseRules(reward_count=1 if cross_barracks else 2))
    store = Store(tmp_path / "compensation.sqlite3", saved)
    capacity_error(store, lambda: service.deposit(deposit_request(store, added), store))


def test_cross_barracks_net_removal_can_free_the_technical_limit(bundle, tmp_path, monkeypatch):
    saved = roster(bundle, 3)
    saved, added = bundle.player_state.credit_units(saved, [
        {"card_id": 8, "level": 30, "count": 1}, {"card_id": 3, "level": 1, "count": 1}])
    saved["tables"]["config"][0][PURCHASE_FIELD] = 1 << EXTRA_SHIFT
    for row in saved["player_state"]["units"][-2:]:
        row["GeneralPurposeFlags"] |= 1 << BARRACKS_SHIFT
    store = Store(tmp_path / "net-removal.sqlite3", saved)
    monkeypatch.setattr(player_state_module, "MAX_PLAYER_UNITS", 5)
    bundle.silver_house.deposit(deposit_request(store, added), store)
    assert bundle.unit_capacity.usage(store.profile().state).counts == (4, 0)


def test_single_present_at_full_capacity_preserves_mail_and_wallet(bundle, tmp_path):
    saved, mails = bundle.presents.enqueue(roster(bundle, 756), [unit(), crystal()])
    store = Store(tmp_path / "single.sqlite3", saved)
    capacity_error(store, lambda: bundle.presents.receive(take_presents(store, [row["PresentID"] for row in mails], 1), store))


def test_partial_batch_uses_request_order_keeps_whole_skipped_rewards_and_replays(bundle, tmp_path):
    saved = credit_free_crystals(roster(bundle, 755), 997, DAY)
    saved, mails = bundle.presents.enqueue(saved, [unit(2), unit(3), crystal(5), crystal(2)])
    identifiers = [mails[index]["PresentID"] for index in (2, 1, 3, 0)]
    def application():
        return create_app(tmp_path / "partial", initial_state=saved,
            handlers=bundle.presents.handlers() | bundle.bootstrap.handlers() | bundle.player_state.handlers())
    app = application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = take_presents(app.state.store, identifiers)
        body = encode_wire(envelope.xml, envelope.profile.request_key)
        response = client.post("/api/" + envelope.profile.route, content=body)
        assert response.status_code == 200, response.text
        tree = {child.name: child for child in loads(decode_wire(response.content, envelope.profile.response_key)).children}
        assert tree["PPU"].children[0].values == (57,)
        profile = app.state.store.profile()
        assert wallet_totals(profile.state)["free"] == 999
        assert [row["PresentID"] for row in bundle.presents.rows(profile.state)] == [mails[0]["PresentID"], mails[2]["PresentID"]]
        assert bundle.unit_capacity.usage(profile.state).counts == (756,)
    reopened = application()
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        replay = client.post("/api/" + envelope.profile.route, content=body)
        assert replay.status_code == 200 and replay.content == response.content
        assert reopened.state.store.profile() == profile


def test_entire_blocked_batch_keeps_mail_and_returns_empty_ppu(bundle, tmp_path):
    saved, mails = bundle.presents.enqueue(roster(bundle, 756), [unit(2), unit(3)])
    store = Store(tmp_path / "blocked.sqlite3", saved)
    envelope = take_presents(store, [row["PresentID"] for row in mails])
    response = bundle.presents.receive(envelope, store)
    ppu = next(child for child in loads(response).children if child.name == "PPU")
    assert all(not column.values for column in ppu.children)
    assert bundle.presents.rows(store.profile().state) == mails
    assert store.profile().state["player_state"] == saved["player_state"]
    assert bundle.presents.receive(envelope, store) == response


def test_missing_native_capacity_blocks_unit_claims_without_blocking_currency(bundle, tmp_path, monkeypatch):
    saved, mails = bundle.presents.enqueue(roster(bundle, 1), [unit(), crystal(1)])
    store = Store(tmp_path / "missing.sqlite3", saved)
    def unavailable(state):
        raise ProtocolError("unit_capacity_unavailable", "fixture", 503)
    monkeypatch.setattr(bundle.player_state, "capacity_provider", unavailable)
    capacity_error(store, lambda: bundle.presents.receive(take_presents(store, [mails[0]["PresentID"]], 1), store), "unit_capacity_unavailable")
    bundle.presents.receive(take_presents(store, [mails[1]["PresentID"]], 1), store)
    assert wallet_totals(store.profile().state)["free"] == 1


def test_full_barracks_blocks_battle_before_entry_cost(setup):
    service, store = setup
    profile = store.profile()
    saved, _ = service.players.credit_units(profile.state, [{"card_id": 3, "level": 1, "count": 755}])
    store.update_state(saved, profile.revision)
    capacity_error(store, lambda: service.start(start_request(service, store), store))


def test_battle_started_with_one_slot_awards_all_drops_then_blocks_next_entry(setup):
    service, store = setup
    allow_quest(store, 1001)
    profile = store.profile()
    saved, _ = service.players.credit_units(profile.state, [{"card_id": 3, "level": 1, "count": 754}])
    store.update_state(saved, profile.revision)
    service.start(start_request(service, store, 1002), store)
    finish = finish_request(store, 1002, obtained=7, selected=7)
    response = service.finish(finish, store)
    assert len(store.profile().state["player_state"]["units"]) > 756
    profile = store.profile()
    assert service.finish(finish, store) == response and store.profile() == profile
    capacity_error(store, lambda: service.start(start_request(service, store, 1002, tick=1002), store))


def test_technical_reward_preflight_does_not_charge_or_advance_identity(setup, monkeypatch):
    service, store = setup
    allow_quest(store, 1001)
    before = store.profile()
    def fail(state, grants):
        assert grants
        raise ProtocolError("unit_roster_capacity", "fixture", 409)
    monkeypatch.setattr(service.players, "credit_units", fail)
    capacity_error(store, lambda: service.start(start_request(service, store, 1002), store))
    assert store.profile() == before


@pytest.mark.parametrize("counter", ["UX", "last_unique_id"])
def test_inflight_rewards_keep_identity_room_while_other_presents_are_claimed(setup, bundle, counter):
    service, store = setup
    allow_quest(store, 1001)
    profile = store.profile()
    grants = service._preflight_unit_rewards(profile.state, service.catalog.get(1002), 0, service.rules)
    reserved = sum(row["count"] for row in grants)
    container = profile.state["player_state"]["battle"] if counter == "UX" else profile.state["player_state"]
    container[counter] = 0x7FFFFFFF - reserved
    saved, mails = bundle.presents.enqueue(profile.state, [unit()])
    store.update_state(saved, profile.revision)
    service.start(start_request(service, store, 1002), store)
    assert sum(row["count"] for row in store.profile().state["quest_state"]["active"]["unit_reservations"]) == reserved
    capacity_error(store, lambda: bundle.presents.receive(take_presents(store, [mails[0]["PresentID"]], 1), store))
    before = store.profile()
    capacity_error(store, lambda: service.players.credit_units(before.state, [{"card_id": 3, "level": 1, "count": 1}]), "unit_identity_exhausted")
    service.finish(finish_request(store, 1002, obtained=7, selected=7), store)
    after = store.profile().state
    assert after["quest_state"]["active"] is None
    assert bundle.presents.rows(after) == mails
    result = after["player_state"]["battle"] if counter == "UX" else after["player_state"]
    assert result[counter] == 0x7FFFFFFF


def test_inflight_rewards_keep_technical_row_room(setup, bundle, monkeypatch):
    service, store = setup
    allow_quest(store, 1001)
    profile = store.profile()
    grants = service._preflight_unit_rewards(profile.state, service.catalog.get(1002), 0, service.rules)
    reserved = sum(row["count"] for row in grants)
    monkeypatch.setattr(player_state_module, "MAX_PLAYER_UNITS", reserved + 1)
    service.start(start_request(service, store, 1002), store)
    saved = store.profile().state
    assert service.players.available_unit_slots(saved) == 0
    capacity_error(store, lambda: service.players.credit_units(saved, [{"card_id": 3, "level": 1, "count": 1}]))
    service.finish(finish_request(store, 1002, obtained=7, selected=7), store)
    assert len(store.profile().state["player_state"]["units"]) == reserved + 1


@pytest.mark.parametrize("restart", [False, True])
def test_legacy_active_battle_can_finish_or_restart_without_reservations(setup, restart):
    service, store = setup
    allow_quest(store, 1001)
    service.start(start_request(service, store, 1002), store)
    profile = store.profile()
    del profile.state["quest_state"]["active"]["unit_reservations"]
    store.update_state(profile.state, profile.revision)
    saved = store.profile().state
    assert service.players.unit_response(saved["player_state"]["units"])
    capacity_error(store, lambda: service.players.credit_units(saved, [{"card_id": 3, "level": 1, "count": 1}]), "quest_reservation_unavailable")
    if restart:
        service.start(start_request(service, store, 1002, tick=1002), store)
        assert store.profile().state["quest_state"]["active"]["unit_reservations"]
    else:
        service.finish(finish_request(store, 1002, obtained=7, selected=7), store)
        assert store.profile().state["quest_state"]["active"] is None
