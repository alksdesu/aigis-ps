"use strict";

function filterScheduleXml(input, allowedGroups, privateFields) {
    const invalid = () => { throw new Error("invalid_schedule_xml"); };
    const privateName = name => name.split("[", 1)[0].toLowerCase().replace(/[_.-]/g, "");
    const denied = new Set(privateFields);
    const allowed = new Set(allowedGroups);
    const stack = [];
    const output = ["<DA>"];
    const kept = new Set();
    const removed = new Set();
    let started = false, closed = false, position = 0, elements = 0;
    input = input.replace(/^\uFEFF/, "");
    if (input.includes("\0") || /<!\s*(?:DOCTYPE|ENTITY)/i.test(input)) invalid();

    function attributes(source) {
        const result = new Map();
        const pattern = /^\s+([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(["'])([\s\S]*?)\2/;
        while (source.trim()) {
            const match = pattern.exec(source);
            if (!match || result.has(match[1]) || match[3].includes("<")) invalid();
            result.set(match[1], match[3]);
            source = source.slice(match[0].length);
        }
        return result;
    }

    while (position < input.length) {
        const start = input.indexOf("<", position);
        const text = input.slice(position, start < 0 ? input.length : start);
        if (stack.length > 1 && stack[stack.length - 1].keep) output.push(text);
        else if (stack.length === 0 && text.trim()) invalid();
        if (start < 0) { position = input.length; break; }
        if (input.startsWith("<!--", start)) {
            const end = input.indexOf("-->", start + 4);
            if (end < 0) invalid();
            position = end + 3;
            continue;
        }
        if (input.startsWith("<![CDATA[", start)) {
            const end = input.indexOf("]]>", start + 9);
            if (end < 0 || stack.length < 2) invalid();
            if (stack[stack.length - 1].keep) output.push(input.slice(start, end + 3));
            position = end + 3;
            continue;
        }
        if (input.startsWith("<?", start)) {
            const end = input.indexOf("?>", start + 2);
            if (started || end < 0 || !/^<\?xml\s/.test(input.slice(start, end))) invalid();
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
            const entry = stack.pop();
            if (entry.name !== match[1]) invalid();
            if (entry.keep) output.push("</" + entry.name + ">");
            if (!stack.length) closed = true;
            continue;
        }
        const selfClosing = /\/\s*$/.test(token);
        const body = selfClosing ? token.replace(/\/\s*$/, "") : token;
        const match = /^([A-Za-z_][A-Za-z0-9_.-]*)([\s\S]*)$/.exec(body);
        if (!match || ++elements > 1000000 || stack.length >= 32) invalid();
        const name = match[1], attrs = attributes(match[2]);
        if (!stack.length) {
            if (started || closed || name !== "DA" || attrs.size || selfClosing) invalid();
            started = true;
            stack.push({ name, keep: false });
            continue;
        }
        let keep = false;
        if (stack.length === 1) {
            const effective = attrs.get("N") || name;
            keep = allowed.has(effective);
            if (keep) {
                if (Array.from(attrs.keys()).some(key => key !== "N") || kept.has(effective)) invalid();
                kept.add(effective);
            }
        } else if (stack[stack.length - 1].keep) {
            const names = [name];
            for (const [key, value] of attrs) {
                if (!["T", "N", "I"].includes(key)) invalid();
                if ((key === "N" || key === "I") && /[&<>]/.test(value)) invalid();
                if (key === "N") names.push(value);
            }
            if (attrs.has("T") && attrs.get("T") !== "P" && (attrs.has("N") || attrs.has("I"))) invalid();
            const hidden = names.map(privateName).filter(value => denied.has(value));
            hidden.forEach(value => removed.add(value));
            keep = hidden.length === 0;
        }
        if (keep) output.push("<" + body + (selfClosing ? "/>" : ">"));
        if (!selfClosing) stack.push({ name, keep });
    }
    if (!started || !closed || stack.length) invalid();
    output.push("</DA>");
    return { xml: output.join(""), groups: Array.from(kept), removed: Array.from(removed).sort() };
}
