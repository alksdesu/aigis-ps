from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
import logging
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from aigis_ps import gift_service
from aigis_ps.alxml import Column, Container, loads
from aigis_ps.card_status import FIRST_EPISODE, SECOND_EPISODE, CardStatusService
from aigis_ps.energy_clock import JST, utc_now
from aigis_ps.errors import ProtocolError
from aigis_ps.gift_catalog import GiftCatalog
from aigis_ps.gift_service import GiftService
from aigis_ps.image_aliases import ImageAliasCatalog
from aigis_ps.presents import MAX_PRESENTS, PresentService
from aigis_ps.request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, SCHEMA_VERSION
from aigis_ps.storage import Store
from test_quest_service import bundle, request


NOW = datetime(2026, 11, 1, 12)
ORDINARY = ("gift_0000_bouquet", "gift_0001_quartz", "gift_0002_ruby", "gift_0003_diamond",
            "gift_1000_beer", "gift_1001_wine", "gift_1002_fineLiquor", "gift_1003_millenniumWine")
ADVANCED = ("gift_0103_ring", "gift_0104_ring", "gift_0105_ring", "gift_0107_ring", "gift_1200_emblem")
HIDDEN = ("gift_0106_ring", "gift_0108_ring", "gift_0109_ring", "gift_0200_ring")


@pytest.fixture(scope="module")
def components(bundle):
    snapshot = bundle.masters.snapshot()
    reader = lambda name: bundle.native_cdn.assets.read(name)[0]
    catalog = GiftCatalog(snapshot, reader, bundle.possessions, resource_version=bundle.bootstrap.asset_version)
    aliases = ImageAliasCatalog(reader, catalog._cards)
    presents = PresentService(bundle.bootstrap, bundle.player_state, asset_reader=reader)
    assert presents.status()["ready"]
    return SimpleNamespace(bundle=bundle, catalog=catalog, aliases=aliases,
                           status=CardStatusService(snapshot), presents=presents)


def choose(components, *, gender=1, rare=None, threshold=None, advanced=None, gift=None):
    for card_id, card in components.catalog._cards.items():
        if not card["BonusType"] or not 0 < card["LoveEv1"] < 100:
            continue
        image = components.aliases.resolve(card_id).image_card_id
        if components.catalog.card(image)["Kind"] != gender:
            continue
        if rare is not None and card["Rare"] != rare:
            continue
        if threshold is not None and card["LoveEv1"] != threshold:
            continue
        if advanced is not None and bool(card["BonusType3"]) != advanced:
            continue
        if gift is not None and -1 not in gift.rarities and card["Rare"] not in gift.rarities:
            continue
        return card_id
    raise AssertionError("No current card satisfies the targeted test condition")


@pytest.fixture
def make_case(components, tmp_path):
    sequence = 0
    def make(card_id=None, *, love=0, watch=0, extra=()):
        nonlocal sequence
        sequence += 1
        data = components.bundle
        state = data.player_state.initialize(data.bootstrap.initial_state())
        identifiers = []
        for identifier, old_love, old_watch in [(card_id or choose(components, rare=3, threshold=30), love, watch), *extra]:
            state, added = data.player_state.credit_units(state, [{"card_id": identifier, "level": 1, "count": 1}])
            index = added[0]["GetIndex"]
            next(unit for unit in state["player_state"]["units"] if unit["GetIndex"] == index).update(Love=old_love, WatchLoveEv=old_watch)
            identifiers.append(index)
        state = data.possessions.credit(state, {gift.item_id: 999 for gift in components.catalog.gifts})
        store = Store(tmp_path / f"gift-{sequence}.sqlite3", state)
        clock = [NOW]
        service = GiftService(data.bootstrap, data.player_state, data.possessions, components.status,
                              components.presents, components.catalog, components.aliases, clock=lambda: clock[0])
        return SimpleNamespace(service=service, store=store, indices=identifiers, clock=clock, components=components)
    return make


def target(case, index=None):
    index = case.indices[0] if index is None else index
    return next(unit for unit in case.store.profile().state["player_state"]["units"] if unit["GetIndex"] == index)


