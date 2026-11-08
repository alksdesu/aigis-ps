import { api, AUTO_BACKUP_KEEP, isDefinite, ROUTES } from "../api.js";
import {
    badge, button, code, cursorList, digest, fill, formatInstant, formatSeconds, h, keyValues, loading, notice,
    renderChecks, renderDiff, renderError, section, table, toolbar,
} from "../diff-view.js";
import {
    auditNote, confirmPhrases, OPERATIONS, once, PLAN_REFUSALS, renderAttemptFailure, renderOutcome,
} from "../plan-flow.js";

const PAGE_SIZE = 50;
const MAX_REASON = 200;
// The server takes one printable line: Unicode "Other" and "Separator" characters are refused except the ASCII space.
const UNPRINTABLE = /(?! )[\p{C}\p{Z}]/u;
const SECONDS_PER_HOUR = 3600;
const HOURS_PER_DAY = 24;
const AUTO_KIND = "auto";
const KIND_LABELS = Object.freeze({ auto: "自动", manual: "手动" });
const SAME_STATE_NOTE = "目标与当前存档相同，不会自动备份。";
const UNCHANGED_NOTE = "目标与当前存档相同：没有自动备份，存档和修订号都没有改变。";

export default Object.freeze({ title: "备份与恢复", order: 50, mount });

function mount(root, { signal }) {
    const retention = h("div");
    const restoreStatus = h("div", { "aria-live": "polite" });
    const restoreArea = h("div", {}, h("p", { class: "muted" }, "从备份列表选择一份，或输入数据目录内的备份路径后预览。"));
    let restorePending = false;
    const backups = cursorList({
        fetchPage: async (cursor) => {
            const page = await api.get(ROUTES.backups, { query: { cursor, limit: PAGE_SIZE }, signal });
            fill(retention, renderRetention(page.retention));
            return page;
        },
        renderItems: (items) => renderBackups(items, preview),
        empty: "还没有登记的备份。", end: "已经是最早的备份。", older: "更早的备份",
    });
    root.append(
        section("保留规则", retention),
        section("创建手动备份", manualBackup(signal, backups.refresh)),
        section("备份列表", toolbar(button("刷新", { action: "refresh", onClick: () => backups.refresh() })), backups.element),
        section("恢复", restoreNotes(), pathPreview(preview), restoreStatus, restoreArea),
        section("导出", exporter(signal)));

    function pending(waiting) {
        restorePending = waiting;
        if (!waiting) restoreStatus.replaceChildren();
    }

    async function preview(path) {
        if (restorePending) {
            // A new preview drops the same-ID retry, and a second restore's auto backup would replace the pre-restore copy.
            restoreStatus.replaceChildren(notice("warn",
                "上一次恢复的结果未知。请先在下面重试同一次恢复；确认结果之前不能预览其他恢复，以免重复执行并挤掉恢复前的自动备份。"));
            return;
        }
        restoreStatus.replaceChildren();
        restoreArea.replaceChildren(loading("正在读取备份并预览恢复…"));
        try {
            const plan = await api.post(ROUTES.restorePreview, { backup: path }, { signal });
            restoreArea.replaceChildren(restorePanel(plan, { reload: backups.refresh, onPending: pending }));
        } catch (error) {
            if (error?.name !== "AbortError") restoreArea.replaceChildren(renderError(error));
        }
    }

    backups.refresh();
}

function lifetime(seconds) {
    const hours = seconds / SECONDS_PER_HOUR;
    return Number.isInteger(hours / HOURS_PER_DAY) ? `${hours / HOURS_PER_DAY} 天` : `${hours} 小时`;
}

function renderRetention(retention) {
    if (!retention) return null;
    return notice("info",
        `自动备份只保留最新 ${retention.auto_keep} 份，超过 ${lifetime(retention.auto_lifetime_seconds)}即删除，因此可能一份不剩；`,
        retention.manual_pruned ? "手动备份也会被清理。" : "手动备份永不自动删除。",
        "恢复、迁移切换、运行中的内容切换、破坏性命令和批量命令改动存档之前都会自动备份；不改动存档时不备份。");
}

