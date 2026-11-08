import { api } from "../api.js";
import {
    badge, button, code, formatInstant, h, keyValues, notice, present, renderError, renderValue, section, table, toolbar,
} from "../diff-view.js";
import { paramsProblems } from "../plan-flow.js";
import { schemaForm } from "../schema-form.js";
import {
    amount, anchorsTable, barracksUsage, fieldsList, GOLD_LIMIT, inline, newestRead, outcome, PAGE_LIMIT, passed, pager,
    projectionNote, protectionBadges, recordsTable, tabs, viewCatalog,
} from "../view-kit.js";

const UNIT_SALE_VIEW = "tools.unit_sale";
const SILVER_TRADE_VIEW = "tools.silver_trade";
const GROWTH_SHOP_VIEW = "tools.growth_shop";
const EVOLUTION_VIEW = "tools.evolution";
const TRAINING_VIEW = "tools.training";
const ORB_EXCHANGE_VIEW = "tools.orb_exchange";
const UNIT_SKINS_VIEW = "tools.unit_skins";
const ITEM_TICKET_VIEW = "tools.item_ticket";
const PRESENTS_VIEW = "tools.presents";
const ENERGY_CLOCK_VIEW = "tools.energy_clock";
const UNIT_CAPACITY_VIEW = "tools.unit_capacity";
const FAME_VIEW = "tools.fame";
const EMBLEMS_VIEW = "tools.emblems";
const BACKGROUNDS_VIEW = "tools.backgrounds";
const SUMMON_POOLS_VIEW = "tools.summon_pools";
const SUMMON_POOL_VIEW = "tools.summon_pool";
const ROUTE = "tools";
const TOOL_PARAM = "tool";
const PARAMS_INVALID = "admin_params_invalid";
const VIEW_UNKNOWN = "admin_view_unknown";
const PAGE_FIELDS = Object.freeze(["offset", "limit"]);
const PROJECTION_FIELDS = new Set(["projected", "persisted", "note"]);
const MATERIAL_NOTE = "materials 的每一项是兵营单位（source=barracks 加 get_index）或仓库库存（source=silver_house / fairy_farm 加 card_id）；"
    + "保护检查只针对兵营单位，仓库库存按数量扣除。";
const TOOLS_NOTE = "预览按服务器时间在每次点击时重新计算，不保存任何内容，也不会从缓存取结果。";
const SALE_STORAGE_NOTE = "只能出售第一兵营的单位；「可出售」为否的单位会被 units.sell 拒绝。";
const GROWTH_STORAGE_NOTE = "觉醒和合成的目标只能是第一兵营的单位；兵营素材在接入额外兵营时可以来自已解锁的兵营，否则只能来自第一兵营。「可用」为否时服务端会拒绝。";
const TRADE_STORAGE_NOTE = "银兑换的素材和成长商店的目标只能是第一兵营的单位；「可用」为否时服务端会拒绝。";
const STORAGE_FIELDS = new Set(["barracks", "storage_supported"]);
const TOOLS = Object.freeze([
    { view: UNIT_SALE_VIEW, label: "出售报价", render: renderUnitSale,
        note: `按原生手动出售规则报价；“出售会被拒的保护”不为空的单位会被 units.sell 拒绝。金币上限是 ${amount(GOLD_LIMIT)}，超出部分会被丢弃。` },
    { view: SILVER_TRADE_VIEW, label: "银兑换报价", render: renderSilverTrade,
        note: "material_get_index 指定作为素材的兵营单位；兵营一节检查兑换得到的单位是否还有空位。" },
    { view: GROWTH_SHOP_VIEW, label: "成长商店报价", render: renderGrowthShop,
        note: "指定 target_get_index 时同时列出成长后的字段变化。" },
    { view: EVOLUTION_VIEW, label: "觉醒报价", render: renderGrowth, note: MATERIAL_NOTE },
    { view: TRAINING_VIEW, label: "合成报价", render: renderGrowth, note: MATERIAL_NOTE },
    { view: ORB_EXCHANGE_VIEW, label: "宝珠兑换", render: renderGeneric, note: "列出全部宝珠兑换定义，并按 item_id 和 base_id 报价。" },
    { view: UNIT_SKINS_VIEW, label: "单位服装", render: renderGeneric, note: "列出这张卡当前允许的显示服装。" },
    { view: ITEM_TICKET_VIEW, label: "物品召唤券", render: renderGeneric, note: "检查物品召唤券的定义以及能否发放。" },
    { view: PRESENTS_VIEW, label: "礼物预览", render: renderGeneric, note: "预览这些奖励放进礼物箱后的邮件行。" },
    { view: ENERGY_CLOCK_VIEW, label: "能量时钟换算", render: renderEnergyClock,
        note: "把一个带时区的时刻换算成能量时钟锚点；不指定 at 时用服务器当前时间。" },
    { view: UNIT_CAPACITY_VIEW, label: "兵营容量", render: renderUnitCapacity, note: "兵营容量和还能放下的单位数。" },
    { view: FAME_VIEW, label: "名声登记资格", render: renderGeneric, note: "列出每个单位能否登记到名声寄存。" },
    { view: EMBLEMS_VIEW, label: "称号资格", render: renderGeneric, note: "列出称号以及当前存档是否满足获得条件。" },
    { view: BACKGROUNDS_VIEW, label: "背景目录", render: renderGeneric, note: "列出背景定义和在当前秘书下的可选范围。" },
    { view: SUMMON_POOLS_VIEW, label: "召唤池", render: renderGeneric, note: "列出召唤池以及权重是否完整。" },
    { view: SUMMON_POOL_VIEW, label: "召唤池候选", render: renderGeneric,
        note: "权重不完整的召唤池只显示原始百分比，不换算抽中概率。" },
]);

