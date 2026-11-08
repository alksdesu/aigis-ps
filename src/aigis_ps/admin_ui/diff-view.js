import { errorHint } from "./api.js";

const PROPERTIES = new Set([
    "autocomplete", "checked", "colSpan", "disabled", "hidden", "htmlFor", "id", "inputMode", "name", "open",
    "placeholder", "readOnly", "required", "rows", "selected", "spellcheck", "tabIndex", "title", "type", "value",
]);
const OP_LABELS = Object.freeze({ add: "新增", remove: "删除", replace: "修改", reorder: "重排" });
const CHECK_LABELS = Object.freeze({
    preflight: "预检", restart_check: "重启兼容检查", serve_gate: "启动闸门", migration: "状态迁移",
    energy_clock: "能量时钟标记", battle: "进行中战斗", master_pin: "主表快照核对", audit: "审计记录体积",
});
const CHECK_FIELDS = new Set(["ok", "error", "readback"]);
const BUILD_STATES = Object.freeze({
    running: { label: "构建中", tone: "info" }, succeeded: { label: "已完成", tone: "ok" },
    failed: { label: "失败", tone: "danger" },
});
const DIFF_COLUMNS = ["变化", "位置", "之前", "之后"];
const SHORT_ID = 12;
const JST_FORMAT = new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    second: "2-digit", hourCycle: "h23",
});

export function h(tag, props = {}, ...children) {
    const element = document.createElement(tag);
    for (const [name, value] of Object.entries(props ?? {})) {
        if (value === undefined || value === null) continue;
        if (value === false && !PROPERTIES.has(name)) continue;
        if (name === "class") element.className = Array.isArray(value) ? value.filter(Boolean).join(" ") : value;
        else if (name === "dataset") Object.assign(element.dataset, value);
        else if (name === "on") Object.entries(value).forEach(([event, listener]) => element.addEventListener(event, listener));
        else if (PROPERTIES.has(name)) element[name] = value;
        else element.setAttribute(name, value === true ? "" : String(value));
    }
    append(element, children);
    return element;
}

function append(element, children) {
    for (const child of children) {
        if (child === undefined || child === null || child === false) continue;
        if (Array.isArray(child)) append(element, child);
        else element.append(isNode(child) ? child : String(child));
    }
}

export function fill(container, ...children) {
    // The DOM would print a skipped optional part as the text "null", so children go through the same filter as h().
    container.replaceChildren();
    append(container, children);
    return container;
}

export function isNode(value) {
    return value !== null && typeof value === "object" && "nodeType" in value;
}

export function button(label, { tone, action, onClick, disabled = false, type = "button" } = {}) {
    return h("button", {
        type, disabled, class: ["button", tone && `button-${tone}`], dataset: action ? { action } : undefined,
        on: onClick ? { click: onClick } : undefined,
    }, label);
}

export function badge(label, tone = "muted") {
    return h("span", { class: ["badge", `badge-${tone}`] }, label);
}

export function notice(tone, ...children) {
    return h("div", { class: ["notice", `notice-${tone}`], role: tone === "danger" ? "alert" : "status" }, ...children);
}

export function section(title, ...children) {
    return h("section", { class: "card" }, h("h2", { class: "card-title" }, title), ...children);
}

export function toolbar(...children) {
    return h("div", { class: "toolbar" }, ...children);
}

export function loading(text = "加载中…") {
    return h("p", { class: "loading", "aria-busy": "true" }, text);
}

export function code(text) {
    return h("code", { class: "code" }, String(text));
}

export function shortId(text) {
    return typeof text === "string" && text.length > SHORT_ID ? `${text.slice(0, SHORT_ID)}…` : String(text ?? "—");
}

export function digest(text) {
    return h("code", { class: "code", title: typeof text === "string" ? text : undefined }, shortId(text));
}

export function formatInstant(text) {
    if (typeof text !== "string") return "—";
    const moment = new Date(text);
    return Number.isNaN(moment.getTime()) ? text : `${JST_FORMAT.format(moment)} JST`;
}

export function formatSeconds(seconds) {
    return typeof seconds === "number" ? `${seconds.toFixed(1)} 秒` : "—";
}

export function list(items) {
    return Array.isArray(items) && items.length ? items.join("、") : "无";
}

