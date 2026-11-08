from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from http import HTTPStatus
import json
import re
import threading
import time
from types import MappingProxyType
from typing import Any, Protocol, get_args
import uuid

from pydantic import BaseModel, ConfigDict, ValidationError

from .admin_diff import json_digest, redact
from .energy_clock import JST
from .errors import ProtocolError
from .native_numbers import INT32_MAX
from .startup_diagnostics import INTERNAL_ERROR
from .storage import ADMIN_ACTOR, serialize


MAX_BATCH = 100
PLAN_CAPACITY = 32
PLAN_TTL_SECONDS = 600
MAX_CONFIRM_PHRASE_CHARS = 128
OPERATION_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\Z")
DOMAIN_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
FIELD_REFERENCE = re.compile(r"[a-z][a-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\Z")
COMMAND_UNAVAILABLE = "admin_command_unavailable"
COMMAND_INVALID = "admin_command_invalid"
BOOLEAN_HINT = "0 or 1"
Availability = Callable[[Any], str | None]


class AdminError(ProtocolError):
    def __init__(self, code: str, message: str, status: int = HTTPStatus.CONFLICT, *,
                 fields: Iterable[Mapping[str, Any]] = (), current_revision: int | None = None,
                 details: Mapping[str, Any] | None = None):
        # Failure records accept only plain int statuses, not HTTPStatus members.
        super().__init__(code, message, int(status))
        self.fields = tuple(dict(item) for item in fields)
        self.current_revision = current_revision
        self.details = dict(details or {})

    def payload(self) -> dict:
        result: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.fields:
            result["fields"] = [dict(item) for item in self.fields]
        if self.current_revision is not None:
            result["current_revision"] = self.current_revision
        if self.details:
            result["details"] = dict(self.details)
        return result


def error_payload(error: BaseException) -> dict:
    if isinstance(error, AdminError):
        return error.payload()
    if isinstance(error, ProtocolError):
        return {"code": error.code, "message": str(error)}
    return {"code": INTERNAL_ERROR, "message": type(error).__name__}


def require_op_id(value: object) -> str:
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.version != 4 or str(parsed) != value:
        raise AdminError("admin_op_id_invalid", "Operation IDs must be canonical lowercase UUIDv4 strings",
                         HTTPStatus.UNPROCESSABLE_ENTITY)
    return value


class CommandParams(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    found, pending = [], [annotation]
    while pending:
        item = pending.pop()
        if isinstance(item, type) and issubclass(item, BaseModel):
            found.append(item)
        else:
            pending.extend(get_args(item))
    return found


def validate_params_model(model: object) -> None:
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise ValueError("Admin parameters require a pydantic model")
    pending, seen = [model], set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        # Nested models keep their own configuration, so each one must opt in.
        if current.model_config.get("strict") is not True or current.model_config.get("extra") != "forbid":
            raise ValueError(f"Admin parameters must be strict and forbid extra fields: {current.__name__}")
        for info in current.model_fields.values():
            pending.extend(_nested_models(info.annotation))


def field_errors(error: ValidationError) -> list[dict]:
    return [{"loc": list(item["loc"]), "type": item["type"], "msg": item["msg"]}
            for item in error.errors(include_url=False, include_context=False, include_input=False)]


def require_distinct(values: Iterable[Hashable], label: str) -> None:
    seen = set()
    for value in values:
        if value in seen:
            raise ValueError(f"{label} must be distinct")
        seen.add(value)


def counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}S"


