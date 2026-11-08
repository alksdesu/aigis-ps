import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.item_summon_tickets import ItemSummonTicketService
from aigis_ps.summon_tickets import SummonTicketRules
from test_event_pass_ticket_pools import default_service
from test_quest_service import bundle


def test_activity_pool_463_is_complete_and_all_candidates_initialize(default_service):
    pool = default_service.grants.validated_pools[463]
    values = default_service.grants.initial_values[463]
    assert pool.complete and len(pool.candidates) == len(values) == 507
    assert abs(pool.percent_sum - 100) < 0.001
    assert all(row.level == 1 and row.evolution_count == 0 for row in pool.candidates)
    assert all(row.class_id > 0 and row.skill_id >= 0 and row.experience >= 0 for row in values)
    for name, count in (("item_smn_ticket_19158", 10), ("item_smn_ticket_85140", 1)):
        item = default_service.validate_item(name)
        assert item.pool_id == 463 and item.count == count


@pytest.mark.parametrize("enabled", [(), (428,)])
def test_activity_pool_does_not_expand_explicit_configuration(bundle, enabled):
    service = ItemSummonTicketService(bundle.bootstrap, bundle.player_state, bundle.presents,
        bundle.summon_pools, bundle.item_summon_tickets.catalog, rules=SummonTicketRules(enabled))
    assert service.rules.enabled_pools == enabled
    with pytest.raises(ProtocolError) as caught:
        service.validate_item("item_smn_ticket_19158")
    assert caught.value.code == "summon_ticket_pool_disabled"
