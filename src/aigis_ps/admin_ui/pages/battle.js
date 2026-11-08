import { api, ROUTES } from "../api.js";
import {
    badge, button, code, digest, formatInstant, h, keyValues, list, loading, notice, present, renderError, renderValue,
    section, table, toolbar,
} from "../diff-view.js";
import { commandPanel, OPERATIONS, once, renderAttemptFailure } from "../plan-flow.js";
import {
    labelled, newestRead, offsetsOf, outcome, PAGE_LIMIT, pagedRoute, pageLink, pager, recordsTable, viewPanel,
} from "../view-kit.js";

const WINDOWS_VIEW = "battle.windows";
const JOURNAL_VIEW = "battle.journal";
const RECORDS_VIEW = "battle.records";
const RECORD_ARCHIVE_VIEW = "battle.record_archive";
const ENTRUST_VIEW = "battle.entrust";
const ENTRUST_AVAILABILITY_VIEW = "battle.entrust_availability";
const ENTRUST_QUEST_VIEW = "battle.entrust_quest";
const ENTRUST_PREVIEW_VIEW = "battle.entrust_preview";
const TOWERS_VIEW = "towers.overview";
const SUBJUGATION_VIEW = "battle.subjugation";
const SUBJUGATION_PROGRESS_VIEW = "battle.subjugation_progress";
const DOMAIN = "battle";
const ROUTE = "battle";
const PARAMS = Object.freeze({ records: "records" });
const PRESET_INVALID = "console_preset_invalid";
const STATE_LABELS = Object.freeze({
    upcoming: { label: "未开始", tone: "info" }, open: { label: "开放中", tone: "ok" },
    ended: { label: "已结束", tone: "muted" },
});
const FLAG_LABELS = Object.freeze([["cleared", "通关"], ["three_star", "★3"], ["four_star", "★4"], ["episode", "剧情"]]);
const WINDOWS_NOTE = "原生窗口来自基础资源，生效窗口是 mission-windows 规则覆盖后的结果；"
    + "窗口按开始时刻含、结束时刻不含判定，成长活动窗口按客户端时钟判定。";
const PRESET_NOTE = "预设是完整的 mission-windows 规则文档：按批次键（成长活动按 MissionID）整组配置窗口。"
    + "校验通过后在配置页按内容档切换，默认暂存到下次启动；切换后要重新运行 prepare-client 并重启客户端。";
const JOURNAL_NOTE = "战斗日志只读：开战、结算和中止都由客户端请求或 battle 域命令写入。";
const ENTRUST_NOTE = "委任目录按服务器时间实时计算；定义与预览按本地客户端的平台码报价。";

export default Object.freeze({ title: "战斗", order: 90, mount });

function mount(root, { signal, params, navigate }) {
    const offsets = offsetsOf(params, Object.values(PARAMS));
    const go = pagedRoute(navigate, ROUTE, offsets);
    const windows = viewPanel(WINDOWS_VIEW, undefined, (data) => renderWindows(data, signal), { signal });
    const journal = viewPanel(JOURNAL_VIEW, undefined, renderJournal, { signal });
    const records = viewPanel(RECORDS_VIEW, { offset: offsets[PARAMS.records], limit: PAGE_LIMIT },
        (data) => renderRecords(data, go(PARAMS.records)), { signal });
    const archive = viewPanel(RECORD_ARCHIVE_VIEW, undefined, renderRecordArchive, { signal });
    const entrust = viewPanel(ENTRUST_VIEW, undefined, renderEntrust, { signal });
    const availability = viewPanel(ENTRUST_AVAILABILITY_VIEW, undefined, renderAvailability, { signal });
    const towers = viewPanel(TOWERS_VIEW, undefined, renderTowers, { signal });
    const subjugation = viewPanel(SUBJUGATION_VIEW, undefined, (data) => outcome(data.subjugation, renderDetails),
        { signal });
    const progress = viewPanel(SUBJUGATION_PROGRESS_VIEW, undefined, (data) => outcome(data.progress, renderDetails),
        { signal });
    const panels = [windows, journal, records, archive, entrust, availability, towers, subjugation, progress];
    const refresh = () => panels.forEach((panel) => panel.refresh());
    root.append(
        section("窗口", notice("info", WINDOWS_NOTE), windows.element),
        section("战斗日志", notice("info", JOURNAL_NOTE), journal.element),
        section("关卡记录", records.element, archive.element),
        section("委任", notice("info", ENTRUST_NOTE), entrust.element, availability.element, entrustInspector(signal)),
        section("英傑の塔", towers.element),
        section("大討伐", subjugation.element, progress.element),
        section("命令", commandPanel(DOMAIN, { signal, onCommitted: refresh })));
}

