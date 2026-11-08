from copy import deepcopy
from dataclasses import FrozenInstanceError
from xml.etree import ElementTree as ET

import pytest

from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.errors import ProtocolError
from aigis_ps.protocol import ProtocolRegistry, encode_response, encode_wire, prepare_request
from aigis_ps.quest_protocol import (
    MAX_CANDIDATES, MAX_KILLS, MAX_TEAM_ROWS, QuestCandidate, QuestExit, QuestFinish, QuestStart, SallyRecord,
    TeamMember, parse_exit, parse_finish, parse_start, validate_damage_display,
)


def common(client_time=1793491200):
    return tuple(Column(name, kind, (value,)) for name, kind, value in (
        ("UI", "S", "1"), ("UK", "S", "local-account"), ("MK", "S", "local-method"),
        ("CT", "I", client_time), ("PFN", "S", "dmm-r18"), ("PlatformCode", "I", 1),
        ("GPVI", "S", "unkown"), ("GPOT", "S", "unkown")))


def candidates(rows=((0, 1, 0), (1, 2, 1), (2, 3, 3))):
    return Container("RT", tuple(Column(name, "I", tuple(row[index] for row in rows))
                                 for index, name in enumerate(("ID", "TYPE", "ATTR"))))


def result(quest_id=1002, stars=3, kills=40, obtained=2, selected=7, gold=90):
    return Container("RT", (
        Container("LG", (Column("TG", "S", ("finishQuest",)), Column("TT", "S", (f"{quest_id} {stars} {kills}",)))),
        Container("DropResult", (Column("DP", "I", (obtained,)), Column("DPLIST", "I", (selected,)),
                                 Column("GoldGet", "I", (gold,)))),
    ))


def envelope(number=81, *, extra=(), client_time=1793491200, rt=None):
    registry = ProtocolRegistry.load()
    profile = registry.by_registration[number]
    if number in (77, 78, 81, 82):
        body = (Column("QI", "I", (1002,)), Column("Rev", "S", ("resource-revision",)))
        if rt is not None:
            body += (rt,)
    else:
        body = (result() if rt is None else rt,)
    xml = prepare_request(dumps(Container(children=common(client_time) + body + extra)), profile.st)
    return registry.decode(profile.route, encode_wire(xml, profile.request_key))


def exit_envelope(text="1002,0"):
    xml = dumps(Container(children=(Column("UI", "S", ("1",)), Column("UK", "S", ("local-account",)),
                                    Column("TG", "S", ("finishQuest",)), Column("TT", "S", (text,)))))
    return ProtocolRegistry.load().decode("ntwh57oy", xml)


def node(entry):
    return ET.fromstring(dumps(Container(children=(entry,))))[0]


def assert_rejected(parser, request):
    before = ET.tostring(request.document)
    with pytest.raises(ProtocolError) as error:
        parser(request)
    assert error.value.code == "quest_request_invalid" and error.value.status == 400
    assert ET.tostring(request.document) == before


@pytest.mark.parametrize("number", [77, 78, 81, 82])
def test_start_registered_profiles_preserve_native_fields(number):
    request = envelope(number)
    before = ET.tostring(request.document)
    parsed = parse_start(request)
    assert parsed == QuestStart(1002, "resource-revision", 1793491200, ())
    assert ET.tostring(request.document) == before
    with pytest.raises(FrozenInstanceError):
        parsed.quest_id = 9


@pytest.mark.parametrize("number", [77, 81])
def test_start_candidates_are_ordered_immutable_treasure_slots(number):
    parsed = parse_start(envelope(number, rt=candidates()))
    assert parsed.candidates == (QuestCandidate(0, 1, 0), QuestCandidate(1, 2, 1), QuestCandidate(2, 3, 3))
    with pytest.raises(FrozenInstanceError):
        parsed.candidates[0].slot = 2


@pytest.mark.parametrize("number", [78, 82])
def test_special_start_profiles_do_not_invent_ordinary_candidates(number):
    assert_rejected(parse_start, envelope(number, rt=candidates()))


@pytest.mark.parametrize("rt", [Container("RT"), candidates(())])
def test_empty_start_candidate_container_is_unambiguous(rt):
    assert parse_start(envelope(rt=rt)).candidates == ()


