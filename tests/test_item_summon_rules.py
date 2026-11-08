from copy import copy, deepcopy
from dataclasses import replace
import logging
from types import MappingProxyType

import pytest

from aigis_ps.alxml import loads
from aigis_ps.barracks_service import EXTRA_SHIFT, PURCHASE_FIELD
from aigis_ps.errors import ProtocolError
from aigis_ps.item_summon_catalog import ENSURED_RULE, UNOWNED_RULE, LotteryRule
import aigis_ps.item_summon_tickets as module
from aigis_ps.item_summon_tickets import DEFAULT_ITEM_TICKET_POOLS, ItemSummonTicketService
from aigis_ps.player_state import BARRACKS_SHIFT, UNIT_FIELDS
from aigis_ps.summon_grants import SummonGrants
from aigis_ps.summon_pool_catalog import SummonCandidate, SummonPool
from aigis_ps.summon_tickets import SummonTicketRules
from test_item_summon_tickets import case, change, fail, message
from test_quest_service import bundle


# cfgRarity ConfigID 5/11 share the ★5 group and 6/12 the ★6 group; ConfigID is CardData.Rare + 1.
GROUP_RARES = {5: {4, 10}, 6: {5, 11}}
ENSURED = {"item_smn_ticket_19056": (212, 6, 1), "item_smn_ticket_19073": (248, 6, 3),
           "item_smn_ticket_19122": (375, 5, 1)}
UNOWNED, UNOWNED_POOL = "item_smn_ticket_blacknohave_10029", 423
HERO, HERO_POOL = "item_smn_ticket_blacknohave_10024", 367
RULE_POOLS = (212, 218, 248, 253, 300, 325, 334, 335, 340, 364, 365, 367, 375, 376, 386, 403, 416, 417, 419, 420, 423, 460)
RULE_ITEMS = {
    "item_smn_ticket_19056": (212, 10, 6), "item_smn_ticket_19059": (218, 10, 6), "item_smn_ticket_19073": (248, 10, 7),
    "item_smn_ticket_19106": (325, 10, 6), "item_smn_ticket_19111": (340, 10, 6), "item_smn_ticket_19122": (375, 10, 9),
    "item_smn_ticket_19123": (376, 10, 6), "item_smn_ticket_19125": (386, 10, 6), "item_smn_ticket_19129": (403, 10, 9),
    **{f"item_smn_ticket_blacknohave_{10018 + offset}": (pool_id, 1, 4) for offset, pool_id in enumerate(
        (253, 300, 334, 335, 364, 365, 367, 416, 417, 419, 420, 423, 460))},
}
SHARED_BASIC = {"item_smn_ticket_19055": (212, 10), "item_smn_ticket_85058": (212, 1),
                "item_smn_ticket_19060": (218, 10), "item_smn_ticket_19061": (218, 10), "item_smn_ticket_85062": (218, 1),
                "item_smn_ticket_85063": (218, 1), "item_smn_ticket_19104": (325, 10), "item_smn_ticket_19105": (325, 10),
                "item_smn_ticket_85099": (325, 1), "item_smn_ticket_19124": (386, 10), "item_smn_ticket_19126": (386, 10),
                "item_smn_ticket_85113": (386, 1), "item_smn_ticket_85118": (403, 1)}
EVENT_LOGIN_RULE_REWARDS = {(90, 5): "item_smn_ticket_19122", (90, 10): "item_smn_ticket_19123",
                            (92, 2): "item_smn_ticket_19125", (104, 7): UNOWNED}
CARD_ALIAS = dict(UNIT_FIELDS)["CardID"]
ENSURED_RULE_SIX = LotteryRule(6, ENSURED_RULE, 6, 6, 1)
UNOWNED_RULE_FOUR = LotteryRule(4, UNOWNED_RULE, 6, 6, 0)


def mail(case, item_id, amount=1):
    profile = case.store.profile()
    state, rows = case.presents.enqueue(deepcopy(profile.state), [
        {"kind": "item", "item_id": item_id, "amount": amount, "description": "rule ticket"}])
    case.store.update_state(state, profile.revision)
    return [row["PresentID"] for row in rows]


def claim(case, present_id):
    return case.service.receive(message(case, (present_id,)), case.store, (present_id,))


def receipt(case):
    return case.store.profile().state["item_summon_history"]["receipts"][-1]


def matched(case, pool_id, group):
    pool = case.service.grants.validate_pool(pool_id)
    rarities = case.service.grants.candidate_rarities(pool_id)
    return pool, [index for index, (candidate, rare) in enumerate(zip(pool.candidates, rarities))
                  if candidate.weight > 0 and rare in GROUP_RARES[group]]


