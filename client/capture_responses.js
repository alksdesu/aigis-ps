"use strict";

(() => {
    const config = RESPONSE_CAPTURE_CONFIG;
    const listeners = [];
    let stopped = false, sequence = 0;
    function stop(code) {
        if (stopped) return;
        stopped = true;
        for (const listener of listeners) listener.detach();
        send({ event: "error", code });
    }
    function utf8Length(value) {
        let size = 0;
        for (const char of value) {
            const point = char.codePointAt(0);
            if (point >= 0xD800 && point <= 0xDFFF) throw new Error("invalid_utf8");
            size += point < 0x80 ? 1 : point < 0x800 ? 2 : point < 0x10000 ? 3 : 4;
        }
        return size;
    }
    try {
        const module = Process.mainModule;
        const normalize = path => path.replace(/\//g, "\\").toLowerCase();
        if (Process.platform !== "windows" || Process.arch !== "ia32" || Process.pointerSize !== 4 ||
                normalize(module.path) !== normalize(config.exePath)) {
            stop("unsupported_process"); return;
        }
        for (const block of config.blocks) {
            if (!captureSignatureMatches(module.base, block)) {
                stop("signature_mismatch"); return;
            }
        }
        for (const table of config.vtables) {
            for (const slot of table.slots) {
                if (!module.base.add(table.rva).add(slot.offset).readPointer().equals(module.base.add(slot.rva))) {
                    stop("vtable_mismatch"); return;
                }
            }
        }
        const caller = module.base.add(config.returnRva);
        const excludedOperations = new Set(config.excludedOperations);
        const excludedTables = config.excludedVtables.map(rva => module.base.add(rva));
        const selected = config.operations === null ? null : new Set(config.operations);

        function capture(invocation, args, target) {
            if (stopped || !invocation.returnAddress.equals(caller)) return;
            let operation, accepted = false;
            try {
                const reactor = invocation.context.ecx;
                operation = reactor.add(0x6C).readU32();
                const table = reactor.readPointer();
                if (!table.add(0x58).readPointer().equals(target)) return;
                if (excludedOperations.has(operation) || excludedTables.some(item => item.equals(table))) {
                    send({ event: "excluded", operation }); return;
                }
                if (selected !== null && !selected.has(operation)) return;
                accepted = true;
                const value = args[0];
                const length = value.add(0x10).readU32();
                const capacity = value.add(0x14).readU32();
                if (!length || length > config.maxBytes || length > capacity || (capacity < 16 && length > 15)) {
                    send({ event: "rejected", operation, code: "invalid_length" }); return;
                }
                const data = capacity < 16 ? value : value.readPointer();
                if (data.isNull()) throw new Error("null_data");
                const raw = data.readUtf8String(length);
                if (utf8Length(raw) !== length) throw new Error("length_mismatch");
                let clean;
                try {
                    clean = filterResponseXml(raw, config.rules);
                } catch (_) {
                    const code = raw.replace(/^﻿/, "").trimStart().startsWith("<") ? "invalid_response_xml" : "not_xml";
                    send({ event: "rejected", operation, code }); return;
                }
                const bytes = utf8Length(clean.xml);
                if (bytes > config.maxBytes) {
                    send({ event: "rejected", operation, code: "output_too_large" }); return;
                }
                const buffer = Memory.allocUtf8String(clean.xml).readByteArray(bytes);
                send({ event: "response", operation, sequence: ++sequence, length: bytes, originalLength: length,
                    functionAddress: target.toString(), returnAddress: invocation.returnAddress.toString(),
                    vtable: table.toString(), removed: clean.removed, removedRows: clean.removedRows }, buffer);
            } catch (_) {
                if (accepted) send({ event: "rejected", operation, code: "invalid_response_payload" });
            }
        }

        for (const consumer of config.consumers) {
            const target = module.base.add(consumer.rva);
            listeners.push(Interceptor.attach(target, { onEnter(args) { capture(this, args, target); } }));
        }
        send({ event: "ready", base: module.base.toString(), pointerSize: 4,
            consumers: config.consumers.map(consumer => module.base.add(consumer.rva).toString()) });
    } catch (_) {
        stop("setup_failed");
    }
})();
