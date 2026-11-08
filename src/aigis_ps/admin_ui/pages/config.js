import {
    ADMIT_TIMEOUT_SECONDS, api, AUTO_BACKUP_KEEP, CANDIDATE_TTL_SECONDS, isDefinite, ROUTES, watch,
} from "../api.js";
import {
    badge, buildBadge, button, code, digest, fill, formatInstant, formatSeconds, h, keyValues, list, loading, notice,
    present, renderChecks, renderDiff, renderError, renderValue, section, table, toolbar,
} from "../diff-view.js";
import { auditNote, confirmPhrases, OPERATIONS, once, renderAttemptFailure, renderOutcome } from "../plan-flow.js";
import { amount, newestRead, PAGE_LIMIT, pager, semanticLabel, semantics, sourceLabel } from "../view-kit.js";

const OVERVIEW_REFRESH_MS = 15000;
const JOB_POLL_MS = 1500;
const JSON_ROWS = 10;
const BUILD_RUNNING = "running";
const EDIT = Object.freeze({ KEEP: "keep", DEFAULT: "default", CUSTOM: "custom" });
const MODES = Object.freeze({ APPLY: "apply", STAGE: "stage" });
const CONTENT_TIER = "content";
const STAGED_TIER = "staged";
const EDIT_CHOICES = [[EDIT.KEEP, "保持不变"], [EDIT.DEFAULT, "恢复代码默认值"], [EDIT.CUSTOM, "自定义 JSON 文档"]];
const MASTER_NOTE = "主表快照属于内容档：默认暂存到下次启动；运行中应用需要确认客户端已关闭并输入确认短语，之后要运行 prepare-client 并重启客户端。";
const IMPORT_NOTE = "导入只把采集写成数据目录里的主表快照，不改变运行中的世代和下次启动；导入后会自动选作校验目标。";
const VALIDITY = Object.freeze(["有效", "无效"]);
const IMPORTABILITY = Object.freeze(["是", "否"]);
const MASTER_UNPUBLISHED = "切换已提交但主表指针没有更新；下次切换或启动服务时会按切换记录前滚修复。";
const TIER_LABELS = Object.freeze({ hot: "热切换", gated: "迁移切换", content: "内容切换", staged: "暂存", unstaged: "撤销暂存" });
const TIER_TONES = Object.freeze({ hot: "ok", gated: "warn", content: "danger" });
const TIER_NOTES = Object.freeze({
    hot: "只改服务端规则且 initialize 没有差异：直接换世代，在途请求在原世代内完成。不校验修订号；客户端在途请求重放可能收到 *_retry_stale，这是预期行为。",
    gated: `需要迁移存档（initialize 有差异、时间累积规则变化、料理阈值或新手奖励总数变化）：存档有变化时切换前自动备份，并在切换栅栏内与审计同事务提交。栅栏期间游戏请求最多等待约 ${ADMIT_TIMEOUT_SECONDS} 秒，请在客户端空闲时操作。`,
    content: "资源版本或主表变化：默认只暂存，下次启动生效。运行中切换需要确认客户端已关闭、在静默阈值内没有游戏请求并输入确认短语，之后要运行 prepare-client 并重启客户端。",
});
const TOWER_PRODUCT_REUSED = "tower_trade_product_reused";
const NOTICE_TEXTS = Object.freeze({ [TOWER_PRODUCT_REUSED]: towerProductNotice });
const STAGE_NOTE = "只写入配置集与 active.json，下次启动服务时生效；不备份、不进切换栅栏、不要求关闭客户端。";
const BUILD_NOTE = "构建候选世代约需 16 秒、约 167 MiB 内存，期间会争抢 CPU，游戏请求可能短暂变慢。";
const REPORT_PLACEHOLDER = "校验完成后在这里显示报告。";
const SWITCH_PENDING_NOTE = "上一次切换的结果未知。请先在报告里重试同一次切换；确认结果之前不能刷新报告，也不能开始新的校验。";
const REPORT_SPENT_NOTE = "这份报告已经用过或已失效，请刷新报告或重新校验。";
// The server can never carry out these reports again, so only a refreshed report or a new validation can switch.
const REPORT_REFUSALS = new Set(["config_report_expired", "config_report_stale", "generation_candidate_missing",
    "config_tier_changed", "admin_revision_conflict"]);

export default Object.freeze({ title: "配置", order: 40, mount });

