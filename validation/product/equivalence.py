"""Offline structural checks for environment-equivalence attestations."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path


_REQUIRED_FIELDS = {
    "shared_snapshot_sha256",
    "corpus_sha256",
    "model_revision",
    "embedding_revision",
    "embedding_dimensions",
    "retrieval",
    "only_candidate_projection",
    "synthetic_or_authorized_sources",
    "baseline_preserved",
    "retrieval_uses_only_local_models",
}
_BOOLEAN_GATES = (
    "only_candidate_projection",
    "synthetic_or_authorized_sources",
    "baseline_preserved",
    "retrieval_uses_only_local_models",
)
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_RECIPE_EMBEDDING_MODEL = re.compile(
    r"^\s*EMBEDDING_MODEL:\s+\$\{OGR_VALIDATION_LOCAL_EMBEDDING_MODEL:-([^}\r\n]+)\}\s*$",
    re.MULTILINE,
)
_RECIPE_EMBEDDING_DIMENSIONS = re.compile(
    r"^\s*EMBEDDING_DIM:\s*[\"']?([0-9]+)[\"']?\s*$", re.MULTILINE
)
_RETRIEVAL_MODES = {"mix", "local", "global", "hybrid", "naive"}
_RESERVED_RETRIEVAL_FIELDS = {"query", "workspace", "api_key", "token"}


def embedding_settings_from_recipe(recipe_path: Path):
    """Resolve the pinned recipe's local embedding model and dimensions offline."""
    try:
        recipe = Path(recipe_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ValueError("environment recipe is unreadable") from None

    model_defaults = _RECIPE_EMBEDDING_MODEL.findall(recipe)
    dimensions = _RECIPE_EMBEDDING_DIMENSIONS.findall(recipe)
    if len(model_defaults) != 1 or len(dimensions) != 1:
        raise ValueError("environment recipe must declare one pinned embedding model and dimension")

    model = os.environ.get("OGR_VALIDATION_LOCAL_EMBEDDING_MODEL", model_defaults[0])
    if not isinstance(model, str) or not model.strip():
        raise ValueError("configured embedding revision must be non-empty")
    return model, int(dimensions[0])


def validate_environment_equivalence(attestation, corpus_path: Path, model, retrieval,
                                     embedding_revision, embedding_dimensions):
    """Validate attested frozen inputs against the configured corpus/model/retrieval."""
    if not isinstance(attestation, dict):
        raise ValueError("environment equivalence attestation must be a JSON object")

    missing = _REQUIRED_FIELDS - attestation.keys()
    if missing:
        raise ValueError("environment equivalence attestation missing required fields: " + ", ".join(sorted(missing)))

    for field in ("shared_snapshot_sha256", "corpus_sha256"):
        value = attestation[field]
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ValueError(f"environment attestation {field} must be a 64-character SHA-256 hex digest")

    for field in ("model_revision", "embedding_revision"):
        if not isinstance(attestation[field], str) or not attestation[field].strip():
            raise ValueError(f"environment attestation {field} must be a non-empty pinned revision")

    dimensions = attestation["embedding_dimensions"]
    if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0:
        raise ValueError("environment attestation embedding_dimensions must be a positive integer")

    if not isinstance(model, str) or not model.strip():
        raise ValueError("pinned model revision required")
    if attestation["model_revision"] != model:
        raise ValueError("attested model revision does not match configured model")

    if not isinstance(embedding_revision, str) or not embedding_revision.strip():
        raise ValueError("configured embedding revision required")
    if attestation["embedding_revision"] != embedding_revision:
        raise ValueError("attested embedding revision does not match configured recipe")
    if (isinstance(embedding_dimensions, bool) or not isinstance(embedding_dimensions, int)
            or embedding_dimensions <= 0):
        raise ValueError("configured embedding dimensions must be a positive integer")
    if dimensions != embedding_dimensions:
        raise ValueError("attested embedding dimensions do not match configured recipe")

    _validate_retrieval_settings(retrieval, "retrieval configuration")
    _validate_retrieval_settings(attestation["retrieval"], "attested retrieval settings")
    if not _same_json_value(attestation["retrieval"], retrieval):
        raise ValueError("attested retrieval settings do not match configured settings")

    for field in _BOOLEAN_GATES:
        if attestation[field] is not True:
            raise ValueError(f"environment attestation gate not met: {field} must be true")

    try:
        corpus_sha256 = hashlib.sha256(Path(corpus_path).read_bytes()).hexdigest()
    except OSError:
        raise ValueError("unable to read frozen corpus for environment equivalence check") from None
    if attestation["corpus_sha256"].lower() != corpus_sha256:
        raise ValueError("attested corpus SHA-256 does not match frozen corpus")
    return attestation


def _validate_retrieval_settings(retrieval, label):
    mode = retrieval.get("mode") if isinstance(retrieval, dict) else None
    if not isinstance(mode, str) or mode not in _RETRIEVAL_MODES:
        raise ValueError(f"{label} must specify a supported mode")
    top_k = retrieval.get("top_k")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 30:
        raise ValueError(f"{label} top_k must be an integer from 1..30")
    if _RESERVED_RETRIEVAL_FIELDS & retrieval.keys():
        raise ValueError(f"{label} includes reserved fields")


def _same_json_value(left, right):
    try:
        return json.dumps(left, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(
            right, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return False


def read_and_validate_environment_equivalence(path: Path, corpus_path: Path, model, retrieval,
                                              recipe_path: Path):
    """Read an attestation without exposing its contents in errors, then validate it."""
    try:
        attestation = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("environment equivalence manifest is unreadable or invalid JSON") from None
    embedding_revision, embedding_dimensions = embedding_settings_from_recipe(recipe_path)
    return validate_environment_equivalence(attestation, corpus_path, model, retrieval,
        embedding_revision, embedding_dimensions)
