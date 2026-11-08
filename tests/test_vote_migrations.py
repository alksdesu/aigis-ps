import ast
from copy import copy, deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigis_ps import admin_cmd_vote_migrations
from aigis_ps.admin_cmd_vote_migrations import (ADOPT_COOKING, ADOPT_REWARDS, ADOPT_REWARDS_OPERATION, ADOPT_VOTES,
                                                ADOPT_VOTE_OPERATION, COMMANDS, COOKING_ADOPTION_UNNEEDED,
                                                COOKING_PHRASE, COOKING_STILL_PENDING, DEFINITION_PHRASE,
                                                ESTABLISH_CAPACITY, MONTHS_PENDING, NO_CURRENT_GROUP, NOTICE_DROPPED,
                                                REOPEN_SETTLED, REWARD_ADOPTION_UNNEEDED, REWARDS_PHRASE,
                                                REWARDS_STILL_BOUND, STAMINA_CLEARED, STILL_REAPPEARED,
                                                TRANSACTIONS_REASON, VOTE_ADOPTION_UNNEEDED, VOTE_GROUP_UNKNOWN,
                                                VOTES_STILL_BOUND)
from aigis_ps.admin_command import AdminError, PlanStore, missing_service
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, discover
from aigis_ps.alxml import Column, loads
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.monthly_capacity import (BASELINE_REQUIRED, BASELINE_UNNEEDED, CHARISMA_FIELDS, PERIOD_AHEAD, STAMINA_FIELDS,
                                       read_capacity)
from aigis_ps.presents import MAX_PRESENTS
from aigis_ps.storage import AUTO_BACKUP_KIND
from aigis_ps.vote_cooking_rules import VoteCookingRules
from aigis_ps.vote_cooking_service import PENDING as COOKING_PENDING, SUPERSEDED_KEY, VoteCookingService
from aigis_ps.vote_expiry import SETTLED as EXPIRY_SETTLED, VoteExpiryNotifications
from aigis_ps.vote_finalists import REFREEZE_REFUSED
from aigis_ps.vote_rewards import ARCHIVED_KEY
from aigis_ps.vote_rules import VoteRules
from aigis_ps.vote_service import VoteService
from aigis_ps.vote_state import adopt_current, read_vote_history, record_vote
from test_admin_cmd_storage import commit, planned, preview_error
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_admin_seams import MemoryStore
from test_quest_service import bundle, request
from test_vote_catalog import build, vote_sources
from test_vote_service import table_rows


REVIVAL = "2026-11-01T15:00:00+09:00"
GENERATION = "gen-vote-migrations"
ITEM = "item_vote_016001"
UNKNOWN_ITEM = "item_vote_099001"
ONE_TICKET, TWO_TICKETS = "MIGRATE 1 VOTE TICKET", "MIGRATE 2 VOTE TICKETS"
EVENT = 16
VOTE_READ, VOTE_SUBMIT = 55, 57
TICKETS, REAPPEARED = 999, 2
DEFAULT_INTERVAL, FASTER_INTERVAL = 15, 10
STRICTER_MINIMUM = 2
EARLIER = "e" * 64
CAPACITY_NOW = datetime(2026, 11, 10, 12, tzinfo=JST)
BUSINESS_MONTH, EARLIER_MONTH, LATER_MONTH = "2026-11", "2026-10", "2026-12"
LEGACY_CHARISMA, LEGACY_NEXT, LEGACY_STAMINA = 30, 20, 5
DOMAINS = {"votes.reopen_settled": "votes", "votes.adopt_reward_definition": "votes",
           "votes.adopt_cooking_definition": "votes", "votes.adopt_vote_definition": "votes",
           "sugoroku.establish_capacity_baseline": "events"}
FILLER = {"kind": "gold", "amount": 1, "description": "Filler"}


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture(scope="module")
def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_vote_migrations.register(commands)
    return commands


@pytest.fixture(scope="module")
def catalogs(vote_sources):
    return SimpleNamespace(revival=build(vote_sources, VoteRules(revival_start=REVIVAL)),
                           faster=build(vote_sources, VoteRules(revival_start=REVIVAL, earned_votes=FASTER_INTERVAL)))


