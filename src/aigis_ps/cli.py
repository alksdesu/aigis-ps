from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
import hashlib
import json
import logging
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from . import __version__
from .asset_service import AdditionalAssetService, AssetService
from .assets import export_asset, inventory_assets, write_inventory
from .client_versions import SUPPORTED_CLIENT_SHA256
from .config_registry import ConfigError
from .config_sets import ResolvedConfig, resolve
from .generation import (MAX_PORT, Generation, GenerationIdentity, GenerationInputs, GenerationRuntime, Progress,
                         code_digest)
from .masterdata import MasterDataStore
from .protocol import ProtocolRegistry
from .local_io import checked_path, read_bounded, read_json_object
from .resource_library import ResourceLibrary
from .native_cdn import NativeCdnService, load_bootstrap
from .energy_clock import instant
from .errors import ProtocolError
from .resource_view import load_resource_view
from .session_stub_service import OPERATIONS as SESSION_STUB_OPERATIONS
from .startup_diagnostics import StartupDiagnostics
from .summon_schedule import OPERATIONS as SUMMON_SCHEDULE_OPERATIONS

if TYPE_CHECKING:
    from fastapi import FastAPI
    from .admin_app import LeasedGeneration
    from .admin_command import Preflight, RestartCheck
    from .storage import Store
    from .bootstrap import BootstrapService
    from .player_state import PlayerStateService
    from .possessions import PossessionService
    from .quest_service import QuestService
    from .unit_actions import UnitActionService
    from .filter_state import FilterService
    from .session_stub_service import SessionStubService
    from .summon_schedule import SummonScheduleService
    from .card_status import CardStatusService
    from .presents import PresentService
    from .gift_service import GiftService
    from .peruse_state import PeruseService
    from .peruse_episode import PeruseEpisodeService
    from .quest_record_service import QuestRecordService
    from .training_service import TrainingService
    from .fairy_farm import FairyFarmService
    from .unit_sale import UnitSaleService
    from .silver_house import SilverHouseService
    from .unit_capacity import UnitCapacity
    from .evolution_service import EvolutionService
    from .evolution_rewards import EvolutionRewards
    from .unit_detail import UnitDetailService
    from .orb_exchange import OrbExchangeService
    from .guide_service import GuideService
    from .barracks_service import BarracksService
    from .recovery_service import RecoveryService
    from .trade_service import TradeService
    from .tower_trade_service import TowerTradeService
    from .tower_service import TowerService
    from .entrust_service import EntrustService
    from .mission_window_revival import MissionWindowRevival
    from .mission_service import MissionService
    from .blessing_service import BlessingService
    from .skin_shop_service import SkinShopService
    from .universal_flags_service import UniversalFlagsService
    from .fame_service import FameService
    from .fame_state import FameRepository
    from .background_service import BackgroundService
    from .summon_pool_catalog import SummonPoolCatalog
    from .summon_tickets import SummonTicketService
    from .login_reward_service import LoginRewardService
    from .current_squad import CurrentSquadService
    from .sugoroku_service import SugorokuService
    from .item_summon_tickets import ItemSummonTicketService
    from .select_tickets import SelectTicketService
    from .rank_rewards import RankRewardService
    from .view_events import ViewEventService
    from .event_pass_service import EventPassService
    from .package_service import PackageService
    from .event_login_service import EventLoginService
    from .comeback_login_service import ComebackLoginService
    from .purchased_login_service import PurchasedLoginService
    from .daily_gift_service import DailyGiftService
    from .vote_service import VoteService
    from .vote_cooking_service import VoteCookingService
    from .vote_cooking_notifications import VoteCookingNotifications
    from .vote_expiry import VoteAssaultNotifications, VoteExpiryNotifications
    from .serial_code_service import SerialCodeService
    from .letstd_service import LetsTDService


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ASSETS = PROJECT_ROOT.parent / "aigisr_exe"
DEFAULT_DATA = PROJECT_ROOT / "runtime" / "server"
DEFAULT_RESOURCES = PROJECT_ROOT / "runtime" / "resources"
LOOPBACK_HOSTS = ("127.0.0.1", "::1")
DEFAULT_HOST = LOOPBACK_HOSTS[0]
DEFAULT_PORT = 18103
# Native loaders resend these until they succeed, so their refusals stay on the startup record whoever answers them.
NATIVE_STARTUP_LOADS = (35, 36, 38, 39, 41, 43, 44, 51, 54, 137)
# The native login, home, menu and story flows send these unprompted, and a refusal loops the client on an error popup.
AUTO_SENT_OPERATIONS = tuple(sorted({45, 46, 54, 55, 62, 66, 67, 84, 115, 122, 126, 130, 157,
                                     *SUMMON_SCHEDULE_OPERATIONS, *NATIVE_STARTUP_LOADS, *SESSION_STUB_OPERATIONS}))
AUTO_REQUEST_AFTERMATH = "stops on a communication error popup and resends the same request after every confirmation"
# A later service replaces an earlier one's handler for a shared operation, so this order is part of the wiring.
HANDLER_SERVICES = ("bootstrap", "session_stubs", "summon_schedule", "player_state", "unit_actions", "filters",
                    "presents", "peruse", "peruse_episodes", "quests", "quest_records", "entrust", "gifts", "training",
                    "fairy_farm", "sales", "silver_house", "evolution", "unit_detail", "orb_exchange", "guide",
                    "barracks", "recovery", "trades", "tower_trades", "towers", "missions", "blessings", "skin_shop",
                    "universal_flags", "fame", "backgrounds", "login_rewards", "rank_rewards", "current_squad",
                    "sugoroku", "view_events", "event_passes", "votes", "packages", "serial_codes")
CONTENT_VERSION = "content_version"
ADMIN_THREAD = "aigis-admin-console"
ADMIN_STARTUP_SECONDS = 10.0
ADMIN_SHUTDOWN_SECONDS = 10.0
ADMIN_POLL_SECONDS = 0.01
RECORDED_CONSOLE_FIELDS = ("admin_host", "admin_port", "pid", "created_at")
MAX_RULE_BYTES = 16 * 1024
ENERGY_PLAN_BYTES = 128 * 1024
# A compact offline plan is its bounded diff plus at most two paths of 32 KiB, a few digests and small ledgers.
OFFLINE_PLAN_MARGIN_BYTES = 128 * 1024
log = logging.getLogger("aigis_ps")


