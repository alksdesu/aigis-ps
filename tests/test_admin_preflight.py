from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import random
import secrets
from types import SimpleNamespace
import uuid

import pytest
from pydantic import Field

from aigis_ps.admin_command import (AdminCommand, AdminError, Batch, BatchStep, CommandParams, CommandResult, NoopPreflight,
                                    PlanStore, PreflightReport)
from aigis_ps.admin_commit import CommitPipeline, InitializerRestartCheck
from aigis_ps.admin_preflight import (BATTLE_PARAM_READ, FREE_CRYSTALS_READ, PAID_CRYSTALS_READ, PREFLIGHT_DOMAINS,
                                      PREFLIGHT_FAILED, PREFLIGHT_KEY, QUEST_RECORD_ROUTE, QUEST_RECORDS_READ,
                                      RESPONSE_XML_INVALID, ClientRead, GenerationPreflight, PreflightCandidate,
                                      PreflightDomain)
from aigis_ps.admin_registry import discover
from aigis_ps.alxml import DEFAULT_LIMITS, Column, Container, dumps, loads
from aigis_ps.background_state import background_response
from aigis_ps.bootstrap import QUEST_LIMITS, TABLES, BootstrapService
from aigis_ps.card_status import CARD_STATUS_FIELDS, CardStatusService
from aigis_ps.energy_clock import instant
from aigis_ps.errors import ProtocolError
from aigis_ps.event_pass_service import STATUS_LIMITS
from aigis_ps.filter_state import filter_response
from aigis_ps.native_numbers import INT32_MAX
from aigis_ps.peruse_state import peruse_response
from aigis_ps.player_state import MAX_PLAYER_UNITS, MAX_STANDARD_TEAMS, PlayerStateService
from aigis_ps.presents import PRESENT_LIMITS
from aigis_ps.startup_diagnostics import INTERNAL_ERROR
from aigis_ps.storage import Store
from aigis_ps.tower_state import saved_records
from aigis_ps.universal_flags import flags_response
from aigis_ps.vote_state import vote_history_response
from test_admin_command import fake_package
from test_admin_commit import FakeGeneration, FakeRuntime, FakeStore
from test_bootstrap import asset_root, quest_master, request as route_request
from test_card_status import snapshot as card_snapshot
from test_player_state import assets, master
from test_quest_service import bundle, request as registered_request


NOW = datetime(2026, 11, 3, 3, tzinfo=timezone.utc)
SAVED_AT = datetime(2026, 11, 3, 12)
MASTER_QUESTS = (1001, 1002)
ALL_DOMAINS = frozenset(PREFLIGHT_DOMAINS)
REQUIRED_DOMAINS = frozenset((
    "config", "possessions", "universal_flags", "perused", "backgrounds", "filters", "units", "squads", "presents",
    "game_param", "crystals", "card_status", "quest_records", "quest_state", "spending", "fairy", "silver", "missions",
    "packages", "rank_rewards", "login_rewards", "login_cooking", "event_login", "comeback_login", "purchased_login",
    "daily_gift", "sugoroku", "event_pass", "vote", "towers",
))
UNREAD_LEDGER_REASONS = {
    "gifts": "The client never reads the trust reward ledger; the gift service validates it before its next use",
    "evolution_rewards": "The client never reads the awakening reward ledger; the restart check validates it",
    "unit_overrides": ("The client never reads temporary unit overrides; only the training, silver exchange and "
                       "shop growth guards check them"),
}
NONE_DOMAINS = frozenset(("letstd", "sugoroku", *UNREAD_LEDGER_REASONS))
OPTIONAL_SERVICES = ("card_status", "fairy_farm", "silver_house", "missions", "packages", "event_passes", "towers")
SERVING_SERVICES = ("bootstrap", "player_state", "fairy_farm", "silver_house", "missions", "packages", "event_passes",
                    "votes", "towers")
MAPPED_READS = {read.request: read for entry in PREFLIGHT_DOMAINS.values() for read in entry.reads}
RESERVATION = [{"card_id": 7, "level": 1, "count": 2}]
SENTINEL_BATTLE = {"VN": "12.34.56", "UX": INT32_MAX, "TN": MAX_STANDARD_TEAMS - 1, "RTN": 171}


class Listing:
    def __init__(self, name, table):
        self.name, self.table, self.calls = name, table, 0

    def rows(self, state):
        rows = state["tables"].get(self.table, [])
        if not isinstance(rows, list) or any(type(row) is not int for row in rows):
            raise ProtocolError("fixture_rows_invalid", f"Fixture {self.table} rows are invalid", 503)
        return rows

    def response(self, state):
        self.calls += 1
        return Container(self.name, (Column("ID", "I", tuple(self.rows(state))),))


class MissionSnapshots(Listing):
    def __init__(self):
        super().__init__("GL", "missions")
        self.seen = []

    def preflight_snapshot(self, state, now, profile=None):
        self.seen.append((now, profile))
        return dumps(Container(children=(self.response(state),)))


