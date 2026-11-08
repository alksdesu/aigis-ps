from copy import copy, deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import Column, Container
from aigis_ps.app import create_app
from aigis_ps.errors import ProtocolError
from aigis_ps.mission_service import MissionService
from aigis_ps.startup_diagnostics import StartupDiagnostics
from aigis_ps.storage import Store
from test_evolution_integration import post
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)


def service(bundle, **options):
    assert bundle.missions is not None, bundle.mission_error
    clock = SimpleNamespace(now=NOW)
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: clock.now
    result = MissionService(bootstrap, bundle.player_state, bundle.presents, bundle.missions.catalog, **options)
    return result, clock


def seed(bundle, missions):
    return missions.initialize(bundle.player_state.initialize(missions.bootstrap.initial_state()))


def message(store, operation, identifier=0, period=1, tab=1, group=0, tick=1000):
    return request(store, operation, (Container("GL", tuple(Column(name, "I", (value,)) for name, value in (
        ("ID", identifier), ("PT", period), ("TabGroupId", tab), ("TaskGroupId", group)))),), tick=tick)


def application(missions, path, state):
    return create_app(path, initial_state=state,
                      handlers=missions.bootstrap.handlers() | missions.handlers())


def quest_clear(missions, state, count):
    for _ in range(count):
        state = missions.record(state, "quest_clear", {"quest_id": 1001, "quest_type": 0,
                                 "stars": 3, "hard": False}, NOW)
    return state


