from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from aigis_ps import cli, letstd_catalog
from aigis_ps.alxml import loads
from aigis_ps.config_registry import REGISTRY, TimeSemantics
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.letstd_rules import MAX_RULE_BYTES, LetsTDRules, load_letstd_rules
from aigis_ps.letstd_service import LetsTDService
from aigis_ps.letstd_state import LEDGER_KEY, SOURCES
from aigis_ps.login_reward_service import LETSTD_SOURCE
from aigis_ps.rank_rewards import LETSTD_SECTION
from aigis_ps.storage import Store
from test_cli import LETSTD_FLAG, LETSTD_PARAMETER, assembled, serve_arguments
from test_letstd_dispatch import login_request, rank_request


PROJECT = Path(__file__).resolve().parents[1]
EXAMPLE = PROJECT / "config/letstd-rewards.example.json"
KEY, PREVIOUS_KEY = "letstd-rewards", "serial-code-rules"
CONFIGURED = LetsTDRules(native_popup=True)
ONE_GROUP = LetsTDRules(partners={"rlyeh": {"newPurchase": True}})
SETTLED_AT = datetime(2026, 11, 3, 12, tzinfo=JST)
UNKNOWN_FIELD = {**LetsTDRules().to_dict(), "unknown": True}
UNSUPPORTED_LEDGER = {"schema_version": 2}
MARKERS = (("login_reward_delivery", LETSTD_SOURCE), ("rank_reward_delivery", LETSTD_SECTION))
SYSTEM_NODE = "SYSTEM"
DEPENDENCIES_UNAVAILABLE = "letstd_dependencies_unavailable"
CATALOG_INVALID = "letstd_catalog_invalid"
SERVICE_UNAVAILABLE = "letstd_unavailable"
STATE_INVALID = "letstd_state_invalid"


@pytest.fixture(scope="module")
def configured(tmp_path_factory):
    path = tmp_path_factory.mktemp("letstd-rules") / "letstd-rewards.json"
    path.write_text(json.dumps(CONFIGURED.to_dict()), encoding="utf-8")
    return cli.services(assembled(letstd_rewards=path))


@pytest.fixture(scope="module")
def refused():
    def refuse(asset_reader):
        raise ProtocolError(CATALOG_INVALID, "LetsTD resources are refused for this test", 503)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(letstd_catalog, "LetsTDCatalog", refuse)
        return cli.services(assembled())


@pytest.fixture(scope="module")
def initial(configured):
    return configured.initialize(configured.bootstrap.initial_state())


@pytest.fixture(scope="module")
def delivered(configured, initial):
    opened = LetsTDService(configured.letstd.catalog, configured.presents, ONE_GROUP)
    state, grants = opened.settle(deepcopy(initial), SETTLED_AT, source=SOURCES[0])
    assert grants and LEDGER_KEY in state
    return state, grants


@pytest.fixture
def played(configured, refused, tmp_path, monkeypatch):
    for bundle in (configured, refused):
        monkeypatch.setattr(bundle.bootstrap, "clock", lambda: SETTLED_AT)
        monkeypatch.setattr(bundle.login_rewards, "clock", lambda: SETTLED_AT)
    start = configured.initialize(configured.bootstrap.initial_state())

    def play(bundle, name):
        store = Store(tmp_path / f"{name}.sqlite3", start)
        replies = (bundle.login_rewards.receive(login_request(store), store),
                   bundle.rank_rewards.receive(rank_request(store, tick=1001), store))
        return store.profile().state, replies

    return play


def refusal(action):
    with pytest.raises(ProtocolError) as caught:
        action()
    return caught.value.code, caught.value.status


def reply_nodes(reply):
    # SYSTEM carries the exchange key of each save.
    return [node for node in loads(reply).children if node.name != SYSTEM_NODE]


def test_the_rules_key_follows_the_serial_code_table_with_its_flag_loader_and_example():
    spec = REGISTRY[KEY]
    assert (spec.cli_flag, spec.ps_parameter, spec.rules_type()) == (LETSTD_FLAG, LETSTD_PARAMETER, LetsTDRules)
    assert (spec.examples, spec.domains, spec.max_bytes) == (
        (EXAMPLE.name,), ("letstd", "login_rewards", "rank_rewards"), MAX_RULE_BYTES)
    assert (spec.time_semantics, spec.resource_hint) == (TimeSemantics.ELIGIBILITY, False)
    assert spec.default() == load_letstd_rules(None) == load_letstd_rules(EXAMPLE) == LetsTDRules()
    assert spec.normalize(spec.load(EXAMPLE)) == LetsTDRules().to_dict()
    keys = list(REGISTRY)
    assert keys[keys.index(PREVIOUS_KEY) + 1] == KEY