function mount(root, { signal }) {
    const overview = h("div", {}, loading());
    const unstageArea = h("div", { "aria-live": "polite" });
    const masterChoice = h("div", { "aria-live": "polite" });
    const editorArea = h("div", {}, loading());
    const jobArea = h("div", {}, h("p", { class: "muted" }, "还没有校验作业。"));
    const jobNote = h("div", { "aria-live": "polite" });
    const reportArea = h("div", {}, h("p", { class: "muted" }, REPORT_PLACEHOLDER));
    const historyArea = h("div");
    const content = contentPanel({ signal, choose: chooseMaster });
    root.append(section("当前配置", overview, unstageArea), section("编辑", masterChoice, editorArea),
        section("主表快照与采集", content.element), section("校验", jobArea, jobNote),
        section("报告与切换", reportArea), section("切换记录", historyArea));
    let editors = null;
    let editorsGeneration = null;
    let jobStop = null;
    let resumed = false;
    let switchPending = false;
    let masterTarget = null;

    const overviewWatch = watch(() => api.read(ROUTES.config, { signal }), {
        interval: OVERVIEW_REFRESH_MS, signal,
        onData: (config) => {
            overview.replaceChildren(renderOverview(config, { unstage, unstageMaster }));
            historyArea.replaceChildren(renderHistory(config.history));
            // Editors prefill the running documents, so only a newly running generation rebuilds them.
            if (config.running.generation_id !== editorsGeneration) {
                const replaced = editors !== null;
                editorsGeneration = config.running.generation_id;
                editors = configEditors(config.running.config.keys, validate);
                fill(editorArea, replaced ? notice("info", "运行中的配置已变化，编辑器已按新的配置重建。") : null,
                    editors.element);
            }
            if (!resumed && config.validation?.job) {
                resumed = true;
                showJob({ job: config.validation.job, validation: config.validation.validation });
            }
        },
        onError: (error) => {
            overview.replaceChildren(renderError(error));
            if (editors === null) editorArea.replaceChildren(h("p", { class: "muted" }, "读取不到当前配置，暂时不能编辑。"));
        },
    });

    async function validate(changes, status) {
        if (switchPending) {
            // A new validation replaces the report whose switch must be retried under the same operation ID.
            status.replaceChildren(notice("warn", SWITCH_PENDING_NOTE));
            return;
        }
        if (!Object.keys(changes).length && masterTarget === null) {
            status.replaceChildren(notice("info", "没有修改。"));
            return;
        }
        const request = masterTarget === null ? { route: ROUTES.validations, body: { changes } }
            : { route: ROUTES.contentValidations, body: { changes, master_snapshot: masterTarget } };
        status.replaceChildren(loading("正在提交校验…"));
        try {
            const started = await once(OPERATIONS, `validate:${JSON.stringify(request)}`,
                (opId) => api.post(request.route, { ...request.body, op_id: opId }));
            status.replaceChildren(notice("info", "校验作业已开始。"));
            showJob(started);
        } catch (error) {
            editors?.showErrors(error.fields ?? []);
            status.replaceChildren(renderError(error));
        }
    }

    function chooseMaster(snapshot) {
        masterTarget = snapshot;
        fill(masterChoice, snapshot === null ? null : notice("info", "校验时同时把下次使用的主表快照换成 ", code(snapshot), "。",
            MASTER_NOTE, toolbar(button("不切换主表", { action: "clear-master", onClick: () => chooseMaster(null) }))));
    }

    function showJob(view) {
        jobNote.replaceChildren();
        // The server drops the reports of a replaced validation, so their switch controls must go with them.
        reportArea.replaceChildren(h("p", { class: "muted" }, REPORT_PLACEHOLDER));
        renderJob(view);
        follow(view.job.job_id);
    }

    function follow(jobId) {
        jobStop?.abort();
        const stop = new AbortController();
        jobStop = stop;
        const jobSignal = AbortSignal.any([signal, stop.signal]);
        watch(() => api.read(ROUTES.job, { values: { job_id: jobId }, signal: jobSignal }), {
            interval: JOB_POLL_MS, signal: jobSignal,
            onData: (view) => {
                renderJob(view);
                // Each poll after the build re-assesses the whole state on the game lease, so polling stops here.
                if (view.job.state !== BUILD_RUNNING) stop.abort();
            },
            onError: (error) => {
                stop.abort();
                fill(jobArea, renderError(error),
                    toolbar(button("重新读取作业", { action: "follow-job", onClick: () => follow(jobId) })));
            },
        });
    }

    function renderJob(view) {
        const { job, validation } = view;
        fill(jobArea,
            keyValues([
                ["作业", job ? digest(job.job_id) : "—"],
                ["状态", job ? buildBadge(job.state) : "—"],
                ["耗时", job ? formatSeconds(job.elapsed_seconds) : "—"],
                ["候选配置集", validation ? code(validation.config_set.id) : "—"],
                ["修改的键", validation ? list(validation.keys) : "—"],
                ["主表快照", validation?.master_snapshot ? code(validation.master_snapshot) : "不切换"],
            ]),
            job?.state === BUILD_RUNNING ? notice("info", BUILD_NOTE) : null,
            job?.progress?.length ? h("ol", { class: "progress-log" }, job.progress.map((line) => h("li", {}, line))) : null,
            job?.error ? renderError(job.error) : null,
            view.candidate_expired ? notice("warn", `候选世代已过期（保留 ${CANDIDATE_TTL_SECONDS} 秒），请重新校验。`) : null,
            job && job.state !== BUILD_RUNNING
                ? toolbar(button("刷新报告", { action: "refresh-report", onClick: () => refreshReport(job.job_id) })) : null);
        if (view.report) {
            reportArea.replaceChildren(renderReport(view.report, runSwitch));
        } else if (job && job.state !== BUILD_RUNNING && "report" in view) {
            reportArea.replaceChildren(h("p", { class: "muted" }, view.candidate_expired
                ? "候选世代已过期，请重新校验。" : "这次校验没有生成报告。"));
        }
    }

    async function refreshReport(jobId) {
        if (switchPending) {
            // A refreshed report replaces the one that holds the same-ID retry of the unknown switch.
            jobNote.replaceChildren(notice("warn", SWITCH_PENDING_NOTE));
            return;
        }
        jobNote.replaceChildren();
        reportArea.replaceChildren(loading("正在重新评估报告…"));
        try {
            renderJob(await api.get(ROUTES.job, { values: { job_id: jobId }, signal }));
        } catch (error) {
            reportArea.replaceChildren(renderError(error));
        }
    }

    async function runSwitch(report, mode, clientClosed, area) {
        const phrases = await confirmPhrases(report.modes[mode].confirmations, {
            title: "确认运行中切换内容", note: "请先关闭游戏客户端；切换完成后运行 prepare-client 并重启客户端。",
        });
        if (phrases === null) return false;
        area.replaceChildren(loading("正在切换…"));
        try {
            const result = await once(OPERATIONS, `switch:${report.report_id}:${mode}`, (opId) => api.post(ROUTES.switches, {
                report_id: report.report_id, op_id: opId, mode, confirm: { client_closed: clientClosed, confirmations: phrases },
            }));
            switchPending = false;
            area.replaceChildren(renderSwitchOutcome(result));
            overviewWatch.refresh();
            content.refresh();
            return true;
        } catch (error) {
            switchPending = !isDefinite(error);
            const spent = REPORT_REFUSALS.has(error.code);
            fill(area, renderAttemptFailure(error, "再次切换"), spent ? notice("warn", REPORT_SPENT_NOTE) : null);
            return spent;
        }
    }

    async function unstage(staged) {
        unstageArea.replaceChildren(loading("正在撤销暂存…"));
        try {
            const result = await once(OPERATIONS, `unstage:${staged}`,
                (opId) => api.post(ROUTES.unstage, { staged_set: staged, op_id: opId }));
            unstageArea.replaceChildren(renderSwitchOutcome(result));
            overviewWatch.refresh();
        } catch (error) {
            unstageArea.replaceChildren(...renderAttemptFailure(error, "再次撤销暂存"));
        }
    }

    async function unstageMaster(snapshot) {
        unstageArea.replaceChildren(loading("正在撤销主表暂存…"));
        try {
            const result = await once(OPERATIONS, `unstage-master:${snapshot}`,
                (opId) => api.post(ROUTES.contentUnstage, { staged_snapshot: snapshot, op_id: opId }));
            unstageArea.replaceChildren(renderSwitchOutcome(result));
            overviewWatch.refresh();
            content.refresh();
        } catch (error) {
            unstageArea.replaceChildren(...renderAttemptFailure(error, "再次撤销主表暂存"));
        }
    }
}

