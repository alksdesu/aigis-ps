from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import asdict, astuple
from datetime import datetime
from functools import wraps
from http import HTTPStatus
from operator import itemgetter
from types import SimpleNamespace
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .admin_app import server_time
from .admin_cmd_growth import (EVOLUTION_REGISTRATIONS, MATERIAL_SUBJECT, MAX_AWAKENING_BRANCH, EvolutionOperation,
                               WarehouseMaterial)
from .admin_cmd_units import distinct_indexes, protection_details, protection_names, summary as unit_summary
from .admin_command import AdminError, CommandParams
from .admin_commit import attempt
from .admin_fields import anchors
from .admin_registry import AdminView, ViewRegistry
from .admin_views import PageParams, ViewContext, ViewRead, page, projected, redacted_view, service
from .admin_views_account import GAME_PARAM, energy_clock as clock_marker
from .admin_views_units import barracks_usage
from .config_state import configuration_row
from .energy_clock import PREFIXES, energy_seconds, set_anchor
from .errors import ProtocolError
from .evolution_materials import EVOLUTION_MATERIALS_INVALID
from .evolution_protocol import MAX_EVOLUTION_MATERIALS
from .evolution_service import EvolutionService
from .native_numbers import INT32_MAX
from .player_state import MAX_PLAYER_UNITS, barracks_index
from .possessions import STORED_ITEM_NAME_BYTES
from .presents import MAX_DESCRIPTION_BYTES, MAX_PRESENTS
from .sale_protocol import MANUAL_SALE
from .training_protocol import MAX_TRAINING_MATERIALS
from .training_service import MaterialsProtected, TrainingService
from .unit_reference import FIRST_BARRACKS
from .unit_removal import protection_check
from .unit_sale import SALE_POLICIES
from .wallet_money import GOLD_LIMIT, money_after_capped_credit


TOOLS = "tools"
GetIndex = Annotated[int, Field(ge=0, le=INT32_MAX)]
Identifier = Annotated[int, Field(ge=1, le=INT32_MAX)]
Amount = Annotated[int, Field(ge=1, le=INT32_MAX)]
ItemName = Annotated[str, Field(min_length=1, max_length=STORED_ITEM_NAME_BYTES)]
Description = Annotated[str, Field(max_length=MAX_DESCRIPTION_BYTES)]
# Silver and growth exchanges buy exactly one unit per request.
SINGLE_PURCHASE = 1
UNCONFIGURED_RESET = "unconfigured"
# Skill awakening consumes the fairy its rarity names, so only class awakenings fill material slots.
SLOTTED_OPERATIONS = frozenset((EvolutionOperation.FIRST_AWAKENING, EvolutionOperation.SECOND_AWAKENING))


class OrbExchangeParams(CommandParams):
    item_id: ItemName
    base_id: ItemName


class BarracksMaterial(CommandParams):
    source: Literal["barracks"]
    get_index: GetIndex


PreviewMaterial = Annotated[BarracksMaterial | WarehouseMaterial, Field(discriminator="source")]


def _barracks_used_once(target_get_index: int, materials: Iterable[PreviewMaterial]) -> None:
    distinct_indexes((target_get_index, *(material.get_index for material in materials
                                          if isinstance(material, BarracksMaterial))), MATERIAL_SUBJECT)


class EvolutionParams(CommandParams):
    target_get_index: GetIndex
    operation: EvolutionOperation
    branch: int = Field(default=0, ge=0, le=MAX_AWAKENING_BRANCH)
    materials: list[PreviewMaterial] = Field(default_factory=list, max_length=MAX_EVOLUTION_MATERIALS)

    @model_validator(mode="after")
    def _units_used_once(self) -> EvolutionParams:
        _barracks_used_once(self.target_get_index, self.materials)
        return self


class TrainingParams(CommandParams):
    target_get_index: GetIndex
    materials: list[PreviewMaterial] = Field(min_length=1, max_length=MAX_TRAINING_MATERIALS)

    @model_validator(mode="after")
    def _units_used_once(self) -> TrainingParams:
        _barracks_used_once(self.target_get_index, self.materials)
        return self