def message(case, *, index=None, slots=None, number=None, tick=1000, single=0, alternative=None, unique=None):
    unit = target(case, index)
    alias = case.service.aliases.resolve(unit["CardID"])
    gender = case.service.catalog.card(alias.image_card_id)["Kind"]
    if number is None:
        number = 100 if gender == 1 else 101
    if alternative is None:
        alternative = alias.image_card_id if alias.image_card_id != unit["CardID"] else 0
    if slots is None:
        slots = [("gift_0003_diamond" if gender == 1 else "gift_1003_millenniumWine", 1)]
    slots = [*slots, *((0, 0),) * (4 - len(slots))]
    fields = [Container("BASE", (Column("A7", "I", (unit["GetIndex"],)),
                                Column("AB", "I", (unit["UniqueID"] if unique is None else unique,)),
                                Column("ALTERNATIVE", "I", (alternative,)))),
              Column("ITEM", "S" if isinstance(single, str) else "I", (single,))]
    for slot, (item, count) in enumerate(slots):
        fields.extend((Column(f"ITEM{slot}", "S" if isinstance(item, str) else "I", (item,)), Column(f"NUM{slot}", "I", (count,))))
    return request(case.store, number, fields, tick=tick)


def nodes(response):
    return {child.name: child for child in loads(response).children}


def count(case, item):
    return next(row["Count"] for row in case.store.profile().state["tables"]["possessions"] if row["ItemID"] == item)


def mutate(case, operation):
    profile = case.store.profile()
    state = deepcopy(profile.state)
    operation(state)
    case.store.update_state(state, profile.revision)


def rewrite(case, operation):
    state = deepcopy(case.store.profile().state)
    operation(state)
    with case.store.connection() as connection:
        connection.execute("UPDATE profiles SET state_json=? WHERE id=1", (json.dumps(state, ensure_ascii=True),))


def fails_unchanged(case, envelope, code=None):
    before = case.store.profile()
    with pytest.raises(ProtocolError) as caught:
        case.service.send(envelope, case.store)
    if code is not None:
        assert caught.value.code == code
    assert case.store.profile() == before
    return caught.value


@pytest.mark.parametrize("item", ORDINARY)
def test_every_normal_male_and_female_gift(components, make_case, item):
    gift = components.catalog.gift(item)
    case = make_case(choose(components, gender=gift.gender))
    response = nodes(case.service.send(message(case, slots=[(item, 2)]), case.store))
    assert target(case)["Love"] == gift.add_value * 2
    assert count(case, item) == 997
    assert response["TrustReward"].children[0].values == ()
    assert "PFC" not in response and "PPT" not in response


@pytest.mark.parametrize("item", ADVANCED)
def test_every_currently_usable_advanced_gift(components, make_case, item):
    gift = components.catalog.gift(item)
    case = make_case(choose(components, gender=gift.gender, advanced=True, gift=gift), love=100, watch=2)
    case.service.send(message(case, slots=[(item, 1)]), case.store)
    assert target(case)["Love"] == 110 and count(case, item) == 998


@pytest.mark.parametrize("item", (*HIDDEN, "gift_0110_ring", "gift_0111_ring", "gift_1201_emblem"))
def test_hidden_and_incompatible_special_rarity_gifts_do_not_become_universally_usable(components, make_case, item):
    gender = components.catalog.gift(item).gender
    case = make_case(choose(components, gender=gender, advanced=True, rare=3), love=100, watch=2)
    fails_unchanged(case, message(case, slots=[(item, 1)]), "gift_item_ineligible")


def test_zero_quantity_real_names_and_integer_hash_resolve_to_canonical_item(make_case):
    case = make_case()
    item = case.service.catalog.gift("gift_0002_ruby")
    response = nodes(case.service.send(message(case, slots=[("gift_0000_bouquet", 0), (item.item_hash, 1), ("gift_0003_diamond", 0)]), case.store))
    assert count(case, item.item_id) == 998 and target(case)["Love"] == 4
    assert response["POSSESSION"].children[0].values == (item.item_id,)


def test_base_menu_registration_the_native_client_never_sends_is_refused(make_case):
    case = make_case()
    assert set(case.service.handlers()) == {100, 101}
    status = case.service.status()
    assert status["implemented_operations"] == [100, 101]
    assert status["native_exclusions"] == ["Operation 99, which both native gift item menus override with 100 or 101"]
    fails_unchanged(case, message(case, number=99), "invalid_request_fields")


