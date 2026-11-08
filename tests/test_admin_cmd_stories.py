from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from aigis_ps import admin_cmd_stories, admin_views_settings
from aigis_ps.admin_cmd_stories import GIFT_SERVICE_REASON, STORIES, STORY_INDICES, STORY_WRITES, mask, story_bits
from aigis_ps.admin_command import AdminError, Batch, BatchStep, CommandContext, PlanStore, missing_service
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck, requires_backup
from aigis_ps.admin_fields import BIT_TABLES, GIFT_LEDGER, GIFTS, STORY_BITS
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, ViewRegistry
from aigis_ps.admin_views_settings import NO_COMMAND, UNCONFIRMED
from aigis_ps.card_status import CARD_STATUS_FIELDS, THIRD_EPISODE, CardStatusService
from aigis_ps.energy_clock import instant
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_admin_preflight import none_reasons, unread_reasons
from test_card_status import snapshot
from test_quest_service import bundle


NOW = datetime(2026, 11, 3, 3, tzinfo=timezone.utc)
CARDS = (1, 4, 67)
UNKNOWN_CARD = 99
HIGH_BIT = 1 << 5
REGISTERED = 1_700_000_000


def row(identifier, **changes):
    return {**dict.fromkeys(CARD_STATUS_FIELDS, 0), "CardID": identifier, **changes}


def saved(*rows, **extra):
    state = {"tables": {"card_status": list(rows), "quest_records": [{"QuestID": 1001, "MaxRank": 3}],
                        "perused_events": [{"ID": "event_a", "FLG": 1}]},
             "player_state": {"units": [{"CardID": 1, "Love": 80, "WatchLoveEv": 1}]}, "card_status_schema": 1}
    state.update(extra)
    return state


class Gifts:
    def __init__(self):
        self.revisions = []

    def preserve_history(self, state, revision):
        self.revisions.append(revision)
        baseline = {str(entry["CardID"]): entry["A7"] for entry in state["tables"]["card_status"]}
        return {**deepcopy(state), GIFT_LEDGER: {"baseline": baseline}}


@pytest.fixture
def services():
    return SimpleNamespace(card_status=CardStatusService(snapshot(CARDS)), gifts=Gifts())


def context(services, revision=0):
    return CommandContext(now=NOW, acquisition_day=1, revision=revision, generation_id="gen-stories", services=services)


def run(state, raw, services, revision=0):
    outcome = Batch((BatchStep(STORIES, STORIES.parse(raw)),)).apply(state, context(services, revision))
    return outcome.state, outcome.steps[0]


def refused(state, raw, services):
    with pytest.raises(AdminError) as caught:
        run(state, raw, services)
    return caught.value


def stored(state):
    return {entry["CardID"]: entry for entry in state["tables"]["card_status"]}


def test_story_command_owns_only_the_verified_story_bits():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_stories.register(commands)
    assert [command.operation for command in commands] == ["stories.set_unlock"]
    assert STORIES.writes == STORY_WRITES == frozenset({"card_status.A7"})
    assert STORIES.touched == frozenset({"card_status", GIFTS}) and STORIES.destructive_by_params
    assert STORIES.is_destructive(STORIES.parse({"cards": [{"CardID": 1, "set_bits": [0]}]})) is False
    assert STORIES.is_destructive(STORIES.parse({"cards": [{"CardID": 1, "clear_bits": [0]}]})) is True
    assert STORIES.is_destructive(STORIES.parse({"cards": [{"CardID": 1, "set_bits": [0]},
                                                           {"CardID": 4, "clear_bits": [1]}]})) is True
    assert not STORIES.preflight_none and STORIES.touched <= PREFLIGHT_DOMAINS.keys()
    assert none_reasons(STORIES.touched) == unread_reasons(GIFTS)
    assert BIT_TABLES[next(iter(STORY_WRITES))] is STORY_BITS
    assert STORY_INDICES == (0, 1, 2) and THIRD_EPISODE == 1 << 2
    assert [(bit.mask, bit.name, bit.level) for bit in STORY_BITS.bits] == [
        (1, "first_episode", "verified"), (2, "second_episode", "verified"), (4, "harlem_episode", "verified")]
    schema = STORIES.describe()["params_schema"]["$defs"]["StoryChange"]["properties"]
    assert schema["set_bits"]["items"]["enum"] == schema["clear_bits"]["items"]["enum"] == [0, 1, 2]


