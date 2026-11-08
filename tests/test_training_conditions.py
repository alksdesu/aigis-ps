import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.training_conditions import ConditionExpression, name_index, named_values


@pytest.mark.parametrize("expression,expected", [("1;", True), ("", False), ("0;", False),
    ("!0 && (0 || 1);", True), ("0 || 1 && 0", False), ("!IsCardID(2,4) || IsGenus(\"ちび\");", True)])
def test_boolean_precedence_and_typed_predicate_arguments(expression, expected):
    def predicate(name, args):
        if name == "IsCardID":
            return 2 in args
        if name == "IsGenus":
            return args == ("ちび",)
        raise AssertionError(name)
    assert ConditionExpression(expression, predicate).evaluate() is expected


@pytest.mark.parametrize("expression", ["1;Unknown();", "SetMoney(999);", "true", "1+1", "(1;", "IsCardID(-1)"])
def test_unsupported_syntax_and_predicates_are_never_executed(expression):
    def predicate(name, args):
        raise ProtocolError("unknown", "Unknown predicate", 501)
    with pytest.raises(ProtocolError):
        ConditionExpression(expression, predicate).evaluate()


def test_short_circuit_does_not_hide_an_unknown_predicate():
    def predicate(name, args):
        raise ProtocolError("unknown", "Unknown predicate", 501)
    with pytest.raises(ProtocolError):
        ConditionExpression("1 || Unknown();", predicate).evaluate()


def test_resource_names_resolve_by_native_crc32():
    index = name_index([{"_ResID": -2095642104, "_TypeID": 108}])
    assert named_values(("ちび",), index) == (108,)
    with pytest.raises(ProtocolError):
        named_values(("unknown",), index)
