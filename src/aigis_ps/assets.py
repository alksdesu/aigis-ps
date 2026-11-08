from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

from .al_format import ALFormatError, ParseLimits, decompress_all4, parse_archive, parse_schema, parse_table


_HASH_NAME = re.compile(r"[a-fA-F0-9]{64}\Z")
_RESOURCE_NAME = re.compile(rb"[A-Za-z0-9_./-]+\.(?:atb|ard|aar|atx|aft|ogg|nut|lua)\x00")
_NUMBERED_NAMES = (
    "Map%04d.aar", "PlayerDot%04d.aar", "EnemyDot%04d.aar", "BattleEffect_%02d.aar",
)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def asset_key(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def recover_names(executable: Path | str | None, extra_names: Iterable[str] = ()) -> dict[str, list[dict[str, str]]]:
    candidates: dict[str, list[dict[str, str]]] = {}

    def add(name: str, evidence: str) -> None:
        entry = {"name": name, "evidence": evidence}
        values = candidates.setdefault(asset_key(name), [])
        if entry not in values:
            values.append(entry)

    if executable is not None:
        path = Path(executable)
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("Executable exceeds the name-recovery limit")
        data = path.read_bytes()
        for match in _RESOURCE_NAME.finditer(data):
            add(match[0][:-1].decode("ascii"), "executable_literal")
        for pattern in _NUMBERED_NAMES:
            if pattern.encode("ascii") + b"\0" not in data:
                continue
            for number in range(100 if "%02d" in pattern else 10000):
                add(pattern % number, "executable_format_candidate")
    for name in extra_names:
        add(name, "provided_name")
    return candidates


def _hash_file(path: Path) -> tuple[str, str]:
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()


def inventory_assets(source_root: Path | str, extra_names: Iterable[str] = ()) -> dict[str, Any]:
    root = Path(source_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Asset source must be a directory")
    exe = root / "Aigis_r.exe"
    names = recover_names(exe if exe.is_file() and not _is_link(exe) else None, extra_names)
    entries = []
    diagnostics = []
    directories = [root]
    cache = root / ".cache"
    if cache.is_dir() and not _is_link(cache):
        directories.append(cache)
    for directory in directories:
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if not _HASH_NAME.fullmatch(path.name) or not path.is_file():
                continue
            if _is_link(path) or not path.resolve().is_relative_to(root):
                diagnostics.append({"code": "source_symlink", "path": str(path.relative_to(root))})
                continue
            content_sha, content_md5 = _hash_file(path)
            with path.open("rb") as stream:
                header = stream.read(8)
            sidecar = path.with_name(path.name + ".md5")
            md5_info: dict[str, Any] = {"status": "absent", "actual": content_md5}
            if sidecar.exists():
                if _is_link(sidecar) or not sidecar.is_file() or sidecar.stat().st_size > 128:
                    md5_info["status"] = "invalid"
                else:
                    try:
                        expected = sidecar.read_text(encoding="utf-8-sig").strip().lower()
                    except UnicodeError:
                        expected = ""
                    if re.fullmatch(r"[a-f0-9]{32}", expected):
                        md5_info.update(expected=expected, status="matched" if expected == content_md5 else "mismatch")
                    else:
                        md5_info["status"] = "invalid"
            matches = names.get(path.name.lower(), [])
            unique_names = sorted({match["name"] for match in matches})
            entries.append({
                "source_path": path.relative_to(root).as_posix(), "filename_hash": path.name.lower(),
                "name": unique_names[0] if len(unique_names) == 1 else None,
                "name_candidates": matches, "size": path.stat().st_size, "sha256": content_sha,
                "magic": header[:4].decode("ascii", "backslashreplace"), "header_hex": header.hex(),
                "md5": md5_info,
            })
        for path in sorted(directory.glob("*.md5")):
            if _HASH_NAME.fullmatch(path.stem) and not path.with_suffix("").is_file():
                diagnostics.append({"code": "orphan_md5", "path": path.relative_to(root).as_posix()})
    return {
        "schema_version": 1, "source_root": str(root), "entries": entries, "diagnostics": diagnostics,
        "summary": {"files": len(entries), "bytes": sum(e["size"] for e in entries),
                    "mapped": sum(e["name"] is not None for e in entries),
                    "unmapped": sum(e["name"] is None for e in entries),
                    "md5_mismatches": sum(e["md5"]["status"] == "mismatch" for e in entries)},
    }


def _safe_name(name: str) -> None:
    if not name or "\x00" in name or "\\" in name or ":" in name:
        raise ValueError("Unsafe asset name")
    posix = PurePosixPath(name)
    if posix.is_absolute() or PureWindowsPath(name).drive or any(part in (".", "..") for part in name.split("/")):
        raise ValueError("Unsafe asset name")
    if any(not part or part.endswith((".", " ")) for part in name.split("/")):
        raise ValueError("Unsafe asset name")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if any(part.split(".")[0].upper() in reserved for part in posix.parts):
        raise ValueError("Unsafe asset name")
    if any(ord(character) < 32 or character in '<>"|?*' for character in name):
        raise ValueError("Unsafe asset name")


def _output_root(output_dir: Path | str, source_root: Path | None = None) -> Path:
    target = Path(output_dir).absolute()
    if any(_is_link(path) for path in (target, *target.parents)):
        raise ValueError("Output path must not contain symlinks")
    root = target.resolve()
    if source_root is not None and (root.is_relative_to(source_root) or source_root.is_relative_to(root)):
        raise ValueError("Output and source directories must be separate")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_json(path: Path, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(encoded)
        stream.write(b"\n")


def write_inventory(manifest: dict[str, Any], output_dir: Path | str) -> Path:
    root = _output_root(output_dir, Path(manifest["source_root"]).resolve())
    path = root / "manifest.json"
    _write_json(path, manifest)
    return path


def resolve_asset(source_root: Path | str, name_or_hash: str) -> Path:
    root = Path(source_root).resolve(strict=True)
    _safe_name(name_or_hash)
    key = name_or_hash.lower() if _HASH_NAME.fullmatch(name_or_hash) else asset_key(name_or_hash)
    matches = [path for path in (root / ".cache" / key, root / key) if path.is_file()]
    if not matches:
        raise FileNotFoundError(f"No local asset for {name_or_hash}")
    if len(matches) != 1:
        raise ValueError(f"Ambiguous local asset for {name_or_hash}")
    path = matches[0]
    if any(_is_link(item) for item in (path, path.parent)) or not path.resolve().is_relative_to(root):
        raise ValueError("Asset source must not follow symlinks")
    return path


def export_asset(
    source_root: Path | str,
    name_or_hash: str,
    output_dir: Path | str,
    limits: ParseLimits | None = None,
) -> dict[str, Any]:
    limits = limits or ParseLimits()
    source = resolve_asset(source_root, name_or_hash)
    if source.stat().st_size > limits.max_input_bytes:
        raise ALFormatError("Input exceeds the byte limit", code="input_limit")
    output = _output_root(output_dir, Path(source_root).resolve(strict=True))
    destination = output / source.name.lower()
    destination.mkdir(exist_ok=False)
    state = {"bytes": 0, "nodes": 0}
    diagnostics: list[dict[str, Any]] = []
    tables = []

    def visit(data: bytes, label: str, directory: Path, depth: int) -> dict[str, Any]:
        state["nodes"] += 1
        if depth > limits.max_depth:
            raise ALFormatError("Container nesting exceeds the limit", code="depth_limit")
        if state["nodes"] > limits.max_members:
            raise ALFormatError("Total node count exceeds the limit", code="member_limit")
        if state["bytes"] + len(data) > limits.max_total_bytes:
            raise ALFormatError("Total exported byte count exceeds the limit", code="total_byte_limit")
        state["bytes"] += len(data)
        directory.mkdir(exist_ok=True)
        with (directory / "payload.bin").open("xb") as stream:
            stream.write(data)
        magic = data[:4].decode("ascii", "backslashreplace")
        node: dict[str, Any] = {"name": label, "magic": magic, "size": len(data),
                                "sha256": hashlib.sha256(data).hexdigest(),
                                "payload_path": (directory / "payload.bin").relative_to(output).as_posix(),
                                "status": "ok", "children": []}
        issue_count = len(diagnostics)
        try:
            if data[:4] == b"ALL4":
                expanded = decompress_all4(data, limits)
                node["children"].append(visit(expanded, label, directory / "expanded", depth + 1))
            elif data[:4] == b"ALAR":
                archive = parse_archive(data, limits)
                node.update(version=archive.version, flags=archive.flags)
                node["members"] = [asdict(member) for member in archive.members]
                for member in archive.members:
                    member_label = member.name or f"member_{member.index:05d}"
                    try:
                        _safe_name(member_label)
                    except ValueError:
                        diagnostics.append({"code": "unsafe_member_name", "name": member_label, "parent": label})
                    child_dir = directory / f"member_{member.index:05d}"
                    try:
                        node["children"].append(visit(data[member.offset:member.offset + member.size], member_label, child_dir, depth + 1))
                    except ALFormatError as exc:
                        issue = {**exc.diagnostic(), "name": member_label, "parent": label}
                        diagnostics.append(issue)
                        node["children"].append({"name": member_label, "status": "error", "diagnostic": issue})
            elif data[:4] == b"ALRD":
                parsed = {"kind": "ALRD", **asdict(parse_schema(data, limits=limits))}
                _write_json(directory / "schema.json", parsed)
                node["schema_path"] = (directory / "schema.json").relative_to(output).as_posix()
            elif data[:4] == b"ALTB":
                parsed = parse_table(data, limits)
                _write_json(directory / "table.json", parsed)
                node.update(record_count=parsed["record_count"], record_size=parsed["record_size"])
                node["table_path"] = (directory / "table.json").relative_to(output).as_posix()
                tables.append({"name": label, "path": node["table_path"], "record_count": parsed["record_count"]})
            else:
                node["status"] = "opaque"
                diagnostics.append({"code": "unsupported_format", "name": label, "magic": magic})
        except ALFormatError as exc:
            node["status"] = "error"
            node["diagnostic"] = exc.diagnostic()
            diagnostics.append({**exc.diagnostic(), "name": label})
        if node["status"] == "ok" and len(diagnostics) > issue_count:
            node["status"] = "partial"
        return node

    tree = visit(source.read_bytes(), name_or_hash, destination, 0)
    report = {"schema_version": 1, "source": str(source), "asset": name_or_hash,
              "limits": asdict(limits), "tree": tree, "tables": tables, "diagnostics": diagnostics,
              "exported_bytes": state["bytes"], "nodes": state["nodes"],
              "complete": not diagnostics}
    report_path = destination / "report.json"
    _write_json(report_path, report)
    report["report_path"] = str(report_path)
    return report
