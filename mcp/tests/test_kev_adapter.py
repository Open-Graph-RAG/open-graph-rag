import json
import math
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decision_contract import validate_decision_request
from kev_adapter import KevAdapter, KevUnavailable, verify_artifacts


class Encoded(dict):
    pass


class FakeModel:
    distributions = [[0.7, 0.3]]
    def encode(self, tokenizer, record, *, max_state, max_branch, strict):
        text = record["state"] + json.dumps(record["questions"])
        state_tokens = len(record["state"].split())
        return Encoded(ids=text.split(), state_tokens=state_tokens)
    def probs(self, encoded):
        return self.distributions


class FakeTorch:
    class inference_mode:
        def __enter__(self): pass
        def __exit__(self, *args): pass


def fake_to_record(request):
    options, metadata = [], []
    for qid, question in request["questions"].items():
        criteria = question["criteria"]
        if question["type"] == "noul":
            keys = ["false", "true"]
            opts = ["no: " + str(criteria.get("false", "False")),
                    "yes: " + str(criteria.get("true", "True"))]
        elif question["type"] == "choice":
            keys, opts = list(criteria), list(criteria.values())
        else:
            keys, opts = [str(i) for i in range(len(criteria))], criteria
        metadata.append({"id": qid, "type": question["type"], "keys": keys,
                         **({"legend": dict(zip(keys, opts))} if question["type"] == "score" else {})})
        options.append({"instr": question["instructions"], "options": opts, "label": 0})
    return {"state": request["state"], "questions": options}, metadata


def fake_to_answers(distributions, metadata):
    result = {}
    for p, meta in zip(distributions, metadata):
        if meta["type"] == "noul":
            result[meta["id"]] = {"type": "noul", "noul": round(p[1], 4)}
        elif meta["type"] == "choice":
            k = len(p)
            result[meta["id"]] = {"choice": meta["keys"][max(range(k), key=p.__getitem__)],
                "probabilities": dict(zip(meta["keys"], (round(x, 4) for x in p))),
                "confidence": round(1 if k == 1 else (max(p) - 1/k)/(1 - 1/k), 4)}
        else:
            mode = max(range(len(p)), key=p.__getitem__)
            spread = sum(abs(i-(len(p)-1)/2) for i in range(len(p))) / len(p)
            confidence = max(0, 1 - sum(prob*abs(i-mode) for i, prob in enumerate(p))/spread)
            result[meta["id"]] = {"score": round(sum(i*x for i, x in enumerate(p)), 4),
                "probabilities": {str(i): round(x, 4) for i, x in enumerate(p)},
                "confidence": round(confidence, 4), "legend": meta["legend"]}
    return result