class PackageListings(Listing):
    def __init__(self):
        super().__init__("PPF", "packages")
        self.operations = []

    def read_response(self, operation, profile, state, now):
        self.operations.append((operation, now))
        return dumps(Container(children=(self.response(state),)))


class TowerPortal:
    def __init__(self, inventory):
        self.catalog = SimpleNamespace(towers=())
        self.possessions = SimpleNamespace(
            response=lambda state, item_ids, include_enabled=False: inventory.response(state))

    def settle(self, state, now):
        saved_records(state)
        return state, ()


class Counting:
    def __init__(self, function):
        self.function, self.calls = function, 0

    def __call__(self, state):
        self.calls += 1
        return self.function(state)


@pytest.fixture
def services(asset_root, assets, master):
    inventory, passes = Listing("POSSESSION", "possessions"), Listing("EventPassStatus", "event_pass_status")
    bootstrap = BootstrapService(asset_root, master_provider=lambda name: quest_master(MASTER_QUESTS), clock=lambda: SAVED_AT,
                                 asset_version="a" * 40, possession_provider=inventory.response, filter_provider=filter_response,
                                 peruse_provider=peruse_response, universal_flag_provider=flags_response,
                                 background_provider=background_response, event_pass_provider=passes.response,
                                 vote_provider=Counting(vote_history_response))
    return SimpleNamespace(bootstrap=bootstrap, player_state=PlayerStateService(assets[0], lambda name: master),
                           presents=Listing("PPT", "presents"), card_status=CardStatusService(card_snapshot()),
                           fairy_farm=Listing("PFF", "fairy_farm"), silver_house=Listing("PSH", "silver_house"),
                           missions=MissionSnapshots(), packages=PackageListings(), event_passes=passes,
                           towers=TowerPortal(inventory))


@pytest.fixture
def state(services):
    return services.player_state.initialize(services.bootstrap.initial_state())


@pytest.fixture
def generation(services):
    return FakeGeneration(id="gen-preflight", bundle=services, clock=NOW)


@pytest.fixture(scope="module")
def preflight():
    return GenerationPreflight()


def check(preflight, state, domains, generation):
    return preflight.check(deepcopy(state), state, frozenset(domains), generation)


def failure(preflight, state, domains, generation):
    with pytest.raises(AdminError) as caught:
        check(preflight, state, domains, generation)
    error = caught.value
    assert error.code == PREFLIGHT_FAILED and error.status == 409
    assert str(error) == f"Response {error.details['response']} could not be encoded for the candidate state"
    return error


def served_readback(domains, services):
    readback = {}
    for name, entry in sorted(domains.items()):
        requests = tuple(read.request for read in entry.reads if read.served(services))
        if requests:
            readback[name] = requests
    return readback


def none_reasons(domains):
    return {name: PREFLIGHT_DOMAINS[name].none_reason for name in domains if not PREFLIGHT_DOMAINS[name].reads}


def unread_reasons(*domains):
    return {name: UNREAD_LEDGER_REASONS[name] for name in domains}


def quest_record(identifier, **changes):
    return {"QuestID": identifier, "MaxRank": 0, "AchieveFlag": 0, "RemainAssault": 0, "FirstDefeatDate": 0, "Shown": False,
            **changes}


def card_status_row(identifier):
    return {**dict.fromkeys(CARD_STATUS_FIELDS, 0), "CardID": identifier}


def long_user_name(state):
    state["tables"]["config"][0]["UserName"] = "n" * 40


def new_account_values(state):
    state["tables"]["game_param"][0].update(Money=0, Experience=0)


def rank_beyond_experience(state):
    state["tables"]["game_param"][0]["Rank"] += 1


def textual_spending_projection(state):
    state["tables"]["game_param2"][0]["StampCardGrade"] = "x"


def negative_paid_bucket(state):
    state["tables"]["crystals"] = [{"FreeFlag": False, "Num": -1, "Date": 1}]


def unknown_bucket_field(state):
    state["tables"]["crystals"] = [{"FreeFlag": False, "Num": 1, "Date": 1, "Expiry": 0}]


def negative_experience(state):
    state["player_state"]["units"][0]["Exp"] = -1


def unknown_unit_card(state):
    state["player_state"]["units"][0]["CardID"] = 999_999


def team_out_of_range(state):
    state["player_state"]["battle"]["TN"] = MAX_STANDARD_TEAMS


def full_mailbox(state):
    state["tables"]["presents"] = list(range(PRESENT_LIMITS.max_values))


def unknown_card_status(state):
    state["tables"]["card_status"] = [card_status_row(99)]
    state["card_status_schema"] = 1


def unknown_quest(state):
    state["tables"]["quest_records"] = [quest_record(9999)]


def negative_rank_record(state):
    state["tables"]["quest_records"] = [quest_record(1001, MaxRank=-1)]


