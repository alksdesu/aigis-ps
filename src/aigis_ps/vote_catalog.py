from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import re
from types import MappingProxyType

from .al_format import ParseLimits, decompress_all4, parse_table
from .alxml import loads
from .energy_clock import JST, instant
from .errors import ProtocolError
from .local_present_templates import LOCAL_TEMPLATE_CARDS
from .native_numbers import int32
from .public_tables import MASTER_LIMITS
from .system_literals import command_id
from .vote_rules import VoteRules, revival_instant


LIMITS = ParseLimits(max_input_bytes=16 * 1024 * 1024, max_decompressed_bytes=16 * 1024 * 1024,
                     max_rows=65535, max_fields=128, max_cells=2_000_000)
RARITIES = MappingProxyType({"ブラック": (5, 11), "プラチナ": (4, 10), "サファイア": (7,),
                             "ゴールド": (3,), "シルバー": (2,), "ブロンズ": (1,), "アイアン": (0,)})
ITEM_FIELDS = {"ItemID": 32, **{name: 1 for name in ("ID", "Category", "EffectType", "Stack", "CanAllGet",
    "ReceiveAction", "ReceiveConfirmation", "InvisibleContaint", "NoAutoDelete")}, "EventID": 32}
TEMPLATE_FIELDS = {name: 1 for name in ("PresentID", "CardID", "InitExp", "InitSkillID", "InitSkillLevel",
                                      "CostMod", "InitLove", "EvolutionCount", "ClassChangeStatusMod")}
TEMPLATE_FIELDS["ClassChangeEnable"] = 5
CATALOG_CHANGED = "vote_catalog_changed"
CANDIDATES_UNRESOLVED = "vote_candidates_unresolved"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _integer(value, minimum=0):
    if not int32(value) or value < minimum:
        raise ValueError("Vote values require bounded native integers")
    return value


@dataclass(frozen=True)
class VoteEvent:
    event_id: int
    title: str
    source_start: datetime
    source_end: datetime
    source_result_start: datetime
    start_at: datetime
    end_at: datetime
    result_start: datetime
    result_end: datetime
    announcement_start: datetime
    announcement_end: datetime


@dataclass(frozen=True)
class VoteGroup:
    group_id: int
    item_id: str
    dispersion_start: datetime
    dispersion_end: datetime
    vote_start: datetime
    vote_end: datetime
    result_start: datetime
    final: bool
    source_dispersion_start: datetime
    source_dispersion_end: datetime
    source_vote_start: datetime
    source_vote_end: datetime
    source_result_start: datetime


@dataclass(frozen=True)
class VoteCandidate:
    card_id: int
    group_id: int
    name: str
    rarity: int


@dataclass(frozen=True)
class VoteCandidateChoice:
    name: str
    group_id: int
    card_ids: tuple[int, ...]


@dataclass(frozen=True)
class VoteCandidateResolution:
    name: str
    group_id: int
    card_id: int
    choices: tuple[int, ...]
    template_ids: tuple[int, ...]


@dataclass(frozen=True)
class VoteReward:
    group_id: int
    threshold: int
    repeat_every: int
    kind: str
    template_id: int
    card_id: int
    item_id: str
    amount: int
    label: str
    unit_level: int = 0
    skill_level: int = 0
    class_change: int = 0
    likable: int = 0
    cost: int = 0


