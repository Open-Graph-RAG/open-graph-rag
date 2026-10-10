from __future__ import annotations
import json, os, stat, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from validation.product.adapters.fixture import FixtureAdapter
from validation.product.adapters.http_readonly import HttpAdapter
from validation.product.environments.prepare import prepare_snapshot
from validation.product.scoring.score import score_rows
from validation.product.scoring.aggregate import aggregate
from validation.product.cli import _atomic_private_write, _private_directory, _validate_case_population

class HarnessTests(unittest.TestCase):
    def test_frozen_case_counts_categories_and_scenario_separation(self):
        categories=["multi_hop","conflicts","missing_evidence","freshness"]
        dev=[{"scenario_id":f"dev-{s}","category":cat} for s in range(6) for cat in categories]
        held=[{"scenario_id":f"held-{s}","category":cat} for s in range(30) for cat in categories]
        _validate_case_population(dev,held)
        with self.assertRaisesRegex(ValueError,"overlap"):
            _validate_case_population(dev,[{**held[0],"scenario_id":dev[0]["scenario_id"]},*held[1:]])
        with self.assertRaisesRegex(ValueError,"category counts"):
            _validate_case_population(dev,[{**held[0],"category":"unbalanced"},*held[1:]])
        with self.assertRaisesRegex(ValueError,"scenarios"):
            _validate_case_population(dev,[{**case,"scenario_id":"held-one"} for case in held])

    def test_fixture_ignores_gold_and_marks_non_lightrag(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"f.jsonl"; p.write_text(json.dumps({"case_id":"c","answers":{"baseline":{"classification":"ok"}}})+"\n")
            got=FixtureAdapter(p).answer({"id":"c"},[{"secret_gold":"not passed"}],"baseline")
            self.assertEqual(got["answer"],{"classification":"ok"})
            self.assertEqual(got["evidence_status"],"FIXTURE_ONLY_NOT_LIGHTRAG_EVIDENCE")

    def test_failed_rows_remain_in_denominator_and_semantics_unscored(self):
        rows=[{"case_id":"c","scenario_id":"s","arm":"baseline","repeat":1,"status":"failed","visible_source_ids":[]}, {"case_id":"c","scenario_id":"s","arm":"candidate","repeat":1,"status":"completed","answer":{"classification":"conflict"},"visible_source_ids":[]}]
        gold={"c":{"expected":{"required_claims":[],"supporting_sources":[],"must_not_claim":[],"known_unknowns":[]}}}
        scored=score_rows(rows,gold)
        self.assertEqual(scored["denominator"],2); self.assertEqual(scored["failed_in_denominator"],1)
        self.assertIsNone(scored["rows"][0]["grounded_task_success"]); self.assertTrue(scored["grounded_task_success_pending_review"])

    def test_paired_scenario_bootstrap_retains_repeats_as_case_average(self):
        rows=[]
        for rep,ok in ((1,True),(2,False)):
            rows.extend([{"scenario_id":"s1","case_id":"c1","repeat":rep,"arm":"baseline","completed":True,"grounded_task_success":None if ok else False,"human_review_complete":False}, {"scenario_id":"s1","case_id":"c1","repeat":rep,"arm":"candidate","completed":True,"grounded_task_success":None if ok else True,"human_review_complete":False}])
        result=aggregate({"rows":rows},["baseline","candidate"],100,4)
        self.assertEqual(result["paired_complete_scenario_count"],1)
        self.assertEqual(result["paired_candidate_minus_baseline"]["grounded_task_success_rate_conservative_unreviewed_as_failure"]["point_estimate"],0.5)

    def test_human_reviews_gate_grounded_task_success_and_category_conflict_recall(self):
        row={"case_id":"c","scenario_id":"s","category":"conflicts","arm":"candidate","repeat":1,"response_id":"blind-1","status":"completed","visible_source_ids":["doc"],"answer":{"citations":["doc"]}}
        gold={"c":{"expected":{"required_claims":["claim"],"supporting_sources":["doc"],"must_not_claim":[],"known_unknowns":[]}}}
        pending=score_rows([row],gold)
        self.assertIsNone(pending["rows"][0]["grounded_task_success"])
        review={"blind_response_id":"blind-1","reviewer_id":"r1","factual_correctness":True,"citations_semantically_supported":True,"state_correct":True,"no_invented_ownership_or_authorization":True,"uncertainty_correct":True,"conflict_recall":True}
        one_review=score_rows([row],gold,[review])
        self.assertIsNone(one_review["rows"][0]["grounded_task_success"])
        self.assertEqual(one_review["rows"][0]["human_review_status"],"requires_two_independent_reviewers")
        review2={**review,"reviewer_id":"r2"}
        done=score_rows([row],gold,[review,review2])
        self.assertTrue(done["rows"][0]["grounded_task_success"])
        self.assertEqual(done["rows"][0]["citation_coverage_diagnostic"],1)
        review["conflict_recall"]=False
        review2["conflict_recall"]=False
        rejected=score_rows([row],gold,[review,review2])
        self.assertFalse(rejected["rows"][0]["grounded_task_success"])

    def test_direct_http_adapter_disabled_until_live_retrieval_wiring(self):
        with self.assertRaisesRegex(RuntimeError,"LightRAG/MCP A/B wiring"):
            HttpAdapter("https://api.example.test/v1","model",{})

    def test_prepare_snapshot_never_overwrites_or_copies_private_files(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); src=root/"src"; src.mkdir(); (src/"snapshot.txt").write_text("sanitized corpus")
            import hashlib
            digest=hashlib.sha256((src/"snapshot.txt").read_bytes()).hexdigest()
            (src/"snapshot-manifest.json").write_text(json.dumps({"schema_version":1,"sanitized":True,"snapshot_id":"test","files":{"snapshot.txt":digest}}))
            dst=root/"copy"; info=prepare_snapshot(src,dst)
            self.assertEqual((dst/"baseline/snapshot.txt").read_text(),"sanitized corpus")
            self.assertEqual(info["arms"]["baseline"]["files_sha256"],info["arms"]["candidate"]["files_sha256"])
            self.assertEqual(stat.S_IMODE(dst.stat().st_mode),0o700)
            self.assertEqual(stat.S_IMODE((dst/"baseline").stat().st_mode),0o700)
            self.assertEqual(stat.S_IMODE((dst/"baseline/snapshot.txt").stat().st_mode),0o600)
            self.assertFalse(info["services_started"]); self.assertFalse(info["host_ports_exposed"])
            with self.assertRaises(FileExistsError): prepare_snapshot(src,dst)

    def test_validation_artifacts_are_private_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            parent=Path(td)/"runs"; parent.mkdir(mode=0o775); parent.chmod(0o775)
            old=parent/"older-answer.jsonl"; old.write_text("previous private answer\n"); old.chmod(0o664)
            artifact=parent/"answer.jsonl"
            _atomic_private_write(artifact,"answer with retrieved evidence\n",private_parent=True)
            _private_directory(parent,secure_contents=True)
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode),0o700)
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE(old.stat().st_mode),0o600)

    def test_disagreeing_peer_cannot_adjudicate_own_response(self):
        row={"case_id":"c","scenario_id":"s","category":"multi_hop","arm":"candidate","repeat":1,"response_id":"blind-2","status":"completed","visible_source_ids":["doc"],"answer":{}}
        gold={"c":{"expected":{"required_claims":[],"supporting_sources":[],"must_not_claim":[],"known_unknowns":[]}}}
        base={"blind_response_id":"blind-2","citations_semantically_supported":True,"state_correct":True,"no_invented_ownership_or_authorization":True,"uncertainty_correct":True}
        r1={**base,"reviewer_id":"r1","factual_correctness":True}; r2={**base,"reviewer_id":"r2","factual_correctness":False}
        self_review={**base,"reviewer_id":"r1","adjudicator_id":"r1","adjudication":{"factual_correctness":True,"citations_semantically_supported":True,"state_correct":True,"no_invented_ownership_or_authorization":True,"uncertainty_correct":True}}
        rejected=score_rows([row],gold,[r1,r2,self_review])["rows"][0]
        self.assertIsNone(rejected["grounded_task_success"])
        self.assertEqual(rejected["human_review_status"],"disagreement_requires_single_adjudication")

if __name__=="__main__": unittest.main()
