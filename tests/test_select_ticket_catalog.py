from dataclasses import FrozenInstanceError, replace
import hashlib
import struct
from types import SimpleNamespace

import lz4.frame
import pytest

import aigis_ps.select_ticket_catalog as module
from aigis_ps.al_format import decompress_all4, parse_table
from aigis_ps.errors import ProtocolError
from aigis_ps.select_ticket_catalog import FIELDS, MAX_CANDIDATES, MAX_CONFIGS, RESOURCE_NAME, SelectTicketCatalog
from test_al_format import schema_bytes
from test_image_aliases import command_id, command_table, encode
from test_public_tables import digested
from test_quest_service import bundle


class Cards:
    def __init__(self, identifiers=(2, 3, 4, 5, 6)):
        self.card_ids = list(identifiers)
        self.raw = b"verified-card-master"
        self.snapshot = SimpleNamespace(get_xml=lambda _: self.raw)


def row(identifier=100, card=2, **changes):
    return {"_ID": identifier, "_NameID": 123, "_TexID": 456, "_TexScl": 100,
            "_CardID": card, "_Level": 1, "EvolutionCount": 0, **changes}


def table(rows, schema=None):
    schema = tuple(FIELDS.items()) if schema is None else tuple(schema)
    stride = 4 * len(schema)
    fields = [(name, index * 4, kind, b"\0" * (12 if kind == 4 else 8))
              for index, (name, kind) in enumerate(schema)]
    header = schema_bytes(fields, stride)
    start = 20 + len(header)
    records = b"".join(struct.pack("<f" if kind == 4 else "<i", source[name])
                       for source in rows for name, kind in schema)
    return struct.pack("<4sBBHHHI4s", b"ALTB", 1, 0x10, len(rows), 20, start, stride, b"selt") + header + records


def component(rows=None, *, cards=None, raw=None, ratings=()):
    cards = Cards() if cards is None else cards
    payload = table([row()] if rows is None else rows) if raw is None else raw
    sources = {RESOURCE_NAME: payload, module.RATING_RESOURCE: command_table(ratings)}
    return SimpleNamespace(catalog=SelectTicketCatalog(cards, sources.__getitem__), cards=cards, payload=payload)


def invalid(callback):
    with pytest.raises(ProtocolError) as caught:
        callback()
    assert (caught.value.code, caught.value.status) == ("select_ticket_catalog_invalid", 503)


@pytest.fixture(scope="module")
def current(bundle):
    reader = lambda name: bundle.native_cdn.assets.read(name)[0]
    raw = reader(RESOURCE_NAME)
    decoded = decompress_all4(raw) if raw.startswith(b"ALL4") else raw
    return SimpleNamespace(catalog=SelectTicketCatalog(bundle.gifts.catalog, reader), raw=raw,
                           rows=parse_table(decoded)["rows"], bundle=bundle)


def test_real_resource_keeps_all_224_configs_and_19153_candidates(current):
    status = current.catalog.status()
    assert status["ready"] is True and status["error"] is None
    assert status["configs"] == 224 and status["candidates"] == 19153
    assert set(current.catalog.configs) == set(range(1, 225))
    assert status["resource_sha256"] == hashlib.sha256(current.raw).hexdigest()
    assert len(current.catalog.content_version) == 64


def test_all_real_candidates_preserve_physical_source_order_and_growth(current):
    grouped, identifier = {}, None
    for source_index, source in enumerate(current.rows):
        if source["_ID"]:
            identifier = source["_ID"]
            grouped[identifier] = []
        grouped[identifier].append((source_index, source))
    for identifier, rows in grouped.items():
        config = current.catalog.get(identifier)
        assert (config.config_id, config.name_id) == (identifier, rows[0][1]["_NameID"])
        assert len(config.candidates) == len(rows)
        assert len({candidate.card_id for candidate in config.candidates}) == len(rows)
        for candidate, (source_index, source) in zip(config.candidates, rows, strict=True):
            assert (candidate.card_id, candidate.level, candidate.evolution_count, candidate.source_index) == (
                source["_CardID"], source["_Level"], source["EvolutionCount"], source_index)