class UnitSaleParams(CommandParams):
    get_indexes: list[GetIndex] = Field(min_length=1, max_length=MAX_PLAYER_UNITS)

    @field_validator("get_indexes")
    @classmethod
    def _units_listed_once(cls, value: list[int]) -> list[int]:
        distinct_indexes(value)
        return value


class SilverTradeParams(CommandParams):
    shop_id: Identifier
    material_get_index: GetIndex | None = None


class GrowthShopParams(CommandParams):
    shop_id: Identifier
    target_get_index: GetIndex | None = None


class UnitSkinParams(CommandParams):
    card_id: Identifier


class SummonPoolParams(PageParams):
    pool_id: Identifier


class ItemTicketParams(CommandParams):
    item_id: ItemName


class UnitReward(CommandParams):
    kind: Literal["unit"]
    template_id: Identifier
    count: Amount
    description: Description = ""


class AmountReward(CommandParams):
    kind: Literal["free_crystal", "gold"]
    amount: Amount
    description: Description = ""


class ItemReward(CommandParams):
    kind: Literal["item"]
    item_id: ItemName
    amount: Amount
    description: Description = ""


class LegacyItemReward(CommandParams):
    kind: Literal["legacy_item"]
    present_type: Annotated[int, Field(ge=0, le=INT32_MAX)]
    amount: Amount
    description: Description = ""


class SummonTicketReward(CommandParams):
    kind: Literal["summon_ticket"]
    pool_id: Identifier
    count: Amount
    description: Description = ""


class SelectionTicketReward(CommandParams):
    kind: Literal["selection_ticket"]
    config_id: Identifier
    count: Amount
    description: Description = ""


class RecoveryReward(CommandParams):
    kind: Literal["charisma_recovery"]
    item_id: ItemName | None = None
    description: Description = ""


class CookingReward(CommandParams):
    kind: Literal["vote_cooking"]
    card_id: Identifier
    description: Description = ""


PresentReward = Annotated[UnitReward | AmountReward | ItemReward | LegacyItemReward | SummonTicketReward
                          | SelectionTicketReward | RecoveryReward | CookingReward, Field(discriminator="kind")]


class PresentPreviewParams(CommandParams):
    rewards: list[PresentReward] = Field(min_length=1, max_length=MAX_PRESENTS)


class EnergyClockParams(CommandParams):
    at: AwareDatetime | None = None


def passes(check: Callable[[], object]) -> dict:
    def verdict() -> None:
        check()

    return attempt(verdict)


def changes(unit: Mapping, values: Mapping) -> dict:
    return {name: {"before": unit.get(name), "after": value} for name, value in sorted(values.items())}


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def roster(bundle: Any, state: dict) -> dict:
    return service(bundle, "player_state").validate(state)


def owned_unit(player: Mapping, get_index: int) -> dict:
    unit = next((unit for unit in player["units"] if unit["GetIndex"] == get_index), None)
    if unit is None:
        raise AdminError("admin_tool_unit_missing", f"No owned unit has GetIndex {get_index}", HTTPStatus.NOT_FOUND)
    return unit


def money(bundle: Any, state: dict) -> int:
    return service(bundle, "bootstrap").table_rows(state, GAME_PARAM)[0]["Money"]


def held(possessions: Any, state: dict, item_ids: Iterable[str]) -> dict[str, int]:
    columns = {column.name: column.values for column in possessions.response(state, tuple(item_ids)).children}
    return dict(zip(columns.get("ItemID", ()), columns.get("Count", ())))


def orb_exchange_preview(context: ViewContext, params: OrbExchangeParams) -> dict:
    orbs = service(context.generation.bundle, "orb_exchange")
    return {"definitions": [asdict(definition) for definition in orbs.definitions],
            "quote": attempt(lambda: projected(**asdict(orbs.quote(context.state, params.item_id, params.base_id))))}


