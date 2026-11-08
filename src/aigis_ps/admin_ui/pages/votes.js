import { formatInstant, notice, renderError, section } from "../diff-view.js";
import { ledgerTabs } from "../ledger-view.js";
import { commandPanel } from "../plan-flow.js";
import { recordsTable } from "../view-kit.js";

const HISTORY_VIEW = "votes.history";
const REWARDS_VIEW = "votes.rewards";
const TICKETS_VIEW = "votes.tickets";
const RANKINGS_VIEW = "votes.rankings";
const EXPIRY_VIEW = "votes.expiry";
const ASSAULT_VIEW = "votes.assault";
const COOKING_VIEW = "votes.cooking";
const RECEIPTS_VIEW = "votes.receipts";
const DOMAIN = "votes";
const ROUTE = "votes";
const LEDGERS = Object.freeze([
    [HISTORY_VIEW, "投票记录"], [REWARDS_VIEW, "投票奖励"], [TICKETS_VIEW, "投票券"], [RANKINGS_VIEW, "排名"],
    [EXPIRY_VIEW, "投票券过期"], [ASSAULT_VIEW, "强袭挑战权"], [COOKING_VIEW, "投票料理"], [RECEIPTS_VIEW, "投票回执"],
]);
const NOTICE_NOTE = "通知一节列出 116 已提交通知的永久记录；读取时不初始化、不复制存档。";
const NO_NOTICES = "没有通知记录。";
const NOTES = Object.freeze({
    expiry: NOTICE_NOTE,
    assault: "投票活动结束后，没用完的强袭挑战权（earned − spent）按 rate 折算成金币邮件：pending 表示还没寄出，"
        + `邮箱放不下时 reason 记下原因并在之后的结算里重试；settled 表示已寄出。这个账本只读。${NOTICE_NOTE}`,
    cooking: NOTICE_NOTE,
    receipts: "客户端重发同一次投票时按这里保存的回执原样重放，不会重复投票。",
});
const SECTIONS = Object.freeze({
    expiry: Object.freeze({ notifications: noticeTable("物品 ID") }),
    assault: Object.freeze({ notifications: noticeTable("活动编号") }),
    cooking: Object.freeze({ notifications: noticeTable("投票券物品 ID") }),
});
const LEDGER_NOTE = "投票账本由客户端的投票和结算写入，这里只读；定义变化或已结算的投票券重新出现库存时，"
    + "用命令一节的 votes.* 迁移命令改写（破坏性，需要确认短语）。";

export default Object.freeze({ title: "投票", order: 96, mount });

function mount(root, { signal, params, navigate }) {
    const ledger = ledgerTabs(LEDGERS, { route: ROUTE, notes: NOTES, sections: SECTIONS, signal, params, navigate });
    root.append(
        section("投票账本", notice("info", LEDGER_NOTE), ledger.element),
        section("命令", commandPanel(DOMAIN, { signal, onCommitted: ledger.refresh })));
}

function noticeInstant(value) {
    return formatInstant(typeof value === "number" ? new Date(value).toISOString() : value);
}

function noticeTable(label) {
    return ({ ok, error, records }) => {
        if (ok === false) return renderError(error);
        const rows = Object.entries(records ?? {}).map(([key, record]) =>
            ({ [label]: key, ...record, notified_at: noticeInstant(record.notified_at) }));
        return recordsTable(rows, { empty: NO_NOTICES });
    };
}
