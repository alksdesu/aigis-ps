from copy import deepcopy
from functools import partial
from types import SimpleNamespace
import re

import pytest

from aigis_ps import evolution_rewards
from aigis_ps.alxml import Container, dumps, loads
from aigis_ps.card_status import CARD_STATUS_FIELDS, CardStatusService
from aigis_ps.errors import ProtocolError
from aigis_ps.evolution_rewards import EvolutionRewards, REWARD_DESCRIPTION
from aigis_ps.local_present_templates import LOCAL_TEMPLATE_CARDS
from aigis_ps.presents import MAX_PRESENTS, PresentService
from test_quest_service import bundle


@pytest.fixture(scope="module")
def components(bundle):
    cards = bundle.gifts.catalog
    reader = lambda name: bundle.native_cdn.assets.read(name)[0]
    presents = PresentService(bundle.bootstrap, bundle.player_state, asset_reader=reader)
    assert presents.status()["ready"]
    assert cards.card(17)["Rare"] == cards.card(22)["Rare"] == 5
    status = CardStatusService(cards.snapshot)
    service = EvolutionRewards(cards, bundle.player_state, status, presents)
    return SimpleNamespace(bundle=bundle, cards=cards, players=bundle.player_state,
                           status=status, presents=presents, service=service)


def make_state(components, card_ids=(17,)):
    state = components.players.initialize(components.bundle.bootstrap.initial_state())
    targets = []
    for card_id in card_ids:
        state, units = components.players.credit_units(state, [{"card_id": card_id, "level": 1, "count": 1}])
        unit = state["player_state"]["units"][-1]
        initial = components.cards.card(card_id)["InitClassID"]
        unit.update(ClassChangeCount=2, Love=100,
                    ClassID=components.players.classes[initial]["JobChange"] or initial)
        targets.append(unit["GetIndex"])
    return state, targets


def evolve(components, state, target, stage=3):
    candidate = deepcopy(state)
    unit = next(unit for unit in candidate["player_state"]["units"] if unit["GetIndex"] == target)
    current = components.players.classes[unit["ClassID"]]
    unit.update(ClassChangeCount=stage, ClassID=current["AwakeType1" if stage == 3 else "AwakeType2"] or unit["ClassID"])
    return candidate


def add_status(components, state, card_id, **fields):
    return components.status.update(state, [{"CardID": card_id, "A1": 0, **fields}])


def rows(container):
    return [dict(zip((column.name for column in container.children), values))
            for values in zip(*(column.values for column in container.children))]


def ledger(*identifiers):
    return {"schema_version": 1, "reward_scope": "card", "claimed": list(identifiers)}


def assert_empty_status(response):
    assert response == (Container("CardStatus"),)
    wire = loads(dumps(Container(children=response)))
    assert len(wire.children) == 1 and wire.children[0].name == "CardStatus"
    assert wire.children[0].children == ()


def assert_failure_preserves(components, before, candidate, card_id, code):
    saved_before, saved_candidate = deepcopy(before), deepcopy(candidate)
    with pytest.raises(ProtocolError) as error:
        components.service.apply(before, candidate, card_id)
    assert error.value.code == code
    assert before == saved_before and candidate == saved_candidate


@pytest.mark.parametrize("stage", [3, 4])
def test_first_reward_keeps_candidate_changes_and_emits_full_status(components, stage):
    before, targets = make_state(components)
    candidate = evolve(components, before, targets[0], stage)
    candidate["tables"]["game_param"][0]["Money"] -= 100
    candidate["reward_test"] = {"nested": [1, 2]}
    original_before, original_candidate = deepcopy(before), deepcopy(candidate)
    updated, response = components.service.apply(before, candidate, 17)
    assert updated["evolution_rewards"] == ledger(17)
    assert updated["player_state"] == candidate["player_state"]
    assert updated["tables"]["game_param"] == candidate["tables"]["game_param"]
    assert tuple(node.name for node in response) == ("PPT", "CardStatus")
    mail = components.presents.rows(updated)
    assert len(mail) == 1 and mail[0]["Content"] == components.presents.initial_unit_template(17)
    assert mail[0]["Type"] == 1 and mail[0]["Description"] == REWARD_DESCRIPTION
    assert rows(response[0])[0]["PresentID"] == mail[0]["PresentID"]
    assert rows(response[1]) == [dict.fromkeys(CARD_STATUS_FIELDS, 0) | {"CardID": 17}]
    assert before == original_before and candidate == original_candidate
    updated["reward_test"]["nested"].append(3)
    updated["player_state"]["units"][-1]["Love"] = 150
    assert candidate == original_candidate and before == original_before