def test_unlock_sets_the_named_bits_and_keeps_every_other_bit(services):
    state = saved(row(1, A7=HIGH_BIT | 2, A1=3, A3=4, A5=5, B0=REGISTERED))
    updated, step = run(state, {"cards": [{"CardID": 1, "set_bits": [0]}, {"CardID": 4, "set_bits": [2, 0]}]}, services)
    assert stored(updated) == {1: row(1, A7=HIGH_BIT | 3, A1=3, A3=4, A5=5, B0=REGISTERED), 4: row(4, A7=5)}
    assert updated["card_status_schema"] == 1
    assert step.effects == {"cards": [{"CardID": 1, "before": HIGH_BIT | 2, "after": HIGH_BIT | 3, "set": [0], "cleared": []},
                                      {"CardID": 4, "before": 0, "after": 5, "set": [0, 2], "cleared": []}],
                            "gift_baseline_frozen": True}


@pytest.mark.parametrize("before, clear, after", [(-1, [2], -5), (7, [0, 1], 4), (HIGH_BIT | 4, [2], HIGH_BIT), (1, [1], 1)])
def test_clear_removes_only_the_named_bits(services, before, clear, after):
    updated, step = run(saved(row(67, A7=before)), {"cards": [{"CardID": 67, "clear_bits": clear}]}, services)
    assert stored(updated)[67]["A7"] == after
    assert step.effects["cards"][0]["cleared"] == story_bits(before & ~after)


def test_set_and_clear_combine_on_one_card(services):
    updated, step = run(saved(row(1, A7=2)), {"cards": [{"CardID": 1, "set_bits": [2], "clear_bits": [1]}]}, services)
    assert stored(updated)[1]["A7"] == 4
    assert (step.effects["cards"][0]["set"], step.effects["cards"][0]["cleared"]) == ([2], [1])
    assert mask([0, 2]) == 5 and story_bits(-1) == [0, 1, 2]


@pytest.mark.parametrize("bit", [3, 5, 31, -1, True, "0", 1.0, None])
def test_only_story_bits_zero_to_two_are_accepted(services, bit):
    for name in ("set_bits", "clear_bits"):
        error = refused(saved(), {"cards": [{"CardID": 1, name: [bit]}]}, services)
        assert (error.code, error.status) == ("admin_params_invalid", 422)


@pytest.mark.parametrize("raw", [
    {"cards": [{"CardID": 1}]},
    {"cards": [{"CardID": 1, "set_bits": [], "clear_bits": []}]},
    {"cards": [{"CardID": 1, "set_bits": [0], "clear_bits": [0]}]},
    {"cards": [{"CardID": 1, "set_bits": [1, 1]}]},
    {"cards": [{"CardID": 1, "set_bits": [0]}, {"CardID": 1, "clear_bits": [1]}]},
    {"cards": [{"set_bits": [0]}]},
    {"cards": [{"CardID": 1, "set_bits": [0], "WatchLoveEv": 3}]},
    {"cards": [{"CardID": "1", "set_bits": [0]}]},
    {"cards": []},
    {"cards": [{"CardID": 1, "set_bits": [0]}], "A1": 1},
])
def test_story_changes_must_be_unambiguous(services, raw):
    error = refused(saved(), raw, services)
    assert (error.code, error.status) == ("admin_params_invalid", 422)


@pytest.mark.parametrize("change", [{"set_bits": [0]}, {"clear_bits": [0]}])
def test_unknown_cards_are_refused_even_when_nothing_changes(services, change):
    error = refused(saved(), {"cards": [{"CardID": UNKNOWN_CARD, **change}]}, services)
    assert (error.code, error.status) == ("card_status_patch_invalid", 400)
    error = refused(saved(), {"cards": [{"CardID": 2 ** 31, **change}]}, services)
    assert (error.code, error.status) == ("card_status_patch_invalid", 400)


def test_clearing_a_card_without_a_row_changes_nothing(services):
    state = saved(row(1, A7=1))
    updated, step = run(state, {"cards": [{"CardID": 4, "clear_bits": [0, 1, 2]}]}, services)
    assert updated == state
    assert step.effects == {"cards": [{"CardID": 4, "before": 0, "after": 0, "set": [], "cleared": []}],
                            "gift_baseline_frozen": False}


def test_story_edits_leave_love_reads_records_and_rewards_alone(services):
    state = saved(row(1, A7=1), gift_state={"claims": {"1": [1]}}, presents_schema=1)
    updated, _ = run(state, {"cards": [{"CardID": 1, "set_bits": [1, 2]}, {"CardID": 67, "set_bits": [0]}]}, services)
    untouched = {key: value for key, value in state.items() if key != "tables"}
    assert {key: value for key, value in updated.items() if key != "tables"} == untouched
    assert {key: value for key, value in updated["tables"].items() if key != "card_status"} == {
        key: value for key, value in state["tables"].items() if key != "card_status"}
    assert stored(updated)[1] == row(1, A7=7)


