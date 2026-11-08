import ast
from collections.abc import Iterator
from pathlib import Path
import zlib

import pytest

import aigis_ps
from aigis_ps import system_literals as literals
from aigis_ps.system_literals import (RATING_COMMAND, command_id, decode_command, encode_command, rating_command_id,
    system_literals)


SOURCE = Path(aigis_ps.__file__).resolve().parent
SERVER_MODULES = tuple(sorted(SOURCE.glob("*.py")))
LITERALS_MODULE = SOURCE / Path(literals.__file__).name
CRC32 = zlib.crc32.__name__
RATING_KEY = RATING_COMMAND.format("")
RATING_KEYS = {str: RATING_KEY, bytes: RATING_KEY.encode()}


def row(command):
    key = bytes.fromhex("fe5866c7")
    return {"_ID": 1, "_Cmnd": bytes(value ^ key[index % 4] for index, value in enumerate(command.encode())).hex()}


def test_literal_settings_preserve_explicit_empty_and_missing_defaults():
    settings = system_literals([row('SetSystemConfigParam("Cap",600);\nSetSystemConfigParam("Classes",""); '
                                   'SetSystemConfigParam("Scale",0.8);')], {"Cap": 99, "Classes": "7,9,13,15,16", "Other": 1})
    assert settings == {"Cap": 600, "Classes": "", "Other": 1}


@pytest.mark.parametrize("text", ['if(1){SetSystemConfigParam("Cap",600);}',
    'SetSystemConfigParam("Cap",GetValue());', 'SetSystemConfigParam("Cap",600);SetSystemConfigParam("Cap",99);'])
def test_conditional_dynamic_or_ambiguous_assignment_is_not_treated_as_literal(text):
    with pytest.raises(ValueError):
        system_literals([row(text)], {"Cap": 99})


@pytest.mark.parametrize(("name", "identifier"), [
    ("FCMenuQuestTypeList::IsEntrust_0", 2052496187), ("HasEntrustByType_2", 1512763177),
    ("HasEntrustByType_8", -1157691849), ("HasEntrustByType_18", -1815759277),
    ("VoteGiftInfomationText", -828020215), ("英傑", 163175264),
])
def test_command_ids_are_signed_utf8_crc32_values(name, identifier):
    assert command_id(name) == identifier


@pytest.mark.parametrize("name", [None, b"HasEntrustByType_2", 2, "\ud800"])
def test_command_ids_require_encodable_text(name):
    with pytest.raises(ValueError):
        command_id(name)


@pytest.mark.parametrize(("card_id", "identifier"), [(1, -504556704), (2, 2028225242)])
def test_rating_command_ids_hash_the_per_card_rating_key(card_id, identifier):
    key = RATING_COMMAND.format(card_id)
    signed = int.from_bytes(zlib.crc32(key.encode("utf-8")).to_bytes(4, "big"), "big", signed=True)
    assert key == f"IsCanUseRating_{card_id}"
    assert rating_command_id(card_id) == command_id(key) == signed == identifier


@pytest.mark.parametrize("text", ["", "1;", 'IsOpenMission( "Tag" , $Tag );', 'SetUpdaterFunc( "Func_GeneralQuestMenu" );',
                                  'MnObj_SetString("VoteResultMainWindow::TX03" , "text=\u3000" );\n1;', "英傑"])
def test_encoded_commands_decode_back_and_match_native_table_encoding(text):
    encoded = encode_command(text)
    assert encoded == row(text)["_Cmnd"] and encoded == encoded.lower()
    assert decode_command(encoded) == text and encode_command(decode_command(encoded.upper())) == encoded


@pytest.mark.parametrize("text", [None, b"1;", 1, "\ud800"])
def test_command_encoding_rejects_values_that_cannot_round_trip(text):
    with pytest.raises(ValueError):
        encode_command(text)


def test_encoding_and_decoding_share_one_length_bound(monkeypatch):
    monkeypatch.setattr(literals, "_MAX_ENCODED_LENGTH", 8)
    assert decode_command(encode_command("1234")) == "1234"
    with pytest.raises(ValueError):
        encode_command("12345")
    with pytest.raises(ValueError):
        decode_command(row("12345")["_Cmnd"])


def callee(call: ast.Call) -> str | None:
    function = call.func
    if isinstance(function, ast.Attribute):
        return function.attr
    return function.id if isinstance(function, ast.Name) else None


def reads_signed(call: ast.Call) -> bool:
    return any(keyword.arg == "signed" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
               for keyword in call.keywords)


