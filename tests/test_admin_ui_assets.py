from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from aigis_ps.admin_app import PAGE_NAME, PAGE_SUFFIX, PAGES_DIRECTORY, UI_DIRECTORY, create_admin_app, ui_pages
from aigis_ps.admin_command import NoopPreflight, NoopRestartCheck, PlanStore
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS
from aigis_ps.admin_registry import discover
from aigis_ps.admin_routes_backup import ManualBackupRequest
from aigis_ps.admin_security import (ACCESS_SCHEMA, API_ROOT, CONTENT_SECURITY_POLICY, STATIC_ROOT, TOKEN_PATTERN, access_path,
                                     write_access_file)
from aigis_ps.generation import Generation, GenerationIdentity, GenerationRuntime
from aigis_ps.storage import MAX_BACKUP_REASON, Store

from test_admin_views_tools import TOOL_NAMES


PROJECT = Path(__file__).resolve().parents[1]
PACKAGE = UI_DIRECTORY.parent
SCRIPT = PROJECT / "open-console.ps1"
FIRST_PAGES = frozenset(("audit", "backups", "config", "state", "status"))
SCRIPT_SUFFIXES = (".js",)
PORT = 18104
GAME_PORT = 18103
TOKEN = "u" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
JAVASCRIPT_TYPES = ("text/javascript", "application/javascript")
NODE_TIMEOUT = 120
POWERSHELL_TIMEOUT = 60
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
EXTERNAL_LINK = re.compile(r"https?://", re.IGNORECASE)
FORBIDDEN_SCRIPT = {
    "HTML string sinks": re.compile(r"\b(?:innerHTML|outerHTML|insertAdjacentHTML)\b|document\.write"),
    "code evaluation": re.compile(r"\beval\s*\(|\bnew\s+Function\b"),
    "inline styles": re.compile(r"\.style\b|setAttribute\(\s*[\"']style"),
    "persistent token storage": re.compile(r"\b(?:localStorage|sessionStorage|indexedDB)\b|document\.cookie"),
}
STATIC_IMPORT = re.compile(r"^\s*(?:import|export)\s+(?:[^;]*?\s+from\s+)?\"([^\"]+)\";", re.MULTILINE | re.DOTALL)
NAMED_IMPORT = re.compile(r"import\s*\{([^}]*)\}\s*from\s*\"([^\"]+)\"")
EXPORTED_NAME = re.compile(r"^export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z_$][\w$]*)", re.MULTILINE)
ROUTE_TABLE = re.compile(r"export const ROUTES = Object\.freeze\(\{(.*?)\}\);", re.DOTALL)
ROUTE_ENTRY = re.compile(r"(\w+):\s*\"([^\"]+)\"")
ROUTE_USE = re.compile(r"\bROUTES\.(\w+)")
ROUTE_CALL = re.compile(r"\bapi\.(get|read|post)\(\s*ROUTES\.(\w+)")
HINT_TABLE = re.compile(r"const ERROR_HINTS = Object\.freeze\(\{(.*?)\}\);", re.DOTALL)
HINT_KEY = re.compile(r"^\s+(\w+):", re.MULTILINE)
REFUSAL_SET = re.compile(r"const [A-Z_]*REFUSALS = new Set\(\[(.*?)\]\);", re.DOTALL)
QUOTED_CODE = re.compile(r"\"([a-z_]+)\"")
VIEW_CALL = re.compile(r"api\.view\(\s*\"([^\"]+)\"")
VIEW_CONSTANT = re.compile(r"^const [A-Z_]*VIEW = \"([^\"]+)\";$", re.MULTILINE)
CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
CSS_VARIABLE_USE = re.compile(r"var\((--[a-z0-9-]+)\)")
CSS_NUMBER = re.compile(r"(?<![\w-])[-+]?(?:\d*\.)?\d+[a-z%]*", re.IGNORECASE)
CSS_COLOR = re.compile(r"#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color|color-mix)\(|\b(?:white|black|red|"
                       r"green|blue|gray|grey|silver|maroon|purple|fuchsia|lime|olive|yellow|navy|teal|aqua|orange)\b",
                       re.IGNORECASE)
CSS_ALLOWED_NUMBERS = frozenset(("0", "1"))
REASON_SAMPLES = ("manual backup", "中文原因", "emoji \U0001F600 ok", "two words", "全角　空格", "tab\there",
                  "zero​width", " nbsp", "soft­hyphen", "line separator", "privateuse")
DARK_SCHEME = "@media (prefers-color-scheme: dark)"
THEMED_PREFIXES = ("--color-", "--shadow-")


def ui_files():
    return sorted(path for path in UI_DIRECTORY.rglob("*") if path.is_file() and "__pycache__" not in path.parts)


def scripts():
    return [path for path in ui_files() if path.suffix in SCRIPT_SUFFIXES]


def relative(path):
    return path.relative_to(UI_DIRECTORY).as_posix()


def text_of(path):
    return path.read_text(encoding="utf-8")


def javascript_constant(path, name):
    found = re.search(rf"^(?:export\s+)?const {name} = (.+?);$", text_of(path), re.MULTILINE)
    assert found, f"{relative(path)} defines no {name}"
    return found.group(1)


def string_constant(path, name):
    return json.loads(javascript_constant(path, name))


def regex_body(literal):
    found = re.fullmatch(r"/\^(.*)\$/[a-z]*", literal)
    assert found, literal
    return found.group(1)


def parse_css(text, prelude=()):
    rules, position = [], 0
    while (opening := text.find("{", position)) >= 0:
        selector, depth, cursor = text[position:opening].strip(), 1, opening + 1
        while depth:
            next_open, next_close = text.find("{", cursor), text.find("}", cursor)
            assert next_close >= 0, "Unbalanced stylesheet"
            if 0 <= next_open < next_close:
                depth, cursor = depth + 1, next_open + 1
            else:
                depth, cursor = depth - 1, next_close + 1
        body = text[opening + 1:cursor - 1]
        rules.extend(parse_css(body, (*prelude, selector)) if "{" in body else [((*prelude, selector), body)])
        position = cursor
    return rules


def declarations(body):
    pairs = []
    for item in body.split(";"):
        if item.strip():
            name, separator, value = item.partition(":")
            assert separator, f"Malformed declaration {item!r}"
            pairs.append((name.strip(), value.strip()))
    return pairs


def stylesheet():
    text = CSS_COMMENT.sub("", text_of(UI_DIRECTORY / "style.css"))
    return text, parse_css(text)


def root_tokens(rules, prelude):
    return {name: value for selectors, body in rules if selectors == (*prelude, ":root") for name, value in declarations(body)
            if name.startswith("--")}


def test_the_first_pages_are_discovered_by_the_manifest():
    pages = ui_pages(UI_DIRECTORY)
    assert FIRST_PAGES <= {page["name"] for page in pages}
    for page in pages:
        assert PAGE_NAME.fullmatch(page["name"])
        assert page["module"] == f"{STATIC_ROOT}/{PAGES_DIRECTORY}/{page['name']}{PAGE_SUFFIX}"


@pytest.mark.parametrize("path", ui_files(), ids=relative)
def test_assets_have_no_external_links_or_important_rules(path):
    data = path.read_bytes()
    text = data.decode("utf-8")
    assert b"\r" not in data
    assert not EXTERNAL_LINK.search(text)
    assert "!important" not in text


@pytest.mark.parametrize("path", scripts(), ids=relative)
def test_scripts_stay_inside_the_content_security_policy(path):
    text = text_of(path)
    for label, pattern in FORBIDDEN_SCRIPT.items():
        assert not pattern.search(text), f"{relative(path)} uses {label}"
    if path.name != "api.js":
        assert "fetch(" not in text and API_ROOT not in text and "replaceState" not in text


class MarkupInspector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.inline_scripts, self.open_script = [], [], False

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        self.open_script = tag == "script"

    def handle_endtag(self, tag):
        if tag == "script":
            self.open_script = False

    def handle_data(self, data):
        if self.open_script and data.strip():
            self.inline_scripts.append(data)


def test_the_page_loads_only_its_module_and_stylesheet():
    inspector = MarkupInspector()
    inspector.feed(text_of(UI_DIRECTORY / "index.html"))
    assert not inspector.inline_scripts
    assert all(tag != "style" for tag, _ in inspector.tags)
    for tag, attrs in inspector.tags:
        assert "style" not in attrs and not any(name.startswith("on") for name in attrs), tag
    scripts_loaded = [attrs for tag, attrs in inspector.tags if tag == "script"]
    assert scripts_loaded == [{"type": "module", "src": f"{STATIC_ROOT}/app.js"}]
    links = {attrs["rel"]: attrs["href"] for tag, attrs in inspector.tags if tag == "link"}
    assert links == {"icon": "data:,", "stylesheet": f"{STATIC_ROOT}/style.css"}
    for reference in (*links.values(), scripts_loaded[0]["src"]):
        if reference.startswith(STATIC_ROOT + "/"):
            assert (UI_DIRECTORY / reference.removeprefix(STATIC_ROOT + "/")).is_file()
    assert {"session", "nav", "alerts", "page"} <= {attrs.get("id") for _, attrs in inspector.tags}


