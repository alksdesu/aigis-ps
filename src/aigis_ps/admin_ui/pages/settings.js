import { badge, code, fill, formatInstant, h, keyValues, loading, notice, section, table } from "../diff-view.js";
import { commandCatalog, commandPanel } from "../plan-flow.js";
import {
    bitBadges, commandPages, counts, FIELD_COLUMNS, labelled, offsetsOf, outcome, ownership, PAGE_LIMIT, pageLink,
    pagedRoute, pager, viewPanel,
} from "../view-kit.js";

const CONFIG_VIEW = "settings.config";
const EVENT_VIEW_VIEW = "settings.event_view";
const FLAGS_VIEW = "settings.flags";
const PERUSED_VIEW = "settings.perused";
const BACKGROUNDS_VIEW = "settings.backgrounds";
const CARD_STATUS_VIEW = "settings.card_status";
const DOMAIN = "settings";
const ROUTE = "settings";
const ITEMS_ROUTE = "items";
const BLESSING_FIELD = "_BattleGodBlessFilter";
const GLOBAL_CARD = 0;
const UNREACHED = 0;
const PARAMS = Object.freeze({ flags: "flags", perused: "perused", backgrounds: "backgrounds", cards: "cards" });
const PLACEMENTS = Object.freeze({
    config: Object.freeze(["config.sound.set", "config.damage_display.set", "config.sort.set", "config.auto_select.set",
        "guide.set"]),
    blessings: Object.freeze(["blessings.set"]),
    eventView: Object.freeze(["view_event.set", "view_event.clear"]),
    flags: Object.freeze(["flags.set"]),
    perused: Object.freeze(["perused.set"]),
    backgrounds: Object.freeze(["backgrounds.merge"]),
    cards: Object.freeze(["stories.set_unlock"]),
});
const AUDIT_LABELS = Object.freeze({
    unknown_name: { label: "背景名不在目录里", tone: "danger" },
    page_background: { label: "不是可选背景", tone: "warn" },
    fixed_outside_global: { label: "固定背景写在单卡行", tone: "warn" },
    non_normalized_key: { label: "卡片键未规范化", tone: "warn" },
    private_room_mismatch: { label: "私室背景不属于这张卡", tone: "danger" },
});
const BLESSING_NOTE = "blessings.set 修改加护筛选 FI（_BattleGodBlessFilter）和加护物品的 Enabled 标志；加护物品的持有数量和启用状态在物品页的持有物里。";
const EVENT_VIEW_NOTE = "EventViewRequest 是等待客户端播放的活动剧情请求；view_event.set 写入请求，view_event.clear 清空，两条命令共用这个字段。";
const FLAGS_NOTE = "服装商店的购买标志只有在 purchase_flag_ack 设为 true 时才能修改；标记为已购买却没有持有服装，会让这件商品再也买不到。";
const BACKGROUNDS_NOTE = "backgrounds.merge 只合并给出的行，SaveTime 由操作者明确填写（JST，格式 YYYY/MM/DD HH:MM:SS，不能晚于服务器时间）。"
    + "下面的诊断只读，控制台不会自动修复异常行。";
const CARD_NOTE = "A7 是剧情解锁位，由 stories.set_unlock 修改；B0–B4 是名声登记五个阶段的时刻，由仓库页的 fame.register 写入；A1/A3/A5 含义未确认，只显示。";
const NO_REQUEST_TEXT = "没有待播放的活动剧情。";
const PLACED_EMPTY = "命令目录里没有这一节的命令。";
const REST_EMPTY = "其余设置命令都已放在上面对应的分节里。";

export default Object.freeze({ title: "设置", order: 84, mount });

function mount(root, { signal, params, navigate }) {
    const offsets = offsetsOf(params, Object.values(PARAMS));
    const go = pagedRoute(navigate, ROUTE, offsets);
    const catalog = commandCatalog(signal);
    const body = h("div", {}, loading());
    root.append(body);
    // Ownership links need the command catalog, and a failed read still leaves the stored values readable.
    catalog().then(commandPages, () => new Map()).then((pages) => {
        if (!signal.aborted) fill(body, ...settingsSections({ signal, go, offsets, catalog, pages }));
    });
}

