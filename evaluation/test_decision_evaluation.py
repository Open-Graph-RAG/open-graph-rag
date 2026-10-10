"""Offline guardrails for the controlled decision evaluation harness."""

from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
from evaluation import decision_evaluation as evaluation


class EvaluationHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = next(case for case in evaluation.load_cases(split="heldout", expected_count=60)
                        if len(case["sources"]) >= 2)

    def test_invalid_provider_token_counts_abort_instead_of_being_ignored(self):
        for value in (True, -1, "4"):
            usage = evaluation.Usage()
            with self.subTest(value=value), self.assertRaises(evaluation.PaidRunAbort):
                usage.add({"prompt_tokens": value, "completion_tokens": 1})

    def test_budget_reserve_enforces_input_output_and_call_caps(self):
        with self.assertRaisesRegex(evaluation.PaidRunAbort, "input-token budget"):
            evaluation.Budget(input_limit=10).reserve(11)
        with self.assertRaisesRegex(evaluation.PaidRunAbort, "output-token budget"):
            evaluation.Budget(output_limit=evaluation.MAX_COMPLETION_TOKENS - 1).reserve(0)
        with self.assertRaisesRegex(evaluation.PaidRunAbort, "generation-call cap"):
            evaluation.Budget(call_limit=0).reserve(0)

    def test_empty_expansion_mapping_still_counts_as_a_targeted_search(self):
        case = dict(self.case)
        case["initial_source_ids"] = [self.case["sources"][0]["id"]]
        case["targeted_query_expansion"] = {"billing renewal timing query": []}
        selected, expanded = evaluation.route_source_ids(case, "billing renewal timing query")
        self.assertEqual(selected, case["initial_source_ids"])
        self.assertTrue(expanded)

    def test_graph_assertion_waits_until_every_linked_source_is_retrieved(self):
        case = dict(self.case)
        case["sources"] = [
            {**self.case["sources"][0], "id": "source-a"},
            {**self.case["sources"][1], "id": "source-b"},
        ]
        case["initial_source_ids"] = ["source-a"]
        case["targeted_query_expansion"] = {}
        case["user_query"] = "Only source A initially"
        case["graph_assertions"] = [{"text": "combined fact", "source_ids": ["source-a", "source-b"]}]
        result = evaluation.retrieval_payload(case, case["user_query"], include_graph=True)
        self.assertEqual(result["data"]["entities"], [])

    def test_gold_case_id_and_fields_are_not_returned_as_public_source(self):
        source = self.case["sources"][0]
        self.assertEqual(set(evaluation.public_source(source)), {"id", "locator", "text"})
        visible = str(evaluation.initial_messages(self.case, "baseline"))
        self.assertNotIn(self.case["id"], visible)
        self.assertNotIn("gold_supporting_source_ids", visible)
        self.assertNotIn("expected_limitations", visible)

    def test_final_parser_rejects_non_string_classification_and_empty_quotes(self):
        self.assertIsNone(evaluation.parse_final_answer('{"classification":[],"claims":[],"limitations":[]}'))
        claim = {"claim": "a", "source_id": "s", "quote": "", "actor": None, "scope": None,
                 "status": None, "effective_time": None, "owner": None}
        payload = {"classification": "insufficient", "claims": [claim], "limitations": []}
        import json
        self.assertIsNone(evaluation.parse_final_answer(json.dumps(payload)))

    def test_client_treats_non_object_choice_as_a_malformed_response(self):
        async def response_handler(_request):
            return httpx.Response(200, json={"choices": [1], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

        async def invoke():
            async with httpx.AsyncClient(transport=httpx.MockTransport(response_handler)) as client:
                return await evaluation.ChatCompletionsClient("https://example.invalid/v1", "test", client=client).complete([], [])

        import asyncio
        reply = asyncio.run(invoke())
        self.assertEqual(reply.error, "malformed_response")
        self.assertEqual(reply.http_status, 200)

    def test_passage_ablation_bypasses_only_the_graph_requirement(self):
        from decision_contract import ContextLimitations, ContextTokenCounts, DecisionContext, DecisionContextItem

        usable = DecisionContext(state_text="source state", items=(DecisionContextItem(
            id="source-1", kind="retrieved_passage", text="passage",
        ),), included_item_ids=("source-1",), omitted_items=(),
            limitations=ContextLimitations(items=("no_source_linked_graph_assertion_available", "other_gap")),
            token_counts=ContextTokenCounts(state=1, question_branches=1, final_sequence=2),
            status="insufficient_context")
        allowed = evaluation.allow_passage_only_context(usable)
        self.assertEqual(allowed.status, "ready")
        self.assertEqual(allowed.limitations.items, ("other_gap",))

        overflow = usable.model_copy(update={"state_text": "", "items": ()})
        self.assertEqual(evaluation.allow_passage_only_context(overflow).status, "insufficient_context")

    def test_model_tool_projection_removes_only_duplicate_state_text(self):
        raw = {"status": "evaluated", "context": {"state_text": "duplicated source text",
                "items": [{"text": "original source excerpt"}], "limitations": ["gap"]},
               "answers": {"assessment": {"selected": "compatible"}}}
        visible = evaluation.model_visible_tool_result(raw)
        self.assertNotIn("state_text", visible["context"])
        self.assertEqual(visible["context"]["items"], raw["context"]["items"])
        self.assertEqual(raw["context"]["state_text"], "duplicated source text")

    def test_citations_require_sources_to_have_been_retrieved(self):
        sources = self.case["sources"]
        answer = {"classification": "conflict", "limitations": [], "claims": [
            {"source_id": source["id"], "quote": source["text"][:240],
             **{field: source[field] for field in ("actor", "scope", "status", "effective_time", "owner")}}
            for source in sources[:2]
        ]}
        state = evaluation.ConversationState(case=self.case, arm="baseline")
        state.raw_tool_results.append({"result": {"data": {"chunks": [{"reference_id": sources[0]["id"]}]}}})
        score = evaluation.score_answer(self.case, answer, state)
        self.assertEqual(score["retrieved_source_ids"], [sources[0]["id"]])
        self.assertFalse(score["all_cited_sources_retrieved"])
        self.assertFalse(score["gold_citation_pair_with_claim_quotes"])
        self.assertEqual(score["citation_precision"], 0.5)
        self.assertEqual(score["quote_count"], 1)


class DecisionAttemptAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_named_decision_calls_consume_the_attempt_cap(self):
        case = evaluation.load_cases(split="heldout", expected_count=60)[0]
        state = evaluation.ConversationState(case=case, arm="graph_decision")
        tools = evaluation.EvaluationTools(evaluation.FixtureRetriever(), object())
        call = {"function": {"name": "decision_evaluate", "arguments": "not-json"}}
        for _ in range(4):
            await tools.dispatch(state, call)
        self.assertEqual(state.decision_attempts, 4)
        self.assertEqual(state.decision_executed, 0)
        self.assertEqual(state.decision_recipe_violations, 4)

    async def test_decision_exception_records_failed_status_for_disclosure_scoring(self):
        class BrokenRuntime:
            async def evaluate(self, _request):
                raise RuntimeError("test failure")

        case = evaluation.load_cases(split="heldout", expected_count=60)[0]
        state = evaluation.ConversationState(case=case, arm="graph_decision")
        tools = evaluation.EvaluationTools(evaluation.FixtureRetriever(), BrokenRuntime())
        from direct_kev_ablation import QUESTIONS
        call = {"function": {"name": "decision_evaluate", "arguments": json.dumps({
            "objective": case["objective"], "query": case["user_query"], "questions": QUESTIONS,
        })}}
        result = await tools.dispatch(state, call)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(state.decision_statuses, ["failed"])


class RunPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_abort_keeps_all_180_denominator_rows(self):
        class ReadyRuntime:
            state = "ready"

            async def close(self):
                return None

        async def aborted_run(_self, case, arm):
            return {"case_id": case["id"], "scenario_id": case["scenario_id"],
                    "split": case["split"], "arm": arm, "error": "http_error"}

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.jsonl"
            with patch("kev_adapter.load_adapter", return_value=object()), \
                 patch.object(evaluation, "DecisionRuntime", return_value=ReadyRuntime()), \
                 patch.object(evaluation.ConversationRunner, "run", aborted_run):
                records = await evaluation.run_evaluation(object(), output)

            saved = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 180)
        self.assertEqual(len(saved), 180)
        self.assertEqual(saved[0]["status"], "aborted")
        self.assertEqual(saved[0]["error"], "http_error")
        self.assertTrue(all(row["status"] == "not_run_after_abort" for row in saved[1:]))


if __name__ == "__main__":
    unittest.main()