def oversized_inventory(state):
    state["tables"]["possessions"] = list(range(DEFAULT_LIMITS.max_values))


def broken_flags(state):
    state["tables"]["universal_flags"] = [{"ID": "x"}]


def broken_perused(state):
    state["tables"]["perused_events"] = [{"ID": "x"}]


def broken_backgrounds(state):
    state["tables"]["backgrounds"] = [{"CardID": "x"}]


def broken_filters(state):
    state["tables"]["filter_settings"] = [{"FilterNameHash": "x"}]


def unserved_fame_stock(state):
    state["tables"]["fame_depository"] = [{"CardId": 2, "Stage": 0, "Count": 1}]


def broken_vote_history(state):
    state["tables"]["vote_history"] = [{"itemId": 1}]


def oversized_event_passes(state):
    state["tables"]["event_pass_status"] = list(range(STATUS_LIMITS.max_values))


def oversized_fairy_stock(state):
    state["tables"]["fairy_farm"] = list(range(DEFAULT_LIMITS.max_values))


def oversized_silver_stock(state):
    state["tables"]["silver_house"] = list(range(DEFAULT_LIMITS.max_values))


def broken_missions(state):
    state["tables"]["missions"] = ["broken"]


def broken_packages(state):
    state["tables"]["packages"] = ["broken"]


DOMAIN_FAILURES = [
    ("config", long_user_name, 36, "invalid_bootstrap_value"),
    ("game_param", new_account_values, 34, "invalid_player_state"),
    ("game_param", rank_beyond_experience, 34, "invalid_player_rank"),
    ("crystals", negative_paid_bucket, 43, "invalid_crystal_state"),
    ("crystals", unknown_bucket_field, 43, "bootstrap_state_unavailable"),
    ("units", negative_experience, 41, "player_units_invalid"),
    ("units", unknown_unit_card, 41, "player_reference_invalid"),
    ("squads", team_out_of_range, 39, "battle_param_invalid"),
    ("squads", unknown_unit_card, 72, "player_reference_invalid"),
    ("presents", full_mailbox, 42, RESPONSE_XML_INVALID),
    ("card_status", unknown_card_status, 51, "card_status_state_invalid"),
    ("quest_records", unknown_quest, QUEST_RECORD_ROUTE, "quest_record_master_mismatch"),
    ("quest_state", unknown_quest, QUEST_RECORD_ROUTE, "quest_record_master_mismatch"),
    ("towers", negative_rank_record, 66, "tower_records_invalid"),
    ("possessions", oversized_inventory, 129, RESPONSE_XML_INVALID),
    ("universal_flags", broken_flags, 137, "universal_flags_invalid"),
    ("perused", broken_perused, 137, "peruse_state_invalid"),
    ("backgrounds", broken_backgrounds, 137, "background_state_invalid"),
    ("filters", broken_filters, 137, "filter_state_invalid"),
    ("fame", unserved_fame_stock, 137, "collection_rows_unavailable"),
    ("vote", broken_vote_history, 137, "vote_state_invalid"),
    ("event_pass", oversized_event_passes, 158, RESPONSE_XML_INVALID),
    ("spending", textual_spending_projection, 33, "invalid_bootstrap_value"),
    ("login_rewards", textual_spending_projection, 33, "invalid_bootstrap_value"),
    ("login_cooking", textual_spending_projection, 33, "invalid_bootstrap_value"),
    ("comeback_login", textual_spending_projection, 33, "invalid_bootstrap_value"),
    ("event_login", full_mailbox, 42, RESPONSE_XML_INVALID),
    ("daily_gift", full_mailbox, 42, RESPONSE_XML_INVALID),
    ("rank_rewards", full_mailbox, 42, RESPONSE_XML_INVALID),
    ("purchased_login", full_mailbox, 42, RESPONSE_XML_INVALID),
    ("fairy", oversized_fairy_stock, 122, RESPONSE_XML_INVALID),
    ("silver", oversized_silver_stock, 126, RESPONSE_XML_INVALID),
    ("missions", broken_missions, 54, "fixture_rows_invalid"),
    ("packages", broken_packages, 46, "fixture_rows_invalid"),
]


def test_every_listed_domain_has_client_reads_or_an_explicit_none_reason():
    assert REQUIRED_DOMAINS <= ALL_DOMAINS
    assert {name for name, entry in PREFLIGHT_DOMAINS.items() if not entry.reads} == NONE_DOMAINS
    assert all(entry.none_reason.strip() for name, entry in PREFLIGHT_DOMAINS.items() if name in NONE_DOMAINS)
    assert all(entry.requests and entry.none_reason is None for name, entry in PREFLIGHT_DOMAINS.items() if name not in NONE_DOMAINS)
    assert {name for name, *_ in DOMAIN_FAILURES} == ALL_DOMAINS - NONE_DOMAINS


