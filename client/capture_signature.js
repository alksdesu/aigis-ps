"use strict";

function captureSignatureMatches(base, block) {
    const expected = [...block.bytes];
    for (const entry of block.relocations || []) {
        if (!Number.isInteger(entry.offset) || entry.offset < 0 || entry.offset + 4 > expected.length ||
                !Number.isInteger(entry.rva) || entry.rva < 0 || entry.rva > 0xFFFFFFFF) {
            throw new Error("invalid_signature_relocation");
        }
        const value = base.add(entry.rva).toUInt32();
        for (let index = 0; index < 4; index++) expected[entry.offset + index] = (value >>> (index * 8)) & 255;
    }
    const actual = new Uint8Array(base.add(block.rva).readByteArray(expected.length));
    return expected.every((value, index) => actual[index] === value);
}
