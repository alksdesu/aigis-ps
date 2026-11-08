"use strict";

(() => {
    let listener = null, stopped = false, sequence = 0;
    const config = SCHEDULE_CAPTURE_CONFIG;
    function stop(code) {
        if (stopped) return;
        stopped = true;
        if (listener !== null) listener.detach();
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
        for (const entry of Object.values(config.operations)) {
            const table = module.base.add(entry.vtableRva);
            for (const slot of entry.slots) {
                if (!table.add(slot.offset).readPointer().equals(module.base.add(slot.rva))) {
                    stop("vtable_mismatch"); return;
                }
            }
        }
        const target = module.base.add(config.functionRva);
        const caller = module.base.add(config.returnRva);
        listener = Interceptor.attach(target, {
            onEnter(args) {
                if (stopped || !this.returnAddress.equals(caller)) return;
                let operation;
                try {
                    const reactor = this.context.ecx;
                    operation = reactor.add(0x6C).readU32();
                    const entry = config.operations[operation];
                    if (entry === undefined || !reactor.readPointer().equals(module.base.add(entry.vtableRva))) return;
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
                    const clean = filterScheduleXml(raw, entry.groups, config.privateFields);
                    if (!clean.groups.includes(entry.required)) {
                        send({ event: "rejected", operation, code: "missing_schedule_group" }); return;
                    }
                    const bytes = utf8Length(clean.xml);
                    if (bytes > config.maxBytes) throw new Error("output_too_large");
                    const buffer = Memory.allocUtf8String(clean.xml).readByteArray(bytes);
                    send({ event: "response", operation, sequence: ++sequence, length: bytes,
                        originalLength: length, functionAddress: target.toString(),
                        returnAddress: this.returnAddress.toString(), vtable: reactor.readPointer().toString() }, buffer);
                } catch (_) {
                    if (config.operations[operation] !== undefined) send({ event: "rejected", operation, code: "invalid_schedule_payload" });
                }
            }
        });
        send({ event: "ready", base: module.base.toString(), functionAddress: target.toString(), pointerSize: 4 });
    } catch (_) {
        stop("setup_failed");
    }
})();
