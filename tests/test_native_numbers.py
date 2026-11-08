import ast
from dataclasses import replace
from pathlib import Path
import sys
from xml.etree import ElementTree as ET

import pytest

import aigis_ps.al_format as al_format_module
import aigis_ps.native_numbers as native_numbers
import aigis_ps.possessions as possessions_module
import aigis_ps.system_literals as system_literals_module
import aigis_ps.unit_skin_catalog as unit_skin_module
from aigis_ps import (comeback_login_catalog, comeback_login_rules, comeback_login_state, crystal_wallet,
                      daily_gift_rules, daily_gift_state, event_login_rules, event_login_state, event_pass_catalog,
                      event_pass_revival, event_pass_rules, event_pass_state, gift_catalog, gift_service, image_aliases,
                      letstd_state, mission_catalog, mission_ep_bindings, mission_pass_points, package_catalog,
                      package_text_overlay, player_state, present_backlog, present_items, present_recovery, presents,
                      purchased_login_rules, purchased_login_state, purchased_login_texts, quest_catalog,
                      rank_reward_state, tower_state, unit_detail, unit_initialization, unit_sale_catalog, wallet_money)
from aigis_ps.al_format import ALFormatError
from aigis_ps.card_status import CardStatusService
from aigis_ps.config_state import CONFIGURATION_FIELDS, CONFIGURATION_REQUIRED_FIELDS, configuration_delta, configuration_row, patch_configuration
from aigis_ps.errors import ProtocolError
from aigis_ps.filter_state import read_filters
from aigis_ps.native_numbers import INT32_MAX, INT32_MIN, UINT32_MASK, float32, int32, signed32
from aigis_ps.player_state import RESERVED_SQUAD_GROUP, SQUAD_GROUP_SHIFT, SQUAD_TEAM_SHIFT, squad_address
from aigis_ps.possessions import PossessionService
from aigis_ps.protocol import WireProfile, encode_response
from aigis_ps.request_fields import values
from aigis_ps.resource_fetch import ResourceEntry, ResourceFetchError, ResourceFetcher
from aigis_ps.table_row_append import append_table_rows
from aigis_ps.universal_flags import read_flags, set_flags
from test_card_status import row as card_row, snapshot as card_snapshot, state as card_state
from test_possessions import GIFTS, item_table
from test_table_row_append import source as table_source
from test_unit_skin_catalog import Cards, card, catalog, identifier


BOUNDARY_VALUES = (-2 ** 31 - 1, -2 ** 31, -1, 0, 1, 2 ** 31 - 1, 2 ** 31, True, False, 1.0, "1", None)
DECIMAL_TEXTS = ("-2147483649", "-2147483648", "-1", "-0", "0", "2147483647", "2147483648", "9999999999", "-9999999999")
SIGNED_TEXTS = (*DECIMAL_TEXTS, "+0", "+2147483647", "+2147483648")
UNSIGNED_VALUES = (-1, 0, 1, 2 ** 31, 2 ** 32 - 1, 2 ** 32, True, 1.0, None)
NATIVE_FLOORS = (INT32_MIN, 0, 1)
RESOURCE_ENTRY = ResourceEntry("Limit.aar", "a" * 40, "b" * 32, 0, 0)
SESSION_PAIR = WireProfile("session", (), 0, 0, None, response_kind="integer_pair")
SQUAD_TEAMS = (0, 1, 127, 128, 254, 255)
SQUAD_SLOTS = (0, 1, 14, 15)
ITEM, ITEM_HASH = GIFTS[0][:2]
SKIN_ITEMS = (("get_skin_paid", identifier("get_skin_paid"), 1), ("other_item", identifier("other_item"), 10))
SKIN_PACKAGE_CELLS = ("ConfigId", "FreeCondition", "Aggregate")
# Aggregate 0 keeps FreeCondition out of the counted-package rule, and the unlock item verifies once Aggregate is set.
UNCOUNTED_PACKAGE = ("paid", 0, "get_skin_paid", False, 0)
COUNTED_PACKAGE = ("paid", 0, "get_skin_paid", False, 1)
EMPTY_POSSESSIONS = {"bootstrap_schema": 1, "tables": {"possessions": []}}
INVALID_CONFIGURATION = "invalid_configuration_state"
SOURCE = Path(native_numbers.__file__).resolve().parent
SERVER_MODULES = tuple(sorted(SOURCE.glob("*.py")))
SPELLED_BOUNDS = frozenset((INT32_MAX, -INT32_MIN, UINT32_MASK, UINT32_MASK + 1))
SPELLED_POWERS = frozenset(((ast.Pow, 2), (ast.LShift, 1)))
SPELLED_EXPONENTS = frozenset((31, 32))
BOUND_MODULES = frozenset(Path(module.__file__).name
                          for module in (native_numbers, system_literals_module, al_format_module))