def test_unread_ledgers_are_none_domains_that_name_their_own_check():
    entries = {name: PREFLIGHT_DOMAINS.get(name) for name in UNREAD_LEDGER_REASONS}
    assert all(entry is not None and entry.reads == () and entry.none_reason.strip() for entry in entries.values())
    assert none_reasons(UNREAD_LEDGER_REASONS) == UNREAD_LEDGER_REASONS


def test_domain_table_maps_the_verified_client_reads():
    requests = {name: entry.requests for name, entry in PREFLIGHT_DOMAINS.items() if entry.reads}
    assert requests == {
        "backgrounds": (137,), "card_status": (51,), "comeback_login": (33, 35, 42), "config": (36,), "crystals": (43, 44),
        "daily_gift": (42,), "event_login": (42,), "event_pass": (137, 158), "fairy": (122,), "fame": (137,),
        "filters": (137,), "game_param": (33, 34, 35), "login_cooking": (33, 35, 42), "login_rewards": (33, 35, 42),
        "missions": (54,), "packages": (46, 47), "perused": (137,), "possessions": (129, 137), "presents": (42,),
        "purchased_login": (42, 46, 47), "quest_records": (QUEST_RECORD_ROUTE,), "quest_state": (QUEST_RECORD_ROUTE,),
        "rank_rewards": (42,), "silver": (126,), "spending": (33, 35), "squads": (39, 72), "towers": (66,),
        "units": (41,), "universal_flags": (137,), "vote": (137,)}


def test_generation_preflight_is_a_real_check_over_the_exported_table(preflight):
    assert GenerationPreflight.is_noop is False and NoopPreflight.is_noop is True
    assert dict(preflight.domains) == dict(PREFLIGHT_DOMAINS)
    with pytest.raises(TypeError):
        preflight.domains["config"] = PREFLIGHT_DOMAINS["letstd"]


def test_valid_candidate_passes_every_domain_and_reports_readback(preflight, services, state, generation):
    report = check(preflight, state, ALL_DOMAINS, generation)
    assert isinstance(report, PreflightReport)
    assert dict(report.readback) == served_readback(PREFLIGHT_DOMAINS, services)
    assert report.to_dict()["readback"]["possessions"] == [129, 137]


@pytest.mark.parametrize("domain,corrupt,response,code", DOMAIN_FAILURES,
                         ids=[f"{domain}-{corrupt.__name__}" for domain, corrupt, *_ in DOMAIN_FAILURES])
def test_each_domain_refuses_a_candidate_its_client_read_cannot_encode(preflight, state, generation, domain, corrupt, response,
                                                                       code):
    corrupt(state)
    error = failure(preflight, state, {domain}, generation)
    assert error.details["domains"] == [domain] and error.details["response"] == response
    assert error.payload()["details"]["error"]["code"] == code


def test_over_limit_refusals_keep_the_serializer_reason(preflight, state, generation):
    full_mailbox(state)
    error = failure(preflight, state, {"presents"}, generation)
    assert error.details["error"] == {"code": RESPONSE_XML_INVALID,
                                      "message": "XML value count or depth exceeds the configured limit"}


def test_a_shared_read_names_every_requested_domain_that_reaches_it(preflight, state, generation):
    broken_vote_history(state)
    error = failure(preflight, state, {"fame", "vote", "config", "possessions"}, generation)
    assert error.details["response"] == 137 and error.details["domains"] == ["fame", "possessions", "vote"]


def test_quest_records_also_refuse_duplicate_and_negative_records(preflight, state, generation):
    state["tables"]["quest_records"] = [quest_record(1001), quest_record(1001)]
    assert failure(preflight, state, {"quest_records"}, generation).details["error"]["code"] == "quest_record_master_mismatch"
    state["tables"]["quest_records"] = [quest_record(1001, RemainAssault=-1)]
    assert failure(preflight, state, {"quest_records"}, generation).details["error"]["code"] == "invalid_quest_record"
    state["tables"]["quest_records"] = [quest_record(1002, MaxRank=3, Shown=True)]
    assert dict(check(preflight, state, {"quest_records"}, generation).readback) == {"quest_records": (QUEST_RECORD_ROUTE,)}


def test_quest_record_reads_answer_unplayed_master_quests_with_zero_rows(state, generation):
    played = [quest_record(1002, MaxRank=3, Shown=True)]
    state["tables"]["quest_records"] = deepcopy(played)
    reply = loads(QUEST_RECORDS_READ.encode(PreflightCandidate(state, generation)), limits=QUEST_LIMITS)
    columns = {column.name: column.values for column in reply.children}
    assert columns["QuestID"] == (1002, 1001) and columns["A1"] == (3, 0) and columns["Shown"] == (True, False)
    assert state["tables"]["quest_records"] == played


