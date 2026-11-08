from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import re
from types import MappingProxyType

from .config_state import configuration_row
from .errors import ProtocolError
from .guide_service import GUIDE_FIELD, NO_GUIDE
from .native_numbers import int32
from .player_state import EMPTY_SQUAD_SLOT, LOCK_FLAG, MAX_PLAYER_UNITS, PRINCE_CARD_IDS, squad_group


_PREFIX = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class Protection(StrEnum):
    PRINCE = "prince"
    LOCKED = "locked"
    SQUAD = "squad"
    CURRENT_SQUAD = "current_squad"
    SECRETARY = "secretary"
    BATTLE_RESERVATION = "battle_reservation"


SAVE_PROTECTIONS = frozenset((Protection.SECRETARY, Protection.BATTLE_RESERVATION))


@dataclass(frozen=True)
class RemovalPolicy:
    refuse: frozenset[Protection] = frozenset(Protection)

    def __post_init__(self) -> None:
        refuse = frozenset(self.refuse) if isinstance(self.refuse, (set, frozenset)) else None
        if refuse is None or any(not isinstance(item, Protection) for item in refuse):
            raise ValueError("Removal policies refuse a set of unit protections")
        object.__setattr__(self, "refuse", refuse)


STRICT_REMOVAL = RemovalPolicy()


@dataclass(frozen=True)
class UnitIdentity:
    get_index: int
    unique_id: int

    def __post_init__(self) -> None:
        if not int32(self.get_index) or self.get_index < 0 or not int32(self.unique_id) or self.unique_id <= 0:
            raise ValueError("Unit identities pair a nonnegative GetIndex with a positive UniqueID")


@dataclass(frozen=True)
class RemovalResult:
    state: dict
    removed: tuple[dict, ...]
    vacated: tuple[int, ...]


class UnitsProtected(ProtocolError):
    def __init__(self, code: str, protected: Mapping[int, tuple[Protection, ...]]):
        super().__init__(code, "Selected units are protected from removal", 409)
        self.protected = MappingProxyType(dict(protected))


def protection_check(player: Mapping, checked: frozenset[Protection], *, players=None,
                     state: dict | None = None) -> Callable[[Mapping], tuple[Protection, ...]]:
    if (players is None or state is None) and not SAVE_PROTECTIONS.isdisjoint(checked):
        raise ValueError("Secretary and battle protections need the player service and the complete save")
    battle = player["battle"]
    current = {battle["TN"], battle["RTN"]}
    members = [row for row in player["squads"] if row["UnitIndex"] != EMPTY_SQUAD_SLOT]
    organized = {row["UnitIndex"] for row in members}
    leading = {row["UnitIndex"] for row in members if squad_group(row) in current}
    # Unchecked protections skip the guide and the battle journal, so relaxed policies never wait for legacy battles.
    guide = configuration_row(state).get(GUIDE_FIELD, NO_GUIDE) if Protection.SECRETARY in checked else NO_GUIDE
    # Battle reservations name reward CardIDs rather than instances, so owned units of a reserved card stay untouched.
    reserved = ({grant["card_id"] for grant in players.quest_reservations(state)}
                if Protection.BATTLE_RESERVATION in checked else frozenset())

    def protections(unit: Mapping) -> tuple[Protection, ...]:
        return tuple(protection for protection, applies in (
            (Protection.PRINCE, unit["CardID"] in PRINCE_CARD_IDS),
            (Protection.LOCKED, bool(unit["GeneralPurposeFlags"] & LOCK_FLAG)),
            (Protection.SQUAD, unit["GetIndex"] in organized),
            (Protection.CURRENT_SQUAD, unit["GetIndex"] in leading),
            (Protection.SECRETARY, guide != NO_GUIDE and unit["GetIndex"] == guide),
            (Protection.BATTLE_RESERVATION, unit["CardID"] in reserved)) if applies and protection in checked)
    return protections