@pytest.fixture
def voting(bundle, catalogs):
    assert bundle.vote_cooking is not None, bundle.vote_cooking_error

    def create(catalog=None, cooking_rules=None):
        catalog = catalogs.revival if catalog is None else catalog
        bootstrap, presents = copy(bundle.bootstrap), copy(bundle.presents)
        presents.bootstrap = bootstrap
        group = catalog.groups[0]
        clock = SimpleNamespace(now=group.vote_start + timedelta(hours=1))
        bootstrap.clock = lambda: clock.now
        cooking = VoteCookingService(catalog, bundle.vote_cooking.recipes, presents, cooking_rules)
        service = VoteService(bootstrap, bundle.player_state, catalog, possessions=bundle.possessions, presents=presents,
                              cooking=cooking)
        bootstrap.vote_provider, bootstrap.present_provider = service.response, presents.response
        notifier = VoteExpiryNotifications(service.transactions.expiry)
        services = SimpleNamespace(bootstrap=bootstrap, player_state=bundle.player_state, presents=presents,
                                   votes=service, vote_cooking=cooking, vote_expiry_notifier=notifier,
                                   initialize=lambda state: notifier.initialize(service.initialize(state)))
        card = next(candidate.card_id for candidate in catalog.candidates if candidate.group_id == group.group_id)
        return SimpleNamespace(services=services, service=service, transactions=service.transactions, cooking=cooking,
                               notifier=notifier, presents=presents, catalog=catalog, group=group, card=card,
                               clock=clock, bootstrap=bootstrap)
    return create


@pytest.fixture
def saved(bundle):
    def create(voter, *, tickets=TICKETS):
        initial = bundle.player_state.initialize(voter.bootstrap.initial_state())
        initial["tables"]["quest_records"] = [{"QuestID": 1004, "MaxRank": 3, "AchieveFlag": 1, "RemainAssault": 0,
                                               "FirstDefeatDate": 0, "Shown": False}]
        return voter.service.initialize(bundle.possessions.credit(initial, {voter.group.item_id: tickets}))
    return create


def console(services, state, preflight, now):
    store = FakeStore(state)
    generation = FakeGeneration(id=GENERATION, bundle=services, clock=now)
    return store, CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                                 backup_directory=Path("backups"))


def migrate(services, state, preflight, registry, operation, params, now):
    store, pipeline = console(services, state, preflight, now)
    plan = planned(pipeline, registry, operation, params)
    command = registry.get(operation)
    phrase = command.phrase(command.parse(params))
    assert [item.describe() for item in plan.confirmations] == [{"step": 0, "operation": operation, "phrase": phrase}]
    assert plan.restart_check == {"ok": True}
    assert set(plan.preflight["readback"]) == {name for name in command.touched if PREFLIGHT_DOMAINS[name].reads}
    with pytest.raises(AdminError) as caught:
        commit(pipeline, plan)
    assert caught.value.code == "admin_confirmation_required" and store.calls == [] and store.backups == []
    commit(pipeline, plan, (phrase,))
    [backup] = store.backups
    assert backup["row"]["kind"] == AUTO_BACKUP_KIND and backup["state"] == state and store.revision == 1
    [step] = plan.describe()["steps"]
    return store.state, step


def refused(services, state, preflight, registry, operation, params, now, code, status):
    store, pipeline = console(services, state, preflight, now)
    preview_error(pipeline, registry, operation, params, code, status)
    assert store.calls == [] and store.backups == []


def ballot(store, voter, count, *, tick):
    columns = (Column("GID", "I", (voter.group.group_id,)), Column("CID", "I", (voter.card,)),
               Column("CNT", "I", (count,)))
    payload = voter.service.handlers()[VOTE_SUBMIT](request(store, VOTE_SUBMIT, columns, tick=tick), store)
    return {node.name: node for node in loads(payload).children}


def directory(store, voter, *, tick):
    payload = voter.service.handlers()[VOTE_READ](request(store, VOTE_READ, (), tick=tick), store)
    return {node.name: node for node in loads(payload).children}


def voted(tables, voter):
    return next(row["Count"] for row in table_rows(tables["VCC"])
                if (row["CardID"], row["GroupID"]) == (voter.card, voter.group.group_id))


def refusal(action):
    with pytest.raises(ProtocolError) as caught:
        action()
    return caught.value.code, caught.value.status


def cast(voter, state, count):
    previous = next((row["count"] for row in read_vote_history(state)
                     if row["itemId"] == voter.group.item_id and row["cardId"] == voter.card), 0)
    after = record_vote(state, item_id=voter.group.item_id, group_id=voter.group.group_id, card_id=voter.card,
                        count=previous + count, definition=voter.transactions.rewards.group_definition(voter.group))
    mailed, _, _ = voter.transactions.rewards.apply_vote(state, after, voter.group)
    return voter.cooking.record_vote(state, mailed, voter.group, voter.clock.now)


