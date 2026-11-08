from collections import Counter
from copy import copy, deepcopy
from datetime import datetime, timedelta
import json
import time
from types import SimpleNamespace

import pytest

from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.item_summon_tickets import DEFAULT_ITEM_TICKET_POOLS, ItemSummonTicketService
from aigis_ps.letstd_catalog import RESOURCE_NAME, LetsTDCatalog
from aigis_ps.letstd_rules import GROUPS, PARTNERS, LetsTDRules
from aigis_ps.letstd_service import (LOCAL_LABEL, SETTLE_CAPACITY_ERRORS, VERIFICATION, LetsTDService,
    mail_description)
from aigis_ps.letstd_state import (DELIVERED, LEDGER_KEY, PENDING, SCHEDULED, complete_rows, defer_rows,
    group_record, letstd_clock, read_letstd)
from aigis_ps.presents import GIFT_BOX_DEPENDENCIES, ITEM_CATALOG, MAX_PRESENTS
from aigis_ps.summon_tickets import SummonTicketRules
from aigis_ps.table_cell_patch import patch_table_cells
from aigis_ps.table_row_append import append_table_rows
from test_event_login_service import wired_gift_box
from test_event_pass_service import item_catalogs
from test_letstd_catalog import native_sources
from test_quest_service import bundle


NOW = datetime(2026, 11, 2, 12, tzinfo=JST)
ALL = {partner: {name: True for name in GROUPS.values()} for partner in PARTNERS}
OSHIRORE_CAMPAIGN = {"oshirore": {"newChallenge": True}}
OSHIRORE_DELIVERED = (2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 14, 15, 16, 21, 22, 23, 24, 25)
OSHIRORE_TICKETS = (1, 9, 13, 17, 18, 19, 20)
PURCHASE_UNITS = (3, 6, 10)
TEMPLATES = {97: 24, 136: 62, 250: 173, 397: 375, 1269: 1043}
LETSTD_POOLS = {338, 341, 343, 344, 426, 461}
# The pools before the LetsTD ticket pools opened, so ticket rows give these tests a natural pending dependency.
NARROW_POOLS = tuple(pool for pool in DEFAULT_ITEM_TICKET_POOLS if pool not in LETSTD_POOLS)
DEFAULT_MAILS = 114
FIRST_SETTLE_SECONDS = 20


@pytest.fixture(scope="module")
def initial(bundle):
    bootstrap = copy(bundle.bootstrap)
    bootstrap.clock = lambda: NOW
    return bundle.initialize(bootstrap.initial_state())


def native_index(sources, partner, group, reward_id):
    return LetsTDCatalog(sources.__getitem__).get(partner, group, reward_id).source_index


def native_row(sources, index):
    raw = sources[RESOURCE_NAME]
    return parse_table(decompress_all4(raw) if raw.startswith(b"ALL4") else raw)["rows"][index]


@pytest.fixture(scope="module")
def narrow_presents(bundle):
    presents = copy(bundle.presents)
    presents.item_tickets = ItemSummonTicketService(bundle.bootstrap, bundle.player_state, presents, bundle.summon_pools,
                                                    bundle.item_summon_tickets.catalog, rules=SummonTicketRules(NARROW_POOLS))
    return presents


@pytest.fixture
def factory(bundle, initial, native_sources, narrow_presents):
    def build(partners=None, *, native_popup=False, changes=None, additions=None, default_pools=False):
        sources = dict(native_sources)
        if changes:
            sources[RESOURCE_NAME] = patch_table_cells(sources[RESOURCE_NAME], {
                native_index(native_sources, *key): values for key, values in changes.items()})
        if additions:
            sources[RESOURCE_NAME] = append_table_rows(sources[RESOURCE_NAME], additions)
        catalog = LetsTDCatalog(sources.__getitem__)
        presents = copy(bundle.presents if default_pools else narrow_presents)
        rules = LetsTDRules(partners=ALL if partners is None else partners, native_popup=native_popup)
        return SimpleNamespace(catalog=catalog, presents=presents, rules=rules, state=deepcopy(initial),
                               service=LetsTDService(catalog, presents, rules))
    return build


def ledger_rows(state):
    ledger = read_letstd(state)
    return {(partner, int(group), int(reward)): row for partner, groups in ledger["partners"].items()
            for group, record in groups.items() for reward, row in record["rows"].items()}


def keys(grants):
    return [(grant["partner"], grant["group"], grant["reward_id"]) for grant in grants]