def test_zero_quantity_duplicate_or_hidden_display_slots_are_not_consumed(make_case):
    case = make_case()
    case.service.send(message(case, slots=[("gift_0003_diamond", 1), ("gift_0003_diamond", 0),
                                           ("gift_0200_ring", 0)]), case.store)
    assert count(case, "gift_0003_diamond") == 998 and count(case, "gift_0200_ring") == 999


@pytest.mark.parametrize("card_id", [2700, 2701])
def test_r18_alias_gender_changes_menu_but_not_persisted_card_identity(make_case, card_id):
    case = make_case(card_id, love=29)
    fails_unchanged(case, message(case, number=101), "gift_gender_mismatch")
    fails_unchanged(case, message(case, alternative=0), "gift_alternative_mismatch")
    case.service.send(message(case, number=100), case.store)
    state = case.store.profile().state
    assert target(case)["CardID"] == card_id
    assert state["tables"]["card_status"][0]["CardID"] == card_id
    assert str(card_id) in state["gift_state"]["claims"]


@pytest.mark.parametrize("kind", [2, None, True])
def test_effective_gender_served_by_neither_menu_is_unsupported(make_case, kind):
    case = make_case()
    image = case.service.aliases.resolve(target(case)["CardID"], rating="R18").image_card_id
    catalog = copy(case.service.catalog)
    catalog._cards = {**catalog._cards, image: {**catalog._cards[image], "Kind": kind}}
    case.service.catalog = catalog
    fails_unchanged(case, message(case, number=100), "gift_gender_unsupported")


@pytest.mark.parametrize("threshold", [20, 25, 30])
def test_first_event_uses_actual_card_threshold(components, make_case, threshold):
    card_id = choose(components, threshold=threshold)
    case = make_case(card_id, love=threshold - 3)
    first = nodes(case.service.send(message(case, slots=[("gift_0000_bouquet", 1)]), case.store))
    assert first["TrustReward"].children[0].values == () and target(case)["WatchLoveEv"] == 0
    second = nodes(case.service.send(message(case, slots=[("gift_0000_bouquet", 1)], tick=1001), case.store))
    assert target(case)["WatchLoveEv"] == 1
    assert len(second["TrustReward"].children[0].values) == 1
    assert case.store.profile().state["tables"]["card_status"][0]["A7"] == FIRST_EPISODE == 1


def test_one_gift_can_cross_first_event_and_100_with_two_mail_rewards(make_case):
    case = make_case()
    before = case.store.profile().state
    response = nodes(case.service.send(message(case, slots=[("gift_0003_diamond", 20)]), case.store))
    state = case.store.profile().state
    assert target(case)["Love"] == 100 and target(case)["WatchLoveEv"] == 2
    assert len(response["TrustReward"].children[0].values) == 2
    assert len(state["tables"]["presents"]) == 2
    assert all(row["Type"] == 1 and row["Content"] == 3 for row in state["tables"]["presents"])
    assert len(state["player_state"]["units"]) == len(before["player_state"]["units"])
    assert state["tables"]["card_status"][0]["A7"] == FIRST_EPISODE | SECOND_EPISODE == 3
    assert state["gift_state"]["claims"][str(target(case)["CardID"])] == [1, 100]


def test_love_99_accepts_one_bouquet_and_consumes_whole_quantity(make_case):
    case = make_case(love=99, watch=1)
    response = nodes(case.service.send(message(case, slots=[("gift_0000_bouquet", 1)]), case.store))
    assert target(case)["Love"] == 100 and count(case, "gift_0000_bouquet") == 998
    assert len(response["TrustReward"].children[0].values) == 1


def test_150_reward_is_mail_only_and_does_not_unlock_third_character_episode(components, make_case):
    case = make_case(choose(components, rare=3, advanced=True), love=149, watch=2)
    before = case.store.profile().state
    response = nodes(case.service.send(message(case, slots=[("gift_0103_ring", 1)]), case.store))
    state = case.store.profile().state
    assert target(case)["Love"] == 150 and target(case)["WatchLoveEv"] == 3
    assert state["tables"]["card_status"][0]["A7"] == 3
    assert state["tables"]["presents"][0]["Type"] == 0 and state["tables"]["presents"][0]["Content"] == 1
    assert state["tables"]["game_param"] == before["tables"]["game_param"]
    assert state["tables"]["crystals"] == before["tables"]["crystals"]
    assert "PFC" not in response