def add_units(case, state, pool_id, indices, *, barracks=0):
    player = state["player_state"]
    for index in indices:
        player["battle"]["UX"] += 1
        player["last_unique_id"] = player.get("last_unique_id", max(unit["UniqueID"] for unit in player["units"])) + 1
        unit = case.service.grants.expected_unit(pool_id, index, player["battle"]["UX"], player["last_unique_id"])
        unit["GeneralPurposeFlags"] |= barracks << BARRACKS_SHIFT
        player["units"].append(unit)


def own(case, pool_id, indices, *, barracks=0):
    change(case, lambda state: add_units(case, state, pool_id, indices, barracks=barracks))


def rares(case, units):
    rarities = case.players.card_rarities()
    return [rarities[unit["CardID"]] for unit in units]


def outsider(pool, guaranteed):
    index = next(index for index, candidate in enumerate(pool.candidates) if candidate.weight > 0 and index not in guaranteed)
    return index, sum(candidate.weight for candidate in pool.candidates[:index])


def test_default_rule_pools_are_complete_and_every_candidate_initializes(case):
    grants = case.service.grants
    assert set(RULE_POOLS) <= set(DEFAULT_ITEM_TICKET_POOLS) and len(DEFAULT_ITEM_TICKET_POOLS) == 44
    assert sum(len(grants.validated_pools[pool_id].candidates) for pool_id in RULE_POOLS) == 4412
    for pool_id in RULE_POOLS:
        pool = grants.validated_pools[pool_id]
        assert pool.complete and len(grants.initial_values[pool_id]) == len(pool.candidates)
    status = case.service.status()
    assert status["rule_pool_unsupported"] == [] and "guarantee_policy" in status
    assert {name for name in status["enabled_items"] if case.service.catalog.definition(name).rule} == set(RULE_ITEMS)
    assert set(SHARED_BASIC) <= set(status["enabled_items"]) and len(status["enabled_items"]) == 83
    for name, (pool_id, count, rule_id) in RULE_ITEMS.items():
        definition = case.service.validate_item(name)
        assert (definition.pool_id, definition.count, definition.rule.rule_id) == (pool_id, count, rule_id)
    for name, expected in SHARED_BASIC.items():
        definition = case.service.validate_item(name)
        assert (definition.pool_id, definition.count) == expected and definition.rule is None


@pytest.mark.parametrize("item_id", ["item_smn_ticket_blacknohave_10001", "item_smn_ticket_blacknohave_10006",
                                     "item_smn_ticket_19089", "item_smn_ticket_19093"])
def test_rule_tickets_on_incomplete_pools_stay_refused_without_normalizing(case, item_id):
    definition = case.service.catalog.definition(item_id)
    assert definition.supported and definition.pool_id not in DEFAULT_ITEM_TICKET_POOLS
    with pytest.raises(ProtocolError) as caught:
        case.service.validate_item(item_id)
    assert caught.value.code == "summon_ticket_pool_disabled"
    with pytest.raises(ProtocolError) as caught:
        case.service.pools.require_complete(definition.pool_id)
    assert caught.value.code == "summon_pool_incomplete"


@pytest.mark.parametrize("item_id", sorted(ENSURED))
def test_ensured_slots_are_drawn_last_from_the_guaranteed_rarity(case, item_id):
    pool_id, group, ensured = ENSURED[item_id]
    pool, guaranteed = matched(case, pool_id, group)
    other, offset = outsider(pool, guaranteed)
    calls = []
    def source(limit):
        calls.append(limit)
        return offset if limit == pool.total_weight else 0
    case.service.randbelow = source
    (present_id,) = mail(case, item_id)
    result = loads(claim(case, present_id))
    entry = receipt(case)
    assert entry["candidate_indices"] == [other] * (10 - ensured) + [guaranteed[0]] * ensured
    assert calls == [pool.total_weight] * (10 - ensured) + [sum(pool.candidates[index].weight
                                                                for index in guaranteed)] * ensured
    drawn = rares(case, entry["units"])
    assert drawn[0] not in GROUP_RARES[group] and all(rare in GROUP_RARES[group] for rare in drawn[-ensured:])
    ppu = next(node for node in result.children if node.name == "PPU")
    assert next(column.values for column in ppu.children if column.name == CARD_ALIAS) == tuple(
        unit["CardID"] for unit in entry["units"])


def test_ensured_ten_draw_still_needs_all_ten_slots(case):
    (present_id,) = mail(case, "item_smn_ticket_19056")
    case.players.capacity_provider = lambda _: 9
    fail(case, message(case, (present_id,)), (present_id,), "unit_roster_capacity")
    assert not case.calls


