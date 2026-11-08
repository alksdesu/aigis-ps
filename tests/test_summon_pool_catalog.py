from dataclasses import FrozenInstanceError
from fractions import Fraction
import hashlib
import math
import struct
from types import SimpleNamespace

import lz4.frame
import pytest

import aigis_ps.summon_pool_catalog as module
from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.errors import ProtocolError
from aigis_ps.summon_pool_catalog import (
    FIELDS,
    MAX_CANDIDATES,
    MAX_POOLS,
    RESOURCE_NAME,
    SummonPoolCatalog,
)
from test_al_format import schema_bytes
from test_public_tables import digested
from test_quest_service import bundle


class Cards:
    def __init__(self, identifiers=(2, 3, 4, 5, 6)):
        self.card_ids = list(identifiers)
        self.raw = b"verified-card-master"
        self.snapshot = SimpleNamespace(get_xml=lambda _: self.raw)


def row(identifier=100, card=2, percent=100.0, **changes):
    result = {"_ID": identifier, "_NameID": 123, "_TexID": 456, "_TexScl": 100,
              "_CardID": card, "_Level": 1, "PersonalRatio": percent, "EvolutionCount": 0}
    result.update(changes)
    return result


def table(rows, schema=None):
    schema = tuple(FIELDS.items()) if schema is None else tuple(schema)
    stride = 4 * len(schema)
    fields = [(name, index * 4, kind, b"\0" * (12 if kind == 4 else 8))
              for index, (name, kind) in enumerate(schema)]
    header = schema_bytes(fields, stride)
    start = 20 + len(header)
    records = b"".join(struct.pack("<f" if kind == 4 else "<i", source[name])
                       for source in rows for name, kind in schema)
    return struct.pack("<4sBBHHHI4s", b"ALTB", 1, 0x10, len(rows), 20, start, stride, b"summ") + header + records


def component(rows=None, *, cards=None, raw=None):
    cards = Cards() if cards is None else cards
    payload = table([row()] if rows is None else rows) if raw is None else raw
    return SimpleNamespace(catalog=SummonPoolCatalog(cards, lambda _: payload), cards=cards, payload=payload)


def invalid(callback):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert (caught.value.code, caught.value.status) == ("summon_pool_catalog_invalid", 503)


@pytest.fixture(scope="module")
def current(bundle):
    reader = lambda name: bundle.native_cdn.assets.read(name)[0]
    raw = reader(RESOURCE_NAME)
    decoded = decompress_all4(raw) if raw.startswith(b"ALL4") else raw
    return SimpleNamespace(catalog=SummonPoolCatalog(bundle.gifts.catalog, reader), raw=raw,
                           rows=parse_table(decoded)["rows"], bundle=bundle)


def test_real_resource_has_all_479_pools_and_187_complete_distributions(current):
    status = current.catalog.status()
    assert status["ready"] is True and status["error"] is None
    assert status["pools"] == 479
    assert status["complete_probability_pools"] == 187
    assert status["resource_sha256"] == hashlib.sha256(current.raw).hexdigest()
    assert len(current.rows) == 45333
    assert sum(len(pool.candidates) for pool in current.catalog.pools.values()) == 45333
    assert len(current.catalog.content_version) == 64


def test_real_group_headers_preserve_all_rows_and_exact_probability_ratios(current):
    grouped, identifier = {}, None
    for source in current.rows:
        if source["_ID"]:
            identifier = source["_ID"]
            grouped[identifier] = []
        grouped[identifier].append(source)
    assert set(grouped) == set(current.catalog.pools)
    for identifier, rows in grouped.items():
        pool = current.catalog.pools[identifier]
        assert pool.pool_id == identifier and pool.name_id == rows[0]["_NameID"]
        assert pool.percent_sum == math.fsum(source["PersonalRatio"] for source in rows)
        total = sum((Fraction.from_float(source["PersonalRatio"]) for source in rows), Fraction())
        for candidate, source in zip(pool.candidates, rows, strict=True):
            assert (candidate.card_id, candidate.level, candidate.evolution_count, candidate.percent) == (
                source["_CardID"], source["_Level"], source["EvolutionCount"], source["PersonalRatio"])
            if total:
                assert Fraction(candidate.weight, pool.total_weight) == Fraction.from_float(candidate.percent) / total
            else:
                assert candidate.weight == 0


