from copy import copy, deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import base64
import json

import pytest
from fastapi.testclient import TestClient

import aigis_ps.presents as presents_module
from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.app import create_app
from aigis_ps.business_calendar import civil_day
from aigis_ps.crystal_wallet import credit_free_crystals, credit_paid_crystals, wallet_totals
from aigis_ps.energy_clock import anchor_seconds, energy_seconds, set_anchor
from aigis_ps.errors import ProtocolError
from aigis_ps.presents import PresentService
from aigis_ps.recovery_service import RecoveryRules, RecoveryService
from test_evolution_integration import post
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)
COOK = "item_cook_logbo_00001"
EVENT_COOK = "item_sample_cook_001"
BOUQUET = "gift_0000_bouquet"


def services(bundle, now=NOW, *, limit=2000):
    clock = SimpleNamespace(now=now)
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: clock.now
    recovery = RecoveryService(bootstrap, rules=RecoveryRules(charisma_limit=limit))
    presents = PresentService(bootstrap, bundle.player_state,
        asset_reader=lambda name: bundle.native_cdn.assets.read(name)[0],
        possessions=bundle.possessions, recovery=recovery)
    bootstrap.present_provider = presents.response
    return SimpleNamespace(bootstrap=bootstrap, recovery=recovery, presents=presents,
                           clock=clock, players=bundle.player_state, possessions=bundle.possessions)


def game(state):
    return state["tables"]["game_param"][0]


def seed(case, *, current=20, extended=0):
    state = case.players.initialize(case.bootstrap.initial_state())
    game(state).update(Money=1000, NowCharisma=current, ExtendedMaxCharisma=extended)
    day = civil_day(case.clock.now)
    state = credit_free_crystals(state, 2, day)
    state = credit_paid_crystals(state, 3, day)
    return case.possessions.credit(state, {BOUQUET: 7})


def reward(item_id=None):
    result = {"kind": "charisma_recovery", "description": "カリスマ回復の贈り物"}
    if item_id is not None:
        result["item_id"] = item_id
    return result


def application(case, path, state):
    return create_app(path, initial_state=state,
        handlers=case.bootstrap.handlers() | case.players.handlers() | case.presents.handlers())