def filled(case, state, free):
    rewards = [{"kind": "gold", "amount": 1, "description": "capacity"}] * (
        MAX_PRESENTS - len(case.presents.rows(state)) - free)
    return case.presents.enqueue(state, rewards)[0] if rewards else deepcopy(state)


def without_filler(case, state):
    updated = deepcopy(state)
    updated["tables"]["presents"] = [row for row in case.presents.rows(state) if row["Description"] != "capacity"]
    return updated


def fail(code, status):
    def action(*args, **kwargs):
        raise ProtocolError(code, "Injected failure", status)
    return action


def bounded(limit, calls=None):
    def validate(candidate, values):
        if calls is not None:
            calls.append(len(values))
        if len(values) > limit:
            raise ProtocolError("login_reward_response_capacity", "Injected response capacity", 409)
    return validate


def test_closed_switches_neither_arm_nor_deliver(factory):
    for case in (factory({}), factory({}, native_popup=True)):
        result, grants = case.service.settle(case.state, NOW, source=115)
        assert (result, grants) == (case.state, []) and LEDGER_KEY not in result
        assert case.service.response(grants) is None


def test_open_group_mails_supported_rows_and_keeps_disabled_ticket_pools_pending(factory):
    case = factory(OSHIRORE_CAMPAIGN)
    before = deepcopy(case.state)
    result, grants = case.service.settle(case.state, NOW, source=115)
    assert case.state == before
    assert keys(grants) == [("oshirore", 1, reward) for reward in OSHIRORE_DELIVERED]
    assert all((grant["via"], grant["popup"], grant["delivered_at"]) == (115, False, letstd_clock(NOW)) for grant in grants)
    mails = {mail["PresentID"]: mail for mail in case.presents.rows(result)}
    assert len(mails) == len(OSHIRORE_DELIVERED) == sum(len(grant["mail_ids"]) for grant in grants)
    rows = ledger_rows(result)
    for reward in OSHIRORE_TICKETS:
        record = rows["oshirore", 1, reward]
        assert (record["status"], record["reason"], record["dependency_version"]) == (
            PENDING, "summon_ticket_pool_disabled", case.service.dependency_version)
    assert {case.catalog.get("oshirore", 1, reward).item_id for reward in OSHIRORE_TICKETS} == {
        "item_smn_ticket_19113", "item_smn_ticket_85106", "item_smn_ticket_60005", "item_smn_ticket_40052"}
    record = group_record(read_letstd(result), "oshirore", 1)
    assert (record["source"], record["armed_at"], record["rules_version"], record["resource_sha256"]) == (
        "local_simulation", letstd_clock(NOW), case.rules.content_version, case.catalog.resource_sha256)
    assert all(group_record(read_letstd(result), partner, group) is None
               for partner in PARTNERS for group in GROUPS if (partner, group) != ("oshirore", 1))
    shown = {}
    for grant in grants:
        row = case.catalog.get("oshirore", 1, grant["reward_id"])
        mail = mails[grant["mail_ids"][0]]
        assert mail["Description"] == "【ローカル模擬】タワーディフェンスやろうぜ！キャンペーン報酬(城プロRE)"
        shown[row.item_type, row.card_id or row.item_id] = (mail["Type"], mail["Content"], mail.get("ItemID"))
    assert {key: value for key, value in shown.items() if key[0] == 2} == {
        (2, card): (1, template, None) for card, template in TEMPLATES.items()}
    assert shown[1, "gift_0003_diamond"] == (22, 20, "gift_0003_diamond")
    assert shown[1, "trade_item_005000"] == (22, 4, "trade_item_005000")
    assert shown[1, "item_smn_ticket_19158"] == (22, 1, "item_smn_ticket_19158")
    assert shown[4, ""] == (2, 5000000, None)
    assert sorted((mails[grant["mail_ids"][0]]["Type"], mails[grant["mail_ids"][0]]["Content"]) for grant in grants
                  if case.catalog.get("oshirore", 1, grant["reward_id"]).item_type == 3) == [(0, 5), (0, 5), (0, 7)]


