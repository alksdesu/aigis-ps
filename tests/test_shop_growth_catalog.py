from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path

import pytest

from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.evolution_catalog import EvolutionRules
from aigis_ps.player_state import PRINCE_CARD_IDS, UNIT_BOOLEANS, UNIT_FIELDS
from aigis_ps.possessions import PossessionService
from aigis_ps.shop_growth import (
    MENU_CONDITION, MENU_NAME, PAYMENT_HASH, PAYMENT_ITEM, RARITY_FIELDS, ShopGrowthCatalog,
)
from aigis_ps.system_literals import encode_command
from aigis_ps.trade_catalog import TradeCatalog
from aigis_ps.training_math import experience_threshold
from test_public_tables import digested
from test_quest_service import bundle
from test_trade_catalog import name_hash, native_date, product, resources, table


NOW = datetime(2026, 11, 1, tzinfo=JST)
OUTPUT_FIELDS = {"ClassID", "ClassChangeCount", "Exp", "Love", "CostModMixValue", "SettingSkillID", "_DisplayType"}


def setup_case(*, products=None, menu=None, rating=None):
    rows = {identifier: {"CardID": identifier, "Rare": rarity, "InitClassID": 100,
                        "ClassLV1SkillID": 2, "CostDecValue": 3, "ClassLV0SkillID": 1}
            for identifier, rarity in ((1, 5), (7, 3), (8, 4), (9, 5), (10, 7), (11, 10), (12, 11), (80, 5), (99, 5), (309, 5))}
    rows[80].update(InitClassID=700, ClassLV1SkillID=47)
    rows[99].update(InitClassID=710, ClassLV1SkillID=72, CostDecValue=0)
    cards = SimpleNamespace(card_ids=tuple(rows), card=rows.__getitem__, rows=rows,
        resource_version="fixture-v1", snapshot=SimpleNamespace(identifier="fixture", client_hash="a" * 64,
                                                               get_xml=lambda _: b"frozen-card-master"))
    classes = {100: {"ClassID": 100, "MaxLevel": 5, "JobChange": 120},
               120: {"ClassID": 120, "MaxLevel": 5, "JobChange": 0},
               700: {"ClassID": 700, "MaxLevel": 2, "JobChange": 710},
               710: {"ClassID": 710, "MaxLevel": 4, "JobChange": 730},
               730: {"ClassID": 730, "MaxLevel": 5, "JobChange": 0}}
    rarities = {index + 1: {"ConfigID": index + 1, "EvolutionConfig": 1, "RequireExpRatio": 100,
                          "LevelCap00": 3, "LevelCap01": 4, "LevelCap02": 5} for index in range(12)}
    experience = (0, 100, 400, 1000, 1600)
    skills = [{"LevelMax": 0}, *({"LevelMax": 5} for _ in range(72))]
    growth = SimpleNamespace(cards=cards, classes=classes, rarities=rarities, experience=experience,
                             skills=skills, content_version="b" * 64)
    selected = products if products is not None else [
        product(identifier, CardID=0, ShopType=2, ProductType=6, Type=9, TargetRarity=target,
                RequiredValue=price, ConsumeItemID=PAYMENT_ITEM)
        for identifier, target, price in ((2000, "5,11", 6), (2001, "4,10", 4), (2003, "7", 4), (2004, "3", 2))]
    payloads = resources(selected, stacks={PAYMENT_ITEM: 9999, "coin": 1000, "reward": 99},
                         inventory=[{"ConfigID": 3001, "ShopType": 2, "TradeType": 9, "ConsumeItemID": PAYMENT_HASH}])
    payloads["ExperienceTable.atb"] = table([{"Level": index + 1, "Exp": value} for index, value in enumerate(experience)],
                                             {"Level": 1, "Exp": 1})
    payloads["cfgRarity.atb"] = table(list(rarities.values()), {name: 1 for name in RARITY_FIELDS})
    payloads["SkillList.atb"] = table(skills, {"LevelMax": 1})
    payloads["OnCommandMenuUnitGrowthSelect.atb"] = table(
        [{"_ID": name_hash(MENU_NAME), "_Cmnd": encode_command(MENU_CONDITION)}] if menu is None else menu,
        {"_ID": 1, "_Cmnd": 32})
    payloads["OnCommand.atb"] = table(rating or [], {"_ID": 1, "_Cmnd": 32})
    possessions = PossessionService(Path("."), asset_reader=payloads.__getitem__)
    base = TradeCatalog(cards, possessions, payloads.__getitem__, resource_version=cards.resource_version)
    return SimpleNamespace(cards=cards, growth=growth, payloads=payloads, possessions=possessions, base=base)