@dataclass
class ServerServices:
    masters: MasterDataStore
    bootstrap: BootstrapService
    player_state: PlayerStateService
    possessions: PossessionService
    resource_entries: tuple
    native_cdn: NativeCdnService | None
    resource_error: str | None
    quests: QuestService | None
    quest_error: str | None
    unit_actions: UnitActionService
    filters: FilterService
    session_stubs: SessionStubService
    summon_schedule: SummonScheduleService
    card_status: CardStatusService | None
    card_status_error: str | None
    presents: PresentService
    gifts: GiftService | None
    gift_error: str | None
    peruse: PeruseService
    peruse_episodes: PeruseEpisodeService
    training: TrainingService | None
    training_error: str | None
    fairy_farm: FairyFarmService | None
    fairy_farm_error: str | None
    sales: UnitSaleService | None
    sale_error: str | None
    silver_house: SilverHouseService | None
    silver_house_error: str | None
    unit_capacity: UnitCapacity | None
    unit_capacity_error: str | None
    evolution: EvolutionService | None
    evolution_error: str | None
    evolution_rewards: EvolutionRewards | None
    evolution_rewards_error: str | None
    unit_detail: UnitDetailService | None
    unit_detail_error: str | None
    unit_skin_error: str | None
    orb_exchange: OrbExchangeService | None
    orb_exchange_error: str | None
    guide: GuideService | None
    guide_error: str | None
    barracks: BarracksService | None
    barracks_error: str | None
    recovery: RecoveryService | None
    recovery_error: str | None
    trades: TradeService | None
    trade_error: str | None
    missions: MissionService | None = None
    mission_error: str | None = None
    blessings: BlessingService | None = None
    blessing_error: str | None = None
    skin_shop: SkinShopService | None = None
    skin_shop_error: str | None = None
    universal_flags: UniversalFlagsService | None = None
    universal_flags_error: str | None = None
    fame: FameService | None = None
    fame_repository: FameRepository | None = None
    fame_error: str | None = None
    backgrounds: BackgroundService | None = None
    background_error: str | None = None
    summon_pools: SummonPoolCatalog | None = None
    summon_tickets: SummonTicketService | None = None
    summon_ticket_error: str | None = None
    login_rewards: LoginRewardService | None = None
    login_reward_error: str | None = None
    current_squad: CurrentSquadService | None = None
    current_squad_error: str | None = None
    sugoroku: SugorokuService | None = None
    sugoroku_error: str | None = None
    item_summon_tickets: ItemSummonTicketService | None = None
    item_summon_error: str | None = None
    selection_tickets: SelectTicketService | None = None
    selection_ticket_error: str | None = None
    rank_rewards: RankRewardService | None = None
    rank_reward_error: str | None = None
    view_events: ViewEventService | None = None
    view_event_error: str | None = None
    event_passes: EventPassService | None = None
    event_pass_error: str | None = None
    event_pass_revival: dict | None = None
    packages: PackageService | None = None
    package_error: str | None = None
    event_logins: EventLoginService | None = None
    event_login_error: str | None = None
    comeback_logins: ComebackLoginService | None = None
    comeback_login_error: str | None = None
    purchased_logins: PurchasedLoginService | None = None
    purchased_login_error: str | None = None
    daily_gifts: DailyGiftService | None = None
    daily_gift_error: str | None = None
    votes: VoteService | None = None
    vote_error: str | None = None
    vote_cooking: VoteCookingService | None = None
    vote_cooking_error: str | None = None
    vote_cooking_notifier: VoteCookingNotifications | None = None
    vote_cooking_notifier_error: str | None = None
    vote_expiry_notifier: VoteExpiryNotifications | None = None
    vote_expiry_notifier_error: str | None = None
    vote_assault_notifier: VoteAssaultNotifications | None = None
    vote_assault_notifier_error: str | None = None
    tower_trades: TowerTradeService | None = None
    tower_trade_error: str | None = None
    entrust: EntrustService | None = None
    entrust_error: str | None = None
    towers: TowerService | None = None
    tower_error: str | None = None
    subjugation_error: str | None = None
    quest_records: QuestRecordService | None = None
    quest_record_error: str | None = None
    vote_quest_ticket_error: str | None = None
    mission_windows: dict | None = None
    command_texts: dict | None = None
    shop_window_closure: dict | None = None
    mission_window_revival: MissionWindowRevival | None = None
    mission_window_error: str | None = None
    serial_codes: SerialCodeService | None = None
    serial_code_error: str | None = None
    letstd: LetsTDService | None = None
    letstd_error: str | None = None
    master_snapshot: str | None = None

    def initialize(self, state: dict) -> dict:
        from .energy_clock import validate_clock
        from .universal_flags import read_flags
        from .background_state import read_backgrounds
        from .tower_state import DOMAIN as TOWER_STATE
        from .vote_service import TRANSACTION_DOMAINS as VOTE_TRANSACTION_DOMAINS
        from .vote_expiry import ASSAULT_NOTIFICATION_KEY as ASSAULT_NOTICES, NOTIFICATION_KEY as EXPIRY_NOTICES
        from .event_pass_service import (AUTOMATION_KEY as EVENT_PASS_AUTOMATION, DELIVERY_KEY as EVENT_PASS_DELIVERY,
                                         HISTORY_NAMESPACE as EVENT_PASS_HISTORY)
        from .event_pass_state import LEDGER_KEY as EVENT_PASS_LEDGER
        from .package_service import HISTORY_NAMESPACE as PACKAGE_HISTORY
        from .view_events import HISTORY_NAMESPACE as VIEW_EVENT_HISTORY
        from .tower_trade_service import RECEIPT_NAMESPACE as TOWER_TRADE_RECEIPTS
        from .tower_trade_state import HISTORY_KEY as TOWER_TRADE_HISTORY
        from .entrust_service import HISTORY_NAMESPACE as ENTRUST_HISTORY
        from .mission_service import HISTORY_NAMESPACE as MISSION_HISTORY, PASS_DELIVERY_KEY as MISSION_PASS_DELIVERY
        from .mission_state import JOURNAL_KEY as MISSION_JOURNAL, SKIN_HISTORY_KEY as SKIN_HISTORY
        from .mission_ep_bindings import LEDGER_KEY as MISSION_EP_BINDINGS
        from .sugoroku_service import HISTORY_NAMESPACE as SUGOROKU_HISTORY
        from .item_summon_tickets import HISTORY_NAMESPACE as ITEM_SUMMON_HISTORY
        from .select_tickets import HISTORY_NAMESPACE as SELECT_TICKET_HISTORY
        from .serial_code_service import NAMESPACES as SERIAL_CODE_NAMESPACES
        from .event_login_state import LEDGER_KEY as EVENT_LOGIN_LEDGER
        from .comeback_login_state import LEDGER_KEY as COMEBACK_LOGIN_LEDGER
        from .purchased_login_state import LEDGER_KEY as PURCHASED_LOGIN_LEDGER
        from .daily_gift_state import LEDGER_KEY as DAILY_GIFT_LEDGER
        from .letstd_state import LEDGER_KEY as LETSTD_LEDGER
        from .login_cooking_state import LEDGER_KEY as COOKING_LEDGER
        from .login_reward_state import LEDGER_KEY as LOGIN_REWARD_LEDGER
        from .login_reward_service import (DELIVERY_KEY as LOGIN_REWARD_DELIVERY, HISTORY_NAMESPACE as LOGIN_REWARD_HISTORY,
                                           LETSTD_SOURCE as LOGIN_LETSTD_GRANTS)
        from .rank_reward_state import LEDGER_KEY as RANK_REWARD_LEDGER
        from .rank_rewards import (DELIVERY_KEY as RANK_REWARD_DELIVERY, HISTORY_NAMESPACE as RANK_REWARD_HISTORY,
                                   LETSTD_SECTION as RANK_LETSTD_GRANTS)
        validate_clock(state)
        read_flags(state)
        read_backgrounds(state)
        updated = self.player_state.initialize(state)
        if self.votes is not None:
            updated = self.votes.initialize(updated)
        elif (any(key in updated for key in ("vote_state", *VOTE_TRANSACTION_DOMAINS, "vote_cooking_notifications"))
                or updated.get("tables", {}).get("vote_history")):
            raise ProtocolError("vote_service_unavailable", "Existing vote history requires a verified service", 503)
        if self.vote_cooking is not None:
            updated = self.vote_cooking.initialize(updated)
        elif "vote_cooking" in updated:
            raise ProtocolError("vote_cooking_service_unavailable", "Existing cooking progress requires its service", 503)
        if self.vote_cooking_notifier is not None:
            updated = self.vote_cooking_notifier.initialize(updated)
        elif "vote_cooking_notifications" in updated:
            raise ProtocolError("vote_cooking_notifications_unavailable", "Existing cooking notices require their service", 503)
        if self.vote_expiry_notifier is not None:
            updated = self.vote_expiry_notifier.initialize(updated)
        elif EXPIRY_NOTICES in updated:
            raise ProtocolError("vote_expiry_notifications_unavailable", "Existing expiry notices require their service", 503)
        if self.vote_assault_notifier is not None:
            updated = self.vote_assault_notifier.initialize(updated)
        elif ASSAULT_NOTICES in updated:
            raise ProtocolError("vote_assault_notifications_unavailable", "Existing assault notices require their service", 503)
        if self.event_passes is not None:
            updated = self.event_passes.initialize(updated)
        elif (any(key in updated for key in (EVENT_PASS_LEDGER, EVENT_PASS_HISTORY, EVENT_PASS_DELIVERY, EVENT_PASS_AUTOMATION))
                or updated.get("tables", {}).get("event_pass_status")):
            raise ProtocolError("event_pass_service_unavailable", "Existing event pass history requires a verified service", 503)
        if self.event_logins is not None:
            updated = self.event_logins.initialize(updated)
        elif EVENT_LOGIN_LEDGER in updated:
            raise ProtocolError("event_login_service_unavailable", "Existing event login history requires a verified service", 503)
        if self.comeback_logins is not None:
            updated = self.comeback_logins.initialize(updated)
        elif COMEBACK_LOGIN_LEDGER in updated:
            raise ProtocolError("comeback_login_service_unavailable", "Existing comeback history requires a verified service", 503)
        if self.purchased_logins is not None:
            updated = self.purchased_logins.initialize(updated)
        elif PURCHASED_LOGIN_LEDGER in updated:
            raise ProtocolError("purchased_login_service_unavailable", "Existing purchased login history requires a verified service", 503)
        if self.daily_gifts is not None:
            updated = self.daily_gifts.initialize(updated)
        elif DAILY_GIFT_LEDGER in updated:
            raise ProtocolError("daily_gift_service_unavailable", "Existing daily gift history requires a verified service", 503)
        if self.packages is not None:
            updated = self.packages.initialize(updated)
        elif any(key in updated for key in ("package_purchases", PACKAGE_HISTORY, "package_delivery")):
            raise ProtocolError("package_service_unavailable", "Existing package history requires a verified service", 503)
        if self.view_events is not None:
            updated = self.view_events.initialize(updated)
        elif VIEW_EVENT_HISTORY in updated:
            raise ProtocolError("view_event_service_unavailable", "Existing view event history requires a verified service", 503)
        if self.evolution_rewards is not None:
            updated = self.evolution_rewards.initialize(updated)
        elif self.gifts is not None:
            raise ProtocolError("evolution_rewards_unavailable", "Old awakening reward history cannot be preserved", 503)
        if self.trades is not None:
            updated = self.trades.initialize(updated)
        elif updated.get("trade_history") or updated.get("tables", {}).get("exchange_history"):
            raise ProtocolError("trade_history_unavailable", "Existing purchase history requires a verified trade service", 503)
        if self.tower_trades is not None:
            updated = self.tower_trades.initialize(updated)
        elif any(key in updated for key in (TOWER_TRADE_HISTORY, TOWER_TRADE_RECEIPTS)):
            raise ProtocolError("tower_trade_unavailable", "Existing tower purchases require their verified service", 503)
        if self.towers is not None:
            updated = self.towers.initialize(updated)
        elif TOWER_STATE in updated:
            raise ProtocolError("tower_service_unavailable", "Existing tower progress requires its verified service", 503)
        if self.entrust is not None:
            updated = self.entrust.initialize(updated)
        elif ENTRUST_HISTORY in updated:
            raise ProtocolError("entrust_unavailable", "Existing entrust receipts require their verified service", 503)
        if self.fame_repository is not None:
            self.fame_repository.read(updated)
        elif updated.get("tables", {}).get("fame_depository") or "fame_depository_schema" in updated:
            raise ProtocolError("fame_repository_unavailable", "Existing fame stock requires verified repository definitions", 503)
        if self.missions is not None:
            updated = self.missions.initialize(updated)
        elif any(key in updated for key in (MISSION_JOURNAL, MISSION_HISTORY, SKIN_HISTORY,
                                           MISSION_EP_BINDINGS, MISSION_PASS_DELIVERY)):
            raise ProtocolError("mission_service_unavailable", "Existing mission history requires verified mission resources", 503)
        if self.letstd is not None:
            updated = self.letstd.initialize(updated)
        elif LETSTD_LEDGER in updated or any(
                isinstance(updated.get(marker), dict) and updated[marker].get(grants)
                for marker, grants in ((LOGIN_REWARD_DELIVERY, LOGIN_LETSTD_GRANTS),
                                       (RANK_REWARD_DELIVERY, RANK_LETSTD_GRANTS))):
            raise ProtocolError("letstd_unavailable", "Existing LetsTD rewards require their verified service", 503)
        if self.login_rewards is not None:
            updated = self.login_rewards.initialize(updated)
        elif any(key in updated for key in (LOGIN_REWARD_LEDGER, LOGIN_REWARD_HISTORY, LOGIN_REWARD_DELIVERY, COOKING_LEDGER)):
            raise ProtocolError("login_reward_service_unavailable", "Existing login rewards require verified reward resources", 503)
        if self.rank_rewards is not None:
            updated = self.rank_rewards.initialize(updated)
        elif any(key in updated for key in (RANK_REWARD_LEDGER, RANK_REWARD_HISTORY, RANK_REWARD_DELIVERY)):
            raise ProtocolError("rank_reward_service_unavailable", "Existing rank rewards require verified reward resources", 503)
        if self.sugoroku is not None:
            updated = self.sugoroku.initialize(updated)
        elif any(key in updated for key in ("sugoroku", SUGOROKU_HISTORY, "monthly_capacity")):
            raise ProtocolError("sugoroku_service_unavailable", "Existing reward board history requires verified resources", 503)
        if ITEM_SUMMON_HISTORY in updated and self.item_summon_tickets is None:
            raise ProtocolError("item_summon_service_unavailable", "Existing item ticket history requires verified resources", 503)
        if SELECT_TICKET_HISTORY in updated and self.selection_tickets is None:
            raise ProtocolError("select_ticket_service_unavailable", "Existing selection ticket history requires verified resources", 503)
        if self.serial_codes is not None:
            updated = self.serial_codes.initialize(updated)
        elif any(key in updated for key in SERIAL_CODE_NAMESPACES):
            raise ProtocolError("serial_code_service_unavailable", "Existing serial code redemptions require their service", 503)
        return updated


