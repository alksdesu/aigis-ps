import logging
from copy import deepcopy
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from aigis_ps.alxml import loads
from aigis_ps.app import create_app
from aigis_ps.bootstrap import BootstrapService
from aigis_ps.errors import ProtocolError
from aigis_ps.protocol import decode_wire, encode_wire, prepare_request
from aigis_ps.startup_diagnostics import StartupDiagnostics
from test_bootstrap import asset_root, columns, invoke, request, service, store


FLAG_CAPACITY = 255


def mutate(store, change):
    profile = store.profile()
    state = deepcopy(profile.state)
    change(state)
    return store.update_state(state, profile.revision)


def set_fields(table, **values):
    def change(state):
        state["tables"][table][0].update(values)
    return change


def loads_status(service):
    return service.status()["startup_diagnostics"]


def issue_codes(service, operation):
    return [issue["code"] for issue in loads_status(service)["loads"][str(operation)]["issues"]]


def refused(service, store, number, **overrides):
    with pytest.raises(ProtocolError) as caught:
        invoke(service, store, number, **overrides)
    return caught.value


def failing_month(code="login_cooking_invalid"):
    def prepare(state, now):
        raise ProtocolError(code, "Injected monthly ledger failure", 503)
    return prepare


def settling_month(state, now):
    updated = deepcopy(state)
    updated["tables"]["game_param"][0]["ExtendedMaxCharisma"] = 5
    return updated


def key_values(response):
    group = next(child for child in response.children if child.name == "PGP2")
    return dict(zip(group.children[0].values, group.children[1].values))


def flags(positions):
    text = ["0"] * FLAG_CAPACITY
    for position in positions:
        text[position] = "1"
    return "".join(text)


LEGACY_BITS = {
    "LookHarlemFlag00": 0b101, "LookHarlemFlag01": -2 ** 31, "LookHarlemFlag03": (1 << 14) | (1 << 15),
    "LookHarlemFlag10": 1, "LookHarlemFlag12": 1 << 3,
}
EXPECTED_FIRST = flags((15, 17, 78, 125))
EXPECTED_SECOND = flags((15, 82))


def test_inconsistent_progression_loads_game_param_but_parks_collection_loads(service, store):
    saved = mutate(store, set_fields("game_param", Rank=2))
    player = columns(invoke(service, store, 34))
    assert (player["A1"].values, player["A2"].values, player["A3"].values) == ((1000,), (0,), (2,))
    assert issue_codes(service, 34) == ["invalid_player_rank"]
    for number in ("quest_record", 51, 137):
        assert refused(service, store, number).code == "invalid_player_rank"
    status = loads_status(service)
    assert status["attention_required"] is True
    assert {operation: entry["outcome"] for operation, entry in status["loads"].items()} == {
        "34": "degraded", "quest_record": "refused", "51": "refused", "137": "refused"}
    assert store.profile() == saved


def test_zero_wallet_is_refused_by_34_and_every_collection_load(service, store):
    saved = mutate(store, set_fields("game_param", Money=0))
    for number in (34, 51, 129, 137):
        assert refused(service, store, number).code == "invalid_player_state"
    assert all(entry["outcome"] == "refused" for entry in loads_status(service)["loads"].values())
    assert store.profile() == saved


def test_unserializable_game_param_refuses_34_without_inventing_a_row(service, store):
    saved = mutate(store, set_fields("game_param", CharismaChangeTime=12.5))
    assert refused(service, store, 34).code == "invalid_bootstrap_value"
    assert loads_status(service)["loads"]["34"]["outcome"] == "refused"
    assert store.profile() == saved


def test_monthly_failure_serves_saved_state_and_recovers_when_settled(service, store):
    saved = store.profile()
    service.monthly_state_provider = failing_month()
    for number in (34, 35, 33):
        invoke(service, store, number)
        assert issue_codes(service, number) == ["login_cooking_invalid"]
    assert store.profile() == saved
    service.monthly_state_provider = settling_month
    assert columns(invoke(service, store, 34))["ExtendedMaxCharisma"].values == (5,)
    assert store.profile().revision == saved.revision + 1
    for number in (35, 33):
        invoke(service, store, number)
    assert loads_status(service) == {"attention_required": False, "loads": {}}


