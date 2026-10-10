"""Offline adapter for the pinned Kev decision model."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from decision_contract import DecisionAnswer, DecisionQuestion, KevQuestion, ModelMetadata
from prepare_kev_artifacts import expected_files

EXPECTED_SOURCE = "5e42a7a03f28134853dd3ff77461457e921e5ec1"
EXPECTED_KEV = "9a45d25eb2ab761841196625383fa1dff0e56c1e"
EXPECTED_BASE = "dc7cdfe2ee4154fa7e30f5b51ca41bfa40174e68"


class KevUnavailable(RuntimeError):
    """The pinned local model cannot be loaded or used."""


def _digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def verify_artifacts(root: Path, source: Path, manifest_path: Path) -> None:
    try:
        manifest = json.loads(manifest_path.read_text())
        if (manifest["schema_version"] != 1 or manifest["kev_source_commit"] != EXPECTED_SOURCE
                or manifest["kev_model_revision"] != EXPECTED_KEV or manifest["base_revision"] != EXPECTED_BASE):
            raise ValueError
        entries = manifest["files"]
        if not isinstance(entries, list) or not entries:
            raise ValueError
        pinned_entries = expected_files(json.loads(
            Path(__file__).with_name("kev-artifact-checksums.json").read_text()
        ))
        if sorted(entries, key=lambda entry: entry["path"]) != pinned_entries:
            raise ValueError
        for entry in entries:
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts or relative.parts[0] not in {"kev", "base"}:
                raise ValueError
            path = (root / relative).resolve()
            if root.resolve() not in path.parents:
                raise ValueError
            size, digest = _digest(path)
            if size != entry["bytes"] or digest != entry["sha256"]:
                raise ValueError
        if not (root / "kev").is_dir() or not (root / "base").is_dir():
            raise ValueError
        if not (source / ".git").exists():
            raise ValueError
        git = ["git", "-c", f"safe.directory={source}"]
        head = subprocess.check_output([*git, "rev-parse", "HEAD"], cwd=source, text=True).strip()
        dirty = subprocess.check_output([*git, "status", "--porcelain"], cwd=source, text=True).strip()
        if head != EXPECTED_SOURCE or dirty:
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise KevUnavailable("pinned local model artifacts failed verification") from exc


class KevAdapter:
    """Small seam around Kev's strict encoder and probability API."""

    def __init__(self, *, model: Any, tokenizer: Any, request_type: Any, to_record: Any, to_answers: Any,
                 choice_confidence: Any, score_confidence: Any, checkpoint: Any, torch: Any, device: str):
        self.model, self.tokenizer = model, tokenizer
        self.request_type, self.to_record, self.checkpoint = request_type, to_record, checkpoint
        self.to_answers = to_answers
        self.choice_confidence, self.score_confidence = choice_confidence, score_confidence
        self.torch, self.device = torch, device
        self._metadata = ModelMetadata(
            resolved_model="jaredpalmer/kev-0.8b@" + EXPECTED_KEV,
            base="Qwen/Qwen3.5-0.8B-Base@" + EXPECTED_BASE,
            tokenizer="Qwen/Qwen3.5-0.8B-Base@" + EXPECTED_BASE,
            code_revision=EXPECTED_SOURCE, dtype="fp32", temperature=float(checkpoint.meta.temperature),
            execution_device=device,
            device_name=(torch.cuda.get_device_name() if device == "cuda" else None),
            cuda_runtime=(str(torch.version.cuda) if device == "cuda" else None),
        )

    @property
    def metadata(self) -> ModelMetadata:
        return self._metadata

    @staticmethod
    def _upstream_questions(questions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        upstream = {}
        for question in questions:
            value = {key: item for key, item in question.items() if key != "id"}
            if "kind" in value:
                kind = value.pop("kind")
                options = value.pop("options", [])
                levels = value.pop("levels", [])
                value["type"] = kind
                if kind == "score":
                    value["criteria"] = list(levels)
                else:
                    value["criteria"] = {option["id"]: option["description"] for option in options}
            upstream[question["id"]] = value
        return upstream

    def _record(self, state: str, questions: list[dict[str, Any]]) -> Any:
        upstream = self._upstream_questions(questions)
        if not upstream:
            upstream = {"context": {"type": "choice", "instructions": "Context token counting.",
                                     "criteria": {"included": "Included", "omitted": "Omitted"}}}
        request = self.request_type.model_validate({"state": state, "questions": upstream})
        return self.to_record(request)[0]

    def _encode(self, state: str, questions: list[dict[str, Any]]) -> Any:
        record = self._record(state, questions)
        return self.model.encode(self.tokenizer, record, max_state=4096, max_branch=5120, strict=True)

    @staticmethod
    def _question_payload(question_id: str, question: KevQuestion) -> dict[str, Any]:
        if question.kind == "choice":
            criteria: Any = {option.id: option.description for option in question.options}
        elif question.kind == "noul":
            criteria = {option.id: option.description for option in question.options}
        else:
            criteria = list(question.levels)
        return {"id": question_id, "type": question.kind, "instructions": question.instructions,
                "criteria": criteria}

    def _context_tokens(self, state: str, questions: list[dict[str, Any]], which: str, max_tokens: int,
                        strict: bool) -> list[int]:
        encoded = self._encode(state, questions)
        ids = encoded["ids"]
        state_count = int(encoded["state_tokens"])
        if which == "state":
            result = ids[:state_count]
        elif which == "branches":
            result = ids[state_count:]
        else:
            result = ids
        if strict and len(result) > max_tokens:
            raise ValueError("strict Kev encoding exceeded context budget")
        return result

    def encode_state(self, state_text: str, *, max_tokens: int, strict: bool):
        # Kev's record serializer treats the state as an opaque string.
        encoded = self._encode(state_text, [])
        ids = encoded["ids"][:int(encoded["state_tokens"])]
        if strict and len(ids) > max_tokens:
            raise ValueError("strict Kev state encoding exceeded context budget")
        return ids

    def encode_question_branches(self, questions: list[dict[str, Any]], *, max_tokens: int, strict: bool):
        ids = self._context_tokens("", questions, "branches", max_tokens, strict)
        return ids

    def encode_final_sequence(self, state_text: str, questions: list[dict[str, Any]], *, max_tokens: int, strict: bool):
        return self._context_tokens(state_text, questions, "all", max_tokens, strict)

    def infer(self, state_text: str, questions: dict[str, DecisionQuestion]) -> tuple[
        dict[str, DecisionAnswer], float, float
    ]:
        payload = [self._question_payload(qid, question.kev_question()) for qid, question in questions.items()]
        started = time.perf_counter()
        record = self._record(state_text, payload)
        encoded = self.model.encode(self.tokenizer, record, max_state=4096, max_branch=5120, strict=True)
        _record, metadata = self.to_record(self.request_type.model_validate({
            "state": state_text, "questions": self._upstream_questions(payload),
        }))
        prepared = (time.perf_counter() - started) * 1000
        branches = len(encoded["ids"]) - int(encoded["state_tokens"])
        if branches > 1024 or len(encoded["ids"]) > 8192:
            raise ValueError("strict Kev request exceeded validated context limits")
        if self.device == "cuda":
            self.torch.cuda.synchronize()
        started = time.perf_counter()
        with self.torch.inference_mode():
            distributions = self.model.probs(encoded)
        if self.device == "cuda":
            self.torch.cuda.synchronize()
        elapsed = (time.perf_counter() - started) * 1000
        if len(distributions) != len(payload) or len(metadata) != len(payload):
            raise ValueError("Kev probability cardinality mismatch")
        raw_distributions: list[list[float]] = []
        answers: dict[str, DecisionAnswer] = {}
        for (qid, question), raw, meta in zip(questions.items(), distributions, metadata):
            values = raw.tolist() if hasattr(raw, "tolist") else list(raw)
            if len(values) != len(meta["keys"]):
                raise ValueError("Kev option probability cardinality mismatch")
            probs = [float(value) for value in values]
            if not all(math.isfinite(x) and 0 <= x <= 1 for x in probs) or abs(sum(probs) - 1.0) > 1e-5:
                raise ValueError("Kev returned an invalid probability distribution")
            raw_distributions.append(probs)
        upstream_answers = self.to_answers(raw_distributions, metadata)
        for (qid, question), probs, meta in zip(questions.items(), raw_distributions, metadata):
            upstream = upstream_answers[qid]
            item: dict[str, Any] = {}
            if question.type == "choice":
                item["probabilities"] = upstream["probabilities"]
                item["selected"] = upstream["choice"]
                item["confidence"] = upstream["confidence"]
            elif question.type == "yes_no":
                item["probabilities"] = {"false": probs[0], "true": probs[1]}
                item["probability_true"] = upstream["noul"]
            else:
                item["probabilities"] = upstream["probabilities"]
                item["expected_score"] = upstream["score"]
                item["confidence"] = upstream["confidence"]
            answers[qid] = DecisionAnswer(**item)
        return answers, prepared, elapsed


def load_adapter(*, source: str, artifact_root: str, checkpoint_path: str, base_path: str,
                 manifest_path: str, device: str) -> KevAdapter:
    source_path, root = Path(source).resolve(), Path(artifact_root).resolve()
    checkpoint_dir, base_dir = Path(checkpoint_path).resolve(), Path(base_path).resolve()
    if checkpoint_dir != (root / "kev").resolve() or base_dir != (root / "base").resolve():
        raise KevUnavailable("model paths must point to the verified local artifact directories")
    verify_artifacts(root, source_path, Path(manifest_path))
    if device not in {"cpu", "cuda"}:
        raise KevUnavailable("unsupported model device")
    sys.path.insert(0, str(source_path))
    try:
        import torch
        torch.set_num_threads(4)
        torch.set_num_interop_threads(1)
        if device == "cuda" and not torch.cuda.is_available():
            raise KevUnavailable("CUDA is unavailable")
        from kev.api import SystemOneRequest, choice_confidence, score_confidence, to_answers, to_record
        from kev.checkpoint import Checkpoint, LoadOptions
        checkpoint = Checkpoint(checkpoint_path)
        if checkpoint.meta.base_revision != EXPECTED_BASE or abs(float(checkpoint.meta.temperature) - 2.3510958125672174) > 1e-9:
            raise KevUnavailable("checkpoint metadata did not match the pinned model")
        if checkpoint.meta.base != "Qwen/Qwen3.5-0.8B-Base":
            raise KevUnavailable("checkpoint base model did not match the pinned model")
        # Kev's public loader resolves Hub IDs. Point its existing loader calls at the
        # byte-verified offline snapshot while retaining the same model/revision metadata.
        checkpoint.meta.base = str(Path(base_path).resolve())
        tokenizer, model = checkpoint.load(
            device, LoadOptions(dtype=torch.float32, backend="torch", temperature=None,
                                merge=True, attn=None, cuda_graphs=False, fused=False),
        )
        model.eval()
        return KevAdapter(model=model, tokenizer=tokenizer, request_type=SystemOneRequest, to_record=to_record,
                          to_answers=to_answers, choice_confidence=choice_confidence,
                          score_confidence=score_confidence, checkpoint=checkpoint, torch=torch, device=device)
    except KevUnavailable:
        raise
    except Exception as exc:
        raise KevUnavailable("pinned local model could not be loaded") from exc
