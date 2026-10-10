"""Failure injection proves errors do not publish verified recovery points."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_backup import FakeConfig
import backup_engine
from scripts.backup import restore_engine
from types import SimpleNamespace


class FailureTests(unittest.TestCase):
    def test_database_unavailable_does_not_stop_writers(self):
        with tempfile.TemporaryDirectory() as directory:
            c = FakeConfig(directory)
            with patch.object(backup_engine, 'preflight', side_effect=RuntimeError('Database unavailable')):
                with self.assertRaises(RuntimeError):
                    backup_engine.backup(c)
            self.assertFalse(any('stop' in event for event in c.events))
            self.assertEqual(json.loads((c.state / 'last-backup.json').read_text())['state'], 'failed')

    def test_interruption_restores_writers_and_cleans_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            c = FakeConfig(directory)
            with patch.object(backup_engine, 'preflight', return_value={'running': c.running()}), patch.object(
                backup_engine, '_dump_postgres', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    backup_engine.backup(c)
            self.assertTrue(any('up' in event and 'librechat' in event for event in c.events))
            self.assertEqual(list((c.state / 'staging').iterdir()), [])
            self.assertEqual(json.loads((c.state / 'last-backup.json').read_text())['state'], 'failed')

    def test_wrong_repository_credentials_fail_without_quiescence(self):
        with tempfile.TemporaryDirectory() as directory:
            c = FakeConfig(directory)
            with patch.object(backup_engine, 'preflight', side_effect=RuntimeError('Backup command failed: restic')):
                with self.assertRaises(RuntimeError):
                    backup_engine.backup(c)
            self.assertFalse(any('stop' in event for event in c.events))
            self.assertFalse(any('tag' in event or 'forget' in event for event in c.events))

    def test_workspace_mismatch_is_rejected(self):
        manifest = {'workspace': 'old', 'embedding': {'model': 'bge-m3', 'dimension': 1024}}
        args = SimpleNamespace(workspace='new', embedding_model=None, embedding_dim=None)
        with self.assertRaisesRegex(RuntimeError, 'workspace mismatch'):
            restore_engine._check_compatibility(manifest, {}, args)

    def test_retention_is_global_and_only_managed_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            c = FakeConfig(directory)
            backup_engine._apply_retention(c, c.env['BACKUP_OFFSITE_REPOSITORY'])
            self.assertEqual(len(c.events), 2)
            for command in c.events:
                self.assertIn('--group-by', command)
                self.assertEqual(command[command.index('--group-by') + 1], '')
                self.assertEqual(command[command.index('--tag') + 1], 'ogr-backup')
                self.assertNotIn('before-upgrade', command)


if __name__ == '__main__':
    unittest.main()