def fits_int32(value) -> bool:
    return type(value) is int and -2 ** 31 <= value < 2 ** 31


def spells_bound(node: ast.AST, widths: frozenset[str] = frozenset()) -> bool:
    if isinstance(node, ast.Constant):
        return type(node.value) is int and node.value in SPELLED_BOUNDS
    return (isinstance(node, ast.BinOp) and isinstance(node.left, ast.Constant)
            and (type(node.op), node.left.value) in SPELLED_POWERS
            and (isinstance(node.right, ast.Constant) and node.right.value in SPELLED_EXPONENTS
                 or isinstance(node.right, ast.Name) and node.right.id in widths))


def assigned_names(target: ast.expr, value: ast.expr):
    if isinstance(target, ast.Name):
        yield target.id, value
    elif (isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List))
          and len(target.elts) == len(value.elts)):
        for element, item in zip(target.elts, value.elts):
            yield from assigned_names(element, item)


def module_assignments(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield from assigned_names(target, node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            yield from assigned_names(node.target, node.value)


def spelled_widths(tree: ast.Module) -> frozenset[str]:
    return frozenset(name for name, value in module_assignments(tree) if isinstance(value, ast.Constant)
                     and type(value.value) is int and value.value in SPELLED_EXPONENTS)


def spelled_bound_lines(source: str | bytes, filename: str = "<snippet>") -> list[int]:
    tree = ast.parse(source, filename)
    widths = spelled_widths(tree)
    return sorted({node.lineno for node in ast.walk(tree) if spells_bound(node, widths)})


def admitted(action, code: str) -> bool:
    try:
        action()
    except (ProtocolError, ALFormatError, ResourceFetchError) as error:
        return error.code != code
    return True


def outcome(action):
    try:
        return action()
    except ValueError:
        return ValueError


def override_decoded_cell(monkeypatch, module, field, value):
    decode = module.parse_table
    def altered(data, limits):
        table = decode(data, limits)
        table["rows"][0][field] = value
        return table
    monkeypatch.setattr(module, "parse_table", altered)


def configuration_state(**changes):
    row = {name: False if CONFIGURATION_FIELDS[name][1] is bool else 0 for name in CONFIGURATION_REQUIRED_FIELDS}
    return {"bootstrap_schema": 1, "tables": {"config": [{**row, **changes}]}}


def filter_state(**changes):
    return {"bootstrap_schema": 1, "filter_settings_schema": 1,
            "tables": {"filter_settings": [{"FilterName": "", "FilterNameHash": 1, "SaveValue": "", **changes}]}}


def flag_state(*rows):
    return {"bootstrap_schema": 1, "universal_flags_schema": 1, "tables": {"universal_flags": list(rows)}}


def possession_state(*rows):
    return {"bootstrap_schema": 1, "tables": {"possessions": list(rows)}}


@pytest.fixture
def possessions(tmp_path):
    raw = item_table()
    return PossessionService(tmp_path, asset_reader=lambda _: raw)


@pytest.fixture
def skin_items(tmp_path):
    raw = item_table(SKIN_ITEMS)
    return PossessionService(tmp_path, asset_reader=lambda _: raw)


def test_int32_limits_are_the_signed_and_unsigned_32_bit_bounds():
    assert (INT32_MIN, INT32_MAX, UINT32_MASK) == (-2 ** 31, 2 ** 31 - 1, 2 ** 32 - 1)


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_int32_admits_only_plain_integers_within_the_signed_range(value):
    assert int32(value) is fits_int32(value)


@pytest.mark.parametrize("value,folded", [
    (0, 0), (-1, -1), (INT32_MAX, INT32_MAX), (INT32_MIN, INT32_MIN), (0x40000000 | 0x80000000, -0x40000000),
    (2 ** 32 - 1, -1), (2 ** 31, INT32_MIN), (2 ** 32, 0), (INT32_MIN - 1, INT32_MAX),
])
def test_signed32_folds_a_32_bit_pattern_into_its_signed_value(value, folded):
    assert signed32(value) == folded
    assert signed32(value) & UINT32_MASK == value & UINT32_MASK


def test_float32_rounds_through_single_precision():
    assert float32(0.1) == 0.10000000149011612 and float32(2 ** 24 + 1) == 2 ** 24
    assert type(float32(3)) is float and float32(1.5) == 1.5 and float32(float("inf")) == float("inf")
    with pytest.raises(OverflowError):
        float32(1e39)


def test_player_state_keeps_exporting_the_shared_primitives():
    assert player_state.int32 is int32


def test_native_numbers_imports_only_the_standard_library():
    # config_state and request_fields load while player_state imports, so the primitives may never import them back.
    tree = ast.parse(Path(native_numbers.__file__).read_bytes())
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {"." * node.level + (node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported and all(name.partition(".")[0] in sys.stdlib_module_names for name in imported)


@pytest.mark.parametrize("source", [
    "limit = 0x7FFFFFFF", "low = -0x80000000", "mask = 0xFFFFFFFF", "span = 0x100000000", "limit = 2147483647",
    "fits = -2 ** 31 <= value < 2 ** 31", "fits = 0 <= value < 2 ** 32", "sign = 1 << 31", "span = 1 << 32",
])
def test_the_bound_guard_finds_spelled_32_bit_bounds(source):
    assert spelled_bound_lines(source) == [1]


@pytest.mark.parametrize("source", [
    "fits = int32(value) and value >= 0", "fits = 0 <= value <= UINT32_MASK", "byte = value & 0xFF",
    "limit = 0xFFFFFFFFFFFFFFFF", "fits = 0 <= value < 2 ** 63", "low = 1 << (bits - 1)", "raw = high << 32 | low",
    "text = '0x7FFFFFFF'", "bits = 8\nmask = (1 << bits) - 1", "count, bits = 32, 8\nmask = (1 << bits) - 1",
])
def test_the_bound_guard_allows_named_bounds_and_other_widths(source):
    assert spelled_bound_lines(source) == []


@pytest.mark.parametrize("source", [
    "bits = 32\nmask = (1 << bits) - 1", "BITS = 31\nlow = -2 ** BITS", "TYPE, BITS = 7, 32\nmask = (1 << BITS) - 1",
    "bits: int = 32\nspan = 2 ** bits",
])
def test_the_bound_guard_follows_module_level_width_names(source):
    assert spelled_bound_lines(source) == [2]


# system_literals keeps the CRC sign fold its signed-CRC guard looks for; al_format tests a flag bit, not a range.
def test_only_the_native_primitives_spell_32_bit_bounds():
    assert BOUND_MODULES <= {path.name for path in SERVER_MODULES}
    spelled = {path.name: lines for path in SERVER_MODULES if path.name not in BOUND_MODULES
               and (lines := spelled_bound_lines(path.read_bytes(), str(path)))}
    assert spelled == {}


def test_squad_addresses_fold_every_native_group_to_a_signed_int32():
    for group in range(RESERVED_SQUAD_GROUP + 1):
        for team in SQUAD_TEAMS:
            for slot in SQUAD_SLOTS:
                packed = (group << SQUAD_GROUP_SHIFT) | (team << SQUAD_TEAM_SHIFT) | slot
                assert squad_address(group, team, slot) == (packed - 2 ** 32 if packed >= 2 ** 31 else packed)


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_resource_index_flags_admit_int32_values(value):
    assert admitted(lambda: replace(RESOURCE_ENTRY, flags=value), "invalid_resource_flags") == fits_int32(value)


@pytest.mark.parametrize("value", UNSIGNED_VALUES)
def test_resource_sizes_and_download_limits_span_the_unsigned_32_bit_range(tmp_path, value):
    fits = type(value) is int and 0 <= value < 2 ** 32
    assert admitted(lambda: replace(RESOURCE_ENTRY, size=value), "invalid_resource_size") == fits
    assert admitted(lambda: ResourceFetcher(tmp_path, max_file_bytes=value, opener=object()),
                    "invalid_file_limit") == (fits and value > 0)


@pytest.mark.parametrize("text", DECIMAL_TEXTS)
def test_session_flag_pairs_admit_int32_halves(text):
    for pair in (f"{text},0", f"0,{text}"):
        assert admitted(lambda: encode_response(SESSION_PAIR, pair.encode()), "invalid_response") == fits_int32(int(text))


@pytest.mark.parametrize("minimum", NATIVE_FLOORS)
@pytest.mark.parametrize("value", BOUNDARY_VALUES)
@pytest.mark.parametrize("integer", [
    pytest.param(lambda value, minimum: quest_catalog.integer(value, minimum=minimum), id="quest_catalog"),
    pytest.param(mission_catalog._integer, id="mission_catalog"),
    pytest.param(unit_detail._integer, id="unit_detail"),
    pytest.param(unit_initialization._integer, id="unit_initialization"),
    pytest.param(unit_sale_catalog._integer, id="unit_sale_catalog"),
    pytest.param(event_pass_catalog._integer, id="event_pass_catalog"),
    pytest.param(event_pass_revival._integer, id="event_pass_revival"),
    pytest.param(gift_catalog._integer, id="gift_catalog"),
])
def test_resource_integers_admit_int32_values_from_their_minimum(integer, value, minimum):
    expected = value if fits_int32(value) and value >= minimum else ValueError
    assert outcome(lambda: integer(value, minimum)) == expected


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
@pytest.mark.parametrize("check,floor", [
    pytest.param(crystal_wallet._nonnegative_int32, 0, id="crystal_wallet"),
    pytest.param(wallet_money._nonnegative_int32, 0, id="wallet_money"),
    pytest.param(tower_state._native, 0, id="tower_state"),
    pytest.param(lambda value: tower_state._native(value, INT32_MIN), INT32_MIN, id="tower_state_signed"),
    pytest.param(image_aliases._card_id, 1, id="image_aliases"),
    pytest.param(letstd_state._positive, 1, id="letstd_state"),
    pytest.param(daily_gift_state._positive, 1, id="daily_gift_state"),
    pytest.param(mission_pass_points._integer, 0, id="mission_pass_points"),
    pytest.param(present_backlog._positive_int32, 1, id="present_backlog"),
])
def test_stored_integers_admit_int32_values_from_their_floor(check, floor, value):
    assert check(value) is (fits_int32(value) and value >= floor)


@pytest.mark.parametrize("minimum", NATIVE_FLOORS)
@pytest.mark.parametrize("value", BOUNDARY_VALUES)
@pytest.mark.parametrize("check", [
    pytest.param(comeback_login_catalog._integer, id="comeback_login_catalog"),
    pytest.param(comeback_login_state._integer, id="comeback_login_state"),
    pytest.param(event_login_state._integer, id="event_login_state"),
    pytest.param(event_pass_state._integer, id="event_pass_state"),
    pytest.param(gift_service._integer, id="gift_service"),
    pytest.param(mission_ep_bindings._integer, id="mission_ep_bindings"),
    pytest.param(package_catalog._integer, id="package_catalog"),
    pytest.param(present_items._integer, id="present_items"),
    pytest.param(present_recovery._integer, id="present_recovery"),
    pytest.param(lambda value, minimum: presents._integer(value, minimum=minimum), id="presents"),
    pytest.param(purchased_login_state._integer, id="purchased_login_state"),
    pytest.param(rank_reward_state._integer, id="rank_reward_state"),
])
def test_stored_integers_admit_int32_values_from_their_minimum(check, value, minimum):
    assert check(value, minimum) is (fits_int32(value) and value >= minimum)


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
@pytest.mark.parametrize("build,floor", [
    pytest.param(lambda value: comeback_login_rules.ComebackLoginRules(min_absence_days=value), 1,
                 id="comeback_absence"),
    pytest.param(lambda value: purchased_login_rules.PurchasedLoginRules(purchase_limit=value), 1, id="purchase_limit"),
    pytest.param(lambda value: purchased_login_rules.PurchasedLoginRules(campaign_ids=(value,)), 1,
                 id="purchased_campaigns"),
    pytest.param(lambda value: event_pass_rules.EventPassAutomationRules(closed_statuses=(value,)), 2,
                 id="event_pass_statuses"),
    pytest.param(daily_gift_rules._identifier, 1, id="daily_gift_rules"),
    pytest.param(event_login_rules._identifier, 1, id="event_login_rules"),
])
def test_rule_integers_admit_int32_values_from_their_floor(build, floor, value):
    assert (outcome(lambda: build(value)) is not ValueError) is (fits_int32(value) and value >= floor)


@pytest.mark.parametrize("identify,base", [
    pytest.param(purchased_login_texts.purchased_package_id, purchased_login_texts.PACKAGE_ID_BASE, id="purchased"),
    pytest.param(package_text_overlay.local_package_id, package_text_overlay.LOCAL_PACKAGE_ID_BASE, id="event_pass"),
])
def test_package_identities_stop_at_the_int32_ceiling(identify, base):
    top = INT32_MAX - base
    assert identify(top) == INT32_MAX and outcome(lambda: identify(top + 1)) is ValueError


def test_local_package_confirmation_keys_stop_at_the_int32_ceiling():
    assert package_text_overlay.confirmation_key(INT32_MAX).endswith(str(INT32_MAX))
    assert outcome(lambda: package_text_overlay.confirmation_key(INT32_MAX + 1)) is ValueError


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_card_status_rows_patches_and_selections_admit_int32_values(value):
    service = CardStatusService(card_snapshot())
    assert admitted(lambda: service.read(card_state(card_row(4, A1=value))), "card_status_state_invalid") == fits_int32(value)
    assert admitted(lambda: service.update(card_state(), [{"CardID": 4, "A3": value}]),
                    "card_status_patch_invalid") == fits_int32(value)
    assert admitted(lambda: service.response(card_state(card_row(4)), [value]),
                    "card_status_selection_invalid") == fits_int32(value)


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_configuration_fields_keep_their_declared_type_and_int32_range(value):
    assert admitted(lambda: patch_configuration(configuration_state(), {"MenuTutorialFlags": value}),
                    INVALID_CONFIGURATION) == fits_int32(value)
    assert admitted(lambda: configuration_row(configuration_state(_GuideUnitIndex=value)), INVALID_CONFIGURATION) == fits_int32(value)
    assert admitted(lambda: configuration_delta({"HaveUnitMaxSizeBuy": value}, ("HaveUnitMaxSizeBuy",)),
                    INVALID_CONFIGURATION) == fits_int32(value)
    assert admitted(lambda: patch_configuration(configuration_state(), {"UseBgm": value}),
                    INVALID_CONFIGURATION) == (type(value) is bool)


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_stored_filter_hashes_admit_int32_values(value):
    assert admitted(lambda: read_filters(filter_state(FilterNameHash=value)), "filter_state_invalid") == fits_int32(value)


@pytest.mark.parametrize("text", DECIMAL_TEXTS)
def test_filter_selections_admit_int32_decimal_identifiers(text):
    assert admitted(lambda: read_filters(filter_state(SaveValue=text)), "filter_state_invalid") == fits_int32(int(text))


@pytest.mark.parametrize("text", SIGNED_TEXTS)
def test_request_integer_values_admit_int32_decimal_text(text):
    node = ET.fromstring(f'<F T="I"><V>{text}</V></F>')
    assert admitted(lambda: values(node, "I"), "invalid_request_fields") == fits_int32(int(text))


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_appended_integer_cells_admit_int32_values(value):
    assert admitted(lambda: append_table_rows(table_source(), [{"ID": value, "Text": "x"}]),
                    "table_append_invalid") == fits_int32(value)


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_skin_card_identities_admit_int32_values(skin_items, value):
    assert admitted(lambda: catalog(skin_items, cards=Cards({value: card(value)})),
                    "unit_skin_catalog_invalid") == (fits_int32(value) and value > 0)
    # True and 1.0 equal the card identity 1, so only the int32 type rule turns them away.
    assert admitted(lambda: catalog(skin_items, cards=Cards({1: {**card(1), "CardID": value}})),
                    "unit_skin_catalog_invalid") == (fits_int32(value) and value == 1)
    skins = catalog(skin_items, cards=Cards({1: card(1, "free"), INT32_MAX: card(INT32_MAX)}))
    assert admitted(lambda: skins.allowed_displays(value, EMPTY_POSSESSIONS, smartphone_images=False),
                    "unit_skin_card_unknown") == (fits_int32(value) and value in (1, INT32_MAX))


@pytest.mark.parametrize("field", SKIN_PACKAGE_CELLS)
@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_decoded_skin_package_cells_admit_int32_values(skin_items, monkeypatch, field, value):
    override_decoded_cell(monkeypatch, unit_skin_module, field, value)
    assert admitted(lambda: catalog(skin_items, [UNCOUNTED_PACKAGE], Cards({1: card(1)})),
                    "unit_skin_catalog_invalid") == fits_int32(value)


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_counted_skin_unlock_stacks_admit_int32_values(skin_items, monkeypatch, value):
    monkeypatch.setattr(skin_items, "stack", lambda item_id, *, stored=False: value)
    assert admitted(lambda: catalog(skin_items, [COUNTED_PACKAGE], Cards({1: card(1)})),
                    "unit_skin_catalog_invalid") == (fits_int32(value) and value > 0)


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_universal_flag_values_admit_int32_values(value):
    assert admitted(lambda: set_flags(flag_state(), {"Name": value}), "universal_flag_updates_invalid") == fits_int32(value)
    assert admitted(lambda: read_flags(flag_state({"ID": "Name", "Value": value})), "universal_flags_invalid") == fits_int32(value)


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_possession_quantities_admit_int32_values(possessions, value):
    positive = fits_int32(value) and value > 0
    assert admitted(lambda: possessions.credit(possession_state(), {ITEM: value}), "possession_grant_invalid") == positive
    assert admitted(lambda: possessions.credit_capped(possession_state(), {ITEM: value}), "possession_grant_invalid") == positive
    assert admitted(lambda: possessions.debit(possession_state({"ItemID": ITEM, "Count": 5}), {ITEM: value}),
                    "possession_cost_invalid") == positive
    assert admitted(lambda: possessions.overflow_response({ITEM: value}), "possession_overflow_invalid") == positive
    assert admitted(lambda: possessions.counts(possession_state({"ItemID": ITEM, "Count": value})),
                    "possession_state_invalid") == (fits_int32(value) and value >= 0)


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
def test_decoded_item_stacks_admit_int32_values(monkeypatch, tmp_path, value):
    override_decoded_cell(monkeypatch, possessions_module, "Stack", value)
    raw = item_table()
    assert PossessionService(tmp_path, asset_reader=lambda _: raw).is_ready() == (fits_int32(value) and value >= 0)


# A float equal to the hash finds the same dict entry, so only the int32 type rule turns it away.
@pytest.mark.parametrize("value", [ITEM_HASH, float(ITEM_HASH)])
def test_item_hashes_resolve_and_load_only_as_int32_values(possessions, monkeypatch, tmp_path, value):
    assert admitted(lambda: possessions.resolve_hash(value), "possession_item_unknown") == fits_int32(value)
    override_decoded_cell(monkeypatch, possessions_module, "ID", value)
    raw = item_table()
    assert PossessionService(tmp_path, asset_reader=lambda _: raw).is_ready() == fits_int32(value)