def test_real_daily_counters_group_claim_and_reopen_keep_rewards_in_mailbox(bundle, tmp_path):
    missions, _ = service(bundle)
    state = missions.record(seed(bundle, missions), "login", {}, NOW)
    state, units = bundle.player_state.credit_units(state, [{"card_id": 7, "level": 1, "count": 1}])
    before, after = deepcopy(units[0]), deepcopy(units[0])
    after["Love"] = 1
    state["player_state"]["units"][-1] = after
    state = missions.record(state, "gift", {"before": before, "after": after}, NOW)
    before, after = deepcopy(after), deepcopy(after)
    after["Exp"] = bundle.player_state.experience_for_level(7, 2)
    state["player_state"]["units"][-1] = after
    state = missions.record(state, "training", {"before": before, "after": after, "level": 2}, NOW)
    state = quest_clear(missions, state, 3)
    snapshot = {row["ID"]: row for row in missions.progress.snapshot(state, NOW)}
    assert all(snapshot[identifier]["Status"] == 1 for identifier in range(1605, 1612))
    app = application(missions, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = message(app.state.store, 59)
        wire, result = post(client, envelope)
        assert set(result) == {"SYSTEM", "GLRW", "PPT"}
        assert set(result["GLRW"].children[0].values) == set(range(1605, 1612))
        saved = app.state.store.profile()
        assert len(bundle.presents.rows(saved.state)) == 7
        assert saved.state["player_state"] == state["player_state"]
        for table in ("game_param", "game_param2", "crystals", "possessions"):
            assert saved.state["tables"][table] == state["tables"][table]
        _, loaded = post(client, request(app.state.store, 54, ()))
        statuses = dict(zip(loaded["GL"].children[0].values, loaded["GL"].children[1].values))
        assert all(statuses[identifier] == 2 for identifier in range(1605, 1612))
        assert loaded["SMN"].children == (Column("A1", "S", ("Consumed", "Snapshot")), Column("A2", "I", (1, 0)))
        _, mail = post(client, request(app.state.store, 42, ()))
        assert len(mail["PPT"].children[0].values) == 7
    reopened_missions, _ = service(bundle)
    reopened = application(reopened_missions, tmp_path, state)
    with TestClient(reopened, base_url="http://127.0.0.1") as client:
        assert post(client, envelope)[0] == wire
        assert reopened.state.store.profile() == saved


def test_real_monthly_unsupported_ticket_stays_claimable_while_other_rewards_are_claimed(bundle, tmp_path):
    missions, _ = service(bundle)
    state = quest_clear(missions, seed(bundle, missions), 30)
    page = {item.condition_id for item in missions.catalog.page(2, 2, 0, NOW)}
    ready = {row["ID"] for row in missions.progress.snapshot(state, NOW) if row["Status"] == 1} & page
    assert 1634 in ready and len(ready) > 1
    app = application(missions, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = message(app.state.store, 59, period=2, tab=2)
        wire, result = post(client, envelope)
        claimed = result["GLRW"].children[0].values
        assert 1634 not in claimed and set(claimed) == ready - {1634}
        saved = app.state.store.profile()
        statuses = {row["ID"]: row["Status"] for row in missions.progress.snapshot(saved.state, NOW)}
        assert statuses[1634] == 1 and all(statuses[identifier] == 2 for identifier in claimed)
        mail = bundle.presents.rows(saved.state)
        assert mail and set(result["PPT"].children[0].values) == {row["PresentID"] for row in mail}
        assert all(row.get("ItemID") != missions.catalog.by_id[1634].reward_item_id for row in mail)
        assert post(client, envelope)[0] == wire and app.state.store.profile() == saved
        response = post(client, message(app.state.store, 58, 1634, period=2, tab=2, tick=1001), expected=501)
        assert response.json()["error"]["code"] == "summon_ticket_pool_disabled"
        assert app.state.store.profile() == saved


def test_real_read_notifications_do_not_claim_and_second_request_is_noop(bundle, tmp_path):
    missions, _ = service(bundle)
    state = missions.record(seed(bundle, missions), "login", {}, NOW)
    app = application(missions, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        wire, result = post(client, message(app.state.store, 60))
        assert set(result) == {"SYSTEM", "GLRW"} and not result["GLRW"].children[0].values
        saved = app.state.store.profile()
        row = next(row for row in missions.progress.snapshot(saved.state, NOW) if row["ID"] == 1611)
        assert row == {"ID": 1611, "Status": 1, "Read": 1, "Progress": 1}
        assert not bundle.presents.rows(saved.state)
        assert post(client, message(app.state.store, 60))[0] == wire
        post(client, message(app.state.store, 60, tick=1001))
        assert app.state.store.profile() == saved


def test_real_read_notification_marks_claimed_unread_daily_missions(bundle, tmp_path):
    missions, _ = service(bundle)
    state = missions.record(seed(bundle, missions), "login", {}, NOW)
    app = application(missions, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        post(client, message(app.state.store, 58, 1611))
        claimed = {row["ID"]: row for row in missions.progress.snapshot(app.state.store.profile().state, NOW)}
        assert claimed[1611] == {"ID": 1611, "Status": 2, "Read": 0, "Progress": 1}
        envelope = message(app.state.store, 60, tick=1001)
        wire, result = post(client, envelope)
        assert set(result) == {"SYSTEM", "GLRW"} and not result["GLRW"].children[0].values
        saved = app.state.store.profile()
        after = {row["ID"]: row for row in missions.progress.snapshot(saved.state, NOW)}
        assert after[1611] == {**claimed[1611], "Read": 1}
        assert [(entry["operation"], entry["ids"]) for entry in saved.state["mission_history"]["receipts"]] == [
            (58, [1611]), (60, [1611])]
        assert all(row == claimed[key] for key, row in after.items() if key != 1611)
        assert post(client, envelope)[0] == wire and app.state.store.profile() == saved


def test_real_expired_daily_version_answers_unchanged_while_a_foreign_page_still_refuses(bundle, tmp_path):
    missions, _ = service(bundle)
    expired = missions.catalog.by_id[1223]
    assert expired.condition_id in missions.catalog.listed(1, 1) and not missions.catalog.is_active(expired, NOW)
    app = application(missions, tmp_path, missions.record(seed(bundle, missions), "login", {}, NOW))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = app.state.store.profile()
        _, result = post(client, message(app.state.store, 58, 1223))
        assert set(result) == {"SYSTEM", "GLRW"} and not result["GLRW"].children[0].values
        response = post(client, message(app.state.store, 58, 1223, period=2, tab=2, tick=1001), expected=400)
        assert response.json()["error"]["code"] == "mission_scope_invalid"
        assert app.state.store.profile() == before


def test_real_read_notification_covers_the_achievement_and_skin_tabs(bundle, tmp_path):
    real = bundle.missions
    missions, _ = service(bundle, progress=real.progress, skins=real.skins, fame=real.fame, fame_status=real.fame_status)
    state = missions.record(seed(bundle, missions), "login", {}, NOW)
    skin = min(missions.progress.skin_items)
    state = missions.record(state, "skin_acquired", {"previous_items": [], "current_items": [skin]}, NOW)
    tabs = {tab: [row.condition_id for row in missions.catalog.page(4, tab, 0, NOW)] for tab in (4, 7)}
    before = {row["ID"]: row for row in missions.progress.snapshot(state, NOW)}
    unread = [key for key in tabs[4] + tabs[7] if before[key]["Status"] != 0 and before[key]["Read"] == 0]
    assert set(unread) & set(tabs[4]) and set(unread) & set(tabs[7])
    app = application(missions, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = message(app.state.store, 60, period=4, tab=7)
        wire, result = post(client, envelope)
        assert set(result) == {"SYSTEM", "GLRW"} and not result["GLRW"].children[0].values
        saved = app.state.store.profile()
        after = {row["ID"]: row for row in missions.progress.snapshot(saved.state, NOW)}
        assert saved.state["mission_history"]["receipts"][-1]["ids"] == unread
        assert all(after[key]["Read"] == 1 for key in unread)
        assert all(row == before[key] for key, row in after.items() if key not in unread)
        assert post(client, envelope)[0] == wire and app.state.store.profile() == saved


def test_real_daily_rollover_rejects_old_request_and_allows_new_login_reward(bundle, tmp_path):
    missions, clock = service(bundle)
    state = missions.record(seed(bundle, missions), "login", {}, NOW)
    app = application(missions, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        envelope = message(app.state.store, 58, 1611)
        post(client, envelope)
        first = app.state.store.profile()
        clock.now += timedelta(days=1)
        response = post(client, envelope, expected=409)
        assert response.json()["error"]["code"] == "mission_retry_stale"
        assert app.state.store.profile() == first
        updated = missions.record(first.state, "login", {}, clock.now)
        app.state.store.update_state(updated, first.revision)
        post(client, message(app.state.store, 58, 1611, tick=1001))
        final = app.state.store.profile()
        assert len(bundle.presents.rows(final.state)) == 2
        periods = [entry["period"] for entry in final.state["mission_state"]["entries"]
                   if entry["condition_id"] == 1611 and entry["status"] == 2]
        assert len(periods) == len(set(periods)) == 2


def test_real_daily_rollover_answers_stale_claims_unchanged(bundle, tmp_path):
    missions, clock = service(bundle)
    state = missions.record(seed(bundle, missions), "login", {}, NOW)
    app = application(missions, tmp_path, state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, loaded = post(client, request(app.state.store, 54, ()))
        assert dict(zip(loaded["GL"].children[0].values, loaded["GL"].children[1].values))[1611] == 1
        clock.now += timedelta(days=1)
        before = app.state.store.profile()
        for envelope in (message(app.state.store, 58, 1611, tick=1001), message(app.state.store, 59, tick=1002)):
            _, result = post(client, envelope)
            assert set(result) == {"SYSTEM", "GLRW"} and not result["GLRW"].children[0].values
        assert app.state.store.profile() == before
        assert not bundle.presents.rows(before.state)


def test_54_refuses_and_records_when_the_catalog_changed_under_progress(bundle, tmp_path, monkeypatch):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    bootstrap.startup_diagnostics = StartupDiagnostics()
    missions = MissionService(bootstrap, bundle.player_state, bundle.presents, bundle.missions.catalog)
    store = Store(tmp_path / "save.sqlite3", seed(bundle, missions))
    monkeypatch.setattr(bundle.missions.catalog, "content_version", "f" * 64)
    with pytest.raises(ProtocolError) as caught:
        missions.handlers()[54](request(store, 54, ()), store)
    assert (caught.value.code, caught.value.status) == ("mission_catalog_changed", 503)
    record = bootstrap.startup_diagnostics.status()["loads"]["54"]
    assert record["outcome"] == "refused"
    assert [issue["code"] for issue in record["issues"]] == ["mission_dependencies_changed", "mission_catalog_changed"]


def test_all_current_reward_definitions_are_classified_without_persistent_writes(bundle, tmp_path):
    missions, _ = service(bundle)
    state = seed(bundle, missions)
    original = deepcopy(state)
    results = []
    for definition in missions.catalog.definitions:
        row = {"condition_id": definition.condition_id, "period": definition.period_type,
               "reward_type": definition.reward_item_type, "parameter": definition.reward_item_param,
               "item_id": definition.reward_item_id}
        try:
            rewards = missions._rewards((definition.condition_id,))
            candidate, mail = bundle.presents.enqueue(state, rewards)
            assert len(mail) == 1 and candidate is not state
            assert candidate["player_state"] == state["player_state"]
            for table in ("game_param", "game_param2", "crystals", "possessions"):
                assert candidate["tables"][table] == state["tables"][table]
            if definition.reward_item_type == 1:
                assert mail[0]["Content"] == definition.reward_item_param and "ItemID" not in mail[0]
            row.update(supported=True, mail_type=mail[0]["Type"], mail_content=mail[0]["Content"])
        except ProtocolError as error:
            row.update(supported=False, error=error.code, status=error.status)
        results.append(row)
        assert state == original
    unused = [row for row in results if row["reward_type"] != 22 and row["item_id"]]
    assert {row["condition_id"] for row in unused} == {1362, 1372, 1382, 1392}
    assert all(row["supported"] and row["mail_type"] == 1 and row["mail_content"] == 5 for row in unused)
    report = {"catalog_content_version": missions.catalog.content_version, "definition_count": len(results),
              "supported": sum(row["supported"] for row in results), "unused_unit_item_metadata": unused,
              "unused_metadata_evidence": ["5E8F50: Type1 selects unit branch without ItemID",
                                            "6D2C80: unit branch resolves RewardItemParam in PresentUnitData"],
              "rows": results}
    (tmp_path / "reward-coverage.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    assert len(results) == 382
    assert report["supported"] == 355
    assert {row["error"] for row in results if not row["supported"]} == {"summon_ticket_pool_disabled"}
    assert {row["condition_id"] for row in results if row["supported"] and row["item_id"] == "item_smn_ticket_19138"} == {
        1411, 1415, 1419, 1428, 1437, 1446, 1451, 1456, 1460, 1465, 1469}
    assert all(row["reward_type"] == 22 for row in results if not row["supported"])
    assert all(row["item_id"].startswith("item_smn_ticket_") for row in results if not row["supported"])