@dataclass(frozen=True)
class CommandContext:
    now: datetime
    acquisition_day: int
    revision: int
    generation_id: str
    content_versions: Mapping[str, str | None] = field(default_factory=dict)
    op_id: str | None = None
    draws: tuple = ()
    services: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.now, datetime) or self.now.utcoffset() is None:
            raise ValueError("Admin commands require a timezone-aware frozen time")
        if type(self.acquisition_day) is not int or not 1 <= self.acquisition_day <= INT32_MAX:
            raise ValueError("Admin commands require a positive native acquisition day")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("Admin commands require a nonnegative profile revision")
        if not isinstance(self.generation_id, str) or not self.generation_id:
            raise ValueError("Admin commands require a generation identity")
        versions = dict(self.content_versions)
        if any(not isinstance(name, str) or not (version is None or isinstance(version, str)) for name, version in versions.items()):
            raise ValueError("Content versions must map domain names to version text")
        object.__setattr__(self, "content_versions", MappingProxyType(dict(sorted(versions.items()))))
        if self.op_id is not None:
            require_op_id(self.op_id)
        object.__setattr__(self, "draws", tuple(self.draws))
        serialize(list(self.draws))

    @property
    def actor(self) -> str:
        return ADMIN_ACTOR

    @property
    def now_utc(self) -> datetime:
        return self.now.astimezone(timezone.utc)

    @property
    def now_jst(self) -> datetime:
        return self.now.astimezone(JST)

    @property
    def jst_date(self) -> date:
        return self.now_jst.date()

    def frozen(self) -> dict:
        return {"now": self.now_utc.isoformat(), "now_jst": self.now_jst.isoformat(),
                "acquisition_day": self.acquisition_day, "revision": self.revision,
                "generation_id": self.generation_id, "content_versions": dict(self.content_versions),
                "actor": self.actor, "draws": list(self.draws)}


@dataclass(frozen=True)
class CommandResult:
    state: dict
    effects: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, dict):
            raise ValueError("Admin commands must return a state object")
        if not isinstance(self.effects, Mapping) or any(not isinstance(name, str) for name in self.effects):
            raise ValueError("Admin command effects must be an object")
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if any(not isinstance(warning, str) for warning in self.warnings):
            raise ValueError("Admin command warnings must be text")


def missing_service(name: str) -> str:
    return f"This generation has no {name} service"


def requires(name: str, reason: str | None = None) -> Availability:
    def unavailable(services: Any) -> str | None:
        return None if getattr(services, name, None) is not None else reason or missing_service(name)

    return unavailable


def first_unavailable(*checks: Availability) -> Availability:
    def unavailable(services: Any) -> str | None:
        return next((reason for check in checks if (reason := check(services)) is not None), None)

    return unavailable


def requires_all(*names: str) -> Availability:
    return first_unavailable(*(requires(name) for name in names))


def required_service(context: CommandContext, name: str, reason: str | None = None) -> Any:
    found = getattr(context.services, name, None)
    if found is None:
        raise AdminError(COMMAND_UNAVAILABLE, reason or missing_service(name), HTTPStatus.SERVICE_UNAVAILABLE)
    return found