function renderMasterState(master) {
    if (!master) return h("p", { class: "muted" }, "服务端没有报告主表快照状态。");
    const disk = master.disk;
    return h("div", {},
        keyValues([
            ["运行中的主表", master.in_use ? code(master.in_use) : "无（没有固定的主表快照）"],
            ["磁盘指针", disk ? h("span", {}, code(disk.snapshot), " ", digest(disk.manifest_sha256)) : "无"],
            ["下次启动使用", master.next_start ? code(master.next_start) : "—"],
            ["等待前滚", master.roll_forward_pending ? badge("是", "warn") : "否"],
        ]),
        master.error ? renderError(master.error) : null);
}

function renderOverview(config, { unstage, unstageMaster }) {
    const { running, active } = config;
    const master = config.masterdata;
    const pointer = active.error ? renderError(active.error)
        : active.active ? `配置集 ${active.active.set}，切换序号 ${active.active.sequence}` : "不存在：全部键使用代码默认值";
    return h("div", {},
        config.client_warning ? notice("danger", "内容配置已在运行中切换。观察到客户端重新启动之前请不要继续游戏；请先运行：",
            code(config.client_warning.client_actions?.prepare_client?.command ?? "client/prepare-client.ps1")) : null,
        config.switching ? notice("warn", "正在进行一项配置切换。") : null,
        keyValues([
            ["运行中的配置集", code(running.set)],
            ["运行中的世代", digest(running.generation_id)],
            ["当前世代", config.current_generation_id === running.generation_id ? digest(config.current_generation_id)
                : h("span", {}, digest(config.current_generation_id), " ", badge("与切换记录不符", "warn"))],
            ["active.json", pointer],
            ["下次启动使用", active.next_start ?? "未知"],
            ["等待前滚", active.roll_forward_pending === null ? "未知" : active.roll_forward_pending ? badge("是", "warn") : "否"],
            ["命令行覆盖的键", list(running.config.overridden)],
        ]),
        config.staged ? notice("warn", `已暂存配置集 ${config.staged.set}，下次启动生效。变化的键：${stagedKeys(config.staged)}。`,
            toolbar(button("撤销暂存", { action: "unstage", onClick: () => unstage(config.staged.set) }))) : null,
        h("h3", { class: "subheading" }, "主表快照"),
        renderMasterState(master),
        master?.staged ? notice("warn", "已暂存主表快照 ", code(master.staged), "，下次启动生效。",
            toolbar(button("撤销主表暂存", { action: "unstage-master", onClick: () => unstageMaster(master.staged) }))) : null);
}

