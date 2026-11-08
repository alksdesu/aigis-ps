import dataclasses
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from aigis_ps.asset_service import IndexedAssetService
from aigis_ps.al_format import parse_table
from aigis_ps.alxml import Column, Container, dumps
from aigis_ps.command_text_overlay import CommandTextOverrides
from aigis_ps.growth_campaign import CAMPAIGN_RESOURCE
from aigis_ps.local_present_templates import LOCAL_TEMPLATE_CARDS, build_present_template_overlay
from aigis_ps.masterdata import MasterDataStore
from aigis_ps.mission_window_revival import MissionWindowRules, verify_growth_campaign_windows
import aigis_ps.resource_view as resource_view
from aigis_ps.system_literals import command_id
from aigis_ps.trade_catalog import SHOP_FIELDS, SHOP_RESOURCE
from client import prepare_client as client
from test_cli import SCANNER_OVERFLOW_DEPTH
from test_growth_campaign_windows import FIRST as CAMPAIGN_MISSION, REOPENED as CAMPAIGN_REOPENED, campaign_world
from test_mission_window_revival import GOLD, REOPENED, world
from test_native_commands import commands
from test_shop_window_closure import shop_rows
from test_trade_catalog import product, swapped_fields, table as shop_table


REOPENED_BATCH = 'Reproduce:300010'
REPRODUCE_TABLE = 'ReproduceMissionConfig.atb'
COMMAND_TABLE = 'OnCommand.atb'
LATER_END = '2026-11-23T10:59:59+09:00'
RESOURCE_WINDOW = {'start_at': GOLD[0].isoformat(), 'end_at': GOLD[1].isoformat()}
OVERFLOWING_JSON = '[' * SCANNER_OVERFLOW_DEPTH + ']' * SCANNER_OVERFLOW_DEPTH
PYTHON_WRAPPERS = ('prepare-client.ps1', 'start-client.ps1', 'capture-content.ps1', 'capture-schedule.ps1')
ENVIRONMENT_SOURCE = ". (Join-Path $projectRoot 'environment.ps1')"
PROJECT_INTERPRETER = '$pythonPath = Initialize-AigisEnvironment -ProjectRoot $projectRoot'
PROJECT_LAUNCH = r'(?m)^& \$pythonPath @\w+$'
SYSTEM_INTERPRETER = r'(?im)Get-Command\s+python|^\s*(?:&\s*)?py(?:thon)?(?:\.exe)?\s'


class ClientPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary_root = client.PROJECT_ROOT / 'tmp' / 'client-tests'
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.source = self.workspace / 'original'
        self.runtime = self.workspace / 'runtime'
        self.resources = self.workspace / 'resources'
        self.source.mkdir()
        (self.source / '.cache' / 'nested').mkdir(parents=True)
        self.data = b'MZ\0fixture\0' + client.ORIGINAL_PREFIX + b'\0' + client.CDN_PREFIX + b'\0tail'
        self.profile = client.ClientProfile(
            client.digest(self.data), len(self.data), self.data.index(client.ORIGINAL_PREFIX),
            (('bootstrap', client.digest(b'resource')),),
            cdn_offset=self.data.index(client.CDN_PREFIX),
        )
        (self.source / client.EXECUTABLE).write_bytes(self.data)
        (self.source / 'bootstrap').write_bytes(b'resource')
        (self.source / 'credentials').write_bytes(b'not a resource')
        (self.source / '.cache' / 'nested' / 'asset').write_bytes(b'cache')
        self.content = b'ALL4 test resource'
        self.resource_name = 'TestResource.aar'
        self.resource_key = client.digest(self.resource_name.encode())
        self.resource_md5 = client.hashlib.md5(self.content).hexdigest()
        (self.resources / '.cache').mkdir(parents=True)
        (self.resources / '.cache' / self.resource_key).write_bytes(self.content)
        self.activate_index('1' * 40)

    def activate_index(self, version):
        self.write_index(f"{'a' * 40},{self.resource_md5},5,{len(self.content)},{self.resource_key},{self.resource_name}\n",
                         version)

    def index_resources(self, payloads, version='1' * 40):
        rows = []
        for name, payload in payloads.items():
            key = client.digest(name.encode())
            (self.resources / '.cache' / key).write_bytes(payload)
            rows.append(f"{'a' * 40},{client.hashlib.md5(payload).hexdigest()},5,{len(payload)},{key},{name}\n")
        self.write_index(''.join(rows), version)

    def write_index(self, rows, version):
        raw = bytes(value ^ 0xda for value in rows.encode())
        checksum = client.digest(raw)
        snapshot = version + '-' + checksum[:16]
        directory = self.resources / 'indexes' / snapshot
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'index.bin').write_bytes(raw)
        manifest = {'schema_version': 1, 'snapshot': snapshot, 'version': version, 'input_sha256': self.profile.sha256,
                    'index_name': '5nhhyzpc90mlwd9i06s8nfz781im220y', 'index_sha256': checksum}
        (directory / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        (self.resources / 'active-index.json').write_text(json.dumps({'snapshot': snapshot, 'index_sha256': checksum}), encoding='utf-8')

    def prepare(self, **kwargs):
        return client.prepare(self.source, self.runtime, self.workspace, self.profile, resource_root=self.resources, **kwargs)

    def activate_present_templates(self, master_root=None, *, missing_card=False):
        from test_local_present_templates import table_bytes
        self.content = table_bytes()
        self.resource_name = 'PresentUnitData.atb'
        self.resource_key = client.digest(self.resource_name.encode())
        self.resource_md5 = client.hashlib.md5(self.content).hexdigest()
        (self.resources / '.cache' / self.resource_key).write_bytes(self.content)
        self.activate_index('1' * 40)
        identifiers = (1, 99, 1605, 1777, 1785) + (() if missing_card else (2761,))
        documents = {
            'CardData': Container(children=(Column('CardID', 'I', identifiers),
                *(Column(name, 'I', (1,) * len(identifiers)) for name in ('InitClassID', 'ClassLV0SkillID', 'ClassLV1SkillID')))),
            'QuestTable': Container(children=tuple(Column(name, 'I', (1001,))
                for name in ('QuestID', 'MapNo', 'EntryNo', 'LocationNo', 'Capacity'))),
        }
        capture = self.workspace / 'public-capture'
        capture.mkdir()
        manifest = {'schema_version': 1, 'complete': True, 'input_sha256': self.profile.sha256, 'tables': {}}
        for name, document in documents.items():
            raw = dumps(document)
            (capture / (name + '.xml')).write_bytes(raw)
            manifest['tables'][name] = {'path': name + '.xml', 'bytes': len(raw), 'sha256': client.digest(raw)}
        (capture / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        master_root = master_root or self.workspace / client.MASTER_RELATIVE
        MasterDataStore(master_root, expected_hash=self.profile.sha256).import_capture(capture)
        return master_root, identifiers

    def test_equal_length_patch_changes_only_api_and_cdn(self):
        result = self.prepare()
        patched = (self.runtime / client.EXECUTABLE).read_bytes()
        offset = self.profile.prefix_offset
        self.assertEqual(len(patched), len(self.data))
        self.assertEqual(patched[:offset], self.data[:offset])
        self.assertEqual(patched[offset:offset + 27], client.LOCAL_PREFIX)
        restored = bytearray(patched)
        for record in client.patch_records(self.profile):
            start = record['offset']
            old = bytes.fromhex(record['original_hex'])
            new = bytes.fromhex(record['replacement_hex'])
            self.assertEqual(patched[start:start + len(new) + 1], new + b'\0')
            restored[start:start + len(old)] = old
        self.assertEqual(bytes(restored), self.data)
        self.assertEqual((self.source / client.EXECUTABLE).read_bytes(), self.data)
        self.assertNotIn(client.CDN_PREFIX, patched)
        self.assertIn(client.LOCAL_CDN_PREFIX, patched)
        self.assertFalse((self.runtime / 'credentials').exists())
        manifest = result['manifest']
        self.assertEqual(manifest['source_sha256'], self.profile.sha256)
        self.assertEqual(manifest['target_sha256'], client.digest(patched))
        self.assertEqual(len(manifest['files']), 5)
        self.assertEqual(manifest['patches'][0]['replacement_hex'], client.LOCAL_PREFIX.hex())
        self.assertEqual(manifest['patches'][1]['replacement_hex'], client.LOCAL_CDN_PREFIX.hex())
        self.assertEqual(manifest['resource_snapshot']['version'], '1' * 40)
        self.assertEqual(manifest['api_literal_inventory'], [{
            'offset': offset, 'encoding': 'ascii', 'url': client.ORIGINAL_PREFIX.decode('ascii'),
        }])

    def test_cache_is_copied_and_subsequent_prepare_preserves_user_changes(self):
        self.prepare()
        asset = self.runtime / '.cache' / 'nested' / 'asset'
        self.assertFalse(os.path.samefile(asset, self.source / '.cache' / 'nested' / 'asset'))
        asset.write_bytes(b'local changes')
        result = self.prepare()
        self.assertEqual(result['status'], 'already-prepared')
        self.assertEqual(asset.read_bytes(), b'local changes')
        self.assertEqual((self.source / '.cache' / 'nested' / 'asset').read_bytes(), b'cache')

    def test_unknown_source_is_rejected_without_creating_runtime(self):
        (self.source / client.EXECUTABLE).write_bytes(self.data + b'changed')
        with self.assertRaisesRegex(client.ClientError, 'Unknown or modified'):
            self.prepare()
        self.assertFalse(self.runtime.exists())

    def test_wrong_offset_duplicate_prefix_and_missing_terminator_are_rejected(self):
        with self.assertRaisesRegex(client.ClientError, 'offset'):
            client.patch_executable(self.data, dataclasses.replace(self.profile, prefix_offset=0))
        duplicate = self.data + client.ORIGINAL_PREFIX
        duplicate_profile = dataclasses.replace(self.profile, size=len(duplicate), sha256=client.digest(duplicate))
        with self.assertRaisesRegex(client.ClientError, 'exactly one original'):
            client.patch_executable(duplicate, duplicate_profile)
        broken = self.data.replace(client.ORIGINAL_PREFIX + b'\0', client.ORIGINAL_PREFIX + b'x')
        with self.assertRaisesRegex(client.ClientError, 'terminator'):
            client.patch_executable(broken, dataclasses.replace(self.profile, sha256=client.digest(broken)))

    def test_short_replacement_is_rejected(self):
        with patch.object(client, 'LOCAL_PREFIX', b'http://127.0.0.1:18103/'):
            with self.assertRaisesRegex(client.ClientError, 'same byte length'):
                client.patch_executable(self.data, self.profile)

    def test_unaccounted_direct_and_all_api_urls_are_rejected(self):
        candidates = (
            b'https://millennium-war.net/foi6moes',
            b'http://millennium-war.net/foi6moes',
            b'https://all.millennium-war.net/foi6moes',
            b'HTTPS://ALL.MILLENNIUM-WAR.NET/foi6moes',
            'https://millennium-war.net/foi6moes'.encode('utf-16le'),
            'https://all.millennium-war.net/foi6moes'.encode('utf-16be'),
        )
        for url in candidates:
            data = self.data + b'\0\0' + url + b'\0\0'
            profile = dataclasses.replace(self.profile, sha256=client.digest(data), size=len(data))
            with self.subTest(url=url):
                self.assertGreaterEqual(len(client.api_literal_inventory(data)), 2)
                with self.assertRaises(client.ClientError):
                    client.patch_executable(data, profile)

    def test_direct_request_uses_the_patched_shared_prefix(self):
        patched = client.patch_executable(self.data, self.profile)
        shared_prefix = patched[self.profile.prefix_offset:].split(b'\0', 1)[0]
        self.assertEqual(shared_prefix + b'foi6moes', b'http://127.0.0.1:18103/api/foi6moes')

    def test_literal_inventory_ignores_other_hosts(self):
        urls = b'\0https://contact.games.dmm.co.jp/game/contact/aigis\0http://millennium-war.net.example.invalid/\0'
        inventory = client.api_literal_inventory(self.data + urls)
        self.assertEqual(len(inventory), 1)

    def test_modified_runtime_is_never_overwritten(self):
        self.prepare()
        executable = self.runtime / client.EXECUTABLE
        executable.write_bytes(executable.read_bytes() + b'user change')
        with self.assertRaises(client.ClientError):
            self.prepare()
        self.assertTrue(executable.read_bytes().endswith(b'user change'))

    def test_modified_resource_is_rejected(self):
        self.prepare()
        resource = self.runtime / 'bootstrap'
        resource.write_bytes(b'user change')
        with self.assertRaisesRegex(client.ClientError, 'bootstrap resource'):
            self.prepare()
        self.assertEqual(resource.read_bytes(), b'user change')

    def test_existing_unmanaged_directory_is_not_overwritten(self):
        self.runtime.mkdir()
        (self.runtime / 'keep').write_bytes(b'keep')
        with self.assertRaisesRegex(client.ClientError, 'manifest'):
            self.prepare()
        self.assertEqual((self.runtime / 'keep').read_bytes(), b'keep')

    def test_paths_outside_workspace_and_overlapping_source_are_rejected(self):
        with self.assertRaisesRegex(client.ClientError, 'inside'):
            client.prepare(self.source, self.workspace.parent / 'outside', self.workspace, self.profile)
        with self.assertRaisesRegex(client.ClientError, 'separate'):
            client.prepare(self.source, self.source / 'copy', self.workspace, self.profile, resource_root=self.resources)
        with self.assertRaisesRegex(client.ClientError, 'separate'):
            client.prepare(self.source, self.workspace, self.workspace, self.profile, resource_root=self.resources)

    def test_failure_during_copy_leaves_source_and_no_partial_runtime(self):
        with patch.object(client.shutil, 'copyfile', side_effect=OSError('copy failed')):
            with self.assertRaisesRegex(OSError, 'copy failed'):
                self.prepare()
        self.assertFalse(self.runtime.exists())
        self.assertFalse(list(self.workspace.glob('.runtime.prepare-*')))
        self.assertEqual((self.source / client.EXECUTABLE).read_bytes(), self.data)

    def test_runtime_hardlinked_cache_is_rejected(self):
        self.prepare()
        asset = self.runtime / '.cache' / 'nested' / 'asset'
        asset.unlink()
        os.link(self.source / '.cache' / 'nested' / 'asset', asset)
        with self.assertRaisesRegex(client.ClientError, 'independent files'):
            client.verify_runtime(self.runtime, self.workspace, self.profile)

    def test_source_symlink_is_rejected(self):
        link = self.source / '.cache' / 'link'
        try:
            link.symlink_to(self.source / 'bootstrap')
        except OSError as error:
            self.skipTest(f'Symlinks unavailable: {error}')
        with self.assertRaisesRegex(client.ClientError, 'Links and reparse'):
            self.prepare()

    def test_changed_manifest_patch_is_rejected(self):
        self.prepare()
        path = self.runtime / client.MANIFEST
        manifest = json.loads(path.read_text('utf-8'))
        manifest['patches'][0]['replacement_url'] = 'http://other.invalid/'
        path.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaisesRegex(client.ClientError, 'unknown client or patch'):
            client.verify_runtime(self.runtime, self.workspace, self.profile)

    def test_unreadable_cache_is_not_silently_omitted(self):
        original_iterdir = Path.iterdir

        def fail_nested(path):
            if path.name == 'nested':
                raise PermissionError('cache unreadable')
            return original_iterdir(path)

        with patch.object(Path, 'iterdir', fail_nested):
            with self.assertRaisesRegex(PermissionError, 'cache unreadable'):
                self.prepare()
        self.assertFalse(self.runtime.exists())

    def test_changed_source_during_copy_is_rejected(self):
        original_copy = client.shutil.copyfile

        def mutate_source(source, destination):
            result = original_copy(source, destination)
            source.write_bytes(b'changed during copy')
            return result

        with patch.object(client.shutil, 'copyfile', mutate_source):
            with self.assertRaisesRegex(client.ClientError, 'Source changed'):
                self.prepare()
        self.assertFalse(self.runtime.exists())

    def test_cdn_wrong_offset_duplicate_missing_nul_and_short_replacement(self):
        with self.assertRaises(client.ClientError):
            client.patch_executable(self.data, dataclasses.replace(self.profile, cdn_offset=0))
        for data in (self.data + b'\0' + client.CDN_PREFIX,
                     self.data.replace(client.CDN_PREFIX + b'\0', client.CDN_PREFIX + b'x'),
                     self.data + b'\0\0' + 'https://drc1bk94f7rq8.cloudfront.net/other'.encode('utf-16le')):
            profile = dataclasses.replace(self.profile, size=len(data), sha256=client.digest(data))
            with self.subTest(data=data[-30:]), self.assertRaises(client.ClientError):
                client.patch_executable(data, profile)
        with patch.object(client, 'LOCAL_CDN_PREFIX', b'http://127.0.0.1/'), self.assertRaisesRegex(client.ClientError, 'same byte length'):
            client.patch_executable(self.data, self.profile)

    def test_native_index_and_object_paths_use_shared_cdn_replacement(self):
        patched = client.patch_executable(self.data, self.profile)
        prefix = patched[self.profile.cdn_offset:].split(b'\0', 1)[0]
        for suffix in (b'1' * 40 + b'/5nhhyzpc90mlwd9i06s8nfz781im220y', b'a' * 40 + b'/' + b'b' * 32,
                       b'000/_GameInfoText'):
            self.assertEqual(prefix + suffix, client.LOCAL_CDN_PREFIX + suffix)

    def test_indexed_cache_is_independent_and_has_native_md5_sidecar(self):
        events = []
        result = self.prepare(progress=events.append)
        copied = self.runtime / '.cache' / self.resource_key
        self.assertEqual(copied.read_bytes(), self.content)
        self.assertFalse(os.path.samefile(copied, self.resources / '.cache' / self.resource_key))
        self.assertEqual(copied.stat().st_nlink, 1)
        self.assertEqual(copied.with_suffix('.md5').read_bytes(), b'\xef\xbb\xbf' + self.resource_md5.encode() + b'\r\n')
        self.assertEqual(result['manifest']['resource_snapshot']['entries'], 1)
        self.assertEqual(events[-1]['event'], 'resources_copied')
        self.assertEqual(events[-1]['bytes'], len(self.content))
        self.assertFalse((self.runtime / 'indexes').exists())
        self.assertFalse((self.runtime / 'operation.lock').exists())

    def test_invalid_overlay_falls_back_to_valid_original_cache(self):
        overlay = self.resources / '.cache' / self.resource_key
        overlay.write_bytes(b'x' * len(self.content))
        (self.source / '.cache' / self.resource_key).write_bytes(self.content)
        (self.source / '.cache' / (self.resource_key + '.md5')).write_bytes(b'outdated sidecar')
        self.prepare()
        self.assertEqual((self.runtime / '.cache' / self.resource_key).read_bytes(), self.content)
        self.assertEqual(overlay.read_bytes(), b'x' * len(self.content))

    def test_missing_or_wrong_size_resource_prevents_publishing(self):
        path = self.resources / '.cache' / self.resource_key
        for content in (None, b'short'):
            if content is None:
                path.unlink()
            else:
                path.write_bytes(content)
            with self.assertRaisesRegex(client.ClientError, 'missing or has the wrong size'):
                self.prepare()
            self.assertFalse(self.runtime.exists())

    def test_same_size_corruption_prevents_publishing(self):
        (self.resources / '.cache' / self.resource_key).write_bytes(b'x' * len(self.content))
        with self.assertRaisesRegex(client.ClientError, 'verification failed'):
            self.prepare()
        self.assertFalse(self.runtime.exists())
        self.assertFalse(list(self.workspace.glob('.runtime.prepare-*')))

    def test_new_version_preserves_old_directory_and_local_cache(self):
        self.prepare()
        (self.runtime / '.cache' / self.resource_key).write_bytes(b'local cache edit')
        self.activate_index('2' * 40)
        result = self.prepare()
        backup = Path(result['backup'])
        self.assertTrue(backup.is_dir())
        self.assertEqual((backup / '.cache' / self.resource_key).read_bytes(), b'local cache edit')
        self.assertEqual((self.runtime / '.cache' / self.resource_key).read_bytes(), self.content)
        self.assertEqual(result['manifest']['resource_snapshot']['version'], '2' * 40)

    def test_refresh_preserves_existing_cache_in_backup(self):
        self.prepare()
        (self.runtime / '.cache' / 'keep').write_bytes(b'existing')
        result = self.prepare(refresh=True)
        self.assertEqual((Path(result['backup']) / '.cache' / 'keep').read_bytes(), b'existing')

    def test_legacy_api_only_runtime_is_verified_and_preserved_on_upgrade(self):
        self.prepare()
        executable = self.runtime / client.EXECUTABLE
        data = executable.read_bytes().replace(client.LOCAL_CDN_PREFIX, client.CDN_PREFIX)
        executable.write_bytes(data)
        path = self.runtime / client.MANIFEST
        manifest = json.loads(path.read_text('utf-8'))
        manifest.update(schema_version=1, patch=client.patch_record(self.profile), target_sha256=client.digest(data))
        manifest.pop('patches')
        path.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaises(client.ClientError):
            client.verify_runtime(self.runtime, self.workspace, self.profile)
        result = self.prepare()
        self.assertEqual((Path(result['backup']) / client.EXECUTABLE).read_bytes(), data)
        self.assertEqual(result['manifest']['schema_version'], 2)

    def test_publication_failure_restores_existing_directory(self):
        first = self.prepare()['manifest']
        original_rename = Path.rename

        def fail_publication(path, destination):
            if path.name.startswith('.runtime.prepare-') and destination == self.runtime:
                raise OSError('publication failed')
            return original_rename(path, destination)

        with patch.object(Path, 'rename', fail_publication), self.assertRaisesRegex(OSError, 'publication failed'):
            self.prepare(refresh=True)
        self.assertEqual(client.verify_runtime(self.runtime, self.workspace, self.profile), first)
        self.assertFalse(list(self.workspace.glob('runtime.backup-*')))

    def test_corrupt_new_version_does_not_modify_existing_runtime(self):
        first = self.prepare()['manifest']
        self.activate_index('2' * 40)
        (self.resources / '.cache' / self.resource_key).write_bytes(b'x' * len(self.content))
        with self.assertRaises(client.ClientError):
            self.prepare()
        self.assertEqual(client.verify_runtime(self.runtime, self.workspace, self.profile), first)

    def test_resource_collection_lock_prevents_preparation(self):
        with client.exclusive_file(self.resources / 'operation.lock'):
            with self.assertRaisesRegex(ValueError, 'Another resource'):
                self.prepare()
        self.assertFalse(self.runtime.exists())

    def test_parallel_preparation_lock_prevents_preparation(self):
        with client.exclusive_file(self.workspace / '.runtime.prepare.lock'):
            with self.assertRaisesRegex(ValueError, 'Another resource'):
                self.prepare()
        self.assertFalse(self.runtime.exists())

    def test_insufficient_space_prevents_copy(self):
        with patch.object(client.shutil, 'disk_usage', return_value=Mock(free=0)), self.assertRaisesRegex(client.ClientError, 'free disk'):
            self.prepare()
        self.assertFalse(self.runtime.exists())

    def test_index_change_during_copy_prevents_publishing(self):
        original_read = IndexedAssetService.read

        def changing_read(service, name):
            result = original_read(service, name)
            self.activate_index('2' * 40)
            return result

        with patch.object(IndexedAssetService, 'read', changing_read), self.assertRaisesRegex(client.ClientError, 'index changed'):
            self.prepare()
        self.assertFalse(self.runtime.exists())

    def test_resource_root_cannot_overlap_runtime(self):
        with self.assertRaisesRegex(client.ClientError, 'separate'):
            client.prepare(self.source, self.resources / 'client', self.workspace, self.profile, resource_root=self.resources)

    def test_invalid_snapshot_metadata_and_incomplete_inventory_are_rejected(self):
        original = self.prepare()['manifest']
        path = self.runtime / client.MANIFEST
        for field, value in (('index_name', 'other'), ('entries', True), ('total_bytes', -1), ('snapshot', '../escape')):
            manifest = json.loads(json.dumps(original))
            manifest['resource_snapshot'][field] = value
            path.write_text(json.dumps(manifest), encoding='utf-8')
            with self.subTest(field=field), self.assertRaises(client.ClientError):
                client.verify_runtime(self.runtime, self.workspace, self.profile)
        manifest = json.loads(json.dumps(original))
        manifest['files'] = [item for item in manifest['files'] if item['kind'] != 'indexed-resource']
        path.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaisesRegex(client.ClientError, 'does not cover'):
            client.verify_runtime(self.runtime, self.workspace, self.profile)

    def test_resource_without_present_templates_does_not_read_default_master_directory(self):
        with patch.object(client, 'MasterDataStore', side_effect=AssertionError('Unexpected master read')):
            result = self.prepare()
        self.assertEqual(result['manifest']['resource_snapshot']['version'], '1' * 40)

    def test_plan_uses_in_memory_replacement_size_and_effective_snapshot(self):
        _, identifiers = self.activate_present_templates()
        original = (self.resources / '.cache' / self.resource_key).read_bytes()
        replacement = build_present_template_overlay(original, identifiers)
        plan = client.preparation_plan(self.source, self.resources, self.workspace, self.profile)
        entry, = plan['entries']
        self.assertNotEqual(entry.size, len(original))
        self.assertEqual(entry.size, len(replacement))
        self.assertNotEqual(plan['resource_snapshot']['version'], '1' * 40)
        self.assertEqual(plan['resource_view'].assets.read(entry.name)[0], replacement)
        expected_size = len(plan['patched']) + sum(path.stat().st_size for path in plan['files']) + len(replacement) + 37
        self.assertEqual(plan['bytes_to_copy'], expected_size)
        self.assertFalse(self.runtime.exists())
        self.assertEqual((self.resources / '.cache' / self.resource_key).read_bytes(), original)

    def test_prepare_copies_effective_template_body_and_md5_without_changing_source(self):
        master_root, identifiers = self.activate_present_templates()
        replacement = build_present_template_overlay(self.content, identifiers)
        view = client.load_resource_view(self.resources, self.source, self.profile.sha256,
            master_provider=MasterDataStore(master_root, expected_hash=self.profile.sha256).get_xml)
        with patch.object(client, 'load_resource_view', wraps=client.load_resource_view) as loader:
            result = self.prepare()
        self.assertEqual(loader.call_count, 2)
        self.assertEqual(result['manifest']['resource_snapshot'], client.resource_snapshot(view.manifest, view.entries))
        copied = self.runtime / '.cache' / self.resource_key
        self.assertEqual(copied.read_bytes(), replacement)
        self.assertEqual(copied.with_suffix('.md5').read_bytes(), b'\xef\xbb\xbf' + client.hashlib.md5(replacement).hexdigest().encode() + b'\r\n')
        self.assertEqual(parse_table(copied.read_bytes())['record_count'], 5)
        self.assertEqual((self.resources / '.cache' / self.resource_key).read_bytes(), self.content)
        self.assertEqual((self.source / client.EXECUTABLE).read_bytes(), self.data)
        self.assertFalse((self.runtime / 'credentials').exists())
        self.assertEqual(client.verify_runtime(self.runtime, self.workspace, self.profile), result['manifest'])
        self.assertEqual(self.prepare()['status'], 'already-prepared')

    def test_explicit_public_master_directory_is_used(self):
        directory = self.workspace / 'separate-public-masters'
        self.activate_present_templates(directory)
        self.assertFalse((self.workspace / client.MASTER_RELATIVE).exists())
        result = self.prepare(master_root=directory)
        self.assertEqual(parse_table((self.runtime / '.cache' / self.resource_key).read_bytes())['record_count'], 5)
        self.assertNotEqual(result['manifest']['resource_snapshot']['version'], '1' * 40)

    def test_default_master_directory_follows_the_project_folder_name(self):
        self.assertEqual(client.WORKSPACE_ROOT / client.MASTER_RELATIVE,
                         client.PROJECT_ROOT / 'runtime' / 'server' / 'masterdata')

    def test_explicit_master_directory_must_remain_in_workspace(self):
        with self.assertRaisesRegex(client.ClientError, 'inside'):
            self.prepare(master_root=self.workspace.parent / 'outside-master')
        self.assertFalse(self.runtime.exists())

    def test_missing_overlay_card_prevents_publishing(self):
        self.activate_present_templates(missing_card=True)
        with self.assertRaisesRegex(ValueError, 'absent from verified CardData'):
            self.prepare()
        self.assertFalse(self.runtime.exists())
        self.assertFalse(list(self.workspace.glob('.runtime.prepare-*')))

    def test_effective_index_change_during_copy_preserves_existing_runtime(self):
        self.activate_present_templates()
        first = self.prepare()['manifest']
        old_copy = (self.runtime / '.cache' / self.resource_key).read_bytes()
        def change_index(event):
            if event['event'] == 'resources_copied':
                self.activate_index('2' * 40)
        with self.assertRaisesRegex(client.ClientError, 'index changed'):
            self.prepare(refresh=True, progress=change_index)
        self.assertEqual(client.verify_runtime(self.runtime, self.workspace, self.profile), first)
        self.assertEqual((self.runtime / '.cache' / self.resource_key).read_bytes(), old_copy)
        self.assertFalse(list(self.workspace.glob('runtime.backup-*')))
        self.assertFalse(list(self.workspace.glob('.runtime.prepare-*')))

    def test_reused_runtime_checks_the_effective_view_again_before_returning(self):
        self.activate_present_templates()
        first = self.prepare()['manifest']
        original_load = client.load_resource_view
        count = 0
        def changing_view(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                self.activate_index('2' * 40)
            return original_load(*args, **kwargs)
        with patch.object(client, 'load_resource_view', side_effect=changing_view), self.assertRaisesRegex(client.ClientError, 'index changed'):
            self.prepare()
        self.assertEqual(count, 2)
        self.assertEqual(client.verify_runtime(self.runtime, self.workspace, self.profile), first)

    def test_new_effective_view_keeps_previous_local_cache_in_backup(self):
        self.activate_present_templates()
        first = self.prepare()['manifest']
        copied = self.runtime / '.cache' / self.resource_key
        copied.write_bytes(b'old local cache')
        self.activate_index('2' * 40)
        result = self.prepare()
        self.assertNotEqual(result['manifest']['resource_snapshot'], first['resource_snapshot'])
        self.assertEqual((Path(result['backup']) / '.cache' / self.resource_key).read_bytes(), b'old local cache')
        self.assertEqual({row['PresentID'] for row in parse_table(copied.read_bytes())['rows']} & set(LOCAL_TEMPLATE_CARDS), set(LOCAL_TEMPLATE_CARDS))

    def test_manifest_nested_past_the_scanner_is_rejected_without_overwriting(self):
        self.runtime.mkdir()
        manifest = self.runtime / client.MANIFEST
        manifest.write_text(OVERFLOWING_JSON, encoding='utf-8')
        with self.assertRaisesRegex(client.ClientError, 'no valid preparation manifest'):
            client.verify_runtime(self.runtime, self.workspace, self.profile)
        self.assertEqual(manifest.read_text(encoding='utf-8'), OVERFLOWING_JSON)

    def test_rule_file_nested_past_the_scanner_fails_the_command_without_a_traceback(self):
        path = self.workspace / 'mission-windows.json'
        path.write_text(OVERFLOWING_JSON, encoding='utf-8')
        argv = ['prepare_client.py', 'verify', '--runtime', str(self.workspace / 'runtime' / 'client'),
                '--mission-windows', str(path)]
        errors = io.StringIO()
        with patch.object(client, 'PROJECT_ROOT', self.workspace), patch.object(client, 'WORKSPACE_ROOT', self.workspace), \
                patch.object(client, 'verify_runtime', side_effect=AssertionError('Rules load before verification')), \
                patch.object(client.sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(errors):
            self.assertEqual(client.main(), 1)
        self.assertIn('Client preparation failed', errors.getvalue())


class MissionWindowPreparationTests(unittest.TestCase):
    def setUp(self):
        self.case = ClientPreparationTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.index_resources(world())
        self.rules = MissionWindowRules(True, {REOPENED_BATCH: REOPENED})
        self.later = MissionWindowRules(True, {REOPENED_BATCH: {**REOPENED, 'end_at': LATER_END}})

    def prepare(self, rules=None, **kwargs):
        return self.case.prepare(mission_window_rules=rules, **kwargs)

    def view(self, rules=None):
        return client.load_resource_view(self.case.resources, self.case.source, self.case.profile.sha256,
                                         mission_window_rules=rules)

    def verify(self):
        return client.verify_runtime(self.case.runtime, self.case.workspace, self.case.profile)

    def test_rules_without_batches_keep_the_plain_snapshot_and_record_nothing(self):
        plain = self.prepare()['manifest']
        self.assertNotIn('mission_windows', plain)
        self.assertEqual(self.verify(), plain)
        for rules in (MissionWindowRules(), MissionWindowRules(False, {REOPENED_BATCH: REOPENED}),
                      MissionWindowRules(True, {})):
            with self.subTest(rules=rules):
                result = self.prepare(rules)
                self.assertEqual(result['status'], 'already-prepared')
                self.assertEqual(result['manifest'], plain)
                client._check_requested_mission_windows(plain, rules)
        with self.assertRaisesRegex(client.ClientError, 'differ'):
            client._check_requested_mission_windows(plain, self.rules)

    def test_plan_copy_and_tail_check_share_the_frozen_rules(self):
        plain, current = self.view(), self.view(self.rules)
        observed, original = [], client.load_resource_view

        def load(*args, **kwargs):
            observed.append(kwargs['mission_window_rules'])
            return original(*args, **kwargs)

        with patch.object(client, 'load_resource_view', load):
            manifest = self.prepare(self.rules)['manifest']
        self.assertEqual(len(observed), 2)
        self.assertTrue(all(rules is self.rules for rules in observed))
        self.assertEqual(manifest['mission_windows'], current.mission_window_metadata)
        self.assertEqual(manifest['resource_snapshot']['version'], current.manifest['version'])
        self.assertNotEqual(current.manifest['version'], plain.manifest['version'])
        copied = self.case.runtime / '.cache' / client.digest(REPRODUCE_TABLE.encode())
        self.assertEqual(copied.read_bytes(), current.replacements[REPRODUCE_TABLE])
        self.assertEqual(self.verify(), manifest)
        client._check_requested_mission_windows(manifest, self.rules)
        self.assertEqual(self.prepare(self.rules)['status'], 'already-prepared')

    def test_changed_rules_publish_a_new_snapshot_and_keep_the_old_runtime_as_backup(self):
        first = self.prepare()['manifest']
        result = self.prepare(self.rules)
        self.assertEqual(result['status'], 'prepared')
        self.assertNotEqual(result['manifest']['resource_snapshot'], first['resource_snapshot'])
        self.assertEqual(client.verify_runtime(Path(result['backup']), self.case.workspace, self.case.profile), first)

    def test_rules_keeping_the_resource_window_are_recorded_on_the_unchanged_snapshot(self):
        plain = self.prepare()['manifest']
        kept = MissionWindowRules(True, {REOPENED_BATCH: RESOURCE_WINDOW})
        result = self.prepare(kept)
        self.assertEqual(result['status'], 'prepared')
        self.assertEqual(result['manifest']['resource_snapshot'], plain['resource_snapshot'])
        self.assertEqual(result['manifest']['mission_windows'], self.view(kept).mission_window_metadata)
        client._check_requested_mission_windows(result['manifest'], kept)
        self.assertEqual(self.prepare(kept)['status'], 'already-prepared')

    def test_tampered_mission_window_metadata_is_rejected(self):
        manifest = self.prepare(self.rules)['manifest']
        table, = manifest['mission_windows']['tables']
        changes = {
            'schema_version': lambda record: record.update(schema_version=2),
            'rules_hash': lambda record: record.update(rules_hash='0' * 64),
            'window_policy': lambda record: record.update(window_policy='other'),
            'disabled_rules': lambda record: record['rules'].update(enabled=False),
            'batch_window': lambda record: record['batches'][0]['window'].update(end_at=LATER_END),
            'effective_sha256': lambda record: record['tables'][table].update(effective_sha256='0' * 64),
            'tables': lambda record: record['tables'].clear(),
        }
        path = self.case.runtime / client.MANIFEST
        for name, change in changes.items():
            altered = json.loads(json.dumps(manifest))
            change(altered['mission_windows'])
            path.write_text(json.dumps(altered), encoding='utf-8')
            with self.subTest(field=name), self.assertRaisesRegex(client.ClientError, 'mission window'):
                self.verify()

    def test_launch_compares_the_requested_rules_with_the_prepared_runtime(self):
        manifest = self.prepare(self.rules)['manifest']
        with patch.object(client, 'verify_runtime', return_value=manifest), \
                patch.object(client, 'check_server', side_effect=AssertionError('Dry launch contacted a server')), \
                patch.object(client.subprocess, 'Popen', side_effect=AssertionError('Dry launch started a process')):
            result = client.launch(self.case.runtime, dry_run=True, mission_window_rules=self.rules)
            self.assertEqual(result['status'], 'verified')
            for rules in (MissionWindowRules(), self.later):
                with self.subTest(rules=rules), self.assertRaisesRegex(client.ClientError, 'differ'):
                    client.launch(self.case.runtime, dry_run=True, mission_window_rules=rules)
        with self.assertRaisesRegex(client.ClientError, 'invalid'):
            client._check_requested_mission_windows(manifest, self.rules.to_dict())

    def test_verify_command_compares_the_requested_rules_with_the_prepared_runtime(self):
        manifest = self.prepare(self.rules)['manifest']
        path = self.case.workspace / 'mission-windows.json'
        argv = ['prepare_client.py', 'verify', '--runtime', str(self.case.workspace / 'runtime' / 'client'),
                '--mission-windows', str(path)]
        for rules, code in ((self.rules, 0), (self.later, 1)):
            path.write_text(json.dumps(rules.to_dict()), encoding='utf-8')
            with self.subTest(rules=rules), patch.object(client, 'PROJECT_ROOT', self.case.workspace),                     patch.object(client, 'WORKSPACE_ROOT', self.case.workspace),                     patch.object(client, 'verify_runtime', return_value=manifest),                     patch.object(client.sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()),                     contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(client.main(), code)

    def test_inspect_command_forwards_one_loaded_rule_file(self):
        path = self.case.workspace / 'mission-windows.json'
        path.write_text(json.dumps(self.rules.to_dict()), encoding='utf-8')
        loaded, original = [], client.load_mission_window_rules

        def load(value):
            loaded.append(value)
            return original(value)

        runtime = self.case.workspace / 'runtime' / 'client'
        argv = ['prepare_client.py', 'inspect', '--source', str(self.case.source), '--runtime', str(runtime),
                '--resource-dir', str(self.case.resources), '--mission-windows', str(path)]
        output = io.StringIO()
        with patch.object(client, 'PROJECT_ROOT', self.case.workspace), \
                patch.object(client, 'WORKSPACE_ROOT', self.case.workspace), \
                patch.object(client, 'PROFILE', self.case.profile), \
                patch.object(client, 'load_mission_window_rules', load), \
                patch.object(client.sys, 'argv', argv), contextlib.redirect_stdout(output):
            self.assertEqual(client.main(), 0)
        self.assertEqual(loaded, [path])
        self.assertEqual(json.loads(output.getvalue())['mission_windows'], self.view(self.rules).mission_window_metadata)
        self.assertFalse(runtime.exists())

    def test_powershell_wrapper_forwards_the_mission_window_file(self):
        script = Path(client.__file__).with_name('prepare-client.ps1').read_text('utf-8')
        self.assertIn("[string]$MissionWindowsFile = ''", script)
        self.assertIn("@('--mission-windows', $MissionWindowsFile)", script)


class GrowthCampaignPreparationTests(unittest.TestCase):
    def setUp(self):
        self.case = ClientPreparationTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.index_resources(world() | campaign_world())
        self.campaign = str(CAMPAIGN_MISSION)
        self.rules = MissionWindowRules(True, {REOPENED_BATCH: REOPENED}, {self.campaign: CAMPAIGN_REOPENED})

    def prepare(self, rules=None, **kwargs):
        return self.case.prepare(mission_window_rules=rules, **kwargs)

    def view(self, rules=None):
        return client.load_resource_view(self.case.resources, self.case.source, self.case.profile.sha256,
                                         mission_window_rules=rules)

    def verify(self):
        return client.verify_runtime(self.case.runtime, self.case.workspace, self.case.profile)

    def test_growth_windows_share_the_served_patch_with_the_server(self):
        current = self.view(self.rules)
        manifest = self.prepare(self.rules)['manifest']
        record = manifest['mission_windows']
        self.assertEqual(record, current.mission_window_metadata)
        copied = self.case.runtime / '.cache' / client.digest(CAMPAIGN_RESOURCE.encode())
        self.assertEqual(copied.read_bytes(), current.replacements[CAMPAIGN_RESOURCE])
        report = verify_growth_campaign_windows(self.rules, lambda name: current.assets.read(name)[0])
        self.assertTrue(report['verification']['verified'])
        self.assertEqual(report['verification']['served_sha256'], record['growth_campaigns']['effective_sha256'])
        self.assertEqual(self.verify(), manifest)
        client._check_requested_mission_windows(manifest, self.rules)
        self.assertEqual(self.prepare(self.rules)['status'], 'already-prepared')

    def test_campaign_only_rules_record_their_patch(self):
        only = MissionWindowRules(True, {}, {self.campaign: CAMPAIGN_REOPENED})
        manifest = self.prepare(only)['manifest']
        record = manifest['mission_windows']
        self.assertEqual(record['batches'], [])
        self.assertEqual(record, self.view(only).mission_window_metadata)
        self.assertEqual(self.verify(), manifest)
        client._check_requested_mission_windows(manifest, only)
        self.assertEqual(self.prepare(only)['status'], 'already-prepared')

    def test_tampered_campaign_metadata_is_rejected(self):
        manifest = self.prepare(self.rules)['manifest']
        changes = {
            'window': lambda record: record['growth_campaigns']['missions'][0]['window'].update(end_at=LATER_END),
            'effective_sha256': lambda record: record['growth_campaigns'].update(effective_sha256='0' * 64),
            'orphaned': lambda record: record.pop('growth_campaigns'),
            'table': lambda record: record['growth_campaigns'].update(table='Other.atb'),
        }
        path = self.case.runtime / client.MANIFEST
        for name, change in changes.items():
            altered = json.loads(json.dumps(manifest))
            change(altered['mission_windows'])
            path.write_text(json.dumps(altered), encoding='utf-8')
            with self.subTest(field=name), self.assertRaisesRegex(client.ClientError, 'mission window'):
                self.verify()


class CommandTextPreparationTests(unittest.TestCase):
    def setUp(self):
        self.case = ClientPreparationTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.index_resources({COMMAND_TABLE: commands(('First', '1;'), ('Second', '0;'))})
        self.rules = CommandTextOverrides({COMMAND_TABLE: {str(command_id('First')): '0;'}})
        self.kept = CommandTextOverrides({COMMAND_TABLE: {str(command_id('First')): '1;'}})

    def prepare(self, rules=None, **kwargs):
        return self.case.prepare(command_text_overrides=rules, **kwargs)

    def view(self, rules=None):
        return client.load_resource_view(self.case.resources, self.case.source, self.case.profile.sha256,
                                         command_text_overrides=rules)

    def verify(self):
        return client.verify_runtime(self.case.runtime, self.case.workspace, self.case.profile)

    def test_overrides_patch_the_copied_table_and_record_their_metadata(self):
        current = self.view(self.rules)
        manifest = self.prepare(self.rules)['manifest']
        self.assertEqual(manifest['command_texts'], current.command_text_metadata)
        copied = self.case.runtime / '.cache' / client.digest(COMMAND_TABLE.encode())
        self.assertEqual(copied.read_bytes(), current.replacements[COMMAND_TABLE])
        self.assertNotEqual(manifest['resource_snapshot']['version'], self.view().manifest['version'])
        self.assertEqual(self.verify(), manifest)
        client._check_requested_command_texts(manifest, self.rules)
        self.assertEqual(self.prepare(self.rules)['status'], 'already-prepared')

    def test_overrides_keeping_the_source_text_record_on_the_unchanged_snapshot(self):
        plain = self.prepare()['manifest']
        self.assertNotIn('command_texts', plain)
        client._check_requested_command_texts(plain, CommandTextOverrides())
        result = self.prepare(self.kept)
        self.assertEqual(result['status'], 'prepared')
        self.assertEqual(result['manifest']['resource_snapshot'], plain['resource_snapshot'])
        client._check_requested_command_texts(result['manifest'], self.kept)
        with self.assertRaisesRegex(client.ClientError, 'differ'):
            client._check_requested_command_texts(result['manifest'], self.rules)

    def test_tampered_command_text_metadata_is_rejected(self):
        manifest = self.prepare(self.rules)['manifest']
        changes = {
            'schema_version': lambda record: record.update(schema_version=2),
            'rules_hash': lambda record: record.update(rules_hash='0' * 64),
            'policy': lambda record: record.update(policy='other'),
            'row_key': lambda record: record['rows'][0].update(key='#5'),
            'effective_sha256': lambda record: record['tables'][COMMAND_TABLE].update(effective_sha256='0' * 64),
            'tables': lambda record: record['tables'].clear(),
        }
        path = self.case.runtime / client.MANIFEST
        for name, change in changes.items():
            altered = json.loads(json.dumps(manifest))
            change(altered['command_texts'])
            path.write_text(json.dumps(altered), encoding='utf-8')
            with self.subTest(field=name), self.assertRaisesRegex(client.ClientError, 'command text'):
                self.verify()

    def test_verify_and_launch_compare_the_requested_overrides(self):
        manifest = self.prepare(self.rules)['manifest']
        with patch.object(client, 'verify_runtime', return_value=manifest), \
                patch.object(client, 'check_server', side_effect=AssertionError('Dry launch contacted a server')), \
                patch.object(client.subprocess, 'Popen', side_effect=AssertionError('Dry launch started a process')):
            result = client.launch(self.case.runtime, dry_run=True, command_text_overrides=self.rules)
            self.assertEqual(result['status'], 'verified')
            for rules in (CommandTextOverrides(), self.kept):
                with self.subTest(rules=rules), self.assertRaisesRegex(client.ClientError, 'differ'):
                    client.launch(self.case.runtime, dry_run=True, command_text_overrides=rules)
        with self.assertRaisesRegex(client.ClientError, 'invalid'):
            client._check_requested_command_texts(manifest, self.rules.to_dict())

    def test_inspect_command_forwards_one_loaded_rule_file(self):
        path = self.case.workspace / 'command-text-overrides.json'
        path.write_text(json.dumps(self.rules.to_dict()), encoding='utf-8')
        loaded, original = [], client.load_command_text_overrides

        def load(value):
            loaded.append(value)
            return original(value)

        runtime = self.case.workspace / 'runtime' / 'client'
        argv = ['prepare_client.py', 'inspect', '--source', str(self.case.source), '--runtime', str(runtime),
                '--resource-dir', str(self.case.resources), '--command-text-overrides', str(path)]
        output = io.StringIO()
        with patch.object(client, 'PROJECT_ROOT', self.case.workspace), \
                patch.object(client, 'WORKSPACE_ROOT', self.case.workspace), \
                patch.object(client, 'PROFILE', self.case.profile), \
                patch.object(client, 'load_command_text_overrides', load), \
                patch.object(client.sys, 'argv', argv), contextlib.redirect_stdout(output):
            self.assertEqual(client.main(), 0)
        self.assertEqual(loaded, [path])
        self.assertEqual(json.loads(output.getvalue())['command_texts'], self.view(self.rules).command_text_metadata)
        self.assertFalse(runtime.exists())

    def test_powershell_wrapper_forwards_the_command_text_file(self):
        script = Path(client.__file__).with_name('prepare-client.ps1').read_text('utf-8')
        self.assertIn("[string]$CommandTextOverridesFile = ''", script)
        self.assertIn("@('--command-text-overrides', $CommandTextOverridesFile)", script)


class ShopWindowClosurePreparationTests(unittest.TestCase):
    def setUp(self):
        self.case = ClientPreparationTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.index_resources({SHOP_RESOURCE: shop_table(shop_rows(), SHOP_FIELDS)})

    def view(self):
        return client.load_resource_view(self.case.resources, self.case.source, self.case.profile.sha256)

    def verify(self):
        return client.verify_runtime(self.case.runtime, self.case.workspace, self.case.profile)

    def test_the_closed_shop_table_is_copied_and_recorded(self):
        current = self.view()
        manifest = self.case.prepare()['manifest']
        self.assertEqual(manifest[client.SHOP_CLOSURE_RECORD], current.shop_window_closure_metadata)
        self.assertEqual(manifest['resource_snapshot']['version'], current.manifest['version'])
        copied = self.case.runtime / '.cache' / client.digest(SHOP_RESOURCE.encode())
        self.assertEqual(copied.read_bytes(), current.replacements[SHOP_RESOURCE])
        self.assertEqual(self.verify(), manifest)
        self.assertEqual(self.case.prepare()['status'], 'already-prepared')

    def test_tables_without_summon_products_keep_the_plain_snapshot_and_record_nothing(self):
        self.case.index_resources({SHOP_RESOURCE: shop_table([product(1), product(2, Type=2)], SHOP_FIELDS)},
                                  version='2' * 40)
        manifest = self.case.prepare()['manifest']
        self.assertNotIn(client.SHOP_CLOSURE_RECORD, manifest)
        self.assertEqual(manifest['resource_snapshot']['version'], '2' * 40)
        self.assertEqual(self.verify(), manifest)

    def test_a_shop_table_the_closure_cannot_read_refuses_the_effective_view(self):
        self.case.index_resources({SHOP_RESOURCE: shop_table(shop_rows(), swapped_fields())}, version='2' * 40)
        with self.assertRaisesRegex(ValueError, 'native column layout'):
            self.view()
        with self.assertRaises(ValueError):
            self.case.prepare()
        self.assertFalse(self.case.runtime.exists())

    def test_a_runtime_prepared_before_the_closure_is_rebuilt_on_the_closed_snapshot(self):
        with patch.object(resource_view, 'SHOP_RESOURCE', 'Absent.atb'):
            legacy = self.case.prepare()['manifest']
        self.assertNotIn(client.SHOP_CLOSURE_RECORD, legacy)
        self.assertEqual(self.verify(), legacy)
        result = self.case.prepare()
        self.assertEqual(result['status'], 'prepared')
        self.assertNotEqual(result['manifest']['resource_snapshot'], legacy['resource_snapshot'])
        self.assertEqual(client.verify_runtime(Path(result['backup']), self.case.workspace, self.case.profile), legacy)

    def test_tampered_closure_metadata_is_rejected(self):
        manifest = self.case.prepare()['manifest']
        changes = {
            'schema_version': lambda record: record.update(schema_version=2),
            'policy': lambda record: record.update(policy='other'),
            'registration': lambda record: record.update(registration=104),
            'window': lambda record: record['window'].update(end_at=None),
            'selection': lambda record: record['selection'].update(Type=1),
            'source_sha256': lambda record: record.update(source_sha256='bad'),
            'effective_sha256': lambda record: record.update(effective_sha256='0' * 64),
            'no_products': lambda record: record['products'].clear(),
            'shop_id': lambda record: record['products'][0].update(shop_id=0),
            'duplicated': lambda record: record['products'].append(dict(record['products'][0])),
            'product_fields': lambda record: record['products'][0].pop('original'),
            'extra': lambda record: record.update(rules_hash=None),
        }
        path = self.case.runtime / client.MANIFEST
        for name, change in changes.items():
            altered = json.loads(json.dumps(manifest))
            change(altered[client.SHOP_CLOSURE_RECORD])
            path.write_text(json.dumps(altered), encoding='utf-8')
            with self.subTest(field=name), self.assertRaisesRegex(client.ClientError, 'shop window closure'):
                self.verify()


class ClientStartupTests(unittest.TestCase):
    def test_inspect_passes_optional_master_directory_to_plan(self):
        directory = client.PROJECT_ROOT / 'runtime/test-public-masters'
        with patch.object(client.sys, 'argv', ['prepare_client.py', 'inspect', '--master-dir', str(directory)]), \
                patch.object(client, 'preparation_plan', return_value={'patched': b'fixture', 'files': [], 'entries': [],
                             'api_literal_inventory': [], 'cdn_literal_inventory': [], 'resource_snapshot': {}, 'bytes_to_copy': 0}) as plan, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(client.main(), 0)
        self.assertEqual(plan.call_args.kwargs['master_root'], directory)

    def test_prepare_dry_run_cannot_write_runtime(self):
        with patch.object(client.sys, 'argv', ['prepare_client.py', 'prepare', '--dry-run']), \
                patch.object(client, 'preparation_plan', return_value={'patched': b'fixture', 'files': [], 'entries': [],
                             'api_literal_inventory': [], 'cdn_literal_inventory': [], 'resource_snapshot': {}, 'bytes_to_copy': 0}), \
                patch.object(client, 'prepare') as prepare, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(client.main(), 0)
        prepare.assert_not_called()

    def test_dry_run_does_not_contact_server_or_start_process(self):
        with patch.object(client, 'verify_runtime') as verify, \
                patch.object(client, 'check_server') as server, \
                patch.object(client.subprocess, 'Popen') as process:
            result = client.launch(client.RUNTIME_ROOT, dry_run=True)
        verify.assert_called_once()
        server.assert_not_called()
        process.assert_not_called()
        self.assertEqual(result['command'][1:], ['/viewer_id=1', '/onetime_token=local-only'])

    def test_failed_verification_prevents_start(self):
        with patch.object(client, 'verify_runtime', side_effect=client.ClientError('modified')), \
                patch.object(client.subprocess, 'Popen') as process:
            with self.assertRaisesRegex(client.ClientError, 'modified'):
                client.launch(client.RUNTIME_ROOT)
        process.assert_not_called()

    def test_unavailable_local_server_prevents_start(self):
        with patch.object(client, 'verify_runtime'), \
                patch.object(client.os, 'name', 'nt'), \
                patch.object(client, 'check_server', side_effect=OSError('unavailable')), \
                patch.object(client.subprocess, 'Popen') as process:
            with self.assertRaisesRegex(OSError, 'unavailable'):
                client.launch(client.RUNTIME_ROOT)
        process.assert_not_called()

    def test_health_check_rejects_wrong_service_and_non_json(self):
        for body in [b'{"service":"unrelated"}', b'[]', b'not json', b'x' * 65537, OVERFLOWING_JSON.encode()]:
            response = Mock(status=200)
            response.read.return_value = body
            opener = Mock()
            opener.open.return_value.__enter__ = Mock(return_value=response)
            opener.open.return_value.__exit__ = Mock(return_value=False)
            with self.subTest(body=body[:30]), \
                    patch.object(client.urllib.request, 'build_opener', return_value=opener):
                with self.assertRaises(client.ClientError):
                    client.check_server()
            opener.open.assert_called_once_with(client.HEALTH_URL, timeout=3)

    def test_health_check_accepts_only_local_service_identity(self):
        response = Mock(status=200)
        response.read.return_value = b'{"service":"aigis-ps"}'
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(client.urllib.request, 'build_opener', return_value=opener) as build:
            client.check_server()
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], client.NoRedirect)

    def test_health_redirect_is_rejected(self):
        with self.assertRaisesRegex(client.ClientError, 'must not redirect'):
            client.NoRedirect().redirect_request(None, None, 302, None, None, 'https://example.invalid/')

    def test_health_requires_ready_native_cdn_with_matching_version_and_path(self):
        version = '1' * 40
        ready = {'ready': True, 'version': version, 'path': '/client-assets/'}
        for native in (None, {}, {**ready, 'ready': False}, {**ready, 'ready': 1},
                       {**ready, 'version': '2' * 40}, {**ready, 'path': '/assets/'}, ready):
            response = Mock(status=200)
            response.read.return_value = json.dumps({'service': 'aigis-ps', 'native_cdn': native}).encode()
            opener = Mock()
            opener.open.return_value.__enter__ = Mock(return_value=response)
            opener.open.return_value.__exit__ = Mock(return_value=False)
            with self.subTest(native=native), patch.object(client.urllib.request, 'build_opener', return_value=opener):
                if native is ready:
                    client.check_server(resource_version=version)
                else:
                    with self.assertRaises(client.ClientError):
                        client.check_server(resource_version=version)

    def test_launch_checks_the_prepared_resource_version(self):
        version = '1' * 40
        with patch.object(client, 'verify_runtime', return_value={'resource_snapshot': {'version': version}}), \
                patch.object(client.os, 'name', 'nt'), patch.object(client, 'check_server') as check, \
                patch.object(client.subprocess, 'Popen', return_value=Mock(pid=123)) as process:
            self.assertEqual(client.launch(client.RUNTIME_ROOT)['pid'], 123)
        check.assert_called_once_with(resource_version=version)
        process.assert_called_once()

    def test_powershell_wrappers_run_the_project_interpreter(self):
        for name in PYTHON_WRAPPERS:
            script = Path(client.__file__).with_name(name).read_text('utf-8')
            with self.subTest(script=name):
                self.assertIn(ENVIRONMENT_SOURCE, script)
                self.assertIn(PROJECT_INTERPRETER, script)
                self.assertLess(script.index(ENVIRONMENT_SOURCE), script.index(PROJECT_INTERPRETER))
                self.assertRegex(script[script.index(PROJECT_INTERPRETER):], PROJECT_LAUNCH)
                self.assertNotRegex(script, SYSTEM_INTERPRETER)


if __name__ == '__main__':
    unittest.main()
