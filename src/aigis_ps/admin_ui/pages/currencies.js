import { badge, h, keyValues, notice, section } from "../diff-view.js";
import { commandPanel } from "../plan-flow.js";
import {
    amount, CRYSTAL_LIMITS, decodedTable, GOLD_LIMIT, offsetOf, outcome, PAGE_LIMIT, pager, viewPanel,
} from "../view-kit.js";

const SUMMARY_VIEW = "account.summary";
const DOMAIN = "currencies";
const ROUTE = "currencies";
const GOLD_SET = "wallet.gold.set";
const GOLD_FIELD = "Money";
const CURRENT_FIELD = "current";
const CURRENT_NOTE = "wallet.gold.set 的 current 必须等于存档里的余额：表单打开时和余额刷新后都会自动填入最新读到的余额。"
    + "只有 value 小于 current 时才需要确认短语并在提交前自动备份。";
const OVER_CAP_NOTE = "余额高于上限：这是旧存档留下的余额，只读且只能往下调；游戏侧的入账超出上限的部分会被丢弃。";
const CRYSTAL_NOTE = `有偿结晶最多 ${amount(CRYSTAL_LIMITS.paid)}、合计最多 ${amount(CRYSTAL_LIMITS.total)}；`
    + `免费结晶已超过 ${amount(CRYSTAL_LIMITS.free)} 时，任何结晶入账都会被拒绝。`;

export default Object.freeze({ title: "货币", order: 64, mount });

function goldOf(data) {
    return data.game_param?.ok ? data.game_param.fields?.[GOLD_FIELD]?.value : undefined;
}

function mount(root, { signal, params, navigate }) {
    const offset = offsetOf(params.get("offset"));
    const go = (next) => navigate(ROUTE, { offset: next || undefined });
    const current = currentBalance();
    const balances = viewPanel(SUMMARY_VIEW, { offset, limit: PAGE_LIMIT }, (data) => renderBalances(data, go), {
        signal, onData: (data) => current.update(goldOf(data)),
    });
    root.append(
        section("余额", balances.element),
        section("命令", commandPanel(DOMAIN, { signal, onCommitted: balances.refresh, onForm: current.follow })));
}

function currentBalance() {
    let money;
    let form = null;
    const write = () => {
        // The command refuses a current other than the saved balance, so the form always carries the newest balance read.
        if (form !== null && Number.isSafeInteger(money)) form.setValue(CURRENT_FIELD, money);
    };
    return Object.freeze({
        update(value) {
            money = value;
            write();
        },
        follow(command, opened) {
            if (command.operation !== GOLD_SET) return;
            form = opened;
            write();
        },
    });
}

function limited(value, limit) {
    return h("span", {}, amount(value), " / ", amount(limit), value > limit ? [" ", badge("超出上限", "warn")] : null);
}

function renderGold(money) {
    return h("div", {},
        keyValues([["金币", limited(money, GOLD_LIMIT)]]),
        money > GOLD_LIMIT ? notice("warn", OVER_CAP_NOTE) : null,
        notice("info", CURRENT_NOTE));
}

function renderCrystals(crystals, go) {
    const totals = crystals.totals ?? {};
    return h("div", {},
        keyValues([
            ["免费结晶", limited(totals.free, CRYSTAL_LIMITS.free)],
            ["有偿结晶", limited(totals.paid, CRYSTAL_LIMITS.paid)],
            ["合计", limited(totals.total, CRYSTAL_LIMITS.total)],
        ]),
        notice("info", CRYSTAL_NOTE),
        h("h3", { class: "subheading" }, "按取得日分桶"),
        decodedTable(crystals.buckets?.items, { empty: "没有结晶。" }),
        pager(crystals.buckets, go));
}

function renderBalances(data, go) {
    return h("div", {},
        outcome(data.game_param, (param) => renderGold(param.fields?.[GOLD_FIELD]?.value)),
        h("h3", { class: "subheading" }, "结晶"),
        outcome(data.crystals, (crystals) => renderCrystals(crystals, go)));
}
