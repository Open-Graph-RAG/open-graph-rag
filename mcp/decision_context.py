"""Deterministic, provenance-preserving context selection for Kev decisions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from decision_contract import (
    ContextLimitations,
    ContextReference,
    ContextTokenCounts,
    DecisionContext,
    DecisionContextItem,
    DecisionRequest,
    OmittedContextItem,
)
from decision_retrieval import GraphAssertion, RetrievedEvidence, RetrievedPassage


MAX_STATE_CHARACTERS = 24_000
MAX_STATE_TOKENS = 4_096
MAX_QUESTION_BRANCH_TOKENS = 1_024
MAX_FINAL_SEQUENCE_TOKENS = 8_192
CONTEXT_ORIGIN_INSTRUCTIONS = (
    "Evidence below is bounded decision context. Caller-supplied excerpts are unverified claims from the caller. "
    "Graph descriptions and relationships are graph-extracted assertions, not established facts. Verify assertions "
    "against the included original source passages. Missing status, dates, and versions are unknown."
)


class DecisionEncoder(Protocol):
    """Kev encoding seam. Implementations must use Kev's escaped strict encoder."""

    def encode_state(self, state_text: str, *, max_tokens: int, strict: bool) -> Sequence[int]: ...

    def encode_question_branches(
        self, questions: list[dict[str, Any]], *, max_tokens: int, strict: bool
    ) -> Sequence[int]: ...

    def encode_final_sequence(
        self,
        state_text: str,
        questions: list[dict[str, Any]],
        *,
        max_tokens: int,
        strict: bool,
    ) -> Sequence[int]: ...


@dataclass
class _Candidate:
    item: DecisionContextItem
    dedupe_key: tuple[str, str, str | None, str | None] | None
    source_ids: tuple[str, ...] = ()
    references: list[ContextReference] = field(default_factory=list)


