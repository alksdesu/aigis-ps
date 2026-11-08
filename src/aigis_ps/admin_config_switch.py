from __future__ import annotations

from argparse import Namespace
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, fields, replace
from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
import logging
from pathlib import Path
import threading
from types import MappingProxyType
from typing import Any

from fastapi import FastAPI

from .admin_app import AdminServices, LeasedGeneration, admin_page, server_time
from .admin_command import (AdminError, Confirmation, Preflight, PreflightReport, check_confirmations, error_payload,
                            require_op_id)
from .admin_commit import (AUDIT_DIFF_LIMITS, PREVIEW_OP_ID, CommitOutcome, InitializerRestartCheck, auto_backup,
                           commit_audited, preview_audit, recorded_outcome, require_auditable, require_plan_digest,
                           revision_conflict, run_preflight)
from .admin_diff import canonical, diff_states, json_digest, render_diff, same
from .admin_gates import require_no_active_battle
from .config_registry import REGISTRY, ConfigError, ConfigKey, ConfigRegistry, TimeSemantics
from .config_sets import (SET_ID_LENGTH, ActivePointer, ConfigSet, ConfigSets, ConfigSource, ResolvedConfig,
                          ResolvedEntry, config_identity)
from .errors import ProtocolError, caught
from .generation import Builder, BuildState, Generation, GenerationInputs, GenerationRuntime
from .login_cooking_state import LEDGER_KEY as COOKING_LEDGER, migrate_cooking, read_cooking
from .login_reward_state import LEDGER_KEY as LOGIN_LEDGER, read_login_rewards, rebase_beginner
from .masterdata import MasterDataStore, MasterPointer, is_sha256, is_snapshot_id
from .quest_economy import settle_energy
from .storage import ADMIN_ACTOR, Store, admin_payload_digest
from .tower_state import DOMAIN as TOWER_STATE
from .tower_trade_state import PRODUCT_REUSED as TOWER_PRODUCT_REUSED


log = logging.getLogger("aigis_ps")
CONFIG_KIND = "config"
SET_SOURCE = "console"
SWITCH_OPERATION = "config.switch"
UNSTAGE_OPERATION = "config.unstage"
CONTENT_OPERATION = "config.switch.content"
CONTENT_PHRASE = "APPLY CONTENT {revision}"
STAGED_TIER = "staged"
UNSTAGED_TIER = "unstaged"
BATTLE_SOURCE = "player state"
BACKUP_REASON = "before {tier} switch to config set {config_set}"
ADMIN_STATE = "admin"
SWITCH_STATE = "config_switch"
CANDIDATE_MISSING = "generation_candidate_missing"
MASTER_TARGET = "masterdata"
MASTER_ACTIVATION = "master_activation"
STAGE_UNSUPPORTED = "config_stage_unsupported"
REPORT_CAPACITY = 8
HISTORY_LIMIT = 20
MAX_CONFIRMATIONS = 1
EMPTY_SET = config_identity({})[:SET_ID_LENGTH]
PREPARE_CLIENT_SCRIPT = "client/prepare-client.ps1"
# The client reads these from its prepared files, and code only reloads with the process, so both need a restart.
CONTENT_FIELDS = frozenset(("code_digest", "client_fingerprint", "master_snapshot", "base_resource_version",
                            "effective_resource_version"))
AVAILABILITY_FIELDS = ("ready", "available", "startup_ready")
SERVE_GATE, MASTER_PIN, MIGRATION = "serve_gate", "master_pin", "migration"
RESTART_CHECK, PREFLIGHT = "restart_check", "preflight"
CHECKS = (SERVE_GATE, MASTER_PIN, MIGRATION, RESTART_CHECK, PREFLIGHT)
AUDIT = "audit"
CANDIDATE_RESTART_CHECK = InitializerRestartCheck()
_MISSING = object()


class SwitchTier(StrEnum):
    HOT = "hot"
    GATED = "gated"
    CONTENT = "content"


class SwitchMode(StrEnum):
    APPLY = "apply"
    STAGE = "stage"


Migration = Callable[[dict, Generation, Any, datetime], dict]


def settle_charisma(state: dict, generation: Generation, rate: Any, now: datetime) -> dict:
    return settle_energy(state, generation.bundle.bootstrap.rank_rows, now, recovery_seconds=rate)


def settle_towers(state: dict, generation: Generation, _hour: Any, now: datetime) -> dict:
    towers = generation.bundle.towers
    # Tower clocks start at the player's first tower request, so without the service or saved progress none accrued.
    return state if towers is None or TOWER_STATE not in state else towers.settle(state, now)[0]


# Time accrued so far belongs to the previous rule, so the running generation settles it before the switch.
ACCUMULATING_MIGRATIONS: Mapping[tuple[str, str], Migration] = MappingProxyType({
    ("quest-rules", "charisma_recovery_seconds"): settle_charisma,
    ("tower-rules", "generation_hour"): settle_towers,
    ("tower-rules", "replenish_hour"): settle_towers,
})


def running_set(config: ResolvedConfig) -> str:
    return EMPTY_SET if config.config_set is None else config.config_set.identifier


def command_line(config: ResolvedConfig) -> Namespace:
    return Namespace(**{entry.spec.dest: entry.path for entry in config.entries.values()
                        if entry.source is ConfigSource.CLI})


def latest_switch(store: Store) -> dict | None:
    items = store.list_generations(limit=1).items
    return dict(items[0]) if items else None


def raise_first(errors: Sequence[ProtocolError]) -> None:
    if errors:
        raise errors[0]


def check_view(error: ProtocolError | None, **extra: Any) -> dict:
    return {"ok": True, **extra} if error is None else {"ok": False, "error": error_payload(error)}


def config_failure(error: ConfigError) -> AdminError:
    fields = () if error.key is None else ({"loc": ["changes", error.key], "type": error.code, "msg": str(error)},)
    details = {name: value for name, value in (("key", error.key), ("pointer", error.pointer)) if value is not None}
    return AdminError(error.code, str(error), error.status, fields=fields, details=details)


@contextmanager
def writing_sets() -> Iterator[None]:
    try:
        yield
    except ConfigError as error:
        raise config_failure(error) from error
    except OSError as error:
        raise AdminError("config_set_unwritable", "The configuration set could not be written in the data directory",
                         HTTPStatus.SERVICE_UNAVAILABLE) from error


def stale(changed: Sequence[str]) -> AdminError:
    return AdminError("config_report_stale", "The running generation, a pointer or the saved state changed; "
                      "validate again", details={"changed": list(changed)})


@dataclass(frozen=True)
class PointerState:
    digest: str | None
    active: ActivePointer | None
    error: ConfigError | None = None

    @classmethod
    def read(cls, sets: ConfigSets) -> PointerState:
        digest = sets.pointer_digest()
        try:
            return cls(digest, sets.read_active())
        except ConfigError as error:
            return cls(digest, None, error)

    @property
    def set_id(self) -> str | None:
        if self.error is not None:
            return None
        return EMPTY_SET if self.active is None else self.active.set_id

    @property
    def sequence(self) -> int | None:
        return None if self.active is None else self.active.sequence

    def behind(self, latest: Mapping[str, Any] | None) -> bool:
        return latest is not None and (self.sequence is None or self.sequence < latest["sequence"])

    def next_start(self, latest: Mapping[str, Any] | None) -> str | None:
        # A start first rolls the pointer forward to the newest committed switch.
        return latest["config_set"] if self.behind(latest) else self.set_id

    def describe(self, latest: Mapping[str, Any] | None = None) -> dict:
        return {"digest": self.digest, "active": None if self.active is None else self.active.describe(),
                "set": self.set_id, "error": None if self.error is None else error_payload(self.error),
                "roll_forward_pending": self.behind(latest), "next_start": self.next_start(latest)}

    @staticmethod
    def unreadable(error: ConfigError) -> dict:
        # A pointer without a digest can be neither replaced nor resolved, so the next start is unknown.
        return {"digest": None, "active": None, "set": None, "error": error_payload(error),
                "roll_forward_pending": None, "next_start": None}


