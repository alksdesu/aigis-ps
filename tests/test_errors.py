import ast
from pathlib import Path

import pytest

import aigis_ps.errors as module
from aigis_ps.errors import REFUSAL_AFTERMATH, REFUSAL_POLICY, ProtocolError, UnsupportedOperation, caught


def raising(error: Exception):
    def action():
        raise error
    return action


def test_caught_returns_the_protocol_error_the_action_raised():
    error = ProtocolError("fixture_unavailable", "Fixture is unavailable", 503)
    assert caught(raising(error)) is error


def test_caught_returns_protocol_error_subclasses_unchanged():
    error = caught(raising(UnsupportedOperation("Fixture rule is unverified")))
    assert isinstance(error, UnsupportedOperation)
    assert (error.code, str(error), error.status) == ("not_implemented", "Fixture rule is unverified", 501)


def test_caught_runs_the_action_once_and_returns_none_on_success():
    calls = []
    assert caught(lambda: calls.append(1) or "ignored result") is None
    assert calls == [1]


@pytest.mark.parametrize("error", [ValueError("plain value error"), KeyError("missing"), TypeError("wrong type")],
                         ids=lambda error: type(error).__name__)
def test_caught_lets_other_exceptions_propagate(error):
    with pytest.raises(type(error)) as raised:
        caught(raising(error))
    assert raised.value is error


@pytest.mark.parametrize("sentence", [REFUSAL_AFTERMATH, REFUSAL_POLICY], ids=["aftermath", "policy"])
def test_refusal_sentences_are_spelled_once_where_they_are_published(sentence):
    published = Path(module.__file__).resolve()
    spelled = [path.name for path in sorted(published.parent.glob("*.py"))
               for node in ast.walk(ast.parse(path.read_bytes()))
               if isinstance(node, ast.Constant) and isinstance(node.value, str) and sentence in node.value]
    assert spelled == [published.name]
