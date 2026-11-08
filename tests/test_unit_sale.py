from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import astuple, dataclass
from functools import cache
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from aigis_ps.alxml import Column, Container, loads
from aigis_ps.errors import ProtocolError
from aigis_ps.player_state import BARRACKS_SHIFT, LOCK_FLAG
from aigis_ps.sale_protocol import AUTOMATIC_SALE, MANUAL_SALE
from aigis_ps.storage import Store
from aigis_ps.unit_reference import BARRACKS_STORAGE, FIRST_BARRACKS, UNIT_REFERENCE_COLUMNS, UnitReference
from aigis_ps.unit_removal import Protection
from aigis_ps.unit_sale import SALE_POLICIES, UnitSale, UnitSaleService
from aigis_ps.unit_sale_catalog import RAINBOW_ITEM, UnitSaleCatalog
from aigis_ps.wallet_money import GOLD_LIMIT
from test_admin_seams import MemoryStore
from test_quest_service import bundle, request
from test_training_service import columns


GOLDEN = Path(__file__).resolve().parent / "golden" / "p23b" / "unit-sales.json"
GOLD_ARMORS = (77, 533, 902)
RAINBOW_CARD = 7
PRINCE = 0
STARTING_MONEY = 1_000_000
PRIMARY_FLAGS = 1
SECOND_BARRACKS_FLAGS = PRIMARY_FLAGS | 1 << BARRACKS_SHIFT
WAREHOUSE_STORAGE = 1
FORGED_IDENTITY = 999
FULL_RAINBOW_STOCK = 999_999
MANUAL_OVERFLOW_UNITS, AUTOMATIC_BULK_UNITS = 11, 12
SECOND_AWAKENING = 3
FULL_LOVE, WATCHED_LOVE_EPISODES = 150, 3
NEWEST_ARMOR = len(GOLD_ARMORS)
OTHER_TEAM = 1
LEGACY_QUEST = 1001


@pytest.fixture(scope="module")
def catalog(bundle):
    return UnitSaleCatalog(bundle.gifts.catalog, bundle.possessions, lambda name: bundle.native_cdn.assets.read(name)[0])


@pytest.fixture
def make_case(bundle, catalog, tmp_path):
    serial = 0
    def create(card_ids=GOLD_ARMORS):
        nonlocal serial
        serial += 1
        state = bundle.player_state.initialize(bundle.bootstrap.initial_state())
        state, units = bundle.player_state.credit_units(state,
            [{"card_id": card_id, "level": 1, "count": 1} for card_id in card_ids])
        state["tables"]["game_param"][0]["Money"] = 1000000
        store = Store(tmp_path / f"sale-{serial}.sqlite3", state)
        return SimpleNamespace(bundle=bundle, service=wired_service(bundle, catalog), catalog=catalog, store=store,
                               units=units)
    return create


def wired_service(bundle, catalog):
    return UnitSaleService(bundle.bootstrap, bundle.player_state, bundle.possessions, bundle.gifts, catalog,
                           evolution_rewards=bundle.evolution_rewards)


def message(case, *, number=MANUAL_SALE, units=None, tick=1000):
    return request(case.store, number, (Container("APPEND", columns(case.units if units is None else units)),), tick=tick)


def mutate(case, action):
    before = case.store.profile()
    state = deepcopy(before.state)
    action(state)
    case.store.update_state(state, before.revision)


def fails(case, envelope, code=None):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.sell(envelope, case.store)
    if code is not None:
        assert caught.value.code == code
    assert case.store.profile() == before


@pytest.mark.parametrize("number", [3, 4])
def test_three_gold_armor_prices_are_independent_and_instances_are_removed_once(make_case, number):
    case = make_case()
    before = case.store.profile()
    response = case.service.sell(message(case, number=number), case.store)
    after = case.store.profile()
    assert after.revision == before.revision + 1
    assert after.state["tables"]["game_param"][0]["Money"] == 1136000
    assert after.state["player_state"]["units"] == before.state["player_state"]["units"][:1]
    assert after.state["player_state"]["battle"] == before.state["player_state"]["battle"]
    assert after.state["player_state"]["last_unique_id"] == 4
    for table, rows in before.state["tables"].items():
        if table != "game_param":
            assert after.state["tables"][table] == rows
    children = {child.name: child for child in loads(response).children}
    assert set(children) == {"SYSTEM", "PGP"}
    assert children["PGP"].children[0].values == ("A1",)
    assert children["PGP"].children[1].values == ("1136000",)


