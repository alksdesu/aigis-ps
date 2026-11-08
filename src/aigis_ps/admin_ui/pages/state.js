import { api, watch } from "../api.js";
import {
    badge, button, code, formatInstant, h, keyValues, notice, present, renderError, renderValue, section, table,
    toolbar,
} from "../diff-view.js";
import { offsetOf, PAGE_LIMIT, pager } from "../view-kit.js";

const BROWSE_VIEW = "state.browse";
const FIELDS_VIEW = "state.fields";
const REFRESH_MS = 10000;
const CONTAINER_TYPES = new Set(["object", "array"]);

export default Object.freeze({ title: "存档浏览", order: 20, mount });

function escapeSegment(segment) {
    return String(segment).replaceAll("~", "~0").replaceAll("/", "~1");
}

function segments(pointer) {
    return pointer === "" ? [] : pointer.slice(1).split("/").map((part) => part.replaceAll("~1", "/").replaceAll("~0", "~"));
}

function child(pointer, segment) {
    return `${pointer}/${escapeSegment(segment)}`;
}

function mount(root, { signal, params, navigate }) {
    const pointer = params.get("pointer") ?? "";
    const offset = offsetOf(params.get("offset"));
    const go = (target, start = 0) => navigate("state", { pointer: target || undefined, offset: start || undefined });
    const meta = h("p", { class: "muted" }, "读取中…");
    const content = h("div");
    const browser = watch((etag) => api.view(BROWSE_VIEW, { pointer, offset, limit: PAGE_LIMIT }, { etag, signal }), {
        interval: REFRESH_MS, signal,
        onData: (view) => {
            meta.textContent = `修订号 ${view.revision} · 世代 ${view.generation_id} · 读取于 ${formatInstant(view.server_now?.jst)}`;
            content.replaceChildren(renderNode(view.data, go));
        },
        onError: (error) => content.replaceChildren(renderError(error)),
    });
    root.append(
        section("位置", breadcrumb(pointer, go), toolbar(button("刷新", { onClick: () => browser.refresh() })), meta, content),
        section("字段字典", fieldDictionary(signal)));
}

function breadcrumb(pointer, go) {
    const parts = segments(pointer);
    const crumbs = [button("/", { onClick: () => go("") })];
    parts.forEach((part, index) => {
        const target = parts.slice(0, index + 1).reduce((path, segment) => child(path, segment), "");
        crumbs.push(h("span", { class: "crumb-separator", "aria-hidden": "true" }, "/"),
            button(part, { onClick: () => go(target) }));
    });
    return h("nav", { class: "breadcrumb", "aria-label": "存档位置" }, crumbs);
}

function entryCell(pointer, item, go) {
    const segment = "key" in item ? item.key : item.index;
    return CONTAINER_TYPES.has(item.type) ? button(String(segment), { onClick: () => go(child(pointer, segment)) })
        : code(String(segment));
}

function renderNode(data, go) {
    const parts = [];
    if (data.parent !== null && data.parent !== undefined) {
        parts.push(toolbar(button("上一级", { onClick: () => go(data.parent) })));
    }
    if (Array.isArray(data.items)) {
        parts.push(table([
            { label: data.type === "array" ? "序号" : "键", cell: (item) => entryCell(data.pointer, item, go) },
            { label: "类型", cell: (item) => item.type },
            { label: "内容", cell: (item) => ("size" in item ? `${item.size} 项` : renderValue(item.value, { exact: true })) },
        ], data.items, { empty: "空容器" }), pager(data, (offset) => go(data.pointer, offset)));
    } else {
        parts.push(keyValues([["类型", data.type], ["值", renderValue(data.value, { exact: true, open: true })]]));
    }
    const ledgers = data.ledgers ?? (data.ledger ? [data.ledger] : []);
    if (ledgers.length) parts.push(h("h3", { class: "subheading" }, "账本校验"), renderLedgers(ledgers));
    return h("div", {}, parts);
}

function ledgerState(entry) {
    if (!entry.checked) return badge(entry.claimed ? "无读函数" : "未认领", "muted");
    return entry.valid ? badge("有效", "ok") : h("span", {}, badge("无效", "danger"), " ", entry.error?.message ?? "");
}

function renderLedgers(entries) {
    return table([
        { label: "顶层键", cell: (entry) => code(entry.key) },
        { label: "归属域", cell: (entry) => entry.domain ?? "—" },
        { label: "结构", cell: ledgerState },
    ], entries);
}

function fieldDictionary(signal) {
    const body = h("div");
    const details = h("details", { class: "value-tree" }, h("summary", {}, "展开读取字段字典与共享表字段归属"), body);
    let loaded = false;
    details.addEventListener("toggle", () => {
        if (!details.open || loaded) return;
        loaded = true;
        present(body, async () => (await api.view(FIELDS_VIEW, undefined, { signal })).data.data, renderFields);
    });
    return details;
}

function renderFields(dictionary) {
    const tables = Object.entries(dictionary.tables ?? {});
    return h("div", {},
        tables.length ? tables.map(([name, spec]) => h("details", { class: "value-tree" },
            h("summary", {}, code(name), " ", spec.verified ? badge("已核实", "ok") : badge("未核实", "warn")),
            spec.issue ? notice("warn", String(spec.issue)) : null,
            table([
                { label: "字段", cell: (field) => code(field.field) },
                { label: "线上名", cell: (field) => field.wire },
                { label: "类型", cell: (field) => field.value_type },
                { label: "归属命令", cell: (field) => field.owner ?? "无（只读）" },
            ], spec.fields ?? []))) : null,
        renderValue(dictionary, { summary: "完整字典" }));
}
