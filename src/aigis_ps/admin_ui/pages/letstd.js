import { api } from "../api.js";
import {
    badge, button, code, digest, formatInstant, h, keyValues, list, notice, present, renderError, section, table,
    toolbar,
} from "../diff-view.js";
import { DEFINITION_LABELS } from "../ledger-view.js";
import { commandPanel, planFlow } from "../plan-flow.js";
import { counts, labelled, offsetsOf, pagedRoute, pager, sourceLabel, viewPanel } from "../view-kit.js";

const OVERVIEW_VIEW = "letstd.overview";
const LEDGER_VIEW = "letstd.ledger";
const DELIVERABILITY_VIEW = "letstd.deliverability";
const PROJECTION_VIEW = "letstd.projection";
const ROUTE = "letstd";
const DOMAIN = "letstd";
const LEDGER_KEY = "letstd";
const REARM_OPERATION = "letstd.rearm";
const CONFIG_ROUTE = "config";
const LOCAL_LABEL = "【ローカル模擬】";
const PAGE_LIMIT = 500;
const ROW_UNIT = "行";
const SOURCES = [115, 116];
const OFFSETS = Object.freeze({ ledger: "ledger", checks: "checks" });

const BANNER = "本页是本地模拟的合作方活动：未验证任何真实跨游戏账号、合作方进度或购买，奖励只以带本地标签的礼物邮件发放。"
    + "标签写死在服务端代码里，不能通过配置去掉。";
const NATIVE_POPUP_NOTE = "原生弹窗文案来自客户端资源，无法加本地标签；打开后游戏内会显示与真实联动相同的提示。";
const SWITCH_NOTE = "开关在配置页的 LetsTD 规则键中修改，属热切换档：只改服务端规则，不迁移存档。关闭开关只暂停该组投递（含 pending），"
    + "已发奖励不收回；重新打开后继续投未投递的行，不重发。";
const GROUP_NOTE = "六个奖励组全开并打开 native_popup 时，一次 115 会连续弹出十几个模态框，建议一组一组开启。";
const REARM_NOTE = "资源新增的行，或定义变化且尚未投递的行，只有执行 letstd.rearm 才会并入；已投递的行只标记需要适配，不会重发。";
const FINGERPRINT_NOTE = "武装时的规则与资源指纹只作对照：任一开关变化都会改变规则指纹，不必因此重新武装；是否需要重新武装以各组的预判为准。";
const REARM_PENDING_NOTE = "上一次重新武装的结果未知。请先在下面重试同一次提交；确认结果之前不能开始新的预览。";
const DELIVERABILITY_NOTE = "逐行按礼物服务的预览判断能否发信，不复制存档。转 pending 的行在依赖内容变化或邮箱腾出空间后才会重试。";
const PROJECTION_NOTE = "投影在服务端的存档深拷贝上执行一次 LetsTD 结算，不保存任何内容。行数较多的首次结算可能需要数秒，"
    + "期间占用一个世代租约，请在需要时再计算。";
const PROJECTION_SCOPE = "这里只执行 LetsTD 部分的结算；同一次 115/116 中先结算的其他奖励也会占用邮箱，没有计入。";
const PROJECTION_PLACEHOLDER = "点击“计算投影”后，这里显示下一次 115/116 的投递。";
const POPUP_ON_NOTE = "native_popup 已打开：下面的弹窗会在游戏内出现，且无法加本地标签。";
const POPUP_OFF_NOTE = "native_popup 关闭：115/116 不下发 LetsTDRewardParam，游戏内不会弹窗；下面是打开后会显示的内容。";
const SOURCE_UNREGISTERED = "配置注册表里还没有加载 LetsTD 规则的键，无法确定开关来源。";
const SOURCE_UNKNOWN = "运行中的世代不对应可读取的已存配置集（例如用命令行规则文件启动），无法确定开关来源。";
const WAITING_NOTE = "包括未开始、未武装和待重新武装的行；下一次结算实际投递哪些行，以下面的投影为准。";

