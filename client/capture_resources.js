"use strict";

(() => {
    let listener = null;
    let stopped = false;
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
        const config = RESOURCE_CAPTURE_CONFIG;
        const module = Process.mainModule;
        const normalize = path => path.replace(/\//g, "\\").toLowerCase();
        if (Process.platform !== "windows" || Process.arch !== "ia32" || Process.pointerSize !== 4 ||
            normalize(module.path) !== normalize(config.exePath)) {
            fail("unsupported_process");
            return;
        }
        const expected = [...config.functionBytes];
        for (const entry of config.relocations) {
            const value = module.base.add(entry.rva).toUInt32();
            for (let index = 0; index < 4; index++) expected[entry.offset + index] = (value >>> (index * 8)) & 255;
        }
        const target = module.base.add(config.functionRva);
        if (!matches(target, expected)) {
            fail("signature_mismatch");
            return;
        }
        const callers = new Set();
        for (const caller of config.callers) {
            const address = module.base.add(caller.rva);
            if (!matches(address, caller.bytes)) {
                fail("signature_mismatch");
                return;
            }
            callers.add(address.add(5).toString());
        }
        listener = Interceptor.attach(target, {
            onEnter(args) {
                if (stopped || !callers.has(this.returnAddress.toString())) return;
                try {
                    const raw = new Uint8Array(args[0].readByteArray(41));
                    if (raw[40] !== 0) {
                        fail("unexpected_asset_version");
                        return;
                    }
                    const version = String.fromCharCode(...raw.slice(0, 40));
                    if (!/^[a-f0-9]{40}$/.test(version)) {
                        fail("unexpected_asset_version");
                        return;
                    }
                    stopped = true;
                    send({ event: "version", version, returnAddress: this.returnAddress.toString() });
                    listener.detach();
                } catch (_) {
                    fail("read_failed");
                }
            }
        });
        send({ event: "ready", base: module.base.toString(), functionAddress: target.toString() });
    } catch (_) {
        fail("setup_failed");
    }
})();