@pytest.mark.parametrize("number", [80, 83])
def test_finish_reads_string_log_and_distinct_obtained_selected_masks(number):
    request = envelope(number, rt=result(obtained=10, selected=11))
    before = ET.tostring(request.document)
    assert parse_finish(request) == QuestFinish(1002, 3, 40, 1793491200, 10, 11, 90)
    assert ET.tostring(request.document) == before


@pytest.mark.parametrize("obtained,selected,expected", [
    (-2147483648, -1, (0x80000000, 0xFFFFFFFF)), (0, 2147483647, (0, 0x7FFFFFFF)),
])
def test_native_signed_masks_are_exposed_as_uint32(obtained, selected, expected):
    parsed = parse_finish(envelope(83, rt=result(obtained=obtained, selected=selected)))
    assert (parsed.obtained_mask, parsed.selected_mask) == expected


@pytest.mark.parametrize("client_time", [-2147483648, -1, 0, 2147483647])
def test_client_time_keeps_time64_low_signed_int32(client_time):
    assert parse_start(envelope(client_time=client_time)).client_time == client_time
    assert parse_finish(envelope(83, client_time=client_time)).client_time == client_time


@pytest.mark.parametrize("request_factory,parser", [(envelope, parse_start), (lambda: envelope(83), parse_finish)])
@pytest.mark.parametrize("name", ["UI", "UK", "MK", "CT", "PFN", "PlatformCode", "GPVI", "GPOT", "VN", "ST", "HS"])
def test_registered_header_fields_are_required_and_unique(request_factory, parser, name):
    missing = request_factory()
    field = missing.document.find(name)
    missing.document.remove(field)
    assert_rejected(parser, missing)
    repeated = request_factory()
    repeated.document.append(deepcopy(repeated.document.find(name)))
    assert_rejected(parser, repeated)


@pytest.mark.parametrize("name,kind", [("UI", "I"), ("UK", "B"), ("MK", "I"), ("CT", "S"),
                                      ("PFN", "I"), ("PlatformCode", "B"), ("GPVI", "I"), ("GPOT", "I")])
def test_registered_header_types_are_not_coerced(name, kind):
    request = envelope()
    request.document.find(name).set("T", kind)
    assert_rejected(parse_start, request)


@pytest.mark.parametrize("name", ["VN", "ST", "HS"])
def test_transport_metadata_stays_untyped_scalar(name):
    request = envelope()
    request.document.find(name).set("T", "S")
    assert_rejected(parse_start, request)


@pytest.mark.parametrize("path", [".", "RT", "RT/ID", "QI", "QI/V"])
def test_unknown_attributes_and_mixed_text_are_rejected(path):
    request = envelope(rt=candidates())
    request.document.find(path).set("N", "other")
    assert_rejected(parse_start, request)
    request = envelope(rt=candidates())
    request.document.find(path).tail = "unexpected"
    assert_rejected(parse_start, request)


@pytest.mark.parametrize("path", ["QI", "RT/ID"])
def test_values_require_v_children_without_nested_nodes(path):
    request = envelope(rt=candidates())
    request.document.find(f"{path}/V").tag = "Item"
    assert_rejected(parse_start, request)
    request = envelope(rt=candidates())
    ET.SubElement(request.document.find(f"{path}/V"), "Nested")
    assert_rejected(parse_start, request)


@pytest.mark.parametrize("rows", [((1, 1, 0),), ((0, 0, 0),), ((0, 256, 0),), ((0, 1, -1),),
                                   ((0, 1, 4),), ((0, 1, 0), (0, 2, 0))])
def test_start_candidate_identity_slot_and_attribute_bounds(rows):
    assert_rejected(parse_start, envelope(rt=candidates(rows)))


def test_start_candidates_keep_native_slots_beyond_the_treasure_columns():
    parsed = parse_start(envelope(rt=candidates(((0, 4, 0), (1, 8, 1), (2, 41, 0), (3, 255, 0)))))
    assert [candidate.slot for candidate in parsed.candidates] == [4, 8, 41, 255]


def test_start_candidate_mask_capacity_and_column_lengths():
    rows = tuple((index, 1, 0) for index in range(MAX_CANDIDATES))
    assert len(parse_start(envelope(rt=candidates(rows))).candidates) == 32
    assert_rejected(parse_start, envelope(rt=candidates(rows + ((32, 1, 0),))))
    request = envelope(rt=candidates())
    request.document.find("RT/TYPE").remove(request.document.find("RT/TYPE/V"))
    assert_rejected(parse_start, request)


