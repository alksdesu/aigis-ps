import { notice, section } from "../diff-view.js";
import { ledgerTabs } from "../ledger-view.js";
import { commandPanel } from "../plan-flow.js";

const EVENT_LOGIN_VIEW = "events.event_login";
const COMEBACK_LOGIN_VIEW = "events.comeback_login";
const PURCHASED_LOGIN_VIEW = "events.purchased_login";
const DAILY_GIFT_VIEW = "events.daily_gift";
const LOGIN_REWARDS_VIEW = "events.login_rewards";
const EVENT_PASS_VIEW = "events.event_pass";
const MISSION_EP_BINDINGS_VIEW = "events.mission_ep_bindings";
const SUGOROKU_VIEW = "events.sugoroku";
const PACKAGES_VIEW = "events.packages";
const TRADES_VIEW = "events.trades";
const TOWER_TRADES_VIEW = "events.tower_trades";
const DOMAIN = "events";
const ROUTE = "events";
const LEDGERS = Object.freeze([
    [EVENT_LOGIN_VIEW, "活动登录奖励"], [COMEBACK_LOGIN_VIEW, "回归登录奖励"], [PURCHASED_LOGIN_VIEW, "购买型登录奖励"],
    [DAILY_GIFT_VIEW, "每日礼物"], [LOGIN_REWARDS_VIEW, "登录奖励"], [EVENT_PASS_VIEW, "活动通行证"],
    [MISSION_EP_BINDINGS_VIEW, "任务 EP 绑定"], [SUGOROKU_VIEW, "双六"], [PACKAGES_VIEW, "礼包"], [TRADES_VIEW, "交换所"],
    [TOWER_TRADES_VIEW, "塔交换"],
]);
const VOID_RECORDED = "void 作废并在账本留痕";
const VOID_DROPPED = "void 直接移除，账本不留记录";
const NOTES = Object.freeze({
    event_login: changedRewardsNote("event_login.resolve_changed"),
    comeback_login: changedRewardsNote("comeback_login.resolve_changed"),
    purchased_login: changedRewardsNote("purchased_login.resolve_changed"),
    daily_gift: changedRewardsNote("daily_gift.resolve_changed"),
    sugoroku: "双六的可领取状态按服务器时间计算；sugoroku.claim 和客户端的领取一样，没有可领取的奖励时什么也不保存。",
    login_rewards: "料理门槛变化后由 login_cooking.migrate 把登录料理配额迁移到当前规则，新手奖励总数变化后由 "
        + "login_rewards.rebase_beginner 重建基线；完成之前 115 会拒绝，客户端弹出通信错误并原包重发。两者都是破坏性操作，"
        + "需要确认短语。" + changedRewardsNote("login_rewards.resolve_pending_stamps", VOID_DROPPED),
    event_pass: "通行证的普通或高级物品身份变化后，已有投递记录的通行证手动领取新档位（159 与 event_pass.claim）会以 "
        + "event_pass_definition_changed 拒绝，116 自动补领把它留在待处理；"
        + "用 event_pass.rebind_definition 把账本改绑到当前定义，已投递记录保留；改绑是破坏性操作，需要确认短语。",
    tower_trades: "资源把塔兑换商品 ID 换给别的商品后，存档的购买记录仍属于原商品；改绑之前 65 与 69 整体拒绝，"
        + "客户端弹出通信错误并原包重发。用 tower_trade.rebind_product 逐个改绑这些商品的购买记录；改绑是破坏性操作，需要确认短语。",
});
const LEDGER_NOTE = "每一行是账本里的一个键：状态说明它是否已投递，定义核对说明冻结的奖励定义是否仍与当前内容一致。";

export default Object.freeze({ title: "活动", order: 92, mount });

function changedRewardsNote(command, voiding = VOID_RECORDED) {
    return `冻结定义已变化的待投递奖励由 ${command} 处理：redeliver 按当前定义补发，${voiding}；这是破坏性操作，需要确认短语。`;
}

function mount(root, { signal, params, navigate }) {
    const ledger = ledgerTabs(LEDGERS, { route: ROUTE, notes: NOTES, signal, params, navigate });
    root.append(
        section("活动账本", notice("info", LEDGER_NOTE), ledger.element),
        section("命令", commandPanel(DOMAIN, { signal, onCommitted: ledger.refresh })));
}
