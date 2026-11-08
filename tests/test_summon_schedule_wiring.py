import pytest
from fastapi.testclient import TestClient

from aigis_ps import cli
from aigis_ps.alxml import Container
from aigis_ps.app import create_app
from aigis_ps.schedule_capture import SCHEDULE_GROUPS
from aigis_ps.startup_diagnostics import StartupLoad
from aigis_ps.summon_schedule import OPERATIONS
from test_cli import serve_arguments
from test_evolution_integration import post
from test_quest_service import bundle
from test_session_stub_service import envelope


@pytest.fixture(scope="module")
def served(bundle, tmp_path_factory):
    return serve_arguments(bundle, tmp_path_factory.mktemp("serve") / "unused")


def test_serve_routes_both_operations_to_the_bundle_service(served, bundle):
    assert bundle.summon_schedule.bootstrap is bundle.bootstrap
    assert {number: served["handlers"][number] for number in OPERATIONS} == bundle.summon_schedule.handlers()
    assert not any(isinstance(served["handlers"][number], StartupLoad) for number in OPERATIONS)


def test_readiness_reports_the_schedule_without_a_startup_load(bundle):
    ready = cli.service_readiness(bundle)
    assert ready["summon_schedule"] == bundle.summon_schedule.status()
    assert ready["summon_schedule"]["implemented_operations"] == list(OPERATIONS)
    assert not set(OPERATIONS) & set(cli.NATIVE_STARTUP_LOADS)
    assert set(OPERATIONS) <= set(cli.AUTO_SENT_OPERATIONS)
    assert not set(OPERATIONS) & set(ready["auto_requests"]["unhandled"])


def test_schedule_answers_over_http_without_touching_the_save(served, bundle, tmp_path):
    app = create_app(tmp_path, **served)
    store = app.state.store
    before = store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for number in OPERATIONS:
            _, groups = post(client, envelope(store, number))
            assert groups == {"SYSTEM": bundle.bootstrap.system_container(store.profile()),
                              **{name: Container(name) for name in SCHEDULE_GROUPS[number]}}, number
        status = client.get("/local/status").json()
    assert not set(OPERATIONS) & set(status["unimplemented_operations"])
    assert store.profile() == before and store.profile().revision == before.revision
