from argparse import Namespace
from dataclasses import replace
import threading

import pytest

from aigis_ps.config_registry import REGISTRY
from aigis_ps.config_sets import resolve
from aigis_ps.errors import ProtocolError
from aigis_ps.generation import (CANDIDATE_TTL_SECONDS, GENERATION_ID_LENGTH, MAX_PROGRESS_ENTRIES, MAX_PROGRESS_TEXT,
                                 BuildState, Generation, GenerationIdentity, GenerationInputs, GenerationRuntime,
                                 code_digest)
from aigis_ps.startup_diagnostics import StartupDiagnostics


SHORT = 0.2
WAIT = 5.0
CLEAN_DIAGNOSTICS = {"attention_required": False, "loads": {}}


def identity(**changes):
    values = {"code_digest": "c" * 64, "config_set": "s" * 24, "content_versions": {"quests": "q1", "presents": "p1"},
              "client_fingerprint": "client", "master_snapshot": "master", "base_resource_version": "base",
              "effective_resource_version": "effective"}
    values.update(changes)
    return GenerationIdentity(**values)


def generation(config_set="first", *, diagnostics=None, readiness=None):
    return Generation(identity(config_set=config_set), object(), {31: lambda *_: b""},
                      readiness or {"bootstrap": {"startup_ready": True}}, diagnostics=diagnostics)


def runtime(initial=None, **options):
    for name in ("admit_timeout", "drain_timeout", "draining_limit"):
        options.setdefault(name, SHORT)
    return GenerationRuntime(initial or generation(), **options)


def synchronous(target):
    target()


def inputs(tmp_path):
    data = tmp_path / "data"
    args = Namespace(data_dir=data, **{spec.dest: None for spec in REGISTRY.values()})
    return GenerationInputs(resolve(args), data, tmp_path / "assets", tmp_path / "resources", "127.0.0.1", 18103,
                            StartupDiagnostics())


def refused(diagnostics, operation):
    with pytest.raises(ProtocolError):
        with diagnostics.load(operation):
            raise ProtocolError("broken", "Broken", 503)


def holding(live):
    entered, release = threading.Event(), threading.Event()

    def request():
        with live.lease():
            entered.set()
            release.wait(WAIT)

    thread = threading.Thread(target=request)
    thread.start()
    assert entered.wait(WAIT)
    return thread, release


def error_of(action):
    with pytest.raises(ProtocolError) as caught:
        action()
    return caught.value.code, caught.value.status


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_identity_is_deterministic_and_covers_every_field():
    base = identity()
    assert len(base.id) == GENERATION_ID_LENGTH
    assert base.id == identity(content_versions={"presents": "p1", "quests": "q1"}).id
    variants = [identity(code_digest="d" * 64), identity(config_set="other"), identity(client_fingerprint=None),
                identity(master_snapshot="m2"), identity(base_resource_version="b2"),
                identity(effective_resource_version="e2"), identity(content_versions={"quests": "q2", "presents": "p1"})]
    assert len({base.id, *(variant.id for variant in variants)}) == len(variants) + 1


def test_identity_differences_name_changed_fields_and_domains():
    changed = identity().differences(identity(master_snapshot="m2", content_versions={"quests": "q2", "vote": "v1"}))
    assert changed == ("master_snapshot", "content_versions.presents", "content_versions.quests",
                       "content_versions.vote")
    assert identity().differences(identity()) == ()


@pytest.mark.parametrize("change", [{"code_digest": ""}, {"config_set": None}, {"content_versions": {"": "x"}},
                                    {"content_versions": {"quests": 1}}, {"master_snapshot": ""},
                                    {"content_versions": [("quests", "q1")]}])
def test_identity_rejects_invalid_values(change):
    with pytest.raises(ValueError):
        identity(**change)


def test_code_digest_follows_python_sources_only(tmp_path):
    package = tmp_path / "package"
    (package / "nested").mkdir(parents=True)
    module, data = package / "nested" / "module.py", package / "data.json"
    module.write_bytes(b"value = 1\n")
    data.write_bytes(b"{}")
    first = code_digest(package)
    data.write_bytes(b"[]")
    assert code_digest(package) == first
    module.write_bytes(b"value = 2\n")
    assert code_digest(package) != first
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError):
        code_digest(tmp_path / "empty")
    assert code_digest() == code_digest() and len(code_digest()) == 64


