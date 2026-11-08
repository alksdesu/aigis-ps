import json
import re
import shutil
import subprocess

import pytest

from aigis_ps.admin_app import PAGES_DIRECTORY, UI_DIRECTORY, ui_pages
from aigis_ps.admin_cmd_migrations import SPLIT_TICKET_MAIL
from aigis_ps.admin_cmd_units import ABSENT, EMPTY, ENTRIES, INVALID, OVERRIDES_PHRASE, REMOVE_OVERRIDES
from aigis_ps.admin_command import PLAN_CAPACITY, PLAN_TTL_SECONDS, AdminError, error_payload
from aigis_ps.admin_commit import AUDIT_SKIPPED, AUDIT_TOO_LARGE, attempt
from aigis_ps.admin_fields import LOGIN_REWARDS, BitLevel
from aigis_ps.admin_gates import CLIENT_QUIET_SECONDS, ClientGate
from aigis_ps.admin_preflight import PREFLIGHT_DOMAINS
from aigis_ps.admin_registry import discover
from aigis_ps.admin_views import DEFAULT_PAGE_ITEMS, service
from aigis_ps.admin_views_events import (LEDGERS, REPAIR_SECTION, DefinitionState, EntryStatus, entry as ledger_entry,
                                         repair_section)
from aigis_ps.admin_views_presents import TYPE_KINDS
from aigis_ps.admin_views_units import OVERRIDE_GUARDS
from aigis_ps.background_catalog import AUDIT_KINDS
from aigis_ps.blessing_service import FILTER_FIELD
from aigis_ps.business_calendar import BUSINESS_DAY_START_HOUR, MARKER
from aigis_ps.crystal_wallet import CRYSTAL_LIMITS
from aigis_ps.energy_clock import PREFIXES
from aigis_ps.errors import REFUSAL_AFTERMATH
from aigis_ps.event_pass_service import REBIND_OPERATION as PASS_REBIND_OPERATION
from aigis_ps.generation import ADMIT_TIMEOUT_SECONDS, CANDIDATE_TTL_SECONDS
from aigis_ps.item_classes import ItemClass
from aigis_ps.login_reward_service import (COOKING_MIGRATION, OFFLINE_COOKING_MIGRATION, PENDING_STAMP_WARNING_DAYS,
                                           REBASE_OPERATION)
from aigis_ps.login_stamp_pending import MAX_PENDING_STAMPS
from aigis_ps.storage import AUTO_BACKUP_KEEP, MAX_AUDIT_FIELD_BYTES, MAX_BACKUP_REASON
from aigis_ps.tower_trade_state import PRODUCT_REUSED as TOWER_PRODUCT_REUSED, REBIND_OPERATION
from aigis_ps.unit_removal import Protection
from aigis_ps.wallet_money import GOLD_LIMIT

from test_admin_ui_assets import FAKE_DOM, NODE_TIMEOUT, UUID4, VIEW_CONSTANT, scripts, string_constant, text_of


PAGES = UI_DIRECTORY / PAGES_DIRECTORY
DOMAIN_PAGES = frozenset(("account", "battle", "currencies", "items", "presents", "units", "storage", "settings", "missions",
                          "events", "votes", "rules", "tools"))
VIEW_DOMAINS = frozenset(("account", "battle", "events", "items", "letstd", "presents", "progress", "rules", "settings",
                          "tools", "units", "votes"))
# The first console pages keep these slots, so domain pages stay clear of them.
RESERVED_ORDERS = frozenset((10, 20, 30, 40, 50))
SETTINGS = "settings"
STORAGE = "storage"
VOTES = "votes"
REUSED_PRODUCT = 3001
TOWER_TRADE_OPERATIONS = (65, 69)
LOGIN_REWARD_OPERATIONS = (115,)
TOWER_NOTICE = {"code": TOWER_PRODUCT_REUSED, "reused_products": [REUSED_PRODUCT],
                "refused_operations": list(TOWER_TRADE_OPERATIONS), "repair": REBIND_OPERATION,
                "client": REFUSAL_AFTERMATH}
BEGINNER_REPAIR = {"command": REBASE_OPERATION, "refused_operations": list(LOGIN_REWARD_OPERATIONS),
                   "client": REFUSAL_AFTERMATH}
REPAIR_KEYS = {"event_login": ({"campaign_id": 7, "ordinal": 2}, {"campaign_id": 9, "ordinal": 1}),
               "comeback_login": ({"round_id": 3, "campaign_id": 7, "ordinal": 1},
                                  {"round_id": 3, "campaign_id": 7, "ordinal": 2},
                                  {"round_id": 4, "campaign_id": 7, "ordinal": 1}),
               "purchased_login": ({"purchase_id": 11, "campaign_id": 8, "ordinal": 3},),
               "daily_gift": ({"gift_id": 5, "earned_day": "2026-10-01"}, {"gift_id": 5, "earned_day": "2026-10-02"}),
               "login_rewards": ({"day": "2026-09-30"}, {"day": "2026-10-05"})}
PASS_LEDGER = "event_pass"
PASS_REBINDS = ({"pass_id": "pass_a"}, {"pass_id": "pass_b"})
LOGIN_REWARDS_VIEW = "progress.login_rewards"
LOGIN_COOKING_VIEW = "progress.login_cooking"
LOGIN_CHECKS = {LOGIN_REWARDS_VIEW: {"rules": "新手奖励总数核对", "stamp_capacity": "待发印章容量"},
                LOGIN_COOKING_VIEW: {"cooking": "料理阈值核对"}}
UNCHANGED, CHANGED, UNAVAILABLE = "unchanged", "changed", "unavailable"
SAVED_BEGINNER_TOTAL, CURRENT_BEGINNER_TOTAL = 7, 14
SAVED_COOKING_BORDER, CURRENT_COOKING_BORDER = 3000, 5000
HELD_STAMPS = 2
SETTLED_LABEL = "最后结算"
BUNDLED_PRESENTS = (7, 9)
STAGE_UNSUPPORTED_HINT = "含时间累积规则、料理阈值或新手奖励总数变化的切换只能在运行中应用，不能暂存。"
QUIET_DETAIL = "quiet_seconds"
QUOTED = re.compile(r"\"([^\"]+)\"")
LABEL_KEY = re.compile(r"(\w+):")
LABEL_TEXT = re.compile(r"(\w+): \"([^\"]+)\"")
NOTE_CONSTANT = re.compile(r"^(?:export\s+)?const [A-Z_]*NOTES? = (.*?);$", re.MULTILINE | re.DOTALL)
# ASCII boundaries, because a name may touch Chinese text that Unicode counts as word characters.
DOTTED_NAME = re.compile(r"\b[a-z_]+(?:\.[a-z_]+)+\b", re.ASCII)
FILE_SUFFIXES = frozenset(("js", "json", "md", "py"))
TOP_KEY = re.compile(r"^ {4}(\w+):", re.MULTILINE)
PLACEMENT = re.compile(r"(\w+): Object\.freeze\(\[(.*?)\]\)", re.DOTALL)
KEPT_BACKUPS = re.compile(r"只保留最新 (.+?) 份")
MIRRORED_KEEP, SERVED_KEEP = "${AUTO_BACKUP_KEEP}", "${retention.auto_keep}"
BACKUP_NOTE_PAGES = ("backups.js", "units.js")
SPELLED_REASON_LIMIT = re.compile(rf"\b{MAX_BACKUP_REASON}\b", re.ASCII)


def declared(path, name):
    found = re.search(rf"^const {name} = Object\.freeze\((.*?)\);$", text_of(path), re.MULTILINE | re.DOTALL)
    assert found, f"{path.name} defines no {name}"
    return found.group(1)


@pytest.fixture(scope="module")
def catalog():
    return discover(PREFLIGHT_DOMAINS)


def operations_of(catalog, domain):
    return sorted(command.operation for command in catalog.commands if command.domain == domain)


def named_commands(text):
    return {name for name in DOTTED_NAME.findall(text) if name.rpartition(".")[2] not in FILE_SUFFIXES}


def repair_sections():
    views = {view.name: view for view in LEDGERS}
    changed = {"state": DefinitionState.CHANGED}
    sections = {name: views[name].repair.describe([ledger_entry(key, EntryStatus.PENDING, definition=changed)
                                                   for key in keys])
                for name, keys in REPAIR_KEYS.items()}
    sections["login_rewards"][REPAIR_SECTION] = BEGINNER_REPAIR
    sections[PASS_LEDGER] = repair_section(PASS_REBIND_OPERATION, list(PASS_REBINDS))
    return sections


def login_rule_checks(changed):
    beginner = {"ok": True, "saved_total": SAVED_BEGINNER_TOTAL,
                "current_total": CURRENT_BEGINNER_TOTAL if changed else SAVED_BEGINNER_TOTAL, "changed": changed}
    cooking = {"ok": True, "saved_border": SAVED_COOKING_BORDER,
               "current_border": CURRENT_COOKING_BORDER if changed else SAVED_COOKING_BORDER, "changed": changed}
    if changed:
        refused = list(LOGIN_REWARD_OPERATIONS)
        beginner.update(refused_operations=refused, repair=REBASE_OPERATION, client=REFUSAL_AFTERMATH)
        cooking.update(refused_operations=refused, repair=COOKING_MIGRATION, offline_repair=list(OFFLINE_COOKING_MIGRATION),
                       client=REFUSAL_AFTERMATH)
    remaining = MAX_PENDING_STAMPS - HELD_STAMPS
    return {"rules": beginner, "cooking": cooking, "stamp_capacity": {
        "ok": True, "count": HELD_STAMPS, "capacity": MAX_PENDING_STAMPS, "remaining": remaining,
        "warning": remaining < PENDING_STAMP_WARNING_DAYS}}


def unavailable_login_checks():
    refused = attempt(lambda: service(None, LOGIN_REWARDS))
    return dict.fromkeys(login_rule_checks(False), refused)


LOGIN_RULES = {UNCHANGED: login_rule_checks(False), CHANGED: login_rule_checks(True),
               UNAVAILABLE: unavailable_login_checks()}


class BusyClient:
    def idle_seconds(self):
        return 0.0

    def session_observed_since_switch(self):
        return None


def client_active_refusal():
    with pytest.raises(AdminError) as caught:
        ClientGate(BusyClient()).require_closed(True)
    return error_payload(caught.value)