def test_existing_attained_stages_across_all_owned_duplicates_form_migration_baseline(components, make_case):
    card_id = choose(components, rare=3, threshold=30)
    case = make_case(card_id, extra=[(card_id, 100, 2)])
    response = nodes(case.service.send(message(case, slots=[("gift_0003_diamond", 20)]), case.store))
    assert response["TrustReward"].children[0].values == ()
    assert case.store.profile().state["tables"]["presents"] == []
    assert case.store.profile().state["gift_state"]["claims"][str(card_id)] == [1, 100]


def test_consumed_high_love_instance_retains_baseline_through_card_history(make_case):
    case = make_case()
    card_id = target(case)["CardID"]
    profile = case.store.profile()
    state = case.service.card_status.update(profile.state, [{"CardID": card_id, "A7": 3}])
    case.store.update_state(state, profile.revision)
    response = nodes(case.service.send(message(case, slots=[("gift_0003_diamond", 20)]), case.store))
    assert response["TrustReward"].children[0].values == ()
    assert case.store.profile().state["tables"]["presents"] == []
    assert case.store.profile().state["gift_state"]["claims"][str(card_id)] == [1, 100]


def test_unowned_historical_card_is_baselined_before_later_reacquisition(components, make_case):
    case = make_case()
    historical = choose(components, gender=0, rare=3)
    assert all(unit["CardID"] != historical for unit in case.store.profile().state["player_state"]["units"])
    profile = case.store.profile()
    state = case.service.card_status.update(profile.state, [{"CardID": historical, "A7": 3}])
    case.store.update_state(state, profile.revision)
    case.service.send(message(case), case.store)
    assert case.store.profile().state["gift_state"]["claims"][str(historical)] == [1, 100]
    profile = case.store.profile()
    state, units = case.service.players.credit_units(profile.state, [{"card_id": historical, "level": 1, "count": 1}])
    case.store.update_state(state, profile.revision)
    response = nodes(case.service.send(message(case, index=units[0]["GetIndex"],
                                              slots=[("gift_1003_millenniumWine", 20)], tick=1001), case.store))
    assert response["TrustReward"].children[0].values == ()
    assert case.store.profile().state["tables"]["presents"] == []


def test_third_episode_bit_never_implies_150_reward_was_attained(components, make_case):
    case = make_case(choose(components, rare=3, advanced=True), love=149, watch=2)
    card_id = target(case)["CardID"]
    profile = case.store.profile()
    state = case.service.card_status.update(profile.state, [{"CardID": card_id, "A7": 4, "B0": 1730400000}])
    case.store.update_state(state, profile.revision)
    response = nodes(case.service.send(message(case, slots=[("gift_0103_ring", 1)]), case.store))
    assert len(response["TrustReward"].children[0].values) == 1
    assert case.store.profile().state["tables"]["presents"][0]["Content"] == 1
    assert case.store.profile().state["tables"]["card_status"][0]["A7"] == 7


@pytest.mark.parametrize("watch,expected", [(0, []), (1, [1]), (2, [1, 100]), (3, [1, 100, 150])])
def test_known_old_event_stages_contribute_to_conservative_baseline(make_case, watch, expected):
    case = make_case(watch=watch)
    case.service.send(message(case), case.store)
    assert case.store.profile().state["gift_state"]["claims"].get(str(target(case)["CardID"]), []) == expected


def test_unknown_event_stage_on_another_unit_does_not_mean_every_reward_was_attained(components, make_case):
    other = choose(components, gender=0, rare=3)
    case = make_case(extra=[(other, 0, 99)])
    case.service.send(message(case), case.store)
    assert str(other) not in case.store.profile().state["gift_state"]["claims"]


def test_existing_journal_is_not_replaced_by_later_history_changes(make_case):
    case = make_case()
    case.service.send(message(case), case.store)
    profile = case.store.profile()
    state = case.service.card_status.update(profile.state, [{"CardID": target(case)["CardID"], "A7": 3}])
    case.store.update_state(state, profile.revision)
    case.service.send(message(case, tick=1001), case.store)
    assert case.store.profile().state["gift_state"]["claims"] == {}


