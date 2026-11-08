import json

import pytest
from fastapi.testclient import TestClient

from aigis_ps import cli
from aigis_ps.app import create_app
from aigis_ps.quest_modes import EPISODE_FLAG
from aigis_ps.quest_record_service import (OPERATIONS, PERUSE_QUESTS, QuestRecordService,
                                           REPRODUCE_MISSIONS, SAVE_SHOWN)
from test_cli import serve_arguments
from test_evolution_integration import post
from test_quest_record_service import MISSION_COLUMNS, RECORD_COLUMNS, quests_request
from test_quest_service import bundle
from test_unit_actions import request


@pytest.fixture(scope="module")
def served(bundle, tmp_path_factory):
    return serve_arguments(bundle, tmp_path_factory.mktemp("serve") / "unused")


def test_the_record_service_shares_the_quest_catalog_and_served_resources(bundle):
    records = bundle.quest_records
    assert records is not None, bundle.quest_record_error
    assert records.catalog is bundle.quests.catalog and records.bootstrap is bundle.bootstrap
    assert records.perusable is not None, records.peruse_error
    reader = lambda name: bundle.native_cdn.assets.read(name)[0]
    assert records.status() == QuestRecordService(bundle.bootstrap, bundle.quests.catalog, asset_reader=reader).status()


def test_readiness_reports_the_quest_record_service(bundle):
    ready = cli.service_readiness(bundle)
    assert ready["quest_records"] == bundle.quest_records.status()
    assert ready["quest_records"]["ready"] is True
    assert ready["quest_records"]["implemented_operations"] == list(OPERATIONS)


def test_serve_routes_the_record_operations_to_the_new_service(served, bundle):
    for operation in OPERATIONS:
        assert served["handlers"][operation].__self__ is bundle.quest_records


def test_native_record_requests_answer_through_the_wire(served, bundle, tmp_path):
    app = create_app(tmp_path, **served)
    store = app.state.store
    records = bundle.quest_records
    quest_id = min(quest for quest in records.perusable if records.catalog.quest_type(quest) is not None)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, reproduce = post(client, request(store, REPRODUCE_MISSIONS, ()))
        assert [(column.name, column.values) for column in reproduce["Mission"].children] == [
            (name, ()) for name, _ in MISSION_COLUMNS]
        _, shown = post(client, request(store, SAVE_SHOWN, ()))
        assert [(column.name, column.values) for column in shown["QR"].children] == [
            (name, ()) for name in RECORD_COLUMNS]
        before = store.profile()
        _, perused = post(client, quests_request(store, PERUSE_QUESTS, (quest_id,)))
        assert set(perused) == {"SYSTEM"}
    saved = store.profile()
    assert saved.revision > before.revision
    rows = {row["QuestID"]: row for row in saved.state["tables"]["quest_records"]}
    assert rows[quest_id]["AchieveFlag"] & EPISODE_FLAG and not rows[quest_id]["Shown"]


def test_doctor_reports_missing_record_dependencies_without_resources(tmp_path, capsys):
    data = tmp_path / "server"
    assert cli.main(["doctor", "--data-dir", str(data), "--asset-dir", str(tmp_path / "missing")]) == 0
    readiness = json.loads(capsys.readouterr().out)["readiness"]
    assert readiness["quest_records"] == {"ready": False, "error": "quest_record_dependencies_unavailable"}
    assert not data.exists()
