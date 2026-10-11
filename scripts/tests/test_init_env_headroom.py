import importlib
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "init_env.py"
sys.path.insert(0, str(SCRIPT.parent))
MODULE = importlib.import_module("init_env")


class InitEnvHeadroomTests(unittest.TestCase):
    def test_fresh_initialization_generates_a_unique_token_without_printing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.example").write_text("HEADROOM_PROXY_TOKEN=GENERATE\n")
            first_output = MODULE.initialize_env(root)
            first = (root / ".env").read_text()
            first_match = re.search(r"^HEADROOM_PROXY_TOKEN=(.+)$", first, re.M)
            assert first_match is not None
            first_token = first_match.group(1)

            self.assertEqual(len(first_token), 64)
            self.assertNotIn(first_token, first_output)
            self.assertEqual(stat.S_IMODE((root / ".env").stat().st_mode), 0o600)

            other_root = root / "other"
            other_root.mkdir()
            (other_root / ".env.example").write_text("HEADROOM_PROXY_TOKEN=GENERATE\n")
            MODULE.initialize_env(other_root)
            second = (other_root / ".env").read_text()
            second_match = re.search(r"^HEADROOM_PROXY_TOKEN=(.+)$", second, re.M)
            assert second_match is not None
            second_token = second_match.group(1)
            self.assertNotEqual(first_token, second_token)

    def test_fresh_initialization_refuses_to_overwrite_existing_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.example").write_text("HEADROOM_PROXY_TOKEN=GENERATE\n")
            existing = b"EXISTING_SECRET=leave-me\n"
            (root / ".env").write_bytes(existing)

            with self.assertRaises(SystemExit):
                MODULE.initialize_env(root)

            self.assertEqual((root / ".env").read_bytes(), existing)

    def test_migration_appends_only_missing_token_preserving_bytes_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            original = b"# preserve\r\nCHAT_API_KEY=existing-secret\r\nCUSTOM=value"
            env_file.write_bytes(original)
            env_file.chmod(0o640)

            added = MODULE.add_headroom_settings(env_file)
            result = env_file.read_bytes()

            self.assertEqual(added, ["HEADROOM_PROXY_TOKEN"])
            self.assertTrue(
                result.startswith(original + b"\n\n# Headroom settings (added by scripts/init_env.py)\n")
            )
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o640)
            self.assertIn(b"CHAT_API_KEY=existing-secret\r\n", result)
            match = re.search(rb"^HEADROOM_PROXY_TOKEN=([0-9a-f]{64})$", result, re.M)
            assert match is not None
            token = match.group(1)
            self.assertNotIn(token, result[:len(original)])

    def test_migration_is_idempotent_after_adding_missing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            original = b"CUSTOM=value\n"
            env_file.write_bytes(original)

            self.assertEqual(MODULE.add_headroom_settings(env_file), ["HEADROOM_PROXY_TOKEN"])
            first = env_file.read_bytes()
            self.assertEqual(MODULE.add_headroom_settings(env_file), [])
            self.assertEqual(env_file.read_bytes(), first)
            self.assertEqual(len(re.findall(rb"^HEADROOM_PROXY_TOKEN=", first, re.M)), 1)

    def test_migration_preserves_an_existing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            original = b"HEADROOM_PROXY_TOKEN=existing-token\nCUSTOM=value\n"
            env_file.write_bytes(original)

            self.assertEqual(MODULE.add_headroom_settings(env_file), [])
            self.assertEqual(env_file.read_bytes(), original)

    def test_migration_cli_does_not_print_generated_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.example").write_text("HEADROOM_PROXY_TOKEN=GENERATE\n")
            script = root / "scripts" / "init_env.py"
            script.parent.mkdir()
            script.write_bytes(SCRIPT.read_bytes())
            env_file = root / ".env"
            env_file.write_text("EXISTING=yes\n")

            result = subprocess.run(
                [sys.executable, str(script), "--migrate-headroom"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )

            token = re.search(r"^HEADROOM_PROXY_TOKEN=(.+)$", env_file.read_text(), re.M)
            assert token is not None
            token_value = token.group(1)
            self.assertIn("Added", result.stdout)
            self.assertNotIn(token_value, result.stdout)
            self.assertNotIn("HEADROOM_PROXY_TOKEN=", result.stdout)


if __name__ == "__main__":
    unittest.main()