def test_auto_sale_does_not_apply_manual_ten_unit_limit(make_case):
    case = make_case([77] * 12)
    case.service.sell(message(case, number=4), case.store)
    assert case.store.profile().state["tables"]["game_param"][0]["Money"] == 1072000


def test_rainbow_sale_credits_absolute_possession_count_without_mail(make_case):
    case = make_case([7])
    card = case.bundle.gifts.catalog.card(7)
    assert card["_TradePoint"] > 0
    mutate(case, lambda state: state["tables"]["possessions"].append({"ItemID": case.catalog.rainbow_item, "Count": 17}))
    response = case.service.sell(message(case), case.store)
    children = {child.name: child for child in loads(response).children}
    assert set(children) == {"SYSTEM", "PGP", "POSSESSION"}
    assert children["POSSESSION"].children[0].values == (case.catalog.rainbow_item,)
    assert children["POSSESSION"].children[1].values == (17 + card["_TradePoint"],)
    assert case.store.profile().state["tables"]["presents"] == []


def test_price_does_not_change_with_level_love_or_skill(make_case):
    normal = make_case([7])
    grown = make_case([7])
    mutate(grown, lambda state: state["player_state"]["units"][1].update(Exp=12345, Love=150, SkillLevel=5))
    normal.service.sell(message(normal), normal.store)
    grown.service.sell(message(grown), grown.store)
    money = [case.store.profile().state["tables"]["game_param"][0]["Money"] for case in (normal, grown)]
    assert money[0] == money[1] > 1000000
    assert normal.store.profile().state["tables"]["possessions"] == grown.store.profile().state["tables"]["possessions"]


@pytest.mark.parametrize("number,protection", [(3, "lock"), (3, "squad"), (3, "prince"), (4, "lock"), (4, "prince")])
def test_protected_instance_rejects_entire_batch(make_case, number, protection):
    case = make_case()
    units = None
    if protection == "lock":
        mutate(case, lambda state: state["player_state"]["units"][2].update(GeneralPurposeFlags=3))
    elif protection == "squad":
        mutate(case, lambda state: state["player_state"]["squads"].append(
            {"Address": 0x70000, "UnitIndex": 2, "Name": "", "Flags": 0}))
    else:
        units = [*case.units, case.store.profile().state["player_state"]["units"][0]]
    fails(case, message(case, number=number, units=units), "sale_unit_protected")


def squad_rows(*members):
    return [{"Address": (group << 24) | slot, "UnitIndex": unit, "Name": f"team{group}", "Flags": 0}
            for group, slot, unit in members]


def test_auto_sale_sells_organized_gold_armor_and_empties_only_its_squad_slots(make_case):
    case = make_case()
    sold, prince = case.units[1]["GetIndex"], case.store.profile().state["player_state"]["units"][0]["GetIndex"]
    mutate(case, lambda state: state["player_state"]["squads"].extend(
        squad_rows((0, 1, prince), (0, 2, sold), (1, 1, sold), (1, 2, -1))))
    before = case.store.profile()
    response = case.service.sell(message(case, number=4), case.store)
    after = case.store.profile().state
    assert after["tables"]["game_param"][0]["Money"] == 1136000
    assert after["player_state"]["units"] == before.state["player_state"]["units"][:1]
    assert after["player_state"]["squads"] == squad_rows((0, 1, prince), (0, 2, -1), (1, 1, -1), (1, 2, -1))
    assert {child.name for child in loads(response).children} == {"SYSTEM", "PGP"}


