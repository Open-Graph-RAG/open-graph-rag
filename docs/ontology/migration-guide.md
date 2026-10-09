# Ontology version changes and migrations

Published ontology versions are immutable. Create and validate a new version, then inspect a migration plan before applying a breaking change. A dry run must state affected canonical facts, validation failures and required mappings; it does not mutate records or projections.

Adding an entity type or optional property is usually compatible. Making an optional property required, removing or renaming a type, changing a relation's direction, or changing source/target compatibility is breaking and needs an explicit mapping or remediation. Never infer a direction change from LightRAG edges: its v1.5.7 graph relation endpoint stores undirected links. Canonical fact order remains authoritative.

Use `POST /v1/ontologies/{id}/migrations/plan` with the target definition and mappings to generate a dry run. A rename map handles entity and relation names. When a fact needs property changes, `renames.fact_properties` maps its entity fact ID to the **complete replacement properties object**; it is not merged with the old object. For example, to change `person-17` from `{ "name": "Ada", "email": "ada@example.test" }` to `{ "name": "Ada", "role": "Engineer" }`, include:

```json
{
  "from_version": "1.0.0",
  "definition": { "id": "ogr-core", "version": "2.0.0", "entities": {}, "relations": {} },
  "renames": {
    "fact_properties": {
      "person-17": { "name": "Ada", "role": "Engineer" }
    }
  }
}
```

Use the actual complete target ontology definition in place of the abbreviated `entities` and `relations` in this illustration. The dry run validates every transformed fact against it and reports required corrections before any mutation. Review the returned impact and validation errors, then use the apply endpoint only after each affected fact has a deterministic mapping. Apply publishes and activates the target version, records an audit event, and preserves the previous canonical facts for rollback. Rollback restores canonical ontology/fact state and reconciles the projection from the outbox; it does not assume a distributed transaction with LightRAG. Projection failures remain retryable and visible as unsynchronized outbox items.

Before applying in production, back up the PostgreSQL database using the repository backup procedure. Rehearse the plan and apply against a restored copy. Do not use the raw LightRAG graph as the migration source of truth; ungoverned extracted content cannot be safely classified by ontology migration alone.
