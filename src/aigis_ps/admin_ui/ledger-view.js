import { badge, code, digest, formatInstant, h, keyValues, list, notice, renderError, table } from "./diff-view.js";
import {
    counts, fieldsList, inline, labelled, offsetOf, PAGE_LIMIT, pager, recordsTable, tabs, viewPanel,
} from "./view-kit.js";

export const STATUS_LABELS = Object.freeze({
    delivered: { label: "已投递", tone: "ok" }, pending: { label: "待投递", tone: "warn" },
    scheduled: { label: "未开始", tone: "info" }, lapsed: { label: "已错过", tone: "muted" },
    unclaimed: { label: "未领取", tone: "warn" }, baseline: { label: "基线", tone: "muted" },
    bound: { label: "已绑定", tone: "info" }, frozen: { label: "已冻结", tone: "info" }, live: { label: "实时", tone: "info" },
    recorded: { label: "已记录", tone: "muted" }, settled: { label: "已结算", tone: "ok" },
    closed: { label: "已关闭", tone: "muted" }, tracking: { label: "跟踪中", tone: "info" },
    ineligible: { label: "不符合条件", tone: "muted" }, available: { label: "可用", tone: "ok" },
    consumed: { label: "已使用", tone: "muted" }, expired: { label: "已过期", tone: "muted" },
    unresolved: { label: "未确定", tone: "warn" }, voided: { label: "已作废", tone: "muted" },
});
export const DEFINITION_LABELS = Object.freeze({
    matching: { label: "一致", tone: "ok" }, changed: { label: "已变化", tone: "danger" },
    unavailable: { label: "无法核对", tone: "danger" }, unfrozen: { label: "未冻结", tone: "muted" },
});
const SECTION_LABELS = Object.freeze({
    rounds: "轮次", observation: "观测状态", purchases: "购买记录", progress: "进度账本", cooking: "料理账本",
    frozen_rules: "冻结的规则", passes: "通行证", automation: "自动领取", projection: "投影", catalog: "目录",
    totals: "累计", items: "物品", assault: "强袭挑战权", frozen: "已冻结的排名", live: "实时排名", notifications: "通知",
    repair: "修复", stamp_repair: "待发印章修复", cooking_repair: "料理门槛修复", pending_stamps: "待发印章容量",
});
const TIMESTAMP_FIELDS = new Set(["last_time_utc"]);
const LEDGER_PARAM = "ledger";
const OFFSET_PARAM = "offset";

export function keyText(key) {
    return code(Object.entries(key ?? {}).map(([name, value]) => `${name}=${value}`).join(" ") || "—");
}

export function definitionState(definition) {
    if (definition === null || definition === undefined) return "—";
    return h("span", {}, labelled(DEFINITION_LABELS, definition.state),
        definition.error ? [" ", code(definition.error.code)] : null);
}

function serviceState(service) {
    if (!service) return "—";
    return h("span", {}, code(service.name), " ",
        service.available ? badge("已接入", "ok") : badge("当前世代没有这个服务", "warn"),
        service.content_version ? [" ", digest(service.content_version)] : null);
}

function keyStates(keys) {
    const pairs = Object.entries(keys ?? {});
    if (!pairs.length) return "无";
    return h("span", {}, pairs.map(([key, present]) => [code(key), " ", present ? badge("已建立", "ok") : badge("未建立"), " "]));
}

function reasonCounts(reasons) {
    const pairs = Object.entries(reasons ?? {});
    return pairs.length ? h("span", {}, pairs.map(([reason, count]) => [code(reason), ` ${count} `])) : "无";
}

function ledgerField(name, value) {
    return TIMESTAMP_FIELDS.has(name) ? formatInstant(value) : inline(value);
}

const ENTRY_COLUMNS = [
    { label: "键", cell: (item) => keyText(item.key) },
    { label: "状态", cell: (item) => labelled(STATUS_LABELS, item.status) },
    { label: "原因", cell: (item) => (item.reason ? code(item.reason) : "—") },
    { label: "定义", cell: (item) => definitionState(item.definition) },
    { label: "详情", cell: (item) => inline(item.detail) },
];

