from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import MISSING, asdict, dataclass, fields, replace
from datetime import datetime
import json
import logging
import secrets
import threading
from types import MappingProxyType
from weakref import WeakKeyDictionary

from . import quest_modes
from .alxml import Column, Container, dumps
from .battle_events import EVENT_PARAM_LIMIT, EVENT_PARAM_REWRITE, RESULT, BattleEventContext, BattleEventRewards
from .bootstrap import BootstrapService
from .business_calendar import NATIVE_DATETIME, business_day, civil_date, civil_day
from .config_state import patch_configuration
from .crystal_wallet import credit_free_crystals, free_crystal_response, free_crystals_fit
from .drop_bonus import DropBonusGrant, bonus_grant
from .errors import ProtocolError
from .energy_clock import ENCODING, JST, UTC_OFFSET_MINUTES, instant, validate_clock
from .event_mission_rewards import EventMissionCatalog
from .local_io import reject_duplicate_keys
from .native_numbers import INT32_MAX, UINT32_MASK, int32, signed32
from .player_state import PRINCE_CARD_IDS, UNIT_FIELDS, PlayerStateService
from .possessions import PossessionService
from .present_backlog import PresentBacklog, reward_valid
from .protocol import RequestEnvelope, encode_response
from .public_tables import validate_public_master
from .quest_catalog import HARMONY_TYPE, DropCandidate, QuestCatalog, QuestDefinition, QuestMode
from .quest_economy import (
    NO_GOLD_ABILITY, STAR_GOLD_RATES, UNRATED_STARS, GoldAbilities, GoldPercents, boosted_experience, charge_quest,
    credit_quest, game_param_response, quest_gold, require_quest_energy, settle_energy,
)
from .quest_modes import (
    BOOSTS, CLEAR_FLAG, HARD_STAR_FLAG, MAX_BOOST, MAX_OBTAINED, MODE_PROGRESS, SLOT_FLAGS, THREE_STAR_FLAG, UNBOOSTED,
    ModePolicy, ModeServices, ScoreContext, ScoreResult, SettleContext, SettleOutcome, StartContext,
)
from .quest_protocol import (
    AVATAR_INDEX_OFFSETS, FIXED_INDEX_OFFSET, MAX_CANDIDATES, PlatformTally, QuestFinish, parse_exit, parse_finish,
    parse_start, platform_code,
)
from .quest_record_service import UNPLAYED
from .request_ledger import PROCESSED_LIMIT, REQUEST_ORDER
from .storage import PlayerProfile, Store
from .wallet_money import GOLD_LIMIT


JOURNAL_KEY = "quest_state"
HISTORY_LIMIT = 8
JOURNAL_SCHEMA_VERSION = 2
LEGACY_JOURNAL_SCHEMA_VERSION = 1
PROCESSED_KEYS, LEGACY_WATERMARK = "processed_keys", "last_client_time"
# Start and result keys a journal entry may hold; exits are acknowledged by their own replay window.
REQUEST_KEYS = ("start_key", "finish_key")
ABORTED = "aborted"
CHARGE_KEY, SETTLEMENT_CHARGE = "charge", "settle"
GOLD_CARD_FIELDS = ("CardID", "Rare", "Ability", "Ability_Default")
RESPONSE_CACHE_LIMIT = 256 * 1024
REGISTRATION_PAIRS = {77: 80, 78: 80, 81: 83, 82: 83}
HARD_REGISTRATIONS = frozenset((78, 82))
STAR_RESULTS = {"normal": frozenset((1, 2, 3)), "hard": frozenset((1, 2, 4))}
FIRST_STARS = {"normal": (3, THREE_STAR_FLAG), "hard": (4, HARD_STAR_FLAG)}
STAR_PRESENT_TEXT = {"normal": "★３報酬", "hard": "★４チャレンジ報酬"}
LEGACY_MODE_KEYS = frozenset(("quest_type", "difficulty", "star_rewards", "boost_ignored"))
MODE_KEYS = LEGACY_MODE_KEYS | {"boost"}
# Attempts saved before conditional prize rows stored candidates without these trailing fields.
CANDIDATE_DEFAULTS = MappingProxyType({field.name: field.default for field in fields(DropCandidate)
                                       if field.default is not MISSING})
COLLECTION_SLOTS = 0x11100
EXPERIENCE_CAMPAIGN, MONEY_CAMPAIGN = "ExperienceUpCloseDateTime", "MoneyUpCloseDateTime"
# 54E0A0 adds a prince copy to a fixed team only for the selected Type 10 partner.
FIXED_TEAM_STAND_INS = MappingProxyType({HARMONY_TYPE: 1})
# Public sources pay a four-star result like a three-star clear.
MAX_GOLD_STARS = max(STAR_GOLD_RATES)
log = logging.getLogger("aigis_ps")
# Services built on one catalog share its gold abilities, because parsing CardData takes most of a second.
_GOLD_ABILITIES: WeakKeyDictionary = WeakKeyDictionary()
_GOLD_ABILITIES_LOCK = threading.Lock()


@dataclass(frozen=True)
class QuestRules:
    drop_basis_points: int = 10000
    experience_percent: int = 100
    gold_percent: int = 100
    first_three_star_crystals: int = 1
    collection_unit_level: int = 1
    charisma_recovery_seconds: int = 180
    refill_on_rank_up: bool = True
    exit_replay_window_seconds: int = 5
    gold_per_event: int = 100
    max_gold_events: int = 1000

    def __post_init__(self):
        limits = {
            "drop_basis_points": (0, 10000), "experience_percent": (0, 10000),
            "gold_percent": (0, 10000), "first_three_star_crystals": (0, 100),
            "collection_unit_level": (0, 1000), "charisma_recovery_seconds": (1, 86400),
            "exit_replay_window_seconds": (0, 60), "gold_per_event": (0, 100000),
            "max_gold_events": (0, 100000),
        }
        for name, (low, high) in limits.items():
            if type(getattr(self, name)) is not int or not low <= getattr(self, name) <= high:
                raise ValueError(f"Invalid local quest rule: {name}")
        if type(self.refill_on_rank_up) is not bool:
            raise ValueError("Rank-up refill rule must be boolean")


def _conflict(code: str, message: str):
    raise ProtocolError(code, message, 409)


def _invalid_state(message: str):
    raise ProtocolError("quest_state_invalid", message, 503)


def _require_aware(now) -> None:
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ProtocolError("quest_clock_invalid", "Quest rewards require an aware datetime", 503)


def _load_gold_abilities(catalog: QuestCatalog, schemas: dict) -> GoldAbilities:
    try:
        document, _ = validate_public_master("CardData", catalog.snapshot.get_xml("CardData"), schemas)
        columns = {column.name: column.values for column in document.children}
        cards = [dict(zip(GOLD_CARD_FIELDS, row)) for row in zip(*(columns[name] for name in GOLD_CARD_FIELDS))]
        return GoldAbilities(cards, catalog.resource_rows("AbilityList.atb"), catalog.resource_rows("AbilityConfig.atb"))
    except (KeyError, ValueError, ProtocolError) as error:
        raise ProtocolError("quest_gold_abilities_invalid",
                            "Gold abilities require verified CardData and ability tables", 503) from error