def test_generation_freezes_handlers_and_readiness():
    handlers, readiness = {31: lambda *_: b""}, {"bootstrap": {"startup_ready": True}}
    built = Generation(identity(), object(), handlers, readiness)
    handlers[32] = lambda *_: b""
    readiness["bootstrap"]["startup_ready"] = False
    assert set(built.handlers) == {31} and built.readiness["bootstrap"]["startup_ready"] is True
    assert built.id == identity().id
    with pytest.raises(TypeError):
        built.handlers[33] = lambda *_: b""


@pytest.mark.parametrize("arguments", [
    {"handlers": {31: "not callable"}}, {"readiness": []}, {"identity": {"code_digest": "c"}},
    {"diagnostics": object()}, {"gate_issues": (1,)}])
def test_generation_rejects_invalid_parts(arguments):
    values = {"identity": identity(), "bundle": object(), "handlers": {}, "readiness": {}}
    values.update(arguments)
    with pytest.raises(ValueError):
        Generation(**values)


def test_live_status_reads_startup_diagnostics_at_call_time():
    diagnostics = StartupDiagnostics(clock=lambda: "2026-11-02T00:00:00Z")
    built = generation(diagnostics=diagnostics,
                       readiness={"bootstrap": {"startup_ready": True, "startup_diagnostics": {"stale": True}}})
    assert built.live_status()["bootstrap"]["startup_diagnostics"] == CLEAN_DIAGNOSTICS
    refused(diagnostics, 39)
    assert built.live_status()["bootstrap"]["startup_diagnostics"]["loads"]["39"]["outcome"] == "refused"
    assert built.readiness["bootstrap"]["startup_diagnostics"] == {"stale": True}


def test_leases_count_concurrent_requests():
    live = runtime()
    inside, release = threading.Barrier(17), threading.Event()

    def request():
        with live.lease():
            inside.wait(WAIT)
            release.wait(WAIT)

    threads = [threading.Thread(target=request) for _ in range(16)]
    for thread in threads:
        thread.start()
    inside.wait(WAIT)
    assert live.status()["current"]["leases"] == 16
    release.set()
    for thread in threads:
        thread.join(WAIT)
    assert live.status()["current"]["leases"] == 0


def test_requests_leased_before_a_swap_finish_on_the_old_generation():
    first, second = generation("first"), generation("second")
    live = runtime(first)
    thread, release = holding(live)
    assert live.swap(second, first.id) is first
    with live.lease() as served:
        assert served is second
    assert live.status()["draining"]["id"] == first.id and live.status()["draining"]["leases"] == 1
    release.set()
    thread.join(WAIT)
    assert live.status()["draining"] is None and live.current is second


def test_swap_requires_the_expected_running_generation():
    live = runtime(generation("first"))
    assert error_of(lambda: live.swap(generation("second"), "0" * GENERATION_ID_LENGTH)) == ("generation_changed", 409)


def test_swap_waits_for_the_draining_generation_within_the_limit():
    first, second, third = generation("first"), generation("second"), generation("third")
    live = runtime(first, draining_limit=WAIT)
    thread, release = holding(live)
    live.swap(second, first.id)
    threading.Timer(SHORT, release.set).start()
    assert live.swap(third, second.id) is second
    thread.join(WAIT)
    assert live.current is third and live.status()["draining"] is None


def test_swap_refuses_while_the_draining_generation_outlives_the_limit():
    first, second, third = generation("first"), generation("second"), generation("third")
    live = runtime(first)
    thread, release = holding(live)
    live.swap(second, first.id)
    assert error_of(lambda: live.swap(third, second.id)) == ("generation_draining", 409)
    assert live.current is second
    release.set()
    thread.join(WAIT)


