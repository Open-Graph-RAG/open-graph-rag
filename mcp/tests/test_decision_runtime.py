import asyncio
import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decision_contract import ModelMetadata, validate_decision_request
from decision_retrieval import normalize_retrieval_response
from decision_runtime import DecisionRuntime

def request():
    return validate_decision_request({
        "objective": "Classify the documented change.", "query": "documented change",
        "questions": {"kind": {"type": "choice", "instructions": "Which claim is supported?",
                      "options": [{"id": "yes", "description": "Supported."},
                                  {"id": "no", "description": "Not supported."}]}}
    })


def retrieval():
    return normalize_retrieval_response({"status": "success", "data": {
        "chunks": [{"chunk_id": "c1", "content": "A documented source passage.", "file_path": "doc.md"},
                   {"chunk_id": "c2", "content": "A second documented source passage.", "file_path": "doc-2.md"}],
        "references": [], "entities": [{"entity_name": "Change", "description": "A change.",
                                           "source_id": "c1<SEP>c2"}], "relationships": []}})


class FakeAdapter:
    metadata = ModelMetadata(resolved_model="fixture", base="fixture", tokenizer="fixture",
                             code_revision="fixture", dtype="fp32", temperature=1)

    def encode_state(self, state, *, max_tokens, strict):
        return state.split()

    def encode_question_branches(self, questions, *, max_tokens, strict):
        return str(questions).split()

    def encode_final_sequence(self, state, questions, *, max_tokens, strict):
        return (state + str(questions)).split()

    def infer(self, state, questions):
        return {"kind": {"probabilities": {"yes": 0.8, "no": 0.2}, "selected": "yes", "confidence": 0.8}}, 2.0, 3.0


class FakeRetriever:
    async def retrieve(self, query):
        return retrieval()


class DecisionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_state_tracks_loading_ready_disabled_and_draining(self):
        entered, release = threading.Event(), threading.Event()
        def loader():
            entered.set()
            release.wait(2)
            return FakeAdapter()
        runtime = DecisionRuntime(FakeRetriever(), loader)
        self.assertEqual(runtime.state, "loading")
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        release.set()
        await asyncio.wrap_future(runtime._load_future)
        self.assertEqual(runtime.state, "ready")
        await runtime.close()
        self.assertEqual(runtime.state, "draining")

        disabled = DecisionRuntime(FakeRetriever(), FakeAdapter, enabled=False)
        self.assertEqual(disabled.state, "disabled")
        result = await disabled.evaluate(request())
        self.assertEqual(result.context.limitations.items, ("decision_runtime_disabled",))
        await disabled.close()

    async def test_background_load_and_evaluated_result(self):
        runtime = DecisionRuntime(FakeRetriever(), FakeAdapter)
        await asyncio.wrap_future(runtime._load_future)
        result = await runtime.evaluate(request())
        self.assertEqual(result.status, "evaluated")
        self.assertEqual(result.answers["kind"].selected, "yes")
        self.assertEqual(result.model.resolved_model, "fixture")
        await runtime.close()

    async def test_busy_request_rejected_before_second_inference(self):
        entered, release = threading.Event(), threading.Event()
        class Slow(FakeAdapter):
            def infer(self, state, questions):
                entered.set()
                release.wait(2)
                return super().infer(state, questions)
        runtime = DecisionRuntime(FakeRetriever(), Slow, response_deadline=1, stalled_after=0.08)
        await asyncio.wrap_future(runtime._load_future)
        first = asyncio.create_task(runtime.evaluate(request()))
        await asyncio.sleep(0.05)
        try:
            self.assertTrue(entered.is_set(), "worker did not reach inference")
            self.assertEqual(runtime.state, "busy")
            second = await runtime.evaluate(request())
            self.assertEqual(second.status, "unavailable")
            self.assertEqual(second.context.limitations.items, ("decision_runtime_busy",))
        finally:
            release.set()
        first_result = await first
        self.assertEqual(first_result.status, "evaluated", first_result.model_dump(mode="json"))
        await runtime.close()

    async def test_timeout_retains_admission_until_worker_finishes(self):
        entered, release = threading.Event(), threading.Event()
        class Slow(FakeAdapter):
            def infer(self, state, questions):
                entered.set()
                release.wait(2)
                return super().infer(state, questions)
        runtime = DecisionRuntime(FakeRetriever(), Slow, response_deadline=0.02, stalled_after=0.08)
        await asyncio.wrap_future(runtime._load_future)
        call = asyncio.create_task(runtime.evaluate(request()))
        await asyncio.sleep(0.05)
        timed = await call
        self.assertEqual(timed.status, "failed")
        self.assertEqual(timed.answers, {})
        busy = await runtime.evaluate(request())
        self.assertEqual(busy.context.limitations.items, ("decision_runtime_busy",))
        release.set()
        await asyncio.sleep(0.03)
        self.assertEqual((await runtime.evaluate(request())).status, "evaluated")
        await runtime.close()

    async def test_cancellation_retains_admission_until_actual_worker_completion(self):
        entered, release = threading.Event(), threading.Event()
        class Slow(FakeAdapter):
            def infer(self, state, questions):
                entered.set()
                release.wait(5)
                return super().infer(state, questions)
        runtime = DecisionRuntime(FakeRetriever(), Slow, response_deadline=1, stalled_after=0.15)
        await asyncio.wrap_future(runtime._load_future)
        first = asyncio.create_task(runtime.evaluate(request()))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            busy = await runtime.evaluate(request())
            self.assertEqual(busy.context.limitations.items, ("decision_runtime_busy",))
            await asyncio.sleep(0.18)
            self.assertTrue(runtime._stalled)
            self.assertEqual(runtime.state, "stalled")
            stalled = await runtime.evaluate(request())
            self.assertEqual(stalled.context.limitations.items, ("inference_worker_stalled",))
        finally:
            release.set()
        for _ in range(100):
            if runtime._worker_future.done():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(runtime._worker_future.done())
        self.assertEqual((await runtime.evaluate(request())).context.limitations.items,
                         ("inference_worker_stalled",))
        await runtime.close()

    async def test_stalled_worker_is_restart_only_after_threshold(self):
        entered, release = threading.Event(), threading.Event()
        class Slow(FakeAdapter):
            def infer(self, state, questions):
                entered.set()
                release.wait(5)
                return super().infer(state, questions)
        runtime = DecisionRuntime(FakeRetriever(), Slow, response_deadline=0.02, stalled_after=0.08)
        await asyncio.wrap_future(runtime._load_future)
        first = asyncio.create_task(runtime.evaluate(request()))
        try:
            await asyncio.sleep(0.05)
            timed = await first
            self.assertEqual(timed.status, "failed")
            self.assertEqual(timed.context.limitations.items, ("decision_response_deadline_exceeded",))
            self.assertTrue(entered.is_set())
            self.assertFalse(runtime._stalled)
            await asyncio.sleep(0.06)
            self.assertTrue(runtime._stalled)
            stalled = await runtime.evaluate(request())
            self.assertEqual(stalled.context.limitations.items, ("inference_worker_stalled",))
        finally:
            release.set()
        for _ in range(100):
            if runtime._worker_future.done():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(runtime._worker_future.done())
        # A native worker finishing after the stalled limit cannot reopen admission.
        self.assertEqual((await runtime.evaluate(request())).context.limitations.items,
                         ("inference_worker_stalled",))
        await runtime.close()

    async def test_shutdown_wait_is_bounded_and_does_not_release_active_work(self):
        entered, release = threading.Event(), threading.Event()
        class Slow(FakeAdapter):
            def infer(self, state, questions):
                entered.set()
                release.wait(5)
                return super().infer(state, questions)
        runtime = DecisionRuntime(FakeRetriever(), Slow, response_deadline=0.02, shutdown_wait=0.03)
        await asyncio.wrap_future(runtime._load_future)
        try:
            task = asyncio.create_task(runtime.evaluate(request()))
            await asyncio.sleep(0.05)
            await task
            self.assertTrue(entered.is_set())
            await asyncio.wait_for(runtime.close(), timeout=0.2)
            self.assertFalse(runtime._worker_future.done())
        finally:
            release.set()

    async def test_shutdown_wait_drains_worker_that_finishes_within_bound(self):
        entered, release = threading.Event(), threading.Event()
        class Slow(FakeAdapter):
            def infer(self, state, questions):
                entered.set()
                release.wait(2)
                return super().infer(state, questions)
        runtime = DecisionRuntime(FakeRetriever(), Slow, shutdown_wait=0.5)
        await asyncio.wrap_future(runtime._load_future)
        evaluation = asyncio.create_task(runtime.evaluate(request()))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        asyncio.get_running_loop().call_later(0.03, release.set)
        await runtime.close()
        result = await evaluation
        self.assertEqual(result.status, "evaluated")
        self.assertTrue(runtime._worker_future.done())

    async def test_retrieval_failure_returns_empty_sanitized_context(self):
        class Broken:
            async def retrieve(self, query):
                raise RuntimeError("contains secret URL and key")
        runtime = DecisionRuntime(Broken(), FakeAdapter)
        await asyncio.wrap_future(runtime._load_future)
        result = await runtime.evaluate(request())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.answers, {})
        self.assertEqual(result.context.items, ())
        self.assertEqual(result.context.limitations.items, ("decision_retrieval_unavailable",))
        await runtime.close()


if __name__ == "__main__":
    unittest.main()