function stagedKeys(staged) {
    return staged.keys === null ? "未知（读不出配置集）" : list(staged.keys);
}

function renderHistory(history) {
    return table([
        { label: "序号", cell: (row) => row.sequence },
        { label: "时间", cell: (row) => formatInstant(row.created_at) },
        { label: "配置集", cell: (row) => code(row.config_set) },
        { label: "档位", cell: (row) => badge(TIER_LABELS[row.tier] ?? row.tier, TIER_TONES[row.tier] ?? "muted") },
        {
            label: "世代", cell: (row) => (row.tier === STAGED_TIER
                ? h("span", {}, digest(row.generation_id), " ", badge("未运行的候选，仅供参考")) : digest(row.generation_id)),
        },
        { label: "操作 ID", cell: (row) => (row.op_id ? code(row.op_id) : "—") },
    ], history?.items ?? [], { empty: "还没有配置切换记录。" });
}

function configEditors(entries, validate) {
    const editors = entries.map(keyEditor);
    const status = h("div", { "aria-live": "polite" });
    const submit = button("校验修改", { tone: "primary", action: "validate", onClick: () => {
        const problems = [];
        const changes = {};
        for (const editor of editors) {
            const change = editor.read(problems);
            if (change !== undefined) changes[editor.key] = change;
        }
        if (problems.length) {
            status.replaceChildren(notice("danger", `这些键的文档无效：${list(problems)}`));
        } else {
            validate(changes, status);
        }
    } });
    return {
        element: h("div", {},
            h("p", { class: "muted" }, "自定义文档以当前生效值预填，校验时由原加载器严格检查；没有修改的键沿用运行中的配置集。"),
            editors.map((editor) => editor.element), toolbar(submit), status),
        showErrors(fields) {
            for (const item of fields) {
                const editor = item.loc?.[0] === "changes" ? editors.find((entry) => entry.key === item.loc[1]) : undefined;
                editor?.showError(item.msg ?? item.type);
            }
        },
    };
}

function keyEditor(entry) {
    const select = h("select", { class: "input", disabled: entry.read_only },
        EDIT_CHOICES.map(([value, label]) => h("option", { value }, label)));
    select.value = EDIT.KEEP;
    const area = h("textarea", {
        class: "input textarea json-input", rows: JSON_ROWS, spellcheck: false, hidden: true,
        value: JSON.stringify(entry.effective ?? entry.default ?? {}, null, 2),
    });
    const message = h("p", { class: "field-error", hidden: true });
    select.addEventListener("change", () => { area.hidden = select.value !== EDIT.CUSTOM; });
    const showError = (text) => {
        message.textContent = text;
        message.hidden = false;
    };
    const element = h("details", { class: "config-key", dataset: { key: entry.key } },
        h("summary", {}, code(entry.key), " ", badge(sourceLabel(entry.source), entry.read_only ? "warn" : "muted"),
            entry.resource_hint ? badge("资源相关", "danger") : null, entry.error ? badge("加载失败", "danger") : null),
        keyValues([
            ["消费域", list(entry.domains)],
            ["时间语义", semantics(entry)],
            ["文件", entry.path ? code(entry.path) : "无（代码默认值）"],
            ["内容版本", entry.content_version ? digest(entry.content_version) : "—"],
            ["命令行参数", h("span", {}, code(entry.cli_flag), " / ", code(`-${entry.ps_parameter}`))],
            ["示例文件", list(entry.examples)],
            ["当前生效值", renderValue(entry.effective)],
            ["代码默认值", renderValue(entry.default)],
        ]),
        entry.read_only ? notice("warn", "命令行规则文件覆盖了这个键，本进程内只读；修改需改命令行文件并重启服务。") : null,
        entry.resource_hint ? notice("info", "这个键影响客户端资源，修改会进入内容切换档。") : null,
        entry.error ? renderError(entry.error) : null,
        h("label", { class: "field" }, h("span", { class: "field-label" }, "修改方式"), select), area, message);
    return {
        key: entry.key,
        element,
        showError,
        read(problems) {
            message.hidden = true;
            if (select.value === EDIT.KEEP) return undefined;
            if (select.value === EDIT.DEFAULT) return null;
            try {
                const value = JSON.parse(area.value);
                if (value === null || typeof value !== "object" || Array.isArray(value)) throw new TypeError("配置文档必须是 JSON 对象");
                return value;
            } catch (error) {
                showError(error.message);
                problems.push(entry.key);
                return undefined;
            }
        },
    };
}

