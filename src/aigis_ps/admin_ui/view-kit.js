import { api, ROUTES, watch } from "./api.js";
import {
    badge, button, code, fill, formatInstant, h, keyValues, loading, notice, renderError, table, toolbar,
} from "./diff-view.js";

export const REFRESH_MS = 15000;
export const PAGE_LIMIT = 100;
export const GOLD_LIMIT = 99999999;
export const CRYSTAL_LIMITS = Object.freeze({ free: 1000, paid: 1000, total: 2000 });
export const COMMAND_PAGES = Object.freeze({
    account: "account", battle: "battle", currencies: "currencies", events: "events", items: "items", letstd: "letstd",
    presents: "presents", progress: "missions", settings: "settings", storage: "storage", units: "units", votes: "votes",
});
const SOURCE_LABELS = Object.freeze({ default: "代码默认值", set: "配置集", cli: "命令行覆盖" });
const SEMANTIC_LABELS = Object.freeze({
    new_save: "只影响新存档", first_baseline: "首次建立基线", frozen_per_operation: "按操作冻结", time_accumulating: "时间累积",
    eligibility: "资格判定", resource: "资源",
});
export const ANCHOR_LABELS = Object.freeze({ Charisma: "魅力", ActionPoint: "体力" });
export const PROTECTION_LABELS = Object.freeze({
    prince: "王子", locked: "已锁定", squad: "编队成员", current_squad: "当前编队", secretary: "秘书",
    battle_reservation: "战斗预留",
});
const NUMBER_FORMAT = new Intl.NumberFormat("zh-CN");
const INLINE_FIELDS = 6;
const PROJECTION_TEXT = "按服务器时间估算，没有保存任何内容。";
const STALLED_TEXT = "读取失败后不会自动重试，点击「重试」再读一次。";
const UNKNOWN_BIT = "unknown";
const ignore = () => {};

export function routeHash(name, params = {}) {
    const pairs = Object.entries(params).filter(([, value]) => value !== undefined && value !== null);
    return pairs.length ? `#/${name}?${new URLSearchParams(pairs)}` : `#/${name}`;
}

export function pageLink(name, label, params) {
    return h("a", { href: routeHash(name, params), dataset: { link: name } }, label);
}

