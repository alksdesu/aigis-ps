import { api, DESKTOP_ROUTES, onUnauthorized, ROUTES, setServiceContext, STATIC_ROOT, TOKEN_STATE, tokenState, watch } from "./api.js";
import { badge, button, code, fill, formatInstant, h, notice, renderError, shortId } from "./diff-view.js";
import { routeHash } from "./view-kit.js";

const TITLE = "Aigis 本地管理控制台";
const PAGE_NAME = /^[a-z][a-z0-9-]*$/;
const PAGE_MODULES = `${STATIC_ROOT}/pages/`;
const PAGE_SUFFIX = ".js";
const DEFAULT_ORDER = 1000;
const SESSION_REFRESH_MS = 10000;
const DESKTOP_REFRESH_MS = 2000;
const OFFLINE_PAGES = new Set(["launcher"]);

const shell = Object.freeze({
    session: document.getElementById("session"),
    nav: document.getElementById("nav"),
    alerts: document.getElementById("alerts"),
    main: document.getElementById("page"),
});
const navLinks = new Map();
let latestSession = null;
let active = null;
let availablePages = null;
let latestDesktop = null;
let desktopError = null;
let desktopPending = false;
let desktopRevision = 0;
let sessionWatch = null;
let desktopWatch = null;
const desktopListeners = new Set();
const desktop = Object.freeze({
    snapshot: () => ({ state: latestDesktop, error: desktopError, pending: desktopPending }),
    subscribe(listener) {
        desktopListeners.add(listener);
        return () => desktopListeners.delete(listener);
    },
    refresh: () => desktopWatch?.refresh(),
    execute: desktopRequest,
});

start();

async function start() {
    const state = tokenState();
    if (state !== TOKEN_STATE.PRESENT) {
        shell.main.replaceChildren(tokenProblem(state));
        return;
    }
    onUnauthorized((error) => shell.alerts.replaceChildren(renderError(error)));
    let pages;
    try {
        renderSession(await api.get(ROUTES.session));
        if (latestSession.desktop === true) {
            setServiceContext("loading", false);
            watchDesktop();
        }
        watchSession();
        pages = await loadPages();
    } catch (error) {
        shell.main.replaceChildren(renderError(error));
        return;
    }
    availablePages = pages;
    renderNav(pages);
    window.addEventListener("hashchange", () => route(pages));
    route(pages);
}

function tokenProblem(state) {
    return h("section", { class: "card" },
        h("h1", { class: "page-title" }, state === TOKEN_STATE.MALFORMED ? "管理令牌格式无效" : "没有管理令牌"),
        h("p", {}, "请在项目目录运行 ", code(".\\open-console.ps1"), "，或打开服务启动时打印的带 ", code("#token="),
            " 的地址。"),
        h("p", { class: "muted" }, "令牌只保存在本页内存中，刷新页面后需要重新从脚本打开；每次启动服务都会更换令牌。"));
}

function watchSession() {
    const { signal } = new AbortController();
    let first = true;
    sessionWatch = watch(async () => {
        if (first) {
            first = false;
            return { notModified: true };
        }
        const revision = desktopRevision;
        const result = await api.read(ROUTES.session, { signal });
        return revision === desktopRevision ? result : { notModified: true };
    }, {
        interval: SESSION_REFRESH_MS, signal, onData: renderSession,
        onError: (error) => shell.session.replaceChildren(badge(`会话不可用：${error.code ?? error.message}`, "danger")),
    });
}

function renderSession(session) {
    latestSession = session;
    if (session.desktop === true) {
        const state = latestDesktop?.service.state ?? session.service_state;
        const profile = latestDesktop?.selected_profile;
        const running = state === "running" && session.service_state === "running" && desktopError === null
            && session.desktop_profile_id != null && (!latestDesktop || profile?.id === session.desktop_profile_id);
        fill(shell.session,
            badge(running ? "服务运行中" : "服务未就绪", running ? "ok" : "warn"),
            latestDesktop ? h("span", { class: "session-item" }, profile ? `存档 ${profile.name}` : "尚未创建存档") : null,
            running && session.player_id !== null ? h("span", { class: "session-item" }, `玩家 ${session.player_id}`) : null,
            running && session.revision !== null ? h("span", { class: "session-item" }, `修订号 ${session.revision}`) : null,
            running && session.client?.restart_required ? badge("需要重启客户端", "warn") : null);
        return;
    }
    fill(shell.session,
        h("span", { class: "session-item" }, `玩家 ${session.player_id}`),
        h("span", { class: "session-item" }, `修订号 ${session.revision}`),
        h("span", { class: "session-item", title: session.generation_id }, `世代 ${shortId(session.generation_id)}`),
        h("span", { class: "session-item" }, formatInstant(session.server_now?.jst)),
        session.checks?.preflight_noop ? badge("预检为空实现", "warn") : null,
        session.checks?.restart_check_noop ? badge("重启检查为空实现", "warn") : null,
        session.client?.restart_required ? badge("需要重启客户端", "warn") : null);
}

function notifyDesktop() {
    desktopListeners.forEach((listener) => listener(desktop.snapshot()));
}