def test_all_actual_unique_candidates_have_supported_initialization(current):
    unique = {(candidate.card_id, candidate.level, candidate.evolution_count)
              for config in current.catalog.configs.values() for candidate in config.candidates}
    assert len(unique) == 1025
    initializer = current.bundle.player_state.unit_initializer()
    for card, level, stage in unique:
        values = initializer.values(card, level, evolution_count=stage)
        assert values.class_id > 0 and values.stage in (0, 2)
        assert values.experience >= 0 and values.skill_id >= 0


def test_real_level50_groups_do_not_normalize_to_level1(current):
    groups = [config for config in current.catalog.configs.values()
              if any(candidate.level == 50 for candidate in config.candidates)]
    assert [config.config_id for config in groups] == [190, 202]
    assert [len(config.candidates) for config in groups] == [2, 2]
    assert all(candidate.level == 50 and candidate.evolution_count == 0
               for config in groups for candidate in config.candidates)


def test_real_nonzero_continuation_texture_does_not_replace_group_name(current):
    assert current.rows[37]["_ID"] == current.rows[38]["_ID"] == 0
    assert current.rows[37]["_TexID"] == current.rows[38]["_TexID"] == 9453723
    assert current.catalog.get(2).name_id == 330
    assert {candidate.source_index for candidate in current.catalog.get(2).candidates} >= {37, 38}


def test_headers_and_unsorted_cards_remain_in_resource_order():
    catalog = component([row(9, 5), row(0, 2, _NameID=999, _TexID=1234), row(1, 4), row(0, 3)]).catalog
    assert tuple(catalog.configs) == (9, 1)
    assert [candidate.card_id for candidate in catalog.get(9).candidates] == [5, 2]
    assert [candidate.source_index for candidate in catalog.get(1).candidates] == [2, 3]
    assert catalog.get(9).name_id == 123


@pytest.mark.parametrize("level,stage", [(0, 0), (1, 2), (50, 0), (0x7fffffff, 0x7fffffff)])
def test_growth_metadata_is_preserved_for_transaction_validation(level, stage):
    candidate = component([row(_Level=level, EvolutionCount=stage)]).catalog.get(100).candidates[0]
    assert (candidate.level, candidate.evolution_count) == (level, stage)


@pytest.mark.parametrize("rows", [[], [row(0)], [row(-1)], [row(), row()], [row(), row(101), row(100)],
    [row(card=1)], [row(card=999)], [row(_Level=-1)], [row(EvolutionCount=-1)],
    [row(), row(0, 2)], [row(), row(0, 2, _Level=50)]],
    ids=["empty", "orphan", "negative_id", "duplicate_header", "repeated_header", "prince", "missing_card",
         "negative_level", "negative_stage", "duplicate_card", "ambiguous_duplicate_growth"])
def test_invalid_groups_and_candidates_fail_as_a_whole(rows):
    invalid(lambda: component(rows))


def test_same_card_in_distinct_configs_is_legal():
    catalog = component([row(1, 2), row(2, 2, _Level=50)]).catalog
    assert catalog.get(1).candidates[0].level == 1
    assert catalog.get(2).candidates[0].level == 50


@pytest.mark.parametrize("identifier", [0, -1, 999, True, False, "100", 100.0, None, 0x80000000])
def test_unknown_or_mistyped_config_is_not_selected(identifier):
    with pytest.raises(ProtocolError) as caught:
        component().catalog.get(identifier)
    assert (caught.value.code, caught.value.status) == ("select_ticket_unknown", 501)


@pytest.mark.parametrize("kind", ["missing", "extra", "wrong_type", "duplicate"])
def test_only_the_exact_seven_native_integer_columns_are_accepted(kind):
    schema, source = list(FIELDS.items()), row()
    if kind == "missing":
        schema.pop()
    elif kind == "extra":
        schema.append(("Unknown", 1))
        source["Unknown"] = 0
    elif kind == "wrong_type":
        schema = [(name, 4 if name == "_Level" else value) for name, value in schema]
    else:
        schema.append(("_ID", 1))
    invalid(lambda: component(raw=table([source], schema)))


@pytest.mark.parametrize("field", list(FIELDS))
def test_boolean_values_are_not_native_integer_metadata(monkeypatch, field):
    parsed = parse_table(table([row()]))
    parsed["rows"][0][field] = True
    monkeypatch.setattr(module, "parse_table", lambda *_: parsed)
    invalid(component)


