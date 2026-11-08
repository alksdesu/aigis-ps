from __future__ import annotations

from collections.abc import Collection
import hashlib

from .alxml import AlXmlError, Column, Container, Limits, ValueType, loads


MASTER_LIMITS = Limits(max_bytes=32 * 1024 * 1024, max_depth=4, max_elements=2_000_000,
                       max_values=1_500_000, max_text_bytes=1024 * 1024)
MASTER_IDS = {"CardData": "CardID", "QuestTable": "QuestID"}
REQUIRED_FIELDS = {
    "CardData": frozenset({"CardID", "InitClassID", "ClassLV0SkillID", "ClassLV1SkillID"}),
    "QuestTable": frozenset({"QuestID", "MapNo", "EntryNo", "LocationNo", "Capacity"}),
}


class PublicTableError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def validate_public_master(name: str, data: bytes, schemas: dict[str, Collection[str]]) -> tuple[Container, dict]:
    if not isinstance(name, str) or name not in MASTER_IDS or not isinstance(data, bytes) or not 0 < len(data) <= MASTER_LIMITS.max_bytes:
        raise PublicTableError("invalid_master_payload")
    if not isinstance(schemas, dict):
        raise PublicTableError("schema_mismatch")
    declared = schemas.get(name)
    if not isinstance(declared, Collection) or isinstance(declared, (str, bytes)) or any(not isinstance(field, str) or not field for field in declared):
        raise PublicTableError("schema_mismatch")
    expected = set(declared)
    if not REQUIRED_FIELDS[name] <= expected:
        raise PublicTableError("schema_mismatch")
    try:
        document = loads(data, limits=MASTER_LIMITS)
    except AlXmlError as exc:
        raise PublicTableError("invalid_master_xml") from exc
    if any(not isinstance(child, Column) for child in document.children):
        raise PublicTableError("non_public_table_shape")
    columns = {child.name: child for child in document.children}
    if len(columns) != len(document.children):
        raise PublicTableError("duplicate_master_columns")
    names = set(columns)
    if not REQUIRED_FIELDS[name] <= names or not names <= expected:
        raise PublicTableError("non_public_table_columns")
    lengths = {len(column.values) for column in columns.values()}
    if len(lengths) != 1 or not 0 < next(iter(lengths)) <= 65535:
        raise PublicTableError("invalid_master_row_count")
    identifiers = columns[MASTER_IDS[name]]
    if identifiers.type is not ValueType.INT or any(type(value) is not int or value <= 0 for value in identifiers.values):
        raise PublicTableError("invalid_master_ids")
    if len(set(identifiers.values)) != len(identifiers.values):
        raise PublicTableError("duplicate_master_ids")
    if any(column.type is ValueType.PROP for column in columns.values()):
        raise PublicTableError("unsupported_master_type")
    metadata = {"rows": len(identifiers.values), "columns": len(columns),
                "column_types": {key: value.type.value for key, value in columns.items()},
                "missing_schema_columns": sorted(expected - names), "full_schema": names == expected}
    return document, metadata


class BytesDigest:
    def __init__(self):
        self._digested = None, None

    def __call__(self, raw: bytes) -> str:
        source, digest = self._digested
        # Bytes never change, so the object digested last keeps its digest.
        if digest is None or raw is not source:
            digest = hashlib.sha256(raw).hexdigest()
            if type(raw) is bytes:
                self._digested = raw, digest
        return digest