def test_two_instances_share_one_reward_tier_after_migration(components, make_case):
    card_id = choose(components, rare=3, threshold=30)
    case = make_case(card_id, extra=[(card_id, 0, 0)])
    case.service.send(message(case, slots=[("gift_0003_diamond", 6)]), case.store)
    response = nodes(case.service.send(message(case, index=case.indices[1], slots=[("gift_0003_diamond", 6)], tick=1001), case.store))
    assert response["TrustReward"].children[0].values == ()
    assert len(case.store.profile().state["tables"]["presents"]) == 1


def test_complete_card_history_and_unrelated_tutorial_bits_are_preserved(make_case):
    case = make_case(love=29)
    card_id = target(case)["CardID"]
    profile = case.store.profile()
    state = case.service.card_status.update(profile.state, [{"CardID": card_id, "A1": 9, "A3": 8, "A5": 7,
                                                            "A7": 0x40000000, "B0": 1730400000}])
    state["tables"]["config"][0]["MenuTutorialFlags"] = 0x400
    case.store.update_state(state, profile.revision)
    response = nodes(case.service.send(message(case), case.store))
    saved = case.store.profile().state
    row = saved["tables"]["card_status"][0]
    assert (row["A1"], row["A3"], row["A5"], row["A7"], row["B0"]) == (9, 8, 7, 0x40000001, 1730400000)
    assert len(response["CardStatus"].children) == 10
    assert response["PGC"].children[0].values == ("A4",)
    assert saved["tables"]["config"][0]["MenuTutorialFlags"] == 0x401


def test_zero_inventory_count_is_returned_explicitly(make_case):
    case = make_case()
    mutate(case, lambda state: next(row for row in state["tables"]["possessions"] if row["ItemID"] == "gift_0003_diamond").update(Count=1))
    response = nodes(case.service.send(message(case), case.store))
    assert count(case, "gift_0003_diamond") == 0
    assert response["POSSESSION"].children[1].values == (0,)


@pytest.mark.parametrize("failure", ["inventory", "mailbox", "41", "42", "137", "gift", "51"])
def test_capacity_and_inventory_failures_leave_every_field_unchanged(components, make_case, failure):
    case = make_case(love=29)
    if failure == "inventory":
        mutate(case, lambda state: next(row for row in state["tables"]["possessions"] if row["ItemID"] == "gift_0003_diamond").update(Count=0))
    elif failure == "mailbox":
        profile = case.store.profile()
        state, _ = components.presents.enqueue(profile.state, [{"kind": "free_crystal", "amount": 1, "description": "full"}] * MAX_PRESENTS)
        case.store.update_state(state, profile.revision)
    elif failure in ("41", "42", "137"):
        attribute = {"41": "_unit_profile", "42": "_present_profile", "137": "_configuration_profile"}[failure]
        setattr(case.service, attribute, replace(getattr(case.service, attribute), response_limit=1))
    elif failure == "51":
        service = copy(case.service.card_status)
        service._response_profile = replace(service._response_profile, response_limit=1)
        case.service.card_status = service
    envelope = message(case)
    if failure == "gift":
        envelope = replace(envelope, profile=replace(envelope.profile, response_limit=1))
    fails_unchanged(case, envelope)


@pytest.mark.parametrize("failure", ["identity", "menu", "alternative", "single", "duplicate", "empty", "mixed", "maximum", "target"])
def test_invalid_business_request_is_atomic(components, make_case, failure):
    case = make_case(choose(components, rare=3, advanced=True) if failure == "mixed" else None,
                     love=100 if failure in ("mixed", "maximum") else 0)
    kwargs = {}
    if failure == "identity":
        kwargs["unique"] = 99999
    elif failure == "menu":
        kwargs["number"] = 101
    elif failure == "alternative":
        kwargs["alternative"] = 123
    elif failure == "single":
        kwargs["single"] = "gift_0003_diamond"
    elif failure == "duplicate":
        kwargs["slots"] = [("gift_0003_diamond", 1), (-1016865595, 1)]
    elif failure == "empty":
        kwargs["slots"] = [("gift_0003_diamond", 0)]
    elif failure == "mixed":
        kwargs["slots"] = [("gift_0103_ring", 1), ("gift_0003_diamond", 1)]
    elif failure == "target":
        kwargs["index"] = 0
    fails_unchanged(case, message(case, **kwargs))


@pytest.mark.parametrize("single", ["gift_0003_diamond", -1016865595])
def test_nonzero_single_item_has_no_native_writer_and_stays_unsupported(make_case, single):
    case = make_case()
    error = fails_unchanged(case, message(case, single=single), "gift_single_item_unsupported")
    assert error.status == 501