PAGE_FLOWS = r'''
import { readFileSync } from "node:fs";

const fixtures = JSON.parse(readFileSync(process.argv[3], "utf-8"));
const {
    ADMIT_TIMEOUT_SECONDS, API_ROOT, AUDIT_FIELD_BYTES, AUTO_BACKUP_KEEP, CANDIDATE_TTL_SECONDS, errorHint, PLAN_CAPACITY,
    PLAN_TTL_SECONDS,
} = await load("api.js");
const view = await load("diff-view.js");
const kit = await load("view-kit.js");
const ledgers = await load("ledger-view.js");
const forms = await load("schema-form.js");
const flow = await load("plan-flow.js");
const viewSchemas = Object.fromEntries(fixtures.views.map((entry) => [entry.name, entry.params_schema]));

results.mirrors = {
    goldLimit: kit.GOLD_LIMIT, pageLimit: kit.PAGE_LIMIT, crystalLimits: kit.CRYSTAL_LIMITS, commandPages: kit.COMMAND_PAGES,
    protections: Object.keys(kit.PROTECTION_LABELS), anchors: Object.keys(kit.ANCHOR_LABELS),
    definitions: Object.keys(ledgers.DEFINITION_LABELS), statuses: Object.keys(ledgers.STATUS_LABELS),
    planCapacity: PLAN_CAPACITY, planTtl: PLAN_TTL_SECONDS, candidateTtl: CANDIDATE_TTL_SECONDS,
    auditFieldBytes: AUDIT_FIELD_BYTES, autoBackupKeep: AUTO_BACKUP_KEEP, admitTimeout: ADMIT_TIMEOUT_SECONDS,
};
results.pages = {};
for (const name of fixtures.pages) {
    const page = (await load(`pages/${name}.js`)).default;
    results.pages[name] = { title: page.title, order: page.order, frozen: Object.isFrozen(page) };
}

const NOW = { utc: "2026-11-03T03:00:00+00:00", jst: "2026-11-03T12:00:00+09:00" };
const SNAPSHOT_TAG = '"12:gen-b"';
// A record handed to the DOM as a child prints as this text instead of its fields.
const LEAKED_RECORD = String({});
const FAST_POLL_MS = 5;
const QUIET_MS = 60;
const SNAPSHOT_POLLS = 3;
const served = [];
const unexpected = [];
let routes = {};
globalThis.fetch = async (url, init) => {
    const address = new URL(String(url), "http://console.test");
    const path = address.pathname.slice(API_ROOT.length);
    const text = address.searchParams.get("params");
    const request = { method: init.method, path, params: text === null ? null : JSON.parse(text),
        etag: init.headers["If-None-Match"] ?? null, body: init.body === undefined ? null : JSON.parse(init.body) };
    served.push(request);
    const answer = routes[`${init.method} ${path}`];
    if (answer === undefined) {
        unexpected.push(`${init.method} ${path}`);
        return reply(404, { error: { code: "admin_view_unknown", message: "No admin view is registered under this name" } })();
    }
    return answer(request);
};
const envelope = (name, data, etag = null) => () => new Response(JSON.stringify({ view: name, revision: 12,
    generation_id: "gen-b", server_now: NOW, data }),
    { status: 200, headers: { "Content-Type": "application/json", ...(etag === null ? {} : { ETag: etag }) } });
const listing = (items, extra = {}) => ({ items, total: items.length, offset: 0, limit: 100, next_offset: null, ...extra });
const catalog = { commands: fixtures.commands, field_owners: fixtures.field_owners };
const commandRoute = { "GET /commands": () => reply(200, catalog)() };
const viewRoute = { "GET /views": () => reply(200, { views: fixtures.views })() };
const refusal = (status, code, message) => reply(status, { error: { code, message } })();
const requestsTo = (path, since = 0) => served.slice(since).filter((request) => request.path === path);
const operations = (node) => all(node, (item) => item.localName === "section" && item.dataset.operation !== undefined)
    .map((item) => item.dataset.operation);
const card = (node, title) => find(node, (item) => item.localName === "section" && item.childNodes[0]?.textContent === title);
const input = (node) => find(node, (item) => item.localName === "input" && item.type === "text");
const crashes = (node) => all(node, (item) => item.className === "error" && item.dataset.code === "")
    .map((item) => item.textContent);
const cellsOf = (row) => row.childNodes.map((cell) => cell.textContent);
const rowsOf = (node) => all(node, (item) => item.localName === "tr").map(cellsOf);
const pairsOf = (node) => all(node, (item) => item.className === "kv-row").map(cellsOf);
const plan = (operation, phrase) => ({ plan_id: "p-1", plan_digest: "d".repeat(64), committable: true, expected_revision: 12,
    generation_id: "gen-b", context: { now_jst: NOW.jst, acquisition_day: 9000 }, commands: [], touched: ["units"], writes: [],
    confirmations: [{ step: 0, operation, phrase }], steps: [], auto_backup: true,
    diff: { entries: [], total: 0, truncated: false, omitted: 0 }, preflight: { ok: true, readback: {} },
    restart_check: { ok: true }, audit: { ok: true }, state_sha256_before: "1".repeat(64), state_sha256_after: "2".repeat(64) });
results.texts = {};
results.crashes = {};
results.leaks = {};

async function mountPage(name, query = {}) {
    const stop = new AbortController();
    const root = view.h("div");
    document.body.append(root);
    (await load(`pages/${name}.js`)).default.mount(root, { signal: stop.signal, params: new URLSearchParams(query),
        navigate: () => {}, session: () => null });
    return { root, stop };
}

function finish(label, page) {
    results.texts[label] = page.root.textContent;
    results.crashes[label] = crashes(page.root);
    results.leaks[label] = page.root.textContent.includes(LEAKED_RECORD);
    page.stop.abort();
    page.root.remove();
}

function specify(node, name, text) {
    const optional = field(node, name);
    const [include, empty] = all(optional, (item) => item.type === "checkbox");
    include.checked = true;
    empty.checked = false;
    input(optional).value = text;
}

const overrides = { present: true, blocking: true, shape: "entries", entries: 2, bytes: 48, sha256: "a".repeat(64),
    pointer: "/unit_dynamic_overrides", truncated: false, value: { 7: { Lv: 99 } },
    guards: { training: 5, silver_trade: 106, shop_growth: 113 }, removal: "units.overrides.remove" };
const unitRoutes = (shown) => ({
    ...commandRoute,
    "GET /views/units.overrides": envelope("units.overrides", shown, SNAPSHOT_TAG),
    "GET /views/units.roster": envelope("units.roster", { battle: { group: 1 }, last_unique_id: 30, protections: { ok: true },
        growth: { ok: true }, units: listing([{ unit: { GetIndex: 3, UniqueID: 30, CardID: 1, ClassID: 100, Exp: 0, Love: 0,
            SkillLevel: 1, _DisplayType: 0 }, flags: { barracks: 0, bits: [] }, protections: ["prince"],
            growth: { ok: true, level: 1, level_cap: 50, love_limit: 100, skill_level_max: 10 } }]) }, SNAPSHOT_TAG),
    "GET /views/units.squads": envelope("units.squads", { battle: {}, legacy_squad_imported: true, squads: [] }, SNAPSHOT_TAG),
    "GET /views/units.ledgers": envelope("units.ledgers", { gifts: { ok: true, persisted: true, ledger: { tiers: 1 } } },
        SNAPSHOT_TAG),
    "POST /plans": () => reply(200, plan("units.overrides.remove", "REMOVE UNIT OVERRIDES"))(),
});
routes = unitRoutes(overrides);
const units = await mountPage("units");
await until(() => action(units.root, "remove-overrides") !== null && operations(units.root).length > 0);
action(units.root, "remove-overrides").click();
await until(() => requestsTo("/plans").length === 1 && units.root.textContent.includes("检查已通过"));
results.units = { plan: requestsTo("/plans")[0].body, operations: operations(units.root), text: units.root.textContent,
    overrides: find(units.root, (item) => item.dataset.view === "units.overrides").textContent };
finish("units", units);
routes = unitRoutes({ ...overrides, truncated: true, value: null });
const truncated = await mountPage("units");
await until(() => find(truncated.root, (item) => item.dataset.link === "state") !== null);
results.units.stateLink = find(truncated.root, (item) => item.dataset.link === "state").getAttribute("href");
finish("unitsTruncated", truncated);

const markedCalendar = { ok: true, marker: fixtures.calendar_marker };
const summaryOf = ({ money = 5000, calendar = markedCalendar } = {}) => ({
    game_param: { ok: true, fields: { Money: { wire: "A9", type_code: "I", value: money } }, anchors: {}, rank: null },
    crystals: { ok: true, totals: { free: 10, paid: 0, total: 10 }, buckets: listing([]) },
    spending: { ok: true, ledger: { spent: 0 }, last_time_utc: null }, capacity: { ok: true, ledger: {}, last_time_utc: null },
    energy_clock: { ok: true, marker: { version: 1 } }, business_calendar: calendar });
const projectionRoute = envelope("account.projection", {
    calendar: { logical_day: "2026-11-03", acquisition_day: 9000, acquisition_date: "2026-11-03" },
    energy: { ok: true, projected: true, persisted: false, note: "Projected", as_of: NOW, recovery_seconds: 300,
        charisma: { before: 1, after: 2 }, anchors: { before: {}, after: {} }, rank: null,
        capacity: { ok: true, ledger: {}, last_time_utc: null } } });
let balance = 5000;
let releaseCommands = null;
const commandsHeld = new Promise((resolve) => { releaseCommands = resolve; });
routes = {
    "GET /commands": async () => {
        await commandsHeld;
        return reply(200, catalog)();
    },
    "GET /views/account.summary": () => envelope("account.summary", summaryOf({ money: balance }), SNAPSHOT_TAG)(),
};
const currencies = await mountPage("currencies");
const goldForm = () => find(currencies.root, (item) => item.dataset.operation === "wallet.gold.set");
const goldCurrent = () => input(field(goldForm(), "current"))?.value ?? null;
const refreshBalances = () => action(find(currencies.root, (item) => item.dataset.view === "account.summary"),
    "refresh-view").click();
await until(() => currencies.root.textContent.includes("5,000"));
balance = 6000;
refreshBalances();
await until(() => currencies.root.textContent.includes("6,000"));
releaseCommands();
await until(() => goldForm() !== null);
const openedWith = goldCurrent();
balance = 7000;
refreshBalances();
await until(() => goldCurrent() === "7000");
results.currencies = { openedWith, refreshedTo: goldCurrent(), goldText: goldForm().textContent,
    debitText: find(currencies.root, (item) => item.dataset.operation === "wallet.gold.debit")?.textContent ?? "",
    summaryReads: requestsTo("/views/account.summary").length, text: currencies.root.textContent };
finish("currencies", currencies);

const calendars = {
    account: markedCalendar,
    legacyCalendar: { ok: true, marker: null },
    invalidCalendar: { ok: false, error: { code: "business_calendar_invalid",
        message: "Saved business calendar marker is invalid or unsupported" } },
};
results.calendars = {};
for (const [label, calendar] of Object.entries(calendars)) {
    routes = { ...commandRoute, "GET /views/account.projection": projectionRoute,
        "GET /views/account.summary": envelope("account.summary", summaryOf({ calendar }), SNAPSHOT_TAG) };
    const page = await mountPage("account");
    await until(() => ["JST 逻辑日", "业务日历标记"].every((text) => page.root.textContent.includes(text))
        && operations(page.root).length > 0);
    results.calendars[label] = page.root.textContent;
    finish(label, page);
}

routes = {
    ...commandRoute,
    "GET /views/units.storage": envelope("units.storage", { fame: { ok: true, deposit_limit: 10, rows: [] },
        fairy: { ok: true, rows: [] }, silver: { ok: true, per_card_capacity: 99, rows: [{ CardID: 1, Count: 2 }] } },
        SNAPSHOT_TAG),
    "GET /views/units.barracks": envelope("units.barracks", { barracks: { ok: true, capacities: [100, 50], counts: [3, 0],
        primary_available: 97 }, grantable: { ok: true, units: 147 },
        battle_reservations: { ok: true, grants: [{ card_id: 7, level: 1, count: 2 }] } }, SNAPSHOT_TAG),
};
const storage = await mountPage("storage");
const storagePanel = () => find(storage.root, (item) => item.dataset.domain === "storage");
const barracksText = () => find(storage.root, (item) => item.dataset.view === "units.barracks")?.textContent ?? "";
await until(() => storagePanel() !== null && operations(storagePanel()).length > 0
    && ["第一兵营可用格数", "card_id"].every((text) => barracksText().includes(text)));
results.storage = { operations: operations(storagePanel()), text: storage.root.textContent, barracks: barracksText(),
    fameBadge: find(storage.root, (item) => item.dataset.operation === "fame.adjust_stock").textContent.includes("按参数确认") };
finish("storage", storage);

routes = {
    ...viewRoute,
    "GET /views/tools.unit_sale": envelope("tools.unit_sale", { ok: true,
        units: [{ GetIndex: 3, UniqueID: 30, CardID: 1, ClassID: 100, ClassChangeCount: 0, rating: { ok: true }, automatic: false,
            protections: ["prince", "locked"], barracks: 0, storage_supported: true },
            { GetIndex: 4, UniqueID: 40, CardID: 2, ClassID: 100, ClassChangeCount: 0, rating: { ok: true }, automatic: false,
                protections: [], barracks: 1, storage_supported: false }],
        quote: { ok: true, projected: true, persisted: false, note: "Projected", gold: 500, rainbow: 1,
            rainbow_item: "rainbow_coin", money: { before: 99999900, after: 99999999, discarded: 401 } } },
        SNAPSHOT_TAG),
};
const sale = await mountPage("tools", { tool: "unit_sale" });
await until(() => field(sale.root, "get_indexes") !== null);
const indexes = field(sale.root, "get_indexes");
const addIndex = find(indexes, (item) => item.localName === "button" && item.textContent === "添加一项");
addIndex.click();
addIndex.click();
all(indexes, (item) => item.localName === "input" && item.type === "text").forEach((item, index) => {
    item.value = String(3 + index);
});
action(sale.root, "run-tool").click();
await until(() => sale.root.textContent.includes("丢弃的金币"));
action(sale.root, "run-tool").click();
await until(() => requestsTo("/views/tools.unit_sale").length === 2);
await settle();
results.unitSale = { requests: requestsTo("/views/tools.unit_sale").map(({ params, etag }) => ({ params, etag })),
    text: sale.root.textContent, rows: rowsOf(find(sale.root, (item) => item.localName === "table")) };
finish("unitSale", sale);

let catalogCalls = 0;
routes = { "GET /views": () => (++catalogCalls === 1
    ? refusal(503, "generation_switching", "The server is switching generations") : viewRoute["GET /views"]()) };
const capacity = await mountPage("tools", { tool: "unit_capacity" });
await until(() => action(capacity.root, "retry") !== null);
const catalogFailure = capacity.root.textContent;
action(capacity.root, "retry").click();
await until(() => action(capacity.root, "run-tool") !== null);
results.toolCatalog = { failure: catalogFailure, calls: catalogCalls, button: action(capacity.root, "run-tool").textContent };
finish("toolCatalog", capacity);
routes = { "GET /views": () => reply(200, { views: fixtures.views.filter((entry) => entry.name !== "tools.unit_capacity") })() };
const unlisted = await mountPage("tools", { tool: "unit_capacity" });
await until(() => find(unlisted.root, (item) => item.className === "error") !== null);
results.toolCatalog.unlisted = { text: unlisted.root.textContent, button: action(unlisted.root, "run-tool") !== null };
finish("toolUnlisted", unlisted);

const secondBarracksUnit = { GetIndex: 4, UniqueID: 40, CardID: 2, ClassID: 100, ClassChangeCount: 0, barracks: 1,
    storage_supported: false };
const tradeQuote = { ok: true, projected: true, persisted: false, note: "Projected", shop_id: 7, period: "none",
    exchange_count: 0, limit: 1, window: { start: null, end: null }, unit_price: 10, total_price: 10, ownership_discount: 0,
    currency: { kind: "item", item_id: "silver", balance: 3 }, reward: { card_id: 9, item_id: null, amount: 1 } };
const toolView = (node, name) => find(node, (item) => item.dataset.view === name);
let tradeCalls = 0;
routes = {
    ...viewRoute,
    "GET /views/tools.silver_trade": () => {
        tradeCalls += 1;
        if (tradeCalls === 1) {
            return reply(422, { error: { code: "admin_params_invalid", message: "The request parameters are invalid",
                fields: [{ loc: ["shop_id"], type: "less_than_equal", msg: "Input should be at most 9" }] } })();
        }
        return envelope("tools.silver_trade", { ok: true, quote: tradeQuote, material: { ok: true, ...secondBarracksUnit },
            barracks: { ok: false, error: { code: "unit_roster_capacity", message: "The barracks are full" } } })();
    },
};
const trade = await mountPage("tools", { tool: "silver_trade" });
await until(() => field(trade.root, "shop_id") !== null);
input(field(trade.root, "shop_id")).value = "7";
specify(trade.root, "material_get_index", "4");
action(trade.root, "run-tool").click();
await until(() => field(trade.root, "shop_id").textContent.includes("Input should be at most 9"));
const fieldError = field(trade.root, "shop_id").textContent.includes("Input should be at most 9");
action(trade.root, "run-tool").click();
await until(() => trade.root.textContent.includes("unit_roster_capacity"));
results.silverTrade = { fieldError, text: trade.root.textContent,
    params: requestsTo("/views/tools.silver_trade").map((request) => request.params),
    pairs: pairsOf(toolView(trade.root, "tools.silver_trade")) };
finish("silverTrade", trade);

const storedMaterial = { source: "barracks", GetIndex: 5, UniqueID: 50, CardID: 3, ClassID: 100, ClassChangeCount: 0,
    barracks: 1, storage_supported: true };
const warehouseMaterial = { source: "silver_house", GetIndex: 0, UniqueID: 0, CardID: 9, ClassID: 100, ClassChangeCount: 0 };
routes = {
    ...viewRoute,
    "GET /views/tools.training": envelope("tools.training", { ok: true, target: secondBarracksUnit,
        materials: [storedMaterial, warehouseMaterial], protection: { ok: true },
        quote: { ok: true, projected: true, persisted: false, note: "Projected", price: 300, money: 5000,
            bonus: { percent: 0, sources: [] }, level: { before: 1, after: 2, cap: 50 },
            changes: { Exp: { before: 0, after: 100 } } } }),
};
const training = await mountPage("tools", { tool: "training" });
await until(() => field(training.root, "materials") !== null);
input(field(training.root, "target_get_index")).value = "4";
const chosenMaterials = field(training.root, "materials");
const addMaterial = find(chosenMaterials, (item) => item.localName === "button" && item.textContent === "添加一项");
addMaterial.click();
addMaterial.click();
input(field(chosenMaterials, "get_index")).value = "5";
const warehouse = all(chosenMaterials, (item) => item.localName === "select")[1];
warehouse.value = String(warehouse.childNodes.findIndex((option) => option.textContent === "silver_house"));
warehouse.dispatchEvent(new FakeEvent("change"));
input(field(chosenMaterials, "card_id")).value = "9";
action(training.root, "run-tool").click();
await until(() => toolView(training.root, "tools.training").textContent.includes("字段变化"));
const trainingView = toolView(training.root, "tools.training");
results.training = { params: requestsTo("/views/tools.training").map((request) => request.params),
    text: training.root.textContent, pairs: pairsOf(trainingView),
    materials: rowsOf(find(trainingView, (item) => item.localName === "table")) };
finish("training", training);

routes = {
    ...viewRoute,
    "GET /views/tools.evolution": envelope("tools.evolution", { ok: true,
        target: { ...secondBarracksUnit, GetIndex: 3, UniqueID: 30, barracks: 0, storage_supported: true },
        materials: [storedMaterial, warehouseMaterial], protection: { ok: true }, slots: { ok: true, slots: [] },
        quote: { ok: true, projected: true, persisted: false, note: "Projected", price: 1000, money: 5000, items: {},
            changes: { ClassID: { before: 100, after: 200 } }, material_assignment: [0, 1], reward_card_id: null } }),
};
const awakening = await mountPage("tools", { tool: "evolution" });
await until(() => field(awakening.root, "target_get_index") !== null);
input(field(awakening.root, "target_get_index")).value = "3";
action(awakening.root, "run-tool").click();
await until(() => toolView(awakening.root, "tools.evolution").textContent.includes("字段变化"));
const awakeningView = toolView(awakening.root, "tools.evolution");
results.evolution = { params: requestsTo("/views/tools.evolution").map((request) => request.params),
    text: awakening.root.textContent, pairs: pairsOf(awakeningView),
    materials: rowsOf(find(awakeningView, (item) => item.localName === "table")) };
finish("evolution", awakening);

routes = {
    ...viewRoute,
    "GET /views/tools.growth_shop": envelope("tools.growth_shop", { ok: true, quote: { ...tradeQuote, shop_id: 2000 },
        values: { ok: true, projected: true, persisted: false, note: "Projected",
            target: { ...secondBarracksUnit, GetIndex: 5, UniqueID: 50, barracks: 2 },
            changes: { ClassChangeCount: { before: 0, after: 1 } } } }),
};
const shop = await mountPage("tools", { tool: "growth_shop" });
await until(() => field(shop.root, "shop_id") !== null);
input(field(shop.root, "shop_id")).value = "2000";
specify(shop.root, "target_get_index", "5");
action(shop.root, "run-tool").click();
await until(() => toolView(shop.root, "tools.growth_shop").textContent.includes("ClassChangeCount"));
results.growthShop = { params: requestsTo("/views/tools.growth_shop").map((request) => request.params),
    text: shop.root.textContent, pairs: pairsOf(toolView(shop.root, "tools.growth_shop")) };
finish("growthShop", shop);

routes = {
    ...viewRoute,
    "GET /views/tools.fame": (request) => envelope("tools.fame", { ok: true, units: { items: [{ GetIndex: 3, UniqueID: 30,
        CardID: 1, ClassID: 100, ClassChangeCount: 0, eligibility: { ok: false, error: { code: "fame_unit_ineligible",
            message: "The unit cannot be registered" } } }], total: 150, offset: request.params.offset, limit: 100,
        next_offset: request.params.offset === 0 ? 100 : null } })(),
};
const fame = await mountPage("tools", { tool: "fame" });
await until(() => action(fame.root, "run-tool") !== null);
action(fame.root, "run-tool").click();
await until(() => find(fame.root, (item) => item.localName === "button" && item.textContent === "下一页") !== null);
find(fame.root, (item) => item.localName === "button" && item.textContent === "下一页").click();
await until(() => requestsTo("/views/tools.fame").length === 2 && fame.root.textContent.includes("第 101"));
results.fame = { params: requestsTo("/views/tools.fame").map((request) => request.params), text: fame.root.textContent };
finish("fame", fame);

const owned = (owner, read_only = false, reason = null) => ({ owner, read_only, reason });
routes = {
    ...commandRoute,
    "GET /views/settings.config": envelope("settings.config", { ok: true, fields: {
        UseBgm: { wire: "A1", type_code: "I", value: 1, ...owned("config.sound.set") },
        HaveUnitMaxSizeBuy: { wire: "B1", type_code: "I", value: 0, ...owned("barracks.grant_slot") },
        _BattleGodBlessFilter: { wire: "C1", type_code: "I", value: 3, ...owned("blessings.set") },
        UserName: { wire: "D1", type_code: "S", value: "Aigis",
            ...owned(null, true, "Player names have no shared native validator") },
    } }, SNAPSHOT_TAG),
    "GET /views/settings.event_view": envelope("settings.event_view", { ok: true, value: "CardId=5,Id=1,Mode=0,Male=1",
        request: { ok: true, fields: { CardId: 5, Id: 1, Mode: 0, Male: 1 } }, ...owned("view_event.set") }, SNAPSHOT_TAG),
    "GET /views/settings.flags": envelope("settings.flags", {
        flags: { ok: true, ...listing([{ ID: "flag_a", Value: 1, hash: 77 }]) },
        purchases: { ok: true, products: [{ flag: "skin_flag", stored: true, value: 1, shop_id: 4, item_id: "skin_1",
            supported: true, holding: 0, acquisition_recorded: false }] } }, SNAPSHOT_TAG),
    "GET /views/settings.perused": envelope("settings.perused", { events: { ok: true, ...listing([{ ID: "ev_1", FLG: 1 }]) } },
        SNAPSHOT_TAG),
    "GET /views/settings.backgrounds": envelope("settings.backgrounds", {
        rows: { ok: true, ...listing([{ CardID: 0, ConfigID: "bg_a", SaveTime: "2026/11/01 10:00:00" }]) },
        diagnostics: { ok: true, rows: 1, abnormal_rows: 1, kinds: { unknown_name: 1 },
            findings: [{ CardID: 0, ConfigID: "bg_a", kinds: ["unknown_name"] }], truncated: false } }, SNAPSHOT_TAG),
    "GET /views/settings.card_status": envelope("settings.card_status", {
        fields: { A1: owned(null, true, "The meaning is unconfirmed, so the stored value is only shown"),
            A7: owned("stories.set_unlock"), B0: owned("fame.register") },
        story_bits: [{ bit: 0, name: "first_episode", level: "verified" }],
        rows: { ok: true, ...listing([{ CardID: 1, A7: { value: 1, bits: [{ bit: 0, name: "first_episode", level: "verified",
            set: true }] }, B0: { value: 0, utc: null }, A1: { value: 2 } }]) } }, SNAPSHOT_TAG),
};
const commandsBefore = requestsTo("/commands").length;
const settings = await mountPage("settings");
await until(() => operations(settings.root).length >= fixtures.commands.filter((command) => command.domain === "settings").length
    && ["bg_a", "first_episode", "_BattleGodBlessFilter", "CardId=5"].every((text) => settings.root.textContent.includes(text)));
results.settings = {
    eventView: operations(card(settings.root, "活动剧情播放请求")),
    eventViewShown: find(card(settings.root, "活动剧情播放请求"), (item) => item.dataset.view === "settings.event_view") !== null,
    blessings: operations(card(settings.root, "加护")), blessingText: card(settings.root, "加护").textContent,
    config: operations(card(settings.root, "客户端设置")), rest: card(settings.root, "其他设置命令").textContent,
    storageLink: find(card(settings.root, "客户端设置"), (item) => item.dataset.link === "storage")?.getAttribute("href") ?? null,
    commandReads: requestsTo("/commands").length - commandsBefore, text: settings.root.textContent,
};
finish("settings", settings);

const reopened = { start_at: "2026-11-02T15:00:00+09:00", end_at: "2026-11-16T10:59:59+09:00" };
const windowsData = { ok: true, schema_version: 1, enabled: true, window_policy: "start_inclusive_end_exclusive_8CB420",
    rules_hash: "a".repeat(64), verification: { verified: true }, error: null,
    categories: [
        { category: 2, kind: "Subjugation", table: "SubjugationMissionConfig.atb",
            support: { 6: { enabled: true, mission_category: 2 } },
            batches: [{ key: "Subjugation:400183", category: 2, kind: "Subjugation", table: "SubjugationMissionConfig.atb",
                original: { start_at: null, end_at: "2022-12-01T23:59:59+09:00" },
                members: [{ mission_id: 400183, name: "legacy", quests: 1 }, { mission_id: 400212, name: "legacy 2", quests: 1 }],
                window: reopened, configured: true, state: "open", original_state: "ended" }] },
        { category: 13, kind: "Training", table: "TrainingMissionConfig.atb",
            support: { 15: { enabled: true, mission_category: 13 } },
            batches: [{ key: "Training:910001", category: 13, kind: "Training", table: "TrainingMissionConfig.atb",
                original: { start_at: null, end_at: null }, members: [{ mission_id: 910001, name: "練習", quests: 4 }],
                window: { start_at: null, end_at: null }, configured: false, state: "open", original_state: "open" }] },
    ],
    growth_campaigns: { table: "GrowthCampaignConfig.atb", window_policy: "unset_bound_or_open_le_clock_lt_close",
        clock: "2026-11-03T12:00:00.000+09:00",
        missions: [{ mission_id: 700101, rows: 3, quests: [7001], original: reopened, window: reopened,
            configured: false, state: "open", original_state: "open" }] },
    preset: { config_key: "mission-windows", tier: "content", max_bytes: 16384,
        document: { enabled: true, batches: { "Subjugation:400183": reopened } } },
};
routes = {
    ...commandRoute,
    "GET /views/battle.windows": envelope("battle.windows", windowsData),
    "GET /views/battle.journal": envelope("battle.journal", { journal: { ok: true, schema_version: 2, next_sequence: 3,
        processed_keys: 1, active: null, pending_presents: [], mode_progress: {}, history: [] } }),
    "GET /views/battle.records": envelope("battle.records", listing([{ QuestID: 910001, MaxRank: 3,
        flags: { cleared: true, three_star: true, four_star: false, episode: false, slots: [1, 5] },
        RemainAssault: 0, FirstDefeatDate: 0, Shown: true }]), SNAPSHOT_TAG),
    "GET /views/battle.record_archive": envelope("battle.record_archive", { archived: { ok: true, rows: [] },
        running_master: { ok: true, missing: [] }, candidate: null }),
    "GET /views/battle.entrust": envelope("battle.entrust", { status: { ok: true, status: { ready: true } },
        history: { ok: true, history: { schema_version: 1, entries: [] } } }),
    "GET /views/battle.entrust_availability": envelope("battle.entrust_availability", { ok: true, platform_code: 9,
        preview: false, types: { 0: { open: true } } }),
    "GET /views/towers.overview": envelope("towers.overview", { status: { ok: true, status: { ready: true } },
        state: { ok: true, floors: 10 } }),
    "GET /views/battle.subjugation": envelope("battle.subjugation", { subjugation: { ok: true, events: 1 } }),
    "GET /views/battle.subjugation_progress": envelope("battle.subjugation_progress", { progress: { ok: true,
        cleared: 0 } }),
    "POST /config/validations": (request) => reply(200, { job: { job_id: "job-w", state: "running", progress: [],
        candidate_id: null, error: null, elapsed_seconds: 0 },
        validation: { job_id: "job-w", op_id: request.body.op_id, config_set: { id: "9".repeat(16) },
            directory: "sets/9", base_set: null, keys: ["mission-windows"] } })(),
};
const battle = await mountPage("battle");
const presetArea = () => find(battle.root, (item) => item.dataset.preset === "mission-windows");
await until(() => presetArea() !== null && action(battle.root, "validate-windows") !== null
    && operations(battle.root).length > 0);
const presetText = presetArea().value;
action(battle.root, "validate-windows").click();
await until(() => requestsTo("/config/validations").length === 1 && battle.root.textContent.includes("job-w"));
await settle();
const [windowsValidation] = requestsTo("/config/validations");
results.battle = { text: battle.root.textContent, preset: JSON.parse(presetText),
    operations: operations(battle.root),
    validation: (({ op_id: ignored, ...body }) => body)(windowsValidation.body),
    validationOpId: windowsValidation.body.op_id,
    configLink: find(battle.root, (item) => item.dataset.link === "config")?.getAttribute("href") ?? null };
finish("battle", battle);
results.noticeInstants = { zero: view.formatInstant(new Date(0).toISOString()),
    expiry: view.formatInstant(new Date(1762138800000).toISOString()),
    cooking: view.formatInstant("2026-11-03T03:00:00.000000+00:00") };
results.clientActive = { hint: errorHint(fixtures.client_active.code),
    text: view.renderError(fixtures.client_active).textContent };

const ledger = (sections = {}) => ({ service: { name: "votes", available: true, content_version: "v1" },
    ledger: { keys: { ledger_key: true }, ok: true }, summary: { statuses: { pending: 1 }, reasons: { presents_full: 1 },
        definitions: {} },
    entries: listing([{ key: { event_id: 7 }, status: "pending", reason: "presents_full", definition: null,
        detail: { earned: 5, spent: 2, amount: 3, rate: 1000, gold: 3000 } }]),
    readback: { requests: [55], none_reasons: [] }, ...sections });
const sectionOf = (node, name) => find(node, (item) => item.dataset.section === name);
const notificationsOf = (node) => sectionOf(node, "notifications");
routes = { ...commandRoute, "GET /views/votes.assault": envelope("votes.assault", ledger({ notifications: { ok: true,
    records: { "7": { mission_id: 576, gold: 3000, mail_ids: [1], notified_at: 0 } } } }), SNAPSHOT_TAG) };
const votes = await mountPage("votes", { ledger: "assault" });
await until(() => votes.root.textContent.includes("presents_full") && operations(votes.root).length > 0);
results.votes = { reads: requestsTo("/views/votes.assault").map((request) => request.params), text: votes.root.textContent,
    notices: rowsOf(notificationsOf(votes.root)), operations: operations(votes.root) };
finish("votes", votes);
const cookingGrant = { item_id: "vote_ticket_b", group_id: 3, definition: "c".repeat(64), card_id: 9, count: 1,
    receive_text_id: 4, mail_ids: [3] };
const voteNotices = {
    votesExpiry: ["expiry", { ok: true, records: { vote_ticket_a: { gold: 2000, mail_ids: [2], notified_at: 1762138800000 } } }],
    votesCooking: ["cooking", { ok: true, records: { vote_ticket_b: { grant: cookingGrant,
        notified_at: "2026-11-03T03:00:00.000000+00:00", source: "e".repeat(64) } } }],
    votesExpiryEmpty: ["expiry", { ok: true, records: {} }],
    votesCookingRefused: ["cooking", { ok: false, error: { code: "vote_cooking_notifications_invalid",
        message: "Stored vote cooking notifications are invalid" }, records: null }],
};
results.voteNotices = {};
for (const [label, [name, notifications]] of Object.entries(voteNotices)) {
    routes = { ...commandRoute,
        [`GET /views/votes.${name}`]: envelope(`votes.${name}`, ledger({ notifications }), SNAPSHOT_TAG) };
    const page = await mountPage("votes", { ledger: name });
    await until(() => notificationsOf(page.root) !== null && operations(page.root).length > 0);
    results.voteNotices[label] = { rows: rowsOf(notificationsOf(page.root)), text: page.root.textContent };
    finish(label, page);
}
results.repairs = {};
for (const [name, sections] of Object.entries(fixtures.repairs)) {
    routes = { ...commandRoute,
        [`GET /views/events.${name}`]: envelope(`events.${name}`, ledger(sections), SNAPSHOT_TAG) };
    const page = await mountPage("events", { ledger: name });
    await until(() => Object.keys(sections).every((section) => sectionOf(page.root, section) !== null)
        && operations(page.root).length > 0);
    results.repairs[name] = Object.fromEntries(Object.keys(sections).map((section) => {
        const shown = sectionOf(page.root, section);
        return [section, { title: shown.childNodes[0].textContent, fields: pairsOf(shown).map(([field]) => field),
            rows: rowsOf(shown), text: shown.textContent }];
    }));
    finish(`${name}Repairs`, page);
}

const rulesConfig = {
    running: { digest: "c".repeat(64), set: "empty", known: true, reason: null, error: null },
    keys: [{ key: "quest-rules", domains: ["main_quests"], time_semantics: "frozen_per_operation", field_semantics: {},
        source: "default", read_only: false, shadowed: false, content_version: "cv-1", error: null, effective: { stamina: 1 },
        default: { stamina: 1 } }],
    versions: { masterdata: { in_use: null, disk: null, matches: null },
        resources: { base: null, effective: null, served: null, overlaid: null, matches: null },
        client_manifest: { path: "client/client-manifest.json", present: false, valid: null }, content: { quests: "v1" } } };
const boardSections = { catalog: { ok: true }, projection: { ok: false, error: { code: "sugoroku_dependencies_changed",
    message: "Reward board definitions changed after setup" } } };
routes = { ...commandRoute,
    "GET /views/events.event_login": envelope("events.event_login", ledger(), SNAPSHOT_TAG),
    "GET /views/events.sugoroku": envelope("events.sugoroku", ledger(boardSections)) };
const realTimeout = globalThis.setTimeout;
// Polls come due within milliseconds here, so a live view that kept polling would be read again during a quiet pause.
globalThis.setTimeout = (callback, milliseconds = 0, ...rest) =>
    realTimeout(callback, Math.min(milliseconds, FAST_POLL_MS), ...rest);
const quiet = () => new Promise((resolve) => realTimeout(resolve, QUIET_MS));
// Earlier flows already mounted these ledgers, so only the reads of this mount count.
const eventsStart = served.length;
const snapshot = await mountPage("events");
await until(() => requestsTo("/views/events.event_login", eventsStart).length >= SNAPSHOT_POLLS);
finish("events", snapshot);
const live = await mountPage("events", { ledger: "sugoroku" });
await until(() => requestsTo("/views/events.sugoroku", eventsStart).length === 1
    && live.root.textContent.includes("实时计算"));
await quiet();
action(live.root, "refresh-view").click();
await until(() => requestsTo("/views/events.sugoroku", eventsStart).length > 1);
await quiet();
results.events = { snapshot: requestsTo("/views/events.event_login", eventsStart).map((request) => request.etag),
    live: requestsTo("/views/events.sugoroku", eventsStart).map((request) => request.etag), liveText: live.root.textContent,
    tabs: all(live.root, (item) => (item.dataset.action ?? "").startsWith("tab-")).length,
    sections: Object.fromEntries(all(live.root, (item) => item.dataset.section !== undefined)
        .map((item) => [item.dataset.section, item.textContent])) };
finish("eventsLive", live);

let liveReads = 0;
routes = { "GET /views/rules.config": () => (++liveReads === 1
    ? refusal(503, "admin_view_service_unavailable", "The view needs a service this generation lacks")
    : envelope("rules.config", rulesConfig)()) };
const stalled = await mountPage("rules");
await until(() => action(stalled.root, "retry-view") !== null);
await quiet();
const stalledText = stalled.root.textContent;
const readsWhileStalled = liveReads;
action(stalled.root, "retry-view").click();
await until(() => stalled.root.textContent.includes("quest-rules"));
await quiet();
results.stalled = { text: stalledText, readsWhileStalled, reads: liveReads,
    retryLeft: action(stalled.root, "retry-view") !== null };
finish("stalled", stalled);
let snapshotReads = 0;
routes = { "GET /views/rules.config": () => (++snapshotReads === 2
    ? refusal(503, "generation_switching", "The server is switching generations")
    : envelope("rules.config", rulesConfig, SNAPSHOT_TAG)()) };
const polled = await mountPage("rules");
await until(() => snapshotReads > SNAPSHOT_POLLS);
results.polled = { reads: snapshotReads };
finish("polled", polled);
globalThis.setTimeout = realTimeout;

const loginViews = ({ rules, cooking, stamp_capacity }) => ({
    "GET /views/progress.login_rewards": envelope("progress.login_rewards", { ledger: { ok: true, ledger: { stamp_count: 1 },
        last_time_utc: "2026-11-03T03:00:00+00:00" }, pending_stamps: { ok: true, entries: [] }, rules, stamp_capacity },
        SNAPSHOT_TAG),
    "GET /views/progress.login_cooking": envelope("progress.login_cooking",
        { ok: true, ledger: { issued: 1 }, last_time_utc: null, rules: cooking }, SNAPSHOT_TAG),
});
routes = { ...commandRoute,
    "GET /views/items.holdings": envelope("items.holdings", { classes: { ok: true }, items: { ok: true, ...listing([{
        ItemID: "item_a", Count: 2, stack: 99, Enabled: 1, class: "passive", command: "possessions.credit" }]) } }, SNAPSHOT_TAG),
    "GET /views/items.catalog": envelope("items.catalog", { classes: { ok: true }, items: { ok: true, ...listing([{
        ItemID: "item_a", EffectType: 1, ReceiveAction: 0, held: 2, stack: 99, class: "passive", command: null }]) } },
        SNAPSHOT_TAG),
    "GET /views/presents.mailbox": envelope("presents.mailbox", { ok: true, count: 1, capacity: 1000, last_present_id: 5,
        notification_category: { field: "NotificationCategory", reason: "Only the client sets it" },
        mails: listing([{ PresentID: 5, Type: 1, kind: "gold", Content: 100, target: { amount: 100 }, Description: "",
            NotificationCategory: 0 }]) }, SNAPSHOT_TAG),
    "GET /views/presents.backlog": envelope("presents.backlog", { ok: true, copies: 0, read_only: true, reason: "Battle owned",
        entries: [] }, SNAPSHOT_TAG),
    "GET /views/presents.receipts": envelope("presents.receipts", { ok: true, schema_version: 1, limit: 16,
        receipts: listing([]) }, SNAPSHOT_TAG),
    "GET /views/progress.missions": envelope("progress.missions", { missions: { ok: true, projected: true, persisted: false,
        note: "Projected", as_of: NOW, ...listing([{ ID: 1, Status: 1, Read: 0, Progress: 3, period_type: 100, tab: 1,
            claimable: true, unread: true }]) }, history: null }),
    "GET /views/progress.rank_rewards": envelope("progress.rank_rewards", { ok: true, baseline_rank: 1, observed_rank: 5,
        definitions_checked: true, pending: [{ bonus_id: 2, rank: 3, definition: "a".repeat(64), status: "pending",
            mail_ids: [], reason: "presents_full", definition_current: true }], delivered: [] }, SNAPSHOT_TAG),
    ...loginViews(fixtures.login_rules.unchanged),
    "GET /views/rules.config": envelope("rules.config", rulesConfig),
};
const shown = { items: ["item_a", "possessions.credit"], presents: ["PresentID", "金币"], missions: ["可领取", "presents_full"],
    rules: ["quest-rules", "按操作冻结"] };
for (const [name, texts] of Object.entries(shown)) {
    const page = await mountPage(name);
    await until(() => texts.every((text) => page.root.textContent.includes(text)));
    finish(name, page);
}

const viewOf = (node, name) => find(node, (item) => item.dataset.view === name);
const warningsOf = (node) => all(node, (item) => item.classList.contains("notice-warn")).map((item) => item.textContent);
results.loginRules = {};
for (const [label, checks] of Object.entries(fixtures.login_rules)) {
    routes = { ...routes, ...loginViews(checks) };
    const page = await mountPage("missions");
    await until(() => fixtures.login_views.every((name) => pairsOf(viewOf(page.root, name)).length > 0));
    results.loginRules[label] = Object.fromEntries(fixtures.login_views.map((name) => {
        const panel = viewOf(page.root, name);
        return [name, { text: panel.textContent, warnings: warningsOf(panel), fields: pairsOf(panel).map(([field]) => field) }];
    }));
    finish(`${label}LoginRules`, page);
}

routes = { ...routes,
    "GET /views/presents.mailbox": envelope("presents.mailbox", { ok: false, error: { code: "present_state_invalid",
        message: "Saved present rows are invalid" }, bundled_tickets: { ok: true, ids: fixtures.bundled_presents } },
        SNAPSHOT_TAG),
    "POST /plans": () => reply(200, plan("presents.split_ticket_mail", "MIGRATE TICKET MAIL 7"))() };
const plansBeforeSplit = requestsTo("/plans").length;
const bundled = await mountPage("presents");
await until(() => action(bundled.root, "split-bundled") !== null && operations(bundled.root).length > 0);
const bundledText = bundled.root.textContent;
const splitButton = action(bundled.root, "split-bundled").textContent;
action(bundled.root, "split-bundled").click();
await until(() => requestsTo("/plans").length > plansBeforeSplit && bundled.root.textContent.includes("检查已通过"));
results.bundledTickets = { text: bundledText, button: splitButton, plans: requestsTo("/plans").slice(plansBeforeSplit)
    .map((request) => request.body), previewed: bundled.root.textContent.includes("检查已通过") };
finish("bundledTickets", bundled);

routes = { "GET /views/rules.config": () => reply(503, { error: { code: "admin_view_service_unavailable",
    message: "The view needs a service this generation lacks" } })() };
const failing = await mountPage("rules");
await until(() => failing.root.textContent.includes("当前世代缺少这个视图依赖的服务"));
results.rulesFailure = failing.root.textContent;
finish("rulesFailure", failing);

const quest = { key: "quest-rules", cli_flag: "--quest-rules", ps_parameter: "QuestRulesFile", domains: ["main_quests"],
    time_semantics: "frozen_per_operation", field_semantics: {}, resource_hint: false, examples: [], source: "default",
    mode: "default", path: null, sha256: null, shadowed: false, read_only: false, effective: { stamina: 1 },
    content_version: "cv-1", error: null, default: { stamina: 1 } };
const masterdata = { in_use: "snap-0", disk: { snapshot: "snap-1", manifest_sha256: "e".repeat(64), digest: "f".repeat(64) },
    error: null, roll_forward_pending: false, next_start: "snap-1", staged: "snap-1" };
const snapshotEntry = (snapshot, flags = {}) => ({ snapshot, valid: true, error: null, imported_at: NOW.utc,
    tables: { cards: { rows: 1, columns: 2, full_schema: true, bytes: 10, sha256: "a".repeat(64) } }, in_use: false,
    disk: false, next_start: false, ...flags });
const configRead = () => reply(200, {
    running: { set: "empty", generation_id: "gen-b", config: { keys: [quest], overridden: [] } },
    current_generation_id: "gen-b", active: { digest: null, active: null, set: "empty", error: null,
        roll_forward_pending: false, next_start: "empty" },
    staged: null, history: { items: [], next_cursor: null }, validation: null, client_warning: null, switching: false,
    masterdata })();
routes = {
    "GET /config": configRead,
    "GET /content": () => reply(200, { masterdata, side_by_side: { in_use: snapshotEntry("snap-0", { in_use: true }),
        disk: snapshotEntry("snap-1", { disk: true }) },
        snapshots: listing([snapshotEntry("snap-0", { in_use: true }),
            snapshotEntry("snap-1", { disk: true, next_start: true })]) })(),
    "GET /content/captures": () => reply(200, { root: "captures", ...listing([{ name: "20261103-120000", importable: true,
        error: null, snapshot: "snap-2", imported: false, started_utc: NOW.utc, finished_utc: NOW.utc, tables: {} }]) })(),
    "POST /content/imports": () => reply(200, { capture: "20261103-120000", snapshot: "snap-2", imported_at: NOW.utc,
        tables: {}, already_present: false, activated: false, masterdata })(),
    "POST /content/validations": () => reply(409, { error: { code: "config_switch_busy",
        message: "Another configuration switch is running" } })(),
    "POST /content/unstage": (request) => reply(200, { op_id: request.body.op_id, kind: "config", operation: "config.unstage",
        mode: "stage", tier: "unstaged", staged_master: "snap-1", revision_before: 12, revision_after: 12, readback: {},
        replayed: false })(),
};
const config = await mountPage("config");
await until(() => action(config.root, "unstage-master") !== null && action(config.root, "validate") !== null);
const contentReadsBeforeOpen = served.filter((request) => request.path.startsWith("/content")).length;
const contentPanel = find(config.root, (item) => item.dataset.panel === "content");
contentPanel.open = true;
contentPanel.dispatchEvent(new FakeEvent("toggle"));
await until(() => action(config.root, "import-capture") !== null && action(config.root, "choose-master") !== null);
action(config.root, "import-capture").click();
await until(() => config.root.textContent.includes("校验时同时把下次使用的主表快照换成"));
action(config.root, "validate").click();
await until(() => requestsTo("/content/validations").length === 1 && config.root.textContent.includes("config_switch_busy"));
action(config.root, "unstage-master").click();
await until(() => requestsTo("/content/unstage").length === 1 && config.root.textContent.includes("撤销的主表暂存"));
await settle();
const [validation] = requestsTo("/content/validations");
const [unstaged] = requestsTo("/content/unstage");
results.configContent = { contentReadsBeforeOpen, imports: requestsTo("/content/imports").map((request) => request.body),
    validation: (({ op_id: ignored, ...body }) => body)(validation.body), validationOpId: validation.body.op_id,
    unstage: (({ op_id: ignored, ...body }) => body)(unstaged.body), unstageOpId: unstaged.body.op_id,
    contentReads: requestsTo("/content").length };
finish("config", config);

const oversized = { code: "admin_audit_too_large",
    message: "The audit record exceeds the size the store keeps, so nothing was committed",
    details: { fields: { diff: 2097152 }, limit: 1048576 } };
const cookingValidation = { job_id: "job-9", op_id: "v-9", config_set: { id: "7".repeat(16) }, directory: "sets/7",
    base_set: null, keys: ["login-rewards"] };
const border = { before: 3000, after: 5000 };
const cookingReport = {
    report_id: "k".repeat(64), job_id: "job-9", revision: 12, running_set: "empty", validation: cookingValidation, staged: null,
    created_at: NOW,
    generations: { current: { id: "gen-b", identity: {} }, candidate: { id: "gen-c", identity: {}, gate_issues: [] } },
    tier: "gated", identity_changes: [], reasons: { identity: [], accumulating: [], initialize: false, cooking: true }, keys: [],
    migration: { changed: true, energy: [], cooking: { period: "2026-11", issued: 1, border, quota: { before: 4, after: 2 },
        migrated_at: NOW },
        diff: { entries: [{ op: "replace", path: "/login_cooking", field: "border", before: 3000, after: 5000 }], total: 1,
            truncated: false, omitted: 0 } },
    checks: { serve_gate: { ok: true }, audit: { ok: true } }, battle: { in_progress: false, required: false, error: null },
    default_mode: "apply",
    modes: { apply: { allowed: true, blockers: [], confirmations: [] },
        stage: { allowed: false, confirmations: [], blockers: [{ code: "config_stage_unsupported",
            message: "Cooking border changes switch only while the server runs", details: { border } }] } },
    requirements: { backup: true, fence: true, client_closed: false }, overridden: [], content_versions: {},
    receipts_invalidated: [], availability: { regressions: [] }, client_actions: null,
    client: { quiet_seconds: 120, idle_seconds: 30, quiet: false, session_observed_since_switch: null, restart_required: false,
        notices: [] },
};
const reportJob = (validation, report) => ({ job: { job_id: validation.job_id, state: "succeeded", progress: ["built"],
    candidate_id: "gen-c", error: null, elapsed_seconds: 1 }, validation, report, candidate_expired: false });
const switchStates = (node) => ["switch-apply", "switch-stage"].map((name) => action(node, name)?.disabled ?? null);

async function validateReport(job, extra = {}) {
    const poll = `/jobs/${job.job.job_id}`;
    routes = { "GET /config": configRead, "POST /config/validations": () => reply(200, job)(),
        [`GET ${poll}`]: () => reply(200, job)(), ...extra };
    const page = await mountPage("config");
    await until(() => find(page.root, (item) => item.localName === "select") !== null);
    const editMode = find(page.root, (item) => item.localName === "select");
    editMode.value = "custom";
    editMode.dispatchEvent(new FakeEvent("change"));
    find(page.root, (item) => item.localName === "textarea").value = '{"stamina": 2}';
    action(page.root, "validate").click();
    await until(() => action(page.root, "switch-apply") !== null && requestsTo(poll).length === 1);
    await settle();
    return page;
}

const cooking = await validateReport(reportJob(cookingValidation, cookingReport), {
    "POST /config/switches": () => refusal(409, "config_report_stale",
        "The running generation, a pointer or the saved state changed; validate again"),
});
results.cookingReport = { text: cooking.root.textContent,
    migration: find(cooking.root, (item) => item.dataset.migration === "cooking").textContent,
    stageDisabled: action(cooking.root, "switch-stage").disabled, applyDisabled: action(cooking.root, "switch-apply").disabled };
action(cooking.root, "switch-apply").click();
await until(() => requestsTo("/config/switches").length === 1 && cooking.root.textContent.includes("config_report_stale"));
await settle();
results.cookingReport.stale = { text: cooking.root.textContent, switches: switchStates(cooking.root) };
finish("cookingReport", cooking);

const towerValidation = { ...cookingValidation, job_id: "job-10", op_id: "v-10", config_set: { id: "8".repeat(16) },
    directory: "sets/8", keys: ["tower-rules"] };
const towerReport = { ...cookingReport, report_id: "t".repeat(64), job_id: "job-10", validation: towerValidation,
    reasons: { identity: [], accumulating: ["tower-rules.generation_hour"], initialize: false, cooking: false },
    migration: { changed: true, cooking: null,
        energy: [{ key: "tower-rules", field: "generation_hour", before: 4, after: 5, settled_at: NOW }],
        diff: { entries: [{ op: "replace", path: "/tower_state", field: "settled_day", before: 1, after: 2 }], total: 1,
            truncated: false, omitted: 0 } },
    checks: { serve_gate: { ok: true }, audit: { ok: false, error: oversized } },
    modes: { apply: { allowed: false, blockers: [oversized], confirmations: [] },
        stage: { allowed: false, confirmations: [], blockers: [{ code: "config_stage_unsupported",
            message: "Time-accumulating rule changes switch only while the server runs" }, oversized] } } };
const tower = await validateReport(reportJob(towerValidation, towerReport));
const auditCheck = find(tower.root, (item) => item.dataset.check === "audit");
results.towerReport = { text: tower.root.textContent, check: auditCheck.className,
    head: find(auditCheck, (item) => item.className === "check-head").textContent, switches: switchStates(tower.root) };
finish("towerReport", tower);

const beginnerValidation = { ...cookingValidation, job_id: "job-11", op_id: "v-11", config_set: { id: "6".repeat(16) },
    directory: "sets/6" };
const beginnerTotal = { before: 7, after: 14 };
const beginnerReport = { ...cookingReport, report_id: "b".repeat(64), job_id: "job-11", validation: beginnerValidation,
    reasons: { identity: [], accumulating: [], initialize: false, cooking: false, beginner: true },
    migration: { changed: true, energy: [], cooking: null, beginner: { total: beginnerTotal, claimed: { before: 3, after: 3 } },
        diff: { entries: [{ op: "replace", path: "/login_rewards", field: "beginner_total", before: 7, after: 14 }],
            total: 1, truncated: false, omitted: 0 } },
    modes: { apply: { allowed: true, blockers: [], confirmations: [] },
        stage: { allowed: false, confirmations: [], blockers: [{ code: "config_stage_unsupported",
            message: "Beginner total changes switch only while the server runs", details: { total: beginnerTotal } }] } } };
const beginner = await validateReport(reportJob(beginnerValidation, beginnerReport));
results.beginnerReport = { text: beginner.root.textContent,
    migration: find(beginner.root, (item) => item.dataset.migration === "beginner").textContent,
    switches: switchStates(beginner.root) };
finish("beginnerReport", beginner);

const contentValidation = { ...cookingValidation, job_id: "job-12", op_id: "v-12", config_set: { id: "5".repeat(16) },
    directory: "sets/5", keys: [], master_snapshot: "snap-2" };
const contentReport = { ...cookingReport, report_id: "m".repeat(64), job_id: "job-12", validation: contentValidation,
    tier: "content", default_mode: "stage", identity_changes: ["master_snapshot"],
    reasons: { identity: ["master_snapshot"], accumulating: [], initialize: false, cooking: false, beginner: false },
    migration: { changed: false, energy: [], cooking: null, beginner: null,
        diff: { entries: [], total: 0, truncated: false, omitted: 0 } },
    modes: { apply: { allowed: true, blockers: [], confirmations: [] },
        stage: { allowed: true, blockers: [], confirmations: [] } },
    requirements: { backup: false, fence: true, client_closed: true },
    client: { ...cookingReport.client, quiet_seconds: fixtures.quiet_seconds, quiet: true },
    notices: [fixtures.tower_notice] };
const content = await validateReport(reportJob(contentValidation, contentReport));
results.contentReport = { text: content.root.textContent, switches: switchStates(content.root),
    apply: find(content.root, (item) => item.dataset.mode === "apply").textContent };
finish("contentReport", content);

const presentsForm = forms.schemaForm(viewSchemas["tools.presents"]);
document.body.append(presentsForm.element);
const rewards = field(presentsForm.element, "rewards");
find(rewards, (item) => item.localName === "button" && item.textContent === "添加一项").click();
const kind = find(rewards, (item) => item.localName === "select");
const kinds = kind.childNodes.map((option) => option.textContent);
kind.value = String(kinds.indexOf("gold"));
kind.dispatchEvent(new FakeEvent("change"));
input(field(rewards, "amount")).value = "50";
const presentsRead = presentsForm.read();
presentsForm.showErrors([{ path: ["rewards", 0, "gold", "amount"], message: "too much gold" }]);
results.unionForm = { kinds, read: presentsRead, amountError: field(rewards, "amount").textContent };

const evolutionForm = forms.schemaForm(viewSchemas["tools.evolution"]);
input(field(evolutionForm.element, "target_get_index")).value = "3";
const materials = field(evolutionForm.element, "materials");
find(materials, (item) => item.type === "checkbox").checked = true;
find(materials, (item) => item.localName === "button" && item.textContent === "添加一项").click();
const source = find(materials, (item) => item.localName === "select");
const sources = source.childNodes.map((option) => option.textContent);
source.value = String(sources.indexOf("silver_house"));
source.dispatchEvent(new FakeEvent("change"));
input(field(materials, "card_id")).value = "9";
results.evolutionForm = { sources, read: evolutionForm.read() };

const clockForm = forms.schemaForm(viewSchemas["tools.energy_clock"]);
const at = field(clockForm.element, "at");
const [include, empty] = all(at, (item) => item.type === "checkbox");
include.checked = true;
empty.checked = false;
input(at).value = "2026-11-03 12:00";
const missingZone = clockForm.read();
input(at).value = "2026-11-03T12:00:00+09:00";
results.clockForm = { missingZone, zoned: clockForm.read() };

const growthForm = forms.schemaForm(viewSchemas["tools.growth_shop"]);
input(field(growthForm.element, "shop_id")).value = "2000";
const rewardsForm = forms.schemaForm(viewSchemas["tools.presents"]);
results.writeBack = { optional: growthForm.setValue("target_get_index", 5), missing: growthForm.setValue("missing", 1),
    union: rewardsForm.setValue("rewards", [{ kind: "gold", amount: 7 }]) };
growthForm.showErrors([{ path: ["shop_id"], message: "unknown shop" }]);
const shopError = () => field(growthForm.element, "shop_id").textContent.includes("unknown shop");
const marked = shopError();
growthForm.setValue("shop_id", 2001);
Object.assign(results.writeBack, { marked, cleared: !shopError(), growth: growthForm.read(), rewards: rewardsForm.read() });

const newest = kit.newestRead();
const raced = view.h("div");
let releaseFirst = null;
const overtaken = newest(() => new Promise((resolve) => { releaseFirst = resolve; }));
const firstShown = view.present(raced, overtaken, (value) => view.h("p", {}, value));
await view.present(raced, newest(async () => "second"), (value) => view.h("p", {}, value));
releaseFirst("first");
await firstShown;
results.newest = raced.textContent;

let readinessReads = 0;
routes = {
    "GET /session": () => reply(200, { actor: "admin-console", admin: { console: "http://127.0.0.1:18104/admin/",
        game_port: 18103 }, server_now: NOW, player_id: "1", revision: 12, generation_id: "gen-b", checks: {}, client: null })(),
    "GET /runtime": () => reply(200, { runtime: { current: { id: "gen-b", leases: 0 }, draining: null, fenced: false, build: null,
        candidate: null, switched_seconds_ago: null }, identity: { content_versions: {} }, gate_issues: [],
        readiness_domains: ["bootstrap", "quests"] })(),
    "GET /requests": () => reply(200, { requests: [] })(),
    "GET /views/runtime.versions": envelope("runtime.versions", { client_manifest: { path: "m", present: false },
        masterdata: {} }),
    "GET /views/runtime.operations": envelope("runtime.operations", { registered_operations: 1, configured_handlers: [],
        unimplemented: [] }),
    "GET /views/runtime.readiness": () => envelope("runtime.readiness", { domains: { bootstrap: { ready: true },
        quests: { ready: ++readinessReads > 1 } } })(),
    "GET /readiness/bootstrap": () => reply(200, { domain: "bootstrap",
        readiness: { ready: true, reads: requestsTo("/readiness/bootstrap").length } })(),
};
const status = await mountPage("status");
const domainNode = (name) => find(status.root, (item) => item.dataset.domain === name);
const stateOf = (name) => {
    const node = domainNode(name);
    return node === null ? null : find(node, (item) => item.className === "readiness-state").textContent;
};
await until(() => stateOf("quests") === "不可用" && status.root.textContent.includes("尚未准备客户端"));
const opened = domainNode("bootstrap");
opened.open = true;
opened.dispatchEvent(new FakeEvent("toggle"));
await until(() => opened.textContent.includes('"reads": 1'));
const statusPaths = ["/session", "/runtime", "/requests", "/views/runtime.versions", "/views/runtime.operations"];
const statusReads = () => Object.fromEntries(statusPaths.map((path) => [path, requestsTo(path).length]));
const beforeRefresh = statusReads();
action(status.root, "refresh-readiness").click();
await until(() => stateOf("quests") === "就绪" && opened.textContent.includes('"reads": 2'));
results.readiness = { before: beforeRefresh, after: statusReads(), readinessReads, open: opened.open,
    details: requestsTo("/readiness/bootstrap").length, unopened: requestsTo("/readiness/quests").length,
    text: status.root.textContent };
finish("status", status);

const refusedPlan = (fields) => ({ ...plan("wallet.credit", "CREDIT 1"), plan_id: null, committable: false, confirmations: [],
    ...fields });
routes = { "POST /plans": () => reply(200, refusedPlan({ audit: { ok: false, error: oversized } }))() };
const auditFlow = flow.planFlow({ commands: [{ operation: "wallet.credit", params: { amount: 1 } }],
    ids: new flow.OperationIds() });
document.body.append(auditFlow.element);
await auditFlow.preview();
results.auditPlan = { text: auditFlow.element.textContent, commitDisabled: action(auditFlow.element, "commit").disabled,
    check: find(auditFlow.element, (item) => item.dataset.check === "audit")?.className ?? null };
routes = { "POST /plans": () => reply(200, refusedPlan({ audit: { ok: false, skipped: "preflight" }, preflight: { ok: false,
    error: { code: "admin_preflight_failed", message: "The client would fail to read this state" } } }))() };
await auditFlow.preview();
results.auditPlan.skipped = auditFlow.element.textContent;
auditFlow.element.remove();

const backupRow = { id: 3, kind: "manual", reason: "before test", op_id: null, path: "backups/admin-manual-3.sqlite3",
    sha256: "e".repeat(64), revision: 9, created_at: NOW.utc };
const restoreChecks = { energy_clock: { ok: true }, battle: { ok: true }, restart_check: { ok: true },
    preflight: { ok: true, readback: {} }, audit: { ok: true } };
const restorePlan = (fields) => ({ restore_digest: "9".repeat(64), expected_revision: 12, generation_id: "gen-b",
    player_id: "1", backup: { path: backupRow.path, sha256: backupRow.sha256, revision: 9, player_id: "1" },
    cross_account: false, state_sha256_before: "1".repeat(64), state_sha256_after: "2".repeat(64), auto_backup: true,
    confirmations: [{ step: 0, operation: "admin.restore", phrase: "RESTORE 12" }],
    diff: { entries: [], total: 0, truncated: false, omitted: 0 }, checks: restoreChecks,
    client: { quiet_seconds: 120, idle_seconds: 300, quiet: true, session_observed_since_switch: null, restart_required: false,
        notices: [] },
    committable: true, ...fields });
let previewed = restorePlan({ committable: false, checks: { ...restoreChecks, audit: { ok: false, error: oversized } } });
routes = {
    "GET /backups": () => reply(200, { items: [backupRow], next_cursor: null,
        retention: { auto_keep: 1, auto_lifetime_seconds: 86400, manual_pruned: false } })(),
    "POST /restores/preview": () => reply(200, previewed)(),
    "POST /restores": (request) => reply(200, { op_id: request.body.op_id, kind: "restore", revision_before: 12,
        revision_after: 12, readback: {}, backup_before_restore: null, client_restart_required: false, replayed: false })(),
};
const backups = await mountPage("backups");
await until(() => action(backups.root, "preview-restore") !== null);
action(backups.root, "preview-restore").click();
await until(() => backups.root.textContent.includes("admin_audit_too_large"));
const blockedRestore = { text: backups.root.textContent, disabled: action(backups.root, "restore").disabled };
previewed = restorePlan({ auto_backup: false, state_sha256_after: "1".repeat(64) });
action(backups.root, "preview-restore").click();
await until(() => action(backups.root, "restore")?.disabled === false);
const sameState = backups.root.textContent;
action(backups.root, "restore").click();
await settle();
answer(["RESTORE 12"]);
await until(() => requestsTo("/restores").length === 1 && requestsTo("/backups").length === 2);
await settle();
results.restore = { blocked: blockedRestore, sameState, restored: backups.root.textContent,
    body: (({ op_id: ignored, ...body }) => body)(requestsTo("/restores")[0].body) };
finish("backups", backups);

results.unexpected = unexpected;
console.log(JSON.stringify(results));
process.exit(0);
'''