export function keyValues(rows) {
    return h("dl", { class: "kv" }, rows.filter(([, value]) => value !== undefined).map(([label, value]) =>
        h("div", { class: "kv-row" }, h("dt", {}, label), h("dd", {}, isNode(value) ? value : renderValue(value)))));
}

export function table(columns, rows, { empty = "没有记录" } = {}) {
    if (!rows.length) return h("p", { class: "muted" }, empty);
    return h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, columns.map((column) => h("th", { scope: "col" }, column.label)))),
        h("tbody", {}, rows.map((row) => h("tr", {}, columns.map((column) => {
            const value = column.cell(row);
            return h("td", {}, isNode(value) ? value : renderValue(value));
        }))))));
}

export function renderValue(value, { open = false, summary, exact = false } = {}) {
    if (value === undefined) return h("span", { class: "value-missing" }, "—");
    if (value === null || typeof value !== "object") {
        const text = exact || value === null ? JSON.stringify(value) : String(value);
        return h("span", { class: ["value", `value-${value === null ? "null" : typeof value}`] }, text);
    }
    const size = Array.isArray(value) ? value.length : Object.keys(value).length;
    if (!size) return h("span", { class: "value value-empty" }, Array.isArray(value) ? "[]" : "{}");
    const label = summary ?? `${Array.isArray(value) ? "数组" : "对象"}（${size} 项）`;
    return h("details", { class: "value-tree", open }, h("summary", {}, label),
        h("pre", { class: "json" }, JSON.stringify(value, null, 2)));
}

function identity(value) {
    if (value === null || typeof value !== "object") return String(value);
    return Object.entries(value).map(([name, item]) => `${name}=${JSON.stringify(item)}`).join(", ");
}

function place(entry) {
    const parts = [entry.path || "/"];
    if (entry.key !== undefined) parts.push(`[${identity(entry.key)}]`);
    if (entry.label !== undefined) parts.push(`（${identity(entry.label)}）`);
    if (entry.field !== undefined) parts.push(entry.field);
    if (entry.index !== undefined) parts.push(`#${entry.index}`);
    return parts.join(" ");
}

function summarized(value) {
    if (value === null || typeof value !== "object") return null;
    if (value.truncated === true) return `已截断：${value.bytes} 字节，sha256 ${shortId(value.sha256)}`;
    if (typeof value.redacted === "string") return `报文已脱敏：${value.bytes} 字节，sha256 ${shortId(value.sha256)}`;
    return null;
}

function diffValue(entry, side) {
    if (!(side in entry)) return h("span", { class: "value-missing" }, "—");
    const summary = summarized(entry[side]);
    return summary === null ? renderValue(entry[side], { exact: true }) : h("span", { class: "value-omitted" }, summary);
}

export function renderDiff(diff) {
    const entries = Array.isArray(diff?.entries) ? diff.entries : [];
    const total = Number.isInteger(diff?.total) ? diff.total : entries.length;
    if (!total) return h("p", { class: "muted diff-empty" }, "没有状态变化");
    const rows = entries.map((entry) => h("tr", { class: ["diff-row", `diff-${entry.op}`] },
        h("td", {}, badge(OP_LABELS[entry.op] ?? entry.op)),
        h("td", { class: "diff-location" }, code(place(entry))),
        h("td", {}, diffValue(entry, "before")),
        h("td", {}, diffValue(entry, "after"))));
    const footer = diff?.truncated ? `共 ${total} 项，只显示 ${entries.length} 项（省略 ${diff.omitted} 项）` : `共 ${total} 项`;
    return h("div", { class: "diff" },
        h("div", { class: "table-wrap" }, h("table", { class: "table diff-table" },
            h("thead", {}, h("tr", {}, DIFF_COLUMNS.map((label) => h("th", { scope: "col" }, label)))),
            h("tbody", {}, rows))),
        h("p", { class: "muted" }, footer));
}

export function renderReadback(readback) {
    const pairs = Object.entries(readback ?? {});
    if (!pairs.length) return h("span", { class: "muted" }, "无");
    return h("ul", { class: "readback" }, pairs.map(([domain, requests]) =>
        h("li", {}, code(domain), " → ", requests.map(String).join("、"))));
}