def filled(voter, state):
    return voter.presents.enqueue(state, [FILLER] * (MAX_PRESENTS - len(voter.presents.rows(state))))[0]


def test_migration_commands_are_discovered_destructive_and_preflighted():
    catalog = discover(PREFLIGHT_DOMAINS)
    assert "admin_cmd_vote_migrations" in catalog.modules
    assert {command.operation for command in COMMANDS} <= {command.operation for command in catalog.commands}
    assert {command.operation: command.domain for command in COMMANDS} == DOMAINS
    for command in COMMANDS:
        assert command.touched <= PREFLIGHT_DOMAINS.keys() and not command.preflight_none
        assert command.destructive is True and not command.writes


@pytest.mark.parametrize("warning, command, operation", [
    (VOTES_STILL_BOUND, ADOPT_VOTES, ADOPT_VOTE_OPERATION),
    (REWARDS_STILL_BOUND, ADOPT_REWARDS, ADOPT_REWARDS_OPERATION),
])
def test_still_bound_warnings_name_the_command_registered_from_the_same_constant(warning, command, operation):
    # Equal literals in one module compile to one object, so only the source shows a name spelled twice.
    spelled = [node for node in ast.walk(ast.parse(Path(admin_cmd_vote_migrations.__file__).read_bytes()))
               if isinstance(node, ast.Constant) and isinstance(node.value, str) and operation in node.value]
    assert command.operation is operation and operation in warning and len(spelled) == 1


def test_ledger_statuses_are_compared_through_their_owners():
    spelled = {node.value for node in ast.walk(ast.parse(Path(admin_cmd_vote_migrations.__file__).read_bytes()))
               if isinstance(node, ast.Constant)}
    assert spelled.isdisjoint((EXPIRY_SETTLED, COOKING_PENDING))


def test_confirmation_phrases_name_the_migration(registry):
    phrases = [registry.get(operation).phrase(registry.get(operation).parse(params)) for operation, params in (
        (REOPEN_SETTLED.operation, {"item_ids": [ITEM]}), (REOPEN_SETTLED.operation, {"item_ids": [ITEM, UNKNOWN_ITEM]}),
        (ADOPT_REWARDS.operation, {}), (ADOPT_COOKING.operation, {}), (ADOPT_VOTES.operation, {"item_id": ITEM}),
        (ESTABLISH_CAPACITY.operation, {"period": EARLIER_MONTH}))]
    assert phrases == [ONE_TICKET, TWO_TICKETS, REWARDS_PHRASE, COOKING_PHRASE, DEFINITION_PHRASE,
                       f"MIGRATE CAPACITY {EARLIER_MONTH}"]


@pytest.mark.parametrize("operation,params", [
    ("votes.reopen_settled", {}), ("votes.reopen_settled", {"item_id": ITEM}), ("votes.reopen_settled", {"item_ids": []}),
    ("votes.reopen_settled", {"item_ids": ITEM}), ("votes.reopen_settled", {"item_ids": [1]}),
    ("votes.reopen_settled", {"item_ids": [""]}), ("votes.reopen_settled", {"item_ids": [ITEM, ITEM]}),
    ("votes.reopen_settled", {"item_ids": [ITEM], "now": 1}), ("votes.adopt_reward_definition", {"item_id": ITEM}),
    ("votes.adopt_cooking_definition", {"now": 1}), ("votes.adopt_vote_definition", {}),
    ("sugoroku.establish_capacity_baseline", {}), ("sugoroku.establish_capacity_baseline", {"period": "2026-13"}),
    ("sugoroku.establish_capacity_baseline", {"period": "2026-1"}),
    ("sugoroku.establish_capacity_baseline", {"period": 202610})])
def test_parameters_are_strict(registry, operation, params):
    with pytest.raises(AdminError) as caught:
        registry.batch([{"operation": operation, "params": params}])
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


@pytest.mark.parametrize("command,services,reason", [
    (REOPEN_SETTLED, SimpleNamespace(), missing_service("votes")),
    (REOPEN_SETTLED, SimpleNamespace(votes=SimpleNamespace(transactions=None)), TRANSACTIONS_REASON),
    (ADOPT_REWARDS, SimpleNamespace(votes=SimpleNamespace(transactions=None)), TRANSACTIONS_REASON),
    (ADOPT_VOTES, SimpleNamespace(), missing_service("votes")),
    (ADOPT_COOKING, SimpleNamespace(votes=SimpleNamespace(transactions=object())), missing_service("vote_cooking")),
    (ESTABLISH_CAPACITY, SimpleNamespace(), missing_service("sugoroku"))])