def staged_set(pointer: PointerState, latest: Mapping[str, Any] | None, running: str) -> str | None:
    following = pointer.next_start(latest)
    return None if following is None or following == running else following


@dataclass(frozen=True)
class ContentInputs(GenerationInputs):
    # Builders read the snapshot from here, so a candidate never picks up a pointer moved after the running build.
    master_snapshot: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.master_snapshot is not None and (not isinstance(self.master_snapshot, str) or not self.master_snapshot):
            raise ValueError("Generation inputs name the master snapshot by a non-empty identifier or None")

    @classmethod
    def pinned(cls, inputs: GenerationInputs, *, config: ResolvedConfig, master_snapshot: str | None) -> ContentInputs:
        values = {item.name: getattr(inputs, item.name) for item in fields(GenerationInputs)}
        return cls(**{**values, "config": config, "master_snapshot": master_snapshot})

    def describe(self) -> dict:
        return {**super().describe(), "master_snapshot": self.master_snapshot}


@dataclass(frozen=True)
class MasterActivation:
    target: str
    before: str | None
    pointer_digest: str | None

    @classmethod
    def recorded(cls, value: object) -> MasterActivation | None:
        if not isinstance(value, Mapping):
            return None
        target, before, digest = value.get("target"), value.get("before"), value.get("pointer_digest")
        if (not is_snapshot_id(target) or (before is not None and not is_snapshot_id(before))
                or (digest is not None and not is_sha256(digest))):
            return None
        return cls(target, before, digest)

    def describe(self) -> dict:
        return {"target": self.target, "before": self.before, "pointer_digest": self.pointer_digest}


@dataclass(frozen=True)
class MasterState:
    in_use: str | None
    pointer: MasterPointer | None
    digest: str | None
    error: ProtocolError | None = None
    pending: MasterActivation | None = None

    @classmethod
    def read(cls, masters: MasterDataStore, in_use: str | None, recorded: MasterActivation | None) -> MasterState:
        try:
            digest = masters.pointer_digest()
        except ProtocolError as error:
            return cls(in_use, None, None, error)
        try:
            pointer, failure = masters.pointer(), None
        except ProtocolError as error:
            pointer, failure = None, error
        disk = None if pointer is None else pointer.snapshot
        # A committed activation whose pointer still holds the bytes it replaced was never published.
        pending = (recorded if recorded is not None and recorded.pointer_digest == digest and recorded.target != disk
                   else None)
        return cls(in_use, pointer, digest, failure, pending)

    @property
    def disk(self) -> str | None:
        return None if self.pointer is None else self.pointer.snapshot

    @property
    def unreadable(self) -> bool:
        return self.error is not None and self.digest is None

    @property
    def mark(self) -> tuple[str | None, str | None]:
        return self.digest, None if self.error is None else self.error.code

    @property
    def next_start(self) -> str | None:
        return self.disk if self.pending is None else self.pending.target

    @property
    def staged(self) -> str | None:
        following = self.next_start
        return None if following is None or following == self.in_use else following

    def activation_to(self, target: str | None) -> MasterActivation | None:
        return None if target is None or target == self.disk else MasterActivation(target, self.disk, self.digest)

    def unstaging(self, staged: str) -> MasterActivation | None:
        waiting = self.staged
        if waiting is None:
            raise AdminError("masterdata_not_staged", "No staged master snapshot waits for the next start")
        if waiting != staged:
            raise AdminError("masterdata_staged_changed", "A different master snapshot is staged now",
                             details={"staged_master": waiting})
        if self.in_use is None:
            raise AdminError("masterdata_unstage_unsupported",
                             "The running generation has no master snapshot to point the next start back to")
        return self.activation_to(self.in_use)

    def describe(self) -> dict:
        return {"in_use": self.in_use, "disk": None if self.pointer is None else self.pointer.describe(),
                "error": None if self.error is None else error_payload(self.error),
                "roll_forward_pending": self.pending is not None, "next_start": self.next_start,
                "staged": self.staged}


@dataclass(frozen=True)
class MasterPlan:
    state: MasterState
    requested: str | None

    @property
    def target(self) -> str | None:
        # Without a request the pointer keeps what the next start uses, which republishes an unpublished activation.
        return self.state.next_start if self.requested is None else self.requested

    @property
    def activation(self) -> MasterActivation | None:
        return self.state.activation_to(self.target)

    @property
    def waiting(self) -> str | None:
        staged = self.state.staged
        return None if staged is None or staged == self.requested else staged

    @property
    def involved(self) -> bool:
        return self.requested is not None or self.activation is not None

    def describe(self) -> dict:
        activation = self.activation
        return {**self.state.describe(), "requested": self.requested, "target": self.target,
                "activation": None if activation is None else activation.describe()}


def recorded_activation(store: Store, latest: Mapping[str, Any] | None) -> MasterActivation | None:
    op_id = None if latest is None else latest.get("op_id")
    record = None if op_id is None else store.find_admin_operation(op_id)
    result = record.get("result") if isinstance(record, Mapping) else None
    return MasterActivation.recorded(result.get(MASTER_ACTIVATION) if isinstance(result, Mapping) else None)


def master_state(store: Store, masters: MasterDataStore, in_use: str | None) -> MasterState:
    return MasterState.read(masters, in_use, recorded_activation(store, latest_switch(store)))


def roll_master_forward(store: Store, masters: MasterDataStore, latest: Mapping[str, Any] | None) -> dict:
    activation = recorded_activation(store, latest)
    view = {"rolled_forward": False, "activation": None if activation is None else activation.describe()}
    if activation is None or masters.pointer_digest() != activation.pointer_digest:
        return view
    pointer = masters.activate(activation.target, expected=activation.pointer_digest)
    log.info("Rolled the master snapshot pointer forward to %s at switch %d", pointer.snapshot, latest["sequence"])
    return {**view, "rolled_forward": True, "pointer": pointer.describe()}


@dataclass(frozen=True)
class KeyChange:
    before: ResolvedEntry
    after: ResolvedEntry
    rules_before: dict | None
    rules_after: dict | None
    version_before: str | None
    version_after: str | None

    @classmethod
    def between(cls, before: ResolvedEntry, after: ResolvedEntry) -> KeyChange:
        spec, old, new = after.spec, before.load(), after.load()
        return cls(before, after, spec.normalize(old), spec.normalize(new), spec.content_version(old),
                   spec.content_version(new))

    @property
    def spec(self) -> ConfigKey:
        return self.after.spec

    @property
    def key(self) -> str:
        return self.spec.key

    def value(self, name: str, *, after: bool = False) -> Any:
        return ((self.rules_after if after else self.rules_before) or {}).get(name)

    @property
    def accumulating(self) -> tuple[str, ...]:
        old, new = self.rules_before or {}, self.rules_after or {}
        return tuple(sorted(name for name in old.keys() | new.keys()
                            if old.get(name, _MISSING) != new.get(name, _MISSING)
                            and self.spec.semantics_for(name) is TimeSemantics.TIME_ACCUMULATING))

    def describe(self) -> dict:
        return {"key": self.key, "domains": list(self.spec.domains), "resource_hint": self.spec.resource_hint,
                "source": {"before": self.before.source, "after": self.after.source},
                "mode": {"before": self.before.mode, "after": self.after.mode},
                "rules": {"before": self.rules_before, "after": self.rules_after},
                "content_version": {"before": self.version_before, "after": self.version_after},
                "semantics": sorted(self.spec.changed_semantics(self.rules_before, self.rules_after))}


