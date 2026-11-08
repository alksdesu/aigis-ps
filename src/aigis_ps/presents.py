from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import asdict, replace
from enum import StrEnum
import base64
import hashlib
from itertools import combinations
import json
import logging
from pathlib import Path
import re
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import Column, Container, Limits, dumps, loads
from .assets import resolve_asset
from .bootstrap import BootstrapService
from .business_calendar import civil_day
from .crystal_wallet import (CRYSTAL_LIMITS, claim_crystal_space, claim_crystals_fit, credit_free_crystals,
                             free_crystal_response, paid_crystal_response, wallet_totals)
from .errors import ProtocolError
from .emblem_catalog import EmblemCatalog
from .local_io import read_bounded
from .native_numbers import INT32_MAX, int32
from .player_state import PlayerStateService, UNIT_BOOLEANS, UNIT_FIELDS, UNIT_XML_LIMITS
from .possessions import MAX_ITEM_NAME_BYTES, MAX_POSSESSION_ITEMS, PossessionService
from .present_items import LEGACY_ITEMS, PresentItemCatalog
from .present_recovery import RESOURCE_LIMIT as RECOVERY_RESOURCE_LIMIT, PresentRecoveryCatalog, PresentRecoveryDefinition
from .quest_economy import game_param_response
from .protocol import RESPONSE_TOO_LARGE, ProtocolRegistry, RequestEnvelope, encode_response
from .request_fields import fields, invalid, registered, values
from .storage import Store
from .wallet_money import GOLD_LIMIT, MoneyRefusal, MoneyRefusals, credit_space, money_after_credit


MAX_PRESENTS = 500
MAX_DESCRIPTION_BYTES = 4096
MAX_RETRIES = 8
MAX_RETRY_BYTES = 1024 * 1024
RESOURCE_LIMIT = 8 * 1024 * 1024
SPLIT_UNNEEDED = "present_split_unneeded"
PRESENT_CAPACITY, PRESENT_IDENTITY_EXHAUSTED = "present_capacity", "present_identity_exhausted"
# A full mailbox or native list response raises these, so callers keep the reward pending for a later delivery.
PRESENT_CAPACITY_ERRORS = frozenset((PRESENT_CAPACITY, PRESENT_IDENTITY_EXHAUSTED, RESPONSE_TOO_LARGE))
PRESENT_RESPONSE_CAPACITY = "present_response_capacity"
CLAIM_GOLD_REFUSALS = MoneyRefusals(capacity=MoneyRefusal("present_gold_capacity", "Claim would exceed the native gold capacity"))
ITEM_TYPES = frozenset((*LEGACY_ITEMS, 22))
TICKET_TYPES = frozenset((16, 20))
PRESENT_LIMITS = Limits(max_values=MAX_PRESENTS * 8 + 32, max_elements=MAX_PRESENTS * 8 + 128)
CLAIM_LIMITS = replace(UNIT_XML_LIMITS, max_values=UNIT_XML_LIMITS.max_values + MAX_PRESENTS * 4 + 64)
TEMPLATE_CARDS = {2: 56, 3: 57, 4: 58, 5: 59}
TEMPLATE_INITIAL = {"InitExp": 0, "InitSkillID": -1, "InitSkillLevel": 1, "CostMod": 0,
                    "InitLove": 0, "EvolutionCount": 0, "ClassChangeEnable": False, "ClassChangeStatusMod": 0}
PRESENT_FIELDS = (("PresentID", "PresentID", "I"), ("Type", "A1", "I"),
                  ("Content", "A2", "I"), ("Description", "A3", "S"))
ITEM_CATALOG = "item_catalog"
GIFT_BOX_DEPENDENCIES = (ITEM_CATALOG, "recovery_catalog", "tickets", "item_tickets", "selection_tickets")
log = logging.getLogger("aigis_ps")


class PendingAction(StrEnum):
    REDELIVER = "redeliver"
    VOID = "void"


def _integer(value, *, minimum=0):
    return int32(value) and value >= minimum


