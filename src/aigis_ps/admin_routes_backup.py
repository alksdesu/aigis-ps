from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
import hashlib
from http import HTTPStatus
import json
import os
from pathlib import Path
from types import MappingProxyType
import uuid

from fastapi import APIRouter, Request
from pydantic import Field, field_validator

from .admin_app import (AdminServices, ConfirmPhrase, Cursor, LeasedGeneration, PageSize, admin_page,
                        request_body)
from .admin_command import AdminError, CommandParams, Confirmation, PreflightReport, check_confirmations, require_op_id
from .admin_commit import (AUDIT_DIFF_LIMITS, PREVIEW_OP_ID, attempt, attempt_preflight, auto_backup, commit_audited,
                           preview_audit, recorded_outcome, require_auditable, require_expected_revision,
                           require_plan_digest, revision_conflict, run_preflight)
from .admin_diff import ResponseView, canonical, diff_digest, diff_states, json_digest, redact, render_diff
from .admin_gates import cross_account, require_no_active_battle
from .admin_offline import CROSS_ACCOUNT_PHRASE, MAX_PATH_TEXT, require_clock_marker
from .admin_registry import AdminRoute, RouteRegistry
from .local_io import atomic_replace, checked_path
from .storage import (ADMIN_ACTOR, ADMIN_PAGE_SIZE, AUTO_BACKUP_KEEP, AUTO_BACKUP_LIFETIME, MAX_BACKUP_REASON,
                      BackupSnapshot, PlayerProfile, admin_payload_digest, read_backup)


ROUTE_NAME = "backup"
MANUAL_BACKUP_KIND = "manual"
BACKUP_KIND = "backup"
RESTORE_KIND = "restore"
EXPORT_KIND = "export"
RESTORE_OPERATION = "admin.restore"
CROSS_ACCOUNT_OPERATION = "admin.restore.cross_account"
RESTORE_TARGETS = ("/",)
RESTORE_BACKUP_REASON = "before restore"
RESTORE_PHRASE = "RESTORE {revision}"
RESTORE_NOTICE = "restore"
BACKUP_SOURCE = "backup"
MAX_RESTORE_CONFIRMATIONS = 2
EXPORT_FORMAT = "aigis-ps-admin-export"
EXPORT_SCHEMA = 1
EXPORT_PREFIX = "admin-export"
EXPORT_SUFFIX = ".json"
EXPORT_INDENT = 2
TEMPORARY_SUFFIX = ".tmp"
RETENTION = MappingProxyType({"auto_keep": AUTO_BACKUP_KEEP,
                              "auto_lifetime_seconds": int(AUTO_BACKUP_LIFETIME.total_seconds()),
                              "manual_pruned": False})


class ManualBackupRequest(CommandParams):
    reason: str = Field(min_length=1, max_length=MAX_BACKUP_REASON)

    @field_validator("reason")
    @classmethod
    def _one_printable_line(cls, value: str) -> str:
        if not value.isprintable():
            raise ValueError("Backup reasons must be one printable line")
        return value


class RestorePreviewRequest(CommandParams):
    backup: str = Field(min_length=1, max_length=MAX_PATH_TEXT)


class RestoreRequest(RestorePreviewRequest):
    restore_digest: str
    expected_revision: int
    op_id: str
    client_closed: bool
    confirmations: list[ConfirmPhrase] = Field(default_factory=list, max_length=MAX_RESTORE_CONFIRMATIONS)


class ExportRequest(CommandParams):
    pass


def register(registry: RouteRegistry) -> None:
    registry.add(AdminRoute(ROUTE_NAME, build))