def unit_protections(players, state: dict,
                     checked: frozenset[Protection] = frozenset(Protection)) -> Mapping[int, tuple[Protection, ...]]:
    player = players.validate(state, references=False)
    protections = protection_check(player, checked, players=players, state=state)
    return MappingProxyType({unit["GetIndex"]: protections(unit) for unit in player["units"]})


def refused_protections(player: Mapping, units: Iterable[Mapping], policy: RemovalPolicy, *, players=None,
                        state: dict | None = None) -> dict[int, tuple[Protection, ...]]:
    protections = protection_check(player, policy.refuse, players=players, state=state)
    return {unit["GetIndex"]: found for unit in units if (found := protections(unit))}


def consume_units(player: dict, units: Iterable[Mapping]) -> tuple[int, ...]:
    consumed = {unit["GetIndex"] for unit in units}
    # The high-water mark keeps the UniqueIDs of consumed units from being granted again.
    player["last_unique_id"] = max(player.get("last_unique_id", 0), *(unit["UniqueID"] for unit in player["units"]))
    player["units"] = [unit for unit in player["units"] if unit["GetIndex"] not in consumed]
    vacated = tuple(row["Address"] for row in player["squads"] if row["UnitIndex"] in consumed)
    if vacated:
        player["squads"] = [{**row, "UnitIndex": EMPTY_SQUAD_SLOT} if row["UnitIndex"] in consumed else row
                            for row in player["squads"]]
    return vacated


class UnitRemoval:
    def __init__(self, players, gifts, evolution_rewards, *, error_prefix: str = "unit_removal"):
        if type(error_prefix) is not str or not _PREFIX.fullmatch(error_prefix):
            raise ValueError("Unit removal errors require a bounded code prefix")
        self._prefix = error_prefix
        if gifts is None or evolution_rewards is None:
            self._error("dependencies_unavailable", "Removing units needs the trust and awakening reward ledgers", 503)
        self.players, self.gifts, self.evolution_rewards = players, gifts, evolution_rewards

    def _error(self, suffix: str, message: str, status: int = 409):
        raise ProtocolError(f"{self._prefix}_{suffix}", message, status)

    def _selection(self, selection) -> tuple[UnitIdentity, ...]:
        if (isinstance(selection, (str, bytes)) or not isinstance(selection, Sequence)
                or not 1 <= len(selection) <= MAX_PLAYER_UNITS
                or any(not isinstance(identity, UnitIdentity) for identity in selection)):
            self._error("invalid", "Unit removal needs a bounded nonempty selection of unit identities", 400)
        if len({identity.get_index for identity in selection}) != len(selection):
            self._error("invalid", "Each unit can be selected for removal only once", 400)
        return tuple(selection)

    def remove(self, state: dict, revision: int, selection: Sequence[UnitIdentity],
               policy: RemovalPolicy = STRICT_REMOVAL) -> RemovalResult:
        if not isinstance(policy, RemovalPolicy):
            raise ValueError("Unit removal needs a removal policy")
        identities = self._selection(selection)
        player = self.players.validate(state)
        owned = {unit["GetIndex"]: unit for unit in player["units"]}
        if any(identity.get_index not in owned or owned[identity.get_index]["UniqueID"] != identity.unique_id
               for identity in identities):
            self._error("missing", "Removal selection does not match an owned unit")
        selected = tuple(owned[identity.get_index] for identity in identities)
        refused = refused_protections(player, selected, policy, players=self.players, state=state)
        if refused:
            raise UnitsProtected(f"{self._prefix}_protected", refused)
        if len(identities) == len(owned):
            self._error("invalid", "Removal must leave at least one unit in the roster", 400)
        # Both reward baselines are derived from the roster that still holds the selected units.
        updated = self.gifts.preserve_history(self.evolution_rewards.initialize(state), revision)
        vacated = consume_units(updated["player_state"], selected)
        self.players.validate(updated)
        return RemovalResult(updated, tuple(dict(unit) for unit in selected), vacated)