def claim(store, operation, identifiers, *, tick=1000):
    return request(store, operation, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", tuple(identifiers)),)),)),), tick=tick)


def columns(node):
    return {column.name: column.values for column in node.children}


def reject(client, store, envelope, status, code):
    before = store.profile()
    response = post(client, envelope, expected=status)
    assert response.json()["error"]["code"] == code
    assert store.profile() == before


@pytest.mark.parametrize("item_id", [None, COOK, EVENT_COOK], ids=["type8", "cook", "event_cook"])
@pytest.mark.parametrize("operation", [1, 2])
def test_one_recovery_present_updates_full_energy_without_assets_or_event_flags(bundle, tmp_path, item_id, operation):
    case = services(bundle)
    state = seed(case)
    state, mail = case.presents.enqueue(state, [reward(item_id)])
    assert mail[0]["Type"] == (8 if item_id is None else 22)
    assert mail[0]["Content"] == (0 if item_id is None else 1)
    assert mail[0].get("ItemID") == item_id
    original = deepcopy(state)
    amount = case.bootstrap.rank_rows[0]["CharismaMax"]
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, listing = post(client, request(app.state.store, 42, ()))
        assert columns(listing["PPT"])["A1"] == (mail[0]["Type"],)
        envelope = claim(app.state.store, operation, [mail[0]["PresentID"]])
        wire, result = post(client, envelope)
        assert set(result) == {"SYSTEM", "PGP", "PPU"}
        pgp = columns(result["PGP"])
        assert pgp["A1"] == tuple(f"A{index}" for index in range(1, 10))
        assert int(dict(zip(pgp["A1"], pgp["A2"]))["A4"]) == 20 + amount
        assert len(result["PPU"].children) == 21
        assert all(not column.values for column in result["PPU"].children)
        saved = app.state.store.profile()
        assert game(saved.state)["NowCharisma"] == 20 + amount
        assert anchor_seconds(game(saved.state)) == energy_seconds(NOW)
        assert saved.state["tables"]["crystals"] == original["tables"]["crystals"]
        assert saved.state["tables"]["possessions"] == original["tables"]["possessions"]
        assert saved.state["tables"]["perused_events"] == original["tables"]["perused_events"]
        assert saved.state.get("peruse_state") == original.get("peruse_state")
        assert saved.state["player_state"] == original["player_state"]
        assert not case.presents.rows(saved.state)
        assert post(client, envelope)[0] == wire and app.state.store.profile() == saved
        _, loaded = post(client, request(app.state.store, 34, ()))
        assert loaded["A4"].values == (20 + amount,)
        _, listing = post(client, request(app.state.store, 42, ()))
        assert all(not column.values for column in listing["PPT"].children)
    assert state == original


def test_recovery_receipt_reopens_without_consuming_another_mail_or_recovering_again(bundle, tmp_path):
    case = services(bundle)
    state, mail = case.presents.enqueue(seed(case), [reward(EVENT_COOK), reward(COOK)])
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = claim(app.state.store, 1, [mail[0]["PresentID"]])
        wire, _ = post(client, envelope)
        saved = app.state.store.profile()
    reopened_case = services(bundle, NOW + timedelta(minutes=10))
    reopened = application(reopened_case, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == saved
        assert [row["PresentID"] for row in reopened_case.presents.rows(saved.state)] == [mail[1]["PresentID"]]
        post(client, claim(reopened.state.store, 2, [mail[1]["PresentID"]], tick=1001))
        final = reopened.state.store.profile()
        amount = case.bootstrap.rank_rows[0]["CharismaMax"]
        assert game(final.state)["NowCharisma"] == 20 + amount * 2
        assert wallet_totals(final.state) == {"paid": 3, "free": 2, "total": 5}
        reject(client, reopened.state.store, envelope, 409, "present_replay_stale")


def test_cooking_content_is_metadata_and_does_not_multiply_recovery(bundle, tmp_path):
    case = services(bundle)
    state, mail = case.presents.enqueue(seed(case), [reward(COOK)])
    state["tables"]["presents"][0]["Content"] = 7
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, claim(app.state.store, 1, [mail[0]["PresentID"]]))
        assert game(app.state.store.profile().state)["NowCharisma"] == 20 + case.bootstrap.rank_rows[0]["CharismaMax"]


@pytest.mark.parametrize("limit,over", [(2000, 0), (2000, 1), (3000, 0)])
def test_recovery_obeys_shared_configured_cap_without_partial_mail_consumption(bundle, tmp_path, limit, over):
    case = services(bundle, limit=limit)
    amount = case.bootstrap.rank_rows[0]["CharismaMax"]
    state, mail = case.presents.enqueue(seed(case, current=limit - amount + over), [reward()])
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = claim(app.state.store, 1, [mail[0]["PresentID"]])
        if over:
            reject(client, app.state.store, envelope, 409, "recovery_cap_exceeded")
        else:
            post(client, envelope)
            assert game(app.state.store.profile().state)["NowCharisma"] == limit
            assert not case.presents.rows(app.state.store.profile().state)


@pytest.mark.parametrize("offset", [0, 480, 540])
def test_natural_recovery_and_extended_capacity_share_jst_instant_across_midnight(bundle, tmp_path, offset):
    moment = datetime(2026, 10, 31, 15, 1, tzinfo=timezone.utc)
    case = services(bundle, moment.astimezone(timezone(timedelta(minutes=offset))))
    state = seed(case, current=5, extended=7)
    set_anchor(game(state), energy_seconds(moment - timedelta(minutes=6)))
    state, mail = case.presents.enqueue(state, [reward(COOK)])
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, claim(app.state.store, 1, [mail[0]["PresentID"]]))
        saved = app.state.store.profile().state
        assert game(saved)["NowCharisma"] == 7 + case.bootstrap.rank_rows[0]["CharismaMax"] + 7
        assert anchor_seconds(game(saved)) == energy_seconds(moment)
        assert game(saved)["CharismaChangeDate"] == datetime(2026, 11, 1).toordinal() - 1
        assert saved["tables"]["crystals"] == state["tables"]["crystals"]


