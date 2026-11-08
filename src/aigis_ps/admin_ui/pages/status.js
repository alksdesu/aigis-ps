import { api, ApiError, ROUTES, watch } from "../api.js";
import {
    badge, buildBadge, button, code, digest, formatInstant, formatSeconds, h, keyValues, list, notice, present, renderError,
    renderValue, section, table, toolbar,
} from "../diff-view.js";
import { newestRead } from "../view-kit.js";

const REFRESH_MS = 5000;
const REQUEST_REFRESH_MS = 10000;
const REQUEST_LIMIT = 50;
const VIEW_MISSING = "admin_view_unknown";
const READINESS_VIEW = "runtime.readiness";
const VERSIONS_VIEW = "runtime.versions";
const OPERATIONS_VIEW = "runtime.operations";
const AVAILABILITY_FIELDS = ["ready", "available", "startup_ready"];
const ERROR_STATUS = 400;
const READINESS_NOTE = "就绪结果在服务端构建世代时计算，启动诊断实时合并；刷新只重新读取，不会重算。展开一个域即可读取它的完整状态。";

export default Object.freeze({ title: "运行状态", order: 10, mount });

function mount(root, { signal }) {
    const session = h("div");
    const gate = h("div");
    const runtime = h("div");
    const readiness = h("div");
    const versions = h("div");
    const operations = h("div");
    const requests = h("div");
    const showVersions = () => optionalView(versions, VERSIONS_VIEW, renderVersions, signal);
    const showOperations = () => optionalView(operations, OPERATIONS_VIEW, renderOperations, signal);
    root.append(section("会话", session), section("客户端闸门", gate), section("世代运行时", runtime),
        section("就绪状态", readiness),
        section("版本对照", toolbar(button("刷新", { action: "refresh-versions", onClick: showVersions })), versions),
        section("未实现的接口", operations), section("最近的游戏请求", requests));
    watch(() => api.read(ROUTES.session, { signal }), {
        interval: REFRESH_MS, signal,
        onData: (data) => {
            session.replaceChildren(renderSession(data));
            gate.replaceChildren(renderGate(data.client));
        },
        onError: (error) => session.replaceChildren(renderError(error)),
    });
    let generation = null;
    watch(() => api.read(ROUTES.runtime, { signal }), {
        interval: REFRESH_MS, signal,
        onData: (data) => {
            runtime.replaceChildren(renderRuntime(data));
            const current = data.runtime.current.id;
            // Re-rendering would collapse the domains an operator has opened, so only a new generation reloads them.
            if (current === generation) return;
            const switched = generation !== null;
            generation = current;
            readiness.replaceChildren(renderReadiness(data.readiness_domains, signal));
            if (switched) {
                showVersions();
                showOperations();
            }
        },
        onError: (error) => runtime.replaceChildren(renderError(error)),
    });
    watch(() => api.read(ROUTES.requests, { query: { limit: REQUEST_LIMIT }, signal }), {
        interval: REQUEST_REFRESH_MS, signal, onData: (data) => requests.replaceChildren(renderRequests(data.requests)),
        onError: (error) => requests.replaceChildren(renderError(error)),
    });
    showVersions();
    showOperations();
}

function flag(value, yes, no, tone = "warn") {
    return value ? badge(yes, tone) : badge(no, "ok");
}

function renderSession(data) {
    return keyValues([
        ["管理身份", data.actor],
        ["控制台地址", code(data.admin?.console ?? "—")],
        ["游戏端口", data.admin?.game_port],
        ["服务端时间", `${formatInstant(data.server_now?.jst)}（UTC ${data.server_now?.utc ?? "—"}）`],
        ["玩家", data.player_id],
        ["存档修订号", data.revision],
        ["世代", digest(data.generation_id)],
        ["预检", flag(data.checks?.preflight_noop, "空实现：只适合开发", "已接入")],
        ["重启兼容检查", flag(data.checks?.restart_check_noop, "空实现：只适合开发", "已接入")],
    ]);
}

