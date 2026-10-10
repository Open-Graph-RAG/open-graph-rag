from __future__ import annotations
import hashlib, json, os, stat, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from validation.product.adapters.fixture import FixtureAdapter
from validation.product.adapters.http_readonly import HttpAdapter
from validation.product.environments.prepare import prepare_snapshot
from validation.product.scoring.score import score_rows
from validation.product.scoring.aggregate import aggregate
from validation.product.cli import (_atomic_private_write, _private_directory, _validate_case_population,
                                    _validate_result_population, _replay_response_id, preflight,
                                    run_live, DEFAULT_CONFIG)

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

    def test_preflight_manifest_pins_compose_recipe_and_rejects_drift(self):
        cfg=json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td:
            manifest=Path(td)/"manifest.json"
            result=preflight(cfg,DEFAULT_CONFIG,manifest,freeze=True)
            key="environment_recipe"
            self.assertIn(key,result["hashes"])
            recipe=(DEFAULT_CONFIG.parent/cfg["environment_recipe"]).resolve()
            self.assertEqual(result["hashes"][key],hashlib.sha256(recipe.read_bytes()).hexdigest())
            frozen=json.loads(manifest.read_text(encoding="utf-8"))
            frozen["sha256"][key]="0"*64
            manifest.write_text(json.dumps(frozen),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,key):
                preflight(cfg,DEFAULT_CONFIG,manifest)

    def test_preflight_freeze_does_not_write_manifest_before_validation(self):
        cfg=json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td:
            manifest=Path(td)/"manifest.json"
            with patch("validation.product.cli._validate_case_population",side_effect=ValueError("invalid population")):
                with self.assertRaisesRegex(ValueError,"invalid population"):
                    preflight(cfg,DEFAULT_CONFIG,manifest,freeze=True)
            self.assertFalse(manifest.exists())

    def test_scoring_input_must_contain_each_expected_case_arm_repeat_once(self):
        cfg={"arms":["baseline","candidate"],"repeats":1}
        dev=[{"id":"dev-a","scenario_id":"s1","category":"multi_hop"},{"id":"dev-b","scenario_id":"s2","category":"freshness"}]
        held=[{"id":"held-a","scenario_id":"s3","category":"conflicts"}]
        rows=[{"case_id":case["id"],"scenario_id":case["scenario_id"],"category":case["category"],
               "arm":arm,"repeat":1,"run_split":"development","run_id":"r1","expected_slots":4,
               "input_hashes":{"config":"frozen"}} for case in dev for arm in cfg["arms"]]
        hashes={"config":"frozen"}
        _validate_result_population(rows,cfg,dev,held,expected_hashes=hashes)
        with self.assertRaisesRegex(ValueError,"missing"):
            _validate_result_population(rows[:-1],cfg,dev,held,expected_hashes=hashes)
        with self.assertRaisesRegex(ValueError,"duplicate"):
            _validate_result_population([*rows,rows[0]],cfg,dev,held,expected_hashes=hashes)
        with self.assertRaisesRegex(ValueError,"unexpected"):
            _validate_result_population([*rows[:-1],{**rows[-1],"case_id":"unknown"}],cfg,dev,held,expected_hashes=hashes)
        with self.assertRaisesRegex(ValueError,"metadata"):
            _validate_result_population([*rows[:-1],{**rows[-1],"scenario_id":"wrong"}],cfg,dev,held,expected_hashes=hashes)
        with self.assertRaisesRegex(ValueError,"frozen input hash"):
            _validate_result_population([*rows[:-1],{**rows[-1],"input_hashes":{"config":"other"}}],cfg,dev,held,expected_hashes=hashes)

    def test_replay_fallback_response_ids_are_isolated_per_run(self):
        key=("case-1","baseline",1)
        self.assertNotEqual(_replay_response_id("run-a",key),_replay_response_id("run-b",key))

    def test_live_run_reports_nonzero_when_every_slot_fails_to_initialize(self):
        case={"id":"case-1","scenario_id":"scenario-1","category":"multi_hop","source_ids":[]}
        slot={"case_id":"case-1","arm":"baseline","repeat":1,"status":"not_started"}
        slots={("case-1","baseline",1):slot}
        args=type("Args",(),{"split":"development","approve_paid":False})()
        cfg={"arms":["baseline"],"repeats":1,"bootstrap":{"seed":1}}
        with tempfile.TemporaryDirectory() as td:
            output=Path(td)/"run.jsonl"
            with patch("validation.product.cli.preflight",return_value={"hashes":{}}), \
                 patch("validation.product.cli._load",return_value=({},[],[],[case],[])), \
                 patch("validation.product.cli._new_journal",return_value=(output,"r1",slots)), \
                 patch("validation.product.cli._flush_journal"), \
                 patch("validation.product.cli._schedule",return_value=[(case,"baseline",1)]):
                self.assertEqual(run_live(args,cfg,DEFAULT_CONFIG,None),2)

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

    def test_paired_bootstrap_omits_scenarios_with_different_case_sets(self):
        rows=[
            {"scenario_id":"s1","case_id":"c1","arm":"baseline","completed":True,"grounded_task_success":True},
            {"scenario_id":"s1","case_id":"c2","arm":"candidate","completed":True,"grounded_task_success":True},
        ]
        result=aggregate({"rows":rows},["baseline","candidate"],100,4)
        self.assertEqual(result["paired_complete_scenario_count"],0)
        self.assertIsNone(result["paired_candidate_minus_baseline"]["completion_rate"]["point_estimate"])

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

    def test_live_retrieval_metrics_use_ranked_chunks_and_frozen_source_mapping(self):
        row={"case_id":"c","scenario_id":"s","category":"multi_hop","arm":"candidate","repeat":1,
             "status":"completed","visible_source_ids":["a/source.txt","b/source.txt"],
             "evidence_status":"LIVE_NOT_YET_REVIEWED","evidence":{
                 "entities":[{"source_id":"not-a-chunk"}],
                 "relationships":[{"file_path":"not-a-chunk"}],
                 "chunks":[{"file_path":"/app/inputs/a/source.txt","chunk_id":"light-rag-chunk-1"},
                           {"file_path":"/app/inputs/b/source.txt","chunk_id":"light-rag-chunk-2"}]}}
        gold={"c":{"expected":{"supporting_sources":["a/source.txt"],"required_claims":[],"must_not_claim":[],"known_unknowns":[]},
                   "evidence":[{"source_id":"a/source.txt","version":"1","evidence_id":"a/source.txt#body"}]}}
        corpus=[{"source_id":"a/source.txt","version":"1","evidence_id":"a/source.txt#body"},
                {"source_id":"b/source.txt","version":"2","evidence_id":"b/source.txt#body"}]
        result=score_rows([row],gold,corpus=corpus)["rows"][0]
        self.assertEqual(result["retrieval_source_ids"],["a/source.txt","b/source.txt"])
        self.assertEqual(result["retrieval_evidence_precision"],0.5)
        self.assertEqual(result["retrieval_reciprocal_rank"],1.0)
        self.assertEqual(result["source_version_resolution_rate"],1.0)

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
