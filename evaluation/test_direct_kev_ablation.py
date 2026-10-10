import unittest
from unittest.mock import patch
import json
from pathlib import Path
import tempfile

from direct_kev_ablation import LABELS, metrics, run


class DirectMetricsTests(unittest.TestCase):
    def rows(self):
        return [{"arm": "graph_on", "gold": label, "prediction": label, "status": "evaluated"}
                for label in LABELS for _ in range(20)]

    def test_complete_perfect_suite_passes(self):
        result = metrics(self.rows(), "graph_on")
        self.assertEqual(result["denominator"], 60)
        self.assertEqual(result["macro_f1"], 1)
        self.assertTrue(result["passes_quality_gate"])

    def test_failed_assessments_remain_false_negatives(self):
        rows = self.rows()
        for row in rows[:3]:
            row.update(prediction=None, status="failed")
        result = metrics(rows, "graph_on")
        self.assertEqual(result["denominator"], 60)
        self.assertEqual(result["classes"]["conflict"]["recall"], .85)
        self.assertFalse(result["passes_quality_gate"])

    def test_missing_rows_cannot_pass(self):
        self.assertFalse(metrics(self.rows()[:-1], "graph_on")["passes_quality_gate"])

    def test_loading_failure_persists_full_denominator_without_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.json"
            with patch("direct_kev_ablation.load_adapter", side_effect=RuntimeError("load failed")):
                with self.assertRaises(RuntimeError):
                    run(output)
            result = json.loads(output.read_text())
            self.assertEqual(len(result["rows"]), 120)
            self.assertTrue(all(row["status"] == "failed" and "answers" not in row for row in result["rows"]))
            for arm in ("graph_on", "graph_off"):
                self.assertEqual(result["metrics"][arm]["denominator"], 60)
                self.assertFalse(result["metrics"][arm]["passes_quality_gate"])


if __name__ == "__main__":
    unittest.main()