export default Object.freeze({ title: "预览工具", order: 104, mount });

function toolKey(view) {
    return view.slice(view.indexOf(".") + 1);
}

function mount(root, { signal, params, navigate }) {
    const requested = params.get(TOOL_PARAM);
    const current = TOOLS.find((tool) => toolKey(tool.view) === requested) ?? TOOLS[0];
    const panel = h("div");
    root.append(section("预览工具", notice("info", TOOLS_NOTE),
        tabs(TOOLS.map((tool) => [toolKey(tool.view), tool.label]), toolKey(current.view),
            (key) => navigate(ROUTE, { [TOOL_PARAM]: key })),
        panel));
    present(panel, viewCatalog(signal), (views) => toolPanel(current, views.get(current.view), signal));
}

function isPaged(schema) {
    return PAGE_FIELDS.every((name) => Object.hasOwn(schema?.properties ?? {}, name));
}

function formSchema(schema) {
    // Paging belongs to the result list, so the form asks only for what the preview is about.
    const properties = Object.fromEntries(Object.entries(schema.properties ?? {})
        .filter(([name]) => !PAGE_FIELDS.includes(name)));
    return Object.keys(properties).length
        ? { ...schema, properties, required: (schema.required ?? []).filter((name) => Object.hasOwn(properties, name)) } : null;
}

function toolPanel(tool, descriptor, signal) {
    if (descriptor === undefined) return renderError({ code: VIEW_UNKNOWN, message: tool.view });
    const schema = descriptor.params_schema;
    const paged = isPaged(schema);
    const fields = schema === null ? null : formSchema(schema);
    const form = fields === null ? null : schemaForm(fields);
    const result = h("div", { class: "view", dataset: { view: tool.view } });
    const newest = newestRead();
    let chosen = {};

    function read(offset) {
        const query = schema === null ? undefined : { ...chosen, ...(paged ? { offset, limit: PAGE_LIMIT } : {}) };
        present(result, newest(async (current) => {
            try {
                return (await api.view(tool.view, query, { signal })).data;
            } catch (error) {
                if (current() && error.code === PARAMS_INVALID) form?.showErrors(paramsProblems(error.fields));
                throw error;
            }
        }), (envelope) => [
            h("p", { class: "muted" }, `修订号 ${envelope.revision} · 计算于 ${formatInstant(envelope.server_now?.jst)}`),
            tool.render(envelope.data, read),
        ]);
    }

    function run() {
        const values = form === null ? { ok: true, value: {} } : form.read();
        if (!values.ok) return;
        chosen = values.value;
        read(0);
    }

    return h("div", { class: "tool", dataset: { tool: toolKey(tool.view) } },
        notice("info", tool.note),
        form?.element ?? null,
        toolbar(button(form === null ? "读取" : "预览", { tone: "primary", action: "run-tool", onClick: run })),
        result);
}

