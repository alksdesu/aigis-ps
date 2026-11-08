from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any

from .admin_command import error_payload
from .admin_config_switch import EMPTY_SET
from .admin_registry import ViewRegistry
from .admin_views import ViewContext, redacted_view
from .admin_views_runtime import client_manifest_versions, identified_set, masterdata_versions, versions_match
from .config_registry import REGISTRY, ConfigError
from .config_sets import ConfigSets, ConfigSource, ResolvedConfig, ResolvedEntry
from .generation import GenerationIdentity


RULES = "rules"
NATIVE_CDN = "native_cdn"
# Generation digests carry command-line files by content only, so no stored set can stand in for them.
RUNNING_UNKNOWN = "The running digest names no readable stored set, as when command-line rules files are in use"


def registry_keys() -> list[dict]:
    return [{**spec.describe(), "default": spec.normalize(spec.default())} for spec in REGISTRY.values()]


def defaults() -> ResolvedConfig:
    return ResolvedConfig(None, None, {key: ResolvedEntry(spec, ConfigSource.DEFAULT, None, None)
                                       for key, spec in REGISTRY.items()})


def running_config(data_dir: Path, digest: str) -> tuple[str | None, ResolvedConfig | None]:
    sets = ConfigSets(data_dir, registry=REGISTRY)
    identifier = identified_set(digest, sets)
    if identifier is None:
        return None, None
    return identifier, defaults() if identifier == EMPTY_SET else sets.resolve_candidate(identifier, Namespace())


def configuration(data_dir: Path, digest: str) -> dict:
    running = {"digest": digest, "set": None, "known": False, "reason": None, "error": None}
    try:
        identifier, config = running_config(data_dir, digest)
    except ConfigError as error:
        return {"running": {**running, "error": error_payload(error)}, "keys": registry_keys()}
    if config is None:
        return {"running": {**running, "reason": RUNNING_UNKNOWN}, "keys": registry_keys()}
    return {"running": {**running, "set": identifier, "known": True},
            "keys": [entry.describe() for entry in config.entries.values()]}


def resource_versions(identity: GenerationIdentity, bundle: Any) -> dict:
    native = getattr(bundle, NATIVE_CDN, None)
    served = None if native is None else native.status().get("version")
    base, effective = identity.base_resource_version, identity.effective_resource_version
    unchanged = versions_match(base, effective)
    return {"base": base, "effective": effective, "served": served,
            "overlaid": None if unchanged is None else not unchanged, "matches": versions_match(effective, served)}


def rules_view(context: ViewContext, params: None) -> dict:
    leased, data_dir = context.generation.generation, context.services.data_dir
    identity, bundle = leased.identity, leased.bundle
    return {**configuration(data_dir, identity.config_set),
            "versions": {"masterdata": masterdata_versions(identity, bundle),
                         "resources": resource_versions(identity, bundle),
                         "client_manifest": client_manifest_versions(data_dir, identity, bundle),
                         "content": dict(identity.content_versions)}}


# Stored rule sets, the client manifest and the served resources change on disk without a new generation.
VIEWS = (redacted_view(f"{RULES}.config", RULES, rules_view, live=True),)


def register(registry: ViewRegistry) -> None:
    for view in VIEWS:
        registry.add(view)
