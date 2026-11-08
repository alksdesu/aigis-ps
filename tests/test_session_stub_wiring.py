import pytest
from fastapi.testclient import TestClient

from aigis_ps import cli
from aigis_ps.app import create_app
from aigis_ps.session_stub_service import OPERATIONS, PREMIUM_RECEIVER, STARTUP_LOADS
from aigis_ps.startup_diagnostics import REFUSED, StartupLoad
from test_cli import serve_arguments
from test_evolution_integration import post
from test_quest_service import bundle
from test_session_stub_service import accepted_business, envelope, receiver


UNSUPPORTED_RECEIVER = 2


@pytest.fixture(scope="module")
def served(bundle, tmp_path_factory):
    return serve_arguments(bundle, tmp_path_factory.mktemp("serve") / "unused")


def test_serve_routes_every_stub_to_the_bundle_service(served, bundle):
    assert bundle.session_stubs.bootstrap is bundle.bootstrap
    stubs = bundle.session_stubs.handlers()
    assert {number: served["handlers"][number] for number in OPERATIONS} == stubs
    for number in STARTUP_LOADS:
        assert isinstance(stubs[number], StartupLoad)
        assert stubs[number].diagnostics is bundle.bootstrap.startup_diagnostics


def test_readiness_reports_the_stubs(bundle):
    ready = cli.service_readiness(bundle)
    assert ready["session_stubs"] == bundle.session_stubs.status()
    assert ready["session_stubs"]["implemented_operations"] == list(OPERATIONS)
    assert ready["session_stubs"]["startup_loads"] == list(STARTUP_LOADS)


def test_stubs_answer_over_http_and_refusals_reach_the_startup_record(served, bundle, tmp_path):
    app = create_app(tmp_path, **served)
    store = app.state.store
    before = store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for number in OPERATIONS:
            _, groups = post(client, envelope(store, number, *accepted_business(store, number)))
            assert groups["SYSTEM"] == bundle.bootstrap.system_container(store.profile()), number
        refused = post(client, envelope(store, PREMIUM_RECEIVER, receiver(UNSUPPORTED_RECEIVER)), 501)
        assert refused.json()["error"]["code"] == "dmm_premium_receiver_unsupported"
        status = client.get("/local/status").json()
        load = status["readiness"]["bootstrap"]["startup_diagnostics"]["loads"][str(PREMIUM_RECEIVER)]
        assert load["outcome"] == REFUSED and load["issues"][0]["code"] == "dmm_premium_receiver_unsupported"
        post(client, envelope(store, PREMIUM_RECEIVER, *accepted_business(store, PREMIUM_RECEIVER)))
        loads = client.get("/local/status").json()["readiness"]["bootstrap"]["startup_diagnostics"]["loads"]
    assert str(PREMIUM_RECEIVER) not in loads
    assert not set(OPERATIONS) & set(status["unimplemented_operations"])
    assert store.profile() == before
