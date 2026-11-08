from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import sys
import textwrap
from types import SimpleNamespace
import uuid

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from aigis_ps.admin_command import (COMMAND_UNAVAILABLE, MAX_BATCH, PLAN_CAPACITY, PLAN_TTL_SECONDS, AdminCommand,
                                    AdminError, Batch, BatchStep, CommandContext, CommandParams, CommandResult,
                                    Confirmation, NoopPreflight, NoopRestartCheck, Plan, PlanStore, PreflightReport,
                                    check_confirmations, compute_plan_digest, counted, error_payload, field_errors,
                                    first_unavailable, missing_service, require_distinct, require_op_id,
                                    required_service, requires, requires_all)
from aigis_ps.admin_registry import (AdminRoute, AdminView, CommandRegistry, RouteRegistry, ViewRegistry, discover)
from aigis_ps.errors import ProtocolError
from aigis_ps.storage import ADMIN_ACTOR


NOW = datetime(2026, 11, 2, 1, 30, tzinfo=timezone.utc)
DIGEST = "e" * 64
LARGE_DEBIT = 5


class AmountParams(CommandParams):
    amount: int = Field(ge=1)


class EmptyParams(CommandParams):
    pass


class DistinctParams(CommandParams):
    ids: list[int]

    @model_validator(mode="after")
    def _distinct(self):
        require_distinct(self.ids, "IDs")
        return self


class LooseParams(BaseModel):
    amount: int


class LaxParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: int


class Inner(BaseModel):
    model_config = ConfigDict(strict=True)
    value: int


class NestedParams(CommandParams):
    inner: list[Inner]


def credit(state, params, context):
    updated = deepcopy(state)
    updated["wallet"] += params.amount
    return CommandResult(updated, {"credited": params.amount}, ("credited",))


def debit(state, params, context):
    if state["wallet"] < params.amount:
        raise ProtocolError("wallet_insufficient", "Not enough currency", 409)
    updated = deepcopy(state)
    updated["wallet"] -= params.amount
    return CommandResult(updated, {"debited": params.amount})


def explode(state, params, context):
    raise ProtocolError("fixture_rejected", "Fixture command rejected the state", 409)


def in_place(state, params, context):
    state["wallet"] += params.amount
    return CommandResult(state)


def command(operation="wallet.credit", apply=credit, **options):
    values = {"domain": "wallet", "params_model": AmountParams, "apply": apply, "touched": frozenset({"wallet"})}
    values.update(options)
    return AdminCommand(operation, **values)


def debit_phrase(params):
    return f"DEBIT {params.amount}"


def large_debit(params):
    return params.amount >= LARGE_DEBIT


CREDIT = command()
DEBIT = command("wallet.debit", debit, destructive=True, confirm_phrase=debit_phrase)
SCALED_DEBIT = command("wallet.scaled_debit", debit, destructive=large_debit, confirm_phrase=debit_phrase)
EXPLODE = command("wallet.explode", explode)
IN_PLACE = command("wallet.in_place", in_place)


def batch(*steps):
    return Batch(tuple(BatchStep(item, item.params_model(amount=amount)) for item, amount in steps))


def context(**changes):
    values = {"now": NOW, "acquisition_day": 739921, "revision": 3, "generation_id": "gen-a",
              "content_versions": {"wallet": "v1", "presents": "p1"}}
    values.update(changes)
    return CommandContext(**values)


def plan(batch_value=None, **changes):
    values = {"batch": batch_value or batch((CREDIT, 1)), "context": context(), "plan_digest": DIGEST, "diff_sha256": DIGEST,
              "state_sha256_before": DIGEST, "state_sha256_after": DIGEST, "diff": {"entries": []}, "steps": (),
              "confirmations": (), "preflight": {"ok": True}, "restart_check": {"ok": True}, "audit": {"ok": True}}
    values.update(changes)
    return Plan(**values)


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