def create(case, policy="overflow"):
    return ShopGrowthCatalog(case.base, case.growth, case.payloads.__getitem__,
                             rules=EvolutionRules(first_awake_experience=policy))


def unit(card_id=7, *, index=1, unique=2):
    result = {name: False if name in UNIT_BOOLEANS else 0 for name, _ in UNIT_FIELDS}
    result.update(CardID=card_id, ClassID=100, GetIndex=index, UniqueID=unique,
                  GeneralPurposeFlags=1, SkillLevel=1, SettingSkillID=1, OrganizationIndex=-1)
    return result


def state(target=None):
    return {"bootstrap_schema": 1, "tables": {"possessions": [{"ItemID": PAYMENT_ITEM, "Count": 100}],
            "game_param": [{"Money": 1000}], "quest_records": []},
            "player_state": {"schema_version": 1, "units": [unit() if target is None else target],
                "squads": [], "battle": {"VN": "1.0.1", "UX": 1, "TN": 0, "RTN": 64},
                "legacy_squad_imported": True}}


def values(subject, saved=None, *, shop_id=2004, get_index=1):
    saved = state() if saved is None else saved
    return subject.values(saved["player_state"], subject.product(shop_id), get_index, state=saved)


def fails(code, action, status=None):
    with pytest.raises(ProtocolError) as error:
        action()
    assert error.value.code == code
    if status is not None:
        assert error.value.status == status


def test_four_products_have_independent_registration_and_preserve_base():
    case = setup_case()
    before = case.base.products, case.base.content_version
    subject = create(case)
    assert subject.base is case.base and subject.growth is case.growth
    assert tuple(item.shop_id for item in subject.products) == (2000, 2001, 2003, 2004)
    for selected in subject.products:
        assert selected.registration == 113 and not selected.ordinary
        assert case.base.product(selected.shop_id).registration is None
    assert (case.base.products, case.base.content_version) == before
    with pytest.raises(FrozenInstanceError):
        subject.product(2004).registration = 7


@pytest.mark.parametrize("shop_id,card_id,price", [(2000, 9, 6), (2000, 12, 6), (2001, 8, 4),
                                                   (2001, 11, 4), (2003, 10, 4), (2004, 7, 2)])
def test_each_product_uses_its_original_rarity_and_price(shop_id, card_id, price):
    subject, saved = create(setup_case()), state(unit(card_id))
    before = deepcopy(saved)
    quote = subject.quote(saved, shop_id, 1, 0, NOW)
    assert quote.total_price == price and quote.currency_item_id == PAYMENT_ITEM
    assert quote.registration == 113 and quote.quantity == 1
    changed = values(subject, saved, shop_id=shop_id)
    assert changed == {"ClassID": 120, "ClassChangeCount": 2, "Exp": 0, "Love": 100,
                       "CostModMixValue": 0, "SettingSkillID": 2, "_DisplayType": 0}
    assert saved == before
    with pytest.raises(TypeError):
        changed["Exp"] = 5


@pytest.mark.parametrize("quantity", [True, False, 0, 2, -1, 0x80000000, 1.0, "1", None])
def test_quantity_is_exactly_one(quantity):
    subject = create(setup_case())
    fails("shop_growth_quantity_invalid", lambda: subject.quote(state(), 2004, quantity, 0, NOW), 400)


