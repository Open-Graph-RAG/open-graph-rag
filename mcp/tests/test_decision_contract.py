import json
import sys
import unittest
from pathlib import Path

import httpx
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decision_contract import DecisionRequest, validate_decision_request
from decision_context import build_decision_context, evidence_fingerprint, new_evidence_fingerprints
from decision_retrieval import DecisionRetriever, RetrievalError, normalize_retrieval_response


def request_data():
    return {
        "objective": "Determine whether the two proposed behaviors conflict.",
        "query": "approval timing for plan changes",
        "questions": {
            "assessment": {
                "type": "choice",
                "instructions": "Classify the evidence.",
                "options": [
                    {"id": "conflict", "description": "Claims conflict."},
                    {"id": "compatible", "description": "Claims can coexist."},
                ],
            },
            "scope_ok": {
                "type": "yes_no",
                "instructions": "Does the evidence cover the same workflow?",
                "true_description": "Same workflow.",
            },
            "severity": {
                "type": "score",
                "instructions": "Rate the impact.",
                "levels": ["low", "medium", "high"],
            },
        },
    }


class DecisionContractTests(unittest.TestCase):
    def test_accepts_all_question_kinds_and_translates_yes_no_to_noul(self):
        request = validate_decision_request(request_data())
        self.assertIsInstance(request, DecisionRequest)
        self.assertEqual(request.questions["scope_ok"].kev_question().kind, "noul")
        self.assertEqual(request.questions["assessment"].kev_question().options[0].id, "conflict")
        self.assertEqual(request.questions["severity"].kev_question().levels[2], "high")

    def test_rejects_extra_fields_and_coercion(self):
        data = request_data()
        data["runtime_options"] = {"temperature": 0}
        with self.assertRaises(ValidationError):
            validate_decision_request(data)
        data = request_data()
        data["questions"]["assessment"]["options"][0]["id"] = 7
        with self.assertRaises(ValidationError):
            validate_decision_request(data)

    def test_rejects_duplicate_option_and_invalid_question_ids(self):
        data = request_data()
        data["questions"]["assessment"]["options"][1]["id"] = "conflict"
        with self.assertRaises(ValidationError):
            validate_decision_request(data)
        data = request_data()
        data["questions"]["bad id"] = data["questions"].pop("severity")
        with self.assertRaises(ValidationError):
            validate_decision_request(data)

    def test_rejects_request_over_64_kib(self):
        data = request_data()
        data["objective"] = "x" * 4000
        data["supplied_evidence"] = [{
            "id": f"evidence_{n}", "text": "y" * 8000,
            "source_locator": "z" * 2000, "origin": "caller",
        } for n in range(8)]
        with self.assertRaises(ValueError):
            validate_decision_request(data)

    def test_rejects_duplicate_keys_in_raw_json(self):
        raw = ('{"objective":"Valid decision objective","query":"valid query",'
               '"questions":{"same":{"type":"yes_no","instructions":"Is scope aligned?"},'
               '"same":{"type":"yes_no","instructions":"Is scope aligned?"}}}')
        with self.assertRaises(ValueError):
            validate_decision_request(raw)