def test_batch_failure_at_any_step_leaves_the_source_state_unchanged():
    state = {"wallet": 0, "history": [1]}
    original = deepcopy(state)
    with pytest.raises(AdminError) as caught:
        batch((IN_PLACE, 5), (CREDIT, 5), (EXPLODE, 1), (CREDIT, 5)).apply(state, context())
    assert caught.value.code == "fixture_rejected" and caught.value.status == 409
    assert caught.value.details == {"step": 2, "operation": "wallet.explode"}
    assert state == original


def test_batch_applies_every_step_to_one_candidate_in_order():
    state = {"wallet": 1}
    outcome = batch((CREDIT, 2), (DEBIT, 3), (IN_PLACE, 4)).apply(state, context())
    assert outcome.state == {"wallet": 4} and state == {"wallet": 1}
    assert [step.operation for step in outcome.steps] == ["wallet.credit", "wallet.debit", "wallet.in_place"]
    assert outcome.steps[0].describe(0) == {"step": 0, "operation": "wallet.credit", "effects": {"credited": 2}, "warnings": ["credited"]}
    with pytest.raises(AdminError) as caught:
        batch((DEBIT, 9)).apply(state, context())
    assert caught.value.code == "wallet_insufficient" and caught.value.details["step"] == 0


def test_batch_size_is_bounded():
    with pytest.raises(AdminError) as caught:
        Batch(())
    assert caught.value.code == "admin_batch_invalid" and caught.value.status == 422
    with pytest.raises(AdminError):
        batch(*[(CREDIT, 1)] * (MAX_BATCH + 1))
    assert len(batch(*[(CREDIT, 1)] * MAX_BATCH).steps) == MAX_BATCH


def test_commands_must_return_json_results():
    broken = command("wallet.broken", lambda state, params, ctx: state)
    unencodable = command("wallet.unencodable", lambda state, params, ctx: CommandResult(state, {"bad": object()}))
    for item in (broken, unencodable):
        with pytest.raises(AdminError) as caught:
            batch((item, 1)).apply({"wallet": 0}, context())
        assert caught.value.code == "admin_command_result_invalid" and caught.value.status == 500
    with pytest.raises(ValueError):
        CommandResult(["not", "a", "state"])


def test_errors_raised_by_admin_kernels_keep_their_details():
    def conflicting(state, params, ctx):
        raise AdminError("kernel_conflict", "Kernel conflict", current_revision=9, details={"unit": 4})

    with pytest.raises(AdminError) as caught:
        batch((CREDIT, 1), (command("wallet.conflict", conflicting), 1)).apply({"wallet": 0}, context())
    assert caught.value.payload() == {"code": "kernel_conflict", "message": "Kernel conflict", "current_revision": 9,
                                      "details": {"unit": 4, "step": 1, "operation": "wallet.conflict"}}


def test_plan_digest_is_deterministic_and_covers_its_inputs():
    first = batch((CREDIT, 2), (DEBIT, 1))
    same_batch = Batch(tuple(BatchStep(step.command, step.command.parse({"amount": step.params.amount})) for step in first.steps))
    digest = compute_plan_digest(first, context(), DIGEST)
    assert digest == compute_plan_digest(same_batch, context(content_versions={"presents": "p1", "wallet": "v1"}), DIGEST)
    assert len({digest,
                compute_plan_digest(batch((CREDIT, 3), (DEBIT, 1)), context(), DIGEST),
                compute_plan_digest(first, context(revision=4), DIGEST),
                compute_plan_digest(first, context(generation_id="gen-b"), DIGEST),
                compute_plan_digest(first, context(now=NOW + timedelta(seconds=1)), DIGEST),
                compute_plan_digest(first, context(content_versions={"wallet": "v2"}), DIGEST),
                compute_plan_digest(first, context(), "f" * 64)}) == 7
    assert digest == compute_plan_digest(first, context(op_id=str(uuid.uuid4())), DIGEST)