def _settled_gold(quest: QuestDefinition, rules: QuestRules, events: int, stars: int, percents: GoldPercents,
                  money_up: bool) -> int:
    return quest_gold(quest.gold, gold_percent=rules.gold_percent, events=events, gold_per_event=rules.gold_per_event,
                      stars=stars, percents=percents, money_up=money_up)


def _progress_valid(progress) -> bool:
    if not isinstance(progress, dict) or any(not isinstance(entry, dict) for entry in progress.values()):
        return False
    return not progress or set(progress) <= {policy.name for policy in quest_modes.registry().values()}


def _requests_valid(journal) -> bool:
    if journal["schema_version"] == LEGACY_JOURNAL_SCHEMA_VERSION:
        watermark = journal.get(LEGACY_WATERMARK)
        return PROCESSED_KEYS not in journal and (watermark is None or type(watermark) is int)
    keys = journal.get(PROCESSED_KEYS)
    return (LEGACY_WATERMARK not in journal and isinstance(keys, list) and len(keys) <= PROCESSED_LIMIT
            and all(type(key) is str and key for key in keys) and len(set(keys)) == len(keys))


def _upgraded_journal(journal) -> dict:
    # Schema 1 ordered requests by a client CT watermark; the start and result keys it still holds stay processed.
    entries = [*journal["history"], journal.get("active") or {}]
    keys = (entry.get(name) for entry in entries for name in REQUEST_KEYS)
    processed = list(dict.fromkeys(key for key in keys if type(key) is str and key))
    upgraded = deepcopy({name: value for name, value in journal.items() if name != LEGACY_WATERMARK})
    return {**upgraded, "schema_version": JOURNAL_SCHEMA_VERSION, PROCESSED_KEYS: processed[-PROCESSED_LIMIT:]}


def journal(state: dict) -> dict:
    saved = state.get(JOURNAL_KEY)
    if saved is None:
        return {"schema_version": JOURNAL_SCHEMA_VERSION, "next_sequence": 1, PROCESSED_KEYS: [],
                "active": None, "history": []}
    if (not isinstance(saved, dict) or type(saved.get("schema_version")) is not int
            or saved["schema_version"] not in (LEGACY_JOURNAL_SCHEMA_VERSION, JOURNAL_SCHEMA_VERSION)
            or type(saved.get("next_sequence")) is not int
            or not 1 <= saved["next_sequence"] <= INT32_MAX
            or not isinstance(saved.get("history"), list) or len(saved["history"]) > HISTORY_LIMIT
            or any(not isinstance(item, dict) for item in saved["history"])
            or (saved.get("active") is not None and not isinstance(saved["active"], dict))
            or not PresentBacklog.valid(saved.get("pending_presents", []))
            or not _progress_valid(saved.get(MODE_PROGRESS, {})) or not _requests_valid(saved)):
        _invalid_state("Unsupported battle journal")
    if saved["schema_version"] == LEGACY_JOURNAL_SCHEMA_VERSION:
        return _upgraded_journal(saved)
    return deepcopy(saved)


def active_identity(state: dict) -> tuple | None:
    active = journal(state)["active"]
    if not active:
        return None
    return active.get("master_snapshot"), active.get("resource_version"), active.get("start_key")


def _remember(current: dict, entry: dict) -> None:
    current["history"] = [*current["history"], entry][-HISTORY_LIMIT:]


def _mark_processed(current: dict, key: str) -> None:
    current[PROCESSED_KEYS] = [*current[PROCESSED_KEYS], key][-PROCESSED_LIMIT:]


def _close(current: dict, kind: str, completed_at: float, **receipt) -> None:
    active = current["active"]
    _remember(current, {"kind": kind, "quest_id": active.get("quest_id"), "start_key": active.get("start_key"),
                        "sequence": active.get("sequence"), **receipt, "completed_at": completed_at})
    current["active"] = None


def abort(state: dict, now: datetime) -> dict:
    current = journal(state)
    if not current["active"]:
        _conflict("quest_attempt_missing", "No active attempt to abort")
    _close(current, ABORTED, instant(now).timestamp())
    updated = deepcopy(state)
    updated[JOURNAL_KEY] = current
    return updated