@pytest.mark.parametrize("raw, frozen", [
    ({"cards": [{"CardID": 1, "set_bits": [0]}]}, True),
    ({"cards": [{"CardID": 1, "clear_bits": [1]}]}, True),
    ({"cards": [{"CardID": 1, "set_bits": [1]}]}, False),
    ({"cards": [{"CardID": 1, "set_bits": [2]}]}, False),
    ({"cards": [{"CardID": 4, "clear_bits": [0]}]}, False),
])
def test_gift_baseline_bits_freeze_the_trust_ledger_from_the_unedited_save(services, raw, frozen):
    updated, step = run(saved(row(1, A7=2)), raw, services, revision=7)
    assert services.gifts.revisions == ([7] if frozen else [])
    assert updated.get(GIFT_LEDGER) == ({"baseline": {"1": 2}} if frozen else None)
    assert step.effects["gift_baseline_frozen"] is frozen and step.warnings == ()
    ledger = {"claims": {"1": [1]}}
    updated, step = run(saved(row(1, A7=2), **{GIFT_LEDGER: ledger}), raw, services)
    assert updated[GIFT_LEDGER] == ledger and step.effects["gift_baseline_frozen"] is False
    assert services.gifts.revisions == ([7] if frozen else [])


def test_only_a_baseline_freeze_needs_the_gift_service(services):
    services.gifts = None
    error = refused(saved(row(1, A7=2)), {"cards": [{"CardID": 1, "clear_bits": [1]}]}, services)
    assert (error.code, error.status, str(error)) == ("admin_command_unavailable", 503, GIFT_SERVICE_REASON)
    error = refused(saved(), {"cards": [{"CardID": UNKNOWN_CARD, "set_bits": [0]}]}, services)
    assert (error.code, error.status) == ("card_status_patch_invalid", 400)
    assert STORIES.availability(services) is None
    updated, _ = run(saved(row(1, A7=2)), {"cards": [{"CardID": 1, "set_bits": [2]}]}, services)
    assert stored(updated)[1]["A7"] == 6
    updated, _ = run(saved(row(1, A7=2), **{GIFT_LEDGER: {"claims": {}}}), {"cards": [{"CardID": 1, "clear_bits": [1]}]},
                     services)
    assert stored(updated)[1]["A7"] == 0


def test_confirmation_phrases_name_the_destructive_direction():
    phrase = STORIES.phrase
    assert phrase(STORIES.parse({"cards": [{"CardID": 1, "set_bits": [0]}]})) == "UNLOCK STORIES 1 CARD"
    assert phrase(STORIES.parse({"cards": [{"CardID": 1, "set_bits": [0]}, {"CardID": 4, "set_bits": [1]}]})) == (
        "UNLOCK STORIES 2 CARDS")
    assert phrase(STORIES.parse({"cards": [{"CardID": 1, "set_bits": [0]}, {"CardID": 4, "clear_bits": [1]}]})) == (
        "CLEAR STORIES 2 CARDS")


def test_an_unlock_without_a_clear_needs_no_confirmation_and_no_backup():
    unlock = Batch((BatchStep(STORIES, STORIES.parse({"cards": [{"CardID": 1, "set_bits": [0, 1]}]})),))
    assert unlock.confirmations() == () and not requires_backup(unlock)
    clear = Batch((BatchStep(STORIES, STORIES.parse({"cards": [{"CardID": 1, "clear_bits": [2]}]})),))
    assert [item.phrase for item in clear.confirmations()] == ["CLEAR STORIES 1 CARD"]
    assert requires_backup(clear)


def test_story_command_needs_the_card_status_service(services):
    assert STORIES.availability(services) is None
    assert STORIES.availability(SimpleNamespace(card_status=None)) == missing_service("card_status")
    error = refused(saved(), {"cards": [{"CardID": 1, "set_bits": [0]}]}, SimpleNamespace(card_status=None))
    assert (error.code, error.status) == ("admin_command_unavailable", 503)


def test_story_edits_commit_with_their_phrase_a_backup_and_the_51_readback(services):
    store = FakeStore(saved(row(1, A7=1)))
    initializer = lambda state: (services.card_status.read(state), deepcopy(state))[1]
    runtime = FakeRuntime(FakeGeneration(id="gen-stories", bundle=services, content_versions={}, clock=NOW))
    pipeline = CommitPipeline(store, PlanStore(), GenerationPreflight(), InitializerRestartCheck(initializer), runtime,
                              backup_directory=Path("backups"))
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_stories.register(commands)
    plan = pipeline.preview(commands.batch([{"operation": STORIES.operation,
                                             "params": {"cards": [{"CardID": 1, "clear_bits": [0]}]}}]))
    assert plan.committable and requires_backup(plan.batch)
    assert [item.phrase for item in plan.confirmations] == ["CLEAR STORIES 1 CARD"]
    arguments = dict(plan_id=plan.plan_id, plan_digest=plan.plan_digest, expected_revision=0, op_id=str(uuid.uuid4()))
    with pytest.raises(AdminError) as caught:
        pipeline.commit(**arguments, confirmations=("UNLOCK STORIES 1 CARD",))
    assert caught.value.code == "admin_confirmation_required" and store.revision == 0
    outcome = pipeline.commit(**arguments, confirmations=("CLEAR STORIES 1 CARD",))
    assert outcome.result["readback"] == {"card_status": [51]} and outcome.result["backup_before_commit"] is not None
    assert store.revision == 1 and stored(store.state)[1]["A7"] == 0 and store.backups[0]["commits_before"] == 0
    assert store.state[GIFT_LEDGER] == {"baseline": {"1": 1}} and plan.describe()["touched"] == ["card_status", GIFTS]