function manualBackup(signal, reload) {
    const reason = h("input", { type: "text", class: "input", autocomplete: "off", placeholder: `备份原因（必填，最多 ${MAX_REASON} 字）` });
    const status = h("div", { "aria-live": "polite" });
    const create = button("创建备份", { tone: "primary", action: "backup", onClick: async () => {
        const text = reason.value.trim();
        if (!text || [...text].length > MAX_REASON || UNPRINTABLE.test(text)) {
            status.replaceChildren(notice("warn",
                `请填写 1 到 ${MAX_REASON} 个字符的单行原因，不能包含全角空格、制表符或其他不可打印字符。`));
            return;
        }
        create.disabled = true;
        status.replaceChildren(loading("正在备份…"));
        try {
            const { backup } = await api.post(ROUTES.backups, { reason: text }, { signal });
            status.replaceChildren(notice("ok", "已创建手动备份：", code(backup.path), `（修订号 ${backup.revision}）`));
            reason.value = "";
            reload();
        } catch (error) {
            status.replaceChildren(renderError(error));
        } finally {
            create.disabled = false;
        }
    } });
    return h("div", {}, h("p", { class: "muted" }, "用 SQLite 在线备份复制当前存档，完整性检查通过后登记 sha256，写在数据目录的 backups 下。"),
        h("label", { class: "field" }, h("span", { class: "field-label" }, "原因"), reason), toolbar(create), status);
}

function renderBackups(items, preview) {
    return table([
        { label: "编号", cell: (row) => row.id },
        { label: "类型", cell: (row) => badge(KIND_LABELS[row.kind] ?? row.kind, row.kind === AUTO_KIND ? "info" : "muted") },
        { label: "原因", cell: (row) => row.reason },
        { label: "修订号", cell: (row) => row.revision },
        { label: "时间", cell: (row) => formatInstant(row.created_at) },
        { label: "文件", cell: (row) => code(row.path) },
        { label: "sha256", cell: (row) => digest(row.sha256) },
        { label: "", cell: (row) => button("预览恢复", { action: "preview-restore", onClick: () => preview(row.path) }) },
    ], items);
}

function restoreNotes() {
    return h("ul", { class: "muted" },
        h("li", {}, "只接受数据目录（runtime/server）内的备份，不能恢复运行中的库文件本身。"),
        h("li", {}, "缺少能量时钟标记的旧备份需先离线执行 energy-clock-plan 与 energy-clock-apply。"),
        h("li", {}, "服务无法启动时改用离线的 admin-restore-plan 与 admin-restore-apply。"));
}

function pathPreview(preview) {
    const path = h("input", { type: "text", class: "input", autocomplete: "off", spellcheck: false,
        placeholder: "例如 backups/admin-manual-….sqlite3（相对数据目录）或绝对路径" });
    return h("div", {}, h("label", { class: "field" }, h("span", { class: "field-label" }, "备份路径"), path),
        toolbar(button("预览恢复", { action: "preview-path", onClick: () => {
            if (path.value.trim()) preview(path.value.trim());
        } })));
}

function gateHints(plan) {
    const client = plan.client ?? {};
    const changed = plan.state_sha256_before !== plan.state_sha256_after;
    return h("ul", { class: "gate-hints" },
        h("li", {}, "需要确认游戏客户端已关闭，且 ", formatSeconds(client.quiet_seconds), " 内没有游戏请求；当前",
            client.idle_seconds === null ? "启动以来没有游戏请求" : `距上次请求 ${formatSeconds(client.idle_seconds)}`, " ",
            client.quiet ? badge("已静默", "ok") : badge("未静默", "warn")),
        h("li", {}, "需要逐字输入确认短语", plan.cross_account ? "，跨账号恢复另需一条短语" : "", "。"),
        h("li", {}, plan.auto_backup
            ? `执行前会在切换栅栏内自动备份当前存档；自动备份只保留最新 ${AUTO_BACKUP_KEEP} 份，若恢复来源本身是自动备份，执行后它会被这份新备份替换。`
            : SAME_STATE_NOTE),
        h("li", {}, changed ? "修订号继续递增，不替换运行中的库文件；恢复后请重启游戏客户端。" : "存档不会改变，修订号保持不变。"));
}

