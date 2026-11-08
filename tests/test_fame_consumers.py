from copy import copy, deepcopy

import pytest

from aigis_ps.alxml import Column, Container
from aigis_ps.fame_state import COUNT_FIELDS
from aigis_ps.training_catalog import NO_BOOST, OwnershipBonus, TrainingBoost
from test_fame_service import make_case, catalog, call, message, NOW
from test_quest_service import bundle, request
from test_trade_catalog import catalog as trade_catalog, effect, product, quote, resources, state as trade_state
from test_training_service import columns


SUPPORT_CARD, SUPPORT_BONUS = 416, 10


def test_nonempty_fame_stock_does_not_hide_actual_training_bonus_sources(make_case):
    case = make_case(card_id=17)
    call(case, message(case, 149))
    call(case, message(case, 150, tick=1001))
    profile = case.store.profile()
    saved, growth = profile.state, case.bundle.training.catalog
    assert growth.ownership_bonus(saved["player_state"], saved) == OwnershipBonus(0, frozenset((NO_BOOST,)))
    state, added = case.bundle.player_state.credit_units(saved, [{"card_id": SUPPORT_CARD, "level": 1, "count": 1}])
    case.store.update_state(state, profile.revision)
    saved = case.store.profile().state
    held = TrainingBoost(SUPPORT_CARD, added[0]["GetIndex"], SUPPORT_BONUS)
    assert growth.ownership_bonus(saved["player_state"], saved) == OwnershipBonus(SUPPORT_BONUS, frozenset((held,)))


def test_actual_synthesis_retains_fame_stock_and_uses_the_owned_support_bonus(make_case):
    case = make_case(card_id=17)
    call(case, message(case, 149))
    call(case, message(case, 150, tick=1001))
    before = case.store.profile()
    state, units = case.bundle.player_state.credit_units(before.state, [{"card_id": 3, "level": 1, "count": 3}])
    state, support = case.bundle.player_state.credit_units(state, [{"card_id": SUPPORT_CARD, "level": 1, "count": 1}])
    state["tables"]["game_param"][0]["Money"] = 10000
    case.store.update_state(state, before.revision)
    source = deepcopy(state["tables"]["fame_depository"])
    service = copy(case.bundle.training)
    service.bootstrap, service.missions = case.bootstrap, case.missions
    envelope = request(case.store, 5, (
        Container("BASE", columns((units[0],))), Container("APPEND", columns(units[1:])),
        Container("CMB", (Column("A1", "I", (SUPPORT_CARD,)), Column("A7", "I", (support[0]["GetIndex"],)),
                          Column("B0", "I", (SUPPORT_BONUS,)))),
    ), tick=1002)
    service.integrate(envelope, case.store)
    saved = case.store.profile().state
    target = next(unit for unit in saved["player_state"]["units"] if unit["GetIndex"] == units[0]["GetIndex"])
    assert target["Exp"] == 220
    assert saved["tables"]["fame_depository"] == source
    assert case.repository.owned_card_ids(saved) == frozenset((17,))


@pytest.mark.parametrize("stage", range(5))
def test_trade_discount_reads_any_positive_fame_stage_and_not_zero_rows(stage):
    subject = trade_catalog(resources([product(Type=2)], specialty=[effect()]))
    state = trade_state()
    state["player_state"]["units"] = []
    state["fame_depository_schema"] = 1
    state["tables"]["fame_depository"] = [{"CardId": 7, **dict.fromkeys(COUNT_FIELDS, 0)}]
    assert quote(subject, state, registration=105).ownership_discount == 0
    state["tables"]["fame_depository"][0][f"Count{stage}"] = 1
    before = deepcopy(state)
    assert quote(subject, state, registration=105).ownership_discount == -50
    assert state == before