def test_fence_holds_new_leases_until_it_opens():
    live = runtime(admit_timeout=WAIT)
    admitted = threading.Event()

    def request():
        with live.lease():
            admitted.set()

    with live.fence() as running:
        assert running is live.current and live.status()["fenced"] is True
        thread = threading.Thread(target=request)
        thread.start()
        assert not admitted.wait(SHORT)
    assert admitted.wait(WAIT)
    thread.join(WAIT)


def test_fence_refuses_new_leases_after_the_admit_timeout():
    live = runtime()

    def lease():
        with live.lease():
            pytest.fail("A fenced runtime must not admit requests")

    with live.fence():
        assert error_of(lease) == ("generation_switching", 503)
    with live.lease() as served:
        assert served is live.current


def test_fence_waits_for_requests_that_finish_in_time():
    live = runtime(drain_timeout=WAIT)
    thread, release = holding(live)
    threading.Timer(SHORT, release.set).start()
    with live.fence():
        assert live.status()["current"]["leases"] == 0
    thread.join(WAIT)


def test_fence_abandons_the_switch_when_requests_do_not_drain():
    live = runtime()
    thread, release = holding(live)

    def fence():
        with live.fence():
            pytest.fail("The fence must not open while requests are in flight")

    assert error_of(fence) == ("switch_drain_timeout", 409)
    assert live.status()["fenced"] is False
    with live.lease():
        assert live.status()["current"]["leases"] == 2
    release.set()
    thread.join(WAIT)


def test_only_one_fence_at_a_time():
    live = runtime()

    def nested():
        with live.fence():
            pass

    with live.fence():
        assert error_of(nested) == ("generation_switch_busy", 409)


def test_a_swap_inside_the_fence_leaves_nothing_draining():
    first, second = generation("first"), generation("second")
    live = runtime(first)
    with live.fence() as running:
        live.swap(second, running.id)
    assert live.current is second and live.status()["draining"] is None


def test_build_slot_runs_one_build_and_keeps_its_candidate(tmp_path):
    live = runtime()
    started, release = threading.Event(), threading.Event()
    built = generation("candidate")

    def builder(given, progress):
        progress("loading master data")
        started.set()
        release.wait(WAIT)
        return built

    assert live.start_build(inputs(tmp_path), builder)["state"] == BuildState.RUNNING
    assert started.wait(WAIT)
    assert error_of(lambda: live.start_build(inputs(tmp_path), builder)) == ("generation_build_busy", 409)
    release.set()
    finished = live.wait_for_build(WAIT)
    assert finished["state"] == BuildState.SUCCEEDED and finished["candidate_id"] == built.id
    assert finished["progress"] == ["loading master data"]
    assert live.candidate() is built
    assert live.take_candidate(built.id) is built and live.candidate() is None
    assert error_of(lambda: live.take_candidate(built.id)) == ("generation_candidate_missing", 409)


def test_a_failed_build_reports_its_error_and_frees_the_slot(tmp_path):
    live = runtime(start=synchronous)

    def failing(given, progress):
        raise ProtocolError("masterdata_unavailable", "No master data", 503)

    status = live.start_build(inputs(tmp_path), failing)
    assert status["state"] == BuildState.FAILED
    assert status["error"] == {"code": "masterdata_unavailable", "message": "No master data"}
    assert live.candidate() is None
    status = live.start_build(inputs(tmp_path), lambda given, progress: object())
    assert status["state"] == BuildState.FAILED and status["error"]["code"] == "internal_error"
    status = live.start_build(inputs(tmp_path), lambda given, progress: generation("next"))
    assert status["state"] == BuildState.SUCCEEDED


def test_candidate_expires_after_its_time_to_live(tmp_path):
    clock = FakeClock()
    live = runtime(clock=clock, start=synchronous)
    built = generation("candidate")
    live.start_build(inputs(tmp_path), lambda given, progress: built)
    clock.now += CANDIDATE_TTL_SECONDS
    assert live.candidate() is built
    clock.now += 1
    assert live.candidate() is None and live.status()["candidate"] is None