function settingsSections({ signal, go, offsets, catalog, pages }) {
    const panels = [];
    const refresh = () => panels.forEach((panel) => panel.refresh());
    const placed = new Set(Object.values(PLACEMENTS).flat());
    const commands = (name) => commandPanel(DOMAIN, {
        signal, catalog, onCommitted: refresh, empty: PLACED_EMPTY,
        select: (command) => PLACEMENTS[name].includes(command.operation),
    });
    const paged = (name) => ({ offset: offsets[PARAMS[name]], limit: PAGE_LIMIT });
    const blessing = h("div", {}, loading());
    const config = viewPanel(CONFIG_VIEW, undefined, (data) => renderConfig(data, pages), {
        signal,
        onData: (data) => fill(blessing, renderBlessing(data, pages)),
        onError: () => fill(blessing, h("p", { class: "muted" }, "加护筛选值随客户端设置一起读取，见上面的错误。")),
    });
    const eventView = viewPanel(EVENT_VIEW_VIEW, undefined, (data) => renderEventView(data, pages), { signal });
    const flags = viewPanel(FLAGS_VIEW, paged("flags"), (data) => renderFlags(data, go(PARAMS.flags)), { signal });
    const perused = viewPanel(PERUSED_VIEW, paged("perused"), (data) => renderPerused(data, go(PARAMS.perused)), { signal });
    const backgrounds = viewPanel(BACKGROUNDS_VIEW, paged("backgrounds"),
        (data) => renderBackgrounds(data, go(PARAMS.backgrounds)), { signal });
    const cards = viewPanel(CARD_STATUS_VIEW, paged("cards"), (data) => renderCards(data, pages, go(PARAMS.cards)), { signal });
    panels.push(config, eventView, flags, perused, backgrounds, cards);
    return [
        section("客户端设置", config.element, commands("config")),
        section("加护", notice("info", BLESSING_NOTE, " ", pageLink(ITEMS_ROUTE, "打开物品页")), blessing, commands("blessings")),
        section("活动剧情播放请求", notice("info", EVENT_VIEW_NOTE), eventView.element, commands("eventView")),
        section("通用标志", notice("info", FLAGS_NOTE), flags.element, commands("flags")),
        section("已读活动", perused.element, commands("perused")),
        section("背景", notice("info", BACKGROUNDS_NOTE), backgrounds.element, commands("backgrounds")),
        section("卡片剧情与名声登记", notice("info", CARD_NOTE), cards.element, commands("cards")),
        section("其他设置命令", commandPanel(DOMAIN, {
            signal, catalog, onCommitted: refresh, empty: REST_EMPTY, select: (command) => !placed.has(command.operation),
        })),
    ];
}

function fieldColumns(pages) {
    return [...FIELD_COLUMNS, { label: "归属命令", cell: ([, field]) => ownership(field, pages, ROUTE) }];
}

function renderConfig(data, pages) {
    return outcome(data, (config) => table(fieldColumns(pages), Object.entries(config.fields), { empty: "config 表没有字段。" }));
}

function renderBlessing(data, pages) {
    return outcome(data, (config) => table(fieldColumns(pages),
        Object.entries(config.fields).filter(([name]) => name === BLESSING_FIELD), { empty: "config 表里没有加护筛选字段。" }));
}

function renderEventView(data, pages) {
    return outcome(data, (pointer) => h("div", {},
        keyValues([
            ["EventViewRequest", pointer.value ? code(pointer.value) : NO_REQUEST_TEXT],
            ["归属命令", ownership(pointer, pages, ROUTE)],
        ]),
        pointer.request === null ? null : outcome(pointer.request, (request) => keyValues(Object.entries(request.fields)))));
}

function acquisition(recorded) {
    if (recorded === null || recorded === undefined) return "—";
    return recorded ? badge("已记录", "ok") : badge("未记录", "muted");
}

const PURCHASE_COLUMNS = [
    { label: "标志", cell: (product) => code(product.flag) },
    { label: "Value", cell: (product) => (product.stored ? product.value : h("span", { class: "muted" }, "未保存")) },
    { label: "商品", cell: (product) => product.shop_id },
    { label: "服装", cell: (product) => code(product.item_id) },
    { label: "支持", cell: (product) => (product.supported ? badge("支持", "ok") : badge("不支持", "warn")) },
    { label: "持有", cell: (product) => product.holding ?? "—" },
    { label: "取得记录", cell: (product) => acquisition(product.acquisition_recorded) },
    { label: "检查", cell: (product) => (product.value !== 0 && !product.holding ? badge("已标记购买但未持有", "danger") : "—") },
];