def test_plan_store_expires_plans_after_their_lifetime():
    clock = Clock()
    plans = PlanStore(clock=clock)
    stored = plans.put(plan())
    assert stored.plan_id and plans.get(stored.plan_id) is stored
    clock.value += PLAN_TTL_SECONDS - 1
    assert plans.get(stored.plan_id) is stored
    clock.value += 1
    with pytest.raises(AdminError) as caught:
        plans.get(stored.plan_id)
    assert caught.value.code == "plan_expired" and caught.value.status == 409
    assert len(plans) == 0


def test_plan_store_keeps_only_the_newest_plans():
    plans = PlanStore(clock=Clock())
    stored = [plans.put(plan()) for _ in range(PLAN_CAPACITY + 1)]
    assert len(plans) == PLAN_CAPACITY
    with pytest.raises(AdminError):
        plans.get(stored[0].plan_id)
    assert plans.get(stored[-1].plan_id) is stored[-1]
    plans.discard(stored[-1].plan_id)
    with pytest.raises(AdminError):
        plans.get(stored[-1].plan_id)
    with pytest.raises(AdminError):
        plans.get(None)


def test_plan_store_refuses_plans_that_cannot_commit():
    with pytest.raises(ValueError):
        PlanStore().put(plan(preflight={"ok": False, "error": {"code": "admin_preflight_failed"}}))
    with pytest.raises(ValueError):
        PlanStore(capacity=0)


@pytest.mark.parametrize("check", ["preflight", "restart_check", "audit"])
def test_plans_commit_only_when_every_check_passed(check):
    assert plan().committable is True and plan().describe()[check] == {"ok": True}
    refused = plan(**{check: {"ok": False}})
    assert refused.committable is False and refused.describe()[check] == {"ok": False}
    assert refused.describe()["committable"] is False
    with pytest.raises(ValueError):
        PlanStore().put(refused)


def test_plans_know_whether_they_change_the_state():
    assert plan().changed is False
    assert plan(state_sha256_after="f" * 64).changed is True


def test_destructive_commands_require_their_exact_phrases():
    required = batch((CREDIT, 2), (DEBIT, 4)).confirmations()
    assert required == (Confirmation(1, "wallet.debit", "DEBIT 4"),)
    for provided in ((), ["DEBIT 5"], ["DEBIT 4", "extra"], "DEBIT 4", None):
        with pytest.raises(AdminError) as caught:
            check_confirmations(required, provided)
        assert caught.value.code == "admin_confirmation_required" and caught.value.status == 422
        assert caught.value.details == {"required": [{"step": 1, "operation": "wallet.debit", "phrase": "DEBIT 4"}]}
    check_confirmations(required, ["DEBIT 4"])
    check_confirmations(batch((CREDIT, 2)).confirmations(), ())


def test_confirmation_phrases_must_be_usable_text():
    blank = command("wallet.blank", debit, destructive=True, confirm_phrase=lambda params: " ")
    with pytest.raises(AdminError) as caught:
        batch((blank, 1)).confirmations()
    assert caught.value.code == "admin_command_invalid" and caught.value.status == 500


def test_destructive_commands_can_decide_by_their_parameters():
    small, large = batch((SCALED_DEBIT, LARGE_DEBIT - 1)), batch((CREDIT, 1), (SCALED_DEBIT, LARGE_DEBIT))
    assert [step.destructive for step in small.steps] == [False] and small.confirmations() == ()
    assert [step.destructive for step in large.steps] == [False, True]
    assert large.confirmations() == (Confirmation(1, "wallet.scaled_debit", f"DEBIT {LARGE_DEBIT}"),)
    check_confirmations(small.confirmations(), ())
    with pytest.raises(AdminError) as caught:
        check_confirmations(large.confirmations(), ())
    assert caught.value.details == {"required": [{"step": 1, "operation": "wallet.scaled_debit",
                                                  "phrase": f"DEBIT {LARGE_DEBIT}"}]}
    assert SCALED_DEBIT.is_destructive(AmountParams(amount=LARGE_DEBIT)) is True
    assert DEBIT.is_destructive(AmountParams(amount=1)) is True and CREDIT.is_destructive(AmountParams(amount=9)) is False