const DIAGNOSIS_COLUMNS = [
    { label: "成员", cell: (item) => code(String(item.key)) },
    { label: "错误", cell: (item) => renderError(item.error) },
];

function renderSection(value) {
    if (Array.isArray(value)) return recordsTable(value, { empty: "没有记录。" });
    if (value === null || typeof value !== "object") return inline(value);
    const { ok, error, ...rest } = value;
    return h("div", {}, ok === false ? renderError(error) : null, Object.keys(rest).length ? fieldsList(rest) : null);
}

function renderRepair(value) {
    // The beginner rebase shares the repair section name but names no targets.
    if (!Array.isArray(value?.targets)) return renderSection(value);
    const { targets, ...rest } = value;
    return h("div", {}, renderSection(rest), recordsTable(targets, { empty: "没有待修复的条目。" }));
}

const SECTION_RENDERERS = Object.freeze({ repair: renderRepair, stamp_repair: renderRepair });

function sectionRenderer(renderers, name) {
    const owner = [renderers, SECTION_RENDERERS].find((source) => Object.hasOwn(source, name));
    return owner ? owner[name] : renderSection;
}

export function renderLedger(data, { go, note, sections: renderers = {} } = {}) {
    const { service, ledger = {}, summary = {}, entries, readback, diagnosis, ...sections } = data;
    const { keys, ok, error, ...fields } = ledger;
    // Entries and diagnosis share one offset, so paging follows the longer list.
    const [paged = null] = [entries, diagnosis].filter(Boolean).sort((left, right) => right.total - left.total);
    return h("div", { class: "ledger" },
        note ? notice("info", note) : null,
        keyValues([
            ["服务", serviceState(service)],
            ["账本键", keyStates(keys)],
            ["账本", ok === false ? badge("无法读取", "danger") : badge("有效", "ok")],
            ...Object.entries(fields).map(([name, value]) => [name, ledgerField(name, value)]),
            ["状态统计", counts(summary.statuses, STATUS_LABELS)],
            ["原因统计", reasonCounts(summary.reasons)],
            ["定义核对", counts(summary.definitions, DEFINITION_LABELS)],
            ["客户端回读请求号", list(readback?.requests?.map(String))],
            ["回读说明", list(readback?.none_reasons)],
        ]),
        ok === false ? renderError(error) : null,
        entries ? table(ENTRY_COLUMNS, entries.items, { empty: "没有记录。" }) : null,
        diagnosis ? [h("h3", { class: "subheading" }, "逐条诊断"),
            table(DIAGNOSIS_COLUMNS, diagnosis.items, { empty: "没有被拒的成员。" })] : null,
        go ? pager(paged, go, { unit: "条" }) : null,
        Object.entries(sections).map(([name, value]) => h("section", { dataset: { section: name } },
            h("h3", { class: "subheading" }, SECTION_LABELS[name] ?? name), sectionRenderer(renderers, name)(value))));
}

function ledgerKey(view) {
    return view.slice(view.indexOf(".") + 1);
}

export function ledgerTabs(ledgers, { route, notes = {}, sections = {}, signal, params, navigate }) {
    const keys = ledgers.map(([view]) => ledgerKey(view));
    const requested = params.get(LEDGER_PARAM);
    const current = keys.includes(requested) ? requested : keys[0];
    const [view] = ledgers[keys.indexOf(current)];
    const go = (ledger, offset) => navigate(route, {
        [LEDGER_PARAM]: ledger === keys[0] ? undefined : ledger, [OFFSET_PARAM]: offset || undefined,
    });
    const render = (data) => renderLedger(data, {
        go: (offset) => go(current, offset), note: notes[current], sections: sections[current],
    });
    const panel = viewPanel(view, { offset: offsetOf(params.get(OFFSET_PARAM)), limit: PAGE_LIMIT }, render, { signal });
    return Object.freeze({
        element: h("div", { dataset: { ledger: current } },
            tabs(ledgers.map(([name, label]) => [ledgerKey(name), label]), current, (ledger) => go(ledger, 0)), panel.element),
        refresh: panel.refresh,
    });
}
