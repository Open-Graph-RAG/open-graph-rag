import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "backup"))
import backup_engine


class FakeConfig:
    def __init__(self, root):
        self.root = Path(root)
        self.state = self.root / "state"
        self.state.mkdir()
        self.env = {"BACKUP_OFFSITE_REPOSITORY": "s3:offsite/repo", "RESTIC_REPOSITORY": "local:repo", "RESTIC_PASSWORD_FILE": "/password"}
        self.compose = ["docker", "compose"]
        self.events = []
        self._inventory = {
            "services": {
                "postgres": {"volumes": [{"type": "volume", "source": "postgres_data"}]},
                "mongodb": {"volumes": [{"type": "volume", "source": "mongo_data"}]},
                "librechat": {"volumes": [{"type": "volume", "source": "librechat_data"}]},
            },
            "volumes": {"postgres_data": {}, "mongo_data": {}, "librechat_data": {}},
        }

    @contextlib.contextmanager
    def lock(self):
        yield

    def inventory(self):
        return self._inventory

    def running(self):
        return ["postgres", "mongodb", "librechat"]

    def run(self, args, input=None):
        self.events.append(tuple(args))
        return b""

    def restic(self, args):
        self.events.append(tuple(["restic"] + args))
        if args[0] == "backup":
            return b'{"message_type":"summary","snapshot_id":"a1b2c3"}\n'
        if args[0] == "snapshots":
            backup_tag = next(arg for arg in args if str(arg).startswith("backup-id:"))
            return json.dumps([{ "id": "a1b2c3", "tags": [backup_tag] }]).encode()
        return b"{}"


class BackupTests(unittest.TestCase):
    def test_backup_resumes_writers_before_marking_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = FakeConfig(tmp)
            with (
                mock.patch.object(backup_engine, "preflight", return_value={"running": config.running()}),
                mock.patch.object(backup_engine, "_dump_postgres", return_value=[{"service": "postgres", "kind": "postgres", "database": "app", "file": "dumps/postgres/app.dump"}]),
                mock.patch.object(backup_engine, "_dump_mongo", return_value=[{"service": "mongodb", "kind": "mongo", "database": "all", "file": "dumps/mongodb/archive.gz"}]),
                mock.patch.object(backup_engine, "_snapshot_volumes", return_value=["librechat_data"]),
                mock.patch.object(backup_engine, "_copy_config"),
                mock.patch.object(backup_engine, "database_evidence", return_value={}),
                mock.patch.object(backup_engine, "checksums", return_value={"dumps/db.dump": "0" * 64}),
                mock.patch.object(backup_engine, "validate_bundle"),
                mock.patch.object(backup_engine, "_verify_snapshot"),
                mock.patch.object(backup_engine, "_apply_retention"),
            ):
                backup_engine.backup(config)
            last = json.loads((config.state / "last-backup.json").read_text())
            self.assertEqual(last["state"], "complete")
            self.assertEqual(last["snapshot_id"], "a1b2c3")
            stop = config.events.index(("docker", "compose", "stop", "librechat"))
            start = config.events.index(("docker", "compose", "up", "-d", "--no-deps", "--no-recreate", "--wait", "--wait-timeout", "180", "librechat"))
            self.assertLess(stop, start)

    def test_dump_failure_resumes_writer_and_is_not_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = FakeConfig(tmp)
            with (
                mock.patch.object(backup_engine, "preflight", return_value={"running": config.running()}),
                mock.patch.object(backup_engine, "_dump_postgres", side_effect=RuntimeError("safe failure")),
                mock.patch.object(backup_engine, "_copy_config"),
            ):
                with self.assertRaises(RuntimeError):
                    backup_engine.backup(config)
            last = json.loads((config.state / "last-backup.json").read_text())
            self.assertEqual(last["state"], "failed")
            self.assertIn(("docker", "compose", "up", "-d", "--no-deps", "--no-recreate", "--wait", "--wait-timeout", "180", "librechat"), config.events)

    def test_offsite_verification_failure_never_marks_backup_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = FakeConfig(tmp)
            with (
                mock.patch.object(backup_engine, "preflight", return_value={"running": config.running()}),
                mock.patch.object(backup_engine, "_dump_postgres", return_value=[]),
                mock.patch.object(backup_engine, "_dump_mongo", return_value=[]),
                mock.patch.object(backup_engine, "_snapshot_volumes", return_value=[]),
                mock.patch.object(backup_engine, "_copy_config"),
                mock.patch.object(backup_engine, "database_evidence", return_value={}),
                mock.patch.object(backup_engine, "checksums", return_value={"dumps/db.dump": "0" * 64}),
                mock.patch.object(backup_engine, "validate_bundle"),
                mock.patch.object(backup_engine, "_verify_snapshot", side_effect=[None, RuntimeError("offsite failed")]),
                mock.patch.object(backup_engine, "_apply_retention"),
            ):
                with self.assertRaises(RuntimeError):
                    backup_engine.backup(config)
            last = json.loads((config.state / "last-backup.json").read_text())
            self.assertEqual(last["state"], "unverified")

    def test_snapshot_id_requires_restic_summary(self):
        with self.assertRaises(backup_engine.BackupError):
            backup_engine._extract_snapshot_id(b"not json\n")


if __name__ == "__main__":
    unittest.main()