def test_auto_sale_rechecks_squads_written_during_a_conflict(make_case, monkeypatch):
    case = make_case()
    sold = case.units[0]["GetIndex"]
    original = case.store.update_state
    called = []
    def competing(state, revision):
        if not called:
            called.append(True)
            current = case.store.profile()
            current.state["player_state"]["squads"].extend(squad_rows((2, 3, sold)))
            original(current.state, current.revision)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", competing)
    case.service.sell(message(case, number=4), case.store)
    saved = case.store.profile().state
    assert saved["player_state"]["squads"] == squad_rows((2, 3, -1))
    assert len(saved["player_state"]["units"]) == 1


def test_emptied_squad_readback_preflight_failure_keeps_every_unit(make_case, monkeypatch):
    case = make_case()
    def reject(*args):
        raise ProtocolError("squad_readback_test", "test")
    monkeypatch.setattr(case.bundle.player_state, "squad_response", reject)
    case.service.sell(message(case, number=4, units=case.units[:1]), case.store)
    mutate(case, lambda state: state["player_state"]["squads"].extend(squad_rows((0, 1, case.units[1]["GetIndex"]))))
    fails(case, message(case, number=4, units=case.units[1:], tick=1001), "squad_readback_test")


def test_refused_sale_is_logged_because_the_native_client_resends_it(make_case, caplog):
    case = make_case([77, 7])
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        fails(case, message(case, number=4), "sale_automatic_ineligible")
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        "Unit sale 4 refused with sale_automatic_ineligible; the native client shows a communication error and resends "
        "the same request until the server accepts it"]


def test_status_separates_native_exclusions_from_the_refusal_aftermath(make_case):
    status = make_case().service.status()
    assert status["native_exclusions"] == [
        "Warehouse and additional barrack references, which the native sale lists never send"]
    assert "unsupported" not in status and status["implemented_operations"] == [MANUAL_SALE, AUTOMATIC_SALE]
    assert status["refusals"] == ("Logged, because the native client shows a communication error and resends "
                                  "the refused request until it is accepted")


def test_auto_sale_cannot_sell_an_arbitrary_character(make_case):
    case = make_case([77, 7])
    fails(case, message(case, number=4), "sale_automatic_ineligible")


@pytest.mark.parametrize("number", [3, 4])
@pytest.mark.parametrize("flags", [0x11, 0x21, -0x80000000 | 0x81])
def test_old_primary_reference_rejects_unit_moved_to_other_barracks(make_case, number, flags):
    case = make_case()
    envelope = message(case, number=number)
    mutate(case, lambda state: state["player_state"]["units"][2].update(GeneralPurposeFlags=flags))
    fails(case, envelope, "sale_storage_unsupported")


@pytest.mark.parametrize("number", [3, 4])
def test_primary_barracks_allows_unrelated_flag_bits(make_case, number):
    case = make_case()
    def change(state):
        for unit in state["player_state"]["units"][1:]:
            unit["GeneralPurposeFlags"] = -0x80000000 | 0x105
    mutate(case, change)
    case.service.sell(message(case, number=number), case.store)
    after = case.store.profile().state
    assert len(after["player_state"]["units"]) == 1
    assert after["tables"]["game_param"][0]["Money"] == 1136000


@pytest.mark.parametrize("number", [3, 4])
def test_cas_conflict_rechecks_actual_barracks(make_case, monkeypatch, number):
    case = make_case()
    update = case.store.update_state
    competing_state = []
    def conflict(state, revision):
        if not competing_state:
            current = case.store.profile()
            current.state["player_state"]["units"][2]["GeneralPurposeFlags"] = 0x11
            update(current.state, current.revision)
            competing_state.append(case.store.profile())
        return update(state, revision)
    monkeypatch.setattr(case.store, "update_state", conflict)
    with pytest.raises(ProtocolError) as caught:
        case.service.sell(message(case, number=number), case.store)
    assert caught.value.code == "sale_storage_unsupported"
    assert case.store.profile() == competing_state[0]


def test_forged_card_id_and_uid_do_not_change_price_or_ownership(make_case):
    case = make_case()
    for change in ({"CardID": 902}, {"UniqueID": 999}):
        fails(case, message(case, units=[{**case.units[0], **change}]), "sale_unit_missing")


