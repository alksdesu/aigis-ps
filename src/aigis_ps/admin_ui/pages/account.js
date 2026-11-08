import { badge, formatInstant, h, keyValues, notice, section, table } from "../diff-view.js";
import { commandPanel } from "../plan-flow.js";
import {
    amount, anchorsTable, clockedLedger, FIELD_COLUMNS, fieldsList, outcome, pageLink, projectionNote, viewPanel,
} from "../view-kit.js";

const SUMMARY_VIEW = "account.summary";
const PROJECTION_VIEW = "account.projection";
const DOMAIN = "account";
const CURRENCIES_ROUTE = "currencies";
const RANK_MISSING = "等级超出等级表，无法给出魅力上限与经验门槛。";
const CALENDAR_MARKED = "业务日历：JST 04:00 换日（已标记）";
const CALENDAR_LEGACY = "旧存档：未写业务日历标记（只报告，不影响使用）";

export default Object.freeze({ title: "账号", order: 60, mount });

function mount(root, { signal }) {
    const summary = viewPanel(SUMMARY_VIEW, undefined, renderSummary, { signal });
    const projection = viewPanel(PROJECTION_VIEW, undefined, renderProjection, { signal });
    const panels = [summary, projection];
    const refresh = () => panels.forEach((panel) => panel.refresh());
    root.append(
        section("账号状态", summary.element),
        section("能量与日历投影", projection.element),
        section("命令", commandPanel(DOMAIN, { signal, onCommitted: refresh })));
}

function renderRank(rank) {
    if (rank === null || rank === undefined) return notice("warn", RANK_MISSING);
    return keyValues([
        ["魅力上限", amount(rank.charisma_max)],
        ["魅力容量（含扩展）", amount(rank.charisma_capacity)],
        ["本级经验起点", amount(rank.experience_floor)],
        ["升到下一级的经验", amount(rank.experience_next)],
    ]);
}

function renderGameParam(param) {
    return h("div", {},
        h("h3", { class: "subheading" }, "等级"), renderRank(param.rank),
        h("h3", { class: "subheading" }, "能量时钟锚点"), anchorsTable(param.anchors),
        h("h3", { class: "subheading" }, "game_param 字段"),
        table(FIELD_COLUMNS, Object.entries(param.fields ?? {})));
}

function renderBusinessCalendar(calendar) {
    return calendar.marker === null ? notice("info", CALENDAR_LEGACY)
        : h("div", {}, h("p", {}, CALENDAR_MARKED), fieldsList(calendar.marker));
}

function renderSummary(data) {
    return h("div", {},
        outcome(data.game_param, renderGameParam),
        notice("info", "金币与结晶余额在货币页查看和修改：", pageLink(CURRENCIES_ROUTE, "打开货币页")),
        h("h3", { class: "subheading" }, "结晶消费账本"), outcome(data.spending, clockedLedger),
        h("h3", { class: "subheading" }, "月度扩展魅力账本"), outcome(data.capacity, clockedLedger),
        h("h3", { class: "subheading" }, "能量时钟标记"), outcome(data.energy_clock, (clock) => fieldsList(clock.marker)),
        h("h3", { class: "subheading" }, "业务日历标记"), outcome(data.business_calendar, renderBusinessCalendar));
}

function renderEnergy(energy) {
    return h("div", {},
        projectionNote(energy),
        keyValues([
            ["投影时间", formatInstant(energy.as_of?.jst)],
            ["回复间隔", `${energy.recovery_seconds} 秒/点`],
            ["当前魅力", `${amount(energy.charisma?.before)} → ${amount(energy.charisma?.after)}`],
        ]),
        h("h3", { class: "subheading" }, "结算前锚点"), anchorsTable(energy.anchors?.before),
        h("h3", { class: "subheading" }, "结算后锚点"), anchorsTable(energy.anchors?.after),
        h("h3", { class: "subheading" }, "结算后的等级"), renderRank(energy.rank),
        h("h3", { class: "subheading" }, "结算后的月度扩展魅力"), outcome(energy.capacity, clockedLedger));
}

function renderProjection(data) {
    const calendar = data.calendar ?? {};
    return h("div", {},
        keyValues([
            ["JST 逻辑日", calendar.logical_day ?? "—"],
            ["原生取得日", calendar.acquisition_day === undefined ? "—"
                : h("span", {}, String(calendar.acquisition_day), " ", badge(calendar.acquisition_date ?? "—"))],
        ]),
        outcome(data.energy, renderEnergy));
}