def test_commands_describe_how_they_decide_destructiveness():
    described = {item.operation: item.describe() for item in (CREDIT, DEBIT, SCALED_DEBIT)}
    assert {operation: (entry["destructive"], entry["destructive_by_params"]) for operation, entry in described.items()} == {
        "wallet.credit": (False, False), "wallet.debit": (True, False), "wallet.scaled_debit": (True, True)}


def test_destructive_predicates_must_answer_with_a_bool():
    vague = command("wallet.vague", debit, destructive=lambda params: params.amount, confirm_phrase=debit_phrase)
    with pytest.raises(AdminError) as caught:
        batch((vague, 1)).confirmations()
    assert caught.value.code == "admin_command_invalid" and caught.value.status == 500


@pytest.mark.parametrize("options", [
    {"operation": "Wallet.Credit"},
    {"operation": "credit"},
    {"domain": "Wallet"},
    {"params_model": LooseParams},
    {"params_model": LaxParams},
    {"params_model": NestedParams},
    {"params_model": dict},
    {"touched": frozenset()},
    {"touched": "wallet"},
    {"writes": frozenset({"config"})},
    {"writes": "config.A2"},
    {"destructive": True},
    {"destructive": 1},
    {"destructive": "always"},
    {"destructive": large_debit},
    {"confirm_phrase": lambda params: "PHRASE"},
    {"preflight_none": {"letstd": "Server-only ledger"}},
    {"touched": frozenset({"wallet", "letstd"}), "preflight_none": {"letstd": " "}},
    {"apply": None},
    {"unavailable": "missing"},
    {"group": "wallet.credit"},
    {"group": "credit"},
    {"group": "wal"},
    {"group": ""},
    {"group": 1},
])
def test_command_definitions_are_checked(options):
    values = {"operation": "wallet.credit"}
    values.update(options)
    with pytest.raises(ValueError):
        command(**values)


def test_parameters_are_strict_and_reject_unknown_fields():
    assert CREDIT.parse({"amount": 2}) == AmountParams(amount=2)
    for raw, location in (({"amount": "2"}, ["amount"]), ({"amount": True}, ["amount"]), ({"amount": 0}, ["amount"]),
                          ({"amount": 2, "extra": 1}, ["extra"]), ([], [])):
        with pytest.raises(AdminError) as caught:
            CREDIT.parse(raw)
        assert caught.value.code == "admin_params_invalid" and caught.value.status == 422
        assert [item["loc"] for item in caught.value.fields] == [location]
    with pytest.raises(AdminError):
        CREDIT.parse({"amount": float("nan")})


def test_field_errors_keep_only_the_location_type_and_message():
    with pytest.raises(ValidationError) as caught:
        AmountParams.model_validate({"amount": "2", "extra": 1})
    errors = field_errors(caught.value)
    assert [(item["loc"], item["type"]) for item in errors] == [(["amount"], "int_type"), (["extra"], "extra_forbidden")]
    assert all(set(item) == {"loc", "type", "msg"} and item["msg"] for item in errors)


def test_parameter_helpers_count_nouns_and_refuse_repeats():
    assert (counted(0, "FLAG"), counted(1, "FLAG"), counted(2, "FLAG")) == ("0 FLAGS", "1 FLAG", "2 FLAGS")
    require_distinct((1, 2, 3), "IDs")
    with pytest.raises(ValueError, match="IDs must be distinct"):
        require_distinct((1, 2, 1), "IDs")
    distinct = command("wallet.pick", params_model=DistinctParams)
    assert distinct.parse({"ids": [1, 2]}) == DistinctParams(ids=[1, 2])
    with pytest.raises(AdminError) as caught:
        distinct.parse({"ids": [1, 1]})
    assert caught.value.code == "admin_params_invalid" and caught.value.fields[0]["type"] == "value_error"