def key_changes(running: ResolvedConfig, proposed: ResolvedConfig) -> tuple[KeyChange, ...]:
    return tuple(KeyChange.between(running.entries[key], entry) for key, entry in proposed.entries.items()
                 if (running.entries[key].source, running.entries[key].sha256) != (entry.source, entry.sha256))


def available(status: object) -> bool | None:
    if isinstance(status, bool):
        return status
    if isinstance(status, Mapping):
        return next((status[name] for name in AVAILABILITY_FIELDS if isinstance(status.get(name), bool)), None)
    return None


def availability(before: Mapping[str, object], after: Mapping[str, object]) -> dict:
    changes = {domain: {"before": available(before.get(domain)), "after": available(after.get(domain))}
               for domain in sorted(before.keys() | after.keys())}
    changed = {domain: value for domain, value in changes.items() if value["before"] != value["after"]}
    return {"regressions": [domain for domain, value in changed.items()
                            if value["before"] is True and value["after"] is not True], "changes": changed}


def content_changes(current: Generation, candidate: Generation) -> dict:
    before, after = current.identity.content_versions, candidate.identity.content_versions
    return {domain: {"before": before.get(domain), "after": after.get(domain)}
            for domain in sorted(before.keys() | after.keys()) if before.get(domain) != after.get(domain)}


def powershell_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def client_actions(tier: SwitchTier, config: ResolvedConfig, config_set: ConfigSet) -> dict | None:
    if tier is not SwitchTier.CONTENT:
        return None
    arguments = [{"name": entry.spec.ps_parameter, "value": str(entry.path)} for entry in config.entries.values()
                 if entry.spec.resource_hint and entry.path is not None]
    command = " ".join((PREPARE_CLIENT_SCRIPT, *(f"-{item['name']} {powershell_literal(item['value'])}"
                                                 for item in arguments)))
    return {"restart_client": True, "config_set": str(config_set.directory),
            "prepare_client": {"script": PREPARE_CLIENT_SCRIPT, "arguments": arguments, "command": command}}


def confirmations_for(tier: SwitchTier, mode: SwitchMode, revision: int) -> tuple[Confirmation, ...]:
    if mode is SwitchMode.APPLY and tier is SwitchTier.CONTENT:
        return (Confirmation(0, CONTENT_OPERATION, CONTENT_PHRASE.format(revision=revision)),)
    return ()


def switch_digest(report_id: str, mode: SwitchMode | None) -> str:
    return json_digest({"operation": SWITCH_OPERATION, "report_id": report_id, "mode": mode})


def settle(keys: Sequence[KeyChange], state: dict, generation: Generation, now: datetime) -> tuple[dict, tuple[dict, ...]]:
    settled, applied = state, []
    for change in keys:
        for name in change.accumulating:
            migration = ACCUMULATING_MIGRATIONS.get((change.key, name))
            if migration is None:
                raise AdminError("config_migration_unsupported",
                                 f"No migration settles the time accrued under {change.key}.{name}",
                                 details={"key": change.key, "field": name})
            settled, _ = canonical(migration(deepcopy(settled), generation, change.value(name), now))
            applied.append({"key": change.key, "field": name, "before": change.value(name),
                            "after": change.value(name, after=True), "settled_at": server_time(now)})
    return settled, tuple(applied)


def migrate_cooking_ledger(state: dict, candidate: Generation, now: datetime) -> tuple[dict, dict | None]:
    login_rewards = getattr(candidate.bundle, "login_rewards", None)
    if login_rewards is None or COOKING_LEDGER not in state:
        return state, None
    before, border = read_cooking(state), login_rewards.cooking_border
    if before["border"] == border:
        return state, None
    # 115 refuses a ledger frozen at another border, so the switch carries the explicit migration.
    migrated, _ = canonical(migrate_cooking(state, now, border=border))
    after = read_cooking(migrated)
    return migrated, {"period": after["period"], "issued": after["issued"],
                      "border": {"before": before["border"], "after": after["border"]},
                      "quota": {"before": before["quota"], "after": after["quota"]}, "migrated_at": server_time(now)}


def rebase_beginner_ledger(state: dict, candidate: Generation) -> tuple[dict, dict | None]:
    login_rewards = getattr(candidate.bundle, "login_rewards", None)
    if login_rewards is None or LOGIN_LEDGER not in state:
        return state, None
    before, total = read_login_rewards(state), login_rewards.beginner_total
    if before["beginner_total"] == total:
        return state, None
    # 115 refuses a ledger frozen at another beginner total, so the switch carries the explicit migration.
    rebased, _ = canonical(rebase_beginner(state, beginner_count=total))
    after = read_login_rewards(rebased)
    return rebased, {"total": {"before": before["beginner_total"], "after": after["beginner_total"]},
                     "claimed": {"before": before["beginner_claimed"], "after": after["beginner_claimed"]}}


def candidate_notices(candidate: Generation, state: dict) -> tuple[dict, ...]:
    tower_trades = getattr(candidate.bundle, "tower_trades", None)
    if tower_trades is None:
        return ()
    try:
        report = tower_trades.reuse_report(state)
    except ProtocolError:
        # The candidate initializer already read this history, so its refusal belongs to the restart check.
        return ()
    return ({"code": TOWER_PRODUCT_REUSED, **report},) if report["reused_products"] else ()


def initialized(candidate: Generation, state: dict) -> dict:
    try:
        result, _ = canonical(candidate.bundle.initialize(deepcopy(state)))
        if not isinstance(result, dict):
            raise TypeError("Player-state initializers return an object")
    except Exception as error:
        raise AdminError("admin_restart_incompatible", "The candidate generation would reject this state",
                         details={"error": error_payload(error)}) from error
    return result


@dataclass(frozen=True)
class Assessment:
    tier: SwitchTier
    identity: tuple[str, ...]
    keys: tuple[KeyChange, ...]
    energy: tuple[dict, ...]
    cooking: dict | None
    beginner: dict | None
    notices: tuple[dict, ...]
    rewritten: bool
    target: dict
    entries: tuple[dict, ...]
    state_sha256_before: str
    state_sha256_after: str
    checks: Mapping[str, ProtocolError | None]
    readback: PreflightReport | None
    battle: ProtocolError | None

    @property
    def changed(self) -> bool:
        return self.state_sha256_before != self.state_sha256_after

    @property
    def accumulating(self) -> tuple[str, ...]:
        return tuple(f"{change.key}.{name}" for change in self.keys for name in change.accumulating)

    @property
    def battle_required(self) -> bool:
        return bool(self.accumulating) or self.tier is SwitchTier.CONTENT

    @property
    def problems(self) -> tuple[ProtocolError, ...]:
        return tuple(error for error in self.checks.values() if error is not None)

    def describe(self, audit: Mapping[str, Any]) -> dict:
        readback = {} if self.readback is None else {"readback": self.readback.to_dict()["readback"]}
        return {"tier": self.tier, "identity_changes": list(self.identity),
                "reasons": {"identity": sorted(CONTENT_FIELDS.intersection(self.identity)),
                            "accumulating": list(self.accumulating), "initialize": self.rewritten,
                            "cooking": self.cooking is not None, "beginner": self.beginner is not None},
                "keys": [change.describe() for change in self.keys],
                "migration": {"changed": self.changed, "energy": list(self.energy), "cooking": self.cooking,
                              "beginner": self.beginner,
                              "diff": render_diff(self.entries), "state_sha256_before": self.state_sha256_before,
                              "state_sha256_after": self.state_sha256_after},
                "checks": {**{name: check_view(error, **(readback if name == PREFLIGHT else {}))
                              for name, error in self.checks.items()}, AUDIT: dict(audit)},
                "battle": {"in_progress": self.battle is not None, "required": self.battle_required,
                           "error": None if self.battle is None else error_payload(self.battle)},
                "notices": list(self.notices)}


