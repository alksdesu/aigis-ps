from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from aigis_ps.command_text_overlay import CLOSING_POLICY, CommandTextOverrides, load_command_text_overrides
from aigis_ps.client_versions import CURRENT_CLIENT_SHA256, LEGACY_CLIENT_SHA256, SUPPORTED_CLIENT_SHA256
from aigis_ps.errors import ProtocolError
from aigis_ps.event_pass_revival import EventPassRevivalRules, MISSION_RESOURCE, PASS_RESOURCE, load_revival_rules
from aigis_ps.event_login_catalog import RESOURCE_NAME as EVENT_LOGIN_RESOURCE
from aigis_ps.event_login_rules import EventLoginRules, load_event_login_rules
from aigis_ps.growth_campaign import CAMPAIGN_RESOURCE
from aigis_ps.mission_window_revival import (CAMPAIGN_FIELD, CAMPAIGN_WINDOW_POLICY, WINDOW_POLICY, MissionWindowRules,
                                             load_mission_window_rules)
from aigis_ps.purchased_login_rules import PurchasedLoginRules, load_purchased_login_rules
from aigis_ps.purchased_login_texts import purchased_package_keys
from aigis_ps.package_text_overlay import IMPLANT_RESOURCE, SYSTEM_TEXT_RESOURCE
from aigis_ps.local_io import exclusive_file, read_bounded, read_json_object
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.resource_fetch import MAX_INDEX_ENTRIES, PC_INDEX_NAME
from aigis_ps.resource_view import load_resource_view
from aigis_ps.shop_window_closure import (METADATA_FIELDS as SHOP_CLOSURE_FIELDS, PRODUCT_FIELDS as SHOP_CLOSURE_PRODUCTS,
                                          SCHEMA_VERSION as SHOP_CLOSURE_SCHEMA, TERM_FIELDS as SHOP_CLOSURE_TERMS,
                                          closure_terms)
from aigis_ps.trade_catalog import SHOP_RESOURCE


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
SOURCE_ROOT = WORKSPACE_ROOT / "aigisr_exe"
RUNTIME_ROOT = PROJECT_ROOT / "runtime" / "client"
EXECUTABLE = "Aigis_r.exe"
MANIFEST = "client-manifest.json"
ORIGINAL_PREFIX = b"https://millennium-war.net/"
LOCAL_PREFIX = b"http://127.0.0.1:18103/api/"
CDN_PREFIX = b"https://drc1bk94f7rq8.cloudfront.net/"
LOCAL_CDN_PREFIX = b"http://127.0.0.1:18103/client-assets/"
RESOURCE_ROOT = PROJECT_ROOT / "runtime" / "resources"
MASTER_RELATIVE = Path(PROJECT_ROOT.name, "runtime", "server", "masterdata")
HEALTH_URL = "http://127.0.0.1:18103/health"
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
SHOP_CLOSURE_RECORD = "shop_window_closure"
OVERLAY_RECORDS = ("event_pass_revival", "event_login_revival", "purchased_login", "mission_windows", "command_texts",
                   SHOP_CLOSURE_RECORD)
MISSION_WINDOW_FIELDS = frozenset(("schema_version", "enabled", "rules", "rules_hash", "window_policy", "tables", "batches"))
MISSION_TABLE_FIELDS = frozenset(("source_sha256", "effective_sha256"))
CAMPAIGN_RECORD_FIELDS = frozenset(("table", "window_policy", "source_sha256", "effective_sha256", "missions"))
COMMAND_TEXT_FIELDS = frozenset(("schema_version", "rules", "rules_hash", "policy", "tables", "rows", "entrust_gates"))


class ClientError(Exception):
    pass


@dataclass(frozen=True)
class ClientProfile:
    sha256: str
    size: int
    prefix_offset: int
    resources: tuple[tuple[str, str], ...]
    cdn_offset: int = 0xA6D690


LEGACY_PROFILE = ClientProfile(
    sha256=LEGACY_CLIENT_SHA256,
    size=13175680,
    prefix_offset=0xA6D674,
    resources=(
        (
            "bf9992bdbfda74126cef29e142e49641270090c297ba2ea305de07b759b2feee",
            "d83f0b5005dff88162e2b1acf0fd1a081c7389b92863102892ea141bc95735ff",
        ),
        (
            "d7f11563812edbb0a99d618cc74c51e7cb696ef049dfb559dbe50ff35a91cb4f",
            "8239764da9887c95d547ea3dd6779be9e5e171386e58ee5013c86d6179830c83",
        ),
        (
            "e7045fecc508227951e5d515df24587f6e31043139db580a3faf272d1436b59a",
            "28a6f7c3b6248c058bfa3f15a9022041234819a35a0ddd811b3842e695063e68",
        ),
        (
            "f00e83cfe03f32520fe10f877129ee6fc3eeb9b6bdd2790ec9403f1e380c59bb",
            "dcaf935fa117a5d9ca63d9bec13cd607b2cde4119b3446abf44de5f96a0b3510",
        ),
    ),
)
PROFILE = ClientProfile(
    sha256=CURRENT_CLIENT_SHA256,
    size=13175680,
    prefix_offset=0xA6D8F0,
    cdn_offset=0xA6D90C,
    resources=LEGACY_PROFILE.resources,
)


def select_profile(sha256: str) -> ClientProfile:
    for profile in (PROFILE, LEGACY_PROFILE):
        if sha256 == profile.sha256:
            return profile
    raise ClientError("Unknown or modified client version; source SHA256 does not match.")


def source_profile(source: Path, workspace: Path) -> ClientProfile:
    executable = checked_path(source / EXECUTABLE, workspace)
    data = read_bounded(executable, max(PROFILE.size, LEGACY_PROFILE.size), root=source)
    return select_profile(digest(data))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_path(path: Path, root: Path) -> Path:
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(root))
    if not path.is_relative_to(root):
        raise ClientError(f"Path must remain inside {root}: {path}")
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ClientError(f"Links and reparse points are not accepted: {part}")
    return path