def test_command_descriptions_export_json_schema():
    description = command(writes=frozenset({"config.A2"}), touched=frozenset({"wallet", "letstd"}),
                          preflight_none={"letstd": "Server-only ledger"}).describe()
    assert description["operation"] == "wallet.credit" and description["destructive"] is False
    assert description["touched"] == ["letstd", "wallet"] and description["writes"] == ["config.A2"]
    assert description["preflight_none"] == {"letstd": "Server-only ledger"} and description["group"] is None
    schema = description["params_schema"]
    assert schema["additionalProperties"] is False and schema["properties"]["amount"]["minimum"] == 1


def test_command_availability_reports_reasons():
    gated = command(unavailable=lambda services: None if services else "Wallet service is unavailable")
    assert gated.availability(object()) is None and gated.availability(None) == "Wallet service is unavailable"
    with pytest.raises(AdminError) as caught:
        batch((CREDIT, 1), (gated, 1)).require_available(None)
    assert caught.value.code == COMMAND_UNAVAILABLE == "admin_command_unavailable" and caught.value.status == 503
    assert caught.value.details == {"step": 1, "operation": "wallet.credit"}


def test_availability_helpers_report_the_first_missing_service():
    services = SimpleNamespace(wallet=object(), ledger=None)
    assert requires("wallet")(services) is None
    assert requires("ledger")(services) == missing_service("ledger") == "This generation has no ledger service"
    assert requires("ledger", "Ledger rules are not configured")(services) == "Ledger rules are not configured"
    assert requires_all("wallet")(services) is None and first_unavailable()(services) is None
    assert requires_all("wallet", "ledger", "absent")(services) == missing_service("ledger")
    assert first_unavailable(requires("wallet"), lambda services: "Wallet is closed", requires("ledger"))(services) == (
        "Wallet is closed")
    with pytest.raises(AdminError) as caught:
        batch((command(unavailable=requires_all("wallet", "ledger")), 1)).require_available(services)
    assert (caught.value.code, caught.value.status, str(caught.value)) == (COMMAND_UNAVAILABLE, 503,
                                                                          missing_service("ledger"))


def test_required_services_refuse_with_the_unavailable_code():
    services = SimpleNamespace(wallet="ready", ledger=None)
    assert required_service(context(services=services), "wallet") == "ready"
    for reason, message in ((None, missing_service("ledger")), ("No ledger rules", "No ledger rules")):
        with pytest.raises(AdminError) as caught:
            required_service(context(services=services), "ledger", reason)
        assert (caught.value.code, caught.value.status, str(caught.value)) == (COMMAND_UNAVAILABLE, 503, message)


def test_context_is_frozen_and_timezone_aware():
    frozen = context(content_versions={"wallet": "v1", "alpha": None})
    assert list(frozen.content_versions) == ["alpha", "wallet"] and frozen.actor == ADMIN_ACTOR
    with pytest.raises(TypeError):
        frozen.content_versions["wallet"] = "v2"
    assert frozen.frozen() == {"now": "2026-11-02T01:30:00+00:00", "now_jst": "2026-11-02T10:30:00+09:00",
                               "acquisition_day": 739921, "revision": 3, "generation_id": "gen-a",
                               "content_versions": {"alpha": None, "wallet": "v1"}, "actor": ADMIN_ACTOR, "draws": []}
    assert frozen.jst_date.isoformat() == "2026-11-02"
    assert replace(frozen, services=object()) == frozen
    for changes in ({"now": NOW.replace(tzinfo=None)}, {"acquisition_day": 0}, {"revision": -1}, {"generation_id": ""},
                    {"content_versions": {"wallet": 1}}, {"op_id": "not-a-uuid"}, {"draws": (object(),)}):
        with pytest.raises((ValueError, TypeError)):
            context(**changes)


