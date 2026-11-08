import { button, h } from "./diff-view.js";

const OMIT = Symbol("omit");
const NULL_TYPE = "null";
const INTEGER_TEXT = /^-?(0|[1-9][0-9]*)$/;
const NUMBER_TEXT = /^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$/;
const DATE_TIME = "date-time";
const DATE_TIME_TEXT = /^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([Zz]|[+-]\d{2}:?\d{2})$/;
const DATE_TIME_EXAMPLE = "2026-11-03T12:00:00+09:00";
// Validation errors name a smart-union member by these tags when it is not a model.
const UNION_TAGS = Object.freeze({ integer: "int", number: "float", string: "str", boolean: "bool" });
const LONG_TEXT = 120;
const TEXT_ROWS = 4;
const JSON_ROWS = 6;
const DEFAULT_PREVIEW = 40;
let labelSequence = 0;

function lookup(root, reference) {
    if (!reference.startsWith("#/")) return {};
    const parts = reference.slice(2).split("/").map((part) => part.replaceAll("~1", "/").replaceAll("~0", "~"));
    return parts.reduce((node, part) => (node !== null && typeof node === "object" ? node[part] : undefined), root) ?? {};
}

function resolve(schema, root) {
    let current = schema !== null && typeof schema === "object" ? schema : {};
    const seen = new Set();
    for (;;) {
        if (typeof current.$ref === "string" && !seen.has(current.$ref)) {
            seen.add(current.$ref);
            const { $ref, ...siblings } = current;
            current = { ...lookup(root, $ref), ...siblings };
        } else if (Array.isArray(current.allOf) && current.allOf.length === 1) {
            const { allOf: [only], ...siblings } = current;
            current = { ...(only ?? {}), ...siblings };
        } else {
            return current;
        }
    }
}

function withoutNull(schema, root) {
    if (Array.isArray(schema.type)) {
        const types = schema.type.filter((type) => type !== NULL_TYPE);
        if (types.length === 1 && types.length < schema.type.length) {
            return { schema: { ...schema, type: types[0] }, nullable: true };
        }
        return { schema, nullable: false };
    }
    const branches = schema.anyOf ?? schema.oneOf;
    if (!Array.isArray(branches)) return { schema, nullable: false };
    const others = branches.filter((branch) => resolve(branch, root).type !== NULL_TYPE);
    if (others.length !== 1 || others.length === branches.length) return { schema, nullable: false };
    const { anyOf, oneOf, ...siblings } = schema;
    return { schema: resolve({ ...others[0], ...siblings }, root), nullable: true };
}

function isSchema(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
}

function kindOf(schema) {
    if ("const" in schema) return "const";
    if (Array.isArray(schema.enum) && schema.enum.length) return "enum";
    if (Array.isArray(schema.oneOf ?? schema.anyOf)) return "union";
    switch (schema.type) {
        case NULL_TYPE:
            return "null";
        case "boolean":
        case "integer":
        case "number":
        case "string":
            return schema.type;
        case "object":
            return isSchema(schema.properties) && !isSchema(schema.additionalProperties) && !schema.patternProperties
                ? "object" : "json";
        case "array":
            return isSchema(schema.items) && !schema.prefixItems ? "array" : "json";
        default:
            return "json";
    }
}

function fail(problems, path, message) {
    problems.push({ path, message });
    return OMIT;
}

function leaf(element, read) {
    return { element, read, child: () => null };
}

function bounds(schema, number) {
    if (schema.minimum !== undefined && number < schema.minimum) return `不能小于 ${schema.minimum}`;
    if (schema.maximum !== undefined && number > schema.maximum) return `不能大于 ${schema.maximum}`;
    if (schema.exclusiveMinimum !== undefined && number <= schema.exclusiveMinimum) return `必须大于 ${schema.exclusiveMinimum}`;
    if (schema.exclusiveMaximum !== undefined && number >= schema.exclusiveMaximum) return `必须小于 ${schema.exclusiveMaximum}`;
    if (schema.multipleOf !== undefined && !Number.isInteger(number / schema.multipleOf)) return `必须是 ${schema.multipleOf} 的倍数`;
    return null;
}

function numberControl(schema, initial, integer) {
    const input = h("input", {
        type: "text", class: "input", inputMode: integer ? "numeric" : "decimal", autocomplete: "off", spellcheck: false,
        value: typeof initial === "number" ? String(initial) : "",
    });
    return leaf(input, (problems, path) => {
        const text = input.value.trim();
        if (text === "") return fail(problems, path, "请填写数值");
        if (!(integer ? INTEGER_TEXT : NUMBER_TEXT).test(text)) return fail(problems, path, integer ? "需要整数" : "需要数值");
        const number = Number(text);
        if (integer ? !Number.isSafeInteger(number) : !Number.isFinite(number)) return fail(problems, path, "超出可精确表示的范围");
        const problem = bounds(schema, number);
        return problem === null ? number : fail(problems, path, problem);
    });
}

