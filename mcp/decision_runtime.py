"""Single-flight runtime for optional Kev inference."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import time
from typing import Any, Callable, Protocol, Sequence

from decision_contract import (ContextLimitations, ContextTokenCounts, DecisionAnswer, DecisionContext,
                               DecisionQuestion, DecisionRequest, DecisionResult, DecisionTimings, ModelMetadata)
from decision_context import build_decision_context, new_evidence_fingerprints
from decision_retrieval import RetrievedEvidence, RetrievalError

RESPONSE_DEADLINE_SECONDS = 15
STALLED_SECONDS = 60
SHUTDOWN_WAIT_SECONDS = 20


class DecisionProvider(Protocol):
    """Provider seam: strict tokenization plus one raw question distribution per input."""
    metadata: ModelMetadata

    def encode_state(self, state_text: str, *, max_tokens: int, strict: bool) -> Sequence[int]: ...
    def encode_question_branches(self, questions: list[dict[str, Any]], *, max_tokens: int,
                                 strict: bool) -> Sequence[int]: ...
    def encode_final_sequence(self, state_text: str, questions: list[dict[str, Any]], *, max_tokens: int,
                              strict: bool) -> Sequence[int]: ...
    def infer(self, state_text: str, questions: dict[str, DecisionQuestion]) -> tuple[
        dict[str, DecisionAnswer], float, float
    ]: ...


class DecisionEvidenceSource(Protocol):
    async def retrieve(self, query: str) -> RetrievedEvidence: ...


def empty_context(limitation: str) -> DecisionContext:
    return DecisionContext(state_text="", items=(), included_item_ids=(), omitted_items=(),
                           limitations=ContextLimitations(items=(limitation,)),
                           token_counts=ContextTokenCounts(state=0, question_branches=0, final_sequence=0),
                           status="insufficient_context")


class DecisionRuntime:
    def __init__(self, retriever: DecisionEvidenceSource, adapter_loader: Callable[[], DecisionProvider], *, enabled: bool = True,
                 response_deadline: float = RESPONSE_DEADLINE_SECONDS,
                 stalled_after: float = STALLED_SECONDS,
                 shutdown_wait: float = SHUTDOWN_WAIT_SECONDS):
        self.retriever, self.adapter_loader = retriever, adapter_loader
        self.enabled = enabled
        self.response_deadline, self.stalled_after = response_deadline, stalled_after
        self.shutdown_wait = shutdown_wait
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kev-runtime")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._busy = False
        self._admission_token: object | None = None
        self._stalled = False
        self._closed = False
        self._admission = asyncio.Lock()
        self._load_future: Future | None = self._executor.submit(adapter_loader) if enabled else None
        self._adapter: Any = None
        self._load_started = time.monotonic()
        self._worker_future: Future | None = None

    @property
    def state(self) -> str:
        """Report the runtime's current lifecycle without exposing mutable controls."""
        if not self.enabled:
            return "disabled"
        if self._closed:
            return "draining"
        if self._stalled:
            return "stalled"
        if self._busy:
            return "busy"
        if self._adapter is not None:
            return "ready"
        if self._load_future is None or not self._load_future.done():
            return "loading"
        try:
            self._load_future.result()
        except Exception:
            return "failed"
        return "ready"

    async def evaluate(self, request: DecisionRequest) -> DecisionResult:
        started = time.perf_counter()
        if not self.enabled:
            return self._failure("unavailable", "decision_runtime_disabled", 0)
        if self._closed:
            return self._failure("unavailable", "decision_runtime_draining", 0)
        async with self._admission:
            if self._stalled:
                return self._failure("unavailable", "inference_worker_stalled", 0)
            if self._busy:
                return self._failure("unavailable", "decision_runtime_busy", 0)
            self._busy = True
            token = self._admission_token = object()
        loop = asyncio.get_running_loop()
        self._loop = loop
        # Warm loading is asynchronous and shares the sole owned worker. Do not queue an
        # inference behind model loading; the request gets an explicit loading outcome.
        load_future = self._load_future
        if self._adapter is None:
            if load_future is None or not load_future.done():
                self._release(token)
                return self._failure("unavailable", "decision_model_loading", 0)
            try:
                self._adapter = load_future.result()
            except Exception:
                self._release(token)
                return self._failure("unavailable", "decision_model_unavailable", 0)
        retrieval_started = time.perf_counter()
        try:
            remaining = max(0.001, self.response_deadline - (time.perf_counter() - started))
            retrieval = await asyncio.wait_for(self.retriever.retrieve(request.query), timeout=remaining)
        except asyncio.CancelledError:
            self._release(token)
            raise
        except asyncio.TimeoutError:
            self._release(token)
            return self._failure("failed", "decision_response_deadline_exceeded", 0)
        except (RetrievalError, Exception):
            self._release(token)
            return self._failure("failed", "decision_retrieval_unavailable", 0)
        retrieval_ms = (time.perf_counter() - retrieval_started) * 1000
        if self._closed:
            self._release(token)
            return self._failure("unavailable", "decision_runtime_draining", retrieval_ms)
        adapter = self._adapter

        def prepare_and_infer():
            context_started = time.perf_counter()
            context = build_decision_context(request, retrieval, adapter)
            context_ms = (time.perf_counter() - context_started) * 1000
            if context.status != "ready":
                return context, {}, context_ms
            answers, preparation_ms, inference_ms = adapter.infer(context.state_text, request.questions)
            return context, answers, context_ms + preparation_ms, inference_ms

        worker_future = self._executor.submit(prepare_and_infer)
        self._worker_future = worker_future

        def release_after_worker(_future: Future) -> None:
            try:
                loop.call_soon_threadsafe(self._release, token)
            except RuntimeError:
                # A bounded shutdown may finish after the owning ASGI loop closes.
                pass

        worker_future.add_done_callback(release_after_worker)
        loop.call_later(self.stalled_after, self._mark_stalled_if_running, worker_future)
        try:
            remaining = max(0.001, self.response_deadline - (time.perf_counter() - started))
            deadline = loop.time() + remaining
            while not worker_future.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                await asyncio.sleep(min(0.01, remaining))
            result = worker_future.result()
        except asyncio.TimeoutError:
            return self._failure("failed", "decision_response_deadline_exceeded", retrieval_ms)
        except asyncio.CancelledError:
            # Admission remains held by the future callback until native inference ends.
            raise
        except Exception:
            self._release(token)
            return self._failure("failed", "decision_inference_failed", retrieval_ms)
        self._release(token)
        if len(result) == 3:
            context, _answers, preparation_ms = result
            return DecisionResult(status="insufficient_context", context=context,
                                  novelty_fingerprints=new_evidence_fingerprints(request, retrieval),
                                  timings=DecisionTimings(retrieval_ms=retrieval_ms, preparation_ms=preparation_ms,
                                                          inference_ms=None))
        context, answers, preparation_ms, inference_ms = result
        return DecisionResult(status="evaluated", answers=answers, context=context,
                              novelty_fingerprints=new_evidence_fingerprints(request, retrieval),
                              timings=DecisionTimings(retrieval_ms=retrieval_ms, preparation_ms=preparation_ms,
                                                      inference_ms=inference_ms), model=adapter.metadata)

    def _release(self, token: object) -> None:
        # Completion callbacks can arrive after the caller has observed completion
        # and a subsequent request has acquired admission.
        if self._admission_token is token:
            self._busy = False
            self._admission_token = None

    def _mark_stalled_if_running(self, future: Future) -> None:
        if not future.done():
            self._stalled = True

    @staticmethod
    def _failure(status: str, limitation: str, retrieval_ms: float) -> DecisionResult:
        return DecisionResult(status=status, context=empty_context(limitation),
                              timings=DecisionTimings(retrieval_ms=retrieval_ms, preparation_ms=0,
                                                      inference_ms=None))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        future = self._worker_future or self._load_future
        if future is not None and not future.done():
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)),
                                       timeout=self.shutdown_wait)
            except asyncio.TimeoutError:
                pass
        self._executor.shutdown(wait=False, cancel_futures=False)
