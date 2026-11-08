import dataclasses

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.protocol import (
    NATIVE_CELL_BYTES, RESPONSE_TOO_LARGE, ProtocolRegistry, decode_wire, encode_response, encode_wire, native_checksum,
    parse_document, prepare_request, validate_snappy, xor_bytes,
)


def test_registry_preserves_wire_equivalent_aliases():
    registry = ProtocolRegistry.load()
    assert len(registry.by_registration) == 156
    assert registry.by_registration[29] is registry.by_registration[31]
    assert registry.by_registration[31].registration_keys == (29, 31)
    assert registry.by_registration[34].request_key != registry.by_registration[34].response_key


def test_native_row_cells_keep_255_bytes_before_their_terminator():
    assert NATIVE_CELL_BYTES == 255


def test_same_route_is_selected_by_encoding_and_st():
    registry = ProtocolRegistry.load()
    for number in (21, 23, 24, 29, 31, 34, 81, 83, 114, 129):
        profile = registry.by_registration[number]
        xml = prepare_request(b'<DA><UI Type="String">1</UI><RT></RT></DA>', profile.st)
        envelope = registry.decode(profile.route, encode_wire(xml, profile.request_key))
        assert number in envelope.profile.registration_keys
        assert b"<RT/>" in envelope.xml
        response = b'<DA><Value Type="Int">12</Value></DA>'
        wire, content_type = encode_response(profile, response)
        assert decode_wire(wire, profile.response_key) == response
        assert content_type == "application/octet-stream"


def test_native_literal_control_bytes_and_overlapping_copy():
    block = b"\x04\x0cA\x00B\x1f"
    assert validate_snappy(block) == 4
    assert decode_wire(xor_bytes(block, 14), 14) == b"A\x00B\x1f"
    assert decode_wire(xor_bytes(b"\x05\x00a\x01\x01", 14), 14) == b"aaaaa"


@pytest.mark.parametrize("block", [b"", b"\x80", b"\xff\xff\xff\xff\x10", b"\x04\x01\x00", b"\x01\x08ab", b"\x01\xfc", b"\x01\x00a\x00b"])
def test_malformed_snappy_is_rejected(block):
    with pytest.raises(ProtocolError):
        validate_snappy(block)


def test_snappy_length_limit_is_checked_before_decompression():
    with pytest.raises(ProtocolError, match="size limit"):
        validate_snappy(b"\x80\x80\x80\x01", limit=1024)


def test_checksum_uses_signed_bytes_at_even_offsets():
    assert native_checksum(b"\xff\x00\x80") == -7814047
    registry = ProtocolRegistry.load()
    profile = registry.by_registration[34]
    xml = prepare_request('<DA><N>日本語</N></DA>'.encode(), profile.st)
    assert registry.decode(profile.route, encode_wire(xml, profile.request_key)).xml == xml
    with pytest.raises(ProtocolError):
        registry.decode(profile.route, encode_wire(xml.replace(b"3002", b"3003"), profile.request_key))


def test_tampered_checksum_and_duplicate_fields_fail():
    registry = ProtocolRegistry.load()
    profile = registry.by_registration[34]
    xml = prepare_request(b"<DA><N>valid</N></DA>", profile.st)
    with pytest.raises(ProtocolError) as error:
        registry.decode(profile.route, encode_wire(xml.replace(b"valid", b"other"), profile.request_key))
    assert error.value.code == "invalid_checksum"
    with pytest.raises(ProtocolError):
        registry.decode(profile.route, encode_wire(xml.replace(b"</DA>", b"<ST>3002</ST></DA>"), profile.request_key))


def test_dtd_and_wrong_root_are_rejected():
    for xml in (b'<!DOCTYPE DA [<!ENTITY x "value">]><DA>&x;</DA>', b"<Other/>"):
        with pytest.raises(ProtocolError):
            parse_document(xml)


