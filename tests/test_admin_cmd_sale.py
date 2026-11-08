from dataclasses import replace
from pathlib import Path
import uuid

import pytest

from aigis_ps import admin_cmd_sale
from aigis_ps.admin_cmd_sale import GOLD_DISCARDED, SELL
from aigis_ps.admin_command import AdminError, PlanStore, missing_service
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS, GenerationPreflight
from aigis_ps.admin_registry import CommandRegistry, discover
from aigis_ps.admin_routes_state import describe_command
from aigis_ps.alxml import Container
from aigis_ps.energy_clock import instant
from aigis_ps.player_state import BARRACKS_SHIFT, LOCK_FLAG, MAX_PLAYER_UNITS
from aigis_ps.sale_protocol import MANUAL_SALE
from aigis_ps.storage import AUTO_BACKUP_KIND
from aigis_ps.unit_sale import UnitSaleService
from aigis_ps.unit_sale_catalog import RAINBOW_ITEM
from aigis_ps.wallet_money import GOLD_LIMIT
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_admin_preflight import none_reasons, unread_reasons
from test_admin_seams import MemoryStore
from test_quest_service import request
from test_training_service import columns
from test_unit_removal import PRINCE_INDEX, bundle, granted, new_save, unit_at


GOLD_ARMOR, PLATINUM_ARMOR, RAINBOW_CARD = 77, 533, 7
STARTING_MONEY = 1_000_000
FULL_RAINBOW_STOCK = 999_999
SECOND_BARRACKS = 1 << BARRACKS_SHIFT
FORGED_IDENTITY = 999
SALE_READBACK = {"game_param": [33, 34, 35], "possessions": [129, 137], "units": [41]}
SALE_DOMAINS = {"units", "game_param", "possessions", "gifts", "evolution_rewards"}


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


@pytest.fixture(scope="module")
def registry():
    commands = CommandRegistry(PREFLIGHT_DOMAINS)
    admin_cmd_sale.register(commands)
    return commands


def console(services, state, preflight):
    store = FakeStore(state)
    generation = FakeGeneration(id="gen-sale", bundle=services, clock=instant(services.bootstrap.clock()))
    return store, CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(), FakeRuntime(generation),
                                 backup_directory=Path("backups"))


def selector(unit, **changes):
    return {"get_index": unit["GetIndex"], "unique_id": unit["UniqueID"], "card_id": unit["CardID"], **changes}


def sale(registry, *selectors):
    return registry.batch([{"operation": SELL.operation, "params": {"units": list(selectors)}}])


def commit(subject, plan, confirmations=()):
    return subject.commit(plan_id=plan.plan_id, plan_digest=plan.plan_digest, expected_revision=plan.expected_revision,
                          op_id=str(uuid.uuid4()), confirmations=confirmations)


def preview_error(subject, batch, code, status):
    with pytest.raises(AdminError) as caught:
        subject.preview(batch)
    assert (caught.value.code, caught.value.status) == (code, status)
    return caught.value


def sale_save(bundle, *cards, money=STARTING_MONEY):
    state, added = new_save(bundle), []
    for card in cards:
        state, units = granted(bundle, state, card_id=card)
        added.extend(units)
    state["tables"]["game_param"][0]["Money"] = money
    return state, added


def roster(state):
    return [unit["GetIndex"] for unit in state["player_state"]["units"]]


def rainbow(state):
    return next((row["Count"] for row in state["tables"]["possessions"] if row["ItemID"] == RAINBOW_ITEM), 0)


def test_sale_command_registers_against_the_preflight_table():
    catalog = discover(PREFLIGHT_DOMAINS)
    assert SELL.operation in {command.operation for command in catalog.commands} and "admin_cmd_sale" in catalog.modules
    assert SELL.touched == SALE_DOMAINS and SELL.domain == "units" and SELL.destructive
    assert not SELL.preflight_none and SELL.touched <= PREFLIGHT_DOMAINS.keys()
    assert none_reasons(SELL.touched) == unread_reasons("gifts", "evolution_rewards")
    assert SALE_DOMAINS - none_reasons(SELL.touched).keys() == SALE_READBACK.keys()
    # Money and unit rows belong to many game flows, so the sale claims no exclusive field ownership.
    assert not SELL.writes


def test_sale_phrases_count_the_selected_units():
    one = SELL.parse({"units": [{"get_index": 3, "unique_id": 4, "card_id": GOLD_ARMOR}]})
    two = SELL.parse({"units": [{"get_index": 3, "unique_id": 4, "card_id": GOLD_ARMOR},
                                {"get_index": 5, "unique_id": 6, "card_id": GOLD_ARMOR}]})
    assert (SELL.phrase(one), SELL.phrase(two)) == ("SELL 1 UNIT", "SELL 2 UNITS")