@pytest.fixture(scope="module")
def pages_run(tmp_path_factory, catalog):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not available to run the console modules")
    workspace = tmp_path_factory.mktemp("console-pages")
    copy = workspace / "ui"
    shutil.copytree(UI_DIRECTORY, copy, ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "package.json").write_bytes(b'{"type": "module"}\n')
    fixtures = workspace / "fixtures.json"
    fixtures.write_bytes(json.dumps({
        "commands": [{**command.describe(), "available": True, "unavailable_reason": None} for command in catalog.commands],
        "field_owners": dict(catalog.commands.field_owners()),
        "views": catalog.views.describe(),
        "calendar_marker": dict(MARKER),
        "pages": [page["name"] for page in ui_pages(UI_DIRECTORY)],
        "quiet_seconds": CLIENT_QUIET_SECONDS,
        "tower_notice": TOWER_NOTICE,
        "repairs": repair_sections(),
        "login_rules": LOGIN_RULES,
        "login_views": list(LOGIN_CHECKS),
        "bundled_presents": list(BUNDLED_PRESENTS),
        "client_active": client_active_refusal(),
    }).encode("utf-8"))
    harness = workspace / "pages.mjs"
    harness.write_bytes((FAKE_DOM + PAGE_FLOWS).encode("utf-8"))
    completed = subprocess.run([node, str(harness), str(copy), str(fixtures)], capture_output=True, timeout=NODE_TIMEOUT,
                               cwd=workspace)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    return json.loads(completed.stdout.decode("utf-8").strip().splitlines()[-1])