@pytest.mark.parametrize("failure", ["backwards", "unmarked"])
def test_invalid_energy_clock_keeps_recovery_mail_and_other_assets(bundle, tmp_path, failure):
    case = services(bundle)
    state, mail = case.presents.enqueue(seed(case), [reward(EVENT_COOK)])
    if failure == "backwards":
        set_anchor(game(state), energy_seconds(NOW) + 1)
        status, code = 409, "recovery_clock_backwards"
    else:
        state.pop("energy_clock")
        status, code = 503, "energy_clock_source_required"
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(client, app.state.store, claim(app.state.store, 1, [mail[0]["PresentID"]]), status, code)


@pytest.mark.parametrize("operation,selected", [(1, (0, 1)), (2, (1, 0)), (1, (0, 2))])
def test_recovery_cannot_be_received_through_mixed_selection(bundle, tmp_path, operation, selected):
    case = services(bundle)
    state, mail = case.presents.enqueue(seed(case), [reward(COOK),
        {"kind": "gold", "amount": 25, "description": "ゴールド"}, reward()])
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(client, app.state.store,
            claim(app.state.store, operation, [mail[index]["PresentID"] for index in selected]),
            409, "present_recovery_single_only")


@pytest.mark.parametrize("selected", [(0,), (0, 2), (1, 0, 2)])
def test_batch_keeps_recovery_mail_for_its_single_claim(bundle, tmp_path, selected):
    case = services(bundle)
    state, mail = case.presents.enqueue(seed(case), [reward(COOK),
        {"kind": "gold", "amount": 25, "description": "ゴールド"}, reward()])
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        envelope = claim(app.state.store, 117, [mail[index]["PresentID"] for index in selected])
        wire, result = post(client, envelope)
        saved = app.state.store.profile()
        assert game(saved.state)["NowCharisma"] == game(before.state)["NowCharisma"]
        assert [row["PresentID"] for row in case.presents.rows(saved.state)] == [
            row["PresentID"] for index, row in enumerate(mail) if index != 1 or 1 not in selected]
        assert all(not column.values for column in result["PPU"].children)
        if 1 in selected:
            assert set(result) == {"SYSTEM", "PGP", "PPU"} and game(saved.state)["Money"] == 1025
        else:
            assert set(result) == {"SYSTEM", "PPU"} and saved == before
        assert post(client, envelope)[0] == wire and app.state.store.profile() == saved
        post(client, claim(app.state.store, 1, [mail[0]["PresentID"]], tick=1001))
        assert game(app.state.store.profile().state)["NowCharisma"] == 20 + case.bootstrap.rank_rows[0]["CharismaMax"]


@pytest.mark.parametrize("operation", [1, 2, 117])
def test_unselected_recovery_mail_does_not_break_gold_crystal_and_item_claims(bundle, tmp_path, operation):
    case = services(bundle)
    state, mail = case.presents.enqueue(seed(case), [reward(EVENT_COOK),
        {"kind": "gold", "amount": 25, "description": "ゴールド"},
        {"kind": "free_crystal", "amount": 2, "description": "結晶"},
        {"kind": "item", "item_id": BOUQUET, "amount": 1, "description": "花束"}])
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = claim(app.state.store, operation, [row["PresentID"] for row in mail[1:]])
        wire, result = post(client, envelope)
        saved = app.state.store.profile()
        assert set(result) == {"SYSTEM", "PGP", "PFC", "PPU", "POSSESSION"}
        assert game(saved.state)["NowCharisma"] == 20
        assert game(saved.state)["Money"] == 1025
        assert wallet_totals(saved.state) == {"free": 4, "paid": 3, "total": 7}
        assert saved.state["tables"]["possessions"] == [{"ItemID": BOUQUET, "Count": 8}]
        assert [row["PresentID"] for row in case.presents.rows(saved.state)] == [mail[0]["PresentID"]]
        assert post(client, envelope)[0] == wire and app.state.store.profile() == saved
        post(client, claim(app.state.store, 1, [mail[0]["PresentID"]], tick=1001))
        final = app.state.store.profile().state
        assert game(final)["NowCharisma"] == 20 + case.bootstrap.rank_rows[0]["CharismaMax"]
        assert wallet_totals(final) == wallet_totals(saved.state)
        assert final["tables"]["possessions"] == saved.state["tables"]["possessions"]