def _description(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        raise ValueError("Present descriptions must be bounded strings")
    dumps(Container(children=(Column("A3", "S", (value,)),)))


def _identifiers(present_ids, *, allow_empty=False):
    if (not isinstance(present_ids, (list, tuple)) or len(present_ids) > MAX_PRESENTS
            or (not present_ids and not allow_empty)
            or any(not _integer(value, minimum=1) for value in present_ids)
            or len(set(present_ids)) != len(present_ids)):
        raise ProtocolError("present_selection_invalid", "Present IDs must be a bounded distinct list of positive int32 values")
    return tuple(present_ids)


class PresentService:
    def __init__(self, bootstrap: BootstrapService, players: PlayerStateService,
                 *, asset_reader: Callable[[str], bytes] | None = None,
                 possessions: PossessionService | None = None, recovery=None, emblems=None,
                 vote_cooking_catalog=None):
        self.bootstrap = bootstrap
        self.players = players
        self.missions = None
        self.tickets = None
        self.item_tickets = None
        self.selection_tickets = None
        self.possessions = possessions
        self.item_catalog = None
        self.item_catalog_error = None
        self.emblem_catalog = emblems
        self.emblem_error = None
        self.recovery = recovery
        self.vote_cooking_catalog = vote_cooking_catalog
        self.recovery_catalog = None
        self.recovery_catalog_error = None
        if recovery is not None:
            try:
                root = Path(bootstrap.asset_root)
                reader = asset_reader or (lambda name: read_bounded(resolve_asset(root, name), RECOVERY_RESOURCE_LIMIT, root=root))
                self.recovery_catalog = PresentRecoveryCatalog(reader)
            except (OSError, ValueError, ProtocolError) as error:
                self.recovery_catalog_error = error.code if isinstance(error, ProtocolError) else str(error)
        if possessions is not None:
            if self.emblem_catalog is None:
                try:
                    root = Path(bootstrap.asset_root)
                    reader = asset_reader or (lambda name: read_bounded(resolve_asset(root, name), RESOURCE_LIMIT, root=root))
                    self.emblem_catalog = EmblemCatalog(possessions, reader)
                except (OSError, ValueError, ProtocolError) as error:
                    self.emblem_error = error.code if isinstance(error, ProtocolError) else str(error)
            try:
                root = Path(bootstrap.asset_root)
                raw = (asset_reader("ItemConfigTable.atb") if asset_reader else
                       read_bounded(resolve_asset(root, "ItemConfigTable.atb"), RESOURCE_LIMIT, root=root))
                self.item_catalog = PresentItemCatalog(possessions, raw, emblems=self.emblem_catalog)
            except (OSError, ValueError, ProtocolError) as error:
                self.item_catalog_error = error.code if isinstance(error, ProtocolError) else str(error)
        self.issues = {}
        self._templates = {}
        self._initial_templates = {}
        self._normalized_skill_templates = ()
        self._initial_template_lookup_unavailable = False
        self.asset_sha256 = None
        registry = ProtocolRegistry.load()
        self._list_profile = registry.by_registration[42]
        self._unit_profile = registry.by_registration[41]
        self._game_param_profile = registry.by_registration[34]
        try:
            root = Path(bootstrap.asset_root)
            raw = (asset_reader("PresentUnitData.atb") if asset_reader else
                   read_bounded(resolve_asset(root, "PresentUnitData.atb"), RESOURCE_LIMIT, root=root))
            if not isinstance(raw, bytes) or len(raw) > RESOURCE_LIMIT:
                raise ValueError("Present templates require bounded resource bytes")
            limits = ParseLimits(max_input_bytes=RESOURCE_LIMIT, max_decompressed_bytes=RESOURCE_LIMIT)
            data = decompress_all4(raw, limits) if raw.startswith(b"ALL4") else raw
            table = parse_table(data, limits)
            expected = {name: 5 if name == "ClassChangeEnable" else 1
                        for name in ("PresentID", "CardID", *TEMPLATE_INITIAL)}
            if {field["name"]: field["type_code"] for field in table["fields"]} != expected or len(table["fields"]) != len(expected):
                raise ValueError("Present unit template fields differ from the verified contract")
            seen, selected, additional = set(), {}, []
            for row in table["rows"]:
                identifier = row["PresentID"]
                if not _integer(identifier) or identifier in seen:
                    raise ValueError("Present template IDs must be nonnegative and unique")
                seen.add(identifier)
                if identifier not in TEMPLATE_CARDS:
                    if (identifier > 0 and _integer(row["CardID"], minimum=2)
                            and type(row["InitSkillLevel"]) is int and row["InitSkillLevel"] in (0, 1)
                            and all(type(row[name]) is type(value) and row[name] == value
                                    for name, value in TEMPLATE_INITIAL.items() if name != "InitSkillLevel")):
                        additional.append((identifier, row["CardID"], row["InitSkillLevel"]))
                    continue
                if not _integer(row["CardID"], minimum=2) or row["CardID"] != TEMPLATE_CARDS[identifier] or any(
                        type(row[name]) is not type(value) or row[name] != value for name, value in TEMPLATE_INITIAL.items()):
                    raise ValueError("Supported present templates have unexpected initial values")
                selected[identifier] = row["CardID"]
            if set(selected) != set(TEMPLATE_CARDS):
                raise ValueError("Supported present templates are missing")
            normalized = []
            if additional:
                try:
                    known_cards = self.players.cards()
                    if not isinstance(known_cards, Mapping):
                        raise ValueError("Verified card lookup is unavailable")
                except (AttributeError, OSError, ValueError, TypeError):
                    self._initial_template_lookup_unavailable = True
                    known_cards = {}
                for identifier, card_id, skill_level in additional:
                    if card_id in known_cards:
                        selected[identifier] = card_id
                        if skill_level == 0:
                            normalized.append(identifier)
            self._templates = selected
            for identifier, card_id in sorted(selected.items()):
                self._initial_templates.setdefault(card_id, identifier)
            self._normalized_skill_templates = tuple(sorted(normalized))
            self.asset_sha256 = hashlib.sha256(data).hexdigest()
        except (OSError, ValueError, KeyError) as error:
            self.issues["present_templates"] = str(error)

    def is_ready(self) -> bool:
        return not self.issues

    def status(self):
        return {"ready": self.is_ready(), "unavailable": dict(self.issues), "implemented_operations": [1, 2, 117],
                "supported_templates": sorted(self._templates), "asset_sha256": self.asset_sha256,
                "initial_unit_policy": "Initial-state templates only; native unit initialization normalizes InitSkillLevel 0 to SkillLevel 1",
                "normalized_skill_templates": list(self._normalized_skill_templates),
                "initial_template_lookup_unavailable": self._initial_template_lookup_unavailable,
                "expiration_policy": "Presents remain until claimed", "mail_capacity": MAX_PRESENTS,
                "wallet_limits": dict(CRYSTAL_LIMITS),
                "gold_limit": GOLD_LIMIT,
                "supported_types": sorted({0, 1, 2, *(self.item_catalog.supported_types if self.item_catalog else ()),
                                           *((8, 22) if self.recovery_catalog else ()),
                                           *((21,) if self.recovery_catalog and self.vote_cooking_catalog else ()),
                                           *((16,) if self.tickets is not None else ()),
                                           *((20,) if self.selection_tickets is not None else ())}),
                "summon_tickets": self.tickets.status() if self.tickets is not None else {"ready": False},
                "item_summon_tickets": self.item_tickets.status() if self.item_tickets is not None else {"ready": False},
                "selection_tickets": self.selection_tickets.status() if self.selection_tickets is not None else {"ready": False},
                "items": self.item_catalog.status() if self.item_catalog else {"ready": False, "error": self.item_catalog_error},
                "emblems": self.emblem_catalog.status() if self.emblem_catalog else {"ready": False, "error": self.emblem_error},
                "recovery": {**self.recovery_catalog.status(),
                             "effective_limit": self.recovery.rules.charisma_limit,
                             "recovery_seconds": self.recovery.recovery_seconds} if self.recovery_catalog else {
                    "ready": False, "error": self.recovery_catalog_error},
                "retry_policy": "Exact request replay at its committed revision; eight persisted responses of at most 1 MiB each",
                "unit_capacity_policy": "Native first-barracks capacity and full operation 41 capacity",
                "batch_policy": ("Request order; keep mail that needs an individual claim or cannot fit "
                                 "without splitting its reward"),
                "empty_claim_policy": "Nothing claimed: SYSTEM and an empty PPU, nothing recorded"}

    def handlers(self):
        handler = self.receive
        return {1: handler, 2: handler, 117: handler}

    def _ready(self):
        if not self.is_ready():
            raise ProtocolError("present_templates_unavailable", "Verified present unit templates are unavailable", 503)

    @property
    def templates(self) -> Mapping[int, int]:
        return MappingProxyType(self._templates)

    def initial_unit_template(self, card_id: int) -> int:
        self._ready()
        if not _integer(card_id, minimum=2) or card_id not in self._initial_templates:
            raise ProtocolError("present_template_unsupported", "No supported initial-state present template exists for this card", 501)
        return self._initial_templates[card_id]

    def gift_box_configurations(self) -> tuple[Mapping[str, str | None], ...]:
        attached = {name: dependency.content_version for name in GIFT_BOX_DEPENDENCIES
                    if (dependency := getattr(self, name)) is not None}
        catalog = self.item_catalog
        item_versions = () if catalog is None else (
            catalog.content_version, *sorted(catalog.replayable_content_versions() - {catalog.content_version}))
        configurations = []
        # Fewest masked dependencies first, so the current wiring is compared before any absent subset.
        for size in range(len(attached) + 1):
            for absent in combinations(attached, size):
                versions = {name: None if name in absent else attached.get(name) for name in GIFT_BOX_DEPENDENCIES}
                variants = (None,) if versions[ITEM_CATALOG] is None else item_versions
                configurations.extend(MappingProxyType({**versions, ITEM_CATALOG: version}) for version in variants)
        return tuple(configurations)

    def _item_definition(self, row):
        if self.item_catalog is None or self.possessions is None:
            raise ProtocolError("present_items_unavailable", "Verified present item definitions are unavailable", 503)
        return self.item_catalog.definition(row["Type"], row.get("ItemID"))

    def _item_ticket_definition(self, row):
        if (row["Type"] == 22 and self.item_tickets is not None
                and self.item_tickets.catalog.supports_item(row.get("ItemID"))):
            self.item_tickets._ready()
            return self.item_tickets.catalog.definition(row["ItemID"])
        return None

    def _recovery_definition(self, row):
        if row["Type"] == 21:
            if self.recovery_catalog is None or self.recovery is None or self.vote_cooking_catalog is None:
                raise ProtocolError("vote_cooking_unavailable", "Verified character cooking presents are unavailable", 503)
            if not self.vote_cooking_catalog.is_ready():
                raise ProtocolError("vote_cooking_unavailable", "Verified character cooking resources are unavailable", 503)
            self.vote_cooking_catalog.definition_from_content(row["Content"])
            return PresentRecoveryDefinition(None)
        if row["Type"] == 8:
            if self.recovery_catalog is None or self.recovery is None:
                raise ProtocolError("present_recovery_unavailable", "Verified recovery presents are unavailable", 503)
            return self.recovery_catalog.definition(8, row["Content"])
        if (row["Type"] == 22 and self.recovery_catalog is not None
                and self.recovery_catalog.supports_item(row.get("ItemID"))):
            return self.recovery_catalog.definition(22, row["Content"], row.get("ItemID"))
        return None

    def _claimed_individually(self, row) -> bool:
        if row["Type"] in TICKET_TYPES or self._recovery_definition(row) is not None:
            return True
        # rows() already verified ticket readiness; checking it per mail would rehash the card master each time.
        if row["Type"] == 22 and self.item_tickets is not None and row["ItemID"] in self.item_tickets.catalog.item_ids:
            return True
        return row["Type"] in ITEM_TYPES and self._item_definition(row).single_only

    def _bundled_ticket(self, row) -> bool:
        return row["Content"] > 1 and (row["Type"] in TICKET_TYPES or self._item_ticket_definition(row) is not None)

    def rows(self, state: dict) -> list[dict]:
        return self._rows(state)

    def _rows(self, state: dict, *, bundled_summons: bool = False) -> list[dict]:
        self._ready()
        try:
            if not isinstance(state, dict) or type(state.get("bootstrap_schema")) is not int or state["bootstrap_schema"] != 1:
                raise ValueError("Missing bootstrap state")
            tables = state.get("tables")
            rows = tables.get("presents") if isinstance(tables, dict) else None
            if not isinstance(rows, list) or len(rows) > MAX_PRESENTS:
                raise ValueError("Missing or oversized present list")
            version = state.get("presents_schema")
            if version is None:
                if rows or "last_present_id" in state:
                    raise ValueError("Unversioned present data cannot be replaced")
                return []
            if type(version) is not int or version != 1 or not _integer(state.get("last_present_id")):
                raise ValueError("Unsupported present schema or identity counter")
            ticket_service = self.item_tickets
            ticket_catalog = ticket_definitions = None
            ticket_checked = False

            def check_ticket_identity():
                if (self.item_tickets is not ticket_service or ticket_service.catalog is not ticket_catalog
                        or ticket_catalog.definitions is not ticket_definitions):
                    raise ProtocolError("item_summon_dependencies_changed", "Item ticket definitions changed during mail validation", 503)

            seen = set()
            for row in rows:
                expected = {name for name, _, _ in PRESENT_FIELDS}
                if isinstance(row, dict) and row.get("Type") == 22:
                    expected.add("ItemID")
                if isinstance(row, dict) and row.get("Type") == 16:
                    expected.add("SummonID")
                if isinstance(row, dict) and row.get("Type") == 20:
                    expected.add("SelectionConfigID")
                if isinstance(row, dict) and "NotificationCategory" in row:
                    expected.add("NotificationCategory")
                    if not _integer(row["NotificationCategory"]):
                        raise ValueError("Present notification categories must be nonnegative int32 values")
                if not isinstance(row, dict) or set(row) != expected:
                    raise ValueError("Present row fields are invalid")
                identifier, kind, content = row["PresentID"], row["Type"], row["Content"]
                if not _integer(identifier, minimum=1) or identifier in seen or identifier > state["last_present_id"]:
                    raise ValueError("Present identity is invalid or reused")
                seen.add(identifier)
                if (type(kind) is not int or (kind not in (0, 1, 2, 8, 16, 20, 21) and kind not in ITEM_TYPES)
                        or not _integer(content, minimum=0 if kind == 8 else 1)):
                    raise ValueError("Present content is invalid or unsupported")
                if kind == 16 and ((content != 1 and not bundled_summons) or not _integer(row["SummonID"], minimum=1)):
                    raise ValueError("Summon tickets require one draw and a valid pool identity")
                if kind == 16:
                    if self.tickets is None:
                        raise ProtocolError("summon_ticket_service_unavailable", "Stored summon tickets require their pool catalog", 503)
                    self.tickets.pools.get(row["SummonID"])
                if kind == 20:
                    if not _integer(row["SelectionConfigID"], minimum=1):
                        raise ValueError("Selection tickets require a valid configuration identity")
                    if self.selection_tickets is None:
                        raise ProtocolError("select_ticket_service_unavailable", "Stored selection tickets require their catalog", 503)
                    self.selection_tickets.catalog.get(row["SelectionConfigID"])
                if kind == 1 and content not in self._templates:
                    raise ValueError("Present unit template is unsupported")
                if (kind in (8, 21) or kind in ITEM_TYPES) and self._recovery_definition(row) is None:
                    ticket = False
                    if kind == 22 and ticket_service is not None:
                        if ticket_catalog is None:
                            ticket_catalog = ticket_service.catalog
                            ticket_definitions = ticket_catalog.definitions
                            check_ticket_identity()
                            ticket_catalog._ready()
                        item_id = row.get("ItemID")
                        ticket = type(item_id) is str and item_id in ticket_definitions
                        if ticket and not ticket_checked:
                            ticket_service._ready()
                            check_ticket_identity()
                            ticket_checked = True
                    if not ticket:
                        self._item_definition(row)
                _description(row["Description"])
            if ticket_catalog is not None:
                check_ticket_identity()
                if ticket_checked:
                    ticket_service._ready()
                else:
                    ticket_catalog._ready()
                check_ticket_identity()
            return deepcopy(rows)
        except (ValueError, TypeError) as error:
            raise ProtocolError("present_state_invalid", "Stored presents are invalid, missing or unsupported", 503) from error

    def response(self, state: dict, present_ids=None) -> Container:
        rows = self.rows(state)
        # The 42 listing fixes the client mail columns (A714E0/901610); later PPT deltas lose any column it lacked.
        listing = present_ids is None
        if not listing:
            selected = _identifiers(present_ids, allow_empty=True)
            indexed = {row["PresentID"]: row for row in rows}
            if set(selected) - indexed.keys():
                raise ProtocolError("present_not_found", "A requested present is not available", 409)
            rows = [indexed[identifier] for identifier in selected]
        columns = tuple(Column(wire, kind, tuple(row[name] for row in rows)) for name, wire, kind in PRESENT_FIELDS)
        if listing or any(row["Type"] in TICKET_TYPES for row in rows):
            columns += (Column("A4", "I", tuple(row.get("SummonID", row.get("SelectionConfigID", 0)) for row in rows)),)
        if listing or any(row["Type"] == 22 for row in rows):
            columns += (Column("A6", "S", tuple(row.get("ItemID", "") for row in rows)),)
        if listing or any("NotificationCategory" in row for row in rows):
            columns += (Column("A9", "I", tuple(row.get("NotificationCategory", 0) for row in rows)),)
        return Container("PPT", columns)

    def set_notification_category(self, state: dict, ids, category: int) -> dict:
        if not _integer(category):
            raise ProtocolError("present_category_invalid", "Notification category must be a nonnegative int32 value")
        selected = set(_identifiers(ids))
        rows = self.rows(state)
        if selected - {row["PresentID"] for row in rows}:
            raise ProtocolError("present_not_found", "A requested present is not available", 409)
        for row in rows:
            if row["PresentID"] in selected and row.get("NotificationCategory", 0) != category:
                row["NotificationCategory"] = category
        updated = deepcopy(state)
        updated["tables"]["presents"] = rows
        encode_response(self._list_profile, dumps(Container(children=(self.response(updated),)), limits=PRESENT_LIMITS))
        return updated

    def bundled_tickets(self, state: dict) -> tuple[int, ...]:
        return tuple(row["PresentID"] for row in self._rows(state, bundled_summons=True) if self._bundled_ticket(row))

    def split_ticket_mail(self, state: dict, present_id: int) -> tuple[dict, list[dict]]:
        (present_id,) = _identifiers((present_id,))
        rows = self._rows(state, bundled_summons=True)
        position = next((index for index, row in enumerate(rows) if row["PresentID"] == present_id), None)
        if position is None:
            raise ProtocolError("present_not_found", "A requested present is not available", 409)
        mail, last = rows[position], state["last_present_id"]
        if not self._bundled_ticket(mail):
            raise ProtocolError(SPLIT_UNNEEDED, "Only ticket mail holding several tickets is split", 409)
        count = mail["Content"]
        if len(rows) - 1 + count > MAX_PRESENTS:
            raise ProtocolError(PRESENT_CAPACITY, "Split tickets exceed the local mailbox capacity", 409)
        if last + count > INT32_MAX:
            raise ProtocolError(PRESENT_IDENTITY_EXHAUSTED, "Present identities exceed int32", 409)
        # Every ticket takes a new identity, so ledgers that recorded the bundled mail read it as claimed.
        pieces = [{**mail, "PresentID": last + offset, "Content": 1} for offset in range(1, count + 1)]
        updated = deepcopy(state)
        updated["last_present_id"] = last + count
        updated["tables"]["presents"] = [*rows[:position], *pieces, *rows[position + 1:]]
        self._rows(updated, bundled_summons=True)
        return updated, deepcopy(pieces)

    def _prepare_reward_rows(self, rewards: list[dict], existing_count: int) -> list[dict]:
        if not isinstance(rewards, list) or len(rewards) > MAX_PRESENTS:
            raise ProtocolError("present_reward_invalid", "Present rewards must be a bounded list")
        additions = []
        for reward in rewards:
            if not isinstance(reward, dict):
                raise ProtocolError("present_reward_invalid", "Present rewards must be objects")
            kind = reward.get("kind")
            if kind == "vote_cooking":
                if set(reward) != {"kind", "card_id", "description"} or not _integer(reward["card_id"], minimum=1):
                    raise ProtocolError("present_reward_invalid", "Character cooking requires a card identity and description")
                try:
                    _description(reward["description"])
                except (ValueError, TypeError) as error:
                    raise ProtocolError("present_reward_invalid", "Present reward description is invalid") from error
                if existing_count + len(additions) >= MAX_PRESENTS:
                    raise ProtocolError(PRESENT_CAPACITY, "Present rewards exceed the local mailbox capacity", 409)
                row = {"Type": 21, "Content": reward["card_id"], "Description": reward["description"]}
                self._recovery_definition(row)
                additions.append(row)
                continue
            if kind == "selection_ticket":
                if (set(reward) != {"kind", "config_id", "count", "description"}
                        or not _integer(reward["config_id"], minimum=1)
                        or not _integer(reward["count"], minimum=1)):
                    raise ProtocolError("present_reward_invalid", "Selection tickets require a configuration and positive count")
                try:
                    _description(reward["description"])
                except (ValueError, TypeError) as error:
                    raise ProtocolError("present_reward_invalid", "Present reward description is invalid") from error
                if existing_count + len(additions) + reward["count"] > MAX_PRESENTS:
                    raise ProtocolError(PRESENT_CAPACITY, "Selection rewards exceed mailbox capacity", 409)
                if self.selection_tickets is None:
                    raise ProtocolError("select_ticket_service_unavailable", "Selection delivery requires its claim service", 503)
                self.selection_tickets.validate_config(reward["config_id"])
                additions.extend({"Type": 20, "Content": 1, "SelectionConfigID": reward["config_id"],
                                  "Description": reward["description"]} for _ in range(reward["count"]))
                continue
            if kind == "summon_ticket":
                if (set(reward) != {"kind", "pool_id", "count", "description"}
                        or not _integer(reward["pool_id"], minimum=1)
                        or not _integer(reward["count"], minimum=1)):
                    raise ProtocolError("present_reward_invalid", "Summon tickets require a pool and positive count")
                try:
                    _description(reward["description"])
                except (ValueError, TypeError) as error:
                    raise ProtocolError("present_reward_invalid", "Present reward description is invalid") from error
                if existing_count + len(additions) + reward["count"] > MAX_PRESENTS:
                    raise ProtocolError(PRESENT_CAPACITY, "Summon ticket rewards exceed mailbox capacity", 409)
                if self.tickets is None:
                    raise ProtocolError("summon_ticket_service_unavailable", "Summon ticket delivery requires its claim service", 503)
                self.tickets.validate_pool(reward["pool_id"])
                additions.extend({"Type": 16, "Content": 1, "SummonID": reward["pool_id"],
                                  "Description": reward["description"]} for _ in range(reward["count"]))
                continue
            if kind == "charisma_recovery":
                if set(reward) not in ({"kind", "description"}, {"kind", "item_id", "description"}):
                    raise ProtocolError("present_reward_invalid", "Recovery presents require only an optional item identity and description")
                try:
                    _description(reward["description"])
                except (ValueError, TypeError) as error:
                    raise ProtocolError("present_reward_invalid", "Present reward description is invalid") from error
                if existing_count + len(additions) >= MAX_PRESENTS:
                    raise ProtocolError(PRESENT_CAPACITY, "Present rewards exceed the local mailbox capacity", 409)
                item_present = "item_id" in reward
                row = {"Type": 22 if item_present else 8, "Content": 1 if item_present else 0,
                       "Description": reward["description"]}
                if item_present:
                    row["ItemID"] = reward["item_id"]
                if self._recovery_definition(row) is None:
                    raise ProtocolError("present_recovery_unsupported", "This item is not a verified recovery present", 501)
                additions.append(row)
                continue
            expected = {"kind", "template_id", "count", "description"} if kind == "unit" else {"kind", "amount", "description"}
            if kind == "item":
                expected.add("item_id")
            elif kind == "legacy_item":
                expected.add("present_type")
            if kind not in ("unit", "free_crystal", "gold", "item", "legacy_item") or set(reward) != expected:
                raise ProtocolError("present_reward_invalid", "Present reward fields are invalid or unsupported")
            try:
                _description(reward["description"])
            except (ValueError, TypeError) as error:
                raise ProtocolError("present_reward_invalid", "Present reward description is invalid") from error
            count = reward["count"] if kind == "unit" else 1
            content = reward["template_id"] if kind == "unit" else reward["amount"]
            if not _integer(count, minimum=1) or not _integer(content, minimum=1):
                raise ProtocolError("present_reward_invalid", "Present quantities and identifiers must be positive int32 values")
            if kind == "unit" and content not in self._templates:
                raise ProtocolError("present_template_unsupported", "Present unit template is not supported")
            if existing_count + len(additions) + count > MAX_PRESENTS:
                raise ProtocolError(PRESENT_CAPACITY, "Present rewards exceed the local mailbox capacity", 409)
            if kind in ("item", "legacy_item"):
                present_type = 22 if kind == "item" else reward["present_type"]
                if type(present_type) is not int or present_type not in ITEM_TYPES or (kind == "legacy_item" and present_type == 22):
                    raise ProtocolError("present_reward_invalid", "Legacy item rewards require a verified fixed present type")
                row = {"Type": present_type, "Content": content, "Description": reward["description"]}
                if kind == "item":
                    row["ItemID"] = reward["item_id"]
                if self._item_ticket_definition(row) is not None:
                    self.item_tickets.validate_item(row["ItemID"])
                    if existing_count + len(additions) + content > MAX_PRESENTS:
                        raise ProtocolError(PRESENT_CAPACITY, "Item ticket rewards exceed mailbox capacity", 409)
                    additions.extend({**row, "Content": 1} for _ in range(content))
                else:
                    self._item_definition(row)
                    additions.append(row)
            else:
                additions.extend({"Type": {"unit": 1, "free_crystal": 0, "gold": 2}[kind], "Content": content,
                                  "Description": reward["description"]} for _ in range(count))
        if any(len(item_id.encode("utf-8")) > MAX_ITEM_NAME_BYTES
               for item_id in {row["ItemID"] for row in additions if "ItemID" in row}):
            raise ProtocolError("present_item_unreplayable", "New present rows need item identities that fit the native cell", 503)
        return additions

    def preview(self, rewards: list[dict], existing_count: int) -> list[dict]:
        self._ready()
        if not _integer(existing_count):
            raise ProtocolError("present_reward_invalid", "Existing mailbox size must be a nonnegative int32 value")
        return self._prepare_reward_rows(rewards, existing_count)

    def enqueue_groups(self, state: dict, reward_groups) -> tuple[dict, list[list[dict]]]:
        rows = self.rows(state)
        if not isinstance(reward_groups, (list, tuple)) or len(reward_groups) > MAX_PRESENTS:
            raise ProtocolError("present_reward_invalid", "Present reward groups must be a bounded sequence")
        prepared, total = [], 0
        for rewards in reward_groups:
            additions = self._prepare_reward_rows(rewards, len(rows) + total)
            prepared.append(additions)
            total += len(additions)
        updated = deepcopy(state)
        if not total:
            return updated, prepared
        last_id = state.get("last_present_id", 0)
        if last_id + total > INT32_MAX:
            raise ProtocolError(PRESENT_IDENTITY_EXHAUSTED, "Present identities exceed int32", 409)
        grouped, offset = [], 0
        for additions in prepared:
            grouped.append([{"PresentID": last_id + offset + index + 1, **row} for index, row in enumerate(additions)])
            offset += len(additions)
        updated["presents_schema"] = 1
        updated["last_present_id"] = last_id + total
        updated["tables"]["presents"] = rows + deepcopy([row for group in grouped for row in group])
        encode_response(self._list_profile, dumps(Container(children=(self.response(updated),)), limits=PRESENT_LIMITS))
        return updated, grouped

    def enqueue(self, state: dict, rewards: list[dict]) -> tuple[dict, list[dict]]:
        updated, grouped = self.enqueue_groups(state, [rewards])
        return updated, grouped[0]

    def claim(self, state: dict, present_ids, *, acquisition_day: int, partial: bool = False, now=None) -> tuple[dict, list[dict], int]:
        if type(partial) is not bool:
            raise ProtocolError("present_selection_invalid", "Partial claim policy must be boolean")
        selected = _identifiers(present_ids, allow_empty=partial)
        if not _integer(acquisition_day, minimum=1):
            raise ProtocolError("present_day_invalid", "Claim acquisition day must be a positive native int32 day")
        rows = self.rows(state)
        indexed = {row["PresentID"]: row for row in rows}
        if set(selected) - indexed.keys():
            raise ProtocolError("present_not_found", "A requested present is unavailable or already claimed", 409)
        if partial:
            kept = [identifier for identifier in selected if self._claimed_individually(indexed[identifier])]
            if kept:
                log.warning("Present claim 117 keeps %s for individual claims and claims the rest", kept)
                selected = tuple(identifier for identifier in selected if identifier not in kept)
        else:
            if any(indexed[identifier]["Type"] in TICKET_TYPES for identifier in selected):
                raise ProtocolError("present_ticket_transaction_required", "Tickets require their individual claim transaction", 409)
            if any(self._item_ticket_definition(indexed[identifier]) is not None for identifier in selected):
                raise ProtocolError("present_ticket_transaction_required", "Item summon tickets require their individual claim transaction", 409)
            if any(self._recovery_definition(indexed[identifier]) is not None for identifier in selected):
                if len(selected) != 1:
                    raise ProtocolError("present_recovery_single_only", "Recovery presents must be claimed individually", 409)
                updated = self.recovery.restore_state(state, self.bootstrap.clock() if now is None else now)
                updated["tables"]["presents"] = [row for row in rows if row["PresentID"] != selected[0]]
                self.rows(updated)
                return updated, [], 0
            if any(self._item_definition(indexed[identifier]).single_only
                   for identifier in selected if indexed[identifier]["Type"] in ITEM_TYPES):
                if len(selected) != 1:
                    raise ProtocolError("present_item_single_only", "These items must be claimed individually", 409)
        if any(indexed[key]["Type"] == 2 for key in selected):
            # Bootstrap validates GameParam first, so corrupt saves report its refusals rather than wallet_money's.
            self.bootstrap.player_row(state)
            gold_space = credit_space(state, cap=GOLD_LIMIT)
        else:
            gold_space = 0
        item_counts = (self.possessions.counts(state)
                       if any(indexed[key]["Type"] in ITEM_TYPES for key in selected) else {})
        if partial:
            slots = self.players.available_unit_slots(state) if any(indexed[key]["Type"] == 1 for key in selected) else 0
            crystal_room = claim_crystal_space(state) if any(indexed[key]["Type"] == 0 for key in selected) else 0
            gold_room = gold_space
            accepted = []
            for identifier in selected:
                row = indexed[identifier]
                if row["Type"] == 1:
                    if not slots:
                        continue
                    slots -= 1
                elif row["Type"] == 0:
                    if row["Content"] > crystal_room:
                        continue
                    crystal_room -= row["Content"]
                elif row["Type"] == 2:
                    if row["Content"] > gold_room:
                        continue
                    gold_room -= row["Content"]
                else:
                    item = self._item_definition(row)
                    total = item_counts.get(item.item_id, 0) + row["Content"]
                    if total > item.stack_limit or (item.item_id not in item_counts and len(item_counts) >= MAX_POSSESSION_ITEMS):
                        continue
                    item_counts[item.item_id] = total
                accepted.append(identifier)
            selected = tuple(accepted)
        grants, crystals, gold, item_grants = [], 0, 0, {}
        for identifier in selected:
            row = indexed[identifier]
            if row["Type"] == 1:
                grants.append({"card_id": self._templates[row["Content"]], "level": 1, "count": 1})
            elif row["Type"] == 0:
                crystals += row["Content"]
            elif row["Type"] == 2:
                gold += row["Content"]
            else:
                item = self._item_definition(row)
                if not partial:
                    total = item_counts.get(item.item_id, 0) + row["Content"]
                    if total > item.stack_limit:
                        raise ProtocolError("present_item_capacity", "Claim would exceed the native item capacity", 409)
                    item_counts[item.item_id] = total
                item_grants[item.item_id] = item_grants.get(item.item_id, 0) + row["Content"]
        if crystals and not claim_crystals_fit(state, crystals):
            raise ProtocolError("present_crystal_capacity", "Claim would exceed the native crystal wallet capacity", 409)
        # Comparing with the space also refuses mail sums beyond int32 as a capacity failure.
        if gold > gold_space:
            raise CLAIM_GOLD_REFUSALS.capacity.error()
        self.players.require_unit_space(state, len(grants))
        updated, units = self.players.credit_units(state, grants)
        if crystals:
            updated = credit_free_crystals(updated, crystals, acquisition_day)
        if gold:
            updated["tables"]["game_param"][0]["Money"] = money_after_credit(updated, gold, cap=GOLD_LIMIT,
                                                                          refusals=CLAIM_GOLD_REFUSALS)
        if item_grants:
            updated = self.possessions.credit(updated, item_grants)
        removed = set(selected)
        updated["tables"]["presents"] = [row for row in rows if row["PresentID"] not in removed]
        if item_grants:
            if self.missions is None and self.item_catalog.skin_item_ids.intersection(item_grants):
                raise ProtocolError("mission_skin_service_unavailable", "Skin acquisition requires the mission service", 503)
            if self.missions is not None:
                updated = self.missions.record_acquisitions(state, updated, self.bootstrap.clock() if now is None else now)
        self.rows(updated)
        return updated, units, crystals

    @staticmethod
    def history(state: dict, revision: int) -> list[dict]:
        version = state.get("present_claim_history_schema")
        entries = state.get("present_claim_history", [])
        try:
            if version is None:
                if entries:
                    raise ValueError("Unversioned claim history")
                if not isinstance(entries, list):
                    raise ValueError("Invalid claim history")
                return []
            if type(version) is not int or version != 1 or not isinstance(entries, list) or len(entries) > MAX_RETRIES:
                raise ValueError("Unsupported claim history")
            seen, previous = set(), 0
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != {"profile", "digest", "revision", "response"}:
                    raise ValueError("Invalid claim receipt")
                if any(not isinstance(entry[name], str) or not re.fullmatch("[0-9a-f]{64}", entry[name]) for name in ("profile", "digest")):
                    raise ValueError("Invalid claim receipt identity")
                key = entry["profile"], entry["digest"]
                number = entry["revision"]
                if key in seen or type(number) is not int or not previous < number <= revision:
                    raise ValueError("Invalid claim receipt revision")
                encoded = entry["response"]
                if not isinstance(encoded, str) or len(encoded) > ((MAX_RETRY_BYTES + 2) // 3) * 4:
                    raise ValueError("Oversized claim receipt")
                response = base64.b64decode(encoded, validate=True)
                if not response or len(response) > MAX_RETRY_BYTES:
                    raise ValueError("Invalid claim response")
                loads(response, limits=CLAIM_LIMITS)
                seen.add(key)
                previous = number
            return deepcopy(entries)
        except (ValueError, TypeError) as error:
            raise ProtocolError("present_history_invalid", "Stored present claim history is invalid or unsupported", 503) from error

    def receive(self, envelope: RequestEnvelope, store: Store) -> bytes:
        request = registered(envelope, {1, 2, 117}, {"RT"}, {"RT"})
        root = fields(request["RT"], {"PT2"}, {"PT2"})
        batch = envelope.profile.registration_keys == (117,)
        table = fields(root["PT2"], {"PresentID", "SelectID", "SelectCardID"}, () if batch else {"PresentID"})
        # Native 6C4020 submits an empty batch list as a PT2 without PresentID.
        selected = (values(table["PresentID"], "I", minimum=0 if batch else 1, maximum=MAX_PRESENTS)
                    if "PresentID" in table else ())
        if any(identifier <= 0 for identifier in selected) or len(set(selected)) != len(selected):
            invalid("Present IDs must be positive and distinct")
        identity = hashlib.sha256(json.dumps(asdict(envelope.profile), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        for _ in range(3):
            profile = self.bootstrap._authenticate(envelope, store)
            self._ready()
            self.players.validate(profile.state)
            if (not batch and self.selection_tickets is not None
                    and self.selection_tickets.matches_request(profile, envelope, selected)):
                return self.selection_tickets.receive(envelope, store, selected)
            if set(table) - {"PresentID"}:
                invalid("Selection fields require a selection ticket")
            if not batch and self.tickets is not None and self.tickets.matches_request(profile, envelope, selected):
                return self.tickets.receive(envelope, store, selected)
            if not batch and self.item_tickets is not None and self.item_tickets.matches_request(profile, envelope, selected):
                return self.item_tickets.receive(envelope, store, selected)
            history = self.history(profile.state, profile.revision)
            cached = next((entry for entry in history if entry["profile"] == identity and entry["digest"] == envelope.digest), None)
            if cached is not None:
                if cached["revision"] != profile.revision:
                    raise ProtocolError("present_replay_stale", "The saved claim response is older than the current state", 409)
                response = base64.b64decode(cached["response"], validate=True)
                self._validate_cached_rewards(profile, response)
                encode_response(envelope.profile, response)
                return response
            now = self.bootstrap.clock()
            day = civil_day(now)
            updated, units, crystals = self.claim(profile.state, selected, acquisition_day=day, partial=batch, now=now)
            children = [self.bootstrap._system(profile)]
            previous_game = profile.state["tables"]["game_param"][0]
            if updated["tables"]["game_param"][0] != previous_game:
                row = self.bootstrap.player_row(updated)
                if {name for name in row if row[name] != previous_game[name]} == {"Money"}:
                    children.append(Container("PGP", (Column("A1", "S", ("A1",)),
                                                      Column("A2", "S", (str(row["Money"]),)))))
                else:
                    children.append(game_param_response(updated))
                encode_response(self._game_param_profile, dumps(Container(children=self.bootstrap.columns("game_param", [row]))))
            if updated["tables"]["possessions"] != profile.state["tables"]["possessions"]:
                old_counts = self.possessions.counts(profile.state)
                changed = [item for item, count in self.possessions.counts(updated).items()
                           if count != old_counts.get(item, 0)]
                children.append(self.possessions.response(updated, changed))
                self._preflight_items(profile, updated)
            if units:
                children.append(Container("PBP", (Column("A1", "S", ("UX",)),
                    Column("A2", "I", (updated["player_state"]["battle"]["UX"],)))))
            children.append(Container("PPU", tuple(Column(alias, "B" if name in UNIT_BOOLEANS else "I",
                tuple(unit[name] for unit in units)) for name, alias in UNIT_FIELDS)))
            if crystals:
                if batch:
                    children.append(free_crystal_response(updated, profile.player_id))
                else:
                    amount = next(row["Num"] for row in updated["tables"]["crystals"] if row["FreeFlag"] and row["Date"] == day)
                    children.append(Container("PFC", (Column("key", "S", ("A2", "Date")),
                                                       Column("value", "I", (amount, day)))))
            response = dumps(Container(children=tuple(children)), limits=CLAIM_LIMITS)
            encode_response(envelope.profile, response)
            if batch and updated == profile.state:
                # Nothing was committed, so a retry can recompute safely instead of replaying a receipt.
                log.warning("Present claim 117 claimed nothing among %s; replying unchanged", list(selected))
                return response
            encode_response(self._unit_profile, self.players.unit_response(updated["player_state"]["units"]))
            listing = dumps(Container(children=(self.bootstrap._system(profile), self.response(updated))), limits=PRESENT_LIMITS)
            encode_response(self._list_profile, listing)
            if len(response) > MAX_RETRY_BYTES:
                raise ProtocolError(PRESENT_RESPONSE_CAPACITY, "Claim response exceeds the bounded retry history", 409)
            updated["present_claim_history_schema"] = 1
            updated["present_claim_history"] = [*history, {"profile": identity, "digest": envelope.digest,
                "revision": profile.revision + 1, "response": base64.b64encode(response).decode("ascii")}][-MAX_RETRIES:]
            self.history(updated, profile.revision + 1)
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while claiming presents", 409)

    def _preflight_items(self, profile, state):
        candidate = replace(profile, state=state)
        bootstrap = self.bootstrap
        registry = ProtocolRegistry.load()
        encode_response(registry.by_registration[129], dumps(Container(children=(
            bootstrap._system(candidate), self.possessions.response(state)))))
        encode_response(registry.by_registration[137], bootstrap._configuration_response(candidate))

    def _validate_cached_rewards(self, profile, response):
        root = loads(response, limits=CLAIM_LIMITS)
        names = {node.name for node in root.children}
        if "PGP" in names:
            groups = [node for node in root.children if node.name == "PGP"]
            if len(groups) != 1 or not isinstance(groups[0], Container):
                raise ProtocolError("present_history_invalid", "Saved reward receipt has an invalid game parameter table", 503)
            row = self.bootstrap._player_row(profile)
            gold = Container("PGP", (Column("A1", "S", ("A1",)), Column("A2", "S", (str(row["Money"]),))))
            if groups[0] == gold:
                pass
            elif groups[0] == game_param_response(profile.state):
                if self.recovery is None or self.recovery_catalog is None:
                    raise ProtocolError("present_recovery_unavailable", "Recovery receipt dependencies are unavailable", 503)
                self.recovery._ready()
                if not self.recovery_catalog.is_ready():
                    raise ProtocolError("present_recovery_unavailable", "Recovery receipt resources are unavailable", 503)
            else:
                raise ProtocolError("present_history_invalid", "Saved game parameter receipt disagrees with the current state", 503)
            encode_response(self._game_param_profile, dumps(Container(children=self.bootstrap.columns("game_param", [row]))))
        if names & {"PFC", "PPC"}:
            wallet_totals(profile.state)
            rows = self.bootstrap._rows(profile, "crystals", single=False)
            for name, canonical in (("PFC", free_crystal_response(profile.state, profile.player_id)),
                                    ("PPC", paid_crystal_response(profile.state, profile.player_id))):
                groups = [node for node in root.children if node.name == name]
                if not groups:
                    continue
                allowed = [canonical]
                if name == "PFC":
                    allowed.extend(Container("PFC", (Column("key", "S", ("A2", "Date")),
                        Column("value", "I", (row["Num"], row["Date"])))) for row in rows if row["FreeFlag"])
                if len(groups) != 1 or groups[0] not in allowed:
                    raise ProtocolError("present_history_invalid", "Saved crystal receipt disagrees with the current wallet", 503)
            registry = ProtocolRegistry.load()
            for number, free in ((43, False), (44, True)):
                selected = [row for row in rows if row["FreeFlag"] is free]
                encode_response(registry.by_registration[number], dumps(Container(children=self.bootstrap.columns("crystals", selected))))
        tables = [node for node in root.children if node.name == "POSSESSION"]
        if not tables:
            return
        if len(tables) != 1 or not isinstance(tables[0], Container):
            raise ProtocolError("present_history_invalid", "Saved item receipt has an invalid possession table", 503)
        names = [column for column in tables[0].children if isinstance(column, Column) and column.name == "ItemID"]
        if len(names) != 1 or names[0].type != "S" or not names[0].values:
            raise ProtocolError("present_history_invalid", "Saved item receipt has invalid item identities", 503)
        for item_id in names[0].values:
            self._item_definition({"Type": 22, "ItemID": item_id})
        if not self.possessions.table_matches(tables[0], profile.state, names[0].values):
            raise ProtocolError("present_history_invalid", "Saved item receipt disagrees with the current possession state", 503)
        self._preflight_items(profile, profile.state)