def assess(current: Generation, candidate: Generation, running: ResolvedConfig, proposed: ResolvedConfig, state: dict,
           now: datetime, *, preflight: Preflight, domains: frozenset[str], pinned: str | None = None) -> Assessment:
    keys = key_changes(running, proposed)
    original, before = canonical(state)
    leased = LeasedGeneration(candidate)
    checks: dict[str, ProtocolError | None] = dict.fromkeys(CHECKS)
    if candidate.gate_issues:
        checks[SERVE_GATE] = AdminError("config_candidate_blocked", "The server would refuse to start with this configuration",
                                        details={"gate_issues": list(candidate.gate_issues)})
    if pinned is not None and candidate.identity.master_snapshot != pinned:
        # A builder that ignores the pin would swap in one snapshot while the pointer names another.
        checks[MASTER_PIN] = AdminError("config_candidate_master_mismatch",
                                        "The candidate generation was not built from the pinned master snapshot",
                                        details={"pinned": pinned, "built": candidate.identity.master_snapshot})
    try:
        settled, energy = settle(keys, original, current, now)
    except ProtocolError as error:
        settled, energy, checks[MIGRATION] = original, (), error
    cooking = beginner = None
    if checks[MIGRATION] is None:
        try:
            settled, cooking = migrate_cooking_ledger(settled, candidate, now)
        except ProtocolError as error:
            checks[MIGRATION] = error
    if checks[MIGRATION] is None:
        try:
            settled, beginner = rebase_beginner_ledger(settled, candidate)
        except ProtocolError as error:
            checks[MIGRATION] = error
    notices: tuple[dict, ...] = ()
    try:
        target = initialized(candidate, settled)
    except ProtocolError as error:
        target, checks[RESTART_CHECK] = settled, error
    else:
        checks[RESTART_CHECK] = caught(lambda: CANDIDATE_RESTART_CHECK.check(target, leased))
        notices = candidate_notices(candidate, target)
    readback = None
    try:
        readback = run_preflight(preflight, original, target, domains, leased)
    except ProtocolError as error:
        checks[PREFLIGHT] = error
    identity = current.identity.differences(candidate.identity)
    rewritten = not same(target, settled)
    accumulating = any(change.accumulating for change in keys)
    migrated = cooking is not None or beginner is not None
    tier = (SwitchTier.CONTENT if CONTENT_FIELDS.intersection(identity)
            else SwitchTier.GATED if accumulating or rewritten or migrated else SwitchTier.HOT)
    return Assessment(tier=tier, identity=identity, keys=keys, energy=energy, cooking=cooking, beginner=beginner,
                      notices=notices, rewritten=rewritten, target=target, entries=diff_states(original, target),
                      state_sha256_before=before, state_sha256_after=json_digest(target),
                      checks=MappingProxyType(checks), readback=readback,
                      battle=caught(lambda: require_no_active_battle(original, source=BATTLE_SOURCE)))


def pointer_blockers(mode: SwitchMode, *, config_set: str, staged: str | None, pointer_set: str | None, current_id: str,
                     candidate_id: str, master: MasterPlan) -> list[ProtocolError]:
    found: list[ProtocolError] = []
    if staged is not None and staged != config_set:
        found.append(AdminError("config_staged_pending", "A staged configuration waits for the next start; apply it or "
                                "undo the staging first", details={"staged_set": staged}))
    if master.waiting is not None:
        found.append(AdminError("config_staged_pending", "A staged master snapshot waits for the next start; apply it "
                                "or undo the staging first", details={"staged_master": master.waiting}))
    if master.activation is not None and master.state.unreadable:
        found.append(master.state.error)
    if mode is SwitchMode.APPLY and candidate_id == current_id:
        found.append(AdminError("config_unchanged", "The candidate generation is the running generation"))
    if mode is SwitchMode.STAGE and pointer_set == config_set and master.activation is None:
        found.append(AdminError("config_unchanged", "The active pointers already name this configuration set and "
                                "master snapshot"))
    return found


def assessment_blockers(assessment: Assessment, mode: SwitchMode) -> list[ProtocolError]:
    found = list(assessment.problems)
    if mode is SwitchMode.STAGE and assessment.accumulating:
        found.append(AdminError(STAGE_UNSUPPORTED, "Time-accumulating rules switch only while the server runs, "
                                "so the time accrued so far is settled under the previous rule",
                                details={"fields": list(assessment.accumulating)}))
    if mode is SwitchMode.STAGE and assessment.cooking is not None:
        found.append(AdminError(STAGE_UNSUPPORTED, "Cooking border changes switch only while the server runs, "
                                "so the saved cooking ledger migrates together with the switch",
                                details={"border": dict(assessment.cooking["border"])}))
    if mode is SwitchMode.STAGE and assessment.beginner is not None:
        found.append(AdminError(STAGE_UNSUPPORTED, "Beginner total changes switch only while the server runs, "
                                "so the saved login reward ledger rebases together with the switch",
                                details={"total": dict(assessment.beginner["total"])}))
    if mode is SwitchMode.APPLY and assessment.battle_required and assessment.battle is not None:
        found.append(assessment.battle)
    return found


@dataclass(frozen=True)
class Validation:
    job_id: str
    op_id: str
    request_digest: str
    config_set: ConfigSet
    base_set: str | None
    keys: tuple[str, ...]
    inputs: GenerationInputs
    master_snapshot: str | None = None

    @property
    def pinned(self) -> str | None:
        return self.inputs.master_snapshot if isinstance(self.inputs, ContentInputs) else None

    def describe(self) -> dict:
        return {"job_id": self.job_id, "op_id": self.op_id, "config_set": self.config_set.describe(),
                "directory": str(self.config_set.directory), "base_set": self.base_set, "keys": list(self.keys),
                "master_snapshot": self.master_snapshot}