function renderGate(client) {
    if (!client) return h("p", { class: "muted" }, "没有闸门状态。");
    return h("div", {},
        keyValues([
            ["静默阈值", formatSeconds(client.quiet_seconds)],
            ["距上次游戏请求", client.idle_seconds === null ? "启动以来没有游戏请求" : formatSeconds(client.idle_seconds)],
            ["客户端已静默", client.quiet ? badge("是", "ok") : badge("否", "warn")],
            ["换代后的新会话", client.session_observed_since_switch === null ? "启动以来未换代"
                : client.session_observed_since_switch ? badge("已观察到", "ok") : badge("尚未观察到", "warn")],
        ]),
        client.restart_required
            ? notice("warn", "需要重启游戏客户端：", h("ul", {}, client.notices.map((item) =>
                h("li", {}, `${item.reason}（${formatInstant(item.created_at)}）`))))
            : null);
}

function renderRuntime(data) {
    const { runtime, identity } = data;
    const versions = identity.content_versions ?? {};
    return h("div", {},
        keyValues([
            ["当前世代", digest(runtime.current.id)],
            ["在途租约", runtime.current.leases],
            ["排空中的旧世代", runtime.draining
                ? `${runtime.draining.id}（${runtime.draining.leases} 个租约，已排空 ${formatSeconds(runtime.draining.seconds)}）` : "无"],
            ["切换栅栏", runtime.fenced ? badge("已关闭：新请求排队", "warn") : "开放"],
            ["候选世代", runtime.candidate
                ? `${runtime.candidate.id}（${formatSeconds(runtime.candidate.expires_in_seconds)}后过期）` : "无"],
            ["构建作业", runtime.build
                ? h("span", {}, buildBadge(runtime.build.state), ` ${formatSeconds(runtime.build.elapsed_seconds)}`) : "无"],
            ["上次换代", runtime.switched_seconds_ago === null ? "启动以来未换代"
                : `${formatSeconds(runtime.switched_seconds_ago)}前`],
        ]),
        h("h3", { class: "subheading" }, "世代身份"),
        keyValues([
            ["代码摘要", digest(identity.code_digest)],
            ["配置摘要", digest(identity.config_set)],
            ["主表快照", identity.master_snapshot ?? "—"],
            ["客户端指纹", identity.client_fingerprint ? digest(identity.client_fingerprint) : "—"],
            ["基础资源版本", identity.base_resource_version ?? "—"],
            ["有效资源版本", identity.effective_resource_version ?? "—"],
            ["内容版本", renderValue(versions, { summary: `${Object.keys(versions).length} 个域` })],
        ]),
        data.gate_issues?.length
            ? notice("danger", "启动闸门问题：", h("ul", {}, data.gate_issues.map((issue) => h("li", {}, issue)))) : null);
}

function availability(status) {
    if (typeof status === "boolean") return status;
    if (status === null || typeof status !== "object") return null;
    const field = AVAILABILITY_FIELDS.find((name) => typeof status[name] === "boolean");
    return field === undefined ? null : status[field];
}

function availabilityBadge(status) {
    const ready = availability(status);
    return ready === true ? badge("就绪", "ok") : ready === false ? badge("不可用", "danger") : badge("未知");
}

function readinessDomain(domain, signal) {
    const state = h("span", { class: "readiness-state" }, badge("…"));
    const body = h("div", { class: "readiness-body" });
    const element = h("details", { class: "readiness-domain", dataset: { domain } },
        h("summary", {}, code(domain), " ", state), body);
    const newest = newestRead();
    let loaded = false;
    const load = () => present(body, newest(() => api.get(ROUTES.readiness, { values: { domain }, signal })),
        (data) => renderValue(data.readiness, { open: true, summary: "完整状态（已脱敏）" }));
    element.addEventListener("toggle", () => {
        if (!element.open || loaded) return;
        loaded = true;
        load();
    });
    return Object.freeze({
        domain, state, element,
        reload: () => {
            if (loaded) load();
        },
    });
}