def test_operation_ids_are_canonical_uuid4():
    value = str(uuid.uuid4())
    assert require_op_id(value) == value
    for invalid in (value.upper(), str(uuid.uuid1()), value.replace("-", ""), f"{{{value}}}", 7, None):
        with pytest.raises(AdminError) as caught:
            require_op_id(invalid)
        assert caught.value.code == "admin_op_id_invalid" and caught.value.status == 422


def test_noop_checks_are_marked_for_the_server_gate():
    assert NoopPreflight.is_noop is True and NoopRestartCheck.is_noop is True
    assert NoopPreflight().check({}, {}, frozenset({"wallet"}), None) == PreflightReport()
    assert NoopRestartCheck().check({}, None) is None


def test_preflight_reports_are_validated():
    report = PreflightReport({"units": [41], "config": (36,), "quest_records": ("quest_record",)})
    assert report.to_dict() == {"readback": {"config": [36], "quest_records": ["quest_record"], "units": [41]}}
    for readback in ({"Units": [41]}, {"units": [True]}, {"units": "41"}, {"units": [4.1]}):
        with pytest.raises(ValueError):
            PreflightReport(readback)


def test_error_payloads_for_clients():
    error = AdminError("admin_revision_conflict", "Changed", current_revision=8, fields=[{"loc": ["x"]}], details={"a": 1})
    assert error.status == 409 and error.payload() == {"code": "admin_revision_conflict", "message": "Changed",
                                                       "fields": [{"loc": ["x"]}], "current_revision": 8, "details": {"a": 1}}
    assert error_payload(ProtocolError("x_unavailable", "Unavailable", 503)) == {"code": "x_unavailable", "message": "Unavailable"}
    assert error_payload(KeyError("secret detail")) == {"code": "internal_error", "message": "KeyError"}


def registry(table=("wallet",)):
    return CommandRegistry(dict.fromkeys(table, object()))


def test_registry_requires_preflight_or_an_explicit_reason():
    commands = registry()
    commands.add(CREDIT)
    with pytest.raises(ValueError, match="letstd"):
        commands.add(command("letstd.rearm", touched=frozenset({"letstd"})))
    commands.add(command("letstd.rearm", touched=frozenset({"letstd"}), preflight_none={"letstd": "Server-only ledger"}))
    assert [item.operation for item in commands] == ["letstd.rearm", "wallet.credit"]
    with pytest.raises(ValueError, match="Duplicate"):
        commands.add(CREDIT)
    with pytest.raises(ValueError):
        CommandRegistry({"Bad Domain": object()})


def test_registry_refuses_exemptions_for_domains_that_have_a_preflight():
    commands = registry()
    for exempt in ({"wallet": "Covered anyway"}, {"wallet": "Covered anyway", "letstd": "Server-only ledger"}):
        with pytest.raises(ValueError, match="exempts domains that have a preflight: wallet"):
            commands.add(command("wallet.audit", touched=frozenset({"wallet", "letstd"}), preflight_none=exempt))
    assert len(commands) == 0 and dict(commands.field_owners()) == {}


def test_registry_collects_exclusive_field_owners():
    commands = registry(("config", "card_status"))
    commands.add(command("config.sound.set", touched=frozenset({"config"}), writes=frozenset({"config.A3", "config.A2"})))
    commands.add(command("guide.set", touched=frozenset({"config"}), writes=frozenset({"config.C1"})))
    commands.add(command("stories.set_unlock", touched=frozenset({"card_status"}), writes=frozenset({"card_status.A7"})))
    assert dict(commands.field_owners()) == {"card_status.A7": "stories.set_unlock", "config.A2": "config.sound.set",
                                             "config.A3": "config.sound.set", "config.C1": "guide.set"}
    with pytest.raises(ValueError, match=r"config\.A2 \(config\.sound\.set\)"):
        commands.add(command("config.other.set", touched=frozenset({"config"}), writes=frozenset({"config.A2", "config.F7"})))
    assert "config.F7" not in commands.field_owners() and "config.other.set" not in commands