@pytest.mark.parametrize("kind,code", [("identity", "gift_target_mismatch"), ("malformed", "invalid_request_fields")])
def test_refused_gift_is_logged_because_the_native_client_resends_it(make_case, caplog, kind, code):
    case = make_case()
    number = message(case).profile.registration_keys[0]
    envelope = message(case, unique=99999) if kind == "identity" else request(case.store, number, ())
    with caplog.at_level(logging.WARNING, logger="aigis_ps"):
        fails_unchanged(case, envelope, code)
        case.service.send(message(case), case.store)
    assert [record.getMessage() for record in caplog.records if record.name == "aigis_ps"] == [
        f"Gift {number} refused with {code}; the native client shows a communication error and resends the same request until the server accepts it"]


def test_nonzero_bonus_rule_is_not_silently_ignored(make_case):
    case = make_case()
    catalog = copy(case.service.catalog)
    catalog._gifts = dict(catalog._gifts)
    catalog._gifts["gift_0003_diamond"] = replace(catalog.gift("gift_0003_diamond"), bonus_ratio=10)
    case.service.catalog = catalog
    fails_unchanged(case, message(case), "gift_rule_unsupported")


def test_previous_event_progress_never_decreases(make_case):
    case = make_case(love=0, watch=2)
    response = nodes(case.service.send(message(case), case.store))
    assert target(case)["Love"] == 5 and target(case)["WatchLoveEv"] == 2
    assert response["PPU"].children[2].values == (2,)
    assert case.store.profile().state["tables"]["card_status"][0]["A7"] == 0


def test_exact_response_survives_service_and_store_restart_without_another_debit(make_case):
    case = make_case(love=29)
    envelope = message(case)
    response = case.service.send(envelope, case.store)
    committed = case.store.profile()
    original = case.service
    case.service = GiftService(original.bootstrap, original.players, original.possessions, original.card_status,
                               original.presents, original.catalog, original.aliases, clock=lambda: case.clock[0])
    case.store = Store(case.store.path)
    assert case.service.send(envelope, case.store) == response
    assert case.store.profile() == committed
    assert len(committed.state["tables"]["presents"]) == 1


def test_new_second_repeats_same_gift_and_different_body_can_share_a_second(make_case):
    case = make_case()
    case.service.send(message(case), case.store)
    case.service.send(message(case, slots=[("gift_0002_ruby", 1)]), case.store)
    case.service.send(message(case, tick=1001), case.store)
    assert target(case)["Love"] == 14
    assert count(case, "gift_0003_diamond") == 997
    assert count(case, "gift_0002_ruby") == 998


@pytest.mark.parametrize("reason", ["revision", "expiry", "clock_backwards", "content"])
def test_old_exact_body_never_becomes_a_new_gift(make_case, reason):
    case = make_case()
    envelope = message(case)
    case.service.send(envelope, case.store)
    if reason == "revision":
        mutate(case, lambda state: state.update(unrelated_marker=True))
    elif reason == "expiry":
        case.clock[0] += timedelta(seconds=301)
    elif reason == "clock_backwards":
        case.clock[0] -= timedelta(seconds=1)
    else:
        case.service.content_version = "f" * 64
    fails_unchanged(case, envelope)


class WallClock(datetime):
    def timestamp(self):
        assert self.tzinfo is not None, "A zone-less reading would be converted through the host zone"
        return super().timestamp()


def test_without_its_own_clock_the_service_reads_the_bootstrap_clock(components):
    data = components.bundle
    injected = copy(data.bootstrap)
    injected.clock = lambda: NOW
    def build(bootstrap, **clock):
        return GiftService(bootstrap, data.player_state, data.possessions, components.status,
                           components.presents, components.catalog, components.aliases, **clock)
    assert build(data.bootstrap).clock is data.bootstrap.clock is utc_now
    assert build(injected).clock is injected.clock
    for clock in (NOW, "utc_now"):
        with pytest.raises(ValueError):
            build(injected, clock=clock)


def test_naive_clock_readings_commit_the_jst_wall_clock_instant(make_case):
    case = make_case()
    case.clock[0] = WallClock(2026, 11, 1, 12)
    case.service.send(message(case), case.store)
    receipt, = case.store.profile().state["gift_state"]["history"]["receipts"]
    assert receipt["committed_at"] == datetime(2026, 11, 1, 12, tzinfo=JST).timestamp()