@pytest.mark.parametrize("money,expected", [(GOLD_LIMIT - 5, GOLD_LIMIT), (GOLD_LIMIT, GOLD_LIMIT),
                                            (GOLD_LIMIT + 5, GOLD_LIMIT + 5), (0x7fffffff - 5, 0x7fffffff - 5)])
def test_gold_beyond_the_limit_is_discarded_without_refusing_the_sale(make_case, money, expected):
    case = make_case()
    mutate(case, lambda state: state["tables"]["game_param"][0].update(Money=money))
    before = case.store.profile()
    envelope = message(case)
    response = case.service.sell(envelope, case.store)
    saved = case.store.profile()
    assert saved.state["tables"]["game_param"][0]["Money"] == expected
    assert saved.state["player_state"]["units"] == before.state["player_state"]["units"][:1]
    children = {child.name: child for child in loads(response).children}
    assert set(children) == {"SYSTEM", "PGP"} and children["PGP"].children[1].values == (str(expected),)
    assert case.service.sell(envelope, case.store) == response and case.store.profile() == saved


def test_status_reports_the_shared_gold_limit(make_case):
    assert make_case().service.status()["gold_limit"] == GOLD_LIMIT


def test_rainbow_overflow_rolls_back_gold_and_unit_deletion(make_case):
    case = make_case([77, 7])
    mutate(case, lambda state: state["tables"]["possessions"].append(
        {"ItemID": case.catalog.rainbow_item, "Count": 999999}))
    fails(case, message(case), "possession_stack_exceeded")


def test_old_love_reward_baseline_and_high_water_marks_survive_sale(make_case):
    case = make_case([7])
    mutate(case, updated_unit(1, Love=FULL_LOVE, WatchLoveEv=WATCHED_LOVE_EPISODES))
    mutate(case, without_identity_high_water_mark)
    case.service.sell(message(case), case.store)
    state = case.store.profile().state
    assert state["gift_state"]["claims"]["7"] == [1, 100, 150]
    _, units = case.bundle.player_state.credit_units(state, [{"card_id": 7, "level": 1, "count": 1}])
    assert units[0]["UniqueID"] == 3 and units[0]["GetIndex"] == 2


def test_exact_retry_survives_store_and_service_reconstruction(make_case):
    case = make_case()
    envelope = message(case)
    response = case.service.sell(envelope, case.store)
    expected = case.store.profile()
    reopened = Store(case.store.path)
    service = UnitSaleService(case.bundle.bootstrap, case.bundle.player_state, case.bundle.possessions, case.bundle.gifts, case.catalog)
    assert service.sell(envelope, reopened) == response
    assert reopened.profile() == expected
    fails(case, message(case, tick=1001), "sale_unit_missing")


def test_evicted_receipt_cannot_resell_consumed_identity(make_case, monkeypatch):
    import aigis_ps.unit_sale as module
    monkeypatch.setattr(module, "HISTORY_LIMIT", 1)
    case = make_case()
    first = message(case, units=case.units[:1])
    case.service.sell(first, case.store)
    case.service.sell(message(case, units=case.units[1:2]), case.store)
    fails(case, first, "sale_unit_missing")


def test_changed_content_or_revision_never_replays_an_obsolete_balance(make_case, monkeypatch):
    case = make_case()
    envelope = message(case)
    case.service.sell(envelope, case.store)
    original = case.catalog.content_version
    monkeypatch.setattr(case.catalog, "content_version", "f" * 64)
    fails(case, envelope, "sale_retry_stale")
    monkeypatch.setattr(case.catalog, "content_version", original)
    mutate(case, lambda state: state["tables"]["game_param"][0].update(Money=999999))
    fails(case, envelope, "sale_retry_stale")


def test_simultaneous_identical_requests_credit_once(make_case):
    case = make_case()
    envelope = message(case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: case.service.sell(envelope, case.store), range(2)))
    assert results[0] == results[1]
    assert case.store.profile().revision == 1
    assert case.store.profile().state["tables"]["game_param"][0]["Money"] == 1136000


