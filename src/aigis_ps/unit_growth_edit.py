from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import re

from .errors import ProtocolError
from .native_numbers import int32
from .player_state import PRINCE_CARD_IDS
from .unit_removal import UnitIdentity


EXPERIENCE, LOVE, SKILL_LEVEL, SKILL_EXPERIENCE = "Exp", "Love", "SkillLevel", "SkillExperience"
GROWTH_FIELDS = (EXPERIENCE, LOVE, SKILL_LEVEL, SKILL_EXPERIENCE)
TRUST_LIMIT, ADVANCED_TRUST_LIMIT = 100, 150
MINIMUM_SKILL_LEVEL = 1
OVERFLOW_DROPPED = "Experience above the level-cap threshold was dropped by the training overflow rule"
_PREFIX = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


@dataclass(frozen=True)
class GrowthEdit:
    experience: int | None = None
    love: int | None = None
    skill_level: int | None = None

    def __post_init__(self) -> None:
        values = [getattr(self, field.name) for field in fields(self)]
        if (all(value is None for value in values)
                or any(value is not None and (not int32(value) or value < 0) for value in values)):
            raise ValueError("Growth edits set at least one nonnegative native value")


@dataclass(frozen=True)
class GrowthLimits:
    level: int
    level_cap: int
    cap_experience: int
    retains_overflow: bool
    love_limit: int
    skill_level_max: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GrowthResult:
    state: dict
    before: dict
    after: dict
    warnings: tuple[str, ...] = ()


class UnitGrowthEditor:
    def __init__(self, players, growth, gifts, *, error_prefix: str = "unit_growth"):
        if type(error_prefix) is not str or not _PREFIX.fullmatch(error_prefix):
            raise ValueError("Unit growth errors require a bounded code prefix")
        self._prefix = error_prefix
        if growth is None or gifts is None:
            self._error("dependencies_unavailable", "Growth edits need the training growth tables and the trust ledger", 503)
        self.players, self.growth, self.gifts = players, growth, gifts

    def _error(self, suffix: str, message: str, status: int = 409):
        raise ProtocolError(f"{self._prefix}_{suffix}", message, status)

    def limits(self, unit: dict) -> GrowthLimits:
        try:
            view = self.growth.describe(unit)
            card = self.gifts.catalog.card(unit["CardID"])
            love_limit = ADVANCED_TRUST_LIMIT if card["BonusType"] and card["BonusType3"] else TRUST_LIMIT
            return GrowthLimits(view.level, view.cap, view.thresholds[view.cap - 1],
                                self.growth.rules.retain_overflow_experience, love_limit, view.skill_max)
        except ProtocolError:
            raise
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
            raise ProtocolError(f"{self._prefix}_reference_invalid",
                                "Unit growth state or resource references are invalid", 503) from error

    def _changes(self, unit: dict, limits: GrowthLimits, edit: GrowthEdit) -> tuple[dict, tuple[str, ...]]:
        changes, warnings = {}, []
        if edit.experience is not None:
            experience = edit.experience if limits.retains_overflow else min(edit.experience, limits.cap_experience)
            if experience != edit.experience:
                warnings.append(OVERFLOW_DROPPED)
            changes[EXPERIENCE] = experience
        if edit.love is not None:
            if edit.love > limits.love_limit:
                self._error("limit", f"Trust for this card stays between 0 and {limits.love_limit}")
            changes[LOVE] = edit.love
        if edit.skill_level is not None:
            if limits.skill_level_max < MINIMUM_SKILL_LEVEL:
                self._error("limit", "This unit's skill has no levels to edit")
            if not MINIMUM_SKILL_LEVEL <= edit.skill_level <= limits.skill_level_max:
                self._error("limit", f"Skill level for this unit stays between {MINIMUM_SKILL_LEVEL} and {limits.skill_level_max}")
            if edit.skill_level != unit[SKILL_LEVEL]:
                # Material skill-ups that grant whole levels restart the progress toward the next level the same way.
                changes.update({SKILL_LEVEL: edit.skill_level, SKILL_EXPERIENCE: 0})
        return changes, tuple(warnings)

    def edit(self, state: dict, revision: int, identity: UnitIdentity, edit: GrowthEdit) -> GrowthResult:
        if not isinstance(identity, UnitIdentity) or not isinstance(edit, GrowthEdit):
            raise ValueError("Growth edits need a unit identity and the edited values")
        unit = next((unit for unit in self.players.validate(state)["units"] if unit["GetIndex"] == identity.get_index), None)
        if unit is None or unit["UniqueID"] != identity.unique_id:
            self._error("missing", "Growth target does not match an owned unit")
        if unit["CardID"] in PRINCE_CARD_IDS:
            self._error("unsupported", "The prince grows with the account rank, so its growth is not editable", 501)
        changes, warnings = self._changes(unit, self.limits(unit), edit)
        # Trust tiers reached before the edit are frozen first, so lowering trust can never reopen them.
        updated = self.gifts.preserve_history(state, revision)
        target = next(row for row in updated["player_state"]["units"] if row["GetIndex"] == identity.get_index)
        target.update(changes)
        updated = self.gifts.record_current_thresholds(updated, identity.get_index, revision)
        after = next(row for row in updated["player_state"]["units"] if row["GetIndex"] == identity.get_index)
        return GrowthResult(updated, dict(unit), dict(after), warnings)
