from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Protocol

from .admin_command import (AdminError, Batch, BatchOutcome, CommandContext, Plan, PlanStore, Preflight, PreflightReport,
                            RestartCheck, check_confirmations, compute_plan_digest, error_payload, require_op_id)
from .admin_diff import DiffLimits, canonical, diff_digest, diff_states, json_digest, redact, render_diff, same
from .errors import ProtocolError
from .storage import (ADMIN_ACTOR, AUDIT_JSON_FIELDS, AUTO_BACKUP_KIND, BACKUP_FAILED, MAX_AUDIT_FIELD_BYTES,
                      MAX_BACKUP_REASON, AdminCommit, AdminOperationConflict, PlayerProfile, RevisionConflict,
                      admin_payload_digest, serialize)


COMMAND_KIND = "command"
AUDIT_TOO_LARGE = "admin_audit_too_large"
AUDIT_DIFF_LIMITS = DiffLimits(max_entries=200, max_value_bytes=2 * 1024, max_bytes=64 * 1024)
RESTART_DIFF_LIMITS = DiffLimits(max_entries=100, max_value_bytes=4 * 1024, max_bytes=128 * 1024)
# Previews have no op_id yet, so the record is measured with a fixed placeholder of the same canonical form.
PREVIEW_OP_ID = "00000000-0000-4000-8000-000000000000"
# An audit record carries the preflight readback, so a failed preflight leaves no record to measure.
AUDIT_SKIPPED = MappingProxyType({"ok": False, "skipped": "preflight"})
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class AdminGeneration(Protocol):
    id: str
    bundle: Any
    content_versions: Mapping[str, str | None]

    def now(self) -> datetime: ...

    def acquisition_day(self, now: datetime) -> int: ...


class AdminRuntime(Protocol):
    def lease(self) -> AbstractContextManager[AdminGeneration]: ...


class AdminStore(Protocol):
    def profile(self) -> PlayerProfile: ...

    def find_admin_operation(self, op_id: str) -> Mapping[str, Any] | None: ...

    def commit_admin(self, op_id: str, payload_digest: str, expected_revision: int | None,
                     state: dict | None, record: Mapping[str, Any]) -> AdminCommit: ...

    def backup(self, directory: Path, reason: str, op_id: str | None = None, *, kind: str) -> Mapping[str, Any]: ...


def revision_conflict(current: int) -> AdminError:
    return AdminError("admin_revision_conflict", "The player state changed; preview the plan again", current_revision=current)


def run_preflight(preflight: Preflight, before: dict, candidate: dict, domains: frozenset[str],
                  generation: AdminGeneration) -> PreflightReport:
    report = preflight.check(before, candidate, domains, generation)
    if not isinstance(report, PreflightReport):
        raise AdminError("admin_preflight_invalid", "Preflight must return a preflight report",
                         HTTPStatus.INTERNAL_SERVER_ERROR)
    return report


def require_plan_digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise AdminError("admin_plan_digest_invalid", "Plan digests are 64 lowercase hex characters",
                         HTTPStatus.UNPROCESSABLE_ENTITY)
    return value


def require_expected_revision(value: object) -> int:
    if type(value) is not int or value < 0:
        raise AdminError("admin_revision_invalid", "Expected revisions are nonnegative integers",
                         HTTPStatus.UNPROCESSABLE_ENTITY)
    return value


def attempt(check: Callable[[], Mapping[str, Any] | None]) -> dict:
    try:
        return {"ok": True, **(check() or {})}
    except ProtocolError as error:
        return {"ok": False, "error": error_payload(error)}


def attempt_preflight(check: Callable[[], PreflightReport]) -> tuple[dict, PreflightReport | None]:
    report: PreflightReport | None = None

    def readback() -> dict:
        nonlocal report
        report = check()
        return report.to_dict()

    outcome = attempt(readback)
    return outcome, report


def requires_backup(batch: Batch) -> bool:
    return len(batch.steps) > 1 or any(step.destructive for step in batch.steps)


def takes_backup(plan: Plan) -> bool:
    # Only one automatic backup is kept, so a commit that leaves the state as it is must not replace it.
    return plan.changed and requires_backup(plan.batch)


