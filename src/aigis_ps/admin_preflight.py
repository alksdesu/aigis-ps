from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property, partial
from types import MappingProxyType
from typing import Any

from .admin_command import DOMAIN_NAME, AdminError, PreflightReport, error_payload
from .admin_commit import AdminGeneration
from .alxml import DEFAULT_LIMITS, AlXmlError, Column, Container, Limits, dumps
from .bootstrap import QUEST_LIMITS, STARTUP_COLLECTION_LIMITS, BootstrapService
from .crystal_wallet import wallet_totals
from .errors import ProtocolError
from .event_pass_service import STATUS_LIMITS
from .fairy_farm import LOAD as FAIRY_FARM_LOAD
from .player_state import PlayerStateService
from .presents import PRESENT_LIMITS
from .protocol import ProtocolRegistry, WireProfile, encode_response
from .silver_house import LOAD as SILVER_HOUSE_LOAD
from .storage import PlayerProfile
from .tower_service import FLOOR_INFO
from .tower_state import read_state as read_tower_state


PREFLIGHT_FAILED = "admin_preflight_failed"
# The serializer's messages name the exceeded limit, which the type name alone would hide.
RESPONSE_XML_INVALID = "response_xml_invalid"
QUEST_RECORD_ROUTE = "quest_record"
GAME_PARAM, GAME_PARAM2, CONFIG, CRYSTALS, QUEST_RECORDS = "game_param", "game_param2", "config", "crystals", "quest_records"
# Preflight receives no profile; a stand-in with the native key length keeps every SYSTEM block at its served size.
PREFLIGHT_PLAYER_ID = "1"
PREFLIGHT_KEY = "0" * 32
PREFLIGHT_REVISION = 0


class PreflightCandidate:
    def __init__(self, state: dict, generation: AdminGeneration):
        services = getattr(generation, "bundle", None)
        if services is None:
            raise ValueError("Preflight encodes the candidate with the leased generation's services")
        self.state, self.services, self._generation = state, services, generation
        self.profile = PlayerProfile(player_id=PREFLIGHT_PLAYER_ID, user_key=PREFLIGHT_KEY, method_key=PREFLIGHT_KEY, state=state,
                                     revision=PREFLIGHT_REVISION)

    @property
    def bootstrap(self) -> BootstrapService:
        return self.services.bootstrap

    @property
    def players(self) -> PlayerStateService:
        return self.services.player_state

    @cached_property
    def now(self) -> datetime:
        return self._generation.now()

    @cached_property
    def system(self) -> Container:
        return self.bootstrap.system_container(self.profile)


Encoder = Callable[[PreflightCandidate], bytes]


@dataclass(frozen=True)
class ClientRead:
    request: int | str
    encode: Encoder
    service: str | None = None

    def __post_init__(self) -> None:
        request = self.request
        if isinstance(request, bool) or not ((isinstance(request, int) and request > 0)
                                             or (isinstance(request, str) and request)):
            raise ValueError("Client reads name a registration number or a direct route")
        if not callable(self.encode):
            raise ValueError(f"Client read {request} needs an encoder")
        if self.service is not None and (not isinstance(self.service, str) or not self.service.isidentifier()):
            raise ValueError(f"Client read {request} names its service by its bundle attribute")

    def served(self, services: Any) -> bool:
        return self.service is None or getattr(services, self.service, None) is not None


@dataclass(frozen=True)
class PreflightDomain:
    reads: tuple[ClientRead, ...] = ()
    none_reason: str | None = None

    def __post_init__(self) -> None:
        reads = tuple(self.reads) if not isinstance(self.reads, (str, bytes)) else None
        if reads is None or any(not isinstance(read, ClientRead) for read in reads):
            raise ValueError("Preflight domains list the client reads they affect")
        if len({read.request for read in reads}) != len(reads):
            raise ValueError("A preflight domain lists each client read once")
        reason = self.none_reason
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError("Domains without client reads state why")
        if bool(reads) == (reason is not None):
            raise ValueError("A preflight domain has either client reads or a none reason")
        object.__setattr__(self, "reads", reads)

    @property
    def requests(self) -> tuple[int | str, ...]:
        return tuple(read.request for read in self.reads)


