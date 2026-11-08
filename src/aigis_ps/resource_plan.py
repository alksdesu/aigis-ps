from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
import hashlib
from pathlib import Path, PurePosixPath
import re

from .al_format import MAX_RESOURCE_BYTES, decompress_all4, parse_table
from .assets import inventory_assets
from .local_io import checked_path, read_bounded
from .mission_tags import mission_tables
from .resource_fetch import ResourceEntry


STORY_PACKAGES = frozenset(("HarlemText.aar", "HarlemEventText0.aar", "HarlemEventText1.aar", "EventArc.aar", "EventHomeArc.aar"))
STARTER_MAPS = {"Map9002.aar": "verified_tutorial_map", "Map1026.aar": "verified_first_story_map", "Map1001.aar": "verified_second_story_map"}
STARTUP_MISSION_CATEGORIES = (0, 7)
MISSION_LISTS = tuple(mission_tables(category)[1] for category in STARTUP_MISSION_CATEGORIES)
STORY_PARTITION = re.compile(r"(?:BattleTalkEvent[0-9]{6,}\.aar|(?:QuestNameText|MessageText)[0-9]{6,}\.atb)\Z")
MAP_NAME = re.compile(r"Map(?:(?P<mission>[0-9]+)_)?(?P<map>[0-9]+)\.aar\Z")
RESOURCE_LITERAL = re.compile(rb"[A-Za-z0-9_./-]+\.(?:atb|ard|aar|atx|aft|ogg|nut|lua)\Z")
SCOPES = ("startup", "story", "maps", "all")


def _exact_literals(root: Path) -> dict[str, list[int]]:
    path = root / "Aigis_r.exe"
    if not path.is_file():
        return {}
    data = read_bounded(path, MAX_RESOURCE_BYTES, root=root)
    literals = defaultdict(list)
    for match in re.finditer(rb"[\x20-\x7e]+\x00", data):
        value = match[0][:-1]
        if RESOURCE_LITERAL.fullmatch(value):
            literals[value.decode("ascii")].append(match.start())
    return dict(literals)


def _locations(inventory: dict) -> dict[str, list[dict]]:
    entries = defaultdict(list)
    for item in inventory["entries"]:
        entries[item["filename_hash"]].append(item)
    return dict(entries)


def _matches(entry: ResourceEntry, candidates: list[dict], layer: str) -> list[dict]:
    return [{"layer": layer, "path": row["source_path"], "size": row["size"],
             "actual_md5": row["md5"]["actual"], "sidecar_status": row["md5"]["status"],
             "valid": row["size"] == entry.size and row["md5"]["actual"] == entry.md5}
            for row in candidates]


def _category(name: str) -> str:
    basename = PurePosixPath(name).name
    suffix = PurePosixPath(name).suffix.lower()
    if basename in STORY_PACKAGES or STORY_PARTITION.fullmatch(basename):
        return "story"
    if basename.startswith("Map") and suffix == ".aar":
        return "maps"
    if suffix in (".atb", ".ard"):
        return "tables"
    if suffix in (".ogg", ".wav", ".mp3", ".asn") or basename.startswith("Sound"):
        return "audio"
    if suffix in (".lua", ".nut"):
        return "scripts"
    if basename.startswith(("PlayerDot", "EnemyDot")):
        return "unit_graphics"
    if basename.startswith(("BattleEffect", "eff_", "efftex_")):
        return "effects"
    if suffix in (".atx", ".png", ".jpg", ".dds") or basename.startswith("Texture"):
        return "textures"
    if "Menu" in basename:
        return "ui"
    return "other"


def _mission_links(planned: dict[str, dict], asset_root: Path, overlay_root: Path, diagnostics: list[dict]) -> dict[int, list[dict]]:
    links = defaultdict(list)
    for name in MISSION_LISTS:
        item = planned.get(name)
        if item is None:
            diagnostics.append({"code": "mission_list_not_indexed", "name": name})
            continue
        candidates = [location for location in item["locations"] if location["valid"]]
        candidates.sort(key=lambda row: row["layer"] != "overlay")
        if not candidates:
            diagnostics.append({"code": "mission_list_not_available", "name": name})
            continue
        location = candidates[0]
        root = overlay_root if location["layer"] == "overlay" else asset_root
        try:
            data = read_bounded(root / location["path"], MAX_RESOURCE_BYTES, root=root)
            if len(data) != item["size"] or hashlib.md5(data, usedforsecurity=False).hexdigest() != item["md5"]:
                raise ValueError("Mission list changed after inventory")
            table = parse_table(decompress_all4(data) if data.startswith(b"ALL4") else data)
            rows = table["rows"]
            if not rows or any(type(row.get("QuestID")) is not int or type(row.get("MissionID")) is not int for row in rows):
                raise ValueError("Mission list does not contain verified quest and mission IDs")
            for row in rows:
                if row["QuestID"] > 0 and row["MissionID"] > 0:
                    link = {"quest_id": row["QuestID"], "mission_id": row["MissionID"], "source_table": name}
                    if link not in links[row["QuestID"]]:
                        links[row["QuestID"]].append(link)
        except (ValueError, OSError) as error:
            diagnostics.append({"code": "mission_list_invalid", "name": name, "message": str(error)})
    return dict(links)


