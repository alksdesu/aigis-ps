import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.item_summon_tickets import ItemSummonTicketService
from aigis_ps.summon_tickets import SummonTicketRules
from test_event_pass_ticket_pools import default_service
from test_quest_service import bundle


def test_comeback_pool_is_complete_and_every_candidate_initializes(default_service):
    pool = default_service.grants.validated_pools[346]
    values = default_service.grants.initial_values[346]
    assert pool.complete and len(pool.candidates) == len(values) == 478
    assert abs(pool.percent_sum - 100) < 0.001
    assert {row.level for row in pool.candidates} == {1, 10, 20, 30}
    assert all(row.evolution_count == 0 for row in pool.candidates)
    assert all(row.class_id > 0 and row.skill_id >= 0 and row.experience >= 0 for row in values)
    for item_id, count in (("item_smn_ticket_19114", 10), ("item_smn_ticket_85107", 1)):
        item = default_service.validate_item(item_id)
        assert item.pool_id == 346 and item.count == count


@pytest.mark.parametrize("enabled", [(), (428,), (463,)])
def test_explicit_pool_policy_does_not_gain_comeback_pool(bundle, enabled):
    service = ItemSummonTicketService(bundle.bootstrap, bundle.player_state, bundle.presents,
        bundle.summon_pools, bundle.item_summon_tickets.catalog, rules=SummonTicketRules(enabled))
    assert service.rules.enabled_pools == enabled
    with pytest.raises(ProtocolError) as caught:
        service.validate_item("item_smn_ticket_19114")
    assert caught.value.code == "summon_ticket_pool_disabled"