class DecisionRetrieverTests(unittest.IsolatedAsyncioTestCase):
    def payload(self):
        return {
            "status": "success", "metadata": {"mode": "mix"},
            "data": {
                "chunks": [
                    {"chunk_id": "c1", "content": "Approval is required.", "reference_id": "1", "file_path": "CP-248"},
                    {"chunk_id": "c2", "content": "It applies immediately.", "reference_id": "1", "file_path": "FLOW-02"},
                ],
                "references": [{"reference_id": "1", "file_path": "ref.txt"}],
                "entities": [{"entity_name": "Plan", "description": "A customer plan.", "source_id": "c1"}],
                "relationships": [{"src_id": "CP-248", "tgt_id": "FLOW-02", "description": "Describes same workflow.", "source_id": "c1<SEP>c2"}],
            },
        }

    async def test_fixed_query_and_exact_source_links(self):
        async def handler(request):
            self.assertEqual(request.url.path, "/query/data")
            body = json.loads(request.content)
            self.assertEqual(body, {
                "query": "approval timing", "mode": "mix", "top_k": 12,
                "chunk_top_k": 8, "max_entity_tokens": 1500,
                "max_relation_tokens": 1500, "max_total_tokens": 6000,
                "enable_rerank": False,
            })
            return httpx.Response(200, json=self.payload())

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = await DecisionRetriever("http://lightrag", "secret", client=client).retrieve("approval timing")
        self.assertEqual([p.local_id for p in result.passages], ["r1-p1", "r1-p2"])
        self.assertEqual(result.assertions[0].supporting_passage_ids, ("r1-p1",))
        self.assertEqual(result.assertions[1].supporting_passage_ids, ("r1-p1", "r1-p2"))
        self.assertEqual(result.assertions[0].kind, "entity")
        self.assertEqual(result.upstream_order, ("r1-p1", "r1-p2", "r1-g1", "r1-g2"))
        await client.aclose()

    async def test_unresolved_graph_sources_are_reported_and_never_linked(self):
        payload = self.payload()
        payload["data"]["entities"][0]["source_id"] = "missing"
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
        result = await DecisionRetriever("http://lightrag", "secret", client=client).retrieve("approval timing")
        self.assertEqual(result.unresolved_assertion_ids, ("r1-g1",))
        self.assertEqual([assertion.local_id for assertion in result.assertions], ["r1-g2"])
        await client.aclose()

    async def test_mismatched_reference_locator_is_preserved_as_unresolved(self):
        payload = self.payload()
        payload["data"]["references"] = [{"reference_id": "1", "file_path": "other.txt"}]
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
        result = await DecisionRetriever("http://lightrag", "secret", client=client).retrieve("approval timing")
        self.assertEqual(result.passages[0].reference_id, "1")
        self.assertIsNone(result.passages[0].original_reference)
        self.assertIn("r1-p1", result.unresolved_reference_ids)
        self.assertEqual(result.passages[0].upstream_metadata["file_path"], "CP-248")
        await client.aclose()

    async def test_mismatched_graph_reference_keeps_each_supporting_reference_separate(self):
        payload = self.payload()
        payload["data"]["references"] = [{"reference_id": "1", "file_path": "CP-248"}]
        payload["data"]["entities"][0]["reference_id"] = "1"
        payload["data"]["entities"][0]["file_path"] = "elsewhere.txt"
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
        result = await DecisionRetriever("http://lightrag", "secret", client=client).retrieve("approval timing")
        entity = result.assertions[0]
        self.assertIsNone(entity.original_reference)
        self.assertIn("r1-g1", result.unresolved_reference_ids)
        self.assertEqual(entity.supporting_sources[0].original_reference["file_path"], "CP-248")
        await client.aclose()

    async def test_partially_unresolved_graph_sources_are_visible(self):
        payload = self.payload()
        payload["data"]["entities"][0]["source_id"] = "c1<SEP>missing-chunk"
        result = normalize_retrieval_response(payload)
        self.assertEqual(result.assertions[0].supporting_passage_ids, ("r1-p1",))
        self.assertEqual(result.assertions[0].unresolved_source_ids, ("missing-chunk",))
        self.assertEqual(result.unresolved_source_links[0].assertion_id, "r1-g1")

    async def test_rejects_response_over_one_mib(self):
        content = b"{" + (b" " * (1024 * 1024)) + b"}"
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content)))
        with self.assertRaises(RetrievalError):
            await DecisionRetriever("http://lightrag", "secret", client=client).retrieve("approval timing")
        await client.aclose()