const GROUP_LABELS = Object.freeze({ 1: "キャンペーン報酬", 2: "やろうぜパック購入特典" });
const STATUS_LABELS = Object.freeze({
    scheduled: { label: "未开始", tone: "info" }, pending: { label: "待投递", tone: "warn" },
    ended: { label: "已过期", tone: "muted" }, delivered: { label: "已投递", tone: "ok" },
    unarmed: { label: "未武装", tone: "muted" }, added: { label: "资源新增", tone: "danger" },
});
const FINGERPRINT_LABELS = Object.freeze({
    matching: { label: "与当前一致", tone: "ok" }, changed: { label: "与当前不同", tone: "info" },
    unavailable: { label: "无法核对", tone: "warn" },
});
const OUTCOME_LABELS = Object.freeze({
    deliverable: { label: "可投递", tone: "ok" }, deferred: { label: "转 pending", tone: "warn" },
    failing: { label: "会使结算失败", tone: "danger" },
});
const RETRY_LABELS = Object.freeze({
    every_settlement: "每次结算重试", next_settlement: "下次结算重试", dependency_change: "依赖内容变化后重试",
    paused: "开关关闭，暂停投递",
});
const KIND_LABELS = Object.freeze({ item: "物品", unit: "单位", free_crystal: "免费结晶", gold: "金币" });
const TEXT_LABELS = Object.freeze({ single: "Single", multi: "Multi" });

export default Object.freeze({ title: "LetsTD 本地模拟", order: 110, mount });

function mount(root, { signal, params, navigate }) {
    const offsets = offsetsOf(params, Object.values(OFFSETS));
    const go = pagedRoute(navigate, ROUTE, offsets);
    const rearmNote = h("div", { "aria-live": "polite" });
    const rearmArea = h("div");
    const panels = [];
    const refresh = () => panels.forEach((panel) => panel.refresh());
    let rearmPending = false;

    function startRearm(group) {
        if (rearmPending) {
            // Another preview would draw a new operation ID while the last commit may already have been applied.
            rearmNote.replaceChildren(notice("warn", REARM_PENDING_NOTE));
            return;
        }
        rearmNote.replaceChildren();
        const flow = planFlow({
            commands: [{ operation: REARM_OPERATION, params: { partner: group.partner, group: group.group } }],
            onCommitted: refresh, onPending: (waiting) => { rearmPending = waiting; },
        });
        rearmArea.replaceChildren(h("h3", { class: "subheading" }, "重新武装 ", groupTitle(group)), flow.element);
        flow.preview();
    }

    const switches = viewPanel(OVERVIEW_VIEW, undefined, (data) => renderOverview(data, navigate), { signal });
    const ledger = viewPanel(LEDGER_VIEW, { offset: offsets[OFFSETS.ledger], limit: PAGE_LIMIT },
        (data) => renderLedger(data, startRearm, go(OFFSETS.ledger)), { signal });
    const deliverability = viewPanel(DELIVERABILITY_VIEW, { offset: offsets[OFFSETS.checks], limit: PAGE_LIMIT },
        (data) => renderDeliverability(data, go(OFFSETS.checks)), { signal });
    panels.push(switches, ledger, deliverability);
    root.append(
        notice("warn", badge("本地模拟", "warn"), " ", code(LOCAL_LABEL), " ", BANNER),
        section("开关", switches.element),
        section("账本", ledger.element, rearmNote, rearmArea),
        section("可投性预判", deliverability.element),
        section("下一次 115/116 的投递投影", projectionPanel(signal)),
        section("命令", commandPanel(DOMAIN, { signal, onCommitted: refresh })));
}

function groupTitle(group) {
    const name = group.name ? `（${group.name}）` : "";
    return h("span", {}, code(group.partner), ` ${group.group} ${GROUP_LABELS[group.group] ?? ""}${name}`);
}

function groupKey(item) {
    return `${item.partner}:${item.group}`;
}

function enabledBadge(enabled) {
    return enabled === true ? badge("开", "ok") : enabled === false ? badge("关") : badge("开关未知");
}