def test_domain_pages_are_listed_with_distinct_orders_and_titles(pages_run):
    pages = pages_run["pages"]
    assert DOMAIN_PAGES <= set(pages)
    orders = [page["order"] for page in pages.values()]
    titles = [page["title"] for page in pages.values()]
    assert len(set(orders)) == len(orders) and len(set(titles)) == len(titles)
    for name in DOMAIN_PAGES:
        assert pages[name]["frozen"] is True and pages[name]["order"] not in RESERVED_ORDERS, name


def test_every_command_domain_opens_on_a_page(pages_run, catalog):
    command_pages = pages_run["mirrors"]["commandPages"]
    assert {command.domain for command in catalog.commands} <= set(command_pages)
    assert set(command_pages.values()) <= {page["name"] for page in ui_pages(UI_DIRECTORY)}


def test_every_domain_view_is_read_by_a_page(catalog):
    referenced = {name for path in scripts() for name in VIEW_CONSTANT.findall(text_of(path))}
    views = {view.name for view in catalog.views if view.domain in VIEW_DOMAINS}
    assert views and views <= referenced, sorted(views - referenced)


def test_console_mirrors_match_the_server_constants(pages_run):
    mirrors = pages_run["mirrors"]
    assert mirrors["goldLimit"] == GOLD_LIMIT and mirrors["pageLimit"] == DEFAULT_PAGE_ITEMS
    assert mirrors["crystalLimits"] == dict(CRYSTAL_LIMITS)
    assert (mirrors["planCapacity"], mirrors["planTtl"]) == (PLAN_CAPACITY, PLAN_TTL_SECONDS)
    assert (mirrors["candidateTtl"], mirrors["admitTimeout"]) == (CANDIDATE_TTL_SECONDS, ADMIT_TIMEOUT_SECONDS)
    assert (mirrors["auditFieldBytes"], mirrors["autoBackupKeep"]) == (MAX_AUDIT_FIELD_BYTES, AUTO_BACKUP_KEEP)
    kept = {count for path in scripts() for count in KEPT_BACKUPS.findall(text_of(path))}
    assert kept <= {MIRRORED_KEEP, SERVED_KEEP}, kept
    assert all(MIRRORED_KEEP in KEPT_BACKUPS.findall(text_of(PAGES / page)) for page in BACKUP_NOTE_PAGES)
    # Only the MAX_REASON declaration may spell the backup reason limit.
    assert len(SPELLED_REASON_LIMIT.findall(text_of(PAGES / "backups.js"))) == 1
    assert f"JST {BUSINESS_DAY_START_HOUR:02d}:00" in string_constant(PAGES / "account.js", "CALENDAR_MARKED")
    assert set(mirrors["protections"]) == {str(item) for item in Protection}
    assert set(mirrors["anchors"]) == set(PREFIXES)
    assert set(mirrors["definitions"]) == {str(state) for state in DefinitionState}
    assert {str(status) for status in EntryStatus} <= set(mirrors["statuses"])
    assert string_constant(UI_DIRECTORY / "view-kit.js", "UNKNOWN_BIT") == BitLevel.UNKNOWN
    assert string_constant(UI_DIRECTORY / "plan-flow.js", "AUDIT_TOO_LARGE") == AUDIT_TOO_LARGE
    assert string_constant(UI_DIRECTORY / "plan-flow.js", "AUDIT_SKIPPED_BY") == AUDIT_SKIPPED["skipped"]
    assert QUOTED.findall(declared(PAGES / "items.js", "ITEM_CLASSES")) == [str(item) for item in ItemClass]
    assert string_constant(PAGES / "units.js", "OVERRIDES_REMOVAL") == REMOVE_OVERRIDES.operation
    assert set(LABEL_KEY.findall(declared(PAGES / "units.js", "SHAPE_LABELS"))) == {ABSENT, EMPTY, ENTRIES, INVALID}
    assert set(LABEL_KEY.findall(declared(PAGES / "units.js", "GUARD_LABELS"))) == set(OVERRIDE_GUARDS)
    assert set(TOP_KEY.findall(declared(PAGES / "settings.js", "AUDIT_LABELS"))) == set(AUDIT_KINDS)
    assert string_constant(PAGES / "settings.js", "BLESSING_FIELD") == FILTER_FIELD
    assert set(LABEL_KEY.findall(declared(PAGES / "presents.js", "KIND_LABELS"))) == set(TYPE_KINDS.values())
    assert string_constant(PAGES / "presents.js", "SPLIT_OPERATION") == SPLIT_TICKET_MAIL.operation
    assert string_constant(PAGES / "config.js", "TOWER_PRODUCT_REUSED") == TOWER_PRODUCT_REUSED