def test_shared_pool_ticket_without_a_rule_draws_the_whole_pool(case):
    pool = case.service.grants.validate_pool(212)
    (present_id,) = mail(case, "item_smn_ticket_19055")
    claim(case, present_id)
    assert case.calls == [pool.total_weight] * 10


def test_rules_sharing_a_pool_keep_their_own_guaranteed_group(case):
    shared, ensured = "item_smn_ticket_19055", "item_smn_ticket_19056"
    catalog = copy(case.service.catalog)
    definitions = dict(catalog.definitions)
    definitions[shared] = replace(definitions[shared], rule=LotteryRule(9, ENSURED_RULE, 5, 5, 1))
    catalog._definitions = MappingProxyType(definitions)
    case.service = case.presents.item_tickets = ItemSummonTicketService(
        case.bootstrap, case.players, case.presents, case.service.pools, catalog, randbelow=case.service.randbelow)
    for item_id, group in ((shared, 5), (ensured, 6)):
        assert case.service.validate_item(item_id).pool_id == 212
        (present_id,) = mail(case, item_id)
        claim(case, present_id)
        assert rares(case, receipt(case)["units"])[-1] in GROUP_RARES[group]


@pytest.mark.parametrize("campaign_id,ordinal", sorted(EVENT_LOGIN_RULE_REWARDS))
def test_event_login_rule_rewards_mail_and_claim_under_their_rules(case, bundle, campaign_id, ordinal):
    (reward,) = (reward for reward in bundle.event_logins.catalog.get(campaign_id).rewards_for(ordinal)
                 if reward.item_type == 22)
    assert reward.item_id == EVENT_LOGIN_RULE_REWARDS[campaign_id, ordinal]
    definition = case.service.validate_item(reward.item_id)
    present_ids = mail(case, reward.item_id, reward.item_param)
    claim(case, present_ids[0])
    drawn = rares(case, receipt(case)["units"])
    tail = 1 if definition.rule.rule_type == UNOWNED_RULE else definition.rule.ensured_count
    assert len(drawn) == definition.count and all(rare in GROUP_RARES[definition.rule.rarity_group]
                                                  for rare in drawn[-tail:])


def test_unowned_ticket_skips_cards_held_in_any_barracks(case):
    pool, candidates = matched(case, UNOWNED_POOL, 6)
    assert len(candidates) == len(pool.candidates) == 59
    change(case, lambda state: state["tables"]["config"][0].update({PURCHASE_FIELD: 1 << EXTRA_SHIFT}))
    own(case, UNOWNED_POOL, candidates[:1])
    own(case, UNOWNED_POOL, candidates[1:2], barracks=1)
    (present_id,) = mail(case, UNOWNED)
    claim(case, present_id)
    assert receipt(case)["candidate_indices"] == [candidates[2]]
    assert case.calls == [sum(pool.candidates[index].weight for index in candidates[2:])]
    assert len(case.store.profile().state["player_state"]["units"]) == 4


def test_unowned_ticket_refuses_and_keeps_the_mail_when_every_unit_is_owned(case, caplog):
    _, candidates = matched(case, UNOWNED_POOL, 6)
    own(case, UNOWNED_POOL, candidates)
    (present_id,) = mail(case, UNOWNED)
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        error = fail(case, message(case, (present_id,)), (present_id,), "item_summon_unowned_exhausted")
    assert error.status == 409 and not case.calls
    assert "item_summon_history" not in case.store.profile().state
    assert any(UNOWNED in line for line in caplog.messages)


def test_hero_pool_matches_rare_eleven_units_through_the_rarity_group(case):
    pool, candidates = matched(case, HERO_POOL, 6)
    assert len(candidates) == len(pool.candidates) == 22
    assert set(rares(case, [{"CardID": candidate.card_id} for candidate in pool.candidates])) == {11}
    (present_id,) = mail(case, HERO)
    claim(case, present_id)
    assert receipt(case)["candidate_indices"] == [candidates[0]]


def test_concurrent_copy_of_the_drawn_unit_refuses_without_redraw(case, monkeypatch):
    _, candidates = matched(case, UNOWNED_POOL, 6)
    (present_id,) = mail(case, UNOWNED)
    actual = case.store.update_state
    def save(state, revision):
        if revision == case.store.profile().revision:
            profile = case.store.profile()
            concurrent = deepcopy(profile.state)
            add_units(case, concurrent, UNOWNED_POOL, candidates[:1])
            actual(concurrent, profile.revision)
        return actual(state, revision)
    monkeypatch.setattr(case.store, "update_state", save)
    with pytest.raises(ProtocolError) as caught:
        claim(case, present_id)
    assert (caught.value.code, caught.value.status) == ("item_summon_changed", 409)
    state = case.store.profile().state
    assert len(case.calls) == 1 and "item_summon_history" not in state
    assert any(row["PresentID"] == present_id for row in state["tables"]["presents"])


