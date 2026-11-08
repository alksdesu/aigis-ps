from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.app import create_app
from aigis_ps.protocol import decode_wire, encode_wire
from test_quest_service import bundle, request


def initial(bundle):
    return bundle.initialize(bundle.bootstrap.initial_state())


@pytest.fixture
def factory(bundle, tmp_path):
    service = bundle.current_squad
    assert service is not None
    assert service.status()["ready"] is True
    serial = 0

    def build(state=None):
        nonlocal serial
        serial += 1
        handlers = (bundle.bootstrap.handlers() | bundle.player_state.handlers()
                    | bundle.presents.handlers() | service.handlers())
        path = tmp_path / str(serial)
        app = create_app(path, handlers=handlers, initial_state=initial(bundle) if state is None else state)
        return SimpleNamespace(service=service, handlers=handlers, path=path, app=app, store=app.state.store)

    return build


def save(store, group=19, name="第十九編成", title=0, *, tutorial=False, tick=1000, selector="OI"):
    columns = [Column(selector, "I", (group,)), Column("A3", "S", (name,)), Column("A4", "I", (title,))]
    if tutorial:
        columns.append(Column("TL", "I", (1,)))
    return request(store, 120, columns, tick=tick)


def post(client, envelope, *, status=200):
    response = client.post("/api/" + envelope.profile.route,
                           content=encode_wire(envelope.xml, envelope.profile.request_key))
    assert response.status_code == status, response.text
    if status != 200:
        return response, None
    document = loads(decode_wire(response.content, envelope.profile.response_key))
    return response, {node.name: node for node in document.children}


def squad_row(group, team, slot, unit=-1, name="旧編成", title=0):
    return {"Address": (group << 24) | (team << 16) | slot,
            "UnitIndex": unit, "Name": name, "Flags": title}


def tgi_rows(container):
    assert tuple((column.name, column.type) for column in container.children) == (
        ("A0", "I"), ("A1", "I"), ("A3", "S"), ("A4", "I"))
    return [dict(zip(("Address", "UnitIndex", "Name", "Flags"), values))
            for values in zip(*(column.values for column in container.children))]


def assert_save_reply(nodes):
    assert set(nodes) == {"SYSTEM", "PPU"}
    assert len(nodes["PPU"].children) == 21
    assert all(not column.values for column in nodes["PPU"].children)


def claim(store, present_id, *, tick=1000):
    return request(store, 1, (Container("RT", (Container("PT2", (
        Column("PresentID", "I", (present_id,)),)),)),), tick=tick)


def test_last_normal_team_creates_fifteen_empty_slots_and_reopens(factory):
    case = factory()
    before = case.store.profile()
    envelope = save(case.store)
    expected = [squad_row(19, 0, slot, name="第十九編成") for slot in range(1, 16)]
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        response, nodes = post(client, envelope)
        assert_save_reply(nodes)
        saved = case.store.profile()
        assert saved.revision == before.revision + 1
        assert saved.state["player_state"]["battle"] == {**before.state["player_state"]["battle"], "TN": 19}
        assert saved.state["player_state"]["squads"] == expected
        assert saved.state["player_state"]["legacy_squad_imported"] is True
        assert saved.state["player_state"]["units"] == before.state["player_state"]["units"]
        assert saved.state["tables"] == before.state["tables"]
        assert post(client, envelope)[0].content == response.content
        assert case.store.profile() == saved
    reopened = create_app(case.path, handlers=case.handlers)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        _, battle = post(client, request(reopened.state.store, 39, ()))
        assert battle["TN"].values == (19,) and battle["RTN"].values == (64,)
        _, squads = post(client, request(reopened.state.store, 72, ()))
        assert tgi_rows(squads["TGI"]) == expected
        assert post(client, envelope)[0].content == response.content
    assert reopened.state.store.profile() == saved


@pytest.mark.parametrize("group", [-1, 20])
def test_outside_normal_team_range_is_rejected_without_mutation(factory, group):
    case = factory()
    before = case.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, save(case.store, group), status=400)
    assert case.store.profile() == before


def test_metadata_changes_every_subteam_but_preserves_members_and_other_groups(factory, bundle):
    state, added = bundle.player_state.credit_units(initial(bundle),
        [{"card_id": 3, "level": 1, "count": 1}, {"card_id": 8, "level": 1, "count": 1}])
    original = [squad_row(0, 0, 1, added[0]["GetIndex"], "第一編成", 1),
                squad_row(3, 0, 1, added[0]["GetIndex"]), squad_row(3, 0, 15),
                squad_row(3, 1, 2, added[1]["GetIndex"]), squad_row(3, 1, 14),
                squad_row(19, 0, 1, -1, "別の編成", 1)]
    state["player_state"].update(squads=original, legacy_squad_imported=True)
    case = factory(state)
    expected = [{**row, "Name": "新しい編成", "Flags": 1} if row["Address"] >> 24 == 3 else row
                for row in original]
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        _, original_units = post(client, request(case.store, 41, ()))
        assert_save_reply(post(client, save(case.store, 3, "新しい編成", 1))[1])
        saved = case.store.profile()
        assert saved.state["player_state"]["squads"] == expected
        assert saved.state["player_state"]["units"] == state["player_state"]["units"]
        assert post(client, request(case.store, 41, ()))[1] == original_units
        assert tgi_rows(post(client, request(case.store, 72, ()))[1]["TGI"]) == expected
        assert case.store.profile() == saved


