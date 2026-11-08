import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.item_summon_tickets import ItemSummonTicketService
from aigis_ps.summon_tickets import SummonTicketRules
from test_event_pass_ticket_pools import default_service, EXPECTED_ITEMS
from test_quest_service import bundle


def test_purchased_reward_pools_are_complete_and_have_native_initializations(default_service):
    expected = {194: 416, 275: 450, 415: 98, 472: 103}
    for identifier, count in expected.items():
        pool = default_service.grants.validated_pools[identifier]
        values = default_service.grants.initial_values[identifier]
        assert pool.complete and len(pool.candidates) == len(values) == count
        assert abs(pool.percent_sum - 100) < 0.001
        assert all(row.evolution_count == 0 and row.level in (1, 10, 20, 30) for row in pool.candidates)
        assert all(row.class_id > 0 and row.skill_id >= 0 and row.experience >= 0 for row in values)
    for name, (pool_id, count) in EXPECTED_ITEMS.items():
        if pool_id in expected:
            item = default_service.validate_item(name)
            assert item.pool_id == pool_id and item.count == count


@pytest.mark.parametrize("enabled", [(), (428,)])
def test_explicit_pool_configuration_stays_narrow(bundle, enabled):
    service = ItemSummonTicketService(bundle.bootstrap, bundle.player_state, bundle.presents,
        bundle.summon_pools, bundle.item_summon_tickets.catalog, rules=SummonTicketRules(enabled))
    assert service.rules.enabled_pools == enabled
    for name in ("item_smn_ticket_85051", "item_smn_ticket_85084", "item_smn_ticket_85122", "item_smn_ticket_85143"):
        with pytest.raises(ProtocolError) as caught:
            service.validate_item(name)
        assert caught.value.code == "summon_ticket_pool_disabled"
