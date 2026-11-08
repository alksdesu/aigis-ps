import { api, ROUTES } from "../api.js";
import {
    badge, button, code, cursorList, digest, formatInstant, h, keyValues, list, renderDiff, renderValue, section,
    toolbar,
} from "../diff-view.js";

const PAGE_SIZE = 50;
const FAILURES = "failures";

export default Object.freeze({ title: "审计记录", order: 30, mount });

const TABS = Object.freeze({
    operations: { label: "管理操作", route: ROUTES.audit, render: renderOperation },
    [FAILURES]: { label: "失败记录", route: ROUTES.failures, render: renderFailure },
});

function mount(root, { signal, params, navigate }) {
    const current = params.get("tab") === FAILURES ? FAILURES : "operations";
    const tab = TABS[current];
    const records = cursorList({
        fetchPage: (cursor) => api.get(tab.route, { query: { cursor, limit: PAGE_SIZE }, signal }),
        renderItems: (items) => items.map(tab.render),
        empty: "还没有记录。", end: "已经是最早的记录。", older: "更早的记录",
    });
    const tabs = toolbar(...Object.entries(TABS).map(([name, entry]) => button(entry.label, {
        tone: name === current ? "primary" : undefined,
        onClick: () => navigate("audit", { tab: name === FAILURES ? name : undefined }),
    })), button("刷新", { action: "refresh", onClick: () => records.refresh() }));
    root.append(section(tab.label, tabs, records.element));
    records.refresh();
}

function operationNames(record) {
    return (record.operations ?? []).map((item) => item.operation).join("、") || record.kind;
}

function revisions(record) {
    return record.revision_before === record.revision_after
        ? `修订号 ${record.revision_before}（未变化）` : `修订号 ${record.revision_before} → ${record.revision_after}`;
}

function renderOperation(record) {
    return h("details", { class: "record", dataset: { sequence: String(record.sequence) } },
        h("summary", {}, badge(record.kind), " ", `#${record.sequence}`, " ", formatInstant(record.created_at), " ",
            code(operationNames(record)), " ", revisions(record)),
        keyValues([
            ["操作 ID", code(record.op_id)],
            ["操作者", record.actor],
            ["目标", list(record.targets)],
            ["世代", record.generation_id ? digest(record.generation_id) : "—"],
            ["计划摘要", record.plan_digest ? digest(record.plan_digest) : "—"],
            ["状态摘要", h("span", {}, digest(record.state_sha256_before), " → ", digest(record.state_sha256_after))],
        ]),
        h("h3", { class: "subheading" }, "操作参数"), renderValue(record.operations, { open: true }),
        h("h3", { class: "subheading" }, "差异"), renderDiff(record.diff),
        h("h3", { class: "subheading" }, "结果"), renderValue(record.result),
        h("h3", { class: "subheading" }, "内容版本"), renderValue(record.content_versions));
}

function renderFailure(record) {
    return h("details", { class: "record record-failed", dataset: { failure: String(record.id) } },
        h("summary", {}, badge(String(record.status), "danger"), " ", `#${record.id}`, " ", formatInstant(record.created_at),
            " ", badge(record.kind), " ", code(record.error_code)),
        keyValues([
            ["说明", record.message],
            ["操作 ID", record.op_id ? code(record.op_id) : "—"],
            ["操作者", record.actor],
        ]),
        renderValue(record.detail, { summary: "错误详情" }));
}
