from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
import hashlib
import json
import re
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .errors import ProtocolError, caught
from .evolution_catalog import EvolutionRules, first_awake_experience
from .native_numbers import INT32_MIN, int32
from .player_state import (
    DISPLAY_FIELD, MAX_PLAYER_UNITS, PRINCE_CARD_IDS, UNIT_BOOLEANS, UNIT_FIELDS, barracks_index,
)
from .public_tables import MASTER_LIMITS, BytesDigest
from .system_literals import command_id, decode_command, rating_command_id
from .trade_catalog import TradeProduct
from .training_math import experience_threshold
from .unit_initialization import CLASSIC_ROUTE_CARD_ID, stage_one_class_id


REGISTRATION = 113
PAYMENT_ITEM = "trade_item_005000"
PAYMENT_HASH = command_id(PAYMENT_ITEM)
# Native 4DA6E0 rewrites the classic-route card as this card when it buys the first awakening.
CLASSIC_RESULT_CARD_ID = 99
MENU_NAME = "CMenuUnitGrowthSelect::HasShowUnitEvolve1"
MENU_CONDITION = "HasFirstEvolveClass() && (GetClassChange() < 2) && !IsPrince();"
_MENU_PATTERN = re.compile(r"\s*HasFirstEvolveClass\s*\(\s*\)\s*&&\s*\(\s*GetClassChange\s*\(\s*\)\s*"
                           r"<\s*2\s*\)\s*&&\s*!\s*IsPrince\s*\(\s*\)\s*;\s*\Z")
CARD_FIELDS = ("CardID", "Rare", "InitClassID", "ClassLV1SkillID", "CostDecValue")
CLASS_FIELDS = ("ClassID", "MaxLevel", "JobChange")
RARITY_FIELDS = ("ConfigID", "EvolutionConfig", "RequireExpRatio", "LevelCap00", "LevelCap01", "LevelCap02")
LIMITS = ParseLimits(max_input_bytes=64 * 1024 * 1024, max_decompressed_bytes=64 * 1024 * 1024,
                     max_rows=65535, max_fields=128)
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _integer(value, minimum=0):
    if not int32(value) or value < minimum:
        raise ValueError("Expected a bounded native integer")
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _indexed(source, fields, key_field):
    if not isinstance(source, Mapping) or not 1 <= len(source) <= 65535:
        raise ValueError("Expected a bounded resource mapping")
    result = {}
    for key, row in source.items():
        _integer(key, 1)
        if not isinstance(row, Mapping):
            raise ValueError("Expected a resource record")
        selected = {name: _integer(row[name]) for name in fields}
        if selected[key_field] != key:
            raise ValueError("Resource key does not match its record")
        result[key] = selected
    return result


def _rarities(value):
    if not isinstance(value, str) or len(value) > 4096 or not re.fullmatch(r"[0-9]+(?:,[0-9]+)*,?", value):
        raise ValueError("Target rarities require a literal integer list")
    result = tuple(_integer(int(part)) for part in value.rstrip(",").split(","))
    if len(set(result)) != len(result):
        raise ValueError("Target rarities must be distinct")
    return result


def _freeze(rows):
    return MappingProxyType({key: MappingProxyType(row) for key, row in rows.items()})