def url_literal_inventory(data: bytes, hosts: tuple[str, ...]) -> list[dict]:
    inventory = []
    encodings = (
        ("ascii", rb"[\x21-\x7e]{8,}", 1),
        ("utf-16le", rb"(?:[\x21-\x7e]\x00){8,}", 2),
        ("utf-16be", rb"(?:\x00[\x21-\x7e]){8,}", 2),
    )
    for encoding, pattern, width in encodings:
        for match in re.finditer(pattern, data):
            text = match.group().decode(encoding)
            for url_match in re.finditer(r"https?://\S+", text, re.IGNORECASE):
                url = url_match.group()
                try:
                    hostname = urlsplit(url).hostname
                except ValueError:
                    continue
                if hostname in hosts:
                    inventory.append({
                        "offset": match.start() + url_match.start() * width,
                        "encoding": encoding,
                        "url": url,
                    })
    return sorted(inventory, key=lambda item: item["offset"])


def api_literal_inventory(data: bytes) -> list[dict]:
    return url_literal_inventory(data, ("millennium-war.net", "all.millennium-war.net"))


def cdn_literal_inventory(data: bytes) -> list[dict]:
    return url_literal_inventory(data, ("drc1bk94f7rq8.cloudfront.net",))


def patch_records(profile: ClientProfile) -> list[dict]:
    return [
        {"name": name, "offset": offset, "offset_hex": hex(offset),
         "original_hex": original.hex(), "replacement_hex": replacement.hex(),
         "original_url": original.decode("ascii"), "replacement_url": replacement.decode("ascii"),
         "preserved_nul_terminator": True}
        for name, offset, original, replacement in (
            ("api", profile.prefix_offset, ORIGINAL_PREFIX, LOCAL_PREFIX),
            ("cdn", profile.cdn_offset, CDN_PREFIX, LOCAL_CDN_PREFIX),
        )
    ]


def patch_executable(data: bytes, profile: ClientProfile | None = None) -> bytes:
    profile = select_profile(digest(data)) if profile is None else profile
    if len(data) != profile.size or digest(data) != profile.sha256:
        raise ClientError("Unknown or modified client version; source SHA256 does not match.")
    if data.count(ORIGINAL_PREFIX) != 1:
        raise ClientError("Expected exactly one original API prefix.")
    offset = profile.prefix_offset
    if data[offset : offset + len(ORIGINAL_PREFIX) + 1] != ORIGINAL_PREFIX + b"\0":
        raise ClientError("API prefix offset or NUL terminator does not match.")
    expected_inventory = [{"offset": offset, "encoding": "ascii", "url": ORIGINAL_PREFIX.decode("ascii")}]
    if api_literal_inventory(data) != expected_inventory:
        raise ClientError("Unaccounted API URL literal; this client requires a new audited patch profile.")
    if data.count(CDN_PREFIX) != 1:
        raise ClientError("Expected exactly one unmodified CDN prefix.")
    expected_cdn = [{"offset": profile.cdn_offset, "encoding": "ascii", "url": CDN_PREFIX.decode("ascii")}]
    if cdn_literal_inventory(data) != expected_cdn:
        raise ClientError("Unaccounted CDN URL literal or incorrect CDN offset.")
    patched = bytearray(data)
    previous_end = -1
    for record in sorted(patch_records(profile), key=lambda item: item["offset"]):
        original = bytes.fromhex(record["original_hex"])
        replacement = bytes.fromhex(record["replacement_hex"])
        offset = record["offset"]
        # Native constructors also use explicit 27-byte and 37-byte lengths.
        if len(replacement) != len(original):
            raise ClientError("Each replacement prefix must have the same byte length.")
        if offset < previous_end or data[offset:offset + len(original) + 1] != original + b"\0":
            raise ClientError("Prefix offset, range, or NUL terminator does not match.")
        patched[offset:offset + len(original)] = replacement
        previous_end = offset + len(original) + 1
    return bytes(patched)


def cache_files(cache: Path, workspace: Path) -> Iterator[Path]:
    cache = checked_path(cache, workspace)
    if not cache.is_dir():
        raise ClientError(f"Missing resource cache: {cache}")
    pending = [cache]
    while pending:
        for entry in pending.pop().iterdir():
            entry = checked_path(entry, workspace)
            if entry.is_dir():
                pending.append(entry)
            elif entry.is_file():
                yield entry
            else:
                raise ClientError(f"Unsupported cache entry: {entry}")


def inspect_source(source: Path, workspace: Path, profile: ClientProfile | None = None) -> dict:
    source = checked_path(source, workspace)
    profile = source_profile(source, workspace) if profile is None else profile
    executable = checked_path(source / EXECUTABLE, workspace)
    if not executable.is_file() or executable.stat().st_size != profile.size:
        raise ClientError("Unknown or modified client version; source size does not match.")
    source_data = read_bounded(executable, profile.size, root=source)
    patched = patch_executable(source_data, profile)
    files = []
    for name, expected in profile.resources:
        resource = checked_path(source / name, workspace)
        if not resource.is_file() or file_digest(resource) != expected:
            raise ClientError(f"Missing or modified bootstrap resource: {name}")
        files.append(resource)
    files.extend(cache_files(source / ".cache", workspace))
    return {
        "source": source,
        "patched": patched,
        "api_literal_inventory": api_literal_inventory(source_data),
        "cdn_literal_inventory": cdn_literal_inventory(source_data),
        "files": sorted(files, key=lambda path: path.relative_to(source).as_posix()),
    }


def patch_record(profile: ClientProfile) -> dict:
    return {
        "offset": profile.prefix_offset,
        "offset_hex": hex(profile.prefix_offset),
        "original_hex": ORIGINAL_PREFIX.hex(),
        "replacement_hex": LOCAL_PREFIX.hex(),
        "original_url": ORIGINAL_PREFIX.decode("ascii"),
        "replacement_url": LOCAL_PREFIX.decode("ascii"),
        "preserved_nul_terminator": True,
        "cdn_url": CDN_PREFIX.decode("ascii"),
    }