def test_the_content_version_is_the_status_fingerprint_its_receipts_record(make_case):
    case = make_case()
    case.service.send(message(case), case.store)
    receipt, = case.store.profile().state["gift_state"]["history"]["receipts"]
    assert receipt["content"] == case.service.content_version == case.service.status()["content_version"]


def test_evicted_same_second_request_cannot_be_reapplied(make_case):
    case = make_case()
    first = message(case, slots=[("gift_0000_bouquet", 1)])
    case.service.send(first, case.store)
    for quantity in range(2, HISTORY_LIMIT + 2):
        current = target(case)["Love"]
        if current >= 90:
            mutate(case, lambda state: next(unit for unit in state["player_state"]["units"] if unit["GetIndex"] == case.indices[0]).update(Love=0))
        case.service.send(message(case, slots=[("gift_0000_bouquet", quantity)]), case.store)
    history = case.store.profile().state["gift_state"]["history"]
    assert len(history["receipts"]) == HISTORY_LIMIT and len(history["processed_keys"]) == HISTORY_LIMIT + 1
    fails_unchanged(case, first, "gift_request_stale")


def test_client_clock_rollback_and_signed_client_times_still_accept_new_gifts(make_case):
    case = make_case()
    for tick in (1002, 1001, 0x7FFFFFFF, -0x80000000, -1):
        case.service.send(message(case, tick=tick, slots=[("gift_0002_ruby", 1)]), case.store)
    assert count(case, "gift_0002_ruby") == 994
    history = case.store.profile().state["gift_state"]["history"]
    assert history["schema_version"] == SCHEMA_VERSION and len(history["processed_keys"]) == 5


def test_processed_window_is_bounded_and_forgets_the_oldest_request(make_case):
    case = make_case()
    case.service.send(message(case), case.store)
    def fill(state):
        history = state["gift_state"]["history"]
        history["processed_keys"] = [f"{index:064x}" for index in range(PROCESSED_LIMIT - 1)] + history["processed_keys"]
    mutate(case, fill)
    case.service.send(message(case, slots=[("gift_0002_ruby", 1)]), case.store)
    processed = case.store.profile().state["gift_state"]["history"]["processed_keys"]
    assert len(processed) == PROCESSED_LIMIT and processed[0] == f"{1:064x}"


def legacy_journal(case, *, seen=()):
    journal = case.store.profile().state["gift_state"]
    receipt = journal["history"]["receipts"][0]
    entry = {"key": GiftService._entry_key(receipt["request"], receipt["revision"], receipt["content"]),
             "request_key": receipt["request"], "client_time": 2000, "response_revision": receipt["revision"],
             "committed_at": receipt["committed_at"], "content_version": receipt["content"], "response": receipt["response"]}
    return {"schema_version": 1, "reward_scope": "card", "claims": journal["claims"], "last_client_time": 2000,
            "seen_keys": [*seen, receipt["request"]], "history": [entry]}


def test_legacy_journal_upgrades_replays_its_entry_and_accepts_an_earlier_time(make_case):
    case = make_case()
    envelope = message(case, tick=2000)
    response = case.service.send(envelope, case.store)
    request_key = case.store.profile().state["gift_state"]["history"]["receipts"][0]["request"]
    evicted = message(case, tick=2000, slots=[("gift_0002_ruby", 1)])
    evicted_key = hashlib.sha256(f"{evicted.profile.identity}:{evicted.profile.route}:{evicted.digest}".encode()).hexdigest()
    legacy = legacy_journal(case, seen=(evicted_key,))
    rewrite(case, lambda state: state.update(gift_state=legacy))
    profile = case.store.profile()
    upgraded = case.service.preserve_history(profile.state, profile.revision)["gift_state"]
    assert upgraded["schema_version"] == 2 and upgraded["claims"] == legacy["claims"]
    assert upgraded["history"]["processed_keys"] == [evicted_key, request_key]
    assert case.service.send(envelope, case.store) == response
    fails_unchanged(case, evicted, "gift_request_stale")
    case.service.send(message(case, tick=1999, slots=[("gift_0001_quartz", 1)]), case.store)
    history = case.store.profile().state["gift_state"]["history"]
    assert history["processed_keys"] == [evicted_key, request_key, history["receipts"][-1]["request"]]


