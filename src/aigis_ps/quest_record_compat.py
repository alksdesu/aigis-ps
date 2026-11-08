from __future__ import annotations

from functools import cache
import hashlib
from importlib.resources import files
import json
import re
from types import MappingProxyType

from .errors import ProtocolError
from .native_numbers import int32


@cache
def _versions():
    data = json.loads(files("aigis_ps").joinpath("data/quest-record-compat.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("versions"), list):
        raise ValueError("Quest record compatibility definitions are invalid")
    versions = {}
    for entry in data["versions"]:
        digest, identifiers = entry["master_sha256"], entry["retained_quest_ids"]
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or digest in versions
                or not isinstance(identifiers, list) or len(set(identifiers)) != len(identifiers)
                or any(not int32(identifier) or identifier <= 0 for identifier in identifiers)):
            raise ValueError("Quest record compatibility identities are invalid")
        versions[digest] = frozenset(identifiers)
    return MappingProxyType(versions)


def retained_quest_ids(master: bytes) -> frozenset[int]:
    try:
        return _versions().get(hashlib.sha256(master).hexdigest(), frozenset())
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ProtocolError("quest_record_compat_invalid", "Quest record compatibility definitions are invalid", 503) from error