def test_cas_conflict_rechecks_new_lock_before_crediting(make_case, monkeypatch):
    case = make_case()
    original = case.store.update_state
    called = []
    def competing(state, revision):
        if not called:
            called.append(True)
            current = case.store.profile()
            current.state["player_state"]["units"][1]["GeneralPurposeFlags"] |= LOCK_FLAG
            original(current.state, current.revision)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", competing)
    with pytest.raises(ProtocolError, match="protected"):
        case.service.sell(message(case), case.store)
    assert case.store.profile().state["tables"]["game_param"][0]["Money"] == 1000000
    assert len(case.store.profile().state["player_state"]["units"]) == 4


def test_reload_preflight_failure_does_not_commit_sale(make_case, monkeypatch):
    case = make_case()
    def reject(*args, **kwargs):
        raise ProtocolError("response_too_large", "test")
    monkeypatch.setattr(case.bundle.bootstrap, "_configuration_response", reject)
    fails(case, message(case), "response_too_large")


def owned_reference(unit, **changes):
    identity = {"storage_type": BARRACKS_STORAGE, "storage_index": FIRST_BARRACKS, "invite_id": unit["GetIndex"],
                "unique_id": unit["UniqueID"], "card_id": unit["CardID"], **changes}
    return UnitReference(**identity)


def credited(units):
    return [owned_reference(unit) for unit in units[1:]]


def held(position, **changes):
    return lambda units: owned_reference(units[position], **changes)


def warehouse(card_id):
    # Native warehouse references carry only a CardID and leave every instance field zero.
    return lambda units: UnitReference(WAREHOUSE_STORAGE, 0, 0, 0, card_id)


def select(*references):
    return lambda units: [reference(units) for reference in references]


def append(references):
    rows = [(*astuple(reference), reference.invite_id, reference.unique_id) for reference in references]
    return Container("APPEND", tuple(Column(name, "I", tuple(row[index] for row in rows))
                                     for index, name in enumerate(UNIT_REFERENCE_COLUMNS)))


def updated_unit(position, **values):
    return lambda state: state["player_state"]["units"][position].update(values)


def flagged(position, flags):
    return updated_unit(position, GeneralPurposeFlags=flags)


def locked(position):
    return flagged(position, PRIMARY_FLAGS | LOCK_FLAG)


def current_squad(*positions):
    # Battle TN starts at team 0, so these rows form the current squad.
    return lambda state: state["player_state"]["squads"].extend(
        {"Address": slot, "UnitIndex": position, "Name": "", "Flags": 0} for slot, position in enumerate(positions, 1))


def organized(*members):
    return lambda state: state["player_state"]["squads"].extend(squad_rows(*members))


def without_identity_high_water_mark(state):
    del state["player_state"]["last_unique_id"]


def money(value):
    return lambda state: state["tables"]["game_param"][0].update(Money=value)


def rainbow_stock(count):
    return lambda state: state["tables"]["possessions"].append({"ItemID": RAINBOW_ITEM, "Count": count})


def secretary(position):
    return lambda state: state["tables"]["config"][0].update(_GuideUnitIndex=position)


def battle_reserving(card_id):
    return lambda state: state.update(quest_state={"active": {
        "unit_reservations": [{"card_id": card_id, "level": 1, "count": 1}]}})


def unsupported_history(state):
    state["unit_sale_history"] = {"schema_version": 2, "receipts": []}


@dataclass(frozen=True)
class SaleCase:
    number: int = MANUAL_SALE
    cards: tuple[int, ...] = GOLD_ARMORS
    prepare: tuple[Callable[[dict], None], ...] = ()
    select: Callable[[list[dict]], list[UnitReference]] = credited
    replay: bool = False
    stale: bool = False


