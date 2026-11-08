from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import logging
from types import MappingProxyType

from .errors import ProtocolError
from .present_backlog import require_deliverable
from .quest_catalog import TREASURE_SLOTS
from .quest_modes import (
    BOOSTS, MAX_BOOST, MODE_PROGRESS, UNBOOSTED, MissionWindow, ModePolicy, ModeServices, ScoreContext, ScoreResult,
    SettleContext, SettleOutcome, StartContext, require_row_values,
)
from .subjugation_rules import (
    BEST_LIMIT, DEFAULT_RULES, PROGRESS_NAME, SUBJUGATION_TYPE, BoostPolicy, KillsPolicy, crossed_rewards, higher,
    pack, present, recorded_scores, settlement_basis, watermark, watermark_mismatch, with_watermark,
)


# Score quests pay through their reward ladder alone, so a Treasure code marks an unaudited resource.
NO_TREASURE = 0
# Score quests never earn a star bit (461E80), so a star reward type other than crystal marks an unaudited resource.
CRYSTAL_STAR_REWARD = 0
NO_FIXED_TEAM = 0
ROW_VALUES = MappingProxyType({
    "Type": SUBJUGATION_TYPE, **{f"Treasure{slot}": NO_TREASURE for slot in range(1, TREASURE_SLOTS + 1)},
    "BonusType": CRYSTAL_STAR_REWARD, "UnitList": NO_FIXED_TEAM,
})
# A tier crossed once shows whether its present form can be mailed at all.
SINGLE_CROSSING = 1
LOCAL_RULES = MappingProxyType({
    "windows": "resource_window_unless_mission_window_rules_set_the_batch",
    "first_quests": "start_button_less_quests_refused_without_a_synthesized_record",
    "cooperative_missions": "refused_as_unsupported",
    "score": "ceil_float32_kills_times_clear_or_complete_factor_raised_to_one",
    "max_rank": "mission_capped_total_above_the_low_12_bits_holding_the_capped_best",
    "best_limit": BEST_LIMIT,
    "kills_policies": [policy.value for policy in KillsPolicy],
    "boost_policies": [policy.value for policy in BoostPolicy],
    "default_rules": DEFAULT_RULES.to_dict(),
    "rules_source": "subjugation_catalog_status",
    "boost": f"requested_boost_up_to_{MAX_BOOST}_unless_ignored",
    "rewards": "event_mission_catalog_ladder_tiers_of_conditions_4_6_7",
    "repeating_rewards": "type_7_tiers_pay_only_named_items_or_single_gold_and_legacy_presents_the_result_page_counts",
    "reward_presents": "one_present_per_crossed_tier_with_repeats_merged",
    "reward_preflight": "tiers_a_run_can_reach_must_fit_the_native_cells_and_be_mailable_before_charging",
    "watermark": f"{MODE_PROGRESS}.{PROGRESS_NAME}.mission_best_and_total",
    "missing_watermark": "starts_from_the_records_without_mailing_tiers_they_reached",
    "settlement_basis": "higher_of_the_watermark_and_the_records_before_the_run_so_raised_records_mail_no_gap",
    "watermark_updates": "never_lowered_so_lowered_records_never_mail_a_settled_tier_again",
    "mismatches": "marked_by_describe_and_logged_at_settlement_without_blocking",
})
log = logging.getLogger("aigis_ps")


def _catalog(services: ModeServices):
    catalog = services.subjugation
    if catalog is None:
        raise ProtocolError("quest_subjugation_unavailable", "Subjugation battles require the subjugation catalog", 409)
    return catalog


def _presents(services: ModeServices):
    if services.presents is None:
        raise ProtocolError("quest_present_unavailable", "Subjugation rewards require the present service", 409)
    return services.presents


def _records_before(records: Mapping, quest_id: int, previous: Mapping | None) -> dict:
    restored = dict(records)
    if previous is None:
        restored.pop(quest_id, None)
    else:
        restored[quest_id] = previous
    return restored


