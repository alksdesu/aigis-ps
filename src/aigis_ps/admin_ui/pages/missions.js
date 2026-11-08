import { badge, code, digest, formatInstant, h, keyValues, list, notice, renderValue, section, table } from "../diff-view.js";
import { commandPanel } from "../plan-flow.js";
import {
    clockedLedger, fieldsList, labelled, offsetsOf, outcome, PAGE_LIMIT, pagedRoute, pager, projectionNote, recordsTable,
    viewPanel,
} from "../view-kit.js";

const MISSIONS_VIEW = "progress.missions";
const RANK_REWARDS_VIEW = "progress.rank_rewards";
const LOGIN_REWARDS_VIEW = "progress.login_rewards";
const LOGIN_COOKING_VIEW = "progress.login_cooking";
const DOMAIN = "progress";
const ROUTE = "missions";
const SEQUENTIAL_PERIOD = 100;
const PARAMS = Object.freeze({ missions: "missions" });
const MISSION_STATUS = Object.freeze({
    0: { label: "进行中", tone: "muted" }, 1: { label: "可领取", tone: "ok" }, 2: { label: "已领取", tone: "info" },
});
const MISSIONS_NOTE = "任务进度按服务器时间实时计算，与客户端读取任务列表时看到的一致，不会保存；顺序任务只有当前一步显示进度。"
    + "missions.mark_read 和客户端的已读操作一样，只标记已完成且未读的任务。";
const RANK_NOTE = "邮箱放不下的等级奖励记为待投递；rank_rewards.retry_pending 在邮箱有空位时投递它们。";
const BEGINNER_RULE = "新手奖励总数";
const COOKING_RULE = "料理阈值";

export default Object.freeze({ title: "任务与奖励", order: 88, mount });

function mount(root, { signal, params, navigate }) {
    const offsets = offsetsOf(params, Object.values(PARAMS));
    const go = pagedRoute(navigate, ROUTE, offsets);
    const missions = viewPanel(MISSIONS_VIEW, { offset: offsets[PARAMS.missions], limit: PAGE_LIMIT },
        (data) => renderMissions(data, go(PARAMS.missions)), { signal });
    const rank = viewPanel(RANK_REWARDS_VIEW, undefined, renderRankRewards, { signal });
    const login = viewPanel(LOGIN_REWARDS_VIEW, undefined, renderLoginRewards, { signal });
    const cooking = viewPanel(LOGIN_COOKING_VIEW, undefined, renderLoginCooking, { signal });
    const panels = [missions, rank, login, cooking];
    const refresh = () => panels.forEach((panel) => panel.refresh());
    root.append(
        section("任务", notice("info", MISSIONS_NOTE), missions.element),
        section("等级奖励", notice("info", RANK_NOTE), rank.element),
        section("登录奖励", login.element),
        section("登录料理", cooking.element),
        section("命令", commandPanel(DOMAIN, { signal, onCommitted: refresh })));
}

function period(row) {
    if (row.period_type !== SEQUENTIAL_PERIOD) return row.period_type;
    return h("span", {}, String(row.period_type), " ", badge("顺序", "info"));
}

const MISSION_COLUMNS = [
    { label: "ID", cell: (row) => row.ID },
    { label: "状态", cell: (row) => labelled(MISSION_STATUS, row.Status) },
    { label: "已读", cell: (row) => (row.unread ? badge("未读", "warn") : row.Read) },
    { label: "进度", cell: (row) => row.Progress },
    { label: "周期类型", cell: period },
    { label: "页签", cell: (row) => row.tab },
];

function renderMissions(data, go) {
    return h("div", {},
        outcome(data.missions, (missions) => h("div", {},
            projectionNote(missions),
            keyValues([["计算时间", formatInstant(missions.as_of?.jst)]]),
            table(MISSION_COLUMNS, missions.items, { empty: "当前没有开放的任务。" }),
            pager(missions, go))),
        renderValue(data.history ?? null, { summary: "任务请求记录" }));
}

function definitionCurrent(current) {
    if (current === null || current === undefined) return badge("无法核对", "muted");
    return current ? badge("一致", "ok") : badge("已变化", "danger");
}

const RANK_COLUMNS = [
    { label: "BonusID", cell: (entry) => entry.bonus_id },
    { label: "等级", cell: (entry) => entry.rank },
    { label: "定义", cell: (entry) => h("span", {}, digest(entry.definition), " ", definitionCurrent(entry.definition_current)) },
    { label: "邮件", cell: (entry) => (entry.mail_ids.length ? entry.mail_ids.join(", ") : "—") },
    { label: "原因", cell: (entry) => entry.reason ?? "—" },
];

function renderRankRewards(data) {
    return outcome(data, (ledger) => h("div", {},
        keyValues([
            ["基线等级", ledger.baseline_rank],
            ["已观测等级", ledger.observed_rank],
            ["定义核对", ledger.definitions_checked ? badge("已核对", "ok") : badge("当前世代没有等级奖励目录", "warn")],
        ]),
        h("h3", { class: "subheading" }, "待投递"),
        table(RANK_COLUMNS, ledger.pending, { empty: "没有待投递的等级奖励。" }),
        h("h3", { class: "subheading" }, "已投递"),
        table(RANK_COLUMNS, ledger.delivered, { empty: "没有已投递的等级奖励。" })));
}

function ruleWarning(rules, rule) {
    if (!rules?.ok || !rules.changed) return null;
    return notice("warn", `存档冻结的${rule}与当前世代不同：完成 `, code(rules.repair),
        ` 之前 ${list(rules.refused_operations)} 被拒，客户端弹出通信错误并原包重发。`);
}

function checkFields({ ok, ...fields }) {
    return fieldsList(fields);
}

function renderLoginRewards(data) {
    return h("div", {},
        ruleWarning(data.rules, BEGINNER_RULE),
        outcome(data.ledger, clockedLedger),
        h("h3", { class: "subheading" }, "待补的登录印章"),
        outcome(data.pending_stamps, (stamps) => recordsTable(stamps.entries, { empty: "没有待补的登录印章。" })),
        h("h3", { class: "subheading" }, `${BEGINNER_RULE}核对`),
        outcome(data.rules, checkFields),
        h("h3", { class: "subheading" }, "待发印章容量"),
        outcome(data.stamp_capacity, checkFields));
}

function renderLoginCooking(data) {
    return h("div", {},
        ruleWarning(data.rules, COOKING_RULE),
        outcome(data, clockedLedger),
        h("h3", { class: "subheading" }, `${COOKING_RULE}核对`),
        outcome(data.rules, checkFields));
}