@pytest.mark.parametrize("units", [
    [], [{"get_index": 1, "unique_id": 2}], [{"get_index": 1, "unique_id": 2, "card_id": 0}],
    [{"get_index": -1, "unique_id": 2, "card_id": GOLD_ARMOR}], [{"get_index": 1, "unique_id": 0, "card_id": GOLD_ARMOR}],
    [{"get_index": 1, "unique_id": 2, "card_id": True}], [{"get_index": 1, "unique_id": 2, "card_id": "77"}],
    [{"get_index": 1, "unique_id": 2, "card_id": GOLD_ARMOR, "storage_index": 0}],
    [{"get_index": 1, "unique_id": 2, "card_id": GOLD_ARMOR}, {"get_index": 1, "unique_id": 3, "card_id": GOLD_ARMOR}],
    [{"get_index": 1, "unique_id": 2, "card_id": GOLD_ARMOR}, {"get_index": 2, "unique_id": 2, "card_id": GOLD_ARMOR}],
    [{"get_index": index, "unique_id": index + 1, "card_id": GOLD_ARMOR} for index in range(MAX_PLAYER_UNITS + 1)],
])
def test_sale_parameters_are_strict(units):
    with pytest.raises(AdminError) as caught:
        SELL.parse({"units": units})
    assert (caught.value.code, caught.value.status) == ("admin_params_invalid", 422)


def test_sale_plans_credit_gold_and_rainbow_and_commit_after_its_phrase_and_a_backup(bundle, preflight, registry):
    state, added = sale_save(bundle, GOLD_ARMOR, RAINBOW_CARD, GOLD_ARMOR)
    store, subject = console(bundle, state, preflight)
    sold, kept = added[:2], added[2]
    quote = bundle.sales.catalog.quote(sold)
    plan = subject.preview(sale(registry, *map(selector, sold)))
    assert plan.committable and plan.preflight["readback"] == SALE_READBACK and quote.rainbow > 0
    assert [item.phrase for item in plan.confirmations] == ["SELL 2 UNITS"]
    step = plan.steps[0]
    assert step.warnings == () and step.effects["last_unique_id"] == kept["UniqueID"]
    assert step.effects["gold"] == {"quoted": quote.gold, "credited": quote.gold, "discarded": 0,
                                    "balance": STARTING_MONEY + quote.gold}
    assert step.effects["rainbow"] == {"item_id": RAINBOW_ITEM, "credited": quote.rainbow}
    assert [unit["GetIndex"] for unit in step.effects["sold"]] == [unit["GetIndex"] for unit in sold]
    with pytest.raises(AdminError) as caught:
        commit(subject, plan)
    assert caught.value.code == "admin_confirmation_required" and store.revision == 0
    outcome = commit(subject, plan, ("SELL 2 UNITS",))
    assert outcome.result["backup_before_commit"]["kind"] == AUTO_BACKUP_KIND and len(store.backups) == 1
    assert outcome.result["readback"] == SALE_READBACK and store.revision == 1
    assert roster(store.state) == [PRINCE_INDEX, kept["GetIndex"]]
    assert store.state["tables"]["game_param"][0]["Money"] == STARTING_MONEY + quote.gold
    assert rainbow(store.state) == quote.rainbow
    # Console sales write no game receipt, so a native retry of an older request sees the new revision.
    assert set(store.state) - set(state) <= {"gift_state"}


@pytest.mark.parametrize("money, credited", [(GOLD_LIMIT - 5, 5), (GOLD_LIMIT + 5, 0)])
def test_gold_beyond_the_limit_is_discarded_with_a_warning(bundle, preflight, registry, money, credited):
    state, added = sale_save(bundle, GOLD_ARMOR, money=money)
    store, subject = console(bundle, state, preflight)
    quote = bundle.sales.catalog.quote(added)
    plan = subject.preview(sale(registry, selector(added[0])))
    step = plan.steps[0]
    assert step.effects["gold"] == {"quoted": quote.gold, "credited": credited, "discarded": quote.gold - credited,
                                    "balance": money + credited}
    assert step.warnings == (GOLD_DISCARDED,)
    commit(subject, plan, ("SELL 1 UNIT",))
    assert store.state["tables"]["game_param"][0]["Money"] == money + credited


