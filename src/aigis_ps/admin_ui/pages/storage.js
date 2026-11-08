import { h, keyValues, notice, section } from "../diff-view.js";
import { commandPanel } from "../plan-flow.js";
import { amount, barracksUsage, outcome, pageLink, recordsTable, viewPanel } from "../view-kit.js";

const STORAGE_VIEW = "units.storage";
const BARRACKS_VIEW = "units.barracks";
const DOMAIN = "storage";
const TOOLS_ROUTE = "tools";
const FAME_TOOL = Object.freeze({ tool: "fame" });
const STOCK_LABELS = Object.freeze({ fame: "名声寄存", fairy: "圣灵仓库", silver: "银仓库" });
const LIMIT_LABELS = Object.freeze({ deposit_limit: "寄存上限", per_card_capacity: "每张卡的容量" });
const STOCK_NOTE = "库存调整只在减少时需要确认短语并在提交前自动备份；增加名声库存要求单位已登记且 151 能取回，增加银仓库存沿用 127 的材料规则。";
const BARRACKS_NOTE = "barracks.grant_slot 不检查等级、不扣结晶，最多授予两座额外兵营；兵营之间的移动用 barracks.transfer。";

export default Object.freeze({ title: "仓库与兵营", order: 80, mount });

function mount(root, { signal }) {
    const stock = viewPanel(STORAGE_VIEW, undefined, renderStorage, { signal });
    const barracks = viewPanel(BARRACKS_VIEW, undefined, renderBarracks, { signal });
    const refresh = () => [stock, barracks].forEach((panel) => panel.refresh());
    root.append(
        section("仓库库存", notice("info", STOCK_NOTE), stock.element),
        section("兵营", notice("info", BARRACKS_NOTE), barracks.element),
        section("命令", notice("info", "名声登记资格可以先在预览工具页查看：", pageLink(TOOLS_ROUTE, "打开名声登记资格", FAME_TOOL)),
            commandPanel(DOMAIN, { signal, onCommitted: refresh })));
}

function renderStock(name, result) {
    return h("div", { class: "record", dataset: { stock: name } },
        h("h3", { class: "subheading" }, STOCK_LABELS[name] ?? name),
        outcome(result, (stock) => h("div", {},
            keyValues(Object.entries(LIMIT_LABELS).filter(([field]) => field in stock)
                .map(([field, label]) => [label, amount(stock[field])])),
            recordsTable(stock.rows, { empty: "没有库存。" }))));
}

function renderStorage(data) {
    return h("div", { class: "records" }, Object.entries(data).map(([name, result]) => renderStock(name, result)));
}

function renderBarracks(data) {
    return h("div", {},
        outcome(data.barracks, barracksUsage),
        h("h3", { class: "subheading" }, "还能授予的单位数"),
        outcome(data.grantable, (grantable) => keyValues([["单位", amount(grantable.units)]])),
        h("h3", { class: "subheading" }, "进行中战斗预留的授予"),
        outcome(data.battle_reservations,
            (reservations) => recordsTable(reservations.grants, { empty: "没有进行中战斗预留的授予。" })));
}
