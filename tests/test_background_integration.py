from copy import copy, deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.app import create_app
from aigis_ps.barracks_service import EXTRA_SHIFT, PURCHASE_FIELD, RETAINED_BITS
from aigis_ps.energy_clock import JST
from aigis_ps.player_state import BARRACKS_BITS, BARRACKS_SHIFT
from aigis_ps.protocol import decode_wire, encode_wire
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12, 34, 56, tzinfo=JST)


class Clock:
    def __init__(self, now=NOW):
        self.now = now
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.now


def initial_state(bundle):
    return bundle.player_state.initialize(bundle.bootstrap.initial_state())


@pytest.fixture
def factory(bundle, tmp_path):
    assert bundle.backgrounds is not None
    assert bundle.backgrounds.status()["ready"] is True
    serial = 0

    def build(state=None, *, now=NOW):
        nonlocal serial
        serial += 1
        service = copy(bundle.backgrounds)
        bootstrap = copy(bundle.bootstrap)
        clock = Clock(now)
        service.bootstrap, service.clock = bootstrap, clock
        handlers = {**bootstrap.handlers(), **service.handlers()}
        path = tmp_path / str(serial)
        app = create_app(path, handlers=handlers,
                         initial_state=initial_state(bundle) if state is None else state)
        return SimpleNamespace(service=service, bootstrap=bootstrap, clock=clock,
                               handlers=handlers, path=path, app=app, store=app.state.store)

    return build


def selection(case, name="system_default", identifiers=(0, 1), *, tick=1000):
    return request(case.store, 135, (Column("CardID", "I", identifiers),
        Column("ConfigID", "S", (name,) * len(identifiers))), tick=tick)


def post(client, envelope, *, status=200, error=None):
    response = client.post("/api/" + envelope.profile.route,
                           content=encode_wire(envelope.xml, envelope.profile.request_key))
    assert response.status_code == status, response.text
    if error is not None:
        assert response.json()["error"]["code"] == error
    return response


def groups(response, envelope):
    return {node.name: node for node in loads(decode_wire(response.content, envelope.profile.response_key)).children}


def rows(node):
    assert tuple((column.name, column.type) for column in node.children) == (
        ("CardID", "I"), ("ConfigID", "S"), ("SaveTime", "S"))
    assert len({len(column.values) for column in node.children}) == 1
    return [dict(zip(("CardID", "ConfigID", "SaveTime"), values))
            for values in zip(*(column.values for column in node.children))]


def unrelated(state):
    result = deepcopy(state)
    result.pop("background_history", None)
    result.pop("backgrounds_schema", None)
    result["tables"].pop("backgrounds")
    return result


def test_default_uses_prince_zero_index_and_survives_http_reopen(factory):
    case = factory()
    before = case.store.profile()
    assert before.state["tables"]["config"][0]["_GuideUnitIndex"] == 0
    assert any(unit["GetIndex"] == 0 and unit["CardID"] == 1 for unit in before.state["player_state"]["units"])
    envelope = selection(case)
    expected = [{"CardID": card, "ConfigID": "system_default", "SaveTime": "2026/11/01 12:34:56"}
                for card in (0, 1)]
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        reply = post(client, envelope)
        assert set(groups(reply, envelope)) == {"SYSTEM", "PRM"}
        assert rows(groups(reply, envelope)["PRM"]) == expected
        after = case.store.profile()
        assert after.revision == before.revision + 1
        assert unrelated(after.state) == unrelated(before.state)
        read = request(case.store, 137, (), tick=1001)
        assert rows(groups(post(client, read), read)["PRM"]) == expected
        assert case.store.profile() == after
    reopened = create_app(case.path, handlers=case.handlers)
    case.clock.now += timedelta(days=1)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope).content == reply.content
        read = request(reopened.state.store, 137, (), tick=1002)
        assert rows(groups(post(client, read), read)["PRM"]) == expected
    assert reopened.state.store.profile() == after
    assert case.clock.calls == 1


def test_same_background_new_action_updates_time_but_exact_retry_does_not(factory):
    case = factory()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, selection(case))
        first = case.store.profile()
        case.clock.now += timedelta(seconds=17)
        second_request = selection(case, tick=1001)
        second = post(client, second_request)
        saved = case.store.profile()
        assert saved.revision == first.revision + 1
        assert {row["SaveTime"] for row in rows(groups(second, second_request)["PRM"])} == {"2026/11/01 12:35:13"}
        case.clock.now += timedelta(days=1)
        assert post(client, second_request).content == second.content
        assert case.store.profile() == saved
        assert case.clock.calls == 2


def test_same_server_second_new_action_is_recorded_and_older_reply_is_stale(factory):
    case = factory()
    first_request = selection(case)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, first_request)
        first = case.store.profile()
        second_request = selection(case, tick=1001)
        second = post(client, second_request)
        saved = case.store.profile()
        assert saved.revision == first.revision + 1
        assert saved.state["tables"]["backgrounds"] == first.state["tables"]["backgrounds"]
        assert len(saved.state["background_history"]["receipts"]) == 2
        case.clock.now += timedelta(minutes=5)
        assert post(client, second_request).content == second.content
        post(client, first_request, status=409, error="background_retry_stale")
        assert case.store.profile() == saved
        assert case.clock.calls == 2