@pytest.mark.parametrize("registration", [True, 7, 112, None])
def test_wrong_registration_is_rejected(registration):
    subject = create(setup_case())
    fails("trade_registration_mismatch", lambda: subject.quote(state(), 2004, 1, 0, NOW, registration), 400)


def test_quote_uses_catalog_tiers_and_balance_without_mutation():
    case = setup_case(products=[product(2004, CardID=0, ShopType=2, ProductType=6, Type=9, TargetRarity="3",
        RequiredValue=17, ConsumeItemID=PAYMENT_ITEM), product(2004, CardID=0, ShopType=2, ProductType=6,
        Type=9, TargetRarity="3", RequiredValue=23, TradeCount=3, ConsumeItemID=PAYMENT_ITEM)])
    subject, saved = create(case), state()
    assert subject.quote(saved, 2004, 1, 0, NOW).total_price == 17
    assert subject.quote(saved, 2004, 1, 1, NOW).total_price == 23
    saved["tables"]["possessions"][0]["Count"] = 22
    before = deepcopy(saved)
    fails("trade_currency_insufficient", lambda: subject.quote(saved, 2004, 1, 1, NOW), 409)
    assert saved == before


def test_quote_respects_resource_window_disclosure_and_limit():
    row = product(2004, CardID=0, ShopType=2, ProductType=6, Type=9, TargetRarity="3",
        RequiredValue=2, ConsumeItemID=PAYMENT_ITEM, StartDateTime=native_date(NOW),
        EndDateTime=native_date(datetime(2026, 12, 1, tzinfo=JST)), Data_Restrict=1, DisclosureQuestIdList="1001")
    subject, saved = create(setup_case(products=[row])), state()
    fails("trade_quest_required", lambda: subject.quote(saved, 2004, 1, 0, NOW), 409)
    saved["tables"]["quest_records"] = [{"QuestID": 1001, "AchieveFlag": 0x01000000}]
    assert subject.quote(saved, 2004, 1, 0, NOW).total_price == 2
    fails("trade_limit_reached", lambda: subject.quote(saved, 2004, 1, 1, NOW), 409)
    fails("trade_product_closed", lambda: subject.quote(saved, 2004, 1, 0, datetime(2026, 12, 1, tzinfo=JST)), 409)


@pytest.mark.parametrize("stage,experience,expected", [(0, 0, 0), (0, 399, 0), (0, 400, 0), (0, 401, 1),
                                                     (1, 999, 0), (1, 1000, 0), (1, 1023, 23)])
def test_old_stage_selects_its_own_cap_for_overflow(stage, experience, expected):
    target = unit()
    target.update(ClassChangeCount=stage, Exp=experience)
    assert values(create(setup_case()), state(target))["Exp"] == expected


@pytest.mark.parametrize("policy,expected", [("overflow", 100), ("total", 500), ("reset", 0)])
def test_explicit_existing_evolution_policies_are_shared(policy, expected):
    target = unit()
    target["Exp"] = 500
    assert values(create(setup_case(), policy), state(target))["Exp"] == expected


@pytest.mark.parametrize("stage,old_class,cap_exp", [(0, 700, 100), (1, 710, 1000)])
def test_card80_replacement_uses_new_skill_cost_and_old_experience_cap(stage, old_class, cap_exp):
    target = unit(80)
    target.update(ClassID=old_class, ClassChangeCount=stage, Exp=cap_exp + 23, SettingSkillID=47,
                  CostModMixValue=-3, SkillLevel=4, GeneralPurposeFlags=3, WatchLoveEv=2,
                  SkillExperience=8, CostExperience=7)
    saved = state(target)
    before = deepcopy(saved)
    changed = values(create(setup_case()), saved, shop_id=2000)
    assert changed == {"CardID": 99, "ClassID": 730, "ClassChangeCount": 2, "Exp": 23, "Love": 100,
                       "CostModMixValue": 0, "SettingSkillID": 72, "_DisplayType": 0}
    assert saved == before
    assert set(changed) == OUTPUT_FIELDS | {"CardID"}