def test_console_notes_name_only_registered_commands_and_views(catalog):
    operations = {command.operation for command in catalog.commands}
    known = operations | {view.name for view in catalog.views}
    for path in scripts():
        for note in NOTE_CONSTANT.findall(text_of(path)):
            unknown = named_commands(note) - known
            assert not unknown, (path.name, sorted(unknown))
    repairs = {view.repair.command for view in LEDGERS if view.repair is not None}
    named = named_commands(declared(PAGES / "events.js", "NOTES"))
    assert repairs | {REBASE_OPERATION, COOKING_MIGRATION, REBIND_OPERATION, PASS_REBIND_OPERATION} <= named <= operations


def test_tool_forms_come_from_the_view_catalog_and_wait_for_it(pages_run):
    tools = pages_run["toolCatalog"]
    assert "generation_switching" in tools["failure"] and tools["calls"] == 2 and tools["button"] == "读取"
    unlisted = tools["unlisted"]
    assert "admin_view_unknown" in unlisted["text"] and "服务端没有提供这个视图" in unlisted["text"]
    assert unlisted["button"] is False


def test_settings_placements_name_settings_commands(catalog):
    placed = {name: QUOTED.findall(body) for name, body in PLACEMENT.findall(declared(PAGES / "settings.js", "PLACEMENTS"))}
    operations = [operation for names in placed.values() for operation in names]
    assert len(operations) == len(set(operations)) and set(operations) <= set(operations_of(catalog, SETTINGS))
    assert placed["eventView"] == ["view_event.set", "view_event.clear"] and placed["blessings"] == ["blessings.set"]


