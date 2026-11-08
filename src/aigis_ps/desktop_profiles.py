from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import threading
import uuid

from .config_registry import REGISTRY, document_name
from .config_sets import ConfigSets, MAX_MANIFEST_BYTES as CONFIG_MANIFEST_LIMIT, MAX_POINTER_BYTES as CONFIG_POINTER_LIMIT
from .local_io import atomic_replace, checked_path, deserialize, exclusive_file, read_bounded
from .masterdata import (MASTER_TABLES, MAX_MANIFEST_BYTES as MASTER_MANIFEST_LIMIT,
                         MAX_POINTER_BYTES as MASTER_POINTER_LIMIT, MasterDataStore, is_sha256, is_snapshot_id, json_bytes)
from .public_tables import MASTER_LIMITS, validate_public_master


MAX_NAME_LENGTH = 64
MAX_PROFILES = 256
MAX_METADATA_BYTES = 1024 * 1024
MAX_CONFIG_ANCESTORS = 256
_PROFILE_ID = re.compile(r"[a-f0-9]{32}\Z")
_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("存档名称必须是文本")
    name = value.strip()
    if not 1 <= len(name) <= MAX_NAME_LENGTH or not name.isprintable():
        raise ValueError(f"存档名称须为 1–{MAX_NAME_LENGTH} 个可打印字符")
    return name


def _identifier(value: object) -> str:
    if not isinstance(value, str) or value != "default" and not _PROFILE_ID.fullmatch(value):
        raise ValueError("存档 ID 无效")
    return value


def _relative(profile_id: str) -> str:
    return "runtime/server" if profile_id == "default" else f"runtime/saves/{profile_id}"


def _regular(path: Path, root: Path) -> Path:
    path = checked_path(path, root)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("存档配置必须使用独立的普通文件")
    return path


def _read(path: Path, limit: int, root: Path) -> bytes:
    return read_bounded(_regular(path, root), limit, root=root)


def _object(raw: bytes) -> dict:
    value = deserialize(raw)
    if not isinstance(value, dict):
        raise ValueError("存档配置必须是 JSON 对象")
    return value