def storage(unit: Mapping, capacity: Any = None, state: dict | None = None) -> dict:
    index = barracks_index(unit)
    # Services take first-barracks units, except growth materials once barracks capacity is tracked.
    supported = index == FIRST_BARRACKS if capacity is None else index < len(capacity.usage(state).capacities)
    return {"barracks": index, "storage_supported": supported}


def growth_material(growth: Any, player: Mapping, reference: PreviewMaterial) -> dict:
    if isinstance(reference, BarracksMaterial):
        return owned_unit(player, reference.get_index)
    return service(growth, reference.source).material(reference.card_id)


def material_summaries(growth: Any, state: dict, references: Iterable[PreviewMaterial],
                       materials: Iterable[Mapping]) -> list[dict]:
    return [{"source": reference.source, **unit_summary(material),
             **(storage(material, growth.barracks, state) if isinstance(reference, BarracksMaterial) else {})}
            for reference, material in zip(references, materials)]


def protection(protect: Callable[[Mapping, Mapping, Iterable[Mapping]], None], player: Mapping, target: Mapping,
               references: Iterable[PreviewMaterial], materials: Iterable[Mapping]) -> None:
    # The growth kernels protect barracks units only; warehouse stock is consumed by count.
    held = [material for reference, material in zip(references, materials) if isinstance(reference, BarracksMaterial)]
    with protection_details(MaterialsProtected):
        protect(player, target, held)


def material_slots(catalog: Any, target: Mapping, operation: int) -> dict:
    card = catalog.cards.card(target["CardID"])
    try:
        described = SimpleNamespace(card=card, class_row=catalog.classes[target["ClassID"]],
                                    rarity=catalog.rarities[card["Rare"] + 1])
    except (KeyError, TypeError) as error:
        raise ProtocolError(EVOLUTION_MATERIALS_INVALID, "Evolution unit does not match the verified catalogs",
                            503) from error
    slots = catalog.materials.slots(described, operation)
    return {"slots": [[asdict(condition) for condition in slot] for slot in slots]}


def evolution_quote(bundle: Any, state: dict, evolution: Any, target: dict, materials: list[dict], registration: int,
                    branch: int) -> dict:
    # Every section reads the same snapshot, so the catalog quotes copies as the evolution service does.
    quote = evolution.catalog.quote(deepcopy(target), deepcopy(materials), registration, branch)
    holdings = held(evolution.possessions, state, quote.items)
    return projected(price=quote.price, money=money(bundle, state),
                     items={name: {"cost": cost, "held": holdings[name]} for name, cost in quote.items.items()},
                     changes=changes(target, quote.values), material_assignment=list(quote.material_assignment),
                     reward_card_id=quote.reward_card_id)


def evolution_preview(context: ViewContext, params: EvolutionParams) -> dict:
    bundle, state = context.generation.bundle, context.state
    evolution, player = service(bundle, "evolution"), roster(bundle, state)
    target = owned_unit(player, params.target_get_index)
    materials = [growth_material(evolution, player, reference) for reference in params.materials]
    registration = EVOLUTION_REGISTRATIONS[params.operation]
    return {"target": {**unit_summary(target), **storage(target)},
            "materials": material_summaries(evolution, state, params.materials, materials),
            "protection": passes(lambda: protection(EvolutionService.protect, player, target, params.materials,
                                                    materials)),
            "slots": (attempt(lambda: material_slots(evolution.catalog, target, registration))
                      if params.operation in SLOTTED_OPERATIONS else None),
            "quote": attempt(lambda: evolution_quote(bundle, state, evolution, target, materials, registration,
                                                     params.branch))}


def training_quote(bundle: Any, state: dict, catalog: Any, player: Mapping, target: dict,
                   materials: list[dict]) -> dict:
    bonus = catalog.ownership_bonus(player, state)
    # Every section reads the same snapshot, so the catalog quotes copies as the evolution preview does.
    quote = catalog.quote_materials(player, deepcopy(target), deepcopy(materials), state=state)
    before, after = catalog.describe(target), catalog.describe({**target, **quote.values})
    return projected(price=quote.price, money=money(bundle, state),
                     bonus={"percent": bonus.percent,
                            "sources": [asdict(source) for source in sorted(bonus.sources, key=astuple)]},
                     level={"before": before.level, "after": after.level, "cap": before.cap},
                     changes=changes(target, quote.values))