class KevAdapterTests(unittest.TestCase):
    def setUp(self):
        class RequestType:
            @staticmethod
            def model_validate(value): return value
        self.adapter = KevAdapter(model=FakeModel(), tokenizer=None, request_type=RequestType,
                                  to_record=fake_to_record, to_answers=fake_to_answers,
                                  choice_confidence=lambda probs: 0, score_confidence=lambda probs: 0,
                                  checkpoint=type("Checkpoint", (), {"meta": type("Meta", (), {"temperature": 2.35})()})(),
                                  torch=FakeTorch(), device="cpu")

    def test_adapter_maps_answer_types_and_preserves_first_tie(self):
        req = validate_decision_request({"objective": "Classify source claims.", "query": "source claims",
          "questions": {"choice": {"type": "choice", "instructions": "Which?", "options": [
              {"id": "first", "description": "One"}, {"id": "second", "description": "Two"}]},
            "yn": {"type": "yes_no", "instructions": "Is it?"},
            "score": {"type": "score", "instructions": "How much?", "levels": ["low", "high"]}}})
        self.adapter.model.distributions = [[0.5, 0.5], [0.1, 0.9], [0.2, 0.8]]
        answers, preparation, inference = self.adapter.infer("evidence", req.questions)
        self.assertEqual(answers["choice"].selected, "first")
        self.assertEqual(answers["yn"].probability_true, 0.9)
        self.assertEqual(answers["score"].expected_score, 0.8)
        self.assertEqual(answers["choice"].confidence, 0.0)
        self.assertEqual(answers["score"].confidence, 0.6)
        self.assertGreaterEqual(preparation, 0)
        self.assertGreaterEqual(inference, 0)

    def test_rejects_probability_cardinality_and_invalid_values_without_renormalizing(self):
        req = validate_decision_request({"objective": "Classify source claims.", "query": "source claims",
          "questions": {"choice": {"type": "choice", "instructions": "Which?", "options": [
              {"id": "first", "description": "One"}, {"id": "second", "description": "Two"}]}}})
        self.adapter.model.distributions = [[0.4, 0.4]]
        with self.assertRaises(ValueError):
            self.adapter.infer("evidence", req.questions)
        self.adapter.model.distributions = [[math.nan, math.nan]]
        with self.assertRaises(ValueError):
            self.adapter.infer("evidence", req.questions)
        self.adapter.model.distributions = [[0.4]]
        with self.assertRaises(ValueError):
            self.adapter.infer("evidence", req.questions)

    def test_manifest_requires_pinned_commits_and_exact_artifact_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("kev", "base"):
                (root / name).mkdir()
                (root / name / "weights").write_bytes(name.encode())
            source = root / "source"
            source.mkdir()
            (source / ".git").mkdir()
            files = []
            for name in ("kev", "base"):
                path = root / name / "weights"
                import hashlib
                files.append({"path": f"{name}/weights", "bytes": path.stat().st_size,
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            manifest = root / "manifest.json"
            from kev_adapter import EXPECTED_BASE, EXPECTED_KEV, EXPECTED_SOURCE
            manifest.write_text(json.dumps({"schema_version": 1, "kev_source_commit": EXPECTED_SOURCE,
                "kev_model_revision": EXPECTED_KEV, "base_revision": EXPECTED_BASE, "files": files}))
            with patch("kev_adapter.subprocess.check_output", side_effect=[EXPECTED_SOURCE + "\n", ""]):
                verify_artifacts(root, source, manifest)
            (root / "kev" / "weights").write_bytes(b"tampered")
            with self.assertRaises(KevUnavailable):
                with patch("kev_adapter.subprocess.check_output", side_effect=[EXPECTED_SOURCE + "\n", ""]):
                    verify_artifacts(root, source, manifest)

    def test_pinned_kev_serializer_and_helpers_keep_canonical_yes_no_and_confidence(self):
        source = os.environ.get("KEV_SOURCE")
        if not source or not Path(source, "kev", "api.py").is_file():
            self.skipTest("set KEV_SOURCE to the pinned Kev checkout for API parity")
        sys.path.insert(0, source)
        from kev.api import SystemOneRequest, choice_confidence, score_confidence, to_answers, to_record
        from kev_adapter import KevAdapter
        request = SystemOneRequest.model_validate({"state": "evidence", "questions": {
            "yes_no": {"type": "noul", "instructions": "Is it?", "criteria": {
                "true": "Yes", "false": "No"}},
            "choice": {"type": "choice", "instructions": "Which?", "criteria": {
                "a": "A", "b": "B"}},
            "score": {"type": "score", "instructions": "How much?", "criteria": ["low", "high"]},
        }})
        record, meta = to_record(request)
        self.assertEqual(record["questions"][0]["options"], ["no: No", "yes: Yes"])
        distributions = [[0.1, 0.9], [0.5, 0.5], [0.2, 0.8]]
        upstream = to_answers(distributions, meta)
        self.assertEqual(upstream["yes_no"]["noul"], 0.9)
        self.assertEqual(upstream["choice"]["confidence"], choice_confidence(distributions[1]))
        self.assertEqual(upstream["score"]["confidence"], score_confidence(distributions[2]))


if __name__ == "__main__":
    unittest.main()