function pattern(source) {
    if (typeof source !== "string") return null;
    try {
        return new RegExp(source, "u");
    } catch {
        // Python-only syntax cannot be checked here; the server still validates it strictly.
        return null;
    }
}

function stringControl(schema, initial) {
    const value = typeof initial === "string" ? initial : "";
    const moment = schema.format === DATE_TIME;
    const input = moment || (schema.maxLength !== undefined && schema.maxLength <= LONG_TEXT)
        ? h("input", { type: "text", class: "input", autocomplete: "off", value,
            placeholder: moment ? DATE_TIME_EXAMPLE : undefined })
        : h("textarea", { class: "input textarea", rows: TEXT_ROWS, value });
    const expression = pattern(schema.pattern);
    return leaf(input, (problems, path) => {
        const text = moment ? input.value.trim() : input.value;
        if (moment && !DATE_TIME_TEXT.test(text)) return fail(problems, path, `需要带时区的 ISO 时间，例如 ${DATE_TIME_EXAMPLE}`);
        const length = [...text].length;
        if (schema.minLength !== undefined && length < schema.minLength) {
            return fail(problems, path, `至少 ${schema.minLength} 个字符`);
        }
        if (schema.maxLength !== undefined && length > schema.maxLength) {
            return fail(problems, path, `最多 ${schema.maxLength} 个字符`);
        }
        if (expression !== null && !expression.test(text)) return fail(problems, path, `格式应符合 ${schema.pattern}`);
        return text;
    });
}

function booleanControl(initial) {
    const input = h("input", { type: "checkbox", class: "checkbox", checked: initial === true });
    return leaf(h("label", { class: "inline-choice" }, input, "是"), () => input.checked);
}

function same(left, right) {
    return JSON.stringify(left) === JSON.stringify(right);
}

function enumControl(schema, initial) {
    const options = schema.enum;
    const chosen = Math.max(0, options.findIndex((option) => same(option, initial)));
    const select = h("select", { class: "input" }, options.map((option, index) =>
        h("option", { value: String(index) }, typeof option === "string" ? option : JSON.stringify(option))));
    select.value = String(chosen);
    return leaf(select, () => options[Number(select.value)]);
}

function constControl(schema) {
    return leaf(h("code", { class: "code" }, JSON.stringify(schema.const)), () => schema.const);
}

function jsonControl(initial) {
    const area = h("textarea", {
        class: "input textarea json-input", rows: JSON_ROWS, spellcheck: false,
        value: initial === undefined ? "" : JSON.stringify(initial, null, 2),
    });
    return leaf(area, (problems, path) => {
        const text = area.value.trim();
        if (text === "") return fail(problems, path, "请填写 JSON 值");
        try {
            return JSON.parse(text);
        } catch (error) {
            return fail(problems, path, `JSON 无效：${error.message}`);
        }
    });
}

function markable(element, message) {
    return {
        mark(text) {
            message.textContent = text;
            message.hidden = false;
            element.classList.add("field-invalid");
        },
        clear() {
            message.textContent = "";
            message.hidden = true;
            element.classList.remove("field-invalid");
        },
    };
}

function preview(value) {
    const text = JSON.stringify(value);
    return text.length > DEFAULT_PREVIEW ? `${text.slice(0, DEFAULT_PREVIEW)}…` : text;
}

function field(name, schema, initial, required, root) {
    const resolved = resolve(schema, root);
    const start = initial !== undefined ? initial : resolved.default;
    let inner = control(resolved, start, root);
    const labelId = `schema-field-${++labelSequence}`;
    const include = required ? null : h("input", { type: "checkbox", class: "checkbox", checked: initial !== undefined });
    const body = h("fieldset", { class: "field-body", disabled: include !== null && !include.checked }, inner.element);
    include?.addEventListener("change", () => { body.disabled = !include.checked; });
    const message = h("p", { class: "field-error", hidden: true });
    const element = h("div", { class: "field", role: "group", "aria-labelledby": labelId, dataset: { field: String(name) } },
        h("div", { class: "field-head" },
            h("span", { class: "field-label", id: labelId }, resolved.title ?? String(name),
                required ? h("span", { class: "field-required", "aria-hidden": "true" }, "*") : null),
            include ? h("label", { class: "inline-choice" }, include, "指定") : null,
            !required && "default" in resolved ? h("span", { class: "field-default" }, `默认 ${preview(resolved.default)}`) : null),
        resolved.description ? h("p", { class: "field-hint" }, resolved.description) : null,
        body, message);
    const marks = markable(element, message);
    return {
        element,
        ...marks,
        read: (problems, path) => (include !== null && !include.checked ? OMIT : inner.read(problems, path)),
        child: (segment) => inner.child(segment),
        write(value) {
            // A rebuilt control renders the value the way an initial value is rendered, for every kind of field.
            inner = control(resolved, value, root);
            body.replaceChildren(inner.element);
            if (include !== null) include.checked = true;
            body.disabled = false;
            marks.clear();
        },
    };
}