function objectOrEmpty(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function renderCause(cause) {
    if (cause === undefined) return null;
    return typeof cause?.code === "string" ? h("div", { class: "error-cause" }, renderError(cause))
        : renderValue(cause, { summary: "原因" });
}

export function renderError(error) {
    const payload = error ?? {};
    const hint = payload.hint ?? errorHint(payload.code);
    const currentRevision = payload.currentRevision ?? payload.current_revision;
    const { diff, required, error: cause, ...rest } = objectOrEmpty(payload.details);
    const fields = Array.isArray(payload.fields) ? payload.fields : [];
    return h("div", { class: "error", role: "alert", dataset: { code: String(payload.code ?? "") } },
        h("p", { class: "error-head" }, code(payload.code ?? "error"), " ", payload.message ?? String(error)),
        hint ? h("p", { class: "error-hint" }, hint) : null,
        Number.isInteger(currentRevision) ? h("p", {}, `当前修订号：${currentRevision}`) : null,
        fields.length ? h("ul", { class: "error-fields" }, fields.map((item) => h("li", {},
            item.step === undefined ? null : `第 ${item.step + 1} 条 `, code((item.loc ?? []).join(".") || "/"), " ",
            item.msg ?? ""))) : null,
        Array.isArray(required) && required.length ? h("ul", { class: "error-fields" }, required.map((item) =>
            h("li", {}, `${item.operation}：`, code(item.phrase)))) : null,
        diff ? h("div", { class: "error-diff" }, h("p", {}, "差异："), renderDiff(diff)) : null,
        renderCause(cause),
        Object.keys(rest).length ? renderValue(rest, { summary: "详情" }) : null);
}

export function renderCheck(name, check) {
    if (check === undefined || check === null) return null;
    const passed = check.ok === true;
    const extra = Object.fromEntries(Object.entries(check).filter(([field]) => !CHECK_FIELDS.has(field)));
    return h("div", { class: ["check", passed ? "check-ok" : "check-failed"], dataset: { check: name } },
        h("p", { class: "check-head" }, badge(passed ? "通过" : "未通过", passed ? "ok" : "danger"), " ",
            CHECK_LABELS[name] ?? name),
        check.error ? renderError(check.error) : null,
        check.readback !== undefined ? h("div", { class: "check-body" }, h("span", {}, "客户端回读请求号："),
            renderReadback(check.readback)) : null,
        Object.keys(extra).length ? renderValue(extra, { summary: "详情" }) : null);
}

export function renderChecks(checks) {
    const entries = Object.entries(checks ?? {}).filter(([, check]) => check !== undefined && check !== null);
    return h("div", { class: "checks" }, entries.map(([name, check]) => renderCheck(name, check)));
}

export function buildBadge(state) {
    const known = Object.hasOwn(BUILD_STATES, state) ? BUILD_STATES[state] : { label: String(state), tone: "muted" };
    return badge(known.label, known.tone);
}

export function cursorList({ fetchPage, renderItems, empty, end, older }) {
    const records = h("div", { class: "records" });
    const footer = h("div");
    let turn = 0;
    async function show(cursor) {
        turn += 1;
        const mine = turn;
        if (cursor === null) records.replaceChildren(loading());
        footer.replaceChildren(loading());
        try {
            const page = await fetchPage(cursor);
            // A refresh started meanwhile owns the list, so an older answer must not mix into it.
            if (mine !== turn) return;
            const content = page.items.length ? renderItems(page.items)
                : cursor === null ? h("p", { class: "muted" }, empty) : null;
            if (cursor === null) fill(records, content);
            else append(records, [content]);
            fill(footer, page.next_cursor === null ? h("p", { class: "muted" }, end)
                : button(older, { action: "older", onClick: () => show(page.next_cursor) }));
        } catch (error) {
            if (mine !== turn || error?.name === "AbortError") return;
            if (cursor === null) records.replaceChildren();
            fill(footer, renderError(error), toolbar(button("重试", { action: "retry", onClick: () => show(cursor) })));
        }
    }
    return Object.freeze({ element: h("div", {}, records, footer), refresh: () => show(null) });
}

export async function present(container, load, render) {
    container.replaceChildren(loading());
    try {
        fill(container, render(await load()));
    } catch (error) {
        if (error?.name === "AbortError") return;
        // One-shot reads have no poll that would repair a failed answer later.
        fill(container, renderError(error),
            toolbar(button("重试", { action: "retry", onClick: () => present(container, load, render) })));
    }
}
