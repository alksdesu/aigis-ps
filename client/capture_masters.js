"use strict";

(() => {
    let listener = null;
    let stopped = false;
    const captured = new Set();

    function fail(code) {
        if (stopped) return;
        stopped = true;
        if (listener !== null) listener.detach();
        send({ event: "error", code });
    }

    function matches(address, expected) {
        const actual = new Uint8Array(address.readByteArray(expected.length));
        return expected.every((value, index) => actual[index] === value);
    }

    try {
        const config = CAPTURE_CONFIG;
        const module = Process.mainModule;
        const normalize = path => path.replace(/\//g, "\\").toLowerCase();
        if (Process.platform !== "windows" || Process.arch !== "ia32" || Process.pointerSize !== 4 ||
            normalize(module.path) !== normalize(config.exePath)) {
            fail("unsupported_process");
            return;
        }
        const target = module.base.add(config.functionRva);
        if (!matches(target, config.functionBytes)) {
            fail("signature_mismatch");
            return;
        }
        const callers = new Map();
        for (const [name, entry] of Object.entries(config.tables)) {
            const call = module.base.add(entry.callRva);
            if (!matches(call, entry.callBytes)) {
                fail("signature_mismatch");
                return;
            }
            callers.set(call.add(5).toString(), name);
        }
        // Frida's ia32 arguments exclude the this pointer in ECX.
        listener = Interceptor.attach(target, {
            onEnter(args) {
                if (stopped) return;
                const name = callers.get(this.returnAddress.toString());
                if (name === undefined || captured.has(name)) return;
                const length = args[1].toUInt32();
                if (args[0].isNull() || length === 0 || length > config.maxBytes) {
                    fail("invalid_length");
                    return;
                }
                let data;
                try {
                    data = args[0].readByteArray(length);
                    if (data === null || data.byteLength !== length) {
                        fail("read_failed");
                        return;
                    }
                } catch (_) {
                    fail("read_failed");
                    return;
                }
                captured.add(name);
                send({ event: "master", table: name, length,
                    returnAddress: this.returnAddress.toString(), functionAddress: target.toString() }, data);
                if (captured.size === Object.keys(config.tables).length) {
                    stopped = true;
                    listener.detach();
                    send({ event: "complete" });
                }
            }
        });
        send({ event: "ready", base: module.base.toString(), functionAddress: target.toString(), pointerSize: 4 });
    } catch (_) {
        fail("setup_failed");
    }
})();