def test_a_restored_candidate_keeps_its_identity_and_its_original_time_to_live(tmp_path):
    clock = FakeClock()
    live = runtime(clock=clock, start=synchronous)
    built = generation("candidate")
    live.start_build(inputs(tmp_path), lambda given, progress: built)
    clock.now += 10
    taken = live.take_candidate(built.id)
    assert live.candidate() is None
    live.restore_candidate(taken)
    assert live.candidate() is built and live.take_candidate(built.id) is built
    live.restore_candidate(built)
    clock.now += CANDIDATE_TTL_SECONDS - 10
    assert live.candidate() is built
    clock.now += 1
    assert live.candidate() is None
    live.restore_candidate(built)
    assert live.candidate() is None


def test_restoring_an_untaken_or_replaced_candidate_changes_nothing(tmp_path):
    live = runtime(start=synchronous)
    built, newer = generation("candidate"), generation("newer")
    live.start_build(inputs(tmp_path), lambda given, progress: built)
    with pytest.raises(ValueError):
        live.restore_candidate("candidate")
    live.restore_candidate(generation("stranger"))
    assert live.candidate() is built
    taken = live.take_candidate(built.id)
    live.start_build(inputs(tmp_path), lambda given, progress: newer)
    live.restore_candidate(taken)
    assert live.candidate() is newer
    second = live.take_candidate(newer.id)
    live.restore_candidate(taken)
    assert live.candidate() is None
    live.restore_candidate(second)
    assert live.candidate() is newer and live.take_candidate(newer.id) is newer


def test_build_progress_is_bounded(tmp_path):
    live = runtime(start=synchronous)

    def chatty(given, progress):
        for index in range(MAX_PROGRESS_ENTRIES + 5):
            progress(f"step {index} " + "x" * MAX_PROGRESS_TEXT)
        return generation("candidate")

    progress = live.start_build(inputs(tmp_path), chatty)["progress"]
    assert len(progress) == MAX_PROGRESS_ENTRIES and progress[0].startswith("step 5 ")
    assert all(len(entry) == MAX_PROGRESS_TEXT for entry in progress)


def test_startup_diagnostics_survive_generation_switches(tmp_path):
    given = inputs(tmp_path)
    first = generation("first", diagnostics=given.diagnostics)
    live = runtime(first, start=synchronous)
    refused(given.diagnostics, 39)
    status = live.start_build(given, lambda chosen, progress: generation("second", diagnostics=chosen.diagnostics))
    live.swap(live.take_candidate(status["candidate_id"]), first.id)
    assert live.current.live_status()["bootstrap"]["startup_diagnostics"]["loads"]["39"]["outcome"] == "refused"


def test_request_observation_tracks_idleness_and_fresh_sessions():
    clock = FakeClock()
    first, second = generation("first"), generation("second")
    live = runtime(first, clock=clock)
    assert live.idle_seconds() is None and live.session_observed_since_switch() is None
    live.observe_request(41)
    clock.now += 30
    assert live.idle_seconds() == 30
    live.swap(second, first.id)
    assert live.session_observed_since_switch() is False
    live.observe_request("41")
    assert live.session_observed_since_switch() is False
    live.observe_request(114)
    assert live.session_observed_since_switch() is True and live.idle_seconds() == 0
    live.swap(first, second.id)
    status = live.status()
    assert status["session_observed_since_switch"] is False and status["switched_seconds_ago"] == 0


def test_inputs_validate_and_describe(tmp_path):
    given = inputs(tmp_path)
    assert given.config_digest == given.config.digest
    described = given.describe()
    assert described["port"] == 18103 and described["config"]["digest"] == given.config_digest
    for change in ({"data_dir": "data"}, {"port": 0}, {"port": True}, {"host": ""}, {"diagnostics": None},
                   {"config": {}}):
        with pytest.raises(ValueError):
            replace(given, **change)


@pytest.mark.parametrize("limits", [{"admit_timeout": 0}, {"drain_timeout": -1}, {"draining_limit": True},
                                    {"candidate_ttl": "600"}])
def test_runtime_rejects_invalid_limits(limits):
    with pytest.raises(ValueError):
        GenerationRuntime(generation(), **limits)