def test_all_real_incomplete_pools_fail_before_rng(current):
    calls = []
    def rng(limit):
        calls.append(limit)
        return 0
    rejected = 0
    for identifier, pool in current.catalog.pools.items():
        if pool.complete:
            assert current.catalog.require_complete(identifier) is pool
        else:
            with pytest.raises(ProtocolError) as caught:
                current.catalog.draw(identifier, rng)
            assert caught.value.code == "summon_pool_incomplete"
            rejected += 1
    assert rejected == 292 and not calls


def test_pool350_retains_509_candidates_and_their_real_initial_levels(current):
    pool = current.catalog.require_complete(350)
    assert len(pool.candidates) == 509
    assert {candidate.level for candidate in pool.candidates} == {1, 10, 20, 30}
    for candidate in pool.candidates:
        values = current.bundle.player_state.initial_unit_values(candidate.card_id, candidate.level,
                                                                  evolution_count=candidate.evolution_count)
        assert values.class_id > 0 and values.stage in (0, 2)
        assert values.experience >= 0 and values.skill_id >= 0
        if candidate.level > 1:
            assert values.experience > 0


@pytest.mark.parametrize("identifier", [1, 51, 349])
def test_known_incomplete_ticket_pools_are_not_promoted_to_full_probability(current, identifier):
    pool = current.catalog.get(identifier)
    assert not pool.complete and 0 < pool.percent_sum < 100
    with pytest.raises(ProtocolError) as caught:
        current.catalog.require_complete(identifier)
    assert caught.value.code == "summon_pool_incomplete"


def test_integer_weights_preserve_original_float32_ratios_instead_of_decimal_rounding():
    case = component([row(percent=0.1), row(0, 3, 0.2), row(0, 4, 99.7)])
    pool = case.catalog.require_complete(100)
    exact = [Fraction.from_float(struct.unpack("<f", struct.pack("<f", value))[0]) for value in (0.1, 0.2, 99.7)]
    assert pool.total_weight == sum(candidate.weight for candidate in pool.candidates)
    assert math.gcd(*(candidate.weight for candidate in pool.candidates)) == 1
    for candidate, proportion in zip(pool.candidates, exact, strict=True):
        assert Fraction(candidate.weight, pool.total_weight) == proportion / sum(exact)
    assert tuple(candidate.weight for candidate in pool.candidates) != (1, 2, 997)


def test_weight_boundaries_skip_zero_probability_rows():
    catalog = component([row(percent=25.0), row(0, 3, 0.0), row(0, 4, 75.0), row(0, 5, 0.0)]).catalog
    pool = catalog.require_complete(100)
    assert tuple(candidate.weight for candidate in pool.candidates) == (1, 0, 3, 0)
    selected, limits = [], []
    for index in range(pool.total_weight):
        def rng(limit):
            limits.append(limit)
            return index
        selected.append(catalog.draw(100, rng).card_id)
    assert selected == [2, 4, 4, 4]
    assert limits == [4] * 4


def test_same_card_duplicate_candidates_keep_separate_levels_and_weights():
    catalog = component([row(percent=25.0), row(0, 2, 75.0, _Level=20)]).catalog
    pool = catalog.require_complete(100)
    assert len(pool.candidates) == 2
    assert (catalog.draw(100, lambda _: 0).level, catalog.draw(100, lambda _: 1).level) == (1, 20)
    assert tuple(candidate.weight for candidate in pool.candidates) == (1, 3)


@pytest.mark.parametrize("percents,complete", [([50, 50], True), ([50, 49.9995], True), ([50, 50.0005], True),
    ([50, 49.99], False), ([50, 50.01], False), ([0, 0], False), ([30, 20], False), ([100, 100], False)])
def test_complete_tolerance_does_not_normalize_missing_mass(percents, complete):
    catalog = component([row(100 if index == 0 else 0, index + 2, float(value))
                         for index, value in enumerate(percents)]).catalog
    assert catalog.get(100).complete is complete
    if not complete:
        with pytest.raises(ProtocolError) as caught:
            catalog.draw(100, lambda _: pytest.fail("Incomplete pools must not use randomness"))
        assert caught.value.code == "summon_pool_incomplete"


@pytest.mark.parametrize("value", [-1, 4, True, False, 0.0, "0", None])
def test_rng_requires_in_range_nonboolean_integer(value):
    catalog = component([row(percent=25.0), row(0, 3, 75.0)]).catalog
    with pytest.raises(ProtocolError) as caught:
        catalog.draw(100, lambda _: value)
    assert (caught.value.code, caught.value.status) == ("summon_random_invalid", 503)


