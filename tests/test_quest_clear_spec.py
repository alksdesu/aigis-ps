from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

import pytest

from aigis_ps.errors import ProtocolError
from aigis_ps.quest_clear_spec import NEUTRAL_TEAM, QuestClearSpec, QuestClearSpecs, TeamRule, derive_spec
from test_quest_service import bundle


TEXT = 100
GROUP = 200


class Source:
    def __init__(self, text, *, group=None, types=None, playable=None, titles=None, cards=None, failing=False):
        self.texts = {TEXT: text, GROUP: group}
        self.types = types or {}
        self.playable = playable or {}
        self.titles = titles or {}
        self.cards = cards or {}
        self.failing = failing

    def system_text(self, index):
        return self.texts.get(index)

    def quest_type(self, quest_id):
        return self.types.get(quest_id)

    def playable_quests(self, quest_type):
        if self.failing:
            raise ProtocolError("quest_asset_unavailable", "Mission resource is missing", 503)
        return self.playable.get(quest_type, ())

    def quest_titles(self, quest_id):
        return self.titles.get(quest_id, ())

    def card_ids_with(self, field, value):
        return self.cards.get((field, value), frozenset())


def definition(condition_id=1, **changes):
    values = {"condition_id": condition_id, "condition_type": 2, "condition_text": TEXT, "quest_type": -2,
              "quest_id": 0, "group_name_text_id": GROUP, **NEUTRAL_TEAM}
    values.update(changes)
    return SimpleNamespace(**values)


def clear(quest_id=101, quest_type=2, team=None):
    payload = {"quest_id": quest_id, "quest_type": quest_type, "stars": 3, "hard": False}
    if team is not None:
        payload["team"] = [{"card_id": card_id, "get_index": index + 1, "unique_id": index + 1}
                           for index, card_id in enumerate(team)]
    return payload


def test_fields_alone_fix_the_type_and_the_named_quest():
    story = derive_spec(definition(quest_type=0, quest_id=1108), Source("第1章「[QUEST_NAME]」をクリア", types={1108: 0}))
    assert (story.claimed_by, story.quest_types, story.quest_ids, story.resolved) == ("story", (0,), frozenset({1108}), True)
    guide = derive_spec(definition(quest_id=3007), Source("戦術指南クエスト「…」をクリア", types={3007: 4}))
    assert (guide.claimed_by, guide.quest_types, guide.quest_ids) == ("default", None, frozenset({3007}))
    assert derive_spec(definition(quest_type=0, quest_id=1108), Source("")).problems == ("quest_id_missing",)
    assert derive_spec(definition(quest_type=0, quest_id=1108), Source("", types={1108: 4})).problems == ("quest_id_type_mismatch",)
    assert derive_spec(definition(quest_type=-1), Source("")).problems == ("quest_type_unknown",)


@pytest.mark.parametrize("text,quest_type,types,claimed_by,conflict", [
    ("ミッション又はクエストを[COUNT]回クリア", -2, None, "default", False),
    ("いずれかのイベントヒストリーをクリア", -2, (16,), "history", False),
    ("いずれかのデイリー復刻ミッションを[COUNT]回クリア", -2, (12,), "type12", False),
    ("緊急ミッションを[COUNT]回クリア", 18, (18,), "empire", True),
    ("いずれかのプラクティスクエストをクリア", 15, (15,), "training", False),
    ("いずれかのイベントヒストリークリア", -2, (16,), "history", False),
    ("いずれかのデイリー復刻ミッション[COUNT]回クリア", -2, (12,), "type12", False),
    ("曜日ミッション[COUNT]回クリア", -2, (2,), "daily", False),
])
def test_text_kind_narrows_only_unset_types_and_records_conflicts(text, quest_type, types, claimed_by, conflict):
    spec = derive_spec(definition(quest_type=quest_type), Source(text))
    assert (spec.quest_types, spec.claimed_by, spec.kind_conflict, spec.resolved) == (types, claimed_by, conflict, True)