def _verify_revival_metadata(manifest, indexed):
    if "event_pass_revival" not in manifest:
        return
    record = manifest["event_pass_revival"]
    try:
        if (not isinstance(record, dict) or set(record) != {"schema_version", "rules", "rules_hash",
                "source_hashes", "effective_hashes", "metadata"} or type(record["schema_version"]) is not int
                or record["schema_version"] != 1 or not isinstance(record["rules"], dict)):
            raise ValueError("Invalid revival metadata")
        rules = EventPassRevivalRules(**record["rules"])
        if rules.to_dict() != record["rules"] or rules.content_version != record["rules_hash"]:
            raise ValueError("Revival rules changed")
        inventory = {item["logical_name"]: item for item in indexed}
        for key in ("source_hashes", "effective_hashes"):
            values = record[key]
            if (not isinstance(values, dict) or set(values) != {PASS_RESOURCE, MISSION_RESOURCE}
                    or any(type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None for value in values.values())):
                raise ValueError("Invalid revival resource hashes")
        if any(inventory.get(name, {}).get("target_sha256") != value for name, value in record["effective_hashes"].items()):
            raise ValueError("Revival metadata does not match prepared resources")
        summary = record["metadata"]
        if (not isinstance(summary, dict) or set(summary) != {"enabled", "pass_id", "pass_source_index",
                "reward_count", "start_at", "end_at", "display_text", "mission_ids", "daily_points", "policy"}
                or summary["enabled"] is not True or summary["pass_id"] != rules.pass_id
                or summary["display_text"] != rules.display_text or summary["mission_ids"] != list(rules.mission_points)
                or type(summary["daily_points"]) is not int or summary["daily_points"] != sum(rules.mission_points.values())
                or summary["policy"] != "explicit_local_revival"
                or type(summary["pass_source_index"]) is not int or summary["pass_source_index"] < 0
                or type(summary["reward_count"]) is not int or summary["reward_count"] <= 0
                or type(summary["start_at"]) is not str or type(summary["end_at"]) is not str
                or rules.start_at is not None and rules.start_at != summary["start_at"]
                or rules.end_at is not None and rules.end_at != summary["end_at"]):
            raise ValueError("Invalid revival summary")
        window = EventPassRevivalRules(rules.pass_id, rules.mission_points, summary["start_at"], summary["end_at"])
        if window.start_at != summary["start_at"] or window.end_at != summary["end_at"]:
            raise ValueError("Invalid revival window")
    except (KeyError, TypeError, ValueError) as error:
        raise ClientError("Runtime event pass revival metadata is invalid or inconsistent.") from error


def _check_requested_revival(manifest, rules):
    if rules is not None and (not isinstance(rules, EventPassRevivalRules)
            or manifest.get("event_pass_revival", {}).get("rules") != rules.to_dict()):
        raise ClientError("Requested event pass revival differs from the prepared client.")


def _verify_event_login_metadata(manifest, indexed):
    if "event_login_revival" not in manifest:
        return
    record = manifest["event_login_revival"]
    try:
        if (not isinstance(record, dict) or set(record) != {"schema_version", "rules", "rules_hash", "campaign_ids",
                "source_sha256", "effective_sha256", "window_policy"}
                or type(record["schema_version"]) is not int or record["schema_version"] != 1):
            raise ValueError("Invalid event login metadata")
        rules = EventLoginRules(**record["rules"])
        if (not rules.enabled or not rules.revivals or rules.to_dict() != record["rules"]
                or rules.content_version != record["rules_hash"]
                or record["campaign_ids"] != list(rules.revivals)
                or record["window_policy"] != "explicit_local_eligibility_start_inclusive_end_exclusive"):
            raise ValueError("Invalid event login rules")
        for field in ("source_sha256", "effective_sha256"):
            if type(record[field]) is not str or re.fullmatch(r"[a-f0-9]{64}", record[field]) is None:
                raise ValueError("Invalid event login resource hashes")
        inventory = {item["logical_name"]: item for item in indexed}
        if inventory.get(EVENT_LOGIN_RESOURCE, {}).get("target_sha256") != record["effective_sha256"]:
            raise ValueError("Event login metadata differs from prepared resources")
    except (KeyError, TypeError, ValueError) as error:
        raise ClientError("Runtime event login revival metadata is invalid or inconsistent.") from error


def _check_requested_event_login(manifest, rules):
    if rules is None:
        return
    if not isinstance(rules, EventLoginRules):
        raise ClientError("Requested event login rules are invalid.")
    expected = rules.to_dict() if rules.enabled and rules.revivals else None
    if manifest.get("event_login_revival", {}).get("rules") != expected:
        raise ClientError("Requested event login revival differs from the prepared client.")


def _verify_purchased_login_metadata(manifest, indexed):
    if "purchased_login" not in manifest:
        return
    record = manifest["purchased_login"]
    try:
        if (not isinstance(record, dict) or set(record) != {"schema_version", "rules", "rules_hash", "campaign_ids",
                "source_hashes", "effective_hashes", "text_indices"}
                or type(record["schema_version"]) is not int or record["schema_version"] != 1):
            raise ValueError("Invalid purchased login metadata")
        rules = PurchasedLoginRules(**record["rules"])
        identifiers = record["campaign_ids"]
        if (not rules.enabled or rules.to_dict() != record["rules"] or rules.content_version != record["rules_hash"]
                or not isinstance(identifiers, list) or len(identifiers) > 256
                or any(type(value) is not int or value <= 0 for value in identifiers)
                or identifiers != sorted(set(identifiers))
                or rules.campaign_ids is not None and identifiers != list(rules.campaign_ids)):
            raise ValueError("Invalid purchased login rules")
        expected = {name for identifier in identifiers for name in purchased_package_keys(identifier)}
        indices = record["text_indices"]
        if (not isinstance(indices, dict) or set(indices) != expected
                or any(type(value) is not int or not 0 <= value < 65535 for value in indices.values())):
            raise ValueError("Invalid purchased login text references")
        inventory = {item["logical_name"]: item for item in indexed}
        for field in ("source_hashes", "effective_hashes"):
            hashes = record[field]
            if (not isinstance(hashes, dict) or set(hashes) != {IMPLANT_RESOURCE, SYSTEM_TEXT_RESOURCE}
                    or any(type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None for value in hashes.values())):
                raise ValueError("Invalid purchased login text hashes")
        if any(inventory.get(name, {}).get("target_sha256") != digest for name, digest in record["effective_hashes"].items()):
            raise ValueError("Purchased login text differs from prepared resources")
    except (KeyError, TypeError, ValueError) as error:
        raise ClientError("Runtime purchased login metadata is invalid or inconsistent.") from error


def _check_requested_purchased_login(manifest, rules):
    if rules is None:
        return
    if not isinstance(rules, PurchasedLoginRules):
        raise ClientError("Requested purchased login rules are invalid.")
    if manifest.get("purchased_login", {}).get("rules") != (rules.to_dict() if rules.enabled else None):
        raise ClientError("Requested purchased login rules differ from the prepared client.")


