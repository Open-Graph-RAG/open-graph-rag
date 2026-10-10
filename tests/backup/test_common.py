import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'backup'))
from common import Config, checksums, read_env, validate_bundle, write_json
from monitor import check


class CommonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def bundle(self, member='safe'):
        (self.path / 'volumes').mkdir()
        with tarfile.open(self.path / 'volumes' / 'test.tar', 'w') as archive:
            record = tarfile.TarInfo(member)
            record.size = 4
            archive.addfile(record, io.BytesIO(b'data'))
        manifest = {'schema_version': 1, 'state': 'complete', 'volumes': ['test'], 'dumps': [], 'checksums': checksums(self.path)}
        write_json(self.path / 'manifest.json', manifest)
        return manifest

    def test_tampering_rejected(self):
        self.bundle()
        validate_bundle(self.path)
        (self.path / 'volumes' / 'test.tar').write_bytes(b'bad')
        with self.assertRaises(ValueError):
            validate_bundle(self.path)

    def test_archive_traversal_rejected(self):
        self.bundle('../escape')
        with self.assertRaises(ValueError):
            validate_bundle(self.path)

    def test_extra_file_rejected(self):
        self.bundle()
        (self.path / 'extra').write_text('secret')
        with self.assertRaises(ValueError):
            validate_bundle(self.path)

    def test_config_does_not_execute_shell(self):
        config = self.path / 'settings.env'
        config.write_text('VALUE=$(touch /tmp/do-not-create)\nQUOTED="literal"\n')
        self.assertEqual(read_env(config)['VALUE'], '$(touch /tmp/do-not-create)')
        self.assertEqual(read_env(config)['QUOTED'], 'literal')

    def test_lock_rejects_concurrent_operation(self):
        c = Config(root=self.path, env={'BACKUP_STATE_DIR': str(self.path / 'state')})
        with c.lock():
            with self.assertRaises(RuntimeError):
                with c.lock():
                    pass

    def test_missing_or_old_status_alerts(self):
        c = Config(root=self.path, env={'BACKUP_STATE_DIR': str(self.path / 'state')})
        with self.assertRaises(RuntimeError):
            check(c)
        now = datetime(2026, 10, 9, tzinfo=timezone.utc)
        for name in ['last-backup.json', 'last-drill.json']:
            write_json(c.state / name, {'state': 'complete', 'finished_at': now.isoformat()})
        check(c, now)
        write_json(c.state / 'last-backup.json', {'state': 'complete', 'finished_at': '2026-10-01T00:00:00+00:00'})
        with self.assertRaises(RuntimeError):
            check(c, now)

    def test_command_errors_do_not_leak_credentials(self):
        c = Config(root=self.path, env={})
        import subprocess
        with patch('subprocess.run', return_value=subprocess.CompletedProcess(['tool'], 1, b'', b'password=hidden')):
            with self.assertRaisesRegex(RuntimeError, '^Backup command failed: tool$'):
                c.run(['tool', '--password', 'hidden'])


if __name__ == '__main__':
    unittest.main()
