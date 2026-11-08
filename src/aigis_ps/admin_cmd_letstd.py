from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field

from .admin_command import AdminCommand, CommandContext, CommandParams, CommandResult, required_service, requires
from .admin_registry import CommandRegistry
from .letstd_rules import GROUPS, PARTNERS
from .letstd_state import DELIVERED, group_record, read_letstd


LETSTD = "letstd"
FINGERPRINTS = ("rules_version", "resource_sha256")
NOTHING_REARMED = "No row changed; rearming only recorded the current rule and resource fingerprints"


def strict_choice(values: Iterable[int], subject: str) -> Any:
    allowed = tuple(values)
    listed = " or ".join(map(str, allowed))

    def check(value: int) -> int:
        if value not in allowed:
            raise ValueError(f"The {subject} must be {listed}")
        return value

    # Literal integers also match true and 2.0, so the value stays a strict int checked against the native choices.
    return Annotated[int, AfterValidator(check), Field(json_schema_extra={"enum": list(allowed)})]


Partner = Literal[PARTNERS]
RewardGroup = strict_choice(GROUPS, "LetsTD reward group")


class RearmParams(CommandParams):
    partner: Partner
    group: RewardGroup


def rearmed_rows(before: dict, after: dict) -> dict:
    rows, updated = before["rows"], after["rows"]
    delivered = {key for key, row in rows.items() if row["status"] == DELIVERED}
    return {"added": sorted(int(key) for key in updated.keys() - rows.keys()),
            "reset": sorted(int(key) for key, row in rows.items() if key not in delivered and updated[key] != row),
            "adapted": sorted(int(key) for key in delivered
                              if updated[key]["needs_adaptation"] != rows[key]["needs_adaptation"])}


def rearm(state: dict, params: RearmParams, context: CommandContext) -> CommandResult:
    letstd = required_service(context, LETSTD)
    updated = letstd.rearm(state, params.partner, params.group, context.now)
    before = group_record(read_letstd(state), params.partner, params.group)
    after = group_record(read_letstd(updated), params.partner, params.group)
    changes = rearmed_rows(before, after)
    effects = {"partner": params.partner, "group": params.group, "name": GROUPS[params.group], **changes,
               "rearmed_at": after["rearmed_at"],
               **{name: {"before": before[name], "after": after[name]} for name in FINGERPRINTS}}
    return CommandResult(updated, effects, () if any(changes.values()) else (NOTHING_REARMED,))


def rearm_phrase(params: RearmParams) -> str:
    return f"REARM {params.partner.upper()} {GROUPS[params.group].upper()}"


REARM = AdminCommand("letstd.rearm", LETSTD, RearmParams, rearm, frozenset({LETSTD}), destructive=True,
                     confirm_phrase=rearm_phrase, unavailable=requires(LETSTD))
COMMANDS = (REARM,)


def register(registry: CommandRegistry) -> None:
    for command in COMMANDS:
        registry.add(command)
