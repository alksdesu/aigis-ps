from __future__ import annotations

from collections import Counter
from collections.abc import Callable
import hashlib
import logging
from typing import TYPE_CHECKING

from .al_format import ParseLimits, parse_table
from .alxml import Column, Container, dumps
from .errors import ProtocolError
from .peruse_state import (
    HASH_COLLISION, MAX_EVENT_ID_BYTES, PACKAGE_ERRORS, SAVE_ARGUMENT, HomeEventArchive, asset_root_reader,
    client_string, colliding_sources, command_arguments, decoded_resource, exclusion_reason, mark_read,
    validate_event_id,
)
from .protocol import ProtocolRegistry, RequestEnvelope
from .request_fields import MAX_STRING_BYTES, invalid, registered, scalar
from .storage import Store

if TYPE_CHECKING:
    from .bootstrap import BootstrapService


log = logging.getLogger("aigis_ps")

EPISODE_RESOURCE = "EventEpisodeConfig.atb"
EPISODE_FIELDS = {"ConfigIDName": "string_ref", "EventID": "string_ref"}
SAVE_EPISODE_COMMAND = "DoPeruseSaveEpisode"
MAX_CONFIG_ID_BYTES = MAX_STRING_BYTES
CONFIG_LIMIT = 4 * 1024 * 1024
CONFIG_LIMITS = ParseLimits(max_input_bytes=CONFIG_LIMIT, max_decompressed_bytes=CONFIG_LIMIT,
                            max_rows=65535, max_fields=64)


def _validate_config_id(value) -> None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_CONFIG_ID_BYTES:
        raise ValueError("ConfigIDName must be a nonempty request-sized string")
    if not client_string(value):
        raise ValueError("The client keeps a numeric ConfigIDName as a number and never sends it")
    dumps(Container(children=(Column("ConfigID", "S", (value,)),)))


def _launched_event_id(value) -> str:
    validate_event_id(value, MAX_EVENT_ID_BYTES)
    if not client_string(value):
        raise ValueError("The client only launches episodes under a string EventID")
    return value


def _sent_config_id(active: str, parameters: str) -> str:
    arguments = command_arguments(parameters)
    if SAVE_ARGUMENT not in arguments:
        return active
    saved = arguments[SAVE_ARGUMENT]
    if not client_string(saved):
        # The command reads a numeric Save as a null string, which the client cannot send.
        raise ValueError("DoPeruseSaveEpisode cannot send a numeric Save argument")
    return saved


def _row_key(index: int, name) -> str:
    return name if isinstance(name, str) and name else f"#{index}"


class EpisodeCatalog:
    def __init__(self, asset_reader: Callable[[str], bytes]):
        archive = HomeEventArchive(asset_reader)
        try:
            data = decoded_resource(asset_reader(EPISODE_RESOURCE), CONFIG_LIMITS)
            table = parse_table(data, CONFIG_LIMITS)
            fields = {field["name"]: field["type_name"] for field in table["fields"]}
            if any(fields.get(name) != kind for name, kind in EPISODE_FIELDS.items()):
                raise ValueError("EventEpisodeConfig differs from the native episode table")
        except (OSError, *PACKAGE_ERRORS) as error:
            raise ProtocolError("peruse_episode_catalog_invalid", "Episode resources differ from the verified contract", 503) from error
        self.resource_hash = archive.resource_hash
        self.config_hash = hashlib.sha256(data).hexdigest()
        self.episode_count = len(table["rows"])
        self.excluded: dict[str, str] = {}
        episodes = self._episodes(table["rows"])
        sends, launched = self._sends(archive, episodes)
        accepted = {}
        for name, sent in sends.items():
            unknown = sorted(config_id for config_id in sent if config_id not in episodes)
            if unknown:
                self.excluded[name] = f"DoPeruseSaveEpisode sends a ConfigID without a valid episode row: {unknown[0]!r}"
                continue
            accepted.update((config_id, episodes[config_id]) for config_id in sent)
        for config_id in colliding_sources({config_id: frozenset((event_id,)) for config_id, event_id in accepted.items()}):
            self.excluded[config_id] = HASH_COLLISION
            del accepted[config_id]
        self.episodes = accepted
        self.unlaunched_packages = sorted(package.member for package in archive.packages.values()
                                          if package.parameters(SAVE_EPISODE_COMMAND) and package.member not in launched)

    def _episodes(self, rows: list[dict]) -> dict[str, str]:
        names = Counter(row["ConfigIDName"] for row in rows)
        episodes = {}
        for index, row in enumerate(rows):
            name = row["ConfigIDName"]
            try:
                if names[name] > 1:
                    raise ValueError("ConfigIDName names several episodes")
                _validate_config_id(name)
                episodes[name] = _launched_event_id(row["EventID"])
            except PACKAGE_ERRORS as error:
                self.excluded[_row_key(index, name)] = exclusion_reason(error)
        return episodes

    def _sends(self, archive: HomeEventArchive, episodes: dict[str, str]) -> tuple[dict[str, frozenset[str]], set[str]]:
        sends, launched = {}, set()
        for name, event_id in episodes.items():
            package = archive.launched(event_id)
            if package is None:
                reason = archive.launch_exclusion(event_id)
                if reason is not None:
                    self.excluded[name] = f"Its home event package is excluded: {reason}"
                continue
            launched.add(package.member)
            try:
                sends[name] = frozenset(_sent_config_id(name, parameters)
                                        for parameters in package.parameters(SAVE_EPISODE_COMMAND))
            except PACKAGE_ERRORS as error:
                self.excluded[name] = exclusion_reason(error)
        return sends, launched

    def event_id(self, config_id: str) -> str | None:
        return self.episodes.get(config_id)

    def excluded_reason(self, config_id: str) -> str | None:
        return self.excluded.get(config_id)

    def status(self) -> dict:
        return {"ready": True, "resource_hash": self.resource_hash, "config_hash": self.config_hash,
                "episode_count": self.episode_count, "supported_episode_count": len(self.episodes),
                "supported_episodes": dict(sorted(self.episodes.items())),
                "excluded_episode_count": len(self.excluded),
                "excluded_episodes": [{"config_id": key, "reason": reason} for key, reason in sorted(self.excluded.items())],
                "unlaunched_packages": self.unlaunched_packages}