def test_commands_name_the_service_they_wait_for(command, services, reason):
    assert command.availability(services) == reason


def test_reopening_a_ticket_archives_its_settlement_and_converts_the_reappeared_stock(voting, saved, preflight,
                                                                                       registry):
    voter = voting()
    end = voter.group.vote_end
    settled, report = voter.transactions.expiry.settle(saved(voter, tickets=3), end)
    notified, notices = voter.notifier.settle(settled, end)
    reappeared = voter.transactions.possessions.credit(notified, {ITEM: REAPPEARED})
    assert refusal(lambda: voter.services.initialize(reappeared)) == ("vote_expiry_already_closed", 503)
    later = end + timedelta(hours=1)
    migrated, step = migrate(voter.services, reappeared, preflight, registry, REOPEN_SETTLED.operation,
                             {"item_ids": [ITEM]}, later)
    mails = report["grants"][0]["mail_ids"]
    assert step["effects"] == {"tickets": [{"item_id": ITEM, "stock": REAPPEARED,
                                            "archived": {"status": "settled", "amount": 3, "gold": 150, "mail_ids": mails},
                                            "announced": True, "group_id": voter.group.group_id,
                                            "converts_on_next_observation": True}], "still_reappeared": []}
    assert step["warnings"] == []
    assert [record["mail_ids"] for record in voter.transactions.expiry.archived(migrated)] == [mails]
    assert voter.notifier.archived(migrated) == notices and voter.notifier.records(migrated) == {}
    converted, again = voter.transactions.expiry.settle(migrated, later)
    assert [grant["amount"] for grant in again["grants"]] == [REAPPEARED]
    announced, fresh = voter.notifier.settle(converted, later)
    assert [notice["mail_ids"] for notice in fresh] == [again["grants"][0]["mail_ids"]]
    assert voter.notifier.count(announced) == 2
    refused(voter.services, announced, preflight, registry, REOPEN_SETTLED.operation, {"item_ids": [ITEM]}, later,
            "vote_expiry_reopen_unneeded", 409)


def test_reopening_an_unannounced_or_foreign_ticket_warns_what_stays_unseen(voting, saved, preflight, registry):
    voter = voting()
    end = voter.group.vote_end
    settled, _ = voter.transactions.expiry.settle(saved(voter, tickets=3), end)
    reappeared = voter.transactions.possessions.credit(settled, {ITEM: REAPPEARED})
    _, step = migrate(voter.services, reappeared, preflight, registry, REOPEN_SETTLED.operation, {"item_ids": [ITEM]},
                      end + timedelta(hours=1))
    assert step["effects"]["tickets"][0]["announced"] is False and step["warnings"] == [NOTICE_DROPPED]
    # A catalog whose groups no longer use the ticket stands in for a later event's resources.
    elsewhere = copy(voter.services)
    elsewhere.votes = SimpleNamespace(transactions=copy(voter.transactions))
    elsewhere.votes.transactions.catalog = SimpleNamespace(groups=())
    _, step = migrate(elsewhere, reappeared, preflight, registry, REOPEN_SETTLED.operation, {"item_ids": [ITEM]},
                      end + timedelta(hours=1))
    [ticket] = step["effects"]["tickets"]
    assert ticket["group_id"] is None and ticket["converts_on_next_observation"] is False
    assert step["warnings"] == [NO_CURRENT_GROUP, NOTICE_DROPPED]