def test_same_team_new_name_and_title_are_saved_without_member_request(factory):
    case = factory()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        first = save(case.store, 0, "第一編成", 0)
        post(client, first)
        old = case.store.profile()
        second = save(case.store, 0, "名称変更", 1, tick=1001)
        assert_save_reply(post(client, second)[1])
        saved = case.store.profile()
        assert saved.revision == old.revision + 1
        assert {row["Name"] for row in saved.state["player_state"]["squads"]} == {"名称変更"}
        assert {row["Flags"] for row in saved.state["player_state"]["squads"]} == {1}
        assert saved.state["player_state"]["units"] == old.state["player_state"]["units"]
        post(client, first, status=409)
        assert case.store.profile() == saved


def test_received_title_can_be_selected_and_survives_reopen(factory, bundle):
    catalog = bundle.presents.emblem_catalog
    title = next(row for row in catalog.titles
                 if row.predicate == "possession" and row.argument == "get_emblem_00001")
    state, mail = bundle.presents.enqueue(initial(bundle), [{"kind": "item", "item_id": title.argument,
        "amount": 1, "description": "称号獲得"}])
    case = factory(state)
    before_units = deepcopy(state["player_state"]["units"])
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, save(case.store, title=title.index), status=409)
        post(client, claim(case.store, mail[0]["PresentID"]))
        assert_save_reply(post(client, save(case.store, title=title.index, tick=1001))[1])
        saved = case.store.profile()
        assert saved.state["player_state"]["units"] == before_units
        assert next(row["Count"] for row in saved.state["tables"]["possessions"]
                    if row["ItemID"] == title.argument) == 1
    reopened = create_app(case.path, handlers=case.handlers)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        _, squads = post(client, request(reopened.state.store, 72, ()))
        assert {row["Flags"] for row in tgi_rows(squads["TGI"])} == {title.index}
        assert catalog.eligible(title.index, reopened.state.store.profile().state)
    assert reopened.state.store.profile() == saved


def test_emblem_display_seventy_unlocks_seventy_one_not_seventy(factory, bundle):
    state, mail = bundle.presents.enqueue(initial(bundle), [{"kind": "item", "item_id": "get_emblem_00034",
        "amount": 1, "description": "称号獲得"}])
    case = factory(state)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, claim(case.store, mail[0]["PresentID"]))
        before = case.store.profile()
        post(client, save(case.store, title=70, tick=1001), status=409)
        assert case.store.profile() == before
        assert_save_reply(post(client, save(case.store, title=71, tick=1002))[1])
        assert {row["Flags"] for row in case.store.profile().state["player_state"]["squads"]} == {71}


def test_emblem_display_one_twenty_three_still_requires_quest(factory, bundle):
    state, mail = bundle.presents.enqueue(initial(bundle), [{"kind": "item", "item_id": "get_emblem_10018",
        "amount": 1, "description": "称号獲得"}])
    case = factory(state)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, claim(case.store, mail[0]["PresentID"]))
        before = case.store.profile()
        post(client, save(case.store, title=123, tick=1001), status=409)
        assert case.store.profile() == before
        updated = deepcopy(before.state)
        updated["tables"]["quest_records"].append({"QuestID": 45002, "AchieveFlag": 0x01000000,
            "MaxRank": 1, "RemainAssault": 0, "FirstDefeatDate": 0, "Shown": False})
        case.store.update_state(updated, before.revision)
        assert_save_reply(post(client, save(case.store, title=123, tick=1002))[1])
        assert {row["Flags"] for row in case.store.profile().state["player_state"]["squads"]} == {123}


def test_tutorial_preserves_signed_high_bits_and_is_read_from_36(factory, bundle):
    state = initial(bundle)
    state["tables"]["config"][0]["MenuTutorialFlags"] = -0x80000000
    case = factory(state)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        assert_save_reply(post(client, save(case.store, tutorial=True))[1])
        saved = case.store.profile()
        assert saved.state["tables"]["config"][0] == {
            **state["tables"]["config"][0], "MenuTutorialFlags": -0x80000000 | 6}
        _, config = post(client, request(case.store, 36, ()))
        assert config["A4"].type == "I" and config["A4"].values == (-0x80000000 | 6,)
        assert case.store.profile() == saved


def test_late_74_cannot_replace_current_team_metadata(factory):
    case = factory()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, save(case.store))
        saved = case.store.profile()
        legacy = request(case.store, 74, (Column("A0", "I", (1,)), Column("A1", "I", (-1,)),
            Column("A3", "S", ("古い編成",)), Column("A4", "I", (0,))), tick=1001)
        _, response = post(client, legacy)
        assert tgi_rows(response["TGI"]) == saved.state["player_state"]["squads"]
        assert case.store.profile() == saved


