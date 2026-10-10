import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.backup import common, restore_engine


class RestoreTests(unittest.TestCase):
    def test_corrupt_bundle_is_rejected_before_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "payload").write_text("changed")
            (root / "manifest.json").write_text(json.dumps({
                "schema_version": 1,
                "state": "complete",
                "checksums": {"payload": "0" * 64},
            }))
            with self.assertRaisesRegex(ValueError, "checksum"):
                common.validate_bundle(root)

    def test_expected_embedding_mismatch_rejects_restore(self):
        args = SimpleNamespace(workspace="ws", embedding_model="embed-v2", embedding_dim=1024)
        manifest = {"workspace": "ws", "embedding": {"model": "embed-v1", "dimension": 1024}}
        with self.assertRaisesRegex(RuntimeError, "embedding model mismatch"):
            restore_engine._check_compatibility(manifest, {}, args)

    def test_snapshot_argument_cannot_inject_restic_options(self):
        class Fake:
            def restic(self, args):
                raise AssertionError("unsafe input reached Restic")

        with self.assertRaisesRegex(RuntimeError, "hexadecimal"):
            restore_engine._resolve_snapshot(Fake(), "latest;--repo=/prod")

    def test_isolated_compose_keeps_only_private_database_services(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config"
            config.mkdir()
            (config / ".env").write_text("POSTGRES_PASSWORD=secret\nMONGO_PASSWORD=secret\n")
            (config / "compose.yaml").write_text("services: {}\n")
            source = {
                "name": "production-name",
                "services": {
                    "postgres": {"image": "pg", "ports": ["5432:5432"], "restart": "always", "volumes": []},
                    "mongodb": {"image": "mongo", "ports": ["27017:27017"], "volumes": []},
                    "librechat": {"image": "app", "ports": ["3080:3080"]},
                },
            }

            class Fake:
                def run(self, args):
                    return json.dumps(source).encode()

            env = {"COMPOSE_PROJECT_NAME": "ogr-restore-unit", "BACKUP_COMPOSE_FILES": "compose.yaml"}
            with patch.object(restore_engine.common, "Config", return_value=Fake()):
                output = restore_engine._render_sandbox_compose(Fake(), config, root, env, root, {
                    "compose_files": ["compose.yaml"],
                    "images": {name: service.get("image", "") for name, service in source["services"].items()},
                })
            compose = json.loads(output.read_text())
            self.assertTrue({"postgres", "mongodb"} <= set(compose["services"]))
            self.assertEqual(compose["name"], "ogr-restore-unit")
            self.assertTrue(compose["networks"]["default"]["internal"])
            for service in compose["services"].values():
                self.assertNotIn("ports", service)
                self.assertNotIn("restart", service)
            for name in ("postgres", "mongodb"):
                self.assertTrue(any(mount.get("target") == "/ogr-restore" for mount in compose["services"][name]["volumes"]))

    def test_verify_only_validates_snapshot_without_starting_containers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state"

            class FakeConfig:
                def __init__(self):
                    self.root = root
                    self.state = state
                    self.state.mkdir()
                    self.calls = []

                def restic(self, args):
                    self.calls.append(args)
                    return b""

            config = FakeConfig()
            manifest = {"schema_version": 1, "state": "complete", "checksums": {}}
            with patch.object(restore_engine, "_resolve_snapshot", return_value="a" * 64), patch.object(
                restore_engine, "_find_bundle", return_value=root
            ), patch.object(common, "validate_bundle", return_value=manifest):
                code = restore_engine.main(config, ["--snapshot", "latest", "--verify-only"])
            self.assertEqual(code, 0)
            self.assertEqual(len(config.calls), 1)
            self.assertEqual(config.calls[0][0], "restore")
            self.assertFalse(any(hasattr(config, name) for name in ("docker",)))


if __name__ == "__main__":
    unittest.main()