def _system_reply(candidate: PreflightCandidate, body: Container, limits: Limits = DEFAULT_LIMITS) -> bytes:
    return dumps(Container(children=(candidate.system, body)), limits=limits)


def _table_reply(table: str, rows: list[dict], limits: Limits = DEFAULT_LIMITS) -> bytes:
    return dumps(Container(children=BootstrapService.columns(table, rows)), limits=limits)


def _table(table: str, candidate: PreflightCandidate) -> bytes:
    return _table_reply(table, candidate.bootstrap.table_rows(candidate.state, table))


def _game_param(candidate: PreflightCandidate) -> bytes:
    return _table_reply(GAME_PARAM, [candidate.bootstrap.player_row(candidate.state)])


def _game_param2_fields(candidate: PreflightCandidate) -> bytes:
    return candidate.bootstrap.game_param2_fields_response(candidate.profile)


def _crystals(free: bool, candidate: PreflightCandidate) -> bytes:
    rows = candidate.bootstrap.table_rows(candidate.state, CRYSTALS)
    wallet_totals(candidate.state)
    return _table_reply(CRYSTALS, [row for row in rows if row["FreeFlag"] is free])


def _battle_param(candidate: PreflightCandidate) -> bytes:
    return PlayerStateService.battle_param_response(candidate.players.validate(candidate.state, references=False))


def _player_units(candidate: PreflightCandidate) -> bytes:
    players, state = candidate.players, candidate.state
    units = players.validate(state)["units"]
    reserved = players.quest_reservations(state)
    if reserved:
        # A battle in progress must still be able to settle its reserved unit rewards on top of this roster.
        trial = deepcopy(state)
        trial["quest_state"]["active"] = None
        players.credit_units(trial, reserved)
    return PlayerStateService.unit_response(units)


def _squads(candidate: PreflightCandidate) -> bytes:
    return PlayerStateService.squad_response(candidate.profile, candidate.players.validate(candidate.state))


def _presents(candidate: PreflightCandidate) -> bytes:
    return _system_reply(candidate, candidate.services.presents.response(candidate.state), PRESENT_LIMITS)


def _card_status(candidate: PreflightCandidate) -> bytes:
    return candidate.services.card_status.flat_response(candidate.state)


def _initialize_player(candidate: PreflightCandidate) -> bytes:
    return _system_reply(candidate, candidate.bootstrap.possessions_container(candidate.profile))


def _configuration_database(candidate: PreflightCandidate) -> bytes:
    collections = candidate.bootstrap.configuration_container(candidate.state)
    return dumps(Container(children=(candidate.system, *collections.children)), limits=STARTUP_COLLECTION_LIMITS)


def _quest_records(candidate: PreflightCandidate) -> bytes:
    # The quest_record read stores the zero rows it adds for unplayed master quests; preflight only answers with them.
    rows, _ = candidate.bootstrap.quest_record_rows(candidate.state)
    return _table_reply(QUEST_RECORDS, rows, QUEST_LIMITS)


def _missions(candidate: PreflightCandidate) -> bytes:
    return candidate.services.missions.preflight_snapshot(candidate.state, candidate.now, candidate.profile)


def _package_listing(operation: int, candidate: PreflightCandidate) -> bytes:
    return candidate.services.packages.read_response(operation, candidate.profile, candidate.state, candidate.now)


def _fairy_farm(candidate: PreflightCandidate) -> bytes:
    return _system_reply(candidate, candidate.services.fairy_farm.response(candidate.state))


def _silver_house(candidate: PreflightCandidate) -> bytes:
    return _system_reply(candidate, candidate.services.silver_house.response(candidate.state))


def _event_passes(candidate: PreflightCandidate) -> bytes:
    return _system_reply(candidate, candidate.services.event_passes.response(candidate.state), STATUS_LIMITS)