def test_boost_is_preserved_without_applying_rewards_or_changing_units():
    parsed = parse_start(envelope(extra=(Container("Boost", (Column("V", "I", (3,)),)),)))
    assert parsed.boost == 3
    for value in (0, -1):
        assert_rejected(parse_start, envelope(extra=(Container("Boost", (Column("V", "I", (value,)),)),)))


def team(name="BL", count=2):
    return Container(name, tuple(Column(field, "I", tuple(-1 if field == "A8" else index for index in range(count)))
                                 for field in ("A1", "A7", "A8", "AB", "AA")))


@pytest.mark.parametrize("number,parser", [(81, parse_start), (83, parse_finish)])
def test_all_native_team_containers_are_checked_without_assuming_ownership(number, parser):
    guest = Container("GG", (Column("I", "I", (2,)),) + tuple(Column(field, "I", (-1, 2147483647))
                                                              for field in ("TP", "A1", "A2", "A7", "AB")))
    request = envelope(number, extra=tuple(team(name) for name in ("BL", "GL", "HL", "AL")) + (guest,))
    before = ET.tostring(request.document)
    parser(request)
    assert ET.tostring(request.document) == before
    parser(envelope(number, extra=(Container("BL"), Container("GG", (Column("I", "I", (1,)),)))))


@pytest.mark.parametrize("number,parser", [(81, parse_start), (83, parse_finish)])
def test_battle_lists_keep_their_rows_and_temporary_units_stay_unreported(number, parser):
    parsed = parser(envelope(number, extra=(team("BL", 2), team("GL", 1), team("HL", 1), team("AL", 3))))
    assert parsed.members == (TeamMember(0, 0, -1, 0, 0), TeamMember(1, 1, -1, 1, 1))
    assert len(parsed.visitors) == 3 and len(parsed.fixed) == 1
    assert parser(envelope(number)).members == ()


@pytest.mark.parametrize("name", ["BL", "GL", "HL", "AL", "GG"])
def test_partial_or_unknown_team_columns_are_rejected(name):
    partial = Container(name, (Column("I" if name == "GG" else "A1", "I", (1,)), Column("A7", "I", (1,))))
    assert_rejected(parse_start, envelope(extra=(partial,)))
    unknown = Container(name, (Column("Unknown", "I", (1,)),))
    assert_rejected(parse_start, envelope(extra=(unknown,)))


SALLY_INTS = ("InviteIndex", "PlainCost", "SallyCost", "PropCount")
SALLY_BOOLS = ("TokenUnit", "GuestUnit", "HoldUnit", "AvatarUnit")


def sally_record(index, values, flags):
    columns = dict(zip(SALLY_INTS, values))
    fields = tuple(Column(name, "I", (value,)) for name, value in columns.items())
    fields += tuple(Column(name, "B", (flag,)) for name, flag in zip(SALLY_BOOLS, flags))
    if columns["PropCount"]:
        fields += tuple(Column(name, "I", tuple(range(columns["PropCount"]))) for name in ("Mul", "Add", "MinMaxFlag"))
    return Container(f"Sally{index:03d}", fields)


def sally_container(*records):
    return Container("SallyContainer", (Column("SallyCount", "I", (len(records),)),
                                         *(sally_record(index, *record) for index, record in enumerate(records))))


def sally(count=1, properties=2):
    return sally_container(*(((index, index, index, properties), tuple(name == "GuestUnit" for name in SALLY_BOOLS))
                             for index in range(count)))


@pytest.mark.parametrize("number,parser", [(81, parse_start), (83, parse_finish)])
@pytest.mark.parametrize("properties", [0, 2])
def test_sally_optional_record_structure_and_property_arrays(number, parser, properties):
    assert parser(envelope(number, extra=(sally(2, properties),))).quest_id == 1002


ONE_HOT_SALLIES = (((-1, 12, 9, 0), (True, False, False, False)), ((0, 15, 11, 2), (False, True, False, False)),
                   ((1, 18, 14, 0), (False, False, True, False)), ((2, 21, 17, 1), (False, False, False, True)))