def test_all_six_groups_keep_ticket_rows_pending_while_their_pools_are_closed(factory):
    case = factory()
    started = time.perf_counter()
    result, grants = case.service.settle(case.state, NOW, source=115)
    assert time.perf_counter() - started < FIRST_SETTLE_SECONDS
    assert len(grants) == len(case.presents.rows(result)) == 63
    assert Counter((row["status"], row["reason"]) for row in ledger_rows(result).values()) == {
        (DELIVERED, None): 63, (PENDING, "summon_ticket_pool_disabled"): 42}
    assert len(ledger_rows(result)) == len(case.catalog.rows) == 105
    assert [grant["mail_ids"][0] for grant in grants] == list(range(1, 64))
    tickets = case.presents.item_tickets.catalog
    pending = {tickets.definition(case.catalog.get(*key).item_id).pool_id
               for key, row in ledger_rows(result).items() if row["status"] == PENDING}
    assert pending == LETSTD_POOLS
    mailed = {mail["ItemID"] for mail in case.presents.rows(result) if mail.get("ItemID", "").startswith("item_smn_")}
    assert {tickets.definition(item).pool_id for item in mailed} == {463}
    assert case.service.response(grants) is None
    assert case.service.settle(result, NOW + timedelta(hours=1), source=116) == (result, [])


def test_default_pools_mail_every_row_of_all_six_groups(factory):
    case = factory(default_pools=True)
    started = time.perf_counter()
    result, grants = case.service.settle(case.state, NOW, source=115)
    assert time.perf_counter() - started < FIRST_SETTLE_SECONDS
    assert len(grants) == len(ledger_rows(result)) == len(case.catalog.rows)
    assert Counter(row["status"] for row in ledger_rows(result).values()) == {DELIVERED: len(case.catalog.rows)}
    mails = case.presents.rows(result)
    assert [mail_id for grant in grants for mail_id in grant["mail_ids"]] == [mail["PresentID"] for mail in mails]
    assert len(mails) == DEFAULT_MAILS
    tickets = case.presents.item_tickets.catalog
    mailed = {mail["ItemID"] for mail in mails if mail.get("ItemID", "").startswith("item_smn_")}
    assert {tickets.definition(item).pool_id for item in mailed} == LETSTD_POOLS | {463}


def test_ticket_rows_waiting_on_closed_pools_are_mailed_once_the_pools_open(factory):
    narrow = factory()
    waiting, first = narrow.service.settle(narrow.state, NOW, source=115)
    pending = {key for key, row in ledger_rows(waiting).items() if row["status"] == PENDING}
    opened = factory(default_pools=True)
    result, later = opened.service.settle(waiting, NOW + timedelta(hours=1), source=116)
    assert set(keys(later)) == pending and not set(keys(later)) & set(keys(first))
    assert all(row["status"] == DELIVERED for row in ledger_rows(result).values())
    assert len(opened.presents.rows(result)) == DEFAULT_MAILS
    assert opened.service.settle(result, NOW + timedelta(hours=2), source=115) == (result, [])


def test_first_come_first_served_between_operations_and_switch_cycles(factory):
    case = factory(OSHIRORE_CAMPAIGN)
    result, grants = case.service.settle(case.state, NOW, source=116)
    assert {grant["via"] for grant in grants} == {116}
    assert case.service.settle(result, NOW + timedelta(minutes=1), source=115) == (result, [])
    closed = factory({})
    assert closed.service.settle(result, NOW + timedelta(minutes=2), source=115) == (result, [])
    reopened = factory(OSHIRORE_CAMPAIGN)
    assert reopened.service.settle(result, NOW + timedelta(minutes=3), source=115) == (result, [])


def test_closing_a_group_pauses_it_including_pending_rows(factory):
    purchases = {"rlyeh": {"newPurchase": True}}
    case = factory({**OSHIRORE_CAMPAIGN, **purchases})
    crowded = filled(case, case.state, 5)
    result, grants = case.service.settle(crowded, NOW, source=115)
    assert keys(grants) == [("oshirore", 1, reward) for reward in OSHIRORE_DELIVERED[:5]]
    paused = [("oshirore", 1, reward) for reward in OSHIRORE_DELIVERED[5:]]
    waiting = [*paused, *(("rlyeh", 2, reward) for reward in PURCHASE_UNITS)]
    assert [ledger_rows(result)[key]["reason"] for key in waiting] == ["present_capacity"] * len(waiting)
    space = without_filler(case, result)
    closed = factory({})
    assert closed.service.settle(space, NOW + timedelta(hours=1), source=115) == (space, [])
    partial, grants = factory(purchases).service.settle(space, NOW + timedelta(hours=2), source=115)
    assert keys(grants) == [("rlyeh", 2, reward) for reward in PURCHASE_UNITS]
    assert [ledger_rows(partial)[key]["reason"] for key in paused] == ["present_capacity"] * len(paused)
    reopened = factory(OSHIRORE_CAMPAIGN)
    delivered, more = reopened.service.settle(partial, NOW + timedelta(hours=3), source=115)
    assert keys(more) == paused
    assert Counter(row["status"] for row in ledger_rows(delivered).values()) == {DELIVERED: 21, PENDING: 14}
    assert len(case.presents.rows(delivered)) == 21