function serviceState(status) {
    return status.ready ? badge("就绪", "ok") : h("span", {}, badge("未就绪", "danger"), " ", code(status.error ?? "—"));
}

function renderOverview(data, navigate) {
    const simulation = data.simulation ?? {};
    const status = data.service ?? {};
    return h("div", {},
        keyValues([
            ["性质", simulation.simulated ? badge("本地模拟", "warn") : badge("未知")],
            ["实机验证", simulation.native_tested ? "已验证" : badge("未验证", "warn")],
            ["邮件标签", code(simulation.label ?? LOCAL_LABEL)],
            ["标签可配置", simulation.label_configurable ? "是" : "否（写死在代码里）"],
            ["服务端说明", simulation.verification ?? "—"],
            ["LetsTD 服务", status.ok ? serviceState(status) : badge("当前世代不可用", "danger")],
        ]),
        status.ok ? null : renderError(status.error),
        switchesOrError(data.switches, status),
        renderNativePopup(data.switches),
        h("h3", { class: "subheading" }, "开关来源"), renderSource(data.source),
        notice("info", SWITCH_NOTE),
        notice("warn", GROUP_NOTE),
        toolbar(button("到配置页修改开关", { action: "edit-switches", onClick: () => navigate(CONFIG_ROUTE) })));
}

function switchesOrError(switches, status) {
    if (switches?.ok) return renderSwitches(switches, status);
    // Both reads need the same service, so its failure is already shown once above.
    return status.ok ? renderError(switches?.error) : null;
}

function renderSwitches(switches, status) {
    const groups = status.ok ? status.groups ?? {} : {};
    return table([
        { label: "合作方", cell: (row) => code(row.partner) },
        { label: "奖励组", cell: (row) => `${row.group} ${GROUP_LABELS[row.group] ?? ""}（${row.name}）` },
        { label: "开关", cell: (row) => enabledBadge(row.enabled) },
        { label: "资源行数", cell: (row) => groups[row.partner]?.[row.name]?.rows ?? "—" },
    ], switches.groups ?? []);
}

function renderNativePopup(switches) {
    const popup = switches?.ok ? switches.native_popup : null;
    const state = popup === null ? badge("未知") : popup.enabled ? badge("开：游戏内会弹出原生提示", "danger") : badge("关", "ok");
    const accent = popup === null ? null : popup.enabled ? "check-failed" : "check-ok";
    return h("div", { class: ["check", accent], dataset: { switch: "native_popup" } },
        h("p", { class: "check-head" }, code("native_popup"), " ", state),
        notice("warn", NATIVE_POPUP_NOTE));
}

function renderSource(source) {
    if (!source?.known) {
        const reason = source?.key === null ? SOURCE_UNREGISTERED : source?.error ? null : SOURCE_UNKNOWN;
        return h("div", {}, reason ? notice("warn", reason) : null, source?.error ? renderError(source.error) : null);
    }
    return h("div", {},
        keyValues([
            ["配置键", code(source.key)],
            ["来源", sourceLabel(source.source)],
            ["运行中的配置集", code(source.set)],
            ["文件", source.path ? code(source.path) : "无（代码默认值）"],
            ["命令行参数", h("span", {}, code(source.cli_flag), " / ", code(`-${source.ps_parameter}`))],
            ["内容版本", source.content_version ? digest(source.content_version) : "—"],
        ]),
        source.read_only ? notice("warn", "命令行规则文件覆盖了开关，本进程内只读；修改需改命令行文件并重启服务。") : null,
        source.error ? renderError(source.error) : null);
}

function checkState(labels, check) {
    if (check === null || check === undefined) return "—";
    return h("span", {}, labelled(labels, check.state), check.error ? [" ", code(check.error.code)] : null);
}

function fingerprint(check) {
    if (check === null || check === undefined) return "—";
    return h("span", {}, digest(check.frozen), " ", checkState(FINGERPRINT_LABELS, check));
}

function mailIds(ids) {
    return Array.isArray(ids) && ids.length ? ids.join("、") : "—";
}

