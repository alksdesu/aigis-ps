import pytest

from client import capture_responses
from aigis_ps.admin_diff import COLUMN_XML_SECRETS, KEYED_XML_SECRETS
from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.response_capture import (EXCLUDED_OPERATIONS, POSITIONAL_SECRETS, PRIVATE_NAMES, RESPONSE_PRESETS,
                                       ResponseCaptureError, inspect_filtered_response, parse_response,
                                       sanitize_response, selected_operations)


def clean(raw):
    return sanitize_response(raw.encode() if isinstance(raw, str) else raw)


def login_group(user_key="private-user-key", player="900000123"):
    values = (1, int(player), 0, 77, 77, int(player), user_key, 0, 0, player, 0, 0, 0)
    return Container("LGI", tuple(Column(f"A{index + 1}", "S" if index in (6, 9) else "I", (value,))
                                  for index, value in enumerate(values)))


def test_credentials_are_removed_at_every_depth_and_through_name_overrides():
    raw = ('<DA><SYSTEM><A1 T="S"><V>ExchangeKey</V></A1><A2 T="S"><V>private-method</V></A2></SYSTEM>'
           '<UK T="S"><V>private-top</V></UK><Result T="I"><V>1</V></Result>'
           '<PPU><Rows><C N="UserKey[0]"><X T="S"><V>private-alias</V></X></C><Session_ID T="S"><V>private-session</V></Session_ID>'
           '<CardID T="I"><V>3</V></CardID></Rows></PPU></DA>')
    document, report = clean(raw)
    assert document == parse_response(b'<DA><Result T="I"><V>1</V></Result><PPU><Rows><CardID T="I"><V>3</V></CardID></Rows></PPU></DA>')
    assert report == {"removed": ["sessionid", "system", "uk", "userkey"], "removed_rows": 0}


def test_login_columns_carrying_identity_are_removed_wherever_the_group_appears():
    nested = Container("History", (login_group(),))
    document, report = clean(dumps(Container(children=(login_group(), nested))))
    kept = ["A1", "A3", "A4", "A5", "A8", "A9", "A11", "A12", "A13"]
    for group in (document.children[0], document.children[1].children[0]):
        assert [column.tag for column in group.children] == kept
    assert "private-user-key" not in repr(document) and "900000123" not in repr(document)
    assert report["removed"] == ["LGI.A10", "LGI.A2", "LGI.A6", "LGI.A7"]


def test_key_value_rows_with_credential_keys_are_removed_from_every_column():
    group = Container("PGP2", (Column("A1", "S", ("C1", "ExchangeKey", "LastLoginDate", "UK")),
                               Column("A2", "S", ("5", "private-exchange", "20261106", "private-uk"))))
    document, report = clean(dumps(Container(children=(group,))))
    expected = Container("PGP2", (Column("A1", "S", ("C1", "LastLoginDate")), Column("A2", "S", ("5", "20261106"))))
    assert document == parse_response(dumps(Container(children=(expected,))))
    assert report == {"removed": ["exchangekey", "uk"], "removed_rows": 2}


@pytest.mark.parametrize("extra", [Column("A2", "S", ("only-one",)), Container("A2")])
def test_misaligned_key_value_group_with_a_credential_key_is_dropped_whole(extra):
    group = Container("PGP2", (Column("A1", "S", ("C1", "MK")), extra))
    document, report = clean(dumps(Container(children=(group, Container("Keep")))))
    assert [node.name for node in document.children] == ["Keep"]
    assert "PGP2.*" in report["removed"] and report["removed_rows"] == 0


def test_misaligned_credential_rows_directly_under_the_root_reject_the_response():
    with pytest.raises(ResponseCaptureError, match="invalid_response_xml"):
        clean('<DA><A1 T="S"><V>UK</V></A1><Group/></DA>')


def test_business_groups_values_and_property_columns_survive_unchanged():
    raw = ('<?xml version="1.0"?>\n<DA><!--note--><PSC><ID T="I"><V>1</V><V>2</V></ID>'
           '<Name T="S"><V><![CDATA[<pack> & more]]></V><V>召喚 &amp; 光</V></Name></PSC>'
           '<C N="_1Prop" T="P" I="3"><V>x</V></C><Empty/></DA>')
    document, report = clean(raw)
    assert document == parse_response(raw.encode()) and report == {"removed": [], "removed_rows": 0}
    assert document.children[0].children[1].children[0].text == "<pack> & more"