def test_the_stylesheet_takes_every_value_from_root_tokens():
    text, rules = stylesheet()
    assert "@import" not in text and "@font-face" not in text and "url(" not in text
    light, dark = root_tokens(rules, ()), root_tokens(rules, (DARK_SCHEME,))
    assert dark and set(dark) <= set(light)
    assert {name for name in light if name.startswith(THEMED_PREFIXES)} == set(dark)
    assert "system-ui" in light["--font-sans"]
    for name in set(CSS_VARIABLE_USE.findall(text)):
        assert name in light, f"{name} is used but never defined"
    for selectors, body in rules:
        if selectors[-1] == ":root":
            continue
        for name, value in declarations(body):
            assert not name.startswith("--"), f"{name} is defined outside :root"
            rest = CSS_VARIABLE_USE.sub(" ", value)
            assert not CSS_COLOR.search(rest), f"{selectors[-1]} {name}: {value}"
            numbers = set(CSS_NUMBER.findall(rest))
            assert numbers <= CSS_ALLOWED_NUMBERS, f"{selectors[-1]} {name}: {value}"
            if name in ("font-family", "font-size", "color", "background", "background-color", "border-color"):
                assert "var(" in value or value in ("inherit", "transparent"), f"{selectors[-1]} {name}: {value}"


def test_modules_import_only_existing_console_files():
    exports = {path: set(EXPORTED_NAME.findall(text_of(path))) for path in scripts()}
    for path in scripts():
        text = text_of(path)
        for specifier in STATIC_IMPORT.findall(text):
            assert specifier.startswith(("./", "../")) and specifier.endswith(".js"), specifier
            target = (path.parent / specifier).resolve()
            assert target.is_relative_to(UI_DIRECTORY) and target.is_file(), specifier
        for names, specifier in NAMED_IMPORT.findall(text):
            target = (path.parent / specifier).resolve()
            for name in (item.split(" as ")[0].strip() for item in names.split(",") if item.strip()):
                assert name in exports[target], f"{relative(path)} imports missing {name} from {specifier}"


def test_pages_export_a_default_console_page():
    for page in ui_pages(UI_DIRECTORY):
        text = text_of(UI_DIRECTORY / PAGES_DIRECTORY / f"{page['name']}{PAGE_SUFFIX}")
        assert "export default" in text and "mount" in text, page["name"]


def test_script_mirrors_match_the_server_constants():
    api = UI_DIRECTORY / "api.js"
    assert string_constant(api, "API_ROOT") == API_ROOT
    assert string_constant(api, "STATIC_ROOT") == STATIC_ROOT
    assert regex_body(javascript_constant(api, "TOKEN_PATTERN")) == TOKEN_PATTERN.pattern.removesuffix(r"\Z")
    assert regex_body(javascript_constant(UI_DIRECTORY / "app.js", "PAGE_NAME")) == PAGE_NAME.pattern.removesuffix(r"\Z")
    assert int(javascript_constant(UI_DIRECTORY / PAGES_DIRECTORY / "backups.js", "MAX_REASON")) == MAX_BACKUP_REASON
    assert TOKEN_PATTERN.fullmatch(TOKEN)


def test_error_codes_named_by_the_console_exist_on_the_server():
    source = "\n".join(path.read_text(encoding="utf-8") for path in sorted(PACKAGE.glob("*.py")))
    named = set(HINT_KEY.findall(HINT_TABLE.search(text_of(UI_DIRECTORY / "api.js")).group(1)))
    refusals = {code for path in scripts() for group in REFUSAL_SET.findall(text_of(path))
                for code in QUOTED_CODE.findall(group)}
    assert named and refusals
    for code in named | refusals:
        assert f'"{code}"' in source, code


def server_accepts_reason(reason):
    try:
        ManualBackupRequest.model_validate({"reason": reason})
    except ValidationError:
        return False
    return True


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not available to run the console modules")
def test_the_backup_reason_check_mirrors_the_server_rule(tmp_path):
    rule = javascript_constant(UI_DIRECTORY / PAGES_DIRECTORY / "backups.js", "UNPRINTABLE")
    script = tmp_path / "reasons.mjs"
    script.write_bytes(f"const rule = {rule};\nconst samples = JSON.parse(process.argv[2]);\n"
                       "console.log(JSON.stringify(samples.map((sample) => !rule.test(sample))));\n".encode("utf-8"))
    completed = subprocess.run([shutil.which("node"), str(script), json.dumps(REASON_SAMPLES)], capture_output=True,
                               timeout=NODE_TIMEOUT)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    expected = [server_accepts_reason(sample) for sample in REASON_SAMPLES]
    assert json.loads(completed.stdout) == expected and True in expected and False in expected


def routes_of(path):
    table = ROUTE_TABLE.search(text_of(path))
    assert table, "api.js defines no route table"
    return dict(ROUTE_ENTRY.findall(table.group(1)))


@pytest.fixture
def console(tmp_path):
    data = tmp_path / "data"
    store = Store(data / "aigis.sqlite3")
    bundle = SimpleNamespace(bootstrap=SimpleNamespace(clock=lambda: None), initialize=lambda state: state)
    identity = GenerationIdentity(code_digest="c" * 64, config_set="s" * 64, content_versions={})
    runtime = GenerationRuntime(Generation(identity, bundle, {}, {}))
    catalog = discover(PREFLIGHT_DOMAINS)
    application = create_admin_app(
        runtime, store, data_dir=data, admin_host="127.0.0.1", admin_port=PORT, game_port=GAME_PORT, token=TOKEN,
        preflight=NoopPreflight(), restart_check=NoopRestartCheck(), plan_store=PlanStore(),
        preflight_domains=PREFLIGHT_DOMAINS, catalog=catalog)
    return SimpleNamespace(application=application, catalog=catalog,
                           client=TestClient(application, base_url=f"http://127.0.0.1:{PORT}"))


def test_every_api_route_of_the_shell_exists_on_the_server(console):
    served = console.application.openapi()["paths"]
    routes = routes_of(UI_DIRECTORY / "api.js")
    for name, template in routes.items():
        assert API_ROOT + template in served, f"{name}: {template}"
    assert "get" in served[API_ROOT + routes["view"]]
    for path in scripts():
        text = text_of(path)
        for name in ROUTE_USE.findall(text):
            assert name in routes, f"{relative(path)} uses unknown route {name}"
        for call, name in ROUTE_CALL.findall(text):
            method = "post" if call == "post" else "get"
            assert method in served[API_ROOT + routes[name]], f"{relative(path)} sends {method} to {name}"


def test_the_view_catalog_publishes_every_view_schema(console):
    response = console.client.get(API_ROOT + "/views", headers=AUTH)
    assert response.status_code == 200 and console.client.get(API_ROOT + "/views").status_code == 401
    published = {entry["name"]: entry for entry in response.json()["views"]}
    registered = {view.name: view.describe() for view in console.catalog.views}
    assert set(TOOL_NAMES) <= set(published) and published == registered
    for name in TOOL_NAMES:
        assert published[name]["params_schema"] == console.catalog.views.get(name).describe()["params_schema"], name
    assert "tool-schemas.js" not in {relative(path) for path in ui_files()}
    assert not any("tool-schemas" in text_of(path) for path in scripts())


def test_pages_read_only_registered_views(console):
    registered = {view.name for view in console.catalog.views}
    referenced = {}
    for path in scripts():
        text = text_of(path)
        referenced.update(dict.fromkeys(VIEW_CALL.findall(text) + VIEW_CONSTANT.findall(text), relative(path)))
    assert {"state.browse", "runtime.readiness"} <= set(referenced)
    for name, owner in referenced.items():
        assert name in registered, f"{owner} reads unknown view {name}"


def test_the_console_serves_the_shipped_assets(console):
    client = console.client
    page = client.get("/admin/")
    assert page.status_code == 200 and page.content == (UI_DIRECTORY / "index.html").read_bytes()
    assert page.headers["content-security-policy"] == CONTENT_SECURITY_POLICY and page.headers["cache-control"] == "no-store"
    for path in ui_files():
        response = client.get(f"{STATIC_ROOT}/{relative(path)}")
        assert response.status_code == 200 and response.content == path.read_bytes(), relative(path)
        if path.suffix == ".js":
            assert response.headers["content-type"].split(";")[0] in JAVASCRIPT_TYPES, relative(path)
        if path.suffix == ".css":
            assert response.headers["content-type"].split(";")[0] == "text/css"
    listed = client.get(API_ROOT + "/ui-pages", headers=AUTH).json()["pages"]
    assert FIRST_PAGES <= {page["name"] for page in listed}


def test_package_data_ships_every_console_asset():
    patterns = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]
    console_patterns = [pattern for pattern in patterns["aigis_ps"] if pattern.startswith(UI_DIRECTORY.name + "/")]
    shipped = set()
    for pattern in console_patterns:
        matched = {path for path in PACKAGE.glob(pattern) if path.is_file()}
        assert matched, f"{pattern} matches no console asset"
        shipped |= matched
    assert shipped == set(ui_files())


