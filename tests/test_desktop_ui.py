import json
import shutil
import subprocess

import pytest

from aigis_ps.admin_app import UI_DIRECTORY
from test_admin_ui_assets import FAKE_DOM, NODE_TIMEOUT


HARNESS = FAKE_DOM + r'''
const { api, DESKTOP_ROUTES, ROUTES, setServiceContext } = await load("api.js");
const { default: launcher } = await load("desktop-page.js");

setServiceContext("a-stopped", false, "a");
const beforeBlocked = requests.length;
try { await api.post(ROUTES.plans, { commands: [] }); }
catch (error) { results.blocked = { code: error.code, requests: requests.length - beforeBlocked }; }

setServiceContext("a-running", true, "a");
let oldSignal;
let release;
queue.push((url, init) => { oldSignal = init.signal; return new Promise((resolve) => { release = resolve; }); });
const oldRead = api.get(ROUTES.commands).catch((error) => error.name);
const oldHeaders = requests.at(-1).headers;
setServiceContext("b-running", true, "b");
release(new Response(JSON.stringify({ commands: ["old"] }), { status: 200 }));
results.cancelled = { aborted: oldSignal.aborted, result: await oldRead, headers: oldHeaders };
queue.push(reply(200, { commands: [] }), reply(200, {}));
await api.get(ROUTES.commands);
const newHeaders = requests.at(-1).headers;
await api.get(DESKTOP_ROUTES.status);
results.profileHeaders = { current: newHeaders, desktop: requests.at(-1).headers };

function state(service = "stopped", game = "stopped", selected = "a") {
    const profiles = (selected === null ? [] : ["a", "b"]).map((id) => (
        { id, name: `存档 ${id}`, data_dir: `data/${id}`, selected: id === selected }));
    return { service: { state: service, pid: service === "running" ? 25 : null, error: null },
        game: { state: game, running: game === "running", pid: null, error: null }, profiles,
        selected_profile: profiles.find((profile) => profile.selected) ?? null, log_path: null };
}
let snapshot = { state: state(), error: null, pending: false };
const listeners = new Set();
const calls = [];
const desktop = {
    snapshot: () => snapshot,
    subscribe: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
    execute: async (route, body) => {
        calls.push({ route, body });
        if (route === DESKTOP_ROUTES.createProfile && snapshot.state.selected_profile === null) {
            const created = state();
            created.profiles = [created.selected_profile];
            created.selected_profile.name = body.name;
            update({ state: created, error: null, pending: false });
        }
        return true;
    },
};
const controller = new AbortController();
const panel = new FakeElement("div");
const cleanup = launcher.mount(panel, { signal: controller.signal, desktop });
const controls = () => Object.fromEntries(["start-service", "stop-service", "start-game", "create-profile", "select-profile"]
    .map((name) => [name, action(panel, name)?.disabled ?? null]));
const update = (next) => { snapshot = next; listeners.forEach((listener) => listener(snapshot)); };
const input = find(panel, (element) => element.name === "profile_name");
input.value = " 新档 ";
input.dispatchEvent(new FakeEvent("input"));
results.stopped = { controls: controls(), text: panel.textContent };
action(panel, "select-profile").click();
find(panel, (element) => element.localName === "form").dispatchEvent(new FakeEvent("submit"));
await settle();
results.profileActions = calls.splice(0);
results.clearedName = input.value;
update({ state: state("starting"), error: null, pending: false });
results.starting = controls();
results.cancelLabel = action(panel, "stop-service").textContent;
action(panel, "stop-service").click();
results.cancelAction = calls.splice(0);
update({ state: state("starting"), error: null, pending: true });
results.cancelPending = controls();
action(panel, "stop-service").click();
update({ state: state("starting", "starting"), error: null, pending: false });
results.startingWithGame = controls();
action(panel, "stop-service").click();
update({ state: state("stopping"), error: null, pending: false });
results.stopping = controls();
action(panel, "stop-service").click();
results.blockedCancelActions = calls.splice(0);
update({ state: state("running"), error: null, pending: false });
results.running = controls();
results.stopLabel = action(panel, "stop-service").textContent;
action(panel, "stop-service").click();
results.stopAction = calls.splice(0);
action(panel, "start-game").click();
results.gameAction = calls.splice(0);
update({ state: state("running", "starting"), error: null, pending: false });
results.gameStarting = controls();
update({ state: state("running", "running"), error: null, pending: false });
results.gameRunning = controls();
update({ state: state(), error: null, pending: true });
results.pending = controls();
update({ state: state("stopped", "stopped", null), error: null, pending: false });
input.value = " 我的第一份存档 ";
input.dispatchEvent(new FakeEvent("input"));
results.emptyLauncher = { controls: controls(), text: panel.textContent };
action(panel, "start-service").click();
action(panel, "start-game").click();
results.emptyStartCalls = calls.splice(0);
find(panel, (element) => element.localName === "form").dispatchEvent(new FakeEvent("submit"));
await settle();
results.firstCreated = { controls: controls(), text: panel.textContent, name: input.value,
    selected: snapshot.state.selected_profile, calls: calls.splice(0) };
update({ state: state("running", "stopped", null), error: null, pending: false });
results.emptyRunning = controls();
controller.abort();
cleanup();
results.unsubscribed = listeners.size;

const shell = Object.fromEntries(["session", "nav", "alerts", "page"].map((id) => [id, new FakeElement("div")]));
document.getElementById = (id) => shell[id] ?? null;
document.querySelectorAll = () => all(document.body, (element) => element.localName === "dialog" && element.open);
const windowEvents = new FakeNode();
globalThis.window = { addEventListener: (...args) => windowEvents.addEventListener(...args) };
location.replace = (url) => {
    location.hash = url;
    windowEvents.dispatchEvent(new FakeEvent("hashchange"));
};
location.hash = "";
globalThis.testMounts = [];
let current = state("stopped", "stopped", null);
let commandRequests = 0;
let finishOldCommands;
const shellRequests = [];
const sessionProfiles = [];
globalThis.fetch = async (url, init) => {
    shellRequests.push({ url, method: init.method, headers: init.headers });
    if (url.endsWith("/session")) {
        sessionProfiles.push(current.selected_profile?.id ?? null);
        return reply(200, { desktop: true, desktop_profile_id: current.selected_profile?.id ?? null,
        service_state: current.service.state, player_id: current.service.state === "running" ? "1" : null,
        revision: current.service.state === "running" ? 3 : null, generation_id: null, checks: {}, server_now: {}, client: {} })();
    }
    if (url.endsWith("/ui-pages")) return reply(200, { pages: [
        { name: "status", module: "/admin/static/pages/status.js" },
        { name: "launcher", module: "/admin/static/pages/launcher.js" },
    ] })();
    if (url.endsWith("/desktop")) return reply(200, current)();
    if (url.endsWith("/desktop/profiles")) {
        current = state();
        current.profiles = [current.selected_profile];
        current.selected_profile.name = JSON.parse(init.body).name;
        return reply(200, current)();
    }
    if (url.endsWith("/desktop/service/start")) {
        current.service = { state: "running", pid: 25, error: null };
        return reply(200, current)();
    }
    if (url.endsWith("/commands")) {
        commandRequests += 1;
        if (commandRequests === 1) return new Promise((resolve) => { finishOldCommands = resolve; });
        return reply(200, { profile: current.selected_profile.id })();
    }
    throw new Error(`Unexpected request ${url}`);
};
await load("app.js");
await until(() => globalThis.testDesktop !== undefined);
results.shellInitial = { route: location.hash, session: shell.session.textContent };
location.replace("#/status");
await settle();
results.shellStopped = { text: shell.page.textContent, mounts: testMounts.length, commandRequests };
current = state("running", "stopped", null);
testDesktop.refresh();
await until(() => testDesktop.snapshot().state.service.state === "running");
results.shellEmptyRunning = { text: shell.page.textContent, session: shell.session.textContent,
    mounts: testMounts.length, commandRequests };
current = state("stopped", "stopped", null);
testDesktop.refresh();
await until(() => testDesktop.snapshot().state.service.state === "stopped");
location.replace("#/launcher");
await testDesktop.execute(DESKTOP_ROUTES.createProfile, { name: "我的存档" });
await until(() => sessionProfiles.at(-1) === "a");
results.shellFirstCreated = { session: shell.session.textContent, mounts: testMounts.length,
    commandRequests, state: testDesktop.snapshot().state, posts: shellRequests.filter((request) => request.method === "POST") };
await testDesktop.execute(DESKTOP_ROUTES.startService, {});
await until(() => shell.session.textContent.includes("运行中"));
location.replace("#/status");
await until(() => testMounts.length === 1);
results.firstRequestIdentity = shellRequests.find((request) => request.url.endsWith("/commands")).headers;
const oldMount = testMounts[0];
const dialog = new FakeElement("dialog");
document.body.append(dialog);
dialog.showModal();
current = state("running", "stopped", "b");
testDesktop.refresh();
await until(() => testMounts.length === 2);
finishOldCommands(new Response(JSON.stringify({ profile: "a" }), { status: 200 }));
await settle();
results.shellSwitched = { aborted: oldMount.signal.aborted, cleaned: oldMount.cleaned,
    mounts: testMounts.length, dialogOpen: dialog.open, oldResult: oldMount.result,
    newResult: testMounts[1].result, text: shell.page.textContent };
current = state();
testDesktop.refresh();
await until(() => testMounts[1].signal.aborted);
results.shellRestopped = { text: shell.page.textContent, mounts: testMounts.length, commandRequests };
console.log(JSON.stringify(results));
process.exit(0);
'''