def test_rearm_merges_rows_added_by_a_later_resource_without_redelivery(factory, native_sources):
    case = factory(OSHIRORE_CAMPAIGN)
    result, _ = case.service.settle(case.state, NOW, source=115)
    template = native_row(native_sources, native_index(native_sources, "oshirore", 1, 15))
    addition = {**template, "ConfigId": 106, "RewardId": 26, "Count": 1000}
    later = factory(OSHIRORE_CAMPAIGN, additions=[addition])
    unchanged, grants = later.service.settle(result, NOW + timedelta(hours=1), source=115)
    assert grants == [] and ("oshirore", 1, 26) not in ledger_rows(unchanged)
    assert {key: row for key, row in ledger_rows(unchanged).items() if row["status"] == DELIVERED} == {
        key: row for key, row in ledger_rows(result).items() if row["status"] == DELIVERED}
    rearmed = later.service.rearm(unchanged, "oshirore", 1, NOW + timedelta(hours=2))
    rows = ledger_rows(rearmed)
    assert rows["oshirore", 1, 26]["status"] == PENDING
    assert {key: row for key, row in rows.items() if key[2] != 26} == ledger_rows(unchanged)
    record = group_record(read_letstd(rearmed), "oshirore", 1)
    assert record["rearmed_at"] == letstd_clock(NOW + timedelta(hours=2)) and record["armed_at"] == letstd_clock(NOW)
    assert record["resource_sha256"] == later.catalog.resource_sha256 != case.catalog.resource_sha256
    final, grants = later.service.settle(rearmed, NOW + timedelta(hours=3), source=115)
    assert keys(grants) == [("oshirore", 1, 26)]
    assert later.presents.rows(final)[-1]["Content"] == 1000
    with pytest.raises(ProtocolError) as error:
        later.service.rearm(case.state, "oshirore", 1, NOW)
    assert (error.value.code, error.value.status) == ("letstd_group_unarmed", 409)


def test_definition_changes_defer_pending_rows_and_mark_delivered_rows_for_adaptation(factory):
    case = factory(OSHIRORE_CAMPAIGN)
    result, grants = case.service.settle(filled(case, case.state, 1), NOW, source=115)
    assert keys(grants) == [("oshirore", 1, 2)]
    space = without_filler(case, result)
    changed = factory(OSHIRORE_CAMPAIGN, changes={("oshirore", 1, 2): {"Count": 21}, ("oshirore", 1, 23): {"Count": 6}})
    deferred, grants = changed.service.settle(space, NOW + timedelta(hours=1), source=115)
    assert ("oshirore", 1, 23) not in keys(grants) and ("oshirore", 1, 2) not in keys(grants)
    assert len(grants) == len(OSHIRORE_DELIVERED) - 2
    rows = ledger_rows(deferred)
    assert (rows["oshirore", 1, 23]["status"], rows["oshirore", 1, 23]["reason"]) == (PENDING, "letstd_definition_changed")
    assert rows["oshirore", 1, 2]["needs_adaptation"] is False
    assert changed.service.settle(deferred, NOW + timedelta(hours=2), source=115) == (deferred, [])
    rearmed = changed.service.rearm(deferred, "oshirore", 1, NOW + timedelta(hours=3))
    rows = ledger_rows(rearmed)
    assert rows["oshirore", 1, 2]["needs_adaptation"] is True and rows["oshirore", 1, 2]["status"] == DELIVERED
    assert rows["oshirore", 1, 23]["reason"] is None
    final, grants = changed.service.settle(rearmed, NOW + timedelta(hours=4), source=115)
    assert keys(grants) == [("oshirore", 1, 23)]
    assert changed.presents.rows(final)[-1]["Content"] == 6
    assert len(changed.presents.rows(final)) == len(OSHIRORE_DELIVERED)