def _quest_maps(quest_rows: list[dict] | None, links: dict[int, list[dict]]) -> dict[int, list[dict]]:
    maps = defaultdict(list)
    if quest_rows is None:
        return {}
    if not isinstance(quest_rows, list):
        raise ValueError("Quest rows must be a list")
    seen = set()
    for row in quest_rows:
        if not isinstance(row, dict) or type(row.get("QuestID")) is not int or row["QuestID"] <= 0 or type(row.get("MapNo")) is not int or row["MapNo"] < 0:
            raise ValueError("Quest rows require positive QuestID and nonnegative MapNo integers")
        quest_id, map_no = row["QuestID"], row["MapNo"]
        if quest_id in seen:
            raise ValueError("Quest rows contain duplicate QuestID values")
        seen.add(quest_id)
        if map_no > 0:
            maps[map_no].extend({**link, "map_no": map_no} for link in links.get(quest_id, ()))
    return dict(maps)


def _counts(items: Iterable[dict]) -> dict:
    result = {"total": 0, "total_bytes": 0, "available_source": 0, "available_overlay": 0,
              "missing": 0, "missing_bytes": 0, "stale": 0, "stale_bytes": 0,
              "download": 0, "download_bytes": 0}
    for item in items:
        result["total"] += 1
        result["total_bytes"] += item["size"]
        result[item["status"]] += 1
        if item["status"] in ("missing", "stale"):
            result[item["status"] + "_bytes"] += item["size"]
            result["download"] += 1
            result["download_bytes"] += item["size"]
    return result


def build_plan(entries: Iterable[ResourceEntry], asset_root: Path, overlay_root: Path, *, quest_rows: list[dict] | None = None) -> dict:
    asset_root = checked_path(asset_root)
    overlay_root = checked_path(overlay_root)
    if asset_root == overlay_root:
        raise ValueError("Source and overlay directories must be different")
    source_inventory = inventory_assets(asset_root)
    overlay_inventory = inventory_assets(overlay_root) if overlay_root.exists() else {"entries": [], "diagnostics": []}
    source_locations, overlay_locations = _locations(source_inventory), _locations(overlay_inventory)
    literals = _exact_literals(asset_root)
    planned = {}
    for entry in entries:
        locations = [*_matches(entry, overlay_locations.get(entry.cache_key, []), "overlay"),
                     *_matches(entry, source_locations.get(entry.cache_key, []), "source")]
        selected = next((row for row in locations if row["valid"]), None)
        status = "available_" + selected["layer"] if selected else "stale" if locations else "missing"
        item = {"name": entry.name, "group": entry.group, "md5": entry.md5, "flags": entry.flags,
                "size": entry.size, "cache_key": entry.cache_key, "status": status,
                "category": _category(entry.name), "scopes": [], "evidence": [], "locations": locations}
        if selected is not None:
            item["selected_path"] = selected["path"]
        native_key = getattr(entry, "native_cache_key", None)
        if native_key is not None:
            item["native_cache_key"] = native_key
        previous = planned.setdefault(entry.name, item)
        identity_fields = ("group", "md5", "flags", "size", "cache_key")
        if any(previous[field] != item[field] for field in identity_fields):
            raise ValueError("Resource index has conflicting metadata for one name")
        if native_key is not None:
            previous["native_cache_key"] = native_key
    diagnostics = [{"layer": "source", **item} for item in source_inventory["diagnostics"]]
    diagnostics.extend({"layer": "overlay", **item} for item in overlay_inventory["diagnostics"])
    links = _mission_links(planned, asset_root, overlay_root, diagnostics) if quest_rows is not None else {}
    quest_maps = _quest_maps(quest_rows, links)
    for item in planned.values():
        name = item["name"]
        basename = PurePosixPath(name).name
        suffix = PurePosixPath(name).suffix.lower()
        scopes = {"all"}
        evidence = [{"kind": "resource_index"}]
        if name in literals:
            scopes.add("startup")
            evidence.append({"kind": "executable_literal", "file_offsets": literals[name]})
        if suffix in (".atb", ".ard"):
            scopes.add("startup")
            evidence.append({"kind": "native_table"})
        if basename in STORY_PACKAGES:
            scopes.update(("startup", "story"))
            evidence.append({"kind": "verified_story_parent"})
        if STORY_PARTITION.fullmatch(basename):
            scopes.add("story")
            evidence.append({"kind": "native_mission_story_pattern", "function_va": "0x008cf420"})
        if basename.startswith("Map") and suffix == ".aar":
            scopes.add("maps")
            evidence.append({"kind": "indexed_map_archive"})
        if basename in STARTER_MAPS:
            scopes.add("startup")
            evidence.append({"kind": STARTER_MAPS[basename]})
        match = MAP_NAME.fullmatch(basename)
        if match:
            map_no = int(match["map"])
            mission = int(match["mission"]) if match["mission"] else None
            references = [row for row in quest_maps.get(map_no, ()) if mission is None or row["mission_id"] == mission]
            if references:
                scopes.add("startup")
                evidence.extend({"kind": "verified_mission_quest", **row} for row in references)
        item["scopes"] = [scope for scope in SCOPES if scope in scopes]
        item["evidence"] = evidence
    items = list(planned.values())
    summary = _counts(items)
    summary["by_status"] = {status: summary[status] for status in ("available_source", "available_overlay", "missing", "stale")}
    summary["by_category"] = {category: _counts(item for item in items if item["category"] == category) for category in sorted({item["category"] for item in items})}
    summary["by_scope"] = {scope: _counts(item for item in items if scope in item["scopes"]) for scope in SCOPES}
    return {"schema_version": 1, "asset_root": str(asset_root), "overlay_root": str(overlay_root),
            "entries": items, "summary": summary, "diagnostics": diagnostics}