def require_auditable(record: Mapping[str, Any]) -> Mapping[str, Any]:
    sizes = {name: len(serialize(record[name]).encode("utf-8")) for name in AUDIT_JSON_FIELDS if name in record}
    oversized = {name: size for name, size in sizes.items() if size > MAX_AUDIT_FIELD_BYTES}
    if oversized:
        raise AdminError(AUDIT_TOO_LARGE, "The audit record exceeds the size the store keeps, so nothing was committed",
                         HTTPStatus.UNPROCESSABLE_ENTITY, details={"fields": oversized, "limit": MAX_AUDIT_FIELD_BYTES})
    return record


def preview_audit(report: PreflightReport | None, record: Callable[[PreflightReport], Mapping[str, Any]]) -> dict:
    if report is None:
        return dict(AUDIT_SKIPPED)
    return attempt(lambda: require_auditable(record(report)) and None)


def backup_reason(batch: Batch) -> str:
    first, more = batch.steps[0].command.operation, len(batch.steps) - 1
    return (f"before {first} and {more} more" if more else f"before {first}")[:MAX_BACKUP_REASON]


def auto_backup(store: AdminStore, directory: Path, reason: str, op_id: str) -> dict:
    try:
        registered = store.backup(directory, reason, op_id, kind=AUTO_BACKUP_KIND)
    except ProtocolError as error:
        raise AdminError(BACKUP_FAILED, "The automatic backup failed, so nothing was written",
                         HTTPStatus.SERVICE_UNAVAILABLE, details={"error": error_payload(error)}) from error
    backup, _ = canonical(dict(registered))
    return backup


class InitializerRestartCheck:
    is_noop = False

    def __init__(self, initializer: Callable[[dict], dict] | None = None, *, limits: DiffLimits = RESTART_DIFF_LIMITS):
        if (initializer is not None and not callable(initializer)) or not isinstance(limits, DiffLimits):
            raise ValueError("Restart checks need a callable initializer and diff limits")
        self._initializer, self._limits = initializer, limits

    def check(self, candidate: dict, generation: AdminGeneration | None = None) -> None:
        initializer = self._initializer
        if initializer is None:
            if generation is None:
                raise ValueError("Restart checks without an initializer use the leased generation")
            initializer = generation.bundle.initialize
        # A restart initializes the stored JSON, so both sides are compared in that form.
        stored, _ = canonical(candidate)
        try:
            initialized, _ = canonical(initializer(deepcopy(stored)))
        except Exception as error:
            raise AdminError("admin_restart_incompatible", "Restarting the server would reject this state",
                             details={"error": error_payload(error)}) from error
        if not same(initialized, stored):
            raise AdminError("admin_restart_incompatible", "Restarting the server would rewrite this state",
                             details={"diff": render_diff(diff_states(stored, initialized), self._limits)})


@dataclass(frozen=True)
class CommitOutcome:
    result: Mapping[str, Any]
    replayed: bool

    def to_dict(self) -> dict:
        return {**self.result, "replayed": self.replayed}


def stored_result(record: Mapping[str, Any]) -> dict:
    result = record.get("result") if isinstance(record, Mapping) else None
    if not isinstance(result, Mapping):
        raise AdminError("admin_operation_record_invalid", "The recorded operation has no result",
                         HTTPStatus.INTERNAL_SERVER_ERROR)
    return dict(result)


def recorded_outcome(store: AdminStore, op_id: str, payload_digest: str) -> CommitOutcome | None:
    record = store.find_admin_operation(op_id)
    if record is None:
        return None
    if record.get("payload_digest") != payload_digest:
        raise AdminError("admin_operation_conflict", "This operation ID was already used for a different plan")
    return CommitOutcome(stored_result(record), replayed=True)


def commit_audited(store: AdminStore, op_id: str, payload_digest: str, expected_revision: int, state: dict | None,
                   record: Mapping[str, Any]) -> CommitOutcome:
    require_auditable(record)
    try:
        committed = store.commit_admin(op_id, payload_digest, expected_revision, state, record)
    except RevisionConflict as error:
        raise revision_conflict(error.current_revision) from error
    except AdminOperationConflict:
        # A concurrent request committed this op_id first; answer as the idempotent lookup does.
        replayed = recorded_outcome(store, op_id, payload_digest)
        if replayed is None:
            raise
        return replayed
    if not isinstance(committed, AdminCommit):
        raise AdminError("admin_commit_unrecorded", "The store did not confirm the audited commit",
                         HTTPStatus.INTERNAL_SERVER_ERROR)
    return CommitOutcome(stored_result(committed.operation), replayed=committed.replayed)


