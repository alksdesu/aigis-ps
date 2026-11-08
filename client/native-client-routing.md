# Native client routing and preparation

Preparation supports two explicit source profiles for `Aigis_r.exe`, both 13,175,680 bytes. It selects the profile by the complete source SHA256 and never edits the source. Unknown or modified versions are rejected.

| Profile | Source SHA256 | API file offset | CDN file offset |
|---|---|---|---|
| Current 5.0.1 | `d0be510fb38b1d6dfe63c3562c97e45f49d318bf7e155dee309d88efe963f92b` | `0xA6D8F0` | `0xA6D90C` |
| Legacy | `babe8210a8652fb25b1c61056ee500b05bd9a3ae37f55d1682a2fb8b850802ba` | `0xA6D674` | `0xA6D690` |

| Original prefix | Replacement | Bytes |
|---|---|---:|
| `https://millennium-war.net/` | `http://127.0.0.1:18103/api/` | 27 |
| `https://drc1bk94f7rq8.cloudfront.net/` | `http://127.0.0.1:18103/client-assets/` | 37 |

Both original NUL terminators stay in place. The target executable SHA256 is recorded in its preparation manifest. Whole-file fingerprints, exact offsets, unique literals, and ASCII/UTF-16 host inventories are checked before patching. All four pinned root bootstrap resources have identical hashes across the two profiles.

The current source preserves all 156 protocol route literals and the complete metadata initializer at `0x61DB20` through `0x623CC5`. Checked constructor, registration, route transformation and common request/response blocks are also byte-identical; evidence is in `tmp/probe/new-resources/client-version-compatibility.json`. The consumer-address inventory below comes from the legacy static audit and is not a list of current-version hook offsets.

The 2026-11-06 resource upgrade is active: master snapshot `9159442edcc59db92d37856e` contains 2932 CardData rows and 3524 QuestTable rows; resource version `86164496048d9b4329259cfb248a9d3ff283bb36` has 24,019 verified entries independently stored under `runtime/resources/.cache`. The upgrade backup is `runtime/backups/client-upgrade-20261106T130021Z`. The new isolated client has been prepared, verified and published to `runtime/client`. Its target SHA256 is `48ab67a9d3397637f43c716a58ec7ba4745442288f245efb652ffa0d0ff28f96`; its effective resource version is `6d9554288063047475ca96a4626468621480190f`, with 24,019 entries and 11,519,326,920 body bytes. The previous client is preserved at `runtime/client.backup-20261106T133440Z-7b08302e`. The final offline doctor reports `source_supported=true`, `startup_ready=true` and `not_ready=[]`. The game and server were not started, and the production SQLite database was not read. Reports are `runtime/client-upgrade.json` and `tmp/probe/new-resources/prepared-client-upgrade.json`.

## Resource requests

The native PC index URL becomes `/client-assets/{AssetPath}/5nhhyzpc90mlwd9i06s8nfz781im220y`. `AssetPath` remains the captured 40-hex version, not a URL. Function `0x618920` composes the index URL from the shared CDN root, version, and leaf. Object URLs become `/client-assets/{group}/{md5}`. Index HTTP bodies must retain their XOR `0xDA` representation. Resource HTTP bodies retain the original ALL4/ALAR/ALTB or other container bytes. HTTP transfer gzip is separate from resource compression.

Function `0x642C10` also requests `/client-assets/000/_GameInfoText`. Its observed body is an ALTB table tagged `gitx`, 23 ordered records, each with one `Message` string-reference field. It is passed to `0xB01FD0` and wrapped as `GameInfoTextTable`; arbitrary announcement text cannot replace this binary table. Individual message-slot consumers have not all been traced, so changing slot order or inventing success messages is outside this contract.

Function `0xA9F880` registers six fixed bootstrap paths that are absent from the active PC index. Their verified independent copies and public source metadata are stored in `runtime/resources/native-bootstrap/manifest.json`. Original root resources remain copied into the isolated client using their exact pinned SHA256 values: the PC font and SoundSeFix bytes differ from their fixed CDN counterparts, so a route must not label the original files with the fixed object MD5.

## Shared CDN consumers

The one shared constant has 45 direct references in 44 functions. `0xA9DAB0` returns this same pointer to indirect users. No consumer requires a shorter prefix; native function `0x642C10` explicitly copies `0x25` bytes. Exact xref and decompilation evidence is in `artifacts/cdn-contract-20261101/native/`.

