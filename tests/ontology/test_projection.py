import json
import unittest
from contextlib import contextmanager

import httpx

from services.ontology.adapters.lightrag import LightRAGAdapter, extraction_guidance, projection_id
from services.ontology.projection import sync_outbox


class ProjectionAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_fact_node_projection_preserves_direction_and_workspace(self):
        requests = []

        async def handler(request):
            requests.append(request)
            if request.url.path == "/graph/entity/exists":
                return httpx.Response(200, json={"exists": True})
            return httpx.Response(200, json={"status": "success"})

        adapter = LightRAGAdapter(
            "http://lightrag:9621", "secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        fact = {
            "id": "fact-1", "kind": "relation", "ontology_id": "ogr-core",
            "ontology_version": "1.0.0", "workspace": "acme",
            "subject_id": "system-a", "predicate": "DEPENDS_ON",
            "object_id": "system-b", "properties": {"criticality": "high"},
            "provenance": [{"document_id": "doc-1", "source_id": "chunk-1"}],
        }
        ids = await adapter.project(fact)
        self.assertEqual(ids["fact"], projection_id("acme", "fact", "fact-1"))
        self.assertNotEqual(
            projection_id("acme", "entity", "system-a"),
            projection_id("other", "entity", "system-a"),
        )
        self.assertEqual([request.url.path for request in requests], [
            "/graph/entity/exists", "/graph/entity/exists", "/graph/entity/create",
            "/graph/relation/create", "/graph/relation/create",
        ])
        payloads = [json.loads(request.content) for request in requests if request.method == "POST"]
        self.assertEqual(requests[0].headers["X-API-Key"], "secret")
        self.assertEqual(payloads[0]["entity_data"]["entity_type"], "ONTOLOGY_FACT")
        self.assertIn("ontology=ogr-core@1.0.0", payloads[0]["entity_data"]["description"])
        self.assertIn("directed=true", payloads[0]["entity_data"]["description"])
        self.assertEqual(payloads[1]["source_entity"], ids["subject"])
        self.assertEqual(payloads[1]["target_entity"], ids["fact"])
        self.assertEqual(payloads[1]["relation_data"]["fact_role"], "subject_to_fact")
        self.assertIn("ontology=ogr-core@1.0.0", payloads[1]["relation_data"]["description"])
        self.assertTrue(payloads[1]["relation_data"]["directed"])
        self.assertEqual(payloads[2]["source_entity"], ids["fact"])
        self.assertEqual(payloads[2]["target_entity"], ids["object"])
        self.assertEqual(payloads[2]["relation_data"]["source_id"], "chunk-1")
        self.assertFalse(any(request.url.path.endswith("/graph/entity/edit") for request in requests))
        await adapter.close()

    async def test_symmetric_relation_is_marked_as_symmetric(self):
        writes = []

        async def handler(request):
            if request.method == "POST":
                writes.append((request.url.path, json.loads(request.content)))
            if request.url.path == "/graph/entity/exists":
                return httpx.Response(200, json={"exists": True})
            return httpx.Response(200, json={"status": "success"})

        adapter = LightRAGAdapter(
            "http://lightrag:9621", "secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        fact = {
            "id": "symmetric-fact", "kind": "relation", "ontology_id": "ogr-core",
            "ontology_version": "1.0.0", "workspace": "acme", "subject_id": "a",
            "predicate": "RELATED_TO", "object_id": "b", "directed": False,
            "provenance": [{"document_id": "doc", "source_id": "chunk"}],
        }
        await adapter.project(fact)
        fact_payload = writes[0][1]["entity_data"]
        self.assertIn("Canonical symmetric fact", fact_payload["description"])
        self.assertIn("directed=false", fact_payload["description"])
        await adapter.close()

    async def test_relation_never_overwrites_existing_entity_type(self):
        requests = []
        created_entities = set()

        async def handler(request):
            requests.append(request)
            if request.url.path == "/graph/entity/exists":
                return httpx.Response(200, json={"exists": True})
            if request.url.path == "/graph/entity/create":
                name = json.loads(request.content)["entity_name"]
                if name in created_entities:
                    return httpx.Response(400, json={"detail": "already exists"})
                created_entities.add(name)
            return httpx.Response(200, json={"status": "success"})

        adapter = LightRAGAdapter(
            "http://lightrag:9621", "secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        entity = {
            "id": "system-a", "kind": "entity", "ontology_id": "ogr-core",
            "ontology_version": "1.1.0", "workspace": "acme", "entity_type": "System",
            "properties": {"name": "System A"},
            "provenance": [{"document_id": "doc-1", "source_id": "chunk-1"}],
        }
        await adapter.project(entity)
        migrated_entity = {**entity, "ontology_version": "1.2.0"}
        await adapter.project(migrated_entity)
        relation = {
            "id": "rel-1", "kind": "relation", "ontology_id": "ogr-core",
            "ontology_version": "1.2.0", "workspace": "acme", "subject_id": "system-a",
            "predicate": "DEPENDS_ON", "object_id": "system-b", "properties": {},
            "provenance": entity["provenance"],
        }
        await adapter.project(relation)
        entity_create = next(json.loads(r.content) for r in requests if r.url.path == "/graph/entity/create")
        description = entity_create["entity_data"]["description"]
        self.assertEqual(entity_create["entity_data"]["entity_type"], "System")
        self.assertIn("ontology=ogr-core@1.1.0", description)
        entity_edit = next(json.loads(r.content) for r in requests if r.url.path == "/graph/entity/edit")
        updated_description = entity_edit["updated_data"]["description"]
        self.assertIn("ontology=ogr-core@1.2.0", updated_description)
        self.assertIn('"name":"System A"', updated_description)
        self.assertIn('"source_id":"chunk-1"', updated_description)
        await adapter.close()

    async def test_missing_provenance_is_rejected(self):
        adapter = LightRAGAdapter("http://lightrag", "key")
        fact = {
            "id": "f", "workspace": "w", "ontology_id": "o", "ontology_version": "1.0.0",
            "subject_id": "a", "predicate": "R", "object_id": "b", "provenance": [],
        }
        with self.assertRaisesRegex(ValueError, "provenance"):
            await adapter.project(fact)

    async def test_outbox_is_scoped_to_the_configured_lightrag_workspace(self):
        class StoreStub:
            def __init__(self, row):
                self.row = row
                self.failed = []
                self.completed = []
                self.leased_workspace = None

            def lease_outbox(self, limit=10, workspace=None):
                self.leased_workspace = workspace
                return [self.row]

            @contextmanager
            def projection_guard(self, row):
                yield True

            def complete_outbox(self, outbox_id, lease_token):
                self.completed.append((outbox_id, lease_token))

            def fail_outbox(self, outbox_id, error, lease_token):
                self.failed.append((outbox_id, error, lease_token))

        class AdapterStub:
            def __init__(self):
                self.projected = []

            async def project(self, fact):
                self.projected.append(fact)

        valid_fact = {
            "id": "f", "workspace": "workspace-a", "ontology_id": "o",
            "ontology_version": "1.0.0", "kind": "entity",
        }
        row = {"id": 1, "workspace": "workspace-b", "fact": valid_fact, "lease_token": "t"}
        store, adapter = StoreStub(row), AdapterStub()
        result = await sync_outbox(store, adapter, workspace="workspace-a")
        self.assertEqual(store.leased_workspace, "workspace-a")
        self.assertEqual(adapter.projected, [])
        self.assertEqual(result["failed"], 1)
        self.assertIn("does not match configured LightRAG workspace", store.failed[0][1])

    async def test_extraction_guidance_includes_property_contract_and_uncertainty(self):
        profile = extraction_guidance({
            "id": "ogr-core", "version": "1.0.0",
            "entities": {"System": {"description": "Software", "properties": {"name": {"type": "string", "required": True}}}},
            "relations": {"DEPENDS_ON": {"description": "Dependency", "source": "System", "target": "System", "directed": True, "constraints": {"allow_self_reference": False}}},
        })["profile"]
        self.assertIn("name: string (required)", profile)
        self.assertIn("System -> System", profile)
        self.assertIn("allow_self_reference", profile)
        self.assertIn("uncertain candidates for human review", profile)


if __name__ == "__main__":
    unittest.main()