class PeruseEpisodeService:
    def __init__(self, bootstrap: BootstrapService, *, asset_reader: Callable[[str], bytes] | None = None):
        self.bootstrap = bootstrap
        self._startup_profile = ProtocolRegistry.load().by_registration[137]
        self.catalog = None
        self.catalog_error = None
        try:
            self.catalog = EpisodeCatalog(asset_reader or asset_root_reader(bootstrap.asset_root))
        except ProtocolError as error:
            self.catalog_error = error.code
        if self.catalog is not None and self.catalog.excluded:
            log.warning("Request 136 serves episodes without %d excluded entries: %s", len(self.catalog.excluded),
                        "; ".join(f"{key}: {reason}" for key, reason in sorted(self.catalog.excluded.items())))

    def handlers(self):
        return {136: self.save}

    def status(self):
        catalog = self.catalog.status() if self.catalog is not None else {"ready": False, "error": self.catalog_error}
        return {"implemented_operations": [136], "schema_version": 1, **catalog,
                "save_policy": ("Mark the EventID of the episode named by ConfigID read in PEV; "
                                "preserve existing nonzero flags"),
                "reward_policy": "Record read history only; EventEpisodeConfig experience is not granted",
                "exclusion_policy": "Episodes outside the client contract are excluded one by one; the rest stay served",
                "reload_policy": "Full PEV snapshot through operation 137",
                "local_limits": {"config_id_bytes": MAX_CONFIG_ID_BYTES, "event_id_bytes": MAX_EVENT_ID_BYTES}}

    def _episode(self, config_id: str) -> str:
        if self.catalog is None:
            log.warning("Request 136 refused episode %r: episode resources are unavailable (%s)", config_id, self.catalog_error)
            raise ProtocolError("peruse_episode_catalog_unavailable", "Verified episode resources are unavailable", 503)
        event_id = self.catalog.event_id(config_id)
        if event_id is not None:
            return event_id
        reason = self.catalog.excluded_reason(config_id)
        if reason is not None:
            log.warning("Request 136 refused episode %r: it is excluded (%s)", config_id, reason)
            raise ProtocolError("peruse_episode_excluded", "The episode that sends this ConfigID is excluded", 503)
        log.warning("Request 136 refused episode %r: no episode sends it through DoPeruseSaveEpisode", config_id)
        raise ProtocolError("peruse_episode_unsupported", "No native episode saves this ConfigID", 501)

    def save(self, envelope: RequestEnvelope, store: Store) -> bytes:
        request = registered(envelope, {136}, {"ConfigID"}, {"ConfigID"})
        config_id = scalar(request["ConfigID"], "S", max_bytes=MAX_CONFIG_ID_BYTES)
        if not config_id:
            invalid("Episode saves require a nonempty ConfigID")
        return mark_read(self.bootstrap, self._startup_profile, envelope, store, lambda: self._episode(config_id))