def test_response_capacity_and_unknown_route_fail_explicitly():
    profile = dataclasses.replace(ProtocolRegistry.load().by_registration[34], response_limit=1)
    with pytest.raises(ProtocolError) as error:
        encode_response(profile, b"<DA><N>1</N></DA>")
    # Pending reasons in saves and the HTTP error body carry this code, so its spelling stays fixed.
    assert error.value.code == RESPONSE_TOO_LARGE == "response_too_large"
    with pytest.raises(ProtocolError) as error:
        ProtocolRegistry.load().decode("unknown1", b"<DA/>")
    assert error.value.status == 404


def test_request_structure_never_contains_field_values():
    registry = ProtocolRegistry.load()
    profile = registry.by_registration[31]
    xml = prepare_request(b"<DA><UK>private-value</UK><MK>another-secret</MK></DA>", profile.st)
    summary = registry.decode(profile.route, encode_wire(xml, profile.request_key)).structure()
    assert "/DA/UK" in summary["field_paths"]
    assert "private-value" not in str(summary)
    assert "another-secret" not in str(summary)


def test_master_download_uses_bare_revision_document():
    registry = ProtocolRegistry.load()
    envelope = registry.decode("GRs733a4", b"<Rev>current</Rev>")
    assert envelope.profile.name == "master_cards"
    assert envelope.document.text == "current"
    with pytest.raises(ProtocolError):
        registry.decode("GRs733a4", b"<DA></DA>")


def test_application_payload_keeps_typed_data_and_removes_transport():
    registry = ProtocolRegistry.load()
    profile = registry.by_registration[114]
    payload = b'<DA><PLG T="I"><V>1</V></PLG></DA>'
    xml = prepare_request(payload, profile.st)
    envelope = registry.decode(profile.route, encode_wire(xml, profile.request_key))
    assert envelope.payload_xml() == payload
    assert envelope.document.find("HS") is not None


def test_large_public_response_does_not_relax_request_limits():
    registry = ProtocolRegistry.load()
    profile = registry.by_route["GRs733a4"][0]
    xml = b"<DA><Value>" + b"a" * (8 * 1024 * 1024) + b"</Value></DA>"
    wire, _ = encode_response(profile, xml)
    assert decode_wire(wire, profile.response_key, limit=profile.response_xml_limit) == xml
    with pytest.raises(ProtocolError):
        parse_document(xml)
    with pytest.raises(ProtocolError):
        encode_response(registry.by_registration[34], xml)


@pytest.mark.parametrize("st", [b'<ST T="I">3002</ST>', b"<ST>3002<V/></ST>"])
def test_structured_st_is_not_a_scalar(st):
    from aigis_ps.protocol import read_st
    with pytest.raises(ProtocolError, match="integer ST"):
        read_st(parse_document(b"<DA>" + st + b"</DA>"))


@pytest.mark.parametrize("route,name,xml", [
    ("foi6moes", "quest_record", b"<DA><UI>1</UI><UK>local-only</UK></DA>"),
    ("AekvKZk6", "game_info", b"<DA><Platform>Windows</Platform></DA>"),
])
def test_direct_plaintext_profiles_do_not_require_registered_transport_fields(route, name, xml):
    envelope = ProtocolRegistry.load().decode(route, xml)
    assert envelope.profile.name == name
    assert envelope.xml == xml
    assert envelope.profile.request_key == envelope.profile.response_key == 0
    response = b'<?xml version="1.0"?><DA><KEY T="S"><V>Major</V></KEY><VALUE T="I"><V>2</V></VALUE></DA>'
    assert encode_response(envelope.profile, response)[0] == response


def test_session_flags_use_bare_ui_and_a_bounded_integer_pair():
    envelope = ProtocolRegistry.load().decode("E935RTof", b"<UI>1</UI>")
    assert envelope.profile.name == "session_flags"
    assert encode_response(envelope.profile, b"0,0")[0] == b"0,0"
    for invalid in (b"{}", b"0", b"0,", b"0,2147483648", b"0,0,0"):
        with pytest.raises(ProtocolError):
            encode_response(envelope.profile, invalid)