SALE_CASES = {
    "manual_gold_armors": SaleCase(replay=True, stale=True),
    "automatic_gold_armors": SaleCase(AUTOMATIC_SALE, replay=True),
    "manual_rainbow": SaleCase(cards=(RAINBOW_CARD,), prepare=(rainbow_stock(17),), replay=True),
    "automatic_sells_a_current_squad_member": SaleCase(AUTOMATIC_SALE, cards=(77, 77), prepare=(current_squad(PRINCE, 1, 2),),
                                                       select=select(held(1)), replay=True),
    "automatic_bulk_beyond_the_manual_limit": SaleCase(AUTOMATIC_SALE, cards=(77,) * AUTOMATIC_BULK_UNITS),
    "automatic_member_of_several_squads": SaleCase(AUTOMATIC_SALE, cards=(77, 77), prepare=(
        organized((0, 1, PRINCE), (0, 2, 1), (OTHER_TEAM, 1, 1), (OTHER_TEAM, 2, 2)),), select=select(held(1))),
    "manual_secretary": SaleCase(cards=(RAINBOW_CARD,), prepare=(secretary(1),)),
    "manual_card_reserved_by_battle": SaleCase(cards=(77,), prepare=(battle_reserving(77),)),
    "manual_trust_baseline": SaleCase(cards=(RAINBOW_CARD,), prepare=(
        updated_unit(1, Love=FULL_LOVE, WatchLoveEv=WATCHED_LOVE_EPISODES),)),
    "manual_keeps_the_saved_awakening_ledger": SaleCase(cards=(RAINBOW_CARD,),
                                                        prepare=(updated_unit(1, ClassChangeCount=SECOND_AWAKENING),)),
    "manual_without_identity_high_water_mark": SaleCase(prepare=(without_identity_high_water_mark,),
                                                        select=select(held(NEWEST_ARMOR))),
    "gold_reaches_the_limit": SaleCase(prepare=(money(GOLD_LIMIT - 5),), replay=True),
    "gold_balance_above_the_limit": SaleCase(prepare=(money(GOLD_LIMIT + 5),), replay=True),
    "warehouse_reference": SaleCase(select=select(warehouse(77))),
    "storage_index_reference": SaleCase(select=select(held(1, storage_index=2))),
    "unique_id_mismatch": SaleCase(select=select(held(1, unique_id=FORGED_IDENTITY))),
    "card_id_mismatch": SaleCase(select=select(held(1, card_id=902))),
    "unknown_get_index": SaleCase(select=select(held(1, invite_id=FORGED_IDENTITY))),
    "second_barracks": SaleCase(prepare=(flagged(2, SECOND_BARRACKS_FLAGS),)),
    "second_barracks_before_lock": SaleCase(prepare=(flagged(2, SECOND_BARRACKS_FLAGS | LOCK_FLAG),)),
    "manual_locked": SaleCase(prepare=(locked(2),)),
    "automatic_locked": SaleCase(AUTOMATIC_SALE, prepare=(locked(2),)),
    "manual_prince": SaleCase(select=select(held(1), held(PRINCE))),
    "automatic_prince": SaleCase(AUTOMATIC_SALE, select=select(held(1), held(PRINCE))),
    "manual_squad_member": SaleCase(prepare=(current_squad(2),)),
    "automatic_ineligible_card": SaleCase(AUTOMATIC_SALE, cards=(77, RAINBOW_CARD)),
    "rainbow_stack_exceeded": SaleCase(cards=(77, RAINBOW_CARD), prepare=(rainbow_stock(FULL_RAINBOW_STOCK),)),
    "protected_before_later_missing": SaleCase(prepare=(locked(1),),
                                               select=select(held(1), held(2, unique_id=FORGED_IDENTITY))),
    "missing_before_later_protected": SaleCase(prepare=(locked(2),),
                                               select=select(held(1, unique_id=FORGED_IDENTITY), held(2))),
    "ineligible_before_later_warehouse": SaleCase(AUTOMATIC_SALE, cards=(RAINBOW_CARD,),
                                                  select=select(held(1), warehouse(77))),
    "warehouse_before_later_ineligible": SaleCase(AUTOMATIC_SALE, cards=(RAINBOW_CARD,),
                                                  select=select(warehouse(77), held(1))),
    "unsupported_history": SaleCase(prepare=(unsupported_history,)),
    "manual_beyond_ten_units": SaleCase(cards=(77,) * MANUAL_OVERFLOW_UNITS),
    "repeated_reference": SaleCase(select=select(held(1), held(1))),
}
GOLDEN_REFUSALS = frozenset(("sale_storage_unsupported", "sale_unit_missing", "sale_unit_protected",
                             "sale_automatic_ineligible", "possession_stack_exceeded", "sale_history_invalid",
                             "invalid_request_fields", "sale_retry_stale"))