def _tower_portal(candidate: PreflightCandidate) -> bytes:
    # 66 with QUEST 0 settles the clock and reads every tower, so the portal reply covers the whole tower domain.
    service = candidate.services.towers
    state, _ = service.settle(deepcopy(candidate.state), candidate.now)
    records = read_tower_state(state, service.catalog)["towers"]
    towers = service.catalog.towers
    counts = tuple(records[str(tower.mission_id)]["stack"] if str(tower.mission_id) in records else 0
                   for tower in towers)
    items = tuple(dict.fromkeys((*(tower.consume_item_id for tower in towers),
                                 *(tower.crystal_item_id for tower in towers))))
    record = Container("PersonalRecord", (Column("Score", "I", (0,)), Column("TotalScore", "I", (0,))))
    stack = Container("StackCrystal", (Column("MissionID", "I", tuple(tower.mission_id for tower in towers)),
                                       Column("Count", "I", counts)))
    return dumps(Container(children=(candidate.system, record, stack,
                                     service.possessions.response(state, items, include_enabled=True))))


def _package_read(operation: int) -> ClientRead:
    return ClientRead(operation, partial(_package_listing, operation), "packages")


GAME_PARAM2_FIELDS_READ = ClientRead(33, _game_param2_fields)
GAME_PARAM_READ = ClientRead(34, _game_param)
GAME_PARAM2_READ = ClientRead(35, partial(_table, GAME_PARAM2))
CONFIG_READ = ClientRead(36, partial(_table, CONFIG))
BATTLE_PARAM_READ = ClientRead(39, _battle_param)
UNITS_READ = ClientRead(41, _player_units)
PRESENTS_READ = ClientRead(42, _presents)
PAID_CRYSTALS_READ = ClientRead(43, partial(_crystals, False))
FREE_CRYSTALS_READ = ClientRead(44, partial(_crystals, True))
PACKAGE_READS = (_package_read(46), _package_read(47))
CARD_STATUS_READ = ClientRead(51, _card_status, "card_status")
MISSIONS_READ = ClientRead(54, _missions, "missions")
SQUADS_READ = ClientRead(72, _squads)
FAIRY_FARM_READ = ClientRead(FAIRY_FARM_LOAD, _fairy_farm, "fairy_farm")
SILVER_HOUSE_READ = ClientRead(SILVER_HOUSE_LOAD, _silver_house, "silver_house")
TOWER_PORTAL_READ = ClientRead(FLOOR_INFO, _tower_portal, "towers")
POSSESSIONS_READ = ClientRead(129, _initialize_player)
STARTUP_READ = ClientRead(137, _configuration_database)
EVENT_PASS_READ = ClientRead(158, _event_passes, "event_passes")
QUEST_RECORDS_READ = ClientRead(QUEST_RECORD_ROUTE, _quest_records)
GAME_PARAM2_READS = (GAME_PARAM2_FIELDS_READ, GAME_PARAM2_READ)
LEDGER_PROJECTION_READS = (*GAME_PARAM2_READS, PRESENTS_READ)

PREFLIGHT_DOMAINS: Mapping[str, PreflightDomain] = MappingProxyType({
    "backgrounds": PreflightDomain((STARTUP_READ,)),
    "card_status": PreflightDomain((CARD_STATUS_READ,)),
    "comeback_login": PreflightDomain(LEDGER_PROJECTION_READS),
    "config": PreflightDomain((CONFIG_READ,)),
    "crystals": PreflightDomain((PAID_CRYSTALS_READ, FREE_CRYSTALS_READ)),
    "daily_gift": PreflightDomain((PRESENTS_READ,)),
    "event_login": PreflightDomain((PRESENTS_READ,)),
    "event_pass": PreflightDomain((STARTUP_READ, EVENT_PASS_READ)),
    "evolution_rewards": PreflightDomain(none_reason="The client never reads the awakening reward ledger; "
                                                     "the restart check validates it"),
    "fairy": PreflightDomain((FAIRY_FARM_READ,)),
    "fame": PreflightDomain((STARTUP_READ,)),
    "filters": PreflightDomain((STARTUP_READ,)),
    "game_param": PreflightDomain((GAME_PARAM2_FIELDS_READ, GAME_PARAM_READ, GAME_PARAM2_READ)),
    "gifts": PreflightDomain(none_reason="The client never reads the trust reward ledger; "
                                         "the gift service validates it before its next use"),
    "letstd": PreflightDomain(none_reason="The client never reads this ledger; its rewards reach the client only as presents (42)"),
    "login_cooking": PreflightDomain(LEDGER_PROJECTION_READS),
    "login_rewards": PreflightDomain(LEDGER_PROJECTION_READS),
    "missions": PreflightDomain((MISSIONS_READ,)),
    "packages": PreflightDomain(PACKAGE_READS),
    "perused": PreflightDomain((STARTUP_READ,)),
    "possessions": PreflightDomain((POSSESSIONS_READ, STARTUP_READ)),
    "presents": PreflightDomain((PRESENTS_READ,)),
    "purchased_login": PreflightDomain((PRESENTS_READ, *PACKAGE_READS)),
    "quest_records": PreflightDomain((QUEST_RECORDS_READ,)),
    # The client never reads the battle journal itself; its edits surface through the quest records read.
    "quest_state": PreflightDomain((QUEST_RECORDS_READ,)),
    "rank_rewards": PreflightDomain((PRESENTS_READ,)),
    "silver": PreflightDomain((SILVER_HOUSE_READ,)),
    "spending": PreflightDomain(GAME_PARAM2_READS),
    "squads": PreflightDomain((BATTLE_PARAM_READ, SQUADS_READ)),
    "sugoroku": PreflightDomain(none_reason="62 re-synchronizes the board with the current month on every read; "
                                            "the restart check validates the stored board ledger"),
    "towers": PreflightDomain((TOWER_PORTAL_READ,)),
    "unit_overrides": PreflightDomain(none_reason="The client never reads temporary unit overrides; "
                                                  "only the training, silver exchange and shop growth guards check them"),
    "units": PreflightDomain((UNITS_READ,)),
    "universal_flags": PreflightDomain((STARTUP_READ,)),
    "vote": PreflightDomain((STARTUP_READ,)),
})


