import { AUTO_BACKUP_KEEP } from "../api.js";
import {
    badge, button, code, digest, fill, h, keyValues, notice, renderError, renderValue, section, table, toolbar,
} from "../diff-view.js";
import { commandPanel, planFlow } from "../plan-flow.js";
import {
    amount, bitBadges, fieldsList, offsetsOf, outcome, PAGE_LIMIT, pageLink, pagedRoute, pager, projectionNote,
    protectionBadges, viewPanel,
} from "../view-kit.js";

const ROSTER_VIEW = "units.roster";
const SQUADS_VIEW = "units.squads";
const LEDGERS_VIEW = "units.ledgers";
const OVERRIDES_VIEW = "units.overrides";
const DOMAIN = "units";
const ROUTE = "units";
const STATE_ROUTE = "state";
const BACKUPS_ROUTE = "backups";
const TOOLS_ROUTE = "tools";
const SALE_TOOL = Object.freeze({ tool: "unit_sale" });
const OVERRIDES_REMOVAL = "units.overrides.remove";
const PARAMS = Object.freeze({ roster: "roster" });
const ENTRIES_SHAPE = "entries";
const INVALID_SHAPE = "invalid";
const GUARD_LABELS = Object.freeze({ training: "合成", silver_trade: "银兑换", shop_growth: "商店成长" });
const SHAPE_LABELS = Object.freeze({ absent: "不存在", empty: "空值", entries: "有条目", invalid: "不是集合" });
const LEDGER_LABELS = Object.freeze({ evolution_rewards: "觉醒奖励账本", gifts: "好感奖励账本" });
const OVERRIDES_NOTE = "unit_dynamic_overrides 是原生客户端从不读取的临时覆盖，只有合成、银兑换和商店成长会检查它。";
const REMOVAL_NOTE = "移除是破坏性操作：需要逐字输入 REMOVE UNIT OVERRIDES，提交前会自动备份；"
    + `自动备份只保留最新 ${AUTO_BACKUP_KEEP} 份，需要长期保留原值时请先手动备份。`;
const REMOVAL_PENDING_NOTE = "上一次移除的结果未知。请先在下面重试同一次提交；确认结果之前不能开始新的预览。";
const SALE_NOTE = "units.sell 只按原生手动出售规则执行，不写游戏回执：客户端同时有在途的出售请求时会收到 sale_unit_missing 并一直重发，"
    + "请在客户端空闲时操作。出售报价可以先在预览工具页查看：";

export default Object.freeze({ title: "单位", order: 76, mount });

function mount(root, { signal, params, navigate }) {
    const offsets = offsetsOf(params, Object.values(PARAMS));
    const go = pagedRoute(navigate, ROUTE, offsets);
    const removalArea = h("div");
    const removalNote = h("div", { "aria-live": "polite" });
    let removalPending = false;
    const panels = [];
    const refresh = () => panels.forEach((panel) => panel.refresh());

    function startRemoval(operation) {
        if (removalPending) {
            // Another preview would draw a new operation ID while the last commit may already have been applied.
            fill(removalNote, notice("warn", REMOVAL_PENDING_NOTE));
            return;
        }
        fill(removalNote);
        const flow = planFlow({ commands: [{ operation, params: {} }], onCommitted: refresh,
            onPending: (waiting) => { removalPending = waiting; } });
        removalArea.replaceChildren(h("h3", { class: "subheading" }, "移除临时覆盖"), flow.element);
        flow.preview();
    }

    const overrides = viewPanel(OVERRIDES_VIEW, undefined, (data) => renderOverrides(data, startRemoval), { signal });
    const roster = viewPanel(ROSTER_VIEW, { offset: offsets[PARAMS.roster], limit: PAGE_LIMIT },
        (data) => renderRoster(data, go(PARAMS.roster)), { signal });
    const squads = viewPanel(SQUADS_VIEW, undefined, renderSquads, { signal });
    const ledgers = viewPanel(LEDGERS_VIEW, undefined, renderLedgers, { signal });
    panels.push(overrides, roster, squads, ledgers);
    root.append(
        section("临时单位覆盖", notice("info", OVERRIDES_NOTE), overrides.element, removalNote, removalArea),
        section("单位列表", roster.element),
        section("编队", squads.element),
        section("单位奖励账本", ledgers.element),
        section("命令", notice("info", SALE_NOTE, pageLink(TOOLS_ROUTE, "打开出售报价", SALE_TOOL)),
            commandPanel(DOMAIN, {
                signal, onCommitted: refresh, select: (command) => command.operation !== OVERRIDES_REMOVAL,
            })));
}

function guardText(guards, names, status) {
    return `${names.map((name) => `${GUARD_LABELS[name] ?? name} ${guards[name]}`).join("、")} 回 ${status}`;
}

function blockingNote(data) {
    const guards = data.guards ?? {};
    if (data.shape === ENTRIES_SHAPE) return `有条目时${guardText(guards, Object.keys(guards), 501)}。`;
    if (data.shape === INVALID_SHAPE) {
        return `值不是集合时${guardText(guards, ["silver_trade", "shop_growth"], 503)}；值为真时${guardText(guards, ["training"], 501)}。`;
    }
    return "当前值为空，不会阻断任何成长流程。";
}