def test_fixed_background_only_changes_global_row(factory):
    case = factory()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, selection(case))
        old_guide = deepcopy(case.store.profile().state["tables"]["backgrounds"][1])
        case.clock.now += timedelta(seconds=1)
        fixed = selection(case, "system_fix", (0,), tick=1001)
        assert rows(groups(post(client, fixed), fixed)["PRM"]) == [
            {"CardID": 0, "ConfigID": "system_fix", "SaveTime": "2026/11/01 12:34:57"}]
        saved = case.store.profile()
        assert saved.state["tables"]["backgrounds"][1] == old_guide
        post(client, selection(case, "system_fix", (0, 1), tick=1002),
             status=409, error="background_guide_mismatch")
        assert case.store.profile() == saved


def test_origin_identity_from_extra_barracks_is_used_in_http_request(factory, bundle):
    cards = bundle.gifts.catalog
    source = next(cards.card(identifier) for identifier in cards.card_ids if cards.card(identifier)["OriginID"])
    state, added = bundle.player_state.credit_units(initial_state(bundle),
        [{"card_id": source["CardID"], "level": 1, "count": 1}])
    selected = next(unit for unit in state["player_state"]["units"] if unit["GetIndex"] == added[0]["GetIndex"])
    selected["GeneralPurposeFlags"] = (selected["GeneralPurposeFlags"] & ~BARRACKS_BITS) | 1 << BARRACKS_SHIFT
    config = state["tables"]["config"][0]
    config["_GuideUnitIndex"] = selected["GetIndex"]
    config[PURCHASE_FIELD] = (config[PURCHASE_FIELD] & RETAINED_BITS) | 1 << EXTRA_SHIFT
    case = factory(state)
    envelope = selection(case, identifiers=(0, source["OriginID"]))
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        response = post(client, envelope)
        assert [row["CardID"] for row in rows(groups(response, envelope)["PRM"])] == [0, source["OriginID"]]
        assert case.store.profile().state["player_state"] == state["player_state"]
        before = case.store.profile()
        post(client, selection(case, identifiers=(0, source["CardID"]), tick=1001),
             status=409, error="background_guide_mismatch")
        assert case.store.profile() == before


def test_missing_selected_instance_allows_only_global_default(factory, bundle):
    state = initial_state(bundle)
    state["tables"]["config"][0]["_GuideUnitIndex"] = 123456
    case = factory(state)
    envelope = selection(case, identifiers=(0,))
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        assert [row["CardID"] for row in rows(groups(post(client, envelope), envelope)["PRM"])] == [0]


def test_owning_private_room_character_does_not_replace_current_guide(factory, bundle):
    state, _ = bundle.player_state.credit_units(initial_state(bundle), [{"card_id": 436, "level": 1, "count": 1}])
    case = factory(state)
    before = case.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, selection(case, "private_0001", (0, 436)), status=409, error="background_guide_mismatch")
    assert case.store.profile() == before


def test_historical_unknown_name_for_another_guide_survives_save_and_137(factory, bundle):
    state = initial_state(bundle)
    historical = {"CardID": 777, "ConfigID": "retired_local_background", "SaveTime": "2001/01/01 00:00:00"}
    state["backgrounds_schema"] = 1
    state["tables"]["backgrounds"] = [historical]
    case = factory(state)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        envelope = selection(case)
        assert len(rows(groups(post(client, envelope), envelope)["PRM"])) == 2
        read = request(case.store, 137, (), tick=1001)
        result = rows(groups(post(client, read), read)["PRM"])
        assert result[0] == historical and len(result) == 3
        assert unrelated(case.store.profile().state) == unrelated(state)


# The client clock ticks in whole seconds, so the end second stays open until the next one starts.
@pytest.mark.parametrize("microseconds,expected", [(0, 200), (999_999, 200), (1_000_000, 409)])
def test_actual_event_end_includes_its_whole_last_second(factory, bundle, microseconds, expected):
    definition = next(row for row in bundle.backgrounds.catalog.definitions
                      if row.category == -1777172756 and row.end is not None)
    case = factory(now=definition.end + timedelta(microseconds=microseconds))
    before = case.store.profile()
    envelope = selection(case, definition.name)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        response = post(client, envelope, status=expected,
                        error="background_unavailable" if expected == 409 else None)
    if expected == 409:
        assert case.store.profile() == before
    else:
        assert rows(groups(response, envelope)["PRM"])[0]["SaveTime"] == definition.end.strftime("%Y/%m/%d %H:%M:%S")


def test_inconsistent_137_provider_prevents_any_save_or_receipt(factory, monkeypatch):
    case = factory()
    monkeypatch.setattr(case.bootstrap, "background_provider", lambda _: Container("PRM"))
    before = case.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, selection(case), status=503, error="background_provider_inconsistent")
    assert case.store.profile() == before
