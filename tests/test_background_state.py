from calendar import monthrange
from copy import deepcopy
from datetime import date, datetime, time
import re

import pytest

from aigis_ps.alxml import Column, Container, dumps, loads
from aigis_ps.background_state import (
    MAX_BACKGROUNDS,
    MAX_CONFIG_BYTES,
    MAX_CONFIG_ID_BYTES,
    STORED_CONFIG_ID_BYTES,
    background_response,
    merge_backgrounds,
    read_backgrounds,
)
from aigis_ps.business_calendar import NATIVE_DATETIME
from aigis_ps.energy_clock import JST
from aigis_ps.errors import ProtocolError
from aigis_ps.protocol import NATIVE_CELL_BYTES


def row(card_id=0, config_id="system_default", saved="2026/11/01 04:00:00"):
    return {"CardID": card_id, "ConfigID": config_id, "SaveTime": saved}


def state(rows=None, *, versioned=True):
    result = {"bootstrap_schema": 1, "tables": {"backgrounds": deepcopy(rows or []), "other": [1, {"a": 2}]},
              "player": {"units": []}, "receipt": {"nested": [3]}}
    if versioned:
        result["backgrounds_schema"] = 1
    return result


def rejects_read(candidate):
    before = deepcopy(candidate)
    with pytest.raises(ProtocolError) as caught:
        read_backgrounds(candidate)
    assert (caught.value.code, caught.value.status) == ("background_state_invalid", 503)
    assert candidate == before


def test_full_response_has_native_column_types_and_retains_row_order():
    rows = [row(17, "private_0001"), row(0), row(0x7FFFFFFF, "future_background")]
    response = background_response(state(rows))
    assert response == Container("PRM", (
        Column("CardID", "I", (17, 0, 0x7FFFFFFF)),
        Column("ConfigID", "S", ("private_0001", "system_default", "future_background")),
        Column("SaveTime", "S", ("2026/11/01 04:00:00",) * 3)))
    assert loads(dumps(Container(children=(response,)))).children == (response,)


@pytest.mark.parametrize("versioned", [True, False])
def test_empty_response_has_all_columns_and_does_not_migrate(versioned):
    source = state(versioned=versioned)
    before = deepcopy(source)
    assert read_backgrounds(source) == []
    assert background_response(source) == Container("PRM", (
        Column("CardID", "I", ()), Column("ConfigID", "S", ()), Column("SaveTime", "S", ())))
    assert source == before


def test_upsert_replaces_whole_rows_preserving_other_keys_and_history():
    old = row(17, "retired_event", "2000/02/29 00:00:00")
    source = state([row(), old])
    before = deepcopy(source)
    updates = [row(0, "system_fix", "2026/11/02 05:06:07"), row(88, "private_future")]
    updated = merge_backgrounds(source, updates)
    assert read_backgrounds(updated) == [updates[0], old, updates[1]]
    assert updated["tables"]["other"] == source["tables"]["other"]
    assert updated["player"] == source["player"]
    assert updated["receipt"] == source["receipt"]
    assert source == before
    updates[0]["ConfigID"] = "changed_after_merge"
    updated["receipt"]["nested"].append(4)
    updated["tables"]["other"][1]["a"] = 99
    assert read_backgrounds(updated)[0]["ConfigID"] == "system_fix"
    assert source == before


def test_same_configuration_accepts_new_explicit_save_time():
    source = state([row()])
    updated = merge_backgrounds(source, [row(saved="2026/11/02 04:00:00")])
    assert read_backgrounds(updated) == [row(saved="2026/11/02 04:00:00")]
    assert source["tables"]["backgrounds"] == [row()]


def test_empty_updates_return_isolated_equal_state_without_creating_schema():
    source = state(versioned=False)
    updated = merge_backgrounds(source, [])
    assert updated == source and updated is not source
    assert "backgrounds_schema" not in updated
    updated["receipt"]["nested"].append(4)
    updated["tables"]["other"][1]["a"] = 99
    assert source["receipt"]["nested"] == [3]
    assert source["tables"]["other"][1]["a"] == 2


def test_first_update_versions_empty_legacy_state_only_in_candidate():
    source = state(versioned=False)
    updated = merge_backgrounds(source, [row()])
    assert updated["backgrounds_schema"] == 1
    assert read_backgrounds(updated) == [row()]
    assert "backgrounds_schema" not in source and read_backgrounds(source) == []