def test_crystal_reads_answer_only_their_own_buckets(state, generation):
    state["tables"]["crystals"] = [{"FreeFlag": False, "Num": 5, "Date": 1}, {"FreeFlag": True, "Num": 2, "Date": 1}]
    target = PreflightCandidate(state, generation)
    for read, free, amount in ((PAID_CRYSTALS_READ, False, 5), (FREE_CRYSTALS_READ, True, 2)):
        columns = {column.name: column.values for column in loads(read.encode(target)).children}
        assert columns["A1"] == (free,) and columns["A2"] == (amount,)


def test_battle_param_reads_answer_with_the_served_encoder(preflight, state, generation, monkeypatch):
    state["player_state"]["battle"] = dict(SENTINEL_BATTLE)
    encoded = BATTLE_PARAM_READ.encode(PreflightCandidate(state, generation))
    assert encoded == PlayerStateService.battle_param_response(state["player_state"])
    assert {column.name: column.values for column in loads(encoded).children} == {
        name: (value,) for name, value in SENTINEL_BATTLE.items()}
    assert dict(check(preflight, state, {"squads"}, generation).readback) == {"squads": (39, 72)}
    answered = []

    def served(player):
        answered.append(player["battle"])
        return encoded

    monkeypatch.setattr(PlayerStateService, "battle_param_response", staticmethod(served))
    assert BATTLE_PARAM_READ.encode(PreflightCandidate(state, generation)) is encoded and answered == [SENTINEL_BATTLE]


def test_free_bucket_refusals_come_from_the_wallet_check(preflight, state, generation):
    state["tables"]["crystals"] = [{"FreeFlag": True, "Num": 3, "Date": 1}, {"FreeFlag": True, "Num": 2, "Date": 1}]
    error = failure(preflight, state, {"crystals"}, generation)
    assert error.details["response"] == 43 and error.details["error"]["code"] == "invalid_crystal_state"