def test_invalid_rules_stop_doctor_without_creating_a_save(tmp_path, capsys):
    data, path = tmp_path / "server", tmp_path / "letstd.json"
    path.write_text(json.dumps(UNKNOWN_FIELD), encoding="utf-8")
    assert cli.main(["doctor", "--data-dir", str(data), "--asset-dir", str(tmp_path / "missing"),
                     LETSTD_FLAG, str(path)]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "ValueError",
                                                   "message": "LetsTD configuration contains unknown fields"}
    assert not data.exists()


def test_doctor_reports_the_missing_dependencies_without_resources(tmp_path, capsys):
    data = tmp_path / "server"
    assert cli.main(["doctor", "--data-dir", str(data), "--asset-dir", str(tmp_path / "missing")]) == 0
    readiness = json.loads(capsys.readouterr().out)["readiness"]
    assert readiness["letstd"] == {"ready": False, "error": DEPENDENCIES_UNAVAILABLE}
    assert not data.exists()


def test_configured_rules_reach_one_service_shared_by_both_login_operations(configured):
    letstd = configured.letstd
    assert configured.letstd_error is None and letstd.rules == CONFIGURED != LetsTDRules()
    assert configured.login_rewards.letstd is letstd and configured.rank_rewards.letstd is letstd
    assert letstd.presents is configured.presents
    # Readiness fails with a changed dependency fingerprint if the ticket services attach after construction.
    assert letstd.status()["ready"] is True
    assert configured.login_rewards.status()["ready"] is True and configured.rank_rewards.status()["ready"] is True


def test_readiness_marks_the_simulation_outside_startup(configured, refused):
    ready = cli.service_readiness(configured)
    assert ready["letstd"] == configured.letstd.status()
    assert (ready["letstd"]["simulated"], ready["letstd"]["native_tested"], ready["letstd"]["native_popup"]) == (
        True, False, CONFIGURED.native_popup)
    assert cli.service_readiness(refused)["startup_ready"] == ready["startup_ready"]


def test_a_refused_catalog_leaves_the_login_operations_without_letstd(refused):
    assert (refused.letstd, refused.letstd_error) == (None, CATALOG_INVALID)
    assert refused.login_rewards.letstd is None and refused.rank_rewards.letstd is None
    ready = cli.service_readiness(refused)
    assert ready["letstd"] == {"ready": False, "error": CATALOG_INVALID}
    assert ready["login_rewards"]["ready"] is True and ready["rank_rewards"]["ready"] is True


def test_saves_without_letstd_rewards_load_with_or_without_the_service(configured, refused, initial):
    assert LEDGER_KEY not in initial
    assert configured.initialize(deepcopy(initial)) == initial == refused.initialize(deepcopy(initial))


def test_saves_written_without_letstd_load_once_it_is_wired(configured, refused, played):
    saved, _ = played(refused, "unwired")
    assert LEDGER_KEY not in saved and not any(saved[marker].get(section) for marker, section in MARKERS)
    assert configured.initialize(deepcopy(saved)) == saved


def test_switched_off_groups_keep_native_replies_and_saves_load_without_the_service(configured, refused, played):
    saved, replies = played(configured, "wired")
    _, unwired = played(refused, "unwired")
    assert LEDGER_KEY not in saved and [saved[marker][section] for marker, section in MARKERS] == [[], []]
    assert configured.initialize(deepcopy(saved)) == saved == refused.initialize(deepcopy(saved))
    assert list(map(reply_nodes, replies)) == list(map(reply_nodes, unwired))


def test_a_saved_ledger_requires_the_service_and_stays_a_fixed_point(configured, refused, delivered):
    state, _ = delivered
    assert configured.initialize(deepcopy(state)) == state
    assert refusal(lambda: refused.initialize(deepcopy(state))) == (SERVICE_UNAVAILABLE, 503)


def test_the_service_validates_the_saved_ledger(configured, initial):
    assert refusal(lambda: configured.initialize({**deepcopy(initial), LEDGER_KEY: UNSUPPORTED_LEDGER})) == (
        STATE_INVALID, 503)


@pytest.mark.parametrize("marker,section", MARKERS)
def test_recorded_grants_without_their_ledger_require_the_service(refused, initial, delivered, marker, section):
    _, grants = delivered
    damaged = {**deepcopy(initial), marker: {section: deepcopy(grants)}}
    assert refusal(lambda: refused.initialize(damaged)) == (SERVICE_UNAVAILABLE, 503)


def test_the_server_answers_both_login_operations_through_the_shared_service(configured, tmp_path):
    served = serve_arguments(configured, tmp_path / "not-created")
    login, rank = (served["handlers"][operation].__self__ for operation in SOURCES)
    assert login is configured.login_rewards and rank is configured.rank_rewards
    assert login.letstd is rank.letstd is configured.letstd
    assert LEDGER_KEY not in served["initial_state"] and not (tmp_path / "not-created").exists()