def test_card80_old_stage_one_requires_the_native_intermediate_class():
    target = unit(80)
    target.update(ClassID=700, ClassChangeCount=1)
    subject = create(setup_case())
    fails("shop_growth_target_ineligible", lambda: values(subject, state(target), shop_id=2000), 409)


@pytest.mark.parametrize("old_cost,expected", [(-9, -3), (-3, -3), (-1, -1), (0, 0), (5, 5)])
def test_cost_clamp_is_not_a_free_full_reduction(old_cost, expected):
    target = unit()
    target["CostModMixValue"] = old_cost
    assert values(create(setup_case()), state(target))["CostModMixValue"] == expected


def test_growth_ignores_training_only_restrictions_and_preserves_other_fields():
    case = setup_case()
    case.growth.describe = lambda *_: pytest.fail("Training target restrictions must not run")
    case.growth.aliases = SimpleNamespace(resolve=lambda *_: pytest.fail("The original card must be used"))
    case.growth._abilities = {123: [{"InfluenceType": 214}]}
    target = unit()
    target.update(Love=150, SkillLevel=0, GeneralPurposeFlags=-0x80000000 | 7,
        WatchLoveEv=3, SkillEvolved=1, SkillExperience=11, CostExperience=13,
        OrganizationIndex=4, OrganizationIndex2=8, ClassChangeStatusMod=9, _DisplayType=22)
    saved = state(target)
    saved["player_state"]["squads"] = [{"UnitIndex": target["GetIndex"], "Address": 0}]
    before = deepcopy(saved)
    changed = values(create(case), saved)
    assert set(changed) == OUTPUT_FIELDS and changed["Love"] == 150
    assert all({**target, **changed}[key] == value for key, value in target.items() if key not in OUTPUT_FIELDS)
    assert saved == before


@pytest.mark.parametrize("changes", [{"CardID": 1}, {"CardID": 309}, {"CardID": 8},
                                      {"ClassChangeCount": 2}, {"ClassChangeCount": 4}, {"ClassID": 120}])
def test_ineligible_target_does_not_become_a_growth_purchase(changes):
    target = unit()
    target.update(changes)
    subject = create(setup_case())
    fails("shop_growth_target_ineligible", lambda: values(subject, state(target)), 409)


@pytest.mark.parametrize("card_id", sorted(PRINCE_CARD_IDS))
def test_princes_are_refused_even_when_their_rarity_and_route_qualify(card_id):
    subject = create(setup_case())
    values(subject, state(unit(9)), shop_id=2000)
    fails("shop_growth_target_ineligible", lambda: values(subject, state(unit(card_id)), shop_id=2000), 409)


@pytest.mark.parametrize("flags", [16, 32, 0xF0, -0x80000000 | 16])
def test_unverified_extra_barracks_targets_are_explicit(flags):
    target = unit()
    target["GeneralPurposeFlags"] = flags
    subject = create(setup_case())
    fails("shop_growth_storage_unsupported", lambda: values(subject, state(target)), 501)


@pytest.mark.parametrize("index", [True, -1, 0x80000000, "1", None])
def test_get_index_requires_native_identity(index):
    subject = create(setup_case())
    fails("shop_growth_identity_invalid", lambda: values(subject, get_index=index), 400)


def test_get_index_selects_one_instance_and_missing_target_is_explicit():
    subject, saved = create(setup_case()), state()
    another = unit(index=2, unique=3)
    another["Exp"] = 450
    saved["player_state"]["units"].append(another)
    assert values(subject, saved, get_index=2)["Exp"] == 50
    assert values(subject, saved)["Exp"] == 0
    fails("shop_growth_target_missing", lambda: values(subject, saved, get_index=3), 409)