def test_removed_source_rows_wait_without_blocking_other_rows(factory):
    case = factory(OSHIRORE_CAMPAIGN)
    result, _ = case.service.settle(filled(case, case.state, 0), NOW, source=115)
    space = without_filler(case, result)
    moved = factory(OSHIRORE_CAMPAIGN, changes={("oshirore", 1, 25): {"RewardId": 99}})
    settled, grants = moved.service.settle(space, NOW + timedelta(hours=1), source=115)
    assert keys(grants) == [("oshirore", 1, reward) for reward in OSHIRORE_DELIVERED[:-1]]
    assert ledger_rows(settled)["oshirore", 1, 25]["reason"] == "letstd_source_unavailable"
    assert ("oshirore", 1, 99) not in ledger_rows(settled)


def reference_settle(service, state, now, source, validate=None):
    updated, grants = service.initialize(state), []
    ledger = read_letstd(updated)
    for partner, group in service.rules.enabled_groups:
        rows = group_record(ledger, partner, group)["rows"]
        for reward in sorted(int(key) for key in rows):
            record = rows[str(reward)]
            if record["status"] != PENDING or not (record["reason"] is None or record["reason"] in SETTLE_CAPACITY_ERRORS
                                                   or record["dependency_version"] != service.dependency_version):
                continue
            try:
                rewards = service.rewards(service.catalog.get(partner, group, reward))
                candidate, mailed = service.presents.enqueue_groups(updated, [rewards])
                candidate, delivered = complete_rows(candidate, [(partner, group, reward,
                    [mail["PresentID"] for mail in mailed[0]])], now, source, service.rules.native_popup)
                if validate is not None:
                    validate(deepcopy(candidate), [*grants, *delivered])
            except ProtocolError as failure:
                if failure.status != 501 and failure.code not in SETTLE_CAPACITY_ERRORS:
                    raise
                updated = defer_rows(updated, [(partner, group, reward, failure.code)], service.dependency_version)
                continue
            updated, grants = candidate, [*grants, *delivered]
    return updated, grants


def test_batch_fast_path_and_row_fallback_match_the_pure_row_algorithm(factory):
    case = factory({"rlyeh": {"newChallenge": True, "newPurchase": True}}, changes={("rlyeh", 1, 22): {"Count": 3}})
    armed, grants = case.service.settle(case.state, NOW, source=115, validate=fail("login_reward_response_capacity", 409))
    assert grants == [] and Counter(row["reason"] for row in ledger_rows(armed).values()) == {
        "login_reward_response_capacity": 21, "summon_ticket_pool_disabled": 14}
    later = NOW + timedelta(hours=1)
    for free, validate, delivered in ((23, None, 21), (22, None, 20), (16, None, 16), (23, bounded(10), 10),
                                      (16, bounded(15), 15)):
        crowded = filled(case, armed, free)
        result = case.service.settle(crowded, later, source=115, validate=validate)
        assert result == reference_settle(case.service, crowded, later, 115, validate)
        assert len(result[1]) == delivered
    rows = ledger_rows(case.service.settle(filled(case, armed, 16), later, source=115)[0])
    assert rows["rlyeh", 1, 22]["reason"] == "present_capacity"
    assert [rows["rlyeh", 1, reward]["status"] for reward in (23, 24, 25)] == [DELIVERED, DELIVERED, PENDING]
    assert [rows["rlyeh", 2, reward]["reason"] for reward in (3, 6, 10)] == ["present_capacity"] * 3


def test_native_popup_response_lists_new_reward_ids_per_partner_in_native_order(factory):
    case = factory({"rlyeh": {"newChallenge": True}, "monmusutd": {"newPurchase": True},
                    "oshirore": {"newPurchase": True}}, native_popup=True)
    result, grants = case.service.settle(case.state, NOW, source=115)
    assert {grant["popup"] for grant in grants} == {True}
    response = case.service.response(grants)
    purchased = ",".join(map(str, PURCHASE_UNITS))
    assert response == Container("LetsTDRewardParam", (Column("ServiceId", "S", ("oshirore", "monmusutd", "rlyeh")),
        Column("newChallenge", "S", ("", "", ",".join(map(str, OSHIRORE_DELIVERED)))),
        Column("newPurchase", "S", (purchased, purchased, ""))))
    dumps(Container(children=(response,)))
    assert case.service.settle(result, NOW + timedelta(hours=1), source=116) == (result, [])
    assert case.service.response([]) is None
    hidden = factory({"rlyeh": {"newChallenge": True}})
    _, grants = hidden.service.settle(hidden.state, NOW, source=115)
    assert grants and hidden.service.response(grants) is None


