from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
import hashlib
import json
import re
from types import MappingProxyType

from . import quest_modes
from .errors import ProtocolError


# 9888A0 maps these native kind names to QuestTable.Type; mission texts name quest kinds with them.
NATIVE_TYPE_NAMES = MappingProxyType({
    0: "ストーリー", 1: "チャレンジ", 2: "曜日", 3: "緊急", 4: "戦術指南", 6: "大討伐", 7: "大討伐2", 8: "復刻",
    9: "魔神降臨", 10: "交流", 11: "神獣", 12: "デイリー復刻", 13: "強襲", 14: "英傑の塔", 15: "練習",
    16: "ヒストリー", 17: "コロシアム", 18: "帝国大戦", 19: "女神の復活クエスト",
})
TEAM_FIELDS = MappingProxyType({"genus_condition": "Genus", "assign_condition": "Assign"})
NEUTRAL_TEAM = MappingProxyType({
    "member_count_condition": 0, "kind_condition": -1, "race_condition": 0, "genus_condition": 0,
    "assign_condition": 0, "identity_condition": 0, "blood_condition": 0, "rarity_condition": -1,
})
QUEST_CLEAR_CONDITION = 2
_COLOR = re.compile(r"%c\[[0-9a-fA-F]{6}\]")
_BEGINNER = re.compile(r"ビギナー任務\d+：\Z")
_AT_LEAST = re.compile(r"(?P<attr>[^\s／/]+?)属性を(?P<count>\d+)人編成で")
_ONLY = re.compile(r"(?P<attr>[^\s／/]+?)属性のみ編成で")
_MEMBER_LIMIT = re.compile(r"\[MEMBER\]人以下編成")
_ANY_KIND_MARKER = "いずれかの"
# Some official texts drop this particle before クリア, so a kind stops at it instead of swallowing a later clause.
_CLEAR_PARTICLE = "を"
_ANY_KIND = re.compile(rf"{re.escape(_ANY_KIND_MARKER)}(?P<kind>[^{_CLEAR_PARTICLE}]+?)"
                       rf"(?:の(?P<suffix>[^{_CLEAR_PARTICLE}の「」]+)|(?P<exact>Lv\.\d+))?"
                       rf"{_CLEAR_PARTICLE}?(?:\[COUNT\]回|\d+回)?クリア")
_COUNT_KIND = re.compile(rf"(?P<kind>[^「」]+?){_CLEAR_PARTICLE}?\[COUNT\]回クリア\Z")


@dataclass(frozen=True)
class TeamRule:
    field: str
    value: int
    mode: str
    count: int | None
    card_ids: frozenset[int]

    def satisfied(self, team) -> bool:
        matched = sum(member["card_id"] in self.card_ids for member in team)
        if self.mode == "at_least":
            return matched >= self.count
        return bool(team) and matched == len(team)


@dataclass(frozen=True)
class QuestClearSpec:
    condition_id: int
    claimed_by: str
    quest_types: tuple[int, ...] | None
    quest_ids: frozenset[int] | None
    team: TeamRule | None = None
    member_limit: int | None = None
    problems: tuple[str, ...] = ()
    kind_conflict: bool = False

    @property
    def resolved(self) -> bool:
        return not self.problems

    def covers(self, payload) -> bool:
        return ((self.quest_types is None or payload["quest_type"] in self.quest_types)
                and (self.quest_ids is None or payload["quest_id"] in self.quest_ids))

    def team_satisfied(self, payload) -> bool:
        if self.team is None and self.member_limit is None:
            return True
        # Entrust results and older payloads carry no formation, so formation conditions cannot hold.
        team = payload.get("team")
        if team is None:
            return False
        return ((self.member_limit is None or len(team) <= self.member_limit)
                and (self.team is None or self.team.satisfied(team)))

    def evaluate(self, payload) -> bool:
        return self.resolved and self.covers(payload) and self.team_satisfied(payload)


def _normalize(text) -> str:
    lines = _COLOR.sub("", text if isinstance(text, str) else "").split("\n")
    if lines and _BEGINNER.match(lines[0].strip()):
        lines = lines[1:]
    return "".join(line.strip() for line in lines)


def _native_kind(phrase: str) -> int | None:
    # The longest contained native name wins; equally long rival names leave the kind undecided.
    hits = [(len(name), quest_type) for quest_type, name in NATIVE_TYPE_NAMES.items() if name in phrase]
    if not hits:
        return None
    longest = max(length for length, _ in hits)
    chosen = sorted(quest_type for length, quest_type in hits if length == longest)
    return chosen[0] if len(chosen) == 1 else None


def _title_matches(title: str, *, suffix=None, exact=None) -> bool:
    if exact is not None:
        return title == exact
    return title == suffix or title.endswith("　" + suffix) or title.endswith(" " + suffix)


def _claimant(quest_types: tuple[int, ...] | None) -> str:
    if quest_types is None:
        return "default"
    entry = quest_modes.registry().get(quest_types[0])
    return entry.name if entry is not None else f"type{quest_types[0]}"


def _field_scope(definition) -> tuple[tuple[int, ...] | None, frozenset[int] | None]:
    quest_types = None if definition.quest_type == -2 else (definition.quest_type,)
    return quest_types, frozenset((definition.quest_id,)) if definition.quest_id else None


