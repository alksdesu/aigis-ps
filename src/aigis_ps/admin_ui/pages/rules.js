import {
    badge, code, digest, formatInstant, h, keyValues, list, notice, renderError, renderValue, section, table,
} from "../diff-view.js";
import { fieldsList, inline, pageLink, semantics, sourceLabel, viewPanel } from "../view-kit.js";

const RULES_VIEW = "rules.config";
const CONFIG_ROUTE = "config";
const RULES_NOTE = "这个视图每次读取都重新计算：磁盘上的配置集、客户端清单和正在分发的资源都可能在没有新世代时变化。修改规则请到配置页。";
const UNKNOWN_SET_NOTE = "运行中的配置摘要对应不到已保存的配置集（例如使用了命令行规则文件），下面只列出各键的代码默认值。";

export default Object.freeze({ title: "运行规则", order: 100, mount });

function mount(root, { signal }) {
    const rules = viewPanel(RULES_VIEW, undefined, renderRules, { signal });
    root.append(section("运行中的规则", notice("info", RULES_NOTE, " ", pageLink(CONFIG_ROUTE, "打开配置页")), rules.element));
}

function matched(value) {
    if (value === null || value === undefined) return badge("无法比较", "muted");
    return value ? badge("一致", "ok") : badge("不一致", "danger");
}

function version(text) {
    return text ? digest(text) : "—";
}

function renderRunning(running) {
    return h("div", {},
        keyValues([
            ["配置摘要", version(running.digest)],
            ["配置集", running.set ? code(running.set) : "—"],
            ["已识别", running.known ? badge("是", "ok") : badge("否", "warn")],
        ]),
        running.error ? renderError(running.error) : null,
        running.known || running.error ? null : notice("warn", UNKNOWN_SET_NOTE));
}

function keyState(entry) {
    return h("span", {},
        entry.error ? [badge("加载失败", "danger"), " ", code(entry.error.code), " "] : null,
        entry.read_only ? [badge("只读", "warn"), " "] : null,
        entry.shadowed ? badge("被命令行覆盖", "warn") : null);
}

const KEY_COLUMNS = [
    { label: "键", cell: (entry) => code(entry.key) },
    { label: "来源", cell: (entry) => (entry.source ? sourceLabel(entry.source) : "—") },
    { label: "消费域", cell: (entry) => list(entry.domains) },
    { label: "时间语义", cell: (entry) => semantics(entry) },
    { label: "内容版本", cell: (entry) => version(entry.content_version) },
    { label: "状态", cell: keyState },
    { label: "生效值", cell: (entry) => renderValue(entry.effective ?? entry.default, { summary: "值" }) },
];

function renderMasterdata(masterdata) {
    return keyValues([
        ["生成时使用", masterdata.in_use ? code(masterdata.in_use) : "—"],
        ["磁盘", inline(masterdata.disk)],
        ["一致", matched(masterdata.matches)],
    ]);
}

function renderResources(resources) {
    return keyValues([
        ["基础版本", version(resources.base)],
        ["生效版本", version(resources.effective)],
        ["正在分发", version(resources.served)],
        ["有覆盖层", resources.overlaid === null ? "—" : resources.overlaid ? badge("是", "info") : "否"],
        ["分发与生效一致", matched(resources.matches)],
    ]);
}

function renderPair(prepared, current, matches) {
    return h("span", {}, version(prepared), " / ", version(current), " ", matched(matches));
}

function renderManifest(manifest) {
    if (!manifest.present) return h("p", { class: "muted" }, "没有客户端清单：", code(manifest.path));
    if (!manifest.valid) return notice("danger", "客户端清单无法读取：", code(manifest.path));
    return h("div", {},
        keyValues([
            ["位置", code(manifest.path)],
            ["格式版本", manifest.schema_version],
            ["准备时间", formatInstant(manifest.prepared_at)],
            ["客户端（准备 / 世代）", renderPair(manifest.client.prepared, manifest.client.generation, manifest.client.matches)],
            ["资源（准备 / 分发）", renderPair(manifest.resources.prepared, manifest.resources.served, manifest.resources.matches)],
        ]),
        table([
            { label: "覆盖层", cell: ([name]) => code(name) },
            { label: "存在", cell: ([, overlay]) => (overlay.present ? badge("是", "ok") : "否") },
            { label: "规则哈希", cell: ([, overlay]) => version(overlay.rules_hash) },
        ], Object.entries(manifest.overlays ?? {}), { empty: "没有覆盖层。" }));
}

function renderRules(data) {
    const versions = data.versions ?? {};
    return h("div", {},
        renderRunning(data.running ?? {}),
        h("h3", { class: "subheading" }, "规则键"),
        table(KEY_COLUMNS, data.keys ?? [], { empty: "没有规则键。" }),
        h("h3", { class: "subheading" }, "主表快照"), renderMasterdata(versions.masterdata ?? {}),
        h("h3", { class: "subheading" }, "客户端资源"), renderResources(versions.resources ?? {}),
        h("h3", { class: "subheading" }, "客户端清单"), renderManifest(versions.client_manifest ?? {}),
        h("h3", { class: "subheading" }, "内容版本"), fieldsList(versions.content));
}