class ShopGrowthCatalog:
    def __init__(self, base, growth, asset_reader, *, rules=EvolutionRules()):
        self.base, self.growth, self.rules = base, growth, rules
        self._card_digest = BytesDigest()
        try:
            if not callable(asset_reader) or not isinstance(rules, EvolutionRules):
                raise ValueError("Shop growth requires a resource reader and evolution rules")
            if not isinstance(base.content_version, str) or not _HASH.fullmatch(base.content_version):
                raise ValueError("Shop growth requires a verified base trade catalog")
            self._base_version = base.content_version
            self._configured_rules = rules
            snapshot = self._snapshot()
            self._signature = _digest(snapshot)
            self._cards = _freeze(snapshot["cards"])
            self._classes = _freeze(snapshot["classes"])
            self._rarities = _freeze(snapshot["rarities"])
            self._experience = tuple(snapshot["experience"])
            self._skills = tuple(snapshot["skills"])
            self._resource_version = growth.cards.resource_version
            hashes = {}

            def table(name, schema):
                raw = asset_reader(name)
                if not isinstance(raw, bytes) or not 0 < len(raw) <= LIMITS.max_input_bytes:
                    raise ValueError("Growth resources must be bounded immutable bytes")
                hashes[name] = hashlib.sha256(raw).hexdigest()
                payload = decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw
                parsed = parse_table(payload, LIMITS)
                fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
                if len(fields) != len(parsed["fields"]) or any(fields.get(key) != value for key, value in schema.items()):
                    raise ValueError("Growth resource schema differs from the verified contract")
                return parsed["rows"]

            experience = table("ExperienceTable.atb", {"Level": 1, "Exp": 1})
            if (any(row["Level"] != index + 1 for index, row in enumerate(experience))
                    or tuple(row["Exp"] for row in experience) != self._experience):
                raise ValueError("Growth experience resources disagree")
            rarities = table("cfgRarity.atb", {name: 1 for name in RARITY_FIELDS})
            indexed = {row["ConfigID"]: row for row in rarities}
            if (len(indexed) != len(rarities)
                    or _indexed(indexed, RARITY_FIELDS, "ConfigID") != snapshot["rarities"]):
                raise ValueError("Growth rarity resources disagree")
            skills = table("SkillList.atb", {"LevelMax": 1})
            if tuple(row["LevelMax"] for row in skills) != self._skills:
                raise ValueError("Growth skill resources disagree")
            menus = table("OnCommandMenuUnitGrowthSelect.atb", {"_ID": 1, "_Cmnd": 32})
            menu = [row for row in menus if row["_ID"] == command_id(MENU_NAME)]
            if len(menu) != 1 or not _MENU_PATTERN.fullmatch(decode_command(menu[0]["_Cmnd"])):
                raise ValueError("Growth target condition differs from the verified command")
            commands = {}
            for row in table("OnCommand.atb", {"_ID": 1, "_Cmnd": 32}):
                identifier = _integer(row["_ID"], INT32_MIN)
                if not isinstance(row["_Cmnd"], str):
                    raise ValueError("Rating commands require encoded text")
                commands.setdefault(identifier, []).append(row["_Cmnd"])
            restricted, owners = set(), {}
            for card_id in self._cards:
                identifier = rating_command_id(card_id)
                if identifier in owners:
                    raise ValueError("Rating command identifiers collide")
                owners[identifier] = card_id
                entries = commands.get(identifier, ())
                try:
                    if len(entries) > 1 or (entries and decode_command(entries[0]).strip()):
                        restricted.add(card_id)
                except ValueError:
                    restricted.add(card_id)
            self._rating_restricted = frozenset(restricted)
            products, sources, targets = {}, {}, {}
            for product in base.products:
                if not isinstance(product, TradeProduct):
                    raise ValueError("Base catalog returned an invalid product")
                if (product.shop_type, product.product_type, product.trade_type) != (2, 6, 9):
                    continue
                if product.shop_id in products:
                    raise ValueError("Growth product identifiers are duplicated")
                if (product.currency_kind != "possession" or product.currency_item_id != PAYMENT_ITEM
                        or product.consume_item_id != PAYMENT_ITEM or product.card_id or product.item_id
                        or any(getattr(product, field) for field in ("gain_exp", "type_catalog", "unit_base_level",
                            "unit_cost_modify", "unit_evolution_count", "unit_skill_level", "unit_skill_evolution", "unit_init_trust"))):
                    raise ValueError("Growth product has unsupported payment or result metadata")
                targets[product.shop_id] = _rarities(product.target_rarity)
                if any(rare + 1 not in self._rarities for rare in targets[product.shop_id]):
                    raise ValueError("Growth product references an unknown rarity")
                sources[product.shop_id] = product
                products[product.shop_id] = replace(product, registration=REGISTRATION)
            self._sources, self._products, self._targets = (MappingProxyType(value) for value in (sources, products, targets))
            self._hashes = MappingProxyType(hashes)
            self._payment_stack = self._check_payment()
            self.content_version = _digest({"policy": 1, "base": self._base_version, "growth": self._signature,
                "resources": hashes, "rules": asdict(rules), "registration": REGISTRATION,
                "target_storage": "first_barracks", "quantity": 1})
            self._check_dependencies()
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError) as error:
            raise ProtocolError("shop_growth_catalog_invalid", "Shop growth resources are invalid or inconsistent", 503) from error

    def _snapshot(self):
        cards = self.growth.cards
        if cards.resource_version != self.base.resource_version:
            raise ValueError("Shop and growth resource versions disagree")
        raw = cards.snapshot.get_xml("CardData")
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MASTER_LIMITS.max_bytes:
            raise ValueError("CardData must be bounded immutable bytes")
        identifiers = cards.card_ids
        if not isinstance(identifiers, (list, tuple)) or not 1 <= len(identifiers) <= 65535 or len(set(identifiers)) != len(identifiers):
            raise ValueError("Card identifiers must be a bounded distinct sequence")
        selected = _indexed({key: cards.card(key) for key in identifiers}, CARD_FIELDS, "CardID")
        classes = _indexed(self.growth.classes, CLASS_FIELDS, "ClassID")
        rarities = _indexed(self.growth.rarities, RARITY_FIELDS, "ConfigID")
        experience = self.growth.experience
        if not isinstance(experience, Sequence) or not 1 <= len(experience) <= 65535:
            raise ValueError("Growth experience must be a bounded sequence")
        experience = tuple(_integer(value) for value in experience)
        if experience[0] != 0 or any(left >= right for left, right in zip(experience, experience[1:])):
            raise ValueError("Growth experience must increase from zero")
        skills = self.growth.skills
        if not isinstance(skills, Sequence) or not 1 <= len(skills) <= 65535:
            raise ValueError("Growth skill definitions must be a bounded sequence")
        skills = tuple(_integer(row["LevelMax"]) for row in skills)
        identity = (cards.snapshot.identifier, cards.snapshot.client_hash, self._card_digest(raw))
        if any(not isinstance(value, str) or not value for value in identity):
            raise ValueError("CardData identity is unavailable")
        return {"master": identity, "resource_version": cards.resource_version, "cards": selected,
                "classes": classes, "rarities": rarities, "experience": experience, "skills": skills}

    def _check_payment(self):
        possessions = self.base.possessions
        stack = _integer(possessions.stack(PAYMENT_ITEM), 1)
        if not possessions.is_ready() or possessions.resolve_hash(PAYMENT_HASH) != PAYMENT_ITEM:
            raise ValueError("Growth payment is not a verified positive-stack item")
        return stack

    def _check_dependencies(self):
        try:
            if (self.base.content_version != self._base_version or not self.base.is_ready()
                    or self.rules != self._configured_rules
                    or _digest(self._snapshot()) != self._signature
                    or any(self.base.product(key) != row for key, row in self._sources.items())):
                raise ValueError("Shop growth dependencies changed")
            if self._check_payment() != self._payment_stack:
                raise ValueError("Growth payment definition changed")
        except (AttributeError, OSError, ValueError, KeyError, TypeError, OverflowError) as error:
            raise ProtocolError("shop_growth_content_changed", "Shop growth dependencies changed or are unavailable", 503) from error

    @property
    def products(self):
        self._check_dependencies()
        return tuple(self._products.values())

    def product(self, shop_id):
        self._check_dependencies()
        self.base.product(shop_id)
        if shop_id not in self._products:
            raise ProtocolError("shop_growth_product_unsupported", "This product is not a verified first-awakening purchase", 501)
        return self._products[shop_id]

    def quote(self, state, shop_id, quantity, exchange_count, now, registration=REGISTRATION):
        if not int32(quantity) or quantity != 1:
            raise ProtocolError("shop_growth_quantity_invalid", "Growth purchases require exactly one target")
        if not int32(registration) or registration != REGISTRATION:
            raise ProtocolError("trade_registration_mismatch", "Growth purchases require registration 113")
        return self.base._quote_product(state, self.product(shop_id), quantity, exchange_count, now, registration)

    def values(self, player, product, get_index, *, state):
        if not isinstance(product, TradeProduct) or product != self.product(product.shop_id):
            raise ProtocolError("shop_growth_product_invalid", "Target selection requires the verified growth product")
        if not int32(get_index) or get_index < 0:
            raise ProtocolError("shop_growth_identity_invalid", "Growth target requires a nonnegative GetIndex")
        try:
            if not isinstance(state, dict) or state.get("player_state") != player:
                raise ValueError("Growth target requires the matching complete state")
            overrides = state.get("unit_dynamic_overrides")
            if overrides is not None and not isinstance(overrides, (dict, list)):
                raise ValueError("Dynamic unit overrides have an invalid shape")
            if overrides:
                raise ProtocolError("shop_growth_overrides_unsupported", "Dynamic unit overrides require a separate growth rule", 501)
            if (not isinstance(player, dict) or type(player.get("schema_version")) is not int or player["schema_version"] != 1
                    or not isinstance(player.get("units"), list) or not 1 <= len(player["units"]) <= MAX_PLAYER_UNITS):
                raise ValueError("Growth requires a bounded verified player roster")
            indices, identities, selected = set(), set(), None
            for unit in player["units"]:
                if not isinstance(unit, dict) or set(unit) != {name for name, _ in UNIT_FIELDS}:
                    raise ValueError("Growth target fields do not match the native unit")
                if any(type(value) is not bool if name in UNIT_BOOLEANS else not int32(value) for name, value in unit.items()):
                    raise ValueError("Growth unit field types are invalid")
                if (unit["CardID"] not in self._cards or unit["ClassID"] not in self._classes
                        or unit["GetIndex"] < 0 or unit["UniqueID"] <= 0
                        or any(unit[name] < 0 for name in ("ClassChangeCount", "Exp", "Love", "SkillLevel",
                                                         "SettingSkillID", "SkillExperience", "CostExperience"))
                        or unit["GetIndex"] in indices or unit["UniqueID"] in identities):
                    raise ValueError("Growth unit identities or quantities are invalid")
                indices.add(unit["GetIndex"])
                identities.add(unit["UniqueID"])
                if unit["GetIndex"] == get_index:
                    selected = unit
            if selected is None:
                raise ProtocolError("shop_growth_target_missing", "Growth target is not an owned unit", 409)
            if barracks_index(selected):
                raise ProtocolError("shop_growth_storage_unsupported", "Growth targets must be in the first barracks", 501)
            card = self._cards[selected["CardID"]]
            if card["CardID"] in self._rating_restricted:
                raise ProtocolError("shop_growth_rating_unsupported", "This unit has an unverified rating restriction", 501)
            stage = selected["ClassChangeCount"]
            initial = self._classes[card["InitClassID"]]
            rarity = self._rarities[card["Rare"] + 1]
            first_class = self._classes[stage_one_class_id(card)]
            expected_class = first_class["ClassID"] if stage == 1 else initial["ClassID"]
            if (card["CardID"] in PRINCE_CARD_IDS or stage not in (0, 1) or card["Rare"] not in self._targets[product.shop_id]
                    or selected["ClassID"] != expected_class or not initial["JobChange"]
                    or not first_class["JobChange"] or not rarity["EvolutionConfig"]):
                raise ProtocolError("shop_growth_target_ineligible", "This target cannot buy the selected first awakening", 409)
            result_card = self._cards[CLASSIC_RESULT_CARD_ID] if card["CardID"] == CLASSIC_ROUTE_CARD_ID else card
            result_initial = self._classes[result_card["InitClassID"]]
            result_rarity = self._rarities[result_card["Rare"] + 1]
            if not result_initial["JobChange"] or not result_rarity["EvolutionConfig"]:
                raise ValueError("The resulting card has no valid first-awakening route")
            destination = self._classes[result_initial["JobChange"]]
            final_cap = min(_integer(destination["MaxLevel"], 1), _integer(result_rarity["LevelCap02"], 1))
            old_class = self._classes[selected["ClassID"]]
            cap = min(_integer(old_class["MaxLevel"], 1), _integer(rarity[f"LevelCap{stage:02}"], 1))
            if max(cap, final_cap) > len(self._experience) or result_card["ClassLV1SkillID"] >= len(self._skills):
                raise ValueError("Growth level or skill references are outside the resource tables")
            threshold = experience_threshold(self._experience[cap - 1], _integer(rarity["RequireExpRatio"], 1))
            changed = {"ClassID": destination["ClassID"], "ClassChangeCount": 2,
                "Exp": first_awake_experience(selected["Exp"], threshold, self.rules.first_awake_experience),
                "Love": max(selected["Love"], 100), "CostModMixValue": max(selected["CostModMixValue"], -result_card["CostDecValue"]),
                "SettingSkillID": result_card["ClassLV1SkillID"], DISPLAY_FIELD: 0}
            if result_card["CardID"] != card["CardID"]:
                changed["CardID"] = result_card["CardID"]
            return MappingProxyType(changed)
        except ProtocolError:
            raise
        except (AttributeError, ValueError, KeyError, TypeError, IndexError, OverflowError) as error:
            raise ProtocolError("shop_growth_state_invalid", "Growth target state or resource references are invalid", 503) from error

    def is_ready(self) -> bool:
        return caught(self._check_dependencies) is None

    def status(self):
        result = {"ready": True, "registration": REGISTRATION, "products": len(self._products),
                  "payment_item": PAYMENT_ITEM, "content_version": self.content_version,
                  "resource_sha256": dict(self._hashes), "rules": asdict(self.rules),
                  "rating_conditions_unavailable": sorted(self._rating_restricted), "target_stages": [0, 1],
                  "target_storage": "first_barracks", "native_tested": False,
                  "refusals": "Rejected before charging; the unit and balance stay unchanged while the native client "
                              "shows a communication error and resends the request"}
        try:
            self._check_dependencies()
        except ProtocolError as error:
            result.update(ready=False, error=error.code)
        return result