@pytest.mark.parametrize("raw", [b"", b"ALTB", b"ALL4", b"invalid", "text", bytearray(b"ALTB"), memoryview(b"ALTB")])
def test_resource_must_be_bounded_immutable_complete_bytes(raw):
    invalid(lambda: component(raw=raw))


def test_truncated_resource_fails():
    invalid(lambda: component(raw=table([row()])[:-1]))


def test_all4_and_plain_resource_have_same_candidates_but_distinct_source_hashes():
    raw = table([row()])
    compressed = struct.pack("<4sHHI", b"ALL4", 1, 0, len(raw)) + lz4.frame.compress(raw)
    first, second = component(raw=raw).catalog, component(raw=compressed).catalog
    assert first.configs == second.configs
    assert first.content_version != second.content_version


@pytest.mark.parametrize("identifiers", [[], [1], [True], [2, -1], [2, 0x80000000], ["2"], [2.0]])
def test_invalid_master_card_identities_are_rejected(identifiers):
    invalid(lambda: component(cards=Cards(identifiers)))


@pytest.mark.parametrize("raw", [b"", None, "raw", bytearray(b"raw")])
def test_invalid_master_snapshot_is_rejected(raw):
    cards = Cards()
    cards.raw = raw
    invalid(lambda: component(cards=cards))


def test_missing_or_unavailable_dependencies_are_explicit_errors():
    invalid(lambda: component(cards=SimpleNamespace(card_ids=(2,))))
    invalid(lambda: SelectTicketCatalog(Cards(), None))
    def unavailable(_):
        raise OSError("unavailable resource")
    invalid(lambda: SelectTicketCatalog(Cards(), unavailable))


@pytest.mark.parametrize("kind", ["snapshot", "mutable_copy", "identities", "boolean_identity", "unavailable"])
def test_changed_master_blocks_get_and_reports_not_ready(kind):
    case = component()
    if kind == "snapshot":
        case.cards.raw += b"changed"
    elif kind == "mutable_copy":
        case.cards.raw = bytearray(case.cards.raw)
    elif kind == "identities":
        case.cards.card_ids.append(7)
    elif kind == "boolean_identity":
        case.cards.card_ids.append(True)
    else:
        def unavailable(_):
            raise OSError("unavailable master")
        case.cards.snapshot.get_xml = unavailable
    assert case.catalog.status()["ready"] is False
    assert case.catalog.status()["error"] == "select_ticket_catalog_changed"
    with pytest.raises(ProtocolError) as caught:
        case.catalog.get(100)
    assert caught.value.code == "select_ticket_catalog_changed"


def test_card_master_is_digested_once_per_bytes_object(digested):
    case = component()
    verified = case.cards.raw
    for _ in range(3):
        assert case.catalog.status()["ready"] is True
    assert case.catalog.get(100).config_id == 100
    assert len(digested) == 1 and digested[0] is verified
    case.cards.raw = bytes(bytearray(verified))
    for _ in range(3):
        case.catalog.ready()
    assert len(digested) == 2 and digested[1] is case.cards.raw
    case.cards.raw = verified + b"changed"
    for _ in range(2):
        assert case.catalog.status()["error"] == "select_ticket_catalog_changed"
    assert len(digested) == 3 and digested[2] is case.cards.raw
    with pytest.raises(ProtocolError) as caught:
        case.catalog.ready()
    assert (caught.value.code, caught.value.status) == ("select_ticket_catalog_changed", 503)
    assert len(digested) == 3


def test_real_card_master_is_not_digested_again(current, digested):
    assert current.catalog.status()["ready"] is True
    assert current.catalog.get(1).config_id == 1
    assert digested == []


def test_configs_candidates_and_old_resource_view_are_immutable():
    cards, source = Cards(), {"raw": table([row()])}
    reader = lambda name: source["raw"] if name == RESOURCE_NAME else command_table(())
    first = SelectTicketCatalog(cards, reader)
    source["raw"] = table([row(card=3)])
    second = SelectTicketCatalog(cards, reader)
    assert first.get(100).candidates[0].card_id == 2
    assert second.get(100).candidates[0].card_id == 3
    assert first.content_version != second.content_version
    with pytest.raises(TypeError):
        first.configs[100] = second.get(100)
    with pytest.raises(FrozenInstanceError):
        first.get(100).name_id = 999
    with pytest.raises(FrozenInstanceError):
        first.get(100).candidates[0].level = 999