def _normalized_content(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def evidence_fingerprint(source_locator: str, content: str, *, version: str | None = None,
                         status: str | None = None) -> str:
    """Stable novelty ID; retrieval time and request-local IDs are deliberately excluded."""
    material = json.dumps(
        [source_locator, _normalized_content(content), version, status],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def new_evidence_fingerprints(request: DecisionRequest, retrieval: RetrievedEvidence) -> tuple[str, ...]:
    """Return unseen fingerprints for supplied excerpts and source-linked retrieval.

    Request-local IDs and retrieval timestamps never affect novelty. Line endings
    are normalized for hashing only; context assembly retains the original text.
    """
    fingerprints: list[str] = []
    for evidence in request.supplied_evidence:
        fingerprints.append(evidence_fingerprint(
            evidence.source_locator, evidence.text, version=evidence.version, status=evidence.status,
        ))
    for passage in retrieval.passages:
        metadata = passage.upstream_metadata
        fingerprints.append(evidence_fingerprint(
            passage.source_locator or "", passage.content,
            version=_metadata_string(metadata, "version"),
            status=_metadata_string(metadata, "status"),
        ))
    for assertion in retrieval.assertions:
        fingerprints.append(evidence_fingerprint(
            assertion.source_locator or "", assertion.text,
            version=_metadata_string(assertion.upstream_metadata, "version"),
            status=_metadata_string(assertion.upstream_metadata, "status"),
        ))
    seen = set(request.seen_fingerprints)
    return tuple(dict.fromkeys(fingerprint for fingerprint in fingerprints if fingerprint not in seen))


def _metadata_string(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) else None


def _supplied_candidate(item: Any, index: int) -> _Candidate:
    reference = ContextReference(
        source_locator=item.source_locator,
        original_reference=item.original_reference,
        source_metadata={},
        caller_supplied=True,
        origin=item.origin,
        retrieved_at=item.retrieved_at,
        status=item.status,
        version=item.version,
        caller_evidence_id=item.id,
    )
    content = item.text
    dedupe = (item.source_locator, _normalized_content(content), item.version, item.status)
    return _Candidate(
        item=DecisionContextItem(id=f"s1-p{index}", kind="supplied_passage", text=content, references=(reference,)),
        dedupe_key=dedupe, source_ids=(f"s1-p{index}",), references=[reference],
    )


def _passage_candidate(passage: RetrievedPassage) -> _Candidate:
    metadata = passage.upstream_metadata
    reference = ContextReference(
        source_locator=passage.source_locator,
        original_reference=passage.original_reference,
        reference_id=passage.reference_id,
        upstream_chunk_id=passage.chunk_id,
        source_metadata={key: value for key, value in passage.upstream_metadata.items() if key != "content"},
        caller_supplied=False,
        origin="indexed LightRAG passage",
        retrieved_at=_metadata_string(metadata, "retrieved_at"),
        status=_metadata_string(metadata, "status"),
        version=_metadata_string(metadata, "version"),
    )
    dedupe = None if passage.source_locator is None else (
        passage.source_locator, _normalized_content(passage.content), reference.version, reference.status,
    )
    return _Candidate(
        item=DecisionContextItem(id=passage.local_id, kind="retrieved_passage", text=passage.content,
                                 references=(reference,)),
        dedupe_key=dedupe, source_ids=(passage.local_id,), references=[reference],
    )


def _assertion_candidate(assertion: GraphAssertion, supporting_ids: tuple[str, ...]) -> _Candidate:
    references = [ContextReference(
        source_locator=assertion.source_locator,
        original_reference=assertion.original_reference,
        reference_id=assertion.reference_id,
        source_ids=assertion.source_ids,
        source_metadata={key: value for key, value in assertion.upstream_metadata.items() if key != "description"},
        caller_supplied=False,
        origin=assertion.label,
        retrieved_at=_metadata_string(assertion.upstream_metadata, "retrieved_at"),
        status=_metadata_string(assertion.upstream_metadata, "status"),
        version=_metadata_string(assertion.upstream_metadata, "version"),
    )]
    references.extend(ContextReference(
        source_locator=source.source_locator,
        original_reference=source.original_reference,
        reference_id=source.reference_id,
        upstream_chunk_id=source.chunk_id,
        origin="supporting indexed passage",
        source_metadata={"chunk_id": source.chunk_id, "passage_id": source.passage_id},
    ) for source in assertion.supporting_sources)
    content = f"{assertion.label}: {assertion.text}"
    dedupe = None if assertion.source_locator is None else (
        assertion.source_locator, _normalized_content(content), references[0].version, references[0].status,
    )
    return _Candidate(
        item=DecisionContextItem(id=assertion.local_id, kind="graph_assertion", text=content,
                                 references=tuple(references), supporting_item_ids=supporting_ids),
        dedupe_key=dedupe, source_ids=(assertion.local_id,), references=references,
    )


def _merge_candidate(target: _Candidate, duplicate: _Candidate) -> None:
    target.source_ids += duplicate.source_ids
    target.references.extend(duplicate.references)
    target.item = target.item.model_copy(update={
        "references": tuple(target.references),
        "supporting_item_ids": tuple(dict.fromkeys(target.item.supporting_item_ids + duplicate.item.supporting_item_ids)),
    })


def _state_text(objective: str, selected: Sequence[_Candidate]) -> str:
    record = {
        "context_origin_instructions": CONTEXT_ORIGIN_INSTRUCTIONS,
        "objective": objective,
        "evidence": [candidate.item.model_dump(mode="json") for candidate in selected],
    }
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _questions_payload(request: DecisionRequest) -> list[dict[str, Any]]:
    result = []
    for identifier, question in request.questions.items():
        kev_question = question.kev_question()
        result.append({"id": identifier, **kev_question.model_dump(mode="json")})
    return result


def _counts(encoder: DecisionEncoder, state_text: str, questions: list[dict[str, Any]],
            sequence_limit: int) -> ContextTokenCounts:
    state_tokens = len(encoder.encode_state(state_text, max_tokens=MAX_STATE_TOKENS, strict=True))
    branch_tokens = len(encoder.encode_question_branches(
        questions, max_tokens=MAX_QUESTION_BRANCH_TOKENS, strict=True,
    ))
    final_tokens = len(encoder.encode_final_sequence(
        state_text, questions, max_tokens=sequence_limit, strict=True,
    ))
    return ContextTokenCounts(state=state_tokens, question_branches=branch_tokens,
                              final_sequence=final_tokens)


def build_decision_context(
    request: DecisionRequest,
    retrieval: RetrievedEvidence,
    encoder: DecisionEncoder,
    *,
    max_final_sequence_tokens: int = MAX_FINAL_SEQUENCE_TOKENS,
) -> DecisionContext:
    """Select mandatory then optional evidence without shortening any source text.

    Supplied excerpts, the first two distinct retrieved passages, and the first
    source-linked graph package are mandatory. A graph package is added together
    with every linked source passage not already present.
    """
    questions = _questions_payload(request)
    supplied = [_supplied_candidate(item, index) for index, item in enumerate(request.supplied_evidence, 1)]
    passages = [_passage_candidate(passage) for passage in retrieval.passages]

    # Canonicalize duplicates while retaining their available provenance. Only
    # duplicate content at a known common locator is safe to collapse.
    by_key: dict[tuple[str, str, str | None, str | None], _Candidate] = {}
    passage_alias: dict[str, str] = {}
    omitted: list[OmittedContextItem] = []
    all_passages: list[_Candidate] = []
    for candidate in [*supplied, *passages]:
        canonical = by_key.get(candidate.dedupe_key) if candidate.dedupe_key is not None else None
        if canonical is not None:
            _merge_candidate(canonical, candidate)
            omitted.append(OmittedContextItem(id=candidate.item.id, reason="duplicate"))
            for alias in candidate.source_ids:
                passage_alias[alias] = canonical.item.id
        else:
            if candidate.dedupe_key is not None:
                by_key[candidate.dedupe_key] = candidate
            all_passages.append(candidate)
            for alias in candidate.source_ids:
                passage_alias[alias] = candidate.item.id

    supplied_canonical = [
        by_key.get(candidate.dedupe_key, candidate) if candidate.dedupe_key is not None else candidate
        for candidate in supplied
    ]
    passage_canonical = [
        by_key.get(candidate.dedupe_key, candidate) if candidate.dedupe_key is not None else candidate
        for candidate in passages
    ]
    # A merged supplied/indexed duplicate is mandatory because the caller
    # supplied that excerpt; its indexed origin and references remain attached.
    selected: list[_Candidate] = []
    selected_ids: set[str] = set()

    def select(candidate: _Candidate) -> None:
        if candidate.item.id not in selected_ids:
            selected.append(candidate)
            selected_ids.add(candidate.item.id)

    for candidate in supplied_canonical:
        select(candidate)

    distinct_retrieved: list[_Candidate] = []
    seen_keys: set[tuple[str, str, str | None, str | None]] = set()
    for candidate in passage_canonical:
        if candidate.item.kind != "retrieved_passage":
            # A retrieved passage merged into supplied evidence is still a
            # distinct retrieved passage only if its own ID remains an alias.
            if any(alias.startswith("r1-p") for alias in candidate.source_ids):
                pass
            else:
                continue
        key = candidate.dedupe_key
        if key is not None and key in seen_keys:
            continue
        if key is not None:
            seen_keys.add(key)
        distinct_retrieved.append(candidate)
    mandatory_passages = distinct_retrieved[:2]
    for candidate in mandatory_passages:
        select(candidate)

    assertion_candidates: list[_Candidate] = []
    unresolved_ids = set(retrieval.unresolved_assertion_ids)
    for assertion in retrieval.assertions:
        support_ids = tuple(dict.fromkeys(
            passage_alias.get(support_id, support_id) for support_id in assertion.supporting_passage_ids
        ))
        support_candidates = [
            next((candidate for candidate in all_passages if candidate.item.id == support_id), None)
            for support_id in support_ids
        ]
        support_candidates = [candidate for candidate in support_candidates if candidate is not None]
        if not support_candidates:
            unresolved_ids.add(assertion.local_id)
            continue
        assertion_candidates.append(_assertion_candidate(assertion, support_ids))

    # The first linked assertion's support passages and claim form one mandatory
    # atomic graph package. Existing mandatory passages may satisfy its source set.
    first_package: tuple[list[_Candidate], _Candidate] | None = None
    for graph_candidate in assertion_candidates:
        support_candidates = [
            next((candidate for candidate in all_passages if candidate.item.id == support_id), None)
            for support_id in graph_candidate.item.supporting_item_ids
        ]
        support_candidates = [candidate for candidate in support_candidates if candidate is not None]
        if support_candidates:
            first_package = (support_candidates, graph_candidate)
            break
    if first_package:
        for candidate in first_package[0]:
            select(candidate)
        select(first_package[1])
    else:
        unresolved_ids.update(assertion.local_id for assertion in retrieval.assertions)

    omitted_ids = {item.id for item in omitted}
    for unresolved_id in sorted(unresolved_ids):
        if unresolved_id not in omitted_ids:
            omitted.append(OmittedContextItem(id=unresolved_id, reason="unresolved_source_link"))
            omitted_ids.add(unresolved_id)

    def attempt(items: list[_Candidate]) -> tuple[bool, str, ContextTokenCounts | None]:
        state = _state_text(request.objective, items)
        if len(state) > MAX_STATE_CHARACTERS:
            return False, state, None
        try:
            counts = _counts(encoder, state, questions, max_final_sequence_tokens)
        except (ValueError, OverflowError):
            return False, state, None
        fits = (counts.state <= MAX_STATE_TOKENS
                and counts.question_branches <= MAX_QUESTION_BRANCH_TOKENS
                and counts.final_sequence <= max_final_sequence_tokens)
        return fits, state, counts if fits else counts

    mandatory_fits, state, counts = attempt(selected)
    mandatory_failure = not mandatory_fits
    if mandatory_failure:
        # Mandatory excerpts and source passages cannot be cut to force a fit.
        status = "insufficient_context"
        limitations = ["mandatory_evidence_exceeds_context_budget"]
        if counts is None:
            counts = ContextTokenCounts(state=0, question_branches=0, final_sequence=0)
        for candidate in [*all_passages, *assertion_candidates]:
            for alias in candidate.source_ids:
                if alias not in omitted_ids:
                    omitted.append(OmittedContextItem(id=alias, reason="token_budget"))
                    omitted_ids.add(alias)
        state = ""
    else:
        status = "ready"
        limitations = []
        selected_ids = {candidate.item.id for candidate in selected}
        passage_by_alias = {
            alias: candidate for candidate in all_passages
            for alias in candidate.source_ids if alias.startswith("r1-p")
        }
        assertion_by_id = {candidate.item.id: candidate for candidate in assertion_candidates}
        events = retrieval.upstream_order or tuple([*passage_by_alias, *assertion_by_id])
        for event_id in events:
            if event_id in passage_by_alias:
                candidate = passage_by_alias[event_id]
                if candidate.item.id in selected_ids:
                    continue
                fits, candidate_state, candidate_counts = attempt([*selected, candidate])
                if fits and candidate_counts is not None:
                    selected.append(candidate)
                    selected_ids.add(candidate.item.id)
                    state, counts = candidate_state, candidate_counts
                elif event_id not in omitted_ids:
                    omitted.append(OmittedContextItem(id=event_id, reason="token_budget"))
                    omitted_ids.add(event_id)
            elif event_id in assertion_by_id:
                candidate = assertion_by_id[event_id]
                if candidate.item.id in selected_ids:
                    continue
                support_candidates = [
                    next((item for item in all_passages if item.item.id == support_id), None)
                    for support_id in candidate.item.supporting_item_ids
                ]
                package = [item for item in support_candidates if item is not None and item.item.id not in selected_ids]
                package.append(candidate)
                fits, candidate_state, candidate_counts = attempt([*selected, *package])
                if fits and candidate_counts is not None:
                    selected.extend(package)
                    selected_ids.update(item.item.id for item in package)
                    state, counts = candidate_state, candidate_counts
                else:
                    for item in package:
                        for alias in (item.item.id, *item.source_ids):
                            if alias not in selected_ids and alias not in omitted_ids:
                                omitted.append(OmittedContextItem(id=alias, reason="token_budget"))
                                omitted_ids.add(alias)

    has_source = any(item.item.kind in ("supplied_passage", "retrieved_passage") for item in selected)
    has_graph = any(item.item.kind == "graph_assertion" for item in selected)
    if not has_source or not has_graph:
        status = "insufficient_context"
        if not has_source:
            limitations.append("no_source_passage_available")
        if not has_graph:
            limitations.append("no_source_linked_graph_assertion_available")
    limitations.extend(
        f"unresolved_original_reference:{identifier}"
        for identifier in retrieval.unresolved_reference_ids
    )
    limitations.extend(
        f"unresolved_source_link:{link.assertion_id}:{link.source_id}"
        for link in retrieval.unresolved_source_links
    )

    if mandatory_failure:
        # Preserve explicit omitted IDs for all retrieval evidence that could not
        # be represented in the request; no partial inference is permitted.
        included: tuple[str, ...] = ()
    else:
        included = tuple(item.item.id for item in selected)
    return DecisionContext(
        state_text=state,
        items=tuple(candidate.item for candidate in selected) if not mandatory_failure else (),
        included_item_ids=included,
        omitted_items=tuple(omitted),
        limitations=ContextLimitations(items=tuple(dict.fromkeys(limitations))),
        token_counts=counts,
        status="ready" if status == "ready" and has_source and has_graph else "insufficient_context",
    )
