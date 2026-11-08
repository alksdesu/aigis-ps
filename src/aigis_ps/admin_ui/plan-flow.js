import { api, AUTO_BACKUP_KEEP, isDefinite, PLAN_TTL_SECONDS, ROUTES } from "./api.js";
import {
    badge, button, code, fill, h, keyValues, list, loading, notice, present, renderChecks, renderDiff, renderError,
    renderReadback, renderValue, toolbar,
} from "./diff-view.js";
import { schemaForm } from "./schema-form.js";
import { amount } from "./view-kit.js";

// The server refused these plans for good, so only a fresh preview can lead to a commit.
export const PLAN_REFUSALS = new Set(["plan_expired", "plan_changed", "plan_stale", "admin_revision_conflict",
    "admin_operation_conflict"]);
const BACKUP_FIELDS = ["backup_before_commit", "backup_before_restore", "backup_before_switch"];
const AUDIT_TOO_LARGE = "admin_audit_too_large";
const AUDIT_SKIPPED_BY = "preflight";
const AUDIT_SKIPPED_NOTE = "预检没有通过，没有检查审计记录的体积。";
const RETRY_STALE_NOTE = "客户端若有在途请求，重放时会因修订号或内容版本变化收到 *_retry_stale，不会重复发放；请让客户端按回读请求号刷新。";
const BY_PARAMS_NOTE = "是否需要确认短语和提交前的自动备份取决于参数，以预览给出的确认要求为准。";
const NO_COMMANDS = "这个领域还没有可用的管理命令。";
const ignore = () => {};
const everything = () => true;

export class OperationIds {
    #ids = new Map();

    for(key) {
        if (!this.#ids.has(key)) this.#ids.set(key, crypto.randomUUID());
        return this.#ids.get(key);
    }

    peek(key) {
        return this.#ids.get(key) ?? null;
    }

    settle(key) {
        this.#ids.delete(key);
    }
}

export const OPERATIONS = new OperationIds();

export async function once(ids, key, send) {
    const opId = ids.for(key);
    try {
        const result = await send(opId);
        ids.settle(key);
        return result;
    } catch (error) {
        // Until the server answers definitely, every retry must repeat this ID so the operation applies at most once.
        if (isDefinite(error)) ids.settle(key);
        throw error;
    }
}

export function renderAttemptFailure(error, retry) {
    if (isDefinite(error)) return [renderError(error)];
    return [renderError(error), notice("warn", `结果未知。${retry}会沿用同一个操作 ID，已经执行过就只返回原结果。`)];
}

function blockPaste(event) {
    event.preventDefault();
}

export function confirmPhrases(required, { title = "确认破坏性操作", note } = {}) {
    if (!Array.isArray(required) || !required.length) return Promise.resolve([]);
    return new Promise((resolve) => {
        const inputs = required.map(() => h("input", {
            type: "text", class: "input", autocomplete: "off", spellcheck: false,
            on: { paste: blockPaste, drop: blockPaste },
        }));
        const confirm = button("确认执行", { tone: "danger", type: "submit", action: "confirm", disabled: true });
        const cancel = button("取消", { action: "cancel" });
        const titleId = `confirm-title-${crypto.randomUUID()}`;
        const form = h("form", { class: "dialog-form" },
            h("h2", { class: "dialog-title", id: titleId }, title),
            note ? h("p", {}, note) : null,
            h("p", { class: "muted" }, "请逐字输入下列确认短语（不能粘贴）。"),
            required.map((item, index) => h("label", { class: "field" },
                h("span", { class: "field-label" }, item.operation), code(item.phrase), inputs[index])),
            toolbar(cancel, confirm));
        const dialog = h("dialog", { class: "dialog", "aria-labelledby": titleId }, form);
        let settled = false;
        const finish = (value) => {
            if (settled) return;
            settled = true;
            dialog.close();
            dialog.remove();
            resolve(value);
        };
        const sync = () => {
            confirm.disabled = !inputs.every((input, index) => input.value === required[index].phrase);
        };
        inputs.forEach((input) => input.addEventListener("input", sync));
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            if (!confirm.disabled) finish(inputs.map((input) => input.value));
        });
        cancel.addEventListener("click", () => finish(null));
        dialog.addEventListener("cancel", (event) => {
            event.preventDefault();
            finish(null);
        });
        dialog.addEventListener("close", () => finish(null));
        document.body.append(dialog);
        dialog.showModal();
        inputs[0].focus();
    });
}

function revisions(result) {
    const { revision_before: before, revision_after: after } = result;
    if (!Number.isInteger(before) || !Number.isInteger(after)) return undefined;
    return before === after ? `${before}（未变化）` : `${before} → ${after}`;
}

function staleForClient(result) {
    const { revision_before: before, revision_after: after, receipts_invalidated: invalidated } = result;
    return (Number.isInteger(before) && Number.isInteger(after) && before !== after)
        || (Array.isArray(invalidated) && invalidated.length > 0);
}