def test_full_existing_card_status_values_are_preserved(components):
    before, targets = make_state(components)
    fields = {"A1": 1234, "A3": -7, "A5": 9, "A7": 13, "B0": 1700000001,
              "B1": 1700000002, "B2": 1700000003, "B3": 0, "B4": 0}
    before = add_status(components, before, 17, **fields)
    before = add_status(components, before, 22, A7=7)
    candidate = evolve(components, before, targets[0])
    updated, response = components.service.apply(before, candidate, 17)
    assert components.status.read(updated) == components.status.read(before)
    assert rows(response[1]) == [{"CardID": 17, **fields}]


def test_second_instance_never_receives_a_second_reward(components):
    before, targets = make_state(components, (17, 17))
    first, _ = components.service.apply(before, evolve(components, before, targets[0]), 17)
    second_candidate = evolve(components, first, targets[1], 4)
    second, response = components.service.apply(first, second_candidate, 17)
    assert_empty_status(response)
    assert second == second_candidate
    assert second["evolution_rewards"] == ledger(17)
    assert len(components.presents.rows(second)) == 1
    assert second is not second_candidate and second["player_state"] is not second_candidate["player_state"]


def test_old_second_awakening_instance_suppresses_reward_for_new_instance(components):
    before, targets = make_state(components, (17, 17))
    before = evolve(components, before, targets[0])
    candidate = evolve(components, before, targets[1], 4)
    updated, response = components.service.apply(before, candidate, 17)
    assert_empty_status(response)
    assert updated["evolution_rewards"] == ledger(17)
    assert not components.presents.rows(updated)
    assert components.status.read(updated) == []


@pytest.mark.parametrize("field", ["B3", "B4"])
@pytest.mark.parametrize("timestamp", [1700000000, -1])
def test_prior_second_awakening_time_suppresses_even_after_old_instance_is_gone(components, field, timestamp):
    before, targets = make_state(components)
    before = add_status(components, before, 17, **{field: timestamp})
    candidate = evolve(components, before, targets[0])
    updated, response = components.service.apply(before, candidate, 17)
    assert_empty_status(response)
    assert updated["evolution_rewards"] == ledger(17)
    assert updated["tables"]["card_status"] == before["tables"]["card_status"]
    assert not components.presents.rows(updated)


def test_baseline_keeps_history_for_cards_without_any_current_instance(components):
    before, targets = make_state(components)
    before = add_status(components, before, 22, B4=1700000000)
    updated, _ = components.service.apply(before, evolve(components, before, targets[0]), 17)
    assert updated["evolution_rewards"] == ledger(17, 22)
    assert all(unit["CardID"] != 22 for unit in updated["player_state"]["units"])
    assert next(row for row in components.status.read(updated) if row["CardID"] == 22)["B4"] == 1700000000


@pytest.mark.parametrize("field", ["A1", "A3", "A5", "A7", "B0", "B1", "B2"])
def test_other_status_values_are_not_used_as_second_awakening_receipts(components, field):
    before, targets = make_state(components)
    before = add_status(components, before, 17, **{field: 1700000000})
    updated, response = components.service.apply(before, evolve(components, before, targets[0]), 17)
    assert len(response) == 2 and len(components.presents.rows(updated)) == 1
    assert rows(response[1])[0][field] == 1700000000


def test_current_transaction_new_stage_and_new_date_do_not_poison_legacy_baseline(components):
    before, targets = make_state(components)
    candidate = evolve(components, before, targets[0])
    candidate = add_status(components, candidate, 17, B3=1700000000)
    updated, response = components.service.apply(before, candidate, 17)
    assert updated["evolution_rewards"] == ledger(17) and len(response) == 2
    assert rows(response[1])[0]["B3"] == 1700000000
    assert "evolution_rewards" not in before and "evolution_rewards" not in candidate