def test_title_phrases_select_playable_quests_by_suffix_or_exact_level():
    titles = {101: ("黄金の鎧　初級",), 102: ("黄金の鎧　中級",), 111: ("初級",), 120: ("聖霊救出 初級",),
              130: ("最初級",), 901: ("Lv.8",), 902: ("Lv.80",), 903: ("別名", "Lv.8")}
    source = Source("%c[ff0000]ビギナー任務2：%c[ffffff]\nいずれかの曜日ミッションの初級をクリア", titles=titles,
                    playable={2: (101, 102, 111, 120, 130), 9: (901, 902, 903)})
    daily = derive_spec(definition(10002), source)
    assert (daily.claimed_by, daily.quest_types, daily.quest_ids) == ("daily", (2,), frozenset({101, 111, 120}))
    source.texts[TEXT] = "いずれかの魔神降臨Lv.8をクリア"
    devil = derive_spec(definition(quest_type=9), source)
    assert (devil.claimed_by, devil.quest_ids) == ("devil", frozenset({901, 903}))
    source.texts[TEXT] = "いずれかの魔神降臨Lv.8クリア"
    assert derive_spec(definition(quest_type=9), source) == devil
    source.texts[TEXT] = "いずれかの曜日ミッションの初級クリア"
    assert derive_spec(definition(10002), source) == daily
    source.texts[TEXT] = "いずれかの曜日ミッションの超級をクリア"
    assert derive_spec(definition(), source).problems == ("title_unresolved",)
    source.texts[TEXT] = "いずれかのプラクティスクエストの初級をクリア"
    assert derive_spec(definition(), source).problems == ("title_without_single_type",)


def test_an_unread_kind_phrase_is_unresolved_instead_of_widening_the_scope():
    unread = derive_spec(definition(quest_type=9), Source("いずれかの魔神降臨Lv.1で勝利"))
    assert (unread.problems, unread.quest_ids, unread.evaluate(clear(901, 9))) == (("kind_phrase_unresolved",), None, False)
    assert derive_spec(definition(), Source("いずれかの曜日ミッションを制覇")).problems == ("kind_phrase_unresolved",)
    for text in ("いずれかの魔神降臨Lv.1を[MEMBER]人以下編成でクリア", "いずれかの曜日ミッションの初級を[MEMBER]人以下編成でクリア"):
        assert derive_spec(definition(member_count_condition=5), Source(text)).problems == ("kind_phrase_unresolved",)
    named =derive_spec(definition(quest_type=0, quest_id=1108), Source("いずれかの「[QUEST_NAME]」を制覇", types={1108: 0}))
    assert named.resolved and named.quest_ids == frozenset({1108})
    assert derive_spec(definition(), Source("ミッションを制覇")).resolved


def test_team_rules_need_one_genus_or_assign_field_and_the_group_name_in_the_text():
    cards = {("Genus", 101): frozenset({7, 8, 9}), ("Assign", 2): frozenset({11})}
    at_least = Source("お正月属性を4人編成で\nいずれかの魔神降臨Lv.1をクリア", group="お正月", cards=cards,
                      titles={901: ("Lv.1",)}, playable={9: (901,)})
    spec = derive_spec(definition(quest_type=9, genus_condition=101), at_least)
    assert spec.team == TeamRule("Genus", 101, "at_least", 4, frozenset({7, 8, 9})) and spec.resolved
    tower = "白の帝国属性のみ編成で 悪霊の塔「第1層」をクリア"
    only = Source(tower, group="白の帝国", cards=cards, types={14001: 14})
    spec = derive_spec(definition(quest_type=14, quest_id=14001, assign_condition=2), only)
    assert (spec.claimed_by, spec.team.mode, spec.team.count, spec.quest_ids) == ("tower", "only", None, frozenset({14001}))
    renamed = Source(tower, group="お正月", cards=cards, types={14001: 14})
    assert derive_spec(definition(quest_type=14, quest_id=14001, assign_condition=2),
                       renamed).problems == ("team_attribute_name_mismatch",)
    assert derive_spec(definition(quest_type=14, quest_id=14001, assign_condition=3), only).problems == ("team_cards_missing",)
    assert derive_spec(definition(quest_type=14, quest_id=14001, genus_condition=101, assign_condition=2),
                       only).problems == ("team_fields_unsupported",)
    assert derive_spec(definition(quest_type=14, quest_id=14001, kind_condition=0), only).problems == ("team_fields_unsupported",)
    assert derive_spec(definition(quest_type=14, quest_id=14001), only).problems == ("team_text_without_fields",)
    plain = Source("悪霊の塔「第1層」をクリア", group="白の帝国", cards=cards, types={14001: 14})
    assert derive_spec(definition(quest_type=14, quest_id=14001, assign_condition=2), plain).problems == ("team_mode_unresolved",)


def test_member_limit_is_supported_only_with_its_formation_text():
    limited = derive_spec(definition(member_count_condition=5), Source("ミッションを[MEMBER]人以下編成でクリア"))
    assert (limited.member_limit, limited.resolved) == (5, True)
    assert derive_spec(definition(member_count_condition=5), Source("ミッションをクリア")).problems == ("member_text_unresolved",)