def test_read_returns_independent_rows():
    source = state([row()])
    result = read_backgrounds(source)
    result[0]["ConfigID"] = "changed"
    result.append(row(5))
    assert read_backgrounds(source) == [row()]


@pytest.mark.parametrize("selection", [[17, 0], (17, 0), []])
def test_selected_response_uses_requested_order(selection):
    source = state([row(0), row(17, "private_0001"), row(42, "old_event")])
    response = background_response(source, selection)
    assert response.children[0].values == tuple(selection)
    assert response.children[1].values == (("private_0001", "system_default") if selection else ())
    assert len(response.children) == 3
    assert len(read_backgrounds(source)) == 3


@pytest.mark.parametrize("selection", ["0", 0, {0}, [0, 0], [True], [-1], [0x80000000], [0.0], [None], list(range(MAX_BACKGROUNDS + 1))])
def test_invalid_selection_is_rejected(selection):
    source = state([row()])
    with pytest.raises(ProtocolError) as caught:
        background_response(source, selection)
    assert (caught.value.code, caught.value.status) == ("background_selection_invalid", 400)
    assert read_backgrounds(source) == [row()]


def test_selected_unknown_record_is_not_created():
    source = state([row()])
    with pytest.raises(ProtocolError) as caught:
        background_response(source, [17])
    assert (caught.value.code, caught.value.status) == ("background_not_found", 409)
    assert read_backgrounds(source) == [row()]


@pytest.mark.parametrize("version", [None, True, False, 0, 2, "1", 1.0])
def test_explicit_unknown_schema_is_rejected_even_if_empty(version):
    candidate = state()
    candidate["backgrounds_schema"] = version
    rejects_read(candidate)
    with pytest.raises(ProtocolError, match="Stored backgrounds"):
        merge_backgrounds(candidate, [row()])


def test_nonempty_unversioned_state_cannot_be_overwritten():
    candidate = state([row()], versioned=False)
    rejects_read(candidate)
    with pytest.raises(ProtocolError) as caught:
        merge_backgrounds(candidate, [])
    assert caught.value.code == "background_state_invalid"


@pytest.mark.parametrize("candidate", [None, [], {}, {"bootstrap_schema": True, "tables": {"backgrounds": []}},
    {"bootstrap_schema": 1, "tables": None}, {"bootstrap_schema": 1, "tables": {}},
    {"bootstrap_schema": 1, "tables": {"backgrounds": None}},
    {"bootstrap_schema": 1, "tables": {"backgrounds": ()}}])
def test_invalid_storage_shape_is_rejected(candidate):
    rejects_read(candidate)


@pytest.mark.parametrize("bad_row", [None, [], {}, {"CardID": 0, "ConfigID": "system_default"},
    {**row(), "extra": 1}, {**row(), "CardID": True}, {**row(), "CardID": -1},
    {**row(), "CardID": 0x80000000}, {**row(), "CardID": "0"}, {**row(), "CardID": 1.0}])
def test_invalid_rows_are_rejected_for_read_and_update(bad_row):
    rejects_read(state([bad_row]))
    source = state([row()])
    before = deepcopy(source)
    with pytest.raises(ProtocolError) as caught:
        merge_backgrounds(source, [bad_row])
    assert (caught.value.code, caught.value.status) == ("background_updates_invalid", 400)
    assert source == before


@pytest.mark.parametrize("name", [None, 1, "", "x" * (STORED_CONFIG_ID_BYTES + 1), "界" * 86, "bad\x00id", "\ud800", "\ufffe"])
def test_invalid_config_names_are_rejected(name):
    rejects_read(state([row(config_id=name)]))
    with pytest.raises(ProtocolError) as caught:
        merge_backgrounds(state(), [row(config_id=name)])
    assert caught.value.code == "background_updates_invalid"


def test_new_selections_follow_the_native_cell_and_stored_rows_keep_the_older_bound():
    assert MAX_CONFIG_BYTES == MAX_CONFIG_ID_BYTES == NATIVE_CELL_BYTES == 255
    assert STORED_CONFIG_ID_BYTES == 256