function backupOf(result) {
    const field = BACKUP_FIELDS.find((name) => name in result);
    if (field === undefined) return undefined;
    const backup = result[field];
    return backup === null ? "无" : h("span", {}, code(backup.path ?? "?"), ` 修订号 ${backup.revision ?? "?"}`);
}

function renderSteps(steps) {
    if (!Array.isArray(steps) || !steps.length) return null;
    return h("ol", { class: "steps" }, steps.map((step) => h("li", {},
        code(step.operation), " ", renderValue(step.effects, { summary: "效果" }),
        step.warnings?.length ? notice("warn", step.warnings.join("；")) : null)));
}

export function renderOutcome(result) {
    const replayed = result.replayed === true;
    return h("div", { class: "outcome", dataset: { replayed: String(replayed) } },
        replayed
            ? notice("warn", badge("重放", "warn"), " 这个操作 ID 已经提交过，下面是原来的结果，没有再次执行。")
            : notice("ok", badge("已提交", "ok"), " 操作已写入。"),
        keyValues([
            ["操作 ID", result.op_id === undefined ? undefined : code(result.op_id)],
            ["修订号", revisions(result)],
            ["世代", result.generation_id ?? result.generation_after],
            ["自动备份", backupOf(result)],
            ["客户端回读请求号", "readback" in result ? renderReadback(result.readback) : undefined],
            ["失效的回执域", Array.isArray(result.receipts_invalidated) ? list(result.receipts_invalidated) : undefined],
        ]),
        result.client_restart_required ? notice("warn", "存档已改变，请重启游戏客户端后再继续游戏。") : null,
        renderSteps(result.steps),
        staleForClient(result) ? notice("info", RETRY_STALE_NOTE) : null,
        renderValue(result, { summary: "完整结果" }));
}

export function auditNote(audit) {
    if (audit?.skipped === AUDIT_SKIPPED_BY) return notice("info", AUDIT_SKIPPED_NOTE);
    const details = audit?.error?.code === AUDIT_TOO_LARGE ? audit.error.details : undefined;
    if (details?.fields === null || typeof details?.fields !== "object") return null;
    return notice("danger", `这些审计字段超过了每个字段 ${amount(details.limit)} 字节的上限：`,
        list(Object.entries(details.fields).map(([name, bytes]) => `${name}（${amount(bytes)} 字节）`)));
}

export function renderPlan(plan) {
    return h("div", { class: "plan" },
        keyValues([
            ["预期修订号", plan.expected_revision],
            ["世代", plan.generation_id],
            ["冻结时间（JST）", plan.context?.now_jst],
            ["原生取得日", plan.context?.acquisition_day],
            ["涉及域", list(plan.touched)],
            ["写入字段", list(plan.writes)],
            ["自动备份", plan.auto_backup ? `提交前自动备份（自动备份只保留最新 ${AUTO_BACKUP_KEEP} 份）` : "不需要"],
        ]),
        renderSteps(plan.steps),
        h("h3", { class: "subheading" }, "检查"),
        renderChecks({ preflight: plan.preflight, restart_check: plan.restart_check, audit: plan.audit }),
        auditNote(plan.audit),
        h("h3", { class: "subheading" }, "状态差异"),
        renderDiff(plan.diff),
        plan.confirmations?.length ? notice("warn", "包含破坏性命令，提交时需要逐字输入确认短语：",
            h("ul", {}, plan.confirmations.map((item) => h("li", {}, `${item.operation}：`, code(item.phrase))))) : null);
}