def test_tickets_that_reappeared_together_reopen_together(voting, saved, preflight, registry):
    voter = voting()
    other = voter.catalog.groups[1]
    source = voter.transactions.possessions.credit(saved(voter, tickets=3), {other.item_id: 1})
    settled, report = voter.transactions.expiry.settle(source, other.vote_end)
    assert sorted(grant["item_id"] for grant in report["grants"]) == [ITEM, other.item_id]
    notified, notices = voter.notifier.settle(settled, other.vote_end)
    both = voter.transactions.possessions.credit(notified, {ITEM: REAPPEARED, other.item_id: REAPPEARED})
    later = other.vote_end + timedelta(hours=1)
    store, pipeline = console(voter.services, both, preflight, later)
    single = pipeline.preview(registry.batch([{"operation": REOPEN_SETTLED.operation, "params": {"item_ids": [ITEM]}}]))
    [step] = single.describe()["steps"]
    assert step["effects"]["still_reappeared"] == [other.item_id] and step["warnings"] == [STILL_REAPPEARED]
    assert not single.committable
    assert single.restart_check["error"]["details"]["error"]["code"] == "vote_expiry_already_closed"
    batch = pipeline.preview(registry.batch([{"operation": REOPEN_SETTLED.operation, "params": {"item_ids": [item]}}
                                             for item in (ITEM, other.item_id)]))
    assert batch.committable and [item.phrase for item in batch.confirmations] == [ONE_TICKET, ONE_TICKET]
    migrated, step = migrate(voter.services, both, preflight, registry, REOPEN_SETTLED.operation,
                             {"item_ids": [other.item_id, ITEM]}, later)
    assert [ticket["item_id"] for ticket in step["effects"]["tickets"]] == [other.item_id, ITEM]
    assert step["effects"]["still_reappeared"] == [] and step["warnings"] == []
    assert [record["item_id"] for record in voter.transactions.expiry.archived(migrated)] == [other.item_id, ITEM]
    assert sorted(voter.notifier.archived(migrated), key=lambda notice: notice["item_id"]) == notices


def test_adopting_changed_vote_and_reward_definitions_lets_new_votes_through_without_backfill(
        voting, catalogs, saved, preflight, registry):
    voter, faster = voting(), voting(catalogs.faster)
    store = MemoryStore(saved(voter))
    first = ballot(store, voter, 20, tick=1000)
    assert [row["Count"] for row in table_rows(first["VRC"])] == [5, 10, 20]
    assert refusal(lambda: ballot(store, faster, 1, tick=1001)) == ("vote_definition_changed", 409)
    state, now = store.profile().state, faster.clock.now
    adopted, step = migrate(faster.services, state, preflight, registry, ADOPT_VOTES.operation, {"item_id": ITEM}, now)
    assert step["effects"] == {"item_id": ITEM, "group_id": 1, "card_ids": [voter.card], "refrozen": []}
    assert step["warnings"] == [REWARDS_STILL_BOUND]
    rewarded, step = migrate(faster.services, adopted, preflight, registry, ADOPT_REWARDS.operation, {}, now)
    assert step["effects"] == {
        "items": [{"item_id": ITEM, "group_id": 1, "event_id": EVENT, "count": 20, "archived_deliveries": 3}],
        "assault": {"event_id": EVENT, "earned_votes": {"before": DEFAULT_INTERVAL, "after": FASTER_INTERVAL}},
        "votes_still_bound": []}
    assert step["warnings"] == []
    ledger, previous = rewarded["vote_rewards"]["items"][ITEM], state["vote_rewards"]["items"][ITEM]
    assert (ledger["definition"], ledger["baseline_count"], ledger["count"], ledger["deliveries"]) == (None, 20, 20, {})
    assert ledger[ARCHIVED_KEY] == [{"event_id": EVENT, "definition": previous["definition"],
                                     "earned_votes": DEFAULT_INTERVAL, "baseline_count": previous["baseline_count"],
                                     "count": 20, "deliveries": previous["deliveries"]}]
    store.update_state(rewarded, store.profile().revision)
    second = ballot(store, faster, 25, tick=1002)
    assert [row["Count"] for row in table_rows(second["VRC"])] == [30, 40]
    assert table_rows(second["ASSAULT"]) == [{"Remain": 1 + 45 // FASTER_INTERVAL - 20 // FASTER_INTERVAL,
                                              "Next": FASTER_INTERVAL - 45 % FASTER_INTERVAL}]
    current = store.profile().state["vote_rewards"]["items"][ITEM]
    assert sorted(map(int, current["deliveries"])) == [30, 40]
    assert [sorted(map(int, instance["deliveries"])) for instance in current[ARCHIVED_KEY]] == [[5, 10, 20]]
    assert voted(directory(store, faster, tick=1003), faster) == 45
    latest = store.profile().state
    for operation, params, code in ((ADOPT_REWARDS.operation, {}, REWARD_ADOPTION_UNNEEDED),
                                    (ADOPT_VOTES.operation, {"item_id": ITEM}, VOTE_ADOPTION_UNNEEDED)):
        refused(faster.services, latest, preflight, registry, operation, params, now, code, 409)
    refused(faster.services, latest, preflight, registry, ADOPT_VOTES.operation, {"item_id": UNKNOWN_ITEM}, now,
            VOTE_GROUP_UNKNOWN, 422)


