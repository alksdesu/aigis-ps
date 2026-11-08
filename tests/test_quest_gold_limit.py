import pytest

from aigis_ps.wallet_money import GOLD_LIMIT
from test_quest_battle_modes import game_params, gold_events, update_game_param
from test_quest_service import THREE_STAR_GOLD, bundle, finish_request, nodes, setup, start_request


INT32_MAX = 0x7FFFFFFF
# Quest 1001 settles 17 experience and its QuestTable Gold of 450 at the three-star rate under the default rules.
EXPERIENCE = 17
# Five counted gold events add 500 to the base 450, and the three-star rate raises the sum to 1425.
EVENT_SETTLEMENT = 1425


@pytest.mark.parametrize("money,credited", [(GOLD_LIMIT - 1000, THREE_STAR_GOLD),
                                            (GOLD_LIMIT - THREE_STAR_GOLD, THREE_STAR_GOLD), (GOLD_LIMIT - 1, 1),
                                            (GOLD_LIMIT, 0), (GOLD_LIMIT + 1, 0), (INT32_MAX, 0)])
def test_settlement_discards_gold_beyond_the_limit_and_reports_the_credited_amount(setup, money, credited):
    service, store = setup
    update_game_param(store, Money=money)
    service.start(start_request(service, store), store)
    finishing = finish_request(store)
    response = service.finish(finishing, store)
    settled = store.profile()
    tree = nodes(response)
    assert settled.state["tables"]["game_param"][0]["Money"] == money + credited
    assert game_params(tree)["A1"] == str(money + credited)
    assert tree["REWARD"].children[0].values == (EXPERIENCE,) and tree["REWARD"].children[1].values == (credited,)
    assert service.finish(finishing, store) == response and store.profile() == settled


def test_gold_events_share_the_limit_with_the_base_gold(setup):
    service, store = setup
    update_game_param(store, Money=GOLD_LIMIT - EVENT_SETTLEMENT)
    service.start(start_request(service, store), store)
    tree = nodes(service.finish(finish_request(store, gold=5, extra=gold_events(5)), store))
    settled = store.profile().state
    assert settled["tables"]["game_param"][0]["Money"] == GOLD_LIMIT
    assert tree["REWARD"].children[1].values == (EVENT_SETTLEMENT,)
    assert settled["quest_state"]["history"][-1]["gold_events"] == 5
