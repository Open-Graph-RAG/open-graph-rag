import importlib.util
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "add_activepieces_env.py"
SPEC = importlib.util.spec_from_file_location("add_activepieces_env", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class AddActivepiecesEnvTests(unittest.TestCase):
    def test_appends_missing_keys_without_changing_existing_bytes_or_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            original = b"# keep this comment\nPASSWORD=existing-secret\nCUSTOM=value"
            env_file.write_bytes(original)
            env_file.chmod(0o640)

            added = MODULE.add_missing_settings(env_file)
            result = env_file.read_bytes()

            self.assertIn("AP_ENCRYPTION_KEY", added)
            self.assertTrue(result.startswith(original + b"\n\n# Activepieces settings"))
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o640)
            self.assertIn(b"PASSWORD=existing-secret\n", result)

    def test_is_idempotent_and_does_not_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_bytes(b"ORIGINAL=yes\n")
            MODULE.add_missing_settings(env_file)
            first = env_file.read_bytes()

            self.assertEqual(MODULE.add_missing_settings(env_file), [])
            self.assertEqual(env_file.read_bytes(), first)
            for key in MODULE.VARIABLES:
                self.assertEqual(len(re.findall(rb"^" + key.encode() + rb"=", first, re.M)), 1)

    def test_command_does_not_print_generated_secret_values(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("EXISTING=yes\n")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(env_file)],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertIn("Added", result.stdout)
            self.assertNotIn("AP_POSTGRES_PASSWORD=", result.stdout)
            self.assertNotIn("AP_ENCRYPTION_KEY=", result.stdout)


if __name__ == "__main__":
    unittest.main()
