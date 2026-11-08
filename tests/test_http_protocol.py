from dataclasses import replace
import hashlib
import json
import logging
import sqlite3
import pytest

from fastapi.testclient import TestClient

from aigis_ps.app import create_app, requested_route
from aigis_ps.asset_service import AssetService
from aigis_ps.assets import asset_key
from aigis_ps.cli import service_readiness
from aigis_ps.errors import ProtocolError
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.native_cdn import LOCAL_CDN_PATH
from aigis_ps.protocol import ProtocolRegistry, decode_wire, encode_wire, prepare_request
from aigis_ps.startup_diagnostics import StartupDiagnostics
from test_quest_service import bundle


STARTUP_TIME = "2026-11-02T00:00:00+00:00"
HEALTH_BUDGET_BYTES = 32 * 1024
BOOTSTRAP_DETAILS = ("startup_blockers", "unavailable", "startup_diagnostics")
CLEAN_STARTUP = {"attention_required": False, "loads": {}}
PROFILE_FIELDS = ("player_id", "revision", "save_schema", "unimplemented_operations")


def test_binary_post_prefix_health_and_safe_request_log(tmp_path):
    xml_response = b'<DA><ExchangeKey T="S"><V>local</V></ExchangeKey></DA>'
    handler = lambda envelope, store: xml_response
    app = create_app(tmp_path, handlers={31: handler})
    registry = app.state.registry
    profile = registry.by_registration[31]
    xml_request = prepare_request(b"<DA><UK>not-for-logs</UK></DA>", profile.st)
    wire = encode_wire(xml_request, profile.request_key)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/health").json()["service"] == "aigis-ps"
        response = client.post("/api/" + profile.route, content=wire, headers={"Content-Type": "application/octet-stream"})
        assert response.status_code == 200
        assert decode_wire(response.content, profile.response_key) == xml_response
        assert client.post("/" + profile.route, content=wire).content == response.content
        log = client.get("/local/requests").json()
        assert "not-for-logs" not in str(log)
        assert len(log["requests"]) == 2


def test_unimplemented_operation_is_not_a_success_stub(tmp_path):
    app = create_app(tmp_path)
    profile = ProtocolRegistry.load().by_registration[81]
    wire = encode_wire(prepare_request(b"<DA></DA>", profile.st), profile.request_key)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/api/" + profile.route, content=wire)
        assert response.status_code == 501
        assert response.json()["error"]["code"] == "not_implemented"
        assert client.get("/local/requests").json()["requests"][0]["status"] == 501