function changedFields(before, after) {
    const left = before ?? {};
    const right = after ?? {};
    return [...new Set([...Object.keys(left), ...Object.keys(right)])]
        .filter((name) => JSON.stringify(left[name]) !== JSON.stringify(right[name]))
        .map((name) => ({ name, before: left[name], after: right[name] }));
}

function renderKeyChanges(keys) {
    if (!keys?.length) return h("p", { class: "muted" }, "没有键发生变化。");
    return keys.map((change) => h("div", { class: "key-change" },
        h("p", {}, code(change.key), " ", `${sourceLabel(change.source.before)} → ${sourceLabel(change.source.after)}`,
            change.resource_hint ? badge("资源相关", "danger") : null),
        table([
            { label: "字段", cell: (field) => code(field.name) },
            { label: "之前", cell: (field) => renderValue(field.before, { exact: true }) },
            { label: "之后", cell: (field) => renderValue(field.after, { exact: true }) },
        ], changedFields(change.rules.before, change.rules.after), { empty: "规则值没有变化（文件来源或模式不同）。" }),
        keyValues([
            ["内容版本", h("span", {}, digest(change.content_version.before), " → ", digest(change.content_version.after))],
            ["变化的时间语义", list(change.semantics.map(semanticLabel))],
        ])));
}

function renderReasons(report) {
    const reasons = [];
    if (report.reasons.identity.length) reasons.push(`身份字段变化：${list(report.reasons.identity)}`);
    if (report.reasons.accumulating.length) reasons.push(`时间累积规则变化：${list(report.reasons.accumulating)}`);
    if (report.reasons.initialize) reasons.push("候选世代的 initialize 会改写存档");
    if (report.reasons.cooking) reasons.push("料理阈值变化，存档的料理账本随切换迁移");
    if (report.reasons.beginner) reasons.push("新手奖励总数变化，存档的登录奖励账本随切换重建");
    return reasons.length ? reasons.join("；") : "只改服务端规则";
}

function beforeAfter(change) {
    return `${amount(change?.before)} → ${amount(change?.after)}`;
}

function renderCookingMigration(cooking) {
    return h("div", { dataset: { migration: "cooking" } },
        h("p", {}, "料理账本随切换迁移："),
        keyValues([
            ["业务月", cooking.period],
            ["料理阈值", beforeAfter(cooking.border)],
            ["本月额度", beforeAfter(cooking.quota)],
            ["本月已发", amount(cooking.issued)],
            ["迁移时刻", formatInstant(cooking.migrated_at?.jst)],
        ]));
}

function renderBeginnerMigration(beginner) {
    return h("div", { dataset: { migration: "beginner" } },
        h("p", {}, "登录奖励账本随切换重建："),
        keyValues([
            ["新手奖励总数", beforeAfter(beginner.total)],
            ["已领新手奖励", beforeAfter(beginner.claimed)],
        ]));
}

function towerProductNotice(item) {
    return [`候选世代把这些塔兑换商品 ID 换给了别的商品：${list(item.reused_products)}。存档的购买记录仍属于原商品，`
        + `新世代运行后 ${list(item.refused_operations)} 在改绑之前整体拒绝，不只是这些商品；客户端弹出通信错误并原包重发。用 `,
        code(item.repair), " 逐个改绑这些商品的购买记录后恢复。这条提示不阻止切换。"];
}

function renderNotice(item) {
    return notice("warn", ...(Object.hasOwn(NOTICE_TEXTS, item.code) ? NOTICE_TEXTS[item.code](item)
        : [code(item.code), " ", renderValue(item, { summary: "详情" })]));
}

