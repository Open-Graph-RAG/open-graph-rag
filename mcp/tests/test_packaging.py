from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prepare_kev_artifacts import (
    BASE_REVISION,
    KEV_REVISION,
    create_manifest,
    expected_files,
)


class ArtifactManifestTests(unittest.TestCase):
    def test_lock_and_prep_pins_match_feasibility_evidence(self) -> None:
        root = Path(__file__).resolve().parents[2]
        evidence = json.loads((root / "docs/kev-feasibility-gpu.json").read_text())
        packaged_evidence = json.loads((root / "mcp/kev-artifact-checksums.json").read_text())
        self.assertEqual(len(expected_files(evidence)), len(evidence["artifact_files"]))
        self.assertEqual(packaged_evidence["artifact_files"], evidence["artifact_files"])
        self.assertEqual(
            packaged_evidence["pins"],
            {key: evidence["pins"][key] for key in ("kev_model", "qwen_base", "kev_source_commit")},
        )
        locked = {
            line.split("==", 1)[0].lower(): line
            for line in (root / "mcp/requirements-kev.lock").read_text().splitlines()
        }
        expected = {
            line.split("==", 1)[0].lower(): line
            for line in evidence["installed_packages"]
        }
        expected["torch"] = "torch==2.8.0+cu128"
        expected.update({
            line.split("==", 1)[0].lower(): line
            for line in (root / "mcp/requirements.lock").read_text().splitlines()
        })
        self.assertEqual(locked, expected)

    def test_manifest_is_emitted_only_after_every_pinned_file_matches(self) -> None:
        contents = {
            f"kev/adapter_model.safetensors": b"kev-adapter",
            f"base/config.json": b"base-config",
        }
        kev_file = contents["kev/adapter_model.safetensors"]
        base_file = contents["base/config.json"]
        evidence = {
            "artifact_files": [
                {
                    "path": f"/cache/snapshots/{KEV_REVISION}/adapter_model.safetensors",
                    "bytes": len(kev_file),
                    "sha256": hashlib.sha256(kev_file).hexdigest(),
                },
                {
                    "path": f"/cache/snapshots/{BASE_REVISION}/config.json",
                    "bytes": len(base_file),
                    "sha256": hashlib.sha256(base_file).hexdigest(),
                },
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, value in contents.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)

            manifest = create_manifest(root, evidence)

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["kev_model_revision"], KEV_REVISION)
        self.assertEqual(manifest["base_revision"], BASE_REVISION)
        self.assertEqual(
            [entry["path"] for entry in manifest["files"]],
            ["base/config.json", "kev/adapter_model.safetensors"],
        )

    def test_manifest_rejects_modified_checkpoint(self) -> None:
        value = b"modified"
        evidence = {
            "artifact_files": [
                {
                    "path": f"/cache/snapshots/{KEV_REVISION}/weights.bin",
                    "bytes": len(value),
                    "sha256": "0" * 64,
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kev/weights.bin"
            path.parent.mkdir(parents=True)
            path.write_bytes(value)
            with self.assertRaisesRegex(ValueError, "Checksum or size mismatch"):
                create_manifest(path.parents[1], evidence)


if __name__ == "__main__":
    unittest.main()