function rewardName(item) {
    const { title, item_id: itemId, card_id: cardId } = item.detail;
    const target = itemId ?? cardId;
    return h("span", {}, title ? title.replaceAll("\n", " ") : "—",
        target === null || target === undefined ? null : [" ", code(String(target))]);
}

function popupState(popup) {
    return popup === true ? badge("带弹窗", "danger") : popup === false ? "无" : "—";
}

function rowStatus(item) {
    return h("span", {}, labelled(STATUS_LABELS, item.status),
        item.detail.needs_adaptation ? [" ", badge("定义已变，需要适配", "danger")] : null,
        item.detail.rearm ? [" ", badge("重新武装会改动", "warn")] : null);
}

const ROW_COLUMNS = [
    { label: "RewardId", cell: (item) => item.key.reward_id },
    { label: "ConfigId", cell: (item) => item.detail.config_id ?? "—" },
    { label: "类型", cell: (item) => KIND_LABELS[item.detail.kind] ?? item.detail.kind ?? "—" },
    { label: "名称", cell: rewardName },
    { label: "数量", cell: (item) => item.detail.count ?? "—" },
    { label: "StartDate", cell: (item) => formatInstant(item.detail.start ?? item.detail.start_at) },
    { label: "状态", cell: rowStatus },
    { label: "原因", cell: (item) => (item.reason ? code(item.reason) : "—") },
    { label: "重试", cell: (item) => RETRY_LABELS[item.detail.retry] ?? "—" },
    { label: "定义", cell: (item) => checkState(DEFINITION_LABELS, item.definition) },
    { label: "邮件", cell: (item) => mailIds(item.detail.mail_ids) },
    { label: "投递时间", cell: (item) => formatInstant(item.detail.delivered_at) },
    { label: "经由", cell: (item) => item.detail.via ?? "—" },
    { label: "弹窗", cell: (item) => popupState(item.detail.popup) },
];

function rearmList(label, rewards) {
    return rewards.length ? h("p", {}, `${label}：RewardId ${rewards.join("、")}`) : null;
}

function renderGroup(group, rows, startRearm) {
    const rearm = group.rearm;
    return h("div", { class: "record", dataset: { partner: group.partner, group: String(group.group) } },
        h("h3", { class: "subheading" }, groupTitle(group), " ", enabledBadge(group.enabled), " ",
            group.armed ? badge("已武装", "info") : badge("未武装")),
        keyValues([
            ["武装时间", formatInstant(group.armed_at)],
            ["重新武装时间", formatInstant(group.rearmed_at)],
            ["武装时的规则", fingerprint(group.rules_version)],
            ["武装时的资源", fingerprint(group.resource_sha256)],
            ["资源行数", group.rows ?? "—"],
            ["账本状态", counts(group.statuses, STATUS_LABELS)],
        ]),
        rearm?.needed ? notice("warn", "需要重新武装：", rearmList("新增", rearm.added), rearmList("重新开始", rearm.reset),
            rearmList("标记需要适配", rearm.adapted),
            toolbar(button("预览重新武装", { tone: "primary", action: "rearm", onClick: () => startRearm(group) }))) : null,
        rearm?.unavailable?.length
            ? notice("danger", `资源中已没有这些尚未投递的行，重新武装也无法并入：RewardId ${rearm.unavailable.join("、")}`) : null,
        table(ROW_COLUMNS, rows, { empty: "当前资源和账本里都没有这一组的行。" }));
}

