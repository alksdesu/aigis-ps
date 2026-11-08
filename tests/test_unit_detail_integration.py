from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.barracks_service import EXTRA_SHIFT, PURCHASE_FIELD
from test_evolution_catalog import unit
from test_evolution_integration import post, seed
from test_quest_service import bundle, request


def change(store, index, *, display=0, toggle=1, tick=1000):
    return request(store, 10, (Container("PU", (Column("A7", "I", (index,)),
        Column("B0", "I", (display,)), Column("SC", "I", (toggle,)))),), tick=tick)


def application(bundle, path, state):
    return create_app(path, initial_state=bundle.initialize(state), state_initializer=bundle.initialize,
        handlers=bundle.unit_detail.handlers() | bundle.player_state.handlers() | bundle.unit_actions.handlers())


def test_alias_skill_switch_uses_original_skills_and_replays_after_restart(bundle, tmp_path):
    assert bundle.unit_detail is not None, bundle.unit_detail_error
    target = unit(bundle.evolution.catalog, 497, stage=2)
    target.update(SkillEvolved=1, GeneralPurposeFlags=0x27, SettingSkillID=649,
                  SkillLevel=3, SkillExperience=7, CostExperience=2, Exp=123)
    state, rows = seed(bundle, target, [])
    state["tables"]["config"][0][PURCHASE_FIELD] = 2 << EXTRA_SHIFT
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        off = change(app.state.store, rows[0]["GetIndex"])
        wire, response = post(client, off)
        assert set(response) == {"SYSTEM", "PPU"} and len(response["PPU"].children) == 21
        after = app.state.store.profile()
        assert after.state["player_state"]["units"][1] == {**rows[0], "GeneralPurposeFlags": 0x23, "SettingSkillID": 648}
        assert after.state["tables"] == before.state["tables"]
        assert post(client, off)[0] == wire and app.state.store.profile() == after
        on = change(app.state.store, rows[0]["GetIndex"], tick=1001)
        wire, _ = post(client, on)
        saved = app.state.store.profile()
        assert saved.state["player_state"]["units"][1] == rows[0]
    reopened = application(bundle, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, on)[0] == wire
        assert reopened.state.store.profile() == saved
        _, reloaded = post(client, request(reopened.state.store, 41, ()))
        assert reloaded["AD"].values == (59, 649)
        assert reloaded["B1"].values == (0, 1)


def test_free_smartphone_costume_can_be_saved_without_consuming_items(bundle, tmp_path):
    assert bundle.unit_detail.smartphone_images is True
    state, rows = seed(bundle, unit(bundle.evolution.catalog, 416), [])
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        _, response = post(client, change(app.state.store, rows[0]["GetIndex"], display=21, toggle=0))
        saved = app.state.store.profile()
        assert saved.state["player_state"]["units"][1] == {**rows[0], "_DisplayType": 21}
        assert saved.state["tables"] == before.state["tables"]


def test_paid_costume_requires_current_possession_and_preserves_existing_choice(bundle, tmp_path):
    state, rows = seed(bundle, unit(bundle.evolution.catalog, 29), [])
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        index = rows[0]["GetIndex"]
        before = app.state.store.profile()
        post(client, change(app.state.store, index, display=5, toggle=0), 501)
        assert app.state.store.profile() == before
        granted = bundle.possessions.credit(before.state, {"get_skin_00001": 1})
        app.state.store.update_state(granted, before.revision)
        post(client, change(app.state.store, index, display=5, toggle=0, tick=1001))
        owned = app.state.store.profile()
        assert owned.state["tables"]["possessions"] == [{"ItemID": "get_skin_00001", "Count": 1}]
        assert owned.state["player_state"]["units"][1]["_DisplayType"] == 5
        removed = bundle.possessions.debit(owned.state, {"get_skin_00001": 1})
        app.state.store.update_state(removed, owned.revision)
        post(client, change(app.state.store, index, display=5, toggle=0, tick=1002))
        post(client, change(app.state.store, index, display=0, toggle=0, tick=1003))
        before = app.state.store.profile()
        post(client, change(app.state.store, index, display=5, toggle=0, tick=1004), 501)
        assert app.state.store.profile() == before


def test_old_awake_flag_without_execution_marker_can_switch_off_without_fabricating_history(bundle, tmp_path):
    target = unit(bundle.evolution.catalog, 7, stage=2)
    target.update(GeneralPurposeFlags=5, SkillEvolved=0, SettingSkillID=218)
    state, rows = seed(bundle, target, [])
    app = application(bundle, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, change(app.state.store, rows[0]["GetIndex"]))
        saved = app.state.store.profile().state["player_state"]["units"][1]
        assert saved["SettingSkillID"] == 41 and saved["GeneralPurposeFlags"] == 1
        assert saved["SkillEvolved"] == 0