def test_mail_label_is_fixed_and_titles_are_joined_on_one_line(factory):
    case = factory()
    assert "label" not in LetsTDRules.__dataclass_fields__ and LOCAL_LABEL == "【ローカル模擬】"
    assert mail_description(case.catalog.get("rlyeh", 2, 3)) == "【ローカル模擬】やろうぜパック購入特典(ルルイエ少女隊)"
    assert mail_description(case.catalog.get("monmusutd", 1, 1)) == (
        "【ローカル模擬】タワーディフェンスやろうぜ！キャンペーン報酬(モンスター娘TD)")
    result, _ = case.service.settle(case.state, NOW, source=115)
    descriptions = {mail["Description"] for mail in case.presents.rows(result)}
    assert all(text.startswith(LOCAL_LABEL) and "\n" not in text for text in descriptions) and len(descriptions) == 6


def test_status_labels_the_local_simulation_and_counts_each_group(factory):
    case = factory({"monmusutd": {"newPurchase": True}}, native_popup=True)
    status = case.service.status()
    assert (status["ready"], status["error"], status["simulated"], status["native_tested"]) == (True, None, True, False)
    assert status["verification"] == VERIFICATION and "No real cross-game account" in VERIFICATION
    assert status["native_popup"] is True and status["label"] == LOCAL_LABEL and status["label_configurable"] is False
    assert status["groups"]["monmusutd"]["newPurchase"] == {"enabled": True, "rows": 10, "item_types": {"1": 7, "2": 3}}
    assert status["groups"]["oshirore"]["newChallenge"]["enabled"] is False
    assert status["groups"]["rlyeh"]["newChallenge"]["rows"] == 25 and status["catalog"]["row_count"] == 105
    assert status["rules"] == case.rules.to_dict() and status["content_version"] == case.service.content_version
    assert json.loads(json.dumps(status, ensure_ascii=False)) == status


def test_validation_callback_sees_cumulative_grants_and_cannot_change_candidates(factory):
    case = factory(OSHIRORE_CAMPAIGN)
    observed = []
    result, grants = case.service.settle(case.state, NOW, source=115, validate=lambda candidate, values: observed.append(
        (len(values), len(case.presents.rows(candidate)))))
    assert observed == [(18, 18)] and len(grants) == 18
    calls = []
    limited, grants = case.service.settle(case.state, NOW, source=115, validate=bounded(5, calls))
    assert calls == [18, 1, 2, 3, 4, 5, *[6] * 13] and len(grants) == 5
    assert [ledger_rows(limited)["oshirore", 1, reward]["reason"] for reward in OSHIRORE_DELIVERED[5:]] == [
        "login_reward_response_capacity"] * 13
    def mutate(candidate, values):
        candidate["tables"]["game_param"][0]["Money"] += 1
    for validate, code in ((mutate, "letstd_validation_invalid"), (fail("bad_provider", 503), "bad_provider")):
        with pytest.raises(ProtocolError) as error:
            case.service.settle(case.state, NOW, source=115, validate=validate)
        assert error.value.code == code and LEDGER_KEY not in case.state
    with pytest.raises(ValueError):
        case.service.settle(case.state, NOW, source=115, validate="validate")


def test_mail_candidate_and_receipt_check_the_real_reward_rows(factory, monkeypatch):
    case = factory(OSHIRORE_CAMPAIGN)
    original = case.presents.enqueue_groups
    def wrong(source, groups):
        result, mailed = original(source, groups)
        result["tables"]["presents"][-1]["Content"] += 1
        mailed[-1][-1]["Content"] += 1
        return result, mailed
    def unrelated(source, groups):
        result, mailed = original(source, groups)
        result["tables"]["game_param"][0]["Money"] += 1
        return result, mailed
    for replacement in (wrong, unrelated):
        monkeypatch.setattr(case.presents, "enqueue_groups", replacement)
        with pytest.raises(ProtocolError) as error:
            case.service.settle(case.state, NOW, source=115)
        assert error.value.code == "letstd_delivery_invalid"
    monkeypatch.setattr(case.presents, "enqueue_groups", original)
    result, grants = case.service.settle(case.state, NOW, source=115)
    case.service.validate_receipt(result, grants)
    altered = deepcopy(result)
    altered["tables"]["presents"][0]["Content"] += 1
    with pytest.raises(ProtocolError) as error:
        case.service.validate_receipt(altered, grants)
    assert error.value.code == "letstd_receipt_invalid"
    claimed = deepcopy(result)
    claimed["tables"]["presents"] = []
    case.service.validate_receipt(claimed, grants)
    changed = factory(OSHIRORE_CAMPAIGN, changes={("oshirore", 1, 2): {"Count": 21}})
    changed.service.validate_receipt(result, grants, verify_current=False)
    with pytest.raises(ProtocolError) as error:
        changed.service.validate_receipt(result, grants)
    assert error.value.code == "letstd_receipt_invalid"
    with pytest.raises(ValueError):
        case.service.validate_receipt(result, grants, verify_current=1)