function renderFlags(data, go) {
    return h("div", {},
        outcome(data.flags, (flags) => h("div", {},
            table([
                { label: "ID", cell: (row) => code(row.ID) },
                { label: "Value", cell: (row) => row.Value },
                { label: "原生哈希", cell: (row) => row.hash },
            ], flags.items, { empty: "没有通用标志。" }),
            pager(flags, go))),
        h("h3", { class: "subheading" }, "服装商店购买标志"),
        outcome(data.purchases, (purchases) => table(PURCHASE_COLUMNS, purchases.products, { empty: "没有服装商品。" })));
}

function renderPerused(data, go) {
    return outcome(data.events, (events) => h("div", {},
        table([
            { label: "ID", cell: (row) => code(row.ID) },
            { label: "FLG", cell: (row) => row.FLG },
        ], events.items, { empty: "没有已读活动记录。" }),
        pager(events, go)));
}

function cardKey(cardId) {
    return cardId === GLOBAL_CARD ? h("span", {}, String(cardId), " ", badge("全局", "info")) : cardId;
}

function renderDiagnostics(diagnostics) {
    return h("div", {},
        keyValues([
            ["行数", diagnostics.rows],
            ["异常行", badge(String(diagnostics.abnormal_rows), diagnostics.abnormal_rows ? "warn" : "ok")],
            ["异常类型", counts(diagnostics.kinds, AUDIT_LABELS)],
        ]),
        diagnostics.truncated ? notice("info", `只列出前 ${diagnostics.findings.length} 条异常行。`) : null,
        table([
            { label: "CardID", cell: (finding) => cardKey(finding.CardID) },
            { label: "ConfigID", cell: (finding) => code(finding.ConfigID) },
            { label: "异常", cell: (finding) => h("span", {}, finding.kinds.map((kind) => [labelled(AUDIT_LABELS, kind), " "])) },
        ], diagnostics.findings, { empty: "没有异常行。" }));
}

function renderBackgrounds(data, go) {
    return h("div", {},
        outcome(data.rows, (rows) => h("div", {},
            table([
                { label: "CardID", cell: (row) => cardKey(row.CardID) },
                { label: "ConfigID", cell: (row) => code(row.ConfigID) },
                { label: "SaveTime", cell: (row) => row.SaveTime },
            ], rows.items, { empty: "没有背景记录。" }),
            pager(rows, go))),
        h("h3", { class: "subheading" }, "只读诊断"),
        outcome(data.diagnostics, renderDiagnostics));
}

function cardCell(cell) {
    if (cell === null || typeof cell !== "object") return cell ?? "—";
    if (Array.isArray(cell.bits)) return h("span", {}, String(cell.value), " ", bitBadges(cell.bits));
    if (!("utc" in cell)) return cell.value;
    if (cell.value === UNREACHED) return h("span", { class: "muted" }, "未达成");
    return cell.utc === null ? badge(String(cell.value), "warn") : formatInstant(cell.utc);
}

function renderCards(data, pages, go) {
    const names = Object.keys(data.fields ?? {});
    return h("div", {},
        h("h3", { class: "subheading" }, "字段归属"),
        table([
            { label: "字段", cell: ([name]) => code(name) },
            { label: "归属命令", cell: ([, entry]) => ownership(entry, pages, ROUTE) },
        ], Object.entries(data.fields ?? {}), { empty: "没有字段。" }),
        h("h3", { class: "subheading" }, "剧情位"),
        table([
            { label: "位", cell: (bit) => bit.bit },
            { label: "名称", cell: (bit) => code(bit.name) },
            { label: "可信度", cell: (bit) => bit.level },
        ], data.story_bits ?? [], { empty: "没有剧情位定义。" }),
        h("h3", { class: "subheading" }, "卡片记录"),
        outcome(data.rows, (rows) => h("div", {},
            table([{ label: "CardID", cell: (row) => row.CardID },
                ...names.map((name) => ({ label: name, cell: (row) => cardCell(row[name]) }))],
            rows.items, { empty: "没有卡片记录。" }),
            pager(rows, go))));
}
