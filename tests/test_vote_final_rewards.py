from copy import copy, deepcopy

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.vote_finalists import VoteFinalistService
from aigis_ps.vote_rewards import VoteRewardService
from aigis_ps.vote_state import record_vote
from test_quest_service import bundle
from test_vote_catalog import vote_sources, build
from test_vote_rewards import case, catalog, proposal, vote


def helper(case):
    finalists = VoteFinalistService(case.catalog)
    return VoteRewardService(case.catalog, case.presents, finalists=finalists), finalists


def final_proposal(service, state, card_id, count=5):
    group = service.catalog.groups[-1]
    after = record_vote(state, item_id=group.item_id, group_id=4, card_id=card_id,
                        count=count, definition=service.group_definition(group))
    return after, group


def test_optional_finalists_preserve_every_existing_semantic_fingerprint(case):
    service, finalists = helper(case)
    assert service.content_version == case.helper.content_version
    assert service._event_definition() == case.helper._event_definition()
    for group in case.catalog.groups:
        assert service.group_definition(group) == case.helper.group_definition(group)
    original, _, _ = vote(case, case.state, 15)
    after, group = proposal(case, original, 5)
    assert service.apply_vote(original, after, group) == case.helper.apply_vote(original, after, group)
    assert "vote_finalists" not in original


def test_final_vote_requires_qualified_frozen_preliminaries(case):
    service, finalists = helper(case)
    card = case.catalog.candidates[0].card_id
    after, group = final_proposal(service, case.state, card)
    with pytest.raises(ProtocolError) as failure:
        service.apply_vote(case.state, after, group)
    assert failure.value.code == "vote_reward_candidate_invalid"
    before, _ = finalists.settle(case.state, group.vote_start)
    after, _ = final_proposal(service, before, card)
    updated, awards, ids = service.apply_vote(before, after, group)
    assert len(awards) == len(ids) == 1 and awards[0]["GroupID"] == 4 and awards[0]["Count"] == 5
    assert updated["vote_rewards"]["items"][group.item_id]["count"] == 5
    assert updated["vote_finalists"] == before["vote_finalists"]
    service.validate_delivery(updated, group.item_id, awards, ids)


def test_final_rewards_without_qualification_service_are_unavailable(case):
    card = case.catalog.candidates[0].card_id
    after, group = final_proposal(case.helper, case.state, card)
    with pytest.raises(ProtocolError) as failure:
        case.helper.apply_vote(case.state, after, group)
    assert failure.value.code == "vote_finalists_unavailable"


def test_eliminated_candidate_cannot_receive_final_vote_rewards(case):
    service, finalists = helper(case)
    before = deepcopy(case.state)
    for group in case.catalog.groups[:3]:
        candidates = [candidate for candidate in case.catalog.candidates if candidate.group_id == group.group_id]
        for candidate in candidates[:10]:
            before = record_vote(before, item_id=group.item_id, group_id=group.group_id,
                card_id=candidate.card_id, count=1, definition=service.group_definition(group))
    final = case.catalog.groups[-1]
    before, _ = finalists.settle(before, final.vote_start)
    candidate = [candidate for candidate in case.catalog.candidates if candidate.group_id == 1][10]
    after, _ = final_proposal(service, before, candidate.card_id)
    with pytest.raises(ProtocolError) as failure:
        service.apply_vote(before, after, final)
    assert failure.value.code == "vote_reward_candidate_invalid"
    assert final.item_id not in before["vote_finalists"]["groups"]


@pytest.mark.parametrize("group_id", (1, 4))
def test_closed_preliminary_and_final_groups_cannot_bypass_transaction_guard(case, group_id):
    service, finalists = helper(case)
    group = case.catalog.groups_by_id[group_id]
    before, _ = finalists.settle(case.state, group.vote_end)
    card = case.catalog.candidates[0].card_id
    after = record_vote(before, item_id=group.item_id, group_id=group_id, card_id=card,
                        count=5, definition=service.group_definition(group))
    with pytest.raises(ProtocolError) as failure:
        service.apply_vote(before, after, group)
    assert failure.value.code == "vote_group_frozen"


@pytest.mark.parametrize("change", ("delete", "replace"))
def test_vote_transition_cannot_remove_or_replace_qualification_evidence(case, change):
    service, finalists = helper(case)
    before, _ = finalists.settle(case.state, case.catalog.groups[-1].vote_start)
    after, group = final_proposal(service, before, case.catalog.candidates[0].card_id)
    if change == "delete":
        del after["vote_finalists"]
    else:
        after["vote_finalists"]["groups"][case.catalog.groups[0].item_id]["definition"] = "b" * 64
    with pytest.raises(ProtocolError) as failure:
        service.apply_vote(before, after, group)
    assert failure.value.code == "vote_reward_transition_invalid"


def test_bound_finalist_dependency_identity_cannot_change(case):
    service, finalists = helper(case)
    service.finalists = VoteFinalistService(case.catalog)
    with pytest.raises(ProtocolError) as failure:
        service.initialize(case.state)
    assert failure.value.code == "vote_rewards_dependencies_changed"


def test_finalist_catalog_must_share_the_exact_reward_catalog(case):
    wrong = copy(case.catalog)
    finalists = VoteFinalistService(wrong)
    with pytest.raises(ProtocolError) as failure:
        VoteRewardService(case.catalog, case.presents, finalists=finalists)
    assert failure.value.code == "vote_rewards_dependencies_changed"