def test_group_count_limit_is_checked():
    rows = [row(index + 1) for index in range(MAX_CONFIGS)]
    assert len(component(rows).catalog.configs) == MAX_CONFIGS
    invalid(lambda: component([*rows, row(MAX_CONFIGS + 1)]))


def test_candidate_count_limit_is_checked_without_reinterpreting_source_order():
    cards = Cards(range(2, MAX_CANDIDATES + 3))
    rows = [row(100 if index == 0 else 0, index + 2) for index in range(MAX_CANDIDATES)]
    assert len(component(rows, cards=cards).catalog.get(100).candidates) == MAX_CANDIDATES
    invalid(lambda: component([*rows, row(0, MAX_CANDIDATES + 2)], cards=cards))


def test_byte_limits_are_enforced_before_parsing(monkeypatch):
    monkeypatch.setattr(module, "LIMITS", replace(module.LIMITS, max_input_bytes=4))
    invalid(component)


def test_master_byte_limit_is_enforced(monkeypatch):
    limit = len(Cards().raw)
    monkeypatch.setattr(module, "MAX_MASTER_BYTES", limit)
    case = component()
    assert case.catalog.status()["ready"] is True
    monkeypatch.setattr(module, "MAX_MASTER_BYTES", limit - 1)
    assert case.catalog.status()["error"] == "select_ticket_catalog_changed"
    invalid(component)


@pytest.mark.parametrize("text", ["", " \n\t"])
def test_empty_rating_expression_has_the_native_default_allow_behavior(text):
    catalog = component(ratings=[(command_id("IsCanUseRating_2"), encode(text))]).catalog
    assert catalog.get(100).candidates[0].card_id == 2
    assert catalog.status()["unsupported_config_ids"] == []


@pytest.mark.parametrize("text", ["1;", "0;", 'IsPlatForm("R18");'])
def test_nonempty_rating_preserves_candidates_but_refuses_unverified_group(text):
    catalog = component([row(100, 2), row(101, 3)],
        ratings=[(command_id("IsCanUseRating_2"), encode(text))]).catalog
    assert catalog.status()["ready"] is True
    assert catalog.status()["unsupported_config_ids"] == [100]
    assert catalog.status()["unverified_rating_cards"] == [2]
    assert catalog.configs[100].candidates[0].card_id == 2
    assert catalog.get(100) is catalog.configs[100]
    assert catalog.get(101).candidates[0].card_id == 3
    with pytest.raises(ProtocolError) as caught:
        catalog.require_supported(100)
    assert (caught.value.code, caught.value.status) == ("select_ticket_rating_unsupported", 501)


def test_rating_resource_changes_the_fingerprint_without_rewriting_old_view():
    ordinary = component().catalog
    changed = component(ratings=[(command_id("IsCanUseRating_2"), encode("0;"))]).catalog
    assert ordinary.get(100).candidates == changed.configs[100].candidates
    assert ordinary.content_version != changed.content_version
    assert ordinary.status()["rating_sha256"] != changed.status()["rating_sha256"]


def test_unrelated_rating_does_not_disable_other_candidates():
    catalog = component(ratings=[(command_id("IsCanUseRating_3"), encode("0;"))]).catalog
    assert catalog.get(100).candidates[0].card_id == 2
    assert catalog.status()["unsupported_config_ids"] == []


def test_duplicate_rating_keys_fail_before_selection():
    key = command_id("IsCanUseRating_2")
    invalid(lambda: component(ratings=[(key, encode("")), (key, encode("0;"))]))


def test_invalid_rating_encoding_is_not_treated_as_missing():
    invalid(lambda: component(ratings=[(command_id("IsCanUseRating_2"), "not-hex")]))


def test_current_ratings_have_no_candidate_restrictions(current):
    status = current.catalog.status()
    assert status["unsupported_config_ids"] == status["unverified_rating_cards"] == []
    assert status["rating_sha256"] == hashlib.sha256(current.bundle.native_cdn.assets.read(module.RATING_RESOURCE)[0]).hexdigest()