def incompressible_fairy_stock(state):
    source = random.Random(0)
    state["tables"]["fairy_farm"] = [source.randrange(INT32_MAX) for _ in range(DEFAULT_LIMITS.max_values // 2)]


def test_replies_must_also_fit_the_native_receiver_capacity(preflight, state, generation):
    incompressible_fairy_stock(state)
    error = failure(preflight, state, {"fairy"}, generation)
    assert error.details["response"] == 122 and error.details["error"]["code"] == "response_too_large"


def test_startup_collections_keep_their_own_native_limits(preflight, state, generation):
    state["tables"]["possessions"] = [0] * (DEFAULT_LIMITS.max_values + 1)
    assert dict(check(preflight, state, {"filters"}, generation).readback) == {"filters": (137,)}
    assert failure(preflight, state, {"possessions"}, generation).details["response"] == 129


def test_quest_record_reads_keep_the_native_quest_limits(preflight, services, state, generation):
    identifiers = tuple(range(1, DEFAULT_LIMITS.max_values // len(TABLES["quest_records"].fields) + 2))
    services.bootstrap.master_provider = lambda name: quest_master(identifiers)
    assert dict(check(preflight, state, {"quest_records"}, generation).readback) == {"quest_records": (QUEST_RECORD_ROUTE,)}


def test_only_requested_table_domains_are_checked_and_unknown_names_are_ignored(preflight, state, generation):
    full_mailbox(state)
    report = check(preflight, state, {"config", "never_mapped"}, generation)
    assert dict(report.readback) == {"config": (36,)}


def test_none_domains_encode_nothing(preflight, generation):
    assert dict(check(preflight, {"tables": "broken"}, NONE_DOMAINS, generation).readback) == {}


def test_shared_reads_are_encoded_once_per_check(preflight, services, state, generation):
    report = check(preflight, state, {"vote", "fame", "filters", "backgrounds", "perused", "universal_flags",
                                      "presents", "rank_rewards", "daily_gift"}, generation)
    assert services.bootstrap.vote_provider.calls == 1 and services.presents.calls == 1
    assert dict(report.readback) == {"backgrounds": (137,), "daily_gift": (42,), "fame": (137,), "filters": (137,),
                                     "perused": (137,), "presents": (42,), "rank_rewards": (42,), "universal_flags": (137,),
                                     "vote": (137,)}


def test_reads_of_unavailable_services_are_skipped(preflight, services, state, generation):
    for name in OPTIONAL_SERVICES:
        setattr(services, name, None)
    services.bootstrap.event_pass_provider = None
    oversized_fairy_stock(state)
    report = check(preflight, state, ALL_DOMAINS, generation)
    readback = dict(report.readback)
    assert {"card_status", "fairy", "silver", "missions", "packages", "towers"}.isdisjoint(readback)
    assert readback["event_pass"] == (137,) and readback["purchased_login"] == (42,)
    assert readback == served_readback(PREFLIGHT_DOMAINS, services)


def test_time_dependent_reads_use_the_generation_clock_and_a_native_identity(preflight, services, state, generation):
    check(preflight, state, {"missions", "purchased_login"}, generation)
    [(now, profile)] = services.missions.seen
    assert now == NOW and services.packages.operations == [(46, NOW), (47, NOW)]
    assert profile.state is state and len(profile.method_key) == len(secrets.token_hex(16))


def test_unexpected_encoder_errors_still_name_the_response(preflight, services, state, generation):
    def broken(current):
        raise RuntimeError("fixture failure")

    services.fairy_farm.response = broken
    error = failure(preflight, state, {"fairy"}, generation)
    assert error.details["error"] == {"code": INTERNAL_ERROR, "message": "RuntimeError"}
    assert isinstance(error.__cause__, RuntimeError)


def test_unit_reads_try_the_reserved_battle_rewards_on_the_candidate_roster(preflight, services, state, generation, monkeypatch):
    state["quest_state"] = {"active": {"unit_reservations": deepcopy(RESERVATION), "frozen": [1]}}
    trials = []

    def trial(candidate, grants):
        trials.append((candidate["quest_state"], grants, candidate["player_state"]))
        return candidate, []

    monkeypatch.setattr(services.player_state, "credit_units", trial)
    assert dict(check(preflight, state, {"units"}, generation).readback) == {"units": (41,)}
    assert trials == [({"active": None}, RESERVATION, state["player_state"])]
    assert state["quest_state"]["active"]["unit_reservations"] == RESERVATION

    def full(candidate, grants):
        raise ProtocolError("unit_roster_capacity", "Reserved rewards no longer fit", 409)

    monkeypatch.setattr(services.player_state, "credit_units", full)
    assert failure(preflight, state, {"units"}, generation).details["error"]["code"] == "unit_roster_capacity"


def test_unit_reads_refuse_candidates_during_a_battle_without_reservations(preflight, state, generation):
    state["quest_state"] = {"active": {"quest_id": 1}}
    error = failure(preflight, state, {"units"}, generation)
    assert error.details["response"] == 41 and error.details["error"]["code"] == "quest_reservation_unavailable"


def test_preflight_requires_the_leased_generation_services(preflight, state):
    with pytest.raises(ValueError, match="services"):
        preflight.check(state, state, frozenset({"config"}), SimpleNamespace(now=lambda: NOW))


def encoder(candidate):
    return b"<DA></DA>"


@pytest.mark.parametrize("arguments", [
    (0, encoder), (True, encoder), ("", encoder), (36.0, encoder), (36, None), (36, encoder, "fairy farm"), (36, encoder, 7),
], ids=["zero", "boolean", "empty_route", "float", "no_encoder", "spaced_service", "numeric_service"])
def test_client_reads_validate_their_shape(arguments):
    with pytest.raises(ValueError):
        ClientRead(*arguments)


def test_preflight_domains_have_either_reads_or_a_reason():
    read = ClientRead(36, encoder)
    for arguments in ({}, {"reads": (read,), "none_reason": "both"}, {"none_reason": " "}, {"reads": (read, read)},
                      {"reads": ("36",)}, {"reads": "36"}):
        with pytest.raises(ValueError):
            PreflightDomain(**arguments)
    assert PreflightDomain([read]).reads == (read,) and PreflightDomain(none_reason="Server only").requests == ()


@pytest.mark.parametrize("domains", [
    {"Bad Name": PreflightDomain(none_reason="x")},
    {"config": "36"},
    {"config": PreflightDomain((ClientRead(36, encoder),)), "presents": PreflightDomain((ClientRead(36, lambda candidate: b""),))},
    {"config": PreflightDomain((ClientRead(999_999, encoder),))},
    {"config": PreflightDomain((ClientRead("no_such_route", encoder),))},
    ["config"],
], ids=["name", "entry", "conflicting_encoders", "unknown_registration", "unknown_route", "not_a_mapping"])
def test_preflight_tables_are_validated_against_native_contracts(domains):
    with pytest.raises(ValueError):
        GenerationPreflight(domains)


class NameParams(CommandParams):
    name: str = Field(min_length=1, max_length=64)


def rename(state, params, context):
    updated = deepcopy(state)
    updated["tables"]["config"][0]["UserName"] = params.name
    return CommandResult(updated)


RENAME = AdminCommand("config.user_name", "config", NameParams, rename, frozenset({"config"}))


def test_commit_pipeline_reports_readback_and_refuses_unencodable_plans(services, state, generation, preflight):
    store = FakeStore(state)
    subject = CommitPipeline(store, PlanStore(), preflight, InitializerRestartCheck(lambda value: value),
                             FakeRuntime(generation), backup_directory=Path("backups"))
    refused = subject.preview(Batch((BatchStep(RENAME, NameParams(name="n" * 40)),)))
    assert refused.plan_id is None and refused.preflight["ok"] is False
    assert refused.preflight["error"]["code"] == PREFLIGHT_FAILED and refused.preflight["error"]["details"]["response"] == 36
    plan = subject.preview(Batch((BatchStep(RENAME, NameParams(name="Admin")),)))
    assert plan.preflight == {"ok": True, "readback": {"config": [36]}}
    outcome = subject.commit(plan_id=plan.plan_id, plan_digest=plan.plan_digest, expected_revision=plan.expected_revision,
                             op_id=str(uuid.uuid4()))
    assert outcome.result["readback"] == {"config": [36]}
    assert store.state["tables"]["config"][0]["UserName"] == "Admin" and store.revision == 1


COMMAND_MODULE = """
from copy import deepcopy

from aigis_ps.admin_command import AdminCommand, CommandParams, CommandResult


class Params(CommandParams):
    pass


def apply(state, params, context):
    return CommandResult(deepcopy(state))


def register(registry):
    registry.add(AdminCommand("fixture.touch", "fixture", Params, apply, frozenset({touched!r}), preflight_none={exempt!r}))
"""


def test_every_discovered_command_has_preflight_or_an_explicit_reason():
    catalog = discover(PREFLIGHT_DOMAINS)
    for command in catalog.commands:
        assert command.touched <= ALL_DOMAINS | command.preflight_none.keys(), command.operation
        assert all(reason.strip() for reason in command.preflight_none.values())
        # The preflight table already decides every domain it maps, so a command's exemption there would only mislead.
        assert not command.preflight_none.keys() & ALL_DOMAINS, command.operation


def test_discovery_accepts_commands_touching_mapped_domains(fake_package):
    covered = discover(PREFLIGHT_DOMAINS, package=fake_package(admin_cmd_fixture=COMMAND_MODULE.format(
        touched={"config", "presents"}, exempt={})))
    assert [command.operation for command in covered.commands] == ["fixture.touch"]


def test_discovery_needs_a_reason_for_domains_outside_the_table(fake_package):
    with pytest.raises(ValueError, match="unmapped_ledger"):
        discover(PREFLIGHT_DOMAINS, package=fake_package(admin_cmd_fixture=COMMAND_MODULE.format(
            touched={"config", "unmapped_ledger"}, exempt={})))


def test_discovery_accepts_an_explicit_reason_for_a_domain_outside_the_table(fake_package):
    catalog = discover(PREFLIGHT_DOMAINS, package=fake_package(admin_cmd_fixture=COMMAND_MODULE.format(
        touched={"unmapped_ledger"}, exempt={"unmapped_ledger": "Server-side receipts only"})))
    assert dict(next(iter(catalog.commands)).preflight_none) == {"unmapped_ledger": "Server-side receipts only"}


@pytest.mark.parametrize("mapped", sorted(NONE_DOMAINS) + ["config"])
def test_discovery_refuses_a_reason_for_a_domain_the_table_maps(fake_package, mapped):
    with pytest.raises(ValueError, match=f"exempts domains that have a preflight: {mapped}"):
        discover(PREFLIGHT_DOMAINS, package=fake_package(admin_cmd_fixture=COMMAND_MODULE.format(
            touched={mapped, "unmapped_ledger"}, exempt={mapped: "Covered anyway", "unmapped_ledger": "Server-side only"})))


@pytest.fixture(scope="module")
def live_generation(bundle):
    return FakeGeneration(id="gen-live", bundle=bundle, clock=instant(bundle.bootstrap.clock()))


@pytest.fixture
def live_state(bundle):
    return bundle.initialize(bundle.bootstrap.initial_state())


def live_handlers(bundle, request):
    services = (getattr(bundle, name) for name in SERVING_SERVICES)
    return [handlers[request] for handlers in (service.handlers() for service in services if service is not None)
            if request in handlers]


# 66 is the only mapped read whose request carries a column: QUEST 0 asks for the tower portal.
LIVE_REQUEST_COLUMNS = {66: (Column("QUEST", "I", (0,)),)}


def live_request(store, request):
    if isinstance(request, str):
        return route_request(request, store)
    return registered_request(store, request, LIVE_REQUEST_COLUMNS.get(request, ()))


def test_live_bundle_serves_every_mapped_client_read(bundle):
    assert all(getattr(bundle, name) is not None for name in OPTIONAL_SERVICES)
    assert all(live_handlers(bundle, request) for request in MAPPED_READS)


def test_live_new_save_passes_every_domain(preflight, bundle, live_state, live_generation):
    report = check(preflight, live_state, ALL_DOMAINS, live_generation)
    assert dict(report.readback) == served_readback(PREFLIGHT_DOMAINS, bundle)
    assert dict(report.readback) == {name: entry.requests for name, entry in PREFLIGHT_DOMAINS.items() if entry.reads}


def test_live_reads_encode_exactly_what_their_handlers_serve(bundle, live_state, live_generation, tmp_path):
    store = Store(tmp_path / "served.sqlite3", live_state)
    for request, read in MAPPED_READS.items():
        for handler in live_handlers(bundle, request):
            # A first read may store startup bookkeeping, so the comparison uses the state the second read serves.
            handler(live_request(store, request), store)
            served = handler(live_request(store, request), store)
            profile = store.profile()
            expected = served.replace(profile.method_key.encode(), PREFLIGHT_KEY.encode())
            assert read.encode(PreflightCandidate(profile.state, live_generation)) == expected, request


def test_live_checks_never_write_to_the_candidate(preflight, live_state, live_generation):
    live_state["quest_state"] = {"active": {"unit_reservations": deepcopy(RESERVATION)}}
    untouched = deepcopy(live_state)
    check(preflight, live_state, ALL_DOMAINS, live_generation)
    assert live_state == untouched


def live_unknown_card_status(state):
    state["tables"]["card_status"] = [card_status_row(999_999_999)]
    state["card_status_schema"] = 1


def live_broken_present(state):
    state["tables"]["presents"] = [{"PresentID": 1}]


def live_unknown_item(state):
    state["tables"]["possessions"] = [{"ItemID": "fixture_unknown_item", "Count": 1}]


def live_unknown_quest(state):
    state["tables"]["quest_records"] = [quest_record(999_999_999)]


def live_broken_fairy_stock(state):
    state["tables"]["fairy_farm"], state["fairy_farm_schema"] = [{"CardID": -1, "Count": 1}], 1


def live_broken_silver_stock(state):
    state["tables"]["silver_house"], state["silver_house_schema"] = [{"CardID": -1, "Count": 1}], 1


def live_broken_missions(state):
    state["mission_state"] = {"schema_version": 0}


def live_broken_packages(state):
    state["package_purchases"] = {"schema_version": 0}


def live_broken_event_passes(state):
    state["tables"]["event_pass_status"] = [{"ID": 5}]


LIVE_FAILURES = [
    ("config", long_user_name, 36, "invalid_bootstrap_value"),
    ("game_param", new_account_values, 34, "invalid_player_state"),
    ("crystals", negative_paid_bucket, 43, "invalid_crystal_state"),
    ("units", negative_experience, 41, "player_units_invalid"),
    ("squads", team_out_of_range, 39, "battle_param_invalid"),
    ("presents", live_broken_present, 42, "present_state_invalid"),
    ("card_status", live_unknown_card_status, 51, "card_status_state_invalid"),
    ("quest_records", live_unknown_quest, QUEST_RECORD_ROUTE, "quest_record_master_mismatch"),
    ("possessions", live_unknown_item, 129, "possession_item_unknown"),
    ("universal_flags", broken_flags, 137, "universal_flags_invalid"),
    ("spending", textual_spending_projection, 33, "invalid_bootstrap_value"),
    ("fairy", live_broken_fairy_stock, 122, "fairy_farm_state_invalid"),
    ("silver", live_broken_silver_stock, 126, "silver_house_state_invalid"),
    ("missions", live_broken_missions, 54, "mission_state_invalid"),
    ("packages", live_broken_packages, 46, "package_purchase_state_invalid"),
    ("event_pass", live_broken_event_passes, 137, "event_pass_state_invalid"),
]


@pytest.mark.parametrize("domain,corrupt,response,code", LIVE_FAILURES,
                         ids=[f"{domain}-{corrupt.__name__}" for domain, corrupt, *_ in LIVE_FAILURES])
def test_live_services_refuse_candidates_their_reads_cannot_encode(preflight, live_state, live_generation, domain, corrupt,
                                                                 response, code):
    corrupt(live_state)
    error = failure(preflight, live_state, {domain}, live_generation)
    assert error.details["domains"] == [domain] and error.details["response"] == response
    assert error.details["error"]["code"] == code


def test_live_unit_reads_check_reserved_battle_rewards(preflight, bundle, live_state, live_generation):
    live_state["quest_state"] = {"active": {"unit_reservations": deepcopy(RESERVATION)}}
    assert dict(check(preflight, live_state, {"units"}, live_generation).readback) == {"units": (41,)}
    live_state["quest_state"]["active"]["unit_reservations"] = [{**RESERVATION[0], "count": MAX_PLAYER_UNITS}]
    error = failure(preflight, live_state, {"units"}, live_generation)
    assert error.details["response"] == 41 and error.details["error"]["code"] == "unit_roster_capacity"


def test_live_larger_save_passes_every_domain(preflight, bundle, live_state, live_generation):
    state, _ = bundle.player_state.credit_units(live_state, [{"card_id": 7, "level": 1, "count": 2000}])
    state, _ = bundle.presents.enqueue(state, [{"kind": "gold", "amount": 1, "description": "preflight"}] * 450)
    state["tables"]["crystals"] = [{"FreeFlag": index % 2 == 0, "Num": 1, "Date": 700_000 + index} for index in range(500)]
    untouched = deepcopy(state)
    report = check(preflight, state, ALL_DOMAINS, live_generation)
    assert dict(report.readback) == served_readback(PREFLIGHT_DOMAINS, bundle) and state == untouched