@pytest.mark.parametrize("change", [lambda s: s["player_state"].update(schema_version=True),
    lambda s: s["player_state"].update(units=[]),
    lambda s: s["player_state"]["units"].append(deepcopy(s["player_state"]["units"][0])),
    lambda s: s["player_state"]["units"][0].update(UniqueID=0),
    lambda s: s["player_state"]["units"][0].update(GeneralPurposeFlags=True),
    lambda s: s["player_state"]["units"][0].update(Exp=-1),
    lambda s: s["player_state"]["units"][0].update(CardID=999),
    lambda s: s["player_state"]["units"][0].update(ClassID=999),
    lambda s: s["player_state"]["units"][0].update(Unverified=3),
    lambda s: s.update(unit_dynamic_overrides="invalid")])
def test_invalid_state_is_rejected_without_mutation(change):
    subject, saved = create(setup_case()), state()
    change(saved)
    before = deepcopy(saved)
    fails("shop_growth_state_invalid", lambda: values(subject, saved), 503)
    assert saved == before


@pytest.mark.parametrize("overrides", [{"target": 1}, [1]])
def test_dynamic_overrides_are_not_treated_as_ordinary_targets(overrides):
    subject, saved = create(setup_case()), state()
    saved["unit_dynamic_overrides"] = overrides
    before = deepcopy(saved)
    fails("shop_growth_overrides_unsupported", lambda: values(subject, saved), 501)
    assert saved == before


@pytest.mark.parametrize("overrides", [None, {}, []])
def test_absent_or_empty_dynamic_overrides_keep_the_ordinary_target(overrides):
    subject, saved = create(setup_case()), state()
    saved["unit_dynamic_overrides"] = overrides
    assert values(subject, saved) == values(subject)


@pytest.mark.parametrize("text", ["", " \n\t"])
def test_missing_or_empty_rating_commands_allow_current_target(text):
    subject = create(setup_case(rating=[{"_ID": name_hash("IsCanUseRating_7"), "_Cmnd": encode_command(text)}]))
    assert values(subject)["ClassID"] == 120


@pytest.mark.parametrize("encoded", [encode_command("1;"), encode_command("0;"), "not-hex"])
def test_unknown_rating_only_disables_its_target(encoded):
    subject = create(setup_case(rating=[{"_ID": name_hash("IsCanUseRating_7"), "_Cmnd": encoded}]))
    assert subject.status()["rating_conditions_unavailable"] == [7]
    fails("shop_growth_rating_unsupported", lambda: values(subject), 501)
    assert values(subject, state(unit(8)), shop_id=2001)["ClassID"] == 120


@pytest.mark.parametrize("rows", [[], [{"_ID": name_hash(MENU_NAME), "_Cmnd": encode_command("1;")}],
    [{"_ID": name_hash(MENU_NAME), "_Cmnd": encode_command(MENU_CONDITION.replace("HasFirst", "Has First"))}],
    [{"_ID": name_hash(MENU_NAME), "_Cmnd": encode_command(MENU_CONDITION)}] * 2])
def test_missing_changed_or_ambiguous_menu_condition_fails_closed(rows):
    case = setup_case(menu=rows)
    fails("shop_growth_catalog_invalid", lambda: create(case), 503)


def test_whitespace_between_menu_tokens_is_allowed():
    text = "\nHasFirstEvolveClass ( )&& ( GetClassChange ( ) < 2 ) && ! IsPrince ( ) ;\t"
    subject = create(setup_case(menu=[{"_ID": name_hash(MENU_NAME), "_Cmnd": encode_command(text)}]))
    assert values(subject)["ClassChangeCount"] == 2


def test_forged_product_does_not_select_or_price_an_owned_target():
    subject, saved = create(setup_case()), state()
    forged = replace(subject.product(2004), target_rarity="5", tiers=((1, 0),))
    fails("shop_growth_product_invalid", lambda: subject.values(saved["player_state"], forged, 1, state=saved), 400)


@pytest.mark.parametrize("change", [lambda c: c.cards.rows[7].update(CostDecValue=8),
    lambda c: c.growth.classes[100].update(MaxLevel=4),
    lambda c: c.growth.rarities[4].update(LevelCap00=2),
    lambda c: setattr(c.growth, "experience", (0, 200, 400, 1000, 1600)),
    lambda c: c.growth.skills[2].update(LevelMax=6),
    lambda c: setattr(c.base, "content_version", "c" * 64),
    lambda c: setattr(c.cards, "resource_version", "changed"),
    lambda c: setattr(c.cards.snapshot, "get_xml", lambda _: b"different-master")])