def event_view(operation, *, group="view_event", writes=frozenset({"config.F8"})):
    return command(operation, touched=frozenset({"config"}), writes=writes, group=group)


def test_command_groups_share_the_fields_they_write():
    commands = registry(("config",))
    commands.add(event_view("view_event.set"))
    commands.add(event_view("view_event.clear", writes=frozenset({"config.F8", "config.F9"})))
    assert dict(commands.field_owners()) == {"config.F8": "view_event.set", "config.F9": "view_event.clear"}
    assert [item.operation for item in commands] == ["view_event.clear", "view_event.set"]
    for outsider in (event_view("guide.set", group=None), event_view("view.reset", group="view")):
        with pytest.raises(ValueError, match=r"config\.F8 \(view_event\.set\)"):
            commands.add(outsider)
    assert event_view("view_event.set").describe()["group"] == "view_event"


def test_grouped_commands_cannot_claim_a_field_owned_outside_their_group():
    commands = registry(("config",))
    commands.add(event_view("view_event.set", group=None))
    with pytest.raises(ValueError, match=r"config\.F8 \(view_event\.set\)"):
        commands.add(event_view("view_event.clear"))
    assert dict(commands.field_owners()) == {"config.F8": "view_event.set"}


def test_registry_builds_validated_batches():
    commands = registry()
    commands.add(CREDIT)
    commands.add(DEBIT)
    built = commands.batch([{"operation": "wallet.credit", "params": {"amount": 2}},
                            {"operation": "wallet.debit", "params": {"amount": 1}}])
    assert built.normalized() == [{"operation": "wallet.credit", "params": {"amount": 2}},
                                  {"operation": "wallet.debit", "params": {"amount": 1}}]
    with pytest.raises(AdminError) as caught:
        commands.batch([{"operation": "wallet.missing", "params": {}},
                        {"operation": "wallet.credit", "params": {"amount": "2", "extra": 1}},
                        {"operation": "wallet.credit"},
                        {"operation": "wallet.debit", "params": {"amount": 1}}])
    assert caught.value.code == "admin_params_invalid" and caught.value.status == 422
    assert [(item["step"], item["loc"], item["type"]) for item in caught.value.fields] == [
        (0, ["operation"], "command_unknown"), (1, ["params", "amount"], "int_type"),
        (1, ["params", "extra"], "extra_forbidden"), (2, [], "command_invalid")]
    for raw in ([], [{"operation": "wallet.credit", "params": {"amount": 1}}] * (MAX_BATCH + 1), "wallet.credit", None):
        with pytest.raises(AdminError) as caught:
            commands.batch(raw)
        assert caught.value.code == "admin_batch_invalid"


def test_registry_lookup_and_description():
    commands = registry()
    commands.add(CREDIT)
    assert commands.get("wallet.credit") is CREDIT and len(commands) == 1 and "wallet.credit" in commands
    assert [item["operation"] for item in commands.describe()] == ["wallet.credit"]
    with pytest.raises(AdminError) as caught:
        commands.get("wallet.missing")
    assert caught.value.code == "admin_command_unknown" and caught.value.status == 404


def test_view_and_route_registries():
    views, routes = ViewRegistry(), RouteRegistry()
    view = AdminView("units.page", "units", lambda context, params: {}, AmountParams)
    views.add(view)
    routes.add(AdminRoute("config", lambda context: None))
    assert views.get("units.page") is view and "units.page" in views and len(views) == 1
    assert views.describe()[0]["params_schema"]["additionalProperties"] is False
    assert [route.name for route in routes] == ["config"] and "config" in routes
    with pytest.raises(AdminError) as caught:
        views.get("missing")
    assert caught.value.code == "admin_view_unknown" and caught.value.status == 404
    for action in (lambda: views.add(view), lambda: routes.add(AdminRoute("config", lambda context: None)),
                   lambda: AdminView("Units", "units", lambda: None), lambda: AdminView("units", "units", None),
                   lambda: AdminView("units", "units", lambda: None, LooseParams), lambda: AdminRoute("config", None),
                   lambda: AdminView("units", "units", lambda: None, live="yes"),
                   lambda: AdminView("units", "units", lambda: None, live=1)):
        with pytest.raises(ValueError):
            action()