@pytest.fixture(scope="module")
def desktop_browser(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not available to run the console modules")
    workspace = tmp_path_factory.mktemp("desktop-ui")
    ui = workspace / "ui"
    shutil.copytree(UI_DIRECTORY, ui, ignore=shutil.ignore_patterns("__pycache__"))
    (ui / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    app = ui / "app.js"
    app.write_text(app.read_text(encoding="utf-8").replace(
        "import(entry.module)", 'import("." + entry.module.slice("/admin/static".length))'), encoding="utf-8")
    (ui / "pages" / "launcher.js").write_text(
        'export default { title: "启动与存档", order: 0, mount(root, context) { '
        'globalThis.testDesktop = context.desktop; } };', encoding="utf-8")
    (ui / "pages" / "status.js").write_text('''
import { api, ROUTES } from "../api.js";
export default { title: "运行状态", order: 10, mount(root, { signal }) {
    const mount = { signal, cleaned: false, result: null };
    globalThis.testMounts.push(mount);
    api.get(ROUTES.commands, { signal }).then((data) => {
        mount.result = data.profile;
        root.textContent = data.profile;
    }).catch((error) => { mount.result = error.name; });
    return () => { mount.cleaned = true; };
} };
''', encoding="utf-8")
    harness = workspace / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    completed = subprocess.run([node, str(harness), str(ui)], capture_output=True, timeout=NODE_TIMEOUT, cwd=workspace)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    return json.loads(completed.stdout.decode("utf-8").strip().splitlines()[-1])


def test_stopped_service_blocks_save_requests_and_scopes_running_requests(desktop_browser):
    assert desktop_browser["blocked"] == {"code": "desktop_service_unavailable", "requests": 0}
    cancelled = desktop_browser["cancelled"]
    assert cancelled["aborted"] and cancelled["result"] == "AbortError"
    assert cancelled["headers"]["X-Aigis-Profile"] == "a"
    assert desktop_browser["profileHeaders"]["current"]["X-Aigis-Profile"] == "b"
    assert "X-Aigis-Profile" not in desktop_browser["profileHeaders"]["desktop"]


def test_launcher_enables_only_actions_valid_for_service_and_game_state(desktop_browser):
    assert desktop_browser["stopped"]["controls"] == {
        "start-service": False, "stop-service": True, "start-game": True, "create-profile": False, "select-profile": False}
    assert "新建或切换存档前" in desktop_browser["stopped"]["text"]
    assert "尚未生成" in desktop_browser["stopped"]["text"] and "null" not in desktop_browser["stopped"]["text"]
    assert desktop_browser["starting"] == {
        "start-service": True, "stop-service": False, "start-game": True, "create-profile": True, "select-profile": True}
    assert desktop_browser["cancelLabel"] == "取消启动"
    assert desktop_browser["stopLabel"] == "停止服务"
    for state in ("cancelPending", "startingWithGame", "stopping", "gameStarting", "gameRunning", "pending"):
        assert all(desktop_browser[state].values()), state
    assert desktop_browser["running"] == {
        "start-service": True, "stop-service": False, "start-game": False, "create-profile": True, "select-profile": True}
    assert desktop_browser["unsubscribed"] == 0


def test_launcher_creates_selects_profiles_and_starts_game(desktop_browser):
    assert desktop_browser["profileActions"] == [
        {"route": "/desktop/profiles/select", "body": {"profile_id": "b"}},
        {"route": "/desktop/profiles", "body": {"name": "新档"}},
    ]
    assert desktop_browser["clearedName"] == ""
    assert desktop_browser["gameAction"] == [{"route": "/desktop/game/start", "body": {}}]
    assert desktop_browser["cancelAction"] == [{"route": "/desktop/service/stop", "body": {}}]
    assert desktop_browser["stopAction"] == desktop_browser["cancelAction"]
    assert desktop_browser["blockedCancelActions"] == []


def test_desktop_shell_defaults_to_launcher_and_defers_save_pages_until_running(desktop_browser):
    assert desktop_browser["shellInitial"]["route"] == "#/launcher"
    assert "null" not in desktop_browser["shellInitial"]["session"]
    assert "尚未创建存档" in desktop_browser["shellInitial"]["session"]
    stopped = desktop_browser["shellStopped"]
    assert stopped["mounts"] == 0 and stopped["commandRequests"] == 0
    assert "创建第一份存档" in stopped["text"]


def test_first_profile_creation_enables_start_without_starting_any_process(desktop_browser):
    empty = desktop_browser["emptyLauncher"]
    assert empty["controls"] == {
        "start-service": True, "stop-service": True, "start-game": True, "create-profile": False, "select-profile": None}
    assert "创建第一份存档" in empty["text"] and "null" not in empty["text"]
    assert desktop_browser["emptyStartCalls"] == []
    created = desktop_browser["firstCreated"]
    assert created["calls"] == [{"route": "/desktop/profiles", "body": {"name": "我的第一份存档"}}]
    assert created["selected"]["selected"] and created["selected"]["name"] == "我的第一份存档"
    assert not created["controls"]["start-service"] and created["controls"]["start-game"]
    assert created["name"] == "" and "创建第一份存档" not in created["text"]
    shell = desktop_browser["shellFirstCreated"]
    assert shell["state"]["selected_profile"]["id"] == "a" and shell["state"]["service"]["state"] == "stopped"
    assert "我的存档" in shell["session"] and shell["mounts"] == shell["commandRequests"] == 0
    assert len(shell["posts"]) == 1 and shell["posts"][0]["url"].endswith("/desktop/profiles")
    assert "X-Aigis-Profile" not in shell["posts"][0]["headers"]
    assert desktop_browser["firstRequestIdentity"]["X-Aigis-Profile"] == "a"


def test_missing_profile_blocks_save_access_even_if_service_reports_running(desktop_browser):
    controls = desktop_browser["emptyRunning"]
    assert controls["start-service"] and controls["start-game"]
    shell = desktop_browser["shellEmptyRunning"]
    assert shell["mounts"] == shell["commandRequests"] == 0
    assert "创建第一份存档" in shell["text"] and "玩家" not in shell["session"]


def test_profile_switch_cancels_old_requests_closes_confirmation_and_remounts(desktop_browser):
    switched = desktop_browser["shellSwitched"]
    assert switched["aborted"] and switched["cleaned"] and not switched["dialogOpen"]
    assert switched["mounts"] == 2 and switched["oldResult"] == "AbortError"
    assert switched["newResult"] == "b" and switched["text"].endswith("b")
    stopped = desktop_browser["shellRestopped"]
    assert stopped["mounts"] == 2 and stopped["commandRequests"] == 2
    assert "先在启动页开启服务" in stopped["text"]