def test_the_overrides_removal_previews_through_the_plan_flow(pages_run):
    units = pages_run["units"]
    assert units["plan"] == {"commands": [{"operation": REMOVE_OVERRIDES.operation, "params": {}}]}
    assert "有条目时合成 5、银兑换 106、商店成长 113 回 501。" in units["text"]
    assert OVERRIDES_PHRASE in units["overrides"] and "请先手动备份" in units["overrides"]
    assert REMOVE_OVERRIDES.operation not in units["operations"] and "units.sell" in units["operations"]
    assert units["stateLink"] == "#/state?pointer=%2Funit_dynamic_overrides"


def test_the_gold_form_follows_the_newest_balance_from_the_shared_summary_read(pages_run):
    currencies = pages_run["currencies"]
    assert currencies["openedWith"] == "6000" and currencies["refreshedTo"] == "7000" and currencies["summaryReads"] == 3
    assert "按参数确认" in currencies["goldText"] and "取决于参数" in currencies["goldText"]
    assert "破坏性" in currencies["debitText"] and "按参数确认" not in currencies["debitText"]
    limits = {name: f"{limit:,}" for name, limit in CRYSTAL_LIMITS.items()}
    assert f"有偿结晶最多 {limits['paid']}、合计最多 {limits['total']}；免费结晶已超过 {limits['free']} 时" in currencies["text"]