def training_preview(context: ViewContext, params: TrainingParams) -> dict:
    bundle, state = context.generation.bundle, context.state
    training, player = service(bundle, "training"), roster(bundle, state)
    target = owned_unit(player, params.target_get_index)
    materials = [growth_material(training, player, reference) for reference in params.materials]
    return {"target": {**unit_summary(target), **storage(target)},
            "materials": material_summaries(training, state, params.materials, materials),
            "protection": passes(lambda: protection(TrainingService.protect, player, target, params.materials,
                                                    materials)),
            "quote": attempt(lambda: training_quote(bundle, state, training.catalog, player, target, materials))}


def sale_quote(bundle: Any, state: dict, catalog: Any, units: list[dict]) -> dict:
    quote, balance = catalog.quote(units), money(bundle, state)
    # Game-side credits discard gold over the cap, as the sale service does.
    after = money_after_capped_credit(state, quote.gold, cap=GOLD_LIMIT)
    return projected(gold=quote.gold, rainbow=quote.rainbow, rainbow_item=catalog.rainbow_item,
                     money={"before": balance, "after": after, "discarded": balance + quote.gold - after})


def unit_sale_preview(context: ViewContext, params: UnitSaleParams) -> dict:
    bundle, state = context.generation.bundle, context.state
    catalog, player = service(bundle, "sales").catalog, roster(bundle, state)
    units = [owned_unit(player, get_index) for get_index in params.get_indexes]
    automatic = frozenset(catalog.auto_card_ids)
    # units.sell follows the manual sale list, so its policy names the units a sale would refuse.
    protections = protection_check(player, SALE_POLICIES[MANUAL_SALE].refuse)
    # units.sell, like native 74, sells only units in the first barracks.
    return {"units": [{**unit_summary(unit), "rating": passes(lambda: catalog.check_rating(unit["CardID"])),
                       "automatic": unit["CardID"] in automatic, "protections": protection_names(protections(unit)),
                       **storage(unit)} for unit in units],
            "quote": attempt(lambda: sale_quote(bundle, state, catalog, units))}


def trade_quote(trades: Any, catalog: Any, state: dict, shop_id: int, now: datetime) -> dict:
    history = trades.history
    # The trade service validates the purchase history before the product, so refusals keep its priority.
    records = history.records(state, now)
    product = catalog.product(shop_id)
    period = history.period(product, now)
    if period == UNCONFIGURED_RESET:
        raise ProtocolError("trade_reset_unconfigured",
                            "The local reset policy for this product has not been configured", 501)
    quote = catalog.quote(state, shop_id, SINGLE_PURCHASE, history.count(records, product, now), now)
    return projected(shop_id=shop_id, period=period, exchange_count=quote.exchange_count, limit=product.data_restrict,
                     window={"start": _iso(product.start_at), "end": _iso(product.end_at)},
                     unit_price=quote.unit_price, total_price=quote.total_price,
                     ownership_discount=quote.ownership_discount,
                     currency={"kind": quote.currency_kind, "item_id": quote.currency_item_id,
                               "balance": quote.currency_balance},
                     reward={"card_id": product.card_id, "item_id": product.item_id, "amount": quote.amount})


def silver_material(catalog: Any, player: Mapping, state: dict, shop_id: int, material: Mapping) -> dict:
    selected = catalog.material(player, catalog.product(shop_id), material["GetIndex"], material["UniqueID"],
                                state=state)
    return {**unit_summary(selected), **storage(selected)}


def silver_trade_preview(context: ViewContext, params: SilverTradeParams) -> dict:
    bundle, state = context.generation.bundle, context.state
    trades, players = service(bundle, "trades"), service(bundle, "player_state")
    catalog, player = service(trades, "silver"), roster(bundle, state)
    material = None if params.material_get_index is None else owned_unit(player, params.material_get_index)
    return {"quote": attempt(lambda: trade_quote(trades, catalog, state, params.shop_id, context.now)),
            "material": None if material is None else attempt(lambda: silver_material(
                catalog, player, state, params.shop_id, material)),
            "barracks": passes(lambda: players.require_barracks_space(state, SINGLE_PURCHASE))}