def test_the_console_script_opens_a_workspace_browser_profile():
    data = SCRIPT.read_bytes()
    text = data.decode("ascii")
    assert b"\r" not in data
    assert ". (Join-Path $PSScriptRoot 'environment.ps1')" in text and "Initialize-AigisEnvironment" in text
    assert "'admin\\access.json'" in text and "'runtime\\console-browser'" in text
    assert "--user-data-dir=" in text and "--app=$url" in text and "msedge.exe" in text
    assert "Write-Output $url" in text
    assert f"schema_version -ne {ACCESS_SCHEMA}" in text
    assert TOKEN_PATTERN.pattern.removesuffix(r"\Z") in text
    assert text.index("if ($PrintUrl)") < text.index("Find-Edge")


def print_console_url(data_dir):
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
                           "-DataDirectory", str(data_dir), "-PrintUrl"], capture_output=True, timeout=POWERSHELL_TIMEOUT)


def run_console_script(data_dir, document):
    access = access_path(data_dir)
    access.parent.mkdir(parents=True, exist_ok=True)
    if document is None:
        access.unlink(missing_ok=True)
    else:
        access.write_bytes(json.dumps(document).encode("utf-8"))
    return print_console_url(data_dir)


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell is not available")
def test_the_console_script_prints_only_loopback_addresses_of_a_live_server(tmp_path):
    for host in ("127.0.0.1", "::1"):
        written = json.loads(write_access_file(tmp_path, token=TOKEN, host=host, port=PORT).read_bytes())
        printed = print_console_url(tmp_path)
        assert printed.returncode == 0 and printed.stdout.decode("ascii").strip() == written["url"], host
    url = f"http://127.0.0.1:{PORT}/admin/#token={TOKEN}"
    document = {"schema_version": ACCESS_SCHEMA, "url": url, "token": TOKEN, "admin_host": "127.0.0.1", "admin_port": PORT,
                "pid": os.getpid(), "created_at": "2026-11-03T00:00:00+00:00"}
    refused = [{**document, "url": f"http://evil.example:{PORT}/admin/#token={TOKEN}"},
               {**document, "url": f"http://127.0.0.1:{PORT}/admin/?token={TOKEN}"},
               {**document, "url": url + "\n"},
               {**document, "schema_version": ACCESS_SCHEMA + 1},
               {**document, "pid": 2 ** 31 - 1},
               None]
    for case in refused:
        result = run_console_script(tmp_path, case)
        assert result.returncode != 0 and not result.stdout.strip(), case


FAKE_DOM = r'''
import { pathToFileURL } from "node:url";

const root = process.argv[2];
const TOKEN = "t".repeat(43);

class FakeEvent {
    constructor(type, { bubbles = false } = {}) {
        Object.assign(this, { type, bubbles, defaultPrevented: false, target: null, currentTarget: null, stopped: false });
    }
    preventDefault() { this.defaultPrevented = true; }
    stopPropagation() { this.stopped = true; }
}

class FakeNode {
    constructor() { this.parentNode = null; this.childNodes = []; this.listeners = new Map(); }
    addEventListener(type, listener) { this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]); }
    removeEventListener(type, listener) {
        this.listeners.set(type, (this.listeners.get(type) ?? []).filter((item) => item !== listener));
    }
    dispatchEvent(event) {
        event.target ??= this;
        for (let node = this; node && !event.stopped; node = event.bubbles ? node.parentNode : null) {
            event.currentTarget = node;
            for (const listener of [...(node.listeners.get(event.type) ?? [])]) listener.call(node, event);
        }
        return !event.defaultPrevented;
    }
}

class FakeText extends FakeNode {
    constructor(text) { super(); this.data = String(text); }
    get nodeType() { return 3; }
    get textContent() { return this.data; }
    set textContent(value) { this.data = String(value); }
}

class FakeElement extends FakeNode {
    constructor(tag) {
        super();
        Object.assign(this, { localName: tag, tagName: tag.toUpperCase(), attributes: new Map(), dataset: {}, className: "",
            hidden: false, disabled: false, checked: false, open: false, value: "" });
    }
    get nodeType() { return 1; }
    get classList() {
        const tokens = () => this.className.split(/\s+/).filter(Boolean);
        return {
            add: (...names) => { this.className = [...new Set([...tokens(), ...names])].join(" "); },
            remove: (...names) => { this.className = tokens().filter((name) => !names.includes(name)).join(" "); },
            contains: (name) => tokens().includes(name),
        };
    }
    append(...nodes) {
        nodes.forEach((node) => {
            // A real DOM prints these as text, which in the console always means a skipped optional part leaked.
            if (node === null || node === undefined) throw new TypeError(`append received ${node}`);
            this.appendChild(typeof node === "string" ? new FakeText(node) : node);
        });
    }
    appendChild(node) { node.parentNode?.removeChild(node); node.parentNode = this; this.childNodes.push(node); return node; }
    removeChild(node) { this.childNodes = this.childNodes.filter((child) => child !== node); node.parentNode = null; return node; }
    replaceChildren(...nodes) {
        this.childNodes.forEach((child) => { child.parentNode = null; });
        this.childNodes = [];
        this.append(...nodes);
    }
    remove() { this.parentNode?.removeChild(this); }
    get textContent() { return this.childNodes.map((node) => node.textContent).join(""); }
    set textContent(value) { this.replaceChildren(String(value)); }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    removeAttribute(name) { this.attributes.delete(name); }
    focus() {}
    click() { if (!this.disabled) this.dispatchEvent(new FakeEvent("click", { bubbles: true })); }
    showModal() { this.open = true; }
    close() {
        if (!this.open) return;
        this.open = false;
        this.dispatchEvent(new FakeEvent("close"));
    }
}

const documentEvents = new FakeNode();
globalThis.document = {
    body: new FakeElement("body"), hidden: false,
    createElement: (tag) => new FakeElement(tag),
    createTextNode: (text) => new FakeText(text),
    addEventListener: (...args) => documentEvents.addEventListener(...args),
    removeEventListener: (...args) => documentEvents.removeEventListener(...args),
};
const replaced = [];
globalThis.location = { hash: `#token=${TOKEN}`, pathname: "/admin/", search: "" };
globalThis.history = { replaceState(state, title, url) { replaced.push(url); location.hash = ""; } };

const requests = [];
const queue = [];
globalThis.fetch = async (url, init) => {
    requests.push({ url: String(url), method: init.method, headers: { ...init.headers }, credentials: init.credentials,
        mode: init.mode, cache: init.cache, redirect: init.redirect, referrerPolicy: init.referrerPolicy,
        body: init.body === undefined ? null : JSON.parse(init.body) });
    const respond = queue.shift();
    if (respond === undefined) throw new Error(`Unexpected request ${init.method} ${url}`);
    return respond(url, init);
};
const reply = (status, body, headers = {}) => () => new Response(JSON.stringify(body),
    { status, headers: { "Content-Type": "application/json", ...headers } });
const offline = () => () => { throw new TypeError("fetch failed"); };
const echo = (fields) => (url, init) => reply(200, { op_id: JSON.parse(init.body).op_id, ...fields })();

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
async function settle() { for (let index = 0; index < 10; index += 1) await tick(); }
async function until(condition) {
    for (let index = 0; index < 500 && !condition(); index += 1) await new Promise((resolve) => setTimeout(resolve, 2));
}
function all(node, predicate, found = []) {
    if (node.nodeType === 1 && predicate(node)) found.push(node);
    (node.childNodes ?? []).forEach((child) => all(child, predicate, found));
    return found;
}
const find = (node, predicate) => all(node, predicate)[0] ?? null;
const action = (node, name) => find(node, (item) => item.dataset.action === name);
const field = (node, name) => find(node, (item) => item.dataset.field === name);
const openDialog = () => find(document.body, (item) => item.localName === "dialog" && item.open);
function answer(phrases) {
    const dialog = openDialog();
    all(dialog, (item) => item.localName === "input").forEach((input, index) => {
        input.value = phrases[index];
        input.dispatchEvent(new FakeEvent("input"));
    });
    find(dialog, (item) => item.localName === "form").dispatchEvent(new FakeEvent("submit"));
}
const load = (name) => import(pathToFileURL(`${root}/${name}`).href);
const results = {};
'''