def test_finish_keeps_each_sally_record_with_its_costs_and_unit_flags():
    parsed = parse_finish(envelope(83, extra=(sally_container(*ONE_HOT_SALLIES),)))
    assert parsed.sallies == tuple(SallyRecord(*values, *flags) for values, flags in ONE_HOT_SALLIES)
    assert [(record.invite_index, record.plain_cost, record.sally_cost, record.prop_count)
            for record in parsed.sallies] == [values for values, _ in ONE_HOT_SALLIES]
    assert [record.token_unit for record in parsed.sallies] == [True, False, False, False]
    assert [record.guest_unit for record in parsed.sallies] == [False, True, False, False]
    assert [record.hold_unit for record in parsed.sallies] == [False, False, True, False]
    assert [record.avatar_unit for record in parsed.sallies] == [False, False, False, True]
    with pytest.raises(FrozenInstanceError):
        parsed.sallies[0].sally_cost = 0
    assert parse_finish(envelope(83)).sallies == ()
    assert QuestFinish(1002, 3, 40, 1793491200, 10, 11, 90).sallies == ()


@pytest.mark.parametrize("fault", ["count", "gap", "bool", "prop_count", "partial", "unknown"])
def test_sally_count_types_and_complete_arrays_are_required(fault):
    request = envelope(83, extra=(sally(),))
    container = request.document.find("SallyContainer")
    record = container.find("Sally000")
    if fault == "count":
        container.find("SallyCount/V").text = "2"
    elif fault == "gap":
        record.tag = "Sally001"
    elif fault == "bool":
        record.find("TokenUnit").set("T", "I")
    elif fault == "prop_count":
        record.find("PropCount/V").text = "1"
    elif fault == "partial":
        record.remove(record.find("Mul"))
    else:
        record.append(node(Column("Extra", "I", (1,))))
    assert_rejected(parse_finish, request)


def test_auxiliary_row_limits_apply_before_accepting_payload():
    assert_rejected(parse_start, envelope(extra=(team(count=MAX_TEAM_ROWS + 1),)))
    request = envelope(83, extra=(sally(),))
    request.document.find("SallyContainer/SallyCount/V").text = str(MAX_TEAM_ROWS + 1)
    assert_rejected(parse_finish, request)


@pytest.mark.parametrize("extra,expected", [((), None), ((Container("GC"),), None),
    ((Container("GC", (Column("F7", "I", (0,)),)),), 0), ((Container("GC", (Column("F7", "I", (1,)),)),), 1)])
def test_damage_display_is_optional_configuration_not_victory(extra, expected):
    assert parse_finish(envelope(83, extra=extra)).damage_display == expected


@pytest.mark.parametrize("value", [2, -1])
def test_finish_rejects_damage_display_values_outside_the_native_switch(value):
    assert_rejected(parse_finish, envelope(83, extra=(Container("GC", (Column("F7", "I", (value,)),)),)))


def test_validate_damage_display_shares_the_settlement_boundaries():
    assert [validate_damage_display(value) for value in (0, 1)] == [0, 1]
    for value in (2, -1, True, False, None, "1", 1.0):
        with pytest.raises(ProtocolError) as error:
            validate_damage_display(value)
        assert (error.value.code, error.value.status) == ("quest_request_invalid", 400)


def test_dead_unit_list_only_validates_its_native_integer_column():
    assert parse_finish(envelope(83, extra=(Container("DeadUnitList", (Column("UnitId", "I", (1, 5)),)),))).quest_id == 1002
    assert_rejected(parse_finish, envelope(83, extra=(Container("DeadUnitList", (Column("CardID", "I", (1,)),)),)))


@pytest.mark.parametrize("text", ["1002,3,40", "1002  3 40", "1002\t3\t40", "1002 3", "1002 3 40 1",
                                  "0 3 40", f"1002 {MAX_KILLS + 1} 40", "1002 -1 40", "1002 3 -1", "2147483648 3 40",
                                  f"1002 3 {MAX_KILLS + 1}"])
def test_finish_log_format_and_result_bounds(text):
    request = envelope(83)
    request.document.find("RT/LG/TT/V").text = text
    assert_rejected(parse_finish, request)


@pytest.mark.parametrize("second", [0, 5, MAX_KILLS])
def test_finish_log_second_value_reaches_score_mode_kill_counts(second):
    request = envelope(83)
    request.document.find("RT/LG/TT/V").text = f"1002 {second} {second}"
    parsed = parse_finish(request)
    assert (parsed.stars, parsed.kills) == (second, second)


