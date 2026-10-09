"""HTTP adapter for the pinned LightRAG v1.5.7 graph API."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Mapping

import httpx


def projection_id(workspace: str, kind: str, key: str) -> str:
    """Return a stable, workspace-scoped LightRAG node label."""
    digest = hashlib.sha256(f"{workspace}\0{kind}\0{key}".encode()).hexdigest()[:32]
    return f"ogr_{kind}_{digest}"


class LightRAGAdapter:
    """Project canonical directed facts through LightRAG's authenticated API.

    LightRAG v1.5.7 stores graph relationships as undirected edges. Each
    canonical relation is therefore represented by a dedicated fact node and
    two role-labelled edges, preserving direction without relying on endpoint
    ordering or replacing a subject/object pair's existing edge.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client = client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        try:
            response: httpx.Response | None = None
            for attempt in range(3):
                try:
                    response = await client.post(
                        f"{self.base_url}{path}",
                        json=payload,
                        headers={"X-API-Key": self.api_key},
                    )
                    if response.status_code < 500 or attempt == 2:
                        break
                except httpx.TransportError:
                    if attempt == 2:
                        raise
                await asyncio.sleep(0.1 * (2**attempt))
            assert response is not None
            response.raise_for_status()
            return response.json()
        finally:
            if owns_client:
                await client.aclose()

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        try:
            response = await client.get(
                f"{self.base_url}{path}", params=params,
                headers={"X-API-Key": self.api_key},
            )
            response.raise_for_status()
            return response.json()
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _metadata_description(description: str, metadata: Mapping[str, Any]) -> str:
        source_id = metadata.get("source_id")
        if not source_id:
            provenance = metadata.get("provenance", [])
            if provenance and isinstance(provenance[0], Mapping):
                source_id = provenance[0].get("source_id") or provenance[0].get("chunk_id") or provenance[0].get("document_id")
        parts = [description]
        canonical_id = metadata.get("canonical_fact_id") or metadata.get("canonical_entity_id")
        if canonical_id:
            parts.append(f"id={canonical_id}")
        if metadata.get("subject_id") and metadata.get("object_id"):
            parts.append(f"subject={metadata['subject_id']}")
            parts.append(f"object={metadata['object_id']}")
        if metadata.get("predicate"):
            parts.append(f"predicate={metadata['predicate']}")
        if metadata.get("directed") is not None:
            parts.append(f"directed={str(bool(metadata['directed'])).lower()}")
        if metadata.get("ontology_id") and metadata.get("ontology_version"):
            parts.append(f"ontology={metadata['ontology_id']}@{metadata['ontology_version']}")
        if metadata.get("workspace"):
            parts.append(f"workspace={metadata['workspace']}")
        if source_id:
            parts.append(f"source={source_id}")
        canonical_data = {
            key: metadata[key]
            for key in ("properties", "provenance")
            if key in metadata
        }
        if canonical_data:
            parts.append(
                "canonical_data="
                + json.dumps(canonical_data, sort_keys=True, separators=(",", ":"), default=str)
            )
        return " | ".join(parts)

    @staticmethod
    def _entity_data(
        *, description: str, entity_type: str, source_id: str, workspace: str,
        ontology_id: str, ontology_version: str, role: str | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "description": description,
            "entity_type": entity_type,
            "source_id": source_id,
            "workspace": workspace,
            "ontology_id": ontology_id,
            "ontology_version": ontology_version,
        }
        if role:
            data["fact_role"] = role
        return data

    async def project(self, fact: Mapping[str, Any]) -> dict[str, str]:
        """Idempotently project a stored canonical fact; return its graph IDs.

        Relation facts require subject/predicate/object IDs. Entity facts
        require an ``entity_type``. Provenance is retained on the projection;
        LightRAG source_id is derived from its first entry.
        """
        required = (
            "id", "kind", "workspace", "ontology_id", "ontology_version", "provenance",
        )
        missing = [name for name in required if not fact.get(name)]
        if missing:
            raise ValueError(f"fact is missing required projection fields: {', '.join(missing)}")

        workspace = str(fact["workspace"])
        fact_key = str(fact["id"])
        ontology_id = str(fact["ontology_id"])
        ontology_version = str(fact["ontology_version"])
        provenance = fact["provenance"]
        if not isinstance(provenance, list) or not provenance:
            raise ValueError("fact provenance must be a non-empty list")
        first_provenance = provenance[0]
        if not isinstance(first_provenance, Mapping):
            raise ValueError("fact provenance entries must be objects")
        source_id = str(first_provenance.get("source_id") or first_provenance.get("chunk_id") or first_provenance.get("document_id") or "")
        if not source_id:
            raise ValueError("fact provenance must include source_id, chunk_id, or document_id")

        if fact["kind"] == "entity":
            entity_type = str(fact.get("entity_type", ""))
            if not entity_type:
                raise ValueError("entity fact is missing entity_type")
            entity_id = projection_id(workspace, "entity", fact_key)
            properties = fact.get("properties", {})
            if not isinstance(properties, Mapping):
                raise ValueError("entity properties must be an object")
            display_name = str(properties.get("name") or fact_key)
            entity_metadata = {
                "canonical_entity_id": fact_key,
                "workspace": workspace,
                "ontology_id": ontology_id,
                "ontology_version": ontology_version,
                "source_id": source_id,
                "provenance": provenance,
                "properties": dict(properties),
            }
            await self._ensure_entity(
                entity_id,
                {
                    "description": self._metadata_description(
                        str(properties.get("description") or f"{entity_type}: {display_name}"),
                        entity_metadata,
                    ),
                    "entity_type": entity_type,
                    "source_id": source_id,
                },
            )
            return {"entity": entity_id}

        if fact["kind"] != "relation":
            raise ValueError("fact kind must be entity or relation")
        relation_fields = ("subject_id", "predicate", "object_id")
        missing = [name for name in relation_fields if not fact.get(name)]
        if missing:
            raise ValueError(f"relation fact is missing projection fields: {', '.join(missing)}")
        subject_value = str(fact["subject_id"])
        object_value = str(fact["object_id"])
        predicate = str(fact["predicate"])
        directed = bool(fact.get("directed", True))
        direction = "directed" if directed else "symmetric"
        subject_id = projection_id(workspace, "entity", subject_value)
        object_id = projection_id(workspace, "entity", object_value)
        fact_node_id = projection_id(workspace, "fact", fact_key)

        entities = (
            (subject_id, subject_value, "ONTOLOGY_ENTITY", "subject"),
            (object_id, object_value, "ONTOLOGY_ENTITY", "object"),
            (fact_node_id, fact_key, "ONTOLOGY_FACT", None),
        )
        for node_id, label, node_type, role in entities:
            if role is not None:
                # Never edit an already projected canonical entity while
                # processing a relation outbox item.
                endpoint = self._entity_data(
                    description=f"Canonical {role} entity: {label}",
                    entity_type=node_type,
                    source_id=source_id,
                    workspace=workspace,
                    ontology_id=ontology_id,
                    ontology_version=ontology_version,
                    role=role,
                )
                await self._ensure_endpoint_entity(node_id, endpoint)
                continue

            description = (
                f"{subject_value} {predicate} {object_value}. "
                f"Canonical {direction} fact; subject={subject_value}; predicate={predicate}; object={object_value}."
            )
            metadata = self._entity_data(
                description=self._metadata_description(
                    description,
                    {
                        "canonical_fact_id": fact_key,
                        "predicate": predicate,
                        "subject_id": subject_value,
                        "object_id": object_value,
                        "workspace": workspace,
                        "ontology_id": ontology_id,
                        "ontology_version": ontology_version,
                        "source_id": source_id,
                        "directed": directed,
                        "properties": fact.get("properties", {}),
                        "provenance": provenance,
                    },
                ),
                entity_type=node_type,
                source_id=source_id,
                workspace=workspace,
                ontology_id=ontology_id,
                ontology_version=ontology_version,
            )
            await self._ensure_entity(node_id, metadata)

        for source, target, role in (
            (subject_id, fact_node_id, "subject_to_fact"),
            (fact_node_id, object_id, "fact_to_object"),
        ):
            await self._ensure_relation(
                source,
                target,
                {
                    "description": self._metadata_description(
                        f"{role} for directed predicate {predicate}; fact {fact_key}",
                        {
                            "canonical_fact_id": fact_key,
                            "workspace": workspace,
                            "ontology_id": ontology_id,
                            "ontology_version": ontology_version,
                            "directed": directed,
                            "predicate": predicate,
                            "source_id": source_id,
                            "provenance": provenance,
                            "fact_role": role,
                        },
                    ),
                    "keywords": f"{predicate}, {role}, ontology={ontology_id}@{ontology_version}",
                    "source_id": source_id,
                    "weight": 1.0,
                    "workspace": workspace,
                    "ontology_id": ontology_id,
                    "ontology_version": ontology_version,
                    "directed": directed,
                    "fact_role": role,
                },
            )

        return {"subject": subject_id, "fact": fact_node_id, "object": object_id}

    async def _ensure_endpoint_entity(self, entity_name: str, data: dict[str, Any]) -> None:
        result = await self._get("/graph/entity/exists", {"name": entity_name})
        if isinstance(result, dict) and result.get("exists"):
            return
        try:
            await self._post("/graph/entity/create", {"entity_name": entity_name, "entity_data": data})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
            exists = await self._get("/graph/entity/exists", {"name": entity_name})
            if not isinstance(exists, dict) or not exists.get("exists"):
                raise

    async def _ensure_entity(self, entity_name: str, data: dict[str, Any]) -> None:
        try:
            await self._post("/graph/entity/create", {"entity_name": entity_name, "entity_data": data})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
            # v1.5.7 exposes no entity upsert. Editing the deterministic node
            # makes retry after a partial projection safe; all fields accepted
            # by its edit endpoint are strings.
            edit_data = {key: str(value) for key, value in data.items() if key in {"description", "entity_type", "source_id"}}
            await self._post("/graph/entity/edit", {"entity_name": entity_name, "updated_data": edit_data})

    async def _ensure_relation(self, source: str, target: str, data: dict[str, Any]) -> None:
        try:
            await self._post(
                "/graph/relation/create",
                {"source_entity": source, "target_entity": target, "relation_data": data},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
            edit_data = {key: str(value) for key, value in data.items() if key in {"description", "keywords", "source_id"}}
            await self._post(
                "/graph/relation/edit",
                {"source_id": source, "target_id": target, "updated_data": edit_data},
            )


def extraction_guidance(definition: Mapping[str, Any] | None) -> dict[str, Any]:
    """Generate a prompt profile from ontology vocabulary (guidance only)."""
    if definition is None:
        raise ValueError("ontology version not found")
    entity_lines = []
    for name, item in sorted(definition.get("entities", {}).items()):
        properties = item.get("properties", {})
        required = sorted(key for key, value in properties.items() if value.get("required"))
        fields = "; ".join(
            f"{key}: {value.get('type', 'any')}"
            + (f" enum={value['enum']}" if "enum" in value else "")
            + (" (required)" if value.get("required") else "")
            for key, value in sorted(properties.items())
        )
        entity_lines.append(
            f"- {name}: {item.get('description', '')}; properties: {fields or '(none)'}; required: {required or '(none)'}"
        )
    relation_lines = []
    for name, item in sorted(definition.get("relations", {}).items()):
        source_value = item.get("source", [])
        target_value = item.get("target", [])
        source = ", ".join([source_value] if isinstance(source_value, str) else source_value)
        target = ", ".join([target_value] if isinstance(target_value, str) else target_value)
        description = item.get("description", "")
        relation_lines.append(
            f"- {name} ({source} -> {target}; directed={bool(item.get('directed', True))}; "
            f"constraints={item.get('constraints', {})}): {description}".rstrip()
        )
    profile = [
        f"Ontology {definition.get('id')} version {definition.get('version')}.",
        "Extract only candidates supported by the source. Preserve source identifiers.",
        "Use the entity and relation names below as candidate labels; uncertain candidates need review.",
        "Return candidates as JSON with entity_type/properties and subject_id/predicate/object_id/provenance fields.",
        "Never invent missing required values or source identifiers; flag uncertain candidates for human review.",
        "This profile is prompt guidance and does not validate or govern ingestion.",
        "Entities:",
        *(entity_lines or ["- (none)" ]),
        "Relations:",
        *(relation_lines or ["- (none)"]),
    ]
    return {
        "ontology_id": definition.get("id"),
        "ontology_version": definition.get("version"),
        "profile": "\n".join(profile),
        "governance": "guidance_only",
    }
