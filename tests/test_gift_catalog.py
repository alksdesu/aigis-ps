from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.gift_catalog import GiftCatalog, _rarities
from test_quest_service import bundle


@pytest.fixture(scope="module")
def catalog(bundle):
    return GiftCatalog(bundle.masters.snapshot(), lambda name: bundle.native_cdn.assets.read(name)[0],
                       bundle.possessions, resource_version=bundle.bootstrap.asset_version)


def test_current_gifts_and_trust_rewards_are_all_retained(catalog):
    assert len(catalog.gifts) == len(catalog.rewards) == 20
    assert catalog.status()["cards"] == 2932
    assert len(catalog.status()["categories"]) == 5
    for gift in catalog.gifts:
        assert catalog.gift(gift.item_id) is gift
        assert catalog.gift(gift.item_hash) is gift
    assert catalog.gift("gift_0000_bouquet").add_value == 2
    assert catalog.gift("gift_0003_diamond").add_value == 5
    assert catalog.gift("gift_1003_millenniumWine").add_value == 5
    assert catalog.gift("gift_0110_ring").rarities == (100,)
    assert catalog.gift("gift_1201_emblem").rarities == (110,)


def test_unit_rewards_resolve_present_ids_instead_of_using_them_as_card_ids(catalog):
    for reward in catalog.rewards:
        if reward.reward_type == 1:
            present = catalog.present(int(reward.reward_id))
            assert present["CardID"] in (56, 57, 58, 59)
            assert catalog.card(present["CardID"])["CardID"] == present["CardID"]
            assert present["InitSkillID"] == -1
    assert {reward.condition for reward in catalog.rewards} == {1, 100, 150}


def test_catalog_results_cannot_mutate_shared_definitions(catalog):
    with pytest.raises(FrozenInstanceError):
        catalog.gifts[0].add_value = 900
    with pytest.raises(TypeError):
        catalog.card(56)["Rare"] = 900
    with pytest.raises(TypeError):
        catalog.present(2)["CardID"] = 900
    report = catalog.status()
    report["resource_sha256"].clear()
    assert len(catalog.status()["resource_sha256"]) == 3


@pytest.mark.parametrize("value", [True, [], {}, "missing", 0, 0x80000000])
def test_unknown_gift_keys_fail_explicitly(catalog, value):
    with pytest.raises(ProtocolError, match="trust gift"):
        catalog.gift(value)


@pytest.mark.parametrize("text,expected", [("-1,", (-1,)), ("4,10,", (4, 10)), ("5,11", (5, 11)), ("100,", (100,))])
def test_rarity_restrictions_preserve_wildcard_and_special_codes(text, expected):
    assert _rarities(text) == expected


@pytest.mark.parametrize("text", ["", "1,,2", "1,1", "-2,", "2147483648", "1, 2", "x", None])
def test_invalid_rarity_restrictions_are_not_silently_ignored(text):
    with pytest.raises(ValueError):
        _rarities(text)


@pytest.mark.parametrize("fault", ["effect_type", "rarity", "range", "item_hash", "duplicate_reward", "missing_present", "unknown_card", "float_card", "bool_card"])
def test_broken_linked_definitions_fail_catalog_creation(bundle, catalog, monkeypatch, fault):
    tables = deepcopy({name: catalog._table(name) for name in (
        "ItemConfigTable.atb", "PresentUnitData.atb", "TrustRewardConfigTable.atb")})
    gift = next(row for row in tables["ItemConfigTable.atb"] if row["EffectType"] == 4)
    reward = next(row for row in tables["TrustRewardConfigTable.atb"] if row["RewardType"] == 1)
    if fault == "effect_type":
        gift["EffectType"] = 4.0
    elif fault == "rarity":
        gift["UsageRestrictionInRareCSV"] = "bad"
    elif fault == "range":
        gift["TrustMaxScope"] = gift["TrustMinScope"]
    elif fault == "item_hash":
        gift["ID"] = 1
    elif fault == "duplicate_reward":
        tables["TrustRewardConfigTable.atb"].append(deepcopy(reward))
    elif fault == "missing_present":
        reward["RewardID"] = "2147483647"
    elif fault in ("unknown_card", "float_card", "bool_card"):
        value = {"unknown_card": 2147483647, "float_card": 56.0, "bool_card": True}[fault]
        next(row for row in tables["PresentUnitData.atb"] if row["PresentID"] == int(reward["RewardID"]))["CardID"] = value
    monkeypatch.setattr(GiftCatalog, "_table", lambda _self, name: tables[name])
    with pytest.raises(ProtocolError) as error:
        GiftCatalog(catalog.snapshot, lambda _: b"", bundle.possessions, resource_version=catalog.resource_version)
    assert error.value.code == "gift_catalog_invalid"


@pytest.mark.parametrize("payload", [b"bad", "not bytes"])
def test_malformed_resource_is_rejected(bundle, catalog, payload):
    with pytest.raises(ProtocolError) as error:
        GiftCatalog(catalog.snapshot, lambda _: payload, bundle.possessions, resource_version=catalog.resource_version)
    assert error.value.code == "gift_catalog_invalid"