@dataclass(frozen=True)
class RestorePlan:
    source: BackupSnapshot
    current: PlayerProfile
    generation: LeasedGeneration
    entries: tuple[dict, ...]
    state_sha256_before: str
    state_sha256_after: str
    digest: str

    @classmethod
    def build(cls, source: BackupSnapshot, current: PlayerProfile, generation: LeasedGeneration) -> RestorePlan:
        entries = diff_states(current.state, source.profile.state)
        before, after = json_digest(current.state), json_digest(source.profile.state)
        summary = {"operation": RESTORE_OPERATION, "backup": str(source.path), "backup_sha256": source.sha256,
                   "backup_revision": source.profile.revision, "backup_player_id": source.profile.player_id,
                   "target_state_sha256": after, "expected_revision": current.revision, "state_sha256": before,
                   "player_id": current.player_id, "generation_id": generation.id, "diff_sha256": diff_digest(entries)}
        return cls(source, current, generation, entries, before, after, json_digest(summary))

    @property
    def target(self) -> dict:
        return self.source.profile.state

    @property
    def changed(self) -> bool:
        return self.state_sha256_before != self.state_sha256_after

    @property
    def cross_account(self) -> bool:
        return cross_account(self.current.player_id, self.source.profile.player_id)

    @property
    def revision_after(self) -> int:
        return self.current.revision + 1 if self.changed else self.current.revision

    def confirmations(self) -> tuple[Confirmation, ...]:
        restore = Confirmation(0, RESTORE_OPERATION, RESTORE_PHRASE.format(revision=self.current.revision))
        return (restore, Confirmation(1, CROSS_ACCOUNT_OPERATION, CROSS_ACCOUNT_PHRASE)) if self.cross_account else (restore,)

    def backup_view(self) -> dict:
        return {"path": str(self.source.path), "sha256": self.source.sha256, "revision": self.source.profile.revision,
                "player_id": self.source.profile.player_id}

    def describe(self) -> dict:
        return {"restore_digest": self.digest, "expected_revision": self.current.revision,
                "generation_id": self.generation.id, "player_id": self.current.player_id, "backup": self.backup_view(),
                "cross_account": self.cross_account, "state_sha256_before": self.state_sha256_before,
                "state_sha256_after": self.state_sha256_after, "auto_backup": self.changed,
                "confirmations": [item.describe() for item in self.confirmations()], "diff": render_diff(self.entries)}

    def record(self, op_id: str, report: PreflightReport, backup: dict | None) -> dict:
        source = self.backup_view()
        result, _ = canonical({
            "op_id": op_id, "kind": RESTORE_KIND, "plan_digest": self.digest, "generation_id": self.generation.id,
            "revision_before": self.current.revision, "revision_after": self.revision_after,
            "state_sha256_before": self.state_sha256_before, "state_sha256_after": self.state_sha256_after,
            "backup": source, "cross_account": self.cross_account, "backup_before_restore": backup,
            "readback": report.to_dict()["readback"], "client_restart_required": self.changed})
        return {"kind": RESTORE_KIND, "actor": ADMIN_ACTOR, "plan_digest": self.digest, "targets": list(RESTORE_TARGETS),
                "operations": [{"operation": RESTORE_OPERATION, "params": {
                    "backup": source["path"], "backup_sha256": source["sha256"], "backup_revision": source["revision"],
                    "cross_account": self.cross_account}}],
                "revision_before": self.current.revision, "revision_after": self.revision_after,
                "state_sha256_before": self.state_sha256_before, "state_sha256_after": self.state_sha256_after,
                "diff": render_diff(self.entries, AUDIT_DIFF_LIMITS), "generation_id": self.generation.id,
                "content_versions": dict(self.generation.content_versions), "result": result}