def _verify_mission_window_metadata(manifest, indexed):
    if "mission_windows" not in manifest:
        return
    record = manifest["mission_windows"]
    try:
        if (not isinstance(record, dict) or set(record) - {CAMPAIGN_FIELD} != MISSION_WINDOW_FIELDS
                or type(record["schema_version"]) is not int or record["schema_version"] != 1):
            raise ValueError("Invalid mission window metadata")
        rules = MissionWindowRules(**record["rules"])
        if (not rules.enabled or not (rules.batches or rules.growth_campaigns) or rules.to_dict() != record["rules"]
                or rules.content_version != record["rules_hash"] or record["enabled"] is not True
                or record["window_policy"] != WINDOW_POLICY):
            raise ValueError("Invalid mission window rules")
        batches = record["batches"]
        if (not isinstance(batches, list) or not all(isinstance(batch, dict) for batch in batches)
                or [batch.get("key") for batch in batches] != list(rules.batches)
                or any(batch.get("window") != rules.batches[batch["key"]].to_dict() for batch in batches)):
            raise ValueError("Invalid mission window batches")
        tables = record["tables"]
        if (not isinstance(tables, dict) or set(tables) != {batch.get("table") for batch in batches}
                or any(not isinstance(hashes, dict) or set(hashes) != MISSION_TABLE_FIELDS
                       or any(type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None
                              for value in hashes.values()) for hashes in tables.values())):
            raise ValueError("Invalid mission window resource hashes")
        inventory = {item["logical_name"]: item for item in indexed}
        if any(inventory.get(name, {}).get("target_sha256") != hashes["effective_sha256"]
               for name, hashes in tables.items()):
            raise ValueError("Mission window metadata differs from prepared resources")
        campaigns = record.get(CAMPAIGN_FIELD)
        if (campaigns is None) != (not rules.growth_campaigns):
            raise ValueError("Invalid growth campaign records")
        if campaigns is not None:
            if (not isinstance(campaigns, dict) or set(campaigns) != CAMPAIGN_RECORD_FIELDS
                    or campaigns["table"] != CAMPAIGN_RESOURCE or campaigns["window_policy"] != CAMPAIGN_WINDOW_POLICY
                    or any(type(campaigns[name]) is not str or re.fullmatch(r"[a-f0-9]{64}", campaigns[name]) is None
                           for name in MISSION_TABLE_FIELDS)):
                raise ValueError("Invalid growth campaign records")
            missions = campaigns["missions"]
            if (not isinstance(missions, list) or not all(isinstance(entry, dict) for entry in missions)
                    or [str(entry.get("mission_id")) for entry in missions] != list(rules.growth_campaigns)
                    or any(entry.get("window") != rules.growth_campaigns[str(entry["mission_id"])].to_dict()
                           for entry in missions)):
                raise ValueError("Invalid growth campaign missions")
            if inventory.get(CAMPAIGN_RESOURCE, {}).get("target_sha256") != campaigns["effective_sha256"]:
                raise ValueError("Growth campaign windows differ from prepared resources")
    except (KeyError, TypeError, ValueError) as error:
        raise ClientError("Runtime mission window metadata is invalid or inconsistent.") from error


def _check_requested_mission_windows(manifest, rules):
    if rules is None:
        return
    if not isinstance(rules, MissionWindowRules):
        raise ClientError("Requested mission window rules are invalid.")
    expected = rules.to_dict() if rules.enabled and (rules.batches or rules.growth_campaigns) else None
    if manifest.get("mission_windows", {}).get("rules") != expected:
        raise ClientError("Requested mission windows differ from the prepared client.")


def _verify_command_text_metadata(manifest, indexed):
    if "command_texts" not in manifest:
        return
    record = manifest["command_texts"]
    try:
        if (not isinstance(record, dict) or set(record) != COMMAND_TEXT_FIELDS
                or type(record["schema_version"]) is not int or record["schema_version"] != 1
                or not isinstance(record["entrust_gates"], list)):
            raise ValueError("Invalid command text metadata")
        rules = CommandTextOverrides(**record["rules"])
        if (not rules.tables or rules.to_dict() != record["rules"] or rules.content_version != record["rules_hash"]
                or record["policy"] != CLOSING_POLICY):
            raise ValueError("Invalid command text rules")
        rows = record["rows"]
        configured = [(name, key) for name, texts in rules.tables.items() for key in texts]
        if (not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows)
                or [(row.get("table"), row.get("key")) for row in rows] != configured):
            raise ValueError("Invalid command text rows")
        tables = record["tables"]
        if (not isinstance(tables, dict) or set(tables) != set(rules.tables)
                or any(not isinstance(hashes, dict) or set(hashes) != MISSION_TABLE_FIELDS
                       or any(type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None
                              for value in hashes.values()) for hashes in tables.values())):
            raise ValueError("Invalid command text resource hashes")
        inventory = {item["logical_name"]: item for item in indexed}
        if any(inventory.get(name, {}).get("target_sha256") != hashes["effective_sha256"]
               for name, hashes in tables.items()):
            raise ValueError("Command text metadata differs from prepared resources")
    except (KeyError, TypeError, ValueError) as error:
        raise ClientError("Runtime command text metadata is invalid or inconsistent.") from error


def _check_requested_command_texts(manifest, rules):
    if rules is None:
        return
    if not isinstance(rules, CommandTextOverrides):
        raise ClientError("Requested command text overrides are invalid.")
    expected = rules.to_dict() if rules.tables else None
    if manifest.get("command_texts", {}).get("rules") != expected:
        raise ClientError("Requested command text overrides differ from the prepared client.")


def _verify_shop_window_closure_metadata(manifest, indexed):
    if SHOP_CLOSURE_RECORD not in manifest:
        return
    record = manifest[SHOP_CLOSURE_RECORD]
    try:
        if (not isinstance(record, dict) or set(record) != SHOP_CLOSURE_FIELDS
                or type(record["schema_version"]) is not int or record["schema_version"] != SHOP_CLOSURE_SCHEMA
                or {name: record[name] for name in SHOP_CLOSURE_TERMS} != closure_terms()):
            raise ValueError("Invalid shop window closure metadata")
        products = record["products"]
        if (not isinstance(products, list) or not products
                or any(not isinstance(product, dict) or set(product) != SHOP_CLOSURE_PRODUCTS
                       or type(product["shop_id"]) is not int or product["shop_id"] <= 0 for product in products)
                or len({product["shop_id"] for product in products}) != len(products)):
            raise ValueError("Invalid shop window closure products")
        if any(type(record[field]) is not str or re.fullmatch(r"[a-f0-9]{64}", record[field]) is None
               for field in ("source_sha256", "effective_sha256")):
            raise ValueError("Invalid shop window closure resource hashes")
        inventory = {item["logical_name"]: item for item in indexed}
        if inventory.get(SHOP_RESOURCE, {}).get("target_sha256") != record["effective_sha256"]:
            raise ValueError("Shop window closure metadata differs from prepared resources")
    except (KeyError, TypeError, ValueError) as error:
        raise ClientError("Runtime shop window closure metadata is invalid or inconsistent.") from error