def test_monthly_write_conflicts_serve_the_latest_saved_row(service, store, monkeypatch):
    saved = store.profile()
    service.monthly_state_provider = settling_month
    def conflict(state, revision):
        raise ValueError("Profile revision changed; reload before updating")
    monkeypatch.setattr(store, "update_state", conflict)
    assert columns(invoke(service, store, 34))["ExtendedMaxCharisma"].values == (0,)
    assert issue_codes(service, 34) == ["profile_changed"]
    assert store.profile() == saved


def test_monthly_write_keeps_the_strict_progression_gate(service, store):
    saved = mutate(store, set_fields("game_param", Rank=2))
    service.monthly_state_provider = settling_month
    player = columns(invoke(service, store, 34))
    assert player["ExtendedMaxCharisma"].values == (0,) and player["A3"].values == (2,)
    assert issue_codes(service, 34) == ["invalid_player_rank"]
    assert store.profile() == saved


def test_game_param_does_not_depend_on_game_param2_or_its_monthly_projection(service, store):
    saved = mutate(store, set_fields("game_param2", GameStartDateTime="2026-10-31T00:00:00Z"))
    service.monthly_state_provider = lambda state, now: state
    assert columns(invoke(service, store, 34))["A1"].values == (1000,)
    assert "34" not in loads_status(service)["loads"]
    assert refused(service, store, 35).code == "invalid_bootstrap_value"
    service.monthly_state_provider = settling_month
    assert columns(invoke(service, store, 34))["ExtendedMaxCharisma"].values == (0,)
    assert issue_codes(service, 34) == ["invalid_bootstrap_value"]
    assert store.profile() == saved


def test_legacy_event_flags_load_and_33_saves_the_client_fallback_conversion(service, store):
    legacy = mutate(store, set_fields("game_param2", _EvCmb=6, **LEGACY_BITS))
    service.monthly_state_provider = lambda state, now: state
    assert columns(invoke(service, store, 34))["A1"].values == (1000,)
    assert columns(invoke(service, store, 35))["C7"].values == (6,)
    assert store.profile() == legacy
    values = key_values(invoke(service, store, 33))
    assert (values["AD"], values["AE"]) == (EXPECTED_FIRST, EXPECTED_SECOND)
    assert values["AF"] == values["B1"] == "0" * FLAG_CAPACITY
    assert values["C7"] == "7"
    converted = store.profile()
    assert converted.revision == legacy.revision + 1
    row = converted.state["tables"]["game_param2"][0]
    assert (row["WatchEventFlag0"], row["WatchEventFlag1"], row["_EvCmb"]) == (EXPECTED_FIRST, EXPECTED_SECOND, 7)
    assert all(row[name] == value for name, value in LEGACY_BITS.items())
    assert key_values(invoke(service, store, 33)) == values
    assert columns(invoke(service, store, 35))["AD"].values == (EXPECTED_FIRST,)
    assert store.profile() == converted
    assert loads_status(service)["loads"] == {}


def test_legacy_conversion_appends_like_the_client_and_keeps_filled_slots(service, store):
    mutate(store, set_fields("game_param2", _EvCmb=0, WatchEventFlag1="1" * 10, **LEGACY_BITS))
    row = key_values(invoke(service, store, 33))
    assert row["AE"] == ("1" * 10 + EXPECTED_SECOND)[:FLAG_CAPACITY]
    assert len(row["AE"]) == FLAG_CAPACITY
    mutate(store, set_fields("game_param2", _EvCmb=0, WatchEventFlag0="10", WatchEventFlag1="01",
                             WatchEventFlag0_2="", WatchEventFlag1_2="1"))
    row = key_values(invoke(service, store, 33))
    assert (row["AD"], row["AE"], row["AF"], row["C7"]) == ("10", "01", "0" * FLAG_CAPACITY, "1")
    assert row["B1"] == ("1" + "0" * FLAG_CAPACITY)[:FLAG_CAPACITY]
    mutate(store, set_fields("game_param2", _EvCmb=0, WatchEventFlag0_2="1", WatchEventFlag1_2=""))
    row = key_values(invoke(service, store, 33))
    assert (row["AD"], row["AF"], row["B1"], row["C7"]) == ("10", "1", "", "1")