def test_existing_ledger_is_not_recomputed_from_newly_seen_history(components):
    before, targets = make_state(components, (17, 22))
    before = evolve(components, before, targets[1])
    before = add_status(components, before, 22, B3=1700000000)
    before["evolution_rewards"] = ledger()
    updated, _ = components.service.apply(before, evolve(components, before, targets[0]), 17)
    assert updated["evolution_rewards"] == ledger(17)


def test_existing_claim_is_a_noop_with_no_repeated_ppt_or_status(components, monkeypatch):
    before, targets = make_state(components)
    before["evolution_rewards"] = ledger(17)
    candidate = evolve(components, before, targets[0])
    monkeypatch.setattr(components.presents, "initial_unit_template", lambda _: pytest.fail("Repeated claim queried a template"))
    updated, response = components.service.apply(before, candidate, 17)
    assert_empty_status(response)
    assert updated == candidate
    assert updated["evolution_rewards"] is not candidate["evolution_rewards"]


def test_nonblack_returns_detached_candidate_without_creating_ledger(components):
    card_id = next(identifier for identifier in components.cards.card_ids if identifier > 1 and components.cards.card(identifier)["Rare"] == 3)
    before, targets = make_state(components, (card_id,))
    candidate = evolve(components, before, targets[0])
    updated, response = components.service.apply(before, candidate, card_id)
    assert_empty_status(response)
    assert updated == candidate
    assert "evolution_rewards" not in updated
    assert updated["tables"] is not candidate["tables"]


@pytest.mark.parametrize("bad", [None, [], {}, {"schema_version": True, "reward_scope": "card", "claimed": []},
    {"schema_version": 2, "reward_scope": "card", "claimed": []},
    {"schema_version": 1, "reward_scope": "instance", "claimed": []},
    {**ledger(), "extra": 1}, {**ledger(), "claimed": ()}, ledger(22, 17), ledger(17, 17), ledger(True),
    ledger(17.0), ledger("17"), ledger(0), ledger(-1), ledger(0x80000000), ledger(999999)])
def test_unknown_or_corrupt_reward_ledger_fails_without_changes(components, bad):
    before, targets = make_state(components)
    before["evolution_rewards"] = deepcopy(bad)
    candidate = evolve(components, before, targets[0])
    assert_failure_preserves(components, before, candidate, 17, "evolution_rewards_state_invalid")


@pytest.mark.parametrize("candidate_ledger", [ledger(), ledger(17)])
def test_candidate_cannot_introduce_or_replace_claims_outside_reward_transaction(components, candidate_ledger):
    before, targets = make_state(components)
    candidate = evolve(components, before, targets[0])
    candidate["evolution_rewards"] = candidate_ledger
    assert_failure_preserves(components, before, candidate, 17, "evolution_rewards_state_invalid")


@pytest.mark.parametrize("stage", [0, 1, 2, 5])
def test_first_claim_requires_an_actual_second_awakening_transition(components, stage):
    before, targets = make_state(components)
    candidate = deepcopy(before)
    candidate["player_state"]["units"][-1]["ClassChangeCount"] = stage
    assert_failure_preserves(components, before, candidate, 17, "evolution_reward_transition_invalid")


def test_replaced_instance_cannot_count_as_a_second_awakening(components):
    before, targets = make_state(components)
    candidate = evolve(components, before, targets[0])
    candidate["player_state"]["last_unique_id"] += 1
    candidate["player_state"]["units"][-1]["UniqueID"] = candidate["player_state"]["last_unique_id"]
    assert_failure_preserves(components, before, candidate, 17, "evolution_reward_transition_invalid")