export function offsetOf(text) {
    const value = Number(text);
    return Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

export function offsetsOf(params, names) {
    return Object.fromEntries(names.map((name) => [name, offsetOf(params.get(name))]));
}

export function pagedRoute(navigate, route, offsets) {
    // The first page needs no parameter, so the address names only the lists that are paged forward.
    return (key) => (offset) => navigate(route, Object.fromEntries(Object.entries({ ...offsets, [key]: offset })
        .map(([name, value]) => [name, value || undefined])));
}

export function pager(page, go, { unit = "项" } = {}) {
    if (page === null || page === undefined || (page.next_offset === null && page.offset === 0)) return null;
    const last = Math.min(page.offset + page.limit, page.total);
    return toolbar(
        button("上一页", { disabled: page.offset === 0, onClick: () => go(Math.max(0, page.offset - page.limit)) }),
        h("span", { class: "muted" }, `第 ${page.offset + 1}–${last} ${unit}，共 ${page.total} ${unit}`),
        button("下一页", { disabled: page.next_offset === null, onClick: () => go(page.next_offset) }));
}

export function sourceLabel(source) {
    return SOURCE_LABELS[source] ?? source;
}

export function semanticLabel(name) {
    return SEMANTIC_LABELS[name] ?? name;
}

export function semantics(entry) {
    const fields = Object.entries(entry.field_semantics ?? {});
    const base = semanticLabel(entry.time_semantics);
    return fields.length ? `${base}；${fields.map(([name, value]) => `${name}：${semanticLabel(value)}`).join("，")}` : base;
}

export function amount(value) {
    return Number.isSafeInteger(value) ? NUMBER_FORMAT.format(value) : value;
}

export function labelled(labels, name) {
    const known = Object.hasOwn(labels, name) ? labels[name] : { label: String(name), tone: "muted" };
    return badge(known.label, known.tone);
}

export function counts(values, labels) {
    const pairs = Object.entries(values ?? {});
    if (!pairs.length) return "无";
    return h("span", {}, pairs.map(([name, count]) => [labelled(labels, name), ` ${count} `]));
}

export function outcome(result, render) {
    if (result === null || result === undefined) return h("span", { class: "value-missing" }, "—");
    return result.ok ? render(result) : renderError(result.error);
}

export function passed(check) {
    if (check === null || check === undefined) return "—";
    return check.ok ? badge("通过", "ok") : h("span", {}, badge("不通过", "danger"), " ", code(check.error?.code ?? "?"),
        check.error?.message ? ` ${check.error.message}` : null);
}

export function projectionNote(result) {
    return result?.projected ? notice("info", badge("投影", "info"), " ", PROJECTION_TEXT) : null;
}

export function protectionBadges(names) {
    if (names === null || names === undefined) return "—";
    if (!names.length) return badge("无", "ok");
    return h("span", {}, names.map((name) => [badge(PROTECTION_LABELS[name] ?? name, "warn"), " "]));
}

export function bitBadges(bits) {
    const raised = (bits ?? []).filter((bit) => bit.set);
    if (!raised.length) return "无";
    return h("span", {}, raised.map((bit) => [
        badge(bit.name ?? `bit ${bit.bit}`, bit.level === UNKNOWN_BIT ? "warn" : "info"), " "]));
}

export function fieldDecoding(field) {
    if (field.invalid) return badge("无法解码", "danger");
    if ("date" in field) return field.date ?? "空";
    if ("datetime" in field) return field.datetime === null ? "空" : formatInstant(field.datetime);
    if ("time" in field) return field.time;
    return Array.isArray(field.bits) ? bitBadges(field.bits) : "—";
}

export const FIELD_COLUMNS = Object.freeze([
    { label: "字段", cell: ([name]) => code(name) },
    { label: "线上名", cell: ([, field]) => field.wire },
    { label: "值", cell: ([, field]) => field.value },
    { label: "解码", cell: ([, field]) => fieldDecoding(field) },
]);

function decodedCell(field) {
    if (field === null || typeof field !== "object") return field;
    const decoding = fieldDecoding(field);
    return decoding === "—" ? field.value : h("span", {}, String(field.value), " ", decoding);
}

export function decodedTable(rows, { empty } = {}) {
    const records = Array.isArray(rows) ? rows : [];
    const names = [...new Set(records.flatMap((row) => Object.keys(row ?? {})))];
    return table(names.map((name) => ({ label: name, cell: (row) => decodedCell(row?.[name]) })), records, { empty });
}

export function anchorsTable(anchors) {
    return table([
        { label: "时钟", cell: ([prefix]) => ANCHOR_LABELS[prefix] ?? prefix },
        { label: "秒", cell: ([, anchor]) => anchor.seconds },
        { label: "时刻", cell: ([, anchor]) => (anchor.invalid ? badge("无效", "danger") : formatInstant(anchor.jst)) },
    ], Object.entries(anchors ?? {}), { empty: "没有时钟锚点。" });
}

function primitive(value) {
    return value === null || typeof value !== "object";
}

export function inline(value) {
    if (value === null || typeof value !== "object" || Array.isArray(value)) return value;
    const pairs = Object.entries(value);
    // Short records of plain values read better on one line than as a collapsed tree.
    if (!pairs.length || pairs.length > INLINE_FIELDS || !pairs.every(([, item]) => primitive(item))) return value;
    return code(pairs.map(([name, item]) => `${name}=${JSON.stringify(item)}`).join(" "));
}

export function recordsTable(rows, { empty, cell = inline } = {}) {
    const records = Array.isArray(rows) ? rows : [];
    const names = [...new Set(records.flatMap((row) => (row !== null && typeof row === "object" ? Object.keys(row) : [])))];
    return table(names.map((name) => ({ label: name, cell: (row) => cell(row?.[name]) })), records, { empty });
}

export function fieldsList(record) {
    return keyValues(Object.entries(record ?? {}).map(([name, value]) => [name, inline(value)]));
}

export function barracksUsage(usage) {
    const rows = usage.capacities.map((capacity, index) => ({ index, capacity, count: usage.counts[index] }));
    return h("div", {},
        keyValues([["第一兵营可用格数", amount(usage.primary_available)]]),
        table([
            { label: "兵营", cell: (row) => row.index },
            { label: "容量", cell: (row) => amount(row.capacity) },
            { label: "单位数", cell: (row) => amount(row.count) },
        ], rows, { empty: "没有兵营。" }));
}

export function clockedLedger(result) {
    return h("div", {}, keyValues([["最后结算", formatInstant(result.last_time_utc)]]), fieldsList(result.ledger));
}

export function viewPanel(name, params, render, { signal, interval = REFRESH_MS, onData = ignore, onError = ignore } = {}) {
    const meta = h("span", { class: "muted" });
    const body = h("div", {}, loading());
    // Until an answer carries a validator the view may be computed on every read, so failed reads wait for a retry.
    let live = true;
    const watcher = watch(async (etag) => {
        const result = await api.view(name, params, { etag, signal });
        if (!result.notModified) live = result.etag === null;
        return result;
    }, {
        interval, signal,
        // A view answered without a validator is computed on every read, so only an explicit refresh reads it again.
        repeat: () => !live,
        retry: () => !live,
        onData: (envelope) => {
            fill(meta, `修订号 ${envelope.revision} · 读取于 ${formatInstant(envelope.server_now?.jst)}`,
                live ? [" ", badge("实时计算：点击刷新重新计算", "info")] : null);
            fill(body, render(envelope.data, envelope));
            onData(envelope.data);
        },
        onError: (error, { waiting }) => {
            fill(body, renderError(error), waiting ? [notice("warn", STALLED_TEXT),
                toolbar(button("重试", { action: "retry-view", onClick: () => watcher.refresh() }))] : null);
            onError(error);
        },
    });
    const refresh = button("刷新", { action: "refresh-view", onClick: () => watcher.refresh() });
    return Object.freeze({
        element: h("div", { class: "view", dataset: { view: name } }, toolbar(refresh, meta), body),
        refresh: () => watcher.refresh(),
    });
}

export function viewCatalog(signal) {
    let pending = null;
    return () => {
        // A failed read is dropped so that a retry asks the server again.
        pending ??= api.get(ROUTES.views, { signal })
            .then(({ views }) => new Map(views.map((entry) => [entry.name, entry])))
            .catch((error) => {
                pending = null;
                throw error;
            });
        return pending;
    };
}

export function newestRead() {
    let latest = 0;
    return (load) => {
        const reading = ++latest;
        const current = () => reading === latest;
        return async () => {
            try {
                const value = await load(current);
                if (current()) return value;
            } catch (error) {
                if (current()) throw error;
            }
            // A newer read owns the area, so an overtaken answer is dropped like an aborted read.
            throw new DOMException("A newer read replaced this answer", "AbortError");
        };
    };
}

export function tabs(entries, current, choose) {
    return toolbar(...entries.map(([name, label]) => button(label, {
        tone: name === current ? "primary" : undefined, action: `tab-${name}`, onClick: () => choose(name),
    })));
}

export function commandPages(catalog) {
    return new Map(catalog.commands.map((command) => [command.operation, COMMAND_PAGES[command.domain]]));
}

export function ownership(entry, pages, current) {
    const owner = entry?.owner ?? null;
    const page = owner === null ? undefined : pages.get(owner);
    const link = owner === null ? null : page === undefined || page === current ? code(owner) : pageLink(page, owner);
    return h("span", { dataset: { owner: owner ?? "" } }, link,
        entry?.read_only ? [link ? " " : null, badge("只读", "muted"), entry.reason ? ` ${entry.reason}` : null] : null);
}
