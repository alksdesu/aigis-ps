"use strict";

function filterResponseXml(input, rules) {
    const invalid = () => { throw new Error("invalid_response_xml"); };
    const normalize = name => name.split("[", 1)[0].toLowerCase().replace(/[_.-]/g, "");
    const denied = new Set(rules.privateNames);
    const positional = new Map(Object.entries(rules.positionalSecrets).map(([group, names]) => [group, new Set(names)]));
    const removed = new Set();
    let removedRows = 0;
    const xmlPoint = point => point === 0x9 || point === 0xA || point === 0xD || (point >= 0x20 && point <= 0xD7FF)
        || (point >= 0xE000 && point <= 0xFFFD) || (point >= 0x10000 && point <= 0x10FFFF);
    input = input.replace(/^﻿/, "");
    if (/[\x00-\x08\x0B\x0C\x0E-\x1F￾￿]/.test(input) || /<!\s*(?:DOCTYPE|ENTITY)/i.test(input)) invalid();

    function decode(text) {
        return text.replace(/&(#x[0-9A-Fa-f]{1,6}|#[0-9]{1,7}|lt|gt|amp|quot|apos);|&/g, (_, entity) => {
            if (entity === undefined) invalid();
            const named = { lt: "<", gt: ">", amp: "&", quot: "\"", apos: "'" }[entity];
            if (named !== undefined) return named;
            const point = entity[1] === "x" ? parseInt(entity.slice(2), 16) : parseInt(entity.slice(1), 10);
            if (!xmlPoint(point)) invalid();
            return String.fromCodePoint(point);
        });
    }

    function attributes(source) {
        const result = new Map();
        const pattern = /^\s+([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(["'])([\s\S]*?)\2/;
        while (source.trim()) {
            const match = pattern.exec(source);
            if (!match || result.has(match[1]) || /[&<>]/.test(match[3]) || !["T", "N", "I"].includes(match[1])) invalid();
            result.set(match[1], match[3]);
            source = source.slice(match[0].length);
        }
        return result;
    }

    function parse() {
        const stack = [];
        let root = null, position = 0, elements = 0, declared = false;
        while (position < input.length) {
            const start = input.indexOf("<", position);
            const text = input.slice(position, start < 0 ? input.length : start);
            if (stack.length) stack[stack.length - 1].text.push({ raw: text, value: decode(text) });
            else if (text.trim()) invalid();
            if (start < 0) break;
            if (input.startsWith("<!--", start)) {
                const end = input.indexOf("-->", start + 4);
                if (end < 0) invalid();
                position = end + 3;
                continue;
            }
            if (input.startsWith("<![CDATA[", start)) {
                const end = input.indexOf("]]>", start + 9);
                if (end < 0 || !stack.length) invalid();
                stack[stack.length - 1].text.push({ raw: input.slice(start, end + 3), value: input.slice(start + 9, end) });
                position = end + 3;
                continue;
            }
            if (input.startsWith("<?", start)) {
                const end = input.indexOf("?>", start + 2);
                if (declared || root !== null || stack.length || end < 0 || !/^<\?xml\s/.test(input.slice(start, end))) invalid();
                declared = true;
                position = end + 2;
                continue;
            }
            let end = start + 1, quote = null;
            for (; end < input.length; end++) {
                const char = input[end];
                if (quote !== null) { if (char === quote) quote = null; }
                else if (char === "\"" || char === "'") quote = char;
                else if (char === ">") break;
            }
            if (end >= input.length || quote !== null) invalid();
            const token = input.slice(start + 1, end);
            position = end + 1;
            if (token.startsWith("/")) {
                const match = /^\/([A-Za-z_][A-Za-z0-9_.-]*)\s*$/.exec(token);
                if (!match || !stack.length) invalid();
                const node = stack.pop();
                if (node.name !== match[1]) invalid();
                if (!stack.length) root = node;
                continue;
            }
            const selfClosing = /\/\s*$/.test(token);
            const body = selfClosing ? token.replace(/\/\s*$/, "") : token;
            const match = /^([A-Za-z_][A-Za-z0-9_.-]*)([\s\S]*)$/.exec(body);
            if (!match || ++elements > rules.maxElements || stack.length >= rules.maxDepth) invalid();
            if (!stack.length && (root !== null || match[1] !== "DA" || match[2].trim())) invalid();
            const node = { name: match[1], attrs: attributes(match[2]), source: body.replace(/\s+$/, ""),
                children: [], text: [], item: false };
            if (stack.length) stack[stack.length - 1].children.push(node);
            if (!selfClosing) stack.push(node);
            else if (!stack.length) root = node;
        }
        if (root === null || stack.length) invalid();
        return root;
    }

    function check(node) {
        const kind = node.attrs.get("T");
        if (kind !== undefined && kind !== "P" && (node.attrs.has("N") || node.attrs.has("I"))) invalid();
        if (node.text.some(piece => piece.value.trim())) invalid();
        for (const child of node.children) {
            if (kind === undefined) { check(child); continue; }
            if (child.attrs.size || child.children.length) invalid();
            child.item = true;
        }
    }

    const effectiveName = node => node.attrs.get("N") || node.name;
    const itemValue = item => item.text.map(piece => piece.value).join("");

    function privateNode(node) {
        const names = [node.name];
        if (node.attrs.has("N")) names.push(node.attrs.get("N"));
        const hits = names.map(normalize).filter(value => denied.has(value));
        hits.forEach(value => removed.add(value));
        return hits.length > 0;
    }

    function keyedRows(node) {
        const columns = node.children;
        if (!columns.length || columns[0].attrs.get("T") !== "S" || effectiveName(columns[0]) !== "A1") return node;
        const keys = columns[0].children.map(item => normalize(itemValue(item)));
        const hits = new Set();
        keys.forEach((key, index) => { if (denied.has(key)) hits.add(index); });
        if (!hits.size) return node;
        hits.forEach(index => removed.add(keys[index]));
        if (columns.some(column => !column.attrs.has("T") || column.children.length !== keys.length)) {
            removed.add(effectiveName(node) + ".*");
            return null;
        }
        removedRows += hits.size;
        for (const column of columns) column.children = column.children.filter((_, index) => !hits.has(index));
        return node;
    }

    function filterNode(node) {
        const secrets = positional.get(effectiveName(node));
        const kept = [];
        for (const child of node.children) {
            if (privateNode(child)) continue;
            if (secrets !== undefined && secrets.has(effectiveName(child))) {
                removed.add(effectiveName(node) + "." + effectiveName(child));
                continue;
            }
            if (child.attrs.has("T")) { kept.push(child); continue; }
            const filtered = filterNode(child);
            if (filtered !== null) kept.push(filtered);
        }
        node.children = kept;
        return keyedRows(node);
    }

    function serialize(node) {
        const content = node.item ? node.text.map(piece => piece.raw).join("") : node.children.map(serialize).join("");
        return "<" + node.source + ">" + content + "</" + node.name + ">";
    }

    const root = parse();
    check(root);
    const clean = filterNode(root);
    if (clean === null) invalid();
    return { xml: serialize(clean), groups: clean.children.map(effectiveName),
        removed: Array.from(removed).sort(), removedRows };
}