def growth_values(catalog: Any, player: Mapping, state: dict, shop_id: int, get_index: int) -> dict:
    values = catalog.values(player, catalog.product(shop_id), get_index, state=state)
    target = owned_unit(player, get_index)
    return projected(target={**unit_summary(target), **storage(target)}, changes=changes(target, values))


def growth_shop_preview(context: ViewContext, params: GrowthShopParams) -> dict:
    bundle, state = context.generation.bundle, context.state
    trades = service(bundle, "trades")
    catalog, player = service(trades, "growth_shop"), roster(bundle, state)
    return {"quote": attempt(lambda: trade_quote(trades, catalog, state, params.shop_id, context.now)),
            "values": None if params.target_get_index is None else attempt(lambda: growth_values(
                catalog, player, state, params.shop_id, params.target_get_index))}


def unit_skins_preview(context: ViewContext, params: UnitSkinParams) -> dict:
    detail = service(context.generation.bundle, "unit_detail")
    displays = service(detail, "skin_catalog").allowed_displays(params.card_id, context.state,
                                                                smartphone_images=detail.smartphone_images)
    return {"card_id": params.card_id, "smartphone_images": detail.smartphone_images,
            "allowed_displays": sorted(displays)}


def background(catalog: Any, definition: Any, guide: int, now: datetime) -> dict:
    return {"name": definition.name, "card_id": definition.card_id, "category": definition.category,
            "selectable": definition.category in catalog.categories, "is_fixed": definition.is_fixed,
            "begin": _iso(definition.begin), "end": _iso(definition.end),
            "selection": attempt(lambda: {"card_ids": list(catalog.selection(definition.name, guide, now))})}


def backgrounds_preview(context: ViewContext, params: PageParams) -> dict:
    bundle, state, now = context.generation.bundle, context.state, context.now
    catalog = service(bundle, "backgrounds").catalog
    guide = catalog.guide_id(roster(bundle, state), configuration_row(state))
    return {"guide_card_id": guide, "definitions": page(
        catalog.definitions, params, lambda definition: background(catalog, definition, guide, now))}


def emblems_preview(context: ViewContext, params: PageParams) -> dict:
    catalog = service(service(context.generation.bundle, "presents"), "emblem_catalog")
    return {"titles": page(catalog.titles, params, lambda title: {
        **asdict(title), "eligibility": passes(lambda: catalog.require_eligible(title.index, context.state))})}


def fame_preview(context: ViewContext, params: PageParams) -> dict:
    bundle = context.generation.bundle
    catalog = service(bundle, "fame_repository").catalog
    units = sorted(roster(bundle, context.state)["units"], key=itemgetter("GetIndex"))
    return {"units": page(units, params, lambda unit: {
        **unit_summary(unit), "eligibility": passes(lambda: catalog.require_eligible(unit))})}


def unit_capacity_preview(context: ViewContext, params: None) -> dict:
    bundle, state = context.generation.bundle, context.state
    capacity, players = service(bundle, "unit_capacity"), service(bundle, "player_state")
    return {"barracks": attempt(lambda: barracks_usage(capacity, state)),
            "unit_slots": attempt(lambda: {"available": players.available_unit_slots(state)})}


def pool_summary(catalog: Any, pool: Any) -> dict:
    return {"pool_id": pool.pool_id, "name_id": pool.name_id, "candidate_count": len(pool.candidates),
            "total_weight": pool.total_weight, "percent_sum": pool.percent_sum, "complete": pool.complete,
            "drawable": passes(lambda: catalog.require_complete(pool.pool_id))}


def summon_pools_preview(context: ViewContext, params: PageParams) -> dict:
    catalog = service(context.generation.bundle, "summon_pools")
    return {"pools": page(sorted(catalog.pools), params, lambda pool_id: pool_summary(catalog, catalog.get(pool_id)))}