@dataclass(frozen=True)
class SwitchReport:
    report_id: str
    fingerprint: tuple
    validation: Validation
    candidate_id: str
    current_id: str
    running_set: str
    pointer_digest: str | None
    revision: int
    tier: SwitchTier
    keys: tuple[str, ...]
    content_versions: Mapping[str, Mapping[str, str | None]]
    current_versions: Mapping[str, str]
    client_actions: Mapping[str, Any] | None
    view: Mapping[str, Any]
    master: MasterPlan

    @classmethod
    def create(cls, *, validation: Validation, current: Generation, candidate: Generation, running: ResolvedConfig,
               pointer: PointerState, latest: Mapping[str, Any] | None, master: MasterPlan, revision: int,
               assessment: Assessment, now: datetime, fingerprint: tuple,
               audit: Callable[[SwitchReport, Assessment], Mapping[str, Any]]) -> SwitchReport:
        set_id, running_id, tier = validation.config_set.identifier, running_set(running), assessment.tier
        staged = staged_set(pointer, latest, running_id)
        versions = content_changes(current, candidate)
        actions = client_actions(tier, validation.inputs.config, validation.config_set)
        report_id = json_digest({"job_id": validation.job_id, "config_set": set_id, "candidate_id": candidate.id,
                                 "current_id": current.id, "running_set": running_id, "pointer_digest": pointer.digest,
                                 "revision": revision, "state_sha256": assessment.state_sha256_before, "tier": tier,
                                 MASTER_TARGET: {"requested": master.requested, "mark": master.state.mark}})
        report = cls(report_id, fingerprint, validation, candidate.id, current.id, running_id, pointer.digest, revision,
                     tier, tuple(change.key for change in assessment.keys), MappingProxyType(versions),
                     MappingProxyType(dict(current.identity.content_versions)), actions, MappingProxyType({}), master)
        # The audit record names the report, so it is measured before the view that shows it.
        measured = audit(report, assessment)
        # Applying and staging both refuse a record the store cannot keep.
        refused = [measured["error"]] if "error" in measured else []
        modes = {}
        for mode in SwitchMode:
            errors = [*pointer_blockers(mode, config_set=set_id, staged=staged, pointer_set=pointer.set_id,
                                        current_id=current.id, candidate_id=candidate.id, master=master),
                      *assessment_blockers(assessment, mode)]
            blockers = [*(error_payload(error) for error in errors), *refused]
            modes[mode] = {"allowed": not blockers, "blockers": blockers,
                           "confirmations": [item.describe() for item in confirmations_for(tier, mode, revision)]}
        view, _ = canonical({
            "report_id": report_id, "job_id": validation.job_id, "created_at": server_time(now), "revision": revision,
            "validation": validation.describe(), "running_set": running_id, "pointer": pointer.describe(latest),
            "staged": None if staged is None else {"set": staged, "is_candidate": staged == set_id},
            MASTER_TARGET: master.describe(),
            "generations": {"current": {"id": current.id, "identity": current.identity.to_dict()},
                            "candidate": {"id": candidate.id, "identity": candidate.identity.to_dict(),
                                          "gate_issues": list(candidate.gate_issues)}},
            **assessment.describe(measured),
            "default_mode": report.default_mode, "modes": modes,
            "requirements": {"backup": tier is not SwitchTier.HOT and assessment.changed,
                             "fence": tier is not SwitchTier.HOT, "client_closed": tier is SwitchTier.CONTENT},
            "overridden": list(running.overridden), "content_versions": versions, "receipts_invalidated": sorted(versions),
            "availability": availability(current.readiness, candidate.readiness), "client_actions": actions})
        return replace(report, view=view)

    @property
    def config_set(self) -> str:
        return self.validation.config_set.identifier

    @property
    def default_mode(self) -> SwitchMode:
        return SwitchMode.STAGE if self.tier is SwitchTier.CONTENT else SwitchMode.APPLY

    @property
    def backup_reason(self) -> str:
        return BACKUP_REASON.format(tier=self.tier, config_set=self.config_set)

    def params(self, mode: SwitchMode) -> dict:
        return {"mode": mode, "tier": self.tier, "config_set": self.config_set, "previous_set": self.running_set,
                "base_set": self.validation.base_set, "report_id": self.report_id, "keys": list(self.keys),
                "master_snapshot": self.master.requested}


def audit_record(*, operation: str, params: Mapping[str, Any], targets: Sequence[str], digest: str, generation_id: str,
                 content_versions: Mapping[str, str], switch: Mapping[str, Any], result: dict,
                 entries: tuple[dict, ...] = (), claims: Mapping[str, Any] | None = None) -> dict:
    return {"kind": CONFIG_KIND, "actor": ADMIN_ACTOR, "plan_digest": digest,
            "operations": [{"operation": operation, "params": dict(params)}], "targets": list(targets),
            "diff": render_diff(entries, AUDIT_DIFF_LIMITS), "generation_id": generation_id,
            "content_versions": dict(content_versions), "switch": dict(switch), "result": result, **(claims or {})}


@dataclass(frozen=True)
class _Running:
    inputs: GenerationInputs
    generation_id: str


