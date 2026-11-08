from copy import copy
from dataclasses import asdict
import json

import pytest

from aigis_ps.cli import load_item_ticket_rules
from aigis_ps.errors import ProtocolError
from aigis_ps.item_summon_tickets import DEFAULT_ITEM_TICKET_POOLS, ItemSummonTicketService
from aigis_ps.quest_mode_goddess import ITEM_REWARD_TYPE, reward_tiers
from aigis_ps.summon_tickets import SummonTicketRules
from test_item_summon_rules import RULE_ITEMS, SHARED_BASIC
from test_quest_service import bundle


FULL_PERCENT, PERCENT_TOLERANCE = 100, 0.001
GODDESS_POOL, GODDESS_CANDIDATES = 418, 494
GODDESS_LEVELS = {1, 10, 20, 30}
GODDESS_TIER_TICKETS = {"item_smn_ticket_19134", "item_smn_ticket_85125"}
# 428 lists the same candidates as 418, so an explicit 428 policy must still leave 418 closed.
GODDESS_TWIN_POOL = 428
EXPECTED_ITEMS = {
    "item_smn_ticket_19138": (428, 10), "item_smn_ticket_19144": (443, 10),
    "item_smn_ticket_19147": (448, 10), "item_smn_ticket_19151": (452, 10),
    "item_smn_ticket_19153": (454, 10), "item_smn_ticket_50027": (451, 1),
    "item_smn_ticket_50029": (470, 1), "item_smn_ticket_85127": (428, 1),
    "item_smn_ticket_85128": (428, 1), "item_smn_ticket_85131": (443, 1),
    "item_smn_ticket_85133": (448, 1), "item_smn_ticket_85136": (452, 1),
    "item_smn_ticket_85138": (454, 1), "item_smn_ticket_85142": (471, 1),
    "item_smn_ticket_19158": (463, 10), "item_smn_ticket_85140": (463, 1),
    "item_smn_ticket_19114": (346, 10), "item_smn_ticket_85107": (346, 1),
    "item_smn_ticket_19046": (194, 10), "item_smn_ticket_19048": (194, 10),
    "item_smn_ticket_19084": (275, 10), "item_smn_ticket_19133": (415, 10),
    "item_smn_ticket_19160": (472, 10), "item_smn_ticket_85050": (194, 1),
    "item_smn_ticket_85051": (194, 1), "item_smn_ticket_85052": (194, 1),
    "item_smn_ticket_85053": (194, 1), "item_smn_ticket_85083": (275, 1),
    "item_smn_ticket_85084": (275, 1), "item_smn_ticket_85122": (415, 1),
    "item_smn_ticket_85143": (472, 1), "item_smn_ticket_19134": (418, 10),
    "item_smn_ticket_85125": (418, 1), "item_smn_ticket_85126": (418, 1),
    "item_smn_ticket_19110": (338, 10), "item_smn_ticket_19113": (338, 10),
    "item_smn_ticket_85105": (338, 1), "item_smn_ticket_85106": (338, 1),
    "item_smn_ticket_40051": (341, 1), "item_smn_ticket_40052": (343, 1),
    "item_smn_ticket_40053": (343, 1), "item_smn_ticket_60005": (344, 1),
    "item_smn_ticket_40062": (426, 1), "item_smn_ticket_40072": (461, 1),
    "item_smn_ticket_19159": (464, 10), "item_smn_ticket_19161": (464, 10),
    "item_smn_ticket_85141": (464, 1), "item_smn_ticket_85144": (464, 1),
    **SHARED_BASIC,
}
# Pools opened by their native reward sources: LetsTD rows for the first six, event login 131 and the raid tier for 464.
SOURCED_POOLS = {338: (474, {1, 10, 20, 30}), 341: (51, {1}), 343: (161, {1}), 344: (196, {10}), 426: (72, {1}),
                 461: (80, {1}), 464: (508, {1})}
GODDESS_ITEMS = {name: (pool_id, count) for name, (pool_id, count) in EXPECTED_ITEMS.items() if pool_id == GODDESS_POOL}


@pytest.fixture(scope="module")
def default_service(bundle):
    return ItemSummonTicketService(bundle.bootstrap, bundle.player_state, bundle.presents,
        bundle.summon_pools, bundle.item_summon_tickets.catalog)