function renderClientActions(actions) {
    if (!actions) return null;
    return notice("warn", "需要重新准备并重启客户端：", code(actions.prepare_client?.command ?? "client/prepare-client.ps1"),
        h("p", { class: "muted" }, "配置集目录：", code(actions.config_set ?? "—")));
}

function renderReport(report, runSwitch) {
    const tier = report.tier;
    const switches = [];
    return h("div", { class: "report", dataset: { tier } },
        h("p", {}, badge(TIER_LABELS[tier] ?? tier, TIER_TONES[tier] ?? "muted"), " ", TIER_NOTES[tier] ?? ""),
        keyValues([
            ["报告", digest(report.report_id)],
            ["生成于", formatInstant(report.created_at?.jst)],
            ["基于修订号", report.revision],
            ["候选配置集", code(report.validation.config_set.id)],
            ["运行中的配置集", code(report.running_set)],
            ["候选世代", digest(report.generations.candidate.id)],
            ["分档原因", renderReasons(report)],
            ["身份变化", list(report.identity_changes)],
            ["失效的回执域", list(report.receipts_invalidated)],
        ]),
        report.staged ? notice("warn", `已有暂存配置集 ${report.staged.set}${report.staged.is_candidate ? "（就是这个候选）" : ""}。`) : null,
        report.availability.regressions.length
            ? notice("danger", `这些域会从可用变为不可用：${list(report.availability.regressions)}`) : null,
        (report.notices ?? []).map(renderNotice),
        h("h3", { class: "subheading" }, "键变化"), renderKeyChanges(report.keys),
        h("h3", { class: "subheading" }, "主表快照"), renderMasterPlan(report.masterdata),
        h("h3", { class: "subheading" }, "检查"), renderChecks(report.checks), auditNote(report.checks.audit),
        report.battle.in_progress ? notice(report.battle.required ? "danger" : "warn",
            report.battle.required ? "存档中有进行中的战斗，这次切换在战斗结束或中止之前不能应用。" : "存档中有进行中的战斗。",
            report.battle.error ? renderError(report.battle.error) : null) : null,
        h("h3", { class: "subheading" }, "存档迁移"),
        report.migration.changed ? h("div", {},
            report.migration.energy.length
                ? renderValue(report.migration.energy, { summary: "按旧规则结算的时间累积字段", open: true }) : null,
            report.migration.cooking ? renderCookingMigration(report.migration.cooking) : null,
            report.migration.beginner ? renderBeginnerMigration(report.migration.beginner) : null,
            renderDiff(report.migration.diff)) : h("p", { class: "muted" }, "这次切换不改动存档。"),
        renderValue(report.content_versions, { summary: "内容版本变化" }),
        renderClientActions(report.client_actions),
        h("h3", { class: "subheading" }, "切换"),
        h("div", { class: "modes" },
            [MODES.APPLY, MODES.STAGE].map((mode) => renderMode(report, mode, runSwitch, switches))));
}

function renderMode(report, mode, runSwitch, switches) {
    const spec = report.modes[mode];
    const apply = mode === MODES.APPLY;
    const content = report.tier === CONTENT_TIER;
    const area = h("div", { "aria-live": "polite" });
    const closed = apply && content ? h("input", { type: "checkbox", class: "checkbox" }) : null;
    const run = button(apply ? "立即应用" : "暂存到下次启动", {
        tone: apply && content ? "danger" : "primary", action: `switch-${mode}`, disabled: !spec.allowed,
        onClick: async () => {
            switches.forEach((entry) => { entry.run.disabled = true; });
            const spent = await runSwitch(report, mode, closed?.checked === true, area);
            // Every mode switches to the same candidate generation, so a spent report retires all of them.
            switches.forEach((entry) => { entry.run.disabled = spent || !entry.allowed; });
        },
    });
    switches.push({ run, allowed: spec.allowed });
    const client = report.client ?? {};
    const requirements = apply ? [
        report.requirements.backup ? `切换前自动备份（自动备份只保留最新 ${AUTO_BACKUP_KEEP} 份）` : null,
        report.requirements.fence ? `在切换栅栏内提交，游戏请求最多等待约 ${ADMIT_TIMEOUT_SECONDS} 秒` : null,
        report.requirements.client_closed
            ? `需要确认游戏客户端已关闭，且 ${formatSeconds(client.quiet_seconds)} 内没有游戏请求` : null,
    ].filter(Boolean) : [];
    return h("div", { class: "mode", dataset: { mode } },
        h("h4", { class: "mode-title" }, apply ? "立即应用" : "暂存", report.default_mode === mode ? badge("默认", "info") : null),
        h("p", {}, apply ? TIER_NOTES[report.tier] ?? "" : STAGE_NOTE),
        requirements.length ? h("ul", {}, requirements.map((item) => h("li", {}, item))) : null,
        apply && content ? keyValues([
            ["距上次游戏请求", client.idle_seconds === null ? "启动以来没有游戏请求" : formatSeconds(client.idle_seconds)],
            ["客户端已静默", client.quiet ? badge("是", "ok") : badge("否", "warn")],
        ]) : null,
        spec.allowed ? null : h("div", { class: "blockers" }, h("p", {}, "不能执行："), spec.blockers.map(renderError)),
        closed ? h("label", { class: "inline-choice" }, closed, "我已关闭游戏客户端") : null,
        toolbar(run), area);
}