function applyDesktop(state) {
    latestDesktop = state;
    desktopError = null;
    const profileId = state.selected_profile?.id ?? null;
    const identity = JSON.stringify([profileId, state.service.state, state.service.pid]);
    if (setServiceContext(identity, profileId !== null && state.service.state === "running", profileId)) {
        desktopRevision += 1;
        document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
        if (availablePages && !OFFLINE_PAGES.has(active?.name)) route(availablePages);
        sessionWatch?.refresh();
    }
    renderSession(latestSession);
    notifyDesktop();
}

function watchDesktop() {
    const { signal } = new AbortController();
    desktopWatch = watch(async () => {
        const revision = desktopRevision;
        const result = await api.read(DESKTOP_ROUTES.status, { signal });
        return revision === desktopRevision && !desktopPending ? result : { notModified: true };
    }, {
        interval: DESKTOP_REFRESH_MS, signal, onData: applyDesktop,
        onError(error) {
            desktopError = error;
            if (setServiceContext("unavailable", false)) {
                desktopRevision += 1;
                document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
                if (availablePages && !OFFLINE_PAGES.has(active?.name)) route(availablePages);
            }
            renderSession(latestSession);
            notifyDesktop();
        },
    });
}

async function desktopRequest(path, body) {
    if (desktopPending) return false;
    desktopPending = true;
    desktopRevision += 1;
    desktopError = null;
    notifyDesktop();
    try {
        applyDesktop(await api.post(path, body));
        return true;
    } catch (error) {
        desktopError = error;
        return false;
    } finally {
        desktopPending = false;
        notifyDesktop();
        desktopWatch?.refresh();
    }
}

async function loadPages() {
    const { pages } = await api.get(ROUTES.pages);
    // The server derives each module path from the page name, so anything else is refused before import().
    const entries = pages.filter((entry) => PAGE_NAME.test(entry.name)
        && entry.module === `${PAGE_MODULES}${entry.name}${PAGE_SUFFIX}`);
    const loaded = await Promise.all(entries.map(loadPage));
    return loaded.sort((left, right) => left.order - right.order || left.name.localeCompare(right.name));
}

async function loadPage(entry) {
    try {
        const { default: page } = await import(entry.module);
        if (page === null || typeof page !== "object" || typeof page.mount !== "function") {
            throw new TypeError(`${entry.module} does not export a console page`);
        }
        return {
            name: entry.name, title: typeof page.title === "string" ? page.title : entry.name,
            order: Number.isFinite(page.order) ? page.order : DEFAULT_ORDER, page,
        };
    } catch (error) {
        return { name: entry.name, title: entry.name, order: DEFAULT_ORDER, failure: error };
    }
}

function renderNav(pages) {
    navLinks.clear();
    shell.nav.replaceChildren(h("ul", { class: "nav-list" }, pages.map((page) => {
        const link = h("a", {
            class: ["nav-link", page.failure && "nav-link-failed"], href: `#/${page.name}`, dataset: { page: page.name },
        }, page.title);
        navLinks.set(page.name, link);
        return h("li", {}, link);
    })));
}

function parseRoute() {
    const text = location.hash.replace(/^#\/?/, "");
    const mark = text.indexOf("?");
    return {
        name: mark < 0 ? text : text.slice(0, mark),
        params: new URLSearchParams(mark < 0 ? "" : text.slice(mark + 1)),
    };
}

function navigate(name, params = {}) {
    location.hash = routeHash(name, params);
}

function unmount() {
    if (active === null) return;
    const { controller, cleanup } = active;
    active = null;
    controller.abort();
    try {
        cleanup?.();
    } catch (error) {
        console.error(error);
    }
}

function route(pages) {
    const { name, params } = parseRoute();
    const target = pages.find((page) => page.name === name);
    if (target === undefined) {
        const fallback = (latestSession?.desktop === true ? pages.find((page) => page.name === "launcher")
            : pages.find((page) => page.name === "status" && page.failure === undefined))
            ?? pages.find((page) => page.failure === undefined) ?? pages[0];
        if (fallback === undefined) shell.main.replaceChildren(notice("warn", "服务端没有提供任何控制台页面。"));
        else location.replace(`#/${fallback.name}`);
        return;
    }
    unmount();
    navLinks.forEach((link, page) => (page === target.name
        ? link.setAttribute("aria-current", "page") : link.removeAttribute("aria-current")));
    document.title = `${target.title} · ${TITLE}`;
    const root = h("div", { class: "page", dataset: { page: target.name } });
    shell.main.replaceChildren(h("h1", { class: "page-title" }, target.title), root);
    const controller = new AbortController();
    active = { name: target.name, controller, cleanup: null };
    if (latestSession?.desktop === true && !OFFLINE_PAGES.has(target.name)
        && (!latestDesktop?.selected_profile || latestDesktop.service.state !== "running" || desktopError !== null)) {
        root.append(notice("info", latestDesktop && !latestDesktop.selected_profile
            ? "请先在启动页创建第一份存档，再开启服务。" : "请先在启动页开启服务，再查看或修改当前存档。"),
            button("前往启动页", { onClick: () => navigate("launcher") }));
        return;
    }
    if (target.failure !== undefined) {
        root.append(renderError(target.failure));
        return;
    }
    const context = Object.freeze({ signal: controller.signal, params, navigate, session: () => latestSession,
        desktop: latestSession?.desktop === true ? desktop : null });
    try {
        const cleanup = target.page.mount(root, context);
        active.cleanup = typeof cleanup === "function" ? cleanup : null;
    } catch (error) {
        root.append(renderError(error));
    }
}