def bindings(tree: ast.AST) -> Iterator[tuple[str, ast.expr]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            yield from ((target.id, node.value) for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and isinstance(node.target, ast.Name) and node.value:
            yield node.target.id, node.value


class Crc32Values:
    # A second fold may reach crc32 through an import alias or a variable, so both are traced.
    def __init__(self, tree: ast.AST):
        self.callees = {CRC32} | {alias.asname for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                                  for alias in node.names if alias.name == CRC32 and alias.asname}
        self.names, bound = set(), list(bindings(tree))
        while found := {name for name, value in bound if name not in self.names and self.holds(value)}:
            self.names |= found

    def holds(self, node: ast.AST) -> bool:
        # Arithmetic keeps a CRC32 value, while other calls, subscripts and attributes derive something else.
        if isinstance(node, ast.Call):
            return callee(node) in self.callees
        if isinstance(node, ast.Name):
            return node.id in self.names
        if isinstance(node, ast.BinOp):
            return self.holds(node.left) or self.holds(node.right)
        return isinstance(node, ast.UnaryOp) and self.holds(node.operand)

    def folds_sign(self, node: ast.AST) -> bool:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
            return self.holds(node.left)
        return isinstance(node, ast.Call) and reads_signed(node) and any(map(self.holds, ast.walk(node)))


def signed_crc32_lines(source: str | bytes, filename: str = "<snippet>") -> list[int]:
    tree = ast.parse(source, filename)
    values = Crc32Values(tree)
    return sorted(node.lineno for node in ast.walk(tree) if values.folds_sign(node))


def spells_rating_key(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and type(node.value) in RATING_KEYS and RATING_KEYS[type(node.value)] in node.value


def rating_key_lines(source: str | bytes, filename: str = "<snippet>") -> list[int]:
    return sorted(node.lineno for node in ast.walk(ast.parse(source, filename)) if spells_rating_key(node))


def assignment_line(path: Path, name: str) -> int:
    return next(statement.lineno for statement in ast.parse(path.read_bytes(), str(path)).body
                if isinstance(statement, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == name for target in statement.targets))


def test_the_literal_guards_scan_every_server_module():
    assert SERVER_MODULES and LITERALS_MODULE in SERVER_MODULES


@pytest.mark.parametrize("source", [
    "key = (zlib.crc32(b'IsCanUseRating_7') ^ 0x80000000) - 0x80000000",
    "key = (crc32(name.encode('utf-8')) ^ SIGN) - SIGN",
    "key = (0x80000000 ^ binascii.crc32(data)) - 0x80000000",
    "key = int.from_bytes(zlib.crc32(data).to_bytes(4, 'big'), 'big', signed=True)",
    "digest = zlib.crc32(data); key = (digest ^ SIGN) - SIGN",
    "digest = zlib.crc32(data); folded = digest ^ SIGN; key = folded - SIGN",
    "digest = zlib.crc32(data); key = digest - (1 << 32) if digest >= 1 << 31 else digest",
    "key = value if (value := zlib.crc32(data)) < 1 << 31 else value - (1 << 32)",
    "digest: int = zlib.crc32(data) & MASK; key = int.from_bytes(digest.to_bytes(4, 'big'), 'big', signed=True)",
    "from zlib import crc32 as checksum; key = (checksum(data) ^ SIGN) - SIGN",
])
def test_the_crc32_guard_finds_inline_and_named_sign_folds(source):
    assert signed_crc32_lines(source) == [1]


@pytest.mark.parametrize("source", [
    "digest = zlib.crc32(name.encode('utf-8'))", "key = command_id(name)",
    "flags = (flags ^ 0x80000000) - 0x80000000", "value = int.from_bytes(raw, 'little', signed=True)",
    "digest = int.from_bytes(zlib.crc32(data).to_bytes(4, 'big'), 'big')",
    "value = raw if raw < 0x80000000 else raw - 0x100000000",
    "mask = zlib.crc32(key.encode('utf-8')).to_bytes(4, 'big'); last = len(mask) - 1",
    "spare = len(keys) - len({zlib.crc32(key.encode('utf-8')) for key in keys})",
])
def test_the_crc32_guard_allows_unsigned_digests_and_unrelated_sign_folds(source):
    assert signed_crc32_lines(source) == []


def test_command_id_is_the_only_signed_crc32_in_the_server():
    signed = {path.name: lines for path in SERVER_MODULES if (lines := signed_crc32_lines(path.read_bytes(), str(path)))}
    assert list(signed) == [LITERALS_MODULE.name]


@pytest.mark.parametrize("source", [
    "key = command_id('IsCanUseRating_7')", "key = command_id(f'IsCanUseRating_{card_id}')",
    "key = zlib.crc32(b'IsCanUseRating_7')", "key = command_id('IsCanUse' 'Rating_' + str(card_id))",
])
def test_the_rating_guard_finds_spelled_rating_keys(source):
    assert rating_key_lines(source) == [1]


@pytest.mark.parametrize("source", [
    "key = rating_command_id(card_id)", "key = command_id(RATING_COMMAND.format(card_id))",
    "key = command_id('HasEntrustByType_2')",
])
def test_the_rating_guard_allows_keys_built_from_the_rating_command(source):
    assert rating_key_lines(source) == []


def test_rating_command_is_the_only_spelled_rating_key_in_the_server():
    spelled = {path.name: lines for path in SERVER_MODULES if (lines := rating_key_lines(path.read_bytes(), str(path)))}
    assert spelled == {LITERALS_MODULE.name: [assignment_line(LITERALS_MODULE, "RATING_COMMAND")]}
