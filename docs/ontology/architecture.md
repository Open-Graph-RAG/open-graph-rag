# Ontology control plane architecture

The ontology service is the semantic control plane. PostgreSQL stores immutable ontology versions, canonical entities/facts, validation/audit records and a transactional projection outbox. Accepted facts are committed before LightRAG is updated. A retrying projector consumes leased outbox rows and calls the pinned LightRAG graph API. PostgreSQL and LightRAG are not one transaction; outbox state makes partial failure recoverable.

```text
authors / API clients
        |
        v
ontology API ---> PostgreSQL ontology registry + canonical facts + outbox
                                              |
                                              v
                                    LightRAG projector
                                              |
                                              v
                                      private LightRAG

LibreChat / MCP / retrieval clients ---> retrieval gateway ---> LightRAG
```

The projector writes stable workspace-scoped node IDs. For a directed fact `A P B`, it writes `A -> fact(P) -> B`, with role labels `subject_to_fact` and `fact_to_object`. The native edges remain undirected, but each edge touches a unique fact node, so the representation cannot collapse the canonical pair into one ambiguous edge. The fact node records the predicate, canonical ID, ontology ID/version and provenance.

The gateway forwards only authenticated `POST /query/data`, `POST /query`, graph retrieval `GET` routes, and `/health`. It overwrites the upstream API key with its configured secret. It does not expose document ingestion, graph mutation or arbitrary proxy paths. The ontology projector alone has backend network access for governed graph writes.

Ontology-derived extraction profiles can improve classification prompts. LightRAG v1.5.7 does not provide a verified deterministic hook for applying those profiles to all raw ingestion paths. Therefore direct raw ingestion may still create ungoverned graph data unless the deployment blocks that path. Governed facts enter retrieval only after deterministic ontology validation and canonical acceptance.