@dataclass(frozen=True)
class _Evaluation:
    outcome: BatchOutcome
    candidate: dict
    entries: tuple[dict, ...]
    diff_sha256: str
    state_sha256_before: str
    state_sha256_after: str

    @property
    def changed(self) -> bool:
        return self.state_sha256_before != self.state_sha256_after


class CommitPipeline:
    def __init__(self, store: AdminStore, plan_store: PlanStore, preflight: Preflight, restart_check: RestartCheck,
                 runtime: AdminRuntime, *, backup_directory: Path):
        if not isinstance(plan_store, PlanStore):
            raise ValueError("Commits require the process plan store")
        for role, value, methods in (("store", store, ("profile", "find_admin_operation", "commit_admin", "backup")),
                                     ("preflight", preflight, ("check",)), ("restart check", restart_check, ("check",)),
                                     ("runtime", runtime, ("lease",))):
            if any(not callable(getattr(value, method, None)) for method in methods):
                raise ValueError(f"The {role} must provide {', '.join(methods)}")
        if any(not isinstance(getattr(check, "is_noop", None), bool) for check in (preflight, restart_check)):
            raise ValueError("Preflight and restart checks must state whether they are no-ops")
        if not isinstance(backup_directory, Path):
            raise ValueError("Commits need the directory for automatic backups")
        self._store, self._plans, self._backup_directory = store, plan_store, backup_directory
        self._preflight, self._restart_check, self._runtime = preflight, restart_check, runtime

    def preview(self, batch: Batch) -> Plan:
        if not isinstance(batch, Batch):
            raise ValueError("Previews take a validated command batch")
        with self._runtime.lease() as generation:
            batch.require_available(generation.bundle)
            profile = self._store.profile()
            context = self._context(generation, profile.revision)
            evaluation = self._evaluate(batch, profile.state, context)
            preflight, report = attempt_preflight(lambda: self._preflight_report(
                batch, profile.state, evaluation.candidate, generation))
            restart = attempt(lambda: self._restart_check.check(evaluation.candidate, generation))
        plan = Plan(batch=batch, context=replace(context, services=None),
                    plan_digest=compute_plan_digest(batch, context, evaluation.diff_sha256), diff_sha256=evaluation.diff_sha256,
                    state_sha256_before=evaluation.state_sha256_before, state_sha256_after=evaluation.state_sha256_after,
                    diff=render_diff(evaluation.entries), steps=evaluation.outcome.steps,
                    confirmations=batch.confirmations(), preflight=preflight, restart_check=restart, audit={"ok": True})
        plan = replace(plan, audit=preview_audit(
            report, lambda checked: self._record(PREVIEW_OP_ID, plan, evaluation, checked, None)))
        return self._plans.put(plan) if plan.committable else plan

    def commit(self, *, plan_id: str, plan_digest: str, expected_revision: int, op_id: str,
               confirmations: tuple[str, ...] = ()) -> CommitOutcome:
        op_id = require_op_id(op_id)
        plan_digest, expected_revision = require_plan_digest(plan_digest), require_expected_revision(expected_revision)
        payload = admin_payload_digest(op_id, plan_digest)
        replayed = recorded_outcome(self._store, op_id, payload)
        if replayed is not None:
            return replayed
        plan = self._plans.get(plan_id)
        if plan.plan_digest != plan_digest or plan.expected_revision != expected_revision:
            raise AdminError("plan_changed", "The request does not match the previewed plan; preview it again")
        check_confirmations(plan.confirmations, confirmations)
        with self._runtime.lease() as generation:
            profile = self._store.profile()
            if profile.revision != expected_revision:
                raise revision_conflict(profile.revision)
            context = self._replay_context(plan, generation, op_id)
            evaluation = self._evaluate(plan.batch, profile.state, context)
            if (evaluation.diff_sha256 != plan.diff_sha256 or evaluation.state_sha256_before != plan.state_sha256_before
                    or evaluation.state_sha256_after != plan.state_sha256_after):
                raise AdminError("plan_changed", "Replaying the plan produced a different result; preview it again")
            report = self._preflight_report(plan.batch, profile.state, evaluation.candidate, generation)
            self._restart_check.check(evaluation.candidate, generation)
            # Measured before the backup, so an oversized record cannot replace the single automatic backup.
            record = require_auditable(self._record(op_id, plan, evaluation, report, None))
            if takes_backup(plan):
                backup = auto_backup(self._store, self._backup_directory, backup_reason(plan.batch), op_id)
                record = self._record(op_id, plan, evaluation, report, backup)
            outcome = commit_audited(self._store, op_id, payload, expected_revision,
                                     evaluation.candidate if evaluation.changed else None, record)
        self._plans.discard(plan.plan_id)
        return outcome

    @staticmethod
    def _context(generation: AdminGeneration, revision: int) -> CommandContext:
        now = generation.now()
        return CommandContext(now=now, acquisition_day=generation.acquisition_day(now), revision=revision,
                              generation_id=generation.id, content_versions=generation.content_versions,
                              services=generation.bundle)

    @staticmethod
    def _replay_context(plan: Plan, generation: AdminGeneration, op_id: str) -> CommandContext:
        frozen, now = plan.context, generation.now()
        current = CommandContext(now=now, acquisition_day=generation.acquisition_day(now), revision=frozen.revision,
                                 generation_id=generation.id, content_versions=generation.content_versions)
        changed = [name for name, unchanged in (
            ("generation_id", current.generation_id == frozen.generation_id),
            ("content_versions", dict(current.content_versions) == dict(frozen.content_versions)),
            ("acquisition_day", current.acquisition_day == frozen.acquisition_day),
            ("jst_date", current.jst_date == frozen.jst_date)) if not unchanged]
        if changed:
            raise AdminError("plan_stale", "The server generation, content or JST day changed; preview the plan again",
                             details={"changed": changed})
        return replace(frozen, op_id=op_id, services=generation.bundle)

    @staticmethod
    def _evaluate(batch: Batch, state: dict, context: CommandContext) -> _Evaluation:
        outcome = batch.apply(state, context)
        try:
            candidate, after = canonical(outcome.state)
        except (TypeError, ValueError) as error:
            raise AdminError("admin_command_result_invalid", "The batch produced a state that cannot be stored",
                             HTTPStatus.INTERNAL_SERVER_ERROR) from error
        entries = diff_states(state, candidate)
        return _Evaluation(outcome, candidate, entries, diff_digest(entries), json_digest(state), after)

    def _preflight_report(self, batch: Batch, before: dict, candidate: dict, generation: AdminGeneration) -> PreflightReport:
        return run_preflight(self._preflight, before, candidate, batch.touched, generation)

    @staticmethod
    def _result(op_id: str, plan: Plan, evaluation: _Evaluation, report: PreflightReport, backup: dict | None) -> dict:
        revision = plan.expected_revision
        result, _ = canonical({
            "op_id": op_id, "kind": COMMAND_KIND, "plan_digest": plan.plan_digest, "generation_id": plan.generation_id,
            "revision_before": revision, "revision_after": revision + 1 if evaluation.changed else revision,
            "state_sha256_before": evaluation.state_sha256_before, "state_sha256_after": evaluation.state_sha256_after,
            "steps": [outcome.describe(index) for index, outcome in enumerate(evaluation.outcome.steps)],
            "readback": report.to_dict()["readback"], "backup_before_commit": backup})
        return result

    @classmethod
    def _record(cls, op_id: str, plan: Plan, evaluation: _Evaluation, report: PreflightReport, backup: dict | None) -> dict:
        result = cls._result(op_id, plan, evaluation, report, backup)
        return {"kind": COMMAND_KIND, "actor": ADMIN_ACTOR, "operations": redact(plan.batch.normalized()),
                "plan_digest": plan.plan_digest, "targets": sorted(plan.batch.touched),
                "revision_before": result["revision_before"], "revision_after": result["revision_after"],
                "state_sha256_before": evaluation.state_sha256_before, "state_sha256_after": evaluation.state_sha256_after,
                "diff": render_diff(evaluation.entries, AUDIT_DIFF_LIMITS), "generation_id": plan.generation_id,
                "content_versions": dict(plan.context.content_versions), "result": result}