function renderReadiness(domains, signal) {
    if (!domains?.length) return h("p", { class: "muted" }, "这个世代没有报告就绪状态。");
    const entries = domains.map((domain) => readinessDomain(domain, signal));
    const states = new Map(entries.map((entry) => [entry.domain, entry.state]));
    const newest = newestRead();
    const showStates = () => {
        states.forEach((state) => state.replaceChildren(badge("…")));
        newest(async () => (await api.view(READINESS_VIEW, undefined, { signal })).data)().then((envelope) => {
            Object.entries(envelope.data.domains).forEach(([domain, status]) =>
                states.get(domain)?.replaceChildren(availabilityBadge(status)));
        }, (error) => {
            if (error?.name !== "AbortError") states.forEach((state) => state.replaceChildren(badge("未知")));
        });
    };
    const refresh = () => {
        showStates();
        entries.forEach((entry) => entry.reload());
    };
    showStates();
    return h("div", { class: "readiness" }, h("p", { class: "muted" }, READINESS_NOTE),
        toolbar(button("刷新", { action: "refresh-readiness", onClick: refresh })), entries.map((entry) => entry.element));
}

function renderRequests(requests) {
    return table([
        { label: "时间", cell: (row) => formatInstant(row.created_at) },
        { label: "接口", cell: (row) => code(row.route) },
        { label: "状态", cell: (row) => badge(String(row.status), row.status < ERROR_STATUS ? "ok" : "danger") },
        { label: "错误码", cell: (row) => row.error_code ?? "—" },
        { label: "请求字节", cell: (row) => row.wire_bytes },
        { label: "响应字节", cell: (row) => row.response_bytes },
    ], requests, { empty: "还没有游戏请求记录。" });
}

function optionalView(container, name, render, signal) {
    present(container, async () => {
        try {
            return (await api.view(name, undefined, { signal })).data;
        } catch (error) {
            if (error instanceof ApiError && error.code === VIEW_MISSING) return null;
            throw error;
        }
    }, (view) => (view === null ? h("p", { class: "muted" }, "服务端没有提供这个视图。") : render(view.data)));
}

function matchBadge(matches) {
    return matches === true ? badge("一致", "ok") : matches === false ? badge("不一致", "danger") : badge("未知");
}

function renderVersions(data) {
    const manifest = data.client_manifest ?? {};
    const master = data.masterdata ?? {};
    const prepared = !manifest.present
        ? h("p", { class: "muted" }, "尚未准备客户端：", code(manifest.path ?? "—"))
        : !manifest.valid
            ? notice("danger", "客户端清单无法解析：", code(manifest.path ?? "—"))
            : keyValues([
                ["清单", code(manifest.path)],
                ["准备时间", formatInstant(manifest.prepared_at)],
                ["客户端", h("span", {}, digest(manifest.client?.prepared), " ↔ 世代 ", digest(manifest.client?.generation),
                    " ", matchBadge(manifest.client?.matches))],
                ["资源版本", h("span", {}, String(manifest.resources?.prepared ?? "—"), " ↔ 提供 ",
                    String(manifest.resources?.served ?? "—"), " ", matchBadge(manifest.resources?.matches))],
                ["覆盖层", renderValue(manifest.overlays)],
            ]);
    return h("div", {},
        h("h3", { class: "subheading" }, "客户端清单"), prepared,
        h("h3", { class: "subheading" }, "主表"),
        keyValues([["在用快照", master.in_use ?? "—"], ["磁盘状态", renderValue(master.disk)],
            ["一致", matchBadge(master.matches)]]));
}

function renderOperations(data) {
    return keyValues([
        ["已登记的协议接口", data.registered_operations],
        ["已配置的处理器", Array.isArray(data.configured_handlers) ? data.configured_handlers.length : undefined],
        ["未实现", list(data.unimplemented?.map(String))],
    ]);
}