def test_pending_rows_retry_only_after_capacity_failures_or_dependency_changes(factory, monkeypatch):
    case = factory(OSHIRORE_CAMPAIGN)
    result, _ = case.service.settle(filled(case, case.state, 17), NOW, source=115)
    original, calls = case.presents.preview, []
    def counted(rewards, existing):
        calls.append(rewards[0].get("item_id"))
        return original(rewards, existing)
    monkeypatch.setattr(case.presents, "preview", counted)
    space = without_filler(case, result)
    final, grants = case.service.settle(space, NOW + timedelta(hours=1), source=115)
    assert keys(grants) == [("oshirore", 1, OSHIRORE_DELIVERED[-1])]
    assert not {item for item in calls if item and item.startswith("item_smn_ticket_")} - {"item_smn_ticket_19158"}
    other = factory(OSHIRORE_CAMPAIGN, changes={("monmusutd", 2, 11): {"Count": 2}})
    assert other.service.dependency_version != case.service.dependency_version
    retried, grants = other.service.settle(final, NOW + timedelta(hours=2), source=115)
    assert grants == []
    assert {ledger_rows(retried)["oshirore", 1, reward]["dependency_version"] for reward in OSHIRORE_TICKETS} == {
        other.service.dependency_version}
    assert other.service.settle(retried, NOW + timedelta(hours=3), source=115) == (retried, [])
    popup = factory(OSHIRORE_CAMPAIGN, native_popup=True)
    assert popup.service.dependency_version == case.service.dependency_version
    assert popup.service.content_version != case.service.content_version


@pytest.mark.parametrize("changes,reason", [({"ItemType": 9}, "letstd_reward_unsupported"),
    ({"CardId": 1}, "present_template_unsupported"), ({"GeneralItemID": "missing_item"}, "present_item_unsupported")])
def test_unsupported_rows_stay_pending_while_other_rows_deliver(factory, changes, reason):
    key = ("oshirore", 1, 3) if "CardId" in changes else ("oshirore", 1, 2)
    case = factory(OSHIRORE_CAMPAIGN, changes={key: changes})
    result, grants = case.service.settle(case.state, NOW, source=115)
    assert key not in keys(grants) and len(grants) == len(OSHIRORE_DELIVERED) - 1
    assert (ledger_rows(result)[key]["status"], ledger_rows(result)[key]["reason"]) == (PENDING, reason)


def test_zero_item_quantity_is_unsupported(factory):
    case = factory(OSHIRORE_CAMPAIGN, changes={("oshirore", 1, 2): {"Count": 0}})
    result, _ = case.service.settle(case.state, NOW, source=115)
    assert ledger_rows(result)["oshirore", 1, 2]["reason"] == "letstd_reward_unsupported"


def test_ticket_rows_mail_one_present_per_ticket(factory):
    case = factory(OSHIRORE_CAMPAIGN, changes={("oshirore", 1, 21): {"Count": 3}})
    result, grants = case.service.settle(case.state, NOW, source=115)
    grant = next(grant for grant in grants if grant["reward_id"] == 21)
    mails = {mail["PresentID"]: mail for mail in case.presents.rows(result)}
    assert len(grant["mail_ids"]) == 3
    assert all((mails[number]["Type"], mails[number]["Content"], mails[number]["ItemID"]) == (
        22, 1, "item_smn_ticket_19158") for number in grant["mail_ids"])


def test_scheduled_rows_wait_for_their_jst_start(factory):
    early = datetime(2026, 5, 1, 12, tzinfo=JST)
    case = factory(OSHIRORE_CAMPAIGN)
    result, grants = case.service.settle(case.state, early, source=115)
    assert keys(grants) == [("oshirore", 1, reward) for reward in OSHIRORE_DELIVERED if reward < 21]
    assert [ledger_rows(result)["oshirore", 1, reward]["status"] for reward in range(21, 26)] == [SCHEDULED] * 5
    start = datetime(2026, 9, 18, 11, 30, tzinfo=JST)
    assert case.service.settle(result, start - timedelta(seconds=1), source=115) == (result, [])
    released, grants = case.service.settle(result, start, source=116)
    assert keys(grants) == [("oshirore", 1, reward) for reward in range(21, 26)]
    assert {row["status"] for key, row in ledger_rows(released).items() if key[2] >= 21} == {DELIVERED}