HARNESS = FAKE_DOM + r'''
const { api, API_ROOT, ApiError, ROUTES, onUnauthorized, tokenState, watch } = await load("api.js");
const view = await load("diff-view.js");
const flow = await load("plan-flow.js");
const forms = await load("schema-form.js");
results.token = { replaced: [...replaced], state: tokenState(), hash: location.hash };

const unauthorized = [];
onUnauthorized((error) => unauthorized.push(error.code));
queue.push(reply(401, { error: { code: "admin_unauthorized", message: "A valid admin bearer token is required" } }));
try {
    await api.get(ROUTES.session);
} catch (error) {
    results.unauthorized = { code: error.code, definite: error.definite, hint: error.hint, notified: unauthorized,
        request: requests.at(-1) };
}

const restartDiff = { entries: [{ op: "add", path: "/rewritten_marker", after: true }], total: 1, truncated: false, omitted: 0 };
const blocked = {
    plan_id: null, plan_digest: "a".repeat(64), committable: false, expected_revision: 3, generation_id: "gen-1",
    context: { now_jst: "2026-11-03T12:00:00+09:00", acquisition_day: 9000 }, commands: [], touched: ["wallet"],
    writes: ["game_param.Money"], confirmations: [], steps: [], auto_backup: false,
    diff: { entries: [{ op: "replace", path: "/wallet", before: 1, after: 2 }], total: 1, truncated: false, omitted: 0 },
    preflight: { ok: true, readback: { wallet: [137] } },
    restart_check: { ok: false, error: { code: "admin_restart_incompatible",
        message: "Restarting the server would rewrite this state", details: { diff: restartDiff } } },
    audit: { ok: true }, state_sha256_before: "b".repeat(64), state_sha256_after: "c".repeat(64),
};
queue.push(reply(200, blocked));
const first = flow.planFlow({ commands: [{ operation: "wallet.credit", params: { amount: 1 } }], ids: new flow.OperationIds() });
document.body.append(first.element);
await first.preview();
results.blocked = {
    text: first.element.textContent, commitDisabled: action(first.element, "commit").disabled,
    restartCheck: find(first.element, (item) => item.dataset.check === "restart_check")?.className ?? null,
    request: requests.at(-1),
};

const ids = new flow.OperationIds();
const committed = [];
const destructive = { ...blocked, plan_id: "p-1", plan_digest: "d".repeat(64), committable: true, auto_backup: true,
    restart_check: { ok: true }, confirmations: [{ step: 0, operation: "wallet.debit", phrase: "DEBIT 5" }] };
queue.push(reply(200, destructive));
const second = flow.planFlow({ commands: [{ operation: "wallet.debit", params: { amount: 5 } }], ids,
    onCommitted: (result) => committed.push(result) });
document.body.append(second.element);
await second.preview();
queue.push(offline());
let pending = second.commit();
await settle();
answer(["DEBIT 5"]);
await pending;
const afterFailure = { text: second.element.textContent, previewDisabled: action(second.element, "preview").disabled,
    commitDisabled: action(second.element, "commit").disabled, commitLabel: action(second.element, "commit").textContent };
queue.push(echo({ kind: "command", plan_digest: "d".repeat(64), generation_id: "gen-1", revision_before: 3,
    revision_after: 4, steps: [{ step: 0, operation: "wallet.debit", effects: { debited: 5 }, warnings: [] }],
    readback: { wallet: [137, 34] }, backup_before_commit: { path: "backups/admin-auto-1.sqlite3", revision: 3 },
    replayed: true }));
pending = second.commit();
await settle();
answer(["DEBIT 5"]);
await pending;
const commits = () => requests.filter((request) => request.url.endsWith("/commits"));
results.retry = {
    afterFailure, opIds: commits().map((request) => request.body.op_id),
    confirmations: commits().map((request) => request.body.confirmations), text: second.element.textContent,
    replayed: find(second.element, (item) => item.dataset.replayed !== undefined)?.dataset.replayed ?? null,
    committed: committed.length, previewDisabled: action(second.element, "preview").disabled,
};

const fresh = { ...destructive, plan_id: "p-2", plan_digest: "f".repeat(64), confirmations: [] };
queue.push(reply(200, fresh));
await second.preview();
queue.push(reply(409, { error: { code: "plan_expired", message: "The plan expired or was replaced; preview it again" } }));
await second.commit();
const expired = { text: second.element.textContent, commitDisabled: action(second.element, "commit").disabled,
    previewDisabled: action(second.element, "preview").disabled };
queue.push(reply(200, fresh));
await second.preview();
queue.push(echo({ revision_before: 4, revision_after: 5, readback: {}, replayed: false }));
await second.commit();
results.settled = { expired, opIds: commits().slice(2).map((request) => request.body.op_id), text: second.element.textContent };

const unwritten = { ...fresh, plan_id: "p-3", plan_digest: "e".repeat(64) };
queue.push(reply(200, unwritten));
await second.preview();
queue.push(reply(503, { error: { code: "admin_backup_failed",
    message: "The automatic backup failed, so nothing was written" } }));
await second.commit();
const afterBackupFailure = { previewDisabled: action(second.element, "preview").disabled,
    commitLabel: action(second.element, "commit").textContent, hinted: second.element.textContent.includes("没有写入任何内容") };
queue.push(echo({ revision_before: 5, revision_after: 6, readback: {}, replayed: false }));
await second.commit();
results.unwritten = { afterBackupFailure, distinctIds: new Set(commits().slice(4).map((request) => request.body.op_id)).size };

const confirmation = flow.confirmPhrases([{ step: 0, operation: "admin.restore", phrase: "RESTORE 7" }]);
await settle();
const dialog = openDialog();
const input = find(dialog, (item) => item.localName === "input");
const paste = new FakeEvent("paste", { bubbles: true });
input.dispatchEvent(paste);
input.value = "RESTORE 8";
input.dispatchEvent(new FakeEvent("input"));
const wrongDisabled = action(dialog, "confirm").disabled;
input.value = "RESTORE 7";
input.dispatchEvent(new FakeEvent("input"));
const rightDisabled = action(dialog, "confirm").disabled;
find(dialog, (item) => item.localName === "form").dispatchEvent(new FakeEvent("submit"));
const phrases = await confirmation;
const cancelled = flow.confirmPhrases([{ step: 0, operation: "admin.restore", phrase: "RESTORE 7" }]);
await settle();
action(openDialog(), "cancel").click();
results.dialog = { pastePrevented: paste.defaultPrevented, wrongDisabled, rightDisabled, phrases,
    cancelled: await cancelled, open: openDialog() !== null, empty: await flow.confirmPhrases([]) };

const schema = {
    type: "object", additionalProperties: false, title: "Params", required: ["amount", "kind"],
    properties: {
        amount: { type: "integer", minimum: 0, title: "Amount" },
        kind: { $ref: "#/$defs/Kind" },
        note: { anyOf: [{ type: "string", maxLength: 5 }, { type: "null" }], default: null, title: "Note" },
        items: { type: "array", items: { type: "integer" }, maxItems: 2, default: [], title: "Items" },
        flag: { type: "boolean", default: false, title: "Flag" },
        extra: { type: "object", additionalProperties: { type: "integer" }, default: {}, title: "Extra" },
    },
    $defs: { Kind: { enum: ["gold", "gems"], title: "Kind", type: "string" } },
};
const form = forms.schemaForm(schema);
const control = (name, tag = "input") => find(field(form.element, name), (item) => item.localName === tag);
const empty = form.read();
control("amount").value = "12";
control("kind", "select").value = "1";
const accepted = form.read();
control("amount").value = "1.5";
const fractional = form.read();
control("amount").value = "-1";
const negative = form.read();
control("amount").value = "7";
const note = field(form.element, "note");
const [noteIncluded, noteNull] = all(note, (item) => item.type === "checkbox");
noteIncluded.checked = true;
noteNull.checked = false;
find(note, (item) => item.localName === "input" && item.type === "text").value = "hey";
const items = field(form.element, "items");
find(items, (item) => item.type === "checkbox").checked = true;
const add = find(items, (item) => item.localName === "button" && item.textContent === "添加一项");
add.click();
add.click();
const [firstItem, secondItem] = all(items, (item) => item.localName === "input" && item.type === "text");
firstItem.value = "3";
secondItem.value = "4";
const full = form.read();
form.showErrors([{ path: ["amount"], message: "server says no" }, { path: ["items", 1], message: "too big" },
    { path: [], message: "whole form" }]);
results.form = { empty, accepted, fractional: fractional.problems, negative: negative.problems, full,
    amountError: field(form.element, "amount").textContent, itemsError: items.textContent,
    formText: form.element.textContent, addDisabled: add.disabled };

const command = { operation: "wallet.credit", domain: "wallet", destructive: false, touched: ["wallet"],
    writes: ["game_param.Money"], preflight_none: {}, available: true, unavailable_reason: null,
    params_schema: { type: "object", additionalProperties: false, required: ["amount"],
        properties: { amount: { type: "integer", title: "Amount" } } } };
const panel = flow.commandForm(command, { ids: new flow.OperationIds() });
document.body.append(panel);
find(field(panel, "amount"), (item) => item.localName === "input").value = "5";
queue.push(reply(422, { error: { code: "admin_params_invalid", message: "The batch contains invalid commands",
    fields: [{ step: 0, loc: ["params", "amount"], type: "less_than", msg: "Input should be less than 3" }] } }));
action(panel, "preview-command").click();
await settle();
const offlineCommand = flow.commandForm({ ...command, available: false, unavailable_reason: "Wallet service is offline" });
results.command = { request: requests.at(-1).body, amountError: field(panel, "amount").textContent,
    unavailableText: offlineCommand.textContent, unavailableDisabled: action(offlineCommand, "preview-command").disabled };

const stop = new AbortController();
const revisions = [];
queue.push(reply(200, { view: "state.browse", revision: 1 }, { ETag: '"1:gen"' }));
queue.push(() => new Response(null, { status: 304, headers: { ETag: '"1:gen"' } }));
queue.push(reply(200, { view: "state.browse", revision: 2 }, { ETag: '"2:gen"' }));
watch((etag) => api.view("state.browse", { pointer: "" }, { etag, signal: stop.signal }), {
    interval: 1, signal: stop.signal,
    onData: (data) => { revisions.push(data.revision); if (revisions.length === 2) stop.abort(); },
});
await until(() => stop.signal.aborted);
const viewRequests = requests.filter((request) => request.url.includes("/views/"));
results.watch = { revisions, etags: viewRequests.map((request) => request.headers["If-None-Match"] ?? null),
    urls: viewRequests.map((request) => request.url) };

const refreshStop = new AbortController();
const refreshed = [];
let releaseLoad = null;
queue.push(() => new Promise((resolve) => { releaseLoad = () => resolve(reply(200, { revision: 1 })()); }));
queue.push(reply(200, { revision: 2 }));
const slow = watch(() => api.read(ROUTES.config, { signal: refreshStop.signal }), {
    interval: 60000, signal: refreshStop.signal,
    onData: (data) => { refreshed.push(data.revision); if (refreshed.length === 2) refreshStop.abort(); },
});
await until(() => releaseLoad !== null);
slow.refresh();
releaseLoad();
await until(() => refreshStop.signal.aborted);
refreshStop.abort();
results.refresh = { revisions: refreshed };

const pageAnswers = [];
const listed = view.cursorList({
    fetchPage: (cursor) => new Promise((resolve, reject) => pageAnswers.push({ cursor, resolve, reject })),
    renderItems: (items) => items.map((item) => view.h("p", {}, item)),
    empty: "EMPTY", end: "END", older: "OLDER",
});
listed.refresh();
pageAnswers[0].resolve({ items: ["first-a", "first-b"], next_cursor: 7 });
await settle();
const firstPage = listed.element.textContent;
action(listed.element, "older").click();
listed.refresh();
pageAnswers[2].resolve({ items: ["fresh"], next_cursor: null });
await settle();
pageAnswers[1].resolve({ items: ["late-older"], next_cursor: null });
await settle();
const raced = listed.element.textContent;
listed.refresh();
pageAnswers[3].reject(new ApiError({ status: 503, code: "admin_backup_failed", message: "boom", definite: false }));
await settle();
const failedPage = { text: listed.element.textContent, retry: action(listed.element, "retry") !== null };
action(listed.element, "retry").click();
pageAnswers[4].resolve({ items: [], next_cursor: null });
await settle();
results.cursorList = { cursors: pageAnswers.map((entry) => entry.cursor), firstPage, raced, failedPage,
    emptied: listed.element.textContent };

const presented = view.h("div");
let presentCalls = 0;
await view.present(presented, async () => {
    presentCalls += 1;
    if (presentCalls === 1) throw new ApiError({ status: 503, code: "admin_view_failed", message: "boom", definite: false });
    return "loaded";
}, (value) => view.h("p", {}, value));
const presentFailed = { retry: action(presented, "retry") !== null, boom: presented.textContent.includes("boom") };
action(presented, "retry")?.click();
await settle();
results.present = { presentFailed, text: presented.textContent, calls: presentCalls };

const definite = new ApiError({ status: 409, code: "plan_expired", message: "gone", definite: true });
const lost = new ApiError({ code: "console_network_error", message: "fetch failed", definite: false });
results.attempt = {
    definite: flow.renderAttemptFailure(definite, "RETRY").map((node) => node.textContent),
    lost: flow.renderAttemptFailure(lost, "RETRY").map((node) => node.textContent),
};
results.builds = ["running", "succeeded", "failed", "queued"].map((state) => view.buildBadge(state).textContent);
results.outcomeNotes = [
    { revision_before: 3, revision_after: 4 }, { revision_before: 3, revision_after: 3 },
    { receipts_invalidated: ["trades"] }, { receipts_invalidated: [] }, {},
].map((result) => flow.renderOutcome(result).textContent.includes("请让客户端按回读请求号刷新"));

const pageContext = (signal) => ({ signal, params: new URLSearchParams(), navigate: () => {}, session: () => null });
const backupRow = { id: 3, kind: "manual", reason: "before test", op_id: null, path: "backups/admin-manual-3.sqlite3",
    sha256: "e".repeat(64), revision: 9, created_at: "2026-11-03T03:00:00+00:00" };
const backupList = reply(200, { items: [backupRow], next_cursor: null,
    retention: { auto_keep: 1, auto_lifetime_seconds: 86400, manual_pruned: false } });
const restorePlan = {
    restore_digest: "9".repeat(64), expected_revision: 12, generation_id: "gen-a", player_id: "1",
    backup: { path: backupRow.path, sha256: backupRow.sha256, revision: 9, player_id: "1" }, cross_account: false,
    state_sha256_before: "1".repeat(64), state_sha256_after: "2".repeat(64), auto_backup: true,
    confirmations: [{ step: 0, operation: "admin.restore", phrase: "RESTORE 12" }],
    diff: { entries: [{ op: "replace", path: "/wallet", before: 5, after: 1 }], total: 1, truncated: false, omitted: 0 },
    checks: { energy_clock: { ok: true }, battle: { ok: true }, restart_check: { ok: true }, preflight: { ok: true, readback: {} },
        audit: { ok: true } },
    client: { quiet_seconds: 120, idle_seconds: 300, quiet: true, session_observed_since_switch: null,
        restart_required: false, notices: [] },
    committable: true,
};
const backupsStop = new AbortController();
const backupsRoot = view.h("div");
document.body.append(backupsRoot);
queue.push(backupList);
(await load("pages/backups.js")).default.mount(backupsRoot, pageContext(backupsStop.signal));
await settle();
const reasonInput = find(backupsRoot, (item) => typeof item.placeholder === "string" && item.placeholder.startsWith("备份原因"));
reasonInput.value = "全角　空格";
let mark = requests.length;
action(backupsRoot, "backup").click();
await settle();
const reasonRefused = { requests: requests.length - mark, warned: backupsRoot.textContent.includes("不可打印") };
queue.push(reply(200, restorePlan));
action(backupsRoot, "preview-restore").click();
await settle();
find(backupsRoot, (item) => item.localName === "input" && item.type === "checkbox").checked = true;
queue.push(offline());
action(backupsRoot, "restore").click();
await settle();
answer(["RESTORE 12"]);
await settle();
mark = requests.length;
action(backupsRoot, "preview-restore").click();
await settle();
const restoreGuard = { requests: requests.length - mark, warned: backupsRoot.textContent.includes("上一次恢复的结果未知") };
queue.push(echo({ kind: "restore", revision_before: 12, revision_after: 13, readback: { wallet: [5] },
    backup_before_restore: { path: "backups/admin-auto-13.sqlite3", revision: 12 }, client_restart_required: true,
    replayed: true }));
queue.push(backupList);
action(backupsRoot, "restore").click();
await settle();
answer(["RESTORE 12"]);
await settle();
const restoredText = backupsRoot.textContent;
const restoredReplay = find(backupsRoot, (item) => item.dataset.replayed !== undefined)?.dataset.replayed ?? null;
queue.push(reply(200, restorePlan));
action(backupsRoot, "preview-restore").click();
await settle();
const previewAfter = requests.at(-1).url;
const restoreCalls = requests.filter((request) => request.url.endsWith("/restores"));
queue.push(reply(409, { error: { code: "plan_changed", message: "The backup, the state or the generation changed" } }));
action(backupsRoot, "restore").click();
await settle();
answer(["RESTORE 12"]);
await settle();
const refusedRestore = { disabled: action(backupsRoot, "restore").disabled,
    hinted: backupsRoot.textContent.includes("plan_changed") && backupsRoot.textContent.includes("请重新预览") };
results.backupsPage = {
    reasonRefused, restoreGuard, opIds: restoreCalls.map((request) => request.body.op_id),
    bodies: restoreCalls.map(({ body: { op_id: ignored, ...body } }) => body),
    restored: restoredReplay === "true" && restoredText.includes("backups/admin-auto-13.sqlite3"),
    retryStaleNote: restoredText.includes("请让客户端按回读请求号刷新"), previewAfter, refusedRestore,
};
backupsStop.abort();

const quest = { key: "quest-rules", cli_flag: "--quest-rules", ps_parameter: "QuestRulesFile", domains: ["main_quests"],
    time_semantics: "frozen_per_operation", field_semantics: {}, resource_hint: false, examples: [], source: "default",
    mode: "default", path: null, sha256: null, shadowed: false, read_only: false, effective: { stamina: 1 },
    content_version: "cv-1", error: null, default: { stamina: 1 } };
const configView = (generation) => reply(200, {
    running: { set: "empty", generation_id: generation, config: { keys: [quest], overridden: [] } },
    current_generation_id: generation,
    active: { digest: null, active: null, set: "empty", error: null, roll_forward_pending: false, next_start: "empty" },
    staged: null, history: { items: [], next_cursor: null }, validation: null, client_warning: null, switching: false,
});
const job = { job_id: "job-1", state: "succeeded", progress: ["built"], candidate_id: "gen-b", error: null,
    elapsed_seconds: 1.5 };
const validation = { job_id: "job-1", op_id: "v-1", config_set: { id: "5".repeat(16) }, directory: "sets/5", base_set: null,
    keys: ["quest-rules"] };
const report = {
    report_id: "r".repeat(64), job_id: "job-1", revision: 12, running_set: "empty", validation, staged: null,
    created_at: { utc: "2026-11-03T03:00:00+00:00", jst: "2026-11-03T12:00:00+09:00" },
    generations: { current: { id: "gen-a", identity: {} }, candidate: { id: "gen-b", identity: {}, gate_issues: [] } },
    tier: "gated", identity_changes: [], reasons: { identity: [], accumulating: ["charisma_recovery_seconds"], initialize: false },
    keys: [], migration: { changed: false, energy: [], diff: { entries: [], total: 0, truncated: false, omitted: 0 } },
    checks: { serve_gate: { ok: true } }, battle: { in_progress: false, required: true, error: null }, default_mode: "apply",
    modes: { apply: { allowed: true, blockers: [], confirmations: [] }, stage: { allowed: true, blockers: [], confirmations: [] } },
    requirements: { backup: true, fence: true, client_closed: false }, overridden: [], content_versions: {},
    receipts_invalidated: [], availability: { regressions: [] }, client_actions: null,
    client: { quiet_seconds: 120, idle_seconds: 30, quiet: false, session_observed_since_switch: null,
        restart_required: false, notices: [] },
};
const configStop = new AbortController();
const configRoot = view.h("div");
document.body.append(configRoot);
queue.push(configView("gen-a"));
(await load("pages/config.js")).default.mount(configRoot, pageContext(configStop.signal));
await until(() => find(configRoot, (item) => item.localName === "select") !== null);
const editMode = find(configRoot, (item) => item.localName === "select");
editMode.value = "custom";
editMode.dispatchEvent(new FakeEvent("change"));
find(configRoot, (item) => item.localName === "textarea").value = '{"stamina": 2}';
queue.push(echo({ job, validation }));
queue.push(reply(200, { job, validation, report, candidate_expired: false }));
action(configRoot, "validate").click();
await until(() => action(configRoot, "switch-apply") !== null);
const switchStates = () => ["switch-apply", "switch-stage"].map((name) => action(configRoot, name)?.disabled ?? null);
queue.push(reply(409, { error: { code: "config_report_stale", message: "The running generation changed" } }));
action(configRoot, "switch-stage").click();
await settle();
const staleReport = { switches: switchStates(), note: configRoot.textContent.includes("这份报告已经用过或已失效") };
queue.push(reply(200, { job, validation, report, candidate_expired: false }));
action(configRoot, "refresh-report")?.click();
await until(() => action(configRoot, "switch-apply")?.disabled === false);
const refreshedReport = switchStates();
queue.push(offline());
action(configRoot, "switch-apply").click();
await settle();
const afterUnknown = switchStates();
mark = requests.length;
action(configRoot, "refresh-report")?.click();
await settle();
const refreshGuard = { requests: requests.length - mark, warned: configRoot.textContent.includes("不能刷新报告"),
    kept: action(configRoot, "switch-apply") !== null };
mark = requests.length;
action(configRoot, "validate").click();
await settle();
const switchGuard = { requests: requests.length - mark, warned: configRoot.textContent.includes("上一次切换的结果未知") };
queue.push(echo({ kind: "config", operation: "config.switch", mode: "apply", tier: "gated", config_set: "5".repeat(16),
    previous_set: "empty", generation_before: "gen-a", generation_after: "gen-b", keys: ["quest-rules"],
    receipts_invalidated: [], client_actions: null, revision_before: 12, revision_after: 12, readback: {},
    backup_before_switch: { path: "backups/admin-auto-12.sqlite3", revision: 12 }, replayed: true,
    pointer: { set: "5".repeat(16), published: true } }));
queue.push(configView("gen-b"));
action(configRoot, "switch-apply").click();
await until(() => configRoot.textContent.includes("运行中的配置已变化"));
const afterSuccess = switchStates();
const switchedText = configRoot.textContent;
const switchedReplay = find(configRoot, (item) => item.dataset.replayed !== undefined)?.dataset.replayed ?? null;
const nextMode = find(configRoot, (item) => item.localName === "select");
nextMode.value = "custom";
nextMode.dispatchEvent(new FakeEvent("change"));
find(configRoot, (item) => item.localName === "textarea").value = '{"stamina": 3}';
const runningJob = { ...job, job_id: "job-2", state: "running", progress: [], elapsed_seconds: 0.5 };
const jobPolls = () => requests.filter((request) => request.url.endsWith("/jobs/job-2")).length;
queue.push(echo({ job: runningJob, validation: { ...validation, job_id: "job-2" } }));
queue.push(offline());
action(configRoot, "validate").click();
await until(() => action(configRoot, "follow-job") !== null);
const replacedReport = { switches: switchStates(), placeholder: configRoot.textContent.includes("校验完成后在这里显示报告") };
queue.push(reply(200, { job: runningJob, validation: { ...validation, job_id: "job-2" }, report: null,
    candidate_expired: false }));
action(configRoot, "follow-job")?.click();
await until(() => configRoot.textContent.includes("构建中"));
const resumedJob = { polls: jobPolls(), running: configRoot.textContent.includes("构建中"),
    button: action(configRoot, "follow-job") !== null };
configStop.abort();
const switchCalls = requests.filter((request) => request.url.endsWith("/config/switches"));
const applyCalls = switchCalls.filter((request) => request.body.mode === "apply");
results.configPage = {
    validation: requests.find((request) => request.url.endsWith("/config/validations")).body.changes,
    switchGuard, refreshGuard, staleReport, refreshedReport, afterUnknown, afterSuccess, replacedReport, resumedJob,
    modes: switchCalls.map((request) => request.body.mode), opIds: applyCalls.map((request) => request.body.op_id),
    stageOpId: switchCalls[0].body.op_id, body: (({ op_id: ignored, ...body }) => body)(applyCalls[0].body),
    replayed: switchedReplay, rebuilt: switchedText.includes("运行中的配置已变化"),
    retryStaleNote: switchedText.includes("请让客户端按回读请求号刷新"),
};
const stagedStop = new AbortController();
const stagedRoot = view.h("div");
queue.push(async (url, init) => {
    const staged = await configView("gen-b")(url, init).json();
    return reply(200, { ...staged, staged: { set: "6".repeat(24), keys: null } })();
});
(await load("pages/config.js")).default.mount(stagedRoot, pageContext(stagedStop.signal));
await until(() => stagedRoot.textContent.includes("已暂存配置集"));
stagedStop.abort();
results.configPage.stagedKeys = stagedRoot.textContent.includes("变化的键：未知") && action(stagedRoot, "unstage") !== null;

const envelope = (name, data) => reply(200, { view: name, revision: 12, generation_id: "gen-b",
    server_now: { utc: "2026-11-03T03:00:00+00:00", jst: "2026-11-03T12:00:00+09:00" }, data });
const gate = { quiet_seconds: 120, idle_seconds: null, quiet: true, session_observed_since_switch: false,
    restart_required: true, notices: [{ reason: "content switch", created_at: "2026-11-03T03:00:00+00:00" }] };
const statusStop = new AbortController();
const statusRoot = view.h("div");
queue.push(reply(200, { actor: "admin-console", admin: { console: "http://127.0.0.1:8081/admin/", game_port: 8080 },
    server_now: { utc: "2026-11-03T03:00:00+00:00", jst: "2026-11-03T12:00:00+09:00" }, player_id: "1", revision: 12,
    generation_id: "gen-b", checks: { preflight_noop: false, restart_check_noop: true }, client: gate }));
queue.push(reply(200, { runtime: { current: { id: "gen-b", leases: 0 }, draining: null, fenced: false,
    build: { job_id: "job-2", state: "failed", progress: [], candidate_id: null, error: { code: "x", message: "y" },
        elapsed_seconds: 2 }, candidate: null, idle_seconds: null, switched_seconds_ago: null,
    session_observed_since_switch: null },
    identity: { code_digest: "c".repeat(64), config_set: "empty", content_versions: { quests: "v1" }, client_fingerprint: null,
        master_snapshot: null, base_resource_version: null, effective_resource_version: null },
    gate_issues: [], readiness_domains: ["bootstrap"], checks: {}, client: gate }));
queue.push(reply(200, { requests: [{ id: 1, created_at: "2026-11-03T03:00:00+00:00", route: "/api/login", wire_bytes: 10,
    response_bytes: 20, status: 500, error_code: "boom", structure: {} }] }));
queue.push(envelope("runtime.versions", { client_manifest: { path: "client/client-manifest.json", present: false, valid: null },
    masterdata: { in_use: null, disk: null, matches: null } }));
queue.push(reply(404, { error: { code: "admin_view_unknown", message: "No admin view is registered under this name" } }));
queue.push(envelope("runtime.readiness", { domains: { bootstrap: { ready: true } } }));
(await load("pages/status.js")).default.mount(statusRoot, pageContext(statusStop.signal));
const readinessBadge = () => find(statusRoot, (item) => item.className === "readiness-state")?.textContent ?? null;
const statusTexts = ["content switch", "/api/login", "服务端没有提供这个视图。", "尚未准备客户端"];
await until(() => readinessBadge() === "就绪" && statusTexts.every((text) => statusRoot.textContent.includes(text)));
statusStop.abort();

const stateStop = new AbortController();
const stateRoot = view.h("div");
queue.push(envelope("state.browse", { pointer: "", parent: null, type: "object", total: 2, limit: 100, offset: 0,
    next_offset: null, items: [{ key: "wallet", type: "object", size: 3 }, { key: "name", type: "string", value: "Aigis" }],
    ledgers: [{ key: "wallet", claimed: true, domain: "wallet", checked: true, valid: false,
        error: { code: "ledger_invalid", message: "ledger broken" } }] }));
(await load("pages/state.js")).default.mount(stateRoot, pageContext(stateStop.signal));
await until(() => stateRoot.textContent.includes("ledger broken"));
stateStop.abort();

const auditStop = new AbortController();
const auditRoot = view.h("div");
queue.push(reply(200, { items: [{ op_id: "o-1", sequence: 4, kind: "command", actor: "admin-console",
    operations: [{ operation: "wallet.credit", params: { amount: 1 } }], plan_digest: null, targets: ["wallet"],
    revision_before: 3, revision_after: 4, state_sha256_before: "1".repeat(64), state_sha256_after: "2".repeat(64),
    diff: { entries: [], total: 0, truncated: false, omitted: 0 }, generation_id: null, content_versions: {}, result: {},
    created_at: "2026-11-03T03:00:00+00:00" }], next_cursor: null }));
(await load("pages/audit.js")).default.mount(auditRoot, pageContext(auditStop.signal));
await until(() => auditRoot.textContent.includes("wallet.credit"));
auditStop.abort();
results.readPages = { status: statusRoot.textContent, readiness: readinessBadge(), state: stateRoot.textContent,
    audit: auditRoot.textContent };

const realTimeout = globalThis.setTimeout;
const queuedFetch = globalThis.fetch;
const served = {};
let liveGeneration = "gen-b";
globalThis.setTimeout = (callback, milliseconds = 0, ...rest) => realTimeout(callback, Math.min(milliseconds, 5), ...rest);
globalThis.fetch = async (url) => {
    const path = new URL(String(url), "http://console.test").pathname.replace(API_ROOT, "");
    served[path] = (served[path] ?? 0) + 1;
    const answers = {
        "/session": () => reply(200, { actor: "admin-console", player_id: "1", revision: 12, generation_id: liveGeneration,
            server_now: { jst: "2026-11-03T12:00:00+09:00" }, checks: {}, client: gate })(),
        "/runtime": () => reply(200, { runtime: { current: { id: liveGeneration, leases: 0 }, draining: null, fenced: false,
            build: null, candidate: null, switched_seconds_ago: null }, identity: { content_versions: {} }, gate_issues: [],
            readiness_domains: ["bootstrap"] })(),
        "/requests": () => reply(200, { requests: [] })(),
        "/views/runtime.versions": () => envelope("runtime.versions", { client_manifest: { path: "m", present: false },
            masterdata: {} })(),
        "/views/runtime.operations": () => envelope("runtime.operations", { registered_operations: 1,
            configured_handlers: [], unimplemented: [] })(),
        "/views/runtime.readiness": () => envelope("runtime.readiness", { domains: { bootstrap: { ready: true } } })(),
    };
    return answers[path]();
};
const generationViews = () => ["/views/runtime.versions", "/views/runtime.operations", "/views/runtime.readiness"]
    .map((path) => served[path] ?? 0);
const switchStop = new AbortController();
const switchRoot = view.h("div");
(await load("pages/status.js")).default.mount(switchRoot, pageContext(switchStop.signal));
await until(() => (served["/runtime"] ?? 0) >= 4);
const steadyViews = generationViews();
liveGeneration = "gen-c";
const runtimePolls = served["/runtime"];
await until(() => served["/runtime"] >= runtimePolls + 2 && generationViews().every((count) => count >= 2));
await settle();
const switchedViews = generationViews();
action(switchRoot, "refresh-versions")?.click();
await settle();
results.statusSwitch = { steadyViews, switchedViews, refreshedVersions: generationViews()[0] };
switchStop.abort();
await settle();
globalThis.fetch = queuedFetch;
globalThis.setTimeout = realTimeout;

const diff = view.renderDiff({ entries: [{ op: "replace", path: "/tables/presents", key: { PresentID: 9 }, field: "/A9",
    before: { truncated: true, bytes: 9000, sha256: "f".repeat(64) },
    after: { redacted: "response", encoding: "base64", bytes: 12, sha256: "0".repeat(64) } }],
    total: 3, truncated: true, omitted: 2 });
results.diff = { text: diff.textContent, none: view.renderDiff({ entries: [], total: 0 }).textContent };

location.hash = "";
const missing = await import(pathToFileURL(`${root}/api.js`).href + "?missing");
const before = requests.length;
let missingError = null;
try {
    await missing.api.get(missing.ROUTES.session);
} catch (error) {
    missingError = { code: error.code, definite: error.definite };
}
location.hash = "#token=short";
const malformed = await import(pathToFileURL(`${root}/api.js`).href + "?malformed");
results.tokenStates = { missing: missing.tokenState(), malformed: malformed.tokenState(), missingError,
    requestsWithoutToken: requests.length - before, replaced };

const shellNodes = Object.fromEntries(["session", "nav", "alerts", "page"].map((id) => [id, new FakeElement("div")]));
document.getElementById = (id) => shellNodes[id] ?? null;
globalThis.window = { addEventListener: () => {} };
location.replace = (url) => { location.hash = url; };
const injected = "globalThis.injected = true; export default { title: 'injected', order: 1, mount() {} };";
queue.push(reply(200, { actor: "admin-console", player_id: "1", revision: 12, generation_id: "gen-b",
    server_now: { jst: "2026-11-03T12:00:00+09:00" }, checks: { preflight_noop: true, restart_check_noop: false },
    client: { restart_required: false } }));
queue.push(reply(200, { pages: [
    { name: "earlier", module: "/admin/static/pages/earlier.js" },
    { name: "status", module: "/admin/static/pages/status.js" },
    { name: "injected", module: `data:text/javascript,${encodeURIComponent(injected)}` },
    { name: "Bad Name", module: "/admin/static/pages/Bad Name.js" },
] }));
await load("app.js");
await until(() => location.hash === "#/status");
results.shell = { nav: shellNodes.nav.textContent, injected: globalThis.injected === true, hash: location.hash,
    session: shellNodes.session.textContent };

const pages = {};
for (const name of process.argv.slice(3)) {
    const page = (await load(`pages/${name}.js`)).default;
    pages[name] = { title: typeof page?.title, order: typeof page?.order, mount: typeof page?.mount };
}
results.pages = pages;
console.log(JSON.stringify(results));
process.exit(0);
'''