def test_reward_identity_is_original_card_not_its_r18_image(components):
    before, targets = make_state(components, (498,))
    candidate = evolve(components, before, targets[0])
    assert_failure_preserves(components, before, candidate, 513, "evolution_reward_target_invalid")
    updated, response = components.service.apply(before, candidate, 498)
    assert updated["evolution_rewards"] == ledger(498)
    assert rows(response[0])[0]["A2"] == 453
    assert rows(response[1])[0]["CardID"] == 498


@pytest.mark.parametrize("card_id", [*sorted(LOCAL_TEMPLATE_CARDS.values()), 513])
def test_unavailable_initial_template_is_rejected_without_substituting_another_card(components, monkeypatch, card_id):
    monkeypatch.delitem(components.presents._initial_templates, card_id, raising=False)
    before, targets = make_state(components, (card_id,))
    candidate = evolve(components, before, targets[0])
    assert_failure_preserves(components, before, candidate, card_id, "present_template_unsupported")


def test_wrong_card_template_cannot_be_used_as_a_substitute(components, monkeypatch):
    before, targets = make_state(components)
    candidate = evolve(components, before, targets[0])
    other_template = components.presents.initial_unit_template(22)
    monkeypatch.setattr(components.presents, "initial_unit_template", lambda _: other_template)
    assert_failure_preserves(components, before, candidate, 17, "evolution_reward_template_invalid")


def test_full_mailbox_failure_preserves_both_inputs_and_claim_absence(components):
    before, targets = make_state(components)
    before, _ = components.presents.enqueue(before, [{"kind": "unit", "template_id": components.presents.initial_unit_template(17),
        "count": MAX_PRESENTS, "description": "既存の報酬"}])
    candidate = evolve(components, before, targets[0])
    assert_failure_preserves(components, before, candidate, 17, "present_capacity")


def test_exception_after_enqueue_cannot_leak_mail_or_claims(components, monkeypatch):
    before, targets = make_state(components)
    candidate = evolve(components, before, targets[0])
    def fail(_):
        raise ProtocolError("card_status_capacity_exceeded", "Test capacity", 409)
    monkeypatch.setattr(components.status, "flat_response", fail)
    assert_failure_preserves(components, before, candidate, 17, "card_status_capacity_exceeded")


def test_even_a_mutating_failed_dependency_only_sees_detached_candidate(components, monkeypatch):
    before, targets = make_state(components)
    candidate = evolve(components, before, targets[0])
    def fail(state, rewards):
        state["evolution_rewards"]["claimed"].append(22)
        state["tables"]["presents"].append({"invalid": True})
        raise ProtocolError("present_capacity", "Test capacity", 409)
    monkeypatch.setattr(components.presents, "enqueue", fail)
    assert_failure_preserves(components, before, candidate, 17, "present_capacity")


def test_old_card_status_schema_is_not_silently_discarded(components):
    before, targets = make_state(components)
    before["tables"]["card_status"] = [{"CardID": 17, "B3": 1700000000}]
    candidate = evolve(components, before, targets[0])
    assert_failure_preserves(components, before, candidate, 17, "card_status_schema_unavailable")


def test_content_version_includes_present_assets_and_supported_template_set(components, monkeypatch):
    original = components.service.content_version
    assert re.fullmatch(r"[a-f0-9]{64}", original)
    monkeypatch.setattr(components.presents, "asset_sha256", "0" * 64)
    changed_asset = EvolutionRewards(components.cards, components.players, components.status, components.presents).content_version
    assert changed_asset != original
    status = components.presents.status()
    status["supported_templates"] = status["supported_templates"][:-1]
    monkeypatch.setattr(components.presents, "status", lambda: status)
    changed_templates = EvolutionRewards(components.cards, components.players, components.status, components.presents).content_version
    assert changed_templates != changed_asset


def test_status_reports_real_supported_templates_and_known_gaps(components):
    status = components.service.status()
    template_ids = components.presents.status()["supported_templates"]
    assert len(template_ids) > 4
    assert all(components.cards.present(identifier)["PresentID"] == identifier for identifier in template_ids)
    assert status["available"] is True and status["verified_templates"] == len(template_ids)
    assert status["content_version"] == components.service.content_version
    assert status["known_template_gaps"] == []
    assert status["policy"]["reward_scope"] == "card"
    assert status["policy"]["legacy_status_fields"] == ["B3", "B4"]