def pool_candidate(candidate: Any, total_weight: int | None) -> dict:
    # Undrawable pools keep their raw percentages; normalizing them would invent the missing candidates.
    return {"card_id": candidate.card_id, "level": candidate.level, "evolution_count": candidate.evolution_count,
            "percent": candidate.percent, "weight": candidate.weight,
            "draw_probability": None if total_weight is None else candidate.weight / total_weight}


def summon_pool_preview(context: ViewContext, params: SummonPoolParams) -> dict:
    catalog = service(context.generation.bundle, "summon_pools")
    pool = catalog.get(params.pool_id)
    summary = pool_summary(catalog, pool)
    total_weight = pool.total_weight if summary["drawable"]["ok"] else None
    return {**summary, "candidates": page(pool.candidates, params,
                                          lambda candidate: pool_candidate(candidate, total_weight))}


def item_ticket_preview(context: ViewContext, params: ItemTicketParams) -> dict:
    tickets = service(context.generation.bundle, "item_summon_tickets")
    return {"definition": attempt(lambda: asdict(tickets.catalog.definition(params.item_id))),
            "delivery": passes(lambda: tickets.validate_item(params.item_id))}


def presents_preview(context: ViewContext, params: PresentPreviewParams) -> dict:
    presents = service(context.generation.bundle, "presents")
    existing = len(presents.rows(context.state))
    rows = presents.preview([reward.model_dump(exclude_none=True) for reward in params.rewards], existing)
    return projected(existing=existing, added=len(rows), capacity=MAX_PRESENTS, rows=rows)


def clock_conversion(bundle: Any, state: dict, moment: datetime) -> dict:
    seconds, encoded = energy_seconds(moment), {}
    for prefix in PREFIXES:
        set_anchor(encoded, seconds, prefix)
    current = service(bundle, "bootstrap").table_rows(state, GAME_PARAM)[0]
    return projected(instant=server_time(moment), seconds=seconds, fields=encoded, decoded=anchors(encoded),
                     current=anchors(current))


def energy_clock_preview(context: ViewContext, params: EnergyClockParams) -> dict:
    moment = context.now if params.at is None else params.at
    return {"marker": attempt(lambda: clock_marker(context.state)),
            "conversion": attempt(lambda: clock_conversion(context.generation.bundle, context.state, moment))}


def tool(name: str, read: ViewRead, params_model: type[CommandParams] | None = None) -> AdminView:
    # A refused preview is itself the answer, so it is reported as a result rather than as a failed request.
    @wraps(read)
    def attempted(context: ViewContext, params: Any) -> dict:
        return attempt(lambda: read(context, params))

    # Previews are computed on request against the server clock, so no answer is ever served from a cache.
    return redacted_view(name, TOOLS, attempted, params_model, live=True)


VIEWS = (
    tool("tools.orb_exchange", orb_exchange_preview, OrbExchangeParams),
    tool("tools.evolution", evolution_preview, EvolutionParams),
    tool("tools.training", training_preview, TrainingParams),
    tool("tools.unit_sale", unit_sale_preview, UnitSaleParams),
    tool("tools.silver_trade", silver_trade_preview, SilverTradeParams),
    tool("tools.growth_shop", growth_shop_preview, GrowthShopParams),
    tool("tools.unit_skins", unit_skins_preview, UnitSkinParams),
    tool("tools.backgrounds", backgrounds_preview, PageParams),
    tool("tools.emblems", emblems_preview, PageParams),
    tool("tools.fame", fame_preview, PageParams),
    tool("tools.unit_capacity", unit_capacity_preview),
    tool("tools.summon_pools", summon_pools_preview, PageParams),
    tool("tools.summon_pool", summon_pool_preview, SummonPoolParams),
    tool("tools.item_ticket", item_ticket_preview, ItemTicketParams),
    tool("tools.presents", presents_preview, PresentPreviewParams),
    tool("tools.energy_clock", energy_clock_preview, EnergyClockParams),
)


def register(registry: ViewRegistry) -> None:
    for view in VIEWS:
        registry.add(view)