function isRecord(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isAttempt(value) {
    return isRecord(value) && typeof value.ok === "boolean";
}

function isPage(value) {
    return isRecord(value) && Array.isArray(value.items) && Object.hasOwn(value, "next_offset");
}

function withoutOk(value) {
    return Object.fromEntries(Object.entries(value).filter(([name]) => name !== "ok"));
}

function attemptCell(value) {
    if (!isAttempt(value)) return inline(value);
    const rest = withoutOk(value);
    return value.ok && Object.keys(rest).length ? inline(rest) : passed(value);
}

function renderFields(record, go) {
    const pairs = Object.entries(record).filter(([name]) => !(record.projected && PROJECTION_FIELDS.has(name)));
    return h("div", {}, projectionNote(record), keyValues(pairs.map(([name, value]) => [name, renderAny(value, go)])));
}

function renderAny(value, go) {
    if (isPage(value)) return h("div", {}, recordsTable(value.items, { empty: "没有记录。", cell: attemptCell }), pager(value, go));
    if (isAttempt(value)) {
        if (!value.ok) return renderError(value.error);
        const rest = withoutOk(value);
        return Object.keys(rest).length ? renderFields(rest, go) : passed(value);
    }
    if (Array.isArray(value)) {
        return value.length && value.every(isRecord) ? recordsTable(value, { cell: attemptCell }) : renderValue(value);
    }
    return isRecord(value) ? renderFields(value, go) : value;
}

function renderGeneric(data, go) {
    return renderAny(data, go);
}

function storageBadge(unit) {
    if (typeof unit.storage_supported !== "boolean") return "—";
    return unit.storage_supported ? badge("是", "ok") : badge("否", "danger");
}

const BARRACKS_COLUMN = { label: "兵营", cell: (unit) => unit.barracks ?? "—" };
const STORAGE_COLUMNS = [BARRACKS_COLUMN, { label: "可用", cell: storageBadge }];

function storageNote(units, text) {
    return units.some((unit) => unit?.storage_supported === false) ? notice("warn", text) : null;
}

function unitFields(unit) {
    const fields = Object.entries(withoutOk(unit)).filter(([name]) => !STORAGE_FIELDS.has(name));
    return keyValues([...fields.map(([name, value]) => [name, inline(value)]),
        ...STORAGE_COLUMNS.map((column) => [column.label, column.cell(unit)])]);
}

function changesTable(changes) {
    return table([
        { label: "字段", cell: ([name]) => code(name) },
        { label: "之前", cell: ([, change]) => change.before ?? "—" },
        { label: "之后", cell: ([, change]) => change.after },
    ], Object.entries(changes ?? {}), { empty: "没有字段变化。" });
}

const SALE_COLUMNS = [
    { label: "GetIndex", cell: (unit) => unit.GetIndex },
    { label: "UniqueID", cell: (unit) => unit.UniqueID },
    { label: "CardID", cell: (unit) => unit.CardID },
    { label: "ClassID", cell: (unit) => unit.ClassID },
    BARRACKS_COLUMN,
    { label: "可出售", cell: storageBadge },
    { label: "稀有度核对", cell: (unit) => passed(unit.rating) },
    { label: "自动出售", cell: (unit) => (unit.automatic ? badge("是", "info") : "否") },
    { label: "出售会被拒的保护", cell: (unit) => protectionBadges(unit.protections) },
];

const MATERIAL_COLUMNS = [
    { label: "来源", cell: (unit) => code(unit.source) },
    { label: "GetIndex", cell: (unit) => unit.GetIndex },
    { label: "UniqueID", cell: (unit) => unit.UniqueID },
    { label: "CardID", cell: (unit) => unit.CardID },
    { label: "ClassID", cell: (unit) => unit.ClassID },
    { label: "ClassChangeCount", cell: (unit) => unit.ClassChangeCount },
    ...STORAGE_COLUMNS,
];

function goldNote(after, discarded) {
    if (discarded > 0) return notice("warn", `超过金币上限 ${amount(GOLD_LIMIT)} 的 ${amount(discarded)} 金币会被丢弃。`);
    return after >= GOLD_LIMIT ? notice("warn", "出售后金币达到上限，之后获得的金币都会被丢弃。") : null;
}

function renderSaleQuote(quote) {
    const { before, after, discarded } = quote.money;
    return h("div", {},
        projectionNote(quote),
        keyValues([
            ["出售所得金币", amount(quote.gold)],
            ["彩虹币", h("span", {}, String(amount(quote.rainbow)), " ", code(quote.rainbow_item ?? "—"))],
            ["金币余额", `${amount(before)} → ${amount(after)}`],
            ["金币上限", amount(GOLD_LIMIT)],
            ["丢弃的金币", discarded > 0 ? badge(String(amount(discarded)), "danger") : amount(discarded)],
        ]),
        goldNote(after, discarded));
}

function renderUnitSale(data) {
    return outcome(data, (result) => h("div", {},
        table(SALE_COLUMNS, result.units, { empty: "没有单位。" }),
        storageNote(result.units, SALE_STORAGE_NOTE),
        h("h3", { class: "subheading" }, "报价"),
        outcome(result.quote, renderSaleQuote)));
}

function renderTradeQuote(quote) {
    return h("div", {},
        projectionNote(quote),
        keyValues([
            ["商品", quote.shop_id],
            ["重置周期", quote.period],
            ["已兑换 / 上限", `${quote.exchange_count} / ${quote.limit}`],
            ["开放时间", `${formatInstant(quote.window?.start)} – ${formatInstant(quote.window?.end)}`],
            ["单价", amount(quote.unit_price)],
            ["总价", amount(quote.total_price)],
            ["持有折扣", inline(quote.ownership_discount)],
            ["货币", inline(quote.currency)],
            ["奖励", inline(quote.reward)],
        ]));
}

function renderSilverTrade(data) {
    return outcome(data, (result) => h("div", {},
        h("h3", { class: "subheading" }, "报价"),
        outcome(result.quote, renderTradeQuote),
        h("h3", { class: "subheading" }, "素材"),
        result.material === null ? h("p", { class: "muted" }, "没有指定素材单位。") : outcome(result.material, unitFields),
        storageNote([result.material], TRADE_STORAGE_NOTE),
        h("h3", { class: "subheading" }, "兵营"),
        h("div", { dataset: { check: "barracks" } }, "兑换得到的单位需要一个空的兵营格：", passed(result.barracks))));
}

function renderGrowthShop(data) {
    return outcome(data, (result) => h("div", {},
        h("h3", { class: "subheading" }, "报价"),
        outcome(result.quote, renderTradeQuote),
        h("h3", { class: "subheading" }, "成长后的字段"),
        result.values === null ? h("p", { class: "muted" }, "没有指定目标单位。") : outcome(result.values, (values) => h("div", {},
            projectionNote(values), unitFields(values.target), storageNote([values.target], TRADE_STORAGE_NOTE),
            changesTable(values.changes)))));
}

function renderGrowthQuote(quote) {
    const shown = Object.fromEntries(Object.entries(quote).filter(([name]) => name !== "changes" && name !== "ok"));
    return h("div", {},
        quote.money < quote.price ? notice("warn", `持有金币 ${amount(quote.money)} 不够支付 ${amount(quote.price)}。`) : null,
        renderFields(shown),
        h("h3", { class: "subheading" }, "字段变化"),
        changesTable(quote.changes));
}

function renderGrowth(data) {
    return outcome(data, (result) => h("div", {},
        keyValues([["目标单位", unitFields(result.target)], ["保护检查", passed(result.protection)]]),
        h("h3", { class: "subheading" }, "素材"),
        table(MATERIAL_COLUMNS, result.materials, { empty: "没有素材。" }),
        storageNote([result.target, ...result.materials], GROWTH_STORAGE_NOTE),
        result.slots === undefined || result.slots === null ? null
            : [h("h3", { class: "subheading" }, "素材格"),
                outcome(result.slots, (slots) => renderValue(slots.slots, { open: true }))],
        h("h3", { class: "subheading" }, "报价"),
        outcome(result.quote, renderGrowthQuote)));
}

function renderEnergyClock(data) {
    return outcome(data, (result) => h("div", {},
        h("h3", { class: "subheading" }, "能量时钟标记"),
        outcome(result.marker, (clock) => fieldsList(clock.marker)),
        h("h3", { class: "subheading" }, "换算"),
        outcome(result.conversion, (conversion) => h("div", {},
            projectionNote(conversion),
            keyValues([
                ["时刻", formatInstant(conversion.instant?.jst)],
                ["能量秒", conversion.seconds],
                ["写入的字段", inline(conversion.fields)],
            ]),
            h("h3", { class: "subheading" }, "换算后的锚点"), anchorsTable(conversion.decoded),
            h("h3", { class: "subheading" }, "当前锚点"), anchorsTable(conversion.current)))));
}

function renderUnitCapacity(data) {
    return outcome(data, (result) => h("div", {},
        outcome(result.barracks, barracksUsage),
        h("h3", { class: "subheading" }, "还能放下的单位"),
        outcome(result.unit_slots, (slots) => keyValues([["单位", amount(slots.available)]]))));
}