function stateBadge(state) {
    return state === null || state === undefined ? "—" : labelled(STATE_LABELS, state);
}

function bound(value) {
    return value === null || value === undefined ? "不限" : formatInstant(value);
}

function windowText(window) {
    return window ? `${bound(window.start_at)} ～ ${bound(window.end_at)}` : "—";
}

function windowCell(window, state, configured) {
    return h("span", {}, windowText(window), " ", stateBadge(state),
        configured ? [" ", badge("已配置", "info")] : null);
}

const BATCH_COLUMNS = [
    { label: "批次", cell: (batch) => code(batch.key) },
    { label: "成员任务", cell: (batch) => list(batch.members.map((member) => member.mission_id)) },
    { label: "原生窗口", cell: (batch) => windowCell(batch.original, batch.original_state, false) },
    { label: "生效窗口", cell: (batch) => windowCell(batch.window, batch.state, batch.configured) },
];

const CAMPAIGN_COLUMNS = [
    { label: "MissionID", cell: (mission) => mission.mission_id },
    { label: "关卡", cell: (mission) => list(mission.quests) },
    { label: "原生窗口", cell: (mission) => windowCell(mission.original, mission.original_state, false) },
    { label: "生效窗口", cell: (mission) => windowCell(mission.window, mission.state, mission.configured) },
];

function supportBadges(support) {
    const entries = Object.entries(support ?? {});
    if (!entries.length) return "—";
    return h("span", {}, entries.map(([questType, mode]) => [
        badge(`类型 ${questType}：${mode.enabled ? "已支持" : "未支持"}`, mode.enabled ? "ok" : "warn"), " "]));
}

function renderWindows(data, signal) {
    return outcome(data, (windows) => h("div", {},
        keyValues([
            ["窗口规则", windows.enabled ? badge("已启用", "ok") : badge("未启用", "muted")],
            ["规则摘要", windows.rules_hash === null ? "—" : digest(windows.rules_hash)],
            ["启动核对", renderVerification(windows)],
        ]),
        windows.categories.map((category) => h("div", {},
            h("h3", { class: "subheading" }, `${category.kind}（分类 ${category.category}）`),
            keyValues([["配置表", code(category.table)], ["模式支持", supportBadges(category.support)]]),
            table(BATCH_COLUMNS, category.batches, { empty: "这个分类没有可配置的批次。" }))),
        h("h3", { class: "subheading" }, "成长活动窗口"),
        keyValues([["配置表", code(windows.growth_campaigns.table)],
            ["客户端时钟", formatInstant(windows.growth_campaigns.clock)]]),
        table(CAMPAIGN_COLUMNS, windows.growth_campaigns.missions, { empty: "没有成长活动。" }),
        h("h3", { class: "subheading" }, "窗口预设"),
        presetEditor(windows.preset, signal)));
}

function renderVerification(windows) {
    if (windows.error) return h("span", {}, badge("不一致", "danger"), " ", code(windows.error));
    if (windows.verification?.verified === true) return badge("一致", "ok");
    return badge("未核对", "muted");
}

function presetEditor(preset, signal) {
    const editor = h("textarea", { rows: 12, spellcheck: false, dataset: { preset: preset.config_key } });
    editor.value = JSON.stringify(preset.document, null, 2);
    const status = h("div", { "aria-live": "polite" });

    async function validate() {
        let document;
        try {
            document = JSON.parse(editor.value);
        } catch (error) {
            status.replaceChildren(renderError({ code: PRESET_INVALID, message: String(error?.message ?? error) }));
            return;
        }
        status.replaceChildren(loading("正在提交校验…"));
        try {
            const started = await once(OPERATIONS, `windows-preset:${JSON.stringify(document)}`,
                (opId) => api.post(ROUTES.validations, { changes: { [preset.config_key]: document }, op_id: opId }));
            status.replaceChildren(notice("info", "校验作业 ", code(started.job.job_id), " 已开始：请到 ",
                pageLink("config", "配置页"), " 查看报告并切换。"));
        } catch (error) {
            status.replaceChildren(...renderAttemptFailure(error, "再次校验"));
        }
    }

    return h("div", {},
        notice("info", PRESET_NOTE),
        editor,
        toolbar(button("校验预设", { tone: "primary", action: "validate-windows", onClick: validate })),
        status);
}

function renderJournal(data) {
    return outcome(data.journal, (journal) => h("div", {},
        keyValues([["schema", journal.schema_version], ["下一序号", journal.next_sequence],
            ["处理过的开战键", journal.processed_keys]]),
        renderActive(journal.active),
        h("h3", { class: "subheading" }, "待寄战斗奖励"),
        recordsTable(journal.pending_presents, { empty: "没有待寄的战斗奖励。" }),
        renderValue(journal.mode_progress, { summary: "模式进度" }),
        renderValue(journal.history, { summary: "战斗历史" })));
}