@dataclass(frozen=True)
class AdminCommand:
    operation: str
    domain: str
    params_model: type[BaseModel]
    apply: Callable[[dict, BaseModel, CommandContext], CommandResult]
    touched: frozenset[str]
    writes: frozenset[str] = frozenset()
    destructive: bool | Callable[[BaseModel], bool] = False
    confirm_phrase: Callable[[BaseModel], str] | None = None
    preflight_none: Mapping[str, str] = field(default_factory=dict)
    unavailable: Availability | None = None
    group: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, str) or not OPERATION_NAME.fullmatch(self.operation):
            raise ValueError("Admin operations use lowercase dotted names such as units.grant")
        if not isinstance(self.domain, str) or not DOMAIN_NAME.fullmatch(self.domain):
            raise ValueError(f"Invalid admin command domain: {self.operation}")
        validate_params_model(self.params_model)
        if not callable(self.apply) or (self.unavailable is not None and not callable(self.unavailable)):
            raise ValueError(f"Admin command hooks must be callable: {self.operation}")
        touched = frozenset(self.touched) if not isinstance(self.touched, str) else frozenset()
        if not touched or any(not isinstance(name, str) or not DOMAIN_NAME.fullmatch(name) for name in touched):
            raise ValueError(f"Admin commands must declare the domains they touch: {self.operation}")
        writes = frozenset(self.writes) if not isinstance(self.writes, str) else None
        if writes is None or any(not isinstance(name, str) or not FIELD_REFERENCE.fullmatch(name) for name in writes):
            raise ValueError(f"Shared-table writes use table.Field references: {self.operation}")
        exempt = dict(self.preflight_none)
        if (set(exempt) - touched
                or any(not isinstance(reason, str) or not reason.strip() for reason in exempt.values())):
            raise ValueError(f"Domains without preflight need a touched domain and a reason: {self.operation}")
        if type(self.destructive) is not bool and not callable(self.destructive):
            raise ValueError(f"Destructive commands are marked by a bool or a parameter predicate: {self.operation}")
        if (self.destructive is not False) != (self.confirm_phrase is not None):
            raise ValueError(f"Destructive commands and confirmation phrases go together: {self.operation}")
        if self.confirm_phrase is not None and not callable(self.confirm_phrase):
            raise ValueError(f"Confirmation phrases must be computed by a callable: {self.operation}")
        if self.group is not None and (not isinstance(self.group, str) or not self.operation.startswith(self.group + ".")):
            raise ValueError(f"Command groups are named by a leading part of their operations: {self.operation}")
        object.__setattr__(self, "touched", touched)
        object.__setattr__(self, "writes", writes)
        object.__setattr__(self, "preflight_none", MappingProxyType(dict(sorted(exempt.items()))))

    @property
    def destructive_by_params(self) -> bool:
        return type(self.destructive) is not bool

    def parse(self, raw: object) -> BaseModel:
        try:
            document = serialize(raw)
        except (TypeError, ValueError) as error:
            raise AdminError("admin_params_invalid", f"Parameters for {self.operation} must be JSON values",
                             HTTPStatus.UNPROCESSABLE_ENTITY) from error
        try:
            return self.params_model.model_validate_json(document)
        except ValidationError as error:
            raise AdminError("admin_params_invalid", f"Invalid parameters for {self.operation}",
                             HTTPStatus.UNPROCESSABLE_ENTITY, fields=field_errors(error)) from error

    def is_destructive(self, params: BaseModel) -> bool:
        if not self.destructive_by_params:
            return self.destructive
        decision = self.destructive(params)
        if type(decision) is not bool:
            raise AdminError(COMMAND_INVALID, f"{self.operation} could not decide whether its parameters are destructive",
                             HTTPStatus.INTERNAL_SERVER_ERROR)
        return decision

    def phrase(self, params: BaseModel) -> str | None:
        if self.confirm_phrase is None:
            return None
        phrase = self.confirm_phrase(params)
        if not isinstance(phrase, str) or not phrase.strip() or len(phrase) > MAX_CONFIRM_PHRASE_CHARS or not phrase.isprintable():
            raise AdminError(COMMAND_INVALID, f"{self.operation} produced an unusable confirmation phrase",
                             HTTPStatus.INTERNAL_SERVER_ERROR)
        return phrase

    def availability(self, services: Any) -> str | None:
        if self.unavailable is None:
            return None
        reason = self.unavailable(services)
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise AdminError(COMMAND_INVALID, f"{self.operation} reported an unusable availability reason",
                             HTTPStatus.INTERNAL_SERVER_ERROR)
        return reason

    def describe(self) -> dict:
        return {"operation": self.operation, "domain": self.domain, "destructive": self.destructive is not False,
                "destructive_by_params": self.destructive_by_params, "group": self.group,
                "touched": sorted(self.touched), "writes": sorted(self.writes),
                "preflight_none": dict(self.preflight_none), "params_schema": self.params_model.model_json_schema()}


@dataclass(frozen=True)
class BatchStep:
    command: AdminCommand
    params: BaseModel

    def __post_init__(self) -> None:
        if not isinstance(self.command, AdminCommand) or not isinstance(self.params, self.command.params_model):
            raise ValueError("Batch steps pair a command with its validated parameters")

    @property
    def destructive(self) -> bool:
        return self.command.is_destructive(self.params)


@dataclass(frozen=True)
class StepOutcome:
    operation: str
    effects: Mapping[str, Any]
    warnings: tuple[str, ...]

    def describe(self, step: int) -> dict:
        return {"step": step, "operation": self.operation, "effects": redact(dict(self.effects)), "warnings": list(self.warnings)}


@dataclass(frozen=True)
class BatchOutcome:
    state: dict
    steps: tuple[StepOutcome, ...]


@dataclass(frozen=True)
class Confirmation:
    step: int
    operation: str
    phrase: str

    def describe(self) -> dict:
        return {"step": self.step, "operation": self.operation, "phrase": self.phrase}