def verify_runtime(runtime: Path, workspace: Path, profile: ClientProfile | None = None, *, allow_legacy: bool = False) -> dict:
    runtime = checked_path(runtime, workspace)
    manifest_path = checked_path(runtime / MANIFEST, workspace)
    try:
        manifest = read_json_object(manifest_path, limit=MAX_MANIFEST_BYTES, root=runtime)
    except (OSError, ValueError, RecursionError) as error:
        raise ClientError("Runtime has no valid preparation manifest; nothing was overwritten.") from error
    profile = select_profile(manifest.get("source_sha256")) if profile is None else profile
    legacy = type(manifest.get("schema_version")) is int and manifest["schema_version"] == 1
    if type(manifest.get("schema_version")) is not int or manifest.get("source_sha256") != profile.sha256 or not (
        legacy and allow_legacy and manifest.get("patch") == patch_record(profile)
        or manifest.get("schema_version") == 2 and manifest.get("patches") == patch_records(profile)
    ):
        raise ClientError("Runtime manifest belongs to an unknown client or patch.")
    executable = checked_path(runtime / EXECUTABLE, workspace)
    if executable.stat().st_nlink != 1:
        raise ClientError("The runtime executable must be an independent file.")
    if executable.stat().st_size != profile.size:
        raise ClientError("The runtime executable has been modified.")
    data = read_bounded(executable, profile.size, root=runtime)
    restored = bytearray(data)
    records = patch_records(profile)[:1] if legacy else patch_records(profile)
    for record in records:
        offset = record["offset"]
        replacement = bytes.fromhex(record["replacement_hex"])
        if data[offset:offset + len(replacement) + 1] != replacement + b"\0":
            raise ClientError("The runtime API/CDN prefix has been modified.")
        restored[offset:offset + len(replacement)] = bytes.fromhex(record["original_hex"])
    expected = patch_executable(bytes(restored), profile)
    if legacy:
        offset = profile.cdn_offset
        expected = expected[:offset] + CDN_PREFIX + expected[offset + len(CDN_PREFIX):]
    if expected != data or digest(data) != manifest.get("target_sha256"):
        raise ClientError("The runtime executable has been modified.")
    if manifest.get("api_literal_inventory") != api_literal_inventory(bytes(restored)):
        raise ClientError("The runtime API literal inventory is missing or has been modified.")
    if not legacy:
        if manifest.get("cdn_literal_inventory") != cdn_literal_inventory(bytes(restored)):
            raise ClientError("The runtime CDN literal inventory is missing or has been modified.")
        snapshot = manifest.get("resource_snapshot")
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("version"), str) or not re.fullmatch(r"[a-f0-9]{40}", snapshot["version"]):
            raise ClientError("The runtime resource snapshot is missing or invalid.")
        if not isinstance(snapshot.get("index_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", snapshot["index_sha256"]):
            raise ClientError("The runtime resource index hash is invalid.")
        if snapshot.get("snapshot") != snapshot["version"] + "-" + snapshot["index_sha256"][:16]:
            raise ClientError("The runtime resource snapshot identity is invalid.")
        if type(snapshot.get("entries")) is not int or not 0 < snapshot["entries"] <= MAX_INDEX_ENTRIES:
            raise ClientError("The runtime resource count is invalid.")
        if snapshot.get("index_name") != PC_INDEX_NAME or type(snapshot.get("total_bytes")) is not int or snapshot["total_bytes"] < 0:
            raise ClientError("The runtime resource index metadata is invalid.")
        files = manifest.get("files")
        if not isinstance(files, list) or len(files) > 2 * MAX_INDEX_ENTRIES + 100_000:
            raise ClientError("The runtime file inventory is invalid.")
        names = set()
        indexed = []
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ClientError("The runtime file inventory contains an invalid entry.")
            relative = item["path"]
            if not relative or any(part in ("", ".", "..") for part in relative.split("/")) or any(char in relative for char in ("\\", ":")) or relative.casefold() in names:
                raise ClientError("The runtime file inventory contains an unsafe or duplicate path.")
            names.add(relative.casefold())
            if item.get("kind") == "indexed-resource":
                logical_name = item.get("logical_name")
                if not isinstance(logical_name, str) or relative != ".cache/" + digest(logical_name.encode("utf-8")):
                    raise ClientError("The runtime resource cache key is invalid.")
                if type(item.get("size")) is not int or not 0 <= item["size"] <= 64 * 1024 * 1024:
                    raise ClientError("The runtime resource size is invalid.")
                for field, length in (("md5", 32), ("target_sha256", 64)):
                    if not isinstance(item.get(field), str) or not re.fullmatch(r"[a-f0-9]{" + str(length) + "}", item[field]):
                        raise ClientError("The runtime resource hash is invalid.")
                indexed.append(item)
        if len(indexed) != snapshot["entries"] or sum(item["size"] for item in indexed) != snapshot["total_bytes"]:
            raise ClientError("The runtime file inventory does not cover its resource snapshot.")
        _verify_revival_metadata(manifest, indexed)
        _verify_event_login_metadata(manifest, indexed)
        _verify_purchased_login_metadata(manifest, indexed)
        _verify_mission_window_metadata(manifest, indexed)
        _verify_command_text_metadata(manifest, indexed)
        _verify_shop_window_closure_metadata(manifest, indexed)
    for name, expected in profile.resources:
        resource = checked_path(runtime / name, workspace)
        if resource.stat().st_nlink != 1 or file_digest(resource) != expected:
            raise ClientError(f"Runtime bootstrap resource was modified or linked: {name}")
    for candidate in cache_files(runtime / ".cache", workspace):
        if candidate.stat().st_nlink != 1:
            raise ClientError(f"The runtime cache must contain independent files: {candidate}")
    return manifest


def resource_snapshot(manifest: dict, entries) -> dict:
    return {"version": manifest["version"], "snapshot": manifest["snapshot"],
            "index_sha256": manifest["index_sha256"], "index_name": manifest["index_name"],
            "entries": len(entries), "total_bytes": sum(entry.size for entry in entries)}


def _resource_view(resource_root, source, workspace, profile, master_root, revival_rules=None, event_login_rules=None,
                   purchased_login_rules=None, mission_window_rules=None, command_text_overrides=None):
    if master_root is not None:
        master_root = checked_path(master_root, workspace)

    def master_provider(name):
        directory = master_root if master_root is not None else workspace / MASTER_RELATIVE
        return MasterDataStore(checked_path(directory, workspace), expected_hash=profile.sha256).get_xml(name)

    return load_resource_view(resource_root, source, profile.sha256, master_provider=master_provider,
                              revival_rules=revival_rules, event_login_rules=event_login_rules,
                              purchased_login_rules=purchased_login_rules, mission_window_rules=mission_window_rules,
                              command_text_overrides=command_text_overrides)


def _overlay_records(view) -> dict:
    windows = view.mission_window_metadata
    texts = view.command_text_metadata
    closure = view.shop_window_closure_metadata
    records = {"event_pass_revival": view.revival_metadata, "event_login_revival": view.event_login_metadata,
               "purchased_login": view.purchased_login_metadata,
               # Rules that configure no window and no row patch nothing, so the runtime keeps no record of them.
               "mission_windows": windows if windows is not None and (windows["batches"] or CAMPAIGN_FIELD in windows)
                                  else None,
               "command_texts": texts if texts is not None and texts["rows"] else None,
               SHOP_CLOSURE_RECORD: closure if closure is not None and closure["products"] else None}
    return {name: record for name, record in records.items() if record is not None}


def _recorded(document: dict) -> dict:
    return {name: document[name] for name in OVERLAY_RECORDS if name in document}


def preparation_plan(source: Path, resource_root: Path, workspace: Path, profile: ClientProfile,
                     *, master_root: Path | None = None, revival_rules: EventPassRevivalRules | None = None,
                     event_login_rules: EventLoginRules | None = None,
                     purchased_login_rules: PurchasedLoginRules | None = None,
                     mission_window_rules: MissionWindowRules | None = None,
                     command_text_overrides: CommandTextOverrides | None = None) -> dict:
    resource_root = checked_path(resource_root, workspace)
    inspected = inspect_source(source, workspace, profile)
    source = inspected["source"]
    view = _resource_view(resource_root, source, workspace, profile, master_root, revival_rules, event_login_rules,
                          purchased_login_rules, mission_window_rules, command_text_overrides)
    entries = view.entries
    indexed_paths = {".cache/" + entry.cache_key + suffix for entry in entries for suffix in ("", ".md5")}
    inspected["files"] = [path for path in inspected["files"] if path.relative_to(source).as_posix() not in indexed_paths]
    for entry in entries:
        if entry.size > 64 * 1024 * 1024:
            raise ClientError(f"Indexed resource exceeds the verified read limit: {entry.name}")
        if entry.name in view.replacements:
            continue
        candidates = (resource_root / ".cache" / entry.cache_key, source / ".cache" / entry.cache_key, source / entry.cache_key)
        if not any(checked_path(path, workspace).is_file() and path.stat().st_size == entry.size for path in candidates):
            raise ClientError(f"Indexed resource is missing or has the wrong size: {entry.name}")
    inspected.update(resource_snapshot=resource_snapshot(view.manifest, entries), entries=entries, resource_view=view,
                     resource_root=resource_root, bytes_to_copy=len(inspected["patched"]) +
                     sum(path.stat().st_size for path in inspected["files"]) + sum(entry.size + 37 for entry in entries))
    inspected.update(_overlay_records(view))
    return inspected


def _separate_directories(source: Path, runtime: Path, resource_root: Path) -> None:
    for first, second in ((source, runtime), (resource_root, runtime), (source, resource_root)):
        if first.is_relative_to(second) or second.is_relative_to(first):
            raise ClientError("Source, runtime, and resource library directories must be separate.")


def copy_verified(original: Path, destination: Path) -> str:
    source_hash = file_digest(original)
    shutil.copyfile(original, destination)
    if source_hash != file_digest(destination) or source_hash != file_digest(original):
        raise ClientError(f"Source changed while copying: {original.name}")
    return source_hash


def _publish(staging: Path, runtime: Path, existing: dict | None, workspace: Path) -> Path | None:
    backup = None
    if existing is not None:
        backup = checked_path(runtime.with_name(runtime.name + ".backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]), workspace)
        runtime.rename(backup)
    elif runtime.exists():
        raise ClientError("Runtime appeared during preparation; nothing was overwritten.")
    try:
        staging.rename(runtime)
    except BaseException:
        if backup is not None and not runtime.exists():
            backup.rename(runtime)
        raise
    return backup


def prepare(
    source: Path,
    runtime: Path,
    workspace: Path = WORKSPACE_ROOT,
    profile: ClientProfile | None = None,
    *,
    resource_root: Path = RESOURCE_ROOT,
    master_root: Path | None = None,
    revival_rules: EventPassRevivalRules | None = None,
    event_login_rules: EventLoginRules | None = None,
    purchased_login_rules: PurchasedLoginRules | None = None,
    mission_window_rules: MissionWindowRules | None = None,
    command_text_overrides: CommandTextOverrides | None = None,
    refresh: bool = False,
    progress=None,
) -> dict:
    source = checked_path(source, workspace)
    runtime = checked_path(runtime, workspace)
    resource_root = checked_path(resource_root, workspace)
    _separate_directories(source, runtime, resource_root)
    if not resource_root.is_dir():
        raise ClientError("A completed local resource library is required.")
    profile = source_profile(source, workspace) if profile is None else profile
    with exclusive_file(resource_root / "operation.lock"), exclusive_file(runtime.parent / f".{runtime.name}.prepare.lock"):
        return _prepare(source, runtime, workspace, profile, resource_root, refresh, progress,
                        master_root=master_root, revival_rules=revival_rules, event_login_rules=event_login_rules,
                        purchased_login_rules=purchased_login_rules, mission_window_rules=mission_window_rules,
                        command_text_overrides=command_text_overrides)


def _prepare(source, runtime, workspace, profile, resource_root, refresh, progress, *, master_root=None,
             revival_rules=None, event_login_rules=None, purchased_login_rules=None, mission_window_rules=None,
             command_text_overrides=None):
    existing_profile = None if profile.sha256 in SUPPORTED_CLIENT_SHA256 else profile
    existing = verify_runtime(runtime, workspace, existing_profile, allow_legacy=True) if runtime.exists() else None
    inspected = preparation_plan(source, resource_root, workspace, profile, master_root=master_root,
                                 revival_rules=revival_rules, event_login_rules=event_login_rules,
                                 purchased_login_rules=purchased_login_rules, mission_window_rules=mission_window_rules,
                                 command_text_overrides=command_text_overrides)
    snapshot = inspected["resource_snapshot"]
    if (existing is not None and existing.get("source_sha256") == profile.sha256
            and existing.get("schema_version") == 2 and existing.get("resource_snapshot") == snapshot
            and _recorded(existing) == _recorded(inspected) and not refresh):
        current = _resource_view(resource_root, source, workspace, profile, master_root, revival_rules, event_login_rules,
                                 purchased_login_rules, mission_window_rules, command_text_overrides)
        if (resource_snapshot(current.manifest, current.entries) != snapshot
                or _overlay_records(current) != _recorded(inspected)):
            raise ClientError("The active resource index changed during preparation.")
        return {"status": "already-prepared", "runtime": str(runtime), "manifest": existing}
    runtime.parent.mkdir(parents=True, exist_ok=True)
    if inspected["bytes_to_copy"] + 2 * 1024 ** 3 > shutil.disk_usage(runtime.parent).free:
        raise ClientError("Preparation would leave less than 2 GiB of free disk space.")
    staging = runtime.parent / f".{runtime.name}.prepare-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        patched = inspected["patched"]
        (staging / EXECUTABLE).write_bytes(patched)
        (staging / ".cache").mkdir()
        inventory = [{
            "path": EXECUTABLE,
            "size": len(patched),
            "source_sha256": profile.sha256,
            "target_sha256": digest(patched),
            "kind": "patched-executable",
        }]
        for original in inspected["files"]:
            checked_path(original, workspace)
            relative = original.relative_to(source)
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_hash = copy_verified(original, destination)
            inventory.append({
                "path": relative.as_posix(),
                "size": destination.stat().st_size,
                "source_sha256": source_hash,
                "target_sha256": source_hash,
                "kind": "resource-cache" if relative.parts[0] == ".cache" else "bootstrap",
            })
        service = inspected["resource_view"].assets
        completed_bytes = 0
        if progress:
            progress({"event": "copying_resources", **snapshot})
        for number, entry in enumerate(inspected["entries"], 1):
            try:
                content, content_hash = service.read(entry.cache_key)
            except ProtocolError as error:
                raise ClientError(f"Indexed resource verification failed: {entry.name} ({error.code})") from error
            relative = ".cache/" + entry.cache_key
            destination = staging / relative
            with destination.open("xb") as stream:
                stream.write(content)
            if file_digest(destination) != content_hash:
                raise ClientError(f"Copied resource verification failed: {entry.name}")
            sidecar = b"\xef\xbb\xbf" + entry.md5.encode("ascii") + b"\r\n"
            with destination.with_suffix(".md5").open("xb") as stream:
                stream.write(sidecar)
            inventory.append({"path": relative, "size": len(content), "target_sha256": content_hash,
                              "md5": entry.md5, "logical_name": entry.name, "kind": "indexed-resource"})
            inventory.append({"path": relative + ".md5", "size": len(sidecar), "target_sha256": digest(sidecar),
                              "kind": "resource-md5"})
            completed_bytes += len(content)
            if progress and (number % 100 == 0 or number == len(inspected["entries"])):
                progress({"event": "resources_copied", "completed": number, "total": len(inspected["entries"]),
                          "bytes": completed_bytes})
        if file_digest(source / EXECUTABLE) != profile.sha256:
            raise ClientError("Source executable changed during preparation.")
        current = _resource_view(resource_root, source, workspace, profile, master_root, revival_rules, event_login_rules,
                                 purchased_login_rules, mission_window_rules, command_text_overrides)
        if (resource_snapshot(current.manifest, current.entries) != snapshot
                or _overlay_records(current) != _recorded(inspected)):
            raise ClientError("The active resource index changed during preparation.")
        if existing is not None and verify_runtime(runtime, workspace, existing_profile, allow_legacy=True) != existing:
            raise ClientError("The existing runtime changed during preparation.")
        manifest = {
            "schema_version": 2,
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "source_directory": str(source),
            "source_sha256": profile.sha256,
            "target_sha256": digest(patched),
            "patches": patch_records(profile),
            "api_literal_inventory": inspected["api_literal_inventory"],
            "cdn_literal_inventory": inspected["cdn_literal_inventory"],
            "resource_snapshot": snapshot,
            "resource_directory": str(resource_root),
            "cache_validation": "Size, MD5, and SHA256 at copy time; the runtime cache remains mutable.",
            "files": inventory,
            "root_copy_policy": "Only the executable and four known bootstrap resources; no credentials.",
            **_recorded(inspected),
        }
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        verify_runtime(staging, workspace, profile)
        backup = _publish(staging, runtime, existing, workspace)
        return {"status": "prepared", "runtime": str(runtime), "manifest": manifest,
                "backup": str(backup) if backup is not None else None}
    finally:
        if staging.exists():
            checked_path(staging, workspace)
            if staging.resolve().is_relative_to(workspace.resolve()) and staging.name.startswith(f".{runtime.name}.prepare-"):
                shutil.rmtree(staging)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        raise ClientError("The local health endpoint must not redirect.")


def check_server(*, resource_version: str | None = None) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(HEALTH_URL, timeout=3) as response:
        body = response.read(65537)
        if response.status != 200 or len(body) > 65536:
            raise ClientError("The local server did not return a valid health response.")
    try:
        health = json.loads(body)
    except (ValueError, RecursionError) as error:
        raise ClientError("The local server health response is not JSON.") from error
    if not isinstance(health, dict) or health.get("service") != "aigis-ps":
        raise ClientError("The process listening on port 18103 is not the expected Aigis server.")
    if resource_version is not None:
        native = health.get("native_cdn")
        if not isinstance(native, dict) or native.get("ready") is not True or native.get("path") != "/client-assets/":
            raise ClientError("The local native resource service is not ready.")
        if native.get("version") != resource_version:
            raise ClientError("The server resource version differs from the prepared client.")


def launch(runtime: Path, *, dry_run: bool = False, revival_rules: EventPassRevivalRules | None = None,
           event_login_rules: EventLoginRules | None = None, purchased_login_rules: PurchasedLoginRules | None = None,
           mission_window_rules: MissionWindowRules | None = None,
           command_text_overrides: CommandTextOverrides | None = None) -> dict:
    manifest = verify_runtime(runtime, WORKSPACE_ROOT)
    _check_requested_revival(manifest, revival_rules)
    _check_requested_event_login(manifest, event_login_rules)
    _check_requested_purchased_login(manifest, purchased_login_rules)
    _check_requested_mission_windows(manifest, mission_window_rules)
    _check_requested_command_texts(manifest, command_text_overrides)
    command = [str(runtime / EXECUTABLE), "/viewer_id=1", "/onetime_token=local-only"]
    if dry_run:
        return {"status": "verified", "command": command, "working_directory": str(runtime)}
    if os.name != "nt":
        raise ClientError("The PC client can only be started on Windows.")
    check_server(resource_version=manifest["resource_snapshot"]["version"])
    process = subprocess.Popen(command, cwd=runtime, close_fds=True)
    return {"status": "started", "pid": process.pid, "runtime": str(runtime)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare and verify an isolated local Aigis client.")
    parser.add_argument("action", choices=("prepare", "inspect", "verify", "launch"))
    parser.add_argument("--source", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--runtime", type=Path, default=RUNTIME_ROOT)
    parser.add_argument("--resource-dir", type=Path, default=RESOURCE_ROOT)
    parser.add_argument("--master-dir", type=Path, help="Verified public master directory for local resource templates")
    parser.add_argument("--event-pass-revival", type=Path, help="Explicit local event pass resource revival rules")
    parser.add_argument("--event-login-rules", type=Path, help="Public event login selection and fixed revival windows")
    parser.add_argument("--purchased-login-rules", type=Path, help="Local free packages granting daily login rewards")
    parser.add_argument("--mission-windows", type=Path, help="Local mission batch windows patched into served resources")
    parser.add_argument("--command-text-overrides", type=Path,
                        help="Local command text cells patched into served resources; gates only close further")
    parser.add_argument("--refresh", action="store_true", help="Build a fresh cache and preserve the existing verified runtime as a backup.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect preparation or verify launch without making changes.")
    args = parser.parse_args()
    if args.dry_run and args.action == "prepare":
        args.action = "inspect"
    try:
        revival_rules = load_revival_rules(checked_path(args.event_pass_revival, WORKSPACE_ROOT)
                                          if args.event_pass_revival is not None else None)
        event_login_rules = (load_event_login_rules(checked_path(args.event_login_rules, WORKSPACE_ROOT))
                             if args.event_login_rules is not None else None)
        purchased_login_rules = (load_purchased_login_rules(checked_path(args.purchased_login_rules, WORKSPACE_ROOT))
                                 if args.purchased_login_rules is not None else None)
        mission_window_rules = (load_mission_window_rules(checked_path(args.mission_windows, WORKSPACE_ROOT))
                                if args.mission_windows is not None else None)
        command_text_overrides = (load_command_text_overrides(checked_path(args.command_text_overrides, WORKSPACE_ROOT))
                                  if args.command_text_overrides is not None else None)
        runtime = checked_path(args.runtime, PROJECT_ROOT / "runtime")
        if runtime == PROJECT_ROOT / "runtime":
            raise ClientError("Use a child directory of the runtime directory.")
        if args.action == "prepare":
            result = prepare(args.source, runtime, resource_root=args.resource_dir, master_root=args.master_dir, refresh=args.refresh,
                             revival_rules=revival_rules, event_login_rules=event_login_rules,
                             purchased_login_rules=purchased_login_rules, mission_window_rules=mission_window_rules,
                             command_text_overrides=command_text_overrides,
                             progress=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
            manifest = result.pop("manifest")
            result.update(manifest=str(runtime / MANIFEST), copied_files=len(manifest["files"]),
                          resource_snapshot=manifest["resource_snapshot"])
        elif args.action == "inspect":
            source = checked_path(args.source, WORKSPACE_ROOT)
            resource_root = checked_path(args.resource_dir, WORKSPACE_ROOT)
            _separate_directories(source, runtime, resource_root)
            profile = source_profile(source, WORKSPACE_ROOT)
            inspected = preparation_plan(source, resource_root, WORKSPACE_ROOT, profile, master_root=args.master_dir,
                                         revival_rules=revival_rules, event_login_rules=event_login_rules,
                                         purchased_login_rules=purchased_login_rules,
                                         mission_window_rules=mission_window_rules,
                                         command_text_overrides=command_text_overrides)
            result = {
                "status": "source-verified",
                "source_sha256": profile.sha256,
                "target_sha256": digest(inspected["patched"]),
                "patches": patch_records(profile),
                "api_literal_inventory": inspected["api_literal_inventory"],
                "cdn_literal_inventory": inspected["cdn_literal_inventory"],
                "resource_snapshot": inspected["resource_snapshot"],
                "resource_check": "Local replacements verified in memory; other files checked for existence and size, then hashed during preparation.",
                "files_to_copy": 1 + len(inspected["files"]) + 2 * len(inspected["entries"]),
                "bytes_to_copy": inspected["bytes_to_copy"],
                **_recorded(inspected),
            }
        elif args.action == "verify":
            manifest = verify_runtime(runtime, WORKSPACE_ROOT)
            _check_requested_revival(manifest, revival_rules)
            _check_requested_event_login(manifest, event_login_rules)
            _check_requested_purchased_login(manifest, purchased_login_rules)
            _check_requested_mission_windows(manifest, mission_window_rules)
            _check_requested_command_texts(manifest, command_text_overrides)
            result = {"status": "verified", "runtime": str(runtime), "target_sha256": manifest["target_sha256"]}
        else:
            result = launch(runtime, dry_run=args.dry_run, revival_rules=revival_rules, event_login_rules=event_login_rules,
                            purchased_login_rules=purchased_login_rules, mission_window_rules=mission_window_rules,
                            command_text_overrides=command_text_overrides)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ClientError, OSError, ValueError, RecursionError) as error:
        print(f"Client preparation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