class SubjugationPolicy(ModePolicy):
    name = PROGRESS_NAME
    quest_types = (SUBJUGATION_TYPE,)
    enabled = True
    gate_quest = 1004
    # settle mails the condition 4, 6 and 7 ladder tiers that make up the score family.
    event_reward_families = frozenset({"score"})

    def validate_row(self, row, catalog) -> None:
        require_row_values(row, ROW_VALUES, "Subjugation quest")

    def check_start(self, context: StartContext) -> MissionWindow:
        mission = super().check_start(context)
        catalog = _catalog(context.services)
        quest = catalog.quest(context.quest.quest_id)
        if not quest.start_button:
            raise ProtocolError("quest_locked", "Subjugation quests without a StartButtonID cannot start", 409)
        presents = _presents(context.services)
        recorded = catalog.mission_score(context.records, quest.mission_id)
        before = settlement_basis(watermark(context.state, quest.mission_id), recorded)
        # Settlement crosses from that basis to the records a run leaves, so only the records grow the reach.
        reach = catalog.accumulate(recorded.best, recorded.total, quest.per_run_cap, self.start_boost(context),
                                   quest.limit)
        for tier, reward in crossed_rewards(catalog, quest.mission_id, before, reach):
            try:
                require_deliverable(presents, reward)
            except ProtocolError as error:
                raise ProtocolError("quest_subjugation_reward_unsupported", f"Subjugation reward {tier.incentive} "
                                    f"of mission {quest.mission_id} cannot be mailed", 501) from error
        return mission

    def start_boost(self, context: StartContext) -> int:
        catalog = _catalog(context.services)
        requested = None if context.request is None else context.request.boost
        if catalog.rules.boost_policy is BoostPolicy.IGNORE or requested is None:
            return UNBOOSTED
        if type(requested) is not int or requested not in BOOSTS:
            raise ProtocolError("quest_boost_invalid",
                                f"Subjugation starts accept Boost.V from {UNBOOSTED} to {MAX_BOOST}")
        return requested

    def score_result(self, context: ScoreContext) -> ScoreResult:
        catalog = _catalog(context.services)
        quest = catalog.quest(context.quest.quest_id)
        scored = catalog.score(quest, context.request.kills)
        current = recorded_scores(context.record)
        best, total = catalog.accumulate(current.best, current.total, scored.score, context.boost, quest.limit)
        return ScoreResult(pack(best, total), scored.score, {
            "expected": quest.expected, "factor": scored.factor, "kills_scored": scored.kills_scored,
            "kills_over_expected": scored.kills_over_expected, "best": best, "total": total,
            "rules": catalog.rules.to_dict(), "rules_version": catalog.rules.content_version})

    def settle(self, context: SettleContext) -> SettleOutcome:
        catalog = _catalog(context.services)
        quest = catalog.quest(context.quest.quest_id)
        records = {row["QuestID"]: row for row in context.state["tables"]["quest_records"]}
        saved = watermark(context.state, quest.mission_id)
        recorded = catalog.mission_score(_records_before(records, quest.quest_id, context.previous_record),
                                         quest.mission_id)
        before = settlement_basis(saved, recorded)
        if watermark_mismatch(saved, recorded):
            log.warning("Subjugation mission %d settles quest %d from best %d and total %d: its watermark holds %d "
                        "and %d while its records held %d and %d", quest.mission_id, quest.quest_id, *before, *saved,
                        *recorded)
        # The watermark never falls, so records lowered later cannot mail a settled tier again.
        reached = higher(before, catalog.mission_score(records, quest.mission_id))
        if reached == saved:
            return SettleOutcome(context.state)
        rewards = crossed_rewards(catalog, quest.mission_id, before, reached)
        return SettleOutcome(with_watermark(context.state, quest.mission_id, reached),
                             presents=tuple(reward for _, reward in rewards))

    def local_rules(self) -> dict:
        return deepcopy(dict(LOCAL_RULES))

    def undeliverable_rewards(self, catalog, services: ModeServices) -> tuple[dict, ...]:
        subjugation, presents, undeliverable = _catalog(services), _presents(services), []
        description = subjugation.description()
        for mission_id in subjugation.mission_ids:
            if not subjugation.mission(mission_id).supported:
                continue
            for tier in subjugation.ladder(mission_id):
                try:
                    require_deliverable(presents, present(tier, SINGLE_CROSSING, description))
                except ProtocolError as error:
                    undeliverable.append({"mission_id": mission_id, **tier.to_dict(), "reason": error.code})
        return tuple(undeliverable)


POLICY = SubjugationPolicy()
