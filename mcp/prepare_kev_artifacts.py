"""Download pinned Kev checkpoints and write their verified artifact manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


EVIDENCE_PATH = Path(__file__).resolve().with_name("kev-artifact-checksums.json")
KEV_SOURCE_COMMIT = "5e42a7a03f28134853dd3ff77461457e921e5ec1"
KEV_MODEL = "jaredpalmer/kev-0.8b"
KEV_REVISION = "9a45d25eb2ab761841196625383fa1dff0e56c1e"
BASE_MODEL = "Qwen/Qwen3.5-0.8B-Base"
BASE_REVISION = "dc7cdfe2ee4154fa7e30f5b51ca41bfa40174e68"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_files(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    files = []
    for entry in evidence["artifact_files"]:
        original = entry["path"]
        if f"/snapshots/{KEV_REVISION}/" in original:
            prefix, directory = "kev", KEV_REVISION
        elif f"/snapshots/{BASE_REVISION}/" in original:
            prefix, directory = "base", BASE_REVISION
        else:
            raise ValueError(f"Unexpected artifact path in feasibility evidence: {original}")
        relative = original.split(f"/snapshots/{directory}/", 1)[1]
        files.append({
            "path": f"{prefix}/{relative}",
            "bytes": entry["bytes"],
            "sha256": entry["sha256"],
        })
    if not files or len({entry["path"] for entry in files}) != len(files):
        raise ValueError("Feasibility evidence contains no files or duplicate paths")
    return sorted(files, key=lambda entry: entry["path"])


def create_manifest(artifact_root: Path, evidence: dict[str, Any]) -> dict[str, Any]:
    files = expected_files(evidence)
    for entry in files:
        path = artifact_root / entry["path"]
        if not path.is_file():
            raise ValueError(f"Missing pinned artifact: {entry['path']}")
        if path.stat().st_size != entry["bytes"] or sha256_file(path) != entry["sha256"]:
            raise ValueError(f"Checksum or size mismatch for pinned artifact: {entry['path']}")
    return {
        "schema_version": 1,
        "kev_source_commit": KEV_SOURCE_COMMIT,
        "kev_model_revision": KEV_REVISION,
        "base_revision": BASE_REVISION,
        "files": files,
    }


def prepare(output: Path, hf_home: Path) -> None:
    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    evidence_pins = evidence["pins"]
    if evidence_pins["kev_source_commit"] != KEV_SOURCE_COMMIT:
        raise ValueError("Feasibility evidence Kev source pin does not match this preparer")
    if evidence_pins["kev_model"].split("@", 1)[1] != KEV_REVISION:
        raise ValueError("Feasibility evidence Kev model revision does not match this preparer")
    if evidence_pins["qwen_base"].split("@", 1)[1] != BASE_REVISION:
        raise ValueError("Feasibility evidence base revision does not match this preparer")

    os.environ["HF_HOME"] = str(hf_home)
    from huggingface_hub import snapshot_download

    output = output.resolve()
    hf_home = hf_home.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kev-artifacts-", dir=output.parent) as temp_name:
        temp = Path(temp_name)
        downloads = temp / "downloads"
        staged = temp / "artifacts"
        for repo_id, revision, name in (
            (KEV_MODEL, KEV_REVISION, "kev"),
            (BASE_MODEL, BASE_REVISION, "base"),
        ):
            downloaded = downloads / name
            snapshot_download(
                repo_id=repo_id,
                revision=revision,
                cache_dir=hf_home / "hub",
                local_dir=downloaded,
            )

        for entry in expected_files(evidence):
            prefix, relative = entry["path"].split("/", 1)
            source = downloads / prefix / relative
            target = staged / prefix / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        manifest = create_manifest(staged, evidence)
        (staged / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        for path in staged.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        staged.chmod(0o755)

        backup = temp / "previous-artifacts"
        if output.exists():
            output.rename(backup)
        try:
            staged.rename(output)
        except Exception:
            if backup.exists():
                backup.rename(output)
            raise
    if hf_home == output.parent / "prepare-hf":
        shutil.rmtree(hf_home, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/model-cache/artifacts"))
    parser.add_argument("--hf-home", type=Path, default=Path("/tmp/hf-home"))
    args = parser.parse_args()
    prepare(args.output, args.hf_home)
    print(f"Prepared and verified pinned Kev artifacts in {args.output}")


if __name__ == "__main__":
    main()