function renderOverrides(data, startRemoval) {
    if (!data.present) return h("p", { class: "muted" }, "存档里没有 unit_dynamic_overrides。");
    return h("div", { dataset: { overrides: data.shape } },
        notice(data.blocking ? "danger" : "warn", badge(data.blocking ? "会阻断成长流程" : "存在但不阻断", data.blocking ? "danger" : "warn"),
            " ", blockingNote(data)),
        keyValues([
            ["形态", SHAPE_LABELS[data.shape] ?? data.shape],
            ["条目数", data.entries ?? "—"],
            ["字节", amount(data.bytes)],
            ["sha256", digest(data.sha256)],
            ["位置", code(data.pointer)],
        ]),
        data.truncated
            ? notice("info", "值太大，没有在这里展开：", pageLink(STATE_ROUTE, "在存档浏览页查看", { pointer: data.pointer }))
            : renderValue(data.value, { open: true, summary: "完整值" }),
        notice("warn", REMOVAL_NOTE, " ", pageLink(BACKUPS_ROUTE, "打开备份页")),
        toolbar(button("预览移除临时覆盖", { tone: "danger", action: "remove-overrides", onClick: () => startRemoval(data.removal) })));
}

function growthLimits(growth) {
    if (growth === null || growth === undefined) return "—";
    if (!growth.ok) return code(growth.error?.code ?? "?");
    return `Lv ${growth.level}/${growth.level_cap} · 好感上限 ${growth.love_limit} · 技能上限 ${growth.skill_level_max}`;
}

const ROSTER_COLUMNS = [
    { label: "GetIndex", cell: (entry) => entry.unit.GetIndex },
    { label: "UniqueID", cell: (entry) => entry.unit.UniqueID },
    { label: "CardID", cell: (entry) => entry.unit.CardID },
    { label: "ClassID", cell: (entry) => entry.unit.ClassID },
    { label: "Exp", cell: (entry) => amount(entry.unit.Exp) },
    { label: "Love", cell: (entry) => entry.unit.Love },
    { label: "SkillLevel", cell: (entry) => entry.unit.SkillLevel },
    { label: "_DisplayType", cell: (entry) => entry.unit._DisplayType },
    { label: "兵营", cell: (entry) => entry.flags?.barracks ?? "—" },
    { label: "标志", cell: (entry) => bitBadges(entry.flags?.bits) },
    { label: "保护", cell: (entry) => protectionBadges(entry.protections) },
    { label: "成长上限", cell: (entry) => growthLimits(entry.growth) },
    { label: "全部字段", cell: (entry) => renderValue(entry.unit, { summary: "字段" }) },
];

function renderRoster(data, go) {
    return h("div", {},
        keyValues([
            ["出击编队", fieldsList(data.battle)],
            ["最大 UniqueID", data.last_unique_id ?? "—"],
            ["保护检查", data.protections?.ok ? badge("可用", "ok") : renderError(data.protections?.error)],
            ["成长上限", data.growth?.ok ? badge("可用", "ok") : renderError(data.growth?.error)],
        ]),
        table(ROSTER_COLUMNS, data.units.items, { empty: "没有单位。" }),
        pager(data.units, go));
}

function renderSquad(squad) {
    return h("div", { class: "record", dataset: { squad: String(squad.group) } },
        h("h3", { class: "subheading" }, `编队 ${squad.group} `, squad.name ? code(squad.name) : null, " ",
            squad.selected ? badge("当前出击", "ok") : null, " ", squad.raid ? badge("突袭编队", "info") : null),
        keyValues([["Flags", squad.flags]]),
        table([
            { label: "小队", cell: (member) => member.team },
            { label: "位置", cell: (member) => member.slot },
            { label: "GetIndex", cell: (member) => member.GetIndex ?? "空" },
            { label: "CardID", cell: (member) => member.CardID ?? "—" },
        ], squad.members, { empty: "没有成员。" }));
}

function renderSquads(data) {
    return h("div", {},
        keyValues([["出击编队", fieldsList(data.battle)],
            ["旧编队已导入", data.legacy_squad_imported ? "是" : badge("否：编队保存会被拒绝", "warn")]]),
        h("div", { class: "records" }, data.squads.length ? data.squads.map(renderSquad)
            : h("p", { class: "muted" }, "没有编队。")));
}

function renderLedger(result) {
    return h("div", {}, projectionNote(result),
        result.persisted ? badge("已保存", "ok") : badge("尚未保存：显示下次写入时会建立的基线", "info"),
        fieldsList(result.ledger));
}

function renderLedgers(data) {
    return h("div", {}, Object.entries(data).map(([name, result]) => h("div", { class: "record", dataset: { ledger: name } },
        h("h3", { class: "subheading" }, LEDGER_LABELS[name] ?? name, " ", code(name)), outcome(result, renderLedger))));
}