function objectControl(schema, initial, root) {
    const required = new Set(schema.required ?? []);
    const source = isSchema(initial) ? initial : {};
    const fields = Object.entries(schema.properties).map(([name, property]) =>
        [name, field(name, property, source[name], required.has(name), root)]);
    const element = h("div", { class: "schema-object" },
        fields.length ? fields.map(([, node]) => node.element) : h("p", { class: "muted" }, "没有参数"));
    return {
        element,
        read(problems, path) {
            const value = {};
            for (const [name, node] of fields) {
                const item = node.read(problems, [...path, name]);
                if (item !== OMIT) value[name] = item;
            }
            return value;
        },
        child: (segment) => fields.find(([name]) => name === segment)?.[1] ?? null,
    };
}

function arrayControl(schema, initial, root) {
    const items = [];
    const rows = h("ol", { class: "schema-array" });
    const add = button("添加一项", { onClick: () => { addItem(undefined); sync(); } });
    function sync() {
        add.disabled = schema.maxItems !== undefined && items.length >= schema.maxItems;
    }
    function addItem(value) {
        const itemSchema = resolve(schema.items, root);
        const node = control(itemSchema, value === undefined ? itemSchema.default : value, root);
        const message = h("p", { class: "field-error", hidden: true });
        const row = h("li", { class: "schema-item" }, node.element,
            button("移除", { onClick: () => { items.splice(items.indexOf(entry), 1); row.remove(); sync(); } }), message);
        const entry = { node, ...markable(row, message), child: (segment) => node.child(segment) };
        items.push(entry);
        rows.append(row);
    }
    (Array.isArray(initial) ? initial : []).forEach((value) => addItem(value));
    sync();
    return {
        element: h("div", { class: "schema-array-wrap" }, rows, add),
        read(problems, path) {
            const values = items.map((entry, index) => entry.node.read(problems, [...path, index]));
            if (schema.minItems !== undefined && items.length < schema.minItems) fail(problems, path, `至少 ${schema.minItems} 项`);
            if (schema.maxItems !== undefined && items.length > schema.maxItems) fail(problems, path, `最多 ${schema.maxItems} 项`);
            return values;
        },
        child: (segment) => items[Number(segment)] ?? null,
    };
}

function nullableControl(node, startsNull) {
    const toggle = h("input", { type: "checkbox", class: "checkbox", checked: startsNull });
    const body = h("fieldset", { class: "field-body", disabled: startsNull }, node.element);
    toggle.addEventListener("change", () => { body.disabled = toggle.checked; });
    return {
        element: h("div", { class: "schema-nullable" }, h("label", { class: "inline-choice" }, toggle, "空值（null）"), body),
        read: (problems, path) => (toggle.checked ? null : node.read(problems, path)),
        child: (segment) => node.child(segment),
    };
}

function tagValues(property) {
    if (!isSchema(property)) return null;
    if ("const" in property) return [property.const];
    return Array.isArray(property.enum) && property.enum.length ? property.enum : null;
}

function discriminator(schema, branches, root) {
    const tagged = (name) => branches.map((branch) => (isSchema(branch.properties)
        ? tagValues(resolve(branch.properties[name], root)) : null));
    const named = schema.discriminator?.propertyName;
    if (typeof named === "string") return tagged(named).every((values) => values !== null) ? named : null;
    return Object.keys(branches[0]?.properties ?? {}).find((name) => {
        const values = tagged(name);
        if (values.some((entry) => entry === null)) return false;
        const flat = values.flat().map((value) => JSON.stringify(value));
        // An undeclared tag is used only when its values pick exactly one member.
        return new Set(flat).size === flat.length;
    }) ?? null;
}