def test_live_views_are_marked_in_their_description():
    live = AdminView("units.projection", "units", lambda context, params: {}, live=True)
    snapshot = AdminView("units.page", "units", lambda context, params: {}, AmountParams)
    assert (live.live, live.describe()["live"], live.describe()["params_schema"]) == (True, True, None)
    assert (snapshot.live, snapshot.describe()["live"]) == (False, False)


COMMAND_MODULE = """
from copy import deepcopy

from aigis_ps.admin_command import AdminCommand, CommandParams, CommandResult


class Params(CommandParams):
    amount: int


def apply(state, params, context):
    return CommandResult(deepcopy(state))


def register(registry):
    registry.add(AdminCommand("{operation}", "wallet", Params, apply, frozenset({{"wallet"}})))
"""
VIEW_MODULE = """
from aigis_ps.admin_registry import AdminView


def register(registry):
    registry.add(AdminView("wallet.summary", "wallet", lambda context, params: {{}}))
"""
ROUTE_MODULE = """
from aigis_ps.admin_registry import AdminRoute


def register(registry):
    registry.add(AdminRoute("wallet", lambda context: None))
"""


@pytest.fixture
def fake_package(tmp_path, monkeypatch):
    name = f"admin_fixture_{uuid.uuid4().hex}"
    root = tmp_path / name
    root.mkdir()
    (root / "__init__.py").write_bytes(b"")
    monkeypatch.syspath_prepend(str(tmp_path))

    def create(**modules):
        for module_name, source in modules.items():
            (root / f"{module_name}.py").write_bytes(textwrap.dedent(source).encode("utf-8"))
        importlib.invalidate_caches()
        return importlib.import_module(name)

    yield create
    for module_name in [module_name for module_name in sys.modules if module_name.split(".")[0] == name]:
        del sys.modules[module_name]


def test_discovery_works_without_admin_modules(fake_package):
    catalog = discover({}, package=fake_package(helpers="VALUE = 1\n"))
    assert catalog.modules == () and len(catalog.commands) == len(catalog.views) == len(catalog.routes) == 0


def test_discovery_registers_modules_by_naming_convention(fake_package):
    package = fake_package(admin_cmd_wallet=COMMAND_MODULE.format(operation="wallet.grant"), admin_views_wallet=VIEW_MODULE.format(),
                           admin_routes_wallet=ROUTE_MODULE, admin_command_helpers="raise RuntimeError('not a command module')\n",
                           unrelated="raise RuntimeError('never imported')\n")
    catalog = discover({"wallet": object()}, package=package)
    assert catalog.modules == ("admin_cmd_wallet", "admin_routes_wallet", "admin_views_wallet")
    assert [item.operation for item in catalog.commands] == ["wallet.grant"]
    assert [item.name for item in catalog.views] == ["wallet.summary"] and [item.name for item in catalog.routes] == ["wallet"]
    assert f"{package.__name__}.unrelated" not in sys.modules


def test_discovery_applies_preflight_coverage(fake_package):
    package = fake_package(admin_cmd_wallet=COMMAND_MODULE.format(operation="wallet.grant"))
    with pytest.raises(ValueError, match="wallet"):
        discover({}, package=package)


def test_discovered_modules_must_export_register(fake_package):
    with pytest.raises(ValueError, match="register"):
        discover({}, package=fake_package(admin_views_broken="VALUE = 1\n"))