def sale_state(bundle, case):
    initial = bundle.player_state.initialize(bundle.bootstrap.initial_state())
    # The server initializes the awakening ledger at startup, so sales always see it.
    state, _ = bundle.player_state.credit_units(bundle.evolution_rewards.initialize(initial),
                                                [{"card_id": card_id, "level": 1, "count": 1} for card_id in case.cards])
    state["tables"]["game_param"][0]["Money"] = STARTING_MONEY
    for change in case.prepare:
        change(state)
    return state


def sale_snapshot(state):
    tables, player = state["tables"], state["player_state"]
    receipts = state.get("unit_sale_history", {}).get("receipts", ())
    return {"money": tables["game_param"][0]["Money"], "last_unique_id": player.get("last_unique_id"),
            "units": [[unit["GetIndex"], unit["UniqueID"], unit["CardID"]] for unit in player["units"]],
            "squads": [[row["Address"], row["UnitIndex"]] for row in player["squads"]],
            "rainbow": next((row["Count"] for row in tables["possessions"] if row["ItemID"] == RAINBOW_ITEM), None),
            "guide": tables["config"][0]["_GuideUnitIndex"], "presents": len(tables["presents"]),
            "trust_claims": state.get("gift_state", {}).get("claims"),
            "awakening_claims": state["evolution_rewards"]["claimed"],
            "receipts": [receipt["revision"] for receipt in receipts]}


def sale_attempt(service, envelope, store):
    before = store.profile()
    try:
        result = {"response": service.sell(envelope, store).decode("utf-8")}
    except ProtocolError as error:
        result = {"error": {"code": error.code, "message": str(error), "status": error.status}}
    after = store.profile()
    return {**result, "revisions": after.revision - before.revision, "save": sale_snapshot(after.state)}


def bump_revision(store):
    profile = store.profile()
    profile.state["tables"]["game_param"][0]["Money"] += 1
    store.update_state(profile.state, profile.revision)


def sale_outcome(service, bundle, case):
    store = MemoryStore(sale_state(bundle, case))
    envelope = request(store, case.number, (append(case.select(store.profile().state["player_state"]["units"])),))
    outcome = {"request": envelope.xml.decode("utf-8"), **sale_attempt(service, envelope, store)}
    if case.replay:
        outcome["replay"] = sale_attempt(service, envelope, store)
    if case.stale:
        bump_revision(store)
        outcome["stale"] = sale_attempt(service, envelope, store)
    return outcome


@cache
def recorded_sales():
    return json.loads(GOLDEN.read_bytes())["cases"]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def recorded_refusals(outcome):
    attempts = (outcome, *(outcome[name] for name in ("replay", "stale") if name in outcome))
    return {attempt["error"]["code"] for attempt in attempts if "error" in attempt}


@pytest.fixture(scope="module")
def golden_services(bundle, catalog):
    # Without an explicit awakening ledger the service builds one from the trust ledger's sources.
    return {"wired": wired_service(bundle, catalog),
            "derived": UnitSaleService(bundle.bootstrap, bundle.player_state, bundle.possessions, bundle.gifts, catalog)}


def test_sale_golden_covers_every_case_and_refusal():
    recorded = recorded_sales()
    assert recorded.keys() == SALE_CASES.keys()
    assert GOLDEN_REFUSALS <= set().union(*map(recorded_refusals, recorded.values()))


@pytest.mark.parametrize("wiring", ["wired", "derived"])
@pytest.mark.parametrize("name", SALE_CASES)
def test_sale_matches_the_recorded_golden(bundle, golden_services, wiring, name):
    outcome = sale_outcome(golden_services[wiring], bundle, SALE_CASES[name])
    assert canonical(outcome) == canonical(recorded_sales()[name])


def test_sale_policies_follow_the_native_sale_lists():
    assert SALE_POLICIES[MANUAL_SALE].refuse == {Protection.PRINCE, Protection.LOCKED, Protection.SQUAD}
    assert SALE_POLICIES[AUTOMATIC_SALE].refuse == {Protection.PRINCE, Protection.LOCKED}