function variants(schema, root) {
    const branches = (schema.oneOf ?? schema.anyOf).map((branch) => resolve(branch, root));
    const tag = discriminator(schema, branches, root);
    if (tag === null) {
        return branches.map((branch, index) => ({
            label: branch.title ?? UNION_TAGS[branch.type] ?? branch.type ?? `选项 ${index + 1}`,
            segment: branch.title ?? UNION_TAGS[branch.type] ?? null, tag: null, value: undefined, schema: branch,
        }));
    }
    return branches.flatMap((branch) => {
        const property = resolve(branch.properties[tag], root);
        // One variant per tag value, so a member listing several tags keeps the chosen one fixed in its form.
        return tagValues(property).map((value) => ({
            label: typeof value === "string" ? value : JSON.stringify(value), segment: String(value), tag, value,
            schema: { ...branch, properties: { ...branch.properties, [tag]: { title: property.title, const: value } } },
        }));
    });
}

function fits(schema, value) {
    switch (schema.type) {
        case "object":
            return isSchema(value);
        case "array":
            return Array.isArray(value);
        case "integer":
            return Number.isInteger(value);
        case NULL_TYPE:
            return value === null;
        default:
            return typeof value === schema.type;
    }
}

function chosenVariant(options, initial) {
    const index = options.findIndex((option) => (option.tag === null ? fits(option.schema, initial)
        : isSchema(initial) && same(initial[option.tag], option.value)));
    return Math.max(0, index);
}

function unionControl(schema, initial, root) {
    const options = variants(schema, root);
    if (!options.length) return jsonControl(initial);
    const select = h("select", { class: "input" }, options.map((option, index) =>
        h("option", { value: String(index) }, option.label)));
    const body = h("div", { class: "schema-union-body" });
    let active = null;
    const choose = (index, value) => {
        active = { option: options[index], node: control(options[index].schema, value, root) };
        body.replaceChildren(active.node.element);
    };
    const start = chosenVariant(options, initial);
    select.value = String(start);
    select.addEventListener("change", () => choose(Number(select.value), undefined));
    choose(start, initial);
    return {
        element: h("div", { class: "schema-union" }, select, body),
        read: (problems, path) => active.node.read(problems, path),
        // Validation errors name the chosen member before its fields, so that segment passes through to the member.
        child: (segment) => (String(segment) === active.option.segment
            ? { child: (next) => active.node.child(next) } : active.node.child(segment)),
    };
}

function plainControl(schema, initial, root) {
    switch (kindOf(schema)) {
        case "const":
            return constControl(schema);
        case "null":
            return constControl({ const: null });
        case "union":
            return unionControl(schema, initial, root);
        case "enum":
            return enumControl(schema, initial);
        case "boolean":
            return booleanControl(initial);
        case "integer":
            return numberControl(schema, initial, true);
        case "number":
            return numberControl(schema, initial, false);
        case "string":
            return stringControl(schema, initial);
        case "object":
            return objectControl(schema, initial, root);
        case "array":
            return arrayControl(schema, initial, root);
        default:
            return jsonControl(initial);
    }
}

function control(schema, initial, root) {
    const { schema: inner, nullable } = withoutNull(resolve(schema, root), root);
    const node = plainControl(inner, initial === null && nullable ? undefined : initial, root);
    return nullable ? nullableControl(node, initial === null) : node;
}

function locate(top, path) {
    let current = top;
    let found = null;
    let depth = 0;
    for (const [index, segment] of path.entries()) {
        current = current.child(segment);
        if (current === null) break;
        if (typeof current.mark === "function") {
            found = current;
            depth = index + 1;
        }
    }
    return { found, rest: path.slice(depth) };
}

export function schemaForm(schema, { value } = {}) {
    const root = isSchema(schema) ? schema : {};
    const top = control(root, value, root);
    const summary = h("ul", { class: "form-problems", hidden: true });
    const element = h("div", { class: "schema-form" }, summary, top.element);
    let marked = [];

    function showErrors(problems) {
        marked.forEach((node) => node.clear());
        marked = [];
        const unplaced = [];
        for (const problem of problems) {
            const { found, rest } = locate(top, problem.path);
            const text = rest.length ? `${rest.join(".")}：${problem.message}` : problem.message;
            if (found === null) {
                unplaced.push(text);
            } else {
                found.mark(text);
                marked.push(found);
            }
        }
        summary.replaceChildren(...unplaced.map((text) => h("li", {}, text)));
        summary.hidden = unplaced.length === 0;
    }

    return Object.freeze({
        element,
        showErrors,
        read() {
            const problems = [];
            const result = top.read(problems, []);
            showErrors(problems);
            return problems.length ? { ok: false, problems } : { ok: true, value: result };
        },
        setValue(name, value) {
            const node = top.child(name);
            if (typeof node?.write !== "function") return false;
            node.write(value);
            return true;
        },
    });
}