class ProfileStore:
    def __init__(self, project_root: Path, state_dir: Path | None = None):
        self.project_root = checked_path(Path(project_root))
        if not self.project_root.is_dir():
            raise ValueError("项目目录不存在")
        requested = Path(state_dir) if state_dir is not None else Path("runtime/desktop")
        self.state_dir = checked_path(requested if requested.is_absolute() else self.project_root / requested,
                                      self.project_root)
        self.path = checked_path(self.state_dir / "profiles.json", self.project_root)
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(self.path, threading.RLock())
        with self._locked():
            if not self.path.exists():
                database = checked_path(self.project_root / "runtime/server/aigis.sqlite3", self.project_root)
                legacy = database.exists() and _regular(database, self.project_root).stat().st_size > 0
                profiles = [{"id": "default", "name": "当前存档", "data_dir": _relative("default")}] if legacy else []
                self._save({"schema_version": 1, "selected": "default" if legacy else None, "profiles": profiles})
            self._load()

    @contextmanager
    def _locked(self):
        with self._lock:
            checked_path(self.state_dir, self.project_root).mkdir(parents=True, exist_ok=True)
            lock = checked_path(self.state_dir / "profiles.lock", self.project_root)
            if lock.exists():
                _regular(lock, self.project_root)
            with exclusive_file(lock):
                yield

    def _data_dir(self, profile_id: str) -> Path:
        path = checked_path(self.project_root / _relative(profile_id), self.project_root)
        if path.exists() and not path.is_dir():
            raise ValueError("存档数据路径不是目录")
        return path

    def _load(self) -> dict:
        value = _object(_read(self.path, MAX_METADATA_BYTES, self.project_root))
        if (set(value) != {"schema_version", "selected", "profiles"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or not isinstance(value["profiles"], list) or len(value["profiles"]) > MAX_PROFILES):
            raise ValueError("存档列表格式无效")
        ids, names = set(), set()
        for profile in value["profiles"]:
            if not isinstance(profile, dict) or set(profile) != {"id", "name", "data_dir"}:
                raise ValueError("存档条目格式无效")
            profile_id, name = _identifier(profile["id"]), _name(profile["name"])
            if (profile_id in ids or name.casefold() in names or name != profile["name"]
                    or profile["data_dir"] != _relative(profile_id)):
                raise ValueError("存档条目重复或路径无效")
            directory = self._data_dir(profile_id)
            if profile_id != "default" and not directory.is_dir():
                raise ValueError("已登记的存档目录不存在")
            ids.add(profile_id)
            names.add(name.casefold())
        if (not ids and value["selected"] is not None
                or ids and _identifier(value["selected"]) not in ids):
            raise ValueError("当前选中的存档不存在")
        return value

    def _save(self, value: dict) -> None:
        temporary = checked_path(self.state_dir / f"profiles-{uuid.uuid4().hex}.tmp", self.project_root)
        created = False
        try:
            with temporary.open("xb") as stream:
                created = True
                stream.write(json_bytes(value))
                stream.flush()
                os.fsync(stream.fileno())
            destination = checked_path(self.path, self.project_root)
            if destination.exists():
                _regular(destination, self.project_root)
            atomic_replace(temporary, destination)
        finally:
            if created and temporary.exists():
                _regular(temporary, self.project_root).unlink()

    def _view(self, profile: dict, selected: str | None) -> dict:
        return {**profile, "data_dir": str(self._data_dir(profile["id"])), "selected": profile["id"] == selected}

    def list(self) -> list[dict]:
        with self._locked():
            value = self._load()
            return [self._view(profile, value["selected"]) for profile in value["profiles"]]

    def selected(self) -> dict | None:
        return next((profile for profile in self.list() if profile["selected"]), None)

    def select(self, profile_id: str) -> dict:
        profile_id = _identifier(profile_id)
        with self._locked():
            value = self._load()
            profile = next((item for item in value["profiles"] if item["id"] == profile_id), None)
            if profile is None:
                raise ValueError("存档不存在")
            if value["selected"] != profile_id:
                self._save({**value, "selected": profile_id})
            return self._view(profile, profile_id)

    def create(self, name: str) -> dict:
        name = _name(name)
        with self._locked():
            value = self._load()
            if len(value["profiles"]) >= MAX_PROFILES:
                raise ValueError("存档数量已达上限")
            if any(item["name"].casefold() == name.casefold() for item in value["profiles"]):
                raise ValueError("已有同名存档")
            source = self._data_dir(value["selected"] or "default")
            profile_id = uuid.uuid4().hex
            destination = self._data_dir(profile_id)
            if destination.exists():
                raise ValueError("新存档目录已经存在")
            parent = checked_path(destination.parent, self.project_root)
            parent.mkdir(parents=True, exist_ok=True)
            staging = checked_path(parent / f".creating-{uuid.uuid4().hex}", self.project_root)
            staging.mkdir()
            written: list[Path] = []
            try:
                try:
                    self._copy_public(source, staging, written)
                except FileNotFoundError as error:
                    raise ValueError("新建存档所需的公开主表或规则文件缺失，请先准备公开数据后重试。") from error
                if self._load() != value:
                    raise ValueError("存档列表已变化，请重试")
                if self._data_dir(profile_id).exists():
                    raise ValueError("新存档目录已经存在")
                checked_path(staging, self.project_root).rename(self._data_dir(profile_id))
                profile = {"id": profile_id, "name": name, "data_dir": _relative(profile_id)}
                selected = value["selected"] or profile_id
                self._save({**value, "selected": selected, "profiles": [*value["profiles"], profile]})
                return self._view(profile, selected)
            finally:
                if staging.exists():
                    self._clean_staging(staging, written)

    def _copy_public(self, source: Path, staging: Path, written: list[Path]) -> None:
        master_path = source / "masterdata/active.json"
        master_raw = _read(master_path, MASTER_POINTER_LIMIT, self.project_root)
        pointer = _object(master_raw)
        if (set(pointer) != {"snapshot", "manifest_sha256"} or not is_snapshot_id(pointer["snapshot"])
                or not is_sha256(pointer["manifest_sha256"])):
            raise ValueError("当前存档的主表指针无效")
        relative = Path("masterdata/snapshots") / pointer["snapshot"]
        manifest_raw = _read(source / relative / "manifest.json", MASTER_MANIFEST_LIMIT, self.project_root)
        if hashlib.sha256(manifest_raw).hexdigest() != pointer["manifest_sha256"]:
            raise ValueError("当前存档的主表清单已变化")
        manifest = _object(manifest_raw)
        self._write_public(staging, relative / "manifest.json", manifest_raw, written)
        for table in MASTER_TABLES:
            for suffix in (".xml", ".source.xml"):
                target = relative / f"{table}{suffix}"
                self._write_public(staging, target, _read(source / target, MASTER_LIMITS.max_bytes, self.project_root), written)
        self._write_public(staging, Path("masterdata/active.json"), master_raw, written)
        masters = MasterDataStore.for_data_dir(staging)
        snapshot = masters.snapshot()
        for table in MASTER_TABLES:
            validate_public_master(table, snapshot.get_xml(table), masters.schema_fields)
            raw = _read(staging / relative / f"{table}.source.xml", MASTER_LIMITS.max_bytes, staging)
            validate_public_master(table, raw, masters.schema_fields)
            if hashlib.sha256(raw).hexdigest() != manifest["tables"][table]["source_sha256"]:
                raise ValueError("当前存档的原始公开主表已变化")
        config_path = checked_path(source / "config/active.json", self.project_root)
        config_raw = _read(config_path, CONFIG_POINTER_LIMIT, self.project_root) if config_path.exists() else None
        if config_raw is not None:
            self._copy_config(source, staging, config_raw, written)
        if (_read(master_path, MASTER_POINTER_LIMIT, self.project_root) != master_raw
                or (config_raw is None) != (not checked_path(config_path, self.project_root).exists())
                or config_raw is not None and _read(config_path, CONFIG_POINTER_LIMIT, self.project_root) != config_raw):
            raise ValueError("主表或规则指针已变化，请重试新建存档")

    def _copy_config(self, source: Path, staging: Path, pointer_raw: bytes, written: list[Path]) -> None:
        pointer = _object(pointer_raw)
        identifier = pointer.get("set")
        seen = set()
        sets = ConfigSets(staging)
        while identifier is not None:
            if not is_snapshot_id(identifier) or identifier in seen or len(seen) >= MAX_CONFIG_ANCESTORS:
                raise ValueError("配置集的继承链无效或过长")
            seen.add(identifier)
            relative = Path("config/sets") / identifier
            raw = _read(source / relative / "manifest.json", CONFIG_MANIFEST_LIMIT, self.project_root)
            manifest = _object(raw)
            keys = manifest.get("keys")
            if not isinstance(keys, dict) or any(key not in REGISTRY for key in keys):
                raise ValueError("配置集的规则目录无效")
            self._write_public(staging, relative / "manifest.json", raw, written)
            for key, entry in keys.items():
                if not isinstance(entry, dict):
                    raise ValueError("配置集的规则条目无效")
                if entry.get("mode") == "explicit":
                    spec = REGISTRY[key]
                    target = relative / document_name(key)
                    document = _read(source / target, spec.max_bytes, self.project_root)
                    spec.canonical(document)
                    self._write_public(staging, target, document, written)
            config_set = sets.load(identifier)
            for key in config_set.explicit:
                spec = REGISTRY[key]
                if spec.content_version(spec.load(config_set.path(key))) != config_set.content_versions[key]:
                    raise ValueError("配置集的规则版本无效")
            identifier = config_set.parent
        self._write_public(staging, Path("config/active.json"), pointer_raw, written)
        sets.read_active()

    def _write_public(self, staging: Path, relative: Path, data: bytes, written: list[Path]) -> None:
        target = checked_path(staging / relative, staging)
        missing = []
        parent = target.parent
        while parent != staging and not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            checked_path(directory, staging).mkdir()
            written.append(directory)
        with target.open("xb") as stream:
            written.append(target)
            stream.write(data)

    def _clean_staging(self, staging: Path, written: list[Path]) -> None:
        for path in reversed(written):
            checked_path(path, staging)
            if path.is_dir():
                path.rmdir()
            elif path.exists():
                _regular(path, staging).unlink()
        checked_path(staging, self.project_root).rmdir()