export function planFlow({ commands, ids = OPERATIONS, onCommitted = ignore, onFieldErrors = ignore, onPending = ignore }) {
    const body = h("div", { class: "flow-body" });
    const status = h("div", { class: "flow-status", "aria-live": "polite" });
    const previewButton = button("重新预览", { action: "preview", onClick: () => preview() });
    const commitButton = button("提交", { tone: "primary", action: "commit", disabled: true, onClick: () => commit() });
    const element = h("div", { class: "plan-flow" }, body, toolbar(previewButton, commitButton), status);
    let plan = null;
    let unresolved = null;
    let busy = false;

    function sync() {
        previewButton.disabled = busy || unresolved !== null;
        commitButton.disabled = busy || plan === null || plan.committable !== true;
        commitButton.textContent = unresolved === null ? "提交" : "重试提交";
        onPending(unresolved !== null);
    }

    async function preview() {
        if (busy || unresolved !== null) return;
        busy = true;
        plan = null;
        sync();
        status.replaceChildren(loading("正在预览…"));
        try {
            plan = await api.post(ROUTES.plans, { commands });
            body.replaceChildren(renderPlan(plan));
            status.replaceChildren(plan.committable
                ? notice("info", `检查已通过。确认差异后提交；计划在服务端保留 ${PLAN_TTL_SECONDS} 秒。`)
                : notice("danger", "检查未通过，这个计划不能提交。"));
        } catch (error) {
            if (error.code === "admin_params_invalid") onFieldErrors(error.fields);
            body.replaceChildren(renderError(error));
            status.replaceChildren();
        } finally {
            busy = false;
            sync();
        }
    }

    function advice() {
        if (unresolved !== null) {
            return notice("warn", "结果未知。重试提交会沿用操作 ID ", code(unresolved),
                "：已经执行过就只返回原结果。确认结果之前不能重新预览，以免重复执行。");
        }
        return plan === null ? notice("warn", "请重新预览后再提交。") : null;
    }

    async function commit() {
        if (busy || plan === null || plan.committable !== true) return;
        const current = plan;
        const phrases = await confirmPhrases(current.confirmations);
        if (phrases === null) return;
        busy = true;
        sync();
        status.replaceChildren(loading("正在提交…"));
        try {
            const result = await once(ids, current.plan_digest, (opId) => api.post(ROUTES.commits, {
                plan_id: current.plan_id, plan_digest: current.plan_digest, expected_revision: current.expected_revision,
                op_id: opId, confirmations: phrases,
            }));
            plan = null;
            unresolved = null;
            body.replaceChildren(renderOutcome(result));
            status.replaceChildren();
            onCommitted(result);
        } catch (error) {
            unresolved = isDefinite(error) ? null : ids.peek(current.plan_digest);
            if (PLAN_REFUSALS.has(error.code)) plan = null;
            fill(status, renderError(error), advice());
        } finally {
            busy = false;
            sync();
        }
    }

    return Object.freeze({ element, preview, commit });
}

export function paramsProblems(fields) {
    return fields.filter((item) => (item.step ?? 0) === 0).map((item) => {
        const loc = Array.isArray(item.loc) ? item.loc : [];
        return { path: loc[0] === "params" ? loc.slice(1) : loc, message: item.msg ?? item.type ?? "" };
    });
}

function destructiveBadge(command) {
    if (command.destructive_by_params) return badge("按参数确认", "warn");
    return command.destructive ? badge("破坏性", "danger") : null;
}

export function commandForm(command, { ids = OPERATIONS, onCommitted = ignore, initial, onForm = ignore } = {}) {
    const form = schemaForm(command.params_schema, { value: initial });
    onForm(form);
    const flowArea = h("div", { class: "command-flow" });
    const previewButton = button("预览", { tone: "primary", action: "preview-command", disabled: !command.available });
    const pending = (waiting) => {
        // A new plan would draw a new operation ID while the last commit may still have been applied.
        previewButton.disabled = !command.available || waiting;
    };
    previewButton.addEventListener("click", () => {
        const read = form.read();
        if (!read.ok) return;
        const flow = planFlow({
            commands: [{ operation: command.operation, params: read.value }], ids, onCommitted, onPending: pending,
            onFieldErrors: (fields) => form.showErrors(paramsProblems(fields)),
        });
        flowArea.replaceChildren(flow.element);
        flow.preview();
    });
    return h("section", { class: "command", dataset: { operation: command.operation } },
        h("header", { class: "command-head" }, h("h3", { class: "command-title" }, command.operation),
            badge(command.domain),
            destructiveBadge(command),
            command.available ? null : badge("不可用", "warn")),
        command.available ? null : notice("warn", command.unavailable_reason ?? "当前世代不能执行这条命令。"),
        command.destructive_by_params ? notice("info", BY_PARAMS_NOTE) : null,
        keyValues([["涉及域", list(command.touched)], ["写入字段", list(command.writes)]]),
        form.element, toolbar(previewButton), flowArea);
}

export function commandCatalog(signal) {
    let pending = null;
    return () => {
        // Panels of one page share a read, and a failed read is dropped so that a retry asks the server again.
        pending ??= api.get(ROUTES.commands, { signal }).catch((error) => {
            pending = null;
            throw error;
        });
        return pending;
    };
}

async function initialFor(initial, command) {
    const source = Object.hasOwn(initial, command.operation) ? initial[command.operation] : undefined;
    return typeof source === "function" ? source(command) : source;
}

export function commandPanel(domain, {
    signal, ids = OPERATIONS, onCommitted = ignore, catalog = commandCatalog(signal), select = everything, initial = {},
    empty = NO_COMMANDS, onForm = ignore,
} = {}) {
    const element = h("div", { class: "command-panel", dataset: { domain } });
    present(element, async () => {
        const { commands } = await catalog();
        const chosen = commands.filter((command) => command.domain === domain && select(command));
        return Promise.all(chosen.map(async (command) => ({ command, initial: await initialFor(initial, command) })));
    }, (entries) => (entries.length
        ? entries.map((entry) => commandForm(entry.command, {
            ids, onCommitted, initial: entry.initial, onForm: (form) => onForm(entry.command, form),
        }))
        : h("p", { class: "muted" }, empty)));
    return element;
}