def test_evaluation_covers_types_quests_and_counted_team_members():
    rule = TeamRule("Genus", 101, "at_least", 2, frozenset({7, 8}))
    spec = QuestClearSpec(1, "devil", (9,), frozenset({901}), rule)
    assert spec.evaluate(clear(901, 9, team=[7, 8, 3]))
    assert not spec.evaluate(clear(901, 9, team=[7, 3]))
    assert not spec.evaluate(clear(901, 9))
    assert not spec.evaluate(clear(902, 9, team=[7, 8])) and not spec.evaluate(clear(901, 0, team=[7, 8]))
    only = replace(spec, team=replace(rule, mode="only", count=None))
    assert only.evaluate(clear(901, 9, team=[7, 8, 8])) and not only.evaluate(clear(901, 9, team=[7, 3]))
    assert not only.evaluate(clear(901, 9, team=[]))
    capped = QuestClearSpec(2, "default", None, None, member_limit=2)
    assert capped.evaluate(clear(team=[1, 2])) and not capped.evaluate(clear(team=[1, 2, 3]))
    assert not capped.evaluate(clear())
    assert QuestClearSpec(3, "default", None, None).evaluate(clear())
    assert not replace(spec, problems=("title_unresolved",)).evaluate(clear(901, 9, team=[7, 8]))


def test_build_isolates_resource_failures_to_the_rows_that_need_them():
    source = Source("いずれかの曜日ミッションの初級をクリア", failing=True, types={1108: 0})
    rows = [definition(1), definition(2, quest_type=0, quest_id=1108), definition(3, condition_type=1)]
    specs = QuestClearSpecs.build(rows, source)
    assert specs.ids == {1, 2}
    assert specs.get(1).problems == ("resource:quest_asset_unavailable",) and specs.get(1).claimed_by == "default"
    assert specs.get(2).resolved and specs.get(3) is None
    status = specs.status()
    assert status["unresolved"] == {1: ["resource:quest_asset_unavailable"]} and status["specs"] == 2
    assert QuestClearSpecs.build(rows, source).digest == specs.digest


def test_digest_tracks_every_derived_value_and_keys_are_checked():
    rule = TeamRule("Genus", 101, "only", None, frozenset({7}))
    base = QuestClearSpecs({1: QuestClearSpec(1, "devil", (9,), frozenset({901}), rule)})
    widened = QuestClearSpecs({1: QuestClearSpec(1, "devil", (9,), frozenset({901}), replace(rule, card_ids=frozenset({7, 8})))})
    assert base.digest != widened.digest
    with pytest.raises(ValueError):
        QuestClearSpecs({2: QuestClearSpec(1, "default", None, None)})


def test_current_resources_derive_the_verified_quest_clear_specs(bundle):
    catalog = bundle.missions.catalog
    specs = QuestClearSpecs.build(catalog.definitions, bundle.quests.catalog)
    status = specs.status()
    assert status["specs"] == 229 and status["unresolved"] == {} and status["team_rules"] == 99
    assert status["claimed_by"] == {"daily": 1, "default": 18, "devil": 77, "empire": 4, "history": 1,
                                    "story": 105, "tower": 22, "training": 1}
    assert status["kind_conflicts"] == [642, 643, 644, 645]
    assert specs.get(10002).quest_ids == {101, 111, 121, 135, 138}
    assert specs.get(10008).quest_ids is None and specs.get(10008).quest_types == (15,)
    derived = [specs.get(key) for key in sorted(specs.ids)]
    modes = Counter((spec.team.mode, spec.team.count) for spec in derived if spec.team is not None)
    assert modes == {("at_least", 3): 3, ("at_least", 4): 21, ("at_least", 8): 9, ("only", None): 66}
    devil = [spec for spec in derived if spec.claimed_by == "devil"]
    leveled = [spec for spec in devil if spec.quest_ids is not None]
    assert len(devil) == len(leveled) == 77 and all(len(spec.quest_ids) == 18 for spec in leveled)
    assert all(bundle.quests.catalog.quest_type(quest_id) == 9 for spec in leveled for quest_id in spec.quest_ids)
    # 1169 omits を before クリア yet names the same Lv.1 quests as 1015.
    bride = specs.get(1169).quest_ids
    assert bride == specs.get(1015).quest_ids
    assert all("Lv.1" in bundle.quests.catalog.quest_titles(quest_id) for quest_id in bride)
    assert bundle.missions.progress.clear_specs.digest == specs.digest


class BrokenTitles(Source):
    def quest_titles(self, quest_id):
        raise ValueError("malformed title table")


def test_data_errors_in_one_row_leave_only_that_row_unresolved():
    source = BrokenTitles("いずれかの曜日ミッションの初級をクリア", playable={2: (101,)}, types={1108: 0})
    specs = QuestClearSpecs.build([definition(1), definition(2, quest_type=0, quest_id=1108)], source)
    assert specs.get(1).problems == ("data:ValueError",) and specs.get(2).resolved