function renderLedger(data, startRearm, go) {
    const ledger = data.ledger ?? {};
    if (!ledger.ok) return renderError(ledger.error);
    const rows = new Map();
    for (const item of data.entries.items) {
        const key = groupKey(item.key);
        rows.set(key, [...(rows.get(key) ?? []), item]);
    }
    return h("div", {},
        keyValues([
            ["LetsTD 服务", data.service.available ? badge("已接入当前世代", "ok") : badge("当前世代没有 LetsTD 服务", "warn")],
            ["账本", ledger.keys?.[LEDGER_KEY] ? "已建立" : "尚未建立：开关打开后的第一次 115/116 才会武装"],
            ["最后结算", formatInstant(ledger.last_time_utc)],
            ["客户端回读请求号", list(data.readback?.requests?.map(String))],
            ["回读说明", list(data.readback?.none_reasons)],
            ["行状态", counts(data.summary?.statuses, STATUS_LABELS)],
            ["定义核对", counts(data.summary?.definitions, DEFINITION_LABELS)],
        ]),
        data.catalog?.ok === false ? renderError(data.catalog.error) : null,
        notice("info", REARM_NOTE),
        h("p", { class: "muted" }, FINGERPRINT_NOTE),
        h("div", { class: "records" },
            (data.groups ?? []).map((group) => renderGroup(group, rows.get(groupKey(group)) ?? [], startRearm))),
        pager(data.entries, go, { unit: ROW_UNIT }));
}

function target(item) {
    return item.target === null || item.target === undefined ? "—" : code(String(item.target));
}

const CHECK_COLUMNS = [
    { label: "合作方", cell: (item) => code(item.key.partner) },
    { label: "组", cell: (item) => item.key.group },
    { label: "RewardId", cell: (item) => item.key.reward_id },
    { label: "开关", cell: (item) => enabledBadge(item.enabled) },
    { label: "账本状态", cell: (item) => labelled(STATUS_LABELS, item.status) },
    { label: "类型", cell: (item) => KIND_LABELS[item.kind] ?? item.kind ?? "—" },
    { label: "目标", cell: target },
    { label: "数量", cell: (item) => item.count },
    { label: "预判", cell: (item) => labelled(OUTCOME_LABELS, item.outcome) },
    { label: "邮件数", cell: (item) => item.mails ?? "—" },
    { label: "原因", cell: (item) => (item.error ? code(item.error.code) : "—") },
    { label: "重试", cell: (item) => RETRY_LABELS[item.retry] ?? "—" },
];

function renderDeliverability(result, go) {
    if (!result.ok) return renderError(result.error);
    const { mailbox, enabled_waiting: waiting } = result;
    return h("div", {},
        notice("info", DELIVERABILITY_NOTE),
        keyValues([
            ["邮箱", `${mailbox.held} / ${mailbox.capacity}（剩余 ${mailbox.room} 封）`],
            ["全部行", counts(result.outcomes, OUTCOME_LABELS)],
            ["启用组中尚未投递", `${waiting.rows} 行，可投递的需要 ${waiting.mails} 封邮件`],
            ["其中状态", counts(waiting.statuses, STATUS_LABELS)],
            ["其中预判", counts(waiting.outcomes, OUTCOME_LABELS)],
            ["邮箱放得下", waiting.fits ? badge("放得下", "ok") : badge("放不下：超出的行转 pending，腾出空间后重试", "warn")],
        ]),
        h("p", { class: "muted" }, WAITING_NOTE),
        table(CHECK_COLUMNS, result.entries.items, { empty: "资源里没有 LetsTD 行。" }),
        pager(result.entries, go, { unit: ROW_UNIT }));
}

function projectionPanel(signal) {
    const source = h("select", { class: "input" },
        SOURCES.map((value) => h("option", { value: String(value) }, String(value))));
    source.value = String(SOURCES[0]);
    const area = h("div", { "aria-live": "polite" }, h("p", { class: "muted" }, PROJECTION_PLACEHOLDER));
    const load = async () => (await api.view(PROJECTION_VIEW, { source: Number(source.value) }, { signal })).data;
    const run = button("计算投影", { tone: "primary", action: "project", onClick: () => project() });
    async function project() {
        // The projection can hold a generation lease for seconds, so one runs at a time.
        run.disabled = true;
        try {
            await present(area, load, (view) => renderProjection(view.data));
        } finally {
            run.disabled = false;
        }
    }
    return h("div", {}, notice("info", PROJECTION_NOTE),
        h("label", { class: "field" }, h("span", { class: "field-label" }, "结算来源"), source), toolbar(run), area);
}