def test_default_rules_and_cli_use_the_same_verified_pools(default_service):
    assert DEFAULT_ITEM_TICKET_POOLS == (194, 212, 218, 248, 253, 275, 300, 325, 334, 335, 338, 340, 341, 343, 344,
                                         346, 364, 365, 367, 375, 376, 386, 403, 415, 416, 417, 418, 419, 420, 423,
                                         426, 428, 443, 448, 451, 452, 454, 460, 461, 463, 464, 470, 471, 472)
    assert default_service.rules.enabled_pools == DEFAULT_ITEM_TICKET_POOLS
    assert load_item_ticket_rules(None).enabled_pools == DEFAULT_ITEM_TICKET_POOLS


def test_all_new_pool_candidates_have_complete_cached_initializations(default_service):
    expected = {443: 88, 448: 137, 451: 68, 452: 320, 454: 87, 470: 18, 471: 86}
    grants = default_service.grants
    assert set(grants.validated_pools) == set(DEFAULT_ITEM_TICKET_POOLS)
    for pool_id, count in expected.items():
        pool = grants.validated_pools[pool_id]
        initial = grants.initial_values[pool_id]
        assert pool.complete and len(pool.candidates) == count == len(initial)
        assert abs(pool.percent_sum - FULL_PERCENT) < PERCENT_TOLERANCE
        assert all(candidate.evolution_count == 0 and candidate.level in (0, 1) for candidate in pool.candidates)
        assert all(value.class_id > 0 and value.stage in (0, 2) and value.skill_id >= 0
                   and value.experience >= 0 for value in initial)


def test_pool_471_zero_level_metadata_stays_zero_and_initializes_without_experience(default_service):
    grants = default_service.grants
    pool = grants.validated_pools[471]
    zero = [index for index, candidate in enumerate(pool.candidates) if candidate.level == 0]
    assert len(zero) == 35
    assert all(grants.initial_values[471][index].experience == 0 for index in zero)
    assert all(pool.candidates[index].level == 0 for index in zero)


def test_goddess_reward_pool_is_complete_and_every_candidate_initializes(default_service):
    pool = default_service.grants.validated_pools[GODDESS_POOL]
    initial = default_service.grants.initial_values[GODDESS_POOL]
    assert pool.complete and len(pool.candidates) == len(initial) == GODDESS_CANDIDATES
    assert abs(pool.percent_sum - FULL_PERCENT) < PERCENT_TOLERANCE
    assert {candidate.level for candidate in pool.candidates} == GODDESS_LEVELS
    assert all(candidate.evolution_count == 0 and candidate.weight > 0 for candidate in pool.candidates)
    assert all(value.class_id > 0 and value.skill_id >= 0 and value.experience >= 0 for value in initial)
    for item_id, expected in GODDESS_ITEMS.items():
        definition = default_service.validate_item(item_id)
        assert (definition.pool_id, definition.count) == expected


@pytest.mark.parametrize("pool_id", sorted(SOURCED_POOLS))
def test_sourced_pools_are_complete_and_every_candidate_initializes(default_service, pool_id):
    count, levels = SOURCED_POOLS[pool_id]
    pool = default_service.grants.validated_pools[pool_id]
    initial = default_service.grants.initial_values[pool_id]
    assert pool.complete and len(pool.candidates) == len(initial) == count
    assert abs(pool.percent_sum - FULL_PERCENT) < PERCENT_TOLERANCE
    assert {candidate.level for candidate in pool.candidates} == levels
    assert all(candidate.evolution_count == 0 and candidate.weight > 0 for candidate in pool.candidates)
    assert all(value.class_id > 0 and value.skill_id >= 0 and value.experience >= 0 for value in initial)
    assert {item_id for item_id, (owner, _) in EXPECTED_ITEMS.items() if owner == pool_id}


@pytest.mark.parametrize("enabled", [(), (463,), (194, 418)])
def test_explicit_pool_policy_does_not_gain_sourced_pools(bundle, enabled):
    service = ItemSummonTicketService(bundle.bootstrap, bundle.player_state, bundle.presents,
        bundle.summon_pools, bundle.item_summon_tickets.catalog, rules=SummonTicketRules(enabled))
    for item_id in (name for name, (pool_id, _) in EXPECTED_ITEMS.items() if pool_id in SOURCED_POOLS):
        with pytest.raises(ProtocolError) as caught:
            service.validate_item(item_id)
        assert caught.value.code == "summon_ticket_pool_disabled"


def test_goddess_total_star_tickets_draw_from_the_goddess_reward_pool(bundle, default_service):
    tickets = {tier.item_id for tier in reward_tiers(bundle.quests.catalog)
               if tier.reward_type == ITEM_REWARD_TYPE and default_service.catalog.supports_item(tier.item_id)}
    assert tickets == GODDESS_TIER_TICKETS
    assert {default_service.validate_item(item_id).pool_id for item_id in tickets} == {GODDESS_POOL}