def _at_step(error: ProtocolError, step: int, operation: str) -> AdminError:
    extra = error if isinstance(error, AdminError) else None
    return AdminError(error.code, str(error), error.status, fields=extra.fields if extra else (),
                      current_revision=extra.current_revision if extra else None,
                      details={**(extra.details if extra else {}), "step": step, "operation": operation})


@dataclass(frozen=True)
class Batch:
    steps: tuple[BatchStep, ...]

    def __post_init__(self) -> None:
        steps = tuple(self.steps)
        if not 1 <= len(steps) <= MAX_BATCH:
            raise AdminError("admin_batch_invalid", f"A batch needs between 1 and {MAX_BATCH} commands",
                             HTTPStatus.UNPROCESSABLE_ENTITY)
        if any(not isinstance(step, BatchStep) for step in steps):
            raise ValueError("Batches contain validated batch steps")
        object.__setattr__(self, "steps", steps)

    @property
    def touched(self) -> frozenset[str]:
        return frozenset().union(*(step.command.touched for step in self.steps))

    @property
    def writes(self) -> frozenset[str]:
        return frozenset().union(*(step.command.writes for step in self.steps))

    def normalized(self) -> list[dict]:
        return [{"operation": step.command.operation, "params": step.params.model_dump(mode="json")} for step in self.steps]

    def confirmations(self) -> tuple[Confirmation, ...]:
        return tuple(Confirmation(index, step.command.operation, step.command.phrase(step.params))
                     for index, step in enumerate(self.steps) if step.destructive)

    def require_available(self, services: Any) -> None:
        for index, step in enumerate(self.steps):
            reason = step.command.availability(services)
            if reason is not None:
                raise AdminError(COMMAND_UNAVAILABLE, reason, HTTPStatus.SERVICE_UNAVAILABLE,
                                 details={"step": index, "operation": step.command.operation})

    def apply(self, state: dict, context: CommandContext) -> BatchOutcome:
        candidate = deepcopy(state)
        outcomes = []
        for index, step in enumerate(self.steps):
            operation = step.command.operation
            try:
                result = step.command.apply(candidate, step.params, context)
            except ProtocolError as error:
                raise _at_step(error, index, operation) from error
            if not isinstance(result, CommandResult):
                raise AdminError("admin_command_result_invalid", f"{operation} did not return a command result",
                                 HTTPStatus.INTERNAL_SERVER_ERROR, details={"step": index, "operation": operation})
            try:
                effects = json.loads(serialize(dict(result.effects)))
            except (TypeError, ValueError) as error:
                raise AdminError("admin_command_result_invalid", f"{operation} reported effects that are not JSON",
                                 HTTPStatus.INTERNAL_SERVER_ERROR, details={"step": index, "operation": operation}) from error
            candidate = result.state
            outcomes.append(StepOutcome(operation, MappingProxyType(effects), result.warnings))
        return BatchOutcome(candidate, tuple(outcomes))


def check_confirmations(required: Sequence[Confirmation], provided: object) -> None:
    supplied = tuple(provided) if isinstance(provided, Sequence) and not isinstance(provided, str) else None
    if (supplied is None or len(supplied) != len(required)
            or any(not isinstance(text, str) or text != item.phrase for text, item in zip(supplied, required))):
        raise AdminError("admin_confirmation_required", "Destructive commands need their exact confirmation phrases",
                         HTTPStatus.UNPROCESSABLE_ENTITY, details={"required": [item.describe() for item in required]})


def compute_plan_digest(batch: Batch, context: CommandContext, diff_sha256: str) -> str:
    return json_digest({"commands": batch.normalized(), "context": context.frozen(),
                        "expected_revision": context.revision, "generation_id": context.generation_id,
                        "diff_sha256": diff_sha256})


