from copy import deepcopy
from datetime import timedelta

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.vote_tickets import validate_vote_ticket_observation
from test_vote_tickets import components, state, NOW, START, GROUP, count


def test_observation_is_a_detached_snapshot_and_old_schema_fingerprint_stays_stable():
    service = components().service
    version = service.content_version
    initial = state(ordinary=10)
    updated, observed = service.observe(initial, NOW)
    assert "vote_tickets" not in initial and count(updated) == 1
    assert observed == updated["vote_tickets"] and observed is not updated["vote_tickets"]
    service.validate_observation(updated, observed, exact=True)
    consumed = service.consume(updated, GROUP, 1, NOW)[0]
    service.validate_observation(consumed, observed)
    with pytest.raises(ProtocolError, match="committed observation"):
        service.validate_observation(consumed, observed, exact=True)
    later, new = service.observe(consumed, NOW + timedelta(days=1))
    service.validate_observation(later, observed)
    service.validate_observation(later, new, exact=True)
    assert service.content_version == version


def test_unobserved_inactive_and_old_undated_observations():
    service = components().service
    empty, observed = service.observe(state(), START - timedelta(days=1))
    assert observed is None and "vote_tickets" not in empty
    service.validate_observation(empty, observed, exact=True)
    old, unknown = service.observe(state(daily=1), START - timedelta(days=1))
    assert unknown["status"] == "unresolved"
    current, _ = service.observe(old, NOW)
    service.validate_observation(current, unknown)
    service.validate_observation(current, None)
    with pytest.raises(ProtocolError, match="committed observation"):
        service.validate_observation(current, None, exact=True)


@pytest.mark.parametrize("corruption", ("missing", "time", "day"))
def test_permanent_observation_cannot_lose_or_rewind_domain(corruption):
    service = components().service
    saved, observation = service.observe(state(), NOW)
    broken = deepcopy(saved)
    if corruption == "missing":
        del broken["vote_tickets"]
    elif corruption == "time":
        broken["vote_tickets"]["last_time"] -= 1000
    else:
        broken["vote_tickets"]["last_time"] -= 86400000
        broken["vote_tickets"]["ticket_day"] = "2026-10-31"
    with pytest.raises(ProtocolError): service.validate_observation(broken, observation)


@pytest.mark.parametrize("value", ([], False, {}, {"schema_version": True}, {"status": "invalid"}))
def test_invalid_observation_values_are_rejected(value):
    with pytest.raises(ProtocolError): validate_vote_ticket_observation(value)


def test_snapshot_cannot_claim_expired_ticket_on_its_own_day():
    service = components().service
    _, observation = service.observe(state(), NOW)
    observation["status"] = "expired"
    with pytest.raises(ProtocolError): validate_vote_ticket_observation(observation)


@pytest.mark.parametrize("change", ("available", "origin"))
def test_same_day_receipt_cannot_be_reset_after_consumption(change):
    service = components().service
    consumed = service.consume(state(), GROUP, 1, NOW)[0]
    observed = deepcopy(consumed["vote_tickets"])
    damaged = deepcopy(consumed)
    if change == "available":
        damaged["vote_tickets"]["status"] = "available"
        damaged["tables"]["possessions"][0]["Count"] = 1
    else:
        damaged["vote_tickets"]["origin"] = "baseline"
    with pytest.raises(ProtocolError, match="cannot regain"):
        service.validate_observation(damaged, observed)