def test_card_status_view_decodes_story_bits_and_registration_times(services):
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_stories.register(commands)
    views = ViewRegistry()
    admin_views_settings.register(views)
    state = saved(row(1, A7=HIGH_BIT | 5, A1=9, B1=REGISTERED), row(4))
    request = SimpleNamespace(services=SimpleNamespace(catalog=SimpleNamespace(commands=commands)),
                              generation=SimpleNamespace(bundle=services), state=state, now=NOW, revision=0)
    view = views.get("settings.card_status")
    result = view.read(request, view.params_model(limit=1))
    assert result["fields"]["A7"] == {"owner": "stories.set_unlock", "read_only": False, "reason": None}
    assert all(result["fields"][name] == {"owner": None, "read_only": True, "reason": UNCONFIRMED} for name in ("A1", "A3", "A5"))
    assert result["fields"]["B0"] == {"owner": None, "read_only": True, "reason": NO_COMMAND}
    assert result["story_bits"] == STORY_BITS.describe()
    rows = result["rows"]
    assert (rows["ok"], rows["total"], rows["next_offset"]) == (True, 2, 1)
    decoded = rows["items"][0]
    assert decoded["A7"] == {"value": HIGH_BIT | 5, "bits": [
        {"bit": 0, "name": "first_episode", "level": "verified", "set": True},
        {"bit": 1, "name": "second_episode", "level": "verified", "set": False},
        {"bit": 2, "name": "harlem_episode", "level": "verified", "set": True},
        {"bit": 5, "name": None, "level": "unknown", "set": True}]}
    assert decoded["B1"] == {"value": REGISTERED, "utc": "2023-11-14T22:13:20+00:00"}
    assert decoded["B0"] == {"value": 0, "utc": None} and decoded["A1"] == {"value": 9}
    request.generation.bundle = SimpleNamespace()
    assert view.read(request, view.params_model())["rows"]["error"]["code"] == "admin_view_service_unavailable"


def test_live_story_unlock_freezes_the_real_trust_baseline_and_passes_the_real_checks(bundle):
    state = bundle.initialize(bundle.bootstrap.initial_state())
    card = state["player_state"]["units"][0]["CardID"]
    assert GIFT_LEDGER not in state
    store = FakeStore(state)
    generation = FakeGeneration(id="gen-live", bundle=bundle, content_versions={}, clock=instant(bundle.bootstrap.clock()))
    pipeline = CommitPipeline(store, PlanStore(), GenerationPreflight(), InitializerRestartCheck(bundle.initialize),
                              FakeRuntime(generation), backup_directory=Path("backups"))
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_stories.register(commands)
    plan = pipeline.preview(commands.batch([{"operation": STORIES.operation,
                                             "params": {"cards": [{"CardID": card, "set_bits": [0, 1]}]}}]))
    assert plan.committable, plan.describe()
    # An unlock clears nothing, so the live commit needs neither a phrase nor a backup.
    assert plan.confirmations == () and not requires_backup(plan.batch)
    outcome = pipeline.commit(plan_id=plan.plan_id, plan_digest=plan.plan_digest, expected_revision=0,
                              op_id=str(uuid.uuid4()))
    assert outcome.result["readback"] == {"card_status": [51]}
    assert outcome.result["backup_before_commit"] is None and store.backups == []
    assert {entry["CardID"]: entry["A7"] for entry in store.state["tables"]["card_status"]}[card] == 3
    assert store.state[GIFT_LEDGER] == bundle.gifts.preserve_history(state, 0)[GIFT_LEDGER]
    # Without the freeze, the first gift would read the new bits as trust tiers the card had already claimed.
    edited = bundle.card_status.update(state, [{"CardID": card, "A7": 3}])
    assert bundle.gifts.preserve_history(edited, 0)[GIFT_LEDGER]["claims"] != store.state[GIFT_LEDGER]["claims"]