def _wire_profile(registry: ProtocolRegistry, request: int | str) -> WireProfile:
    if isinstance(request, int):
        profile = registry.by_registration.get(request)
    else:
        profile = next((item for item in registry.profiles if item.name == request and not item.registration_keys), None)
    if profile is None:
        raise ValueError(f"Client read {request} has no native response contract")
    return profile


class GenerationPreflight:
    is_noop = False

    def __init__(self, domains: Mapping[str, PreflightDomain] = PREFLIGHT_DOMAINS, *,
                 registry: ProtocolRegistry | None = None):
        if not isinstance(domains, Mapping) or any(
                not isinstance(name, str) or not DOMAIN_NAME.fullmatch(name) or not isinstance(entry, PreflightDomain)
                for name, entry in domains.items()):
            raise ValueError("Preflight domain tables map domain names to preflight domains")
        reads: dict[int | str, ClientRead] = {}
        for entry in domains.values():
            for read in entry.reads:
                # Each request is encoded once per check, so every domain must reach it through the same encoder.
                if reads.setdefault(read.request, read) != read:
                    raise ValueError(f"Client read {read.request} has conflicting encoders")
        registry = ProtocolRegistry.load() if registry is None else registry
        self._domains = MappingProxyType(dict(sorted(domains.items())))
        self._profiles = {request: _wire_profile(registry, request) for request in reads}

    @property
    def domains(self) -> Mapping[str, PreflightDomain]:
        return self._domains

    def check(self, before: dict, candidate: dict, domains: Iterable[str], generation: AdminGeneration) -> PreflightReport:
        target = PreflightCandidate(candidate, generation)
        readback: dict[str, tuple[int | str, ...]] = {}
        reached: dict[int | str, tuple[ClientRead, list[str]]] = {}
        for name in sorted(self._domains.keys() & set(domains)):
            served = [read for read in self._domains[name].reads if read.served(target.services)]
            if served:
                readback[name] = tuple(read.request for read in served)
            for read in served:
                reached.setdefault(read.request, (read, []))[1].append(name)
        for read, names in reached.values():
            self._encode(read, names, target)
        return PreflightReport(readback)

    def _encode(self, read: ClientRead, domains: list[str], target: PreflightCandidate) -> None:
        try:
            encode_response(self._profiles[read.request], read.encode(target))
        except Exception as error:
            cause = ProtocolError(RESPONSE_XML_INVALID, str(error)) if isinstance(error, AlXmlError) else error
            raise AdminError(PREFLIGHT_FAILED, f"Response {read.request} could not be encoded for the candidate state",
                             details={"response": read.request, "domains": domains, "error": error_payload(cause)}) from error
