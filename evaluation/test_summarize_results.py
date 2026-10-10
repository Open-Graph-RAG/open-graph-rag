import unittest

from evaluation.summarize_results import controlled_mechanics, score_arm, summarize_controlled


class SummarizerTests(unittest.TestCase):
    def test_disclosure_counts_include_only_outcomes_that_require_disclosure(self):
        from evaluation.summarize_results import controlled_mechanics
        score = {"exact_unavailable_disclosure_present": True,
                 "no_inference_disclosed_for_empty_answers": True,
                 "context_gap_not_called_model_assessment": True}
        rows = [{"arm": "baseline", "score": score, "usage": {"decision_statuses": []}},
                {"arm": "baseline", "score": score, "usage": {"decision_statuses": ["failed"]}}]
        metrics = controlled_mechanics(rows, "baseline")
        self.assertEqual(metrics["unavailable_disclosure_required_rows"], 1)
        self.assertEqual(metrics["exact_unavailable_disclosures"], 1)
        self.assertEqual(metrics["no_inference_disclosures"], 1)
        self.assertEqual(metrics["context_gap_disclosures"], 0)

    def test_missing_case_is_a_false_negative_in_full_denominator(self):
        gold = {"heldout-a": "conflict", "heldout-b": "conflict"}
        rows = [{"case_id": "heldout-a", "arm": "graph_off", "status": "evaluated",
                 "prediction": "conflict"}]
        result = score_arm(rows, "graph_off", gold, "direct")
        self.assertEqual(result["denominator"], 2)
        self.assertEqual(result["classes"]["conflict"]["fn"], 1)
        self.assertEqual(result["unparsed_predictions"], 1)

    def test_mechanical_support_never_counts_false_conflicts_as_true_accepts(self):
        row = {"arm": "baseline", "final": {"classification": "conflict"},
               "score": {"gold_classification": "compatible", "gold_citation_pair_with_claim_quotes": True,
                         "all_quotes_exact": True, "all_cited_sources_retrieved": True}}
        result = controlled_mechanics([row], "baseline")
        self.assertEqual(result["conflict_predictions"], 1)
        self.assertEqual(result["true_conflict_predictions"], 0)
        self.assertEqual(result["false_conflict_predictions"], 1)
        self.assertEqual(result["mechanically_supported_true_conflicts"], 0)

    def test_all_completed_slots_are_complete_even_when_one_final_is_malformed(self):
        gold = {f"heldout-{index:02}": "conflict" for index in range(60)}
        rows = []
        for arm in ("baseline", "passage_only_kev", "graph_decision"):
            for index, case_id in enumerate(gold):
                row = {"case_id": case_id, "arm": arm, "status": "completed",
                       "final": {"classification": "conflict"},
                       "score": {"gold_classification": "conflict", "classification": "conflict"}}
                if arm == "baseline" and index == 0:
                    row["final"] = None
                    row["score"]["classification"] = None
                rows.append(row)
        result = summarize_controlled(rows, gold)
        self.assertTrue(result["complete"])
        self.assertEqual(result["completed_rows"], 180)
        self.assertEqual(result["parsed_rows"], 179)
        self.assertAlmostEqual(result["parsed_completeness"], 179 / 180)
        self.assertEqual(result["verdict"], "COMPLETE_REVIEW_REQUIRED")

    def test_unrun_abort_keeps_comparison_incomplete_even_with_full_rows(self):
        gold = {f"heldout-{index:02}": "conflict" for index in range(60)}
        rows = []
        for arm in ("baseline", "passage_only_kev", "graph_decision"):
            for index, case_id in enumerate(gold):
                rows.append({"case_id": case_id, "arm": arm,
                             "status": "not_run_after_abort" if arm == "graph_decision" and index == 59 else "completed",
                             "final": {"classification": "conflict"}})
        result = summarize_controlled(rows, gold)
        self.assertFalse(result["complete"])
        self.assertEqual(result["rows_present"], 180)
        self.assertEqual(result["verdict"], "INCOMPLETE_NO_PASS")

    def test_latency_labels_separate_provider_calls_from_conversation_total(self):
        rows = [{"arm": "baseline", "usage": {
            "model_call_latency_ms": [12, 18],
            "raw_tool_results": [
                {"result": {"timings": {"retrieval_ms": 2, "preparation_ms": 3,
                                         "inference_ms": 4, "ignored_duration_ms": 900}}},
                {"result": {"timings": {"inference_ms": 1}}},
            ],
        }}]
        from evaluation.summarize_results import operational_metrics
        rows.append({"arm": "baseline", "status": "not_run"})
        result = operational_metrics(rows, "baseline", "controlled")
        self.assertEqual(result["provider_request_latency_ms"]["median"], 15)
        lower_bound = result["measured_sequential_component_lower_bound_ms"]
        self.assertEqual(lower_bound["total"], 40)
        self.assertEqual(lower_bound["median_per_conversation"], 40)
        self.assertEqual(lower_bound["conversations"], 1)
        self.assertEqual(lower_bound["components_total_ms"]["retrieval_ms"], 2)
        self.assertIn("lower bound", lower_bound["excludes"])


if __name__ == "__main__":
    unittest.main()