def test_pending_legacy_members_are_not_hidden_by_new_empty_group(factory, bundle):
    state, added = bundle.player_state.credit_units(initial(bundle), [{"card_id": 8, "level": 1, "count": 1}])
    unit = next(row for row in state["player_state"]["units"] if row["GetIndex"] == added[0]["GetIndex"])
    unit["OrganizationIndex2"] = 1
    case = factory(state)
    before = case.store.profile()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, save(case.store), status=409)
        assert case.store.profile() == before
        legacy = request(case.store, 74, (Column("A0", "I", (1,)),
            Column("A1", "I", (unit["GetIndex"],)), Column("A3", "S", ("移行編成",)),
            Column("A4", "I", (0,))), tick=1001)
        post(client, legacy)
        assert_save_reply(post(client, save(case.store, tick=1002))[1])
        rows = case.store.profile().state["player_state"]["squads"]
        assert rows[0] == squad_row(0, 0, 1, unit["GetIndex"], "移行編成")
        assert len(rows) == 16
        assert case.store.profile().state["player_state"]["units"] == state["player_state"]["units"]


def test_existing_group_zero_keeps_legacy_af_without_reimport(factory, bundle):
    state, added = bundle.player_state.credit_units(initial(bundle), [{"card_id": 8, "level": 1, "count": 1}])
    unit = next(row for row in state["player_state"]["units"] if row["GetIndex"] == added[0]["GetIndex"])
    unit["OrganizationIndex2"] = 1
    state["player_state"]["squads"] = [squad_row(0, 0, 1, unit["GetIndex"])]
    case = factory(state)
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        assert_save_reply(post(client, save(case.store))[1])
    saved = case.store.profile().state["player_state"]
    assert saved["legacy_squad_imported"] is True
    assert saved["squads"][0] == state["player_state"]["squads"][0]
    assert saved["units"] == state["player_state"]["units"]


def test_native_legacy_padding_overlap_imports_and_unblocks_current_squad(factory, bundle):
    state, added = bundle.player_state.credit_units(initial(bundle),
        [{"card_id": 3, "level": 1, "count": 1}, {"card_id": 8, "level": 1, "count": 1}])
    members = [row["GetIndex"] for row in added]
    for slot, unit in enumerate(state["player_state"]["units"][-2:], start=1):
        unit["OrganizationIndex2"] = slot
    case = factory(state)
    native = ([squad_row(0, 0, slot, unit, "") for slot, unit in enumerate(members, start=1)]
              + [squad_row(0, 0, slot, -1, "") for slot in range(len(members), 15)])
    legacy = request(case.store, 74, tuple(Column(wire, kind, tuple(row[name] for row in native)) for name, wire, kind in (
        ("Address", "A0", "I"), ("UnitIndex", "A1", "I"), ("Name", "A3", "S"), ("Flags", "A4", "I"))))
    expected = native[:2] + native[3:]
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        post(client, save(case.store), status=409)
        _, imported = post(client, legacy)
        assert tgi_rows(imported["TGI"]) == expected
        assert_save_reply(post(client, save(case.store, 0, "移行編成", tick=1001))[1])
        _, squads = post(client, request(case.store, 72, ()))
    assert tgi_rows(squads["TGI"]) == [{**row, "Name": "移行編成"} for row in expected]
    assert case.store.profile().state["player_state"]["units"] == state["player_state"]["units"]


def test_raid_selection_persists_metadata_and_survives_reopen(factory):
    case = factory()
    before = case.store.profile()
    envelope = save(case.store, 64, "突撃編成", selector="RTN")
    expected = [squad_row(64, 0, slot, name="突撃編成") for slot in range(1, 16)]
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        response, nodes = post(client, envelope)
        assert_save_reply(nodes)
        saved = case.store.profile()
        assert saved.state["player_state"]["battle"] == before.state["player_state"]["battle"]
        assert saved.state["player_state"]["squads"] == expected
        assert post(client, envelope)[0].content == response.content
        post(client, save(case.store, 65, "誤り", selector="RTN", tick=1001), status=400)
        assert case.store.profile() == saved
    reopened = create_app(case.path, handlers=case.handlers)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        _, battle = post(client, request(reopened.state.store, 39, ()))
        assert battle["TN"].values == (0,) and battle["RTN"].values == (64,)
        _, squads = post(client, request(reopened.state.store, 72, ()))
        assert tgi_rows(squads["TGI"]) == expected


def test_rolled_back_client_clock_still_saves_the_current_team(factory):
    case = factory()
    with TestClient(case.app, base_url="http://127.0.0.1") as client:
        later = save(case.store, 2, "先", tick=1_800_000_000)
        post(client, later)
        assert_save_reply(post(client, save(case.store, 3, "後", tick=1_700_000_000))[1])
        assert case.store.profile().state["player_state"]["battle"]["TN"] == 3
        post(client, later, status=409)
        assert case.store.profile().state["player_state"]["battle"]["TN"] == 3