const GRANT_COLUMNS = [
    { label: "合作方", cell: (grant) => code(grant.partner) },
    { label: "组", cell: (grant) => grant.group },
    { label: "RewardId", cell: (grant) => grant.reward_id },
    { label: "ConfigId", cell: (grant) => grant.config_id },
    { label: "类型", cell: (grant) => KIND_LABELS[grant.kind] ?? grant.kind ?? "—" },
    { label: "目标", cell: target },
    { label: "数量", cell: (grant) => grant.count },
    { label: "邮件", cell: (grant) => `${grant.mails} 封（${mailIds(grant.mail_ids)}）` },
];

function stageText(stage) {
    if (stage === null) return "—";
    return h("span", {}, labelled(STATUS_LABELS, stage.status), stage.reason ? [" ", code(stage.reason)] : null);
}

const CHANGE_COLUMNS = [
    { label: "合作方", cell: (change) => code(change.key.partner) },
    { label: "组", cell: (change) => change.key.group },
    { label: "RewardId", cell: (change) => change.key.reward_id },
    { label: "之前", cell: (change) => stageText(change.before) },
    { label: "之后", cell: (change) => stageText(change.after) },
];

const PARAM_COLUMNS = ["ServiceId", "newChallenge", "newPurchase"].map((name) => ({
    label: name, cell: (row) => (row[name] ? code(row[name]) : "—"),
}));

function lineText(line) {
    return h("span", {}, KIND_LABELS[line.kind] ?? line.kind,
        line.target === null ? null : [" ", code(String(line.target))], ` × ${line.count} `,
        badge(TEXT_LABELS[line.text] ?? line.text), " ", code(String(line.text_key)),
        line.rewards.length > 1 ? h("span", { class: "muted" }, `（合并 ${line.rewards.length} 行）`) : null);
}

function renderWindow(popup) {
    return h("div", { class: "mode", dataset: { partner: popup.partner, window: String(popup.window) } },
        h("h4", { class: "mode-title" }, code(popup.partner), badge(`第 ${popup.window} 个弹窗`, "info")),
        h("p", { class: "muted" }, "标题：", code(popup.title.format_key), " 的 [SERVICE_NAME] 换成 ",
            code(popup.title.name_key)),
        h("ol", { class: "steps" }, popup.lines.map((line) => h("li", {}, lineText(line)))));
}

function renderPopups(popups) {
    return h("div", {},
        popups.shown ? notice("danger", POPUP_ON_NOTE) : notice("info", POPUP_OFF_NOTE),
        notice("warn", NATIVE_POPUP_NOTE),
        popups.reward_param.length ? table(PARAM_COLUMNS, popups.reward_param) : null,
        popups.windows.length ? h("div", { class: "modes" }, popups.windows.map(renderWindow))
            : h("p", { class: "muted" }, "没有新投递，不会弹窗。"));
}

function renderProjection(result) {
    if (!result.ok) return renderError(result.error);
    const mails = result.grants.reduce((total, grant) => total + grant.mails, 0);
    return h("div", {},
        keyValues([
            ["投影时间", formatInstant(result.as_of?.jst)],
            ["结算来源", result.source],
            ["保存", result.persisted ? "会保存" : badge("只是投影，不保存", "info")],
            ["新武装的组", result.armed.length ? h("span", {}, result.armed.map((group) => [groupTitle(group), " "])) : "无"],
            ["本次投递", `${result.grants.length} 行，${mails} 封邮件`],
            ["邮箱", `${result.mailbox.before} → ${result.mailbox.after} / ${result.mailbox.capacity}`],
        ]),
        h("p", { class: "muted" }, PROJECTION_SCOPE),
        table(GRANT_COLUMNS, result.grants, { empty: "下一次结算没有新投递。" }),
        h("h3", { class: "subheading" }, "其他行的状态变化"),
        table(CHANGE_COLUMNS, result.changes, { empty: "没有其他行的状态变化。" }),
        h("h3", { class: "subheading" }, "原生弹窗预览"),
        renderPopups(result.popups));
}
