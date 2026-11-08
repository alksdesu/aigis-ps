import hashlib

import pytest

from aigis_ps import public_tables
from aigis_ps.public_tables import BytesDigest


MASTER = b"<DA><CardID><V>1</V></CardID></DA>"
OTHER_MASTER = b"<DA><QuestID><V>1</V></QuestID></DA>"


class TaggedBytes(bytes):
    pass


class CountingHashlib:
    def __init__(self):
        self.inputs = []

    def sha256(self, raw):
        self.inputs.append(raw)
        return hashlib.sha256(raw)


@pytest.fixture
def digested(monkeypatch):
    counter = CountingHashlib()
    monkeypatch.setattr(public_tables, "hashlib", counter)
    return counter.inputs


def expected(raw) -> str:
    return hashlib.sha256(raw).hexdigest()


def copy_of(raw: bytes) -> bytes:
    copy = bytes(bytearray(raw))
    assert copy == raw and copy is not raw
    return copy


def test_the_same_bytes_object_is_digested_once(digested):
    digest = BytesDigest()
    assert [digest(MASTER) for _ in range(3)] == [expected(MASTER)] * 3
    assert len(digested) == 1 and digested[0] is MASTER


def test_a_new_object_is_digested_again_even_with_equal_bytes(digested):
    digest, copy = BytesDigest(), copy_of(MASTER)
    assert digest(MASTER) == digest(copy) == digest(copy) == expected(MASTER)
    assert len(digested) == 2 and digested[1] is copy


def test_only_the_last_bytes_object_keeps_its_digest(digested):
    digest = BytesDigest()
    assert [digest(MASTER), digest(OTHER_MASTER), digest(MASTER)] == [
        expected(MASTER), expected(OTHER_MASTER), expected(MASTER)]
    assert len(digested) == 3


def test_each_digest_remembers_only_its_own_object(digested):
    first, second = BytesDigest(), BytesDigest()
    assert first(MASTER) == second(MASTER) == first(MASTER) == second(MASTER) == expected(MASTER)
    assert len(digested) == 2


@pytest.mark.parametrize("buffer", [bytearray(MASTER), TaggedBytes(MASTER), memoryview(MASTER)])
def test_other_buffers_are_digested_every_time_without_replacing_the_memo(digested, buffer):
    digest = BytesDigest()
    digest(MASTER)
    assert digest(buffer) == digest(buffer) == expected(MASTER)
    assert digest(MASTER) == expected(MASTER)
    assert len(digested) == 3 and digested[0] is MASTER and digested[1] is digested[2] is buffer


def test_a_mutated_bytearray_gets_its_new_digest():
    digest, buffer = BytesDigest(), bytearray(MASTER)
    before = digest(buffer)
    buffer[0] ^= 1
    assert digest(buffer) == expected(buffer) != before


@pytest.mark.parametrize("raw", [b"", MASTER, bytes(range(256))])
def test_digests_are_the_sha256_hex_of_the_bytes(raw):
    assert BytesDigest()(raw) == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("raw", [None, "CardData", 1])
def test_non_buffers_are_refused_before_and_after_a_digest_is_remembered(raw):
    digest = BytesDigest()
    with pytest.raises(TypeError):
        digest(raw)
    assert digest(MASTER) == expected(MASTER)
    with pytest.raises(TypeError):
        digest(raw)
    assert digest(MASTER) == expected(MASTER)