@pytest.fixture(scope="module")
def browser_run(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not available to run the console modules")
    workspace = tmp_path_factory.mktemp("console-ui")
    copy = workspace / "ui"
    shutil.copytree(UI_DIRECTORY, copy, ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "package.json").write_bytes(b'{"type": "module"}\n')
    app = copy / "app.js"
    app.write_text(app.read_text(encoding="utf-8").replace(
        "import(entry.module)", 'import("." + entry.module.slice("/admin/static".length))'), encoding="utf-8")
    (copy / "pages" / "earlier.js").write_text(
        'export default { title: "提前页面", order: 0, mount() {} };', encoding="utf-8")
    harness = workspace / "harness.mjs"
    harness.write_bytes(HARNESS.encode("utf-8"))
    for path in sorted(copy.rglob("*.js")):
        checked = subprocess.run([node, "--check", str(path)], capture_output=True, timeout=NODE_TIMEOUT)
        assert checked.returncode == 0, checked.stderr.decode("utf-8", "replace")
    pages = [page["name"] for page in ui_pages(UI_DIRECTORY)]
    completed = subprocess.run([node, str(harness), str(copy), *pages], capture_output=True, timeout=NODE_TIMEOUT,
                               cwd=workspace)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    return json.loads(completed.stdout.decode("utf-8").strip().splitlines()[-1])


def test_the_token_is_taken_from_the_fragment_and_kept_out_of_urls(browser_run):
    assert browser_run["token"] == {"replaced": ["/admin/"], "state": "present", "hash": ""}
    request = browser_run["unauthorized"]["request"]
    assert request["headers"]["Authorization"] == "Bearer " + "t" * 43 and "t" * 43 not in request["url"]
    # A no-referrer POST would serialize its Origin as null, which the server's exact-origin check refuses.
    assert (request["credentials"], request["mode"], request["cache"], request["redirect"],
            request["referrerPolicy"]) == ("omit", "same-origin", "no-store", "error", "same-origin")
    states = browser_run["tokenStates"]
    assert states["missing"] == "missing" and states["malformed"] == "malformed"
    assert states["missingError"] == {"code": "console_token_missing", "definite": True}
    assert states["requestsWithoutToken"] == 0 and states["replaced"] == ["/admin/", "/admin/"]


def test_unauthorized_answers_map_to_a_hint_and_notify_the_shell(browser_run):
    unauthorized = browser_run["unauthorized"]
    assert unauthorized["code"] == "admin_unauthorized" and unauthorized["definite"] is True
    assert unauthorized["notified"] == ["admin_unauthorized"] and "open-console.ps1" in unauthorized["hint"]


def test_plan_flow_shows_the_restart_check_diff_and_blocks_the_commit(browser_run):
    blocked = browser_run["blocked"]
    assert "admin_restart_incompatible" in blocked["text"] and "/rewritten_marker" in blocked["text"]
    assert "/wallet" in blocked["text"] and "137" in blocked["text"]
    assert blocked["commitDisabled"] is True and "check-failed" in blocked["restartCheck"]
    assert blocked["request"]["url"] == API_ROOT + "/plans" and blocked["request"]["method"] == "POST"
    assert blocked["request"]["body"] == {"commands": [{"operation": "wallet.credit", "params": {"amount": 1}}]}


def test_plan_flow_retries_under_one_operation_id_and_shows_the_replay(browser_run):
    retry = browser_run["retry"]
    first, second = retry["opIds"]
    assert UUID4.fullmatch(first) and first == second
    assert retry["confirmations"] == [["DEBIT 5"], ["DEBIT 5"]]
    failure = retry["afterFailure"]
    assert failure["previewDisabled"] is True and failure["commitDisabled"] is False and failure["commitLabel"] == "重试提交"
    assert "console_network_error" in failure["text"] and first in failure["text"]
    assert retry["replayed"] == "true" and "重放" in retry["text"] and "3 → 4" in retry["text"]
    assert "请让客户端按回读请求号刷新" in retry["text"]
    assert "137、34" in retry["text"] and "backups/admin-auto-1.sqlite3" in retry["text"]
    assert retry["committed"] == 1 and retry["previewDisabled"] is False


def test_a_definite_answer_retires_the_operation_id(browser_run):
    settled, retry = browser_run["settled"], browser_run["retry"]
    expired_id, committed_id = settled["opIds"]
    assert len({expired_id, committed_id, retry["opIds"][0]}) == 3
    expired = settled["expired"]
    assert "plan_expired" in expired["text"] and "重新预览" in expired["text"]
    assert expired["commitDisabled"] is True and expired["previewDisabled"] is False
    assert "4 → 5" in settled["text"]


def test_refusals_raised_before_any_write_settle_the_operation(browser_run):
    unwritten = browser_run["unwritten"]
    assert unwritten["afterBackupFailure"] == {"previewDisabled": False, "commitLabel": "提交", "hinted": True}
    assert unwritten["distinctIds"] == 2


def test_destructive_confirmations_must_be_typed_exactly(browser_run):
    dialog = browser_run["dialog"]
    assert dialog["pastePrevented"] is True and dialog["wrongDisabled"] is True and dialog["rightDisabled"] is False
    assert dialog["phrases"] == ["RESTORE 7"] and dialog["cancelled"] is None
    assert dialog["open"] is False and dialog["empty"] == []


def test_schema_forms_read_strict_values_and_place_server_errors(browser_run):
    form = browser_run["form"]
    assert form["empty"]["ok"] is False and {"path": ["amount"], "message": "请填写数值"} in form["empty"]["problems"]
    assert form["accepted"] == {"ok": True, "value": {"amount": 12, "kind": "gems"}}
    assert form["fractional"] == [{"path": ["amount"], "message": "需要整数"}]
    assert form["negative"] == [{"path": ["amount"], "message": "不能小于 0"}]
    assert form["full"] == {"ok": True, "value": {"amount": 7, "kind": "gems", "note": "hey", "items": [3, 4]}}
    assert "server says no" in form["amountError"] and "too big" in form["itemsError"]
    assert "whole form" in form["formText"] and form["addDisabled"] is True


def test_command_forms_preview_through_the_plan_flow(browser_run):
    command = browser_run["command"]
    assert command["request"] == {"commands": [{"operation": "wallet.credit", "params": {"amount": 5}}]}
    assert "Input should be less than 3" in command["amountError"]
    assert "Wallet service is offline" in command["unavailableText"] and command["unavailableDisabled"] is True


def test_views_are_polled_with_their_etag(browser_run):
    watch = browser_run["watch"]
    assert watch["revisions"] == [1, 2]
    assert watch["etags"] == [None, '"1:gen"', '"1:gen"']
    assert watch["urls"][0] == API_ROOT + "/views/state.browse?params=%7B%22pointer%22%3A%22%22%7D"


def test_a_refresh_during_a_load_reloads_right_after_it(browser_run):
    assert browser_run["refresh"]["revisions"] == [1, 2]


def test_paged_lists_drop_answers_overtaken_by_a_refresh(browser_run):
    listed = browser_run["cursorList"]
    assert listed["cursors"] == [None, 7, None, None, None]
    assert listed["firstPage"] == "first-afirst-bOLDER"
    assert listed["raced"] == "freshEND"
    failed = listed["failedPage"]
    assert "admin_backup_failed" in failed["text"] and "boom" in failed["text"] and failed["retry"] is True
    assert listed["emptied"] == "EMPTYEND"


def test_failed_one_shot_reads_offer_a_retry(browser_run):
    assert browser_run["present"] == {"presentFailed": {"retry": True, "boom": True}, "text": "loaded", "calls": 2}


def test_the_status_page_reloads_generation_views_only_for_a_new_generation(browser_run):
    status = browser_run["statusSwitch"]
    assert status["steadyViews"] == [1, 1, 1]
    assert status["switchedViews"] == [2, 2, 2]
    assert status["refreshedVersions"] == 3


def test_unknown_outcomes_explain_the_same_id_retry(browser_run):
    attempt = browser_run["attempt"]
    assert len(attempt["definite"]) == 1 and "plan_expired" in attempt["definite"][0]
    assert len(attempt["lost"]) == 2 and "console_network_error" in attempt["lost"][0]
    assert "RETRY会沿用同一个操作 ID" in attempt["lost"][1]
    assert browser_run["builds"] == ["构建中", "已完成", "失败", "queued"]


def test_restores_wait_for_an_unknown_outcome_before_another_preview(browser_run):
    page = browser_run["backupsPage"]
    assert page["reasonRefused"] == {"requests": 0, "warned": True}
    assert page["restoreGuard"] == {"requests": 0, "warned": True}
    first, second = page["opIds"]
    assert UUID4.fullmatch(first) and first == second
    assert page["bodies"] == [{"backup": "backups/admin-manual-3.sqlite3", "restore_digest": "9" * 64,
                               "expected_revision": 12, "client_closed": True, "confirmations": ["RESTORE 12"]}] * 2
    assert page["restored"] is True and page["retryStaleNote"] is True
    assert page["previewAfter"] == API_ROOT + "/restores/preview"


def test_a_refused_restore_preview_cannot_be_executed_again(browser_run):
    assert browser_run["backupsPage"]["refusedRestore"] == {"disabled": True, "hinted": True}


def test_the_retry_stale_note_follows_revision_and_content_changes(browser_run):
    assert browser_run["outcomeNotes"] == [True, False, True, False, False]


def test_config_switches_wait_for_an_unknown_outcome_before_another_validation(browser_run):
    page = browser_run["configPage"]
    assert page["validation"] == {"quest-rules": {"stamina": 2}}
    assert page["switchGuard"] == {"requests": 0, "warned": True}
    assert page["refreshGuard"] == {"requests": 0, "warned": True, "kept": True}
    assert page["modes"] == ["stage", "apply", "apply"]
    first, second = page["opIds"]
    assert UUID4.fullmatch(first) and first == second and page["stageOpId"] != first
    assert page["body"] == {"report_id": "r" * 64, "mode": "apply", "confirm": {"client_closed": False, "confirmations": []}}
    assert page["replayed"] == "true" and page["rebuilt"] is True and page["retryStaleNote"] is False


def test_config_reports_retire_their_switches_once_used_or_refused(browser_run):
    page = browser_run["configPage"]
    assert page["staleReport"] == {"switches": [True, True], "note": True}
    assert page["refreshedReport"] == [False, False]
    assert page["afterUnknown"] == [False, False]
    assert page["afterSuccess"] == [True, True]
    assert page["replacedReport"] == {"switches": [None, None], "placeholder": True}
    assert page["stagedKeys"] is True


def test_a_failed_job_poll_can_be_followed_again(browser_run):
    assert browser_run["configPage"]["resumedJob"] == {"polls": 2, "running": True, "button": False}


def test_read_pages_render_status_state_and_audit_answers(browser_run):
    pages = browser_run["readPages"]
    status, state, audit = pages["status"], pages["state"], pages["audit"]
    assert "需要重启游戏客户端" in status and "content switch" in status and "空实现：只适合开发" in status
    assert "失败" in status and "/api/login" in status and "服务端没有提供这个视图。" in status
    assert "尚未准备客户端" in status and pages["readiness"] == "就绪"
    assert "wallet" in state and "Aigis" in state and "3 项" in state and "ledger broken" in state
    assert "wallet.credit" in audit and "修订号 3 → 4" in audit and "已经是最早的记录。" in audit


def test_the_shell_imports_only_modules_derived_from_page_names(browser_run):
    shell = browser_run["shell"]
    assert shell["injected"] is False and shell["nav"] == "提前页面运行状态"
    assert "玩家 1" in shell["session"] and "预检为空实现" in shell["session"]


def test_traditional_shell_defaults_to_status_even_when_another_page_sorts_first(browser_run):
    assert browser_run["shell"]["nav"].startswith("提前页面")
    assert browser_run["shell"]["hash"] == "#/status"


def test_diffs_summarize_truncated_and_redacted_values(browser_run):
    diff = browser_run["diff"]
    assert "已截断：9000 字节" in diff["text"] and "报文已脱敏：12 字节" in diff["text"]
    assert "PresentID=9" in diff["text"] and "省略 2 项" in diff["text"]
    assert diff["none"] == "没有状态变化"


def test_every_page_module_links_and_exports_a_page(browser_run):
    pages = browser_run["pages"]
    assert FIRST_PAGES <= set(pages)
    assert all(page == {"title": "string", "order": "number", "mount": "function"} for page in pages.values())