def test_arguments_and_clock_order_are_enforced(factory):
    case = factory(OSHIRORE_CAMPAIGN)
    for source in (117, "115", True):
        with pytest.raises(ValueError):
            case.service.settle(case.state, NOW, source=source)
    result, _ = case.service.settle(case.state, NOW, source=115)
    with pytest.raises(ProtocolError) as error:
        case.service.settle(result, NOW - timedelta(hours=1), source=115)
    assert (error.value.code, error.value.status) == ("letstd_clock_backwards", 409)
    with pytest.raises(ValueError):
        LetsTDService(case.catalog, case.presents, {"partners": {}})


def test_service_requires_stable_dependencies(factory):
    case = factory(OSHIRORE_CAMPAIGN)
    presents = copy(case.presents)
    presents.item_catalog = None
    with pytest.raises(ProtocolError) as error:
        LetsTDService(case.catalog, presents, case.rules)
    assert (error.value.code, error.value.status) == ("letstd_dependencies_invalid", 503)
    case.presents.asset_sha256 = "0" * 64
    with pytest.raises(ProtocolError) as error:
        case.service.settle(case.state, NOW, source=115)
    assert (error.value.code, error.value.status) == ("letstd_dependencies_changed", 503)
    assert case.service.status()["error"] == "letstd_dependencies_changed"


def test_receipts_of_every_gift_box_with_the_item_catalog_replay_and_others_stay_stale(factory, item_catalogs):
    case = factory(OSHIRORE_CAMPAIGN)
    current = copy(case.presents)
    current.item_catalog = item_catalogs.mailed
    assert all(getattr(current, name) is not None for name in GIFT_BOX_DEPENDENCIES)
    def build(presents):
        return LetsTDService(case.catalog, presents, case.rules)
    service = build(current)
    configurations = [configuration for configuration in current.gift_box_configurations()
                      if configuration[ITEM_CATALOG] is not None]
    committed = [build(wired_gift_box(current, configuration)) for configuration in configurations]
    assert [built.dependency_version for built in committed] == list(map(service.dependency_version_for, configurations))
    assert [built.content_version for built in committed] == list(map(service.content_version_for, configurations))
    assert (committed[0].dependency_version, committed[0].content_version) == (
        service.dependency_version, service.content_version)
    replayable = service.replayable_content_versions()
    assert replayable == {built.content_version for built in committed}
    assert len(replayable) == len({(configuration[ITEM_CATALOG], configuration["item_tickets"])
                                   for configuration in configurations})
    for name in GIFT_BOX_DEPENDENCIES:
        detached = copy(current)
        setattr(detached, name, None)
        if name == ITEM_CATALOG:
            with pytest.raises(ProtocolError) as error:
                build(detached)
            assert (error.value.code, error.value.status) == ("letstd_dependencies_invalid", 503)
            continue
        version = build(detached).content_version
        assert version in replayable and (version == service.content_version) is (name != "item_tickets")
    earlier = copy(current)
    earlier.item_catalog = item_catalogs.plain
    assert build(earlier).content_version in replayable - {service.content_version}
    for malformed in ({}, {**configurations[0], ITEM_CATALOG: None}, {**configurations[0], "item_tickets": "not a digest"}):
        with pytest.raises(ProtocolError) as error:
            service.content_version_for(malformed)
        assert (error.value.code, error.value.status) == ("letstd_dependencies_invalid", 503)
    assert factory(OSHIRORE_CAMPAIGN, native_popup=True).service.content_version not in replayable


def test_only_the_fingerprinted_gift_box_dependencies_need_a_version(factory):
    case = factory(OSHIRORE_CAMPAIGN)
    for name in GIFT_BOX_DEPENDENCIES:
        unversioned = copy(case.presents)
        setattr(unversioned, name, SimpleNamespace())
        if name in (ITEM_CATALOG, "item_tickets"):
            with pytest.raises(ProtocolError) as error:
                LetsTDService(case.catalog, unversioned, case.rules)
            assert (error.value.code, error.value.status) == ("letstd_dependencies_invalid", 503)
        else:
            assert LetsTDService(case.catalog, unversioned, case.rules).content_version == case.service.content_version
    case.presents.item_tickets = SimpleNamespace()
    status = case.service.status()
    assert (status["ready"], status["error"]) == (False, "letstd_dependencies_invalid")