def derive_spec(definition, source) -> QuestClearSpec:
    body = _normalize(source.system_text(definition.condition_text))
    problems, kind_conflict = [], False
    quest_types, quest_ids = _field_scope(definition)
    if quest_types is not None and quest_types[0] < 0:
        problems.append("quest_type_unknown")
    if definition.quest_id:
        actual = source.quest_type(definition.quest_id)
        if actual is None:
            problems.append("quest_id_missing")
        elif quest_types is not None and actual != definition.quest_type:
            problems.append("quest_id_type_mismatch")
    phrase = None
    if not definition.quest_id:
        phrase = _ANY_KIND.search(body) or _COUNT_KIND.match(body)
        # An unread kind would otherwise widen the scope to every quest of the field type.
        if phrase is None and _ANY_KIND_MARKER in body:
            problems.append("kind_phrase_unresolved")
    if phrase is not None:
        kind = _native_kind(phrase["kind"])
        if kind is not None:
            if quest_types is None:
                quest_types = (kind,)
            elif kind not in quest_types:
                kind_conflict = True
        suffix, exact = phrase.groupdict().get("suffix"), phrase.groupdict().get("exact")
        if suffix or exact:
            if quest_types is None:
                problems.append("title_without_single_type")
            else:
                quest_ids = frozenset(quest_id for quest_id in source.playable_quests(quest_types[0])
                                      if any(_title_matches(title, suffix=suffix, exact=exact)
                                             for title in source.quest_titles(quest_id)))
                if not quest_ids:
                    problems.append("title_unresolved")
    restricted = {name: getattr(definition, name) for name, neutral in NEUTRAL_TEAM.items()
                  if getattr(definition, name) != neutral}
    member_limit = restricted.pop("member_count_condition", None)
    if member_limit is not None and not _MEMBER_LIMIT.search(body):
        problems.append("member_text_unresolved")
    team = None
    if restricted:
        if set(restricted) - set(TEAM_FIELDS) or len(restricted) != 1:
            problems.append("team_fields_unsupported")
        else:
            name, value = next(iter(restricted.items()))
            at_least, only = _AT_LEAST.search(body), _ONLY.search(body)
            matched = at_least or only
            card_ids = source.card_ids_with(TEAM_FIELDS[name], value)
            if matched is None:
                problems.append("team_mode_unresolved")
            elif matched["attr"] != source.system_text(definition.group_name_text_id):
                problems.append("team_attribute_name_mismatch")
            elif not card_ids:
                problems.append("team_cards_missing")
            else:
                team = TeamRule(TEAM_FIELDS[name], value, "at_least" if at_least else "only",
                                int(at_least["count"]) if at_least else None, card_ids)
    elif "属性" in body and "編成" in body:
        problems.append("team_text_without_fields")
    return QuestClearSpec(definition.condition_id, _claimant(quest_types), quest_types, quest_ids, team,
                          member_limit, tuple(problems), kind_conflict)


def _unresolved(definition, problem: str) -> QuestClearSpec:
    quest_types, quest_ids = _field_scope(definition)
    return QuestClearSpec(definition.condition_id, _claimant(quest_types), quest_types, quest_ids, problems=(problem,))


def _canonical(spec: QuestClearSpec) -> dict:
    data = asdict(spec)
    data["quest_ids"] = None if spec.quest_ids is None else sorted(spec.quest_ids)
    if spec.team is not None:
        data["team"]["card_ids"] = sorted(spec.team.card_ids)
    return data


class QuestClearSpecs:
    def __init__(self, specs: Mapping[int, QuestClearSpec]):
        if any(not isinstance(spec, QuestClearSpec) or spec.condition_id != key for key, spec in specs.items()):
            raise ValueError("Quest clear specs must be keyed by their condition identifiers")
        self._specs = MappingProxyType(dict(sorted(specs.items())))
        self.digest = hashlib.sha256(json.dumps([_canonical(spec) for spec in self._specs.values()],
                                                sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    @classmethod
    def build(cls, definitions: Iterable, source) -> QuestClearSpecs:
        specs = {}
        for definition in definitions:
            if definition.condition_type != QUEST_CLEAR_CONDITION:
                continue
            try:
                specs[definition.condition_id] = derive_spec(definition, source)
            except ProtocolError as error:
                # A missing or invalid resource leaves only the rows that need it unresolved.
                specs[definition.condition_id] = _unresolved(definition, f"resource:{error.code}")
            except (ValueError, KeyError, TypeError) as error:
                specs[definition.condition_id] = _unresolved(definition, f"data:{type(error).__name__}")
        return cls(specs)

    @property
    def ids(self) -> frozenset[int]:
        return frozenset(self._specs)

    def get(self, condition_id: int) -> QuestClearSpec | None:
        return self._specs.get(condition_id)

    def status(self) -> dict:
        specs = self._specs.values()
        return {"digest": self.digest, "specs": len(self._specs),
                "claimed_by": dict(sorted(Counter(spec.claimed_by for spec in specs).items())),
                "unresolved": {spec.condition_id: list(spec.problems) for spec in specs if not spec.resolved},
                "kind_conflicts": [spec.condition_id for spec in specs if spec.kind_conflict],
                "team_rules": sum(spec.team is not None for spec in specs)}