def test_adopting_rewards_first_points_at_the_candidate_bindings_still_left(voting, catalogs, saved, preflight,
                                                                           registry):
    voter, faster = voting(), voting(catalogs.faster)
    store = MemoryStore(saved(voter))
    ballot(store, voter, 20, tick=1000)
    _, step = migrate(faster.services, store.profile().state, preflight, registry, ADOPT_REWARDS.operation, {},
                      faster.clock.now)
    assert step["effects"]["votes_still_bound"] == [ITEM] and step["warnings"] == [VOTES_STILL_BOUND]


def test_adopted_cooking_rules_reopen_voting_and_keep_earlier_receipts_valid(voting, saved, preflight, registry):
    voter, stricter = voting(), voting(cooking_rules=VoteCookingRules(minimum_votes=STRICTER_MINIMUM))
    store = MemoryStore(saved(voter))
    ballot(store, voter, 3, tick=1000)
    earlier = store.profile().state["vote_cooking"]["groups"][ITEM]
    assert refusal(lambda: ballot(store, stricter, 1, tick=1001)) == ("vote_cooking_progress_closed", 409)
    migrated, step = migrate(stricter.services, store.profile().state, preflight, registry, ADOPT_COOKING.operation, {},
                             stricter.clock.now)
    assert step["effects"] == {"adopted": [{"item_id": ITEM, "group_id": 1,
                                            "status": {"before": "tracking", "after": "tracking"}, "reason": None,
                                            "mail_ids": []}], "mail_ids": []}
    assert step["warnings"] == []
    entry = migrated["vote_cooking"]["groups"][ITEM]
    assert entry["minimum_votes"] == STRICTER_MINIMUM and entry["counts"] == earlier["counts"]
    assert len(entry[SUPERSEDED_KEY]) == 1 and entry[SUPERSEDED_KEY][0]["minimum_votes"] == earlier["minimum_votes"]
    stricter.cooking.validate_vote_receipt(migrated, ITEM, earlier["definition"])
    store.update_state(migrated, store.profile().revision)
    ballot(store, stricter, 1, tick=1002)
    assert store.profile().state["vote_cooking"]["groups"][ITEM]["counts"] == {str(voter.card): 4}
    refused(stricter.services, store.profile().state, preflight, registry, ADOPT_COOKING.operation, {},
            stricter.clock.now, COOKING_ADOPTION_UNNEEDED, 409)


def test_pending_cooking_from_an_earlier_recipe_source_is_mailed_under_the_current_one(voting, saved, preflight,
                                                                                      registry):
    voter = voting()
    release = voter.group.result_start
    held, _ = voter.cooking.settle(filled(voter, cast(voter, saved(voter), 3)), release)
    entry = held["vote_cooking"]["groups"][ITEM]
    entry["source"] = EARLIER
    entry["definition"] = VoteCookingService._saved_definition(ITEM, entry)
    marked, _ = voter.cooking.settle(held, release)
    assert marked["vote_cooking"]["groups"][ITEM]["reason"] == "vote_cooking_recipe_changed"
    waiting, step = migrate(voter.services, marked, preflight, registry, ADOPT_COOKING.operation, {},
                            release + timedelta(seconds=1))
    assert step["effects"]["adopted"] == [{"item_id": ITEM, "group_id": 1,
                                           "status": {"before": "pending", "after": "pending"},
                                           "reason": "present_capacity", "mail_ids": []}]
    assert step["warnings"] == [COOKING_STILL_PENDING]
    assert waiting["vote_cooking"]["groups"][ITEM]["source"] == voter.cooking.recipes.content_version
    freed = deepcopy(marked)
    freed["tables"]["presents"] = [row for row in voter.presents.rows(marked) if row["Description"] != FILLER["description"]]
    mail = freed["last_present_id"] + 1
    delivered, step = migrate(voter.services, freed, preflight, registry, ADOPT_COOKING.operation, {},
                              release + timedelta(seconds=2))
    assert step["effects"] == {"adopted": [{"item_id": ITEM, "group_id": 1,
                                            "status": {"before": "pending", "after": "delivered"}, "reason": None,
                                            "mail_ids": [mail]}], "mail_ids": [mail]}
    assert step["warnings"] == []
    assert voter.presents.rows(delivered)[-1] == {"PresentID": mail, "Type": 21, "Content": voter.card,
                                                  "Description": voter.cooking.recipes.description}
    assert voter.cooking.settle(delivered, release + timedelta(seconds=3))[1] == []


