from __future__ import annotations

import hashlib
import json

import pytest

from aigis_ps.alxml import Column, Container, Limits, ValueType, dumps, loads
from aigis_ps.schedule_capture import CREDENTIAL_NAMES, SCHEDULE_GROUPS, SCHEDULE_LIMITS, ScheduleCaptureError, sanitize_schedule_response


def xml(*groups):
    return dumps(Container("DA", groups), limits=SCHEDULE_LIMITS)


def field(name, kind="I", values=(1,)):
    return Column(name, kind, values)


def test_keeps_native_business_groups_and_types_without_raw_metadata():
    groups = tuple(Container(name, (field("ID"), field("Label", "S", ("pool",)))) for name in SCHEDULE_GROUPS[70])
    raw = xml(*groups, Container("SYSTEM", (field("UK", "S", ("private-system-key",)),)),
              Container("PrivateUser", (field("Secret", "S", ("private-user-key",)),)))
    output, metadata = sanitize_schedule_response(70, raw)
    assert loads(output) == Container("DA", groups)
    assert metadata == {
        "operation": 70, "input_bytes": len(raw), "output_bytes": len(output),
        "kept_groups": list(SCHEDULE_GROUPS[70]), "missing_groups": [],
        "groups": {name: {"rows": 1, "row_counts": [1], "field_types": {"ID": "I", "Label": "S"}} for name in SCHEDULE_GROUPS[70]},
        "removed_columns": [], "sha256": hashlib.sha256(output).hexdigest(),
    }
    combined = output + json.dumps(metadata).encode()
    for secret in (b"SYSTEM", b"private-system-key", b"PrivateUser", b"private-user-key"):
        assert secret not in combined
    assert hashlib.sha256(raw).hexdigest().encode() not in combined


@pytest.mark.parametrize("name", ["UI", "UK", "MK", "UID", "UserID", "PlayerID", "Authorization", "Cookie",
                                  "password", "token", "sessionkey", "SESSION_ID", "Access-Token", "client.secret",
                                  "GPVI", "GPOT", "oauth_token_secret", "UserKey", "MethodKey", "USER_KEY", "method_key",
                                  "uSeR.kEy", "METHOD-KEY"])
def test_removes_credentials_case_insensitively_at_every_depth(name):
    nested = Container("Rows", (field("ID"), field(name, "S", ("credential-value",))))
    raw = xml(Container("CSS", (field(name, "S", ("credential-value",)), nested)))
    output, metadata = sanitize_schedule_response(70, raw)
    assert loads(output) == Container("DA", (Container("CSS", (Container("Rows", (field("ID"),)),)),))
    assert b"credential-value" not in output
    assert "credential-value" not in json.dumps(metadata)
    assert len(metadata["removed_columns"]) == 1


def test_removes_credential_containers_and_nested_system():
    raw = xml(Container("CSS", (Container("Credentials", (field("Value", "S", ("hidden",)),)),
                               Container("system", (field("Unknown", "S", ("sensitive",)),)), field("ID"))))
    output, metadata = sanitize_schedule_response(70, raw)
    assert loads(output) == Container("DA", (Container("CSS", (field("ID"),)),))
    assert metadata["removed_columns"] == ["credentials", "system"]


def test_business_session_counter_and_all_native_types_survive():
    columns = (field("SessionCount", values=(4,)), field("TokenCount", values=(8,)),
               field("Signed", values=(-2147483648,)), field("Large", "I64", (2**62,)),
               field("Enabled", "B", (True,)), field("Ratio", "F", (0.25,)),
               field("Weight", "D", (1.125,)), field("Text", "S", ("& <pool>",)),
               field("Date", "DT", ("2026-11-01 00:00:00",)), field("Property", "P", ("value",)))
    group = Container("CSP", columns)
    output, metadata = sanitize_schedule_response(71, xml(group))
    assert loads(output) == Container("DA", (group,))
    assert metadata["groups"]["CSP"]["field_types"] == {column.name: column.type.value for column in columns}
    assert metadata["missing_groups"] == []


@pytest.mark.parametrize("operation,name", [(70, "CSS"), (71, "CSP")])
@pytest.mark.parametrize("columns", [(), (Column("ID", "I", ()), Column("Name", "S", ()))])
def test_empty_tables_are_valid_and_not_synthesized(operation, name, columns):
    group = Container(name, columns)
    output, metadata = sanitize_schedule_response(operation, xml(group))
    assert loads(output) == Container("DA", (group,))
    assert metadata["groups"][name]["rows"] == 0
    assert metadata["missing_groups"] == [group for group in SCHEDULE_GROUPS[operation] if group != name]


def test_preserves_nested_rows_indices_and_reports_types_without_values():
    group = Container("CSS", (Container("Rows", (field("ID"),), "0"), Container("Rows", (field("ID", values=(2,)),), "1")))
    output, metadata = sanitize_schedule_response(70, xml(group))
    assert loads(output) == Container("DA", (group,))
    assert metadata["groups"]["CSS"]["groups"] == {
        "Rows[0]": {"rows": 1, "row_counts": [1], "field_types": {"ID": "I"}},
        "Rows[1]": {"rows": 1, "row_counts": [1], "field_types": {"ID": "I"}},
    }


def test_top_group_name_override_uses_native_effective_name():
    output, metadata = sanitize_schedule_response(70, b'<DA><C N="CSS"><ID T="I"><V>1</V></ID></C></DA>')
    assert loads(output).children[0].name == "CSS"
    assert metadata["kept_groups"] == ["CSS"]