def guarded_save(bundle):
    state, added = sale_save(bundle, GOLD_ARMOR, GOLD_ARMOR, GOLD_ARMOR, PLATINUM_ARMOR, GOLD_ARMOR)
    locked, organized, secretary, reserved, free = added
    unit_at(state, locked["GetIndex"])["GeneralPurposeFlags"] |= LOCK_FLAG
    team = state["player_state"]["battle"]["TN"]
    state["player_state"]["squads"] = [{"Address": team << 24, "UnitIndex": organized["GetIndex"], "Name": "team",
                                        "Flags": 0}]
    state["tables"]["config"][0]["_GuideUnitIndex"] = secretary["GetIndex"]
    state["quest_state"] = {"active": {"unit_reservations": [{"card_id": PLATINUM_ARMOR, "level": 1, "count": 1}]}}
    return state, unit_at(state, PRINCE_INDEX), added


def test_sales_follow_the_manual_sale_list_protections(bundle, preflight, registry):
    state, prince, (locked, organized, secretary, reserved, free) = guarded_save(bundle)
    store, subject = console(bundle, state, preflight)
    for unit in (prince, locked, organized):
        error = preview_error(subject, sale(registry, selector(free), selector(unit)), "admin_sale_unit_protected", 409)
        assert error.details == {"step": 0, "operation": SELL.operation}
    # The native manual list offers the secretary and reserved cards, and a sold secretary keeps its saved selection.
    plan = subject.preview(sale(registry, *map(selector, (secretary, reserved, free))))
    assert plan.committable and plan.restart_check == {"ok": True}
    commit(subject, plan, ("SELL 3 UNITS",))
    assert roster(store.state) == [PRINCE_INDEX, locked["GetIndex"], organized["GetIndex"]]
    assert store.state["tables"]["config"][0]["_GuideUnitIndex"] == secretary["GetIndex"]
    assert store.state["player_state"]["squads"] == state["player_state"]["squads"]


def test_sales_refuse_identities_that_no_longer_match(bundle, preflight, registry):
    state, added = sale_save(bundle, GOLD_ARMOR, GOLD_ARMOR)
    unit_at(state, added[1]["GetIndex"])["GeneralPurposeFlags"] |= SECOND_BARRACKS
    _, subject = console(bundle, state, preflight)
    for changes in ({"unique_id": FORGED_IDENTITY}, {"card_id": PLATINUM_ARMOR}, {"get_index": FORGED_IDENTITY}):
        preview_error(subject, sale(registry, selector(added[0], **changes)), "admin_sale_unit_missing", 409)
    preview_error(subject, sale(registry, selector(added[1])), "admin_sale_storage_unsupported", 501)


def test_rainbow_beyond_its_stack_refuses_the_whole_sale(bundle, preflight, registry):
    state, added = sale_save(bundle, GOLD_ARMOR, RAINBOW_CARD)
    state["tables"]["possessions"].append({"ItemID": RAINBOW_ITEM, "Count": FULL_RAINBOW_STOCK})
    store, subject = console(bundle, state, preflight)
    preview_error(subject, sale(registry, *map(selector, added)), "possession_stack_exceeded", 400)
    assert store.revision == 0 and store.backups == []


@pytest.mark.parametrize("missing", ["sales", "gifts", "evolution_rewards"])
def test_sales_need_the_sale_catalog_and_both_reward_ledgers(bundle, preflight, registry, missing):
    state, added = sale_save(bundle, GOLD_ARMOR)
    services = replace(bundle, **{missing: None})
    _, subject = console(services, state, preflight)
    error = preview_error(subject, sale(registry, selector(added[0])), "admin_command_unavailable", 503)
    assert missing in str(error)
    assert describe_command(SELL, bundle)["available"] is True
    listed = describe_command(SELL, services)
    assert (listed["available"], listed["unavailable_reason"]) == (False, missing_service(missing))


def test_console_sale_changes_the_save_exactly_like_the_native_manual_sale(bundle, preflight, registry):
    state, added = sale_save(bundle, GOLD_ARMOR, RAINBOW_CARD)
    native = MemoryStore(state)
    sales = UnitSaleService(bundle.bootstrap, bundle.player_state, bundle.possessions, bundle.gifts, bundle.sales.catalog,
                            evolution_rewards=bundle.evolution_rewards)
    sales.sell(request(native, MANUAL_SALE, (Container("APPEND", columns(added)),)), native)
    store, subject = console(bundle, state, preflight)
    commit(subject, subject.preview(sale(registry, *map(selector, added))), ("SELL 2 UNITS",))
    expected = native.profile().state
    del expected["unit_sale_history"]
    assert store.state == expected