def test_failed_legacy_conversion_is_refused_and_left_to_the_client(service, store, monkeypatch):
    legacy = mutate(store, set_fields("game_param2", _EvCmb=0))
    def conflict(state, revision):
        raise ValueError("Profile revision changed; reload before updating")
    monkeypatch.setattr(store, "update_state", conflict)
    error = refused(service, store, 33)
    assert (error.code, error.status) == ("profile_changed", 409)
    assert loads_status(service)["loads"]["33"]["outcome"] == "refused"
    assert store.profile() == legacy


def test_legacy_event_flags_no_longer_block_comeback_login_writes(service, store):
    legacy = mutate(store, set_fields("game_param2", _EvCmb=0))
    def observe(state, now):
        updated = deepcopy(state)
        updated["tables"]["game_param2"][0]["ComebackLoginDateTime"] = "2026/10/31 12:30:00"
        return updated
    service.comeback_logins = SimpleNamespace(observe=observe)
    assert invoke(service, store, 114).children[1].name == "LGI"
    saved = store.profile()
    assert saved.revision == legacy.revision + 1
    row = saved.state["tables"]["game_param2"][0]
    assert (row["ComebackLoginDateTime"], row["_EvCmb"]) == ("2026/10/31 12:30:00", 0)


def test_33_projection_serves_unconverted_rows_to_write_preflights(service, store):
    legacy = mutate(store, set_fields("game_param2", _EvCmb=0))
    candidate = SimpleNamespace(state=legacy.state, method_key=legacy.method_key)
    assert key_values(loads(service.game_param2_fields_response(candidate)))["C7"] == "0"
    assert store.profile() == legacy


def test_identity_refusals_are_recorded_and_cleared_by_the_next_clean_load(service, store):
    assert refused(service, store, 34, UK="wrong").code == "invalid_identity"
    entry = loads_status(service)["loads"]["34"]
    assert (entry["outcome"], entry["occurrences"]) == ("refused", 1)
    invoke(service, store, 34)
    assert loads_status(service)["loads"] == {}


def test_repeated_refusals_log_once_and_count_occurrences(service, store, caplog):
    mutate(store, set_fields("game_param", Rank=2))
    caplog.set_level(logging.INFO, logger="aigis_ps")
    for _ in range(3):
        refused(service, store, 51)
    entry = loads_status(service)["loads"]["51"]
    assert entry["occurrences"] == 3 and entry["first_seen"] <= entry["last_seen"]
    assert [record.levelno for record in caplog.records if "Startup load 51" in record.getMessage()] == [logging.ERROR]
    mutate(store, set_fields("game_param", Rank=1))
    invoke(service, store, 51)
    assert [record.levelno for record in caplog.records if "Startup load 51" in record.getMessage()] == [logging.ERROR, logging.INFO]


def test_shared_registry_and_cli_rewrapping_record_each_load_once(asset_root, store):
    diagnostics = StartupDiagnostics(clock=lambda: "2026-11-02T00:00:00+00:00")
    service = BootstrapService(asset_root, startup_diagnostics=diagnostics)
    handlers = service.handlers()
    assert diagnostics.wrap(34, handlers[34]) is handlers[34]
    mutate(store, set_fields("game_param", Rank=2))
    diagnostics.wrap(34, handlers[34])(request(34, store), store)
    entry = diagnostics.status()["loads"]["34"]
    assert (entry["occurrences"], entry["first_seen"]) == (1, "2026-11-02T00:00:00+00:00")
    with pytest.raises(ValueError, match="StartupDiagnostics"):
        BootstrapService(asset_root, startup_diagnostics={})