def test_live_dependency_drift_is_not_silently_used(change):
    case = setup_case()
    subject = create(case)
    change(case)
    assert subject.status()["ready"] is False
    fails("shop_growth_content_changed", lambda: values(subject), 503)


@pytest.mark.parametrize("fault", [None, "base_hash", "base_unready", "rules"])
def test_readiness_matches_status_without_building_it(monkeypatch, fault):
    case = setup_case()
    subject = create(case)
    if fault == "base_hash":
        case.base.content_version = "c" * 64
    elif fault == "base_unready":
        case.possessions.issues["item_definitions"] = "Item definitions changed after setup"
    elif fault == "rules":
        subject.rules = EvolutionRules(first_awake_experience="total")
    ready = subject.status()["ready"]
    assert ready is (fault is None)
    for provider in (subject, case.base, case.possessions):
        monkeypatch.setattr(provider, "status", lambda: pytest.fail("Readiness built a full status"))
    assert subject.is_ready() is ready


def test_card_master_is_digested_once_per_bytes_object(digested):
    case = setup_case()
    subject = create(case)
    verified = case.cards.snapshot.get_xml("CardData")
    sources = {"equal": bytes(bytearray(verified)), "changed": verified + b"changed", "verified": verified}

    def names():
        return [name for data in digested for name, raw in sources.items() if data is raw]
    for _ in range(2):
        assert subject.is_ready() is subject.status()["ready"] is True
    assert names() == ["verified"]
    case.cards.snapshot.get_xml = lambda _: sources["equal"]
    for _ in range(2):
        assert subject.product(2004).shop_id == 2004
    assert names() == ["verified", "equal"]
    case.cards.snapshot.get_xml = lambda _: sources["changed"]
    for _ in range(2):
        fails("shop_growth_content_changed", lambda: subject.product(2004), 503)
    assert names() == ["verified", "equal", "changed"]
    case.cards.snapshot.get_xml = lambda _: verified
    assert subject.status()["ready"] is True and names() == ["verified", "equal", "changed", "verified"]


def test_card_master_subclass_is_checked_on_every_readiness_check():
    case = setup_case()
    subject = create(case)
    verified = case.cards.snapshot.get_xml("CardData")

    class Master(bytes):
        def __buffer__(self, flags):
            return memoryview(self.shown)
    master = Master(verified)
    master.shown = verified
    case.cards.snapshot.get_xml = lambda _: master
    assert subject.status()["ready"] is True
    master.shown = verified + b"changed"
    assert subject.status()["ready"] is False
    fails("shop_growth_content_changed", lambda: subject.product(2004), 503)


def test_frozen_assets_and_status_are_not_mutable_backdoors():
    case = setup_case()
    subject = create(case)
    before = subject.status()
    case.payloads["OnCommand.atb"] = b"changed after construction"
    status = subject.status()
    status["resource_sha256"].clear()
    status["rating_conditions_unavailable"].append(7)
    assert subject.status() == before
    assert values(subject)["ClassID"] == 120
    with pytest.raises(TypeError):
        subject._cards[7]["CostDecValue"] = 10


def test_rules_cannot_drift_under_the_same_content_identity():
    subject = create(setup_case())
    subject.rules = EvolutionRules(first_awake_experience="total")
    assert subject.status()["ready"] is False
    fails("shop_growth_content_changed", lambda: values(subject), 503)


@pytest.mark.parametrize("source", ["ExperienceTable.atb", "cfgRarity.atb", "SkillList.atb"])
def test_reader_must_match_growth_resource_snapshot(source):
    case = setup_case()
    case.payloads[source] = b"invalid"
    fails("shop_growth_catalog_invalid", lambda: create(case), 503)


