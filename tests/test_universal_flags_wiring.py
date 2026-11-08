from dataclasses import replace

import pytest

from aigis_ps import cli
from aigis_ps.errors import ProtocolError
from aigis_ps.serial_code_service import OPERATION as SERIAL_CODE_OPERATION
from aigis_ps.universal_flags import set_flags
from aigis_ps.universal_flags_catalog import UniversalFlagsCatalog
from aigis_ps.universal_flags_service import OPERATION
from test_cli import assembled, serve_arguments
from test_quest_service import bundle


@pytest.fixture(scope="module")
def served(bundle, tmp_path_factory):
    return serve_arguments(bundle, tmp_path_factory.mktemp("serve") / "unused")


def test_default_bundle_builds_the_upload_over_the_served_resources(bundle):
    assert bundle.universal_flags is not None, bundle.universal_flags_error
    assert bundle.universal_flags.bootstrap is bundle.bootstrap
    assert set(bundle.universal_flags.handlers()) == {OPERATION}
    served = UniversalFlagsCatalog(lambda name: bundle.native_cdn.assets.read(name)[0])
    assert bundle.universal_flags.catalog.content_version == served.content_version
    ready = cli.service_readiness(bundle)
    assert ready["universal_flags"] == bundle.universal_flags.status() and ready["universal_flags"]["ready"] is True


def test_serve_registers_138_beside_32(served, bundle):
    assert served["handlers"][OPERATION].__self__ is bundle.universal_flags
    assert served["handlers"][SERIAL_CODE_OPERATION].__self__ is bundle.serial_codes


def test_missing_flag_service_leaves_138_unregistered(bundle, tmp_path):
    broken = replace(bundle, universal_flags=None, universal_flags_error="fixture_missing")
    assert cli.service_readiness(broken)["universal_flags"] == {"ready": False, "error": "fixture_missing"}
    assert cli.service_readiness(broken)["startup_ready"] == cli.service_readiness(bundle)["startup_ready"]
    handlers = serve_arguments(broken, tmp_path / "unused")["handlers"]
    assert OPERATION not in handlers and SERIAL_CODE_OPERATION in handlers


def test_saved_flags_load_without_the_upload_service(bundle):
    # 138 writes the startup flag table that 137 already validates, so it owns no saved state of its own.
    state = set_flags(bundle.initialize(bundle.bootstrap.initial_state()), {"wiring_saved_flag": 1})
    broken = replace(bundle, universal_flags=None, universal_flags_error="fixture_missing")
    assert broken.initialize(state)["tables"]["universal_flags"] == state["tables"]["universal_flags"]


def test_catalog_failure_is_reported_as_the_flag_service_error(monkeypatch):
    def refuse(_):
        raise ProtocolError("universal_flags_fixture_unavailable", "fixture", 503)

    monkeypatch.setattr("aigis_ps.universal_flags_catalog.UniversalFlagsCatalog", refuse)
    result = cli.services(assembled())
    assert result.universal_flags is None
    assert result.universal_flags_error == "universal_flags_fixture_unavailable"
    assert cli.service_readiness(result)["universal_flags"] == {
        "ready": False, "error": "universal_flags_fixture_unavailable"}
    assert result.serial_codes is not None and result.serial_code_error is None
