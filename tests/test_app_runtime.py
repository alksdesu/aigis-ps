from dataclasses import dataclass
import hashlib
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aigis_ps.app import create_app, observed_operations
from aigis_ps.errors import ProtocolError
from aigis_ps.generation import STARTUP_SEQUENCE, Generation, GenerationIdentity, GenerationRuntime
from aigis_ps.native_cdn import LOCAL_CDN_PATH, CdnPayload
from aigis_ps.protocol import ProtocolRegistry, decode_wire, encode_wire, prepare_request
from aigis_ps.startup_diagnostics import REFUSED, StartupDiagnostics


BASE = "http://127.0.0.1"
SESSION_OPERATION, QUIET_OPERATION = 31, 81
FIRST_REPLY = b'<DA><ExchangeKey T="S"><V>first</V></ExchangeKey></DA>'
SECOND_REPLY = b'<DA><ExchangeKey T="S"><V>second</V></ExchangeKey></DA>'
EMPTY = b"<DA></DA>"
CDN_GROUP, CDN_OBJECT = "0" * 40, "object"
CDN_PATH = f"{LOCAL_CDN_PATH}/{CDN_GROUP}/{CDN_OBJECT}"
CDN_ROUTE = f"cdn/{CDN_GROUP}/{CDN_OBJECT}"
STARTUP_TIME = "2026-11-02T00:00:00+00:00"
CLEAN_STARTUP = {"attention_required": False, "loads": {}}
REFUSAL = "fixture_refused"
SWITCHING = (503, "generation_switching")
SHORT = 0.05
WAIT = 5.0


@dataclass(frozen=True)
class Resources:
    content: bytes

    def status(self) -> dict:
        return {"version": hashlib.sha1(self.content).hexdigest(), "ready": True}

    def read(self, group: str, object_name: str) -> CdnPayload:
        return CdnPayload(self.content, hashlib.sha256(self.content).hexdigest())


def readiness(startup_ready=True):
    return {"startup_ready": startup_ready,
            "bootstrap": {"startup_ready": startup_ready, "startup_diagnostics": CLEAN_STARTUP}}


def generation(name, handlers, *, resources=None, diagnostics=None, startup_ready=True):
    identity = GenerationIdentity(code_digest="c" * 64, config_set=name, content_versions={})
    return Generation(identity, SimpleNamespace(native_cdn=resources), handlers, readiness(startup_ready),
                      diagnostics=diagnostics)


def replying(reply):
    return lambda envelope, store: reply


def serving(tmp_path, runtime):
    return TestClient(create_app(tmp_path, runtime=runtime), base_url=BASE)


def post(client, number):
    profile = client.app.state.registry.by_registration[number]
    return client.post("/api/" + profile.route, content=encode_wire(prepare_request(EMPTY, profile.st), profile.request_key))


def decoded(client, response, number):
    assert response.status_code == 200, response.text
    return decode_wire(response.content, client.app.state.registry.by_registration[number].response_key)


def startup_loads(report):
    return report["readiness"]["bootstrap"]["startup_diagnostics"]


def served(client):
    health = client.get("/health").json()
    return {"reply": decoded(client, post(client, SESSION_OPERATION), SESSION_OPERATION),
            "resource": client.get(CDN_PATH).content, "handlers": health["configured_handlers"],
            "startup_ready": health["readiness"]["startup_ready"], "native_cdn": health["native_cdn"],
            "unimplemented": QUIET_OPERATION in client.get("/local/status").json()["unimplemented_operations"]}


def test_runtime_operations_run_the_current_handler_inside_a_lease(tmp_path):
    leases = []

    def leased(envelope, store):
        leases.append(runtime.status()["current"]["leases"])
        return FIRST_REPLY

    runtime = GenerationRuntime(generation("first", {SESSION_OPERATION: leased}))
    with serving(tmp_path, runtime) as client:
        assert decoded(client, post(client, SESSION_OPERATION), SESSION_OPERATION) == FIRST_REPLY
        logged = client.get("/local/requests").json()["requests"]
    assert leases == [1] and runtime.status()["current"]["leases"] == 0
    assert [(entry["status"], entry["error_code"]) for entry in logged] == [(200, None)]


def test_a_swap_switches_operations_health_and_native_resources(tmp_path):
    first = generation("first", {SESSION_OPERATION: replying(FIRST_REPLY)}, resources=Resources(b"first"),
                       startup_ready=False)
    second = generation("second", {SESSION_OPERATION: replying(SECOND_REPLY), QUIET_OPERATION: replying(EMPTY)},
                        resources=Resources(b"second"))
    runtime = GenerationRuntime(first)
    with serving(tmp_path, runtime) as client:
        before = served(client)
        runtime.swap(second, first.id)
        after = served(client)
    assert before == {"reply": FIRST_REPLY, "resource": b"first", "handlers": [str(SESSION_OPERATION)],
                      "startup_ready": False, "native_cdn": Resources(b"first").status(), "unimplemented": True}
    assert after == {"reply": SECOND_REPLY, "resource": b"second",
                     "handlers": [str(SESSION_OPERATION), str(QUIET_OPERATION)], "startup_ready": True,
                     "native_cdn": Resources(b"second").status(), "unimplemented": False}