function renderRestored(outcome) {
    return h("div", {}, renderOutcome(outcome),
        outcome.backup_before_restore === null ? notice("info", UNCHANGED_NOTE) : null);
}

function restorePanel(plan, { reload, onPending }) {
    const closed = h("input", { type: "checkbox", class: "checkbox" });
    const result = h("div", { "aria-live": "polite" });
    const execute = button("执行恢复", { tone: "danger", action: "restore", disabled: !plan.committable, onClick: async () => {
        const phrases = await confirmPhrases(plan.confirmations, {
            title: "确认恢复备份",
            note: plan.auto_backup ? "当前存档会被替换为备份内容；执行前会先自动备份当前存档。" : SAME_STATE_NOTE,
        });
        if (phrases === null) return;
        execute.disabled = true;
        result.replaceChildren(loading("正在恢复…"));
        try {
            const outcome = await once(OPERATIONS, `restore:${plan.restore_digest}`, (opId) => api.post(ROUTES.restores, {
                backup: plan.backup.path, restore_digest: plan.restore_digest, expected_revision: plan.expected_revision,
                op_id: opId, client_closed: closed.checked, confirmations: phrases,
            }));
            onPending(false);
            result.replaceChildren(renderRestored(outcome));
            reload();
        } catch (error) {
            onPending(!isDefinite(error));
            result.replaceChildren(...renderAttemptFailure(error, "再次执行恢复"));
            execute.disabled = PLAN_REFUSALS.has(error.code) || !plan.committable;
        }
    } });
    return h("div", { class: "restore", dataset: { digest: plan.restore_digest } },
        keyValues([
            ["备份", code(plan.backup.path)],
            ["备份修订号", plan.backup.revision],
            ["备份 sha256", digest(plan.backup.sha256)],
            ["备份玩家", plan.backup.player_id],
            ["当前玩家", plan.player_id],
            ["当前修订号", plan.expected_revision],
            ["状态摘要", h("span", {}, digest(plan.state_sha256_before), " → ", digest(plan.state_sha256_after))],
        ]),
        plan.cross_account ? notice("danger", "这份备份属于另一个账号：恢复会把当前存档换成另一个玩家的数据。") : null,
        h("h3", { class: "subheading" }, "恢复闸门"), gateHints(plan),
        h("h3", { class: "subheading" }, "检查"), renderChecks(plan.checks), auditNote(plan.checks?.audit),
        plan.committable ? null : notice("danger", "检查未通过，不能执行这次恢复。"),
        h("h3", { class: "subheading" }, "差异"), renderDiff(plan.diff),
        h("label", { class: "inline-choice" }, closed, "我已关闭游戏客户端"),
        toolbar(execute), result);
}

function exporter(signal) {
    const status = h("div", { "aria-live": "polite" });
    const run = button("写出脱敏导出", { action: "export", onClick: async () => {
        run.disabled = true;
        status.replaceChildren(loading("正在导出…"));
        try {
            const { export: written } = await api.post(ROUTES.exports, {}, { signal });
            status.replaceChildren(keyValues([
                ["文件", code(written.path)], ["大小", `${written.bytes} 字节`], ["sha256", digest(written.sha256)],
                ["修订号", written.revision], ["时间", formatInstant(written.created_at)],
            ]));
        } catch (error) {
            status.replaceChildren(renderError(error));
        } finally {
            run.disabled = false;
        }
    } });
    return h("div", {},
        h("p", { class: "muted" }, "导出为脱敏 JSON，写在数据目录的 exports 下，不触发浏览器下载；导出文件带 importable: false，不能再导入或恢复。"),
        toolbar(run), status);
}