def test_a_vote_adoption_refreezes_rankings_frozen_under_an_earlier_source(voting, saved, preflight, registry):
    voter = voting()
    end = voter.group.vote_end
    frozen, _ = voter.transactions.finalists.settle(cast(voter, saved(voter), 5), end)
    stale = deepcopy(frozen)
    for snapshot in stale["vote_finalists"]["groups"].values():
        snapshot.update(definition=EARLIER, source_sha256=EARLIER)
    assert refusal(lambda: voter.transactions.finalists.ranking(stale, voter.group)) == (
        "vote_finalists_definition_changed", 503)
    later = end + timedelta(hours=1)
    migrated, step = migrate(voter.services, stale, preflight, registry, ADOPT_VOTES.operation, {"item_id": ITEM}, later)
    assert step["effects"] == {"item_id": ITEM, "group_id": 1, "card_ids": [], "refrozen": [ITEM]}
    assert step["warnings"] == [] and migrated["vote_finalists"] == frozen["vote_finalists"]
    voter.clock.now = later
    assert voted(directory(MemoryStore(migrated), voter, tick=1000), voter) == 5
    tampered = deepcopy(stale)
    next(iter(tampered["vote_finalists"]["groups"].values()))["counts"][0]["Count"] += 1
    refused(voter.services, tampered, preflight, registry, ADOPT_VOTES.operation, {"item_id": ITEM}, later,
            REFREEZE_REFUSED, 409)


def test_vote_state_adoption_rebinds_only_bound_totals_of_the_named_items(voting, saved):
    voter = voting()
    state = cast(voter, saved(voter), 4)
    other = voter.catalog.groups[1].item_id
    baseline = record_vote(state, item_id=other, group_id=2, card_id=voter.card, count=1, definition=EARLIER)
    baseline["vote_state"]["entries"][str(read_vote_history(baseline)[-1]["key"])].update(definition=None,
                                                                                         baseline_count=1)
    before = deepcopy(baseline)
    updated, adopted = adopt_current(baseline, {ITEM: EARLIER, other: EARLIER})
    assert baseline == before and read_vote_history(updated) == read_vote_history(baseline)
    assert adopted == [{"item_id": ITEM, "card_id": voter.card, "count": 4,
                        "definition": {"before": voter.transactions.rewards.group_definition(voter.group),
                                       "after": EARLIER}}]
    assert adopt_current(updated, {ITEM: EARLIER})[1] == []
    for definitions in ({ITEM: "short"}, {ITEM: None}, {"": EARLIER}, [(ITEM, EARLIER)]):
        assert refusal(lambda: adopt_current(baseline, definitions)) == ("vote_update_invalid", 400)