class RestoreFlow:
    def __init__(self, services: AdminServices):
        self._services = services

    def preview(self, backup: str) -> dict:
        services, path = self._services, self._path(backup)
        with services.lease() as generation:
            plan = self._plan(path, generation)
            preflight, report = attempt_preflight(lambda: self._preflight(plan))
            quests = getattr(generation.bundle, "quests", None)
            checks = {"energy_clock": attempt(lambda: require_clock_marker(plan.target, AdminError)),
                      "battle": attempt(lambda: require_no_active_battle(plan.target, source=BACKUP_SOURCE,
                                                                         quests=quests)),
                      "restart_check": attempt(lambda: services.restart_check.check(plan.target, generation)),
                      "preflight": preflight,
                      "audit": preview_audit(report, lambda checked: plan.record(PREVIEW_OP_ID, checked, None))}
        return {**plan.describe(), "checks": checks, "client": services.gate.status(),
                "committable": all(check["ok"] for check in checks.values())}

    def commit(self, request: RestoreRequest) -> dict:
        services = self._services
        op_id = require_op_id(request.op_id)
        digest, revision = require_plan_digest(request.restore_digest), require_expected_revision(request.expected_revision)
        payload = admin_payload_digest(op_id, digest)
        replayed = recorded_outcome(services.store, op_id, payload)
        if replayed is not None:
            return replayed.to_dict()
        path = self._path(request.backup)
        with services.lease() as generation:
            plan = self._plan(path, generation)
            if plan.current.revision != revision:
                raise revision_conflict(plan.current.revision)
            if plan.digest != digest:
                raise AdminError("plan_changed", "The backup, the state or the generation changed; preview the restore again")
            require_clock_marker(plan.target, AdminError)
            services.gate.require_closed(request.client_closed)
            check_confirmations(plan.confirmations(), request.confirmations)
            require_no_active_battle(plan.target, source=BACKUP_SOURCE, quests=getattr(generation.bundle, "quests", None))
            services.restart_check.check(plan.target, generation)
            report = self._preflight(plan)
        with services.runtime.fence() as fenced:
            if fenced.id != plan.generation.id:
                raise AdminError("plan_stale", "The server generation changed; preview the restore again",
                                 details={"changed": ["generation_id"]})
            current = services.store.profile().revision
            if current != revision:
                raise revision_conflict(current)
            require_auditable(plan.record(op_id, report, None))
            # The copy prunes older automatic copies, possibly this restore's source, so it waits until nothing can refuse it.
            # Only one automatic backup is kept, so a restore that leaves the state as it is must not replace it.
            backup = (auto_backup(services.store, services.backup_directory, RESTORE_BACKUP_REASON, op_id)
                      if plan.changed else None)
            outcome = commit_audited(services.store, op_id, payload, revision, plan.target if plan.changed else None,
                                     plan.record(op_id, report, backup))
        if plan.changed and not outcome.replayed:
            services.gate.notice_restart(RESTORE_NOTICE)
        return outcome.to_dict()

    def _path(self, text: str) -> Path:
        candidate = Path(text)
        path = candidate if candidate.is_absolute() else self._services.data_dir / candidate
        try:
            live = path.exists() and os.path.samefile(path, self._services.store.path)
        except (OSError, ValueError):
            live = False
        if live:
            raise AdminError("admin_backup_path_invalid", "The live database cannot be restored onto itself",
                             HTTPStatus.BAD_REQUEST)
        return path

    def _plan(self, path: Path, generation: LeasedGeneration) -> RestorePlan:
        source = read_backup(path, root=self._services.data_dir)
        return RestorePlan.build(source, self._services.store.profile(), generation)

    def _preflight(self, plan: RestorePlan) -> PreflightReport:
        services = self._services
        return run_preflight(services.preflight, plan.current.state, plan.target, services.preflight_domains,
                             plan.generation)


def write_export(services: AdminServices) -> dict:
    profile, generation = services.store.profile(), services.current()
    created = generation.now().astimezone(timezone.utc).isoformat(timespec="seconds")
    document = {"format": EXPORT_FORMAT, "schema_version": EXPORT_SCHEMA, "importable": False, "created_at": created,
                "generation_id": generation.id, "player_id": profile.player_id, "revision": profile.revision,
                "state_sha256": json_digest(profile.state), "state": redact(profile.state, responses=ResponseView.MASKED)}
    content = (json.dumps(document, ensure_ascii=False, allow_nan=False, indent=EXPORT_INDENT, sort_keys=True)
               + "\n").encode("utf-8")
    try:
        directory = checked_path(services.export_directory, services.data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = checked_path(directory / f"{EXPORT_PREFIX}-{uuid.uuid4().hex}{EXPORT_SUFFIX}", directory)
        temporary = path.with_name(path.name + TEMPORARY_SUFFIX)
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
            atomic_replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, ValueError) as error:
        raise AdminError("admin_export_failed", "The export could not be written inside the data directory",
                         HTTPStatus.SERVICE_UNAVAILABLE) from error
    return {"path": str(path), "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
            "revision": profile.revision, "created_at": created}


def build(services: AdminServices) -> APIRouter:
    router, restores = APIRouter(), RestoreFlow(services)

    @router.get("/backups")
    def backups(cursor: Cursor = None, limit: PageSize = ADMIN_PAGE_SIZE) -> dict:
        return {**admin_page(services.store.list_backups(cursor, limit)), "retention": dict(RETENTION)}

    @router.post("/backups")
    def create_backup(request: Request) -> dict:
        with services.recording_failures(BACKUP_KIND, request):
            reason = request_body(request, ManualBackupRequest).reason
            return {"backup": services.store.backup(services.backup_directory, reason, kind=MANUAL_BACKUP_KIND)}

    @router.post("/restores/preview")
    def preview_restore(request: Request) -> dict:
        return restores.preview(request_body(request, RestorePreviewRequest).backup)

    @router.post("/restores")
    def restore(request: Request) -> dict:
        with services.recording_failures(RESTORE_KIND, request):
            return restores.commit(request_body(request, RestoreRequest))

    @router.post("/exports")
    def export(request: Request) -> dict:
        with services.recording_failures(EXPORT_KIND, request):
            request_body(request, ExportRequest)
            return {"export": write_export(services)}

    return router
