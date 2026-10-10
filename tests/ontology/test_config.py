import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class GovernedConfigurationTests(unittest.TestCase):
    def test_secret_generation_includes_workspace_scoped_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts/init_env.py").write_text((ROOT / "scripts/init_env.py").read_text())
            (root / ".env.example").write_text((ROOT / ".env.example").read_text())
            subprocess.run(["python3", str(root / "scripts/init_env.py")], check=True, capture_output=True)
            text = (root / ".env").read_text()
            value = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("ONTOLOGY_TOKENS="))
            tokens = json.loads(value.strip("'"))
            self.assertEqual({"ontology_reader", "ontology_admin"}, {p["role"] for p in tokens.values()})
            self.assertTrue(all(len(token) == 64 for token in tokens))
            self.assertTrue(all(p["workspaces"] == ["company_governed"] for p in tokens.values()))
            self.assertNotIn("GENERATE_ONTOLOGY", text)
            self.assertNotIn("ONTOLOGY_UI_SESSION_SECRET=", text)