ARCHIVE_TAMPERS = {
    "chain": lambda ledger: ledger[ARCHIVED_KEY][0].update(count=ledger[ARCHIVED_KEY][0]["count"] - 1),
    "interval": lambda ledger: ledger[ARCHIVED_KEY][0].update(earned_votes=0),
    "entitlement": lambda ledger: ledger[ARCHIVED_KEY][0].update(earned_votes=FASTER_INTERVAL // 2),
    "point": lambda ledger: ledger[ARCHIVED_KEY][0]["deliveries"].update(
        {"21": ledger[ARCHIVED_KEY][0]["deliveries"]["20"]}),
    "empty": lambda ledger: ledger.update({ARCHIVED_KEY: []}),
    "extra": lambda ledger: ledger[ARCHIVED_KEY][0].update(group_id=1),
}


@pytest.mark.parametrize("damage", sorted(ARCHIVE_TAMPERS))
def test_archived_reward_instances_keep_their_chain_and_entitlement(voting, catalogs, saved, damage):
    voter, faster = voting(), voting(catalogs.faster)
    adopted, _ = faster.transactions.rewards.adopt_current(cast(voter, saved(voter), 20))
    ARCHIVE_TAMPERS[damage](adopted["vote_rewards"]["items"][ITEM])
    assert refusal(lambda: faster.transactions.rewards.validated(adopted)) == ("vote_rewards_state_invalid", 503)


def test_cooking_rules_adopted_back_make_the_earlier_definition_current_again(voting, saved):
    voter, stricter = voting(), voting(cooking_rules=VoteCookingRules(minimum_votes=STRICTER_MINIMUM))
    state = cast(voter, saved(voter), 3)
    original = state["vote_cooking"]["groups"][ITEM]["definition"]
    adopted, _ = stricter.cooking.adopt_current(state, stricter.clock.now)
    changed = adopted["vote_cooking"]["groups"][ITEM]["definition"]
    restored, report = voter.cooking.adopt_current(adopted, voter.clock.now)
    entry = restored["vote_cooking"]["groups"][ITEM]
    assert entry["definition"] == original and [row["status"] for row in report] == [
        {"before": "tracking", "after": "tracking"}]
    assert [VoteCookingService._saved_definition(ITEM, old) for old in entry[SUPERSEDED_KEY]] == [changed]
    for definition in (original, changed):
        voter.cooking.validate_vote_receipt(restored, ITEM, definition)


COOKING_TAMPERS = {
    "parameters": lambda entry: entry[SUPERSEDED_KEY][0].update(copies=0),
    "group": lambda entry: entry[SUPERSEDED_KEY][0].update(group_id=2),
    "current": lambda entry: entry[SUPERSEDED_KEY].append({**entry[SUPERSEDED_KEY][0], "minimum_votes": STRICTER_MINIMUM}),
    "repeated": lambda entry: entry[SUPERSEDED_KEY].append(dict(entry[SUPERSEDED_KEY][0])),
    "empty": lambda entry: entry.update({SUPERSEDED_KEY: []}),
}


@pytest.mark.parametrize("damage", sorted(COOKING_TAMPERS))
def test_superseded_cooking_definitions_stay_verifiable(voting, saved, damage):
    voter, stricter = voting(), voting(cooking_rules=VoteCookingRules(minimum_votes=STRICTER_MINIMUM))
    adopted, _ = stricter.cooking.adopt_current(cast(voter, saved(voter), 3), stricter.clock.now)
    COOKING_TAMPERS[damage](adopted["vote_cooking"]["groups"][ITEM])
    assert refusal(lambda: stricter.cooking.ledger(adopted)) == ("vote_cooking_state_invalid", 503)


def test_a_capacity_baseline_unblocks_a_save_with_nonzero_legacy_capacity(bundle, preflight, registry, monkeypatch):
    assert bundle.sugoroku is not None, bundle.sugoroku_error
    monkeypatch.setattr(bundle.bootstrap, "clock", lambda: CAPACITY_NOW)
    legacy = bundle.initialize(bundle.player_state.initialize(bundle.bootstrap.initial_state()))
    legacy.pop("monthly_capacity")
    legacy["tables"]["game_param"][0].update(zip((*CHARISMA_FIELDS, *STAMINA_FIELDS),
                                                 (LEGACY_CHARISMA, LEGACY_NEXT, LEGACY_STAMINA, 0)))
    assert refusal(lambda: bundle.initialize(legacy)) == (BASELINE_REQUIRED, 503)
    refused(bundle, legacy, preflight, registry, ESTABLISH_CAPACITY.operation, {"period": LATER_MONTH}, CAPACITY_NOW,
            PERIOD_AHEAD, 409)
    migrated, step = migrate(bundle, legacy, preflight, registry, ESTABLISH_CAPACITY.operation,
                             {"period": EARLIER_MONTH}, CAPACITY_NOW)
    assert step["effects"] == {"period": EARLIER_MONTH, "business_month": BUSINESS_MONTH, "current": LEGACY_CHARISMA,
                               "next": LEGACY_NEXT, "stamina": {STAMINA_FIELDS[0]: {"before": LEGACY_STAMINA, "after": 0},
                                                                STAMINA_FIELDS[1]: {"before": 0, "after": 0}}}
    assert step["warnings"] == [STAMINA_CLEARED, MONTHS_PENDING]
    assert bundle.initialize(migrated) == migrated
    assert (read_capacity(migrated)["current"], read_capacity(migrated)["next"]) == (LEGACY_CHARISMA, LEGACY_NEXT)
    refused(bundle, migrated, preflight, registry, ESTABLISH_CAPACITY.operation, {"period": BUSINESS_MONTH},
            CAPACITY_NOW, BASELINE_UNNEEDED, 409)
    _, step = migrate(bundle, legacy, preflight, registry, ESTABLISH_CAPACITY.operation, {"period": BUSINESS_MONTH},
                      CAPACITY_NOW)
    assert step["effects"]["period"] == BUSINESS_MONTH and step["warnings"] == [STAMINA_CLEARED]
