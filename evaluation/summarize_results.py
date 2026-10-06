"""Build a deterministic, denominator-preserving summary of held-out evaluation results."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import statistics
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evaluation" / "decision_cases.json"
DIRECT_DEFAULT = ROOT / "evaluation" / "direct-results.json"
CONTROLLED_DEFAULT = ROOT / "evaluation" / "results.jsonl"
SEMANTIC_DEFAULT = ROOT / "evaluation" / "semantic-review.json"
LABELS = ("conflict", "compatible", "insufficient")
ARMS = {
    "direct": ("graph_off", "graph_on"),
    "controlled": ("baseline", "passage_only_kev", "graph_decision"),
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"expected JSON object at {path}:{line_number}")
        rows.append(row)
    return rows


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


def classification(row: dict[str, Any], kind: str) -> str | None:
    if kind == "direct":
        value = row.get("prediction")
    else:
        value = (row.get("final") or {}).get("classification")
        if value is None:
            value = (row.get("score") or {}).get("classification")
    return value if value in LABELS else None


def score_arm(rows: list[dict[str, Any]], arm: str, gold_by_id: dict[str, str], kind: str) -> dict[str, Any]:
    arm_rows = [row for row in rows if row.get("arm") == arm]
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in arm_rows:
        if isinstance(row.get("case_id"), str):
            by_id[row["case_id"]].append(row)

    predictions: dict[str, str | None] = {}
    statuses: Counter[str] = Counter()
    parsed = 0
    for case_id in gold_by_id:
        candidate_rows = by_id.get(case_id, [])
        row = candidate_rows[-1] if candidate_rows else {}
        prediction = classification(row, kind)
        predictions[case_id] = prediction
        parsed += prediction is not None
        status = row.get("status", "missing") if row else "missing"
        statuses[str(status)] += 1

    confusion = {gold: {pred: 0 for pred in (*LABELS, "unparsed")} for gold in LABELS}
    class_scores = {}
    for case_id, gold in gold_by_id.items():
        pred = predictions[case_id] or "unparsed"
        confusion[gold][pred] += 1
    for label in LABELS:
        tp = confusion[label][label]
        fp = sum(confusion[other][label] for other in LABELS if other != label)
        fn = sum(confusion[label][pred] for pred in (*LABELS, "unparsed") if pred != label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        class_scores[label] = {"tp": tp, "fp": fp, "fn": fn,
                               "precision": precision, "recall": recall,
                               "f1": 2 * precision * recall / (precision + recall)
                               if precision + recall else 0.0}
    macro_f1 = sum(score["f1"] for score in class_scores.values()) / len(LABELS)
    expected_count = len(gold_by_id)
    unique_count = sum(bool(by_id.get(case_id)) for case_id in gold_by_id)
    duplicate_rows = sum(max(0, len(by_id.get(case_id, [])) - 1) for case_id in gold_by_id)
    completed_status_value = "evaluated" if kind == "direct" else "completed"
    completed_rows = sum(
        1 for case_id in gold_by_id
        if by_id.get(case_id)
        and by_id[case_id][-1].get("status") == completed_status_value
    )
    completed_and_parsed = sum(
        1 for case_id in gold_by_id
        if by_id.get(case_id)
        and by_id[case_id][-1].get("status") == completed_status_value
        and classification(by_id[case_id][-1], kind) is not None
    )
    return {"denominator": expected_count, "rows_present": len(arm_rows), "unique_cases_present": unique_count,
            "duplicate_rows": duplicate_rows, "status_completed": completed_status_value,
            "completed_rows": completed_rows, "completed_and_parsed": completed_and_parsed,
            "parsed": parsed,
            "status_counts_in_denominator": dict(statuses), "confusion_matrix": confusion,
            "classes": class_scores, "macro_f1": macro_f1,
            "false_conflicts": class_scores["conflict"]["fp"],
            "insufficient_predictions": sum(1 for value in predictions.values() if value == "insufficient"),
            "unparsed_predictions": sum(1 for value in predictions.values() if value is None)}


def controlled_mechanics(rows: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    selected = [row for row in rows if row.get("arm") == arm]
    conflict_predictions = [row for row in selected if classification(row, "controlled") == "conflict"]
    true_conflict_predictions = [row for row in conflict_predictions
                                 if (row.get("score") or {}).get("gold_classification") == "conflict"]
    false_conflict_predictions = len(conflict_predictions) - len(true_conflict_predictions)
    supported = []
    citation_errors = 0
    citation_precisions: list[float] = []
    citation_recalls: list[float] = []
    exact_quote_rows = 0
    source_eligible_rows = 0
    owner_flags: list[bool] = []
    qualifier_matches = qualifier_fields = 0
    unavailable_disclosures = no_inference_disclosures = context_gap_disclosures = 0
    insufficient_context_rows = 0
    for row in selected:
        score = row.get("score") or {}
        if score.get("citation_precision") is not None:
            citation_precisions.append(float(score["citation_precision"]))
        if score.get("citation_recall") is not None:
            citation_recalls.append(float(score["citation_recall"]))
        has_claim_citation_error = (score.get("all_cited_sources_retrieved") is False
                                    or score.get("all_quotes_exact") is False
                                    or (score.get("citation_precision") is not None
                                        and score["citation_precision"] < 1.0))
        citation_errors += int(has_claim_citation_error)
        exact_quote_rows += int(score.get("all_quotes_exact") is True)
        source_eligible_rows += int(score.get("all_cited_sources_retrieved") is True)
        if score.get("owner_unknown_when_unsupported") is not None:
            owner_flags.append(bool(score["owner_unknown_when_unsupported"]))
        fields = score.get("qualifier_fields_checked", 0) or 0
        accuracy = score.get("qualifier_field_accuracy")
        if accuracy is not None and fields:
            qualifier_matches += round(float(accuracy) * fields)
            qualifier_fields += fields
        unavailable_disclosures += int(score.get("exact_unavailable_disclosure_present") is True)
        no_inference_disclosures += int(score.get("no_inference_disclosed_for_empty_answers") is True)
        context_gap_disclosures += int(score.get("context_gap_not_called_model_assessment") is True)
        usage = row.get("usage") or {}
        insufficient_context_rows += sum(
            status == "insufficient_context" for status in usage.get("decision_statuses", [])
        )
        if classification(row, "controlled") == "conflict" and score.get("gold_classification") == "conflict":
            supported.append(bool(
                score.get("gold_citation_pair_with_claim_quotes") is True
                and score.get("all_quotes_exact") is True
                and score.get("all_cited_sources_retrieved") is True
            ))
    return {
        "conflict_predictions": len(conflict_predictions),
        "true_conflict_predictions": len(true_conflict_predictions),
        "false_conflict_predictions": false_conflict_predictions,
        "mechanically_supported_true_conflicts": sum(supported),
        "unsupported_true_conflict_predictions": len(supported) - sum(supported),
        "support_rule": "gold pair covered by claim quotes AND all quotes exact AND every cited source retrieved",
        "rows_with_citation_errors": citation_errors,
        "mean_citation_precision": statistics.mean(citation_precisions) if citation_precisions else None,
        "mean_citation_recall": statistics.mean(citation_recalls) if citation_recalls else None,
        "rows_with_all_quotes_exact": exact_quote_rows,
        "rows_with_all_citations_retrieved": source_eligible_rows,
        "owner_exact_match_proxy_rows": sum(owner_flags),
        "owner_exact_match_proxy_scored_rows": len(owner_flags),
        "owner_and_qualifier_note": "mechanical comparison to hidden labels; proxy only, not semantic adjudication",
        "qualifier_field_exact_matches_proxy": qualifier_matches,
        "qualifier_fields_checked": qualifier_fields,
        "exact_unavailable_disclosures": unavailable_disclosures,
        "no_inference_disclosures": no_inference_disclosures,
        "context_gap_disclosures": context_gap_disclosures,
        "insufficient_context_tool_statuses": insufficient_context_rows,
    }


def operational_metrics(rows: list[dict[str, Any]], arm: str, kind: str) -> dict[str, Any]:
    selected = [row for row in rows if row.get("arm") == arm]
    if kind == "direct":
        def summary(field: str) -> dict[str, Any]:
            values = [float(row[field]) for row in selected if isinstance(row.get(field), (int, float))]
            return {"median": statistics.median(values) if values else None,
                    "p95": percentile(values, .95), "n": len(values)}
        return {"end_to_end_ms": summary("total_ms"), "inference_ms": summary("inference_ms"),
                "preparation_ms": summary("preparation_ms"),
                "tool_use": "not recorded by direct ablation",
                "provider_usage": "not applicable; local inference"}

    totals = Counter()
    latencies: list[float] = []
    tool_counts: Counter[str] = Counter()
    novelty_counts: list[int] = []
    expansion = Counter()
    sequential_lower_bounds: list[float] = []
    measured_components = Counter()
    timing_keys = ("retrieval_ms", "preparation_ms", "inference_ms")
    for row in selected:
        usage = row.get("usage") or {}
        provider = usage.get("provider_usage") or {}
        for field in ("generation_calls", "prompt_tokens", "completion_tokens", "cached_input_tokens",
                      "reasoning_output_tokens"):
            totals[field] += provider.get(field, 0) if isinstance(provider.get(field, 0), int) else 0
        request_latencies = [float(value) for value in usage.get("model_call_latency_ms", [])
                             if isinstance(value, (int, float)) and not isinstance(value, bool)]
        latencies.extend(request_latencies)
        components = Counter()
        components["model_call_latency_ms"] += sum(request_latencies)
        for call in usage.get("tool_use", []):
            if isinstance(call, dict):
                tool_counts[call.get("tool", "unknown")] += 1
        for key in ("calls", "decision_attempts", "decision_executed", "decision_recipe_violations",
                    "duplicate_decision_queries", "search_attempts", "expansion_searches",
                    "expansion_new_sources", "inference_completed"):
            expansion[key] += usage.get(key, 0) if isinstance(usage.get(key, 0), int) else 0
        for tool in usage.get("raw_tool_results", []):
            result = tool.get("result", {}) if isinstance(tool, dict) else {}
            fingerprints = result.get("novelty_fingerprints")
            if isinstance(fingerprints, list):
                novelty_counts.append(len(fingerprints))
            timings = result.get("timings") if isinstance(result, dict) else None
            if isinstance(timings, dict):
                for key in timing_keys:
                    value = timings.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                        components[key] += float(value)
        if request_latencies or any(components[key] for key in timing_keys):
            sequential_lower_bounds.append(sum(components.values()))
        measured_components.update(components)
    return {"provider_request_latency_ms": {"median": statistics.median(latencies) if latencies else None,
                                             "p95": percentile(latencies, .95), "n": len(latencies)},
            "measured_sequential_component_lower_bound_ms": {
                "total": sum(sequential_lower_bounds),
                "median_per_conversation": statistics.median(sequential_lower_bounds)
                if sequential_lower_bounds else None,
                "p95_per_conversation": percentile(sequential_lower_bounds, .95),
                "conversations": len(sequential_lower_bounds),
                "components_total_ms": dict(measured_components),
                "components": ["model_call_latency_ms sum", *timing_keys],
                "excludes": "tool dispatch/serialization, unreported work, model cold-load; lower bound, not observed wall-clock end-to-end",
            },
            "tool_calls_by_name": dict(tool_counts), "tool_attempt_and_expansion_totals": dict(expansion),
            "decision_novelty_fingerprints": {"calls_with_measure": len(novelty_counts),
                                               "total": sum(novelty_counts),
                                               "mean_per_measured_call": statistics.mean(novelty_counts)
                                               if novelty_counts else None},
            "provider_usage": dict(totals)}


def summarize_direct(document: dict[str, Any], gold_by_id: dict[str, str]) -> dict[str, Any]:
    rows = document.get("rows", [])
    result = {arm: score_arm(rows, arm, gold_by_id, "direct") for arm in ARMS["direct"]}
    for arm in ARMS["direct"]:
        result[arm]["operational"] = operational_metrics(rows, arm, "direct")
        metrics = result[arm]
        metrics["passes_quality_gate"] = bool(
            metrics["completed_and_parsed"] == len(gold_by_id)
            and metrics["classes"]["conflict"]["precision"] >= .90
            and metrics["classes"]["conflict"]["recall"] >= .90
            and metrics["macro_f1"] >= .85
        )
    attempted = all(
        result[arm]["unique_cases_present"] == len(gold_by_id)
        and "not_run" not in result[arm]["status_counts_in_denominator"]
        and "not_run_after_abort" not in result[arm]["status_counts_in_denominator"]
        for arm in ARMS["direct"]
    )
    return {"arms": result, "verdict": "INCOMPLETE_NO_PASS" if not attempted
        else "PASS" if all(result[arm]["passes_quality_gate"] for arm in ARMS["direct"])
        else "FAIL_QUALITY_GATE", "quality_gate": "each arm: conflict precision >= .90, conflict recall >= .90, macro F1 >= .85"}


def summarize_controlled(rows: list[dict[str, Any]], gold_by_id: dict[str, str]) -> dict[str, Any]:
    arms = {}
    expected_total = len(gold_by_id) * len(ARMS["controlled"])
    for arm in ARMS["controlled"]:
        arm_metrics = score_arm(rows, arm, gold_by_id, "controlled")
        arm_metrics["mechanical_review"] = controlled_mechanics(rows, arm)
        arm_metrics["operational"] = operational_metrics(rows, arm, "controlled")
        arms[arm] = arm_metrics
    complete = len(rows) == expected_total and all(
        arms[arm]["unique_cases_present"] == len(gold_by_id)
        and arms[arm]["completed_rows"] == len(gold_by_id)
        and arms[arm]["duplicate_rows"] == 0
        for arm in ARMS["controlled"]
    )
    parsed_total = sum(arms[arm]["parsed"] for arm in ARMS["controlled"])
    return {"expected_rows": expected_total, "rows_present": len(rows), "completed_rows": sum(
                arms[arm]["completed_rows"] for arm in ARMS["controlled"]),
            "parsed_rows": parsed_total, "parsed_completeness": parsed_total / expected_total,
            "complete": complete,
            "verdict": "COMPLETE_REVIEW_REQUIRED" if complete else "INCOMPLETE_NO_PASS",
            "arms": arms}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direct", type=Path, default=DIRECT_DEFAULT)
    parser.add_argument("--controlled", type=Path, default=CONTROLLED_DEFAULT)
    parser.add_argument("--semantic-review", type=Path, default=SEMANTIC_DEFAULT)
    parser.add_argument("--output", type=Path, help="write summary JSON here instead of stdout")
    args = parser.parse_args()
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    gold_by_id = {case["id"]: case["label"] for case in dataset["cases"] if case["split"] == "heldout"}
    if len(gold_by_id) != 60:
        raise ValueError("expected 60 held-out gold cases")

    summary: dict[str, Any] = {"heldout_denominator_per_arm": len(gold_by_id)}
    if args.direct.is_file():
        summary["direct"] = summarize_direct(json.loads(args.direct.read_text(encoding="utf-8")), gold_by_id)
    if args.controlled.is_file():
        summary["controlled"] = summarize_controlled(read_jsonl(args.controlled), gold_by_id)
    if args.semantic_review.is_file():
        summary["semantic_review"] = {"kept_separate": True, "path": str(args.semantic_review),
                                      "content": json.loads(args.semantic_review.read_text(encoding="utf-8"))}
    else:
        summary["semantic_review"] = {"kept_separate": True, "status": "not_present"}
    rendered = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