def test_operator_status_reports_startup_problems_over_http(tmp_path, service):
    state = service.initial_state()
    state["tables"]["game_param"][0]["Rank"] = 2
    app = create_app(tmp_path / "startup-http", handlers=service.handlers(), initial_state=state,
                     status_provider=lambda: {"bootstrap": service.status()})

    def post(number):
        envelope = request(number, app.state.store)
        xml = prepare_request(ET.tostring(envelope.document), envelope.profile.st)
        return client.post("/api/" + envelope.profile.route, content=encode_wire(xml, envelope.profile.request_key))

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = post(34)
        assert response.status_code == 200
        player = columns(loads(decode_wire(response.content, app.state.registry.by_registration[34].response_key)))
        assert player["A3"].values == (2,)
        assert post(51).status_code == 503
        diagnostics = client.get("/local/status").json()["readiness"]["bootstrap"]["startup_diagnostics"]
        assert diagnostics["attention_required"] is True
        assert diagnostics["loads"]["34"]["outcome"] == "degraded"
        assert diagnostics["loads"]["51"]["issues"][0]["code"] == "invalid_player_rank"
        profile = app.state.store.profile()
        repaired = deepcopy(profile.state)
        repaired["tables"]["game_param"][0]["Rank"] = 1
        app.state.store.update_state(repaired, profile.revision)
        assert post(34).status_code == post(51).status_code == 200
        assert client.get("/health").json()["readiness"]["bootstrap"]["startup_diagnostics"] == {
            "attention_required": False, "loads": {}}


def failing_record(code):
    def record(state, *arguments):
        raise ProtocolError(code, "Injected optional login record failure", 503)
    return record


@pytest.mark.parametrize("hook,code", [("comeback_logins", "comeback_login_dependencies_changed"),
                                       ("missions", "mission_dependencies_changed")])
def test_login_answers_with_stored_values_when_its_optional_records_fail(service, store, hook, code):
    saved = store.profile()
    setattr(service, hook, SimpleNamespace(observe=failing_record(code), record=failing_record(code)))
    login = invoke(service, store, 114).children[1]
    assert login.name == "LGI" and login.children[3].values == login.children[4].values == (1,)
    assert store.profile() == saved
    assert loads_status(service)["loads"]["114"]["outcome"] == "degraded"
    assert issue_codes(service, 114) == [code]
    setattr(service, hook, None)
    invoke(service, store, 114)
    assert loads_status(service)["loads"] == {}


def test_login_records_that_fail_their_preflight_are_not_saved(service, store):
    saved = store.profile()

    def observe(state, now):
        updated = deepcopy(state)
        updated["tables"]["game_param2"][0]["GameStartDateTime"] = "2026-10-31T00:00:00Z"
        return updated

    service.comeback_logins = SimpleNamespace(observe=observe)
    assert invoke(service, store, 114).children[1].name == "LGI"
    assert store.profile() == saved
    assert issue_codes(service, 114) == ["invalid_bootstrap_value"]


def test_login_write_conflicts_answer_with_the_latest_stored_values(service, store, monkeypatch):
    saved = store.profile()
    attempts = []

    def observe(state, now):
        attempts.append(now)
        return {**deepcopy(state), "comeback_marker": len(attempts)}

    def conflict(state, revision):
        raise ValueError("Profile revision changed; reload before updating")

    service.comeback_logins = SimpleNamespace(observe=observe)
    monkeypatch.setattr(store, "update_state", conflict)
    assert invoke(service, store, 114).children[1].name == "LGI"
    assert len(attempts) == 3 and store.profile() == saved
    assert issue_codes(service, 114) == ["profile_changed"]


def test_login_of_an_inconsistent_account_answers_without_recording(service, store):
    saved = mutate(store, set_fields("game_param", Rank=2))
    observed = []
    service.comeback_logins = SimpleNamespace(observe=lambda state, now: observed.append(now) or state)
    login = invoke(service, store, 114).children[1]
    assert login.children[3].values == login.children[4].values == (2,)
    assert observed == [] and store.profile() == saved
    assert issue_codes(service, 114) == ["invalid_player_rank"]


def test_login_without_a_loadable_player_row_is_refused(service, store):
    saved = mutate(store, set_fields("game_param", Money=0))
    assert refused(service, store, 114).code == "invalid_player_state"
    assert loads_status(service)["loads"]["114"]["outcome"] == "refused"
    assert store.profile() == saved