def test_missing_rng_is_rejected_and_rng_exception_is_not_hidden():
    catalog = component().catalog
    with pytest.raises(ValueError, match="callable"):
        catalog.draw(100, None)
    def broken(_):
        raise RuntimeError("test random failure")
    with pytest.raises(RuntimeError, match="test random failure"):
        catalog.draw(100, broken)


@pytest.mark.parametrize("identifier", [0, -1, 101, True, None, [], 100.0, "100", 0x80000000])
def test_unknown_or_invalid_pool_identifier_is_explicitly_unavailable(identifier):
    with pytest.raises(ProtocolError) as caught:
        component().catalog.get(identifier)
    assert (caught.value.code, caught.value.status) == ("summon_pool_unknown", 501)


@pytest.mark.parametrize("rows", [[], [row(0)], [row(-1)], [row(), row()],
    [row(), row(101), row(100)], [row(card=1)], [row(card=999)], [row(_Level=-1)],
    [row(EvolutionCount=-1)]])
def test_invalid_headers_and_candidate_metadata_fail_catalog(rows):
    invalid(lambda: component(rows))


@pytest.mark.parametrize("percent", [-0.1, 100.1, float("nan"), float("inf"), float("-inf")])
def test_invalid_probability_values_are_not_sanitized(percent):
    invalid(lambda: component([row(percent=percent)]))


def test_header_metadata_belongs_to_group_head_only():
    catalog = component([row(percent=50.0), row(0, 3, 50.0, _NameID=0, _TexID=0, _TexScl=0),
                         row(101, 4, 100.0, _NameID=456)]).catalog
    assert catalog.get(100).name_id == 123
    assert catalog.get(101).name_id == 456
    assert catalog.get(100).candidates[1].card_id == 3


def test_zero_level_and_nondefault_stage_remain_explicit_metadata():
    candidate = component([row(_Level=0, EvolutionCount=2)]).catalog.get(100).candidates[0]
    assert candidate.level == 0 and candidate.evolution_count == 2


@pytest.mark.parametrize("kind", ["missing", "extra", "wrong_type", "duplicate"])
def test_schema_changes_are_explicitly_rejected(kind):
    schema = list(FIELDS.items())
    source = row()
    if kind == "missing":
        schema.pop()
    elif kind == "extra":
        schema.append(("Unknown", 1))
        source["Unknown"] = 0
    elif kind == "wrong_type":
        schema = [(name, 1 if name == "PersonalRatio" else value) for name, value in schema]
        source["PersonalRatio"] = 100
    else:
        schema.append(("_ID", 1))
    invalid(lambda: component(raw=table([source], schema)))


@pytest.mark.parametrize("raw", [b"", b"ALTB", b"ALL4", b"invalid", "not_bytes", bytearray(b"ALTB"), memoryview(b"ALTB")])
def test_resource_must_be_complete_bounded_bytes(raw):
    invalid(lambda: component(raw=raw))


def test_truncated_table_is_rejected():
    invalid(lambda: component(raw=table([row()])[:-1]))


def test_raw_and_all4_resources_produce_equal_pools_with_distinct_source_fingerprints():
    raw = table([row()])
    compressed = struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)
    first, second = component(raw=raw), component(raw=compressed)
    assert first.catalog.pools == second.catalog.pools
    assert first.catalog.content_version != second.catalog.content_version


@pytest.mark.parametrize("identifiers", [[], [1], [True], [2, -1], [2, 0x80000000], ["2"], [2.0]])
def test_missing_or_invalid_master_card_ids_are_rejected(identifiers):
    invalid(lambda: component(cards=Cards(identifiers)))


@pytest.mark.parametrize("raw", [b"", None, "raw", bytearray(b"raw")])
def test_missing_or_invalid_master_snapshot_is_rejected(raw):
    cards = Cards()
    cards.raw = raw
    invalid(lambda: component(cards=cards))


def test_missing_master_provider_and_resource_reader_are_rejected():
    invalid(lambda: component(cards=SimpleNamespace(card_ids=(2,))))
    invalid(lambda: SummonPoolCatalog(Cards(), None))
    def unavailable(_):
        raise OSError("fixture resource unavailable")
    invalid(lambda: SummonPoolCatalog(Cards(), unavailable))