class ConfigSwitch:
    def __init__(self, services: AdminServices, *, inputs: GenerationInputs, builder: Builder,
                 registry: ConfigRegistry = REGISTRY):
        if not isinstance(services, AdminServices):
            raise ValueError("Configuration switching needs the admin console services")
        if not isinstance(inputs, GenerationInputs) or not callable(builder):
            raise ValueError("Configuration switching needs the running generation inputs and its builder")
        if Path(inputs.data_dir).resolve() != services.data_dir:
            raise ValueError("The running generation must use the admin data directory")
        if inputs.config_digest != services.runtime.current.identity.config_set:
            raise ValueError("Configuration switching needs the inputs the running generation was built from")
        self._services, self._builder = services, builder
        self._sets = ConfigSets(services.data_dir, registry=registry)
        self._masters = MasterDataStore.for_data_dir(services.data_dir)
        self._running = _Running(inputs, services.runtime.current.id)
        self._validation: Validation | None = None
        self._reports: OrderedDict[str, SwitchReport] = OrderedDict()
        self._notice: dict | None = None
        self._guard = threading.Lock()
        self._switching = threading.Lock()

    @property
    def _runtime(self) -> GenerationRuntime:
        return self._services.runtime

    @property
    def _store(self) -> Store:
        return self._services.store

    def describe(self) -> dict:
        running = self._running_snapshot()
        config, target = running.inputs.config, running_set(running.inputs.config)
        latest = latest_switch(self._store)
        try:
            pointer = PointerState.read(self._sets)
        except ConfigError as error:
            pointer, active = None, PointerState.unreadable(error)
        else:
            active = pointer.describe(latest)
        staged = None if pointer is None else staged_set(pointer, latest, target)
        with self._guard:
            validation = self._validation
        view = config.describe()
        view.pop("active")
        current = self._runtime.current
        return {"running": {"set": target, "generation_id": running.generation_id, "config": view},
                "current_generation_id": current.id, "active": active,
                "staged": None if staged is None else {"set": staged, "keys": self._differences(target, staged)},
                MASTER_TARGET: self._master_plan(current, latest, None).state.describe(),
                "history": admin_page(self._store.list_generations(limit=HISTORY_LIMIT)),
                "validation": None if validation is None else self._validation_view(validation),
                "client_warning": self._client_warning(), "switching": self._switching.locked()}

    def validate(self, changes: Mapping[str, Mapping[str, Any] | None], *, base_set: str | None, op_id: str,
                 master_snapshot: str | None = None) -> dict:
        op_id = require_op_id(op_id)
        try:
            request = {"changes": dict(changes), "base_set": base_set}
            if master_snapshot is not None:
                request["master_snapshot"] = master_snapshot
            request_digest = json_digest(request)
        except (TypeError, ValueError) as error:
            raise AdminError("config_changes_invalid", "Configuration changes map keys to JSON objects or null",
                             HTTPStatus.UNPROCESSABLE_ENTITY) from error
        with self._guard:
            latest = self._validation
        if latest is not None and latest.op_id == op_id:
            if latest.request_digest != request_digest:
                raise AdminError("admin_operation_conflict", "This operation ID was already used for a different validation")
            return self._validation_view(latest)
        current = self._runtime.current
        running = self._running_for(current)
        config = running.inputs.config
        overridden = sorted(set(changes) & set(config.overridden))
        if overridden:
            raise AdminError("config_key_overridden", "Command-line rules files override these keys for this process",
                             fields=[{"loc": ["changes", key], "type": "config_key_overridden",
                                      "msg": "The key is read-only while a command-line file overrides it"}
                                     for key in overridden], details={"keys": overridden})
        if master_snapshot is not None:
            self._masters.snapshot(master_snapshot)
        if base_set is None and config.config_set is not None:
            base_set = config.config_set.identifier
        with writing_sets():
            config_set = self._sets.create(dict(changes), source=SET_SOURCE, base=base_set)
            proposed = self._sets.resolve_candidate(config_set.identifier, command_line(config))
        drifted = sorted(key for key in config.overridden if config.entries[key].sha256 != proposed.entries[key].sha256)
        if drifted:
            raise AdminError("config_override_changed", "Command-line rules files changed on disk; restart the server "
                             "to use them", details={"keys": drifted})
        pinned = current.identity.master_snapshot if master_snapshot is None else master_snapshot
        inputs = ContentInputs.pinned(running.inputs, config=proposed, master_snapshot=pinned)
        status = self._runtime.start_build(inputs, self._builder)
        validation = Validation(status["job_id"], op_id, request_digest, config_set, base_set, tuple(sorted(changes)),
                                inputs, master_snapshot)
        with self._guard:
            self._validation = validation
            self._reports.clear()
        return {"job": status, "validation": validation.describe()}

    def job(self, job_id: str) -> dict:
        with self._guard:
            validation = self._validation
        status = self._runtime.build_status()
        if validation is None or validation.job_id != job_id or status is None or status["job_id"] != job_id:
            raise AdminError("config_job_unknown", "This validation job is unknown or was replaced", HTTPStatus.NOT_FOUND)
        view = {"job": status, "validation": validation.describe(), "report": None, "candidate_expired": False}
        if status["state"] != BuildState.SUCCEEDED:
            return view
        candidate = self._runtime.candidate()
        if candidate is None or candidate.id != status["candidate_id"]:
            return {**view, "candidate_expired": True}
        # Cached reports stay valid for the same state, but the client gate changes with every game request.
        return {**view, "report": {**self._report(validation, candidate).view, "client": self._services.gate.status()}}

    def switch(self, *, report_id: str, op_id: str, mode: SwitchMode | str | None = None, client_closed: bool = False,
               confirmations: Sequence[str] = ()) -> dict:
        op_id, report_id = require_op_id(op_id), require_plan_digest(report_id)
        requested = None if mode is None else SwitchMode(mode)
        digest = switch_digest(report_id, requested)
        payload = admin_payload_digest(op_id, digest)
        return self._once(op_id, payload, lambda: self._switch(self._report_for(report_id), requested, op_id, payload,
                                                                digest, client_closed, tuple(confirmations)))

    def unstage(self, *, op_id: str, staged_set: str | None = None, staged_master: str | None = None) -> dict:
        op_id = require_op_id(op_id)
        if staged_set is None and staged_master is None:
            raise AdminError("config_unstage_empty", "Name the staged configuration set or master snapshot to undo",
                             HTTPStatus.UNPROCESSABLE_ENTITY)
        target = running_set(self._running_snapshot().inputs.config)
        request = {"operation": UNSTAGE_OPERATION, "staged_set": staged_set, "config_set": target}
        if staged_master is not None:
            request["staged_master"] = staged_master
        digest = json_digest(request)
        payload = admin_payload_digest(op_id, digest)
        return self._once(op_id, payload,
                          lambda: self._unstage(staged_set, staged_master, target, op_id, payload, digest))

    def _running_snapshot(self) -> _Running:
        with self._guard:
            return self._running

    def _running_for(self, current: Generation) -> _Running:
        running = self._running_snapshot()
        if current.id != running.generation_id:
            raise AdminError("generation_changed", "The running generation changed outside configuration switching")
        return running

    def _validation_view(self, validation: Validation) -> dict:
        status = self._runtime.build_status()
        return {"job": status if status is not None and status["job_id"] == validation.job_id else None,
                "validation": validation.describe()}

    def _explicit(self, identifier: str) -> Mapping[str, str]:
        return {} if identifier == EMPTY_SET else self._sets.load(identifier).explicit

    def _differences(self, first: str, second: str) -> list[str] | None:
        try:
            left, right = self._explicit(first), self._explicit(second)
        except ConfigError:
            return None
        return sorted(key for key in left.keys() | right.keys() if left.get(key) != right.get(key))

    def _assess(self, current: Generation, candidate: Generation, running: ResolvedConfig, validation: Validation,
                state: dict, now: datetime) -> Assessment:
        return assess(current, candidate, running, validation.inputs.config, state, now,
                      preflight=self._services.preflight, domains=self._services.preflight_domains,
                      pinned=validation.pinned)

    def _report(self, validation: Validation, candidate: Generation) -> SwitchReport:
        with self._runtime.lease() as current:
            running = self._running_for(current)
            pointer, latest = PointerState.read(self._sets), latest_switch(self._store)
            master = self._master_plan(current, latest, validation.master_snapshot)
            profile = self._store.profile()
            fingerprint = (candidate.id, current.id, pointer.digest, latest, profile.revision, master.state.mark)
            with self._guard:
                cached = next((report for report in self._reports.values() if report.fingerprint == fingerprint), None)
            if cached is not None:
                return cached
            now = LeasedGeneration(current).now()
            assessment = self._assess(current, candidate, running.inputs.config, validation, profile.state, now)
            report = SwitchReport.create(validation=validation, current=current, candidate=candidate,
                                         running=running.inputs.config, pointer=pointer, latest=latest, master=master,
                                         revision=profile.revision, assessment=assessment, now=now,
                                         fingerprint=fingerprint, audit=self._preview_audit)
        with self._guard:
            if self._validation is validation:
                self._reports[report.report_id] = report
                while len(self._reports) > REPORT_CAPACITY:
                    self._reports.popitem(last=False)
        return report

    def _report_for(self, report_id: str) -> SwitchReport:
        with self._guard:
            report = self._reports.get(report_id)
        if report is None:
            raise AdminError("config_report_expired", "The validation report expired or was replaced; validate again")
        return report

    def _once(self, op_id: str, payload: str, action: Callable[[], dict]) -> dict:
        replayed = self._replayed(op_id, payload)
        if replayed is not None:
            return replayed
        if not self._switching.acquire(blocking=False):
            raise AdminError("config_switch_busy", "Another configuration switch is in progress")
        try:
            return self._replayed(op_id, payload) or action()
        finally:
            self._switching.release()

    def _replayed(self, op_id: str, payload: str) -> dict | None:
        outcome = recorded_outcome(self._store, op_id, payload)
        return None if outcome is None else self._replay_view(outcome)

    def _replay_view(self, outcome: CommitOutcome) -> dict:
        return {**outcome.to_dict(), "pointer": self._pointer_view(), MASTER_TARGET: self._master_view()}

    def _pointer_view(self) -> dict:
        try:
            return PointerState.read(self._sets).describe(latest_switch(self._store))
        except ConfigError as error:
            return PointerState.unreadable(error)

    def _master_plan(self, current: Generation, latest: Mapping[str, Any] | None, requested: str | None) -> MasterPlan:
        recorded = recorded_activation(self._store, latest)
        return MasterPlan(MasterState.read(self._masters, current.identity.master_snapshot, recorded), requested)

    def _master_view(self) -> dict:
        return self._master_plan(self._runtime.current, latest_switch(self._store), None).state.describe()

    def _switch(self, report: SwitchReport, requested: SwitchMode | None, op_id: str, payload: str, digest: str,
                client_closed: bool, confirmations: tuple[str, ...]) -> dict:
        mode = requested or report.default_mode
        if mode is SwitchMode.STAGE:
            return self._stage(report, op_id, payload, digest)
        if report.tier is SwitchTier.HOT:
            return self._hot(report, op_id, payload, digest)
        return self._fenced(report, op_id, payload, digest, client_closed, confirmations)

    def _ready(self, report: SwitchReport, current: Generation,
               mode: SwitchMode) -> tuple[_Running, Generation, PointerState, MasterPlan]:
        pointer, latest = PointerState.read(self._sets), latest_switch(self._store)
        master = self._master_plan(current, latest, report.master.requested)
        changed = [name for name, unchanged in (("generation_id", current.id == report.current_id),
                                                ("pointer", pointer.digest == report.pointer_digest),
                                                (MASTER_TARGET, master.state.mark == report.master.state.mark))
                   if not unchanged]
        if changed:
            raise stale(changed)
        running = self._running_for(current)
        candidate = self._runtime.candidate()
        if candidate is None or candidate.id != report.candidate_id:
            raise AdminError(CANDIDATE_MISSING, "The candidate generation expired or was replaced; validate again")
        raise_first(pointer_blockers(mode, config_set=report.config_set,
                                     staged=staged_set(pointer, latest, report.running_set), pointer_set=pointer.set_id,
                                     current_id=current.id, candidate_id=candidate.id, master=master))
        if master.activation is not None:
            # The pointer moves only after the commit, so its target must still verify before anything is recorded.
            self._masters.snapshot(master.activation.target)
        return running, candidate, pointer, master

    def _require_master(self, report: SwitchReport) -> None:
        if MasterState.read(self._masters, None, None).mark != report.master.state.mark:
            raise stale((MASTER_TARGET,))

    def _checked(self, report: SwitchReport, mode: SwitchMode, running: _Running, current: Generation,
                 candidate: Generation, state: dict) -> Assessment:
        assessment = self._assess(current, candidate, running.inputs.config, report.validation, state,
                                  LeasedGeneration(current).now())
        if assessment.tier is not report.tier:
            raise AdminError("config_tier_changed", "The player state now needs a different switch tier; validate again",
                             details={"reported": report.tier, "current": assessment.tier})
        raise_first(assessment_blockers(assessment, mode))
        return assessment

    def _require_sequence(self, pointer: PointerState) -> None:
        latest = latest_switch(self._store)
        following = (0 if latest is None else latest["sequence"]) + 1
        if pointer.sequence is not None and pointer.sequence >= following:
            raise AdminError("config_sequence_regressed", "The active pointer is ahead of the recorded switch history",
                             details={"pointer_sequence": pointer.sequence, "next_sequence": following})

    def _hot(self, report: SwitchReport, op_id: str, payload: str, digest: str) -> dict:
        # The lease spans the swap, so a restore fence cannot open between this commit and the swap.
        with self._runtime.lease() as current:
            running, candidate, pointer, master = self._ready(report, current, SwitchMode.APPLY)
            self._checked(report, SwitchMode.APPLY, running, current, candidate, self._store.profile().state)
            # Checked before the commit: a draining generation would make the swap wait or fail after the commit.
            if self._runtime.status()["draining"] is not None:
                raise AdminError("generation_draining",
                                 "The previous generation is still serving requests; retry shortly")
            self._require_sequence(pointer)
            candidate = self._runtime.take_candidate(report.candidate_id)
            try:
                result = self._result(report, SwitchMode.APPLY, op_id, master)
                outcome = commit_audited(self._store, op_id, payload, None, None,
                                         self._record(report, SwitchMode.APPLY, report.tier.value, digest, result, master))
            except BaseException:
                # A refused commit must not consume the candidate, so the retry can switch without a rebuild.
                self._runtime.restore_candidate(candidate)
                raise
            return self._finish(outcome, result, op_id, pointer, report=report, swapped=(candidate, current.id),
                                master=master.activation)

    def _fenced(self, report: SwitchReport, op_id: str, payload: str, digest: str, client_closed: bool,
                confirmations: tuple[str, ...]) -> dict:
        content, gate = report.tier is SwitchTier.CONTENT, self._services.gate
        with self._runtime.lease() as current:
            running, candidate, pointer, master = self._ready(report, current, SwitchMode.APPLY)
            if content:
                gate.require_closed(client_closed)
                check_confirmations(confirmations_for(report.tier, SwitchMode.APPLY, report.revision), confirmations)
            profile = self._store.profile()
            if profile.revision != report.revision:
                raise revision_conflict(profile.revision)
            checked = self._checked(report, SwitchMode.APPLY, running, current, candidate, profile.state)
        self._require_sequence(pointer)
        # Measured before the backup, so an oversized record cannot replace the single automatic backup.
        require_auditable(self._record(report, SwitchMode.APPLY, report.tier.value, digest,
                                       self._result(report, SwitchMode.APPLY, op_id, master, assessment=checked),
                                       master, assessment=checked))
        # Only one automatic backup is kept, so a switch that leaves the state as it is must not replace it.
        backup = (auto_backup(self._store, self._services.backup_directory, report.backup_reason, op_id)
                  if checked.changed else None)
        with self._runtime.fence() as fenced:
            if fenced.id != report.current_id:
                raise stale(("generation_id",))
            self._require_master(report)
            if content:
                gate.require_closed(client_closed)
            profile = self._store.profile()
            if profile.revision != report.revision:
                raise revision_conflict(profile.revision)
            assessment = self._checked(report, SwitchMode.APPLY, running, fenced, candidate, profile.state)
            # The revision held, so only time passing since the check can make a skipped-backup switch change the state.
            if assessment.changed and backup is None:
                raise stale(("state_sha256",))
            candidate = self._runtime.take_candidate(report.candidate_id)
            try:
                result = self._result(report, SwitchMode.APPLY, op_id, master, assessment=assessment, backup=backup)
                outcome = commit_audited(self._store, op_id, payload, report.revision,
                                         assessment.target if assessment.changed else None,
                                         self._record(report, SwitchMode.APPLY, report.tier.value, digest, result, master,
                                                      assessment=assessment))
            except BaseException:
                # A refused commit must not consume the candidate, so the retry can switch without a rebuild.
                self._runtime.restore_candidate(candidate)
                raise
            return self._finish(outcome, result, op_id, pointer, report=report, swapped=(candidate, fenced.id),
                                master=master.activation)

    def _stage(self, report: SwitchReport, op_id: str, payload: str, digest: str) -> dict:
        with self._runtime.lease() as current:
            running, candidate, pointer, master = self._ready(report, current, SwitchMode.STAGE)
            self._checked(report, SwitchMode.STAGE, running, current, candidate, self._store.profile().state)
        self._require_sequence(pointer)
        candidate = self._runtime.take_candidate(report.candidate_id)
        try:
            result = self._result(report, SwitchMode.STAGE, op_id, master)
            outcome = commit_audited(self._store, op_id, payload, None, None,
                                     self._record(report, SwitchMode.STAGE, STAGED_TIER, digest, result, master))
        except BaseException:
            # A refused commit must not consume the candidate, so the retry can stage without a rebuild.
            self._runtime.restore_candidate(candidate)
            raise
        return self._finish(outcome, result, op_id, pointer, report=report, master=master.activation)

    def _unstage(self, staged: str | None, staged_master: str | None, target: str, op_id: str, payload: str,
                 digest: str) -> dict:
        current = self._runtime.current
        if running_set(self._running_for(current).inputs.config) != target:
            raise stale(("running_set",))
        pointer, latest = PointerState.read(self._sets), latest_switch(self._store)
        if staged is not None:
            waiting = staged_set(pointer, latest, target)
            if waiting is None:
                raise AdminError("config_not_staged", "No staged configuration waits for the next start")
            if waiting != staged:
                raise AdminError("config_staged_changed", "A different configuration is staged now",
                                 details={"staged_set": waiting})
        # Undoing only the master keeps whichever configuration the next start would use.
        following = target if staged is not None else pointer.next_start(latest)
        if following is None:
            raise config_failure(pointer.error)
        activation = None
        if staged_master is not None:
            activation = self._master_plan(current, latest, None).state.unstaging(staged_master)
        self._require_sequence(pointer)
        if following == EMPTY_SET:
            with writing_sets():
                self._sets.create({}, source=SET_SOURCE)
        keys = [] if staged is None else self._differences(target, staged) or []
        params = {"staged_set": staged, "config_set": following,
                  **({} if staged_master is None else {"staged_master": staged_master})}
        result, _ = canonical({"op_id": op_id, "kind": CONFIG_KIND, "operation": UNSTAGE_OPERATION, **params,
                               "generation_id": current.id, "keys": keys,
                               MASTER_ACTIVATION: None if activation is None else activation.describe()})
        targets = [*keys, *(() if staged_master is None else (MASTER_TARGET,))]
        outcome = commit_audited(self._store, op_id, payload, None, None, audit_record(
            operation=UNSTAGE_OPERATION, params=params, targets=targets, digest=digest, generation_id=current.id,
            content_versions=current.identity.content_versions,
            switch={"config_set": following, "tier": UNSTAGED_TIER, "generation_id": current.id}, result=result))
        return self._finish(outcome, result, op_id, pointer, config_set=following, master=activation)

    def _result(self, report: SwitchReport, mode: SwitchMode, op_id: str, master: MasterPlan, *,
                assessment: Assessment | None = None, backup: Mapping[str, Any] | None = None) -> dict:
        applied, activation = mode is SwitchMode.APPLY, master.activation
        result = {"op_id": op_id, "kind": CONFIG_KIND, "operation": SWITCH_OPERATION, "mode": mode, "tier": report.tier,
                  "report_id": report.report_id, "config_set": report.config_set, "previous_set": report.running_set,
                  "generation_before": report.current_id,
                  "generation_after": report.candidate_id if applied else report.current_id, "keys": list(report.keys),
                  "receipts_invalidated": sorted(report.content_versions) if applied else [],
                  "client_actions": report.client_actions, "master_snapshot": master.requested,
                  MASTER_ACTIVATION: None if activation is None else activation.describe()}
        if assessment is not None:
            result.update(revision_before=report.revision, revision_after=report.revision + int(assessment.changed),
                          state_sha256_before=assessment.state_sha256_before,
                          state_sha256_after=assessment.state_sha256_after, energy=list(assessment.energy),
                          cooking=assessment.cooking, beginner=assessment.beginner,
                          readback={} if assessment.readback is None else assessment.readback.to_dict()["readback"],
                          backup_before_switch=None if backup is None else dict(backup))
        result, _ = canonical(result)
        return result

    def _record(self, report: SwitchReport, mode: SwitchMode, tier: str, digest: str, result: dict, master: MasterPlan,
                *, assessment: Assessment | None = None) -> dict:
        claims = None if assessment is None else {
            "revision_before": result["revision_before"], "revision_after": result["revision_after"],
            "state_sha256_before": assessment.state_sha256_before, "state_sha256_after": assessment.state_sha256_after}
        targets = [*report.keys, *((MASTER_TARGET,) if master.involved else ())]
        return audit_record(operation=SWITCH_OPERATION, params=report.params(mode), targets=targets, digest=digest,
                            generation_id=report.current_id, content_versions=report.current_versions,
                            switch={"config_set": report.config_set, "tier": tier, "generation_id": report.candidate_id},
                            result=result, entries=() if assessment is None else assessment.entries, claims=claims)

    def _preview_audit(self, report: SwitchReport, assessment: Assessment) -> dict:
        mode, master = report.default_mode, report.master
        digest = switch_digest(report.report_id, mode)
        return preview_audit(assessment.readback, lambda _readback: self._record(
            report, mode, report.tier.value, digest,
            self._result(report, mode, PREVIEW_OP_ID, master, assessment=assessment), master, assessment=assessment))

    def _finish(self, outcome: CommitOutcome, result: dict, op_id: str, pointer: PointerState, *,
                report: SwitchReport | None = None, swapped: tuple[Generation, str] | None = None,
                config_set: str | None = None, master: MasterActivation | None = None) -> dict:
        if outcome.replayed:
            return self._replay_view(outcome)
        latest = latest_switch(self._store)
        sequence = latest["sequence"] if latest is not None and latest["op_id"] == op_id else None
        if swapped is not None:
            candidate, expected = swapped
            self._settle_notice()
            self._runtime.swap(candidate, expected)
            self._adopt(report, candidate.id)
            log.info("Configuration set %s now runs as generation %s", report.config_set, candidate.id)
        target = report.config_set if config_set is None else config_set
        published = self._publish(target, sequence, pointer.digest)
        moved = self._publish_master(master)
        return {**result, "replayed": False, "pointer": published, MASTER_TARGET: {**self._master_view(), **moved}}

    def _publish(self, config_set: str, sequence: int | None, expected: str | None) -> dict:
        try:
            if sequence is None:
                raise AdminError("config_switch_unrecorded", "The committed switch has no recorded sequence",
                                 HTTPStatus.INTERNAL_SERVER_ERROR)
            self._sets.activate(config_set, sequence, expected=expected)
        except (ProtocolError, OSError) as error:
            log.warning("The configuration pointer was not moved to set %s: %s", config_set, error)
            return {**self._pointer_view(), "published": False, "publish_error": error_payload(error)}
        return {**self._pointer_view(), "published": True}

    def _publish_master(self, activation: MasterActivation | None) -> dict:
        if activation is None:
            return {}
        # Snapshot path checks raise plain ValueError, and after the commit a failed move is reported, never raised.
        try:
            self._masters.activate(activation.target, expected=activation.pointer_digest)
        except (ValueError, OSError) as error:
            log.warning("The master snapshot pointer was not moved to %s: %s", activation.target, error)
            return {"published": False, "publish_error": error_payload(error)}
        return {"published": True}

    def _adopt(self, report: SwitchReport, generation_id: str) -> None:
        with self._guard:
            self._running = _Running(report.validation.inputs, generation_id)
            self._reports.clear()
            if report.tier is SwitchTier.CONTENT:
                self._notice = {"config_set": report.config_set, "generation_id": generation_id,
                                "client_actions": report.client_actions}

    def _settle_notice(self) -> None:
        # Every swap resets session observation, so a notice a restarted client already answered is dropped first.
        if self._runtime.session_observed_since_switch():
            with self._guard:
                self._notice = None

    def _client_warning(self) -> dict | None:
        self._settle_notice()
        with self._guard:
            return None if self._notice is None else dict(self._notice)