function renderActive(active) {
    if (!active) return notice("ok", "没有进行中的战斗。");
    const matches = active.matches_running_generation;
    return notice("warn", "战斗进行中：", code(active.quest_id), ` 序号 ${active.sequence}，扣费 ${active.charge}。`,
        matches === null ? badge("无法核对世代", "muted")
            : matches ? badge("属于运行中的世代", "ok") : badge("属于其他世代", "danger"),
        " ", renderValue(active, { summary: "开战详情" }));
}

function flagBadges(flags) {
    const raised = FLAG_LABELS.filter(([name]) => flags[name]).map(([, label]) => badge(label, "info"));
    const slots = flags.slots.length ? badge(`格 ${flags.slots.join("/")}`, "muted") : null;
    return raised.length || slots ? h("span", {}, raised.map((item) => [item, " "]), slots) : "—";
}

const RECORD_COLUMNS = [
    { label: "QuestID", cell: (row) => row.QuestID },
    { label: "MaxRank", cell: (row) => row.MaxRank },
    { label: "标志", cell: (row) => flagBadges(row.flags) },
    { label: "剩余强袭", cell: (row) => row.RemainAssault },
    { label: "首通日", cell: (row) => row.FirstDefeatDate },
    { label: "显示", cell: (row) => (row.Shown ? "是" : "否") },
];

function renderRecords(data, go) {
    return h("div", {}, table(RECORD_COLUMNS, data.items, { empty: "没有关卡记录。" }), pager(data, go));
}

function renderRecordArchive(data) {
    return h("div", {},
        h("h3", { class: "subheading" }, "归档的关卡记录"),
        outcome(data.archived, (archived) => recordsTable(archived.rows, { empty: "没有归档的关卡记录。" })),
        keyValues([
            ["运行中主表缺少的记录", outcome(data.running_master, (master) => list(master.missing))],
            ["候选主表", data.candidate === null ? "—"
                : outcome(data.candidate, (candidate) => h("span", {}, code(candidate.generation_id),
                    ` 缺少：${candidate.missing.length ? candidate.missing.join("、") : "无"}`))],
        ]));
}

function renderEntrust(data) {
    return h("div", {},
        outcome(data.status, (entrust) => renderValue(entrust.status, { summary: "委任目录状态" })),
        outcome(data.history, (entrust) => renderValue(entrust.history, { summary: "委任记录" })));
}

function renderAvailability(data) {
    return outcome(data, (availability) => h("div", {},
        keyValues([["平台码", availability.platform_code]]),
        renderValue(availability.types, { summary: "各委任类型" })));
}

function entrustInspector(signal) {
    const quest = h("input", { type: "text", inputMode: "numeric", placeholder: "QuestID" });
    const quantity = h("input", { type: "text", inputMode: "numeric", value: "1" });
    const result = h("div", { class: "view", dataset: { view: ENTRUST_QUEST_VIEW } });
    const newest = newestRead();

    function read(view, params) {
        result.dataset.view = view;
        present(result, newest(async () => (await api.view(view, params, { signal })).data),
            (data) => renderDetails(data));
    }

    function chosen() {
        const questId = Number(quest.value);
        return Number.isSafeInteger(questId) && questId > 0 ? questId : null;
    }

    function describe() {
        const questId = chosen();
        if (questId !== null) read(ENTRUST_QUEST_VIEW, { quest_id: questId });
    }

    function preview() {
        const questId = chosen();
        const count = Number(quantity.value);
        if (questId === null || !Number.isSafeInteger(count) || count < 1) return;
        read(ENTRUST_PREVIEW_VIEW, { quest_id: questId, quantity: count });
    }

    return h("div", {},
        toolbar(h("label", {}, "QuestID ", quest), h("label", {}, "份数 ", quantity),
            button("查看定义", { action: "describe-entrust", onClick: describe }),
            button("预览产出", { tone: "primary", action: "preview-entrust", onClick: preview })),
        result);
}

function renderTowers(data) {
    return h("div", {},
        outcome(data.status, (towers) => renderValue(towers.status, { summary: "塔服务状态" })),
        outcome(data.state, renderDetails));
}

function renderDetails(data) {
    const pairs = Object.entries(data).filter(([name]) => name !== "ok");
    return keyValues(pairs.map(([name, value]) => [name,
        value !== null && typeof value === "object" ? renderValue(value, { summary: `${name} 详情` }) : value]));
}