def test_derived_awakening_ledger_matches_the_shared_service(bundle, catalog):
    derived = UnitSaleService(bundle.bootstrap, bundle.player_state, bundle.possessions, bundle.gifts, catalog)
    assert derived.evolution_rewards is not bundle.evolution_rewards
    assert derived.evolution_rewards.content_version == bundle.evolution_rewards.content_version
    assert wired_service(bundle, catalog).evolution_rewards is bundle.evolution_rewards


def test_sale_kernel_reports_the_sale_without_receipts_and_leaves_its_input_alone(make_case):
    case = make_case()
    profile = case.store.profile()
    before = deepcopy(profile.state)
    result = case.service.kernel.sell(profile.state, profile.revision, credited(before["player_state"]["units"]),
                                      MANUAL_SALE)
    assert profile.state == before
    assert result.quote == case.catalog.quote(case.units) and result.balance == 1136000
    assert result.state["tables"]["game_param"][0]["Money"] == result.balance
    assert list(result.sold) == case.units and result.vacated == ()
    assert result.state["player_state"]["units"] == before["player_state"]["units"][:1]
    assert "unit_sale_history" not in result.state


def test_sale_freezes_the_awakening_baseline_of_saves_without_its_ledger(make_case):
    case = make_case()
    sold = case.units[0]
    mutate(case, updated_unit(1, ClassChangeCount=SECOND_AWAKENING))
    assert "evolution_rewards" not in case.store.profile().state
    case.service.sell(message(case, units=[sold]), case.store)
    saved = case.store.profile().state
    assert saved["evolution_rewards"]["claimed"] == [sold["CardID"]]
    assert case.bundle.evolution_rewards.initialize(saved) == saved


def test_sale_kernel_names_its_refusals_with_its_error_prefix(make_case):
    case = make_case()
    kernel = UnitSale(case.bundle.player_state, case.bundle.possessions, case.bundle.gifts, case.bundle.evolution_rewards,
                      case.catalog, error_prefix="admin_sale")
    profile = case.store.profile()
    units = profile.state["player_state"]["units"]
    for references, code in (([owned_reference(units[1], unique_id=FORGED_IDENTITY)], "admin_sale_unit_missing"),
                             ([warehouse(77)(units)], "admin_sale_storage_unsupported"),
                             ([owned_reference(units[PRINCE])], "admin_sale_unit_protected")):
        with pytest.raises(ProtocolError) as caught:
            kernel.sell(profile.state, profile.revision, references, MANUAL_SALE)
        assert caught.value.code == code


@pytest.mark.parametrize("prefix", ["", "Upper", "x" * 64])
def test_sale_kernel_error_prefixes_are_bounded(bundle, catalog, prefix):
    with pytest.raises(ValueError):
        UnitSale(bundle.player_state, bundle.possessions, bundle.gifts, bundle.evolution_rewards, catalog,
                 error_prefix=prefix)


def test_sale_kernel_needs_both_reward_ledgers(bundle, catalog):
    for gifts, rewards in ((None, bundle.evolution_rewards), (bundle.gifts, None)):
        with pytest.raises(ProtocolError) as caught:
            UnitSale(bundle.player_state, bundle.possessions, gifts, rewards, catalog)
        assert (caught.value.code, caught.value.status) == ("sale_unit_dependencies_unavailable", 503)


def test_sale_kernel_accepts_only_the_native_sale_modes(make_case):
    case = make_case()
    profile = case.store.profile()
    with pytest.raises(ValueError):
        case.service.kernel.sell(profile.state, profile.revision, credited(profile.state["player_state"]["units"]),
                                 max(SALE_POLICIES) + 1)


@pytest.mark.parametrize("number", [MANUAL_SALE, AUTOMATIC_SALE])
def test_sales_do_not_wait_for_legacy_battles(make_case, number):
    case = make_case()
    legacy = {"active": {"quest_id": LEGACY_QUEST}}
    mutate(case, lambda state: state.update(quest_state=deepcopy(legacy)))
    case.service.sell(message(case, number=number), case.store)
    after = case.store.profile().state
    assert after["tables"]["game_param"][0]["Money"] == 1136000
    assert after["quest_state"] == legacy