@pytest.mark.parametrize("name", ["x" * MAX_CONFIG_ID_BYTES, "界" * 85, "old<&>'\"event", "unknown_future_config"])
def test_names_within_the_native_cell_are_saved_preserved_and_xml_escaped(name):
    source = merge_backgrounds(state(), [row(config_id=name)])
    response = background_response(source)
    assert loads(dumps(Container(children=(response,)))).children[0].children[1].values == (name,)


@pytest.mark.parametrize("name", ["x" * STORED_CONFIG_ID_BYTES, "界" * 85 + "x"])
def test_stored_names_past_the_native_cell_stay_loadable_but_are_never_written_again(name):
    legacy = state([row(17, name)])
    before = deepcopy(legacy)
    assert read_backgrounds(legacy) == [row(17, name)]
    assert background_response(legacy, [17]).children[1].values == (name,)
    updated = merge_backgrounds(legacy, [row(0, "system_fix")])
    assert read_backgrounds(updated) == [row(17, name), row(0, "system_fix")]
    with pytest.raises(ProtocolError) as caught:
        merge_backgrounds(legacy, [row(17, name)])
    assert caught.value.code == "background_updates_invalid"
    with pytest.raises(ProtocolError) as caught:
        merge_backgrounds(state(), [row(5, name)])
    assert caught.value.code == "background_updates_invalid"
    assert legacy == before


@pytest.mark.parametrize("saved", [None, 1, "", "2026/1/01 04:00:00", "2026/11/1 04:00:00",
    "2026/11/01 4:00:00", "2026-11-01 04:00:00", "2026/11/01T04:00:00",
    "2026/11/01 04:00:00+09:00", "2026/11/01 04:00:00Z", "2026/11/01 04:00:00.000",
    "2026/11/01 04:00:00\n", "+2026/11/01 04:00:00", "-001/11/01 04:00:00", "0000/01/01 00:00:00",
    "2026/00/01 04:00:00", "2026/13/01 04:00:00", "2026/11/00 04:00:00", "2026/04/31 04:00:00",
    "2026/02/29 04:00:00", "1900/02/29 04:00:00", "2026/11/01 24:00:00", "2026/11/01 04:60:00",
    "2026/11/01 04:00:60", "２０２６/11/01 04:00:00"])
def test_invalid_jst_wall_clock_text_is_rejected(saved):
    rejects_read(state([row(saved=saved)]))
    with pytest.raises(ProtocolError) as caught:
        merge_backgrounds(state(), [row(saved=saved)])
    assert caught.value.code == "background_updates_invalid"


@pytest.mark.parametrize("saved", ["0001/01/01 00:00:00", "2000/02/29 23:59:59", "2024/02/29 00:00:00", "9999/12/31 23:59:59"])
def test_valid_calendar_bounds_are_preserved_without_timezone_conversion(saved):
    source = state([row(saved=saved)])
    assert read_backgrounds(source)[0]["SaveTime"] == saved
    assert background_response(source).children[2].values == (saved,)


PADDED_WALL_CLOCK = re.compile(r"([0-9]{4})/([0-9]{2})/([0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})\Z")
SAMPLE_YEARS = (1000, 1001, 1582, 1900, 1999, 2000, 2024, 2026, 2100, 2400, 9998, 9999)
SAMPLE_TIMES = (time(0), time(3, 59, 59, 999999), time(4), time(9, 5, 7), time(23, 59, 59, 500000))
FULL_WIDTH_DIGITS = str.maketrans("0123456789", "０１２３４５６７８９")


def padded_text(moment):
    return (f"{moment.year:04d}/{moment.month:02d}/{moment.day:02d} "
            f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}")


def padded_pattern_accepts(saved):
    match = PADDED_WALL_CLOCK.fullmatch(saved) if isinstance(saved, str) else None
    if match is None:
        return False
    try:
        datetime(*(int(part) for part in match.groups()))
    except ValueError:
        return False
    return True


def accepts(saved):
    try:
        read_backgrounds(state([row(saved=saved)]))
    except ProtocolError:
        return False
    return True


def sample_moments():
    for year in SAMPLE_YEARS:
        for month in range(1, 13):
            for day in (1, monthrange(year, month)[1]):
                for clock in SAMPLE_TIMES:
                    yield datetime.combine(date(year, month, day), clock, tzinfo=JST)