def test_finish_parser_accepts_the_four_star_result_and_gold_event_count():
    request = envelope(83, extra=(Container("GG", (Column("I", "I", (2,)),)),))
    request.document.find("RT/LG/TT/V").text = "1002 4 40"
    parsed = parse_finish(request)
    assert (parsed.stars, parsed.gold_events) == (4, 2)
    assert parse_finish(envelope(83)).gold_events == 0
    request.document.find("RT/LG/TT/V").text = f"1002 4 {MAX_KILLS}"
    assert parse_finish(request).kills == MAX_KILLS


@pytest.mark.parametrize("path", ["RT/LG/TG", "RT/LG/TT", "RT/DropResult/DP", "RT/DropResult/DPLIST", "RT/DropResult/GoldGet"])
def test_finish_required_columns_cannot_be_missing_or_repeated(path):
    request = envelope(83)
    parent_path, name = path.rsplit("/", 1)
    parent = request.document.find(parent_path)
    parent.remove(parent.find(name))
    assert_rejected(parse_finish, request)
    request = envelope(83)
    request.document.find(parent_path).append(deepcopy(request.document.find(path)))
    assert_rejected(parse_finish, request)


@pytest.mark.parametrize("path,value", [("CT/V", "2147483648"), ("QI/V", "0"), ("QI/V", "-1"), ("QI/V", "1.0"),
                                       ("QI/V", "1" * 1000)])
def test_start_numeric_boundaries(path, value):
    request = envelope()
    request.document.find(path).text = value
    assert_rejected(parse_start, request)


@pytest.mark.parametrize("path,value", [("RT/DropResult/DP/V", "4294967295"), ("RT/DropResult/DPLIST/V", "-2147483649"),
                                       ("RT/DropResult/GoldGet/V", "-1"), ("RT/LG/TG/V", "other")])
def test_finish_numeric_boundaries_and_log_category(path, value):
    request = envelope(83)
    request.document.find(path).text = value
    assert_rejected(parse_finish, request)


@pytest.mark.parametrize("factory,parser", [(envelope, parse_start), (lambda: envelope(83), parse_finish), (exit_envelope, parse_exit)])
def test_unknown_business_fields_never_become_empty_success(factory, parser):
    request = factory()
    request.document.append(node(Container("Unexpected")))
    assert_rejected(parser, request)


@pytest.mark.parametrize("code", [0, -1])
def test_plain_exit_route_has_no_registered_transport_and_allows_xml_ack(code):
    request = exit_envelope(f"1002,{code}")
    assert request.profile.name == "quest_exit"
    assert request.profile.request_key == request.profile.response_key == 0
    assert request.profile.st is None and request.profile.checksum_required is False
    assert request.profile.registration_keys == ()
    assert parse_exit(request) == QuestExit(1002, code)
    assert encode_response(request.profile, b"<DA/>") == (b"<DA/>", "text/xml; charset=utf-8")
    assert ProtocolRegistry.load().decode("ntwh57oy", request.xml).digest == request.digest


@pytest.mark.parametrize("text", ["1002,1", "1002,-2", "1002, 0", "1002 0", "1002,0,1", "0,0", "2147483648,0"])
def test_exit_only_accepts_native_failure_and_withdraw_format(text):
    assert_rejected(parse_exit, exit_envelope(text))


@pytest.mark.parametrize("name", ["CT", "MK", "VN", "ST", "HS", "PFN", "PlatformCode"])
def test_exit_rejects_registered_metadata(name):
    request = exit_envelope()
    request.document.append(node(Column(name, "S", ("unexpected",))))
    assert_rejected(parse_exit, request)


def test_wrong_operation_profiles_cannot_be_parsed_as_another_event():
    assert_rejected(parse_finish, envelope())
    assert_rejected(parse_start, envelope(83))
    assert_rejected(parse_exit, envelope(83))
    assert_rejected(parse_start, exit_envelope())


def test_network_retry_keeps_identical_ct_payload_and_digest():
    request = envelope(83)
    retry = ProtocolRegistry.load().decode(request.profile.route, encode_wire(request.xml, request.profile.request_key))
    assert retry.xml == request.xml and retry.digest == request.digest
    assert parse_finish(retry) == parse_finish(request)
