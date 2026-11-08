from copy import copy

from aigis_ps.select_tickets import SelectTicketService
from test_quest_service import bundle


def test_all_selection_variants_share_a_bounded_initialization_snapshot(bundle):
    players = copy(bundle.player_state)
    original = players._initialization
    calls = []

    def observed():
        calls.append(1)
        return original()

    players._initialization = observed
    service = SelectTicketService(bundle.bootstrap, players, bundle.presents, bundle.selection_tickets.catalog)
    assert len(service._initial_values) == 1025
    assert len(calls) <= 12
    assert service.status()["ready"]
