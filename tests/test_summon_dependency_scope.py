from copy import copy

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.item_summon_tickets import DEFAULT_ITEM_TICKET_POOLS
from aigis_ps.summon_grants import SummonGrants
from test_quest_service import bundle


def test_type16_cannot_gain_pool428_from_a_broader_shared_grant_helper(bundle):
    assert bundle.summon_tickets is not None, bundle.summon_ticket_error
    service = copy(bundle.summon_tickets)
    assert service.rules.enabled_pools == (350,)
    original_helper = service.grants
    content_version = service.content_version
    service.grants = SummonGrants(service.players, service.pools, (350, 428))

    with pytest.raises(ProtocolError) as caught:
        service.validate_pool(428)

    assert caught.value.code == "summon_ticket_dependencies_changed"
    assert service.rules.enabled_pools == (350,)
    assert service.content_version == content_version
    assert bundle.summon_tickets.grants is original_helper
    assert original_helper.enabled_pools == (350,)


def test_type22_rejects_broader_helper_even_for_an_otherwise_enabled_item(bundle):
    assert bundle.item_summon_tickets is not None, bundle.item_summon_error
    service = copy(bundle.item_summon_tickets)
    assert service.rules.enabled_pools == DEFAULT_ITEM_TICKET_POOLS
    original_helper = service.grants
    content_version = service.content_version
    service.grants = SummonGrants(service.players, service.pools, (350, *DEFAULT_ITEM_TICKET_POOLS))

    with pytest.raises(ProtocolError) as caught:
        service.validate_item("item_smn_ticket_19138")

    assert caught.value.code == "item_summon_dependencies_changed"
    assert service.rules.enabled_pools == DEFAULT_ITEM_TICKET_POOLS
    assert service.content_version == content_version
    assert bundle.item_summon_tickets.grants is original_helper
    assert original_helper.enabled_pools == DEFAULT_ITEM_TICKET_POOLS
