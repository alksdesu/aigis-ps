from types import SimpleNamespace

import pytest

from aigis_ps.admin_command import AdminError
from aigis_ps.admin_registry import RouteRegistry
from aigis_ps.admin_routes_entrust import VALIDATIONS_PATH, EntrustValidationRunner, register
from aigis_ps.generation import BuildState


RESULT = {0: {"products": 2, "excluded": {"entrust_reward_unsupported": 1}},
          2: {"products": 1, "excluded": {}}}


class FakeCatalog:
    def __init__(self, result=RESULT, *, failure=None):
        self.content_version = "catalog-v1"
        self.result, self.failure = result, failure
        self.calls = 0

    def validate(self, quest_types=None, *, progress=None):
        self.calls += 1
        for quest_type, entry in sorted(self.result.items()):
            total = entry["products"] + sum(entry["excluded"].values())
            progress({"quest_type": quest_type, "done": 0, "total": total})
            progress({"quest_type": quest_type, "done": total, "total": total})
        if self.failure is not None:
            raise self.failure
        return {quest_type: dict(entry) for quest_type, entry in self.result.items()}


def generation_for(catalog, identifier="gen-1"):
    return SimpleNamespace(id=identifier, bundle=SimpleNamespace(entrust=SimpleNamespace(catalog=catalog)))


def services_for(generation):
    return SimpleNamespace(runtime=SimpleNamespace(current=generation))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class HeldStart:
    def __init__(self):
        self.targets = []

    def __call__(self, target):
        self.targets.append(target)

    def run_last(self):
        self.targets[-1]()


def runner_for(catalog, *, generation=None, start=None):
    generation = generation or generation_for(catalog)
    services = services_for(generation)
    clock = Clock()
    runner = EntrustValidationRunner(services, clock=clock,
                                     start=start if start is not None else (lambda target: target()))
    return runner, services, clock


def test_a_job_runs_to_completion_with_progress_and_the_synchronous_result():
    catalog = FakeCatalog()
    runner, _, clock = runner_for(catalog)
    clock.now = 3.0
    started = runner.start()
    job = started["validation"]
    assert started["generation_id"] == "gen-1"
    assert job["state"] == BuildState.SUCCEEDED and job["stale"] is False
    assert job["generation_id"] == "gen-1" and job["content_version"] == "catalog-v1"
    assert job["progress"] == {"0": {"done": 3, "total": 3}, "2": {"done": 1, "total": 1}}
    assert job["error"] is None and job["elapsed_seconds"] == 0.0
    synchronous = FakeCatalog().validate(progress=lambda entry: None)
    assert job["result"] == {str(quest_type): entry for quest_type, entry in synchronous.items()}
    assert runner.status()["validation"] == job


def test_a_failing_validation_reports_its_error():
    runner, _, _ = runner_for(FakeCatalog(failure=ValueError("broken sources")))
    job = runner.start()["validation"]
    assert job["state"] == BuildState.FAILED and job["result"] is None
    assert job["error"] == {"code": "internal_error", "message": "ValueError"}


def test_only_one_job_runs_at_a_time_and_a_finished_one_can_be_replaced():
    held = HeldStart()
    catalog = FakeCatalog()
    runner, _, _ = runner_for(catalog, start=held)
    first = runner.start()["validation"]
    assert first["state"] == BuildState.RUNNING and first["result"] is None
    with pytest.raises(AdminError) as caught:
        runner.start()
    assert (caught.value.code, caught.value.status) == ("admin_entrust_validation_busy", 409)
    held.run_last()
    assert runner.status()["validation"]["state"] == BuildState.SUCCEEDED
    second = runner.start()["validation"]
    assert second["job_id"] != first["job_id"] and second["state"] == BuildState.RUNNING
    held.run_last()
    assert runner.status()["validation"]["state"] == BuildState.SUCCEEDED and catalog.calls == 2


def test_a_generation_switch_marks_the_job_stale():
    held = HeldStart()
    generation = generation_for(FakeCatalog())
    services = services_for(generation)
    runner = EntrustValidationRunner(services, clock=Clock(), start=held)
    runner.start()
    services.runtime.current = generation_for(FakeCatalog(), identifier="gen-2")
    running = runner.status()
    assert running["generation_id"] == "gen-2"
    assert running["validation"]["state"] == BuildState.RUNNING and running["validation"]["stale"] is True
    held.run_last()
    finished = runner.status()["validation"]
    assert finished["state"] == BuildState.SUCCEEDED and finished["stale"] is True


def test_progress_reported_after_the_job_finished_is_ignored():
    held = HeldStart()

    class LateCatalog(FakeCatalog):
        def validate(self, quest_types=None, *, progress=None):
            self.report = progress
            return super().validate(quest_types, progress=progress)

    catalog = LateCatalog()
    runner, _, _ = runner_for(catalog, start=held)
    runner.start()
    held.run_last()
    assert runner.status()["validation"]["state"] == BuildState.SUCCEEDED
    catalog.report({"quest_type": 99, "done": 1, "total": 1})
    assert "99" not in runner.status()["validation"]["progress"]


def test_a_generation_without_the_entrust_service_is_refused():
    runner, _, _ = runner_for(FakeCatalog(), generation=SimpleNamespace(id="gen-1", bundle=SimpleNamespace()))
    with pytest.raises(AdminError) as caught:
        runner.start()
    assert (caught.value.code, caught.value.status) == ("admin_entrust_unavailable", 503)
    assert runner.status() == {"generation_id": "gen-1", "validation": None}


def test_the_route_serves_a_read_and_a_start_under_one_path():
    registry = RouteRegistry()
    register(registry)
    [route] = list(registry)
    assert route.name == "entrust"
    services = services_for(generation_for(FakeCatalog()))
    router = route.build(services)
    methods = {(item.path, method) for item in router.routes for method in item.methods}
    assert methods == {(VALIDATIONS_PATH, "GET"), (VALIDATIONS_PATH, "POST")}
