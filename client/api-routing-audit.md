# PC API routing audit

Source: `aigisr_exe/Aigis_r.exe`, SHA256 `babe8210a8652fb25b1c61056ee500b05bd9a3ae37f55d1682a2fb8b850802ba`.

The executable contains one ASCII API URL literal: `https://millennium-war.net/`, at file offset `0xA6D674`, RVA `0xA6E874`, preferred VA `0xE6E874`. No additional absolute URL using this host or `all.millennium-war.net` was found in ASCII, UTF-16LE, or UTF-16BE strings. This conclusion covers literal URLs in this exact executable; it does not claim to inspect encrypted data or resource-script URLs.

The replacement `http://127.0.0.1:18103/api/` has the same 27-byte length. The existing NUL terminator is preserved. The CDN prefix now has a separate audited 37-byte replacement, described in [native-client-routing.md](native-client-routing.md). DMM, website, and signing-certificate URLs remain outside both patch ranges.

## Direct `foi6moes` request

`sub_65BC00` uses the same shared prefix. It does not contain a separate full URL literal:

```text
0065BEA1 push 0xDD293C     ; foi6moes
0065BEA6 push 0xE6E874    ; shared API prefix
0065BEAB lea eax, [ebp-0x90]
0065BEB1 push 0xDC1EBC    ; %s%s
0065BEB6 push eax
0065BEB7 call 0x405810
0065BECF call 0xAF9240
```

The shared-prefix patch produces `http://127.0.0.1:18103/api/foi6moes` for this request. Its server route and payload contract require separate handling because it bypasses the common endpoint metadata table.

## Shared-prefix references

All 20 immediate references to preferred VA `0xE6E874` were matched to decoded instructions. Addresses below identify the instruction, not the immediate operand.

| Function | Reference instructions | Use |
|---|---|---|
| `0x406FC0` | `0x40715A` | `%s%s` formatter |
| `0x44E390` | `0x44E672` | `%s%s` formatter |
| `0x624170` | `0x6244E3` | `%s%s` formatter |
| `0x624D80` | `0x624DD2` | Common endpoint formatter |
| `0x6432E0` | `0x6433EE` | `%s%s` formatter |
| `0x644600` | `0x6447AD` | `%s%s` formatter |
| `0x65BC00` | `0x65BEA6` | Direct `foi6moes` formatter |
| `0x6608D0` | `0x660B2D` | `%s%s` formatter |
| `0x677E90` | `0x678183` | `%s%s` formatter |
| `0x8E4CC0` | `0x8E4D7B`, `0x8E4F02`, `0x8E5083`, `0x8E511F`, `0x8E51BB`, `0x8E5257`, `0x8E5342`, `0x8E53DE`, `0x8E547A` | String constructors with explicit length `0x1B` |
| `0xA9DAA0` | `0xA9DAA0` | Returns the shared prefix pointer |
| `0xA9DAD0` | `0xA9DD39` | String constructor with explicit length `0x1B` |

Equal-length replacement preserves both NUL-terminated formatter use and all ten explicit-length constructors. Indirect users of the returned pointer receive the same patched storage.

## Preparation checks

Preparation checks the whole-file SHA256, original bytes, exact offset, unique prefix, terminator, and an inventory of literal API URLs. An additional direct, `all`-host, HTTP, uppercase, or UTF-16 API URL is rejected rather than silently left pointing at the original service. The manifest records the verified literal inventory alongside the byte patch and file hashes.

The earlier API-only executable had SHA256 `99bfcf60826cf5df4f9cad1f0ff8a82def2073ff230fca0720f32a8b6f990d27`. The current API-and-CDN executable has SHA256 `453e2eaa60f3e8b6a446b298b5109c02129855258e3c25fb636e2ea76f1041cf`. Tests use fixtures and source inspection only. The game was not launched for this audit.
