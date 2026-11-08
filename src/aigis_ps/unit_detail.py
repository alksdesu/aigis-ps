from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import re

from .alxml import Container, dumps, loads
from .errors import ProtocolError
from .native_numbers import INT32_MIN, int32
from .player_state import DISPLAY_FIELD, SKILL_SWITCH_FLAG, primary_unit_updates
from .protocol import ProtocolRegistry, encode_response
from .request_ledger import HISTORY_LIMIT, PROCESSED_LIMIT, REQUEST_ORDER, RequestLedger
from .unit_detail_protocol import parse_unit_detail


RESPONSE_LIMIT = 16384
HISTORY_NAMESPACE = "unit_detail_history"
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _integer(value, minimum=INT32_MIN):
    if not int32(value) or value < minimum:
        raise ValueError("Expected a bounded native integer")
    return value


class UnitDetailService:
    def __init__(self, bootstrap, players, growth, *, smartphone_images=False, skin_catalog=None):
        if type(smartphone_images) is not bool:
            raise ValueError("Smartphone image policy must be boolean")
        self.bootstrap, self.players, self.growth = bootstrap, players, growth
        self.smartphone_images = smartphone_images
        self.skin_catalog = skin_catalog
        self._unit_profile = ProtocolRegistry.load().by_registration[41]
        self._ledger = RequestLedger(HISTORY_NAMESPACE, "unit_detail_history_invalid", "Stored unit-detail receipts are invalid",
                                     fields=("request", "content", "revision", "response"), response_limit=RESPONSE_LIMIT)

    def handlers(self):
        return {10: self.save}

    def status(self):
        return {"implemented_operations": [10], "content_version": self._content_version(),
                "smartphone_images": self.smartphone_images, "native_tested": False,
                "skin_catalog": self.skin_catalog.status() if self.skin_catalog is not None else None,
                "history_limit": HISTORY_LIMIT, "processed_request_limit": PROCESSED_LIMIT,
                "replay_policy": ("Exact request at its committed revision and content version, also once a costume "
                                  "catalog or optional growth inputs absent at its commit attach later"),
                "request_order": REQUEST_ORDER,
                "noop_policy": "Preserve unit values and commit a request receipt once",
                "unsupported": ["Unverified costume selections"]}

    def _content_version(self, *, skins=True, growth=None):
        version = getattr(self.growth, "content_version", None) if growth is None else growth
        if not isinstance(version, str) or not _HASH.fullmatch(version):
            raise ProtocolError("unit_detail_dependencies_invalid", "Unit detail requires verified growth content", 503)
        skin_version = getattr(self.skin_catalog, "content_version", None)
        if self.skin_catalog is not None and (not isinstance(skin_version, str) or not _HASH.fullmatch(skin_version)):
            raise ProtocolError("unit_detail_dependencies_invalid", "Unit detail requires verified costume content", 503)
        content = {"growth": version, "skin_catalog": skin_version if skins else None,
                   "smartphone_images": self.smartphone_images, "policy_version": 2}
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()

    def _replayable(self, committed, current):
        # A receipt keeps the digest of its commit, which costume or growth inputs attached since then cannot reach.
        costumes = (True,) if self.skin_catalog is None else (True, False)
        return committed == current or any(self._content_version(skins=skins, growth=growth) == committed
                                           for growth in self.growth.replayable_content_versions()
                                           for skins in costumes)

    def _display(self, unit, card, display, state):
        if display in (unit[DISPLAY_FIELD], 0):
            return
        base = display - 16 if self.smartphone_images and display in (17, 18, 19) else display
        stage = unit["ClassChangeCount"]
        fields = {1: ("CharaFace0", "SmartPhoneImageEvolve0")}
        if stage >= 2:
            fields[2] = ("Face1", "SmartPhoneImageEvolve2")
        if stage in (3, 4):
            fields[3] = (f"CharaFace{stage}", f"SmartPhoneImageEvolve{stage}")
        available = base in fields and _integer(card[fields[base][0]]) != 0
        if available and display != base:
            available = _integer(card[fields[base][1]]) != 0
        if not available and self.skin_catalog is not None:
            allowed = self.skin_catalog.allowed_displays(unit["CardID"], deepcopy(state),
                                                         smartphone_images=self.smartphone_images)
            possible = {5, 6, 7, 8} | ({21, 22, 23, 24} if self.smartphone_images else set())
            if not isinstance(allowed, (set, frozenset)) or any(not int32(value) or value not in possible for value in allowed):
                raise ValueError("Costume choices are outside the verified display range")
            available = display in allowed
        if not available:
            raise ProtocolError("unit_detail_display_unsupported", "This new display selection is not available", 501)

    def _toggle(self, unit, card):
        stage = unit["ClassChangeCount"]
        if not 2 <= stage <= 4 or not (unit["SkillEvolved"] or unit["GeneralPurposeFlags"] & SKILL_SWITCH_FLAG):
            raise ProtocolError("unit_detail_skill_ineligible", "This unit has not unlocked skill switching", 409)
        current = self.growth.classes[_integer(card["InitClassID"], 1)]
        advanced = _integer(current["JobChange"], 0)
        if advanced:
            current = self.growth.classes[advanced]
        if stage >= 3:
            if not _integer(card["_AwakePattern"], 0) & (1 << (stage - 3)):
                raise ProtocolError("unit_detail_skill_ineligible", "This awakening branch is unavailable", 409)
            current = self.growth.classes[_integer(current[f"AwakeType{stage - 2}"], 1)]
        if _integer(current["ClassID"], 1) != unit["ClassID"]:
            raise ProtocolError("unit_detail_skill_ineligible", "Saved class does not match this awakening stage", 409)
        evolved, normal = _integer(card["EvoSkillID"], 0), _integer(card["ClassLV1SkillID"], 0)
        if not evolved or evolved >= len(self.growth.skills) or normal >= len(self.growth.skills):
            raise ProtocolError("unit_detail_skill_ineligible", "This unit has no valid pair of switchable skills", 409)
        flags = unit["GeneralPurposeFlags"] ^ SKILL_SWITCH_FLAG
        return {"GeneralPurposeFlags": flags, "SettingSkillID": evolved if flags & SKILL_SWITCH_FLAG else normal}

    def _values(self, unit, display, toggle_skill, state):
        try:
            card = self.growth.cards.card(unit["CardID"])
            if not isinstance(card, Mapping) or _integer(card["CardID"], 1) != unit["CardID"]:
                raise ValueError("Card lookup does not match the persisted identity")
            self._display(unit, card, display, state)
            values = {DISPLAY_FIELD: display}
            if toggle_skill:
                values.update(self._toggle(unit, card))
            return values
        except ProtocolError:
            raise
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
            raise ProtocolError("unit_detail_catalog_invalid", "Unit-detail resources do not satisfy the verified contract", 503) from error

    def _with_selection(self, state, units, get_index, display, *, toggle_skill):
        target = next((unit for unit in units if unit["GetIndex"] == get_index), None)
        if target is None:
            raise ProtocolError("unit_detail_unit_missing", "Unit-detail selection does not match an owned unit", 409)
        values = self._values(target, display, toggle_skill, state)
        updated = deepcopy(state)
        saved = updated["player_state"]
        result = next(unit for unit in saved["units"] if unit["GetIndex"] == get_index)
        result.update(values)
        self.players.validate_player(saved)
        encode_response(self._unit_profile, self.players.unit_response(saved["units"]))
        return updated, result

    def set_display(self, state, get_index, display):
        if not int32(get_index) or get_index < 0 or not int32(display):
            raise ValueError("Display selections need a nonnegative unit index and a native display type")
        units = self.players.validate(state)["units"]
        self._content_version()
        return self._with_selection(state, units, get_index, display, toggle_skill=False)[0]

    def save(self, envelope, store):
        request = parse_unit_detail(envelope)
        key = hashlib.sha256(envelope.profile.name.encode() + b"\0" + envelope.xml).hexdigest()
        for _ in range(3):
            profile = self.players.authenticate(envelope, store)
            player = self.players.validate(profile.state)
            content = self._content_version()
            history = self._ledger.read(profile.state, profile.revision)
            receipt = self._ledger.receipt(history, key)
            if receipt is not None:
                if receipt["revision"] != profile.revision or not self._replayable(receipt["content"], content):
                    raise ProtocolError("unit_detail_retry_stale", "Unit-detail state or content changed after this request", 409)
                response = receipt["response"].encode("utf-8")
                try:
                    loads(response)
                    encode_response(envelope.profile, response)
                except (ValueError, ProtocolError) as error:
                    raise ProtocolError("unit_detail_history_invalid", "Saved unit-detail response is invalid", 503) from error
                return response
            if self._ledger.processed(history, key):
                raise ProtocolError("unit_detail_request_stale", "Unit-detail request was already processed", 409)
            updated, result = self._with_selection(profile.state, player["units"], request.get_index,
                                                   request.display_type, toggle_skill=request.toggle_skill)
            response = dumps(Container(children=(self.bootstrap._system(profile), primary_unit_updates((result,)))))
            encode_response(envelope.profile, response)
            if len(response) > RESPONSE_LIMIT:
                raise ProtocolError("unit_detail_response_capacity", "Unit-detail response exceeds the receipt limit", 503)
            self._ledger.record(updated, history, {"request": key, "content": content,
                                                   "revision": profile.revision + 1, "response": response.decode("utf-8")})
            try:
                store.update_state(updated, profile.revision)
                return response
            except ValueError:
                continue
        raise ProtocolError("profile_changed", "Player state changed while saving unit details", 409)