@pytest.mark.parametrize("operation,raw,code", [
    (70, b"<DA></DA>", "missing_schedule_group"),
    (71, b"<DA><CSS /></DA>", "missing_schedule_group"),
    (70, b"<DA><CSS /><CSS /></DA>", "duplicate_schedule_group"),
    (70, b"<DA><CSS /><FPR /><FPR /></DA>", "duplicate_schedule_group"),
    (71, b"<DA><CSP /><CSP /></DA>", "duplicate_schedule_group"),
    (70, b'<DA><CSS T="S"><V>secret</V></CSS></DA>', "invalid_schedule_group"),
    (70, b'<DA><CSS I="0" /></DA>', "invalid_schedule_group"),
    (70, b'<DA><CSS><ID T="I"/><ID T="I"/></CSS></DA>', "duplicate_schedule_field"),
    (70, b'<DA><CSS><Row/><Row/></CSS></DA>', "duplicate_schedule_field"),
    (70, b'<DA><CSS><Count T="I"><V>secret</V></Count></CSS></DA>', "invalid_schedule_xml"),
    (70, b'<DA><CSS><Count T="secret"/></CSS></DA>', "invalid_schedule_xml"),
    (70, b'<!DOCTYPE DA [<!ENTITY token "secret">]><DA><CSS/></DA>', "invalid_schedule_xml"),
    (70, b'<!DOCTYPE DA SYSTEM "file:///secret"><DA><CSS/></DA>', "invalid_schedule_xml"),
    (70, b'<DA><CSS><X T="I"><V>2147483648</V></X></CSS></DA>', "invalid_schedule_xml"),
    (70, b'<DA><CSS><X T="B"><V>1</V></X></CSS></DA>', "invalid_schedule_xml"),
    (70, b'<DA><CSS><X T="F"><V>NaN</V></X></CSS></DA>', "invalid_schedule_xml"),
    (70, b'<DA><CSS><X T="S"><V>secret', "invalid_schedule_xml"),
    (70, b'<NotDA><CSS /></NotDA>', "invalid_schedule_xml"),
    (70, b'<Outer><DA><CSS /></DA></Outer>', "invalid_schedule_xml"),
    (70, b'<DA N="Other"><CSS /></DA>', "invalid_schedule_xml"),
])
def test_invalid_shapes_raise_only_fixed_code(operation, raw, code):
    with pytest.raises(ScheduleCaptureError) as caught:
        sanitize_schedule_response(operation, raw)
    assert str(caught.value) == caught.value.code == code
    assert "secret" not in repr(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("operation", [True, False, 70.0, "70", None, 69, 72])
def test_rejects_unknown_operations(operation):
    with pytest.raises(ScheduleCaptureError, match="^unsupported_schedule_operation$"):
        sanitize_schedule_response(operation, b"<DA><CSS /></DA>")


@pytest.mark.parametrize("raw", [None, "<DA><CSS /></DA>", bytearray(b"<DA><CSS /></DA>"), b""],
                         ids=["none", "text", "bytearray", "empty"])
def test_payload_bounds(raw):
    with pytest.raises(ScheduleCaptureError, match="^invalid_schedule_payload$"):
        sanitize_schedule_response(70, raw)


def test_rejects_oversized_payload():
    with pytest.raises(ScheduleCaptureError, match="^invalid_schedule_payload$"):
        sanitize_schedule_response(70, b"x" * (SCHEDULE_LIMITS.max_bytes + 1))


def test_element_limit():
    raw = b"<DA><CSS>" + b"<X />" * (SCHEDULE_LIMITS.max_elements - 1) + b"</CSS></DA>"
    with pytest.raises(ScheduleCaptureError, match="^invalid_schedule_xml$"):
        sanitize_schedule_response(70, raw)


def test_depth_limit():
    raw = b"<DA><CSS>" + b"<Row>" * 31 + b"</Row>" * 31 + b"</CSS></DA>"
    with pytest.raises(ScheduleCaptureError, match="^invalid_schedule_xml$"):
        sanitize_schedule_response(70, raw)


def test_repeated_sanitization_is_stable():
    first, _ = sanitize_schedule_response(70, xml(Container("CSS", (field("ID"),)), Container("SYSTEM")))
    second, metadata = sanitize_schedule_response(70, first)
    assert second == first
    assert metadata["sha256"] == hashlib.sha256(first).hexdigest()


def test_preserves_ragged_columns_without_claiming_rectangular_shape():
    group = Container("CSS", (field("ID", values=(1, 2)), field("Optional", "S", ("one",))))
    output, metadata = sanitize_schedule_response(70, xml(group))
    assert loads(output) == Container("DA", (group,))
    assert metadata["groups"]["CSS"]["row_counts"] == [1, 2]


def test_limits_are_explicit_and_bounded():
    assert isinstance(SCHEDULE_LIMITS, Limits)
    assert SCHEDULE_LIMITS.max_bytes == 32 * 1024 * 1024
    assert SCHEDULE_LIMITS.max_elements == 1_000_000
    assert SCHEDULE_LIMITS.max_values == 1_000_000


def test_shared_credentials_are_read_only_and_include_full_key_names():
    assert isinstance(CREDENTIAL_NAMES, frozenset)
    assert {"userkey", "methodkey", "uk", "mk"} <= CREDENTIAL_NAMES