@pytest.mark.parametrize("enabled", [(), (GODDESS_TWIN_POOL,)])
def test_explicit_pool_policy_does_not_gain_goddess_reward_pool(bundle, enabled):
    service = ItemSummonTicketService(bundle.bootstrap, bundle.player_state, bundle.presents,
        bundle.summon_pools, bundle.item_summon_tickets.catalog, rules=SummonTicketRules(enabled))
    assert service.rules.enabled_pools == enabled
    for item_id in GODDESS_ITEMS:
        with pytest.raises(ProtocolError) as caught:
            service.validate_item(item_id)
        assert caught.value.code == "summon_ticket_pool_disabled"


@pytest.mark.parametrize("item_id,expected", EXPECTED_ITEMS.items())
def test_all_default_item_shapes_are_known_and_validate(default_service, item_id, expected):
    definition = default_service.validate_item(item_id)
    assert (definition.pool_id, definition.count) == expected
    assert definition.unsupported_reason is None
    row = default_service.catalog.source_rows[item_id]
    assert {name: row[name] for name in ("EffectType", "Stack", "CanPresent", "InvisibleContaint",
        "CanAllGet", "NoAutoDelete", "ReceiveAction", "LotteryRuleID", "EventID")} == {
        "EffectType": 2, "Stack": 0, "CanPresent": 1, "InvisibleContaint": 1, "CanAllGet": 0,
        "NoAutoDelete": 1, "ReceiveAction": 0, "LotteryRuleID": 0, "EventID": ""}


@pytest.mark.parametrize("item_id,expected", RULE_ITEMS.items())
def test_default_rule_tickets_keep_the_basic_mail_shape_and_validate(default_service, item_id, expected):
    definition = default_service.validate_item(item_id)
    assert (definition.pool_id, definition.count, definition.rule.rule_id) == expected
    row = default_service.catalog.source_rows[item_id]
    assert {name: row[name] for name in ("EffectType", "Stack", "CanPresent", "InvisibleContaint", "CanAllGet",
        "NoAutoDelete", "ReceiveAction", "LotteryRuleID", "EventID")} == {
        "EffectType": 2, "Stack": 0, "CanPresent": 1, "InvisibleContaint": 1, "CanAllGet": 0,
        "NoAutoDelete": 1, "ReceiveAction": 0, "LotteryRuleID": expected[2], "EventID": ""}


def test_status_lists_exact_default_items(default_service):
    status = default_service.status()
    assert status["ready"] is True
    assert set(status["enabled_items"]) == set(EXPECTED_ITEMS) | set(RULE_ITEMS)


@pytest.mark.parametrize("enabled", [[], [428], [471], [471, 443]])
def test_explicit_json_configuration_does_not_merge_recommended_defaults(bundle, tmp_path, enabled):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"enabled_pools": enabled}), encoding="utf8")
    rules = load_item_ticket_rules(path)
    assert rules.enabled_pools == tuple(sorted(enabled))
    service = ItemSummonTicketService(bundle.bootstrap, bundle.player_state, bundle.presents,
        bundle.summon_pools, bundle.item_summon_tickets.catalog, rules=rules)
    assert service.rules == rules
    assert set(service.status()["enabled_items"]) == {
        name for name, (pool_id, _) in EXPECTED_ITEMS.items() if pool_id in enabled}
    if 448 not in enabled:
        with pytest.raises(ProtocolError) as error:
            service.validate_item("item_smn_ticket_19147")
        assert error.value.code == "summon_ticket_pool_disabled"


def test_expanded_defaults_change_receipt_identity_without_mutating_explicit_rules(bundle, default_service):
    rules = SummonTicketRules((428,))
    before = asdict(rules)
    explicit = ItemSummonTicketService(bundle.bootstrap, bundle.player_state, bundle.presents,
        bundle.summon_pools, bundle.item_summon_tickets.catalog, rules=rules)
    assert explicit.content_version != default_service.content_version
    assert asdict(rules) == before


def test_constructor_has_no_present_or_player_state_mutation(bundle):
    presents = copy(bundle.presents)
    sentinel = object()
    presents.item_tickets = sentinel
    ItemSummonTicketService(bundle.bootstrap, bundle.player_state, presents,
        bundle.summon_pools, bundle.item_summon_tickets.catalog)
    assert presents.item_tickets is sentinel