@pytest.mark.parametrize("kind", ["snapshot", "mutable_copy", "card_ids", "provider_error"])
def test_master_dependency_change_blocks_all_public_sampling_paths(kind):
    case = component()
    assert case.catalog.is_ready() is case.catalog.status()["ready"] is True
    if kind == "snapshot":
        case.cards.raw += b"changed"
    elif kind == "mutable_copy":
        case.cards.raw = bytearray(case.cards.raw)
    elif kind == "card_ids":
        case.cards.card_ids.append(7)
    else:
        def unavailable(_):
            raise OSError("fixture master unavailable")
        case.cards.snapshot.get_xml = unavailable
    assert case.catalog.is_ready() is case.catalog.status()["ready"] is False
    assert case.catalog.status()["error"] == "summon_pool_catalog_changed"
    for action in (lambda: case.catalog.get(100), lambda: case.catalog.require_complete(100),
                   lambda: case.catalog.draw(100, lambda _: pytest.fail("Changed dependencies must not draw"))):
        with pytest.raises(ProtocolError) as caught:
            action()
        assert caught.value.code == "summon_pool_catalog_changed"


def test_card_master_is_digested_once_per_bytes_object(digested):
    case = component()
    verified = case.cards.raw
    for _ in range(3):
        assert case.catalog.is_ready() is case.catalog.status()["ready"] is True
    assert len(digested) == 1 and digested[0] is verified
    case.cards.raw = bytes(bytearray(verified))
    for _ in range(3):
        assert case.catalog.get(100).pool_id == 100
    assert len(digested) == 2 and digested[1] is case.cards.raw
    case.cards.raw = verified + b"changed"
    for _ in range(2):
        assert (case.catalog.status()["ready"], case.catalog.status()["error"]) == (False, "summon_pool_catalog_changed")
    assert len(digested) == 3 and digested[2] is case.cards.raw
    with pytest.raises(ProtocolError) as caught:
        case.catalog.require_complete(100)
    assert (caught.value.code, caught.value.status) == ("summon_pool_catalog_changed", 503)
    case.cards.raw = verified
    assert case.catalog.status()["ready"] is True and case.catalog.draw(100, lambda _: 0).card_id == 2
    assert len(digested) == 4 and digested[3] is verified


def test_real_master_snapshot_stays_verified_without_hashing_it_again(current, digested):
    assert current.catalog.is_ready() is current.catalog.status()["ready"] is True
    assert current.catalog.require_complete(350).pool_id == 350
    assert digested == []


@pytest.mark.parametrize("changed", [False, True])
def test_readiness_matches_status_without_building_it(monkeypatch, changed):
    case = component()
    if changed:
        case.cards.card_ids.append(7)
    ready = case.catalog.status()["ready"]
    assert ready is (not changed)
    monkeypatch.setattr(case.catalog, "status", lambda: pytest.fail("Readiness built the full catalog status"))
    assert case.catalog.is_ready() is ready


def test_rebuilding_changed_resource_changes_fingerprint_and_old_snapshot_is_immutable():
    cards = Cards()
    source = {"raw": table([row()])}
    first = SummonPoolCatalog(cards, lambda _: source["raw"])
    source["raw"] = table([row(card=3)])
    second = SummonPoolCatalog(cards, lambda _: source["raw"])
    assert first.get(100).candidates[0].card_id == 2
    assert second.get(100).candidates[0].card_id == 3
    assert first.content_version != second.content_version
    with pytest.raises(TypeError):
        first.pools[100] = second.get(100)
    with pytest.raises(FrozenInstanceError):
        first.get(100).total_weight = 0
    with pytest.raises(FrozenInstanceError):
        first.get(100).candidates[0].percent = 0


def test_pool_count_boundary_is_enforced():
    rows = [row(index + 1) for index in range(MAX_POOLS)]
    assert len(component(rows).catalog.pools) == MAX_POOLS
    invalid(lambda: component([*rows, row(MAX_POOLS + 1)]))


def test_candidate_count_boundary_is_enforced():
    rows = [row(percent=100.0), *[row(0, percent=0.0) for _ in range(MAX_CANDIDATES - 1)]]
    assert len(component(rows).catalog.get(100).candidates) == MAX_CANDIDATES
    invalid(lambda: component([*rows, row(0, percent=0.0)]))


def test_input_byte_limit_is_checked_before_parsing(monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(module, "LIMITS", replace(module.LIMITS, max_input_bytes=4))
    invalid(lambda: component())


def test_master_byte_limit_is_enforced(monkeypatch):
    limit = len(Cards().raw)
    monkeypatch.setattr(module, "MAX_MASTER_BYTES", limit)
    case = component()
    assert case.catalog.is_ready() is True
    monkeypatch.setattr(module, "MAX_MASTER_BYTES", limit - 1)
    assert case.catalog.is_ready() is case.catalog.status()["ready"] is False
    assert case.catalog.status()["error"] == "summon_pool_catalog_changed"
    invalid(lambda: component())
