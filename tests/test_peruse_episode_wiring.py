import pytest
from fastapi.testclient import TestClient

from aigis_ps import cli, peruse_episode, peruse_state
from aigis_ps.alxml import Column
from aigis_ps.app import create_app
from aigis_ps.errors import ProtocolError
from aigis_ps.peruse_episode import EpisodeCatalog
from aigis_ps.peruse_state import PeruseCatalog, peruse_response, read_perused
from test_cli import assembled, serve_arguments
from test_evolution_integration import post
from test_peruse_episode import save_request
from test_unit_actions import request


OPERATION = 136
STARTUP_OPERATION = 137


@pytest.fixture(scope="module")
def bundle():
    def refuse(_):
        raise ProtocolError("raw_client_directory_read", "Home event catalogs must read the served resources", 503)

    # Building with the raw client directory refused proves both catalogs read the effective resource view.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(peruse_state, "asset_root_reader", refuse)
        patch.setattr(peruse_episode, "asset_root_reader", refuse)
        return cli.services(assembled())


@pytest.fixture(scope="module")
def served(bundle, tmp_path_factory):
    return serve_arguments(bundle, tmp_path_factory.mktemp("serve") / "unused")


def test_both_home_event_catalogs_read_the_served_resources(bundle):
    reader = lambda name: bundle.native_cdn.assets.read(name)[0]
    assert bundle.peruse_episodes.catalog is not None, bundle.peruse_episodes.catalog_error
    assert bundle.peruse.catalog is not None, bundle.peruse.catalog_error
    assert bundle.peruse_episodes.catalog.status() == EpisodeCatalog(reader).status()
    assert bundle.peruse.catalog.status() == PeruseCatalog(reader).status()
    assert bundle.peruse_episodes.bootstrap is bundle.peruse.bootstrap is bundle.bootstrap


def test_readiness_reports_the_episode_catalog(bundle):
    ready = cli.service_readiness(bundle)
    assert ready["peruse_episodes"] == bundle.peruse_episodes.status()
    assert ready["peruse_episodes"]["ready"] is True and ready["peruse_episodes"]["implemented_operations"] == [OPERATION]


def test_serve_routes_136_to_the_episode_service(served, bundle):
    assert served["handlers"][OPERATION].__self__ is bundle.peruse_episodes


def test_native_episode_save_is_read_back_through_137(served, bundle, tmp_path):
    config_id, event_id = min(bundle.peruse_episodes.catalog.episodes.items())
    app = create_app(tmp_path, **served)
    store = app.state.store
    with TestClient(app, base_url="http://127.0.0.1") as client:
        _, groups = post(client, save_request(store, config_id))
        assert groups["PEV"].children == (Column("ID", "S", (event_id,)), Column("FLG", "I", (1,)))
        saved = store.profile()
        assert {"ID": event_id, "FLG": 1} in read_perused(saved.state)
        _, reloaded = post(client, request(store, STARTUP_OPERATION, ()))
    assert reloaded["PEV"] == peruse_response(saved.state)


def test_episode_without_a_native_sender_is_refused_without_touching_the_save(served, tmp_path):
    app = create_app(tmp_path, **served)
    store = app.state.store
    before = store.profile()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = post(client, save_request(store, "wiring_unsent_episode"), 501)
    assert response.json()["error"]["code"] == "peruse_episode_unsupported"
    assert store.profile() == before