def year_edges():
    for year in SAMPLE_YEARS:
        yield from (datetime(year, 1, 1, tzinfo=JST), datetime(year, 2, monthrange(year, 2)[1], 9, 5, 7, tzinfo=JST),
                    datetime(year, 12, 31, 23, 59, 59, tzinfo=JST))


def calendar_edges(year):
    for month, day in ((2, 28), (2, 29), (2, 30), (4, 30), (4, 31), (12, 31), (12, 32), (0, 1), (13, 1), (1, 0)):
        yield f"{year:04d}/{month:02d}/{day:02d} 00:00:00"
    for clock in ("23:59:59", "24:00:00", "23:60:00", "23:59:60"):
        yield f"{year:04d}/12/31 {clock}"


def noncanonical_spellings(moment):
    text = moment.strftime(NATIVE_DATETIME)
    yield from (text.replace("/", "-"), text.replace(" ", "T"), text.replace(" ", "  "), text.replace(" ", "\t"),
                " " + text, "0" + text, text + "\n", text + ".000", text + "Z", text + "+09:00",
                text.translate(FULL_WIDTH_DIGITS), f"{moment.year % 100:02d}{text[4:]}")
    unpadded = f"{moment.year}/{moment.month}/{moment.day} {moment.hour}:{moment.minute}:{moment.second}"
    if unpadded != text:
        yield unpadded
    # strptime accepts a space-padded %d, which only the round trip rejects.
    if moment.day < 10:
        yield f"{text[:8]} {moment.day}{text[10:]}"


def test_native_format_writes_the_padded_wall_clock_text_for_four_digit_years():
    for moment in sample_moments():
        text = moment.strftime(NATIVE_DATETIME)
        assert text == padded_text(moment), moment
        assert accepts(text) and padded_pattern_accepts(text), text


def test_save_time_check_agrees_with_the_padded_pattern_for_four_digit_years():
    for year in SAMPLE_YEARS:
        for saved in calendar_edges(year):
            assert accepts(saved) is padded_pattern_accepts(saved), saved
    for moment in year_edges():
        for saved in noncanonical_spellings(moment):
            assert not accepts(saved) and not padded_pattern_accepts(saved), saved


def test_duplicate_cards_are_rejected_in_storage_and_updates():
    rejects_read(state([row(), row(config_id="system_fix")]))
    with pytest.raises(ProtocolError) as caught:
        merge_backgrounds(state(), [row(), row(config_id="system_fix")])
    assert caught.value.code == "background_updates_invalid"


@pytest.mark.parametrize("updates", [None, (), {}, "", [row()] * (MAX_BACKGROUNDS + 1)])
def test_updates_require_a_bounded_list(updates):
    with pytest.raises(ProtocolError) as caught:
        merge_backgrounds(state(), updates)
    assert caught.value.code == "background_updates_invalid"


def test_capacity_accepts_4096_complete_rows_and_replacement():
    rows = [row(identifier, "x" * MAX_CONFIG_ID_BYTES) for identifier in range(MAX_BACKGROUNDS)]
    source = state(rows)
    assert len(read_backgrounds(source)) == MAX_BACKGROUNDS
    response = background_response(source)
    assert len(loads(dumps(Container(children=(response,)))).children[0].children[0].values) == MAX_BACKGROUNDS
    updated = merge_backgrounds(source, [row(MAX_BACKGROUNDS - 1, "replacement")])
    assert len(read_backgrounds(updated)) == MAX_BACKGROUNDS
    assert read_backgrounds(updated)[-1]["ConfigID"] == "replacement"


def test_combined_capacity_failure_preserves_both_inputs():
    source = state([row(identifier) for identifier in range(MAX_BACKGROUNDS)])
    updates = [row(MAX_BACKGROUNDS, "new_background")]
    before = deepcopy(source)
    with pytest.raises(ProtocolError) as caught:
        merge_backgrounds(source, updates)
    assert (caught.value.code, caught.value.status) == ("background_capacity", 409)
    assert source == before and updates == [row(MAX_BACKGROUNDS, "new_background")]
    rejects_read(state([row(identifier) for identifier in range(MAX_BACKGROUNDS + 1)]))