def test_the_account_summary_reports_the_business_calendar_marker(pages_run):
    calendars = pages_run["calendars"]
    marked = string_constant(PAGES / "account.js", "CALENDAR_MARKED")
    legacy = string_constant(PAGES / "account.js", "CALENDAR_LEGACY")
    assert marked in calendars["account"] and MARKER["origin"] in calendars["account"] and legacy not in calendars["account"]
    assert legacy in calendars["legacyCalendar"] and marked not in calendars["legacyCalendar"]
    invalid = calendars["invalidCalendar"]
    assert "business_calendar_invalid" in invalid and marked not in invalid and legacy not in invalid


def test_the_storage_page_offers_every_storage_command(pages_run, catalog):
    storage = pages_run["storage"]
    assert sorted(storage["operations"]) == operations_of(catalog, STORAGE) and storage["fameBadge"] is True
    assert "名声寄存" in storage["text"]
    for text in ("第一兵营可用格数", "card_id", "level", "count", "147"):
        assert text in storage["barracks"], text
    assert pages_run["leaks"]["storage"] is False


def test_tool_previews_never_send_a_cache_validator(pages_run):
    sale = pages_run["unitSale"]
    assert sale["requests"] == [{"params": {"get_indexes": [3, 4]}, "etag": None}] * 2
    for text in ("王子", "已锁定", "丢弃的金币", "99,999,999", "401"):
        assert text in sale["text"], text


def test_unit_sale_previews_mark_units_outside_the_first_barracks(pages_run):
    header, *rows = pages_run["unitSale"]["rows"]
    barracks, sellable = header.index("兵营"), header.index("可出售")
    assert [(row[0], row[barracks], row[sellable]) for row in rows] == [("3", "0", "是"), ("4", "1", "否")]
    assert "只能出售第一兵营的单位" in pages_run["unitSale"]["text"]


def test_silver_trade_previews_show_the_barracks_check_and_place_parameter_errors(pages_run):
    trade = pages_run["silverTrade"]
    assert trade["params"] == [{"shop_id": 7, "material_get_index": 4}] * 2 and trade["fieldError"] is True
    assert "unit_roster_capacity" in trade["text"] and "空的兵营格" in trade["text"]


def test_growth_previews_show_where_the_target_and_each_material_is_stored(pages_run):
    training = pages_run["training"]
    assert training["params"] == [{"target_get_index": 4, "materials": [{"source": "barracks", "get_index": 5},
                                                                     {"source": "silver_house", "card_id": 9}]}]
    target = dict(training["pairs"])
    assert (target["兵营"], target["可用"]) == ("1", "否")
    header, stored, warehouse = training["materials"]
    barracks, usable = header.index("兵营"), header.index("可用")
    assert (stored[barracks], stored[usable]) == ("1", "是") and (warehouse[barracks], warehouse[usable]) == ("—", "—")
    assert string_constant(PAGES / "tools.js", "GROWTH_STORAGE_NOTE") in training["text"]
    assert "storage_supported" not in training["text"]


def test_growth_previews_warn_only_about_units_the_service_refuses(pages_run):
    evolution = pages_run["evolution"]
    assert evolution["params"] == [{"target_get_index": 3, "operation": "first_awakening"}]
    target = dict(evolution["pairs"])
    assert (target["兵营"], target["可用"]) == ("0", "是")
    header, stored, warehouse = evolution["materials"]
    barracks, usable = header.index("兵营"), header.index("可用")
    assert (stored[barracks], stored[usable]) == ("1", "是") and (warehouse[barracks], warehouse[usable]) == ("—", "—")
    assert string_constant(PAGES / "tools.js", "GROWTH_STORAGE_NOTE") not in evolution["text"]


def test_trade_previews_show_where_the_material_or_target_is_stored(pages_run):
    note = string_constant(PAGES / "tools.js", "TRADE_STORAGE_NOTE")
    shop = pages_run["growthShop"]
    assert shop["params"] == [{"shop_id": 2000, "target_get_index": 5}]
    for preview, barracks in ((pages_run["silverTrade"], "1"), (shop, "2")):
        stored = dict(preview["pairs"])
        assert (stored["兵营"], stored["可用"]) == (barracks, "否")
        assert note in preview["text"] and "storage_supported" not in preview["text"]


def test_paged_tools_reread_the_same_preview_at_the_next_offset(pages_run):
    fame = pages_run["fame"]
    assert fame["params"] == [{"offset": offset, "limit": DEFAULT_PAGE_ITEMS} for offset in (0, DEFAULT_PAGE_ITEMS)]
    assert "fame_unit_ineligible" in fame["text"]


def test_settings_place_commands_beside_their_views(pages_run):
    settings = pages_run["settings"]
    assert sorted(settings["eventView"]) == ["view_event.clear", "view_event.set"] and settings["eventViewShown"] is True
    assert settings["blessings"] == ["blessings.set"] and "_BattleGodBlessFilter" in settings["blessingText"]
    assert sorted(settings["config"]) == ["config.auto_select.set", "config.damage_display.set", "config.sort.set",
                                          "config.sound.set", "guide.set"]
    assert "其余设置命令都已放在上面对应的分节里" in settings["rest"] and settings["commandReads"] == 1
    assert settings["storageLink"] == "#/storage"
    for text in ("背景名不在目录里", "已标记购买但未持有", "Player names have no shared native validator", "未达成"):
        assert text in settings["text"], text


def test_the_battle_page_lists_windows_and_submits_the_preset_to_the_config_validation(pages_run, catalog):
    battle = pages_run["battle"]
    for text in ("Subjugation:400183", "Training:910001", "已配置", "开放中", "已结束", "成长活动窗口",
                 "prepare-client", "校验作业", "job-w", "没有进行中的战斗"):
        assert text in battle["text"], text
    assert battle["preset"] == {"enabled": True, "batches": {
        "Subjugation:400183": {"start_at": "2026-11-02T15:00:00+09:00", "end_at": "2026-11-16T10:59:59+09:00"}}}
    assert battle["validation"] == {"changes": {"mission-windows": battle["preset"]}}
    assert UUID4.fullmatch(battle["validationOpId"])
    assert battle["configLink"] == "#/config"
    assert sorted(battle["operations"]) == operations_of(catalog, "battle")


def test_the_votes_page_shows_the_assault_ledger_and_every_votes_command(pages_run, catalog):
    votes = pages_run["votes"]
    assert votes["reads"] == [{"offset": 0, "limit": DEFAULT_PAGE_ITEMS}]
    for text in ("强袭挑战权", "presents_full", "earned − spent", "通知"):
        assert text in votes["text"], text
    assert sorted(votes["operations"]) == operations_of(catalog, VOTES)


def test_vote_notifications_list_each_committed_record_under_its_key(pages_run):
    note = string_constant(PAGES / "votes.js", "NOTICE_NOTE")
    instants = pages_run["noticeInstants"]
    notices = pages_run["voteNotices"]
    header, row = pages_run["votes"]["notices"]
    assert header == ["活动编号", "mission_id", "gold", "mail_ids", "notified_at"]
    assert (row[0], row[1], row[2]) == ("7", "576", "3000")
    assert row[4] == instants["zero"] and row[4].endswith("JST")
    header, row = notices["votesExpiry"]["rows"]
    assert header == ["物品 ID", "gold", "mail_ids", "notified_at"]
    assert (row[0], row[1]) == ("vote_ticket_a", "2000")
    assert row[3] == instants["expiry"] and "2025-11-03" in row[3] and "1762138800000" not in row[3]
    header, row = notices["votesCooking"]["rows"]
    assert header == ["投票券物品 ID", "grant", "notified_at", "source"]
    assert row[0] == "vote_ticket_b" and row[1].startswith("对象（7 项）") and '"card_id": 9' in row[1]
    assert row[2] == instants["cooking"] and "2026-11-03" in row[2] and row[3] == "e" * 64
    for text in (pages_run["votes"]["text"], notices["votesExpiry"]["text"], notices["votesCooking"]["text"]):
        assert note in text