function activationText(activation) {
    return h("span", {}, code(activation.before ?? "无"), " → ", code(activation.target));
}

function renderMasterPlan(master) {
    if (!master || (master.requested === null && master.activation === null)) {
        return h("p", { class: "muted" }, "这次切换不改变主表快照。");
    }
    return keyValues([
        ["请求的快照", master.requested ? code(master.requested) : "未指定（沿用下次启动的快照）"],
        ["运行中的主表", master.in_use ? code(master.in_use) : "—"],
        ["切换后指向", master.target ? code(master.target) : "—"],
        ["磁盘指针变化", master.activation ? activationText(master.activation) : "不变"],
    ]);
}

function masterPointer(result) {
    const activation = result.master_activation;
    if (!activation) return undefined;
    const published = result.masterdata?.published;
    return h("span", {}, activationText(activation), " ", published === false ? badge("未更新", "warn")
        : published ? badge("已更新", "ok") : null);
}

function renderSwitchOutcome(result) {
    const pointer = result.pointer ?? {};
    const master = result.masterdata ?? {};
    return h("div", {},
        renderOutcome(result),
        keyValues([
            ["方式", result.mode ? (result.mode === MODES.APPLY ? "立即应用" : "暂存") : undefined],
            ["档位", result.tier ? TIER_LABELS[result.tier] ?? result.tier : undefined],
            ["配置集", result.previous_set ? h("span", {}, code(result.previous_set), " → ", code(result.config_set))
                : result.config_set ? code(result.config_set) : undefined],
            ["撤销的暂存配置集", result.staged_set ? code(result.staged_set) : undefined],
            ["撤销的主表暂存", result.staged_master ? code(result.staged_master) : undefined],
            ["主表快照", result.master_snapshot ? code(result.master_snapshot) : undefined],
            ["主表指针", masterPointer(result)],
            ["active.json", pointer.published === false ? badge("未更新", "warn") : pointer.set ? `配置集 ${pointer.set}` : undefined],
        ]),
        pointer.published === false ? notice("warn", "切换已提交但 active.json 没有更新；下次切换或启动服务时会按切换记录前滚修复。",
            pointer.publish_error ? renderError(pointer.publish_error) : null) : null,
        master.published === false ? notice("warn", MASTER_UNPUBLISHED,
            master.publish_error ? renderError(master.publish_error) : null) : null,
        renderClientActions(result.client_actions));
}

const TABLE_COLUMNS = [
    { label: "主表", cell: ([name]) => code(name) },
    { label: "行数", cell: ([, summary]) => summary?.rows },
    { label: "列数", cell: ([, summary]) => summary?.columns },
    { label: "完整架构", cell: ([, summary]) => summary?.full_schema },
    { label: "字节", cell: ([, summary]) => summary?.bytes },
    { label: "sha256", cell: ([, summary]) => (summary?.sha256 ? digest(summary.sha256) : "—") },
];

function tableSummaries(tables) {
    return tables ? table(TABLE_COLUMNS, Object.entries(tables)) : h("p", { class: "muted" }, "没有表摘要。");
}

function verdict(passed, [yes, no], error) {
    return passed ? badge(yes, "ok") : h("span", {}, badge(no, "danger"), " ", error ?? "");
}

function sideCard(title, entry) {
    return h("div", { class: "mode" }, h("h4", { class: "mode-title" }, title),
        entry ? [keyValues([["快照", code(entry.snapshot)], ["导入时间", formatInstant(entry.imported_at)],
            ["校验", verdict(entry.valid, VALIDITY, entry.error)]]),
        tableSummaries(entry.tables)] : h("p", { class: "muted" }, "无"));
}

function snapshotFlags(entry) {
    return [entry.in_use ? badge("运行中", "ok") : null, entry.disk ? badge("磁盘指针", "info") : null,
        entry.next_start ? badge("下次启动", "warn") : null].filter(Boolean).flatMap((flag) => [" ", flag]);
}

