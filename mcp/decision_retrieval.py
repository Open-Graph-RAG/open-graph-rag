"""Bounded LightRAG retrieval and exact graph-to-chunk provenance linking."""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field


MAX_RESPONSE_BYTES = 1024 * 1024
GRAPH_SOURCE_SEPARATOR = "<SEP>"


class RetrievalError(ValueError):
    """A sanitized retrieval or upstream-contract failure."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetrievedPassage(_FrozenModel):
    local_id: str
    chunk_id: str
    content: str
    source_locator: str | None = None
    reference_id: str | None = None
    original_reference: dict[str, Any] | None = None
    upstream_metadata: dict[str, Any] = Field(default_factory=dict)


class GraphSourceReference(_FrozenModel):
    passage_id: str
    chunk_id: str
    source_locator: str | None = None
    reference_id: str | None = None
    original_reference: dict[str, Any] | None = None


class UnresolvedGraphSource(_FrozenModel):
    assertion_id: str
    source_id: str


class GraphAssertion(_FrozenModel):
    local_id: str
    kind: str
    text: str
    source_ids: tuple[str, ...]
    unresolved_source_ids: tuple[str, ...]
    supporting_passage_ids: tuple[str, ...]
    supporting_sources: tuple[GraphSourceReference, ...]
    source_locator: str | None = None
    reference_id: str | None = None
    original_reference: dict[str, Any] | None = None
    upstream_metadata: dict[str, Any] = Field(default_factory=dict)
    label: str = "graph-extracted assertion"


class RetrievedEvidence(_FrozenModel):
    passages: tuple[RetrievedPassage, ...]
    assertions: tuple[GraphAssertion, ...]
    unresolved_assertion_ids: tuple[str, ...]
    unresolved_reference_ids: tuple[str, ...]
    unresolved_source_links: tuple[UnresolvedGraphSource, ...]
    references: tuple[dict[str, Any], ...]
    metadata: dict[str, Any]
    status: str
    message: str
    raw_data: dict[str, Any]
    upstream_order: tuple[str, ...]


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _source_ids(value: Any) -> tuple[str, ...]:
    """LightRAG serializes graph evidence chunk IDs using GRAPH_FIELD_SEP (<SEP>)."""
    if not isinstance(value, str) or not value:
        return ()
    return tuple(dict.fromkeys(part for part in value.split(GRAPH_SOURCE_SEPARATOR) if part))


def _reference_index(references: list[Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for reference in references:
        if isinstance(reference, dict) and isinstance(reference.get("reference_id"), str):
            result.setdefault(reference["reference_id"], []).append(reference)
    return result


def _match_reference(reference_id: str | None, locator: str | None,
                     index: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    if reference_id is None:
        return None
    candidates = index.get(reference_id, [])
    if locator is not None:
        same_locator = [ref for ref in candidates if ref.get("file_path") == locator]
        if len(same_locator) == 1:
            return same_locator[0]
        if any(isinstance(ref.get("file_path"), str) for ref in candidates):
            return None
    return candidates[0] if len(candidates) == 1 else None


def normalize_retrieval_response(payload: Any) -> RetrievedEvidence:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise RetrievalError("LightRAG returned an invalid decision response")
    data = payload["data"]
    chunks = data.get("chunks", [])
    references = data.get("references", [])
    entities = data.get("entities", [])
    relationship_key = "relationships" if "relationships" in data else "relations"
    relationships = data.get(relationship_key, [])
    if not all(isinstance(value, list) for value in (chunks, references, entities, relationships)):
        raise RetrievalError("LightRAG returned an invalid decision response")

    ref_index = _reference_index(references)
    chunks_by_id: dict[str, RetrievedPassage] = {}
    passages: list[RetrievedPassage] = []
    unresolved_references: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        if not isinstance(chunk, dict):
            raise RetrievalError("LightRAG returned an invalid decision response")
        chunk_id, content = _string(chunk.get("chunk_id")), _string(chunk.get("content"))
        if not chunk_id or content is None or chunk_id in chunks_by_id:
            raise RetrievalError("LightRAG returned an invalid decision response")
        locator = _string(chunk.get("file_path"))
        reference_id = _string(chunk.get("reference_id"))
        original_reference = _match_reference(reference_id, locator, ref_index)
        if locator is None and original_reference is not None:
            locator = _string(original_reference.get("file_path"))
        if reference_id is not None and original_reference is None:
            unresolved_references.append(f"r1-p{index}")
        passage = RetrievedPassage(
            local_id=f"r1-p{index}", chunk_id=chunk_id, content=content,
            source_locator=locator, reference_id=reference_id,
            original_reference=original_reference,
            upstream_metadata=dict(chunk),
        )
        passages.append(passage)
        chunks_by_id[chunk_id] = passage

    assertions: list[GraphAssertion] = []
    unresolved: list[str] = []
    unresolved_sources: list[UnresolvedGraphSource] = []
    graph_rows = [("entity", row) for row in entities] + [("relationship", row) for row in relationships]
    for index, (kind, row) in enumerate(graph_rows, 1):
        local_id = f"r1-g{index}"
        if not isinstance(row, dict):
            unresolved.append(local_id)
            continue
        source_ids = _source_ids(row.get("source_id"))
        linked = tuple(chunks_by_id[source_id] for source_id in source_ids if source_id in chunks_by_id)
        unresolved_ids = tuple(source_id for source_id in source_ids if source_id not in chunks_by_id)
        if not linked:
            unresolved.append(local_id)
            unresolved_sources.extend(UnresolvedGraphSource(assertion_id=local_id, source_id=source_id)
                                     for source_id in unresolved_ids)
            continue
        unresolved_sources.extend(UnresolvedGraphSource(assertion_id=local_id, source_id=source_id)
                                 for source_id in unresolved_ids)
        if kind == "entity":
            name, description = _string(row.get("entity_name")), _string(row.get("description"))
            assertion_text = f"Entity {name or '[unknown name]'}: {description or '[no description provided]'}"
        else:
            source, target = _string(row.get("src_id")), _string(row.get("tgt_id"))
            description = _string(row.get("description"))
            assertion_text = f"Relationship {source or '[unknown source]'} -> {target or '[unknown target]'}: {description or '[no description provided]'}"
        first = linked[0]
        assertion_reference_id = _string(row.get("reference_id"))
        assertion_locator = _string(row.get("file_path")) or first.source_locator
        assertion_reference = _match_reference(assertion_reference_id, assertion_locator, ref_index)
        if assertion_reference_id is not None and assertion_reference is None:
            unresolved_references.append(local_id)
        assertions.append(GraphAssertion(
            local_id=local_id, kind=kind, text=assertion_text,
            source_ids=source_ids,
            unresolved_source_ids=unresolved_ids,
            supporting_passage_ids=tuple(passage.local_id for passage in linked),
            supporting_sources=tuple(GraphSourceReference(
                passage_id=passage.local_id, chunk_id=passage.chunk_id,
                source_locator=passage.source_locator,
                reference_id=passage.reference_id,
                original_reference=passage.original_reference,
            ) for passage in linked),
            source_locator=assertion_locator,
            reference_id=assertion_reference_id,
            original_reference=(assertion_reference if assertion_reference_id is not None else None),
            upstream_metadata=dict(row),
        ))

    metadata = payload.get("metadata", {})
    entity_ids = [f"r1-g{index}" for index in range(1, len(entities) + 1)]
    relation_ids = [f"r1-g{len(entities) + index}" for index in range(1, len(relationships) + 1)]
    upstream_order: list[str] = []
    # LightRAG's response arrays have stable semantic order. JSON object key
    # ordering is transport detail, so preserve order within each array while
    # emitting chunks first, then entities, then relationships.
    for key in ("chunks", "entities", relationship_key):
        if key not in data:
            continue
        if key == "chunks":
            upstream_order.extend(passage.local_id for passage in passages)
        elif key == "entities":
            upstream_order.extend(entity_ids)
        elif key == relationship_key:
            upstream_order.extend(relation_ids)
    return RetrievedEvidence(
        passages=tuple(passages), assertions=tuple(assertions),
        unresolved_assertion_ids=tuple(unresolved),
        unresolved_reference_ids=tuple(dict.fromkeys(unresolved_references)),
        unresolved_source_links=tuple(unresolved_sources),
        references=tuple(dict(item) for item in references if isinstance(item, dict)),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
        status=str(payload.get("status", "unknown")),
        message=str(payload.get("message", "")), raw_data=dict(data),
        upstream_order=tuple(upstream_order),
    )


class DecisionRetriever:
    """Calls only LightRAG's fixed authenticated `/query/data` decision path."""

    def __init__(self, base_url: str, api_key: str, *, client: httpx.AsyncClient | None = None):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client

    async def retrieve(self, query: str) -> RetrievedEvidence:
        body = {
            "query": query,
            "mode": "mix",
            "top_k": 12,
            "chunk_top_k": 8,
            "max_entity_tokens": 1500,
            "max_relation_tokens": 1500,
            "max_total_tokens": 6000,
            "enable_rerank": False,
        }
        headers = {"X-API-Key": self._api_key}
        try:
            if self._client is not None:
                raw = await self._request(self._client, body, headers)
            else:
                async with httpx.AsyncClient(timeout=httpx.Timeout(150, connect=10)) as client:
                    raw = await self._request(client, body, headers)
        except httpx.TimeoutException as exc:
            raise RetrievalError("LightRAG decision retrieval timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise RetrievalError(f"LightRAG decision retrieval returned HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise RetrievalError("LightRAG decision retrieval failed") from exc
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RetrievalError("LightRAG returned invalid JSON") from exc
        return normalize_retrieval_response(payload)

    async def _request(self, client: httpx.AsyncClient, body: dict[str, Any],
                       headers: dict[str, str]) -> bytes:
        async with client.stream("POST", f"{self._base_url}/query/data", json=body, headers=headers) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise RetrievalError("LightRAG decision response exceeds 1 MiB")
                chunks.append(chunk)
            return b"".join(chunks)