def test_unrelated_concurrent_write_keeps_the_frozen_ensured_draw(case, monkeypatch):
    (present_id,) = mail(case, "item_smn_ticket_19073")
    actual = case.store.update_state
    attempts = []
    def save(state, revision):
        attempts.append(1)
        if len(attempts) == 1:
            profile = case.store.profile()
            concurrent = deepcopy(profile.state)
            concurrent["sentinel"]["keep"].append(2)
            actual(concurrent, profile.revision)
        return actual(state, revision)
    monkeypatch.setattr(case.store, "update_state", save)
    claim(case, present_id)
    assert len(attempts) == 2 and len(case.calls) == 10
    assert case.store.profile().state["sentinel"]["keep"] == [1, 2]


@pytest.mark.parametrize("item_id", ["item_smn_ticket_19073", UNOWNED])
def test_rule_claims_replay_exactly_after_restart_without_drawing(case, item_id):
    (present_id,) = mail(case, item_id)
    envelope = message(case, (present_id,))
    result = case.service.receive(envelope, case.store, (present_id,))
    service = ItemSummonTicketService(case.bootstrap, case.players, case.presents, case.service.pools,
        case.service.catalog, randbelow=lambda _: pytest.fail("replay drew again"))
    before = case.store.profile()
    assert service.receive(envelope, case.store, (present_id,)) == result
    assert case.store.profile() == before


@pytest.mark.parametrize("item_id,slot,guaranteed_slot,accepted", [
    ("item_smn_ticket_19056", -1, True, True), ("item_smn_ticket_19056", -1, False, False),
    ("item_smn_ticket_19073", -3, False, False), ("item_smn_ticket_19073", -4, False, True),
], ids=["ensured-in-group", "ensured-outside", "earliest-ensured-outside", "open-slot-outside"])
def test_replay_checks_that_every_ensured_slot_belongs_to_the_guarantee(case, item_id, slot, guaranteed_slot, accepted):
    pool_id, group, _ = ENSURED[item_id]
    pool, guaranteed = matched(case, pool_id, group)
    (present_id,) = mail(case, item_id)
    envelope = message(case, (present_id,))
    case.service.receive(envelope, case.store, (present_id,))
    profile = case.store.profile()
    drawn = receipt(case)["candidate_indices"][slot]
    choices = guaranteed if guaranteed_slot else [index for index, candidate in enumerate(pool.candidates)
                                                  if candidate.weight > 0 and index not in guaranteed]
    replacement = next(index for index in choices if index != drawn)
    def forge(state):
        entry = state["item_summon_history"]["receipts"][-1]
        old = entry["units"][slot]
        unit = case.service.grants.expected_unit(pool_id, replacement, old["GetIndex"], old["UniqueID"])
        entry["units"][slot], state["player_state"]["units"][slot] = unit, deepcopy(unit)
        entry["candidate_indices"][slot] = replacement
        entry["state"] = module._state_digest(state)
        entry["response"] = case.service._response(profile, state, entry["units"]).decode("utf-8")
    change(case, forge, revision=False)
    if accepted:
        assert case.service.receive(envelope, case.store, (present_id,)).decode("utf-8") == receipt(case)["response"]
    else:
        fail(case, envelope, (present_id,), "item_summon_history_invalid")


@pytest.mark.parametrize("drawn_card,accepted", [(False, True), (True, False)])
def test_replay_checks_that_the_unowned_unit_was_absent_before_the_claim(case, drawn_card, accepted):
    _, candidates = matched(case, UNOWNED_POOL, 6)
    change(case, lambda state: (state["player_state"]["battle"].update(UX=5),
                                state["player_state"].update(last_unique_id=5)))
    (present_id,) = mail(case, UNOWNED)
    envelope = message(case, (present_id,))
    case.service.receive(envelope, case.store, (present_id,))
    assert receipt(case)["candidate_indices"] == [candidates[0]]
    def forge(state):
        index = candidates[0] if drawn_card else candidates[1]
        units = state["player_state"]["units"]
        units.insert(1, case.service.grants.expected_unit(UNOWNED_POOL, index, 3, 3))
        state["item_summon_history"]["receipts"][-1]["state"] = module._state_digest(state)
    change(case, forge, revision=False)
    if accepted:
        assert case.service.receive(envelope, case.store, (present_id,)).decode("utf-8") == receipt(case)["response"]
    else:
        fail(case, envelope, (present_id,), "item_summon_history_invalid")