def test_absent_destination_and_skill_references_are_not_repaired():
    case = setup_case()
    case.growth.classes[100]["JobChange"] = 999
    subject = create(case)
    fails("shop_growth_state_invalid", lambda: values(subject), 503)
    case = setup_case()
    case.cards.rows[7]["ClassLV1SkillID"] = 999
    subject = create(case)
    fails("shop_growth_state_invalid", lambda: values(subject), 503)


@pytest.mark.parametrize("field,value", [("MaxLevel", 0), ("MaxLevel", 999)])
def test_destination_level_cap_must_remain_representable(field, value):
    case = setup_case()
    case.growth.classes[120][field] = value
    if value > 5:
        case.growth.rarities[4]["LevelCap02"] = value
        case.payloads["cfgRarity.atb"] = table(list(case.growth.rarities.values()), {name: 1 for name in RARITY_FIELDS})
    subject = create(case)
    fails("shop_growth_state_invalid", lambda: values(subject), 503)


@pytest.fixture(scope="module")
def current(bundle):
    reader = lambda name: bundle.native_cdn.assets.read(name)[0]
    base = TradeCatalog(bundle.gifts.catalog, bundle.possessions, reader, resource_version=bundle.native_cdn.version)
    return ShopGrowthCatalog(base, bundle.training.catalog, reader)


def test_current_four_products_cover_six_rarities_without_training_target_rules(current):
    assert {row.shop_id for row in current.products} == {2000, 2001, 2003, 2004}
    checked = []
    for product in current.products:
        for rare in current._targets[product.shop_id]:
            card = next(row for row in current._cards.values() if row["Rare"] == rare and row["CardID"] not in (1, 80, 309)
                and row["CardID"] not in current._rating_restricted
                and current._classes[row["InitClassID"]]["JobChange"] and current._rarities[rare + 1]["EvolutionConfig"])
            target = unit(card["CardID"])
            target.update(ClassID=card["InitClassID"], GeneralPurposeFlags=3, Love=0, Exp=0)
            saved = state(target)
            changed = current.values(saved["player_state"], product, 1, state=saved)
            assert changed["ClassID"] == current._classes[card["InitClassID"]]["JobChange"]
            assert changed["Love"] == 100 and changed["Exp"] == 0
            assert changed["SettingSkillID"] == card["ClassLV1SkillID"]
            assert set(changed) == OUTPUT_FIELDS
            assert current.quote(saved, product.shop_id, 1, 0, NOW).total_price == product.tiers[0][1]
            checked.append(rare)
    assert sorted(checked) == [3, 4, 5, 7, 10, 11]


def test_native_threshold_uses_current_rarity_float32_formula(current):
    card = current._cards[7]
    target = unit(7)
    target["ClassID"] = card["InitClassID"]
    target["ClassChangeCount"] = 1
    rarity = current._rarities[card["Rare"] + 1]
    cap = min(current._classes[target["ClassID"]]["MaxLevel"], rarity["LevelCap01"])
    target["Exp"] = experience_threshold(current._experience[cap - 1], rarity["RequireExpRatio"]) + 19
    assert values(current, state(target))["Exp"] == 19


@pytest.mark.parametrize("stage", [0, 1])
def test_current_card80_is_transformed_without_allocating_a_new_instance(current, stage):
    card = current._cards[80]
    target = unit(80)
    target.update(ClassID=700 if stage == 0 else 710, ClassChangeCount=stage, SettingSkillID=47,
                  CostModMixValue=-3, Love=80)
    rarity = current._rarities[card["Rare"] + 1]
    cap = min(current._classes[target["ClassID"]]["MaxLevel"], rarity[f"LevelCap{stage:02}"])
    target["Exp"] = experience_threshold(current._experience[cap - 1], rarity["RequireExpRatio"]) + 37
    saved = state(target)
    changed = values(current, saved, shop_id=2000)
    assert changed["CardID"] == 99 and changed["ClassID"] == 730
    assert changed["SettingSkillID"] == 72 and changed["CostModMixValue"] == 0
    assert changed["Exp"] == 37 and changed["Love"] == 100
    assert "GetIndex" not in changed and "UniqueID" not in changed