@dataclass(frozen=True)
class Plan:
    batch: Batch
    context: CommandContext
    plan_digest: str
    diff_sha256: str
    state_sha256_before: str
    state_sha256_after: str
    diff: Mapping[str, Any]
    steps: tuple[StepOutcome, ...]
    confirmations: tuple[Confirmation, ...]
    preflight: Mapping[str, Any]
    restart_check: Mapping[str, Any]
    audit: Mapping[str, Any]
    plan_id: str | None = None

    @property
    def expected_revision(self) -> int:
        return self.context.revision

    @property
    def generation_id(self) -> str:
        return self.context.generation_id

    @property
    def committable(self) -> bool:
        return all(check.get("ok") is True for check in (self.preflight, self.restart_check, self.audit))

    @property
    def changed(self) -> bool:
        return self.state_sha256_before != self.state_sha256_after

    def describe(self) -> dict:
        return {"plan_id": self.plan_id, "plan_digest": self.plan_digest, "committable": self.committable,
                "expected_revision": self.expected_revision, "generation_id": self.generation_id,
                "context": self.context.frozen(), "commands": redact(self.batch.normalized()),
                "touched": sorted(self.batch.touched), "writes": sorted(self.batch.writes),
                "confirmations": [item.describe() for item in self.confirmations],
                "diff": self.diff, "steps": [outcome.describe(index) for index, outcome in enumerate(self.steps)],
                "preflight": dict(self.preflight), "restart_check": dict(self.restart_check), "audit": dict(self.audit),
                "state_sha256_before": self.state_sha256_before, "state_sha256_after": self.state_sha256_after}


class PlanStore:
    def __init__(self, *, capacity: int = PLAN_CAPACITY, ttl_seconds: float = PLAN_TTL_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        if type(capacity) is not int or capacity < 1 or isinstance(ttl_seconds, bool) or not ttl_seconds > 0 or not callable(clock):
            raise ValueError("Plan storage needs a positive capacity, a positive lifetime and a clock")
        self._capacity, self._ttl, self._clock = capacity, ttl_seconds, clock
        self._plans: OrderedDict[str, tuple[float, Plan]] = OrderedDict()
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        for plan_id in [plan_id for plan_id, (deadline, _) in self._plans.items() if deadline <= now]:
            del self._plans[plan_id]

    def put(self, plan: Plan) -> Plan:
        if not isinstance(plan, Plan) or not plan.committable:
            raise ValueError("Only committable plans can be stored")
        stored = replace(plan, plan_id=uuid.uuid4().hex)
        with self._lock:
            now = self._clock()
            self._expire(now)
            while len(self._plans) >= self._capacity:
                self._plans.popitem(last=False)
            self._plans[stored.plan_id] = (now + self._ttl, stored)
        return stored

    def get(self, plan_id: object) -> Plan:
        with self._lock:
            self._expire(self._clock())
            entry = self._plans.get(plan_id) if isinstance(plan_id, str) else None
        if entry is None:
            raise AdminError("plan_expired", "The plan expired or was replaced; preview it again")
        return entry[1]

    def discard(self, plan_id: str) -> None:
        with self._lock:
            self._plans.pop(plan_id, None)

    def __len__(self) -> int:
        with self._lock:
            self._expire(self._clock())
            return len(self._plans)


@dataclass(frozen=True)
class PreflightReport:
    readback: Mapping[str, tuple[int | str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        readback = {}
        for domain, requests in dict(self.readback).items():
            requests = tuple(requests) if not isinstance(requests, (str, bytes)) else None
            if (not isinstance(domain, str) or not DOMAIN_NAME.fullmatch(domain) or requests is None
                    or any(isinstance(item, bool) or not isinstance(item, (int, str)) for item in requests)):
                raise ValueError("Preflight readback maps domains to client request numbers or routes")
            readback[domain] = requests
        object.__setattr__(self, "readback", MappingProxyType(dict(sorted(readback.items()))))

    def to_dict(self) -> dict:
        return {"readback": {domain: list(requests) for domain, requests in self.readback.items()}}


class Preflight(Protocol):
    is_noop: bool

    def check(self, before: dict, candidate: dict, domains: frozenset[str], generation: Any) -> PreflightReport: ...


class RestartCheck(Protocol):
    is_noop: bool

    def check(self, candidate: dict, generation: Any) -> None: ...


class NoopPreflight:
    is_noop = True

    def check(self, before: dict, candidate: dict, domains: frozenset[str], generation: Any) -> PreflightReport:
        return PreflightReport()


class NoopRestartCheck:
    is_noop = True

    def check(self, candidate: dict, generation: Any) -> None:
        return None