class FakeKevEncoder:
    """Deterministic stand-in that counts the entire escaped input record."""

    @staticmethod
    def _encode(text, *, max_tokens, strict):
        tokens = text.split()
        if strict and len(tokens) > max_tokens:
            raise ValueError("strict fake encoder overflow")
        return list(range(len(tokens)))

    def encode_state(self, state_text, *, max_tokens, strict):
        return self._encode("<state> " + state_text, max_tokens=max_tokens, strict=strict)

    def encode_question_branches(self, questions, *, max_tokens, strict):
        return self._encode(json.dumps(questions, ensure_ascii=False), max_tokens=max_tokens, strict=strict)

    def encode_final_sequence(self, state_text, questions, *, max_tokens, strict):
        packed = "<state> " + state_text + " " + json.dumps(questions, ensure_ascii=False)
        return self._encode(packed, max_tokens=max_tokens, strict=strict)


class DecisionContextTests(unittest.TestCase):
    def retrieval(self, *, no_graph=False):
        payload = DecisionRetrieverTests().payload()
        if no_graph:
            payload["data"]["entities"] = [{
                "entity_name": "Unlinked", "description": "No chunk source.", "source_id": "missing",
            }]
            payload["data"]["relationships"] = []
        return normalize_retrieval_response(payload)

    def test_mandatory_order_exact_passages_and_atomic_graph_assertion(self):
        request = validate_decision_request(request_data())
        retrieval = self.retrieval()
        context = build_decision_context(request, retrieval, FakeKevEncoder())
        self.assertEqual(context.status, "ready")
        self.assertEqual(context.included_item_ids[:3], ("r1-p1", "r1-p2", "r1-g1"))
        self.assertEqual(context.items[0].text, "Approval is required.")
        self.assertEqual(context.items[1].text, "It applies immediately.")
        self.assertEqual(context.items[2].kind, "graph_assertion")
        self.assertIn("graph-extracted assertion", context.items[2].text)
        self.assertEqual(context.items[2].supporting_item_ids, ("r1-p1",))
        self.assertEqual(context.token_counts.question_branches, len(
            FakeKevEncoder().encode_question_branches(
                [{"id": qid, **question.kev_question().model_dump(mode="json")}
                 for qid, question in request.questions.items()],
                max_tokens=1024, strict=True,
            )))
        reversed_payload = DecisionRetrieverTests().payload()
        reversed_payload["data"] = dict(reversed(list(reversed_payload["data"].items())))
        reordered = build_decision_context(request, normalize_retrieval_response(reversed_payload), FakeKevEncoder())
        self.assertEqual(reordered.state_text, context.state_text)
        self.assertEqual(reordered.included_item_ids, context.included_item_ids)

    def test_caller_ids_are_separate_from_request_local_ids_and_source_is_intact(self):
        data = request_data()
        data["supplied_evidence"] = [{
            "id": "r1-p1", "text": "LIVE\r\nOnly after approval.",
            "source_locator": "CP-248", "origin": "live Jira MCP",
            "status": "open", "version": "v7",
            "original_reference": {"url": "https://jira.example/CP-248"},
        }]
        context = build_decision_context(validate_decision_request(data), self.retrieval(), FakeKevEncoder())
        self.assertEqual(context.items[0].id, "s1-p1")
        self.assertEqual(context.items[0].text, "LIVE\r\nOnly after approval.")
        self.assertEqual(context.items[0].references[0].caller_evidence_id, "r1-p1")
        self.assertEqual(context.items[0].references[0].version, "v7")
        self.assertEqual(context.items[1].id, "r1-p1")
        self.assertEqual(context.items[1].text, "Approval is required.")
        self.assertEqual(context.items[1].references[0].caller_supplied, False)
        self.assertEqual(context.items[1].references[0].status, None)

    def test_new_versioned_content_remains_separate_and_unseen_fingerprints_are_reported(self):
        data = request_data()
        data["supplied_evidence"] = [{
            "id": "live_claim", "text": "Approval after customer confirmation.",
            "source_locator": "CP-248", "origin": "live Jira MCP",
            "version": "v8", "status": "open",
        }]
        retrieval = self.retrieval()
        request = validate_decision_request(data)
        context = build_decision_context(request, retrieval, FakeKevEncoder())
        self.assertEqual(context.items[0].text, "Approval after customer confirmation.")
        self.assertEqual(context.items[1].text, "Approval is required.")
        self.assertNotEqual(context.items[0].references[0].version, context.items[1].references[0].version)
        seen = evidence_fingerprint("CP-248", "Approval is required.")
        request = request.model_copy(update={"seen_fingerprints": (seen,)})
        fingerprints = new_evidence_fingerprints(request, retrieval)
        self.assertNotIn(seen, fingerprints)
        self.assertTrue(fingerprints)

    def test_graph_package_adds_support_beyond_first_two_passages_and_overflow_is_explicit(self):
        payload = DecisionRetrieverTests().payload()
        payload["data"]["chunks"].append({
            "chunk_id": "c3", "content": "third source", "file_path": "third.txt",
        })
        payload["data"]["entities"] = [{
            "entity_name": "Plan", "description": "Connected to third source.", "source_id": "c3",
        }]
        payload["data"]["relationships"] = []
        context = build_decision_context(validate_decision_request(request_data()),
                                         normalize_retrieval_response(payload), FakeKevEncoder())
        self.assertEqual(context.status, "ready")
        self.assertEqual(context.included_item_ids[:4], ("r1-p1", "r1-p2", "r1-p3", "r1-g1"))
        self.assertEqual(context.items[3].supporting_item_ids, ("r1-p3",))

        payload["data"]["chunks"][2]["content"] = "third " * 5000
        overflow = build_decision_context(validate_decision_request(request_data()),
                                          normalize_retrieval_response(payload), FakeKevEncoder())
        self.assertEqual(overflow.status, "insufficient_context")
        self.assertEqual({(item.id, item.reason) for item in overflow.omitted_items}, {
            ("r1-p1", "token_budget"), ("r1-p2", "token_budget"),
            ("r1-p3", "token_budget"), ("r1-g1", "token_budget"),
        })

    def test_unresolved_graph_is_omitted_and_context_is_insufficient(self):
        context = build_decision_context(validate_decision_request(request_data()),
                                         self.retrieval(no_graph=True), FakeKevEncoder())
        self.assertEqual(context.status, "insufficient_context")
        self.assertIn("no_source_linked_graph_assertion_available", context.limitations.items)
        self.assertIn(("r1-g1", "unresolved_source_link"),
                      [(item.id, item.reason) for item in context.omitted_items])

    def test_optional_passage_is_reported_when_strict_budget_would_overflow(self):
        payload = DecisionRetrieverTests().payload()
        payload["data"]["chunks"].append({
            "chunk_id": "c3", "content": "optional " * 5000,
            "reference_id": "3", "file_path": "large.txt",
        })
        retrieval = normalize_retrieval_response(payload)
        context = build_decision_context(validate_decision_request(request_data()), retrieval, FakeKevEncoder())
        self.assertEqual(context.status, "ready")
        self.assertIn(("r1-p3", "token_budget"),
                      [(item.id, item.reason) for item in context.omitted_items])
        self.assertNotIn("optional", context.items[0].text)

    def test_mandatory_evidence_overflow_returns_no_partial_inference_context(self):
        data = request_data()
        data["supplied_evidence"] = [{
            "id": f"caller_{index}", "text": "x " * 4000,
            "source_locator": f"source-{index}", "origin": "caller",
        } for index in range(4)]
        context = build_decision_context(validate_decision_request(data), self.retrieval(), FakeKevEncoder())
        self.assertEqual(context.status, "insufficient_context")
        self.assertEqual(context.items, ())
        self.assertEqual(context.included_item_ids, ())
        self.assertIn("mandatory_evidence_exceeds_context_budget", context.limitations.items)

    def test_fingerprint_normalizes_line_endings_but_preserves_versions_and_status(self):
        first = evidence_fingerprint("CP-248", "claim\r\ntext", version="7", status="open")
        self.assertEqual(first, evidence_fingerprint("CP-248", "claim\ntext", version="7", status="open"))
        self.assertNotEqual(first, evidence_fingerprint("CP-248", "claim\ntext", version="8", status="open"))
        self.assertNotEqual(first, evidence_fingerprint("CP-248", "claim\ntext", version="7", status="closed"))

if __name__ == "__main__":
    unittest.main()
