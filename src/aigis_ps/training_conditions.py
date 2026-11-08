from __future__ import annotations

import json
import re

from .errors import ProtocolError
from .native_numbers import INT32_MAX
from .system_literals import command_id


_TOKEN = re.compile(r'\s*("(?:[^"\\\x00-\x1f]|\\["\\/bfnrt]|\\u[0-9a-fA-F]{4})*"|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|&&|\|\||[!(),;])')


def _unsupported():
    raise ProtocolError("training_condition_unsupported", "This training condition is not supported", 501)


class ConditionExpression:
    def __init__(self, text, predicate):
        if not isinstance(text, str) or len(text.encode("utf-8")) > 8192:
            _unsupported()
        self.tokens = []
        offset = 0
        while text[offset:].strip():
            match = _TOKEN.match(text, offset)
            if match is None or len(self.tokens) >= 512:
                _unsupported()
            self.tokens.append(match.group(1))
            offset = match.end()
        self.position, self.predicate = 0, predicate

    def _take(self, token):
        if self.position < len(self.tokens) and self.tokens[self.position] == token:
            self.position += 1
            return True
        return False

    def _primary(self, depth):
        if depth > 32:
            _unsupported()
        if self._take("!"):
            return not self._primary(depth + 1)
        if self._take("("):
            value = self._or(depth + 1)
            if not self._take(")"):
                _unsupported()
            return value
        if self.position >= len(self.tokens):
            _unsupported()
        token = self.tokens[self.position]
        self.position += 1
        if token in ("0", "1"):
            return token == "1"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", token) or not self._take("("):
            _unsupported()
        arguments = []
        if not self._take(")"):
            while True:
                if self.position >= len(self.tokens):
                    _unsupported()
                argument = self.tokens[self.position]
                self.position += 1
                if argument.startswith('"'):
                    arguments.append(json.loads(argument))
                elif re.fullmatch(r"[0-9]{1,10}", argument) and int(argument) <= INT32_MAX:
                    arguments.append(int(argument))
                else:
                    _unsupported()
                if self._take(")"):
                    break
                if not self._take(","):
                    _unsupported()
        value = self.predicate(token, tuple(arguments))
        if type(value) is not bool:
            _unsupported()
        return value

    def _and(self, depth):
        result = self._primary(depth)
        while self._take("&&"):
            operand = self._primary(depth)
            result = result and operand
        return result

    def _or(self, depth):
        result = self._and(depth)
        while self._take("||"):
            operand = self._and(depth)
            result = result or operand
        return result

    def evaluate(self):
        if not self.tokens:
            return False
        value = self._or(0)
        self._take(";")
        if self.position != len(self.tokens):
            _unsupported()
        return value


def name_index(rows):
    result = {}
    for row in rows:
        key, value = row["_ResID"], row["_TypeID"]
        if type(key) is not int or type(value) is not int or value < 0 or key in result:
            raise ValueError("Invalid condition name table")
        result[key] = value
    return result


def named_values(arguments, index):
    if not arguments or any(not isinstance(value, str) for value in arguments):
        _unsupported()
    result = []
    for name in arguments:
        key = command_id(name)
        if key not in index:
            _unsupported()
        result.append(index[key])
    return tuple(result)