class VoteCatalog:
    def __init__(self, asset_reader, card_provider, rules=None):
        try:
            self.rules = VoteRules() if rules is None else rules
            if not isinstance(self.rules, VoteRules) or not callable(asset_reader):
                raise ValueError("Vote catalog requires verified rules and a resource reader")
            hashes, resources = {}, {}

            def table(name, expected):
                raw = asset_reader(name)
                if type(raw) is not bytes or not 0 < len(raw) <= LIMITS.max_input_bytes:
                    raise ValueError("Vote source must be bounded bytes")
                parsed = parse_table(decompress_all4(raw, LIMITS) if raw.startswith(b"ALL4") else raw, LIMITS)
                fields = {field["name"]: field["type_code"] for field in parsed["fields"]}
                if len(fields) != len(parsed["fields"]) or any(fields.get(key) != kind for key, kind in expected.items()):
                    raise ValueError("Vote source fields differ from the verified schema")
                hashes[name], resources[name] = hashlib.sha256(raw).hexdigest(), raw
                return parsed["rows"]

            texts = table("SystemText.atb", {"Data_Text": 32, "Recode_Index": 1})
            implants = table("cfgImplantText.atb", {"_ID": 32, "_TextID": 1})
            text_ids = {}
            for row in implants:
                if type(row["_ID"]) is not str or row["_ID"] in text_ids:
                    raise ValueError("Vote text identities must be unique")
                text_ids[row["_ID"]] = _integer(row["_TextID"])

            def text(name):
                index = text_ids[name]
                if index >= len(texts) or type(texts[index]["Data_Text"]) is not str:
                    raise ValueError("Vote text pointer is unavailable")
                return texts[index]["Data_Text"]

            names = table("NameText.atb", {"Message": 32})
            getter = card_provider if callable(card_provider) else card_provider.get_xml
            card_raw = getter("CardData")
            if type(card_raw) is not bytes:
                raise ValueError("Vote card data must be immutable bytes")
            card_table = loads(card_raw, limits=MASTER_LIMITS)
            columns = {column.name: column.values for column in card_table.children}
            if len(columns) != len(card_table.children) or not {"CardID", "Rare"} <= columns.keys():
                raise ValueError("Vote public card fields are unavailable")
            if len({len(values) for values in columns.values()}) != 1:
                raise ValueError("Vote public card columns must align")
            cards = {}
            for identifier, rarity in zip(columns["CardID"], columns["Rare"]):
                _integer(identifier, 1)
                _integer(rarity)
                if identifier in cards:
                    raise ValueError("Vote card identities must be unique")
                cards[identifier] = rarity
            if not cards:
                raise ValueError("Vote requires public cards")
            hashes["CardData"], resources["CardData"] = hashlib.sha256(card_raw).hexdigest(), card_raw
            by_name = {}
            for index, row in enumerate(names):
                if type(row["Message"]) is not str:
                    raise ValueError("Vote names must be strings")
                if index + 1 in cards:
                    by_name.setdefault(row["Message"], []).append(index + 1)

            items = {}
            for row in table("ItemConfigTable.atb", ITEM_FIELDS):
                identifier = row["ItemID"]
                if type(identifier) is not str or identifier in items:
                    raise ValueError("Vote item identities must be unique")
                items[identifier] = row
            for group_id in range(1, 5):
                identifier = f"item_vote_016{group_id:03}"
                row = items[identifier]
                expected = {"ID": command_id(identifier),
                    "Category": 1925062086, "EffectType": 10, "Stack": 999, "CanAllGet": 1,
                    "ReceiveAction": 0, "ReceiveConfirmation": 0, "InvisibleContaint": 0, "NoAutoDelete": 0}
                if any(type(row[key]) is not int or row[key] != value for key, value in expected.items()) or row["EventID"] != "":
                    raise ValueError("Vote ticket behavior differs from the source contract")

            templates, native_templates, local_templates = {}, {}, []
            for row in table("PresentUnitData.atb", TEMPLATE_FIELDS):
                identifier = _integer(row["PresentID"])
                if identifier in templates:
                    raise ValueError("Vote present templates must be unique")
                templates[identifier] = row
                if identifier > 0:
                    card_id = _integer(row["CardID"], 1)
                    # Locally appended delivery templates prove no official distribution of their card.
                    if LOCAL_TEMPLATE_CARDS.get(identifier) == card_id:
                        local_templates.append(identifier)
                    else:
                        native_templates.setdefault(card_id, []).append(identifier)

            group_sources, candidates, unresolved, seen_names, seen_cards = [], [], [], set(), set()
            local_resolutions = []
            override_names = set()
            for group_id in range(1, 5):
                source = text(f"SystemTextVoteInfomation{group_id + 5}")
                windows = re.findall(r"\d{4}/\d{1,2}/\d{1,2} \d{1,2}:\d{2}", source)
                if len(windows) != 5:
                    raise ValueError("Vote groups require exactly five source timestamps")
                window = [datetime.strptime(value, "%Y/%m/%d %H:%M").replace(tzinfo=JST) for value in windows]
                window[1] += timedelta(minutes=1)
                window[3] += timedelta(minutes=1)
                if not window[0] < window[1] <= window[3] <= window[4] or not window[0] <= window[2] < window[3]:
                    raise ValueError("Vote source windows have invalid ordering")
                group_sources.append(window)
                if group_id == 4:
                    if "各グループ上位10名" not in source or "同数のキャラクター全員" not in source:
                        raise ValueError("Vote final qualification differs from the documented rule")
                    continue
                rarity = None
                count = 0
                for line in source.splitlines():
                    line = line.strip()
                    if re.fullmatch(r"【[^】]+】", line):
                        rarity = RARITIES[line[1:-1]]
                    elif rarity and line and set(line) != {"-"}:
                        if line in seen_names or len(line.encode()) > 512:
                            raise ValueError("Vote candidate names repeat or exceed the field bound")
                        seen_names.add(line)
                        count += 1
                        choices = tuple(identifier for identifier in by_name.get(line, ()) if cards[identifier] in rarity)
                        if not choices:
                            raise ValueError("Vote candidate name or rarity is absent from public cards")
                        override = self.rules.candidate_overrides.get(line)
                        if override is not None:
                            override_names.add(line)
                            if override not in choices:
                                raise ValueError("Vote candidate override contradicts its source name or rarity")
                            selected = override
                        elif len(choices) == 1:
                            selected = choices[0]
                        else:
                            distributed = tuple(identifier for identifier in choices if identifier in native_templates)
                            if self.rules.candidate_policy != "present_template" or len(distributed) != 1:
                                unresolved.append(VoteCandidateChoice(line, group_id, choices))
                                continue
                            selected = distributed[0]
                            local_resolutions.append(VoteCandidateResolution(line, group_id, selected, choices,
                                tuple(sorted(native_templates[selected]))))
                        if selected in seen_cards:
                            raise ValueError("Vote candidates must not repeat across groups")
                        seen_cards.add(selected)
                        candidates.append(VoteCandidate(selected, group_id, line, cards[selected]))
                if not 0 < count <= 4096:
                    raise ValueError("Vote source groups require bounded candidate lists")
            if override_names != set(self.rules.candidate_overrides):
                raise ValueError("Vote candidate overrides name an unknown source candidate")
            if any(left[1] != right[0] for left, right in zip(group_sources, group_sources[1:])):
                raise ValueError("Vote dispersion windows must form the verified continuous schedule")
            offset = timedelta(0) if self.rules.revival_start is None else revival_instant(self.rules.revival_start) - group_sources[0][0]
            groups = tuple(VoteGroup(identifier, f"item_vote_016{identifier:03}",
                *(value + offset for value in window), identifier == 4, *window)
                for identifier, window in enumerate(group_sources, start=1))

            info = text("SystemTextVoteInfomation5")
            if info != text("SystemTextVoteInfomation") or "■投票数報酬" not in info:
                raise ValueError("Vote reward text sources disagree")
            parsed_rewards, section, threshold = [], None, 0
            for line in info.split("■投票数報酬", 1)[1].splitlines():
                line = line.strip()
                if line in ("【グループA～C】", "【決戦闘兵】"):
                    section, threshold = ((1, 2, 3) if line == "【グループA～C】" else (4,)), 0
                elif re.match(r"^\d+：", line):
                    value, label = line.split("：", 1)
                    value = _integer(int(value), 1)
                    if section is None or value <= threshold:
                        raise ValueError("Vote reward thresholds must increase within each section")
                    threshold = value
                    parsed_rewards.append((section, threshold, 0, label))
                elif line.startswith("以降："):
                    match = re.fullmatch(r"以降：(\d+)票毎に(.+)", line)
                    if match is None or not threshold:
                        raise ValueError("Vote repeated reward requires a positive base threshold")
                    parsed_rewards.append((section, threshold, _integer(int(match[1]), 1), match[2]))
            if sum(len(section) for section, _, repeat, _ in parsed_rewards if not repeat) != 52 or sum(len(section) for section, _, repeat, _ in parsed_rewards if repeat) != 4:
                raise ValueError("Vote reward counts differ from the verified sixteenth event")
            rewards = []
            for section, threshold, repeat, label in parsed_rewards:
                item_id, template_id, card_id, amount, kind = "", 0, 0, 1, "unit"
                unit_level = skill_level = class_change = likable = cost = 0
                if label in ("水晶×5", "ルビー×5"):
                    kind, amount = "item", 5
                    item_id = "gift_0001_quartz" if label == "水晶×5" else "gift_0002_ruby"
                    if items[item_id]["EffectType"] != 4 or items[item_id]["Stack"] < amount:
                        raise ValueError("Vote gift reward inventory definition differs")
                else:
                    identities = by_name[label]
                    if len(identities) != 1:
                        raise ValueError("Vote unit reward identity is ambiguous")
                    card_id = identities[0]
                    # The unique name already fixes the card, so local templates stay valid delivery paths here.
                    matches = [row for row in templates.values() if row["CardID"] == card_id and row["PresentID"] > 0]
                    if label == "技強化聖霊クリスティア":
                        matches = [row for row in matches if row["PresentID"] == self.rules.skill_fairy_template_id]
                    if len(matches) != 1:
                        raise ValueError("Vote unit reward template is missing or ambiguous")
                    row = matches[0]
                    if (any(type(row[key]) is not int or row[key] != 0 for key in ("InitExp", "CostMod", "InitLove", "EvolutionCount", "ClassChangeStatusMod"))
                            or row["InitSkillID"] != -1 or row["InitSkillLevel"] not in (0, 1) or row["ClassChangeEnable"] is not False):
                        raise ValueError("Vote reward template has unverified initial state")
                    template_id = row["PresentID"]
                    unit_level, skill_level = 1, row["InitSkillLevel"]
                rewards.extend(VoteReward(group_id, threshold, repeat, kind, template_id, card_id, item_id, amount, label,
                                          unit_level, skill_level, class_change, likable, cost)
                               for group_id in section)

            self.event = VoteEvent(16, "第十六回人気闘兵決定戦", group_sources[0][0], group_sources[-1][3],
                group_sources[-1][4], min(group.vote_start for group in groups), max(group.vote_end for group in groups),
                groups[-1].result_start, groups[-1].result_start + timedelta(days=self.rules.result_display_days),
                groups[0].dispersion_start, groups[-1].result_start + timedelta(days=self.rules.result_display_days))
            self.groups, self.candidates, self.rewards = groups, tuple(candidates), tuple(rewards)
            self.groups_by_id = MappingProxyType({group.group_id: group for group in groups})
            self.candidates_by_id = MappingProxyType({candidate.card_id: candidate for candidate in candidates})
            self.unresolved_candidates = tuple(unresolved)
            self.local_resolutions = tuple(local_resolutions)
            self.excluded_template_ids = tuple(sorted(local_templates))
            self.resource_hashes, self._resources = MappingProxyType(hashes), MappingProxyType(resources)
            self._source_content = MappingProxyType({"event": 16, "resources": MappingProxyType(hashes)})
            self.source_content_version = _digest({"event": 16, "resources": hashes})
            self.content_version = _digest({"source": self.source_content_version, "rules": self.rules.to_dict(),
                "policy": "local_vote_16_text_source_v1"})
            self._snapshot = tuple(getattr(self, name) for name in self._snapshot_names())
        except (AttributeError, KeyError, TypeError, ValueError, OSError, OverflowError) as error:
            raise ProtocolError("vote_catalog_invalid", "Vote source or configuration differs from the verified contract", 503) from error

    @staticmethod
    def _snapshot_names():
        return ("rules", "event", "groups", "groups_by_id", "candidates", "candidates_by_id", "rewards",
                "unresolved_candidates", "local_resolutions", "excluded_template_ids", "resource_hashes", "_resources",
                "_source_content", "source_content_version", "content_version")

    def _ready(self):
        if any(getattr(self, name) is not value for name, value in zip(self._snapshot_names(), self._snapshot)):
            raise ProtocolError(CATALOG_CHANGED, "Vote catalog changed after construction", 503)

    def active(self, now):
        self._ready()
        current = instant(now)
        if not self.rules.enabled or not self.event.start_at <= current < self.event.end_at:
            return False
        if self.unresolved_candidates:
            raise ProtocolError(CANDIDATES_UNRESOLVED, "Vote candidate identities require explicit resolution", 503)
        return True

    def status(self):
        try:
            self._ready()
            error = None
        except ProtocolError as failure:
            error = failure.code
        return {"ready": error is None, "error": error, "content_version": self.content_version,
            "source_content_version": self.source_content_version, "rules": self.rules.to_dict(),
            "group_count": len(self.groups), "candidate_count": len(self.candidates), "reward_count": len(self.rewards),
            "unresolved_candidates": [{"name": entry.name, "group_id": entry.group_id, "card_ids": list(entry.card_ids)}
                                      for entry in self.unresolved_candidates],
            "local_resolution": {"policy": self.rules.candidate_policy, "official_vcc_confirmed": False,
                "resource": "PresentUnitData.atb", "excluded_present_ids": list(self.excluded_template_ids),
                "choices": [{"name": entry.name, "group_id": entry.group_id, "card_id": entry.card_id,
                    "alternatives": list(entry.choices), "present_ids": list(entry.template_ids)}
                    for entry in self.local_resolutions]},
            "resource_hashes": dict(self.resource_hashes), "source": "Local revival of the sixteenth event resource text",
            "qualification_ready": not self.unresolved_candidates}