def test_recovery_retries_cas_against_latest_energy_and_preserves_other_update(bundle, tmp_path, monkeypatch):
    case = services(bundle)
    state, mail = case.presents.enqueue(seed(case), [reward()])
    app = application(case, tmp_path, state)
    update = app.state.store.update_state
    attempts = []
    def concurrent_update(candidate, revision):
        attempts.append(revision)
        if len(attempts) == 1:
            current = app.state.store.profile()
            other = deepcopy(current.state)
            game(other)["Money"] += 17
            game(other)["NowCharisma"] += 3
            update(other, current.revision)
            raise ValueError("Concurrent state update")
        return update(candidate, revision)
    monkeypatch.setattr(app.state.store, "update_state", concurrent_update)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, claim(app.state.store, 1, [mail[0]["PresentID"]]))
        saved = app.state.store.profile().state
        assert len(attempts) == 2
        assert game(saved)["Money"] == 1017
        assert game(saved)["NowCharisma"] == 23 + case.bootstrap.rank_rows[0]["CharismaMax"]
        assert saved["tables"]["crystals"] == state["tables"]["crystals"]
        assert not case.presents.rows(saved)


def test_response_capacity_failure_keeps_recovery_mail_and_energy(bundle, tmp_path, monkeypatch):
    case = services(bundle)
    state, mail = case.presents.enqueue(seed(case), [reward()])
    app = application(case, tmp_path, state)
    encode = presents_module.encode_response
    def reject_claim(profile, payload):
        if 1 in profile.registration_keys:
            raise ProtocolError("test_response_capacity", "Response cannot fit", 503)
        return encode(profile, payload)
    monkeypatch.setattr(presents_module, "encode_response", reject_claim)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        reject(client, app.state.store, claim(app.state.store, 1, [mail[0]["PresentID"]]),
               503, "test_response_capacity")


def test_cli_present_and_crystal_recovery_share_one_policy_instance(bundle):
    assert bundle.presents.recovery is bundle.recovery
    assert bundle.presents.recovery.rules == bundle.recovery.rules


@pytest.mark.parametrize("damage", ["wrong_charisma", "missing_charisma", "integer_keys", "integer_values",
                                   "unequal_lengths", "duplicate_group", "column_group", "empty_group"])
