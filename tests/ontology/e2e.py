"""Opt-in ontology -> PostgreSQL outbox -> pinned LightRAG retrieval smoke."""

import json
import hashlib
import asyncio
import os
import time
import urllib.error
import urllib.request


ONTOLOGY_URL = os.environ.get("ONTOLOGY_URL", "http://127.0.0.1:18010")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:29621")
TOKEN = os.environ.get("ONTOLOGY_ADMIN_TOKEN", "ontology-e2e-admin-token-00000000000000000000000000000000")
WORKSPACE = "ontology-e2e"
LIGHTRAG_KEY = "ontology-e2e-lightrag-key"


def request(url, *, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"{method} {url} returned HTTP {error.code}: {error.read().decode()}") from error


def ontology_headers():
    return {"Authorization": f"Bearer {TOKEN}", "X-Workspace": WORKSPACE, "Content-Type": "application/json"}


def main():
    definition = {
        "id": "ogr-e2e", "version": "1.0.0", "status": "draft",
        "entities": {
            "System": {"description": "A software system", "properties": {"name": {"type": "string", "required": True}}},
        },
        "relations": {
            "DEPENDS_ON": {"description": "One system depends on another", "source": ["System"], "target": ["System"], "directed": True},
        },
        "validation": {"unknown_entity_type": "reject", "unknown_relation_type": "reject", "missing_required_property": "reject"},
    }
    versions = request(f"{ONTOLOGY_URL}/v1/ontologies/ogr-e2e/versions", headers=ontology_headers())
    if not any(item.get("version") == "1.0.0" and item.get("status") == "published" for item in versions):
        request(f"{ONTOLOGY_URL}/v1/ontologies", method="POST", body={"definition": definition}, headers=ontology_headers())
        request(f"{ONTOLOGY_URL}/v1/ontologies/ogr-e2e/publish", method="POST", body={"version": "1.0.0"}, headers=ontology_headers())

    provenance = [{"document_id": "e2e-doc", "source_id": "e2e-chunk"}]
    for fact in (
        {"id": "e2e-system-a", "kind": "entity", "ontology_id": "ogr-e2e", "ontology_version": "1.0.0", "workspace": WORKSPACE, "entity_type": "System", "properties": {"name": "E2E System A"}, "provenance": provenance},
        {"id": "e2e-system-b", "kind": "entity", "ontology_id": "ogr-e2e", "ontology_version": "1.0.0", "workspace": WORKSPACE, "entity_type": "System", "properties": {"name": "E2E System B"}, "provenance": provenance},
        {"id": "e2e-directed-fact", "kind": "relation", "ontology_id": "ogr-e2e", "ontology_version": "1.0.0", "workspace": WORKSPACE, "subject_id": "e2e-system-a", "predicate": "DEPENDS_ON", "object_id": "e2e-system-b", "directed": True, "properties": {}, "provenance": provenance},
    ):
        existing = request(f"{ONTOLOGY_URL}/v1/facts", headers=ontology_headers())
        if not any(item.get("id") == fact["id"] for item in existing):
            request(f"{ONTOLOGY_URL}/v1/facts", method="POST", body={"fact": fact}, headers=ontology_headers())

    status = None
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        status = request(f"{ONTOLOGY_URL}/v1/projection-status", headers=ontology_headers())
        if status and all(item.get("delivered_at") for item in status):
            break
        time.sleep(1)
    else:
        raise AssertionError(f"projection did not complete: {status}")

    if os.environ.get("RUN_ADAPTER_RETRY") == "1":
        # Exercise real v1.5.7 duplicate-create/edit recovery after an item has
        # already been projected, including an entity already linked by a fact.
        from services.ontology.adapters.lightrag import LightRAGAdapter

        async def repeat_projection():
            adapter = LightRAGAdapter(os.environ["LIGHTRAG_URL"], LIGHTRAG_KEY)
            try:
                await adapter.project({
                    "id": "e2e-system-a", "kind": "entity", "ontology_id": "ogr-e2e",
                    "ontology_version": "1.0.0", "workspace": WORKSPACE,
                    "entity_type": "System", "properties": {"name": "E2E System A"},
                    "provenance": provenance,
                })
                await adapter.project({
                    "id": "e2e-directed-fact", "kind": "relation", "ontology_id": "ogr-e2e",
                    "ontology_version": "1.0.0", "workspace": WORKSPACE,
                    "subject_id": "e2e-system-a", "predicate": "DEPENDS_ON",
                    "object_id": "e2e-system-b", "directed": True, "properties": {},
                    "provenance": provenance,
                })
            finally:
                await adapter.close()

        asyncio.run(repeat_projection())

    digest = hashlib.sha256(f"{WORKSPACE}\0fact\0e2e-directed-fact".encode()).hexdigest()[:32]
    graph = request(f"{GATEWAY_URL}/graphs?label=ogr_fact_{digest}", headers={"X-API-Key": LIGHTRAG_KEY})
    graph_text = json.dumps(graph)
    if "e2e-directed-fact" not in graph_text or "DEPENDS_ON" not in graph_text:
        raise AssertionError(f"canonical fact missing from LightRAG graph projection: {graph_text[:1000]}")
    if not any(node.get("properties", {}).get("entity_type") == "System" for node in graph.get("nodes", [])):
        raise AssertionError("relation projection overwrote canonical entity type")
    if "ontology=ogr-e2e@1.0.0" not in graph_text or "e2e-chunk" not in graph_text:
        raise AssertionError("graph projection omitted ontology version or provenance source")

    query = request(
        f"{GATEWAY_URL}/query/data",
        method="POST",
        body={"query": "DEPENDS_ON e2e-directed-fact", "mode": "global", "top_k": 20},
        headers={"X-API-Key": LIGHTRAG_KEY, "Content-Type": "application/json"},
    )
    query_text = json.dumps(query)
    if "e2e-directed-fact" not in query_text or "DEPENDS_ON" not in query_text:
        raise AssertionError(f"query/data did not return projected graph sources: {query_text[:1500]}")
    if "ontology=ogr-e2e@1.0.0" not in query_text or "e2e-chunk" not in query_text:
        raise AssertionError("query/data omitted ontology version or provenance source")

    # Publish a compatible version, plan/apply the migration and wait until its
    # outbox rewrites every graph description with the new explicit version.
    target = dict(definition)
    target["version"] = "1.1.0"
    target["entities"] = dict(definition["entities"])
    target["entities"]["System"] = dict(definition["entities"]["System"])
    target["entities"]["System"]["properties"] = {
        **definition["entities"]["System"]["properties"],
        "lifecycle": {"type": "string", "enum": ["active", "retired"]},
    }
    request(f"{ONTOLOGY_URL}/v1/ontologies", method="POST", body={"definition": target}, headers=ontology_headers())
    request(f"{ONTOLOGY_URL}/v1/ontologies/ogr-e2e/publish", method="POST", body={"version": "1.1.0"}, headers=ontology_headers())
    plan = request(
        f"{ONTOLOGY_URL}/v1/ontologies/ogr-e2e/migrations/plan",
        method="POST",
        body={"from_version": "1.0.0", "definition": target},
        headers=ontology_headers(),
    )
    request(
        f"{ONTOLOGY_URL}/v1/ontologies/ogr-e2e/migrations/apply",
        method="POST", body={"plan_id": plan["id"]}, headers=ontology_headers(),
    )

    def wait_projection():
        current = None
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            current = request(f"{ONTOLOGY_URL}/v1/projection-status", headers=ontology_headers())
            if current and all(item.get("delivered_at") for item in current):
                return current
            time.sleep(1)
        raise AssertionError(f"migration projection did not complete: {current}")

    wait_projection()
    graph_after_migration = request(f"{GATEWAY_URL}/graphs?label=ogr_fact_{digest}", headers={"X-API-Key": LIGHTRAG_KEY})
    migrated_text = json.dumps(graph_after_migration)
    if "ontology=ogr-e2e@1.1.0" not in migrated_text:
        raise AssertionError(f"migration did not update LightRAG projection version: {migrated_text[:1000]}")
    if not any(node.get("properties", {}).get("entity_type") == "System" for node in graph_after_migration.get("nodes", [])):
        raise AssertionError("migration projection changed canonical entity type")
    if "e2e-chunk" not in migrated_text:
        raise AssertionError("migration projection lost the provenance source")
    migrated_query = request(
        f"{GATEWAY_URL}/query/data", method="POST",
        body={"query": "DEPENDS_ON e2e-directed-fact", "mode": "global", "top_k": 20},
        headers={"X-API-Key": LIGHTRAG_KEY, "Content-Type": "application/json"},
    )
    if "ontology=ogr-e2e@1.1.0" not in json.dumps(migrated_query):
        raise AssertionError("query/data did not return migrated ontology version")

    request(
        f"{ONTOLOGY_URL}/v1/ontologies/ogr-e2e/migrations/rollback",
        method="POST", body={"migration_id": plan["id"]}, headers=ontology_headers(),
    )
    wait_projection()
    graph_after_rollback = request(f"{GATEWAY_URL}/graphs?label=ogr_fact_{digest}", headers={"X-API-Key": LIGHTRAG_KEY})
    rolled_back_text = json.dumps(graph_after_rollback)
    if "ontology=ogr-e2e@1.0.0" not in rolled_back_text or "e2e-chunk" not in rolled_back_text:
        raise AssertionError("rollback did not restore ontology version and source in graph projection")
    if not any(node.get("properties", {}).get("entity_type") == "System" for node in graph_after_rollback.get("nodes", [])):
        raise AssertionError("rollback projection changed canonical entity type")
    print("PASS canonical fact API -> PostgreSQL outbox -> LightRAG v1.5.7 graph -> query/data")


if __name__ == "__main__":
    main()
