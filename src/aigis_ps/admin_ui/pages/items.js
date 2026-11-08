import { badge, code, h, notice, renderError, section, table } from "../diff-view.js";
import { commandPanel } from "../plan-flow.js";
import { offsetOf, outcome, PAGE_LIMIT, pageLink, pager, tabs, viewPanel } from "../view-kit.js";

const HOLDINGS_VIEW = "items.holdings";
const CATALOG_VIEW = "items.catalog";
const DOMAIN = "items";
const ROUTE = "items";
const SETTINGS_ROUTE = "settings";
const PRESENTS_ROUTE = "presents";
const ALL_CLASSES = "all";
const ITEM_CLASSES = Object.freeze(["passive", "skin", "ownership", "recovery", "item_ticket", "emblem",
    "event_pass", "vote_ticket", "stack_zero", "unknown"]);
const CLASS_LABELS = Object.freeze({
    [ALL_CLASSES]: "全部", passive: "普通物品", skin: "服装", ownership: "持有证明", recovery: "回复道具",
    item_ticket: "召唤券", emblem: "称号", event_pass: "活动通行证", vote_ticket: "投票券", stack_zero: "不可持有",
    unknown: "未分类",
});
const PARAMS = Object.freeze({ holdings: "holdings", catalog: "catalog", class: "class" });
const ROUTE_NOTE = "“应使用的命令”一列给出改动这种物品的命令；空白表示它是礼包或不能直接改动。只能邮寄的物品要在礼物箱页用 presents.enqueue 发放。";

export default Object.freeze({ title: "物品", order: 68, mount });

function classOf(text) {
    return ITEM_CLASSES.includes(text) ? text : null;
}

function mount(root, { signal, params, navigate }) {
    const state = {
        holdings: offsetOf(params.get(PARAMS.holdings)), catalog: offsetOf(params.get(PARAMS.catalog)),
        class: classOf(params.get(PARAMS.class)),
    };
    const go = (changes) => {
        const next = { ...state, ...changes };
        navigate(ROUTE, { [PARAMS.holdings]: next.holdings || undefined, [PARAMS.catalog]: next.catalog || undefined,
            [PARAMS.class]: next.class ?? undefined });
    };
    const holdings = viewPanel(HOLDINGS_VIEW, { offset: state.holdings, limit: PAGE_LIMIT },
        (data) => renderHoldings(data, (offset) => go({ holdings: offset })), { signal });
    const filter = state.class === null ? {} : { item_class: state.class };
    const catalog = viewPanel(CATALOG_VIEW, { offset: state.catalog, limit: PAGE_LIMIT, ...filter },
        (data) => renderCatalog(data, (offset) => go({ catalog: offset })), { signal });
    const refresh = () => [holdings, catalog].forEach((panel) => panel.refresh());
    const classTabs = tabs([[ALL_CLASSES, CLASS_LABELS[ALL_CLASSES]], ...ITEM_CLASSES.map((name) => [name, CLASS_LABELS[name]])],
        state.class ?? ALL_CLASSES, (name) => go({ class: name === ALL_CLASSES ? null : name, catalog: 0 }));
    root.append(
        section("持有物",
            notice("info", "Enabled 是加护的启用状态，由设置页的 blessings.set 修改：", pageLink(SETTINGS_ROUTE, "打开设置页")),
            holdings.element),
        section("物品定义", classTabs, notice("info", ROUTE_NOTE, " ", pageLink(PRESENTS_ROUTE, "打开礼物箱页")),
            catalog.element),
        section("命令", commandPanel(DOMAIN, { signal, onCommitted: refresh })));
}

function classified(row) {
    return row.class === null || row.class === undefined ? badge("无法分类", "warn") : badge(CLASS_LABELS[row.class] ?? row.class);
}

function routeOf(row) {
    return row.command ? code(row.command) : "—";
}

function renderHoldings(data, go) {
    return h("div", {},
        data.classes?.ok === false ? renderError(data.classes.error) : null,
        outcome(data.items, (page) => h("div", {},
            table([
                { label: "ItemID", cell: (row) => code(row.ItemID) },
                { label: "数量", cell: (row) => row.Count },
                { label: "Stack", cell: (row) => row.stack ?? "—" },
                { label: "Enabled", cell: (row) => (row.Enabled ? badge("启用", "ok") : "—") },
                { label: "分类", cell: classified },
                { label: "应使用的命令", cell: routeOf },
            ], page.items, { empty: "没有持有物。" }),
            pager(page, go))));
}

function renderCatalog(data, go) {
    return h("div", {},
        data.classes?.ok === false ? renderError(data.classes.error) : null,
        outcome(data.items, (page) => h("div", {},
            table([
                { label: "ItemID", cell: (row) => code(row.ItemID) },
                { label: "EffectType", cell: (row) => row.EffectType },
                { label: "ReceiveAction", cell: (row) => row.ReceiveAction },
                { label: "持有", cell: (row) => row.held },
                { label: "Stack", cell: (row) => row.stack ?? "—" },
                { label: "分类", cell: classified },
                { label: "应使用的命令", cell: routeOf },
            ], page.items, { empty: "这个分类没有物品。" }),
            pager(page, go))));
}