def test_known_gaps_are_the_local_template_cards_left_without_a_supported_template(components, monkeypatch):
    status = components.presents.status()
    status["supported_templates"] = [identifier for identifier in status["supported_templates"]
                                     if identifier not in LOCAL_TEMPLATE_CARDS]
    monkeypatch.setattr(components.presents, "status", lambda: status)
    rewards = partial(EvolutionRewards, components.cards, components.players, components.status, components.presents)
    assert rewards().status()["known_template_gaps"] == sorted(LOCAL_TEMPLATE_CARDS.values())
    template_id, card_id = min(LOCAL_TEMPLATE_CARDS.items())
    monkeypatch.setattr(evolution_rewards, "LOCAL_TEMPLATE_CARDS", {template_id: card_id})
    assert rewards().status()["known_template_gaps"] == [card_id]


def test_status_is_cached_and_detached(components, monkeypatch):
    status = components.service.status()
    status["policy"]["legacy_status_fields"].append("A1")
    status["known_template_gaps"].append(99999)
    monkeypatch.setattr(components.presents, "status", lambda: pytest.fail("Status rescanned present resources"))
    assert components.service.status()["policy"]["legacy_status_fields"] == ["B3", "B4"]
    assert components.service.status()["known_template_gaps"] == []


def test_initialize_seeds_current_unit_and_historical_status_without_other_changes(components):
    state, targets = make_state(components)
    state = evolve(components, state, targets[0])
    state = add_status(components, state, 22, B4=1700000000)
    original = deepcopy(state)
    updated = components.service.initialize(state)
    expected = deepcopy(state)
    expected["evolution_rewards"] = ledger(17, 22)
    assert updated == expected and state == original
    assert updated["tables"] is not state["tables"]
    assert updated["player_state"]["units"] is not state["player_state"]["units"]
    assert components.presents.rows(updated) == components.presents.rows(state) == []


def test_initialize_empty_history_does_not_mark_unawakened_owned_cards(components):
    state, _ = make_state(components, (17, 22))
    state = add_status(components, state, 17, A1=1, A7=7, B2=1700000000)
    updated = components.service.initialize(state)
    assert updated["evolution_rewards"] == ledger()
    assert "evolution_rewards" not in state


def test_initialize_existing_ledger_only_validates_without_recomputing(components):
    state, targets = make_state(components)
    state = evolve(components, state, targets[0], 4)
    state = add_status(components, state, 22, B3=1700000000)
    state["evolution_rewards"] = ledger()
    original = deepcopy(state)
    updated = components.service.initialize(state)
    assert updated == original == state
    assert updated["evolution_rewards"] is not state["evolution_rewards"]


def test_eager_initialization_remembers_unit_consumed_before_first_new_awakening(components):
    state, targets = make_state(components)
    state = evolve(components, state, targets[0])
    initialized = components.service.initialize(state)
    assert initialized["evolution_rewards"] == ledger(17)
    initialized["player_state"]["units"] = [unit for unit in initialized["player_state"]["units"] if unit["CardID"] != 17]
    reacquired, units = components.players.credit_units(initialized, [{"card_id": 17, "level": 1, "count": 1}])
    reacquired["player_state"]["units"][-1]["ClassChangeCount"] = 2
    candidate = evolve(components, reacquired, units[0]["GetIndex"])
    updated, response = components.service.apply(reacquired, candidate, 17)
    assert_empty_status(response)
    assert updated["evolution_rewards"] == ledger(17)
    assert components.presents.rows(updated) == []


@pytest.mark.parametrize("operation", ["read", "initialize"])
@pytest.mark.parametrize("bad", [None, {}, {"schema_version": 2, "reward_scope": "card", "claimed": []}, ledger(22, 17),
                                 ledger(True), {**ledger(), "extra": 1}, ledger(17, 17), ledger(999999)])
def test_reads_and_initialization_do_not_replace_invalid_existing_ledger(components, operation, bad):
    state, _ = make_state(components)
    state["evolution_rewards"] = deepcopy(bad)
    original = deepcopy(state)
    with pytest.raises(ProtocolError) as error:
        getattr(components.service, operation)(state)
    assert (error.value.code, error.value.status) == ("evolution_rewards_state_invalid", 503) and state == original