@pytest.mark.parametrize("corruption", ["entry_key", "entry_field", "watermark", "schema"])
def test_corrupt_legacy_journal_is_never_upgraded_silently(make_case, corruption):
    case = make_case()
    case.service.send(message(case), case.store)
    legacy = legacy_journal(case)
    if corruption == "entry_key":
        legacy["history"][0]["key"] = "0" * 64
    elif corruption == "entry_field":
        legacy["history"][0]["client_time"] = True
    elif corruption == "watermark":
        legacy["last_client_time"] = None
    else:
        legacy["schema_version"] = 2
    mutate(case, lambda state: state.update(gift_state=legacy))
    fails_unchanged(case, message(case, tick=1001), "gift_state_invalid")


@pytest.mark.parametrize("journal", ["baseline", "stored", "legacy"])
def test_read_is_the_journal_preserve_history_saves_without_copying_the_save(make_case, monkeypatch, journal):
    case = make_case(watch=2)
    if journal != "baseline":
        case.service.send(message(case), case.store)
    if journal == "legacy":
        legacy = legacy_journal(case)
        rewrite(case, lambda state: state.update(gift_state=legacy))
    profile = case.store.profile()
    state, original = profile.state, deepcopy(profile.state)
    copied = []
    monkeypatch.setattr(gift_service, "deepcopy", lambda value, *args: copied.append(value) or deepcopy(value, *args))
    current = case.service.read(state, profile.revision)
    assert not any(value is state for value in copied)
    assert current == case.service.preserve_history(state, profile.revision)["gift_state"]
    assert any(value is state for value in copied)
    assert current["claims"] == {str(target(case)["CardID"]): [1, 100]}
    current["claims"].clear()
    current["history"]["receipts"].clear()
    assert state == original


def test_two_concurrent_different_bodies_preserve_both_updates_and_shared_claim(components, make_case, monkeypatch):
    card_id = choose(components, rare=3, threshold=30)
    case = make_case(card_id, love=29, extra=[(card_id, 29, 0)])
    envelopes = [message(case, index=index) for index in case.indices]
    barrier, lock, original, calls = Barrier(2), Lock(), case.store.update_state, [0]
    def update(state, revision):
        with lock:
            calls[0] += 1
            synchronize = calls[0] <= 2
        if synchronize:
            barrier.wait(timeout=15)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda envelope: case.service.send(envelope, case.store), envelopes))
    assert all(target(case, index)["Love"] == 34 for index in case.indices)
    assert count(case, "gift_0003_diamond") == 997
    assert len(case.store.profile().state["tables"]["presents"]) == 1
    assert sum(len(nodes(response)["TrustReward"].children[0].values) for response in results) == 1


def test_cas_retry_preserves_unrelated_competing_change(make_case, monkeypatch):
    case = make_case()
    original, calls = case.store.update_state, [0]
    def update(state, revision):
        calls[0] += 1
        if calls[0] == 1:
            changed = case.store.profile().state
            changed["concurrent_marker"] = 7
            original(changed, revision)
        return original(state, revision)
    monkeypatch.setattr(case.store, "update_state", update)
    case.service.send(message(case), case.store)
    assert calls[0] == 2 and case.store.profile().state["concurrent_marker"] == 7
    assert count(case, "gift_0003_diamond") == 998


@pytest.mark.parametrize("mutation", ["schema", "scope", "claim", "history", "processed", "commit_time", "legacy_field"])
def test_malformed_journal_is_preserved_instead_of_reset(make_case, mutation):
    case = make_case()
    case.service.send(message(case), case.store)
    def change(state):
        journal = state["gift_state"]
        if mutation == "schema":
            journal["schema_version"] = 3
        elif mutation == "scope":
            journal["reward_scope"] = "unit"
        elif mutation == "claim":
            journal["claims"]["1"] = [True]
        elif mutation == "history":
            journal["history"]["receipts"][0]["content"] = "0"
        elif mutation == "processed":
            journal["history"]["processed_keys"].insert(0, True)
        elif mutation == "commit_time":
            journal["history"]["receipts"][0]["committed_at"] = True
        else:
            journal["last_client_time"] = None
    mutate(case, change)
    fails_unchanged(case, message(case, tick=1001), "gift_state_invalid")