def test_cached_recovery_rejects_modified_game_parameter_snapshot(bundle, tmp_path, damage):
    case = services(bundle)
    state, mail = case.presents.enqueue(seed(case), [reward(COOK)])
    app = application(case, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = claim(app.state.store, 1, [mail[0]["PresentID"]])
        post(client, envelope)
        saved = app.state.store.profile()
    altered = deepcopy(saved.state)
    receipt = altered["present_claim_history"][-1]
    root = loads(base64.b64decode(receipt["response"]))
    original = next(node for node in root.children if node.name == "PGP")
    values = columns(original)
    keys, amounts = values["A1"], values["A2"]
    if damage == "wrong_charisma":
        amounts = tuple(str(int(value) + 1) if key == "A4" else value for key, value in zip(keys, amounts))
        changed = Container("PGP", (Column("A1", "S", keys), Column("A2", "S", amounts)))
    elif damage == "missing_charisma":
        selected = tuple((key, value) for key, value in zip(keys, amounts) if key != "A4")
        changed = Container("PGP", (Column("A1", "S", tuple(key for key, _ in selected)),
                                     Column("A2", "S", tuple(value for _, value in selected))))
    elif damage == "integer_keys":
        changed = Container("PGP", (Column("A1", "I", tuple(range(1, 10))), Column("A2", "S", amounts)))
    elif damage == "integer_values":
        changed = Container("PGP", (Column("A1", "S", keys), Column("A2", "I", (0,) * len(amounts))))
    elif damage == "unequal_lengths":
        changed = Container("PGP", (Column("A1", "S", keys), Column("A2", "S", amounts[:-1])))
    elif damage == "column_group":
        changed = Column("PGP", "S", ("snapshot",))
    elif damage == "empty_group":
        changed = Container("PGP")
    else:
        changed = original
    children = tuple(changed if node is original else node for node in root.children)
    if damage == "duplicate_group":
        children += (original,)
    receipt["response"] = base64.b64encode(dumps(Container(children=children))).decode("ascii")
    reopened = application(services(bundle), tmp_path, state)
    with reopened.state.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(altered),))
    before = reopened.state.store.profile()
    assert before.revision == saved.revision
    assert game(before.state)["NowCharisma"] == game(saved.state)["NowCharisma"]
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        reject(client, reopened.state.store, envelope, 503, "present_history_invalid")


@pytest.mark.parametrize("operation", [1, 117])
def test_legacy_gold_only_snapshot_replays_after_recovery_support_is_enabled(bundle, tmp_path, operation):
    legacy = services(bundle)
    legacy.presents = PresentService(legacy.bootstrap, legacy.players,
        asset_reader=lambda name: bundle.native_cdn.assets.read(name)[0], possessions=legacy.possessions)
    legacy.bootstrap.present_provider = legacy.presents.response
    assert legacy.presents.recovery is None
    state, mail = legacy.presents.enqueue(seed(legacy), [{"kind": "gold", "amount": 17, "description": "ゴールド"}])
    app = application(legacy, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = claim(app.state.store, operation, [mail[0]["PresentID"]])
        wire, result = post(client, envelope)
        assert columns(result["PGP"]) == {"A1": ("A1",), "A2": ("1017",)}
        saved = app.state.store.profile()
    current = services(bundle)
    assert current.presents.recovery is current.recovery
    reopened = application(current, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == saved


@pytest.mark.parametrize("operation", [1, 117])
def test_legacy_crystal_cache_accepts_original_and_rejects_changed_bucket_amount(bundle, tmp_path, operation):
    legacy = services(bundle)
    legacy.presents = PresentService(legacy.bootstrap, legacy.players,
        asset_reader=lambda name: bundle.native_cdn.assets.read(name)[0], possessions=legacy.possessions)
    legacy.bootstrap.present_provider = legacy.presents.response
    state, mail = legacy.presents.enqueue(seed(legacy), [
        {"kind": "free_crystal", "amount": 5, "description": "結晶"}])
    app = application(legacy, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = claim(app.state.store, operation, [mail[0]["PresentID"]])
        wire, _ = post(client, envelope)
        saved = app.state.store.profile()
    reopened = application(services(bundle), tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == saved
        altered = deepcopy(saved.state)
        receipt = altered["present_claim_history"][-1]
        root = loads(base64.b64decode(receipt["response"]))
        pfc = next(node for node in root.children if node.name == "PFC")
        amount_field = "Num" if operation == 117 else "value"
        column = next(column for column in pfc.children if column.name == amount_field)
        amounts = (column.values[0] + 1, *column.values[1:])
        changed = Container("PFC", tuple(Column(amount_field, column.type, amounts)
            if child is column else child for child in pfc.children))
        root = Container(children=tuple(changed if node is pfc else node for node in root.children))
        receipt["response"] = base64.b64encode(dumps(root)).decode("ascii")
        with reopened.state.store.connection() as connection:
            connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(altered),))
        assert reopened.state.store.profile().revision == saved.revision
        reject(client, reopened.state.store, envelope, 503, "present_history_invalid")