| Function | Reference instructions |
|---|---|
| `0x432360` | `0x4324a7` |
| `0x4aab80` | `0x4aacb9` |
| `0x5d5ae0` | `0x5d5ea6` |
| `0x5e2720` | `0x5e278b` |
| `0x5e4d60` | `0x5e4e86` |
| `0x5ec040` | `0x5ec09e` |
| `0x602bd0` | `0x602c20` |
| `0x618920` | `0x6189b1`, `0x618a9f` |
| `0x642c10` | `0x642c85` |
| `0x6835d0` | `0x683783` |
| `0x7078e0` | `0x707981` |
| `0x70c850` | `0x70c90d` |
| `0x7458a0` | `0x745aa8` |
| `0x74d9f0` | `0x74dae6` |
| `0x760c40` | `0x760e68` |
| `0x7610c0` | `0x7612bd` |
| `0x7614c0` | `0x761724` |
| `0x7650e0` | `0x7652b8` |
| `0x7692d0` | `0x7693c6` |
| `0x7ab4f0` | `0x7ab5cc` |
| `0x7abc60` | `0x7abc95` |
| `0x7af850` | `0x7af8cf` |
| `0x7afab0` | `0x7afb2f` |
| `0x7be670` | `0x7be704` |
| `0x7be820` | `0x7be869` |
| `0x7ce1e0` | `0x7ce25a` |
| `0x7ce610` | `0x7ce693` |
| `0x7ceb20` | `0x7ceb9a` |
| `0x7d8570` | `0x7d86ea` |
| `0x7e0390` | `0x7e0502` |
| `0x7ec130` | `0x7ec680` |
| `0x7f61f0` | `0x7f636a` |
| `0x89e300` | `0x89e3df` |
| `0x8b7de0` | `0x8b7e5b` |
| `0x8b9950` | `0x8b99d4` |
| `0x8bd1b0` | `0x8bd2b1` |
| `0x8cb880` | `0x8cb93b` |
| `0x8d7eb0` | `0x8d7f29` |
| `0x8e4b80` | `0x8e4bcc` |
| `0x94e110` | `0x94e47c` |
| `0x9e5720` | `0x9e574a` |
| `0xa13270` | `0xa132cc` |
| `0xa975f0` | `0xa978f8` |
| `0xa9dab0` | `0xa9dab0` |

## Independent cache preparation

Run `client/prepare-client.ps1 -InspectOnly` to check the exact source executable, four bootstrap files, active PC index, and size/presence of every indexed object. Inspection does not copy files and does not claim to hash-verify every cache body. Run `client/prepare-client.ps1` to perform preparation. `-ResourceDirectory` can select another completed library under the workspace. Neither action starts the client or contacts a network service. Both client wrappers dot-source `environment.ps1` like the other entry scripts: they run only the project `.venv` interpreter (running `setup.ps1` first when it is missing), keep TEMP/TMP, pip and bytecode caches inside the project, and the game started by `start-client.ps1` inherits the project `tmp` as its TEMP/TMP.

Preparation holds the resource-library operation lock and a separate lock for the target client directory. It freezes one active index, copies the four pinned root resources and non-indexed original cache files, and reads every indexed object through `IndexedAssetService` (overlay, original cache, original root). Each selected body must match index size and MD5; the destination is independently written and SHA256 checked. Its `.md5` companion is UTF-8 BOM, 32 lowercase MD5 digits, and CRLF. Source credentials, resource jobs, indexes, and locks are not copied as game files. No hardlinks or junctions are used.

A schema-2 preparation manifest records both patch ranges, source/target executable fingerprints, URL inventories, pinned resource snapshot, and copied file hashes. All indexed objects are required; missing or corrupt resources abort before publication. A temporary sibling directory is published only after verification, with a final active-index identity check. The operation requires enough disk for the full new copy plus a 2 GiB free-space margin.

An existing verified client with the same source profile and resource snapshot is reused without changing its mutable cache. A new source profile or resource snapshot, a verified legacy API-only client, or `-Refresh` prepares a complete new directory, then retains the old directory as `client.backup-<UTC>-<random>`. If publishing the new directory fails, the old name is restored. Unknown or modified executables and bootstrap files are rejected without overwriting them. Existing caches are never deleted to make room.

`client/start-client.ps1 -DryRun` verifies the prepared client without contacting the server or starting a process. Actual launch first requires the local health service identity and `native_cdn.ready=true`, `native_cdn.path=/client-assets/`, and a resource version equal to the preparation manifest. Runtime cache contents remain mutable after initial verification; the manifest fingerprints document the copied version rather than forbidding future native cache writes.

## Verification

Fixture tests cover both patch ranges, unexpected URL literals, source identity, cache fallback and corruption, native MD5 companions, all-or-nothing publication, old-cache preservation, legacy migration, rollback, workspace boundaries, file independence, concurrent operation locks, index changes, disk limits, and the launch health contract. Real validation patches the source bytes only in memory and confirms the expected digest. PowerShell wrappers are parsed without execution. No game or service is started by these checks.