def install(application: FastAPI, *, inputs: GenerationInputs, builder: Builder,
            registry: ConfigRegistry = REGISTRY) -> ConfigSwitch:
    switch = ConfigSwitch(getattr(application.state, ADMIN_STATE, None), inputs=inputs, builder=builder,
                          registry=registry)
    setattr(application.state, SWITCH_STATE, switch)
    return switch


def config_switch(application: FastAPI) -> ConfigSwitch:
    switch = getattr(application.state, SWITCH_STATE, None)
    if not isinstance(switch, ConfigSwitch):
        raise AdminError("config_switch_unavailable", "Configuration switching is not connected to this server",
                         HTTPStatus.SERVICE_UNAVAILABLE)
    return switch


def roll_forward(store: Store, data_dir: Path, *, registry: ConfigRegistry = REGISTRY) -> dict:
    if Path(data_dir).resolve() != store.path.parent:
        raise ValueError("Roll-forward reads the switch history of the database in the data directory")
    sets = ConfigSets(data_dir, registry=registry)
    latest, pointer = latest_switch(store), PointerState.read(sets)
    masters = roll_master_forward(store, MasterDataStore.for_data_dir(data_dir), latest)
    result = {"rolled_forward": False, "switch": latest, "previous": pointer.describe(latest), MASTER_TARGET: masters}
    if not pointer.behind(latest):
        return {**result, "pointer": result["previous"]}
    activated = sets.activate(latest["config_set"], latest["sequence"], expected=pointer.digest)
    log.info("Rolled the configuration pointer forward to set %s at switch %d", activated.set_id, activated.sequence)
    return {**result, "rolled_forward": True, "pointer": PointerState(activated.digest, activated).describe(latest)}