@pytest.mark.parametrize("operation", ["read", "initialize"])
@pytest.mark.parametrize("state", [None, [], "state"])
def test_reads_and_initialization_reject_nonstate_inputs(components, operation, state):
    with pytest.raises(ProtocolError) as error:
        getattr(components.service, operation)(state)
    assert error.value.code == "evolution_rewards_state_invalid"


def test_ledger_key_is_the_saved_ledger_name():
    assert evolution_rewards.LEDGER_KEY == "evolution_rewards"


@pytest.mark.parametrize("stored", [None, ledger(), ledger(17, 22)])
def test_read_returns_the_ledger_initialize_would_save(components, stored):
    state, targets = make_state(components, (17, 22))
    state = evolve(components, state, targets[0])
    state = add_status(components, state, 22, B4=1700000000)
    if stored is not None:
        state[evolution_rewards.LEDGER_KEY] = deepcopy(stored)
    original = deepcopy(state)
    read = components.service.read(state)
    assert read == components.service.initialize(state)[evolution_rewards.LEDGER_KEY]
    assert read == (ledger(17, 22) if stored is None else stored)
    assert state == original


def test_read_result_is_detached_from_the_save(components):
    state, _ = make_state(components)
    state[evolution_rewards.LEDGER_KEY] = ledger(17)
    original = deepcopy(state)
    read = components.service.read(state)
    read["claimed"].append(22)
    read["schema_version"] = 2
    assert state == original
    assert components.service.read(state) == ledger(17)


@pytest.mark.parametrize("stored", [False, True])
def test_read_copies_only_the_ledger_never_the_whole_save(components, monkeypatch, stored):
    state, targets = make_state(components)
    state = evolve(components, state, targets[0])
    if stored:
        state[evolution_rewards.LEDGER_KEY] = ledger(17)
    copied, real = [], evolution_rewards.deepcopy

    def recording(value, *args):
        copied.append(value)
        return real(value, *args)

    monkeypatch.setattr(evolution_rewards, "deepcopy", recording)
    assert components.service.read(state) == ledger(17)
    saved = state.get(evolution_rewards.LEDGER_KEY)
    assert [value is saved for value in copied] == ([True] if stored else [])
    components.service.initialize(state)
    assert any(value is state for value in copied)


def test_consecutive_first_rewards_then_nonfirst_reset_only_the_temporary_status(components):
    before, targets = make_state(components, (17, 22, 17))
    first, first_response = components.service.apply(before, evolve(components, before, targets[0]), 17)
    second, second_response = components.service.apply(first, evolve(components, first, targets[1]), 22)
    for card_id, response in ((17, first_response), (22, second_response)):
        assert tuple(node.name for node in response) == ("PPT", "CardStatus")
        assert tuple(column.name for column in response[1].children) == CARD_STATUS_FIELDS
        assert rows(response[1])[0]["CardID"] == card_id and "A1" in rows(response[1])[0]
    saved_status = components.status.read(second)
    final, final_response = components.service.apply(second, evolve(components, second, targets[2]), 17)
    assert_empty_status(final_response)
    assert components.status.read(final) == saved_status
    assert {row["CardID"] for row in saved_status} == {17, 22}
    assert len(components.presents.rows(final)) == 2 and final["evolution_rewards"] == ledger(17, 22)


def test_nonblack_after_first_reward_also_returns_bare_status_reset(components):
    other = next(identifier for identifier in components.cards.card_ids
                 if identifier > 1 and components.cards.card(identifier)["Rare"] == 3)
    before, targets = make_state(components, (17, other))
    first, first_response = components.service.apply(before, evolve(components, before, targets[0]), 17)
    assert "A1" in rows(first_response[1])[0]
    candidate = evolve(components, first, targets[1])
    updated, response = components.service.apply(first, candidate, other)
    assert_empty_status(response)
    assert updated == candidate and components.status.read(updated) == components.status.read(first)