@pytest.mark.parametrize("raw", [
    '<!DOCTYPE DA [<!ENTITY x "y">]><DA/>', '<Root/>', '<DA N="x"/>', '<DA/><DA/>', '<DA><G>',
    '<DA><G bad="1"/></DA>', '<DA><G N="a&amp;b"/></DA>', '<DA><Count T="I" N="Other"><V>1</V></Count></DA>',
    '<DA><G>text</G></DA>', '<DA><G T="I">text<V>1</V></G></DA>', '<DA><G T="I"><V N="x">1</V></G></DA>',
    '<DA><G T="I"><V><W/></V></G></DA>', '<DA><G T="S"><V>&#1;</V></G></DA>', b'<DA>\xff</DA>', b'<DA>\x00</DA>',
])
def test_malformed_or_ambiguous_documents_are_rejected(raw):
    with pytest.raises(ResponseCaptureError, match="invalid_response_xml"):
        clean(raw)


def test_inspection_accepts_filtered_output_and_reports_structure():
    data = dumps(Container(children=(Container("PPU", (Column("CardID", "I", (1, 2)), Column("Lv", "I", (5, 6)))),
                                     Column("Result", "I", (0,)))))
    metadata = inspect_filtered_response(data)
    assert metadata["groups"] == [{"name": "PPU", "columns": 2, "groups": 0, "rows": 2},
                                  {"name": "Result", "type": "I", "values": 1}]
    assert metadata["alxml_loadable"] is True and metadata["output_bytes"] == len(data)


def test_inspection_keeps_structurally_valid_responses_that_strict_alxml_cannot_load():
    metadata = inspect_filtered_response(b'<DA><Count T="I"><V></V></Count></DA>')
    assert metadata["alxml_loadable"] is False and metadata["alxml_error"]


@pytest.mark.parametrize("data", [
    dumps(Container(children=(Container("SYSTEM", (Column("A1", "S", ("x",)),)),))),
    dumps(Container(children=(login_group(),))),
    dumps(Container(children=(Container("PGP2", (Column("A1", "S", ("ExchangeKey",)), Column("A2", "S", ("x",)))),))),
    b'<DA><G><MethodKey T="S"><V>x</V></MethodKey></G></DA>',
])
def test_inspection_refuses_output_that_still_contains_private_material(data):
    with pytest.raises(ResponseCaptureError) as caught:
        inspect_filtered_response(data)
    assert caught.value.code == "response_agent_filter_incomplete"


def test_secrets_masked_by_the_admin_console_are_also_dropped_by_capture():
    for group, columns in COLUMN_XML_SECRETS.items():
        for column in columns:
            document, _ = clean(f'<DA><{group}><{column} T="S"><V>private-value</V></{column}></{group}></DA>')
            assert "private-value" not in repr(document)
    for group, keys in KEYED_XML_SECRETS.items():
        for key in keys:
            raw = f'<DA><{group}><A1 T="S"><V>{key}</V></A1><A2 T="S"><V>private-value</V></A2></{group}></DA>'
            assert "private-value" not in repr(clean(raw)[0])
            assert "private-value" not in repr(clean(raw.replace(group, "Other"))[0])


def test_positional_secrets_name_real_columns_and_never_clash_with_credential_names():
    for group, columns in POSITIONAL_SECRETS.items():
        assert group.lower() not in PRIVATE_NAMES
        assert all(column.lower() not in PRIVATE_NAMES for column in columns)


def test_operation_selection_is_deduplicated_and_refuses_excluded_or_invalid_numbers():
    assert selected_operations([41, 12, 41]) == (12, 41)
    for values, code in (([152], "excluded_operation"), ([0], "invalid_operation"), ([True], "invalid_operation"),
                         ([0x10000], "invalid_operation"), ([], "empty_operation_selection")):
        with pytest.raises(ResponseCaptureError, match=code):
            selected_operations(values)


def test_presets_and_excluded_reactors_agree_with_the_exclusion_list():
    assert all(not set(operations) & EXCLUDED_OPERATIONS.keys() for operations in RESPONSE_PRESETS.values())
    excluded = {operation for reactor in capture_responses.EXCLUDED_REACTORS for operation in reactor.operations}
    assert excluded == EXCLUDED_OPERATIONS.keys()