def test_vote_notifications_report_empty_and_unreadable_ledgers(pages_run):
    empty, refused = pages_run["voteNotices"]["votesExpiryEmpty"], pages_run["voteNotices"]["votesCookingRefused"]
    assert empty["rows"] == [] and "没有通知记录。" in empty["text"]
    assert refused["rows"] == [] and "vote_cooking_notifications_invalid" in refused["text"]
    assert "没有通知记录。" not in refused["text"]


def test_live_views_are_never_revalidated_from_a_cache(pages_run):
    events = pages_run["events"]
    first, *polls = events["snapshot"]
    assert first is None and len(polls) >= 2 and set(polls) == {'"12:gen-b"'}
    assert events["live"] == [None, None] and "实时计算" in events["liveText"] and events["tabs"] == 11


def test_ledger_sections_without_a_page_renderer_keep_the_generic_view(pages_run):
    sections = pages_run["events"]["sections"]
    assert sections["catalog"] == "目录"
    assert sections["projection"].startswith("投影") and "sugoroku_dependencies_changed" in sections["projection"]


def test_ledger_repair_sections_list_their_targets_under_their_labels(pages_run):
    labels = dict(LABEL_TEXT.findall(declared(UI_DIRECTORY / "ledger-view.js", "SECTION_LABELS")))
    expected = repair_sections()
    # The pass ledger builds its repair section from identity checks while rendering, not from pending entries.
    assert {view.name for view in LEDGERS if view.repair is not None} | {PASS_LEDGER} == set(expected)
    assert {"targets" in section for sections in expected.values() for section in sections.values()} == {True, False}
    for ledger, sections in expected.items():
        for name, section in sections.items():
            shown = pages_run["repairs"][ledger][name]
            assert shown["title"] == labels.get(name) and section["command"] in shown["text"], (ledger, name)
            assert shown["fields"] == [field for field in section if field != "targets"], (ledger, name)
            targets = section.get("targets")
            rows = [list(targets[0]), *([str(value) for value in target.values()] for target in targets)] if targets else []
            assert shown["rows"] == rows, (ledger, name)


def test_login_rule_checks_list_their_findings_after_the_ledger(pages_run):
    for label in (UNCHANGED, CHANGED):
        views = pages_run["loginRules"][label]
        for name, checks in LOGIN_CHECKS.items():
            findings = [field for check in checks for field in LOGIN_RULES[label][check] if field != "ok"]
            fields = views[name]["fields"]
            assert fields.index(SETTLED_LABEL) < len(fields) - len(findings), (label, name)
            assert fields[-len(findings):] == findings and "ok" not in fields, (label, name)
            assert all(heading in views[name]["text"] for heading in checks.values()), (label, name)


def test_login_rule_checks_that_cannot_run_fail_alone_under_their_headings(pages_run):
    failed, healthy = LOGIN_RULES[UNAVAILABLE], pages_run["loginRules"][UNCHANGED]
    for name, checks in LOGIN_CHECKS.items():
        view = pages_run["loginRules"][UNAVAILABLE][name]
        findings = [field for check in checks for field in LOGIN_RULES[UNCHANGED][check] if field != "ok"]
        assert not view["warnings"] and view["fields"] == healthy[name]["fields"][:-len(findings)], name
        order = [SETTLED_LABEL, *(part for check, heading in checks.items()
                                  for part in (heading, failed[check]["error"]["code"]))]
        assert re.search(".*".join(map(re.escape, order)), view["text"], re.DOTALL), name


def test_changed_login_rules_pin_a_warning_naming_the_repair_above_the_ledger(pages_run):
    assert not any(view["warnings"] for view in pages_run["loginRules"][UNCHANGED].values())
    changed = pages_run["loginRules"][CHANGED]
    for name, repair in ((LOGIN_REWARDS_VIEW, REBASE_OPERATION), (LOGIN_COOKING_VIEW, COOKING_MIGRATION)):
        [warning] = changed[name]["warnings"]
        assert repair in warning and all(str(operation) in warning for operation in LOGIN_REWARD_OPERATIONS), name
        assert "客户端弹出通信错误并原包重发" in warning, name
        assert changed[name]["text"].index(warning) < changed[name]["text"].index(SETTLED_LABEL), name


def test_failed_live_views_wait_for_an_explicit_retry(pages_run):
    stalled = pages_run["stalled"]
    assert stalled["readsWhileStalled"] == 1 and stalled["reads"] == 2 and stalled["retryLeft"] is False
    assert "当前世代缺少这个视图依赖的服务" in stalled["text"] and "读取失败后不会自动重试" in stalled["text"]


def test_snapshot_views_keep_polling_through_a_failed_read(pages_run):
    assert pages_run["polled"]["reads"] > 3


def test_the_readiness_refresh_rereads_only_the_readiness_answers(pages_run):
    readiness = pages_run["readiness"]
    assert readiness["after"] == readiness["before"]
    assert readiness["readinessReads"] == 2 and readiness["details"] == 2 and readiness["unopened"] == 0
    assert readiness["open"] is True
    assert "构建世代时计算" in readiness["text"] and "不会重算" in readiness["text"]


def test_master_snapshots_import_validate_and_unstage_from_the_config_page(pages_run):
    content = pages_run["configContent"]
    assert content["contentReadsBeforeOpen"] == 0
    assert content["imports"] == [{"capture": "20261103-120000"}]
    assert content["validation"] == {"changes": {}, "master_snapshot": "snap-2"} and UUID4.fullmatch(content["validationOpId"])
    assert content["unstage"] == {"staged_snapshot": "snap-1"} and UUID4.fullmatch(content["unstageOpId"])
    assert content["contentReads"] == 3


def test_cooking_switch_reports_name_the_reason_and_the_ledger_migration(pages_run):
    report = pages_run["cookingReport"]
    assert "料理阈值变化，存档的料理账本随切换迁移" in report["text"]
    for text in ("2026-11", "3,000 → 5,000", "4 → 2"):
        assert text in report["migration"], text
    assert STAGE_UNSUPPORTED_HINT in report["text"]
    assert report["stageDisabled"] is True and report["applyDisabled"] is False


def test_beginner_switch_reports_name_the_reason_and_the_ledger_rebase(pages_run):
    report = pages_run["beginnerReport"]
    assert "新手奖励总数变化，存档的登录奖励账本随切换重建" in report["text"] and "只改服务端规则" not in report["text"]
    for text in ("登录奖励账本随切换重建", "新手奖励总数7 → 14", "已领新手奖励3 → 3"):
        assert text in report["migration"], text
    assert STAGE_UNSUPPORTED_HINT in report["text"] and "Beginner total changes" in report["text"]
    assert report["switches"] == [False, True]


def test_content_reports_name_the_served_quiet_period_and_reused_tower_products(pages_run):
    report = pages_run["contentReport"]
    assert f"需要确认游戏客户端已关闭，且 {CLIENT_QUIET_SECONDS:.1f} 秒 内没有游戏请求" in report["apply"]
    assert "在静默阈值内没有游戏请求" in report["text"] and "120 秒" not in report["text"]
    products, operations = ("、".join(map(str, values)) for values in (TOWER_NOTICE["reused_products"], TOWER_TRADE_OPERATIONS))
    for text in (f"候选世代把这些塔兑换商品 ID 换给了别的商品：{products}",
                 f"新世代运行后 {operations} 在改绑之前整体拒绝，不只是这些商品", "客户端弹出通信错误并原包重发",
                 f"用 {REBIND_OPERATION} 逐个改绑这些商品的购买记录后恢复", "这条提示不阻止切换"):
        assert text in report["text"], text
    assert report["switches"] == [False, False]


def test_the_client_activity_hint_points_at_the_served_quiet_threshold(pages_run):
    refusal = pages_run["clientActive"]
    assert refusal["hint"] in refusal["text"] and not re.search(r"\d", refusal["hint"])
    assert QUIET_DETAIL in refusal["hint"] and f'"{QUIET_DETAIL}": {CLIENT_QUIET_SECONDS:g}' in refusal["text"]


def test_bundled_ticket_mails_split_together_in_one_plan(pages_run):
    bundled = pages_run["bundledTickets"]
    assert "present_state_invalid" in bundled["text"] and "、".join(map(str, BUNDLED_PRESENTS)) in bundled["text"]
    assert string_constant(PAGES / "presents.js", "BUNDLED_NOTE") in bundled["text"]
    assert bundled["button"] == f"全部拆开（{len(BUNDLED_PRESENTS)} 封）" and bundled["previewed"] is True
    assert bundled["plans"] == [{"commands": [{"operation": SPLIT_TICKET_MAIL.operation, "params": {"present_id": present_id}}
                                              for present_id in BUNDLED_PRESENTS]}]


def test_a_stale_config_report_names_what_changed_and_retires_its_switches(pages_run):
    stale = pages_run["cookingReport"]["stale"]
    assert "config_report_stale" in stale["text"]
    assert "报告之后运行中的世代、配置或主表指针、存档已变化，请刷新报告或重新校验。" in stale["text"]
    assert "这份报告已经用过或已失效" in stale["text"] and stale["switches"] == [True, True]


def test_config_reports_show_the_audit_size_check_and_the_settled_time_fields(pages_run):
    report, passing = pages_run["towerReport"], pages_run["cookingReport"]["text"]
    assert "check-failed" in report["check"] and report["head"] == "未通过 审计记录体积"
    assert "diff（2,097,152 字节）" in report["text"] and "1,048,576 字节" in report["text"]
    assert "按旧规则结算的时间累积字段" in report["text"] and "generation_hour" in report["text"]
    assert report["switches"] == [True, True]
    assert "通过 审计记录体积" in passing and "未通过 审计记录体积" not in passing and "这些审计字段超过了" not in passing


def test_plan_previews_show_oversized_audit_fields_and_block_the_commit(pages_run):
    audit = pages_run["auditPlan"]
    assert audit["commitDisabled"] is True and "check-failed" in audit["check"] and "未通过 审计记录体积" in audit["text"]
    assert "没有提交任何内容" in audit["text"] and "diff（2,097,152 字节）" in audit["text"] and "1,048,576 字节" in audit["text"]
    assert "没有检查审计记录的体积" in audit["skipped"] and "admin_preflight_failed" in audit["skipped"]


def test_restore_previews_check_the_audit_size_and_skip_same_state_backups(pages_run):
    restore = pages_run["restore"]
    blocked = restore["blocked"]
    assert blocked["disabled"] is True and "diff（2,097,152 字节）" in blocked["text"] and "未通过 审计记录体积" in blocked["text"]
    assert "执行前会在切换栅栏内自动备份当前存档" in blocked["text"]
    assert "目标与当前存档相同，不会自动备份。" in restore["sameState"] and "修订号保持不变" in restore["sameState"]
    assert "存档和修订号都没有改变" in restore["restored"] and "admin-auto" not in restore["restored"]
    assert restore["body"] == {"backup": "backups/admin-manual-3.sqlite3", "restore_digest": "9" * 64, "expected_revision": 12,
                               "client_closed": False, "confirmations": ["RESTORE 12"]}


def test_schema_forms_write_field_values_back(pages_run):
    written = pages_run["writeBack"]
    assert written["optional"] is True and written["union"] is True and written["missing"] is False
    assert written["marked"] is True and written["cleared"] is True
    assert written["growth"] == {"ok": True, "value": {"shop_id": 2001, "target_get_index": 5}}
    assert written["rewards"] == {"ok": True, "value": {"rewards": [{"kind": "gold", "amount": 7}]}}


def test_union_and_time_fields_read_strict_values_and_place_errors(pages_run):
    union = pages_run["unionForm"]
    assert {"unit", "free_crystal", "gold", "item", "vote_cooking"} <= set(union["kinds"])
    assert union["read"] == {"ok": True, "value": {"rewards": [{"kind": "gold", "amount": 50}]}}
    assert "too much gold" in union["amountError"]
    evolution = pages_run["evolutionForm"]
    assert evolution["sources"] == ["barracks", "silver_house", "fairy_farm"]
    assert evolution["read"] == {"ok": True, "value": {"target_get_index": 3, "operation": "first_awakening",
                                                       "materials": [{"source": "silver_house", "card_id": 9}]}}
    clock = pages_run["clockForm"]
    assert clock["missingZone"]["ok"] is False and clock["missingZone"]["problems"][0]["path"] == ["at"]
    assert clock["zoned"] == {"ok": True, "value": {"at": "2026-11-03T12:00:00+09:00"}}


def test_an_overtaken_answer_never_replaces_a_newer_read(pages_run):
    assert pages_run["newest"] == "second"


def test_domain_pages_render_their_answers_without_failures(pages_run):
    assert pages_run["unexpected"] == []
    for label, found in pages_run["crashes"].items():
        assert found == [] and pages_run["leaks"][label] is False, label
    for label in ("account", "items", "presents", "missions", "rules"):
        assert pages_run["texts"][label], label
    assert "当前世代缺少这个视图依赖的服务" in pages_run["rulesFailure"]
