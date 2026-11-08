import { badge, button, code, h, keyValues, notice, renderError, section, table, toolbar } from "../diff-view.js";
import { commandPanel, planFlow } from "../plan-flow.js";
import {
    amount, inline, offsetsOf, outcome, PAGE_LIMIT, pagedRoute, pager, passed, recordsTable, viewPanel,
} from "../view-kit.js";

const MAILBOX_VIEW = "presents.mailbox";
const BACKLOG_VIEW = "presents.backlog";
const RECEIPTS_VIEW = "presents.receipts";
const DOMAIN = "presents";
const ROUTE = "presents";
const PARAMS = Object.freeze({ mails: "mails", receipts: "receipts" });
const SPLIT_OPERATION = "presents.split_ticket_mail";
const KIND_LABELS = Object.freeze({
    free_crystal: "免费结晶", unit: "单位", gold: "金币", charisma_recovery: "魅力回复", summon_ticket: "召唤券",
    selection_ticket: "自选券", vote_cooking: "料理", item: "物品", legacy_item: "旧式物品",
});
const ENQUEUE_NOTE = "presents.enqueue 只往礼物箱追加邮件；领取时整封放不下的邮件会留在箱里，不会部分领取。";
const BACKLOG_NOTE = "战斗结算时邮箱放不下的奖励先记在这里，之后某次结算发现邮箱有空位时再寄出；控制台不修改它。";
const RECEIPT_NOTE = "领取回执只保存最近的若干条，用于客户端重发同一请求时原样重放；完整报文已脱敏为摘要。";
const BUNDLED_NOTE = "这些邮件各装着多张券，游戏内领不了；其中多张召唤券的邮件还会让严格读取拒绝整个邮箱，这时多封必须在同一个计划里一起拆，单拆一封的预览不能提交。";
const SPLIT_PENDING_NOTE = "上一次拆分的结果未知。请先在下面重试同一次提交；确认结果之前不能开始新的预览。";

export default Object.freeze({ title: "礼物箱", order: 72, mount });

function mount(root, { signal, params, navigate }) {
    const offsets = offsetsOf(params, Object.values(PARAMS));
    const go = pagedRoute(navigate, ROUTE, offsets);
    const splitNote = h("div", { "aria-live": "polite" });
    const splitArea = h("div");
    const panels = [];
    const refresh = () => panels.forEach((panel) => panel.refresh());
    let splitPending = false;

    function startSplit(ids) {
        if (splitPending) {
            // Another preview would draw a new operation ID while the last commit may already have been applied.
            splitNote.replaceChildren(notice("warn", SPLIT_PENDING_NOTE));
            return;
        }
        splitNote.replaceChildren();
        const flow = planFlow({
            commands: ids.map((presentId) => ({ operation: SPLIT_OPERATION, params: { present_id: presentId } })),
            onCommitted: refresh, onPending: (waiting) => { splitPending = waiting; },
        });
        splitArea.replaceChildren(h("h3", { class: "subheading" }, "拆开多券邮件"), flow.element);
        flow.preview();
    }

    const mailbox = viewPanel(MAILBOX_VIEW, { offset: offsets[PARAMS.mails], limit: PAGE_LIMIT },
        (data) => renderMailbox(data, go(PARAMS.mails), startSplit), { signal });
    const backlog = viewPanel(BACKLOG_VIEW, undefined, renderBacklog, { signal });
    const receipts = viewPanel(RECEIPTS_VIEW, { offset: offsets[PARAMS.receipts], limit: PAGE_LIMIT },
        (data) => renderReceipts(data, go(PARAMS.receipts)), { signal });
    panels.push(mailbox, backlog, receipts);
    root.append(
        section("邮箱", mailbox.element, splitNote, splitArea),
        section("战斗奖励待寄队列", notice("info", BACKLOG_NOTE), backlog.element),
        section("领取回执", notice("info", RECEIPT_NOTE), receipts.element),
        section("命令", notice("info", ENQUEUE_NOTE), commandPanel(DOMAIN, { signal, onCommitted: refresh })));
}

function kindOf(kind) {
    return kind ? badge(KIND_LABELS[kind] ?? kind) : badge("未知类型", "warn");
}

const MAIL_COLUMNS = [
    { label: "PresentID", cell: (mail) => mail.PresentID },
    { label: "Type", cell: (mail) => mail.Type },
    { label: "种类", cell: (mail) => kindOf(mail.kind) },
    { label: "Content", cell: (mail) => mail.Content },
    { label: "目标", cell: (mail) => inline(mail.target) },
    { label: "说明", cell: (mail) => mail.Description || "—" },
    { label: "通知类别", cell: (mail) => mail.NotificationCategory },
];

function renderBundled(bundled, split) {
    if (bundled === null || bundled === undefined) return null;
    if (!bundled.ok) return renderError(bundled.error);
    if (!bundled.ids.length) return null;
    return notice("warn", BUNDLED_NOTE, " PresentID：", code(bundled.ids.join("、")), " ",
        toolbar(button(`全部拆开（${bundled.ids.length} 封）`, {
            tone: "danger", action: "split-bundled", onClick: () => split(bundled.ids),
        })));
}

function renderMailbox(data, go, split) {
    return h("div", {}, renderBundled(data?.bundled_tickets, split), outcome(data, (mailbox) => h("div", {},
        keyValues([
            ["邮件数", `${amount(mailbox.count)} / ${amount(mailbox.capacity)}`],
            ["最后的 PresentID", mailbox.last_present_id],
            ["通知类别", h("span", {}, code(mailbox.notification_category?.field ?? "—"), " ",
                badge("只读", "muted"), ` ${mailbox.notification_category?.reason ?? ""}`)],
        ]),
        mailbox.count >= mailbox.capacity ? notice("warn", "邮箱已满：新的奖励会转为待寄或被拒绝，请先在游戏内领取。") : null,
        table(MAIL_COLUMNS, mailbox.mails.items, { empty: "邮箱是空的。" }),
        pager(mailbox.mails, go))));
}

function renderBacklog(data) {
    return outcome(data, (backlog) => h("div", {},
        keyValues([
            ["待寄份数", backlog.copies],
            ["只读", backlog.read_only ? h("span", {}, badge("只读", "muted"), ` ${backlog.reason ?? ""}`) : "否"],
        ]),
        table([
            { label: "奖励", cell: (entry) => inline(entry.reward) },
            { label: "份数", cell: (entry) => entry.copies },
            { label: "每份邮件数", cell: (entry) => (entry.delivery?.ok ? entry.delivery.mails_per_copy : passed(entry.delivery)) },
        ], backlog.entries, { empty: "没有待寄的战斗奖励。" })));
}

function renderReceipts(data, go) {
    return outcome(data, (receipts) => h("div", {},
        keyValues([["回执格式版本", receipts.schema_version ?? "—"], ["保留上限", receipts.limit]]),
        recordsTable(receipts.receipts.items, { empty: "没有领取回执。" }),
        pager(receipts.receipts, go)));
}