def output(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def client_fingerprint(asset_dir: Path) -> str | None:
    executable = asset_dir / "Aigis_r.exe"
    return hashlib.sha256(read_bounded(executable, 64 * 1024 * 1024)).hexdigest() if executable.is_file() else None


def load_rules(path: Path | None):
    from .bootstrap import LocalRules
    if path is None:
        return LocalRules()
    values = read_json_object(path, limit=MAX_RULE_BYTES)
    allowed = {"money", "experience", "rank", "charisma", "action_points", "user_name"}
    if not isinstance(values, dict) or set(values) - allowed:
        raise ValueError("Unknown or invalid local starter rules")
    return LocalRules(**values)


def load_quest_rules(path: Path | None):
    from .quest_service import QuestRules
    if path is None:
        return QuestRules()
    values = read_json_object(path, limit=MAX_RULE_BYTES)
    if set(values) - QuestRules.__dataclass_fields__.keys():
        raise ValueError("Unknown local quest rules")
    return QuestRules(**values)


def load_training_rules(path: Path | None):
    from .training_catalog import TrainingRules
    if path is None:
        return TrainingRules()
    values = read_json_object(path, limit=MAX_RULE_BYTES)
    if set(values) - TrainingRules.__dataclass_fields__.keys():
        raise ValueError("Unknown local training rules")
    return TrainingRules(**values)


def load_silver_house_rules(path: Path | None):
    from .silver_house import SilverHouseRules
    if path is None:
        return SilverHouseRules()
    values = read_json_object(path, limit=MAX_RULE_BYTES)
    if set(values) - SilverHouseRules.__dataclass_fields__.keys():
        raise ValueError("Unknown local silver warehouse rules")
    return SilverHouseRules(**values)


def load_evolution_rules(path: Path | None):
    from .evolution_catalog import EvolutionRules
    if path is None:
        return EvolutionRules()
    values = read_json_object(path, limit=MAX_RULE_BYTES)
    if set(values) - EvolutionRules.__dataclass_fields__.keys():
        raise ValueError("Unknown local evolution rules")
    return EvolutionRules(**values)


def load_recovery_rules(path: Path | None):
    from .recovery_service import RecoveryRules
    if path is None:
        return RecoveryRules()
    values = read_json_object(path, limit=MAX_RULE_BYTES)
    if set(values) - RecoveryRules.__dataclass_fields__.keys():
        raise ValueError("Unknown local recovery rules")
    return RecoveryRules(**values)


def load_trade_rules(path: Path | None):
    from .trade_history import TradeRules
    if path is None:
        return TradeRules()
    values = read_json_object(path, limit=MAX_RULE_BYTES)
    if set(values) - TradeRules.__dataclass_fields__.keys():
        raise ValueError("Unknown local trade rules")
    return TradeRules(**values)


def load_ticket_rules(path: Path | None):
    from .summon_tickets import SummonTicketRules
    if path is None:
        return SummonTicketRules()
    values = read_json_object(path, limit=MAX_RULE_BYTES)
    if set(values) - SummonTicketRules.__dataclass_fields__.keys():
        raise ValueError("Unknown local summon ticket rules")
    return SummonTicketRules(**values)


def load_item_ticket_rules(path: Path | None):
    from .item_summon_tickets import DEFAULT_ITEM_TICKET_POOLS
    from .summon_tickets import SummonTicketRules
    return SummonTicketRules(DEFAULT_ITEM_TICKET_POOLS) if path is None else load_ticket_rules(path)


def load_login_rules(path: Path | None):
    from .login_reward_service import LoginRewardRules
    if path is None:
        return LoginRewardRules()
    values = read_json_object(path, limit=MAX_RULE_BYTES)
    if set(values) - LoginRewardRules.__dataclass_fields__.keys():
        raise ValueError("Unknown local login reward rules")
    return LoginRewardRules(**values)


def load_event_pass_rules(path: Path | None):
    from .event_pass_rules import EventPassAutomationRules
    if path is None:
        return EventPassAutomationRules()
    values = read_json_object(path, limit=MAX_RULE_BYTES)
    if set(values) - EventPassAutomationRules.__dataclass_fields__.keys():
        raise ValueError("Unknown local event pass rules")
    return EventPassAutomationRules(**values)


def load_entrust_rules(path: Path | None):
    from .entrust_service import EntrustRules
    if path is None:
        return EntrustRules()
    values = read_json_object(path, limit=MAX_RULE_BYTES)
    if set(values) - EntrustRules.__dataclass_fields__.keys():
        raise ValueError("Unknown local entrust rules")
    return EntrustRules(**values)


def services(args):
    from .bootstrap import BootstrapService
    from .player_state import PlayerStateService
    from .possessions import PossessionService
    from .quest_catalog import QuestCatalog
    from .quest_service import QuestService
    from .unit_actions import UnitActionService
    from .filter_state import FilterService, filter_response
    from .session_stub_service import SessionStubService
    from .summon_schedule import SummonScheduleService
    from .card_status import CardStatusService
    from .presents import PresentService
    from .gift_catalog import GiftCatalog
    from .gift_service import GiftService
    from .image_aliases import ImageAliasCatalog
    from .peruse_state import PeruseService, peruse_response
    from .peruse_episode import PeruseEpisodeService
    from .quest_record_service import QuestRecordService
    from .training_catalog import TrainingCatalog
    from .training_service import TrainingService
    from .fairy_farm import FairyFarmService
    from .unit_sale import UnitSaleService
    from .unit_sale_catalog import UnitSaleCatalog
    from .silver_house import SilverHouseService
    from .unit_capacity import UnitCapacity
    from .evolution_catalog import EvolutionCatalog
    from .evolution_materials import EvolutionMaterials
    from .evolution_service import EvolutionService
    from .evolution_rewards import EvolutionRewards
    from .unit_detail import UnitDetailService
    from .unit_detail_options import smartphone_images_enabled
    from .unit_skin_catalog import UnitSkinCatalog
    from .orb_exchange import OrbExchangeService
    from .guide_service import GuideService
    from .barracks_service import BarracksService
    from .recovery_service import RecoveryService
    from .trade_catalog import TradeCatalog
    from .trade_grants import TradeUnitGrants
    from .trade_service import TradeService
    from .tower_trade_catalog import TowerTradeCatalog
    from .tower_trade_grants import TowerTradeGrants
    from .tower_trade_service import TowerTradeService
    from .entrust_catalog import EntrustCatalog
    from .entrust_service import EntrustService
    from .silver_trade import SilverTradeCatalog
    from .shop_growth import ShopGrowthCatalog
    from .mission_catalog import MissionCatalog
    from .mission_service import MissionService
    from .mission_state import MissionProgress
    from .quest_clear_spec import QuestClearSpecs
    from .mission_ep_bindings import MissionEpBindings
    from .mission_pass_points import MissionPassPoints
    from .blessing_catalog import BlessingCatalog
    from .blessing_service import BlessingService
    from .universal_flags import flags_response
    from .skin_shop_catalog import SkinShopCatalog
    from .skin_shop_service import SkinShopService
    from .universal_flags_catalog import UniversalFlagsCatalog
    from .universal_flags_service import UniversalFlagsService
    from .fame_catalog import FameCatalog
    from .fame_service import FameService
    from .fame_state import FameRepository
    from .background_catalog import BackgroundCatalog
    from .background_service import BackgroundService
    from .background_state import background_response
    from .summon_pool_catalog import SummonPoolCatalog
    from .summon_tickets import SummonTicketService
    from .login_reward_catalog import LoginRewardCatalog
    from .login_reward_service import LoginRewardService
    from .current_squad import CurrentSquadService
    from .sugoroku_catalog import SugorokuCatalog
    from .sugoroku_service import SugorokuService
    from .item_summon_catalog import ItemSummonCatalog
    from .item_summon_tickets import ItemSummonTicketService
    from .select_ticket_catalog import SelectTicketCatalog
    from .select_tickets import SelectTicketService
    from .rank_bonus_catalog import RankBonusCatalog
    from .rank_rewards import RankRewardService
    from .view_events import ViewEventService
    from .event_pass_catalog import EventPassCatalog
    from .event_pass_service import EventPassService
    from .event_pass_revival import load_revival_rules
    from .package_catalog import PackageCatalog
    from .package_service import PackageService
    from .event_login_catalog import EventLoginCatalog
    from .event_login_rules import load_event_login_rules
    from .event_login_service import EventLoginService
    from .comeback_login_catalog import ComebackLoginCatalog
    from .comeback_login_rules import load_comeback_login_rules
    from .comeback_login_service import ComebackLoginService
    from .purchased_login_catalog import PurchasedLoginCatalog
    from .purchased_login_rules import load_purchased_login_rules
    from .purchased_login_service import PurchasedLoginService
    from .daily_gift_catalog import DailyGiftCatalog
    from .daily_gift_rules import load_daily_gift_rules
    from .daily_gift_service import DailyGiftService
    from .vote_catalog import VoteCatalog
    from .vote_rules import load_vote_rules
    from .vote_service import VoteService
    from .vote_cooking_catalog import VoteCookingCatalog
    from .vote_cooking_rules import load_vote_cooking_rules
    from .vote_cooking_service import VoteCookingService
    from .vote_cooking_notifications import VoteCookingNotifications
    from .vote_expiry import VoteAssaultNotifications, VoteExpiryNotifications
    from .vote_quest_tickets import DEPENDENCIES_UNAVAILABLE as VOTE_QUEST_TICKETS_UNAVAILABLE, VoteQuestTickets
    from .command_text_overlay import load_command_text_overrides
    from .mission_window_revival import (GrowthCampaignWindowMismatch, MissionWindowMismatch, load_mission_window_rules,
                                         verify_growth_campaign_windows, verify_mission_windows)
    from .subjugation_rules import SubjugationCatalog, load_subjugation_rules
    from .tower_catalog import TowerCatalog
    from .tower_service import TowerService
    from .tower_state import load_tower_rules
    from .serial_code_rules import load_serial_code_rules
    from .serial_code_service import SerialCodeService
    from .letstd_catalog import LetsTDCatalog
    from .letstd_rules import load_letstd_rules
    from .letstd_service import LetsTDService
    masters = MasterDataStore.for_data_dir(args.data_dir)
    selected_snapshot = getattr(args, "master_snapshot", None)
    quest_rules = load_quest_rules(getattr(args, "quest_rules", None))
    training_rules = load_training_rules(getattr(args, "training_rules", None))
    silver_rules = load_silver_house_rules(getattr(args, "silver_house_rules", None))
    evolution_rules = load_evolution_rules(getattr(args, "evolution_rules", None))
    recovery_rules = load_recovery_rules(getattr(args, "recovery_rules", None))
    trade_rules = load_trade_rules(getattr(args, "trade_rules", None))
    ticket_rules = load_ticket_rules(getattr(args, "ticket_rules", None))
    item_ticket_rules = load_item_ticket_rules(getattr(args, "item_ticket_rules", None))
    login_rules = load_login_rules(getattr(args, "login_rules", None))
    event_pass_rules = load_event_pass_rules(getattr(args, "event_pass_rules", None))
    revival_rules = load_revival_rules(getattr(args, "event_pass_revival", None))
    event_login_rules = load_event_login_rules(getattr(args, "event_login_rules", None))
    comeback_login_rules = load_comeback_login_rules(getattr(args, "comeback_login_rules", None))
    purchased_login_rules = load_purchased_login_rules(getattr(args, "purchased_login_rules", None))
    daily_gift_rules = load_daily_gift_rules(getattr(args, "daily_gift_rules", None))
    vote_rules = load_vote_rules(getattr(args, "vote_rules", None))
    vote_cooking_rules = load_vote_cooking_rules(getattr(args, "vote_cooking_rules", None))
    entrust_rules = load_entrust_rules(getattr(args, "entrust_rules", None))
    mission_window_rules = load_mission_window_rules(getattr(args, "mission_windows", None))
    command_text_overrides = load_command_text_overrides(getattr(args, "command_text_overrides", None))
    tower_rules = load_tower_rules(getattr(args, "tower_rules", None))
    subjugation_rules = load_subjugation_rules(getattr(args, "subjugation_rules", None))
    serial_code_rules = load_serial_code_rules(getattr(args, "serial_code_rules", None))
    letstd_rules = load_letstd_rules(getattr(args, "letstd_rewards", None))
    snapshot = None
    quest_error = None
    try:
        snapshot = masters.snapshot(selected_snapshot)
    except ProtocolError as error:
        if selected_snapshot is not None:
            # The fallback reads the active pointer, which is not the snapshot this build was pinned to.
            raise
        quest_error = error.code
    master_provider = snapshot.get_xml if snapshot is not None else masters.get_xml
    resource_root = getattr(args, "resource_dir", DEFAULT_RESOURCES)
    native_cdn = None
    resource_error = None
    resource_view = None
    try:
        resource_view = load_resource_view(resource_root, args.asset_dir, masters.expected_hash,
                                           master_provider=master_provider, revival_rules=revival_rules,
                                           event_login_rules=event_login_rules, purchased_login_rules=purchased_login_rules,
                                           mission_window_rules=mission_window_rules,
                                           command_text_overrides=command_text_overrides)
        resources, resource_entries = resource_view.manifest, resource_view.entries
    except (ValueError, OSError, RecursionError) as error:
        resources, resource_entries = {}, ()
        resource_error = str(error)
    if resources:
        try:
            fixed_objects, game_info = load_bootstrap(resource_root / "native-bootstrap", masters.expected_hash)
            native_cdn = NativeCdnService(resources["version"], resource_view.index, resource_entries, resource_root, args.asset_dir,
                extra_objects=fixed_objects, game_info_text=game_info, replacements=resource_view.replacements,
                base_version=resources["base_version"])
        except (ValueError, OSError, RecursionError) as error:
            resource_error = str(error)
    host = getattr(args, "host", DEFAULT_HOST)
    authority = f"[{host}]" if ":" in host else host
    asset_base = f"http://{authority}:{getattr(args, 'port', DEFAULT_PORT)}/assets/"
    indexed_assets = native_cdn.assets if native_cdn is not None else (resource_view.assets if resource_view else None)
    asset_reader = (lambda name: indexed_assets.read(name)[0]) if indexed_assets else None
    possessions = PossessionService(args.asset_dir, asset_reader=asset_reader)
    card_status = None
    card_status_error = None
    if snapshot is not None:
        try:
            card_status = CardStatusService(snapshot)
        except ProtocolError as error:
            card_status_error = error.code
    else:
        card_status_error = "masterdata_missing"
    bootstrap = BootstrapService(args.asset_dir, master_provider=master_provider,
                                 local_rules=load_rules(args.rules), asset_base_url=asset_base,
                                 asset_version=resources.get("version"), possession_provider=possessions.startup_response,
                                 filter_provider=filter_response,
                                 card_status_provider=card_status.response if card_status is not None else None,
                                 peruse_provider=peruse_response, universal_flag_provider=flags_response,
                                 background_provider=background_response,
                                 startup_diagnostics=getattr(args, "startup_diagnostics", None))
    unit_capacity, unit_capacity_error = None, None
    if snapshot is not None and asset_reader is not None:
        try:
            unit_capacity = UnitCapacity(snapshot, asset_reader)
        except ProtocolError as error:
            unit_capacity_error = error.code
    else:
        unit_capacity_error = "unit_capacity_dependencies_unavailable"

    def unavailable_capacity(state):
        raise ProtocolError("unit_capacity_unavailable", "Verified barracks capacity is unavailable", 503)

    player_state = PlayerStateService(args.asset_dir, master_provider, asset_reader=asset_reader,
        capacity_provider=unit_capacity.available if unit_capacity is not None else unavailable_capacity)
    recovery, recovery_error = None, None
    try:
        recovery = RecoveryService(bootstrap, recovery_seconds=quest_rules.charisma_recovery_seconds, rules=recovery_rules,
                                   client_sha256=masters.expected_hash)
    except ProtocolError as error:
        recovery_error = error.code
    presents = PresentService(bootstrap, player_state, asset_reader=asset_reader, possessions=possessions, recovery=recovery)
    def unavailable_squad_emblem(index, state):
        raise ProtocolError("emblem_catalog_unavailable", "New squad titles require the verified emblem catalog", 503)
    player_state.squad_emblem_validator = (presents.emblem_catalog.require_eligible
                                         if presents.emblem_catalog is not None else unavailable_squad_emblem)
    bootstrap.present_provider = presents.response
    unit_actions = UnitActionService(bootstrap, player_state)
    filters = FilterService(bootstrap)
    session_stubs = SessionStubService(bootstrap)
    summon_schedule = SummonScheduleService(bootstrap)
    peruse = PeruseService(bootstrap, asset_reader=asset_reader)
    peruse_episodes = PeruseEpisodeService(bootstrap, asset_reader=asset_reader)
    view_events, view_event_error = None, None
    try:
        view_events = ViewEventService(bootstrap, player_state)
    except ProtocolError as error:
        view_event_error = error.code
    mission_window_revival = resource_view.mission_window if resource_view is not None else None
    mission_windows, mission_window_error = None, None
    if asset_reader is not None:
        try:
            mission_windows = verify_mission_windows(asset_reader, mission_window_revival)
        except MissionWindowMismatch as error:
            mission_window_error = error.code
            mission_windows = {**mission_window_revival.metadata,
                               "verification": {"verified": False, "mismatches": list(error.mismatches)}}
    quests = None
    if snapshot is not None and asset_reader is not None and mission_window_error is None:
        try:
            catalog = QuestCatalog(snapshot, asset_reader, possessions=possessions, resource_version=resources["version"])
            quests = QuestService(catalog, bootstrap, player_state, possessions, rules=quest_rules)
            quests.presents = presents
            quests.card_status = card_status
            quests.subjugation = SubjugationCatalog(catalog, rules=subjugation_rules, events=quests.event_missions)
        except ProtocolError as error:
            quest_error = error.code
    elif quest_error is None:
        quest_error = mission_window_error or "resource_index_missing"
    subjugation_error = None if quests is not None else "subjugation_dependencies_unavailable"
    quest_records = QuestRecordService(bootstrap, quests.catalog, asset_reader=asset_reader) if quests is not None else None
    quest_record_error = None if quests is not None else "quest_record_dependencies_unavailable"
    gifts = None
    gift_error = None
    if snapshot is not None and asset_reader is not None and card_status is not None and presents.is_ready() and possessions.is_ready():
        try:
            gift_catalog = GiftCatalog(snapshot, asset_reader, possessions, resource_version=resources["version"])
            aliases = ImageAliasCatalog(asset_reader, gift_catalog.card_ids)
            # 4D5110 reads a sortie unit's gold ability from the card its image alias names.
            if quests is not None:
                quests.image_aliases = aliases
            gifts = GiftService(bootstrap, player_state, possessions, card_status, presents, gift_catalog, aliases)
        except ProtocolError as error:
            gift_error = error.code
    else:
        gift_error = "gift_dependencies_unavailable"
    evolution_rewards, evolution_rewards_error = None, None
    if gifts is not None:
        try:
            evolution_rewards = EvolutionRewards(gifts.catalog, player_state, card_status, presents)
        except ProtocolError as error:
            evolution_rewards_error = error.code
    else:
        evolution_rewards_error = "evolution_rewards_dependencies_unavailable"
    training = None
    training_error = None
    fairy_farm = None
    fairy_farm_error = None
    if gifts is not None and asset_reader is not None:
        try:
            fairy_farm = FairyFarmService(bootstrap, player_state, gifts, asset_reader, barracks=unit_capacity)
        except ProtocolError as error:
            fairy_farm_error = error.code
    else:
        fairy_farm_error = "fairy_farm_dependencies_unavailable"
    silver_house = None
    silver_house_error = None
    if gifts is not None and asset_reader is not None:
        try:
            silver_house = SilverHouseService(bootstrap, player_state, gifts.catalog, gifts, asset_reader, rules=silver_rules)
        except ProtocolError as error:
            silver_house_error = error.code
            silver_house = SilverHouseService(bootstrap, player_state, gifts.catalog)
    else:
        silver_house_error = "silver_house_dependencies_unavailable"
    if gifts is not None and asset_reader is not None:
        try:
            training_catalog = TrainingCatalog(gifts.catalog, player_state, asset_reader, gifts.aliases,
                                               rules=training_rules, fairy_farm=fairy_farm,
                                               silver_house=silver_house if silver_house and silver_house.status()["write_ready"] else None)
            training = TrainingService(bootstrap, player_state, training_catalog, gifts, barracks=unit_capacity)
        except ProtocolError as error:
            training_error = error.code
    else:
        training_error = "training_dependencies_unavailable"
    sales = None
    sale_error = None
    if gifts is not None and asset_reader is not None:
        try:
            sale_catalog = UnitSaleCatalog(gifts.catalog, possessions, asset_reader)
            sales = UnitSaleService(bootstrap, player_state, possessions, gifts, sale_catalog,
                                    evolution_rewards=evolution_rewards)
        except ProtocolError as error:
            sale_error = error.code
    else:
        sale_error = "sale_dependencies_unavailable"
    evolution, evolution_error = None, None
    if training is not None and evolution_rewards is not None:
        try:
            growth = training.catalog
            materials = EvolutionMaterials(growth.cards, growth.classes, growth.rarities)
            catalog = EvolutionCatalog(growth, possessions, materials, rules=evolution_rules)
            evolution = EvolutionService(bootstrap, player_state, catalog, gifts, fairy_farm=fairy_farm,
                silver_house=silver_house if silver_house and silver_house.status()["write_ready"] else None,
                rewards=evolution_rewards, barracks=unit_capacity)
        except ProtocolError as error:
            evolution_error = error.code
    else:
        evolution_error = "evolution_dependencies_unavailable"
    unit_detail, unit_detail_error, unit_skin_error = None, None, None
    if training is not None and asset_reader is not None:
        skins = None
        try:
            skins = UnitSkinCatalog(training.catalog.cards, possessions, asset_reader)
        except ProtocolError as error:
            unit_skin_error = error.code
        try:
            unit_detail = UnitDetailService(bootstrap, player_state, training.catalog,
                smartphone_images=smartphone_images_enabled(asset_reader), skin_catalog=skins)
        except ProtocolError as error:
            unit_detail_error = error.code
    else:
        unit_detail_error = "unit_detail_dependencies_unavailable"
    orb_exchange, orb_exchange_error = None, None
    if asset_reader is not None and possessions.is_ready():
        try:
            orb_exchange = OrbExchangeService(bootstrap, player_state, possessions, asset_reader)
        except ProtocolError as error:
            orb_exchange_error = error.code
    else:
        orb_exchange_error = "orb_exchange_dependencies_unavailable"
    guide, guide_error = None, None
    if gifts is not None and asset_reader is not None:
        try:
            guide = GuideService(bootstrap, player_state, gifts.catalog, asset_reader)
        except ProtocolError as error:
            guide_error = error.code
    else:
        guide_error = "guide_dependencies_unavailable"
    barracks, barracks_error = None, None
    if unit_capacity is not None and asset_reader is not None:
        try:
            barracks = BarracksService(bootstrap, player_state, unit_capacity, asset_reader,
                                      client_sha256=masters.expected_hash)
        except ProtocolError as error:
            barracks_error = error.code
    else:
        barracks_error = "barracks_dependencies_unavailable"
    trades, trade_error = None, None
    if gifts is not None and asset_reader is not None:
        try:
            trade_catalog = TradeCatalog(gifts.catalog, possessions, asset_reader, resource_version=resources["version"],
                                         earlier_resources=lambda: resource_view.unclosed_resources)
            trade_grants = TradeUnitGrants(player_state)
            silver_trade, silver_trade_error = None, None
            try:
                if evolution_rewards is None:
                    raise ProtocolError("silver_trade_history_unavailable", "Silver trades require verified legacy reward history", 503)
                silver_trade = SilverTradeCatalog(trade_catalog, gifts.catalog, asset_reader)
            except ProtocolError as error:
                silver_trade_error = error.code
            growth_shop, growth_shop_error = None, None
            try:
                if training is None or evolution_rewards is None:
                    raise ProtocolError("shop_growth_dependencies_unavailable", "Growth trades require verified growth and reward history", 503)
                growth_shop = ShopGrowthCatalog(trade_catalog, training.catalog, asset_reader, rules=evolution_rules)
            except ProtocolError as error:
                growth_shop_error = error.code
            trades = TradeService(bootstrap, player_state, possessions, trade_catalog, grants=trade_grants,
                                  rules=trade_rules, silver=silver_trade, gifts=gifts, evolution_rewards=evolution_rewards,
                                  silver_error=silver_trade_error, growth_shop=growth_shop, growth_shop_error=growth_shop_error)
            bootstrap.exchange_history_provider = trades.history_response
        except ProtocolError as error:
            trade_error = error.code
    else:
        trade_error = "trade_dependencies_unavailable"
    tower_trades, tower_trade_error = None, None
    if gifts is not None and asset_reader is not None and presents.emblem_catalog is not None:
        try:
            tower_catalog = TowerTradeCatalog(asset_reader, possessions, gifts.catalog, emblems=presents.emblem_catalog)
            tower_grants = TowerTradeGrants(player_state, possessions, tower_catalog)
            tower_trades = TowerTradeService(bootstrap, player_state, possessions, tower_catalog, grants=tower_grants)
        except ProtocolError as error:
            tower_trade_error = error.code
    else:
        tower_trade_error = "tower_trade_dependencies_unavailable"
    fame_catalog, fame_repository, fame_error = None, None, None
    if training is not None and evolution is not None and asset_reader is not None:
        try:
            fame_catalog = FameCatalog(training.catalog, evolution.catalog, asset_reader)
            fame_repository = FameRepository(fame_catalog, diagnostics=bootstrap.startup_diagnostics)
            bootstrap.fame_provider = fame_repository.response
        except ProtocolError as error:
            fame_error = error.code
    else:
        fame_error = "fame_catalog_dependencies_unavailable"
    summon_pools, summon_tickets, summon_ticket_error = None, None, None
    if asset_reader is not None and gifts is not None:
        try:
            summon_pools = SummonPoolCatalog(gifts.catalog, asset_reader)
            summon_tickets = SummonTicketService(bootstrap, player_state, presents, summon_pools, rules=ticket_rules)
            presents.tickets = summon_tickets
        except ProtocolError as error:
            summon_ticket_error = error.code
    else:
        summon_ticket_error = "summon_ticket_dependencies_unavailable"
    item_summon_tickets, item_summon_error = None, None
    if asset_reader is not None and summon_pools is not None:
        try:
            item_summon_catalog = ItemSummonCatalog(summon_pools, asset_reader)
            item_summon_tickets = ItemSummonTicketService(bootstrap, player_state, presents, summon_pools,
                item_summon_catalog, rules=item_ticket_rules)
            presents.item_tickets = item_summon_tickets
        except ProtocolError as error:
            item_summon_error = error.code
    else:
        item_summon_error = "item_summon_dependencies_unavailable"
    towers, tower_error = None, None
    if quests is not None:
        try:
            towers = TowerService(bootstrap, player_state, possessions, presents,
                                  TowerCatalog(quests.catalog, possessions), rules=tower_rules)
            quests.tower = towers
        except ProtocolError as error:
            tower_error = error.code
    else:
        tower_error = "tower_dependencies_unavailable"
    selection_tickets, selection_ticket_error = None, None
    if asset_reader is not None and gifts is not None:
        try:
            selection_catalog = SelectTicketCatalog(gifts.catalog, asset_reader)
            selection_tickets = SelectTicketService(bootstrap, player_state, presents, selection_catalog)
            presents.selection_tickets = selection_tickets
        except ProtocolError as error:
            selection_ticket_error = error.code
    else:
        selection_ticket_error = "select_ticket_dependencies_unavailable"
    votes, vote_error = None, None
    vote_cooking, vote_cooking_error = None, None
    if asset_reader is not None and snapshot is not None:
        try:
            vote_catalog = VoteCatalog(asset_reader, master_provider, vote_rules)
            try:
                recipes = VoteCookingCatalog(asset_reader, master_provider)
                presents.vote_cooking_catalog = recipes
                vote_cooking = VoteCookingService(vote_catalog, recipes, presents, vote_cooking_rules)
            except ProtocolError as error:
                vote_cooking_error = error.code
            votes = VoteService(bootstrap, player_state, vote_catalog, possessions=possessions, presents=presents,
                                cooking=vote_cooking)
            bootstrap.vote_provider = votes.response
        except ProtocolError as error:
            vote_error = error.code
    else:
        vote_error = "vote_dependencies_unavailable"
    if vote_cooking is None and vote_cooking_error is None:
        vote_cooking_error = "vote_cooking_dependencies_unavailable"
    vote_cooking_notifier, vote_cooking_notifier_error = None, None
    if vote_cooking is not None:
        try:
            vote_cooking_notifier = VoteCookingNotifications(vote_cooking)
        except ProtocolError as error:
            vote_cooking_notifier_error = error.code
    else:
        vote_cooking_notifier_error = "vote_cooking_notifications_dependencies_unavailable"
    vote_expiry_notifier, vote_expiry_notifier_error = None, "vote_expiry_notifications_dependencies_unavailable"
    vote_assault_notifier, vote_assault_notifier_error = None, "vote_assault_notifications_dependencies_unavailable"
    if votes is not None and votes.transactions is not None:
        try:
            vote_expiry_notifier = VoteExpiryNotifications(votes.transactions.expiry)
            vote_expiry_notifier_error = None
        except ProtocolError as error:
            vote_expiry_notifier_error = error.code
        try:
            vote_assault_notifier = VoteAssaultNotifications(votes.transactions.assault_expiry, asset_reader)
            vote_assault_notifier_error = None
        except ProtocolError as error:
            vote_assault_notifier_error = error.code
    vote_tickets = votes.transactions.tickets if votes is not None and votes.transactions is not None else None
    quest_tickets, vote_quest_ticket_error = None, VOTE_QUEST_TICKETS_UNAVAILABLE
    if quests is not None and votes is not None and votes.transactions is not None:
        try:
            quest_tickets = VoteQuestTickets(votes.catalog, possessions, votes.transactions.expiry, asset_reader)
            vote_quest_ticket_error = None
        except ProtocolError as error:
            vote_quest_ticket_error = error.code
    if quests is not None:
        # Attaching changes the event reward content version, so it precedes the services built over quests.
        quests.event_rewards.attach_vote_tickets(quest_tickets, vote_quest_ticket_error)
    event_passes, event_pass_error = None, None
    if asset_reader is not None and possessions.is_ready() and presents.is_ready():
        try:
            event_pass_catalog = EventPassCatalog(asset_reader, possessions)
            event_passes = EventPassService(bootstrap, player_state, presents, possessions, event_pass_catalog,
                                           automation_rules=event_pass_rules)
            bootstrap.event_pass_provider = event_passes.response
        except ProtocolError as error:
            event_pass_error = error.code
    else:
        event_pass_error = "event_pass_dependencies_unavailable"
    event_login_catalog, event_login_catalog_error = None, None
    if asset_reader is not None:
        try:
            event_login_catalog = EventLoginCatalog(asset_reader)
        except ProtocolError as error:
            event_login_catalog_error = error.code
    purchased_logins, purchased_login_error = None, None
    if event_login_catalog is not None:
        try:
            purchased_catalog = PurchasedLoginCatalog(event_login_catalog, asset_reader, purchased_login_rules)
            purchased_logins = PurchasedLoginService(purchased_catalog, presents, purchased_login_rules)
        except ProtocolError as error:
            purchased_login_error = error.code
    else:
        purchased_login_error = event_login_catalog_error or "purchased_login_dependencies_unavailable"
    packages, package_error = None, None
    if event_passes is not None and asset_reader is not None and possessions.is_ready():
        try:
            package_catalog = PackageCatalog(event_passes.catalog, possessions, asset_reader,
                                             enabled=revival_rules is not None)
            packages = PackageService(bootstrap, player_state, possessions, package_catalog,
                                      purchased_logins=purchased_logins)
        except ProtocolError as error:
            package_error = error.code
    else:
        package_error = "package_dependencies_unavailable"
    missions, mission_error = None, None
    if asset_reader is not None and presents.is_ready():
        try:
            mission_catalog = MissionCatalog(asset_reader, resource_version=resources["version"])
            base_missions = MissionCatalog(lambda name: resource_view.base_assets.read(name)[0],
                                           resource_version=resources["base_version"])
            ep_bindings = MissionEpBindings(base_missions, mission_catalog)
            if revival_rules is not None and event_passes is None:
                raise ProtocolError("mission_pass_dependencies_unavailable", "Revival missions require the verified event pass service", 503)
            pass_points = MissionPassPoints(event_passes) if event_passes is not None else None
            dgp_login = (masters.expected_hash in SUPPORTED_CLIENT_SHA256
                         and client_fingerprint(args.asset_dir) == masters.expected_hash)
            mission_skins = unit_detail.skin_catalog if unit_detail is not None else None
            clear_specs = (QuestClearSpecs.build(mission_catalog.definitions, quests.catalog)
                           if quests is not None else None)
            missions = MissionService(bootstrap, player_state, presents, mission_catalog,
                progress=MissionProgress(mission_catalog, dgp_login=dgp_login,
                    skin_items=mission_skins.counted_item_ids if mission_skins is not None else (),
                    fame_cards=fame_catalog.counted_card_ids if fame_catalog is not None else (),
                    fame_support_cards=fame_catalog.support_card_ids if fame_catalog is not None else (),
                    clear_specs=clear_specs),
                skins=mission_skins, fame=fame_catalog, fame_status=card_status if fame_catalog is not None else None,
                ep_bindings=ep_bindings, pass_points=pass_points)
            for service in (bootstrap, training, gifts, evolution, quests, presents):
                if service is not None:
                    service.missions = missions
        except ProtocolError as error:
            mission_error = error.code
    else:
        mission_error = "mission_dependencies_unavailable"
    fame = None
    if (fame_repository is not None and missions is not None and card_status is not None
            and gifts is not None and evolution_rewards is not None and unit_capacity is not None):
        try:
            fame = FameService(bootstrap, player_state, fame_repository, fame_catalog, card_status,
                               gifts, evolution_rewards, missions, barracks=unit_capacity)
        except ProtocolError as error:
            fame_error = error.code
    elif fame_error is None:
        fame_error = "fame_service_dependencies_unavailable"
    blessings, blessing_error = None, None
    if asset_reader is not None and possessions.is_ready():
        try:
            blessing_catalog = BlessingCatalog(possessions, asset_reader)
            blessings = BlessingService(bootstrap, player_state, possessions, blessing_catalog)
        except ProtocolError as error:
            blessing_error = error.code
    else:
        blessing_error = "blessing_dependencies_unavailable"
    skin_shop, skin_shop_error = None, None
    if asset_reader is not None and missions is not None and missions.skins is not None:
        try:
            skin_catalog = SkinShopCatalog(possessions, missions.skins, asset_reader)
            skin_shop = SkinShopService(bootstrap, player_state, possessions, skin_catalog, missions)
        except ProtocolError as error:
            skin_shop_error = error.code
    else:
        skin_shop_error = "skin_shop_dependencies_unavailable"
    universal_flags, universal_flags_error = None, None
    if asset_reader is not None:
        try:
            universal_flags = UniversalFlagsService(bootstrap, UniversalFlagsCatalog(asset_reader))
        except ProtocolError as error:
            universal_flags_error = error.code
    else:
        universal_flags_error = "universal_flags_dependencies_unavailable"
    backgrounds, background_error = None, None
    if asset_reader is not None and gifts is not None:
        try:
            background_catalog = BackgroundCatalog(gifts.catalog, asset_reader)
            backgrounds = BackgroundService(bootstrap, player_state, background_catalog)
        except ProtocolError as error:
            background_error = error.code
    else:
        background_error = "background_dependencies_unavailable"
    event_logins, event_login_error = None, None
    if event_login_catalog is not None and all(value is not None for value in (summon_tickets, item_summon_tickets, selection_tickets)):
        try:
            event_logins = EventLoginService(event_login_catalog, presents, event_login_rules)
        except ProtocolError as error:
            event_login_error = error.code
    else:
        event_login_error = "event_login_dependencies_unavailable"
    comeback_logins, comeback_login_error = None, None
    if event_logins is not None:
        try:
            comeback_catalog = ComebackLoginCatalog(event_logins.catalog)
            comeback_logins = ComebackLoginService(comeback_catalog, presents, comeback_login_rules)
            bootstrap.comeback_logins = comeback_logins
        except ProtocolError as error:
            comeback_login_error = error.code
    else:
        comeback_login_error = "comeback_login_dependencies_unavailable"
    daily_gifts, daily_gift_error = None, None
    if asset_reader is not None and presents.is_ready():
        try:
            daily_catalog = DailyGiftCatalog(asset_reader, daily_gift_rules)
            daily_gifts = DailyGiftService(daily_catalog, presents, daily_gift_rules)
        except ProtocolError as error:
            daily_gift_error = error.code
    else:
        daily_gift_error = "daily_gift_dependencies_unavailable"
    letstd, letstd_error = None, None
    if asset_reader is not None and presents.is_ready():
        try:
            # Its dependency fingerprint includes presents.item_tickets, so it is built after the ticket services.
            letstd = LetsTDService(LetsTDCatalog(asset_reader), presents, letstd_rules)
        except ProtocolError as error:
            letstd_error = error.code
    else:
        letstd_error = "letstd_dependencies_unavailable"
    login_rewards, login_reward_error = None, None
    if asset_reader is not None and summon_tickets is not None:
        try:
            login_catalog = LoginRewardCatalog(asset_reader, resource_version=resources["version"])
            login_rewards = LoginRewardService(bootstrap, player_state, presents, login_catalog, rules=login_rules,
                                               event_logins=event_logins, comeback_logins=comeback_logins,
                                               purchased_logins=purchased_logins, daily_gifts=daily_gifts,
                                               vote_cooking=vote_cooking, vote_tickets=vote_tickets, letstd=letstd)
            bootstrap.login_reward_seed_provider = login_rewards.seed_new
            bootstrap.monthly_state_provider = login_rewards.prepare_month
        except ProtocolError as error:
            login_reward_error = error.code
    else:
        login_reward_error = "login_reward_dependencies_unavailable"
    rank_rewards, rank_reward_error = None, None
    if asset_reader is not None and all(service is not None for service in (
            summon_tickets, item_summon_tickets, selection_tickets)):
        try:
            rank_catalog = RankBonusCatalog(asset_reader)
            rank_rewards = RankRewardService(bootstrap, player_state, presents, rank_catalog, event_passes=event_passes,
                                             event_logins=event_logins, comeback_logins=comeback_logins,
                                             purchased_logins=purchased_logins, daily_gifts=daily_gifts,
                                             vote_tickets=vote_tickets, vote_cooking_notifier=vote_cooking_notifier,
                                             letstd=letstd, vote_expiry_notifier=vote_expiry_notifier,
                                             vote_assault_notifier=vote_assault_notifier)
            bootstrap.rank_reward_seed_provider = rank_rewards.seed_new
        except ProtocolError as error:
            rank_reward_error = error.code
    else:
        rank_reward_error = "rank_reward_dependencies_unavailable"
    if quests is not None:
        quests.rank_rewards = rank_rewards
    entrust, entrust_error = None, None
    if quests is not None and quests.missions is not None and rank_rewards is not None:
        try:
            # Entrust waives tickets by these windows, so the served table is read back before the catalog assembles.
            verify_growth_campaign_windows(mission_window_rules, asset_reader)
            entrust = EntrustService(quests, EntrustCatalog(quests.catalog), rules=entrust_rules)
        except (GrowthCampaignWindowMismatch, ProtocolError) as error:
            entrust_error = error.code
    else:
        entrust_error = "entrust_dependencies_unavailable"
    current_squad, current_squad_error = None, None
    if asset_reader is not None and presents.emblem_catalog is not None:
        try:
            current_squad = CurrentSquadService(bootstrap, player_state, presents.emblem_catalog, asset_reader)
        except ProtocolError as error:
            current_squad_error = error.code
    else:
        current_squad_error = "current_squad_dependencies_unavailable"
    sugoroku, sugoroku_error = None, None
    if asset_reader is not None and login_rewards is not None:
        try:
            sugoroku_catalog = SugorokuCatalog(asset_reader, resource_version=resources["version"])
            sugoroku = SugorokuService(bootstrap, player_state, presents, sugoroku_catalog,
                                      recovery_seconds=quest_rules.charisma_recovery_seconds)
            def prepare_month(state, now):
                return login_rewards.prepare_month(sugoroku.prepare_month(state, now), now)
            bootstrap.monthly_state_provider = prepare_month
        except ProtocolError as error:
            sugoroku_error = error.code
    else:
        sugoroku_error = "sugoroku_dependencies_unavailable"
    serial_codes, serial_code_error = None, None
    try:
        # The fingerprint covers every ticket service attached to presents, so it is taken after all of them.
        serial_codes = SerialCodeService(bootstrap, presents, serial_code_rules)
    except ProtocolError as error:
        serial_code_error = error.code
    return ServerServices(masters=masters, bootstrap=bootstrap, player_state=player_state, possessions=possessions,
        resource_entries=resource_entries, native_cdn=native_cdn, resource_error=resource_error,
        quests=quests, quest_error=quest_error, unit_actions=unit_actions, filters=filters, session_stubs=session_stubs,
        summon_schedule=summon_schedule,
        card_status=card_status, card_status_error=card_status_error, presents=presents,
        gifts=gifts, gift_error=gift_error, peruse=peruse, peruse_episodes=peruse_episodes,
        training=training, training_error=training_error,
        fairy_farm=fairy_farm, fairy_farm_error=fairy_farm_error, sales=sales, sale_error=sale_error,
        silver_house=silver_house, silver_house_error=silver_house_error,
        unit_capacity=unit_capacity, unit_capacity_error=unit_capacity_error,
        evolution=evolution, evolution_error=evolution_error,
        evolution_rewards=evolution_rewards, evolution_rewards_error=evolution_rewards_error,
        unit_detail=unit_detail, unit_detail_error=unit_detail_error, unit_skin_error=unit_skin_error,
        orb_exchange=orb_exchange, orb_exchange_error=orb_exchange_error,
        guide=guide, guide_error=guide_error, barracks=barracks, barracks_error=barracks_error,
        recovery=recovery, recovery_error=recovery_error, trades=trades, trade_error=trade_error,
        tower_trades=tower_trades, tower_trade_error=tower_trade_error,
        entrust=entrust, entrust_error=entrust_error,
        towers=towers, tower_error=tower_error, subjugation_error=subjugation_error,
        quest_records=quest_records, quest_record_error=quest_record_error,
        vote_quest_ticket_error=vote_quest_ticket_error, mission_windows=mission_windows,
        mission_window_revival=mission_window_revival, mission_window_error=mission_window_error,
        command_texts=resource_view.command_text_metadata if resource_view is not None else None,
        shop_window_closure=resource_view.shop_window_closure_metadata if resource_view is not None else None,
        missions=missions, mission_error=mission_error, blessings=blessings, blessing_error=blessing_error,
        skin_shop=skin_shop, skin_shop_error=skin_shop_error,
        universal_flags=universal_flags, universal_flags_error=universal_flags_error,
        fame=fame, fame_repository=fame_repository, fame_error=fame_error,
        backgrounds=backgrounds, background_error=background_error,
        summon_pools=summon_pools, summon_tickets=summon_tickets, summon_ticket_error=summon_ticket_error,
        login_rewards=login_rewards, login_reward_error=login_reward_error,
        current_squad=current_squad, current_squad_error=current_squad_error,
        sugoroku=sugoroku, sugoroku_error=sugoroku_error,
        item_summon_tickets=item_summon_tickets, item_summon_error=item_summon_error,
        selection_tickets=selection_tickets, selection_ticket_error=selection_ticket_error,
        rank_rewards=rank_rewards, rank_reward_error=rank_reward_error,
        view_events=view_events, view_event_error=view_event_error,
        event_passes=event_passes, event_pass_error=event_pass_error,
        packages=packages, package_error=package_error,
        event_logins=event_logins, event_login_error=event_login_error,
        comeback_logins=comeback_logins, comeback_login_error=comeback_login_error,
        purchased_logins=purchased_logins, purchased_login_error=purchased_login_error,
        daily_gifts=daily_gifts, daily_gift_error=daily_gift_error,
        votes=votes, vote_error=vote_error,
        vote_cooking=vote_cooking, vote_cooking_error=vote_cooking_error,
        vote_cooking_notifier=vote_cooking_notifier, vote_cooking_notifier_error=vote_cooking_notifier_error,
        vote_expiry_notifier=vote_expiry_notifier, vote_expiry_notifier_error=vote_expiry_notifier_error,
        vote_assault_notifier=vote_assault_notifier, vote_assault_notifier_error=vote_assault_notifier_error,
        serial_codes=serial_codes, serial_code_error=serial_code_error, letstd=letstd, letstd_error=letstd_error,
        event_pass_revival=(dict(resource_view.revival_metadata)
                           if resource_view is not None and resource_view.revival_metadata is not None else None),
        master_snapshot=None if snapshot is None else snapshot.identifier)


def readiness(masters, bootstrap, snapshot: str | None = None) -> dict:
    master_state = masters.status(snapshot)
    bootstrap_state = bootstrap.status()
    return {"startup_ready": master_state["ready"] and bootstrap_state.get("startup_ready", False),
            "masterdata": master_state, "bootstrap": bootstrap_state}


def service_readiness(bundle: ServerServices) -> dict:
    result = readiness(bundle.masters, bundle.bootstrap, bundle.master_snapshot)
    cdn_ready = bundle.native_cdn is not None and bundle.native_cdn.status()["ready"]
    result["startup_ready"] = result["startup_ready"] and cdn_ready
    result["native_cdn"] = {"ready": cdn_ready, "error": bundle.resource_error}
    result["player_state"] = bundle.player_state.status()
    result["unit_capacity"] = bundle.unit_capacity.status() if bundle.unit_capacity is not None else {
        "available": False, "error": bundle.unit_capacity_error}
    result["startup_ready"] = result["startup_ready"] and result["player_state"]["ready"]
    result["possessions"] = bundle.possessions.status()
    result["startup_ready"] = result["startup_ready"] and result["possessions"]["ready"]
    result["main_quests"] = bundle.quests.status() if bundle.quests else {"available": False, "error": bundle.quest_error}
    result["quest_records"] = bundle.quest_records.status() if bundle.quest_records is not None else {
        "ready": False, "error": bundle.quest_record_error}
    result["entrust"] = bundle.entrust.status() if bundle.entrust is not None else {"ready": False, "error": bundle.entrust_error}
    result["towers"] = bundle.towers.status() if bundle.towers is not None else {"ready": False, "error": bundle.tower_error}
    now = instant(bundle.bootstrap.clock())
    result["subjugation"] = (bundle.quests.subjugation.status(now)
                             if bundle.quests is not None and bundle.quests.subjugation is not None
                             else {"ready": False, "error": bundle.subjugation_error})
    windows = bundle.mission_windows or {"enabled": False}
    if bundle.mission_window_revival is not None and "verification" in windows:
        windows = {**bundle.mission_window_revival.describe(now), "verification": windows["verification"]}
    result["mission_windows"] = windows if bundle.mission_window_error is None else {
        **windows, "error": bundle.mission_window_error}
    result["command_texts"] = bundle.command_texts or {"enabled": False}
    result["shop_window_closure"] = bundle.shop_window_closure or {"enabled": False}
    result["unit_actions"] = bundle.unit_actions.status()
    result["filters"] = bundle.filters.status()
    result["session_stubs"] = bundle.session_stubs.status()
    result["summon_schedule"] = bundle.summon_schedule.status()
    result["card_status"] = {"available": bundle.card_status is not None, "error": bundle.card_status_error}
    result["presents"] = bundle.presents.status()
    result["startup_ready"] = result["startup_ready"] and result["presents"]["ready"]
    result["gifts"] = bundle.gifts.status() if bundle.gifts is not None else {"available": False, "error": bundle.gift_error}
    result["peruse"] = bundle.peruse.status()
    result["peruse_episodes"] = bundle.peruse_episodes.status()
    result["training"] = bundle.training.status() if bundle.training is not None else {"available": False, "error": bundle.training_error}
    result["fairy_farm"] = bundle.fairy_farm.status() if bundle.fairy_farm is not None else {"available": False, "error": bundle.fairy_farm_error}
    result["unit_sales"] = bundle.sales.status() if bundle.sales is not None else {"available": False, "error": bundle.sale_error}
    result["silver_house"] = {**(bundle.silver_house.status() if bundle.silver_house is not None else {"available": False}),
                              "error": bundle.silver_house_error}
    result["evolution"] = bundle.evolution.status() if bundle.evolution is not None else {
        "available": False, "error": bundle.evolution_error}
    result["evolution_rewards"] = bundle.evolution_rewards.status() if bundle.evolution_rewards is not None else {
        "available": False, "error": bundle.evolution_rewards_error}
    if bundle.gifts is not None and bundle.evolution_rewards is None:
        result["startup_ready"] = False
    result["unit_detail"] = bundle.unit_detail.status() if bundle.unit_detail is not None else {
        "available": False, "error": bundle.unit_detail_error}
    result["unit_detail"]["skin_error"] = bundle.unit_skin_error
    result["orb_exchange"] = bundle.orb_exchange.status() if bundle.orb_exchange is not None else {
        "available": False, "error": bundle.orb_exchange_error}
    result["guide"] = bundle.guide.status() if bundle.guide is not None else {
        "available": False, "error": bundle.guide_error}
    result["barracks"] = bundle.barracks.status() if bundle.barracks is not None else {
        "available": False, "error": bundle.barracks_error}
    result["recovery"] = bundle.recovery.status() if bundle.recovery is not None else {
        "available": False, "error": bundle.recovery_error}
    result["trades"] = bundle.trades.status() if bundle.trades is not None else {
        "available": False, "error": bundle.trade_error}
    result["tower_trades"] = bundle.tower_trades.status() if bundle.tower_trades is not None else {
        "ready": False, "error": bundle.tower_trade_error}
    result["missions"] = bundle.missions.status() if bundle.missions is not None else {
        "available": False, "error": bundle.mission_error}
    result["blessings"] = bundle.blessings.status() if bundle.blessings is not None else {
        "available": False, "error": bundle.blessing_error}
    result["skin_shop"] = bundle.skin_shop.status() if bundle.skin_shop is not None else {
        "available": False, "error": bundle.skin_shop_error}
    result["universal_flags"] = bundle.universal_flags.status() if bundle.universal_flags is not None else {
        "ready": False, "error": bundle.universal_flags_error}
    result["fame"] = bundle.fame.status() if bundle.fame is not None else {
        "available": False, "error": bundle.fame_error,
        "repository": bundle.fame_repository.status() if bundle.fame_repository is not None else None}
    result["backgrounds"] = bundle.backgrounds.status() if bundle.backgrounds is not None else {
        "available": False, "error": bundle.background_error}
    result["summon_tickets"] = bundle.summon_tickets.status() if bundle.summon_tickets is not None else {
        "available": False, "error": bundle.summon_ticket_error,
        "pools": bundle.summon_pools.status() if bundle.summon_pools is not None else None}
    result["login_rewards"] = bundle.login_rewards.status() if bundle.login_rewards is not None else {
        "ready": False, "error": bundle.login_reward_error}
    result["startup_ready"] = result["startup_ready"] and result["login_rewards"]["ready"]
    result["rank_rewards"] = bundle.rank_rewards.status() if bundle.rank_rewards is not None else {
        "ready": False, "error": bundle.rank_reward_error}
    result["startup_ready"] = result["startup_ready"] and result["rank_rewards"]["ready"]
    result["current_squad"] = bundle.current_squad.status() if bundle.current_squad is not None else {
        "ready": False, "error": bundle.current_squad_error}
    result["sugoroku"] = bundle.sugoroku.status() if bundle.sugoroku is not None else {
        "ready": False, "error": bundle.sugoroku_error}
    result["item_summon_tickets"] = bundle.item_summon_tickets.status() if bundle.item_summon_tickets is not None else {
        "ready": False, "error": bundle.item_summon_error}
    result["selection_tickets"] = bundle.selection_tickets.status() if bundle.selection_tickets is not None else {
        "ready": False, "error": bundle.selection_ticket_error}
    result["view_events"] = bundle.view_events.status() if bundle.view_events is not None else {
        "ready": False, "error": bundle.view_event_error}
    result["event_passes"] = bundle.event_passes.status() if bundle.event_passes is not None else {
        "ready": False, "error": bundle.event_pass_error}
    result["event_pass_revival"] = bundle.event_pass_revival or {"enabled": False}
    result["packages"] = bundle.packages.status() if bundle.packages is not None else {
        "ready": False, "error": bundle.package_error}
    result["event_logins"] = bundle.event_logins.status() if bundle.event_logins is not None else {
        "ready": False, "error": bundle.event_login_error}
    result["startup_ready"] = result["startup_ready"] and result["event_logins"]["ready"]
    result["comeback_logins"] = bundle.comeback_logins.status() if bundle.comeback_logins is not None else {
        "ready": False, "error": bundle.comeback_login_error}
    result["startup_ready"] = result["startup_ready"] and result["comeback_logins"]["ready"]
    result["purchased_logins"] = bundle.purchased_logins.status() if bundle.purchased_logins is not None else {
        "ready": False, "error": bundle.purchased_login_error}
    result["startup_ready"] = result["startup_ready"] and result["purchased_logins"]["ready"]
    result["daily_gifts"] = bundle.daily_gifts.status() if bundle.daily_gifts is not None else {
        "ready": False, "error": bundle.daily_gift_error}
    result["startup_ready"] = result["startup_ready"] and result["daily_gifts"]["ready"]
    result["votes"] = bundle.votes.status() if bundle.votes is not None else {
        "ready": False, "error": bundle.vote_error}
    result["vote_cooking"] = bundle.vote_cooking.status() if bundle.vote_cooking is not None else {
        "ready": False, "error": bundle.vote_cooking_error}
    result["vote_cooking_notifications"] = bundle.vote_cooking_notifier.status() if bundle.vote_cooking_notifier is not None else {
        "ready": False, "error": bundle.vote_cooking_notifier_error}
    if bundle.vote_cooking is not None and bundle.vote_cooking_notifier is None:
        # 115 keeps serving VoteGiftParam while 116 cannot clear it, so the pair must start together or not at all.
        result["startup_ready"] = False
    result["vote_expiry_notifications"] = bundle.vote_expiry_notifier.status() if bundle.vote_expiry_notifier is not None else {
        "ready": False, "error": bundle.vote_expiry_notifier_error}
    result["vote_assault_notifications"] = bundle.vote_assault_notifier.status() if bundle.vote_assault_notifier is not None else {
        "ready": False, "error": bundle.vote_assault_notifier_error}
    result["serial_codes"] = bundle.serial_codes.status() if bundle.serial_codes is not None else {
        "ready": False, "error": bundle.serial_code_error}
    result["letstd"] = bundle.letstd.status() if bundle.letstd is not None else {
        "ready": False, "error": bundle.letstd_error}
    if bundle.event_pass_revival is not None:
        points = result["missions"].get("pass_points")
        result["startup_ready"] = (result["startup_ready"] and result["missions"].get("ready", False)
            and result["event_passes"].get("ready", False) and isinstance(points, dict) and points.get("ready", False)
            and result["packages"].get("ready", False))
    result["auto_requests"] = auto_request_gaps(bundle, result["subjugation"].get("cooperative"))
    return result


def auto_request_gaps(bundle: ServerServices, cooperative: Sequence[Mapping] | None) -> dict:
    handled = service_handlers(bundle)
    known = cooperative is not None
    entries = list(cooperative or ())
    # A listed cooperative subjugation sends its 143 or 147 unprompted as the mission menu loads.
    listed = {entry["registration"] for entry in entries if "error" not in entry}
    operations = sorted({*AUTO_SENT_OPERATIONS, *listed})
    unhandled = [operation for operation in operations if operation not in handled]
    readable = known and all("error" not in entry for entry in entries)
    return {"ready": readable and not unhandled, "operations": operations, "unhandled": unhandled,
            "cooperative_subjugations": entries if known else None, "client": AUTO_REQUEST_AFTERMATH}


def warn_auto_request_gaps(gaps: Mapping | None) -> None:
    from .mission_window_revival import COOPERATIVE_REGISTRATIONS
    if not gaps:
        return
    registrations = " or ".join(map(str, COOPERATIVE_REGISTRATIONS.values()))
    if gaps["unhandled"]:
        log.warning("Operations %s have no handler, yet the client sends them unprompted as the home page or a menu "
                    "opens or a story ends, so it %s; run doctor for details", ", ".join(map(str, gaps["unhandled"])),
                    gaps["client"])
    if gaps["cooperative_subjugations"] is None:
        log.warning("The subjugation catalog is unreadable (see readiness.subjugation), so whether loading the mission "
                    "menu sends %s is unknown", registrations)
    for entry in gaps["cooperative_subjugations"] or ():
        if "error" in entry:
            log.warning("The SubjugationMissionConfig row of subjugation %s is unreadable (%s), so whether loading the "
                        "mission menu sends %s is unknown", entry["mission_id"], entry["error"]["code"], registrations)
        elif entry["registration"] in gaps["unhandled"]:
            window = entry["window"]
            log.warning("Cooperative subjugation %s (SubjugationType %s, %s, window %s to %s from %s) is listed in the "
                        "mission menu: loading the menu with it selected sends %s unprompted, so the client %s; serve "
                        "resources whose Enable is 0 for it", entry["mission_id"], entry["subjugation_type"],
                        entry["state"], window["start_at"], window["end_at"], window["source"],
                        entry["registration"], gaps["client"])


def _diagnose_saved(database: Path, reader: Callable[[dict], dict], unreadable: str) -> dict:
    from .energy_migration import database_path, read_database
    try:
        with read_database(database_path(database)) as (_, snapshot):
            return reader(snapshot.state)
    except (sqlite3.Error, OSError, ValueError, TypeError, RecursionError) as error:
        return {"error": getattr(error, "code", unreadable)}


def diagnose_saved_backgrounds(backgrounds: BackgroundService, database: Path) -> dict:
    return _diagnose_saved(database, backgrounds.diagnose, "saved_backgrounds_unreadable")


def diagnose_saved_business_calendar(database: Path) -> dict:
    from .business_calendar import describe
    return _diagnose_saved(database, describe, "saved_business_calendar_unreadable")


def diagnose_saved_login_rewards(login_rewards: LoginRewardService, database: Path) -> dict:
    return _diagnose_saved(database, login_rewards.saved_report, "saved_login_rewards_unreadable")


def diagnose_saved_tower_trades(tower_trades: TowerTradeService, database: Path) -> dict:
    return _diagnose_saved(database, tower_trades.reuse_report, "saved_tower_trades_unreadable")


def service_handlers(bundle: ServerServices) -> dict[int | str, Callable]:
    handlers = {}
    for name in HANDLER_SERVICES:
        service = getattr(bundle, name)
        if service is not None:
            handlers.update(service.handlers())
    return handlers


def assemble_handlers(bundle: ServerServices) -> dict[int | str, Callable]:
    handlers = service_handlers(bundle)
    for operation in NATIVE_STARTUP_LOADS:
        if operation in handlers:
            handlers[operation] = bundle.bootstrap.startup_diagnostics.wrap(operation, handlers[operation])
    return handlers


def serve_gate(bundle: ServerServices, fingerprint: str | None, readiness: Mapping) -> tuple[str, ...]:
    cdn = bundle.native_cdn
    refusals = (
        (bundle.bootstrap.status()["unavailable"], "Required bootstrap resources or starter rules are invalid; run doctor for details"),
        (bundle.player_state.issues, "Required player-state resources are invalid; run doctor for details"),
        (bundle.possessions.issues, "Required item definitions are invalid; run doctor for details"),
        (bundle.presents.issues, "Required present templates are invalid; run doctor for details"),
        (fingerprint != bundle.masters.expected_hash, "Asset directory does not contain the verified original client version"),
        (cdn is None or not cdn.status()["ready"], "The effective native resource view is not ready; run doctor for details"),
        (not readiness["startup_ready"], "Required startup data is unavailable; run doctor for details"),
    )
    return tuple(message for refused, message in refusals if refused)


def content_versions(readiness: Mapping) -> dict[str, str]:
    # Several domains report one version per catalog, so each version is kept under its readiness path.
    found, pending = {}, list(readiness.items())
    while pending:
        path, status = pending.pop()
        if not isinstance(status, Mapping):
            continue
        version = status.get(CONTENT_VERSION)
        if isinstance(version, str) and version:
            found[path] = version
        pending.extend((f"{path}.{name}", child) for name, child in status.items() if isinstance(child, Mapping))
    return found


def service_arguments(config: ResolvedConfig, **inputs) -> SimpleNamespace:
    return SimpleNamespace(**config.arguments(), **inputs)


def build_generation(inputs: GenerationInputs, progress: Progress | None = None) -> Generation:
    from .admin_config_switch import ContentInputs
    report = progress or log.debug
    report("Building the services")
    pinned = inputs.master_snapshot if isinstance(inputs, ContentInputs) else None
    bundle = services(service_arguments(inputs.config, data_dir=inputs.data_dir, asset_dir=inputs.asset_dir,
                                        resource_dir=inputs.resource_dir, host=inputs.host, port=inputs.port,
                                        startup_diagnostics=inputs.diagnostics, master_snapshot=pinned))
    report("Assembling the handlers and readiness")
    handlers, ready = assemble_handlers(bundle), service_readiness(bundle)
    fingerprint = client_fingerprint(inputs.asset_dir)
    cdn = {} if bundle.native_cdn is None else bundle.native_cdn.status()
    identity = GenerationIdentity(code_digest=code_digest(), config_set=inputs.config_digest,
                                  content_versions=content_versions(ready), client_fingerprint=fingerprint,
                                  master_snapshot=bundle.master_snapshot,
                                  base_resource_version=cdn.get("base_version"),
                                  effective_resource_version=cdn.get("version"))
    return Generation(identity, bundle, handlers, ready, serve_gate(bundle, fingerprint, ready),
                      diagnostics=bundle.bootstrap.startup_diagnostics)


def game_source(generation: Generation, runtime: GenerationRuntime | None) -> dict:
    # Only the admin console switches generations, so without it the game serves this one as fixed handlers.
    if runtime is not None:
        return {"runtime": runtime}
    return {"handlers": generation.handlers, "status_provider": generation.live_status,
            "native_cdn": generation.bundle.native_cdn}


def admin_checks(registry: ProtocolRegistry) -> tuple[Preflight, RestartCheck]:
    from .admin_commit import InitializerRestartCheck
    from .admin_preflight import GenerationPreflight
    return GenerationPreflight(registry=registry), InitializerRestartCheck()


def real_admin_checks(registry: ProtocolRegistry) -> tuple[Preflight, RestartCheck]:
    preflight, restart_check = admin_checks(registry)
    if preflight.is_noop or restart_check.is_noop:
        raise ValueError("Admin writes need the real preflight and restart checks")
    return preflight, restart_check


@contextmanager
def loopback_listener(host: str, port: int, *, label: str = "admin console") -> Iterator[socket.socket]:
    listener = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    with listener:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            # Windows otherwise lets another socket that sets SO_REUSEADDR bind the same port.
            listener.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        try:
            listener.bind((host, port))
        except OSError as error:
            raise ValueError(f"The {label} cannot listen on {host} port {port}: {error}") from error
        yield listener


@contextmanager
def background_server(application: FastAPI, listener: socket.socket) -> Iterator[None]:
    import uvicorn
    # The game server configures process logging, and no proxy may rewrite the scheme the origin check reads.
    server = uvicorn.Server(uvicorn.Config(application, access_log=False, log_config=None, proxy_headers=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, name=ADMIN_THREAD, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + ADMIN_STARTUP_SECONDS
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise ValueError("The admin console did not start; see the log above")
            time.sleep(ADMIN_POLL_SECONDS)
        yield
    finally:
        server.should_exit = True
        thread.join(ADMIN_SHUTDOWN_SECONDS)


@dataclass(frozen=True)
class AdminConsole:
    host: str
    port: int
    game_port: int
    data_dir: Path
    preflight: Preflight
    restart_check: RestartCheck

    @classmethod
    def requested(cls, args) -> AdminConsole:
        return cls(args.admin_host or DEFAULT_HOST, args.admin_port, args.port, args.data_dir,
                   *real_admin_checks(ProtocolRegistry.load()))

    @contextmanager
    def running(self, store: Store, runtime: GenerationRuntime, inputs: GenerationInputs,
                listener: socket.socket, *, print_url: bool = True, token: str | None = None) -> Iterator[str]:
        from .admin_app import create_admin_app
        from .admin_command import PlanStore
        from .admin_config_switch import install
        from .admin_security import console_url, new_token, remove_access_file, require_token, write_access_file
        token = new_token() if token is None else require_token(token)
        application = create_admin_app(runtime, store, data_dir=self.data_dir, admin_host=self.host, admin_port=self.port,
                                       game_port=self.game_port, token=token, preflight=self.preflight,
                                       restart_check=self.restart_check, plan_store=PlanStore(),
                                       preflight_domains=self.preflight.domains)
        install(application, inputs=inputs, builder=build_generation)
        with background_server(application, listener):
            write_access_file(self.data_dir, token=token, host=self.host, port=self.port)
            try:
                url = console_url(self.host, self.port, token)
                if print_url:
                    print(f"Admin console: {url}", flush=True)
                yield url
            finally:
                remove_access_file(self.data_dir, token)


def stoppable_server(application: FastAPI, args, stop_event: threading.Event, listener: socket.socket) -> None:
    import uvicorn
    if stop_event.is_set():
        return
    server = uvicorn.Server(uvicorn.Config(application, host=args.host, port=args.port, access_log=False))
    finished = threading.Event()

    def watch_stop():
        while not finished.wait(ADMIN_POLL_SECONDS):
            # Older Uvicorn releases skip shutdown if should_exit is set during startup.
            if stop_event.is_set() and server.started:
                server.should_exit = True
                return

    monitor = threading.Thread(target=watch_stop, name="desktop-server-stop", daemon=True)
    monitor.start()
    try:
        server.run(sockets=[listener])
    finally:
        finished.set()
        monitor.join()


def serve(args, *, stop_event: threading.Event | None = None, print_admin_url: bool = True,
          admin_token: str | None = None) -> None:
    import uvicorn
    from .admin_config_switch import roll_forward
    from .admin_security import require_token
    from .app import DATABASE_NAME, create_app
    from .energy_migration import inspect_database
    from .storage import Store
    if stop_event is not None and stop_event.is_set():
        return
    if admin_token is not None:
        admin_token = require_token(admin_token)
    database = args.data_dir / DATABASE_NAME
    saved_clock = inspect_database(database)
    if stop_event is not None and stop_event.is_set():
        return
    if not saved_clock["ready"]:
        code = saved_clock.get("error") or "energy_migration_database_invalid"
        if saved_clock.get("status") == "stop_service_required":
            raise ProtocolError(code, "The saved database has journal files or open connections; "
                                "stop the other service and inspect the database logs before retrying", 409)
        if saved_clock.get("status") == "migration_required":
            raise ProtocolError(code, "The saved energy clock requires its explicit source timezone; "
                                "inspect an energy-clock-plan before applying a migration", 503)
        raise ProtocolError(code, f"The saved database did not pass startup validation ({code}); "
                            "inspect the saved data before starting the service", 503)
    with ExitStack() as stack:
        game_listener = None if stop_event is None else stack.enter_context(
            loopback_listener(args.host, args.port, label="game server"))
        console = None if args.admin_port is None else AdminConsole.requested(args)
        listener = None if console is None else stack.enter_context(loopback_listener(console.host, console.port))
        if saved_clock["exists"]:
            roll_forward(Store(database), args.data_dir)
        inputs = GenerationInputs(resolve(args), args.data_dir, args.asset_dir, args.resource_dir, args.host, args.port,
                                  StartupDiagnostics())
        generation = build_generation(inputs)
        if stop_event is not None and stop_event.is_set():
            return
        if generation.gate_issues:
            raise ValueError(generation.gate_issues[0])
        warn_auto_request_gaps(generation.readiness.get("auto_requests"))
        bundle = generation.bundle
        assets = bundle.native_cdn.assets
        if args.asset_manifest:
            additional = AssetService(read_json_object(args.asset_manifest, limit=8 * 1024 * 1024))
            assets = AdditionalAssetService(bundle.native_cdn.assets, additional)
        runtime = None if console is None else GenerationRuntime(generation)
        application = create_app(args.data_dir, assets=assets, initial_state=bundle.initialize(bundle.bootstrap.initial_state()),
                                 state_initializer=bundle.initialize, **game_source(generation, runtime))
        if stop_event is not None and stop_event.is_set():
            return
        if console is not None:
            options = {} if print_admin_url else {"print_url": False}
            if admin_token is not None:
                options["token"] = admin_token
            stack.enter_context(console.running(application.state.store, runtime, inputs, listener, **options))
        if stop_event is None:
            uvicorn.run(application, host=args.host, port=args.port, access_log=False)
        else:
            stoppable_server(application, args, stop_event, game_listener)


def recorded_console(data_dir: Path) -> dict | None:
    from .admin_security import MAX_ACCESS_BYTES, access_path
    try:
        path = access_path(data_dir)
        if not path.is_file():
            return None
        recorded = read_json_object(path, limit=MAX_ACCESS_BYTES, root=data_dir)
    except (OSError, ValueError):
        return {"error": "admin_access_unreadable"}
    # The access file also holds the bearer token, which a report must never repeat.
    return {name: recorded.get(name) for name in RECORDED_CONSOLE_FIELDS}


def admin_status(data_dir: Path, registry: ProtocolRegistry) -> dict:
    from .admin_app import INDEX_FILE, UI_DIRECTORY, ui_pages
    preflight, restart_check = admin_checks(registry)
    return {"ui_installed": (UI_DIRECTORY / INDEX_FILE).is_file(),
            "ui_pages": [page["name"] for page in ui_pages(UI_DIRECTORY)],
            "preflight_noop": preflight.is_noop, "restart_check_noop": restart_check.is_noop,
            "console": recorded_console(data_dir)}


def doctor(args) -> dict:
    from .app import DATABASE_NAME
    from .energy_migration import inspect_database
    config, config_error = None, None
    try:
        config = resolve(args)
    except ConfigError as error:
        # serve refuses this configuration, so readiness is reported for the command-line files alone.
        config_error = {"code": error.code, "message": str(error)}
    bundle = services(args if config is None else service_arguments(config, data_dir=args.data_dir, asset_dir=args.asset_dir,
                                                                    resource_dir=args.resource_dir))
    masters = bundle.masters
    registry = ProtocolRegistry.load()
    source_hash = client_fingerprint(args.asset_dir)
    ready = service_readiness(bundle)
    ready["startup_ready"] = ready["startup_ready"] and source_hash == masters.expected_hash and config is not None
    database = args.data_dir / DATABASE_NAME
    saved_clock = inspect_database(database)
    ready["saved_energy_clock"] = saved_clock
    ready["startup_ready"] = ready["startup_ready"] and saved_clock["ready"]
    if saved_clock.get("status") == "ready":
        if bundle.backgrounds is not None:
            ready["saved_backgrounds"] = diagnose_saved_backgrounds(bundle.backgrounds, database)
        # Unmarked saves keep their host-calendar dates, so the marker is reported without gating startup.
        ready["saved_business_calendar"] = diagnose_saved_business_calendar(database)
        # These saves still load and only their own requests refuse, so the repairs are reported without gating startup.
        if bundle.login_rewards is not None:
            ready["saved_login_rewards"] = diagnose_saved_login_rewards(bundle.login_rewards, database)
        if bundle.tower_trades is not None:
            ready["saved_tower_trades"] = diagnose_saved_tower_trades(bundle.tower_trades, database)
    return {"service": "aigis-ps", "version": __version__, "data_dir": str(args.data_dir.resolve()),
            "asset_dir": str(args.asset_dir.resolve()), "source_sha256": source_hash,
            "source_supported": source_hash == masters.expected_hash,
            "native_cdn": bundle.native_cdn.status() if bundle.native_cdn else None,
            "registered_operations": len(registry.by_registration),
            "config": {"error": config_error} if config is None else config.describe(),
            "admin": admin_status(args.data_dir, registry), "readiness": ready}


def offline_generation(args) -> tuple[LeasedGeneration, Callable[[dict], None]]:
    from .admin_app import LeasedGeneration
    from .admin_config_switch import roll_forward
    from .admin_offline import offline_database, read_current, require_offline
    from .app import DATABASE_NAME
    from .storage import Store
    preflight, _ = real_admin_checks(ProtocolRegistry.load())
    database = offline_database(args.data_dir / DATABASE_NAME)
    require_offline(database, checkpoint=args.checkpoint)
    # Store would seed a profile into a file that is not a readable save, so the save is checked before any write.
    read_current(database)
    roll_forward(Store(database), args.data_dir)
    inputs = GenerationInputs(resolve(args), args.data_dir, args.asset_dir, args.resource_dir, DEFAULT_HOST, DEFAULT_PORT,
                              StartupDiagnostics())
    generation = LeasedGeneration(build_generation(inputs))
    domains = frozenset(preflight.domains)

    def encodes(state: dict) -> None:
        # Offline plans rewrite the stopped server's save, so every response the client reads back is encoded.
        preflight.check({}, state, domains, generation)

    return generation, encodes


def offline_plan_output(path: Path, subject: str, *sources: Path) -> Path:
    if path.suffix.lower() != ".json" or path.resolve() in {source.resolve() for source in sources}:
        raise ValueError(f"{subject} plans require a separate JSON output file")
    return path


def saved_offline_plan(path: Path, plan: dict) -> dict:
    from .storage import serialize
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(serialize(plan).encode("utf-8") + b"\n")
    return {"plan_path": str(path.resolve()), "plan": plan}


def read_offline_plan(args, subject: str) -> dict:
    from .admin_diff import DEFAULT_DIFF_LIMITS
    from .app import DATABASE_NAME
    plan = read_json_object(args.plan, limit=DEFAULT_DIFF_LIMITS.max_bytes + OFFLINE_PLAN_MARGIN_BYTES)
    if plan.get("database") != str(checked_path(args.data_dir / DATABASE_NAME)):
        raise ValueError(f"The {subject} plan names the database of another data directory")
    return plan


def plan_offline_restore(args) -> dict:
    from .admin_offline import plan_restore
    from .app import DATABASE_NAME
    database = args.data_dir / DATABASE_NAME
    path = offline_plan_output(args.output, "Restore", database, args.backup)
    generation, encodes = offline_generation(args)
    plan = plan_restore(database, args.backup, initializer=generation.bundle.initialize, preflight=encodes,
                        checkpoint=args.checkpoint)
    return saved_offline_plan(path, plan)


def apply_offline_restore(args) -> dict:
    from .admin_app import BACKUP_DIRECTORY
    from .admin_offline import apply_restore
    plan = read_offline_plan(args, "restore")
    generation, encodes = offline_generation(args)
    return apply_restore(plan, initializer=generation.bundle.initialize, preflight=encodes,
                         backup_directory=args.backup_directory or args.data_dir / BACKUP_DIRECTORY,
                         confirm=args.confirm, checkpoint=args.checkpoint)


def next_start_cooking_border(generation: LeasedGeneration) -> int:
    login_rewards = generation.bundle.login_rewards
    if login_rewards is None:
        raise ProtocolError("login_reward_service_unavailable", "Login reward resources are unavailable, so the cooking "
                            "border of the next start is unknown; run doctor for details", 503)
    return login_rewards.cooking_border


def plan_offline_cooking(args) -> dict:
    from .admin_offline_cooking import plan_cooking_migration
    from .app import DATABASE_NAME
    database = args.data_dir / DATABASE_NAME
    path = offline_plan_output(args.output, "Cooking migration", database)
    generation, encodes = offline_generation(args)
    plan = plan_cooking_migration(database, border=next_start_cooking_border(generation), now=generation.now(),
                                  initializer=generation.bundle.initialize, preflight=encodes, checkpoint=args.checkpoint)
    return saved_offline_plan(path, plan)


def apply_offline_cooking(args) -> dict:
    from .admin_app import BACKUP_DIRECTORY
    from .admin_offline_cooking import apply_cooking_migration
    plan = read_offline_plan(args, "cooking migration")
    generation, encodes = offline_generation(args)
    return apply_cooking_migration(plan, border=next_start_cooking_border(generation),
                                   initializer=generation.bundle.initialize, preflight=encodes,
                                   backup_directory=args.backup_directory or args.data_dir / BACKUP_DIRECTORY,
                                   checkpoint=args.checkpoint)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aigis-local")
    subcommands = parser.add_subparsers(dest="command", required=True)
    server = subcommands.add_parser("serve", help="Run the loopback protocol server")
    server.add_argument("--host", choices=LOOPBACK_HOSTS, default=DEFAULT_HOST)
    server.add_argument("--port", type=int, default=DEFAULT_PORT)
    server.add_argument("--asset-manifest", type=Path, help="Additional assets; indexed resources always use the effective snapshot")
    server.add_argument("--admin-port", type=int, help="Also serve the local admin console on this loopback port")
    server.add_argument("--admin-host", choices=LOOPBACK_HOSTS, help=f"Admin console address; {DEFAULT_HOST} by default")
    checker = subcommands.add_parser("doctor", help="Report protocol, resource and startup readiness")
    clock_plan = subcommands.add_parser("energy-clock-plan", help="Preview a source-declared energy clock migration")
    clock_plan.add_argument("--database", type=Path, default=DEFAULT_DATA / "aigis.sqlite3")
    clock_plan.add_argument("--source-offset-minutes", type=int, required=True)
    clock_plan.add_argument("--output", type=Path, required=True)
    clock_apply = subcommands.add_parser("energy-clock-apply", help="Apply an exact energy clock plan with a backup")
    clock_apply.add_argument("--plan", type=Path, required=True)
    clock_apply.add_argument("--backup-directory", type=Path, default=DEFAULT_DATA / "backups")
    clock_apply.add_argument("--asset-dir", type=Path, default=DEFAULT_ASSETS)
    restore_plan = subcommands.add_parser("admin-restore-plan", help="Preview an exact offline restore of a data directory backup")
    restore_plan.add_argument("--backup", type=Path, required=True)
    restore_apply = subcommands.add_parser("admin-restore-apply", help="Apply an exact offline restore plan after an automatic backup")
    restore_apply.add_argument("--confirm", help="Confirmation phrase for restoring another account's backup")
    cooking_plan = subcommands.add_parser("login-cooking-migrate-plan",
                                          help="Preview an exact offline login cooking migration to the resources' border")
    cooking_apply = subcommands.add_parser("login-cooking-migrate-apply",
                                           help="Apply an exact offline login cooking migration plan after an automatic backup")
    offline_plans, offline_applies = (restore_plan, cooking_plan), (restore_apply, cooking_apply)
    for command in offline_plans:
        command.add_argument("--output", type=Path, required=True)
    for command in offline_applies:
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--backup-directory", type=Path, help="Automatic backup directory; the data directory's backups by default")
    for command in (*offline_plans, *offline_applies):
        command.add_argument("--checkpoint", action="store_true", help="Clear journal files left beside the stopped server's database")
    for command in (server, checker, *offline_plans, *offline_applies):
        command.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
        command.add_argument("--asset-dir", type=Path, default=DEFAULT_ASSETS)
        command.add_argument("--rules", type=Path, help="Local starter rules for newly created saves")
        command.add_argument("--quest-rules", type=Path, help="Explicit local main-quest rules, frozen at battle start")
        command.add_argument("--training-rules", type=Path, help="Local synthesis overflow policy")
        command.add_argument("--silver-house-rules", type=Path, help="Local silver warehouse compensation policy")
        command.add_argument("--evolution-rules", type=Path, help="Local awakening experience policy")
        command.add_argument("--recovery-rules", type=Path, help="Local crystal recovery capacity policy")
        command.add_argument("--trade-rules", type=Path, help="Local shop reset policies")
        command.add_argument("--ticket-rules", type=Path, help="Explicitly enabled complete summon ticket pools")
        command.add_argument("--item-ticket-rules", type=Path, help="Enabled complete pools for item summon tickets")
        command.add_argument("--login-rules", type=Path, help="Explicit login reward legacy spending baseline")
        command.add_argument("--event-pass-rules", type=Path, help="Local automatic event pass settlement rules")
        command.add_argument("--event-pass-revival", type=Path, help="Local event pass revival and daily mission EP bindings")
        command.add_argument("--event-login-rules", type=Path, help="Public event login selection and fixed revival windows")
        command.add_argument("--comeback-login-rules", type=Path, help="Local comeback eligibility and absence threshold")
        command.add_argument("--purchased-login-rules", type=Path, help="Local free daily-login package selection and limits")
        command.add_argument("--daily-gift-rules", type=Path, help="Local daily gift selection and inclusive revival windows")
        command.add_argument("--vote-rules", type=Path, help="Local vote catalog, candidate resolutions and revival schedule")
        command.add_argument("--vote-cooking-rules", type=Path, help="Local vote cooking eligibility and quantity rules")
        command.add_argument("--entrust-rules", type=Path, help="Local entrust experience campaign rule")
        command.add_argument("--mission-windows", type=Path, help="Local mission batch windows patched into served resources")
        command.add_argument("--command-text-overrides", type=Path,
                             help="Local command text cells patched into served resources; gates only close further")
        command.add_argument("--tower-rules", type=Path, help="Local tower day boundaries, first access and high scores")
        command.add_argument("--subjugation-rules", type=Path, help="Local subjugation kill and boost scoring policies")
        command.add_argument("--serial-code-rules", type=Path, help="Local serial code table; without it every code answers as invalid")
        command.add_argument("--letstd-rewards", type=Path, help="Local simulated LetsTD reward groups; all stay off without it")
        command.add_argument("--resource-dir", type=Path, default=DEFAULT_RESOURCES)
    inventory = subcommands.add_parser("inventory", help="Index local resources without modifying them")
    inventory.add_argument("--source", type=Path, default=DEFAULT_ASSETS)
    inventory.add_argument("--output", type=Path, required=True)
    export = subcommands.add_parser("export", help="Export one local resource and its nested tables")
    export.add_argument("resource")
    export.add_argument("--source", type=Path, default=DEFAULT_ASSETS)
    export.add_argument("--output", type=Path, required=True)
    importer = subcommands.add_parser("import-masters", help="Validate a two-table capture and activate it unless staged")
    importer.add_argument("capture", type=Path)
    importer.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    importer.add_argument("--stage", action="store_true",
                          help="Only store the snapshot; the admin console's content switch activates it")
    resource_index = subcommands.add_parser("resources-index", help="Import a captured current resource version and fetch its public index")
    resource_index.add_argument("context", type=Path)
    resource_plan = subcommands.add_parser("resources-plan", help="Refresh the resource acquisition plan")
    resource_collect = subcommands.add_parser("resources-collect", help="Collect indexed resources into an independent cache")
    resource_collect.add_argument("--scope", choices=("startup", "story", "maps", "all"), default="startup")
    resource_collect.add_argument("--workers", type=int, default=4)
    resource_collect.add_argument("--max-gib", type=float)
    for command in (resource_index, resource_plan, resource_collect):
        command.add_argument("--resource-dir", type=Path, default=DEFAULT_RESOURCES)
        command.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
        command.add_argument("--asset-dir", type=Path, default=DEFAULT_ASSETS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "energy-clock-plan":
            from .energy_migration import plan_migration
            path = offline_plan_output(args.output, "Migration", args.database)
            plan = plan_migration(args.database, args.source_offset_minutes)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            output({"plan_path": str(path.resolve()), "plan": plan})
        elif args.command == "energy-clock-apply":
            from .alxml import Container, dumps
            from .bootstrap import BootstrapService
            from .energy_migration import apply_migration
            from .protocol import encode_response
            from .quest_economy import game_param_response
            plan = read_json_object(args.plan, limit=ENERGY_PLAN_BYTES)
            bootstrap = BootstrapService(args.asset_dir)
            registry = ProtocolRegistry.load()

            def preflight(state):
                row = bootstrap.player_row(state)
                encode_response(registry.by_registration[34], dumps(Container(children=bootstrap.columns("game_param", [row]))))
                encode_response(registry.by_registration[85], dumps(Container(children=(game_param_response(state),))))

            output(apply_migration(plan, preflight=preflight, backup_directory=args.backup_directory))
        elif args.command.startswith("resources-"):
            masters = MasterDataStore.for_data_dir(args.data_dir)
            library = ResourceLibrary(args.resource_dir, client_hash=masters.expected_hash)
            progress = lambda event: print(json.dumps(event, ensure_ascii=False), flush=True)
            if args.command == "resources-index":
                output(library.import_context(args.context, progress=progress))
            elif args.command == "resources-plan":
                report = library.plan(args.asset_dir, masters)
                output({"path": str(library.root / "collection-plan.json"), "summary": report["summary"]})
            else:
                budget = None if args.max_gib is None else int(args.max_gib * 1024 ** 3)
                result = library.collect(args.asset_dir, masters, scope=args.scope, workers=args.workers,
                                         max_bytes=budget, progress=progress)
                output(result)
                if result["failed"] or (budget is None and result["status"] != "complete"):
                    return 2
        elif args.command == "inventory":
            manifest = inventory_assets(args.source)
            path = write_inventory(manifest, args.output)
            output({"manifest": str(path), "summary": manifest["summary"]})
        elif args.command == "export":
            report = export_asset(args.source, args.resource, args.output)
            output({key: report.get(key) for key in ("report_path", "complete", "diagnostics", "tables")})
        elif args.command == "import-masters":
            store = MasterDataStore.for_data_dir(args.data_dir)
            manifest = store.import_capture(args.capture, activate=not args.stage)
            output({"snapshot": manifest["snapshot"], "tables": manifest["tables"], "activated": not args.stage})
        elif args.command == "doctor":
            output(doctor(args))
        elif args.command == "admin-restore-plan":
            output(plan_offline_restore(args))
        elif args.command == "admin-restore-apply":
            output(apply_offline_restore(args))
        elif args.command == "login-cooking-migrate-plan":
            output(plan_offline_cooking(args))
        elif args.command == "login-cooking-migrate-apply":
            output(apply_offline_cooking(args))
        elif args.command == "serve":
            if not 1 <= args.port <= MAX_PORT:
                parser.error("Port must be between 1 and 65535")
            if args.admin_port is None and args.admin_host is not None:
                parser.error("--admin-host requires --admin-port")
            if args.admin_port is not None and (not 1 <= args.admin_port <= MAX_PORT or args.admin_port == args.port):
                parser.error("The admin console needs a port of its own between 1 and 65535")
            serve(args)
        return 0
    except ProtocolError as error:
        output({"error": type(error).__name__, "code": error.code, "status": error.status, "message": str(error)})
        return 1
    except (ValueError, OSError, RecursionError) as error:
        output({"error": type(error).__name__, "message": str(error)})
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