def test_invalid_host_and_browser_origin_are_rejected(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        assert client.get("/health", headers={"Host": "unrelated.invalid"}).status_code == 400
        assert client.post("/api/PeMDvjps", content=b"", headers={"Origin": "https://unrelated.invalid"}).status_code == 403


def test_asset_serving_is_approved_and_version_bound(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    data = b"ALTBpayload"
    key = asset_key("Example.atb")
    (source / key).write_bytes(data)
    private_key = asset_key("private")
    (source / private_key).write_bytes(b"private")
    manifest = {"source_root": str(source), "entries": [
        {"filename_hash": key, "source_path": key, "magic": "ALTB", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "md5": {"status": "absent"}},
        {"filename_hash": private_key, "source_path": private_key, "magic": "other", "size": 7, "sha256": "unused", "md5": {"status": "absent"}},
    ]}
    with TestClient(create_app(tmp_path / "save", assets=AssetService(manifest)), base_url="http://127.0.0.1") as client:
        response = client.get("/assets/Example.atb")
        assert response.content == data
        assert client.get("/assets/Example.atb", headers={"If-None-Match": response.headers["ETag"]}).status_code == 304
        assert client.get("/assets/" + private_key).status_code == 404
        (source / key).write_bytes(b"ALTBchanged")
        assert client.get("/assets/Example.atb").status_code == 409


@pytest.mark.parametrize("manifest", [[], {}, {"source_root": ".", "entries": [{}]}])
def test_malformed_resource_manifest_has_explicit_error(manifest):
    with pytest.raises(ValueError):
        AssetService(manifest)


def test_session_flags_error_does_not_crash_the_native_two_field_reader(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        response = client.post("/api/E935RTof", content=b"<UI>1</UI>")
        assert response.status_code == 501 and response.content == b"0,0"
        response = client.post("/api/E935RTof", content=b"invalid")
        assert response.status_code == 400 and response.content == b"0,0"
        assert client.get("/local/requests").json()["requests"][0]["status"] == 400


@pytest.mark.parametrize("path", ["/api/E935RTof", "/E935RTof"])
@pytest.mark.parametrize("headers,status", [({"Host": "192.168.1.5:18103"}, 400), ({"Origin": "https://unrelated.invalid"}, 403)])
def test_local_origin_refusal_keeps_the_native_two_field_reply(tmp_path, caplog, path, headers, status):
    caplog.set_level(logging.WARNING, logger="aigis_ps")
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(path, content=b"<UI>1</UI>", headers=headers)
        assert response.status_code == status and response.content == b"0,0"
        assert response.headers["content-type"].startswith("text/plain")
        assert client.get("/local/requests").json()["requests"] == []
    assert any(record.levelno == logging.WARNING and "Refused POST" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("path", ["/api/PeMDvjps", "/health", "/local/status"])
def test_local_origin_refusal_of_other_routes_stays_json(tmp_path, path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        response = client.post(path, content=b"", headers={"Host": "192.168.1.5"})
        assert response.status_code == 400 and response.json() == {"error": {"code": "invalid_host"}}
        response = client.get(path, headers={"Origin": "https://unrelated.invalid"})
        assert response.status_code == 403 and response.json() == {"error": {"code": "cross_origin_denied"}}


@pytest.mark.parametrize("path,route", [("/E935RTof", "E935RTof"), ("/api/E935RTof", "E935RTof"), ("/api", "api"),
                                        ("/", ""), ("/api/E935RTof/x", ""), ("/assets/E935RTof", ""), ("/api/", "")])
def test_requested_route_matches_the_operation_paths(path, route):
    assert requested_route(path) == route


def test_request_log_failure_does_not_replace_native_replies(tmp_path, caplog, monkeypatch):
    xml_response = b'<DA><ExchangeKey T="S"><V>local</V></ExchangeKey></DA>'
    app = create_app(tmp_path, handlers={31: lambda envelope, store: xml_response})
    profile = app.state.registry.by_registration[31]
    wire = encode_wire(prepare_request(b"<DA></DA>", profile.st), profile.request_key)

    def unavailable(*arguments):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app.state.store, "record_request", unavailable)
    caplog.set_level(logging.ERROR, logger="aigis_ps")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/api/E935RTof", content=b"<UI>1</UI>")
        assert response.status_code == 501 and response.content == b"0,0"
        response = client.post("/api/" + profile.route, content=wire)
        assert response.status_code == 200 and decode_wire(response.content, profile.response_key) == xml_response
        response = client.get(LOCAL_CDN_PATH + "/resources/object")
        assert response.status_code == 503 and response.json()["error"]["code"] == "native_cdn_unavailable"
    assert sum("Request log write failed" in record.getMessage() for record in caplog.records) == 3


def oversized_reply(limit):
    text = "".join(hashlib.sha256(str(index).encode()).hexdigest() for index in range(limit // 32 + 1))
    return b'<DA><KEY T="S"><V>' + text.encode() + b"</V></KEY></DA>"


def scripted(replies):
    def load(envelope, store):
        if isinstance(replies[-1], ProtocolError):
            raise replies[-1]
        return replies[-1]
    return load


def named(registry, name):
    return next(profile for profile in registry.profiles if profile.name == name)


def test_startup_reply_encoding_failures_stay_on_the_startup_record(tmp_path):
    diagnostics = StartupDiagnostics(clock=lambda: STARTUP_TIME)
    replies = []
    registry = ProtocolRegistry.load()
    game_info = named(registry, "game_info")
    heartbeat = named(registry, "heartbeat")
    app = create_app(tmp_path, registry=registry, handlers={
        "game_info": diagnostics.wrap("game_info", scripted(replies)),
        "heartbeat": lambda envelope, store: oversized_reply(heartbeat.response_limit)})
    with TestClient(app, base_url="http://127.0.0.1") as client:
        def post():
            return client.post("/api/" + game_info.route, content=b"<DA></DA>")

        replies.append(ProtocolError("native_response_unavailable", "Injected missing response", 503))
        assert post().status_code == 503
        replies.append(oversized_reply(game_info.response_limit))
        for occurrences in (1, 2):
            response = post()
            assert response.status_code == 500 and response.json()["error"]["code"] == "response_too_large"
            entry = diagnostics.status()["loads"]["game_info"]
            assert (entry["outcome"], entry["occurrences"]) == ("refused", occurrences)
            assert [issue["code"] for issue in entry["issues"]] == ["response_too_large"]
        replies.append(b"<DA></DA>")
        response = post()
        assert response.status_code == 200 and response.content == b"<DA></DA>"
        assert diagnostics.status() == CLEAN_STARTUP
        wire = encode_wire(prepare_request(b"<DA></DA>", heartbeat.st), heartbeat.request_key)
        response = client.post("/api/" + heartbeat.route, content=wire)
        assert response.status_code == 500 and response.json()["error"]["code"] == "response_too_large"
        assert diagnostics.status()["loads"] == {}
        assert [entry["status"] for entry in client.get("/local/requests").json()["requests"]] == [500, 200, 500, 500, 503]


def health_value(value):
    if isinstance(value, str):
        return not any(character.isspace() for character in value)
    return value is None or isinstance(value, (int, float))


def health_projection(readiness):
    projection = {}
    for domain, status in readiness.items():
        if isinstance(status, dict):
            projection[domain] = {name: value for name, value in status.items()
                                  if health_value(value) or (domain == "bootstrap" and name in BOOTSTRAP_DETAILS)}
        elif health_value(status):
            projection[domain] = status
    return projection


def recorded(provider):
    reports = []

    def readiness():
        reports.append(provider())
        return reports[-1]
    return readiness, reports


def wire_json(value):
    return json.loads(json.dumps(value))


def every_operation(registry):
    return [*registry.by_registration, *(profile.name for profile in registry.profiles if not profile.registration_keys)]


def test_health_keeps_values_and_identifiers_while_status_keeps_the_full_report(tmp_path):
    readiness = {
        "startup_ready": False, "mode": "offline", "scope": "Local play only", "notes": ["nested"],
        "masterdata": {"ready": False, "code": "masterdata_missing", "message": "Import masters", "required": ["cards"]},
        "bootstrap": {"startup_ready": False, "asset_version": None, "schemas": {"GameParam": 12},
                      "startup_blockers": ["master_provider"], "unavailable": {"GameParam": "Missing resource"},
                      "startup_diagnostics": CLEAN_STARTUP, "unimplemented_operations": ["master_cards"]},
        "presents": {"ready": True, "error": None, "ratio": 0.5, "templates": 2043, "supported_types": [16, 22],
                     "unavailable": {"PresentItem": "Missing resource"}, "items": {"count": 3}},
    }
    with TestClient(create_app(tmp_path, status_provider=lambda: readiness), base_url="http://127.0.0.1") as client:
        health = client.get("/health").json()
        status = client.get("/local/status").json()
    assert health["readiness"] == {
        "startup_ready": False, "mode": "offline",
        "masterdata": {"ready": False, "code": "masterdata_missing"},
        "bootstrap": {"startup_ready": False, "asset_version": None, "startup_blockers": ["master_provider"],
                      "unavailable": {"GameParam": "Missing resource"}, "startup_diagnostics": CLEAN_STARTUP},
        "presents": {"ready": True, "error": None, "ratio": 0.5, "templates": 2043},
    }
    assert status["readiness"] == readiness
    assert "master_cards" in status["unimplemented_operations"]
    assert {name: value for name, value in status.items() if name not in PROFILE_FIELDS} == {
        **health, "readiness": readiness}


def test_health_reads_startup_diagnostics_on_every_request(tmp_path):
    diagnostics = StartupDiagnostics(clock=lambda: STARTUP_TIME)
    replies = [ProtocolError("native_response_unavailable", "Injected missing response", 503)]
    registry = ProtocolRegistry.load()
    game_info = named(registry, "game_info")
    app = create_app(tmp_path, registry=registry, handlers={"game_info": diagnostics.wrap("game_info", scripted(replies))},
                     status_provider=lambda: {"bootstrap": {"startup_diagnostics": diagnostics.status()}})
    with TestClient(app, base_url="http://127.0.0.1") as client:
        def startup_diagnostics():
            return client.get("/health").json()["readiness"]["bootstrap"]["startup_diagnostics"]

        def post():
            return client.post("/api/" + game_info.route, content=b"<DA></DA>")

        assert startup_diagnostics() == CLEAN_STARTUP
        assert post().status_code == 503
        refused = diagnostics.status()
        assert refused["loads"]["game_info"]["outcome"] == "refused"
        assert startup_diagnostics() == refused
        replies.append(b"<DA></DA>")
        assert post().status_code == 200
        assert startup_diagnostics() == CLEAN_STARTUP


def test_effective_health_fits_its_budget_even_with_every_operation_configured(bundle, tmp_path):
    registry = ProtocolRegistry.load()
    readiness, reports = recorded(lambda: service_readiness(bundle))
    handlers = dict.fromkeys(every_operation(registry), lambda envelope, store: b"<DA></DA>")
    app = create_app(tmp_path, registry=registry, handlers=handlers, assets=bundle.native_cdn.assets,
                     native_cdn=bundle.native_cdn, status_provider=readiness)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        health = client.get("/health")
        status = client.get("/local/status").json()
    assert health.status_code == 200 and len(health.content) <= HEALTH_BUDGET_BYTES
    compact, served, full = health.json()["readiness"], wire_json(reports[0]), wire_json(reports[1])
    assert compact == health_projection(served) and set(compact) == set(served)
    assert compact["startup_ready"] is served["startup_ready"]
    assert compact["bootstrap"]["startup_diagnostics"] == served["bootstrap"]["startup_diagnostics"]
    assert health.json()["native_cdn"] == wire_json(bundle.native_cdn.status())
    assert status["readiness"] == full
    assert status["unimplemented_operations"] == sorted(set(full["bootstrap"]["unimplemented_operations"]), key=str)


def test_health_of_a_fresh_data_directory_keeps_the_masterdata_code(bundle, tmp_path):
    fresh = replace(bundle, masters=MasterDataStore(tmp_path / "masterdata"), master_snapshot=None)
    readiness, reports = recorded(lambda: service_readiness(fresh))
    app = create_app(tmp_path / "save", native_cdn=bundle.native_cdn, status_provider=readiness)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        compact = client.get("/health").json()["readiness"]
        status = client.get("/local/status").json()
    served = wire_json(reports[0])
    assert compact == health_projection(served)
    assert compact["masterdata"]["code"] == "masterdata_missing"
    assert compact["startup_ready"] is served["startup_ready"] is False
    assert status["readiness"] == wire_json(reports[1])
