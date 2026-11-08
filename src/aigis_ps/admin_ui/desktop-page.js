import { DESKTOP_ROUTES } from "./api.js";
import { badge, button, code, fill, h, keyValues, loading, notice, renderError, section, table, toolbar } from "./diff-view.js";

const SERVICE_STATES = Object.freeze({ stopped: "已停止", starting: "正在启动", running: "运行中",
    stopping: "正在停止", failed: "启动失败" });
const GAME_STATES = Object.freeze({ stopped: "未启动", starting: "正在启动", running: "运行中", failed: "启动失败" });
const CHANGE_HINT = "新建或切换存档前，请先在游戏中退出，再停止服务。每份存档使用独立的数据目录。";

export default Object.freeze({ title: "启动与存档", order: 0, mount });

function mount(root, { signal, desktop }) {
    if (!desktop) {
        root.append(notice("info", "此页面需要从桌面应用打开。"));
        return;
    }
    const errors = h("div", { "aria-live": "polite" });
    const onboarding = h("div");
    const status = h("div");
    const profiles = h("div");
    const start = button("启动服务", { tone: "primary", action: "start-service",
        onClick: () => desktop.execute(DESKTOP_ROUTES.startService, {}) });
    const stop = button("停止服务", { action: "stop-service",
        onClick: () => desktop.execute(DESKTOP_ROUTES.stopService, {}) });
    const game = button("启动游戏", { tone: "primary", action: "start-game",
        onClick: () => desktop.execute(DESKTOP_ROUTES.startGame, {}) });
    const name = h("input", { class: "input", name: "profile_name", type: "text", required: true,
        autocomplete: "off", placeholder: "输入新存档名称", "aria-label": "新存档名称" });
    const create = button("新建独立存档", { type: "submit", action: "create-profile" });
    const form = h("form", { class: "dialog-form" },
        h("label", { class: "field" }, h("span", { class: "field-label" }, "新存档名称"), name), toolbar(create));
    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (create.disabled) return;
        if (await desktop.execute(DESKTOP_ROUTES.createProfile, { name: name.value.trim() })) name.value = "";
        if (!signal.aborted) render();
    });
    name.addEventListener("input", () => render());
    root.append(errors, onboarding, section("本地游戏", status, toolbar(start, stop, game),
        h("p", { class: "muted" }, "先启动服务，再启动游戏。停止服务前请退出游戏。")),
        section("存档", notice("info", CHANGE_HINT), profiles), section("新建存档", form));

    function render() {
        if (signal.aborted) return;
        const { state, error, pending } = desktop.snapshot();
        fill(errors, error ? renderError(error) : null,
            pending ? notice("info", "正在处理，请稍候…") : null);
        if (!state) {
            status.replaceChildren(loading("正在读取桌面状态…"));
            [start, stop, game, create, name].forEach((control) => { control.disabled = true; });
            return;
        }
        const profile = state.selected_profile;
        fill(onboarding, !profile ? notice("info", "创建第一份存档：在下方输入存档名称并创建，然后启动服务和游戏。新存档从空白进度开始。") : null);
        const service = state.service.state;
        const gameState = state.game.state ?? (state.game.running ? "running" : "stopped");
        const busy = pending || service === "starting" || service === "stopping" || gameState === "starting";
        const gameActive = state.game.running || gameState === "starting";
        const canChange = !busy && !gameActive && (service === "stopped" || service === "failed");
        start.disabled = !profile || busy || gameActive || !["stopped", "failed"].includes(service);
        stop.textContent = service === "starting" ? "取消启动" : "停止服务";
        stop.disabled = pending || gameActive || !["starting", "running"].includes(service);
        game.disabled = !profile || busy || state.game.running || service !== "running";
        name.disabled = !canChange;
        create.textContent = profile ? "新建独立存档" : "创建第一份存档";
        create.disabled = !canChange || !name.value.trim();
        fill(status, keyValues([
            ["当前存档", profile?.name ?? "尚未创建"],
            ["服务", badge(SERVICE_STATES[service] ?? service, service === "running" ? "ok" : "muted")],
            ["游戏", badge(GAME_STATES[gameState] ?? gameState, state.game.running ? "ok" : "muted")],
            ["存档目录", profile ? code(profile.data_dir) : "创建后生成"],
            ["日志文件", state.log_path ? code(state.log_path) : "尚未生成"],
        ]), state.service.error ? renderError(state.service.error) : null,
        state.game.error ? renderError(state.game.error) : null);
        profiles.replaceChildren(table([
            { label: "名称", cell: (profile) => profile.name },
            { label: "数据目录", cell: (profile) => code(profile.data_dir) },
            { label: "选择", cell: (profile) => profile.selected ? badge("当前存档", "ok")
                : button(canChange ? "切换到此存档" : "关闭游戏并停服后切换", {
                    action: "select-profile", disabled: !canChange,
                    onClick: () => desktop.execute(DESKTOP_ROUTES.selectProfile, { profile_id: profile.id }),
                }) },
        ], state.profiles));
    }

    render();
    return desktop.subscribe(render);
}