function renderContent(view, { choose, go }) {
    return h("div", {},
        h("div", { class: "modes" }, sideCard("运行中的主表", view.side_by_side?.in_use),
            sideCard("磁盘指针指向的主表", view.side_by_side?.disk)),
        renderMasterState(view.masterdata),
        table([
            { label: "快照", cell: (entry) => h("span", {}, code(entry.snapshot), snapshotFlags(entry)) },
            { label: "导入时间", cell: (entry) => formatInstant(entry.imported_at) },
            { label: "校验", cell: (entry) => verdict(entry.valid, VALIDITY, entry.error) },
            { label: "表摘要", cell: (entry) => renderValue(entry.tables, { summary: "表摘要" }) },
            { label: "", cell: (entry) => button("选作校验目标", {
                action: "choose-master", disabled: !entry.valid, onClick: () => choose(entry.snapshot),
            }) },
        ], view.snapshots.items, { empty: "数据目录里还没有主表快照。" }),
        pager(view.snapshots, go));
}

function renderCaptures(listing, { importCapture, go }) {
    return h("div", {},
        keyValues([["采集目录", code(listing.root)]]),
        table([
            { label: "名称", cell: (entry) => code(entry.name) },
            { label: "可导入", cell: (entry) => verdict(entry.importable, IMPORTABILITY, entry.error) },
            { label: "快照", cell: (entry) => (entry.snapshot ? code(entry.snapshot) : "—") },
            { label: "已导入", cell: (entry) => (entry.imported ? badge("已导入", "info") : "否") },
            { label: "采集时间", cell: (entry) => `${formatInstant(entry.started_utc)} → ${formatInstant(entry.finished_utc)}` },
            { label: "表摘要", cell: (entry) => renderValue(entry.tables, { summary: "表摘要" }) },
            { label: "", cell: (entry) => importButton(entry, importCapture) },
        ], listing.items, { empty: "采集目录里没有可识别的采集。" }),
        pager(listing, go));
}

function importButton(entry, importCapture) {
    const trigger = button("导入为快照", {
        action: "import-capture", disabled: !entry.importable, onClick: () => importCapture(entry.name, trigger),
    });
    return trigger;
}

function renderImport(result) {
    return h("div", {},
        notice("ok", "已导入主表快照 ", code(result.snapshot), result.already_present ? "（快照已存在，没有重复写入）。" : "。"),
        keyValues([
            ["采集", code(result.capture)],
            ["导入时间", formatInstant(result.imported_at)],
            ["已激活", result.activated ? badge("是", "warn") : "否（只有校验后切换才会改变主表）"],
        ]),
        tableSummaries(result.tables));
}

function contentPanel({ signal, choose }) {
    const offsets = { snapshots: 0, captures: 0 };
    const snapshots = h("div");
    const captures = h("div");
    const imported = h("div", { "aria-live": "polite" });
    const newestSnapshots = newestRead();
    const newestCaptures = newestRead();
    const showSnapshots = () => present(snapshots,
        newestSnapshots(() => api.get(ROUTES.content, { query: { offset: offsets.snapshots, limit: PAGE_LIMIT }, signal })),
        (view) => renderContent(view, { choose, go: (offset) => { offsets.snapshots = offset; showSnapshots(); } }));
    const showCaptures = () => present(captures,
        newestCaptures(() => api.get(ROUTES.contentCaptures, { query: { offset: offsets.captures, limit: PAGE_LIMIT }, signal })),
        (listing) => renderCaptures(listing, { importCapture, go: (offset) => { offsets.captures = offset; showCaptures(); } }));

    async function importCapture(name, trigger) {
        trigger.disabled = true;
        imported.replaceChildren(loading("正在导入…"));
        try {
            const result = await api.post(ROUTES.contentImports, { capture: name }, { signal });
            imported.replaceChildren(renderImport(result));
            choose(result.snapshot);
            showSnapshots();
            showCaptures();
        } catch (error) {
            if (error?.name === "AbortError") return;
            imported.replaceChildren(renderError(error));
            trigger.disabled = false;
        }
    }

    const details = h("details", { class: "value-tree", dataset: { panel: "content" } },
        h("summary", {}, "展开主表快照与采集目录"),
        notice("info", MASTER_NOTE),
        h("h3", { class: "subheading" }, "主表快照"),
        toolbar(button("刷新", { action: "refresh-content", onClick: () => { showSnapshots(); showCaptures(); } })),
        snapshots,
        h("h3", { class: "subheading" }, "采集目录"),
        notice("info", IMPORT_NOTE),
        captures, imported);
    let loaded = false;
    details.addEventListener("toggle", () => {
        if (!details.open || loaded) return;
        loaded = true;
        showSnapshots();
        showCaptures();
    });
    return Object.freeze({
        element: details,
        refresh: () => {
            if (loaded) showSnapshots();
        },
    });
}