class QuestService:
    def __init__(self, catalog: QuestCatalog, bootstrap: BootstrapService,
                 players: PlayerStateService, possessions: PossessionService, *,
                 rules: QuestRules | None = None, clock=None, randbelow=secrets.randbelow):
        self.catalog = catalog
        self.bootstrap = bootstrap
        self.players = players
        self.possessions = possessions
        self.rules = rules or QuestRules()
        self.clock = bootstrap.clock if clock is None else clock
        self.randbelow = randbelow
        self.missions = None
        self.rank_rewards = None
        self.presents = None
        self.card_status = None
        self.tower = None
        self.subjugation = None
        self.image_aliases = None
        self.event_rewards = BattleEventRewards()
        self.event_missions = EventMissionCatalog(catalog)
        self.platforms = PlatformTally()

    def handlers(self):
        start, finish = self.start, self.finish
        return {77: start, 78: start, 81: start, 82: start, 80: finish, 83: finish, "quest_exit": self.exit}

    def status(self):
        return {"implemented_operations": [77, 78, 81, 82, 80, 83, "quest_exit"],
                "scope": "Registered quest modes with star, four-star or score results; client reports battle outcome",
                "catalog": self.catalog.status(), "local_rules": asdict(self.rules),
                "official_server_rules": False,
                "registration_pairs": {str(key): value for key, value in REGISTRATION_PAIRS.items()},
                "difficulty_policy": "78/82 open the four-star challenge without drop candidates; results accept 1, 2 or 4 stars",
                "restart_policy": "A new start replaces the active attempt without a charge; only the clear that "
                                  "settles an attempt charges its entry costs",
                "exit_policy": "Failure and retreat close the attempt without rewards or charges; attempts saved before "
                               "settlement charges keep the entry costs their start paid; short duplicate-exit "
                               "acknowledgement window",
                "charge_policy": "Starts precheck charisma and action points for every Boost.V run without charging; a "
                                 "successful result charges the costs frozen at the start once per run, after recovery and "
                                 "before experience and rank-up refills, and a balance spent below them stops at zero with "
                                 "a warning; attempts saved before settlement charges were charged at their start and are "
                                 "neither charged again nor refunded",
                "revision_policy": "Active attempts require the same master and resource snapshot after restart",
                "story_policy": "Preserve narrative and tutorial flags; no inferred completion acknowledgement",
                "capacity_policy": "Require a free primary slot at entry; preflight potential unit rewards against technical limits; allow settlement over the business cap",
                "boost_policy": f"The mode's start boost hook returns {UNBOOSTED} run or the requested Boost.V up to "
                                f"{MAX_BOOST}: a boosted clear charges charisma and action points and credits base "
                                "experience, gold and quest tickets that many times without drawing drops again; "
                                "Boost.V the mode leaves unboosted is accepted, ignored and saved as boost_ignored",
                "score_policy": "Score modes settle through the mode's score hook: TT repeats the kill count, the record "
                                "keeps CLEAR and this run's slot bits without star bits or star rewards, MaxRank stores the "
                                "hook's packed value, and mission events see zero stars with the reported kills",
                "star_reward_policy": "First star rewards follow QuestTable BonusType/BonusNum or HardMode reward fields",
                "crystal_overflow_policy": "Free crystals beyond 1000 become one present; with a full mailbox battle presents "
                                           "wait in the battle journal as copies, each later mailed with its original amount",
                "item_overflow_policy": "Items beyond Stack are discarded and reported through IOF; Stack 0 items are bounded only by int32",
                "prize_slot_policy": "Obtained slots 1-5 set record bits and grant nonzero treasures; other slots grant nothing; "
                                     "7000-7999 drops with an override code grant that code without a record bit",
                "candidate_policy": "Starts select prize candidates by the quest record's raw MaxRank and save it; results "
                                    "restore that set and refuse more than seven obtained drops",
                "drop_bonus_policy": "DropBonusConfig scales this result's obtained-slot drops of its item once by the JST "
                                     "weekday value of the attempt start and reports BONUS; star, collection, mode and "
                                     "four-star rewards are never scaled; a table fault grants no bonus",
                "event_reward_policy": "The shared battle event hook adds quest tickets of the vote group open at the "
                                       "attempt start to the single capped credit and rewrites EventParam to "
                                       f"min(previous + event drops, {EVENT_PARAM_LIMIT}) on every result",
                "event_rewards": self.event_rewards.status(),
                "event_mission_reward_policy": "Starts refuse with 501 before any charge when the admitted mission "
                                               "shows score, emergency or after-event rewards that no mode settles",
                "event_mission_rewards": self.event_missions.status(),
                "gold_event_policy": "Each settlement counts min(GoldGet, GG.I, max_gold_events) gold events worth "
                                     "gold_per_event raised by the strongest Gold Get Plus percent among the saved "
                                     "sortie units",
                "experience_policy": "Config.ExperienceUpCloseDateTime later than the JST clock credits (int)(RankExp * 1.2f)",
                "money_policy": "Each run credits floor((Gold * gold_percent / 100 + gold events) * star rate * (100 + "
                                "strongest reward-gold percent) / 100 * MoneyUp rate): two and three stars pay 1.2 and "
                                "1.5 times, a four-star result pays like three stars, score results take no star rate, "
                                "Config.MoneyUpCloseDateTime later than the JST clock pays 1.5 times, and gold beyond "
                                "the cap is discarded",
                "gold_ability_policy": "Reward-gold and Gold Get Plus percents come from the saved units the BL rows "
                                       "name, each through the CardData Ability_Default or Ability of its image card "
                                       "that its class stage selects; abilities of one kind never stack",
                "gold_abilities": self._gold_ability_status(),
                "platform_policy": "Starts count every PlatformCode inside the native range after parsing, in memory "
                                   "only, including exact replays and later refusals",
                "platform_codes_seen": self.platforms.status(),
                "mode_hook_policy": "prepare_start and settle results commit in the same CAS as the base attempt; "
                                    "settle item_ids must name known items; settle presents share the mailbox queue "
                                    "and the single PPT; settle hooks keep server-private progress in the battle "
                                    "journal under their policy name",
                "mode_reward_delivery_policy": "Enabled modes whose starts preflight their rewards list each reward the "
                                               "attached present service cannot mail; a start that can reach one "
                                               "refuses with 501 before charging, and summon ticket rewards need their "
                                               "pools in the item ticket rules",
                "mode_reward_delivery": self._reward_delivery(),
                "mission_team_policy": "Mission formation rules count BL rows whose GetIndex, UniqueID and CardID match "
                                       "the saved roster, excluding the prince; other rows are logged, never rejected",
                "fixed_team_policy": "HL rows must match the quest's QuestUnitTable members in CardID % 10000, GetIndex "
                                     f"from UX+{FIXED_INDEX_OFFSET} and CostMod; BL stand-ins copy the first saved prince, "
                                     f"and beside a fixed team only Type {HARMONY_TYPE} keeps them, at most "
                                     f"{FIXED_TEAM_STAND_INS[HARMONY_TYPE]}; differences are logged, never rejected",
                "replay_policy": "Exact response replay only before another profile mutation",
                "request_order": REQUEST_ORDER, "processed_request_limit": PROCESSED_LIMIT,
                "energy_clock": {"encoding": ENCODING, "utc_offset_minutes": UTC_OFFSET_MINUTES},
                "initial_units": "Official new-account ordinary unit grants remain unresolved"}

    def _now(self):
        return instant(self.clock())

    _journal = staticmethod(journal)

    @staticmethod
    def _settled_progress(journal, state, policy) -> dict:
        returned = state.get(JOURNAL_KEY)
        settled = returned.get(MODE_PROGRESS, {}) if isinstance(returned, dict) else None
        if not _progress_valid(settled):
            raise ProtocolError("quest_mode_hook_invalid", "Mode settlement hook returned invalid progress", 503)
        progress = {name: entry for name, entry in journal.get(MODE_PROGRESS, {}).items() if name != policy.name}
        if policy.name in settled:
            progress[policy.name] = deepcopy(settled[policy.name])
        return progress

    def _records(self, profile):
        rows = self.bootstrap._rows(profile, "quest_records", single=False)
        records = {row["QuestID"]: row for row in rows}
        if len(records) != len(rows):
            _invalid_state("Quest records contain duplicate identifiers")
        return records

    @staticmethod
    def _record(records, quest_id):
        return records.get(quest_id, {"QuestID": quest_id, **UNPLAYED})

    @staticmethod
    def _key(envelope):
        return envelope.profile.route + ":" + envelope.digest

    def _cached(self, envelope, profile, entry):
        validate_clock(profile.state)
        self._check_snapshot(entry)
        if entry.get("response_revision") != profile.revision:
            _conflict("quest_response_stale", "The profile changed after this response; reload current player data")
        response = entry.get("response")
        if not isinstance(response, str) or len(response.encode("utf-8")) > RESPONSE_CACHE_LIMIT:
            _invalid_state("Invalid cached battle response")
        result = response.encode("utf-8")
        encode_response(envelope.profile, result)
        return result

    @staticmethod
    def _prepare(envelope, children):
        response = dumps(Container(children=tuple(children)))
        encode_response(envelope.profile, response)
        if len(response) > RESPONSE_CACHE_LIMIT:
            raise ProtocolError("quest_response_too_large", "Battle response exceeds the local replay cache limit", 503)
        return response

    @staticmethod
    def _reward(experience=0, money=0):
        return Container("REWARD", (Column("EXPERIENCE", "I", (experience,)), Column("GOLD", "I", (money,))))

    @staticmethod
    def _collection_ready(quest, flags):
        return quest.treasures[3] == 0 and all(quest.treasures[index] > 0 for index in (0, 1, 2, 4)) and not flags & 1

    @staticmethod
    def _star_snapshot(mode, difficulty):
        rewards = mode.star_rewards_hard if difficulty == "hard" else mode.star_rewards_normal
        return [asdict(reward) for reward in rewards]

    def _preflight_unit_rewards(self, state, quest, flags, rules, *, candidates=None, star_rewards=()):
        rows = quest.candidates if candidates is None else candidates
        potential = [(code, quest.unit_level) for code in map(quest.reward_code, rows) if code]
        if self._collection_ready(quest, flags):
            potential.append((quest.treasures[4], rules.collection_unit_level))
        grants = []
        for code, level in potential:
            reward = self.catalog.decode_reward(code)
            if reward.kind == "unit":
                grants.append({"card_id": reward.card_id, "level": level, "count": reward.count})
        grants.extend({"card_id": reward["card_id"], "level": quest.unit_level, "count": reward["count"]}
                      for reward in star_rewards if reward["kind"] == "unit")
        self.players.credit_units(state, grants)
        return grants

    def _present_service(self):
        if self.presents is not None:
            return self.presents
        return getattr(self.rank_rewards, "presents", None)

    @staticmethod
    def _star_crystals(star_rewards, rules):
        return sum(reward["count"] if reward["count"] > 0 else rules.first_three_star_crystals
                   for reward in star_rewards if reward["kind"] == "crystal")

    def _campaign_open(self, profile, now, field):
        # 6604F0 and 660440 compare the close text with the JST wall clock.
        value = self.bootstrap._rows(profile, "config")[0][field]
        return value != "" and datetime.strptime(value, NATIVE_DATETIME).replace(tzinfo=JST) > now

    def gold_abilities(self) -> GoldAbilities:
        with _GOLD_ABILITIES_LOCK:
            abilities = _GOLD_ABILITIES.get(self.catalog)
            if abilities is None:
                abilities = _GOLD_ABILITIES[self.catalog] = _load_gold_abilities(self.catalog, self.players.master_schemas)
            return abilities

    def _gold_ability_status(self) -> dict:
        try:
            abilities = self.gold_abilities()
        except ProtocolError as error:
            return {"ready": False, "error": error.code}
        return {"ready": True, **abilities.status(), "image_aliases": self.image_aliases is not None}

    def _image_card(self, card_id: int) -> int:
        return card_id if self.image_aliases is None else self.image_aliases.resolve(card_id).image_card_id

    def gold_percents(self, units) -> GoldPercents:
        units = tuple(units)
        return self.gold_abilities().team(units, self._image_card) if units else NO_GOLD_ABILITY

    def experience_reward(self, quest: QuestDefinition, rules: QuestRules, profile: PlayerProfile, now: datetime, *,
                          experience_up: bool = True) -> int:
        _require_aware(now)
        experience = quest.rank_exp * rules.experience_percent // 100
        if experience_up and self._campaign_open(profile, now, EXPERIENCE_CAMPAIGN):
            experience = boosted_experience(experience)
        return experience

    def gold_reward(self, quest: QuestDefinition, rules: QuestRules, profile: PlayerProfile, now: datetime, *,
                    stars: int = UNRATED_STARS, events: int = 0, units=(), money_up: bool = True) -> int:
        _require_aware(now)
        return _settled_gold(quest, rules, events, stars, self.gold_percents(units),
                             money_up and self._campaign_open(profile, now, MONEY_CAMPAIGN))

    def base_rewards(self, quest: QuestDefinition, rules: QuestRules, profile: PlayerProfile, now: datetime, *,
                     experience_up: bool = True, money_up: bool = True) -> tuple[int, int]:
        return (self.experience_reward(quest, rules, profile, now, experience_up=experience_up),
                self.gold_reward(quest, rules, profile, now, money_up=money_up))

    def preview_event_rewards(self, quest_id: int, *, source: str, runs: int, now: datetime, bonus_instant: datetime,
                              drop_items: Mapping[str, int], state: dict, event_points: int = 0) -> dict:
        if (type(source) is not str or source not in EVENT_PARAM_REWRITE or not int32(runs) or runs < 1
                or not int32(event_points) or event_points < 0 or not isinstance(drop_items, Mapping)
                or any(type(item_id) is not str or not item_id or not int32(count) or count < 0
                       for item_id, count in drop_items.items())
                or not isinstance(now, datetime) or not isinstance(bonus_instant, datetime) or not isinstance(state, dict)):
            raise ProtocolError("quest_event_preview_invalid", "Event reward previews take a known source, native run, "
                                "point and item counts, two datetimes and a player state")
        quest, mode = self.catalog.get(quest_id), self.catalog.mode(quest_id)
        moment = instant(bonus_instant)
        bonus = None if mode.drop_bonus is None else bonus_grant(mode.drop_bonus, moment, drop_items)
        events = self.event_rewards.preview(state, BattleEventContext(
            source, quest.quest_id, mode.quest_type, quest.charisma, runs, event_points, now, EVENT_PARAM_REWRITE[source]))
        return {"quest_id": quest.quest_id, "source": source, "bonus_instant": moment.isoformat(timespec="seconds"),
                "drop_bonus": None if bonus is None else bonus.receipt(), "event_rewards": events.receipt}

    @staticmethod
    def _sortie(player, members) -> tuple[list[dict], int]:
        saved = {(unit["GetIndex"], unit["UniqueID"]): unit for unit in player["units"]}
        matched, foreign = {}, 0
        for member in members:
            key = member.get_index, member.unique_id
            unit = saved.get(key)
            if unit is not None and unit["CardID"] == member.card_id:
                matched.setdefault(key, unit)
            elif member.get_index - player["battle"]["UX"] not in AVATAR_INDEX_OFFSETS:
                foreign += 1
        return list(matched.values()), foreign

    @staticmethod
    def _counted_team(quest_id, units, foreign):
        if foreign:
            log.warning("Quest %d result lists %d formation rows absent from the saved roster", quest_id, foreign)
        # The prince joins every formation without a Genus, so formation conditions leave him out.
        return [{"card_id": unit["CardID"], "get_index": unit["GetIndex"], "unique_id": unit["UniqueID"]}
                for unit in units if unit["CardID"] not in PRINCE_CARD_IDS]

    def _log_team_differences(self, player, quest: QuestDefinition, quest_type: int, request: QuestFinish) -> None:
        ux = player["battle"]["UX"]
        # 4C8E40 picks the first saved prince row for the copy.
        prince = next(((unit["CardID"], unit["UniqueID"]) for unit in player["units"]
                       if unit["CardID"] in PRINCE_CARD_IDS), None)
        stand_ins = [(member.card_id, member.unique_id) for member in request.members
                     if member.get_index - ux in AVATAR_INDEX_OFFSETS]
        foreign = sum(stand_in != prince for stand_in in stand_ins)
        if foreign:
            log.warning("Quest %d result lists %d stand-in rows that do not copy the saved prince", quest.quest_id, foreign)
        if quest.unit_list and len(stand_ins) > FIXED_TEAM_STAND_INS.get(quest_type, 0):
            log.warning("Quest %d result lists %d stand-in rows beside its fixed team", quest.quest_id, len(stand_ins))
        try:
            members = self.catalog.fixed_units(quest.unit_list) if quest.unit_list else ()
        except ProtocolError as error:
            log.warning("Quest %d fixed team %d cannot be compared: %s", quest.quest_id, quest.unit_list, error.code)
            return
        reported = sorted((member.get_index - ux - FIXED_INDEX_OFFSET, member.card_id, member.cost_mod)
                          for member in request.fixed)
        if reported != [(index, unit.card_id, unit.cost_mod) for index, unit in enumerate(members)]:
            log.warning("Quest %d result HL differs from its fixed team: %d rows reported, %d members expected",
                        quest.quest_id, len(reported), len(members))

    def _services(self):
        return ModeServices(self._present_service(), self.card_status, possessions=self.possessions,
                            tower=self.tower, subjugation=self.subjugation)

    def _reward_delivery(self) -> dict:
        services = self._services()
        reports = {quest_type: self._delivery_report(policy, services)
                   for quest_type, policy in quest_modes.registry().items() if policy.enabled}
        return {quest_type: report for quest_type, report in reports.items() if report is not None}

    def _delivery_report(self, policy: ModePolicy, services: ModeServices) -> dict | None:
        try:
            undeliverable = policy.undeliverable_rewards(self.catalog, services)
        except ProtocolError as error:
            return {"ready": False, "error": error.code}
        if undeliverable is None:
            return None
        if not isinstance(undeliverable, tuple) or any(not isinstance(entry, dict) for entry in undeliverable):
            return {"ready": False, "error": "quest_mode_hook_invalid"}
        return {"ready": not undeliverable, "undeliverable": list(undeliverable)}

    @staticmethod
    def _hook_children(children):
        if not isinstance(children, tuple) or any(not isinstance(child, Container) for child in children):
            raise ProtocolError("quest_mode_hook_invalid", "Mode hooks must return native response containers", 503)
        return children

    @staticmethod
    def _start_boost(policy: ModePolicy, context: StartContext) -> int:
        boost = policy.start_boost(context)
        if type(boost) is not int or boost not in BOOSTS:
            raise ProtocolError("quest_mode_hook_invalid",
                                f"Mode boost hooks must return a native boost from {UNBOOSTED} to {MAX_BOOST}", 503)
        # 77A690 prechecks costs for the QuestBoost that 4316C0 sends as Boost.V, so a mode applies it or none of it.
        if boost != UNBOOSTED and boost != context.request.boost:
            raise ProtocolError("quest_mode_hook_invalid",
                                "Mode boost hooks must apply the requested Boost.V or leave the start unboosted", 503)
        return boost

    @staticmethod
    def _mode_state(mode: QuestMode, difficulty: str, star_rewards: list, boost: int, requested: int | None) -> dict:
        state = {"quest_type": mode.quest_type, "difficulty": difficulty, "star_rewards": star_rewards,
                 "boost_ignored": requested if boost == UNBOOSTED else None}
        # Unboosted attempts keep the four legacy keys, so builds that predate start boosts still settle them.
        if boost != UNBOOSTED:
            state["boost"] = boost
        return state

    def start(self, envelope: RequestEnvelope, store: Store) -> bytes:
        request = parse_start(envelope)
        platform = quest_modes.require_platform(platform_code(envelope))
        self.platforms.record(platform)
        registration = envelope.profile.registration_keys[0]
        difficulty = "hard" if registration in HARD_REGISTRATIONS else "normal"
        quest = self.catalog.get(request.quest_id)
        mode = self.catalog.mode(request.quest_id)
        policy = quest_modes.policy(mode.quest_type)
        policy.require_enabled()
        star_rewards = self._star_snapshot(mode, difficulty)
        key, now = self._key(envelope), self._now()
        for _ in range(3):
            profile = self.bootstrap._authenticate(envelope, store)
            journal = self._journal(profile.state)
            active = journal["active"]
            if active and active.get("start_key") == key:
                return self._cached(envelope, profile, active)
            if any(entry.get("start_key") == key for entry in journal["history"]):
                _conflict("quest_attempt_closed", "This start request belongs to a closed attempt")
            if key in journal[PROCESSED_KEYS]:
                _conflict("quest_request_stale", "This start request was already processed")
            records = self._records(profile)
            candidate_stars = self._record(records, quest.quest_id)["MaxRank"]
            candidates = () if difficulty == "hard" else quest.candidates_for(candidate_stars)
            if len(candidates) > MAX_CANDIDATES:
                raise ProtocolError("quest_candidates_overflow", "Quest exceeds the verified result mask width", 501)
            player = self.bootstrap._player_row(profile)
            context = StartContext(self.catalog, quest, mode, records, player["Rank"], now, difficulty == "hard",
                                   platform, deepcopy(profile.state), request, self._services())
            mission = quest_modes.admit(policy, context)
            self.event_missions.require_settled(quest.quest_id, mission)
            boost = self._start_boost(policy, context)
            old_flags = self._record(records, quest.quest_id)["AchieveFlag"] & UINT32_MASK
            expected = quest_modes.candidate_attributes(records.get(quest.quest_id), candidates)
            actual = tuple((candidate.id, candidate.slot, candidate.attributes) for candidate in request.candidates)
            if actual != expected:
                _conflict("quest_candidates_mismatch", "Client candidates do not match the saved quest and map")
            _, star_flag = FIRST_STARS[difficulty]
            pending_stars = [] if old_flags & star_flag else star_rewards
            if any(reward["kind"] == "unsupported" for reward in pending_stars):
                raise ProtocolError("quest_star_reward_unsupported", "This quest's first star reward type is not implemented", 501)
            rules = self.rules
            capacity_state = deepcopy(profile.state)
            if JOURNAL_KEY in capacity_state:
                capacity_state[JOURNAL_KEY]["active"] = None
            self.players.require_unit_space(capacity_state, 1)
            reservations = self._preflight_unit_rewards(capacity_state, quest, old_flags, rules,
                                                        candidates=candidates, star_rewards=pending_stars)
            crystals = self._star_crystals(pending_stars, rules)
            if crystals and not free_crystals_fit(capacity_state, crystals) and self._present_service() is None:
                _conflict("quest_present_unavailable", "Overflowing star crystals require the present service")
            updated = require_quest_energy(profile.state, self.bootstrap.rank_rows, now, charisma=quest.charisma * boost,
                                           action_points=quest.action_points * boost,
                                           recovery_seconds=rules.charisma_recovery_seconds)
            prepared = policy.prepare_start(context, updated)
            if not isinstance(prepared, tuple) or len(prepared) != 2 or not isinstance(prepared[0], dict):
                raise ProtocolError("quest_mode_hook_invalid", "Mode start hook must return a state and children", 503)
            updated, start_children = prepared[0], self._hook_children(prepared[1])
            lottery = []
            for _candidate in candidates:
                roll = self.randbelow(10000)
                if type(roll) is not int or not 0 <= roll < 10000:
                    raise ValueError("Quest random source returned an invalid roll")
                lottery.append(int(roll < rules.drop_basis_points))
            response = self._prepare(envelope, (self.bootstrap._system(profile), game_param_response(updated),
                Container("LOTTERY", (Column("Index", "I", tuple(range(len(lottery)))), Column("Item", "I", tuple(lottery)))),
                self._reward(), Container("TREASURE"), Container("TreasureInfo"), Container("PPU"), *start_children))
            if active:
                _remember(journal, {"kind": "superseded", "quest_id": active.get("quest_id"),
                                    "start_key": active.get("start_key"), "sequence": active.get("sequence")})
            if journal["next_sequence"] == INT32_MAX:
                _invalid_state("Battle sequence capacity exhausted")
            journal["active"] = {"sequence": journal["next_sequence"], "quest_id": quest.quest_id,
                "start_key": key, "registration": registration,
                "client_time": request.client_time, "client_revision": request.revision,
                "started_at": now.timestamp(), "master_snapshot": self.catalog.snapshot.identifier,
                "resource_version": self.catalog.resource_version, "definition": asdict(quest),
                "rules": asdict(rules), "selected_mask": sum(value << index for index, value in enumerate(lottery)),
                "candidate_stars": candidate_stars, "unit_reservations": reservations, "mission_id": mission.mission_id,
                "mode": self._mode_state(mode, difficulty, star_rewards, boost, request.boost),
                CHARGE_KEY: SETTLEMENT_CHARGE,
                "response": response.decode("utf-8"), "response_revision": profile.revision + 1}
            # JSON stores tuples as arrays; normalize the frozen definition before comparing after reload.
            journal["active"]["definition"]["treasures"] = list(quest.treasures)
            journal["active"]["definition"]["candidates"] = [asdict(value) for value in quest.candidates]
            journal["next_sequence"] += 1
            _mark_processed(journal, key)
            updated[JOURNAL_KEY] = journal
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        _conflict("profile_changed", "Player state changed while starting the quest")

    @staticmethod
    def _saved_definition(definition):
        candidates = definition.get("candidates") if isinstance(definition, dict) else None
        if not isinstance(candidates, list):
            return definition
        return {**definition, "candidates": [{**CANDIDATE_DEFAULTS, **row} if isinstance(row, dict) else row
                                             for row in candidates]}

    def _active_definition(self, active):
        self._check_snapshot(active)
        quest = self.catalog.get(active.get("quest_id"))
        expected = asdict(quest)
        expected["treasures"] = list(quest.treasures)
        expected["candidates"] = [asdict(value) for value in quest.candidates]
        if self._saved_definition(active.get("definition")) != expected:
            _invalid_state("Stored quest definition does not match its fixed catalog")
        try:
            values = dict(active["rules"])
            # Attempts saved before GoldGet credits settle without the per-event amount.
            values.setdefault("gold_per_event", 0)
            rules = QuestRules(**values)
        except (KeyError, TypeError, ValueError) as error:
            raise ProtocolError("quest_state_invalid", "Stored quest rules are invalid", 503) from error
        mode = self.catalog.mode(quest.quest_id)
        stored = active.get("mode")
        hard_registration = active.get("registration") in HARD_REGISTRATIONS
        if stored is None:
            if mode.quest_type != 0 or hard_registration:
                _invalid_state("Battle attempts without a saved mode must be ordinary main quests")
            stored = {"quest_type": 0, "difficulty": "normal", "star_rewards": self._star_snapshot(mode, "normal"),
                      "boost_ignored": None}
        if (not isinstance(stored, dict) or set(stored) not in (LEGACY_MODE_KEYS, MODE_KEYS)
                or stored["quest_type"] != mode.quest_type
                or stored["difficulty"] not in STAR_RESULTS or (stored["difficulty"] == "hard") != hard_registration
                or stored["star_rewards"] != self._star_snapshot(mode, stored["difficulty"])
                or (stored["boost_ignored"] is not None and (type(stored["boost_ignored"]) is not int
                                                             or stored["boost_ignored"] <= 0))):
            _invalid_state("Stored battle mode does not match its fixed catalog")
        boost = stored.get("boost", UNBOOSTED)
        # Starts save boost_ignored only for unboosted attempts, so a boosted one carries None there.
        if type(boost) is not int or boost not in BOOSTS or (boost != UNBOOSTED and stored["boost_ignored"] is not None):
            _invalid_state("Stored battle boost does not match a native start")
        return quest, rules, {**stored, "boost": boost}

    @staticmethod
    def _charged_at_settlement(active) -> bool:
        # Attempts saved before settlement charges paid their entry costs at the start.
        if CHARGE_KEY not in active:
            return False
        if active[CHARGE_KEY] != SETTLEMENT_CHARGE:
            _invalid_state("Stored battle charge does not match a native start")
        return True

    def _check_snapshot(self, active):
        if (active.get("master_snapshot") != self.catalog.snapshot.identifier
                or active.get("resource_version") != self.catalog.resource_version):
            _conflict("quest_snapshot_changed", "Restore this attempt's master and resource snapshot before settlement")

    @staticmethod
    def _saved_candidates(quest: QuestDefinition, active) -> tuple[DropCandidate, ...]:
        if "candidate_stars" in active:
            return quest.candidates_for(active["candidate_stars"])
        # Attempts saved without the record's MaxRank started with every candidate; only conditional rows need it.
        if quest.conditional:
            _invalid_state("Conditional prize candidates require the MaxRank saved at the attempt start")
        return quest.candidates

    @staticmethod
    def _started_at(active) -> datetime:
        value = active.get("started_at")
        try:
            if type(value) not in (int, float):
                raise TypeError("Attempt start times are POSIX timestamps")
            moment = datetime.fromtimestamp(value, JST)
            # A start the client calendar cannot date is a damaged save, not a clock the request brought.
            civil_date(moment)
        except (TypeError, ValueError, OverflowError, OSError, ProtocolError) as error:
            raise ProtocolError("quest_state_invalid", "Saved attempt start time is invalid", 503) from error
        return moment

    @staticmethod
    def _drop_bonus(mode: QuestMode, difficulty: str, started_at: datetime,
                    items: Mapping[str, int]) -> DropBonusGrant | None:
        # Four-star challenges start without prize candidates, so DropBonus never reaches them.
        if difficulty == "hard" or mode.drop_bonus is None:
            return None
        # The attempt's start day decides the weekday value; the official choice is not captured yet.
        return bonus_grant(mode.drop_bonus, started_at, items)

    @staticmethod
    def _result_stars(request, mode: QuestMode, difficulty: str) -> int:
        if not mode.score_mode:
            if request.stars not in STAR_RESULTS[difficulty]:
                raise ProtocolError("quest_result_invalid", "Result stars are outside the native set for this difficulty")
            return request.stars
        # 462850 repeats the kill count in place of stars for score types, which have no four-star challenge.
        if request.stars != request.kills or difficulty != "normal":
            raise ProtocolError("quest_result_invalid", "Score results must repeat the kill count of a normal start")
        return 0

    @staticmethod
    def _score_result(policy: ModePolicy, context: ScoreContext) -> ScoreResult:
        result = policy.score_result(context)
        if (not isinstance(result, ScoreResult) or not int32(result.max_rank) or result.max_rank < 0
                or type(result.score) is not int or result.score < 0 or not isinstance(result.details, Mapping)):
            raise ProtocolError("quest_mode_hook_invalid",
                                "Score hooks must return a nonnegative native MaxRank, score and details", 503)
        try:
            # The journal stores details as JSON, so they are kept exactly as a reload returns them.
            details = json.loads(json.dumps(dict(result.details), allow_nan=False), object_pairs_hook=reject_duplicate_keys)
        except (TypeError, ValueError, RecursionError) as error:
            raise ProtocolError("quest_mode_hook_invalid", "Score details must be JSON values", 503) from error
        return replace(result, details=details)

    def finish(self, envelope: RequestEnvelope, store: Store) -> bytes:
        request = parse_finish(envelope)
        raw_now = self.clock()
        key, now = self._key(envelope), instant(raw_now)
        first_clear_day, acquisition_day = business_day(now), civil_day(now)
        for _ in range(3):
            profile = self.bootstrap._authenticate(envelope, store)
            journal = self._journal(profile.state)
            for entry in reversed(journal["history"]):
                if entry.get("finish_key") == key:
                    return self._cached(envelope, profile, entry)
            if key in journal[PROCESSED_KEYS]:
                _conflict("quest_request_stale", "This result was already processed")
            active = journal["active"]
            if not active or active.get("quest_id") != request.quest_id:
                _conflict("quest_attempt_missing", "Result does not match an active attempt")
            if envelope.profile.registration_keys[0] != REGISTRATION_PAIRS.get(active.get("registration")):
                _conflict("quest_result_profile_mismatch", "Result profile does not match the start profile")
            quest, rules, mode_state = self._active_definition(active)
            charge_at_settlement = self._charged_at_settlement(active)
            policy = quest_modes.policy(mode_state["quest_type"])
            difficulty, boost = mode_state["difficulty"], mode_state["boost"]
            mode = self.catalog.mode(quest.quest_id)
            stars = self._result_stars(request, mode, difficulty)
            candidates = () if difficulty == "hard" else self._saved_candidates(quest, active)
            mask_limit = (1 << len(candidates)) - 1
            selected = active.get("selected_mask")
            if type(selected) is not int or not 0 <= selected <= mask_limit:
                _invalid_state("Saved lottery is outside the quest candidate mask")
            if (request.selected_mask != selected or request.obtained_mask & ~mask_limit
                    or request.obtained_mask & ~selected):
                _conflict("quest_drop_mismatch", "Result drops do not match the saved lottery")
            if request.obtained_mask.bit_count() > MAX_OBTAINED:
                _conflict("quest_drop_mismatch", "Result obtains more drops than one battle records")
            records = self._records(profile)
            record = deepcopy(self._record(records, quest.quest_id))
            old_flags = record["AchieveFlag"] & UINT32_MASK
            flags = old_flags | CLEAR_FLAG
            # DropBonus scales only obtained-slot drops, so each reward records whether it is one.
            rewards = []
            for index, candidate in enumerate(candidates):
                if not request.obtained_mask & (1 << index):
                    continue
                if quest.marks_slot(candidate):
                    flags |= SLOT_FLAGS[candidate.slot - 1]
                code = quest.reward_code(candidate)
                if code:
                    rewards.append((code, quest.unit_level, True))
            # 460020 writes this run's slot bits first; slot 5 shares bit0 with the collection reward.
            if self._collection_ready(quest, flags) and flags & COLLECTION_SLOTS == COLLECTION_SLOTS:
                flags |= 1
                rewards.append((quest.treasures[4], rules.collection_unit_level, False))
            star_count, star_flag = FIRST_STARS[difficulty]
            first_star = stars == star_count and not old_flags & star_flag
            if stars == star_count:
                flags |= star_flag
            previous, score = deepcopy(record), None
            if mode.score_mode:
                score = self._score_result(policy, ScoreContext(self.catalog, quest, mode, request, deepcopy(previous),
                                                                boost, now, active.get("mission_id"), self._services()))
            record.update(MaxRank=max(record["MaxRank"], stars) if score is None else score.max_rank,
                          AchieveFlag=signed32(flags))
            if not old_flags & CLEAR_FLAG and record["FirstDefeatDate"] == 0:
                record["FirstDefeatDate"] = first_clear_day
            gold_events = (min(request.gold_reported, request.gold_events, rules.max_gold_events)
                           if mode.gold_counted else 0)
            player = self.players.validate(profile.state)
            sortie, foreign = self._sortie(player, request.members)
            experience = self.experience_reward(quest, rules, profile, now)
            gold_stars = UNRATED_STARS if mode.score_mode else min(stars, MAX_GOLD_STARS)
            percents, money_up = self.gold_percents(sortie), self._campaign_open(profile, now, MONEY_CAMPAIGN)
            money = _settled_gold(quest, rules, gold_events, gold_stars, percents, money_up)
            costs = {"charisma": quest.charisma * boost, "action_points": quest.action_points * boost}
            if charge_at_settlement:
                updated, charged = charge_quest(profile.state, self.bootstrap.rank_rows, now, **costs,
                                                recovery_seconds=rules.charisma_recovery_seconds)
                if charged != costs:
                    log.warning("Quest %d settles %d charisma and %d action points short of the costs frozen at its "
                                "start; the balance stops at zero", quest.quest_id,
                                costs["charisma"] - charged["charisma"], costs["action_points"] - charged["action_points"])
            else:
                updated = settle_energy(profile.state, self.bootstrap.rank_rows, now,
                                        recovery_seconds=rules.charisma_recovery_seconds)
                charged = dict.fromkeys(costs, 0)
            # Credits past the last rank or the gold cap are discarded, so larger sums settle like the caps themselves.
            updated = credit_quest(updated, self.bootstrap.rank_rows, now, experience=min(experience * boost, INT32_MAX),
                money=min(money * boost, GOLD_LIMIT),
                refill_on_rank_up=rules.refill_on_rank_up, recovery_seconds=rules.charisma_recovery_seconds)
            records[quest.quest_id] = record
            updated["tables"]["quest_records"] = list(records.values())
            unit_grants, item_grants, drop_items, reward_counts, event_points = [], Counter(), Counter(), Counter(), 0
            for code, level, slot_drop in rewards:
                reward = self.catalog.decode_reward(code)
                if reward.kind == "unit":
                    unit_grants.append({"card_id": reward.card_id, "level": level, "count": reward.count})
                elif reward.kind == "item":
                    item_grants[reward.item_id] += reward.count
                    if slot_drop:
                        drop_items[reward.item_id] += reward.count
                elif reward.kind == "event_param":
                    event_points += reward.count
                reward_counts[code] += reward.count
            started_at = self._started_at(active)
            bonus = self._drop_bonus(mode, difficulty, started_at, drop_items)
            if bonus is not None:
                item_grants.update(bonus.increments)
            granted_stars = mode_state["star_rewards"] if first_star else []
            for reward in granted_stars:
                if reward["kind"] == "unit":
                    unit_grants.append({"card_id": reward["card_id"], "level": quest.unit_level, "count": reward["count"]})
                elif reward["kind"] == "item":
                    item_grants[reward["item_id"]] += reward["count"]
                elif reward["kind"] == "unsupported":
                    raise ProtocolError("quest_star_reward_unsupported", "This quest's first star reward type is not implemented", 501)
            updated[JOURNAL_KEY]["active"] = None
            updated, new_units = self.players.credit_units(updated, unit_grants)
            # Officially a clear across a vote period switch earns the tickets of the group open at the attempt start.
            events = self.event_rewards.evaluate(updated, BattleEventContext(
                RESULT, quest.quest_id, mode_state["quest_type"], quest.charisma, boost, event_points, started_at,
                EVENT_PARAM_REWRITE[RESULT]))
            item_grants.update(events.items)
            discarded = {}
            if item_grants:
                updated, discarded = self.possessions.credit_capped(updated, dict(item_grants))
            updated["tables"]["game_param"][0]["EventParam"] = events.event_param
            presents = self._present_service()
            backlog = PresentBacklog(presents, journal.get("pending_presents", ()))
            updated, delivered = backlog.flush(updated)
            crystals = self._star_crystals(granted_stars, rules)
            wallet_crystals, star_present_ids, deferred = 0, [], 0
            if crystals:
                if free_crystals_fit(updated, crystals):
                    updated = credit_free_crystals(updated, crystals, acquisition_day)
                    wallet_crystals = crystals
                else:
                    reward = {"kind": "free_crystal", "amount": crystals, "description": STAR_PRESENT_TEXT[difficulty]}
                    updated, star_present_ids, queued = backlog.send(updated, reward)
                    deferred = crystals if queued else 0
            outcome = policy.settle(SettleContext(self.catalog, quest, mode, request, updated, difficulty == "hard",
                stars, now, old_flags, flags, not old_flags & CLEAR_FLAG, active.get("mission_id"),
                self._services(), boost=boost, score=None if score is None else score.score, previous_record=previous))
            if (not isinstance(outcome, SettleOutcome) or not isinstance(outcome.state, dict)
                    or not isinstance(outcome.item_ids, tuple) or not isinstance(outcome.presents, tuple)
                    or any(not self.possessions.known(item_id) for item_id in outcome.item_ids)
                    or any(not reward_valid(reward) for reward in outcome.presents)):
                raise ProtocolError("quest_mode_hook_invalid", "Mode settlement hook returned an invalid outcome", 503)
            updated, settle_children = outcome.state, self._hook_children(outcome.children)
            progress = self._settled_progress(journal, updated, policy)
            mode_present_ids, mode_pending = [], 0
            for reward in outcome.presents:
                updated, added, queued = backlog.send(updated, dict(reward))
                mode_present_ids.extend(added)
                mode_pending += int(queued)
            if request.damage_display is not None:
                updated = patch_configuration(updated, {"DamageDispEnable": request.damage_display})
            self._log_team_differences(player, quest, mode_state["quest_type"], request)
            if self.missions is not None:
                updated = self.missions.record(updated, "quest_clear", {
                    "quest_id": quest.quest_id, "quest_type": mode_state["quest_type"], "stars": stars,
                    "hard": difficulty == "hard", "mission_id": active.get("mission_id"),
                    "team": self._counted_team(quest.quest_id, sortie, foreign), "score_mode": mode.score_mode,
                    "kills": request.kills}, now)
            rank_mail_ids = ()
            if self.rank_rewards is not None:
                updated, rank_mail_ids = self.rank_rewards.apply(profile.state, updated, profile)
            final_profile = replace(profile, state=updated)
            self.bootstrap._rows(final_profile, "quest_records", single=False)
            before, after = self.bootstrap._player_row(profile), self.bootstrap._player_row(final_profile)
            children = [self.bootstrap._system(profile), game_param_response(updated, event_param=events.event_param_changed),
                Container("PBP", (Column("A1", "S", ("UX",)), Column("A2", "I", (updated["player_state"]["battle"]["UX"],)))),
                Container("PPU", tuple(Column(wire, "I", tuple(unit[name] for unit in new_units)) for name, wire in UNIT_FIELDS[:17])),
                Container("QR", self.bootstrap.columns("quest_records", [record])),
                self._reward(after["Experience"] - before["Experience"], after["Money"] - before["Money"]),
                Container("TREASURE"), Container("TreasureInfo", (
                    Column("Index", "I", tuple(range(len(reward_counts)))),
                    Column("DropID", "I", tuple(reward_counts)), Column("Count", "I", tuple(reward_counts.values()))))]
            item_ids = tuple(dict.fromkeys((*item_grants, *outcome.item_ids)))
            if item_ids:
                children.append(self.possessions.response(updated, item_ids=item_ids, include_enabled=True))
            if discarded:
                children.append(self.possessions.overflow_response(discarded))
            if wallet_crystals:
                children.append(free_crystal_response(updated, profile.player_id))
            present_ids = list(dict.fromkeys((*rank_mail_ids, *delivered, *star_present_ids, *mode_present_ids)))
            if present_ids:
                children.append(presents.response(updated, present_ids))
            if bonus is not None:
                children.extend(bonus.children)
            children.extend(settle_children)
            response = self._prepare(envelope, children)
            _remember(journal, {"kind": "success", "sequence": active["sequence"],
                "quest_id": quest.quest_id, "start_key": active["start_key"], "finish_key": key,
                "completed_at": now.timestamp(), "client_time": request.client_time,
                "stars": stars, "kills_reported": request.kills, "difficulty": difficulty, "boost": boost,
                "score": None if score is None else score.score,
                "score_details": None if score is None else score.details,
                "obtained_mask": request.obtained_mask, "rewards": dict(reward_counts),
                "gold_events": gold_events, "gold_rates": {"stars": gold_stars, "reward_percent": percents.reward,
                                                           "event_percent": percents.event, "money_up": money_up},
                "charged": charged, "star_rewards": granted_stars, "discarded": discarded,
                "master_snapshot": active["master_snapshot"], "resource_version": active["resource_version"],
                "crystals": wallet_crystals, "crystal_presents": star_present_ids, "crystals_pending": deferred,
                "delivered_presents": delivered, "mode_presents": mode_present_ids, "mode_presents_pending": mode_pending,
                "mission_id": active.get("mission_id"), "candidate_stars": active.get("candidate_stars"),
                "drop_bonus": None if bonus is None else bonus.receipt(),
                "event_rewards": self.event_rewards.receipt(events, discarded),
                "response": response.decode("utf-8"), "response_revision": profile.revision + 1})
            journal["active"] = None
            _mark_processed(journal, key)
            for section, value in (("pending_presents", backlog.entries), (MODE_PROGRESS, progress)):
                if value:
                    journal[section] = value
                else:
                    journal.pop(section, None)
            updated[JOURNAL_KEY] = journal
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        _conflict("profile_changed", "Player state changed while settling the quest")

    def exit(self, envelope: RequestEnvelope, store: Store) -> bytes:
        request = parse_exit(envelope)
        key, now = self._key(envelope), self._now()
        for _ in range(3):
            profile = self.bootstrap._authenticate(envelope, store, require_method_key=False)
            journal = self._journal(profile.state)
            active = journal["active"]
            previous = next((entry for entry in reversed(journal["history"]) if entry.get("exit_key") == key), None)
            response = self._prepare(envelope, (self.bootstrap._system(profile),))
            if previous and (not active or 0 <= now.timestamp() - previous.get("completed_at", 0) <= self.rules.exit_replay_window_seconds):
                return response
            if not active or active.get("quest_id") != request.quest_id:
                _conflict("quest_attempt_missing", "Exit does not match an active attempt")
            _close(journal, "failure" if request.result_code == 0 else "retreat", now.timestamp(), exit_key=key)
            updated = deepcopy(profile.state)
            updated[JOURNAL_KEY] = journal
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        _conflict("profile_changed", "Player state changed while closing the quest")