def test_a_request_in_flight_finishes_on_the_generation_it_leased(tmp_path):
    entered, release, replies = threading.Event(), threading.Event(), []

    def held(envelope, store):
        entered.set()
        release.wait(WAIT)
        return FIRST_REPLY

    runtime = GenerationRuntime(generation("first", {SESSION_OPERATION: held}))
    with serving(tmp_path, runtime) as client:
        request = threading.Thread(target=lambda: replies.append(post(client, SESSION_OPERATION)))
        request.start()
        try:
            assert entered.wait(WAIT)
            runtime.swap(generation("second", {SESSION_OPERATION: replying(SECOND_REPLY)}), runtime.current.id)
            draining = runtime.status()["draining"]["leases"]
        finally:
            release.set()
            request.join(WAIT)
        assert decoded(client, replies[0], SESSION_OPERATION) == FIRST_REPLY
        assert decoded(client, post(client, SESSION_OPERATION), SESSION_OPERATION) == SECOND_REPLY
    assert draining == 1 and runtime.status()["draining"] is None


def test_health_reports_the_live_startup_diagnostics_of_the_current_generation(tmp_path):
    diagnostics = StartupDiagnostics(clock=lambda: STARTUP_TIME)

    def refused(envelope, store):
        raise ProtocolError(REFUSAL, "Refused", 503)

    runtime = GenerationRuntime(generation("first", {SESSION_OPERATION: diagnostics.wrap(SESSION_OPERATION, refused)},
                                           diagnostics=diagnostics))
    with serving(tmp_path, runtime) as client:
        clean = startup_loads(client.get("/health").json())
        assert post(client, SESSION_OPERATION).json()["error"]["code"] == REFUSAL
        health = startup_loads(client.get("/health").json())
        status = startup_loads(client.get("/local/status").json())
    assert clean == CLEAN_STARTUP
    assert health == status == diagnostics.status() != CLEAN_STARTUP
    assert health["loads"][str(SESSION_OPERATION)]["outcome"] == REFUSED
    assert runtime.current.readiness["bootstrap"]["startup_diagnostics"] == CLEAN_STARTUP


def test_operations_and_native_resources_report_client_activity_but_health_does_not(tmp_path):
    handlers = {SESSION_OPERATION: replying(FIRST_REPLY), QUIET_OPERATION: replying(EMPTY)}
    runtime = GenerationRuntime(generation("first", handlers, resources=Resources(b"first")))
    runtime.swap(generation("second", handlers, resources=Resources(b"second")), runtime.current.id)
    with serving(tmp_path, runtime) as client:
        assert client.get("/health").status_code == client.get("/local/status").status_code == 200
        quiet = runtime.idle_seconds()
        assert client.get(CDN_PATH).status_code == 200
        fetched = (runtime.idle_seconds() is not None, runtime.session_observed_since_switch())
        assert post(client, QUIET_OPERATION).status_code == 200
        playing = runtime.session_observed_since_switch()
        assert post(client, SESSION_OPERATION).status_code == 200
    assert SESSION_OPERATION in STARTUP_SEQUENCE and QUIET_OPERATION not in STARTUP_SEQUENCE
    assert (quiet, fetched, playing) == (None, (True, False), False)
    assert runtime.session_observed_since_switch() is True


def test_observed_operations_name_the_keys_handlers_are_looked_up_by():
    registry = ProtocolRegistry.load()
    profile = registry.by_registration[SESSION_OPERATION]
    assert SESSION_OPERATION in observed_operations(profile)
    assert observed_operations(profile) == profile.registration_keys
    unregistered = [profile for profile in registry.profiles if not profile.registration_keys]
    assert unregistered and all(observed_operations(profile) == (profile.name,) for profile in unregistered)


def test_a_fenced_runtime_answers_every_route_with_the_switching_code(tmp_path):
    runtime = GenerationRuntime(generation("first", {SESSION_OPERATION: replying(FIRST_REPLY)},
                                           resources=Resources(b"first")), admit_timeout=SHORT)
    with serving(tmp_path, runtime) as client:
        with runtime.fence():
            responses = [client.get("/health"), client.get("/local/status"), post(client, SESSION_OPERATION),
                         client.get(CDN_PATH)]
        logged = client.get("/local/requests").json()["requests"]
        recovered = decoded(client, post(client, SESSION_OPERATION), SESSION_OPERATION)
        route = client.app.state.registry.by_registration[SESSION_OPERATION].route
    assert [(response.status_code, response.json()["error"]["code"]) for response in responses] == [SWITCHING] * 4
    assert sorted((entry["route"], entry["status"], entry["error_code"]) for entry in logged) == sorted(
        [(CDN_ROUTE, *SWITCHING), (route, *SWITCHING)])
    assert recovered == FIRST_REPLY


def test_create_app_serves_either_a_runtime_or_fixed_sources(tmp_path):
    runtime = GenerationRuntime(generation("first", {}))
    data = tmp_path / "data"
    for fixed in ({"handlers": {}}, {"status_provider": readiness}, {"native_cdn": Resources(b"first")}):
        with pytest.raises(ValueError):
            create_app(data, runtime=runtime, **fixed)
    with pytest.raises(ValueError):
        create_app(data, runtime=runtime.current)
    assert not data.exists()


def test_fixed_handlers_readiness_and_resources_keep_serving_without_a_runtime(tmp_path):
    handlers = {SESSION_OPERATION: replying(FIRST_REPLY)}
    app = create_app(tmp_path, handlers=handlers, status_provider=readiness, native_cdn=Resources(b"fixed"))
    handlers[QUIET_OPERATION] = replying(EMPTY)
    with TestClient(app, base_url=BASE) as client:
        reply = decoded(client, post(client, SESSION_OPERATION), SESSION_OPERATION)
        unimplemented = post(client, QUIET_OPERATION).status_code
        health = client.get("/health").json()
        resource = client.get(CDN_PATH).content
    assert (reply, unimplemented, resource) == (FIRST_REPLY, 501, b"fixed")
    assert health["configured_handlers"] == [str(SESSION_OPERATION)]
    assert health["readiness"] == readiness() and health["native_cdn"] == Resources(b"fixed").status()