def test_receipts_written_under_the_previous_ticket_content_turn_stale(case):
    (present_id,) = mail(case, "item_smn_ticket_19056")
    envelope = message(case, (present_id,))
    case.service.receive(envelope, case.store, (present_id,))
    change(case, lambda state: state["item_summon_history"]["receipts"][-1].update(content="0" * 64), revision=False)
    fail(case, envelope, (present_id,), "item_summon_retry_stale")
    assert len(case.calls) == 10


@pytest.mark.parametrize("item_id", ["item_smn_ticket_90001", "item_smn_ticket_90008"])
def test_pack_tickets_cannot_be_mailed_and_imported_copies_stay_unclaimable(case, item_id):
    profile = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.presents.enqueue(deepcopy(profile.state), [{"kind": "item", "item_id": item_id, "amount": 1,
                                                          "description": "pack"}])
    assert (caught.value.code, caught.value.status) == ("item_summon_unsupported", 501)
    assert "receive_action_unsupported" in str(caught.value)
    change(case, lambda state: state["tables"]["presents"][0].update(ItemID=item_id))
    fail(case, code="item_summon_unsupported", adapter=True)
    assert not case.calls


def test_explicit_policy_without_the_rule_pool_refuses_its_tickets(case):
    service = ItemSummonTicketService(case.bootstrap, case.players, case.presents, case.service.pools,
        case.service.catalog, rules=SummonTicketRules(tuple(pool for pool in DEFAULT_ITEM_TICKET_POOLS
                                                            if pool != UNOWNED_POOL)))
    with pytest.raises(ProtocolError) as caught:
        service.validate_item(UNOWNED)
    assert caught.value.code == "summon_ticket_pool_disabled"
    assert UNOWNED not in service.status()["enabled_items"]


def test_rule_pool_mismatch_marks_only_that_ticket_unsupported(case, monkeypatch):
    actual = SummonGrants.candidate_rarities
    def altered(self, pool_id):
        rarities = actual(self, pool_id)
        return (4, *rarities[1:]) if pool_id == UNOWNED_POOL else rarities
    monkeypatch.setattr(SummonGrants, "candidate_rarities", altered)
    service = ItemSummonTicketService(case.bootstrap, case.players, case.presents, case.service.pools,
                                      case.service.catalog)
    status = service.status()
    assert status["rule_pool_unsupported"] == [UNOWNED] and len(status["enabled_items"]) == 82
    with pytest.raises(ProtocolError) as caught:
        service.validate_item(UNOWNED)
    assert (caught.value.code, caught.value.status, str(caught.value)) == (
        "item_summon_unsupported", 501, module.RULE_POOL_UNSUPPORTED)
    assert service.validate_item(HERO).rule == UNOWNED_RULE_FOUR


def pool_of(*rows):
    candidates = tuple(SummonCandidate(card_id, 1, 0, float(weight), weight) for card_id, weight in rows)
    return SummonPool(1, 1, candidates, sum(weight for _, weight in rows), 100.0, True)


@pytest.mark.parametrize("rule,rows,groups,expected", [
    (ENSURED_RULE_SIX, ((2, 1), (3, 1), (4, 1)), (6, 5, 6), (0, 2)),
    (ENSURED_RULE_SIX, ((2, 1), (3, 1), (4, 2)), (6, 5, 6), None),
    (ENSURED_RULE_SIX, ((2, 1), (3, 1), (2, 1)), (6, 5, 6), None),
    (ENSURED_RULE_SIX, ((2, 1), (3, 1)), (5, None), None),
    (ENSURED_RULE_SIX, ((2, 1), (3, 0), (4, 1)), (6, 6, 5), (0,)),
    (UNOWNED_RULE_FOUR, ((2, 1), (3, 1)), (6, 6), (0, 1)),
    (UNOWNED_RULE_FOUR, ((2, 1), (3, 1)), (6, 5), None),
    (UNOWNED_RULE_FOUR, ((2, 1), (3, 0)), (6, 5), (0,)),
    (UNOWNED_RULE_FOUR, ((2, 1), (3, 1)), (6, None), None),
], ids=["uniform", "ratio-differs", "repeated-card", "no-match", "zero-weight-ignored", "unowned-all-match",
        "unowned-outsider", "unowned-zero-weight-outsider", "unowned-unknown-group"])
def test_rule_pool_guard_only_accepts_unambiguous_subsets(rule, rows, groups, expected):
    assert module._guaranteed_indices(rule, pool_of(*rows), groups) == expected
